#!/usr/bin/env python3
"""DeviceHub server mode: one hub on a LAN server (a container) instead of on your own PC.

Everything devhubd.py does stays (locks, approvals, queues, the shared SSH connections, installs, screenshots).
On top of it:
  - Handhelds announce themselves: the DeviceHub Beacon port (beacon/) tells the hub "I'm up at this IP", signed with
    the hub's beacon key. New ones wait for your approval; approved ones get the hub's SSH key in their
    authorized_keys and the hub connects back to them. Their SSH host keys come signed in the announce and are
    pinned, and an IP change from DHCP follows the device by itself.
  - Login: the web page needs DEVHUB_PASSWORD, or a sign-in in front of the hub (see below); only a logged-in page
    acts as the user (approvals, settings, Away mode). Agents use the API token (X-DevHub-Token), as before.
  - Sign-in in front of the hub (a reverse proxy with forward auth: Authelia, Authentik, oauth2-proxy...): requests
    through the proxy carry the person in headers (Remote-User/Remote-Groups) and a secret the proxy adds. Those
    headers count only when the secret matches DEVHUB_SSO_SECRET (anything reaching the port directly can send fake
    ones); then members of DEVHUB_SSO_GROUP are logged in. With DEVHUB_SSO_URL set, the port itself serves only the
    handhelds' and agents' API: browser pages there are sent to the signed-in address, and a password login there
    doesn't count. docs/SSO.md: what a proxy has to do, and the setup for some.
  - It listens on DEVHUB_BIND (0.0.0.0) instead of loopback only.

Environment (besides devhubd's DEVHUB_PORT/DEVHUB_DATA):
  DEVHUB_PASSWORD     the web page's login password (required unless DEVHUB_SSO_SECRET is set)
  DEVHUB_SSO_SECRET   the secret the sign-in proxy adds to every signed-in request, 16 characters or more (or
                      DEVHUB_SSO_SECRET_FILE: a file holding it, e.g. a Docker secret)
  DEVHUB_SSO_SECRET_HEADER  the header it comes in (default X-DevHub-SSO)
  DEVHUB_SSO_USER_HEADER, DEVHUB_SSO_GROUPS_HEADER, DEVHUB_SSO_NAME_HEADER  the person's headers (default Remote-User,
                      Remote-Groups: comma or | separated, Remote-Name: shown on the page)
  DEVHUB_SSO_GROUP    the group whose signed-in members are logged in (default: admins)
  DEVHUB_SSO_IGNORE_USERS  users that are never people (scripts' keys), space-separated patterns, e.g. svc-*
  DEVHUB_SSO_URL      the hub's signed-in address, e.g. https://devicehub.example.net: browser pages on the port go there
  DEVHUB_SSO_LOGOUT   where Log out goes after a sign-in, e.g. https://auth.example.net/logout
  DEVHUB_PUBLIC_URL   the URL handhelds announce to, e.g. http://192.168.68.133:8765 (default: what the browser used)
  DEVHUB_HUB_NAME     the hub's name in the beacon's messages (default: DeviceHub)
"""
import fnmatch, hashlib, hmac, html, io, json, os, re, secrets, subprocess, threading, time, urllib.parse, zipfile

os.environ.setdefault("DEVHUB_BIND", "0.0.0.0")
os.environ.setdefault("DEVHUB_PLATFORM", "linux")
import devhubd as D   # noqa: E402  (after the environment defaults above)
from devhubd import H, ApiError, route, now, event, alert, log   # noqa: E402

PASSWORD = os.environ.get("DEVHUB_PASSWORD") or ""
PUBLIC_URL = (os.environ.get("DEVHUB_PUBLIC_URL") or "").rstrip("/")
HUB_NAME = os.environ.get("DEVHUB_HUB_NAME") or "DeviceHub"


def setting(name):
    return (os.environ.get(name) or "").strip()


def sso_secret():
    """(the secret, why there's none): DEVHUB_SSO_SECRET, else the file DEVHUB_SSO_SECRET_FILE names."""
    if setting("DEVHUB_SSO_SECRET"):
        return setting("DEVHUB_SSO_SECRET"), ""
    path = setting("DEVHUB_SSO_SECRET_FILE")
    if not path:
        return "", ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip(), ""
    except OSError as e:
        return "", "can't read DEVHUB_SSO_SECRET_FILE: %s" % e


SSO_SECRET, SSO_SECRET_PROBLEM = sso_secret()
if SSO_SECRET and len(SSO_SECRET) < 16:
    SSO_SECRET, SSO_SECRET_PROBLEM = "", "the secret is shorter than 16 characters: ignored"
SSO_HEADER = setting("DEVHUB_SSO_SECRET_HEADER") or "X-DevHub-SSO"
SSO_USER_HEADER = setting("DEVHUB_SSO_USER_HEADER") or "Remote-User"
SSO_GROUPS_HEADER = setting("DEVHUB_SSO_GROUPS_HEADER") or "Remote-Groups"
SSO_NAME_HEADER = setting("DEVHUB_SSO_NAME_HEADER") or "Remote-Name"
SSO_GROUP = setting("DEVHUB_SSO_GROUP") or "admins"
SSO_IGNORE = setting("DEVHUB_SSO_IGNORE_USERS").split()
SSO_URL = setting("DEVHUB_SSO_URL").rstrip("/") if SSO_SECRET else ""
SSO_LOGOUT = setting("DEVHUB_SSO_LOGOUT")
SESSION_DAYS = 30
BEACON_DIR = os.path.join(D.HERE, "beacon")
OPEN_PATHS = {"/api/ping", "/api/beacon/hello", "/api/beacon", "/login", "/sw.js"}
# what devices and agents fetch from the port without a token; with DEVHUB_SSO_URL every other open path is a page
DEVICE_PATHS = {"/api/ping", "/api/beacon/hello", "/api/beacon", "/devhub.py", "/agent-guide.md"}

D.DEFAULT_SETTINGS["beacon_auto_approve"] = False   # new handhelds join without asking (trusted LAN)
D.DEFAULT_SETTINGS["poll_devices"] = False          # probe every device and connect when it answers, like local DeviceHub
for _k in ("beacon_auto_approve", "poll_devices"):
    H.settings.setdefault(_k, False)
H.beacons = {}      # beacon id -> record (beacons.json)
H.nonces = {}       # nonce -> expiry
H.login_fails = []
H.sso_logged = set()


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


def sso(handler):
    """(through the sign-in, username, logged in) from the proxy's headers. The person's headers count only beside the
    proxy's secret; DEVHUB_SSO_IGNORE_USERS are never people; only members of SSO_GROUP are logged in. A request that
    looks like it came through a sign-in that isn't set up right logs why once (sso_problem), and the login page says
    only that it isn't set up right."""
    D.REQ.sso_problem = ""
    got = handler.headers.get(SSO_HEADER) or ""
    user = (handler.headers.get(SSO_USER_HEADER) or "").strip()
    if not SSO_SECRET:
        if got or user:
            sso_problem("the sign-in sent %s, but the hub has no DEVHUB_SSO_SECRET%s (docs/SSO.md)" % (
                SSO_HEADER if got else SSO_USER_HEADER, ": " + SSO_SECRET_PROBLEM if SSO_SECRET_PROBLEM else ""))
        return False, "", False
    if not hmac.compare_digest(got.encode(), SSO_SECRET.encode()):
        if got:
            sso_problem("%s doesn't match DEVHUB_SSO_SECRET: the proxy and the hub have different secrets (the hub "
                        "reads its settings when its container is made: recreate it after changing them)" % SSO_HEADER)
        elif user:
            sso_problem("the sign-in sent %s but no %s: the proxy must add the secret header (docs/SSO.md)" % (
                SSO_USER_HEADER, SSO_HEADER))
        return False, "", False
    groups = {g.strip() for g in re.split(r"[,|]", handler.headers.get(SSO_GROUPS_HEADER) or "") if g.strip()}
    if not user:
        sso_problem("the sign-in passed no %s: the proxy must copy it from the sign-in's answer" % SSO_USER_HEADER)
    elif not groups:
        sso_problem("the sign-in passed no %s for %s: the proxy must copy it from the sign-in's answer" % (SSO_GROUPS_HEADER, user))
    person = bool(user) and not any(fnmatch.fnmatchcase(user, pat) for pat in SSO_IGNORE)
    return True, user, person and SSO_GROUP in groups


def sso_problem(why):
    D.REQ.sso_problem = why
    if why not in H.sso_logged:     # once per kind of problem, not per request
        H.sso_logged.add(why)
        log("sign-in:", why)


def cross_site(handler, method):
    """A browser request made by another site's page (another name under the same domain is the same site, so SameSite cookies
    and the sign-in still come along): never acts as the user, except to read."""
    return method not in ("GET", "HEAD") and (handler.headers.get("Sec-Fetch-Site") or "") in ("same-site", "cross-site")


def header_text(handler, name):
    """A header's text: http.server decodes headers as Latin-1, a sign-in sends UTF-8 (a name like "Zoë")."""
    value = (handler.headers.get(name) or "").strip()
    try:
        return value.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return value


def gate(handler, method, path, q, auth):
    via_sso, user, sso_in = sso(handler)
    locked = bool(SSO_URL) and not via_sso     # the port itself, while the hub has a signed-in address
    logged_in = (sso_in or (not locked and session_ok(handler))) and not cross_site(handler, method)
    D.REQ.ui = logged_in
    D.REQ.sso_user = user if sso_in else ""
    D.REQ.sso_name = (header_text(handler, SSO_NAME_HEADER) or user) if sso_in else ""
    D.REQ.sso_other = user if via_sso and not sso_in else ""
    if locked and not auth and path not in DEVICE_PATHS and not D.SERVED_RE.match(path):
        if method in ("GET", "HEAD"):
            u = urllib.parse.urlsplit(handler.path)
            return (302, "text/plain", b"", {"Location": SSO_URL + u.path + ("?" + u.query if u.query else "")})
        raise ApiError(403, "sign in at %s" % SSO_URL)
    if path in OPEN_PATHS:
        return None
    if path == "/" and not logged_in:
        return (302, "text/plain", b"", {"Location": "/login"})
    if auth and not logged_in:
        tok = handler.headers.get("X-DevHub-Token") or (q.get("t") or [""])[0]
        if not D.token_ok(tok):     # the master token, or an agent's (which never acts as the user)
            raise ApiError(401, "log in, or send an API token (X-DevHub-Token)")
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


def login_hint():
    if getattr(D.REQ, "sso_problem", ""):     # what's wrong goes to the log, never to the page
        return "The sign-in in front of DeviceHub isn't set up right: the hub's log says why"
    other = getattr(D.REQ, "sso_other", "")
    if other:
        return "Signed in as %s, who isn't in %s%s" % (other, SSO_GROUP, ": log in with the hub's password" if PASSWORD else "")
    return "" if PASSWORD else "DEVHUB_PASSWORD is not set on the server: nobody can log in here"


@route("GET", "/login", auth=False)
def r_login_page(body, q):
    if D.REQ.ui:
        return redirect("/")
    return login_page(login_hint())


@route("POST", "/login", auth=False, raw=True)
def r_login(h, q):
    n = int(h.headers.get("Content-Length") or 0)
    form = urllib.parse.parse_qs(h.rfile.read(min(n, 4096)).decode("utf-8", "replace"))
    H.login_fails[:] = [t for t in H.login_fails if now() - t < 60]
    if len(H.login_fails) >= 10:
        return login_page("Too many tries: wait a minute")
    if cross_site(h, "POST"):
        return login_page("Log in from this page")
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
    facts = {k: kv.get(k, "") for k in ("name", "cfw", "cfw_version", "hostname", "mac", "ssh", "autostart", "ssid",
                                        "width", "height", "ram", "cpu", "arch", "kernel")}
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
        if b.get("want_autostart") in ("1", "0"):     # the user's choice on the page; the beacon (un)hooks itself
            out.append(("autostart", b["want_autostart"]))
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
                                   "autostart", "want_autostart", "ssid", "width", "height", "first", "last")}
            for b in H.beacons.values()]


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


@route("POST", r"/api/beacons(?:/([0-9a-f]{8,32}))?/autostart")
def r_beacon_autostart(body, q, bid=None):
    """The user switches a device's boot autostart on or off (one beacon, or every approved one without an id). The
    beacon gets the choice in its next announce's signed answer (within a minute) and hooks or unhooks itself; on,
    it only starts on the network it was on then. Agents can't: it's the user's setting."""
    c = D.caller(body)
    if not c.get("ui"):
        raise ApiError(403, "only the user switches autostart (DeviceHub page)")
    on = body.get("on")
    want = "1" if on is True else "0" if on is False else ""
    with H.lock:
        if bid:
            b = H.beacons.get(bid)
            if not b:
                raise ApiError(404, "no such beacon")
            todo = [b]
        else:
            todo = [b for b in H.beacons.values() if b.get("state") == "approved"]
        for b in todo:
            b["want_autostart"] = want
        save_beacons()
    what = {"1": "on", "0": "off", "": "left to the device"}[want]
    event(None, "beacon", "autostart %s for %s (applies on its next announce)" % (
        what, (todo[0].get("name") or bid) if bid else "every device"), c["name"])
    return {"ok": True, "count": len(todo)}


# /api/state carries the beacons (and that this is a server) for the page
_r_state = next(fn for m, rx, fn, a, raw in D.ROUTES if m == "GET" and rx.pattern == "^/api/state$")


def r_state(body, q):
    res = _r_state(body, q)
    with H.lock:
        res["server"] = {"hub": HUB_NAME, "beacons": beacons_public(), "public_url": PUBLIC_URL,
                         "user": getattr(D.REQ, "sso_user", ""), "name": getattr(D.REQ, "sso_name", ""),
                         "logout": SSO_LOGOUT if getattr(D.REQ, "sso_user", "") else ""}
    return res


D.ROUTES[:] = [(m, rx, r_state if fn is _r_state else fn, a, raw) for m, rx, fn, a, raw in D.ROUTES]


# ---------------------------------------------------------------- no blind polling: devices are reached only when they ask
# Local DeviceHub probes every device's SSH port every 10 s and connects whenever it answers. Here devices come and go,
# so the hub only reaches out to a device that announced itself within ANNOUNCE_LEASE_S (the beacon re-announces every
# minute), or for a minute after you press Connect (devices added by hand). An open connection is kept while it lives.
# Settings > poll_devices brings back the local behaviour.

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
    if (H.settings.get("poll_devices") or not d["enabled"] or now() - last_announce(dev) < ANNOUNCE_LEASE_S or now() < r.get("connect_until", 0)
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
    if not PUBLIC_URL and getattr(D.REQ, "sso_user", ""):
        raise ApiError(409, "set DEVHUB_PUBLIC_URL (the hub's LAN address): the beacon can't announce through the sign-in")
    url = PUBLIC_URL or "http://%s" % (h.headers.get("Host") or "")
    autostart = (q.get("autostart") or ["0"])[0] == "1"
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
        with open(os.path.join(BEACON_DIR, "beacon"), "rb") as f:
            add("devicehub/beacon", f.read(), 0o755)     # no .sh in a port's folder: run as `sh beacon`
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
    if not PASSWORD and not SSO_SECRET:
        print("devhubsrv: set DEVHUB_PASSWORD (the web page's login) or DEVHUB_SSO_SECRET (a sign-in in front of it); "
              "refusing to serve the LAN without one")
        raise SystemExit(1)
    os.makedirs(D.DATA, exist_ok=True)
    if SSO_SECRET:
        log("sign-in: on (secret in %s, person in %s/%s; %s are logged in)%s%s" % (
            SSO_HEADER, SSO_USER_HEADER, SSO_GROUPS_HEADER, SSO_GROUP, "; the port sends pages to " + SSO_URL if SSO_URL else "; the port keeps the password login",
            "" if PUBLIC_URL else "; set DEVHUB_PUBLIC_URL (the LAN address handhelds announce to): beacon downloads need it"))
    else:
        log("sign-in: off (%s)%s" % (SSO_SECRET_PROBLEM or "no DEVHUB_SSO_SECRET",
                                      "; DEVHUB_SSO_URL is ignored" if os.environ.get("DEVHUB_SSO_URL") else ""))
    D.BEACON_KEY = secret_file("beacon.key", 32)
    ensure_hub_key()
    H.beacons = D.load_json("beacons.json", {})
    D.GATE = gate
    D.main()


if __name__ == "__main__":
    main()
