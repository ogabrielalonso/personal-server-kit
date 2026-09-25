"""`pskit brain apply`: install the brain's declared services.

The brain never installs units itself (contract, clause 5): it ships a
manifest, and the host turns each service into a systemd unit running as
the brain's identity, in the brain's memory
reservation, with the heavy-job lock where declared. Applying a new
manifest removes the units of services that disappeared from it.
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Dict, List, Optional

from . import manifest as mf
from .paths import BRAIN_USER_LINUX, SLICE_BRAIN, SYS
from .render import exec_line, render, systemd_quote
from .state import Ledger, install_file
from .system import Host

UNITS_FILE = f"{SYS.state}/brain-units.json"


def _previous(host: Host) -> Dict:
    try:
        return json.loads(host.read_text(UNITS_FILE, "{}") or "{}")
    except json.JSONDecodeError:
        return {}


def linux_units(man: mf.Manifest, cfg) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for s in man.services:
        base = f"pskit-brain-{s.name}"
        cmd = (["/usr/bin/flock", SYS.heavy_lock] if s.heavy_lock else []) + s.command
        env = "\n".join(f"Environment={systemd_quote(f'{k}={v}')}" for k, v in sorted(man.environment.items()))
        resident = s.kind == "resident"
        values = {
            "manifest_name": man.name,
            "description": s.description or s.name,
            "service_type": "simple" if resident else "oneshot",
            "brain_user": BRAIN_USER_LINUX,
            "brain_group": BRAIN_USER_LINUX,
            "brain_slice": SLICE_BRAIN,
            "brain_home": cfg.brain_home,
            "host_config": SYS.host_config,
            "environment_block": env,
            "exec_line": exec_line(cmd),
            "restart_block": "Restart=always\nRestartSec=10s" if resident else "",
            "timeout": "90s" if resident else f"{s.timeout_minutes}min",
            "spool": SYS.spool,
            "run_dir": SYS.run,
            "wanted_by": "multi-user.target" if resident else "",
        }
        text = render("systemd/brain.service", values)
        if not resident:
            text = text.replace("\n[Install]\nWantedBy=\n", "\n")
        out[f"{base}.service"] = text
        if s.schedule:
            out[f"{base}.timer"] = render("systemd/brain.timer", {
                "manifest_name": man.name, "description": s.description or s.name,
                "schedule_block": "\n".join(s.schedule.systemd_timer_lines())})
    return out


def apply_linux(host: Host, ledger: Ledger, cfg, text: str) -> Dict:
    man = mf.load(text)
    units = linux_units(man, cfg)
    prev = _previous(host)
    install_file(host, ledger, SYS.brain_manifest, text, mode=0o644)
    inbox = f"{cfg.brain_home}/{man.session_inbox}"
    host.mkdir(inbox, mode=0o2770, owner=BRAIN_USER_LINUX, group=BRAIN_USER_LINUX)
    for gone in sorted(set(prev.get("all", [])) - set(units)):
        host.run(["systemctl", "disable", "--now", gone])
        host.remove(f"/etc/systemd/system/{gone}")
    for name, content in units.items():
        install_file(host, ledger, f"/etc/systemd/system/{name}", content, mode=0o644)
    host.check(["systemctl", "daemon-reload"])
    expect: List[str] = []
    for s in man.services:
        base = f"pskit-brain-{s.name}"
        if s.kind == "resident":
            host.check(["systemctl", "enable", "--now", f"{base}.service"])
            host.run(["systemctl", "restart", f"{base}.service"])
            expect.append(f"{base}.service")
        else:
            host.check(["systemctl", "enable", "--now", f"{base}.timer"])
            expect.append(f"{base}.timer")
    record = {"manifest": man.name, "applied": time.time(), "all": sorted(units), "expect_active": expect}
    host.write_atomic(UNITS_FILE, json.dumps(record, indent=1), mode=0o644)
    return record



def wait_healthy(man: mf.Manifest, timeout_s: float = 90, getter=None) -> Optional[bool]:
    if not man.health_url:
        return None
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if getter:
                status, body = getter(man.health_url, man.health_timeout)
            else:
                with urllib.request.urlopen(man.health_url, timeout=man.health_timeout) as r:  # noqa: S310
                    status, body = r.status, r.read(65536)
            if status == 200 and json.loads(body.decode() or "{}").get("status") == man.health_expect:
                return True
        except Exception:
            pass
        time.sleep(3)
    return False
