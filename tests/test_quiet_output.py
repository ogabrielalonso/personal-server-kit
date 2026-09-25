import io
from contextlib import redirect_stdout
from unittest import mock

from pskit import prove
from pskit.device import pair
from pskit.runtime import backup as BK
from tests.helpers import HostCase


class QuietOutputTest(HostCase):
    def test_backup_functions_do_not_print(self):
        # Found on a real VM: the installer showed raw JSON lines from the
        # first backup and the restore test. The CLI prints them instead.
        self.kit.backup_kind = "later"
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(BK.run_backup(self.host, self.cfg, self.kit), {"skipped": "not configured"})
            self.assertEqual(BK.restore_test(self.host, self.cfg, self.kit), {"skipped": "not configured"})
        self.assertEqual(buf.getvalue(), "")

    def test_proof_status_reads_as_a_sentence(self):
        ok = {"ok": True, "passed": 15, "total": 15, "failed": [], "finished": 1790193572}
        text = prove.status_text(ok)
        self.assertIn("15 of 15", text)
        self.assertNotIn("{", text)
        bad = {"ok": False, "passed": 13, "total": 15, "failed": ["firewall", "tailscale"], "finished": 1790193572}
        self.assertIn("firewall, tailscale", prove.status_text(bad))

    def test_confirmation_is_a_sentence(self):
        self.assertEqual(pair.confirmed_text("confirmed"), "Access through the private network confirmed.")
        self.assertIn("weird", pair.confirmed_text("weird"))

    def test_bridge_push_does_not_print(self):
        from pskit.device import bridge_push
        from pskit.device.local import Paths
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch.object(bridge_push, "save_state", lambda *a: None):
            bridge_push.push(Paths(str(self.root / "home/alice")), "myserver",
                             ssh_cmd=["/nonexistent-ssh-for-test"], timeout=1)
        self.assertEqual(buf.getvalue(), "")
