#!/usr/bin/env python3
"""Patch cage 0.1.4 (render.c) so the compositor paints a background picture instead of black until the browser's
first frame: /run/splash.rgba (or $CAGE_BACKGROUND), raw 32-bit BGRA (DRM_FORMAT_ARGB8888), 1080x1920 or
1920x1080, letterboxed to the output. Written by eo1-agent as state/splash.rgba.gz, unpacked by the initramfs.
On the EO1 the panel otherwise goes black from cage's start (~16 s) until Chromium paints (~30 s).
Works on Debian 12's source (cage 0.1.4-4, wlroots 0.15 patch: renderer in server->renderer) and upstream.
The texture is created outside the render pass (wlroots asserts otherwise).
Usage: cage-splash.py <cage source dir>   (idempotent)
"""
import io
import sys

src = sys.argv[1] + "/render.c"
s = io.open(src, encoding="utf-8").read()
if "eo1_background" in s:
    print("already patched")
    sys.exit(0)

helpers = '''#include <drm_fourcc.h>
#include <stdio.h>
#include <stdlib.h>
#include <wlr/render/wlr_texture.h>
#include <wlr/types/wlr_matrix.h>

/* EO1: background picture until the first client frame (see debian/build/cage-splash.py) */
static struct wlr_texture *eo1_background = NULL;
static int eo1_background_w = 0, eo1_background_h = 0;
static bool eo1_background_tried = false;

static void
eo1_background_load(struct wlr_renderer *renderer)
{
	if (eo1_background_tried) {
		return;
	}
	eo1_background_tried = true;
	const char *path = getenv("CAGE_BACKGROUND");
	if (!path) {
		path = "/run/splash.rgba";
	}
	FILE *f = fopen(path, "rb");
	if (!f) {
		return;
	}
	fseek(f, 0, SEEK_END);
	long n = ftell(f);
	fseek(f, 0, SEEK_SET);
	int w, h;
	if (n == 1080L * 1920L * 4L) {
		w = 1080;
		h = 1920;
	} else if (n == 1920L * 1080L * 4L) {
		w = 1920;
		h = 1080;
	} else {
		fclose(f);
		wlr_log(WLR_ERROR, "EO1 background %s: unexpected size %ld", path, n);
		return;
	}
	void *buf = malloc(n);
	if (!buf || fread(buf, 1, n, f) != (size_t)n) {
		free(buf);
		fclose(f);
		return;
	}
	fclose(f);
	eo1_background = wlr_texture_from_pixels(renderer, DRM_FORMAT_ARGB8888, w * 4, w, h, buf);
	free(buf);
	eo1_background_w = w;
	eo1_background_h = h;
	wlr_log(WLR_INFO, "EO1 background %s loaded (%dx%d)", path, w, h);
}

static void
eo1_background_render(struct wlr_renderer *renderer, struct wlr_output *wlr_output, pixman_region32_t *damage)
{
	if (!eo1_background) {
		return;
	}
	int ow, oh;
	wlr_output_transformed_resolution(wlr_output, &ow, &oh);
	float sx = (float)ow / eo1_background_w, sy = (float)oh / eo1_background_h;
	float sc = sx < sy ? sx : sy;
	struct wlr_box box = {.width = (int)(eo1_background_w * sc), .height = (int)(eo1_background_h * sc)};
	box.x = (ow - box.width) / 2;
	box.y = (oh - box.height) / 2;
	float matrix[9];
	wlr_matrix_project_box(matrix, &box, WL_OUTPUT_TRANSFORM_NORMAL, 0, wlr_output->transform_matrix);
	int nrects;
	pixman_box32_t *rects = pixman_region32_rectangles(damage, &nrects);
	for (int i = 0; i < nrects; i++) {
		scissor_output(wlr_output, &rects[i]);
		wlr_render_texture_with_matrix(renderer, eo1_background, matrix, 1.0f);
	}
}
'''

marker = "\nvoid\noutput_render(struct cg_output *output, pixman_region32_t *damage)\n"
if marker not in s:
    raise SystemExit("output_render not found")

# Debian's cage (wlroots 0.15 patch) keeps the renderer in server->renderer; upstream 0.1.4 used a local
for renderer_expr in ("server->renderer", "renderer"):
    clear_loop = ("\tfor (int i = 0; i < nrects; i++) {\n"
                  "\t\tscissor_output(wlr_output, &rects[i]);\n"
                  "\t\twlr_renderer_clear(%s, color);\n"
                  "\t}\n" % renderer_expr)
    if clear_loop in s:
        s = s.replace(clear_loop, clear_loop + "\n\teo1_background_render(%s, wlr_output, damage);\n" % renderer_expr, 1)
        break
else:
    raise SystemExit("clear loop not found")

begin = "\twlr_renderer_begin(%s, wlr_output->width, wlr_output->height);\n" % renderer_expr
if begin not in s:
    raise SystemExit("renderer_begin not found")
s = s.replace(begin, "\teo1_background_load(%s);\n" % renderer_expr + begin, 1)

s = s.replace(marker, "\n" + helpers + marker, 1)
io.open(src, "w", encoding="utf-8", newline="\n").write(s)
print("render.c patched (renderer: %s)" % renderer_expr)
