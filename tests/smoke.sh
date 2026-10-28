#!/bin/sh
# End-to-end smoke test on Linux/WSL: a throwaway DeviceHub (own port and data folder) and a fake SSH device on
# localhost. Checks password encryption, the shared connection and acquire/run/release.
# Needs python3 with paramiko (pip install paramiko, or PYTHONPATH pointing at it), ssh, openssl.
#   tests/smoke.sh [linux|wsl]       platform to test (default linux)
set -e
HERE=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d)
HUB=18799; DEVPORT=12299
export DEVHUB_PORT=$HUB DEVHUB_DATA=$T/data DEVHUB_NO_AUTOSTART=1 DEVHUB_PLATFORM=${1:-linux} DEVHUB_NAME=smoke-test
PIDS=
forget() { ssh-keygen -R "[127.0.0.1]:$DEVPORT" >/dev/null 2>&1 || true; }   # the fake device's key is new every run
cleanup() { for p in $PIDS; do kill "$p" 2>/dev/null || true; done; ssh -S "/tmp/smoketest.sock" -O exit x 2>/dev/null || true
            forget; rm -rf "$T"; }
forget
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; tail -n 30 "$T/data/devhubd.log" >&2 2>/dev/null; exit 1; }

head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n' > "$T/pw"
python3 "$HERE/tests/fake_device_sshd.py" $DEVPORT "$T/pw" "$T/hostkey" & PIDS="$PIDS $!"
python3 "$HERE/devhubd.py" > "$T/hub.out" 2>&1 & PIDS="$PIDS $!"
for i in $(seq 40); do
    python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$HUB/api/ping', timeout=1)" 2>/dev/null && break
    sleep 0.5
done

api() {   # METHOD PATH JSON
    python3 - "$@" <<'EOF'
import json, os, sys, urllib.request
m, p, b = sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None
tok = open(os.path.join(os.environ["DEVHUB_DATA"], "token")).read().strip()
r = urllib.request.Request("http://127.0.0.1:%s%s" % (os.environ["DEVHUB_PORT"], p), method=m,
                           data=b.encode() if b else None,
                           headers={"X-DevHub-Token": tok, "Content-Type": "application/json"})
try:
    print(urllib.request.urlopen(r, timeout=30).read().decode())
except urllib.error.HTTPError as e:
    print(e.read().decode())
EOF
}

api GET /api/state | grep -q "\"platform\": \"$DEVHUB_PLATFORM\"" || fail "platform is not $DEVHUB_PLATFORM"
api PUT /api/settings '{"client":"cli","settings":{"away_mode":true}}' | grep -q "only the user" || fail "an agent changed settings"
api POST /api/devices "{\"client\":\"ui\",\"device\":{\"id\":\"smoketest\",\"name\":\"Smoke test\",\"host\":\"127.0.0.1\",
    \"port\":$DEVPORT,\"user\":\"root\",\"auth\":\"password\",\"approval\":\"off\",\"socket\":\"/tmp/smoketest.sock\"}}" |
    grep -q '"ok": true' || fail "device not added"
api POST /api/devices/smoketest/password "{\"client\":\"ui\",\"password\":\"$(cat "$T/pw")\"}" | grep -q '"ok": true' ||
    fail "password not stored"
if [ "$DEVHUB_PLATFORM" = linux ]; then
    grep -q '"openssl"' "$T/data/secrets.json" || fail "password not openssl-encrypted"
    ! grep -q "$(cat "$T/pw")" "$T/data/secrets.json" || fail "password in clear text"
fi

for i in $(seq 40); do
    api GET /api/state | python3 -c 'import json,sys; d=json.load(sys.stdin); v=[x for x in d["devices"] if x["id"]=="smoketest"][0]; sys.exit(0 if v["rt"]["master"].get("state")=="up" else 1)' 2>/dev/null && break
    [ "$i" = 40 ] && fail "shared connection did not come up"
    sleep 0.5
done

DH="python3 $HERE/devhub.py"
$DH run smoketest -- 'echo nope' 2>&1 | grep -q "take the lock first" || fail "run without the lock was allowed"
$DH acquire smoketest "smoke test" --for 5m | grep -q "is yours" || fail "acquire"
[ "$($DH run smoketest -- 'echo hello-from-device')" = hello-from-device ] || fail "run"
$DH run smoketest --ro -- 'true' || fail "read-only run"
$DH release smoketest --dirty "no front end on the fake device" >/dev/null || fail "release"
api GET /api/state | python3 -c 'import json,sys; d=json.load(sys.stdin); v=[x for x in d["devices"] if x["id"]=="smoketest"][0]; sys.exit(0 if not v.get("lock") else 1)' ||
    fail "device not free after release"
echo "PASS ($DEVHUB_PLATFORM)"
