"""Per-attempt worktree deltas, shared by host and native CLI policy checks."""

import subprocess
from pathlib import Path

from agent_dev.domain.content import content_digest
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.role_policy import validate_changes


def capture(bindings):
    result = {}
    for name, binding in bindings.items():
        root = Path(binding["worktree"]).resolve()
        files = (
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
        state = {}
        for relative in set(files) - {""}:
            path = root / relative
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION, "Worktree contains an escaping symlink."
                )
            state[relative] = (
                (content_digest(path.read_bytes()), bool(path.stat().st_mode & 0o111))
                if path.is_file()
                else None
            )
        result[name] = state
    return result


def enforce(bindings, before, role_policy, module=None):
    after = capture(bindings)
    for name, binding in bindings.items():
        previous, current = before.get(name, {}), after.get(name, {})
        delta = [p for p in previous.keys() | current.keys() if previous.get(p) != current.get(p)]
        if not delta:
            continue
        if module and name != module:
            raise DomainError(
                ErrorCode.POLICY_VIOLATION, "Role modified a different registered module."
            )
        root = Path(binding["worktree"])
        extra = [binding.get("allowed_paths", [])]
        narrowed = dict(role_policy, scopes=role_policy.get("scopes", []) + extra)
        if "any_of" in role_policy:
            narrowed["any_of"] = [
                dict(member, scopes=member.get("scopes", []) + extra)
                for member in role_policy["any_of"]
            ]
        validate_changes(root, delta, narrowed)
    return after


# Interrupted attempts ------------------------------------------------------------------
# A role that crashes never reaches enforce(), and the next run would capture its leftovers
# as the new baseline. Each attempt therefore stores where it started: the file digests
# (to find what changed) and a Git tree of the whole worktree (to put changed paths back
# without touching the index or the work of earlier roles, which stays uncommitted).


def _attempts_dir(bindings):
    first = next(iter(bindings.values()))
    return Path(first["worktree"]).resolve().parent / ".attempts"


def _git(root, *arguments, env=None, check=True):
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        capture_output=True,
        text=True, encoding="utf-8", errors="surrogateescape",
        timeout=120,
        check=check,
        env=env,
    )


def worktree_tree(binding):
    """A tree object of the worktree as it is now (tracked and new files), index untouched."""
    import os
    import tempfile

    root = Path(binding["worktree"]).resolve()
    with tempfile.TemporaryDirectory(prefix="devroster-index-") as folder:
        env = dict(os.environ, GIT_INDEX_FILE=str(Path(folder) / "index"))
        _git(root, "read-tree", "HEAD", env=env)
        _git(root, "add", "-A", env=env)
        return _git(root, "write-tree", env=env).stdout.strip()


def save_baseline(bindings, attempt_id, before):
    import json

    if not bindings:
        return
    folder = _attempts_dir(bindings)
    folder.mkdir(parents=True, exist_ok=True)
    trees = {name: worktree_tree(binding) for name, binding in bindings.items()}
    data = {
        "capture": {name: {p: list(v) if v else None for p, v in files.items()} for name, files in before.items()},
        "trees": trees,
    }
    (folder / f"{attempt_id}.json").write_text(json.dumps(data), encoding="utf-8")


def load_baseline(bindings, attempt_id):
    import json

    path = _attempts_dir(bindings) / f"{attempt_id}.json" if bindings else None
    if path is None or not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    data["capture"] = {
        name: {p: tuple(v) if v else None for p, v in files.items()} for name, files in data["capture"].items()
    }
    return data


def changed_since(bindings, before):
    """{repository: [paths]} that differ from ``before``."""
    after = capture(bindings)
    result = {}
    for name in bindings:
        previous, current = before.get(name, {}), after.get(name, {})
        delta = sorted(p for p in previous.keys() | current.keys() if previous.get(p) != current.get(p))
        if delta:
            result[name] = delta
    return result


def restore_paths(bindings, baseline, changed, backup):
    """Put ``changed`` paths back to the attempt's start; current files are copied to ``backup``."""
    import shutil

    for name, paths in changed.items():
        root = Path(bindings[name]["worktree"]).resolve()
        tree = baseline["trees"][name]
        for relative in paths:
            current = root / relative
            if current.is_file() and not current.is_symlink():
                target = Path(backup) / name / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(current, target)
            literal = ":(literal)" + relative
            if _git(root, "cat-file", "-e", f"{tree}:{relative}", check=False).returncode == 0:
                _git(root, "restore", "--source", tree, "--worktree", "--", literal)
            elif current.is_file() or current.is_symlink():
                current.unlink()
