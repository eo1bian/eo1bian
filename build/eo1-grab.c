/* eo1-grab: real screenshots of the EO1 panel, read back from the display controller (what is scanned out,
 * not what a program thinks it shows), including video frames from the VPU.
 *
 *   eo1-grab <out-prefix> [count] [interval_ms] [scale]
 *
 * Finds the planes that are on the active CRTC (overlay above primary), takes the topmost one with a
 * framebuffer, maps it (DRM_IOCTL_MODE_GETFB2 + MAP_DUMB: imx-drm's buffers are all DMA GEM objects) and
 * writes <out-prefix>-NNN.grab: one text header line
 *   EO1GRAB <fourcc> <width> <height> <pitch0> <offset0> <pitch1> <offset1> <ms-since-first>
 * followed by the bytes from the start of the buffer to the end of the last plane. With scale > 1 it writes a
 * small RGB copy instead (header "EO1GRAB RGB3 w h pitch 0 0 0 ms <source fourcc>"), ~390 KB at scale 4. debian/build/framecheck.py
 * turns them into PNGs. A burst (count > 1) catches transitions frame by frame. Needs root (GETFB2 handles).
 * Build: gcc -O2 -o eo1-grab eo1-grab.c $(pkg-config --cflags --libs libdrm)
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
#include <drm_fourcc.h>

static double now_ms(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec * 1000.0 + t.tv_nsec / 1e6;
}

/* the framebuffer the panel shows right now: the topmost plane on an active CRTC */
static uint32_t scanout_fb(int fd)
{
	drmSetClientCap(fd, DRM_CLIENT_CAP_UNIVERSAL_PLANES, 1);
	drmModeRes *res = drmModeGetResources(fd);
	if (!res)
		return 0;
	uint32_t crtc_id = 0;
	for (int i = 0; i < res->count_crtcs && !crtc_id; i++) {
		drmModeCrtc *c = drmModeGetCrtc(fd, res->crtcs[i]);
		if (c && c->mode_valid && c->buffer_id)
			crtc_id = c->crtc_id;
		drmModeFreeCrtc(c);
	}
	uint32_t fb = 0;
	int best = -1;
	drmModePlaneRes *pr = drmModeGetPlaneResources(fd);
	for (uint32_t i = 0; pr && i < pr->count_planes; i++) {
		drmModePlane *p = drmModeGetPlane(fd, pr->planes[i]);
		if (!p)
			continue;
		if (p->crtc_id == crtc_id && p->fb_id) {
			/* plane type: primary 1, overlay 0, cursor 2 - an overlay sits above the primary */
			int rank = 1;
			uint64_t alpha = 0xffff;
			drmModeObjectProperties *props = drmModeObjectGetProperties(fd, p->plane_id, DRM_MODE_OBJECT_PLANE);
			for (uint32_t k = 0; props && k < props->count_props; k++) {
				drmModePropertyRes *pp = drmModeGetProperty(fd, props->props[k]);
				if (pp && !strcmp(pp->name, "type"))
					rank = props->prop_values[k] == DRM_PLANE_TYPE_OVERLAY ? 2 : 1;
				if (pp && !strcmp(pp->name, "alpha"))
					alpha = props->prop_values[k];
				drmModeFreeProperty(pp);
			}
			/* eo1-layers keeps the overlay on all the time: mostly hidden (alpha < half) = the primary shows */
			if (rank == 2 && alpha < 0x8000)
				rank = 0;
			drmModeFreeObjectProperties(props);
			if (rank > best) {
				best = rank;
				fb = p->fb_id;
			}
		}
		drmModeFreePlane(p);
	}
	drmModeFreePlaneResources(pr);
	drmModeFreeResources(res);
	return fb;
}

static int scale = 0;   /* 0: raw buffer; 1..8: RGB, every scale-th pixel */

static int grab(int fd, const char *path, double t)
{
	uint32_t fb_id = scanout_fb(fd);
	if (!fb_id) {
		fprintf(stderr, "no framebuffer on screen\n");
		return -1;
	}
	drmModeFB2 *fb = drmModeGetFB2(fd, fb_id);
	if (!fb || !fb->handles[0]) {
		fprintf(stderr, "GETFB2: %s (run as root)\n", strerror(errno));
		return -1;
	}
	uint32_t h = fb->height;
	/* bytes needed: up to the end of the last plane (two planes at most here: NV12) */
	uint64_t need = (uint64_t)fb->offsets[0] + (uint64_t)fb->pitches[0] * h;
	if (fb->pitches[1])
		need = (uint64_t)fb->offsets[1] + (uint64_t)fb->pitches[1] * ((h + 1) / 2);
	/* dumb buffers (eo1-show) map through the DRM device; a video frame is a dmabuf imported from the VPU,
	 * which only maps through its own dmabuf fd */
	uint8_t *map = MAP_FAILED;
	int dfd = -1;
	struct drm_mode_map_dumb m = {.handle = fb->handles[0]};
	if (!drmIoctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &m))
		map = mmap(0, need, PROT_READ, MAP_SHARED, fd, m.offset);
	if (map == MAP_FAILED && !drmPrimeHandleToFD(fd, fb->handles[0], DRM_CLOEXEC, &dfd))
		map = mmap(0, need, PROT_READ, MAP_SHARED, dfd, 0);
	if (map == MAP_FAILED) {
		fprintf(stderr, "cannot map the framebuffer: %s\n", strerror(errno));
		if (dfd >= 0)
			close(dfd);
		return -1;
	}
	uint8_t *copy = malloc(need);      /* one fast pass over the (uncached) buffer, then write */
	memcpy(copy, map, need);
	munmap(map, need);
	if (dfd >= 0)
		close(dfd);
	FILE *f = fopen(path, "wb");
	if (!f) {
		perror(path);
		free(copy);
		return -1;
	}
	char cc[5] = {fb->pixel_format & 0xff, (fb->pixel_format >> 8) & 0xff, (fb->pixel_format >> 16) & 0xff,
		      (fb->pixel_format >> 24) & 0xff, 0};
	if (scale >= 1) {
		/* small RGB copy (every scale-th pixel, BT.601), for bursts over Wi-Fi */
		uint32_t ow = fb->width / scale, oh = h / scale;
		uint8_t *o = malloc((size_t)ow * oh * 3), *q = o;
		for (uint32_t y = 0; y < oh; y++) {
			for (uint32_t x = 0; x < ow; x++, q += 3) {
				uint32_t sx = x * scale, sy = y * scale;
				int Y = 0, U = 128, V = 128, R, G, B;
				if (fb->pixel_format == DRM_FORMAT_XRGB8888 || fb->pixel_format == DRM_FORMAT_ARGB8888) {
					const uint8_t *p = copy + fb->offsets[0] + (size_t)sy * fb->pitches[0] + sx * 4;
					q[0] = p[2]; q[1] = p[1]; q[2] = p[0];
					continue;
				} else if (fb->pixel_format == DRM_FORMAT_RGB565) {   /* fb0 (fbdev) */
					const uint8_t *p = copy + fb->offsets[0] + (size_t)sy * fb->pitches[0] + sx * 2;
					unsigned v = p[0] | p[1] << 8;
					q[0] = (v >> 11 & 31) << 3; q[1] = (v >> 5 & 63) << 2; q[2] = (v & 31) << 3;
					continue;
				} else if (fb->pixel_format == DRM_FORMAT_YUYV) {
					const uint8_t *p = copy + fb->offsets[0] + (size_t)sy * fb->pitches[0] + (sx & ~1u) * 2;
					Y = p[(sx & 1) * 2]; U = p[1]; V = p[3];
				} else if (fb->pixel_format == DRM_FORMAT_NV12) {
					Y = copy[fb->offsets[0] + (size_t)sy * fb->pitches[0] + sx];
					const uint8_t *c = copy + fb->offsets[1] + (size_t)(sy / 2) * fb->pitches[1] + (sx & ~1u);
					U = c[0]; V = c[1];
				}
				int C = Y - 16, D = U - 128, E = V - 128;
				R = (298 * C + 409 * E + 128) >> 8;
				G = (298 * C - 100 * D - 208 * E + 128) >> 8;
				B = (298 * C + 516 * D + 128) >> 8;
				q[0] = R < 0 ? 0 : R > 255 ? 255 : R;
				q[1] = G < 0 ? 0 : G > 255 ? 255 : G;
				q[2] = B < 0 ? 0 : B > 255 ? 255 : B;
			}
		}
		fprintf(f, "EO1GRAB RGB3 %u %u %u 0 0 0 %.0f %s\n", ow, oh, ow * 3, t, cc);
		fwrite(o, 1, (size_t)ow * oh * 3, f);
		free(o);
	} else {
		fprintf(f, "EO1GRAB %s %u %u %u %u %u %u %.0f\n", cc, fb->width, h, fb->pitches[0], fb->offsets[0],
			fb->pitches[1], fb->offsets[1], t);
		fwrite(copy, 1, need, f);
	}
	fclose(f);
	free(copy);
	struct drm_gem_close gc = {.handle = fb->handles[0]};
	drmIoctl(fd, DRM_IOCTL_GEM_CLOSE, &gc);
	if (fb->handles[1] && fb->handles[1] != fb->handles[0]) {
		gc.handle = fb->handles[1];
		drmIoctl(fd, DRM_IOCTL_GEM_CLOSE, &gc);
	}
	printf("%s %s %ux%u %.0f ms\n", path, cc, fb->width, h, t);
	drmModeFreeFB2(fb);
	return 0;
}

/* --planes: which plane shows what (debugging the handover between eo1-show, fbdev and the video) */
static void planes(int fd)
{
	drmSetClientCap(fd, DRM_CLIENT_CAP_UNIVERSAL_PLANES, 1);
	drmModePlaneRes *pr = drmModeGetPlaneResources(fd);
	for (uint32_t i = 0; pr && i < pr->count_planes; i++) {
		drmModePlane *p = drmModeGetPlane(fd, pr->planes[i]);
		if (!p)
			continue;
		const char *type = "?";
		drmModeObjectProperties *props = drmModeObjectGetProperties(fd, p->plane_id, DRM_MODE_OBJECT_PLANE);
		for (uint32_t k = 0; props && k < props->count_props; k++) {
			drmModePropertyRes *pp = drmModeGetProperty(fd, props->props[k]);
			if (pp && !strcmp(pp->name, "type"))
				type = props->prop_values[k] == DRM_PLANE_TYPE_OVERLAY ? "overlay" :
				       props->prop_values[k] == DRM_PLANE_TYPE_PRIMARY ? "primary" : "cursor";
			if (pp && p->possible_crtcs & 1)
				printf("  plane %u prop %s = %llu\n", p->plane_id, pp->name,
				       (unsigned long long)props->prop_values[k]);
			drmModeFreeProperty(pp);
		}
		drmModeFreeObjectProperties(props);
		char cc[5] = "-";
		if (p->fb_id) {
			drmModeFB2 *fb = drmModeGetFB2(fd, p->fb_id);
			if (fb) {
				snprintf(cc, sizeof cc, "%c%c%c%c", fb->pixel_format & 0xff, (fb->pixel_format >> 8) & 0xff,
					 (fb->pixel_format >> 16) & 0xff, (fb->pixel_format >> 24) & 0xff);
				drmModeFreeFB2(fb);
			}
		}
		printf("plane %u %s crtc %u fb %u %s at %d,%d possible_crtcs %#x formats %u\n", p->plane_id, type, p->crtc_id,
		       p->fb_id, cc, p->crtc_x, p->crtc_y, p->possible_crtcs, p->count_formats);
		drmModeFreePlane(p);
	}
	drmModeFreePlaneResources(pr);
}

int main(int argc, char **argv)
{
	if (argc > 1 && !strcmp(argv[1], "--planes")) {
		int fd = open("/dev/dri/by-path/platform-display-subsystem-card", O_RDWR | O_CLOEXEC);
		if (fd < 0)
			return 1;
		drmDropMaster(fd);
		planes(fd);
		close(fd);
		return 0;
	}
	if (argc < 2) {
		fprintf(stderr, "usage: %s out-prefix [count] [interval_ms]\n", argv[0]);
		return 2;
	}
	const char *card = "/dev/dri/by-path/platform-display-subsystem-card";
	int count = argc > 2 ? atoi(argv[2]) : 1, interval = argc > 3 ? atoi(argv[3]) : 200;
	scale = argc > 4 ? atoi(argv[4]) : 0;
	double t0 = now_ms();
	int bad = 0;
	for (int i = 0; i < count; i++) {
		char path[1024];
		snprintf(path, sizeof path, "%s-%03d.grab", argv[1], i);
		double due = t0 + (double)i * interval;
		while (now_ms() < due)
			usleep(2000);
		/* the card is open only for the moment of one grab: a DRM fd held across the player's handover (eo1-show
		 * quitting, the video starting) keeps the kernel from restoring the display, and the video then finds no
		 * mode and does not start (seen twice during switch captures). Reading needs root, not master. */
		int fd = open(card, O_RDWR | O_CLOEXEC);
		if (fd < 0)
			fd = open("/dev/dri/card1", O_RDWR | O_CLOEXEC);
		if (fd < 0) {
			perror(card);
			return 1;
		}
		drmDropMaster(fd);
		bad |= grab(fd, path, now_ms() - t0) != 0;
		close(fd);
	}
	return bad;
}
