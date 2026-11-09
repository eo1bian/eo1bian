#!/usr/bin/env python3
"""A gate in front of an ntfy server that carries only EO1 live-remote messages.

nginx sends every publish (PUT/POST on /eo1-<topic>) here instead of to ntfy. A body gets through only when it is
exactly what an EO1 or the remote page sends: {"v":1,"iv","ct","mac"}, base64url, a 16-byte IV and a 32-byte MAC.
The server cannot read those messages (they are encrypted end to end), but it can tell them from anything else, so
the server is useless as a general push service: plain text, ntfy actions, e-mail or phone-call headers and links
never reach ntfy. Only a few harmless headers are passed on, and the query string is dropped except ?auth=.

Standard library only. Run: python3 eo1-ntfy-filter.py [listen_port] [ntfy_url]   (default 8098, http://127.0.0.1:8097)
"""
import base64
import http.client
import json
import re
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LISTEN = int(sys.argv[1]) if len(sys.argv) > 1 else 8098
NTFY = urllib.parse.urlsplit(sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8097")
MAX_BODY = 2 * 1024 * 1024                        # an album sheet (80 small pictures at most) is ~1 MB sealed
PATH_RE = re.compile(r"^/eo1-[A-Za-z0-9_-]{1,60}$")
B64_RE = re.compile(r"^[A-Za-z0-9_-]+$")
PASS_HEADERS = ("Authorization", "Filename", "Cache", "Title", "Content-Type", "X-Forwarded-For", "X-Real-IP")


def unb64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def eo1_message(body):
    """True when the body is an EO1 sealed message and nothing else."""
    try:
        m = json.loads(body)
    except ValueError:
        return False
    if not isinstance(m, dict) or set(m) != {"v", "iv", "ct", "mac"} or m["v"] != 1:
        return False
    if not all(isinstance(m[k], str) and B64_RE.match(m[k]) for k in ("iv", "ct", "mac")):
        return False
    try:
        return len(unb64(m["iv"])) == 16 and len(unb64(m["mac"])) == 32 and len(unb64(m["ct"])) > 0
    except ValueError:
        return False


class Gate(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "eo1-ntfy-filter"
    sys_version = ""

    def log_message(self, fmt, *args):            # nginx keeps the access log
        pass

    def answer(self, code, text):
        body = json.dumps({"http": code, "error": text}).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def publish(self):
        url = urllib.parse.urlsplit(self.path)
        if not PATH_RE.match(url.path):
            return self.answer(404, "not found")
        try:
            size = int(self.headers.get("Content-Length", ""))
        except ValueError:
            return self.answer(411, "length required")
        if not 0 < size <= MAX_BODY:
            return self.answer(413, "too large" if size > MAX_BODY else "empty")
        body = self.rfile.read(size)
        if not eo1_message(body):
            return self.answer(400, "only EO1 live-remote messages")
        query = [(k, v) for k, v in urllib.parse.parse_qsl(url.query) if k == "auth"]
        target = url.path + ("?" + urllib.parse.urlencode(query) if query else "")
        headers = {h: self.headers[h] for h in PASS_HEADERS if self.headers.get(h)}
        if headers.get("Filename") and not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", headers["Filename"]):
            del headers["Filename"]
        headers["Content-Length"] = str(len(body))
        try:
            conn = http.client.HTTPConnection(NTFY.hostname, NTFY.port or 80, timeout=30)
            conn.request(self.command, target, body=body, headers=headers)
            r = conn.getresponse()
            data = r.read()
            conn.close()
        except OSError:
            return self.answer(502, "ntfy unreachable")
        self.send_response(r.status)
        for h in ("Content-Type", "Access-Control-Allow-Origin", "Access-Control-Expose-Headers"):
            if r.getheader(h):
                self.send_header(h, r.getheader(h))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_PUT = do_POST = publish

    def do_GET(self):
        self.answer(404, "not found")


if __name__ == "__main__":
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer(("127.0.0.1", LISTEN), Gate).serve_forever()
