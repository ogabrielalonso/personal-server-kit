"""One-time pairing between the server and the owner's laptop.

The server listens on its tailnet address only, for 15 minutes, for one
request carrying the code shown on its screen. The code proves the request
comes from the person at the installer; Tailscale already encrypts and
authenticates the path. Five wrong codes end the session.
"""

from __future__ import annotations

import hmac
import json
import secrets
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Dict, Optional

from .. import VERSION, CONTRACT_VERSION
from ..config import DEVICE_RE, HostConfig, KitConfig
from ..paths import SYS
from ..state import Ledger
from ..system import Host
from . import keys as K

ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
MAX_ATTEMPTS = 5
MAX_BODY = 16384


def new_code() -> str:
    raw = "".join(secrets.choice(ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def normalize(code: str) -> str:
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


class PairingSession:
    def __init__(self, host: Host, ledger: Ledger, cfg: HostConfig, kit: KitConfig, code: str,
                 now: Callable[[], float] = time.time, ttl_s: float = 900):
        self.host = host
        self.ledger = ledger
        self.cfg = cfg
        self.kit = kit
        self.code = normalize(code)
        self.attempts = 0
        self.deadline = now() + ttl_s
        self.now = now
        self.result: Optional[Dict] = None
        self.failed: Optional[str] = None

    def handle(self, body: bytes) -> Dict:
        if self.now() > self.deadline:
            self.failed = "expired"
            return {"status": 410, "error": "expired"}
        if self.result is not None:
            return {"status": 409, "error": "already paired"}
        try:
            req = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"status": 400, "error": "bad request"}
        if not hmac.compare_digest(normalize(str(req.get("code", ""))), self.code):
            self.attempts += 1
            if self.attempts >= MAX_ATTEMPTS:
                self.failed = "too many attempts"
            return {"status": 403, "error": "wrong code", "attempts_left": max(0, MAX_ATTEMPTS - self.attempts)}
        device = str(req.get("device", ""))
        if not DEVICE_RE.match(device):
            return {"status": 400, "error": "bad device name"}
        raw_keys = req.get("keys") or {}
        try:
            keys = {
                "human": K.parse_key(raw_keys.get("human", "")),
                "tunnel": K.parse_key(raw_keys.get("tunnel", ""), require_ed25519=True),
                "bridge": K.parse_key(raw_keys.get("bridge", ""), require_ed25519=True),
            }
        except K.KeyError_ as exc:
            return {"status": 400, "error": str(exc)}
        K.install_device(self.host, self.ledger, self.cfg.owner, device, keys, self.cfg.brain_port)
        self._record_device(device, str(req.get("os", ""))[:40])
        self.result = {"device": device}
        return {
            "status": 200,
            "contract_version": CONTRACT_VERSION,
            "server_version": VERSION,
            "machine_name": self.cfg.machine_name,
            "owner": self.cfg.owner,
            "language": self.cfg.language,
            "tailnet_ip": self.kit.tailnet_ip,
            "tailnet_name": self.kit.tailnet_name,
            "ssh_port": 22,
            "host_keys": K.host_public_keys(self.host),
            "brain_port": self.cfg.brain_port,
        }

    def _record_device(self, device: str, os_name: str) -> None:
        try:
            devices = json.loads(self.host.read_text(SYS.devices, "{}") or "{}")
        except json.JSONDecodeError:
            devices = {}
        devices[device] = {"paired_at": self.now(), "os": os_name}
        self.host.write_atomic(SYS.devices, json.dumps(devices, indent=1, sort_keys=True), mode=0o644)

    @property
    def finished(self) -> bool:
        return self.result is not None or self.failed is not None or self.now() > self.deadline


def serve(session: PairingSession, bind: str, port: int, poll_s: float = 0.5,
          on_tick: Optional[Callable[[int], None]] = None) -> PairingSession:
    class Handler(BaseHTTPRequestHandler):
        server_version = "pskit-pair"
        sys_version = ""

        def log_message(self, fmt, *args):  # quiet: the installer shows progress
            return

        def _send(self, status: int, payload: Dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/ping":
                self._send(200, {"pskit": VERSION, "machine": session.cfg.machine_name})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/pair":
                self._send(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._send(413, {"error": "too large"})
                return
            answer = session.handle(self.rfile.read(length))
            status = answer.pop("status")
            self._send(status, answer)

    httpd = HTTPServer((bind, port), Handler)
    httpd.timeout = poll_s
    try:
        last_tick = 0.0
        while not session.finished:
            httpd.handle_request()
            if on_tick and time.time() - last_tick >= 5:
                on_tick(int(session.deadline - session.now()))
                last_tick = time.time()
    finally:
        httpd.server_close()
    return session
