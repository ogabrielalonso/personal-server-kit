
from pskit import lockdown as L
from pskit.state import Ledger
from tests.helpers import HostCase, fail, ok


class LockdownTest(HostCase):
    def setUp(self):
        super().setUp()
        self.host.write_atomic("/etc/ufw/user.rules", "ORIGINAL RULES\n")
        self.host.write_atomic("/etc/ufw/ufw.conf", "ENABLED=no\n")
        self.runner.on(["ufw", "status"], ok("Status: inactive\n"))

    def test_rule_classification(self):
        self.assertTrue(L.ssh_rule("ufw allow 22/tcp"))
        self.assertTrue(L.ssh_rule("ufw allow OpenSSH"))
        self.assertTrue(L.ssh_rule("ufw limit ssh"))
        self.assertFalse(L.ssh_rule("ufw allow 2222/tcp"))
        self.assertFalse(L.ssh_rule("ufw allow 443/tcp"))
        self.assertFalse(L.ssh_rule("ufw allow in on tailscale0"))

    def test_tailnet_origin(self):
        self.assertTrue(L.from_tailnet("100.101.1.2 5555 100.64.0.1 22"))
        self.assertTrue(L.from_tailnet("fd7a:115c:a1e0::1 5555 x 22"))
        self.assertFalse(L.from_tailnet("203.0.113.9 5555 1.2.3.4 22"))
        self.assertFalse(L.from_tailnet(""))

    def test_revert_timer_counts_from_arming_not_from_boot(self):
        # A boot-relative trigger is already in the past on a server that has been up for
        # more than ten minutes, and systemd fires such a timer at once: the lockdown was
        # undone seconds after it was applied (found on a real VM, 23 Sep 2026). The timer
        # is enabled, so OnActiveSec also restarts the ten minutes at boot.
        L.apply(self.host, Ledger(self.host), self.cfg, self.kit, keep_rules=[])
        timer = self.p("/etc/systemd/system/pskit-lockdown-revert.timer").read_text()
        self.assertIn("OnActiveSec=600s", timer)
        self.assertNotIn("OnBootSec", timer)
        self.assertNotIn("OnStartupSec", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_apply_confirm_finalize(self):
        pending = L.apply(self.host, Ledger(self.host), self.cfg, self.kit, keep_rules=["ufw allow 443/tcp",
                                                                                         "ufw allow 22/tcp"])
        cmds = [c for c in self.runner.calls if c[0] == "ufw"]
        self.assertIn(["ufw", "--force", "reset"], cmds)
        self.assertIn(["ufw", "allow", "in", "on", "tailscale0"], cmds)
        self.assertIn(["ufw", "allow", "443/tcp"], cmds)
        self.assertNotIn(["ufw", "allow", "22/tcp"], cmds)
        self.assertEqual(cmds[-1], ["ufw", "--force", "enable"])
        dropin = self.p(L.SSHD_DROPIN).read_text()
        self.assertIn("PasswordAuthentication no", dropin)
        self.assertIn("AllowUsers alice", dropin)
        self.assertTrue(self.runner.called(["systemctl", "enable", "--now", "pskit-lockdown-revert.timer"]))
        self.assertIn("OnActiveSec=600s", self.p("/etc/systemd/system/pskit-lockdown-revert.timer").read_text())
        with self.assertRaises(L.LockdownError):
            L.confirm(self.host, "203.0.113.9 1 2 22")
        self.assertEqual(L.confirm(self.host, "100.90.1.1 1 2 22"), "confirmed")
        self.assertTrue(L.finalize(self.host))
        self.assertTrue(L.is_applied(self.host))
        self.assertIsNone(L.read_pending(self.host))
        self.assertEqual(L.confirm(self.host, ""), "already-applied")
        self.assertEqual(pending["deadline"] - pending["armed_at"], 600)

    def test_revert_restores_everything(self):
        L.apply(self.host, Ledger(self.host), self.cfg, self.kit, [])
        self.host.write_atomic("/etc/ufw/user.rules", "NEW RULES\n")
        self.assertEqual(L.revert_if_due(self.host, self.cfg, now=0), "not-due")
        self.assertEqual(L.revert_if_due(self.host, self.cfg, now=float("inf")), "reverted")
        self.assertEqual(self.p("/etc/ufw/user.rules").read_text(), "ORIGINAL RULES\n")
        self.assertFalse(self.p(L.SSHD_DROPIN).exists())
        self.assertTrue(self.runner.called(["ufw", "--force", "disable"]))
        self.assertIsNone(L.read_pending(self.host))
        self.assertTrue(list(self.p("/var/spool/pskit/notify").iterdir()))

    def test_revert_prefers_late_confirmation(self):
        L.apply(self.host, Ledger(self.host), self.cfg, self.kit, [])
        L.confirm(self.host, "100.90.1.1 1 2 22")
        self.assertEqual(L.revert_if_due(self.host, self.cfg, now=float("inf")), "finalized")
        self.assertTrue(L.is_applied(self.host))

    def test_invalid_sshd_config_rolls_back(self):
        self.runner.rules.insert(0, (["sshd", "-t"], fail(255, "bad option")))
        with self.assertRaises(L.LockdownError):
            L.apply(self.host, Ledger(self.host), self.cfg, self.kit, [])
        self.assertFalse(self.p(L.SSHD_DROPIN).exists())
        self.assertFalse(self.runner.called(["ufw", "--force", "enable"]))

    def test_firewall_failure_rolls_back(self):
        self.runner.rules.insert(0, (["ufw", "--force", "enable"], fail(1)))
        with self.assertRaises(L.LockdownError):
            L.apply(self.host, Ledger(self.host), self.cfg, self.kit, [])
        self.assertIsNone(L.read_pending(self.host))


class PrivsepTest(HostCase):
    def test_privsep_dir_created_before_validation(self):
        order = []
        self.runner.on(["ufw", "status"], ok("Status: inactive\n"))

        def sshd_t(cmd, _):
            order.append(self.p("/run/sshd").is_dir())
            return ok()

        self.runner.on(["sshd", "-t"], sshd_t)
        L.apply(self.host, Ledger(self.host), self.cfg, self.kit, [])
        self.assertEqual(order, [True])

    def test_tmpfiles_recreates_it_at_boot(self):
        from pskit.linux.steps_base import Tmpfiles
        self.assertIn("d /run/sshd 0755 root root -", Tmpfiles().files(self.ctx())["/etc/tmpfiles.d/pskit.conf"])


class LockdownHardeningTest(HostCase):
    def setUp(self):
        super().setUp()
        self.runner.on(["ufw", "status"], ok("Status: inactive\n"))
        self.host.write_atomic("/etc/ufw/user.rules", "ORIGINAL\n")

    def test_arm_failure_changes_nothing(self):
        self.runner.rules.insert(0, (["systemctl", "enable", "--now", "pskit-lockdown-revert.timer"], fail(1)))
        with self.assertRaises(L.LockdownError):
            L.apply(self.host, Ledger(self.host), self.cfg, self.kit, [])
        self.assertIsNone(L.read_pending(self.host))
        self.assertFalse(self.runner.called(["ufw", "--force", "reset"]))
        self.assertFalse(self.p(L.SSHD_DROPIN).exists())

    def test_unexpected_error_restores_and_disarms(self):
        def boom(cmd, _):
            raise OSError("disk full")

        self.runner.rules.insert(0, (["ufw", "default", "deny", "incoming"], boom))
        with self.assertRaises(L.LockdownError):
            L.apply(self.host, Ledger(self.host), self.cfg, self.kit, [])
        self.assertIsNone(L.read_pending(self.host))
        self.assertTrue(self.runner.called(["systemctl", "disable", "--now", "pskit-lockdown-revert.timer"]))
        self.assertFalse(self.p(L.SSHD_DROPIN).exists())
        self.assertEqual(self.p("/etc/ufw/user.rules").read_text(), "ORIGINAL\n")

    def test_quoted_rules_kept_whole(self):
        L.apply(self.host, Ledger(self.host), self.cfg, self.kit,
                ["ufw allow 'Nginx Full'", "ufw allow 443/tcp comment 'web site'"])
        self.assertIn(["ufw", "allow", "Nginx Full"], self.runner.calls)
        self.assertIn(["ufw", "allow", "443/tcp", "comment", "web site"], self.runner.calls)

    def test_port_lists_and_ranges(self):
        self.assertTrue(L.ssh_rule("ufw allow proto tcp from any to any port 22,80"))
        self.assertTrue(L.ssh_rule("ufw allow 20:25/tcp"))
        self.assertTrue(L.ssh_rule("ufw allow 22"))
        self.assertFalse(L.ssh_rule("ufw allow 2222/tcp"))
        self.assertFalse(L.ssh_rule("ufw allow 'Nginx Full'"))
        self.assertFalse(L.ssh_rule("ufw allow proto tcp from any to any port 80,443"))

    def test_timer_reverts_even_if_clock_went_back(self):
        L.apply(self.host, Ledger(self.host), self.cfg, self.kit, [])
        self.assertEqual(L.revert_if_due(self.host, self.cfg, now=0), "not-due")
        self.assertEqual(L.revert_if_due(self.host, self.cfg, now=0, from_timer=True), "reverted")


class VerifierFindingsTest(HostCase):
    def test_logged_short_syntax_is_ssh(self):
        self.assertTrue(L.ssh_rule("ufw allow log 22/tcp"))
        self.assertTrue(L.ssh_rule("ufw limit log-all 22/tcp"))
        self.assertTrue(L.ssh_rule("ufw allow in log 22"))
        self.assertFalse(L.ssh_rule("ufw allow log 443/tcp"))

    def test_confirmation_waits_until_changes_are_applied(self):
        self.runner.on(["ufw", "status"], ok("Status: inactive\n"))
        seen = []

        def sshd_t(cmd, _):
            # A laptop polling right now, while the changes are being applied.
            seen.append(L.confirm(self.host, "100.90.1.1 1 2 22"))
            return ok()

        self.runner.on(["sshd", "-t"], sshd_t)
        L.apply(self.host, Ledger(self.host), self.cfg, self.kit, [])
        self.assertEqual(seen, ["not-ready"])
        self.assertEqual(L.confirm(self.host, "100.90.1.1 1 2 22"), "confirmed")

    def test_interrupt_is_not_wrapped(self):
        self.runner.on(["ufw", "status"], ok("Status: inactive\n"))

        def interrupted(cmd, _):
            raise KeyboardInterrupt

        self.runner.on(["ufw", "--force", "reset"], interrupted)
        with self.assertRaises(KeyboardInterrupt):
            L.apply(self.host, Ledger(self.host), self.cfg, self.kit, [])
        self.assertIsNone(L.read_pending(self.host))

    def test_stream_local_forwarding_off(self):
        from pskit.render import render
        self.assertIn("AllowStreamLocalForwarding no", render("misc/sshd-hardening.conf", {"owner": "a"}))
