#!/usr/bin/env python3
"""devhub: command line for DeviceHub (device locks, shared SSH connections, status).

Runs on Windows (Git Bash / PowerShell) or in WSL. Talks to the DeviceHub server on 127.0.0.1:8765, or to a DeviceHub
on another machine (server mode): DEVHUB_URL=http://hub:8765 and DEVHUB_TOKEN=<an agent token from its web page>.
Then run/push/pull go through the hub, which runs them over the device's shared connection.

  devhub status [DEV]                          devices, connections, locks
  devhub acquire DEV "purpose" [--for 30m] [--wait 20m [--need "why this device"]] [--watch NAME]
  devhub renew DEV [30m] ["why"] [--watch NAME]  still need it (answers a lock check)
  devhub release DEV [--dirty "why"]           give it back; the front end must be running
  devhub run DEV [--ro] -- COMMAND             run over the shared connection (lock needed unless --ro)
  devhub push DEV LOCAL REMOTE                 copy a file to the device (lock needed)
  devhub pull DEV REMOTE LOCAL                 copy a file from the device
  devhub install DEV PORT.zip|RUNTIME.squashfs|URL [--refresh ...]  port zip via harbourmaster; .squashfs runtime to
                                               PortMaster/libs; an http(s) URL (.zip or .zip.md5) is downloaded by the device
  devhub ports DEV [--json]                    ports installed on the device (read-only)
  devhub fe DEV start|stop|restart|status      front end (EmulationStation, ...)
  devhub shot DEV [OUT.png]                    framebuffer screenshot
  devhub env DEV [--wsl]                       shell variables for scripts (DH_SSH, DH_SMB, ...)
  devhub log [DEV] [-n 30]                     recent events
  devhub name "session name"                   how this Claude session shows up in DeviceHub
  devhub whoami                                who DeviceHub thinks you are, what you hold
  devhub ui | start | stop                     open the web UI / start or stop the server
  devhub hook                                  Claude Code hook entry (event JSON on stdin)
  devhub install-hooks [--settings FILE] [--print] [--remove]   add the hooks to Claude Code's settings
  devhub agent-guide                           instructions for an AI agent (docs/AGENTS.md)

Identity: CLAUDE_CODE_SESSION_ID (set by Claude Code) or DEVHUB_SESSION (any other agent), plus --as NAME
or DEVHUB_NAME.
Exit codes: 0 ok, 1 refused/busy, 2 usage, 3 DeviceHub not running.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("DEVHUB_DATA") or os.path.join(HERE, "data")
PORT = int(os.environ.get("DEVHUB_PORT") or 8765)
IS_WIN = os.name == "nt"
REMOTE = (os.environ.get("DEVHUB_URL") or "").rstrip("/")     # a DeviceHub on another machine


def hub_conn(timeout):
    import http.client
    import urllib.parse
    if not REMOTE:
        return http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    u = urllib.parse.urlsplit(REMOTE)
    if u.scheme == "https":
        return http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout)
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)


def hub_token():
    if REMOTE:
        tok = os.environ.get("DEVHUB_TOKEN") or ""
        if not tok:
            raise HubDown("DEVHUB_URL is set but DEVHUB_TOKEN isn't (make an agent token on the hub's page: Agents)")
        return tok
    try:
        with open(os.path.join(DATA, "token")) as f:
            return f.read().strip()
    except OSError:
        raise HubDown("no token file in %s: DeviceHub has never run with this data folder" % DATA)


class HubDown(Exception):
    pass


def call(method, path, body=None, timeout=60):
    import http.client
    tok = hub_token()
    c = hub_conn(timeout)
    try:
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None,
                  headers={"X-DevHub-Token": tok, "Content-Type": "application/json"})
        r = c.getresponse()
        raw = r.read()
    except (OSError, http.client.HTTPException) as e:
        raise HubDown(str(e))
    finally:
        c.close()
    try:
        res = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        res = {"ok": False, "error": raw[:300].decode("utf-8", "replace")}
    if r.status >= 400:
        res["ok"] = False
    return res


def hook():
    """Claude Code hook: pass the event to the hub, print what it answers. Never fails the tool call."""
    try:
        ev = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
        out = call("POST", "/api/hook", ev, timeout=4).get("output")
        if out:
            sys.stdout.write(json.dumps(out))
    except Exception:
        pass
    return 0


# ---------------------------------------------------------------- helpers

def find_transcript(sid):
    base = os.path.join(os.path.expanduser("~"), ".claude", "projects")
    try:
        for sub in os.listdir(base):
            p = os.path.join(base, sub, sid + ".jsonl")
            if os.path.exists(p):
                return p
    except OSError:
        pass
    return None


def ident(a=None):
    b = {"client": "cli"}
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid and os.environ.get("DEVHUB_SESSION"):     # any other agent: its own stable id
        sid = "x-" + os.environ["DEVHUB_SESSION"][:60]
    if sid:
        b["session"] = sid
        t = find_transcript(sid) if IS_WIN and not sid.startswith("x-") else None
        if t:
            b["transcript"] = t
    name = (getattr(a, "as_name", None) if a else None) or os.environ.get("DEVHUB_NAME")
    if name:
        b["name"] = name
    elif not sid:
        b["name"] = "%s@terminal" % (os.environ.get("USERNAME") or os.environ.get("USER") or "user")
    return b


def notices(r):
    for n in r.get("notices") or []:
        print(n, file=sys.stderr)
    if r.get("warning"):
        print("[DeviceHub] warning: " + r["warning"], file=sys.stderr)


def finish(r):
    notices(r)
    msg = r.get("msg") or r.get("error") or ""
    if msg:
        print(msg, file=sys.stdout if r.get("ok") else sys.stderr)
    return 0 if r.get("ok") else 1


def ago(s):
    s = int(max(0, s))
    if s < 90:
        return "%ds" % s
    if s < 5400:
        return "%dm" % ((s + 30) // 60)
    return "%dh%02dm" % (s // 3600, (s % 3600) // 60)


def hm(t):
    import time
    return time.strftime("%H:%M", time.localtime(t))


def ssh_argv(conn):
    a = ["ssh", "-S", conn["socket"], "-o", "ControlMaster=no", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
         "-o", "LogLevel=ERROR"]
    if conn.get("port", 22) != 22:
        a += ["-p", str(conn["port"])]
    if conn.get("known_hosts"):      # this SD card's own host key (card profiles)
        a += ["-o", 'UserKnownHostsFile="%s"' % conn["known_hosts"]]
    a.append(conn["target"])
    return (["wsl.exe", "-e"] + a) if IS_WIN else a


def local_path(p):
    """Git Bash paths (/c/..., /tmp/...) to Windows paths for Windows Python; the wrapper turns MSYS conversion off."""
    if IS_WIN and p.startswith("/") and os.environ.get("MSYSTEM"):
        import subprocess
        try:
            return subprocess.check_output(["cygpath", "-w", p], text=True).strip()
        except (OSError, subprocess.CalledProcessError):
            pass
    return p


def wsl_path(p):
    p = os.path.abspath(p)
    if IS_WIN and len(p) > 2 and p[1] == ":":
        return "/mnt/%s%s" % (p[0].lower(), p[2:].replace("\\", "/"))
    return p


def squote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def authorize(a, ro, cmd=""):
    body = ident(a)
    body.update(dev=a.dev, ro=ro, cmd=cmd)
    r = call("POST", "/api/authorize", body)
    notices(r)
    if not r.get("ok"):
        print(r.get("msg") or r.get("error"), file=sys.stderr)
        return None
    return r["conn"]


# ---------------------------------------------------------------- commands

def c_status(a):
    st = call("GET", "/api/state")
    if not st.get("ok"):
        return finish(st)
    r = call("POST", "/api/whoami", ident(a))
    devs = [d for d in st["devices"] if not a.dev or d["id"] == a.dev]
    devs.sort(key=lambda d: 0 if d.get("lock") else 3 if d.get("oos") else 1 if d["rt"]["online"] is True else 2)
    # locked, online, offline, out of service
    if a.dev and not devs:
        print("no device %r" % a.dev, file=sys.stderr)
        return 1
    if a.json:
        print(json.dumps(devs, indent=1))
        return 0
    t = st["now"]
    for d in devs:
        rt = d["rt"]
        s = rt.get("status") or {}
        m = rt["master"]
        net = "disabled" if not d["enabled"] else ("online" if rt["online"] else "offline" if rt["online"] is False else "?")
        ssh = m["state"] + ("" if m["state"] != "up" else "(hub)" if m.get("owned") else "(ext)")
        bits = ["OUT OF SERVICE%s" % (" (%s)" % d["oos_note"] if d.get("oos_note") else "")] if d.get("oos") else []
        if len(d.get("cards") or []) > 1:
            bits.append("card: %s" % next((c["label"] for c in d["cards"] if c["id"] == d["card"]), "?"))
        if s and t - rt["status_at"] < 600:
            if d["fe_proc"]:
                bits.append("fe:" + ("up" if s.get("fe") else "desktop" if d.get("fe_alt") and s.get("fealt") else "DOWN"))
            if s.get("bat") is not None:
                plug = {True: " plugged in", False: " on battery"}.get(s.get("plugged"), "")
                if s.get("batst") == "Charging":
                    plug += " (charging)"
                bits.append("bat %d%%%s" % (s["bat"], plug))
            if s.get("MemAvailable"):
                bits.append("free %d/%d MB" % (s["MemAvailable"] // 1024, s["MemTotal"] // 1024))
            if s.get("app"):
                bits.append("app %s %dMB" % (s["app"]["name"], s["app"]["rss"] // 1024))
            if s.get("gpu_driver"):
                bits.append("gpu driver: %s" % s["gpu_driver"])
        print("%-9s %-18s %-9s %-15s %-7s ssh:%-14s %s" % (d["id"], d["name"][:18], d["cfw"][:9], d["host"], net, ssh,
                                                           "  ".join(bits)))
        if m.get("msg") and m["state"] not in ("up",):
            print("          ssh: %s" % m["msg"])
        lk = d["lock"]
        if lk:
            left = lk["lease_until"] - t
            print("          LOCK %s - \"%s\" - since %s, lease %s, %s, holder idle %s%s" % (
                lk["holder"]["name"], lk["purpose"], hm(lk["since"]),
                ("%s left" % ago(left)) if left > 0 else "ran out %s ago" % ago(-left), lk["state"], ago(lk["idle"]),
                " [%s]" % lk["verdict"] if lk.get("verdict") else ""))
        else:
            print("          free")
        for i, e in enumerate(d["queue"]):
            if e.get("offer_from"):
                print("          came online: held for %s until %s (it waited longest)" % (e["name"], hm(e["until"])))
                continue
            print("          queue %d: %s - %s%s%s" % (i + 1, e["name"], e["purpose"],
                                                      " (needs this device: %s)" % e["need"] if e.get("need") else "",
                                                      " - deciding on an offer, place kept until %s" % hm(e["until"])
                                                      if e.get("held") else ""))
        for x in d["requests"]:
            print("          approval pending: %s - %s" % (x["name"], x["purpose"]))
    if r.get("ok"):
        notices(r)
        if r.get("holds"):
            print("you (%s) hold: %s" % (r["caller"]["name"], ", ".join(r["holds"])))
    return 0


def c_acquire(a):
    import time
    body = ident(a)
    body.update(dev=a.dev, purpose=a.purpose, ttl=a.for_, watch=a.watch, need=a.need)
    wait_s = parse_secs(a.wait) if a.wait else 0
    end = time.time() + wait_s
    last, asked, moves = None, a.dev, 0
    while True:
        body["wait"] = bool(wait_s)
        r = call("POST", "/api/lock/acquire", body)
        notices(r)
        if r.get("ok"):
            print(r["msg"])
            if body["dev"] != asked:
                print("NOTE: you now hold %s, not %s: use \"%s\" in your devhub commands" % (body["dev"], asked, body["dev"]))
            return 0
        msg = r.get("msg") or r.get("error")
        if r.get("status") == "move" and r.get("alt"):
            print(msg, file=sys.stderr)
            moves += 1
            body["dev"] = r["alt"]
            if moves >= 3:      # do not bounce around: wait where we are now
                body["need"] = "moved %d times without getting a device" % moves
            last = None
            continue
        if r.get("status") == "offer":      # another device came online: the session decides (place kept 5 min)
            print(msg, file=sys.stderr)
            return 1
        if not wait_s or time.time() >= end or r.get("status") in ("denied", "offline", "out_of_service") or r.get("error"):
            print(msg + (" (gave up waiting)" if wait_s and time.time() >= end else ""), file=sys.stderr)
            return 1
        key = (r.get("status"), r.get("deadline")) if r.get("status") == "approval" else msg
        if key != last:
            if r.get("status") == "approval" and r.get("deadline"):
                print("asked the user through a DeviceHub notification; approved automatically at %s unless they "
                      "decline - waiting" % hm(r["deadline"]), file=sys.stderr)
            else:
                print(msg + " - waiting", file=sys.stderr)
            last = key
        time.sleep(3)


def parse_secs(v):
    import re
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\w*\s*", str(v).lower())
    if not m:
        raise SystemExit("bad duration %r (use 30m, 2h, 90s)" % v)
    return int(float(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2) or "m"])


def c_renew(a):
    body = ident(a)
    body.update(dev=a.dev, ttl=a.duration, note=" ".join(a.note), watch=a.watch)
    return finish(call("POST", "/api/lock/renew", body))


def c_release(a):
    body = ident(a)
    body.update(dev=a.dev, dirty=a.dirty)
    r = call("POST", "/api/lock/release", body)
    return finish(r)


def c_run(a):
    import subprocess
    cmd = list(a.command)
    while cmd and cmd[0] != "--" and cmd[0].startswith("-"):   # options given after DEV
        opt = cmd.pop(0)
        if opt == "--ro":
            a.ro = True
        elif opt in ("-t", "--timeout") and cmd:
            a.timeout = int(cmd.pop(0))
        else:
            print("unknown option %s (put the command after --)" % opt, file=sys.stderr)
            return 2
    if cmd and cmd[0] == "--":
        cmd.pop(0)
    if not cmd:
        print("nothing to run: devhub run DEV -- COMMAND", file=sys.stderr)
        return 2
    remote = " ".join(cmd)
    if REMOTE:
        return remote_run(a, remote)
    conn = authorize(a, a.ro, remote)
    if not conn:
        return 1
    feed = ConsoleFeed(a)
    feed.add("cmd", ("$ " if not a.ro else "$ (read-only) ") + remote)
    p = subprocess.Popen(ssh_argv(conn) + [remote], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    pumps = [feed.pump(p.stdout, sys.stdout, "out"), feed.pump(p.stderr, sys.stderr, "err")]
    try:
        rc = p.wait(timeout=a.timeout or None)
    except subprocess.TimeoutExpired:
        p.kill()
        print("[DeviceHub] timed out after %d s (the remote command may still be running)" % a.timeout, file=sys.stderr)
        rc = 124
    for t in pumps:
        t.join(5)
    feed.add("cmd", "[exit %d]" % rc)
    feed.close()
    try:
        call("POST", "/api/activity", dict(ident(a), dev=a.dev), timeout=5)
    except HubDown:
        pass
    return rc


def remote_run(a, remote):
    """run through a DeviceHub on another machine: it runs the command over the shared connection and streams the
    output back (application/x-ndjson: {"s": "o"|"e", "d": base64} lines, then {"rc": N})."""
    import base64
    import http.client
    body = dict(ident(a), cmd=remote, ro=bool(a.ro), timeout=a.timeout or 0)
    c = hub_conn(None)
    try:
        c.request("POST", "/api/devices/%s/exec" % a.dev, body=json.dumps(body).encode(),
                  headers={"X-DevHub-Token": hub_token(), "Content-Type": "application/json"})
        r = c.getresponse()
        if "ndjson" not in (r.getheader("Content-Type") or ""):
            try:
                res = json.loads(r.read().decode("utf-8") or "{}")
            except ValueError:
                res = {"ok": False, "error": "HTTP %d" % r.status}
            res["ok"] = False
            return finish(res)
        rc = 255
        for line in r:
            m = json.loads(line)
            if "s" in m:
                dst = sys.stdout if m["s"] == "o" else sys.stderr
                dst.buffer.write(base64.b64decode(m["d"]))
                dst.flush()
            elif "rc" in m:
                rc = m["rc"]
            else:
                notices(m)
        return rc
    except (OSError, http.client.HTTPException) as e:
        raise HubDown(str(e))
    finally:
        c.close()


def id_headers(a):
    import urllib.parse
    b = ident(a)
    return {"X-DevHub-Session": b.get("session") or "", "X-DevHub-Name": urllib.parse.quote(b.get("name") or "")}


def remote_push(a):
    import http.client
    import urllib.parse
    size = os.path.getsize(a.local)
    try:
        c = hub_conn(600)
        c.putrequest("PUT", "/api/devices/%s/file?path=%s" % (a.dev, urllib.parse.quote(a.remote)))
        for k, v in dict(id_headers(a), **{"X-DevHub-Token": hub_token(), "Content-Length": str(size),
                                           "Content-Type": "application/octet-stream"}).items():
            c.putheader(k, v)
        c.endheaders()
        with open(a.local, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                c.send(chunk)
        r = c.getresponse()
        res = json.loads(r.read().decode("utf-8") or "{}")
    except (OSError, http.client.HTTPException) as e:
        raise HubDown(str(e))
    if not res.get("ok"):
        return finish(res)
    notices(res)
    print("%s -> %s:%s (%d bytes)" % (a.local, a.dev, a.remote, size))
    return 0


def remote_pull(a):
    import http.client
    import urllib.parse
    tmp = a.local + ".devhub-part"
    try:
        c = hub_conn(600)
        c.request("GET", "/api/devices/%s/file?path=%s" % (a.dev, urllib.parse.quote(a.remote)),
                  headers=dict(id_headers(a), **{"X-DevHub-Token": hub_token()}))
        r = c.getresponse()
        if r.getheader("Content-Type") != "application/octet-stream":
            res = json.loads(r.read().decode("utf-8") or "{}")
            res["ok"] = False
            return finish(res)
        want, got = int(r.getheader("Content-Length") or -1), 0
        with open(tmp, "wb") as f:
            for chunk in iter(lambda: r.read(1 << 20), b""):
                f.write(chunk)
                got += len(chunk)
    except (OSError, http.client.HTTPException) as e:
        raise HubDown(str(e))
    if got != want:
        os.unlink(tmp)
        print("%s:%s: copy cut short (%d of %d bytes)" % (a.dev, a.remote, got, want), file=sys.stderr)
        return 1
    os.replace(tmp, a.local)
    print("%s:%s -> %s (%d bytes)" % (a.dev, a.remote, a.local, got))
    return 0


class ConsoleFeed:
    """Mirrors a command's output to DeviceHub's console view for the device (batched, best effort)."""

    def __init__(self, a):
        import threading
        self.a, self.lines, self.lock = a, [], threading.Lock()
        self.done = threading.Event()
        self.sender = threading.Thread(target=self.run, daemon=True)
        self.sender.start()

    def add(self, stream, text):
        with self.lock:
            self.lines.append([stream, text])

    def pump(self, src, dst, stream):
        import threading

        def go():
            out = dst.buffer if hasattr(dst, "buffer") else dst
            for raw in iter(src.readline, b""):
                out.write(raw)
                out.flush()
                self.add(stream, raw.decode("utf-8", "replace").rstrip("\r\n"))
        t = threading.Thread(target=go, daemon=True)
        t.start()
        return t

    def flush(self):
        with self.lock:
            batch, self.lines = self.lines, []
        if batch:
            try:
                call("POST", "/api/devices/%s/console/append" % self.a.dev, dict(ident(self.a), lines=batch), timeout=3)
            except HubDown:
                pass

    def run(self):
        while not self.done.wait(0.5):
            self.flush()

    def close(self):
        self.done.set()
        self.sender.join(2)
        self.flush()


def c_push(a):
    import subprocess
    a.local = local_path(a.local)
    if not os.path.isfile(a.local):
        print("no such file: %s" % a.local, file=sys.stderr)
        return 2
    if REMOTE:
        return remote_push(a)
    conn = authorize(a, False, "push %s -> %s" % (os.path.basename(a.local), a.remote))
    if not conn:
        return 1
    part = a.remote + ".devhub-part"
    remote = "cat > %s && mv -f %s %s" % (squote(part), squote(part), squote(a.remote))
    with open(a.local, "rb") as f:
        rc = subprocess.run(ssh_argv(conn) + [remote], stdin=f).returncode
    print("%s -> %s:%s (%d bytes)%s" % (a.local, a.dev, a.remote, os.path.getsize(a.local), "" if rc == 0 else " FAILED"))
    return rc


def c_pull(a):
    import subprocess
    a.local = local_path(a.local)
    if REMOTE:
        return remote_pull(a)
    conn = authorize(a, True)
    if not conn:
        return 1
    tmp = a.local + ".devhub-part"
    with open(tmp, "wb") as f:
        rc = subprocess.run(ssh_argv(conn) + ["cat %s" % squote(a.remote)], stdout=f).returncode
    if rc == 0:
        os.replace(tmp, a.local)
        print("%s:%s -> %s (%d bytes)" % (a.dev, a.remote, a.local, os.path.getsize(a.local)))
    else:
        os.unlink(tmp)
    return rc


def upload(dev, path):
    """Stream a zip to the hub (no size limit, no JSON wrapping); returns the hub's preview."""
    import http.client
    import urllib.parse
    tok = hub_token()
    try:
        c = hub_conn(600)
        c.putrequest("POST", "/api/devices/%s/upload?name=%s" % (dev, urllib.parse.quote(os.path.basename(path))))
        c.putheader("X-DevHub-Token", tok)
        c.putheader("Content-Type", "application/zip")
        c.putheader("Content-Length", str(os.path.getsize(path)))
        c.endheaders()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                c.send(chunk)
        r = c.getresponse()
        res = json.loads(r.read().decode("utf-8") or "{}")
        c.close()
    except (OSError, http.client.HTTPException) as e:
        raise HubDown(str(e))
    if r.status >= 400:
        res["ok"] = False
    return res


def c_install(a):
    import time
    is_url = a.zip.lower().startswith(("http://", "https://"))
    path = a.zip if is_url else local_path(a.zip)
    if not is_url and not os.path.isfile(path):
        print("no such file: %s" % a.zip, file=sys.stderr)
        return 2
    me = call("POST", "/api/whoami", ident(a))
    if me.get("ok") and a.dev not in (me.get("holds") or []):
        print("take the lock first: devhub acquire %s \"install %s\"" % (a.dev, os.path.basename(path)), file=sys.stderr)
        return 1
    if is_url:      # the device's harbourmaster downloads it (a .zip.md5 URL makes it check the download)
        body = ident(a)
        body.update(dev=a.dev, url=a.zip, refresh=a.refresh)
        return install_watch(a, call("POST", "/api/devices/%s/install" % a.dev, body))
    print("uploading %s (%.1f MB) to DeviceHub..." % (os.path.basename(path), os.path.getsize(path) / 1048576), file=sys.stderr)
    res = upload(a.dev, path)
    if not res.get("ok"):
        return finish(res)
    info = res["info"]
    if info.get("kind") == "squashfs":
        print("%s: runtime (%.1f MB) for PortMaster's libs folder" % (res["name"], res.get("size", 0) / 1048576))
    else:
        print("%s: %s - launchers %s, folders %s, %d files, %.1f MB unpacked" % (
            res["name"], info.get("title") or "?", ", ".join(info.get("launchers") or []) or "none",
            ", ".join(info.get("dirs") or []) or "none", info.get("files", 0), info.get("uncompressed", 0) / 1048576))
    for e in info.get("errors") or []:
        print("error: " + e, file=sys.stderr)
    for w in info.get("warnings") or []:
        print("note: " + w, file=sys.stderr)
    if not res.get("upload"):
        return 1
    body = ident(a)
    body.update(dev=a.dev, upload=res["upload"], refresh=a.refresh)
    return install_watch(a, call("POST", "/api/devices/%s/install" % a.dev, body))


def install_watch(a, r):
    """Follow an install job until it ends, printing its log."""
    import time
    notices(r)
    if not r.get("ok"):
        print(r.get("msg") or r.get("error"), file=sys.stderr)
        return 1
    jid, seen, step, last_pct = r["job"]["id"], 0, None, -1
    while True:
        time.sleep(1)
        job = call("GET", "/api/jobs/%s?lines=500" % jid).get("job") or {}
        lines = job.get("log") or []
        for line in lines[seen:] if len(lines) >= seen else lines:
            if a.verbose or not line.lstrip().startswith("- "):
                print("  " + line)
        seen = len(lines)
        if job.get("step") != step and job.get("state") == "running":
            step = job.get("step")
            print("[%s]" % step)
        if step == "sending" and job.get("total"):
            pct = int(100 * job["sent"] / job["total"])
            if pct // 10 != last_pct // 10:
                print("  sent %d%%" % pct)
                last_pct = pct
        if job.get("state") != "running":
            break
    if job.get("state") == "done":
        print(job.get("result") + (" (note: %s)" % job["warning"] if job.get("warning") else ""))
        return 0
    print("install failed: %s" % job.get("error"), file=sys.stderr)
    return 1


def c_ports(a):
    import time
    r = call("GET", "/api/devices/%s/ports" % a.dev, timeout=150)
    if not r.get("ok"):
        return finish(r)
    if a.json:
        print(json.dumps(r, indent=1))
        return 0
    print("%s - PortMaster %s, ports in %s" % (r["device"], r.get("pm_version") or "?", r["ports_dir"]))
    for p in sorted(r["ports"], key=lambda p: (p.get("title") or p.get("name") or "").lower()):
        rtm = p.get("runtime") or []
        print("  %-30s %-26s %-10s %s%s" % (
            (p.get("title") or p.get("dir") or "?")[:30], (p.get("name") or "")[:26],
            time.strftime("%Y-%m-%d", time.localtime(p["when"])) if p.get("when") else "",
            ("runtime " + ", ".join(str(x) for x in (rtm if isinstance(rtm, list) else [rtm])) + " ") if rtm else "",
            ("MISSING " + ", ".join(p["missing"])) if p.get("missing") else (p.get("error") or "")))
    print("%d ports" % len(r["ports"]))
    if r.get("unknown"):
        print("launchers PortMaster does not manage: " + ", ".join(r["unknown"]))
    if r.get("runtimes"):
        print("runtimes: " + ", ".join("%s (%d MB)" % (n.replace(".squashfs", ""), s // 1048576) for n, s in r["runtimes"]))
    return 0


def c_fe(a):
    body = ident(a)
    body["action"] = a.action
    if a.action != "status":
        print("front end %s on %s (verifying takes ~15 s)..." % (a.action, a.dev), file=sys.stderr)
    return finish(call("POST", "/api/devices/%s/frontend" % a.dev, body, timeout=120))


def c_shot(a):
    import shutil
    r = call("POST", "/api/devices/%s/screenshot" % a.dev, ident(a), timeout=120)
    if not r.get("ok"):
        return finish(r)
    notices(r)
    if REMOTE:     # fetch it from the hub
        out = local_path(a.out) if a.out else r["file"]
        c = hub_conn(60)
        c.request("GET", "/shots/" + r["file"], headers={"X-DevHub-Token": hub_token()})
        resp = c.getresponse()
        data = resp.read()
        if resp.status != 200:
            print("could not fetch the screenshot (HTTP %d)" % resp.status, file=sys.stderr)
            return 1
        with open(out, "wb") as f:
            f.write(data)
        print("%s (%dx%d)" % (out, r["w"], r["h"]))
        return 0
    p = os.path.join(DATA, "shots", r["file"])
    if a.out:
        shutil.copyfile(p, local_path(a.out))
        p = local_path(a.out)
    print("%s (%dx%d)" % (p, r["w"], r["h"]))
    return 0


def c_env(a):
    if REMOTE:
        print("devhub env needs DeviceHub on this machine (its shared SSH sockets); with a remote hub use "
              "devhub run / push / pull", file=sys.stderr)
        return 2
    conn = authorize(a, True)
    if not conn:
        return 1
    ssh = ssh_argv(conn) if not a.wsl else [x for x in ssh_argv(conn) if x not in ("wsl.exe", "-e")]
    vals = [("DH_DEV", a.dev), ("DH_HOST", conn["host"]), ("DH_TARGET", conn["target"]), ("DH_SOCK", conn["socket"]),
            ("DH_SSH", " ".join(ssh)), ("DH_SMB", conn.get("smb") or ""), ("DH_PORTS", conn.get("ports_dir") or "")]
    for k, v in vals:
        print("%s=%s" % (k, squote(v)))
    return 0


def c_log(a):
    import time
    q = "/api/events?n=%d%s" % (a.n, "&dev=" + a.dev if a.dev else "")
    r = call("GET", q)
    for e in r.get("events", []):
        print("%s %-9s %-9s %s" % (time.strftime("%m-%d %H:%M", time.localtime(e["t"])), e.get("dev") or "-",
                                   e["kind"], e["msg"]))
    return 0


def c_name(a):
    body = ident(a)
    body["name"] = a.name
    return finish(call("POST", "/api/session", body))


def c_whoami(a):
    r = call("POST", "/api/whoami", ident(a))
    notices(r)
    c = r.get("caller") or {}
    print("%s%s; holds: %s" % (c.get("name"), " (session %s)" % c["session"][:8] if c.get("session") else " (no Claude session)",
                               ", ".join(r.get("holds") or []) or "nothing"))
    return 0


def url():
    return REMOTE + "/" if REMOTE else "http://127.0.0.1:%d/" % PORT


def c_ui(a):
    import webbrowser
    webbrowser.open(url())
    print(url())
    return 0


def c_start(a):
    import subprocess
    import time
    if REMOTE:
        print("DEVHUB_URL points at %s: start or stop that DeviceHub there" % REMOTE, file=sys.stderr)
        return 3
    try:
        call("GET", "/api/ping", timeout=3)
        print("DeviceHub already running: " + url(), file=sys.stderr)
        return 0
    except HubDown:
        pass
    env_pre = ["env", "DEVHUB_PORT=%d" % PORT, "DEVHUB_DATA=%s" % wsl_path(DATA)]
    server = wsl_path(os.path.join(HERE, "devhubd.py"))
    if IS_WIN:
        flags = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(["wsl.exe", "-e"] + env_pre + ["python3", server], creationflags=flags | 0x01000000,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:   # job does not allow breakaway
            subprocess.Popen(["wsl.exe", "-e"] + env_pre + ["python3", server], creationflags=flags,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        subprocess.Popen(env_pre + ["python3", server], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        time.sleep(0.5)
        try:
            call("GET", "/api/ping", timeout=2)
            print("DeviceHub started: " + url(), file=sys.stderr)
            return 0
        except HubDown:
            pass
    print("DeviceHub did not come up; see %s" % os.path.join(DATA, "devhubd.log"), file=sys.stderr)
    return 3


def c_stop(a):
    if REMOTE:
        return c_start(a)     # says to stop it there
    return finish(call("POST", "/api/shutdown", {}))


HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd")


def c_install_hooks(a):
    """Add DeviceHub's hooks to a Claude Code settings file (user settings by default); old DeviceHub entries are replaced."""
    path = a.settings or os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
    me = os.path.abspath(__file__).replace("\\", "/")
    cmd = '%s "%s" hook' % ("python" if IS_WIN else "python3", me)
    tools = "Bash|PowerShell" if IS_WIN else "Bash"
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    except ValueError as e:
        print("%s is not valid JSON (%s): fix it first" % (path, e), file=sys.stderr)
        return 1
    hooks = cfg.setdefault("hooks", {})
    for ev in HOOK_EVENTS:
        keep = []
        for grp in hooks.get(ev, []):
            hs = [h for h in grp.get("hooks", []) if not ("devhub" in h.get("command", "") and h["command"].endswith(" hook"))]
            if hs:
                keep.append(dict(grp, hooks=hs))
        if not a.remove:
            grp = {"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}
            if ev in ("PreToolUse", "PostToolUse"):
                grp = dict({"matcher": tools}, **grp)
            keep.append(grp)
        if keep:
            hooks[ev] = keep
        else:
            hooks.pop(ev, None)
    if not hooks:
        cfg.pop("hooks")
    text = json.dumps(cfg, indent=2) + "\n"
    if a.print:
        sys.stdout.write(text)
        return 0
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print("%s DeviceHub hooks %s %s; new Claude Code sessions pick them up" %
          ("removed" if a.remove else "wrote", "from" if a.remove else "to", path))
    return 0


def c_agent_guide(a):
    """Print the agent guide: paste it into an LLM's instructions (AGENTS.md, CLAUDE.md, system prompt)."""
    try:
        with open(os.path.join(HERE, "docs", "AGENTS.md"), encoding="utf-8") as f:
            sys.stdout.write(f.read())
        return 0
    except OSError:
        if not REMOTE:
            raise
    c = hub_conn(30)      # devhub downloaded on its own (from the hub): the hub has the guide
    c.request("GET", "/agent-guide.md")
    sys.stdout.write(c.getresponse().read().decode("utf-8", "replace"))
    return 0


def main(argv):
    if argv[:1] == ["hook"]:
        return hook()
    try:   # keep stdout and stderr lines in order when piped
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        pass
    import argparse
    p = argparse.ArgumentParser(prog="devhub", description="DeviceHub: handheld test devices, locks, shared SSH.")
    p.add_argument("--as", dest="as_name", metavar="NAME", help="holder name (default: this Claude session's name)")
    sub = p.add_subparsers(dest="cmd", metavar="command")

    def sp(name, fn, help, **kw):
        s = sub.add_parser(name, help=help, **kw)
        s.add_argument("--as", dest="as_name", metavar="NAME", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
        s.set_defaults(fn=fn)
        return s

    s = sp("status", c_status, "devices, connections and locks")
    s.add_argument("dev", nargs="?")
    s.add_argument("--json", action="store_true")
    s = sp("acquire", c_acquire, "take a device's lock")
    s.add_argument("dev")
    s.add_argument("purpose")
    s.add_argument("--for", dest="for_", metavar="DURATION", help="lease, e.g. 30m (default from settings)")
    s.add_argument("--wait", metavar="DURATION", help="wait in line this long if it is busy")
    s.add_argument("--watch", metavar="NAME", help="keep the lease while a process matching NAME runs on the device")
    s.add_argument("--need", metavar="REASON", help="why you need this device: keeps you waiting for it when others are "
                                                    "free (without it a waiting acquire is moved to a free device)")
    s = sp("renew", c_renew, "keep the lock longer (answers a lock check)")
    s.add_argument("dev")
    s.add_argument("duration", nargs="?")
    s.add_argument("note", nargs="*")
    s.add_argument("--watch", metavar="NAME")
    s = sp("release", c_release, "give the lock back")
    s.add_argument("dev")
    s.add_argument("--dirty", metavar="WHY", help="release although the front end is not running")
    s = sp("run", c_run, "run a command over the shared connection")
    s.add_argument("dev")
    s.add_argument("--ro", action="store_true", help="read-only: allowed without the lock")
    s.add_argument("-t", "--timeout", type=int, default=0)
    s.add_argument("command", nargs=argparse.REMAINDER)
    s = sp("push", c_push, "copy a file to the device")
    s.add_argument("dev")
    s.add_argument("local")
    s.add_argument("remote")
    s = sp("pull", c_pull, "copy a file from the device")
    s.add_argument("dev")
    s.add_argument("remote")
    s.add_argument("local")
    s = sp("install", c_install, "install a port zip with PortMaster's harbourmaster (lock needed)")
    s.add_argument("dev")
    s.add_argument("zip")
    s.add_argument("--refresh", choices=["reload", "restart", "none"], help="game list afterwards (default: reload where the front end can)")
    s.add_argument("-v", "--verbose", action="store_true", help="show every file harbourmaster extracts")
    s = sp("ports", c_ports, "list the ports installed on a device (read-only)")
    s.add_argument("dev")
    s.add_argument("--json", action="store_true")
    s = sp("fe", c_fe, "front end start/stop/restart/status")
    s.add_argument("dev")
    s.add_argument("action", choices=["start", "stop", "restart", "status"])
    s = sp("shot", c_shot, "framebuffer screenshot")
    s.add_argument("dev")
    s.add_argument("out", nargs="?")
    s = sp("env", c_env, "shell variables for scripts")
    s.add_argument("dev")
    s.add_argument("--wsl", action="store_true", help="DH_SSH for use inside WSL")
    s = sp("log", c_log, "recent events")
    s.add_argument("dev", nargs="?")
    s.add_argument("-n", type=int, default=30)
    s = sp("name", c_name, "set this session's display name")
    s.add_argument("name")
    sp("whoami", c_whoami, "who you are to DeviceHub, what you hold")
    sp("ui", c_ui, "open the web UI")
    sp("start", c_start, "start the DeviceHub server")
    sp("stop", c_stop, "stop the DeviceHub server")
    s = sp("install-hooks", c_install_hooks, "add DeviceHub's hooks to Claude Code's settings")
    s.add_argument("--settings", metavar="FILE", help="settings file (default ~/.claude/settings.json; "
                                                       "a project's .claude/settings.json works too)")
    s.add_argument("--print", action="store_true", help="print the result instead of writing it")
    s.add_argument("--remove", action="store_true", help="take DeviceHub's hooks out again")
    sp("agent-guide", c_agent_guide, "print the instructions for an AI agent (AGENTS.md)")
    a = p.parse_args(argv)
    if not getattr(a, "fn", None):
        p.print_help()
        return 2
    if a.fn in (c_install_hooks, c_agent_guide):     # no server needed
        return a.fn(a)
    try:
        return a.fn(a)
    except HubDown as e:
        if REMOTE:
            print("DeviceHub at %s is not reachable (%s)" % (REMOTE, e), file=sys.stderr)
            return 3
        if a.fn in (c_start, c_stop) or os.environ.get("DEVHUB_NO_AUTOSTART"):
            print("DeviceHub is not running (%s). Start it: devhub start" % e, file=sys.stderr)
            return 3
    print("[DeviceHub] server was not running: starting it", file=sys.stderr)
    if c_start(a) != 0:
        return 3
    try:
        return a.fn(a)
    except HubDown as e:
        print("DeviceHub is not reachable (%s)" % e, file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
