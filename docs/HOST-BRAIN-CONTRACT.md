# Host and brain contract

**Version 0, implemented since pskit 0.1.0 (2026-09-22); 0.2.0 dropped the
`mac-mini` scenario value.** The only interface
between the host layer (this repository) and the brain layer (designed in a
separate project). Each side may change anything behind its half; changing
this file needs agreement from both.

The host never reaches into brain internals. The brain never configures the
machine. Most breakages happen where the two get mixed; every rule below
keeps them apart.

## Shared configuration: `/etc/pskit/host.json`

Written by the installer, readable by everyone, never contains secrets.
Services get its path in `PSKIT_HOST_CONFIG`.

| Key | Example | Meaning |
|---|---|---|
| `contract_version` | `0` | both sides refuse to run on a version they do not know |
| `language` | `en` | `en` or `pt`; every message to the owner uses it |
| `scenario` | `linux-server` | `local` or `linux-server` |
| `machine_name` | `myserver` | used in paths, alerts, and as the inbox folder of the server's own sessions |
| `owner` | `alex` | the owner's account |
| `brain_home` | `/srv/myserver/brain` | the only place the brain writes |
| `brain_user` | `pskit-brain` | the brain's identity; its group has the same name |
| `brain_port` | `8799` | loopback port of the brain's query service |
| `session_inbox` | `inbox/sessions` | relative to `brain_home` |
| `workspace` | `/srv/myserver/workspace` | the owner's work (not the brain's) |
| `memory_budget` | `{"brain_max": 10.0, ...}` | the limits computed at install, in GiB |
| `alert_channel` | `telegram` | informational; the credential stays with the host |
| `notify_command` | `/usr/local/bin/pskit notify` | how to alert the owner |
| `heavy_lock` | `/run/pskit/heavy.lock` | the heavy-job lock |
| `kit_group` | `pskit` | members may call `notify` |

## What the host gives the brain

1. **An identity**: system user and group `pskit-brain`. The owner is in the
   group and can read everything the brain writes (files 0640, folders
   setgid 2770).
2. **A home**: `brain_home`, created by the host. Disk use is watched.
3. **A memory reservation**: `pskit-brain.slice` with
   `MemoryLow` = 40%, `MemoryHigh` = 80% and `MemoryMax` = 100% of the
   brain's ceiling. The ceiling is the declared peak (resident plus one
   batch) plus 25%, at least 1 GiB, at most a third of RAM. Without a
   manifest, 31.9% of RAM, rounded down in 256 MiB steps (10 GiB on a 32 GiB machine).
4. **A heavy-job lock**: services marked `heavy_lock` run under the kit's
   own lock wrapper (`pskit with-lock /run/pskit/heavy.lock -- ...`, a
   `flock` on that file). The host's backup takes
   the same lock, so a brain batch and a backup never peak together.
5. **Scheduling**: the host turns each declared service into a systemd unit
   (`pskit-brain-<name>.service`, plus `.timer` for scheduled ones). The
   brain never installs units.
   Units run with `ProtectSystem=strict`, `ProtectHome=yes`,
   `NoNewPrivileges=yes`, `PrivateTmp=yes`, write access only to
   `brain_home`, the notify spool and the lock.
6. **A private port**: `brain_port` stays on loopback. The laptop reaches it
   through an SSH tunnel whose key can do nothing else.
7. **Session delivery**: every agent session arrives under
   `<brain_home>/<session_inbox>/<source>/...`:

   ```text
   inbox/sessions/<device>/claude/projects/<project>/<session>.jsonl
   inbox/sessions/<device>/codex/sessions/YYYY/MM/DD/rollout-*.jsonl
   inbox/sessions/<device>/grok/sessions/%2F<project>/<uuid>/<file>
   inbox/sessions/<machine_name>/...        the server's own sessions
   inbox/sessions/<device>/.pskit-delivery.json   {"finished", "files", "bytes"}
   ```

   Files only grow (append) or are replaced whole; the host never deletes
   them. The brain decides what to read and keeps its own cursor.
8. **Backup**: the host backs up the brain's `backup.include` paths (or all
   of `brain_home` when none are declared), minus `backup.exclude`, nightly,
   and proves restore weekly on a sample.
9. **Alerts**: `pskit notify <critical|warn|info> "<title>" ["<body>"]
   [--key K --cooldown-hours H]`. Critical and warn reach the owner now;
   info goes to the daily summary. A repeated key within its cooldown is
   dropped. Messages should be plain language, without secrets.
10. **Health polling**: every 5 minutes the host calls the brain's health
    URL and alerts (once, with hysteresis) when it fails.

Services also get `BRAIN_HOME` and `PSKIT_HOST_CONFIG` in their environment,
plus the manifest's `environment` map.

## What the brain gives the host: the manifest

A JSON file (`manifest_version` 0), applied with
`sudo pskit brain apply <file>` or at install with the `brain_manifest`
answer; validated with `pskit brain validate <file>`. Full example:
[`examples/brain-manifest.json`](../examples/brain-manifest.json).

| Field | Rules |
|---|---|
| `name` | lowercase letters, digits, hyphens |
| `services[]` | `name`; `kind` `resident` (always running, restarted) or `scheduled`; `command` as a list whose first item is an absolute path (systemd starts with a minimal `PATH`); optional `heavy_lock`, `timeout_minutes` (1 to 1440), `description` |
| `services[].schedule` | scheduled only: `{"every_minutes": 5..1440}`, `{"daily_at": "HH:MM" or ["HH:MM", ...]}`, or `{"weekly": {"day": "mon".."sun", "at": "HH:MM"}}` |
| `health` | `url` must be `http://127.0.0.1:<port>/...`; `expect_status` (default `ok`) compared with the JSON field `status`; `timeout_seconds` |
| `backup` | `include` and `exclude`: relative paths inside `brain_home` |
| `peak_memory` | `resident_mib` and `batch_mib`, measured |
| `e2e_test` | `command` (absolute path first) proving the brain works end to end; run as the brain user in its slice during the final proof; exit 0 means pass |
| `session_inbox` | relative path, default `inbox/sessions` |
| `environment` | `UPPER_CASE` names to strings; never secrets |

Applying a new manifest stops and removes the units of services that are no
longer listed, and recomputes the memory reservation.

## The brain's promises

- It writes only inside `brain_home`.
- It listens only on loopback.
- It reports failure through `pskit notify`, never by failing silently.

## Why each clause exists

| Clause | The failure it prevents |
|---|---|
| Host installs the units | two tools managing the same services undo each other's changes |
| Memory reservation from declared peaks | brain jobs moved into a slice without revisiting its ceiling exceed it on the next full cycle |
| Heavy-job lock | brain batches, backups and CI competing for the same RAM |
| Session delivery by the host, into one inbox | a device that silently stops sending goes unnoticed for days; each device has a delivery receipt and silence is reported |
| Backup list from the brain | only the brain knows which of its data is rebuildable (indexes, models) and which is not (its notes) |
| `notify` owns hysteresis and the credential | an alert that fires on every check repeats while usage hovers near the threshold |
| Loopback only | the brain query service has no authentication |

## Still open for the brain project

- Real `peak_memory` values for a typical personal brain.
- Health fields beyond `status`.
- The brain's `e2e_test` command.
- Whether scenario A (local only) uses the same manifest.
