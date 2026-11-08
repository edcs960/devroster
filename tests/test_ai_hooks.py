"""Project hooks (.ai/hooks): validation, approval, and the moments DevRoster runs them."""

import json
import os
import sys

import pytest
from test_reusable_workspace import Runner, approved, workspace  # noqa: F401 - fixture

from agent_dev.adapters import ai_hooks
from agent_dev.domain.errors import DomainError, ErrorCode


def write_hooks(root, value):
    path = root / ".ai/hooks/hooks.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def py(code):
    return [sys.executable, "-c", code]


def approve(root):
    config = ai_hooks._config(root)
    return ai_hooks.approve(config, root, ai_hooks.read_hooks(root)["digest"])


@pytest.mark.parametrize(
    "value",
    [
        {"after_commit": [{"run": ["true"]}]},
        {"after_edit": [{"run": "npm run lint"}]},
        {"after_edit": [{"run": []}]},
        {"after_edit": [{"run": ["x"], "timeout": 0}]},
        {"after_edit": [{"run": ["x"], "timeout": 601}]},
        {"after_edit": [{"run": ["echo", "ghp_" + "a" * 30]}]},
        {"after_edit": [{"run": ["x"]}] * 11},
        ["not", "an", "object"],
    ],
    ids=["unknown-event", "shell-string", "empty", "timeout-0", "timeout-601", "secret", "too-many", "list"],
)
def test_invalid_hooks_are_refused(tmp_path, value):
    write_hooks(tmp_path, value)
    with pytest.raises(DomainError):
        ai_hooks.read_hooks(tmp_path)


def test_links_inside_hooks_are_refused(tmp_path):
    write_hooks(tmp_path, {"after_edit": [{"run": ["x"]}]})
    (tmp_path / "outside.sh").write_text("echo", encoding="utf-8")
    os.symlink(tmp_path / "outside.sh", tmp_path / ".ai/hooks/run.sh")
    with pytest.raises(DomainError):
        ai_hooks.read_hooks(tmp_path)


def test_no_hooks_file_means_no_hooks(tmp_path):
    assert ai_hooks.read_hooks(tmp_path) == {"events": {}, "digest": ""}


def test_approval_never_lands_in_a_plain_repository(tmp_path):
    write_hooks(tmp_path, {"after_edit": [{"run": ["x"]}]})
    with pytest.raises(DomainError):
        ai_hooks.approve(tmp_path, tmp_path, ai_hooks.read_hooks(tmp_path)["digest"])
    assert not (tmp_path / ai_hooks.APPROVAL_FILE).exists()


def test_tasks_need_the_exact_hooks_approved(workspace):  # noqa: F811
    write_hooks(workspace, {"before_verify": [{"run": py("print('ok')")}]})
    with pytest.raises(DomainError) as error:
        approved(workspace)
    assert error.value.code == ErrorCode.APPROVAL_REQUIRED
    stale = ai_hooks.read_hooks(workspace)["digest"]
    (workspace / ".ai/hooks/prepare.sh").write_text("echo changed", encoding="utf-8")
    with pytest.raises(DomainError) as error:
        ai_hooks.approve(ai_hooks._config(workspace), workspace, stale)
    assert error.value.code == ErrorCode.STALE_VERSION  # Reviewed content changed meanwhile.
    approve(workspace)
    from agent_dev.adapters.workflow_runtime import WorkflowRuntime

    runtime = WorkflowRuntime(workspace)  # Design and plan were approved before the refusal.
    runtime.store.create_snapshot("T", runtime.resolve_snapshot("T"))
    assert "before_verify" in runtime.store.snapshot("T")["resolved"]["project_hooks"]["events"]


def test_hooks_changed_after_approval_do_not_run(tmp_path):
    write_hooks(tmp_path, {"after_edit": [{"run": py("print('ran')")}]})
    bundle = dict(ai_hooks.read_hooks(tmp_path), root=str(tmp_path))
    assert ai_hooks.run("after_edit", bundle, tmp_path, tmp_path, {})[0]["ok"]
    write_hooks(tmp_path, {"after_edit": [{"run": py("print('changed')")}]})
    result = ai_hooks.run("after_edit", bundle, tmp_path, tmp_path, {})
    assert not result[0]["ok"] and "바뀌어" in result[0]["output"]


def test_hook_gets_context_but_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-for-hooks-123456")
    write_hooks(tmp_path, {"after_edit": [{"run": py(
        "import os; print(os.environ.get('DEVROSTER_EVENT'), os.environ.get('DEVROSTER_PHASE'), "
        "os.environ.get('OPENAI_API_KEY'))"
    )}]})
    bundle = dict(ai_hooks.read_hooks(tmp_path), root=str(tmp_path))
    output = ai_hooks.run("after_edit", bundle, tmp_path, tmp_path, {"phase": "developer"})[0]["output"]
    assert output.split() == ["after_edit", "developer", "None"]


def test_failing_after_edit_sends_the_code_phase_back(workspace):  # noqa: F811
    write_hooks(workspace, {"after_edit": [{"name": "lint", "run": py("import sys; print('lint 실패'); sys.exit(3)")}]})
    approve(workspace)
    runtime = approved(workspace)
    result = runtime.execute("T", "run", runner=Runner())
    assert result["state"] != "verifying"
    runs = [json.loads(r["payload_json"]) for r in runtime.store.db.rows(
        "SELECT payload_json FROM events WHERE aggregate_id='T' AND kind='HookRun'"
    )]
    assert runs and runs[0]["event"] == "after_edit" and runs[0]["results"][0]["exit_code"] == 3
    assert "lint 실패" in runs[0]["results"][0]["output"]


def test_passing_after_edit_lets_the_task_continue(workspace):  # noqa: F811
    write_hooks(workspace, {"after_edit": [{"run": py("print('lint ok')")}]})
    approve(workspace)
    runtime = approved(workspace)
    assert runtime.execute("T", "run", runner=Runner())["state"] == "verifying"


def test_failing_before_verify_fails_verification(workspace):  # noqa: F811
    write_hooks(workspace, {"before_verify": [{"name": "db", "run": py("import sys; sys.exit(1)")}]})
    approve(workspace)
    runtime = approved(workspace)
    runtime.execute("T", "run", runner=Runner())
    runtime.execute("T", "verify", verify=True)
    rows = runtime.store.db.rows(
        "SELECT r.gate, r.passed FROM verification_results r JOIN verification_runs v ON v.id=r.run_id WHERE v.task_id='T'"
    )
    assert {"gate": "hook:db", "passed": 0} in [dict(r) for r in rows]
    assert runtime.store.task("T").state.value != "pr_ready"


def test_record_only_hooks_follow_task_events(workspace, tmp_path):  # noqa: F811
    marker = tmp_path / "phases.txt"
    write_hooks(workspace, {"phase_end": [{"run": py(
        f"import os; open({str(marker)!r}, 'a', encoding='utf-8').write(os.environ['DEVROSTER_PHASE'] + '\\n')"
    )}]})
    approve(workspace)
    runtime = approved(workspace)
    runtime.execute("T", "run", runner=Runner())
    worker = runtime.store.hook_observer.worker
    if worker:
        worker.join(timeout=30)
    assert marker.read_text(encoding="utf-8").split() == ["planner", "developer", "qa"]


def test_studio_confirmation_runs_approved_hooks(workspace, tmp_path):  # noqa: F811
    marker = tmp_path / "studio.txt"
    write_hooks(workspace, {"studio_confirmed": [{"run": py(
        f"import os; open({str(marker)!r}, 'w', encoding='utf-8').write(os.environ['DEVROSTER_WHAT'])"
    )}]})
    assert ai_hooks.studio_event(workspace, {"what": "handoff"}) is None  # Not approved yet.
    approve(workspace)
    thread = ai_hooks.studio_event(workspace, {"what": "handoff"})
    thread.join(timeout=30)
    assert marker.read_text(encoding="utf-8") == "handoff"
    recent = ai_hooks.recent(ai_hooks._config(workspace))
    assert recent[0]["event"] == "studio_confirmed" and recent[0]["results"][0]["ok"]
