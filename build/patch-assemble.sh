#!/bin/bash
# Turn a folder of overlay files (the current directory) into the runtime patch: owners, modes and the
# systemd links the units need, then pack it as $1 (10-eo1.tar.gz, unpacked into the RAM layer at every boot).
# Used by push-patch.sh on the EO1 itself and by make-update.sh (in Docker) for an update release.
set -euo pipefail
OUT="$1"
S=""; [ "$(id -u)" = 0 ] || S=sudo
rm -f patch-assemble.sh
$S chown -R 0:0 .
$S chmod 755 usr/local/bin/* usr/local/lib/eo1/*
$S find etc -type f -exec chmod 644 {} +
$S mkdir -p etc/systemd/system/multi-user.target.wants etc/systemd/system/sysinit.target.wants etc/systemd/system/timers.target.wants
for u in eo1-buttons eo1-trial eo1-boottime eo1-netwatch eo1-wifi-fast; do $S ln -sf /etc/systemd/system/$u.service etc/systemd/system/multi-user.target.wants/$u.service; done
$S ln -sf /etc/systemd/system/eo1-preload.service etc/systemd/system/sysinit.target.wants/eo1-preload.service
$S ln -sf /etc/systemd/system/eo1-player.service etc/systemd/system/sysinit.target.wants/eo1-player.service
$S ln -sf /etc/systemd/system/eo1-resolv.path etc/systemd/system/multi-user.target.wants/eo1-resolv.path
$S ln -sf /lib/systemd/system/systemd-networkd.service etc/systemd/system/multi-user.target.wants/systemd-networkd.service
$S ln -sf /lib/systemd/system/wpa_supplicant@.service etc/systemd/system/multi-user.target.wants/wpa_supplicant@wlan0.service
$S rm -f etc/systemd/system/multi-user.target.wants/eo1-seed.service
# systemd-backlight restored the old 8-step level 7 = 12/255 after the 256-level dtb: the agent owns the backlight
$S ln -sf /dev/null etc/systemd/system/systemd-backlight@.service
$S ln -sf /etc/systemd/system/eo1-seed.timer etc/systemd/system/timers.target.wants/eo1-seed.timer
$S ln -sf /etc/systemd/system/eo1-bootlog.timer etc/systemd/system/timers.target.wants/eo1-bootlog.timer
$S ln -sf /etc/systemd/system/eo1-gallery-sync.timer etc/systemd/system/timers.target.wants/eo1-gallery-sync.timer
$S ln -sf /etc/systemd/system/media-eo1gallery.mount etc/systemd/system/multi-user.target.wants/media-eo1gallery.mount
$S tar -czf "$OUT" --numeric-owner -C . .
