/* WORK IN PROGRESS, NOT IN USE (3 Nov 2026 03:50): on the EO1 this grew to 264 MB RSS within 14 s and made
 * the frame unreachable, and the decoder thread still used ~50 % CPU into the appsink (vs 5 % into kmssink).
 * The live player is player.py with eo1-show + eo1-video; player-compose.py.wip is the matching player.
 *
 * eo1-compose: the one owner of the EO1 panel for eo1-player. Pictures and videos go through the same
 * kmssink, so nothing ever hands the display over (handovers flashed fb0 and drew a black line at every
 * modeset), and a cross-fade blends two live sources frame by frame: a video keeps moving while it fades out,
 * the next one is already moving while it fades in.
 *
 *   eo1-compose            commands on stdin, one per line, a reply line on stdout for each:
 *     image <file.raw> <fade_ms>   raw panel frame, 1920x1080x4 (Pillow "BGRX", as eo1-show took it)
 *     video <file.mp4> <fade_ms>   H.264, exactly 1920x1080 (the server's .n2.eo1.mp4), looped seamlessly
 *     quit
 *   replies: "ok <ms>" when the new source is fully on screen, "error <text>" when it could not be shown (the
 *   previous source stays up). "ready" at start.
 *
 * Everything is NV12 (the VPU's output): pictures are converted once (BT.601, limited range), blending is a
 * per-byte mix that gcc vectorises for NEON (3 MB per frame). Videos are demuxed once into memory and fed to
 * appsrc ! v4l2h264dec ! appsink as an endless stream, timestamps moved on by the clip length per pass (a
 * seamless loop: the decoder is never drained). The output is appsrc ! kmssink.
 * Build: gcc -O3 -mcpu=cortex-a9 -mfpu=neon -mfloat-abi=hard -o eo1-compose eo1-compose.c \
 *        $(pkg-config --cflags --libs gstreamer-1.0 gstreamer-app-1.0 gstreamer-video-1.0)
 */
#include <gst/app/gstappsink.h>
#include <gst/app/gstappsrc.h>
#include <gst/gst.h>
#include <gst/video/video.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define W 1920
#define H 1080
#define YSZ (W * H)
#define FSZ (YSZ * 3 / 2)

typedef struct {
	int video;
	guint8 *frame;              /* packed NV12, the source's current frame */
	int have;
	/* video only */
	char path[1024];
	GPtrArray *aus;
	GstCaps *caps;
	GstClockTime period;
	GstElement *pipe;
	GstAppSrc *in;
	GstAppSink *sink;
	GThread *loader, *feeder;
	GstSample *sample;          /* the newest decoded frame, as the decoder gave it (dmabuf) */
	volatile int stop, ready, failed;
	char err[256];
} Src;

static GstElement *outpipe;
static GstAppSrc *out;
static GAsyncQueue *cmds;

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

/* ------------------------------------------------------------------ pictures */
static Src *image_src(const char *path)
{
	FILE *f = fopen(path, "rb");
	if (!f)
		return NULL;
	guint8 *bgrx = malloc((size_t)W * H * 4);
	size_t n = fread(bgrx, 1, (size_t)W * H * 4, f);
	fclose(f);
	if (n != (size_t)W * H * 4) {
		free(bgrx);
		return NULL;
	}
	Src *s = calloc(1, sizeof *s);
	s->frame = malloc(FSZ);
	guint8 *Y = s->frame, *UV = s->frame + YSZ;
	for (int y = 0; y < H; y++) {
		const guint8 *p = bgrx + (size_t)y * W * 4;
		for (int x = 0; x < W; x++, p += 4)
			Y[y * W + x] = (guint8)(((66 * p[2] + 129 * p[1] + 25 * p[0] + 128) >> 8) + 16);
	}
	for (int y = 0; y < H; y += 2) {
		for (int x = 0; x < W; x += 2) {
			const guint8 *a = bgrx + ((size_t)y * W + x) * 4, *b = a + (size_t)W * 4;
			int bl = a[0] + a[4] + b[0] + b[4], g = a[1] + a[5] + b[1] + b[5], r = a[2] + a[6] + b[2] + b[6];
			bl >>= 2; g >>= 2; r >>= 2;
			UV[(y / 2) * W + x] = (guint8)(((-38 * r - 74 * g + 112 * bl + 128) >> 8) + 128);
			UV[(y / 2) * W + x + 1] = (guint8)(((112 * r - 94 * g - 18 * bl + 128) >> 8) + 128);
		}
	}
	free(bgrx);
	s->have = 1;
	return s;
}

/* ------------------------------------------------------------------ videos */
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

/* The VPU's frames are 1088 lines high (padding below 1080). Unless the receiver says it understands
 * GstVideoMeta (strides/offsets), v4l2h264dec copies every frame into a tight 1080-line buffer with the CPU:
 * about 50 % CPU into an appsink against 5 % into kmssink. So the appsink answers the allocation query with
 * VideoMeta support, and the decoder's own (dmabuf) buffers come through untouched. */
static GstPadProbeReturn alloc_probe(GstPad *pad, GstPadProbeInfo *info, gpointer data)
{
	(void)pad; (void)data;
	GstQuery *q = GST_PAD_PROBE_INFO_QUERY(info);
	if (GST_QUERY_TYPE(q) == GST_QUERY_ALLOCATION)
		gst_query_add_allocation_meta(q, GST_VIDEO_META_API_TYPE, NULL);
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
		snprintf(s->err, sizeof s->err, "%s", e ? e->message : "demux");
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
		snprintf(s->err, sizeof s->err, "no H.264 video in %s", s->path);
		s->failed = 1;
		return NULL;
	}
	s->period = end - first;
	s->pipe = gst_parse_launch("appsrc name=in format=time block=true max-bytes=1500000 ! "
				   "v4l2h264dec capture-io-mode=dmabuf ! "
				   "video/x-raw,format=NV12 ! appsink name=sink sync=true max-buffers=2", &e);
	if (!s->pipe) {
		snprintf(s->err, sizeof s->err, "%s", e ? e->message : "decoder");
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
		snprintf(s->err, sizeof s->err, "decoder does not start");
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
	s->frame = malloc(FSZ);
	s->loader = g_thread_new("loader", loader, s);
	return s;
}

/* errors from a running decoder pipeline */
static void check_bus(Src *s)
{
	if (!s->video || !s->pipe || s->failed)
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

/* take the next decoded frame (waits up to timeout_ms; the appsink is synced, so this paces at the video's
 * frame rate) and keep it as s->sample. copy=1 also unpacks it into s->frame for blending - only during
 * fades: reading the decoder's buffers with the CPU is slow (about 90 % CPU when every frame went that way).
 * Returns 1 when there is a new frame. */
static int pull(Src *s, int timeout_ms, int copy)
{
	if (!s->video || !s->ready || s->failed)
		return 0;
	GstSample *sm = gst_app_sink_try_pull_sample(s->sink, (GstClockTime)timeout_ms * GST_MSECOND);
	if (!sm)
		return 0;
	if (s->sample)
		gst_sample_unref(s->sample);
	s->sample = gst_sample_ref(sm);
	if (!copy) {
		gst_sample_unref(sm);
		s->have = 1;
		return 1;
	}
	GstVideoInfo vi;
	GstVideoFrame vf;
	int ok = 0;
	if (gst_video_info_from_caps(&vi, gst_sample_get_caps(sm)) &&
	    gst_video_frame_map(&vf, &vi, gst_sample_get_buffer(sm), GST_MAP_READ)) {
		int w = MIN(W, GST_VIDEO_FRAME_WIDTH(&vf)), h = MIN(H, GST_VIDEO_FRAME_HEIGHT(&vf));
		const guint8 *py = GST_VIDEO_FRAME_PLANE_DATA(&vf, 0), *puv = GST_VIDEO_FRAME_PLANE_DATA(&vf, 1);
		int sy = GST_VIDEO_FRAME_PLANE_STRIDE(&vf, 0), suv = GST_VIDEO_FRAME_PLANE_STRIDE(&vf, 1);
		for (int y = 0; y < h; y++)
			memcpy(s->frame + (size_t)y * W, py + (size_t)y * sy, w);
		for (int y = 0; y < h / 2; y++)
			memcpy(s->frame + YSZ + (size_t)y * W, puv + (size_t)y * suv, w);
		gst_video_frame_unmap(&vf);
		s->have = ok = 1;
	}
	gst_sample_unref(sm);
	return ok;
}

static void destroy(Src *s)
{
	if (!s)
		return;
	if (s->video) {
		if (s->loader)
			g_thread_join(s->loader);
		if (s->sample)
			gst_sample_unref(s->sample);
		s->stop = 1;
		if (s->pipe)
			gst_element_set_state(s->pipe, GST_STATE_NULL);   /* unblocks a push in the feeder */
		if (s->feeder)
			g_thread_join(s->feeder);
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
	}
	free(s->frame);
	free(s);
}

/* ------------------------------------------------------------------ output */
static void push(const guint8 *f)
{
	GstBuffer *b = gst_buffer_new_allocate(NULL, FSZ, NULL);
	gst_buffer_fill(b, 0, f, FSZ);
	gst_app_src_push_buffer(out, b);
}

/* a source alone on screen: a video's decoded buffer goes to kmssink as it is (dmabuf, no CPU copy) */
static void show_src(Src *s)
{
	if (s->video && s->sample)
		gst_app_src_push_buffer(out, gst_buffer_ref(gst_sample_get_buffer(s->sample)));
	else
		push(s->frame);
}

static void blend(guint8 *restrict o, const guint8 *restrict a, const guint8 *restrict b, unsigned k)
{
	unsigned ik = 256 - k;
	for (size_t i = 0; i < FSZ; i++)
		o[i] = (guint8)((a[i] * ik + b[i] * k) >> 8);
}

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
	GError *e = NULL;
	outpipe = gst_parse_launch("appsrc name=out is-live=true do-timestamp=true format=time "
				   "caps=video/x-raw,format=NV12,width=1920,height=1080,framerate=0/1 ! "
				   "kmssink driver-name=imx-drm force-modesetting=true sync=false", &e);
	if (!outpipe) {
		fprintf(stderr, "output: %s\n", e ? e->message : "?");
		return 1;
	}
	out = GST_APP_SRC(gst_bin_get_by_name(GST_BIN(outpipe), "out"));
	gst_element_set_state(outpipe, GST_STATE_PLAYING);
	cmds = g_async_queue_new_full(g_free);
	g_thread_new("stdin", reader, NULL);
	reply("ready");

	Src *cur = NULL, *nxt = NULL;
	double t0 = 0, fstart = 0;
	int fade_ms = 0, done = 0;
	guint8 *mix = malloc(FSZ);
	while (!done) {
		int busy = nxt || (cur && cur->video);
		char *c = busy ? g_async_queue_try_pop(cmds) : g_async_queue_timeout_pop(cmds, 250000);
		if (c) {
			char path[1500];
			int ms = 0;
			if (!strncmp(c, "quit", 4)) {
				done = 1;
			} else if (sscanf(c, "image %1499s %d", path, &ms) >= 1 || sscanf(c, "video %1499s %d", path, &ms) >= 1) {
				Src *s = c[0] == 'i' ? image_src(path) : video_src(path);
				if (!s) {
					reply("error cannot read %s", path);
				} else {
					if (nxt) {                    /* a fade still running: land it at once */
						destroy(cur);
						cur = nxt;
						reply("ok 0");
					}
					nxt = s;
					fade_ms = cur ? ms : 0;
					fstart = 0;
					t0 = now();
				}
			}
			g_free(c);
			continue;
		}
		if (cur)
			check_bus(cur);
		if (nxt) {
			check_bus(nxt);
			if (nxt->failed) {
				reply("error %s", nxt->err);
				destroy(nxt);
				nxt = NULL;
				continue;
			}
			/* the newest frame of the incoming video, without blocking */
			while (pull(nxt, nxt->have || (cur && cur->video) ? 0 : 40, 1))
				if (cur && cur->video)
					break;
			if (!nxt->have) {                     /* still loading: keep the current source going */
				if (cur && cur->video && pull(cur, 50, 0))
					show_src(cur);
				else
					g_usleep(15000);
				if (now() - t0 > 20) {
					reply("error %s gave no frame in 20 s", nxt->video ? "video" : "image");
					destroy(nxt);
					nxt = NULL;
				}
				continue;
			}
			if (!fstart)
				fstart = now();
			if (cur && cur->video)
				pull(cur, 50, 1);                 /* paces the fade at the outgoing video's frame rate */
			else if (!nxt->video)
				g_usleep(33000);
			double k = fade_ms > 0 ? (now() - fstart) * 1000.0 / fade_ms : 1.0;
			if (!cur || k >= 1.0) {
				destroy(cur);
				cur = nxt;
				nxt = NULL;
				show_src(cur);
				reply("ok %d", (int)((now() - t0) * 1000));
				continue;
			}
			/* smoothstep: eases in and out like the browser's cross-fade */
			k = k * k * (3 - 2 * k);
			blend(mix, cur->frame, nxt->frame, (unsigned)(k * 256));
			push(mix);
			continue;
		}
		if (cur && cur->video) {
			if (cur->failed) {
				fprintf(stderr, "video failed: %s\n", cur->err);
				g_usleep(100000);
				continue;
			}
			if (pull(cur, 100, 0))
				show_src(cur);
			else
				g_usleep(5000);            /* never spin if the decoder has nothing */
		}
	}
	destroy(nxt);
	destroy(cur);
	gst_element_set_state(outpipe, GST_STATE_NULL);
	return 0;
}
