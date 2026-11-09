#!/usr/bin/env python3
"""Turn Linux's Boundary Nitrogen6x device tree into one for the Electric Objects EO1 board.

Input: decompiled imx6q-nitrogen6x.dtb (dtc -I dtb -O dts). Output: dts to compile into eo1.dtb.
Facts from the live EO1 (Android 3.0.35 kernel, read over ADB on 2 Nov 2026) and its 6x_bootscript:
  - panel: LVDS, split/dual-channel ("ldb=spl0"), 1920x1080 reduced blanking ("1920x1080MR@60"), RGB24
  - backlight: PWM1 (mxc_pwm.0 -> pwm-backlight.0); Nitrogen6x drives LVDS from PWM4, its parallel LCD from PWM1
  - Wi-Fi WL1271 on usdhc2 with WL_EN/BT_EN on GPIO 175/176 = same as Nitrogen6x (left as is)
  - eMMC usdhc4, SD usdhc3 (unpopulated), light sensor APDS9300 on I2C2 0x29, MPR121 keys on I2C3 0x5a
  - the USB stick sits on the OTG port (micro-USB + OTG adapter): force host mode
Changes are additive or disable things; the result is checked by recompiling it with dtc.
"""
import re
import sys


def find_node(src, pattern, start=0):
    """(open_index, close_index) of the first node whose header matches pattern (regex, at line start)."""
    m = re.compile(r"^(\s*)" + pattern + r"\s*\{", re.M).search(src, start)
    if not m:
        raise SystemExit(f"node not found: {pattern}")
    i = m.end() - 1
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return m.start(), j
    raise SystemExit("unbalanced braces")


def insert_props(src, pattern, props, start=0):
    a, b = find_node(src, pattern, start)
    brace = src.index("{", a)
    return src[:brace + 1] + "\n" + props + src[brace + 1:]


def set_status(src, a, b, status):
    body = src[a:b]
    if re.search(r'\n\s*status = "[^"]*";', body):
        body = re.sub(r'(\n\s*)status = "[^"]*";', r'\1status = "%s";' % status, body, count=1)
    else:
        brace = body.index("{")
        body = body[:brace + 1] + f'\n\t\tstatus = "{status}";' + body[brace + 1:]
    return src[:a] + body + src[b:]


def backlight_linear(src):
    """pwm-backlight nodes: 256 linear levels (0..255, interpolated by the driver), full brightness at start"""
    src = re.sub(r"brightness-levels = <0x00 0x04 0x08 0x10 0x20 0x40 0x80 0xff>;",
                 "brightness-levels = <0x00 0xff>;\n\t\tnum-interpolated-steps = <0xff>;", src)
    return re.sub(r"default-brightness-level = <0x07>;", "default-brightness-level = <0xff>;", src)


def main(inp, out):
    src = open(inp, encoding="utf-8").read()

    src = src.replace('model = "Boundary Devices i.MX6 Quad Nitrogen6x Board";',
                      'model = "Electric Objects EO1 (Boundary EO Board, i.MX6 Dual)";')

    # 1) LVDS: dual channel on channel 0 with the EO panel timing; channel 1 unused in split mode
    a, b = find_node(src, r"ldb")
    ldb = src[a:b]
    ldb = ldb.replace("{", "{\n\t\t\t\tfsl,dual-channel;", 1)
    ca, cb = find_node(ldb, r"lvds-channel@0")
    ch0 = ldb[ca:cb]
    timing = """
\t\t\t\t\tfsl,data-mapping = "spwg";
\t\t\t\t\tfsl,data-width = <0x18>;
\t\t\t\t\tstatus = "okay";
\t\t\t\t\tdisplay-timings {
\t\t\t\t\t\tnative-mode = <&eo1_timing>;
\t\t\t\t\t\teo1_timing: timing-1920x1080 {
\t\t\t\t\t\t\tclock-frequency = <138500000>;
\t\t\t\t\t\t\thactive = <1920>;
\t\t\t\t\t\t\tvactive = <1080>;
\t\t\t\t\t\t\thfront-porch = <48>;
\t\t\t\t\t\t\thback-porch = <80>;
\t\t\t\t\t\t\thsync-len = <32>;
\t\t\t\t\t\t\tvfront-porch = <3>;
\t\t\t\t\t\t\tvback-porch = <23>;
\t\t\t\t\t\t\tvsync-len = <5>;
\t\t\t\t\t\t\thsync-active = <1>;
\t\t\t\t\t\t\tvsync-active = <0>;
\t\t\t\t\t\t\tde-active = <1>;
\t\t\t\t\t\t\tpixelclk-active = <0>;
\t\t\t\t\t\t};
\t\t\t\t\t};"""
    ch0 = re.sub(r'\n\s*status = "[^"]*";', "", ch0)
    # dtc requires properties before subnodes: properties right after "{", display-timings at the end
    props, sub = timing.split("\n\t\t\t\t\tdisplay-timings {", 1)
    ch0 = ch0.replace("{", "{" + props, 1)
    ch0 = ch0.rstrip() + "\n\n\t\t\t\t\tdisplay-timings {" + sub + "\n\t\t\t\t"
    ldb = ldb[:ca] + ch0 + ldb[cb:]
    ca, cb = find_node(ldb, r"lvds-channel@1")
    ldb = set_status(ldb, ca, cb, "disabled")
    src = src[:a] + ldb + src[b:]

    # 2) the Nitrogen6x LVDS panel (Hannstar 10") must not claim channel 0: disabled -> imx-ldb uses display-timings
    m = re.search(r'compatible = "hannstar,hsd100pxn1";', src)
    if m:
        start = src.rfind("\n", 0, src.rfind("{", 0, m.start())) + 1
        hdr = src[start:src.index("{", start)].strip()
        pa, pb = find_node(src, re.escape(hdr))
        src = set_status(src, pa, pb, "disabled")

    # 3) USB OTG port: host mode (the stick is on the micro-USB port through an OTG adapter)
    a, b = find_node(src, r"usb@2184000")
    body = src[a:b]
    body = re.sub(r'\n\s*dr_mode = "[^"]*";', "", body)
    body = body.replace("{", '{\n\t\t\t\tdr_mode = "host";', 1)
    src = src[:a] + body + src[b:]

    # 3b) VBUS hands off: U-Boot already powers the OTG port (it read the stick). Nitrogen6x switches VBUS with
    # GPIO3_22, which may not be the EO board's pin. With no consumer and the regulator disabled, Linux never
    # touches that GPIO, so the stick keeps the power U-Boot gave it.
    a, b = find_node(src, r"usb@2184000")
    body = re.sub(r"\n\s*vbus-supply = <[^>]*>;", "", src[a:b])
    src = src[:a] + body + src[b:]
    m = re.search(r'regulator-name = "usb_otg_vbus";', src)
    if m:
        start = src.rfind("\n", 0, src.rfind("{", 0, m.start())) + 1
        hdr = src[start:src.index("{", start)].strip()
        ra, rb = find_node(src, re.escape(hdr), start - 1)
        src = set_status(src, ra, rb, "disabled")

    # 3c) Wi-Fi interrupt: Nitrogen6x has the WL1271 IRQ on GPIO6_14 (pad NANDF_CS1); the EO board has it on
    # GPIO6_11 (pad NANDF_CS0): Android's /proc/interrupts + /sys/kernel/debug/gpio show "wl1271_int" = gpio-171,
    # idle low (read over ADB, 2 Nov 2026). With the Nitrogen pin the IRQ fired >20 000/s (63 % CPU, flaky Wi-Fi).
    a, b = find_node(src, r"wlcore@2")
    body = re.sub(r"interrupts = <0x0e 0x04>;", "interrupts = <0x0b 0x04>;", src[a:b])
    assert body != src[a:b], "wlcore interrupt line not found"
    src = src[:a] + body + src[b:]
    a, b = find_node(src, r"wlan-vmmcgrp")
    body = src[a:b].replace("0x2e4 0x6cc 0x00 0x05 0x00 0x100b0", "0x2e0 0x6c8 0x00 0x05 0x00 0x100b0", 1)
    assert body != src[a:b], "wlan IRQ pad (NANDF_CS1) not found in wlan-vmmcgrp"
    src = src[:a] + body + src[b:]

    # 3d) RTC: the EO1 has a Micro Crystal RV4162 at 0x68 on I2C1 (Android: "rtc-rv4162 0-0068"), driven by
    # rtc-m41t80 in Debian; the Nitrogen6x ISL1208 at 0x6f is not there (its probe just fails, but the wrong
    # clock means no RTC time before the network is up).
    a, b = find_node(src, r"rtc@6f")
    src = src[:a] + src[a:b + 1].replace('compatible = "isil,isl1208";', 'compatible = "microcrystal,rv4162";').replace(
        "reg = <0x6f>;", "reg = <0x68>;").replace("rtc@6f", "rtc@68") + src[b + 1:]

    # 3e) eMMC on usdhc4 (Android: /dev/block/platform/sdhci-esdhc-imx.3). Nitrogen6x treats usdhc4 as a card slot
    # with a card-detect GPIO that the EO1 does not have, so Linux saw "no card" and never created mmcblk0.
    # Soldered eMMC: non-removable, 8 data lines (SD4_DATA4..7 on NANDF_D4..D7, ALT1).
    a, b = find_node(src, r"mmc@219c000")
    body = re.sub(r"\n\s*cd-gpios = <[^>]*>;", "", src[a:b])
    body = body.replace("bus-width = <0x04>;", "bus-width = <0x08>;\n\t\t\t\tnon-removable;", 1)
    assert body != src[a:b], "usdhc4 node unchanged"
    src = src[:a] + body + src[b:]
    a, b = find_node(src, r"usdhc4grp")
    body = src[a:b].replace("0x328 0x710 0x00 0x01 0x00 0x17059",
                            "0x328 0x710 0x00 0x01 0x00 0x17059 0x32c 0x714 0x00 0x01 0x00 0x17059 "
                            "0x330 0x718 0x00 0x01 0x00 0x17059 0x334 0x71c 0x00 0x01 0x00 0x17059 "
                            "0x338 0x720 0x00 0x01 0x00 0x17059", 1)
    assert body != src[a:b], "usdhc4grp pins not found"
    src = src[:a] + body + src[b:]

    # 3f) Hardware the Nitrogen6x has and the EO1 does not: switched off so Linux loads fewer drivers, udev has
    # fewer devices to settle and failing probes (SATA link, PCIe link training, HDMI, audio codec, touch
    # controllers, cameras, CAN, Ethernet PHY, second USB host) stop costing boot time. ecspi1 carries the SPI-NOR
    # with U-Boot: keeping Linux away from it is also a safety measure (never write that flash).
    import os
    phantoms = [r"pcie@1ffc000", r"spi@2008000", r"ssi@2028000", r"asrc@2034000", r"can@2090000", r"usb@2184200",
                r"ethernet@2188000", r"audmux@21d8000", r"sata@2200000", r"capture-subsystem", r"gpio-keys", r"sound",
                r"touchscreen@4", r"touchscreen@38"]
    # the display pipeline's unused components (HDMI, parallel display): cage hung at start when they were
    # disabled (2 Nov 2026, cause not yet understood), so they stay unless EO1_DTS_NO_HDMI=1
    if os.environ.get("EO1_DTS_NO_HDMI") == "1":
        phantoms += [r"hdmi@120000", r"disp0"]
    for pat in phantoms:
        na, nb = find_node(src, pat)
        src = set_status(src, na, nb, "disabled")
    for comp in ("fsl,sgtl5000", "ovti,ov5640", "ovti,ov5642", "ovti,ov5640_mipi"):
        m = re.search(r'compatible = "%s' % re.escape(comp), src)
        if m:
            start = src.rfind("\n", 0, src.rfind("{", 0, m.start())) + 1
            hdr = src[start:src.index("{", start)].strip()
            na, nb = find_node(src, re.escape(hdr), start - 1)
            src = set_status(src, na, nb, "disabled")

    # 3g) Backlight steps: Nitrogen6x has 8 logarithmic levels (0 4 8 16 32 64 128 255), so nothing between
    # 50 % and 100 % exists (the evening limit is 80 %). 256 linear levels like the Android app's
    # 0-255; the agent maps its 0-255 straight onto them.
    src = backlight_linear(src)

    # 4) light sensor APDS9300 on I2C2 (0x21a4000) at 0x29 (IIO driver apds9300)
    a, b = find_node(src, r"i2c@21a4000")
    src = src[:b] + '\n\t\t\t\tlight-sensor@29 {\n\t\t\t\t\tcompatible = "avago,apds9300";\n\t\t\t\t\treg = <0x29>;\n\t\t\t\t};\n' + src[b:]

    open(out, "w", encoding="utf-8").write(src)
    print("eo1 dts written:", out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
