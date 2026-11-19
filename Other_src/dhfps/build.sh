#!/bin/bash
# Build DeviceHub's FPS tools into ../../fps/:
#   dhfps-mon-aarch64      static monitor (Ubuntu's aarch64-linux-gnu-gcc)
#   libdhfps-aarch64.so    preload counter, built in a Debian bullseye arm64 chroot (glibc 2.31)
#   libdhfps-armhf.so      the same for 32-bit ports, in a Debian buster armhf chroot (glibc 2.28)
#   dhfps-mon-x86_64       static monitor (host gcc) for x86_64 devices (Steam Deck, PCs, VMs)
#   libdhfps-x86_64.so     preload counter, built in a debian:bullseye Docker container (glibc 2.31; bullseye is
#                          end-of-life, so apt uses the image's pinned snapshot.debian.org sources)
# The chroots need qemu-user binfmt. Run as root (chroot):  sudo ./build.sh   (CHROOT64=/path CHROOT32=/path)
# x86_64 only (no root, no chroots; Docker for the library):  ONLY=x86_64 ./build.sh
set -e
SRC=$(cd "$(dirname "$0")" && pwd)
OUT=$(cd "$SRC/../.." && pwd)/fps
CHROOT64=${CHROOT64:-/home/$SUDO_USER/chroot-bullseye}
CHROOT32=${CHROOT32:-/home/$SUDO_USER/chroot-buster-armhf}
mkdir -p "$OUT"

if [ "$(uname -m)" = x86_64 ]; then
    gcc -O2 -Wall -static -s -o "$OUT/dhfps-mon-x86_64" "$SRC/dhfps-mon.c"
    docker run --rm -v "$SRC:/src:ro" -v "$OUT:/out" debian:bullseye sh -c \
        "sed -i -e 's|^deb |#deb |' -e 's|^# deb http://snapshot|deb http://snapshot|' /etc/apt/sources.list* /etc/apt/sources.list.d/* 2>/dev/null
         apt-get -o Acquire::Check-Valid-Until=false update -qq >/dev/null &&
         apt-get install -y -qq --no-install-recommends gcc libc6-dev >/dev/null &&
         gcc -O2 -Wall -shared -fPIC -o /out/libdhfps-x86_64.so /src/libdhfps.c -ldl && strip /out/libdhfps-x86_64.so &&
         chown $(id -u):$(id -g) /out/libdhfps-x86_64.so"
    chmod 644 "$OUT/libdhfps-x86_64.so"
fi
if [ "$ONLY" = x86_64 ]; then ls -l "$OUT"; exit 0; fi

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
