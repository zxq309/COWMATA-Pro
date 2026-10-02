"""4.4.6: operators use every business function; only account management stays with the administrator."""
import io
import json
import sys

import pytest

from cowmata_security import worker as worker_module
from cowmata_security.authority import CAPABILITIES, IMPLIED
from cowmata_security.client import AccessDenied, Session

BUSINESS = ("annotate", "prepare", "upload", "behavior", "health", "dataset")


def _operator(capabilities):
    session = Session(lambda _: None, "pro")
    session.token = "t" * 43
    session._accept({"account": "operator01", "role": "operator", "product": "pro", "lease_seconds": 900,
                     "capabilities": capabilities})
    return session


def test_policy_differs_only_in_account_management():
    assert set(CAPABILITIES["admin"]) - set(CAPABILITIES["operator"]) == {"accounts"}
    assert set(BUSINESS) <= set(CAPABILITIES["operator"])


@pytest.mark.parametrize("server_list", [
    ["annotate", "prepare", "upload"],  # a server set up before 4.4.6
    list(CAPABILITIES["operator"]),     # an upgraded server
])
def test_operator_can_generate_candidates_with_old_and_new_servers(server_list):
    session = _operator(server_list)
    for capability in BUSINESS:
        session.require(capability)
    assert not session.allows("accounts")
    with pytest.raises(AccessDenied):
        session.require("accounts")


def test_operator_without_annotate_gets_nothing_implied():
    session = _operator(["upload"])
    assert not any(session.allows(c) for c in ("behavior", "health", "dataset"))


def test_worker_asks_the_server_for_the_capability_operators_have(monkeypatch, tmp_path):
    requests = []

    def transport(request):
        requests.append(request)
        return {"account": "operator01", "role": "operator", "product": "pro", "lease_seconds": 900,
                "capabilities": ["annotate", "prepare", "upload"]}

    class Stdin:
        buffer = io.BytesIO(json.dumps({"session": "s" * 43}).encode("utf-8") + b"\n")

    class NoThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(sys, "stdin", Stdin)
    monkeypatch.setattr(worker_module, "read_config", lambda root: transport)
    monkeypatch.setattr(worker_module.threading, "Thread", NoThread)
    session = worker_module.authorize_worker(tmp_path, "behavior")
    assert requests[0]["capability"] == IMPLIED["behavior"] == "annotate"
    assert session.allows("behavior") and not session.allows("accounts")


def test_menus_only_lock_account_management_for_operators():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "cowmata_security" / "qt_ui.py").read_text(encoding="utf-8")
    assert "此功能仅管理员可用" not in source
    server = (Path(__file__).resolve().parents[1] / "server-upgrade" / "cowmata_security" / "authority.py")
    assert server.read_bytes() == (Path(__file__).resolve().parents[1] / "cowmata_security" / "authority.py").read_bytes()
