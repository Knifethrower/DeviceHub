#!/bin/sh
# Builds helper/dhinput-aarch64 and helper/dhinput-x86_64: static, so they run on any CFW's libc.
# aarch64 needs the cross compiler (Debian: gcc-aarch64-linux-gnu); x86_64 uses the host gcc (with a static libc).
# A missing compiler skips that arch.  ONLY=x86_64 (or aarch64) builds one.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=$HERE/../../helper
for t in aarch64:aarch64-linux-gnu-gcc x86_64:gcc; do
    arch=${t%%:*}; cc=${t#*:}
    [ -z "$ONLY" ] || [ "$ONLY" = "$arch" ] || continue
    if [ "$arch" = x86_64 ] && [ "$(uname -m)" != x86_64 ]; then cc=x86_64-linux-gnu-gcc; fi
    command -v "$cc" >/dev/null 2>&1 || { echo "skipped dhinput-$arch: no $cc"; continue; }
    $cc -O2 -static -s -Wall -o "$OUT/dhinput-$arch" "$HERE/dhinput.c"
    echo "built $OUT/dhinput-$arch ($(wc -c < "$OUT/dhinput-$arch") bytes)"
done
