"""Shared fixtures: a fake machine under a temporary root."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from pskit.config import HostConfig, KitConfig
from pskit.i18n import set_language
from pskit.state import Journal, Ledger
from pskit.steps import Context
from pskit.system import FakeRunner, Host, Result
from pskit.ui import UI

GIB = 1024 ** 3


class NullOut:
    def __init__(self):
        self.lines = []

    def write(self, s):
        self.lines.append(s)

    def flush(self):
        pass

    def isatty(self):
        return False

    @property
    def text(self):
        return "".join(self.lines)


def write(root: Path, path: str, text: str) -> None:
    p = root / path.lstrip("/")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


class HostCase(unittest.TestCase):
    ram_gib = 32

    def setUp(self):
        set_language("en")
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.runner = FakeRunner()
        self.host = Host(str(self.root), runner=self.runner, platform="linux")
        write(self.root, "/etc/passwd", "root:x:0:0:root:/root:/bin/bash\nalice:x:1000:1000::/home/alice:/bin/bash\n")
        write(self.root, "/etc/group", "root:x:0:\nalice:x:1000:\n")
        write(self.root, "/etc/os-release", 'ID=ubuntu\nVERSION_ID="24.04"\nVERSION_CODENAME=noble\n')
        kib = int(self.ram_gib * GIB / 1024)
        write(self.root, "/proc/meminfo",
              f"MemTotal: {kib} kB\nMemAvailable: {kib // 2} kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n")
        (self.root / "home/alice").mkdir(parents=True)
        self.cfg = HostConfig(language="en", scenario="linux-server", machine_name="myserver", owner="alice",
                              brain_home="/srv/myserver/brain", workspace="/srv/myserver/workspace",
                              brain_user="pskit-brain")
        self.kit = KitConfig()
        self.out = NullOut()
        self.ui = UI({}, interactive=False, out=self.out)

    def tearDown(self):
        self._tmp.cleanup()

    def ctx(self, answers=None) -> Context:
        return Context(host=self.host, ui=UI(answers or {}, interactive=False, out=self.out),
                       journal=Journal(self.host), ledger=Ledger(self.host), cfg=self.cfg, kit=self.kit,
                       answers=answers or {}, facts={"ram_bytes": int(self.ram_gib * GIB)})

    def p(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    def read_json(self, path: str):
        return json.loads(self.p(path).read_text())


def ok(stdout: str = "") -> Result:
    return Result(0, stdout, "")


def fail(code: int = 1, stderr: str = "") -> Result:
    return Result(code, "", stderr)


def env_clear(*names):
    for n in names:
        os.environ.pop(n, None)
