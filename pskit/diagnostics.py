"""The diagnostic file: one JSON file the owner can send when something
fails. Technical fields in English; secrets never included; anything that
looks like a token, key, password or public address is redacted."""

from __future__ import annotations

import getpass
import ipaddress
import json
import os
import platform
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import VERSION
from .config import HostConfig, KitConfig
from .paths import SYS
from .system import Host

_PATTERNS = [
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,64}\b"), "<telegram-token>"),
    (re.compile(r"bot\d{6,12}:[A-Za-z0-9_-]+"), "bot<telegram-token>"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<aws-key-id>"),
    (re.compile(r"\bK00[0-9A-Za-z]{20,}\b"), "<b2-key-id>"),
    (re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key|access[_-]?key)(\s*[=:]\s*)\S+"), r"\1\2<redacted>"),
    (re.compile(r"https://hc-ping\.com/[0-9a-f-]{20,}"), "https://hc-ping.com/<redacted>"),
    (re.compile(r"\bpskit-[a-z0-9-]+-[0-9a-f]{16}\b"), "<ntfy-topic>"),
    (re.compile(r"\btskey-[A-Za-z0-9-]+"), "<tailscale-key>"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), "<private-key>"),
]
_IPV4 = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
_IPV6 = re.compile(r"(?<![0-9A-Za-z:])(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{1,4})*(?![0-9A-Za-z:])")
_TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def _mask_ip6(m: re.Match[str]) -> str:
    text = m.group(0)
    try:
        ip = ipaddress.IPv6Address(text)
    except ValueError:
        return text
    if ip.is_global and ip not in _TAILNET_V6:
        return text.split(":")[0] + ":x:x::x"
    return text


def _mask_ip(m: re.Match[str]) -> str:
    a, b = int(m.group(1)), int(m.group(2))
    if a == 127 or a == 10 or (a == 100 and 64 <= b <= 127) or (a == 192 and b == 168) or \
            (a == 172 and 16 <= b <= 31) or a == 0:
        return m.group(0)
    return f"{a}.x.x.x"


def redact(text: str) -> str:
    for pat, rep in _PATTERNS:
        text = pat.sub(rep, text)
    return _IPV6.sub(_mask_ip6, _IPV4.sub(_mask_ip, text))


def _redact_obj(obj: Any) -> Any:
    if isinstance(obj, str):
        return redact(obj)
    if isinstance(obj, list):
        return [_redact_obj(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _redact_obj(v) for k, v in obj.items()}
    return obj


def collect(host: Host, cfg: Optional[HostConfig], kit: Optional[KitConfig]) -> Dict[str, Any]:
    from . import doctor
    data: Dict[str, Any] = {
        "kind": "pskit-diagnostic",
        "version": VERSION,
        "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "platform": host.platform,
        "os": host.os_release().get("PRETTY_NAME") if host.platform == "linux" else platform.mac_ver()[0],
        "kernel": os.uname().release,
        "arch": os.uname().machine,
        "host_config": json.loads(cfg.to_json()) if cfg else None,
        "kit_config": json.loads(kit.to_json()) if kit else None,
    }
    for name, path in (("journal", SYS.journal), ("devices", SYS.devices)):
        raw = host.read_text(path)
        if raw:
            try:
                data[name] = json.loads(raw)
            except json.JSONDecodeError:
                data[name] = "unreadable"
    ledger = host.read_text(SYS.ledger, "") or ""
    counts: Dict[str, int] = {}
    for line in ledger.splitlines():
        try:
            k = json.loads(line).get("kind", "?")
        except json.JSONDecodeError:
            continue
        counts[k] = counts.get(k, 0) + 1
    data["ledger_counts"] = counts
    receipts = {}
    base = host.p(SYS.receipts)
    if base.is_dir():
        for p in sorted(base.glob("*.json")):
            try:
                receipts[p.stem] = json.loads(p.read_text())
            except (OSError, json.JSONDecodeError):
                receipts[p.stem] = "unreadable"
    data["receipts"] = receipts
    data["secrets_present"] = sorted(p.name for p in host.p(SYS.secrets).iterdir()) \
        if host.p(SYS.secrets).is_dir() and os.access(host.p(SYS.secrets), os.R_OK) else "not readable"
    try:
        data["doctor"] = [vars(i) for i in (doctor.server_items(host) if cfg else doctor.audit_items(host))]
    except Exception as exc:
        data["doctor_error"] = f"{type(exc).__name__}: {exc}"
    if host.platform == "linux":
        r = host.run(["journalctl", "--no-pager", "-o", "short-iso", "-n", "400", "--since", "-2d",
                      "-u", "pskit-*"], timeout=60)
        data["logs"] = r.stdout.splitlines()[-400:]
        r = host.run(["systemctl", "list-units", "--all", "--plain", "--no-legend", "pskit-*"])
        data["units"] = r.stdout.splitlines()
    else:
        logs = {}
        base = host.p("/var/log/pskit")
        if base.is_dir():
            for p in base.glob("*.log"):
                try:
                    logs[p.name] = p.read_text(errors="replace").splitlines()[-80:]
                except OSError:
                    pass
        data["logs"] = logs
    return _redact_obj(data)


def _usable(path: Path) -> bool:
    return path.is_dir() and os.access(path, os.W_OK)


def _invoking_user() -> Optional[str]:
    try:
        return os.environ.get("SUDO_USER") or getpass.getuser()
    except (KeyError, OSError):
        return None


def _target_dir(host: Host, cfg: Optional[HostConfig]) -> Tuple[Path, bool]:
    """Where the file goes, and whether that folder is someone's home (the
    file then belongs to them). Nothing installed means no owner and no state
    folder: the invoking user's home, then the current folder (the process's
    own, not under the host root: tests that reach it set the cwd)."""
    owner_home = host.user_home(cfg.owner) if cfg and cfg.owner else None
    if owner_home and _usable(host.p(owner_home)):
        return host.p(owner_home), True
    if _usable(host.p(SYS.state)):
        return host.p(SYS.state), False
    user = _invoking_user()
    home = host.user_home(user) if user else None
    if home and _usable(host.p(home)):
        return host.p(home), True
    return Path.cwd(), False


def write(host: Host, cfg: Optional[HostConfig], kit: Optional[KitConfig]) -> Path:
    data = collect(host, cfg, kit)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target_dir, is_home = _target_dir(host, cfg)
    path = target_dir / f"pskit-diagnostic-{stamp}.json"
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False, default=str) + "\n")
    os.chmod(path, 0o600)
    if is_home and host.real and os.geteuid() == 0:
        st = os.stat(target_dir)
        os.chown(path, st.st_uid, st.st_gid)
    return path
