"""Messages for the owner. English is the default; Portuguese is complete.

Technical output (logs, diagnostic fields) stays in English. Everything the
owner reads in the installer, doctor and alerts goes through `t()`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

_LOCALES = Path(__file__).with_name("locales")
_cache: Dict[str, Dict[str, str]] = {}
_lang = "en"


def load(lang: str) -> Dict[str, str]:
    if lang not in _cache:
        path = _LOCALES / f"{lang}.json"
        _cache[lang] = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return _cache[lang]


def set_language(lang: str) -> None:
    global _lang
    _lang = lang if lang in ("en", "pt") else "en"


def language() -> str:
    return _lang


def t(key: str, /, **params: object) -> str:
    text = load(_lang).get(key) or load("en").get(key) or key
    if params:
        try:
            return text.format(**params)
        except (KeyError, IndexError, ValueError):
            return text
    return text


def t_in(lang: str, key: str, /, **params: object) -> str:
    """Translate in a given language without changing the global one
    (alerts are rendered in the owner's language from root services)."""
    text = load(lang).get(key) or load("en").get(key) or key
    try:
        return text.format(**params) if params else text
    except (KeyError, IndexError, ValueError):
        return text
