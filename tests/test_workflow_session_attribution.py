"""Per-node change attribution and one cross-check per session."""

import json
from itertools import pairwise
from pathlib import Path

import pytest

pytest_plugins = ["test_workflow_runtime"]

from test_workflow_dag import prepare

from agent_dev.domain.contracts import PhaseResult
from agent_dev.domain.session_output import END, START


def chain(*nodes):
    return {
        "schema_version": 1,
        "id": "attr",
        "name": "Attribution",
        "nodes": list(nodes),
        "edges": [{"from": a["id"], "to": b["id"]} for a, b in pairwise(nodes)],
    }


def role(node_id, session, **extra):
    return {"id": node_id, "kind": "role", "role_id": node_id, "session": session, **extra}


class Writer:
    """Writes source.txt during the session and answers with chosen control fields."""

    def __init__(self, controls, write=True):
        self.controls, self.write, self.calls, self.reviews = controls, write, [], []

    def execute(self, request):
        context = json.loads(request.context)
        self.calls.append(request.node_ids)
        if self.write and request.node_ids:
            Path(context["repositories"]["source"]["worktree"], "source.txt").write_text("new", encoding="utf-8")
        sections = []
        for node_id in request.node_ids or (request.phase_id,):
            control = {"handoff_status": "pass", **self.controls.get(node_id, {})}
            body = "```json\n" + json.dumps(control) + "\n```"
            sections.append(f"{START.format(node_id)}\n{body}\n{END.format(node_id)}")
        return PhaseResult("\n".join(sections), "fake")

    def cross_check(self, request, result, *, required=False):
        self.reviews.append((request.node_ids, required))
        return {"state": "passed"}


def evidence(runtime):
    return {
        json.loads(r["payload_json"])["attempt_id"]: json.loads(r["payload_json"])["result"]
        for r in runtime.store.db.rows(
            "SELECT payload_json FROM events WHERE aggregate_id='DAG' AND kind='WorkflowRoleResult'"
        )
    }


def node_evidence(runtime, node_id):
    attempts = {
        r["id"]
        for r in runtime.store.db.rows(
            "SELECT id FROM phase_attempts WHERE task_id='DAG' AND phase_id=?", (node_id,)
        )
    }
    return next(v for k, v in evidence(runtime).items() if k in attempts)


def test_declared_changes_are_recorded_per_node(runtime):
    prepare(runtime, chain(role("developer", "develop"), role("developer-backend", "develop")))
    runner = Writer({"developer": {"changed_paths": "source.txt"}})
    assert runtime.execute("DAG", "run", runner=runner)["state"] == "verifying"
    assert node_evidence(runtime, "developer")["session_changes"] == {
        "changed_paths": ["source.txt"],
        "unattributed_paths": [],
    }


def test_undeclared_changes_are_recorded_as_unattributed(runtime):
    prepare(runtime, chain(role("developer", "develop"), role("developer-backend", "develop")))
    assert runtime.execute("DAG", "run", runner=Writer({}))["state"] == "verifying"
    assert node_evidence(runtime, "developer")["session_changes"] == {
        "unattributed_paths": ["source.txt"]
    }


def test_declared_change_outside_the_nodes_own_scope_blocks(runtime):
    # The session may write source.txt (developer), but the read-only architect may not.
    prepare(runtime, chain(role("architect", "mix"), role("developer", "mix")))
    result = runtime.execute(
        "DAG", "run", runner=Writer({"architect": {"changed_paths": "source.txt"}})
    )
    assert result["state"] == "blocked"
    assert "세션의 'architect' 단계가 자기 수정 허용 경로 밖의 파일을 바꿨다고 보고했습니다: source.txt." in result["reason"]


def worktree(runtime):
    return Path(runtime.store.snapshot("DAG")["resolved"]["repositories"]["source"]["worktree"])


def refused_copies(runtime, name):
    folder = worktree(runtime).parent / ".attempts"
    return [p.read_text(encoding="utf-8") for p in folder.glob(f"*-refused/source/{name}")]


def test_running_again_puts_back_what_the_wrong_session_node_wrote(runtime):
    prepare(runtime, chain(role("architect", "mix"), role("developer", "mix")))
    runner = Writer({"architect": {"changed_paths": "source.txt"}})
    blocked = runtime.execute("DAG", "run", runner=runner)
    assert blocked["state"] == "blocked" and "이어서 실행하면 이 변경을" in blocked["reason"]
    assert (worktree(runtime) / "source.txt").read_text(encoding="utf-8") == "new"
    # Left in place, the next session would start from "new" and the architect's write would stand.
    runner.controls, runner.write = {}, False
    assert runtime.execute("DAG", "run-2", runner=runner)["state"] == "verifying"
    assert (worktree(runtime) / "source.txt").read_text(encoding="utf-8") == "base"
    assert refused_copies(runtime, "source.txt") == ["new"]


class Stray(Writer):
    """The first session call also writes a file no member may change."""

    def execute(self, request):
        if request.node_ids and not self.calls:
            root = json.loads(request.context)["repositories"]["source"]["worktree"]
            Path(root, "package.json").write_text("{}", encoding="utf-8")
        return super().execute(request)


def test_running_again_puts_back_a_session_change_outside_every_members_scope(runtime):
    prepare(runtime, chain(role("developer", "develop"), role("developer-backend", "develop")))
    runner = Stray({})
    blocked = runtime.execute("DAG", "run", runner=runner)
    assert blocked["state"] == "blocked"
    assert "밖의 파일을 바꿨습니다: package.json." in blocked["reason"]
    assert "이어서 실행하면 위 변경을" in blocked["reason"]
    assert runtime.execute("DAG", "run-2", runner=runner)["state"] == "verifying"
    assert not (worktree(runtime) / "package.json").exists()
    assert refused_copies(runtime, "package.json") == ["{}"]
    # What the session was allowed to write is kept.
    assert (worktree(runtime) / "source.txt").read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize(
    ("policies", "required"), [(("optional", "off"), False), (("optional", "required"), True)]
)
def test_one_cross_check_per_session_with_strictest_policy(runtime, policies, required):
    prepare(
        runtime,
        chain(
            role("qa", "verify", cross_check=policies[0]),
            role("reviewer", "verify", cross_check=policies[1]),
        ),
    )
    runner = Writer({}, write=False)
    assert runtime.execute("DAG", "run", runner=runner)["state"] == "verifying"
    assert runner.reviews == [(("qa", "reviewer"), required)]
    assert node_evidence(runtime, "reviewer")["cross_check"] == {
        "state": "passed",
        "scope": "session",
    }
