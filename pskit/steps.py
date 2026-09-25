"""Verified, idempotent, resumable steps.

Every step answers three questions about the real machine:
  check()  is it already in the wanted state? (then nothing is applied)
  apply()  make it so
  verify() prove it, returning None or a plain-language problem
A step that fails verification stops the install with one sentence and a
next action. Re-running the installer resumes: finished steps are
re-checked, never blindly re-applied.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import HostConfig, KitConfig
from .i18n import t
from .state import Journal, Ledger
from .system import Host
from .ui import UI


@dataclass
class Context:
    host: Host
    ui: UI
    journal: Journal
    ledger: Ledger
    cfg: HostConfig
    kit: KitConfig
    answers: Dict[str, Any] = field(default_factory=dict)
    facts: Dict[str, Any] = field(default_factory=dict)
    source_dir: str = ""

    def answer(self, key: str, default: Any = None) -> Any:
        return self.answers.get(key, default)


class StepFailed(RuntimeError):
    def __init__(self, step_id: str, message: str, hint: str = ""):
        self.step_id = step_id
        self.message = message
        self.hint = hint
        super().__init__(f"{step_id}: {message}")


class Step:
    id = "step"
    title = ""

    def applies(self, ctx: Context) -> bool:
        return True

    def check(self, ctx: Context) -> bool:
        return False

    def apply(self, ctx: Context) -> None:
        raise NotImplementedError

    def verify(self, ctx: Context) -> Optional[str]:
        return None if self.check(ctx) else t("engine.not_in_state")

    def hint(self, ctx: Context) -> str:
        return ""


class Engine:
    def __init__(self, ctx: Context, steps: List[Step]):
        self.ctx = ctx
        self.steps = steps

    def run(self) -> None:
        ctx = self.ctx
        active = [s for s in self.steps if s.applies(ctx)]
        for s in self.steps:
            if s not in active:
                ctx.journal.mark(s.id, "skipped")
        total = len(active)
        for n, step in enumerate(active, 1):
            ctx.ui.step(n, total, t(step.title) if step.title else step.id)
            try:
                if ctx.journal.status(step.id) == "done" and step.check(ctx):
                    ctx.ui.ok(t("engine.already_done"))
                    continue
                if step.check(ctx):
                    ctx.journal.mark(step.id, "done")
                    ctx.ui.ok(t("engine.already_in_state"))
                    continue
                ctx.journal.mark(step.id, "running")
                step.apply(ctx)
                problem = step.verify(ctx)
            except StepFailed as exc:
                ctx.journal.mark(step.id, "failed", exc.message)
                raise
            except KeyboardInterrupt:
                ctx.journal.mark(step.id, "interrupted")
                raise
            except Exception as exc:
                ctx.journal.mark(step.id, "failed", traceback.format_exc())
                raise StepFailed(step.id, t("engine.unexpected", error=str(exc)[:300]), step.hint(ctx))
            if problem:
                ctx.journal.mark(step.id, "failed", problem)
                raise StepFailed(step.id, problem, step.hint(ctx))
            ctx.journal.mark(step.id, "done")
            ctx.ui.ok(t("engine.done"))
