"""`pskit backup`: encrypted backup with restic, and a restore proof.

The backup takes the heavy-job lock (it is heavy), writes a receipt on
success, and alerts on failure. The restore proof restores a sample of
files from the latest snapshot and compares them by hash with the live
files that did not change since.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import random
import re
import time
from typing import IO, Dict, List, Optional, Tuple

from .. import manifest as mf
from .. import notify
from ..checks import receipt, write_receipt
from ..config import HostConfig, KitConfig, read_env_file
from ..i18n import t_in
from ..paths import SYS, heavy_lock
from ..system import BytesResult, Host, Result
from ..text import tail_text

ENV_FILE = f"{SYS.secrets}/restic.env"
PASS_FILE = f"{SYS.secrets}/restic.pass"

# Rebuildable by definition. Kept short on purpose: when in doubt, back up.
DEFAULT_EXCLUDES = [
    "node_modules", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".next/cache", ".turbo", ".gradle/caches",
]
HOME_EXCLUDES = [".cache", ".npm", ".local/share/Trash", ".local/share/pnpm", ".cargo/registry",
                 ".rustup", ".bun/install/cache", "go/pkg/mod"]


class BackupError(RuntimeError):
    pass


def restic_env(host: Host) -> Dict[str, str]:
    env = read_env_file(host, ENV_FILE)
    env.setdefault("RESTIC_PASSWORD_FILE", str(host.p(PASS_FILE)))
    env.setdefault("RESTIC_CACHE_DIR", str(host.p("/var/cache/pskit/restic")))
    return env


def _restic_cmd(host: Host, args: List[str]) -> Tuple[List[str], Dict[str, str]]:
    env = restic_env(host)
    extra: List[str] = ["--retry-lock", "30m"]
    sftp_args = env.pop("PSKIT_SFTP_ARGS", "")
    if sftp_args:
        extra += ["-o", f"sftp.args={sftp_args}"]
    exe = str(host.p(SYS.restic)) if host.exists(SYS.restic) else "restic"
    return [exe] + extra + args, env


def restic(host: Host, args: List[str], timeout: float = 6 * 3600) -> Result:
    """Every call waits up to 30 minutes for the repository lock instead of
    failing (the weekly prune can hold it while the restore test starts)."""
    cmd, env = _restic_cmd(host, args)
    return host.run(cmd, env=env, timeout=timeout)


def restic_bytes(host: Host, args: List[str], timeout: float = 3600) -> BytesResult:
    cmd, env = _restic_cmd(host, args)
    return host.runner.run_bytes(cmd, env=env, timeout=timeout)


def backup_set(host: Host, cfg: HostConfig, kit: KitConfig) -> Tuple[List[str], List[str]]:
    paths: List[str] = []
    excludes: List[str] = list(DEFAULT_EXCLUDES) + list(kit.backup_excludes)
    if cfg.workspace:
        paths.append(cfg.workspace)
    home = host.user_home(cfg.owner) if cfg.owner else None
    if home:
        paths.append(home)
        excludes += [f"{home}/{x}" for x in HOME_EXCLUDES]
    paths.append(SYS.etc)
    excludes.append(SYS.secrets)
    raw = host.read_text(SYS.brain_manifest)
    man = None
    if raw:
        try:
            man = mf.load(raw)
        except mf.ManifestError:
            man = None
    if man and man.backup_include:
        paths += [f"{cfg.brain_home}/{p}" for p in man.backup_include]
    elif cfg.brain_home:
        paths.append(cfg.brain_home)
    if man:
        excludes += [f"{cfg.brain_home}/{p}" for p in man.backup_exclude]
    paths += list(kit.backup_paths)
    seen, uniq = set(), []
    for p in paths:
        if p and p not in seen and host.exists(p):
            seen.add(p)
            uniq.append(p)
    return uniq, excludes


class HeavyLock:
    def __init__(self, host: Host, wait_s: float = 3600):
        self.path = host.p(heavy_lock())
        self.wait_s = wait_s
        self.fh: Optional[IO[str]] = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a")
        self.fh = fh
        deadline = time.time() + self.wait_s
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.time() > deadline:
                    raise BackupError("heavy-job lock busy for too long")
                time.sleep(10)

    def __exit__(self, *exc):
        if self.fh:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
            self.fh.close()


def ensure_repo(host: Host) -> None:
    if restic(host, ["cat", "config"], timeout=300).ok:
        return
    r = restic(host, ["init"], timeout=600)
    if not r.ok:
        raise BackupError(f"init failed: {tail_text(r.stderr)}")


def run_backup(host: Host, cfg: HostConfig, kit: KitConfig, if_stale_hours: Optional[float] = None,
               now: Optional[float] = None) -> Dict:
    now = now or time.time()
    if kit.backup_kind == "later":
        return {"skipped": "not configured"}
    if if_stale_hours is not None:
        last = receipt(host, "backup-success")
        if last and now - float(last.get("finished", 0)) < if_stale_hours * 3600:
            return {"skipped": "recent"}
    paths, excludes = backup_set(host, cfg, kit)
    started = time.time()
    try:
        with HeavyLock(host):
            args = ["backup", "--json", "--tag", "pskit", "--host", cfg.machine_name]
            for e in excludes:
                args += ["--exclude", e]
            args += paths
            r = restic(host, args)
            if r.returncode not in (0, 3):
                # 3 = some files could not be read; the snapshot exists.
                raise BackupError(tail_text(r.stderr, 400) or f"restic exit {r.returncode}")
            summary = {}
            for line in r.stdout.splitlines():
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("message_type") == "summary":
                    summary = obj
            keep = ["forget", "--tag", "pskit", "--host", cfg.machine_name,
                    "--keep-daily", str(kit.backup_keep_daily), "--keep-weekly", str(kit.backup_keep_weekly),
                    "--keep-monthly", str(kit.backup_keep_monthly)]
            if time.gmtime(now).tm_wday == 6:
                keep.append("--prune")
            f = restic(host, keep)
    except BackupError as exc:
        write_receipt(host, "backup-failure", {"finished": time.time(), "error": str(exc)[-400:]})
        notify.enqueue(host, "warn", t_in(cfg.language, "backup.failed_title"),
                       t_in(cfg.language, "backup.failed_body"), key="backup-failed", cooldown_hours=12,
                       source="backup")
        raise
    rec = {
        "finished": time.time(),
        "duration_s": int(time.time() - started),
        "snapshot": summary.get("snapshot_id", ""),
        "files_new": summary.get("files_new"),
        "files_changed": summary.get("files_changed"),
        "bytes_added": summary.get("data_added"),
        "partial": r.returncode == 3,
        "forget_ok": f.ok,
        "paths": paths,
    }
    write_receipt(host, "backup-success", rec)
    if r.returncode == 3:
        notify.enqueue(host, "info", t_in(cfg.language, "backup.partial"), source="backup")
    return rec


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def restore_test(host: Host, cfg: HostConfig, kit: KitConfig, sample: int = 12,
                 max_file_bytes: int = 50 * 1024 * 1024) -> Dict:
    """Reads a sample of files back from the latest snapshot and compares
    them by hash with live files that did not change since. `restic dump`
    takes the exact path (restore --include would treat [id].tsx as a glob)."""
    if kit.backup_kind == "later":
        return {"skipped": "not configured"}
    try:
        with HeavyLock(host, wait_s=90 * 60):
            return _restore_test(host, cfg, sample, max_file_bytes)
    except BackupError as exc:
        rec = {"ok": False, "finished": time.time(), "error": str(exc)}
        write_receipt(host, "restore-test", rec)
        return rec


def _restore_test(host: Host, cfg: HostConfig, sample: int, max_file_bytes: int) -> Dict:
    r = restic(host, ["ls", "latest", "--json", "--host", cfg.machine_name], timeout=3600)
    if not r.ok:
        rec = {"ok": False, "finished": time.time(), "error": "cannot list latest snapshot"}
        write_receipt(host, "restore-test", rec)
        return rec
    snap_time = None
    candidates = []
    for line in r.stdout.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("struct_type") == "snapshot" or ("time" in obj and "paths" in obj and "type" not in obj):
            snap_time = obj.get("time")
            continue
        if obj.get("type") == "file" and 0 < int(obj.get("size", 0)) <= max_file_bytes:
            candidates.append(obj)
    snap_ts = _parse_time(snap_time) if snap_time else time.time()
    stable = []
    for c in candidates:
        try:
            st = host.p(c["path"]).stat()
        except OSError:
            continue
        if st.st_mtime < snap_ts - 60 and st.st_size == int(c["size"]):
            stable.append(c)
    random.shuffle(stable)
    chosen = stable[:sample]
    if not chosen:
        rec = {"ok": False, "finished": time.time(), "error": "no stable file to compare"}
        write_receipt(host, "restore-test", rec)
        return rec
    mismatches = []
    for c in chosen:
        got = restic_bytes(host, ["dump", "--host", cfg.machine_name, "latest", c["path"]])
        if not got.ok:
            mismatches.append(c["path"])
            continue
        try:
            live = _sha256(str(host.p(c["path"])))
        except OSError:
            mismatches.append(c["path"])
            continue
        if hashlib.sha256(got.stdout).hexdigest() != live:
            mismatches.append(c["path"])
    rec = {"ok": not mismatches, "finished": time.time(), "checked": len(chosen),
           "mismatches": mismatches[:20]}
    write_receipt(host, "restore-test", rec)
    if mismatches:
        notify.enqueue(host, "warn", t_in(cfg.language, "alert.restore_failed"), key="restore-failed",
                       cooldown_hours=24, source="backup")
    return rec


def _parse_time(value: str) -> float:
    # restic prints RFC3339 with nanoseconds; keep seconds and the offset.
    import datetime
    v = value.strip()
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    if "." in v:
        head, _, tail = v.partition(".")
        m = re.match(r"(\d+)(.*)$", tail)
        if m:
            v = f"{head}.{m.group(1)[:6].ljust(6, '0')}{m.group(2)}"
    try:
        return datetime.datetime.fromisoformat(v).timestamp()
    except ValueError:
        return time.time()
