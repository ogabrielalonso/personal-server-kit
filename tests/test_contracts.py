"""Seams between writers and readers: every command the kit writes into a
unit, a laptop job or an authorized_keys line must be one
the CLI accepts."""

import re
import shlex

from pskit.__main__ import build_parser
from pskit.device import agents
from pskit.device.local import Paths
from pskit.linux import steps_ops
from pskit.paths import SYS
from pskit.server import keys as K
from tests.helpers import HostCase


def parse(argv):
    try:
        build_parser().parse_args(argv)
    except SystemExit as exc:
        raise AssertionError(f"CLI rejects: {argv}") from exc


class ContractTest(HostCase):
    def test_systemd_units(self):
        self.kit.backup_kind = "s3"
        seen = 0
        for text in steps_ops.runtime_units(self.ctx()).values():
            for line in text.splitlines():
                if line.startswith("ExecStart=" + SYS.shim):
                    parse(shlex.split(line[len("ExecStart=" + SYS.shim):]))
                    seen += 1
        self.assertGreaterEqual(seen, 10)

    def test_lockdown_units(self):
        from pskit.render import render
        text = render("systemd/pskit-lockdown-revert.service", {"pskit": SYS.shim})
        parse(shlex.split(re.search(r"ExecStart=\S+ (.*)", text).group(1)))

    def test_laptop_jobs(self):
        paths = Paths(str(self.root))
        for job in agents.launchd_jobs(paths, "myserver").values():
            args = job["ProgramArguments"]
            if "pskit" in args:
                parse(args[args.index("pskit") + 1:])
        unit = agents.systemd_units(paths, "myserver")["pskit-bridge-myserver.service"]
        exec_args = shlex.split(re.search(r"ExecStart=(.*)", unit).group(1))
        parse(exec_args[exec_args.index("pskit") + 1:])

    def test_forced_ssh_commands(self):
        line = K.options_for("bridge", "lap", 8799)
        cmd = re.search(r'command="([^"]+)"', line).group(1).split()
        self.assertEqual(cmd[0], SYS.shim)
        parse(cmd[1:])
        self.assertIn('command="/usr/bin/false"', K.options_for("tunnel", "lap", 8799))

    def test_laptop_confirms_with_existing_command(self):
        import inspect
        from pskit.device import pair
        src = inspect.getsource(pair.confirm_lockdown)
        self.assertIn('"confirm-lockdown", "--wait"', src)
        parse(["confirm-lockdown", "--wait", "300"])
        parse(["bridge-receive", "--device", "lap"])
