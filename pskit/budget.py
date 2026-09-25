"""Memory budget for systemd slices (Linux).

Rule: the sum of the ceilings stays below physical RAM, leaving a margin
for the kernel and anything outside the slices. Every value is a fraction
of RAM; a 32 GiB machine with the CI module gets brain 4/8/10
(low/high/max), owner sessions 12.25/14.25, CI 3/4, system protection 2,
margin 3.75 GiB.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Dict, Optional

GIB = 1024 ** 3
MIB = 1024 ** 2


@dataclass
class Budget:
    ram_bytes: int
    margin_bytes: int
    system_low: int
    user_high: int
    user_max: int
    brain_low: int
    brain_high: int
    brain_max: int
    ci_high: int
    ci_max: int
    swap_max: int
    parent_low: int
    parent_max: int

    def ceilings_sum(self) -> int:
        return self.user_max + self.brain_max + self.ci_max

    def as_gib(self) -> Dict[str, float]:
        return {k: round(v / GIB, 2) for k, v in asdict(self).items()}


def _round_down(value: float, step: int = 256 * MIB) -> int:
    return int(value // step * step)


def compute(ram_bytes: int, brain_peak_bytes: Optional[int] = None, with_ci: bool = False) -> Budget:
    """Computes the slice limits.

    brain_peak_bytes: the brain's declared peak (from its manifest). The
    ceiling gets 25% headroom over it, bounded by a third of RAM. Without a
    manifest, the reference proportion is used.
    """
    if ram_bytes < 2 * GIB:
        raise ValueError("at least 2 GiB of RAM is required")
    ram = float(ram_bytes)
    margin = max(1.0 * GIB, 0.106 * ram)
    system_low = min(2.0 * GIB, 0.064 * ram)
    ci_max = 0.1277 * ram if with_ci else 0.0
    ci_high = ci_max * 0.75
    if brain_peak_bytes:
        brain_max = min(max(brain_peak_bytes * 1.25, 1.0 * GIB), ram / 3)
    else:
        brain_max = 0.3191 * ram
    brain_low = brain_max * 0.4
    brain_high = brain_max * 0.8
    user_max = ram - margin - brain_max - ci_max
    user_high = user_max * (12 / 14)
    if user_max < 1.0 * GIB:
        raise ValueError("not enough memory for owner sessions after the brain reservation")
    b = Budget(
        ram_bytes=int(ram_bytes),
        margin_bytes=0,
        system_low=_round_down(system_low),
        user_high=_round_down(user_high),
        user_max=_round_down(user_max),
        brain_low=_round_down(brain_low),
        brain_high=_round_down(brain_high),
        brain_max=_round_down(brain_max),
        ci_high=_round_down(ci_high),
        ci_max=_round_down(ci_max),
        swap_max=_round_down(min(2.0 * GIB, 0.064 * ram)),
        parent_low=0,
        parent_max=0,
    )
    b.parent_low = b.brain_low
    b.parent_max = b.brain_max + b.ci_max
    b.margin_bytes = b.ram_bytes - b.ceilings_sum()
    return b


def to_systemd(value: int) -> str:
    """Bytes to a systemd size, in whole MiB (exact, never rounded up)."""
    if value <= 0:
        return "0"
    mib = value // MIB
    if mib % 1024 == 0:
        return f"{mib // 1024}G"
    return f"{mib}M"


def swap_size_bytes(ram_bytes: int, free_disk_bytes: int) -> int:
    """Half the RAM, capped at 16 GiB, and never more than 10% of free disk."""
    want = min(ram_bytes / 2, 16 * GIB)
    cap = free_disk_bytes * 0.10
    size = min(want, cap)
    return int(size // GIB * GIB) if size >= GIB else 0


def human(value: int) -> str:
    if value >= GIB:
        return f"{value / GIB:.1f} GiB"
    return f"{math.ceil(value / MIB)} MiB"
