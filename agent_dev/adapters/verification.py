"""Approved argument-array verification, bounded to registered worktrees."""

import subprocess
from dataclasses import replace
from pathlib import Path

from agent_dev.adapters.bounded_process import run_bounded
from agent_dev.adapters.pytest_attestation import observe
from agent_dev.adapters.state.sqlite.store import identifier
from agent_dev.adapters.worktrees import (
    changed_repositories,
    commit_fingerprint,
    evidence_digest,
    skipped_gate_evidence,
)
from agent_dev.domain.content import digest, redact
from agent_dev.domain.contracts import GateResult, aggregate_gates
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.status import TaskStatus
from agent_dev.domain.transitions import TransitionContext, transition_task


class VerificationAdapter:
    def __init__(self, repository, artifacts=None, config_root=None):
        self.repository, self.artifacts = repository, artifacts
        self.config_root = config_root

    def verify(self, task_id: str, lease: dict) -> dict:
        self.repository.assert_change_allowed(task_id)
        task = self.repository.task(task_id)
        if task.state != TaskStatus.VERIFYING:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "Task phases have not completed.")
        resolved = self.repository.snapshot(task_id)["resolved"]
        from agent_dev.adapters.environment import process_environment

        process_env = (
            process_environment(self.config_root, resolved.get("environment_keys", []))
            if self.config_root
            else None
        )
        bindings = resolved.get("repositories", {})
        commands = resolved.get("verification", {}).get("commands", [])
        before = evidence_digest(bindings)
        source_commits = commit_fingerprint(bindings)
        results = []
        observations = {}
        # "changed" skips gates of repositories this task did not touch.
        changed = (
            changed_repositories(bindings)
            if resolved.get("verification", {}).get("scope") == "changed"
            else None
        )
        for item in commands:
            binding = bindings.get(item.get("repository"))
            if binding is None:
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION, "Verification repository must be registered."
                )
            if changed is not None and item["repository"] not in changed:
                evidence = skipped_gate_evidence(item["repository"], binding)
                results.append(
                    GateResult(item["name"], True, item.get("required", True), None, evidence)
                )
                continue
            root = Path(binding["worktree"]).resolve()
            cwd = (root / item.get("cwd", ".")).resolve()
            command = item.get("command")
            if (
                not cwd.is_relative_to(root)
                or not isinstance(command, list)
                or not command
                or any(not isinstance(arg, str) for arg in command)
            ):
                raise DomainError(
                    ErrorCode.INVALID_INPUT, "Unsafe verification command or directory."
                )
            try:
                with observe(
                    command,
                    process_env,
                    root,
                    cwd,
                    source_roots=[b["worktree"] for b in bindings.values()],
                ) as (gate_env, read_observations):
                    process = run_bounded(
                        command, cwd=cwd, env=gate_env,
                        timeout=max(1, int(item.get("timeout_seconds", 900))),
                        limit=2_000_000, tail=12000,
                    )
                    code = 125 if process.overflow else process.returncode
                    observations[item["name"]] = read_observations(code)
                evidence = redact(
                    process.output.decode("utf-8", errors="replace"), self.repository.secrets
                )
                if process.truncated:
                    evidence = "[Output truncated; last 12000 bytes retained.]\n" + evidence
                if process.overflow:
                    evidence = "Verification output limit exceeded (2000000 bytes).\n" + evidence
            except subprocess.TimeoutExpired:
                code, evidence = 124, "Verification timeout."
            except OSError:
                code, evidence = 127, "Verification executable unavailable."
            except DomainError as exc:
                if exc.code != ErrorCode.CAPABILITY_UNAVAILABLE:
                    raise
                code, evidence = 126, "Verification process cleanup could not be proven."
            results.append(
                GateResult(item["name"], code == 0, item.get("required", True), code, evidence)
            )
        if len({result.gate for result in results}) != len(results):
            raise DomainError(ErrorCode.INVALID_INPUT, "Duplicate verification gate name.")
        # Required names are part of the approved policy. Missing results fail closed.
        required_gates = set(resolved.get("verification", {}).get("required_gates", []))
        results = [
            replace(result, required=True) if result.gate in required_gates else result
            for result in results
        ]
        for missing in required_gates - {result.gate for result in results}:
            results.append(GateResult(missing, False, True, None, "Required gate missing."))
        if evidence_digest(bindings) != before:
            results.append(
                GateResult(
                    "source_unchanged", False, True, None, "Verification changed executable source."
                )
            )
        if commit_fingerprint(bindings) != source_commits:
            results.append(
                GateResult(
                    "commit_unchanged", False, True, None, "Verification changed Git revision."
                )
            )
        if not self.repository.approved(task_id):
            raise DomainError(
                ErrorCode.APPROVAL_REQUIRED, "Approvals invalidated during verification."
            )
        passed = aggregate_gates(results)
        run_id = identifier()
        with self.repository.db.transaction() as connection:
            self.repository.assert_change_allowed(task_id, connection)
            self.repository.assert_lease(connection, lease)
            self.repository._task_lease(task_id, lease)
            connection.execute(
                "INSERT INTO verification_runs VALUES(?,?,?,?,?)",
                (
                    run_id,
                    task_id,
                    "passed" if passed else "failed",
                    digest({"commands": commands, "source": before}),
                    self.repository.clock(),
                ),
            )
            for result in results:
                connection.execute(
                    "INSERT INTO verification_results VALUES(?,?,?,?,?,?,?)",
                    (
                        identifier(),
                        run_id,
                        result.gate,
                        result.passed,
                        result.required,
                        result.exit_code,
                        self.repository._json({"text": result.evidence}),
                    ),
                )
            self.repository._event(
                connection, task_id, "VerificationFinished", {"run_id": run_id, "passed": passed}
            )
            if self.artifacts is not None:
                from agent_dev.domain.content import content_digest

                directory = self.artifacts.directory(task_id)
                path = (directory / "verification" / (run_id + "-report.md")).resolve()
                if not path.is_relative_to(self.artifacts.root):
                    raise DomainError(
                        ErrorCode.POLICY_VIOLATION, "Verification report escaped output root."
                    )
                report = "# Verification report\n\n" + self.repository._json(
                    {
                        "run_id": run_id,
                        "passed": passed,
                        "snapshot_digest": self.repository.snapshot(task_id)["digest"],
                        "source_digest": before,
                        "source_commits": source_commits,
                        "policy_digest": digest({"commands": commands, "source": before}),
                        "gates": [
                            {
                                "name": r.gate,
                                "passed": r.passed,
                                "required": r.required,
                                "exit_code": r.exit_code,
                                "evidence": r.evidence,
                                "test_results": observations.get(r.gate),
                            }
                            for r in results
                        ],
                    }
                )
                path.parent.mkdir(exist_ok=True)
                payload = report.encode("utf-8")
                path.write_bytes(payload)
                path.chmod(0o600)
                owner = connection.execute(
                    "SELECT id FROM phase_attempts WHERE task_id=? AND state='passed' ORDER BY started_at DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
                connection.execute(
                    "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        identifier(),
                        task.project_id,
                        task_id,
                        owner[0] if owner else None,
                        str(path.relative_to(self.artifacts.root)),
                        content_digest(payload),
                        "verification",
                        "trusted_gate_result",
                        self.repository.clock(),
                    ),
                )
                latest = (directory / "verification-report.md").resolve()
                if not latest.is_relative_to(self.artifacts.root):
                    raise DomainError(
                        ErrorCode.POLICY_VIOLATION, "Verification pointer escaped output root."
                    )
                latest.write_bytes(payload)
            row = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            target = TaskStatus.PR_READY if passed else TaskStatus.BLOCKED
            transition_task(
                TaskStatus(row["state"]),
                target,
                TransitionContext(
                    verification_passed=passed,
                    expected_version=task.version,
                    current_version=row["version"],
                ),
            )
            connection.execute(
                "UPDATE tasks SET state=?,version=version+1 WHERE id=?", (target.value, task_id)
            )
            self.repository._event(
                connection, task_id, "TaskStateChanged", {"from": row["state"], "to": target.value}
            )
            if passed:
                connection.execute(
                    "UPDATE findings SET state='resolved' WHERE task_id=? AND state='open'",
                    (task_id,),
                )
        linked = 0
        if passed:
            from agent_dev.adapters.traceability import link_criteria_tests

            try:
                linked = link_criteria_tests(self.repository.db, task_id, run_id)
            except (DomainError, ValueError, KeyError):
                linked = 0  # Requirement links are evidence, never a verification gate.
        return {
            "task_id": task_id,
            "run_id": run_id,
            "passed": passed,
            "state": target.value,
            "gates": [{"gate": r.gate, "passed": r.passed} for r in results],
            "requirement_links": linked,
        }
