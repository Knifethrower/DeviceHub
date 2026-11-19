/* DeviceHub FPS counter: the per-process record libdhfps.so keeps and dhfps-mon reads.
 * One file per process in DHFPS_APPS, mapped shared. The layout is fixed (no padding surprises between arm64 and
 * armhf): 64-bit fields at 8-byte offsets. */
#ifndef DHFPS_H
#define DHFPS_H
#include <stdint.h>

#include <stdio.h>
#include <string.h>

/* The folder is where the files themselves are: /tmp/devhub-fps on handhelds, ~/.cache/devhub-fps on x86_64 PCs
 * (a flatpak such as RetroDECK has its own /tmp, but shares the home folder). dhfps_apps_dir() takes the path of
 * the monitor or library and gives <folder>/apps, falling back to the default when the path doesn't say. */
#define DHFPS_DIR   "/tmp/devhub-fps"
#define DHFPS_NAME  "/devhub-fps/"
#define DHFPS_PATH  512
#define DHFPS_MAGIC 0x53504644u     /* "DFPS" */
#define DHFPS_RING  256

struct dhfps_rec {
    uint32_t magic;                 /*  0 */
    uint32_t version;               /*  4: 1 */
    int32_t  pid;                   /*  8 */
    volatile uint32_t pos;          /* 12: frames written to ring (wraps) */
    volatile uint64_t frames;       /* 16: frames counted */
    volatile uint64_t last_ns;      /* 24: CLOCK_MONOTONIC of the last frame */
    char     comm[32];              /* 32: process name */
    uint32_t ring[DHFPS_RING];      /* 64: frame times in microseconds, ring[pos % DHFPS_RING] is the next slot */
};

static inline void dhfps_apps_dir(const char *self, char *out, size_t n)
{
    const char *p = self ? strstr(self, DHFPS_NAME) : 0;
    if (p && (size_t)(p - self) + sizeof DHFPS_NAME + 4 < n)
        snprintf(out, n, "%.*sapps", (int)(p - self + sizeof DHFPS_NAME - 1), self);
    else
        snprintf(out, n, "%s/apps", DHFPS_DIR);
}

#endif
