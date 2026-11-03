#!/bin/sh
# File manager smoke test: a throwaway DeviceHub and a fake SSH device on localhost (see smoke.sh), then the Files
# API: list, mkdir, upload, rename, chmod, copy, move, delete, and its lock rules for sessions and for the user.
set -e
HERE=$(cd "$(dirname "$0")/.." && pwd)
T=$(mktemp -d)
# free ports, and no hub of the caller's: an agent's DEVHUB_URL/TOKEN would send this test's devhub calls to its real hub
unset DEVHUB_URL DEVHUB_TOKEN DEVHUB_SESSION
free_port() { python3 -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'; }
HUB=$(free_port); DEVPORT=$(free_port)
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

D=$T/dev; mkdir -p "$D/sub" && printf 'hi\n' > "$D/a.txt" && ln -s sub "$D/link" && : > "$D/.hidden"
ui()   { api "$1" "$2" "$(printf '%s' "${3:-{\}}" | sed 's/^{/{"client":"ui",/; s/,}$/}/')"; }
cli()  { api "$1" "$2" "$(printf '%s' "${3:-{\}}" | sed 's/^{/{"client":"cli","session":"files-smoke",/; s/,}$/}/')"; }
ok()   { grep -q '"ok": true' || fail "$*"; }
has()  { python3 -c 'import json,sys; d=json.load(sys.stdin); n={e["name"]:e for e in d["entries"]}; sys.exit(0 if eval(sys.argv[1]) else 1)' "$1"; }
ls_()  { api GET "/api/devices/smoketest/fs?path=$1"; }
tok=$(cat "$DEVHUB_DATA/token")

ls_ "$D" | has 'n["sub"]["dir"] and n["link"]["type"]=="l" and n["link"]["dir"] and n["a.txt"]["size"]==3 and ".hidden" in n' ||
    fail "listing"
ls_ "$D/nope" | grep -q 'does not exist' || fail "missing folder not reported"
ui POST /api/devices/smoketest/fs '{"op":"rm","paths":["/etc"]}' | grep -q "won't change" || fail "top-level delete allowed"
ui POST /api/devices/smoketest/fs '{"op":"mkdir","path":"'"$D"'/new"}' | ok "mkdir"
ui POST /api/devices/smoketest/fs '{"op":"mkdir","path":"'"$D"'/new"}' | grep -q 'already exists' || fail "mkdir over a folder"
python3 - "$D" <<'PY' || fail "upload"
import os, sys, urllib.request
r = urllib.request.Request("http://127.0.0.1:%s/api/devices/smoketest/file?path=%s/new/up%%20load.bin" % (os.environ["DEVHUB_PORT"], sys.argv[1]),
    method="PUT", data=b"x" * 70000, headers={"X-DevHub-Token": open(os.environ["DEVHUB_DATA"] + "/token").read().strip(),
                                             "X-DevHub-Client": "ui"})
assert b'"ok": true' in urllib.request.urlopen(r, timeout=30).read()
PY
[ "$(wc -c < "$D/new/up load.bin")" = 70000 ] || fail "uploaded size"
python3 -c "import urllib.request,sys; r=urllib.request.urlopen('http://127.0.0.1:$HUB/api/devices/smoketest/file?path=$D/a.txt&dl=1&t=$tok'); \
  sys.exit(0 if r.read()==b'hi\n' and 'a.txt' in r.headers['Content-Disposition'] else 1)" || fail "download"
ui POST /api/devices/smoketest/fs '{"op":"rename","path":"'"$D"'/a.txt","to":"'"$D"'/b.txt"}' | ok "rename"
ui POST /api/devices/smoketest/fs '{"op":"chmod","path":"'"$D"'/b.txt","mode":"755"}' | ok "chmod"
[ -x "$D/b.txt" ] || fail "chmod had no effect"
python3 - "$D" <<'PY' || fail "save over a file"
import os, sys, urllib.request
r = urllib.request.Request("http://127.0.0.1:%s/api/devices/smoketest/file?path=%s/b.txt" % (os.environ["DEVHUB_PORT"], sys.argv[1]),
    method="PUT", data=b"edited\n", headers={"X-DevHub-Token": open(os.environ["DEVHUB_DATA"] + "/token").read().strip(),
                                            "X-DevHub-Client": "ui"})
assert b'"ok": true' in urllib.request.urlopen(r, timeout=30).read()
PY
[ -x "$D/b.txt" ] && [ "$(cat "$D/b.txt")" = edited ] || fail "saving over a file lost its mode or content"
ui POST /api/devices/smoketest/fs '{"op":"cp","paths":["'"$D"'/b.txt","'"$D"'/sub"],"to":"'"$D"'/new"}' | ok "copy"
ui POST /api/devices/smoketest/fs '{"op":"cp","paths":["'"$D"'/b.txt"],"to":"'"$D"'/new"}' | grep -q 'already exists' || fail "copy overwrote"
ui POST /api/devices/smoketest/fs '{"op":"cp","paths":["'"$D"'/sub"],"to":"'"$D"'/sub"}' | grep -q 'into itself' || fail "copy into itself"
ui POST /api/devices/smoketest/fs '{"op":"mv","paths":["'"$D"'/b.txt"],"to":"'"$D"'/new","overwrite":true}' | ok "move"
[ ! -e "$D/b.txt" ] && [ -f "$D/new/b.txt" ] && [ -d "$D/new/sub" ] || fail "copy/move result"
ui POST /api/devices/smoketest/fs '{"op":"newfile","path":"'"$D"'/new/empty"}' | ok "new file"

# sessions need the lock; the user must confirm when a session holds it
cli POST /api/devices/smoketest/fs '{"op":"rm","paths":["'"$D"'/new/empty"]}' | grep -q 'take the lock first' || fail "rm without the lock"
cli POST /api/lock/acquire '{"dev":"smoketest","purpose":"files test","ttl":"5m"}' | ok "acquire"
ui POST /api/devices/smoketest/fs '{"op":"rm","paths":["'"$D"'/new/empty"]}' | grep -q '"status": "busy"' || fail "user not warned"
cli POST /api/devices/smoketest/fs '{"op":"rm","paths":["'"$D"'/new/empty"]}' | ok "rm with the lock"
ui POST /api/devices/smoketest/fs '{"op":"rm","paths":["'"$D"'/new"],"force":true}' | ok "user rm with force"
[ ! -e "$D/new" ] || fail "rm left files"
cli POST /api/lock/release '{"dev":"smoketest","dirty":"no front end on the fake device"}' >/dev/null
echo "PASS files ($DEVHUB_PLATFORM)"
