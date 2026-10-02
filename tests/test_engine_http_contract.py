"""Malformed transport input returns an envelope; later requests still work."""
import http.client
import json
import threading

import pytest

from cowmata_engine.api import handle
from cowmata_engine.server import make_server


@pytest.fixture
def engine_http():
    server = make_server(port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.mark.parametrize("payload", [["engine.info"], "engine.info", 7, True])
def test_api_rejects_non_object_with_error_envelope(payload):
    response = handle(payload)
    assert response["ok"] is False
    assert response["api"] == "cowmata-engine-1"
    assert response["action"] is None
    assert "对象" in response["error"]


@pytest.mark.parametrize("body", ["[]", "null", '"engine.info"', "7", "true"])
def test_http_rejects_json_non_objects_without_dropping_connection(engine_http, body):
    connection = http.client.HTTPConnection("127.0.0.1", engine_http, timeout=3)
    connection.request("POST", "/api/engine.info", body, {"Content-Type": "application/json"})
    reply = connection.getresponse()
    assert reply.status == 400
    assert json.loads(reply.read())["ok"] is False
    connection.close()
    # A malformed request must not stop the server or leave a running job.
    connection = http.client.HTTPConnection("127.0.0.1", engine_http, timeout=3)
    connection.request("GET", "/api/info")
    reply = connection.getresponse()
    assert reply.status == 200
    assert json.loads(reply.read())["ok"] is True
    connection.close()


@pytest.mark.parametrize("length", ["invalid", "-1", "-100"])
def test_http_rejects_invalid_content_length_promptly(engine_http, length):
    connection = http.client.HTTPConnection("127.0.0.1", engine_http, timeout=3)
    connection.request("POST", "/api/engine.info", b"", {"Content-Length": length})
    reply = connection.getresponse()
    assert reply.status == 400
    assert json.loads(reply.read())["ok"] is False
    connection.close()
