"""4.4.1: the embedded field-ledger uploader must ship every third-party module it imports.

4.4.0 embedded the uploader but not its Excel dependency; the private runtime had no openpyxl and
every .xlsx import or upload failed with "No module named 'openpyxl'". Any third-party import in
cowmata_tailring/ledger must be pinned in requirements-portable.txt and imported by the release gate.
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEDGER = ROOT / "cowmata_tailring" / "ledger"
# Import name -> distribution name where they differ.
DISTRIBUTION = {"PySide6": "pyside6", "et_xmlfile": "et-xmlfile"}


def _third_party_imports():
    found = set()
    for path in LEDGER.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top not in sys.stdlib_module_names and not top.startswith("cowmata_"):
                    found.add(top)
    return found


def _pinned():
    text = (ROOT / "requirements-portable.txt").read_text(encoding="utf-8")
    return {re.split(r"[=<>~!\[ ]", line, maxsplit=1)[0].strip().lower().replace("_", "-")
            for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")}


def test_uploader_uses_openpyxl():
    assert "openpyxl" in _third_party_imports()


def test_every_uploader_dependency_is_pinned_for_the_portable_runtime():
    pinned = _pinned()
    missing = sorted(m for m in _third_party_imports()
                     if DISTRIBUTION.get(m, m).lower().replace("_", "-") not in pinned)
    assert not missing, f"embedded uploader imports {missing} but requirements-portable.txt does not pin them"
    assert {"openpyxl", "et-xmlfile"} <= pinned


def test_release_gate_imports_the_uploader_excel_stack():
    script = (ROOT / "scripts" / "build_portable.py").read_text(encoding="utf-8")
    for needle in ("runtime/Lib/site-packages/openpyxl/__init__.py", "import io,openpyxl,et_xmlfile",
                   "openpyxl.Workbook().save(io.BytesIO())",
                   "from cowmata_tailring.ledger import ledger_files,ledger_import,ledger_sheets,ledger_workbook"):
        assert needle in script, needle
