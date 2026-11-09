#!/bin/bash
# Build the EO1 kernel (Debian's linux-source-6.1 + config-6.1.0-54-armmp + build/kernel-eo1.fragment) in the
# amd64 cross-build container eo1kbuild. Output: debian/out/kernel/{zImage,eo1.dtb,eo1.dts,modules.tar.gz,
# config,version}. The dtb is Debian's imx6q-nitrogen6x.dtb from the same source, patched by boot/eo1-dts.py.
#   bash debian/build/kernel-build.sh            (eo1kbuild: any amd64 Debian container with crossbuild-essential-armhf)
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd -W 2>/dev/null || pwd)
export MSYS_NO_PATHCONV=1
C=eo1kbuild
J=$(docker exec $C nproc)
docker cp "$ROOT/build/kernel-eo1.fragment" "$C:/src/kernel-eo1.fragment"
docker cp "$ROOT/boot/eo1-dts.py" "$C:/src/eo1-dts.py"
docker cp "$ROOT/build/kernel-ipu-alpha.py" "$C:/src/kernel-ipu-alpha.py"
docker exec $C bash -c "set -e; cd /src/linux-source-6.1
export ARCH=arm CROSS_COMPILE=arm-linux-gnueabihf- KBUILD_BUILD_USER=eo1 KBUILD_BUILD_HOST=eo1
cp /config-armmp .config
python3 /src/kernel-ipu-alpha.py /src/linux-source-6.1
scripts/kconfig/merge_config.sh -m .config /src/kernel-eo1.fragment >/tmp/merge.log 2>&1 || { tail -20 /tmp/merge.log; exit 1; }
grep -E 'Value requested for|Previous value|Actual value' /tmp/merge.log | head -40 || true
make olddefconfig >/dev/null
echo 'CONFIG_LOCALVERSION=\"-eo1\"' >> .config; make olddefconfig >/dev/null
echo \"modules: \$(grep -c '=m' .config)  builtin: \$(grep -c '=y' .config)\"
grep -E '^CONFIG_(KERNEL_LZ4|DRM_IMX|DRM_ETNAVIV|WLCORE_SDIO|EXTRA_FIRMWARE|MMC_SDHCI_ESDHC_IMX|EXT4_FS|OVERLAY_FS)=' .config
time make -j$J zImage dtbs modules 2>&1 | grep -E 'error|Error|warning: unmet' | head -20 || true
ls -la arch/arm/boot/zImage
V=\$(make -s kernelrelease); echo \"kernelrelease \$V\"
rm -rf /tmp/mods && make -s modules_install INSTALL_MOD_PATH=/tmp/mods INSTALL_MOD_STRIP=1 >/dev/null
du -sh /tmp/mods/lib/modules/\$V | cut -f1
mkdir -p /out/kernel && cp arch/arm/boot/zImage /out/kernel/zImage && cp .config /out/kernel/config && echo \$V > /out/kernel/version
dtc -q -I dtb -O dts -o /tmp/n.dts arch/arm/boot/dts/imx6q-nitrogen6x.dtb && python3 /src/eo1-dts.py /tmp/n.dts /out/kernel/eo1.dts && dtc -q -I dts -O dtb -o /out/kernel/eo1.dtb /out/kernel/eo1.dts
tar -czf /out/kernel/modules.tar.gz -C /tmp/mods lib
ls -la /out/kernel"
mkdir -p "$ROOT/out/kernel"
docker cp "$C:/out/kernel/." "$ROOT/out/kernel/"
ls -la "$ROOT/out/kernel"
