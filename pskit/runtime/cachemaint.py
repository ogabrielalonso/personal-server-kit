"""`pskit cache-maint`: conservative package cache maintenance.

Runs as the owner. Never touches
sessions or projects. Fails closed on symlinks, foreign ownership, nested
mounts or special files, and skips entirely while a package manager runs.
Uses each tool's own cache command; the pip cache is emptied only after 30
days without use.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

COLD_SECONDS = 30 * 86400
PKG_NAMES = re.compile(r"^(pip[0-9.]*|npm|pnpm|yarn|uv|bun)$")


def _mounts() -> List[Path]:
    out = []
    try:
        for row in Path("/proc/self/mountinfo").read_text().splitlines():
            out.append(Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), row.split()[4])))
    except OSError:
        pass
    return out


def cache_age(path: Path, now: float, uid: int, mounts: Optional[List[Path]] = None) -> Dict:
    if not path.exists():
        return {"files": 0, "bytes": 0, "cold": False}
    if path.is_symlink() or path.resolve() != path:
        raise ValueError("cache path is not canonical")
    for mount in (mounts if mounts is not None else _mounts()):
        if mount != path and path in mount.parents:
            raise ValueError("cache contains a nested mount")
    device = path.stat().st_dev
    newest, size, count = 0.0, 0, 0

    def boom(err):
        raise err

    for parent, dirs, files in os.walk(path, followlinks=False, onerror=boom):
        for name in dirs + files:
            s = (Path(parent) / name).lstat()
            if s.st_dev != device or s.st_uid != uid or stat.S_ISLNK(s.st_mode):
                raise ValueError("cache contains foreign ownership, mount or symlink")
            if not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode)):
                raise ValueError("cache contains a special file")
            newest = max(newest, s.st_mtime, s.st_ctime, s.st_atime if stat.S_ISREG(s.st_mode) else 0)
            if stat.S_ISREG(s.st_mode):
                count += 1
                size += s.st_blocks * 512
    return {"files": count, "bytes": size, "cold": bool(count and now - newest >= COLD_SECONDS)}


def package_managers_running(uid: int) -> List[int]:
    found = []
    for p in Path("/proc").iterdir():
        if not p.name.isdigit() or int(p.name) == os.getpid():
            continue
        try:
            if p.stat().st_uid != uid:
                continue
            args = (p / "cmdline").read_bytes().decode(errors="replace").split("\0")
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            raise RuntimeError("unable to inspect a process of the same user")
        names = [Path(a).name for a in args[:4] if a]
        if any(PKG_NAMES.match(n) for n in names) or ("-m" in args[:3] and "pip" in args[:4]):
            found.append(int(p.name))
    return found


def run(home: Optional[str] = None, apply: bool = True) -> Dict:
    home_p = Path(home or os.path.expanduser("~"))
    uid = os.getuid()
    now = time.time()
    report: Dict = {"time": now, "apply": apply, "actions": []}
    try:
        blockers = package_managers_running(uid)
        if blockers:
            report.update(status="skipped_package_manager_active", blockers=blockers)
            print(json.dumps(report))
            return report
        pip_dir = home_p / ".cache/pip"
        npm_dir = home_p / ".npm/_cacache"
        pip = cache_age(pip_dir, now, uid)
        npm = cache_age(npm_dir, now, uid)
        report.update(pip_before=pip, npm_before=npm, status="ok")
        actions = []
        if pip["cold"]:
            pip_exe = shutil.which("pip3") or shutil.which("pip")
            if pip_exe:
                actions.append(("pip_purge_30d_idle", [pip_exe, "cache", "--cache-dir", str(pip_dir), "purge"]))
        npm_exe = shutil.which("npm")
        if npm["files"] and npm_exe:
            actions.append(("npm_cache_verify", [npm_exe, "cache", "verify", "--cache", str(home_p / ".npm")]))
        uv = shutil.which("uv") or (str(home_p / ".local/bin/uv") if (home_p / ".local/bin/uv").exists() else None)
        if uv:
            actions.append(("uv_cache_prune", [uv, "cache", "prune"]))
        for name, cmd in actions:
            if not apply:
                report["actions"].append({"name": name, "status": "would_run"})
                continue
            if package_managers_running(uid):
                report["actions"].append({"name": name, "status": "skipped_new_package_manager"})
                break
            r = subprocess.run(cmd, cwd="/", capture_output=True, timeout=600)
            report["actions"].append({"name": name, "returncode": r.returncode})
            if r.returncode:
                raise RuntimeError(name + " failed")
        report["pip_after"] = cache_age(pip_dir, time.time(), uid)
        report["npm_after"] = cache_age(npm_dir, time.time(), uid)
    except Exception as exc:
        report.update(status="failed_closed", error=f"{type(exc).__name__}: {exc}")
    print(json.dumps(report))
    return report
