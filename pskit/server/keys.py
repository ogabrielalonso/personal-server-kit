"""The owner's authorized_keys: one managed line per device and role.

  human   the owner's own key, full shell
  tunnel  may only forward to the brain port on loopback; any command it
          asks for is replaced by /usr/bin/false ('restrict' alone does not
          stop command execution); remote listens must match
          127.0.0.1:1, so sshd refuses every real -R before binding anything
          ('port-forwarding' re-enables -R too, and the key has no
          passphrase); unix-socket forwarding is off in the sshd drop-in
  bridge  may only run the session receiver for that device

Lines carry a 'pskit:<device>:<role>' comment, so re-pairing a device
replaces its lines and uninstall removes exactly what the kit added.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from ..paths import SYS
from ..state import Ledger
from ..system import Host

KEY_RE = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ssh-rsa|"
                    r"sk-ssh-ed25519@openssh\.com) ([A-Za-z0-9+/]+={0,3})(?: .*)?$")
ROLES = ("human", "tunnel", "bridge")


class KeyError_(ValueError):
    pass


def parse_key(line: str, require_ed25519: bool = False) -> str:
    m = KEY_RE.match((line or "").strip())
    if not m:
        raise KeyError_("not an SSH public key")
    if require_ed25519 and m.group(1) != "ssh-ed25519":
        raise KeyError_("this key must be ed25519")
    if len(m.group(2)) > 1200:
        raise KeyError_("key too long")
    return f"{m.group(1)} {m.group(2)}"


def options_for(role: str, device: str, brain_port: int, shim: str = SYS.shim) -> str:
    if role == "tunnel":
        return (f'restrict,port-forwarding,permitopen="127.0.0.1:{brain_port}",'
                f'permitlisten="127.0.0.1:1",command="/usr/bin/false"')
    if role == "bridge":
        return f'restrict,command="{shim} bridge-receive --device {device}"'
    return ""


def managed_line(role: str, device: str, key: str, brain_port: int) -> str:
    opts = options_for(role, device, brain_port)
    comment = f"pskit:{device}:{role}"
    return f"{opts} {key} {comment}" if opts else f"{key} {comment}"


def authorized_keys_path(host: Host, owner: str) -> str:
    home = host.user_home(owner)
    if not home:
        raise KeyError_(f"user {owner} has no home directory")
    return f"{home}/.ssh/authorized_keys"


def install_device(host: Host, ledger: Ledger, owner: str, device: str, keys: Dict[str, str],
                   brain_port: int, owner_group: Optional[str] = None) -> List[str]:
    path = authorized_keys_path(host, owner)
    ssh_dir = path.rsplit("/", 1)[0]
    host.mkdir(ssh_dir, mode=0o700, owner=owner, group=owner_group or owner)
    current = (host.read_text(path, "") or "").splitlines()
    ours = tuple(f"pskit:{device}:{r}" for r in ROLES)
    kept = [ln for ln in current if not ln.rstrip().endswith(ours)]
    added = []
    for role in ROLES:
        if keys.get(role):
            added.append(managed_line(role, device, keys[role], brain_port))
    content = "\n".join(kept + added).strip("\n") + "\n"
    host.write_atomic(path, content, mode=0o600, owner=owner, group=owner_group or owner)
    if not ledger.has("authorized_keys", device=device):
        ledger.add("authorized_keys", path=path, device=device)
    return added


def remove_device(host: Host, path: str, device: str) -> int:
    current = (host.read_text(path, "") or "").splitlines()
    kept = [ln for ln in current if not any(ln.rstrip().endswith(f"pskit:{device}:{r}") for r in ROLES)]
    if len(kept) != len(current):
        st = host.p(path).stat()
        host.write_atomic(path, "\n".join(kept) + ("\n" if kept else ""), mode=0o600)
        if host.real:
            import os
            os.chown(host.p(path), st.st_uid, st.st_gid)
    return len(current) - len(kept)


def host_public_keys(host: Host) -> List[str]:
    out = []
    for kind in ("ed25519", "ecdsa", "rsa"):
        raw = host.read_text(f"/etc/ssh/ssh_host_{kind}_key.pub")
        if raw:
            parts = raw.split()
            if len(parts) >= 2:
                out.append(f"{parts[0]} {parts[1]}")
    return out
