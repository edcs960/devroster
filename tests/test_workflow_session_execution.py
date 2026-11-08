"""ADR 0002 W3: one runner invocation per session, recorded per node."""

import json
from itertools import pairwise
from pathlib import Path

import pytest

pytest_plugins = ["test_workflow_runtime"]

from test_workflow_dag import prepare
from test_workflow_sessions import SessionRunner

from agent_dev.application.workflow_dag import session_config
from agent_dev.domain.role_policy import allowed_path, policy, session_policy

ISSUE = {
    "handoff_status": "issue",
    "reentry_step_id": "developer",
    "issue_type": "code",
    "root_cause": "fix",
}


def graph():
    nodes = [
        {"id": "planner", "kind": "role", "role_id": "planner", "session": "analysis"},
        {"id": "architect", "kind": "role", "role_id": "architect", "session": "analysis"},
        {
            "id": "developer",
            "kind": "role",
            "role_id": "developer",
            "session": "develop",
            "reentry_targets": ["developer"],
        },
        {
            "id": "qa",
            "kind": "role",
            "role_id": "qa",
            "session": "verify",
            "reentry_targets": ["developer"],
        },
        {"id": "reviewer", "kind": "role", "role_id": "reviewer", "session": "verify"},
    ]
    return {
        "schema_version": 1,
        "id": "w3",
        "name": "W3",
        "nodes": nodes,
        "edges": [{"from": a["id"], "to": b["id"]} for a, b in pairwise(nodes)],
    }


def attempts(runtime, node_id):
    return runtime.store.db.rows(
        "SELECT * FROM phase_attempts WHERE task_id='DAG' AND phase_id=?", (node_id,)
    )


class Writing(SessionRunner):
    """Writes the registered source file during the given session."""

    def __init__(self, session):
        super().__init__()
        self.session = session

    def execute(self, request):
        context = json.loads(request.context)
        if context.get("session_id") == self.session:
            worktree = context["repositories"]["source"]["worktree"]
            Path(worktree, "source.txt").write_text("changed by " + self.session, encoding="utf-8")
        return super().execute(request)


def test_issue_mid_session_reruns_target_and_drops_later_sections(runtime):
    prepare(runtime, graph())
    runner = SessionRunner(issue={"qa": ISSUE})
    assert runtime.execute("DAG", "run", runner=runner)["state"] == "verifying"
    assert runner.calls == [
        ("planner", "architect"),
        ("developer",),
        ("qa", "reviewer"),
        ("developer",),
        ("qa", "reviewer"),
    ]
    # The reviewer section written alongside the failing QA section was never recorded.
    assert len(attempts(runtime, "reviewer")) == 1
    assert len(attempts(runtime, "qa")) == 2
    # The attempt budget counts the five AI calls, not every member node or the
    # planner/architect passes copied into the re-entered generation.
    assert runtime.store.execution_usage("DAG")["attempts"] == len(runner.calls) == 5


class Partial(SessionRunner):
    """Drops the architect section from the analysis invocation."""

    def __init__(self, broken_retry=False):
        super().__init__()
        self.broken_retry = broken_retry

    def execute(self, request):
        result = super().execute(request)
        if "architect" in request.node_ids:
            cut = result.output.index("<<<NODE architect>>>")
            return type(result)(result.output[:cut], result.provider, result.usage)
        if request.phase_id == "architect" and self.broken_retry:
            return type(result)("no control block", result.provider)
        return result


def test_missing_section_runs_that_node_alone_once(runtime):
    prepare(runtime, graph())
    runner = Partial()
    assert runtime.execute("DAG", "run", runner=runner)["state"] == "verifying"
    assert runner.calls[:2] == [("planner", "architect"), ("architect",)]
    evidence = [
        json.loads(r["payload_json"])["result"]
        for r in runtime.store.db.rows(
            "SELECT payload_json FROM events WHERE aggregate_id='DAG' AND kind='WorkflowRoleResult'"
        )
    ]
    assert sum(1 for e in evidence if e.get("session_fallback")) == 1


def test_missing_section_blocks_when_the_retry_also_fails(runtime):
    prepare(runtime, graph())
    runner = Partial(broken_retry=True)
    result = runtime.execute("DAG", "run", runner=runner)
    assert result["state"] == "blocked"
    runs = runtime.store.node_runs("DAG")
    assert runs["planner"]["state"] == "passed"
    assert runs["architect"]["state"] == "failed"
    assert runner.calls == [("planner", "architect"), ("architect",)]


def test_readonly_session_write_blocks_the_session(runtime):
    prepare(runtime, graph())
    result = runtime.execute("DAG", "run", runner=Writing("analysis"))
    assert result["state"] == "blocked"
    assert result["error"] == "POLICY_VIOLATION"
    assert runtime.store.node_runs("DAG")["planner"]["state"] == "failed"


def test_develop_session_writes_inside_member_scope(runtime):
    prepare(runtime, graph())
    assert runtime.execute("DAG", "run", runner=Writing("develop"))["state"] == "verifying"


def test_session_policy_is_union_of_code_members(tmp_path):
    readonly = policy("planner", project_paths=["a.txt"])
    assert session_policy([readonly])["mode"] == "readonly"
    union = session_policy(
        [
            readonly,
            policy("developer", project_paths=["a.txt"]),
            policy("developer-frontend", project_paths=["b.txt"]),
        ]
    )
    assert union["mode"] == "code"
    assert union["capabilities"] == ["code.read", "code.write"]
    assert allowed_path(tmp_path, "a.txt", union)
    assert allowed_path(tmp_path, "b.txt", union)
    assert not allowed_path(tmp_path, "c.txt", union)


@pytest.mark.parametrize(
    ("members", "expected"),
    [
        ([{"role_id": "qa"}, {"role_id": "reviewer"}], {}),
        (
            [
                {"role_id": "qa", "effort": "low", "model": "m1"},
                {"role_id": "reviewer", "effort": "high", "provider": "claude_cli"},
            ],
            {"effort": "high", "model": "m1", "provider": "claude_cli"},
        ),
    ],
)
def test_session_config_shares_settings_and_highest_effort(members, expected):
    config = session_config("verify", members)
    assert config == {
        "id": "verify",
        "kind": "role",
        "role_id": "qa",
        "session": "verify",
        **expected,
    }


@pytest.mark.parametrize(
    ("members", "written", "sandbox", "blocked"),
    [
        (["planner", "architect"], "source.txt", "read-only", True),
        (["planner", "developer"], "source.txt", "workspace-write", False),
        (["planner", "developer"], "other.txt", "workspace-write", True),
    ],
)
def test_cli_adapter_enforces_session_policy(
    runtime, tmp_path, monkeypatch, members, written, sandbox, blocked
):
    import subprocess

    from agent_dev.adapters.cli_runner import CliRunnerAdapter
    from agent_dev.domain.contracts import PhaseRequest
    from agent_dev.domain.errors import DomainError

    prepare(runtime, graph())
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    (worktree / "source.txt").write_text("base", encoding="utf-8")
    union = session_policy([policy(m, project_paths=["source.txt"]) for m in members])
    context = {
        "repositories": {"source": {"worktree": str(worktree), "allowed_paths": ["source.txt"]}},
        "role_policy": union,
        "node_config": {"id": "s", "kind": "role", "role_id": members[0], "session": "s"},
    }
    adapter = CliRunnerAdapter("codex_cli", {}, tmp_path)
    seen = []

    def run(*args):
        seen.append(adapter.runner.sandbox)
        (worktree / written).write_text("changed", encoding="utf-8")
        return '```json\n{"handoff_status":"pass"}\n```'

    monkeypatch.setattr(adapter.runner, "run", run)
    request = PhaseRequest(
        runtime.store.task("DAG"),
        members[0],
        "fixture",
        "digest",
        0,
        frozenset(union["capabilities"]),
        json.dumps(context),
        node_ids=tuple(members),
    )
    if blocked:
        with pytest.raises(DomainError):
            adapter.execute(request)
    else:
        adapter.execute(request)
    assert seen == [sandbox]


class Rephrasing(SessionRunner):
    """QA keeps reporting the same problem in different words (seen in a live run)."""

    def __init__(self):
        super().__init__()
        self.reports = 0

    def execute(self, request):
        if "qa" in request.node_ids:
            self.reports += 1
            self.issue["qa"] = dict(ISSUE, root_cause=f"same failure, attempt {self.reports}")
        return super().execute(request)


def test_rephrased_root_cause_does_not_reset_the_retry_budget(runtime):
    prepare(runtime, graph())
    runner = Rephrasing()
    result = runtime.execute("DAG", "run", runner=runner)
    assert result["state"] == "blocked"
    assert "retry budget" in result["reason"]
    # One reentry (default max_reentries=1), then the second identical report blocks.
    assert runner.calls.count(("developer",)) == 2
    assert runner.reports == 2


class Merging(SessionRunner):
    """Answers the analysis session with one merged JSON instead of node sections, as Codex
    did in a real run; the format repair in the same conversation may fix it."""

    def __init__(self, repair_works):
        super().__init__()
        self.repair_works = repair_works

    def execute(self, request):
        from agent_dev.domain.contracts import PhaseResult

        if "architect" in request.node_ids:
            repair = "format_repair" in request.context
            if not repair:
                self.calls.append(request.node_ids)
                return PhaseResult(
                    '```json\n{"handoff_status":"pass","planner":{},"architect":{}}\n```',
                    "fake",
                    {"input_tokens": 7},
                    resume_session="fake:session-1",
                )
            assert request.resume_session == "fake:session-1"
            assert request.capabilities == frozenset({"code.read"})
            self.calls.append(("repair",))
            if not self.repair_works:
                return PhaseResult("still merged", "fake", {"input_tokens": 3})
            result = super().execute(request)
            self.calls.pop()
            return PhaseResult(result.output, "fake", {"input_tokens": 3})
        return super().execute(request)


def test_merged_session_answer_is_repaired_in_the_same_conversation(runtime):
    prepare(runtime, graph())
    runner = Merging(repair_works=True)
    assert runtime.execute("DAG", "run", runner=runner)["state"] == "verifying"
    assert runner.calls[:2] == [("planner", "architect"), ("repair",)]
    assert ("architect",) not in runner.calls  # no per-node fallback
    kinds = {r["kind"] for r in runtime.store.db.rows("SELECT kind FROM artifacts WHERE task_id='DAG'")}
    assert {"session_output", "session_repair"} <= kinds
    usage = [
        json.loads(r["payload_json"])
        for r in runtime.store.db.rows(
            "SELECT payload_json FROM events WHERE aggregate_id='DAG' AND kind='RunnerUsage'"
        )
    ]
    planner = next(u for u in usage if u["phase_id"] == "planner")
    assert planner["usage"]["input_tokens"] == 10  # session 7 + repair 3


def test_failed_repair_falls_back_and_keeps_the_session_usage(runtime):
    prepare(runtime, graph())
    runner = Merging(repair_works=False)
    assert runtime.execute("DAG", "run", runner=runner)["state"] == "verifying"
    assert runner.calls[:4] == [("planner", "architect"), ("repair",), ("planner",), ("architect",)]
    usage = [
        json.loads(r["payload_json"])
        for r in runtime.store.db.rows(
            "SELECT payload_json FROM events WHERE aggregate_id='DAG' AND kind='RunnerUsage'"
        )
    ]
    planner = next(u for u in usage if u["phase_id"] == "planner")
    assert planner["usage"]["input_tokens"] == 17  # session 7 + repair 3 + own run 7


def test_sections_after_an_issue_are_not_repaired(runtime):
    """Missing sections after a node's issue are expected; no format repair is spent."""

    class StopsAtIssue(SessionRunner):
        def execute(self, request):
            from agent_dev.domain.contracts import PhaseResult

            result = super().execute(request)
            if "format_repair" in request.context:
                raise AssertionError("no repair expected")
            return PhaseResult(result.output, "fake", result.usage, resume_session="fake:s")

    prepare(runtime, graph())
    runner = StopsAtIssue(issue={"developer": ISSUE})
    runtime.execute("DAG", "run", runner=runner)
    assert all("repair" not in str(call) for call in runner.calls)
