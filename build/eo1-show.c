/* eo1-show: minimal DRM image presenter with cross-fades for eo1-player.
 *
 * Owns the display (KMS) while it runs: finds the connected connector of the given card (imx-drm, LVDS), sets
 * its preferred mode on two 32-bit dumb buffers and page-flips between them. Commands on stdin, one per line:
 *   show <file.raw> <fade_ms>   raw frame, width*height*4 bytes (XRGB8888 = Pillow "BGRX"), cross-fade to it
 *   quit
 * Replies "ok <frames> <ms>" after each show (stdout, line buffered), "ready WxH" at start.
 * Blending is done straight into the write-combined back buffer (no copies, no pipes): ~25-30 fps at 1080p
 * on the i.MX6 Cortex-A9 where GStreamer's rawvideoparse+kmssink path managed ~6.
 *   eo1-show /dev/dri/by-path/platform-display-subsystem-card [initial.raw]
 * Build: gcc -O3 -mcpu=cortex-a9 -mfpu=neon -mfloat-abi=hard -o eo1-show eo1-show.c $(pkg-config --cflags --libs libdrm)
 */
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include <xf86drm.h>
#include <xf86drmMode.h>

struct buf {
	uint32_t handle, pitch, fb;
	uint64_t size;
	uint8_t *map;
};

static int fd;
static uint32_t crtc_id, conn_id;
static drmModeModeInfo mode;
static struct buf bufs[2];
static int front;
static uint8_t *cur;		/* the frame currently on screen (system memory copy, for the next fade) */
static size_t fsize;

static double now(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec + t.tv_nsec / 1e9;
}

static int make_buf(struct buf *b)
{
	struct drm_mode_create_dumb c = {.width = mode.hdisplay, .height = mode.vdisplay, .bpp = 32};
	if (drmIoctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &c) < 0) return -1;
	b->handle = c.handle;
	b->pitch = c.pitch;
	b->size = c.size;
	if (drmModeAddFB(fd, mode.hdisplay, mode.vdisplay, 24, 32, b->pitch, b->handle, &b->fb)) return -1;
	struct drm_mode_map_dumb m = {.handle = b->handle};
	if (drmIoctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &m) < 0) return -1;
	b->map = mmap(0, b->size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, m.offset);
	return b->map == MAP_FAILED ? -1 : 0;
}

static int setup(const char *card)
{
	fd = open(card, O_RDWR | O_CLOEXEC);
	if (fd < 0) { perror(card); return -1; }
	drmModeRes *res = drmModeGetResources(fd);
	if (!res) { fprintf(stderr, "no KMS resources\n"); return -1; }
	drmModeConnector *conn = NULL;
	for (int i = 0; i < res->count_connectors; i++) {
		drmModeConnector *c = drmModeGetConnector(fd, res->connectors[i]);
		if (c && c->connection == DRM_MODE_CONNECTED && c->count_modes > 0) { conn = c; break; }
		drmModeFreeConnector(c);
	}
	if (!conn) { fprintf(stderr, "no connected connector\n"); return -1; }
	conn_id = conn->connector_id;
	mode = conn->modes[0];
	for (int i = 0; i < conn->count_modes; i++)
		if (conn->modes[i].type & DRM_MODE_TYPE_PREFERRED) { mode = conn->modes[i]; break; }
	/* a CRTC this connector's encoder can drive */
	crtc_id = 0;
	for (int e = 0; e < conn->count_encoders && !crtc_id; e++) {
		drmModeEncoder *enc = drmModeGetEncoder(fd, conn->encoders[e]);
		if (!enc) continue;
		if (enc->crtc_id) crtc_id = enc->crtc_id;
		for (int c = 0; c < res->count_crtcs && !crtc_id; c++)
			if (enc->possible_crtcs & (1u << c)) crtc_id = res->crtcs[c];
		drmModeFreeEncoder(enc);
	}
	if (!crtc_id) { fprintf(stderr, "no CRTC\n"); return -1; }
	if (make_buf(&bufs[0]) || make_buf(&bufs[1])) { fprintf(stderr, "dumb buffers: %s\n", strerror(errno)); return -1; }
	fsize = (size_t)mode.hdisplay * mode.vdisplay * 4;
	cur = calloc(1, fsize);
	memset(bufs[0].map, 0, bufs[0].size);
	front = 0;
	/* no modeset here: the first present() does it with the first picture, so what is on the panel (the
	 * initramfs splash on fb0) stays up until then instead of flashing black */
	drmModeFreeConnector(conn);
	drmModeFreeResources(res);
	return 0;
}

static void flip_handler(int f, unsigned int seq, unsigned int s, unsigned int us, void *data)
{
	(void)f; (void)seq; (void)s; (void)us;
	*(int *)data = 0;
}

static int crtc_set;

/* present the back buffer, wait for the flip (vsync); the first call sets the mode */
static void present(void)
{
	int back = front ^ 1, pending = 1;
	if (!crtc_set) {
		if (drmModeSetCrtc(fd, crtc_id, bufs[back].fb, 0, 0, &conn_id, 1, &mode))
			fprintf(stderr, "SetCrtc: %s\n", strerror(errno));
		crtc_set = 1;
		front = back;
		return;
	}
	if (drmModePageFlip(fd, crtc_id, bufs[back].fb, DRM_MODE_PAGE_FLIP_EVENT, &pending) == 0) {
		drmEventContext ev = {.version = 2, .page_flip_handler = flip_handler};
		while (pending) {
			fd_set fds;
			FD_ZERO(&fds);
			FD_SET(fd, &fds);
			struct timeval tv = {1, 0};
			if (select(fd + 1, &fds, NULL, NULL, &tv) <= 0) break;
			drmHandleEvent(fd, &ev);
		}
	} else {
		drmModeSetCrtc(fd, crtc_id, bufs[back].fb, 0, 0, &conn_id, 1, &mode);
	}
	front = back;
}

/* write a frame (system memory, packed width*4) into the back buffer, honouring its pitch */
static void put_rows(const uint8_t *src)
{
	struct buf *b = &bufs[front ^ 1];
	size_t row = (size_t)mode.hdisplay * 4;
	for (int y = 0; y < mode.vdisplay; y++) memcpy(b->map + (size_t)y * b->pitch, src + y * row, row);
}

static void blend_rows(const uint8_t *a, const uint8_t *bsrc, uint32_t k)
{
	struct buf *b = &bufs[front ^ 1];
	size_t row = (size_t)mode.hdisplay * 4;
	uint32_t ik = 256 - k;
	static uint8_t *line;
	if (!line) line = malloc(row);
	for (int y = 0; y < mode.vdisplay; y++) {
		const uint8_t *pa = a + y * row, *pb = bsrc + y * row;
		for (size_t i = 0; i < row; i++) line[i] = (uint8_t)((pa[i] * ik + pb[i] * k) >> 8);
		memcpy(b->map + (size_t)y * b->pitch, line, row);	/* sequential writes into write-combined memory */
	}
}

static uint8_t *load(const char *path)
{
	FILE *f = fopen(path, "rb");
	if (!f) return NULL;
	uint8_t *p = malloc(fsize);
	size_t n = p ? fread(p, 1, fsize, f) : 0;
	fclose(f);
	if (n != fsize) { free(p); return NULL; }
	return p;
}

static void show(const char *path, int fade_ms)
{
	double t0 = now();
	uint8_t *img = load(path);
	if (!img) { printf("error cannot read %s (need %zu bytes)\n", path, fsize); return; }
	int frames = 0;
	if (fade_ms > 0) {
		int n = fade_ms * 25 / 1000;
		if (n < 2) n = 2;
		for (int f = 1; f < n; f++) {
			uint32_t k = (uint32_t)(256 * f / n);
			blend_rows(cur, img, k);
			present();
			frames++;
			double target = t0 + (double)f * fade_ms / 1000.0 / n, wait = target - now();
			if (wait > 0) usleep((useconds_t)(wait * 1e6));
		}
	}
	put_rows(img);
	present();
	frames++;
	memcpy(cur, img, fsize);
	free(img);
	printf("ok %d %d\n", frames, (int)((now() - t0) * 1000));
}

int main(int argc, char **argv)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	if (argc < 2) { fprintf(stderr, "usage: %s /dev/dri/cardN [initial.raw]\n", argv[0]); return 2; }
	if (setup(argv[1])) return 1;
	printf("ready %dx%d\n", mode.hdisplay, mode.vdisplay);
	if (argc > 2) show(argv[2], 0);
	char line[4096];
	while (fgets(line, sizeof line, stdin)) {
		char path[4000];
		int ms = 0;
		if (sscanf(line, "show %3999s %d", path, &ms) >= 1) show(path, ms);
		else if (!strncmp(line, "quit", 4)) break;
	}
	return 0;
}
