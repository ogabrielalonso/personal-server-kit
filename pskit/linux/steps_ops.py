"""Scenario B, operations: alerts, backup, runtime services, brain slot,
optional modules and the final proof."""

from __future__ import annotations

from typing import Dict, List, Optional

from .. import brainslot, prove, setup_alerts, setup_backup
from ..i18n import t
from ..paths import BRAIN_USER_LINUX, SLICE_CI, SYS, unit
from ..render import render
from ..state import install_file
from ..steps import Context, Step, StepFailed
from . import apt
from . import systemd as sd


class Alerts(Step):
    id = "alerts"
    title = "step.alerts"

    def check(self, ctx: Context) -> bool:
        if ctx.journal.status(self.id) != "done":
            return False
        return ctx.cfg.alert_channel == "none" or ctx.host.exists(f"{SYS.secrets}/notify.env")

    def apply(self, ctx: Context) -> None:
        setup_alerts.configure(ctx)

    def verify(self, ctx: Context) -> Optional[str]:
        return None


class Heartbeat(Step):
    id = "heartbeat"
    title = "step.heartbeat"

    def check(self, ctx: Context) -> bool:
        return ctx.journal.status(self.id) == "done"

    def apply(self, ctx: Context) -> None:
        setup_alerts.configure_heartbeat(ctx)

    def verify(self, ctx: Context) -> Optional[str]:
        return None


class Backup(Step):
    id = "backup"
    title = "step.backup"

    def check(self, ctx: Context) -> bool:
        return setup_backup.configured(ctx)

    def apply(self, ctx: Context) -> None:
        setup_backup.configure(ctx)

    def verify(self, ctx: Context) -> Optional[str]:
        if ctx.kit.backup_kind == "later":
            return None
        return None if setup_backup.configured(ctx) else t("backup.not_proven")


def runtime_units(ctx: Context) -> Dict[str, str]:
    c, k = ctx.cfg, ctx.kit
    home = ctx.host.user_home(c.owner) or f"/home/{c.owner}"
    v = {
        "pskit": SYS.shim, "owner": c.owner, "owner_group": c.owner, "owner_home": home,
        "brain_group": BRAIN_USER_LINUX, "spool": SYS.spool, "health_interval_min": k.health_interval_min,
        "digest_time": k.digest_time, "cache_time": k.cache_time, "backup_time": k.backup_time,
        "backup_retry_time": k.backup_retry_time, "proof_pending": prove.PENDING,
        "backup_slice": SLICE_CI if "ci" in k.modules else "system.slice",
    }
    names = ["health.service", "health.timer", "notify.path", "notify.service", "digest.service", "digest.timer",
             "cache.service", "cache.timer", "local-bridge.service", "local-bridge.timer", "prove-boot.service",
             "reaper.service", "reaper.timer", "pressure-guard.service", "heartbeat.service", "heartbeat.timer",
             "backup.service", "backup.timer", "backup-retry.service", "backup-retry.timer",
             "restore-test.service", "restore-test.timer"]
    return {unit(n): render(f"systemd/{unit(n)}", v) for n in names}


def wanted_active(ctx: Context):
    k = ctx.kit
    on = [unit("health.timer"), unit("notify.path"), unit("digest.timer"), unit("cache.timer"),
          unit("local-bridge.timer")]
    off: List[str] = []
    for enabled, names in ((k.reaper_enabled, [unit("reaper.timer")]),
                           (k.pressure_guard_enabled, [unit("pressure-guard.service")]),
                           (k.heartbeat_enabled, [unit("heartbeat.timer")]),
                           (k.backup_kind != "later", [unit("backup.timer"), unit("backup-retry.timer"),
                                                       unit("restore-test.timer")])):
        (on if enabled else off).extend(names)
    return on, off


class Runtime(Step):
    id = "runtime"
    title = "step.runtime"

    def check(self, ctx: Context) -> bool:
        units = runtime_units(ctx)
        if not all(ctx.host.read_text(f"{sd.UNIT_DIR}/{n}") == c for n, c in units.items()):
            return False
        on, off = wanted_active(ctx)
        return all(sd.is_active(ctx.host, n) for n in on) and not any(sd.is_active(ctx.host, n) for n in off)

    def apply(self, ctx: Context) -> None:
        for name, content in runtime_units(ctx).items():
            sd.install_unit(ctx.host, ctx.ledger, name, content)
        sd.daemon_reload(ctx.host)
        on, off = wanted_active(ctx)
        for n in on:
            sd.enable_now(ctx.host, ctx.ledger, n)
        for n in off:
            ctx.host.run(["systemctl", "disable", "--now", n])
        sd.enable(ctx.host, ctx.ledger, unit("prove-boot.service"))


class BrainSlot(Step):
    """Reserved place for the brain. With a manifest, its services are
    installed; without one the slot waits (identity, home, memory, inbox,
    lock, port and backup are ready)."""

    id = "brain-slot"
    title = "step.brain_slot"

    def _source(self, ctx: Context) -> Optional[str]:
        path = ctx.answers.get("brain_manifest") or ""
        if path:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        return None

    def check(self, ctx: Context) -> bool:
        src = self._source(ctx)
        if src is None:
            return True
        return ctx.host.read_text(SYS.brain_manifest) == src and bool(ctx.host.read_text(brainslot.UNITS_FILE))

    def apply(self, ctx: Context) -> None:
        src = self._source(ctx)
        if src is None:
            ctx.ui.info(t("brain.reserved", path=ctx.cfg.brain_home))
            return
        from .. import manifest as mf
        try:
            brainslot.apply_linux(ctx.host, ctx.ledger, ctx.cfg, src)
        except mf.ManifestError as exc:
            raise StepFailed(self.id, t("brain.manifest_invalid", problems="; ".join(exc.problems)))
        # The reservation follows the brain's declared peak.
        from .steps_base import MemoryBudget
        mb = MemoryBudget()
        if not mb.check(ctx):
            mb.apply(ctx)
        if brainslot.wait_healthy(brainslot.mf.load(src)) is False:
            raise StepFailed(self.id, t("brain.not_healthy"))

    def verify(self, ctx: Context) -> Optional[str]:
        return None


class CiModule(Step):
    """Optional: a place and a memory ceiling for self-hosted CI runners."""

    id = "module-ci"
    title = "step.module_ci"

    def applies(self, ctx: Context) -> bool:
        return "ci" in ctx.kit.modules

    def check(self, ctx: Context) -> bool:
        return ctx.host.user_exists("ci") and ctx.host.exists(f"{sd.UNIT_DIR}/{SLICE_CI}")

    def apply(self, ctx: Context) -> None:
        h = ctx.host
        if not h.user_exists("ci"):
            base = ctx.cfg.workspace.rsplit("/", 1)[0]
            h.check(["useradd", "--system", "--create-home", "--home-dir", f"{base}/ci", "--shell", "/bin/bash", "ci"])
            ctx.ledger.add("user_created", name="ci", keep=False)
        from .steps_base import MemoryBudget
        MemoryBudget().apply(ctx)
        ctx.ui.info(t("ci.attach_hint"))


class SambaModule(Step):
    id = "module-samba"
    title = "step.module_samba"

    def applies(self, ctx: Context) -> bool:
        return "samba" in ctx.kit.modules

    def conf(self, ctx: Context) -> str:
        return render("misc/samba-share.conf", {"share_name": ctx.cfg.machine_name,
                                                "workspace": ctx.cfg.workspace, "owner": ctx.cfg.owner})

    def check(self, ctx: Context) -> bool:
        r = ctx.host.run(["pdbedit", "-L"])
        return ctx.host.read_text("/etc/samba/smb.conf") == self.conf(ctx) and r.ok and \
            any(line.split(":")[0] == ctx.cfg.owner for line in r.stdout.splitlines())

    def apply(self, ctx: Context) -> None:
        h = ctx.host
        apt.install(h, ctx.ledger, ["samba"])
        install_file(h, ctx.ledger, "/etc/samba/smb.conf", self.conf(ctx), mode=0o644)
        r = h.run(["testparm", "-s"])
        if not r.ok:
            raise StepFailed(self.id, t("samba.invalid"))
        ctx.ui.info(t("samba.password", owner=ctx.cfg.owner))
        if h.runner.interactive(["smbpasswd", "-a", ctx.cfg.owner]) != 0:
            raise StepFailed(self.id, t("samba.password_failed"))
        sd.enable_now(h, ctx.ledger, "smbd.service")
        h.run(["systemctl", "restart", "smbd.service"])


class Proof(Step):
    id = "proof"
    title = "step.proof"

    def check(self, ctx: Context) -> bool:
        rec = prove.status(ctx.host)
        return bool(rec and rec.get("ok"))

    def apply(self, ctx: Context) -> None:
        mode = ctx.answers.get("prove", "reboot")
        results = prove.checks(ctx.host, ctx.cfg, ctx.kit, run_e2e=True)
        passed, total, failed = prove.summarize(results)
        for name, ok, detail in results:
            (ctx.ui.ok if ok else ctx.ui.fail)(f"{name} ({detail})")
        if failed:
            raise StepFailed(self.id, t("proof.precheck_failed", failed=", ".join(failed)), t("proof.hint"))
        if mode == "skip":
            ctx.ui.warn(t("proof.skipped"))
            return
        if mode == "no-reboot":
            prove.after_boot(ctx.host, ctx.cfg, ctx.kit, retries=1, wait_s=0)
            return
        ctx.ui.box([t("proof.reboot_1"), t("proof.reboot_2"), t("proof.reboot_3", name=ctx.cfg.machine_name)])
        if not ctx.ui.yes_no("reboot_now", t("proof.reboot_ask"), default=True):
            ctx.ui.warn(t("proof.reboot_later"))
            raise StepFailed(self.id, t("proof.not_done"), t("proof.later_hint"))
        prove.arm(ctx.host)
        ctx.journal.mark(self.id, "waiting-reboot")
        ctx.ui.info(t("proof.rebooting"))
        ctx.host.run(["systemctl", "reboot"])
        raise SystemExit(0)

    def verify(self, ctx: Context) -> Optional[str]:
        if ctx.answers.get("prove") == "skip":
            return None
        return None if self.check(ctx) else t("proof.not_done")
