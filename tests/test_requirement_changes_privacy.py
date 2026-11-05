"""Registered-secret privacy at real Changes HTTP, immutable and CLI boundaries."""

import http.client
import json
import threading

import pytest
from test_project_journey import prepared, workspace
from test_requirement_changes import _add_studio_revision

from agent_dev.adapters import requirement_changes as rc
from agent_dev.adapters.command_line import dispatch, parser
from agent_dev.adapters.studio_store import StudioStore
from agent_dev.domain.errors import DomainError
from agent_dev.ui.changes_server import ChangesWeb
from agent_dev.ui.local_web import make_server


@pytest.fixture
def private_workspace(tmp_path, monkeypatch):
    ws = workspace.__wrapped__(tmp_path)
    root, repo, _, _ = ws
    _, old = prepared(ws)
    secret = "synthetic-only-CR01-registered-value"
    monkeypatch.setattr("agent_dev.adapters.secrets.registered_secrets", lambda *_: (secret,))
    studio = StudioStore(root)
    with studio.connect() as conn:
        reqs = json.loads(conn.execute("SELECT payload FROM specs WHERE project_id='STUDIO-001'").fetchone()[0])["requirements"]
    reqs[0]["title"] += " changed " + secret
    _add_studio_revision(studio, "STUDIO-001", reqs)
    with studio.transaction() as conn:
        conn.execute("UPDATE projects SET brief=? WHERE id='STUDIO-001'", ("brief " + secret,))
        conn.execute("UPDATE specs SET source=? WHERE project_id='STUDIO-001' AND revision=2", ("source " + secret,))
        conn.execute("UPDATE approvals SET note=? WHERE project_id='STUDIO-001' AND mock_revision=2", ("approval " + secret,))
    # Authentic canonical existing task display text, never forged evidence or passed state.
    with repo.db.transaction() as conn:
        conn.execute("UPDATE tasks SET title=title||? WHERE project_id='P1'", (" " + secret,))
    return root, repo, old, secret


def test_registered_secret_absent_http_immutable_events_and_approved_followup(private_workspace):
    root, repo, old, secret = private_workspace
    web = ChangesWeb(root)
    server = make_server(web)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()

    def request(action, payload=None):
        client = http.client.HTTPConnection("127.0.0.1", server.server_port)
        try:
            headers = {"Content-Type": "application/json", "X-Local-Token": web.token}
            client.request("POST" if payload is not None else "GET", "/api/changes/" + action,
                           body=json.dumps(payload) if payload is not None else None, headers=headers)
            response = client.getresponse()
            data = json.loads(response.read())
            assert response.status == 200, data
            assert secret not in json.dumps(data)
            return data
        finally:
            client.close()

    try:
        before = repo.db.rows("SELECT count(*) AS n FROM events")[0]["n"]
        bootstrap = request("bootstrap?project_id=P1")
        assert repo.db.rows("SELECT count(*) AS n FROM events")[0]["n"] == before
        source = bootstrap["sources"][0]
        assert source["finalized"] and source["source"]["brief"] == "brief ****"
        assert source["source"]["spec_source"] == "source ****"
        assert source["source"]["approvals"][0]["note"] == "approval ****"
        assert source["handoff"]["tasks"][0]["title"].endswith("changed ****")
        params = {"project_id": "P1", "source_import_id": old["import_id"], "reason": "change " + secret, "actor": "reviewer " + secret}
        initial = request("preview", params)
        # Browser takes its handoff choice from bootstrap; sanitization must not create stale mismatch.
        params["handoff"] = dict(source["handoff"], tasks=source["handoff"]["tasks"][:1])
        params["handoff"]["tasks"][0]["title"] = "User-selected task " + secret
        params["impact_decisions"] = [{"requirement_id": "REQ-001", "task_id": initial["impacts"][0]["task_id"], "disposition": "excluded", "exclusion_reason": "explicit exclusion " + secret}]
        preview = request("preview", params)
        proposal = request("propose", dict(params, preview_digest=preview["preview_digest"], request_id="PRIVACY-PROP"))
        saved = repo.db.rows("SELECT proposal_json FROM requirement_changes WHERE id=?", (proposal["change_id"],))[0]["proposal_json"]
        assert secret not in saved and "User-selected task ****" in saved
        assert secret not in json.dumps(repo.db.rows("SELECT payload_json FROM events WHERE kind LIKE 'RequirementChange%'"))
        request("detail?project_id=P1&change_id=" + proposal["change_id"])
        request("query?project_id=P1")
        other = rc.propose(repo.db, root, dict(params, preview_digest=preview["preview_digest"], request_id="PRIVACY-REJECT-PROP"))
        applied = request("apply", {"change_id": proposal["change_id"], "expected_version": 0, "request_id": "PRIVACY-APPLY"})
        repo.approve("design", applied["design_id"], 1, "reviewer")
        repo.approve("plan", applied["plan_id"], 1, "reviewer")
        task = repo.db.rows("SELECT * FROM tasks WHERE plan_id=?", (applied["plan_id"],))[0]
        assert task["title"] == "User-selected task ****"
        assert repo.db.rows("SELECT id FROM requirement_links WHERE task_id=?", (task["id"],))
        assert secret not in json.dumps(repo.db.rows("SELECT payload_json FROM events WHERE aggregate_id=?", (applied["plan_id"],)))
        request("bootstrap?project_id=P1")
        # Reject reason/actor have the same pre-event boundary and old history is immutable.
        request("reject", {"change_id": other["change_id"], "expected_version": 0, "reason": secret, "actor": secret})
        assert secret not in json.dumps(repo.db.rows("SELECT payload_json FROM events WHERE kind='RequirementChangeRejected'"))
    finally:
        server.shutdown()
        server.server_close()
        worker.join()
        web.close()


def test_cli_public_and_exact_frozen_selection_privacy(private_workspace):
    root, repo, old, secret = private_workspace
    def command(*args):
        return dispatch(parser().parse_args(["changes", *args, "--root", str(root), "--actor", "actor " + secret, "--request-id", "CLI-PRIVACY"]))
    bootstrap = command("bootstrap", "--project-id", "P1")
    preview = command("preview", "P1", old["import_id"], "reason " + secret)
    assert secret not in json.dumps([bootstrap, preview])
    p = preview["selection"]
    assert rc.preview(repo.db, root, p)["preview_digest"] == preview["preview_digest"]
    proposal = rc.propose(repo.db, root, dict(p, preview_digest=preview["preview_digest"], request_id="CLI-PRIVATE-PROP"))
    assert secret not in json.dumps(command("detail", proposal["change_id"], "--project-id", "P1"))
    applied = rc.apply(repo.db, root, {"change_id": proposal["change_id"], "expected_version": 0, "request_id": "CLI-PRIVATE-APPLY"})
    repo.approve("design", applied["design_id"], 1, "reviewer")
    repo.approve("plan", applied["plan_id"], 1, "reviewer")
    impact = rc.detail(repo.db, proposal["change_id"])["impacts"][0]
    assert impact["followup_task_id"] == p["handoff"]["tasks"][0]["id"]
    assert repo.db.rows("SELECT id FROM requirement_links WHERE task_id=?", (impact["followup_task_id"],))
    assert secret not in json.dumps(command("bootstrap", "--project-id", "P1"))


@pytest.mark.parametrize("field", ["id", "allowed_paths", "verification_mapping"])
def test_secret_in_operational_selection_rejected_without_rewriting(private_workspace, field):
    root, repo, old, secret = private_workspace
    params = {"project_id": "P1", "source_import_id": old["import_id"], "reason": "bounded", "actor": "reviewer"}
    initial = rc.preview(repo.db, root, params)
    params["handoff"] = initial["handoff"]["selection"]
    task = params["handoff"]["tasks"][0]
    original = secret if field == "id" else [secret] if field == "allowed_paths" else [{"requirement_id": "REQ-001", "criteria_ids": ["REQ-001:AC-01"], "gate": "acceptance", "target": secret}]
    task[field] = original
    with pytest.raises(DomainError) as error:
        rc.preview(repo.db, root, params)
    assert secret not in str(error.value)
    assert task[field] == original
    assert not repo.db.rows("SELECT id FROM requirement_changes")


@pytest.mark.parametrize("transport", ["service", "HTTP", "CLI"])
def test_operational_apply_secret_rejected_before_any_canonical_writes(private_workspace, transport):
    root, repo, old, secret = private_workspace
    params = {"project_id": "P1", "source_import_id": old["import_id"], "reason": "operational boundary", "actor": "reviewer"}
    preview = rc.preview(repo.db, root, params)
    change = rc.propose(repo.db, root, dict(preview["selection"], preview_digest=preview["preview_digest"], request_id="OPERATIONAL-PROP"))
    apply_params = {"project_id": "P1", "change_id": change["change_id"], "expected_version": 0, "request_id": secret}
    tables = ("commands", "events", "handoff_imports", "design_documents", "task_plans", "requirement_changes")
    before = {t: repo.db.rows("SELECT * FROM " + t) for t in tables}
    if transport == "HTTP":
        web = ChangesWeb(root)
        server = make_server(web)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = http.client.HTTPConnection("127.0.0.1", server.server_port)
            try:
                client.request("POST", "/api/changes/apply", body=json.dumps(apply_params), headers={"Content-Type": "application/json", "X-Local-Token": web.token})
                response = client.getresponse()
                body = json.loads(response.read())
                assert response.status == 400
                assert secret not in json.dumps(body)
            finally:
                client.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
            web.close()
    else:
        with pytest.raises(DomainError) as error:
            if transport == "service":
                rc.apply(repo.db, root, apply_params)
            else:
                dispatch(parser().parse_args(["changes", "apply", change["change_id"], "0", "--project-id", "P1", "--root", str(root), "--request-id", secret]))
        assert secret not in str(error.value)
    assert before == {t: repo.db.rows("SELECT * FROM " + t) for t in tables}
