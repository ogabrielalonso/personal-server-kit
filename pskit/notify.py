"""Alerts to the owner.

Anyone in the kit group (the owner, the brain) calls `pskit notify`, which
drops a small JSON file into a spool directory. Only root delivers, so the
channel credential never leaves /etc/pskit/secrets. Policy:

  critical, warn  sent now
  info            one line in the daily digest, never an interruption

A message with a key is suppressed while an identical key was sent within
its cooldown. Failed deliveries stay queued and are retried by the health
timer, so an outage of the channel loses nothing.
"""

from __future__ import annotations

import fcntl
import itertools
import json
import os
import secrets as _secrets
import stat
import time
import urllib.parse
import urllib.request
from typing import ClassVar, Dict, List, Optional, Tuple

from .config import HostConfig, read_env_file
from .i18n import t_in
from .paths import SYS
from .system import Host

SEVERITIES = ("critical", "warn", "info")
MAX_TITLE = 200
MAX_BODY = 3000
MAX_FILE_BYTES = 16384
SECRETS_FILE = f"{SYS.secrets}/notify.env"
RETRY_DIR = f"{SYS.state}/notify-retry"
COOLDOWN_FILE = f"{SYS.state}/notify-cooldown.json"


class DeliveryError(RuntimeError):
    pass


_SEQ = itertools.count()


def enqueue(host: Host, severity: str, title: str, body: str = "", key: str = "",
            cooldown_hours: float = 0, source: str = "") -> str:
    if severity not in SEVERITIES:
        raise ValueError(f"severity must be one of {SEVERITIES}")
    if not title.strip():
        raise ValueError("title is required")
    msg = {
        "v": 1,
        "severity": severity,
        "title": title.strip()[:MAX_TITLE],
        "body": body.strip()[:MAX_BODY],
        "key": key[:120],
        "cooldown_hours": float(cooldown_hours),
        "source": (source or os.environ.get("USER", ""))[:60],
        "ts": time.time(),
    }
    # Delivery follows file names: nanoseconds, then a per-process counter, so
    # messages queued within the same clock tick keep their order.
    name = f"{time.time_ns()}-{next(_SEQ):06d}-{_secrets.token_hex(4)}.json"
    path = f"{SYS.spool}/{name}"
    host.write_atomic(path, json.dumps(msg), mode=0o660)
    return path


# ---- channels ---------------------------------------------------------

def _post(url: str, data: bytes, headers: Dict[str, str], timeout: float = 15) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https endpoints
        return resp.status, resp.read()


class Channel:
    name = "none"

    def send(self, text: str, severity: str) -> None:
        # Log-only channel: the journal (stdout of the unit) keeps the record.
        print(f"[notify:{severity}] {text}")


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, token: str, chat_id: str, poster=_post):
        self.token = token
        self.chat_id = chat_id
        self.poster = poster

    def send(self, text: str, severity: str) -> None:
        data = urllib.parse.urlencode({
            "chat_id": self.chat_id,
            "text": text[:4000],
            "disable_web_page_preview": "true",
        }).encode()
        try:
            status, raw = self.poster(f"https://api.telegram.org/bot{self.token}/sendMessage", data,
                                      {"Content-Type": "application/x-www-form-urlencoded"})
        except Exception as exc:
            raise DeliveryError(f"telegram: {type(exc).__name__}") from None
        try:
            ok = json.loads(raw.decode() or "{}").get("ok", False)
        except json.JSONDecodeError:
            ok = False
        if status != 200 or not ok:
            raise DeliveryError(f"telegram: status {status}")


class NtfyChannel(Channel):
    """ntfy JSON publishing: UTF-8 safe for titles in any language."""

    name = "ntfy"
    PRIORITY: ClassVar[Dict[str, int]] = {"critical": 5, "warn": 4, "info": 2}

    def __init__(self, server: str, topic: str, token: str = "", poster=_post):
        self.server = server.rstrip("/")
        self.topic = topic
        self.token = token
        self.poster = poster

    def send(self, text: str, severity: str) -> None:
        title, _, body = text.partition("\n")
        payload = {"topic": self.topic, "title": title[:200], "message": body.strip() or title,
                   "priority": self.PRIORITY.get(severity, 3)}
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            status, _ = self.poster(self.server + "/", json.dumps(payload).encode("utf-8"), headers)
        except Exception as exc:
            raise DeliveryError(f"ntfy: {type(exc).__name__}") from None
        if status >= 300:
            raise DeliveryError(f"ntfy: status {status}")


def channel_from_config(host: Host, cfg: HostConfig) -> Channel:
    env = read_env_file(host, SECRETS_FILE)
    if cfg.alert_channel == "telegram" and env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        return TelegramChannel(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"])
    if cfg.alert_channel == "ntfy" and env.get("NTFY_TOPIC"):
        return NtfyChannel(env.get("NTFY_SERVER", "https://ntfy.sh"), env["NTFY_TOPIC"], env.get("NTFY_TOKEN", ""))
    return Channel()


# ---- delivery ---------------------------------------------------------

def format_message(cfg: HostConfig, title: str, body: str) -> str:
    head = f"{cfg.machine_name}: {title}"
    return f"{head}\n\n{body}" if body else head


def _cooldown_ok(host: Host, key: str, hours: float, now: float) -> bool:
    if not key or hours <= 0:
        return True
    try:
        state = json.loads(host.read_text(COOLDOWN_FILE, "{}") or "{}")
    except json.JSONDecodeError:
        state = {}
    last = state.get(key)
    if last is not None and now - last < hours * 3600:
        return False
    state[key] = now
    # Forget keys older than a week so the file stays small.
    state = {k: v for k, v in state.items() if now - v < 7 * 86400}
    host.write_atomic(COOLDOWN_FILE, json.dumps(state), mode=0o600)
    return True


def append_digest(host: Host, title: str, source: str = "") -> None:
    target = host.p(SYS.digest)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time(), "title": title, "source": source}) + "\n")


def _pending_files(host: Host) -> List[str]:
    out = []
    for d in (RETRY_DIR, SYS.spool):
        base = host.p(d)
        if not base.is_dir():
            continue
        for p in sorted(base.iterdir(), key=lambda x: x.name):
            if p.name.endswith(".json") and not p.name.startswith("."):
                out.append(f"{d}/{p.name}")
    return out


def _read_message(host: Host, path: str) -> Optional[bytes]:
    """Reads a spool file the way root must read a file written by others:
    no symlinks, regular files only, never more than the size limit."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(str(host.p(path)), flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE_BYTES:
            return None
        return os.read(fd, MAX_FILE_BYTES + 1)
    finally:
        os.close(fd)


def _sweep(host: Host, now: float, min_age: float = 120.0) -> int:
    """Removes what is not a message (dead temporary files, folders, other
    names) so the path unit, which fires while the spool is not empty, cannot
    loop on leftovers. Young entries may be a write in progress."""
    removed = 0
    base = host.p(SYS.spool)
    if not base.is_dir():
        return 0
    for p in base.iterdir():
        if p.name.endswith(".json") and not p.name.startswith("."):
            continue
        try:
            age = now - p.lstat().st_mtime
        except OSError:
            continue
        if age >= min_age:
            host.remove(f"{SYS.spool}/{p.name}")
            removed += 1
    return removed


def _leftovers(host: Host) -> bool:
    base = host.p(SYS.spool)
    return base.is_dir() and any(not (p.name.endswith(".json") and not p.name.startswith("."))
                                 for p in base.iterdir())


def deliver(host: Host, cfg: HostConfig, channel: Optional[Channel] = None,
            now: Optional[float] = None, sweep_wait: bool = False) -> Dict[str, int]:
    """Delivers everything queued. Root only. Safe to call concurrently."""
    channel = channel or channel_from_config(host, cfg)
    now = now or time.time()
    stats = {"sent": 0, "digest": 0, "suppressed": 0, "failed": 0, "invalid": 0}
    lock_path = host.p(f"{SYS.state}/notify.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for path in _pending_files(host):
            raw = _read_message(host, path)
            try:
                if raw is None or len(raw) > MAX_FILE_BYTES:
                    raise ValueError("not a regular message file")
                msg = json.loads(raw.decode("utf-8"))
                sev = msg["severity"]
                if sev not in SEVERITIES:
                    raise ValueError("severity")
                title = str(msg["title"])[:MAX_TITLE]
                body = str(msg.get("body", ""))[:MAX_BODY]
                key = str(msg.get("key", ""))[:120]
                cooldown = float(msg.get("cooldown_hours", 0) or 0)
                source = str(msg.get("source", ""))[:60]
            except (ValueError, KeyError, TypeError, AttributeError, UnicodeDecodeError):
                stats["invalid"] += 1
                host.remove(path)
                continue
            if not _cooldown_ok(host, key, cooldown, now):
                stats["suppressed"] += 1
                host.remove(path)
                continue
            if sev == "info":
                append_digest(host, title, source)
                stats["digest"] += 1
                host.remove(path)
                continue
            try:
                channel.send(format_message(cfg, title, body), sev)
            except DeliveryError as exc:
                stats["failed"] += 1
                print(f"[notify] delivery failed, kept for retry: {exc}")
                if not path.startswith(RETRY_DIR):
                    name = path.rsplit("/", 1)[1]
                    host.write_atomic(f"{RETRY_DIR}/{name}", raw, mode=0o600)
                    host.remove(path)
                # Stop at the first failure: order is preserved and the
                # channel is probably down for everything else too.
                break
            stats["sent"] += 1
            host.remove(path)
        # A write in progress looks like a leftover for a moment: give it a
        # few seconds, then clear what is still not a message.
        for _ in range(6 if sweep_wait else 0):
            if not _leftovers(host):
                break
            time.sleep(2)
        stats["swept"] = _sweep(host, time.time() if sweep_wait else now)
    return stats


def flush_digest(host: Host, cfg: HostConfig, header: str, extra_lines: List[str],
                 channel: Optional[Channel] = None) -> bool:
    """Sends the daily summary: state lines first, then the day's info
    events grouped with counts. Returns True when something was sent."""
    channel = channel or channel_from_config(host, cfg)
    raw = host.read_text(SYS.digest, "") or ""
    grouped: Dict[str, int] = {}
    for line in raw.splitlines():
        try:
            title = json.loads(line)["title"]
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
        grouped[title] = grouped.get(title, 0) + 1
    lines = list(extra_lines)
    if grouped:
        lines.append("")
        lines.append(t_in(cfg.language, "digest.events"))
        for title, n in grouped.items():
            lines.append(f"- {title}" + (f" ({n}x)" if n > 1 else ""))
    if not lines:
        return False
    channel.send(format_message(cfg, header, "\n".join(lines)), "info")
    host.write_atomic(SYS.digest, "", mode=0o600)
    return True
