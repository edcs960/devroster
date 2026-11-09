"""Task worktrees on Windows: no-privilege dependency links and removal while files are held."""

import subprocess
from pathlib import Path

import pytest

from agent_dev.adapters import worktrees
from agent_dev.domain.errors import DomainError, ErrorCode


def repository(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def test_dependency_link_failure_without_rights_skips_instead_of_failing(tmp_path, monkeypatch):
    root = repository(tmp_path)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    worktree = tmp_path / "task"
    worktree.mkdir()

    def denied(self, *args, **kwargs):
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", denied)
    monkeypatch.setattr(worktrees, "_junction", lambda target, link: False)
    worktrees.link_dependencies(root, worktree, root / ".git")
    assert not (worktree / "node_modules").exists()


def test_junction_fallback_is_used_when_symlinks_are_denied(tmp_path, monkeypatch):
    root = repository(tmp_path)
    (root / "node_modules").mkdir()
    worktree = tmp_path / "task"
    worktree.mkdir()
    made = []

    def denied(self, *args, **kwargs):
        raise OSError(1314, "A required privilege is not held by the client")

    monkeypatch.setattr(Path, "symlink_to", denied)
    monkeypatch.setattr(worktrees, "_junction", lambda target, link: made.append((target, link)) or True)
    worktrees.link_dependencies(root, worktree, root / ".git")
    assert made == [(root / "node_modules", worktree / "node_modules")]


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="symlinks unavailable")
def test_removing_dependency_links_keeps_the_checkout_copy(tmp_path):
    installed = tmp_path / "repo" / "node_modules"
    (installed / "pkg").mkdir(parents=True)
    (installed / "pkg" / "index.js").write_text("x", encoding="utf-8")
    worktree = tmp_path / "task"
    worktree.mkdir()
    try:
        (worktree / "node_modules").symlink_to(installed, target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot create symlinks")
    worktrees.unlink_dependencies(worktree)
    assert not (worktree / "node_modules").exists()
    assert (installed / "pkg" / "index.js").read_text(encoding="utf-8") == "x"


def test_a_real_folder_named_node_modules_is_not_unlinked(tmp_path):
    (tmp_path / "node_modules").mkdir()
    worktrees.unlink_dependencies(tmp_path)
    assert (tmp_path / "node_modules").is_dir()


def test_worktree_removal_is_retried_while_a_file_is_held(monkeypatch):
    calls = []

    def flaky(root, *arguments):
        calls.append(arguments)
        if len(calls) < 3:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "in use")
        return ""

    monkeypatch.setattr(worktrees, "git", flaky)
    monkeypatch.setattr(worktrees.time, "sleep", lambda seconds: None)
    worktrees.remove_worktree("repo", "task", attempts=4)
    assert len(calls) == 3


def test_worktree_removal_gives_up_after_the_last_attempt(monkeypatch):
    def refused(root, *arguments):
        raise DomainError(ErrorCode.POLICY_VIOLATION, "in use")

    monkeypatch.setattr(worktrees, "git", refused)
    monkeypatch.setattr(worktrees.time, "sleep", lambda seconds: None)
    with pytest.raises(DomainError):
        worktrees.remove_worktree("repo", "task", attempts=2)


def test_validate_compares_git_paths_not_their_spelling(tmp_path, monkeypatch):
    common = tmp_path / "repo" / ".git"
    common.mkdir(parents=True)
    worktree = tmp_path / "task"
    worktree.mkdir()
    binding = {"worktree": str(worktree), "branch": "task/T/source", "common_dir": str(common.resolve())}

    def git_output(root, *arguments):
        if arguments[:2] == ("branch", "--show-current"):
            return "task/T/source"
        # Git spells the same folder differently (Windows: C:/x/.git, not C:\x\.git).
        return str(common) + "/./"

    monkeypatch.setattr(worktrees, "git", git_output)
    assert worktrees.WorktreeManager.validate(binding)
    binding["common_dir"] = str(tmp_path / "other" / ".git")
    assert not worktrees.WorktreeManager.validate(binding)
