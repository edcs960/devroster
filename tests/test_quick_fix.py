"""Quick fix: one description and one confirmation create the usual approvals and run."""

import json
from pathlib import Path

import pytest
from test_budget_pause import runtime_with

from agent_dev.adapters import quick_fix
from agent_dev.adapters.agent_console import commands
from agent_dev.adapters.preparation import AI_OPERATIONS
from agent_dev.adapters.workflow_definitions import preset, trusted_metadata
from agent_dev.domain.contracts import PhaseResult
from agent_dev.domain.errors import DomainError

POLICY = {"estimated_budget": 100.0, "price_book": []}
PASS = '```json\n{"handoff_status":"pass","summary":"ok"}\n```'


class Runner:
    def __init__(self):
        self.calls = []

    def execute(self, request):
        self.calls.append(request.phase_id)
        if request.phase_id == "developer":
            root = Path(json.loads(request.context)["repositories"]["source"]["worktree"])
            (root / "source.txt").write_text("fixed", encoding="utf-8")
        return PhaseResult(PASS, provider="codex_cli")


@pytest.fixture
def runtime(tmp_path):
    return runtime_with(tmp_path, POLICY)


@pytest.mark.parametrize(
    "module,title,description",
    [("missing", "제목", "내용"), ("source", "", "내용"), ("source", "x" * 121, "내용"), ("source", "두\n줄", "내용"), ("source", "제목", " ")],
)
def test_invalid_input_is_refused_before_anything_is_written(runtime, module, title, description):
    with pytest.raises(DomainError):
        quick_fix.preview(runtime.store, module, title, description)
    assert runtime.store.db.rows("SELECT id FROM design_documents WHERE id LIKE 'QF-%'") == []


def test_preview_shows_design_scope_and_flow(runtime):
    plan = quick_fix.preview(runtime.store, "source", "로그인 버튼 오류", "버튼을 눌러도 반응이 없다.")
    assert plan["roles"] == ["developer", "qa", "reviewer"]
    assert plan["allowed_paths"] == ["source.txt"] and plan["verification_gates"] == ["unit"]
    assert "## 범위" in plan["design"] and "## 수용 기준" in plan["design"] and "## 제약" in plan["design"]


def test_one_confirmation_records_both_approvals_and_runs(runtime):
    created = quick_fix.create(runtime, "source", "로그인 버튼 오류", "버튼을 눌러도 반응이 없다.", "local-web-user")
    task_id = created["task_id"]
    approvals = runtime.store.db.rows("SELECT target_type AS kind,target_id,actor FROM approvals ORDER BY target_type")
    mine = [(a["kind"], a["target_id"], a["actor"]) for a in approvals if a["target_id"].startswith("QF-")]
    assert mine == [("design", created["design_id"], "local-web-user"), ("plan", created["plan_id"], "local-web-user")]
    resolved = runtime.store.snapshot(task_id)["resolved"]
    assert [n["id"] for n in resolved["workflow_graph"]["nodes"] if n["kind"] == "role"] == ["developer", "qa", "reviewer"]
    assert resolved["allowed_paths"] == ["source.txt"] and resolved["execution_module"] == "source"
    runner = Runner()
    assert runtime.execute(task_id, "run", runner=runner)["state"] == "verifying"
    assert runner.calls == ["developer", "qa", "reviewer"]


def test_operator_console_offers_quick_fix_as_an_ai_operation():
    op = commands()["task.quick"]
    assert op["label"] == "빠른 수정" and [f["label"] for f in op["fields"]] == ["모듈", "제목", "고칠 내용"]
    assert "task.quick" in AI_OPERATIONS


def test_workflow_configuration_is_not_read_as_task_impact():
    metadata = trusted_metadata({"title": "버튼 수정", "config_override": {"workflow": {"document": preset("quick-fix")}}})
    assert metadata["risk"] == [] and "config_override" not in metadata
    assert trusted_metadata({"title": "DB 스키마 마이그레이션"})["risk"] == ["db"]
