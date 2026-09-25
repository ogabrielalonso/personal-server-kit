"""`pskit doctor`: plain-language checks, read-only.

Three modes:
  server   a machine installed with the kit (host.json exists)
  device   a laptop paired with one or more servers
  --audit  any Ubuntu or macOS machine, installed or not: the generic
           checks the kit's contract is built on
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.request
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

from . import lockdown as L
from .alerts import AlertState
from .budget import GIB, human
from .checks import Probes, receipt, tailscale_self
from .config import load_host_config, load_kit_config
from .i18n import t
from .paths import SYS
from .system import Host


@dataclass
class Item:
    key: str
    level: str  # ok, info, warn, crit, unknown
    text: str
    detail: str = ""


# ---- server --------------------------------------------------------------

def server_items(host: Host) -> List[Item]:
    cfg = load_host_config(host)
    if not cfg:
        return []
    kit = load_kit_config(host)
    items: List[Item] = []
    state = AlertState(host)
    findings, crashed = Probes(host, cfg, kit, state).collect()
    for f in findings:
        if f.level == "ok":
            prefix, _, name = f.key.partition(":")
            text = t(f"doctor.ok_{prefix}", name=name, value=int(f.value or 0))
        else:
            text = f.text
        items.append(Item(f.key, f.level, text))
    journal = host.read_text(SYS.journal)
    if journal:
        try:
            steps = json.loads(journal).get("steps", {})
        except json.JSONDecodeError:
            steps = {}
        for sid, st in sorted(steps.items()):
            if st.get("status") in ("failed", "interrupted", "waiting-reboot"):
                items.append(Item(f"step:{sid}", "warn", t("doctor.step_unfinished", step=sid, status=st["status"]),
                                  st.get("detail", "")[-300:]))
    pending = L.read_pending(host)
    if pending:
        left = int(pending.get("deadline", 0) - time.time())
        items.append(Item("lockdown:pending", "warn", t("doctor.lockdown_pending", seconds=max(0, left))))
    retry = host.p(f"{SYS.state}/notify-retry")
    if retry.is_dir():
        n = len([p for p in retry.iterdir() if p.suffix == ".json"])
        if n:
            items.append(Item("notify:retry", "warn", t("doctor.notify_retry", n=n)))
    proof = receipt(host, "proof")
    if proof is None:
        items.append(Item("proof", "info", t("doctor.proof_never")))
    elif not proof.get("ok"):
        items.append(Item("proof", "warn", t("doctor.proof_failed", failed=", ".join(proof.get("failed", [])))))
    else:
        items.append(Item("proof", "ok", t("doctor.proof_ok", passed=proof.get("passed"), total=proof.get("total"))))
    items += tailscale_expiry(host)
    return items


def tailscale_expiry(host: Host) -> List[Item]:
    exe = host.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    r = host.run([exe, "status", "--json"], timeout=15)
    try:
        me = json.loads(r.stdout or "{}").get("Self") or {}
    except json.JSONDecodeError:
        return []
    exp = me.get("KeyExpiry")
    if not exp:
        return [Item("tailscale:expiry", "ok", t("doctor.ts_no_expiry"))] if me else []
    from .runtime.backup import _parse_time
    days = (_parse_time(exp) - time.time()) / 86400
    level = "crit" if days < 14 else "warn"
    return [Item("tailscale:expiry", level, t("doctor.ts_expiry", days=int(days)))]


# ---- device --------------------------------------------------------------

def device_items(home: Optional[str] = None) -> List[Item]:
    from .device import agents
    from .device.bridge_push import load_state
    from .device.local import Paths, list_servers, load_server
    from .device.pair import is_mac
    paths = Paths(home)
    items: List[Item] = []
    servers = list_servers(paths)
    if not servers:
        return items
    host = Host()
    me = tailscale_self(host)
    items.append(Item("device:tailscale", "ok" if me.get("state") == "Running" else "crit",
                      t("doctor.dev_tailscale", state=me.get("state") or "?")))
    cfg_text = (paths.ssh / "config").read_text() if (paths.ssh / "config").exists() else ""
    items.append(Item("device:ssh-include", "ok" if str(paths.ssh_kit) in cfg_text else "warn",
                      t("doctor.dev_ssh_include")))
    for s in servers:
        info = load_server(paths, s) or {}
        kd = paths.server_dir(s)
        items.append(Item(f"device:{s}:known-hosts", "ok" if (kd / "known_hosts").exists() else "warn",
                          t("doctor.dev_known_hosts", server=s)))
        for name, ok in agents.running(paths, s, is_mac()).items():
            items.append(Item(f"device:{s}:{name}", "ok" if ok else "warn", t("doctor.dev_agent", job=name)))
        port = int(info.get("brain_port", 8799))
        listening = _port_open(port)
        items.append(Item(f"device:{s}:tunnel", "ok" if listening else "warn", t("doctor.dev_tunnel", port=port)))
        if listening:
            items.append(Item(f"device:{s}:brain", "ok" if _brain_ok(port) else "info",
                              t("doctor.dev_brain", port=port)))
        st = load_state(paths, s)
        last = st.get("last_success")
        if last:
            hours = int((time.time() - float(last)) / 3600)
            items.append(Item(f"device:{s}:bridge", "ok" if hours < 24 else "warn",
                              t("doctor.dev_bridge", hours=hours)))
        else:
            err = (st.get("last") or {}).get("error", "")
            items.append(Item(f"device:{s}:bridge", "warn", t("doctor.dev_bridge_never"), err))
    return items


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


def _brain_ok(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


# ---- audit ---------------------------------------------------------------

def audit_items(host: Host) -> List[Item]:
    items: List[Item] = []
    if host.platform != "linux":
        items.append(Item("audit:platform", "info", t("doctor.audit_linux_only")))
        return items
    osr = host.os_release()
    items.append(Item("audit:os", "ok" if osr.get("VERSION_ID") == "24.04" else "info",
                      f"{osr.get('PRETTY_NAME', '?')}, kernel {os.uname().release}"))
    items.append(Item("audit:cgroup2", "ok" if host.exists("/sys/fs/cgroup/cgroup.controllers") else "warn",
                      t("doctor.audit_cgroup2")))
    mem = host.meminfo_kib()
    ram = mem.get("MemTotal", 0) * 1024
    swap = mem.get("SwapTotal", 0) * 1024
    items.append(Item("audit:ram", "ok", t("doctor.audit_ram", ram=human(ram), swap=human(swap))))
    items += audit_slices(host, ram)
    items += audit_listeners(host)
    items += audit_firewall(host)
    items += audit_sshd(host)
    items += audit_disks(host)
    tmp_rule = host.read_text("/etc/tmpfiles.d/tmp.conf") or host.read_text("/usr/lib/tmpfiles.d/tmp.conf") or ""
    age = next((ln.split()[-1] for ln in tmp_rule.splitlines() if ln.startswith("D /tmp")), "?")
    items.append(Item("audit:tmp-age", "ok" if age not in ("?", "-", "30d") else "warn",
                      t("doctor.audit_tmp", age=age)))
    reboot = host.exists("/run/reboot-required")
    items.append(Item("audit:reboot", "warn" if reboot else "ok",
                      t("doctor.audit_reboot_yes" if reboot else "doctor.audit_reboot_no")))
    r = host.run(["apt-config", "dump"])
    ua = r.ok and 'APT::Periodic::Unattended-Upgrade "1";' in r.stdout
    items.append(Item("audit:updates", "ok" if ua else "warn", t("doctor.audit_updates")))
    r = host.run(["timedatectl", "show", "-p", "NTPSynchronized", "--value"])
    synced = r.stdout.strip() == "yes"
    items.append(Item("audit:time", "ok" if synced else "warn",
                      t("doctor.ok_time" if synced else "alert.time_unsynced")))
    me = tailscale_self(host)
    items.append(Item("audit:tailscale", "ok" if me.get("state") == "Running" else "warn",
                      t("doctor.dev_tailscale", state=me.get("state") or "?")))
    items += tailscale_expiry(host)
    r = host.run(["systemctl", "list-units", "--failed", "--plain", "--no-legend"])
    failed = [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]
    items.append(Item("audit:failed-units", "warn" if failed else "ok", t("doctor.audit_failed", n=len(failed)),
                      ", ".join(failed[:20])))
    return items


def audit_slices(host: Host, ram: int) -> List[Item]:
    r = host.run(["systemctl", "list-units", "--type=slice", "--all", "--plain", "--no-legend"])
    names = [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]
    capped: Dict[str, int] = {}
    for n in names:
        v = host.run(["systemctl", "show", "-p", "MemoryMax", "--value", n]).stdout.strip()
        if v.isdigit() and int(v) < ram:
            capped[n] = int(v)
    # Only top-level caps count toward the sum: a child is inside its parent.
    top = {n: v for n, v in capped.items()
           if not any(n != p and n.startswith(p[:-len(".slice")] + "-") for p in capped)}
    total = sum(top.values())
    margin = ram - total
    detail = ", ".join(f"{n}={human(v)}" for n, v in sorted(top.items()))
    if not top:
        return [Item("audit:slices", "warn", t("doctor.audit_no_caps"))]
    level = "ok" if margin >= min(2 * GIB, ram * 0.08) else "warn"
    return [Item("audit:slices", level, t("doctor.audit_caps", total=human(total), ram=human(ram),
                                          margin=human(max(0, margin))), detail)]


def audit_listeners(host: Host) -> List[Item]:
    exposed = []
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in (host.read_text(f, "") or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) < 4 or parts[3] != "0A":
                continue
            addr, port = parts[1].rsplit(":", 1)
            port_n = int(port, 16)
            ip = _hex_ip(addr)
            if ip == "::1" or ip.startswith(("127.", "100.", "fd7a:115c:a1e0")):
                continue
            exposed.append(f"{ip}:{port_n}")
    exposed = sorted(set(exposed))
    return [Item("audit:listeners", "info" if exposed else "ok", t("doctor.audit_listeners", n=len(exposed)),
                 ", ".join(exposed[:30]))]


def _hex_ip(h: str) -> str:
    try:
        if len(h) == 8:
            b = bytes.fromhex(h)[::-1]
            return ".".join(str(x) for x in b)
        raw = bytes.fromhex(h)
        words = [raw[i:i + 4][::-1] for i in range(0, 16, 4)]
        full = b"".join(words)
        import ipaddress
        return str(ipaddress.IPv6Address(full))
    except ValueError:
        return h


def audit_firewall(host: Host) -> List[Item]:
    r = host.run(["ufw", "status"])
    if r.returncode != 0 and "root" in (r.stderr + r.stdout).lower():
        return [Item("audit:firewall", "unknown", t("doctor.needs_root", what="ufw"))]
    active = "Status: active" in r.stdout
    text = t("doctor.audit_firewall_on" if active else "doctor.audit_firewall_off")
    return [Item("audit:firewall", "ok" if active else "warn", text, r.stdout.strip()[:400])]


def audit_sshd(host: Host) -> List[Item]:
    eff = L.ssh_effective(host)
    if not eff:
        return [Item("audit:sshd", "unknown", t("doctor.needs_root", what="sshd -T"))]
    pw = eff.get("passwordauthentication", "?")
    root = eff.get("permitrootlogin", "?")
    ok = pw == "no" and root in ("no", "prohibit-password", "without-password")

    def word(v: str) -> str:
        return {"no": t("doctor.value_off"), "yes": t("doctor.value_on"),
                "prohibit-password": t("doctor.value_keys_only"),
                "without-password": t("doctor.value_keys_only")}.get(v, v)

    return [Item("audit:sshd", "ok" if ok else "warn", t("doctor.audit_sshd", password=word(pw), root=word(root)))]


def audit_disks(host: Host) -> List[Item]:
    out = []
    seen = set()
    for line in (host.read_text("/proc/mounts", "") or "").splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].startswith("/dev/") or parts[2] in ("squashfs", "iso9660"):
            continue
        mnt = parts[1].replace("\\040", " ")
        try:
            dev = os.stat(mnt).st_dev
        except OSError:
            continue
        if dev in seen:
            continue
        seen.add(dev)
        total, used, free = host.disk_usage(mnt)
        pct = int(100 * used / max(1, used + free) + 0.999)
        level = "crit" if pct >= 90 else "warn" if pct >= 80 else "ok"
        out.append(Item(f"audit:disk:{mnt}", level, t("alert.disk", path=mnt, pct=pct), human(free) + " free"))
    return out


# ---- output --------------------------------------------------------------

SYMBOL = {"ok": "ok", "info": "--", "warn": "!!", "crit": "XX", "unknown": "??"}


def render(items: List[Item]) -> str:
    lines = []
    for it in items:
        lines.append(f"  [{SYMBOL.get(it.level, '??')}] {it.text}")
        if it.detail and it.level != "ok":
            lines.append(f"         {it.detail}")
    return "\n".join(lines)


def run(host: Host, audit: bool = False, as_json: bool = False) -> int:
    items: List[Item] = []
    if audit:
        items = audit_items(host)
    else:
        if host.exists(SYS.host_config):
            if os.geteuid() == 0:
                items += server_items(host)
            else:
                items.append(Item("server", "info", t("doctor.server_needs_root")))
        items += device_items()
        if not items:
            print(t("doctor.nothing"))
            return 0
    if as_json:
        print(json.dumps([asdict(i) for i in items], indent=1, ensure_ascii=False))
    else:
        print(render(items))
        bad = [i for i in items if i.level in ("warn", "crit")]
        print()
        print(t("doctor.summary_ok") if not bad else t("doctor.summary_bad", n=len(bad)))
    return 1 if any(i.level == "crit" for i in items) else 0
