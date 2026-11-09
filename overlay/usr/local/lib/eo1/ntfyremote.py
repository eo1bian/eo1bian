"""Live remote through ntfy: the phone sees what the EO1 shows and sends next/prev/screen/brightness from anywhere,
with no server of the owner's own and nothing opened on the router.

The EO1 and the phone share a secret made on the control page and handed to the phone in a link or QR code:
an ntfy server (default https://ntfy.zap.pm, a free ntfy server for EO1bian frames hosted by the project's maker;
the public https://ntfy.sh or the owner's own server work too), a random topic name and a 32-byte key. Two topics:
  <topic>-c   commands from the phone   (the EO1 keeps one streaming GET open on it)
  <topic>-s   status from the EO1       (only while a phone is watching: a "watch" command opens a few minutes)
Every message is encrypted and authenticated (AES-256-CTR, then HMAC-SHA256 over the direction, IV and cipher text,
with keys derived from the shared key), so the ntfy server only carries opaque bytes: it can neither read nor
forge nor alter them. Commands are a fixed list carried out through the agent's own local API (exactly what the
control page can do, never code), each with a time stamp and a nonce (old or repeated messages are refused).
Software updates never come this way: they are signed releases (eo1-update).

Crypto without extra packages: the openssl command (AES-CTR) and Python's hmac. The phone uses WebCrypto
(AES-CTR with a 128-bit counter, HMAC-SHA256), so both sides compute the same bytes."""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
import urllib.request

CONF = "/media/eo1gallery/ntfy.json"             # {"server", "topic", "key", "token"?}: on the gallery partition, survives
DEFAULT_SERVER = "https://ntfy.zap.pm"           # free for EO1bian frames (SECURITY.md); ntfy.sh or your own work too
REMOTE_PAGE = "https://eo1bian.github.io/remote/"         # remote_page= in eo1.txt: your own copy of debian/remote/


def remote_page():
    """The page the pairing link opens: the project's, or the owner's own copy (remote_page= in eo1.txt, https)."""
    try:
        for line in open("/etc/eo1/eo1.conf", encoding="utf-8"):
            if line.startswith("remote_page="):
                v = line.strip().split("=", 1)[1]
                if v.startswith("https://"):
                    return v if v.endswith("/") else v + "/"
    except OSError:
        pass
    return REMOTE_PAGE
COMMANDS = ("watch", "next", "prev", "screenOn", "screenOff", "brightness", "auto", "thumb", "albums",
            "interval", "shuffle", "titles", "edges", "show", "sheet")
ID_RE = re.compile(r"^local-[A-Za-z0-9_-]{1,64}$")
SENT_MAX = 2000                                  # picture ids whose small picture the phones already got
PLAYER = "/run/eo1-player.json"                  # what the player shows (written by it at every switch)
FAST_S = 5                                       # after a command: look every 0.2 s for this long
WATCH_S = 300                                    # a "watch" keeps status coming for 5 minutes
MAX_AGE_MS = 120_000                             # commands older (or newer) than this are refused
_state = {"watch_until": 0.0, "nonces": {}, "last_sig": None, "stop": None, "sent": []}
_kick = threading.Event()                        # a command was carried out: look at the screen again at once
_lock = threading.Lock()


def log(*a):
    print("ntfy-remote:", *a, flush=True)


def b64(b):
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def load():
    try:
        c = json.load(open(CONF))
        return c if c.get("topic") and c.get("key") else None
    except (OSError, ValueError):
        return None


def keys(c):
    k = unb64(c["key"])
    return hmac.new(k, b"eo1 enc", hashlib.sha256).digest(), hmac.new(k, b"eo1 mac", hashlib.sha256).digest()


def _ctr(key, iv, data):
    r = subprocess.run(["openssl", "enc", "-aes-256-ctr", "-K", key.hex(), "-iv", iv.hex()], input=data,
                       capture_output=True, timeout=20)
    if r.returncode:
        raise RuntimeError("openssl: " + r.stderr.decode(errors="replace")[:120])
    return r.stdout


def seal(c, direction, obj):
    """obj -> the JSON text that goes over ntfy: {"v":1,"iv","ct","mac"}."""
    ke, km = keys(c)
    iv = secrets.token_bytes(16)
    ct = _ctr(ke, iv, json.dumps(obj, separators=(",", ":")).encode())
    mac = hmac.new(km, direction.encode() + b"|" + iv + ct, hashlib.sha256).digest()
    return json.dumps({"v": 1, "iv": b64(iv), "ct": b64(ct), "mac": b64(mac)})


def unseal(c, direction, text):
    """The JSON text from ntfy -> obj, or None when it was not made with this EO1's key for this direction."""
    try:
        m = json.loads(text)
        iv, ct, mac = unb64(m["iv"]), unb64(m["ct"]), unb64(m["mac"])
    except (ValueError, KeyError, TypeError):
        return None
    ke, km = keys(c)
    good = hmac.new(km, direction.encode() + b"|" + iv + ct, hashlib.sha256).digest()
    if len(iv) != 16 or not hmac.compare_digest(good, mac):
        return None
    try:
        return json.loads(_ctr(ke, iv, ct))
    except (ValueError, RuntimeError):
        return None


TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")    # an ntfy access token (tk_...), for a server that needs one


def auth(c):
    """The header for an ntfy server that only lets its own users in (token in the config), else none."""
    return {"Authorization": "Bearer " + c["token"]} if c.get("token") else {}


def pair(server=None, token=None):
    """A new pairing (the old phone link stops working): returns the config. With no server given, the current server
    and its token stay (a new key on the same server); a new server comes with its own token or none."""
    old = load() or {}
    if server is None:
        server, token = old.get("server") or DEFAULT_SERVER, old.get("token")
    c = {"server": server.rstrip("/"), "topic": "eo1-" + b64(secrets.token_bytes(18)),
         "key": b64(secrets.token_bytes(32))}
    if token:
        c["token"] = token
    with open(CONF + ".tmp", "w") as f:
        json.dump(c, f)
    os.chmod(CONF + ".tmp", 0o600)
    os.replace(CONF + ".tmp", CONF)
    restart()
    return c


def unpair():
    try:
        os.remove(CONF)
    except OSError:
        pass
    restart()


def link(c):
    """The link that pairs a phone: the secret is in the fragment (#), which browsers never send to a server."""
    p = {"s": c["server"], "t": c["topic"], "k": c["key"]}
    if c.get("token"):
        p["a"] = c["token"]                       # the phone needs it too on a server with access tokens
    blob = b64(json.dumps(p, separators=(",", ":")).encode())
    return remote_page() + "#pair=" + blob


def accept(cmd):
    """Time stamp within MAX_AGE_MS and a nonce not seen before (replay protection)."""
    now = time.time() * 1000
    ts, n = cmd.get("ts"), str(cmd.get("n", ""))
    if not isinstance(ts, (int, float)) or abs(now - ts) > MAX_AGE_MS or not 8 <= len(n) <= 64:
        return False
    with _lock:
        seen = _state["nonces"]
        for k in [k for k, t in seen.items() if now - t > 2 * MAX_AGE_MS]:
            del seen[k]
        if n in seen:
            return False
        seen[n] = now
    return True


def run_command(api, cmd):
    """One accepted command through the agent's local API. Returns a short result text."""
    c = cmd.get("c")
    if c not in COMMANDS:
        return "unknown command"
    if c == "watch":
        _state["watch_until"] = time.time() + WATCH_S
        _state["last_sig"] = None                 # send the current status now
        _kick.set()
        return "watching"
    if c == "thumb":                              # a phone without this picture yet (new phone, cleared cache)
        _state["sent"] = []
        _state["last_sig"] = None
        _kick.set()
        return "sending the picture again"
    if c == "brightness":
        try:
            pct = max(1, min(100, int(cmd.get("v"))))
        except (TypeError, ValueError):
            return "brightness: 1-100"
        result = api("setBrightness", value=round(pct * 255 / 100))
    elif c == "auto":                             # automatic brightness from the light sensor: on or off
        result = api("autoBrightness", value="on" if cmd.get("v") is True else "off")
    elif c == "interval":                         # seconds per picture (0 = the gallery's), or "keep"
        v = cmd.get("v")
        if v != "keep" and not (isinstance(v, int) and 0 <= v <= 7 * 86400):
            return "interval: seconds or keep"
        result = api("interval", value=str(v))
    elif c == "shuffle":
        result = api("shuffle", value="on" if cmd.get("v") is True else "off")
    elif c == "titles":
        result = api("titles", value="on" if cmd.get("v") is True else "off")
    elif c == "edges":
        if cmd.get("v") not in ("blur", "mat", "black"):
            return "edges: blur, mat or black"
        result = api("titles", edges=cmd["v"])
    elif c == "show":                             # one picture now, from the picture browser
        if not isinstance(cmd.get("v"), str) or not ID_RE.match(cmd["v"]):
            return "show: a picture id"
        result = api("show", id=cmd["v"])
    elif c == "sheet":                            # the small pictures of one album, sent as one message
        if not isinstance(cmd.get("v"), str) or len(cmd["v"]) > 120:
            return "sheet: an album name"
        _state["sheet"] = cmd["v"]
        _kick.set()
        return "sheet requested"
    elif c == "albums":                           # which albums play: a list of album names, empty = all
        v = cmd.get("v")
        if not isinstance(v, list) or len(v) > 100 or not all(isinstance(x, str) and len(x) <= 120 for x in v):
            return "albums: a list of album names"
        result = api("albums", value=json.dumps(v) if v else "all")
    else:
        result = api(c)
    _kick.set()
    return result


def publish(c, direction, obj):
    body = seal(c, direction, obj).encode()
    headers = {"Cache": "yes", "Title": "EO1", **auth(c)}
    if len(body) > 3500:                         # with its small picture: over ntfy's 4 kB message limit
        headers["Filename"] = "s.json"
    req = urllib.request.Request("%s/%s-s" % (c["server"], c["topic"]), data=body, method="PUT", headers=headers)
    urllib.request.urlopen(req, timeout=30).read()


def file_thumb(showing, width=240):
    """The small picture from the picture's own file (a gallery JPEG, a video's poster, an upload): at once, sharp,
    no waiting for the cross-fade. None when there is no such file (then the screenshot)."""
    from PIL import Image
    url = str(showing.get("url") or "")
    if not url.startswith("file://"):
        return None
    path = url[7:]
    if path.endswith(".n2.eo1.mp4"):              # a gallery video: its poster
        path = path.replace("/vid/", "/img/")[:-len(".n2.eo1.mp4")] + ".poster.jpg"
    if not path.lower().endswith((".jpg", ".jpeg", ".png")) or not os.path.exists(path):
        return None
    im = Image.open(path)
    im.draft("RGB", (width, width * 2))           # JPEG: decode at a small scale (fast on the A9)
    im = im.convert("RGB")
    im.thumbnail((width, width * 2))
    import io as _io
    buf = _io.BytesIO()
    im.save(buf, "JPEG", quality=72)
    return buf.getvalue()


def player_sig():
    try:
        p = json.load(open(PLAYER))
        return json.dumps([p.get("id"), p.get("showing"), p.get("title")])
    except (OSError, ValueError):
        return None


def status_loop(c, api, shot, stop):
    """While a phone is watching: send the status whenever what is on the screen changes (checked every 2 s, at
    once after a command): a small message with the picture's id and title (the phone shows a picture it has seen
    before from its own memory, at once), and the small screenshot only the first time a picture is shown."""
    fast_until, last_psig, last_full = 0.0, None, 0.0
    while not stop.is_set():
        now = time.time()
        psig = player_sig()
        # the full status (it asks the agent) only when the player changed, after a command, or every 2 s
        due = psig != last_psig or now - last_full >= 2
        if now < _state["watch_until"] and due:
            last_psig, last_full = psig, now
            try:
                st = api("status", raw=True) or {}
                showing = st.get("showing") or {}
                sig = json.dumps([showing.get("id"), showing.get("title"), st.get("screenOn"), st.get("wanted"),
                                  st.get("auto"), st.get("albumsOn"), [a.get("count") for a in st.get("albums") or []],
                                  st.get("interval"), st.get("shuffle"), st.get("titles"), st.get("edges")])
                if sig != _state["last_sig"]:
                    pid = str(showing.get("id") or "")
                    info = {"id": pid, "title": showing.get("title") or "", "type": showing.get("showing") or showing.get("type") or "",
                            "screenOn": st.get("screenOn"),
                            "brightness": st.get("brightness") if st.get("auto") else st.get("wanted"),
                            "nightCap": st.get("nightCap"), "auto": st.get("auto"), "version": st.get("version"),
                            "albums": [{"name": a.get("name"), "key": a.get("key"), "count": a.get("count"),
                                        "source": a.get("source")} for a in (st.get("albums") or [])][:50],
                            "albumsOn": st.get("albumsOn") or [], "interval": st.get("interval"),
                            "shuffle": st.get("shuffle"), "titles": st.get("titles"), "edges": st.get("edges")}
                    publish(c, "s", dict(info, ts=int(time.time() * 1000), thumb=None))
                    if pid and pid not in _state["sent"] and st.get("screenOn") is not False:
                        try:
                            raw = file_thumb(showing)
                            if raw is None:
                                stop.wait(1.2)    # a screenshot: let the cross-fade finish first
                                raw = shot(240)
                            thumb = base64.b64encode(raw).decode()
                        except Exception:  # noqa: BLE001 - no picture: the title alone was enough
                            thumb = None
                        if thumb:
                            publish(c, "s", dict(info, ts=int(time.time() * 1000), thumb=thumb))
                            _state["sent"] = (_state["sent"] + [pid])[-SENT_MAX:]
                    _state["last_sig"] = sig
            except Exception as e:  # noqa: BLE001 - offline or ntfy busy: try again
                log("status:", e)
                time.sleep(10)
        if _state.get("sheet") is not None and time.time() < _state["watch_until"]:
            album, _state["sheet"] = _state["sheet"], None
            try:
                sh = api("albumSheet", raw=True, album=album)
                if sh.get("img"):
                    publish(c, "s", {"kind": "sheet", "ts": int(time.time() * 1000), "album": album,
                                     "cols": sh["cols"], "w": sh["w"], "h": sh["h"], "items": sh["items"],
                                     "img": sh["img"]})
            except Exception as e:  # noqa: BLE001 - the browser shows "try again"
                log("sheet:", e)
        if _kick.wait(0.2 if time.time() < fast_until else 2):
            _kick.clear()
            fast_until = time.time() + FAST_S     # a command: watch the player closely for a few seconds
            last_full = 0.0
        if stop.is_set():
            return


def command_loop(c, api, stop):
    """One streaming GET on the command topic, reconnected with a back-off; each message checked and carried out."""
    since, backoff = None, 2                      # no "since" at first: only commands sent from now on
    while not stop.is_set():
        try:
            url = "%s/%s-c/json" % (c["server"], c["topic"])
            if since:                            # after a reconnect: what came in meanwhile (still time-checked)
                url += "?since=" + urllib.parse.quote(since)
            with urllib.request.urlopen(urllib.request.Request(url, headers=auth(c)), timeout=90) as r:   # keepalive every 45 s
                backoff = 2
                for line in r:
                    if stop.is_set():
                        return
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if ev.get("event") != "message":
                        continue
                    since = ev.get("id") or since
                    cmd = unseal(c, "c", ev.get("message", ""))
                    if not cmd or not accept(cmd):
                        log("refused a message (wrong key, too old or repeated)")
                        continue
                    log("command", cmd.get("c"), "->", run_command(api, cmd))
        except Exception as e:  # noqa: BLE001 - offline, DNS, ntfy restart: reconnect later
            if not stop.is_set():
                log("reconnecting:", e)
                stop.wait(backoff)
                backoff = min(300, backoff * 2)


_api, _shot = None, None


def restart():
    """(Re)start the loops for the current config (none when not paired)."""
    with _lock:
        if _state["stop"]:
            _state["stop"].set()
        _state["stop"] = None
        c = load()
        if not c or not _api:
            return
        stop = threading.Event()
        _state["stop"] = stop
    threading.Thread(target=command_loop, args=(c, _api, stop), daemon=True).start()
    threading.Thread(target=status_loop, args=(c, _api, _shot, stop), daemon=True).start()
    log("on (%s)" % c["server"])


def start(api, shot):
    """api(cmd, raw=False, **params) -> statustext (or the whole reply with raw=True); shot(width) -> JPEG bytes."""
    global _api, _shot
    _api, _shot = api, shot
    restart()


def info():
    c = load()
    if not c:
        return {"paired": False}
    return {"paired": True, "server": c["server"], "token": bool(c.get("token")), "link": link(c),
            "watching": time.time() < _state["watch_until"]}
