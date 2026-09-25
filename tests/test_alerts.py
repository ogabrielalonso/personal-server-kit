import unittest

from pskit.alerts import AlertState, Finding, graded, graded_low
from tests.helpers import HostCase


class GradedTest(unittest.TestCase):
    def test_enter_and_leave_with_margin(self):
        self.assertEqual(graded(79, "ok", 80, 90, 5), "ok")
        self.assertEqual(graded(80, "ok", 80, 90, 5), "warn")
        self.assertEqual(graded(77, "warn", 80, 90, 5), "warn")
        self.assertEqual(graded(74, "warn", 80, 90, 5), "ok")
        self.assertEqual(graded(90, "warn", 80, 90, 5), "crit")
        self.assertEqual(graded(86, "crit", 80, 90, 5), "crit")
        self.assertEqual(graded(84, "crit", 80, 90, 5), "warn")

    def test_low_is_worse(self):
        self.assertEqual(graded_low(50, "ok", 10, 5, 3), "ok")
        self.assertEqual(graded_low(9, "ok", 10, 5, 3), "warn")
        self.assertEqual(graded_low(12, "warn", 10, 5, 3), "warn")
        self.assertEqual(graded_low(14, "warn", 10, 5, 3), "ok")
        self.assertEqual(graded_low(4, "warn", 10, 5, 3), "crit")


class AlertStateTest(HostCase):
    def test_oscillation_does_not_repeat(self):
        st = AlertState(self.host)
        sent = 0
        t0 = 1_000_000.0
        for i, pct in enumerate([79, 81, 79, 80, 78, 81, 79, 80]):
            level = graded(pct, st.previous_level("disk:/"), 80, 90, 5)
            out = st.process([Finding("disk:/", level, f"{pct}%")], t0 + i * 300)
            sent += int(out.urgent)
        self.assertEqual(sent, 1)

    def test_lifecycle(self):
        st = AlertState(self.host)
        out = st.process([Finding("a", "warn", "A")], 0)
        self.assertEqual([f.key for f in out.new], ["a"])
        out = st.process([Finding("a", "warn", "A")], 3600)
        self.assertFalse(out.urgent)
        out = st.process([Finding("a", "crit", "A!")], 7200)
        self.assertEqual([f.key for f in out.changed], ["a"])
        self.assertEqual(out.severity(), "critical")
        out = st.process([Finding("a", "crit", "A!")], 7200 + 25 * 3600)
        self.assertEqual([f.key for f in out.reminders], ["a"])
        out = st.process([Finding("a", "ok", "")], 7200 + 26 * 3600)
        self.assertEqual(out.cleared[0][0], "a")

    def test_info_goes_to_digest_once_a_day(self):
        st = AlertState(self.host)
        self.assertEqual(len(st.process([Finding("i", "info", "x")], 0).digest), 1)
        self.assertEqual(len(st.process([Finding("i", "info", "x")], 3600).digest), 0)
        self.assertEqual(len(st.process([Finding("i", "info", "x")], 21 * 3600).digest), 1)

    def test_crashed_probe_keeps_state(self):
        st = AlertState(self.host)
        st.process([Finding("disk:/", "warn", "x")], 0)
        out = st.process([], 60, preserve_prefixes=("disk:",))
        self.assertFalse(out.cleared)
        out = st.process([], 120)
        self.assertEqual(out.cleared[0][0], "disk:/")

    def test_persists(self):
        st = AlertState(self.host)
        st.process([Finding("a", "warn", "A")], 0)
        st.save()
        self.assertEqual(AlertState(self.host).previous_level("a"), "warn")


if __name__ == "__main__":
    unittest.main()
