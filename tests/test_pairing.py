import json

from pskit.server import keys as K
from pskit.server import pairing
from tests.helpers import HostCase

ED = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
ED2 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIB3dXzKLbC7LmzvqW7oM0kR9Bf4Oq0mXAgm6nJ8Ht9yQ"
RSA = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC7"


class KeysTest(HostCase):
    def test_parse(self):
        self.assertEqual(K.parse_key(ED + " me@laptop"), ED)
        with self.assertRaises(K.KeyError_):
            K.parse_key("not a key")
        with self.assertRaises(K.KeyError_):
            K.parse_key(RSA, require_ed25519=True)
        with self.assertRaises(K.KeyError_):
            K.parse_key('command="rm -rf /" ' + ED)

    def test_restrictions(self):
        self.assertEqual(K.managed_line("tunnel", "lap", ED, 8799),
                         'restrict,port-forwarding,permitopen="127.0.0.1:8799",permitlisten="127.0.0.1:1",'
                         f'command="/usr/bin/false" {ED} pskit:lap:tunnel')
        self.assertIn('command="/usr/local/bin/pskit bridge-receive --device lap"',
                      K.managed_line("bridge", "lap", ED, 8799))
        self.assertEqual(K.managed_line("human", "lap", ED, 8799), f"{ED} pskit:lap:human")


class PairingTest(HostCase):
    def session(self, now=lambda: 1000.0):
        self.kit.tailnet_ip = "100.100.1.2"
        return pairing.PairingSession(self.host, __import__("pskit.state", fromlist=["Ledger"]).Ledger(self.host),
                                      self.cfg, self.kit, "ABCD-2345", now=now)

    def body(self, code="abcd2345", device="lap"):
        return json.dumps({"code": code, "device": device, "os": "Darwin",
                           "keys": {"human": ED, "tunnel": ED2, "bridge": ED2}}).encode()

    def test_success(self):
        self.host.write_atomic("/etc/ssh/ssh_host_ed25519_key.pub", ED + " root@server\n")
        self.host.write_atomic("/home/alice/.ssh/authorized_keys", "ssh-ed25519 AAAAexisting old@key\n")
        s = self.session()
        r = s.handle(self.body())
        self.assertEqual(r["status"], 200)
        self.assertEqual(r["host_keys"], [ED])
        self.assertEqual(r["tailnet_ip"], "100.100.1.2")
        ak = self.p("/home/alice/.ssh/authorized_keys").read_text()
        self.assertIn("old@key", ak)
        self.assertIn("pskit:lap:tunnel", ak)
        self.assertEqual(self.p("/home/alice/.ssh/authorized_keys").stat().st_mode & 0o777, 0o600)
        self.assertIn("lap", self.read_json("/var/lib/pskit/devices.json"))
        self.assertTrue(s.finished)
        again = s.handle(self.body())
        self.assertEqual(again["status"], 409)

    def test_wrong_codes_lock_out(self):
        s = self.session()
        for _ in range(4):
            self.assertEqual(s.handle(self.body(code="ZZZZ9999"))["status"], 403)
            self.assertFalse(s.finished)
        s.handle(self.body(code="ZZZZ9999"))
        self.assertTrue(s.finished)
        self.assertEqual(s.failed, "too many attempts")

    def test_expired(self):
        clock = {"t": 1000.0}
        s = self.session(now=lambda: clock["t"])
        clock["t"] += 901
        self.assertEqual(s.handle(self.body())["status"], 410)

    def test_bad_device_and_keys(self):
        s = self.session()
        self.assertEqual(s.handle(self.body(device="Bad Name"))["status"], 400)
        bad = json.dumps({"code": "ABCD2345", "device": "lap", "keys": {"human": ED, "tunnel": RSA, "bridge": ED}})
        self.assertEqual(s.handle(bad.encode())["status"], 400)
        self.assertEqual(s.handle(b"\xff")["status"], 400)

    def test_repair_replaces_lines(self):
        self.session().handle(self.body())
        self.session().handle(self.body())
        ak = self.p("/home/alice/.ssh/authorized_keys").read_text()
        self.assertEqual(ak.count("pskit:lap:bridge"), 1)
        removed = K.remove_device(self.host, "/home/alice/.ssh/authorized_keys", "lap")
        self.assertEqual(removed, 3)

    def test_code_format(self):
        code = pairing.new_code()
        self.assertRegex(code, r"^[2-9A-HJKMNP-Z]{4}-[2-9A-HJKMNP-Z]{4}$")
        self.assertEqual(pairing.normalize(" abcd-2345 "), "ABCD2345")
