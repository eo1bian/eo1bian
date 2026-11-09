#!/usr/bin/env python3
"""EO1 buttons: Freescale MPR121 capacitive controller on I2C bus 2 (0x21a8000), address 0x5a.

The Debian armmp kernel is built without the mpr121 driver, so this polls the chip from user space through
/dev/i2c-2. The two buttons are electrodes 1 and 3 (found by probing). Behaviour as in the Android app:
short press = screen on/off, long press (>= 0.8 s) = next picture, very long press (>= 4 s) = show the control
page's address and QR code for a minute. Actions go through the local agent API.
"""
import fcntl
import glob
import json
import os
import time
import urllib.request

BUS, ADDR, I2C_SLAVE_FORCE = "/dev/i2c-2", 0x5A, 0x0706
BUTTONS = (1, 3)
LONG = 0.8
VERY_LONG = 4.0


def conf():
    c = {"token": "eo1"}
    try:
        for line in open("/etc/eo1/eo1.conf", encoding="utf-8"):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                c[k] = v
    except OSError:
        pass
    return c


def screen_is_on():
    """Read the panel state straight from the backlight (bl_power 0 = on); no agent round trip needed."""
    for b in sorted(glob.glob("/sys/class/backlight/*")):
        try:
            return open(os.path.join(b, "bl_power")).read().strip() == "0"
        except OSError:
            pass
    return True


def agent(cmd):
    url = "http://127.0.0.1:2324/?cmd=%s&token=%s" % (cmd, conf().get("token", "eo1"))
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read().decode() or "{}")


def next_picture():
    """Long press: the agent's next (the server's playlist, or the EO1's own gallery when there is no server)."""
    agent("next")


class MPR121:
    def __init__(self):
        self.fd = os.open(BUS, os.O_RDWR)
        try:
            self._setup()
        except OSError:
            self.close()
            raise

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass

    def _setup(self):
        fcntl.ioctl(self.fd, I2C_SLAVE_FORCE, ADDR)
        self.w(0x80, 0x63)                       # soft reset
        time.sleep(0.01)
        self.w(0x5E, 0x00)                       # stop before configuring
        for i in range(12):                      # touch / release thresholds (values of the Linux driver)
            self.w(0x41 + 2 * i, 0x08)
            self.w(0x42 + 2 * i, 0x05)
        for reg, val in ((0x2B, 1), (0x2C, 1), (0x2F, 1), (0x30, 1), (0x31, 0xFF), (0x32, 2), (0x5D, 0x04),
                         (0x5C, 0x0B), (0x7B, 0x0B), (0x7D, 201), (0x7E, 130), (0x7F, 181)):
            self.w(reg, val)                     # filters + auto-configuration for 3.3 V
        self.w(0x5E, 0x80 | 12)                  # run all 12 electrodes, baseline tracking

    def w(self, reg, val):
        os.write(self.fd, bytes([reg, val]))

    def status(self):
        os.write(self.fd, bytes([0x00]))
        s = os.read(self.fd, 2)
        return s[0] | ((s[1] & 0x1F) << 8)


def main():
    """One physical touch often reaches both electrodes (1 and 3), so the buttons are treated as one:
    a gesture starts when any of them is touched and ends when all are released -> exactly one action."""
    mask = 0
    for b in BUTTONS:
        mask |= 1 << b
    chip = None
    started = None
    last_on, last_check = screen_is_on(), 0.0
    while True:
        try:
            if chip is None:
                chip = MPR121()
                print("MPR121 ready", flush=True)
            st = chip.status() & mask
        except OSError as e:
            print("I2C error:", e, flush=True)
            if chip is not None:
                chip.close()                 # one leaked fd per error ran the process out of fds
            chip = None
            time.sleep(5)
            continue
        now = time.time()
        if now - last_check > 1.0:          # log backlight changes made by anyone (bring-up: who darkens the panel?)
            last_check = now
            on = screen_is_on()
            if on != last_on:
                print("backlight is now %s (not by a button)" % ("on" if on else "OFF"), flush=True)
                last_on = on
        if st and started is None:
            started = now
        elif not st and started is not None:
            held, started = now - started, None
            try:
                if held >= VERY_LONG:
                    agent("showAddress")
                    print("very long press (%.1f s): control page address" % held, flush=True)
                elif held >= LONG:
                    next_picture()
                    print("long press (%.1f s): next picture" % held, flush=True)
                    last_on = screen_is_on()
                else:
                    on = screen_is_on()
                    agent("screenOff" if on else "screenOn")
                    print("short press (%.2f s): screen %s" % (held, "off" if on else "on"), flush=True)
                    last_on = not on
            except Exception as e:  # noqa: BLE001 - never die on a failed request
                print("agent request failed:", e, flush=True)
            time.sleep(0.3)                      # ignore bounce right after a release
        time.sleep(0.03)


if __name__ == "__main__":
    main()
