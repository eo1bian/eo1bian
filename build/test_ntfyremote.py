"""Offline tests for the live remote (ntfyremote.py): encryption round trip, refusal of forged, altered, reflected,
old and repeated messages, and that only the fixed commands reach the agent.
  python -I debian/build/test_ntfyremote.py        (needs the openssl command)"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "overlay", "usr", "local", "lib", "eo1"))
import ntfyremote as nr  # noqa: E402

nr.CONF = os.path.join(tempfile.mkdtemp(), "ntfy.json")
calls = []
nr._api = None                                   # pair() restarts the loops: none without an api


def check(name, ok):
    print(("ok   " if ok else "FAIL ") + name)
    if not ok:
        check.failed = True


check.failed = False
c = nr.pair()
other = dict(c, key=nr.b64(os.urandom(32)))
now = lambda: int(time.time() * 1000)

msg = {"c": "next", "ts": now(), "n": "abcdefgh12"}
sealed = nr.seal(c, "c", msg)
check("round trip", nr.unseal(c, "c", sealed) == msg)
check("another key is refused", nr.unseal(other, "c", sealed) is None)
check("a status cannot be replayed as a command", nr.unseal(c, "c", nr.seal(c, "s", msg)) is None)
m = json.loads(sealed)
ct = bytearray(nr.unb64(m["ct"]))
ct[0] ^= 1
m["ct"] = nr.b64(bytes(ct))
check("an altered message is refused", nr.unseal(c, "c", json.dumps(m)) is None)
check("garbage is refused", nr.unseal(c, "c", "not json") is None and nr.unseal(c, "c", '{"v":1}') is None)

check("a fresh command is accepted", nr.accept({"c": "next", "ts": now(), "n": "nonce-0001"}))
check("the same nonce again is refused", not nr.accept({"c": "next", "ts": now(), "n": "nonce-0001"}))
check("an old command is refused", not nr.accept({"c": "next", "ts": now() - 300_000, "n": "nonce-0002"}))
check("a command from the future is refused", not nr.accept({"c": "next", "ts": now() + 300_000, "n": "nonce-0003"}))
check("a short nonce is refused", not nr.accept({"c": "next", "ts": now(), "n": "x"}))


def api(cmd, raw=False, **p):
    calls.append((cmd, p))
    return "ok"


check("next goes to the agent", nr.run_command(api, {"c": "next"}) == "ok" and calls[-1] == ("next", {}))
check("brightness in % -> 0-255", nr.run_command(api, {"c": "brightness", "v": 40}) == "ok"
      and calls[-1] == ("setBrightness", {"value": 102}))
n = len(calls)
check("an unknown command does nothing", nr.run_command(api, {"c": "update"}) == "unknown command" and len(calls) == n)
check("a shell-like command does nothing", nr.run_command(api, {"c": "rm -rf /"}) == "unknown command" and len(calls) == n)
check("watch opens the status window", nr.run_command(api, {"c": "watch"}) == "watching"
      and nr._state["watch_until"] > time.time())
nr._state["sent"] = ["local-abc"]
check("thumb asks for the picture again", nr.run_command(api, {"c": "thumb"}).startswith("sending")
      and nr._state["sent"] == [] and len(calls) == n)
check("albums: a list goes to the agent", nr.run_command(api, {"c": "albums", "v": ["mummi-ai"]}) == "ok"
      and calls[-1] == ("albums", {"value": '["mummi-ai"]'}))
check("albums: empty = all", nr.run_command(api, {"c": "albums", "v": []}) == "ok" and calls[-1] == ("albums", {"value": "all"}))
n = len(calls)
check("albums: not a list does nothing", nr.run_command(api, {"c": "albums", "v": "rm -rf /"}).startswith("albums:")
      and len(calls) == n)
check("auto on -> autoBrightness on", nr.run_command(api, {"c": "auto", "v": True}) == "ok"
      and calls[-1] == ("autoBrightness", {"value": "on"}))
check("auto anything else -> off", nr.run_command(api, {"c": "auto", "v": "yes"}) == "ok"
      and calls[-1] == ("autoBrightness", {"value": "off"}))
check("interval keep", nr.run_command(api, {"c": "interval", "v": "keep"}) == "ok" and calls[-1] == ("interval", {"value": "keep"}))
check("interval 300 s", nr.run_command(api, {"c": "interval", "v": 300}) == "ok" and calls[-1] == ("interval", {"value": "300"}))
n = len(calls)
check("interval: nonsense does nothing", nr.run_command(api, {"c": "interval", "v": "1; reboot"}).startswith("interval:") and len(calls) == n)
check("shuffle on", nr.run_command(api, {"c": "shuffle", "v": True}) == "ok" and calls[-1] == ("shuffle", {"value": "on"}))
check("edges mat", nr.run_command(api, {"c": "edges", "v": "mat"}) == "ok" and calls[-1] == ("titles", {"edges": "mat"}))
n = len(calls)
check("edges: other values do nothing", nr.run_command(api, {"c": "edges", "v": "../x"}).startswith("edges:") and len(calls) == n)
check("show a picture id", nr.run_command(api, {"c": "show", "v": "local-up-123"}) == "ok" and calls[-1] == ("show", {"id": "local-up-123"}))
n = len(calls)
check("show: not an id does nothing", nr.run_command(api, {"c": "show", "v": "/etc/passwd"}).startswith("show:") and len(calls) == n)
check("sheet is queued for the status loop", nr.run_command(api, {"c": "sheet", "v": "mummi-ai"}) == "sheet requested"
      and nr._state["sheet"] == "mummi-ai" and len(calls) == n)
link = nr.link(c)
check("the pairing link keeps the secret in the fragment", link.startswith(nr.REMOTE_PAGE + "#pair="))
# an own ntfy server with an access token: in the link for the phone, on every request, kept by "New key"
import importlib.util  # noqa: E402
spec = importlib.util.spec_from_file_location("gate", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ntfy",
                                                                      "eo1-ntfy-filter.py"))
gate = importlib.util.module_from_spec(spec)
sys.argv = sys.argv[:1]
spec.loader.exec_module(gate)
check("the server filter lets a sealed message through", gate.eo1_message(nr.seal(c, "s", {"x": 1}).encode()))
check("the server filter refuses plain text", not gate.eo1_message(b'{"message": "hi"}'))
t = nr.pair("https://ntfy.example.com/", "tk_abcdefghijklmnopqrstuvwxyz123")
blob = json.loads(nr.unb64(nr.link(t).split("#pair=")[1]))
check("a token server: the link carries the token", blob.get("a") == t["token"] and blob["s"] == "https://ntfy.example.com")
check("a token server: requests carry it", nr.auth(t) == {"Authorization": "Bearer " + t["token"]})
k = nr.pair()
check("New key keeps the server and its token", k["server"] == t["server"] and k["token"] == t["token"] and k["key"] != t["key"])
b = nr.pair("https://ntfy.sh")
check("back to ntfy.sh: no token", "token" not in b and nr.auth(b) == {} and "a" not in json.loads(nr.unb64(nr.link(b).split("#pair=")[1])))
sys.exit(1 if check.failed else 0)
