"""apt helpers. Waits for the dpkg lock instead of failing: on a fresh VPS,
unattended-upgrades often holds it for the first minutes after boot."""

from __future__ import annotations

from typing import List

from ..state import Ledger, install_file
from ..system import Host

APT_ENV = {"DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "a"}
LOCK_WAIT = ["-o", "DPkg::Lock::Timeout=900"]

TAILSCALE_KEYRING = "/usr/share/keyrings/tailscale-archive-keyring.gpg"
TAILSCALE_LIST = "/etc/apt/sources.list.d/tailscale.list"
TAILSCALE_KEY_URL = "https://pkgs.tailscale.com/stable/ubuntu/{codename}.noarmor.gpg"
TAILSCALE_REPO = ("deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] "
                  "https://pkgs.tailscale.com/stable/ubuntu {codename} main\n")


def installed(host: Host, pkg: str) -> bool:
    r = host.run(["dpkg-query", "-W", "-f=${Status}", pkg])
    return r.ok and "install ok installed" in r.stdout


def update(host: Host) -> None:
    host.check(["apt-get"] + LOCK_WAIT + ["update"], env=APT_ENV, timeout=1200)


def install(host: Host, ledger: Ledger, pkgs: List[str]) -> List[str]:
    missing = [p for p in pkgs if not installed(host, p)]
    if not missing:
        return []
    update(host)
    host.check(["apt-get"] + LOCK_WAIT + ["install", "-y", "--no-install-recommends"] + missing,
               env=APT_ENV, timeout=1800)
    for p in missing:
        ledger.add("package_installed", name=p)
    return missing


def add_tailscale_repo(host: Host, ledger: Ledger, codename: str) -> None:
    import urllib.request
    url = TAILSCALE_KEY_URL.format(codename=codename)
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 - fixed https URL
        key = resp.read()
    if len(key) < 100:
        raise RuntimeError("Tailscale signing key looks wrong")
    install_file(host, ledger, TAILSCALE_KEYRING, key, mode=0o644)
    install_file(host, ledger, TAILSCALE_LIST, TAILSCALE_REPO.format(codename=codename), mode=0o644)
