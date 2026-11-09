"""Render the EO1's setup screens to PNG for the README (same code as the frame: player-layers.py card()), with
made-up example values. Usage: python debian/build/render-cards.py  -> debian/docs/screen-*.png"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OV = os.path.join(HERE, "..", "overlay")
OUT = os.path.join(HERE, "..", "docs")
src = io.open(os.path.join(OV, "usr/local/lib/eo1/player-layers.py"), encoding="utf-8").read()
src = src.replace("/usr/share/fonts/truetype/eo1bian/", os.path.join(OV, "usr/share/fonts/truetype/eo1bian/").replace("\\", "/") + "/")
src = src.replace('"/usr/share/eo1/eo1bian-main.png"', repr(os.path.join(OV, "usr/share/eo1/eo1bian-main.png")))
src = src.replace('sys.path.insert(0, "/usr/local/lib/eo1")', "sys.path.insert(0, %r)" % os.path.join(OV, "usr/local/lib/eo1"))
ns = {"__name__": "render"}
exec(compile(src, "player-layers.py", "exec"), ns)
card, conf = ns["card"], None
TOKEN = "k3x9p2m7q4wd"
os.makedirs(OUT, exist_ok=True)

ns["conf"] = lambda: {"token": TOKEN}
screens = {
    "screen-5-welcome": ns["welcome"]("192.168.1.50"),
    "screen-1-setup": card("Set up this EO1", ["!First turn off mobile data on your phone"],
                           [("WIFI:T:WPA;S:EO1-setup-4F2A;P:48213907;;", "", ["1. Join the Wi-Fi EO1-setup-4F2A", "password 48213907"]),
                            ("http://192.168.4.1:2324/setup#token=" + TOKEN, "", ["2. Open the setup page", "192.168.4.1:2324/setup"])]),
    "screen-2-connected": card("Connected", ["to My-Home-WiFi", "", "Control this EO1 at", "192.168.1.50:2324/", "",
                                                "(mobile data can go back on now)"],
                               [("http://192.168.1.50:2324/#token=" + TOKEN, "scan to open the control page")]),
    "screen-3-installing": card("Installing EO1bian", ["Copying EO1bian", "", "■■□□", "",
                                                       "Keep the power and the stick in.", "(about 10 minutes)"]),
    "screen-4-installed": card("EO1bian is installed", ["", "1. Remove the USB stick", "2. Unplug the power",
                                                        "3. Plug the power back in", "",
                                                        "!Your 16 Electric Objects artworks", "!are saved in eo-art/ on this USB stick.",
                                                        "Copy them to your computer:", "this is the only copy."]),
}
for name, im in screens.items():
    # crop the empty lower part of the portrait card, keep the frame's proportions readable in a README
    bbox = im.convert("L").point(lambda p: 255 if p > 40 else 0).getbbox()
    bottom = min(im.size[1], (bbox[3] if bbox else im.size[1]) + 160)
    im.crop((0, 0, im.size[0], bottom)).resize((540, bottom // 2)).save(os.path.join(OUT, name + ".png"), optimize=True)
    print(name)
