#!/bin/bash
# The GStreamer elements the native player needs that the image's root filesystem does not have:
# qtdemux (isomp4), v4l2h264dec (video4linux2), h264parse (videoparsersbad) and kmssink (kms, the classic
# player), plus the libraries they link that are missing (gstcodecparsers, libv4l2/libv4lconvert).
# Taken from Debian bookworm's own armhf packages (apt-get download on an EO1, which is armhf bookworm) into
# out/gst/ at their normal paths; patch-files.sh adds them to the runtime patch, so a new image and an update
# both carry them (no rootfs rebuild, nothing installed with apt on the EO1).
#   EO1_SSH=<ssh wrapper> bash debian/build/gst-plugins.sh
set -euo pipefail
DEB="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$DEB/out/gst"
SSH="${EO1_SSH:-$DEB/out/eo1ssh}"
rm -rf "$OUT"; mkdir -p "$OUT"
"$SSH" 'set -e; rm -rf /tmp/gstp; mkdir -p /tmp/gstp; cd /tmp/gstp
	apt-get download -qq gstreamer1.0-plugins-good gstreamer1.0-plugins-bad libgstreamer-plugins-bad1.0-0 libv4l-0 libv4lconvert0 >/dev/null
	ls *.deb > SOURCES; for d in *.deb; do dpkg-deb -x "$d" x; done
	L=usr/lib/arm-linux-gnueabihf; mkdir -p o/$L/gstreamer-1.0; mv SOURCES o/
	for p in isomp4 video4linux2 videoparsersbad kms; do cp x/$L/gstreamer-1.0/libgst$p.so o/$L/gstreamer-1.0/; done
	cp -L x/$L/libgstcodecparsers-1.0.so.0 x/$L/libv4l2.so.0 x/$L/libv4lconvert.so.0 o/$L/   # the sonames only (Windows has no symlinks)
	tar -cf - -C o .; cd /; rm -rf /tmp/gstp' | tar -xf - -C "$OUT"
cd "$OUT" && find usr -type f -o -type l | sort && cat SOURCES
