"""Publish evidence and approval remain separate from phase/test completion."""

import json
import subprocess
import tempfile
from pathlib import Path

from agent_dev.adapters.external_tools import ProcessOperation
from agent_dev.adapters.state.sqlite.store import identifier
from agent_dev.adapters.worktrees import WorktreeManager, evidence_digest, git
from agent_dev.domain.content import digest
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.status import TaskStatus
from agent_dev.domain.transitions import TransitionContext
from agent_dev.scm.git_publish import RepositoryTarget, publish_repository, validate_publish_paths


def completion_mode(resolved):
    """How an approved task closes (ADR 0003).

    ``local`` commits the verified task branch and fast-forwards the default branch on this
    machine; ``github`` needs pull requests with named CI checks.
    """
    mode = (resolved.get("completion") or {}).get("mode")
    if mode in {"local", "github"}:
        return mode
    # Snapshots approved before ADR 0003 keep GitHub completion where CI checks were named.
    bindings = resolved.get("repositories", {}).values()
    return "github" if any(b.get("required_ci_checks") for b in bindings) else "local"


def _contains(repository, ancestor, ref):
    """Whether ``ref`` already includes ``ancestor`` (no merge needed)."""
    return (
        subprocess.run(
            ["git", "-C", str(repository), "merge-base", "--is-ancestor", ancestor, ref],
            capture_output=True,
            timeout=30,
            check=False,
        ).returncode
        == 0
    )


def _checkout_of(repository, branch):
    """The working tree that has ``branch`` checked out, if any."""
    current = None
    for line in git(repository, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            current = line.removeprefix("worktree ")
        elif line == f"branch refs/heads/{branch}":
            return Path(current) if current else None
    return None


class PublishAdapter:
    def __init__(self, runtime, *, external=None):
        self.runtime, self.store, self.external = runtime, runtime.store, external

    def _gh(self, worktree, args):
        return (
            self.external(args)
            if self.external
            else ProcessOperation(["gh", *args], worktree).execute(120)
        )

    def publish(self, task_id, repository_name, request_id, *, dry_run=False):
        self.store.assert_change_allowed(task_id)
        self.runtime.validate_snapshot_paths(task_id)
        task = self.store.task(task_id)
        if task.state != TaskStatus.PR_READY or not self.store.approved(task_id):
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED,
                "Publish requires current approvals and passing verification.",
            )
        bindings = self.store.snapshot(task_id)["resolved"].get("repositories", {})
        if repository_name not in bindings:
            raise DomainError(ErrorCode.INVALID_INPUT, "Unregistered publish repository.")
        runs = self.store.db.rows(
            "SELECT * FROM verification_runs WHERE task_id=? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        )
        commands = (
            self.store.snapshot(task_id)["resolved"].get("verification", {}).get("commands", [])
        )
        if (
            not runs
            or runs[0]["state"] != "passed"
            or runs[0]["policy_digest"]
            != digest({"commands": commands, "source": evidence_digest(bindings)})
        ):
            raise DomainError(
                ErrorCode.VERIFICATION_FAILED,
                "Source changed after verification; approve a new task plan.",
            )
        local = completion_mode(self.store.snapshot(task_id)["resolved"]) == "local"
        binding = bindings[repository_name]
        if not WorktreeManager.validate(binding):
            raise DomainError(ErrorCode.POLICY_VIOLATION, "Publish worktree ownership mismatch.")
        target = RepositoryTarget(
            repository_name,
            Path(binding["worktree"]),
            binding.get("remote", "origin"),
            binding.get("default_branch", "main"),
            "task",
            tuple(binding.get("allowed_paths", [])),
        )
        committed = git(
            target.path, "diff", "--name-only", binding["base_commit"], "HEAD"
        ).splitlines()
        if committed:
            validate_publish_paths(target.path, committed, list(target.allowed_paths))
        if dry_run:
            result = publish_repository(
                target=target,
                task_id=task_id,
                commit=True,
                push=not local,
                branch=binding["branch"],
                message=None,
                dry_run=True,
            )
            return {
                "dry_run": True,
                "branch": result.branch,
                "commands": result.commands,
                "completion": "local" if local else "github",
            }
        self.runtime.require_flag("publish")
        key = f"publish:{task_id}:{repository_name}:{self.store.snapshot(task_id)['digest']}"

        def operation():
            with (
                self.runtime.safety.lease(f"task:{task_id}") as lease,
                self.runtime.safety.lease(f"repository:{binding['common_dir']}"),
            ):
                current = self.store.task(task_id)
                if (
                    current.state != TaskStatus.PR_READY
                    or current.version != task.version
                    or not self.store.approved(task_id)
                ):
                    raise DomainError(
                        ErrorCode.STALE_VERSION, "Publish task changed while acquiring ownership."
                    )
                if runs[0]["policy_digest"] != digest(
                    {"commands": commands, "source": evidence_digest(bindings)}
                ):
                    raise DomainError(
                        ErrorCode.VERIFICATION_FAILED, "Verified source changed before publish."
                    )
                rows = self.store.db.rows(
                    "SELECT * FROM publish_records WHERE idempotency_key=?", (key,)
                )
                if local:
                    # Local completion: commit the verified changes on the task branch only.
                    if rows:
                        return rows[0]
                    publish_repository(
                        target=target,
                        task_id=task_id,
                        commit=True,
                        push=False,
                        branch=binding["branch"],
                        message=None,
                        dry_run=False,
                    )
                    publish_id, sha = identifier(), git(target.path, "rev-parse", "HEAD")
                    with self.store.db.transaction() as sql:
                        self.store.assert_change_allowed(task_id, sql)
                        self.store.assert_lease(sql, lease)
                        sql.execute(
                            "INSERT INTO publish_records VALUES(?,?,?,?,?,NULL,'pending','unmerged',?)",
                            (publish_id, task_id, repository_name, binding["branch"], sha, key),
                        )
                        self.store._event(
                            sql, task_id, "LocalCommitCreated", {"publish_id": publish_id, "sha": sha}
                        )
                    return self.store.db.rows(
                        "SELECT * FROM publish_records WHERE id=?", (publish_id,)
                    )[0]
                if rows and rows[0]["pr_number"]:
                    return rows[0]
                # A crash after a push is recovered by querying the branch's existing PR first.
                existing = json.loads(
                    self._gh(
                        target.path,
                        [
                            "pr",
                            "list",
                            "--head",
                            binding["branch"],
                            "--state",
                            "all",
                            "--json",
                            "number,headRefOid",
                        ],
                    )
                )
                if not existing:
                    publish_repository(
                        target=target,
                        task_id=task_id,
                        commit=True,
                        push=True,
                        branch=binding["branch"],
                        message=None,
                        dry_run=False,
                    )
                    with tempfile.TemporaryDirectory() as folder:
                        body = Path(folder) / "body.md"
                        body.write_text(f"{task_id}: approved design and verification evidence.\n")
                        self._gh(
                            target.path,
                            [
                                "pr",
                                "create",
                                "--draft",
                                "--head",
                                binding["branch"],
                                "--base",
                                target.default_branch,
                                "--title",
                                f"{task_id}: {task.definition['title']}",
                                "--body-file",
                                str(body),
                            ],
                        )
                    existing = json.loads(
                        self._gh(
                            target.path,
                            [
                                "pr",
                                "list",
                                "--head",
                                binding["branch"],
                                "--state",
                                "all",
                                "--json",
                                "number,headRefOid",
                            ],
                        )
                    )
                if len(existing) != 1:
                    raise DomainError(
                        ErrorCode.POLICY_VIOLATION, "Expected exactly one task pull request."
                    )
                publish_id = rows[0]["id"] if rows else identifier()
                sha = git(target.path, "rev-parse", "HEAD")
                if existing[0].get("headRefOid") != sha:
                    raise DomainError(
                        ErrorCode.STALE_VERSION, "PR head differs from verified worktree."
                    )
                with self.store.db.transaction() as sql:
                    self.store.assert_change_allowed(task_id, sql)
                    self.store.assert_lease(sql, lease)
                    sql.execute(
                        "INSERT INTO publish_records VALUES(?,?,?,?,?,?,'pending','unmerged',?) ON CONFLICT(idempotency_key) DO UPDATE SET commit_sha=excluded.commit_sha,pr_number=excluded.pr_number",
                        (
                            publish_id,
                            task_id,
                            repository_name,
                            binding["branch"],
                            sha,
                            str(existing[0]["number"]),
                            key,
                        ),
                    )
                    self.store._event(
                        sql, task_id, "PullRequestCreated", {"publish_id": publish_id, "sha": sha}
                    )
                return self.store.db.rows(
                    "SELECT * FROM publish_records WHERE id=?", (publish_id,)
                )[0]

        return self.runtime.safety.command(
            request_id, {"operation": "publish", "key": key}, operation
        )

    def _local_state(self, binding, record):
        """Local completion: the committed branch is unchanged, and merged once the default
        branch contains it."""
        worktree = binding["worktree"]
        passed = (
            git(worktree, "rev-parse", "HEAD") == record["commit_sha"]
            and not git(worktree, "status", "--porcelain")
        )
        merged = _contains(
            binding["path"], record["commit_sha"], f"refs/heads/{binding.get('default_branch', 'main')}"
        )
        return passed, merged

    def _merge_locally(self, task_id):
        """Fast-forward each repository's default branch to its verified task commit."""
        bindings = self.store.snapshot(task_id)["resolved"]["repositories"]
        for record in self.store.db.rows(
            "SELECT * FROM publish_records WHERE task_id=?", (task_id,)
        ):
            binding = bindings[record["repository"]]
            source, sha = Path(binding["path"]), record["commit_sha"]
            default = binding.get("default_branch", "main")
            ref = f"refs/heads/{default}"
            if _contains(source, sha, ref):
                continue
            tip = git(source, "rev-parse", ref)
            if not _contains(source, tip, sha):
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION,
                    f"{default} 브랜치가 작업 시작 후 바뀌어 빠른 병합을 할 수 없습니다. "
                    f"작업 브랜치에 {default}를 반영하고 다시 검증한 뒤 완료해주세요.",
                )
            with self.runtime.safety.lease(f"repository:{binding['common_dir']}"):
                checkout = _checkout_of(source, default)
                if checkout is None:
                    git(source, "update-ref", ref, sha, tip)
                elif git(checkout, "status", "--porcelain", "--untracked-files=no"):
                    raise DomainError(
                        ErrorCode.POLICY_VIOLATION,
                        f"{checkout}에 커밋하지 않은 변경이 있어 {default}에 병합할 수 없습니다. "
                        "변경을 정리한 뒤 다시 완료해주세요.",
                    )
                else:
                    git(checkout, "merge", "--ff-only", sha)

    def _refresh(self, task_id, lease):
        resolved = self.store.snapshot(task_id)["resolved"]
        bindings = resolved["repositories"]
        local = completion_mode(resolved) == "local"
        records = self.store.db.rows("SELECT * FROM publish_records WHERE task_id=?", (task_id,))
        updated = {}
        for record in records:
            binding = bindings[record["repository"]]
            if local:
                passed, merged = self._local_state(binding, record)
                self._record_review(task_id, lease, record, passed, merged)
                updated[record["repository"]] = {"passed": passed, "merged": merged}
                continue
            view = json.loads(
                self._gh(
                    binding["worktree"],
                    [
                        "pr",
                        "view",
                        record["pr_number"],
                        "--json",
                        "headRefOid,state,statusCheckRollup",
                    ],
                )
            )
            checks = view.get("statusCheckRollup") or []
            required = set(binding.get("required_ci_checks", []))
            results = {
                check.get("name", check.get("context")): check.get("conclusion", check.get("state"))
                for check in checks
            }
            passed = bool(required) and all(results.get(name) == "SUCCESS" for name in required)
            passed = passed and view.get("headRefOid") == record["commit_sha"]
            merged = view.get("state") == "MERGED"
            self._record_review(task_id, lease, record, passed, merged)
            updated[record["repository"]] = {"passed": passed, "merged": merged}
        required_repositories = {
            name for name, binding in bindings.items() if binding.get("publish_required", True)
        }
        all_passed = bool(required_repositories) and all(
            updated.get(name, {}).get("passed") for name in required_repositories
        )
        all_merged = all_passed and all(updated[name]["merged"] for name in required_repositories)
        return all_passed, all_merged

    def _record_review(self, task_id, lease, record, passed, merged):
        with self.store.db.transaction() as sql:
            self.store.assert_change_allowed(task_id, sql)
            self.store.assert_lease(sql, lease)
            sql.execute(
                "UPDATE publish_records SET ci_state=?,merge_state=? WHERE id=?",
                (
                    "passed" if passed else "failed",
                    "merged" if merged else "unmerged",
                    record["id"],
                ),
            )
            self.store._event(
                sql,
                task_id,
                "PullRequestReviewed",
                {"publish_id": record["id"], "ci_passed": passed, "merged": merged},
            )

    def review(self, task_id):
        self.store.assert_change_allowed(task_id)
        self.runtime.validate_snapshot_paths(task_id)
        self.runtime.require_flag("publish")
        if not self.store.approved(task_id):
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Current publish approvals required.")
        with self.runtime.safety.lease(f"task:{task_id}") as lease:
            passed, merged = self._refresh(task_id, lease)
            task = self.store.task(task_id)
            if passed and task.state == TaskStatus.PR_READY:
                task = self.store.transition(
                    task_id,
                    TaskStatus.USER_REVIEW,
                    TransitionContext(
                        pr_created=True, ci_passed=True, expected_version=task.version
                    ),
                    lease,
                )
            return {
                "task_id": task_id,
                "state": task.state.value,
                "ci_passed": passed,
                "merged": merged,
            }

    def complete(self, task_id, publish_id, actor):
        self.store.assert_change_allowed(task_id)
        self.runtime.validate_snapshot_paths(task_id)
        if not actor:
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Explicit local-user approval required.")
        self.runtime.require_flag("publish")
        if not self.store.approved(task_id):
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Current publish approvals required.")
        if not self.store.db.rows(
            "SELECT id FROM publish_records WHERE id=? AND task_id=?", (publish_id, task_id)
        ):
            raise DomainError(ErrorCode.INVALID_INPUT, "Unknown publish evidence.")
        local = completion_mode(self.store.snapshot(task_id)["resolved"]) == "local"
        with self.runtime.safety.lease(f"task:{task_id}") as lease:
            passed, merged = self._refresh(task_id, lease)
            if local and passed and not merged:
                # The explicit completion approval is the merge decision for local completion.
                self._merge_locally(task_id)
                passed, merged = self._refresh(task_id, lease)
            if not passed or not merged:
                raise DomainError(
                    ErrorCode.VERIFICATION_FAILED,
                    "게시한 작업 커밋이 바뀌었거나 기본 브랜치에 병합되지 않았습니다. 다시 게시·검증해주세요."
                    if local
                    else "All required repositories need merged PRs and successful named CI checks.",
                )
            task = self.store.task(task_id)
            if task.state == TaskStatus.COMPLETED:
                return {"task_id": task_id, "state": task.state.value}
            if task.state == TaskStatus.PR_READY:
                task = self.store.transition(
                    task_id,
                    TaskStatus.USER_REVIEW,
                    TransitionContext(
                        pr_created=True, ci_passed=True, expected_version=task.version
                    ),
                    lease,
                )
            with self.store.db.transaction() as sql:
                self.store.assert_change_allowed(task_id, sql)
                self.store.assert_lease(sql, lease)
                self.store._event(
                    sql, task_id, "UserApproval", {"actor": actor, "publish_id": publish_id}
                )
            findings = self.store.db.rows(
                "SELECT id FROM findings WHERE task_id=? AND state='open'", (task_id,)
            )
            task = self.store.transition(
                task_id,
                TaskStatus.COMPLETED,
                TransitionContext(
                    user_approved=True,
                    merge_policy_satisfied=True,
                    no_blocking_findings=not findings,
                    expected_version=task.version,
                ),
                lease,
            )
        # Merged code is the new baseline; refresh the code graph outside the task lease.
        from agent_dev.adapters import code_graph

        return {
            "task_id": task_id,
            "state": task.state.value,
            "code_graph": code_graph.refresh(self.runtime.project_root),
        }
