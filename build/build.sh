#!/bin/bash
# Build the EO1 Debian USB stick contents on a Windows/Linux PC with Docker (no Linux machine needed).
#   bash debian/build/build.sh            -> debian/out/{6x_bootscript,zImage,uInitrd,eo1.dtb,rootfs.img}
# Then copy debian/out/* and debian/stick/* to a FAT32-formatted USB stick (see debian/README.md).
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd -W 2>/dev/null || pwd)
DUMP="$ROOT/../original/device-backup/2026-11-01/fs/system"
NAME=eo1deb
OUT="$ROOT/out"
mkdir -p "$OUT"
export MSYS_NO_PATHCONV=1

if ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
	docker rm -f "$NAME" >/dev/null 2>&1 || true
	docker run -d --name "$NAME" --platform linux/arm/v7 debian:bookworm sleep infinity >/dev/null
	docker cp "$ROOT/build/debian.sources" "$NAME:/etc/apt/sources.list.d/debian.sources"
	docker exec "$NAME" sh -c "DEBIAN_FRONTEND=noninteractive apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $(grep -vx tailscale "$ROOT/build/packages.txt" | tr '\n' ' ')"
fi
# Tailscale's own Debian repository: remote access (off until the owner turns it on on the control page)
docker cp "$ROOT/build/tailscale-archive-keyring.gpg" "$NAME:/usr/share/keyrings/tailscale-archive-keyring.gpg"
docker cp "$ROOT/build/tailscale.list" "$NAME:/etc/apt/sources.list.d/tailscale.list"
# packages added to packages.txt since the container was made (it is kept between builds)
MISSING=$(tr '\n' ' ' < "$ROOT/build/packages.txt" | docker exec -i "$NAME" sh -c 'for p in $(cat); do dpkg -s "$p" >/dev/null 2>&1 || printf "%s " "$p"; done')
if [ -n "$MISSING" ]; then
	echo "installing new packages: $MISSING"
	docker exec "$NAME" sh -c "DEBIAN_FRONTEND=noninteractive apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends $MISSING"
fi

# overlay + build inputs (SSH login key: your own public key; not kept in git). PUBLIC=1 builds the image for
# everyone: no key inside, so SSH stays off until the owner puts one in ssh/authorized_keys on the stick
if [ "${PUBLIC:-0}" = 1 ]; then
	: > "$ROOT/build/authorized_keys"
else
	ssh-keygen -y -f "$HOME/.ssh/id_ed25519" > "$ROOT/build/authorized_keys"   # public half of the key this PC logs in with
fi
docker cp "$ROOT/overlay/." "$NAME:/"
docker exec "$NAME" mkdir -p /build/fw
docker cp "$ROOT/boot/boot.cmd" "$NAME:/build/boot.cmd"
docker cp "$ROOT/boot/eo1-dts.py" "$NAME:/build/eo1-dts.py"
docker cp "$ROOT/build/finalize.sh" "$NAME:/build/finalize.sh"
docker cp "$ROOT/build/eo1-seed-credit.c" "$NAME:/build/eo1-seed-credit.c"
docker cp "$ROOT/build/authorized_keys" "$NAME:/build/authorized_keys"
# the EO1's own Android firmware (VPU, Wi-Fi calibration) only in a private build: NXP's VPU firmware is not
# redistributable; a public image takes it from each EO1's Android system at boot (initramfs eo1_vpu_firmware)
docker exec "$NAME" sh -c 'rm -f /build/fw/*.bin /lib/firmware/vpu/vpu_fw_imx6*.bin /lib/firmware/vpu_fw_imx6*.bin /lib/firmware/ti-connectivity/wl1271-nvs.eo1-android.bin'
if [ "${PUBLIC:-0}" != 1 ]; then
	docker cp "$DUMP/etc/firmware/ti-connectivity/wl1271-nvs.bin" "$NAME:/build/fw/wl1271-nvs.bin"
	for f in vpu_fw_imx6d.bin vpu_fw_imx6q.bin; do docker cp "$DUMP/lib/firmware/vpu/$f" "$NAME:/build/fw/$f"; done
fi
# the EO1 kernel (build/kernel-build.sh -> out/kernel): its modules go into the root, zImage/dtb onto the stick
if [ -f "$OUT/kernel/modules.tar.gz" ]; then
	docker exec "$NAME" mkdir -p /build/kernel
	for f in zImage eo1.dtb eo1.dts config version modules.tar.gz; do docker cp "$OUT/kernel/$f" "$NAME:/build/kernel/$f"; done
	docker exec "$NAME" sh -c 'tar -xzf /build/kernel/modules.tar.gz -C /usr && V=$(cat /build/kernel/version) && cp /build/kernel/config /boot/config-$V && depmod -a $V && echo "EO1 kernel $V added"'
fi
docker exec "$NAME" sh /build/finalize.sh

docker cp "$NAME:/out/." "$OUT/"

# root filesystem image (ext4) built from the container's filesystem, in an amd64 helper
docker export "$NAME" -o "$OUT/rootfs.tar"
docker run --rm --platform linux/amd64 -v "$OUT:/o" debian:bookworm bash -c '
	set -e
	apt-get update -qq >/dev/null && apt-get install -y -qq e2fsprogs >/dev/null
	mkdir /r && tar -C /r -xpf /o/rootfs.tar
	# docker export names entries without "./": remove container leftovers explicitly (/.dockerenv makes systemd
	# think it runs in a container and skip udev)
	rm -rf /r/.dockerenv /r/build /r/out /r/usr/local/lib/eo1/__pycache__
	echo eo1 > /r/etc/hostname
	printf "127.0.0.1\tlocalhost\n127.0.1.1\teo1\n" > /r/etc/hosts
	: > /r/etc/resolv.conf
	rm -f /o/rootfs.img
	mkfs.ext4 -q -L eo1root -O ^metadata_csum_seed -E root_owner=0:0,lazy_itable_init=0,lazy_journal_init=0 -d /r /o/rootfs.img 2600M
	e2fsck -fn /o/rootfs.img >/dev/null && echo "rootfs.img ok"
	debugfs -R "stat /.dockerenv" /o/rootfs.img 2>&1 | grep -q "not found" && echo "no /.dockerenv in image"
'
rm -f "$OUT/rootfs.tar"
ls -la "$OUT"
