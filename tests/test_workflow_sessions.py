"""ADR 0002: role nodes grouped into sessions (W1 graph model and default flows)."""

pytest_plugins = ["test_workflow_runtime"]

import json
from itertools import pairwise

import pytest

from agent_dev.adapters.workflow_definitions import preset, roster_graph
from agent_dev.domain import project_roles
from agent_dev.domain.errors import DomainError
from agent_dev.domain.workflow_graph import (
    WorkflowValidationError,
    execution_order,
    normalize,
    session_spans,
)


def chain(*nodes):
    nodes = [dict(n) for n in nodes]
    return {
        "schema_version": 1,
        "id": "sessions",
        "name": "Sessions",
        "nodes": nodes,
        "edges": [{"from": a["id"], "to": b["id"]} for a, b in pairwise(nodes)],
    }


def role(node_id, session=None, **extra):
    node = {"id": node_id, "kind": "role", "role_id": node_id, **extra}
    if session:
        node["session"] = session
    return node


def session_roster(domain="fmea", **extra):
    value = project_roles.default_roles(domain)
    value["session_mode"] = True
    value.update(extra)
    return project_roles.normalize(value)


def kinds(graph):
    return [
        (n["id"], n.get("session"))
        for n in execution_order(graph)
        if n["kind"] in {"role", "approval"}
    ]


def test_session_field_is_optional_and_validated():
    assert "session" not in normalize(chain(role("planner"), role("qa")))["nodes"][0]
    with pytest.raises(WorkflowValidationError) as error:
        normalize(chain(role("planner", "bad session")))
    assert error.value.location == {"node": "planner", "field": "session"}


def test_condition_and_merge_may_sit_inside_a_session():
    graph = normalize(
        {
            "schema_version": 1,
            "id": "s",
            "name": "S",
            "nodes": [
                role("planner", "analysis"),
                {"id": "if-dba", "kind": "condition", "field": "risk", "equals": "db"},
                role("dba", "analysis"),
                {"id": "join-dba", "kind": "merge"},
                role("architect", "analysis"),
            ],
            "edges": [
                {"from": "planner", "to": "if-dba"},
                {"from": "if-dba", "to": "dba", "when": True},
                {"from": "if-dba", "to": "join-dba", "when": False},
                {"from": "dba", "to": "join-dba"},
                {"from": "join-dba", "to": "architect"},
            ],
        }
    )
    assert session_spans(graph) == {
        "analysis": ["planner", "if-dba", "dba", "join-dba", "architect"]
    }


@pytest.mark.parametrize(
    "middle",
    [
        role("architect"),
        role("architect", "design"),
        {"id": "approve", "kind": "approval"},
        {"id": "gate", "kind": "verification", "gate_ids": []},
    ],
)
def test_session_must_be_contiguous(middle):
    with pytest.raises(DomainError, match="Session must be contiguous: analysis"):
        normalize(chain(role("planner", "analysis"), middle, role("dba", "analysis")))


@pytest.mark.parametrize("field", ["module", "provider", "model"])
def test_session_nodes_share_runner_settings(field):
    with pytest.raises(DomainError, match="Session nodes must share " + field):
        normalize(
            chain(
                role("planner", "analysis", **{field: "a"}), role("dba", "analysis", **{field: "b"})
            )
        )
    # A node that leaves the setting empty inherits the session's value.
    normalize(chain(role("planner", "analysis", **{field: "a"}), role("dba", "analysis")))


def test_roster_without_session_mode_is_unchanged():
    value = project_roles.default_roles("fmea")
    assert project_roles.normalize(value) == value
    graph = roster_graph(value)
    assert all("session" not in n for n in graph["nodes"])
    assert not [n for n in graph["nodes"] if n["kind"] == "approval"]


def test_session_roster_builds_default_sessions_with_analysis_approval():
    graph = roster_graph(session_roster())
    assert kinds(graph) == [
        ("planner", "analysis"),
        ("fmea-expert", "analysis"),
        ("architect", "analysis"),
        ("dba", "analysis"),
        ("approve-analysis", None),
        ("ux-ui", "design"),
        ("designer", "design"),
        ("developer", "develop"),
        ("developer-frontend", "develop"),
        ("developer-backend", "develop"),
        ("developer-infra", "develop"),
        ("qa", "verify"),
        ("test-automation", "verify"),
        ("security", "verify"),
        ("reviewer", "verify"),
        ("tech-writer", "docs"),
    ]
    assert list(session_spans(graph)) == ["analysis", "design", "develop", "verify", "docs"]
    writer = next(n for n in graph["nodes"] if n["id"] == "tech-writer")
    assert writer["tools"] == [project_roles.GRAPH_CONNECTION]


def test_gates_add_or_remove_session_approvals():
    graph = roster_graph(session_roster(gates={"analysis": False, "design": True}))
    approvals = [n["id"] for n in graph["nodes"] if n["kind"] == "approval"]
    assert approvals == ["approve-design"]


def test_reordered_roster_splits_a_returning_session():
    value = session_roster("generic")
    value["order"].remove("security")
    value["order"].insert(value["order"].index("developer"), "security")
    sessions = project_roles.sessions(value)
    assert sessions["security"] == "verify"
    assert sessions["qa"] == "verify-2"
    roster_graph(value)  # contiguous after splitting


def test_custom_roles_join_sessions_by_mode():
    value = project_roles.default_roles("generic")
    value["custom"] = [
        {
            "id": "custom-readonly-intro",
            "name": "선행 검토",
            "mode": "readonly",
            "instruction": "검토",
        },
        {"id": "custom-code-perf", "name": "성능 개선", "mode": "code", "instruction": "개선"},
        {
            "id": "custom-documents-glossary",
            "name": "용어",
            "mode": "documents",
            "instruction": "정리",
        },
    ]
    value["order"].insert(0, "custom-readonly-intro")
    value["order"].insert(value["order"].index("qa"), "custom-code-perf")
    value["order"].insert(value["order"].index("architect"), "custom-documents-glossary")
    sessions = project_roles.sessions(project_roles.normalize(value))
    assert "custom-readonly-intro" not in sessions
    assert sessions["custom-code-perf"] == "develop"
    assert sessions["custom-documents-glossary"] == "analysis"


@pytest.mark.parametrize(
    "change",
    [
        {"session_mode": "yes"},
        {"gates": {"unknown": True}},
        {"gates": {"analysis": 1}},
        {"gates": []},
    ],
)
def test_roster_session_settings_are_validated(change):
    value = project_roles.default_roles()
    value.update(change)
    with pytest.raises(DomainError):
        project_roles.normalize(value)


def test_fmea_expert_follows_planner_in_presets():
    for preset_id in ("full-feature", "fmea-feature"):
        roles = [n["id"] for n in execution_order(preset(preset_id)) if n["kind"] == "role"]
        assert roles[:2] == ["planner", "fmea-expert"]


class SessionRunner:
    """Answers a session invocation with one passing section per node."""

    def __init__(self, issue=None):
        self.calls, self.issue = [], issue or {}

    def execute(self, request):
        from agent_dev.domain.contracts import PhaseResult
        from agent_dev.domain.session_output import END, START

        ids = request.node_ids or (request.phase_id,)
        self.calls.append(ids)
        sections = []
        for node_id in ids:
            control = self.issue.pop(node_id, {"handoff_status": "pass"})
            body = "```json\n" + json.dumps(control) + "\n```"
            sections.append(
                body
                if not request.node_ids
                else f"{START.format(node_id)}\n{body}\n{END.format(node_id)}"
            )
        return PhaseResult("\n".join(sections), "fake", {"input_tokens": 7})


def test_session_roster_graph_runs_one_invocation_per_session(runtime):
    from test_workflow_dag import prepare

    from agent_dev.adapters.workflow_definitions import WorkflowDefinitions

    graph = roster_graph(session_roster())
    prepare(runtime, graph, {"workstreams": ["frontend", "backend"], "risk": ["db"]})
    runner = SessionRunner()
    result = runtime.execute("DAG", "first", runner=runner)
    assert result["reason"] == "AWAITING_WORKFLOW_APPROVAL"
    assert runner.calls == [("planner", "fmea-expert", "architect", "dba")]
    runs = runtime.store.node_runs("DAG")
    assert all(runs[n]["state"] == "passed" for n in runner.calls[0])
    WorkflowDefinitions(runtime=runtime).approve(
        "DAG", "approve-analysis", "user", expected_version=runs["approve-analysis"]["version"]
    )
    result = runtime.execute("DAG", "second", runner=runner)
    assert result["state"] == "verifying"
    assert runner.calls[1:] == [
        ("ux-ui", "designer"),
        # developer-infra is skipped: its workstream condition is false.
        ("developer", "developer-frontend", "developer-backend"),
        ("qa", "test-automation", "security", "reviewer"),
        ("tech-writer",),
    ]
    assert runtime.store.node_runs("DAG")["developer-infra"]["state"] == "skipped"
    usage = [
        json.loads(r["payload_json"])
        for r in runtime.store.db.rows(
            "SELECT payload_json FROM events WHERE aggregate_id='DAG' AND kind='RunnerUsage'"
        )
    ]
    assert len(usage) == 14  # one attempt per role node
    assert sum(1 for u in usage if u["usage"]) == 5  # one paid invocation per session


def test_bundled_agents_md_order_matches_role_catalog():
    import re

    from agent_dev import defaults

    text = (defaults.DEFAULTS_DIR / "AGENTS.md").read_text(encoding="utf-8")
    section = text.split("## 2. 기본 개발 파이프라인")[1].split("###")[0]
    listed = re.findall(r"^\d+\. (.+)$", section, re.MULTILINE)
    # Domain roles (FMEA Expert) are listed by their domain pack, not the generic rules.
    domain_roles = {role for roles in project_roles.DOMAINS.values() for role in roles}
    assert listed == [
        project_roles.LABELS[role] for role in project_roles.CATALOG if role not in domain_roles
    ]


def test_bundled_session_table_matches_role_sessions():
    from agent_dev import defaults

    text = (defaults.DEFAULTS_DIR / "AGENTS.md").read_text(encoding="utf-8")
    table = text.split("### 2.1 세션 실행")[1].split("\n\n- ")[0]
    for session in project_roles.SESSIONS:
        assert f"`{session}`" in table


def test_gate_note_reaches_later_roles(runtime):
    """The developer blocked on a decision the analysis left open; the note
    written at the analysis gate now carries the user's decision to later roles."""
    from test_workflow_dag import prepare

    from agent_dev.adapters.workflow_definitions import WorkflowDefinitions

    prepare(runtime, roster_graph(session_roster()), {"workstreams": ["frontend"]})
    runner = SessionRunner()
    runtime.execute("DAG", "first", runner=runner)
    runs = runtime.store.node_runs("DAG")
    note = "연결 방식: src/server.py 표준 라이브러리 HTTP 서버를 추가한다."
    WorkflowDefinitions(runtime=runtime).approve(
        "DAG",
        "approve-analysis",
        "user",
        expected_version=runs["approve-analysis"]["version"],
        note=note,
    )
    assert runtime.store.node_runs("DAG")["approve-analysis"]["result"]["note"] == note
    context = json.loads(runtime.artifacts.context("DAG", "developer"))
    assert context["user_decisions"] == [
        {"node_id": "approve-analysis", "decision": "approved", "note": note}
    ]
