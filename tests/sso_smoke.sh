#!/bin/sh
# Server mode behind a sign-in proxy (Authelia forward-auth, netwatch's sso_header): the Remote-* headers count only
# with the proxy's secret, only the admins group is logged in, and with DEVHUB_SSO_URL the port serves only devices and
# agents. Needs python3 and curl.
#   tests/sso_smoke.sh
set -e
HERE=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3}
T=$(mktemp -d)
unset DEVHUB_URL DEVHUB_TOKEN DEVHUB_SESSION DEVHUB_PASSWORD DEVHUB_SSO_URL DEVHUB_SSO_LOGOUT DEVHUB_SSO_GROUP NETWATCH_SSO_HEADER
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
        DEVHUB_PUBLIC_URL=http://127.0.0.1:$p NETWATCH_SSO_SECRET=$SECRET "$@" "$PY" "$HERE/devhubsrv.py" > "$T/$n.out" 2>&1 &
    PIDS="$PIDS $!"
    for i in $(seq 40); do curl -fs "http://127.0.0.1:$p/api/ping" >/dev/null 2>&1 && return; sleep 0.5; done
    cat "$T/$n.out"; fail "hub $n did not start"
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }
ADMIN="-H X-Netwatch-Auth:$SECRET -H Remote-User:klops -H Remote-Groups:family,admins"

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
[ "$(code -H X-Netwatch-Auth:wrong -H Remote-User:klops -H Remote-Groups:admins "$U/api/state")" = 401 ] || fail "a wrong secret"
[ "$(code -H X-Netwatch-Auth:wrong -H Remote-User:klops -H Remote-Groups:admins "$U/")" = 302 ] || fail "a wrong secret's page"
ok "Remote-* only with the secret"

[ "$(code $ADMIN "$U/")" = 200 ] || fail "an admin's page"
curl -s $ADMIN "$U/api/state" | "$PY" -c 'import json, sys; s = json.load(sys.stdin)["server"]; assert s["user"] == "klops" and s["logout"] == "https://auth.example.net/logout", s' ||
    fail "the page doesn't know who signed in"
curl -s $ADMIN -X PUT -d '{"client":"ui","settings":{"away_mode":true}}' "$U/api/settings" | grep -q '"ok": true' || fail "an admin acts as the user"
[ "$(code $ADMIN "$U/login")" = 303 ] || fail "/login for a signed-in admin"
curl -s $ADMIN -H "Sec-Fetch-Site: same-site" -X PUT -d '{"client":"ui","settings":{"away_mode":false}}' "$U/api/settings" |
    grep -q "log in" || fail "another site's page acted as the user"
curl -s $ADMIN -H "Sec-Fetch-Site: same-origin" -X PUT -d '{"client":"ui","settings":{"away_mode":false}}' "$U/api/settings" |
    grep -q '"ok": true' || fail "the page itself (same-origin)"
ok "admins are logged in"

FAMILY="-H X-Netwatch-Auth:$SECRET -H Remote-User:kid -H Remote-Groups:family"
[ "$(code $FAMILY "$U/")" = 302 ] || fail "a signed-in non-admin's page"
curl -s $FAMILY "$U/login" | grep -q "Signed in as kid, who isn&#x27;t in admins" || fail "the login page doesn't say why"
curl -s $FAMILY -X PUT -d '{"client":"ui","settings":{"away_mode":true}}' "$U/api/settings" | grep -q "log in" || fail "a non-admin acted as the user"
[ "$(code -H X-Netwatch-Auth:$SECRET -H Remote-User:svc-tavern -H Remote-Groups:admins,services "$U/")" = 302 ] ||
    fail "a service key is a person"
[ "$(code -H X-Netwatch-Auth:$SECRET -H Remote-Groups:admins "$U/")" = 302 ] || fail "admins without a user"
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
[ "$(code -H X-Netwatch-Auth:$SECRET -H Remote-User:klops -H Remote-Groups:hub "$U/")" = 200 ] || fail "DEVHUB_SSO_GROUP"
ok "password beside the sign-in, DEVHUB_SSO_GROUP"

# --- neither a password nor a secret: refuses to start
if env -u NETWATCH_SSO_SECRET DEVHUB_PORT=$(free_port) DEVHUB_DATA=$T/c DEVHUB_NO_AUTOSTART=1 timeout 10 "$PY" "$HERE/devhubsrv.py" > "$T/c.out" 2>&1; then
    fail "started without a password or a sign-in"
fi
grep -q "refusing to serve" "$T/c.out" || fail "no reason given"
ok "needs a password or a sign-in"
echo "all sso checks passed"
