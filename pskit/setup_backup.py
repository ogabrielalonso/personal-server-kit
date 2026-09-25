"""Backup setup, shared by the server scenarios.

The default is a destination that never depends on a laptop being awake
(decision D3): S3-compatible storage, or another machine the owner has
over SFTP. A disk on the same machine is offered, with a plain warning.
"""

from __future__ import annotations

import bz2
import hashlib
import platform
import secrets
import urllib.request
from typing import Dict

from .checks import receipt
from .checks import tailnet_address_of, tailnet_policy_blocks
from .common import save_configs
from .config import env_file, read_env_file
from .i18n import t
from .paths import SYS
from .runtime import backup as BK
from .state import install_file
from .steps import Context, StepFailed
from .text import tail_text

RESTIC_VERSION = "0.19.1"
RESTIC_SHA256 = {
    ("linux", "amd64"): "f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c",
    ("linux", "arm64"): "a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465",
}


def restic_target() -> tuple:
    arch = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(arch, arch)
    return "linux", arch


def restic_installed(ctx: Context) -> bool:
    if not ctx.host.exists(SYS.restic):
        return False
    r = ctx.host.run([str(ctx.host.p(SYS.restic)), "version"])
    return r.ok and RESTIC_VERSION in r.stdout


def install_restic(ctx: Context) -> None:
    if restic_installed(ctx):
        return
    key = restic_target()
    expected = RESTIC_SHA256.get(key)
    if not expected:
        raise StepFailed("backup", t("backup.restic_unsupported", target="_".join(key)))
    url = (f"https://github.com/restic/restic/releases/download/v{RESTIC_VERSION}/"
           f"restic_{RESTIC_VERSION}_{key[0]}_{key[1]}.bz2")
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - pinned URL, checksum below
        packed = resp.read()
    if hashlib.sha256(packed).hexdigest() != expected:
        raise StepFailed("backup", t("backup.restic_checksum"))
    install_file(ctx.host, ctx.ledger, SYS.restic, bz2.decompress(packed), mode=0o755)


def repo_hint(ctx: Context) -> str:
    """An SFTP destination on the tailnet that Tailscale reaches but SSH does
    not is blocked by the tailnet's access rules, not by a wrong key."""
    repo = read_env_file(ctx.host, BK.ENV_FILE).get("RESTIC_REPOSITORY", "")
    if repo.startswith("sftp:") and "@" in repo:
        target = repo[5:].split("@", 1)[1].split(":", 1)[0]
        ip = tailnet_address_of(target)
        if ip and tailnet_policy_blocks(ctx.host, ip, 22):
            return t("backup.tailnet_policy", address=target, me=ctx.kit.tailnet_ip or "?")
    return t("backup.repo_hint")


SFTP_KEY_OPTIONS = 'restrict,command="internal-sftp"'


def ask_destination(ctx: Context) -> Dict[str, str]:
    ui = ctx.ui
    kind = ui.choice("backup_kind", t("backup.choose"), [
        ("s3", t("backup.opt_s3")),
        ("sftp", t("backup.opt_sftp")),
        ("local", t("backup.opt_local")),
        ("later", t("backup.opt_later")),
    ], default="s3")
    env: Dict[str, str] = {"kind": kind}
    if kind == "s3":
        ui.info(t("backup.s3_help"))
        env["RESTIC_REPOSITORY"] = ui.text(
            "backup_repository", t("backup.s3_repo"),
            validate=lambda v: None if v.startswith("s3:https://") and v.count("/") >= 4 else t("backup.s3_bad"))
        env["AWS_ACCESS_KEY_ID"] = ui.secret("backup_key_id", t("backup.s3_key_id"), env="AWS_ACCESS_KEY_ID")
        env["AWS_SECRET_ACCESS_KEY"] = ui.secret("backup_secret", t("backup.s3_secret"), env="AWS_SECRET_ACCESS_KEY")
    elif kind == "sftp":
        target = ui.text("backup_repository", t("backup.sftp_target"),
                         validate=lambda v: None if "@" in v and ":/" in v else t("backup.sftp_bad"))
        keyfile = f"{SYS.secrets}/backup_ed25519"
        if not ctx.host.exists(keyfile):
            ctx.host.check(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                            f"pskit-backup@{ctx.cfg.machine_name}", "-f", str(ctx.host.p(keyfile))])
        pub = ctx.host.read_text(keyfile + ".pub", "") or ""
        ui.box([t("backup.sftp_key_1"), t("backup.sftp_key_2")])
        # On its own line, outside the box: copied from a terminal, a key the
        # box wrapped and framed with borders is no longer a valid key.
        # The options limit the key, on the target, to the file transfer
        # restic uses: sshd serves internal-sftp itself (any OpenSSH, no
        # path to guess) and refuses commands, terminals and tunnels.
        ui.say("")
        ui.say(SFTP_KEY_OPTIONS + " " + pub.strip())
        ui.say("")
        ui.pause(t("ui.press_enter"))
        env["RESTIC_REPOSITORY"] = f"sftp:{target}"
        env["PSKIT_SFTP_ARGS"] = (f"-i {ctx.host.p(keyfile)} -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
                                  f"-o UserKnownHostsFile={ctx.host.p(SYS.secrets)}/backup_known_hosts")
    elif kind == "local":
        ui.warn(t("backup.local_warning"))
        path = ui.text("backup_repository", t("backup.local_path"),
                       validate=lambda v: None if v.startswith("/") else t("backup.local_bad"))
        env["RESTIC_REPOSITORY"] = path
    return env


def password_ceremony(ctx: Context) -> str:
    """The password is the only way to read the backup. The owner must
    prove they stored it before the install continues."""
    existing = ctx.host.read_text(BK.PASS_FILE)
    if existing:
        return existing.strip()
    pw = secrets.token_urlsafe(24)
    ctx.ui.box([t("backup.pw_1"), "", "    " + pw, "", t("backup.pw_2"), t("backup.pw_3")])
    if ctx.ui.interactive and "backup_password_saved" not in ctx.answers:
        for _ in range(3):
            typed = ctx.ui.text("backup_password_check", t("backup.pw_check"), validate=lambda v: None)
            if typed.strip() == pw[:6]:
                break
            ctx.ui.warn(t("backup.pw_mismatch"))
        else:
            raise StepFailed("backup", t("backup.pw_not_saved"))
    install_file(ctx.host, ctx.ledger, BK.PASS_FILE, pw + "\n", mode=0o600)
    return pw


def configure(ctx: Context) -> None:
    env = ask_destination(ctx)
    kind = env.pop("kind")
    ctx.kit.backup_kind = kind
    if kind == "later":
        save_configs(ctx)
        ctx.ui.warn(t("backup.later_warning"))
        return
    install_restic(ctx)
    ctx.host.mkdir("/var/cache/pskit/restic", mode=0o700)
    install_file(ctx.host, ctx.ledger, BK.ENV_FILE, env_file(env), mode=0o600)
    password_ceremony(ctx)
    try:
        BK.ensure_repo(ctx.host)
    except BK.BackupError as exc:
        raise StepFailed("backup", t("backup.repo_failed", error=tail_text(str(exc))), repo_hint(ctx))
    save_configs(ctx)
    ctx.ui.info(t("backup.first_run"))
    try:
        BK.run_backup(ctx.host, ctx.cfg, ctx.kit)
    except BK.BackupError as exc:
        raise StepFailed("backup", t("backup.first_failed", error=tail_text(str(exc))))
    rt = BK.restore_test(ctx.host, ctx.cfg, ctx.kit)
    if not rt.get("ok"):
        raise StepFailed("backup", t("backup.restore_failed", error=str(rt.get("error", rt.get("mismatches")))))
    ctx.ui.ok(t("backup.proven", n=rt.get("checked", 0)))


def configured(ctx: Context) -> bool:
    if ctx.kit.backup_kind == "later":
        return "backup_kind" in ctx.answers and ctx.answers["backup_kind"] == "later"
    if ctx.kit.backup_kind in ("s3", "sftp", "local"):
        return restic_installed(ctx) and ctx.host.exists(BK.ENV_FILE) and ctx.host.exists(BK.PASS_FILE) \
            and bool(receipt(ctx.host, "backup-success")) and bool((receipt(ctx.host, "restore-test") or {}).get("ok"))
    return False
