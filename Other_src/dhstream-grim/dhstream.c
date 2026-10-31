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
#include <zlib.h>

#define CHUNK    1300                 /* frame-chunk payload, under one MTU */
#define MAX_FPS  8
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

static void send_config(int w, int h, int bpp)
{
    unsigned char p[11];
    unsigned int be = htonl((unsigned)w), be2 = htonl((unsigned)h);
    unsigned short bp = htons((unsigned short)bpp);
    p[0] = 0x01;
    memcpy(p + 1, &be, 4);
    memcpy(p + 5, &be2, 4);
    memcpy(p + 9, &bp, 2);
    send_dgram(p, sizeof p);
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
    if (!dir || !sockn) {
        fprintf(stderr, "[dhstream] way mode needs the compositor's socket\n");
        return 2;
    }
    return way_loop(fps, dir, sockn);
}
