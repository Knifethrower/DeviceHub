/*
 * dhstream — capture the handheld's display and stream it to DeviceHub over UDP,
 * in the same packets fbstream uses (fb_receiver.py reads both):
 *
 *   config  [u8=0x01][u32 width][u32 height][u16 bpp]
 *   frame   [u8=0x02][u32 frame_id][u16 chunk][u16 total][u32 dlen][u32 orig]  zlib data
 *
 * Routes:
 *   way  the Wayland compositor's own picture, one `grim -t ppm -` per frame
 *        (sway scans out through DRM, so /dev/fb0 stays blank there)
 *   drm  what the display scans out straight from the kernel: legacy DRM
 *        dumb-buffer reads (GETPLANES/GETPLANE/GETFB/MAP_DUMB), so it also works
 *        on the old Rockchip 4.4 kernels where GETFB2-based tools fail
 *   fb   /dev/fb0 mmap (the plain framebuffer console route)
 *
 * Frames go out only when the screen content changed, so an idle menu costs
 * little more than the capture itself.
 *
 * usage: dhstream <receiver_ip> <port> <fps> <way|drm|fb> [wayland_dir wayland_socket] [want_w want_h]
 * exit:  0 running, 2 bad usage, 3 the capture route produced nothing
 */
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <linux/fb.h>
#include <netinet/in.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include <wayland-client.h>
#include "screencopy-unstable-v1.h"
#include "libdeflate/libdeflate.h"
#include <zlib.h>
#if defined(__ARM_NEON) || defined(__ARM_NEON__)
#include <arm_neon.h>
#define DH_NEON 1
#endif

#define CHUNK    1300                 /* frame-chunk payload, under one MTU */
#define MAX_FPS  30
#define MIN_FPS  1
#define MAX_FAIL 20                   /* consecutive route failures before giving up */
#define GRIM_WAIT 400                 /* ms per pipe poll: sway commits no frame on a static screen, so a
                                         grim hang means "nothing changed on the output" */

/* ── DRM ioctls (plain numbers, no libdrm: the calls the old kernels have) ────── */
#define DRM_SET_CLIENT_CAP  _IOW('d', 0x0D, unsigned long long[2])
#define DRM_GET_PLANERES    _IOWR('d', 0xB5, struct drm_plane_res)
#define DRM_GET_PLANE       _IOWR('d', 0xB6, struct drm_plane)
#define DRM_GET_FB          _IOWR('d', 0xAD, unsigned int[7])
#define DRM_MAP_DUMB        _IOWR('d', 0xB3, struct drm_map_dumb)
struct drm_plane_res { unsigned long long ids; unsigned int count, pad; };
struct drm_plane     { unsigned int id, crtc, fb, crtc_mask, gamma, nfmt; unsigned long long fmts; };
struct drm_map_dumb  { unsigned int handle, pad; unsigned long long offset; };

static int sockfd = -1;
static struct sockaddr_in dst;
static unsigned frame_id;

static void die(const char *what)
{
    fprintf(stderr, "[dhstream] %s: %s\n", what, errno ? strerror(errno) : "?");
    exit(3);
}

static void send_dgram(const void *p, size_t n)
{
    sendto(sockfd, p, n, 0, (struct sockaddr *)&dst, sizeof dst);
}

static void send_config_quiet(int w, int h, int bpp)
{
    unsigned char p[11];
    unsigned int be = htonl((unsigned)w), be2 = htonl((unsigned)h);
    unsigned short bp = htons((unsigned short)bpp);
    p[0] = 0x01;
    memcpy(p + 1, &be, 4);
    memcpy(p + 5, &be2, 4);
    memcpy(p + 9, &bp, 2);
    send_dgram(p, sizeof p);
}

static void send_config(int w, int h, int bpp)
{
    send_config_quiet(w, h, bpp);
    fprintf(stderr, "[dhstream] config %dx%d @ %dbpp\n", w, h, bpp);
}

static void maybe_config(int w, int h, int bpp)
{
    static int cw = -1, ch = -1, cb = -1;
    if (w != cw || h != ch || bpp != cb) {
        cw = w; ch = h; cb = bpp;
        send_config(w, h, bpp);
    }
}

/* ── one compressed frame, split into sendable chunks ────────────────────────── */
static unsigned char *comp;
static size_t compcap;
static struct libdeflate_compressor *compressor;

static void send_frame(int kind, const unsigned char *raw, size_t len)
{
    static unsigned char hdr[17], out[CHUNK + 17];
    uLongf clen;
    unsigned i, total, off, n;
    unsigned int be_id, be_orig;

    if (len + 64 > compcap) {
        compcap = compressBound((uLong)len);
        comp = realloc(comp, compcap);
        if (!comp) die("out of memory");
    }
    clen = compcap;
    if (compress2(comp, &clen, raw, len, 1) != Z_OK) return;

    frame_id++;
    be_id = htonl(frame_id);
    be_orig = htonl((unsigned)len);
    total = (unsigned)((clen + CHUNK - 1) / CHUNK);
    if (!total) total = 1;
    for (i = 0; i < total; i++) {
        unsigned int be_n;
        unsigned short be_i = htons(i), be_t = htons(total);
        off = i * CHUNK;
        n = (unsigned)(clen - off > CHUNK ? CHUNK : clen - off);
        be_n = htonl(n);
        hdr[0] = (unsigned char)kind;
        memcpy(hdr + 1, &be_id, 4);
        memcpy(hdr + 5, &be_i, 2);
        memcpy(hdr + 7, &be_t, 2);
        memcpy(hdr + 9, &be_n, 4);
        memcpy(hdr + 13, &be_orig, 4);
        memcpy(out, hdr, 17);
        memcpy(out + 17, comp + off, n);
        send_dgram(out, 17 + n);
    }
}

/* ── frame store: prev holds the last frame that went out ────────────────────── */
static unsigned char *prev;
static size_t prevcap;

static unsigned char *grow(unsigned char **buf, size_t *cap, size_t need)
{
    if (need > *cap) {
        unsigned char *p = realloc(*buf, need);
        if (!p) die("out of memory");
        *buf = p;
        *cap = need;
        memset(p, 0xA5, need);
    }
    return *buf;
}

/* send when the content differs from the last one; 1 = something went out */
static int push(int w, int h, int bpp, const unsigned char *rows, size_t len)
{
    if (!prev || len != prevcap || memcmp(rows, prev, len)) {
        grow(&prev, &prevcap, len);
        memcpy(prev, rows, len);
        maybe_config(w, h, bpp);
        send_frame(2, rows, len);
        return 1;
    }
    return 0;
}

static void pace(const struct timespec *t0, int fps)
{
    long period = 1000000000L / fps, dt;
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    dt = (now.tv_sec - t0->tv_sec) * 1000000000L + (now.tv_nsec - t0->tv_nsec);
    if (dt < period) usleep((useconds_t)((period - dt) / 1000));
}

static void on_term(int s) { (void)s; _exit(0); }

static int sample_uniq(const unsigned char *m, size_t len)
{
    size_t j;
    for (j = 4099; j < len; j += 4099)
        if (m[j] != m[0]) return 1;
    return 0;
}

static int bpp_ok(int bpp) { return bpp == 16 || bpp == 24 || bpp == 32; }

/* ── fb: mmap the framebuffer device ─────────────────────────────────────────── */
static int fb_loop(int fps)
{
    int fd = open("/dev/fb0", O_RDONLY);
    struct fb_var_screeninfo v;
    struct fb_fix_screeninfo f;
    unsigned char *map;
    size_t maplen, stride;
    int px;
    static unsigned char *buf;
    static size_t bufcap;

    if (fd < 0) return 3;
    if (ioctl(fd, FBIOGET_VSCREENINFO, &v) || ioctl(fd, FBIOGET_FSCREENINFO, &f)) return 3;
    if (!bpp_ok(v.bits_per_pixel)) return 3;
    px = v.bits_per_pixel / 8;
    stride = f.line_length ? f.line_length : (size_t)v.xres * px;
    maplen = f.smem_len ? f.smem_len : stride * v.yres_virtual;
    map = mmap(NULL, maplen, PROT_READ, MAP_SHARED, fd, 0);
    if (map == MAP_FAILED) return 3;
    fprintf(stderr, "[dhstream] fb %ux%u @%dbpp line=%u\n", v.xres, v.yres, v.bits_per_pixel, (unsigned)stride);
    fflush(stderr);

    for (;;) {
        struct timespec t0;
        struct fb_var_screeninfo nv;
        size_t avail, len;
        int w, h;
        unsigned char *packed;
        clock_gettime(CLOCK_MONOTONIC, &t0);

        /* reread the small part: the pan offset moves on double-buffered consoles,
           and whole mode switches (xres/bpp) need the fresh geometry too */
        if (ioctl(fd, FBIOGET_VSCREENINFO, &nv) == 0) {
            if ((int)nv.xres == (int)v.xres && (int)nv.yres == (int)v.yres &&
                (int)nv.bits_per_pixel == (int)v.bits_per_pixel) {
                v.yoffset = nv.yoffset;
            } else if (bpp_ok(nv.bits_per_pixel)) {
                struct fb_fix_screeninfo nf;
                if (ioctl(fd, FBIOGET_FSCREENINFO, &nf) == 0) {
                    v = nv;
                    f = nf;
                    px = v.bits_per_pixel / 8;
                    stride = f.line_length ? f.line_length : (size_t)v.xres * px;
                    if (f.smem_len && f.smem_len != maplen) {
                        munmap(map, maplen);
                        maplen = f.smem_len;
                        map = mmap(NULL, maplen, PROT_READ, MAP_SHARED, fd, 0);
                        if (map == MAP_FAILED) return 3;
                    }
                }
            }
        }
        w = (int)v.xres;
        h = (int)v.yres;
        avail = maplen > (size_t)v.yoffset * stride ? maplen - (size_t)v.yoffset * stride : 0;
        if ((size_t)h * stride > avail) h = (int)(avail / stride);
        if (w <= 0 || h <= 0) { pace(&t0, fps); continue; }

        len = (size_t)w * px * h;
        packed = grow(&buf, &bufcap, len);
        if ((int)stride == w * px) {
            memcpy(packed, map + (size_t)v.yoffset * stride, len);
        } else {
            int y;                          /* row padding: pack the visible pixels tight */
            for (y = 0; y < h; y++)
                memcpy(packed + (size_t)y * w * px, map + ((size_t)v.yoffset + y) * stride, (size_t)w * px);
        }
        if (sample_uniq(packed, len))       /* uniform screens are not sent (idle stays cheap) */
            push(w, h, px * 8, packed, len);
        pace(&t0, fps);
    }
}

/* helper: read one plane's current buffer; 0 ok, -1 skip */
static int read_plane(int fd, unsigned int pid, unsigned char **out, size_t *outcap,
                      int *w, int *h, int *bpp, int *uniq)
{
    struct drm_plane pl = { 0 };
    unsigned int fb[7] = { 0 };
    struct drm_map_dumb map = { 0 };
    unsigned char *m;
    size_t len, pitch;

    pl.id = pid;
    if (ioctl(fd, DRM_GET_PLANE, &pl) || !pl.crtc || !pl.fb) return -1;
    fb[0] = pl.fb;
    if (ioctl(fd, DRM_GET_FB, fb) || !fb[6]) return -1;
    if (!bpp_ok((int)fb[4])) return -1;
    map.handle = fb[6];
    if (ioctl(fd, DRM_MAP_DUMB, &map)) return -1;
    pitch = fb[3];
    len = pitch * fb[2];
    m = mmap(NULL, len, PROT_READ, MAP_SHARED, fd, map.offset);
    if (m == MAP_FAILED) return -1;
    *w = (int)fb[1];
    *h = (int)fb[2];
    *bpp = (int)fb[4];
    *uniq = sample_uniq(m, len);
    if (pitch == (size_t)fb[1] * (fb[4] / 8)) {
        unsigned char *p = grow(out, outcap, len);
        memcpy(p, m, len);
    } else {
        int y, px = (int)fb[4] / 8;
        unsigned char *p = grow(out, outcap, (size_t)fb[1] * px * fb[2]);
        for (y = 0; y < (int)fb[2]; y++)
            memcpy(p + (size_t)y * fb[1] * px, m + (size_t)y * pitch, (size_t)fb[1] * px);
    }
    munmap(m, len);
    return 0;
}

/* plane score: matching the device's own screen first (either orientation),
   then planes with actual content, then the biggest */
static int plane_score(int w, int h, int uniq, int want_w, int want_h)
{
    if (!want_w) return uniq * 2;       /* no known screen: only content counts */
    {
        int s = (w == want_w && h == want_h) ? 2 : (w == want_h && h == want_w) ? 1 : 0;
        return s * 2 + (uniq ? 1 : 0);
    }
}

/* ── drm: read the plane the display scans out ───────────────────────────────── */
static int drm_loop(int fps, int want_w, int want_h)
{
    int fd = open("/dev/dri/card0", O_RDWR);
    unsigned long long cap[2] = { 2, 1 };          /* universal planes */
    unsigned int *ids = NULL;
    unsigned int nids = 0;
    static unsigned char *best, *slurp;
    static size_t bestcap, slurpcap;
    int fails = 0;

    if (fd < 0) return 3;
    ioctl(fd, DRM_SET_CLIENT_CAP, cap);            /* older kernels may not know it */
    fprintf(stderr, "[dhstream] drm probing planes\n");
    fflush(stderr);

    for (;;) {
        struct timespec t0;
        struct drm_plane_res res;
        int bw = 0, bh = 0, bbpp = 0, buniq = 0, bscore = -1, w, h, bpp, uniq;
        unsigned i;
        clock_gettime(CLOCK_MONOTONIC, &t0);

        memset(&res, 0, sizeof res);
        if (ioctl(fd, DRM_GET_PLANERES, &res) < 0 || !res.count || res.count > 64) goto fail;
        if (res.count != nids) {
            unsigned int *n = realloc(ids, (size_t)res.count * sizeof *n);
            if (!n) die("out of memory");
            ids = n;
            nids = res.count;
        }
        res.ids = (unsigned long long)(uintptr_t)ids;
        if (ioctl(fd, DRM_GET_PLANERES, &res) < 0) goto fail;

        for (i = 0; i < nids; i++) {
            int sc;
            if (read_plane(fd, ids[i], &slurp, &slurpcap, &w, &h, &bpp, &uniq)) continue;
            sc = plane_score(w, h, uniq, want_w, want_h);
            if (sc > bscore || (sc == bscore && uniq > buniq) ||
                (sc == bscore && uniq == buniq && w * h > bw * bh)) {
                unsigned char *t = best;          /* keep this one's rows */
                size_t tc = bestcap;
                best = slurp; bestcap = slurpcap;
                slurp = t; slurpcap = tc;
                bw = w; bh = h; bbpp = bpp; buniq = uniq; bscore = sc;
            }
        }
        if (buniq) {
            size_t len = (size_t)bw * (bbpp / 8) * bh;
            static unsigned char *prev = NULL, *xorbuf = NULL;
            static size_t prev_cap = 0, xor_cap = 0;
            static unsigned sent_count = 0;
            int use_delta = 0;
            /* change detection */
            if (prev && prev_cap == len && !memcmp(best, prev, len)) {
                pace(&t0, fps);
                continue;
            }
            /* delta encoding */
            if (prev && prev_cap == len && sent_count % 8 != 0) {
                uLongf clen;
                size_t k;
                if (len > xor_cap) { xorbuf = realloc(xorbuf, len); xor_cap = len; }
                for (k = 0; k < len; k++) xorbuf[k] = (unsigned char)(best[k] ^ prev[k]);
                clen = compressBound((uLong)len);
                {
                    unsigned char *tmp = malloc(clen);
                    if (tmp && compress2(tmp, &clen, xorbuf, len, 1) == Z_OK && clen < len / 4) {
                        use_delta = 1;
                    }
                    free(tmp);
                }
            }
            if (use_delta) send_frame(3, xorbuf, len);
            else send_frame(2, best, len);
            if (len > prev_cap) { prev = realloc(prev, len); prev_cap = len; }
            memcpy(prev, best, len);
            sent_count++;
            fails = 0;
        } else {
        }
        pace(&t0, fps);
        continue;
fail:
        if (++fails > MAX_FAIL) return 3;
        pace(&t0, fps);
    }
}

/* ── way: grim shoots the compositor's picture; parse PPM, swap to BGR, send ─── */
static int way_loop(int fps, const char *dir, const char *sockn)
{
    static unsigned char *cur;
    int fails = 0;

    setenv("XDG_RUNTIME_DIR", dir, 1);
    setenv("WAYLAND_DISPLAY", sockn, 1);
    fprintf(stderr, "[dhstream] wayland %s / %s\n", dir, sockn);
    fflush(stderr);

    for (;;) {
        struct timespec t0;
        int pfd[2], st = 0, w = 0, h = 0, maxv = 0;
        size_t got = 0, cap = 0, hdrlen;
        ssize_t n;
        pid_t pid;
        clock_gettime(CLOCK_MONOTONIC, &t0);

        if (pipe(pfd)) die("pipe");
        pid = fork();
        if (pid < 0) die("fork");
        if (pid == 0) {
            dup2(pfd[1], STDOUT_FILENO);
            close(pfd[0]); close(pfd[1]);
            signal(SIGTERM, SIG_DFL);
            execlp("grim", "grim", "-t", "ppm", "-", (char *)NULL);
            _exit(127);
        }
        close(pfd[1]);
        {
            int stale = 0;
            struct timespec capture_start;
            clock_gettime(CLOCK_MONOTONIC, &capture_start);
            for (;;) {
                struct timespec now;
                long elapsed;
                clock_gettime(CLOCK_MONOTONIC, &now);
                elapsed = (now.tv_sec - capture_start.tv_sec) * 1000 +
                          (now.tv_nsec - capture_start.tv_nsec) / 1000000;
                if (elapsed > GRIM_WAIT) {
                    stale = 1;
                    fprintf(stderr, "[dhstream] grim timeout (static)\\n");
                    kill(pid, SIGKILL);
                    break;
                }
                struct pollfd pf = { pfd[0], POLLIN, 0 };
                int remaining = GRIM_WAIT - (int)elapsed;
                if (remaining < 10) remaining = 10;
                int pr = poll(&pf, 1, remaining);
                if (pr < 0 && errno == EINTR) continue;
                if (pr == 0) {
                    /* total timeout: grim never produced a complete frame (static screen) */
                    stale = 1;
                    fprintf(stderr, "[dhstream] grim timeout (no data)\\n");
                    kill(pid, SIGKILL);
                    break;
                }
                if (got + 65536 > cap) {
                    cap = cap ? cap * 2 : (size_t)1 << 20;
                    cur = realloc(cur, cap);
                    if (!cur) die("out of memory");
                }
                n = read(pfd[0], cur + got, cap - got);
                if (n <= 0) break;
                got += (size_t)n;
            }
            close(pfd[0]);
            waitpid(pid, &st, 0);
            if (stale) {
                if (++fails > MAX_FAIL) return 3;
                pace(&t0, fps);
                continue;
            }
        }
        if (!WIFEXITED(st) || WEXITSTATUS(st) != 0) {
            if (++fails > MAX_FAIL) return 3;
            pace(&t0, fps);
            continue;
        }

        /* P6 header: "P6" ws width ws height ws maxval ws pixels */
        if (got >= 20 && cur[0] == 'P' && cur[1] == '6') {
            long vw[3] = { 0, 0, 0 };
            unsigned char *x = cur + 2, *end = cur + got;
            int fi;
            for (fi = 0; fi < 3; fi++) {
                while (x < end && (*x == ' ' || *x == '\n' || *x == '\r' || *x == '\t')) x++;
                if (x >= end || *x < '0' || *x > '9') break;
                while (x < end && *x >= '0' && *x <= '9') {
                    vw[fi] = vw[fi] * 10 + (*x - '0');
                    x++;
                }
            }
            w = (int)vw[0]; h = (int)vw[1]; maxv = (int)vw[2];
            if (x < end) x++;               /* the one whitespace byte before the pixels */
            hdrlen = (size_t)(x - cur);
        }
        if (w <= 0 || h <= 0 || maxv != 255 || hdrlen + (size_t)w * h * 3 > got) {
            fprintf(stderr, "[dhstream] grim output not a ppm (%zu bytes)\n", got);
            if (++fails > MAX_FAIL) return 3;
            pace(&t0, fps);
            continue;
        }
        {
            long i;
            size_t len = (size_t)w * h * 3;
            unsigned char *px = cur + hdrlen;
            static unsigned char *prev = NULL, *xorbuf = NULL;
            static size_t prev_cap = 0, xor_cap = 0;
            static unsigned sent_count = 0;
            int use_delta = 0;
            for (i = 0; i < (long)len; i += 3) {  /* grim is RGB, the hub stores BGR like fb */
                unsigned char t = px[i];
                px[i] = px[i + 2];
                px[i + 2] = t;
            }
            /* change detection: skip identical frames entirely */
            if (prev && prev_cap == len && !memcmp(px, prev, len)) {
                pace(&t0, fps);
                continue;
            }
            /* delta encoding: send XOR with previous frame if it compresses much smaller */
            if (prev && prev_cap == len && sent_count % 8 != 0) {
                uLongf clen;
                size_t k;
                if (len > xor_cap) {
                    xorbuf = realloc(xorbuf, len);
                    xor_cap = len;
                }
                for (k = 0; k < len; k++) xorbuf[k] = (unsigned char)(px[k] ^ prev[k]);
                clen = compressBound((uLong)len);
                {
                    unsigned char *tmp = malloc(clen);
                    if (tmp && compress2(tmp, &clen, xorbuf, len, 1) == Z_OK && clen < len / 4) {
                        use_delta = 1;
                    }
                    free(tmp);
                }
            }
            maybe_config(w, h, 24);
            if (use_delta) send_frame(3, xorbuf, len);
            else send_frame(2, px, len);
            /* store previous */
            if (len > prev_cap) {
                prev = realloc(prev, len);
                prev_cap = len;
            }
            memcpy(prev, px, len);
            sent_count++;
        }
        fails = 0;
        fprintf(stderr, "[dhstream] captured %dx%d\n", w, h);
        pace(&t0, fps);
    }
}

/* ── wlr: wlr-screencopy captures the compositor's picture ─────────────────── */
/* Wayland client state (mirrors scpy.c) */
struct wlr_output { struct wl_output *output; int width, height, has_mode; struct wl_list link; };
struct wlr_client {
    struct wl_display *dpy;
    struct wl_registry *reg;
    struct zwlr_screencopy_manager_v1 *manager;
    struct wl_shm *shm;
    struct wl_list outputs;
    struct wlr_output *target;
    int buf_format, buf_width, buf_height, buf_stride;
    int has_buffer_info, frame_aborted, frame_ready;
};

static void wlr_out_geom(void *d, struct wl_output *o, int x, int y, int pw, int ph, int sp, const char *mk, const char *mdl, int tr) { (void)0; }
static void wlr_out_mode(void *d, struct wl_output *o, uint32_t flags, int w, int h, int r) {
    struct wlr_output *oo = d;
    if (flags & WL_OUTPUT_MODE_CURRENT) { oo->width = w; oo->height = h; oo->has_mode = 1; }
}
static void wlr_out_done(void *d, struct wl_output *o) { (void)0; }
static void wlr_out_scale(void *d, struct wl_output *o, int s) { (void)0; }
static const struct wl_output_listener wlr_out_listener = {
    .geometry = wlr_out_geom, .mode = wlr_out_mode, .done = wlr_out_done, .scale = wlr_out_scale,
};
static void wlr_reg_global(void *d, struct wl_registry *r, uint32_t name, const char *iface, uint32_t ver) {
    struct wlr_client *c = d;
    if (!strcmp(iface, "wl_output") && ver >= 2) {
        struct wlr_output *o = calloc(1, sizeof(*o));
        o->output = wl_registry_bind(r, name, &wl_output_interface, 2);
        wl_output_add_listener(o->output, &wlr_out_listener, o);
        wl_list_insert(&c->outputs, &o->link);
    } else if (!strcmp(iface, "zwlr_screencopy_manager_v1") && ver >= 1) {
        c->manager = wl_registry_bind(r, name, &zwlr_screencopy_manager_v1_interface, 3);
    } else if (!strcmp(iface, "wl_shm")) {
        c->shm = wl_registry_bind(r, name, &wl_shm_interface, 1);
    }
}
static void wlr_reg_remove(void *d, struct wl_registry *r, uint32_t n) { (void)0; }
static const struct wl_registry_listener wlr_reg_listener = { .global = wlr_reg_global, .global_remove = wlr_reg_remove };

static void wlr_frm_buffer(void *d, struct zwlr_screencopy_frame_v1 *f, uint32_t fmt, uint32_t w, uint32_t h, uint32_t stride) {
    struct wlr_client *c = d;
    c->buf_format = fmt; c->buf_width = w; c->buf_height = h; c->buf_stride = stride; c->has_buffer_info = 1;
}
static void wlr_frm_flags(void *d, struct zwlr_screencopy_frame_v1 *f, uint32_t fl) { (void)0; }
static void wlr_frm_ready(void *d, struct zwlr_screencopy_frame_v1 *f, uint32_t a, uint32_t b, uint32_t c2) {
    ((struct wlr_client *)d)->frame_ready = 1;
}
static void wlr_frm_failed(void *d, struct zwlr_screencopy_frame_v1 *f) { ((struct wlr_client *)d)->frame_aborted = 1; }
static void wlr_frm_damage(void *d, struct zwlr_screencopy_frame_v1 *f, uint32_t x, uint32_t y, uint32_t w, uint32_t h) { (void)0; }
static void wlr_frm_dmabuf(void *d, struct zwlr_screencopy_frame_v1 *f, uint32_t fmt, uint32_t w, uint32_t h) { (void)0; }
static void wlr_frm_done(void *d, struct zwlr_screencopy_frame_v1 *f) { (void)0; }
static const struct zwlr_screencopy_frame_v1_listener wlr_frm_listener = {
    .buffer = wlr_frm_buffer, .flags = wlr_frm_flags, .ready = wlr_frm_ready,
    .failed = wlr_frm_failed, .damage = wlr_frm_damage, .linux_dmabuf = wlr_frm_dmabuf,
    .buffer_done = wlr_frm_done,
};

static void send_compressed(int kind, const unsigned char *comp, size_t clen, size_t orig)
{
    static unsigned char hdr[17], out[CHUNK + 17];
    unsigned i, total, off, n;
    unsigned int be_id, be_orig;
    frame_id++;
    be_id = htonl(frame_id);
    be_orig = htonl((unsigned)orig);
    total = (unsigned)((clen + CHUNK - 1) / CHUNK);
    if (!total) total = 1;
    for (i = 0; i < total; i++) {
        unsigned int be_n;
        unsigned short be_i = htons(i), be_t = htons(total);
        off = i * CHUNK;
        n = (unsigned)(clen - off > CHUNK ? CHUNK : clen - off);
        be_n = htonl(n);
        hdr[0] = (unsigned char)kind;
        memcpy(hdr + 1, &be_id, 4);
        memcpy(hdr + 5, &be_i, 2);
        memcpy(hdr + 7, &be_t, 2);
        memcpy(hdr + 9, &be_n, 4);
        memcpy(hdr + 13, &be_orig, 4);
        memcpy(out, hdr, 17);
        memcpy(out + 17, comp + off, n);
        send_dgram(out, 17 + n);
    }
}

/* XRGB8888 (memory order R,G,B,X) -> RGB565 little-endian, 8 pixels at a time with NEON.
   The scalar version cost ~9 ms per 720x1280 frame on the X55. */
static void rgbx_to_565(const unsigned char *src, unsigned char *dst, int w, int h, int src_stride)
{
    for (int y = 0; y < h; y++) {
        const unsigned char *s = src + (size_t)y * src_stride;
        uint16_t *d = (uint16_t *)(dst + (size_t)y * w * 2);
        int x = 0;
#ifdef DH_NEON
        for (; x + 8 <= w; x += 8) {
            uint8x8x4_t p = vld4_u8(s + (size_t)x * 4);
            uint16x8_t r = vmovl_u8(p.val[0]);
            uint16x8_t g = vmovl_u8(p.val[1]);
            uint16x8_t b = vmovl_u8(p.val[2]);
            uint16x8_t v = vorrq_u16(
                vorrq_u16(vshlq_n_u16(vshrq_n_u16(r, 3), 11),
                          vshlq_n_u16(vshrq_n_u16(g, 2), 5)),
                vshrq_n_u16(b, 3));
            vst1q_u16(d + x, v);
        }
#endif
        for (; x < w; x++) {
            uint32_t px = ((const uint32_t *)s)[x];
            d[x] = (uint16_t)(((px & 0xf8) << 8) | ((px & 0xfc00) >> 5) | ((px & 0xf80000) >> 19));
        }
    }
}

/* XOR two buffers 8 bytes at a time; the compiler cannot always vectorise the byte loop. */
static void xor_buf(unsigned char *dst, const unsigned char *a, const unsigned char *b, size_t n)
{
    size_t i = 0;
    for (; i + 8 <= n; i += 8) {
        uint64_t x, y;
        memcpy(&x, a + i, 8);
        memcpy(&y, b + i, 8);
        x ^= y;
        memcpy(dst + i, &x, 8);
    }
    for (; i < n; i++) dst[i] = (unsigned char)(a[i] ^ b[i]);
}

/* ── capture buffers: two, so the compositor can fill one while we encode the other ── */
struct capbuf {
    void *data;
    size_t size;
    struct wl_buffer *wl;
    int w, h, stride, fmt;
};

static int capbuf_ensure(struct wlr_client *c, struct capbuf *b, int w, int h, int stride, int fmt)
{
    if (b->data && b->w == w && b->h == h && b->stride == stride && b->fmt == fmt) return 0;
    if (b->data) { munmap(b->data, b->size); b->data = NULL; }
    if (b->wl) { wl_buffer_destroy(b->wl); b->wl = NULL; }
    b->size = (size_t)stride * (size_t)h;
    int fd = memfd_create("dhstream-pool", 0);
    if (fd < 0) { perror("memfd_create"); return -1; }
    if (ftruncate(fd, (off_t)b->size) < 0) { perror("ftruncate"); close(fd); return -1; }
    b->data = mmap(NULL, b->size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (b->data == MAP_FAILED) { perror("mmap"); close(fd); b->data = NULL; return -1; }
    struct wl_shm_pool *pool = wl_shm_create_pool(c->shm, fd, (int)b->size);
    if (!pool) { close(fd); munmap(b->data, b->size); b->data = NULL; return -1; }
    b->wl = wl_shm_pool_create_buffer(pool, 0, w, h, stride, fmt);
    wl_shm_pool_destroy(pool);
    close(fd);
    if (!b->wl) { munmap(b->data, b->size); b->data = NULL; return -1; }
    b->w = w; b->h = h; b->stride = stride; b->fmt = fmt;
    return 0;
}

/* ── encode one captured frame: convert, change-detect, delta, compress, send ── */
static unsigned char *e_prev, *e_xor, *e_packed;
static size_t e_prev_cap, e_xor_cap, e_packed_cap;
static unsigned e_sent;
static int e_skip;
static time_t e_hb;   /* last packet of any kind, for the keepalive below */
#ifdef BENCH
static double b_conv, b_comp;
static int b_enc_n;
#endif

static void encode_capture(const unsigned char *src, int w, int h, int stride)
{
    size_t len = (size_t)w * 2 * h;
#ifdef BENCH
    struct timespec b0, b1, b2;
#endif
    if (len > e_packed_cap) {
        e_packed = realloc(e_packed, len);
        e_packed_cap = len;
        if (!e_packed) die("out of memory");
    }
#ifdef BENCH
    clock_gettime(CLOCK_MONOTONIC, &b0);
#endif
    rgbx_to_565(src, e_packed, w, h, stride);
#ifdef BENCH
    clock_gettime(CLOCK_MONOTONIC, &b1);
#endif

    /* change detection: skip identical frames entirely */
    if (e_prev && e_prev_cap == len && !memcmp(e_packed, e_prev, len)) {
        e_skip++;
        /* a static screen sends no frames, so tell the hub we are still alive now and then: otherwise
           it cannot tell "nothing changed" from "the streamer died" */
        time_t now = time(NULL);
        if (now - e_hb >= 2) { send_config_quiet(w, h, 16); e_hb = now; }
        return;
    }
    e_skip = 0;

    int use_delta = 0;
    size_t clen = 0;
    if (!compressor) compressor = libdeflate_alloc_compressor(1);
    compcap = libdeflate_zlib_compress_bound(compressor, len);
    if (compcap < len + 64) compcap = len + 64;
    comp = realloc(comp, compcap);
    if (!comp) die("out of memory");
    /* XOR with the previous frame and compress it. If that is much smaller than a full frame we
       send it as a delta; otherwise we compress the full frame. Only one compression per frame. */
    if (e_prev && e_prev_cap == len && e_sent % 8 != 0) {
        if (len > e_xor_cap) {
            e_xor = realloc(e_xor, len);
            e_xor_cap = len;
            if (!e_xor) die("out of memory");
        }
        xor_buf(e_xor, e_packed, e_prev, len);
        clen = libdeflate_zlib_compress(compressor, e_xor, len, comp, compcap);
        if (clen > 0 && clen < len / 4) use_delta = 1;
    }
    if (!use_delta) {
        clen = libdeflate_zlib_compress(compressor, e_packed, len, comp, compcap);
        if (clen == 0) return;
    }
#ifdef BENCH
    clock_gettime(CLOCK_MONOTONIC, &b2);
    b_conv += (b1.tv_sec - b0.tv_sec) * 1e3 + (b1.tv_nsec - b0.tv_nsec) / 1e6;
    b_comp += (b2.tv_sec - b1.tv_sec) * 1e3 + (b2.tv_nsec - b1.tv_nsec) / 1e6;
    b_enc_n++;
#endif

    maybe_config(w, h, 16);
    if (e_sent == 0) {
        for (int r = 0; r < 3; r++) send_config_quiet(w, h, 16);   /* survive UDP loss at start */
    } else if (e_sent % 30 == 0) {
        send_config_quiet(w, h, 16);
    }
    send_compressed(use_delta ? 3 : 2, comp, clen, len);
    e_hb = time(NULL);

    if (len > e_prev_cap) {
        e_prev = realloc(e_prev, len);
        e_prev_cap = len;
        if (!e_prev) die("out of memory");
    }
    memcpy(e_prev, e_packed, len);
    e_sent++;
}

static int wlr_loop(int fps, const char *dir, const char *sockn)
{
    struct wlr_client c;
    memset(&c, 0, sizeof(c));
    wl_list_init(&c.outputs);
    c.buf_format = WL_SHM_FORMAT_XRGB8888;

    setenv("XDG_RUNTIME_DIR", dir, 1);
    setenv("WAYLAND_DISPLAY", sockn, 1);
    fprintf(stderr, "[dhstream] wlr %s / %s\n", dir, sockn);
    fflush(stderr);

    c.dpy = wl_display_connect(NULL);
    if (!c.dpy) { fprintf(stderr, "[dhstream] wl_display_connect failed\n"); return 3; }
    c.reg = wl_display_get_registry(c.dpy);
    wl_registry_add_listener(c.reg, &wlr_reg_listener, &c);
    wl_display_roundtrip(c.dpy);
    wl_display_roundtrip(c.dpy);

    /* pick target output */
    struct wlr_output *o, *target = NULL;
    wl_list_for_each(o, &c.outputs, link) { if (o->has_mode) { target = o; break; } }
    if (!target && !wl_list_empty(&c.outputs))
        target = (struct wlr_output *)((char *)c.outputs.next - __builtin_offsetof(struct wlr_output, link));
    if (!target || !c.manager || !c.shm) { fprintf(stderr, "[dhstream] wlr: no output/manager/shm\n"); return 3; }
    c.target = target;
    int full_w = target->width > 0 ? target->width : 1280;
    int full_h = target->height > 0 ? target->height : 720;

    struct capbuf cb[2];
    memset(cb, 0, sizeof cb);
    int cur = 0, have_pending = 0;

    for (;;) {
        struct timespec t0;
        clock_gettime(CLOCK_MONOTONIC, &t0);

        /* 1. start capture into cb[cur]. The copy is asynchronous: the compositor fills the
              buffer while we encode the previous frame (step 2). */
        struct zwlr_screencopy_frame_v1 *frame = zwlr_screencopy_manager_v1_capture_output(c.manager, 0, c.target->output);
        zwlr_screencopy_frame_v1_add_listener(frame, &wlr_frm_listener, &c);
        c.frame_aborted = 0;
        c.frame_ready = 0;
        c.has_buffer_info = 0;
        c.buf_width = 0; c.buf_height = 0; c.buf_stride = 0; c.buf_format = 0;
        wl_display_roundtrip(c.dpy);

        int started = 0;
        if (!c.frame_aborted) {
            int aw, ah, as;
            if (c.has_buffer_info && c.buf_width > 0 && c.buf_height > 0) {
                aw = c.buf_width; ah = c.buf_height; as = c.buf_stride;
            } else {
                aw = full_w; ah = full_h; as = full_w * 4;
            }
            if (c.buf_format == 0) c.buf_format = WL_SHM_FORMAT_XRGB8888;
            if (capbuf_ensure(&c, &cb[cur], aw, ah, as, c.buf_format) == 0) {
                zwlr_screencopy_frame_v1_copy(frame, cb[cur].wl);
                /* flush now: otherwise the copy request sits in the client buffer while we encode,
                   and the compositor only starts the copy when we next dispatch — no overlap. */
                wl_display_flush(c.dpy);
                started = 1;
            } else {
                c.frame_aborted = 1;
            }
        }

        /* 2. encode the previous capture while this one is in flight */
        if (have_pending) {
            encode_capture(cb[1 - cur].data, cb[1 - cur].w, cb[1 - cur].h, cb[1 - cur].stride);
            have_pending = 0;
        }

        if (!started) { zwlr_screencopy_frame_v1_destroy(frame); goto pace; }

        /* 3. wait for the compositor to actually fill the buffer. Destroying the frame before
              `ready` cancels the copy and leaves the buffer stale (the old freeze bug). */
        while (!c.frame_ready && !c.frame_aborted) {
            if (wl_display_dispatch(c.dpy) < 0) { c.frame_aborted = 1; break; }
        }
        zwlr_screencopy_frame_v1_destroy(frame);
        if (c.frame_aborted || !c.frame_ready) goto pace;

        have_pending = 1;
        cur = 1 - cur;

pace:
        {
            struct timespec now;
            clock_gettime(CLOCK_MONOTONIC, &now);
            long elapsed = (now.tv_sec - t0.tv_sec) * 1000000000L + (now.tv_nsec - t0.tv_nsec);
            long period = (e_skip > 3) ? (1000000000L / 5) : (1000000000L / fps);
            if (period < 1000000000L / fps) period = 1000000000L / fps;
            if (elapsed < period) {
                usleep((useconds_t)((period - elapsed) / 1000));
            }
        }
#ifdef BENCH
        {
            static int iters;
            static struct timespec last;
            struct timespec bn;
            clock_gettime(CLOCK_MONOTONIC, &bn);
            iters++;
            if (!last.tv_sec) last = bn;
            if (bn.tv_sec - last.tv_sec >= 2) {
                fprintf(stderr, "[bench2] iters=%d sent=%u skip=%d\n", iters, e_sent, e_skip);
                fflush(stderr);
                iters = 0;
                last = bn;
            }
            if (b_enc_n >= 20) {
                fprintf(stderr, "[bench] conv=%.1f comp=%.1f ms (avg of %d)\n",
                        b_conv / b_enc_n, b_comp / b_enc_n, b_enc_n);
                fflush(stderr);
                b_conv = b_comp = 0;
                b_enc_n = 0;
            }
        }
#endif
    }

    return 0;
}

int main(int argc, char **argv)
{
    int fps, mode = 0;                     /* 0=way 1=drm 2=fb */
    int want_w = 0, want_h = 0;
    const char *dir = NULL, *sockn = NULL;

    if (argc < 5) {
        fprintf(stderr, "usage: %s <receiver_ip> <port> <fps> <way|drm|fb> [wayland_dir wayland_socket] [want_w want_h]\n", argv[0]);
        return 2;
    }
    fps = atoi(argv[3]);
    if (fps < MIN_FPS) fps = MIN_FPS;
    if (fps > MAX_FPS) fps = MAX_FPS;
    if (!strcmp(argv[4], "drm")) mode = 1;
    else if (!strcmp(argv[4], "fb")) mode = 2;
    else if (!strcmp(argv[4], "wlr")) mode = 3;
    if (argc > 6) { dir = argv[5]; sockn = argv[6]; }
    if (argc > 8) { want_w = atoi(argv[7]); want_h = atoi(argv[8]); }

    signal(SIGTERM, on_term);
    signal(SIGINT, on_term);

    memset(&dst, 0, sizeof dst);
    dst.sin_family = AF_INET;
    dst.sin_port = htons((unsigned short)atoi(argv[2]));
    if (inet_aton(argv[1], &dst.sin_addr) != 1) {
        fprintf(stderr, "[dhstream] bad ip: %s\n", argv[1]);
        return 2;
    }
    sockfd = socket(AF_INET, SOCK_DGRAM, 0);
    if (sockfd < 0) die("socket");
    {
        int b = 4 * 1024 * 1024;
        setsockopt(sockfd, SOL_SOCKET, SO_SNDBUF, &b, sizeof b);
    }
    fprintf(stderr, "[dhstream] streaming to %s:%s @ %d fps (%s)\n", argv[1], argv[2], fps, argv[4]);
    fflush(stderr);

    if (mode == 2) return fb_loop(fps);
    if (mode == 1) return drm_loop(fps, want_w, want_h);
    if (mode == 3) return wlr_loop(fps, dir, sockn);
    if (!dir || !sockn) {
        fprintf(stderr, "[dhstream] way mode needs the compositor's socket\n");
        return 2;
    }
    return way_loop(fps, dir, sockn);
}
