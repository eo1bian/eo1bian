"""Serve the EO1's phone pages (control.html, setup.html) with made-up data, for screenshots in the README.
No frame needed, nothing private: example networks, an abstract test picture.
   python debian/build/mock-pages.py [port]      then open http://127.0.0.1:8099/#token=demo"""
import http.server
import io
import json
import os
import sys
import urllib.parse

from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
OV = os.path.join(HERE, "..", "overlay")
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8099


def art(w, h, seed=0):
    """An abstract picture (soft colour fields) standing in for the art on the frame."""
    im = Image.new("RGB", (w, h), (24, 20, 30))
    d = ImageDraw.Draw(im)
    cols = [(240, 162, 74), (214, 92, 110), (88, 120, 200), (110, 190, 170), (250, 220, 140)]
    for i in range(7):
        c = cols[(i + seed) % len(cols)]
        x = (i * 173 + seed * 61) % w
        y = (i * 311 + seed * 97) % h
        r = w // 3 + (i * 37) % (w // 4)
        d.ellipse((x - r, y - r, x + r, y + r), fill=c)
    return im.filter(ImageFilter.GaussianBlur(w // 12))


def jpeg(im, q=85):
    b = io.BytesIO()
    im.save(b, "JPEG", quality=q)
    return b.getvalue()


STATUS = {
    "showing": {"showing": "image", "id": "local-demo", "title": "Evening Colours — demo", "paused": False},
    "screenOn": True, "brightness": 200, "wanted": 200, "nightCap": int(os.environ.get("EO1_NIGHTCAP", "255")), "auto": False, "autoCurve": "default",
    "lux": 42.0, "autoMin": 2, "autoMax": 100, "autoOffset": 0,
    "schedule": {"enabled": True, "windows": [{"off": "23:00", "on": "07:00"}]}, "interval": 300,
    "offMode": "sleep", "source": "github", "githubRepo": "you/eo1-gallery", "galleryItems": 24,
    "uploads": [{"id": "1", "title": "Beach"}, {"id": "2", "title": "Garden"}, {"id": "3", "title": "Cat"}],
    "hidden": [{"id": "local-up-2", "title": "Garden"}], "titles": True, "titleFont": "serif", "wifi": {"connected": True, "ssid": "My-Home-WiFi", "signal": 72,
                                                                 "ip": "192.168.1.50"},
    "ntp": True, "version": "v1.0.0", "uptime": 93600, "temp": 47.5, "setupAp": False,
    "storage": {"totalMB": 487, "freeMB": 301}, "remote": json.loads(os.environ.get("EO1_REMOTE", '{"state": "off"}')), "fromStick": False, "install": None, "status": "OK", "statustext": "status",
}
STICK = bool(os.environ.get("EO1_STICK"))
CMDS = {"windows": "irm eo1.local/4821|iex", "mac": "curl -s eo1.local/4821|sh", "linux": "curl -s eo1.local/4821|sh",
        "windowsIp": "irm 192.168.1.50/4821|iex", "unixIp": "curl -s 192.168.1.50/4821|sh"}
NETS = [{"ssid": "My-Home-WiFi", "signal": 92, "security": "wpa"}, {"ssid": "Neighbour 2.4G", "signal": 54, "security": "wpa"},
        {"ssid": "Cafe Guest", "signal": 31, "security": "open"}]
FILES = {"/manifest.webmanifest": ("usr/share/eo1/manifest.webmanifest", "application/manifest+json"),
         "/icon.svg": ("usr/share/eo1/icon.svg", "image/svg+xml"), "/icon.png": ("usr/share/eo1/icon.png", "image/png")}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype="application/json", code=200):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self.do_GET()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = dict(urllib.parse.parse_qsl(u.query))
        cmd = q.get("cmd")
        if u.path in FILES:
            f, t = FILES[u.path]
            return self.send(open(os.path.join(OV, f), "rb").read(), t)
        if u.path.startswith("/fonts/"):
            return self.send(open(os.path.join(OV, "usr/share/fonts/truetype/eo1bian", os.path.basename(u.path)), "rb").read(), "font/ttf")
        if u.path.startswith("/pair/") and u.path.endswith("/info"):
            return self.send({"status": "OK", "commands": CMDS})
        if not cmd:
            page = {"/setup": "setup.html", "/pc": "pc.html"}.get(u.path, "control.html")
            return self.send(open(os.path.join(OV, "usr/share/eo1", page), "rb").read(), "text/html; charset=utf-8")
        if cmd == "status":       # ?stick=1 on the page URL is not seen here; the demo runs from the stick when STICK is set
            return self.send(dict(STATUS, fromStick=STICK))
        if cmd == "pairStart":
            import time
            return self.send({"status": "OK", "code": "4821", "expires": int(time.time()) + 600, "pcUrl": "http://eo1.local",
                              "commands": CMDS, "host": "eo1"})
        if cmd == "sshKeys":
            return self.send({"status": "OK", "keys": [{"type": "ed25519", "name": "you@laptop"}], "sshOn": True})
        if cmd == "screenshot":
            w = int(q.get("w", "270"))
            return self.send(jpeg(art(w, w * 16 // 9)), "image/jpeg")
        if cmd == "uploadThumb":
            return self.send(jpeg(art(240, 427, int(q.get("id", "1")))), "image/jpeg")
        if cmd == "setupScan":
            return self.send({"networks": NETS, "status": "OK", "statustext": "Scan"})
        return self.send({"status": "OK", "statustext": "OK (demo)"})


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
