#!/usr/bin/env python3
"""eo1-player on eo1-layers: native picture/video player for the EO1 where one process owns the panel.

Same contract as frame.html and player.py: polls the server's /api/frame/state?scr=1080x1920 and shows `item`,
honours `blank`, `transition` (cross-fade seconds), `poll_ms` and `remaining_ms`. The display side is
eo1-layers (debian/build/eo1-layers.c): the i.MX6 display processor's two layers with a hardware alpha
(kernel patch debian/build/kernel-ipu-alpha.py), so every switch is a cross-fade of two live sources - a video
keeps moving while it fades out, the next one already moves while it fades in - with no plane switched on or
off and no CPU copy of video frames.

  images  - decoded with Pillow, padded and rotated to the panel's raw orientation, handed over as raw BGRX
            (eo1-layers converts them to YUYV, the one format both layers keep)
  video   - the server's native variant (exactly 1920x1080 landscape H.264), VPU-decoded, looped seamlessly
  GIF     - played as its video variant

Started by player-start when eo1-layers exists and eo1.conf does not say compositor=classic (player.py with
eo1-show + eo1-video is the fallback). SIGUSR1 = poll now. Writes /run/eo1-player.json.
"""
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request

from PIL import Image, ImageOps

PANEL_W, PANEL_H = 1920, 1080          # raw panel (landscape)
CONF = "/etc/eo1/eo1.conf"
LAST = "/media/eo1usb/state/last.jpg"
STATUS = "/run/eo1-player.json"
CACHE_DIR = "/tmp/eo1-player"
LAYERS = "/usr/local/bin/eo1-layers"
GALLERY_ROOT = "/media/eo1gallery"              # copies made by gallery-sync.py; "active" names the one to play


UPLOADS = os.path.join(GALLERY_ROOT, "uploads")   # pictures sent from the phone (the frame's control page)
PLAYER_CMD = "/run/eo1-player-cmd"                # next / prev from the control page
PHONE_ALBUM = "Your pictures"                    # the album of the pictures uploaded from the phone
HIDDEN = os.path.join(GALLERY_ROOT, "hidden.json")  # items hidden from the phone (agent cmd=hide)
SETTINGS = os.path.join(GALLERY_ROOT, "settings.json")  # display settings from the control page (titles on/off)
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
TITLE_FONTS = "/usr/share/fonts/truetype/eo1bian/"   # open (OFL) fonts, built by debian/build/fonts.py
FONTS = {                                          # title font choices on the control page: (font, fallback)
    "sans": ("Inter-Medium.ttf", FONT),                                  # Moderne
    "eo": ("Jost-Medium.ttf", FONT),                                     # EO-stil, close to the original Brown
    "serif": ("Fraunces-Soft.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"),  # Klassisk, Souvenir-like
    "bold": ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", FONT),  # older setting, no longer offered
}
_coverage = None


def title_font(key, text):
    """The font file for the chosen title font, or its DejaVu fallback when the title has a character the font
    lacks (Cyrillic in Fraunces, for example) or the font is missing."""
    global _coverage
    name, fallback = FONTS.get(key or "sans", FONTS["sans"])
    path = os.path.join(TITLE_FONTS, name)
    if not os.path.exists(path):
        return fallback
    if _coverage is None:
        try:
            _coverage = json.load(open(TITLE_FONTS + "coverage.json"))
        except (OSError, ValueError):
            _coverage = {}
    ranges = _coverage.get(name)
    if ranges is None:
        return path
    for ch in set(text) - {" "}:
        c = ord(ch)
        if not any(a <= c <= b for a, b in ranges):
            return fallback
    return path


def display_settings():
    try:
        return json.load(open(SETTINGS, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def fill(im, view, edges="blur"):
    """Fit a picture into the panel (contain) and fill the free space: "blur" = the picture itself, enlarged,
    blurred and dimmed behind it (like a TV's ambient mode), "mat" = a dark mat in the picture's own colour with
    a margin all round (a passe-partout), "black" = plain black bars. A picture that already fills the panel
    is only scaled. Cheap on the A9: the blur is done on a tiny copy and scaled up."""
    from PIL import ImageEnhance, ImageFilter, ImageStat
    w, h = view
    s = min(w / im.width, h / im.height)
    fw, fh = max(1, round(im.width * s)), max(1, round(im.height * s))
    if edges == "black" or (w - fw <= 4 and h - fh <= 4):
        return ImageOps.pad(im, view, color=(0, 0, 0), method=Image.BICUBIC)
    if edges == "mat":
        mean = ImageStat.Stat(im.resize((32, 32))).mean
        color = tuple(int(c * 0.22 + 14) for c in mean[:3])          # the picture's colour, very dark
        m = 0.9                                                       # 5 % mat on each side
        pic = im.resize((max(1, round(fw * m)), max(1, round(fh * m))), Image.LANCZOS, reducing_gap=3.0)
        out = Image.new("RGB", view, color)
        x, y = (w - pic.width) // 2, (h - pic.height) // 2
        edge = tuple(min(255, int(c * 1.6 + 10)) for c in color)     # a fine bevel line round the opening
        out.paste(Image.new("RGB", (pic.width + 4, pic.height + 4), edge), (x - 2, y - 2))
        out.paste(pic, (x, y))
        return out
    bg = ImageOps.fit(im, (max(1, w // 24), max(1, h // 24)), Image.BILINEAR)
    bg = bg.filter(ImageFilter.GaussianBlur(1.5)).resize(view, Image.BICUBIC)
    bg = ImageEnhance.Brightness(bg).enhance(0.45)
    pic = im.resize((fw, fh), Image.LANCZOS, reducing_gap=3.0) if (fw, fh) != im.size else im
    bg.paste(pic, ((w - fw) // 2, (h - fh) // 2))
    return bg


def caption(im, title, font_path=FONT):
    """The title centred at the bottom over a soft dark gradient (TV screensaver style, centred like a plaque), always
    on one line: the font shrinks until the title fits (down to a still readable size), and only a title too long
    even then is shortened with an ellipsis. im is the picture as the viewer sees it (portrait); drawn before the
    turn into the panel's orientation."""
    from PIL import ImageDraw, ImageFont
    w, h = im.size
    text = " ".join(title.split())
    x = w // 17
    room = w - 2 * x
    big, small = max(24, w // 27), max(18, w // 48)     # 40 px down to 22 px on the 1080 px wide panel view
    try:
        size = big
        font = ImageFont.truetype(font_path, size)
        while size > small and font.getlength(text) > room:
            size -= 1
            font = ImageFont.truetype(font_path, size)
    except OSError:
        return im
    while font.getlength(text) > room and len(text) > 4:
        text = text.rstrip("…")[:-2].rstrip() + "…"
    from PIL import ImageFilter, ImageStat
    y = h - h // 22
    band = h // 6
    # how bright the picture is behind the text decides how strong the gradient gets: discreet on dark pictures,
    # firm enough on white ones that white text still reads
    tw = int(font.getlength(text))
    x = (w - tw) // 2                                # centred, like a plaque under a painting
    lum = ImageStat.Stat(im.crop((x, y - size - 10, min(w, x + tw + 10), min(h, y + 12))).convert("L")).mean[0]
    peak = int(120 + 110 * max(0.0, (lum - 90) / 165))          # 120 (dark) .. 230 (white) of 255
    im = im.convert("RGBA")
    shade = Image.new("L", (1, 256))
    for yy in range(256):                              # 0 at the top of the band, peak at the bottom
        shade.putpixel((0, yy), int(peak * (yy / 255) ** 1.4))
    grad = Image.new("RGBA", (w, band), (0, 0, 0, 0))
    grad.putalpha(shade.resize((w, band)))
    im.alpha_composite(grad, (0, h - band))
    # a soft glow behind the letters (blurred dark copy), like a TV's screensaver captions
    pad = 3 * max(3, size // 7)                     # blur only the strip around the text (fast on the A9)
    gx, gy = max(0, x - pad), max(0, y - size - pad)
    glow = Image.new("RGBA", (min(w, x + tw + pad) - gx, min(h, y + pad) - gy), (0, 0, 0, 0))
    ImageDraw.Draw(glow).text((x - gx, y - gy), text, font=font, fill=(0, 0, 0, 200), anchor="ls")
    glow = glow.filter(ImageFilter.GaussianBlur(max(3, size // 7)))
    im.alpha_composite(glow, (gx, gy))
    im.alpha_composite(glow, (gx, gy))
    ImageDraw.Draw(im).text((x, y), text, font=font, fill=(255, 255, 255, 240), anchor="ls")
    return im.convert("RGB")


def gallery_dir():
    try:
        name = open(os.path.join(GALLERY_ROOT, "active")).read().strip()
    except OSError:
        name = "eo1-gallery"
    return os.path.join(GALLERY_ROOT, name or "eo1-gallery")


class Gallery:
    """The frame's own slideshow from the GitHub gallery: the same state the server would send (item,
    transition, remaining_ms), so apply() shows it unchanged. Items are local files (file:// URLs)."""

    def __init__(self):
        self.mtime = None
        self.dir = gallery_dir()
        self.items, self.order, self.pos, self.started = [], [], 0, 0.0
        self.settings = {}

    def origin(self):
        return "the GitHub gallery" if "github" in self.dir else "the server's playlist"

    def load(self):
        self.dir = gallery_dir()
        path = os.path.join(self.dir, "manifest.json")
        upath = os.path.join(UPLOADS, "index.json")
        m = tuple(os.path.getmtime(x) if os.path.exists(x) else 0 for x in (path, upath, HIDDEN, SETTINGS))
        if m[:2] == (0, 0):
            self.items = []
            return False
        if m != self.mtime:
            self.mtime = m
            d = {}
            try:
                d = json.load(open(path, encoding="utf-8"))
            except (OSError, ValueError):
                pass
            self.items = d.get("items", [])
            # pictures from the phone join the slideshow (absolute paths: they live outside the gallery copy)
            try:
                for e in json.load(open(upath, encoding="utf-8")):
                    self.items.append({"id": "up-" + e["id"], "type": "image", "title": e.get("title"),
                                       "album": PHONE_ALBUM,
                                       "duration": d.get("settings", {}).get("image_duration", 60),
                                       "file": os.path.join(UPLOADS, e["id"] + ".jpg")})
            except (OSError, ValueError, KeyError):
                pass
            self.settings = d.get("settings", {})
            self.mode = d.get("mode", "")
            try:
                hidden = {e["id"] for e in json.load(open(HIDDEN, encoding="utf-8"))}
            except (OSError, ValueError, KeyError):
                hidden = set()
            self.items = [it for it in self.items if "local-" + it["id"] not in hidden]
            # the albums to play: chosen on the control page (settings.json), else the gallery's default; none = all
            chosen = display_settings().get("albums")
            if chosen is None:
                chosen = self.settings.get("albums") or []
            if chosen:
                picked = [it for it in self.items if it.get("album", "") in chosen]
                if picked:
                    self.items = picked
                    self._album_miss = False
                else:
                    # the chosen album was renamed or removed: playing nothing forever helps nobody
                    if not getattr(self, "_album_miss", False):
                        self._album_miss = True
                        log("albums %s match no pictures: playing everything instead" % (chosen,))
            ids = [it["id"] for it in self.items]
            shuffle = self.shuffle()
            # a new order when the pictures are not the same set any more (pictures added too, not only removed)
            # or shuffle was switched; the picture on screen keeps its place when it is still there
            if set(self.order) != set(ids) or shuffle != getattr(self, "shuffled", None):
                current = self.order[self.pos % len(self.order)] if self.order else None
                self.order = ids[:]
                if shuffle:
                    import random
                    random.shuffle(self.order)
                self.shuffled = shuffle
                if current in self.order:
                    self.order.remove(current)
                    self.order.insert(0, current)
                    self.pos = 0
                else:
                    self.pos, self.started = 0, 0.0
        return bool(self.items)

    def shuffle(self):
        """Shuffle: the control page's choice (settings.json), else the gallery's own setting (default on)."""
        v = display_settings().get("shuffle")
        return bool(self.settings.get("shuffle", True) if v is None else v)

    def state(self):
        if not self.load():
            return None
        byid = {it["id"]: it for it in self.items}
        self.order = [i for i in self.order if i in byid] or list(byid)
        now = time.time()
        step = 0
        try:                                       # next / prev / show:<id> from the control page or the remote
            c = open(PLAYER_CMD).read().strip()
            step = {"next": 1, "prev": -1}.get(c, 0)
            os.remove(PLAYER_CMD)
            if c.startswith("show:") and c[5:] in self.order:
                self.pos, self.started = self.order.index(c[5:]), 0.0
        except OSError:
            pass
        if step:
            self.pos = (self.pos + step) % len(self.order)
            self.started = 0.0
        iv = int(display_settings().get("interval") or 0)     # the control page's "each picture" time
        keep = iv < 0                              # "keep this picture": it stays until next/prev or another choice
        dur = lambda it: 3600 if keep else (iv or it.get("duration", 60))
        cur = byid[self.order[self.pos % len(self.order)]]
        if self.started and not keep and now - self.started >= dur(cur):
            self.pos = (self.pos + 1) % len(self.order)
            if self.pos == 0 and self.shuffle() and len(self.order) > 2:
                import random
                last = self.order[-1]
                random.shuffle(self.order)
                if self.order[0] == last:                 # never the same item twice in a row
                    self.order.append(self.order.pop(0))
            self.started = 0.0
            cur = byid[self.order[self.pos]]
        if not self.started:
            self.started = now
        f = lambda rel: "file://" + (rel if rel.startswith("/") else os.path.join(self.dir, rel))
        item = {"id": "local-" + cur["id"], "type": cur["type"], "title": cur.get("title"), "url": f(cur["file"])}
        if cur["type"] == "video":
            item["native"] = {"url": f(cur["file"]), "rotate": 90}
            item["poster"] = f(cur["poster"]) if cur.get("poster") else None
        nxt = byid[self.order[(self.pos + 1) % len(self.order)]]
        return {"item": item, "transition": self.settings.get("transition", 3), "poll_ms": 5000,
                "remaining_ms": int(max(0, dur(cur) - (now - self.started)) * 1000),
                "next": {"type": nxt["type"], "url": f(nxt["file"])}, "blank": False}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def card(title, lines, qrs=()):
    """A full-screen message picture (portrait 1080x1920): a title, lines of text, and QR codes with captions,
    for the setup Wi-Fi, a fresh connection and an EO1 with nothing to show yet."""
    from PIL import ImageDraw, ImageFont
    sys.path.insert(0, "/usr/local/lib/eo1")
    import qrcodegen
    w, h = 1080, 1920
    im = Image.new("RGB", (w, h), (20, 17, 15))
    d = ImageDraw.Draw(im)
    font = lambda size: ImageFont.truetype(title_font("sans", ""), size)
    y = 210
    d.text((w // 2, y), title, font=font(64), fill=(243, 236, 228), anchor="ma")
    y += 130
    for ln in lines:
        f = font(40)
        color = (168, 156, 144) if ln.startswith("(") else (240, 162, 74) if ln.startswith("!") else (243, 236, 228)
        d.text((w // 2, y), ln.lstrip("!"), font=f, fill=color, anchor="ma")   # "!" = highlighted line
        y += 62
    y += 50
    for text, caption, *above in qrs:            # optional third item: lines drawn right above this QR code
        for ln in (above[0] if above else ()):
            d.text((w // 2, y), ln, font=font(40), fill=(243, 236, 228), anchor="ma")
            y += 62
        if above:
            y += 16
        q = qrcodegen.QrCode.encode_text(text, qrcodegen.QrCode.Ecc.MEDIUM)
        n = q.get_size()
        scale = min(14, 420 // (n + 8))
        side = (n + 8) * scale
        x0 = (w - side) // 2
        d.rectangle((x0, y, x0 + side, y + side), fill=(255, 255, 255))
        for yy in range(n):
            for xx in range(n):
                if q.get_module(xx, yy):
                    d.rectangle((x0 + (xx + 4) * scale, y + (yy + 4) * scale,
                                 x0 + (xx + 5) * scale - 1, y + (yy + 5) * scale - 1), fill=(0, 0, 0))
        y += side + 24
        if caption:
            d.text((w // 2, y), caption, font=font(34), fill=(240, 162, 74), anchor="ma")
            y += 50
        y += 40
    return im


def setup_message():
    """What the screen should say instead of art: ("key", picture) or None."""
    try:
        st = json.load(open("/run/eo1-install.json"))
        if st.get("error"):
            return ("install|err", card("Not installed", ["Nothing was changed on the EO1's own memory", "beyond what the log shows.",
                                                          "", "The log is in logs/ on the USB stick.", "",
                                                          st["error"][:60]]))
        if st.get("done"):
            art = st.get("art")
            if art:                                # the rescued Electric Objects art: say where it is, plainly
                note = ["!Your %d Electric Objects artwork%s" % (art, "" if art == 1 else "s"), "!are saved in eo-art/ on this USB stick.",
                        "Copy them to your computer:", "this is the only copy."]
            elif art == 0:
                note = ["(no Electric Objects art was left on this EO1)"]
            else:
                note = []
            return ("install|done", card("EO1bian is installed", ["", "1. Remove the USB stick", "2. Unplug the power",
                                                                 "3. Plug the power back in", ""] + note))
        bar = "\u25a0" * st["step"] + "\u25a1" * (st["total"] - st["step"])
        return ("install|%s" % st["step"], card("Installing EO1bian", [st["text"], "", bar, "",
                                                                       "Keep the power and the stick in.",
                                                                       "(about 10 minutes)"]))
    except (OSError, ValueError, KeyError):
        pass
    try:
        st = json.load(open("/run/eo1-setup-ap.json"))
        lines = ["!First turn off mobile data on your phone"]
        if st.get("message"):
            lines = [st["message"], ""] + lines
        return ("setup|%s|%s" % (st["ssid"], st.get("message")),
                card("Set up this EO1", lines,
                     [("WIFI:T:WPA;S:%s;P:%s;;" % (st["ssid"], st["code"]), "",
                       ["1. Join the Wi-Fi " + st["ssid"], "password " + st["code"]]),
                      (st["url"] + "#token=" + conf().get("token", "eo1"), "",
                       ["2. Open the setup page", st["url"].replace("http://", "")])]))
    except (OSError, ValueError, KeyError):
        pass
    try:
        st = json.load(open("/run/eo1-setup-done.json"))
        if time.time() - st["at"] < 90:
            head = ["to " + st["ssid"], ""] if st.get("ssid") and not st.get("title") else []
            return ("done|%s" % st["at"], card(st.get("title") or "Connected",
                                               head + ["Open on your phone:" if st.get("title") else "Control this EO1 at",
                                                       st["url"].replace("http://", "")]
                                               + ([] if st.get("title") else ["", "(mobile data can go back on now)"]),
                                               [(st["url"] + "#token=" + conf().get("token", "eo1"),
                                                 "scan to open the control page")]))
        if not st.get("title") and not st.get("welcomeDone"):
            # after the first setup: the plain EO1bian picture until the owner does something on the control page
            # (agent end_welcome), also when there are pictures already (no QR code: the phone is there by now)
            return ("welcome|%s" % st["at"], welcome(""))
    except (OSError, ValueError, KeyError):
        pass
    return None




LOGO = "/usr/share/eo1/eo1bian-main.png"          # the EO1bian picture shown before there are any of yours


def welcome(ip):
    """Nothing to show yet (no gallery, no server): the EO1bian picture, and at the bottom how to add pictures
    (the control page's address and QR code on a soft dark band). Falls back to a plain card without the file."""
    url = "http://%s:2324/" % ip if ip else None
    try:
        im = Image.open(LOGO).convert("RGB")
    except OSError:
        lines = ["Nothing to show yet.", "", "Add pictures from your phone", "on the EO1's control page."]
        return card("EO1 is ready", lines + ([""] + [url.replace("http://", "")] if url else []),
                    [(url + "#token=" + conf().get("token", "eo1"), "scan to open the control page")] if url else [])
    if not url:
        return im
    from PIL import ImageDraw, ImageFont
    sys.path.insert(0, "/usr/local/lib/eo1")
    import qrcodegen
    band = Image.new("L", (1, 256))
    for yy in range(256):                                  # transparent at the top, ~80 % black at the bottom
        band.putpixel((0, yy), int(205 * (yy / 255) ** 1.3))
    shade = Image.new("RGBA", (im.width, 560), (0, 0, 0, 0))
    shade.putalpha(band.resize((im.width, 560)))
    im = im.convert("RGBA")
    im.alpha_composite(shade, (0, im.height - 560))
    im = im.convert("RGB")
    d = ImageDraw.Draw(im)
    font = lambda size: ImageFont.truetype(title_font("sans", ""), size)
    w = im.width
    q = qrcodegen.QrCode.encode_text(url + "#token=" + conf().get("token", "eo1"), qrcodegen.QrCode.Ecc.MEDIUM)
    n = q.get_size()
    scale = 230 // (n + 6)
    side = (n + 6) * scale
    x0, y0 = (w - side) // 2, 1560
    d.rounded_rectangle((x0, y0, x0 + side, y0 + side), radius=14, fill=(255, 255, 255))
    for yy in range(n):
        for xx in range(n):
            if q.get_module(xx, yy):
                d.rectangle((x0 + (xx + 3) * scale, y0 + (yy + 3) * scale,
                             x0 + (xx + 4) * scale - 1, y0 + (yy + 4) * scale - 1), fill=(0, 0, 0))
    d.text((w // 2, y0 - 34), "Add pictures from your phone", font=font(36), fill=(236, 232, 245), anchor="ms")
    d.text((w // 2, y0 + side + 52), url.replace("http://", "").rstrip("/"), font=font(30), fill=(168, 164, 186), anchor="ms")
    return im


def screen_off():
    """The screen is off (the agent's screenOff: backlight off, or dimmed to the lowest level in dim mode)."""
    if os.path.exists("/run/eo1-dimmed"):
        return True
    try:
        for b in os.listdir("/sys/class/backlight"):
            return open(os.path.join("/sys/class/backlight", b, "bl_power")).read().strip() != "0"
    except OSError:
        pass
    return False


def notify(msg):
    """sd_notify without the systemd module (WatchdogSec= in eo1-player.service)."""
    sock = os.environ.get("NOTIFY_SOCKET")
    if not sock:
        return
    import socket
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        s.connect("\0" + sock[1:] if sock.startswith("@") else sock)
        s.send(msg.encode())
        s.close()
    except OSError:
        pass


def fb_write(im):
    """The picture on screen also into fb0 (imx-drm's fbdev, RGB565), in the background: fb0 is what the panel
    shows for a moment whenever eo1-layers is restarted (player restart, watchdog). Without this it showed
    whatever an older player had left there (seen: an old Flower Power poster flashing up)."""
    def run():
        try:
            data = im.convert("BGR;16").tobytes()
            with open("/dev/fb0", "r+b") as f:
                f.write(data)
        except Exception:  # noqa: BLE001 - cosmetic
            pass
    threading.Thread(target=run, daemon=True).start()


def uptime():
    try:
        return float(open("/proc/uptime").read().split()[0])
    except (OSError, ValueError):
        return 0.0


def conf():
    c = {"server": "", "rotate": "270"}
    try:
        for line in open(CONF, encoding="utf-8"):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                c[k.strip()] = v.strip()
    except OSError:
        pass
    return c


class Panel:
    """eo1-layers owns the display. Commands on its stdin: "image <file.raw> <fade_ms>" and
    "video <file.mp4> <fade_ms>"; it answers "ok <ms>" once the new source is fully up, "error <text>" when it
    could not be shown (the previous source stays)."""

    def __init__(self, rotate):
        self.rotate = int(rotate or 0) % 360
        self.proc = None
        self.current = None              # what is on screen: ("image", PIL image) or ("video", path)
        self.n = 0
        self.lock = threading.Lock()

    def to_panel(self, im, title=None, font=FONT, edges="black"):
        """A picture as the viewer sees it (fit contain, the free space filled per edges, optional title) -> raw
        panel frame orientation."""
        r = self.rotate
        view = (PANEL_H, PANEL_W) if r in (90, 270) else (PANEL_W, PANEL_H)
        im = fill(im.convert("RGB"), view, edges)
        if title:
            im = caption(im, title, font)
        return im.rotate((360 - r) % 360, expand=True) if r else im

    def _raw(self, im):
        self.n += 1
        path = os.path.join(CACHE_DIR, "frame%d.raw" % (self.n % 2))
        with open(path, "wb") as f:
            f.write(im.tobytes("raw", "BGRX"))
        return path

    def start(self):
        if self.proc and self.proc.poll() is None:
            return
        self.proc = subprocess.Popen([LAYERS], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        line = self.proc.stdout.readline().strip()
        log("eo1-layers:", line or "did not start")
        if self.current is not None:     # restarted: put back what was on screen, no fade
            kind, what = self.current
            self._cmd("%s %s 0" % (kind, self._raw(what) if kind == "image" else what))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write("quit\n")
                self.proc.stdin.flush()
                self.proc.wait(5)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        self.proc = None

    def _cmd(self, line, timeout=60):
        """one command, one reply - with a timeout, so a hung eo1-layers cannot hang the player"""
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        out = []
        t = threading.Thread(target=lambda: out.append(self.proc.stdout.readline()), daemon=True)
        t.start()
        t.join(timeout)
        if t.is_alive():
            log("eo1-layers did not answer in %d s: restarting it" % timeout)
            self.proc.kill()
            return ""
        return (out[0] if out else "").strip()

    def _send(self, kind, what, arg, fade_s):
        with self.lock:
            self.start()
            fade_ms = int(fade_s * 1000) if self.current is not None else 0
            try:
                reply = self._cmd("%s %s %d" % (kind, arg, fade_ms))
            except Exception as e:  # noqa: BLE001 - it died: restart at the next show
                log("eo1-layers failed:", e)
                self.stop()
                return False
            if not reply.startswith("ok"):
                log("eo1-layers:", reply or "no reply")
                if not reply:
                    self.stop()
                return False
            if fade_ms:
                log("fade %s:" % kind, reply)
            self.current = (kind, what)
            return True

    def show(self, im, fade_s):
        """Cross-fade from what is on screen to a picture (already in panel orientation)."""
        if self._send("image", im, self._raw(im), fade_s):
            fb_write(im)
            return True
        return False

    def video(self, path, fade_s):
        """Cross-fade from what is on screen to a looping video."""
        return self._send("video", path, path, fade_s)


class Player:
    def __init__(self):
        c = conf()
        self.base = c["server"].split("/frame")[0]
        self.panel = Panel(c.get("rotate", "270"))
        self.key = None
        self.paused = None                 # time the screen went off: the slideshow waits until it is on again
        self.last_status = {}
        self.wake = threading.Event()
        self.black = Image.new("RGB", (PANEL_W, PANEL_H))
        self.pre = {}                     # url -> bytes of the next picture, fetched while the current one shows
        self.pre_busy = None
        self.pre_lock = threading.Lock()
        self.video_busy = None            # native URL of a video being prefetched
        self.video_lock = threading.Lock()
        signal.signal(signal.SIGUSR1, lambda *_: self.wake.set())
        signal.signal(signal.SIGTERM, self._quit)
        os.makedirs(CACHE_DIR, exist_ok=True)

    def _quit(self, *_):
        self.panel.stop()
        os._exit(0)

    def get(self, url, timeout=10):
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()

    def fetch(self, url):
        if url.startswith("file://"):            # the GitHub gallery on the frame's own storage
            with open(url[7:], "rb") as f:
                return f.read()
        with self.pre_lock:
            data = self.pre.pop(url, None)
        return data if data is not None else self.get(self.base + url)

    def prefetch(self, item):
        """Download the next item in the background, so the switch at the end of the current one starts at
        once: a 2 MB exact-size JPEG takes 1-5 s over the EO1's Wi-Fi, a 25 MB video ~20 s. Videos go to the RAM
        cache (/tmp, tmpfs) like any video, never to the eMMC."""
        if not item or str(item.get("url", "")).startswith("file://"):
            return                               # gallery items are local already
        native = (item.get("native") or {}).get("url")
        if item.get("type") in ("video", "gif") and native:
            if self.video_busy or os.path.exists(os.path.join(CACHE_DIR, os.path.basename(native))):
                return
            self.video_busy = native
            threading.Thread(target=self._prefetch_video, args=(native,), daemon=True).start()
            return
        if item.get("type") not in (None, "image") or not item.get("url"):
            return
        url = item["url"]
        with self.pre_lock:
            if url in self.pre or url == self.pre_busy:
                return
            self.pre_busy = url

        def run():
            try:
                data = self.get(self.base + url, timeout=60)
                with self.pre_lock:
                    self.pre = {url: data}    # only the next one
            except Exception:  # noqa: BLE001 - it is fetched again when it is due
                pass
            finally:
                self.pre_busy = None
        threading.Thread(target=run, daemon=True).start()

    def _prefetch_video(self, url):
        try:
            self.video_file(url, keep_current=True)
            log("next video ready:", os.path.basename(url))
        except Exception as e:  # noqa: BLE001 - fetched again when it is due
            log("video prefetch failed:", e)
        finally:
            self.video_busy = None

    def status(self, **kw):
        self.last_status = kw
        try:
            with open(STATUS + ".tmp", "w") as f:
                json.dump(dict(kw, at=time.time(), paused=bool(getattr(self, "paused", None))), f)
            os.replace(STATUS + ".tmp", STATUS)
        except OSError:
            pass

    def image(self, data, title=None):
        src = Image.open(io.BytesIO(data))
        src.draft("RGB", (PANEL_H, PANEL_W))   # JPEG: decode at reduced scale when the file is larger (fast)
        src = ImageOps.exif_transpose(src) or src
        ds = display_settings()
        if not ds.get("titles"):
            title = None
        return self.panel.to_panel(src, title, title_font(ds.get("font"), title or ""), ds.get("edges", "blur"))

    def video_file(self, url, keep_current=True):
        """The video in the RAM cache, downloaded if needed (one download at a time: a prefetch of the same
        file is waited for). The cache keeps the video playing plus the new one (two ~5-25 MB files in a 128 MB
        tmpfs)."""
        if url.startswith("file://"):            # gallery videos play straight from the frame's storage
            return url[7:]
        path = os.path.join(CACHE_DIR, os.path.basename(url))
        with self.video_lock:
            if os.path.exists(path):
                return path
            cur = self.panel.current
            for f in os.listdir(CACHE_DIR):
                full = os.path.join(CACHE_DIR, f)
                if f.endswith((".mp4", ".part")) and not (keep_current and cur and cur[0] == "video" and cur[1] == full):
                    try:
                        os.unlink(full)
                    except OSError:
                        pass
            data = self.get(self.base + url, timeout=120)
            with open(path + ".part", "wb") as f:
                f.write(data)
            os.replace(path + ".part", path)
            return path

    def apply(self, st):
        item = st.get("item") or {}
        fade = float(st.get("transition") or 0)
        if st.get("blank"):
            if self.key != "blank":
                self.panel.show(self.black, min(fade, 1.0))
                self.key = "blank"
                self.status(showing="blank")
            return
        if not item:
            return
        native = (item.get("native") or {}).get("url")
        typ = item.get("type")
        key = "%s|%s|%s" % (item.get("id"), item.get("url"), native)
        if key == self.key:
            return
        try:
            showing = typ or "image"
            if typ in ("video", "gif") and native:
                log("video", item.get("id"), native)
                ok = self.panel.video(self.video_file(native), fade)
                if ok and item.get("poster"):        # its first frame into fb0 (see fb_write)
                    try:
                        fb_write(self.image(self.fetch(item["poster"])))
                    except Exception:  # noqa: BLE001
                        pass
                if not ok:
                    poster = item.get("poster")
                    log("video does not play, showing its poster", poster)
                    if not poster or not self.panel.show(self.image(self.fetch(poster)), fade):
                        return
                    showing = "poster"
            else:
                if typ in (None, "image"):
                    url = item.get("url")
                elif item.get("poster"):
                    url = item["poster"]
                else:
                    self.key = key       # not transcoded yet: tried again once the variant exists (key changes)
                    log("item", item.get("id"), "has no EO1 variant or poster yet")
                    return
                log("image", item.get("id"), url)
                if not self.panel.show(self.image(self.fetch(url), item.get("title")), fade):
                    return
            self.key = key
            self.status(showing=showing, id=item.get("id"), url=item.get("url"), title=item.get("title"))
        except Exception as e:  # noqa: BLE001 - keep showing what is up, retry at the next poll
            log("could not show item", item.get("id"), e)

    def run(self):
        try:
            with open(LAST, "rb") as f:
                self.panel.show(self.panel.to_panel(Image.open(io.BytesIO(f.read()))), 0)
            log("last picture shown")
        except Exception as e:  # noqa: BLE001
            log("no last picture:", e)
            self.panel.show(self.black, 0)
        fails = 0
        gallery = Gallery()
        source = conf().get("source", "auto")
        if not conf().get("server"):               # no server: the EO1's own gallery and uploads only
            source = "github"
        while True:
            delay = 10.0
            msg = setup_message()                  # the setup Wi-Fi or a fresh connection: say so on the screen
            if msg:
                if self.key != msg[0]:
                    self.panel.show(self.panel.to_panel(msg[1]), 1.0)
                    self.key = msg[0]
                    self.status(showing="setup")
                notify("WATCHDOG=1")
                self.wake.wait(5)
                self.wake.clear()
                continue
            if self.key and self.key.startswith(("setup|", "done|")):   # "welcome|" stays until there is art
                self.key = None                    # back to the art
            if screen_off():                       # paused while the screen is off (the agent wakes us on a change)
                if not self.paused:
                    self.paused = time.time()
                    if (self.panel.current or ("",))[0] == "video":
                        self.panel.show(self.black, 0)   # no video decoding in the dark; it restarts when on
                        self.key = None
                    self.status(**self.last_status)
                    log("screen off: slideshow paused")
                notify("WATCHDOG=1")
                self.wake.wait(30)
                self.wake.clear()
                continue
            if self.paused:                        # on again: the picture keeps the time it had left
                if gallery.started:
                    gallery.started += time.time() - self.paused
                self.paused = None
                self.status(**self.last_status)
                log("screen on: slideshow resumed")
            try:                                   # "refresh" (display settings changed): show the item again
                if open(PLAYER_CMD).read().strip() == "refresh":
                    os.remove(PLAYER_CMD)
                    self.key = None
            except OSError:
                pass
            try:
                if source == "github" or (source == "auto" and fails >= 3 and gallery.load()):
                    st = gallery.state()             # the frame's own slideshow (no server in use)
                    if st is None:
                        raise RuntimeError("no gallery on the frame yet")
                    if source == "auto" and fails >= 3 and fails % 6 == 0:
                        raise RuntimeError("checking whether the server is back")
                else:
                    st = json.loads(self.get(self.base + "/api/frame/state?scr=1080x1920", timeout=8))
                    fails = 0
                self.apply(st)
                self.prefetch(st.get("next"))
                delay = max(2.0, (st.get("poll_ms") or 10000) / 1000.0)
                rem = st.get("remaining_ms")
                if rem is not None and rem + 300 < delay * 1000:
                    delay = max(1.0, (rem + 300) / 1000.0)
            except Exception as e:  # noqa: BLE001 - server/Wi-Fi not there yet: keep the picture, retry
                fails += 1
                if fails == 3 and source == "auto" and gallery.load():
                    log("server not reachable: playing the copy on the frame (%s)" % gallery.origin())
                delay = 1.0 if uptime() < 90 or (fails >= 3 and gallery.items) else min(30.0, 2.0 * fails)
                if fails in (1, 5) or fails % 30 == 0:
                    log("server not reachable:", e)
                if fails >= 6 and not gallery.items and uptime() > 60 and not (self.key or "").startswith("welcome"):
                    try:                           # nothing at all to show: how to get started
                        ip = subprocess.run(["sh", "-c", "ip -4 -o addr show wlan0 | sed -n 's/.*inet \\([0-9.]*\\).*/\\1/p'"],
                                            capture_output=True, text=True, timeout=5).stdout.strip()
                        self.panel.show(self.panel.to_panel(welcome(ip)), 1.0)
                        self.key = "welcome"
                        self.status(showing="welcome")
                    except Exception as e2:  # noqa: BLE001
                        log("welcome card:", e2)
            notify("WATCHDOG=1")
            self.wake.wait(delay)
            self.wake.clear()


if __name__ == "__main__":
    with open("/run/eo1-player.pid", "w") as f:
        f.write(str(os.getpid()))
    Player().run()
