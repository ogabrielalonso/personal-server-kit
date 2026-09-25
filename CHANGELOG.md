# Changelog

## 0.2.0 (2026-09-24)

- Removed: the Mac mini server scenario (beta, never run on a Mac). The server
  is Linux only; a Mac stays supported as the owner's laptop and for the
  local-only role (decision D8).

From the first run on real virtual machines (see docs/TESTING.md):

- Fixed: the lockdown undid itself at once on a server up for more than ten
  minutes (the safety timer also counted from boot).
- Fixed: alerts queued within the same clock tick could be delivered out of
  order.
- Fixed: `pskit doctor --bundle` crashed on a machine with nothing installed
  (the local-only role, a laptop, an install that failed early).
- The installer says when the tailnet's access rules block pairing or the
  SFTP backup, and which addresses to allow.
- The SFTP backup key is printed whole, outside the box, and limited on the
  destination to file transfer (`restrict,command="internal-sftp"`).
- Pairing and lockdown waits print once a minute; the pairing line repeats
  the address and the code; a second lockdown attempt says what to run on
  the laptop.
- On an Ubuntu laptop the installer installs Tailscale (signed repository,
  waits for the dpkg lock) and signs in with a link or `TS_AUTHKEY`.
- Plain sentences instead of raw JSON in the installer; `pskit prove --status`
  reads as a sentence (`--json` keeps the receipt); context before the
  ssh-keygen passphrase prompt.

## 0.1.0 (2026-09-22)

First complete version.

- Installer: language first (English default, Portuguese), role choice,
  preflight, verified and resumable steps, change ledger, diagnostic file.
- Linux server (Ubuntu 24.04): owner account, packages, service identities,
  folders, versioned code, configuration, swap, memory slices computed from
  RAM, tmpfiles, unattended upgrades, Tailscale, alerts
  (Telegram or ntfy), outside watcher, restic backup with a restore test,
  runtime services, CI and network folder modules, pairing, lockdown with
  confirm-or-revert, brain slot, final reboot proof.
- Mac mini (beta, not run on hardware): power settings, identity with
  `dscl`, launchd services, the same pairing, lockdown and backup.
- Laptop: pairing by code with pinned host keys, three restricted keys,
  tunnel and session delivery as background jobs.
- Runtime: health with hysteresis, root-only alert delivery through a spool,
  daily summary, heartbeat, reaper and cache cleanup, reversible pressure
  guard, backup and restore test.
- `pskit doctor` for installed servers, laptops, and any Ubuntu machine
  (`--audit`).
- Host and brain contract implemented: manifest, reservation from declared
  peaks, heavy-job lock, session inbox, `pskit notify`.
- Tests: unit (Python 3.9 and 3.12), lint, and an end-to-end run on a
  throwaway VM.
- Hardened after an independent adversarial review (21 findings fixed, see
  docs/TESTING.md) and the first end-to-end runs (`/run/sshd`, laptop key
  modes).
