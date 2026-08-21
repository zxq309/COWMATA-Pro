"""Minimal display-layer translation.

Only user-visible strings pass through :func:`t`. Persisted data (project JSON,
exported CSV) keeps its original Chinese label names so existing datasets stay
readable -- ``code`` is the machine key, ``name``/``en`` are display only.

ponytail: a plain dict, not Qt .ts/.qm — no lupdate/lrelease toolchain to run,
switch to Qt Linguist only if translators need a GUI workflow.
"""

from __future__ import annotations

import locale

from cowmata_tailring.ui.translations import ZH_TO_EN

_language = "zh"


def set_language(lang: str = "auto") -> str:
    """Select the interface language. Returns the resolved language code."""
    global _language
    if lang == "auto":
        code, _ = locale.getdefaultlocale()
        _language = "zh" if (code or "").lower().startswith("zh") else "en"
    elif lang in ("zh", "en"):
        _language = lang
    else:
        raise ValueError(f"unsupported language: {lang!r}")
    return _language


def get_language() -> str:
    return _language


def t(text: str) -> str:
    """Translate a Chinese source string for display.

    Unknown strings fall through unchanged, so an incomplete catalogue degrades
    to Chinese rather than to a crash or an empty label.
    """
    if _language == "zh":
        return text
    return ZH_TO_EN.get(text, text)


def label_text(label: object) -> str:
    """Display name for a label mapping/object that carries ``name`` and ``en``."""
    get = label.get if isinstance(label, dict) else lambda k, d=None: getattr(label, k, d)
    if _language == "en":
        return str(get("en", "") or get("name", ""))
    return str(get("name", "") or get("en", ""))


def _self_check() -> None:
    assert t("保存") == "保存"
    set_language("en")
    assert t("保存") == "Save", t("保存")
    assert t("__missing__") == "__missing__"
    assert label_text({"name": "站立", "en": "standing"}) == "standing"
    set_language("zh")
    assert label_text({"name": "站立", "en": "standing"}) == "站立"
    print("i18n self-check OK")


if __name__ == "__main__":
    _self_check()
