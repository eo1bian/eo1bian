"""Tests for the Immich source of gallery-sync.py against a real Immich server (a local test one).
  python -I debian/build/test_immich.py http://localhost:2283 <file with an API key>
The key needs album.read, asset.read and asset.view. Needs Pillow. Uses a temporary folder, writes nothing else."""
import importlib.util
import json
import os
import sys
import tempfile

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "gallery_sync", os.path.join(HERE, "..", "overlay", "usr", "local", "lib", "eo1", "gallery-sync.py"))
gs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gs)

url, keyfile = sys.argv[1], sys.argv[2]
gs.IMMICH_KEY = keyfile
failed = False


def check(name, ok):
    global failed
    print(("ok   " if ok else "FAIL ") + name)
    failed |= not ok


key = open(keyfile).read().strip()
h = {"x-api-key": key, "Accept": "application/json"}
albums = json.loads(gs.get(url + "/api/albums", h))
check("the server has albums", len(albums) >= 2)
ids = ",".join(a["id"] for a in albums)
manifest, files, headers = gs.from_immich({"immich_url": url + "/", "immich_albums": ids})
items = manifest["items"]
check("an asset in two albums comes once", len(items) == len({it["id"] for it in items}) == len(files))
check("every item has an album name", all(it["album"] for it in items))
check("the album names are the Immich ones", {it["album"] for it in items} <= {a["albumName"] for a in albums})
check("files are pictures in img/", all(p.startswith("img/im-") and p.endswith(".jpg") for p in files))
check("the key is sent as a header, never in a URL", all(key not in u for u, *_ in files.values()))

tmp = tempfile.mkdtemp()
for p, (u, version, size, sha) in files.items():
    dst = os.path.join(tmp, os.path.basename(p))
    gs.fetch(u, dst, headers, sha, fit=True)
    im = Image.open(dst)
    check("%s fits 1080x1920 (%dx%d)" % (os.path.basename(p)[:16], im.width, im.height),
          im.width <= 1080 and im.height <= 1920 and im.format == "JPEG" and (im.width == 1080 or im.height == 1920
                                                                              or max(im.size) < 1080))
    check("no .part left", not os.path.exists(dst + ".part"))

# the same pictures again: the same versions (nothing would be downloaded twice)
again = gs.from_immich({"immich_url": url, "immich_albums": ids})[1]
check("versions are stable", {p: v[1] for p, v in again.items()} == {p: v[1] for p, v in files.items()})

try:
    gs.from_immich({"immich_url": url, "immich_albums": ""})
    check("no album: refused", False)
except RuntimeError:
    check("no album: refused", True)
open(keyfile + ".bad", "w").write("x" * 40)
gs.IMMICH_KEY = keyfile + ".bad"
try:
    gs.from_immich({"immich_url": url, "immich_albums": ids})
    check("a wrong key fails", False)
except Exception as e:  # noqa: BLE001
    check("a wrong key fails (%s)" % str(e)[:40], "401" in str(e))
os.remove(keyfile + ".bad")
print("FAILED" if failed else "all ok")
sys.exit(1 if failed else 0)
