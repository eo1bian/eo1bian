#!/bin/bash
# The virtual EO1: the release image booted with the EO1's own kernel on QEMU's "sabrelite" (an i.MX6 Quad board,
# the same SoC family and drivers as the EO1). Tests the boot chain without touching a real frame:
# initramfs (stick found, patches, read-only root + RAM layer), systemd, the agent.
# Not emulated: display (IPU), video decoder (VPU), LVDS panel, backlight, Wi-Fi chip, buttons, light sensor.
#   bash debian/build/veo1.sh v1.0.0-rc1 [seconds]     -> out/veo1-serial.log
set -euo pipefail
VERSION="${1:?image version, e.g. v1.0.0-rc1}"
SECS="${2:-420}"
cd "$(dirname "$0")/.."
OUT="$(pwd -W 2>/dev/null || pwd)/out"
export MSYS_NO_PATHCONV=1
[ -f "$OUT/veo1-sabrelite.dtb" ] || docker cp eo1kbuild:/src/linux-source-6.1/arch/arm/boot/dts/imx6q-sabrelite.dtb "$OUT/veo1-sabrelite.dtb"
docker run --rm --platform linux/amd64 -v "$OUT:/o" debian:trixie bash -c "
	set -e
	apt-get update -qq >/dev/null && apt-get install -y -qq qemu-system-arm xz-utils >/dev/null 2>&1
	xz -dc /o/release/eo1bian-$VERSION.img.xz > /tmp/stick.img
	truncate -s 4G /tmp/stick.img                      # an SD card must be a power of two in QEMU
	dd if=/o/uInitrd of=/tmp/initrd.raw bs=64 skip=1 2>/dev/null   # without U-Boot's 64-byte header
	timeout $SECS qemu-system-arm -M sabrelite -smp 4 -m 1G -display none \
		-kernel /o/zImage -dtb /o/veo1-sabrelite.dtb -initrd /tmp/initrd.raw \
		-append 'console=ttymxc0,115200 boot=eo1usb eo1dev=/dev/disk/by-label/EO1BIAN eo1wait=300 rootwait loglevel=6 eo1virtual' \
		-drive if=none,id=stick,file=/tmp/stick.img,format=raw -device sd-card,drive=stick,bus=sd-bus \
		-serial file:/o/veo1-serial.log -serial null || true
"
grep -E "eo1usb:|Kernel panic|eo1-agent|login:" "$OUT/veo1-serial.log" | sed 's/\x1b\[[0-9;]*m//g'
