import subprocess
from pathlib import Path

from agent_dev.scm.git_publish import RepositoryTarget, publish_repository


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)


def test_publish_repository_defaults_to_task_branch_not_main(tmp_path):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _init_repo(repo_path)
    (repo_path / "feature.txt").write_text("change\n", encoding="utf-8")

    target = RepositoryTarget(
        name="frontend", path=repo_path, remote="origin", default_branch="main", branch_prefix="task"
    )

    result = publish_repository(
        target=target,
        task_id="TASK-999",
        commit=True,
        push=False,
        branch="task/TASK-999",
        message="test commit",
        include_paths=["feature.txt"],
        dry_run=False,
    )

    assert result.branch == "task/TASK-999"
    assert result.committed is True

    current_branch = subprocess.run(
        ["git", "-C", str(repo_path), "branch", "--show-current"],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=True,
    ).stdout.strip()
    assert current_branch == "task/TASK-999"

    main_log = subprocess.run(
        ["git", "-C", str(repo_path), "log", "main", "--oneline"],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=True,
    ).stdout
    assert "test commit" not in main_log


def test_publish_dry_run_leaves_branch_index_and_head_unchanged(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "feature.txt").write_text("change\n", encoding="utf-8")
    before = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"])
    target = RepositoryTarget("frontend", repo, "origin", "main", "task")

    result = publish_repository(target=target, task_id="TASK-900", commit=True,
                                push=True, branch="task/TASK-900", message=None, dry_run=True,
                                include_paths=["feature.txt"])

    assert result.branch == "task/TASK-900"
    assert result.committed is False and result.pushed is False
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]) == before
    assert subprocess.check_output(["git", "-C", str(repo), "branch", "--show-current"]).strip() == b"main"
    assert subprocess.check_output(["git", "-C", str(repo), "diff", "--cached", "--name-only"]) == b""


def test_publish_clean_task_branch_does_not_create_empty_commit(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    target = RepositoryTarget("frontend", repo, "origin", "main", "task")
    before = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"])

    result = publish_repository(target=target, task_id="TASK-900", commit=True,
                                push=False, branch="task/TASK-900", message=None, dry_run=False)

    assert result.skipped_reason == "no changes to commit"
    assert result.committed is False
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"]) == before


def test_publish_rejects_dirty_files_outside_explicit_task_paths(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "feature.txt").write_text("task change\n", encoding="utf-8")
    (repo / "personal.txt").write_text("unrelated\n", encoding="utf-8")
    target = RepositoryTarget("frontend", repo, "origin", "main", "task")

    import pytest

    from agent_dev.domain.errors import DomainError, ErrorCode

    with pytest.raises(DomainError) as exc:
        publish_repository(target=target, task_id="TASK-900", commit=True,
                           push=False, branch="task/TASK-900", message=None, dry_run=False,
                           include_paths=["feature.txt"])
    assert exc.value.code is ErrorCode.POLICY_VIOLATION
    assert subprocess.check_output(["git", "-C", str(repo), "diff", "--cached", "--name-only"]) == b""
    assert (repo / "personal.txt").read_text(encoding="utf-8") == "unrelated\n"
