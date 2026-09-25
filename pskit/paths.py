"""Fixed locations. Every path is absolute on the target machine; the Host
object maps it under its root prefix, so tests never touch the real system."""

from __future__ import annotations

from dataclasses import dataclass

from . import KIT


@dataclass(frozen=True)
class SystemPaths:
    etc: str = f"/etc/{KIT}"
    host_config: str = f"/etc/{KIT}/host.json"
    kit_config: str = f"/etc/{KIT}/kit.json"
    secrets: str = f"/etc/{KIT}/secrets"
    state: str = f"/var/lib/{KIT}"
    journal: str = f"/var/lib/{KIT}/journal.json"
    ledger: str = f"/var/lib/{KIT}/ledger.jsonl"
    backups: str = f"/var/lib/{KIT}/file-backups"
    alerts: str = f"/var/lib/{KIT}/alerts.json"
    digest: str = f"/var/lib/{KIT}/digest.jsonl"
    receipts: str = f"/var/lib/{KIT}/receipts"
    devices: str = f"/var/lib/{KIT}/devices.json"
    lockdown: str = f"/var/lib/{KIT}/lockdown"
    proof: str = f"/var/lib/{KIT}/proof"
    spool: str = f"/var/spool/{KIT}/notify"
    run: str = f"/run/{KIT}"
    heavy_lock: str = f"/run/{KIT}/heavy.lock"
    opt: str = f"/opt/{KIT}"
    releases: str = f"/opt/{KIT}/releases"
    current: str = f"/opt/{KIT}/current"
    shim: str = f"/usr/local/bin/{KIT}"
    restic: str = f"/opt/{KIT}/bin/restic"
    brain_manifest: str = f"/etc/{KIT}/brain-manifest.json"


SYS = SystemPaths()

# Group whose members may use kit services (notify spool, lockdown confirm).
KIT_GROUP = KIT
# Dedicated identity for brain services.
BRAIN_USER_LINUX = f"{KIT}-brain"

# systemd names (Linux)
SLICE_PARENT = f"{KIT}.slice"
SLICE_BRAIN = f"{KIT}-brain.slice"
SLICE_CI = f"{KIT}-ci.slice"


def run_dir() -> str:
    """Runtime folder, emptied at boot: the health job recreates it."""
    return SYS.run


def heavy_lock() -> str:
    return f"{run_dir()}/heavy.lock"


def unit(name: str) -> str:
    """Kit unit name: unit('health.timer') == 'pskit-health.timer'."""
    return f"{KIT}-{name}"


def user_paths(home: str) -> dict:
    """Per-user locations on the owner's laptop (device side)."""
    return {
        "code": f"{home}/.local/share/{KIT}",
        "bin": f"{home}/.local/bin/{KIT}",
        "state": f"{home}/.local/state/{KIT}",
        "ssh_dir": f"{home}/.ssh/{KIT}",
        "ssh_config": f"{home}/.ssh/config",
        "launch_agents": f"{home}/Library/LaunchAgents",
        "systemd_user": f"{home}/.config/systemd/user",
    }
