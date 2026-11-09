"""Builds the title fonts for EO1 (debian/overlay/usr/share/fonts/truetype/eo1bian) from the variable fonts in
Google Fonts' repo (all SIL OFL 1.1): static instances at the weight that reads best as a caption over a picture,
plus coverage.json (the code points each font has) so the player can fall back to DejaVu for anything else.
Usage: python build/fonts.py <dir with the downloaded variable fonts and their OFL.txt>"""
import json
import os
import shutil
import sys

from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

SRC = sys.argv[1]
OUT = os.path.join(os.path.dirname(__file__), "..", "overlay", "usr", "share", "fonts", "truetype", "eo1bian")
FONTS = {   # output name: (source, axis values)
    "Inter-Medium.ttf": ("inter-Inter.ttf", {"wght": 500, "opsz": 32}),
    "Jost-Medium.ttf": ("jost-Jost.ttf", {"wght": 500}),
    "Fraunces-Soft.ttf": ("fraunces-Fraunces.ttf", {"wght": 420, "opsz": 48, "SOFT": 100, "WONK": 0}),
}


def ranges(cps):
    out, start, prev = [], None, None
    for c in sorted(cps):
        if start is None:
            start = prev = c
        elif c == prev + 1:
            prev = c
        else:
            out.append([start, prev])
            start = prev = c
    if start is not None:
        out.append([start, prev])
    return out


os.makedirs(OUT, exist_ok=True)
cover = {}
for name, (src, axes) in FONTS.items():
    f = TTFont(os.path.join(SRC, src))
    f = instancer.instantiateVariableFont(f, axes, updateFontNames=False)
    f.save(os.path.join(OUT, name))
    cover[name] = ranges(f.getBestCmap())
    print(name, os.path.getsize(os.path.join(OUT, name)))
for fam in ("inter", "jost", "fraunces"):
    shutil.copy(os.path.join(SRC, fam + "-OFL.txt"), os.path.join(OUT, "OFL-" + fam + ".txt"))
with open(os.path.join(OUT, "coverage.json"), "w", newline="\n") as fh:
    json.dump(cover, fh, separators=(",", ":"))
