from pskit.state import Journal, Ledger, ensure_dir, install_file
from pskit.steps import Engine, Step, StepFailed
from tests.helpers import HostCase


class StateTest(HostCase):
    def test_install_file_lifecycle(self):
        led = Ledger(self.host)
        self.assertEqual(install_file(self.host, led, "/etc/x.conf", "a\n"), "created")
        self.assertEqual(install_file(self.host, led, "/etc/x.conf", "a\n"), "unchanged")
        self.assertEqual(install_file(self.host, led, "/etc/x.conf", "b\n"), "replaced")
        kinds = [e["kind"] for e in led.entries()]
        # A managed file is never backed up again, but every write is recorded.
        self.assertEqual(kinds, ["file_created", "file_written"])
        from pskit.state import last_written_sha
        from pskit.system import sha256_bytes
        self.assertEqual(last_written_sha(led)["/etc/x.conf"], sha256_bytes(b"b\n"))

    def test_lines_in_shared_files(self):
        from pskit.state import add_line, remove_line
        led = Ledger(self.host)
        self.host.write_atomic("/etc/fstab", "UUID=root / ext4 defaults 0 1\n")
        self.assertTrue(add_line(self.host, led, "/etc/fstab", "/swapfile.pskit none swap sw 0 0"))
        self.assertFalse(add_line(self.host, led, "/etc/fstab", "/swapfile.pskit none swap sw 0 0"))
        with open(self.p("/etc/fstab"), "a") as fh:
            fh.write("UUID=data /srv/data ext4 defaults 0 2\n")
        self.assertTrue(remove_line(self.host, "/etc/fstab", "/swapfile.pskit none swap sw 0 0"))
        self.assertEqual(self.p("/etc/fstab").read_text(),
                         "UUID=root / ext4 defaults 0 1\nUUID=data /srv/data ext4 defaults 0 2\n")

    def test_foreign_file_backed_up_once(self):
        self.host.write_atomic("/etc/owner.conf", "original\n")
        led = Ledger(self.host)
        install_file(self.host, led, "/etc/owner.conf", "ours\n")
        install_file(self.host, led, "/etc/owner.conf", "ours v2\n")
        entries = [e for e in led.entries() if e["kind"] == "file_replaced"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(self.host.read_text(entries[0]["backup"]), "original\n")

    def test_ensure_dir_records_once(self):
        led = Ledger(self.host)
        self.assertTrue(ensure_dir(self.host, led, "/srv/a/b", mode=0o750, keep_on_uninstall=True))
        self.assertFalse(ensure_dir(self.host, led, "/srv/a/b", mode=0o750))
        self.assertEqual(led.entries()[0]["keep"], True)
        self.assertEqual(self.p("/srv/a/b").stat().st_mode & 0o7777, 0o750)

    def test_journal_survives_corruption(self):
        self.host.write_atomic("/var/lib/pskit/journal.json", "{broken")
        j = Journal(self.host)
        self.assertIn("recovered_from_corrupt", j.data)
        j.mark("x", "done")
        self.assertEqual(Journal(self.host).status("x"), "done")


class Flag(Step):
    def __init__(self, sid, fail_verify=False, boom=False):
        self.id = sid
        self.title = ""
        self.state = False
        self.applied = 0
        self.fail_verify = fail_verify
        self.boom = boom

    def check(self, ctx):
        return self.state

    def apply(self, ctx):
        self.applied += 1
        if self.boom:
            raise RuntimeError("kaput")
        self.state = not self.fail_verify

    def verify(self, ctx):
        return "still broken" if self.fail_verify else None


class EngineTest(HostCase):
    def test_runs_and_resumes(self):
        ctx = self.ctx()
        a, b = Flag("a"), Flag("b")
        Engine(ctx, [a, b]).run()
        self.assertEqual((a.applied, b.applied), (1, 1))
        Engine(ctx, [a, b]).run()
        self.assertEqual((a.applied, b.applied), (1, 1))
        self.assertEqual(ctx.journal.status("b"), "done")

    def test_reapplies_when_reality_drifted(self):
        ctx = self.ctx()
        a = Flag("a")
        Engine(ctx, [a]).run()
        a.state = False  # someone undid it by hand
        Engine(ctx, [a]).run()
        self.assertEqual(a.applied, 2)

    def test_verify_failure_stops(self):
        ctx = self.ctx()
        a, b = Flag("a", fail_verify=True), Flag("b")
        with self.assertRaises(StepFailed) as cm:
            Engine(ctx, [a, b]).run()
        self.assertEqual(cm.exception.message, "still broken")
        self.assertEqual(b.applied, 0)
        self.assertEqual(ctx.journal.status("a"), "failed")

    def test_exception_wrapped_with_trace_in_journal(self):
        ctx = self.ctx()
        with self.assertRaises(StepFailed) as cm:
            Engine(ctx, [Flag("a", boom=True)]).run()
        self.assertIn("kaput", cm.exception.message)
        self.assertIn("Traceback", ctx.journal.data["steps"]["a"]["detail"])

    def test_skipped_steps(self):
        ctx = self.ctx()

        class Off(Flag):
            def applies(self, ctx):
                return False

        s = Off("off")
        Engine(ctx, [s]).run()
        self.assertEqual(ctx.journal.status("off"), "skipped")
        self.assertEqual(s.applied, 0)
