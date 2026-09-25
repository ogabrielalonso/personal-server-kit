"""Tailscale on the laptop. On Ubuntu the kit installs it from the same
signed repository the server uses (through sudo) and starts the login.
Elsewhere (macOS, other Linux) the owner installs the app and signs in:
on a Mac the app needs the owner's own approval of its VPN configuration."""

from __future__ import annotations

import os
import tempfile
import urllib.request

from ..i18n import t
from ..linux.apt import APT_ENV, LOCK_WAIT, TAILSCALE_KEY_URL, TAILSCALE_KEYRING, TAILSCALE_LIST, TAILSCALE_REPO
from ..system import Host
from ..ui import UI

LOGIN_MINUTES = 15


class TailscaleSetupError(RuntimeError):
    pass


def can_install(host: Host) -> bool:
    return host.platform == "linux" and host.os_release().get("ID") == "ubuntu" and bool(host.which("apt-get"))


def ensure_sudo(host: Host) -> None:
    """At most one password prompt, on the terminal; none with passwordless sudo."""
    if not host.run(["sudo", "-n", "true"]).ok and host.runner.interactive(["sudo", "-v"]) != 0:
        raise TailscaleSetupError("sudo")


def install(host: Host, codename: str) -> None:
    """At most one password prompt (none with passwordless sudo), then quiet
    steps. apt waits for the dpkg lock like on the server: a laptop's own
    updater often holds it."""
    with urllib.request.urlopen(TAILSCALE_KEY_URL.format(codename=codename), timeout=30) as resp:  # noqa: S310 - fixed https URL
        key = resp.read()
    if len(key) < 100:
        raise TailscaleSetupError("Tailscale signing key looks wrong")
    ensure_sudo(host)
    apt_env = ["env"] + [f"{k}={v}" for k, v in APT_ENV.items()]
    with tempfile.TemporaryDirectory() as tmp:
        keyfile, listfile = os.path.join(tmp, "ts-key.gpg"), os.path.join(tmp, "ts-repo.list")
        with open(keyfile, "wb") as fh:
            fh.write(key)
        with open(listfile, "w") as fh:
            fh.write(TAILSCALE_REPO.format(codename=codename))
        for args in (["install", "-m", "0644", keyfile, TAILSCALE_KEYRING],
                     ["install", "-m", "0644", listfile, TAILSCALE_LIST],
                     [*apt_env, "apt-get", *LOCK_WAIT, "update", "-q"],
                     [*apt_env, "apt-get", *LOCK_WAIT, "install", "-y", "-q", "tailscale"],
                     ["systemctl", "enable", "--now", "tailscaled"]):
            r = host.run(["sudo", "-n", *args], timeout=1900)
            if not r.ok:
                raise TailscaleSetupError(" ".join(args[:3]))


def login(host: Host, ui: UI, hostname: str) -> None:
    args = ["sudo", "tailscale", "up", f"--hostname={hostname}", f"--timeout={LOGIN_MINUTES}m"]
    key = os.environ.get("TS_AUTHKEY", "")
    if key:
        # Like the server: through a file, never on the command line, where
        # every user sees it in the process list. The folder is the owner's
        # (0700); tailscale reads it as root through sudo.
        ensure_sudo(host)
        with tempfile.TemporaryDirectory() as tmp:
            keyfile = os.path.join(tmp, "ts-authkey")
            with open(keyfile, "w") as fh:
                fh.write(key)
            r = host.run(["sudo", "-n", *args[1:], f"--auth-key=file:{keyfile}"], timeout=LOGIN_MINUTES * 60 + 60)
        if not r.ok:
            raise TailscaleSetupError(t("tailscale.authkey_failed"))
        return
    ui.box([t("tailscale.login_1"), t("device.ts_login_2"), t("tailscale.login_3", minutes=LOGIN_MINUTES)])
    rc = host.runner.interactive(args)
    if rc != 0:
        raise TailscaleSetupError(t("tailscale.login_failed"))
