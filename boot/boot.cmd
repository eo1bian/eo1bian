# EO1 Debian – U-Boot script (compiled to 6x_bootscript with mkimage -T script). One script for both media:
#  * USB stick: U-Boot (bootdevs=usb mmc) sources /6x_bootscript from the stick's FAT partition 1; kernel,
#    dtb, initramfs and rootfs.img all live there.
#  * eMMC install: the same script sits on the eMMC boot partition (mmcblk0p1, FAT, next to Android's files,
#    whose own script is kept as /6x_bootscript.android). Debian's root is the ext4 partition 4 (label eo1emmc,
#    was Android's /data; the eMMC is mmcblk3 in Debian) and the
#    boot files are in /boot there. A file /android on p1 boots Android instead; any failure to load Debian
#    also falls back to Android, so the frame always boots something. The USB stick always wins (USB first).
# ${dtype} and ${disk} come from bootcmd. Nothing here writes anywhere (no saveenv).
#
# Memory map (RAM at 0x10000000): the kernel decompresses to 0x10008000 and is ~20.6 MB, so everything is
# placed above it: zImage 0x12000000 | dtb 0x13000000 | initramfs 0x13100000 (~30 MB) | splash 0x18000000.
echo "EO1 Debian: ${dtype} ${disk}"
setenv eo1_kernel 0x12000000
setenv eo1_fdt 0x13000000
setenv eo1_initrd 0x13100000
setenv eo1_part "${disk}:1"
setenv eo1_dir ""
setenv eo1_rootarg ""
if itest.s "x${dtype}" == "xmmc"; then
	if load mmc ${disk}:1 0x18000000 /android; then
		echo "EO1: /android flag found, booting Android"
		load mmc ${disk}:1 0x10008000 /6x_bootscript.android && source 0x10008000
	fi
	setenv eo1_part "${disk}:4"
	setenv eo1_dir "/boot"
	setenv eo1_rootarg "eo1root=/dev/disk/by-label/eo1emmc eo1dev=/dev/disk/by-label/BOOT"
fi

# Splash: the last picture the frame showed (state/splash.bmp, 8-bit 1920x1080, written by eo1-agent) goes on
# the panel right now, ~2 s after power-on. Harmless if missing.
# (U-Boot on the EO1 does not initialise the LVDS panel, so this shows nothing today; kept for boards whose
# U-Boot does. Lighting the backlight here only showed a blank panel.)
if load ${dtype} ${disk}:1 0x18000000 /state/splash.bmp; then bmp display 0x18000000; fi

# quiet: nothing is drawn over the splash (no console on the panel, no cursor); clk_ignore_unused keeps the
# LVDS clocks that U-Boot switched on, so the U-Boot splash stays on the panel until imx-drm takes over.
# A file /debug.txt on partition 1 puts the kernel console back on the panel, verbose.
setenv bootargs "console=ttymxc1,115200 consoleblank=0 boot=eo1usb ${eo1_rootarg} rw rootwait quiet loglevel=3 clk_ignore_unused vt.global_cursor_default=0 usb-storage.delay_use=0 cpufreq.default_governor=performance deferred_probe_timeout=30 cma=384M fbcon=rotate:1 net.ifnames=0 video=DPI-1:d"
if load ${dtype} ${disk}:1 0x18000000 /debug.txt; then setenv bootargs "${bootargs} console=tty0 loglevel=7 ignore_loglevel"; fi
# maintenance: a file /rw.txt on partition 1 boots the root filesystem writable (no RAM overlay) for one-off
# package installs over SSH; delete it again afterwards (eo1-maint off)
if load ${dtype} ${disk}:1 0x18000000 /rw.txt; then setenv bootargs "${bootargs} eo1rw"; fi

if load ${dtype} ${eo1_part} ${eo1_kernel} ${eo1_dir}/zImage; then
	if load ${dtype} ${eo1_part} ${eo1_fdt} ${eo1_dir}/eo1.dtb; then
		if load ${dtype} ${eo1_part} ${eo1_initrd} ${eo1_dir}/uInitrd; then
			bootz ${eo1_kernel} ${eo1_initrd} ${eo1_fdt}
		fi
	fi
fi
echo "EO1: Debian did not boot from ${dtype} ${disk}"
if itest.s "x${dtype}" == "xmmc"; then
	echo "EO1: falling back to Android"
	load mmc ${disk}:1 0x10008000 /6x_bootscript.android && source 0x10008000
fi
