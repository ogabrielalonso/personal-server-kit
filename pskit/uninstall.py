"""`pskit uninstall`: undo what the kit did, from its ledger, newest first.

Kept unless explicitly asked otherwise:
  - the owner's data (workspace, brain home): `--purge-data` plus typing
    the machine name removes them;
  - the lockdown (firewall and key-only SSH): removing it reopens the
    machine, so the owner has to choose it;
  - installed packages (Tailscale, ufw, ...): removing Tailscale would cut
    the only way in. They are listed, never removed.
Files the kit created and the owner changed afterwards are kept aside,
not deleted.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from typing import Any, Dict, List

from . import lockdown as L
from .config import load_host_config
from .i18n import t
from .paths import SLICE_BRAIN, SLICE_CI, SLICE_PARENT, SYS
from .state import Ledger, last_written_sha, remove_line
from .system import Host, sha256_file
from .ui import UI

PROTECTED_WHEN_LOCKED = {L.SSHD_DROPIN}


def plan(host: Host) -> List[Dict]:
    return list(Ledger(host).reversed())


def run(host: Host, ui: UI, purge_data: bool = False, keep_lockdown: bool = True) -> int:
    cfg = load_host_config(host)
    if not cfg:
        ui.fail(t("uninstall.not_installed"))
        return 1
    ui.title(t("uninstall.title", name=cfg.machine_name))
    typed = ui.text("uninstall_confirm", t("uninstall.type_name", name=cfg.machine_name), validate=lambda v: None)
    if typed.strip() != cfg.machine_name:
        ui.warn(t("uninstall.cancelled"))
        return 1
    if host.exists(f"{SYS.secrets}/restic.pass"):
        ui.box([t("uninstall.backup_pw_1"), "",
                "    " + (host.read_text(f"{SYS.secrets}/restic.pass", "") or "").strip(), "",
                t("uninstall.backup_pw_2")])
        ui.pause(t("ui.press_enter"))
    if L.read_pending(host):
        # Never confirmed: undo it now, before its safety timer is removed
        # along with the other units.
        L.revert_if_due(host, cfg, now=float("inf"))
        ui.info(t("uninstall.pending_reverted"))
    locked = L.is_applied(host)
    if locked and not keep_lockdown:
        L.restore_snapshot(host)
        ui.info(t("uninstall.lockdown_restored"))
    aside = f"/root/pskit-uninstalled-{time.strftime('%Y%m%d-%H%M%S')}"
    report: Dict[str, Any] = {"removed": 0, "restored": 0, "kept_aside": 0, "kept_changed": [], "packages": []}
    latest = last_written_sha(Ledger(host))
    _stop_units(host)
    for e in plan(host):
        kind = e.get("kind")
        try:
            if kind == "file_created":
                path = e["path"]
                if locked and keep_lockdown and path in PROTECTED_WHEN_LOCKED:
                    continue
                p = host.p(path)
                if not p.exists():
                    continue
                if p.is_file() and sha256_file(p) != latest.get(path, e.get("sha256")):
                    host.mkdir(aside, mode=0o700)
                    shutil.move(str(p), str(host.p(aside)) + "/" + path.strip("/").replace("/", "__"))
                    report["kept_aside"] += 1
                else:
                    host.remove(path)
                    report["removed"] += 1
            elif kind == "file_replaced":
                path = e["path"]
                if locked and keep_lockdown and path in PROTECTED_WHEN_LOCKED:
                    continue
                data = host.read_bytes(e["backup"])
                if data is None:
                    continue
                p = host.p(path)
                if p.is_file() and sha256_file(p) != latest.get(path, e.get("sha256")):
                    # Changed by the owner after the kit wrote it: keep their
                    # version, put the pre-install copy aside.
                    host.mkdir(aside, mode=0o700)
                    host.write_atomic(f"{aside}/{path.strip('/').replace('/', '__')}.before-pskit", data, mode=0o600)
                    report["kept_changed"].append(path)
                    continue
                mode = p.stat().st_mode & 0o777 if p.exists() else 0o644
                host.write_atomic(path, data, mode=mode)
                report["restored"] += 1
            elif kind == "line_added":
                remove_line(host, e["path"], e["line"])
            elif kind == "authorized_keys":
                from .server.keys import remove_device
                remove_device(host, e["path"], e["device"])
            elif kind == "unit_enabled":
                host.run(["systemctl", "disable", "--now", e["unit"]])
            elif kind == "swapfile_created":
                host.run(["swapoff", e["path"]])
                host.remove(e["path"])
            elif kind == "group_member_added":
                host.run(["gpasswd", "-d", e["user"], e["group"]])
            elif kind == "user_created" and not e.get("keep"):
                host.run(["userdel", e["name"]])
            elif kind == "group_created":
                host.run(["groupdel", e["name"]])
            elif kind == "package_installed":
                report["packages"].append(e["name"])
            elif kind == "dir_created":
                if e.get("keep") and not purge_data:
                    continue
                p = host.p(e["path"])
                if p.is_dir() and (purge_data or not any(p.iterdir())):
                    host.remove(e["path"])
        except (OSError, KeyError) as exc:
            ui.warn(f"{kind}: {exc}")
    host.run(["systemctl", "daemon-reload"])
    if purge_data:
        for path in (cfg.brain_home, cfg.workspace):
            if path and host.exists(path):
                host.remove(path)
    for path in (SYS.opt, SYS.shim, "/var/spool/pskit", "/var/cache/pskit", SYS.etc):
        if host.exists(path):
            host.remove(path)
    state_copy = f"{aside}/state"
    if host.exists(SYS.state):
        host.mkdir(aside, mode=0o700)
        shutil.copytree(host.p(SYS.state), host.p(state_copy), dirs_exist_ok=True)
        host.remove(SYS.state)
    ui.ok(t("uninstall.done", removed=report["removed"], restored=report["restored"]))
    if report["kept_aside"]:
        ui.info(t("uninstall.aside", path=aside))
    if host.exists(state_copy):
        ui.info(t("uninstall.records", path=state_copy))
    for path in report["kept_changed"]:
        ui.warn(t("uninstall.kept_changed", path=path, aside=aside))
    if report["packages"]:
        ui.info(t("uninstall.packages", pkgs=", ".join(sorted(set(report["packages"])))))
    if locked and keep_lockdown:
        ui.info(t("uninstall.lockdown_kept"))
    if not purge_data:
        ui.info(t("uninstall.data_kept", brain=cfg.brain_home, workspace=cfg.workspace))
    print(json.dumps(report))
    return 0


def _stop_units(host: Host) -> None:
    r = host.run(["systemctl", "list-units", "--all", "--plain", "--no-legend", "pskit-*"])
    names = [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]
    for n in names:
        if n.endswith((".timer", ".path", ".service")):
            host.run(["systemctl", "disable", "--now", n])
    for n in os.listdir(host.p("/etc/systemd/system")) if host.p("/etc/systemd/system").is_dir() else []:
        if n.startswith("pskit-brain-"):
            host.run(["systemctl", "disable", "--now", n])
    # Slices stay active once started; stop them after their services.
    for s in (SLICE_BRAIN, SLICE_CI, SLICE_PARENT):
        host.run(["systemctl", "stop", s])
    host.run(["systemctl", "reset-failed", "pskit-*"])


def device(ui: UI, server: str = "") -> int:
    from .device import agents
    from .device.local import Paths, list_servers, remove_include
    from .device.pair import is_mac
    paths = Paths()
    servers = [server] if server else list_servers(paths)
    for s in servers:
        agents.remove(paths, s, is_mac())
        d = paths.server_dir(s)
        if d.exists():
            shutil.rmtree(d)
        f = paths.server_file(s)
        if f.exists():
            f.unlink()
        ui.ok(t("uninstall.device_removed", server=s))
    if not list_servers(paths):
        remove_include(paths)
        if paths.share.exists():
            shutil.rmtree(paths.share)
        if paths.bin.exists() or paths.bin.is_symlink():
            paths.bin.unlink()
    return 0
