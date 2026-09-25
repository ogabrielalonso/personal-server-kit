import socket
from unittest import mock

from pskit import checks
from pskit import setup_backup as SB
from pskit.device import pair
from tests.helpers import HostCase, ok

PONG = "pong from pskit-lab-laptop (100.82.91.112) via DERP(par) in 47ms\n"


class TailnetPolicyTest(HostCase):
    def setUp(self):
        super().setUp()
        self.runner.on(["which", "tailscale"], ok("/usr/bin/tailscale\n"))

    def test_tailnet_addresses(self):
        self.assertTrue(checks.is_tailnet_address("100.82.91.112"))
        self.assertTrue(checks.is_tailnet_address("fd7a:115c:a1e0::1"))
        self.assertFalse(checks.is_tailnet_address("192.168.1.20"))
        self.assertFalse(checks.is_tailnet_address("myserver"))

    def test_magicdns_names_resolve_to_the_tailnet_address(self):
        # Found in review: a MagicDNS destination lost the access rules hint.
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.82.91.112", 0))]
        with mock.patch.object(checks.socket, "getaddrinfo", lambda *a, **k: fake):
            self.assertEqual(checks.tailnet_address_of("laptop.tail1234.ts.net"), "100.82.91.112")
        other = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        with mock.patch.object(checks.socket, "getaddrinfo", lambda *a, **k: other):
            self.assertEqual(checks.tailnet_address_of("example.org"), "")
        self.assertEqual(checks.tailnet_address_of("100.82.91.112"), "100.82.91.112")

    def test_pong_without_tcp_means_blocked(self):
        # The signature measured on a real run: tailscale ping answers via
        # DERP, TCP to the same address times out (the rules had no entry
        # for the new nodes).
        self.runner.on(["/usr/bin/tailscale", "ping"], ok(PONG))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed = s.getsockname()[1]
        self.assertTrue(checks.tailnet_policy_blocks(self.host, "127.0.0.1", closed, timeout_s=1))

    def test_pong_with_tcp_is_not_blocked(self):
        self.runner.on(["/usr/bin/tailscale", "ping"], ok(PONG))
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            self.assertFalse(checks.tailnet_policy_blocks(self.host, "127.0.0.1", s.getsockname()[1], timeout_s=1))

    def test_no_pong_is_not_a_policy_problem(self):
        self.runner.on(["/usr/bin/tailscale", "ping"], ok("no reply\n"))
        self.assertFalse(checks.tailnet_policy_blocks(self.host, "100.82.91.112", 47800, timeout_s=1))

    def test_pairing_names_the_access_rules(self):
        self.runner.on(["/usr/bin/tailscale", "status"], ok('{"Self": {"TailscaleIPs": ["100.82.91.112"]}}'))
        original = pair.Unreachable("unreachable")
        with mock.patch.object(pair, "tailnet_policy_blocks", lambda *a, **k: True):
            err = pair.diagnose_unreachable(self.host, "100.125.250.40", 47800, original)
        self.assertIn("Access controls", str(err))
        self.assertIn("100.82.91.112", str(err))
        self.assertIn("100.125.250.40", str(err))
        with mock.patch.object(pair, "tailnet_policy_blocks", lambda *a, **k: False):
            self.assertIs(pair.diagnose_unreachable(self.host, "100.125.250.40", 47800, original), original)

    def test_backup_hint_names_the_access_rules(self):
        self.kit.tailnet_ip = "100.125.250.40"
        self.host.write_atomic("/etc/pskit/secrets/restic.env",
                               "RESTIC_REPOSITORY=sftp:dev@100.82.91.112:/home/dev/pskit-backup\n", mode=0o600)
        with mock.patch.object(SB, "tailnet_policy_blocks", lambda *a, **k: True):
            self.assertIn("Access controls", SB.repo_hint(self.ctx()))
        with mock.patch.object(SB, "tailnet_policy_blocks", lambda *a, **k: False):
            self.assertEqual(SB.repo_hint(self.ctx()), SB.t("backup.repo_hint"))
