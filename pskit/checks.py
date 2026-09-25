"""Health probes. Each returns findings with stable keys; messages are in
the owner's language and name things plainly (the unit name goes in
parentheses, for whoever helps the owner)."""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import time
import urllib.request
from typing import Callable, Dict, List, Optional, Tuple

from .alerts import AlertState, Finding, graded, graded_low
from .config import HostConfig, KitConfig
from .i18n import t_in
from .paths import SYS, unit
from .system import Host


def receipt(host: Host, name: str) -> Optional[Dict]:
    raw = host.read_text(f"{SYS.receipts}/{name}.json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def write_receipt(host: Host, name: str, data: Dict) -> None:
    data = dict(data, written=time.time())
    host.write_atomic(f"{SYS.receipts}/{name}.json", json.dumps(data, indent=1, sort_keys=True), mode=0o644)


def listening_ports(host: Host) -> set:
    """TCP ports in LISTEN state, from /proc (no ss or netstat needed)."""
    ports = set()
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        text = host.read_text(f, "") or ""
        for line in text.splitlines()[1:]:
            parts = line.split()
            if len(parts) > 3 and parts[3] == "0A":
                try:
                    ports.add(int(parts[1].rsplit(":", 1)[1], 16))
                except (ValueError, IndexError):
                    continue
    return ports


def ssh_ports(host: Host) -> set:
    """Ports sshd is configured to listen on (the owner may have moved it
    from 22). Falls back to 22 when the configuration cannot be read."""
    from .lockdown import ensure_privsep_dir
    ensure_privsep_dir(host)
    r = host.run(["sshd", "-T", "-C", "user=root,host=localhost,addr=127.0.0.1"])
    ports = set()
    for line in r.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key.lower() == "port" and value.strip().isdigit():
            ports.add(int(value.strip()))
    return ports or {22}


def friendly_unit(lang: str, name: str) -> str:
    label = t_in(lang, f"unit.{name}")
    return name if label == f"unit.{name}" else f"{label} ({name})"


class Probes:
    def __init__(self, host: Host, cfg: HostConfig, kit: KitConfig, state: AlertState,
                 now: Optional[float] = None, http_get: Optional[Callable[[str, float], Tuple[int, bytes]]] = None):
        self.host = host
        self.cfg = cfg
        self.kit = kit
        self.state = state
        self.now = now or time.time()
        self.lang = cfg.language
        self.http_get = http_get or _http_get

    def m(self, key: str, **kw) -> str:
        return t_in(self.lang, key, **kw)

    # ---- storage -----------------------------------------------------
    def disk_targets(self) -> List[str]:
        paths = ["/", self.cfg.workspace, self.cfg.brain_home, "/var"]
        paths += list(self.kit.extra_mounts)
        seen_dev: Dict[int, str] = {}
        for p in paths:
            if not p:
                continue
            target = self.host.p(p)
            while not target.exists() and target != target.parent:
                target = target.parent
            try:
                dev = os.stat(target).st_dev
            except OSError:
                continue
            seen_dev.setdefault(dev, p)
        return list(seen_dev.values())

    def disk(self) -> List[Finding]:
        out: List[Finding] = []
        for path in self.disk_targets():
            total, used, free = self.host.disk_usage(path)
            pct = 100.0 * used / max(1, used + free)
            key = f"disk:{path}"
            level = graded(pct, self.state.previous_level(key), self.kit.disk_warn_pct,
                           self.kit.disk_crit_pct, self.kit.disk_hysteresis_pct)
            out.append(Finding(key, level, self.m("alert.disk", path=path, pct=int(pct + 0.999)), pct))
        return out

    # ---- memory ------------------------------------------------------
    def memory(self) -> List[Finding]:
        out: List[Finding] = []
        mi = self.host.meminfo_kib()
        if mi.get("MemTotal"):
            avail = 100.0 * mi.get("MemAvailable", 0) / mi["MemTotal"]
            key = "mem:available"
            level = graded_low(avail, self.state.previous_level(key), self.kit.mem_available_warn_pct,
                               self.kit.mem_available_warn_pct / 2, 3)
            out.append(Finding(key, level, self.m("alert.mem", pct=int(avail)), avail))
        if mi.get("SwapTotal"):
            used = 100.0 * (mi["SwapTotal"] - mi.get("SwapFree", 0)) / mi["SwapTotal"]
            key = "swap:used"
            level = graded(used, self.state.previous_level(key), self.kit.swap_warn_pct, 95, 5)
            out.append(Finding(key, level, self.m("alert.swap", pct=int(used)), used))
        return out

    # ---- access ------------------------------------------------------
    def access(self) -> List[Finding]:
        out: List[Finding] = []
        if self.kit.tailscale_expected:
            ts = tailscale_state(self.host)
            out.append(Finding("access:tailscale", "ok" if ts == "Running" else "crit",
                               self.m("alert.tailscale", state=ts or "?")))
        ports = ssh_ports(self.host)
        ssh_ok = bool(ports & listening_ports(self.host))
        out.append(Finding("access:ssh", "ok" if ssh_ok else "crit", self.m("alert.ssh")))
        if self.lockdown_done():
            r = self.host.run(["ufw", "status"])
            active = r.ok and "Status: active" in r.stdout
            out.append(Finding("access:firewall", "ok" if active else "crit", self.m("alert.firewall_off")))
        return out

    def lockdown_done(self) -> bool:
        return self.host.exists(f"{SYS.lockdown}/applied.json")

    # ---- services ----------------------------------------------------
    def expected_units(self) -> List[str]:
        names = [unit("health.timer"), unit("notify.path"), unit("digest.timer"), unit("cache.timer"),
                 unit("local-bridge.timer")]
        if self.kit.reaper_enabled:
            names.append(unit("reaper.timer"))
        if self.kit.pressure_guard_enabled:
            names.append(unit("pressure-guard.service"))
        if self.kit.heartbeat_enabled:
            names.append(unit("heartbeat.timer"))
        if self.kit.backup_kind != "later":
            names += [unit("backup.timer"), unit("backup-retry.timer"), unit("restore-test.timer")]
        if self.kit.tailscale_expected:
            names.append("tailscaled.service")
        names += brain_units(self.host, active_only=True)
        return names

    def services(self) -> List[Finding]:
        out: List[Finding] = []
        for name in self.expected_units():
            ok = self.host.run(["systemctl", "is-active", "--quiet", name]).ok
            out.append(Finding(f"unit:{name}", "ok" if ok else "warn",
                               self.m("alert.unit_inactive", unit=friendly_unit(self.lang, name))))
        r = self.host.run(["systemctl", "list-units", "--failed", "--plain", "--no-legend", f"{unit('*')}"])
        failed = [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()] if r.ok else []
        for name in failed:
            out.append(Finding(f"failed:{name}", "warn",
                               self.m("alert.unit_failed", unit=friendly_unit(self.lang, name))))
        return out

    # ---- updates, clock ----------------------------------------------
    def updates(self) -> List[Finding]:
        out: List[Finding] = []
        path = "/run/reboot-required"
        if self.host.exists(path):
            age_days = (self.now - self.host.p(path).stat().st_mtime) / 86400
            level = "warn" if age_days >= 7 else "info"
            out.append(Finding("updates:reboot", level, self.m("alert.reboot_required", days=int(age_days))))
        else:
            out.append(Finding("updates:reboot", "ok", ""))
        r = self.host.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
        if r.ok:
            out.append(Finding("time:sync", "ok" if r.stdout.strip() == "yes" else "warn",
                               self.m("alert.time_unsynced")))
        return out

    # ---- backup ------------------------------------------------------
    def backup(self) -> List[Finding]:
        if self.kit.backup_kind == "later":
            return [Finding("backup:configured", "info", self.m("alert.backup_not_configured"))]
        out = [Finding("backup:configured", "ok", "")]
        rec = receipt(self.host, "backup-success")
        if not rec:
            installed = receipt(self.host, "install") or {}
            since = installed.get("written", self.now)
            level = "warn" if self.now - since > self.kit.backup_max_age_hours * 3600 else "ok"
            out.append(Finding("backup:age", level, self.m("alert.backup_never")))
        else:
            hours = (self.now - float(rec.get("finished", 0))) / 3600
            level = "warn" if hours > self.kit.backup_max_age_hours else "ok"
            out.append(Finding("backup:age", level, self.m("alert.backup_stale", hours=int(hours))))
        rt = receipt(self.host, "restore-test")
        if rt:
            days = (self.now - float(rt.get("finished", 0))) / 86400
            if not rt.get("ok"):
                out.append(Finding("backup:restore", "warn", self.m("alert.restore_failed")))
            else:
                out.append(Finding("backup:restore", "warn" if days > 9 else "ok",
                                   self.m("alert.restore_stale", days=int(days))))
        return out

    # ---- brain -------------------------------------------------------
    def brain(self) -> List[Finding]:
        from . import manifest as mf
        raw = self.host.read_text(SYS.brain_manifest)
        if not raw:
            return []
        try:
            man = mf.load(raw)
        except mf.ManifestError:
            return [Finding("brain:manifest", "warn", self.m("alert.brain_manifest_invalid"))]
        if not man.health_url:
            return []
        ok = False
        try:
            status, body = self.http_get(man.health_url, man.health_timeout)
            if status == 200:
                data = json.loads(body.decode("utf-8") or "{}")
                ok = str(data.get("status", "")) == man.health_expect
        except Exception:
            ok = False
        return [Finding("brain:health", "ok" if ok else "warn", self.m("alert.brain_down"))]

    # ---- devices -----------------------------------------------------
    def devices(self) -> List[Finding]:
        out: List[Finding] = []
        try:
            devices = json.loads(self.host.read_text(SYS.devices, "{}") or "{}")
        except json.JSONDecodeError:
            return out
        inbox = f"{self.cfg.brain_home}/{self.cfg.session_inbox}"
        for name in sorted(devices):
            raw = self.host.read_text(f"{inbox}/{name}/.pskit-delivery.json")
            last = devices[name].get("paired_at", self.now)
            if raw:
                try:
                    last = float(json.loads(raw).get("finished", last))
                except (json.JSONDecodeError, TypeError, ValueError):
                    pass
            days = (self.now - float(last)) / 86400
            level = "info" if days > self.kit.device_silence_days else "ok"
            out.append(Finding(f"device:{name}", level, self.m("alert.device_silent", device=name, days=int(days))))
        return out

    # ---- all ---------------------------------------------------------
    def collect(self) -> Tuple[List[Finding], Tuple[str, ...]]:
        findings: List[Finding] = []
        crashed: List[str] = []
        probes = [("disk", self.disk), ("mem", self.memory), ("swap", lambda: []), ("access", self.access),
                  ("unit", self.services), ("failed", lambda: []), ("updates", self.updates),
                  ("time", lambda: []), ("backup", self.backup), ("brain", self.brain), ("device", self.devices)]
        for name, fn in probes:
            try:
                findings += fn()
            except Exception as exc:
                crashed.append(f"{name}:")
                findings.append(Finding(f"probe:{name}", "warn",
                                        self.m("alert.probe_failed", probe=name) + f" [{type(exc).__name__}]"))
        if "unit:" in crashed:
            crashed.append("failed:")
        if "mem:" in crashed:
            crashed.append("swap:")
        if "updates:" in crashed:
            crashed.append("time:")
        return findings, tuple(crashed)


def tailscale_state(host: Host) -> str:
    exe = host.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    r = host.run([exe, "status", "--json"], timeout=15)
    if not r.stdout.strip():
        return ""
    try:
        return str(json.loads(r.stdout).get("BackendState", ""))
    except json.JSONDecodeError:
        return ""


def is_tailnet_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip in ipaddress.ip_network("100.64.0.0/10") or ip in ipaddress.ip_network("fd7a:115c:a1e0::/48")


def tailnet_address_of(address: str) -> str:
    """The tailnet IP behind what the owner typed: the IP itself, or what a
    MagicDNS name resolves to. Empty when it is not on the tailnet."""
    if is_tailnet_address(address):
        return address
    try:
        infos = socket.getaddrinfo(address, None)
    except (OSError, UnicodeError):
        return ""
    for info in infos:
        ip = str(info[4][0])
        if is_tailnet_address(ip):
            return ip
    return ""


def tailnet_policy_blocks(host: Host, address: str, port: int, timeout_s: float = 5) -> bool:
    """True when Tailscale reaches the peer but a TCP connection does not:
    the tailnet's access rules drop the traffic. The default policy lets
    all of the owner's devices talk; a customised one may not, and then
    pairing and an SFTP backup just time out (found on a real run)."""
    exe = host.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    r = host.run([exe, "ping", "-c", "1", "--timeout", "5s", address], timeout=20)
    if "pong" not in (r.stdout or ""):
        return False
    try:
        with socket.create_connection((address, port), timeout=timeout_s):
            return False
    except OSError:
        return True


def tailscale_self(host: Host) -> Dict[str, str]:
    exe = host.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    r = host.run([exe, "status", "--json"], timeout=15)
    try:
        data = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    me = data.get("Self") or {}
    ips = [ip for ip in me.get("TailscaleIPs") or [] if ":" not in ip]
    return {"ip": ips[0] if ips else "", "dns": str(me.get("DNSName", "")).rstrip("."),
            "state": str(data.get("BackendState", ""))}


def brain_units(host: Host, active_only: bool = False) -> List[str]:
    """Units generated from the brain manifest (recorded at apply time)."""
    raw = host.read_text(f"{SYS.state}/brain-units.json")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return list(data.get("expect_active", [])) if active_only else list(data.get("all", []))


def _http_get(url: str, timeout: float) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "pskit-health"})  # noqa: S310
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback url validated by manifest
        return resp.status, resp.read(65536)
