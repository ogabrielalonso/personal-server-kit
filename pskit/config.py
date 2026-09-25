"""Shared configuration (the host and brain contract) and kit settings.

host.json is the contract file both layers read: settings and pointers,
never secrets. kit.json holds the host layer's own knobs. Secrets live in
separate root-only files under /etc/pskit/secrets.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from . import CONTRACT_VERSION
from .paths import SYS
from .system import Host

SCENARIOS = ("local", "linux-server")
LANGUAGES = ("en", "pt")
ALERT_CHANNELS = ("telegram", "ntfy", "none")
BACKUP_KINDS = ("s3", "sftp", "local", "later")

MACHINE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
DEVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


class ConfigError(ValueError):
    pass


def valid_machine_name(name: str) -> bool:
    return bool(MACHINE_NAME_RE.match(name or "")) and "--" not in name


def valid_user(name: str) -> bool:
    return bool(USER_RE.match(name or ""))


def slug(text: str, fallback: str = "device") -> str:
    s = re.sub(r"[^a-z0-9-]+", "-", (text or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)[:31].strip("-")
    return s or fallback


@dataclass
class HostConfig:
    """The contract file. Field names are part of the contract."""

    language: str = "en"
    scenario: str = "linux-server"
    machine_name: str = ""
    owner: str = ""
    brain_home: str = ""
    brain_port: int = 8799
    memory_budget: Dict[str, Any] = field(default_factory=dict)
    alert_channel: str = "none"
    workspace: str = ""
    session_inbox: str = "inbox/sessions"
    brain_user: str = ""
    kit_group: str = "pskit"
    notify_command: str = "/usr/local/bin/pskit notify"
    heavy_lock: str = SYS.heavy_lock
    contract_version: int = CONTRACT_VERSION

    def validate(self) -> List[str]:
        errors = []
        if self.contract_version != CONTRACT_VERSION:
            errors.append(f"contract_version {self.contract_version} is not {CONTRACT_VERSION}")
        if self.language not in LANGUAGES:
            errors.append(f"language must be one of {LANGUAGES}")
        if self.scenario not in SCENARIOS:
            errors.append(f"scenario must be one of {SCENARIOS}")
        if not valid_machine_name(self.machine_name):
            errors.append("machine_name must be 3-32 chars: lowercase letters, digits, hyphens")
        if not valid_user(self.owner):
            errors.append("owner is not a valid user name")
        if not self.brain_home.startswith("/"):
            errors.append("brain_home must be an absolute path")
        if not (1024 <= int(self.brain_port) <= 65535):
            errors.append("brain_port must be between 1024 and 65535")
        if self.alert_channel not in ALERT_CHANNELS:
            errors.append(f"alert_channel must be one of {ALERT_CHANNELS}")
        if self.session_inbox.startswith("/") or ".." in self.session_inbox.split("/"):
            errors.append("session_inbox must be a relative path inside brain_home")
        return errors

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> HostConfig:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class KitConfig:
    """Host layer settings and their defaults."""

    disk_warn_pct: int = 80
    disk_crit_pct: int = 90
    disk_hysteresis_pct: int = 5
    mem_available_warn_pct: int = 10
    swap_warn_pct: int = 75
    backup_max_age_hours: int = 36
    device_silence_days: int = 3
    alert_repeat_hours: int = 24
    health_interval_min: int = 5
    digest_time: str = "19:00"
    backup_time: str = "01:15"
    backup_retry_time: str = "19:30"
    cache_time: str = "16:30"
    reaper_browser_max_age_h: int = 3
    reaper_devserver_max_age_h: int = 12
    reaper_enabled: bool = True
    pressure_guard_enabled: bool = True
    heartbeat_enabled: bool = False
    backup_kind: str = "later"
    backup_repository: str = ""
    backup_paths: List[str] = field(default_factory=list)
    backup_excludes: List[str] = field(default_factory=list)
    backup_keep_daily: int = 7
    backup_keep_weekly: int = 4
    backup_keep_monthly: int = 6
    tailnet_ip: str = ""
    tailnet_name: str = ""
    pair_port: int = 47800
    tailscale_expected: bool = True
    modules: List[str] = field(default_factory=list)
    extra_mounts: List[str] = field(default_factory=list)
    test_mode: bool = False

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> KitConfig:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def load_host_config(host: Host, path: str = SYS.host_config) -> Optional[HostConfig]:
    raw = host.read_text(path)
    if not raw:
        return None
    try:
        return HostConfig.from_dict(json.loads(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        raise ConfigError(f"{path}: {exc}")


def load_kit_config(host: Host, path: str = SYS.kit_config) -> KitConfig:
    raw = host.read_text(path)
    if not raw:
        return KitConfig()
    try:
        return KitConfig.from_dict(json.loads(raw))
    except (json.JSONDecodeError, TypeError) as exc:
        raise ConfigError(f"{path}: {exc}")


def read_env_file(host: Host, path: str) -> Dict[str, str]:
    """KEY=value lines. Used for secrets; never logged."""
    out: Dict[str, str] = {}
    for line in (host.read_text(path, "") or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def env_file(values: Dict[str, str]) -> str:
    lines = []
    for k, v in values.items():
        if "\n" in v or '"' in v:
            raise ConfigError(f"unsupported character in value of {k}")
        lines.append(f'{k}="{v}"')
    return "\n".join(lines) + "\n"
