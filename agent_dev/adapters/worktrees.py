"""Task-specific Git worktrees; the user's checkout is never a write target."""

import contextlib
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

from agent_dev.domain.errors import DomainError, ErrorCode


def git(root, *arguments):
    process = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True, encoding="utf-8", errors="replace",
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
                unlink_dependencies(worktree)
                remove_worktree(binding["path"], worktree)
                # The branch has no commits of its own, so deleting it loses nothing.
                git(binding["path"], "branch", "-D", binding["branch"])
        except DomainError:
            return "kept: git refused to remove it"
        return "removed"

    @staticmethod
    def validate(binding):
        root = Path(binding["worktree"]).resolve()
        # Compare paths, not strings: Git prints C:/x/.git on Windows while the binding
        # stores the resolved C:\x\.git, and drive letters may differ in case.
        return (
            root.is_dir()
            and git(root, "branch", "--show-current") == binding["branch"]
            and Path(
                git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
            ).resolve()
            == Path(binding["common_dir"]).resolve()
        )


# Installed dependencies are ignored by git, so a new worktree has none and its build and
# test commands cannot run (`sh: vitest: command not found`).
DEPENDENCY_DIRS = ("node_modules",)


def _junction(target, link):
    if os.name != "nt":
        return False
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, OSError):
        return False
    return True


def _is_link(path):
    """A symlink or a Windows junction: removing it must never touch what it points to."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & reparse)


def unlink_dependencies(worktree):
    """Drop the dependency links first, so removing the worktree cannot reach the checkout's copy."""
    for name in DEPENDENCY_DIRS:
        link = Path(worktree) / name
        if not _is_link(link):
            continue
        try:
            os.unlink(link)
        except OSError:
            os.rmdir(link)  # A Windows junction is removed as an (empty-looking) directory.


def remove_worktree(repository, worktree, attempts=None, delay=0.5):
    """`git worktree remove`, retried on Windows while an exiting process still holds a file."""
    attempts = attempts or (4 if os.name == "nt" else 1)
    for attempt in range(attempts):
        try:
            git(repository, "worktree", "remove", str(worktree))
            return
        except DomainError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay * 2**attempt)


def link_dependencies(source, destination, common):
    """Link the checkout's installed dependencies into a task worktree, hidden from git."""
    for name in DEPENDENCY_DIRS:
        installed, link = Path(source) / name, Path(destination) / name
        if not installed.is_dir() or link.exists() or link.is_symlink():
            continue
        try:
            link.symlink_to(installed, target_is_directory=True)
        except OSError:
            # Windows without symlink rights (WinError 1314): a junction needs none.
            if not _junction(installed, link):
                continue
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
            text=True, encoding="utf-8", errors="replace",
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


# Cleanup of finished tasks ---------------------------------------------------------------
# release() runs only when a task is cancelled and keeps anything with commits outside the
# base, so a task merged on GitHub (squash: new commit IDs) kept its worktree forever.

FINISHED = ("completed", "cancelled")


def _folder_size(root):
    total = 0
    for folder, directories, files in os.walk(root, followlinks=False):
        directories[:] = [d for d in directories if d != ".git" and not os.path.islink(os.path.join(folder, d))]
        for name in files:
            path = os.path.join(folder, name)
            if not os.path.islink(path):
                with contextlib.suppress(OSError):
                    total += os.lstat(path).st_size
    return total


def _contained(binding):
    """The task branch's work is in the source checkout's current branch or its base ref."""
    worktree = Path(binding["worktree"])
    head = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"], capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=30, check=False,
    ).stdout.strip()
    if not head:
        return False
    for ref in dict.fromkeys(["HEAD", binding.get("base_ref") or "HEAD"]):
        check = subprocess.run(
            ["git", "-C", str(binding["path"]), "merge-base", "--is-ancestor", head, ref],
            capture_output=True, timeout=30, check=False,
        )
        if check.returncode == 0:
            return True
    return False


def cleanup_plan(store, task_id=""):
    """Worktrees of finished tasks: which can go (work merged, nothing uncommitted) and why not."""
    query = "SELECT id,state FROM tasks WHERE state IN (?,?)" + (" AND id=?" if task_id else "") + " ORDER BY id"
    rows = []
    for task in store.db.rows(query, (*FINISHED, task_id) if task_id else FINISHED):
        try:
            bindings = store.snapshot(task["id"])["resolved"].get("repositories", {})
        except DomainError:
            continue  # Never approved: no worktree was made.
        merged = {
            r["repository"]
            for r in store.db.rows(
                "SELECT repository FROM publish_records WHERE task_id=? AND merge_state='merged'", (task["id"],)
            )
        }
        for name, binding in bindings.items():
            worktree = Path(binding["worktree"])
            row = {"task_id": task["id"], "task_state": task["state"], "repository": name,
                   "worktree": str(worktree), "branch": binding.get("branch", "")}
            if not worktree.is_dir():
                continue
            try:
                if not WorktreeManager.validate(binding):
                    rows.append(dict(row, action="keep", reason="다른 저장소나 브랜치의 폴더입니다."))
                    continue
                dirty = subprocess.run(
                    ["git", "-C", str(worktree), "status", "--porcelain"], capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=60, check=True,
                ).stdout.strip()
            except (DomainError, subprocess.SubprocessError, OSError):
                rows.append(dict(row, action="keep", reason="Git으로 상태를 확인하지 못했습니다."))
                continue
            if dirty:
                rows.append(dict(row, action="keep", reason="커밋하지 않은 변경이 있습니다."))
            elif name in merged or _contained(binding):
                rows.append(dict(row, action="remove", reason="작업이 기준 브랜치에 병합되었습니다.",
                                 bytes=_folder_size(worktree)))
            else:
                rows.append(dict(row, action="keep", reason="병합되지 않은 커밋이 있습니다. 병합하거나 직접 정리하세요."))
    removable = [r for r in rows if r["action"] == "remove"]
    return {"worktrees": rows, "removable": len(removable), "bytes": sum(r.get("bytes", 0) for r in removable)}


def cleanup(store, safety, task_id="", apply=False, only=None):
    """Preview by default; with apply, remove the worktrees and task branches the plan allows.

    ``only`` limits removal to the worktree folders the user saw in a preview, so a task that
    finished between preview and apply is not removed without being shown first.
    """
    plan = cleanup_plan(store, task_id)
    if not apply:
        return dict(plan, applied=False)
    chosen = None if only is None else {str(path) for path in only}
    for row in plan["worktrees"]:
        if row["action"] != "remove":
            continue
        if chosen is not None and row["worktree"] not in chosen:
            row["result"] = "kept: not in the preview"
            continue
        binding = store.snapshot(row["task_id"])["resolved"]["repositories"][row["repository"]]
        try:
            with safety.lease(f"repository:{binding['common_dir']}"):
                unlink_dependencies(binding["worktree"])
                remove_worktree(binding["path"], binding["worktree"])
                with contextlib.suppress(DomainError):
                    git(binding["path"], "branch", "-D", binding["branch"])  # Merged; may be gone.
            row["result"] = "removed"
        except DomainError:
            row["result"] = "kept: git refused to remove it"
            continue
        task_folder = Path(binding["worktree"]).parent
        if not any(p.name != ".attempts" for p in task_folder.iterdir()):
            shutil.rmtree(task_folder / ".attempts", ignore_errors=True)  # Crash baselines of this task.
            with contextlib.suppress(OSError):
                task_folder.rmdir()
    return dict(plan, applied=True)
