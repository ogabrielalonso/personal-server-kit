import json
import os
import time

from pskit.checks import receipt
from pskit.runtime import backup as BK
from pskit.runtime import pressure, reaper
from pskit.runtime.reaper import Proc
from tests.helpers import HostCase, fail, ok
from tests.test_manifest import EXAMPLE

USER_SLICE = "0::/user.slice/user-1000.slice/session-3.scope\n"
USER_MGR = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/x.service\n"
SYSTEM = "0::/system.slice/ssh.service\n"


class ReaperTest(HostCase):
    def plan(self, procs, established=False):
        orig = reaper.has_established
        reaper.has_established = lambda host, pid: established
        try:
            return reaper.plan(self.host, 1000, self.kit, procs=procs)
        finally:
            reaper.has_established = orig

    def test_headless_rules(self):
        old = 4 * 3600
        procs = [
            Proc(10, "chrome", ["/opt/chrome", "--headless=new"], old, USER_SLICE, 1000),
            Proc(11, "chrome", ["/opt/chrome", "--window-size=1"], old, USER_SLICE, 1000),
            Proc(12, "headless_shell", ["x"], old, USER_SLICE, 1000),
            Proc(13, "headless_shell", ["x"], 3600, USER_SLICE, 1000),
            Proc(14, "headless_shell", ["x"], old, USER_MGR, 1000),
            Proc(15, "headless_shell", ["x"], old, SYSTEM, 0),
            Proc(16, "bash", ["bash", "-c", "echo headless_shell"], old, USER_SLICE, 1000),
        ]
        self.assertEqual(sorted(v["pid"] for v in self.plan(procs)), [10, 12])

    def test_dev_server_rules(self):
        old = 13 * 3600
        procs = [
            Proc(20, "node", ["node", "node_modules/.bin/vite"], old, USER_SLICE, 1000),
            Proc(21, "node", ["node", "server.js"], old, USER_SLICE, 1000),
            Proc(22, "python3", ["python3", "-m", "http.server"], 3600, USER_SLICE, 1000),
            Proc(23, "ssh", ["ssh", "host", "vite"], old, USER_SLICE, 1000),
        ]
        self.assertEqual([v["pid"] for v in self.plan(procs)], [20])
        self.assertEqual(self.plan(procs, established=True), [])


class PressureTest(HostCase):
    def test_freeze_after_six_bad_then_thaw(self):
        samples = iter([{"cpu": 95, "io": 0, "fork_ms": 900, "listener": True}] * 6 +
                       [{"cpu": 1, "io": 0, "fork_ms": 1, "listener": True}] * 2)
        self.runner.on(["systemctl", "show"], ok("active\n"))
        g = pressure.Guard(self.host, self.cfg, sampler=lambda: next(samples))
        results = [g.cycle() for _ in range(8)]
        self.assertEqual(results[:6], ["bad"] * 5 + ["froze"])
        self.assertTrue(self.runner.called(["systemctl", "freeze", "pskit-brain.slice"]))
        self.assertEqual(results[6:], ["good", "thawed"])
        self.assertTrue(self.runner.called(["systemctl", "thaw", "pskit-ci.slice"]))

    def test_bad_needs_fork_latency(self):
        self.assertFalse(pressure.is_bad({"cpu": 99, "io": 99, "fork_ms": 10, "listener": True}))
        self.assertTrue(pressure.is_bad({"cpu": 99, "io": 0, "fork_ms": 600, "listener": True}))
        self.assertTrue(pressure.is_bad({"cpu": 0, "io": 0, "fork_ms": 0, "listener": False}))

    def test_psi_parse(self):
        self.host.write_atomic("/proc/pressure/cpu", "some avg10=12.50 avg60=1.00 avg300=0.10 total=1\n")
        self.assertEqual(pressure.pressure_avg10(self.host, "cpu", "some"), 12.5)
        self.assertIsNone(pressure.pressure_avg10(self.host, "io", "full"))


class BackupTest(HostCase):
    def setUp(self):
        super().setUp()
        self.kit.backup_kind = "s3"
        for d in ("/srv/myserver/workspace", "/srv/myserver/brain/vault", "/srv/myserver/brain/state",
                  "/etc/pskit/secrets"):
            self.p(d).mkdir(parents=True, exist_ok=True)

    def test_backup_set(self):
        paths, excl = BK.backup_set(self.host, self.cfg, self.kit)
        self.assertIn("/srv/myserver/brain", paths)
        self.host.write_atomic("/etc/pskit/brain-manifest.json", EXAMPLE.read_text())
        paths, excl = BK.backup_set(self.host, self.cfg, self.kit)
        self.assertIn("/srv/myserver/brain/vault", paths)
        self.assertNotIn("/srv/myserver/brain", paths)
        self.assertIn("/srv/myserver/brain/index", excl)
        self.assertIn("/etc/pskit/secrets", excl)
        self.assertIn("/home/alice/.cache", excl)
        self.assertIn("node_modules", excl)

    def test_run_writes_receipt(self):
        summary = json.dumps({"message_type": "summary", "snapshot_id": "abc123", "files_new": 3, "data_added": 99})
        self.runner.on(["restic", "--retry-lock", "30m", "backup"],
                       ok("{\"message_type\":\"status\"}\n" + summary + "\n"))
        rec = BK.run_backup(self.host, self.cfg, self.kit, now=time.time())
        self.assertEqual(rec["snapshot"], "abc123")
        self.assertEqual(receipt(self.host, "backup-success")["snapshot"], "abc123")
        forget = next(c for c in self.runner.calls if c[:4] == ["restic", "--retry-lock", "30m", "forget"])
        self.assertIn("--keep-daily", forget)

    def test_failure_alerts(self):
        self.runner.on(["restic", "--retry-lock", "30m", "backup"], fail(1, "Fatal: unable to open repository"))
        with self.assertRaises(BK.BackupError):
            BK.run_backup(self.host, self.cfg, self.kit)
        self.assertIn("unable to open", receipt(self.host, "backup-failure")["error"])
        self.assertTrue(list(self.p("/var/spool/pskit/notify").iterdir()))

    def test_retry_skips_when_recent(self):
        from pskit.checks import write_receipt
        write_receipt(self.host, "backup-success", {"finished": time.time() - 3600})
        self.assertEqual(BK.run_backup(self.host, self.cfg, self.kit, if_stale_hours=18), {"skipped": "recent"})

    def test_restore_test_compares_hashes(self):
        live = self.p("/srv/myserver/workspace/pages/[id].tsx")
        live.parent.mkdir(parents=True)
        live.write_text("hello")
        old = time.time() - 7200
        os.utime(live, (old, old))
        snap = json.dumps({"struct_type": "snapshot", "time": "2099-01-01T00:00:00.123456789Z", "paths": ["/"]})
        entry = json.dumps({"type": "file", "path": "/srv/myserver/workspace/pages/[id].tsx", "size": 5})
        dumped = []

        def dump(cmd, _input):
            dumped.append(cmd[-1])
            return ok(self._restored)

        self.runner.on(["restic", "--retry-lock", "30m", "ls"], ok(snap + "\n" + entry + "\n"))
        self.runner.on(["restic", "--retry-lock", "30m", "dump"], dump)
        self._restored = "hello"
        self.assertTrue(BK.restore_test(self.host, self.cfg, self.kit)["ok"])
        self.assertEqual(dumped, ["/srv/myserver/workspace/pages/[id].tsx"])  # exact path, no glob
        self._restored = "tampered"
        rec = BK.restore_test(self.host, self.cfg, self.kit)
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["mismatches"], ["/srv/myserver/workspace/pages/[id].tsx"])

    def test_parse_time(self):
        import datetime
        want = datetime.datetime(2026, 9, 22, 1, 15, tzinfo=datetime.timezone.utc).timestamp() + 0.123456
        self.assertAlmostEqual(BK._parse_time("2026-09-22T01:15:00.123456789Z"), want, places=3)
        self.assertAlmostEqual(BK._parse_time("2026-09-22T03:15:00+02:00"), want - 0.123456, places=3)


class PressureLimitsTest(HostCase):
    def test_freeze_is_released_after_30_minutes(self):
        clock = {"t": 0.0}
        self.runner.on(["systemctl", "show"], ok("active\n"))
        g = pressure.Guard(self.host, self.cfg, sampler=lambda: {"cpu": 99, "io": 0, "fork_ms": 900,
                                                                  "listener": True},
                           clock=lambda: clock["t"])
        results = []
        for _ in range(80):  # 40 minutes of bad samples, every 30 s
            results.append(g.cycle())
            clock["t"] += 30
        self.assertEqual(results.count("froze"), 1)
        self.assertEqual(results.count("released"), 1)
        self.assertLess(results.index("froze"), results.index("released"))

    def test_ssh_port_from_config(self):
        from pskit.checks import ssh_ports
        self.runner.on(["sshd", "-T"], ok("port 2200\nport 443\npasswordauthentication no\n"))
        self.assertEqual(ssh_ports(self.host), {2200, 443})
        self.runner.rules.insert(0, (["sshd", "-T"], fail(255, "no")))
        self.assertEqual(ssh_ports(self.host), {22})


class ReaperTokensTest(HostCase):
    def test_substrings_do_not_match(self):
        old = 13 * 3600
        for args in (["node", "/srv/app/invite-bot/worker.js"], ["node", "/x/vitest-utils/daemon.js"],
                     ["python3", "/opt/nodemonitor/agent.py"], ["node", "server.js", "--name", "previte"]):
            self.assertFalse(reaper.is_dev_server(Proc(1, args[0], args, old, USER_SLICE, 1000)), args)
        for args in (["node", "/app/node_modules/.bin/vite"], ["node", "/app/node_modules/.bin/next", "dev"],
                     ["python3", "-m", "http.server", "8000"], ["node", "/usr/bin/nodemon", "app.js"]):
            self.assertTrue(reaper.is_dev_server(Proc(1, args[0], args, old, USER_SLICE, 1000)), args)


class ReaperTitlesTest(HostCase):
    def test_next_title_and_script_paths(self):
        old = 13 * 3600
        for args in (["next-server (v14.2.3)"], ["node", "/app/node_modules/vite/bin/vite.js"],
                     ["node", "/app/node_modules/webpack-dev-server/bin/webpack-dev-server.js"]):
            comm = "next-server (v1" if args[0].startswith("next-server") else "node"
            self.assertTrue(reaper.is_dev_server(Proc(1, comm, args, old, USER_SLICE, 1000)), args)
        self.assertFalse(reaper.is_dev_server(Proc(1, "node", ["node", "/app/invite-bot/worker.js"], old,
                                                   USER_SLICE, 1000)))

    def test_guard_survives_a_failed_alert(self):
        g = pressure.Guard(self.host, self.cfg, sampler=lambda: {})
        orig = pressure.notify.enqueue

        def broken(*a, **k):
            raise FileNotFoundError("spool temp removed")

        pressure.notify.enqueue = broken
        try:
            g._alert("warn", "x")
        finally:
            pressure.notify.enqueue = orig
