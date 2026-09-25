"""`pskit install`: the guided installer.

Order of questions: language first (English default), then what this
machine is. Server roles need root; the installer re-runs itself with sudo
carrying the answers given so far, so nothing is asked twice.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
from typing import Dict, List, Optional

from . import VERSION, diagnostics, preflight
from .checks import write_receipt
from .config import HostConfig, KitConfig, load_host_config, load_kit_config, slug, valid_machine_name, valid_user
from .i18n import set_language, t
from .paths import BRAIN_USER_LINUX, heavy_lock
from .state import Journal, Ledger
from .steps import Context, Engine, Step, StepFailed
from .system import Host
from .ui import UI, NeedAnswer, ask_language

PRESERVED_ENV = ",".join([
    "PYTHONPATH", "TS_AUTHKEY", "PSKIT_TELEGRAM_TOKEN", "PSKIT_HEARTBEAT_URL",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "NO_COLOR",
])


def linux_steps() -> List[Step]:
    from .linux import steps_base as b
    from .linux import steps_net as n
    from .linux import steps_ops as o
    return [b.OwnerAccount(), b.Packages(), b.Timezone(), b.Identity(), b.Storage(), b.Code(), b.Config(),
            b.Swap(), b.MemoryBudget(), b.Tmpfiles(), b.Updates(), n.Tailscale(), o.Alerts(), o.Heartbeat(),
            o.Backup(), o.Runtime(), o.CiModule(), o.SambaModule(), n.Pairing(), n.Lockdown(), o.BrainSlot(),
            o.Proof()]


def reexec_as_root(answers: Dict, extra: List[str]) -> None:
    fd, path = tempfile.mkstemp(prefix="pskit-answers-", suffix=".json")
    with os.fdopen(fd, "w") as fh:
        json.dump(answers, fh)
    os.chmod(path, 0o600)
    args = ["sudo", f"--preserve-env={PRESERVED_ENV}", sys.executable, "-B", "-m", "pskit", "install",
            "--answers", path, "--answers-cleanup"] + extra
    os.execvp("sudo", args)  # noqa: S606 - replaces this process with the same command under sudo


def current_timezone(host: Host) -> str:
    r = host.run(["timedatectl", "show", "-p", "Timezone", "--value"])
    tz = r.stdout.strip()
    if "zoneinfo/" in tz:
        tz = tz.split("zoneinfo/", 1)[1]
    return tz or "UTC"


def valid_tz(host: Host, tz: str) -> Optional[str]:
    base = "/usr/share/zoneinfo"
    return None if tz and ".." not in tz and host.p(f"{base}/{tz}").is_file() else t("install.tz_bad")


def ask_server_config(ui: UI, host: Host, scenario: str) -> tuple:
    default_name = slug(socket.gethostname().split(".")[0], "server")
    if not valid_machine_name(default_name):
        default_name = "server"
    name = ui.text("machine_name", t("install.machine_name"), default=default_name,
                   validate=lambda v: None if valid_machine_name(v) else t("install.machine_name_bad"))
    default_owner = os.environ.get("SUDO_USER") or ""
    if default_owner == "root":
        default_owner = ""
    owner = ui.text("owner", t("install.owner"), default=default_owner,
                    validate=lambda v: None if valid_user(v) and v != "root" else t("install.owner_bad"))
    tz = ui.text("timezone", t("install.timezone"), default=current_timezone(host),
                 validate=lambda v: valid_tz(host, v))
    base = f"/srv/{name}"
    cfg = HostConfig(
        language=ui.answers.get("language", "en"), scenario=scenario, machine_name=name, owner=owner,
        brain_home=f"{base}/brain", workspace=f"{base}/workspace",
        brain_port=int(ui.answers.get("brain_port", 8799)),
        brain_user=BRAIN_USER_LINUX,
        heavy_lock=heavy_lock(),
    )
    kit = KitConfig(test_mode=bool(ui.answers.get("test_mode", False)))
    modules = list(ui.answers.get("modules", []))
    if "modules" not in ui.answers and ui.interactive:
        if ui.yes_no("advanced", t("install.advanced"), default=False):
            if ui.yes_no("module_ci", t("install.module_ci"), default=False):
                modules.append("ci")
            if ui.yes_no("module_samba", t("install.module_samba"), default=False):
                modules.append("samba")
    kit.modules = modules
    return cfg, kit, tz


def run_server(ui: UI, host: Host, scenario: str, answers: Dict) -> int:
    existing = None
    try:
        existing = load_host_config(host)
    except Exception:
        ui.warn(t("install.config_unreadable"))
    if existing:
        ui.info(t("install.existing", name=existing.machine_name, version=VERSION))
        choice = ui.choice("existing", t("install.existing_ask"), [
            ("resume", t("install.existing_resume")), ("exit", t("install.existing_exit"))], default="resume")
        if choice == "exit":
            return 0
        cfg, kit = existing, load_kit_config(host)
        cfg.language = answers.get("language", cfg.language)
        tz = answers.get("timezone", "")
    else:
        cfg, kit, tz = ask_server_config(ui, host, scenario)
    rep = preflight.linux_server(host, need_network=not kit.test_mode or bool(answers.get("preflight_network")))
    ui.title(t("install.preflight"))
    for k, v in rep.facts.items():
        if k in ("os", "arch"):
            ui.info(f"{k}: {v}")
    for w in rep.warnings:
        ui.warn(w)
    if rep.blockers:
        for b in rep.blockers:
            ui.fail(b)
        ui.say(t("install.blocked"))
        return 2
    ui.ok(t("install.preflight_ok"))
    ui.title(t("install.plan_title"))
    for line in (t("install.plan_1", name=cfg.machine_name), t("install.plan_2", owner=cfg.owner),
                 t("install.plan_3", brain=cfg.brain_home), t("install.plan_4"), t("install.plan_5")):
        ui.info(line)
    if not ui.yes_no("confirm", t("install.continue"), default=True):
        return 1
    if kit.test_mode and answers.get("tailscale") == "skip":
        # Tests run without a tailnet: loopback stands in for it.
        kit.tailnet_ip, kit.tailnet_name = "127.0.0.1", "localhost"
        kit.tailscale_expected = False
    journal, ledger = Journal(host), Ledger(host)
    ctx = Context(host=host, ui=ui, journal=journal, ledger=ledger, cfg=cfg, kit=kit, answers=answers,
                  facts=dict(rep.facts, timezone=tz))
    journal.start_run("install", version=VERSION, scenario=scenario)
    if not host.exists("/var/lib/pskit/receipts/install.json"):
        host.mkdir("/var/lib/pskit/receipts", mode=0o755)
        write_receipt(host, "install", {"version": VERSION, "scenario": scenario})
    steps = linux_steps()
    try:
        Engine(ctx, steps).run()
    except StepFailed as exc:
        journal.finish_run(f"failed:{exc.step_id}")
        ui.say()
        ui.fail(t("install.failed", step=exc.step_id))
        ui.say("      " + exc.message)
        if exc.hint:
            ui.say("      " + exc.hint)
        path = diagnostics.write(host, cfg, kit)
        ui.box([t("install.resume_hint"), t("install.diag_hint"), str(path)])
        return 1
    except KeyboardInterrupt:
        journal.finish_run("interrupted")
        ui.say()
        ui.warn(t("install.interrupted"))
        return 130
    journal.finish_run("done")
    path = diagnostics.write(host, cfg, kit)
    ui.title(t("install.done_title"))
    ui.info(t("install.done_1", name=cfg.machine_name))
    if ctx.facts.get("relogin_needed"):
        ui.info(t("install.relogin"))
    ui.info(t("install.done_diag", path=path))
    return 0


def run_local(ui: UI) -> int:
    ui.box([t("local.1"), t("local.2"), t("local.3"), "https://github.com/ogabrielalonso/second-brain-kit"])
    return 0


def main(answers_path: Optional[str], cleanup: bool = False, non_interactive: bool = False) -> int:
    from .ui import load_answers
    answers = load_answers(answers_path)
    if cleanup and answers_path:
        try:
            os.unlink(answers_path)
        except OSError:
            pass
    ui = UI(answers, interactive=False if non_interactive else None)
    host = Host()
    try:
        lang = ask_language(ui)
        answers["language"] = lang
        set_language(lang)
        ui.title(t("install.welcome", version=VERSION))
        if host.platform == "linux":
            options = [("server", t("role.linux_server")), ("device", t("role.device_linux"))]
            default = "server"
        elif host.platform == "macos":
            # A server is Linux only; a Mac is the owner's laptop, or runs the brain locally.
            options = [("device", t("role.device")), ("local", t("role.local"))]
            default = "device"
        else:
            ui.fail(t("install.platform_unsupported", platform=host.platform))
            return 2
        role = ui.choice("role", t("install.role"), options, default)
        answers["role"] = role
        if role == "local":
            return run_local(ui)
        if role == "device":
            from .device import pair
            try:
                pair.run(ui, host, answers)
            except pair.PairError as exc:
                ui.fail(str(exc))
                return 1
            return 0
        if os.geteuid() != 0:
            ui.info(t("install.need_sudo"))
            reexec_as_root(answers, ["--non-interactive"] if non_interactive else [])
        return run_server(ui, host, "linux-server", answers)
    except NeedAnswer as exc:
        ui.fail(t("install.need_answer", key=str(exc)))
        return 2
    except (EOFError, KeyboardInterrupt):
        ui.say()
        ui.warn(t("install.interrupted"))
        return 130
