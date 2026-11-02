#!/bin/sh
# Builds stream/dhcap-aarch64 and stream/dhcap-armhf: static, so they run on any CFW's libc.
# Needs the cross compilers (Debian: gcc-aarch64-linux-gnu gcc-arm-linux-gnueabihf).
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$HERE/../../stream
LD=$HERE/../dhstream/libdeflate
for t in aarch64:aarch64-linux-gnu armhf:arm-linux-gnueabihf; do
    arch=${t%%:*}; cc=${t#*:}-gcc
    $cc -O2 -static -s -Wall -Wno-unused-function -I"$LD" -o "$OUT/dhcap-$arch" "$HERE/dhcap.c" \
        "$LD/lib/deflate_compress.c" "$LD/lib/zlib_compress.c" "$LD/lib/adler32.c" "$LD/lib/utils.c" \
        "$LD/lib/arm/cpu_features.c"
    echo "built $OUT/dhcap-$arch ($(wc -c < "$OUT/dhcap-$arch") bytes)"
done
