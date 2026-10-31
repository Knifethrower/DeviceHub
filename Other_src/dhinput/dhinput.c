/* DeviceHub input helper — runs on the handheld as root over a ControlMaster ssh.
   Listens on a UDP port, translates packets into uinput keyboard + gamepad events.
   Protocol (little-endian): "DHIN" magic, u32 secret, u16 nevents, then for each event:
     1 K/R key  : u8 type=1, u16 code, u8 value
     2 button   : u8 type=2, u16 code, u8 value
     3 abs axis : u8 type=3, u8 axis, i32 value
     4 rel-all  : u8 type=4
     5 ping     : u8 type=5, u32 seq  -> replies "DHPO" magic, u32 secret, u32 seq
     6 quit     : u8 type=6  (release all + clean exit)
   After each datagram that carries input, one EV_SYN SYN_REPORT is emitted.
   Exits after N seconds of inactivity (argv2, default 900). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <time.h>
#include <sys/time.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <linux/uinput.h>

static int kbfd = -1, gpfd = -1;
static int running = 1;
static time_t last = 0;
static int idle_max = 900;

/* keyboard key set */
static int kb_keys[] = {
    KEY_ESC, KEY_1, KEY_2, KEY_3, KEY_4, KEY_5, KEY_6, KEY_7, KEY_8, KEY_9, KEY_0,
    KEY_MINUS, KEY_EQUAL, KEY_BACKSPACE, KEY_TAB, KEY_Q, KEY_W, KEY_E, KEY_R, KEY_T, KEY_Y,
    KEY_U, KEY_I, KEY_O, KEY_P, KEY_LEFTBRACE, KEY_RIGHTBRACE, KEY_ENTER, KEY_LEFTCTRL,
    KEY_A, KEY_S, KEY_D, KEY_F, KEY_G, KEY_H, KEY_J, KEY_K, KEY_L, KEY_SEMICOLON,
    KEY_APOSTROPHE, KEY_GRAVE, KEY_LEFTSHIFT, KEY_BACKSLASH, KEY_Z, KEY_X, KEY_C, KEY_V,
    KEY_B, KEY_N, KEY_M, KEY_COMMA, KEY_DOT, KEY_SLASH, KEY_RIGHTSHIFT, KEY_LEFTALT,
    KEY_SPACE, KEY_CAPSLOCK, KEY_F1, KEY_F2, KEY_F3, KEY_F4, KEY_F5, KEY_F6, KEY_F7,
    KEY_F8, KEY_F9, KEY_F10, KEY_NUMLOCK, KEY_F11, KEY_F12, KEY_HOME, KEY_UP, KEY_PAGEUP,
    KEY_LEFT, KEY_RIGHT, KEY_END, KEY_DOWN, KEY_PAGEDOWN, KEY_INSERT, KEY_DELETE,
    KEY_RIGHTCTRL, KEY_RIGHTALT, KEY_LEFTMETA, 0,
};

/* gamepad buttons */
static int gp_buttons[] = {
    BTN_SOUTH, BTN_EAST, BTN_C, BTN_NORTH, BTN_WEST, BTN_Z, BTN_TL, BTN_TR, BTN_TL2,
    BTN_TR2, BTN_SELECT, BTN_START, BTN_MODE, BTN_THUMBL, BTN_THUMBR, 0,
};

static void emit(int fd, int type, int code, int val) {
    struct input_event ev;
    memset(&ev, 0, sizeof(ev));
    struct timeval tv; gettimeofday(&tv, NULL);
    ev.time = tv;
    ev.type = type;
    ev.code = code;
    ev.value = val;
    write(fd, &ev, sizeof(ev));
}

static void emit_syn(int fd) { emit(fd, EV_SYN, SYN_REPORT, 0); }

static void release_all(void) {
    /* keyboard: release all keys in set */
    for (int i = 0; kb_keys[i]; i++) emit(kbfd, EV_KEY, kb_keys[i], 0);
    /* gamepad: release all buttons */
    for (int i = 0; gp_buttons[i]; i++) emit(gpfd, EV_KEY, gp_buttons[i], 0);
    /* gamepad: center sticks + triggers, neutral hats */
    emit(gpfd, EV_ABS, ABS_X, 0);
    emit(gpfd, EV_ABS, ABS_Y, 0);
    emit(gpfd, EV_ABS, ABS_RX, 0);
    emit(gpfd, EV_ABS, ABS_RY, 0);
    emit(gpfd, EV_ABS, ABS_Z, 0);
    emit(gpfd, EV_ABS, ABS_RZ, 0);
    emit(gpfd, EV_ABS, ABS_HAT0X, 0);
    emit(gpfd, EV_ABS, ABS_HAT0Y, 0);
    emit_syn(kbfd);
    emit_syn(gpfd);
}

static void on_signal(int s) { running = 0; }

static int setup_uinput(void) {
    kbfd = open("/dev/uinput", O_WRONLY | O_NONBLOCK);
    if (kbfd < 0) { perror("open /dev/uinput"); return -1; }
    ioctl(kbfd, UI_SET_EVBIT, EV_KEY);
    ioctl(kbfd, UI_SET_EVBIT, EV_SYN);
    for (int i = 0; kb_keys[i]; i++) ioctl(kbfd, UI_SET_KEYBIT, kb_keys[i]);

    struct uinput_user_dev udev;
    memset(&udev, 0, sizeof(udev));
    snprintf(udev.name, UINPUT_MAX_NAME_SIZE, "DeviceHub Keyboard");
    udev.id.bustype = BUS_USB;
    udev.id.vendor = 0x1234;
    udev.id.product = 0x5678;
    udev.id.version = 1;
    write(kbfd, &udev, sizeof(udev));
    if (ioctl(kbfd, UI_DEV_CREATE) < 0) { perror("UI_DEV_CREATE kb"); close(kbfd); kbfd=-1; return -1; }

    gpfd = open("/dev/uinput", O_WRONLY | O_NONBLOCK);
    if (gpfd < 0) { perror("open /dev/uinput gp"); return -1; }
    ioctl(gpfd, UI_SET_EVBIT, EV_KEY);
    ioctl(gpfd, UI_SET_EVBIT, EV_ABS);
    ioctl(gpfd, UI_SET_EVBIT, EV_SYN);
    for (int i = 0; gp_buttons[i]; i++) ioctl(gpfd, UI_SET_KEYBIT, gp_buttons[i]);

    /* sticks + triggers + dpad hats */
    int axes[] = {ABS_X, ABS_Y, ABS_RX, ABS_RY, ABS_Z, ABS_RZ, ABS_HAT0X, ABS_HAT0Y};
    for (int i = 0; i < 8; i++) ioctl(gpfd, UI_SET_ABSBIT, axes[i]);

    memset(&udev, 0, sizeof(udev));
    snprintf(udev.name, UINPUT_MAX_NAME_SIZE, "DeviceHub Gamepad");
    udev.id.bustype = BUS_USB;
    udev.id.vendor = 0x1234;
    udev.id.product = 0x5679;
    udev.id.version = 1;
    /* sticks */
    udev.absmin[ABS_X] = -32768; udev.absmax[ABS_X] = 32767;
    udev.absmin[ABS_Y] = -32768; udev.absmax[ABS_Y] = 32767;
    udev.absmin[ABS_RX] = -32768; udev.absmax[ABS_RX] = 32767;
    udev.absmin[ABS_RY] = -32768; udev.absmax[ABS_RY] = 32767;
    udev.absmin[ABS_Z] = 0; udev.absmax[ABS_Z] = 1023;
    udev.absmin[ABS_RZ] = 0; udev.absmax[ABS_RZ] = 1023;
    udev.absmin[ABS_HAT0X] = -1; udev.absmax[ABS_HAT0X] = 1;
    udev.absmin[ABS_HAT0Y] = -1; udev.absmax[ABS_HAT0Y] = 1;
    write(gpfd, &udev, sizeof(udev));
    if (ioctl(gpfd, UI_DEV_CREATE) < 0) { perror("UI_DEV_CREATE gp"); close(gpfd); gpfd=-1; return -1; }
    return 0;
}

static void teardown_uinput(void) {
    if (kbfd >= 0) { release_all(); ioctl(kbfd, UI_DEV_DESTROY); close(kbfd); kbfd=-1; }
    if (gpfd >= 0) { ioctl(gpfd, UI_DEV_DESTROY); close(gpfd); gpfd=-1; }
}

int main(int argc, char **argv) {
    if (argc < 2) { fprintf(stderr, "usage: dhinput <port> [idle_seconds]\n"); return 2; }
    int port = atoi(argv[1]);
    if (argc > 2) idle_max = atoi(argv[2]);
    if (idle_max <= 0) idle_max = 900;

    /* secret from /dev/urandom */
    unsigned int secret = 0;
    int ur = open("/dev/urandom", O_RDONLY);
    if (ur >= 0) { read(ur, &secret, sizeof(secret)); close(ur); }
    if (!secret) secret = (unsigned int)time(NULL) ^ 0xdeadbeef;

    signal(SIGTERM, on_signal);
    signal(SIGINT, on_signal);
    signal(SIGHUP, on_signal);

    if (setup_uinput() < 0) { fprintf(stderr, "ERROR uinput\n"); return 1; }

    int s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0) { perror("socket"); teardown_uinput(); return 1; }
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_ANY);
    addr.sin_port = htons(port);
    if (bind(s, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        fprintf(stderr, "ERROR bind %d: %s\n", port, strerror(errno));
        close(s); teardown_uinput(); return 1;
    }

    /* tell parent we're ready */
    printf("READY %u %d\n", secret, port);
    fflush(stdout);

    unsigned char buf[4096];
    last = time(NULL);
    while (running && (time(NULL) - last) < idle_max) {
        struct timeval tv;
        tv.tv_sec = 1; tv.tv_usec = 0;
        fd_set rfds; FD_ZERO(&rfds); FD_SET(s, &rfds);
        int r = select(s+1, &rfds, NULL, NULL, &tv);
        if (r < 0) { if (errno == EINTR) continue; break; }
        if (r == 0) continue;
        struct sockaddr_in peer;
        socklen_t plen = sizeof(peer);
        int n = recvfrom(s, buf, sizeof(buf), 0, (struct sockaddr*)&peer, &plen);
        if (n < 8) continue;
        if (memcmp(buf, "DHIN", 4) != 0) continue;
        unsigned int ssec = buf[4] | (buf[5]<<8) | (buf[6]<<16) | ((unsigned int)buf[7]<<24);
        if (ssec != secret) continue;
        last = time(NULL);
        int i = 8;
        int count = buf[i] | (buf[i+1]<<8); i += 2;
        int had_input = 0;
        for (int e = 0; e < count && i < n; e++) {
            unsigned char t = buf[i++];
            if (t == 1 && i + 3 <= n) {          /* key */
                int code = buf[i] | (buf[i+1]<<8); i += 2;
                int val = buf[i++];
                emit(kbfd, EV_KEY, code, val);
                had_input = 1;
            } else if (t == 2 && i + 3 <= n) {   /* button */
                int code = buf[i] | (buf[i+1]<<8); i += 2;
                int val = buf[i++];
                emit(gpfd, EV_KEY, code, val);
                had_input = 1;
            } else if (t == 3 && i + 5 <= n) {   /* abs */
                int axis = buf[i++];
                int val = buf[i] | (buf[i+1]<<8) | (buf[i+2]<<16) | (buf[i+3]<<24); i += 4;
                emit(gpfd, EV_ABS, axis, val);
                had_input = 1;
            } else if (t == 4) {                  /* release all */
                release_all();
            } else if (t == 5 && i + 4 <= n) {   /* ping */
                unsigned int seq = buf[i] | (buf[i+1]<<8) | (buf[i+2]<<16) | ((unsigned int)buf[i+3]<<24); i += 4;
                unsigned char pong[12];
                memcpy(pong, "DHPO", 4);
                pong[4]=buf[4]; pong[5]=buf[5]; pong[6]=buf[6]; pong[7]=buf[7]; /* secret */
                pong[8]=seq & 0xff; pong[9]=(seq>>8)&0xff; pong[10]=(seq>>16)&0xff; pong[11]=(seq>>24)&0xff;
                sendto(s, pong, 12, 0, (struct sockaddr*)&peer, plen);
            } else if (t == 6) {                  /* quit */
                running = 0;
            } else {
                /* unknown: stop parsing this datagram */
                break;
            }
        }
        if (had_input) { emit_syn(kbfd); emit_syn(gpfd); }
    }

    /* cleanup */
    release_all();
    teardown_uinput();
    close(s);
    return 0;
}
