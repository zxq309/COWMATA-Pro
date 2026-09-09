"""Catalogue completeness and the guarantee that translation never touches stored data."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cowmata_tailring.ui.translations import ZH_TO_EN  # noqa: E402

PKG_ROOT = Path(__file__).resolve().parent.parent / "cowmata_tailring"
CJK = re.compile(r"[\u4e00-\u9fff]")

# Functions that serialise to disk. A t() call in any of these would translate
# stored data and break datasets written by earlier versions.
DATA_WRITERS = {"build_events_csv", "to_dict", "as_dict", "serialize", "_serialize"}


def _source_strings() -> set[str]:
    found: set[str] = set()
    for path in PKG_ROOT.rglob("*.py"):
        if "model_runtime" in path.parts or path.name == "translations.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef)):
                first = node.body[0] if node.body else None
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docs.add(id(first.value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) not in docs and CJK.search(node.value):
                    found.add(node.value)
    return found


def test_catalogue_is_complete() -> None:
    missing = _source_strings() - ZH_TO_EN.keys()
    assert not missing, f"{len(missing)} untranslated strings, e.g. {sorted(missing)[:5]}"


def test_no_chinese_left_in_english_values() -> None:
    leftovers = {k: v for k, v in ZH_TO_EN.items() if CJK.search(v)}
    assert not leftovers, f"English values still containing Chinese: {list(leftovers)[:3]}"


def test_no_empty_translations() -> None:
    empty = [k for k, v in ZH_TO_EN.items() if not v.strip() and k.strip()]
    assert not empty, f"empty translations: {empty[:5]}"


def test_command_line_coverage_includes_all_registered_catalogue_blocks(capsys) -> None:
    import runpy

    runpy.run_path(str(PKG_ROOT / "ui" / "translations.py"), run_name="__main__")
    report = dict(line.split(":", 1) for line in capsys.readouterr().out.splitlines())
    assert report["untranslated      "].strip() == "0"


def test_translation_never_applied_to_stored_data() -> None:
    """No t() call may appear inside a function that serialises to disk."""
    offenders: list[str] = []
    for path in PKG_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for func in ast.walk(tree):
            if isinstance(func, ast.FunctionDef) and func.name in DATA_WRITERS:
                for node in ast.walk(func):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                        if node.func.id == "t":
                            offenders.append(f"{path.name}:{func.name}:{node.lineno}")
    assert not offenders, "translation applied to stored data:\n" + "\n".join(offenders)
