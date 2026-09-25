"""Per-user locations and the managed block in ~/.ssh/config."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from .. import KIT

BEGIN = f"# >>> {KIT} (managed, do not edit inside) >>>"
END = f"# <<< {KIT} <<<"


class Paths:
    def __init__(self, home: Optional[str] = None):
        self.home = Path(home or os.path.expanduser("~"))
        self.config = self.home / ".config" / KIT
        self.servers = self.config / "servers"
        self.state = self.home / ".local" / "state" / KIT
        self.share = self.home / ".local" / "share" / KIT
        self.bin = self.home / ".local" / "bin" / KIT
        self.ssh = self.home / ".ssh"
        self.ssh_kit = self.ssh / KIT
        self.launch_agents = self.home / "Library" / "LaunchAgents"
        self.systemd_user = self.home / ".config" / "systemd" / "user"
        self.logs = self.home / "Library" / "Logs" / KIT if (self.home / "Library").exists() else self.state / "logs"

    def server_dir(self, server: str) -> Path:
        return self.ssh_kit / server

    def server_file(self, server: str) -> Path:
        return self.servers / f"{server}.json"


def load_server(paths: Paths, server: str) -> Optional[Dict]:
    try:
        return json.loads(paths.server_file(server).read_text())
    except (OSError, json.JSONDecodeError):
        return None


def list_servers(paths: Paths) -> List[str]:
    if not paths.servers.is_dir():
        return []
    return sorted(p.stem for p in paths.servers.glob("*.json"))


def save_server(paths: Paths, server: str, data: Dict) -> None:
    paths.servers.mkdir(parents=True, exist_ok=True)
    tmp = paths.server_file(server).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, paths.server_file(server))


def ssh_host_block(server: str, info: Dict, keydir: Path, is_mac: bool) -> str:
    host = info["tailnet_ip"]
    user = info["owner"]
    known = keydir / "known_hosts"
    port = info.get("brain_port", 8799)
    common = [
        f"    HostName {host}",
        f"    User {user}",
        "    Port 22",
        "    IdentitiesOnly yes",
        f"    UserKnownHostsFile {known}",
        "    StrictHostKeyChecking yes",
        "    ConnectTimeout 15",
        "    ServerAliveInterval 15",
        "    ServerAliveCountMax 3",
    ]
    human = [f"Host {server}", f"    IdentityFile {keydir / 'id_ed25519'}"] + common
    if is_mac:
        human += ["    IgnoreUnknown UseKeychain", "    UseKeychain yes", "    AddKeysToAgent yes"]
    tunnel = [f"Host {server}-tunnel", f"    IdentityFile {keydir / 'tunnel'}"] + common + [
        "    BatchMode yes",
        f"    LocalForward 127.0.0.1:{port} 127.0.0.1:{port}",
        "    ExitOnForwardFailure yes",
        "    ControlMaster no",
        "    ControlPath none",
        "    SessionType none",
    ]
    bridge = [f"Host {server}-bridge", f"    IdentityFile {keydir / 'bridge'}"] + common + [
        "    BatchMode yes", "    ControlMaster no", "    ControlPath none"]
    return "\n".join(human + [""] + tunnel + [""] + bridge) + "\n"


def ensure_include(paths: Paths) -> bool:
    """Puts one Include line at the top of ~/.ssh/config. Returns True when
    the file changed. Include must come before any Host block to apply."""
    paths.ssh.mkdir(mode=0o700, exist_ok=True)
    cfg = paths.ssh / "config"
    text = cfg.read_text() if cfg.exists() else ""
    block = f"{BEGIN}\nInclude {paths.ssh_kit}/*/ssh_config\n{END}\n"
    if BEGIN in text:
        new = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", block, text, flags=re.S)
    else:
        new = block + ("\n" + text if text else "")
    if new == text:
        return False
    tmp = cfg.with_name(".config.pskit.tmp")
    tmp.write_text(new)
    os.chmod(tmp, 0o600)
    os.replace(tmp, cfg)
    return True


def remove_include(paths: Paths) -> None:
    cfg = paths.ssh / "config"
    if not cfg.exists():
        return
    text = cfg.read_text()
    new = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?\n?", "", text, flags=re.S)
    if new != text:
        cfg.write_text(new)
