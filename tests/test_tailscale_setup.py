import io
from unittest import mock

from pskit.device import pair
from pskit.device import tailscale_setup as ts
from pskit.system import Host
from pskit.ui import UI
from tests.helpers import HostCase, NullOut, fail, ok

KEY = b"K" * 200


class TailscaleSetupTest(HostCase):
    def setUp(self):
        super().setUp()
        self.runner.on(["which", "apt-get"], ok("/usr/bin/apt-get\n"))

    def test_only_ubuntu_is_installed_by_the_kit(self):
        self.assertTrue(ts.can_install(self.host))
        self.host.write_atomic("/etc/os-release", 'ID=debian\nVERSION_CODENAME=bookworm\n')
        self.assertFalse(ts.can_install(self.host))
        mac = Host(str(self.root), runner=self.runner, platform="macos")
        self.assertFalse(ts.can_install(mac))

    def test_install_uses_the_signed_repository_through_sudo(self):
        with mock.patch.object(ts.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(KEY)):
            ts.install(self.host, "noble")
        self.assertTrue(self.runner.called(["sudo", "-n", "install", "-m", "0644"]))
        env = ["sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive", "NEEDRESTART_MODE=a", "apt-get",
               "-o", "DPkg::Lock::Timeout=900"]
        # Waits for the dpkg lock like the server (found in review: a laptop's
        # own updater often holds it, and apt failed at once).
        self.assertTrue(self.runner.called(env + ["update", "-q"]))
        self.assertTrue(self.runner.called(env + ["install", "-y", "-q", "tailscale"]))
        self.assertFalse(self.runner.called(["sudo", "-v"]))
        self.assertTrue(self.runner.called(["sudo", "-n", "systemctl", "enable", "--now", "tailscaled"]))

    def test_password_is_asked_only_when_sudo_needs_it(self):
        self.runner.on(["sudo", "-n", "true"], fail(1))
        with mock.patch.object(ts.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(KEY)):
            ts.install(self.host, "noble")
        self.assertTrue(self.runner.called(["sudo", "-v"]))

    def test_short_key_is_refused(self):
        with mock.patch.object(ts.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"x")), \
                self.assertRaises(ts.TailscaleSetupError):
            ts.install(self.host, "noble")

    def test_laptop_without_tailscale_gets_it_and_signs_in(self):
        # Suggested by the owner after a real run: on an Ubuntu laptop the
        # kit should install Tailscale instead of sending the owner away.
        states = iter(["", "", "Running"])
        present = {"tailscale": False}
        self.runner.on(["which", "tailscale"],
                       lambda *_: ok("/usr/bin/tailscale\n") if present["tailscale"] else ok(""))

        def fake_install(host, codename):
            self.assertEqual(codename, "noble")
            present["tailscale"] = True

        ui = UI({"tailscale_install": True}, interactive=True, out=NullOut())
        with mock.patch.object(pair, "tailscale_state", lambda host: next(states)), \
                mock.patch.object(ts, "install", fake_install), \
                mock.patch.object(ts, "login", lambda host, ui, name: None):
            pair.wait_tailscale(self.host, ui, timeout_s=1)
        self.assertTrue(present["tailscale"])

    def test_auth_key_goes_through_a_file_not_the_command_line(self):
        seen = {}

        def record(args, *_):
            keyarg = [a for a in args if a.startswith("--auth-key=file:")]
            if keyarg:
                path = keyarg[0].split(":", 1)[1]
                with open(path) as fh:
                    seen["key"] = fh.read()
                seen["path"] = path
            return ok("")

        self.runner.on(["sudo", "-n", "tailscale", "up"], record)
        with mock.patch.dict(ts.os.environ, {"TS_AUTHKEY": "tskey-auth-SECRET"}):
            ts.login(self.host, UI({}, interactive=False, out=NullOut()), "laptop2")
        self.assertEqual(seen["key"], "tskey-auth-SECRET")
        self.assertFalse(ts.os.path.exists(seen["path"]))
        self.assertFalse(any("tskey-auth-SECRET" in " ".join(c) for c in self.runner.calls))

