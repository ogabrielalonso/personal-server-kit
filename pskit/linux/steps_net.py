"""Scenario B, access: Tailscale, pairing with the laptop, lockdown."""

from __future__ import annotations

import os
from typing import Optional

from .. import INSTALL_ONE_LINER
from .. import lockdown as L
from ..checks import tailscale_self
from ..common import save_configs
from ..i18n import t
from ..server import pairing
from ..steps import Context, Step, StepFailed
from ..ui import per_minute
from . import apt
from . import systemd as sd


LOGIN_MINUTES = 15


class Tailscale(Step):
    id = "tailscale"
    title = "step.tailscale"

    def applies(self, ctx: Context) -> bool:
        return ctx.answers.get("tailscale", "interactive") != "skip"

    def check(self, ctx: Context) -> bool:
        if not apt.installed(ctx.host, "tailscale"):
            return False
        me = tailscale_self(ctx.host)
        if me.get("state") != "Running" or not me.get("ip"):
            return False
        if ctx.kit.tailnet_ip != me["ip"] or ctx.kit.tailnet_name != me.get("dns", ""):
            ctx.kit.tailnet_ip, ctx.kit.tailnet_name = me["ip"], me.get("dns", "")
            save_configs(ctx)
        return True

    def apply(self, ctx: Context) -> None:
        h = ctx.host
        if not apt.installed(h, "tailscale"):
            apt.add_tailscale_repo(h, ctx.ledger, ctx.facts.get("codename") or "noble")
            apt.install(h, ctx.ledger, ["tailscale"])
        sd.enable_now(h, ctx.ledger, "tailscaled.service")
        me = tailscale_self(h)
        if me.get("state") != "Running":
            args = ["tailscale", "up", f"--hostname={ctx.cfg.machine_name}", f"--timeout={LOGIN_MINUTES}m"]
            key = os.environ.get("TS_AUTHKEY", "")
            if key:
                # Through a root-only file: a key on the command line is
                # visible to every user in the process list.
                keyfile = "/etc/pskit/secrets/ts-authkey"
                h.write_atomic(keyfile, key, mode=0o600)
                try:
                    r = h.run(args + [f"--auth-key=file:{h.p(keyfile)}"], timeout=1000)
                finally:
                    h.remove(keyfile)
                if not r.ok:
                    raise StepFailed(self.id, t("tailscale.authkey_failed"))
            else:
                ctx.ui.box([t("tailscale.login_1"), t("tailscale.login_2"),
                            t("tailscale.login_3", minutes=LOGIN_MINUTES)])
                if h.runner.interactive(args) != 0:
                    raise StepFailed(self.id, t("tailscale.login_failed"), t("tailscale.login_hint"))
        me = tailscale_self(h)
        ctx.kit.tailnet_ip, ctx.kit.tailnet_name = me.get("ip", ""), me.get("dns", "")
        save_configs(ctx)
        # Node keys expire after 180 days by default; an expired server
        # drops off the private network and only the provider console is
        # left. This cannot be changed without an admin API key, so the
        # owner does it once in the admin console.
        ctx.ui.box([t("tailscale.expiry_1"), t("tailscale.expiry_2", name=ctx.cfg.machine_name),
                    "https://login.tailscale.com/admin/machines"])
        if not ctx.ui.yes_no("tailscale_key_expiry_disabled", t("tailscale.expiry_confirm"), default=True):
            ctx.ui.warn(t("tailscale.expiry_later"))

    def verify(self, ctx: Context) -> Optional[str]:
        me = tailscale_self(ctx.host)
        if me.get("state") != "Running" or not me.get("ip"):
            return t("tailscale.not_running", state=me.get("state") or "?")
        return None


def decide_keep_rules(ctx: Context) -> list:
    """Asked before pairing, so that once the laptop is paired the lockdown
    follows at once and the laptop's confirmation window is not spent
    waiting on a question here."""
    stored = ctx.journal.data.get("keep_rules")
    if stored is not None:
        return list(stored)
    keep: list = []
    if ctx.answers.get("lockdown", "apply") != "skip":
        all_rules = L.existing_rules(ctx.host)
        rules = [r for r in all_rules if not L.ssh_rule(r)]
        dropped = [r for r in all_rules if L.ssh_rule(r)]
        if dropped:
            ctx.ui.info(t("lockdown.dropped_rules"))
            for r in dropped:
                ctx.ui.info(f"  {r}")
        if rules:
            ctx.ui.info(t("lockdown.existing_rules"))
            for r in rules:
                ctx.ui.info(f"  {r}")
            if ctx.ui.yes_no("keep_firewall_rules", t("lockdown.keep_rules"), default=True):
                keep = rules
    ctx.journal.data["keep_rules"] = keep
    ctx.journal.save()
    return keep


class Pairing(Step):
    id = "pairing"
    title = "step.pairing"

    def applies(self, ctx: Context) -> bool:
        return ctx.answers.get("pairing", "wait") != "skip"

    def check(self, ctx: Context) -> bool:
        import json
        raw = ctx.host.read_text("/var/lib/pskit/devices.json", "{}") or "{}"
        try:
            return bool(json.loads(raw))
        except json.JSONDecodeError:
            return False

    def apply(self, ctx: Context) -> None:
        ip = ctx.kit.tailnet_ip
        if not ip:
            raise StepFailed(self.id, t("pairing.no_tailnet"))
        decide_keep_rules(ctx)
        while True:
            code = pairing.new_code()
            session = pairing.PairingSession(ctx.host, ctx.ledger, ctx.cfg, ctx.kit, code)
            ctx.ui.box([
                t("pairing.box_1"),
                "",
                t("pairing.box_2"),
                "  " + INSTALL_ONE_LINER,
                t("pairing.box_3"),
                "",
                t("pairing.box_address", address=ip),
                t("pairing.box_code", code=code),
                "",
                t("pairing.box_4"),
            ])
            # The waiting line repeats the address and the code: they are what
            # the owner types on the laptop, and must not scroll away.
            def waiting(minutes: int, code: str = code) -> None:
                ctx.ui.info(t("pairing.waiting", minutes=minutes, address=ip, code=code))

            pairing.serve(session, ip, ctx.kit.pair_port, on_tick=per_minute(waiting))
            if session.result:
                ctx.ui.ok(t("pairing.paired", device=session.result["device"]))
                return
            reason = session.failed or "expired"
            ctx.ui.warn(t("pairing.failed", reason=reason))
            if not ctx.ui.yes_no("pairing_retry", t("pairing.retry"), default=True):
                raise StepFailed(self.id, t("pairing.not_paired"), t("pairing.hint"))


class Lockdown(Step):
    id = "lockdown"
    title = "step.lockdown"

    def applies(self, ctx: Context) -> bool:
        return ctx.answers.get("lockdown", "apply") != "skip"

    def check(self, ctx: Context) -> bool:
        if not L.is_applied(ctx.host):
            return False
        eff = L.ssh_effective(ctx.host)
        r = ctx.host.run(["ufw", "status"])
        return eff.get("passwordauthentication") == "no" and r.ok and "Status: active" in r.stdout

    def apply(self, ctx: Context) -> None:
        h = ctx.host
        mode = ctx.answers.get("lockdown", "apply")
        pending = L.read_pending(h)
        if pending is None:
            keep = decide_keep_rules(ctx)
            # The laptop confirms by itself only right after pairing; on a
            # second attempt the owner runs the command.
            ctx.ui.box([t("lockdown.box_1"), t("lockdown.box_2"), t("lockdown.box_3", minutes=L.REVERT_SECONDS // 60),
                        t("lockdown.box_manual"), f"  ssh {ctx.cfg.machine_name} pskit confirm-lockdown"])
            try:
                L.apply(h, ctx.ledger, ctx.cfg, ctx.kit, keep)
            except L.LockdownError as exc:
                raise StepFailed(self.id, t("lockdown.apply_failed", error=str(exc)))
        if mode == "test-local":
            if not ctx.kit.test_mode:
                raise StepFailed(self.id, "test-local lockdown needs test_mode")
            L.confirm(h, "", allow_local=True)
        else:
            ctx.ui.info(t("lockdown.waiting"))
        ok = L.wait_for_confirmation(
            h, L.REVERT_SECONDS - 20,
            on_tick=per_minute(lambda m: ctx.ui.info(t("lockdown.left", minutes=m))))
        if not ok:
            state = L.revert_if_due(h, ctx.cfg, now=float("inf"))
            raise StepFailed(self.id, t("lockdown.not_confirmed", state=state), t("lockdown.hint"))

    def verify(self, ctx: Context) -> Optional[str]:
        return None if self.check(ctx) else t("lockdown.not_effective")
