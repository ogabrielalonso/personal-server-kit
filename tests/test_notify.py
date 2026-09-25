import json
from unittest import mock

from pskit import notify
from pskit.paths import SYS
from tests.helpers import HostCase


class FakeChannel(notify.Channel):
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, text, severity):
        if self.fail:
            raise notify.DeliveryError("down")
        self.sent.append((severity, text))


class NotifyTest(HostCase):
    def test_policy(self):
        notify.enqueue(self.host, "critical", "disk full", "details")
        notify.enqueue(self.host, "warn", "backup failed")
        notify.enqueue(self.host, "info", "cleanup done")
        ch = FakeChannel()
        stats = notify.deliver(self.host, self.cfg, channel=ch, now=1000)
        self.assertEqual(stats["sent"], 2)
        self.assertEqual(stats["digest"], 1)
        self.assertEqual(ch.sent[0], ("critical", "myserver: disk full\n\ndetails"))
        self.assertIn("cleanup done", self.p(SYS.digest).read_text())
        self.assertEqual(list(self.p(SYS.spool).iterdir()), [])

    def test_same_instant_keeps_order(self):
        # Messages queued within the same clock tick must go out in the order
        # they were queued, whatever the random part of their file names.
        hexes = iter(["ffffffff", "aaaaaaaa", "00000000"])
        with mock.patch.object(notify.time, "time", return_value=1000.0), \
                mock.patch.object(notify.time, "time_ns", return_value=1_000_000_000_000), \
                mock.patch.object(notify._secrets, "token_hex", side_effect=lambda n: next(hexes)):
            for title in ("first", "second", "third"):
                notify.enqueue(self.host, "warn", title)
        ch = FakeChannel()
        notify.deliver(self.host, self.cfg, channel=ch, now=1000)
        self.assertEqual([text for _, text in ch.sent],
                         ["myserver: first", "myserver: second", "myserver: third"])

    def test_cooldown(self):
        ch = FakeChannel()
        notify.enqueue(self.host, "warn", "x", key="k", cooldown_hours=4)
        notify.deliver(self.host, self.cfg, channel=ch, now=10_000)
        notify.enqueue(self.host, "warn", "x", key="k", cooldown_hours=4)
        stats = notify.deliver(self.host, self.cfg, channel=ch, now=10_000 + 3600)
        self.assertEqual(stats["suppressed"], 1)
        notify.enqueue(self.host, "warn", "x", key="k", cooldown_hours=4)
        notify.deliver(self.host, self.cfg, channel=ch, now=10_000 + 5 * 3600)
        self.assertEqual(len(ch.sent), 2)

    def test_failure_keeps_order_for_retry(self):
        notify.enqueue(self.host, "warn", "first")
        notify.enqueue(self.host, "warn", "second")
        stats = notify.deliver(self.host, self.cfg, channel=FakeChannel(fail=True), now=1)
        self.assertEqual(stats["failed"], 1)
        retry = list(self.p(notify.RETRY_DIR).iterdir())
        self.assertEqual(len(retry), 1)
        self.assertEqual(len(list(self.p(SYS.spool).iterdir())), 1)
        ch = FakeChannel()
        notify.deliver(self.host, self.cfg, channel=ch, now=2)
        self.assertEqual([t.split(": ")[1] for _, t in ch.sent], ["first", "second"])

    def test_invalid_file_dropped(self):
        self.host.write_atomic(f"{SYS.spool}/bad.json", "{not json")
        self.host.write_atomic(f"{SYS.spool}/sev.json", json.dumps({"severity": "panic", "title": "x"}))
        stats = notify.deliver(self.host, self.cfg, channel=FakeChannel(), now=1)
        self.assertEqual(stats["invalid"], 2)

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            notify.enqueue(self.host, "loud", "x")
        with self.assertRaises(ValueError):
            notify.enqueue(self.host, "warn", "  ")

    def test_digest_groups(self):
        for _ in range(3):
            notify.append_digest(self.host, "cleanup: browser closed")
        notify.append_digest(self.host, "other")
        ch = FakeChannel()
        self.assertTrue(notify.flush_digest(self.host, self.cfg, "daily summary", ["Everything is fine."], channel=ch))
        text = ch.sent[0][1]
        self.assertIn("cleanup: browser closed (3x)", text)
        self.assertIn("- other", text)
        self.assertEqual(self.p(SYS.digest).read_text(), "")

    def test_telegram_payload(self):
        calls = []

        def poster(url, data, headers):
            calls.append((url, data))
            return 200, b'{"ok": true}'

        notify.TelegramChannel("123456:" + "a" * 35, "42", poster=poster).send("hello", "warn")
        self.assertIn("/sendMessage", calls[0][0])
        self.assertIn(b"chat_id=42", calls[0][1])

    def test_telegram_error(self):
        ch = notify.TelegramChannel("t", "1", poster=lambda u, d, h: (200, b'{"ok": false}'))
        with self.assertRaises(notify.DeliveryError):
            ch.send("x", "warn")

    def test_ntfy_json_utf8(self):
        got = {}

        def poster(url, data, headers):
            got.update(url=url, body=json.loads(data.decode()), headers=headers)
            return 200, b"{}"

        notify.NtfyChannel("https://ntfy.sh", "topic-1", token="tk", poster=poster).send(
            "myserver: disco quase cheio\n\nDisco / está 91% cheio.", "critical")
        self.assertEqual(got["url"], "https://ntfy.sh/")
        self.assertEqual(got["body"]["title"], "myserver: disco quase cheio")
        self.assertEqual(got["body"]["priority"], 5)
        self.assertEqual(got["headers"]["Authorization"], "Bearer tk")

    def test_channel_from_config(self):
        self.cfg.alert_channel = "telegram"
        self.host.write_atomic(notify.SECRETS_FILE, 'TELEGRAM_BOT_TOKEN="1:x"\nTELEGRAM_CHAT_ID="9"\n')
        self.assertIsInstance(notify.channel_from_config(self.host, self.cfg), notify.TelegramChannel)
        self.cfg.alert_channel = "none"
        self.assertEqual(type(notify.channel_from_config(self.host, self.cfg)), notify.Channel)


class SpoolHardeningTest(HostCase):
    def test_symlink_not_followed(self):
        secret = self.p("/etc/pskit/secrets/notify.env")
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text(json.dumps({"severity": "warn", "title": "LEAK"}))
        spool = self.p(SYS.spool)
        spool.mkdir(parents=True, exist_ok=True)
        (spool / "evil.json").symlink_to(secret)
        ch = FakeChannel()
        stats = notify.deliver(self.host, self.cfg, channel=ch, now=1)
        self.assertEqual(ch.sent, [])
        self.assertEqual(stats["invalid"], 1)
        self.assertFalse((spool / "evil.json").is_symlink())
        self.assertTrue(secret.exists())

    def test_oversized_and_odd_messages(self):
        spool = self.p(SYS.spool)
        spool.mkdir(parents=True, exist_ok=True)
        (spool / "big.json").write_bytes(b" " * 20000)
        (spool / "list.json").write_text("[1, 2]")
        (spool / "cool.json").write_text(json.dumps({"severity": "warn", "title": "x", "cooldown_hours": "soon"}))
        notify.enqueue(self.host, "warn", "real one")
        ch = FakeChannel()
        stats = notify.deliver(self.host, self.cfg, channel=ch, now=1)
        self.assertEqual(stats["invalid"], 3)
        self.assertEqual(len(ch.sent), 1)

    def test_leftovers_swept_so_the_path_unit_stops(self):
        import os
        spool = self.p(SYS.spool)
        spool.mkdir(parents=True, exist_ok=True)
        (spool / ".x.json.abc123").write_text("half written")
        (spool / "subdir").mkdir()
        (spool / "notes.txt").write_text("?")
        for p in spool.iterdir():
            os.utime(p, (1, 1))
        stats = notify.deliver(self.host, self.cfg, channel=FakeChannel(), now=1000)
        self.assertEqual(stats["swept"], 3)
        self.assertEqual(list(spool.iterdir()), [])

    def test_young_temp_file_is_left_alone(self):
        spool = self.p(SYS.spool)
        spool.mkdir(parents=True, exist_ok=True)
        (spool / ".y.json.tmp").write_text("writing")
        import time as _t
        notify.deliver(self.host, self.cfg, channel=FakeChannel(), now=_t.time())
        self.assertTrue((spool / ".y.json.tmp").exists())
