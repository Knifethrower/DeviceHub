#!/usr/bin/env python3
"""Unit test: the fbstream UDP protocol as DeviceHub reads it.

Sends a config packet and chunked, zlib-compressed frames to a locally bound StreamReceiver (the same
class the UI's live view serves from) and checks reassembly, lagged-frame discard, undo-pan rotation
(portrait panel on a landscape screen) and the BGR pixel order of the PNG it produces.
Import works at all thanks to the fb_receiver protocol details documented in devhubd.py.
"""
import os, socket, struct, sys, time, zlib

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import devhubd as dh


def fail(msg):
    print("FAIL: %s" % msg)
    sys.exit(1)


def pack_cfg(w, h, bpp):
    return struct.pack("!BIIH", 1, w, h, bpp)


def frame_packets(fid, raw, chunk=900):
    comp = zlib.compress(raw, 1)
    tot = max(1, (len(comp) + chunk - 1) // chunk)
    out = []
    for i in range(tot):
        part = comp[i * chunk:(i + 1) * chunk]
        out.append(struct.pack("!BIHHII", 2, fid, i, tot, len(part), len(raw)) + part)
    return out


def png_dims(png):
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    return struct.unpack(">II", png[16:24])


def png_rows(png):
    """The unfiltered rows: IHDR, then every IDAT, zlib-decoded. fb_to_png writes filter byte 0 rows."""
    i, dat = 8, b""
    while i < len(png):
        ln = struct.unpack(">I", png[i:i + 4])[0]
        tag, data = png[i + 4:i + 8], png[i + 8:i + 8 + ln]
        if tag == b"IDAT":
            dat += data
        i += 8 + ln + 4
    raw = zlib.decompress(dat)
    stride = 1 + png_dims(png)[0] * 3
    return [raw[y * stride:(y + 1) * stride] for y in range(png_dims(png)[1])]


def pixel(row, x):
    return tuple(row[1 + x * 3:1 + x * 3 + 3])


s = dh.StreamReceiver("t", 19871, 4, "drm")
dh.fb_to_jpeg = lambda *a, **k: None   # these assertions are about the PNG fallback bytes
s.start()
sb = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
time.sleep(0.2)

# a portrait frame (4x8, bpp 32) that the desk shows on a landscape screen ("8x4"): rotation expected.
# fb order BGR(X): red pixel = 0,0,255,255 in the device's bytes, blue = 255,0,0,255, rest a quiet grey.
W, H, bpp = 4, 8, 32
raw = bytearray(W * H * 4)          # a portrait frame (4x8) shown on a landscape screen ("8x4")


def put(x, y, px):
    raw[(y * W + x) * 4:(y * W + x) * 4 + 4] = bytes(px)


put(0, 0, (40, 40, 40, 255))
put(2, 5, (0, 0, 255, 255))         # rgb red, in the device's BGR bytes
put(2, 6, (255, 0, 0, 255))         # rgb blue
sb.sendto(pack_cfg(W, H, bpp), ("127.0.0.1", 19871))
time.sleep(0.1)
for p in frame_packets(7, bytes(raw)):
    sb.sendto(p, ("127.0.0.1", 19871))

end = time.time() + 2
while time.time() < end and s.frame is None:
    time.sleep(0.05)
if not s.frame:
    fail("no frame reassembled from the packets")
if s.frame[0] != 7:
    fail("wrong frame id: %d" % s.frame[0])
if s.cfg != (4, 8, 32):
    fail("config lost or wrong: %r" % (s.cfg,))
st = s.status()
if st["w"] != 4 or st["h"] != 8 or st["frames"] != 1:
    fail("status wrong: %r" % st)

mime, png = s.image({"screen": "8x4"})
if mime != "image/png":
    fail("expected the PNG fallback, got %s" % mime)
if not png:
    fail("png did not come out")
if png_dims(png) != (8, 4):
    fail("portrait frame not rotated to the landscape screen: %r" % (png_dims(png),))
rows = png_rows(png)
# rotate_cw maps the old column x to new row y=x and old y to x=old_h-1-y (same call the screenshot
# routes use, so the live view matches saved screenshots):
# grey (0,0) -> (0,7), red (2,5) -> (2,2), blue (2,6) -> (2,1)
if pixel(rows[2], 2) != (255, 0, 0):
    fail("red pixel not red at (2,2): %r" % (pixel(rows[2], 2),))
if pixel(rows[2], 1) != (0, 0, 255):
    fail("blue pixel not blue at (2,1): %r" % (pixel(rows[2], 1),))
if pixel(rows[0], 7) != (40, 40, 40):
    fail("background not grey at (0,7): %r" % (pixel(rows[0], 7),))

# fb_receiver's discard rules: a fully-old frame (done_fid - 10) and a late-completed stale frame do nothing
for p in frame_packets(20, bytes(raw)):
    sb.sendto(p, ("127.0.0.1", 19871))
time.sleep(0.2)
if s.frame[0] != 20:
    fail("frame 20 did not complete")
for p in frame_packets(9, bytes(raw)):          # 9 <= 20 - 10: straight to the bin
    sb.sendto(p, ("127.0.0.1", 19871))
time.sleep(0.2)
if s.frame[0] != 20:
    fail("a lagged frame (fid 9 after 20) was not discarded")

# a newer frame with a partial chunk count is cached, then a stale whole frame in between changes nothing
for p in frame_packets(103, bytes(raw)):
    sb.sendto(p, ("127.0.0.1", 19871))
time.sleep(0.2)
saved_fid, saved_raw = s.frame
for p in frame_packets(97, bytes(raw)):
    sb.sendto(p, ("127.0.0.1", 19871))
time.sleep(0.2)
if s.frame != (saved_fid, saved_raw):
    fail("a stale frame reassembled late overwrote a newer one")

# the png cache holds per fid: two polls of the same frame reuse the encoding, the next frame re-encodes
p1 = s.image({"screen": "8x4"})
p2 = s.image({"screen": "8x4"})
if p2 is not p1:
    fail("two polls of the same frame were not served from the cache")
next_frame = b"\x11\x22\x33\x44" * (W * H)
for p in frame_packets(104, next_frame):
    sb.sendto(p, ("127.0.0.1", 19871))
time.sleep(0.2)
if s.frame[0] != 104:
    fail("frame 104 did not complete")
second = s.image({"screen": "8x4"})
if second is p1:
    fail("a new frame was not re-encoded (needed for real image changes)")

s.close()
sb.close()
print("stream-receiver test OK")
