"""Lockdown with confirm-or-revert.

Closing public access is the one step that can lock the owner out. So it
is armed, not just applied: the kit snapshots the firewall and SSH
settings, applies the new ones, and starts a timer. Unless the owner's
laptop proves it can still get in over the private network (by running
`pskit confirm-lockdown` through SSH) before the timer fires, everything is
put back exactly as it was. The timer also fires after a reboot, so an
unconfirmed lockdown can never survive one.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import shlex
import time
from typing import Any, Dict, List, Optional

from . import notify
from .config import HostConfig, KitConfig
from .i18n import t_in
from .paths import KIT_GROUP, SYS, unit
from .render import render
from .state import Ledger, install_file
from .system import Host

PENDING = f"{SYS.lockdown}/pending.json"
APPLIED = f"{SYS.lockdown}/applied.json"
REVERTED = f"{SYS.lockdown}/reverted.json"
SNAPSHOT = f"{SYS.lockdown}/snapshot"
SSHD_DROPIN = "/etc/ssh/sshd_config.d/10-pskit.conf"
UFW_FILES = ["/etc/ufw/user.rules", "/etc/ufw/user6.rules", "/etc/ufw/ufw.conf", "/etc/default/ufw"]
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
REVERT_SECONDS = 600


class LockdownError(RuntimeError):
    pass


def _confirm_path(nonce: str) -> str:
    return f"{SYS.lockdown}/confirmed-{nonce}"


def read_pending(host: Host) -> Optional[Dict]:
    raw = host.read_text(PENDING)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def is_applied(host: Host) -> bool:
    return host.exists(APPLIED)


def is_confirmed(host: Host, pending: Dict) -> bool:
    return host.exists(_confirm_path(pending["nonce"]))


def from_tailnet(ssh_connection: str) -> bool:
    """SSH_CONNECTION is 'client_ip client_port server_ip server_port'."""
    try:
        ip = ipaddress.ip_address(ssh_connection.split()[0])
    except (ValueError, IndexError):
        return False
    return ip in TAILNET_V4 or ip in TAILNET_V6


# ---- firewall rules ----------------------------------------------------

def _covers_22(spec: str) -> bool:
    for item in spec.split(","):
        item = item.split("/", 1)[0].strip()
        if ":" in item:
            lo, _, hi = item.partition(":")
            if lo.isdigit() and hi.isdigit() and int(lo) <= 22 <= int(hi):
                return True
        elif item == "22":
            return True
    return False


def ssh_rule(line: str) -> bool:
    """True for a rule that opens SSH (port 22, alone, in a list or in a
    range, or the OpenSSH/ssh application) to anyone: such rules are
    dropped, and the tailnet rule replaces them."""
    try:
        tokens = shlex.split(line)
    except ValueError:
        tokens = line.split()
    low = [tok.lower() for tok in tokens]
    if "tailscale0" in low:
        return False
    for i, tok in enumerate(low):
        if tok in ("openssh", "ssh"):
            return True
        if tok == "port" and i + 1 < len(low) and _covers_22(low[i + 1]):
            return True
    # Short syntax: ufw ACTION [in|out] [log|log-all] PORT[/proto], which is
    # also how `ufw show added` prints rules with logging.
    for i, tok in enumerate(low):
        if tok in ("allow", "limit"):
            j = i + 1
            while j < len(low) and low[j] in ("in", "out", "log", "log-all"):
                j += 1
            if j < len(low) and re.match(r"^[\d,:]+(/(tcp|udp))?$", low[j]) and _covers_22(low[j]):
                return True
    return False


def existing_rules(host: Host) -> List[str]:
    """'ufw allow 80/tcp' style lines added by whoever configured the machine."""
    r = host.run(["ufw", "show", "added"])
    rules = []
    for line in r.stdout.splitlines():
        line = line.strip()
        if line.startswith("ufw ") and "(skipped" not in line:
            rules.append(line)
    return rules


# ---- snapshot / restore -----------------------------------------------

def snapshot(host: Host) -> None:
    host.mkdir(SNAPSHOT, mode=0o700)
    files: Dict[str, str] = {}
    meta: Dict[str, Any] = {"ufw_enabled": False, "files": files, "sshd_dropin": host.read_text(SSHD_DROPIN)}
    r = host.run(["ufw", "status"])
    meta["ufw_enabled"] = r.ok and "Status: active" in r.stdout
    for path in UFW_FILES:
        data = host.read_bytes(path)
        if data is not None:
            name = path.strip("/").replace("/", "__")
            host.write_atomic(f"{SNAPSHOT}/{name}", data, mode=0o600)
            files[path] = name
    host.write_atomic(f"{SNAPSHOT}/meta.json", json.dumps(meta), mode=0o600)


def restore_snapshot(host: Host) -> None:
    raw = host.read_text(f"{SNAPSHOT}/meta.json")
    if not raw:
        raise LockdownError("no snapshot to restore")
    meta = json.loads(raw)
    for path, name in meta.get("files", {}).items():
        data = host.read_bytes(f"{SNAPSHOT}/{name}")
        if data is not None:
            host.write_atomic(path, data, mode=0o640 if path.startswith("/etc/ufw/user") else 0o644)
    if meta.get("sshd_dropin") is None:
        host.remove(SSHD_DROPIN)
    else:
        host.write_atomic(SSHD_DROPIN, meta["sshd_dropin"], mode=0o644)
    if meta.get("ufw_enabled"):
        host.run(["ufw", "reload"])
    else:
        host.run(["ufw", "--force", "disable"])
    reload_sshd(host)


def reload_sshd(host: Host) -> None:
    if host.run(["systemctl", "is-active", "--quiet", "ssh.service"]).ok:
        host.run(["systemctl", "reload", "ssh.service"])


# ---- apply -------------------------------------------------------------

def apply(host: Host, ledger: Ledger, cfg: HostConfig, kit: KitConfig, keep_rules: List[str],
          revert_seconds: int = REVERT_SECONDS) -> Dict:
    """Snapshots, arms the revert timer, then applies. Returns the pending
    record. The timer runs before anything changes, and any failure while
    applying (of any kind) restores the snapshot and disarms it, so the
    machine is never left closed without its safety net."""
    if read_pending(host):
        raise LockdownError("a lockdown is already waiting for confirmation")
    host.mkdir(SYS.lockdown, mode=0o2770, group=KIT_GROUP)
    snapshot(host)
    try:
        pending = _arm(host, ledger, revert_seconds)
    except Exception as exc:
        _abort(host)
        raise LockdownError(f"could not arm the safety timer: {exc}") from exc
    try:
        _apply_changes(host, ledger, cfg, keep_rules)
    except BaseException as exc:
        try:
            restore_snapshot(host)
        finally:
            _abort(host)
        if isinstance(exc, (LockdownError, KeyboardInterrupt, SystemExit)):
            raise
        raise LockdownError(f"{type(exc).__name__}: {exc}") from exc
    # Only now may a login confirm it: a confirmation that arrived while the
    # changes were still being applied would prove nothing.
    pending["applied"] = True
    host.write_atomic(PENDING, json.dumps(pending), mode=0o640, group=KIT_GROUP)
    return pending


def _abort(host: Host) -> None:
    _disarm(host)
    host.remove(PENDING)


def _apply_changes(host: Host, ledger: Ledger, cfg: HostConfig, keep_rules: List[str]) -> None:
    # SSH: keys only, the owner only. Validate before anything reloads.
    install_file(host, ledger, SSHD_DROPIN, render("misc/sshd-hardening.conf", {"owner": cfg.owner}), mode=0o644)
    ensure_privsep_dir(host)
    test = host.run(["sshd", "-t"])
    if not test.ok:
        raise LockdownError(f"sshd rejected the configuration: {test.stderr.strip()[-200:]}")
    # Firewall: deny incoming except the tailnet and Tailscale's own port.
    cmds = [["ufw", "--force", "reset"],
            ["ufw", "default", "deny", "incoming"],
            ["ufw", "default", "allow", "outgoing"],
            ["ufw", "allow", "in", "on", "tailscale0"],
            ["ufw", "allow", "41641/udp"]]
    for rule in keep_rules:
        try:
            parts = shlex.split(rule)
        except ValueError:
            continue
        if parts[:1] == ["ufw"] and not ssh_rule(rule):
            cmds.append(parts)
    cmds.append(["ufw", "--force", "enable"])
    for c in cmds:
        if not host.run(c).ok:
            raise LockdownError(f"firewall command failed: {' '.join(c)}")
    reload_sshd(host)


def _arm(host: Host, ledger: Ledger, revert_seconds: int) -> Dict:
    now = time.time()
    pending = {"nonce": secrets.token_hex(8), "armed_at": now, "deadline": now + revert_seconds, "applied": False}
    host.write_atomic(PENDING, json.dumps(pending), mode=0o640, group=KIT_GROUP)
    values = {"pskit": SYS.shim, "revert_seconds": revert_seconds}
    for name in ("lockdown-revert.service", "lockdown-revert.timer"):
        install_file(host, ledger, f"/etc/systemd/system/{unit(name)}",
                     render(f"systemd/{unit(name)}", values), mode=0o644)
    host.check(["systemctl", "daemon-reload"])
    host.check(["systemctl", "enable", "--now", unit("lockdown-revert.timer")])
    # Restart the countdown from now even if the timer was already active.
    host.run(["systemctl", "restart", unit("lockdown-revert.timer")])
    return pending


def _disarm(host: Host) -> None:
    host.run(["systemctl", "disable", "--now", unit("lockdown-revert.timer")])


def confirm(host: Host, ssh_connection: str, allow_local: bool = False) -> str:
    """Called by the owner through SSH. Returns 'confirmed', 'nothing-pending'
    or raises when the connection did not come over the private network."""
    pending = read_pending(host)
    if not pending:
        return "already-applied" if is_applied(host) else "nothing-pending"
    if not pending.get("applied", True):
        return "not-ready"
    if not allow_local and not from_tailnet(ssh_connection):
        raise LockdownError("confirmation must come through the private network (Tailscale)")
    host.write_atomic(_confirm_path(pending["nonce"]), json.dumps({"at": time.time()}), mode=0o640)
    return "confirmed"


def finalize(host: Host) -> bool:
    """Root, after confirmation: disarm and record. True when finalized."""
    pending = read_pending(host)
    if not pending or not is_confirmed(host, pending):
        return False
    _disarm(host)
    host.write_atomic(APPLIED, json.dumps({"at": time.time(), "nonce": pending["nonce"]}), mode=0o644)
    host.remove(PENDING)
    host.remove(_confirm_path(pending["nonce"]))
    return True


def revert_if_due(host: Host, cfg: Optional[HostConfig], now: Optional[float] = None,
                  from_timer: bool = False) -> str:
    """from_timer: called by the systemd timer, whose firing is the deadline
    (monotonic, so a clock step after arming cannot postpone the undo)."""
    now = now or time.time()
    pending = read_pending(host)
    if not pending:
        _disarm(host)
        return "nothing-pending"
    if is_confirmed(host, pending):
        finalize(host)
        return "finalized"
    if not from_timer and now + 5 < float(pending.get("deadline", 0)):
        return "not-due"
    restore_snapshot(host)
    host.remove(PENDING)
    host.write_atomic(REVERTED, json.dumps({"at": now, "nonce": pending["nonce"]}), mode=0o644)
    _disarm(host)
    if cfg:
        notify.enqueue(host, "warn", t_in(cfg.language, "lockdown.reverted_title"),
                       t_in(cfg.language, "lockdown.reverted_body"), source="lockdown")
    return "reverted"


def wait_for_confirmation(host: Host, timeout_s: float, poll_s: float = 3.0,
                          on_tick=None) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        pending = read_pending(host)
        if not pending:
            return is_applied(host)
        if is_confirmed(host, pending):
            return finalize(host)
        if on_tick:
            on_tick(int(deadline - time.time()))
        time.sleep(poll_s)
    return False


def ensure_privsep_dir(host: Host) -> None:
    """sshd refuses to test or print its configuration without /run/sshd.
    With socket activation (Ubuntu 24.04) the directory only exists once
    ssh.service has run, which on a fresh install may be never. The same
    missing directory keeps an SSH service from starting after a reboot.
    The kit's tmpfiles entry recreates it at every boot."""
    if not host.exists("/run/sshd"):
        try:
            host.mkdir("/run/sshd", mode=0o755)
        except OSError:
            pass


def ssh_effective(host: Host) -> Dict[str, str]:
    ensure_privsep_dir(host)
    r = host.run(["sshd", "-T", "-C", "user=root,host=localhost,addr=127.0.0.1"])
    out: Dict[str, str] = {}
    for line in r.stdout.splitlines():
        k, _, v = line.partition(" ")
        out[k.lower()] = v.strip()
    return out


def owner_can_confirm() -> bool:
    return os.access(os.path.dirname(PENDING), os.W_OK)
