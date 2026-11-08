import json

import pytest

from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.adapters.workflow_definitions import preset, roster_graph
from agent_dev.domain import project_roles
from agent_dev.domain.errors import DomainError
from agent_dev.domain.role_policy import instruction, policy

CUSTOM = {
    "id": "custom-documents-perf0001",
    "name": "성능 검토자",
    "mode": "documents",
    "instruction": "응답 시간 목표를 검토한다.",
}


def roles(domain="generic", extra=()):
    value = project_roles.default_roles(domain)
    value["order"].extend(extra)
    return value


def role_nodes(graph):
    return [n["id"] for n in graph["nodes"] if n["kind"] == "role"]


def test_default_roster_follows_agents_md_and_domain_profile():
    assert roles()["order"] == list(project_roles.BASE)
    assert "fmea-expert" not in roles()["order"]
    fmea = roles("fmea")["order"]
    assert fmea.index("fmea-expert") == fmea.index("planner") + 1  # ADR 0002


def test_base_roles_cannot_be_removed_but_optional_roles_can():
    value = roles("fmea")
    value["order"].remove("fmea-expert")
    assert "fmea-expert" not in project_roles.normalize(value)["order"]
    value["order"].remove("qa")
    with pytest.raises(DomainError, match="기본 역할은 삭제할 수 없습니다"):
        project_roles.normalize(value)


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v["order"].append("unknown"),
        lambda v: v["order"].append("planner"),
        lambda v: v.update(domain="other"),
        lambda v: v["custom"].append(dict(CUSTOM, mode="code")),
        lambda v: v["custom"].append(dict(CUSTOM, instruction=" ")),
        lambda v: v["custom"].append(CUSTOM),
    ],
)
def test_roster_validation_rejects_invalid_input(change):
    value = roles()
    change(value)
    with pytest.raises(DomainError):
        project_roles.normalize(value)


def test_reordered_roster_with_custom_role_generates_workflow():
    value = roles(extra=[CUSTOM["id"]])
    value["custom"].append(CUSTOM)
    value["order"].remove("reviewer")
    value["order"].insert(0, "reviewer")
    graph = roster_graph(value)
    assert role_nodes(graph)[0] == "reviewer"
    assert role_nodes(graph)[-1] == CUSTOM["id"]
    assert "fmea-expert" not in role_nodes(graph)


def test_fmea_expert_runs_always_for_fmea_domain_and_on_risk_otherwise():
    fmea = roster_graph(roles("fmea"))
    assert "if-fmea-expert" not in {n["id"] for n in fmea["nodes"]}
    generic = roles(extra=["fmea-expert"])
    assert "if-fmea-expert" in {n["id"] for n in roster_graph(generic)["nodes"]}


def test_custom_role_authority_follows_its_mode():
    assert policy("custom-readonly-a1")["mode"] == "readonly"
    documents = policy(CUSTOM["id"], document_paths=["docs"])
    assert (documents["mode"], documents["structured_kind"]) == ("structured", "documents")
    assert policy("custom-code-a1")["capabilities"] == ["code.read", "code.write"]
    assert '{"edits"' in instruction(CUSTOM["id"], ["a"])
    assert "Do not modify" in instruction("custom-readonly-a1", ["a"])


def test_view_derives_roster_from_existing_preset_workflow():
    view = project_roles.view({"workflow": {"document": preset("quick-fix")}})
    assert view["order"][:2] == ["planner", "architect"]
    assert set(project_roles.BASE) <= set(view["order"])
    full = project_roles.view({"workflow": {"document": preset("full-feature")}})
    assert full["domain"] == "fmea"


def test_setup_saves_roster_and_keeps_workflow_when_roster_unchanged(tmp_path):
    root = tmp_path / "project"
    value = ProjectSetup(root).read()
    value["create_directories"] = True
    value["roles"]["custom"].append(CUSTOM)
    value["roles"]["order"].insert(1, CUSTOM["id"])
    ProjectSetup(root).save(value)
    _, _, profile = ProjectSetup(root).current_profile()
    assert profile["roles"]["custom"] == [CUSTOM]
    assert role_nodes(profile["workflow"]["document"])[1] == CUSTOM["id"]
    loaded = ProjectSetup(root).read()
    assert loaded["roles"] == profile["roles"]

    custom_graph = dict(profile["workflow"], document=preset("quick-fix"), id="quick-fix")
    from agent_dev.adapters.state.sqlite.database import Database
    from agent_dev.adapters.state.sqlite.store import WorkflowRepository

    store = WorkflowRepository(Database(ProjectSetup(root).db_path))
    project_version = store.db.rows(
        "SELECT version FROM projects WHERE id=?", (loaded["project_id"],)
    )[0]["version"]
    store.revise_profile(
        loaded["project_id"], dict(profile, workflow=custom_graph), project_version
    )
    reloaded = ProjectSetup(root).read()
    ProjectSetup(root).save(reloaded)
    _, _, saved = ProjectSetup(root).current_profile()
    assert saved["workflow"]["id"] == "quick-fix"


def test_snapshot_injects_custom_role_instruction(tmp_path):
    import subprocess

    from agent_dev.adapters.workflow_runtime import WorkflowRuntime

    source = tmp_path / "source"
    source.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.name", "T"],
        ["config", "user.email", "t@example.test"],
    ):
        subprocess.run(["git", "-C", str(source), *args], check=True)
    (source / "a.txt").write_text("a", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(source), "commit", "-qm", "a"], check=True)
    root = tmp_path / "app"
    (root / "config").mkdir(parents=True)
    (root / "config/runtime.json").write_text(json.dumps({"orchestrator_v2": {"execute": True}}), encoding="utf-8")
    runtime = WorkflowRuntime(root)
    value = roles(extra=[CUSTOM["id"]])
    value["custom"].append(CUSTOM)
    graph = roster_graph(value)
    from agent_dev.domain.content import digest

    runtime.store.register_project(
        "P",
        "P",
        {
            "repositories": {"source": {"path": str(source), "allowed_paths": ["a.txt"]}},
            "phases": [],
            "verification": {"commands": [], "required_gates": []},
            "roles": value,
            "workflow": {"id": graph["id"], "version": 1, "digest": digest(graph), "document": graph},
        },
    )
    runtime.store.design("P", "D", "## Scope\nS\n## Acceptance\nA\n## Constraints\nC")
    runtime.store.approve("design", "D", 1, "user")
    runtime.store.create_plan("P", "PLAN", "D", 1, [{"id": "T", "title": "Task"}])
    runtime.store.approve("plan", "PLAN", 1, "user")
    resolved = runtime.resolve_snapshot("T")
    text = resolved["role_context"][CUSTOM["id"]]
    assert CUSTOM["instruction"] in text and '{"edits"' in text
    assert resolved["role_policies"][CUSTOM["id"]]["structured_kind"] == "documents"

    unregistered = dict(value, custom=[])
    unregistered["order"] = [r for r in value["order"] if r != CUSTOM["id"]]
    runtime.store.register_project(
        "P2",
        "P2",
        {
            "repositories": {"source": {"path": str(source), "allowed_paths": ["a.txt"]}},
            "phases": [],
            "verification": {"commands": [], "required_gates": []},
            "roles": unregistered,
            "workflow": {"id": graph["id"], "version": 1, "digest": digest(graph), "document": graph},
        },
    )
    runtime.store.design("P2", "D2", "## Scope\nS\n## Acceptance\nA\n## Constraints\nC")
    runtime.store.approve("design", "D2", 1, "user")
    runtime.store.create_plan("P2", "PLAN2", "D2", 1, [{"id": "T2", "title": "Task"}])
    runtime.store.approve("plan", "PLAN2", 1, "user")
    with pytest.raises(DomainError, match="Unregistered project role"):
        runtime.resolve_snapshot("T2")
