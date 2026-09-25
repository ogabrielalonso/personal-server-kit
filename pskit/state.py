"""Install journal (which steps are done) and change ledger (how to undo).

The journal makes the installer resumable: a step marked done is re-checked,
not re-applied. The ledger records every change the kit made to the machine
so `pskit uninstall` can reverse them in order, restoring replaced files.
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Iterator, List, Optional

from .paths import SYS
from .system import Host, sha256_bytes


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Journal:
    def __init__(self, host: Host, path: str = SYS.journal):
        self.host = host
        self.path = path
        self.data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        raw = self.host.read_text(self.path)
        if not raw:
            return {"steps": {}, "runs": []}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # A corrupt journal only costs re-checks: every step verifies
            # the real machine state before being skipped.
            return {"steps": {}, "runs": [], "recovered_from_corrupt": now_iso()}
        data.setdefault("steps", {})
        data.setdefault("runs", [])
        return data

    def save(self) -> None:
        self.host.write_atomic(self.path, json.dumps(self.data, indent=2, sort_keys=True) + "\n", mode=0o600)

    def status(self, step_id: str) -> Optional[str]:
        return self.data["steps"].get(step_id, {}).get("status")

    def mark(self, step_id: str, status: str, detail: str = "") -> None:
        entry = self.data["steps"].setdefault(step_id, {})
        entry.update(status=status, at=now_iso())
        if detail:
            entry["detail"] = detail[-2000:]
        elif "detail" in entry and status == "done":
            entry.pop("detail")
        self.save()

    def start_run(self, kind: str, **info: Any) -> None:
        self.data["runs"].append(dict(kind=kind, started=now_iso(), **info))
        self.data["runs"] = self.data["runs"][-20:]
        self.save()

    def finish_run(self, result: str) -> None:
        if self.data["runs"]:
            self.data["runs"][-1].update(finished=now_iso(), result=result)
        self.save()


class Ledger:
    """Append-only JSON lines. Each entry is one reversible change."""

    def __init__(self, host: Host, path: str = SYS.ledger):
        self.host = host
        self.path = path

    def add(self, kind: str, **fields: Any) -> None:
        entry = dict(kind=kind, at=now_iso(), **fields)
        target = self.host.p(self.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        try:
            target.chmod(0o600)
        except OSError:
            pass

    def entries(self) -> List[Dict[str, Any]]:
        raw = self.host.read_text(self.path, "") or ""
        out = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def has(self, kind: str, **match: Any) -> bool:
        return any(e.get("kind") == kind and all(e.get(k) == v for k, v in match.items())
                   for e in self.entries())

    def reversed(self) -> Iterator[Dict[str, Any]]:
        return iter(reversed(self.entries()))


def install_file(host: Host, ledger: Ledger, path: str, content: str | bytes, mode: int = 0o644,
                 owner: Optional[str] = None, group: Optional[str] = None) -> str:
    """Writes a managed file. Returns 'unchanged', 'created' or 'replaced'.

    A file the kit did not create is backed up once before the first
    replacement, and the ledger points at the backup."""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    current = host.read_bytes(path)
    if current == raw:
        host.p(path).chmod(mode)
        if owner or group:
            host.chown(path, owner, group)
        return "unchanged"
    managed = ledger.has("file_created", path=path) or ledger.has("file_replaced", path=path)
    digest = sha256_bytes(raw)
    if current is None:
        host.write_atomic(path, raw, mode=mode, owner=owner, group=group)
        ledger.add("file_written" if managed else "file_created", path=path, sha256=digest)
        return "created"
    if not managed:
        backup = f"{SYS.backups}/{int(time.time())}{path}"
        host.write_atomic(backup, current, mode=0o600)
        ledger.add("file_replaced", path=path, backup=backup, sha256=digest)
    else:
        ledger.add("file_written", path=path, sha256=digest)
    host.write_atomic(path, raw, mode=mode, owner=owner, group=group)
    return "replaced"


def last_written_sha(ledger: Ledger) -> Dict[str, str]:
    """Latest content hash the kit wrote, per path: uninstall compares it
    with the file on disk to detect later edits by the owner."""
    out: Dict[str, str] = {}
    for e in ledger.entries():
        if e.get("kind") in ("file_created", "file_replaced", "file_written") and e.get("sha256"):
            out[e["path"]] = e["sha256"]
    return out


def add_line(host: Host, ledger: Ledger, path: str, line: str, mode: int = 0o644) -> bool:
    """Appends one line to a shared file (fstab) and records the line, not
    the file: uninstall removes exactly that line and keeps later edits."""
    text = host.read_text(path, "") or ""
    if line in text.splitlines():
        return False
    host.write_atomic(path, text.rstrip("\n") + ("\n" if text.strip() else "") + line + "\n", mode=mode)
    ledger.add("line_added", path=path, line=line)
    return True


def remove_line(host: Host, path: str, line: str) -> bool:
    text = host.read_text(path)
    if text is None:
        return False
    lines = text.splitlines()
    if line not in lines:
        return False
    kept = [ln for ln in lines if ln != line]
    host.write_atomic(path, "\n".join(kept) + ("\n" if kept else ""), mode=host.p(path).stat().st_mode & 0o777)
    return True


def ensure_dir(host: Host, ledger: Ledger, path: str, mode: int = 0o755, owner: Optional[str] = None,
               group: Optional[str] = None, keep_on_uninstall: bool = False) -> bool:
    created = host.mkdir(path, mode=mode, owner=owner, group=group)
    if created:
        ledger.add("dir_created", path=path, keep=keep_on_uninstall)
    return created
