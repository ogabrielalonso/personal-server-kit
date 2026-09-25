import json
import unittest
from pathlib import Path

from pskit import manifest as mf

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "brain-manifest.json"


def base():
    return json.loads(EXAMPLE.read_text())


class ManifestTest(unittest.TestCase):
    def test_example_is_valid(self):
        m = mf.load(EXAMPLE.read_text())
        self.assertEqual(m.name, "example-brain")
        self.assertEqual([s.name for s in m.services], ["query", "capture", "weekly"])
        self.assertEqual(m.peak_bytes, (2048 + 3072) * 1024 * 1024)
        self.assertTrue(m.services[1].heavy_lock)

    def problems(self, data):
        with self.assertRaises(mf.ManifestError) as cm:
            mf.parse(data)
        return " | ".join(cm.exception.problems)

    def test_relative_command_rejected(self):
        d = base()
        d["services"][0]["command"] = ["python", "-m", "x"]
        self.assertIn("absolute path", self.problems(d))

    def test_health_must_be_loopback(self):
        d = base()
        d["health"]["url"] = "http://0.0.0.0:8799/health"
        self.assertIn("loopback", self.problems(d))

    def test_backup_paths_inside_home(self):
        d = base()
        d["backup"]["include"] = ["../etc"]
        self.assertIn("backup.include", self.problems(d))
        d = base()
        d["backup"]["exclude"] = ["/abs"]
        self.assertIn("backup.exclude", self.problems(d))

    def test_schedule_rules(self):
        d = base()
        d["services"][1]["schedule"] = {"every_minutes": 1}
        self.assertIn("every_minutes", self.problems(d))
        d = base()
        d["services"][1]["schedule"] = {"daily_at": "25:00"}
        self.assertIn("daily_at", self.problems(d))
        d = base()
        d["services"][0]["schedule"] = {"daily_at": "05:00"}
        self.assertIn("resident service has no schedule", self.problems(d))

    def test_duplicates_and_names(self):
        d = base()
        d["services"][1]["name"] = "query"
        self.assertIn("repeated", self.problems(d))
        d = base()
        d["name"] = "Bad Name"
        self.assertIn("name must be", self.problems(d))

    def test_version_and_env(self):
        d = base()
        d["manifest_version"] = 9
        self.assertIn("manifest_version", self.problems(d))
        d = base()
        d["environment"] = {"lower": "x"}
        self.assertIn("environment", self.problems(d))

    def test_not_json(self):
        with self.assertRaises(mf.ManifestError):
            mf.load("{nope")

    def test_schedule_conversions(self):
        s = mf.Schedule(daily_at=["05:00", "12:30"])
        self.assertEqual(s.systemd_timer_lines(), ["OnCalendar=*-*-* 05:00:00", "OnCalendar=*-*-* 12:30:00",
                                                   "Persistent=true"])
        w = mf.Schedule(weekly_day="sun", weekly_at="06:30")
        self.assertEqual(w.systemd_timer_lines()[0], "OnCalendar=Sun *-*-* 06:30:00")
        e = mf.Schedule(every_minutes=15)
        self.assertIn("OnUnitActiveSec=15min", e.systemd_timer_lines())


if __name__ == "__main__":
    unittest.main()
