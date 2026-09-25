"""`pskit pair` (run on the laptop): connect this computer to a server.

1. Tailscale must be running here (the only path to the server).
2. The owner types the server address and the code shown on its screen.
3. Three dedicated keys are created: one for the owner, one that can only
   open the brain tunnel, one that can only deliver sessions.
4. The server's host keys come back through the pairing and are pinned.
5. The tunnel and the session bridge are installed as background jobs.
6. The laptop proves it can still get in over the private network, which
   confirms the server's lockdown (otherwise it reverts on its own).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Optional

from .. import VERSION
from ..checks import tailnet_address_of, tailnet_policy_blocks, tailscale_self, tailscale_state
from ..config import DEVICE_RE, slug
from ..i18n import t
from ..system import Host
from ..ui import UI
from . import agents
from . import tailscale_setup as ts
from .local import Paths, ensure_include, save_server, ssh_host_block


class PairError(RuntimeError):
    pass


class Unreachable(PairError):
    pass


def is_mac() -> bool:
    return platform.system() == "Darwin"


def default_device_name() -> str:
    return slug(socket.gethostname().split(".")[0], "laptop")


def keygen(path: Path, comment: str, passphrase_prompt: bool = False) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args = ["ssh-keygen", "-q", "-t", "ed25519", "-C", comment, "-f", str(path)]
    if passphrase_prompt:
        rc = subprocess.call(args)
    else:
        rc = subprocess.run(args + ["-N", ""], capture_output=True).returncode
    if rc != 0 or not path.exists():
        raise PairError(f"ssh-keygen failed for {path.name}")
    lock_down_key(path)


def lock_down_key(path: Path) -> None:
    """ssh ignores a private key that group or others can read. ssh-keygen
    creates it 0600, but a default ACL on the home folder can still widen
    the reported mode; an explicit chmod also narrows the ACL mask."""
    os.chmod(path, 0o600)
    pub = path.with_name(path.name + ".pub")
    if pub.exists():
        os.chmod(pub, 0o644)


def install_code(paths: Paths) -> None:
    """Copy of this package for the background jobs (versioned, like the
    server's)."""
    src = Path(__file__).resolve().parents[1]
    dest = paths.share / "releases" / VERSION / "pskit"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    link = paths.share / "current"
    tmp = paths.share / ".current.new"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(dest.parent, tmp)
    os.replace(tmp, link)
    paths.bin.parent.mkdir(parents=True, exist_ok=True)
    paths.bin.write_text(f'#!/bin/sh\nPYTHONPATH="{link}" exec "{sys.executable}" -B -m pskit "$@"\n')
    os.chmod(paths.bin, 0o755)  # noqa: S103 - an executable launcher


def post_pair(address: str, port: int, body: Dict, timeout: float = 20) -> Dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(f"http://{address}:{port}/pair", data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - tailnet address given by owner
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode())
        except Exception:
            detail = {}
        raise PairError(detail.get("error", f"HTTP {exc.code}") +
                        (f" ({detail['attempts_left']} left)" if "attempts_left" in detail else ""))
    except (urllib.error.URLError, OSError):
        raise Unreachable(t("device.unreachable", address=address, port=port))


def diagnose_unreachable(host: Host, address: str, port: int, exc: PairError) -> PairError:
    """Tailscale reaching the server while the connection times out means the
    tailnet's access rules block it: say so, with both addresses to allow."""
    ip = tailnet_address_of(address)
    if ip and tailnet_policy_blocks(host, ip, port):
        return PairError(t("device.tailnet_policy", address=address, me=tailscale_self(host).get("ip") or "?"))
    return exc


def write_known_hosts(keydir: Path, info: Dict) -> None:
    names = [info["tailnet_ip"]]
    if info.get("tailnet_name"):
        names.append(info["tailnet_name"])
    lines = [f"{','.join(names)} {k}" for k in info["host_keys"]]
    (keydir / "known_hosts").write_text("\n".join(lines) + "\n")
    os.chmod(keydir / "known_hosts", 0o600)


def wait_tailscale(host: Host, ui: UI, timeout_s: float = 900) -> None:
    if tailscale_state(host) == "Running":
        return
    if ts.can_install(host) and (ui.interactive or os.environ.get("TS_AUTHKEY")):
        try:
            if not host.which("tailscale") and ui.yes_no("tailscale_install", t("device.ts_install_ask"), default=True):
                ui.info(t("device.ts_installing"))
                ts.install(host, host.os_release().get("VERSION_CODENAME") or "noble")
                ui.ok(t("device.ts_installed"))
            if host.which("tailscale"):
                ts.login(host, ui, default_device_name())
        except ts.TailscaleSetupError as exc:
            raise PairError(t("device.ts_setup_failed", error=str(exc)))
        if tailscale_state(host) == "Running":
            ui.ok(t("device.ts_ok"))
            return
    ui.box([t("device.ts_1"), t("device.ts_2"), "https://tailscale.com/download"])
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if tailscale_state(host) == "Running":
            ui.ok(t("device.ts_ok"))
            return
        time.sleep(5)
    raise PairError(t("device.ts_timeout"))


def confirm_lockdown(server: str, ui: UI, timeout_s: float = 600) -> str:
    """Logs in over the private network as the owner and confirms. This
    first real login also proves the pinned host key and the owner key."""
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        r = subprocess.run(["ssh", server, "/usr/local/bin/pskit", "confirm-lockdown", "--wait", "300"],
                           capture_output=True, text=True)
        out = (r.stdout or "").strip().splitlines()
        err = [ln for ln in (r.stderr or "").splitlines() if ln.strip() and not ln.startswith("@")]
        last = out[-1] if out else " / ".join(err[-4:])[-400:]
        if r.returncode == 0:
            return last
        time.sleep(5)
    raise PairError(t("device.confirm_failed", detail=last))


def confirmed_text(result: str) -> str:
    """The server answers with a status word; the owner reads a sentence."""
    if result in ("confirmed", "already-applied"):
        return t("device.confirmed")
    return t("device.confirmed_other", result=result)


def tunnel_listening(port: int, timeout_s: float = 45) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            time.sleep(2)
    return False


def run(ui: UI, host: Host, answers: Dict, home: Optional[str] = None) -> Dict:
    paths = Paths(home)
    mac = is_mac()
    if answers.get("tailscale", "interactive") != "skip":
        wait_tailscale(host, ui)
    address = ui.text("server_address", t("device.address"),
                      validate=lambda v: None if v and " " not in v else t("ui.required"))
    port = int(answers.get("pair_port", 47800))
    device = ui.text("device_name", t("device.name"), default=default_device_name(),
                     validate=lambda v: None if DEVICE_RE.match(v) else t("device.name_bad"))
    for _ in range(5):
        code = ui.text("pair_code", t("device.code"),
                       validate=lambda v: None if len(v.replace("-", "")) == 8 else t("device.code_bad"))
        # Keys go in a per-server folder named after the address until the
        # server tells us its name.
        staging = paths.ssh_kit / f".staging-{device}"
        staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        ask_passphrase = ui.interactive and not answers.get("no_passphrase", False)
        if ask_passphrase and not (staging / "id_ed25519").exists():
            # ssh-keygen asks in its own words, in English; say first what it is.
            ui.info(t("device.passphrase"))
        keygen(staging / "id_ed25519", f"pskit-{device}-owner", passphrase_prompt=ask_passphrase)
        keygen(staging / "tunnel", f"pskit-{device}-tunnel")
        keygen(staging / "bridge", f"pskit-{device}-bridge")
        body = {"code": code, "device": device, "os": f"{platform.system()} {platform.release()}",
                "keys": {k: (staging / f"{n}.pub").read_text().strip() for k, n in
                         (("human", "id_ed25519"), ("tunnel", "tunnel"), ("bridge", "bridge"))}}
        try:
            info = post_pair(address, port, body)
            break
        except Unreachable as exc:
            raise diagnose_unreachable(host, address, port, exc)
        except PairError as exc:
            if "wrong code" not in str(exc) or not ui.interactive:
                raise
            ui.warn(t("device.pair_failed", error=str(exc)))
    else:
        raise PairError(t("device.too_many"))
    server = info["machine_name"]
    keydir = paths.server_dir(server)
    if keydir.exists():
        shutil.rmtree(keydir)
    os.replace(staging, keydir)
    os.chmod(keydir, 0o700)
    for name in ("id_ed25519", "tunnel", "bridge"):
        lock_down_key(keydir / name)
    write_known_hosts(keydir, info)
    (keydir / "ssh_config").write_text(ssh_host_block(server, info, keydir, mac))
    os.chmod(keydir / "ssh_config", 0o600)
    ensure_include(paths)
    install_code(paths)
    record = {"server": server, "device": device, "address": address, "paired_at": time.time(),
              "brain_port": info.get("brain_port", 8799), "language": info.get("language", "en"),
              "owner": info["owner"], "tailnet_ip": info["tailnet_ip"], "server_version": info.get("server_version")}
    save_server(paths, server, record)
    ui.ok(t("device.paired", server=server))
    # Tunnel and session delivery first: they work over the tailnet whether
    # or not the server's lockdown is kept, so a slow confirmation below
    # never leaves the laptop half set up.
    if answers.get("install_agents", True):
        agents.install(paths, server, mac)
        if tunnel_listening(int(record["brain_port"])):
            ui.ok(t("device.tunnel_ok", port=record["brain_port"]))
        else:
            ui.warn(t("device.tunnel_not_yet"))
    if answers.get("first_push", True):
        from . import bridge_push
        # A small first batch: the server is waiting for the confirmation
        # below; the background job delivers the rest every 15 minutes.
        res = bridge_push.push(paths, server, budget=16 * 1024 * 1024, timeout=120)
        if res.get("ok"):
            ui.ok(t("device.bridge_ok", files=res.get("delivered", 0)))
        else:
            ui.warn(t("device.bridge_failed", error=res.get("error", "?")))
    if answers.get("confirm_lockdown", True):
        ui.info(t("device.confirming"))
        ui.ok(confirmed_text(confirm_lockdown(server, ui)))
    return record
