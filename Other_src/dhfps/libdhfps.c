/* libdhfps.so: counts the frames a program presents (eglSwapBuffers), for DeviceHub's FPS view.
 *
 * Loaded with LD_PRELOAD. Interposes eglSwapBuffers / eglSwapBuffersWithDamage{KHR,EXT} for programs linked to
 * libEGL, and dlsym / eglGetProcAddress for those that look the function up at run time (SDL2, box64, gl4es).
 * Each counting process gets a record in /tmp/devhub-fps/apps/<pid> (see dhfps.h); nothing happens unless that
 * folder exists, so the library is inert when DeviceHub isn't watching.
 *
 * Built against an old glibc (Debian bullseye arm64 / buster armhf) so it loads on every CFW. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <fcntl.h>
#include <link.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include "dhfps.h"

typedef unsigned int (*swap_fn)(void *dpy, void *surf);
typedef unsigned int (*swapd_fn)(void *dpy, void *surf, const int *rects, int n);
typedef void *(*proc_fn)(const char *name);
typedef void *(*dlsym_fn)(void *handle, const char *name);

static swap_fn  real_swap;
static swapd_fn real_swapd_khr, real_swapd_ext;
static proc_fn  real_getproc;
static dlsym_fn real_dlsym;

static struct dhfps_rec *rec;
static int rec_pid;
static uint64_t prev_ns;

unsigned int eglSwapBuffers(void *dpy, void *surf);
unsigned int eglSwapBuffersWithDamageKHR(void *dpy, void *surf, const int *rects, int n);
unsigned int eglSwapBuffersWithDamageEXT(void *dpy, void *surf, const int *rects, int n);
void *eglGetProcAddress(const char *name);

static dlsym_fn get_real_dlsym(void)
{
    if (!real_dlsym) {
        /* glibc 2.34 moved dlsym into libc with a new version; older ones only have the base version */
        static const char *vers[] = { "GLIBC_2.34", "GLIBC_2.17", "GLIBC_2.4", 0 };
        for (int i = 0; vers[i] && !real_dlsym; i++)
            real_dlsym = (dlsym_fn)dlvsym(RTLD_NEXT, "dlsym", vers[i]);
    }
    return real_dlsym;
}

static uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000u + ts.tv_nsec;
}

static void open_rec(void)
{
    char path[96];
    int pid = getpid();
    rec = 0;
    rec_pid = pid;
    prev_ns = 0;
    if (access(DHFPS_APPS, W_OK) != 0)
        return;
    snprintf(path, sizeof path, DHFPS_APPS "/%d", pid);
    int fd = open(path, O_RDWR | O_CREAT | O_TRUNC | O_CLOEXEC, 0666);
    if (fd < 0)
        return;
    fchmod(fd, 0666);
    if (ftruncate(fd, sizeof(struct dhfps_rec)) == 0) {
        void *p = mmap(0, sizeof(struct dhfps_rec), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        if (p != MAP_FAILED) {
            rec = p;
            rec->pid = pid;
            rec->version = 1;
            int c = open("/proc/self/comm", O_RDONLY | O_CLOEXEC);
            if (c >= 0) {
                ssize_t n = read(c, rec->comm, sizeof rec->comm - 1);
                close(c);
                while (n > 0 && (rec->comm[n - 1] == '\n' || rec->comm[n - 1] == 0))
                    rec->comm[--n] = 0;
            }
            __atomic_store_n(&rec->magic, DHFPS_MAGIC, __ATOMIC_RELEASE);
        }
    }
    close(fd);
}

static void count_frame(void)
{
    if (rec_pid != getpid())            /* first frame, or a forked child */
        open_rec();
    if (!rec)
        return;
    uint64_t t = now_ns();
    if (prev_ns) {
        uint64_t us = (t - prev_ns) / 1000;
        uint32_t pos = rec->pos;
        rec->ring[pos % DHFPS_RING] = us > 0xffffffffu ? 0xffffffffu : (uint32_t)us;
        __atomic_store_n(&rec->pos, pos + 1, __ATOMIC_RELEASE);
    }
    prev_ns = t;
    rec->last_ns = t;
    __atomic_store_n(&rec->frames, rec->frames + 1, __ATOMIC_RELEASE);
}

/* the real function behind a name, skipping this library's own definition */
static void *next(const char *name)
{
    dlsym_fn ds = get_real_dlsym();
    return ds ? ds(RTLD_NEXT, name) : 0;
}

unsigned int eglSwapBuffers(void *dpy, void *surf)
{
    if (!real_swap)
        real_swap = (swap_fn)next("eglSwapBuffers");
    count_frame();
    return real_swap ? real_swap(dpy, surf) : 0;
}

unsigned int eglSwapBuffersWithDamageKHR(void *dpy, void *surf, const int *rects, int n)
{
    if (!real_swapd_khr)
        real_swapd_khr = (swapd_fn)next("eglSwapBuffersWithDamageKHR");
    count_frame();
    return real_swapd_khr ? real_swapd_khr(dpy, surf, rects, n) : 0;
}

unsigned int eglSwapBuffersWithDamageEXT(void *dpy, void *surf, const int *rects, int n)
{
    if (!real_swapd_ext)
        real_swapd_ext = (swapd_fn)next("eglSwapBuffersWithDamageEXT");
    count_frame();
    return real_swapd_ext ? real_swapd_ext(dpy, surf, rects, n) : 0;
}

/* a function pointer handed out by the driver: keep it, give back this library's counting wrapper */
static void *wrap(const char *name, void *p)
{
    if (!p || !name)
        return p;
    if (!strcmp(name, "eglSwapBuffers")) {
        if (p != (void *)eglSwapBuffers) real_swap = (swap_fn)p;
        return (void *)eglSwapBuffers;
    }
    if (!strcmp(name, "eglSwapBuffersWithDamageKHR")) {
        if (p != (void *)eglSwapBuffersWithDamageKHR) real_swapd_khr = (swapd_fn)p;
        return (void *)eglSwapBuffersWithDamageKHR;
    }
    if (!strcmp(name, "eglSwapBuffersWithDamageEXT")) {
        if (p != (void *)eglSwapBuffersWithDamageEXT) real_swapd_ext = (swapd_fn)p;
        return (void *)eglSwapBuffersWithDamageEXT;
    }
    if (!strcmp(name, "eglGetProcAddress")) {
        if (p != (void *)eglGetProcAddress) real_getproc = (proc_fn)p;
        return (void *)eglGetProcAddress;
    }
    return p;
}

void *eglGetProcAddress(const char *name)
{
    if (!real_getproc)
        real_getproc = (proc_fn)next("eglGetProcAddress");
    return wrap(name, real_getproc ? real_getproc(name) : 0);
}

/* dlsym(RTLD_NEXT) means "after the caller", which a forwarded call would turn into "after this library": walk the
 * objects loaded after the caller instead */
static void *next_after(void *caller, const char *name, dlsym_fn ds)
{
    Dl_info info;
    struct link_map *m = 0;
    if (!dladdr1(caller, &info, (void **)&m, RTLD_DL_LINKMAP) || !m)
        return ds(RTLD_NEXT, name);
    for (m = m->l_next; m; m = m->l_next) {
        if (!m->l_name || !m->l_name[0])
            continue;
        void *h = dlopen(m->l_name, RTLD_LAZY | RTLD_NOLOAD);
        if (!h)
            continue;
        void *p = ds(h, name);
        dlclose(h);
        if (p)
            return p;
    }
    return 0;
}

void *dlsym(void *handle, const char *name)
{
    dlsym_fn ds = get_real_dlsym();
    if (!ds)
        return 0;
    void *p = handle == RTLD_NEXT ? next_after(__builtin_return_address(0), name, ds) : ds(handle, name);
    if (name[0] == 'e' && name[1] == 'g' && name[2] == 'l')
        return wrap(name, p);
    return p;
}
