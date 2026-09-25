"""Template rendering. `{{name}}` placeholders; an unknown or unused
placeholder is an error, so a typo can never ship an empty value into a
unit file."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

TEMPLATES = Path(__file__).with_name("templates")
_PH = re.compile(r"\{\{([a-z_][a-z0-9_]*)\}\}")


class RenderError(ValueError):
    pass


def render_text(text: str, values: Mapping[str, Any], name: str = "<text>") -> str:
    missing = sorted({m for m in _PH.findall(text) if m not in values})
    if missing:
        raise RenderError(f"{name}: no value for {', '.join(missing)}")

    def sub(m: re.Match[str]) -> str:
        v = values[m.group(1)]
        s = str(v)
        if "\n" in s and not m.group(1).endswith("_block"):
            raise RenderError(f"{name}: newline in value of {m.group(1)}")
        return s

    return _PH.sub(sub, text)


def render(template: str, values: Mapping[str, Any]) -> str:
    path = TEMPLATES / template
    return render_text(path.read_text(encoding="utf-8"), values, template)


def template_names(prefix: str) -> list:
    base = TEMPLATES / prefix
    return sorted(p.name for p in base.iterdir() if p.is_file())



def systemd_quote(arg: str) -> str:
    """Quotes one argument for an Exec= line."""
    if arg and re.match(r"^[A-Za-z0-9_@%+=:,./-]+$", arg):
        return arg.replace("%", "%%")
    escaped = arg.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{escaped}"'


def exec_line(args) -> str:
    return " ".join(systemd_quote(a) for a in args)
