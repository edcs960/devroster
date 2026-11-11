import json
import subprocess
from pathlib import Path

from agent_dev.domain.errors import DomainError, ErrorCode


class SnapshotPhaseCatalog:
    def __init__(self, repository, project_root: Path):
        self.repository, self.project_root = repository, Path(project_root).resolve()

    def phases(self, task):
        resolved = self.repository.snapshot(task.task_id)["resolved"]
        phases = resolved.get("phases", [])
        if not phases or any(
            not isinstance(p, dict)
            or not isinstance(p.get("id"), str)
            or not p["id"]
            or Path(p["id"]).name != p["id"]
            or p["id"] in {".", ".."}
            or not isinstance(p.get("capabilities", []), list)
            or any(not isinstance(c, str) for c in p.get("capabilities", []))
            for p in phases
        ):
            raise DomainError(ErrorCode.INVALID_INPUT, "Approved phase catalog required.")
        if len({phase["id"] for phase in phases}) != len(phases):
            raise DomainError(ErrorCode.INVALID_INPUT, "Duplicate phase identifier.")
        from agent_dev.domain.role_policy import legacy_role, policy

        result = []
        for phase in phases:
            requested = set(phase.get("capabilities", ["code.read"]))
            authority = policy(legacy_role(phase))
            caps = requested & set(authority["capabilities"])
            if requested == {"text.generate"} and authority["mode"] != "code":
                caps = requested
            result.append((phase["id"], frozenset(caps or {"code.read"})))
        return result

    def preflight(self, task):
        resolved = self.repository.snapshot(task.task_id)["resolved"]
        if not resolved.get("workflow_graph") or resolved.get("workflow_legacy", False):
            self.phases(task)
        resolved = self.repository.snapshot(task.task_id)["resolved"]
        repositories = resolved.get("repositories", {})
        return bool(repositories) and all(
            Path(binding["worktree"]).is_dir() and (Path(binding["worktree"]) / ".git").exists()
            for binding in repositories.values()
        )

    def verify_node(self, task_id, gate_ids, lease):
        """Execute registered gates, recording real evidence before advancing the DAG."""
        self.repository.assert_change_allowed(task_id)
        import subprocess

        from agent_dev.adapters.worktrees import (
            changed_repositories,
            evidence_digest,
            skipped_gate_evidence,
        )
        from agent_dev.domain.content import redact

        resolved = self.repository.snapshot(task_id)["resolved"]
        from agent_dev.adapters.environment import process_environment
        from agent_dev.adapters.process_launcher import environment

        keys = resolved.get("environment_keys", [])
        process_env = environment(
            "check", allowed=keys, source=process_environment(self.project_root, keys)
        )
        commands = resolved.get("verification", {}).get("commands", [])
        selected = [c for c in commands if not gate_ids or c["name"] in gate_ids]
        missing = set(gate_ids) - {c["name"] for c in selected}
        results = [
            {"gate": name, "passed": False, "exit_code": None, "evidence": "Required gate missing."}
            for name in sorted(missing)
        ]
        bindings = resolved.get("repositories", {})
        before = evidence_digest(bindings)
        changed = (
            changed_repositories(bindings)
            if resolved.get("verification", {}).get("scope") == "changed"
            else None
        )
        for item in selected:
            binding = bindings.get(item.get("repository"))
            if not binding:
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION, "Verification repository must be registered."
                )
            if changed is not None and item["repository"] not in changed:
                results.append(
                    {
                        "gate": item["name"],
                        "passed": True,
                        "required": item.get("required", True),
                        "exit_code": None,
                        "evidence": skipped_gate_evidence(item["repository"], binding),
                    }
                )
                continue
            root = Path(binding["worktree"]).resolve()
            cwd = (root / item.get("cwd", ".")).resolve()
            args = item.get("command")
            if (
                not cwd.is_relative_to(root)
                or not isinstance(args, list)
                or not args
                or any(not isinstance(a, str) for a in args)
            ):
                raise DomainError(ErrorCode.INVALID_INPUT, "Unsafe registered gate command.")
            try:
                from agent_dev.adapters.guarded_verification import run_gate
                code, text = run_gate(
                    args, cwd, process_env,
                    min(900, max(1, int(item.get("timeout_seconds", 900)))),
                    self.repository.db.path, lease,
                )
            except subprocess.TimeoutExpired:
                code, text = 124, "Verification timeout."
            except OSError:
                code, text = 127, "Verification executable unavailable."
            results.append(
                {
                    "gate": item["name"],
                    "passed": code == 0,
                    "required": item.get("required", True),
                    "exit_code": code,
                    "evidence": redact(text, self.repository.secrets),
                }
            )
        required_gates = set(resolved.get("verification", {}).get("required_gates", []))
        required_for_node = required_gates if not gate_ids else set(gate_ids) & required_gates
        for result in results:
            if result["gate"] in required_for_node:
                result["required"] = True
        present = {result["gate"] for result in results}
        for missing in sorted(required_for_node - present):
            results.append(
                {
                    "gate": missing,
                    "passed": False,
                    "required": True,
                    "exit_code": None,
                    "evidence": "Required gate missing.",
                }
            )
        if len({c["name"] for c in commands}) != len(commands):
            raise DomainError(ErrorCode.INVALID_INPUT, "Duplicate verification gate name.")
        if evidence_digest(bindings) != before:
            results.append(
                {
                    "gate": "source_unchanged",
                    "passed": False,
                    "required": True,
                    "evidence": "Verification modified executable source.",
                }
            )
        passed = bool(results) and all(r["passed"] for r in results if r.get("required", True))
        with self.repository.db.transaction() as sql:
            self.repository.assert_change_allowed(task_id, sql)
            self.repository.assert_lease(sql, lease)
            self.repository._task_lease(task_id, lease)
            self.repository._event(
                sql, task_id, "WorkflowVerificationFinished", {"passed": passed, "gates": results}
            )
        return {"passed": passed, "gates": results}

    def role_before(self, task_id, node_id):
        from agent_dev.adapters.role_execution import capture

        return capture(self.repository.snapshot(task_id)["resolved"].get("repositories", {}))

    def save_baseline(self, task_id, node_id, attempt_id, before):
        """Where this attempt started, so a crash can be checked on the next run."""
        from agent_dev.adapters.role_execution import save_baseline

        if before is None:
            return
        bindings = self.repository.snapshot(task_id)["resolved"].get("repositories", {})
        try:
            save_baseline(bindings, attempt_id, before)
        except (OSError, ValueError, subprocess.SubprocessError):
            # Without a baseline the crash check is skipped; the run itself is unaffected.
            self.repository.event(task_id, "AttemptBaselineSkipped", {"attempt_id": attempt_id, "node_id": node_id})

    def _node_policy(self, resolved, node_id):
        policy = resolved.get("role_policies", {}).get(node_id)
        if policy:
            return policy
        from agent_dev.domain.role_policy import legacy_role
        from agent_dev.domain.role_policy import policy as make_policy

        phase = next((p for p in resolved.get("phases", []) if p.get("id") == node_id), {"id": node_id})
        bindings = resolved.get("repositories", {})
        return make_policy(
            legacy_role({"id": node_id, "capabilities": list(phase.get("capabilities", []))}),
            project_paths=resolved.get("allowed_paths", [])
            or [p for b in bindings.values() for p in b.get("allowed_paths", [])],
        )

    def recover_interrupted(self, task_id, node_id):
        """Check what a crashed attempt of this node left in the worktree.

        Nothing changed, or changes within the node's role policy: continue (the role picks
        up its own partial work). Changes the role was not allowed to make: copy them aside
        and stop with INTERRUPTED_CHANGES; when the user resumes, put those paths back to
        where the attempt started and continue.
        """
        from agent_dev.adapters.role_execution import (
            changed_since,
            enforce,
            load_baseline,
            restore_paths,
        )

        rows = self.repository.db.rows(
            "SELECT id,state FROM phase_attempts WHERE task_id=? AND phase_id=? ORDER BY started_at DESC, id DESC LIMIT 1",
            (task_id, node_id),
        )
        if not rows or rows[0]["state"] != "interrupted":
            return
        attempt_id = rows[0]["id"]
        handled = {}
        for row in self.repository.db.rows(
            "SELECT kind,payload_json FROM events WHERE aggregate_id=? AND kind LIKE 'InterruptedChanges%'",
            (task_id,),
        ):
            payload = json.loads(row["payload_json"])
            if payload.get("attempt_id") == attempt_id:
                handled[row["kind"]] = payload
        if handled.keys() & {"InterruptedChangesKept", "InterruptedChangesDiscarded", "InterruptedChangesNone"}:
            return
        resolved = self.repository.snapshot(task_id)["resolved"]
        bindings = resolved.get("repositories", {})
        baseline = load_baseline(bindings, attempt_id)
        if baseline is None:
            return  # Started before baselines existed, or the baseline could not be written.
        changed = changed_since(bindings, baseline["capture"])
        record = {"attempt_id": attempt_id, "node_id": node_id, "paths": changed}
        if not changed:
            self.repository.event(task_id, "InterruptedChangesNone", record)
            return
        if "InterruptedChangesHeld" in handled:
            # The user resumed after the stop: undo only what the crashed role was not allowed to do.
            restore_paths(bindings, baseline, changed, handled["InterruptedChangesHeld"]["backup"])
            self.repository.event(task_id, "InterruptedChangesDiscarded", record)
            return
        graph = {n["id"]: n for n in resolved.get("workflow_graph", {}).get("nodes", [])}
        try:
            enforce(bindings, baseline["capture"], self._node_policy(resolved, node_id), graph.get(node_id, {}).get("module"))
        except DomainError:
            backup = Path(next(iter(bindings.values()))["worktree"]).resolve().parent / ".attempts" / (attempt_id + "-interrupted")
            for name, paths in changed.items():
                root = Path(bindings[name]["worktree"]).resolve()
                for relative in paths:
                    source = root / relative
                    if source.is_file() and not source.is_symlink():
                        target = backup / name / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(source.read_bytes())
            self.repository.event(task_id, "InterruptedChangesHeld", dict(record, backup=str(backup)))
            raise DomainError(ErrorCode.POLICY_VIOLATION, "INTERRUPTED_CHANGES") from None
        self.repository.event(task_id, "InterruptedChangesKept", record)

    def enforce_session(self, task_id, node_ids, before, declared=None):
        """Check one session invocation's changes against the union of member code policies.

        Returns the changed paths. Paths a node declares (`changed_paths`) must also fit that
        node's own policy, so a session cannot launder one role's write through another.
        """
        from agent_dev.adapters.role_execution import enforce
        from agent_dev.domain.role_policy import allowed_path, session_policy

        resolved = self.repository.snapshot(task_id)["resolved"]
        policies = resolved.get("role_policies", {})
        nodes = {n["id"]: n for n in resolved.get("workflow_graph", {}).get("nodes", [])}
        modules = {nodes.get(n, {}).get("module") for n in node_ids} - {None}
        bindings = resolved.get("repositories", {})
        after = enforce(
            bindings,
            before,
            session_policy([policies.get(n, {}) for n in node_ids]),
            next(iter(modules)) if modules else None,
        )
        changed, roots = [], {}
        for name, binding in bindings.items():
            previous, current = before.get(name, {}), after.get(name, {})
            for path in sorted(previous.keys() | current.keys()):
                if previous.get(path) != current.get(path):
                    changed.append(path)
                    roots[path] = binding
        declared = declared or {}
        for node_id, paths in declared.items():
            policy = policies.get(node_id, {})
            for path in paths:
                if path not in roots:
                    continue  # Declared but unchanged: nothing to attribute.
                binding = roots[path]
                narrowed = dict(
                    policy, scopes=policy.get("scopes", []) + [binding.get("allowed_paths", [])]
                )
                if policy.get("mode") != "code" or not allowed_path(
                    Path(binding["worktree"]), path, narrowed
                ):
                    raise DomainError(
                        ErrorCode.POLICY_VIOLATION,
                        f"Session node {node_id} changed a path outside its own scope: {path}",
                    )
        attributed = {path for paths in declared.values() for path in paths}
        return {"changed": changed, "unattributed": [p for p in changed if p not in attributed]}

    def apply_role_result(self, task_id, node_id, output, before=None, lease=None, generation=None):
        import re

        from agent_dev.domain.role_policy import apply_structured

        resolved = self.repository.snapshot(task_id)["resolved"]
        role_policy = resolved.get("role_policies", {}).get(node_id, {})
        graph = resolved.get("workflow_graph")
        if graph:
            node = next(n for n in graph["nodes"] if n["id"] == node_id)
        else:
            node = next(p for p in resolved.get("phases", []) if p["id"] == node_id)
        if not role_policy:
            from agent_dev.domain.role_policy import legacy_role, policy

            phase = next((p for p in resolved.get("phases", []) if p["id"] == node_id), node)
            paths = resolved.get("allowed_paths", []) or list(
                dict.fromkeys(
                    p
                    for b in resolved.get("repositories", {}).values()
                    for p in b.get("allowed_paths", [])
                )
            )
            role_policy = policy(
                legacy_role(phase),
                project_paths=paths,
                node_paths=phase.get("allowed_paths", []),
                test_paths=resolved.get("test_paths", []),
                document_paths=resolved.get("document_paths", []),
            )
            if role_policy["mode"] == "code" and "code.write" not in phase.get("capabilities", []):
                role_policy["mode"] = "readonly"
        if before is not None:
            from agent_dev.adapters.role_execution import enforce

            direct_policy = (
                dict(role_policy, mode="readonly")
                if role_policy.get("mode") == "structured"
                else role_policy
            )
            enforce(resolved.get("repositories", {}), before, direct_policy, node.get("module"))
        if role_policy.get("mode") != "structured":
            return
        parsed = json_blocks(output)
        proposal = next((p for p in parsed if isinstance(p, dict) and "edits" in p), None)
        if proposal is None and re.search(r'```json\s*\{\s*"edits"', output):
            # A proposal we cannot read must not pass as if the documents were updated.
            raise DomainError(ErrorCode.INVALID_INPUT, "Structured edits could not be parsed.")
        named = (proposal or {}).get("criteria_tests")
        if isinstance(named, dict) and named:
            # Read after verification passes to link requirements to their tests.
            self.repository.event(
                task_id,
                "CriteriaTestsNamed",
                {
                    "node_id": node_id,
                    "criteria_tests": {
                        str(k)[:80]: str(v)[:300] for k, v in list(named.items())[:200]
                    },
                },
            )
        # `{"edits": []}` is a readable answer: the role proposes no change.
        edits = [
            edit
            for edit in (proposal or {}).get("edits") or []
            # DevRoster keeps task handoff notes itself; they never belong in the project.
            if not handoff_note(str(edit.get("path", "")) if isinstance(edit, dict) else "")
        ]
        if not edits:
            return
        # Without these profile keys every proposal falls outside the role scope.
        missing = {"tests": "test_paths", "documents": "document_paths"}.get(
            role_policy.get("structured_kind", "")
        )
        if missing and not resolved.get(missing):
            raise DomainError(
                ErrorCode.POLICY_VIOLATION,
                f"프로필에 {missing}가 없어 {node_id} 수정안을 적용할 수 없습니다. "
                f"프로필 {missing}에 허용 경로(예: tests, docs)를 추가해주세요.",
            )
        bindings = resolved.get("repositories", {})
        module = node.get("module")
        if not module and len(bindings) != 1:
            raise DomainError(
                ErrorCode.POLICY_VIOLATION,
                "Structured role requires an explicit repository module.",
            )
        binding = bindings[module] if module else next(iter(bindings.values()))
        narrowed = dict(
            role_policy, scopes=role_policy["scopes"] + [binding.get("allowed_paths", [])]
        )
        if lease is None or generation is None:
            raise DomainError(
                ErrorCode.LEASE_CONFLICT,
                "Structured edit requires current task execution authority.",
            )
        with self.repository.db.transaction() as sql:
            self.repository._task_lease(task_id, lease)
            self.repository.assert_lease(sql, lease)
            current = sql.execute(
                "SELECT generation,state FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
            if (
                current is None
                or current["generation"] != generation
                or current["state"] != "running"
                or not self.repository._approved(sql, task_id)
            ):
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION, "Late structured edit or invalid task approval."
                )
            apply_structured(Path(binding["worktree"]), edits, narrowed)
            self.repository._event(
                sql,
                task_id,
                "StructuredRoleEditsApplied",
                {
                    "node_id": node_id,
                    "generation": generation,
                    "paths": [edit["path"] for edit in edits],
                },
            )


def handoff_note(path):
    """Agent handoff checkpoints (AGENTS.md §11) that a role may try to write into the project."""
    parts = Path(path).parts
    return bool(parts) and (parts[-1] == "latest-checkpoint.md" or "checkpoints" in parts[:-1])


def json_blocks(output):
    """JSON values that follow each ```json marker.

    Decoding the value itself, instead of cutting at the next fence, keeps code fences that
    appear inside string values (Markdown documents) intact.
    """
    import json
    import re

    decoder = json.JSONDecoder()
    values = []
    for match in re.finditer(r"```json[ \t]*\n?", output):
        start = match.end()
        while start < len(output) and output[start].isspace():
            start += 1
        try:
            value, _ = decoder.raw_decode(output, start)
        except json.JSONDecodeError:
            continue
        values.append(value)
    return values

