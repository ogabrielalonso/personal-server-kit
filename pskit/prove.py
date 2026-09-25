"""Final proof: reboot, then confirm everything came back on its own.

Some defects only show up after a reboot: a missing runtime directory
that keeps an SSH service from starting, or a boot ordering cycle. So the install ends with one, and the result
reaches the owner through their alert channel.
"""

from __future__ import annotations

import json
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import lockdown as L
from . import manifest as mf
from . import notify
from .alerts import AlertState
from .checks import Probes, listening_ports, receipt, tailscale_state, write_receipt
from .config import HostConfig, KitConfig
from .i18n import t, t_in
from .paths import BRAIN_USER_LINUX, SLICE_BRAIN, SYS
from .system import Host

PENDING = f"{SYS.proof}/pending"

Check = Tuple[str, bool, str]


def boot_id(host: Host) -> str:
    return (host.read_text("/proc/sys/kernel/random/boot_id", "") or "").strip()


def checks(host: Host, cfg: HostConfig, kit: KitConfig, run_e2e: bool = True,
           health_wait_s: float = 120) -> List[Check]:
    out: List[Check] = []
    if kit.tailscale_expected:
        ts = tailscale_state(host)
        out.append(("tailscale", ts == "Running", ts or "not running"))
    out.append(("ssh-listening", 22 in listening_ports(host), "port 22"))
    if L.is_applied(host):
        eff = L.ssh_effective(host)
        r = host.run(["ufw", "status"])
        out.append(("firewall", r.ok and "Status: active" in r.stdout, "ufw"))
        out.append(("ssh-keys-only", eff.get("passwordauthentication") == "no", "sshd"))
    probes = Probes(host, cfg, kit, AlertState(host))
    for name in probes.expected_units():
        out.append((f"unit:{name}", host.run(["systemctl", "is-active", "--quiet", name]).ok, name))
    raw = host.read_text(SYS.brain_manifest)
    if raw:
        try:
            man = mf.load(raw)
        except mf.ManifestError:
            man = None
            out.append(("brain-manifest", False, "invalid"))
        if man and man.health_url:
            from .brainslot import wait_healthy
            out.append(("brain-health", bool(wait_healthy(man, timeout_s=health_wait_s)), man.health_url))
        if man and man.e2e_command and run_e2e:
            ok, detail = run_brain_e2e(host, cfg, man)
            out.append(("brain-e2e", ok, detail))
    return out


def run_brain_e2e(host: Host, cfg: HostConfig, man: mf.Manifest,
                  timeout: Optional[float] = None) -> Tuple[bool, str]:
    e2e = list(man.e2e_command or [])
    if not e2e:
        return True, "no e2e test declared"
    cmd = ["systemd-run", "--wait", "--pipe", "--quiet", "--collect",
           f"--uid={BRAIN_USER_LINUX}", f"--gid={BRAIN_USER_LINUX}", f"--slice={SLICE_BRAIN}",
           f"--working-directory={cfg.brain_home}",
           f"--setenv=PSKIT_HOST_CONFIG={SYS.host_config}", f"--setenv=BRAIN_HOME={cfg.brain_home}",
           "--"] + e2e
    r = host.run(cmd, timeout=timeout or man.e2e_timeout)
    tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]
    return r.ok, tail[0][:200]


def summarize(results: List[Check]) -> Tuple[int, int, List[str]]:
    failed = [name for name, ok, _ in results if not ok]
    return len(results) - len(failed), len(results), failed


def arm(host: Host) -> Dict:
    data = {"armed_at": time.time(), "boot_id": boot_id(host)}
    host.mkdir(SYS.proof, mode=0o755)
    host.write_atomic(PENDING, json.dumps(data), mode=0o644)
    return data


def after_boot(host: Host, cfg: HostConfig, kit: KitConfig, retries: int = 15, wait_s: float = 20,
               sleep: Callable[[float], None] = time.sleep, send: Optional[Callable[[str, str], None]] = None,
               budget_s: float = 420) -> Dict:
    """Checks after the reboot and always reports, within budget_s: the
    unit's own timeout must never be what ends the proof, or the owner
    would not hear about the failure the proof exists for."""
    deadline = time.monotonic() + budget_s
    raw = host.read_text(PENDING)
    pending = json.loads(raw) if raw else {}
    rebooted = bool(pending) and pending.get("boot_id") != boot_id(host)
    results: List[Check] = []
    for _ in range(retries):
        results = checks(host, cfg, kit, run_e2e=False, health_wait_s=30)
        if all(ok for _, ok, _ in results) or time.monotonic() + wait_s > deadline:
            break
        sleep(wait_s)
    raw_m = host.read_text(SYS.brain_manifest)
    if raw_m:
        try:
            man = mf.load(raw_m)
            if man.e2e_command:
                left = max(30.0, deadline - time.monotonic())
                ok, detail = run_brain_e2e(host, cfg, man, timeout=min(man.e2e_timeout, left))
                results.append(("brain-e2e", ok, detail))
        except mf.ManifestError:
            pass
    passed, total, failed = summarize(results)
    ok = rebooted and not failed if pending else not failed
    rec = {"ok": ok, "rebooted": rebooted, "passed": passed, "total": total, "failed": failed,
           "checks": [{"name": n, "ok": o, "detail": d} for n, o, d in results], "finished": time.time()}
    write_receipt(host, "proof", rec)
    host.remove(PENDING)
    lang = cfg.language
    if ok:
        title = t_in(lang, "proof.ok_title")
        body = t_in(lang, "proof.ok_body", passed=passed, total=total)
    else:
        title = t_in(lang, "proof.fail_title")
        body = t_in(lang, "proof.fail_body", passed=passed, total=total, failed=", ".join(failed) or "reboot")
    text = notify.format_message(cfg, title, body)
    try:
        (send or notify.channel_from_config(host, cfg).send)(text, "warn")
    except notify.DeliveryError:
        notify.enqueue(host, "warn", title, body, source="proof")
    return rec


def status(host: Host) -> Optional[Dict]:
    return receipt(host, "proof")


def status_text(rec: Dict) -> str:
    """The last proof in one readable line; the receipt stays behind --json."""
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(float(rec.get("finished", 0))))
    passed, total = rec.get("passed", 0), rec.get("total", 0)
    if rec.get("ok"):
        head, body = t("proof.ok_title"), t("proof.ok_body", passed=passed, total=total)
    else:
        failed = ", ".join(rec.get("failed") or []) or "?"
        head, body = t("proof.fail_title"), t("proof.fail_body", passed=passed, total=total, failed=failed)
    return f"{head[:1].upper()}{head[1:]} ({when}). {body}"
