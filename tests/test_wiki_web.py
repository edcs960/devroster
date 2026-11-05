"""Wiki review page: the CLI approval rules, reached from the web (04 progress item 9)."""

import json
import threading
import urllib.request

import pytest

from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.ui.wiki_server import WikiWeb

RESOLVED_ISSUE = "## Root Cause\nmigration updated rows\n\n## Resolution\n- Resolved: true\n"


@pytest.fixture
def root(tmp_path):
    root = tmp_path / "app"
    (root / "config").mkdir(parents=True)
    (root / "config/runtime.json").write_text(json.dumps({"orchestrator_v2": {}}))
    runtime = WorkflowRuntime(root)
    runtime.store.register_project("P", "Project", {})
    (root / "issues").mkdir()
    (root / "issues/issue-001.md").write_text(RESOLVED_ISSUE)
    with runtime.store.db.transaction() as sql:
        sql.execute(
            "INSERT INTO workspace_settings VALUES(1,?,?)", (1, json.dumps({"project_id": "P"}))
        )
    entries = [
        {
            "item_id": item_id,
            "page": "lessons",
            "title": "보호 행 무갱신",
            "check": "마이그레이션은 RELEASED 행을 갱신하지 않는다.",
            "condition": "",
            "roles": ["dba"],
            "sources": ["issues/issue-001.md"],
        }
        for item_id in ("L01", "L02")
    ]
    runtime.wiki.propose("P", entries)
    return root


def test_bootstrap_and_items_list_candidates(root):
    web = WikiWeb(root)
    boot = web.get("bootstrap", {})
    assert boot["projects"] == [{"id": "P", "name": "Project"}]
    assert boot["workspace_project"] == "P"
    items = web.get("items", {"project_id": "P"})["items"]
    assert [(i["item_id"], i["state"]) for i in items] == [
        ("L01", "candidate"),
        ("L02", "candidate"),
    ]


def test_approve_reject_retire_follow_the_cli_rules_and_render(root):
    web = WikiWeb(root)
    approved = web.post(
        "approve", {"project_id": "P", "item_id": "L01", "revision": 1, "actor": "kim"}
    )
    assert approved["state"] == "active"
    assert approved["rendered"]["active"] == 1
    assert (root / "docs/wiki/lessons.md").is_file()
    assert (
        web.post("reject", {"project_id": "P", "item_id": "L02", "revision": 1, "actor": "kim"})[
            "state"
        ]
        == "rejected"
    )
    with pytest.raises(DomainError) as error:  # only a candidate can be approved
        web.post("approve", {"project_id": "P", "item_id": "L02", "revision": 1, "actor": "kim"})
    assert error.value.code == ErrorCode.STALE_VERSION
    with pytest.raises(DomainError):  # retirement needs a reason
        web.post("retire", {"project_id": "P", "item_id": "L01", "actor": "kim", "reason": " "})
    retired = web.post(
        "retire", {"project_id": "P", "item_id": "L01", "actor": "kim", "reason": "규칙 변경"}
    )
    assert retired["state"] == "retired"
    states = {
        i["item_id"]: (i["state"], i["approval"]["actor"])
        for i in web.get("items", {"project_id": "P"})["items"]
    }
    assert states == {"L01": ("retired", "kim"), "L02": ("rejected", "kim")}


@pytest.mark.parametrize(
    "payload",
    [
        {"project_id": "P", "item_id": "L01", "revision": 1, "actor": ""},
        {"project_id": "P", "item_id": "L01", "revision": "1", "actor": "kim"},
        {"project_id": "", "item_id": "L01", "revision": 1, "actor": "kim"},
    ],
)
def test_decisions_need_an_actor_and_a_revision(root, payload):
    with pytest.raises(DomainError) as error:
        WikiWeb(root).post("approve", payload)
    assert error.value.code == ErrorCode.INVALID_INPUT


def test_app_serves_the_page_and_rejects_untokened_writes(root):
    from agent_dev import app

    server = app.make_server(root, 0)
    try:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        with urllib.request.urlopen(base + "/wiki", timeout=5) as page:
            assert "위키 승인" in page.read().decode()
        with urllib.request.urlopen(base + "/api/wiki/bootstrap", timeout=5) as response:
            assert json.loads(response.read())["token"]
        request = urllib.request.Request(
            base + "/api/wiki/approve",
            data=json.dumps(
                {"project_id": "P", "item_id": "L01", "revision": 1, "actor": "x"}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 403
    finally:
        server.shutdown()
        server.studio.close()
        server.server_close()
