# Security

## What the kit protects against

| Threat | Protection |
|---|---|
| Internet scans and password guessing | Nothing listens publicly once the install finishes: the firewall allows only the Tailscale interface and Tailscale's own UDP port. SSH accepts keys only, for the owner only, root login off. |
| Locking the owner out while closing access | The lockdown is armed, not just applied: snapshot, apply, 10 minute timer. The laptop must log in over the tailnet to keep it. The timer also fires after a reboot. |
| A stolen laptop key doing everything | Three keys per laptop. The owner key has a shell. The tunnel key can only forward to the brain port on loopback: any command it requests is replaced by `/usr/bin/false`, and remote listens (`-R`) are refused (`permitlisten` must match `127.0.0.1:1`, so sshd denies every real request before binding; unix-socket forwarding is off for everyone in the kit's sshd settings). The bridge key can only run the session receiver for that one device. `sudo pskit forget-device <name>` revokes all three at once. |
| Talking to an impostor server | The server's host keys come back through the pairing and are pinned in a per-server `known_hosts`, with `StrictHostKeyChecking yes`. |
| Someone else pairing a device | Pairing listens only on the tailnet address, for 15 minutes, for one device, and needs the 8 character code shown on the server's screen (about 40 bits). Five wrong codes end the session. |
| The brain reading or writing outside its place | Dedicated user, `ProtectSystem=strict`, `ProtectHome=yes`, write access only to its home, the notify spool and the lock; it receives sessions through the inbox and never reads anyone's home. |
| A brain or build exhausting the machine | Memory ceilings per slice whose sum stays below RAM; heavy jobs serialized by one lock; a pressure guard that freezes (never kills) brain and CI work when the machine stops responding. |
| Channel credentials leaking to the brain | Only root delivers alerts. Everyone else drops a small file into a spool; the Telegram token or ntfy topic stays in `/etc/pskit/secrets` (root, 0700). Root reads the spool without following symlinks, only regular files up to 16 KiB, and clears anything else. |
| A malicious session upload | The receiver accepts only relative paths under the known agent session trees (`claude/projects`, `codex/sessions`, `grok/sessions/%2F...`), refuses `..`, absolute paths, hidden kit files and symlinked directories, validates every header, caps bytes and files per run, writes atomically, and stores under that device's folder only. |
| Losing the backup key with the server | The backup password is shown once; the owner must type its first characters to prove they stored it. Uninstall shows it again before deleting it. |
| Tampered downloads | Tailscale comes from its signed apt repository; restic is a pinned version checked against its published SHA-256. |
| Secrets in support requests | The diagnostic file lists which secret files exist, never their content, and redacts tokens, key IDs, `password=`-style values, ping URLs, ntfy topics, emails, private keys and public IPv4 and IPv6 addresses. |
| A Tailscale auth key in the process list | Passed to `tailscale up` through a root-only file, never on the command line. |
| Answers files shared for support | Secrets are never read from the answers file, only from environment variables. |

## What the kit does not protect against

- A compromised Tailscale account: whoever controls it can join the tailnet.
  Use a strong login with two factors for the Tailscale account.
- A compromised owner account on the server: it can read the brain's files
  (it is in the brain group by design) and use sudo.
- The provider of a rented VPS: they control the hypervisor and the disk.
  Backups are encrypted; the live disk is not.
- Tailscale key expiry: after 180 days the server drops off the tailnet
  unless key expiry is disabled for it in the admin console. The installer
  asks, and `pskit doctor` warns while expiry is on (critical within 14
  days).
- The ntfy topic works like a password: anyone who knows it can read the
  alerts. It is random, but alerts should stay plain (they never contain
  secrets).

## Where secrets live

| Secret | File | Mode |
|---|---|---|
| Telegram token and chat, or ntfy topic | `/etc/pskit/secrets/notify.env` | root 0600 |
| Outside watcher URL | `/etc/pskit/secrets/heartbeat.env` | root 0600 |
| Backup repository and keys | `/etc/pskit/secrets/restic.env` | root 0600 |
| Backup password | `/etc/pskit/secrets/restic.pass` | root 0600 |
| SFTP backup key | `/etc/pskit/secrets/backup_ed25519` | root 0600 |
| Laptop keys | `~/.ssh/pskit/<server>/` | owner 0600 |

The secrets folder is excluded from the backup on purpose: the backup
password must never live inside the thing it protects.

For an SFTP destination the installer prints the public key already
prefixed with `restrict,command="internal-sftp"`. On the destination that
key can only transfer files: sshd serves `internal-sftp` itself and refuses
commands, terminals and tunnels (checked on a real run: backup and restore
worked, `ssh ... id` was refused).

## Reporting a problem

Report security problems privately through GitHub's private vulnerability
reporting: the repository's Security tab, "Report a vulnerability". Do not
open public issues with diagnostic files attached.
