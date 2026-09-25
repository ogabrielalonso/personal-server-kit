# Operations

## Daily life

Nothing to do. The owner receives one message when something starts to go
wrong, changes or recovers, and one summary a day (19:00 by default) with
disk use, the last backup, pending reboots, agent history sizes and the last
session delivery of each laptop.

## Useful commands

```bash
sudo pskit doctor            # every check, in plain language
sudo pskit status            # the short version
sudo pskit doctor --bundle   # diagnostic file for support (no secrets)
sudo pskit prove             # reboot now and prove everything comes back
pskit prove --status         # the last proof result (no sudo needed)
sudo pskit backup snapshots  # list backups
sudo pskit backup run        # back up now
journalctl -u 'pskit-*' -n 100
```

## Pending updates

Security updates install by themselves. When one needs a reboot, the daily
summary says so, and after 7 days the health check alerts. Reboot with
`sudo pskit prove`, which also checks everything came back.

## Updating the kit

Run the installer again from the new version and choose "check everything
and finish what is missing". The new code goes into
`/opt/pskit/releases/<version>` and `current` moves to it; the previous
release stays, so rolling back is pointing `current` back.

## Laptops

- Add another laptop: on the server run `sudo pskit pair-serve`; it shows
  the address and a code for 15 minutes. On the laptop run the installer and
  choose "connect it to a server".
- Remove a laptop (lost or replaced): `sudo pskit forget-device <device>`
  revokes its three keys at once. Sessions it already delivered stay.
- Remove the server from a laptop: `pskit uninstall --device --server <name>`.

Sessions from each laptop land in
`<brain_home>/inbox/sessions/<device>/{claude,codex,grok}/...`; the
server's own sessions in `<brain_home>/inbox/sessions/<server name>/`.

## Restoring from backup

The backup password is in the owner's password manager (it was confirmed at
install). The repository address and keys are in
`/etc/pskit/secrets/restic.env` on the server; keep a copy of that file's
content in the password manager too.

**One file or folder, on the same server:**

```bash
sudo pskit backup snapshots
sudo -i
set -a; . /etc/pskit/secrets/restic.env; set +a
export RESTIC_PASSWORD_FILE=/etc/pskit/secrets/restic.pass
/opt/pskit/bin/restic restore latest --target /tmp/restore --include /srv/<name>/workspace/project
```

Then copy what is needed from `/tmp/restore`. `--include` takes patterns:
escape `[`, `]`, `*` and `?` with a backslash (a Next.js file such as
`pages/[id].tsx` becomes `pages/\[id\].tsx`), or read one file exactly
with `restic dump latest /full/path > file`.

**Everything, on a new server** (the old one is lost):

1. Install the kit on the new machine with the same server name, choosing
   "later" for the backup.
2. Download restic as above (or `apt install restic`), then with the
   repository address, its keys and the password:

   ```bash
   export RESTIC_REPOSITORY='s3:https://...'   # or sftp:user@host:/path
   export AWS_ACCESS_KEY_ID=...  AWS_SECRET_ACCESS_KEY=...
   restic snapshots
   restic restore latest --target /
   ```

3. Run the installer again and configure the backup with the same
   repository and password.
4. Re-pair the laptops (the new server has new host keys).

A restore is proven every week on a sample of files; the result is in the
daily summary and `pskit doctor`.

## Disk filling up

The health check alerts at 80% (critical at 90%, cleared below 75%). The
usual causes are forgotten temporary files and agent histories in the home
folder (Codex, Claude, Grok). The daily summary
shows the history sizes; they are never deleted automatically.

## Memory

`systemctl status pskit-brain.slice` and `systemctl status user-$(id -u).slice`
show usage against the ceilings. The brain's reservation is recomputed from
its manifest's declared peak each time `pskit brain apply` runs.

## Uninstalling

```bash
sudo pskit uninstall                    # keeps data and the lockdown
sudo pskit uninstall --remove-lockdown  # also restores the previous firewall and SSH settings
sudo pskit uninstall --purge-data       # also deletes the workspace and the brain home
```

Packages (Tailscale, ufw, ...) are listed, never removed: removing Tailscale
would cut the only way in. Files the owner changed after the kit wrote them
are kept aside in `/root/pskit-uninstalled-<date>/`.
