# The EO2 and booting from USB: what we know, what we tried, what failed

**Status: research notes, not a plan of work.** No EO2 code, config or build change exists in this repository —
the kernel fragment still carries `CONFIG_ARCH_MESON=n` ([kernel-eo1.fragment:24](../build/kernel-eo1.fragment:24))
and nothing in the tree mentions the EO2. This document records what an EO2 actually is, why EO1bian's USB-stick
trick does not carry over to it, which paths could still work, and what has already been tried on real EO2
hardware by other people and failed.

Written 2026-11-15. Confidence is marked inline:

- **[measured]** — read off a primary source: the EO2's own shipped filesystem, the FCC lab photographs, NXP or
  Amlogic vendor source or documentation.
- **[field]** — reported by an owner of a real EO2 doing the thing described.
- **[single source]** — one report, not corroborated.
- **[inference]** — reasoned from the above; not observed.

The EO2 hardware and filesystem evidence is largely not ours: it comes from the [FCC filing for the EO2](https://fccid.io/2AJPW-10D2AE)
and from the EO2 filesystem backup and live-device investigations kept in [aphex3k/EO1](https://github.com/aphex3k/EO1)
(`EO2/data/backup/`, [`docs/EO2-CAPABILITIES.md`](https://github.com/aphex3k/EO1/blob/main/docs/EO2-CAPABILITIES.md),
[`docs/RESEARCH.md`](https://github.com/aphex3k/EO1/blob/main/docs/RESEARCH.md)). A collaborator has a live EO2
(adb serial `20061229`); the EO1bian developer does not.

---

## 1. Short answer

The EO1 boots from a stick because of one accident of its 2014 board: a **separate SPI NOR chip** holds a U-Boot
whose `bootcmd` searches removable media for a script named `6x_bootscript`. EO1bian writes that script and nothing
else. It is not the CPU booting from USB — the i.MX6 BootROM cannot boot from USB mass storage at all.

**The EO2 is a different machine.** Its SoC is an **Amlogic S805 (Meson8b, quad Cortex-A5, 32-bit ARMv7)** — not an
i.MX6 — on an Amlogic/Geniatech Android TV-box reference board (`stvm8b`), running Android 4.4.2 on Amlogic's vendor
Linux 3.10 BSP **[measured]**. It has **no separate boot chip**: U-Boot, its environment and Android all live in the
one eMMC. Its stock U-Boot does **not** scan USB for a boot script — Amlogic's equivalent hook (`aml_autoscript`) is
wired to the **SD** controller only, and Amlogic's "USB boot" in silicon is a **device-mode download protocol**
(`1b8e:c003`), not a mass-storage boot.

So: **a USB stick cannot be assumed to boot an EO2, and three independent attempts to do something like it have
already failed** (§6). The honest framing for anyone asking is: *the EO1's "put a stick in and it just works" is a
property of the EO1's bootloader, and the EO2's bootloader does not offer that hook.* It does not mean the EO2 is
unbootable — there are four plausible paths (§7), the most promising being a **serial console into the vendor U-Boot**
(`usb start; fatload usb 0 …; autoscr` — proven on this exact silicon by Armbian) and the **U-Boot USB gadget mode
reachable from the EO2's factory adb**. Neither has been demonstrated on an EO2, and neither can promise the
"cannot brick" guarantee EO1bian makes on the EO1.

---

## 2. Why the EO1 boots from a stick

Six facts, each of which has to be re-established on any new board:

1. **Nothing before U-Boot is ours.** The EO1's first stage is i.MX6 BootROM, then a U-Boot in a separate SPI NOR.
   EO1bian never writes that chip — that is what makes it brick-free ([README:198-200](../README.md:198),
   [eo1-install-emmc:2](../overlay/usr/local/bin/eo1-install-emmc:2)), and the device tree even disables `ecspi1`
   so Linux can never touch that flash ([eo1-dts.py:166](../boot/eo1-dts.py:166)).
2. **The i.MX6 BootROM has no USB-mass-storage boot.** Its device list is NOR/OneNAND, NAND, SD/eMMC, SPI/I2C serial
   ROM, SATA — `BOOT_CFG1[7:4]` has no USB entry. USB on i.MX6 is the **Serial Downloader** (`BOOT_MODE=01`,
   protocol `15a2:0054`, driven by `imx_usb`/`uuu`), which streams an image into RAM from a PC.
   [NXP i.MX6DQ Reference Manual §8.5, §8.2.4](https://web.archive.org/web/20180829173128/https://www.nxp.com/docs/en/reference-manual/IMX6DQRM.pdf) **[measured]**
3. **The hook is a vendor convention.** Boundary Devices' `bootcmd` loops over `${bootdevs}` and, per device, runs
   `load ${dtype} ${disk}:1 0x10008000 /6x_bootscript && source 0x10008000`. EO1bian's [boot.cmd](../boot/boot.cmd)
   is compiled to exactly that filename (`mkimage -T script`, [finalize.sh:71](../build/finalize.sh:71)), so no
   hardware change, no env write, no bootloader rebuild is needed
   ([U-Boot `board/boundary/nitrogen6x/README`](https://raw.githubusercontent.com/QQxiaoming/quard_star_tutorial/main/u-boot-2023.04/board/boundary/nitrogen6x/README)) **[measured]**.
4. **"USB first" is an environment setting, not hardware.** Boundary's factory default is `bootdevs=sata mmc usb` —
   USB *last*. The EO1's `bootdevs=usb mmc` is an Electric Objects customisation observed in the field
   ([boot.cmd:2,8](../boot/boot.cmd:2)), and [README:403](../README.md:403) already admits a few EO1s look at
   internal memory first. Two DIP switches set the i.MX6 boot mode; both must be off
   ([README:401](../README.md:401)). **[field]**
5. **One FAT32 partition, everything in its root** — `6x_bootscript`, `zImage`, `eo1.dtb`, `uInitrd`, `rootfs.img`
   ([make-image.sh:28-38](../build/make-image.sh:28)). U-Boot only ever looks at `${disk}:1` and only needs a FAT
   driver, which is why the ZIP-and-unzip distribution works from Windows. **[measured]**
6. **The root filesystem is carried, not flashed**: ext4 `rootfs.img` loop-mounted read-only from FAT, with a tmpfs
   overlay, plus patches applied in the RAM layer ([scripts/eo1usb](../overlay/etc/initramfs-tools/scripts/eo1usb)).
   The board-specific part of that is only the module list (`ci_hdrc_imx`, `usbmisc_imx`, `phy_mxs_usb`,
   `sdhci-esdhc-imx`, `imxdrm` …) — the delivery mechanism itself is portable
   ([initramfs modules](../overlay/etc/initramfs-tools/modules)). **[measured]**

Two EO1 failure modes worth remembering, because both are about the *board file*, not the family: upgrading the EO1
to Boundary's 2022.x U-Boot broke stick boot (distro-boot uses `boot_targets`, not `bootdevs`, and the stick was
scanned and rejected), and Boundary's generic Nitrogen6X build cannot see the EO1's eMMC at all — the board-specific
`u-boot.eo` binary is required **[field]**.

---

## 3. The EO2 is a different machine

| | EO1 | EO2 |
|---|---|---|
| SoC | Freescale **MCIMX6D5EYM10AC** (i.MX6Dual, 2× Cortex-A9) **[measured]** | **Amlogic S805** — Meson8b, 4× Cortex-A5, 32-bit ARMv7 **[measured]** |
| Board lineage | Boundary Devices Nitrogen6x derivative ("Boundary EO Board", [eo1-dts.py:61](../boot/eo1-dts.py:61)) | Amlogic/Geniatech TV-box reference `stvm8b`, `boardtype=m8b_normal`, OTA path `geniatech.net/down-eng/upgrade/stvm8b_4.4/` **[measured]** |
| OS as shipped | Android 4.4, vendor BSP | Android 4.4.2, Amlogic vendor **Linux 3.10.33**, build `V002M8B01_20160907`, fingerprint `MBX/stvm8b/stvm8b:4.4.2/…:eng/test-keys` **[measured]** |
| Bootloader storage | **separate SPI NOR** (never written by EO1bian) | **the eMMC itself**: `bootstart=0`, `bootsize=0x100000`, `bootpath=u-boot.bin`; Armbian writes Meson8b U-Boot at `bs=512 seek=64`; recovery shows an `mtd bootloader` volume and a writable env at `/dev/block/env` **[measured]** |
| Boot script hook | `6x_bootscript`, searched on **usb and mmc** | `aml_autoscript` / `recovery.img`, searched on **mmc 0 only**; no `fatload usb` anywhere in the stock env **[measured]** |
| RAM / storage | 1 GB DDR3, 4 GB Kingston eMMC **[measured]** | 1 GB DDR3L (2× Nanya NT5CB256M16DP-EX), 8 GB Samsung KLM8G1WEPD-B031 eMMC (814 MiB usable, `/data` 4.98 GiB) **[measured]** |
| Panel path | LVDS straight from the i.MX6 (IPU → LDB) | Amlogic **HDMI** → **Lontium LT8668** bridge → LVDS panel; the running kernel has `lcd_lt8668@1-0029` bound on i2c; Samsung LTM230HL 23" PLS **[measured]** |
| Radio | TI WL1271 (TiWi-BLE), 2.4 GHz | dual-band Wi-Fi + **Broadcom BT on `/dev/ttyS2`** (BT 3.0/4.0 + 2.4 G + 5 G test reports in the FCC record) **[measured]** |
| USB | one micro-USB OTG, reachable from outside **[measured]** | micro-USB OTG reachable through the recessed rear pocket; host mode proven (USB keyboard/mouse via an OTG adapter) **[field]**; a second metal-shelled right-angle receptacle is visible in the FCC internal photos whose type is **not resolved** (§8) |
| SD card | microSD footprint **unpopulated** **[measured]** | a microSD reader exists on the board (`mmcblk1`); whether any slot is reachable from outside is **not established** **[field]** |
| Serial console | none published, ever | none published, ever **[field]** |
| Network adb | not shipped | **shipped unauthenticated**: `service.adb.tcp.port=5555`, `#ro.adb.secure=1` commented out, `ro.secure=0`, `ro.debuggable=1` **[measured]** |
| FCC | no grantee filing found (likely DoC via the pre-certified Wi-Fi module) | **FCC ID 2AJPW-10D2AE**, Electric Objects Inc, three applications all final-actioned 2016-12-02, permanent confidentiality on schematics **[measured]** |

The board change is total: different SoC family, different board vendor, different design lineage. Nothing in a
`6x_bootscript` transfers.

---

## 4. How an EO2 actually boots

From Amlogic's own vendor U-Boot source for this board family (`customer/board/configs/m8b_k15.h`,
[endlessm/u-boot-meson](https://raw.githubusercontent.com/endlessm/u-boot-meson/master/customer/board/configs/m8b_k15.h))
**[measured]**, and confirmed against the EO2's own dumped environment (`ubootenv.var.bootcmd=run storeboot`,
`boardname=stvm8b`, `boardtype=m8b_normal`, `envsize=0x10000`) **[measured]**:

```
bootcmd  = run storeboot
storeboot= imgread kernel boot ${loadaddr}; bootm; run recovery
recovery = if mmcinfo; then if fatload mmc 0 ${loadaddr} recovery.img; then bootm; fi; fi;
           imgread kernel recovery ${loadaddr}; bootm
update   = run usb_burning;                     # U-Boot's USB *gadget*, not host
           if mmcinfo; then if fatexist mmc 0 aml_sdc_burn.ini; then run sdc_burning;
           else if fatload mmc 0 ${loadaddr} aml_autoscript; then autoscr ${loadaddr}; fi; run recovery; fi; fi
preboot  = get_rebootmode; clear_rebootmode; run switch_bootmode    # reboot_mode=update|usb_burning|…
```

Consequences:

- **The kernel and ramdisk are read out of the eMMC (`imgread`), not from removable media.** The one script hook the
  stock chain runs from removable media is `aml_autoscript`, and it is looked for on `mmc 0` — the SD controller.
- **USB host capability exists in that U-Boot** (`CONFIG_CMD_USB`, `CONFIG_USB_STORAGE`, DWC2 OTG host) but the
  stock environment never calls `usb start`. The EO2's SoC has two DWC2 OTG controllers (`usb0`, `usb1`).
- **The Amlogic BootROM's "USB boot" is a device-mode download protocol**, gadget ID `1b8e:c003`, driven by Amlogic's
  `update` tool / USB Burning Tool or [pyamlboot](https://github.com/superna9999/pyamlboot). On this generation the
  ROM enters it only when it *cannot* load its second-stage loader from eMMC; the ordinary "burning" entry point is
  U-Boot's gadget (`usb_burning=update 1000`), reached via `reboot_mode` or a recovery button
  ([sormy/tvbian worklog](https://raw.githubusercontent.com/sormy/tvbian/master/tools/amlcmd/worklog.md),
  [U-Boot `doc/board/amlogic/boot-flow.rst`](https://raw.githubusercontent.com/u-boot/u-boot/master/doc/board/amlogic/boot-flow.rst)) **[measured]**.
- **Watch the name trap:** U-Boot's boot-flow table lists `S805X` under GX/AXG with USB boot. **S805X is a 64-bit
  GXL (Cortex-A53) part and is not the EO2's S805.** Mainline keeps them apart (`meson-gxl-s805x.dtsi` vs
  `meson8b.dtsi`) **[measured]**.
- **No signature-verified boot is visible** in this chain (plain `imgread`+`bootm`, `test-keys` build, writable env),
  and the EO1's i.MX6 HAB is confirmed Open/unused by the fact that an unsigned `zImage` runs
  **[measured/inference]**.
- **Mainline U-Boot does not support Meson8/Meson8b at all** — no defconfig, no board directory; the vendor fork is
  based on U-Boot 2011.03 **[measured]**. So EO1bian's trick cannot be re-created with mainline U-Boot; it does not
  need to be, because the vendor U-Boot already has autoscript and USB host.

---

## 5. What the EO2 does expose today

These are working, non-boot-chain entry points — the reason every EO2 revival so far is an APK rather than an OS:

- **adb over TCP 5555, unauthenticated, from boot, as root** (a shipped build property) **[measured]**.
- **USB host on the micro-USB port**: an OTG adapter plus a USB keyboard reaches the AOSP Settings app; this is the
  filmed community method for bringing a reset EO2 back ([r/electricobjects, Feb 2024](https://www.reddit.com/r/electricobjects/comments/1amitpa/howto_video_for_resurrecting_eo2/)) **[field]**.
- **Bluetooth** still works for file transfer (OBEX sideload of APKs) **[field]**.
- **Recovery mounts both an SD card (`/sdcard` = `mmcblk#p#`) and a USB stick (`/udisk` = `/dev/block/sda#`)** — so an
  `update.zip`-style path through recovery exists, but that is an *install* route inside Android, not a boot route
  **[measured]**.
- Every attempt to root the EO2 with Android rooting tools failed (roughly a dozen root APKs and PC tools) **[field]**;
  the frame's privileges come from the vendor build, not from an exploit.

---

## 6. What has already been tried on real EO2 hardware — and what happened

This is the part upstream should not have to rediscover.

| Attempt | Who, when | Result |
|---|---|---|
| Boot a Linux image on an EO2 **from USB**, then burn the same image to an SD card and use the board's own reader "to bypass potential USB issues" | msg43, r/electricobjects, Sep 2023 | **Neither booted.** The closest anyone has come to the EO1bian experiment on an EO2 **[field]** |
| Lumenary's EO1 recovery USB stick (USB Creator 1.1.5) — the closest existing analogue to an EO1bian stick | mdsimmonds1, a live EO2 | **Did not boot at all**: straight to the normal Android Hello screen, no LumenaryOS splash. Same person asks whether the EO2 has SW1/SW2 boot switches like the EO1 **[field]** |
| Clone a working EO2's eMMC to microSD (`dd` of `mmcblk0boot0`, then `mmcblk0` at a 4 MiB offset) and boot the cloned card in a bricked frame | f209, Mar 2026 | **Did not boot.** He attributes it to board revision (bricked frame silkscreened `VER 1.1`, donor `1.2`); that attribution is his own hunch, not established. Ended with "I'm replacing the board and going to put a Raspberry Pi in it." **[field]** |
| Edit the boot ramdisk on an EO2 | r/electricobjects | **Bootloop.** Recovery reachable only by adb; no EO2 firmware exists publicly to re-flash with **[field]** |
| Flash a **foreign** Amlogic box image (hand-edited `updater-script`) onto an EO2 | r/electricobjects | Flashed "successfully", then **killed the panel and USB enumeration entirely** **[field]** |
| Get the EO1's approach working on an EO2 (Lumenary) | README: "EO2 Note: Untested. May have different hardware" | Never tested; the first announced EO2 test is scheduled for **December 2026** ([issue #12](https://github.com/RichN001/lumenary-eo1/issues/12)) **[field]** |
| Reach a U-Boot prompt on either frame over serial | the whole subreddit | **Nobody has ever published a UART/serial session on an EO1 or an EO2**; the question "how do you get to the uboot prompt on a eo1?" went unanswered. The Amlogic community uses UART routinely on M8 boxes — the knowledge exists, it just never reached these frames **[field]** |

And the one success worth studying, because it proves Linux runs on this hardware: **[EO2 Linux](https://auntiepickle.github.io/eo2-linux-showcase/more.html)**
by auntiepickle — Alpine Linux armhf on the frame's **own 3.10 kernel**, installed from a computer over Bluetooth
(Wi-Fi hand-over, reusing the stock app's provisioning path) and then over the network, booting **from the recovery
partition** with Android left in place, three failed starts automatically returning to Android, and "a microSD rescue
card is the last resort". The frame stays closed and no SD card is needed. That is a *recovery-partition* install —
a flash write, though deliberately not of the bootloader — and it is the closest thing to a shipped EO2 Linux today
(code not yet public) **[field]**.

---

## 7. The candidate paths, ranked

Ranked by how much evidence supports them and how much they can still promise the owner.

1. **Serial console into the vendor U-Boot, then load from a stick.** `CONFIG_BOOTDELAY=1`,
   `console=ttyAML0,115200n8`; at the prompt: `usb start; fatload usb 0 <addr> boot.scr; autoscr <addr>`. Proven on
   Amlogic S805 hardware with **no** mainline U-Boot at all — Armbian's OneCloud recipe is exactly this
   ([hzyitc/armbian-onecloud](https://raw.githubusercontent.com/hzyitc/armbian-onecloud/master/README.md)), and another
   S805 box owner made the stock U-Boot search USB by setting `bootcmd='run start_autoscript; run storeboot'`
   **[measured + field]**. Cost: open the frame, find the UART pads (rows of unpopulated pads are visible next to the
   edge connectors in the FCC internal photos but are not confirmed as UART), TTL adapter. **Does not write flash**
   as long as nothing is `saveenv`-ed. This is the path with the best evidence and it is a *diagnostic* first step
   even if we never ship it.
2. **From live Android: `reboot update` / `reboot_mode=usb_burning`, then talk to the U-Boot gadget over the OTG
   port** with `pyamlboot`-style tooling. The stock env already honours those reboot modes, the ROM's USB function
   config is `mtp,adb` on the same controller class, and the gadget path gives "the whole eMMC and any U-Boot command
   over the OTG cable, case closed" **[measured]**. Unknowns: whether an EO2 enumerates as `1b8e:c003`, and pyamlboot
   ships only GX/G12 files, so M8 protocol work may be needed. This is the only path that needs **no** opening of the
   frame.
3. **SD / microSD `aml_autoscript` or `recovery.img`** — supported by the stock environment, but three owners failed
   to boot SD media on an EO2 (§6) and nobody has established an externally reachable slot. Unexplained failures, not
   a proven negative; worth one instrumented try from someone with a serial console attached.
4. **Write the recovery partition** (what EO2 Linux does today). Works, keeps Android, self-reverting after three bad
   starts — but it is a flash write, so it **cannot** carry the EO1bian promise "nothing here can brick your frame".
5. **Full re-flash with the Amlogic USB Burning Tool.** Blocked today: no EO2 firmware image exists publicly, the
   vendor's OTA host (`geniatech.net/down-eng/upgrade/stvm8b_4.4/`) no longer resolves, GIPHY refused to release the
   software, and the Armbian-forum claim that a MyGica ATV585/ATV185 (`stvm8b`) image fits is unconfirmed. A foreign
   image is how the frames in §6 got bricked.
6. **Force BootROM USB burn mode** by shorting pads / stopping the eMMC clock — last resort, board-specific, and the
   community record says a button can never reach ROM mode; it needs `FORCE_USB_BOOT` or a loader that fails.
7. Replace the eMMC, or swap the board for a Raspberry Pi (one owner's conclusion).

---

## 8. What is still unknown, and what would settle it

Open on the hardware:

- **Which receptacle is which.** The FCC internal photos show a right-angle micro-USB-class shell and a second,
  taller metal-shelled right-angle receptacle on another edge (USB-B vs a second micro-B unresolved; the external
  photos show a small capsule cutout in the rear pocket whose contents I could not resolve at exhibit resolution —
  it may be the reset pinhole cover or a status light pipe, not a port). The FCC **schematics, block diagram and
  operational description are withheld** under a permanent confidentiality request, so there is no public wiring
  diagram. Settle it with a photograph of the open rear pocket from an owner, and by checking whether a plugged-in
  stick appears as `/dev/block/sda` in recovery. Note the micro-USB connector is a **known mechanical weak point** —
  it has torn off at least two boards **[field]**.
- **Which DWC2 controller (`usb0`/`usb1`) reaches the accessible port.** U-Boot's own docs warn that Amlogic USB boot
  uses the *first* interface **[measured]**.
- **UART pads**: where, if anywhere. Rows of unpopulated pads are visible in the FCC photos next to the edge
  connectors; nobody has confirmed them.
- **Board revisions 1.1 vs 1.2**: silkscreened on the PCB, not discoverable over adb, differences undocumented. Any
  image plan has to say which revision it was built for.
- Whether the microSD reader is reachable from outside the shell.

Open on the software side, all cheap on the live EO2 over adb **[inference: these are the probes we would run]**:

```sh
getprop | grep -i ubootenv            # the live U-Boot env: bootcmd, boardname, boardtype, reboot_mode
cat /proc/cmdline                     # console=, the boot device, the loader version string
ls -l /dev/block/by-name /dev/block/  # partition names/sizes, the env device, mmcblk1 = the SD reader
ls -l /dev/block/mmcblk0boot0 /dev/block/mmcblk0boot1   # eMMC hardware boot partitions
dd if=/dev/mmcblk0 bs=512 count=64 | hexdump -C        # read-only: is u-boot.bin at offset 0?
getprop ro.bootloader ; getprop ro.build.version.ota
iw list ; ls /sys/class/bluetooth     # radio identity, for driver work later
```

and one decisive experiment with a host attached to the port before rebooting: `reboot update`, then `lsusb` for
`1b8e:c003` (a power cycle returns the frame to Android).

Open beyond booting at all — things an EO1 port never had to solve:

- **Display.** Mainline Linux has **no** Meson8/Meson8b display driver (`drivers/gpu/drm/meson` is GX-family only;
  zero meson8 references under `drivers/gpu`), and the community trees that show picture on M8 boxes carry
  out-of-tree HDMI patch queues. The EO2's panel is HDMI → **LT8668** bridge → LVDS, so no HDMI output means a black
  frame. Either the vendor 3.10 kernel (what EO2 Linux uses) or a patched mainline; the bridge also needs its i2c
  init (`lcd_lt8668@1-0029`).
- **Media**: the Amlogic VPU/player stack (`/dev/amstream*`, `amvdec_h265`, `libTsPlayer-jni.so`) is vendor-proprietary
  and present in the frame but not redistributable — the same licensing position EO1bian takes about NXP's VPU
  firmware ([README, "Doesn't the video decoder need licensed firmware?"](../README.md)).
- **Distribution**: the FAT32-single-partition, unzip-from-Windows story depends on the bootloader's file-system and
  partition assumptions. Amlogic boards relocate the MBR (Armbian's `OFFSET=16` quirk) and the vendor U-Boot of this
  generation wants `bootm`/uImage-style images, not `bootz`.
- **There is no board file.** The EO1 lesson applies: the generic Nitrogen6X build could not see the EO1's eMMC, and
  `u-boot.eo` was "the single critical artifact". An EO2 needs its own `stvm8b`-class description, and we have no EO2
  device tree, no boot image, and no published bootloader binary.

---

## 9. Searches that came up empty (so the next reader does not repeat them)

- **No EO2 teardown exists** in press or on iFixit. The only Electric Objects teardown anywhere is the 2016 [5khz EO1
  teardown](https://web.archive.org/web/20161107064706/http://www.5khz.com/2016-05-01/electric-objects-eo1-digital-art-display-teardown/),
  and it covers the EO1. No EO2 PCB silkscreen board number is legible in any exhibit photo.
- **No EO2 firmware, partition image, `.aml`, `update.zip` or eMMC dump has ever been published.** The only public
  EO2 filesystem material is the `adb pull` backup in [aphex3k/EO1](https://github.com/aphex3k/EO1) (text artifacts;
  the 340 binaries were removed from `main`'s tip and the `dump` branch is gone).
- **No EO1 FCC filing** under Electric Objects' grantee code — `2AJPW` has exactly one FCC ID, the EO2's.
- **No Armbian forum topic mentions Electric Objects**; no GitHub repo or code search turns up an unknown EO2 project;
  no `eo1bian` issue mentions the EO2; the only EO2-tagged issue anywhere in the ecosystem is
  [spalt/EO1#21](https://github.com/spalt/EO1) (a feature request).
- **No serial/UART console session has ever been published for either frame.**
- Tooling notes for whoever researches this next: reddit blocks direct fetching from most networks (403) —
  `api.pullpush.io` works, `ids=` and `after=`/`before=` work while `since`/`until` 400. DuckDuckGo HTML and lite
  hit CAPTCHA from this network after a few queries. The FCC's own servers return 403/503; the
  [`fcc-ea-mirror`](https://github.com/fcc-ea-mirror/2AJPW) GitHub copy of the exhibits has RC4-128 encrypted PDFs
  that render fine with a local PDF renderer, and fccid.io serves the same photographs as plain JPEGs under
  `https://fccid.io/images/device/2AJPW/-10D2AE/…`. `web.archive.org` is unreachable from the WebFetch tool but works
  over curl with ~5 s between requests.

---

## 10. What we propose to contribute, in order

1. This document, as the EO2 record for the project, plus the probe list run on a live EO2 (§8) and photographs of
   the rear pocket and both board sides.
2. A measured answer to "can code be loaded before the EO2's OS without writing its boot flash?" — path 1 or 2 in §7,
   attempted on the collaborator's EO2 with a serial console attached so the result is readable either way. If it
   works, the deliverable is a documented recipe and a device tree for `stvm8b`/EO2, not a stick image.
3. If it does not work, the contribution stays what this file is: the hardware map, the boot-chain read, and the list
   of attempts that failed, so that nobody spends another EO2 discovering that a stick does nothing.

One thing to say out loud in any upstream issue: EO1bian's headline promise — *put a stick in, it always boots, you
cannot brick it* — rests on the EO1's separate boot chip and its `6x_bootscript` hook. Nothing found on the EO2
supports that promise today. Any EO2 support has to either re-establish an equivalent hook (paths 1-3) or be labelled
with a different, weaker safety story (paths 4-6).
