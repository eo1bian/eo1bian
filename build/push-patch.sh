#!/bin/bash
# Push the overlay's runtime files to the running frame over SSH as patches/10-eo1.tar.gz on the boot partition
# (unpacked into the RAM layer at every boot by the initramfs) and apply them live. Everything that is not
# initramfs, kernel, dtb or an apt package goes this way; a new rootfs.img is only needed for packages.
#   bash debian/build/push-patch.sh            (apply live + save on the boot partition)
#   bash debian/build/push-patch.sh --restart  (also restart agent, buttons and kiosk)
set -euo pipefail
cd "$(dirname "$0")/../overlay"
SSH="${EO1_SSH:-$(cd ../out && pwd)/eo1ssh}"   # EO1_SSH: another ssh wrapper
. ../build/patch-files.sh
for f in $FILES; do [ -f "$f" ] || { echo "missing: $f"; exit 1; }; done
# shellcheck disable=SC2086
tar -cf - $FILES -C ../build patch-assemble.sh | "$SSH" 'set -e
sudo rm -rf /tmp/p; mkdir -p /tmp/p; tar -xf - -C /tmp/p; cd /tmp/p
bash /tmp/p/patch-assemble.sh /tmp/10-eo1.tar.gz
sudo tar -xzf /tmp/10-eo1.tar.gz -C /
sudo systemctl daemon-reload
S=/media/eo1usb
sudo eo1-stick-write --put /tmp/10-eo1.tar.gz $S/patches/10-eo1.tar.gz
sudo md5sum /tmp/10-eo1.tar.gz $S/patches/10-eo1.tar.gz | cut -c1-12
echo "patch on the boot partition: $(sudo tar -tzf $S/patches/10-eo1.tar.gz | grep -c -v /$) files"   # root-only FAT'
if [ "${1:-}" = "--restart" ]; then
	"$SSH" 'sudo systemctl restart eo1-agent eo1-buttons eo1-kiosk; sleep 3; systemctl is-active eo1-agent eo1-buttons eo1-kiosk | tr "\n" " "; echo'
fi
