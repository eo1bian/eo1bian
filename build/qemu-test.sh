#!/bin/bash
# Smoke test of the boot chain in QEMU (machine "virt", same Debian armmp kernel and initramfs): the USB stick is an
# emulated usb-storage device holding a FAT image with rootfs.img + eo1.txt, exactly like on the frame. Tests:
# initramfs script boot=eo1usb -> FAT mount -> loop root -> systemd -> eo1-config -> eo1-agent answering on :2324.
# Not covered (no such hardware in QEMU): LVDS panel, backlight, Wi-Fi chip, GPU; the kiosk is expected to fail.
#   bash debian/build/qemu-test.sh     (after build.sh)
set -euo pipefail
cd "$(dirname "$0")/.."
OUT="$(pwd -W 2>/dev/null || pwd)/out"
export MSYS_NO_PATHCONV=1
KVER=$(docker exec eo1deb sh -c 'ls /lib/modules | sort -V | tail -n1' | tr -d '\r')
docker cp "eo1deb:/boot/initrd.img-$KVER" "$OUT/initrd.raw"
rm -rf "$OUT/qemu-logs"

docker run --rm --platform linux/amd64 -v "$OUT:/o" debian:bookworm bash -c '
	set -e
	apt-get update -qq >/dev/null && apt-get install -y -qq qemu-system-arm dosfstools mtools curl >/dev/null 2>&1
	rm -f /tmp/fat.img && truncate -s 3200M /tmp/fat.img && mkfs.vfat -F 32 -n EO1TEST /tmp/fat.img >/dev/null
	mcopy -i /tmp/fat.img /o/rootfs.img ::/rootfs.img
	printf "wifi_ssid=QemuTestNet\r\nwifi_password=qemu-test-only\r\nrotate=0\r\ntoken=eo1\r\nhostname=eo1test\r\n" > /tmp/eo1.txt
	mcopy -i /tmp/fat.img /tmp/eo1.txt ::/eo1.txt
	timeout 3000 qemu-system-arm -M virt,highmem=off -cpu cortex-a15 -m 1G -smp 2 -display none \
		-kernel /o/zImage -initrd /o/initrd.raw \
		-append "console=ttyAMA0 boot=eo1usb eo1wait=1200 rw rootwait loglevel=6" \
		-device qemu-xhci -drive if=none,id=stick,file=/tmp/fat.img,format=raw -device usb-storage,drive=stick \
		-nic user,model=virtio-net-pci,hostfwd=tcp::2324-:2324 \
		-serial file:/o/qemu-serial.log &
	QPID=$!
	ok=0
	for i in $(seq 1 290); do
		sleep 10
		if curl -s -m 3 "http://127.0.0.1:2324/?cmd=version&token=eo1" > /o/qemu-agent.json 2>/dev/null && [ -s /o/qemu-agent.json ]; then
			echo "AGENT ANSWERED after $((i*10)) s"; cat /o/qemu-agent.json; echo
			curl -s -m 10 "http://127.0.0.1:2324/?cmd=deviceInfo&token=eo1"; echo
			curl -s -m 10 "http://127.0.0.1:2324/bootstatus"; echo
			curl -s -m 10 "http://127.0.0.1:2324/?cmd=version&token=wrong"; echo
			ok=1; sleep 70; break
		fi
		if grep -q -E "Kernel panic|eo1usb: no USB stick|cannot mount" /o/qemu-serial.log 2>/dev/null; then echo "BOOT FAILED"; break; fi
	done
	kill -9 $QPID 2>/dev/null || true   # hard power cut: the FAT must still be clean afterwards
	sleep 3
	mkdir -p /o/qemu-logs
	mcopy -n -s -i /tmp/fat.img ::/logs /o/qemu-logs/ 2>/dev/null || true
	mcopy -n -i /tmp/fat.img ::/eo1.txt /o/qemu-logs/eo1.txt.after 2>/dev/null || true
	echo "--- FAT check after the power cut"; fsck.vfat -n /tmp/fat.img 2>&1 | tail -n 4
	echo "--- stick state dir"; mdir -i /tmp/fat.img ::/state 2>&1 | grep -i nmconn
	echo "--- eo1.txt password line"; grep -a wifi_password /o/qemu-logs/eo1.txt.after
	echo "--- trace files"; ls /o/qemu-logs/logs | head -30
	echo "--- serial tail"; tail -n 30 /o/qemu-serial.log
	echo "RESULT ok=$ok"
'
