#!/bin/sh
# Builds stream/dhcap-aarch64, stream/dhcap-armhf and stream/dhcap-x86_64: static, so they run on any CFW's libc.
# ARM needs the cross compilers (Debian: gcc-aarch64-linux-gnu gcc-arm-linux-gnueabihf); x86_64 uses the host gcc
# (with a static libc). A missing compiler skips that arch.  ONLY=x86_64 (or aarch64, armhf) builds one.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$HERE/../../stream
LD=$HERE/../dhstream/libdeflate
for t in aarch64:aarch64-linux-gnu-gcc:arm armhf:arm-linux-gnueabihf-gcc:arm x86_64:gcc:x86; do
    arch=${t%%:*}; rest=${t#*:}; cc=${rest%%:*}; cpu=${rest#*:}
    [ -z "$ONLY" ] || [ "$ONLY" = "$arch" ] || continue
    if [ "$arch" = x86_64 ] && [ "$(uname -m)" != x86_64 ]; then cc=x86_64-linux-gnu-gcc; fi
    command -v "$cc" >/dev/null 2>&1 || { echo "skipped dhcap-$arch: no $cc"; continue; }
    $cc -O2 -static -s -Wall -Wno-unused-function -I"$LD" -o "$OUT/dhcap-$arch" "$HERE/dhcap.c" \
        "$LD/lib/deflate_compress.c" "$LD/lib/zlib_compress.c" "$LD/lib/adler32.c" "$LD/lib/utils.c" \
        "$LD/lib/$cpu/cpu_features.c"
    echo "built $OUT/dhcap-$arch ($(wc -c < "$OUT/dhcap-$arch") bytes)"
done
