"""Session delivery: agent sessions from every machine arrive in one inbox.

Why not rsync: macOS ships openrsync, whose options differ from rsync 3,
and a plain rsync key could write anywhere the owner can. Here the laptop's
bridge key is bound to one forced command on the server, which accepts
files only under the inbox, only for that device, only for known agent
session trees. Session files are append-only, so after the first run only
the new tail of each file travels.

Stream (client to server), after one JSON line {"v": 1}:
  header line {"path": "claude/projects/x/y.jsonl", "offset": N, "length": L}
  exactly L raw bytes
  ... then {"end": true}
Answer (server to client), written only after the whole input was read so
neither side can block on a full pipe: one JSON line per file with its
status (ok, resync, rejected), then {"done": true}.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, Dict, Iterator, List, Optional, Tuple

PROTOCOL = 1
MAX_FILE = 512 * 1024 * 1024
MAX_RUN_SERVER = 2 * 1024 * 1024 * 1024
MAX_RUN_CLIENT = 256 * 1024 * 1024
MAX_PATH = 1024
MAX_DEPTH = 12
MAX_FILES_RUN = 5000
CHUNK = 1 << 20


@dataclass(frozen=True)
class Source:
    label: str
    base: str  # relative to home

    def accepts(self, rel: str) -> bool:
        name = rel.rsplit("/", 1)[-1]
        if self.label == "claude":
            return name.endswith(".jsonl")
        if self.label == "codex":
            return name.startswith("rollout-") and name.endswith(".jsonl")
        if self.label == "grok":
            # Session directories are the url-encoded absolute project path
            # (%2F...). The search index and lock files stay behind: the
            # index is rewritten whole and is not the conversation.
            first = rel.split("/", 1)[0]
            return first.startswith("%2F") and not name.endswith(".lock") \
                and not name.startswith("session_search.sqlite")
        return False


SOURCES = (
    Source("claude", ".claude/projects"),
    Source("codex", ".codex/sessions"),
    Source("grok", ".grok/sessions"),
)
LABELS = {s.label: s for s in SOURCES}


def safe_rel(path: str) -> Optional[str]:
    """Normalized relative path inside a known source, or None."""
    if not isinstance(path, str) or not path or len(path) > MAX_PATH or "\0" in path or "\\" in path:
        return None
    if path.startswith("/"):
        return None
    parts = path.split("/")
    if len(parts) < 3 or len(parts) > MAX_DEPTH:
        return None
    if any(p in ("", ".", "..") or p.startswith(".pskit") for p in parts):
        return None
    src = LABELS.get(parts[0])
    if not src:
        return None
    base_tail = src.base.split("/")[-1]
    if parts[1] != base_tail:
        return None
    if not src.accepts("/".join(parts[2:])):
        return None
    return "/".join(parts)


# ---- scanning (client and local) --------------------------------------

@dataclass
class LocalFile:
    rel: str
    abspath: str
    size: int
    mtime: float
    ino: int


def scan(home: str, sources=SOURCES) -> Iterator[LocalFile]:
    for src in sources:
        base = os.path.join(home, src.base)
        if not os.path.isdir(base):
            continue
        tail = src.base.split("/")[-1]
        for root, dirs, files in os.walk(base, followlinks=False):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                full = os.path.join(root, f)
                inner = os.path.relpath(full, base).replace(os.sep, "/")
                if not src.accepts(inner):
                    continue
                try:
                    st = os.lstat(full)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_FILE:
                    continue
                rel = f"{src.label}/{tail}/{inner}"
                if safe_rel(rel) is None:
                    continue
                yield LocalFile(rel, full, st.st_size, st.st_mtime, st.st_ino)


TAIL = 4096


def tail_hash(path: str, end: int) -> Optional[str]:
    """Hash of the last bytes before `end`: proves the part already sent is
    unchanged, so only the new tail needs to travel."""
    import hashlib
    start = max(0, end - TAIL)
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(end - start)
    except OSError:
        return None
    if len(data) != end - start:
        return None
    return hashlib.sha256(data).hexdigest()


def plan(files: List[LocalFile], state: Dict[str, Dict], budget: int = MAX_RUN_CLIENT) -> List[Tuple[LocalFile, int]]:
    """(file, offset) pairs to send, oldest change first, within budget."""
    todo: List[Tuple[LocalFile, int]] = []
    for f in sorted(files, key=lambda x: x.mtime):
        prev = state.get(f.rel)
        if prev and prev.get("ino") == f.ino and prev.get("size") == f.size and prev.get("mtime") == f.mtime:
            continue
        offset = 0
        if prev and prev.get("ino") == f.ino and f.size > int(prev.get("size", 0)) \
                and prev.get("tail") and tail_hash(f.abspath, int(prev["size"])) == prev["tail"]:
            offset = int(prev["size"])
        if f.size == offset and offset > 0:
            continue
        todo.append((f, offset))
    out: List[Tuple[LocalFile, int]] = []
    used = 0
    for f, off in todo:
        cost = f.size - off
        if out and (used + cost > budget or len(out) >= MAX_FILES_RUN):
            break
        out.append((f, off))
        used += cost
    return out


def write_stream(out: IO[bytes], items: List[Tuple[LocalFile, int]]) -> List[Tuple[LocalFile, int, int]]:
    """Writes the request stream in chunks (a large session file never sits
    in memory whole). Returns (file, offset, length) actually sent. If a
    file shrinks while being sent, the stream stops there: the server keeps
    what arrived complete, and the rest goes next run."""
    sent: List[Tuple[LocalFile, int, int]] = []
    out.write(json.dumps({"v": PROTOCOL}).encode() + b"\n")
    for f, offset in items:
        length = f.size - offset
        try:
            fh = open(f.abspath, "rb")  # noqa: SIM115 - closed by the `with fh` below; unreadable files are skipped
        except OSError:
            continue
        with fh:
            fh.seek(offset)
            out.write(json.dumps({"path": f.rel, "offset": offset, "length": length}).encode() + b"\n")
            left = length
            while left:
                chunk = fh.read(min(CHUNK, left))
                if not chunk:
                    out.flush()
                    return sent
                out.write(chunk)
                left -= len(chunk)
        sent.append((f, offset, length))
    out.write(json.dumps({"end": True}).encode() + b"\n")
    out.flush()
    return sent


# ---- receiving (server) ------------------------------------------------

class Rejected(Exception):
    pass


def _open_parent(root: Path, rel: str) -> Path:
    """Creates parent directories under root, refusing symlinks anywhere."""
    current = root
    parts = rel.split("/")[:-1]
    for p in parts:
        current = current / p
        try:
            st = os.lstat(current)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
                raise Rejected("path component is not a directory")
        except FileNotFoundError:
            os.mkdir(current, 0o2750)
    return current


def _read_exact(src: IO[bytes], n: int, sink: Optional[IO[bytes]] = None) -> int:
    got = 0
    while got < n:
        chunk = src.read(min(1 << 20, n - got))
        if not chunk:
            break
        if sink is not None:
            sink.write(chunk)
        got += len(chunk)
    return got


def receive(src: IO[bytes], dst: IO[bytes], device_root: Path, now: Callable[[], float] = time.time) -> Dict:
    """Server side. device_root = <inbox>/<device>, created if missing."""
    device_root.mkdir(mode=0o2750, parents=True, exist_ok=True)
    if device_root.is_symlink():
        raise Rejected("device directory is a symlink")
    first = src.readline(4096)
    try:
        hello = json.loads(first or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        hello = {}
    if not isinstance(hello, dict) or hello.get("v") != PROTOCOL:
        dst.write(json.dumps({"error": "protocol"}).encode() + b"\n")
        return {"error": "protocol"}
    results: List[Dict] = []
    total = 0
    files_ok = 0
    while True:
        line = src.readline(MAX_PATH + 256)
        if not line:
            break
        try:
            head = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            head = None
        if not isinstance(head, dict):
            results.append({"status": "rejected", "reason": "bad header"})
            break
        if head.get("end"):
            break
        if len(results) >= MAX_FILES_RUN:
            results.append({"status": "rejected", "reason": "too many files in one run"})
            break
        length = head.get("length")
        offset = head.get("offset")
        if not isinstance(length, int) or not isinstance(offset, int) or length < 0 or offset < 0 \
                or length > MAX_FILE or offset + length > MAX_FILE:
            results.append({"status": "rejected", "reason": "bad size"})
            break
        raw_path = head.get("path")
        rel = safe_rel(raw_path) if isinstance(raw_path, str) else None
        if rel is None or total + length > MAX_RUN_SERVER:
            _read_exact(src, length)
            results.append({"path": str(raw_path)[:200], "status": "rejected"})
            continue
        total += length
        try:
            status = _store(src, device_root, rel, offset, length)
        except Rejected as exc:
            # _store already consumed the payload before refusing.
            results.append({"path": rel, "status": "rejected", "reason": str(exc)})
            continue
        if status == "ok":
            files_ok += 1
        results.append({"path": rel, "status": status})
    receipt = {"finished": now(), "files": files_ok, "bytes": total}
    tmp = device_root / ".pskit-delivery.json.tmp"
    tmp.write_text(json.dumps(receipt))
    os.chmod(tmp, 0o640)
    os.replace(tmp, device_root / ".pskit-delivery.json")
    for r in results:
        dst.write(json.dumps(r).encode() + b"\n")
    dst.write(json.dumps({"done": True, "files": files_ok}).encode() + b"\n")
    dst.flush()
    return receipt


def _store(src: IO[bytes], device_root: Path, rel: str, offset: int, length: int) -> str:
    try:
        parent = _open_parent(device_root, rel)
    except Rejected:
        _read_exact(src, length)
        raise
    target = parent / rel.rsplit("/", 1)[-1]
    if offset == 0:
        fd, tmp = tempfile.mkstemp(prefix=".incoming.", dir=str(parent))
        with os.fdopen(fd, "wb") as fh:
            got = _read_exact(src, length, fh)
        if got != length:
            os.unlink(tmp)
            return "rejected"
        os.chmod(tmp, 0o640)
        if os.path.islink(target):
            os.unlink(tmp)
            return "rejected"
        os.replace(tmp, target)
        return "ok"
    try:
        fd = os.open(str(target), os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        _read_exact(src, length)
        return "resync"
    with os.fdopen(fd, "ab") as fh:
        if os.fstat(fh.fileno()).st_size != offset:
            _read_exact(src, length)
            return "resync"
        got = _read_exact(src, length, fh)
    return "ok" if got == length else "resync"


def read_answers(src: IO[bytes]) -> Tuple[Dict[str, str], bool]:
    statuses: Dict[str, str] = {}
    done = False
    for line in src:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("done"):
            done = True
            break
        if "path" in obj:
            statuses[obj["path"]] = obj.get("status", "")
    return statuses, done


def update_state(state: Dict[str, Dict], sent: List[Tuple[LocalFile, int, int]], statuses: Dict[str, str]) -> int:
    ok = 0
    for f, offset, length in sent:
        st = statuses.get(f.rel)
        if st == "ok":
            state[f.rel] = {"size": offset + length, "mtime": f.mtime, "ino": f.ino,
                            "tail": tail_hash(f.abspath, offset + length)}
            ok += 1
        elif st == "resync":
            state.pop(f.rel, None)
    return ok


# ---- local delivery (server's own sessions) ----------------------------

def deliver_local(home: str, device_root: Path, state: Dict[str, Dict], budget: int = 1 << 30) -> int:
    """Same plan and storage rules as the network path, file by file, in
    chunks: memory stays flat whatever the size of the histories."""
    device_root.mkdir(mode=0o2750, parents=True, exist_ok=True)
    items = plan(list(scan(home)), state, budget)
    delivered, total = 0, 0
    for f, offset in items:
        length = f.size - offset
        try:
            fh = open(f.abspath, "rb")  # noqa: SIM115 - closed by the `with fh` below; unreadable files are skipped
        except OSError:
            continue
        with fh:
            fh.seek(offset)
            try:
                status = _store(fh, device_root, f.rel, offset, length)
            except Rejected:
                status = "rejected"
        if status == "ok":
            state[f.rel] = {"size": offset + length, "mtime": f.mtime, "ino": f.ino,
                            "tail": tail_hash(f.abspath, offset + length)}
            delivered += 1
            total += length
        elif status == "resync":
            state.pop(f.rel, None)
    tmp = device_root / ".pskit-delivery.json.tmp"
    tmp.write_text(json.dumps({"finished": time.time(), "files": delivered, "bytes": total}))
    os.chmod(tmp, 0o640)
    os.replace(tmp, device_root / ".pskit-delivery.json")
    return delivered
