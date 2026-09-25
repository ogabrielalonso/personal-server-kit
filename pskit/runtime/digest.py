"""`pskit digest`: the daily summary. State first, then the day's events."""

from __future__ import annotations

import json
import os
import time
from typing import List, Optional

from .. import notify
from ..budget import human
from ..checks import receipt
from ..config import HostConfig, KitConfig
from ..i18n import t_in
from ..paths import SYS
from ..system import Host


def dir_size(path: str, limit_files: int = 400000) -> Optional[int]:
    total, n = 0, 0
    try:
        for root, _dirs, files in os.walk(path, followlinks=False):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_blocks * 512
                except OSError:
                    continue
                n += 1
                if n > limit_files:
                    return total
    except OSError:
        return None
    return total


def lines(host: Host, cfg: HostConfig, kit: KitConfig, now: Optional[float] = None) -> List[str]:
    now = now or time.time()
    lang = cfg.language
    out: List[str] = []
    health = receipt(host, "health") or {}
    problems = health.get("problems", [])
    if problems:
        out.append(t_in(lang, "digest.open_problems", n=len(problems)))
        out += [f"- {p['text']}" for p in problems]
    else:
        out.append(t_in(lang, "digest.all_ok"))
    for path in ("/", cfg.workspace, cfg.brain_home):
        if not path or not host.exists(path):
            continue
        total, used, free = host.disk_usage(path)
        pct = int(100 * used / max(1, used + free) + 0.999)
        out.append(t_in(lang, "digest.disk", path=path, pct=pct, free=human(free)))
    if kit.backup_kind == "later":
        out.append(t_in(lang, "alert.backup_not_configured"))
    else:
        rec = receipt(host, "backup-success")
        if rec:
            hours = int((now - float(rec.get("finished", 0))) / 3600)
            out.append(t_in(lang, "digest.backup_last", hours=hours))
        else:
            out.append(t_in(lang, "alert.backup_never"))
    if host.exists("/run/reboot-required"):
        out.append(t_in(lang, "digest.reboot_pending"))
    # Agent histories (Codex, Claude, Grok) are what usually fills the
    # root disk: report, never delete.
    home = host.user_home(cfg.owner) if cfg.owner else None
    if home:
        sizes = []
        for label, rel in (("Claude", ".claude"), ("Codex", ".codex"), ("Grok", ".grok")):
            p = host.p(f"{home}/{rel}")
            if p.exists():
                size = dir_size(str(p))
                if size:
                    sizes.append(f"{label} {human(size)}")
        if sizes:
            out.append(t_in(lang, "digest.agent_history", sizes=", ".join(sizes)))
    try:
        devices = json.loads(host.read_text(SYS.devices, "{}") or "{}")
    except json.JSONDecodeError:
        devices = {}
    for name in sorted(devices):
        raw = host.read_text(f"{cfg.brain_home}/{cfg.session_inbox}/{name}/.pskit-delivery.json")
        if raw:
            try:
                d = json.loads(raw)
                hours = int((now - float(d.get("finished", 0))) / 3600)
                out.append(t_in(lang, "digest.device", device=name, hours=hours, files=d.get("files", 0)))
                continue
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        out.append(t_in(lang, "digest.device_never", device=name))
    return out


def run(host: Host, cfg: HostConfig, kit: KitConfig, channel: Optional[notify.Channel] = None) -> bool:
    header = t_in(cfg.language, "digest.title", date=time.strftime("%Y-%m-%d"))
    return notify.flush_digest(host, cfg, header, lines(host, cfg, kit), channel=channel)
