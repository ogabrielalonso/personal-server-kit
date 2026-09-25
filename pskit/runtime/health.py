"""`pskit health`: probes, alert state with hysteresis, one message per run."""

from __future__ import annotations

import fcntl
import json
import time
from typing import Any, Callable, Dict, List, Optional

from .. import notify
from ..alerts import AlertState, Finding, Outcome
from ..checks import Probes, write_receipt
from ..config import HostConfig, KitConfig
from ..i18n import t_in
from ..paths import SYS
from ..system import Host


def compose(cfg: HostConfig, out: Outcome) -> Optional[Dict[str, str]]:
    if not out.urgent:
        return None
    lang = cfg.language
    lines: List[str] = []
    for f in out.new + out.changed:
        lines.append(("[!!] " if f.level == "crit" else "[!] ") + f.text)
    for f in out.reminders:
        lines.append(t_in(lang, "health.still") + " " + f.text)
    for _, text in out.cleared:
        lines.append(t_in(lang, "health.resolved") + " " + text)
    if out.new or out.changed or out.reminders:
        title = t_in(lang, "health.title_problem")
        sev = out.severity()
    else:
        title = t_in(lang, "health.title_recovered")
        sev = "warn"
    return {"severity": sev, "title": title, "body": "\n".join(lines)}


def run(host: Host, cfg: HostConfig, kit: KitConfig, now: Optional[float] = None,
        channel: Optional[notify.Channel] = None, http_get: Optional[Callable] = None) -> Dict:
    now = now or time.time()
    lock_file = host.p(f"{SYS.state}/health.lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_file, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"skipped": "locked"}
        state = AlertState(host)
        probes = Probes(host, cfg, kit, state, now=now, http_get=http_get)
        findings, crashed = probes.collect()
        outcome = state.process(findings, now, kit.alert_repeat_hours, preserve_prefixes=crashed)
        msg = compose(cfg, outcome)
        if msg:
            notify.enqueue(host, msg["severity"], msg["title"], msg["body"], source="health")
        for f in outcome.digest:
            notify.append_digest(host, f.text, "health")
        # Deliver now (and retry anything a previous outage left queued).
        stats = notify.deliver(host, cfg, channel=channel, now=now)
        state.save()
        summary: Dict[str, Any] = {
            "checked_at": now,
            "problems": [dict(key=f.key, level=f.level, text=f.text) for f in findings
                         if f.level in ("warn", "crit")],
            "notes": [dict(key=f.key, text=f.text) for f in findings if f.level == "info"],
            "crashed_probes": list(crashed),
            "delivery": stats,
        }
        write_receipt(host, "health", summary)
        print(json.dumps({"problems": len(summary["problems"]), "delivery": stats}))
        return summary


def worst(findings: List[Finding]) -> str:
    order = {"ok": 0, "info": 1, "warn": 2, "crit": 3}
    return max((f.level for f in findings), key=lambda x: order[x], default="ok")
