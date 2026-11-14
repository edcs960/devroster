"""Running a task again puts back what an earlier attempt changed outside its role's scope."""

import json
import subprocess
from pathlib import Path

import pytest
from test_budget_pause import runtime_with

from agent_dev.adapters.role_execution import (
    capture,
    load_baseline,
    refused_changes,
    restore_paths,
    save_baseline,
)
from agent_dev.domain.contracts import PhaseResult
from agent_dev.domain.role_policy import policy

PASS = '```json\n{"handoff_status":"pass","summary":"done"}\n```'
POLICY = {"estimated_budget": 100.0, "price_book": []}
PHASES = [{"id": "developer", "capabilities": ["code.write"]}, {"id": "reviewer", "capabilities": ["code.read"]}]
GRAPH = {
    "schema_version": 1,
    "id": "dev-review",
    "name": "Develop and review",
    "nodes": [
        {"id": "developer", "kind": "role", "role_id": "developer"},
        {"id": "reviewer", "kind": "role", "role_id": "reviewer"},
        {"id": "verify", "kind": "verification", "gate_ids": ["unit"]},
    ],
    "edges": [{"from": "developer", "to": "reviewer"}, {"from": "reviewer", "to": "verify"}],
}
BOTH = pytest.mark.parametrize("graph", [None, GRAPH], ids=["phases", "workflow-graph"])


class Runner:
    """``writes``: {phase: [{file: text} per call]}; a call past the list writes nothing."""

    def __init__(self, writes, forget_baseline=False):
        self.writes, self.forget_baseline, self.calls = writes, forget_baseline, []

    def execute(self, request):
        self.calls.append(request.phase_id)
        root = Path(json.loads(request.context)["repositories"]["source"]["worktree"])
        if self.forget_baseline:
            for baseline in (root.parent / ".attempts").glob("*.json"):
                baseline.unlink()
        planned = self.writes.get(request.phase_id, [])
        turn = self.calls.count(request.phase_id) - 1
        for name, text in (planned[turn] if turn < len(planned) else {}).items():
            (root / name).write_text(text, encoding="utf-8")
        return PhaseResult(PASS, provider="codex_cli")


def worktree(runtime):
    return Path(runtime.store.snapshot("T")["resolved"]["repositories"]["source"]["worktree"])


def events(runtime, prefix="RefusedChanges"):
    rows = runtime.store.db.rows("SELECT kind,payload_json FROM events WHERE aggregate_id='T' ORDER BY version")
    return [(r["kind"], json.loads(r["payload_json"])) for r in rows if r["kind"].startswith(prefix)]


def copies(runtime, name):
    return [p.read_text(encoding="utf-8") for p in (worktree(runtime).parent / ".attempts").glob(f"*-refused/source/{name}")]


@BOTH
def test_resume_puts_the_out_of_scope_file_back_and_keeps_allowed_work(tmp_path, graph):
    runtime = runtime_with(tmp_path, POLICY, graph, PHASES)
    runner = Runner({"developer": [{"source.txt": "developer work", "package.json": "{}"}]})
    blocked = runtime.execute("T", "run-1", runner=runner)
    assert blocked["state"] == "blocked" and blocked["error"] == "POLICY_VIOLATION"
    assert "밖의 파일을 바꿨습니다: package.json." in blocked["reason"]
    assert "이어서 실행하면 위 변경을 작업 폴더의 .attempts에 복사해 두고 되돌린 뒤" in blocked["reason"]
    assert "작업 폴더에서 그 변경을 되돌린 뒤" not in blocked["reason"]  # Nothing for the user to undo.
    folder = worktree(runtime)
    # Until the user runs it again the change stays where it can be inspected.
    assert (folder / "package.json").is_file()
    assert [kind for kind, _ in events(runtime)] == ["RefusedChangesHeld"]

    resumed = runtime.execute("T", "run-2", runner=runner)
    assert resumed["state"] != "blocked"
    assert not (folder / "package.json").exists()
    assert copies(runtime, "package.json") == ["{}"]
    assert (folder / "source.txt").read_text(encoding="utf-8") == "developer work"
    kind, payload = events(runtime)[-1]
    assert kind == "RefusedChangesDiscarded" and payload["paths"] == {"source": ["package.json"]}
    assert payload["node_id"] == "developer" and Path(payload["backup"]).is_dir()
    assert runner.calls.count("developer") == 2  # The step ran again from the restored state.


@BOTH
def test_read_only_role_edit_goes_back_to_the_earlier_roles_work(tmp_path, graph):
    runtime = runtime_with(tmp_path, POLICY, graph, PHASES)
    runner = Runner({"developer": [{"source.txt": "developer work"}], "reviewer": [{"source.txt": "reviewer edit"}]})
    blocked = runtime.execute("T", "run-1", runner=runner)
    assert blocked["state"] == "blocked"
    assert "파일을 직접 바꿀 수 없는 역할이 파일을 바꿨습니다: source.txt." in blocked["reason"]
    resumed = runtime.execute("T", "run-2", runner=runner)
    assert resumed["state"] != "blocked"
    # Not back to the commit: the developer's uncommitted work is where the reviewer started.
    assert (worktree(runtime) / "source.txt").read_text(encoding="utf-8") == "developer work"
    assert copies(runtime, "source.txt") == ["reviewer edit"]


@BOTH
def test_a_file_the_user_already_put_back_is_left_alone(tmp_path, graph):
    runtime = runtime_with(tmp_path, POLICY, graph, PHASES)
    runner = Runner({"developer": [{"package.json": "{}"}]})
    assert runtime.execute("T", "run-1", runner=runner)["state"] == "blocked"
    (worktree(runtime) / "package.json").unlink()
    assert runtime.execute("T", "run-2", runner=runner)["state"] != "blocked"
    assert [kind for kind, _ in events(runtime)] == ["RefusedChangesHeld", "RefusedChangesNone"]
    assert copies(runtime, "package.json") == []


@BOTH
def test_doing_it_again_stops_again_and_each_time_is_put_back_once(tmp_path, graph):
    runtime = runtime_with(tmp_path, POLICY, graph, PHASES)
    runner = Runner({"developer": [{"package.json": "first"}, {"package.json": "second"}]})
    assert runtime.execute("T", "run-1", runner=runner)["state"] == "blocked"
    again = runtime.execute("T", "run-2", runner=runner)
    assert again["state"] == "blocked" and "package.json" in again["reason"]
    assert (worktree(runtime) / "package.json").read_text(encoding="utf-8") == "second"
    assert runtime.execute("T", "run-3", runner=runner)["state"] != "blocked"
    assert not (worktree(runtime) / "package.json").exists()
    assert [kind for kind, _ in events(runtime)] == [
        "RefusedChangesHeld",
        "RefusedChangesDiscarded",
        "RefusedChangesHeld",
        "RefusedChangesDiscarded",
    ]
    assert sorted(copies(runtime, "package.json")) == ["first", "second"]


@BOTH
def test_without_a_baseline_the_task_stays_stopped_until_the_file_is_put_back(tmp_path, graph):
    runtime = runtime_with(tmp_path, POLICY, graph, PHASES)
    runner = Runner({"developer": [{"package.json": "{}"}]}, forget_baseline=True)
    blocked = runtime.execute("T", "run-1", runner=runner)
    # Nothing to restore from: the message asks the user, as before.
    assert "작업 폴더에서 그 변경을 되돌린 뒤 이어서 실행하세요." in blocked["reason"]
    assert ".attempts에 복사해 두고" not in blocked["reason"]
    still = runtime.execute("T", "run-2", runner=runner)
    assert still["state"] == "blocked" and still["error"] == "POLICY_VIOLATION"
    assert "작업 폴더에 남아 있습니다: package.json." in still["reason"]
    assert "직접 되돌린 뒤 이어서 실행하세요." in still["reason"]
    assert (worktree(runtime) / "package.json").is_file()
    assert runner.calls.count("developer") == 1  # The step did not start from the leftover.
    (worktree(runtime) / "package.json").unlink()
    assert runtime.execute("T", "run-3", runner=runner)["state"] != "blocked"


@BOTH
def test_when_putting_back_fails_the_task_stays_stopped_and_the_file_is_untouched(tmp_path, graph):
    runtime = runtime_with(tmp_path, POLICY, graph, PHASES)
    runner = Runner({"developer": [{"source.txt": "developer work"}], "reviewer": [{"source.txt": "reviewer edit"}]})
    assert runtime.execute("T", "run-1", runner=runner)["state"] == "blocked"
    for baseline in (worktree(runtime).parent / ".attempts").glob("*.json"):
        data = json.loads(baseline.read_text(encoding="utf-8"))
        data["trees"] = dict.fromkeys(data["trees"], "0" * 40)  # As if Git had pruned the tree.
        baseline.write_text(json.dumps(data), encoding="utf-8")
    still = runtime.execute("T", "run-2", runner=runner)
    assert still["state"] == "blocked" and "되돌리지 못했습니다: source.txt." in still["reason"]
    assert (worktree(runtime) / "source.txt").read_text(encoding="utf-8") == "reviewer edit"
    assert runner.calls.count("reviewer") == 1
    (worktree(runtime) / "source.txt").write_text("developer work", encoding="utf-8")
    assert runtime.execute("T", "run-3", runner=runner)["state"] != "blocked"


def git(root, *arguments):
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def test_deleted_and_changed_tracked_files_are_restored_and_allowed_changes_kept(tmp_path):
    root = tmp_path / "frontend"
    (root / "src").mkdir(parents=True)
    (root / "src/app.js").write_text("app", encoding="utf-8")
    (root / "package.json").write_text("{}", encoding="utf-8")
    (root / "LICENSE").write_text("license", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-qm", "base")
    # Uncommitted work of an earlier role is part of where this attempt starts.
    (root / "package.json").write_text('{"earlier": true}', encoding="utf-8")
    bindings = {"frontend": {"worktree": str(root), "allowed_paths": ["src"]}}
    before = capture(bindings)
    save_baseline(bindings, "attempt-1", before)

    (root / "src/app.js").write_text("allowed change", encoding="utf-8")
    (root / "package.json").write_text('{"refused": true}', encoding="utf-8")
    (root / "LICENSE").unlink()
    (root / "notes").mkdir()
    (root / "notes/new file.txt").write_text("new", encoding="utf-8")
    role_policy = policy("developer", project_paths=["src", "tests"])
    refused = refused_changes(bindings, before, role_policy, "frontend")
    assert refused == {"frontend": ["LICENSE", "notes/new file.txt", "package.json"]}

    backup = tmp_path / "copies"
    restore_paths(bindings, load_baseline(bindings, "attempt-1"), refused, backup)
    assert (root / "package.json").read_text(encoding="utf-8") == '{"earlier": true}'
    assert (root / "LICENSE").read_text(encoding="utf-8") == "license"
    assert not (root / "notes/new file.txt").exists()
    assert (root / "src/app.js").read_text(encoding="utf-8") == "allowed change"
    assert (backup / "frontend/package.json").read_text(encoding="utf-8") == '{"refused": true}'
    assert refused_changes(bindings, before, role_policy, "frontend") == {}


def test_every_change_in_another_module_is_refused(tmp_path):
    bindings = {}
    for name in ("frontend", "backend"):
        root = tmp_path / name
        (root / "src").mkdir(parents=True)
        (root / "src/keep.txt").write_text("base", encoding="utf-8")
        git(root, "init", "-q")
        bindings[name] = {"worktree": str(root), "allowed_paths": ["src"]}
    before = capture(bindings)
    for name in bindings:
        (tmp_path / name / "src/new.txt").write_text("new", encoding="utf-8")
    refused = refused_changes(bindings, before, policy("developer", project_paths=["src"]), "frontend")
    assert refused == {"backend": ["src/new.txt"]}  # Inside backend's paths, but not this step's module.


def test_a_case_only_rename_is_put_back_without_losing_the_file(tmp_path):
    """On case-insensitive file systems (Windows, macOS) "config.txt" is the same file as
    "Config.txt": the new name is removed before the old one is restored, never after."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "Config.txt").write_text("original", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-qm", "base")
    bindings = {"frontend": {"worktree": str(root), "allowed_paths": ["src"]}}
    save_baseline(bindings, "attempt-1", capture(bindings))
    (root / "Config.txt").unlink()
    (root / "config.txt").write_text("renamed", encoding="utf-8")

    restore_paths(bindings, load_baseline(bindings, "attempt-1"), {"frontend": ["Config.txt", "config.txt"]}, tmp_path / "copies")
    names = sorted(p.name for p in root.iterdir() if p.name.lower() == "config.txt")
    assert names == ["Config.txt"]
    assert (root / "Config.txt").read_text(encoding="utf-8") == "original"
