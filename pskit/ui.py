"""Terminal interaction.

Reads from /dev/tty so `curl ... | bash` still gets keyboard input. An
answers file (JSON) replaces every question for unattended runs and CI.
"""

from __future__ import annotations

import getpass
import json
import os
import sys
import textwrap
from typing import IO, Any, Callable, Dict, List, Optional, Sequence, Tuple

from .i18n import t


class NeedAnswer(RuntimeError):
    """Raised when running unattended and an answer is missing."""


def per_minute(show: Callable[[int], None]) -> Callable[[int], None]:
    """A countdown that speaks once a minute. A line every few seconds
    pushes what the owner has to read (an address, a code) off the screen."""
    last: List[int] = []

    def tick(seconds_left: int) -> None:
        minutes = max(1, (seconds_left + 59) // 60)
        if not last or minutes != last[0]:
            last[:] = [minutes]
            show(minutes)
    return tick


class UI:
    def __init__(self, answers: Optional[Dict[str, Any]] = None, interactive: Optional[bool] = None,
                 out=None):
        self.answers = answers or {}
        self.out = out or sys.stdout
        self._tty_in: Optional[IO[str]] = None
        if interactive is None:
            interactive = self._open_tty()
        self.interactive = interactive
        self.color = bool(getattr(self.out, "isatty", lambda: False)()) and not os.environ.get("NO_COLOR")

    def _open_tty(self) -> bool:
        try:
            self._tty_in = open("/dev/tty")  # noqa: SIM115 - kept open for the whole session
            return True
        except OSError:
            return sys.stdin.isatty()

    # ---- output ------------------------------------------------------
    def _c(self, code: str, text: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.color else text

    def say(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def title(self, text: str) -> None:
        self.say()
        self.say(self._c("1", text))
        self.say(self._c("2", "-" * min(len(text), 72)))

    def step(self, n: int, total: int, text: str) -> None:
        self.say(self._c("1", f"[{n}/{total}] {text}"))

    def ok(self, text: str) -> None:
        self.say(self._c("32", "  ok  ") + text)

    def info(self, text: str) -> None:
        self.say("      " + text)

    def warn(self, text: str) -> None:
        self.say(self._c("33", "  !!  ") + text)

    def fail(self, text: str) -> None:
        self.say(self._c("31", "  xx  ") + text)

    def box(self, lines: Sequence[str]) -> None:
        wrapped: List[str] = []
        for line in lines:
            if len(line) <= 76 or line.startswith(" ") or "://" in line:
                wrapped.append(line)
            else:
                wrapped += textwrap.wrap(line, 76) or [""]
        width = max((len(x) for x in wrapped), default=0)
        self.say("  +" + "-" * (width + 2) + "+")
        for line in wrapped:
            self.say("  | " + line.ljust(width) + " |")
        self.say("  +" + "-" * (width + 2) + "+")

    # ---- input -------------------------------------------------------
    def _readline(self, prompt: str) -> str:
        self.out.write(prompt)
        self.out.flush()
        src = self._tty_in or sys.stdin
        line = src.readline()
        if line == "":
            raise EOFError
        return line.rstrip("\n")

    def _answer(self, key: str) -> Tuple[bool, Any]:
        if key in self.answers:
            return True, self.answers[key]
        return False, None

    def _need(self, key: str) -> None:
        if not self.interactive:
            raise NeedAnswer(key)

    def choice(self, key: str, prompt: str, options: List[Tuple[str, str]], default: str) -> str:
        found, value = self._answer(key)
        values = [v for v, _ in options]
        if found:
            if value not in values:
                raise NeedAnswer(f"{key}: '{value}' is not one of {values}")
            return value
        self._need(key)
        self.say()
        self.say(prompt)
        for i, (v, label) in enumerate(options, 1):
            mark = t("ui.default_mark") if v == default else ""
            self.say(f"  [{i}] {label}{mark}")
        while True:
            raw = self._readline(t("ui.choose", n=len(options))).strip()
            if raw == "":
                return default
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return values[int(raw) - 1]
            if raw in values:
                return raw
            self.warn(t("ui.invalid_choice"))

    def text(self, key: str, prompt: str, default: str = "",
             validate: Optional[Callable[[str], Optional[str]]] = None) -> str:
        found, value = self._answer(key)
        if found:
            value = str(value)
            err = validate(value) if validate else None
            if err:
                raise NeedAnswer(f"{key}: {err}")
            return value
        self._need(key)
        while True:
            suffix = f" [{default}]" if default else ""
            raw = self._readline(f"{prompt}{suffix}: ").strip() or default
            err = validate(raw) if validate else (None if raw else t("ui.required"))
            if not err:
                return raw
            self.warn(err)

    def yes_no(self, key: str, prompt: str, default: bool = True) -> bool:
        found, value = self._answer(key)
        if found:
            return bool(value)
        self._need(key)
        hint = t("ui.yes_no_default_yes") if default else t("ui.yes_no_default_no")
        while True:
            raw = self._readline(f"{prompt} {hint} ").strip().lower()
            if raw == "":
                return default
            if raw in ("y", "yes", "s", "sim"):
                return True
            if raw in ("n", "no", "nao", "não"):
                return False
            self.warn(t("ui.invalid_choice"))

    def secret(self, key: str, prompt: str, env: Optional[str] = None) -> str:
        """Secrets come from an environment variable in unattended runs,
        never from the answers file (it may be shared for support)."""
        if env and os.environ.get(env):
            return os.environ[env]
        self._need(key)
        while True:
            try:
                value = getpass.getpass(f"{prompt}: ", stream=self.out)
            except (EOFError, KeyboardInterrupt):
                raise
            if value.strip():
                return value.strip()
            self.warn(t("ui.required"))

    def pause(self, prompt: str) -> None:
        if not self.interactive:
            return
        self._readline(prompt + " ")


def load_answers(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return {}
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError("answers file must contain a JSON object")
    return data


def ask_language(ui: UI) -> str:
    """First question, asked before any language is known, so bilingual."""
    return ui.choice("language", "Language / Idioma", [("en", "English"), ("pt", "Português")], "en")
