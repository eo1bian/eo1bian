"""Connect a computer to the EO1 over SSH as easily as the QR setup: the control page asks for a one-time 4-digit
code (valid 10 minutes, used once), and one short command on the computer does the rest - it makes an SSH key if
there is none, sends the public half with the code, and adds a "Host eo1" shortcut, so `ssh eo1` just works.

  GET  /<code>              the setup script for that code: PowerShell for Windows, sh for Mac and Linux
  GET  /pair/<code>/info    the commands for the /pc page (checks the code)
  POST /pair/<code>         the public key (text) -> added to authorized_keys, SSH started
Keys are kept on the boot partition (ssh/authorized_keys, read by the initramfs at every start) and live in
/home/eo1/.ssh/authorized_keys. Ten wrong codes in a row cancel the current code."""
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time

STICK = "/media/eo1usb"
LIVE = "/home/eo1/.ssh/authorized_keys"
SAVED = STICK + "/ssh/authorized_keys"
TTL = 600
KEY_RE = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/=]{40,}( [^\r\n]{0,200})?$")
_lock = threading.Lock()
_state = {"code": None, "expires": 0, "wrong": 0}


def start():
    """A new code for the control page (replaces an older one)."""
    with _lock:
        _state.update(code="%04d" % secrets.randbelow(10000), expires=time.time() + TTL, wrong=0)
        return _state["code"], int(_state["expires"])


def check(code, use=False):
    with _lock:
        ok = bool(_state["code"]) and time.time() < _state["expires"] and secrets.compare_digest(code, _state["code"])
        if not ok:
            _state["wrong"] += 1
            if _state["wrong"] >= 10:
                _state.update(code=None, expires=0)
        elif use:
            _state.update(code=None, expires=0)
        return ok


def addresses(ip):
    host = socket.gethostname() or "eo1"
    return host, host + ".local", ip


def commands(code, ip):
    """What the computer types: the short form via port 80 and the name, and the same with the address."""
    _, local, _ = addresses(ip)
    return {"windows": "irm %s/%s|iex" % (local, code), "mac": "curl -s %s/%s|sh" % (local, code),
            "linux": "curl -s %s/%s|sh" % (local, code),
            "windowsIp": "irm %s/%s|iex" % (ip, code), "unixIp": "curl -s %s/%s|sh" % (ip, code)}


def host_key():
    """The EO1's SSH host key ("ssh-ed25519 AAAA..."), so the computer trusts it without the yes/no question
    (the one-time code already proved it is this EO1)."""
    try:
        return " ".join(open("/etc/ssh/ssh_host_ed25519_key.pub").read().split()[:2])
    except OSError:
        return ""


def invalid_script(powershell):
    """What a computer gets for a wrong or used code: a clear message instead of an HTTP error."""
    msg = "That code is not valid (any more): start again on the EO1 control page."
    return "Write-Host '%s' -ForegroundColor Red\n" % msg if powershell else "echo '%s'\n" % msg


def script(code, ip, powershell):
    host, local, _ = addresses(ip)
    url = "http://%s:2324/pair/%s" % (ip, code)
    hk = host_key()
    if powershell:
        # cmd /c for ssh-keygen: PowerShell 5 and 7 pass an empty -N "" argument differently
        return '''$ErrorActionPreference = 'Stop'
$d = Join-Path $env:USERPROFILE '.ssh'
New-Item -ItemType Directory -Force $d | Out-Null
$k = Join-Path $d 'id_ed25519'
if (-not (Test-Path $k)) { cmd /c "ssh-keygen -q -t ed25519 -N """" -f ""$k""" | Out-Null }
# the public half from the private key itself: an old id_ed25519.pub next to a newer key would not match it
$pub = (cmd /c "ssh-keygen -y -f ""$k""" | Out-String).Trim()
if (-not $pub) { $pub = (Get-Content "$k.pub" -Raw).Trim() }
$pub = ($pub -split ' ')[0..1] -join ' '
# ssh refuses a key whose .pub file does not match it: keep the old file as .pub.bak and write the right one
if ((Test-Path "$k.pub") -and -not ((Get-Content "$k.pub" -Raw).StartsWith($pub))) {
  Copy-Item "$k.pub" "$k.pub.bak" -Force; Set-Content -Path "$k.pub" -Value "$pub $env:USERNAME@$env:COMPUTERNAME" -Encoding ascii
}
$pub = "$pub $env:USERNAME@$env:COMPUTERNAME"
Invoke-RestMethod -Method Post -Uri '%(url)s' -Body $pub -ContentType 'text/plain' | Out-Null
$kh = Join-Path $d 'known_hosts'
$hk = '%(local)s,%(ip)s %(hk)s'
if ('%(hk)s' -and -not ((Test-Path $kh) -and (Select-String -Path $kh -SimpleMatch $hk -Quiet))) { Add-Content -Path $kh -Value $hk }
$cfg = Join-Path $d 'config'
if (-not ((Test-Path $cfg) -and (Select-String -Path $cfg -Pattern '^Host %(host)s$' -Quiet))) {
  Add-Content -Path $cfg -Value "`nHost %(host)s`n  HostName %(local)s`n  User eo1`n"
}
Write-Host ''
Write-Host 'Connected to your EO1. Type:  ssh %(host)s' -ForegroundColor Green
''' % {"url": url, "host": host, "local": local, "ip": ip, "hk": hk}
    # Mac and Linux: the name works on a Mac; Linux gets the address (mDNS is not everywhere)
    return """#!/bin/sh
set -e
d="$HOME/.ssh"; mkdir -p "$d"; chmod 700 "$d"
k="$d/id_ed25519"
[ -f "$k" ] || ssh-keygen -q -t ed25519 -N "" -f "$k"
# the public half from the private key itself: an old id_ed25519.pub next to a newer key would not match it
pub=$(ssh-keygen -y -f "$k" </dev/null 2>/dev/null) || pub=$( (ssh-keygen -y -f "$k" </dev/tty) 2>/dev/null) || pub=$(cat "$k.pub")
pub="$(echo "$pub" | cut -d' ' -f1-2) $(whoami)@$(hostname)"
# ssh refuses a key whose .pub file does not match it: keep the old file as .pub.bak and write the right one
[ -f "$k.pub" ] && ! grep -qF "$(echo "$pub" | cut -d' ' -f2)" "$k.pub" && cp "$k.pub" "$k.pub.bak" && echo "$pub" > "$k.pub"
printf '%%s' "$pub" | curl -fsS --data-binary @- -H 'Content-Type: text/plain' '%(url)s' >/dev/null
hn='%(local)s'; [ "$(uname)" = Darwin ] || hn='%(ip)s'
hk='%(local)s,%(ip)s %(hk)s'; [ -z '%(hk)s' ] || grep -qxF "$hk" "$d/known_hosts" 2>/dev/null || echo "$hk" >> "$d/known_hosts"
grep -q '^Host %(host)s$' "$d/config" 2>/dev/null || printf '\\nHost %(host)s\\n  HostName %%s\\n  User eo1\\n' "$hn" >> "$d/config"
echo
echo "Connected to your EO1. Type:  ssh %(host)s"
""" % {"url": url, "host": host, "local": local, "ip": ip, "hk": hk}


def keys():
    try:
        return [ln.strip() for ln in open(LIVE, encoding="utf-8") if ln.strip()]
    except OSError:
        return []


def _write(lines):
    body = "".join(ln + "\n" for ln in lines)
    os.makedirs(os.path.dirname(LIVE), exist_ok=True)
    with open(LIVE + ".tmp", "w", encoding="utf-8") as f:
        f.write(body)
    os.chmod(LIVE + ".tmp", 0o600)
    os.replace(LIVE + ".tmp", LIVE)
    for p in (os.path.dirname(LIVE), LIVE):
        os.chown(p, 1000, 1000)
    os.chmod(os.path.dirname(LIVE), 0o700)
    with open("/run/eo1-authorized_keys", "w", encoding="utf-8") as f:
        f.write(body)
    subprocess.run(["eo1-stick-write", "--put", "/run/eo1-authorized_keys", SAVED], capture_output=True, timeout=60)
    os.unlink("/run/eo1-authorized_keys")
    if lines:
        subprocess.run(["systemctl", "start", "ssh.service"], capture_output=True, timeout=30)
    else:
        subprocess.run(["systemctl", "stop", "ssh.service"], capture_output=True, timeout=30)


def add(key):
    key = " ".join(key.strip().split())
    if len(key) > 4096 or not KEY_RE.match(key):
        return False, "That is not an SSH public key"
    cur = keys()
    if any(k.split()[:2] == key.split()[:2] for k in cur):
        return True, "This computer is already connected"
    _write(cur + [key])
    return True, "Connected"


def remove(index):
    cur = keys()
    if not 0 <= index < len(cur):
        return False
    del cur[index]
    _write(cur)
    return True


def listing():
    """For the control page: the comment (usually user@computer) and type of each key, no key material."""
    out = []
    for k in keys():
        p = k.split()
        out.append({"type": p[0].replace("ssh-", ""), "name": " ".join(p[2:]) or "(no name)"})
    return out
