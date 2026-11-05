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
