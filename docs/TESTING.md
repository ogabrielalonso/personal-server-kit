# Testing

## What runs, where

| Layer | Where | What it proves |
|---|---|---|
| Unit tests (`tests/`) | anywhere, no root: every step runs against a temporary root directory with a scripted command runner | step logic, templates, alert hysteresis, notify policy, lockdown arm/confirm/revert, pairing protocol (including a real HTTP exchange on loopback), session delivery (tails, resync, hostile paths), backup receipts and restore comparison, redaction, uninstall from the ledger, both languages complete |
| Lint | CI and locally | `ruff`, `mypy`, `shellcheck`, no en or em dash in any text |
| Python 3.9 | CI matrix and locally with `uv run --python 3.9` | the macOS system Python can run the kit |
| End to end (`tests/e2e/run.sh`) | a throwaway Ubuntu 24.04 VM in GitHub Actions | the real install as root: packages, users, slices with real ceilings, firewall and key-only SSH, restic backup and restore test, runtime units active, a fake brain applied from a manifest (runs as its user, in its slice, reservation from its peak, batch job with the heavy lock, end-to-end test), a second Linux user paired as "the laptop" over real SSH (owner key logs in; the tunnel key reaches only the brain port and cannot run commands; the bridge key cannot run commands; sessions delivered and appended as tails), the server's own sessions delivered, notify from the owner through the spool, an idempotent second run, the diagnostic file without the backup password, and uninstall |
| Audit (`pskit doctor --audit`) | read-only on real machines | the generic checks work on a machine the kit did not install |
| Real VM run | KVM virtual machines from the Ubuntu 24.04 cloud image, on the owner's network | the whole flow as a person runs it, from the public one-liner: see below |

Run locally:

```bash
python3 -B -m unittest discover -s tests -t .
uvx --from ruff==0.12.11 ruff check pskit tests
uvx --from mypy==1.18.2 mypy pskit
shellcheck -x install.sh tests/e2e/run.sh
```

`tests/e2e/run.sh` refuses to run outside GitHub Actions: it reconfigures the
whole machine.

## Real VM run (23 and 24 Sep 2026)

One server and two laptops, each a fresh virtual machine from the signed
Ubuntu 24.04 cloud image, with no provider tooling. Everything a person
does was done for real: the one-liner from this repository, Portuguese
answers, a Tailscale login by link and later by auth key, a Telegram bot
receiving the test alert and the reboot result, an encrypted backup over
SFTP to another machine with the restore test, two laptops paired, the
lockdown checked from outside the tailnet (the connection times out) and
again after the reboot, and the reboot proof (15 of 15 checks).

It found what CI could not:

- **the lockdown undid itself at once** on any machine up for more than
  ten minutes: its safety timer also counted from boot, and systemd fires
  a boot-relative timer immediately when that moment has passed. CI never
  saw it because its VM reaches the lockdown minutes after boot. Fixed,
  then checked on real systemd, including a reboot;
- a tailnet with custom access rules made pairing and the SFTP backup time
  out with a generic message; the installer now recognises the signature
  (Tailscale answers, TCP does not) and says which addresses to allow;
- the pairing wait printed a line every five seconds and pushed the code
  off the screen;
- the SFTP backup key was wrapped inside a box (a copy was not a valid key),
  and the text claimed a restriction the pasted line did not have; the line
  now carries `restrict,command="internal-sftp"` (commands refused, backup
  and restore work);
- raw JSON, systemd noise and an unexplained English prompt in the middle
  of a Portuguese run; a second lockdown attempt that did not say what to
  run on the laptop;
- on an Ubuntu laptop the kit now installs Tailscale itself and accepts
  `TS_AUTHKEY`.

A different model then reviewed those fixes and found two more (the
laptop's apt calls did not wait for the dpkg lock; the access rules hint
did not know MagicDNS names). Every fix has a test shown to fail first.

After the Mac mini scenario was removed, the run was repeated unattended
from answers files on two fresh machines: Tailscale by auth key on both,
the laptop paired, the lockdown confirmed over the tailnet, 15 of 15 checks
after a real reboot, a manual backup, `pskit doctor` with no problem, and
both uninstalls (firewall and SSH settings restored). It found one gap:
the laptop's `tailscale_install` answer was missing from docs/INSTALLER.md.

## Independent review

After the first complete version, a different model reviewed the code in a
fresh context, adversarially, without the design documents. It reported 21
findings (6 high), most reproduced. All were fixed with a regression test
each, and every new test was shown to fail against the previous code before
the fix counted:

- uninstall could restore a pre-install `/etc/fstab` over later edits;
- the lockdown's safety timer was armed after the changes, so a failure
  arming it could leave the machine closed without its undo;
- uninstall with an unconfirmed lockdown removed its safety timer;
- the restore test treated paths as globs (`[id].tsx` never restored);
- server session delivery could hold up to 2 GiB in memory;
- root followed symlinks in the alert spool;
- and fifteen medium or low ones (quoted firewall rules, port ranges,
  reaper substring matches, header validation, proof time budget, macOS
  runtime folder, remote listens on the tunnel key, SSH port assumptions,
  clock steps, IPv6 redaction, swap cleanup, prompt ordering).

A second independent pass then verified the fixes: 18 confirmed fixed, 3
partial, and 9 new defects introduced by the fixes themselves (among them
`ufw allow log 22/tcp` escaping SSH detection, the macOS lock wrapper
failing for the brain's user, the reaper missing `next-server (vX)` process
titles, and a confirmation window race). All were fixed, again each with a
test shown to fail first.

The end-to-end run itself found two more: `sshd -t` needs `/run/sshd`, which
socket activation never creates on a fresh install (the same missing
directory keeps an SSH service from starting after a reboot), and laptop
keys needed explicit modes.

## Not proven yet

| Gap | Why | How it closes |
|---|---|---|
| A rented VPS | the real VM run used plain virtual machines; a provider's image, network, console and slow first boot were not exercised | one install on a small hourly VPS |
| Real Tailscale in CI | CI skips Tailscale (no auth key in CI); pairing binds to loopback there. The real VM run covered it by hand | a Tailscale auth key stored as a CI secret |
| The laptop side on macOS | launchd agents, Keychain options and openssh on macOS are unit tested only; the Linux laptop side was exercised for real in the VM run | the first real pairing from a Mac |
| ntfy delivery | covered with fake HTTP endpoints; Telegram was exercised for real in the VM run | the first real install with ntfy |
| Long-running behavior | alert reminders, weekly restore tests, pressure freezes under real load | weeks of real use |
