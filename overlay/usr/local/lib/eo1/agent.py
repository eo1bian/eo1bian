#!/usr/bin/env python3
"""eo1-agent: the EO1's HTTP API on port 2324 - the control page, the setup page, the live remote and scripts all
use it: GET /?cmd=<cmd>&token=<token>[&value=|&url=|...], JSON replies {"status": "OK"|"Error", "statustext": ...}.

Runs as root (backlight sysfs, nmcli). In the optional browser mode it talks to Chromium through its DevTools port
on 127.0.0.1:9222 (poll / reload / load URL) and takes screenshots with grim inside the cage session of user eo1.
Config: /etc/eo1/eo1.conf (written by eo1-config from eo1.txt on the stick).
"""
import io
import json
import os
import re
import shlex
import signal
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys_path = os.path.dirname(os.path.abspath(__file__)) if "__file__" in globals() else "/usr/local/lib/eo1"
import sys
sys.path.insert(0, sys_path)
import ntfyremote  # live remote from anywhere through ntfy (encrypted, a fixed list of commands)
import pairing  # connect a computer: one-time code + one command (SSH key, "ssh eo1")
import wifi as eo1wifi  # wpa_supplicant/networkd mode (default); nmcli mode when /run/eo1-nm-on exists

try:
    VERSION = open("/etc/eo1bian-version").read().strip()     # set by the image build and by updates
except OSError:
    VERSION = "debian-0.1"
PORT = 2324
CONF = "/etc/eo1/eo1.conf"
STICK = "/media/eo1usb"
DEVTOOLS = "http://127.0.0.1:9222"
KIOSK_USER = "eo1"
STARTED = time.time()
STATE = {"brightness": None, "last_poll_cmd": 0}
SCREEN_LOCK = threading.Lock()
WANTED = "/run/eo1-brightness"   # the brightness asked for (0-255), before the night cap
SPLASH_NOW = threading.Event()   # set when the screen goes off: save the picture for the next boot at once


def conf():
    c = {"server": "", "token": "eo1", "rotate": "90"}
    try:
        for line in open(CONF, encoding="utf-8"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                c[k.strip().lower()] = v.strip().strip('"')
    except OSError:
        pass
    return c


def sh(cmd, timeout=15):
    try:
        r = subprocess.run(cmd if isinstance(cmd, list) else shlex.split(cmd), capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except Exception as e:  # noqa
        return 1, "", str(e)


# ------------------------------------------------------------------ backlight
def backlights():
    base = "/sys/class/backlight"
    try:
        return [os.path.join(base, d) for d in sorted(os.listdir(base))]
    except OSError:
        return []


def _write(path, value):
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return True
    except OSError:
        return False


def night_cap():
    """Highest allowed brightness (0-255) right now: night_max % (default 80) inside night= (default
    19:00-07:00, may wrap midnight), 255 otherwise. Applies to every setter, auto brightness included."""
    c = conf()
    try:
        a, b = c.get("night", "19:00-07:00").split("-")
        lo, hi = (int(x.split(":")[0]) * 60 + int(x.split(":")[1]) for x in (a, b))
        pct = max(1, min(100, int(c.get("night_max", "80"))))
    except (ValueError, IndexError):
        return 255
    t = time.localtime()
    now = t.tm_hour * 60 + t.tm_min
    inside = lo <= now < hi if lo < hi else (now >= lo or now < hi)
    # pct is perceived brightness: the eye is far from linear, 80 % PWM looked like ~93 % ("far too bright at
    # night"). Gamma 2.2: 80 % -> 156/255, 50 % -> 56/255.
    return max(1, round(255 * (pct / 100) ** 2.2)) if inside else 255


def set_brightness(v255):
    """0-255 like the Android app; scaled to each backlight's max_brightness (256 linear levels with the EO1
    dtb since 3 Nov 2026), capped by night_cap(). STATE["brightness"] keeps the wanted value, so the cap lifts
    by itself in the morning."""
    v255 = max(0, min(255, int(v255)))
    if v255 > 0:
        STATE["brightness"] = v255
        try:
            with open(WANTED, "w") as f:
                f.write(str(v255))
        except OSError:
            pass
    return write_level(v255)


def write_level(v255):
    """Put a 0-255 level on the backlights (night cap applied), without touching the wanted manual level."""
    v255 = min(max(0, int(v255)), night_cap())
    done = []
    for b in backlights():
        try:
            mx = int(open(os.path.join(b, "max_brightness")).read().strip() or "1")
        except (OSError, ValueError):
            mx = 255
        val = round(v255 * mx / 255)
        if v255 > 0 and val == 0:
            val = 1
        if _write(os.path.join(b, "brightness"), val):
            done.append(f"{os.path.basename(b)}={val}/{mx}")
    return done


# ------------------------------------------------------------------ settings kept on the EO1 (gallery partition)
SETTINGS = "/media/eo1gallery/settings.json"     # titles, font, auto-brightness range, schedule, interval


def load_settings():
    try:
        return json.load(open(SETTINGS, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(cur):
    with open(SETTINGS + ".tmp", "w", encoding="utf-8") as f:
        json.dump(cur, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(SETTINGS + ".tmp", SETTINGS)


SETTINGS_LOCK = threading.RLock()                # several threads read-modify-write settings.json


def settings_set(**kv):
    """Change keys under the lock, so the phone, the live remote and background threads never lose
    each other's writes."""
    with SETTINGS_LOCK:
        cur = load_settings()
        cur.update(kv)
        save_settings(cur)
        return cur


# ------------------------------------------------------------------ automatic brightness (APDS9300)
AUTO_FILE = "/run/eo1-auto-brightness.json"
# default from eo1.txt (auto_brightness=on|off, auto_curve=low|default|high; on since 3 Nov 2026), then the
# last choice from the app/server for this boot (/run)
AUTO = {"on": conf().get("auto_brightness", "off") == "on", "curve": conf().get("auto_curve", "default"),
        "lux": None, "level": None,
        # the range the light sensor may use and a nudge up or down, in % (the sensor sits behind the bezel and
        # reads low on some units, so the curve alone can be too dark)
        "min": 2, "max": 100, "offset": 0}
AUTO.update({k: v for k, v in load_settings().get("auto", {}).items() if k in ("min", "max", "offset")})
try:
    AUTO.update({k: v for k, v in json.load(open(AUTO_FILE)).items() if k in ("on", "curve")})
except (OSError, ValueError):
    pass


def auto_save():
    try:
        with open(AUTO_FILE, "w") as f:
            json.dump({"on": AUTO["on"], "curve": AUTO["curve"]}, f)
    except OSError:
        pass


class LightSensor:
    """The EO1's ambient light sensor: APDS9300 on I2C bus 1 at 0x29 (no kernel driver needed; the Android app
    read the same chip). Channel 0 = visible + IR, channel 1 = IR; lux by the datasheet's piecewise formula
    (402 ms integration, gain 1x: the chip's power-on defaults)."""
    BUS, ADDR, I2C_SLAVE = "/dev/i2c-1", 0x29, 0x0703

    def __init__(self):
        import fcntl
        self.fd = os.open(self.BUS, os.O_RDWR)
        try:
            fcntl.ioctl(self.fd, self.I2C_SLAVE, self.ADDR)
            os.write(self.fd, bytes([0x80, 0x03]))      # CONTROL: power on
        except OSError:
            os.close(self.fd)
            raise

    def word(self, reg):
        os.write(self.fd, bytes([0xA0 | reg]))          # command + word protocol
        b = os.read(self.fd, 2)
        return b[0] | b[1] << 8

    def lux(self):
        ch0, ch1 = self.word(0x0C), self.word(0x0E)
        if ch0 == 0:
            return 0.0
        r = ch1 / ch0
        if r <= 0.50:
            v = 0.0304 * ch0 - 0.062 * ch0 * r ** 1.4
        elif r <= 0.61:
            v = 0.0224 * ch0 - 0.031 * ch1
        elif r <= 0.80:
            v = 0.0128 * ch0 - 0.0153 * ch1
        elif r <= 1.30:
            v = 0.00146 * ch0 - 0.00112 * ch1
        else:
            v = 0.0
        return max(0.0, v)

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


def auto_curve(lux, mode):
    """The original Electric Objects lux -> backlight curves (6..255), as in the Android app (AutoBright.java)."""
    lux = max(0.0, lux)
    if lux <= 27:
        v = 4e-4 * (12 + lux) ** 3 + 8 if mode == "high" else 1.62e-4 * (21 + lux) ** 3 + 5
        if mode == "low":
            v = max(6, v * 0.6)
    elif lux <= 100:
        ratio = 220 if mode == "high" else 600 if mode == "low" else 300
        v = max(256.0 / ratio * lux, auto_curve(27, mode))
    else:
        v = ((30 * (lux - 80)) ** 0.5 + 93 if mode == "high" else (10 * (lux - 80)) ** 0.5 + 28 if mode == "low"
             else (10 * (lux - 80)) ** 0.5 + 70)
    i = max(6, min(255, round(v)))
    return 9 if i == 8 else i                     # the EO firmware skipped 8 (flicker)


def auto_thread():
    """Every half second: lux -> target level; the backlight ramps one step per 55 ms like the original."""
    sensor, target, cur = None, None, None
    while True:
        try:
            if sensor is None:
                sensor = LightSensor()
            AUTO["lux"] = round(sensor.lux(), 1)
        except OSError:
            if sensor:
                sensor.close()
            sensor = None
            time.sleep(10)
            continue
        if not AUTO["on"] or not screen_is_on():
            target, cur = None, None
            time.sleep(0.5)
            continue
        t = auto_curve(AUTO["lux"], AUTO["curve"]) + AUTO["offset"] * 2.55
        t = int(round(min(max(t, AUTO["min"] * 2.55), max(AUTO["max"], AUTO["min"]) * 2.55)))
        if target is None or abs(t - target) >= 3 or (t != target and (t <= 12 or target <= 12)):
            target = t
        if cur is None:
            cur = current_brightness() or target
        end = time.time() + 0.5
        while cur != target and time.time() < end and AUTO["on"]:
            cur += 1 if cur < target else -1
            if cur == 8:
                cur += 1 if target > 8 else -1
            write_level(cur)
            AUTO["level"] = cur
            time.sleep(0.055)
        time.sleep(max(0.0, end - time.time()))


# ------------------------------------------------------------------ the frame's own control page and uploads
CONTROL_PAGE = "/usr/share/eo1/control.html"
SETUP_PAGE = "/usr/share/eo1/setup.html"         # Wi-Fi setup from a phone on the EO1's own network
PC_PAGE = "/usr/share/eo1/pc.html"               # "connect a computer": open eo1.local, type the 4-digit code
PAGE_FILES = {                                   # the control page as an app on the phone's home screen
    "/manifest.webmanifest": ("/usr/share/eo1/manifest.webmanifest", "application/manifest+json"),
    "/icon.svg": ("/usr/share/eo1/icon.svg", "image/svg+xml"),
    "/icon.png": ("/usr/share/eo1/icon.png", "image/png"),
}


GALLERY_KEYS = ("github_repo", "github_branch", "source", "immich_url", "immich_albums")
IMMICH_KEY = STICK + "/state/immich-key"


def save_gallery_conf(**new):
    """The gallery settings (GitHub and Immich) into state/gallery.conf on the boot partition (they survive
    reboots; eo1-config reads them at boot) and, live, into eo1.conf."""
    cur = conf()
    vals = {k: new.get(k, cur.get(k, "")) for k in GALLERY_KEYS}
    vals["github_branch"] = vals["github_branch"] or "eo1"
    vals["source"] = vals["source"] or "auto"
    with open("/run/eo1-gallery.conf", "w") as f:
        f.write("".join("%s=%s\n" % (k, vals[k]) for k in GALLERY_KEYS))
    sh(["eo1-stick-write", "--put", "/run/eo1-gallery.conf", STICK + "/state/gallery.conf"], timeout=60)
    lines = [l for l in open(CONF).read().splitlines() if not l.startswith(tuple(k + "=" for k in GALLERY_KEYS))]
    lines += ["%s=%s" % (k, vals[k]) for k in GALLERY_KEYS]
    with open(CONF + ".tmp", "w") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(CONF + ".tmp", CONF)
    return vals


def put_secret(value, dst):
    """A key or token onto the boot partition's state/ (never into a URL, never into eo1.txt)."""
    tmp = "/run/eo1-secret.tmp"
    with open(tmp, "w") as f:
        os.chmod(tmp, 0o600)
        f.write(value)
    rc, _, err = sh(["eo1-stick-write", "--put", tmp, dst], timeout=60)
    os.unlink(tmp)
    if rc != 0:
        raise RuntimeError("could not save it: " + err.strip()[:120])


IMMICH_URL = re.compile(r"https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~/-]*)?")
IMMICH_ID = re.compile(r"[0-9a-fA-F-]{36}")


def immich_albums(url, key):
    """The albums on an Immich server: [{id, name, count}] (the API key needs album.read)."""
    req = urllib.request.Request(url.rstrip("/") + "/api/albums",
                                 headers={"x-api-key": key, "Accept": "application/json", "User-Agent": "eo1"})
    with urllib.request.urlopen(req, timeout=20) as r:
        albums = json.loads(r.read())
    shared = []
    try:                                         # albums shared with this user too
        req2 = urllib.request.Request(req.full_url + "?shared=true", headers=req.headers)
        with urllib.request.urlopen(req2, timeout=20) as r:
            shared = json.loads(r.read())
    except Exception:  # noqa: BLE001 - only the own albums then
        pass
    seen, out = set(), []
    for a in albums + shared:
        if a.get("id") in seen:
            continue
        seen.add(a.get("id"))
        out.append({"id": a.get("id"), "name": a.get("albumName") or "", "count": a.get("assetCount", 0)})
    return sorted(out, key=lambda a: a["name"].lower())


def gallery_space():
    """The gallery partition in MB: a GitHub gallery takes about twice its size (git's packed copy + the files)."""
    try:
        st = os.statvfs("/media/eo1gallery")
        return {"totalMB": st.f_blocks * st.f_frsize // 2 ** 20, "freeMB": st.f_bavail * st.f_frsize // 2 ** 20}
    except OSError:
        return None


def cpu_temp():
    try:
        return round(int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000, 1)
    except (OSError, ValueError):
        return None
UPLOADS = "/media/eo1gallery/uploads"            # pictures sent from the phone, shown with the frame's own gallery
PLAYER_CMD = "/run/eo1-player-cmd"               # next/prev for the player's own slideshow


def uploads_index():
    try:
        return json.load(open(os.path.join(UPLOADS, "index.json"), encoding="utf-8"))
    except (OSError, ValueError):
        return []


UPLOADS_LOCK = threading.RLock()


def uploads_save(items):
    tmp = os.path.join(UPLOADS, "index.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, os.path.join(UPLOADS, "index.json"))


def save_upload(data, name):
    """A picture from the phone -> fitted to the portrait panel (1080x1920, like the server), stored on the
    gallery partition (written once). Returns the new entry."""
    from PIL import Image, ImageOps
    if not os.path.ismount("/media/eo1gallery"):
        raise RuntimeError("the gallery storage is not mounted")
    os.makedirs(UPLOADS, exist_ok=True)
    im = Image.open(io.BytesIO(data))
    im = ImageOps.exif_transpose(im) or im
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (0, 0, 0))
        bg.paste(im, mask=im.getchannel("A"))
        im = bg
    im = im.convert("RGB")
    w, h = im.size
    s = min(1080 / w, 1920 / h)
    if s < 1:
        im = im.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS, reducing_gap=3.0)
    uid = "%d" % int(time.time() * 1000)
    path = os.path.join(UPLOADS, uid + ".jpg")
    im.save(path + ".tmp", "JPEG", quality=92, subsampling=0, optimize=True)
    with open(path + ".tmp", "rb") as f:
        os.fsync(f.fileno())
    os.replace(path + ".tmp", path)
    title = os.path.splitext(os.path.basename(name or ""))[0].replace("_", " ")[:80] or "From your phone"
    entry = {"id": uid, "title": title, "added": int(time.time())}
    with UPLOADS_LOCK:
        items = uploads_index()
        items.append(entry)
        uploads_save(items)
    return entry


HIDDEN = "/media/eo1gallery/hidden.json"        # items hidden from the phone: [{"id", "title"}], kept on the frame


def hidden_list():
    try:
        return json.load(open(HIDDEN, encoding="utf-8"))
    except (OSError, ValueError):
        return []


def item_titles():
    """id -> title of everything the frame's own slideshow knows (gallery and uploads), for the hidden list."""
    out = {}
    try:
        gal = os.path.join("/media/eo1gallery", open("/media/eo1gallery/active").read().strip())
        for it in json.load(open(os.path.join(gal, "manifest.json"), encoding="utf-8")).get("items", []):
            out["local-" + it["id"]] = it.get("title") or ""
    except (OSError, ValueError):
        pass
    for e in uploads_index():
        out["local-up-" + e["id"]] = e.get("title") or ""
    return out


PHONE_ALBUM = "Your pictures"                    # the same name as in the player


def albums_info():
    """The albums the frame has (how many pictures, how many of them hidden) and which ones play."""
    counts, default, items, origin = {}, [], [], "github"
    try:
        gal = os.path.join("/media/eo1gallery", open("/media/eo1gallery/active").read().strip())
        m = json.load(open(os.path.join(gal, "manifest.json"), encoding="utf-8"))
        default = m.get("settings", {}).get("albums") or []
        origin = "github" if "github" in gal else "immich" if m.get("from") == "immich" else "server"
        items = [(it.get("album") or "", "local-" + it["id"]) for it in m.get("items", [])]
    except (OSError, ValueError, KeyError):
        pass
    items += [(PHONE_ALBUM, "local-up-" + e["id"]) for e in uploads_index()]
    hid = {e["id"] for e in hidden_list()}
    for key, iid in items:
        a = counts.setdefault(key, {"name": key or "Gallery", "key": key, "count": 0, "hidden": 0,
                                    "source": "phone" if key == PHONE_ALBUM else origin})
        a["count"] += 1
        a["hidden"] += iid in hid
    chosen = load_settings().get("albums")
    return {"albums": sorted(counts.values(), key=lambda a: a["name"].lower()),
            "albumsOn": default if chosen is None else chosen}


def album_items(album=None):
    """The pictures the frame has, in gallery order: [(id "local-...", title, album, file path)], optionally of one
    album. Gallery pictures, a video's poster for a video, and the phone's uploads ("Your pictures")."""
    out = []
    try:
        gal = os.path.join("/media/eo1gallery", open("/media/eo1gallery/active").read().strip())
        for it in json.load(open(os.path.join(gal, "manifest.json"), encoding="utf-8")).get("items", []):
            f = it.get("poster") if it.get("type") == "video" else it.get("file")
            if f:
                out.append(("local-" + it["id"], it.get("title") or "", it.get("album") or "", os.path.join(gal, f)))
    except (OSError, ValueError, KeyError):
        pass
    for e in uploads_index():
        out.append(("local-up-" + e["id"], e.get("title") or "", PHONE_ALBUM, os.path.join(UPLOADS, e["id"] + ".jpg")))
    return [x for x in out if album is None or x[2] == album]


SHEET_COLS, SHEET_W, SHEET_H = 4, 120, 213       # an album's contact sheet: 4 across, 9:16 cells


def album_sheet(album, limit=80):
    """One JPEG with small pictures of an album (for the live remote's picture browser) and the ids in its order.
    Cached in /run until the album changes."""
    import base64
    import hashlib
    from PIL import Image, ImageOps
    items = album_items(album)[:limit]
    key = hashlib.sha1(json.dumps([album] + [i[0] for i in items]).encode()).hexdigest()[:16]
    cache = "/run/eo1-sheet-%s.jpg" % key
    if not os.path.exists(cache):
        rows = max(1, (len(items) + SHEET_COLS - 1) // SHEET_COLS)
        sheet = Image.new("RGB", (SHEET_COLS * SHEET_W, rows * SHEET_H), (20, 17, 15))
        for n, (_, _, _, path) in enumerate(items):
            try:
                im = Image.open(path)
                im.draft("RGB", (SHEET_W * 2, SHEET_H * 2))
                im = ImageOps.fit(im.convert("RGB"), (SHEET_W, SHEET_H))
                sheet.paste(im, ((n % SHEET_COLS) * SHEET_W, (n // SHEET_COLS) * SHEET_H))
            except Exception:  # noqa: BLE001 - one unreadable picture leaves its cell dark
                pass
        sheet.save(cache + ".tmp", "JPEG", quality=62)
        os.replace(cache + ".tmp", cache)
    return {"album": album, "cols": SHEET_COLS, "w": SHEET_W, "h": SHEET_H,
            "items": [{"id": i[0], "title": i[1]} for i in items],
            "img": base64.b64encode(open(cache, "rb").read()).decode()}


def hidden_save(items):
    with open(HIDDEN + ".tmp", "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(HIDDEN + ".tmp", HIDDEN)


def player_local_cmd(cmd):
    """next/prev for the player's own slideshow (gallery or uploads), then wake it"""
    with open(PLAYER_CMD, "w") as f:
        f.write(cmd)
    pid = native_player()
    if pid:
        try:
            os.kill(pid, signal.SIGUSR1)
        except OSError:                          # restarted between the lookup and the signal
            pass


def read_install():
    """/run/eo1-install.json, or None: never an error while eo1-install-run swaps the file."""
    try:
        return json.load(open("/run/eo1-install.json"))
    except (OSError, ValueError):
        return None


TS_DIR = "/media/eo1gallery/tailscale"           # remote access: Tailscale's state, and "enabled" when it is on
_ts_cache = {"at": 0.0, "v": {"state": "off"}}


def ts_status(fresh=False):
    """Remote access for the control page: state off / starting / login (authUrl) / on (name, ip)."""
    if not fresh and time.time() - _ts_cache["at"] < 8:
        return _ts_cache["v"]
    v = {"state": "off"}
    if os.path.exists(TS_DIR + "/enabled"):
        v = {"state": "starting"}
        try:
            r = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=6)
            j = json.loads(r.stdout or "{}")
            be, me = j.get("BackendState", ""), j.get("Self") or {}
            if be == "Running":
                v = {"state": "on", "name": (me.get("DNSName") or "").rstrip("."),
                     "ip": next((a for a in me.get("TailscaleIPs") or [] if "." in a), "")}
            elif j.get("AuthURL"):
                v = {"state": "login", "authUrl": j["AuthURL"]}
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    _ts_cache.update(at=time.time(), v=v)
    return v


def ts_set(mode):
    """on: start Tailscale and ask for a login link; off: stop it (stays signed in); forget: sign out too."""
    if mode == "on":
        os.makedirs(TS_DIR, exist_ok=True)
        open(TS_DIR + "/enabled", "w").close()
        sh(["systemctl", "start", "tailscaled.service"], timeout=30)
        for _ in range(20):                       # the daemon's socket
            if os.path.exists("/run/tailscale/tailscaled.sock"):
                break
            time.sleep(0.5)
        # "up" waits until the owner has signed in, so it runs on its own; the login link shows in the status
        subprocess.Popen(["tailscale", "up", "--hostname", socket.gethostname() or "eo1"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(16):
            if ts_status(fresh=True)["state"] in ("login", "on"):
                break
            time.sleep(0.5)
    else:
        if mode == "forget":
            sh(["tailscale", "logout"], timeout=20)
        else:
            sh(["tailscale", "down"], timeout=20)
        try:
            os.remove(TS_DIR + "/enabled")
        except OSError:
            pass
        sh(["systemctl", "stop", "tailscaled.service"], timeout=30)
    return ts_status(fresh=True)


WELCOME_PICTURE = "/usr/share/eo1/eo1bian-main.png"


def local_api(cmd, raw=False, **params):
    """The agent's own API from inside (the live remote uses it: the same rules as the control page)."""
    q = urllib.parse.urlencode(dict(params, cmd=cmd, token=conf().get("token", "eo1")))
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/?%s" % (PORT, q), timeout=30) as r:
            j = json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        j = json.loads(e.read().decode() or "{}")
    return j if raw else j.get("statustext", "")


def qr_png(text, scale=8):
    """A QR code as PNG (the control page shows the live remote's pairing link with it)."""
    import qrcodegen
    from PIL import Image
    q = qrcodegen.QrCode.encode_text(text, qrcodegen.QrCode.Ecc.MEDIUM)
    n = q.get_size()
    im = Image.new("L", (n + 8, n + 8), 255)
    for y in range(n):
        for x in range(n):
            if q.get_module(x, y):
                im.putpixel((x + 4, y + 4), 0)
    im = im.resize(((n + 8) * scale, (n + 8) * scale), Image.NEAREST)
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()


def welcome_upload_thread():
    """Once per EO1: the EO1bian picture becomes one of the owner's pictures (title "EO1bian"), so it stays in the
    slideshow after setup; deleting it on the control page is final (settings "welcomeAdded")."""
    for _ in range(120):                       # the gallery partition may mount a little after the agent starts
        try:
            cur = load_settings()
            if os.path.ismount("/media/eo1gallery"):
                # settings.json may mount after the agent started: the saved range applies as soon as it is there
                AUTO.update({k: v for k, v in cur.get("auto", {}).items() if k in ("min", "max", "offset")})
            if cur.get("welcomeAdded"):
                return
            if os.path.ismount("/media/eo1gallery"):
                save_upload(open(WELCOME_PICTURE, "rb").read(), "EO1bian")
                settings_set(welcomeAdded=True)
                return
        except Exception:  # noqa: BLE001 - try again later
            pass
        time.sleep(10)


# commands after which the EO1bian picture shown after setup gives way at once (the owner is doing something)
WELCOME_ENDS = {"next", "prev", "hide", "unhide", "deleteUpload", "titles", "gallery", "interval", "screenOff"}


def end_welcome():
    """The owner acted on the control page: stop showing the EO1bian picture that follows the first setup."""
    p = "/run/eo1-setup-done.json"
    try:
        st = json.load(open(p))
        if st.get("welcomeDone"):
            return
        st["welcomeDone"] = True
        with open(p + ".tmp", "w") as f:
            json.dump(st, f)
        os.replace(p + ".tmp", p)
        pid = native_player()
        if pid:
            os.kill(pid, signal.SIGUSR1)
    except (OSError, ValueError):
        pass


SCHEDULE_WAKE = threading.Event()


def in_window(now_min, w):
    off = int(w["off"][:2]) * 60 + int(w["off"][3:])
    on = int(w["on"][:2]) * 60 + int(w["on"][3:])
    return off <= now_min < on if off <= on else (now_min >= off or now_min < on)


MANUAL_OFF = "/run/eo1-manual-off"   # the owner switched the screen off by hand: the schedule's morning leaves it off


def manual_screen(on):
    """The screen switched by the owner (control page, live remote, the EO1's button). Off by hand while it was on
    stays off past the next scheduled morning; on by hand lifts that."""
    if on:
        try:
            os.remove(MANUAL_OFF)
        except OSError:
            pass
    elif screen_is_on():
        open(MANUAL_OFF, "w").close()
    screen(on)


def schedule_thread():
    """The screen's own on/off times (no server needed). Acts only when a window starts or ends, so a hand on
    the screen button in between is respected until the next change, and a screen switched off by hand stays
    off when a window ends (until it is switched on again). Waits for a believable clock."""
    last = None
    while True:
        SCHEDULE_WAKE.wait(20)
        SCHEDULE_WAKE.clear()
        if time.localtime().tm_year < 2025:
            continue
        try:
            sch = load_settings().get("schedule", {})
            t = time.localtime()
            now_min = t.tm_hour * 60 + t.tm_min
            off = bool(sch.get("enabled")) and any(in_window(now_min, w) for w in sch.get("windows", []))
            if off != last:
                if (last is not None or off) and (off or not os.path.exists(MANUAL_OFF)):
                    screen(not off)                   # at start only a dark window acts (the EO1 starts on)
                last = off
        except Exception:  # noqa: BLE001 - a bad window in a hand-edited settings.json must not stop the schedule
            pass


def night_thread():
    """Apply the night cap when the evening starts and lift it in the morning (checked every minute)."""
    last = None
    while True:
        cap = night_cap()
        if cap != last and screen_is_on() and not AUTO["on"]:
            set_brightness(STATE["brightness"] or 255)
            last = cap
        time.sleep(60)


OFF_MODE = "/run/eo1-off-mode"      # "sleep" (backlight off, default) or "dim" (lowest level)
DIMMED = "/run/eo1-dimmed"          # exists while the screen is "off" in dim mode


def off_mode():
    try:
        return open(OFF_MODE).read().strip() or "sleep"
    except OSError:
        return "sleep"


def screen(on):
    """Serialised: the buttons, the server and the app may switch at the same moment."""
    if not on:
        SPLASH_NOW.set()
    with SCREEN_LOCK:
        dim = not on and off_mode() == "dim"
        for b in backlights():
            _write(os.path.join(b, "bl_power"), 0 if on or dim else 4)
        if on:
            try:
                os.unlink(DIMMED)
            except OSError:
                pass
            set_brightness(STATE["brightness"] or 200)
        elif dim:
            open(DIMMED, "w").close()
            write_level(1)
        else:
            write_level(0)
    pid = native_player()                       # the player pauses its slideshow while the screen is off
    if pid:
        try:
            os.kill(pid, signal.SIGUSR1)
        except OSError:
            pass


def screen_is_on():
    """The truth is the backlight itself (bl_power 0 = on), not a variable that a restart would reset; a
    dimmed screen (screenOffMode dim) counts as off."""
    if os.path.exists(DIMMED):
        return False
    for b in backlights():
        try:
            return open(os.path.join(b, "bl_power")).read().strip() == "0"
        except OSError:
            continue
    return True


def current_brightness():
    for b in backlights():
        try:
            mx = int(open(os.path.join(b, "max_brightness")).read())
            cur = int(open(os.path.join(b, "brightness")).read())
            return round(cur * 255 / max(1, mx))
        except (OSError, ValueError):
            continue
    return None


# ------------------------------------------------------------------ Chromium DevTools
PLAYER_PID = "/run/eo1-player.pid"
PLAYER_STATUS = "/run/eo1-player.json"


def native_player():
    """The native player (eo1-player) is running: its main PID from systemd (not the pid file: after a stop the
    number can belong to another process, and SIGUSR1 kills most processes)."""
    if not os.path.exists("/run/eo1-player-native"):
        return None
    try:
        out = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", "eo1-player"], capture_output=True,
                             text=True, timeout=5).stdout.strip()
        pid = int(out or 0)
        return pid or None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def player_status():
    try:
        return json.load(open(PLAYER_STATUS))
    except (OSError, ValueError):
        return {}


def devtools_page():
    """The frame page target: boot.html hosts the server's frame page in an iframe (so the last picture stays
    until the first one has faded in), hence the iframe target whose URL is the server's /frame wins; else the
    top page (boot.html itself before the server answered)."""
    try:
        with urllib.request.urlopen(DEVTOOLS + "/json", timeout=2) as r:
            targets = json.load(r)
    except Exception:  # noqa
        return None
    server = conf().get("server", "")
    for t in targets:
        if t.get("type") in ("page", "iframe") and server and t.get("url", "").startswith(server.split("/frame")[0]):
            return t
    for t in targets:
        if t.get("type") == "page":
            return t
    return None


def devtools(method, params=None):
    import websocket  # python3-websocket
    page = devtools_page()
    if not page:
        raise RuntimeError("browser not running")
    # Chromium >= 111 rejects DevTools WebSockets that carry an Origin header unless --remote-allow-origins is set
    ws = websocket.create_connection(page["webSocketDebuggerUrl"], timeout=5, suppress_origin=True)
    try:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 1:
                return msg
    finally:
        ws.close()


def js(expr):
    return devtools("Runtime.evaluate", {"expression": expr})


# ------------------------------------------------------------------ screenshot
def kiosk_env():
    try:
        uid = int(subprocess.run(["id", "-u", KIOSK_USER], capture_output=True, text=True).stdout.strip())
    except ValueError:
        uid = 1000
    rt = f"/run/user/{uid}"
    sock = "wayland-0"
    try:
        for n in os.listdir(rt):
            if n.startswith("wayland-") and not n.endswith(".lock"):
                sock = n
                break
    except OSError:
        pass
    return {"XDG_RUNTIME_DIR": rt, "WAYLAND_DISPLAY": sock}


def screenshot_png():
    env = kiosk_env()
    cmd = ["runuser", "-u", KIOSK_USER, "--", "env", f"XDG_RUNTIME_DIR={env['XDG_RUNTIME_DIR']}",
           f"WAYLAND_DISPLAY={env['WAYLAND_DISPLAY']}", "grim", "-"]
    r = subprocess.run(cmd, capture_output=True, timeout=20)
    if r.returncode != 0 or not r.stdout:
        raise RuntimeError("grim failed: " + r.stderr.decode(errors="replace")[:200])
    return r.stdout


def panel_shot(w, quality, jpeg, c):
    """Real screenshot of the panel via eo1-grab (debian/build/eo1-grab.c): the scanned-out framebuffer as a
    small RGB copy, turned to the viewer's orientation. w = width of the result (viewer side)."""
    from PIL import Image
    w = max(32, min(1080, w))
    scale = max(1, min(8, 1080 // w))
    path = "/run/eo1-shot-%d" % threading.get_ident()    # the control page and the live remote overlap
    r = subprocess.run(["/usr/local/bin/eo1-grab", path, "1", "0", str(scale)], capture_output=True, timeout=20)
    if r.returncode != 0:
        raise RuntimeError("eo1-grab failed")
    try:
        blob = open(path + "-000.grab", "rb").read()
    finally:
        try:
            os.unlink(path + "-000.grab")
        except OSError:
            pass
    nl = blob.index(b"\n")
    head = blob[:nl].decode().split()
    if head[1] != "RGB3":                       # scale 1: raw formats are not converted here
        raise RuntimeError("unexpected grab format " + head[1])
    gw, gh, pitch = int(head[2]), int(head[3]), int(head[4])
    im = Image.frombytes("RGB", (gw, gh), blob[nl + 1:nl + 1 + gh * pitch], "raw", "RGB", pitch)
    rot = int(c.get("rotate", "270") or 0) % 360
    if rot:
        im = im.rotate(rot, expand=True)
    im = im.resize((w, max(1, round(im.height * w / im.width))))
    buf = io.BytesIO()
    if jpeg:
        im.save(buf, "JPEG", quality=max(10, min(95, quality)))
    else:
        im.save(buf, "PNG")
    return buf.getvalue()


# ------------------------------------------------------------------ Wi-Fi (NetworkManager)
def nm(*args, timeout=20):
    return sh(["nmcli", "-t", *args], timeout=timeout)


def wifi_status():
    if not eo1wifi.nm_mode():
        return eo1wifi.status()
    _, out, _ = nm("-f", "ACTIVE,SSID,SIGNAL", "dev", "wifi")
    ssid, sig = None, None
    for line in out.splitlines():
        p = line.split(":")
        if len(p) >= 3 and p[0] == "yes":
            ssid, sig = p[1], p[2]
    _, ipout, _ = sh("ip -4 -o addr show")
    ips = [l.split()[3].split("/")[0] for l in ipout.splitlines() if " lo " not in l]
    return {"connected": ssid is not None, "ssid": ssid, "signal": int(sig) if sig and sig.isdigit() else None,
            "ip": ips[0] if ips else None}


def server_reachable(url):
    if not url:                                  # no server configured
        return False
    try:
        p = urllib.parse.urlparse(url)
        with urllib.request.urlopen(f"{p.scheme}://{p.netloc}/healthz", timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa
        return False


# ------------------------------------------------------------------ HTTP
class H(BaseHTTPRequestHandler):
    server_version = "eo1-agent"
    timeout = 30          # a half-open connection must not hold a handler thread forever

    def log_message(self, fmt, *a):  # quiet; journald keeps errors only
        pass

    def reply(self, status, text, extra=None, ctype="application/json", body=None):
        if body is None:
            o = dict(extra or {})
            o["status"] = "OK" if status == 200 else "Error"
            o["statustext"] = text
            body = json.dumps(o).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.reply(204, "", body=b"")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        u = urllib.parse.urlparse(self.path)
        m = re.fullmatch(r"/pair/(\d{4})", u.path)
        if m:                                     # a computer sends its public key with the one-time code
            body = self.rfile.read(n).decode(errors="replace") if 0 < n <= 8192 else ""
            if not pairing.check(m.group(1), use=True):
                return self.reply(403, "That code is not valid (any more): start again on the control page")
            ok, msg = pairing.add(body)
            return self.reply(200 if ok else 400, msg)
        if u.path == "/upload":                   # a picture from the control page: the raw file as the body
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            c = conf()
            if c.get("token") and q.get("token", "") != c["token"]:
                return self.reply(403, "Invalid token")
            if not 0 < n <= 40_000_000:
                return self.reply(413, "picture too large (40 MB max)")
            data = self.rfile.read(n)
            try:
                e = save_upload(data, q.get("name", ""))
                end_welcome()
                return self.reply(200, "Saved: " + e["title"], {"upload": e})
            except Exception as ex:  # noqa - not a picture, storage missing
                return self.reply(400, "Could not save the picture: %s" % ex)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode(errors="replace")) if 0 < n < 65536 else {}
        self.handle_cmd({k: v[0] for k, v in form.items()})

    def do_GET(self):
        self.handle_cmd({})

    def handle_cmd(self, extra):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        q.update(extra)
        c = conf()
        cmd = q.get("cmd", "")
        if u.path == "/bootstatus":   # local boot page, no token (read-only)
            w = wifi_status()
            return self.reply(200, "boot", {"wifi": w, "server": c["server"], "serverOk": server_reachable(c["server"]),
                                            "hostname": socket.gethostname(), "version": VERSION})
        m = re.fullmatch(r"/(\d{4})", u.path)
        if m:                                     # the one-command setup script for a computer
            ua = self.headers.get("User-Agent", "")
            ps = "PowerShell" in ua or "Windows" in ua
            if not pairing.check(m.group(1)):
                # 200 with a message: an HTTP error looks like a crash in PowerShell
                return self.reply(200, "", ctype="text/plain; charset=utf-8", body=pairing.invalid_script(ps).encode())
            return self.reply(200, "", ctype="text/plain; charset=utf-8",
                              body=pairing.script(m.group(1), wifi_status().get("ip") or "", ps).encode())
        m = re.fullmatch(r"/pair/(\d{4})/info", u.path)
        if m:                                     # the /pc page checks the code and shows the commands
            if not pairing.check(m.group(1)):
                return self.reply(403, "That code is not valid (any more)")
            return self.reply(200, "ok", {"commands": pairing.commands(m.group(1), wifi_status().get("ip") or "")})
        if not cmd and (u.path == "/pc" or (u.path == "/" and self.server.server_port == 80)):   # plain eo1.local too
            try:
                return self.reply(200, "", ctype="text/html; charset=utf-8", body=open(PC_PAGE, "rb").read())
            except OSError:
                pass
        if u.path in PAGE_FILES:
            path, ctype = PAGE_FILES[u.path]
            try:
                return self.reply(200, "", ctype=ctype, body=open(path, "rb").read())
            except OSError:
                return self.reply(404, "Not found")
        if u.path.startswith("/fonts/"):         # the title fonts, so the control page shows each in its own face
            name = os.path.basename(u.path)
            if name in ("Inter-Medium.ttf", "Jost-Medium.ttf", "Fraunces-Soft.ttf"):
                try:
                    body = open("/usr/share/fonts/truetype/eo1bian/" + name, "rb").read()
                    return self.reply(200, "", ctype="font/ttf", body=body)
                except OSError:
                    pass
            return self.reply(404, "No such font")
        if not cmd and u.path == "/setup":         # the setup Wi-Fi's page (commands need the token)
            try:
                return self.reply(200, "", ctype="text/html; charset=utf-8", body=open(SETUP_PAGE, "rb").read())
            except OSError:
                pass
        if not cmd and u.path in ("/", "/control"):   # the frame's own control page (commands need the token)
            try:
                body = open(CONTROL_PAGE, "rb").read()
                return self.reply(200, "", ctype="text/html; charset=utf-8", body=body)
            except OSError:
                pass
        if not cmd:
            return self.reply(200, f"EO1 agent {VERSION} - use ?cmd=...&token=...")
        if c.get("token") and q.get("token", "") != c["token"]:
            return self.reply(403, "Invalid token")
        if cmd in WELCOME_ENDS:
            end_welcome()
        try:
            return self.run(cmd, q, c)
        except Exception as e:  # noqa
            return self.reply(500, f"Error: {e}")

    def run(self, cmd, q, c):
        if cmd == "version":
            return self.reply(200, f"{VERSION}", {"package": "eo1-agent", "versionName": VERSION, "versionCode": 0, "os": "debian"})
        if cmd in ("deviceInfo", "info"):
            try:
                w = wifi_status()
            except Exception:  # noqa - info must answer even when nmcli is slow
                w = {"ssid": None, "signal": None, "ip": None}
            try:
                page = devtools_page()
            except Exception:  # noqa - or when Chromium is busy/restarting
                page = None
            if not page and native_player():
                ps = player_status()
                page = {"url": c["server"] + "#native:" + str(ps.get("id", "")), "native": ps}
            return self.reply(200, "info", {
                "app": "eo1-agent", "os": "debian", "version": VERSION, "screenOn": screen_is_on(),
                "lux": AUTO["lux"], "autoBrightness": AUTO["on"], "autoBrightnessCurve": AUTO["curve"],
                "brightness": current_brightness(), "backlights": [os.path.basename(b) for b in backlights()],
                "backlightWritable": bool(backlights()), "wifiSsid": w["ssid"], "wifiSignal": w["signal"], "ip": w["ip"],
                "pageUrl": page.get("url") if page else None, "pageHello": bool(page),
                "uptime": round(float(open("/proc/uptime").read().split()[0])), "hostname": socket.gethostname()})
        if cmd == "screenOn":
            manual_screen(True)
            return self.reply(200, "Screen on")
        if cmd == "screenOff":
            manual_screen(False)
            return self.reply(200, "Screen off")
        if cmd == "setBrightness":
            if AUTO["on"]:                        # a hand on the slider means: no automatic level now
                AUTO["on"] = False
                auto_save()
            done = set_brightness(int(q.get("value", "128")))
            return self.reply(200, f"Brightness {q.get('value')} ({', '.join(done) or 'no backlight'})")
        if cmd in ("next", "prev") and (c.get("source") == "github" or not server_reachable(c["server"])):
            player_local_cmd(cmd)                 # the frame's own slideshow: no server involved
            return self.reply(200, cmd.capitalize())
        if cmd == "status":
            ps = player_status() or {}
            try:
                gal = os.path.join("/media/eo1gallery", open("/media/eo1gallery/active").read().strip())
                n_gal = len(json.load(open(os.path.join(gal, "manifest.json"))).get("items", []))
            except (OSError, ValueError):
                n_gal = 0
            ntp = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], capture_output=True,
                                 text=True, timeout=5).stdout.strip() == "yes"
            return self.reply(200, "status", {
                "showing": ps, "screenOn": screen_is_on(), "brightness": current_brightness(),
                "wanted": STATE["brightness"], "nightCap": night_cap(), "auto": AUTO["on"],
                "autoCurve": AUTO["curve"], "lux": AUTO["lux"],
                "autoMin": AUTO["min"], "autoMax": AUTO["max"], "autoOffset": AUTO["offset"],
                "schedule": load_settings().get("schedule", {"enabled": False, "windows": []}),
                "interval": load_settings().get("interval", 0), "offMode": off_mode(), "source": c.get("source", "auto"), "serverSet": bool(c.get("server")),
                "githubRepo": c.get("github_repo", ""), "galleryItems": n_gal,
                "immich": {"url": c.get("immich_url", ""), "albums": len([a for a in c.get("immich_albums", "").split(",") if a]),
                           "albumIds": [a for a in c.get("immich_albums", "").split(",") if a],
                           "key": os.path.exists(IMMICH_KEY)}, "uploads": uploads_index(),
                "hidden": hidden_list(),
                "titles": bool((lambda: json.load(open("/media/eo1gallery/settings.json")))().get("titles"))
                if os.path.exists("/media/eo1gallery/settings.json") else False,
                "titleFont": (lambda: json.load(open("/media/eo1gallery/settings.json")))().get("font", "sans")
                if os.path.exists("/media/eo1gallery/settings.json") else "sans",
                "edges": load_settings().get("edges", "blur"),
                "shuffle": load_settings().get("shuffle"),
                "remote": ts_status(),
                **albums_info(),
                "liveRemote": {k: v for k, v in ntfyremote.info().items() if k != "link"},
                "wifi": wifi_status(), "time": int(time.time() * 1000), "ntp": ntp, "version": VERSION,
                "uptime": round(float(open("/proc/uptime").read().split()[0])), "temp": cpu_temp(),
                "setupAp": os.path.exists("/run/eo1-setup-ap.json"), "storage": gallery_space(),
                "fromStick": "eo1root=" not in open("/proc/cmdline").read(),
                "install": read_install(),
                "hostname": socket.gethostname()})
        if cmd == "uploadThumb":                 # small preview for the control page
            from PIL import Image
            uid = q.get("id", "")
            if not uid.isdigit():
                return self.reply(400, "id")
            im = Image.open(os.path.join(UPLOADS, uid + ".jpg"))
            im.thumbnail((240, 427))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=75)
            return self.reply(200, "", ctype="image/jpeg", body=buf.getvalue())
        if cmd == "titles":
            # titles on the pictures (lower left, TV screensaver style): kept on the frame
            path = "/media/eo1gallery/settings.json"
            try:
                cur = json.load(open(path, encoding="utf-8"))
            except (OSError, ValueError):
                cur = {}
            if q.get("font"):                      # the title font: sans (Inter), eo (Jost) or serif (Fraunces)
                if q["font"] not in ("sans", "eo", "serif"):
                    return self.reply(400, "font: sans, eo or serif")
                cur["font"] = q["font"]
            elif q.get("edges"):                   # around pictures that do not fill the panel: blur, mat or black
                if q["edges"] not in ("blur", "mat", "black"):
                    return self.reply(400, "edges: blur, mat or black")
                cur["edges"] = q["edges"]
            else:
                cur["titles"] = q.get("value", "on").lower() in ("on", "1", "true", "yes")
            with open(path + ".tmp", "w", encoding="utf-8") as f:
                json.dump(cur, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(path + ".tmp", path)
            player_local_cmd("refresh")           # the picture on screen again, with or without its title
            return self.reply(200, "Titles %s, font %s" % ("on" if cur.get("titles") else "off", cur.get("font", "sans")))
        if cmd in ("hide", "unhide"):
            # hide an item of the frame's own slideshow (GitHub gallery, server copy, uploads) without deleting it
            # anywhere: kept on the frame, works offline. id = the player's item id ("local-..."), or the
            # current one when none is given.
            iid = q.get("id") or str((player_status() or {}).get("id", ""))
            if not iid.startswith("local-"):
                return self.reply(400, "only items of the frame's own slideshow can be hidden here")
            items = [e for e in hidden_list() if e["id"] != iid]
            if cmd == "hide":
                title = q.get("title") or item_titles().get(iid) or (player_status() or {}).get("title") or ""
                items.append({"id": iid, "title": title, "at": int(time.time())})
            hidden_save(items)
            if cmd == "hide" and str((player_status() or {}).get("id")) == iid:
                player_local_cmd("next")          # away from it at once
            return self.reply(200, "Hidden" if cmd == "hide" else "Shown again")
        if cmd == "shuffle":
            # the slideshow's order: shuffled (on) or the gallery's own order (off)
            on = q.get("value", "on").lower() in ("on", "1", "true", "yes")
            settings_set(shuffle=on)
            return self.reply(200, "Shuffle " + ("on" if on else "off"))
        if cmd == "show":
            # show one picture now (from the picture browser); its album plays when it was not playing
            iid = q.get("id", "")
            found = [i for i in album_items() if i[0] == iid]
            if not found:
                return self.reply(404, "no such picture")
            with SETTINGS_LOCK:
                cur = load_settings()
                on = cur.get("albums") or []
                if on and found[0][2] not in on:
                    cur["albums"] = on + [found[0][2]]
                    save_settings(cur)
            hid = hidden_list()
            if any(e["id"] == iid for e in hid):      # written only when it was hidden: nothing else needs storage
                hidden_save([e for e in hid if e["id"] != iid])
            raw = iid[len("local-"):]
            # the player picks up a changed album choice on its next look, then shows the picture
            threading.Timer(0.3, player_local_cmd, args=("show:" + raw,)).start()
            return self.reply(200, "Showing " + (found[0][1] or "the picture"))
        if cmd == "albumSheet":
            # the small pictures of one album in one JPEG (the live remote's picture browser)
            if q.get("album") is None:
                return self.reply(400, "album: an album name")
            return self.reply(200, "Sheet", album_sheet(q["album"]))
        if cmd == "albums":
            # which albums the frame plays: value = a JSON list of album keys ("Your pictures" = the phone's), or "all"
            v = q.get("value", "all")
            try:
                chosen = [] if v == "all" else json.loads(v)
                if not isinstance(chosen, list) or not all(isinstance(x, str) for x in chosen):
                    raise ValueError
            except ValueError:
                return self.reply(400, 'value: "all" or a JSON list of album names')
            settings_set(albums=chosen)
            player_local_cmd("next")              # on to a picture of the chosen albums at once
            return self.reply(200, "Playing " + (", ".join(chosen) if chosen else "all albums"), albums_info())
        if cmd == "deleteUpload":
            with UPLOADS_LOCK:
                items = uploads_index()
                keep = [e for e in items if e["id"] != q.get("id")]
                if len(keep) == len(items):
                    return self.reply(404, "no such picture")
                try:
                    os.remove(os.path.join(UPLOADS, q.get("id") + ".jpg"))
                except OSError:
                    pass
                uploads_save(keep)
            return self.reply(200, "Deleted")
        if cmd == "setTime":
            # the phone's clock, when the frame has no network time (no RTC driver, internet down after a
            # power cut): the night cap and schedules need the time
            try:
                t = int(q.get("value", "0")) / 1000.0
            except ValueError:
                return self.reply(400, "value: milliseconds since 1970")
            ntp = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"], capture_output=True,
                                 text=True, timeout=5).stdout.strip() == "yes"
            tz = q.get("tz", "")
            # the phone's time zone, when eo1.txt does not set one: kept in state/timezone for the next boots
            if tz and not conf().get("timezone") and re.fullmatch(r"[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}", tz) \
                    and os.path.exists("/usr/share/zoneinfo/" + tz) and os.path.realpath("/etc/localtime") != \
                    os.path.realpath("/usr/share/zoneinfo/" + tz):
                sh(["timedatectl", "set-timezone", tz])
                with open("/run/eo1-timezone", "w") as f:
                    f.write(tz)
                sh(["eo1-stick-write", "--put", "/run/eo1-timezone", STICK + "/state/timezone"])
            if not ntp and abs(t - time.time()) > 60:
                sh(["date", "-s", "@%d" % int(t)])
                return self.reply(200, "Clock set from the phone")
            return self.reply(200, "Clock already right" if ntp else "Clock within a minute")
        if cmd in ("next", "prev"):
            # with a server the server owns the playlist and moves it, then the player polls at once
            base = c["server"].split("/frame")[0]
            req = urllib.request.Request(f"{base}/api/now/{cmd}", data=b"", method="POST")
            urllib.request.urlopen(req, timeout=8).read()
            pid = native_player()
            if pid:
                os.kill(pid, signal.SIGUSR1)
            return self.reply(200, cmd.capitalize())
        if cmd == "screenOffMode":
            mode = "dim" if q.get("value", "sleep").lower() == "dim" else "sleep"
            with open(OFF_MODE, "w") as f:
                f.write(mode)
            if not screen_is_on():                # off right now: switch to the new kind of off
                screen(False)
            return self.reply(200, "Screen off mode: " + mode)
        if cmd == "autoBrightness":
            ranged = any(k in q for k in ("min", "max", "offset"))
            if "value" in q or not ranged:        # only the range sent: on/off stays as it is
                AUTO["on"] = q.get("value", "on").lower() in ("on", "1", "true", "yes")
            if q.get("curve") in ("low", "default", "high"):
                AUTO["curve"] = q["curve"]
            rng = {}
            for k, lo, hi in (("min", 0, 100), ("max", 1, 100), ("offset", -50, 50)):
                if q.get(k) not in (None, ""):
                    try:
                        rng[k] = min(max(int(float(q[k])), lo), hi)
                    except ValueError:
                        return self.reply(400, k + " must be a number")
            if rng:                               # the range is kept across reboots
                AUTO.update(rng)
                settings_set(auto={k: AUTO[k] for k in ("min", "max", "offset")})
                if "value" not in q:
                    return self.reply(200, "Automatic brightness range %d-%d %%, adjust %+d %%" % (
                        AUTO["min"], AUTO["max"], AUTO["offset"]))
            auto_save()
            if not AUTO["on"]:
                set_brightness(STATE["brightness"] or 255)
            return self.reply(200, "Automatic brightness %s (%s curve, %s lux now)" % (
                "on" if AUTO["on"] else "off", AUTO["curve"], AUTO["lux"]))
        if cmd == "installEmmc":
            # value=check: can EO1bian be installed here (running from the stick)? value=start&confirm=yes: install
            from_stick = "eo1root=" not in open("/proc/cmdline").read()
            emmc = os.path.exists("/dev/disk/by-label/DATA") or os.path.exists("/dev/disk/by-label/eo1emmc")
            android = os.path.exists("/dev/disk/by-label/DATA")
            if q.get("value", "check") == "check":
                return self.reply(200, "Install", {"possible": from_stick and emmc, "fromStick": from_stick,
                                                   "android": android})
            if not (from_stick and emmc):
                return self.reply(409, "Install works only while the EO1 runs from the USB stick")
            if q.get("confirm") != "yes":
                return self.reply(400, "confirm=yes needed")
            if os.path.exists("/run/eo1-install.json") and not json.load(open("/run/eo1-install.json")).get("done"):
                return self.reply(409, "Already installing")
            subprocess.Popen(["eo1-install-run"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self.reply(200, "Installing: watch the EO1's screen (about 10 minutes)")
        if cmd == "update":
            # value=check: is there a newer release? value=apply: install it (trial boot, automatic rollback)
            v = q.get("value", "check")
            if v not in ("check", "apply"):
                return self.reply(400, "value: check or apply")
            r = subprocess.run(["eo1-update", v], capture_output=True, text=True, timeout=600)
            try:
                d = json.loads(r.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                d = {"error": (r.stderr or "no answer").strip()[-200:]}
            if d.get("error"):
                return self.reply(502, "Update: " + d["error"], d)
            if v == "check":
                return self.reply(200, ("Version %s is available" % d["latest"]) if d["available"]
                                  else "Up to date (%s)" % d["current"], d)
            return self.reply(200 if d.get("ok") else 409, d.get("message", ""), d)
        if cmd == "pairStart":
            # connect a computer: a one-time code (10 minutes) and the commands that use it
            code, expires = pairing.start()
            ip = wifi_status().get("ip") or ""
            host, local, _ = pairing.addresses(ip)
            return self.reply(200, "Code " + code, {"code": code, "expires": expires, "pcUrl": "http://" + local,
                                                     "pcUrlIp": "http://%s:2324/pc" % ip,
                                                     "commands": pairing.commands(code, ip), "host": host})
        if cmd == "liveRemote":
            # the live remote through ntfy: value=pair (a new key: an older phone link stops working), link, off.
            # pair with server=https://... (and ntfytoken=tk_... for a server with access tokens) moves to that server;
            # pair without a server keeps the current one and its token.
            v = q.get("value", "link")
            if v == "pair":
                server = q.get("server", "").strip().rstrip("/") or None
                token = q.get("ntfytoken", "").strip() or None
                if server and not re.fullmatch(r"https://[A-Za-z0-9.-]+(:\d+)?(/[A-Za-z0-9._/-]*)?", server):
                    return self.reply(400, "server: an https:// address")
                if token and (not server or not ntfyremote.TOKEN_RE.match(token)):
                    return self.reply(400, "ntfytoken: an ntfy access token (tk_...), with its server")
                ntfyremote.pair(server, token)
            elif v == "off":
                ntfyremote.unpair()
                return self.reply(200, "Live remote off", {"liveRemote": ntfyremote.info()})
            elif v != "link":
                return self.reply(400, "value: pair, link or off")
            info = ntfyremote.info()
            if not info.get("paired"):
                return self.reply(404, "Not paired", {"liveRemote": info})
            if q.get("qr"):
                return self.reply(200, "", ctype="image/png", body=qr_png(info["link"]))
            return self.reply(200, "Live remote ready", {"liveRemote": info})
        if cmd == "remoteAccess":
            # live control from anywhere through Tailscale: value=on (sign-in link), off, forget (sign out)
            v = q.get("value", "on")
            if v not in ("on", "off", "forget"):
                return self.reply(400, "value: on, off or forget")
            st = ts_set(v)
            return self.reply(200, {"on": "Remote access starting", "off": "Remote access off",
                                    "forget": "Remote access off and signed out"}[v], {"remote": st})
        if cmd == "sshKeys":
            return self.reply(200, "Keys", {"keys": pairing.listing(), "sshOn": subprocess.run(
                ["systemctl", "is-active", "ssh.service"], capture_output=True, text=True).stdout.strip() == "active"})
        if cmd == "sshRemove":
            try:
                ok = pairing.remove(int(q.get("index", "-1")))
            except ValueError:
                ok = False
            return self.reply(200 if ok else 400, "Computer removed" if ok else "No such computer")
        if cmd == "showAddress":
            # the control page's address and QR code on the screen for a minute (a 4 s press on the button)
            w = wifi_status()
            if not w.get("ip"):
                return self.reply(400, "No network")
            with open("/run/eo1-setup-done.json.tmp", "w") as f:
                json.dump({"ssid": w.get("ssid") or "", "ip": w["ip"], "url": "http://%s:2324/" % w["ip"],
                           "at": time.time(), "title": "Control this EO1"}, f)
            os.replace("/run/eo1-setup-done.json.tmp", "/run/eo1-setup-done.json")
            player_local_cmd("")
            return self.reply(200, "Address shown on the screen")
        if cmd == "setupAp":
            # the EO1's own setup Wi-Fi: value=start (the EO1 leaves your network!) or stop
            v = q.get("value", "start")
            if v not in ("start", "stop"):
                return self.reply(400, "value: start or stop")
            subprocess.Popen(["eo1-setup-ap", v], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self.reply(200, "Setup Wi-Fi " + ("starting: join it from your phone" if v == "start" else "stopping"))
        if cmd == "setupScan":
            try:
                nets = json.load(open("/run/eo1-ap/scan.json"))
            except (OSError, ValueError):
                nets = []
            return self.reply(200, "Scan", {"networks": nets})
        if cmd == "setupWifi":
            ssid, psk = q.get("ssid", ""), q.get("psk", "")
            if not os.path.exists("/run/eo1-setup-ap.json"):
                return self.reply(400, "The setup Wi-Fi is not running")
            ok, msg = eo1wifi.add(ssid, psk, connect=False)
            if not ok:
                return self.reply(400, msg)
            subprocess.Popen(["eo1-setup-ap", "try", ssid], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return self.reply(200, "Connecting to " + ssid)
        if cmd == "schedule":
            # screen off in up to two windows a day, e.g. off=23:00 on=07:00 (and off2/on2); enabled=on|off
            wins = None
            if "off" in q or "off2" in q:
                wins = []
                for a, b in (("off", "on"), ("off2", "on2")):
                    if q.get(a) and q.get(b):
                        for t in (q[a], q[b]):
                            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                                return self.reply(400, "times are HH:MM")
                        wins.append({"off": q[a], "on": q[b]})
            with SETTINGS_LOCK:
                cur = load_settings()
                sch = cur.get("schedule", {"enabled": False, "windows": []})
                if "enabled" in q:
                    sch["enabled"] = q["enabled"].lower() in ("on", "1", "true", "yes")
                if wins is not None:
                    sch["windows"] = wins
                cur["schedule"] = sch
                save_settings(cur)
            SCHEDULE_WAKE.set()
            w = ", ".join("off %s-%s" % (x["off"], x["on"]) for x in sch["windows"]) or "no times"
            return self.reply(200, "Schedule %s (%s)" % ("on" if sch.get("enabled") else "off", w))
        if cmd == "interval":
            # how long each picture stays in the EO1's own slideshow, in seconds (0 = the gallery's own times,
            # "keep" = the picture stays until next/previous)
            if q.get("value") == "keep":
                v = -1
            else:
                try:
                    v = max(0, min(int(q.get("value", "0")), 7 * 86400))
                except ValueError:
                    return self.reply(400, "value: seconds or keep")
                if 0 < v < 10:
                    v = 10
            settings_set(interval=v)
            player_local_cmd("refresh")
            if v < 0:
                return self.reply(200, "Keeps this picture")
            return self.reply(200, "Each picture: %s" % ("as set in the gallery" if not v else
                                                         "%d min" % (v // 60) if v % 3600 else "%d h" % (v // 3600)))
        if cmd == "setUrl":
            return self.reply(200, "The native player has no start URL (ignored)")
        if cmd == "poll":
            pid = native_player()
            if pid:
                os.kill(pid, signal.SIGUSR1)
                return self.reply(200, "Poll")
            try:
                js("window.eo1Poll&&eo1Poll()")
            except Exception as e:  # noqa - Chromium busy or restarting: the page polls by itself anyway
                return self.reply(503, f"Browser busy, page will poll by itself ({e.__class__.__name__})")
            return self.reply(200, "Poll")
        if cmd in ("reload", "loadStartURL") and native_player():
            sh("systemctl restart eo1-player")
            return self.reply(200, "Restarting player")
        if cmd in ("reload", "loadStartURL"):
            devtools("Page.navigate", {"url": c["server"]})
            return self.reply(200, "Loading " + c["server"])
        if cmd in ("loadURL", "loadUrl"):
            url = q.get("url")
            if not url:
                return self.reply(400, "Missing url")
            devtools("Page.navigate", {"url": url})
            return self.reply(200, "Loading " + url)
        if cmd == "getSnapshot":                 # a full-size screenshot
            cmd = "getScreenshot"
        if cmd in ("getScreenshot", "screenshot") and native_player():
            # what the panel really shows, read back from the display controller (eo1-grab), video included
            try:
                return self.reply(200, "", ctype="image/jpeg" if cmd == "screenshot" else "image/png",
                                  body=panel_shot(int(q.get("w", "270" if cmd == "screenshot" else "1080")),
                                                  int(q.get("q", "60")), cmd == "screenshot", c))
            except Exception:  # noqa - fall back to the server's idea of the picture (not for the frame's own slideshow)
                if c.get("source") == "github":
                    return self.reply(503, "no screenshot right now")
            # fallback: the picture the player shows, from the server (videos: their poster)
            ps = player_status()
            base = c["server"].split("/frame")[0]
            with urllib.request.urlopen(f"{base}/api/frame/state?scr=1080x1920", timeout=8) as r:
                item = (json.load(r) or {}).get("item") or {}
            url = item.get("url") if item.get("type") in (None, "image") else item.get("poster")
            if not url:
                return self.reply(404, "nothing to show")
            with urllib.request.urlopen(base + url, timeout=15) as r:
                data = r.read()
            from PIL import Image
            im = Image.open(io.BytesIO(data)).convert("RGB")
            w = max(32, min(1920, int(q.get("w", "270"))))
            im = im.resize((w, max(1, round(im.height * w / im.width))))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=max(10, min(95, int(q.get("q", "60")))))
            return self.reply(200, "", ctype="image/jpeg", body=buf.getvalue())
        if cmd in ("getScreenshot", "screenshot"):
            png = screenshot_png()
            if cmd == "screenshot":
                from PIL import Image
                im = Image.open(io.BytesIO(png)).convert("RGB")
                w = max(32, min(1920, int(q.get("w", "270"))))
                im = im.resize((w, max(1, round(im.height * w / im.width))))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=max(10, min(95, int(q.get("q", "60")))))
                return self.reply(200, "", ctype="image/jpeg", body=buf.getvalue())
            return self.reply(200, "", ctype="image/png", body=png)
        if cmd == "wifiStatus":
            return self.reply(200, "Wi-Fi status", wifi_status())
        if cmd == "wifiScan":
            if not eo1wifi.nm_mode():
                return self.reply(200, "Scan", {"networks": eo1wifi.scan()})
            _, out, _ = nm("-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes", timeout=30)
            nets, seen = [], set()
            for line in out.splitlines():
                p = line.split(":")
                if len(p) >= 3 and p[0] and p[0] not in seen:
                    seen.add(p[0])
                    nets.append({"ssid": p[0], "signal": int(p[1]) if p[1].isdigit() else None,
                                 "security": "open" if not p[2] else "wpa"})
            return self.reply(200, "Scan", {"networks": nets})
        if cmd == "gallery":
            # GitHub gallery settings from the app: repo, branch, source; the read token goes to the boot
            # partition's state/ (never into a URL, never into eo1.txt). state/gallery.conf survives reboots.
            if self.command != "POST":
                return self.reply(405, "use POST (the token must not be in a URL)")
            cur = conf()
            repo = q.get("repo", cur.get("github_repo", "")).strip()
            branch = (q.get("branch") or cur.get("github_branch") or "eo1").strip()
            source = q.get("source", cur.get("source", "auto")).strip()
            if source not in ("auto", "server", "github"):
                return self.reply(400, "source: auto, server or github")
            if repo and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
                return self.reply(400, "repo: owner/name")
            token = q.get("github_token", "").strip()   # not "token": that is the agent's own access word
            if token:
                try:
                    put_secret(token, STICK + "/state/github-token")
                except RuntimeError as e:
                    return self.reply(500, "the token: %s" % e)
            # live: the same keys in eo1.conf, the player restarted when the source changed, a sync now
            save_gallery_conf(github_repo=repo, github_branch=branch, source=source)
            if source != cur.get("source", "auto"):
                sh("systemctl restart eo1-player")
            if repo:
                sh("systemctl start --no-block eo1-gallery-sync.service")
            return self.reply(200, "Gallery %s (%s), source %s%s" % (repo or "none", branch, source,
                                                                   ", token saved" if token else ""))
        if cmd in ("immich", "immichAlbums"):
            # Immich: the server, its API key (POST only: never in a URL) and the albums to show.
            # immichAlbums lists the server's albums (with a new key, or the saved one); immich saves the choice;
            # immich with off=1 stops using Immich (the GitHub gallery, if set up, plays again)
            if self.command != "POST":
                return self.reply(405, "use POST (the API key must not be in a URL)")
            if cmd == "immich" and q.get("off") == "1":
                save_gallery_conf(immich_url="", immich_albums="")
                sh("systemctl start --no-block eo1-gallery-sync.service")
                return self.reply(200, "Immich is off")
            url = (q.get("url") or c.get("immich_url", "")).strip().rstrip("/")
            if not IMMICH_URL.fullmatch(url):
                return self.reply(400, "The Immich address, e.g. http://192.168.1.20:2283")
            key = q.get("immich_key", "").strip()
            if not key and os.path.exists(IMMICH_KEY):
                key = open(IMMICH_KEY).read().strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]{20,100}", key):
                return self.reply(400, "The API key from Immich (Account settings > API keys)")
            try:
                albums = immich_albums(url, key)
            except urllib.error.HTTPError as e:
                return self.reply(400, "Immich said %d: %s" % (e.code, "wrong API key, or it may not read albums"
                                                               if e.code in (401, 403) else e.reason))
            except Exception as e:  # noqa: BLE001
                return self.reply(400, "No answer from %s (%s)" % (url, str(e)[:80]))
            if cmd == "immichAlbums":
                return self.reply(200, "%d albums" % len(albums), {"albums": albums})
            chosen = [a for a in q.get("albums", "").split(",") if a.strip()]
            if not chosen or not all(IMMICH_ID.fullmatch(a) for a in chosen):
                return self.reply(400, "Pick at least one album")
            known = {a["id"]: a["name"] for a in albums}
            if any(a not in known for a in chosen):
                return self.reply(400, "That album is not on the server (any more)")
            if q.get("immich_key", "").strip():
                try:
                    put_secret(key, IMMICH_KEY)
                except RuntimeError as e:
                    return self.reply(500, "the API key: %s" % e)
            save_gallery_conf(immich_url=url, immich_albums=",".join(chosen))
            with SETTINGS_LOCK:                  # only some albums play: the Immich ones just picked play too
                cur = load_settings()
                on = cur.get("albums") or []
                if on and any(known[a] not in on for a in chosen):
                    cur["albums"] = on + [known[a] for a in chosen if known[a] not in on]
                    save_settings(cur)
            sh("systemctl start --no-block eo1-gallery-sync.service")
            return self.reply(200, "Immich: %s - the pictures arrive within a few minutes"
                              % ", ".join(known[a] for a in chosen))
        if cmd == "wifiAdd":
            if self.command != "POST":
                return self.reply(405, "use POST (the password must not be in a URL)")
            ok, msg = add_wifi(q.get("ssid", ""), q.get("psk", ""), q.get("connect") == "1")
            return self.reply(200 if ok else 400, msg)
        if cmd == "restartApp":
            unit = "eo1-player" if os.path.exists("/run/eo1-player-native") else "eo1-kiosk"
            sh("systemctl restart " + unit)
            return self.reply(200, "Restarting " + unit)
        if cmd == "log":
            _, out, _ = sh("journalctl -b --no-pager -n 300", timeout=20)
            return self.reply(200, "", ctype="text/plain; charset=utf-8", body=out.encode())
        return self.reply(400, "Unknown cmd: " + cmd)


def add_wifi(ssid, psk, connect=True):
    if not eo1wifi.nm_mode():
        return eo1wifi.add(ssid, psk, connect)
    if not ssid or len(ssid) > 32:
        return False, "ssid must be 1-32 characters"
    if psk and not 8 <= len(psk) <= 63:
        return False, "WPA password must be 8-63 characters"
    nm("con", "delete", "id", ssid)
    args = ["con", "add", "type", "wifi", "ifname", "*", "con-name", ssid, "ssid", ssid,
            "connection.autoconnect", "yes", "connection.autoconnect-priority", "1" if ssid.lower() == "eo1" else "10"]
    if psk:
        args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", psk]
    rc, out, err = nm(*args)
    if rc != 0:
        return False, (err or out).strip()[:200]
    if connect:
        nm("con", "up", "id", ssid, timeout=45)
    return True, f"Network {ssid} saved"


# ------------------------------------------------------------------ boot splash
FB_W, FB_H = 1920, 1080          # the panel as U-Boot/the framebuffer see it (landscape)
# s between splash writes to the boot partition. Each write is ~3.3 MB (splash.fb.gz + last.jpg), the only
# regular write to the eMMC: every 5 min with a changing slideshow was up to ~950 MB a day. Every 30 min plus
# right when the screen goes off (SPLASH_NOW) is ~160 MB a day at most, and the boot picture is still recent.
SPLASH_MIN_INTERVAL = 1800


def splash_images(jpeg_bytes, rotate):
    """The picture as the frame shows it (fit: contain on black) in three forms:
    bmp  - 8-bit BMP on the raw landscape framebuffer, rotated the way cage rotates the desktop (clockwise
           degrees), for U-Boot's "bmp display" ~2 s after power-on;
    fb   - the same as raw little-endian RGB565 (what imx-drm's fb0 uses), gzipped, for the initramfs;
    jpeg - upright, as the desktop shows it, for boot.html while Chromium waits for the server."""
    import gzip
    from PIL import Image, ImageOps
    im = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    r = int(rotate or 0) % 360
    desk = (FB_H, FB_W) if r in (90, 270) else (FB_W, FB_H)
    canvas = ImageOps.pad(im, desk, color=(0, 0, 0))
    fb = canvas.rotate((360 - r) % 360, expand=True) if r else canvas
    bmp = io.BytesIO()
    fb.convert("P", palette=Image.ADAPTIVE, colors=256).save(bmp, "BMP")
    raw = gzip.compress(fb.convert("BGR;16").tobytes(), compresslevel=6)
    jpeg = io.BytesIO()
    canvas.save(jpeg, "JPEG", quality=88)
    # upright, 32-bit BGRA (DRM_FORMAT_ARGB8888 little-endian) for the patched cage: shown as the compositor's
    # background until the browser's first frame, so the panel never goes black between the initramfs splash
    # and Chromium
    rgba = gzip.compress(canvas.convert("RGBA").tobytes("raw", "BGRA"), compresslevel=6)
    return {"bmp": bmp.getvalue(), "fb": raw, "jpeg": jpeg.getvalue(), "rgba": rgba}


def splash_thread():
    """Keep state/splash.bmp on the stick = the picture currently on the frame, so the next boot shows it ~2 s
    after power-on (6x_bootscript). Asks the server what the frame shows; never touches the browser."""
    last_url, last_write = None, 0.0
    time.sleep(120)       # the image work (quantize + gzip, ~5 s CPU) must not compete with the boot
    while True:
        now_wanted = SPLASH_NOW.wait(20)
        SPLASH_NOW.clear()
        if now_wanted:
            time.sleep(120)
            now_wanted = not screen_is_on()
        try:
            c = conf()
            if not os.path.isdir(STICK + "/state"):
                continue
            base = c["server"].split("/frame")[0]
            with urllib.request.urlopen(f"{base}/api/frame/state?scr={FB_H}x{FB_W}", timeout=5) as r:
                st = json.load(r)
            item = st.get("item") or {}
            # a video or GIF: its poster frame is what the frame shows first, so that is the splash
            url = item.get("url") if item.get("type") in (None, "image") else item.get("poster")
            if not url or st.get("blank"):
                continue
            if url == last_url or (not now_wanted and time.time() - last_write < SPLASH_MIN_INTERVAL):
                continue
            with urllib.request.urlopen(base + url, timeout=20) as r:
                data = r.read()
            imgs = splash_images(data, c.get("rotate", "270"))
            # no splash.bmp any more: the EO1 U-Boot never shows it (2 MB on a 20 MB boot partition)
            names = {"fb": "splash.fb.gz", "jpeg": "last.jpg", "rgba": "splash.rgba.gz"}
            stale = ""
            if native_player():
                # the cage background is only for player=browser; 4 MB the 20 MB boot partition cannot spare
                del names["rgba"]
                stale = f"rm -f {STICK}/state/splash.rgba.gz; "
            for k, n in names.items():
                with open("/run/" + n, "wb") as f:
                    f.write(imgs[k])
            script = stale + " && ".join(f"cp -f /run/{n} {STICK}/state/{n}.tmp && mv -f {STICK}/state/{n}.tmp {STICK}/state/{n}"
                                 for n in names.values())
            rc, _, err = sh(["eo1-stick-write", "sh", "-c", script], timeout=120)
            if rc == 0:
                last_url, last_write = url, time.time()
            else:  # e.g. the partition was full: never leave half-written .tmp files behind
                sh(["eo1-stick-write", "sh", "-c", f"rm -f {STICK}/state/*.tmp"], timeout=60)
        except Exception:  # noqa - the splash is cosmetic; never let it kill the agent
            pass


def keys_thread():
    """EO1 capacitive keys (MPR121) and any other key: short press toggles the screen like the Android app.
    Only active when an evdev keyboard-type device exists; harmless otherwise."""
    try:
        import evdev
    except ImportError:
        return
    seen = set()
    while True:
        try:
            for path in evdev.list_devices():
                if path in seen:
                    continue
                dev = evdev.InputDevice(path)
                name = (dev.name or "").lower()
                if "mpr121" in name or "touchkey" in name or "gpio-keys" in name:
                    seen.add(path)
                    threading.Thread(target=key_loop, args=(dev,), daemon=True).start()
        except Exception:  # noqa
            pass
        time.sleep(10)


def key_loop(dev):
    import evdev
    down = {}
    for ev in dev.read_loop():
        if ev.type != evdev.ecodes.EV_KEY:
            continue
        if ev.value == 1:
            down[ev.code] = time.time()
        elif ev.value == 0 and ev.code in down:
            held = time.time() - down.pop(ev.code)
            if held < 0.8:
                manual_screen(not screen_is_on())
            else:
                try:
                    js("window.eo1Poll&&eo1Poll()")
                except Exception:  # noqa
                    pass


def server_asleep(c):
    """True when the server's schedule says the frame should be dark right now (quiet hours)."""
    try:
        base = c["server"].split("/frame")[0]
        with urllib.request.urlopen(f"{base}/api/schedule", timeout=4) as r:
            return bool(json.load(r).get("asleep"))
    except Exception:  # noqa
        return False


def ensure_screen_at_start():
    """A boot or an agent restart must not leave the panel dark: on, unless the server says it is sleep time.
    Retries for a while because the server may not be reachable yet (Wi-Fi comes up after the agent)."""
    if os.path.exists(MANUAL_OFF):
        return                                   # switched off by hand: an agent restart must not light it
    for _ in range(30):
        c = conf()
        if server_reachable(c["server"]):
            screen(not server_asleep(c))     # quiet hours: dark again (the initramfs lit the panel with the splash)
            return
        time.sleep(2)
    screen(True)


def clock_thread():
    """No RTC driver: after a power cut the clock is wrong until NTP syncs, and NTP needs DNS and the internet.
    The night brightness cap and the logs need the time, so take it from the server's HTTP Date header
    whenever NTP has not synced (checked after 60 s, then every 10 min until it has)."""
    time.sleep(60)
    while True:
        try:
            synced = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
                                    capture_output=True, text=True, timeout=5).stdout.strip() == "yes"
            if synced:
                return
            base = conf()["server"].split("/frame")[0]
            with urllib.request.urlopen(base + "/healthz", timeout=5) as r:
                hdr = r.headers.get("Date")
            if hdr:
                import email.utils
                t = email.utils.parsedate_to_datetime(hdr).timestamp()
                if abs(t - time.time()) > 60:
                    subprocess.run(["date", "-s", "@%d" % int(t)], capture_output=True, timeout=5)
                    print("clock set from the server: %s" % hdr, flush=True)
        except Exception:  # noqa - try again later
            pass
        time.sleep(600)


def main():
    try:
        STATE["brightness"] = int(open(WANTED).read().strip())
    except (OSError, ValueError):
        STATE["brightness"] = 255          # boot: full (the night cap still applies), not the initramfs' value
    threading.Thread(target=ensure_screen_at_start, daemon=True).start()
    threading.Thread(target=keys_thread, daemon=True).start()
    threading.Thread(target=splash_thread, daemon=True).start()
    threading.Thread(target=night_thread, daemon=True).start()
    threading.Thread(target=clock_thread, daemon=True).start()
    threading.Thread(target=auto_thread, daemon=True).start()
    threading.Thread(target=schedule_thread, daemon=True).start()
    threading.Thread(target=welcome_upload_thread, daemon=True).start()
    # the live remote: started a little later, once this agent answers on its own port
    threading.Timer(5, ntfyremote.start, args=(local_api, lambda w: panel_shot(w, 70, True, conf()))).start()

    def port80():                             # the short forms: eo1.local/pc and eo1.local/<code> (no :2324)
        try:
            ThreadingHTTPServer(("0.0.0.0", 80), H).serve_forever()
        except OSError:
            pass
    threading.Thread(target=port80, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()


if __name__ == "__main__":
    main()
