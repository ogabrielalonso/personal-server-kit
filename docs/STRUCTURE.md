# Host layout

What the kit installs on the server, component by component, and why each
piece exists. The server is Linux only: Ubuntu 24.04, a rented VPS or an old
PC. The local-only scenario has no host layer; see the end of this file.

`<name>` is the machine name asked at install. It appears in paths, alerts
and the SSH alias on the laptop.

---

## 1. Base system

| Component | Server | Why |
|---|---|---|
| Operating system | Ubuntu 24.04 LTS; preflight refuses anything else | one supported target keeps every step verifiable |
| Machine name | asked at install | paths and alerts must say which machine they are about |
| Time zone | asked at install; the default is the machine's current zone | schedules run in the owner's local time, including after a daylight saving change |
| Minimum hardware | refuses below about 4 GiB of RAM or 10 GiB of free disk; warns below 8 GiB or 30 GiB (decision D5) | below that, the brain and the owner's sessions cannot both fit |

## 2. Identity and users

| Component | Server | Why |
|---|---|---|
| Owner | the owner's own account, with sudo | the server belongs to one person |
| Brain services | system user and group `pskit-brain`; the owner is in the group and reads everything the brain writes | the brain never runs as the owner or as root |
| Kit group | `pskit`; members may call `pskit notify` | the brain can alert the owner without seeing the channel credential |
| CI | user `ci`, no privileges, outside any home (optional module, section 12) | a build job must not be able to read the owner's files |
| Agent CLI accounts | one login per person per tool; the kit never handles them | credentials are personal and are never shared |

## 3. Storage

| Path | Server | Why |
|---|---|---|
| Workspace | `/srv/<name>/workspace`, owner only (0750) | the owner's work, outside the home folder that agents fill |
| Brain home | `/srv/<name>/brain`, owned by `pskit-brain`, setgid (2770) | the only place the brain writes; the owner reads it through the group |
| Session inbox | `<brain home>/inbox/sessions` | every device delivers its AI sessions to one place |
| Kit code | `/opt/pskit/releases/<version>`, with a `current` link | an immutable release per version: rolling back is moving one link |
| Swap | half the RAM, at most 16 GiB, never more than 10% of free disk | relief under memory pressure without filling the disk |

The installer creates directories on the existing disk and never
repartitions it (decision D1): a one-command installer running on a live
machine cannot safely change its root disk. For a new VPS, separate volumes
(LVM) for the workspace and the brain are recommended and documented, not
automated. On an always-on machine with agents, the root disk fills from
temporary files and from agent histories in the home folder, never from
memory; sections 8 and 11 cover both.

## 4. Memory

Linux enforces limits with systemd slices (cgroups v2). All values are
computed from the machine's RAM at install (`pskit/budget.py`):

| Slice | Protected (`MemoryLow`) | Slowed from (`MemoryHigh`) | Ceiling (`MemoryMax`) |
|---|---|---|---|
| `system.slice` | 6.4% of RAM, at most 2 GiB | none | none |
| `user-<uid>.slice` (the owner's sessions and agents) | 0 | 12/14 of its ceiling | what is left after the others and the margin |
| `pskit-brain.slice` | 40% of the brain ceiling | 80% of the brain ceiling | the brain's declared peak plus 25%, at least 1 GiB, at most a third of RAM; 31.9% of RAM without a manifest |
| `pskit-ci.slice` (optional) | 0 | 75% of its ceiling | 12.8% of RAM |
| parent `pskit.slice` | the brain's protection | none | brain plus CI |

Rule: **the sum of the ceilings always stays below physical RAM**, with a
margin of at least 1 GiB (10.6% of RAM) for the kernel and anything outside
the slices. Limits set one by one can otherwise add up to more than the
machine has, and nothing protects the system. Example for a 32 GiB machine
with the CI module: brain 10 GiB, owner sessions 14.25 GiB, CI 4 GiB,
margin 3.75 GiB.

Heavy brain jobs and the backup share one lock (`/run/pskit/heavy.lock`), so
they never peak together. Under pressure, a guard freezes the brain and CI
slices instead of killing anything, and thaws them when the machine
recovers (section 8).

## 5. Access and network

| Component | Server | Why |
|---|---|---|
| Private network | Tailscale | the laptop and the server meet without exposing anything to the internet |
| Firewall | `ufw`: deny incoming, admit only the tailnet interface and Tailscale's own UDP port (41641) | on Linux, nothing on the server answers the public internet |
| SSH | one standard `sshd`, keys only, owner only, root login off; it listens on all interfaces and the firewall filters (decision D2) | an SSH service bound to the tailnet address can start before the network exists and never come back after a reboot |
| `/run/sshd` | recreated at every boot by a tmpfiles entry | with socket activation the directory may never exist, and `sshd` refuses to start or test its configuration without it |
| Closing public access | armed with a 10-minute automatic undo that survives reboots; kept only after the laptop proves it can log in over the tailnet | the only step that can lock the owner out |
| Pairing | a one-time 8-character code, port 47800 on the tailnet only, 15 minutes, one device per session | no trust-on-first-use prompt: host keys come back and are pinned |
| Brain query port | `127.0.0.1:8799`, loopback only | the brain's query service has no authentication of its own |

## 6. Device side (the owner's laptop)

| Component | What the kit installs | Why |
|---|---|---|
| Three keys | owner key (shell), tunnel key (brain port only), bridge key (session delivery only), each with forced restrictions | an automation key without a passphrase must not be able to open a shell |
| Brain tunnel | a background job keeps an SSH tunnel to `127.0.0.1:8799`, with `ConnectTimeout` | a hung SSH connection never exits, so a restart policy alone never fires |
| Session delivery | a background job sends new parts of the agents' session files every 15 minutes | the brain reads one inbox; a delivery receipt per device makes silence visible |
| Host keys | pinned per server in a separate `known_hosts` | an impostor server is refused |
| SSH alias | generated from the machine name | `ssh <name>` just works |

## 7. Backup

| Component | Server | Why |
|---|---|---|
| Tool | restic, encrypted, a pinned version checked by SHA-256 | the same backup restores on any system |
| Destination | S3-compatible storage first, another machine the owner has over SFTP second, a local disk with a plain warning third (decision D3) | a default destination must not depend on a laptop being awake |
| Schedule | nightly at 01:15; at 19:30 a retry runs only when there was no success in the last 18 hours; both in local time | one missed night does not become a gap |
| Retention | 7 daily, 4 weekly, 6 monthly snapshots | recent and older restore points, bounded storage |
| Restore proof | a sample restored and compared by hash at install and every Sunday at 04:30 | a backup that was never restored is not proven |
| Brain data | the paths the brain lists in its manifest | only the brain knows what it can rebuild |

## 8. Health, alerts and cleanup

| Component | What runs | Why |
|---|---|---|
| Health check | every 5 minutes and 2 minutes after boot: disk, memory, access, services, updates, backup age, brain health, device silence | problems are found before the owner feels them |
| Alerts | Telegram or ntfy (decision D4); disk warns at 80% and is critical at 90%, each level clears only 5 points below; one message when a problem starts, changes or clears, a reminder once a day | an alert that fires on every check repeats all day and stops being read |
| Alert delivery | anyone in the kit group drops a message into a spool; only root delivers | the channel token never leaves root's secrets folder |
| Pressure guard | every 30 seconds: CPU and IO pressure and fork time; brain and CI are frozen, never killed, and thawed on recovery | reversible relief when the machine stops responding under load |
| Orphan reaper | every 15 minutes: headless browsers older than 3 hours, idle development servers older than 12 hours | sessions leave processes running that nobody will close |
| Temporary files | `/tmp` cleaned after 10 days (instead of 30), agent temporary patterns after 2 days | agents write temporary files all day |
| Cache cleanup | pip and npm caches, conservatively, daily at 16:30 | package caches grow without bound |
| Daily summary | one message at 19:00 with everything that did not deserve an interruption, including agent history sizes (never deleted, decision D6) | the owner sees trends without being paged |

## 9. External watcher

A machine cannot report its own death. The kit can send a heartbeat every 5
minutes to an external dead-man service, which alerts the owner when the
heartbeats stop. A second machine at home is not a good watcher: it fails
together with the house's power or internet.

## 10. Updates and reboots

| Component | What the kit does | Why |
|---|---|---|
| Security updates | `unattended-upgrades` active | patches arrive without the owner |
| Pending reboot | reported in the daily summary; never an automatic reboot | the owner chooses when |
| Reboot proof | the last install step reboots and confirms everything came back; `pskit prove` repeats it on demand | defects in boot ordering only show up after a reboot |
| Power loss | old PC: set the BIOS to power on after an outage | an always-on machine must come back by itself |

## 11. Agent CLIs

Claude Code, Codex and Grok run under the owner's account with their own
logins. The kit reads their session folders for delivery and reports their
history sizes; it never handles their credentials. Services that call a CLI
need its absolute path, because systemd starts with a minimal
`PATH`.

## 12. Optional modules

| Module | What it adds | Why optional |
|---|---|---|
| CI runners | user `ci` and `pskit-ci.slice` with its own ceiling | only people with self-hosted CI need it |
| Network folder | Samba share of the workspace, private to the tailnet | convenient for a file browser, but it adds a listener |

## 13. Local only (no server)

No host layer. The brain installs on the owner's machine and its scheduled
jobs run only while that machine is awake. The installer says so plainly,
installs nothing, and points to the brain kit.

---

## Decisions

See [DECISIONS.md](DECISIONS.md) for the reasoning. In short: D1 directories
on the existing disk; D2 one SSH service filtered by the firewall; D3 a
backup destination that never depends on a laptop; D4 Telegram by default
with ntfy as the alternative; D5 provisional hardware thresholds; D6 agent
histories reported, never touched; D7 public repository under the PolyForm
Noncommercial license.

## Where each component lives in the code

| Section | Implementation |
|---|---|
| 1 Base system | `pskit/preflight.py`, `linux/steps_base.py` (`Packages`, `Timezone`) |
| 2 Identity | `linux/steps_base.py` (`OwnerAccount`, `Identity`) |
| 3 Storage | `linux/steps_base.py` (`Storage`, `Swap`) |
| 4 Memory | `pskit/budget.py`, `linux/steps_base.py` (`MemoryBudget`), `runtime/pressure.py` |
| 5 Access | `linux/steps_net.py`, `pskit/lockdown.py`, `server/pairing.py`, `server/keys.py` |
| 6 Device side | `pskit/device/`, `pskit/bridge.py` |
| 7 Backup | `setup_backup.py`, `runtime/backup.py` |
| 8 Health, alerts, cleanup | `checks.py`, `alerts.py`, `notify.py`, `runtime/health.py`, `runtime/digest.py`, `runtime/reaper.py`, `runtime/cachemaint.py`, templates |
| 9 External watcher | `runtime/heartbeat.py` |
| 10 Updates and reboots | `linux/steps_base.py` (`Updates`), `prove.py` |
| 11 Agent CLIs | session sources in `bridge.py`; history sizes in the daily summary |
| 12 Optional modules | `linux/steps_ops.py` (`CiModule`, `SambaModule`) |
| 13 Local only | `install.py` (`run_local`) |
