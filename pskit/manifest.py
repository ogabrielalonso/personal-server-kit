"""Brain manifest: what the brain layer declares to the host.

JSON, so it parses with the standard library on Python 3.9. See
docs/HOST-BRAIN-CONTRACT.md and examples/brain-manifest.json.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

MANIFEST_VERSION = 0
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")


class ManifestError(ValueError):
    def __init__(self, problems: List[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


@dataclass
class Schedule:
    every_minutes: Optional[int] = None
    daily_at: List[str] = field(default_factory=list)
    weekly_day: Optional[str] = None
    weekly_at: Optional[str] = None

    def systemd_timer_lines(self) -> List[str]:
        if self.every_minutes:
            return ["OnBootSec=5min", f"OnUnitActiveSec={self.every_minutes}min"]
        if self.daily_at:
            return [f"OnCalendar=*-*-* {t}:00" for t in self.daily_at] + ["Persistent=true"]
        if self.weekly_day and self.weekly_at:
            return [f"OnCalendar={self.weekly_day.capitalize()} *-*-* {self.weekly_at}:00", "Persistent=true"]
        return []


@dataclass
class Service:
    name: str
    kind: str  # "resident" or "scheduled"
    command: List[str]
    heavy_lock: bool = False
    schedule: Optional[Schedule] = None
    timeout_minutes: int = 120
    description: str = ""


@dataclass
class Manifest:
    name: str
    services: List[Service]
    health_url: Optional[str]
    health_expect: str
    health_timeout: int
    backup_include: List[str]
    backup_exclude: List[str]
    peak_resident_mib: int
    peak_batch_mib: int
    e2e_command: Optional[List[str]]
    e2e_timeout: int
    session_inbox: str
    environment: Dict[str, str]

    @property
    def peak_bytes(self) -> int:
        # One resident service set plus one heavy batch at a time (the lock
        # serializes batches), so the peak is the sum of the two.
        return (self.peak_resident_mib + self.peak_batch_mib) * 1024 * 1024


def _rel_path_ok(p: Any) -> bool:
    return isinstance(p, str) and p != "" and not p.startswith("/") and ".." not in p.split("/")


def _schedule(raw: Any, where: str, problems: List[str]) -> Optional[Schedule]:
    if not isinstance(raw, dict):
        problems.append(f"{where}.schedule must be an object")
        return None
    s = Schedule()
    if "every_minutes" in raw:
        v = raw["every_minutes"]
        if not isinstance(v, int) or not (5 <= v <= 1440):
            problems.append(f"{where}.schedule.every_minutes must be an integer between 5 and 1440")
        else:
            s.every_minutes = v
    elif "daily_at" in raw:
        v = raw["daily_at"]
        v = [v] if isinstance(v, str) else v
        if not isinstance(v, list) or not v or not all(isinstance(x, str) and TIME_RE.match(x) for x in v):
            problems.append(f"{where}.schedule.daily_at must be HH:MM or a list of HH:MM")
        else:
            s.daily_at = list(v)
    elif "weekly" in raw:
        w = raw["weekly"]
        if not isinstance(w, dict) or w.get("day") not in DAYS or not TIME_RE.match(str(w.get("at", ""))):
            problems.append(f"{where}.schedule.weekly needs day (mon..sun) and at (HH:MM)")
        else:
            s.weekly_day, s.weekly_at = w["day"], w["at"]
    else:
        problems.append(f"{where}.schedule needs every_minutes, daily_at or weekly")
    return s


def parse(data: Dict[str, Any]) -> Manifest:
    problems: List[str] = []
    if data.get("manifest_version") != MANIFEST_VERSION:
        problems.append(f"manifest_version must be {MANIFEST_VERSION}")
    name = data.get("name", "")
    if not isinstance(name, str) or not NAME_RE.match(name):
        problems.append("name must be lowercase letters, digits and hyphens")
    services: List[Service] = []
    seen = set()
    raw_services = data.get("services", [])
    if not isinstance(raw_services, list):
        problems.append("services must be a list")
        raw_services = []
    for i, rs in enumerate(raw_services):
        where = f"services[{i}]"
        if not isinstance(rs, dict):
            problems.append(f"{where} must be an object")
            continue
        sname = rs.get("name", "")
        if not isinstance(sname, str) or not NAME_RE.match(sname):
            problems.append(f"{where}.name must be lowercase letters, digits and hyphens")
            continue
        if sname in seen:
            problems.append(f"{where}.name '{sname}' is repeated")
        seen.add(sname)
        kind = rs.get("kind")
        if kind not in ("resident", "scheduled"):
            problems.append(f"{where}.kind must be 'resident' or 'scheduled'")
            continue
        cmd = rs.get("command")
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
            problems.append(f"{where}.command must be a non-empty list of strings")
            continue
        if not cmd[0].startswith("/"):
            # systemd starts with a minimal PATH: the executable
            # must be an absolute path.
            problems.append(f"{where}.command[0] must be an absolute path")
        sched = None
        if kind == "scheduled":
            sched = _schedule(rs.get("schedule"), where, problems)
        elif "schedule" in rs:
            problems.append(f"{where}: a resident service has no schedule")
        tmo = rs.get("timeout_minutes", 120)
        if not isinstance(tmo, int) or not (1 <= tmo <= 1440):
            problems.append(f"{where}.timeout_minutes must be between 1 and 1440")
            tmo = 120
        services.append(Service(sname, kind, list(cmd), bool(rs.get("heavy_lock", False)), sched, tmo,
                                str(rs.get("description", ""))[:120]))
    health = data.get("health") or {}
    url = health.get("url")
    if url is not None and not (isinstance(url, str) and re.match(r"^http://127\.0\.0\.1:\d+/", url)):
        problems.append("health.url must be http://127.0.0.1:<port>/... (loopback only)")
    backup = data.get("backup") or {}
    inc = backup.get("include", [])
    exc = backup.get("exclude", [])
    for label, lst in (("include", inc), ("exclude", exc)):
        if not isinstance(lst, list) or not all(_rel_path_ok(x) for x in lst):
            problems.append(f"backup.{label} must list relative paths inside brain_home")
    peak = data.get("peak_memory") or {}
    pr, pb = peak.get("resident_mib", 0), peak.get("batch_mib", 0)
    if not all(isinstance(x, int) and 0 <= x <= 262144 for x in (pr, pb)):
        problems.append("peak_memory.resident_mib and batch_mib must be integers in MiB")
        pr, pb = 0, 0
    e2e = data.get("e2e_test") or {}
    e2e_cmd = e2e.get("command")
    if e2e_cmd is not None and (not isinstance(e2e_cmd, list) or not e2e_cmd
                                or not all(isinstance(x, str) for x in e2e_cmd) or not e2e_cmd[0].startswith("/")):
        problems.append("e2e_test.command must be a list starting with an absolute path")
    inbox = data.get("session_inbox", "inbox/sessions")
    if not _rel_path_ok(inbox):
        problems.append("session_inbox must be a relative path inside brain_home")
    env = data.get("environment") or {}
    if not isinstance(env, dict) or not all(isinstance(k, str) and ENV_KEY_RE.match(k) and isinstance(v, str)
                                            for k, v in env.items()):
        problems.append("environment must map UPPER_CASE names to strings")
        env = {}
    if problems:
        raise ManifestError(problems)
    return Manifest(
        name=name, services=services, health_url=url,
        health_expect=str(health.get("expect_status", "ok")),
        health_timeout=int(health.get("timeout_seconds", 5)),
        backup_include=list(inc), backup_exclude=list(exc),
        peak_resident_mib=pr, peak_batch_mib=pb,
        e2e_command=list(e2e_cmd) if e2e_cmd else None,
        e2e_timeout=int(e2e.get("timeout_seconds", 300)),
        session_inbox=inbox, environment=dict(env),
    )


def load(text: str) -> Manifest:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError([f"not valid JSON: {exc}"])
    if not isinstance(data, dict):
        raise ManifestError(["top level must be an object"])
    return parse(data)
