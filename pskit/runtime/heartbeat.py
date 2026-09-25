"""`pskit heartbeat`: tells an external watcher the machine is alive.

A machine cannot report its own death. The owner creates a check at a
dead-man service (for example healthchecks.io) that alerts them when pings
stop; the URL is stored as a secret because anyone holding it can ping."""

from __future__ import annotations

import urllib.request

from ..config import read_env_file
from ..paths import SYS
from ..system import Host

SECRET = f"{SYS.secrets}/heartbeat.env"


def run(host: Host) -> int:
    url = read_env_file(host, SECRET).get("HEARTBEAT_URL", "")
    if not url.startswith("https://"):
        print("heartbeat: not configured")
        return 0
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "pskit-heartbeat"})  # noqa: S310
        with urllib.request.urlopen(req, timeout=15) as resp:  # noqa: S310 - https only, owner supplied
            ok = 200 <= resp.status < 300
    except Exception as exc:
        print(f"heartbeat: failed ({type(exc).__name__})")
        return 1
    print("heartbeat: ok" if ok else "heartbeat: unexpected status")
    return 0 if ok else 1
