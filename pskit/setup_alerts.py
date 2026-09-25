"""Alert channel setup, shared by the server scenarios.

Telegram: the owner creates a bot with @BotFather and pastes the token;
the chat id is captured automatically when they press Start. ntfy: a
random topic is generated; the owner subscribes to it in the ntfy app.
Either way a real test message is sent and the owner confirms it arrived.
"""

from __future__ import annotations

import json
import re
import secrets
import time
import urllib.parse
import urllib.request
from typing import Optional, Tuple

from . import notify
from .common import save_configs
from .config import env_file
from .i18n import t
from .paths import SYS
from .state import install_file
from .steps import Context, StepFailed

TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_-]{30,64}$")


def _tg(token: str, method: str, params: Optional[dict] = None, timeout: float = 15) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - fixed https host
        return json.loads(resp.read().decode("utf-8"))


def telegram_bot_name(token: str) -> Optional[str]:
    try:
        data = _tg(token, "getMe")
    except Exception:
        return None
    return data.get("result", {}).get("username") if data.get("ok") else None


def wait_for_chat(token: str, timeout_s: float = 240) -> Optional[str]:
    deadline = time.time() + timeout_s
    offset = None
    while time.time() < deadline:
        params = {"timeout": 20}
        if offset is not None:
            params["offset"] = offset
        try:
            data = _tg(token, "getUpdates", params, timeout=30)
        except Exception:
            time.sleep(3)
            continue
        for upd in data.get("result", []):
            offset = upd["update_id"] + 1
            chat = (upd.get("message") or {}).get("chat") or {}
            if chat.get("type") == "private" and chat.get("id"):
                return str(chat["id"])
    return None


def setup_telegram(ctx: Context) -> Tuple[dict, notify.Channel]:
    ui = ctx.ui
    ui.box([t("alerts.tg_1"), t("alerts.tg_2"), t("alerts.tg_3")])
    while True:
        token = ui.secret("telegram_token", t("alerts.tg_token"), env="PSKIT_TELEGRAM_TOKEN")
        if TOKEN_RE.match(token):
            name = telegram_bot_name(token)
            if name:
                break
        ui.warn(t("alerts.tg_bad_token"))
        if not ui.interactive:
            raise StepFailed("alerts", t("alerts.tg_bad_token"))
    chat = str(ctx.answers.get("telegram_chat_id", ""))
    if not chat:
        ui.box([t("alerts.tg_start", bot=name), f"https://t.me/{name}"])
        chat = wait_for_chat(token) or ""
        if not chat:
            raise StepFailed("alerts", t("alerts.tg_no_chat"), t("alerts.tg_no_chat_hint"))
    env = {"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": chat}
    return env, notify.TelegramChannel(token, chat)


def setup_ntfy(ctx: Context) -> Tuple[dict, notify.Channel]:
    server = str(ctx.answers.get("ntfy_server", "https://ntfy.sh")).rstrip("/")
    topic = str(ctx.answers.get("ntfy_topic", "")) or f"pskit-{ctx.cfg.machine_name}-{secrets.token_hex(8)}"
    ctx.ui.box([t("alerts.ntfy_1"), t("alerts.ntfy_2", server=server), t("alerts.ntfy_topic", topic=topic),
                t("alerts.ntfy_3")])
    ctx.ui.pause(t("ui.press_enter"))
    return {"NTFY_SERVER": server, "NTFY_TOPIC": topic}, notify.NtfyChannel(server, topic)


def configure(ctx: Context) -> None:
    ui = ctx.ui
    while True:
        choice = ui.choice("alert_channel", t("alerts.choose"), [
            ("telegram", t("alerts.opt_telegram")),
            ("ntfy", t("alerts.opt_ntfy")),
            ("none", t("alerts.opt_none")),
        ], default="telegram")
        if choice == "none":
            ui.warn(t("alerts.none_warning"))
            ctx.cfg.alert_channel = "none"
            save_configs(ctx)
            return
        env, channel = setup_telegram(ctx) if choice == "telegram" else setup_ntfy(ctx)
        text = notify.format_message(ctx.cfg, t("alerts.test_title"), t("alerts.test_body"))
        try:
            channel.send(text, "warn")
        except notify.DeliveryError as exc:
            ui.warn(t("alerts.test_failed", error=str(exc)))
            if not ui.interactive:
                raise StepFailed("alerts", t("alerts.test_failed", error=str(exc)))
            continue
        if ui.yes_no("alert_test_received", t("alerts.test_arrived"), default=True):
            install_file(ctx.host, ctx.ledger, notify.SECRETS_FILE, env_file(env), mode=0o600)
            ctx.cfg.alert_channel = choice
            save_configs(ctx)
            return
        ui.warn(t("alerts.try_again"))
        if not ui.interactive:
            raise StepFailed("alerts", t("alerts.try_again"))


def configure_heartbeat(ctx: Context) -> None:
    ui = ctx.ui
    ui.box([t("heartbeat.1"), t("heartbeat.2"), t("heartbeat.3")])
    if not ui.yes_no("heartbeat", t("heartbeat.ask"), default=True):
        ctx.kit.heartbeat_enabled = False
        save_configs(ctx)
        return
    while True:
        url = ui.secret("heartbeat_url", t("heartbeat.url"), env="PSKIT_HEARTBEAT_URL")
        if url.startswith("https://") and " " not in url:
            break
        ui.warn(t("heartbeat.bad_url"))
        if not ui.interactive:
            raise StepFailed("heartbeat", t("heartbeat.bad_url"))
    install_file(ctx.host, ctx.ledger, f"{SYS.secrets}/heartbeat.env", env_file({"HEARTBEAT_URL": url}), mode=0o600)
    ctx.kit.heartbeat_enabled = True
    save_configs(ctx)
