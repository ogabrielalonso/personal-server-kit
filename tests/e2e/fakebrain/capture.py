"""Stand-in batch job: counts delivered session files and writes a note."""

import os
import pathlib
import time

home = pathlib.Path(os.environ["BRAIN_HOME"])
inbox = home / "inbox" / "sessions"
count = sum(1 for p in inbox.rglob("*.jsonl")) if inbox.exists() else 0
(home / "state").mkdir(exist_ok=True)
(home / "state" / "last-capture.txt").write_text(f"{time.time()} {count}\n")
print(f"capture: {count} session files")
