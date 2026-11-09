#!/bin/bash
# Reboot the frame and measure the boot: seconds from kernel start to every stage, as logged on the frame by
# eo1-boottime (logs/boottime.txt on the boot partition) plus the screenshot it took when the frame page came up.
#   bash debian/build/measure-boot.sh            reboot + measure
#   bash debian/build/measure-boot.sh --no-reboot   just fetch the last measurement
set -euo pipefail
cd "$(dirname "$0")/../out"
SSH=./eo1ssh
if [ "${1:-}" != "--no-reboot" ]; then
	$SSH 'sudo systemctl reboot' 2>/dev/null || true
	T0=$(date +%s)
	sleep 20
	for i in $(seq 1 150); do
		r=$(curl.exe -s -m 2 "http://${EO1_HOST:-eo1.local}:2324/?cmd=version&token=eo1" 2>/dev/null || true)
		[ -n "$r" ] && { echo "agent reachable from the PC after $(( $(date +%s) - T0 )) s (incl. shutdown + U-Boot)"; break; }
		sleep 1
	done
	# no SSH logins while the boot is measured (each costs CPU on the frame): wait for the browser page via the
	# agent (cheap HTTP), then poll (SSH) until the measurement file carries this boot's id
	for i in $(seq 1 100); do r=$(curl.exe -s -m 2 "http://${EO1_HOST:-eo1.local}:2324/?cmd=deviceInfo&token=eo1" 2>/dev/null || true); echo "$r" | grep -q '"pageHello": true' && break; sleep 2; done
	sleep 20
	for i in $(seq 1 30); do
		$SSH 'b=$(cat /proc/sys/kernel/random/boot_id); grep -q "id $b" /media/eo1usb/logs/boottime.txt 2>/dev/null' >/dev/null 2>&1 && break
		sleep 5
	done
fi
$SSH 'cat /media/eo1usb/logs/boottime.txt; cat /media/eo1usb/logs/boot-shot.png 2>/dev/null | base64 -w0 > /tmp/shot.b64; echo; echo SHOT:; cat /tmp/shot.b64' > boottime-raw.txt
$SSH 'cat /media/eo1usb/logs/boot-shot-early.png 2>/dev/null | base64 -w0' | base64 -d > boot-shot-early.png 2>/dev/null && [ -s boot-shot-early.png ] && echo "early screenshot (cage up, before the browser): debian/out/boot-shot-early.png"
sed -n '1,/^SHOT:/p' boottime-raw.txt | grep -v "^SHOT:"
sed -n '/^SHOT:/,$p' boottime-raw.txt | tail -n 1 | base64 -d > boot-shot.png 2>/dev/null && echo "screenshot: debian/out/boot-shot.png ($(stat -c %s boot-shot.png) bytes)"
