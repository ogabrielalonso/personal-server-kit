from unittest import mock

from pskit.linux import steps_net as n
from pskit.ui import per_minute
from tests.helpers import HostCase, ok


class PerMinuteTest(HostCase):
    def test_speaks_once_per_minute(self):
        said = []
        tick = per_minute(said.append)
        for left in range(900, 777, -3):
            tick(left)
        self.assertEqual(said, [15, 14, 13])

    def test_never_below_one_minute(self):
        said = []
        tick = per_minute(said.append)
        for left in (50, 10, 0):
            tick(left)
        self.assertEqual(said, [1])

    def test_pairing_wait_keeps_address_and_code_on_screen(self):
        # Found on a real VM: a line every five seconds pushed the address
        # and the code the owner has to type off the screen.
        self.kit.tailnet_ip = "100.101.1.2"
        self.runner.on(["ufw", "status"], ok("Status: inactive\n"))

        def fake_serve(session, ip, port, on_tick=None):
            for left in range(900, 600, -5):
                on_tick(left)
            session.result = {"device": "laptop1"}

        with mock.patch.object(n.pairing, "serve", fake_serve), \
                mock.patch.object(n.pairing, "new_code", lambda: "K7QX-M4RW"):
            n.Pairing().apply(self.ctx())
        waiting = [ln for ln in self.out.text.splitlines() if "Waiting for the laptop" in ln]
        self.assertEqual(len(waiting), 5)
        self.assertTrue(all("100.101.1.2" in ln and "K7QX-M4RW" in ln for ln in waiting))

    def test_lockdown_box_shows_the_manual_confirmation(self):
        # On a second attempt the laptop is not waiting to confirm: the box
        # has to say what to run there.
        self.runner.on(["ufw", "status"], ok("Status: inactive\n"))
        with mock.patch.object(n.L, "read_pending", lambda host: None), \
                mock.patch.object(n.L, "apply", lambda *a, **k: {}), \
                mock.patch.object(n.L, "wait_for_confirmation", lambda *a, **k: True):
            n.Lockdown().apply(self.ctx())
        self.assertIn("ssh myserver pskit confirm-lockdown", self.out.text)
