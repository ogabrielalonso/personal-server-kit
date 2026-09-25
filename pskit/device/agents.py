"""Background jobs on the laptop: the tunnel and the session bridge.

Tunnel rule: an SSH that hangs while connecting
never exits, so "restart when it dies" never fires. ConnectTimeout plus
ServerAlive (in the managed ssh config) make it exit, and launchd or
systemd bring it back.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

from .local import Paths


def _label(server: str, kind: str) -> str:
    return f"io.pskit.{server}.{kind}"


def python_cmd(paths: Paths) -> List[str]:
    return [sys.executable, "-B", "-m", "pskit"]


def launchd_jobs(paths: Paths, server: str) -> Dict[str, Dict]:
    paths.logs.mkdir(parents=True, exist_ok=True)
    env = {"PYTHONPATH": str(paths.share / "current"), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    return {
        _label(server, "tunnel"): {
            "Label": _label(server, "tunnel"),
            "ProgramArguments": ["/usr/bin/ssh", "-N", f"{server}-tunnel"],
            "KeepAlive": True,
            "RunAtLoad": True,
            "ThrottleInterval": 15,
            "StandardOutPath": str(paths.logs / f"{server}-tunnel.log"),
            "StandardErrorPath": str(paths.logs / f"{server}-tunnel.log"),
        },
        _label(server, "bridge"): {
            "Label": _label(server, "bridge"),
            "ProgramArguments": python_cmd(paths) + ["bridge-push", "--server", server],
            "EnvironmentVariables": env,
            "StartInterval": 900,
            "RunAtLoad": True,
            "StandardOutPath": str(paths.logs / f"{server}-bridge.log"),
            "StandardErrorPath": str(paths.logs / f"{server}-bridge.log"),
        },
    }


def systemd_units(paths: Paths, server: str) -> Dict[str, str]:
    py = " ".join(python_cmd(paths))
    return {
        f"pskit-tunnel-{server}.service": (
            f"[Unit]\nDescription=pskit tunnel to the brain on {server}\n\n"
            f"[Service]\nExecStart=/usr/bin/ssh -N {server}-tunnel\nRestart=always\nRestartSec=15\n\n"
            "[Install]\nWantedBy=default.target\n"),
        f"pskit-bridge-{server}.service": (
            f"[Unit]\nDescription=pskit session delivery to {server}\n\n"
            f"[Service]\nType=oneshot\nEnvironment=PYTHONPATH={paths.share / 'current'}\n"
            f"ExecStart={py} bridge-push --server {server}\n"),
        f"pskit-bridge-{server}.timer": (
            f"[Unit]\nDescription=pskit session delivery to {server} every 15 minutes\n\n"
            "[Timer]\nOnBootSec=2min\nOnUnitActiveSec=15min\n\n[Install]\nWantedBy=timers.target\n"),
    }


def install(paths: Paths, server: str, is_mac: bool) -> List[str]:
    installed = []
    if is_mac:
        paths.launch_agents.mkdir(parents=True, exist_ok=True)
        uid = os.getuid()
        for label, job in launchd_jobs(paths, server).items():
            plist_path = paths.launch_agents / f"{label}.plist"
            plist_path.write_bytes(plistlib.dumps(job, sort_keys=True))
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
            subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)], capture_output=True, check=True)
            installed.append(label)
        return installed
    paths.systemd_user.mkdir(parents=True, exist_ok=True)
    for name, text in systemd_units(paths, server).items():
        (paths.systemd_user / name).write_text(text)
        installed.append(name)
    # Captured: systemctl's "Created symlink ..." lines are noise in the installer.
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True)
    subprocess.run(["systemctl", "--user", "enable", "--now", f"pskit-tunnel-{server}.service",
                    f"pskit-bridge-{server}.timer"], check=True, capture_output=True)
    return installed


def remove(paths: Paths, server: str, is_mac: bool) -> None:
    if is_mac:
        uid = os.getuid()
        for label in launchd_jobs(paths, server):
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
            p = paths.launch_agents / f"{label}.plist"
            if p.exists():
                p.unlink()
        return
    names = list(systemd_units(paths, server))
    stoppable = [n for n in names if n != f"pskit-bridge-{server}.service"]
    subprocess.run(["systemctl", "--user", "disable", "--now", *stoppable], capture_output=True)
    for n in names:
        p = paths.systemd_user / n
        if p.exists():
            p.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)


def running(paths: Paths, server: str, is_mac: bool) -> Dict[str, bool]:
    if is_mac:
        uid = os.getuid()
        return {label: subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"], capture_output=True).returncode == 0
                for label in launchd_jobs(paths, server)}
    out = {}
    for n in (f"pskit-tunnel-{server}.service", f"pskit-bridge-{server}.timer"):
        out[n] = subprocess.run(["systemctl", "--user", "is-active", "--quiet", n]).returncode == 0
    return out


def unit_path(paths: Paths, name: str) -> Path:
    return paths.systemd_user / name
