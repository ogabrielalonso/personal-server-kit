import json
import re
import unittest
from pathlib import Path

from pskit import i18n

ROOT = Path(__file__).resolve().parents[1]
LOC = ROOT / "pskit" / "locales"
PH = re.compile(r"\{([a-z_]+)\}")
PREFIXES = ("alert", "alerts", "backup", "brain", "budget", "ci", "cli", "device", "digest", "doctor", "engine",
            "health", "heartbeat", "install", "local", "lockdown", "owner", "packages", "pairing",
            "preflight", "pressure", "proof", "reaper", "role", "samba", "status", "step", "swap", "tailscale",
            "ui", "uninstall", "updates", "unit")
KEY_LITERAL = re.compile(r"[\"']((?:" + "|".join(PREFIXES) + r")\.[a-z0-9_.-]+)[\"']")


def load(lang):
    return json.loads((LOC / f"{lang}.json").read_text(encoding="utf-8"))


class I18nTest(unittest.TestCase):
    def test_same_keys_and_placeholders(self):
        en, pt = load("en"), load("pt")
        self.assertEqual(set(en), set(pt))
        for k in en:
            self.assertEqual(set(PH.findall(en[k])), set(PH.findall(pt[k])), k)

    def test_no_dash_punctuation(self):
        for lang in ("en", "pt"):
            for k, v in load(lang).items():
                self.assertNotIn(chr(0x2014), v, k)
                self.assertNotIn(chr(0x2013), v, k)

    def test_every_key_used_in_code_exists(self):
        en = load("en")
        missing = set()
        for py in (ROOT / "pskit").rglob("*.py"):
            text = py.read_text(encoding="utf-8")
            for m in KEY_LITERAL.finditer(text):
                key = m.group(1)
                if key.endswith(".") or key.count(".") < 1:
                    continue
                if key.endswith((".json", ".py", ".service", ".timer", ".slice", ".path", ".conf", ".plist")) \
                        and not key.startswith("unit."):
                    continue
                if key not in en:
                    missing.add(f"{key} ({py.name})")
        self.assertEqual(sorted(missing), [])

    def test_dynamic_families(self):
        en = load("en")
        for prefix in ("disk", "mem", "swap", "access", "unit", "failed", "updates", "time", "backup", "brain",
                       "device", "probe"):
            self.assertIn(f"doctor.ok_{prefix}", en)

    def test_portuguese_is_used(self):
        i18n.set_language("pt")
        try:
            self.assertEqual(i18n.t("engine.done"), "feito")
            self.assertEqual(i18n.t_in("en", "engine.done"), "done")
        finally:
            i18n.set_language("en")

    def test_unknown_key_falls_back_to_key(self):
        self.assertEqual(i18n.t("no.such.key"), "no.such.key")


if __name__ == "__main__":
    unittest.main()
