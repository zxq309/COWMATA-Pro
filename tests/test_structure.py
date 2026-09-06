"""Structural integrity checks that run without PySide6, torch or a display.

These guard the refactor itself: every module parses, no import points at a
module that no longer exists, and no circular imports were introduced.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PKG_ROOT = Path(__file__).resolve().parent.parent / "cowmata_tailring"
REPO_ROOT = PKG_ROOT.parent
PKG = "cowmata_tailring"


def _python_files() -> list[Path]:
    return sorted(p for p in PKG_ROOT.rglob("*.py"))


def _module_name(path: Path) -> str:
    rel = path.relative_to(PKG_ROOT.parent).with_suffix("")
    parts = list(rel.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _package_of(path: Path) -> str:
    """The package a relative import resolves against.

    For ``a/b/__init__.py`` that is ``a.b`` itself; for ``a/b/c.py`` it is ``a.b``.
    """
    name = _module_name(path)
    if path.name == "__init__.py":
        return name
    return name.rsplit(".", 1)[0]


def _local_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:  # relative import
                base = _package_of(path)
                for _ in range(node.level - 1):
                    base = base.rsplit(".", 1)[0]
                out.add(f"{base}.{node.module}" if node.module else base)
            elif node.module and node.module.startswith(PKG):
                out.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(PKG):
                    out.add(alias.name)
    return out


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: p.name)
def test_module_parses(path: Path) -> None:
    """Every shipped module is syntactically valid Python."""
    ast.parse(path.read_text(encoding="utf-8"))


def test_no_imports_of_removed_modules() -> None:
    """No module imports a package module that does not exist on disk."""
    known = {_module_name(p) for p in _python_files()}
    known |= {m.rsplit(".", 1)[0] for m in known if "." in m}

    missing: list[str] = []
    for path in _python_files():
        for target in _local_imports(path):
            if target not in known:
                missing.append(f"{_module_name(path)} -> {target}")

    assert not missing, "imports pointing at removed modules:\n" + "\n".join(missing)


def test_no_import_cycles() -> None:
    """The package import graph is acyclic."""
    graph = {_module_name(p): _local_imports(p) for p in _python_files()}

    visiting: set[str] = set()
    done: set[str] = set()
    cycles: list[str] = []

    def walk(node: str, trail: list[str]) -> None:
        if node in done:
            return
        if node in visiting:
            start = trail.index(node)
            cycles.append(" -> ".join(trail[start:] + [node]))
            return
        visiting.add(node)
        for nxt in sorted(graph.get(node, ())):
            if nxt in graph:
                walk(nxt, trail + [nxt])
        visiting.discard(node)
        done.add(node)

    for module in sorted(graph):
        walk(module, [module])

    assert not cycles, "import cycles detected:\n" + "\n".join(cycles)


def test_no_legacy_flat_module_names() -> None:
    """The pre-refactor flat module names are gone from import statements."""
    legacy = {
        "main_window", "window_ui", "annotation_core", "data_core", "defaults",
        "media_engine", "safe_media_engine", "ui_helpers", "widgets",
        "integrated_window", "ultimate_v7_window", "model_assist",
    }
    offenders: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in legacy:
                offenders.append(f"{path.name}: from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in legacy:
                        offenders.append(f"{path.name}: import {alias.name}")

    assert not offenders, "legacy flat imports remain:\n" + "\n".join(offenders)


def test_entry_point_is_importable_without_qt() -> None:
    """`app.main` parses its CLI without importing PySide6 at module scope."""
    sys.path.insert(0, str(PKG_ROOT.parent))
    from cowmata_tailring.app.main import build_parser

    args = build_parser().parse_args([])
    assert args.mode == "model-assist"
    assert args.lang == "auto"

    args = build_parser().parse_args(["--mode", "basic", "--lang", "en"])
    assert args.mode == "basic"
    assert args.lang == "en"

    args = build_parser().parse_args(
        ["--json", "session.json", "--video", "session.mp4"]
    )
    assert args.json == "session.json"
    assert args.video == "session.mp4"


def test_single_entry_point() -> None:
    """Exactly one module defines the application entry point."""
    entries = [p for p in _python_files() if p.name == "main.py" and p.parent.name == "app"]
    assert len(entries) == 1, f"expected one app/main.py, found {entries}"


def test_root_one_click_launcher() -> None:
    """The requested offline launcher uses its embedded runtime, never a venv."""
    launcher = REPO_ROOT / "START_ANNOTATOR.bat"
    text = launcher.read_text(encoding="utf-8")
    assert 'runtime\\pythonw.exe' in text
    assert 'portable_start.py" %*' in text
    assert "vendor\\vlc\\libvlc.dll" in text
    assert "vendor\\ffmpeg\\bin\\ffprobe.exe" in text
    assert "pip install" not in text
    assert 'set "VENV=' not in text
    start = (REPO_ROOT / "portable_start.py").read_text(encoding="utf-8")
    assert '["--mode", "workspace", *args]' in start
