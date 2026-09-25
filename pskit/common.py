"""Pieces shared by the server scenarios."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from . import VERSION
from .paths import SYS
from .state import install_file
from .steps import Context


def save_configs(ctx: Context) -> None:
    install_file(ctx.host, ctx.ledger, SYS.host_config, ctx.cfg.to_json(), mode=0o644)
    install_file(ctx.host, ctx.ledger, SYS.kit_config, ctx.kit.to_json(), mode=0o644)


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def release_dir(version: str = VERSION) -> str:
    return f"{SYS.releases}/{version}"


def install_code(ctx: Context, python: str = "") -> None:
    """Copies this package to an immutable, versioned release and points
    the `current` link and the `pskit` command at it. A later version is a
    new directory; rolling back is moving the link."""
    host = ctx.host
    dest = release_dir()
    target = host.p(f"{dest}/pskit")
    if not target.exists():
        tmp = host.p(f"{dest}/.pskit.tmp")
        if tmp.exists():
            shutil.rmtree(tmp)
        shutil.copytree(package_dir(), tmp, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for root, dirs, files in os.walk(tmp):
            for d in dirs:
                os.chmod(os.path.join(root, d), 0o755)  # noqa: S103 - code is world-readable on purpose
            for f in files:
                os.chmod(os.path.join(root, f), 0o644)
        os.replace(tmp, target)
        os.chmod(host.p(dest), 0o755)  # noqa: S103
        if not ctx.ledger.has("dir_created", path=SYS.opt):
            ctx.ledger.add("dir_created", path=SYS.opt, keep=False)
    host.symlink(dest, SYS.current)
    py = python or "/usr/bin/python3"
    shim = (
        "#!/bin/sh\n"
        "# Managed by pskit. Runs the current release with the system Python.\n"
        f"PYTHONPATH={SYS.current} exec {py} -B -m pskit \"$@\"\n"
    )
    install_file(host, ctx.ledger, SYS.shim, shim, mode=0o755)


def code_installed(ctx: Context) -> bool:
    host = ctx.host
    return host.exists(f"{release_dir()}/pskit/__init__.py") and host.exists(SYS.shim) and \
        os.path.realpath(host.p(SYS.current)) == os.path.realpath(host.p(release_dir()))
