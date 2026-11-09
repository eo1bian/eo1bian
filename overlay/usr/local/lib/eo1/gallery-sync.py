#!/usr/bin/env python3
"""eo1-gallery-sync: keep a copy of the frame's pictures and videos on its own storage, so it keeps showing
them when nothing else is reachable.

Two sources:
  github  github_repo in eo1.conf: the branch "eo1" of a GitHub gallery (gallery-template/), fetched with git
          over SSH with the frame's own read-only deploy key (state/github-deploy on the boot partition, made on
          the frame; only its public half is on GitHub) into eo1-github/ - a shallow checkout, old objects
          pruned after each change. Falls back to the GitHub API with a read token (state/github-token).
  server  otherwise: the active playlist of a server on the LAN (optional), from
          /api/frame/playlist - the same EO1-ready files the frame streams (exact-size pictures, the videos'
          native variants and posters). With the PC off, the frame then plays the same playlist on its own.

Only missing or changed files are downloaded; files no longer in the source are deleted; the local manifest
(same format for both sources) is written last. Storage: Android's old CACHE partition (ext4, mounted at
/media/eo1gallery by media-eo1gallery.mount), directory eo1-gallery/. Every file goes to .part, is flushed and
renamed, so a power cut never leaves half a picture; the eMMC is written only when the source changes.
Run by eo1-gallery-sync.timer (2 min after boot, then every 10 min) and by the agent when settings change.
"""
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import urllib.parse
import urllib.request

CONF = "/etc/eo1/eo1.conf"
TOKEN = "/media/eo1usb/state/github-token"
DEPLOY_KEY = "/media/eo1usb/state/github-deploy"
KNOWN_HOSTS = "/media/eo1usb/state/github_known_hosts"
ROOT = "/media/eo1gallery"
STORE = ROOT + "/eo1-gallery"                    # the server's playlist (and the API-token GitHub mode)
GIT_STORE = ROOT + "/eo1-github"                 # the GitHub gallery checkout (deploy key mode)
ACTIVE = ROOT + "/active"                        # which of the two the player reads
INDEX = os.path.join(STORE, ".index.json")       # local path -> version (sha256 or source URL) of what is here


def log(*a):
    print(*a, flush=True)


def conf():
    c = {}
    try:
        for line in open(CONF, encoding="utf-8"):
            if "=" in line:
                k, v = line.strip().split("=", 1)
                c[k.strip()] = v.strip()
    except OSError:
        pass
    return c


def fetch(url, dst, headers, sha=None):
    """download to dst (via .part, flushed); sha256 checked when the source gives one"""
    tmp = dst + ".part"
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as f:
        while True:
            b = r.read(1 << 20)
            if not b:
                break
            h.update(b)
            f.write(b)
        f.flush()
        os.fsync(f.fileno())
    if sha and h.hexdigest() != sha:
        os.remove(tmp)
        raise RuntimeError("checksum mismatch")
    os.replace(tmp, dst)


# ------------------------------------------------------------------ sources -> (manifest, {path: (url, version, size, sha)})
def from_github(c):
    repo, branch = c["github_repo"], c.get("github_branch") or "eo1"
    token = open(TOKEN).read().strip()
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github.raw",
               "User-Agent": "eo1-gallery-sync", "X-GitHub-Api-Version": "2022-11-28"}
    url = lambda p: "https://api.github.com/repos/%s/contents/%s?ref=%s" % (repo, p, branch)
    with urllib.request.urlopen(urllib.request.Request(url("manifest.json"), headers=headers), timeout=60) as r:
        manifest = json.loads(r.read())
    files = {}
    for it in manifest.get("items", []):
        for k in ("file", "poster"):
            if it.get(k):
                sha = it.get(k + "_sha256")
                files[it[k]] = (url(it[k]), sha, it.get(k + "_size") or 0, sha)
    return manifest, files, headers


def from_server(c):
    if not c.get("server"):
        raise RuntimeError("no server configured")
    base = c["server"].split("/frame")[0]
    headers = {"User-Agent": "eo1-gallery-sync"}
    with urllib.request.urlopen(base + "/api/frame/playlist?scr=1080x1920", timeout=30) as r:
        pl = json.loads(r.read())
    items, files = [], {}
    for it in pl.get("items", []):
        typ = it.get("type") or "image"
        entry = {"id": "srv-%s" % it.get("id"), "type": "image", "title": it.get("title"),
                 "duration": int(it.get("effective_duration") or it.get("duration") or 60)}
        if typ in ("video", "gif"):
            native = (it.get("native") or {}).get("url")
            if not native:
                continue                              # not transcoded yet: next run
            entry["type"] = "video"
            entry["file"] = "vid/" + os.path.basename(native)
            files[entry["file"]] = (base + native, native, it.get("size") or 0, None)
            if it.get("poster"):
                entry["poster"] = "img/" + os.path.basename(it["poster"])
                files[entry["poster"]] = (base + it["poster"], it["poster"], 0, None)
        else:
            if not it.get("url"):
                continue
            entry["file"] = "img/" + os.path.basename(it["url"])
            files[entry["file"]] = (base + it["url"], it["url"], it.get("size") or 0, None)
        items.append(entry)
    src = pl.get("source") or {}
    manifest = {"version": 1, "from": "server:%s" % src.get("name"),
                "settings": {"shuffle": bool(src.get("shuffle", True)), "transition": pl.get("transition", 3)},
                "items": items}
    return manifest, files, headers


def set_active(name):
    """tell the player which copy to play (atomic); the other copy is removed to keep the 512 MB free"""
    old = open(ACTIVE).read().strip() if os.path.exists(ACTIVE) else ""
    if old != name:
        with open(ACTIVE + ".tmp", "w") as f:
            f.write(name)
            f.flush()
            os.fsync(f.fileno())
        os.replace(ACTIVE + ".tmp", ACTIVE)
        other = GIT_STORE if name == "eo1-gallery" else STORE
        shutil.rmtree(other, ignore_errors=True)
        log("now playing from", name)
    return old != name


def git_sync(c):
    """the GitHub gallery with git over SSH and the frame's deploy key; returns True when something changed"""
    repo, branch = c["github_repo"], c.get("github_branch") or "eo1"
    shutil.copyfile(DEPLOY_KEY, "/run/eo1-github-key")
    os.chmod("/run/eo1-github-key", 0o600)
    shutil.copyfile(KNOWN_HOSTS, "/run/eo1-github-known")
    env = dict(os.environ, GIT_SSH_COMMAND="ssh -i /run/eo1-github-key -o IdentitiesOnly=yes -o BatchMode=yes "
               "-o StrictHostKeyChecking=yes -o UserKnownHostsFile=/run/eo1-github-known")

    def git(*a, check=True):
        r = subprocess.run(["git", "-C", GIT_STORE] + list(a), capture_output=True, text=True, env=env, timeout=1800)
        if check and r.returncode:
            raise RuntimeError("git %s: %s" % (a[0], r.stderr.strip()[-300:]))
        return r.stdout.strip()

    return git_update(git, "git@github.com:%s.git" % repo, branch)


def git_leftovers():
    """What an interrupted fetch or a power cut leaves in the store's .git: half-written packs (tmp_*) and lock
    files. Only this service uses the store, one run at a time, so here they are always stale - and a 150 MB
    half pack left behind once filled the partition and locked every later sync out."""
    freed = 0
    for base, _, files in os.walk(os.path.join(GIT_STORE, ".git")):
        for n in files:
            if n.startswith("tmp_") or n.startswith(".tmp-") or n.endswith(".lock"):
                path = os.path.join(base, n)
                try:
                    freed += os.path.getsize(path)
                    os.remove(path)
                except OSError:
                    pass
    if freed:
        log("removed %d MB left behind by an interrupted sync" % (freed >> 20))


def git_update(git, url, branch):
    """Bring the checkout to the branch's latest build; returns True when it changed. The gallery template makes a
    fresh commit for every build, so a change downloads the whole build again: the old objects are dropped
    BEFORE the download (the checkout the player shows stays), so the store peaks at two copies, not three."""
    git_leftovers()                              # first: a stale config.lock stops even "git remote"
    if not os.path.isdir(os.path.join(GIT_STORE, ".git")):
        os.makedirs(GIT_STORE, exist_ok=True)
        git("init", "-q")
        git("remote", "add", "origin", url)
    git("remote", "set-url", "origin", url)
    remote = git("ls-remote", "origin", "refs/heads/" + branch).split()[:1]
    if not remote:
        raise RuntimeError("no branch %s in the gallery repository (has its build run yet?)" % branch)
    old = git("rev-parse", "-q", "--verify", "HEAD", check=False)
    if remote[0] == old:
        return False
    # the old build's objects out first: HEAD unborn, no index, no packs (the files on disk stay and keep playing)
    gd = os.path.join(GIT_STORE, ".git")

    def force(fn, path, _):                      # git makes its packs read-only: allow and try again
        os.chmod(path, 0o644)
        fn(path)
    shutil.rmtree(os.path.join(gd, "objects"), onerror=force)
    if os.path.exists(os.path.join(gd, "objects")):
        raise RuntimeError("could not clear the old gallery objects")
    os.makedirs(os.path.join(gd, "objects", "pack"), exist_ok=True)
    os.makedirs(os.path.join(gd, "objects", "info"), exist_ok=True)
    for n in ("index", "shallow", "FETCH_HEAD", "ORIG_HEAD", "packed-refs"):
        try:
            os.remove(os.path.join(gd, n))
        except OSError:
            pass
    # refs too: a ref to the old build (refs/remotes/origin/...) makes the fetch bring the old build back
    shutil.rmtree(os.path.join(gd, "refs"), ignore_errors=True)
    os.makedirs(os.path.join(gd, "refs", "heads"), exist_ok=True)
    os.makedirs(os.path.join(gd, "refs", "tags"), exist_ok=True)
    shutil.rmtree(os.path.join(gd, "logs"), ignore_errors=True)
    with open(os.path.join(gd, "HEAD"), "w") as f:
        f.write("ref: refs/heads/eo1-none" + chr(10))
    try:
        git("fetch", "-q", "--depth", "1", "origin", branch)
    except Exception:
        git_leftovers()                          # never leave a half pack behind to fill the partition
        raise
    new = git("rev-parse", "FETCH_HEAD")
    git("-c", "advice.detachedHead=false", "checkout", "-q", "-f", "--detach", "FETCH_HEAD")
    git("clean", "-q", "-f", "-d")
    # no "git gc": only the new build was fetched, and repacking it would need room for a whole third copy
    git_leftovers()
    log("gallery (github, git): now at", new[:12])
    return True


def main():
    c = conf()
    # deploy key mode needs git; without it (or without the key) the API mode with a read token below
    if c.get("github_repo") and os.path.exists(DEPLOY_KEY) and shutil.which("git") and os.path.ismount(ROOT):
        try:
            changed = git_sync(c)
            changed = set_active("eo1-github") or changed
            if changed:
                notify_player()
            apply_control_from(os.path.join(GIT_STORE, "manifest.json"))
            return 0
        except Exception as e:  # noqa: BLE001 - offline or GitHub down: keep playing what is here
            log("github (git) not available:", e)
            return 1
    if not os.path.ismount("/media/eo1gallery"):
        log("gallery storage not mounted")
        return 1
    mode = "github" if c.get("github_repo") else "server"
    if mode == "github" and not os.path.exists(TOKEN):
        log("github_repo set but no token yet: using the server's playlist meanwhile")
        mode = "server"
    os.makedirs(os.path.join(STORE, "img"), exist_ok=True)
    os.makedirs(os.path.join(STORE, "vid"), exist_ok=True)
    try:
        manifest, files, headers = from_github(c) if mode == "github" else from_server(c)
    except Exception as e:  # noqa: BLE001 - offline, wrong token, no build yet: keep what is here
        log("%s not available: %s" % (mode, e))
        return 1
    try:
        index = json.load(open(INDEX))
    except (OSError, ValueError):
        index = {}
    changed = failed = 0
    for p in list(index):                             # no longer in the source: delete
        if p not in files:
            try:
                os.remove(os.path.join(STORE, p))
            except OSError:
                pass
            index.pop(p)
            changed += 1
    for p, (url, version, size, sha) in files.items():
        dst = os.path.join(STORE, p)
        if index.get(p) == version and os.path.exists(dst):
            continue
        if shutil.disk_usage(STORE).free - max(size, 5e6) < 20e6:
            log("skipping (no space):", p)
            failed += 1
            continue
        try:
            fetch(url, dst, headers, sha)
            index[p] = version
            changed += 1
            log("got", p, "%.1f MB" % (os.path.getsize(dst) / 1e6))
        except Exception as e:  # noqa: BLE001
            log("failed", p, e)
            failed += 1
    if changed:
        with open(INDEX + ".tmp", "w") as f:
            json.dump(index, f)
        os.replace(INDEX + ".tmp", INDEX)
    # the local manifest lists only what is actually here
    manifest["items"] = [it for it in manifest.get("items", [])
                         if index.get(it.get("file")) and (not it.get("poster") or index.get(it["poster"]))]
    manifest["mode"] = mode
    changed += set_active("eo1-gallery")
    new = json.dumps(manifest, ensure_ascii=False).encode()
    path = os.path.join(STORE, "manifest.json")
    old = open(path, "rb").read() if os.path.exists(path) else b""
    if new != old:
        with open(path + ".tmp", "wb") as f:
            f.write(new)
            f.flush()
            os.fsync(f.fileno())
        os.replace(path + ".tmp", path)
        changed += 1
    log("gallery (%s): %d items here, %d changes, %d failed" % (mode, len(manifest["items"]), changed, failed))
    if changed and mode == "github":
        notify_player()
    if mode == "github":
        apply_control(manifest)
    return 0 if not failed else 1


CONTROL_DONE = ROOT + "/control-applied.json"    # the control.json values the EO1 has already carried out


def agent(cmd, **params):
    """One command to the EO1's own agent (the same API the control page uses)."""
    q = urllib.parse.urlencode(dict(params, cmd=cmd, token=conf().get("token", "eo1")))
    with urllib.request.urlopen("http://127.0.0.1:2324/?" + q, timeout=30) as r:
        return json.loads(r.read().decode()).get("statustext", "")


def apply_control(manifest):
    """control.json from the gallery repo: carry out each value that changed since the last time, so a change
    made at home on the control page is not undone every 10 minutes - only a new edit on GitHub wins."""
    ctl = manifest.get("control")
    if not isinstance(ctl, dict):
        return
    try:
        done = json.load(open(CONTROL_DONE))
    except (OSError, ValueError):
        done = {}
    steps = {
        "screen": lambda v: agent("screenOn" if str(v).lower() in ("on", "true", "1") else "screenOff"),
        "brightness": lambda v: agent("setBrightness", value=round(max(1, min(100, float(v))) * 255 / 100)),
        "auto_brightness": lambda v: agent("autoBrightness", value="on" if v else "off"),
        "seconds_per_picture": lambda v: agent("interval", value=int(v)),
        "titles": lambda v: agent("titles", value="on" if v else "off"),
        "title_font": lambda v: agent("titles", font=v),
        "edges": lambda v: agent("titles", edges=v),
        "schedule": lambda v: agent("schedule", enabled="on" if v.get("enabled", True) else "off",
                                    off=v.get("off", ""), on=v.get("on", ""),
                                    off2=v.get("off2", ""), on2=v.get("on2", "")),
    }
    for key, run in steps.items():
        if key in ctl and ctl[key] != done.get(key):
            try:
                log("control.json %s=%s: %s" % (key, json.dumps(ctl[key]), run(ctl[key])))
                done[key] = ctl[key]
            except Exception as e:  # noqa: BLE001 - a wrong value or the agent busy: tried again next time
                log("control.json %s: %s" % (key, e))
    with open(CONTROL_DONE + ".tmp", "w") as f:
        json.dump(done, f)
    os.replace(CONTROL_DONE + ".tmp", CONTROL_DONE)


def apply_control_from(path):
    try:
        apply_control(json.load(open(path, encoding="utf-8")))
    except (OSError, ValueError):
        pass


def notify_player():
    """the player re-reads the gallery at its next poll"""
    try:
        pid = int(subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", "eo1-player"],
                                 capture_output=True, text=True, timeout=5).stdout.strip() or 0)
        if pid:
            os.kill(pid, signal.SIGUSR1)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass


if __name__ == "__main__":
    sys.exit(main())
