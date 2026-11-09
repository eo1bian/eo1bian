#!/bin/sh
# Bench GStreamer video paths on the EO1 (run on the frame as root, kiosk stopped):
#   sh gst-bench.sh /tmp/vl.mp4      (landscape 1920x1080 H.264, the server's .hd.eo1.mp4 variant)
# Prints CPU seconds for ~14 s of playback per pipeline and the last lines of output.
F="${1:-/tmp/vl.mp4}"
CONN=$(for c in /sys/class/drm/card*-LVDS-1; do [ -e "$c" ] && cat "$c/../../dev" 2>/dev/null; done | head -n 1)
run() {
	label="$1"; shift
	timeout 16 gst-launch-1.0 -q "$@" >/tmp/gst.log 2>&1 &
	sleep 2
	pid=$(pgrep -n -x gst-launch-1.0)
	sleep 12
	cpu="?"
	[ -n "$pid" ] && cpu=$(awk '{printf "%.1f", ($14+$15)/100}' /proc/$pid/stat 2>/dev/null)
	wait
	echo "$label: cpu_s_in_12s=$cpu"
	grep -v -E "^$|Setting pipeline|Pipeline is|New clock|Redistribute" /tmp/gst.log | tail -n 4 | cut -c1-150 | sed 's/^/    /'
}
run hw-kms filesrc location="$F" ! qtdemux ! h264parse ! v4l2h264dec ! kmssink driver-name=imx-drm force-modesetting=true
run hw-convert-kms filesrc location="$F" ! qtdemux ! h264parse ! v4l2h264dec ! videoconvert ! kmssink driver-name=imx-drm force-modesetting=true
run hw-fakesink filesrc location="$F" ! qtdemux ! h264parse ! v4l2h264dec ! fakesink sync=true
