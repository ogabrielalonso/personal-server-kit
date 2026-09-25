"""Alert state with hysteresis.

An alert whose key changes with the level repeats every time usage
crosses the threshold, and a disk hovering near 80% sends it all day. Here a finding
has a stable key, levels need a margin to clear, and the owner hears about
a problem when it starts, when it changes, when it clears, and at most once
a day while it lasts.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Tuple

from .paths import SYS
from .system import Host

LEVELS = ("ok", "info", "warn", "crit")
_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}


@dataclass
class Finding:
    key: str
    level: str
    text: str
    value: Optional[float] = None

    def __post_init__(self):
        if self.level not in LEVELS:
            raise ValueError(f"bad level {self.level}")


def graded(value: float, previous: str, warn: float, crit: float, margin: float) -> str:
    """Level for a 'higher is worse' metric with hysteresis.

    Entering a level needs the threshold; leaving it needs the threshold
    minus the margin."""
    if previous == "crit" and value >= crit - margin:
        return "crit"
    if value >= crit:
        return "crit"
    if previous in ("warn", "crit") and value >= warn - margin:
        return "warn"
    if value >= warn:
        return "warn"
    return "ok"


def graded_low(value: float, previous: str, warn: float, crit: float, margin: float) -> str:
    """Same, for 'lower is worse' metrics (available memory)."""
    return graded(-value, previous, -warn, -crit, margin)


@dataclass
class Outcome:
    new: List[Finding]
    changed: List[Finding]
    cleared: List[Tuple[str, str]]
    reminders: List[Finding]
    digest: List[Finding]

    @property
    def urgent(self) -> bool:
        return bool(self.new or self.changed or self.cleared or self.reminders)

    def severity(self) -> str:
        levels = [f.level for f in self.new + self.changed + self.reminders]
        return "critical" if "crit" in levels else "warn"


class AlertState:
    def __init__(self, host: Host, path: str = SYS.alerts):
        self.host = host
        self.path = path
        try:
            self.data: Dict[str, Dict] = json.loads(host.read_text(path, "{}") or "{}")
        except json.JSONDecodeError:
            self.data = {}

    def previous_level(self, key: str) -> str:
        return self.data.get(key, {}).get("level", "ok")

    def process(self, findings: List[Finding], now: float, repeat_hours: float = 24,
                preserve_prefixes: Tuple[str, ...] = ()) -> Outcome:
        """preserve_prefixes: keys of probes that crashed this run; their
        previous state is kept instead of being read as cleared."""
        out = Outcome([], [], [], [], [])
        seen = set()
        for f in findings:
            seen.add(f.key)
            prev = self.data.get(f.key)
            if f.level == "ok":
                if prev and prev.get("level") in ("warn", "crit"):
                    out.cleared.append((f.key, prev.get("text", f.key)))
                self.data.pop(f.key, None)
                continue
            if f.level == "info":
                last = (prev or {}).get("digest_at")
                if last is None or now - last >= 20 * 3600:
                    out.digest.append(f)
                    self.data[f.key] = {"level": "info", "text": f.text, "since": (prev or {}).get("since", now),
                                        "digest_at": now}
                continue
            if not prev or prev.get("level") in ("ok", "info"):
                out.new.append(f)
                self.data[f.key] = {"level": f.level, "text": f.text, "since": now, "sent_at": now}
            elif prev.get("level") != f.level:
                out.changed.append(f)
                self.data[f.key] = {"level": f.level, "text": f.text, "since": prev.get("since", now), "sent_at": now}
            elif now - prev.get("sent_at", 0) >= repeat_hours * 3600:
                out.reminders.append(f)
                prev.update(text=f.text, sent_at=now)
            else:
                prev["text"] = f.text
        # A probe that stopped reporting a key counts as cleared.
        for key in list(self.data):
            if key not in seen and not key.startswith(preserve_prefixes or ("\0",)):
                prev = self.data.pop(key)
                if prev.get("level") in ("warn", "crit"):
                    out.cleared.append((key, prev.get("text", key)))
        return out

    def save(self) -> None:
        self.host.write_atomic(self.path, json.dumps(self.data, indent=1, sort_keys=True), mode=0o600)

    def active(self) -> List[Dict]:
        return [dict(key=k, **v) for k, v in sorted(self.data.items()) if v.get("level") in ("warn", "crit")]


def finding_dict(f: Finding) -> Dict:
    return asdict(f)
