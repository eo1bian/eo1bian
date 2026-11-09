# Licences in EO1bian

EO1bian's own code (everything in `overlay/`, `build/`, `boot/` and `stick/` written for this project) is under
the MIT licence of this repository. The image also contains other people's work under their own licences:

| What | Where | Licence |
|---|---|---|
| Debian 12 (bookworm) packages | the root filesystem (`build/packages.txt`) | each package's own licence, listed in `/usr/share/doc/<package>/copyright` in the image |
| Linux kernel 6.1 (`6.1.190-eo1`) | `zImage`, `/lib/modules` | GPL-2.0. Source: Debian's `linux-source-6.1` package, configured with Debian's `config-6.1.0-54-armmp` plus `build/kernel-eo1.fragment`, patched by `build/kernel-ipu-alpha.py`, built by `build/kernel-build.sh`. Ask in an issue if you need the exact source tarball of a release. |
| Device tree | `eo1.dtb` | GPL-2.0 (Debian's `imx6q-nitrogen6x.dts`, changed by `boot/eo1-dts.py`) |
| U-Boot tools, initramfs-tools, BusyBox | build and boot | GPL-2.0 (Debian packages) |
| Inter, Jost, Fraunces | `/usr/share/fonts/truetype/eo1bian/` | SIL Open Font License 1.1 (`OFL-*.txt` next to the fonts) |
| DejaVu fonts | `/usr/share/fonts/truetype/dejavu/` | Bitstream Vera / DejaVu licence (Debian `fonts-dejavu-core`) |
| QR Code generator (`qrcodegen.py`) | `/usr/local/lib/eo1/` | MIT, Project Nayuki (notice at the top of the file) |
| Wi-Fi firmware (TI WiLink) | `/lib/firmware/ti-connectivity/` | redistributable firmware from Debian's `firmware-ti-connectivity` |
| Tailscale (optional remote access) | `tailscale`, `tailscaled` - installed from Tailscale's own Debian repository (`build/tailscale.list`) | BSD-3-Clause (github.com/tailscale/tailscale) |

**Not in the image:** the video decoder firmware (NXP's `vpu_fw_imx6*.bin`) is not redistributable, so EO1bian
does not ship it: at every start the EO1 copies that file from its own Android system partition - the copy
Electric Objects licensed and shipped with the frame - into the running system, and it never leaves the device.
This project does not distribute these files and will not send them on request; every EO1 already carries its
own licensed copy. The Electric Objects app, its art and its fonts are not part of EO1bian; art rescued from an
EO1's cache stays with its owner.
