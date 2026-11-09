#!/bin/sh
# Bench mpv output/decoder variants on the EO1 (run on the frame as root, kiosk stopped):
#   sh mpv-bench.sh /tmp/v.mp4
# Prints per variant: CPU seconds used for 15 s of playback, dropped frames, whether hardware decoding was used.
F="${1:-/tmp/v.mp4}"
run() {
	label="$1"; shift
	rm -f /tmp/mpv.log
	timeout 25 mpv --no-config --really-quiet --no-audio --length=15 --log-file=/tmp/mpv.log "$@" "$F" >/dev/null 2>&1 &
	sleep 2
	pid=$(pgrep -n -x mpv)
	sleep 12
	cpu="?"
	[ -n "$pid" ] && cpu=$(awk '{printf "%.1f", ($14+$15)/100}' /proc/$pid/stat 2>/dev/null)
	wait
	hw=$(grep -c -i "Using hardware decoding" /tmp/mpv.log)
	dropped=$(grep -o -E "Dropped: [0-9]+" /tmp/mpv.log | tail -n 1)
	errs=$(grep -c -E "^\[ *[0-9.]+\]\[e\]" /tmp/mpv.log)
	echo "$label: cpu_s_in_14s=$cpu hw=$hw ${dropped:-Dropped: ?} errors=$errs"
	grep -E "\]\[e\]|Using hardware|VO: |hwdec" /tmp/mpv.log | head -n 4 | cut -c1-150 | sed 's/^/    /'
}
run drm-hw --vo=drm --hwdec=v4l2m2m-copy --video-rotate=270
run gpu-hw --vo=gpu --gpu-context=drm --hwdec=v4l2m2m-copy --video-rotate=270
run drm-sw --vo=drm --hwdec=no --video-rotate=270
