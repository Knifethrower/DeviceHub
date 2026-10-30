#!/bin/sh
# End-to-end test of server mode (devhubsrv.py) on localhost: login, the beacon port, an announce that waits for
# approval, the hub's SSH key landing on a fake device, the hub connecting with it, and an agent running a command.
# Needs python3 with paramiko (PYTHON=/path/to/venv/python), ssh, ssh-keygen, busybox (the beacon runs in busybox sh
# with busybox wget, like on a handheld without curl).
#   tests/server_smoke.sh
set -e
HERE=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON:-python3}
T=$(mktemp -d)
HUB=18798; DEVPORT=12298
export DEVHUB_PORT=$HUB DEVHUB_DATA=$T/data DEVHUB_NO_AUTOSTART=1 DEVHUB_NAME=server-smoke
export DEVHUB_FILE_PORT=18795 DEVHUB_PASSWORD=smoke-pw DEVHUB_BIND=127.0.0.1 DEVHUB_PUBLIC_URL=http://127.0.0.1:$HUB DEVHUB_HUB_NAME=smokehub
PIDS=
cleanup() { for p in $PIDS; do kill "$p" 2>/dev/null || true; done
            for s in /tmp/*.sock; do ssh -S "$s" -O exit x >/dev/null 2>&1 || true; done 2>/dev/null; rm -rf "$T"; }
trap cleanup EXIT
fail() { echo "FAIL: $*" >&2; tail -n 30 "$T/data/devhubd.log" >&2 2>/dev/null; exit 1; }
ok() { echo "ok: $*"; }

mkdir -p "$T/home/.ssh" "$T/ports" "$T/pm"
# a fake PortMaster on the fake device: its harbourmaster downloads a URL and checks ?md5= like the real one
cat > "$T/pm/harbourmaster" <<'EOF'
#!/bin/sh
z=$(eval echo \${$#})
cd "$(dirname "$0")"
case "$z" in
  http*) want=${z##*md5=}; url=${z%%\?*}; name=${url##*/}
         curl -fsS -o "dl-$name" "$url" || { echo "download failed"; exit 1; }
         [ "$(md5sum < "dl-$name" | cut -c1-32)" = "$want" ] || { echo "md5 mismatch"; exit 1; }
         echo "installed $name from $url" >> installed.txt ;;
  *)     echo "installed $(basename "$z") from a copy" >> installed.txt ;;
esac
EOF
chmod +x "$T/pm/harbourmaster"
echo "device-password-unused" > "$T/pw"
$PY "$HERE/tests/fake_device_sshd.py" $DEVPORT "$T/pw" "$T/hostkey" "$T/home/.ssh/authorized_keys" & PIDS="$PIDS $!"
$PY "$HERE/devhubsrv.py" > "$T/hub.out" 2>&1 & PIDS="$PIDS $!"
for i in $(seq 40); do curl -fs "http://127.0.0.1:$HUB/api/ping" >/dev/null 2>&1 && break; sleep 0.5; done
curl -fs "http://127.0.0.1:$HUB/api/ping" >/dev/null || { cat "$T/hub.out"; fail "hub did not start"; }

C="curl -s -b $T/jar -c $T/jar"
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

# --- the gate
[ "$(code "http://127.0.0.1:$HUB/")" = 302 ] || fail "/ without login is not a redirect"
[ "$(code "http://127.0.0.1:$HUB/api/state")" = 401 ] || fail "/api/state without login or token"
$C -d password=wrong "http://127.0.0.1:$HUB/login" | grep -q "Wrong password" || fail "wrong password accepted"
[ "$($C -o /dev/null -w '%{http_code}' -d password=smoke-pw "http://127.0.0.1:$HUB/login")" = 303 ] || fail "login"
$C "http://127.0.0.1:$HUB/" | grep -q "DeviceHub" || fail "page after login"
TOK=$(cat "$T/data/token")
curl -s -H "X-DevHub-Token: $TOK" -X PUT -d '{"client":"ui","settings":{"away_mode":true}}' \
    "http://127.0.0.1:$HUB/api/settings" | grep -q "log in" || fail "the token alone acted as the user"
ok "login gate"

# --- the beacon port, as a handheld would get it
[ "$(code "http://127.0.0.1:$HUB/api/beacon/port.zip?t=$TOK")" = 403 ] || fail "port zip without login"
$C -o "$T/port.zip" "http://127.0.0.1:$HUB/api/beacon/port.zip?autostart=0"
(cd "$T/ports" && unzip -q ../port.zip) || fail "port zip"
(cd "$HERE" && $PY -c "import devhubd, sys; r = devhubd.inspect_zip(sys.argv[1]); print(r); assert not r['errors'] and not r['warnings'] and r['title'] == 'DeviceHub Beacon', r" "$T/port.zip") >/dev/null ||
    fail "the port zip doesn't pass DeviceHub's port check"
[ -x "$T/ports/devicehub/beacon" ] && ! unzip -l "$T/port.zip" | grep -q "devicehub/.*\.sh" && grep -q "HUB_URL='http://127.0.0.1:$HUB'" "$T/ports/devicehub/beacon.conf" || fail "port contents"
cat > "$T/ports/devicehub/device.env" <<EOF
CFW_NAME='ROCKNIX'
CFW_VERSION='20261001'
DEVICE_NAME='Smoke RG35XX'
DISPLAY_WIDTH='640'
DISPLAY_HEIGHT='480'
DEVICE_RAM='1024'
PM_DIR='$T/pm'
PORTS_DIR='/storage/roms/ports'
EOF
# the beacon sees only busybox (no curl, no openssl): busybox wget and the pure-sh HMAC, like a bare handheld
mkdir -p "$T/bin"; for a in $(busybox --list); do ln -s "$(command -v busybox)" "$T/bin/$a"; done
BB=$(command -v busybox)
B() { PATH="$T/bin" DH_SSH_HOME="$T/home" SSH_PORT=$DEVPORT DH_HOSTKEYS="$T/hostkey.pub" "$BB" sh "$T/ports/devicehub/beacon" "$@"; }
B once | grep -q "state=pending" || { B once || true; fail "first announce is not pending"; }
ok "announce: pending"

# --- forged and replayed announces
curl -s "http://127.0.0.1:$HUB/api/beacon/hello" | grep -q '^nonce=' || fail "hello"
[ "$(code -X POST -H 'X-Beacon-Sig: 00' --data-binary 'id=deadbeefdeadbeef' "http://127.0.0.1:$HUB/api/beacon")" = 403 ] || fail "forged announce"
ok "forged announce refused"

BID=$(cat "$T/home/.devicehub-beacon-id")
$C -X POST -d '{"client":"ui"}' "http://127.0.0.1:$HUB/api/beacons/$BID/approve" | grep -q '"ok": true' || fail "approve"
B once | grep -q "state=approved" || fail "announce after approval"
grep -q "devicehub@smokehub" "$T/home/.ssh/authorized_keys" || fail "hub key not installed"
grep -q "^\[127.0.0.1\]:$DEVPORT ssh-rsa " "$T/data/known_hosts/hub" || fail "host key not pinned"
ok "approved: hub key installed, host key pinned"

DEV=$($C "http://127.0.0.1:$HUB/api/state" | $PY -c 'import json,sys; b=json.load(sys.stdin)["server"]["beacons"]; print(b[0]["dev"])')
[ "$DEV" = smoke-rg35xx ] || fail "device id: $DEV"
for i in $(seq 40); do
    $C "http://127.0.0.1:$HUB/api/state" | $PY -c "import json,sys; d=json.load(sys.stdin); v=[x for x in d['devices'] if x['id']=='$DEV'][0]; sys.exit(0 if v['rt']['master'].get('state')=='up' else 1)" 2>/dev/null && break
    [ "$i" = 40 ] && fail "the hub did not connect with its key"
    sleep 0.5
done
$C "http://127.0.0.1:$HUB/api/state" | $PY -c "import json,sys; d=json.load(sys.stdin); v=[x for x in d['devices'] if x['id']=='$DEV'][0]; assert v['auth']=='key' and v['cfw']=='ROCKNIX' and v['ports_dir']=='/storage/roms/ports' and v['screen']=='640x480' and v['pm_dir']=='$T/pm', v" || fail "device profile"
ok "hub connected with its key ($DEV)"

# --- an agent through the token
DH="$PY $HERE/devhub.py"
$DH acquire $DEV "server smoke" --for 5m 2>&1 | grep -q "asked the user" || fail "acquire did not ask the user"
$C -X POST -d "{\"client\":\"ui\",\"dev\":\"$DEV\",\"name\":\"server-smoke\",\"allow\":true}" \
    "http://127.0.0.1:$HUB/api/lock/approve" | grep -q '"ok": true' || fail "the logged-in user could not approve"
$DH acquire $DEV "server smoke" --for 5m | grep -q "is yours" || fail "acquire after approval"
[ "$($DH run $DEV -- 'echo hello-from-device')" = hello-from-device ] || fail "run"
$DH release $DEV --dirty "no front end on the fake device" >/dev/null || fail "release"
ok "agent acquire/run/release"
# --- no blind polling: a device added by hand is left alone until you press Connect
$C -X POST -d "{\"client\":\"ui\",\"device\":{\"id\":\"byhand\",\"name\":\"By hand\",\"host\":\"127.0.0.1\",\"port\":$DEVPORT,
    \"user\":\"root\",\"auth\":\"password\",\"approval\":\"off\"}}" "http://127.0.0.1:$HUB/api/devices" | grep -q '"ok": true' || fail "add by hand"
$C -X POST -d "{\"client\":\"ui\",\"password\":\"$(cat "$T/pw")\"}" "http://127.0.0.1:$HUB/api/devices/byhand/password" | grep -q '"ok": true' || fail "password"
mstate() { $C "http://127.0.0.1:$HUB/api/state" | $PY -c "import json,sys; d=json.load(sys.stdin); v=[x for x in d['devices'] if x['id']=='$1'][0]; print(v['rt']['master'].get('state'), v['rt']['online'])"; }
sleep 4
[ "$(mstate byhand)" = "offline False" ] || fail "the hub reached out to a device that didn't announce: $(mstate byhand)"
$C -X POST -d '{"client":"ui"}' "http://127.0.0.1:$HUB/api/devices/byhand/connect" | grep -q '"ok": true' || fail "connect"
for i in $(seq 40); do [ "$(mstate byhand)" = "up True" ] && break; [ "$i" = 40 ] && fail "Connect didn't connect: $(mstate byhand)"; sleep 0.5; done
ok "no polling: left alone until Connect, then connected"
# --- a remote agent: an agent token, and run/push/pull through the hub (no local data folder, no SSH sockets)
AT=$($C -X POST -d '{"client":"ui","name":"Remote Claude"}' "http://127.0.0.1:$HUB/api/agents" | $PY -c 'import json,sys; print(json.load(sys.stdin)["token"])')
case "$AT" in dh_*) ;; *) fail "no agent token: $AT" ;; esac
curl -s -H "X-DevHub-Token: $AT" -X PUT -d '{"client":"ui","settings":{"away_mode":true}}' "http://127.0.0.1:$HUB/api/settings" |
    grep -q "log in" || fail "an agent token acted as the user"
curl -s -H "X-DevHub-Token: $AT" -X POST -d '{}' "http://127.0.0.1:$HUB/api/shutdown" | grep -q "can't stop" || fail "an agent stopped the hub"
R="env -u DEVHUB_PORT DEVHUB_DATA=$T/nowhere DEVHUB_URL=http://127.0.0.1:$HUB DEVHUB_TOKEN=$AT DEVHUB_NAME=remote-agent $PY $HERE/devhub.py"
$R run byhand -- 'echo nope' 2>&1 | grep -q "take the lock first" || fail "remote run without the lock was allowed"
[ "$($R run byhand --ro -- 'echo ro-ok')" = ro-ok ] || fail "remote read-only run"
$R acquire byhand "remote agent test" --for 5m | grep -q "is yours" || fail "remote acquire"
rc=0; out=$($R run byhand -- 'echo out-line; echo err-line >&2; exit 3' 2>"$T/err") || rc=$?
[ "$out" = out-line ] && [ "$rc" = 3 ] && grep -q err-line "$T/err" || fail "remote run: out=$out rc=$rc err=$(cat "$T/err")"
head -c 300000 /dev/urandom > "$T/blob"
$R push byhand "$T/blob" "$T/remote-blob" >/dev/null || fail "remote push"
cmp -s "$T/blob" "$T/remote-blob" || fail "pushed file differs"
$R pull byhand "$T/remote-blob" "$T/blob-back" >/dev/null || fail "remote pull"
cmp -s "$T/blob" "$T/blob-back" || fail "pulled file differs"
$R pull byhand "$T/no-such-file" "$T/x" 2>&1 | grep -q "can't read" || fail "pull of a missing file"
$C "http://127.0.0.1:$HUB/api/state" | $PY -c "import json,sys; d=json.load(sys.stdin); v=[x for x in d['devices'] if x['id']=='byhand'][0]; assert v['lock']['holder']['name']=='remote-agent', v['lock']" ||
    fail "lock not under the agent's name"
$R release byhand --dirty "fake device" >/dev/null || fail "remote release"
# the devhub command as the Agents page tells an agent to get it: downloaded from the hub, on its own
mkdir -p "$T/cli" && curl -fsS "http://127.0.0.1:$HUB/devhub.py" -o "$T/cli/devhub" || fail "devhub.py download"
RD="env -u DEVHUB_PORT DEVHUB_DATA=$T/nowhere DEVHUB_URL=http://127.0.0.1:$HUB DEVHUB_TOKEN=$AT DEVHUB_NAME=remote-agent $PY $T/cli/devhub"
$RD status | grep -q byhand || fail "the downloaded devhub can't list devices"
$RD agent-guide | grep -q "## The rules" || fail "the downloaded devhub has no agent guide"
AID=$(echo "$AT" | cut -d_ -f2)
$C -X DELETE -d '{"client":"ui"}' "http://127.0.0.1:$HUB/api/agents/$AID" | grep -q '"ok": true' || fail "revoke"
$R status >/dev/null 2>&1 && fail "a revoked token still works"
ok "remote agent: token, run (output, exit code), push/pull, lock, revoke"

# --- install on all: harbourmaster fetches the zip from DeviceHub over HTTP; PortMaster.zip is accepted
$PY -c "import zipfile; z = zipfile.ZipFile('$T/game.zip', 'w'); z.writestr('Game.sh', '#!/bin/sh'); z.writestr('game/port.json', '{\"attr\": {\"title\": \"Game\"}}')"
$PY -c "import zipfile; z = zipfile.ZipFile('$T/pmrel.zip', 'w'); z.writestr('PortMaster/harbourmaster', '#!/bin/sh'); z.writestr('PortMaster/version', '2026.10.01')"
installall() {   # FILE NAME -> waits for the job, prints its state
    up=$($C --data-binary "@$1" -H 'Content-Type: application/octet-stream' "http://127.0.0.1:$HUB/api/devices/_all/upload?name=$(printf %s "$2" | sed 's/ /%20/g')")
    uid=$(echo "$up" | $PY -c 'import json,sys; print(json.load(sys.stdin)["upload"] or "")')
    [ -n "$uid" ] || { echo "upload refused: $up"; return; }
    job=$($C -X POST -d "{\"client\":\"ui\",\"upload\":\"$uid\",\"devices\":[\"$DEV\"],\"refresh\":\"none\"}" "http://127.0.0.1:$HUB/api/install-all" |
          $PY -c 'import json,sys; r=json.load(sys.stdin); print(r["started"][0]["job"]["id"] if r.get("started") else r)')
    for i in $(seq 60); do
        st=$($C "http://127.0.0.1:$HUB/api/jobs/$job" | $PY -c 'import json,sys; j=json.load(sys.stdin)["job"]; print(j["state"], j.get("error") or "")')
        case "$st" in running*) sleep 0.5 ;; *) echo "$st"; return ;; esac
    done
    echo "timeout"
}
r=$(installall "$T/game.zip" "game.zip"); [ "$r" = "done " ] || fail "install-all of a port: $r"
grep -q "installed game.zip from http://127.0.0.1:18795/dl/" "$T/pm/installed.txt" || fail "the port wasn't fetched from DeviceHub: $(cat "$T/pm/installed.txt")"
r=$(installall "$T/pmrel.zip" "PortMaster (2).zip"); [ "$r" = "done " ] || fail "install-all of PortMaster.zip: $r"
grep -q "installed PortMaster.zip from http" "$T/pm/installed.txt" || fail "PortMaster.zip not installed under its own name"
for i in $(seq 20); do   # it shuts down within a couple of seconds of the last install
    [ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:18795/dl/00000000000000000000000000000000/x.zip")" = 000 ] && break
    [ "$i" = 20 ] && fail "the file server is still up after the installs"; sleep 0.5
done
ok "install on all: fetched from DeviceHub (md5 checked); PortMaster.zip accepted"
echo PASS
