"""`pskit pressure-guard`: reversible guard against a machine that stops
responding under load.

Every 30 s it measures CPU and IO pressure (PSI) and how long a fork takes,
and checks that SSH still listens. Six bad cycles in a row freeze the brain
and CI slices (frozen, never killed); two good cycles thaw them.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from typing import Callable, Dict, Optional

from .. import notify
from ..checks import listening_ports, ssh_ports
from ..config import HostConfig
from ..i18n import t_in
from ..paths import SLICE_BRAIN, SLICE_CI, SYS
from ..system import Host

INTERVAL = 30
BAD_CYCLES = 6
GOOD_CYCLES = 2
CPU_BAD = 80
IO_BAD = 40
FORK_BAD_MS = 500
MAX_FREEZE_S = 30 * 60
HOLD_AFTER_S = 30 * 60


def pressure_avg10(host: Host, resource: str, cls: str) -> Optional[float]:
    text = host.read_text(f"/proc/pressure/{resource}", "") or ""
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == cls:
            for p in parts[1:]:
                if p.startswith("avg10="):
                    try:
                        return float(p[6:])
                    except ValueError:
                        return None
    return None


def fork_ms() -> float:
    start = time.monotonic()
    subprocess.run(["/bin/true"], check=False)
    return (time.monotonic() - start) * 1000


def is_bad(sample: Dict) -> bool:
    if sample.get("force"):
        return True
    if not sample.get("listener"):
        return True
    cpu, io = sample.get("cpu"), sample.get("io")
    if cpu is None or io is None:
        return False
    return sample["fork_ms"] >= FORK_BAD_MS and (cpu >= CPU_BAD or io >= IO_BAD)


class Guard:
    def __init__(self, host: Host, cfg: HostConfig, sampler: Optional[Callable[[], Dict]] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.host = host
        self.cfg = cfg
        self.bad = 0
        self.good = 0
        self.sampler = sampler or self.sample
        self.clock = clock
        self.frozen_at: Optional[float] = None
        self.hold_until = 0.0
        self._ssh_ports: Optional[set] = None

    def sample(self) -> Dict:
        return {
            "cpu": pressure_avg10(self.host, "cpu", "some"),
            "io": pressure_avg10(self.host, "io", "full"),
            "fork_ms": fork_ms(),
            "listener": bool(self.ports() & listening_ports(self.host)),
            "force": self.host.exists(f"{SYS.run}/pressure-force-bad"),
        }

    def _alert(self, severity: str, title: str, body: str = "", key: str = "", cooldown_hours: float = 0) -> None:
        # Under heavy IO the spool write itself can fail; the guard must
        # keep guarding either way.
        try:
            notify.enqueue(self.host, severity, title, body, key=key, cooldown_hours=cooldown_hours,
                           source="pressure-guard")
        except OSError as exc:
            print(f"[pressure-guard] alert not queued: {exc}")

    def ports(self) -> set:
        if self._ssh_ports is None:
            self._ssh_ports = ssh_ports(self.host)
        return self._ssh_ports

    def marker(self, slice_name: str) -> str:
        return f"{SYS.run}/frozen-{slice_name}"

    def freeze(self) -> None:
        frozen = []
        for s in (SLICE_CI, SLICE_BRAIN):
            state = self.host.run(["systemctl", "show", "-p", "ActiveState", "--value", s]).stdout.strip()
            if state == "active" and self.host.run(["systemctl", "freeze", s]).ok:
                self.host.write_atomic(self.marker(s), "", mode=0o644)
                frozen.append(s)
        if frozen:
            self._alert("warn", t_in(self.cfg.language, "pressure.frozen_title"),
                        t_in(self.cfg.language, "pressure.frozen_body"), key="pressure-frozen", cooldown_hours=1)

    def thaw(self) -> None:
        thawed = []
        for s in (SLICE_CI, SLICE_BRAIN):
            if self.host.exists(self.marker(s)):
                self.host.run(["systemctl", "thaw", s])
                self.host.remove(self.marker(s))
                thawed.append(s)
        if thawed:
            self._alert("info", t_in(self.cfg.language, "pressure.thawed"))

    def cycle(self) -> str:
        now = self.clock()
        if self.frozen_at is not None and now - self.frozen_at >= MAX_FREEZE_S:
            # Never frozen for long: whatever keeps the machine under
            # pressure is not the brain or CI alone. Resume and hold off.
            self.thaw()
            self.frozen_at = None
            self.hold_until = now + HOLD_AFTER_S
            self.bad = self.good = 0
            self._alert("warn", t_in(self.cfg.language, "pressure.long_title"),
                        t_in(self.cfg.language, "pressure.long_body"), key="pressure-long", cooldown_hours=6)
            return "released"
        if is_bad(self.sampler()):
            self.bad += 1
            self.good = 0
            if self.bad >= BAD_CYCLES and now >= self.hold_until and self.frozen_at is None:
                self.freeze()
                self.frozen_at = now
                self.bad = 0
                return "froze"
            return "bad"
        self.bad = 0
        self.good += 1
        if self.good >= GOOD_CYCLES:
            self.thaw()
            self.frozen_at = None
            self.good = 0
            return "thawed"
        return "good"


def run_forever(host: Host, cfg: HostConfig) -> None:
    guard = Guard(host, cfg)

    def stop(signum, frame):
        guard.thaw()
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    os.makedirs(host.p(SYS.run), exist_ok=True)
    try:
        while True:
            guard.cycle()
            time.sleep(INTERVAL)
    finally:
        guard.thaw()
