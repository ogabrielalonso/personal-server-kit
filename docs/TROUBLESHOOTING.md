# Troubleshooting

First, always: `sudo pskit doctor` on the server, `pskit doctor` on the
laptop. When asking for help, send the file from `sudo pskit doctor --bundle`
(it contains no passwords, tokens or keys).

## During the install

| Symptom | Cause | What to do |
|---|---|---|
| "Only Ubuntu 24.04 LTS is supported" | another distribution or version | reinstall the machine (or rent the VPS) with Ubuntu 24.04 LTS |
| "This looks like a container" | LXC, Docker, some cheap "VPS" | memory limits and the firewall need a real VM or machine |
| The installer waits at "Base packages" | the provider's first-boot updates hold the package lock | nothing: it waits up to 15 minutes, then continues |
| The SSH connection dropped | network, laptop sleep | reconnect and run the installer again; it resumes |
| Tailscale login link expired | the installer waits 15 minutes for the login | run the installer again for a new link |
| "Your message to the bot did not arrive" | the bot was not started | open the bot in Telegram, press Start, run the installer again |
| The test alert did not arrive | wrong topic subscribed, notifications off | check the ntfy topic or Telegram chat; the installer offers another try |
| "The downloaded backup tool did not match its checksum" | a corrupted or tampered download | run again; if it repeats, stop and ask for help |
| "Could not open or create the backup repository" | wrong endpoint, bucket, keys, or SFTP key not added at the destination | fix and run again |
| "Tailscale reaches ... but your tailnet's access rules block ..." | the tailnet has custom access rules (Access controls) with no entry for the new machines; Tailscale itself answers, TCP does not | allow the two addresses shown to reach each other, then run again |
| Pairing: "Cannot reach the server" | Tailscale off on the laptop, a different Tailscale account, or the code expired | turn Tailscale on with the same account; run the installer on the server again for a new code |
| Pairing: "wrong code" | typo (the code never contains 0, 1, O, I or L) | type it again; five mistakes end the session |
| "The laptop did not confirm, so public access was restored" | the laptop could not log in over the tailnet within 10 minutes | nothing was changed. Check Tailscale on both machines, then run the installer again; on a second attempt, run `ssh <server> pskit confirm-lockdown` on the laptop while the server waits |
| "These checks fail before the reboot" | a service did not start | `sudo pskit doctor` shows which; `journalctl -u <unit>` shows why |

## After the install

| Symptom | What to do |
|---|---|
| Alert: "The private network is down" | the only way in. If you can still reach the server some other way, `sudo systemctl restart tailscaled` and `sudo tailscale up`. Otherwise use the provider's web console (VPS) or a keyboard and screen (home PC) |
| Alert about key expiry | disable key expiry for the machine at https://login.tailscale.com/admin/machines |
| Alert: disk full | the daily summary shows agent history sizes; clean old projects or grow the disk |
| Alert: "Last successful backup was N hours ago" | `sudo pskit backup run` shows the error; the evening retry runs automatically |
| Alert: "The restore test failed" | treat as urgent: the backup may not be restorable. `sudo pskit backup restore-test` and ask for help with the diagnostic file |
| Alert: "The brain does not answer its health check" | `systemctl status 'pskit-brain-*'`; the brain layer's own docs |
| "No sessions from 'laptop' for N days" | the laptop was off, or its delivery broke: `pskit doctor` on the laptop |
| `pskit notify` says "Not allowed" | log out and back in once after the install (new group membership) |
| Background jobs paused under pressure | automatic and reversible; they resume when the machine recovers. Frequent pauses mean the machine is too small for the load |

## Locked out

The lockdown only stays if the laptop proved it can log in over the tailnet,
so being locked out means something changed later (Tailscale logged out,
key expiry, the laptop lost its key).

- **VPS:** open the provider's web console (every provider has one; most
  also offer a rescue mode), log in with an account that has a password
  (the owner's sudo password when the installer created the account), then
  `sudo tailscale up` or `sudo pskit uninstall --remove-lockdown`.
- **Home PC:** keyboard and screen, same commands.
- **Lost laptop:** from another trusted machine, pair again after logging in
  through the console; then `sudo pskit forget-device <old laptop>`.
