/* eo1-video: seamless looping H.264 player for eo1-player (VPU decode, straight to the panel).
 *
 *   eo1-video <file.mp4>
 *
 * The clip is demuxed once into memory (filesrc ! qtdemux ! h264parse ! appsink), then fed to
 * appsrc ! v4l2h264dec ! kmssink as one endless stream: every pass pushes the same access units again with
 * their timestamps moved on by the clip length. The decoder never sees an end, so it is never drained and
 * restarted at the loop point - that drain (segment seeks, version 1) showed as a short freeze on the last
 * frame, and restarting gst-launch (version 0) released the display so the boot splash in fb0 flashed up.
 * For a seamless picture the clip itself must loop (last frame leads into the first).
 * Prints "playing" when the stream starts and "loop <n>" at every pass; exits 1 on a pipeline error.
 * SIGUSR1 = finish: play to the end of the current pass and exit 0 (the player then cross-fades from the
 * poster, which for a seamless loop is the frame that follows the last one); SIGTERM = stop at once.
 * Build: gcc -O2 -o eo1-video eo1-video.c $(pkg-config --cflags --libs gstreamer-1.0 gstreamer-app-1.0)
 */
#include <gst/app/gstappsink.h>
#include <gst/app/gstappsrc.h>
#include <gst/gst.h>
#include <fcntl.h>
#include <unistd.h>
#include <xf86drm.h>
#include <xf86drmMode.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static GstElement *play;
static GMainLoop *loop;
static GPtrArray *aus;          /* GstBuffer* access units of one pass, in decode order */
static GstClockTime period;     /* clip length: the timestamp offset per pass */
static volatile int stop_flag, finish_flag;
static int failed;

/* Wait (up to 2 s) until a CRTC is scanning out. When eo1-show lets go, the kernel restores fbdev at the last
 * close of the DRM device; if anything else had the card open at that moment, the CRTC stays off until that
 * fd closes, and a kmssink started in between finds no mode ("Could not get allowed GstCaps of device").
 * The card is opened only for each check, never as master, so this does not delay the restore itself. */
static uint32_t primary_plane;      /* the primary plane of the active CRTC (found by wait_display) */

static void find_primary(int fd, uint32_t crtc_id)
{
	drmSetClientCap(fd, DRM_CLIENT_CAP_UNIVERSAL_PLANES, 1);
	drmModePlaneRes *pr = drmModeGetPlaneResources(fd);
	for (uint32_t i = 0; pr && i < pr->count_planes && !primary_plane; i++) {
		drmModePlane *p = drmModeGetPlane(fd, pr->planes[i]);
		if (p && p->crtc_id == crtc_id) {
			drmModeObjectProperties *props = drmModeObjectGetProperties(fd, p->plane_id, DRM_MODE_OBJECT_PLANE);
			for (uint32_t k = 0; props && k < props->count_props; k++) {
				drmModePropertyRes *pp = drmModeGetProperty(fd, props->props[k]);
				if (pp && !strcmp(pp->name, "type") && props->prop_values[k] == DRM_PLANE_TYPE_PRIMARY)
					primary_plane = p->plane_id;
				drmModeFreeProperty(pp);
			}
			drmModeFreeObjectProperties(props);
		}
		drmModeFreePlane(p);
	}
	drmModeFreePlaneResources(pr);
}

static void wait_display(void)
{
	for (int i = 0; i < 40; i++) {
		int fd = open("/dev/dri/by-path/platform-display-subsystem-card", O_RDWR | O_CLOEXEC);
		if (fd < 0)
			return;
		drmDropMaster(fd);
		int ok = 0;
		drmModeRes *r = drmModeGetResources(fd);
		for (int k = 0; r && k < r->count_crtcs && !ok; k++) {
			drmModeCrtc *c = drmModeGetCrtc(fd, r->crtcs[k]);
			if (c && c->mode_valid && c->buffer_id) {
				ok = 1;
				find_primary(fd, c->crtc_id);
			}
			drmModeFreeCrtc(c);
		}
		drmModeFreeResources(r);
		close(fd);
		if (ok)
			return;
		g_usleep(50000);
	}
}

static int load(const char *path, GstCaps **caps)
{
	gchar *loc = g_strescape(path, NULL);
	gchar *desc = g_strdup_printf("filesrc location=\"%s\" ! qtdemux ! h264parse config-interval=-1 ! "
				      "video/x-h264,stream-format=byte-stream,alignment=au ! appsink name=s sync=false", loc);
	GError *err = NULL;
	GstElement *p = gst_parse_launch(desc, &err);
	g_free(desc);
	g_free(loc);
	if (!p) {
		fprintf(stderr, "ERROR: %s\n", err ? err->message : "parse");
		return -1;
	}
	GstAppSink *sink = GST_APP_SINK(gst_bin_get_by_name(GST_BIN(p), "s"));
	gst_element_set_state(p, GST_STATE_PLAYING);
	GstClockTime first = GST_CLOCK_TIME_NONE, end = 0, dur_guess = 0;
	GstSample *s;
	aus = g_ptr_array_new_with_free_func((GDestroyNotify)gst_buffer_unref);
	while ((s = gst_app_sink_pull_sample(sink))) {
		if (!*caps)
			*caps = gst_caps_ref(gst_sample_get_caps(s));
		GstBuffer *b = gst_sample_get_buffer(s);
		GstClockTime pts = GST_BUFFER_PTS(b), d = GST_BUFFER_DURATION(b);
		if (GST_CLOCK_TIME_IS_VALID(pts)) {
			if (!GST_CLOCK_TIME_IS_VALID(first) || pts < first)
				first = pts;
			if (GST_CLOCK_TIME_IS_VALID(d)) {
				dur_guess = d;
				if (pts + d > end)
					end = pts + d;
			} else if (pts > end) {
				end = pts;
			}
		}
		g_ptr_array_add(aus, gst_buffer_ref(b));
		gst_sample_unref(s);
	}
	gst_element_set_state(p, GST_STATE_NULL);
	gst_object_unref(sink);
	gst_object_unref(p);
	if (!aus->len || !GST_CLOCK_TIME_IS_VALID(first)) {
		fprintf(stderr, "ERROR: no video in %s\n", path);
		return -1;
	}
	period = end - first;
	if (end == first + 0 || period == 0)
		period = dur_guess * aus->len;
	return 0;
}

static gpointer feeder(gpointer data)
{
	GstAppSrc *src = GST_APP_SRC(data);
	for (guint64 n = 0; !stop_flag; n++) {
		GstClockTime off = n * period;
		for (guint i = 0; i < aus->len && !stop_flag; i++) {
			GstBuffer *b = gst_buffer_copy(g_ptr_array_index(aus, i));   /* shares the memory, own metadata */
			if (GST_BUFFER_PTS_IS_VALID(b))
				GST_BUFFER_PTS(b) += off;
			if (GST_BUFFER_DTS_IS_VALID(b))
				GST_BUFFER_DTS(b) += off;
			if (i == 0 && n > 0)
				GST_BUFFER_FLAG_UNSET(b, GST_BUFFER_FLAG_DISCONT);
			if (gst_app_src_push_buffer(src, b) != GST_FLOW_OK) {
				stop_flag = 1;
				break;
			}
		}
		printf("loop %llu\n", (unsigned long long)n + 1);
		if (finish_flag && !stop_flag) {      /* SIGUSR1: end after this pass; its last frame stays up */
			gst_app_src_end_of_stream(src);
			break;
		}
	}
	return NULL;
}

static void on_finish(int sig)
{
	(void)sig;
	finish_flag = 1;
}

static gboolean on_bus(GstBus *bus, GstMessage *m, gpointer data)
{
	(void)bus; (void)data;
	if (GST_MESSAGE_TYPE(m) == GST_MESSAGE_ERROR) {
		GError *e = NULL;
		gchar *dbg = NULL;
		gst_message_parse_error(m, &e, &dbg);
		fprintf(stderr, "ERROR: %s\n", e ? e->message : "?");
		g_clear_error(&e);
		g_free(dbg);
		failed = 1;
		g_main_loop_quit(loop);
	} else if (GST_MESSAGE_TYPE(m) == GST_MESSAGE_EOS) {
		g_main_loop_quit(loop);
	}
	return TRUE;
}

static void on_term(int sig)
{
	(void)sig;
	stop_flag = 1;
	g_main_loop_quit(loop);
}

int main(int argc, char **argv)
{
	setvbuf(stdout, NULL, _IOLBF, 0);
	gst_init(&argc, &argv);
	/* --wait: load the clip, print "loaded", start only on a line on stdin - the player arms the next video
	 * while eo1-show cross-fades to its poster and says go the moment eo1-show has let go of the display */
	/* --modeset: set the display mode ourselves (kmssink force-modesetting). Not the default: the mode is set
	 * with an empty buffer first, and an empty YUV frame shows as a ~100 ms green flash. Without it the video
	 * goes onto the mode eo1-show/fbdev left (no flash); the player retries with --modeset if that failed. */
	int wait = 0, modeset = 0;
	const char *file = NULL;
	for (int i = 1; i < argc; i++) {
		if (!strcmp(argv[i], "--wait"))
			wait = 1;
		else if (!strcmp(argv[i], "--modeset"))
			modeset = 1;
		else
			file = argv[i];
	}
	if (!file) {
		fprintf(stderr, "usage: %s [--wait] [--modeset] file.mp4\n", argv[0]);
		return 2;
	}
	GstCaps *caps = NULL;
	if (load(file, &caps))
		return 1;
	GError *err = NULL;
	/* a small queue (~1-2 s of video): SIGUSR1 then ends the pass that is actually on screen */
	play = gst_parse_launch(modeset ? "appsrc name=src format=time block=true max-bytes=1500000 ! v4l2h264dec ! "
					  "kmssink name=sink driver-name=imx-drm force-modesetting=true"
					: "appsrc name=src format=time block=true max-bytes=1500000 ! v4l2h264dec ! "
					  "kmssink name=sink driver-name=imx-drm", &err);
	if (!play) {
		fprintf(stderr, "ERROR: %s\n", err ? err->message : "parse");
		return 1;
	}
	GstElement *src = gst_bin_get_by_name(GST_BIN(play), "src");
	gst_app_src_set_caps(GST_APP_SRC(src), caps);
	loop = g_main_loop_new(NULL, FALSE);
	GstBus *bus = gst_element_get_bus(play);
	gst_bus_add_watch(bus, on_bus, NULL);
	signal(SIGTERM, on_term);
	signal(SIGINT, on_term);
	signal(SIGUSR1, on_finish);
	if (wait) {
		char go[64];
		printf("loaded\n");
		if (!fgets(go, sizeof go, stdin))
			go[0] = 0;
	}
	if (!modeset) {
		wait_display();
		/* EO1_VIDEO_PRIMARY=1: the video replaces fb0 on the primary plane instead of kmssink's default overlay.
		 * Tried 3 Nov 04:51 against the black line the overlay draws when it is switched on: worse - the format
		 * changes on the primary plane (XR24/RG16 <-> YUYV) drew black lines both before and after the switch. */
		GstElement *sink = gst_bin_get_by_name(GST_BIN(play), "sink");
		if (sink && primary_plane && getenv("EO1_VIDEO_PRIMARY"))
			g_object_set(sink, "plane-id", (gint)primary_plane, NULL);
		if (sink)
			gst_object_unref(sink);
	}
	/* the display can be briefly unavailable right after eo1-show let go: retry for about a second */
	int tries = 0;
	while (gst_element_set_state(play, GST_STATE_PLAYING) == GST_STATE_CHANGE_FAILURE) {
		gst_element_set_state(play, GST_STATE_NULL);
		if (++tries >= 6) {
			fprintf(stderr, "ERROR: cannot start the pipeline\n");
			return 1;
		}
		g_usleep(200000);
	}
	printf("playing %u access units, %.3f s per pass\n", aus->len, period / 1e9);
	GThread *t = g_thread_new("feeder", feeder, src);
	g_main_loop_run(loop);
	stop_flag = 1;
	gst_element_set_state(play, GST_STATE_NULL);   /* unblocks a push waiting in the feeder */
	g_thread_join(t);
	gst_object_unref(src);
	gst_object_unref(bus);
	gst_object_unref(play);
	gst_caps_unref(caps);
	return failed;
}
