# Decisions

## Open decisions from the design (docs/STRUCTURE.md)

| # | Question | Decision | Status |
|---|---|---|---|
| D1 | Storage on machines without LVM | Directories on the existing disk, with disk alerts. A one-command installer on a running machine cannot repartition its root disk. LVM stays recommended for new VPS installs, documented, not automated | decided |
| D2 | Two SSH channels or one | One: the standard `sshd`, keys only, owner only. It listens on all interfaces and the firewall admits only the tailnet. Binding `sshd` to the tailnet address is a known cause of SSH not coming back after a reboot (a missing runtime directory, an ordering cycle with `tailscaled`); filtering by interface has neither | decided |
| D3 | Default backup destination | S3-compatible storage first, another owned machine over SFTP second, a local disk with a plain warning third. Never a laptop by default: a backup sent to a laptop fails whenever the laptop is asleep | decided |
| D4 | Default alert channel | Telegram is the default in the installer, ntfy the alternative (no account, least friction), "none" allowed with a warning. The setup captures the Telegram chat automatically | **implemented; the default is the author's call** |
| D5 | Minimum hardware | Preflight refuses below about 4 GiB of RAM or 10 GiB of free disk, warns below 8 GiB of RAM or 30 GiB of disk. Provisional: to be revisited with the brain's measured peaks | provisional |
| D6 | Agent histories filling the root disk | Reported in the daily summary, never moved or deleted automatically | decided |
| D7 | Name, license, public or private | Name `personal-server-kit`, command `pskit`, public repository under the PolyForm Noncommercial 1.0.0 license: noncommercial use is free, commercial use needs the author's written permission | decided |
| D8 | Which systems can be the server | Linux only (Ubuntu 24.04). An always-on Mac was offered as a beta server and removed before its first real run: macOS cannot enforce memory limits, FileVault blocks an unattended boot, and every step would need a second verification. A Mac stays supported as the owner's laptop | decided |

## Design decisions made while building

| Decision | Why |
|---|---|
| The installer runs on the target machine, not from the laptop over SSH | the owner may not have SSH access yet (fresh VPS, old PC with a screen); the laptop joins later by pairing |
| Pairing by code over the tailnet, host keys returned and pinned | no trust-on-first-use prompt for non-technical owners; the code proves the request comes from whoever sees the server's screen |
| Three keys per laptop with forced restrictions | an automation key without a passphrase must not be able to open a shell |
| Lockdown armed with confirm-or-revert, surviving reboots | the only step that can lock the owner out |
| Own session delivery protocol instead of rsync | macOS ships openrsync with different options; rsync keys can write anywhere the owner can; session files are append-only, so tails suffice and are verified with a hash of the last 4 KiB already sent |
| The server's own sessions copied to the inbox | agent session files are created 0600, so group access cannot work; the brain reads one place and never anyone's home |
| Alerts through a root-only spool | the brain can alert without ever seeing the channel credential |
| Hysteresis per stable key, one message per run, reminders once a day | an alert whose key changes with the level repeats every time usage crosses the threshold |
| Freeze, never kill, under pressure | reversible: nothing is lost when the machine recovers |
| restic pinned to a version with its checksum | one static binary with a checksum, and a backup that restores on any system |
| Standard library only, Python 3.9+ | macOS ships 3.9 with the command line tools; nothing to install before the installer |
| Code installed as an immutable versioned release | an update never changes files in use; rolling back is moving one link |
| JSON for the brain manifest | parses with the standard library on Python 3.9 (no TOML parser before 3.11) |
| Fixed `pskit-*` unit names, machine name in paths and alerts | uninstall and doctor find everything; no clash with units an existing setup already has |
| Review by a different model in a fresh context before calling it done | a project rule: whoever builds does not verify their own work; the review found 21 real defects the builder's tests had not |
| Memory proportions fixed as fractions of RAM | the same rule works from 4 to 64 GiB; a 32 GiB machine with the CI module gets brain 10, sessions 14.25, CI 4, margin 3.75 GiB |
