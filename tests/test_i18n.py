"""Translation layer behaviour, including the guarantee that data is never translated."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cowmata_tailring.ui import i18n  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_language():
    previous = i18n.get_language()
    yield
    i18n.set_language(previous)


def test_chinese_passes_through_unchanged() -> None:
    i18n.set_language("zh")
    assert i18n.t("保存") == "保存"
    assert i18n.t("站立") == "站立"


def test_english_translates_known_strings() -> None:
    i18n.set_language("en")
    assert i18n.t("保存") == "Save"
    assert i18n.t("站立") == "Standing"
    assert i18n.t("模型辅助") == "Model assist"


def test_unknown_strings_fall_through() -> None:
    """An incomplete catalogue degrades to Chinese, never to blank or a crash."""
    i18n.set_language("en")
    assert i18n.t("这条目录里没有") == "这条目录里没有"
    assert i18n.t("") == ""


def test_label_text_follows_language() -> None:
    label = {"name": "抬尾", "en": "tail raised", "code": "TAIL_RAISED"}
    i18n.set_language("zh")
    assert i18n.label_text(label) == "抬尾"
    i18n.set_language("en")
    assert i18n.label_text(label) == "tail raised"


def test_label_text_falls_back_when_one_side_missing() -> None:
    i18n.set_language("en")
    assert i18n.label_text({"name": "抬尾", "en": ""}) == "抬尾"
    i18n.set_language("zh")
    assert i18n.label_text({"name": "", "en": "tail raised"}) == "tail raised"


def test_rejects_unsupported_language() -> None:
    with pytest.raises(ValueError):
        i18n.set_language("fr")


def test_label_code_is_language_independent() -> None:
    """`code` is the machine key and must never be touched by translation.

    This is the contract that keeps previously exported CSV and project JSON
    readable regardless of the interface language.
    """
    from cowmata_tailring.annotation.defaults import DEFAULT_LABELS

    codes_zh = [label["code"] for label in DEFAULT_LABELS]
    i18n.set_language("en")
    codes_en = [label["code"] for label in DEFAULT_LABELS]
    assert codes_zh == codes_en
    assert "STANDING" in codes_zh


def test_default_labels_all_carry_display_pair() -> None:
    """Every label has both a Chinese name and an English name to display."""
    from cowmata_tailring.annotation.defaults import DEFAULT_LABELS

    for label in DEFAULT_LABELS:
        assert label["name"], f"missing Chinese name: {label['code']}"
        assert label["en"], f"missing English name: {label['code']}"
        assert label["code"], f"missing code: {label['name']}"


def test_manual_calving_assistance_label_contract() -> None:
    """New labels append after the existing v4 codes; old indices stay valid."""
    from cowmata_tailring.annotation.defaults import DEFAULT_LABELS

    labels = {label["code"]: label for label in DEFAULT_LABELS}
    assistance = labels["MANUAL_CALVING_ASSISTANCE"]

    assert assistance["name"] == "人工辅助产犊"
    assert assistance["en"] == "manual calving assistance"
    assert assistance["key"] == "D"
    assert assistance["type"] == "interval"
    assert assistance["layer"] == "calving_process"
    assert assistance["overlap"] is True
    assert assistance["specialty"] is True
    assert assistance["trainable"] is True
    assert DEFAULT_LABELS[16]["code"] == "SYNC_ANCHOR"
    assert DEFAULT_LABELS[17]["code"] == "MOUNTING"
