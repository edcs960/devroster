"""Task-specific Git worktrees; the user's checkout is never a write target."""

import subprocess
from pathlib import Path

from agent_dev.domain.errors import DomainError, ErrorCode


def git(root, *arguments):
    process = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if process.returncode:
        raise DomainError(
            ErrorCode.POLICY_VIOLATION, "Git operation failed; check repository configuration."
        )
    return process.stdout.strip()


class WorktreeManager:
    def __init__(self, root, safety):
        self.root, self.safety = Path(root).resolve(), safety

    def prepare(self, task_id, repositories):
        if not task_id or Path(task_id).name != task_id or task_id in {".", ".."}:
            raise DomainError(ErrorCode.INVALID_INPUT, "Invalid task identifier.")
        result = {}
        for name, binding in repositories.items():
            if Path(name).name != name or name in {".", ".."}:
                raise DomainError(ErrorCode.INVALID_INPUT, "Invalid repository role.")
            source = Path(binding["path"]).resolve()
            if Path(git(source, "rev-parse", "--show-toplevel")).resolve() != source:
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION,
                    "Repository path is a Git subdirectory. Save devroster setup again to normalize module paths before task approval.",
                )
            common = Path(
                git(source, "rev-parse", "--path-format=absolute", "--git-common-dir")
            ).resolve()
            destination = self.root / task_id / name
            branch = f"task/{task_id}/{name}"
            with self.safety.lease(f"repository:{common}"):
                if destination.exists():
                    if (
                        Path(
                            git(
                                destination,
                                "rev-parse",
                                "--path-format=absolute",
                                "--git-common-dir",
                            )
                        ).resolve()
                        != common
                        or git(destination, "branch", "--show-current") != branch
                    ):
                        raise DomainError(
                            ErrorCode.POLICY_VIOLATION,
                            "Worktree belongs to another repository or branch.",
                        )
                else:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    git(
                        source,
                        "worktree",
                        "add",
                        "-b",
                        branch,
                        str(destination),
                        binding.get("base_ref", "HEAD"),
                    )
                link_dependencies(source, destination, common)
            result[name] = dict(
                binding,
                path=str(source),
                worktree=str(destination),
                branch=branch,
                common_dir=str(common),
                base_commit=git(destination, "rev-parse", "HEAD"),
            )
        return result

    def release(self, binding):
        """Remove a task worktree and branch that hold no work; anything with work is kept."""
        worktree = Path(binding["worktree"])
        if not worktree.is_dir():
            return "missing"
        try:
            if not self.validate(binding):
                return "kept: worktree belongs to another branch"
            if git(worktree, "status", "--porcelain"):
                return "kept: uncommitted changes"
            if git(worktree, "rev-list", "--count", binding["base_commit"] + "..HEAD") != "0":
                return "kept: commits not in the base"
            with self.safety.lease(f"repository:{binding['common_dir']}"):
                git(binding["path"], "worktree", "remove", str(worktree))
                # The branch has no commits of its own, so deleting it loses nothing.
                git(binding["path"], "branch", "-D", binding["branch"])
        except DomainError:
            return "kept: git refused to remove it"
        return "removed"

    @staticmethod
    def validate(binding):
        root = Path(binding["worktree"]).resolve()
        return (
            root.is_dir()
            and git(root, "branch", "--show-current") == binding["branch"]
            and git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
            == binding["common_dir"]
        )


# Installed dependencies are ignored by git, so a new worktree has none and its build and
# test commands cannot run (`sh: vitest: command not found`).
DEPENDENCY_DIRS = ("node_modules",)


def link_dependencies(source, destination, common):
    """Link the checkout's installed dependencies into a task worktree, hidden from git."""
    for name in DEPENDENCY_DIRS:
        installed, link = Path(source) / name, Path(destination) / name
        if not installed.is_dir() or link.exists() or link.is_symlink():
            continue
        link.symlink_to(installed, target_is_directory=True)
        ignored = subprocess.run(
            ["git", "-C", str(destination), "check-ignore", "-q", name],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if ignored.returncode:
            # `node_modules/` matches directories only, never this link.
            exclude = Path(common) / "info" / "exclude"
            exclude.parent.mkdir(exist_ok=True)
            with exclude.open("a", encoding="utf-8") as handle:
                handle.write(f"\n/{name}\n")


def commit_fingerprint(bindings):
    """Read bounded local Git revisions independently of the executable-byte policy."""
    return {
        name: subprocess.run(
            ["git", "-C", str(binding["worktree"]), "rev-parse", "--verify", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
        for name, binding in bindings.items()
    }


def changed_repositories(bindings):
    """Repository roles whose task worktree differs from its base commit.

    Committed, staged, unstaged and untracked (not ignored) changes all count. A binding
    without a known base commit, or a Git error, counts as changed so it is never skipped.
    """
    changed = set()
    for name, binding in bindings.items():
        root = Path(binding["worktree"]).resolve()
        base = binding.get("base_commit")
        if not base:
            changed.add(name)
            continue
        diff = subprocess.run(
            ["git", "-C", str(root), "diff", "--quiet", base, "--"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        untracked = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--others", "--exclude-standard"],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if diff.returncode != 0 or untracked.returncode != 0 or untracked.stdout:
            changed.add(name)
    return changed


def skipped_gate_evidence(name, binding):
    return (
        f"Skipped: repository '{name}' has no changes since base commit "
        f"{str(binding.get('base_commit', ''))[:12]} (verification scope: changed)."
    )


def evidence_digest(bindings):
    """Fingerprint executable source including staged, unstaged and untracked bytes."""
    from agent_dev.domain.content import content_digest, digest

    result = {}
    for name, binding in bindings.items():
        root = Path(binding["worktree"]).resolve()
        names = (
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                ],
                capture_output=True,
                check=True,
                timeout=30,
            )
            .stdout.decode(errors="surrogateescape")
            .split("\0")
        )
        files = {}
        for filename in sorted(set(names) - {""}):
            path = root / filename
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION, "Source evidence contains an escaping symlink."
                )
            files[filename] = (
                {
                    "digest": content_digest(path.read_bytes()),
                    "executable": bool(path.stat().st_mode & 0o111),
                }
                if path.is_file()
                else "<deleted>"
            )
        result[name] = {"files": files}
    return digest(result)
