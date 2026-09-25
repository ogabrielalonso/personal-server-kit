import json

from pskit import brainslot
from pskit import manifest as mf
from pskit.linux import steps_base as b
from pskit.linux import steps_ops as o
from pskit.state import Ledger
from tests.helpers import HostCase, ok
from tests.test_manifest import EXAMPLE


class LinuxStepsTest(HostCase):
    def test_memory_budget_files(self):
        ctx = self.ctx()
        bud, files = b.MemoryBudget().files(ctx)
        self.assertIn("MemoryMax=10G", files["/etc/systemd/system/pskit-brain.slice"])
        self.assertIn("MemoryLow=4G", files["/etc/systemd/system/pskit-brain.slice"])
        user = files["/etc/systemd/system/user-1000.slice.d/50-pskit.conf"]
        self.assertIn("MemoryMax=18688M", user)
        self.assertNotIn("/etc/systemd/system/pskit-ci.slice", files)
        self.kit.modules = ["ci"]
        _, files = b.MemoryBudget().files(ctx)
        self.assertIn("MemoryMax=14592M", files["/etc/systemd/system/user-1000.slice.d/50-pskit.conf"])
        self.assertIn("MemoryMax=4G", files["/etc/systemd/system/pskit-ci.slice"])

    def test_budget_follows_manifest_peak(self):
        self.host.write_atomic("/etc/pskit/brain-manifest.json", EXAMPLE.read_text())
        _, files = b.MemoryBudget().files(self.ctx())
        self.assertIn("MemoryMax=6400M", files["/etc/systemd/system/pskit-brain.slice"])

    def test_memory_budget_apply(self):
        ctx = self.ctx()
        step = b.MemoryBudget()
        self.assertFalse(step.check(ctx))
        step.apply(ctx)
        self.assertTrue(step.check(ctx))
        self.assertTrue(self.runner.called(["systemctl", "daemon-reload"]))
        self.assertEqual(json.loads(self.p("/etc/pskit/host.json").read_text())["memory_budget"]["brain_max"], 10.0)

    def test_storage_modes(self):
        ctx = self.ctx()
        step = b.Storage()
        step.apply(ctx)
        self.assertTrue(step.check(ctx))
        self.assertEqual(self.p("/srv/myserver/brain").stat().st_mode & 0o7777, 0o2770)
        self.assertEqual(self.p("/var/spool/pskit/notify").stat().st_mode & 0o7777, 0o3770)
        self.assertEqual(self.p("/etc/pskit/secrets").stat().st_mode & 0o7777, 0o700)
        keep = {e["path"]: e["keep"] for e in Ledger(self.host).entries()}
        self.assertTrue(keep["/srv/myserver/brain"])
        self.assertFalse(keep["/etc/pskit"])

    def test_tmpfiles_content(self):
        files = b.Tmpfiles().files(self.ctx({"agent_tmp_patterns": ["/tmp/myagent-*"]}))
        self.assertIn("D /tmp 1777 root root 10d", files["/etc/tmpfiles.d/tmp.conf"])
        pk = files["/etc/tmpfiles.d/pskit.conf"]
        self.assertIn("f /run/pskit/heavy.lock 0664 root pskit -", pk)
        self.assertIn("e /tmp/myagent-* - - - 2d", pk)
        self.assertIn("d /srv/myserver/workspace/scratch 0750 alice alice 14d", pk)

    def test_runtime_units_render_and_selection(self):
        ctx = self.ctx()
        units = o.runtime_units(ctx)
        self.assertIn("ExecStart=/usr/local/bin/pskit health", units["pskit-health.service"])
        self.assertIn("User=alice", units["pskit-cache.service"])
        self.assertIn("DirectoryNotEmpty=/var/spool/pskit/notify", units["pskit-notify.path"])
        on, off = o.wanted_active(ctx)
        self.assertIn("pskit-backup.timer", off)
        self.kit.backup_kind = "s3"
        self.kit.heartbeat_enabled = True
        on, off = o.wanted_active(ctx)
        self.assertIn("pskit-backup.timer", on)
        self.assertIn("pskit-heartbeat.timer", on)

    def test_brain_units(self):
        man = mf.load(EXAMPLE.read_text())
        units = brainslot.linux_units(man, self.cfg)
        self.assertEqual(sorted(units), ["pskit-brain-capture.service", "pskit-brain-capture.timer",
                                         "pskit-brain-query.service", "pskit-brain-weekly.service",
                                         "pskit-brain-weekly.timer"])
        q = units["pskit-brain-query.service"]
        self.assertIn("Type=simple", q)
        self.assertIn("Restart=always", q)
        self.assertIn("Slice=pskit-brain.slice", q)
        self.assertIn("User=pskit-brain", q)
        self.assertIn("WantedBy=multi-user.target", q)
        self.assertIn('Environment=BRAIN_LOG_LEVEL=info', q)
        c = units["pskit-brain-capture.service"]
        self.assertIn("ExecStart=/usr/bin/flock /run/pskit/heavy.lock /srv/myserver/brain/.venv/bin/python", c)
        self.assertNotIn("WantedBy=", c)
        self.assertIn("TimeoutStartSec=180min", c)
        self.assertIn("OnCalendar=*-*-* 05:00:00", units["pskit-brain-capture.timer"])

    def test_brain_apply_removes_old_units(self):
        self.runner.on(["systemctl"], ok())
        rec = brainslot.apply_linux(self.host, Ledger(self.host), self.cfg, EXAMPLE.read_text())
        self.assertIn("pskit-brain-query.service", rec["expect_active"])
        data = json.loads(EXAMPLE.read_text())
        data["services"] = data["services"][:1]
        brainslot.apply_linux(self.host, Ledger(self.host), self.cfg, json.dumps(data))
        self.assertTrue(self.runner.called(["systemctl", "disable", "--now", "pskit-brain-capture.timer"]))
        self.assertFalse(self.p("/etc/systemd/system/pskit-brain-weekly.service").exists())


class ProofBudgetTest(LinuxStepsTest):
    def test_after_boot_always_reports_within_budget(self):
        import time as _time
        from pskit import prove
        clock = {"t": 0.0}

        def slow_failing_checks(*a, **k):
            clock["t"] += 30  # one round of checks, health wait included
            return [("brain-health", False, "down")]

        def fake_sleep(s):
            clock["t"] += s

        orig, real = prove.checks, _time.monotonic
        prove.checks = slow_failing_checks
        _time.monotonic = lambda: clock["t"]
        sent = []
        try:
            rec = prove.after_boot(self.host, self.cfg, self.kit, retries=100, wait_s=20, sleep=fake_sleep,
                                   send=lambda text, sev: sent.append(text), budget_s=420)
        finally:
            prove.checks, _time.monotonic = orig, real
        self.assertFalse(rec["ok"])
        self.assertLessEqual(clock["t"], 420 + 30)  # the budget plus at most one round
        self.assertEqual(len(sent), 1)
        self.assertTrue(self.p("/var/lib/pskit/receipts/proof.json").exists())
