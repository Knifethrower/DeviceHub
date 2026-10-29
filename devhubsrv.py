#!/usr/bin/env python3
"""DeviceHub server mode: one hub on a LAN server (a container) instead of on your own PC.

Everything devhubd.py does stays (locks, approvals, queues, the shared SSH connections, installs, screenshots).
On top of it:
  - Handhelds announce themselves: the DeviceHub Beacon port (beacon/) tells the hub "I'm up at this IP", signed with
    the hub's beacon key. New ones wait for your approval; approved ones get the hub's SSH key in their
    authorized_keys and the hub connects back to them. Their SSH host keys come signed in the announce and are
    pinned, and an IP change from DHCP follows the device by itself.
  - Login: the web page needs DEVHUB_PASSWORD; only a logged-in page acts as the user (approvals, settings, Away
    mode). Agents use the API token (X-DevHub-Token), as before.
  - It listens on DEVHUB_BIND (0.0.0.0) instead of loopback only.

Environment (besides devhubd's DEVHUB_PORT/DEVHUB_DATA):
  DEVHUB_PASSWORD     the web page's login password (required)
  DEVHUB_PUBLIC_URL   the URL handhelds announce to, e.g. http://192.168.68.133:8765 (default: what the browser used)
  DEVHUB_HUB_NAME     the hub's name in the beacon's messages (default: DeviceHub)
"""
import hashlib, hmac, html, io, json, os, re, secrets, subprocess, threading, time, urllib.parse, zipfile

os.environ.setdefault("DEVHUB_BIND", "0.0.0.0")
os.environ.setdefault("DEVHUB_PLATFORM", "linux")
import devhubd as D   # noqa: E402  (after the environment defaults above)
from devhubd import H, ApiError, route, now, event, alert, log   # noqa: E402

PASSWORD = os.environ.get("DEVHUB_PASSWORD") or ""
PUBLIC_URL = (os.environ.get("DEVHUB_PUBLIC_URL") or "").rstrip("/")
HUB_NAME = os.environ.get("DEVHUB_HUB_NAME") or "DeviceHub"
SESSION_DAYS = 30
BEACON_DIR = os.path.join(D.HERE, "beacon")
OPEN_PATHS = {"/api/ping", "/api/beacon/hello", "/api/beacon", "/login", "/sw.js"}

D.DEFAULT_SETTINGS["beacon_auto_approve"] = False   # new handhelds join without asking (trusted LAN)
H.settings.setdefault("beacon_auto_approve", False)
H.beacons = {}      # beacon id -> record (beacons.json)
H.nonces = {}       # nonce -> expiry
H.login_fails = []


def secret_file(name, nbytes):
    p = D.jpath(name)
    if not os.path.exists(p):
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(nbytes))
    return open(p).read().strip()


def hub_key_path():
    return D.jpath("hub_ed25519")


def hub_pubkey():
    with open(hub_key_path() + ".pub") as f:
        return " ".join(f.read().split()[:2]) + " devicehub@" + re.sub(r"[^A-Za-z0-9_.-]", "-", HUB_NAME)


def ensure_hub_key():
    if not os.path.exists(hub_key_path()):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "devicehub", "-f", hub_key_path()], check=True)


# ---------------------------------------------------------------- known hosts: one file for the beacon devices

_kh = D.known_hosts_file


def known_hosts_file(d, card=None):
    """Card profiles keep their own files; every other device shares data/known_hosts/hub (ssh in a container would
    otherwise use the account's ~/.ssh, which isn't on the data volume)."""
    p = _kh(d, card)
    if p:
        return p
    p = D.jpath(os.path.join("known_hosts", "hub"))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


D.known_hosts_file = known_hosts_file


def pin_host_keys(d, keys):
    """The beacon's signed host keys become the only accepted ones for this host:port (other devices' old entries
    for the same IP go, hashed ones too: Debian's ssh hashes what it learns). No keys sent (unreadable on that CFW):
    the entries are cleared and ssh learns the key on connect. Returns True when the file changed."""
    p = known_hosts_file(d)
    spec = d["host"] if d["port"] == 22 else "[%s]:%d" % (d["host"], d["port"])
    want = ["%s %s" % (spec, k) for k in keys]
    try:
        with open(p, encoding="utf-8") as f:
            before = f.read()
    except FileNotFoundError:
        before = ""
    found = subprocess.run(["ssh-keygen", "-F", spec, "-f", p], capture_output=True, text=True).stdout if before else ""
    have = [ln for ln in found.splitlines() if ln and not ln.startswith("#")]
    if sorted(have) == sorted(want):
        return False
    if have:
        subprocess.run(["ssh-keygen", "-R", spec, "-f", p], capture_output=True)
        try:
            os.remove(p + ".old")
        except OSError:
            pass
    if want:
        with open(p, "a", encoding="utf-8") as f:
            f.write("".join(ln + "\n" for ln in want))
    return True


# ---------------------------------------------------------------- login (the web page) and the request gate

def session_key():
    return (secret_file("session.key", 32) + PASSWORD).encode()


def make_session():
    issued = str(int(now()))
    return issued + "." + hmac.new(session_key(), issued.encode(), hashlib.sha256).hexdigest()


def session_ok(handler):
    if not PASSWORD:
        return False
    for part in (handler.headers.get("Cookie") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "devhub_session" and "." in v:
            issued, sig = v.split(".", 1)
            good = hmac.new(session_key(), issued.encode(), hashlib.sha256).hexdigest()
            if hmac.compare_digest(sig, good) and issued.isdigit() and now() - int(issued) < SESSION_DAYS * 86400:
                return True
    return False


def gate(handler, method, path, q, auth):
    logged_in = session_ok(handler)
    D.REQ.ui = logged_in
    if path in OPEN_PATHS:
        return None
    if path == "/" and not logged_in:
        return (302, "text/plain", b"", {"Location": "/login"})
    if auth and not logged_in:
        tok = handler.headers.get("X-DevHub-Token") or (q.get("t") or [""])[0]
        if not hmac.compare_digest(tok.encode(), D.TOKEN.encode()):
            raise ApiError(401, "log in, or send the API token (X-DevHub-Token)")
    return None


LOGIN_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>DeviceHub login</title><style>
:root{--bg:#f4f5f7;--panel:#fff;--text:#1b1f24;--muted:#5d6673;--line:#e1e4e8;--accent:#2563eb;--bad:#c02626}
@media (prefers-color-scheme:dark){:root{--bg:#111418;--panel:#1a1e24;--text:#e6e9ee;--muted:#a0a8b4;--line:#2c323b;--accent:#6b9bff;--bad:#f87171}}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.45 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh}
form{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:24px;width:min(340px,calc(100vw - 32px));display:grid;gap:12px}
h1{font-size:18px;margin:0}input{font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--text)}
button{font:inherit;padding:8px;border-radius:8px;border:0;background:var(--accent);color:#fff;cursor:pointer}.err{color:var(--bad);margin:0}
</style></head><body><form method="post" action="/login"><h1>%(name)s</h1>%(err)s
<input type="password" name="password" placeholder="Password" autofocus autocomplete="current-password" required>
<button>Log in</button></form></body></html>"""


def login_page(err=""):
    body = LOGIN_PAGE % {"name": html.escape(HUB_NAME), "err": '<p class="err">%s</p>' % html.escape(err) if err else ""}
    return ("text/html; charset=utf-8", body.encode())


@route("GET", "/login", auth=False)
def r_login_page(body, q):
    return login_page("" if PASSWORD else "DEVHUB_PASSWORD is not set on the server: nobody can log in")


@route("POST", "/login", auth=False, raw=True)
def r_login(h, q):
    n = int(h.headers.get("Content-Length") or 0)
    form = urllib.parse.parse_qs(h.rfile.read(min(n, 4096)).decode("utf-8", "replace"))
    H.login_fails[:] = [t for t in H.login_fails if now() - t < 60]
    if len(H.login_fails) >= 10:
        return login_page("Too many tries: wait a minute")
    if not PASSWORD or not hmac.compare_digest((form.get("password") or [""])[0].encode(), PASSWORD.encode()):
        H.login_fails.append(now())
        return login_page("Wrong password")
    secure = "; Secure" if (h.headers.get("X-Forwarded-Proto") or "").lower() == "https" else ""
    cookie = "devhub_session=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax%s" % (make_session(), SESSION_DAYS * 86400, secure)
    return redirect("/", {"Set-Cookie": cookie})


def redirect(where, extra=None):
    """A route result that devhubd's handler sends as a redirect (its routes otherwise always answer 200)."""
    return ("text/plain", b"", dict(extra or {}, Location=where, **{"X-Status": "303"}))


@route("POST", "/logout")
def r_logout(body, q):
    return redirect("/login", {"Set-Cookie": "devhub_session=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax"})


_reply = D.Handler.reply


def reply(self, code, ctype, data, extra=None):
    extra = dict(extra or {})
    code = int(extra.pop("X-Status", code))
    _reply(self, code, ctype, data, extra)


D.Handler.reply = reply


# ---------------------------------------------------------------- beacons

def save_beacons():
    D.save_json("beacons.json", H.beacons)


def sign(text):
    return hmac.new(bytes.fromhex(D.BEACON_KEY), text.encode(), hashlib.sha256).hexdigest()


def answer(fields):
    """key=value lines for the beacon, signed: the last line is sig= over every byte before it."""
    body = "".join("%s=%s\n" % (k, str(v).replace("\n", " ")) for k, v in fields if v is not None)
    return ("text/plain; charset=utf-8", (body + "sig=%s\n" % sign(body)).encode())


@route("GET", "/api/beacon/hello", auth=False)
def r_beacon_hello(body, q):
    with H.lock:
        t = now()
        for k in [k for k, exp in H.nonces.items() if exp < t]:
            del H.nonces[k]
        if len(H.nonces) > 500:
            raise ApiError(429, "too many announces")
        nonce = secrets.token_hex(16)
        H.nonces[nonce] = t + 120
    return ("text/plain; charset=utf-8", ("nonce=%s\nhub=%s\n" % (nonce, HUB_NAME)).encode())


BID_RE = re.compile(r"^[0-9a-f]{8,32}$")
KEY_RE = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp\d+) [A-Za-z0-9+/=]+$")


@route("POST", "/api/beacon", auth=False, raw=True)
def r_beacon(h, q):
    n = int(h.headers.get("Content-Length") or 0)
    if not 0 < n <= 16384:
        raise ApiError(413, "announce too big")
    raw = h.rfile.read(n)
    sig = (h.headers.get("X-Beacon-Sig") or "").strip().lower()
    good = hmac.new(bytes.fromhex(D.BEACON_KEY), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, good):
        raise ApiError(403, "bad signature: this beacon was made for another hub, or its key was changed")
    kv, hostkeys = {}, []
    for line in raw.decode("utf-8", "replace").splitlines():
        k, _, v = line.partition("=")
        if k == "hostkey":
            if KEY_RE.match(v.strip()):
                hostkeys.append(v.strip())
        elif k:
            kv[k] = v.strip()[:200]
    with H.lock:
        if H.nonces.pop(kv.get("nonce", ""), 0) < now():
            raise ApiError(403, "stale or reused nonce")
    bid = kv.get("id", "")
    if not BID_RE.match(bid):
        raise ApiError(400, "bad device id")
    ip = kv.get("ip") or h.client_address[0]
    if not re.match(r"^[0-9.]+$", ip):
        ip = h.client_address[0]
    try:
        port = int(kv.get("port") or 22)
    except ValueError:
        port = 22
    user = kv.get("user") or "root"
    if not re.match(r"^[A-Za-z0-9._-]+$", user):
        user = "root"
    keyok = kv.get("keyok", "0")
    facts = {k: kv.get(k, "") for k in ("name", "cfw", "cfw_version", "hostname", "mac", "ssh", "autostart", "width",
                                        "height", "ram", "cpu", "arch", "kernel")}
    for k in ("pm_dir", "ports_dir"):     # PortMaster's own paths on the device; absolute, no shell specials
        v = kv.get(k, "")
        facts[k] = v if re.match(r"^/[A-Za-z0-9._/ -]*$", v) and ".." not in v else ""
    with H.lock:
        b = H.beacons.get(bid)
        new = b is None
        if new:
            b = H.beacons[bid] = {"id": bid, "state": "pending", "dev": "", "first": now()}
        b.update(facts, ip=ip, port=port, user=user, keyok=keyok, hostkeys=hostkeys, last=now(), src=h.client_address[0])
        if new:
            if H.settings.get("beacon_auto_approve"):
                b["state"] = "approved"
                event(None, "beacon", "%s (%s, %s) joined (auto-approve is on)" % (b["name"] or bid, b["cfw"], ip))
            else:
                event(None, "beacon", "%s (%s, %s) asks to join" % (b["name"] or bid, b["cfw"], ip))
                alert(None, "New device wants to join: %s (%s, %s)" % (b["name"] or bid, b["cfw"] or "?", ip),
                      kind="enroll", strip=False)
        state = b["state"]
        save_beacons()
    msg = ""
    if state == "approved" and keyok in ("1", "fail"):
        msg = sync_device(b)
    elif state == "approved":
        msg = "installing the hub's SSH key"
    elif state == "pending":
        msg = "waiting for approval in %s" % HUB_NAME
    out = [("ok", 1), ("state", state), ("dev", b.get("dev") or ""), ("challenge", kv.get("challenge", "")),
           ("interval", 10 if state == "pending" or (state == "approved" and keyok == "0") else 60), ("msg", msg),
           ("hub", HUB_NAME)]
    if state == "approved":
        out.append(("pubkey", hub_pubkey()))
    return answer(out)


CFW_MAP = {k.lower(): k for k in D.CFW_PRESETS}
CFW_MAP.update({"darkos": "dArkOS", "arkos": "ArkOS", "muos": "muOS", "knulli": "Knulli", "rocknix": "ROCKNIX"})


def new_dev_id(name):
    base = re.sub(r"[^a-z0-9]+", "-", (name or "device").lower()).strip("-")[:20] or "device"
    if not re.match(r"^[a-z0-9]", base):
        base = "d" + base
    cand, i = base, 2
    while cand in H.devices:
        cand = "%s-%d" % (base[:20], i)
        i += 1
    return cand


def sync_device(b):
    """An approved beacon: make its device on first contact, afterwards follow its IP/user/port and keep its host
    keys pinned. Runs on every announce, so it also nudges the hub to (re)connect promptly."""
    key_ok = b["keyok"] == "1"
    with H.lock:
        d = H.devices.get(b.get("dev") or "")
        if not d:
            cfw = CFW_MAP.get((b.get("cfw") or "").lower().replace(" ", ""), "Other")
            src = {k: v for k, v in D.CFW_PRESETS.get(cfw, {}).items() if k in D.DEVICE_DEFAULTS}
            share = D.CFW_PRESETS.get(cfw, {}).get("smb_share")
            src.update(id=new_dev_id(b.get("name")), name=b.get("name") or b["id"], host=b["ip"], port=b["port"],
                       user=b["user"], cfw=cfw, auth="key" if key_ok else "password",
                       **{k: b[k] for k in ("pm_dir", "ports_dir") if b.get(k)},
                       identity=hub_key_path() if key_ok else "",
                       smb="\\\\%s\\%s" % (b["ip"], share) if share else "",
                       screen="%sx%s" % (b["width"], b["height"]) if b.get("width") and b.get("height") else "",
                       ram_mb=int(b["ram"]) if (b.get("ram") or "").isdigit() else 0,
                       notes="joined via the DeviceHub beacon (%s)" % b["id"])
            d = D.clean_device(src)
            H.devices[d["id"]] = d
            b["dev"] = d["id"]
            D.save_devices()
            save_beacons()
            pin_host_keys(d, b["hostkeys"])
            event(d["id"], "beacon", "device added from its beacon: %s" % D.dname(d))
            if not key_ok:
                alert(d["id"], "couldn't install the hub's SSH key on it: enter its password (Edit > Password)", kind="ssh")
            D.rt(d["id"])["poke"] = True
            return "added as %s" % d["id"]
        dev, old = d["id"], d
        change = {k: v for k, v in (("host", b["ip"]), ("port", b["port"]), ("user", b["user"]),
                                    ("pm_dir", b.get("pm_dir")), ("ports_dir", b.get("ports_dir"))) if v and old.get(k) != v}
        if key_ok and old["auth"] == "password" and not D.secret_key(old) in H.secrets:
            change.update(auth="key", identity=hub_key_path())     # the key landed after all
    if change:
        if D.rt(dev)["master"]["state"] == "up":
            D.disconnect(dev, old)
        with H.lock:
            d = D.clean_device(change, old)
            H.devices[dev] = d
            H.patterns.pop(dev, None)
            D.save_devices()
        event(dev, "beacon", "now at %s" % ", ".join("%s %s" % kv for kv in change.items() if kv[0] != "identity"))
    repinned = pin_host_keys(d, b["hostkeys"])
    r = D.rt(dev)
    if repinned and r["master"].get("block") == "hostkey_changed":
        r["master"].pop("block", None)    # the device's signed announce brought its current keys: try again
        r["master"]["retry"] = True
        r["poke"] = True
        with H.lock:
            H.alerts[:] = [a for a in H.alerts if not (a["dev"] == dev and a.get("kind") == "ssh")]
        event(dev, "ssh", "host keys updated from its announce; reconnecting")
    if change or r["master"]["state"] in ("offline", "error", "unknown"):
        r["master"]["next_try"] = 0
        r["poke"] = True
    if key_ok and r["master"].get("block") == "auth_failed" and d["auth"] == "key":
        r["master"]["retry"] = True     # the key arrived after a refused first try
        r["poke"] = True
    return "known as %s" % dev


def beacons_public():
    return [{k: b.get(k) for k in ("id", "state", "dev", "name", "cfw", "cfw_version", "ip", "mac", "user", "ssh", "keyok",
                                   "autostart", "width", "height", "first", "last")} for b in H.beacons.values()]


@route("POST", r"/api/beacons/([0-9a-f]{8,32})/(approve|deny|forget)")
def r_beacon_decide(body, q, bid, act):
    c = D.caller(body)
    if not c.get("ui"):
        raise ApiError(403, "only the user lets devices join (DeviceHub page)")
    with H.lock:
        b = H.beacons.get(bid)
        if not b:
            raise ApiError(404, "no such beacon")
        if act == "forget":
            del H.beacons[bid]
        else:
            b["state"] = "approved" if act == "approve" else "denied"
        save_beacons()
    event(None, "beacon", "%s %s" % (b.get("name") or bid, {"approve": "approved: it gets the hub's SSH key on its next announce",
                                                            "deny": "turned down", "forget": "forgotten"}[act]), c["name"])
    return {"ok": True}


# /api/state carries the beacons (and that this is a server) for the page
_r_state = next(fn for m, rx, fn, a, raw in D.ROUTES if m == "GET" and rx.pattern == "^/api/state$")


def r_state(body, q):
    res = _r_state(body, q)
    with H.lock:
        res["server"] = {"hub": HUB_NAME, "beacons": beacons_public(), "public_url": PUBLIC_URL}
    return res


D.ROUTES[:] = [(m, rx, r_state if fn is _r_state else fn, a, raw) for m, rx, fn, a, raw in D.ROUTES]


# ---------------------------------------------------------------- no blind polling: devices are reached only when they ask
# Local DeviceHub probes every device's SSH port every 10 s and connects whenever it answers. Here devices come and go,
# so the hub only reaches out to a device that announced itself within ANNOUNCE_LEASE_S (the beacon re-announces every
# minute), or for a minute after you press Connect (devices added by hand). An open connection is kept while it lives.

ANNOUNCE_LEASE_S = 150
CONNECT_WINDOW_S = 60
_upkeep = D.upkeep


def last_announce(dev):
    return max([b.get("last") or 0 for b in H.beacons.values() if b.get("dev") == dev] or [0])


def upkeep(dev):
    d = H.devices.get(dev)
    if not d:
        return
    r = D.rt(dev)
    if (not d["enabled"] or now() - last_announce(dev) < ANNOUNCE_LEASE_S or now() < r.get("connect_until", 0)
            or D.master_alive(d)[0]):
        return _upkeep(dev)
    if r["online"]:
        event(dev, "net", "%s stopped announcing itself" % D.dname(d))
        r["changed"] = now()
    r["online"] = False
    if r["master"]["state"] == "up":
        event(dev, "ssh", "shared connection closed")
    D.set_master(dev, "offline", "not announcing: run the DeviceHub Beacon port on it (or set up its boot hook), or press Connect")


D.upkeep = upkeep
_r_conn = next(fn for m, rx, fn, a, raw in D.ROUTES if fn.__name__ == "r_conn")


def r_conn(body, q, dev, act):
    if act == "connect":
        D.rt(dev)["connect_until"] = now() + CONNECT_WINDOW_S
    return _r_conn(body, q, dev, act)


D.ROUTES[:] = [(m, rx, r_conn if fn is _r_conn else fn, a, raw) for m, rx, fn, a, raw in D.ROUTES]


# ---------------------------------------------------------------- the beacon port, made for this hub

PORT_JSON = {
    "version": 2, "name": "devicehub.zip", "items": ["DeviceHub Beacon.sh", "devicehub/"], "items_opt": None,
    "attr": {"title": "DeviceHub Beacon", "porter": ["DeviceHub"], "genres": ["other"], "image": {}, "rtr": True,
             "runtime": None, "reqs": [], "arch": ["aarch64", "armhf", "x86_64"],
             "desc": "Announces this handheld to your DeviceHub server, which then connects to it over SSH.",
             "inst": "Switch SSH on, run it once, approve the device on DeviceHub's page."},
}


@route("GET", "/api/beacon/port.zip", raw=True)
def r_beacon_port(h, q):
    if not D.REQ.ui:
        raise ApiError(403, "log in to download the beacon (it carries the hub's key)")
    url = PUBLIC_URL or "http://%s" % (h.headers.get("Host") or "")
    autostart = (q.get("autostart") or ["1"])[0] == "1"
    conf = ("# made by %s; the key lets this device announce itself (keep the port private)\n"
            "HUB_URL='%s'\nHUB_KEY='%s'\nHUB_NAME='%s'\nAUTOSTART=%d\n") % (
        HUB_NAME, url, D.BEACON_KEY, HUB_NAME.replace("'", ""), int(autostart))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        def add(name, data, mode=0o644):
            zi = zipfile.ZipInfo(name, time.localtime()[:6])
            zi.external_attr = (0o100000 | mode) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(zi, data)
        with open(os.path.join(BEACON_DIR, "DeviceHub Beacon.sh"), "rb") as f:
            add("DeviceHub Beacon.sh", f.read(), 0o755)
        with open(os.path.join(BEACON_DIR, "beacon.sh"), "rb") as f:
            add("devicehub/beacon.sh", f.read(), 0o755)
        add("devicehub/beacon.conf", conf)
        add("devicehub/port.json", json.dumps(PORT_JSON, indent=4) + "\n")
        with open(os.path.join(BEACON_DIR, "README.txt"), "rb") as f:
            add("devicehub/README.txt", f.read())
    return ("application/zip", buf.getvalue(), {"Content-Disposition": 'attachment; filename="devicehub.zip"'})


@route("POST", "/api/beacon/rotate")
def r_beacon_rotate(body, q):
    c = D.caller(body)
    if not c.get("ui"):
        raise ApiError(403, "only the user changes the beacon key")
    p = D.jpath("beacon.key")
    os.replace(p, p + ".old")
    D.BEACON_KEY = secret_file("beacon.key", 32)
    event(None, "beacon", "beacon key changed: download the beacon port again for every device", c["name"])
    return {"ok": True}


def main():
    if not PASSWORD:
        print("devhubsrv: set DEVHUB_PASSWORD (the web page's login); refusing to serve the LAN without it")
        raise SystemExit(1)
    os.makedirs(D.DATA, exist_ok=True)
    D.BEACON_KEY = secret_file("beacon.key", 32)
    ensure_hub_key()
    H.beacons = D.load_json("beacons.json", {})
    D.GATE = gate
    D.main()


if __name__ == "__main__":
    main()
