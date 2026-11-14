from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent_dev.core.paths import PROJECT_ROOT
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.role_policy import listed


def _git_run(argv, **options):
    """git never prompts for credentials and never waits forever (exec_policy "git")."""
    from agent_dev.adapters.process_launcher import run

    try:
        return run("git", argv, **options)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{' '.join(argv[:2])} did not finish within {exc.timeout:.0f} seconds.") from None


@dataclass(frozen=True)
class RepositoryTarget:
    name: str
    path: Path
    remote: str
    default_branch: str
    branch_prefix: str
    allowed_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class PublishResult:
    target: RepositoryTarget
    branch: str
    changed_files: list[str]
    diff_stat: list[str]
    commands: list[str]
    committed: bool
    pushed: bool
    skipped_reason: str = ""


def load_repository_targets(runtime: dict) -> dict[str, RepositoryTarget]:
    raw_repositories = runtime.get("repositories", {})
    targets: dict[str, RepositoryTarget] = {}
    for name, config in raw_repositories.items():
        raw_path = Path(config.get("path", ""))
        path = raw_path if raw_path.is_absolute() else PROJECT_ROOT / raw_path
        targets[name] = RepositoryTarget(
            name=name,
            path=path,
            remote=config.get("remote", "origin"),
            default_branch=config.get("default_branch", "main"),
            branch_prefix=config.get("branch_prefix", "task"),
            allowed_paths=tuple(config.get("allowed_paths", [])),
        )
    return targets


def publish_repository(
    *,
    target: RepositoryTarget,
    task_id: str,
    commit: bool,
    push: bool,
    branch: str | None,
    message: str | None,
    dry_run: bool,
    include_paths: list[str] | None = None,
) -> PublishResult:
    ensure_git_repository(target.path)
    selected_branch = branch or f"{target.branch_prefix}/{task_id}"
    commands: list[str] = []
    if commit or push:
        if selected_branch == target.default_branch:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "Default branch publish is forbidden.")
        _git_run(["git", "check-ref-format", "--branch", selected_branch],
                       cwd=target.path, check=True, capture_output=True)
    allowed = list(include_paths or target.allowed_paths)
    dirty_paths = git_dirty_paths(target.path)
    if commit and dirty_paths:
        validate_publish_paths(target.path, dirty_paths, allowed)

    if commit or push or branch:
        run_git(
            target.path,
            ["switch", selected_branch],
            commands,
            dry_run=dry_run,
            fallback=["switch", "-c", selected_branch],
        )
    else:
        current = git_output(target.path, ["branch", "--show-current"])
        selected_branch = current or target.default_branch

    changed_before = git_status(target.path)
    diff_stat_before = git_diff_stat(target.path)
    committed = False
    pushed = False
    skipped_reason = ""

    if commit:
        if not changed_before:
            skipped_reason = "no changes to commit"
        else:
            # Literal file paths include deletions; no directory-wide or global staging.
            add_args = ["--literal-pathspecs", "add", "--", *dirty_paths]
            run_git(target.path, add_args, commands, dry_run=dry_run)
            commit_message = message or default_commit_message(task_id, target.name)
            run_git(target.path, ["commit", "-m", commit_message], commands, dry_run=dry_run)
            committed = not dry_run

    if push:
        run_git(
            target.path,
            ["push", "-u", target.remote, selected_branch],
            commands,
            dry_run=dry_run,
        )
        pushed = not dry_run

    changed_after = git_status(target.path)
    return PublishResult(
        target=target,
        branch=selected_branch,
        changed_files=changed_after,
        diff_stat=diff_stat_before,
        commands=commands,
        committed=committed,
        pushed=pushed,
        skipped_reason=skipped_reason,
    )


def ensure_git_repository(path: Path) -> None:
    if not path.exists():
        raise RuntimeError(f"Repository path does not exist: {path}")
    result = _git_run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=path,
        text=True, encoding="utf-8", errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Not a git repository: {path}")


def git_dirty_paths(path: Path) -> list[str]:
    raw = _git_run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
                         cwd=path, capture_output=True, check=True).stdout
    records = raw.decode("utf-8", errors="surrogateescape").split("\0")
    names, index = [], 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        names.append(record[3:])
        if "R" in record[:2] or "C" in record[:2]:
            if index >= len(records) or not records[index]:
                raise DomainError(ErrorCode.INVALID_INPUT, "Invalid Git rename status.")
            names.append(records[index])
            index += 1
    return list(dict.fromkeys(names))


def validate_publish_paths(root: Path, changed: list[str], allowed: list[str]) -> None:
    if not allowed:
        raise DomainError(ErrorCode.POLICY_VIOLATION, "Explicit task publish paths required.")
    root = root.resolve()
    normalized = []
    for value in allowed:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value in {"", "."}:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "Unsafe publish allowlist.")
        if not (root/path).resolve().is_relative_to(root):
            raise DomainError(ErrorCode.POLICY_VIOLATION, "Publish path escapes repository.")
        normalized.append(path.as_posix().rstrip("/"))
    outside = [
        value
        for value in changed
        if not (root/value).resolve().is_relative_to(root)
        or not any(value == entry or value.startswith(entry+"/") for entry in normalized)
    ]
    if outside:
        raise DomainError(
            ErrorCode.POLICY_VIOLATION,
            f"수정 허용 경로 밖의 변경이 있어 게시하지 않았습니다: {listed(outside)}. "
            f"수정 허용 경로: {listed(normalized, 8, sort=False)}. "
            "작업 폴더에서 그 변경을 되돌린 뒤 다시 게시하세요. 필요한 파일이면 프로젝트 설정의 "
            "모듈 '수정 허용 경로'에 추가한 뒤 계획을 새로 승인하세요.",
        )


def git_status(path: Path) -> list[str]:
    output = git_output(path, ["status", "--short"])
    return [line for line in output.splitlines() if line.strip()]


def git_diff_stat(path: Path) -> list[str]:
    outputs = [
        git_output(path, ["diff", "--stat"]),
        git_output(path, ["diff", "--cached", "--stat"]),
    ]
    lines: list[str] = []
    for output in outputs:
        for line in output.splitlines():
            clean = line.strip()
            if clean and clean not in lines:
                lines.append(clean)
    return lines


def git_diff_preview(path: Path, max_chars: int = 12000) -> dict:
    ensure_git_repository(path)
    diff_parts = [
        git_output(path, ["diff", "--"]),
        git_output(path, ["diff", "--cached", "--"]),
    ]
    diff_text = "\n".join(part for part in diff_parts if part.strip())
    truncated = False
    if len(diff_text) > max_chars:
        diff_text = diff_text[:max_chars].rstrip() + "\n\n... diff truncated ..."
        truncated = True
    return {
        "status": git_status(path),
        "diff_stat": git_diff_stat(path),
        "files": git_changed_files(path),
        "diff": diff_text,
        "truncated": truncated,
    }


def git_changed_files(path: Path) -> list[str]:
    output = git_output(path, ["status", "--short"])
    files: list[str] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        name = line[3:].strip()
        if " -> " in name:
            name = name.split(" -> ", 1)[1].strip()
        files.append(name)
    return files


def git_output(path: Path, args: list[str]) -> str:
    result = _git_run(
        ["git", *args],
        cwd=path,
        text=True, encoding="utf-8", errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def run_git(
    path: Path,
    args: list[str],
    commands: list[str],
    *,
    dry_run: bool,
    fallback: list[str] | None = None,
) -> None:
    command_text = "git " + " ".join(args)
    commands.append(command_text)
    if dry_run:
        return

    result = _git_run(
        ["git", *args],
        cwd=path,
        text=True, encoding="utf-8", errors="replace",
        capture_output=True,
        check=False,
    )
    if result.returncode == 0:
        return
    stderr = result.stderr.strip()
    # Only take the "create the branch" fallback when the failure actually
    # means the branch doesn't exist yet. Falling back on any other failure
    # (dirty tree, permission error, detached HEAD weirdness) would silently
    # start a fresh branch instead of surfacing the real problem.
    branch_missing = "invalid reference" in stderr or "did not match any" in stderr
    if fallback is not None and branch_missing:
        fallback_text = "git " + " ".join(fallback)
        commands.append(fallback_text)
        fallback_result = _git_run(
            ["git", *fallback],
            cwd=path,
            text=True, encoding="utf-8", errors="replace",
            capture_output=True,
            check=False,
        )
        if fallback_result.returncode == 0:
            return
        raise RuntimeError(fallback_result.stderr.strip() or fallback_result.stdout.strip())
    raise RuntimeError(stderr or result.stdout.strip())


def default_commit_message(task_id: str, target_name: str) -> str:
    return f"{task_id}: update {target_name}"
