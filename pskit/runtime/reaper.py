"""`pskit reaper`: ends what sessions forget running.

Two rules learned the hard way:
  - match by executable name (comm), never by the whole command line: a
    test once killed its own SSH session because the command text contained
    'headless_shell';
  - count only real connections (the header of `ss` once counted as one).

Rules:
  headless browser (headless_shell, chrome_crashpad, or chrome/chromium
  with --headless): older than 3 h, killed.
  dev server (next dev, vite, webpack-dev-server, nodemon, http.server under
  node/python/bun/deno): older than 12 h and no established connection on
  any port it listens on, killed.
Only processes in the owner's session slice (never system services, never
the owner's systemd --user manager, never CI).
"""

from __future__ import annotations

import os
import re
import signal
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set

from .. import notify
from ..config import HostConfig, KitConfig
from ..i18n import t_in
from ..system import Host

BROWSER_COMM = re.compile(r"^(headless_shell|chrome_crashpad.*)$")
CHROME_COMM = re.compile(r"^(chrome|chromium|chromium-browse|chromium-browser)$")
DEVSRV_TOOLS = {"next-server", "vite", "webpack-dev-server", "nodemon"}
DEVSRV_COMM = re.compile(r"^(node|python3|python|bun|deno|next-server.*)$")


@dataclass
class Proc:
    pid: int
    comm: str
    args: List[str]
    age_s: float
    cgroup: str
    uid: int


def _boot_time(host: Host) -> float:
    for line in (host.read_text("/proc/stat", "") or "").splitlines():
        if line.startswith("btime "):
            return float(line.split()[1])
    return 0.0


def list_procs(host: Host, now: Optional[float] = None) -> List[Proc]:
    now = now or time.time()
    hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
    btime = _boot_time(host)
    out = []
    proc = host.p("/proc")
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            comm = (d / "comm").read_text().strip()
            args = [a for a in (d / "cmdline").read_bytes().decode("utf-8", "replace").split("\0") if a]
            stat = (d / "stat").read_text()
            # Field 22 (starttime) counted after the ')' that ends comm.
            rest = stat.rsplit(")", 1)[1].split()
            start = btime + int(rest[19]) / hz
            cgroup = (d / "cgroup").read_text()
            uid = d.stat().st_uid
        except (OSError, IndexError, ValueError):
            continue
        out.append(Proc(int(d.name), comm, args, max(0.0, now - start), cgroup, uid))
    return out


def in_owner_session(p: Proc, uid: int) -> bool:
    return f"user-{uid}.slice" in p.cgroup and f"user@{uid}.service" not in p.cgroup


def is_headless_browser(p: Proc) -> bool:
    if BROWSER_COMM.match(p.comm):
        return True
    return bool(CHROME_COMM.match(p.comm)) and any(a == "--headless" or a.startswith("--headless=") for a in p.args)


def is_dev_server(p: Proc) -> bool:
    """Whole argument tokens only: 'invite-bot/worker.js' or a folder named
    'nodemonitor' must not look like vite or nodemon."""
    if not DEVSRV_COMM.match(p.comm):
        return False
    names = []
    for a in p.args:
        n = a.rsplit("/", 1)[-1]
        for ext in (".js", ".mjs", ".cjs"):
            if n.endswith(ext):
                n = n[: -len(ext)]
        names.append(n)
    # Next.js sets its process title to "next-server (vX.Y.Z)".
    if any(n in DEVSRV_TOOLS or n.startswith("next-server (") for n in names):
        return True
    for i, n in enumerate(names[:-1]):
        if n == "next" and names[i + 1] == "dev":
            return True
        if n == "-m" and names[i + 1] == "http.server":
            return True
    return False


def _socket_inodes(host: Host, pid: int) -> Set[str]:
    out = set()
    fd_dir = host.p(f"/proc/{pid}/fd")
    try:
        for fd in fd_dir.iterdir():
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            if target.startswith("socket:["):
                out.add(target[8:-1])
    except OSError:
        pass
    return out


def _tcp_table(host: Host) -> List[List[str]]:
    rows = []
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        for line in (host.read_text(f, "") or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) > 9:
                rows.append(parts)
    return rows


def has_established(host: Host, pid: int) -> bool:
    inodes = _socket_inodes(host, pid)
    table = _tcp_table(host)
    ports = {int(r[1].rsplit(":", 1)[1], 16) for r in table if r[3] == "0A" and r[9] in inodes}
    if not ports:
        return False
    return any(r[3] == "01" and int(r[1].rsplit(":", 1)[1], 16) in ports for r in table)


def plan(host: Host, owner_uid: int, kit: KitConfig, procs: Optional[List[Proc]] = None) -> List[Dict]:
    procs = procs if procs is not None else list_procs(host)
    victims = []
    for p in procs:
        if p.pid == os.getpid() or not in_owner_session(p, owner_uid):
            continue
        if is_headless_browser(p) and p.age_s > kit.reaper_browser_max_age_h * 3600:
            victims.append({"pid": p.pid, "kind": "browser", "age_h": int(p.age_s // 3600), "comm": p.comm})
        elif is_dev_server(p) and p.age_s > kit.reaper_devserver_max_age_h * 3600 \
                and not has_established(host, p.pid):
            victims.append({"pid": p.pid, "kind": "devserver", "age_h": int(p.age_s // 3600), "comm": p.comm})
    return victims


def run(host: Host, cfg: HostConfig, kit: KitConfig, killer: Callable[[int, int], None] = os.kill,
        sleep: Callable[[float], None] = time.sleep) -> List[Dict]:
    if not kit.reaper_enabled or host.platform != "linux":
        return []
    import pwd
    try:
        uid = pwd.getpwnam(cfg.owner).pw_uid
    except KeyError:
        return []
    victims = plan(host, uid, kit)
    for v in victims:
        print(f"[reaper] ending {v['kind']} pid={v['pid']} comm={v['comm']} age={v['age_h']}h")
        try:
            killer(v["pid"], signal.SIGTERM)
        except ProcessLookupError:
            continue
    if victims:
        sleep(3)
        for v in victims:
            try:
                killer(v["pid"], signal.SIGKILL)
            except ProcessLookupError:
                pass
        for v in victims:
            key = "reaper.browser" if v["kind"] == "browser" else "reaper.devserver"
            notify.enqueue(host, "info", t_in(cfg.language, key, hours=v["age_h"]), source="reaper")
    else:
        print("[reaper] nothing to end")
    return victims
