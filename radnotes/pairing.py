"""Pair your own devices with a short code like ``veggiepi-482913``.

The device showing the code listens on PAIR_PORT for a few minutes. The other
device sends the code and its Radicle ID; if the code matches, the first
device adds it as a co-editor and replies with the notes ID and its node ID.
Nothing goes through a server: the two devices talk directly, over Tailscale
or the same Wi-Fi.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .notes import PORT, device_name
from .radicle import RadError, normalize_did

PAIR_PORT = int(os.environ.get("RADNOTES_PAIR_PORT", 8777))
CODE_RE = re.compile(r"(?P<host>[A-Za-z0-9][A-Za-z0-9.-]*)-(?P<pin>\d{6})")
MAX_TRIES = 5
_direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # always talk directly


def is_device_code(code: str) -> bool:
    return bool(CODE_RE.fullmatch((code or "").strip()))


class Invitation:
    """Shows a code and waits for one device to pair with it."""

    def __init__(self, notes, minutes: float = 10, host: str | None = None, port: int = PAIR_PORT):
        notes.prepare_hub()
        self.notes, self.port = notes, port
        self.pin = f"{secrets.randbelow(10**6):06d}"
        self.code = f"{host or device_name()}-{self.pin}"
        self.expires = time.time() + minutes * 60
        self.tries = 0
        self.paired: str | None = None
        self._lock = threading.Lock()
        self._server = None

    @property
    def active(self) -> bool:
        return self.paired is None and time.time() < self.expires and self.tries < MAX_TRIES

    @property
    def state(self) -> str:
        if self.paired:
            return "paired"
        if self.tries >= MAX_TRIES:
            return "locked"
        return "waiting" if time.time() < self.expires else "expired"

    def accept(self, payload: dict) -> tuple[int, dict]:
        with self._lock:
            if not self.active:
                return 410, {"detail": "That code isn't valid anymore. Run `radnotes pair` again for a new one."}
            if not hmac.compare_digest(str(payload.get("pin", "")), self.pin):
                self.tries += 1
                return 403, {"detail": "That code doesn't match. Check the numbers and try again."}
            try:
                self.notes.allow(normalize_did(str(payload.get("did", ""))))
            except (ValueError, RadError) as exc:
                return 500, {"detail": f"The other device couldn't add you: {exc}"}
            self.paired = str(payload.get("name") or "a device")[:60]
            r = self.notes.radicle
            return 200, {"rid": r.rid(self.notes.home), "nid": r.nid(), "port": PORT}

    def start(self) -> "Invitation":
        invitation = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                status, body = 404, {"detail": "Not found"}
                if self.path == "/pair":
                    try:
                        size = min(int(self.headers.get("Content-Length", 0)), 4096)
                        status, body = invitation.accept(json.loads(self.rfile.read(size) or b"{}"))
                    except (ValueError, TypeError):
                        status, body = 400, {"detail": "Bad request"}
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        try:
            self._server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        except OSError as exc:
            raise ValueError(f"Port {self.port} is busy. Is `radnotes pair` already running on this device?") from exc
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        threading.Thread(target=self._close_when_done, daemon=True).start()
        return self

    def _close_when_done(self) -> None:
        while self.active:
            time.sleep(0.3)
        time.sleep(0.5)  # let the last reply go out
        self.close()

    def wait(self) -> str:
        while self.active:
            time.sleep(0.3)
        return self.state

    def close(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None


def _post(host: str, port: int, body: bytes) -> dict:
    req = urllib.request.Request(f"http://{host}:{port}/pair", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with _direct.open(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get("detail")
        except ValueError:
            detail = None
        raise ValueError(detail or f"The other device answered with an error ({exc.code})") from exc


def pair_with(notes, code: str, port: int = PAIR_PORT) -> dict:
    """Run on the new device with the code the other device is showing."""
    m = CODE_RE.fullmatch((code or "").strip())
    if not m:
        raise ValueError("A device code looks like veggiepi-482913")
    me = notes._require_radicle()
    body = json.dumps({"pin": m["pin"], "did": me["did"], "name": device_name()}).encode()
    host = m["host"]
    hosts = [host] if "." in host else [host, f"{host}.local"]  # Tailscale name, then local network name
    reply, last = None, None
    for h in hosts:
        try:
            reply, host = _post(h, port, body), h
            break
        except OSError as exc:  # includes URLError, timeouts and refused connections
            last = exc
    if reply is None:
        raise ValueError(f"Couldn't reach {m['host']} ({getattr(last, 'reason', last)}). Make sure it's showing this code, "
                         "and that both devices are on Tailscale or the same Wi-Fi.")
    return notes.join(reply["rid"], f"{reply['nid']}@{host}:{reply.get('port', PORT)}")
