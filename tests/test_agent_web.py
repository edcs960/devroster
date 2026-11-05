import http.client
import json
import threading
from unittest.mock import patch

import pytest

from agent_dev.ui.agent_server import AgentWeb
from agent_dev.ui.local_web import make_server


@pytest.fixture
def web(tmp_path):
    web = AgentWeb(tmp_path)
    server = make_server(web, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield web, server, tmp_path
    server.shutdown()
    thread.join(timeout=3)
    web.close()
    server.server_close()


def request(server, path, method="GET", payload=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    body = json.dumps(payload) if payload is not None else None
    values = {"Content-Type": "application/json", **(headers or {})}
    conn.request(method, path, body, values)
    response = conn.getresponse()
    data = response.read()
    status = response.status
    conn.close()
    return status, json.loads(data) if path.startswith("/api/") else data


def test_all_gets_are_readonly_and_no_ai(web):
    _app, server, root = web
    with patch(
        "agent_dev.adapters.agent_console.dispatch", side_effect=AssertionError("must not dispatch")
    ):
        for route in [
            "/agent",
            "/agent.js",
            "/agent.css",
            "/api/agent/bootstrap",
            "/api/agent/catalog",
            "/api/agent/history",
            "/api/agent/tasks",
        ]:
            status, _ = request(server, route)
            assert status == 200
        assert request(server, "/api/agent/submit")[0] == 400
    assert not list(root.iterdir())


def test_cross_origin_host_and_csrf(web):
    app, server, _ = web
    route = "/api/agent/preview"
    payload = {"operation": "preset.list", "fields": {}, "options": {}}
    assert request(server, route, "POST", payload)[0] == 403
    assert request(server, route, "POST", payload, {"X-Local-Token": "wrong"})[0] == 403
    headers = {"X-Local-Token": app.token}
    assert request(server, route, "POST", payload, headers)[0] == 200
    assert (
        request(server, route, "POST", payload, {**headers, "Origin": "https://evil.test"})[0]
        == 403
    )
    assert request(server, "/api/agent/bootstrap", headers={"Host": "evil.test"})[0] == 403
    assert (
        request(server, "/api/agent/bootstrap", headers={"Sec-Fetch-Site": "cross-site"})[0] == 403
    )


def test_schema_injection_body_type_and_changed_preview(web):
    app, server, _ = web
    h = {"X-Local-Token": app.token}
    assert request(server, "/api/agent/preview", "POST", [], h)[0] == 400
    assert (
        request(
            server,
            "/api/agent/preview",
            "POST",
            {"operation": "preset.list", "fields": {}, "options": {"db": "/tmp/other"}},
            h,
        )[0]
        == 400
    )
    status, preview = request(
        server,
        "/api/agent/preview",
        "POST",
        {"operation": "preset.list", "fields": {}, "options": {}},
        h,
    )
    assert status == 200
    payload = {
        "operation": "preset.show",
        "fields": {"preset": "python"},
        "options": {},
        "preview_digest": preview["preview_digest"],
    }
    assert request(server, "/api/agent/submit", "POST", payload, h)[0] == 400
