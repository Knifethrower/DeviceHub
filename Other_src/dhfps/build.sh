#!/bin/bash
# Build DeviceHub's FPS tools into ../../fps/:
#   dhfps-mon-aarch64      static monitor (Ubuntu's aarch64-linux-gnu-gcc)
#   libdhfps-aarch64.so    preload counter, built in a Debian bullseye arm64 chroot (glibc 2.31)
#   libdhfps-armhf.so      the same for 32-bit ports, in a Debian buster armhf chroot (glibc 2.28)
# The chroots need qemu-user binfmt. Run as root (chroot):  sudo ./build.sh   (CHROOT64=/path CHROOT32=/path)
set -e
SRC=$(cd "$(dirname "$0")" && pwd)
OUT=$(cd "$SRC/../.." && pwd)/fps
CHROOT64=${CHROOT64:-/home/$SUDO_USER/chroot-bullseye}
CHROOT32=${CHROOT32:-/home/$SUDO_USER/chroot-buster-armhf}
mkdir -p "$OUT"

aarch64-linux-gnu-gcc -O2 -Wall -static -o "$OUT/dhfps-mon-aarch64" "$SRC/dhfps-mon.c"
aarch64-linux-gnu-strip "$OUT/dhfps-mon-aarch64"

in_chroot() {   # ROOT OUTNAME: build libdhfps.so in that chroot
    local R=$1 W=$1/tmp/dhfps-build
    for m in proc dev; do mountpoint -q "$R/$m" || mount --bind "/$m" "$R/$m"; done
    rm -rf "$W" && mkdir -p "$W" && cp "$SRC/libdhfps.c" "$SRC/dhfps.h" "$W/"
    chroot "$R" /usr/bin/env -i PATH=/usr/bin:/bin /bin/sh -c \
        "cd /tmp/dhfps-build && gcc -O2 -Wall -shared -fPIC -o libdhfps.so libdhfps.c -ldl && strip libdhfps.so"
    cp "$W/libdhfps.so" "$OUT/$2"
    rm -rf "$W"
}
in_chroot "$CHROOT64" libdhfps-aarch64.so
in_chroot "$CHROOT32" libdhfps-armhf.so
chmod 644 "$OUT"/libdhfps-*.so
ls -l "$OUT"
