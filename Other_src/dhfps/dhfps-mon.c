/* dhfps-mon: DeviceHub's FPS monitor, run on the device over the shared SSH connection.
 *
 * Two counts, one JSON line per second on stdout:
 *   "flip": frames that reached the screen, for any program and with no setup. Polls every few ms which buffer each
 *           DRM plane scans out (/dev/dri/card*) and the pan offset of fbdev screens (/dev/fb*; Allwinner BSP
 *           kernels such as the RG Cube XX's flip by panning a double-height framebuffer). Each change is a flip.
 *           Under a compositor (sway on ROCKNIX) this is the compositor's output rate.
 *   "apps": frames each program presented, from the records libdhfps.so keeps in /tmp/devhub-fps/apps.
 * "worst" is the longest frame in that second, in ms.
 *
 *   dhfps-mon [poll_ms=3] [max_seconds=21600] [watch_stdin=0]
 * Stops when stdout goes away, after max_seconds, or (watch_stdin=1) when stdin reaches end of file: DeviceHub keeps
 * it open, and an SSH channel whose client died stays open on the device, writes and all. Built static (no libc
 * version to match on the device). */
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include <drm/drm.h>
#include <drm/drm_mode.h>
#include <linux/fb.h>
#include "dhfps.h"

#define MAXSRC 32
#define MAXAPP 32

struct src {                    /* one flip source: a DRM plane or a framebuffer */
    char name[40];
    int fd;                     /* card or fb device */
    uint32_t plane;             /* DRM plane id (0: fbdev) */
    uint64_t last;              /* last fb id / pan offset seen */
    int have;
    uint64_t last_t;            /* time of the last flip */
    int frames;                 /* this second */
    double worst;               /* ms, this second */
    int idle;                   /* seconds without a flip */
};

struct app {
    int pid;
    struct dhfps_rec *rec;
    uint64_t frames;
    uint32_t pos;
};

static struct src srcs[MAXSRC];
static int nsrc;
static struct app apps[MAXAPP];
static int napp;

static uint64_t mono_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000u + ts.tv_nsec;
}

static int find_src(int fd, uint32_t plane)
{
    for (int i = 0; i < nsrc; i++)
        if (srcs[i].fd == fd && srcs[i].plane == plane)
            return i;
    return -1;
}

static void add_src(int fd, uint32_t plane, const char *name)
{
    if (nsrc >= MAXSRC || find_src(fd, plane) >= 0)
        return;
    struct src *s = &srcs[nsrc++];
    memset(s, 0, sizeof *s);
    s->fd = fd;
    s->plane = plane;
    snprintf(s->name, sizeof s->name, "%s", name);
}

static int cards[8], ncard;

static void scan_planes(void)
{
    for (int c = 0; c < ncard; c++) {
        uint32_t ids[64];
        struct drm_mode_get_plane_res res = { 0 };
        res.plane_id_ptr = (uintptr_t)ids;
        res.count_planes = 64;
        if (ioctl(cards[c], DRM_IOCTL_MODE_GETPLANERESOURCES, &res) != 0)
            continue;
        for (uint32_t i = 0; i < res.count_planes && i < 64; i++) {
            char name[40];
            snprintf(name, sizeof name, "card%d plane %u", c, ids[i]);
            add_src(cards[c], ids[i], name);
        }
    }
}

static void open_devices(void)
{
    char path[32];
    for (int i = 0; i < 8; i++) {
        snprintf(path, sizeof path, "/dev/dri/card%d", i);
        int fd = open(path, O_RDWR | O_CLOEXEC);
        if (fd < 0)
            fd = open(path, O_RDONLY | O_CLOEXEC);
        if (fd < 0)
            continue;
        struct drm_set_client_cap cap = { DRM_CLIENT_CAP_UNIVERSAL_PLANES, 1 };
        ioctl(fd, DRM_IOCTL_SET_CLIENT_CAP, &cap);
        cards[ncard++] = fd;
    }
    scan_planes();
    for (int i = 0; i < 4; i++) {
        snprintf(path, sizeof path, "/dev/fb%d", i);
        int fd = open(path, O_RDONLY | O_CLOEXEC);
        if (fd < 0)
            continue;
        struct fb_var_screeninfo v;
        if (ioctl(fd, FBIOGET_VSCREENINFO, &v) != 0) {
            close(fd);
            continue;
        }
        snprintf(path, sizeof path, "fb%d", i);
        add_src(fd, 0, path);
    }
}

static void poll_srcs(uint64_t t)
{
    for (int i = 0; i < nsrc; i++) {
        struct src *s = &srcs[i];
        uint64_t cur;
        if (s->plane) {
            struct drm_mode_get_plane p = { 0 };
            p.plane_id = s->plane;
            if (ioctl(s->fd, DRM_IOCTL_MODE_GETPLANE, &p) != 0)
                continue;
            if (!p.fb_id || !p.crtc_id) {       /* plane off */
                s->have = 0;
                continue;
            }
            cur = p.fb_id;
        } else {
            struct fb_var_screeninfo v;
            if (ioctl(s->fd, FBIOGET_VSCREENINFO, &v) != 0)
                continue;
            cur = ((uint64_t)v.xoffset << 32) | v.yoffset;
        }
        if (s->have && cur != s->last) {
            s->frames++;
            if (s->last_t && t - s->last_t < 2000000000u) {
                double ms = (t - s->last_t) / 1e6;
                if (ms > s->worst)
                    s->worst = ms;
            }
            s->last_t = t;
        }
        s->last = cur;
        s->have = 1;
    }
}

static int alive(int pid)
{
    return kill(pid, 0) == 0 || errno == EPERM;
}

static void scan_apps(void)
{
    for (int i = 0; i < napp; i++)        /* drop the records of programs that ended */
        if (!alive(apps[i].pid)) {
            char path[64];
            munmap(apps[i].rec, sizeof(struct dhfps_rec));
            snprintf(path, sizeof path, DHFPS_APPS "/%d", apps[i].pid);
            unlink(path);
            apps[i--] = apps[--napp];
        }
    DIR *d = opendir(DHFPS_APPS);
    if (!d)
        return;
    struct dirent *e;
    while ((e = readdir(d)) && napp < MAXAPP) {
        int pid = atoi(e->d_name);
        if (pid <= 0)
            continue;
        int known = 0;
        for (int i = 0; i < napp; i++)
            known |= apps[i].pid == pid;
        if (known)
            continue;
        char path[64];
        snprintf(path, sizeof path, DHFPS_APPS "/%d", pid);
        if (!alive(pid)) {
            unlink(path);
            continue;
        }
        int fd = open(path, O_RDONLY | O_CLOEXEC);
        if (fd < 0)
            continue;
        void *p = mmap(0, sizeof(struct dhfps_rec), PROT_READ, MAP_SHARED, fd, 0);
        close(fd);
        if (p == MAP_FAILED)
            continue;
        struct dhfps_rec *r = p;
        if (__atomic_load_n(&r->magic, __ATOMIC_ACQUIRE) != DHFPS_MAGIC) {
            munmap(p, sizeof *r);
            continue;
        }
        apps[napp].pid = pid;
        apps[napp].rec = r;
        apps[napp].frames = __atomic_load_n(&r->frames, __ATOMIC_ACQUIRE);
        apps[napp].pos = __atomic_load_n(&r->pos, __ATOMIC_ACQUIRE);
        napp++;
    }
    closedir(d);
}

static void json_str(const char *s)
{
    putchar('"');
    for (; *s; s++)
        if (*s == '"' || *s == '\\') printf("\\%c", *s);
        else if ((unsigned char)*s >= 32) putchar(*s);
    putchar('"');
}

static void report(double secs)
{
    struct timespec rt;
    clock_gettime(CLOCK_REALTIME, &rt);
    printf("{\"t\":%llu,\"flip\":[", (unsigned long long)rt.tv_sec * 1000 + rt.tv_nsec / 1000000);
    int first = 1;
    for (int i = 0; i < nsrc; i++) {
        struct src *s = &srcs[i];
        s->idle = s->frames ? 0 : s->idle + 1;
        if (s->frames || s->idle < 3) {         /* sources that flipped lately; an idle screen shows none */
            if (s->last_t || s->frames) {
                printf("%s{\"src\":", first ? "" : ",");
                json_str(s->name);
                printf(",\"fps\":%.1f,\"worst\":%.1f}", s->frames / secs, s->worst);
                first = 0;
            }
        }
        s->frames = 0;
        s->worst = 0;
    }
    printf("],\"apps\":[");
    first = 1;
    for (int i = 0; i < napp; i++) {
        struct app *a = &apps[i];
        uint64_t f = __atomic_load_n(&a->rec->frames, __ATOMIC_ACQUIRE);
        uint32_t pos = __atomic_load_n(&a->rec->pos, __ATOMIC_ACQUIRE);
        uint32_t n = pos - a->pos;
        if (n > DHFPS_RING)
            n = DHFPS_RING;
        uint32_t worst = 0;
        for (uint32_t k = 0; k < n; k++) {
            uint32_t us = a->rec->ring[(pos - 1 - k) % DHFPS_RING];
            if (us > worst && us < 10000000u)
                worst = us;
        }
        printf("%s{\"pid\":%d,\"comm\":", first ? "" : ",", a->pid);
        char comm[33];
        memcpy(comm, a->rec->comm, 32);
        comm[32] = 0;
        json_str(comm);
        printf(",\"fps\":%.1f,\"worst\":%.1f}", (f - a->frames) / secs, worst / 1000.0);
        a->frames = f;
        a->pos = pos;
        first = 0;
    }
    printf("]}\n");
    if (fflush(stdout) != 0 || ferror(stdout))
        exit(0);
}

int main(int argc, char **argv)
{
    int poll_ms = argc > 1 ? atoi(argv[1]) : 3;
    long max_s = argc > 2 ? atol(argv[2]) : 21600;
    int watch_stdin = argc > 3 && atoi(argv[3]);
    if (poll_ms < 1) poll_ms = 1;
    signal(SIGPIPE, SIG_DFL);
    open_devices();
    scan_apps();
    printf("{\"start\":1,\"sources\":%d,\"cards\":%d}\n", nsrc, ncard);
    fflush(stdout);
    uint64_t t0 = mono_ns(), tick = t0, start = t0;
    int ticks = 0;
    struct timespec nap = { 0, poll_ms * 1000000L };
    struct pollfd in = { 0, POLLIN, 0 };
    for (;;) {
        if (watch_stdin) {      /* DeviceHub holds stdin open and never writes: end of file = it went away */
            if (poll(&in, 1, poll_ms) > 0) {
                char b[64];
                if (read(0, b, sizeof b) <= 0)
                    return 0;
            }
        } else
            nanosleep(&nap, 0);
        uint64_t t = mono_ns();
        poll_srcs(t);
        if (t - tick >= 1000000000u) {
            report((t - tick) / 1e9);
            tick = t;
            if (++ticks % 2 == 0) {
                scan_apps();
                if (ncard)
                    scan_planes();
            }
            if ((long)((t - start) / 1000000000u) >= max_s)
                return 0;
        }
    }
}
