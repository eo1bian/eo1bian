"""The first-start splash: debian/docs/eo1bian.png (portrait 1080x1920) -> the files the EO1 shows while it starts,
made with the agent's own splash_images() so the format is exactly what the frame writes for "the last picture":
   out/state-default/splash.fb.gz   raw RGB565 for the initramfs (shown within seconds of power-on)
   out/state-default/last.jpg       for the player until the setup screen or the first picture
make-image.sh puts them in state/ on the stick; the agent replaces them with the last picture later.
   python debian/build/make-splash.py"""
import io
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
DEB = os.path.join(HERE, "..")
src = io.open(os.path.join(DEB, "overlay/usr/local/lib/eo1/agent.py"), encoding="utf-8").read()
fn = src[src.index("def splash_images("):]
fn = fn[:fn.index("\n\n\n")]
fb = dict(re.findall(r"^(FB_[WH]) = (\d+)", src, re.M))
ns = {"io": io, "FB_W": int(fb.get("FB_W", 1920)), "FB_H": int(fb.get("FB_H", 1080))}
exec(fn, ns)
rotate = re.search(r"^rotate=(\d+)", io.open(os.path.join(DEB, "stick/eo1.txt"), encoding="utf-8").read(), re.M).group(1)
data = open(os.path.join(DEB, "docs/eo1bian-boot.png"), "rb").read()
imgs = ns["splash_images"](data, rotate)
out = os.path.join(DEB, "out/state-default")
os.makedirs(out, exist_ok=True)
open(os.path.join(out, "splash.fb.gz"), "wb").write(imgs["fb"])
open(os.path.join(out, "last.jpg"), "wb").write(imgs["jpeg"])
print("splash for rotate=%s: %d + %d bytes" % (rotate, len(imgs["fb"]), len(imgs["jpeg"])))
