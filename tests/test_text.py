from pskit import setup_backup as SB
from pskit.text import tail_text
from tests.helpers import HostCase

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIHA7ZxO0LWbFVGtgTYe7QABlxbhJTSQ6nH4l28MMYfx pskit-backup@myserver"


class TailTextTest(HostCase):
    def test_short_text_is_kept(self):
        self.assertEqual(tail_text("init failed: nope\n"), "init failed: nope")

    def test_keeps_the_last_whole_lines(self):
        err = "\n".join(f"line {i} " + "x" * 50 for i in range(20)) + "\nserver unexpectedly closed connection"
        out = tail_text(err, 200)
        self.assertTrue(out.endswith("server unexpectedly closed connection"))
        self.assertLessEqual(len(out), 200)
        self.assertTrue(out.startswith("line "))

    def test_one_long_line_starts_at_a_word(self):
        # Found on a real VM: "...: tory at sftp:dev@..." read as a broken message.
        err = "Fatal: create repository at sftp:dev@100.82.91.112:/home/dev/pskit-backup failed: " * 6
        out = tail_text(err, 120)
        self.assertTrue(out.startswith("..."))
        self.assertIn(out[3:].split(" ")[0], err.split(" "))
        self.assertLessEqual(len(out), 123)


class SftpKeyTest(HostCase):
    def test_key_is_printed_whole_and_restricted_outside_the_box(self):
        # Found on a real VM: the box wrapped the key and framed it with
        # borders, so a copy from the terminal was not a valid key.
        self.host.write_atomic("/etc/pskit/secrets/backup_ed25519", "PRIVATE", mode=0o600)
        self.host.write_atomic("/etc/pskit/secrets/backup_ed25519.pub", KEY + "\n")
        ctx = self.ctx({"backup_kind": "sftp", "backup_repository": "dev@100.1.2.3:/srv/backup"})
        env = SB.ask_destination(ctx)
        self.assertEqual(env["RESTIC_REPOSITORY"], "sftp:dev@100.1.2.3:/srv/backup")
        self.assertIn('restrict,command="internal-sftp" ' + KEY, self.out.text.splitlines())
