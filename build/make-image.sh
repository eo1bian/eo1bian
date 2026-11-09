#!/bin/bash
# Make the image people write to a USB stick (balenaEtcher, Rufus): one FAT32 partition labelled EO1BIAN with
# everything the EO1 needs to start from it, plus eo1.txt and README.txt for the owner.
#   bash debian/build/build.sh                       (PUBLIC=1 for a release: no SSH key inside)
#   bash debian/build/make-update.sh v1.0.0          (the runtime patch, also used for the image)
#   bash debian/build/make-image.sh v1.0.0           -> out/release/eo1bian-v1.0.0.img.xz and .zip (+ .sha256)
# Needs Docker. Nothing here touches a real disk.
set -euo pipefail
VERSION="${1:?version, e.g. v1.0.0}"
DEB="$(cd "$(dirname "$0")/.." && { pwd -W 2>/dev/null || pwd; })"
OUT="$DEB/out"
python -I "$(dirname "$0")/make-splash.py"
for f in 6x_bootscript zImage uInitrd eo1.dtb rootfs.img release/manifest.json release/patches/10-eo1.tar.gz state-default/splash.fb.gz state-default/last.jpg; do
	[ -f "$OUT/$f" ] || { echo "missing: out/$f"; exit 1; }
done
grep -q "\"version\": \"$VERSION\"" "$OUT/release/manifest.json" ||
	{ echo "out/release is for another version: run make-update.sh $VERSION first"; exit 1; }
export MSYS_NO_PATHCONV=1
docker run --rm -v "$OUT:/o" -v "$DEB/stick:/stick:ro" debian:bookworm bash -c "
	set -e
	apt-get update -qq >/dev/null && apt-get install -y -qq dosfstools mtools fdisk xz-utils zip e2fsprogs >/dev/null 2>&1
	IMG=/tmp/eo1bian.img
	# a release image must come from a PUBLIC build: no SSH key baked in, no NXP VPU firmware (not redistributable)
	if debugfs -R 'cat /home/eo1/.ssh/authorized_keys' /o/rootfs.img 2>/dev/null | grep -q .; then
		echo 'rootfs.img holds an SSH key: rebuild it with  PUBLIC=1 bash debian/build/build.sh'; exit 1; fi
	if debugfs -R 'ls /lib/firmware/vpu' /o/rootfs.img 2>/dev/null | grep -q vpu_fw; then
		echo 'rootfs.img holds the VPU firmware: rebuild it with  PUBLIC=1 bash debian/build/build.sh'; exit 1; fi
	MB=\$(( \$(du -cm /o/6x_bootscript /o/zImage /o/uInitrd /o/eo1.dtb /o/rootfs.img /o/release/patches/10-eo1.tar.gz | tail -1 | cut -f1) + 160 ))
	truncate -s \${MB}M \$IMG
	echo 'label: dos
start=2048, type=c, bootable' | sfdisk -q \$IMG
	# the FAT32 file system straight into the partition (sector 2048 = 1 MiB), filled with mtools: no loop device
	mkfs.vfat -F 32 -n EO1BIAN --offset 2048 \$IMG \$(( (MB - 1) * 1024 )) >/dev/null
	M=\"-i \$IMG@@1M\"
	mcopy \$M /o/6x_bootscript /o/zImage /o/uInitrd /o/eo1.dtb /o/rootfs.img ::/
	mmd \$M ::/patches ::/logs ::/state ::/ssh
	mcopy \$M /o/release/patches/10-eo1.tar.gz ::/patches/
	mcopy \$M /stick/eo1.txt /stick/README.txt ::/
	# the first-start splash (build/make-splash.py): shown within seconds instead of a black screen
	mcopy \$M /o/state-default/splash.fb.gz /o/state-default/last.jpg ::/state/
	printf 'Put your SSH public key in a file named authorized_keys in this folder to turn SSH on.\r\n' > /tmp/r.txt
	mcopy \$M /tmp/r.txt ::/ssh/README.txt
	echo '$VERSION' > /tmp/v.txt && mcopy \$M /tmp/v.txt ::/EO1BIAN-VERSION.txt
	mdir \$M ::/ | tail -15; minfo \$M :: | grep -iE 'volume label|sectors per cluster' | head -2
	xz -T0 -6 -c \$IMG > '/o/release/eo1bian-$VERSION.img.xz'
	# the same files as a ZIP: unpack it onto any FAT32 stick, no Rufus or Etcher needed
	mkdir /tmp/tree && mcopy -s \$M ::/* /tmp/tree/
	(cd /tmp/tree && zip -q -r -6 '/o/release/eo1bian-$VERSION.zip' .)
	cd /o/release && sha256sum 'eo1bian-$VERSION.img.xz' > 'eo1bian-$VERSION.img.xz.sha256'
	sha256sum 'eo1bian-$VERSION.zip' > 'eo1bian-$VERSION.zip.sha256' && ls -la eo1bian-*
"
