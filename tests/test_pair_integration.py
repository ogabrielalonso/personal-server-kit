"""Real pairing over a loopback socket: server session with a fake root,
laptop flow with a fake HOME. No system change, no SSH."""

import json
import tempfile
import threading
from pathlib import Path

from pskit.device import pair
from pskit.device.local import Paths
from pskit.server import pairing
from pskit.state import Ledger
from pskit.ui import UI
from tests.helpers import HostCase, NullOut

ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"


def free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class PairIntegrationTest(HostCase):
    def test_full_pairing_over_http(self):
        self.kit.tailnet_ip = "127.0.0.1"
        self.kit.tailnet_name = "myserver.example.ts.net"
        self.kit.pair_port = free_port()
        self.host.write_atomic("/etc/ssh/ssh_host_ed25519_key.pub", ED + " root@myserver\n")
        session = pairing.PairingSession(self.host, Ledger(self.host), self.cfg, self.kit, "WXYZ-6789")
        th = threading.Thread(target=pairing.serve, args=(session, "127.0.0.1", self.kit.pair_port, 0.1))
        th.start()
        home = tempfile.mkdtemp()
        answers = {"tailscale": "skip", "server_address": "127.0.0.1", "pair_port": self.kit.pair_port,
                   "device_name": "alice-lap", "pair_code": "wxyz6789", "confirm_lockdown": False,
                   "install_agents": False, "first_push": False}
        try:
            record = pair.run(UI(answers, interactive=False, out=NullOut()), self.host, answers, home=home)
        finally:
            th.join(timeout=10)
        self.assertEqual(record["server"], "myserver")
        paths = Paths(home)
        kd = paths.server_dir("myserver")
        self.assertEqual(kd.stat().st_mode & 0o777, 0o700)
        for name in ("id_ed25519", "tunnel", "bridge"):
            self.assertEqual((kd / name).stat().st_mode & 0o777, 0o600, name)
        known = (kd / "known_hosts").read_text()
        self.assertIn("127.0.0.1,myserver.example.ts.net ssh-ed25519", known)
        cfg = (kd / "ssh_config").read_text()
        self.assertIn("Host myserver-bridge", cfg)
        self.assertIn(f"UserKnownHostsFile {kd}/known_hosts", cfg)
        self.assertIn("Include", (Path(home) / ".ssh" / "config").read_text())
        ak = self.p("/home/alice/.ssh/authorized_keys").read_text()
        self.assertEqual(ak.count("pskit:alice-lap:"), 3)
        tunnel_pub = (kd / "tunnel.pub").read_text().split()[1]
        tunnel_line = next(ln for ln in ak.splitlines() if ln.endswith("pskit:alice-lap:tunnel"))
        self.assertIn(tunnel_pub, tunnel_line)
        self.assertTrue(tunnel_line.startswith("restrict,port-forwarding,permitopen="))
        saved = json.loads(paths.server_file("myserver").read_text())
        self.assertEqual(saved["device"], "alice-lap")
        self.assertTrue((paths.share / "current" / "pskit" / "__init__.py").exists())
        self.assertFalse(list(paths.ssh_kit.glob(".staging-*")))
