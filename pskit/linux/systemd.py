"""systemd helpers. Every unit written is recorded in the ledger."""

from __future__ import annotations

from typing import List, Optional

from ..state import Ledger, install_file
from ..system import Host

UNIT_DIR = "/etc/systemd/system"


def install_unit(host: Host, ledger: Ledger, name: str, content: str) -> str:
    return install_file(host, ledger, f"{UNIT_DIR}/{name}", content, mode=0o644)


def install_dropin(host: Host, ledger: Ledger, unit: str, name: str, content: str) -> str:
    return install_file(host, ledger, f"{UNIT_DIR}/{unit}.d/{name}", content, mode=0o644)


def daemon_reload(host: Host) -> None:
    host.check(["systemctl", "daemon-reload"])


def enable_now(host: Host, ledger: Ledger, unit: str) -> None:
    was_enabled = is_enabled(host, unit)
    host.check(["systemctl", "enable", "--now", unit])
    if not was_enabled and not ledger.has("unit_enabled", unit=unit):
        ledger.add("unit_enabled", unit=unit)


def enable(host: Host, ledger: Ledger, unit: str) -> None:
    was_enabled = is_enabled(host, unit)
    host.check(["systemctl", "enable", unit])
    if not was_enabled and not ledger.has("unit_enabled", unit=unit):
        ledger.add("unit_enabled", unit=unit)


def is_active(host: Host, unit: str) -> bool:
    return host.run(["systemctl", "is-active", "--quiet", unit]).ok


def is_enabled(host: Host, unit: str) -> bool:
    r = host.run(["systemctl", "is-enabled", unit])
    return r.ok and r.stdout.strip() in ("enabled", "enabled-runtime", "static", "alias")


def is_failed(host: Host, unit: str) -> bool:
    return host.run(["systemctl", "is-failed", "--quiet", unit]).ok


def show(host: Host, unit: str, prop: str) -> str:
    r = host.run(["systemctl", "show", "-p", prop, "--value", unit])
    return r.stdout.strip() if r.ok else ""


def failed_units(host: Host, pattern: str) -> List[str]:
    r = host.run(["systemctl", "list-units", "--failed", "--plain", "--no-legend", pattern])
    if not r.ok:
        return []
    return [line.split()[0] for line in r.stdout.splitlines() if line.strip()]


def timer_last_trigger(host: Host, timer: str) -> Optional[str]:
    v = show(host, timer, "LastTriggerUSec")
    return v or None
