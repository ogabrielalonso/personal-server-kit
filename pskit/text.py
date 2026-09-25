"""Small helpers for messages shown to the owner."""

from __future__ import annotations

from typing import List


def tail_text(text: str, limit: int = 300) -> str:
    """The end of a command's error output, cut at a line or word boundary.
    A plain character slice starts mid-word ("tory at sftp:..."), which reads
    as a broken message."""
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    kept: List[str] = []
    size = 0
    for ln in reversed(lines):
        if kept and size + len(ln) + 3 > limit:
            break
        kept.insert(0, ln)
        size += len(ln) + 3
    joined = " / ".join(kept)
    if len(joined) <= limit:
        return joined
    cut = joined[-limit:]
    space = cut.find(" ")
    return "..." + (cut[space + 1:] if 0 <= space < 40 else cut)
