#!/usr/bin/env python3
"""eo1-player: native picture/video player for the EO1 (instead of cage + Chromium + frame.html).

Same contract as frame.html: polls the server's /api/frame/state?scr=1080x1920 and shows `item`, honours `blank`,
`transition` (cross-fade seconds), `poll_ms` and `remaining_ms`. Output goes straight to the panel through
GStreamer's kmssink (DRM, 32-bit colour, no compositor, no browser):

  images  - decoded and cross-faded with Pillow, streamed as raw BGRx frames into
            `fdsrc ! rawvideoparse ! kmssink` (one long-lived pipeline; kmssink keeps the last frame)
  video   - the server's native variant (<= 1920x1080 landscape H.264, portrait sources already rotated 90 deg
            CCW = the panel's raw orientation), decoded by the i.MX6 VPU: `qtdemux ! h264parse !
            v4l2h264dec ! kmssink` (30 fps, ~7 % CPU measured; Chromium/WPE decoded in software at >100 %)
  GIF     - played as its video variant (the server makes MP4s of GIFs)

The panel is landscape 1920x1080 mounted in portrait; rotate= in eo1.conf is the clockwise rotation of the picture
on the panel (270 for the EO1 hanging with the cable at the bottom). Starts with state/last.jpg from the boot
partition, so a picture is up before the network is. SIGUSR1 = poll now (the agent's "poll"/"next" nudge).
Writes /run/eo1-player.json with what it shows (for the agent).
"""
import io
import json
import os
import signal
import subprocess
import threading
import time
import urllib.request

from PIL import Image, ImageOps

PANEL_W, PANEL_H = 1920, 1080          # raw panel (landscape)
CONF = "/etc/eo1/eo1.conf"
LAST = "/media/eo1usb/state/last.jpg"
STATUS = "/run/eo1-player.json"
CACHE_DIR = "/tmp/eo1-player"
VIDEO = "/usr/local/bin/eo1-video"    # gapless loop: one pipeline, segment seeks (debian/build/eo1-video.c)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


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


def uptime():
    try:
        return float(open("/proc/uptime").read().split()[0])
    except (OSError, ValueError):
        return 0.0


def fb_write(im):
    """Put a panel-orientation picture into fb0 (imx-drm's fbdev, RGB565 1920x1080) in the background. fb0 is
    what the panel shows for a moment whenever nobody holds the display (eo1-show handing over to a video and
    back): with the current picture there, a switch never flashes the old boot splash."""
    def run():
        try:
            data = im.convert("BGR;16").tobytes()
            with open("/dev/fb0", "r+b") as f:
                f.write(data)
        except Exception:  # noqa: BLE001 - cosmetic
            pass
    threading.Thread(target=run, daemon=True).start()


def fb_put(data):
    """Write prepared RGB565 bytes into fb0 now (not in the background): before a handover the panel shows fb0
    for a moment, and a write still running showed the previous picture for ~150 ms (framecheck switch capture)."""
    try:
        with open("/dev/fb0", "r+b") as f:
            f.write(data)
    except OSError:
        pass


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
    """Images: eo1-show (C, DRM dumb buffers + page flips) owns the display and cross-fades at 25 fps.
    Commands on its stdin: "show <file.raw> <fade_ms>"; it answers "ok <frames> <ms>"."""

    CARD = "/dev/dri/by-path/platform-display-subsystem-card"

    def __init__(self, rotate):
        self.rotate = int(rotate or 0) % 360
        self.proc = None
        self.current = None              # PIL image (panel orientation) currently on screen
        self.n = 0

    def to_panel(self, im):
        """A picture as the viewer sees it (fit contain on black) -> raw panel frame orientation."""
        r = self.rotate
        view = (PANEL_H, PANEL_W) if r in (90, 270) else (PANEL_W, PANEL_H)
        im = ImageOps.pad(im.convert("RGB"), view, color=(0, 0, 0), method=Image.BICUBIC)
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
        card = self.CARD if os.path.exists(self.CARD) else "/dev/dri/card0"
        cmd = ["/usr/local/bin/eo1-show", card]
        if self.current is not None:
            cmd.append(self._raw(self.current))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, bufsize=1)
        line = self.proc.stdout.readline().strip()
        if not line.startswith("ready"):
            log("eo1-show did not start:", line, self.proc.stderr.read(400) if self.proc.poll() is not None else "")
        if self.current is not None:
            self.proc.stdout.readline()  # "ok" for the initial picture

    def stop(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.write("quit\n")
                self.proc.stdin.flush()
                self.proc.wait(3)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        self.proc = None

    def show(self, im, fade_s):
        """Cross-fade from what is on screen to im (already in panel orientation)."""
        self.start()
        fade_ms = int(fade_s * 1000) if self.current is not None else 0
        self.current = im
        path = self._raw(im)
        try:
            self.proc.stdin.write("show %s %d\n" % (path, fade_ms))
            self.proc.stdin.flush()
            reply = self.proc.stdout.readline().strip()
            if fade_ms:
                log("fade:", reply)
            fb_write(im)
        except Exception as e:  # noqa: BLE001 - restart the presenter and show without fade
            log("eo1-show failed:", e)
            self.stop()
            self.start()


class Video:
    """VPU-decoded H.264 straight to the panel, looped without a gap by eo1-video until stopped (restarting
    the pipeline at every end released the display, and the boot splash in fb0 flashed up)."""

    def __init__(self, wake=None):
        self.wake = wake
        self.proc = None
        self.stop_flag = threading.Event()
        self.thread = None
        self.failed = False               # the file does not play (e.g. not the panel size): show the poster
        self.started = threading.Event()

    def play(self, path, armed=False):
        """armed: eo1-video loads the clip now and starts on go() (the player cross-fades meanwhile)."""
        self.stop()
        self.stop_flag.clear()
        self.failed = False
        self.started = threading.Event()
        self.thread = threading.Thread(target=self._loop, args=(path, armed), daemon=True)
        self.thread.start()
        if armed:
            self.started.wait(5)          # the process exists, so go() has a stdin to write to

    def go(self):
        try:
            self.proc.stdin.write("go\n")
            self.proc.stdin.flush()
        except Exception:  # noqa: BLE001 - not armed or already gone: it starts (or fails) by itself
            pass

    def _loop(self, path, armed):
        fails = 0
        while not self.stop_flag.is_set():
            t0 = time.time()
            cmd = [VIDEO] + (["--wait"] if armed else []) + (["--modeset"] if fails else []) + [path]
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE if armed else subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            self.started.set()
            armed = False                 # a restart after a failure starts at once
            # not communicate(): it closes stdin at once, and an armed eo1-video takes that EOF as "go" and
            # starts mid-fade while eo1-show still owns the display (seen: resource errors, poster fallback)
            err = self.proc.stderr.read() or ""
            self.proc.wait()
            rc = self.proc.returncode
            if rc != 0 and not self.stop_flag.is_set():
                fails = fails + 1 if time.time() - t0 < 10 else 1
                log("video pipeline exited with", rc, err.strip().splitlines()[-1:] if err else "")
                if fails >= 2:
                    self.failed = True
                    if self.wake:
                        self.wake.set()
                    return
                time.sleep(2)
            else:
                fails = 0

    def stop(self):
        self.stop_flag.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.thread:
            self.thread.join(5)
        self.proc = None
        self.thread = None

    def finish(self, timeout=45):
        """End at the end of the pass on screen (eo1-video SIGUSR1). Its last frame stays up, and in a seamless
        loop that frame leads into the poster (frame 0), so the cross-fade from the poster starts without a jump."""
        proc = self.proc
        if not self.playing or proc is None or proc.poll() is not None:
            self.stop()
            return
        self.stop_flag.set()
        try:
            proc.send_signal(signal.SIGUSR1)
            proc.wait(timeout)
        except Exception:  # noqa: BLE001 - stop() below ends it the hard way
            pass
        self.stop()

    @property
    def playing(self):
        return self.thread is not None and self.thread.is_alive()


class Player:
    def __init__(self):
        c = conf()
        self.base = c["server"].split("/frame")[0]
        self.panel = Panel(c.get("rotate", "270"))
        self.key = None
        self.wake = threading.Event()
        self.video = Video(self.wake)
        self.poster = None                # poster URL of the video item on screen (fallback when it fails)
        self.video_poster = None          # that poster in panel orientation: where every switch away fades from
        self.black = Image.new("RGB", (PANEL_W, PANEL_H))
        self.pre = {}                     # url -> bytes of the next picture, fetched while the current one shows
        self.pre_lock = threading.Lock()
        signal.signal(signal.SIGUSR1, lambda *_: self.wake.set())
        signal.signal(signal.SIGTERM, self._quit)
        os.makedirs(CACHE_DIR, exist_ok=True)

    def _quit(self, *_):
        self.video.stop()
        self.panel.stop()
        os._exit(0)

    def get(self, url, timeout=10):
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.read()

    def fetch(self, url):
        with self.pre_lock:
            data = self.pre.pop(url, None)
        return data if data is not None else self.get(self.base + url)

    def prefetch(self, item):
        """Download the next picture in the background (a 2 MB exact-size JPEG takes 1-5 s over the EO1's
        Wi-Fi), so the switch at the end of the current one starts at once."""
        if not item or item.get("type") not in (None, "image") or not item.get("url"):
            return
        url = item["url"]
        with self.pre_lock:
            if url in self.pre or url == getattr(self, "pre_busy", None):
                return
            self.pre_busy = url

        def run():
            try:
                data = self.get(self.base + url, timeout=60)
                with self.pre_lock:
                    self.pre = {url: data}    # only the next one: RAM is 1 GB
            except Exception:  # noqa: BLE001 - it is fetched again when it is due
                pass
            finally:
                self.pre_busy = None
        threading.Thread(target=run, daemon=True).start()

    def status(self, **kw):
        try:
            with open(STATUS + ".tmp", "w") as f:
                json.dump(dict(kw, at=time.time()), f)
            os.replace(STATUS + ".tmp", STATUS)
        except OSError:
            pass

    def show_image(self, data, fade):
        src = Image.open(io.BytesIO(data))
        src.draft("RGB", (PANEL_H, PANEL_W))   # JPEG: decode at reduced scale when the file is larger (fast)
        src = ImageOps.exif_transpose(src) or src   # server variants are upright already; originals may not be
        t0 = time.time()
        im = self.panel.to_panel(src)
        log("decoded in %.2f s" % (time.time() - t0))
        self.leave_video()
        self.panel.show(im, fade)

    def leave_video(self):
        """Let a playing video finish its pass, then hand the panel back to eo1-show with the video's poster as
        the picture on screen, so the next switch is an ordinary cross-fade."""
        if not self.video.playing:
            return
        t0 = time.time()
        self.video.finish()
        log("video ended its pass after %.1f s" % (time.time() - t0))
        if self.video_poster is not None:
            self.panel.current = self.video_poster

    def show_video(self, url, poster, fade):
        path = os.path.join(CACHE_DIR, os.path.basename(url))
        if not os.path.exists(path):
            for f in os.listdir(CACHE_DIR):        # one video at a time: /tmp is RAM
                try:
                    os.unlink(os.path.join(CACHE_DIR, f))
                except OSError:
                    pass
            data = self.get(self.base + url, timeout=120)
            with open(path + ".part", "wb") as f:
                f.write(data)
            os.replace(path + ".part", path)
        self.leave_video()
        self.video.play(path, armed=True) # loads the clip while the fade runs
        fb = poster.convert("BGR;16").tobytes() if poster is not None else None
        if poster is not None:            # fade to the clip's first frame; the clip then starts from exactly it
            self.panel.show(poster, fade)
            fb_put(fb)                    # fb0 = the poster before eo1-show lets go of the display
        self.panel.stop()
        self.video.go()
        self.video_poster = poster

    def apply(self, st):
        item = st.get("item") or {}
        fade = float(st.get("transition") or 0)
        if st.get("blank"):
            if self.key != "blank":
                self.leave_video()
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
            if typ in ("video", "gif") and native:
                log("video", item.get("id"), native)
                self.poster = item.get("poster")
                poster = None
                if self.poster:
                    try:
                        poster = self.panel.to_panel(Image.open(io.BytesIO(self.fetch(self.poster))))
                    except Exception as e:  # noqa: BLE001 - no fade then, the video still plays
                        log("no poster:", e)
                self.show_video(native, poster, fade)
            else:
                if typ in (None, "image"):
                    url = item.get("url")
                elif item.get("poster"):
                    url = item["poster"]
                else:
                    # a video/GIF not transcoded yet: nothing to show, and fetching the MP4 as a picture every
                    # poll only wasted Wi-Fi; the key includes the native URL, so it is tried again once there
                    self.key = key
                    log("item", item.get("id"), "has no EO1 variant or poster yet")
                    return
                log("image", item.get("id"), url)
                self.show_image(self.fetch(url), fade)
            self.key = key
            self.status(showing=typ or "image", id=item.get("id"), url=item.get("url"), title=item.get("title"))
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
        while True:
            delay = 10.0
            if self.video.failed:
                self.video.failed = False
                self.video.stop()
                log("video does not play, showing its poster", self.poster)
                try:
                    if self.poster:
                        self.show_image(self.fetch(self.poster), 0)
                        self.status(showing="poster", url=self.poster)
                except Exception as e:  # noqa: BLE001
                    log("no poster:", e)
            try:
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
                # right after boot Wi-Fi comes up any second (static address = usable at association): retry
                # every second then, back off later
                delay = 1.0 if uptime() < 90 else min(30.0, 2.0 * fails)
                if fails in (1, 5) or fails % 30 == 0:
                    log("server not reachable:", e)
            notify("WATCHDOG=1")
            self.wake.wait(delay)
            self.wake.clear()


if __name__ == "__main__":
    with open("/run/eo1-player.pid", "w") as f:
        f.write(str(os.getpid()))
    Player().run()
