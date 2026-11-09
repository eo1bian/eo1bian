"""Wi-Fi status/scan/add for eo1-agent without NetworkManager: wpa_supplicant@wlan0 (control socket in
/run/wpa_supplicant) + systemd-networkd. The saved profile lives on the boot partition as state/wifi.nmconnection
(ssid=/psk= lines, NetworkManager keyfile format kept for compatibility); eo1-config turns it into
/etc/wpa_supplicant/wpa_supplicant-wlan0.conf at every boot. When /run/eo1-nm-on exists (eo1.txt: network=nm)
the nmcli-based functions in agent.py are used instead.
"""
import os
import re
import shlex
import subprocess
import time

IFACE = "wlan0"
CONF = "/etc/wpa_supplicant/wpa_supplicant-wlan0.conf"
STATE_PROFILE = "/media/eo1usb/state/wifi.nmconnection"


def nm_mode():
    return os.path.exists("/run/eo1-nm-on")


def _sh(cmd, timeout=10):
    try:
        r = subprocess.run(cmd if isinstance(cmd, list) else shlex.split(cmd), capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:  # noqa
        return 1, "", str(e)


def wpa(*args, timeout=10):
    return _sh(["wpa_cli", "-i", IFACE, *args], timeout=timeout)


def _dbm_to_pct(dbm):
    try:
        return max(0, min(100, 2 * (int(float(dbm)) + 100)))
    except (TypeError, ValueError):
        return None


def status():
    """{"connected", "ssid", "signal", "ip"} like the NetworkManager version."""
    _, out, _ = wpa("status")
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    connected = kv.get("wpa_state") == "COMPLETED"
    ssid = kv.get("ssid") if connected else None
    signal = None
    if connected:
        _, link, _ = _sh(["iw", "dev", IFACE, "link"])
        m = re.search(r"signal:\s*(-?\d+)", link)
        if m:
            signal = _dbm_to_pct(m.group(1))
    ip = None
    _, ipout, _ = _sh(["ip", "-4", "-o", "addr", "show", IFACE])
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", ipout)
    if m:
        ip = m.group(1)
    return {"connected": connected, "ssid": ssid, "signal": signal, "ip": ip}


def scan():
    """[{ssid, signal, security}] - a fresh scan takes ~3 s."""
    wpa("scan")
    time.sleep(3)
    _, out, _ = wpa("scan_results")
    nets, seen = [], set()
    for line in out.splitlines()[1:]:
        p = line.split("\t")
        if len(p) < 5 or not p[4] or p[4] in seen:
            continue
        seen.add(p[4])
        nets.append({"ssid": p[4], "signal": _dbm_to_pct(p[2]), "security": "open" if "WPA" not in p[3] else "wpa"})
    nets.sort(key=lambda n: -(n["signal"] or 0))
    return nets


def write_conf(ssid, psk, freq=None):
    """wpa_supplicant-wlan0.conf for one network. wpa_supplicant's "..." strings take no escapes, so the SSID
    goes in as hex and the password as the derived 256-bit key (PBKDF2, what wpa_passphrase computes): any
    character works, quotes and backslashes included. freq: scan only that channel at boot (eo1-wifi-fast)."""
    import hashlib
    body = ["ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev", "update_config=0", "ap_scan=1", "", "network={",
            "\tssid=%s" % ssid.encode("utf-8").hex()]
    if psk:
        key = hashlib.pbkdf2_hmac("sha1", psk.encode("utf-8"), ssid.encode("utf-8"), 4096, 32).hex()
        body += ["\tpsk=%s" % key, "\tkey_mgmt=WPA-PSK"]
    else:
        body += ["\tkey_mgmt=NONE"]
    if freq:
        body += ["\tscan_freq=%s" % freq]
    body += ["\tscan_ssid=1", "}", ""]
    tmp = CONF + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(body))
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONF)


def add(ssid, psk, connect=True):
    """Save the network (profile on the boot partition + wpa config) and reconnect."""
    if not ssid or len(ssid) > 32:
        return False, "ssid must be 1-32 characters"
    if psk and not 8 <= len(psk) <= 63:
        return False, "WPA password must be 8-63 characters"
    profile = "[connection]\nid=eo1-wifi\ntype=wifi\nautoconnect=true\n\n[wifi]\nmode=infrastructure\nssid=%s\n\n" % ssid
    if psk:
        profile += "[wifi-security]\nkey-mgmt=wpa-psk\npsk=%s\n\n" % psk
    profile += "[ipv4]\nmethod=auto\n"
    with open("/run/eo1-wifi.nmconnection", "w", encoding="utf-8") as f:
        f.write(profile)
    os.chmod("/run/eo1-wifi.nmconnection", 0o600)
    # keep what works now: a wrong password must not leave the frame off the network for good (the boot
    # partition is not editable from a PC once Debian is on the eMMC)
    old_conf = open(CONF).read() if os.path.exists(CONF) else None
    old_profile = open(STATE_PROFILE).read() if os.path.exists(STATE_PROFILE) else None
    rc, _, err = _sh(["eo1-stick-write", "--put", "/run/eo1-wifi.nmconnection", STATE_PROFILE], timeout=60)
    os.unlink("/run/eo1-wifi.nmconnection")
    if rc != 0:
        return False, "could not save the profile: " + err.strip()[:120]
    write_conf(ssid, psk)
    if not connect:
        return True, f"Network {ssid} saved"
    wpa("reconfigure")
    wpa("reassociate")
    for _ in range(30):
        time.sleep(1)
        if "wpa_state=COMPLETED" in wpa("status")[1]:
            return True, f"Network {ssid} saved and connected"
    # did not connect: put the previous network back
    if old_conf is not None:
        with open(CONF + ".tmp", "w", encoding="utf-8") as f:
            f.write(old_conf)
        os.chmod(CONF + ".tmp", 0o600)
        os.replace(CONF + ".tmp", CONF)
    if old_profile is not None:
        with open("/run/eo1-wifi.old", "w", encoding="utf-8") as f:
            f.write(old_profile)
        os.chmod("/run/eo1-wifi.old", 0o600)
        _sh(["eo1-stick-write", "--put", "/run/eo1-wifi.old", STATE_PROFILE], timeout=60)
        os.unlink("/run/eo1-wifi.old")
    wpa("reconfigure")
    return False, f"Could not connect to {ssid} within 30 s: the previous network is back"
