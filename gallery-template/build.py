#!/usr/bin/env python3
"""Build an EO1 gallery: originals in images/ and videos/ -> EO1-ready files + manifest.json.

  python build.py <repo dir> <out dir>

Pictures become JPEGs that fit the EO1's portrait panel exactly (1080x1920, Lanczos, ICC -> sRGB, 4:4:4,
quality 92). Videos and GIFs become what the EO1's hardware decoder plays directly:
H.264 Main, exactly 1920x1080, turned 90 degrees clockwise (the portrait panel's raw orientation),
padded black, 30 fps, short clips repeated to at least 12 s, plus a poster (first frame) for the switch.
Unchanged sources are not rebuilt (their sha256 is in the previous manifest). Needs Pillow and ffmpeg.

gallery.json (optional) sets the defaults the frame uses: seconds per picture/video, shuffle, cross-fade,
titles (an empty title = none) and per-file durations, and "albums": the subfolders of images/ and videos/ the
frame plays until its owner chooses on the control page (e.g. ["Holiday 2024"]; missing or empty = everything).
Every album is built and copied to the frame, so it can switch between them without the internet.
"""
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time

from PIL import Image, ImageFilter, ImageOps

try:                                  # iPhones save HEIC by default; Pillow needs pillow-heif to open it
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

W, H = 1080, 1920                     # portrait panel as the viewer sees it
VW, VH = 1920, 1080                   # video as the decoder outputs it (portrait sources turned)
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic")
VIDEO_EXT = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".gif")
MIN_LOOP_S = 12.0


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def build_image(src, dst):
    with Image.open(src) as im:
        if getattr(im, "n_frames", 1) > 1:
            im.seek(0)
        icc = im.info.get("icc_profile")
        im = ImageOps.exif_transpose(im) or im
        if icc:
            try:
                import io
                from PIL import ImageCms
                im = ImageCms.profileToProfile(im.convert("RGB"), ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                               ImageCms.createProfile("sRGB"), outputMode="RGB")
            except Exception:  # noqa: BLE001 - a broken profile: keep the pixels
                pass
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (0, 0, 0))
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        im = im.convert("RGB")
        w, h = im.size
        s = min(W / w, H / h)
        tw, th = max(1, round(w * s)), max(1, round(h * s))
        if (tw, th) != (w, h):
            im = im.resize((tw, th), Image.LANCZOS, reducing_gap=3.0 if tw < w else None)
            im = im.filter(ImageFilter.UnsharpMask(radius=0.8, percent=35 if tw < w else 20, threshold=2))
        im.save(dst + ".tmp.jpg", "JPEG", quality=92, subsampling=0, optimize=True)
    os.replace(dst + ".tmp.jpg", dst)


def probe(src):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,r_frame_rate:format=duration", "-of", "json", src],
                       capture_output=True, text=True, timeout=300)
    d = json.loads(r.stdout or "{}")
    st = (d.get("streams") or [{}])[0]
    num, _, den = (st.get("r_frame_rate") or "30/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    return int(st.get("width") or 0), int(st.get("height") or 0), float((d.get("format") or {}).get("duration") or 0), fps


def build_video(src, dst, poster):
    w, h, dur, fps = probe(src)
    if not w or not h:
        raise RuntimeError("no video stream")
    portrait = h > w
    fps_out = 30 if not fps or fps > 30 else max(10, round(fps))
    loops = max(0, math.ceil(MIN_LOOP_S / dur) - 1) if 0 < dur < MIN_LOOP_S else 0
    # the EO1 hangs in portrait: every video is turned 90 deg clockwise into the panel's raw landscape frame
    vf = ["transpose=1"] + [
        f"scale=w={VW}:h={VH}:force_original_aspect_ratio=decrease:flags=lanczos",
        "scale=trunc(iw/2)*2:trunc(ih/2)*2", f"pad={VW}:{VH}:(ow-iw)/2:(oh-ih)/2:black,setsar=1",
        f"fps={fps_out}", "format=yuv420p"]
    cmd = ["ffmpeg", "-y", "-v", "error", "-nostdin"] + (["-stream_loop", str(loops)] if loops else []) + [
        "-i", src, "-an", "-sn", "-vf", ",".join(vf), "-c:v", "libx264", "-preset", "slow",
        "-tune", "animation" if src.lower().endswith(".gif") else "film", "-profile:v", "main", "-level:v", "4.1",
        "-pix_fmt", "yuv420p", "-crf", "19", "-maxrate", "10M", "-bufsize", "20M", "-g", str(fps_out * 2),
        "-movflags", "+faststart", dst + ".tmp.mp4"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode:
        raise RuntimeError(r.stderr[-300:])
    os.replace(dst + ".tmp.mp4", dst)
    # poster: the first frame in viewing orientation (the frame shows it if the video cannot play)
    vf = ["transpose=2"] + [f"scale=w='min({W},iw)':h='min({H},ih)':force_original_aspect_ratio=decrease"]
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-nostdin", "-i", dst, "-frames:v", "1", "-vf", ",".join(vf),
                    "-q:v", "3", poster + ".tmp.jpg"], capture_output=True, timeout=300)
    os.replace(poster + ".tmp.jpg", poster)


def main(repo, out):
    cfg = {}
    if os.path.exists(os.path.join(repo, "gallery.json")):
        cfg = json.load(open(os.path.join(repo, "gallery.json"), encoding="utf-8"))
    prev = {}
    if os.path.exists(os.path.join(out, "manifest.json")):
        prev = {it["source"]: it for it in json.load(open(os.path.join(out, "manifest.json"))).get("items", [])}
    os.makedirs(os.path.join(out, "img"), exist_ok=True)
    os.makedirs(os.path.join(out, "vid"), exist_ok=True)
    items, keep, failed = [], set(), []
    titles, durations = cfg.get("titles", {}), cfg.get("durations", {})
    hidden = set(cfg.get("hidden", []))         # files kept in the repository but not shown on the frame
    for folder in ("images", "videos"):
        base = os.path.join(repo, folder)
        for root, _, files in os.walk(base):
            for name in sorted(files):
                src = os.path.join(root, name)
                rel = os.path.relpath(src, repo).replace(os.sep, "/")
                ext = os.path.splitext(name)[1].lower()
                if ext not in IMAGE_EXT + VIDEO_EXT or rel in hidden:
                    continue
                video = ext in VIDEO_EXT
                key = hashlib.sha1(rel.encode()).hexdigest()[:16]
                src_sha = sha256(src)
                it = {"id": key, "source": rel, "src_sha256": src_sha, "type": "video" if video else "image",
                      "album": rel.split("/")[1] if rel.count("/") >= 2 else "",
                      "title": titles[rel] if rel in titles else os.path.splitext(name)[0].replace("_", " "),
                      "duration": int(durations.get(rel) or (cfg.get("video_duration") if video else
                                                             cfg.get("image_duration")) or 60)}
                if video:
                    it["file"], it["poster"] = f"vid/{key}.n2.eo1.mp4", f"img/{key}.poster.jpg"
                else:
                    it["file"] = f"img/{key}.jpg"
                old = prev.get(rel)
                up_to_date = old and old.get("src_sha256") == src_sha and os.path.exists(os.path.join(out, it["file"]))
                try:
                    if not up_to_date:
                        print("build", rel, flush=True)
                        if video:
                            build_video(src, os.path.join(out, it["file"]), os.path.join(out, it["poster"]))
                        else:
                            build_image(src, os.path.join(out, it["file"]))
                    for k in ("file", "poster"):
                        if k in it:
                            p = os.path.join(out, it[k])
                            it[k + "_sha256"], it[k + "_size"] = sha256(p), os.path.getsize(p)
                            keep.add(it[k])
                    items.append(it)
                except Exception as e:  # noqa: BLE001 - one bad file must not stop the gallery
                    failed.append(rel)
                    print("FAILED", rel, e, flush=True)
    for folder in ("img", "vid"):                 # files of removed sources go
        for name in os.listdir(os.path.join(out, folder)):
            if f"{folder}/{name}" not in keep:
                os.remove(os.path.join(out, folder, name))
    manifest = {"version": 1, "built": int(time.time()),
                "settings": {"shuffle": bool(cfg.get("shuffle", True)), "transition": float(cfg.get("transition", 3)),
                             "night": cfg.get("night"), "night_max": cfg.get("night_max"),
                             "albums": list(cfg.get("albums") or [])},
                "items": items, "failed": failed}
    # control.json: remote control from github.com (screen, brightness, schedule, ...); the EO1 applies changes
    if os.path.exists(os.path.join(repo, "control.json")):
        try:
            manifest["control"] = json.load(open(os.path.join(repo, "control.json"), encoding="utf-8"))
        except ValueError as e:
            print("control.json is not valid JSON, ignored:", e, flush=True)
    with open(os.path.join(out, "manifest.json.tmp"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
    os.replace(os.path.join(out, "manifest.json.tmp"), os.path.join(out, "manifest.json"))
    print("%d items, %d failed, %.1f MB" % (len(items), len(failed),
          sum(it.get("file_size", 0) + it.get("poster_size", 0) for it in items) / 1e6))
    return 1 if failed and not items else 0


if __name__ == "__main__":
    if not shutil.which("ffmpeg"):
        print("note: ffmpeg not found, videos will fail", file=sys.stderr)
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ".", sys.argv[2] if len(sys.argv) > 2 else "out"))
