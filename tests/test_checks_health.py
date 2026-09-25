import json
import time

from pskit import notify
from pskit.alerts import AlertState
from pskit.checks import Probes, listening_ports, write_receipt
from pskit.runtime import health
from tests.helpers import HostCase, ok
from tests.test_manifest import EXAMPLE

TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 111 1
   1: 0100007F:225F 00000000:0000 0A 00000000:00000000 00:00000000 00000000     0        0 222 1
   2: 0100007F:225F 0100007F:D431 01 00000000:00000000 00:00000000 00000000     0        0 333 1
"""


class FakeChannel(notify.Channel):
    def __init__(self):
        self.sent = []

    def send(self, text, severity):
        self.sent.append(text)


class ChecksTest(HostCase):
    def setUp(self):
        super().setUp()
        self.host.write_atomic("/proc/net/tcp", TCP)
        self.runner.on(["tailscale", "status", "--json"], ok(json.dumps(
            {"BackendState": "Running", "Self": {"TailscaleIPs": ["100.1.2.3"], "DNSName": "myserver.ts.net."}})))
        self.runner.on(["which", "tailscale"], ok("tailscale\n"))
        self.runner.on(["timedatectl"], ok("yes\n"))

    def probes(self, now=None, http=None):
        return Probes(self.host, self.cfg, self.kit, AlertState(self.host), now=now,
                      http_get=http or (lambda url, t: (200, b'{"status": "ok"}')))

    def test_listening_ports(self):
        self.assertEqual(listening_ports(self.host), {22, 8799})

    def test_access_ok(self):
        f = {x.key: x.level for x in self.probes().access()}
        self.assertEqual(f, {"access:tailscale": "ok", "access:ssh": "ok"})

    def test_backup_ages(self):
        self.kit.backup_kind = "s3"
        now = time.time()
        write_receipt(self.host, "backup-success", {"finished": now - 40 * 3600})
        f = {x.key: x.level for x in self.probes(now=now).backup()}
        self.assertEqual(f["backup:age"], "warn")
        write_receipt(self.host, "backup-success", {"finished": now - 3600})
        write_receipt(self.host, "restore-test", {"ok": False, "finished": now})
        f = {x.key: x.level for x in self.probes(now=now).backup()}
        self.assertEqual(f["backup:age"], "ok")
        self.assertEqual(f["backup:restore"], "warn")

    def test_backup_not_configured_is_info(self):
        f = self.probes().backup()
        self.assertEqual((f[0].key, f[0].level), ("backup:configured", "info"))

    def test_brain_health(self):
        self.host.write_atomic("/etc/pskit/brain-manifest.json", EXAMPLE.read_text())
        self.assertEqual(self.probes().brain()[0].level, "ok")
        down = self.probes(http=lambda u, t: (_ for _ in ()).throw(OSError("refused"))).brain()
        self.assertEqual(down[0].level, "warn")

    def test_device_silence(self):
        now = time.time()
        self.host.write_atomic("/var/lib/pskit/devices.json", json.dumps({"lap": {"paired_at": now - 10 * 86400}}))
        f = self.probes(now=now).devices()
        self.assertEqual(f[0].level, "info")
        self.host.write_atomic("/srv/myserver/brain/inbox/sessions/lap/.pskit-delivery.json",
                               json.dumps({"finished": now - 3600}))
        self.assertEqual(self.probes(now=now).devices()[0].level, "ok")

    def test_crashing_probe_is_isolated(self):
        p = self.probes()
        p.disk = lambda: 1 / 0
        findings, crashed = p.collect()
        self.assertIn("disk:", crashed)
        self.assertIn("probe:disk", [f.key for f in findings])
        self.assertIn("access:ssh", [f.key for f in findings])


class HealthRunTest(ChecksTest):
    def test_alert_once_then_recover(self):
        self.runner.on(["systemctl", "is-active"], ok())
        self.runner.on(["systemctl", "list-units"], ok(""))
        ch = FakeChannel()
        now = time.time()
        self.host.write_atomic("/proc/net/tcp", TCP.replace("0016", "0017"))  # ssh gone
        health.run(self.host, self.cfg, self.kit, now=now, channel=ch)
        self.assertEqual(len(ch.sent), 1)
        self.assertIn("SSH is not listening", ch.sent[0])
        health.run(self.host, self.cfg, self.kit, now=now + 300, channel=ch)
        self.assertEqual(len(ch.sent), 1)
        self.host.write_atomic("/proc/net/tcp", TCP)
        health.run(self.host, self.cfg, self.kit, now=now + 600, channel=ch)
        self.assertEqual(len(ch.sent), 2)
        self.assertIn("back to normal", ch.sent[1])
        self.assertIn("Resolved", ch.sent[1])
