#!/usr/bin/env python3
"""Kernel patch for the EO1: an "alpha" property on the i.MX6 IPU display planes (drivers/gpu/drm/imx/ipuv3-plane.c).

The IPU display processor blends its two layers (background = primary plane, foreground = overlay plane) with a
global alpha in hardware, but the 6.1 driver always programs 0xff. With the standard DRM plane "alpha" property
(0..0xffff) the overlay's global alpha is settable per atomic commit, so eo1-compose can cross-fade two live
videos (or a video and a picture) without the CPU touching a pixel, and without ever switching a plane on or off
(switching the overlay on drew a short black line on the panel).

Idempotent: run on the kernel source tree before building (debian/build/kernel-build.sh does).
  python3 kernel-ipu-alpha.py /src/linux-source-6.1
"""
import sys

path = sys.argv[1].rstrip("/") + "/drivers/gpu/drm/imx/ipuv3-plane.c"
s = open(path, encoding="utf-8").read()
if "EO1: plane alpha" in s:
    print("ipuv3-plane alpha: already patched")
    sys.exit(0)

old_bg = """		if (new_state->normalized_zpos == 1) {
			ipu_dp_set_global_alpha(ipu_plane->dp,
						!fb->format->has_alpha, 0xff,
						true);"""
new_bg = """		if (new_state->normalized_zpos == 1) {
			/* EO1: plane alpha (property, 0..0xffff) instead of a fixed 0xff */
			ipu_dp_set_global_alpha(ipu_plane->dp,
						!fb->format->has_alpha, new_state->alpha >> 8,
						true);"""
old_fg = """		if (new_state->normalized_zpos == 1) {
			ipu_dp_set_global_alpha(ipu_plane->dp,
						!fb->format->has_alpha, 0xff,
						false);"""
new_fg = """		if (new_state->normalized_zpos == 1) {
			/* EO1: plane alpha (property, 0..0xffff) instead of a fixed 0xff */
			ipu_dp_set_global_alpha(ipu_plane->dp,
						!fb->format->has_alpha, new_state->alpha >> 8,
						false);"""
old_init = """	ret = drm_plane_create_color_properties(&ipu_plane->base,"""
new_init = """	/* EO1: plane alpha, used for the global alpha of the layer on top */
	if (dp == IPU_DP_FLOW_SYNC_BG || dp == IPU_DP_FLOW_SYNC_FG) {
		ret = drm_plane_create_alpha_property(&ipu_plane->base);
		if (ret)
			return ERR_PTR(ret);
	}

	ret = drm_plane_create_color_properties(&ipu_plane->base,"""
for a, b in ((old_bg, new_bg), (old_fg, new_fg), (old_init, new_init)):
    if s.count(a) != 1:
        sys.exit("ipuv3-plane alpha: anchor not found:\n" + a)
    s = s.replace(a, b)
if "#include <drm/drm_blend.h>" not in s:
    s = s.replace("#include <drm/drm_atomic.h>\n", "#include <drm/drm_atomic.h>\n#include <drm/drm_blend.h>\n", 1)
open(path, "w", encoding="utf-8").write(s)
print("ipuv3-plane alpha: patched")
