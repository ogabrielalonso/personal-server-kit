"""Command line entry point: `pskit <command>` (or `python3 -m pskit`)."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import List, Optional

from . import VERSION
from .config import load_host_config, load_kit_config
from .i18n import set_language, t
from .paths import SYS
from .system import Host


def _lang_from_env(host: Host) -> None:
    lang = os.environ.get("PSKIT_LANG", "")
    if not lang:
        try:
            cfg = load_host_config(host)
            lang = cfg.language if cfg else ""
        except Exception:
            lang = ""
    if not lang:
        try:
            from .device.local import Paths, list_servers, load_server
            p = Paths()
            servers = list_servers(p)
            lang = (load_server(p, servers[0]) or {}).get("language", "") if servers else ""
        except Exception:
            lang = ""
    set_language(lang or "en")


def _need_root() -> None:
    if os.geteuid() != 0:
        print(t("cli.need_root"), file=sys.stderr)
        raise SystemExit(1)


def _server(host: Host):
    cfg = load_host_config(host)
    if not cfg:
        print(t("cli.not_installed"), file=sys.stderr)
        raise SystemExit(1)
    return cfg, load_kit_config(host)


def build_parser() -> argparse.ArgumentParser:
    # No abbreviations: commands written into units must match exactly.
    p = argparse.ArgumentParser(prog="pskit", description="personal-server-kit", allow_abbrev=False)
    sub = p.add_subparsers(dest="cmd")
    _add = sub.add_parser

    def add_parser(name, **kw):
        return _add(name, allow_abbrev=False, **kw)

    sub.add_parser = add_parser  # type: ignore[method-assign]
    s = sub.add_parser("install", help="guided installer")
    s.add_argument("--answers")
    s.add_argument("--answers-cleanup", action="store_true", help=argparse.SUPPRESS)
    s.add_argument("--non-interactive", action="store_true")
    s = sub.add_parser("doctor", help="plain-language checks (read-only)")
    s.add_argument("--audit", action="store_true", help="generic checks for any machine")
    s.add_argument("--json", action="store_true")
    s.add_argument("--bundle", action="store_true", help="write the diagnostic file")
    sub.add_parser("status", help="short summary")
    sub.add_parser("pair", help="connect this laptop to a server")
    s = sub.add_parser("pair-serve", help="server: accept one laptop (shows an address and a code)")
    s.add_argument("--code", default="")
    s.add_argument("--bind", default="")
    s.add_argument("--ttl", type=int, default=900)
    s = sub.add_parser("forget-device", help="server: remove a paired laptop's access")
    s.add_argument("device")
    s = sub.add_parser("confirm-lockdown")
    s.add_argument("--wait", type=int, default=0)
    s = sub.add_parser("lockdown-revert")
    s.add_argument("--timer", action="store_true", help=argparse.SUPPRESS)
    s = sub.add_parser("prove", help="final proof: reboot and check")
    s.add_argument("--no-reboot", action="store_true")
    s.add_argument("--status", action="store_true")
    s.add_argument("--json", action="store_true", help="with --status: the raw receipt")
    s = sub.add_parser("prove-boot")
    s.add_argument("--if-pending", action="store_true")
    s = sub.add_parser("uninstall")
    s.add_argument("--purge-data", action="store_true")
    s.add_argument("--remove-lockdown", action="store_true")
    s.add_argument("--device", action="store_true", help="remove the laptop side")
    s.add_argument("--confirm-name", default="", help="skip the typed confirmation (automation)")
    s.add_argument("--server", default="")
    s = sub.add_parser("notify", help="send an alert to the owner")
    s.add_argument("severity", choices=["critical", "warn", "info"])
    s.add_argument("title")
    s.add_argument("body", nargs="?", default="")
    s.add_argument("--key", default="")
    s.add_argument("--cooldown-hours", type=float, default=0)
    sub.add_parser("notify-deliver")
    sub.add_parser("health")
    sub.add_parser("digest")
    sub.add_parser("heartbeat")
    s = sub.add_parser("reaper")
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("cache-maint")
    s.add_argument("--dry-run", action="store_true")
    sub.add_parser("pressure-guard")
    s = sub.add_parser("backup")
    s.add_argument("action", choices=["run", "restore-test", "snapshots"])
    s.add_argument("--if-stale-hours", type=float)
    s = sub.add_parser("brain")
    s.add_argument("action", choices=["apply", "validate", "status"])
    s.add_argument("manifest", nargs="?")
    s = sub.add_parser("bridge-receive")
    s.add_argument("--device", required=True)
    sub.add_parser("bridge-local")
    s = sub.add_parser("bridge-push")
    s.add_argument("--server", required=True)
    s = sub.add_parser("ci")
    s.add_argument("action", choices=["attach"])
    s.add_argument("unit")
    s = sub.add_parser("with-lock", help="run a command holding a lock file (used for heavy jobs)")
    s.add_argument("lockfile")
    s.add_argument("command", nargs=argparse.REMAINDER)
    sub.add_parser("version")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    host = Host()
    if args.cmd in (None, "install"):
        from . import install
        a = args if args.cmd else argparse.Namespace(answers=None, answers_cleanup=False, non_interactive=False)
        return install.main(a.answers, cleanup=a.answers_cleanup, non_interactive=a.non_interactive)
    _lang_from_env(host)
    cmd = args.cmd

    if cmd == "version":
        print(VERSION)
        return 0
    if cmd == "with-lock":
        return _with_lock(args.lockfile, args.command)
    if cmd == "doctor":
        from . import doctor
        if args.bundle:
            from . import diagnostics
            cfg = load_host_config(host)
            path = diagnostics.write(host, cfg, load_kit_config(host) if cfg else None)
            print(t("cli.bundle_written", path=path))
            return 0
        return doctor.run(host, audit=args.audit, as_json=args.json)
    if cmd == "status":
        return _status(host)
    if cmd == "pair":
        from .device import pair
        from .ui import UI
        try:
            pair.run(UI(), host, {})
        except pair.PairError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        return 0
    if cmd == "pair-serve":
        _need_root()
        from .server import pairing
        from .state import Ledger
        cfg, kit = _server(host)
        bind = args.bind or kit.tailnet_ip
        if bind.startswith("127.") and not kit.test_mode:
            print("loopback pairing is only for tests", file=sys.stderr)
            return 2
        code = args.code or pairing.new_code()
        print(t("pairing.box_address", address=bind), flush=True)
        print(t("pairing.box_code", code=code), flush=True)
        print(json.dumps({"bind": bind, "port": kit.pair_port, "code": code}), flush=True)
        session = pairing.PairingSession(host, Ledger(host), cfg, kit, code, ttl_s=args.ttl)
        pairing.serve(session, bind, kit.pair_port)
        print(json.dumps({"paired": bool(session.result), "failed": session.failed}))
        return 0 if session.result else 1
    if cmd == "forget-device":
        _need_root()
        return _forget_device(host, args.device)
    if cmd == "confirm-lockdown":
        return _confirm_lockdown(host, args.wait)
    if cmd == "lockdown-revert":
        _need_root()
        from . import lockdown
        cfg = load_host_config(host)
        print(lockdown.revert_if_due(host, cfg, from_timer=args.timer))
        return 0
    if cmd == "prove":
        from . import prove
        if args.status:
            rec = prove.status(host)
            if args.json:
                print(json.dumps(rec or {}, indent=1))
            else:
                print(prove.status_text(rec) if rec else t("doctor.proof_never"))
            return 0 if rec and rec.get("ok") else 1
        _need_root()
        cfg, kit = _server(host)
        results = prove.checks(host, cfg, kit)
        passed, total, failed = prove.summarize(results)
        for name, ok, detail in results:
            print(f"  [{'ok' if ok else 'XX'}] {name} ({detail})")
        if failed:
            return 1
        if args.no_reboot:
            rec = prove.after_boot(host, cfg, kit, retries=1, wait_s=0)
            return 0 if rec["ok"] else 1
        prove.arm(host)
        print(t("proof.rebooting"))
        host.run(["systemctl", "reboot"])
        return 0
    if cmd == "prove-boot":
        _need_root()
        from . import prove
        if args.if_pending and not host.exists(prove.PENDING):
            return 0
        cfg, kit = _server(host)
        rec = prove.after_boot(host, cfg, kit)
        if rec["ok"]:
            from .state import Journal
            Journal(host).mark("proof", "done")
        return 0 if rec["ok"] else 1
    if cmd == "uninstall":
        from . import uninstall
        from .ui import UI
        if args.device:
            return uninstall.device(UI(), args.server)
        _need_root()
        answers = {"uninstall_confirm": args.confirm_name} if args.confirm_name else {}
        ui = UI(answers, interactive=False if args.confirm_name else None)
        return uninstall.run(host, ui, purge_data=args.purge_data, keep_lockdown=not args.remove_lockdown)
    if cmd == "notify":
        from . import notify
        try:
            notify.enqueue(host, args.severity, args.title, args.body, key=args.key,
                           cooldown_hours=args.cooldown_hours)
        except PermissionError:
            print(t("cli.notify_denied"), file=sys.stderr)
            return 1
        return 0
    if cmd == "notify-deliver":
        _need_root()
        from . import notify
        cfg, _ = _server(host)
        print(json.dumps(notify.deliver(host, cfg, sweep_wait=True)))
        return 0
    if cmd == "health":
        _need_root()
        from .runtime import health
        cfg, kit = _server(host)
        from .paths import heavy_lock, run_dir
        from .paths import KIT_GROUP
        # The kit group may create the lock if it is missing (/run is empty
        # at boot, and brain jobs may start before this check).
        host.mkdir(run_dir(), mode=0o775, group=KIT_GROUP)
        lock = host.p(heavy_lock())
        if not lock.exists():
            lock.touch(mode=0o664)
            host.chown(heavy_lock(), None, KIT_GROUP)
        health.run(host, cfg, kit)
        return 0
    if cmd == "digest":
        _need_root()
        from .runtime import digest
        cfg, kit = _server(host)
        digest.run(host, cfg, kit)
        return 0
    if cmd == "heartbeat":
        from .runtime import heartbeat
        return heartbeat.run(host)
    if cmd == "reaper":
        _need_root()
        from .runtime import reaper
        cfg, kit = _server(host)
        if args.dry_run:
            import pwd
            print(json.dumps(reaper.plan(host, pwd.getpwnam(cfg.owner).pw_uid, kit)))
            return 0
        reaper.run(host, cfg, kit)
        return 0
    if cmd == "cache-maint":
        from .runtime import cachemaint
        report = cachemaint.run(apply=not args.dry_run)
        return 0 if report.get("status") != "failed_closed" else 1
    if cmd == "pressure-guard":
        _need_root()
        from .runtime import pressure
        cfg, _ = _server(host)
        pressure.run_forever(host, cfg)
        return 0
    if cmd == "backup":
        _need_root()
        from .runtime import backup
        cfg, kit = _server(host)
        if args.action == "run":
            try:
                rec = backup.run_backup(host, cfg, kit, if_stale_hours=args.if_stale_hours)
            except backup.BackupError as exc:
                print(f"backup failed: {exc}", file=sys.stderr)
                return 1
            skipped = {"not configured": "backup: not configured", "recent": "backup: recent success, retry not needed"}
            print(skipped.get(rec.get("skipped", ""), "") or
                  json.dumps({"backup": "ok", "snapshot": rec.get("snapshot"), "partial": rec.get("partial")}))
            return 0
        if args.action == "restore-test":
            rec = backup.restore_test(host, cfg, kit)
            print(json.dumps({"restore_test": rec.get("ok"), "checked": rec.get("checked"),
                              "skipped": rec.get("skipped")}))
            return 0 if rec.get("ok") or rec.get("skipped") else 1
        r = backup.restic(host, ["snapshots", "--compact"])
        print(r.stdout or r.stderr)
        return r.returncode
    if cmd == "brain":
        return _brain(host, args)
    if cmd == "bridge-receive":
        return _bridge_receive(host, args.device)
    if cmd == "bridge-local":
        return _bridge_local(host)
    if cmd == "bridge-push":
        from .device import bridge_push
        from .device.local import Paths
        pushed = bridge_push.push(Paths(), args.server)
        print(json.dumps({k: v for k, v in pushed.items() if k != "started"}))
        return 0 if pushed.get("ok") else 1
    if cmd == "ci":
        _need_root()
        return _ci_attach(host, args.unit)
    return 2


def _status(host: Host) -> int:
    from .checks import receipt
    cfg = load_host_config(host)
    if not cfg:
        from . import doctor
        return doctor.run(host)
    h = receipt(host, "health") or {}
    print(t("status.header", name=cfg.machine_name, version=VERSION))
    probs = h.get("problems", [])
    print(t("status.problems", n=len(probs)) if probs else t("digest.all_ok"))
    for p in probs:
        print(f"  - {p['text']}")
    b = receipt(host, "backup-success")
    if b:
        print(t("digest.backup_last", hours=int((time.time() - float(b.get("finished", 0))) / 3600)))
    pr = receipt(host, "proof")
    if pr:
        print(t("doctor.proof_ok", passed=pr.get("passed"), total=pr.get("total")) if pr.get("ok")
              else t("doctor.proof_failed", failed=", ".join(pr.get("failed", []))))
    return 0


def _confirm_lockdown(host: Host, wait: int) -> int:
    from . import lockdown
    kit = load_kit_config(host)
    deadline = time.time() + max(0, wait)
    while True:
        try:
            result = lockdown.confirm(host, os.environ.get("SSH_CONNECTION", ""), allow_local=kit.test_mode)
        except lockdown.LockdownError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if result not in ("nothing-pending", "not-ready"):
            print(result)
            return 0
        journal = host.read_text(SYS.journal, "{}") or "{}"
        try:
            st = json.loads(journal).get("steps", {}).get("lockdown", {}).get("status")
        except json.JSONDecodeError:
            st = None
        if st == "skipped":
            print("lockdown-skipped")
            return 0
        if time.time() >= deadline:
            print(result)
            return 0 if wait == 0 else 3
        time.sleep(3)


def _forget_device(host: Host, device: str) -> int:
    from .server import keys as K
    cfg, _ = _server(host)
    removed = K.remove_device(host, K.authorized_keys_path(host, cfg.owner), device)
    try:
        devices = json.loads(host.read_text(SYS.devices, "{}") or "{}")
    except json.JSONDecodeError:
        devices = {}
    known = devices.pop(device, None) is not None
    host.write_atomic(SYS.devices, json.dumps(devices, indent=1, sort_keys=True), mode=0o644)
    print(t("cli.device_forgotten", device=device, keys=removed) if (removed or known) else
          t("cli.device_unknown", device=device))
    return 0 if (removed or known) else 1


def _brain(host: Host, args) -> int:
    from . import brainslot
    from . import manifest as mf
    if args.action == "status":
        print(host.read_text(brainslot.UNITS_FILE, "{}"))
        return 0
    if not args.manifest:
        print("manifest path required", file=sys.stderr)
        return 2
    with open(args.manifest, encoding="utf-8") as fh:
        text = fh.read()
    try:
        man = mf.load(text)
    except mf.ManifestError as exc:
        for p in exc.problems:
            print(f"  - {p}", file=sys.stderr)
        return 1
    if args.action == "validate":
        print(t("brain.valid", name=man.name, n=len(man.services)))
        return 0
    _need_root()
    cfg, kit = _server(host)
    from .state import Ledger
    led = Ledger(host)
    rec = brainslot.apply_linux(host, led, cfg, text)
    from .linux.steps_base import MemoryBudget
    from .state import Journal
    from .steps import Context
    from .ui import UI
    ctx = Context(host=host, ui=UI(interactive=False), journal=Journal(host), ledger=led, cfg=cfg, kit=kit)
    mb = MemoryBudget()
    if not mb.check(ctx):
        mb.apply(ctx)
    healthy = brainslot.wait_healthy(man)
    print(json.dumps({"applied": rec.get("all", []), "healthy": healthy}))
    return 0 if healthy is not False else 1


def _bridge_receive(host: Host, device: str) -> int:
    from . import bridge
    from .config import DEVICE_RE
    from pathlib import Path
    if not DEVICE_RE.match(device):
        return 2
    cfg, _ = _server(host)
    inbox = Path(cfg.brain_home) / cfg.session_inbox
    if not inbox.is_dir():
        print("inbox missing", file=sys.stderr)
        return 1
    os.umask(0o027)
    try:
        bridge.receive(sys.stdin.buffer, sys.stdout.buffer, inbox / device)
    except bridge.Rejected as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


def _bridge_local(host: Host) -> int:
    from . import bridge
    from pathlib import Path
    cfg, _ = _server(host)
    home = os.path.expanduser("~")
    state_dir = Path(home) / ".local" / "state" / "pskit"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "bridge-local.json"
    try:
        state = json.loads(state_file.read_text())
    except (OSError, json.JSONDecodeError):
        state = {}
    os.umask(0o027)
    n = bridge.deliver_local(home, Path(cfg.brain_home) / cfg.session_inbox / cfg.machine_name, state)
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, state_file)
    print(json.dumps({"delivered": n}))
    return 0


def _with_lock(lockfile: str, command: List[str]) -> int:
    """flock(1) semantics: wait for the lock, then replace this process with
    the command; the lock is held until the command exits."""
    import fcntl
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("with-lock: no command", file=sys.stderr)
        return 2
    # Read-only is enough for flock and works for users who cannot write the
    # lock file; create it only if missing.
    try:
        fd = os.open(lockfile, os.O_RDONLY)
    except FileNotFoundError:
        fd = os.open(lockfile, os.O_RDWR | os.O_CREAT, 0o664)
    fcntl.flock(fd, fcntl.LOCK_EX)
    os.set_inheritable(fd, True)
    os.execvp(command[0], command)  # noqa: S606 - the declared command, as given
    return 127


def _ci_attach(host: Host, unit_name: str) -> int:
    from .paths import SLICE_CI
    from .state import Ledger, install_file
    if not unit_name.endswith(".service") or "/" in unit_name:
        print("unit must be a .service name", file=sys.stderr)
        return 2
    path = f"/etc/systemd/system/{unit_name}.d/50-pskit-ci.conf"
    install_file(host, Ledger(host), path, f"# Managed by pskit\n[Service]\nSlice={SLICE_CI}\n", mode=0o644)
    host.check(["systemctl", "daemon-reload"])
    print(t("ci.attached", unit=unit_name))
    return 0


if __name__ == "__main__":
    sys.exit(main())
