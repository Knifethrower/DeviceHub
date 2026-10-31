#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <errno.h>
#include <time.h>
#include <sys/mman.h>
#include <wayland-client.h>
#include "screencopy-unstable-v1.h"

struct output_info {
    struct wl_output *output;
    int width, height, has_mode;
    struct wl_list link;
};

struct client {
    struct wl_display *dpy;
    struct wl_registry *reg;
    struct zwlr_screencopy_manager_v1 *manager;
    struct wl_shm *shm;
    struct wl_list outputs;
    struct output_info *target;
    int fps;
    int buf_format, buf_width, buf_height, buf_stride_actual;
    int has_buffer_info;
    int buf_done;
    int frame_aborted;
};

static struct output_info *find_output(struct wl_list *list) {
    struct output_info *o;
    wl_list_for_each(o, list, link) {
        if (o->has_mode) return o;
    }
    return wl_list_empty(list) ? NULL : (struct output_info *)((char *)list->next - __builtin_offsetof(struct output_info, link));
}

static void output_handle_geometry(void *data, struct wl_output *wlo,
    int x, int y, int phys_w, int phys_h, int subpixel, const char *make,
    const char *model, int transform) { (void)0; }

static void output_handle_mode(void *data, struct wl_output *wlo,
    uint32_t flags, int w, int h, int refresh) {
    struct output_info *o = data;
    if (flags & WL_OUTPUT_MODE_CURRENT) {
        o->width = w; o->height = h; o->has_mode = 1;
    }
}

static void output_handle_done(void *data, struct wl_output *wlo) { (void)0; }
static void output_handle_scale(void *data, struct wl_output *wlo, int scale) { (void)0; }

static const struct wl_output_listener output_listener = {
    .geometry = output_handle_geometry,
    .mode = output_handle_mode,
    .done = output_handle_done,
    .scale = output_handle_scale,
};

static void registry_global(void *data, struct wl_registry *reg,
                            uint32_t name, const char *iface, uint32_t ver) {
    struct client *c = data;
    if (strcmp(iface, "wl_output") == 0) {
        struct output_info *o = calloc(1, sizeof(*o));
        o->output = wl_registry_bind(reg, name, &wl_output_interface, 2);
        wl_output_add_listener(o->output, &output_listener, o);
        wl_list_insert(&c->outputs, &o->link);
    } else if (strcmp(iface, "zwlr_screencopy_manager_v1") == 0 && ver >= 1) {
        c->manager = wl_registry_bind(reg, name, &zwlr_screencopy_manager_v1_interface, 3);
    } else if (strcmp(iface, "wl_shm") == 0) {
        c->shm = wl_registry_bind(reg, name, &wl_shm_interface, 1);
    }
}

static void registry_global_remove(void *data, struct wl_registry *r, uint32_t n) { (void)0; }

static const struct wl_registry_listener registry_listener = {
    .global = registry_global,
    .global_remove = registry_global_remove,
};

static void frame_handle_buffer(void *data, struct zwlr_screencopy_frame_v1 *f,
    uint32_t format, uint32_t width, uint32_t height, uint32_t stride) {
    struct client *c = data;
    c->buf_format = format;
    c->buf_width = width;
    c->buf_height = height;
    c->buf_stride_actual = stride;
    c->has_buffer_info = 1;
}

static void frame_handle_flags(void *data, struct zwlr_screencopy_frame_v1 *f, uint32_t flags) { (void)0; }

static void frame_handle_ready(void *data, struct zwlr_screencopy_frame_v1 *f,
    uint32_t tv_sec_hi, uint32_t tv_sec_lo, uint32_t tv_nsec) { (void)0; }

static void frame_handle_failed(void *data, struct zwlr_screencopy_frame_v1 *f) {
    struct client *c = data;
    c->frame_aborted = 1;
}

static void frame_handle_damage(void *data, struct zwlr_screencopy_frame_v1 *f,
    uint32_t x, uint32_t y, uint32_t w, uint32_t h) { (void)0; }

static void frame_handle_linux_dmabuf(void *data, struct zwlr_screencopy_frame_v1 *f,
    uint32_t format, uint32_t width, uint32_t height) { (void)0; }

static void frame_handle_buffer_done(void *data, struct zwlr_screencopy_frame_v1 *f) {
    struct client *c = data;
    c->buf_done = 1;
}

static const struct zwlr_screencopy_frame_v1_listener frame_listener = {
    .buffer = frame_handle_buffer,
    .flags = frame_handle_flags,
    .ready = frame_handle_ready,
    .failed = frame_handle_failed,
    .damage = frame_handle_damage,
    .linux_dmabuf = frame_handle_linux_dmabuf,
    .buffer_done = frame_handle_buffer_done,
};

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: scpy <scale> <fps> [output-name]\n");
        return 1;
    }
    int scale = 0; for (char *p = argv[1]; *p; p++) scale = scale*10 + (*p - '0'); if (scale < 1) scale = 1;
    int fps = 0; for (char *p = argv[2]; *p; p++) fps = fps*10 + (*p - '0'); if (fps < 1) fps = 20;
    const char *outname = argc > 3 ? argv[3] : NULL;

    struct client c;
    memset(&c, 0, sizeof(c));
    wl_list_init(&c.outputs);
    c.fps = fps;
    c.buf_format = WL_SHM_FORMAT_XRGB8888;

    c.dpy = wl_display_connect(NULL);
    if (!c.dpy) { perror("wl_display_connect"); return 1; }

    c.reg = wl_display_get_registry(c.dpy);
    wl_registry_add_listener(c.reg, &registry_listener, &c);
    wl_display_roundtrip(c.dpy);
    wl_display_roundtrip(c.dpy);

    /* find target output */
    struct output_info *o;
    c.target = NULL;
    wl_list_for_each(o, &c.outputs, link) {
        if (o->has_mode) { c.target = o; break; }
    }
    if (!c.target && !wl_list_empty(&c.outputs)) {
        c.target = (struct output_info *)((char *)c.outputs.next - __builtin_offsetof(struct output_info, link));
    }
    if (!c.target) {
        fprintf(stderr, "[scpy] no output found\n");
        return 1;
    }
    if (!c.manager || !c.shm) {
        fprintf(stderr, "[scpy] screencopy or shm not available\n");
        return 1;
    }

    int full_w = c.target->width;
    int full_h = c.target->height;
    if (full_w <= 0 || full_h <= 0) { full_w = 1280; full_h = 720; }

    int bw = full_w / scale;
    int bh = full_h / scale;

    void *buf_data = NULL;
    size_t buf_size = 0;
    struct wl_buffer *buf = NULL;
    int buf_w = 0, buf_h = 0, buf_stride = 0;

    /* one contiguous output buffer: header + RGB payload, written once/frame */
    size_t hdr_cap = 64;
    size_t frame_cap = (size_t)full_w * full_h * 3 + hdr_cap;
    unsigned char *out = malloc(frame_cap);
    if (!out) { perror("malloc"); return 1; }
    setvbuf(stdout, NULL, _IOFBF, 1 << 20);

    for (;;) {
        struct timespec t0;
        clock_gettime(CLOCK_MONOTONIC, &t0);
        /* create frame */
        struct zwlr_screencopy_frame_v1 *frame = zwlr_screencopy_manager_v1_capture_output(c.manager, 0, c.target->output);
        zwlr_screencopy_frame_v1_add_listener(frame, &frame_listener, &c);
        c.frame_aborted = 0;
        c.has_buffer_info = 0;
        c.buf_done = 0;
        c.buf_width = 0; c.buf_height = 0; c.buf_stride_actual = 0; c.buf_format = 0;
        wl_display_roundtrip(c.dpy);
        if (c.frame_aborted) { zwlr_screencopy_frame_v1_destroy(frame); continue; }

        int aw, ah, as;
        if (c.has_buffer_info && c.buf_width > 0 && c.buf_height > 0) {
            aw = c.buf_width; ah = c.buf_height; as = c.buf_stride_actual;
        } else {
            aw = bw; ah = bh; as = bw * 4;
        }
        if (aw <= 0 || ah <= 0) { aw = bw; ah = bh; as = bw * 4; }

        /* recreate buffer if dimensions changed */
        if (!buf_data || buf_w != aw || buf_h != ah) {
            if (buf_data) munmap(buf_data, buf_size);
            buf_size = (size_t)as * ah;
            int fd = memfd_create("scpy-pool", 0);
            if (fd < 0) { perror("memfd_create"); break; }
            if (ftruncate(fd, buf_size) < 0) { perror("ftruncate"); break; }
            buf_data = mmap(NULL, buf_size, PROT_READ|PROT_WRITE, MAP_SHARED, fd, 0);
            if (buf_data == MAP_FAILED) { perror("mmap"); break; }
            struct wl_shm_pool *pool = wl_shm_create_pool(c.shm, fd, buf_size);
            if (buf) wl_buffer_destroy(buf);
            buf = wl_shm_pool_create_buffer(pool, 0, aw, ah, as, c.buf_format ? c.buf_format : WL_SHM_FORMAT_XRGB8888);
            buf_w = aw; buf_h = ah; buf_stride = as;
        }

        /* copy into buffer */
        zwlr_screencopy_frame_v1_copy(frame, buf);
        wl_display_roundtrip(c.dpy);
        if (c.frame_aborted) { zwlr_screencopy_frame_v1_destroy(frame); continue; }

        zwlr_screencopy_frame_v1_destroy(frame);

        int needed = aw * ah * 3 + hdr_cap;
        if ((size_t)needed > frame_cap) {
            frame_cap = (size_t)needed;
            out = realloc(out, frame_cap);
            if (!out) { perror("realloc"); return 1; }
        }
        int hdr = snprintf((char *)out, hdr_cap, "P6\n%d %d\n255\n", aw, ah);
        unsigned char *dst = out + hdr;
        for (int y = 0; y < ah; y++) {
            uint32_t *row = (uint32_t *)((char*)buf_data + y * as);
            for (int x = 0; x < aw; x++) {
                uint32_t px = row[x];
                *dst++ = (px >> 16) & 0xff;
                *dst++ = (px >> 8) & 0xff;
                *dst++ = px & 0xff;
            }
        }
        fwrite(out, 1, (size_t)(dst - out), stdout);
        fflush(stdout);

/* pace to target fps (account for capture time) */
        struct timespec now;
        clock_gettime(CLOCK_MONOTONIC, &now);
        long elapsed = (now.tv_sec - t0.tv_sec) * 1000000000L + (now.tv_nsec - t0.tv_nsec);
        long period = 1000000000L / fps;
        if (elapsed < period) {
            struct timespec sl;
            sl.tv_sec = 0;
            sl.tv_nsec = period - elapsed;
            nanosleep(&sl, NULL);
        }
    }

    wl_display_disconnect(c.dpy);
    return 0;
}
