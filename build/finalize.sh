#!/bin/sh
# Runs INSIDE the armhf build container after the packages are installed and the overlay is copied to /.
# Produces /out/{zImage,uInitrd,eo1.dtb,6x_bootscript} for the FAT stick; the root filesystem is exported later.
set -eu
export DEBIAN_FRONTEND=noninteractive
KVER=$(ls /lib/modules | sort -V | tail -n 1)
echo "kernel: $KVER"

# --- files copied from Windows arrive with odd modes: fix them
mkdir -p /var/log/journal
for f in /etc/systemd/system/eo1-*.service /etc/systemd/system/eo1-*.timer /etc/pam.d/cage \
	/etc/NetworkManager/conf.d/eo1.conf /etc/ssh/sshd_config.d/eo1.conf /etc/systemd/journald.conf.d/eo1.conf /etc/modules-load.d/eo1.conf \
	/etc/initramfs-tools/modules /etc/initramfs-tools/conf.d/resume /etc/initramfs-tools/conf.d/eo1 /etc/systemd/system/avahi-daemon.service.d/eo1.conf /etc/systemd/system/systemd-random-seed.service.d/eo1.conf /etc/systemd/system/systemd-timesyncd.service.d/eo1.conf /etc/udev/rules.d/80-eo1-wifi.rules /etc/systemd/network/10-wlan0.network /etc/systemd/system/eo1-resolv.path /etc/systemd/system/eo1-resolv.service /etc/systemd/system/NetworkManager.service.d/eo1.conf /etc/systemd/system/wpa_supplicant.service.d/eo1.conf /etc/systemd/system/wpa_supplicant@wlan0.service.d/eo1.conf /etc/systemd/system/systemd-networkd.service.d/eo1.conf /usr/share/eo1/boot.html; do
	[ -f "$f" ] && chmod 644 "$f"
done
chmod 755 /etc/initramfs-tools/hooks/eo1 /usr/local/bin/eo1-* /usr/local/lib/eo1/agent.py /usr/local/lib/eo1/save-wifi /usr/local/lib/eo1/buttons.py /etc/initramfs-tools/scripts/eo1usb

# --- user, groups, SSH key, sudo
id eo1 >/dev/null 2>&1 || useradd -m -s /bin/bash -G video,render,input,audio,netdev eo1
passwd -l eo1 >/dev/null
mkdir -p /home/eo1/.ssh
cp /build/authorized_keys /home/eo1/.ssh/authorized_keys
chown -R eo1:eo1 /home/eo1/.ssh && chmod 700 /home/eo1/.ssh && chmod 600 /home/eo1/.ssh/authorized_keys
echo 'eo1 ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/eo1 && chmod 440 /etc/sudoers.d/eo1

# --- system basics
echo eo1 > /etc/hostname
printf '127.0.0.1\tlocalhost\n127.0.1.1\teo1\n::1\tlocalhost ip6-localhost ip6-loopback\n' > /etc/hosts
ln -sf /usr/share/zoneinfo/UTC /etc/localtime && echo UTC > /etc/timezone   # eo1-config / the phone set the real one
printf 'tmpfs /tmp tmpfs defaults,nosuid,nodev,size=128M 0 0\n' > /etc/fstab
rm -f /etc/resolv.conf 2>/dev/null || true   # docker bind-mounts it; the exported image gets an empty one
systemctl set-default graphical.target
systemctl enable eo1-config.service eo1-agent.service eo1-kiosk.service eo1-bootlog.timer eo1-trace.service eo1-buttons.service eo1-seed.timer eo1-boottime.service eo1-netwatch.service eo1-resolv.path systemd-networkd.service wpa_supplicant@wlan0.service eo1-preload.service \
	NetworkManager.service ssh.service avahi-daemon.service systemd-timesyncd.service
systemctl mask getty@tty1.service
# no background apt or e2scrub on a USB stick (wear, CPU); updates are done by rebuilding the image
systemctl mask apt-daily.timer apt-daily-upgrade.timer e2scrub_all.timer e2scrub_reap.service man-db.timer >/dev/null 2>&1 || true
# sshd host keys are generated on the device at first boot (not baked into the image)
rm -f /etc/ssh/ssh_host_*
# host keys: generated once and kept on the stick by eo1-ssh-keys (overlay: ssh.service.d/keys.conf)

# --- firmware from the frame's own Android image (board-specific Wi-Fi calibration, VPU)
mkdir -p /lib/firmware/ti-connectivity /lib/firmware/vpu
# Debian's generic wl1271-nvs.bin matches its WL127x firmware 6.x; the 2015 Android one (for firmware 2) is kept
# next to it for reference only. If Wi-Fi fails to come up, compare in logs/boot-latest.txt (wlcore lines).
if [ -f /build/fw/wl1271-nvs.bin ]; then
	cp /build/fw/wl1271-nvs.bin /lib/firmware/ti-connectivity/wl1271-nvs.eo1-android.bin
	[ -f /lib/firmware/ti-connectivity/wl1271-nvs.bin ] || cp /build/fw/wl1271-nvs.bin /lib/firmware/ti-connectivity/wl1271-nvs.bin
fi
for f in /build/fw/vpu_fw_imx6*.bin; do
	[ -f "$f" ] && cp "$f" /lib/firmware/vpu/ && cp "$f" /lib/firmware/
done

# --- RNG seed crediting tool for the initramfs (static, see build/eo1-seed-credit.c)
gcc -O2 -static -o /usr/local/sbin/eo1-seed-credit /build/eo1-seed-credit.c && strip /usr/local/sbin/eo1-seed-credit

# --- initramfs with the eo1usb boot script and the USB/FAT/loop modules
chmod +x /etc/initramfs-tools/scripts/eo1usb
update-initramfs -c -k "$KVER" 2>&1 | tail -n 3 || update-initramfs -u -k "$KVER"

# --- boot files for the stick
mkdir -p /out
if [ -f /build/kernel/zImage ]; then cp /build/kernel/zImage /out/zImage; else cp "/boot/vmlinuz-$KVER" /out/zImage; fi
mkimage -A arm -O linux -T ramdisk -C none -n "EO1 Debian initramfs" -d "/boot/initrd.img-$KVER" /out/uInitrd >/dev/null
DTB=$(find /usr/lib/linux-image-$KVER /usr/lib/linux-image-* -name imx6q-nitrogen6x.dtb 2>/dev/null | head -n 1)
dtc -q -I dtb -O dts -o /tmp/nitrogen6x.dts "$DTB"
python3 /build/eo1-dts.py /tmp/nitrogen6x.dts /tmp/eo1.dts
dtc -q -I dts -O dtb -o /out/eo1.dtb /tmp/eo1.dts
cp /tmp/eo1.dts /out/eo1.dts
if [ -f /build/kernel/eo1.dtb ]; then cp /build/kernel/eo1.dtb /out/eo1.dtb; cp /build/kernel/eo1.dts /out/eo1.dts; fi
mkimage -A arm -O linux -T script -C none -n "EO1 Debian USB" -d /build/boot.cmd /out/6x_bootscript >/dev/null

apt-get clean
rm -rf /var/lib/apt/lists/* /var/cache/apt/*.bin /root/install.log /tmp/*.dts
echo "kernel $KVER" > /out/VERSION
ls -la /out
