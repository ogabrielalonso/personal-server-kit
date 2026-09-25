import tempfile
import unittest
from pathlib import Path

from pskit.device import agents
from pskit.device.local import BEGIN, END, Paths, ensure_include, remove_include, ssh_host_block

INFO = {"tailnet_ip": "100.1.2.3", "owner": "alice", "brain_port": 8799}


class DeviceTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.paths = Paths(self._t.name)

    def tearDown(self):
        self._t.cleanup()

    def test_host_block(self):
        text = ssh_host_block("myserver", INFO, Path("/k"), is_mac=True)
        self.assertIn("Host myserver\n", text)
        self.assertIn("Host myserver-tunnel", text)
        self.assertIn("LocalForward 127.0.0.1:8799 127.0.0.1:8799", text)
        self.assertIn("ConnectTimeout 15", text)
        self.assertIn("ExitOnForwardFailure yes", text)
        self.assertIn("StrictHostKeyChecking yes", text)
        self.assertIn("IdentityFile /k/bridge", text)
        self.assertIn("UseKeychain yes", text)
        self.assertNotIn("UseKeychain", ssh_host_block("s", INFO, Path("/k"), is_mac=False))

    def test_include_is_first_and_idempotent(self):
        self.paths.ssh.mkdir()
        cfg = self.paths.ssh / "config"
        cfg.write_text("Host other\n    User bob\n")
        self.assertTrue(ensure_include(self.paths))
        self.assertFalse(ensure_include(self.paths))
        text = cfg.read_text()
        self.assertTrue(text.startswith(BEGIN))
        self.assertIn("Host other", text)
        self.assertEqual(text.count(BEGIN), 1)
        remove_include(self.paths)
        self.assertNotIn(END, cfg.read_text())
        self.assertIn("Host other", cfg.read_text())

    def test_launchd_jobs(self):
        jobs = agents.launchd_jobs(self.paths, "myserver")
        tunnel = jobs["io.pskit.myserver.tunnel"]
        self.assertEqual(tunnel["ProgramArguments"], ["/usr/bin/ssh", "-N", "myserver-tunnel"])
        self.assertTrue(tunnel["KeepAlive"])
        bridge = jobs["io.pskit.myserver.bridge"]
        self.assertEqual(bridge["StartInterval"], 900)
        self.assertIn("bridge-push", bridge["ProgramArguments"])

    def test_systemd_user_units(self):
        units = agents.systemd_units(self.paths, "myserver")
        self.assertIn("Restart=always", units["pskit-tunnel-myserver.service"])
        self.assertIn("OnUnitActiveSec=15min", units["pskit-bridge-myserver.timer"])


if __name__ == "__main__":
    unittest.main()
