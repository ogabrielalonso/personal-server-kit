"""Checks before anything changes. A blocker is one plain sentence and a
next action; nothing is touched until preflight passes."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from typing import List

from .budget import GIB
from .i18n import t
from .system import Host


@dataclass
class Report:
    blockers: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.blockers


def reachable(host_name: str, port: int = 443, timeout: float = 8.0) -> bool:
    try:
        with socket.create_connection((host_name, port), timeout=timeout):
            return True
    except OSError:
        return False


def linux_server(host: Host, need_network: bool = True, is_root: bool = True) -> Report:
    rep = Report()
    osr = host.os_release()
    rep.facts["os"] = f"{osr.get('ID', '?')} {osr.get('VERSION_ID', '?')}"
    rep.facts["codename"] = osr.get("VERSION_CODENAME", "")
    if osr.get("ID") != "ubuntu" or osr.get("VERSION_ID") != "24.04":
        rep.blockers.append(t("preflight.os_unsupported", os=rep.facts["os"]))
    arch = os.uname().machine
    rep.facts["arch"] = arch
    if arch not in ("x86_64", "aarch64"):
        rep.blockers.append(t("preflight.arch_unsupported", arch=arch))
    if not is_root:
        rep.blockers.append(t("preflight.need_root"))
    if not host.exists("/run/systemd/system"):
        rep.blockers.append(t("preflight.no_systemd"))
    if not host.exists("/sys/fs/cgroup/cgroup.controllers"):
        rep.blockers.append(t("preflight.no_cgroup2"))
    virt = host.run(["systemd-detect-virt", "--container"])
    if virt.ok and virt.stdout.strip() not in ("", "none"):
        rep.blockers.append(t("preflight.container", kind=virt.stdout.strip()))
    mem = host.meminfo_kib()
    ram = mem.get("MemTotal", 0) * 1024
    rep.facts["ram_bytes"] = ram
    if ram < 3.5 * GIB:
        rep.blockers.append(t("preflight.ram_low", gib=round(ram / GIB, 1)))
    elif ram < 7.5 * GIB:
        rep.warnings.append(t("preflight.ram_tight", gib=round(ram / GIB, 1)))
    total, used, free = host.disk_usage("/")
    rep.facts["root_free_bytes"] = free
    if free < 10 * GIB:
        rep.blockers.append(t("preflight.disk_low", gib=round(free / GIB, 1)))
    elif free < 30 * GIB:
        rep.warnings.append(t("preflight.disk_tight", gib=round(free / GIB, 1)))
    if need_network:
        for name in ("pkgs.tailscale.com", "github.com"):
            if not reachable(name):
                rep.blockers.append(t("preflight.no_network", host=name))
                break
    if os.environ.get("SSH_CONNECTION") and not (os.environ.get("TMUX") or os.environ.get("STY")):
        rep.warnings.append(t("preflight.no_tmux"))
    return rep
