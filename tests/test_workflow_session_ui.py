"""ADR 0002 W5: session settings round-trip and session progress in the execution view."""

pytest_plugins = ["test_workflow_runtime"]

from test_project_roles import role_nodes
from test_workflow_dag import prepare
from test_workflow_session_execution import ISSUE, graph
from test_workflow_sessions import SessionRunner

from agent_dev.adapters import execution_view
from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.domain import project_roles


def test_catalog_exposes_default_sessions():
    rows = {row["id"]: row for row in project_roles.catalog()}
    assert rows["planner"]["session"] == "analysis"
    assert rows["fmea-expert"]["session"] == "analysis"
    assert rows["tech-writer"]["session"] == "docs"


def test_setup_round_trips_session_mode_and_gates(tmp_path):
    root = tmp_path / "project"
    value = ProjectSetup(root).read()
    value["create_directories"] = True
    # New projects run sessions by default (decision C); setup can turn it off and on.
    assert value["roles"]["session_mode"] is True
    off = dict(value, roles=dict(value["roles"], session_mode=False))
    ProjectSetup(root).save(off)
    assert "session_mode" not in ProjectSetup(root).read()["roles"]
    value = ProjectSetup(root).read()
    value["roles"]["session_mode"] = True
    value["roles"]["gates"] = {"analysis": True, "design": True}
    ProjectSetup(root).save(value)
    _, _, profile = ProjectSetup(root).current_profile()
    document = profile["workflow"]["document"]
    assert {n["id"] for n in document["nodes"] if n["kind"] == "approval"} == {
        "approve-analysis",
        "approve-design",
    }
    assert {n.get("session") for n in document["nodes"] if n["kind"] == "role"} == {
        "analysis",
        "design",
        "develop",
        "verify",
        "docs",
    }
    # The settings screen sends the loaded roster back unchanged: the mode must survive.
    loaded = ProjectSetup(root).read()
    assert loaded["roles"]["session_mode"] is True
    ProjectSetup(root).save(loaded)
    _, _, saved = ProjectSetup(root).current_profile()
    assert saved["workflow"]["document"] == document

    loaded = ProjectSetup(root).read()
    del loaded["roles"]["session_mode"]
    ProjectSetup(root).save(loaded)
    _, _, plain = ProjectSetup(root).current_profile()
    nodes = plain["workflow"]["document"]["nodes"]
    assert not any(n.get("session") for n in nodes)
    assert not any(n["kind"] == "approval" for n in nodes)
    assert role_nodes(plain["workflow"]["document"])[:2] == ["planner", "architect"]


def test_execution_view_reports_session_progress(runtime):
    prepare(runtime, graph())
    runtime.execute("DAG", "run", runner=SessionRunner(issue={"reviewer": ISSUE}))
    data = execution_view.view(runtime.root, "DAG")
    sessions = {row["id"]: row for row in data["sessions"]}
    assert list(sessions) == ["analysis", "develop", "verify"]
    assert sessions["analysis"] == {"id": "analysis", "state": "passed", "roles": 2}
    assert sessions["verify"]["roles"] == 2
    assert {row["id"]: row.get("session") for row in data["stage_items"]}["qa"] == "verify"


def test_execution_view_without_sessions_has_empty_list(runtime):
    from test_workflow_dag import Passing, branch

    prepare(runtime, branch(), {"workstreams": ["frontend"]})
    runtime.execute("DAG", "first", runner=Passing())
    assert execution_view.view(runtime.root, "DAG")["sessions"] == []
