#!/usr/bin/env bash
# End-to-end test of the Linux server scenario on a throwaway Ubuntu 24.04
# virtual machine (GitHub Actions). It installs for real: packages, users,
# slices, firewall, SSH hardening, backup, the brain slot, pairing a second
# local user as "the laptop" over real SSH, then uninstalls.
#
# Never run this on a machine you care about. It refuses outside CI.

set -euo pipefail

if [ "${GITHUB_ACTIONS:-}" != "true" ] || [ "${PSKIT_E2E_CONFIRM:-}" != "throwaway-vm" ]; then
  echo "refusing: this test reconfigures the whole machine (set PSKIT_E2E_CONFIRM=throwaway-vm in CI)" >&2
  exit 2
fi

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
OWNER="$(id -un)"
NAME="ci-box"
WORK="$(mktemp -d)"
chmod 755 "$WORK"   # the "laptop" user reads its answers file from here
PASS=0
FAIL=0

ok()   { PASS=$((PASS + 1)); echo "  ok    $*"; }
bad()  { FAIL=$((FAIL + 1)); echo "  FAIL  $*"; }
check() { local what="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$what"; else bad "$what"; fi; }
section() { echo; echo "== $*"; }
pskit() { sudo PYTHONPATH="$REPO" /usr/bin/python3 -B -m pskit "$@"; }

section "install (unattended)"
sed "s/\"owner\": \"runner\"/\"owner\": \"$OWNER\"/" "$REPO/examples/answers-ci.json" > "$WORK/answers.json"
sudo mkdir -p /var/backups
time pskit install --non-interactive --answers "$WORK/answers.json"

section "installed state"
check "shim installed" test -x /usr/local/bin/pskit
check "host.json valid" sudo python3 -c "import json; d=json.load(open('/etc/pskit/host.json')); assert d['machine_name']=='$NAME'"
for u in pskit-health.timer pskit-notify.path pskit-digest.timer pskit-cache.timer pskit-local-bridge.timer \
         pskit-reaper.timer pskit-pressure-guard.service pskit-backup.timer pskit-backup-retry.timer \
         pskit-restore-test.timer; do
  check "active: $u" systemctl is-active --quiet "$u"
done
check "brain slice has a ceiling" test "$(systemctl show -p MemoryMax --value pskit-brain.slice)" != "infinity"
check "ci slice exists (module)" systemctl cat pskit-ci.slice
check "user slice capped" test "$(systemctl show -p MemoryMax --value "user-$(id -u).slice")" != "infinity"
check "heavy lock present" test -f /run/pskit/heavy.lock
check "tmp age 10d" grep -q "D /tmp 1777 root root 10d" /etc/tmpfiles.d/tmp.conf
check "firewall active" sudo sh -c "ufw status | grep -q 'Status: active'"
check "tailnet rule" sudo sh -c "ufw status | grep -q tailscale0"
check "ssh keys only" sudo sh -c "sshd -T -C user=$OWNER,host=localhost,addr=127.0.0.1 | grep -qx 'passwordauthentication no'"
check "lockdown applied" sudo test -f /var/lib/pskit/lockdown/applied.json
check "backup receipt" sudo test -f /var/lib/pskit/receipts/backup-success.json
check "restore proven" sudo python3 -c "import json; assert json.load(open('/var/lib/pskit/receipts/restore-test.json'))['ok']"
check "proof passed" sudo python3 -c "import json; assert json.load(open('/var/lib/pskit/receipts/proof.json'))['ok']"
check "brain home setgid" test "$(stat -c %a /srv/$NAME/brain)" = "2770"
check "owner in kit group" sh -c "id -nG $OWNER | grep -qw pskit"

section "idempotent re-run"
out="$(pskit install --non-interactive --answers "$WORK/answers.json" 2>&1 || true)"
echo "$out" | tail -n 5
if echo "$out" | grep -q "  ok  done"; then bad "second run applied something"; else ok "second run changed nothing"; fi
check "second run exit 0" pskit install --non-interactive --answers "$WORK/answers.json"

section "doctor"
pskit doctor || true
check "doctor has no critical item" pskit doctor
check "doctor --json parses" sh -c "sudo PYTHONPATH=$REPO python3 -m pskit doctor --json | python3 -c 'import json,sys; json.load(sys.stdin)'"
check "audit runs" pskit doctor --audit --json

section "notify through the spool"
sudo -u "$OWNER" -H /usr/local/bin/pskit notify warn "e2e test alert" "from the owner account"
sleep 3
check "spool drained by the path unit" sh -c "test -z \"\$(sudo ls /var/spool/pskit/notify)\""
check "outsider cannot notify" sh -c "! sudo -u nobody /usr/local/bin/pskit notify warn x"

section "brain slot"
sudo mkdir -p "/srv/$NAME/brain/app"
sudo cp "$REPO/tests/e2e/fakebrain/"*.py "/srv/$NAME/brain/app/"
sudo chown -R pskit-brain:pskit-brain "/srv/$NAME/brain/app"
check "manifest valid" pskit brain validate "$REPO/tests/e2e/fakebrain/manifest.json"
pskit brain apply "$REPO/tests/e2e/fakebrain/manifest.json"
check "brain query active" systemctl is-active --quiet pskit-brain-query.service
check "brain runs in its slice" test "$(systemctl show -p Slice --value pskit-brain-query.service)" = "pskit-brain.slice"
check "brain runs as its user" sh -c "curl -fsS 127.0.0.1:8799/health | python3 -c 'import json,sys; assert json.load(sys.stdin)[\"user\"] == $(id -u pskit-brain)'"
check "brain timer active" systemctl is-active --quiet pskit-brain-capture.timer
sudo systemctl start pskit-brain-capture.service
check "batch job wrote its state" sudo test -f "/srv/$NAME/brain/state/last-capture.txt"
# Declared peak 768 MiB, plus 25%, never below 1 GiB.
check "reservation follows declared peak" test "$(systemctl show -p MemoryMax --value pskit-brain.slice)" = "$((1024 * 1024 * 1024))"
check "proof with brain e2e" pskit prove --no-reboot

section "pair a second user as the laptop, over real SSH"
sudo useradd --create-home --shell /bin/bash laptop
# The laptop has its own copy of the kit (the checkout is not readable by
# other users, as on a real machine).
LAPTOP_SRC="$(mktemp -d)"
cp -r "$REPO/pskit" "$LAPTOP_SRC/"
chmod -R a+rX "$LAPTOP_SRC"
sudo -u laptop -H mkdir -p /home/laptop/.claude/projects/-Users-laptop-demo
echo '{"type":"user","text":"hello from the laptop"}' | sudo -u laptop -H tee /home/laptop/.claude/projects/-Users-laptop-demo/s1.jsonl >/dev/null
( pskit pair-serve --code TEST-2345 --bind 127.0.0.1 --ttl 120 > "$WORK/pair-serve.log" 2>&1 & )
sleep 2
cat > "$WORK/device.json" <<JSON
{"language": "pt", "role": "device", "tailscale": "skip", "server_address": "127.0.0.1",
 "device_name": "laptop", "pair_code": "test-2345", "install_agents": false, "no_passphrase": true}
JSON
chmod 644 "$WORK/device.json"
if ! sudo -u laptop -H env PYTHONPATH="$LAPTOP_SRC" /usr/bin/python3 -B -m pskit install --non-interactive \
    --answers "$WORK/device.json"; then
  sudo find /home/laptop/.ssh -exec stat -c '%a %U:%G %n' {} \;
  sudo getfacl -R /home/laptop/.ssh 2>/dev/null | head -n 40 || true
  cat "$WORK/pair-serve.log"
  exit 1
fi
cat "$WORK/pair-serve.log"
INBOX="/srv/$NAME/brain/inbox/sessions/laptop/claude/projects/-Users-laptop-demo/s1.jsonl"
check "session delivered through the bridge key" sudo grep -q "hello from the laptop" "$INBOX"
check "delivered file readable by the brain group" test "$(sudo stat -c %G "$INBOX")" = "pskit-brain"
check "owner key logs in" sudo -u laptop -H timeout 20 ssh -o BatchMode=yes "$NAME" true
# The tunnel host has SessionType none (like -N); ask for a session anyway to
# prove the key cannot run a command even when one is requested.
out="$(sudo -u laptop -H timeout 20 ssh -o BatchMode=yes -o ClearAllForwardings=yes -o SessionType=default \
  "$NAME-tunnel" id 2>&1 || true)"
if echo "$out" | grep -q "uid="; then bad "tunnel key ran a command"; else ok "tunnel key cannot run commands"; fi
out="$(sudo -u laptop -H timeout 20 ssh -n -o BatchMode=yes "$NAME-bridge" id 2>&1 || true)"
if echo "$out" | grep -q "uid="; then bad "bridge key ran a command"; else ok "bridge key cannot run commands"; fi
# Forwarding tests use the tunnel key with explicit options: the alias sets
# LocalForward 8799, which is taken here by the fake brain itself, and
# ClearAllForwardings would also clear the -L/-R given below.
KD=/home/laptop/.ssh/pskit/$NAME
TUN=(sudo -u laptop -H timeout 20 ssh -F /dev/null -i "$KD/tunnel" -o IdentitiesOnly=yes
     -o UserKnownHostsFile="$KD/known_hosts" -o StrictHostKeyChecking=yes -o BatchMode=yes
     -o ExitOnForwardFailure=yes)
"${TUN[@]}" -f -N -L 127.0.0.1:28799:127.0.0.1:8799 "$OWNER@127.0.0.1"
sleep 1
check "tunnel reaches the brain" curl -fsS 127.0.0.1:28799/health
"${TUN[@]}" -f -N -L 127.0.0.1:28022:127.0.0.1:22 "$OWNER@127.0.0.1" || true
sleep 1
check "tunnel is listening locally for the refused port" sh -c "ss -Hltn 'sport = :28022' | grep -q ."
check "tunnel refuses other ports" sh -c "! timeout 5 bash -c 'echo | nc -w 3 127.0.0.1 28022 | grep -q SSH'"
set +e
"${TUN[@]}" -N -R 127.0.0.1:34567:127.0.0.1:22 "$OWNER@127.0.0.1" >/dev/null 2>&1
rc=$?
set -e
if [ "$rc" -ne 0 ] && [ "$rc" -ne 124 ]; then ok "tunnel refuses remote listens"; else bad "tunnel refuses remote listens (rc=$rc)"; fi
sudo pkill -u laptop -f "ssh -F /dev/null" || true
echo '{"more":1}' | sudo -u laptop -H tee -a /home/laptop/.claude/projects/-Users-laptop-demo/s1.jsonl >/dev/null
sudo -u laptop -H env PYTHONPATH="$LAPTOP_SRC" /usr/bin/python3 -B -m pskit bridge-push --server "$NAME"
check "append delivered as a tail" sudo grep -q '"more":1' "$INBOX"
# No Tailscale in CI, so the laptop's doctor reports it as critical (exit 1);
# check what it says about the pairing instead.
# shellcheck disable=SC2024  # the output file belongs to the test runner, on purpose
sudo -u laptop -H env PYTHONPATH="$LAPTOP_SRC" /usr/bin/python3 -B -m pskit doctor --json > "$WORK/device-doctor.json" || true
check "device doctor: host key pinned" python3 -c "import json; d={i['key']: i['level'] for i in json.load(open('$WORK/device-doctor.json'))}; assert d['device:$NAME:known-hosts'] == 'ok'"
check "device doctor: bridge delivered" python3 -c "import json; d={i['key']: i['level'] for i in json.load(open('$WORK/device-doctor.json'))}; assert d['device:$NAME:bridge'] == 'ok'"

section "server's own sessions"
sudo -u "$OWNER" -H mkdir -p "$HOME/.claude/projects/-srv-demo"
echo '{"server":1}' > "$HOME/.claude/projects/-srv-demo/s2.jsonl"
sudo systemctl start pskit-local-bridge.service
check "server sessions delivered" sudo test -f "/srv/$NAME/brain/inbox/sessions/$NAME/claude/projects/-srv-demo/s2.jsonl"

section "health, digest, reaper, cache"
check "health run" sudo systemctl start pskit-health.service
check "digest run" sudo systemctl start pskit-digest.service
check "reaper run" sudo systemctl start pskit-reaper.service
check "cache run" sudo systemctl start pskit-cache.service
check "backup run" sudo systemctl start pskit-backup.service

section "diagnostic file"
pskit doctor --bundle
diag="$(find "$HOME" -maxdepth 1 -name 'pskit-diagnostic-*.json' -printf '%T@ %p\n' | sort -rn | head -n 1 | cut -d' ' -f2-)"
check "diagnostic has no backup password" sh -c "! sudo grep -qF \"\$(sudo cat /etc/pskit/secrets/restic.pass)\" $diag"

section "uninstall"
pskit uninstall --confirm-name "$NAME"
left="$(systemctl list-units --all --plain --no-legend 'pskit-*' 'pskit.slice' || true)"
if [ -z "$left" ]; then ok "no kit units left"; else bad "no kit units left: $left"; fi
check "config removed" test ! -e /etc/pskit
check "code removed" test ! -e /opt/pskit
check "data kept" sudo test -f "/srv/$NAME/brain/state/last-capture.txt"
check "lockdown kept by default" test -f /etc/ssh/sshd_config.d/10-pskit.conf
check "firewall still on" sudo sh -c "ufw status | grep -q 'Status: active'"
check "brain user removed" sh -c "! id pskit-brain"

echo
echo "passed: $PASS  failed: $FAIL"
[ "$FAIL" -eq 0 ]
