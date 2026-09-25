# Installer

## Principles

1. **Language first.** English is the default, Portuguese the option. Every
   message after that is in the chosen language; technical fields in the
   diagnostic file stay in English.
2. **Nothing changes before preflight passes.** A blocker is one sentence
   and a next action.
3. **Every step is verified.** Each step checks the real machine before
   acting (`check`), acts (`apply`), and proves the result (`verify`). A step
   that cannot prove its result stops the install.
4. **Resumable.** Running the installer again continues where it stopped.
   Finished steps are re-checked against the real machine, never blindly
   re-applied, so a step someone undid by hand is redone.
5. **Reversible.** Every change goes into a ledger
   (`/var/lib/pskit/ledger.jsonl`); `pskit uninstall` reverses it.
6. **The only risky step protects itself.** Closing public access is armed
   with a 10 minute automatic undo that also survives a reboot.
7. **It ends with proof.** A reboot, then an automatic check whose result
   arrives on the owner's phone.

## Running it

```bash
# on the server, as a user with sudo
curl -fsSL https://raw.githubusercontent.com/ogabrielalonso/personal-server-kit/main/install.sh | bash
# or, from a copy of the repository
./install.sh
```

`install.sh` only finds Python 3.9 or newer and the kit's code (the current
checkout, or a download of `PSKIT_REF`, default `main`, optionally checked
against `PSKIT_SHA256`), then runs `python3 -m pskit install`. Server roles
need root: the installer re-runs itself with `sudo`, carrying the answers
given so far, so nothing is asked twice.

Running over SSH? Use `tmux` if you can. If the connection drops, run the
installer again: it resumes.

## Questions

| Order | Question | Default |
|---|---|---|
| 1 | Language | English |
| 2 | What is this computer (on Linux: server or laptop; on macOS: laptop or local only) | server on Linux, laptop on macOS |
| 3 | Server name (lowercase, used in alerts and paths) | the host name |
| 4 | Owner account | the user who ran sudo |
| 5 | Time zone | the machine's current zone |
| 6 | Optional modules (CI runners, network folder) | none |
| 7 | Continue? (after preflight and a plain summary) | yes |

Later steps ask only what they need: the Tailscale login (a link), the alert
channel, the outside watcher URL, the backup destination and its keys, and
the reboot.

## Steps (Linux server)

| # | Step | What it does | How it is verified |
|---|---|---|---|
| 1 | Owner account | creates the owner if missing (only when running as root) and sets the sudo password | the account exists |
| 2 | Base packages | `ca-certificates curl ufw openssh-server unattended-upgrades util-linux bzip2`; waits for the dpkg lock instead of failing | dpkg status |
| 3 | Time zone | `timedatectl set-timezone` | reads it back |
| 4 | Service accounts | groups `pskit` and `pskit-brain`, system user `pskit-brain`; the owner joins both | group membership |
| 5 | Folders | `/srv/<name>/workspace`, `/srv/<name>/brain` (setgid, group `pskit-brain`), the session inbox, `/etc/pskit`, `/var/lib/pskit`, the notify spool | modes and owners |
| 6 | Install the kit | copies the code to `/opt/pskit/releases/<version>`, points `current` at it, writes `/usr/local/bin/pskit` | files and link |
| 7 | Configuration | writes `/etc/pskit/host.json` (the contract) and `kit.json` | content matches |
| 8 | Swap | only if the machine has none: half the RAM, at most 16 GiB and 10% of free disk | swap present |
| 9 | Memory limits | `pskit.slice`, `pskit-brain.slice`, `pskit-ci.slice` (module), caps on the owner's session slice, protection for `system.slice` | files match; systemd reports the ceiling |
| 10 | Temporary files | `/tmp` cleaned after 10 days, the heavy-job lock, 14 day scratch folder in the workspace | lock file exists |
| 11 | Security updates | unattended upgrades on, never an automatic reboot | `apt-config dump` |
| 12 | Private network | Tailscale from its signed apt repository; login by link (or `TS_AUTHKEY`); reminder to disable key expiry | backend state `Running` with an address |
| 13 | Alerts | Telegram (chat captured when the owner presses Start) or ntfy (random topic); a real test message the owner confirms | the owner says it arrived |
| 14 | Outside watcher | optional dead-man URL (healthchecks.io or similar) | stored |
| 15 | Backup | restic 0.19.1 (checksum pinned), repository, password shown once and confirmed by typing its first 6 characters, first backup, restore test | receipts: backup success and restore compared by hash |
| 16 | Background services | health, alert delivery, daily summary, cache cleanup, server session delivery, reaper, pressure guard, heartbeat and backup timers | each unit active |
| 17 | CI module | user `ci`, `pskit-ci.slice`; runners attached with `pskit ci attach` | user and slice |
| 18 | Network folder module | Samba on the workspace, SMB3 with encryption, tailnet only | `testparm`, owner in the Samba database |
| 19 | Pairing | shows the server address and an 8 character code; waits up to 15 minutes for the laptop | a device recorded, its keys installed |
| 20 | Lockdown | key-only SSH for the owner, firewall open only to the tailnet; the 10 minute undo is armed before anything changes; the laptop confirms by logging in over the tailnet once the changes are in place | `sshd -T` and `ufw status` |
| 21 | Brain slot | with a manifest, installs its services; without one, the slot waits | manifest applied, health answering |
| 22 | Final proof | checks everything, then reboots; after boot the result is checked again and sent to the owner | proof receipt |

## When something fails

The installer stops at the failing step with one sentence, a next action,
and the path of a diagnostic file (in the owner's home, readable only by the
owner, with tokens, keys, passwords and public addresses redacted). Run the
installer again after fixing the cause: it resumes. `pskit doctor` shows the
state of every check at any time.

## Unattended installs

Every question has a key. Pass a JSON file with `--answers FILE` and
`--non-interactive` to fail instead of asking when an answer is missing.
Secrets never go in the answers file; they come from environment variables:

| Secret | Variable |
|---|---|
| Tailscale auth key | `TS_AUTHKEY` |
| Telegram bot token | `PSKIT_TELEGRAM_TOKEN` |
| Outside watcher URL | `PSKIT_HEARTBEAT_URL` |
| S3 keys | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` |

Answer keys:

| Key | Values |
|---|---|
| `language` | `en`, `pt` |
| `role` | `server`, `device`, `local` |
| `existing` | `resume`, `exit` (asked when the machine already has the kit) |
| `machine_name`, `owner`, `timezone` | text |
| `modules` | list: `ci`, `samba` |
| `confirm` | `true` |
| `tailscale` | `interactive`, `skip` (tests only) |
| `tailscale_key_expiry_disabled` | `true` once done in the admin console |
| `alert_channel` | `telegram`, `ntfy`, `none` |
| `telegram_chat_id`, `ntfy_server`, `ntfy_topic` | text |
| `alert_test_received` | `true` |
| `heartbeat` | `true`, `false` |
| `backup_kind` | `s3`, `sftp`, `local`, `later` |
| `backup_repository` | `s3:https://endpoint/bucket/folder`, `user@host:/path`, `/path` |
| `backup_password_saved` | `true` (skips typing the first characters) |
| `pairing` | `wait`, `skip` |
| `lockdown` | `apply`, `skip`, `test-local` (tests only) |
| `keep_firewall_rules` | `true`, `false` |
| `brain_manifest` | path to a manifest |
| `prove` | `reboot`, `no-reboot`, `skip` |
| `reboot_now` | `true` |
| `agent_tmp_patterns` | list of `/tmp` globs cleaned after 2 days |

Laptop keys: `server_address`, `device_name`, `pair_code`, `no_passphrase`,
`install_agents`, `confirm_lockdown`, `first_push`, and `tailscale_install`
(`true` lets the installer add Tailscale to an Ubuntu laptop that does not
have it; unattended runs never install a package without it).

Examples: [`examples/answers-server.json`](../examples/answers-server.json),
[`examples/answers-device.json`](../examples/answers-device.json),
[`examples/answers-ci.json`](../examples/answers-ci.json) (the CI run).
