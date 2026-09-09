"""Window teardown explicitly chooses Save; close-choice tests override this."""
import sys

import pytest


@pytest.fixture(autouse=True)
def save_when_test_closes_window(monkeypatch):
    module = sys.modules.get("cowmata_tailring.workspace.window")
    if module is not None:
        monkeypatch.setattr(module.MainWindow, "confirm_close", lambda self: "save")
