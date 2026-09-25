import io
import json
import tempfile
import unittest
from pathlib import Path

from pskit import bridge


def mkfile(base: Path, rel: str, data: bytes) -> Path:
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        base = Path(self._t.name)
        self.home = base / "home"
        self.inbox = base / "inbox" / "laptop"
        mkfile(self.home, ".claude/projects/-Users-a-proj/s1.jsonl", b'{"a":1}\n')
        mkfile(self.home, ".claude/projects/-Users-a-proj/notes.txt", b"ignored")
        mkfile(self.home, ".codex/sessions/2026/09/22/rollout-1.jsonl", b"c1\n")
        mkfile(self.home, ".codex/sessions/2026/09/22/other.jsonl", b"no")
        mkfile(self.home, ".grok/sessions/%2FUsers%2Fa%2Fproj/u1/updates.jsonl", b"g1\n")
        mkfile(self.home, ".grok/sessions/%2FUsers%2Fa%2Fproj/u1/x.lock", b"")
        mkfile(self.home, ".grok/sessions/session_search.sqlite", b"db")

    def tearDown(self):
        self._t.cleanup()

    def roundtrip(self, state, budget=bridge.MAX_RUN_CLIENT):
        items = bridge.plan(list(bridge.scan(str(self.home))), state, budget)
        req = io.BytesIO()
        sent = bridge.write_stream(req, items)
        req.seek(0)
        ans = io.BytesIO()
        bridge.receive(req, ans, self.inbox)
        ans.seek(0)
        statuses, done = bridge.read_answers(ans)
        self.assertTrue(done)
        return bridge.update_state(state, sent, statuses), statuses

    def test_scan_filters(self):
        rels = sorted(f.rel for f in bridge.scan(str(self.home)))
        self.assertEqual(rels, [
            "claude/projects/-Users-a-proj/s1.jsonl",
            "codex/sessions/2026/09/22/rollout-1.jsonl",
            "grok/sessions/%2FUsers%2Fa%2Fproj/u1/updates.jsonl",
        ])

    def test_first_run_then_append_only_tail(self):
        state = {}
        n, _ = self.roundtrip(state)
        self.assertEqual(n, 3)
        target = self.inbox / "claude/projects/-Users-a-proj/s1.jsonl"
        self.assertEqual(target.read_bytes(), b'{"a":1}\n')
        self.assertEqual(oct(target.stat().st_mode & 0o777), oct(0o640))
        n, _ = self.roundtrip(state)
        self.assertEqual(n, 0)
        src = self.home / ".claude/projects/-Users-a-proj/s1.jsonl"
        with open(src, "ab") as fh:
            fh.write(b'{"b":2}\n')
        items = bridge.plan(list(bridge.scan(str(self.home))), state)
        self.assertEqual([(f.rel.split("/")[0], off) for f, off in items], [("claude", 8)])
        self.roundtrip(state)
        self.assertEqual(target.read_bytes(), b'{"a":1}\n{"b":2}\n')
        receipt = json.loads((self.inbox / ".pskit-delivery.json").read_text())
        self.assertEqual(receipt["files"], 1)

    def test_server_mismatch_asks_resync(self):
        state = {}
        self.roundtrip(state)
        target = self.inbox / "claude/projects/-Users-a-proj/s1.jsonl"
        target.write_bytes(b"x")  # server copy diverged
        src = self.home / ".claude/projects/-Users-a-proj/s1.jsonl"
        with open(src, "ab") as fh:
            fh.write(b"more\n")
        _, statuses = self.roundtrip(state)
        self.assertEqual(statuses["claude/projects/-Users-a-proj/s1.jsonl"], "resync")
        self.roundtrip(state)
        self.assertEqual(target.read_bytes(), src.read_bytes())

    def test_rewritten_file_sent_whole(self):
        state = {}
        self.roundtrip(state)
        src = self.home / ".codex/sessions/2026/09/22/rollout-1.jsonl"
        src.write_bytes(b"new\n")  # shorter: rewritten
        items = bridge.plan(list(bridge.scan(str(self.home))), state)
        self.assertEqual([off for _, off in items], [0])

    def test_hostile_paths_rejected(self):
        for bad in ("../etc/passwd", "/etc/passwd", "claude/projects/../../x.jsonl", "claude/projects/a/.pskit-x.jsonl",
                    "evil/projects/a/x.jsonl", "claude/other/a/x.jsonl", "codex/sessions/a/notrollout.jsonl",
                    "grok/sessions/plain/u/x.jsonl", "claude/projects/a\\b.jsonl", "claude/projects/x.txt"):
            self.assertIsNone(bridge.safe_rel(bad), bad)
        self.assertEqual(bridge.safe_rel("claude/projects/p/s.jsonl"), "claude/projects/p/s.jsonl")

    def test_receive_refuses_symlinked_dirs(self):
        self.inbox.mkdir(parents=True)
        outside = Path(self._t.name) / "outside"
        outside.mkdir()
        (self.inbox / "claude").symlink_to(outside)
        req = io.BytesIO()
        f = bridge.LocalFile("claude/projects/p/s.jsonl", str(self.home / ".claude/projects/-Users-a-proj/s1.jsonl"),
                             8, 0.0, 1)
        bridge.write_stream(req, [(f, 0)])
        req.seek(0)
        ans = io.BytesIO()
        bridge.receive(req, ans, self.inbox)
        ans.seek(0)
        statuses, _ = bridge.read_answers(ans)
        self.assertEqual(statuses["claude/projects/p/s.jsonl"], "rejected")
        self.assertEqual(list(outside.iterdir()), [])

    def test_bad_protocol(self):
        ans = io.BytesIO()
        res = bridge.receive(io.BytesIO(b'{"v": 99}\n'), ans, self.inbox)
        self.assertEqual(res, {"error": "protocol"})

    def test_budget_splits_runs(self):
        state = {}
        items = bridge.plan(list(bridge.scan(str(self.home))), state, budget=5)
        self.assertEqual(len(items), 1)

    def test_local_delivery(self):
        state = {}
        n = bridge.deliver_local(str(self.home), self.inbox, state)
        self.assertEqual(n, 3)
        self.assertEqual(bridge.deliver_local(str(self.home), self.inbox, state), 0)
        self.assertTrue((self.inbox / ".pskit-delivery.json").exists())


if __name__ == "__main__":
    unittest.main()


class BridgeHardeningTest(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.inbox = Path(self._t.name) / "inbox" / "lap"

    def tearDown(self):
        self._t.cleanup()

    def receive(self, raw: bytes):
        ans = io.BytesIO()
        bridge.receive(io.BytesIO(raw), ans, self.inbox)
        ans.seek(0)
        return bridge.read_answers(ans)

    def test_non_object_and_non_string_headers(self):
        hello = b'{"v": 1}\n'
        statuses, done = self.receive(hello + b'[1, 2]\n')
        self.assertTrue(done)
        statuses, done = self.receive(hello + b'{"path": 5, "offset": 0, "length": 1}\nx{"end": true}\n')
        self.assertTrue(done)
        self.assertEqual(list(statuses.values()), ["rejected"])

    def test_file_count_is_capped(self):
        old = bridge.MAX_FILES_RUN
        bridge.MAX_FILES_RUN = 5
        try:
            lines = [b'{"v": 1}']
            for i in range(20):
                lines.append(json.dumps({"path": f"claude/projects/p/{i}.jsonl", "offset": 0, "length": 0}).encode())
            lines.append(b'{"end": true}')
            statuses, done = self.receive(b"\n".join(lines) + b"\n")
            self.assertTrue(done)
            files = list((self.inbox / "claude/projects/p").iterdir())
            self.assertEqual(len(files), 5)
        finally:
            bridge.MAX_FILES_RUN = old

    def test_local_delivery_streams_large_files(self):
        home = Path(self._t.name) / "home"
        big = home / ".claude/projects/p/big.jsonl"
        big.parent.mkdir(parents=True)
        big.write_bytes(b"x" * (3 * bridge.CHUNK + 17))
        state = {}
        self.assertEqual(bridge.deliver_local(str(home), self.inbox, state), 1)
        self.assertEqual((self.inbox / "claude/projects/p/big.jsonl").stat().st_size, 3 * bridge.CHUNK + 17)
        with open(big, "ab") as fh:
            fh.write(b"tail\n")
        self.assertEqual(bridge.deliver_local(str(home), self.inbox, state), 1)
        self.assertTrue((self.inbox / "claude/projects/p/big.jsonl").read_bytes().endswith(b"xtail\n"))

    def test_shrinking_file_stops_the_stream_cleanly(self):
        home = Path(self._t.name) / "home"
        f = home / ".claude/projects/p/s.jsonl"
        f.parent.mkdir(parents=True)
        f.write_bytes(b"0123456789")
        lf = next(bridge.scan(str(home)))
        f.write_bytes(b"01")  # shrank after the scan
        out = io.BytesIO()
        sent = bridge.write_stream(out, [(lf, 0)])
        self.assertEqual(sent, [])
        out.seek(0)
        statuses, done = self.receive(out.getvalue())
        self.assertTrue(done)
        self.assertFalse((self.inbox / "claude/projects/p/s.jsonl").exists())


class HelloTest(unittest.TestCase):
    def test_bad_hello_lines(self):
        with tempfile.TemporaryDirectory() as d:
            for raw in (b"[1]\n", b"\xff\xfe\n", b""):
                ans = io.BytesIO()
                self.assertEqual(bridge.receive(io.BytesIO(raw), ans, Path(d) / "lap"), {"error": "protocol"})
