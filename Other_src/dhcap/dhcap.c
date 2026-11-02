/*
 * dhcap: the handheld's screen to DeviceHub over UDP, for devices without a Wayland compositor.
 * One static binary (no libraries on the device): build.sh makes aarch64 and armhf.
 *
 * It does what DeviceHub's screenshots do, continuously: it looks at every source the display could be showing
 * (each DRM plane in use, the CRTCs' framebuffers on kernels without universal planes, and /dev/fb0) and streams
 * the one that matches the device's screen and has content. A KMS front end (dArkOS, spruce) is on a DRM plane while
 * /dev/fb0 holds stale console text; on others (Allwinner, old Rockchip with a GPU) the plane is empty and fb0 is
 * the screen. The choice is looked at again every few seconds, so a game that switches route is followed. A screen
 * that is really one color still goes out (the hub needs a first frame).
 *
 * Packets are fbstream's (DeviceHub's StreamReceiver reads them):
 *   config  [u8=0x01][u32 width][u32 height][u16 bpp=16]          also every 2 s as the keepalive
 *   frame   [u8=0x02 key | 0x03 xor delta][u32 frame_id][u16 chunk][u16 total][u32 dlen][u32 orig]  zlib data
 * Frames are RGB565 (half of XRGB8888), sent only when the picture changed; a delta is the XOR with the last frame
 * sent (mostly zeros: tiny once compressed), with a key frame every 8 and whenever a delta isn't smaller.
 *
 * usage: dhcap <receiver_ip> <port> <fps> [auto|drm|fb] [screen_w screen_h] [-v]
 * exit:  2 bad usage, 3 nothing on the device can be read
 */
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <netinet/in.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>
#include "libdeflate.h"

#define CHUNK      1300            /* frame-chunk payload, under one MTU */
#define MAX_FPS    30
#define RESCAN_S   3               /* look at every source again this often */
#define KEY_EVERY  8

/* DRM ioctls by number (no libdrm), with the kernel's struct layouts */
struct dh_plane_res { uint64_t ids; uint32_t count, pad; };
struct dh_plane     { uint32_t id, crtc, fb, crtcs, gamma, nfmt; uint64_t fmts; };
struct dh_map_dumb  { uint32_t handle, pad; uint64_t offset; };
struct dh_card_res  { uint64_t fbs, crtcs, conns, encs; uint32_t nfb, ncrtc, nconn, nenc, minw, maxw, minh, maxh; };
struct dh_crtc      { uint64_t conns; uint32_t nconn, id, fb, x, y, gamma, valid; unsigned char mode[68]; };
struct dh_gem_close { uint32_t handle, pad; };
struct dh_prime     { uint32_t handle, flags; int32_t fd; };
struct dh_fb2       { uint32_t id, w, h, fmt, flags, handles[4], pitches[4], offsets[4]; uint64_t mods[4]; };
#define DRM_SET_CAP    _IOW('d', 0x0D, uint64_t[2])
#define DRM_GEM_CLOSE  _IOW('d', 0x09, struct dh_gem_close)
#define DRM_GETRES     _IOWR('d', 0xA0, struct dh_card_res)
#define DRM_GETCRTC    _IOWR('d', 0xA1, struct dh_crtc)
#define DRM_GETFB      _IOWR('d', 0xAD, uint32_t[7])
#define DRM_MAP_DUMB   _IOWR('d', 0xB3, struct dh_map_dumb)
#define DRM_PRIME_FD   _IOWR('d', 0x2D, struct dh_prime)
#define DRM_GETFB2     _IOWR('d', 0xCE, struct dh_fb2)
#define DRM_PLANERES   _IOWR('d', 0xB5, struct dh_plane_res)
#define DRM_GETPLANE   _IOWR('d', 0xB6, struct dh_plane)

static int verbose;
#define LOG(...) do { fprintf(stderr, "[dhcap] " __VA_ARGS__); fputc('\n', stderr); fflush(stderr); } while (0)
#define VLOG(...) do { if (verbose) LOG(__VA_ARGS__); } while (0)

/* ── output ─────────────────────────────────────────────────────────────────── */
static int sockfd;
static struct sockaddr_in dst;
static unsigned frame_id;
static struct libdeflate_compressor *deflater;
static unsigned char *zbuf;
static size_t zcap;

static void put32(unsigned char *p, uint32_t v) { v = htonl(v); memcpy(p, &v, 4); }
static void put16(unsigned char *p, uint16_t v) { v = htons(v); memcpy(p, &v, 2); }

static void send_config(int w, int h)
{
    unsigned char p[11];
    p[0] = 1; put32(p + 1, (uint32_t)w); put32(p + 5, (uint32_t)h); put16(p + 9, 16);
    sendto(sockfd, p, sizeof p, 0, (struct sockaddr *)&dst, sizeof dst);
}

/* compressed size, or 0 */
static size_t deflate_frame(const unsigned char *raw, size_t len)
{
    size_t need = libdeflate_zlib_compress_bound(deflater, len);
    if (need > zcap) {
        free(zbuf);
        zbuf = malloc(need);
        zcap = zbuf ? need : 0;
        if (!zbuf) return 0;
    }
    return libdeflate_zlib_compress(deflater, raw, len, zbuf, zcap);
}

static void send_chunks(int kind, size_t clen, size_t orig)
{
    unsigned char out[17 + CHUNK];
    unsigned total = (unsigned)((clen + CHUNK - 1) / CHUNK), i;
    if (!total) total = 1;
    frame_id++;
    for (i = 0; i < total; i++) {
        size_t off = (size_t)i * CHUNK, n = clen - off > CHUNK ? CHUNK : clen - off;
        out[0] = (unsigned char)kind;
        put32(out + 1, frame_id); put16(out + 5, (uint16_t)i); put16(out + 7, (uint16_t)total);
        put32(out + 9, (uint32_t)n); put32(out + 13, (uint32_t)orig);
        memcpy(out + 17, zbuf + off, n);
        sendto(sockfd, out, 17 + n, 0, (struct sockaddr *)&dst, sizeof dst);
    }
}

/* ── pixels ─────────────────────────────────────────────────────────────────── */
/* one row of the source (XRGB8888 / RGB888 little-endian, or RGB565) to RGB565 */
static void row_to_565(uint16_t *d, const unsigned char *s, int w, int bpp)
{
    int x;
    if (bpp == 16) { memcpy(d, s, (size_t)w * 2); return; }
    if (bpp == 32) {
        const uint32_t *p = (const uint32_t *)s;
        for (x = 0; x < w; x++) {
            uint32_t v = p[x];
            d[x] = (uint16_t)(((v >> 8) & 0xF800) | ((v >> 5) & 0x07E0) | ((v >> 3) & 0x001F));
        }
        return;
    }
    for (x = 0; x < w; x++, s += 3)
        d[x] = (uint16_t)(((s[2] & 0xF8) << 8) | ((s[1] & 0xFC) << 3) | (s[0] >> 3));
}

/* how much of a picture isn't one color: 0 = all one color, up to 64 */
static int content(const uint16_t *px, size_t n)
{
    size_t i, step = n / 61 ? n / 61 : 1;
    int diff = 0;
    for (i = step; i < n && diff < 64; i += step)
        if (px[i] != px[0]) diff++;
    return diff;
}

/* ── sources ────────────────────────────────────────────────────────────────── */
enum { SRC_PLANE, SRC_CRTC, SRC_FB };
struct src { int kind, card; uint32_t id; };
static const char *kind_name[] = { "drm plane", "drm crtc", "fb0" };

static int cards[4] = { -1, -1, -1, -1 };

/* one DRM framebuffer kept mapped; page flipping alternates a few, so a small cache keeps them */
struct fbmap { int card, dmafd; uint32_t fb, handle; unsigned char *m; size_t len; int w, h, pitch, bpp; unsigned used; };
static struct fbmap maps[6];
static unsigned map_clock;

static void fbmap_drop(struct fbmap *e)
{
    if (e->m) munmap(e->m, e->len);
    if (e->dmafd > 0) close(e->dmafd);
    if (e->handle) {
        struct dh_gem_close c = { e->handle, 0 };
        ioctl(cards[e->card], DRM_GEM_CLOSE, &c);       /* GETFB made a handle: give it back */
    }
    memset(e, 0, sizeof *e);
}

static struct fbmap *fbmap_get(int card, uint32_t fb)
{
    struct fbmap *e, *old = &maps[0];
    uint32_t f[7] = { 0 };
    struct dh_map_dumb md = { 0 };
    size_t i;
    for (i = 0; i < sizeof maps / sizeof *maps; i++) {
        e = &maps[i];
        if (e->m && e->card == card && e->fb == fb) { e->used = ++map_clock; return e; }
        if (e->used < old->used) old = e;
    }
    f[0] = fb;
    if (ioctl(cards[card], DRM_GETFB, f) || !f[6]) { VLOG("GETFB %u: %s", fb, f[6] ? strerror(errno) : "no handle (not root?)"); return NULL; }
    if (f[4] != 16 && f[4] != 24 && f[4] != 32) {
        struct dh_gem_close c = { f[6], 0 };
        ioctl(cards[card], DRM_GEM_CLOSE, &c);
        return NULL;
    }
    {   /* a buffer the GPU tiled or compressed (a format modifier) can't be read as rows */
        struct dh_fb2 f2 = { 0 };
        f2.id = fb;
        if (ioctl(cards[card], DRM_GETFB2, &f2) == 0 && f2.mods[0] && f2.mods[0] != 0x00ffffffffffffffULL) {
            struct dh_gem_close c = { f[6], 0 };
            uint32_t h2;
            VLOG("fb %u: modifier %llx, not linear: can't read it", fb, (unsigned long long)f2.mods[0]);
            ioctl(cards[card], DRM_GEM_CLOSE, &c);
            for (h2 = 0; h2 < 4; h2++)
                if (f2.handles[h2] && f2.handles[h2] != f[6]) { c.handle = f2.handles[h2]; ioctl(cards[card], DRM_GEM_CLOSE, &c); }
            return NULL;
        }
        if (f2.handles[0] && f2.handles[0] != f[6]) {     /* GETFB2 made its own handle */
            struct dh_gem_close c = { f2.handles[0], 0 };
            ioctl(cards[card], DRM_GEM_CLOSE, &c);
        }
    }
    md.handle = f[6];
    fbmap_drop(old);
    old->card = card; old->fb = fb; old->handle = f[6];
    old->len = (size_t)f[3] * f[2];
    if (ioctl(cards[card], DRM_MAP_DUMB, &md) == 0) {
        old->m = mmap(NULL, old->len, PROT_READ, MAP_SHARED, cards[card], (off_t)md.offset);
    } else {
        /* not a dumb buffer (the GPU drew it: dArkOS's ES): export it as a dma-buf and map that */
        struct dh_prime pr = { f[6], 02000000 /* O_CLOEXEC */, -1 };
        VLOG("MAP_DUMB %u: %s; trying the dma-buf", fb, strerror(errno));
        if (ioctl(cards[card], DRM_PRIME_FD, &pr) || pr.fd < 0) { VLOG("PRIME export %u: %s", fb, strerror(errno)); fbmap_drop(old); return NULL; }
        old->dmafd = pr.fd;
        old->m = mmap(NULL, old->len, PROT_READ, MAP_SHARED, pr.fd, 0);
    }
    if (old->m == MAP_FAILED) { VLOG("mmap fb %u: %s", fb, strerror(errno)); old->m = NULL; fbmap_drop(old); return NULL; }
    old->w = (int)f[1]; old->h = (int)f[2]; old->pitch = (int)f[3]; old->bpp = (int)f[4];
    old->used = ++map_clock;
    return old;
}

/* /dev/fb0, mapped once */
static int fb_fd = -1;
static unsigned char *fb_mem;
static size_t fb_len;

static int fb_open(void)
{
    struct fb_fix_screeninfo fi;
    if (fb_fd >= 0) return 0;
    fb_fd = open("/dev/fb0", O_RDONLY);
    if (fb_fd < 0) return -1;
    if (ioctl(fb_fd, FBIOGET_FSCREENINFO, &fi) || !fi.smem_len) { close(fb_fd); fb_fd = -1; return -1; }
    fb_len = fi.smem_len;
    fb_mem = mmap(NULL, fb_len, PROT_READ, MAP_SHARED, fb_fd, 0);
    if (fb_mem == MAP_FAILED) { fb_mem = NULL; close(fb_fd); fb_fd = -1; return -1; }
    return 0;
}

/* the frame a source shows now, as RGB565 in *out; 0 ok */
static uint16_t *frame;
static size_t frame_cap;
static int fw, fh;

static int grab(const struct src *s)
{
    const unsigned char *base;
    int w, h, pitch, bpp, y;
    if (s->kind == SRC_FB) {
        struct fb_var_screeninfo v;
        struct fb_fix_screeninfo fi;
        if (fb_open() || ioctl(fb_fd, FBIOGET_VSCREENINFO, &v) || ioctl(fb_fd, FBIOGET_FSCREENINFO, &fi)) return -1;
        w = (int)v.xres; h = (int)v.yres; bpp = (int)v.bits_per_pixel; pitch = (int)fi.line_length;
        if (bpp != 16 && bpp != 24 && bpp != 32) return -1;
        if (!pitch) pitch = w * bpp / 8;
        if ((size_t)pitch * (v.yoffset + (unsigned)h) > fb_len) return -1;
        base = fb_mem + (size_t)pitch * v.yoffset + (size_t)v.xoffset * (bpp / 8);
    } else {
        uint32_t fb;
        struct fbmap *e;
        if (s->kind == SRC_PLANE) {
            struct dh_plane p = { 0 };
            p.id = s->id;
            if (ioctl(cards[s->card], DRM_GETPLANE, &p) || !p.fb || !p.crtc) return -1;
            fb = p.fb;
        } else {
            struct dh_crtc c = { 0 };
            c.id = s->id;
            if (ioctl(cards[s->card], DRM_GETCRTC, &c) || !c.fb || !c.valid) return -1;   /* an output that's off */
            fb = c.fb;
        }
        e = fbmap_get(s->card, fb);
        if (!e) return -1;
        base = e->m; w = e->w; h = e->h; pitch = e->pitch; bpp = e->bpp;
    }
    if (w <= 0 || h <= 0 || w > 8192 || h > 8192) return -1;
    if ((size_t)w * h > frame_cap) {
        uint16_t *n = realloc(frame, (size_t)w * h * 2);
        if (!n) return -1;
        frame = n; frame_cap = (size_t)w * h;
    }
    for (y = 0; y < h; y++)
        row_to_565(frame + (size_t)y * w, base + (size_t)y * pitch, w, bpp);
    fw = w; fh = h;
    return 0;
}

/* every source the display could be showing */
static struct src cands[64];
static int ncands;

static void find_sources(int mode)
{
    int c;
    ncands = 0;
    if (mode != 2) {
        for (c = 0; c < 4; c++) {
            char path[32];
            uint64_t cap[2] = { 2, 1 };          /* universal planes: the primary plane too */
            struct dh_plane_res r = { 0 };
            if (cards[c] < 0) {
                snprintf(path, sizeof path, "/dev/dri/card%d", c);
                cards[c] = open(path, O_RDWR);
                if (cards[c] < 0) continue;
                ioctl(cards[c], DRM_SET_CAP, cap);
            }
            if (ioctl(cards[c], DRM_PLANERES, &r) == 0 && r.count && r.count <= 32) {
                uint32_t ids[32];
                unsigned i;
                r.ids = (uint64_t)(uintptr_t)ids;
                if (ioctl(cards[c], DRM_PLANERES, &r) == 0)
                    for (i = 0; i < r.count && ncands < 60; i++)
                        cands[ncands++] = (struct src){ SRC_PLANE, c, ids[i] };
            } else {                             /* no planes (old kernels, some drivers): the CRTCs' framebuffers */
                struct dh_card_res res = { 0 };
                if (ioctl(cards[c], DRM_GETRES, &res) == 0 && res.ncrtc && res.ncrtc <= 8) {
                    uint32_t crtcs[8];
                    unsigned i;
                    memset(&res, 0, sizeof res);
                    res.ncrtc = 8;
                    res.crtcs = (uint64_t)(uintptr_t)crtcs;
                    if (ioctl(cards[c], DRM_GETRES, &res) == 0)
                        for (i = 0; i < res.ncrtc && i < 8 && ncands < 60; i++)
                            cands[ncands++] = (struct src){ SRC_CRTC, c, crtcs[i] };
                }
            }
        }
    }
    if (mode != 1 && access("/dev/fb0", R_OK) == 0)
        cands[ncands++] = (struct src){ SRC_FB, 0, 0 };
}

/* the best source now: the device's own screen size first (either orientation), then content, then DRM over fb0
   (fb0 can hold stale console text), then size. -1 if none can be read. */
static int pick(int mode, int want_w, int want_h, int *score_out)
{
    int i, best = -1, bscore = -1;
    find_sources(mode);
    for (i = 0; i < ncands; i++) {
        int sc, match, cont;
        if (grab(&cands[i])) continue;
        match = want_w ? (fw == want_w && fh == want_h) || (fw == want_h && fh == want_w) : 0;
        cont = content(frame, (size_t)fw * fh);
        sc = match * 1000000 + (cont ? 500000 : 0) + (cands[i].kind != SRC_FB) * 250000 + (fw * fh > 249999 ? 249999 : fw * fh);
        VLOG("%s %u: %dx%d, content %d, score %d", kind_name[cands[i].kind], cands[i].id, fw, fh, cont, sc);
        if (sc > bscore) { bscore = sc; best = i; }
    }
    *score_out = bscore;
    return best;
}

static void on_term(int s) { (void)s; _exit(0); }

static double now_s(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec / 1e9;
}

int main(int argc, char **argv)
{
    int fps, mode = 0, want_w = 0, want_h = 0, i, cur, cur_score, ow = 0, oh = 0, fails = 0;
    unsigned sent = 0;
    uint16_t *prev = NULL, *delta = NULL;
    size_t prev_cap = 0;
    double next_scan, last_cfg = 0, t0;
    struct src cur_src;

    for (i = 1; i < argc; i++)
        if (!strcmp(argv[i], "-v")) { verbose = 1; memmove(argv + i, argv + i + 1, (size_t)(argc - i) * sizeof *argv); argc--; i--; }
    if (argc < 4) {
        fprintf(stderr, "usage: %s <receiver_ip> <port> <fps> [auto|drm|fb] [screen_w screen_h] [-v]\n", argv[0]);
        return 2;
    }
    fps = atoi(argv[3]);
    if (fps < 1) fps = 1;
    if (fps > MAX_FPS) fps = MAX_FPS;
    if (argc > 4) mode = !strcmp(argv[4], "drm") ? 1 : !strcmp(argv[4], "fb") ? 2 : 0;
    if (argc > 6) { want_w = atoi(argv[5]); want_h = atoi(argv[6]); }

    memset(&dst, 0, sizeof dst);
    dst.sin_family = AF_INET;
    dst.sin_port = htons((uint16_t)atoi(argv[2]));
    if (inet_aton(argv[1], &dst.sin_addr) != 1) { LOG("bad address %s", argv[1]); return 2; }
    sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (sockfd < 0) { LOG("socket: %s", strerror(errno)); return 3; }
    { int b = 4 << 20; setsockopt(sockfd, SOL_SOCKET, SO_SNDBUF, &b, sizeof b); }
    deflater = libdeflate_alloc_compressor(1);
    signal(SIGTERM, on_term);
    signal(SIGINT, on_term);
    signal(SIGPIPE, SIG_IGN);

    cur = pick(mode, want_w, want_h, &cur_score);
    if (cur < 0) { LOG("nothing to capture: no readable DRM plane or /dev/fb0 (root needed for DRM)"); return 3; }
    cur_src = cands[cur];
    LOG("streaming %s %u (%dx%d) to %s:%s at %d fps", kind_name[cur_src.kind], cur_src.id, fw, fh, argv[1], argv[2], fps);
    next_scan = now_s() + RESCAN_S;

    for (;;) {
        size_t n, clen;
        t0 = now_s();
        if (t0 >= next_scan) {                    /* the display may show something else now (a game started) */
            int sc, b = pick(mode, want_w, want_h, &sc);
            if (b >= 0 && (cands[b].kind != cur_src.kind || cands[b].id != cur_src.id) && sc > cur_score) {
                cur_src = cands[b];
                LOG("now %s %u (%dx%d)", kind_name[cur_src.kind], cur_src.id, fw, fh);
            }
            cur_score = sc;
            next_scan = t0 + RESCAN_S;
        }
        if (grab(&cur_src)) {
            if (++fails > 3 * fps) next_scan = 0;  /* it went away: find another */
            goto pace;
        }
        fails = 0;
        n = (size_t)fw * fh;
        if (fw != ow || fh != oh) {               /* new size: config first, then a key frame */
            send_config(fw, fh);
            last_cfg = t0;
            ow = fw; oh = fh;
            sent = 0;
        } else if (prev && !memcmp(prev, frame, n * 2)) {
            if (t0 - last_cfg >= 2) { send_config(fw, fh); last_cfg = t0; }   /* keepalive on a still screen */
            goto pace;
        }
        if (n > prev_cap) {
            free(prev); free(delta);
            prev = malloc(n * 2); delta = malloc(n * 2);
            prev_cap = prev && delta ? n : 0;
            if (!prev_cap) { LOG("out of memory"); return 3; }
            sent = 0;
        }
        if (sent % KEY_EVERY) {                   /* delta: the XOR with the last frame, mostly zeros */
            size_t k, dlen;
            for (k = 0; k < n; k++) delta[k] = frame[k] ^ prev[k];
            dlen = deflate_frame((unsigned char *)delta, n * 2);
            if (dlen && dlen < n / 2) {           /* under a quarter of the raw frame: worth it */
                send_chunks(3, dlen, n * 2);
                goto sent_one;
            }
        }
        clen = deflate_frame((unsigned char *)frame, n * 2);
        if (!clen) goto pace;
        send_chunks(2, clen, n * 2);
sent_one:
        memcpy(prev, frame, n * 2);
        sent++;
        last_cfg = t0;                            /* frames keep the receiver's keepalive fresh too */
pace:
        {
            double left = 1.0 / fps - (now_s() - t0);
            if (left > 0) usleep((useconds_t)(left * 1e6));
        }
    }
}
