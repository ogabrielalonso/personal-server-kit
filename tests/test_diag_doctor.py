import json
import os
import unittest
from unittest import mock

from pskit import diagnostics, doctor
from tests.helpers import HostCase, ok


class RedactTest(unittest.TestCase):
    def test_secrets_redacted(self):
        text = ("token 1234567890:AAH-abcdefghijklmnopqrstuvwxyz012345 "
                "url https://api.telegram.org/bot1234567890:AAHabcdefghijklmnopqrstuvwxyz0123456/sendMessage "
                "AKIAABCDEFGHIJKLMNOP password=hunter2 secret: s3cr3t "
                "https://hc-ping.com/0b1c2d3e-4f5a-6b7c-8d9e-0f1a2b3c4d5e pskit-myserver-0123456789abcdef "
                "tskey-auth-kAbC123 mail owner@example.com")
        out = diagnostics.redact(text)
        for leaked in ("AAH-abcdef", "AKIAABCDEFGHIJKLMNOP", "hunter2", "s3cr3t", "0b1c2d3e", "0123456789abcdef",
                       "tskey-auth", "owner@example.com"):
            self.assertNotIn(leaked, out)

    def test_ip_masking(self):
        out = diagnostics.redact("from 203.0.113.9 via 100.101.102.103 lan 192.168.1.4 lo 127.0.0.1")
        self.assertIn("203.x.x.x", out)
        self.assertIn("100.101.102.103", out)
        self.assertIn("192.168.1.4", out)
        self.assertIn("127.0.0.1", out)

    def test_private_key_block(self):
        out = diagnostics.redact("-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n-----END OPENSSH PRIVATE KEY-----")
        self.assertEqual(out, "<private-key>")


class DiagnosticFileTest(HostCase):
    def test_file_has_no_secret_values(self):
        self.host.write_atomic("/etc/pskit/secrets/notify.env", 'TELEGRAM_BOT_TOKEN="1234567:' + "x" * 35 + '"\n')
        self.host.write_atomic("/var/lib/pskit/journal.json",
                               '{"steps": {"alerts": {"status": "failed", "detail": "password=abc"}}, "runs": []}')
        self.runner.on(["journalctl"], ok("Sep 22 pskit-health: sent to bot1234567:" + "y" * 35 + "\n"))
        path = diagnostics.write(self.host, self.cfg, self.kit)
        text = path.read_text()
        self.assertNotIn("x" * 35, text)
        self.assertNotIn("y" * 35, text)
        self.assertNotIn("password=abc", text)
        self.assertIn("notify.env", text)  # presence only
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_nothing_installed_goes_to_invoking_user_home(self):
        # local-only machine: no host.json (so no owner) and no state folder
        self.assertFalse(self.p("/var/lib/pskit").exists())
        with mock.patch.dict(os.environ, {"SUDO_USER": "alice"}):
            path = diagnostics.write(self.host, None, None)
        self.assertEqual(path.parent, self.p("/home/alice"))
        self.assertEqual(json.loads(path.read_text())["kind"], "pskit-diagnostic")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_nothing_installed_and_no_home_goes_to_current_folder(self):
        here = self.p("/work")
        here.mkdir()
        old = os.getcwd()
        os.chdir(here)
        try:
            with mock.patch.dict(os.environ, {"SUDO_USER": "nobody-here"}):
                path = diagnostics.write(self.host, None, None)
        finally:
            os.chdir(old)
        self.assertEqual(path.parent.resolve(), here.resolve())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class AuditTest(HostCase):
    def test_slices_count_top_level_only(self):
        units = "lab.slice loaded\nlab-brain.slice loaded\nlab-ci.slice loaded\nuser-1000.slice loaded\n"
        self.runner.on(["systemctl", "list-units", "--type=slice"], ok(units))
        caps = {"lab.slice": 14, "lab-brain.slice": 10, "lab-ci.slice": 4, "user-1000.slice": 14}

        def show(cmd, _):
            return ok(str(caps[cmd[-1]] * 1024 ** 3) + "\n")

        self.runner.on(["systemctl", "show"], show)
        item = doctor.audit_slices(self.host, 32 * 1024 ** 3)[0]
        self.assertEqual(item.level, "ok")
        self.assertIn("28.0 GiB", item.text)
        self.assertNotIn("lab-brain", item.detail)

    def test_no_caps_warns(self):
        self.runner.on(["systemctl", "list-units", "--type=slice"], ok("user.slice loaded\n"))
        self.runner.on(["systemctl", "show"], ok("infinity\n"))
        self.assertEqual(doctor.audit_slices(self.host, 8 * 1024 ** 3)[0].level, "warn")

    def test_hex_ip(self):
        self.assertEqual(doctor._hex_ip("0100007F"), "127.0.0.1")
        self.assertEqual(doctor._hex_ip("67666564"), "100.101.102.103")
        self.assertEqual(doctor._hex_ip("00000000000000000000000001000000"), "::1")

    def test_public_listener_detected(self):
        tcp = ("  sl local rem st\n"
               "   0: 67666564:0016 00000000:0000 0A 0 0 0 0 0 1 1\n"
               "   1: 00000000:01BB 00000000:0000 0A 0 0 0 0 0 2 1\n")
        self.host.write_atomic("/proc/net/tcp", tcp)
        item = doctor.audit_listeners(self.host)[0]
        self.assertIn("0.0.0.0:443", item.detail)
        self.assertNotIn("100.101.102.103", item.detail)


if __name__ == "__main__":
    unittest.main()


class Ipv6RedactTest(unittest.TestCase):
    def test_public_v6_masked_private_kept(self):
        out = diagnostics.redact("from 2a01:4f8:c17:1234::1 port 22, tailnet fd7a:115c:a1e0::5, lo ::1, "
                                 "link fe80::1, time 12:30:45")
        self.assertNotIn("2a01:4f8:c17:1234::1", out)
        self.assertIn("2a01:x:x::x", out)
        self.assertIn("fd7a:115c:a1e0::5", out)
        self.assertIn("fe80::1", out)
        self.assertIn("12:30:45", out)
