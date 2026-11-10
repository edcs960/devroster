"""Finished tasks' worktrees: removed only when their work is merged and nothing is uncommitted."""

import subprocess
from pathlib import Path

from test_budget_pause import Runner, runtime_with

from agent_dev.adapters import execution_view, live_log
from agent_dev.adapters.worktrees import cleanup, cleanup_plan

POLICY = {"estimated_budget": 100.0, "price_book": []}


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=T", "-c", "user.email=t@example.test", *args],
        check=True, capture_output=True, text=True, encoding="utf-8",
    ).stdout.strip()


def finished(tmp_path, state="completed"):
    runtime = runtime_with(tmp_path, POLICY)
    runtime.execute("T", "run-1", runner=Runner())
    with runtime.store.db.transaction() as sql:
        sql.execute("UPDATE tasks SET state=? WHERE id='T'", (state,))
    binding = runtime.store.snapshot("T")["resolved"]["repositories"]["source"]
    return runtime, binding


def only(plan):
    [row] = plan["worktrees"]
    return row


def test_uncommitted_work_is_kept(tmp_path):
    runtime, _ = finished(tmp_path)
    row = only(cleanup_plan(runtime.store))
    assert row["action"] == "keep" and "커밋하지 않은" in row["reason"]


def test_unmerged_commits_are_kept(tmp_path):
    runtime, binding = finished(tmp_path, "cancelled")
    git(binding["worktree"], "commit", "-qam", "task work")
    row = only(cleanup_plan(runtime.store))
    assert row["action"] == "keep" and "병합되지 않은" in row["reason"]


def test_merged_work_is_removed_only_with_apply(tmp_path):
    runtime, binding = finished(tmp_path)
    git(binding["worktree"], "commit", "-qam", "task work")
    git(binding["path"], "merge", "--ff-only", "-q", binding["branch"])
    attempt = runtime.store.db.rows("SELECT id FROM phase_attempts WHERE task_id='T' ORDER BY started_at DESC, id DESC")[0]["id"]
    bindings = runtime.store.snapshot("T")["resolved"]["repositories"]
    live_log.LiveLog(live_log.log_path(bindings, attempt), "codex_cli").write("stderr", "what the tool did\n")
    preview = cleanup(runtime.store, runtime.safety)
    assert preview["applied"] is False and preview["removable"] == 1 and preview["bytes"] > 0
    assert Path(binding["worktree"]).is_dir()
    done = cleanup(runtime.store, runtime.safety, apply=True)
    assert only(done)["result"] == "removed"
    assert not Path(binding["worktree"]).exists()
    assert not Path(binding["worktree"]).parent.exists()  # The crash baselines (.attempts) go too.
    # The live log outlives the folder: moved next to the worktrees and still shown in the console.
    assert (Path(binding["worktree"]).parent.parent / ".logs" / "T" / f"{attempt}.log").is_file()
    assert "what the tool did" in execution_view.live_log(runtime.root, "T")["text"]
    assert binding["branch"] not in git(binding["path"], "branch", "--list")
    assert (Path(binding["path"]) / "source.txt").read_text(encoding="utf-8").startswith("step")


def test_squash_merged_on_github_counts_as_merged(tmp_path):
    runtime, binding = finished(tmp_path)
    git(binding["worktree"], "commit", "-qam", "task work")  # GitHub squashed it: new commit ID.
    with runtime.store.db.transaction() as sql:
        sql.execute(
            "INSERT INTO publish_records VALUES('P1','T','source',?,?,'7','passed','merged','k1')",
            (binding["branch"], "abc"),
        )
    assert only(cleanup_plan(runtime.store))["action"] == "remove"


def test_running_tasks_are_never_listed(tmp_path):
    runtime = runtime_with(tmp_path, POLICY)
    runtime.execute("T", "run-1", runner=Runner())
    assert cleanup_plan(runtime.store)["worktrees"] == []


def merged(tmp_path):
    runtime, binding = finished(tmp_path)
    git(binding["worktree"], "commit", "-qam", "task work")
    git(binding["path"], "merge", "--ff-only", "-q", binding["branch"])
    return runtime, binding


def test_apply_removes_only_what_the_preview_showed(tmp_path):
    runtime, binding = merged(tmp_path)
    done = cleanup(runtime.store, runtime.safety, apply=True, only=[])
    assert only(done)["result"] == "kept: not in the preview"
    assert Path(binding["worktree"]).is_dir()
    done = cleanup(runtime.store, runtime.safety, apply=True, only=[binding["worktree"]])
    assert only(done)["result"] == "removed" and not Path(binding["worktree"]).exists()


def test_settings_screen_previews_then_removes_the_listed_folders(tmp_path):
    import pytest

    from agent_dev.ui.setup_server import worktree_cleanup

    runtime, binding = merged(tmp_path)
    root = runtime.root
    preview = worktree_cleanup(root, False, None)
    assert preview["applied"] is False and preview["removable"] == 1
    assert Path(binding["worktree"]).is_dir()
    with pytest.raises(ValueError):
        worktree_cleanup(root, True, None)  # Apply must name the folders the user saw.
    done = worktree_cleanup(root, True, [only(preview)["worktree"]])
    assert done["applied"] is True and only(done)["result"] == "removed"
    assert not Path(binding["worktree"]).exists()


def test_settings_screen_without_a_database_lists_nothing(tmp_path):
    from agent_dev.ui.setup_server import worktree_cleanup

    (tmp_path / "config").mkdir()
    assert worktree_cleanup(tmp_path, False, None)["worktrees"] == []
    assert not any(tmp_path.rglob("*.db"))
