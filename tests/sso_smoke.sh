#!/bin/sh
# Server mode behind a sign-in proxy (docs/SSO.md): the person's headers count only beside the proxy's secret, only
# DEVHUB_SSO_GROUP is logged in, and with DEVHUB_SSO_URL the port serves only devices and agents; the header names and
# where the secret comes from can be changed. Needs python3 and curl.
#   tests/sso_smoke.sh
set -e
HERE=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3}
T=$(mktemp -d)
for v in $(env | sed -n 's/^\(DEVHUB_[A-Z_]*\)=.*/\1/p'); do unset "$v"; done   # the caller's hub and settings
free_port() { "$PY" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }
PIDS=
cleanup() { for p in $PIDS; do kill "$p" 2>/dev/null || true; done; rm -rf "$T"; }
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; tail -n 30 "$T"/*/devhubd.log >&2 2>/dev/null; exit 1; }
ok() { echo "ok: $*"; }
SECRET=0123456789abcdef0123456789abcdef

start_hub() {   # name port [VAR=value...]
    n=$1; p=$2; shift 2
    env DEVHUB_PORT=$p DEVHUB_FILE_PORT=$(free_port) DEVHUB_DATA=$T/$n DEVHUB_NO_AUTOSTART=1 DEVHUB_BIND=127.0.0.1 \
        DEVHUB_PUBLIC_URL=http://127.0.0.1:$p DEVHUB_SSO_SECRET=$SECRET DEVHUB_SSO_IGNORE_USERS="svc-* bot-*" "$@" \
        "$PY" "$HERE/devhubsrv.py" > "$T/$n.out" 2>&1 &
    PIDS="$PIDS $!"
    for i in $(seq 40); do curl -fs "http://127.0.0.1:$p/api/ping" >/dev/null 2>&1 && return; sleep 0.5; done
    cat "$T/$n.out"; fail "hub $n did not start"
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
ADMIN="-H X-DevHub-SSO:$SECRET -H Remote-User:klops -H Remote-Groups:family,admins"

# --- hub A: no password, a signed-in address: the port is for devices and agents only
A=$(free_port)
start_hub a "$A" DEVHUB_SSO_URL=https://devicehub.example.net/ DEVHUB_SSO_LOGOUT=https://auth.example.net/logout
U=http://127.0.0.1:$A
TOK=$(cat "$T/a/token")

loc=$(curl -s -o /dev/null -w '%{redirect_url}' "$U/proxy/wiki/a%20b?x=1&y=2")
[ "$loc" = "https://devicehub.example.net/proxy/wiki/a%20b?x=1&y=2" ] || fail "page on the port goes to the name: $loc"
[ "$(code "$U/")" = 302 ] && [ "$(code "$U/login")" = 302 ] || fail "/ and /login on the port"
[ "$(code -d password=x "$U/login")" = 403 ] || fail "a password login on the port"
[ "$(code "$U/api/ping")" = 200 ] && [ "$(code "$U/api/beacon/hello")" = 200 ] && [ "$(code "$U/devhub.py")" = 200 ] ||
    fail "the devices' and agents' open paths on the port"
[ "$(code -H "X-DevHub-Token: $TOK" "$U/api/state")" = 200 ] || fail "the token on the port"
[ "$(code "$U/dl/0123456789abcdef0123456789abcdef/x.zip")" = 404 ] || fail "served files aren't sent to the name"
ok "the port: devices and agents only"

[ "$(code -H Remote-User:klops -H Remote-Groups:admins "$U/api/state")" = 401 ] || fail "made-up Remote-User without the secret"
[ "$(code -H X-DevHub-SSO:wrong -H Remote-User:klops -H Remote-Groups:admins "$U/api/state")" = 401 ] || fail "a wrong secret"
[ "$(code -H X-DevHub-SSO:wrong -H Remote-User:klops -H Remote-Groups:admins "$U/")" = 302 ] || fail "a wrong secret's page"
ok "the person's headers only with the secret"

[ "$(code $ADMIN "$U/")" = 200 ] || fail "an admin's page"
curl -s $ADMIN -H "Remote-Name: Zoë" "$U/api/state" | "$PY" -c '
import json, sys
s = json.load(sys.stdin)["server"]
assert (s["user"], s["name"], s["logout"]) == ("klops", "Zoë", "https://auth.example.net/logout"), s' ||
    fail "the page doesn't know who signed in"
curl -s $ADMIN "$U/api/state" | "$PY" -c 'import json, sys; assert json.load(sys.stdin)["server"]["name"] == "klops"' ||
    fail "no name: the username"
curl -s $ADMIN -X PUT -d '{"client":"ui","settings":{"away_mode":true}}' "$U/api/settings" | grep -q '"ok": true' || fail "an admin acts as the user"
[ "$(code $ADMIN "$U/login")" = 303 ] || fail "/login for a signed-in admin"
curl -s $ADMIN -H "Sec-Fetch-Site: same-site" -X PUT -d '{"client":"ui","settings":{"away_mode":false}}' "$U/api/settings" |
    grep -q "log in" || fail "another site's page acted as the user"
curl -s $ADMIN -H "Sec-Fetch-Site: same-origin" -X PUT -d '{"client":"ui","settings":{"away_mode":false}}' "$U/api/settings" |
    grep -q '"ok": true' || fail "the page itself (same-origin)"
ok "admins are logged in"

FAMILY="-H X-DevHub-SSO:$SECRET -H Remote-User:kid -H Remote-Groups:family"
[ "$(code $FAMILY "$U/")" = 302 ] || fail "a signed-in non-admin's page"
curl -s $FAMILY "$U/login" | grep -q "Signed in as kid, who isn&#x27;t in admins" || fail "the login page doesn't say why"
curl -s $FAMILY -X PUT -d '{"client":"ui","settings":{"away_mode":true}}' "$U/api/settings" | grep -q "log in" || fail "a non-admin acted as the user"
[ "$(code -H X-DevHub-SSO:$SECRET -H Remote-User:svc-tavern -H Remote-Groups:admins,services "$U/")" = 302 ] ||
    fail "a service key is a person"
[ "$(code -H X-DevHub-SSO:$SECRET -H Remote-User:bot-x -H Remote-Groups:admins "$U/")" = 302 ] || fail "the second ignore pattern"
[ "$(code -H X-DevHub-SSO:$SECRET -H Remote-Groups:admins "$U/")" = 302 ] || fail "admins without a user"
ok "nobody else"

# --- hub B: a password too, no signed-in address: the port keeps the password login, the sign-in works beside it
B=$(free_port)
start_hub b "$B" DEVHUB_PASSWORD=pw DEVHUB_SSO_GROUP=hub
U=http://127.0.0.1:$B
C="curl -s -b $T/jar -c $T/jar"
[ "$(code "$U/proxy/wiki/")" != 302 ] || fail "hub B sends pages away"
[ "$($C -o /dev/null -w '%{http_code}' -d password=pw "$U/login")" = 303 ] || fail "password login on the port"
$C -X PUT -d '{"client":"ui","settings":{"away_mode":true}}' "$U/api/settings" | grep -q '"ok": true' || fail "the password's session"
$C -H "Sec-Fetch-Site: cross-site" -X PUT -d '{"client":"ui","settings":{"away_mode":true}}' "$U/api/settings" | grep -q "log in" ||
    fail "another site's page used the session"
[ "$(code $ADMIN "$U/")" = 302 ] || fail "DEVHUB_SSO_GROUP=hub let admins in"
[ "$(code -H X-DevHub-SSO:$SECRET -H Remote-User:klops -H Remote-Groups:hub "$U/")" = 200 ] || fail "DEVHUB_SSO_GROUP"
ok "password beside the sign-in, DEVHUB_SSO_GROUP"

# --- a sign-in that isn't set up right: the log says why (once), the page only that it isn't
for h in "-H X-DevHub-SSO:stale -H Remote-User:klops" "-H Remote-User:klops -H Remote-Groups:admins" "-H X-DevHub-SSO:$SECRET -H Remote-User:klops"; do
    page=$(curl -s $h "$U/login")
    echo "$page" | grep -q "isn&#x27;t set up right: the hub&#x27;s log says why" || fail "the page doesn't say it isn't set up ($h)"
    echo "$page" | grep -q "DEVHUB_SSO_SECRET\|X-DevHub-SSO" && fail "the page shows the setup's details ($h)"
done
curl -s -H X-DevHub-SSO:stale -H Remote-User:klops "$U/login" > /dev/null
grep -q "sign-in: on (secret in X-DevHub-SSO, person in Remote-User/Remote-Groups; hub are logged in); the port keeps the password login" "$T/b/devhubd.log" ||
    fail "startup doesn't say the sign-in is on"
[ "$(grep -c "X-DevHub-SSO doesn't match DEVHUB_SSO_SECRET" "$T/b/devhubd.log")" = 1 ] || fail "a stale secret isn't logged, once"
grep -q "the sign-in sent Remote-User but no X-DevHub-SSO: the proxy must add the secret header" "$T/b/devhubd.log" || fail "a missing header isn't logged"
grep -q "the sign-in passed no Remote-Groups for klops" "$T/b/devhubd.log" || fail "missing groups aren't logged"
D=$(free_port)
start_hub d "$D" DEVHUB_SSO_SECRET= DEVHUB_PASSWORD=pw DEVHUB_SSO_URL=https://x.example.net
curl -s -H X-DevHub-SSO:whatever -H Remote-User:klops "http://127.0.0.1:$D/login" | grep -q "isn&#x27;t set up right" || fail "no secret: the page"
grep -q "the sign-in sent X-DevHub-SSO, but the hub has no DEVHUB_SSO_SECRET" "$T/d/devhubd.log" || fail "no secret isn't logged"
grep -q "sign-in: off (no DEVHUB_SSO_SECRET); DEVHUB_SSO_URL is ignored" "$T/d/devhubd.log" || fail "startup doesn't say the sign-in is off"
[ "$(curl -s -o /dev/null -w '%{redirect_url}' "http://127.0.0.1:$D/")" = "http://127.0.0.1:$D/login" ] ||
    fail "no secret: DEVHUB_SSO_URL must not lock the port"
E=$(free_port)
start_hub e "$E" DEVHUB_SSO_SECRET=short DEVHUB_PASSWORD=pw
[ "$(code -H X-DevHub-SSO:short -H Remote-User:klops -H Remote-Groups:admins "http://127.0.0.1:$E/")" = 302 ] || fail "a short secret is used"
grep -q "sign-in: off (the secret is shorter than 16 characters: ignored)" "$T/e/devhubd.log" || fail "a short secret isn't logged"
ok "a sign-in that isn't set up right: logged, not shown"

# --- another proxy's names (Authentik's headers, groups split by |), a secret from a file
F=$(free_port)
echo "  $SECRET-from-a-file  " > "$T/secret"
start_hub f "$F" DEVHUB_SSO_SECRET= DEVHUB_SSO_SECRET_FILE="$T/secret" DEVHUB_SSO_SECRET_HEADER=X-Proxy-Auth \
    DEVHUB_SSO_USER_HEADER=X-authentik-username DEVHUB_SSO_GROUPS_HEADER=X-authentik-groups \
    DEVHUB_SSO_NAME_HEADER=X-authentik-name DEVHUB_SSO_GROUP="authentik Admins"
U=http://127.0.0.1:$F
AUTHENTIK="-H X-authentik-username:klops -H X-authentik-name:Klops"
[ "$(code -H "X-Proxy-Auth: $SECRET-from-a-file" $AUTHENTIK -H "X-authentik-groups: family|authentik Admins" "$U/")" = 200 ] ||
    fail "a secret file, another header, other user/group headers"
curl -s -H "X-Proxy-Auth: $SECRET-from-a-file" $AUTHENTIK -H "X-authentik-groups: authentik Admins" "$U/api/state" |
    "$PY" -c 'import json, sys; assert json.load(sys.stdin)["server"]["name"] == "Klops"' || fail "another name header"
[ "$(code -H "X-DevHub-SSO: $SECRET-from-a-file" $AUTHENTIK -H "X-authentik-groups: authentik Admins" "$U/")" = 302 ] ||
    fail "the default secret header still counts"
[ "$(code -H "X-Proxy-Auth: $SECRET-from-a-file" -H "Remote-User: klops" -H "Remote-Groups: authentik Admins" "$U/")" = 302 ] ||
    fail "the default person headers still count"
ok "another proxy's headers, a secret file"

# --- neither a password nor a secret: refuses to start
if env DEVHUB_PORT=$(free_port) DEVHUB_DATA=$T/c DEVHUB_NO_AUTOSTART=1 timeout 10 "$PY" "$HERE/devhubsrv.py" > "$T/c.out" 2>&1; then
    fail "started without a password or a sign-in"
fi
grep -q "refusing to serve" "$T/c.out" || fail "no reason given"
ok "needs a password or a sign-in"
echo "all sso checks passed"
