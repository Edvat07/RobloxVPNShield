"""Vietnamese / English UI text.

The language comes from the app's settings ("vi", "en" or "auto"); "auto" follows the Windows display language:
Vietnamese Windows gets Vietnamese, everything else English.
"""

from __future__ import annotations

import ctypes

LANGUAGES = {"auto": "Tự động / Auto", "vi": "Tiếng Việt", "en": "English"}
_LANG_VIETNAMESE = 0x2A   # primary language id of LANGID 0x042A (vi-VN)
_current = "en"


def system_language() -> str:
    try:
        langid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
    except (OSError, AttributeError):
        return "en"
    return "vi" if langid & 0x3FF == _LANG_VIETNAMESE else "en"


def set_language(choice: str | None) -> str:
    """Apply a settings value ("auto", "vi", "en" or None); returns the language now in use."""
    global _current
    _current = choice if choice in ("vi", "en") else system_language()
    return _current


def current() -> str:
    return _current


class Strings:
    """A table of {key: {"vi": ..., "en": ...}}; calling it returns the text in the current language."""

    def __init__(self, table: dict[str, dict[str, str]]) -> None:
        self.table = table

    def __call__(self, key: str, /, **values) -> str:
        entry = self.table[key]
        text = entry.get(_current) or entry["en"]
        return text.format(**values) if values else text
