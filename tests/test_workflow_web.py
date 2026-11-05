import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_dev.adapters.workflow_definitions import preset
from agent_dev.ui.workflow_server import WorkflowWeb


@pytest.fixture
def web(tmp_path):
    app = WorkflowWeb(tmp_path)
    app.service.store.register_project(
        "P", "Project", {"repositories": {"app": {"path": "."}}, "phases": [{"id": "developer"}]}
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            app.handle(self)

        def do_POST(self):
            app.handle(self)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1], app
    server.shutdown()
    thread.join()
    server.server_close()


def call(web, method, path, body=None, headers=None):
    port, _ = web
    connection = http.client.HTTPConnection("127.0.0.1", port)
    raw = json.dumps(body) if isinstance(body, dict) else body
    connection.request(method, path, raw, headers or {})
    response = connection.getresponse()
    data = response.read()
    status = response.status
    connection.close()
    return status, json.loads(data)


def test_bootstrap_save_conflict_apply_and_reload(web):
    code, bootstrap = call(web, "GET", "/api/workflows/bootstrap")
    assert code == 200 and len(bootstrap["presets"]) == 7
    headers = {"Content-Type": "application/json", "X-Workflow-Token": bootstrap["token"]}
    graph = preset("quick-fix")
    graph["id"] = "edited"
    payload = {"project_id": "P", "document": graph, "expected_version": 0}
    assert call(web, "POST", "/api/workflows/save", payload, headers)[0] == 200
    assert call(web, "POST", "/api/workflows/save", payload, headers)[0] == 409
    assert (
        call(
            web,
            "POST",
            "/api/workflows/apply",
            {
                "project_id": "P",
                "workflow_id": "edited",
                "version": 1,
                "expected_profile_version": 0,
            },
            headers,
        )[0]
        == 200
    )
    code, detail = call(web, "GET", "/api/workflows/detail?project_id=P&workflow_id=edited")
    assert code == 200 and detail["document"] == graph


def test_workflow_http_origin_token_and_duplicate_key_rejected(web):
    assert call(web, "GET", "/api/workflows/bootstrap", headers={"Host": "evil.example"})[0] == 403
    assert (
        call(web, "GET", "/api/workflows/bootstrap", headers={"Origin": "https://evil.example"})[0]
        == 403
    )
    assert (
        call(
            web,
            "POST",
            "/api/workflows/validate",
            {"document": preset("quick-fix")},
            {"Content-Type": "application/json"},
        )[0]
        == 403
    )
    _code, bootstrap = call(web, "GET", "/api/workflows/bootstrap")
    headers = {"Content-Type": "application/json", "X-Workflow-Token": bootstrap["token"]}
    assert (
        call(web, "POST", "/api/workflows/validate", '{"document":{},"document":{}}', headers)[0]
        == 400
    )


def test_unsaved_workflow_draft_cannot_apply_previous_version():
    import shutil
    import subprocess
    from pathlib import Path

    if not shutil.which("node"):
        pytest.skip("Node required for Workflow DOM interaction regression.")
    root = Path(__file__).parents[1]
    subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("workflow_ui_regression.js")),
            str(root / "agent_dev/ui/assets/workflows.js"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_validation_errors_carry_node_and_field_location(web):
    _, bootstrap = call(web, "GET", "/api/workflows/bootstrap")
    headers = {"Content-Type": "application/json", "X-Workflow-Token": bootstrap["token"]}
    graph = preset("quick-fix")
    graph["id"] = "located"
    role = next(n for n in graph["nodes"] if n["kind"] == "role")
    role["module"] = "missing-module"  # module name differs from the node id
    status, result = call(web, "POST", "/api/workflows/validate", {"project_id": "P", "document": graph}, headers)
    assert status == 400 and result["error"] == "INVALID_INPUT"
    assert result["message"] == "Unknown module: missing-module"
    assert result["location"] == {"node": role["id"], "field": "module"}
    graph["edges"].append(dict(graph["edges"][0]))
    del role["module"]
    status, result = call(web, "POST", "/api/workflows/validate", {"project_id": "P", "document": graph}, headers)
    assert result["location"] == {"edge": len(graph["edges"]) - 1}


def test_bootstrap_exposes_latest_applied_workflow_without_profile_secrets(web):
    from agent_dev.adapters.workflow_definitions import roster_graph
    from agent_dev.domain.project_roles import default_roles

    _, app = web
    service = app.service
    graph = roster_graph(default_roles())
    profile = service._profile("P")
    applied = {"id": graph["id"], "version": 1, "document": graph}
    profile["workflow"] = applied
    profile["private_config"] = "must-not-be-returned"
    service.store.revise_profile("P", profile, 0)
    before = service.store.db.rows("SELECT * FROM projects")
    code, data = call(web, "GET", "/api/workflows/bootstrap")
    assert code == 200
    assert data["projects"][0]["workflow"] == applied
    assert "private_config" not in json.dumps(data)
    assert service.list("P") == []
    assert service.store.db.rows("SELECT * FROM projects") == before
    # A roster snapshot is not a saved definition: applying it must fail without writes.
    headers = {"Content-Type": "application/json", "X-Workflow-Token": data["token"]}
    code, _ = call(web, "POST", "/api/workflows/apply", {
        "project_id": "P", "workflow_id": graph["id"], "version": 1,
        "expected_profile_version": data["projects"][0]["version"],
    }, headers)
    assert code == 400
    assert service.store.db.rows("SELECT * FROM projects") == before
    assert call(web, "GET", "/api/workflows/bootstrap")[1]["projects"][0]["workflow"] == applied
