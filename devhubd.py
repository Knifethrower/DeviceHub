#!/usr/bin/env python3
"""DeviceHub server: shared SSH connections, device locks with check-backs, live status, web UI.

Runs on Linux or in WSL on Windows: the shared connections are OpenSSH ControlMaster sockets
(/tmp/<id>.sock) that every client's own `ssh -S` commands reuse. Python 3.8+, standard library only.

  python3 devhubd.py      serve http://127.0.0.1:8765 (DEVHUB_PORT), state in ./data (DEVHUB_DATA)

Passwords never leave this process except through a one-shot FIFO to ssh's askpass helper, and the
API never returns them. At rest they are encrypted: with Windows DPAPI (the Windows user) under WSL,
with openssl and a key file only this user can read (data/secret.key) on Linux.
"""
import base64, errno, hashlib, http.server, json, os, re, secrets, shlex, shutil, signal, socket
import queue, struct, subprocess, sys, threading, time, traceback, urllib.parse, zipfile, zlib
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.abspath(os.environ.get("DEVHUB_DATA") or os.path.join(HERE, "data"))
PORT = int(os.environ.get("DEVHUB_PORT") or 8765)
FILE_PORT = int(os.environ.get("DEVHUB_FILE_PORT") or 8766)   # the temporary file server for installs (local mode)
BIND = os.environ.get("DEVHUB_BIND") or "127.0.0.1"   # server mode (devhubsrv.py) listens on the LAN
RUN = "/tmp/devhub-%d-%d" % (os.getuid(), PORT)
ASKPASS = os.path.join(RUN, "askpass.sh")
POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
VERSION = "1.0"


def detect_platform():
    """'wsl' (Windows: DPAPI secrets, Windows toasts) or 'linux'. DEVHUB_PLATFORM forces one."""
    forced = (os.environ.get("DEVHUB_PLATFORM") or "").lower()
    if forced in ("wsl", "linux"):
        return forced
    try:
        with open("/proc/version") as f:
            v = f.read().lower()
    except OSError:
        v = ""
    return "wsl" if "microsoft" in v and os.path.exists(POWERSHELL) else "linux"


PLATFORM = detect_platform()

DEFAULT_SETTINGS = {
    "user_name": "user",           # holder name for locks taken in the web UI
    "default_ttl_min": 30,         # lease when acquire gives no --for
    "max_ttl_min": 360,
    "unseen_grace_min": 20,        # answer time while the holder has not seen the question yet
    "check_grace_min": 10,         # answer time once the question reached the holder
    "auto_release": "when_clean",  # stale locks: never | when_clean | always
    "stale_release_min": 30,       # ...after being stale this long
    "idle_release_min": 30,        # ...and the holder idle this long
    "watch_extend_min": 10,        # a running watched job keeps the lease this far ahead
    "status_interval_s": 60,
    "toasts": True,                # Windows notifications for things that need the user
    "approval_timeout_min": 10,    # 'auto' approval: a session's request counts as yes after this long
    "approval_chain_min": 10,      # request chains: within this long after an approval (or an approved session's end)
                                   # the next request on that device is approved at once (0 = off)
    "battery_alert_pct": 10,       # off the charger at or below this: notify the user (0 = off)
    "battery_shutdown_pct": 3,     # off the charger at or below this: shut the device down cleanly (0 = off)
    "away_mode": False,            # the user's Away mode: sessions' requests for a free device are approved at once
    "notify_cmd": "",              # shell command for notifications (env DEVHUB_TITLE/TEXT/URL/KIND), any platform
    "retrocatalog": False,         # device type list + specs from retrocatalog.com (reads its public pages on demand)
    # Install on all: each device's harbourmaster downloads the zip from DeviceHub over HTTP (with an md5 check) instead
    # of DeviceHub copying it to every device over SSH. Off under WSL, whose network LAN devices can't reach.
    "install_serve": PLATFORM != "wsl",
}

# Defaults filled in when a CFW is picked in the editor (every field stays editable).
CFW_PRESETS = {
    "Knulli": {"user": "root", "auth": "password", "ports_dir": "/userdata/roms/ports", "smb_share": "share",
               "fe_proc": "emulationstation",
               "fe_start": "HOME=/userdata/system XDG_RUNTIME_DIR=/var/run setsid nohup /etc/init.d/S31emulationstation start >/dev/null 2>&1 </dev/null &",
               "fe_stop": "/etc/init.d/S31emulationstation stop",
               "fe_reload": "curl -fs -m 30 http://127.0.0.1:1234/reloadgames >/dev/null"},
    "Batocera": {"user": "root", "auth": "password", "ports_dir": "/userdata/roms/ports", "smb_share": "share",
                 "fe_proc": "emulationstation",
                 "fe_start": "HOME=/userdata/system XDG_RUNTIME_DIR=/var/run setsid nohup /etc/init.d/S31emulationstation start >/dev/null 2>&1 </dev/null &",
                 "fe_stop": "/etc/init.d/S31emulationstation stop",
               "fe_reload": "curl -fs -m 30 http://127.0.0.1:1234/reloadgames >/dev/null"},
    "ROCKNIX": {"user": "root", "auth": "key", "ports_dir": "/storage/roms/ports", "smb_share": "roms",
                "fe_proc": "emulationstation",
                "fe_start": "systemctl start essway.service 2>/dev/null || systemctl start emustation.service",
                "fe_stop": "systemctl stop essway.service 2>/dev/null; systemctl stop emustation.service 2>/dev/null; true"},
    "JELOS": {"user": "root", "auth": "password", "ports_dir": "/storage/roms/ports", "smb_share": "roms",
              "fe_proc": "emulationstation", "fe_start": "systemctl start emustation.service",
              "fe_stop": "systemctl stop emustation.service"},
    "AmberELEC": {"user": "root", "auth": "password", "ports_dir": "/storage/roms/ports", "smb_share": "roms",
                  "fe_proc": "emulationstation",
                  # ES's own screen-off screensaver switches the backlight off; a restart can leave it off (dark screen)
                  "fe_start": "systemctl start emustation.service; echo 0 > /sys/class/backlight/backlight/bl_power",
                  "fe_stop": "systemctl stop emustation.service"},
    "dArkOS": {"user": "ark", "auth": "password", "ports_dir": "/roms/ports", "smb_share": "roms",
               "fe_proc": "emulationstation", "fe_start": "sudo systemctl start emulationstation",
               "fe_stop": "sudo systemctl stop emulationstation"},
    "ArkOS": {"user": "ark", "auth": "password", "ports_dir": "/roms/ports", "smb_share": "roms",
              "fe_proc": "emulationstation", "fe_start": "sudo systemctl start emulationstation",
              "fe_stop": "sudo systemctl stop emulationstation"},
    # FRONTEND is a shell function in muOS's func.sh (interactive shells have it; a plain SSH command loads it here,
    # after /etc/profile for the environment it expects). The start runs in the background so the SSH command returns.
    # frontend.sh is the front end's loop (the mux* menus come and go under it); it is gone after FRONTEND stop.
    "muOS": {"user": "root", "auth": "key", "ports_dir": "", "smb_share": "", "fe_proc": "frontend.sh",
             "fe_start": "(. /etc/profile >/dev/null 2>&1; . /opt/muos/script/var/func.sh; FRONTEND start) >/dev/null 2>&1 </dev/null &",
             "fe_stop": ". /etc/profile >/dev/null 2>&1; . /opt/muos/script/var/func.sh && FRONTEND stop"},
    "Other": {},
}

DEVICE_DEFAULTS = {
    "id": "", "name": "", "type": "", "cfw": "Other", "host": "", "port": 22, "user": "root",
    "auth": "manual",            # password (DeviceHub opens the connection) | key | manual (user opens it)
    "socket": "", "identity": "", "smb": "", "ports_dir": "",
    "soc": "", "gpu": "", "ram_mb": 0, "screen": "",
    "fe_proc": "", "fe_start": "", "fe_stop": "",
    "fe_reload": "",             # refresh the game list without a restart (Knulli/Batocera ES API)
    "fe_alt": "",                # other processes that also count as the front end being up (a laptop's desktop:
                                 # lightdm); start/stop/restart still act on fe_proc only
    "battery_shutdown": True,    # clean shutdown at battery_shutdown_pct (off: notify only, e.g. a laptop)
    "pm_dir": "",                # PortMaster folder on the device (empty: found automatically)
    "autoconnect": True, "poll": True,
    "guard": "enforce",          # hook guard for raw commands naming the device: enforce | warn | off
    "approval": "auto",          # before a session gets the free device: off (lock is enough) |
                                 # ask (user's Approve/Deny) | auto (ask; yes by itself after approval_timeout_min)
    "match": "",                 # extra comma-separated strings that mean "this device" in a command
    "lock_file": "",             # optional lock file of an older script, kept in step with the lock
    "notes": "", "enabled": True,
    # out of service (set by the user's button): still connected and watched, but not handed to sessions
    "oos": False, "oos_note": "", "oos_since": 0,
    # SD-card profiles: one device (same IP, specs, rules), several cards with their own CFW and login.
    # The top-level CARD_FIELDS always hold the active card's values, so everything else just reads those.
    "cards": [], "card": "",
    # Retro Catalog model id (retrocatalog.com/retro-handhelds/<id>): specs filled from there, linked on the card
    "rc_slug": "",
}
CARD_FIELDS = ("cfw", "port", "user", "auth", "identity", "smb", "ports_dir", "pm_dir",
               "fe_proc", "fe_start", "fe_stop", "fe_reload", "fe_alt")

SYSTEM_PROCS = {
    "emulationstation", "Xwayland", "weston", "sway", "labwc", "pipewire", "pipewire-pulse", "wireplumber",
    "pulseaudio", "smbd", "nmbd", "dropbear", "sshd", "systemd", "systemd-journald", "systemd-udevd",
    "systemd-logind", "NetworkManager", "connmand", "wpa_supplicant", "dbus-daemon", "udevd", "syncthing",
    "muxfrontend", "muxlaunch", "frontend.sh", "init", "busybox", "sh", "bash", "ash", "rsyslogd", "crond",
    # firmware background services, not a game left open: dArkOS/ArkOS MIDI synth, exFAT/NTFS card mounts,
    # Windows network discovery (Knulli runs wsdd under python3), Bluetooth, mDNS
    "fluidsynth", "mount.exfat", "mount.exfat-fuse", "exfat-fuse", "mount.ntfs-3g", "ntfs-3g", "wsdd", "wsdd2",
    "wsdd.py", "bluetoothd", "avahi-daemon", "filebrowser",
    # a Linux desktop (laptop/PC test devices): X, the display manager and XFCE, not a port left running
    "Xorg", "X", "lightdm", "xfwm4", "xfce4-panel", "xfdesktop", "xfce4-session", "xfsettingsd", "xfconfd",
    "xfce4-power-manager", "xfce4-notifyd", "Thunar", "thunar", "panel-1-whiskermenu", "nm-applet", "polkitd",
    "upowerd", "colord", "ModemManager", "packagekitd", "gvfsd", "tumblerd", "light-locker",
}
WRAPPERS = {"box64", "box86", "python", "python3", "love", "mono", "sh", "bash", "java", "wine", "wine64",
            "gmloader", "gmloadernext.aarch64", "westonwrap.sh", "taskset"}


class Hub:
    pass


H = Hub()
H.lock = threading.RLock()
H.devices = {}      # id -> profile
H.locks = {}        # id -> lock
H.queues = {}       # id -> [waiter]
H.requests = {}     # id -> [approval request]
H.sessions = {}     # Claude session id -> info
H.notices = {}      # holder key -> [text]
H.settings = dict(DEFAULT_SETTINGS)
H.paused = set()    # auto-connect switched off by the user
H.owned = {}        # id -> pid of the master DeviceHub opened (survives hub restarts)
H.chain = {}        # id -> time of the last approval / approved session end (request chains)
H.secrets = {}      # id -> {"dpapi": b64}
H.pwcache = {}
H.rt = {}           # runtime per device (not saved)
H.events = []
H.alerts = []
H.nag = {}
H.usable = {}       # id -> last seen "a session could work on it" (offers when it turns True)
H.swept = 0         # last sweep of the upload folder (unused port zips are deleted)
H.dirty = False
H.patterns = {}
TOKEN = ""
POOL = ThreadPoolExecutor(max_workers=12)


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- small helpers

def now():
    return time.time()


def hm(t):
    return time.strftime("%H:%M", time.localtime(t))


def fmt_dur(s):
    s = int(max(0, s))
    if s < 90:
        return "%d s" % s
    m = (s + 30) // 60
    if m < 90:
        return "%d min" % m
    h, m = divmod(m, 60)
    return "%d h %02d min" % (h, m) if m else "%d h" % h


def parse_dur(v, default_s):
    if v in (None, ""):
        return default_s
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return int(v)
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(s|sec|m|min|h|hr|d)?\s*", str(v).lower())
    if not m:
        raise ApiError(400, "bad duration %r (use e.g. 30m, 2h, 90s)" % v)
    unit = (m.group(2) or "m")[0]
    return int(float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit])


def clamp_ttl(v):
    ttl = parse_dur(v, H.settings["default_ttl_min"] * 60)
    return max(60, min(ttl, H.settings["max_ttl_min"] * 60))


def to_wsl(p):
    m = re.match(r"^([A-Za-z]):[\\/](.*)$", p or "")
    return "/mnt/%s/%s" % (m.group(1).lower(), m.group(2).replace("\\", "/")) if m else (p or "")


def jpath(name):
    return os.path.join(DATA, name)


_log_lock = threading.Lock()


def log(*a):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    with _log_lock:
        try:
            with open(jpath("devhubd.log"), "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        if sys.stderr.isatty():
            print(line, file=sys.stderr)


def load_json(name, default):
    try:
        with open(jpath(name), encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError:
        bad = jpath(name + ".corrupt-%d" % now())
        shutil.copy(jpath(name), bad)
        log("could not parse", name, "- copy kept as", bad)
        return default


def save_json(name, obj):
    p = jpath(name)
    tmp = p + ".tmp"
    data = json.dumps(obj, indent=1, ensure_ascii=False)
    for i in range(5):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, p)
            return
        except OSError as e:
            if i == 4:
                log("save failed:", name, e)
            time.sleep(0.2)


def save_state():
    with H.lock:
        save_json("state.json", {"locks": H.locks, "queues": H.queues, "requests": H.requests,
                                 "sessions": H.sessions, "notices": H.notices, "settings": H.settings,
                                 "paused": sorted(H.paused), "owned": H.owned, "chain": H.chain})
        H.dirty = False
        sync_lock_files()


def save_devices():
    with H.lock:
        save_json("devices.json", {"devices": list(H.devices.values())})
        H.patterns = {}


def event(dev, kind, msg, who=None):
    e = {"t": round(now(), 1), "dev": dev, "kind": kind, "msg": msg}
    if who:
        e["who"] = who
    with H.lock:
        H.events.append(e)
        del H.events[:-1500]
    try:
        with open(jpath("events.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    except OSError:
        pass
    log("event", dev or "-", kind, msg)


_alert_id = [0]


def alert(dev, text, toast=True, kind="", strip=True):
    """Something for the user: one Windows toast titled with the device, and (strip=True) an entry for the UI's
    alert strip. Approval requests use strip=False: their Approve/Deny row in the UI is the strip entry."""
    d = H.devices.get(dev) if dev else None
    title = "DeviceHub · " + dname(d) if d else "DeviceHub"
    with H.lock:
        if strip:
            _alert_id[0] += 1
            H.alerts.append({"id": _alert_id[0], "t": now(), "dev": dev, "title": title, "text": text, "kind": kind})
            del H.alerts[:-40]
    # A DeviceHub page with browser notifications on was open recently: it notifies (clicking it brings the page
    # up). Otherwise a Windows toast (WSL), whose click opens the page at that device. Never both.
    url = "http://127.0.0.1:%d/%s" % (PORT, "#dev=" + dev if d else "")
    if toast and now() - H.ui_notify_seen > 90:
        if PLATFORM == "wsl" and H.settings.get("toasts"):
            threading.Thread(target=win_toast, args=(title, text, url), daemon=True).start()
        if H.settings.get("notify_cmd"):
            threading.Thread(target=notify_cmd, args=(title, text, url, kind), daemon=True).start()


def notify_cmd(title, text, url, kind):
    """The user's own notification command (Settings): notify-send, an ntfy/Gotify push, a chat webhook, ...
    It gets DEVHUB_TITLE, DEVHUB_TEXT, DEVHUB_URL and DEVHUB_KIND in its environment."""
    env = dict(os.environ, DEVHUB_TITLE=title, DEVHUB_TEXT=text, DEVHUB_URL=url, DEVHUB_KIND=kind or "")
    try:
        p = subprocess.run(["sh", "-c", H.settings["notify_cmd"]], env=env, capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            log("notify command failed (exit %d): %s" % (p.returncode, (p.stderr or "").strip()[-200:]))
    except (OSError, subprocess.TimeoutExpired) as e:
        log("notify command failed:", e)


H.ui_notify_seen = 0      # last poll from a DeviceHub page that shows browser notifications

# stdin: first line = URL opened on click, second = title, the rest = message
TOAST_PS = r"""
$all = [Console]::In.ReadToEnd()
$i = $all.IndexOf("`n")
$url = $all.Substring(0, $i)
$all = $all.Substring($i + 1)
$i = $all.IndexOf("`n")
$title = $all.Substring(0, $i)
$m = $all.Substring($i + 1)
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$t = $x.GetElementsByTagName('text')
$t.Item(0).AppendChild($x.CreateTextNode($title)) | Out-Null
$t.Item(1).AppendChild($x.CreateTextNode($m)) | Out-Null
$x.DocumentElement.SetAttribute('activationType', 'protocol')
$x.DocumentElement.SetAttribute('launch', $url)
$app = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($app).Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""


def win_toast(title, text, url="http://127.0.0.1:8765/"):
    try:
        subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", TOAST_PS],
                       input=url + "\n" + title.replace("\n", " ")[:120] + "\n" + text[:300],
                       capture_output=True, text=True, timeout=30, cwd="/mnt/c/")
    except (OSError, subprocess.TimeoutExpired) as e:
        log("toast failed:", e)


# ---------------------------------------------------------------- devices

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,23}$")


def sock_path(d):
    return d.get("socket") or "/tmp/%s.sock" % d["id"]


def target(d):
    return "%s@%s" % (d.get("user") or "root", d["host"])


def dname(d):
    return "%s (%s, %s)" % (d.get("name") or d["id"], d.get("cfw") or "?", d["host"])


def need_device(dev):
    d = H.devices.get(dev)
    if not d:
        raise ApiError(404, "no device %r (known: %s)" % (dev, ", ".join(H.devices) or "none"))
    return d


def clean_device(src, old=None):
    d = dict(DEVICE_DEFAULTS)
    if old:
        d.update(old)
    for k, v in (src or {}).items():
        if k not in DEVICE_DEFAULTS or (k == "id" and old) or k in ("cards", "card"):
            continue
        dv = DEVICE_DEFAULTS[k]
        if isinstance(dv, bool):
            v = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(dv, int):
            try:
                v = int(v or 0)
            except (TypeError, ValueError):
                raise ApiError(400, "%s must be a number" % k)
        else:
            v = str(v if v is not None else "").strip()
        d[k] = v
    if not ID_RE.match(d["id"]):
        raise ApiError(400, "id: 1-24 characters a-z 0-9 _ -, starting with a letter or digit")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9.:_-]*$", d["host"]):
        raise ApiError(400, "host: an IP address or host name is required")
    if not re.match(r"^[A-Za-z0-9._-]*$", d["user"]):
        raise ApiError(400, "user: letters, digits, . _ - only")
    if not 0 < d["port"] < 65536:
        raise ApiError(400, "port must be 1-65535")
    if d["auth"] not in ("password", "key", "manual"):
        raise ApiError(400, "auth must be password, key or manual")
    if d["guard"] not in ("enforce", "warn", "off"):
        raise ApiError(400, "guard must be enforce, warn or off")
    if "require_approval" in (src or {}) and "approval" not in (src or {}):     # profiles from before 2026-10-06
        d["approval"] = "ask" if src["require_approval"] in (True, "true", "1", "on") else d["approval"]
    d.pop("require_approval", None)
    if d["approval"] not in ("off", "ask", "auto"):
        raise ApiError(400, "approval must be off, ask or auto")
    for k in ("socket", "identity"):
        if d[k] and not d[k].startswith("/"):
            raise ApiError(400, "%s must be an absolute WSL path" % k)
    d["name"] = d["name"] or d["id"]
    if isinstance((src or {}).get("cards"), list):
        d["cards"] = clean_cards(d, src["cards"], (old or {}).get("cards") or [])
        if src.get("card"):
            d["card"] = str(src["card"])
    if not isinstance(d.get("cards"), list):
        d["cards"] = []
    elif not isinstance((src or {}).get("cards"), list):
        d["cards"] = [dict(c) for c in d["cards"]]      # never edit the old profile in place
        ac = active_card(d)
        for k in CARD_FIELDS:                           # a plain edit of the login fields edits the active card
            if ac and k in (src or {}):
                ac[k] = d[k]
    # the IP flows to the settings that contain it (SMB share paths, also in the other card profiles)
    oh = (old or {}).get("host")
    if oh and oh != d["host"]:
        swap = lambda v: re.sub(r"(?<![\w.])%s(?![\w]|\.\d)" % re.escape(oh), d["host"], v) if isinstance(v, str) else v
        d["smb"] = swap(d["smb"])
        for c in d["cards"]:
            c["smb"] = swap(c.get("smb", ""))
    if d["cards"]:
        if d["card"] not in [c["id"] for c in d["cards"]]:
            d["card"] = d["cards"][0]["id"]
        d.update({k: active_card(d).get(k, d.get(k, DEVICE_DEFAULTS[k])) for k in CARD_FIELDS})
    else:
        d["card"] = ""
    return d


def clean_cards(d, cards, old_cards):
    """SD-card profiles from the editor. Each keeps its password slot and host-key file across edits (matched by
    id); the first card ever made inherits the device's own slot (its existing password and known host key)."""
    if len(cards) > 8:
        raise ApiError(400, "at most 8 card profiles")
    oldc = {c["id"]: c for c in old_cards}
    out = []
    for c in cards:
        if not isinstance(c, dict):
            raise ApiError(400, "bad card profile")
        label = str(c.get("label") or c.get("cfw") or "card").strip()[:40]
        cid = str(c.get("id") or "").strip() or re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:20] or "card"
        if not ID_RE.match(cid):
            raise ApiError(400, "card id: a-z 0-9 _ -")
        base, n = cid, 2
        while any(x["id"] == cid for x in out):
            cid, n = "%s-%d" % (base, n), n + 1
        card = {"id": cid, "label": label}
        for k in CARD_FIELDS:
            v = c.get(k, d.get(k))
            card[k] = int(v or 22) if k == "port" else str(v if v is not None else "").strip()
        if card["auth"] not in ("password", "key", "manual"):
            raise ApiError(400, "card %s: auth must be password, key or manual" % label)
        if not re.match(r"^[A-Za-z0-9._-]*$", card["user"]):
            raise ApiError(400, "card %s: user: letters, digits, . _ - only" % label)
        if not 0 < card["port"] < 65536:
            raise ApiError(400, "card %s: port must be 1-65535" % label)
        if card["identity"] and not card["identity"].startswith("/"):
            raise ApiError(400, "card %s: key file must be an absolute WSL path" % label)
        prev = oldc.get(cid)
        if prev:
            card["legacy"] = prev.get("legacy", False)
        out.append(card)
    if out and not any(c.get("legacy") for c in out) and not any(c.get("legacy") for c in old_cards):
        out[0]["legacy"] = True       # the device's existing password and known host key
    for c in out:
        c.setdefault("legacy", False)
    return out


def active_card(d):
    return next((c for c in d.get("cards") or [] if c["id"] == d.get("card")), None)


def secret_key(d, card=None):
    """The password slot: one per card profile; the first card keeps the device's own slot."""
    c = card or active_card(d)
    return d["id"] if not c or c.get("legacy") else "%s@%s" % (d["id"], c["id"])


def known_hosts_file(d, card=None):
    """Each card profile has its own SSH host key file ('' = the default ~/.ssh/known_hosts, first card)."""
    c = card or active_card(d)
    if not c or c.get("legacy"):
        return ""
    p = jpath(os.path.join("known_hosts", "%s@%s" % (d["id"], c["id"])))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def ref_pattern(d):
    p = H.patterns.get(d["id"])
    if p is None:
        parts = [r"(?<![\w.])%s(?![\w]|\.\w)" % re.escape(d["host"]), re.escape(sock_path(d))]
        parts += [re.escape(x.strip()) for x in (d.get("match") or "").split(",") if x.strip()]
        p = H.patterns[d["id"]] = re.compile("|".join(parts), re.I)
    return p


def rt(dev):
    r = H.rt.get(dev)
    if r is None:
        r = H.rt[dev] = {"online": None, "smb": None, "smb_at": 0, "reach_at": 0, "changed": now(),
                         "master": {"state": "unknown", "msg": "", "since": now(), "fails": 0, "next_try": 0},
                         "status": None, "status_at": 0, "status_err": "", "watch": None, "proc": None,
                         "busy": False, "poke": True, "next_status": 0}
    return r


def set_master(dev, state, msg="", **kw):
    m = rt(dev)["master"]
    if m["state"] != state:
        m["since"] = now()
    m["state"] = state
    m["msg"] = msg
    m.update(kw)


# ---------------------------------------------------------------- secrets (Windows DPAPI under WSL, openssl on Linux)

def secret_key_file():
    """Linux: a random key only this user can read; passwords are AES-256 encrypted with it by openssl."""
    p = jpath("secret.key")
    if not os.path.exists(p):
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    return p


def openssl_crypt(encrypt, data):
    args = ["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt", "-a", "-A",
            "-pass", "file:" + secret_key_file()] + ([] if encrypt else ["-d"])
    try:
        p = subprocess.run(args, input=data, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError("openssl is needed to keep device passwords encrypted: %s" % e)
    if p.returncode != 0:
        raise RuntimeError("openssl failed: %s" % ((p.stderr or "").strip()[-200:] or "no output"))
    return p.stdout.strip() if encrypt else p.stdout


def dpapi(protect, b64):
    op = "Protect" if protect else "Unprotect"
    script = ("$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.Security;"
              "$i=[Console]::In.ReadToEnd().Trim();"
              "$o=[Security.Cryptography.ProtectedData]::%s([Convert]::FromBase64String($i),"
              "[Text.Encoding]::UTF8.GetBytes('DeviceHub'),"
              "[Security.Cryptography.DataProtectionScope]::CurrentUser);"
              "[Console]::Out.Write([Convert]::ToBase64String($o))") % op
    try:
        p = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script], input=b64,
                           capture_output=True, text=True, timeout=60, cwd="/mnt/c/")
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError("Windows DPAPI not reachable from WSL: %s" % e)
    out = p.stdout.strip()
    if p.returncode != 0 or not out:
        raise RuntimeError("Windows DPAPI failed: %s" % ((p.stderr or "").strip()[-200:] or "no output"))
    return out


def set_password(dev, pw):
    if PLATFORM == "wsl":
        entry = {"dpapi": dpapi(True, base64.b64encode(pw.encode("utf-8")).decode())}
    else:
        entry = {"openssl": openssl_crypt(True, pw)}
    with H.lock:
        H.secrets[dev] = dict(entry, set=round(now()))
        H.pwcache[dev] = pw
        save_json("secrets.json", H.secrets)
        os.chmod(jpath("secrets.json"), 0o600)


def get_password(dev):
    if dev in H.pwcache:
        return H.pwcache[dev]
    e = H.secrets.get(dev)
    if not e:
        return None
    if "dpapi" in e:
        if PLATFORM != "wsl":
            raise RuntimeError("this password was saved with Windows DPAPI on another machine: enter it again in Edit")
        pw = base64.b64decode(dpapi(False, e["dpapi"])).decode("utf-8")
    else:
        pw = openssl_crypt(False, e["openssl"])
    H.pwcache[dev] = pw
    return pw


def pw_fifo(pw):
    """One-shot FIFO the askpass helper reads; removed after one read or 25 s."""
    path = os.path.join(RUN, "pw-" + secrets.token_hex(8))
    os.mkfifo(path, 0o600)

    def feed():
        end = time.time() + 25
        try:
            while time.time() < end:
                try:
                    fd = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
                except OSError as e:
                    if e.errno == errno.ENXIO:   # nobody reading yet
                        time.sleep(0.1)
                        continue
                    raise
                try:
                    os.write(fd, (pw + "\n").encode("utf-8"))
                finally:
                    os.close(fd)
                return
        except OSError as e:
            log("askpass fifo:", e)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    threading.Thread(target=feed, daemon=True).start()
    return path


# ---------------------------------------------------------------- ssh plumbing

def tcp_ok(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def master_alive(d):
    s = sock_path(d)
    if not os.path.exists(s):
        return False, None
    try:
        p = subprocess.run(["ssh", "-S", s, "-O", "check", "-p", str(d["port"]), target(d)],
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=8)
    except subprocess.TimeoutExpired:
        return False, None
    m = re.search(r"pid=(\d+)", p.stderr)
    return p.returncode == 0, (int(m.group(1)) if m else None)


def ssh_run(d, cmd, inp=None, timeout=20):
    args = ["ssh", "-S", sock_path(d), "-o", "ControlMaster=no", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
            "-o", "LogLevel=ERROR", "-p", str(d["port"]), target(d), cmd]
    if inp is None:
        p = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout)
    else:
        p = subprocess.run(args, input=inp, capture_output=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def classify(err):
    e = err.lower()
    if "permission denied" in e or "authentication failed" in e or "too many authentication" in e:
        return "auth_failed", "login rejected: check user and password (no automatic retry)"
    if "host identification has changed" in e or "host key verification failed" in e:
        return "hostkey_changed", "host key changed (reflashed?): use Forget host key if that is expected"
    if "connection refused" in e:
        return "error", "SSH refused: is SSH switched on on the device?"
    if "timed out" in e or "no route" in e or "unreachable" in e:
        return "error", "no answer from the device"
    lines = [x for x in err.strip().splitlines() if x.strip()]
    return "error", (lines[-1][:200] if lines else "ssh exited without a message")


def connect(dev, d):
    r = rt(dev)
    m = r["master"]
    s = sock_path(d)
    pw = None
    if d["auth"] == "password":
        try:
            pw = get_password(secret_key(d))
        except (RuntimeError, ValueError) as e:
            set_master(dev, "error", str(e), block="error")
            return
        if not pw:
            set_master(dev, "needs_password", "no password saved: Edit > Password", block="needs_password")
            return
    if os.path.exists(s):
        # never take a socket away from a live ssh process (a busy master can miss one check)
        holders = subprocess.run(["pgrep", "-f", "ssh.* -S %s( |$)" % re.escape(s)], capture_output=True, text=True)
        if holders.returncode == 0:
            m["next_try"] = now() + 60
            set_master(dev, "error", "%s belongs to a running ssh (pid %s) that does not answer: left alone" % (
                s, " ".join(holders.stdout.split())))
            return
        try:
            os.unlink(s)      # left over from a dead master
        except OSError:
            pass
    set_master(dev, "connecting", "")
    args = ["ssh", "-M", "-S", s, "-N", "-o", "ControlPersist=no", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "LogLevel=ERROR", "-p", str(d["port"])]
    kh = known_hosts_file(d)
    if kh:
        args += ["-o", 'UserKnownHostsFile="%s"' % kh]      # quoted: ssh splits on spaces
    env = dict(os.environ)
    if pw:
        args += ["-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=keyboard-interactive,password",
                 "-o", "NumberOfPasswordPrompts=1"]
        env.update(SSH_ASKPASS=ASKPASS, SSH_ASKPASS_REQUIRE="force", DISPLAY=env.get("DISPLAY") or ":0",
                   DEVHUB_PW_FIFO=pw_fifo(pw))
    else:
        args += ["-o", "BatchMode=yes"]
        if d["identity"]:
            args += ["-i", d["identity"], "-o", "IdentitiesOnly=yes"]
    args.append(target(d))
    errp = os.path.join(RUN, dev + ".err")
    with open(errp, "wb") as ef:
        p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=ef, env=env,
                             start_new_session=True, close_fds=True)
    r["proc"] = p
    ok = False
    end = now() + 20
    while now() < end:
        time.sleep(0.4)
        if p.poll() is not None:
            break
        if master_alive(d)[0]:
            ok = True
            break
    if ok:
        m["fails"] = 0
        m.pop("block", None)
        set_master(dev, "up", "", owned=True, pid=p.pid)
        H.owned[dev] = p.pid
        H.dirty = True
        event(dev, "ssh", "shared connection opened (%s)" % ("password" if d["auth"] == "password" else "key"))
        r["next_status"] = 0
        return
    if p.poll() is None:
        p.kill()
        p.wait()
    try:
        with open(errp, encoding="utf-8", errors="replace") as f:
            err = f.read()[-1500:]
    except OSError:
        err = ""
    state, msg = classify(err)
    if state in ("hostkey_changed", "auth_failed") and len(d.get("cards") or []) > 1:
        found = detect_card(dev, d)
        if found and found["id"] != d["card"]:
            switch_card(dev, found["id"], "SD card swapped: detected by its SSH host key")
            return
        if state == "hostkey_changed":
            msg = ("the device answers with a host key none of its card profiles knows: a new SD card? Pick its card "
                   "profile on the card (a profile learns its key on first connect), or Forget host key if this card "
                   "was reflashed")
    m["fails"] = m.get("fails", 0) + 1
    m["next_try"] = now() + min(300, 10 * 2 ** min(m["fails"], 5))
    set_master(dev, state, msg)
    if state != "error":
        m["block"] = state
        alert(dev, "Shared SSH connection: " + msg, kind="ssh")
    event(dev, "ssh", "connect failed: " + msg)


def detect_card(dev, d):
    """Which card profile is in the device: compare the host keys it offers now with each profile's saved ones."""
    spec = d["host"] if d["port"] == 22 else "[%s]:%d" % (d["host"], d["port"])
    try:
        p = subprocess.run(["ssh-keyscan", "-T", "5", "-p", str(d["port"]), d["host"]], capture_output=True,
                           text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    offered = {ln.split()[2] for ln in p.stdout.splitlines() if len(ln.split()) >= 3 and not ln.startswith("#")}
    if not offered:
        return None
    for c in d["cards"]:
        f = known_hosts_file(d, c) or os.path.expanduser("~/.ssh/known_hosts")
        if not os.path.isfile(f):
            continue
        q = subprocess.run(["ssh-keygen", "-F", spec, "-f", f], capture_output=True, text=True, timeout=10)
        saved = {ln.split()[2] for ln in q.stdout.splitlines() if len(ln.split()) >= 3 and not ln.startswith("#")}
        if offered & saved:
            return c
    return None


def card_name(c):
    return c["label"] if "card" in c["label"].lower() else "the %s card" % c["label"]


def switch_card(dev, cid, why, by="DeviceHub"):
    """Make another SD-card profile the active one: its CFW, login and folders become the device's, the shared
    connection is closed and reopened with the new login, and the holder of a lock is told."""
    with H.lock:
        d = H.devices[dev]
        c = next((c for c in d.get("cards") or [] if c["id"] == cid), None)
        if not c:
            raise ApiError(404, "%s has no card profile %r" % (d["name"], cid))
        old = active_card(d)
        d["card"] = cid
        d.update({k: c.get(k, DEVICE_DEFAULTS[k]) for k in CARD_FIELDS})
        save_devices()
    if rt(dev)["master"]["state"] == "up":
        disconnect(dev, H.devices[dev])
    with H.lock:
        r = rt(dev)
        r["master"].update(fails=0, next_try=0, retry=True)
        r["master"].pop("block", None)
        set_master(dev, "unknown", "")
        r.update(status=None, status_at=0, status_err="", next_status=0, poke=True)
        H.usable[dev] = False          # offered to waiting sessions once the new card's connection is up
    event(dev, "card", "now %s (was %s): %s" % (c["label"], old["label"] if old else "?", why), by)
    lk = H.locks.get(dev)
    if lk and not lk["holder"].get("ui"):
        add_notice(lk["holder"], "[DeviceHub] %s now runs %s (%s, user %s): %s. Anything you put on the old "
                                 "card is not there." % (dname(H.devices[dev]), card_name(c), c["cfw"], c["user"], why))
    if by == "DeviceHub":
        alert(dev, "Now using %s (%s): %s." % (card_name(c), c["cfw"], why), kind="card")
    return c


def disconnect(dev, d):
    subprocess.run(["ssh", "-S", sock_path(d), "-O", "exit", "-p", str(d["port"]), target(d)],
                   stdin=subprocess.DEVNULL, capture_output=True, timeout=8)
    p = rt(dev).get("proc")
    if p:
        try:
            p.wait(timeout=3)
        except subprocess.TimeoutExpired:
            p.kill()
        rt(dev)["proc"] = None


def upkeep(dev):
    d = H.devices.get(dev)
    if not d:
        return
    r = rt(dev)
    m = r["master"]
    if not d["enabled"]:
        set_master(dev, "disabled", "")
        r["online"] = None
        return
    alive, pid = master_alive(d)
    online = alive or tcp_ok(d["host"], d["port"])   # no bare TCP probes (log noise on the device) while connected
    if online != r["online"]:
        if r["online"] is not None:
            event(dev, "net", "%s is %s" % (dname(d), "online" if online else "offline"))
        r["online"] = online
        r["changed"] = now()
    if d["smb"] and now() - r["smb_at"] > 60:
        r["smb"] = tcp_ok(d["host"], 445)
        r["smb_at"] = now()
    if alive:
        p = r.get("proc")
        owned = bool(p and p.poll() is None and p.pid == pid) or H.owned.get(dev) == pid
        if m["state"] not in ("up", "connecting"):
            event(dev, "ssh", "shared connection is up (%s)" % ("opened by DeviceHub" if owned else "opened outside DeviceHub"))
        set_master(dev, "up", "", owned=owned, pid=pid)
        m.pop("block", None)
        with H.lock:   # login/host-key alerts are settled once the connection works
            H.alerts[:] = [a for a in H.alerts if not (a["dev"] == dev and a.get("kind") == "ssh")]
    else:
        p = r.get("proc")
        if p and p.poll() is not None:
            r["proc"] = None
        if m["state"] == "up":
            event(dev, "ssh", "shared connection closed")
        if d["auth"] == "manual":
            set_master(dev, "manual", "waiting for you to open it (copy the open command)")
        elif dev in H.paused or not d["autoconnect"]:
            set_master(dev, "paused", "auto-connect is off")
        elif not online:
            set_master(dev, "offline", "")
        elif m.get("block") and not m.get("retry"):
            if m["state"] != m["block"]:
                set_master(dev, m["block"], m.get("msg", ""))
        elif now() < m.get("next_try", 0) and not m.get("retry"):
            if m["state"] not in ("error",):
                set_master(dev, "error", m.get("msg") or "waiting to retry")
        else:
            m.pop("retry", None)
            m.pop("block", None)
            connect(dev, d)
            alive = m["state"] == "up"
    if alive and d["poll"] and now() >= r["next_status"]:
        poll_status(dev, d)


# ---------------------------------------------------------------- device status

PROBE = r"""
echo "up=$(cut -d' ' -f1 /proc/uptime)"
echo "load=$(cut -d' ' -f1-3 /proc/loadavg)"
grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo | tr -d ' kB' | sed 's/:/=/'
bdone=""
for p in /sys/class/power_supply/*; do
  [ -e "$p" ] || continue
  t=$(cat "$p/type" 2>/dev/null)
  if [ "$t" = Battery ] || { [ -z "$t" ] && [ -f "$p/capacity" ]; }; then
    if [ -z "$bdone" ] && [ -f "$p/capacity" ]; then
      echo "bat=$(cat "$p/capacity")"; echo "batst=$(cat "$p/status" 2>/dev/null)"
      echo "batuv=$(cat "$p/voltage_now" 2>/dev/null)"; echo "batua=$(cat "$p/current_now" 2>/dev/null)"
      bdone=1
    fi
  elif [ -f "$p/online" ]; then
    echo "psu=$(basename "$p") ${t:-?} $(cat "$p/online" 2>/dev/null)"
  fi
done
echo "temp=$(cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null)"
[ -n "$FE" ] && echo "fe=$(pidof "$FE" 2>/dev/null)"
[ -n "$FEALT" ] && echo "fealt=$(pidof $FEALT 2>/dev/null)"
[ -n "$PD" ] && df -k "$PD" 2>/dev/null | awk 'NR==2 {print "disk=" $4 " " $2}'
echo "os=$(. /etc/os-release 2>/dev/null; echo "$PRETTY_NAME")"
echo "kernel=$(uname -r)"
grep -s VmRSS /proc/[0-9]*/status | awk '$2 > 15000 {split($1, a, "/"); print a[3], $2}' | sort -k2 -nr | head -6 |
while read pid rss; do echo "proc=$pid $rss $(tr '\0' ' ' < /proc/$pid/cmdline 2>/dev/null | cut -c1-200)"; done
[ -n "$W" ] && echo "watch=$(grep -ls -- "$W" /proc/[0-9]*/cmdline 2>/dev/null | wc -l)"
if [ -n "$GPUDRV" ]; then
  # which GPU driver is live: the kernel drivers bound to the DRM cards, plus /dev/mali0 = Arm's kbase (libmali)
  echo "gpudrv=$(for c in /sys/class/drm/card[0-9] /sys/class/drm/card[0-9][0-9]; do [ -e "$c/device/driver" ] && basename "$(readlink -f "$c/device/driver")"; done 2>/dev/null | sort -u | tr '\n' ' ')$([ -e /dev/mali0 ] && echo ' mali_kbase')"
  echo "gpucfg=$(sed -n 's/^gpu\.driver=//p' /storage/.config/system/configs/system.cfg 2>/dev/null | head -1)"
  echo "gpuinfo=$(cat /sys/class/misc/mali0/device/gpuinfo 2>/dev/null)"
fi
true
"""
# ROCKNIX can run either the Mesa or the vendor GPU driver (switchable): show which one is live
GPU_DRIVERS = [("mali_kbase", "libmali"), ("panthor", "Panfrost"), ("panfrost", "Panfrost"),
               ("msm", "Adreno"), ("msm_drm", "Adreno"), ("msm_dpu", "Adreno")]


def grep_pat(s):
    """BRE for grep with the first letter bracketed, so grep never matches its own command line."""
    out, done = [], False
    for ch in s:
        if not done and ch.isalnum():
            out.append("[%s]" % ch)
            done = True
        elif ch in ".[]\\*^$":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def probe_script(d, watch=""):
    return "FEALT=%s; FE=%s; PD=%s; W=%s; GPUDRV=%s\n%s" % (shlex.quote(d.get("fe_alt") or ""), shlex.quote(d["fe_proc"]),
                                                             shlex.quote(d["ports_dir"]),
                                                  shlex.quote(grep_pat(watch) if watch else ""),
                                                  "1" if d.get("cfw") == "ROCKNIX" else "", PROBE)


def proc_name(cmd):
    argv = cmd.split()
    if not argv:
        return "?"
    base = os.path.basename(argv[0])
    if base in WRAPPERS and len(argv) > 1:
        for a in argv[1:]:
            if not a.startswith("-"):
                return "%s %s" % (base, os.path.basename(a))
    return base


def parse_status(text, d):
    st = {"procs": []}
    for line in text.splitlines():
        k, _, v = line.partition("=")
        v = v.strip()
        try:
            if k == "proc":
                pid, rss, cmd = (v.split(" ", 2) + ["", ""])[:3]
                st["procs"].append({"pid": int(pid), "rss": int(rss), "cmd": cmd.strip(), "name": proc_name(cmd)})
            elif k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree", "watch"):
                st[k] = int(v or 0)
            elif k == "up":
                st["up"] = float(v or 0)
            elif k == "bat":
                st["bat"] = int(v) if v.isdigit() else None
            elif k == "batuv":
                st["bat_v"] = round(int(v) / 1e6, 2) if v.lstrip("-").isdigit() else None
            elif k == "batua":
                st["bat_ma"] = round(int(v) / 1000) if v.lstrip("-").isdigit() else None
            elif k == "psu":
                name, typ, online = (v.split() + ["", "", ""])[:3]
                st.setdefault("psu", []).append({"name": name, "type": typ, "online": online == "1"})
            elif k == "temp":
                st["temp"] = round(int(v) / 1000, 1) if v.lstrip("-").isdigit() else None
            elif k in ("fe", "fealt"):
                st[k] = [int(x) for x in v.split() if x.isdigit()]
            elif k == "disk":
                a, t = v.split()
                st["disk"] = {"avail": int(a), "total": int(t)}
            elif k in ("load", "batst", "os", "kernel", "gpucfg", "gpuinfo"):
                st[k] = v
            elif k == "gpudrv":
                mods = v.split()
                st["gpu_drv_raw"] = " ".join(mods)
                st["gpu_driver"] = next((label for m, label in GPU_DRIVERS if m in mods), "unknown (%s)" % (" ".join(mods) or "no GPU driver bound"))
        except ValueError:
            pass
    # plugged in: a charger/USB supply reports online; without such a supply, judge by the battery status
    if st.get("psu"):
        st["plugged"] = any(p["online"] for p in st["psu"])
    elif st.get("batst"):
        st["plugged"] = {"Charging": True, "Full": True, "Not charging": True, "Discharging": False}.get(st["batst"])
    st["app"] = None
    for p in st["procs"]:
        words = p["cmd"].split()
        base = os.path.basename(words[0]) if words else ""
        if base in ("python", "python3") and len(words) > 1 and os.path.basename(words[1]) in SYSTEM_PROCS:
            continue      # a firmware service run by python (Knulli: python3 /usr/bin/wsdd)
        if base not in SYSTEM_PROCS and base != d.get("fe_proc") and base not in (d.get("fe_alt") or "").split():
            st["app"] = p
            break
    return st


def fe_up(d, st):
    """The front end counts as running: its own process, or one of fe_alt (a laptop's desktop)."""
    return bool(st.get("fe") or (d.get("fe_alt") and st.get("fealt")))


def poll_status(dev, d=None):
    d = d or H.devices.get(dev)
    if not d:
        return None
    r = rt(dev)
    lk = H.locks.get(dev)
    watch = (lk or {}).get("watch") or ""
    r["next_status"] = now() + (20 if watch else max(15, int(H.settings["status_interval_s"])))
    try:
        rc, out, err = ssh_run(d, "sh -s", inp=probe_script(d, watch).encode(), timeout=25)
    except subprocess.TimeoutExpired:
        r["status_err"] = "status probe timed out"
        return None
    if rc != 0 and not out.strip():
        lines = err.decode("utf-8", "replace").strip().splitlines()
        r["status_err"] = (lines[-1] if lines else "ssh exit %d" % rc)[:200]
        return None
    st = parse_status(out.decode("utf-8", "replace"), d)
    with H.lock:
        r["status"] = st
        r["status_at"] = now()
        r["status_err"] = ""
        if watch and "watch" in st:
            r["watch"] = (st["watch"], now(), watch)
    try:
        battery_watch(dev, d, st)
    except Exception:
        log("battery watch", dev, traceback.format_exc())
    return st


def battery_watch(dev, d, st):
    """Off the charger: notify the user at battery_alert_pct, shut the device down cleanly at battery_shutdown_pct
    (two readings in a row, so one odd reading cannot power it off). Plugged in or charged again re-arms both."""
    r = rt(dev)
    bat, plugged = st.get("bat"), st.get("plugged")
    warn, cut = H.settings.get("battery_alert_pct", 0), H.settings.get("battery_shutdown_pct", 0)
    if bat is None or plugged is not False:
        r["bat_low"] = 0
        if plugged:
            r["bat_warned"], r["bat_shutdown"] = False, None
        return
    if bat > max(warn, cut) + 5:
        r["bat_warned"], r["bat_low"], r["bat_shutdown"] = False, 0, None
    if warn and bat <= warn and not r.get("bat_warned") and bat > cut:
        r["bat_warned"] = True
        event(dev, "battery", "battery at %d%% and not charging" % bat)
        alert(dev, "Battery at %d%% and not on a charger.%s" % (
            bat, " It will be shut down cleanly at %d%%." % cut if cut and d.get("battery_shutdown", True) else ""),
              kind="battery")
    if cut and bat <= cut:
        r["bat_low"] = r.get("bat_low", 0) + 1
        r["next_status"] = min(r["next_status"], now() + 20)     # confirm soon
        if r["bat_low"] >= 2 and not r.get("bat_shutdown"):
            r["bat_shutdown"] = now()
            if d.get("battery_shutdown", True):
                shutdown_device(dev, d, "battery at %d%% and not charging" % bat)
            else:                    # notify only (a laptop): it powers itself off when it has to
                event(dev, "battery", "battery at %d%% and not charging (no automatic shutdown for this device)" % bat)
                alert(dev, "Battery at %d%% and not on a charger: plug it in (automatic shutdown is off for this "
                           "device)." % bat, kind="battery")
    else:
        r["bat_low"] = 0


def shutdown_device(dev, d, why, notify=True):
    """Clean power-off: stop the front end (it saves its settings and game lists), sync, power off. Runs detached
    on the device so the SSH call returns before the network goes down."""
    # /sbin is often missing from a non-root PATH, and some sudoers (dArkOS: !secure_path) keep the caller's PATH
    off = "poweroff" if d["user"] == "root" else "sudo -n poweroff"
    steps = ([d["fe_stop"]] if d.get("fe_stop") else []) + ["sync", off]
    # checked before detaching, so a missing poweroff or a sudo that wants a password is reported, not swallowed
    check = "command -v poweroff >/dev/null || { echo 'poweroff not found' >&2; exit 1; }; " + \
            ("" if d["user"] == "root" else "sudo -n true 2>/dev/null || { echo 'sudo needs a password' >&2; exit 1; }; ")
    cmd = "export PATH=\"$PATH:/usr/sbin:/sbin\"; %snohup sh -c %s >/dev/null 2>&1 &" % (
        check, shlex.quote("sleep 1; " + "; ".join(steps)))
    try:
        rc, out, err = ssh_run(d, cmd, timeout=30)
    except subprocess.TimeoutExpired:
        rc, err = -1, b"timed out"
    err = err.decode("utf-8", "replace").strip()[-120:] if isinstance(err, bytes) else str(err)
    if rc == 0:
        event(dev, "power", "shutting down: " + why)
        if H.locks.get(dev):
            release_lock(dev, "the device is shutting down: " + why, "DeviceHub", notify_holder=True)
        if notify:
            alert(dev, "Shutting it down cleanly: %s." % why, kind="battery")
    else:
        event(dev, "power", "shutdown failed: %s (%s)" % (why, err))
        if not notify:
            raise ApiError(502, "could not shut %s down: %s" % (dname(d), err))
        alert(dev, "Could not shut it down (%s): %s. Switch it off or plug it in." % (why, err), kind="battery")


def fresh_status(dev, max_age=20):
    r = rt(dev)
    if r["status"] and now() - r["status_at"] <= max_age:
        return r["status"]
    if r["master"]["state"] == "up":
        return poll_status(dev)
    return None


def device_clean(dev, max_age=300):
    """True: front end up and nothing big running; False: not; None: unknown."""
    d = H.devices.get(dev)
    r = rt(dev)
    st = r["status"]
    if not d or not st or now() - r["status_at"] > max_age:
        return None
    if d["fe_proc"] and not fe_up(d, st):
        return False
    return st.get("app") is None


# ---------------------------------------------------------------- sessions and holders

def touch_session(sid, name=None, transcript=None):
    with H.lock:
        s = H.sessions.get(sid)
        if s is None:
            s = H.sessions[sid] = {"name": name or "session " + sid[:8], "first": now(), "last": 0,
                                   "transcript": "", "ended": None}
        s["last"] = now()
        s["ended"] = None
        if transcript:
            s["transcript"] = transcript
        if name and name != s["name"]:
            s["name"] = name
            for lk in H.locks.values():
                if lk["holder"].get("session") == sid:
                    lk["holder"]["name"] = name
            save_state()
        H.dirty = True
        return s


# Server mode's request gate (devhubsrv.py): GATE(handler, method, path, query, auth) checks login and token in place
# of the loopback Host check, and returns a response tuple to send instead (a redirect to the login page) or None.
# REQ.ui says whether this request may speak as the user (client "ui"): always locally, only when logged in there.
GATE = None
REQ = threading.local()


def caller(body):
    if body.get("client") == "ui" and (not getattr(REQ, "ui", True) or getattr(REQ, "agent", None)):
        raise ApiError(403, "log in to DeviceHub to act as the user")
    if body.get("client") == "ui":
        return {"session": None, "name": H.settings.get("user_name") or "user", "ui": True}
    sid = str(body.get("session") or "").strip() or None
    name = str(body.get("name") or "").strip()[:60] or (getattr(REQ, "agent", None) or "")
    if sid:
        s = touch_session(sid, name or None, body.get("transcript"))
        return {"session": sid, "name": s["name"]}
    return {"session": None, "name": name or "unknown"}


def same(h, c):
    if not h or not c:
        return False
    if h.get("session") and c.get("session"):
        return h["session"] == c["session"]
    return (h.get("name") or "").lower() == (c.get("name") or "").lower()


def hkey(h):
    return ("s:" + h["session"]) if h.get("session") else ("n:" + (h.get("name") or "").lower())


def transcript_mtime(s):
    p = to_wsl((s or {}).get("transcript") or "")
    try:
        return os.stat(p).st_mtime if p else 0
    except OSError:
        return 0


def holder_last_active(h, lk=None):
    t = max((lk or {}).get("last_activity", 0), (lk or {}).get("since", 0))
    if h.get("session"):
        s = H.sessions.get(h["session"])
        if s:
            t = max(t, s.get("last", 0), transcript_mtime(s))
    return t


def add_notice(h, text):
    with H.lock:
        lst = H.notices.setdefault(hkey(h), [])
        if text not in lst:
            lst.append(text)
        del lst[:-20]
        H.dirty = True


def holder_label(h):
    return "%s%s" % (h.get("name") or "?", " (you)" if h.get("ui") else "")


# ---------------------------------------------------------------- locks

def lock_public(dev, lk):
    v = {k: lk.get(k) for k in ("holder", "purpose", "since", "lease_until", "ttl", "watch", "state",
                                "renewals", "note", "check", "stale_since", "last_activity")}
    v["idle"] = now() - holder_last_active(lk["holder"], lk)
    v["evidence"] = evidence(dev, lk)
    v["verdict"] = verdict(dev, lk)
    return v


def evidence(dev, lk):
    d = H.devices[dev]
    r = rt(dev)
    st = r["status"]
    idle = now() - holder_last_active(lk["holder"], lk)
    bits = ["%s last active %s" % (lk["holder"].get("name"), "just now" if idle < 60 else fmt_dur(idle // 60 * 60) + " ago")]
    if not r["online"]:
        bits.append("device offline")
    elif st and now() - r["status_at"] < 300:
        if d["fe_proc"]:
            bits.append("%s %s" % (d["fe_proc"], "running" if st.get("fe") else
                                   "not running (%s is)" % d["fe_alt"] if fe_up(d, st) else "NOT running"))
        app = st.get("app")
        bits.append("running: %s (%d MB)" % (app["name"], app["rss"] // 1024) if app else "no app running")
    else:
        bits.append("device state unknown (no fresh status)")
    if lk.get("watch"):
        w = r.get("watch")
        if w and w[2] == lk["watch"]:
            bits.append("watched job '%s' %s" % (lk["watch"], "running" if w[0] > 0 else "not running"))
    return "; ".join(bits)


def verdict(dev, lk):
    idle = now() - holder_last_active(lk["holder"], lk)
    clean = device_clean(dev)
    w = rt(dev).get("watch")
    if lk.get("watch") and w and w[2] == lk["watch"] and w[0] > 0:
        return "job running"
    if clean is True and idle > 15 * 60:
        return "looks finished"
    if clean is False:
        return "in use" if idle < 15 * 60 else "left dirty?"
    return "active" if idle < 15 * 60 else "idle"


def prune_queue(dev):
    """Waiters drop out 45 s after their last poll; an offer reservation or a place kept while a session decides
    on an offer lasts until its 'until'."""
    q = [e for e in H.queues.get(dev, []) if now() - e["seen"] < 45 or e.get("until", 0) > now()]
    if q:
        H.queues[dev] = q
    else:
        H.queues.pop(dev, None)
    return q


def enqueue(dev, c, purpose, need=""):
    q = H.queues.setdefault(dev, [])
    for e in q:
        if same(e, c):
            e["seen"] = now()
            e.pop("held", None)
            if need:
                e["need"] = need
            return q.index(e) + 1
    q.append({"session": c["session"], "name": c["name"], "purpose": purpose, "need": need, "at": now(), "seen": now()})
    event(dev, "queue", "%s is waiting: %s%s" % (c["name"], purpose, " - needs this device: " + need if need else ""), c["name"])
    return len(q)


def usable(did):
    """Can a session work on it now: online and a connection (or a key login). None while not known yet."""
    d, r = H.devices[did], rt(did)
    if not d["enabled"] or d.get("oos") or r["online"] is False:
        return False
    if r["online"] is None:
        return None
    return r["master"]["state"] == "up" or d["auth"] == "key"


def offered_to(did, c):
    """The device came online and is held for this waiting session (see offer_device)."""
    q = prune_queue(did)
    return len(q) == 1 and bool(q[0].get("offer_from")) and same(q[0], c)


def is_free(did, c=None):
    if H.locks.get(did) or (prune_queue(did) and not offered_to(did, c)):
        return False
    if any(x["state"] == "pending" for x in H.requests.get(did, [])):
        return False
    return not any(j["dev"] == did and j["state"] == "running" for j in H.jobs.values())


def free_alternatives(dev, c=None):
    """Other devices a waiting session could use right now: online, usable connection, no lock, nobody queued or
    asking. A device that came online and is held for this session comes first, then same CFW, same GPU, same SoC."""
    d0 = H.devices[dev]
    out = []
    for d in H.devices.values():
        did = d["id"]
        if did == dev or usable(did) is not True or not is_free(did, c):
            continue
        score = (8 if offered_to(did, c) else 0) + (4 if d["cfw"] == d0["cfw"] else 0) + \
                (2 if d["gpu"] and d["gpu"] == d0["gpu"] else 0) + (1 if d["soc"] and d["soc"] == d0["soc"] else 0)
        out.append((-score, d["name"], d))
    return [x[2] for x in sorted(out, key=lambda x: (x[0], x[1]))]


def wait_or_move(dev, d, c, purpose, need, offline=False):
    """A session wants to wait for a busy (or offline) device. With other devices free it must say why it needs this
    one (--need), or it is moved. Returns a 'move' reply, or None to queue it here."""
    if c.get("ui") or need:
        return None
    alts = free_alternatives(dev, c)
    if not alts:
        return None
    a = alts[0]
    if dev in H.queues:
        H.queues[dev] = [e for e in H.queues[dev] if not same(e, c)]
        if not H.queues[dev]:
            H.queues.pop(dev)
    event(dev, "queue", "%s moved to %s: it gave no reason to wait for this device" % (c["name"], a["name"]), c["name"])
    alike = (" (same CFW, %s)" % a["cfw"] if a["cfw"] == d["cfw"] else
             " (same GPU, %s)" % a["gpu"] if a["gpu"] and a["gpu"] == d["gpu"] else " (a different CFW and GPU)")
    came = "just became available and is free%s, and you have waited longest" if offered_to(a["id"], c) else "is online and free%s"
    return {"ok": False, "status": "move", "alt": a["id"],
            "msg": ("%s is %s and %s " + came + ", so you are moved there. If you need %s specifically, "
                    "wait for it with a reason: devhub acquire %s \"%s\" --wait 20m --need \"why this device\"") % (
                dname(d), "offline" if offline else "busy", dname(a), alike, d["name"], dev, purpose)}


def take_offer(dev, d, c):
    """A waiter that gave a reason (--need) is told once about devices that came online while it waited. It decides:
    take one, or wait again (its place in line is kept for 5 min). Returns an 'offer' reply or None."""
    e = next((e for e in H.queues.get(dev, []) if same(e, c)), None)
    if not e or not e.get("offers"):
        return None
    offs = [H.devices[x] for x in e.pop("offers") if x in H.devices and usable(x) is True and is_free(x, c)]
    if not offs:
        return None
    e.update(held=True, until=now() + 300)
    pos = H.queues[dev].index(e) + 1
    a = offs[0]
    H.dirty = True
    return {"ok": False, "status": "offer", "alt": a["id"],
            "msg": ("%s just became available and is free%s while you waited for %s (#%d in line, \"%s\"). If your work can run "
                    "there, take it: devhub acquire %s \"%s\". Otherwise wait again with the same command: your place "
                    "in line is kept until %s.") % (
                dname(a), " (also free: %s)" % ", ".join(dname(x) for x in offs[1:]) if offs[1:] else "",
                d["name"], pos, e.get("need") or e["purpose"], a["id"], e["purpose"], hm(e["until"]))}


def offer_device(did):
    """A free device came online: offer it to the sessions waiting for other devices. The longest waiter that gave
    no reason to wait for its device gets it (held for 2 min; its waiting acquire moves there). Without such a
    waiter, every waiter with a reason is told once and may take it."""
    waiters = []
    for qdev, q in H.queues.items():
        if qdev == did:
            continue
        for e in q:
            if not e.get("offer_from") and not e.get("held") and now() - e["seen"] < 10:   # its wait is still running
                waiters.append((e["at"], qdev, e))
    if not waiters:
        return
    waiters.sort(key=lambda w: w[0])
    plain = [w for w in waiters if not w[2].get("need")]
    if plain:
        at, qdev, e = plain[0]
        H.queues[did] = [{"session": e["session"], "name": e["name"], "purpose": e["purpose"], "need": "",
                          "at": now(), "seen": now(), "until": now() + 120, "offer_from": qdev}]
        event(did, "queue", "came online: offered to %s, waiting for %s since %s with no reason to need it" % (
            e["name"], H.devices[qdev]["name"], hm(at)), e["name"])
    else:
        for at, qdev, e in waiters:
            if did not in e.setdefault("offers", []):
                e["offers"].append(did)
        event(did, "queue", "came online: offered to %s (waiting with a reason for another device)" % ", ".join(
            "%s (%s)" % (w[2]["name"], H.devices[w[1]]["name"]) for w in waiters))
    H.dirty = True


def note_usable():
    """A device came online (or its connection came up) and is free: offer it to the waiters. Runs in the tick and
    on every waiting poll, so the longest waiter gets it rather than whoever polls first."""
    for dev in list(H.devices):
        u, prev = usable(dev), H.usable.get(dev)
        if u is not None:
            H.usable[dev] = u
        if u and prev is False and is_free(dev):
            offer_device(dev)


def free_hint(dev):
    alts = free_alternatives(dev)
    return (". Free right now: %s (or wait here with --wait and --need \"why this device\")." % ", ".join(
        "%s = %s" % (a["id"], a["name"]) for a in alts[:3])) if alts else ""


def busy_text(d, lk):
    return "%s is locked by %s for \"%s\" since %s (lease to %s, %s)" % (
        dname(d), holder_label(lk["holder"]), lk["purpose"], hm(lk["since"]), hm(lk["lease_until"]), lk["state"])


def approval_hint(d):
    mode = d.get("approval", "off")
    if mode != "off" and H.settings.get("away_mode"):
        return " (the user is away: requests are approved at once)"
    if mode == "auto":
        return " (this asks the user through a DeviceHub notification; yes automatically after %d min)" % H.settings["approval_timeout_min"]
    if mode == "ask":
        return " (this asks the user through a DeviceHub notification and waits for the answer)"
    return ""


def prune_requests(dev):
    """Drop decided requests after 15 min (an approval waits that long to be used) and 'ask' requests
    nobody has asked about for 10 min. 'auto' requests stay until their deadline approves them."""
    rs = H.requests.get(dev, [])
    rs[:] = [x for x in rs if not (
        (x["state"] == "pending" and x.get("mode") != "auto" and now() - x["seen"] > 600) or
        (x["state"] != "pending" and now() - x.get("decided", 0) > 900))]
    if not rs:
        H.requests.pop(dev, None)
    return rs


def chain_age(dev):
    """Seconds since this device's last approval or approved session ended, if inside the chain window."""
    win = H.settings.get("approval_chain_min", 0) * 60
    age = now() - H.chain.get(dev, 0)
    return age if win > 0 and age < win else None


def approval(dev, d, c, purpose, ttl):
    """The user's OK for a session that wants a free device: 'ask' waits for Approve/Deny,
    'auto' also says yes by itself after approval_timeout_min. Inside a request chain (an approval or an
    approved session's end on this device less than approval_chain_min ago) the answer is yes at once,
    without a notification. Returns (state, request)."""
    prune_requests(dev)
    rs = H.requests.setdefault(dev, [])
    x = next((x for x in rs if same(x, c)), None)
    if H.settings.get("away_mode") and (x is None or x["state"] == "pending"):
        # Away mode (the user's switch): every request is approved at once, no notification
        event(dev, "approval", "approved for %s automatically: away mode" % c["name"], c["name"])
        if x is not None:
            rs.remove(x)
        if not rs:
            H.requests.pop(dev, None)
        H.dirty = True
        return "approved", {"by": "away"}
    age = chain_age(dev)
    if age is not None and (x is None or x["state"] == "pending"):
        event(dev, "approval", "approved for %s automatically: request chain (last approval/approved use %s ago)" % (
            c["name"], fmt_dur(age)), c["name"])
        if x is not None:
            rs.remove(x)
        if not rs:
            H.requests.pop(dev, None)
        H.dirty = True
        return "approved", {"by": "chain", "age": age}
    if x is None:
        mode = d.get("approval") or "ask"
        x = {"session": c["session"], "name": c["name"], "purpose": purpose, "ttl": ttl, "at": now(),
             "seen": now(), "state": "pending", "mode": mode}
        if mode == "auto":
            x["deadline"] = now() + H.settings["approval_timeout_min"] * 60
        rs.append(x)
        event(dev, "approval", "%s asks to use it: %s" % (c["name"], purpose), c["name"])
        alert(dev, "%s asks to use it: %s. %s" % (
            c["name"], purpose, "Approve or deny in DeviceHub; yes automatically at %s." % hm(x["deadline"])
            if mode == "auto" else "Approve or deny in DeviceHub."), kind="approval", strip=False)
        H.dirty = True
    x["seen"] = now()
    if x["state"] in ("approved", "denied"):
        rs.remove(x)
        if not rs:
            H.requests.pop(dev, None)
    return x["state"], x


def do_acquire(dev, c, body):
    d = need_device(dev)
    purpose = str(body.get("purpose") or "").strip()[:200] or "(no purpose given)"
    ttl = clamp_ttl(body.get("ttl"))
    watch = str(body.get("watch") or "").strip()[:80]
    wait = bool(body.get("wait"))
    take = bool(body.get("take")) and c.get("ui")
    with H.lock:
        lk = H.locks.get(dev)
        if d.get("oos") and not c.get("ui") and not (lk and same(lk["holder"], c)):
            why = "%s is out of service (taken out by the user%s%s)" % (
                dname(d), " at " + hm(d["oos_since"]) if d.get("oos_since") else "", ": " + d["oos_note"] if d.get("oos_note") else "")
            alts = free_alternatives(dev, c) if wait else []
            if alts:
                event(dev, "queue", "%s moved to %s: this device is out of service" % (c["name"], alts[0]["name"]), c["name"])
                return {"ok": False, "status": "move", "alt": alts[0]["id"],
                        "msg": "%s, so you are moved to %s, which is online and free." % (why, dname(alts[0]))}
            return {"ok": False, "status": "out_of_service",
                    "msg": why + ". Do not use it until the user puts it back" + (free_hint(dev) or ".")}
        if lk and same(lk["holder"], c):
            if not lk["holder"].get("session") and c.get("session"):
                lk["holder"]["session"] = c["session"]   # name-only lock: now we know its session
            lk["lease_until"] = max(lk["lease_until"], now() + ttl)
            lk["ttl"] = max(lk["ttl"], ttl)
            if body.get("purpose"):
                lk["purpose"] = purpose
            if watch:
                lk["watch"], lk["watch_seen"] = watch, False
            lk.update(state="held", check=None, stale_since=None, last_activity=now())
            save_state()
            return {"ok": True, "status": "yours", "msg": "%s: you already hold it; lease ends %s" % (dname(d), hm(lk["lease_until"]))}
        if wait:
            note_usable()
        q = prune_queue(dev)
        need = str(body.get("need") or "").strip()[:200]
        offline = not c.get("ui") and rt(dev)["online"] is False
        if lk or (q and not same(q[0], c) and not take):
            if wait:
                moved = wait_or_move(dev, d, c, purpose, need, offline and not lk)
                if moved:
                    return moved
            pos = enqueue(dev, c, purpose, need) if wait else None
            offer = take_offer(dev, d, c) if wait else None
            if offer:
                return offer
            hint = "" if wait else free_hint(dev)
            if lk:
                return {"ok": False, "status": "busy", "msg": busy_text(d, lk) + (" - you are #%d in line" % pos if pos else "") + hint,
                        "lock": lock_public(dev, lk)}
            why = ("came online and is held for %s (waiting longest) until %s" % (q[0]["name"], hm(q[0]["until"]))
                   if q[0].get("offer_from") else
                   "is %s but %s is waiting first" % ("offline" if offline else "free", q[0]["name"]))
            return {"ok": False, "status": "queued",
                    "msg": "%s %s%s%s" % (dname(d), why, " - you are #%d" % pos if pos else "", hint)}
        if offline:
            if not wait:
                return {"ok": False, "status": "offline",
                        "msg": "%s is offline (switched off or off the network): ask the user to switch it on, or wait "
                               "for it with --wait%s" % (dname(d), free_hint(dev))}
            moved = wait_or_move(dev, d, c, purpose, need, True)
            if moved:
                return moved
            pos = enqueue(dev, c, purpose, need)
            return take_offer(dev, d, c) or {
                "ok": False, "status": "offline_wait",
                "msg": "%s is offline (switched off or off the network) - you are #%d in line and get it when it comes "
                       "online: ask the user to switch it on" % (dname(d), pos)}
        for e in q:
            if same(e, c):
                e["seen"] = now()     # first in line and waiting for the user's OK: keep the place
        approved = None
        if d.get("approval", "off") != "off" and not c.get("ui"):
            state, x = approval(dev, d, c, purpose, ttl)
            approved = x if state == "approved" else None
            if state == "denied":
                return {"ok": False, "status": "denied", "msg": "the user declined %s for %s" % (dname(d), c["name"])}
            if state != "approved":
                if x.get("deadline"):
                    msg = ("%s: asked the user through a DeviceHub notification. Approved automatically at %s "
                           "(%s from now) unless they decline. Wait for it in the background with "
                           "`devhub acquire %s \"%s\" --wait 15m`, or run the same acquire again after %s.") % (
                        dname(d), hm(x["deadline"]), fmt_dur(x["deadline"] - now()), dev, purpose, hm(x["deadline"]))
                else:
                    msg = ("%s needs the user's OK: the request is waiting in DeviceHub (no automatic yes on this "
                           "device). Wait with `devhub acquire %s \"%s\" --wait 30m`.") % (dname(d), dev, purpose)
                return {"ok": False, "status": "approval", "msg": msg, "deadline": x.get("deadline")}
        rest = [e for e in q if not same(e, c)]
        if rest:
            H.queues[dev] = rest
        else:
            H.queues.pop(dev, None)
        for qdev, qq in list(H.queues.items()):    # it has a device now: places kept for it elsewhere are free again
            qq[:] = [e for e in qq if not (same(e, c) and (e.get("held") or e.get("offer_from")))]
            if not qq:
                H.queues.pop(qdev)
        H.locks[dev] = {"holder": {"session": c["session"], "name": c["name"], "ui": bool(c.get("ui"))},
                        "purpose": purpose, "since": now(), "lease_until": now() + ttl, "ttl": ttl,
                        "watch": watch, "watch_seen": False, "state": "held", "check": None,
                        "stale_since": None, "last_activity": now(), "renewals": 0, "approved": bool(approved)}
        if approved:
            H.chain[dev] = now()
        save_state()
    how = ""
    if approved:
        how = {"chain": " (approved automatically: request chain, last approval/approved use %s ago)" % fmt_dur(approved.get("age", 0)),
               "timeout": " (approved: no answer from the user in %d min)" % H.settings["approval_timeout_min"],
               "user": " (approved by the user)",
               "away": " (approved automatically: the user is away)"}.get(approved.get("by"), " (approved)")
    event(dev, "lock", "taken by %s for %s: %s%s" % (c["name"], fmt_dur(ttl), purpose, how), c["name"])
    return {"ok": True, "status": "acquired",
            "msg": "%s is yours until %s%s%s. Release it when done: devhub release %s" % (
                dname(d), hm(now() + ttl), " (kept while '%s' runs)" % watch if watch else "", how, dev)}


def do_renew(dev, c, body):
    d = need_device(dev)
    with H.lock:
        lk = H.locks.get(dev)
        if not lk:
            return {"ok": False, "status": "free", "msg": "%s is not locked: acquire it instead" % dname(d)}
        if not same(lk["holder"], c) and not c.get("ui"):
            return {"ok": False, "status": "busy", "msg": "%s holds it, not you" % lk["holder"]["name"]}
        ttl = clamp_ttl(body.get("ttl"))
        if not lk["holder"].get("session") and c.get("session") and same(lk["holder"], c):
            lk["holder"]["session"] = c["session"]
        lk.update(lease_until=now() + ttl, ttl=ttl, state="held", check=None, stale_since=None, last_activity=now())
        lk["renewals"] = lk.get("renewals", 0) + 1
        note = str(body.get("note") or "").strip()[:200]
        if note:
            lk["note"] = note
        if body.get("watch"):
            lk["watch"], lk["watch_seen"] = str(body["watch"])[:80], False
        save_state()
    event(dev, "renew", "%s renewed it for %s%s" % (c["name"], fmt_dur(ttl), ": " + note if note else ""), c["name"])
    return {"ok": True, "status": "renewed", "msg": "%s: lease now ends %s" % (dname(d), hm(now() + ttl))}


def release_lock(dev, why, by=None, notify_holder=False):
    d = H.devices.get(dev)
    with H.lock:
        lk = H.locks.pop(dev, None)
        if lk and lk.get("approved"):
            H.chain[dev] = now()      # the next session's request within the chain window needs no new OK
        save_state()
    if not lk:
        return None
    event(dev, "release", "%s (held by %s since %s)" % (why, lk["holder"]["name"], hm(lk["since"])), by)
    if notify_holder and not lk["holder"].get("ui"):
        add_notice(lk["holder"], "[DeviceHub] Your lock on %s was released: %s. Take it again before you change anything there." % (
            dname(d) if d else dev, why))
    q = prune_queue(dev)
    if q:
        add_notice(q[0], "[DeviceHub] %s is free now and you are first in line: devhub acquire %s \"...\"" % (
            dname(d) if d else dev, dev))
    return lk


def do_release(dev, c, body):
    d = need_device(dev)
    with H.lock:
        lk = H.locks.get(dev)
        if not lk:
            return {"ok": True, "status": "free", "msg": "%s was not locked" % dname(d)}
        mine = same(lk["holder"], c)
        if not mine and not (c.get("ui") and body.get("force")):
            return {"ok": False, "status": "busy", "msg": "%s holds %s; only they (or the user in the UI) can release it" % (
                lk["holder"]["name"], dname(d))}
    warn = []
    if mine and not c.get("ui"):
        st = fresh_status(dev, max_age=2)
        dirty = str(body.get("dirty") or "").strip()
        if st is None:
            warn.append("could not check the device (no shared connection)")
        else:
            if d["fe_proc"] and not fe_up(d, st):
                if not dirty:
                    return {"ok": False, "status": "dirty",
                            "msg": "%s: %s is not running. Leave the device as you found it: `devhub fe %s start`, "
                                   "or release with --dirty \"why\"." % (dname(d), d["fe_proc"], dev)}
                warn.append("%s left stopped: %s" % (d["fe_proc"], dirty))
            if st.get("app"):
                warn.append("still running: %s (%d MB)" % (st["app"]["name"], st["app"]["rss"] // 1024))
    why = "released by %s" % c["name"] if mine else "force-released by %s" % c["name"]
    if warn:
        why += " - " + "; ".join(warn)
    release_lock(dev, why, c["name"], notify_holder=not mine)
    return {"ok": True, "status": "released", "msg": "%s released%s" % (dname(d), " (" + "; ".join(warn) + ")" if warn else ""),
            "warnings": warn}


def open_check(dev, lk, kind, reason):
    t = now()
    lk["state"] = "check"
    lk["check"] = {"kind": kind, "reason": reason, "asked": t, "delivered": None, "last_nag": 0,
                   "deadline": t + H.settings["unseen_grace_min"] * 60, "stop_blocked": False}
    event(dev, "check", "asked %s: still needed? (%s)" % (lk["holder"]["name"], reason))
    if lk["holder"].get("ui"):
        alert(dev, "Your lock on %s: %s. Still using it?" % (H.devices[dev].get("name"), reason))


def check_text(dev, lk):
    d = H.devices[dev]
    ch = lk["check"] or {}
    auto = H.settings["auto_release"]
    tail = {"never": "", "when_clean": " If the device is clean by then, the lock is released for you.",
            "always": " It may then be released for you."}.get(auto, "")
    return ("[DeviceHub] Lock check for %s: you (\"%s\") took it at %s for \"%s\"; %s.\n"
            "Device now: %s.\n"
            "Still needed? `devhub renew %s 30m \"why\"` (add --watch NAME to keep it while a job runs). "
            "Done? Leave it clean (front end running) and `devhub release %s`.\n"
            "No answer by %s: the user is asked to release it.%s") % (
        dname(d), lk["holder"]["name"], hm(lk["since"]), lk["purpose"], ch.get("reason", "please confirm"),
        evidence(dev, lk), dev, dev, hm(ch.get("deadline", now())), tail)


def take_notices(c, channel=None):
    out = []
    with H.lock:
        keys = {hkey(c)}
        if c.get("session") and c.get("name"):
            keys.add("n:" + c["name"].lower())
        for k in keys:
            out += H.notices.pop(k, [])
        for dev, lk in H.locks.items():
            if not same(lk["holder"], c) or dev not in H.devices:
                continue
            ch = lk.get("check")
            if lk["state"] in ("check", "stale") and ch:
                if ch.get("delivered") is None or now() - ch.get("last_nag", 0) > 300:
                    out.append(check_text(dev, lk))
                    if ch.get("delivered") is None:
                        ch["delivered"] = now()
                        ch["deadline"] = max(ch["deadline"], now() + H.settings["check_grace_min"] * 60)
                        event(dev, "check", "question reached %s" % lk["holder"]["name"])
                    ch["last_nag"] = now()
            elif channel == "SessionStart":
                out.append("[DeviceHub] You hold the lock on %s since %s for \"%s\" (lease to %s). "
                           "Release it when done: devhub release %s" % (
                               dname(H.devices[dev]), hm(lk["since"]), lk["purpose"], hm(lk["lease_until"]), dev))
        if out:
            H.dirty = True
    return out


def stop_check(c):
    with H.lock:
        for dev, lk in H.locks.items():
            ch = lk.get("check")
            if same(lk["holder"], c) and lk["state"] in ("check", "stale") and ch and not ch.get("stop_blocked"):
                ch["stop_blocked"] = True
                if ch.get("delivered") is None:
                    ch["delivered"] = now()
                    ch["deadline"] = max(ch["deadline"], now() + H.settings["check_grace_min"] * 60)
                ch["last_nag"] = now()
                H.dirty = True
                return check_text(dev, lk) + "\nAnswer with one of those commands before you finish this turn."
    return None


def is_devhub_call(part):
    """Does this piece of a shell command run devhub (or the legacy cube-lock.sh wrapper)?"""
    for w in part.split():
        w = w.strip("\"'")
        if w in ("(", "{", "!", "if", "then", "else", "do", "while", "until", "time", "exec", "command") or \
                re.fullmatch(r"\w+=\S*", w) or re.search(r"(^|[/\\])python3?(\.exe)?$", w):
            continue
        return re.search(r"(^|[/\\])(devhub(\.py|\.cmd)?|cube-lock\.sh)$", w) is not None
    return False


def shell_parts(cmd):
    """Split a shell command at unquoted ; & && | || and newlines (good enough for the guard)."""
    parts, cur, q, i = [], [], None, 0
    while i < len(cmd):
        ch = cmd[i]
        if q:
            if ch == q:
                q = None
            elif ch == "\\" and q == '"' and i + 1 < len(cmd):
                cur.append(ch)
                i += 1
                ch = cmd[i]
        elif ch in "'\"":
            q = ch
        elif ch == "\\" and i + 1 < len(cmd):
            cur.append(ch)
            i += 1
            ch = cmd[i]
        elif ch in ";&|\n":
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return [p for p in parts if p.strip()]


def guard(c, cmd):
    """PreToolUse: a raw command names a device someone else holds -> deny (or warn). devhub's own calls in the
    command are left out (devhub enforces the locks itself, and a waiting acquire must not be blocked), but
    everything else in the same command line is checked: `devhub acquire X | tail -1 && cp ... X` must not slip
    through when the acquire is refused."""
    own = [p for p in shell_parts(cmd) if is_devhub_call(p)]
    rest = "\n".join(p for p in shell_parts(cmd) if not is_devhub_call(p))
    if not rest.strip():
        return None
    acq = any(re.search(r"\sacquire\s", p) for p in own)
    cmd = rest + ("\n# devhub:ro" if "devhub:ro" in cmd else "")
    deny = []
    for d in list(H.devices.values()):
        if not d["enabled"] or d["guard"] == "off" or not ref_pattern(d).search(cmd):
            continue
        dev = d["id"]
        lk = H.locks.get(dev)
        if lk and same(lk["holder"], c):
            lk["last_activity"] = now()
            H.dirty = True
            continue
        if lk:
            if "devhub:ro" in cmd or "DEVHUB_RO=1" in cmd:
                continue
            text = ("%s. Do not change it. Use a free device instead (`devhub status`), or wait for this one with "
                    "`devhub acquire %s \"purpose\" --wait 20m --need \"why this device\"`; "
                    "a read-only look (logs, files) is fine: add the comment `# devhub:ro` to the command.") % (busy_text(d, lk), dev)
            if acq:
                text += (" This command also runs `devhub acquire`, but the device is held by someone else, so the rest "
                         "would run under their lock. Run the acquire as its own command first and go on only when it "
                         "succeeds (never pipe it into tail/grep: the pipe hides a refusal).")
            # a lock taken by name only (old cube-lock.sh) cannot tell its own session apart: warn, never block
            if d["guard"] == "enforce" and lk["holder"].get("session"):
                deny.append(text)
                event(dev, "guard", "blocked a command from %s" % c["name"], c["name"])
            else:
                add_notice(c, "[DeviceHub] Warning: " + text)
        else:
            k = hkey(c) + "|" + dev
            if now() - H.nag.get(k, 0) > 600:
                H.nag[k] = now()
                add_notice(c, "[DeviceHub] %s is free and you hold no lock on it. Before anything that changes it "
                              "(launch/kill, front end, installs, copies): devhub acquire %s \"purpose\"%s" % (
                                  dname(d), dev, approval_hint(d)))
    return "\n".join(deny) or None


def end_session(sid):
    with H.lock:
        s = H.sessions.get(sid)
        if s:
            s["ended"] = now()
        held = [dev for dev, lk in H.locks.items() if lk["holder"].get("session") == sid]
        H.dirty = True
    for dev in held:
        auto_release(dev, "its Claude session ended")


def auto_release(dev, why):
    d = H.devices.get(dev)
    lk = H.locks.get(dev)
    if not lk:
        return
    clean = device_clean(dev)
    note = "" if clean is not False else " - device was NOT left clean"
    release_lock(dev, "auto-released: " + why + note, "DeviceHub", notify_holder=True)
    if clean is not True and d:
        alert(dev, "%s was released because %s (held by %s). %s" % (
            d.get("name"), why, lk["holder"]["name"],
            "Check the device: front end not running or an app left open." if clean is False else "Device state unknown."))


def tick():
    with H.lock:
        gone = import_lock_files()
    for dev in gone:
        release_lock(dev, "released through the old lock file", "cube-lock.sh")
    t = now()
    todo = []
    with H.lock:
        for dev, lk in list(H.locks.items()):
            d = H.devices.get(dev)
            if not d:
                del H.locks[dev]
                H.dirty = True
                continue
            h = lk["holder"]
            if h.get("session"):
                s = H.sessions.get(h["session"])
                if s and s.get("ended"):
                    todo.append((dev, "its Claude session ended"))
                    continue
            w = rt(dev).get("watch")
            if lk.get("watch") and w and w[2] == lk["watch"] and w[1] > lk.get("watch_checked", 0) and w[1] >= lk["since"]:
                lk["watch_checked"] = w[1]
                if w[0] > 0:
                    lk["watch_seen"] = True
                    lk["lease_until"] = max(lk["lease_until"], t + H.settings["watch_extend_min"] * 60)
                    if lk["state"] != "held" and (lk.get("check") or {}).get("kind") == "watch":
                        lk.update(state="held", check=None, stale_since=None)
                elif (lk.get("watch_seen") or t - lk["since"] > 180) and lk["state"] == "held":
                    open_check(dev, lk, "watch", "the job it watched ('%s') is no longer running" % lk["watch"])
                H.dirty = True
            if lk["state"] == "held" and t > lk["lease_until"]:
                open_check(dev, lk, "lease", "its %s lease ran out at %s" % (fmt_dur(lk["ttl"]), hm(lk["lease_until"])))
                H.dirty = True
            elif lk["state"] == "check" and t > lk["check"]["deadline"]:
                lk["state"] = "stale"
                lk["stale_since"] = t
                H.dirty = True
                seen = "did not answer" if lk["check"].get("delivered") else "has not picked up the question (session idle?)"
                event(dev, "stale", "%s %s" % (h["name"], seen))
                alert(dev, "%s %s whether it still needs %s (held since %s, %s). Release it in DeviceHub if it is done." % (
                    h["name"], seen, d.get("name"), hm(lk["since"]), evidence(dev, lk)))
            elif lk["state"] == "stale":
                pol = H.settings["auto_release"]
                if (pol != "never" and t - (lk.get("stale_since") or t) > H.settings["stale_release_min"] * 60
                        and t - holder_last_active(h, lk) > H.settings["idle_release_min"] * 60):
                    if pol == "always" or device_clean(dev) is True:
                        todo.append((dev, "no answer to the lock check, holder idle %s" %
                                     fmt_dur(t - holder_last_active(h, lk))))
        for dev in list(H.queues):
            prune_queue(dev)
        note_usable()
        # install results (done / failed) leave the alert strip by themselves after 3 minutes
        H.alerts[:] = [a for a in H.alerts if not (a.get("kind") == "install" and t - a["t"] > 180)]
        if t - H.swept > 60:
            H.swept = t
            threading.Thread(target=sweep_uploads, daemon=True).start()
        for dev in list(H.requests):     # 'auto' approval: no answer by the deadline counts as yes
            for x in H.requests.get(dev, []):
                if x["state"] == "pending" and H.settings.get("away_mode"):   # Away mode switched on while it waited
                    x.update(state="approved", decided=t, by="away")
                    d = H.devices.get(dev)
                    add_notice(x, "[DeviceHub] %s: approved (the user is away, so requests are approved at once). "
                                  "Take it now: devhub acquire %s \"%s\"" % (dname(d) if d else dev, dev, x["purpose"]))
                    event(dev, "approval", "approved for %s automatically: away mode" % x["name"])
                    H.dirty = True
                elif x["state"] == "pending" and x.get("deadline") and t >= x["deadline"]:
                    x.update(state="approved", decided=t, by="timeout")
                    H.chain[dev] = t
                    d = H.devices.get(dev)
                    add_notice(x, "[DeviceHub] %s: the user did not answer within %d min, so your request is approved. "
                                  "Take it now: devhub acquire %s \"%s\"" % (dname(d) if d else dev,
                                                                           H.settings["approval_timeout_min"], dev, x["purpose"]))
                    event(dev, "approval", "approved for %s automatically (no answer in %d min)" % (
                        x["name"], H.settings["approval_timeout_min"]))
                    H.dirty = True
            prune_requests(dev)
        for sid, s in list(H.sessions.items()):
            if t - max(s.get("last", 0), s.get("first", 0)) > 7 * 86400 and not any(
                    lk["holder"].get("session") == sid for lk in H.locks.values()):
                del H.sessions[sid]
                H.notices.pop("s:" + sid, None)
                H.dirty = True
        for k, v in list(H.nag.items()):
            if t - v > 3600:
                del H.nag[k]
        dirty = H.dirty
    for dev, why in todo:
        auto_release(dev, why)
    if dirty:
        save_state()


H.lockfiles = {}    # id -> legacy lock file content last written or seen (None = absent)


def read_lock_file(p):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def sync_lock_files():
    """Write legacy cube-lock.sh files from the hub's locks: owner/since/purpose while locked, absent when free."""
    for dev, d in H.devices.items():
        if not d.get("lock_file"):
            continue
        p = to_wsl(d["lock_file"])
        lk = H.locks.get(dev)
        want = ("owner=%s\nsince=%s\npurpose=%s\n" % (lk["holder"]["name"], time.strftime("%Y-%m-%d %H:%M", time.localtime(lk["since"])),
                                                      lk["purpose"])) if lk else None
        try:
            if want != read_lock_file(p):
                if want is None:
                    os.unlink(p)
                else:
                    with open(p, "w", encoding="utf-8", newline="\n") as f:
                        f.write(want)
            H.lockfiles[dev] = want
        except OSError as e:
            log("lock file", p, e)


def import_lock_files():
    """Pick up changes made through the old script itself: a new file is a lock, a vanished file a release."""
    released = []
    for dev, d in H.devices.items():
        p = to_wsl(d.get("lock_file") or "")
        if not p:
            continue
        have = read_lock_file(p)
        known = dev in H.lockfiles
        if known and have == H.lockfiles[dev]:
            continue
        H.lockfiles[dev] = have
        lk = H.locks.get(dev)
        if have is None:
            if lk and known:
                released.append(dev)
            continue
        if lk:
            if known:
                log("lock file %s changed while %s holds the lock: the hub's lock wins" % (p, lk["holder"]["name"]))
            continue
        kv = dict(line.strip().partition("=")[::2] for line in have.splitlines() if "=" in line)
        try:
            since = time.mktime(time.strptime(kv.get("since", ""), "%Y-%m-%d %H:%M"))
        except ValueError:
            since = now()
        ttl = H.settings["default_ttl_min"] * 60
        H.locks[dev] = {"holder": {"session": None, "name": kv.get("owner") or "unknown", "ui": False},
                        "purpose": kv.get("purpose") or "(from the old lock file)", "since": since,
                        "lease_until": max(since + ttl, now() + 600), "ttl": ttl, "watch": "", "watch_seen": False,
                        "state": "held", "check": None, "stale_since": None, "last_activity": now(), "renewals": 0}
        H.dirty = True
        event(dev, "lock", "taken through %s by %s at %s: %s" % (d["lock_file"], kv.get("owner"), hm(since), kv.get("purpose")))
    return released


# ---------------------------------------------------------------- actions

def fe_action(dev, d, action):
    def pids():
        rc, out, _ = ssh_run(d, "pidof %s" % shlex.quote(d["fe_proc"]), timeout=10)
        return out.decode().split()

    if action in ("stop", "restart"):
        if not d["fe_stop"]:
            raise ApiError(400, "no front-end stop command for this device (Edit > Front end)")
        rc, out, err = ssh_run(d, d["fe_stop"], timeout=45)
        if action == "stop":
            time.sleep(1.5)
            left = d["fe_proc"] and pids()
            return (not left), ("%s stopped" % (d["fe_proc"] or "front end")) if not left else \
                "stop command ran (exit %d) but %s is still running" % (rc, d["fe_proc"])
        time.sleep(2)
    if not d["fe_start"]:
        raise ApiError(400, "no front-end start command for this device (Edit > Front end)")
    rc, out, err = ssh_run(d, d["fe_start"], timeout=45)
    if rc != 0:
        return False, "start command failed (exit %d): %s" % (rc, err.decode("utf-8", "replace").strip()[-200:])
    if not d["fe_proc"]:
        return True, "start command sent (no process name set, so not verified)"
    time.sleep(3)
    p1 = pids()
    time.sleep(10)
    p2 = pids()
    if p1 and p1 == p2:
        return True, "%s running (pid %s, unchanged after 10 s)" % (d["fe_proc"], " ".join(p2))
    if p2:
        return False, "%s restarted in between (pid %s -> %s): it may be looping" % (d["fe_proc"], " ".join(p1), " ".join(p2))
    return False, "%s did not stay up" % d["fe_proc"]


def png_bytes(w, h, rows):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(b"".join(rows), 6)) + chunk(b"IEND", b""))


def fb_to_png(raw, w, h, stride, bpp):
    """Framebuffer rows (BGRA/BGR little-endian, or RGB565) to an RGB PNG."""
    rows = []
    for y in range(h):
        r = raw[y * stride: y * stride + w * bpp // 8]
        rgb = bytearray(w * 3)
        if bpp == 32:
            rgb[0::3], rgb[1::3], rgb[2::3] = r[2::4], r[1::4], r[0::4]
        elif bpp == 24:
            rgb[0::3], rgb[1::3], rgb[2::3] = r[2::3], r[1::3], r[0::3]
        else:
            for i, v in enumerate(struct.unpack("<%dH" % w, r)):
                rgb[i * 3] = (v >> 11) * 255 // 31
                rgb[i * 3 + 1] = ((v >> 5) & 63) * 255 // 63
                rgb[i * 3 + 2] = (v & 31) * 255 // 31
        rows.append(b"\x00" + bytes(rgb))
    return png_bytes(w, h, rows)


# Wayland front ends (ROCKNIX's sway) never reach /dev/fb0, so a framebuffer capture comes back blank there:
# grim captures the compositor. Finds the compositor's socket itself; exit 3 = no Wayland here.
GRIM_SH = r"""
command -v grim >/dev/null 2>&1 || exit 3
for r in "$XDG_RUNTIME_DIR" /var/run/0-runtime-dir /run/user/$(id -u) /run/0-runtime-dir; do
  for s in "$r"/wayland-*; do
    case "$s" in *.lock) continue;; esac
    [ -S "$s" ] || continue
    export XDG_RUNTIME_DIR="$r" WAYLAND_DISPLAY="${s##*/}"
    exec grim -l 1 -t png -
  done
done
exit 3
"""


def save_shot(dev, png):
    name = "%s-%s.png" % (dev, time.strftime("%Y%m%d-%H%M%S"))
    shots = os.path.join(DATA, "shots")
    with open(os.path.join(shots, name), "wb") as fh:
        fh.write(png)
    old = sorted(x for x in os.listdir(shots) if x.startswith(dev + "-"))
    for x in old[:-30]:
        try:
            os.unlink(os.path.join(shots, x))
        except OSError:
            pass
    return name


def screenshot(dev, d):
    rc, png, err = ssh_run(d, "sh -s", inp=GRIM_SH.encode(), timeout=60)
    if rc == 0 and png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 33:
        w, h = struct.unpack(">II", png[16:24])
        return save_shot(dev, png), w, h
    if rc != 3:
        log("screenshot", dev, "grim failed (exit %d): %s" % (rc, err.decode("utf-8", "replace").strip()[-200:]))
    # what the display shows: the scanned-out plane. /dev/fb0 comes last, it can hold stale console text while a
    # KMS app (PortMaster, a game, dArkOS's ES) is what is really on screen
    shot = kms_screenshot(dev, d)
    if shot:
        return shot
    rc, out, err = ssh_run(d, "cd /sys/class/graphics/fb0 && echo $(cat virtual_size) $(cat bits_per_pixel) "
                              "$(cat stride 2>/dev/null || echo 0) $(cat pan 2>/dev/null || echo 0,0) $(head -n1 modes 2>/dev/null)",
                           timeout=10)
    f = out.decode().split()
    if rc != 0 or len(f) < 4:
        raise ApiError(500, "no framebuffer info on the device: %s" % err.decode("utf-8", "replace").strip()[-200:])
    vw, vh = [int(x) for x in f[0].split(",")]
    bpp, stride = int(f[1]), int(f[2])
    pan_y = int(f[3].split(",")[1]) if "," in f[3] else 0
    m = re.search(r"(\d+)x(\d+)", f[4]) if len(f) > 4 else None
    w, h = (int(m.group(1)), int(m.group(2))) if m else (vw, vh)
    stride = stride or vw * bpp // 8
    if bpp not in (16, 24, 32):
        raise ApiError(500, "framebuffer is %d bpp: not supported" % bpp)
    sudo = "" if d["user"] == "root" else "sudo -n "
    rc, raw, err = ssh_run(d, "%sdd if=/dev/fb0 bs=%d skip=%d count=%d 2>/dev/null" % (sudo, stride, pan_y, h), timeout=60)
    if len(raw) < stride * (h - 1) + w * bpp // 8:
        raise ApiError(500, "framebuffer read came back short (%d bytes): %s" % (len(raw), err.decode("utf-8", "replace").strip()[-200:]))
    if raw.count(raw[:1]) == len(raw):
        raise ApiError(500, "the framebuffer is blank and the display could not be read directly (no grim, no ffmpeg "
                            "kmsgrab, no python3 + sudo for the DRM read)")
    return save_raw_shot(dev, d, raw, w, h, stride, bpp)


# KMS/DRM front ends (dArkOS's EmulationStation, SDL kmsdrm games) bypass /dev/fb0: ffmpeg's kmsgrab reads the
# scanned-out plane. Its ffmpeg may lack a PNG encoder, so raw BGR0 comes back after a "KMS <w>x<h> <rc>" line.
KMS_SH = r"""
command -v ffmpeg >/dev/null 2>&1 || exit 3
S=""; [ "$(id -u)" = 0 ] || { sudo -n true 2>/dev/null && S="sudo -n"; } || exit 3
E=$(mktemp)
$S ffmpeg -hide_banner -f kmsgrab -device /dev/dri/card0 -i - -frames:v 1 -vf hwdownload,format=bgr0 -f rawvideo - 2>"$E" >"$E.raw"
rc=$?
sz=$(grep -o 'bgr0[^,]*, [0-9]*x[0-9]*' "$E" | grep -o '[0-9]*x[0-9]*' | head -1)
echo "KMS ${sz:-0x0} $rc"
cat "$E.raw"
rm -f "$E" "$E.raw"
"""


def kms_screenshot(dev, d):
    try:
        rc, out, err = ssh_run(d, "sh -s", inp=KMS_SH.encode(), timeout=60)
    except subprocess.TimeoutExpired:
        rc, out, err = -1, b"", b"timed out"
    head, _, raw = out.partition(b"\n")
    m = re.match(rb"KMS (\d+)x(\d+) 0$", head)
    if rc == 0 and m:
        w, h = int(m.group(1)), int(m.group(2))
        if w * h and len(raw) >= w * h * 4 and raw.count(raw[:1]) != len(raw):
            return save_raw_shot(dev, d, raw, w, h, w * 4, 32)
    log("screenshot", dev, "kmsgrab failed: %s %s" % (head[:60], err.decode("utf-8", "replace").strip()[-160:]))
    return drm_screenshot(dev, d)


# Old Rockchip kernels (4.4, dArkOS on RK3326 like the R36S) lack GETFB2, so ffmpeg's kmsgrab fails there
# ("Failed to get framebuffer: Invalid argument"). The legacy calls still work: find the plane on screen, GETFB for
# its buffer handle, MAP_DUMB + mmap to read it. python3 with ioctl, no tools needed; root (sudo) for the handle.
DRMGRAB_PY = r'''
import array, fcntl, mmap, os, struct, sys
def IOWR(nr, size): return (3 << 30) | (size << 16) | (0x64 << 8) | nr
def IOW(nr, size): return (1 << 30) | (size << 16) | (0x64 << 8) | nr
fd = os.open("/dev/dri/card0", os.O_RDWR)
fcntl.ioctl(fd, IOW(0x0D, 16), struct.pack("QQ", 2, 1))
res = bytearray(struct.pack("QI4x", 0, 0)); fcntl.ioctl(fd, IOWR(0xB5, 16), res)
n = struct.unpack("QI4x", res)[1]
ids = array.array("I", [0] * n)
res = bytearray(struct.pack("QI4x", ids.buffer_info()[0], n)); fcntl.ioctl(fd, IOWR(0xB5, 16), res)
# every plane that shows a buffer. A device with two outputs (RG552: panel + HDMI) has a second plane that may hold
# old console text, so the plane matching the device's own screen size (argv, either orientation) wins, then the
# biggest plane that has any content
want = tuple(int(x) for x in sys.argv[1:3]) if len(sys.argv) > 2 else ()
best = None
for pid in ids:
    p = bytearray(struct.pack("6IQ", pid, 0, 0, 0, 0, 0, 0)); fcntl.ioctl(fd, IOWR(0xB6, 32), p)
    _, crtc, fb = struct.unpack("6IQ", p)[:3]
    if not fb or not crtc:
        continue
    c = bytearray(struct.pack("7I", fb, 0, 0, 0, 0, 0, 0)); fcntl.ioctl(fd, IOWR(0xAD, 28), c)
    _, w, h, pitch, bpp, depth, handle = struct.unpack("7I", c)
    if not handle:
        continue
    try:
        m = bytearray(struct.pack("IIQ", handle, 0, 0)); fcntl.ioctl(fd, IOWR(0xB3, 16), m)
        data = mmap.mmap(fd, pitch * h, mmap.MAP_SHARED, mmap.PROT_READ, offset=struct.unpack("IIQ", m)[2])[:pitch * h]
    except OSError:
        continue
    key = ((w, h) in (want, want[::-1]), len(set(data[::4099])) > 1, w * h)
    if best is None or key > best[0]:
        best = (key, w, h, pitch, bpp, data)
if not best:
    sys.exit("no readable framebuffer on the display")
_, w, h, pitch, bpp, data = best
sys.stdout.buffer.write(b"DRM %d %d %d %d\n" % (w, h, pitch, bpp) + data)
'''


def drm_screenshot(dev, d):
    sh = ('command -v python3 >/dev/null || exit 3\nS=""; [ "$(id -u)" = 0 ] || { sudo -n true 2>/dev/null && S="sudo -n"; } || exit 3\n'
          'P=$(mktemp)\ncat > "$P" <<\'DEVHUB_PY\'\n%s\nDEVHUB_PY\n$S python3 "$P" %s; rc=$?; rm -f "$P"; exit $rc\n') % (
        DRMGRAB_PY, " ".join(re.findall(r"\d+", d.get("screen") or "")[:2]))
    try:
        rc, out, err = ssh_run(d, "sh -s", inp=sh.encode(), timeout=60)
    except subprocess.TimeoutExpired:
        return None
    head, _, raw = out.partition(b"\n")
    m = re.match(rb"DRM (\d+) (\d+) (\d+) (\d+)$", head)
    if rc != 0 or not m:
        log("screenshot", dev, "legacy DRM read failed: %s" % err.decode("utf-8", "replace").strip()[-200:])
        return None
    w, h, pitch, bpp = (int(x) for x in m.groups())
    if bpp not in (16, 24, 32) or len(raw) < pitch * h or raw.count(raw[:1]) == len(raw):
        return None
    return save_raw_shot(dev, d, raw, w, h, pitch, bpp)


def save_raw_shot(dev, d, raw, w, h, pitch, bpp):
    """A captured buffer as PNG, turned upright when the panel scans out in portrait but the device's screen is
    landscape (RG552): software rotation, so every capture route (framebuffer, kmsgrab, DRM) needs it."""
    scr = re.match(r"(\d+)\s*x\s*(\d+)", d.get("screen") or "")
    if scr and h > w and int(scr.group(1)) > int(scr.group(2)):
        raw, w, h, pitch = rotate_cw(raw, w, h, pitch, bpp // 8)
    return save_shot(dev, fb_to_png(raw, w, h, pitch, bpp)), w, h


def rotate_cw(raw, w, h, pitch, px):
    """Turn a buffer 90 degrees clockwise: a landscape device whose panel scans out in portrait."""
    mv = memoryview(raw)
    rows = []
    for x in range(w):
        o = x * px
        rows.append(b"".join(mv[(h - 1 - y) * pitch + o:(h - 1 - y) * pitch + o + px] for y in range(h)))
    return b"".join(rows), h, w, h * px


# ---------------------------------------------------------------- port install through PortMaster's harbourmaster

UPLOADS = os.path.join(RUN, "uploads")
H.uploads = {}   # id -> {"id", "path", "name", "dev", "size", "sha1", "at", "info"}
H.jobs = {}      # id -> job


class JobError(Exception):
    pass


LOGURU = re.compile(r"^\d{4}-\d\d-\d\d [\d:.]+ \| (\w+)\s*\| \S+ - (.*)$")


# the folders PortMaster.sh and harbourmaster look in, after an explicit folder from the profile
PM_FIND = r"""
PM=""
for d in "$PMDIR" /opt/system/Tools/PortMaster /opt/tools/PortMaster "${XDG_DATA_HOME:-$HOME/.local/share}/PortMaster" \
         /userdata/system/.local/share/PortMaster /mnt/mmc/MUOS/PortMaster /mnt/sdcard/MUOS/PortMaster \
         /roms2/tools/PortMaster /roms/tools/PortMaster /storage/roms/ports/PortMaster /mnt/SDCARD/Apps/PortMaster \
         /mnt/SDCARD/MIYOO_EX/PortMaster /mnt/SDCARD/Roms/.portmaster/PortMaster /roms/ports/PortMaster; do
  if [ -n "$d" ] && [ -f "$d/harbourmaster" ]; then PM="$d"; break; fi
done
S=""
if [ "$(id -u)" != 0 ] && [ -n "$PM" ] && [ ! -w "$PM" ]; then sudo -n true 2>/dev/null && S="sudo -n"; fi
"""

PM_PROBE = r"""
echo "PM=$PM"
[ -n "$PM" ] || exit 0
echo "VER=$(cat "$PM/version" 2>/dev/null)"
echo "FREE=$(df -k "$PM" 2>/dev/null | awk 'NR==2 {print $4}')"
echo "PY=$(command -v python3)"
echo "SUDO=$S"
"""

PM_INSTALL = r"""
cd "$PM" || { echo "DEVHUB-RC=90"; exit 0; }
case "$PM" in */.local/share/PortMaster) XDG_DATA_HOME="${PM%/PortMaster}"; export XDG_DATA_HOME;; esac
H=""
[ "$(id -u)" = 0 ] || { sudo -n true 2>/dev/null && H="sudo -n -E"; }
$H ./harbourmaster --no-check --no-colour install "$Z" </dev/null 2>&1
rc=$?
$S rm -rf "$PM/.devhub-staging"
echo "DEVHUB-RC=$rc"
"""


def inspect_zip(path):
    """What a port zip holds (harbourmaster does the real check on the device)."""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return {"errors": ["this is not a zip file"], "warnings": []}
    infos = z.infolist()
    names = set(i.filename for i in infos)
    errors, warnings = [], []
    bad = [i.filename for i in infos if i.filename.startswith("/") or "\\" in i.filename or ".." in i.filename.split("/")]
    if bad:
        errors.append("unsafe paths in the zip: " + ", ".join(bad[:3]))
    root_files, dirs = set(), set()
    for i in infos:
        parts = i.filename.split("/")
        if len(parts) == 1:
            root_files.add(parts[0])
        elif parts[0]:
            dirs.add(parts[0])
    launchers = sorted(f for f in root_files if f.lower().endswith(".sh"))
    title, runtime, port_json = None, [], None
    for cand in sorted(n for n in names if n.count("/") <= 1 and (n.endswith("port.json"))):
        try:
            pj = json.loads(z.read(cand).decode("utf-8", "replace"))
        except ValueError:
            continue
        attr = pj.get("attr") or {}
        title, port_json = attr.get("title") or pj.get("name"), cand
        rtm = attr.get("runtime") or []
        runtime = rtm if isinstance(rtm, list) else [rtm]
        break
    if not launchers and ("PortMaster/harbourmaster" in names or "PortMaster/PortMaster.sh" in names):
        # PortMaster itself (its release zip): harbourmaster updates PortMaster in place, as its own updater does
        try:
            ver = z.read("PortMaster/version").decode("utf-8", "replace").strip()[:40]
        except KeyError:
            ver = ""
        return {"kind": "portmaster", "launchers": [], "dirs": sorted(dirs), "other": sorted(root_files),
                "files": sum(1 for i in infos if not i.is_dir()), "uncompressed": sum(i.file_size for i in infos),
                "title": "PortMaster %s" % ver if ver else "PortMaster", "runtime": [], "errors": errors,
                "warnings": ["this is PortMaster itself: harbourmaster updates PortMaster on the device in place"]}
    if not launchers:
        inner = [n for n in names if n.count("/") == 1 and n.lower().endswith(".sh")]
        errors.append("no launcher (.sh) at the top of the zip, so it is not a PortMaster port zip" +
                      (" (it looks wrapped in a folder: %s)" % inner[0].split("/")[0] if inner else ""))
    if launchers and not port_json:
        warnings.append("no port.json in the zip: harbourmaster may refuse it")
    if runtime:
        warnings.append("needs runtime %s: the launcher downloads it on first start if it is missing (internet needed)" %
                        ", ".join(str(x) for x in runtime))
    return {"launchers": launchers, "dirs": sorted(dirs), "other": sorted(root_files - set(launchers)),
            "files": sum(1 for i in infos if not i.is_dir()), "uncompressed": sum(i.file_size for i in infos),
            "title": title, "runtime": runtime, "errors": errors, "warnings": warnings}


def inspect_squashfs(path, name):
    """A PortMaster runtime (frt, mono, love, ...): a squashfs image that goes to <PortMaster>/libs/."""
    try:
        with open(path, "rb") as f:
            magic = f.read(4)
        size = os.path.getsize(path)
    except OSError:
        return {"errors": ["could not read the file"], "warnings": []}
    if magic not in (b"hsqs", b"sqsh"):
        return {"kind": "squashfs", "errors": ["this is not a squashfs image"], "warnings": []}
    return {"kind": "squashfs", "title": name[:-len(".squashfs")], "launchers": [], "dirs": ["libs"], "other": [name],
            "files": 1, "uncompressed": size, "runtime": [], "errors": [],
            "warnings": ["a runtime goes to PortMaster's libs folder; an older file with the same name is replaced"]}


def job_public(job, lines=40):
    v = {k: x for k, x in job.items() if k != "log"}
    v["log"] = job["log"][-lines:]
    return v


def job_log(job, line):
    line = line.rstrip()
    if line:
        job["log"].append(line[:300])
        del job["log"][:-500]


def device_job(dev):
    jobs = [j for j in H.jobs.values() if j["dev"] == dev and (j["state"] == "running" or now() - (j["ended"] or 0) < 900)]
    return job_public(max(jobs, key=lambda j: j["started"]), 12) if jobs else None


def remote_sh(script, **kv):
    return "".join("%s=%s\n" % (k, shlex.quote(str(v))) for k, v in kv.items()) + script


def ssh_args(d, cmd):
    return ["ssh", "-S", sock_path(d), "-o", "ControlMaster=no", "-o", "BatchMode=yes", "-o", "LogLevel=ERROR",
            "-p", str(d["port"]), target(d), cmd]


def push_file(d, local, remote, sha1, job):
    """Stream a file over the shared connection to <remote> (folder created, sudo when needed), SHA-1 checked."""
    part = remote + ".part"
    # the command travels in the ssh command line, so stdin carries nothing but the file
    cmd = ('P=%s; S=""; D="$(dirname "$P")"; [ "$(id -u)" = 0 ] || [ -w "$(dirname "$D")" ] || S="sudo -n"; '
           '$S mkdir -p "$D" && exec $S sh -c \'cat > "$1"\' sh "$P"') % shlex.quote(part)
    for attempt in (1, 2):
        job["sent"] = 0
        p = subprocess.Popen(ssh_args(d, cmd), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            with open(local, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    p.stdin.write(chunk)
                    job["sent"] += len(chunk)
            p.stdin.close()
        except BrokenPipeError:
            pass
        rc = p.wait()
        err = p.stderr.read().decode("utf-8", "replace").strip()
        if rc == 0:
            check = remote_sh('(sha1sum < "$P" 2>/dev/null || echo none) | cut -c1-40; wc -c < "$P"', P=part)
            # hashing a multi-GB zip on a slow handheld (RG351P: A35 cores, SD card) takes minutes: allow ~3 MB/s
            size = os.path.getsize(local)
            limit = max(180, int(size / (3 << 20)) + 120)
            job["step"] = "checking the copy (SHA-1, up to %d min)" % -(-limit // 60)
            try:
                rc2, out, _ = ssh_run(d, "sh -s", inp=check.encode(), timeout=limit)
            except subprocess.TimeoutExpired:
                raise JobError("the copy reached the device, but checking it (SHA-1 of %.1f GB) took longer than %d min "
                               "there: the device is too slow or busy. Try again when nothing else runs on it"
                               % (size / 1073741824, limit // 60))
            job["step"] = "sending"
            got = out.decode().split()
            if len(got) >= 2 and (got[0] == sha1 or (got[0] == "none" and got[1] == str(os.path.getsize(local)))):
                ssh_run(d, "sh -s", inp=remote_sh('S=""; [ -w "$P" ] || S="sudo -n"; $S mv -f "$P" "$R"',
                                                  P=part, R=remote).encode(), timeout=60)
                return
            job_log(job, "copy check failed (attempt %d): %s" % (attempt, " ".join(got) or "no answer"))
        else:
            job_log(job, "copy failed (attempt %d): %s" % (attempt, err[-200:] or "ssh exit %d" % rc))
    raise JobError("could not copy %s to the device" % os.path.basename(remote))


def install_job(job, d, up, refresh, took_lock, who):
    dev = d["id"]
    staged = None
    try:
        job["step"] = "locating PortMaster"
        try:
            rc, out, err = ssh_run(d, "sh -s", inp=remote_sh(PM_FIND + PM_PROBE, PMDIR=d.get("pm_dir") or "").encode(), timeout=40)
        except subprocess.TimeoutExpired:
            raise JobError("%s did not answer for 40 s while looking for PortMaster: is it still on and on Wi-Fi? "
                           "Try again once it is back online" % dname(d))
        info = dict(l.partition("=")[::2] for l in out.decode("utf-8", "replace").splitlines() if "=" in l)
        pm = info.get("PM")
        if "PM" not in info:      # the probe never ran: no answer, not a missing PortMaster
            e = err.decode("utf-8", "replace").strip().splitlines()
            raise JobError("lost the connection to %s while looking for PortMaster (%s): is it still on and on "
                           "Wi-Fi? Try again once it is back online" % (dname(d), e[-1][:120] if e else "ssh exit %d" % rc))
        if not pm:
            raise JobError("PortMaster not found on %s (install PortMaster, or set its folder under Edit > PortMaster folder)" % dname(d))
        job_log(job, "PortMaster %s in %s%s" % (info.get("VER") or "?", pm, " (via sudo)" if info.get("SUDO") else ""))
        if up["info"].get("kind") == "squashfs":       # a runtime: straight into PortMaster's libs folder
            dest = pm + "/libs/" + up["name"]
            free = int(info.get("FREE") or 0)
            if free and free < up["size"] // 1024:
                raise JobError("not enough space: %d MB needed, %d MB free" % (up["size"] // 1048576, free // 1024))
            rc, out, _ = ssh_run(d, "sh -s", inp=remote_sh('[ -f "$P" ] && wc -c < "$P"; true', P=dest).encode(), timeout=20)
            old = out.decode().strip()
            job["step"] = "sending"
            job["total"] = up["size"]
            push_file(d, up["path"], dest, up["sha1"], job)
            job_log(job, "copied %s to %s (%.1f MB, SHA-1 checked)%s" % (
                up["name"], pm + "/libs/", up["size"] / 1048576,
                "; replaced the older copy (%.1f MB)" % (int(old) / 1048576) if old.isdigit() else ""))
            job.update(state="done", step="installed", result="%s installed in PortMaster's libs on %s" % (up["name"], d["name"]))
            event(dev, "install", "%s installed the runtime %s in %s/libs%s" % (
                who, up["name"], pm, " (replaced the older copy)" if old.isdigit() else ""), who)
            if not job.get("quiet"):
                alert(dev, "Installed the runtime %s in PortMaster's libs." % up["name"], kind="install")
            return
        if not info.get("PY"):
            raise JobError("no python3 on the device: harbourmaster cannot run")
        if up.get("url"):          # harbourmaster downloads it on the device itself (and checks a .md5 URL)
            target_z = up["url"]
            job_log(job, "harbourmaster downloads %s on the device" % up["url"])
            job["step"] = "installing (harbourmaster downloads it)"
        elif up.get("serve"):      # install on all: harbourmaster fetches it from DeviceHub's file server (md5 checked)
            need = (up["size"] + up["info"].get("uncompressed", 0)) // 1024
            free = int(info.get("FREE") or 0)
            if free and free < need:
                raise JobError("not enough space: about %d MB needed, %d MB free" % (need // 1024, free // 1024))
            target_z = serve_url(up["serve"], d)
            job_log(job, "harbourmaster downloads %s from DeviceHub (%s)" % (up["name"], target_z.split("/dl/")[0]))
            job["step"] = "installing (harbourmaster downloads it from DeviceHub)"
        else:
            need = (up["size"] + up["info"].get("uncompressed", 0)) // 1024
            free = int(info.get("FREE") or 0)
            if free and free < need:
                raise JobError("not enough space: about %d MB needed, %d MB free" % (need // 1024, free // 1024))
            staged = target_z = pm + "/.devhub-staging/" + up["name"]
            job["step"] = "sending"
            job["total"] = up["size"]
            push_file(d, up["path"], staged, up["sha1"], job)
            job_log(job, "copied %s (%.1f MB, SHA-1 checked)" % (up["name"], up["size"] / 1048576))
            job["step"] = "installing (harbourmaster)"
        p = subprocess.Popen(ssh_args(d, "sh -s"), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        p.stdin.write(remote_sh(PM_FIND + PM_INSTALL, PMDIR=pm, Z=target_z).encode())
        p.stdin.close()
        hm_rc, extracted = None, 0
        for raw in p.stdout:
            for line in raw.decode("utf-8", "replace").replace("\r", "\n").split("\n"):
                if line.startswith("DEVHUB-RC="):
                    hm_rc = int(line[10:] or 1)
                    continue
                m = LOGURU.match(line)
                if m:   # harbourmaster's own log lines: keep the message, count the per-file DEBUG noise
                    level, msg = m.group(1), m.group(2)
                    if level == "DEBUG":
                        extracted += msg.startswith("Extracting ")
                        job["extracted"] = extracted
                        continue
                    line = msg if level == "INFO" else "%s: %s" % (level, msg)
                    if msg.startswith("MATCH REQS: False"):
                        job["warning"] = ("harbourmaster says this port does not match the device's requirements "
                                          "(arch, RAM, firmware...); it was installed anyway, as PortMaster does after its warning")
                        line = "WARNING: " + job["warning"]
                if line.strip():
                    job_log(job, line)
        p.wait()
        if extracted:
            job_log(job, "%d files extracted" % extracted)
        staged = None   # the install script removed the staging folder
        if hm_rc != 0:
            tail = [l for l in job["log"][-8:] if not l.lstrip().startswith("- ")]
            raise JobError("harbourmaster install failed (exit %s)%s" % (hm_rc, ": " + tail[-1] if tail else ""))
        title = up["info"].get("title") or up["name"]
        if refresh == "reload" and d.get("fe_reload"):
            job["step"] = "reloading the game list"
            rc, out, err = ssh_run(d, d["fe_reload"], timeout=60)
            job_log(job, "game list reloaded" if rc == 0 else
                    "game list reload failed (exit %d): restart the front end to see the port" % rc)
        elif refresh in ("restart", "reload") and d.get("fe_start"):
            job["step"] = "restarting the front end"
            ok, msg = fe_action(dev, d, "restart")
            job_log(job, msg)
        job.update(state="done", step="installed", result="%s installed on %s" % (title, d["name"]))
        event(dev, "install", "%s installed %s (%s) with harbourmaster%s" % (
            who, title, up["name"], " - requirements NOT matched" if job.get("warning") else ""), who)
        if not job.get("quiet"):      # install on all devices: one summary instead
            alert(dev, "Installed %s (%s).%s" % (title, up["name"], " Note: " + job["warning"] if job.get("warning") else ""),
                  kind="install")
    except JobError as e:
        job.update(state="failed", error=str(e))
        event(dev, "install", "install of %s failed: %s" % (up["name"], e), who)
        if not job.get("quiet"):
            alert(dev, "Install of %s failed: %s" % (up["name"], e), kind="install")
    except Exception as e:
        log("install job", traceback.format_exc())
        job.update(state="failed", error="internal error: %s" % e)
        if not job.get("quiet"):
            alert(dev, "Install of %s failed (internal error, see devhubd.log)." % up["name"], kind="install")
    finally:
        if staged:
            try:
                ssh_run(d, "sh -s", inp=remote_sh('S=""; [ -w "$D" ] || S="sudo -n"; $S rm -rf "$D"',
                                                  D=staged.rsplit("/", 1)[0]).encode(), timeout=30)
            except Exception:
                pass
        if up.get("path"):
            try:
                os.unlink(up["path"])
            except OSError:
                pass
        H.uploads.pop(up.get("id"), None)
        job["ended"] = now()
        rt(dev)["next_status"] = 0
        if took_lock:
            lk = H.locks.get(dev)
            if lk and lk["holder"].get("ui") and lk["purpose"].startswith("installing "):
                release_lock(dev, "install finished", who)


# read-only: harbourmaster's own config finds the ports/scripts folders, then every port.json is read
# (no harbourmaster command is run, so nothing on the device is rewritten)
PORTS_PY = r'''
import json, os, sys
from pathlib import Path
pm = Path(sys.argv[1])
sys.path[:0] = [str(pm / "pylibs"), str(pm / "exlibs")]
os.chdir(str(pm))
try:
    from loguru import logger
    logger.remove()
except Exception:
    pass
from harbourmaster import config as hmc
pd, sd = Path(hmc.HM_PORTS_DIR), Path(hmc.HM_SCRIPTS_DIR)
ports, owned = [], set()
for pj in sorted(set(pd.glob("*/port.json")) | set(pd.glob("*/*.port.json"))):
    try:
        with open(str(pj)) as fh:
            d = json.load(fh)
    except Exception:
        ports.append({"dir": pj.parent.name, "error": "unreadable port.json"})
        continue
    a, st = d.get("attr") or {}, d.get("status") or {}
    items = d.get("items") or []
    missing = []
    for it in items:
        it = it.rstrip("/")
        owned.add(it)
        if not ((sd if it.lower().endswith(".sh") else pd) / it).exists():
            missing.append(it)
    # the port's log files (launchers write log.txt into the port folder; some games add their own .log files)
    logs = []
    for it in [pj.parent.name] + [i.rstrip("/") for i in items if not i.lower().endswith(".sh")]:
        base = pd / it
        if not base.is_dir():
            continue
        for f in list(base.glob("log.txt")) + list(base.glob("*.log")) + list(base.glob("*/log.txt")) + list(base.glob("*/*.log")):
            try:
                s = f.stat()
            except OSError:
                continue
            if f.is_file() and str(f) not in [l[0] for l in logs]:
                logs.append([str(f), s.st_size, s.st_mtime])
    logs.sort(key=lambda l: -l[2])
    ports.append({"name": d.get("name"), "title": a.get("title"), "porter": a.get("porter"), "runtime": a.get("runtime"),
                  "arch": a.get("arch"), "source": st.get("source"), "status": st.get("status"), "items": items,
                  "missing": missing, "dir": pj.parent.name, "when": os.path.getmtime(str(pj)), "logs": logs[:6]})
unknown = sorted(p.name for p in sd.glob("*.sh") if p.name not in owned and p.name.lower() != "portmaster.sh")
libs = pm / "libs"
runtimes = sorted([p.name, p.stat().st_size] for p in libs.glob("*.squashfs")) if libs.is_dir() else []
ver = (pm / "version").read_text().strip() if (pm / "version").is_file() else ""
print("DEVHUB-JSON=" + json.dumps({"pm": str(pm), "pm_version": ver, "ports_dir": str(pd), "scripts_dir": str(sd),
                                   "ports": ports, "unknown": unknown, "runtimes": runtimes}))
'''

PORTS_SH = r"""
[ -n "$PM" ] || { echo "DEVHUB-NOPM"; exit 0; }
case "$PM" in */.local/share/PortMaster) XDG_DATA_HOME="${PM%/PortMaster}"; export XDG_DATA_HOME;; esac
python3 -c "$PY" "$PM" 2>&1
"""


# ---------------------------------------------------------------- console: what a device is printing

H.console = {}   # id -> {"seq": n, "lines": [{"seq", "t", "who", "stream", "text"}]}  (devhub run output)

# read-only: files behind the running app's (and its parents') stdout/stderr, tee targets, known logs, recent /tmp logs
CONSOLE_SOURCES = PM_FIND + r"""
src() { [ -f "$2" ] && echo "SRC=$(date -r "$2" +%s 2>/dev/null || echo 0)|$1|$2"; }
p="$APP"; n=0
while [ -n "$p" ] && [ "$p" -gt 1 ] 2>/dev/null && [ $n -lt 8 ]; do
  name=$(cat /proc/$p/comm 2>/dev/null)
  for fd in 1 2; do f=$(readlink /proc/$p/fd/$fd 2>/dev/null); case "$f" in /*) src "$name output" "$f";; esac; done
  [ $n = 0 ] && { c=$(readlink /proc/$p/cwd 2>/dev/null); [ -n "$c" ] && src "port log.txt" "$c/log.txt"; }
  p=$(cut -d' ' -f4 /proc/$p/stat 2>/dev/null); n=$((n + 1))
done
for t in $(pidof tee 2>/dev/null); do
  tr '\0' '\n' < /proc/$t/cmdline 2>/dev/null | tail -n +2 | while IFS= read -r a; do case "$a" in /*) src "launcher log (tee)" "$a";; esac; done
done
for f in /userdata/system/logs/es_launch_stdout.log /userdata/system/logs/es_launch_stderr.log; do src "front end launch log" "$f"; done
[ -n "$PM" ] && src "PortMaster log" "$PM/log.txt"
printf '%s' "$LOGS" | while IFS='	' read -r label glob; do   # the CFW preset's own logs (data/presets.json)
  [ -n "$glob" ] && eval "ls -t $glob" 2>/dev/null | head -6 | while IFS= read -r f; do src "$label" "$f"; done
done
find /tmp -maxdepth 2 \( -name '*.log' -o -name 'log.txt' \) -mmin -180 2>/dev/null | head -12 | while IFS= read -r f; do src "recent /tmp log" "$f"; done
true
"""

CONSOLE_TAIL = r"""
S=""; [ -r "$P" ] || S="sudo -n"
[ -f "$P" ] || { echo "DEVHUB-NOFILE"; exit 0; }
SZ=$($S wc -c < "$P"); O=$OFF
if [ "$O" -lt 0 ]; then O=$((SZ - 16384)); [ $O -lt 0 ] && O=0; fi
R=0; if [ "$O" -gt "$SZ" ]; then O=0; R=1; fi
echo "DEVHUB-HDR $SZ $O $R"
$S tail -c +$((O + 1)) "$P" 2>/dev/null | head -c 65536
"""


def load_local_presets():
    """data/presets.json: your own CFW presets, {"Name": {preset fields, "logs": [["label", "glob"], ...]}}; listed
    before "Other". "logs" adds console sources (newest 6 files per glob, $HOME allowed)."""
    try:
        extra = load_json("presets.json", {})
        other = CFW_PRESETS.pop("Other", {})
        for name, p in extra.items():
            if isinstance(p, dict):
                CFW_PRESETS[str(name)[:40]] = p
        CFW_PRESETS["Other"] = other
        if extra:
            log("local presets:", ", ".join(extra))
    except Exception as e:
        log("data/presets.json not loaded:", e)


def console_sources(dev, d):
    st = rt(dev)["status"] or {}
    app = (st.get("app") or {}).get("pid") or ""
    logs = "".join("%s\t%s\n" % (str(l).replace("\t", " "), g) for l, g in
                   (CFW_PRESETS.get(d.get("cfw") or "", {}).get("logs") or []))
    rc, out, err = ssh_run(d, "sh -s", inp=remote_sh(CONSOLE_SOURCES, APP=app, PMDIR=d.get("pm_dir") or "",
                                                    LOGS=logs).encode(), timeout=30)
    found = {}
    for line in out.decode("utf-8", "replace").splitlines():
        if line.startswith("SRC="):
            mt, label, path = (line[4:].split("|", 2) + ["", ""])[:3]
            if path and path not in found:
                found[path] = {"path": path, "label": label, "mtime": int(mt) if mt.isdigit() else 0}
    order = {"output": 0, "port log.txt": 1, "launcher log (tee)": 1}
    return sorted(found.values(), key=lambda s: (min([v for k, v in order.items() if k in s["label"]] or [2]), -s["mtime"]))


def console_tail(d, path, offset):
    rc, out, err = ssh_run(d, "sh -s", inp=remote_sh(CONSOLE_TAIL, P=path, OFF=offset).encode(), timeout=30)
    if out.startswith(b"DEVHUB-NOFILE"):
        raise ApiError(404, "%s does not exist (any more) on %s" % (path, d["name"]))
    head, _, data = out.partition(b"\n")
    f = head.decode().split()
    if len(f) != 4 or f[0] != "DEVHUB-HDR":
        raise ApiError(500, "could not read %s: %s" % (path, err.decode("utf-8", "replace").strip()[-200:] or "no answer"))
    size, start, restarted = int(f[1]), int(f[2]), f[3] == "1"
    return {"ok": True, "size": size, "offset": start + len(data), "restarted": restarted,
            "text": data.decode("utf-8", "replace")}


def console_append(dev, who, lines):
    with H.lock:
        c = H.console.setdefault(dev, {"seq": 0, "lines": []})
        for stream, text in lines[:500]:
            c["seq"] += 1
            c["lines"].append({"seq": c["seq"], "t": round(now(), 2), "who": who, "stream": str(stream)[:10],
                               "text": str(text)[:2000]})
        del c["lines"][:-3000]


def installed_ports(d):
    script = remote_sh(PM_FIND + PORTS_SH, PMDIR=d.get("pm_dir") or "", PY=PORTS_PY)
    try:
        rc, out, err = ssh_run(d, "sh -s", inp=script.encode(), timeout=90)
    except subprocess.TimeoutExpired:
        raise ApiError(504, "the device took too long to list its ports")
    text = out.decode("utf-8", "replace")
    if "DEVHUB-NOPM" in text:
        raise ApiError(404, "PortMaster not found on %s (set its folder under Edit > PortMaster folder)" % dname(d))
    for line in text.splitlines():
        if line.startswith("DEVHUB-JSON="):
            return json.loads(line[12:])
    tail = [l for l in (text + err.decode("utf-8", "replace")).splitlines() if l.strip()]
    raise ApiError(500, "could not read the port list: %s" % (tail[-1][:200] if tail else "no output (exit %d)" % rc))


# ---------------------------------------------------------------- views

def device_public(d):
    dev = d["id"]
    r = rt(dev)
    v = dict(d)
    v["socket"] = sock_path(d)
    v["has_password"] = secret_key(d) in H.secrets
    v["cards"] = [dict(c, has_password=secret_key(d, c) in H.secrets) for c in d.get("cards") or []]
    rc = rc_cache()["devices"].get(d.get("rc_slug") or "")
    v["rc"] = {k: rc.get(k) for k in ("name", "url", "cpu", "size", "aspect", "battery", "released", "os", "storage")} if rc else None
    v["paused"] = dev in H.paused
    v["rt"] = {"online": r["online"], "smb": r["smb"], "changed": r["changed"], "reach_at": r["reach_at"],
               "master": {k: x for k, x in r["master"].items() if k not in ("retry",)},
               "status": r["status"], "status_at": r["status_at"], "status_err": r["status_err"]}
    lk = H.locks.get(dev)
    v["lock"] = lock_public(dev, lk) if lk else None
    v["queue"] = [e for e in H.queues.get(dev, [])]
    v["requests"] = [x for x in H.requests.get(dev, []) if x["state"] == "pending"]
    port = "" if d["port"] == 22 else "-p %d " % d["port"]
    pre = ("wsl.exe -e ", "wsl ") if PLATFORM == "wsl" else ("", "")
    v["ssh_cmd"] = "%sssh -S %s -o BatchMode=yes %s%s" % (pre[0], sock_path(d), port, target(d))
    v["open_cmd"] = "%sssh -M -S %s -N -o ServerAliveInterval=30 %s%s" % (pre[1], sock_path(d), port, target(d))
    v["job"] = device_job(dev)
    age = chain_age(dev) if d.get("approval", "off") != "off" else None
    v["chain_until"] = H.chain[dev] + H.settings["approval_chain_min"] * 60 if age is not None else None
    return v


def sessions_public():
    """Only live sessions: holding a device, waiting for one (queue or approval), or working in the last hour.
    Ended and idle sessions are left out (they are kept in state for a week, so a returning session keeps its name)."""
    out = []
    waiting = {e.get("session") for q in H.queues.values() for e in q}
    waiting |= {x.get("session") for rs in H.requests.values() for x in rs if x["state"] == "pending"}
    for sid, s in H.sessions.items():
        locks = [dev for dev, lk in H.locks.items() if lk["holder"].get("session") == sid]
        last = max(s.get("last", 0), transcript_mtime(s))
        if not locks and sid not in waiting and (s.get("ended") or now() - last > 3600):
            continue
        out.append({"id": sid, "name": s["name"], "first": s.get("first"), "last": last, "ended": s.get("ended"),
                    "locks": locks})
    out.sort(key=lambda x: -x["last"])
    return out


# ---------------------------------------------------------------- HTTP

ROUTES = []
STREAMED = object()   # a raw route that wrote its own reply (a file download) returns this


def route(method, pattern, auth=True, raw=False):
    """raw=True: the handler gets the request object and reads the (large) body itself."""
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn, auth, raw))
        return fn
    return deco


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "DeviceHub/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        pass

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PUT(self):
        self.dispatch("PUT")

    def do_DELETE(self):
        self.dispatch("DELETE")

    def reply(self, code, ctype, data, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def dispatch(self, method):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            u = urllib.parse.urlsplit(self.path)
            q = urllib.parse.parse_qs(u.query)
            hit = None
            for m, rx, fn, auth, raw_body in ROUTES:
                mt = rx.match(u.path) if m == method else None
                if mt:
                    hit = (fn, auth, raw_body, mt)
                    break
            if not (hit and hit[2]):
                raw = self.rfile.read(n) if 0 < n <= 2_000_000 else b""
            if n > 2_000_000 and not (hit and hit[2]):
                self.close_connection = True
                raise ApiError(413, "request too big")
            host = (self.headers.get("Host") or "").lower()
            host = host[1:host.index("]")] if host.startswith("[") and "]" in host else host.split(":")[0]
            if hit and hit[2]:
                self.close_connection = True   # body may be left unread if we refuse below
            REQ.ui = True
            REQ.agent = None
            if GATE:
                res = GATE(self, method, u.path, q, bool(hit and hit[1]))
                if res:
                    self.reply(*res)
                    return
            elif host not in ("127.0.0.1", "localhost", "::1"):
                raise ApiError(403, "bad Host header")
            if hit:
                fn, auth, raw_body, mt = hit
                if auth and not GATE:
                    tok = self.headers.get("X-DevHub-Token") or (q.get("t") or [""])[0]
                    if not token_ok(tok):
                        raise ApiError(401, "missing or wrong token")
                if raw_body:
                    res = fn(self, q, *mt.groups())
                else:
                    try:
                        body = json.loads(raw.decode("utf-8")) if raw.strip() else {}
                    except ValueError:
                        raise ApiError(400, "body is not JSON")
                    if not isinstance(body, dict):
                        raise ApiError(400, "body must be a JSON object")
                    res = fn(body, q, *mt.groups())
                if res is STREAMED:
                    return
                if isinstance(res, tuple):
                    self.reply(200, res[0], res[1], res[2] if len(res) > 2 else None)
                else:
                    self.reply(200, "application/json", json.dumps(res).encode())
                return
            raise ApiError(404, "no such endpoint: %s %s" % (method, u.path))
        except ApiError as e:
            self.reply(e.code, "application/json", json.dumps({"ok": False, "error": str(e)}).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            log("api error", method, self.path, traceback.format_exc())
            try:
                self.reply(500, "application/json", json.dumps({"ok": False, "error": "internal error (see data/devhubd.log)"}).encode())
            except OSError:
                pass


def with_notices(c, res, channel=None):
    if not c.get("ui"):
        n = take_notices(c, channel)
        if n:
            res["notices"] = n
    return res


STARTED = time.time()
_build = {"key": None, "hash": ""}


def ui_build():
    """Changes when the page file changes or the server restarts: open pages compare it and reload themselves."""
    p = os.path.join(HERE, "ui", "index.html")
    try:
        st = os.stat(p)
        key = (st.st_mtime_ns, st.st_size)
        if key != _build["key"]:
            with open(p, "rb") as f:
                _build["hash"] = hashlib.sha1(f.read()).hexdigest()[:10]
            _build["key"] = key
    except OSError:
        pass
    return "%s-%d" % (_build["hash"], STARTED)


@route("GET", "/", auth=False)
def r_index(body, q):
    with open(os.path.join(HERE, "ui", "index.html"), encoding="utf-8") as f:
        html = f.read().replace("__DEVHUB_TOKEN__", TOKEN).replace("__DEVHUB_BUILD__", ui_build())
    return ("text/html; charset=utf-8", html.encode("utf-8"),
            {"X-Frame-Options": "DENY", "Content-Security-Policy":
             "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"})


@route("GET", "/sw.js", auth=False)
def r_sw(body, q):
    """The page's service worker (notifications with Approve/Deny buttons). Static, no secrets in it."""
    with open(os.path.join(HERE, "ui", "sw.js"), "rb") as f:
        return ("text/javascript; charset=utf-8", f.read(), {"Cache-Control": "no-cache"})


@route("POST", "/api/client-log")
def r_client_log(body, q):
    """What the page and its service worker report about browser notifications (for finding out why a
    notification button did nothing). Goes to devhubd.log only."""
    msg = re.sub(r"[\r\n]+", " ", str(body.get("msg") or ""))[:300]
    if msg:
        log("notification:", msg)
    return {"ok": True}


@route("GET", "/api/ping", auth=False)
def r_ping(body, q):
    return {"ok": True, "app": "DeviceHub", "version": VERSION, "pid": os.getpid()}


# ---------------------------------------------------------------- Retro Catalog (retrocatalog.com) specs
# Polite use of a public site: the handheld index (one page, refreshed at most weekly) and one handheld page when the
# user picks that model, both cached in data/retrocatalog.json. Credit and a link are shown in the UI.

RC_BASE = "https://retrocatalog.com"
RC_UA = "DeviceHub/%s (personal handheld manager; fetches single pages on user request)" % VERSION
H.rc = None


def rc_cache():
    if H.rc is None:
        H.rc = load_json("retrocatalog.json", {"index": {"at": 0, "items": []}, "devices": {}})
    return H.rc


def rc_get(path):
    import urllib.request
    req = urllib.request.Request(RC_BASE + path, headers={"User-Agent": RC_UA, "Accept": "text/html"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        raise ApiError(502, "Retro Catalog did not answer (%s)" % e)


def rc_index(refresh=False):
    c = rc_cache()
    if refresh or not c["index"]["items"] or now() - c["index"]["at"] > 7 * 86400:
        h = rc_get("/retro-handhelds")
        items = {}
        for slug, name in re.findall(r'\\"id\\":\\"([a-z0-9-]+)\\",\\"fullName\\":\\"([^"\\]+)\\"', h):
            items[slug] = name
        if not items:
            if c["index"]["items"]:
                return c["index"]
            raise ApiError(502, "could not read Retro Catalog's handheld list (the page changed?)")
        c["index"] = {"at": now(), "items": sorted(([s, n] for s, n in items.items()), key=lambda x: x[1].lower())}
        save_json("retrocatalog.json", c)
    return c["index"]


RC_FIELDS = {"Chipset": "soc", "GPU": "gpu", "CPU": "cpu", "CPU Cores": "cores", "RAM": "ram", "Storage": "storage",
             "Resolution": "resolution", "Screen Size": "size", "Aspect Ratio": "aspect", "Refresh Rate": "refresh",
             "Screen Type": "screen_type", "Battery": "battery", "Architecture": "arch", "Released": "released",
             "OS": "os", "WiFi Version": "wifi", "Weight": "weight"}


def rc_device(slug, refresh=False):
    if not re.match(r"^[a-z0-9-]{1,80}$", slug):
        raise ApiError(400, "bad Retro Catalog id")
    c = rc_cache()
    hit = c["devices"].get(slug)
    if hit and not refresh and now() - hit.get("fetched", 0) < 30 * 86400:
        return hit
    h = rc_get("/retro-handhelds/" + slug)
    spec = {"slug": slug, "url": "%s/retro-handhelds/%s" % (RC_BASE, slug), "fetched": now()}
    t = re.search(r"<title>([^<]*?)(?: Handheld Full Specifications)?(?: - Retro Catalog)?</title>", h)
    spec["name"] = html_text(t.group(1)) if t else slug
    for label, key in RC_FIELDS.items():
        m = re.search(r">%s</dt>(?:\s*<style[^>]*>.*?</style>)*\s*<dd[^>]*>(.*?)</dd>" % re.escape(label), h, re.S)
        if m:
            v = html_text(re.sub(r"<style[^>]*>.*?</style>", "", m.group(1), flags=re.S))
            if v:
                spec[key] = v
    if len(spec) <= 4:
        raise ApiError(502, "no specs found on Retro Catalog's page for %s (the page changed?)" % slug)
    ram = re.match(r"([\d.]+)\s*(GB|MB)", spec.get("ram", ""), re.I)
    spec["ram_mb"] = int(float(ram.group(1)) * (1024 if ram.group(2).upper() == "GB" else 1)) if ram else 0
    res = re.match(r"(\d+)\s*x\s*(\d+)", spec.get("resolution", ""))
    spec["screen"] = "%sx%s" % res.groups() if res else ""
    c["devices"][slug] = spec
    save_json("retrocatalog.json", c)
    return spec


def html_text(s):
    import html as _html
    return re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


@route("GET", "/api/retrocatalog/index")
def r_rc_index(body, q):
    if not H.settings.get("retrocatalog"):
        raise ApiError(403, "Retro Catalog lookups are off (Settings)")
    ix = rc_index(bool(q.get("refresh")))
    return {"ok": True, "at": ix["at"], "items": ix["items"], "source": RC_BASE}


@route("GET", "/api/retrocatalog/device")
def r_rc_device(body, q):
    if not H.settings.get("retrocatalog"):
        raise ApiError(403, "Retro Catalog lookups are off (Settings)")
    return {"ok": True, "spec": rc_device((q.get("slug") or [""])[0], bool(q.get("refresh")))}


@route("GET", "/api/state")
def r_state(body, q):
    if q.get("notify"):          # a DeviceHub page that shows browser notifications is open
        H.ui_notify_seen = now()
    with H.lock:
        return {"ok": True, "version": VERSION, "platform": PLATFORM, "build": ui_build(), "now": now(),
                "settings": H.settings, "presets": CFW_PRESETS,
                "devices": [device_public(d) for d in H.devices.values()], "sessions": sessions_public(),
                "alerts": list(H.alerts)}


@route("POST", "/api/whoami")
def r_whoami(body, q):
    c = caller(body)
    with H.lock:
        held = [dev for dev, lk in H.locks.items() if same(lk["holder"], c)]
    return with_notices(c, {"ok": True, "caller": c, "holds": held})


@route("GET", "/api/events")
def r_events(body, q):
    dev = (q.get("dev") or [""])[0]
    n = int((q.get("n") or ["200"])[0])
    since = float((q.get("since") or ["0"])[0])
    with H.lock:
        ev = [e for e in H.events if (not dev or e["dev"] == dev) and e["t"] > since]
    return {"ok": True, "events": ev[-n:]}


@route("GET", r"/shots/([A-Za-z0-9_.-]+\.png)")
def r_shot(body, q, name):
    p = os.path.join(DATA, "shots", name)
    if not os.path.isfile(p):
        raise ApiError(404, "no such screenshot")
    with open(p, "rb") as f:
        return ("image/png", f.read())


@route("POST", "/api/devices")
def r_dev_create(body, q):
    d = clean_device(body.get("device") or body)
    with H.lock:
        if d["id"] in H.devices:
            raise ApiError(409, "a device with id %r exists" % d["id"])
        H.devices[d["id"]] = d
        save_devices()
        rt(d["id"])["poke"] = True
    event(d["id"], "config", "device added: %s" % dname(d))
    return {"ok": True, "device": d}


@route("PUT", r"/api/devices/([a-z0-9_-]+)")
def r_dev_update(body, q, dev):
    old = need_device(dev)
    d = clean_device(body.get("device") or body, old)
    relogin = any(d.get(k) != old.get(k) for k in ("card", "user", "auth", "port", "identity", "host"))
    if relogin and rt(dev)["master"]["state"] == "up":
        disconnect(dev, old)           # the new login (or card) takes effect on the next connect
    with H.lock:
        gone = {secret_key(old, c) for c in old.get("cards") or []} - {secret_key(d, c) for c in d["cards"]}
        for key in gone - {secret_key(d)}:
            H.secrets.pop(key, None)
            H.pwcache.pop(key, None)
        if gone:
            save_json("secrets.json", H.secrets)
        H.devices[dev] = d
        save_devices()
        m = rt(dev)["master"]
        m.pop("block", None)
        m["next_try"] = 0
        rt(dev)["poke"] = True
        rt(dev)["next_status"] = 0
    changed = sorted(k for k in d if d[k] != old.get(k))
    event(dev, "config", "settings changed: %s" % (", ".join(changed) or "nothing"))
    return {"ok": True, "device": d}


@route("DELETE", r"/api/devices/([a-z0-9_-]+)")
def r_dev_delete(body, q, dev):
    d = need_device(dev)
    if H.locks.get(dev):
        raise ApiError(409, "%s is locked: release it first" % dname(d))
    with H.lock:
        del H.devices[dev]
        for key in [dev] + [secret_key(d, c) for c in d.get("cards") or []]:
            H.secrets.pop(key, None)
            H.pwcache.pop(key, None)
        save_json("secrets.json", H.secrets)
        save_devices()
        H.rt.pop(dev, None)
    event(dev, "config", "device removed: %s" % dname(d))
    return {"ok": True}


def card_arg(d, body):
    """The card profile a password call is for (default: the active one)."""
    cid = str(body.get("card") or "")
    if not cid or not d.get("cards"):
        return None
    c = next((c for c in d["cards"] if c["id"] == cid), None)
    if not c:
        raise ApiError(404, "no card profile %r" % cid)
    return c


@route("POST", r"/api/devices/([a-z0-9_-]+)/card")
def r_card(body, q, dev):
    d = need_device(dev)
    c = caller(body)
    if not c.get("ui"):
        raise ApiError(403, "only the user switches card profiles (DeviceHub UI)")
    card = switch_card(dev, str(body.get("card") or ""), "picked by %s" % c["name"], c["name"])
    return {"ok": True, "msg": "%s: %s (%s) - reconnecting" % (d["name"], card_name(card), card["cfw"])}


@route("POST", r"/api/devices/([a-z0-9_-]+)/password")
def r_password(body, q, dev):
    d = need_device(dev)
    pw = body.get("password")
    if not isinstance(pw, str) or not pw or len(pw) > 256 or "\n" in pw:
        raise ApiError(400, "password: 1-256 characters, one line")
    card = card_arg(d, body)
    try:
        set_password(secret_key(d, card), pw)
    except RuntimeError as e:
        raise ApiError(500, str(e))
    with H.lock:
        m = rt(dev)["master"]
        m.pop("block", None)
        m["next_try"] = 0
        m["retry"] = True
        rt(dev)["poke"] = True
    event(dev, "config", "password saved (encrypted with %s)" % ("Windows DPAPI" if PLATFORM == "wsl" else "openssl"))
    return {"ok": True}


@route("DELETE", r"/api/devices/([a-z0-9_-]+)/password")
def r_password_clear(body, q, dev):
    d = need_device(dev)
    key = secret_key(d, card_arg(d, body))
    with H.lock:
        H.secrets.pop(key, None)
        H.pwcache.pop(key, None)
        save_json("secrets.json", H.secrets)
    event(dev, "config", "password removed")
    return {"ok": True}


@route("POST", r"/api/devices/([a-z0-9_-]+)/(connect|disconnect|forget-hostkey|refresh)")
def r_conn(body, q, dev, act):
    d = need_device(dev)
    r = rt(dev)
    who = caller(body)["name"]
    if act == "connect":
        with H.lock:
            H.paused.discard(dev)
            r["master"]["retry"] = True
            r["master"].pop("block", None)
            r["master"]["next_try"] = 0
            r["poke"] = True
            save_state()
        if d["auth"] == "manual":
            return {"ok": True, "msg": "Manual login: open it yourself with: " + device_public(d)["open_cmd"]}
        return {"ok": True, "msg": "connecting to %s" % dname(d)}
    if act == "disconnect":
        with H.lock:
            H.paused.add(dev)
            save_state()
        disconnect(dev, d)
        set_master(dev, "paused", "disconnected by %s" % who)
        event(dev, "ssh", "shared connection closed by %s (auto-connect paused)" % who, who)
        return {"ok": True, "msg": "disconnected; auto-connect paused until Connect"}
    if act == "forget-hostkey":
        hosts = [d["host"]] if d["port"] == 22 else ["[%s]:%d" % (d["host"], d["port"])]
        kh = known_hosts_file(d)
        for hst in hosts:
            subprocess.run(["ssh-keygen", "-R", hst] + (["-f", kh] if kh else []), capture_output=True, timeout=10)
        with H.lock:
            r["master"].pop("block", None)
            r["master"]["retry"] = True
            r["poke"] = True
        event(dev, "ssh", "old host key forgotten by %s" % who, who)
        return {"ok": True, "msg": "host key forgotten; reconnecting"}
    r["next_status"] = 0
    r["poke"] = True
    return {"ok": True, "msg": "refreshing"}


def need_lock_for(dev, d, c, what):
    if c.get("ui"):
        return
    lk = H.locks.get(dev)
    if not lk or not same(lk["holder"], c):
        raise ApiError(409, "%s: take the lock first (devhub acquire %s \"%s\")%s" % (
            dname(d), dev, what, "; " + busy_text(d, lk) if lk else ""))
    lk["last_activity"] = now()


@route("POST", r"/api/devices/([a-z0-9_-]+)/service")
def r_service(body, q, dev):
    """Out of service / back in service (the user's button). Out of service: connection, status and battery watch go
    on, but no session gets it (acquires refused, waiters moved or told, pending requests dropped). A lock someone
    already holds stays; release it from the card if needed. Back in service: offered to waiting sessions."""
    d = need_device(dev)
    c = caller(body)
    if not c.get("ui"):
        raise ApiError(403, "only the user can take a device out of service")
    out = bool(body.get("out"))
    note = str(body.get("note") or "").strip()[:200]
    with H.lock:
        d.update(oos=out, oos_note=note if out else "", oos_since=int(now()) if out else 0)
        if out:
            for e in H.queues.pop(dev, []):
                if not e.get("offer_from"):
                    add_notice(e, "[DeviceHub] %s was taken out of service by the user%s: stop waiting for it and use "
                                  "another device (devhub status)." % (dname(d), ": " + note if note else ""))
            H.requests.pop(dev, None)
        save_devices()
        save_state()
    event(dev, "service", ("taken out of service" + (": " + note if note else "")) if out else "back in service", c["name"])
    if out and H.locks.get(dev) and not H.locks[dev]["holder"].get("ui"):
        add_notice(H.locks[dev]["holder"], "[DeviceHub] The user took %s out of service%s. Finish up and release it: "
                                            "devhub release %s" % (dname(d), ": " + note if note else "", dev))
    return {"ok": True, "msg": "%s is %s" % (dname(d), "out of service" if out else "back in service")}


@route("POST", r"/api/devices/([a-z0-9_-]+)/shutdown")
def r_shutdown(body, q, dev):
    """The user's Shut down button: the same clean shutdown as the low-battery one (front end stopped, sync,
    poweroff); a lock on the device is released and its holder told."""
    d = need_device(dev)
    c = caller(body)
    if not c.get("ui"):
        raise ApiError(403, "only the user can shut a device down (DeviceHub UI)")
    if rt(dev)["master"]["state"] != "up" and d["auth"] != "key":
        raise ApiError(503, "the shared SSH connection to %s is %s" % (dname(d), rt(dev)["master"]["state"]))
    if rt(dev)["online"] is False:
        raise ApiError(409, "%s is offline already" % dname(d))
    shutdown_device(dev, d, "shut down by %s" % c["name"], notify=False)
    return {"ok": True, "msg": "%s is shutting down" % dname(d)}


@route("POST", r"/api/devices/([a-z0-9_-]+)/frontend")
def r_frontend(body, q, dev):
    d = need_device(dev)
    c = caller(body)
    action = body.get("action")
    if action == "status":
        st = fresh_status(dev, 5)
        if st is None:
            raise ApiError(503, "no status: shared connection is %s" % rt(dev)["master"]["state"])
        run = bool(st.get("fe")) if d["fe_proc"] else None
        return with_notices(c, {"ok": True, "running": run, "pids": st.get("fe"),
                                "msg": "%s %s" % (d["fe_proc"] or "front end", {True: "running", False: "not running", None: "unknown (no process name set)"}[run])})
    if action not in ("start", "stop", "restart"):
        raise ApiError(400, "action must be start, stop, restart or status")
    need_lock_for(dev, d, c, "front end " + action)
    if rt(dev)["master"]["state"] != "up" and d["auth"] != "key":
        raise ApiError(503, "shared connection to %s is %s" % (dname(d), rt(dev)["master"]["state"]))
    ok, msg = fe_action(dev, d, action)
    rt(dev)["status_at"] = 0          # the cached picture is stale now
    rt(dev)["next_status"] = 0
    rt(dev)["poke"] = True
    event(dev, "frontend", "%s by %s: %s" % (action, c["name"], msg), c["name"])
    return with_notices(c, {"ok": ok, "msg": msg})


@route("POST", r"/api/devices/([a-z0-9_-]+)/screenshot")
def r_screenshot(body, q, dev):
    d = need_device(dev)
    c = caller(body)
    name, w, h = screenshot(dev, d)
    event(dev, "shot", "screenshot %dx%d by %s" % (w, h, c["name"]), c["name"])
    with H.lock:
        m = shot_meta()
        m[name] = {"who": c["name"], "w": w, "h": h}
        for x in [x for x in m if not os.path.isfile(os.path.join(DATA, "shots", x))]:
            del m[x]          # the oldest ones screenshot() pruned
        save_json("shots/meta.json", m)
    return with_notices(c, {"ok": True, "file": name, "path": os.path.join(DATA, "shots", name), "w": w, "h": h})


SHOT_RE = re.compile(r"^([a-z0-9_-]+)-(\d{8}-\d{6})\.png$")
H.shotmeta = None


def shot_meta():
    """Who took each screenshot (shots/meta.json). Shots from before this was kept are matched to the
    'screenshot WxH by NAME' events in the log once."""
    if H.shotmeta is not None:
        return H.shotmeta
    m = load_json("shots/meta.json", {})
    missing = {}
    for n in os.listdir(os.path.join(DATA, "shots")):
        mm = SHOT_RE.match(n)
        if mm and n not in m:
            missing[n] = (mm.group(1), time.mktime(time.strptime(mm.group(2), "%Y%m%d-%H%M%S")))
    if missing:
        evs = []
        try:
            with open(jpath("events.jsonl"), encoding="utf-8") as f:
                for line in f:
                    if '"shot"' in line:
                        e = json.loads(line)
                        x = re.match(r"screenshot (\d+)x(\d+) by (.+)$", e.get("msg") or "")
                        if e.get("kind") == "shot" and x:
                            evs.append((e["t"], e.get("dev"), x.group(3), int(x.group(1)), int(x.group(2))))
        except (OSError, ValueError):
            pass
        for n, (dev, t) in missing.items():
            near = [e for e in evs if e[1] == dev and -2 <= e[0] - t <= 120]
            e = min(near, key=lambda e: e[0] - t) if near else None
            m[n] = {"who": e[2], "w": e[3], "h": e[4]} if e else {}
        save_json("shots/meta.json", m)
    H.shotmeta = m
    return m


@route("GET", r"/api/devices/([a-z0-9_-]+)/shots")
def r_shots(body, q, dev):
    need_device(dev)
    d = os.path.join(DATA, "shots")
    with H.lock:
        m = shot_meta()
        out = []
        for n in os.listdir(d):
            mm = SHOT_RE.match(n)
            if not mm or mm.group(1) != dev:
                continue
            st = os.stat(os.path.join(d, n))
            out.append(dict({"file": n, "t": time.mktime(time.strptime(mm.group(2), "%Y%m%d-%H%M%S")), "size": st.st_size},
                            **m.get(n, {})))
    out.sort(key=lambda x: -x["t"])
    return {"ok": True, "shots": out, "keep": 30}


@route("DELETE", r"/api/devices/([a-z0-9_-]+)/shots/([A-Za-z0-9_.-]+\.png)")
def r_shot_delete(body, q, dev, name):
    """The user deletes one screenshot, or every one of the device's with name 'all.png'."""
    need_device(dev)
    if not caller(body).get("ui"):
        raise ApiError(403, "only the user can delete screenshots")
    d = os.path.join(DATA, "shots")
    names = [n for n in os.listdir(d) if (SHOT_RE.match(n) or [None, ""])[1] == dev] if name == "all.png" else [name]
    gone = 0
    with H.lock:
        m = shot_meta()
        for n in names:
            mm = SHOT_RE.match(n)
            if not mm or mm.group(1) != dev:
                raise ApiError(404, "no such screenshot of %s" % dev)
            try:
                os.unlink(os.path.join(d, n))
                gone += 1
            except FileNotFoundError:
                pass
            m.pop(n, None)
        save_json("shots/meta.json", m)
    return {"ok": True, "msg": "deleted %d screenshot%s" % (gone, "" if gone == 1 else "s")}


# ---------------------------------------------------------------- agent tokens
# One token per agent (or per machine), made and revoked by the user in the web page, so agents on other machines
# don't share the hub's master token (data/token). Stored hashed in data/agents.json; shown once when made. A call
# with one acts under that agent's name unless it names itself, and can never act as the user.
H.agents = {}       # id -> {"name", "hash", "created", "used"}
AGENT_TOKEN_RE = re.compile(r"^dh_([0-9a-f]{8})_([0-9a-f]{40})$")


def token_ok(tok):
    """The master token, or an agent's token (then REQ.agent is that agent's name)."""
    if tok and secrets.compare_digest(tok.encode(), TOKEN.encode()):
        return True
    m = AGENT_TOKEN_RE.match(tok or "")
    a = H.agents.get(m.group(1)) if m else None
    if not a or not secrets.compare_digest(hashlib.sha256(m.group(2).encode()).hexdigest(), a["hash"]):
        return False
    REQ.agent = a["name"]
    REQ.ui = False
    if now() - a.get("used", 0) > 60:
        a["used"] = now()
        save_json("agents.json", H.agents)
    return True


def agents_public():
    return [{"id": k, "name": a["name"], "created": a["created"], "used": a.get("used")} for k, a in H.agents.items()]


@route("GET", "/devhub.py", auth=False)
def r_cli(body, q):
    """The devhub command itself (one stdlib-only file, no secrets), for agents on other machines."""
    with open(os.path.join(HERE, "devhub.py"), "rb") as f:
        return ("text/x-python; charset=utf-8", f.read(), {"Content-Disposition": 'attachment; filename="devhub"'})


@route("GET", "/agent-guide.md", auth=False)
def r_agent_guide(body, q):
    try:
        with open(os.path.join(HERE, "docs", "AGENTS.md"), "rb") as f:
            return ("text/markdown; charset=utf-8", f.read())
    except OSError:
        raise ApiError(404, "docs/AGENTS.md is missing next to devhubd.py (an image built without docs/?)")


@route("GET", "/api/agents")
def r_agents(body, q):
    return {"ok": True, "agents": agents_public()}


@route("POST", "/api/agents")
def r_agent_new(body, q):
    c = caller(body)
    if not c.get("ui"):
        raise ApiError(403, "only the user makes agent tokens (DeviceHub page)")
    name = re.sub(r"\s+", " ", str(body.get("name") or "")).strip()[:60]
    if not name:
        raise ApiError(400, "give the agent a name (what you'll see on its locks)")
    aid, secret = secrets.token_hex(4), secrets.token_hex(20)
    with H.lock:
        H.agents[aid] = {"name": name, "hash": hashlib.sha256(secret.encode()).hexdigest(), "created": now(), "used": 0}
        save_json("agents.json", H.agents)
    event(None, "config", "agent token made for %s" % name, c["name"])
    return {"ok": True, "id": aid, "token": "dh_%s_%s" % (aid, secret), "agents": agents_public()}


@route("DELETE", r"/api/agents/([0-9a-f]{8})")
def r_agent_del(body, q, aid):
    c = caller(body)
    if not c.get("ui"):
        raise ApiError(403, "only the user revokes agent tokens (DeviceHub page)")
    with H.lock:
        a = H.agents.pop(aid, None)
        save_json("agents.json", H.agents)
    if a:
        event(None, "config", "agent token of %s revoked" % a["name"], c["name"])
    return {"ok": True, "agents": agents_public()}


# ---------------------------------------------------------------- remote agents: commands and files through the hub
# An agent on another machine can't use the shared SSH sockets, so the hub runs the command (or moves the file) over
# the device's shared connection itself: the same lock rules as /api/authorize, output mirrored to the device's
# Console. exec answers application/x-ndjson lines: {"s": "o"|"e", "d": base64} as output arrives, then {"rc": N}.

def call_identity(h):
    """Identity for raw calls whose body is data: X-DevHub-Session / X-DevHub-Name headers."""
    return {"client": "cli", "session": h.headers.get("X-DevHub-Session") or None,
            "name": urllib.parse.unquote(h.headers.get("X-DevHub-Name") or "")}


def granted(body):
    """/api/authorize's checks for a command or file the hub runs itself: (True, notices) or (False, answer)."""
    res = authorize_call(body)
    if not res.get("ok"):
        return False, res
    return True, res


def stream_start(h, ctype="application/x-ndjson"):
    h.send_response(200)
    h.send_header("Content-Type", ctype)
    h.send_header("Cache-Control", "no-store")
    h.send_header("Connection", "close")
    h.end_headers()
    h.close_connection = True


@route("POST", r"/api/devices/([a-z0-9_-]+)/exec", raw=True)
def r_exec(h, q, dev):
    n = int(h.headers.get("Content-Length") or 0)
    try:
        body = json.loads(h.rfile.read(min(n, 200_000)).decode("utf-8") or "{}") if n else {}
    except ValueError:
        raise ApiError(400, "body is not JSON")
    d = need_device(dev)
    cmd = str(body.get("cmd") or "")
    if not cmd.strip():
        raise ApiError(400, "nothing to run")
    ro = bool(body.get("ro"))
    ok, res = granted(dict(body, dev=dev, ro=ro, cmd=cmd))
    if not ok:
        return ("application/json", json.dumps(res).encode())
    c = caller(body)
    timeout = max(0, int(body.get("timeout") or 0))
    console_append(dev, c["name"], [["cmd", ("$ (read-only) " if ro else "$ ") + cmd[:2000]]])
    p = subprocess.Popen(ssh_args(d, cmd), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out = queue.Queue()

    def pump(src, tag):
        for raw in iter(src.readline, b""):
            out.put((tag, raw))
        out.put((tag, None))

    for src, tag in ((p.stdout, "o"), (p.stderr, "e")):
        threading.Thread(target=pump, args=(src, tag), daemon=True).start()
    stream_start(h)
    open_, end, rc, gone = 2, now() + timeout if timeout else None, None, False
    try:
        h.wfile.write((json.dumps({"notices": res.get("notices") or [], "warning": res.get("warning")}) + "\n").encode())
        while open_:
            try:
                tag, raw = out.get(timeout=1)
            except queue.Empty:
                if end and now() > end:
                    p.kill()
                    rc = 124
                    h.wfile.write((json.dumps({"s": "e", "d": base64.b64encode(
                        b"[DeviceHub] timed out after %d s\n" % timeout).decode()}) + "\n").encode())
                    break
                continue
            if raw is None:
                open_ -= 1
                continue
            h.wfile.write((json.dumps({"s": tag, "d": base64.b64encode(raw).decode()}) + "\n").encode())
            h.wfile.flush()
            console_append(dev, c["name"], [["out" if tag == "o" else "err", raw.decode("utf-8", "replace").rstrip("\r\n")]])
    except (BrokenPipeError, ConnectionResetError):
        gone = True
        p.kill()            # the agent went away (Ctrl-C): don't leave the command running unattended
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
    rc = rc if rc is not None else p.returncode
    console_append(dev, c["name"], [["cmd", "[exit %s]" % rc]])
    with H.lock:
        lk = H.locks.get(dev)
        if lk and same(lk["holder"], c):
            lk["last_activity"] = now()
            H.dirty = True
    if not gone:
        try:
            h.wfile.write((json.dumps({"rc": rc}) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError):
            pass
    return STREAMED


def remote_q(path):
    return "'" + path.replace("'", "'\\''") + "'"


@route("PUT", r"/api/devices/([a-z0-9_-]+)/file", raw=True)
def r_file_put(h, q, dev):
    """Copy the request body to PATH on the device (lock needed): written to PATH.devhub-part, then moved."""
    d = need_device(dev)
    path = (q.get("path") or [""])[0]
    if not path.startswith("/"):
        raise ApiError(400, "path must be absolute")
    n = int(h.headers.get("Content-Length") or -1)
    if n < 0:
        raise ApiError(411, "Content-Length needed")
    ident = call_identity(h)
    ok, res = granted(dict(ident, dev=dev, ro=False, cmd="push %s (%d bytes)" % (path, n)))
    if not ok:
        return ("application/json", json.dumps(res).encode())
    part = path + ".devhub-part"
    p = subprocess.Popen(ssh_args(d, "cat > %s && n=$(wc -c < %s) && [ \"$n\" -eq %d ] && mv -f %s %s" % (
        remote_q(part), remote_q(part), n, remote_q(part), remote_q(path))), stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    got = 0
    try:
        while got < n:
            chunk = h.rfile.read(min(1 << 20, n - got))
            if not chunk:
                break
            p.stdin.write(chunk)
            got += len(chunk)
        p.stdin.close()
    except (BrokenPipeError, ConnectionResetError):
        pass
    err = p.stderr.read().decode("utf-8", "replace").strip()
    rc = p.wait()
    if got != n or rc != 0:
        ssh_run(d, "rm -f %s" % remote_q(part), timeout=20)
        return ("application/json", json.dumps({"ok": False, "msg": "copy to %s failed%s" % (
            path, ": " + err.splitlines()[-1][:200] if err else " (got %d of %d bytes)" % (got, n))}).encode())
    console_append(dev, caller(ident)["name"], [["cmd", "pushed %s (%d bytes)" % (path, n)]])
    return {"ok": True, "bytes": n, "notices": res.get("notices") or []}


@route("GET", r"/api/devices/([a-z0-9_-]+)/file", raw=True)
def r_file_get(h, q, dev):
    """PATH from the device (read-only, no lock needed), with its size as Content-Length."""
    d = need_device(dev)
    path = (q.get("path") or [""])[0]
    if not path.startswith("/"):
        raise ApiError(400, "path must be absolute")
    ok, res = granted(dict(call_identity(h), dev=dev, ro=True, cmd=""))
    if not ok:
        return ("application/json", json.dumps(res).encode())
    rc, out, err = ssh_run(d, "wc -c < %s" % remote_q(path), timeout=20)
    size = out.decode().strip()
    if rc != 0 or not size.isdigit():
        e = err.decode("utf-8", "replace").strip().splitlines()
        return ("application/json", json.dumps({"ok": False, "msg": "can't read %s on %s%s" % (
            path, dname(d), ": " + e[-1][:200] if e else "")}).encode())
    p = subprocess.Popen(ssh_args(d, "cat %s" % remote_q(path)), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL)
    h.send_response(200)
    h.send_header("Content-Type", "application/octet-stream")
    h.send_header("Content-Length", size)
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    try:
        for chunk in iter(lambda: p.stdout.read(1 << 20), b""):
            h.wfile.write(chunk)
    except (BrokenPipeError, ConnectionResetError):
        p.kill()
    p.wait()
    h.close_connection = True
    return STREAMED


# ---------------------------------------------------------------- serving a file to the devices (install on all)
# One upload, fetched by every device's harbourmaster over HTTP (http://<this machine>/dl/<token>/<name>?md5=...),
# instead of a copy pushed to each over SSH. The token is random and the file is served only while its installs run.
# A hub listening on the LAN (server mode) serves it on its own port; a local DeviceHub (loopback only) starts a
# small file server on FILE_PORT for the duration.
H.serves = {}       # token -> {"path", "name", "md5", "size"}
H.file_srv = None


def serve_add(path, name):
    md5 = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            md5.update(chunk)
    tok = secrets.token_hex(16)
    with H.lock:
        H.serves[tok] = {"path": path, "name": name, "md5": md5.hexdigest(), "size": os.path.getsize(path)}
    if BIND in ("127.0.0.1", "localhost", "::1"):
        file_server(True)
    return tok


def serve_drop(tok):
    with H.lock:
        e = H.serves.pop(tok, None)
        idle = not H.serves
    if e:
        try:
            os.unlink(e["path"])
        except OSError:
            pass
    if idle:
        file_server(False)


def local_ip_for(host):
    """This machine's address on the way to the device (what the device should download from)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((host, 9))
            return s.getsockname()[0]
    except OSError:
        raise JobError("can't tell which of this machine's addresses %s would reach" % host)


def serve_url(tok, d):
    e = H.serves.get(tok)
    if not e:
        raise JobError("the file to install is gone (the install-all run ended)")
    port = FILE_PORT if H.file_srv else PORT
    return "http://%s:%d/dl/%s/%s?md5=%s" % (local_ip_for(d["host"]), port, tok, urllib.parse.quote(e["name"]), e["md5"])


def send_served(h, tok, name):
    e = H.serves.get(tok)
    if not e or urllib.parse.unquote(name) != e["name"]:
        h.reply(404, "text/plain", b"not found")
        return STREAMED
    h.send_response(200)
    h.send_header("Content-Type", "application/zip")
    h.send_header("Content-Length", str(e["size"]))
    h.send_header("Cache-Control", "no-store")
    h.end_headers()
    with open(e["path"], "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.wfile.write(chunk)
    log("served", e["name"], "to", h.client_address[0])
    return STREAMED


SERVED_RE = re.compile(r"^/dl/([0-9a-f]{32})/([^/?]+)$")


@route("GET", r"/dl/([0-9a-f]{32})/([^/?]+)", auth=False, raw=True)
def r_served(h, q, tok, name):
    return send_served(h, tok, name)


class FileHandler(http.server.BaseHTTPRequestHandler):
    """The temporary file server of a local (loopback-only) DeviceHub: only /dl/<token>/<name>, nothing else."""
    server_version = "DeviceHub/" + VERSION
    reply = lambda self, code, ctype, data, extra=None: Handler.reply(self, code, ctype, data, extra)

    def log_message(self, fmt, *a):
        pass

    def do_GET(self):
        m = SERVED_RE.match(urllib.parse.urlsplit(self.path).path)
        try:
            if m:
                send_served(self, *m.groups())
            else:
                self.reply(404, "text/plain", b"not found")
        except (BrokenPipeError, ConnectionResetError):
            pass


def file_server(on):
    with H.lock:
        if on and not H.file_srv:
            try:
                H.file_srv = Server(("0.0.0.0", FILE_PORT), FileHandler)
            except OSError as e:
                log("file server: cannot listen on port %d: %s" % (FILE_PORT, e))
                return
            threading.Thread(target=H.file_srv.serve_forever, daemon=True).start()
            log("file server for installs on port %d" % FILE_PORT)
        elif not on and H.file_srv:
            srv, H.file_srv = H.file_srv, None
            threading.Thread(target=lambda: (srv.shutdown(), srv.server_close()), daemon=True).start()


UPLOAD_KEEP_S = 30 * 60      # an uploaded zip nobody installs is deleted after this


def sweep_uploads():
    """Port zips are only kept while they are needed: an upload waiting for its Install click (30 min at most) or a
    running install. Everything else in the upload folder goes, including files a hub restart left behind
    (a file still being written is younger than 10 min, so it is never touched)."""
    if not os.path.isdir(UPLOADS):
        return
    with H.lock:
        busy = {j.get("upload") for j in H.jobs.values() if j["state"] == "running"}
        for uid, u in list(H.uploads.items()):
            if uid not in busy and now() - u["at"] > UPLOAD_KEEP_S:
                H.uploads.pop(uid, None)
        keep = {u["path"] for u in H.uploads.values()}
    for n in os.listdir(UPLOADS):
        p = os.path.join(UPLOADS, n)
        try:
            if p not in keep and now() - os.path.getmtime(p) > 600:
                os.unlink(p)
                log("upload deleted (unused): %s" % n)
        except OSError:
            pass


def install_targets():
    """Every device for 'install on all online devices', with the reason it cannot take an install now."""
    out = []
    for d in H.devices.values():
        dev, r = d["id"], rt(d["id"])
        why = ("disabled" if not d["enabled"] else "out of service" if d.get("oos") else
               "offline" if r["online"] is not True else
               "no shared connection (%s)" % r["master"]["state"] if r["master"]["state"] != "up" and d["auth"] != "key" else
               "an install is already running" if any(j["dev"] == dev and j["state"] == "running" for j in H.jobs.values())
               else "")
        lk = H.locks.get(dev)
        held = lk if lk and not lk["holder"].get("ui") else None
        out.append({"id": dev, "name": d["name"], "cfw": d["cfw"], "ok": not why, "why": why,
                    "held": {"name": held["holder"]["name"], "purpose": held["purpose"]} if held else None,
                    "fe_reload": bool(d.get("fe_reload")), "fe_start": bool(d.get("fe_start"))})
    out.sort(key=lambda t: (not t["ok"], t["name"]))
    return out


@route("POST", r"/api/devices/([a-z0-9_-]+)/upload", raw=True)
def r_upload(h, q, dev):
    """Body = the port zip itself (streamed to disk); answers with what it holds and an upload id for /install.
    dev '_all': one upload for 'install on all online devices' (/api/install-all)."""
    d = None if dev == "_all" else need_device(dev)
    n = int(h.headers.get("Content-Length") or 0)
    if n <= 0:
        raise ApiError(400, "empty upload")
    if n > 8 * 1024 ** 3:
        raise ApiError(413, "file larger than 8 GB")
    name = os.path.basename((q.get("name") or ["port.zip"])[0].replace("\\", "/")).strip()
    name = re.sub(r"[^A-Za-z0-9._ +()-]", "_", name) or "port.zip"
    if not name.lower().endswith((".zip", ".squashfs")):
        raise ApiError(400, "pick a port .zip or a runtime .squashfs file")
    os.makedirs(UPLOADS, mode=0o700, exist_ok=True)
    sweep_uploads()
    uid = secrets.token_hex(8)
    path = os.path.join(UPLOADS, uid + ".zip")
    sha, got = hashlib.sha1(), 0
    with open(path, "wb") as f:
        while got < n:
            chunk = h.rfile.read(min(1 << 20, n - got))
            if not chunk:
                break
            f.write(chunk)
            sha.update(chunk)
            got += len(chunk)
    if got != n:
        os.unlink(path)
        raise ApiError(400, "upload cut short (%d of %d bytes)" % (got, n))
    h.close_connection = False
    info = inspect_squashfs(path, name) if name.lower().endswith(".squashfs") else inspect_zip(path)
    if info.get("kind") == "portmaster":
        name = "PortMaster.zip"      # harbourmaster recognises PortMaster's own zip by this name only
    H.uploads[uid] = {"id": uid, "path": path, "name": name, "dev": dev, "size": n, "sha1": sha.hexdigest(),
                      "at": now(), "info": info}
    if info["errors"]:
        H.uploads.pop(uid)
        os.unlink(path)
    if d is None:
        return {"ok": True, "upload": None if info["errors"] else uid, "name": name, "size": n, "info": info,
                "device": "all online devices", "targets": install_targets()}
    lk = H.locks.get(dev)
    return {"ok": True, "upload": None if info["errors"] else uid, "name": name, "size": n, "info": info,
            "device": dname(d), "lock": lock_public(dev, lk) if lk else None, "fe_reload": bool(d.get("fe_reload")),
            "fe_start": bool(d.get("fe_start"))}


def install_all_summary(jids, up, serve=None):
    """One notification when every install of an 'install on all devices' run has finished (and the served file
    goes away then)."""
    jobs = [H.jobs[j] for j in jids if j in H.jobs]
    while any(j["state"] == "running" for j in jobs):
        time.sleep(2)
    if serve:
        serve_drop(serve)
    title = up["info"].get("title") or up["name"]
    ok = [H.devices[j["dev"]]["name"] for j in jobs if j["state"] == "done" and j["dev"] in H.devices]
    bad = ["%s (%s)" % (H.devices[j["dev"]]["name"] if j["dev"] in H.devices else j["dev"], j["error"])
           for j in jobs if j["state"] != "done"]
    warn = [H.devices[j["dev"]]["name"] for j in jobs if j.get("warning") and j["dev"] in H.devices]
    alert(None, "%s installed on %s.%s%s" % (
        title, ", ".join(ok) or "no device", " Failed: %s." % "; ".join(bad) if bad else "",
        " Requirements not matched on: %s." % ", ".join(warn) if warn else ""), kind="install")


@route("GET", "/api/install-targets")
def r_install_targets(body, q):
    return {"ok": True, "targets": install_targets()}


def url_install(url):
    """An install from a URL: harbourmaster on the device downloads it (a .zip, or a .zip.md5 that it checks)."""
    url = str(url or "").strip()
    if not re.match(r"^https?://[^\s'\"`$\;|&<>]{4,500}$", url):
        raise ApiError(400, "give an http(s) URL of a port zip (or its .zip.md5), without spaces or quotes")
    name = urllib.parse.unquote(url.rstrip("/").rsplit("/", 1)[-1]) or "download"
    title = re.sub(r"\.md5$", "", name)
    return {"id": None, "url": url, "name": name, "size": 0, "sha1": "",
            "info": {"kind": "url", "title": title, "errors": [], "warnings": []}}


@route("POST", "/api/install-all")
def r_install_all(body, q):
    """Install one uploaded port zip on several devices at once (the user's 'Install on all online devices').
    Each device gets its own install job on a hard link of the upload; devices that cannot take it are skipped
    with the reason. A device a session holds is skipped unless it is listed in 'force'."""
    c = caller(body)
    if not c.get("ui"):
        raise ApiError(403, "only the user installs on all devices (DeviceHub UI)")
    up = url_install(body["url"]) if body.get("url") else H.uploads.get(str(body.get("upload") or ""))
    if not up or (not up.get("url") and up["dev"] != "_all"):
        raise ApiError(404, "no such upload: upload the zip again")
    want = [str(x) for x in body.get("devices") or []]
    force = {str(x) for x in body.get("force") or []}
    refresh = body.get("refresh") or "reload"
    targets = {t["id"]: t for t in install_targets()}
    started, skipped = [], []
    serve = None
    if not up.get("url") and up["info"].get("kind") != "squashfs" and H.settings.get("install_serve"):
        serve = serve_add(up["path"], up["name"])      # the served file now owns the upload
        H.uploads.pop(up["id"], None)
    for dev in want:
        t = targets.get(dev)
        if not t:
            skipped.append({"dev": dev, "name": dev, "why": "no such device"})
            continue
        if not t["ok"]:
            skipped.append({"dev": dev, "name": t["name"], "why": t["why"]})
            continue
        if t["held"] and dev not in force:
            skipped.append({"dev": dev, "name": t["name"], "why": "held by %s" % t["held"]["name"]})
            continue
        d = H.devices[dev]
        if up.get("url"):
            uid, link, mine = None, None, dict(up, dev=dev)
        elif serve:
            uid, link, mine = None, None, dict(up, id=None, path=None, serve=serve, dev=dev)
        else:
            uid = secrets.token_hex(8)
            link = os.path.join(UPLOADS, uid + ".zip")
            try:
                os.link(up["path"], link)
            except OSError:
                shutil.copyfile(up["path"], link)
            mine = H.uploads[uid] = dict(up, id=uid, path=link, dev=dev, at=now())
        r = refresh if refresh != "reload" or d.get("fe_reload") or d.get("fe_start") else "none"
        try:
            res = start_install(dev, d, c, mine, r, dev in force)
        except ApiError as e:
            res = {"ok": False, "msg": str(e)}
        if res.get("ok"):
            H.jobs[res["job"]["id"]]["quiet"] = True
            started.append({"dev": dev, "name": t["name"], "job": res["job"]})
        else:
            if uid:
                H.uploads.pop(uid, None)
                try:
                    os.unlink(link)
                except OSError:
                    pass
            skipped.append({"dev": dev, "name": t["name"], "why": res.get("msg") or "refused"})
    if not up.get("url") and not serve:
        H.uploads.pop(up["id"], None)        # every job has its own link now
        try:
            os.unlink(up["path"])
        except OSError:
            pass
    if started:
        threading.Thread(target=install_all_summary, args=([s["job"]["id"] for s in started], up, serve), daemon=True).start()
    elif serve:
        serve_drop(serve)
    event(None, "install", "%s is installing %s on %d device%s%s" % (
        c["name"], up["name"], len(started), "" if len(started) == 1 else "s",
        " (skipped: %s)" % ", ".join("%s - %s" % (s["name"], s["why"]) for s in skipped) if skipped else ""), c["name"])
    return {"ok": bool(started), "started": started, "skipped": skipped,
            "msg": "installing on %d device%s" % (len(started), "" if len(started) == 1 else "s") if started else
            "nothing started: " + "; ".join("%s - %s" % (s["name"], s["why"]) for s in skipped)}


@route("POST", r"/api/devices/([a-z0-9_-]+)/install")
def r_install(body, q, dev):
    d = need_device(dev)
    c = caller(body)
    if body.get("url"):
        up = dict(url_install(body["url"]), dev=dev)
    else:
        up = H.uploads.get(str(body.get("upload") or ""))
        if not up or up["dev"] != dev:
            raise ApiError(404, "no such upload for %s: upload the zip again" % dev)
    refresh = body.get("refresh") or ("reload" if d.get("fe_reload") else "none")
    return with_notices(c, start_install(dev, d, c, up, refresh, bool(body.get("force"))))


def start_install(dev, d, c, up, refresh, force):
    """Start one install job (the device's own harbourmaster). The UI takes the lock for the install when the device
    is free; a session needs its own lock."""
    if any(j["dev"] == dev and j["state"] == "running" for j in H.jobs.values()):
        raise ApiError(409, "an install is already running on %s" % dname(d))
    if rt(dev)["master"]["state"] != "up" and d["auth"] != "key":
        raise ApiError(503, "the shared SSH connection to %s is %s" % (dname(d), rt(dev)["master"]["state"]))
    if refresh not in ("reload", "restart", "none"):
        raise ApiError(400, "refresh must be reload, restart or none")
    took = False
    with H.lock:
        lk = H.locks.get(dev)
        if c.get("ui"):
            if lk and not lk["holder"].get("ui") and not force:
                return {"ok": False, "status": "busy", "msg": busy_text(d, lk)}
            if not lk:
                r = do_acquire(dev, c, {"purpose": "installing " + up["name"], "ttl": "30m", "take": True})
                took = bool(r.get("ok"))
        else:
            need_lock_for(dev, d, c, "install " + up["name"])
        jid = secrets.token_hex(6)
        job = H.jobs[jid] = {"id": jid, "dev": dev, "kind": "install", "name": up["name"], "upload": up["id"],
                             "title": up["info"].get("title"), "who": c["name"], "state": "running",
                             "step": "starting", "sent": 0, "total": up["size"], "log": [], "error": "",
                             "result": "", "started": now(), "ended": None}
        for old in sorted(H.jobs.values(), key=lambda j: j["started"])[:-30]:
            if old["state"] != "running":
                H.jobs.pop(old["id"], None)
    event(dev, "install", "%s is installing %s" % (c["name"], up["name"]), c["name"])
    threading.Thread(target=install_job, args=(job, d, up, refresh, took, c["name"]), daemon=True).start()
    return {"ok": True, "job": job_public(job)}


@route("GET", r"/api/devices/([a-z0-9_-]+)/ports")
def r_ports(body, q, dev):
    d = need_device(dev)
    if rt(dev)["master"]["state"] != "up" and d["auth"] != "key":
        raise ApiError(503, "the shared SSH connection to %s is %s" % (dname(d), rt(dev)["master"]["state"]))
    res = installed_ports(d)
    res.update(ok=True, device=dname(d))
    return res


@route("GET", r"/api/devices/([a-z0-9_-]+)/console/sources")
def r_console_sources(body, q, dev):
    d = need_device(dev)
    if rt(dev)["master"]["state"] != "up" and d["auth"] != "key":
        raise ApiError(503, "the shared SSH connection to %s is %s" % (dname(d), rt(dev)["master"]["state"]))
    st = rt(dev)["status"] or {}
    return {"ok": True, "device": dname(d), "app": st.get("app"), "sources": console_sources(dev, d),
            "session_lines": H.console.get(dev, {}).get("seq", 0)}


@route("GET", r"/api/devices/([a-z0-9_-]+)/console/file")
def r_console_file(body, q, dev):
    d = need_device(dev)
    path = (q.get("path") or [""])[0]
    if not path.startswith("/") or "\n" in path:
        raise ApiError(400, "give an absolute path on the device")
    return console_tail(d, path, int((q.get("offset") or ["-1"])[0]))


@route("GET", r"/api/devices/([a-z0-9_-]+)/console/log")
def r_console_log(body, q, dev):
    need_device(dev)
    since = int((q.get("since") or ["0"])[0])
    c = H.console.get(dev, {"seq": 0, "lines": []})
    return {"ok": True, "seq": c["seq"], "lines": [l for l in c["lines"] if l["seq"] > since][-1000:]}


@route("POST", r"/api/devices/([a-z0-9_-]+)/console/append")
def r_console_append(body, q, dev):
    need_device(dev)
    c = caller(body)
    lines = body.get("lines") or []
    if not isinstance(lines, list):
        raise ApiError(400, "lines must be a list of [stream, text]")
    console_append(dev, c["name"], [l for l in lines if isinstance(l, list) and len(l) == 2])
    return {"ok": True}


@route("GET", r"/api/jobs/([0-9a-f]+)")
def r_job(body, q, jid):
    job = H.jobs.get(jid)
    if not job:
        raise ApiError(404, "no such job")
    return {"ok": True, "job": job_public(job, int((q.get("lines") or ["60"])[0]))}


@route("POST", r"/api/lock/(acquire|renew|release)")
def r_lock(body, q, act):
    c = caller(body)
    dev = str(body.get("dev") or "")
    res = {"acquire": do_acquire, "renew": do_renew, "release": do_release}[act](dev, c, body)
    return with_notices(c, res)


@route("POST", "/api/lock/approve")
def r_approve(body, q):
    c = caller(body)
    if not c.get("ui"):
        raise ApiError(403, "only the user can approve, in the DeviceHub UI")
    dev = str(body.get("dev") or "")
    d = need_device(dev)
    allow = bool(body.get("allow"))
    with H.lock:
        for x in H.requests.get(dev, []):
            if x["state"] == "pending" and same(x, {"session": body.get("session"), "name": body.get("name")}):
                x["state"] = "approved" if allow else "denied"
                x["decided"] = now()
                x["by"] = "user"
                if allow:
                    H.chain[dev] = now()
                else:
                    H.chain.pop(dev, None)   # a Deny closes the request chain: the next session asks again
                add_notice(x, "[DeviceHub] The user %s your request for %s.%s" % (
                    "approved" if allow else "declined", dname(d),
                    " Take it now: devhub acquire %s \"%s\"" % (dev, x["purpose"]) if allow else ""))
                save_state()
                via = " (from a notification)" if body.get("via") == "notification" else ""
                event(dev, "approval", "%s %s%s" % ("approved" if allow else "declined", x["name"], via), c["name"])
                return {"ok": True}
    raise ApiError(404, "no pending request from that session")


@route("POST", "/api/lock/ask")
def r_ask(body, q):
    c = caller(body)
    dev = str(body.get("dev") or "")
    d = need_device(dev)
    text = str(body.get("text") or "").strip()[:300]
    with H.lock:
        lk = H.locks.get(dev)
        if not lk:
            raise ApiError(404, "%s is not locked" % dname(d))
        if lk["state"] == "held":
            open_check(dev, lk, "user", "%s asked whether you still need it%s" % (c["name"], ": " + text if text else ""))
        elif text:
            add_notice(lk["holder"], "[DeviceHub] Message from %s about %s: %s" % (c["name"], dname(d), text))
        save_state()
    return {"ok": True, "msg": "asked %s" % lk["holder"]["name"]}


@route("POST", "/api/authorize")
def r_authorize(body, q):
    return authorize_call(body)


def authorize_call(body):
    c = caller(body)
    dev = str(body.get("dev") or "")
    d = need_device(dev)
    ro = bool(body.get("ro"))
    cmd = str(body.get("cmd") or "")[:300]
    warn = None
    with H.lock:
        lk = H.locks.get(dev)
        if lk and same(lk["holder"], c):
            lk["last_activity"] = now()
            H.dirty = True
        elif not ro:
            if lk:
                return with_notices(c, {"ok": False, "msg": busy_text(d, lk) + ". Use --ro for read-only commands."})
            return with_notices(c, {"ok": False, "msg": "%s: take the lock first (devhub acquire %s \"purpose\"), "
                                                        "or use --ro for a read-only command" % (dname(d), dev)})
        elif lk:
            warn = "read-only: %s" % busy_text(d, lk)
    m = rt(dev)["master"]
    if m["state"] != "up" and d["auth"] != "key":
        how = ("open it yourself: " + device_public(d)["open_cmd"]) if d["auth"] == "manual" else (m.get("msg") or "see the DeviceHub UI")
        return with_notices(c, {"ok": False, "msg": "%s: the shared SSH connection is %s (%s)" % (dname(d), m["state"], how)})
    if not ro and cmd:
        event(dev, "cmd", cmd, c["name"])
    res = {"ok": True, "conn": {"socket": sock_path(d), "target": target(d), "port": d["port"], "host": d["host"],
                                "smb": d["smb"], "ports_dir": d["ports_dir"], "master": m["state"],
                                "known_hosts": known_hosts_file(d)}}
    if warn:
        res["warning"] = warn
    if m["state"] != "up":
        res["warning"] = ((res.get("warning") or "") + " shared connection is %s" % m["state"]).strip()
    return with_notices(c, res)


@route("POST", "/api/activity")
def r_activity(body, q):
    c = caller(body)
    lk = H.locks.get(str(body.get("dev") or ""))
    if lk and same(lk["holder"], c):
        lk["last_activity"] = now()
        H.dirty = True
    return with_notices(c, {"ok": True})


@route("POST", "/api/session")
def r_session(body, q):
    if body.get("client") == "ui":
        sid = str(body.get("target") or "")
        if sid not in H.sessions:
            raise ApiError(404, "no such session")
        touch_session(sid, str(body.get("name") or "").strip()[:60] or None)
        return {"ok": True}
    c = caller(body)
    if not c.get("session"):
        raise ApiError(400, "no Claude session id (CLAUDE_CODE_SESSION_ID) in this shell")
    return with_notices(c, {"ok": True, "msg": "this session shows up as \"%s\"" % c["name"]})


@route("POST", "/api/hook")
def r_hook(body, q):
    ev = body.get("hook_event_name") or ""
    sid = str(body.get("session_id") or "")
    if not sid:
        return {"ok": True}
    c = caller({"session": sid, "transcript": body.get("transcript_path")})
    if ev == "SessionEnd":
        end_session(sid)
        return {"ok": True}
    if ev == "PreToolUse":
        ti = body.get("tool_input") or {}
        cmd = ti.get("command") if isinstance(ti, dict) else None
        if isinstance(cmd, str) and cmd:
            with H.lock:
                reason = guard(c, cmd)
            if reason:
                return {"ok": True, "output": {"hookSpecificOutput": {
                    "hookEventName": "PreToolUse", "permissionDecision": "deny",
                    "permissionDecisionReason": "[DeviceHub] " + reason}}}
        return {"ok": True}
    if ev == "Stop":
        if body.get("stop_hook_active"):
            return {"ok": True}
        text = stop_check(c)
        return {"ok": True, "output": {"decision": "block", "reason": text}} if text else {"ok": True}
    if ev in ("PostToolUse", "UserPromptSubmit", "SessionStart"):
        msgs = take_notices(c, ev)
        if msgs:
            return {"ok": True, "output": {"hookSpecificOutput": {"hookEventName": ev, "additionalContext": "\n\n".join(msgs)}}}
    return {"ok": True}


@route("POST", "/api/broadcast")
def r_broadcast(body, q):
    """A notice for every Claude session with an active task (delivered by their hooks or next devhub call): it holds
    a lock, waits in a queue or for an approval, or its transcript changed in the last N minutes (default 10).
    Idle sessions are left alone."""
    text = str(body.get("text") or "").strip()[:2000]
    if not text:
        raise ApiError(400, "text is empty")
    mins = float(body.get("minutes") or 10)
    sent = []
    with H.lock:
        involved = {lk["holder"].get("session") for lk in H.locks.values()}
        involved |= {e.get("session") for q in H.queues.values() for e in q}
        involved |= {x.get("session") for rs in H.requests.values() for x in rs if x["state"] == "pending"}
        for sid, s in H.sessions.items():
            if s.get("ended") or (sid not in involved and now() - max(s.get("last", 0), transcript_mtime(s)) > mins * 60):
                continue
            add_notice({"session": sid}, "[DeviceHub] " + text)
            sent.append(s["name"])
    event(None, "broadcast", "to %d sessions: %s" % (len(sent), text[:120]), caller(body)["name"])
    return {"ok": True, "sessions": sent}


@route("PUT", "/api/settings")
def r_settings(body, q):
    s = body.get("settings") or {}
    if not caller(body).get("ui"):     # settings (Away mode, the notify command) are the user's, in the UI only
        raise ApiError(403, "only the user changes settings (DeviceHub UI)")
    was_away = H.settings.get("away_mode")
    with H.lock:
        for k, v in s.items():
            if k not in DEFAULT_SETTINGS:
                continue
            dv = DEFAULT_SETTINGS[k]
            try:
                if isinstance(dv, bool):
                    v = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
                elif isinstance(dv, int):
                    v = max(0 if k in ("approval_chain_min", "battery_alert_pct", "battery_shutdown_pct") else 1, int(v))
                    if k.startswith("battery_"):
                        v = min(v, 50)
                else:
                    v = str(v).strip()
            except (TypeError, ValueError):
                raise ApiError(400, "%s must be a number" % k)
            if k == "auto_release" and v not in ("never", "when_clean", "always"):
                raise ApiError(400, "auto_release must be never, when_clean or always")
            H.settings[k] = v
        save_state()
    if H.settings.get("away_mode") != was_away:
        event(None, "config", "Away mode %s: sessions' device requests are %s" % (
            ("on", "approved at once") if H.settings["away_mode"] else ("off", "asked again")), caller(body)["name"])
    else:
        event(None, "config", "settings changed")
    return {"ok": True, "settings": H.settings}


@route("POST", "/api/alerts/dismiss")
def r_dismiss(body, q):
    with H.lock:
        ids = body.get("ids") or ([body.get("id")] if body.get("id") else [])
        H.alerts[:] = [a for a in H.alerts if a["id"] not in ids] if ids else []
    return {"ok": True}


@route("POST", "/api/import")
def r_import(body, q):
    added = []
    with H.lock:
        for src in body.get("devices") or []:
            d = clean_device(src)
            if d["id"] in H.devices and not body.get("replace"):
                continue
            H.devices[d["id"]] = d
            added.append(d["id"])
        save_devices()
        import_lock_files()
        save_state()
    for dev in added:
        event(dev, "config", "device imported: %s" % dname(H.devices[dev]))
    return {"ok": True, "added": added}


@route("POST", "/api/shutdown")
def r_shutdown(body, q):
    if getattr(REQ, "agent", None):
        raise ApiError(403, "agents can't stop DeviceHub")
    threading.Thread(target=lambda: (time.sleep(0.3), HTTPD.shutdown()), daemon=True).start()
    return {"ok": True, "msg": "DeviceHub stopping (shared connections stay open)"}


# ---------------------------------------------------------------- main

def loop():
    last_tick = 0
    while True:
        try:
            for dev in list(H.devices):
                r = rt(dev)
                if not r["busy"] and (r["poke"] or now() - r["reach_at"] >= 10):
                    r["busy"] = True
                    r["poke"] = False
                    POOL.submit(upkeep_job, dev)
            if now() - last_tick >= 5:
                last_tick = now()
                tick()
        except Exception:
            log("loop error", traceback.format_exc())
        time.sleep(1)


def upkeep_job(dev):
    try:
        upkeep(dev)
    except Exception:
        log("upkeep", dev, traceback.format_exc())
    finally:
        r = H.rt.get(dev)
        if r:
            r["busy"] = False
            r["reach_at"] = now()


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


HTTPD = None


def main():
    global TOKEN, HTTPD
    os.makedirs(os.path.join(DATA, "shots"), exist_ok=True)
    try:
        os.chmod(DATA, 0o700)      # token, secrets and logs: this user only (no effect on a Windows drive)
    except OSError:
        pass
    os.makedirs(RUN, mode=0o700, exist_ok=True)
    os.chmod(RUN, 0o700)
    with open(ASKPASS, "w") as f:
        f.write('#!/bin/sh\n# ssh asks here for the password; DeviceHub hands it over once through a FIFO.\n'
                '[ -p "$DEVHUB_PW_FIFO" ] || exit 1\nexec timeout 15 cat "$DEVHUB_PW_FIFO"\n')
    os.chmod(ASKPASS, 0o700)
    tp = jpath("token")
    if not os.path.exists(tp):
        fd = os.open(tp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(24))
    TOKEN = open(tp).read().strip()
    lp = jpath("devhubd.log")
    if os.path.exists(lp) and os.path.getsize(lp) > 2_000_000:
        os.replace(lp, lp + ".1")
    load_local_presets()
    for d in load_json("devices.json", {"devices": []}).get("devices", []):
        try:
            d = clean_device(d)
            H.devices[d["id"]] = d
        except ApiError as e:
            log("skipping bad device", d.get("id"), e)
    st = load_json("state.json", {})
    H.locks = st.get("locks", {})
    H.queues = st.get("queues", {})
    H.requests = st.get("requests", {})
    H.sessions = st.get("sessions", {})
    H.notices = st.get("notices", {})
    H.settings.update({k: v for k, v in st.get("settings", {}).items() if k in DEFAULT_SETTINGS})
    H.paused = set(st.get("paused", []))
    H.owned = st.get("owned", {})
    H.chain = st.get("chain", {})
    H.secrets = load_json("secrets.json", {})
    H.agents = load_json("agents.json", {})
    try:
        with open(jpath("events.jsonl"), encoding="utf-8") as f:
            for line in f.readlines()[-1500:]:
                try:
                    H.events.append(json.loads(line))
                except ValueError:
                    pass
    except FileNotFoundError:
        pass
    import_lock_files()
    save_state()
    try:
        HTTPD = Server((BIND, PORT), Handler)
    except OSError as e:
        print("DeviceHub: cannot listen on %s:%d (%s) - already running?" % (BIND, PORT, e), file=sys.stderr)
        sys.exit(1)
    signal.signal(signal.SIGTERM, lambda *a: threading.Thread(target=HTTPD.shutdown, daemon=True).start())
    threading.Thread(target=loop, daemon=True).start()
    log("DeviceHub %s (%s) listening on http://%s:%d, data in %s, %d devices" % (
        VERSION, "WSL" if PLATFORM == "wsl" else "Linux", BIND, PORT, DATA, len(H.devices)))
    event(None, "hub", "DeviceHub started")
    try:
        HTTPD.serve_forever()
    finally:
        save_state()
        log("DeviceHub stopped")


if __name__ == "__main__":
    main()
