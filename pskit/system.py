"""Machine access: files under a root prefix, commands through a runner.

Tests build a Host on a temporary directory with a FakeRunner, so every
step can be exercised without root and without touching the real system.
"""

from __future__ import annotations

import hashlib
import os
import platform as _platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence


class CommandError(RuntimeError):
    def __init__(self, args: Sequence[str], result: Result):
        self.cmd = list(args)
        self.result = result
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        super().__init__(f"command failed ({result.returncode}): {' '.join(self.cmd)}: {' | '.join(tail)}")


@dataclass
class Result:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class BytesResult:
    returncode: int
    stdout: bytes = b""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Runner:
    """Runs real commands. Never through a shell."""

    def run(self, args: Sequence[str], *, input: Optional[str] = None, timeout: float = 300,
            env: Optional[Dict[str, str]] = None, cwd: Optional[str] = None,
            user: Optional[str] = None) -> Result:
        cmd = list(args)
        if user is not None:
            cmd = ["sudo", "-n", "-u", user, "-H", "--"] + cmd if os.geteuid() == 0 else cmd
        full_env = dict(os.environ)
        full_env.setdefault("LC_ALL", "C.UTF-8")
        if env:
            full_env.update(env)
        try:
            p = subprocess.run(cmd, input=input, capture_output=True, text=True,
                               timeout=timeout, env=full_env, cwd=cwd)
        except FileNotFoundError as exc:
            return Result(127, "", str(exc))
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout if isinstance(exc.stdout, str) else ""
            return Result(124, out, f"timeout after {timeout}s")
        return Result(p.returncode, p.stdout, p.stderr)

    def run_bytes(self, args: Sequence[str], *, timeout: float = 300,
                  env: Optional[Dict[str, str]] = None) -> BytesResult:
        """Like run, for binary output (file contents)."""
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        try:
            p = subprocess.run(list(args), capture_output=True, timeout=timeout, env=full_env)
        except FileNotFoundError as exc:
            return BytesResult(127, b"", str(exc))
        except subprocess.TimeoutExpired:
            return BytesResult(124, b"", f"timeout after {timeout}s")
        return BytesResult(p.returncode, p.stdout, p.stderr.decode("utf-8", "replace"))

    def interactive(self, args: Sequence[str], env: Optional[Dict[str, str]] = None) -> int:
        """Runs attached to the terminal (password prompts, login URLs)."""
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        try:
            return subprocess.call(list(args), env=full_env)
        except FileNotFoundError:
            return 127


Handler = Callable[[List[str], Optional[str]], Result]


@dataclass
class FakeRunner(Runner):
    """Scripted runner for tests. Rules match by command prefix; the first
    matching rule answers. Unmatched commands succeed with empty output."""

    rules: List[tuple] = field(default_factory=list)
    calls: List[List[str]] = field(default_factory=list)
    default: Result = field(default_factory=lambda: Result(0))

    def on(self, prefix: Sequence[str], result: Result | Handler) -> FakeRunner:
        self.rules.append((list(prefix), result))
        return self

    def run(self, args, *, input=None, timeout=300, env=None, cwd=None, user=None):
        cmd = list(args)
        self.calls.append(cmd)
        for prefix, result in self.rules:
            if cmd[: len(prefix)] == prefix:
                return result(cmd, input) if callable(result) else result
        return self.default

    def interactive(self, args, env=None):
        return self.run(args).returncode

    def run_bytes(self, args, *, timeout=300, env=None):
        r = self.run(args, env=env, timeout=timeout)
        return BytesResult(r.returncode, r.stdout.encode("utf-8"), r.stderr)

    def called(self, prefix: Sequence[str]) -> bool:
        p = list(prefix)
        return any(c[: len(p)] == p for c in self.calls)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_platform() -> str:
    system = _platform.system()
    if system == "Linux":
        return "linux"
    if system == "Darwin":
        return "macos"
    return system.lower()


def parse_os_release(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


class Host:
    """The machine being configured.

    `root` prefixes every absolute path (tests use a temp dir). When the root
    is not "/", ownership changes are recorded instead of applied.
    """

    def __init__(self, root: str = "/", runner: Optional[Runner] = None,
                 platform: Optional[str] = None):
        self.root = Path(root)
        self.runner = runner or Runner()
        self.platform = platform or detect_platform()
        self.recorded_chowns: List[tuple] = []

    @property
    def real(self) -> bool:
        return str(self.root) == "/"

    # ---- paths -------------------------------------------------------
    def p(self, path: str | Path) -> Path:
        path = str(path)
        if not path.startswith("/"):
            raise ValueError(f"absolute path required: {path}")
        return self.root / path.lstrip("/") if not self.real else Path(path)

    def exists(self, path: str) -> bool:
        return self.p(path).exists() or self.p(path).is_symlink()

    def read_text(self, path: str, default: Optional[str] = None) -> Optional[str]:
        try:
            return self.p(path).read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return default
        except PermissionError:
            if default is not None:
                return default
            raise

    def read_bytes(self, path: str) -> Optional[bytes]:
        try:
            return self.p(path).read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            return None

    def mkdir(self, path: str, mode: int = 0o755, owner: Optional[str] = None,
              group: Optional[str] = None) -> bool:
        """Creates the directory (and parents). Returns True if it was created.
        Mode and ownership are applied in both cases."""
        target = self.p(path)
        created = not target.exists()
        target.mkdir(parents=True, exist_ok=True)
        # Ownership first: a chown can clear the setgid bit on some systems.
        if owner or group:
            self.chown(path, owner, group)
        os.chmod(target, mode)
        return created

    def write_atomic(self, path: str, data: str | bytes, mode: int = 0o644,
                     owner: Optional[str] = None, group: Optional[str] = None) -> None:
        target = self.p(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = data.encode("utf-8") if isinstance(data, str) else data
        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, mode)
            os.replace(tmp, target)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise
        if owner or group:
            self.chown(path, owner, group)

    def remove(self, path: str) -> bool:
        target = self.p(path)
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
            return True
        try:
            target.unlink()
            return True
        except FileNotFoundError:
            return False

    def chown(self, path: str, owner: Optional[str], group: Optional[str]) -> None:
        if not self.real:
            self.recorded_chowns.append((path, owner, group))
            return
        shutil.chown(self.p(path), user=owner, group=group)  # type: ignore[arg-type]

    def symlink(self, target: str, link: str) -> None:
        lp = self.p(link)
        lp.parent.mkdir(parents=True, exist_ok=True)
        tmp = lp.with_name(f".{lp.name}.new")
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        os.symlink(target, tmp)
        os.replace(tmp, lp)

    # ---- commands ----------------------------------------------------
    def run(self, args: Sequence[str], **kw) -> Result:
        return self.runner.run(args, **kw)

    def check(self, args: Sequence[str], **kw) -> Result:
        r = self.runner.run(args, **kw)
        if not r.ok:
            raise CommandError(args, r)
        return r

    def which(self, name: str) -> Optional[str]:
        if isinstance(self.runner, FakeRunner):
            r = self.runner.run(["which", name])
            return r.stdout.strip() or None if r.ok else None
        return shutil.which(name)

    # ---- facts -------------------------------------------------------
    def os_release(self) -> Dict[str, str]:
        return parse_os_release(self.read_text("/etc/os-release", "") or "")

    def meminfo_kib(self) -> Dict[str, int]:
        text = self.read_text("/proc/meminfo", "") or ""
        out: Dict[str, int] = {}
        for line in text.splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                out[parts[0].rstrip(":")] = int(parts[1])
        return out

    def disk_usage(self, path: str):
        """(total_bytes, used_bytes, free_bytes_for_users). Walks up to the
        nearest existing parent so it works before a directory exists."""
        target = self.p(path)
        while not target.exists() and target != target.parent:
            target = target.parent
        st = os.statvfs(target)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        return total, used, free

    def user_exists(self, name: str) -> bool:
        if not self.real:
            text = self.read_text("/etc/passwd", "") or ""
            return any(line.split(":")[0] == name for line in text.splitlines())
        import pwd
        try:
            pwd.getpwnam(name)
            return True
        except KeyError:
            return False

    def group_exists(self, name: str) -> bool:
        if not self.real:
            text = self.read_text("/etc/group", "") or ""
            return any(line.split(":")[0] == name for line in text.splitlines())
        import grp
        try:
            grp.getgrnam(name)
            return True
        except KeyError:
            return False

    def group_members(self, name: str) -> List[str]:
        if not self.real:
            for line in (self.read_text("/etc/group", "") or "").splitlines():
                parts = line.split(":")
                if parts[0] == name and len(parts) >= 4:
                    return [m for m in parts[3].split(",") if m]
            return []
        import grp
        try:
            return list(grp.getgrnam(name).gr_mem)
        except KeyError:
            return []

    def user_home(self, name: str) -> Optional[str]:
        if not self.real:
            for line in (self.read_text("/etc/passwd", "") or "").splitlines():
                parts = line.split(":")
                if parts[0] == name and len(parts) >= 6:
                    return parts[5]
            return None
        import pwd
        try:
            return pwd.getpwnam(name).pw_dir
        except KeyError:
            return None
