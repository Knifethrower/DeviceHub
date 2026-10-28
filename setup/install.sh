#!/bin/sh
# Install DeviceHub as a systemd service (Linux, or Ubuntu/Debian under WSL 2).
#   sudo setup/install.sh            install or update the service, start it
#   sudo setup/install.sh --remove   stop and remove the service (data/ is kept)
# Without systemd DeviceHub still works: `devhub start` runs the server by hand, and every devhub command
# starts it when it is not running.
set -e

HERE=$(cd "$(dirname "$0")/.." && pwd)
UNIT=/etc/systemd/system/devhub.service
RUN_AS=${SUDO_USER:-$(id -un)}

if [ "$1" = "--remove" ]; then
    systemctl disable --now devhub 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    echo "DeviceHub service removed (settings, devices and passwords stay in $HERE/data)."
    exit 0
fi

fail() { echo "install: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || fail "run it with sudo (it writes $UNIT)"
[ "$RUN_AS" != root ] || fail "run it with sudo from your normal account: the service runs as that user, not root"
command -v python3 >/dev/null || fail "python3 is missing (sudo apt install python3)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || fail "python3 3.8 or newer is needed"
command -v ssh >/dev/null || fail "ssh is missing (sudo apt install openssh-client)"

if grep -qi microsoft /proc/version 2>/dev/null; then
    echo "WSL detected: device passwords are encrypted with Windows DPAPI (your Windows account)."
    command -v powershell.exe >/dev/null || [ -x /mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe ] ||
        echo "  warning: powershell.exe is not reachable; password devices will not work (key devices do)."
else
    echo "Linux: device passwords are encrypted with openssl and a key in $HERE/data/secret.key."
    command -v openssl >/dev/null || echo "  warning: openssl is missing; password devices need it (sudo apt install openssl)."
fi

if [ ! -d /run/systemd/system ]; then
    echo
    echo "systemd is not running here, so no service is installed."
    if grep -qi microsoft /proc/version 2>/dev/null; then
        echo "Turn it on for WSL: put these two lines in /etc/wsl.conf, then run 'wsl --shutdown' in Windows:"
        echo "    [boot]"
        echo "    systemd=true"
    fi
    echo "Or start the server by hand: $HERE/devhub start"
    exit 1
fi

sed -e "s|@DIR@|$HERE|g" -e "s|@USER@|$RUN_AS|g" -e "s|@PYTHON@|$(command -v python3)|g" \
    "$HERE/setup/devhub.service.in" > "$UNIT"
systemctl daemon-reload
systemctl enable devhub >/dev/null 2>&1
systemctl restart devhub

for i in 1 2 3 4 5 6 7 8 9 10; do
    if python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8765/api/ping", timeout=2)' 2>/dev/null; then
        echo
        echo "DeviceHub is running as $RUN_AS: http://127.0.0.1:8765/"
        echo "Next: add your devices in the web UI, then hook up your agent (see README.md)."
        exit 0
    fi
    sleep 1
done
fail "the service did not answer; see: systemctl status devhub, and $HERE/data/devhubd.log"
