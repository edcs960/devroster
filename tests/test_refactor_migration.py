import json
import subprocess
import sys
import time

import pytest

from agent_dev.adapters.command_line import rollback_drill
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.domain.errors import DomainError
from agent_dev.migration.legacy import collect_sources, compare, import_sources
from agent_dev.migration.operations import Operations


def legacy(root):
    (root / "data").mkdir()
    (root / "data/backlog.json").write_text(
        json.dumps(
            {"tasks": [{"id": "T1", "title": "Task", "status": "review_ready", "depends_on": []}]}
        ), encoding="utf-8"
    )
    run = root / "output" / "Topic" / "T1"
    run.mkdir(parents=True)
    (run / "pipeline-state.json").write_text(
        json.dumps(
            {
                "task_id": "T1",
                "phases": {"developer": {"status": "pass"}},
                "findings": [{"id": "finding", "status": "open"}],
            }
        ), encoding="utf-8"
    )
    (run / "events.jsonl").write_text('{"task_id":"T1","kind":"started"}\n', encoding="utf-8")
    (run / "latest-checkpoint.md").write_text("checkpoint", encoding="utf-8")
    return root


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "agent_dev.fa", *map(str, args)],
        text=True, encoding="utf-8", errors="replace",
        capture_output=True,
        check=False,
    )


def test_cli_dry_run_no_writes_and_round_trip(tmp_path):
    root = legacy(tmp_path)
    before = {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    dry = cli("refactor", "migrate", "--root", root, "--dry-run", "--json")
    assert dry.returncode == 0, dry.stderr + dry.stdout
    assert not (root / ".agent-dev").exists()
    assert before == {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    actual = cli("refactor", "migrate", "--root", root, "--json")
    assert actual.returncode == 0, actual.stderr + actual.stdout
    result = json.loads(actual.stdout)["result"]
    assert result["comparison"]["zero_diff"]
    assert cli("refactor", "compare", "--root", root).returncode == 0
    runtime = WorkflowRuntime(root)
    assert {r["kind"] for r in runtime.store.db.rows("SELECT * FROM legacy_projections")} >= {
        "phase",
        "finding",
        "event",
        "artifact",
    }
    assert rollback_drill(runtime)["passed"]
    with runtime.store.db.transaction() as sql:
        sql.execute("UPDATE legacy_projections SET content_json='{}' WHERE kind='phase'")
    assert not compare(runtime.store, collect_sources(root, root / "data"))["zero_diff"]


def test_invalid_dag_atomic_and_backup_digest(tmp_path):
    root = legacy(tmp_path)
    runtime = WorkflowRuntime(root)
    sources = collect_sources(root, root / "data")
    sources["data/backlog.json"]["tasks"][0]["depends_on"] = ["T1"]
    with pytest.raises(DomainError):
        import_sources(runtime.store, sources, dry_run=False)
    assert runtime.store.db.rows("SELECT * FROM tasks") == []
    backup = root / "backup.db"
    Operations(runtime.store).export(backup)
    backup.write_bytes(backup.read_bytes() + b"changed")
    with pytest.raises(DomainError):
        Operations.restore(backup, root / "restored.db")
    assert not (root / "restored.db").exists()


def test_release_gate_does_not_skip_required_observation(tmp_path):
    root = legacy(tmp_path)
    assert cli("refactor", "migrate", "--root", root).returncode == 0
    result = cli("refactor", "configure", "legacy-readonly", "--root", root, "--json")
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == "APPROVAL_REQUIRED"
    assert not (root / "config/runtime.json").exists()


def test_ten_thousand_event_projection(tmp_path):
    runtime = WorkflowRuntime(tmp_path)
    runtime.store.register_project("P", "Benchmark", {})
    with runtime.store.db.transaction() as sql:
        sql.executemany(
            "INSERT INTO tasks(id,project_id,title,priority,state,definition_json) VALUES(?,'P',?,'medium','completed','{}')",
            [(f"T{i}", f"Task {i}") for i in range(100)],
        )
        sql.executemany(
            "INSERT INTO events VALUES(?,?,?,?,?,?)",
            [
                (str(i), "BENCH", i, "Measured", json.dumps({"task_id": f"T{i % 100}"}), i)
                for i in range(1, 10001)
            ],
        )
    start = time.monotonic()
    assert len(Operations(runtime.store).projection("BENCH")) == 10000
    assert time.monotonic() - start < 1.0
    assert len(Operations(runtime.store).projection("BENCH", 9990)) == 10


def test_cli_rejects_dry_run_where_it_would_run_for_real(tmp_path):
    root = legacy(tmp_path)
    assert cli("refactor", "migrate", "--root", root, "--json").returncode == 0
    task = WorkflowRuntime(root).store.db.rows("SELECT id, state FROM tasks ORDER BY id")[0]
    # `task cancel` has no preview: --dry-run used to cancel the task anyway.
    dry = cli("task", "cancel", task["id"], "--root", root, "--dry-run", "--json")
    envelope = json.loads(dry.stdout)
    assert envelope["status"] == "rejected"
    assert envelope["error"]["code"] == "INVALID_INPUT"
    assert "nothing was changed" in envelope["error"]["message"]
    after = WorkflowRuntime(root).store.db.rows("SELECT state FROM tasks WHERE id=?", (task["id"],))
    assert after[0]["state"] == task["state"]
    both = cli("refactor", "retention", "--root", root, "--dry-run", "--apply", "--json")
    assert json.loads(both.stdout)["status"] == "rejected"
    preview = cli("refactor", "retention", "--root", root, "--dry-run", "--json")
    assert json.loads(preview.stdout)["result"]["dry_run"]
