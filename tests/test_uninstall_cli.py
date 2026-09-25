import json
import subprocess
import sys
from pathlib import Path

from pskit import uninstall
from pskit.state import Ledger, ensure_dir, install_file
from pskit.ui import UI
from tests.helpers import HostCase, ok

ROOT = Path(__file__).resolve().parents[1]


class UninstallTest(HostCase):
    def test_reverses_ledger(self):
        led = Ledger(self.host)
        self.host.write_atomic("/etc/pskit/host.json", self.cfg.to_json())
        self.host.write_atomic("/etc/owner.conf", "original\n")
        install_file(self.host, led, "/etc/owner.conf", "ours\n")
        install_file(self.host, led, "/etc/kit-only.conf", "kit\n")
        install_file(self.host, led, "/etc/edited.conf", "kit\n")
        self.host.write_atomic("/etc/edited.conf", "owner edited\n")
        ensure_dir(self.host, led, "/srv/myserver/brain", keep_on_uninstall=True)
        self.p("/srv/myserver/brain/note.md").write_text("precious")
        led.add("package_installed", name="tailscale")
        led.add("group_member_added", user="alice", group="pskit")
        ui = UI({"uninstall_confirm": "myserver"}, interactive=False, out=self.out)
        self.assertEqual(uninstall.run(self.host, ui), 0)
        self.assertEqual(self.p("/etc/owner.conf").read_text(), "original\n")
        self.assertFalse(self.p("/etc/kit-only.conf").exists())
        self.assertFalse(self.p("/etc/edited.conf").exists())
        aside = list(self.p("/root").glob("pskit-uninstalled-*"))
        self.assertTrue(aside and any("edited.conf" in p.name for p in aside[0].iterdir()))
        self.assertEqual(self.p("/srv/myserver/brain/note.md").read_text(), "precious")
        self.assertTrue(self.runner.called(["gpasswd", "-d", "alice", "pskit"]))
        self.assertIn("tailscale", self.out.text)
        self.assertFalse(self.p("/etc/pskit").exists())

    def test_owner_edits_after_install_survive(self):
        from pskit.state import add_line
        led = Ledger(self.host)
        self.host.write_atomic("/etc/pskit/host.json", self.cfg.to_json())
        self.host.write_atomic("/etc/fstab", "UUID=root / ext4 defaults 0 1\n")
        add_line(self.host, led, "/etc/fstab", "/swapfile.pskit none swap sw 0 0")
        led.add("swapfile_created", path="/swapfile.pskit")
        with open(self.p("/etc/fstab"), "a") as fh:
            fh.write("UUID=data /srv/data ext4 defaults 0 2\n")
        self.host.write_atomic("/etc/samba/smb.conf", "vendor\n")
        install_file(self.host, led, "/etc/samba/smb.conf", "kit\n")
        self.host.write_atomic("/etc/samba/smb.conf", "kit plus owner share\n")
        ui = UI({"uninstall_confirm": "myserver"}, interactive=False, out=self.out)
        uninstall.run(self.host, ui)
        self.assertEqual(self.p("/etc/fstab").read_text(),
                         "UUID=root / ext4 defaults 0 1\nUUID=data /srv/data ext4 defaults 0 2\n")
        self.assertEqual(self.p("/etc/samba/smb.conf").read_text(), "kit plus owner share\n")
        aside = next(self.p("/root").glob("pskit-uninstalled-*"))
        self.assertTrue((aside / "etc__samba__smb.conf.before-pskit").exists())

    def test_pending_lockdown_is_undone_first(self):
        from pskit import lockdown as L
        self.runner.on(["ufw", "status"], ok("Status: inactive\n"))
        self.host.write_atomic("/etc/ufw/user.rules", "ORIGINAL\n")
        self.host.write_atomic("/etc/pskit/host.json", self.cfg.to_json())
        L.apply(self.host, Ledger(self.host), self.cfg, self.kit, [])
        self.host.write_atomic("/etc/ufw/user.rules", "LOCKED\n")
        ui = UI({"uninstall_confirm": "myserver"}, interactive=False, out=self.out)
        uninstall.run(self.host, ui)
        self.assertEqual(self.p("/etc/ufw/user.rules").read_text(), "ORIGINAL\n")
        self.assertFalse(self.p(L.SSHD_DROPIN).exists())

    def test_wrong_name_cancels(self):
        self.host.write_atomic("/etc/pskit/host.json", self.cfg.to_json())
        ui = UI({"uninstall_confirm": "nope"}, interactive=False, out=self.out)
        self.assertEqual(uninstall.run(self.host, ui), 1)
        self.assertTrue(self.p("/etc/pskit/host.json").exists())


class CliTest(HostCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-B", "-m", "pskit"] + list(args), cwd=ROOT, capture_output=True,
                              text=True, env={"PATH": "/usr/bin:/bin", "PSKIT_LANG": "pt", "HOME": str(self.root)})

    def test_version_and_help(self):
        r = self.run_cli("version")
        self.assertEqual(r.returncode, 0)
        self.assertRegex(r.stdout.strip(), r"^\d+\.\d+\.\d+$")
        self.assertEqual(self.run_cli("--help").returncode, 0)

    def test_brain_validate(self):
        r = self.run_cli("brain", "validate", str(ROOT / "examples" / "brain-manifest.json"))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("válido", r.stdout)
        bad = self.root / "bad.json"
        bad.write_text(json.dumps({"manifest_version": 0, "name": "x", "services": [{"name": "a"}]}))
        r = self.run_cli("brain", "validate", str(bad))
        self.assertEqual(r.returncode, 1)

    def test_root_commands_refuse(self):
        import os
        if os.geteuid() == 0:
            self.skipTest("running as root")
        r = self.run_cli("health")
        self.assertEqual(r.returncode, 1)
        self.assertIn("sudo", r.stderr)


class WithLockTest(HostCase):
    def test_lock_paths(self):
        from pskit.paths import heavy_lock, run_dir
        self.assertEqual(run_dir(), "/run/pskit")
        self.assertEqual(heavy_lock(), "/run/pskit/heavy.lock")

    def test_with_lock_runs_and_serializes(self):
        import os
        import time as _t
        lock = str(self.root / "l.lock")
        out = str(self.root / "order.txt")
        cmd = [sys.executable, "-B", "-m", "pskit", "with-lock", lock, "--", "sh", "-c",
               f"echo start >> {out}; sleep 0.5; echo end >> {out}"]
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        a = subprocess.Popen(cmd, env=env)
        _t.sleep(0.1)
        b = subprocess.Popen(cmd, env=env)
        self.assertEqual((a.wait(), b.wait()), (0, 0))
        with open(out) as fh:
            self.assertEqual(fh.read().split(), ["start", "end", "start", "end"])


class WithLockReadOnlyTest(HostCase):
    def test_lock_file_the_user_cannot_write(self):
        import os
        lock = self.root / "ro.lock"
        lock.write_text("")
        os.chmod(lock, 0o444)
        r = subprocess.run([sys.executable, "-B", "-m", "pskit", "with-lock", str(lock), "--", "true"],
                           env=dict(os.environ, PYTHONPATH=str(ROOT)), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
