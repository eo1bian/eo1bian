/* eo1-layers: the one owner of the EO1 panel for eo1-player, built on the i.MX6 display processor's two layers.
 *
 * The primary plane (bottom) and the overlay plane (top) are both on all the time; the overlay's global alpha
 * (the "alpha" plane property, added to the kernel by debian/build/kernel-ipu-alpha.py) decides how much of the
 * top layer shows. A switch puts the new picture or video on the hidden layer and ramps the alpha, so:
 *   - both sources keep moving during a cross-fade (the VPU decodes both, the display processor blends);
 *   - no plane is ever switched on or off and no other process takes the display over (both did draw short
 *     black lines on the panel);
 *   - video frames go from the VPU to the screen as dmabufs: no CPU copy (the copying compositor used 50-90 %).
 * Without the alpha property (older kernel) switches are instant instead of faded.
 *
 * Commands on stdin, one per line; one reply line on stdout each:
 *   image <file.raw> <fade_ms>   raw panel frame 1920x1080x4 (Pillow "BGRX" = DRM XRGB8888)
 *   video <file.mp4> <fade_ms>   H.264 exactly 1920x1080 (the server's .n2.eo1.mp4), looped seamlessly
 *   quit
 * replies "ok <ms>" once the new source is fully up, "error <text>" when it cannot be shown (the previous source
 * stays). "ready WxH alpha|noalpha" at start.
 *
 * Everything on both layers is YUYV with the same pitch (the VPU's format): the i.MX6 driver forces a full
 * modeset - all planes off for a moment, a black line on the panel - whenever a plane's format or pitch changes
 * (ipu_plane_atomic_check). So pictures are converted to YUYV (BT.601 limited range, the planes' default) once,
 * and the black buffer is YUYV too; every switch is then a plain buffer flip.
 *
 * Videos are demuxed once into memory and fed to appsrc ! v4l2h264dec ! appsink as an endless stream with
 * timestamps moved on by the clip length per pass (a seamless loop: the decoder never drains). The appsink
 * answers the allocation query with VideoMeta support, so the decoder hands over its own dmabufs (1088-line
 * buffers) instead of copying every frame to 1080 lines.
 * Build: gcc -O2 -o eo1-layers eo1-layers.c $(pkg-config --cflags --libs gstreamer-1.0 gstreamer-app-1.0
 *        gstreamer-video-1.0 gstreamer-allocators-1.0 libdrm)
 */
#include <errno.h>
#include <fcntl.h>
#include <gst/allocators/gstdmabuf.h>
#include <gst/app/gstappsink.h>
#include <gst/app/gstappsrc.h>
#include <gst/gst.h>
#include <gst/video/video.h>
#include <poll.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <time.h>
#include <unistd.h>
#include <drm_fourcc.h>
#include <xf86drm.h>
#include <xf86drmMode.h>

#define CARD "/dev/dri/by-path/platform-display-subsystem-card"

static int fd;
static uint32_t conn_id, crtc_id, mode_blob;
static drmModeModeInfo mode;
static int W, H;

/* ------------------------------------------------------------------ small helpers */
static double now(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec + t.tv_nsec / 1e9;
}

static void reply(const char *fmt, ...)
{
	va_list ap;
	va_start(ap, fmt);
	vprintf(fmt, ap);
	va_end(ap);
	putchar('\n');
	fflush(stdout);
}

static uint32_t prop_id(uint32_t obj, uint32_t type, const char *name)
{
	uint32_t id = 0;
	drmModeObjectProperties *props = drmModeObjectGetProperties(fd, obj, type);
	for (uint32_t i = 0; props && i < props->count_props && !id; i++) {
		drmModePropertyRes *p = drmModeGetProperty(fd, props->props[i]);
		if (p && !strcmp(p->name, name))
			id = p->prop_id;
		drmModeFreeProperty(p);
	}
	drmModeFreeObjectProperties(props);
	return id;
}

/* ------------------------------------------------------------------ layers (planes) */
struct plane_props {
	uint32_t fb, crtc, src_x, src_y, src_w, src_h, crtc_x, crtc_y, crtc_w, crtc_h, alpha;
};

typedef struct {
	uint32_t id;
	struct plane_props p;
	uint32_t fb;              /* framebuffer on screen (or committed) */
	uint32_t want;            /* framebuffer to show at the next commit (0 = unchanged) */
	GstSample *shown, *next;  /* video: the sample behind fb / behind want, kept alive until replaced on screen */
	GstSample *prev;          /* the sample before shown: released once the next flip has completed */
} Layer;

static Layer L[2];               /* 0 = primary (bottom), 1 = overlay (top) */
static uint32_t alpha_prop;      /* 0: kernel without the patch */
static uint32_t alpha_now = 0, alpha_set = 0xffffffff;
static int flip_pending;
static uint32_t black_fb;         /* shown on a hidden layer whose source has gone */

static void get_plane_props(Layer *l)
{
	struct plane_props *p = &l->p;
	p->fb = prop_id(l->id, DRM_MODE_OBJECT_PLANE, "FB_ID");
	p->crtc = prop_id(l->id, DRM_MODE_OBJECT_PLANE, "CRTC_ID");
	p->src_x = prop_id(l->id, DRM_MODE_OBJECT_PLANE, "SRC_X");
	p->src_y = prop_id(l->id, DRM_MODE_OBJECT_PLANE, "SRC_Y");
	p->src_w = prop_id(l->id, DRM_MODE_OBJECT_PLANE, "SRC_W");
	p->src_h = prop_id(l->id, DRM_MODE_OBJECT_PLANE, "SRC_H");
	p->crtc_x = prop_id(l->id, DRM_MODE_OBJECT_PLANE, "CRTC_X");
	p->crtc_y = prop_id(l->id, DRM_MODE_OBJECT_PLANE, "CRTC_Y");
	p->crtc_w = prop_id(l->id, DRM_MODE_OBJECT_PLANE, "CRTC_W");
	p->crtc_h = prop_id(l->id, DRM_MODE_OBJECT_PLANE, "CRTC_H");
	p->alpha = prop_id(l->id, DRM_MODE_OBJECT_PLANE, "alpha");
}

static void flip_handler(int f, unsigned int seq, unsigned int s, unsigned int us, unsigned int crtc, void *data)
{
	(void)f; (void)seq; (void)s; (void)us; (void)crtc; (void)data;
	flip_pending = 0;
	for (int i = 0; i < 2; i++)                 /* the flip is done: what was on screen before can go */
		if (L[i].prev) {
			gst_sample_unref(L[i].prev);
			L[i].prev = NULL;
		}
}

static void wait_flip(int timeout_ms)
{
	struct pollfd pfd = {.fd = fd, .events = POLLIN};
	drmEventContext ev = {.version = 3, .page_flip_handler2 = flip_handler};
	double end = now() + timeout_ms / 1000.0;
	while (flip_pending) {
		int left = (int)((end - now()) * 1000);
		if (left <= 0 || poll(&pfd, 1, left) <= 0)
			break;
		drmHandleEvent(fd, &ev);
	}
}

/* commit what changed: new framebuffers, the overlay alpha; the first commit sets the mode */
static int commit(int modeset)
{
	drmModeAtomicReq *req = drmModeAtomicAlloc();
	int changes = 0;
	if (modeset) {
		if (!L[1].fb && !L[1].want)          /* both layers need a buffer: the top one starts black, invisible */
			L[1].want = black_fb;
		if (!L[0].fb && !L[0].want)
			L[0].want = black_fb;
		drmModeAtomicAddProperty(req, conn_id, prop_id(conn_id, DRM_MODE_OBJECT_CONNECTOR, "CRTC_ID"), crtc_id);
		drmModeAtomicAddProperty(req, crtc_id, prop_id(crtc_id, DRM_MODE_OBJECT_CRTC, "MODE_ID"), mode_blob);
		drmModeAtomicAddProperty(req, crtc_id, prop_id(crtc_id, DRM_MODE_OBJECT_CRTC, "ACTIVE"), 1);
		for (int i = 0; i < 2; i++) {
			struct plane_props *p = &L[i].p;
			drmModeAtomicAddProperty(req, L[i].id, p->crtc, crtc_id);
			drmModeAtomicAddProperty(req, L[i].id, p->src_x, 0);
			drmModeAtomicAddProperty(req, L[i].id, p->src_y, 0);
			drmModeAtomicAddProperty(req, L[i].id, p->src_w, (uint64_t)W << 16);
			drmModeAtomicAddProperty(req, L[i].id, p->src_h, (uint64_t)H << 16);
			drmModeAtomicAddProperty(req, L[i].id, p->crtc_x, 0);
			drmModeAtomicAddProperty(req, L[i].id, p->crtc_y, 0);
			drmModeAtomicAddProperty(req, L[i].id, p->crtc_w, W);
			drmModeAtomicAddProperty(req, L[i].id, p->crtc_h, H);
		}
		changes++;
	}
	for (int i = 0; i < 2; i++) {
		if (L[i].want) {
			drmModeAtomicAddProperty(req, L[i].id, L[i].p.fb, L[i].want);
			changes++;
		}
	}
	if (alpha_prop && alpha_now != alpha_set) {
		drmModeAtomicAddProperty(req, L[1].id, alpha_prop, alpha_now);
		changes++;
	}
	if (!changes) {
		drmModeAtomicFree(req);
		return 0;
	}
	uint32_t flags = DRM_MODE_PAGE_FLIP_EVENT | (modeset ? DRM_MODE_ATOMIC_ALLOW_MODESET : DRM_MODE_ATOMIC_NONBLOCK);
	int ret = drmModeAtomicCommit(fd, req, flags, NULL);
	drmModeAtomicFree(req);
	if (ret) {
		fprintf(stderr, "atomic commit: %s\n", strerror(errno));
		return -1;
	}
	flip_pending = 1;
	alpha_set = alpha_now;
	for (int i = 0; i < 2; i++) {
		if (!L[i].want)
			continue;
		L[i].fb = L[i].want;
		L[i].want = 0;
		if (L[i].prev)                      /* not released yet (flip events can lag): release now */
			gst_sample_unref(L[i].prev);
		L[i].prev = L[i].shown;             /* still scanned out until this flip completes */
		L[i].shown = L[i].next;
		L[i].next = NULL;
	}
	if (modeset)
		wait_flip(1000);
	return 0;
}

/* ------------------------------------------------------------------ sources */
typedef struct {
	int video;
	uint32_t fb;                 /* image: its framebuffer */
	uint32_t handle;
	/* video */
	char path[1024];
	GPtrArray *aus;
	GstCaps *caps;
	GstClockTime period;
	GstElement *pipe;
	GstAppSrc *in;
	GstAppSink *sink;
	GThread *loader, *feeder;
	volatile int stop, ready, failed;
	int have;                    /* a first frame has been put on its layer */
	struct { int dfd; uint32_t handle, fb; } cache[16];   /* dmabuf -> framebuffer, per decoder buffer */
	int ncache;
	char err[256];
} Src;

static uint32_t yuyv_pitch;     /* the pitch every layer buffer must have (the VPU's: W * 2) */

/* a YUYV dumb buffer + framebuffer; *map is its memory (unmap with size *size) */
static int yuyv_dumb(uint32_t *handle, uint32_t *fbid, uint8_t **map, uint64_t *size, uint32_t *pitch)
{
	struct drm_mode_create_dumb c = {.width = W, .height = H, .bpp = 16};
	if (drmIoctl(fd, DRM_IOCTL_MODE_CREATE_DUMB, &c) < 0)
		return -1;
	uint32_t handles[4] = {c.handle}, pitches[4] = {c.pitch}, offsets[4] = {0};
	if (drmModeAddFB2(fd, W, H, DRM_FORMAT_YUYV, handles, pitches, offsets, fbid, 0)) {
		struct drm_mode_destroy_dumb d = {.handle = c.handle};
		drmIoctl(fd, DRM_IOCTL_MODE_DESTROY_DUMB, &d);
		return -1;
	}
	struct drm_mode_map_dumb m = {.handle = c.handle};
	drmIoctl(fd, DRM_IOCTL_MODE_MAP_DUMB, &m);
	*map = mmap(0, c.size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, m.offset);
	*handle = c.handle;
	*size = c.size;
	*pitch = c.pitch;
	return *map == MAP_FAILED ? -1 : 0;
}

static Src *image_src(const char *path)
{
	FILE *f = fopen(path, "rb");
	if (!f)
		return NULL;
	Src *s = calloc(1, sizeof *s);
	uint8_t *map;
	uint64_t size;
	uint32_t pitch;
	if (yuyv_dumb(&s->handle, &s->fb, &map, &size, &pitch)) {
		fclose(f);
		free(s);
		return NULL;
	}
	if (pitch != yuyv_pitch)
		fprintf(stderr, "warning: picture pitch %u, video pitch %u (switches will flash)\n", pitch, yuyv_pitch);
	/* BGRX -> YUYV, BT.601 limited range; chroma = mean of the pixel pair */
	uint8_t *row = malloc((size_t)W * 4), *out = malloc((size_t)W * 2);
	int ok = 1;
	for (int y = 0; ok && y < H; y++) {
		if (fread(row, 1, (size_t)W * 4, f) != (size_t)W * 4) {
			ok = 0;
			break;
		}
		const uint8_t *p = row;
		uint8_t *o = out;
		for (int x = 0; x < W; x += 2, p += 8, o += 4) {
			int b0 = p[0], g0 = p[1], r0 = p[2], b1 = p[4], g1 = p[5], r1 = p[6];
			int r = (r0 + r1) >> 1, g = (g0 + g1) >> 1, b = (b0 + b1) >> 1;
			o[0] = (uint8_t)(((66 * r0 + 129 * g0 + 25 * b0 + 128) >> 8) + 16);
			o[1] = (uint8_t)(((-38 * r - 74 * g + 112 * b + 128) >> 8) + 128);
			o[2] = (uint8_t)(((66 * r1 + 129 * g1 + 25 * b1 + 128) >> 8) + 16);
			o[3] = (uint8_t)(((112 * r - 94 * g - 18 * b + 128) >> 8) + 128);
		}
		memcpy(map + (size_t)y * pitch, out, (size_t)W * 2);   /* one sequential write per row (write-combined) */
	}
	free(row);
	free(out);
	fclose(f);
	munmap(map, size);
	if (!ok) {
		drmModeRmFB(fd, s->fb);
		struct drm_mode_destroy_dumb d = {.handle = s->handle};
		drmIoctl(fd, DRM_IOCTL_MODE_DESTROY_DUMB, &d);
		free(s);
		return NULL;
	}
	s->have = 1;
	return s;
}

static gpointer feeder(gpointer data)
{
	Src *s = data;
	for (guint64 n = 0; !s->stop; n++) {
		GstClockTime off = n * s->period;
		for (guint i = 0; i < s->aus->len && !s->stop; i++) {
			GstBuffer *b = gst_buffer_copy(g_ptr_array_index(s->aus, i));
			if (GST_BUFFER_PTS_IS_VALID(b))
				GST_BUFFER_PTS(b) += off;
			if (GST_BUFFER_DTS_IS_VALID(b))
				GST_BUFFER_DTS(b) += off;
			if (gst_app_src_push_buffer(s->in, b) != GST_FLOW_OK)
				return NULL;
		}
	}
	return NULL;
}

/* the appsink says it understands strides/offsets (VideoMeta): the decoder then hands
 * over its own dmabufs untouched instead of copying each frame with the CPU */
static GstPadProbeReturn alloc_probe(GstPad *pad, GstPadProbeInfo *info, gpointer data)
{
	(void)pad; (void)data;
	GstQuery *q = GST_PAD_PROBE_INFO_QUERY(info);
	if (GST_QUERY_TYPE(q) == GST_QUERY_ALLOCATION) {
		gst_query_add_allocation_meta(q, GST_VIDEO_META_API_TYPE, NULL);
	}
	return GST_PAD_PROBE_OK;
}

static gpointer loader(gpointer data)
{
	Src *s = data;
	gchar *loc = g_strescape(s->path, NULL);
	gchar *desc = g_strdup_printf("filesrc location=\"%s\" ! qtdemux ! h264parse config-interval=-1 ! "
				      "video/x-h264,stream-format=byte-stream,alignment=au ! appsink name=s sync=false", loc);
	g_free(loc);
	GError *e = NULL;
	GstElement *p = gst_parse_launch(desc, &e);
	g_free(desc);
	if (!p) {
		snprintf(s->err, sizeof s->err, "demux: %s", e ? e->message : "?");
		s->failed = 1;
		return NULL;
	}
	GstAppSink *sk = GST_APP_SINK(gst_bin_get_by_name(GST_BIN(p), "s"));
	gst_element_set_state(p, GST_STATE_PLAYING);
	GstClockTime first = GST_CLOCK_TIME_NONE, end = 0;
	GstSample *sm;
	s->aus = g_ptr_array_new_with_free_func((GDestroyNotify)gst_buffer_unref);
	while ((sm = gst_app_sink_pull_sample(sk))) {
		if (!s->caps)
			s->caps = gst_caps_ref(gst_sample_get_caps(sm));
		GstBuffer *b = gst_sample_get_buffer(sm);
		GstClockTime pts = GST_BUFFER_PTS(b), d = GST_BUFFER_DURATION(b);
		if (GST_CLOCK_TIME_IS_VALID(pts)) {
			if (!GST_CLOCK_TIME_IS_VALID(first) || pts < first)
				first = pts;
			GstClockTime e2 = pts + (GST_CLOCK_TIME_IS_VALID(d) ? d : 0);
			if (e2 > end)
				end = e2;
		}
		g_ptr_array_add(s->aus, gst_buffer_ref(b));
		gst_sample_unref(sm);
	}
	gst_element_set_state(p, GST_STATE_NULL);
	gst_object_unref(sk);
	gst_object_unref(p);
	if (!s->aus->len || !GST_CLOCK_TIME_IS_VALID(first) || end <= first) {
		snprintf(s->err, sizeof s->err, "no H.264 video in the file");
		s->failed = 1;
		return NULL;
	}
	s->period = end - first;
	s->pipe = gst_parse_launch("appsrc name=in format=time block=true max-bytes=1500000 ! "
				   "v4l2h264dec capture-io-mode=dmabuf ! video/x-raw,format=YUY2 ! "
				   "appsink name=sink sync=true max-buffers=2", &e);
	if (!s->pipe) {
		snprintf(s->err, sizeof s->err, "decoder: %s", e ? e->message : "?");
		s->failed = 1;
		return NULL;
	}
	s->in = GST_APP_SRC(gst_bin_get_by_name(GST_BIN(s->pipe), "in"));
	s->sink = GST_APP_SINK(gst_bin_get_by_name(GST_BIN(s->pipe), "sink"));
	GstPad *sp = gst_element_get_static_pad(GST_ELEMENT(s->sink), "sink");
	gst_pad_add_probe(sp, GST_PAD_PROBE_TYPE_QUERY_DOWNSTREAM, alloc_probe, NULL, NULL);
	gst_object_unref(sp);
	gst_app_src_set_caps(s->in, s->caps);
	if (gst_element_set_state(s->pipe, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
		snprintf(s->err, sizeof s->err, "the decoder does not start");
		s->failed = 1;
		return NULL;
	}
	s->feeder = g_thread_new("feeder", feeder, s);
	s->ready = 1;
	return NULL;
}

static Src *video_src(const char *path)
{
	Src *s = calloc(1, sizeof *s);
	s->video = 1;
	snprintf(s->path, sizeof s->path, "%s", path);
	s->loader = g_thread_new("loader", loader, s);
	return s;
}

static void check_bus(Src *s)
{
	if (!s || !s->video || !s->pipe || s->failed)
		return;
	GstBus *bus = gst_element_get_bus(s->pipe);
	GstMessage *m;
	while ((m = gst_bus_pop_filtered(bus, GST_MESSAGE_ERROR))) {
		GError *e = NULL;
		gst_message_parse_error(m, &e, NULL);
		snprintf(s->err, sizeof s->err, "%s", e ? e->message : "decoder error");
		g_clear_error(&e);
		gst_message_unref(m);
		s->failed = 1;
	}
	gst_object_unref(bus);
}

/* a decoded frame -> a framebuffer on the decoder's own memory (cached per buffer: the decoder reuses a pool) */
static uint32_t frame_fb(Src *s, GstSample *sm)
{
	GstBuffer *b = gst_sample_get_buffer(sm);
	GstMemory *mem = gst_buffer_peek_memory(b, 0);
	if (!gst_is_dmabuf_memory(mem)) {
		snprintf(s->err, sizeof s->err, "decoder output is not a dmabuf");
		s->failed = 1;
		return 0;
	}
	int dfd = gst_dmabuf_memory_get_fd(mem);
	for (int i = 0; i < s->ncache; i++)
		if (s->cache[i].dfd == dfd)
			return s->cache[i].fb;
	GstVideoInfo vi;
	gst_video_info_from_caps(&vi, gst_sample_get_caps(sm));
	GstVideoMeta *vm = gst_buffer_get_video_meta(b);
	uint32_t w = vm ? vm->width : GST_VIDEO_INFO_WIDTH(&vi), h = vm ? vm->height : GST_VIDEO_INFO_HEIGHT(&vi);
	if ((int)w != W || (int)h != H) {
		snprintf(s->err, sizeof s->err, "video is %ux%u, the panel %dx%d", w, h, W, H);
		s->failed = 1;
		return 0;
	}
	uint32_t handle;
	if (drmPrimeFDToHandle(fd, dfd, &handle)) {
		snprintf(s->err, sizeof s->err, "dmabuf import: %s", strerror(errno));
		s->failed = 1;
		return 0;
	}
	uint32_t handles[4] = {handle}, pitches[4] = {vm ? vm->stride[0] : GST_VIDEO_INFO_PLANE_STRIDE(&vi, 0)},
		 offsets[4] = {(uint32_t)(mem->offset + (vm ? vm->offset[0] : 0))}, fbid = 0;
	if (pitches[0] != yuyv_pitch)
		fprintf(stderr, "warning: video pitch %u, layer pitch %u (switches will flash)\n", pitches[0], yuyv_pitch);
	if (drmModeAddFB2(fd, w, h, DRM_FORMAT_YUYV, handles, pitches, offsets, &fbid, 0)) {
		snprintf(s->err, sizeof s->err, "framebuffer: %s", strerror(errno));
		s->failed = 1;
		return 0;
	}
	if (s->ncache < 16) {
		s->cache[s->ncache].dfd = dfd;
		s->cache[s->ncache].handle = handle;
		s->cache[s->ncache].fb = fbid;
		s->ncache++;
	}
	return fbid;
}

/* newest decoded frame of a video onto its layer (non-blocking). Returns 1 if the layer got a new frame. */
static int pump(Src *s, Layer *l)
{
	if (!s || !s->video || !s->ready || s->failed || l->want)
		return 0;
	GstSample *sm = gst_app_sink_try_pull_sample(s->sink, 0);
	if (!sm)
		return 0;
	uint32_t fbid = frame_fb(s, sm);
	if (!fbid) {
		gst_sample_unref(sm);
		return 0;
	}
	l->want = fbid;
	l->next = sm;
	s->have = 1;
	return 1;
}

static void destroy(Src *s, Layer *l)
{
	if (!s)
		return;
	if (s->video) {
		/* the layer must not scan out the decoder's memory any more: black there first (it is hidden) */
		if (l && l->fb && black_fb) {
			if (flip_pending)
				wait_flip(200);
			if (l->next) {
				gst_sample_unref(l->next);
				l->next = NULL;
			}
			l->want = black_fb;
			commit(0);
			wait_flip(200);
			if (l->shown) {
				gst_sample_unref(l->shown);
				l->shown = NULL;
			}
			if (l->prev) {
				gst_sample_unref(l->prev);
				l->prev = NULL;
			}
		}
		if (s->loader)
			g_thread_join(s->loader);
		s->stop = 1;
		if (s->pipe)
			gst_element_set_state(s->pipe, GST_STATE_NULL);
		if (s->feeder)
			g_thread_join(s->feeder);
		for (int i = 0; i < s->ncache; i++) {
			drmModeRmFB(fd, s->cache[i].fb);
			struct drm_gem_close gc = {.handle = s->cache[i].handle};
			drmIoctl(fd, DRM_IOCTL_GEM_CLOSE, &gc);
		}
		if (s->in)
			gst_object_unref(s->in);
		if (s->sink)
			gst_object_unref(s->sink);
		if (s->pipe)
			gst_object_unref(s->pipe);
		if (s->aus)
			g_ptr_array_unref(s->aus);
		if (s->caps)
			gst_caps_unref(s->caps);
	} else {
		if (l && l->fb == s->fb && black_fb) {     /* hidden layer: black before the buffer goes */
			if (flip_pending)
				wait_flip(200);
			l->want = black_fb;
			commit(0);
			wait_flip(200);
		}
		drmModeRmFB(fd, s->fb);
		struct drm_mode_destroy_dumb d = {.handle = s->handle};
		drmIoctl(fd, DRM_IOCTL_MODE_DESTROY_DUMB, &d);
	}
	free(s);
}

/* ------------------------------------------------------------------ setup */
static int setup(void)
{
	fd = open(CARD, O_RDWR | O_CLOEXEC);
	if (fd < 0) {
		perror(CARD);
		return -1;
	}
	drmSetMaster(fd);
	if (drmSetClientCap(fd, DRM_CLIENT_CAP_UNIVERSAL_PLANES, 1) || drmSetClientCap(fd, DRM_CLIENT_CAP_ATOMIC, 1)) {
		fprintf(stderr, "no atomic modesetting\n");
		return -1;
	}
	drmModeRes *res = drmModeGetResources(fd);
	drmModeConnector *conn = NULL;
	for (int i = 0; res && i < res->count_connectors; i++) {
		drmModeConnector *c = drmModeGetConnector(fd, res->connectors[i]);
		if (c && c->connection == DRM_MODE_CONNECTED && c->count_modes > 0 && c->connector_type == DRM_MODE_CONNECTOR_LVDS) {
			conn = c;
			break;
		}
		drmModeFreeConnector(c);
	}
	if (!conn) {
		fprintf(stderr, "no connected LVDS connector\n");
		return -1;
	}
	conn_id = conn->connector_id;
	mode = conn->modes[0];
	for (int i = 0; i < conn->count_modes; i++)
		if (conn->modes[i].type & DRM_MODE_TYPE_PREFERRED)
			mode = conn->modes[i];
	W = mode.hdisplay;
	H = mode.vdisplay;
	int crtc_index = -1;
	for (int e = 0; e < conn->count_encoders && crtc_index < 0; e++) {
		drmModeEncoder *enc = drmModeGetEncoder(fd, conn->encoders[e]);
		for (int c = 0; enc && c < res->count_crtcs && crtc_index < 0; c++)
			if (enc->possible_crtcs & (1u << c))
				crtc_index = c;
		drmModeFreeEncoder(enc);
	}
	if (crtc_index < 0) {
		fprintf(stderr, "no CRTC\n");
		return -1;
	}
	crtc_id = res->crtcs[crtc_index];
	drmModePlaneRes *pr = drmModeGetPlaneResources(fd);
	for (uint32_t i = 0; pr && i < pr->count_planes; i++) {
		drmModePlane *p = drmModeGetPlane(fd, pr->planes[i]);
		if (p && (p->possible_crtcs & (1u << crtc_index))) {
			drmModeObjectProperties *props = drmModeObjectGetProperties(fd, p->plane_id, DRM_MODE_OBJECT_PLANE);
			for (uint32_t k = 0; props && k < props->count_props; k++) {
				drmModePropertyRes *pp = drmModeGetProperty(fd, props->props[k]);
				if (pp && !strcmp(pp->name, "type")) {
					if (props->prop_values[k] == DRM_PLANE_TYPE_PRIMARY && !L[0].id)
						L[0].id = p->plane_id;
					else if (props->prop_values[k] == DRM_PLANE_TYPE_OVERLAY && !L[1].id)
						L[1].id = p->plane_id;
				}
				drmModeFreeProperty(pp);
			}
			drmModeFreeObjectProperties(props);
		}
		drmModeFreePlane(p);
	}
	drmModeFreePlaneResources(pr);
	drmModeFreeConnector(conn);
	drmModeFreeResources(res);
	if (!L[0].id || !L[1].id) {
		fprintf(stderr, "need a primary and an overlay plane\n");
		return -1;
	}
	get_plane_props(&L[0]);
	get_plane_props(&L[1]);
	alpha_prop = L[1].p.alpha;
	for (int i = 0; i < 2; i++)
		fprintf(stderr, "layer %d: plane %u fb %u crtc %u src %u/%u/%u/%u dst %u/%u/%u/%u alpha %u\n", i, L[i].id, L[i].p.fb,
			L[i].p.crtc, L[i].p.src_x, L[i].p.src_y, L[i].p.src_w, L[i].p.src_h, L[i].p.crtc_x, L[i].p.crtc_y,
			L[i].p.crtc_w, L[i].p.crtc_h, L[i].p.alpha);
	if (drmModeCreatePropertyBlob(fd, &mode, sizeof mode, &mode_blob)) {
		fprintf(stderr, "mode blob: %s\n", strerror(errno));
		return -1;
	}
	/* a black YUYV buffer for hidden layers whose source has gone (and to start the top layer with) */
	yuyv_pitch = W * 2;
	uint32_t bh, bpitch;
	uint8_t *bmap;
	uint64_t bsize;
	if (!yuyv_dumb(&bh, &black_fb, &bmap, &bsize, &bpitch)) {
		yuyv_pitch = bpitch;
		uint8_t *row = malloc((size_t)W * 2);
		for (int x = 0; x < W * 2; x += 4) {
			row[x] = 16; row[x + 1] = 128; row[x + 2] = 16; row[x + 3] = 128;
		}
		for (int y = 0; y < H; y++)
			memcpy(bmap + (size_t)y * bpitch, row, (size_t)W * 2);
		free(row);
		munmap(bmap, bsize);
	}
	return 0;
}

/* ------------------------------------------------------------------ main loop */
static GAsyncQueue *cmds;

static gpointer reader(gpointer data)
{
	(void)data;
	char line[2048];
	while (fgets(line, sizeof line, stdin))
		g_async_queue_push(cmds, g_strdup(line));
	g_async_queue_push(cmds, g_strdup("quit\n"));
	return NULL;
}

int main(int argc, char **argv)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	gst_init(&argc, &argv);
	if (setup())
		return 1;
	cmds = g_async_queue_new_full(g_free);
	g_thread_new("stdin", reader, NULL);
	reply("ready %dx%d %s", W, H, alpha_prop ? "alpha" : "noalpha");

	Src *src[2] = {NULL, NULL};   /* what each layer shows */
	int cur = -1;                 /* the visible layer */
	int nxt = -1;                 /* the layer being faded in (or revealed) */
	int fade_ms = 0, modeset = 1, done = 0;
	double t0 = 0, fstart = 0;
	while (!done) {
		int busy = nxt >= 0 || (cur >= 0 && src[cur] && src[cur]->video);
		char *c = busy ? g_async_queue_timeout_pop(cmds, 4000) : g_async_queue_timeout_pop(cmds, 250000);
		if (c) {
			char path[1500];
			int ms = 0;
			if (!strncmp(c, "quit", 4)) {
				done = 1;
			} else if (sscanf(c, "image %1499s %d", path, &ms) >= 1 || sscanf(c, "video %1499s %d", path, &ms) >= 1) {
				if (nxt >= 0) {                       /* a fade still running: land it at once */
					alpha_now = nxt == 1 ? 0xffff : 0;
					destroy(src[cur], &L[cur]);
					src[cur] = NULL;
					cur = nxt;
					nxt = -1;
					commit(0);
					reply("ok 0");
				}
				Src *s = c[0] == 'i' ? image_src(path) : video_src(path);
				if (!s) {
					reply("error cannot read %s", path);
				} else {
					int layer = cur < 0 ? 0 : 1 - cur;
					if (src[layer])
						destroy(src[layer], &L[layer]);
					src[layer] = s;
					nxt = layer;
					fade_ms = cur < 0 || !alpha_prop ? 0 : ms;
					fstart = 0;
					t0 = now();
					if (!s->video) {
						L[layer].want = s->fb;
						if (L[layer].next) {
							gst_sample_unref(L[layer].next);
							L[layer].next = NULL;
						}
					}
				}
			}
			g_free(c);
			if (done)
				break;
		}
		/* drain flip events without waiting */
		if (flip_pending) {
			struct pollfd pfd = {.fd = fd, .events = POLLIN};
			if (poll(&pfd, 1, 0) > 0) {
				drmEventContext ev = {.version = 3, .page_flip_handler2 = flip_handler};
				drmHandleEvent(fd, &ev);
			}
		}
		for (int i = 0; i < 2; i++) {
			check_bus(src[i]);
			if (!flip_pending)
				pump(src[i], &L[i]);
		}
		if (nxt >= 0) {
			Src *s = src[nxt];
			if (s->failed) {
				reply("error %s", s->err);
				destroy(s, &L[nxt]);
				src[nxt] = NULL;
				nxt = -1;
				continue;
			}
			if (!s->have || (s->video && !L[nxt].fb && !L[nxt].want)) {
				if (now() - t0 > 20) {
					reply("error %s gave no frame in 20 s", s->video ? "video" : "image");
					destroy(s, &L[nxt]);
					src[nxt] = NULL;
					nxt = -1;
				}
				if (!flip_pending && (L[0].want || L[1].want))
					commit(modeset), modeset = 0;
				continue;
			}
			if (!fstart)
				fstart = now();
			double k = fade_ms > 0 ? (now() - fstart) * 1000.0 / fade_ms : 1.0;
			if (k > 1)
				k = 1;
			double e = k * k * (3 - 2 * k);           /* smoothstep, like the browser's cross-fade */
			/* top layer coming in: alpha up; bottom layer revealed: top alpha down */
			alpha_now = (uint32_t)((nxt == 1 ? e : 1 - e) * 0xffff + 0.5);
			if (!flip_pending)
				commit(modeset), modeset = 0;
			if (k >= 1 && !flip_pending) {
				wait_flip(100);
				if (cur >= 0) {
					destroy(src[cur], &L[cur]);
					src[cur] = NULL;
				}
				cur = nxt;
				nxt = -1;
				reply("ok %d", (int)((now() - t0) * 1000));
			}
			continue;
		}
		if (!flip_pending && (L[0].want || L[1].want))
			commit(modeset), modeset = 0;
		if (cur >= 0 && src[cur] && src[cur]->failed)
			fprintf(stderr, "video failed: %s\n", src[cur]->err), src[cur]->failed = 2;
	}
	for (int i = 0; i < 2; i++)
		destroy(src[i], NULL);
	return 0;
}
