"""Feedback and knowledge preserve source lifecycles and project isolation."""

from pathlib import Path

from agent_dev.adapters.state.sqlite.store import identifier
from agent_dev.domain.content import content_digest, digest, redact
from agent_dev.domain.errors import DomainError, ErrorCode


class GovernanceRepository:
    def __init__(self, repository, project_root: Path):
        self.repository, self.root = repository, Path(project_root).resolve()

    def feedback(self, task_id: str, content: str, *, source_key: str = "") -> str:
        task = self.repository.task(task_id)
        safe = redact(content, self.repository.secrets)
        fingerprint = digest([task_id, source_key or safe])
        with self.repository.db.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO feedback_tasks VALUES(?,?,?,?,?,'planned')",
                (
                    identifier(),
                    task.project_id,
                    task_id,
                    fingerprint,
                    self.repository._json({"content": safe, "source_key": source_key}),
                ),
            )
            row = connection.execute(
                "SELECT id FROM feedback_tasks WHERE source_task_id=? AND fingerprint=?",
                (task_id, fingerprint),
            ).fetchone()
            self.repository._event(
                connection, row[0], "FeedbackEvidenceRecorded", {"source_task_id": task_id}
            )
            return row[0]

    def plan_feedback(self, feedback_id, plan_id, design_id, design_version):
        rows = self.repository.db.rows("SELECT * FROM feedback_tasks WHERE id=?", (feedback_id,))
        if not rows:
            raise DomainError(ErrorCode.INVALID_INPUT, "Unknown feedback.")
        feedback = rows[0]
        task_id = "FB-" + feedback_id
        if feedback["state"] != "planned":
            return {"feedback_id": feedback_id, "task_id": task_id, "state": feedback["state"]}
        content = __import__("json").loads(feedback["content_json"])
        self.repository.create_plan(
            feedback["project_id"],
            plan_id,
            design_id,
            design_version,
            [
                {
                    "id": task_id,
                    "title": "Feedback: " + content["content"][:120],
                    "source_task_id": feedback["source_task_id"],
                    "description": content["content"],
                }
            ],
        )
        with self.repository.db.transaction() as sql:
            sql.execute(
                "UPDATE feedback_tasks SET state='approval_pending' WHERE id=?", (feedback_id,)
            )
        return {
            "feedback_id": feedback_id,
            "task_id": task_id,
            "plan_id": plan_id,
            "state": "approval_pending",
        }

    def knowledge_candidate(self, project_id: str, source: str, commit: str, content: str) -> str:
        path = (self.root / source).resolve()
        if not path.is_relative_to(self.root) or not path.is_file() or not commit:
            raise DomainError(
                ErrorCode.INVALID_INPUT, "Knowledge requires bounded source and commit."
            )
        from agent_dev.adapters.worktrees import git

        resolved_commit = git(self.root, "rev-parse", "--verify", commit + "^{commit}")
        # Source provenance refers to committed bytes, not an uncommitted local revision.
        committed = __import__("subprocess").run(
            [
                "git",
                "-C",
                str(self.root),
                "show",
                resolved_commit + ":" + str(path.relative_to(self.root)),
            ],
            capture_output=True,
            check=False,
            timeout=30,
        )
        if committed.returncode or committed.stdout != path.read_bytes():
            raise DomainError(
                ErrorCode.STALE_VERSION, "Knowledge source differs from its recorded commit."
            )
        commit = resolved_commit
        candidate_id = identifier()
        with self.repository.db.transaction() as connection:
            connection.execute(
                "INSERT INTO knowledge_candidates VALUES(?,?,?,?,?,?,'candidate','',?)",
                (
                    candidate_id,
                    project_id,
                    str(path.relative_to(self.root)),
                    content_digest(path.read_bytes()),
                    commit,
                    redact(content, self.repository.secrets),
                    self.repository.clock(),
                ),
            )
        return candidate_id

    def confirm_knowledge(self, candidate_id: str, evidence_id: str) -> None:
        with self.repository.db.transaction() as connection:
            candidate = connection.execute(
                "SELECT * FROM knowledge_candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            evidence = connection.execute(
                "SELECT v.state,t.project_id FROM verification_runs v JOIN tasks t ON t.id=v.task_id WHERE v.id=?",
                (evidence_id,),
            ).fetchone()
            if (
                candidate is None
                or evidence is None
                or evidence["state"] != "passed"
                or evidence["project_id"] != candidate["project_id"]
            ):
                raise DomainError(
                    ErrorCode.POLICY_VIOLATION,
                    "Confirmed knowledge needs same-project passing evidence.",
                )
            path = (self.root / candidate["source_path"]).resolve()
            if (
                not path.is_relative_to(self.root)
                or not path.is_file()
                or content_digest(path.read_bytes()) != candidate["source_digest"]
            ):
                raise DomainError(ErrorCode.STALE_VERSION, "Knowledge source changed.")
            connection.execute(
                "UPDATE knowledge_candidates SET state='confirmed',evidence=? WHERE id=?",
                (evidence_id, candidate_id),
            )

    def search_knowledge(self, project_id: str, query: str) -> list[dict]:
        rows = self.repository.db.rows(
            "SELECT * FROM knowledge_candidates WHERE project_id=? AND state='confirmed' "
            "AND instr(lower(content),lower(?))>0 LIMIT 20",
            (project_id, query),
        )
        result = []
        for row in rows:
            path = (self.root / row["source_path"]).resolve()
            if (
                path.is_relative_to(self.root)
                and path.is_file()
                and content_digest(path.read_bytes()) == row["source_digest"]
            ):
                result.append(dict(row, trust="verified_data_not_instructions"))
        return result

    def deprecate_knowledge(self, candidate_id: str) -> None:
        with self.repository.db.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_candidates SET state='deprecated' WHERE id=?", (candidate_id,)
            )
