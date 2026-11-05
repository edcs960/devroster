import json

import pytest

pytest_plugins = ["test_workflow_runtime"]
from agent_dev.adapters.workflow_definitions import WorkflowDefinitions
from agent_dev.domain.contracts import PhaseResult
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.role_policy import apply_structured, policy


def prepare(runtime, graph, metadata=None, gate_command=None):
    profile = json.loads(
        runtime.store.db.rows(
            "SELECT content_json FROM project_profile_versions ORDER BY version DESC LIMIT 1"
        )[0]["content_json"]
    )
    profile["workflow"] = {"document": graph}
    if gate_command is not None:
        profile["verification"]["commands"][0]["command"] = gate_command
    profile["allowed_paths"] = ["source.txt"]
    runtime.store.revise_profile("P", profile, 0)
    runtime.store.create_plan(
        "P", "PLAN2", "D", 1, [dict({"id": "DAG", "title": "Implement"}, **(metadata or {}))]
    )
    runtime.store.approve("plan", "PLAN2", 1, "user")
    runtime.store.create_snapshot("DAG", runtime.resolve_snapshot("DAG"))


class Passing:
    def __init__(self):
        self.calls = []

    def execute(self, request):
        self.calls.append(request.phase_id)
        return PhaseResult('```json\n{"handoff_status":"pass"}\n```')


def branch():
    return {
        "schema_version": 1,
        "id": "dag",
        "name": "DAG",
        "nodes": [
            {"id": "choose", "kind": "condition", "field": "workstream", "equals": "frontend"},
            {"id": "ui", "kind": "role", "role_id": "designer"},
            {"id": "backend", "kind": "role", "role_id": "architect"},
            {"id": "join", "kind": "merge"},
            {"id": "human", "kind": "approval", "actor": "user"},
            {"id": "developer", "kind": "role", "role_id": "developer"},
            {"id": "verify", "kind": "verification", "gate_ids": ["unit"]},
        ],
        "edges": [
            {"from": "choose", "to": "ui", "when": True},
            {"from": "choose", "to": "backend", "when": False},
            {"from": "ui", "to": "join"},
            {"from": "backend", "to": "join"},
            {"from": "join", "to": "human"},
            {"from": "human", "to": "developer"},
            {"from": "developer", "to": "verify"},
        ],
    }


def test_condition_merge_approval_resume_and_actual_gates(runtime):
    prepare(runtime, branch(), {"workstreams": ["frontend"]})
    runner = Passing()
    result = runtime.execute("DAG", "first", runner=runner)
    assert result["reason"] == "AWAITING_WORKFLOW_APPROVAL"
    runs = runtime.store.node_runs("DAG")
    assert runs["backend"]["state"] == "skipped"
    assert runs["join"]["state"] == "passed"
    service = WorkflowDefinitions(runtime=runtime)
    with pytest.raises(DomainError):
        service.approve("DAG", "human", "forged", expected_version=runs["human"]["version"])
    service.approve("DAG", "human", "user", expected_version=runs["human"]["version"])
    with pytest.raises(DomainError):
        service.approve("DAG", "human", "user", expected_version=runs["human"]["version"])
    result = runtime.execute("DAG", "second", runner=runner)
    assert result["state"] == "verifying"
    assert runner.calls == ["ui", "developer"]
    assert runtime.store.node_runs("DAG")["verify"]["result"]["gates"][0]["exit_code"] == 0
    assert runtime.execute("DAG", "verified", verify=True)["passed"]
    checkpoint = runtime.artifacts.directory("DAG") / "latest-checkpoint.md"
    assert checkpoint.exists()
    # Routing nodes write no checkpoint, and a recorded checkpoint is not copied again.
    saved = sorted((checkpoint.parent / "checkpoints").iterdir())
    assert not [p for p in saved if p.name.endswith("-previous-checkpoint.md")]
    assert checkpoint.read_bytes() == saved[-1].read_bytes()
    reasons = [p.read_text(encoding="utf-8").split("- Reason: ", 1)[1].split("\n", 1)[0] for p in saved]
    assert not [r for r in reasons if r in {"node complete: join", "node skipped: backend"}]


def test_reentry_only_descendants_and_old_approval_rejected(runtime):
    graph = branch()
    graph["nodes"][5]["reentry_targets"] = ["developer"]
    prepare(runtime, graph, {"workstreams": ["frontend"]})
    runner = Passing()
    runtime.execute("DAG", "first", runner=runner)
    service = WorkflowDefinitions(runtime=runtime)
    service.approve("DAG", "human", "user", expected_version=0)

    class Reenter(Passing):
        def execute(self, request):
            self.calls.append(request.phase_id)
            if len(self.calls) == 1:
                return PhaseResult(
                    '```json\n{"handoff_status":"issue","reentry_step_id":"developer","issue_type":"code","root_cause":"fix"}\n```'
                )
            return PhaseResult('```json\n{"handoff_status":"pass"}\n```')

    reenter = Reenter()
    assert runtime.execute("DAG", "reentry", runner=reenter)["state"] == "verifying"
    assert runtime.store.generation("DAG") == 1
    assert reenter.calls == ["developer", "developer"]
    assert runtime.store.node_runs("DAG")["human"]["state"] == "passed"
    assert runtime.store.node_runs("DAG")["ui"]["state"] == "passed"
    assert (
        len(
            runtime.store.db.rows(
                "SELECT * FROM phase_attempts WHERE task_id='DAG' AND phase_id='developer'"
            )
        )
        == 2
    )


def test_late_node_results_fenced_by_generation(runtime):
    prepare(runtime, branch(), {"workstreams": ["frontend"]})
    runtime.execute("DAG", "first", runner=Passing())
    with runtime.safety.lease("task:DAG") as lease:
        with pytest.raises(DomainError) as error:
            runtime.store.record_node("DAG", "human", -1, "passed", "old", {}, lease)
        assert error.value.code == ErrorCode.POLICY_VIOLATION


def test_narrow_roles_reject_unregistered_paths_and_symlink(tmp_path):
    role = policy("tech-writer", project_paths=["docs", "source.py"], document_paths=["docs"])
    with pytest.raises(DomainError):
        apply_structured(tmp_path, [{"path": "source.py", "content": "bad"}], role)
    outside = tmp_path.parent / "outside-doc"
    outside.mkdir(exist_ok=True)
    (tmp_path / "docs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DomainError):
        apply_structured(tmp_path, [{"path": "docs/file.md", "content": "bad"}], role)
    assert not (outside / "file.md").exists()


def test_readonly_role_violation_blocks_even_custom_runner(runtime):
    prepare(runtime, branch(), {"workstreams": ["frontend"]})

    class Mutating(Passing):
        def execute(self, request):
            from pathlib import Path

            context = json.loads(request.context)
            Path(context["repositories"]["source"]["worktree"], "source.txt").write_text(
                "unauthorized"
            )
            return super().execute(request)

    result = runtime.execute("DAG", "mutate", runner=Mutating())
    assert result["state"] == "blocked"
    assert result["error"] == "POLICY_VIOLATION"
    assert runtime.store.node_runs("DAG")["ui"]["state"] == "failed"


def test_failed_gate_is_never_reported_as_pass_or_ready(runtime):
    import sys

    graph = branch()
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "human"]
    graph["edges"] = [e for e in graph["edges"] if e["from"] != "human" and e["to"] != "human"]
    graph["edges"].append({"from": "join", "to": "developer"})
    prepare(
        runtime, graph, {"workstreams": ["frontend"]}, [sys.executable, "-c", "raise SystemExit(3)"]
    )
    result = runtime.execute("DAG", "failed-gate", runner=Passing())
    assert result["state"] == "blocked"
    assert result["error"] == "VERIFICATION_FAILED"
    evidence = runtime.store.node_runs("DAG")["verify"]
    assert evidence["state"] == "failed"
    assert evidence["result"]["gates"][0]["exit_code"] == 3


def test_developer_delta_excludes_preexisting_other_node_changes(runtime):
    from pathlib import Path

    from agent_dev.adapters.role_execution import capture, enforce

    snapshot = runtime.store.snapshot("T")["resolved"]
    bindings = snapshot["repositories"]
    root = Path(bindings["source"]["worktree"])
    (root / "source.txt").write_text("prior approved developer delta")
    before = capture(bindings)
    # A zero-edit node has a narrower path grant; prior dirty files do not violate it.
    narrower = policy("developer", project_paths=["other.txt"], node_paths=["other.txt"])
    enforce(bindings, before, narrower)
    (root / "source.txt").write_text("unauthorized current delta")
    with pytest.raises(DomainError):
        enforce(bindings, before, narrower)


def test_internal_symlink_cannot_expand_document_writer_scope(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "source").symlink_to(tmp_path / "src", target_is_directory=True)
    document_policy = policy("tech-writer", project_paths=["docs"], document_paths=["docs"])
    with pytest.raises(DomainError):
        apply_structured(
            tmp_path, [{"path": "docs/source/product.md", "content": "bypass"}], document_policy
        )
    assert not (tmp_path / "src" / "product.md").exists()


def test_required_gate_false_command_flag_is_overridden(runtime):
    import sys

    from agent_dev.adapters.phase_catalog import SnapshotPhaseCatalog

    graph = branch()
    prepare(
        runtime, graph, {"workstreams": ["frontend"]}, [sys.executable, "-c", "raise SystemExit(3)"]
    )
    # Use a new approved task/profile with a contradictory optional command flag.
    profile = json.loads(
        runtime.store.db.rows(
            "SELECT content_json FROM project_profile_versions ORDER BY version DESC LIMIT 1"
        )[0]["content_json"]
    )
    profile["verification"]["commands"][0]["required"] = False
    runtime.store.revise_profile("P", profile, 1)
    runtime.store.create_plan("P", "PLAN3", "D", 1, [{"id": "GATE", "title": "Gate"}])
    runtime.store.approve("plan", "PLAN3", 1, "user")
    runtime.store.create_snapshot("GATE", runtime.resolve_snapshot("GATE"))
    with runtime.safety.lease("task:GATE") as lease:
        result = SnapshotPhaseCatalog(runtime.store, runtime.root).verify_node(
            "GATE", ["unit"], lease
        )
    assert result["passed"] is False
    assert result["gates"][0]["required"] is True


def test_narrow_proposal_invalid_control_never_applies(runtime):
    from pathlib import Path

    graph = {
        "schema_version": 1,
        "id": "docs",
        "name": "Docs",
        "nodes": [{"id": "writer", "kind": "role", "role_id": "tech-writer"}],
        "edges": [],
    }
    prepare(runtime, graph)

    class Invalid(Passing):
        def execute(self, request):
            return PhaseResult('```json\n{"edits":[{"path":"source.txt","content":"bad"}]}\n```')

    result = runtime.execute("DAG", "invalid-proposal", runner=Invalid())
    assert result["state"] == "blocked"
    binding = runtime.store.snapshot("DAG")["resolved"]["repositories"]["source"]
    assert Path(binding["worktree"], "source.txt").read_text() == "base"


def test_legacy_planner_cannot_expand_write_authority(runtime):
    from pathlib import Path

    profile = json.loads(
        runtime.store.db.rows(
            "SELECT content_json FROM project_profile_versions ORDER BY version DESC LIMIT 1"
        )[0]["content_json"]
    )
    profile["phases"] = [{"id": "01-planner", "role_id": "planner", "capabilities": ["code.write"]}]
    runtime.store.revise_profile("P", profile, 0)
    runtime.store.create_plan("P", "LEGACYPLAN", "D", 1, [{"id": "LEGACY", "title": "Inspect"}])
    runtime.store.approve("plan", "LEGACYPLAN", 1, "user")
    runtime.store.create_snapshot("LEGACY", runtime.resolve_snapshot("LEGACY"))
    resolved = runtime.store.snapshot("LEGACY")["resolved"]
    assert resolved["workflow_legacy"] is True
    assert resolved["role_policies"]["01-planner"]["mode"] == "readonly"
    assert resolved["phases"][0]["id"] == "01-planner"
    assert resolved["phases"][0]["capabilities"] == ["code.read"]

    class Mutating(Passing):
        def execute(self, request):
            assert "code.write" not in request.capabilities
            binding = json.loads(request.context)["repositories"]["source"]
            Path(binding["worktree"], "source.txt").write_text("forged planner write")
            return super().execute(request)

    result = runtime.execute("LEGACY", "forged-planner", runner=Mutating())
    assert result["state"] == "blocked" and result["error"] == "POLICY_VIOLATION"


def test_failed_gate_returns_to_developer_with_its_output(runtime):
    """A failing workflow gate only blocked the task; nothing sent it back to the
    developer, who never saw the failing test output."""
    import sys
    from pathlib import Path

    graph = {
        "schema_version": 1,
        "id": "gate-loop",
        "name": "Gate loop",
        "nodes": [
            {"id": "architect", "kind": "role", "role_id": "architect"},
            {"id": "developer", "kind": "role", "role_id": "developer"},
            {"id": "verify", "kind": "verification", "gate_ids": ["unit"]},
        ],
        "edges": [{"from": "architect", "to": "developer"}, {"from": "developer", "to": "verify"}],
    }
    check = "import sys; t=open('source.txt').read(); print('expected fixed, got ' + t); sys.exit(t != 'fixed')"
    prepare(runtime, graph, gate_command=[sys.executable, "-c", check])

    class Fixer:
        def __init__(self):
            self.contexts = []

        def execute(self, request):
            context = json.loads(request.context)
            self.contexts.append(context)
            if len(self.contexts) == 3:  # architect, developer, then developer again
                worktree = next(iter(context["repositories"].values()))["worktree"]
                Path(worktree, "source.txt").write_text("fixed")
            return PhaseResult('```json\n{"handoff_status":"pass"}\n```')

    runner = Fixer()
    assert runtime.execute("DAG", "fix", runner=runner)["state"] == "verifying"
    assert len(runner.contexts) == 3
    assert "expected fixed, got" in json.dumps(runner.contexts[2], ensure_ascii=False)
    # The architect pass copied into the new generation is not a spent attempt.
    assert runtime.store.execution_usage("DAG")["attempts"] == 3


def test_tech_writer_leaves_code_defects_to_verification():
    """The writer stopped a fixed task on an earlier run's test reports."""
    from agent_dev.domain.role_policy import instruction

    assert "not yours to rule on" in instruction("tech-writer")
    assert "not yours to rule on" not in instruction("qa")
