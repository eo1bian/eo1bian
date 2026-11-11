# The overlay files that make up the runtime patch (sourced by push-patch.sh and make-update.sh from
# debian/overlay). Build artifacts from out/ are added when they exist.
FILES="
usr/local/bin/eo1-cage usr/local/bin/eo1-browser usr/local/bin/eo1-trial usr/local/bin/eo1-config
usr/local/bin/eo1-ssh-keys usr/local/bin/eo1-install-emmc usr/local/bin/eo1-boot-write usr/local/bin/eo1-boottime
usr/local/bin/eo1-trace usr/local/bin/eo1-bootlog usr/local/bin/eo1-stick-write usr/local/bin/eo1-resolv
usr/local/bin/eo1-netwatch usr/local/bin/eo1-setup-ap usr/local/bin/eo1-update usr/local/bin/eo1-install-run etc/eo1bian-version usr/share/eo1/update-key.pem usr/share/eo1/update-repo usr/local/bin/eo1-wifi-fast etc/systemd/system/eo1-wifi-fast.service
usr/local/lib/eo1/save-wifi usr/local/lib/eo1/buttons.py usr/local/lib/eo1/agent.py usr/local/lib/eo1/wifi.py
usr/local/lib/eo1/player.py usr/local/lib/eo1/player-layers.py usr/local/lib/eo1/player-start
usr/local/lib/eo1/gallery-sync.py usr/local/lib/eo1/eo-art-rescue.py usr/local/lib/eo1/pairing.py usr/local/lib/eo1/ntfyremote.py usr/local/lib/eo1/qrcodegen.py usr/share/eo1/setup.html usr/share/eo1/pc.html etc/systemd/system/media-eo1gallery.mount etc/systemd/system/eo1-gallery-sync.service
etc/systemd/system/eo1-gallery-sync.timer
etc/systemd/system/eo1-player.service
etc/systemd/system/eo1-kiosk.service etc/systemd/system/eo1-agent.service etc/systemd/system/eo1-buttons.service
etc/systemd/system/eo1-trial.service etc/systemd/system/eo1-trace.service etc/systemd/system/eo1-preload.service
etc/systemd/system/eo1-bootlog.service etc/systemd/system/eo1-bootlog.timer etc/systemd/system/eo1-seed.service
etc/systemd/system/eo1-seed.timer etc/systemd/system/eo1-boottime.service etc/systemd/system/eo1-netwatch.service
etc/systemd/system/eo1-resolv.path etc/systemd/system/eo1-resolv.service
etc/systemd/system/avahi-daemon.service.d/eo1.conf etc/systemd/system/systemd-random-seed.service.d/eo1.conf
etc/systemd/system/systemd-timesyncd.service.d/eo1.conf etc/systemd/system/ssh.service.d/keys.conf etc/systemd/system/tailscaled.service.d/eo1.conf
etc/systemd/system/NetworkManager.service.d/eo1.conf etc/systemd/system/wpa_supplicant.service.d/eo1.conf
etc/systemd/system/wpa_supplicant@wlan0.service.d/eo1.conf etc/systemd/system/systemd-networkd.service.d/eo1.conf
etc/systemd/network/10-wlan0.network
etc/systemd/system.conf.d/eo1-watchdog.conf
etc/NetworkManager/conf.d/eo1.conf etc/modules-load.d/eo1.conf etc/udev/rules.d/80-eo1-wifi.rules
usr/share/eo1/boot.html usr/share/eo1/control.html usr/share/eo1/manifest.webmanifest usr/share/eo1/icon.svg usr/share/eo1/icon.png usr/share/eo1/eo1bian-main.png
usr/share/fonts/truetype/eo1bian/Inter-Medium.ttf usr/share/fonts/truetype/eo1bian/Jost-Medium.ttf
usr/share/fonts/truetype/eo1bian/Fraunces-Soft.ttf usr/share/fonts/truetype/eo1bian/coverage.json
usr/share/fonts/truetype/eo1bian/OFL-inter.txt usr/share/fonts/truetype/eo1bian/OFL-jost.txt usr/share/fonts/truetype/eo1bian/OFL-fraunces.txt
"
# the patched cage (debian/build/cage-splash.py) is a build artifact in out/, not in git
if [ -f ../out/cage-eo1 ]; then cp -f ../out/cage-eo1 usr/local/bin/cage-eo1; FILES="$FILES usr/local/bin/cage-eo1"; fi
# eo1-show (debian/build/eo1-show.c, DRM presenter of the native player) is a build artifact too
if [ -f ../out/eo1-show ]; then cp -f ../out/eo1-show usr/local/bin/eo1-show; FILES="$FILES usr/local/bin/eo1-show"; fi
if [ -f ../out/eo1-video ]; then cp -f ../out/eo1-video usr/local/bin/eo1-video; FILES="$FILES usr/local/bin/eo1-video"; fi
if [ -f ../out/eo1-layers ]; then cp -f ../out/eo1-layers usr/local/bin/eo1-layers; FILES="$FILES usr/local/bin/eo1-layers"; fi
if [ -f ../out/eo1-grab ]; then cp -f ../out/eo1-grab usr/local/bin/eo1-grab; FILES="$FILES usr/local/bin/eo1-grab"; fi
if [ -f ../out/eo1-compose ]; then cp -f ../out/eo1-compose usr/local/bin/eo1-compose; FILES="$FILES usr/local/bin/eo1-compose"; fi
# GStreamer elements missing from the root filesystem (qtdemux, v4l2h264dec, h264parse, kmssink): build/gst-plugins.sh
if [ -d ../out/gst/usr ]; then
	cp -a ../out/gst/usr/. usr/
	FILES="$FILES $(cd ../out/gst && find usr -type f -o -type l | sort | tr '\n' ' ')"
else
	echo "note: no out/gst (run build/gst-plugins.sh): videos will not play" >&2
fi
