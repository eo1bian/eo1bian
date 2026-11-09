#!/usr/bin/env python3
"""Rescue the original Electric Objects art that the old Android app left in its cache.

The EO app kept the artworks it showed (short H.264 loops, 1080x1920 portrait, and some pictures) in
/data/data/com.electricobjects.app/cache and cache/video-cache. Installing EO1bian reuses that partition, so the
installer runs this first; it also works on a mounted backup of Android's /data.

Only complete files are kept (the video cache also holds half-downloaded clips), duplicates are skipped, and the
result is a folder you can drop into your gallery repo (GitHub builds the EO1 versions from it). Read-only on the
source. Titles and artists are not in the cache, so they cannot be recovered, and neither can the playlist's
timing (the EO cloud decided when to change). The order can: the cache filled up as the EO1 showed the art, so the
files are numbered by when they were cached (001-..., oldest first); a gallery with shuffle off plays them in that
order.

Usage: eo-art-rescue.py SOURCE DEST      (SOURCE = Android's /data or any folder above the EO app's data)
Prints a summary; exit code 0 also when nothing was found, 1 when an artwork could not be saved to DEST."""
import hashlib
import os
import shutil
import struct
import sys

APP = "com.electricobjects.app"
README = """Electric Objects art rescued from this EO1
==========================================

These are the artworks the original Electric Objects app had cached on this EO1 (short video loops and
pictures, 1080x1920 portrait). Titles and artists were not stored on the device, so the files are named by
their content, numbered in the order the EO1 cached them (roughly the order it showed them). To keep that order,
set "shuffle": false in your gallery.json.

To show them with EO1bian: copy the files into the art folder of your gallery repo (see gallery-template),
or upload pictures from the control page. They are yours to keep; the artists' rights still apply.
"""


def kind(head):
    """File type from its first bytes."""
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:3] == b"\xff\xd8\xff":
        return "jpg"
    if head[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def mp4_complete(path):
    """True if the top-level boxes cover the whole file and it has both moov and mdat (a clip the cache only
    half downloaded ends inside a box or misses moov)."""
    size = os.path.getsize(path)
    seen, pos = set(), 0
    with open(path, "rb") as f:
        while pos < size:
            f.seek(pos)
            h = f.read(16)
            if len(h) < 8:
                return False
            n, typ = struct.unpack(">I4s", h[:8])
            if n == 1:
                if len(h) < 16:
                    return False
                n = struct.unpack(">Q", h[8:16])[0]
            elif n == 0:
                n = size - pos
            if n < 8 or pos + n > size:
                return False
            seen.add(typ)
            pos += n
    return b"moov" in seen and b"mdat" in seen


def app_dirs(root):
    """The EO app's data folders under root (Android's /data has it at data/<app>; backups vary)."""
    for base, dirs, _ in os.walk(root):
        depth = base[len(root):].count(os.sep)
        if os.path.basename(base) == APP:
            yield base
            dirs[:] = []
        elif depth >= 4:
            dirs[:] = []


def rescue(src, dest):
    out = os.path.join(dest, "eo-art")
    found = kept = partial = dupes = failed = 0
    seen = set()
    picked = []                                    # (mtime, path, digest, ext): numbered by age at the end
    for app in app_dirs(src):
        for sub in ("cache", "files", "app_webview", "app_xwalkcore"):
            top = os.path.join(app, sub)
            for base, _, files in os.walk(top):
                for name in sorted(files):
                    path = os.path.join(base, name)
                    try:
                        with open(path, "rb") as f:
                            head = f.read(16)
                        ext = kind(head)
                        if not ext or os.path.getsize(path) < 4096:
                            continue
                    except OSError:
                        continue
                    found += 1
                    try:
                        if ext == "mp4" and not mp4_complete(path):
                            partial += 1
                            continue
                        h = hashlib.sha1()
                        with open(path, "rb") as f:
                            for chunk in iter(lambda: f.read(1 << 20), b""):
                                h.update(chunk)
                        digest = h.hexdigest()
                        mtime = os.path.getmtime(path)
                    except OSError:                # a recognized artwork that cannot even be read back
                        failed += 1
                        continue
                    if digest in seen:
                        dupes += 1
                        continue
                    seen.add(digest)
                    picked.append((mtime, path, digest, ext))
    for i, (mtime, path, digest, ext) in enumerate(sorted(picked), 1):
        try:
            os.makedirs(out, exist_ok=True)
            target = os.path.join(out, "%03d-eo-%s.%s" % (i, digest[:12], ext))
            if not os.path.exists(target):
                shutil.copyfile(path, target + ".part")
                os.replace(target + ".part", target)
                os.utime(target, (mtime, mtime))
            kept += 1
        except OSError:
            failed += 1
            continue
    if kept:
        with open(os.path.join(out, "README.txt"), "w", newline="\r\n") as f:
            f.write(README)
    return found, kept, partial, dupes, failed


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    found, kept, partial, dupes, failed = rescue(sys.argv[1], sys.argv[2])
    print("EO art: %d found, %d saved, %d incomplete skipped, %d duplicates" % (found, kept, partial, dupes))
    if failed:
        print("EO art: %d of them could NOT be saved (is the stick full?)" % failed)
        sys.exit(1)
