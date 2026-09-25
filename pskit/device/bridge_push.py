"""`pskit bridge-push --server NAME`: deliver this laptop's agent sessions."""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Dict

from .. import bridge
from .local import Paths


def _state_file(paths: Paths, server: str):
    return paths.state / f"bridge-{server}.json"


def load_state(paths: Paths, server: str) -> Dict:
    try:
        return json.loads(_state_file(paths, server).read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(paths: Paths, server: str, state: Dict) -> None:
    paths.state.mkdir(parents=True, exist_ok=True)
    f = _state_file(paths, server)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, f)


def push(paths: Paths, server: str, ssh_cmd=None, budget: int = bridge.MAX_RUN_CLIENT,
         timeout: float = 600) -> Dict:
    state = load_state(paths, server)
    files_state = state.setdefault("files", {})
    items = bridge.plan(list(bridge.scan(str(paths.home))), files_state, budget)
    started = time.time()
    result: Dict = {"server": server, "started": started, "planned": len(items)}
    cmd = ssh_cmd or ["ssh", f"{server}-bridge", "bridge-receive"]
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.stdin is None or proc.stdout is None or proc.stderr is None:
            raise OSError("ssh pipes not available")
        sent = bridge.write_stream(proc.stdin, items)
        proc.stdin.close()
        statuses, done = bridge.read_answers(proc.stdout)
        err = proc.stderr.read().decode("utf-8", "replace")[-300:]
        rc = proc.wait(timeout=timeout)
    except (OSError, BrokenPipeError, subprocess.TimeoutExpired) as exc:
        result.update(ok=False, error=f"{type(exc).__name__}")
        state["last"] = result
        save_state(paths, server, state)
        return result
    delivered = bridge.update_state(files_state, sent, statuses)
    ok = rc == 0 and done
    result.update(ok=ok, delivered=delivered, sent=len(sent), finished=time.time())
    if not ok:
        result["error"] = err.strip() or f"exit {rc}"
    else:
        state["last_success"] = result["finished"]
    state["last"] = result
    save_state(paths, server, state)
    return result
