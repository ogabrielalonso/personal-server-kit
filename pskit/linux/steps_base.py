"""Scenario B, foundation: account, packages, identity, storage, memory."""

from __future__ import annotations

import json
from typing import Optional

from .. import budget as B
from .. import manifest as mf
from ..common import code_installed, install_code, save_configs
from ..i18n import t
from ..paths import BRAIN_USER_LINUX, KIT_GROUP, SLICE_BRAIN, SLICE_CI, SLICE_PARENT, SYS
from ..render import render
from ..state import add_line, ensure_dir, install_file
from ..steps import Context, Step, StepFailed
from . import apt
from . import systemd as sd

BASE_PACKAGES = ["ca-certificates", "curl", "ufw", "openssh-server", "unattended-upgrades", "util-linux", "bzip2"]


class OwnerAccount(Step):
    id = "owner-account"
    title = "step.owner_account"

    def check(self, ctx: Context) -> bool:
        return ctx.host.user_exists(ctx.cfg.owner)

    def apply(self, ctx: Context) -> None:
        host, owner = ctx.host, ctx.cfg.owner
        if not ctx.ui.interactive:
            raise StepFailed(self.id, t("owner.must_exist_unattended", owner=owner))
        host.check(["useradd", "--create-home", "--shell", "/bin/bash", "--groups", "sudo", owner])
        ctx.ledger.add("user_created", name=owner, keep=True)
        ctx.ui.info(t("owner.set_password", owner=owner))
        for _ in range(3):
            if host.runner.interactive(["passwd", owner]) == 0:
                return
        raise StepFailed(self.id, t("owner.password_failed"))

    def verify(self, ctx: Context) -> Optional[str]:
        if not ctx.host.user_exists(ctx.cfg.owner):
            return t("owner.missing", owner=ctx.cfg.owner)
        return None


class Packages(Step):
    id = "packages"
    title = "step.packages"

    def check(self, ctx: Context) -> bool:
        return all(apt.installed(ctx.host, p) for p in BASE_PACKAGES)

    def apply(self, ctx: Context) -> None:
        added = apt.install(ctx.host, ctx.ledger, BASE_PACKAGES)
        if added:
            ctx.ui.info(t("packages.added", pkgs=", ".join(added)))


class Timezone(Step):
    id = "timezone"
    title = "step.timezone"

    def _wanted(self, ctx: Context) -> str:
        return ctx.facts.get("timezone", "")

    def check(self, ctx: Context) -> bool:
        want = self._wanted(ctx)
        if not want:
            return True
        r = ctx.host.run(["timedatectl", "show", "-p", "Timezone", "--value"])
        return r.ok and r.stdout.strip() == want

    def apply(self, ctx: Context) -> None:
        ctx.host.check(["timedatectl", "set-timezone", self._wanted(ctx)])


class Identity(Step):
    """Kit group, brain service user and group; the owner joins both."""

    id = "identity"
    title = "step.identity"

    def check(self, ctx: Context) -> bool:
        h, owner = ctx.host, ctx.cfg.owner
        return (h.group_exists(KIT_GROUP) and h.group_exists(BRAIN_USER_LINUX) and h.user_exists(BRAIN_USER_LINUX)
                and owner in h.group_members(KIT_GROUP) and owner in h.group_members(BRAIN_USER_LINUX)
                and BRAIN_USER_LINUX in h.group_members(KIT_GROUP))

    def apply(self, ctx: Context) -> None:
        h, led, owner = ctx.host, ctx.ledger, ctx.cfg.owner
        for g in (KIT_GROUP, BRAIN_USER_LINUX):
            if not h.group_exists(g):
                h.check(["groupadd", "--system", g])
                led.add("group_created", name=g)
        if not h.user_exists(BRAIN_USER_LINUX):
            h.check(["useradd", "--system", "--gid", BRAIN_USER_LINUX, "--home-dir", ctx.cfg.brain_home,
                     "--no-create-home", "--shell", "/usr/sbin/nologin", BRAIN_USER_LINUX])
            led.add("user_created", name=BRAIN_USER_LINUX, keep=False)
        for user, group in ((owner, KIT_GROUP), (owner, BRAIN_USER_LINUX), (BRAIN_USER_LINUX, KIT_GROUP)):
            if user not in h.group_members(group):
                h.check(["usermod", "--append", "--groups", group, user])
                led.add("group_member_added", user=user, group=group)
        ctx.facts["relogin_needed"] = True


class Storage(Step):
    """D1: directories on the existing disk (a one-command installer cannot
    repartition a running root disk). Disk use is watched instead."""

    id = "storage"
    title = "step.storage"

    def dirs(self, ctx: Context):
        c = ctx.cfg
        base = c.workspace.rsplit("/", 1)[0]
        owner = c.owner
        return [
            (base, 0o755, "root", "root", True),
            (c.workspace, 0o750, owner, owner, True),
            (c.brain_home, 0o2770, BRAIN_USER_LINUX, BRAIN_USER_LINUX, True),
            (f"{c.brain_home}/{c.session_inbox}", 0o2770, BRAIN_USER_LINUX, BRAIN_USER_LINUX, True),
            (SYS.etc, 0o755, "root", "root", False),
            (SYS.secrets, 0o700, "root", "root", False),
            (SYS.state, 0o755, "root", "root", False),
            (SYS.receipts, 0o755, "root", "root", False),
            (SYS.lockdown, 0o2770, "root", KIT_GROUP, False),
            (SYS.spool.rsplit("/", 1)[0], 0o755, "root", "root", False),
            (SYS.spool, 0o3770, "root", KIT_GROUP, False),
            ("/var/cache/pskit", 0o700, "root", "root", False),
        ]

    def check(self, ctx: Context) -> bool:
        h = ctx.host
        for path, mode, _, _, _ in self.dirs(ctx):
            p = h.p(path)
            if not p.is_dir() or (p.stat().st_mode & 0o7777) != mode:
                return False
        return True

    def apply(self, ctx: Context) -> None:
        for path, mode, owner, group, keep in self.dirs(ctx):
            ensure_dir(ctx.host, ctx.ledger, path, mode=mode, owner=owner, group=group, keep_on_uninstall=keep)


class Code(Step):
    id = "code"
    title = "step.code"

    def check(self, ctx: Context) -> bool:
        return code_installed(ctx)

    def apply(self, ctx: Context) -> None:
        install_code(ctx)


class Config(Step):
    id = "config"
    title = "step.config"

    def check(self, ctx: Context) -> bool:
        h = ctx.host
        return h.read_text(SYS.host_config) == ctx.cfg.to_json() and h.read_text(SYS.kit_config) == ctx.kit.to_json()

    def apply(self, ctx: Context) -> None:
        errors = ctx.cfg.validate()
        if errors:
            raise StepFailed(self.id, "; ".join(errors))
        save_configs(ctx)


class Swap(Step):
    id = "swap"
    title = "step.swap"
    SWAPFILE = "/swapfile.pskit"

    def check(self, ctx: Context) -> bool:
        return ctx.host.meminfo_kib().get("SwapTotal", 0) > 0

    def apply(self, ctx: Context) -> None:
        h = ctx.host
        ram = ctx.facts.get("ram_bytes") or h.meminfo_kib().get("MemTotal", 0) * 1024
        _, _, free = h.disk_usage("/")
        size = B.swap_size_bytes(ram, free)
        if size <= 0:
            ctx.ui.warn(t("swap.no_space"))
            return
        fs = h.run(["stat", "-f", "-c", "%T", "/"]).stdout.strip()
        if fs in ("btrfs", "zfs", "tmpfs", "overlayfs"):
            # A swap file there needs special handling; better none than a
            # half-made one.
            ctx.ui.warn(t("swap.unsupported_fs", fs=fs))
            return
        gib = size // B.GIB
        try:
            r = h.run(["fallocate", "-l", f"{gib}G", self.SWAPFILE])
            if not r.ok:
                h.check(["dd", "if=/dev/zero", f"of={self.SWAPFILE}", "bs=1M", f"count={gib * 1024}"],
                        timeout=3600)
            h.p(self.SWAPFILE).chmod(0o600)
            h.check(["mkswap", self.SWAPFILE])
            h.check(["swapon", self.SWAPFILE])
        except Exception:
            h.run(["swapoff", self.SWAPFILE])
            h.remove(self.SWAPFILE)
            raise
        ctx.ledger.add("swapfile_created", path=self.SWAPFILE)
        add_line(h, ctx.ledger, "/etc/fstab", f"{self.SWAPFILE} none swap sw 0 0")
        ctx.ui.info(t("swap.created", gib=gib))

    def verify(self, ctx: Context) -> Optional[str]:
        return None


def current_budget(ctx: Context) -> B.Budget:
    ram = ctx.facts.get("ram_bytes") or ctx.host.meminfo_kib().get("MemTotal", 0) * 1024
    peak = None
    raw = ctx.host.read_text(SYS.brain_manifest)
    if raw:
        try:
            peak = mf.load(raw).peak_bytes or None
        except mf.ManifestError:
            peak = None
    return B.compute(ram, brain_peak_bytes=peak, with_ci="ci" in ctx.kit.modules)


def owner_uid(ctx: Context) -> int:
    if not ctx.host.real:
        for line in (ctx.host.read_text("/etc/passwd", "") or "").splitlines():
            parts = line.split(":")
            if parts[0] == ctx.cfg.owner:
                return int(parts[2])
        return 1000
    import pwd
    return pwd.getpwnam(ctx.cfg.owner).pw_uid


class MemoryBudget(Step):
    """Slices with protection and ceilings; the sum stays below RAM."""

    id = "memory-budget"
    title = "step.memory_budget"

    def files(self, ctx: Context):
        b = current_budget(ctx)
        v = {k: B.to_systemd(getattr(b, k)) for k in
             ("parent_low", "parent_max", "brain_low", "brain_high", "brain_max", "ci_high", "ci_max",
              "user_high", "user_max", "system_low", "swap_max")}
        uid = owner_uid(ctx)
        out = {
            f"{sd.UNIT_DIR}/{SLICE_PARENT}": render(f"systemd/{SLICE_PARENT}", v),
            f"{sd.UNIT_DIR}/{SLICE_BRAIN}": render(f"systemd/{SLICE_BRAIN}", v),
            f"{sd.UNIT_DIR}/user-{uid}.slice.d/50-pskit.conf": render("systemd/user-slice-dropin.conf", v),
            f"{sd.UNIT_DIR}/system.slice.d/50-pskit.conf": render("systemd/system-slice-dropin.conf", v),
        }
        if "ci" in ctx.kit.modules:
            out[f"{sd.UNIT_DIR}/{SLICE_CI}"] = render(f"systemd/{SLICE_CI}", v)
        return b, out

    def check(self, ctx: Context) -> bool:
        _, files = self.files(ctx)
        return all(ctx.host.read_text(p) == c for p, c in files.items())

    def apply(self, ctx: Context) -> None:
        b, files = self.files(ctx)
        for path, content in files.items():
            install_file(ctx.host, ctx.ledger, path, content, mode=0o644)
        sd.daemon_reload(ctx.host)
        ctx.cfg.memory_budget = {k: round(v, 2) for k, v in b.as_gib().items()}
        save_configs(ctx)
        ctx.ui.info(t("budget.summary", brain=B.human(b.brain_max), user=B.human(b.user_max),
                      margin=B.human(b.margin_bytes)))

    def verify(self, ctx: Context) -> Optional[str]:
        b, _ = self.files(ctx)
        got = sd.show(ctx.host, SLICE_BRAIN, "MemoryMax")
        if ctx.host.real and got and got.isdigit() and int(got) != b.brain_max // B.MIB * B.MIB:
            return t("budget.not_applied", got=got)
        return None if self.check(ctx) else t("engine.not_in_state")


class Tmpfiles(Step):
    id = "tmpfiles"
    title = "step.tmpfiles"

    def files(self, ctx: Context):
        extra = "\n".join(f"e {p} - - - 2d" for p in ctx.answers.get("agent_tmp_patterns", []))
        return {
            "/etc/tmpfiles.d/pskit.conf": render("misc/tmpfiles-pskit.conf", {
                "kit_group": KIT_GROUP, "workspace": ctx.cfg.workspace, "owner": ctx.cfg.owner,
                "owner_group": ctx.cfg.owner, "extra_block": extra}),
            "/etc/tmpfiles.d/tmp.conf": render("misc/tmpfiles-tmp.conf", {"tmp_age": "10d"}),
        }

    def check(self, ctx: Context) -> bool:
        return all(ctx.host.read_text(p) == c for p, c in self.files(ctx).items()) and \
            ctx.host.exists(SYS.heavy_lock)

    def apply(self, ctx: Context) -> None:
        for path, content in self.files(ctx).items():
            install_file(ctx.host, ctx.ledger, path, content, mode=0o644)
        ctx.host.check(["systemd-tmpfiles", "--create", "/etc/tmpfiles.d/pskit.conf"])


class Updates(Step):
    id = "updates"
    title = "step.updates"
    FILE = "/etc/apt/apt.conf.d/52pskit-unattended"

    def check(self, ctx: Context) -> bool:
        return ctx.host.read_text(self.FILE) == render("misc/unattended-upgrades.conf", {}) and \
            sd.is_enabled(ctx.host, "apt-daily-upgrade.timer")

    def apply(self, ctx: Context) -> None:
        install_file(ctx.host, ctx.ledger, self.FILE, render("misc/unattended-upgrades.conf", {}), mode=0o644)
        sd.enable(ctx.host, ctx.ledger, "apt-daily-upgrade.timer")
        sd.enable(ctx.host, ctx.ledger, "apt-daily.timer")

    def verify(self, ctx: Context) -> Optional[str]:
        r = ctx.host.run(["apt-config", "dump"])
        if r.ok and 'APT::Periodic::Unattended-Upgrade "1";' not in r.stdout:
            return t("updates.not_active")
        return None if self.check(ctx) else t("engine.not_in_state")


def json_dump(obj) -> str:
    return json.dumps(obj, indent=2, sort_keys=True) + "\n"
