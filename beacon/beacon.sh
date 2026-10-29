#!/bin/sh
# DeviceHub beacon: tells the DeviceHub server "this handheld is up, at this IP", so the hub can SSH back to it.
# Plain sh with busybox tools (curl or wget, sha256sum; openssl if there is one), no python.
#
#   beacon.sh port            what the port launcher runs: announce once and say so on screen, start the
#                             background beacon, install autostart if beacon.conf asks for it
#   beacon.sh daemon          the background beacon: announce at boot once the network is up, then every minute,
#                             on an IP change and after a resume from sleep
#   beacon.sh once            announce once, print the hub's answer
#   beacon.sh autostart install|remove|status
#   beacon.sh stop            stop the background beacon
#
# Protocol (see docs/SERVER.md): GET /api/beacon/hello gives a one-time nonce; POST /api/beacon sends key=value
# lines with that nonce, signed HMAC-SHA256(hub key, body) in X-Beacon-Sig. The answer is key=value lines echoing
# our random challenge, ending in sig=HMAC(hub key, everything before it). An answer that doesn't verify is ignored,
# so nobody on the network can hand us an SSH key to install.

HERE=$(cd "$(dirname "$0")" && pwd)
CONF="$HERE/beacon.conf"
[ -f "$CONF" ] || { echo "beacon: $CONF is missing (download the port from your DeviceHub again)" >&2; exit 2; }
. "$CONF"                                    # HUB_URL, HUB_KEY, HUB_NAME, AUTOSTART
[ -f "$HERE/device.env" ] && . "$HERE/device.env"   # facts the port launcher read from PortMaster (CFW, device, screen)
TMP=${TMPDIR:-/tmp}/devicehub-beacon.$$
PIDFILE=/tmp/devicehub-beacon.pid
LOG=/tmp/devicehub-beacon.log
TTY=${CUR_TTY:-}

say() { echo "$*"; [ -n "$TTY" ] && [ -w "$TTY" ] && echo "$*" > "$TTY"; return 0; }
log() { [ "$QUIET" = 1 ] && { echo "$(date '+%F %T') $*" >> "$LOG"; return 0; }; say "$*"; }
cleanup() { rm -f "$TMP".*; }
trap cleanup EXIT

# ---------------------------------------------------------------- HMAC-SHA256 (openssl if it works, else pure sh)

hex_to_bin() {   # hex string -> raw bytes
    h=$1
    while [ -n "$h" ]; do
        b=${h%"${h#??}"}; h=${h#??}
        printf "\\$(printf '%03o' "0x$b")"
    done
}

hmac() {   # HEXKEY FILE -> hex digest
    if command -v openssl >/dev/null 2>&1; then
        r=$(openssl dgst -sha256 -mac HMAC -macopt "hexkey:$1" < "$2" 2>/dev/null | sed 's/^.*= *//')
        [ ${#r} = 64 ] && { echo "$r"; return; }
    fi
    k=$1
    while [ ${#k} -lt 128 ]; do k="${k}00"; done
    _ip=""; _op=""; t=$k
    while [ -n "$t" ]; do
        b=${t%"${t#??}"}; t=${t#??}
        _ip="$_ip$(printf '%02x' $((0x$b ^ 0x36)))"; _op="$_op$(printf '%02x' $((0x$b ^ 0x5c)))"
    done
    inner=$({ hex_to_bin "$_ip"; cat "$2"; } | sha256sum | cut -c1-64)
    { hex_to_bin "$_op"; hex_to_bin "$inner"; } | sha256sum | cut -c1-64
}

rand_hex() { od -An -tx1 -N "$1" /dev/urandom | tr -d ' \n'; }

# ---------------------------------------------------------------- HTTP (curl, else busybox wget)

WGET_T=""   # some busybox builds have no -T (timeout)
command -v curl >/dev/null 2>&1 || { wget --help 2>&1 | grep -q -- '-T ' && WGET_T="-T 15"; }

http_get() {
    if command -v curl >/dev/null 2>&1; then curl -fsS -m 10 "$1" 2>/dev/null
    else wget -q $WGET_T -O - "$1" 2>/dev/null; fi
}

http_post() {   # URL FILE SIG
    if command -v curl >/dev/null 2>&1; then
        curl -fsS -m 15 -H "X-Beacon-Sig: $3" -H "Content-Type: text/plain" --data-binary "@$2" "$1" 2>/dev/null
    else
        wget -q $WGET_T -O - --header "X-Beacon-Sig: $3" --header "Content-Type: text/plain" --post-file "$2" "$1" 2>/dev/null
    fi
}

# ---------------------------------------------------------------- facts about this device

hub_host() { echo "$HUB_URL" | sed -e 's|^[a-z]*://||' -e 's|[/:].*$||'; }

route_to_hub() {   # "IP IFACE" this device uses to reach the hub
    h=$(hub_host)
    case "$h" in *[!0-9.]*) h=$(nslookup "$h" 2>/dev/null | sed -n 's/^Address[^:]*: *\([0-9.]*\).*/\1/p' | tail -1) ;; esac
    r=$(ip route get "${h:-1.1.1.1}" 2>/dev/null | head -1)
    src=$(echo "$r" | sed -n 's/.* src \([0-9.]*\).*/\1/p')
    dev=$(echo "$r" | sed -n 's/.* dev \([^ ]*\).*/\1/p')
    if [ -z "$src" ]; then   # no iproute: the first non-loopback IPv4 address
        src=$(ifconfig 2>/dev/null | sed -n 's/.*inet \(addr:\)\{0,1\}\([0-9.]*\).*/\2/p' | grep -v '^127\.' | head -1)
    fi
    echo "$src $dev"
}

ssh_user() { id -un 2>/dev/null || echo root; }

ssh_home() {   # the SSH user's home, where authorized_keys goes
    [ -n "$DH_SSH_HOME" ] && { echo "$DH_SSH_HOME"; return; }
    u=$(ssh_user)
    h=$(awk -F: -v u="$u" '$1 == u { print $6 }' /etc/passwd 2>/dev/null)
    echo "${h:-$HOME}"
}

ssh_listening() {   # something listens on TCP 22 (sshd or dropbear)
    grep -qs ' [0-9A-F]*:0016 [0-9A-F]*:0000 0A' /proc/net/tcp /proc/net/tcp6 && echo up || echo down
}

pubkey_of() {   # private host key file -> "type base64" (its .pub, or derived from the key)
    if [ -f "$1.pub" ]; then cut -d' ' -f1,2 "$1.pub"
    elif command -v ssh-keygen >/dev/null 2>&1 && ssh-keygen -y -f "$1" >/dev/null 2>&1; then ssh-keygen -y -f "$1" | cut -d' ' -f1,2
    elif command -v dropbearkey >/dev/null 2>&1; then dropbearkey -y -f "$1" 2>/dev/null | grep '^ssh-' | cut -d' ' -f1,2
    fi
}

host_keys() {   # the SSH host public keys this device really serves, one per line
    if [ -n "$DH_HOSTKEYS" ]; then cat $DH_HOSTKEYS 2>/dev/null | cut -d' ' -f1,2; return; fi   # tests
    # best: ask the running server itself (exactly what the hub will be shown)
    if command -v ssh-keyscan >/dev/null 2>&1; then
        k=$(ssh-keyscan -T 3 -p "${SSH_PORT:-22}" 127.0.0.1 2>/dev/null | awk '!/^#/ && NF >= 3 { print $2 " " $3 }')
        [ -n "$k" ] && { echo "$k"; return; }
    fi
    # else the key files the running server was told to use (CFWs often keep them on the data partition, while
    # /etc/ssh holds the image's defaults), then the usual places
    {
        for p in $(pidof sshd dropbear 2>/dev/null); do
            tr '\0' '\n' < "/proc/$p/cmdline" 2>/dev/null | awk 'f { print; f = 0 } $0 == "-h" || $0 == "-r" { f = 1 }'
        done
        for c in /etc/ssh/sshd_config /storage/.cache/ssh/sshd_config; do
            [ -f "$c" ] && awk 'tolower($1) == "hostkey" { print $2 }' "$c"
        done
        ls /etc/ssh/ssh_host_*_key /storage/.cache/ssh/ssh_host_*_key /storage/.ssh/ssh_host_*_key \
           /userdata/system/ssh/ssh_host_*_key /userdata/system/.ssh/ssh_host_*_key \
           /etc/dropbear/dropbear_*_host_key /storage/.cache/dropbear/dropbear_*_host_key 2>/dev/null
    } | sort -u | while read -r f; do [ -f "$f" ] && pubkey_of "$f"; done
}

device_id() {   # random, made once per SD card; kept in the SSH user's home so reinstalling the port keeps it
    f="$(ssh_home)/.devicehub-beacon-id"
    if [ ! -s "$f" ]; then
        if [ -s "$HERE/id" ]; then cp "$HERE/id" "$f" 2>/dev/null; else rand_hex 8 > "$f" 2>/dev/null; fi
    fi
    [ -s "$f" ] || f="$HERE/id"                     # home not writable: keep it with the port
    [ -s "$f" ] || rand_hex 8 > "$f"
    cat "$f"
}

clean() { printf '%s' "$1" | tr -d '\r\n' | cut -c1-200; }

hub_key_state() {   # 1 = the hub's key is in authorized_keys, 0 = not (yet), fail = we couldn't write it
    [ -s "$HERE/hub.pub" ] || { echo 0; return; }
    [ -f "$HERE/keyfail" ] && { echo fail; return; }
    k=$(cut -d' ' -f2 "$HERE/hub.pub")
    grep -qsF "$k" "$(ssh_home)/.ssh/authorized_keys" && echo 1 || echo 0
}

install_hub_key() {   # PUBKEY line from the hub (verified): add it to the SSH user's authorized_keys once
    k=$(echo "$1" | cut -d' ' -f2)
    [ -n "$k" ] || return 1
    d="$(ssh_home)/.ssh"
    if grep -qsF "$k" "$d/authorized_keys"; then printf '%s\n' "$1" > "$HERE/hub.pub"; return 0; fi
    if mkdir -p "$d" 2>/dev/null && chmod 700 "$d" && printf '%s\n' "$1" >> "$d/authorized_keys" &&
       chmod 600 "$d/authorized_keys" && grep -qsF "$k" "$d/authorized_keys"; then
        printf '%s\n' "$1" > "$HERE/hub.pub"; rm -f "$HERE/keyfail"
        log "added ${HUB_NAME:-DeviceHub}'s SSH key to $d/authorized_keys"
        return 0
    fi
    printf '%s\n' "$1" > "$HERE/hub.pub"; : > "$HERE/keyfail"
    log "could not write $d/authorized_keys: ${HUB_NAME:-DeviceHub} will need the device's password"
    return 1
}

# ---------------------------------------------------------------- announce

ANSWER_STATE=""; ANSWER_MSG=""; ANSWER_INTERVAL=60

announce() {
    again=$1
    ANSWER_STATE=""; ANSWER_MSG=""
    set -- $(route_to_hub)
    ip=$1; iface=$2
    [ -n "$ip" ] || { ANSWER_MSG="no network yet"; return 1; }
    hello=$(http_get "$HUB_URL/api/beacon/hello") || { ANSWER_MSG="can't reach ${HUB_NAME:-the hub} at $HUB_URL"; return 1; }
    nonce=$(echo "$hello" | sed -n 's/^nonce=//p' | head -1)
    [ -n "$nonce" ] || { ANSWER_MSG="$HUB_URL doesn't answer like a DeviceHub"; return 1; }
    chal=$(rand_hex 16)
    mac=""; [ -n "$iface" ] && mac=$(cat "/sys/class/net/$iface/address" 2>/dev/null)
    {
        echo "v=1"
        echo "nonce=$nonce"
        echo "id=$(device_id)"
        echo "challenge=$chal"
        echo "name=$(clean "${DEVICE_NAME:-$(hostname)}")"
        echo "cfw=$(clean "$CFW_NAME")"
        echo "cfw_version=$(clean "$CFW_VERSION")"
        echo "hostname=$(clean "$(hostname)")"
        echo "ip=$ip"
        echo "mac=$mac"
        echo "user=$(ssh_user)"
        echo "port=${SSH_PORT:-22}"
        echo "ssh=$(ssh_listening)"
        echo "keyok=$(hub_key_state)"
        echo "autostart=$(autostart_state)"
        echo "width=$(clean "$DISPLAY_WIDTH")"
        echo "height=$(clean "$DISPLAY_HEIGHT")"
        echo "ram=$(clean "$DEVICE_RAM")"
        echo "cpu=$(clean "$DEVICE_CPU")"
        echo "pm_dir=$(clean "$PM_DIR")"
        echo "ports_dir=$(clean "$PORTS_DIR")"
        echo "arch=$(uname -m)"
        echo "kernel=$(uname -r)"
        host_keys | sort -u | sed 's/^/hostkey=/'
    } > "$TMP.req"
    sig=$(hmac "$HUB_KEY" "$TMP.req")
    http_post "$HUB_URL/api/beacon" "$TMP.req" "$sig" > "$TMP.ans" || { ANSWER_MSG="${HUB_NAME:-the hub} refused the announce"; return 1; }
    # verify: the last line is sig=, over every byte before it; our challenge must be echoed
    got=$(sed -n 's/^sig=//p' "$TMP.ans" | tail -1)
    sed '$d' "$TMP.ans" > "$TMP.body"
    want=$(hmac "$HUB_KEY" "$TMP.body")
    if [ -z "$got" ] || [ "$got" != "$want" ] || ! grep -qx "challenge=$chal" "$TMP.body"; then
        ANSWER_MSG="the answer from $HUB_URL isn't signed with this port's key (wrong hub, or the key was changed)"
        return 1
    fi
    ANSWER_STATE=$(sed -n 's/^state=//p' "$TMP.body" | head -1)
    ANSWER_MSG=$(sed -n 's/^msg=//p' "$TMP.body" | head -1)
    ANSWER_INTERVAL=$(sed -n 's/^interval=//p' "$TMP.body" | head -1)
    case "$ANSWER_INTERVAL" in ''|*[!0-9]*) ANSWER_INTERVAL=60 ;; esac
    pub=$(sed -n 's/^pubkey=//p' "$TMP.body" | head -1)
    if [ "$ANSWER_STATE" = approved ] && [ -n "$pub" ] && [ "$(hub_key_state)" = 0 ]; then
        install_hub_key "$pub" && [ "$again" != again ] && { announce again; return $?; }   # tell the hub at once
    fi
    return 0
}

# ---------------------------------------------------------------- autostart, per CFW

UNIT=devicehub-beacon.service
MARK="# devicehub-beacon"

cfw_kind() {
    c=$(echo "${CFW_NAME:-}" | tr 'A-Z' 'a-z')
    case "$c" in
        knulli*|batocera*) echo batocera ;;
        rocknix*|jelos*|amberelec*|emuelec*|unofficialos*|351elec*) echo libreelec ;;
        muos*) echo muos ;;
        arkos*|darkos*|"ark os"*) echo systemd ;;
        *) if [ -d /storage/.config/system.d ]; then echo libreelec
           elif [ -f /userdata/system/custom.sh ] || [ -d /userdata/system ]; then echo batocera
           elif [ -d /etc/systemd/system ]; then echo systemd
           else echo none; fi ;;
    esac
}

muos_init_dir() {
    for d in /run/muos/storage/init /mnt/mmc/MUOS/init /mnt/sdcard/MUOS/init; do [ -d "$d" ] && { echo "$d"; return; }; done
}

unit_text() {
    cat <<EOF
[Unit]
Description=DeviceHub beacon (announces this device to ${HUB_NAME:-DeviceHub})
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/bin/sh "$HERE/beacon.sh" daemon
Restart=always
RestartSec=30
$1

[Install]
WantedBy=multi-user.target
EOF
}

SUDO=""
[ "$(id -u)" != 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo -n"

autostart_state() {
    case "$(cfw_kind)" in
        batocera) grep -qs "$MARK" /userdata/system/custom.sh && echo 1 || echo 0 ;;
        libreelec) [ -e /storage/.config/system.d/multi-user.target.wants/$UNIT ] && echo 1 || echo 0 ;;
        muos) d=$(muos_init_dir); [ -n "$d" ] && [ -f "$d/devicehub-beacon.sh" ] && echo 1 || echo 0 ;;
        systemd) [ -e /etc/systemd/system/multi-user.target.wants/$UNIT ] && echo 1 || echo 0 ;;
        *) echo 0 ;;
    esac
}

autostart_install() {
    case "$(cfw_kind)" in
        batocera)
            f=/userdata/system/custom.sh
            grep -qs "$MARK" "$f" && { say "autostart: already in $f"; return 0; }
            [ -f "$f" ] || printf '#!/bin/sh\n' > "$f"
            printf '%s\nif [ "$1" = start ]; then (sh "%s/beacon.sh" daemon >/dev/null 2>&1 &); fi\n%s end\n' \
                "$MARK" "$HERE" "$MARK" >> "$f" && chmod +x "$f" && say "autostart: added to $f" ;;
        libreelec)
            unit_text "" > /storage/.config/system.d/$UNIT && systemctl daemon-reload &&
                systemctl enable $UNIT >/dev/null 2>&1 && say "autostart: systemd unit in /storage/.config/system.d" ;;
        muos)
            d=$(muos_init_dir)
            [ -n "$d" ] || { say "autostart: no MUOS/init folder found"; return 1; }
            printf '#!/bin/sh\n%s\n(sh "%s/beacon.sh" daemon >/dev/null 2>&1 &)\n' "$MARK" "$HERE" > "$d/devicehub-beacon.sh" &&
                chmod +x "$d/devicehub-beacon.sh" &&
                say "autostart: $d/devicehub-beacon.sh (switch on Configuration > Custom > User Init Scripts)" ;;
        systemd)
            u=$(ssh_user); extra=""; [ "$u" != root ] && extra="User=$u"
            unit_text "$extra" | $SUDO tee /etc/systemd/system/$UNIT >/dev/null && $SUDO systemctl daemon-reload &&
                $SUDO systemctl enable $UNIT >/dev/null 2>&1 && say "autostart: /etc/systemd/system/$UNIT" ;;
        *) say "autostart: no known boot hook on this CFW; run the port after each boot"; return 1 ;;
    esac
}

autostart_remove() {
    case "$(cfw_kind)" in
        batocera) f=/userdata/system/custom.sh
                  [ -f "$f" ] && sed -i "/^$MARK\$/,/^$MARK end\$/d" "$f" ;;
        libreelec) systemctl disable $UNIT >/dev/null 2>&1; rm -f /storage/.config/system.d/$UNIT; systemctl daemon-reload ;;
        muos) d=$(muos_init_dir); [ -n "$d" ] && rm -f "$d/devicehub-beacon.sh" ;;
        systemd) $SUDO systemctl disable $UNIT >/dev/null 2>&1; $SUDO rm -f /etc/systemd/system/$UNIT; $SUDO systemctl daemon-reload ;;
    esac
    say "autostart: removed"
}

# ---------------------------------------------------------------- modes

running() { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }

daemon() {
    running && exit 0
    echo $$ > "$PIDFILE"
    QUIET=1
    log "beacon started (hub $HUB_URL)"
    last=0; lastip=""; wait=5; state=""
    while :; do
        now=$(date +%s)
        set -- $(route_to_hub)
        due=$((last + ANSWER_INTERVAL))
        # announce when due, when the IP changed, or when the wall clock jumped (a resume from sleep)
        if [ -n "$1" ] && { [ "$now" -ge "$due" ] || [ "$1" != "$lastip" ] || [ "$now" -lt "$last" ]; }; then
            if announce; then
                [ "$ANSWER_STATE" != "$state" ] && log "hub says: $ANSWER_STATE${ANSWER_MSG:+ ($ANSWER_MSG)}"
                state=$ANSWER_STATE; last=$now; lastip=$1; wait=10
            else
                log "announce failed: $ANSWER_MSG"
                wait=$((wait * 2)); [ "$wait" -gt 60 ] && wait=60
                last=$((now - ANSWER_INTERVAL + wait))
            fi
        fi
        sleep 10
    done
}

case "$1" in
    once)
        if announce; then say "state=$ANSWER_STATE"; [ -n "$ANSWER_MSG" ] && say "$ANSWER_MSG"; exit 0
        else say "failed: $ANSWER_MSG"; exit 1; fi ;;
    daemon) daemon ;;
    stop) running && kill "$(cat "$PIDFILE")"; rm -f "$PIDFILE" ;;
    autostart)
        case "$2" in
            install) autostart_install ;;
            remove) autostart_remove ;;
            *) echo "autostart: $(autostart_state) ($(cfw_kind))" ;;
        esac ;;
    port)
        say "DeviceHub beacon -> ${HUB_NAME:-DeviceHub} ($HUB_URL)"
        [ "$(ssh_listening)" = down ] && say "!! SSH is not running on this device: switch it on in the CFW's settings"
        tries=0
        until announce; do
            tries=$((tries + 1))
            say "... $ANSWER_MSG"
            [ "$tries" -ge 6 ] && break
            sleep 5
        done
        case "$ANSWER_STATE" in
            approved) say "OK: ${HUB_NAME:-DeviceHub} knows this device${ANSWER_MSG:+ - $ANSWER_MSG}" ;;
            pending) say "Waiting: approve this device in ${HUB_NAME:-DeviceHub}'s web page" ;;
            denied) say "Refused: ${HUB_NAME:-DeviceHub} turned this device down${ANSWER_MSG:+ - $ANSWER_MSG}" ;;
            "") say "Not announced yet; the background beacon keeps trying" ;;
        esac
        [ "$AUTOSTART" = 1 ] && [ "$(autostart_state)" = 0 ] && autostart_install
        if ! running; then
            if command -v setsid >/dev/null 2>&1; then setsid sh "$HERE/beacon.sh" daemon >/dev/null 2>&1 < /dev/null &
            else nohup sh "$HERE/beacon.sh" daemon >/dev/null 2>&1 < /dev/null & fi
            say "background beacon started (log: $LOG)"
        fi ;;
    *) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 2 ;;
esac
