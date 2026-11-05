"""Revisioned wiki knowledge: hashed sources, explicit approval and generated Markdown views.

A revision is the unit of approval. Sources are re-hashed at approval and at every read,
so an approved item whose evidence changed stops being served until a new revision is
approved. The Markdown under ``docs/wiki`` is generated from active revisions only and is
never read back as knowledge.
"""

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import NoReturn

from agent_dev.adapters.state.sqlite.store import identifier
from agent_dev.core.output_paths import slugify
from agent_dev.domain.content import content_digest, digest, redact
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.project_roles import CATALOG, LABELS, is_custom

PAGES = {"lessons": "재발 방지 목록", "testing": "테스트 실행"}
ROLES = frozenset(CATALOG) | {"all"}
IMPORTANCE = ("high", "normal", "low")
MODULE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ITEM_ID = re.compile(r"^[A-Z][A-Z0-9-]{1,31}$")
TAG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
LIMITS = {"title": 120, "check": 1000, "condition": 300}
MAX_SOURCES = 10
MAX_SOURCE_BYTES = 5 * 1024 * 1024
WIKI_DIR = Path("docs/wiki")
PENDING = {"state": "pending"}
# Initial budget (plan §8): characters of the serialized prompt entry, not tokens.
POLICY = {"version": 1, "role_items": 8, "role_chars": 6000, "session_chars": 24000}
PROMPT_FIELDS = ("id", "page", "title", "check", "condition", "sources", "trust")
EVIDENCE_KINDS = frozenset({"phase_output", "test_report", "file", "verification_run"})
RECORD_KINDS = EVIDENCE_KINDS | {"source"}
OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
RESOLUTION = re.compile(r"^## Resolution\s*\n(.*?)(?=^## |\Z)", re.DOTALL | re.MULTILINE)
FENCE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)
# Whole field values only: "Resolved: trueish" or "accepted_pending" are not resolutions.
RESOLVED = re.compile(
    r"^\s*(?:-\s*)?(?:Resolved:\s*true|Resolution:\s*accepted)(?![\w-])",
    re.IGNORECASE | re.MULTILINE,
)
UNRESOLVED = re.compile(r"^\s*(?:-\s*)?Resolved:\s*false(?![\w-])", re.IGNORECASE | re.MULTILINE)
SUPERSEDED = re.compile(r"대체|supersed|replaced", re.IGNORECASE)
SUCCESSOR = re.compile(r"issue-\d+\.md")
CONTROL_BLOCK = re.compile(r"```(?:json)?[ \t]*\n(.*?)\n[ \t]*```", re.DOTALL)


def _invalid(message) -> NoReturn:
    raise DomainError(ErrorCode.INVALID_INPUT, message)


def _git(top, *arguments, text=False):
    # Replacement refs could swap the bytes a pinned commit shows; never honour them.
    return subprocess.run(
        ["git", "--no-replace-objects", "-C", str(top), *arguments],
        capture_output=True,
        text=text,
        timeout=30,
        check=False,
    )


def _repository(path):
    process = subprocess.run(
        ["git", "-C", str(path.parent), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return Path(process.stdout.strip()) if process.returncode == 0 else None


def _show(top, commit, relative):
    """Committed bytes of a file; anything but a full object id is refused, never parsed."""
    if not isinstance(commit, str) or not OID.match(commit):
        return None
    process = _git(top, "show", f"{commit}:{relative}")
    return process.stdout if process.returncode == 0 else None


def _committed(path):
    """The commit whose tree holds exactly these bytes; uncommitted edits are refused."""
    top = _repository(path)
    if top is None:
        _invalid(f"File evidence must be inside a git repository: {path.name}")
    relative = path.resolve().relative_to(top.resolve()).as_posix()
    process = _git(top, "log", "-1", "--format=%H", "--", relative, text=True)
    commit = process.stdout.strip()
    if process.returncode or not commit or _show(top, commit, relative) != path.read_bytes():
        _invalid(f"File evidence has uncommitted changes: {relative}")
    return commit


def _at_commit(path, commit, expected_digest):
    """The pinned commit still holds the bytes whose SHA-256 was recorded."""
    top = _repository(path)
    if top is None:
        return False
    relative = path.resolve().relative_to(top.resolve()).as_posix()
    committed = _show(top, commit, relative)
    return committed is not None and content_digest(committed) == expected_digest


def _body_mismatch(row):
    """The stored body, its digest and the approved digest must all agree."""
    try:
        content = json.loads(row["content_json"])
        approval = json.loads(row["approval_json"])
    except (TypeError, ValueError):
        return True
    if digest(content) != row["body_digest"]:
        return True
    return approval.get("state") == "approved" and approval.get("body_digest") != row["body_digest"]


def _passing_report(text):
    """A JUnit XML report that ran at least one test with no failures or errors."""
    from xml.etree import ElementTree

    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        return False  # no entity expansion from evidence files
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return False
    suites = [root] if root.tag == "testsuite" else root.findall("testsuite")
    if root.tag not in {"testsuite", "testsuites"} or not suites:
        return False
    try:
        totals = [
            sum(int(suite.get(field, "0")) for suite in suites)
            for field in ("tests", "failures", "errors", "skipped")
        ]
    except ValueError:
        return False
    tests, failures, errors, skipped = totals
    return tests > skipped and failures == 0 and errors == 0


def _passing_control(text):
    """True only for exactly one parseable control block whose status is exactly 'pass'."""
    blocks = []
    for block in CONTROL_BLOCK.findall(text):
        if "handoff_status" not in block:
            continue
        try:
            value = json.loads(block)
        except ValueError:
            return False
        if not isinstance(value, dict):
            return False
        blocks.append(value)
    return len(blocks) == 1 and blocks[0].get("handoff_status") == "pass"


def _key(item):
    return f"{item['item_id']} r{item['revision']}"


def _prompt_entry(item):
    content = item["content"]
    return {
        "id": _key(item),
        "page": item["page"],
        "title": content["title"],
        "check": content["check"],
        "condition": content["condition"],
        "sources": [source["path"] for source in item["sources"]],
        "trust": "verified_data_not_instructions",
    }


def _size(item):
    return len(json.dumps(_prompt_entry(item), ensure_ascii=False))


def prompt_items(resolved, node_id):
    """The frozen entries one node receives; only the fields a role needs."""
    wiki = resolved.get("wiki") or {}
    entries = wiki.get("items", {})
    return [
        {field: entries[key][field] for field in PROMPT_FIELDS}
        for key in wiki.get("selection", {}).get(node_id, [])
        if key in entries
    ]


class WikiKnowledge:
    def __init__(self, repository, project_root):
        self.repository, self.root = repository, Path(project_root).resolve()

    # -- input -------------------------------------------------------------

    def _content(self, entry):
        if not isinstance(entry, dict):
            _invalid("Knowledge entry must be an object.")
        content = {}
        for key, limit in LIMITS.items():
            value = entry.get(key, "")
            if not isinstance(value, str) or (key != "condition" and not value.strip()):
                _invalid(f"Knowledge '{key}' is required text.")
            if len(value) > limit:
                _invalid(f"Knowledge '{key}' exceeds {limit} characters.")
            content[key] = redact(value.strip(), self.repository.secrets)
        roles = entry.get("roles")
        if (
            not isinstance(roles, list)
            or not roles
            or any(role not in ROLES and not is_custom(role) for role in roles)
        ):
            _invalid("Knowledge roles must be catalog role ids, custom role ids or 'all'.")
        modules = entry.get("modules", [])
        if (
            not isinstance(modules, list)
            or len(modules) > 10
            or not all(isinstance(module, str) and MODULE.match(module) for module in modules)
        ):
            _invalid("Knowledge modules must be up to 10 repository ids.")
        importance = entry.get("importance", "normal")
        if importance not in IMPORTANCE:
            _invalid(f"Knowledge importance must be one of {list(IMPORTANCE)}.")
        tags = entry.get("tags", [])
        if (
            not isinstance(tags, list)
            or len(tags) > 10
            or not all(isinstance(tag, str) and TAG.match(tag) for tag in tags)
        ):
            _invalid("Knowledge tags must be up to 10 lowercase slugs.")
        content["roles"] = [role for role in ("all", *CATALOG) if role in roles] + sorted(
            {role for role in roles if is_custom(role)}
        )
        content["tags"] = sorted(set(tags))
        content["modules"] = sorted(set(modules))
        content["importance"] = importance
        return content

    def _source(self, value):
        if not isinstance(value, str) or not value or Path(value).is_absolute():
            _invalid("Knowledge sources are project-relative paths.")
        path = self.root / value
        current = self.root
        for part in Path(value).parts:
            current = current / part
            if current.is_symlink():
                _invalid("Knowledge sources cannot pass through symlinks.")
        path = path.resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            _invalid(f"Knowledge source is missing: {value}")
        if path.stat().st_size > MAX_SOURCE_BYTES:
            _invalid(f"Knowledge source is too large: {value}")
        return path

    def _sources(self, entry, project_id):
        """Provenance sources plus optional resolution evidence, one recorded list.

        ``source`` and ``phase_output`` records keep their SHA-256 and go stale when the
        file changes. ``file`` evidence (code or documents in a repository) is pinned to
        the commit that holds those bytes, so later code changes do not invalidate it.
        ``verification_run`` evidence names a passed verification run of the same project.
        """
        sources, evidence = entry.get("sources"), entry.get("evidence", [])
        if not isinstance(sources, list) or not isinstance(evidence, list):
            _invalid("Knowledge sources and evidence must be lists.")
        if not 0 < len(sources) <= MAX_SOURCES or len(evidence) > MAX_SOURCES:
            _invalid(
                f"Knowledge needs 1 to {MAX_SOURCES} sources and at most {MAX_SOURCES} evidence."
            )
        requested = [("source", value) for value in sources]
        for value in evidence:
            if not isinstance(value, dict) or value.get("kind") not in EVIDENCE_KINDS:
                _invalid(f"Evidence kind must be one of {sorted(EVIDENCE_KINDS)}.")
            target = value.get("id") if value["kind"] == "verification_run" else value.get("path")
            requested.append((value["kind"], target))
        recorded, seen = [], set()
        for kind, value in requested:
            if (kind, value) in seen:
                continue
            seen.add((kind, value))
            if kind == "verification_run":
                if self._passed_run(value, project_id) is None:
                    _invalid(f"Verification run {value!r} is not a passed run of this project.")
                recorded.append({"kind": kind, "id": value})
                continue
            path = self._source(value)
            if any(ch in value for ch in "`|") or any(ord(ch) < 32 for ch in value):
                _invalid("Knowledge paths cannot contain backticks, '|' or control characters.")
            record = {"kind": kind, "path": value, "digest": content_digest(path.read_bytes())}
            if kind == "file":
                record["commit"] = _committed(path)
            recorded.append(record)
        return recorded

    def _passed_run(self, run_id, project_id):
        if not isinstance(run_id, str) or not run_id:
            return None
        rows = self.repository.db.rows(
            "SELECT v.id FROM verification_runs v JOIN tasks t ON t.id=v.task_id "
            "WHERE v.id=? AND v.state='passed' AND t.project_id=?",
            (run_id, project_id),
        )
        return rows[0]["id"] if rows else None

    def _check(self, record, project_id):
        """None when the record still holds, else the reason it no longer does.

        Records come back from the database or a restored backup, so their shape is
        checked before any field reaches a filesystem path or a git argument.
        """
        if not isinstance(record, dict) or record.get("kind", "source") not in RECORD_KINDS:
            return "source_changed"
        kind = record.get("kind", "source")
        if kind == "verification_run":
            return None if self._passed_run(record.get("id"), project_id) else "source_changed"
        digest_value = record.get("digest")
        if not isinstance(digest_value, str) or not SHA256.match(digest_value):
            return "source_changed"
        try:
            path = self._source(record.get("path"))
        except DomainError:
            return "source_changed"
        if kind == "file":
            return (
                None if _at_commit(path, record.get("commit"), digest_value) else "source_changed"
            )
        if content_digest(path.read_bytes()) != digest_value:
            return "source_changed"
        return None

    def _issue_resolved(self, record, sources, depth=0):
        text = (self.root / record["path"]).read_text(encoding="utf-8", errors="replace")
        section = RESOLUTION.search(text)
        body = FENCE.sub("", section.group(1)) if section else ""
        if not RESOLVED.search(body) or UNRESOLVED.search(body):
            return False
        if not SUPERSEDED.search(body):
            return True
        # "Superseded by issue-003.md" resolves only through a recorded, resolved successor.
        folder = Path(record["path"]).parent
        successors = {(folder / name).as_posix() for name in SUCCESSOR.findall(body)}
        successors.discard(record["path"])
        return depth < 5 and any(
            other.get("kind", "source") == "source"
            and other.get("path") in successors
            and self._issue_resolved(other, sources, depth + 1)
            for other in sources
        )

    def _resolves(self, record, sources):
        kind = record.get("kind", "source")
        if kind == "file":
            return False  # where a fix lives, never proof that it works
        if kind == "verification_run":
            return True
        if kind in {"phase_output", "test_report"}:
            text = (self.root / record["path"]).read_text(encoding="utf-8", errors="replace")
            return _passing_control(text) if kind == "phase_output" else _passing_report(text)
        return self._issue_resolved(record, sources)

    def verify(self, sources, project_id):
        """Automatic provenance check (plan §4.3): unchanged records and a final resolution.

        A resolved issue, a passing phase output, a passing JUnit report or a passed
        verification run resolves an item. Code evidence (``file``) additionally needs one of
        the last three, so a committed file never stands in for a test.
        """
        if not isinstance(sources, list) or not sources:
            return {"state": "source_changed", "resolved_by": []}
        for record in sources:
            reason = self._check(record, project_id)
            if reason:
                return {"state": reason, "resolved_by": []}
        resolved_by = [
            record.get("path") or record.get("id")
            for record in sources
            if self._resolves(record, sources)
        ]
        has_code = any(record.get("kind") == "file" for record in sources)
        tested = any(
            record.get("kind") in {"phase_output", "test_report", "verification_run"}
            and (record.get("path") or record.get("id")) in resolved_by
            for record in sources
        )
        if not resolved_by or (has_code and not tested):
            return {"state": "resolution_unverified", "resolved_by": resolved_by}
        return {"state": "verified", "resolved_by": resolved_by}

    def source_state(self, sources, project_id):
        """'current' when every record still holds, else 'changed'."""
        return (
            "changed" if any(self._check(record, project_id) for record in sources) else "current"
        )

    # -- lifecycle -----------------------------------------------------------

    def _project(self, project_id):
        if not self.repository.db.rows("SELECT id FROM projects WHERE id=?", (project_id,)):
            _invalid("Unknown project.")

    def _revisions(self, sql, project_id, item_id):
        return sql.execute(
            "SELECT * FROM knowledge_revisions WHERE project_id=? AND item_id=? ORDER BY revision",
            (project_id, item_id),
        ).fetchall()

    def _insert(self, sql, project_id, item_id, revision, page, content, sources):
        # Digest exactly what is stored: secret masking happens on serialization (W-20).
        content = json.loads(self.repository._json(content))
        sql.execute(
            "INSERT INTO knowledge_revisions VALUES(?,?,?,?,?,?,?,?,?,'candidate',?)",
            (
                identifier(),
                project_id,
                item_id,
                revision,
                page,
                self.repository._json(content),
                digest(content),
                self.repository._json(sources),
                self.repository._json(PENDING),
                self.repository.clock(),
            ),
        )
        self.repository._event(
            sql,
            project_id,
            "KnowledgeRevisionProposed",
            {"item_id": item_id, "revision": revision, "body_digest": digest(content)},
        )
        return {"item_id": item_id, "revision": revision, "state": "candidate"}

    def propose(self, project_id, entries):
        """Add new items as revision 1 candidates; one transaction for the whole batch."""
        self._project(project_id)
        entries = entries if isinstance(entries, list) else [entries]
        prepared = []
        for entry in entries:
            item_id, page = entry.get("item_id", ""), entry.get("page", "")
            if not isinstance(item_id, str) or not ITEM_ID.match(item_id):
                _invalid("Knowledge item_id must look like 'L01' or 'T-PLAYWRIGHT'.")
            if page not in PAGES:
                _invalid(f"Knowledge page must be one of {sorted(PAGES)}.")
            prepared.append((item_id, page, self._content(entry), self._sources(entry, project_id)))
        if len({item[0] for item in prepared}) != len(prepared):
            _invalid("Duplicate item_id in one proposal.")
        with self.repository.db.transaction() as sql:
            results = []
            for item_id, page, content, sources in prepared:
                if self._revisions(sql, project_id, item_id):
                    _invalid(f"Knowledge item {item_id} exists; revise it instead.")
                results.append(self._insert(sql, project_id, item_id, 1, page, content, sources))
            return results

    def revise(self, project_id, item_id, entry):
        """A new candidate revision; approval never carries over from an earlier body."""
        self._project(project_id)
        content, sources = self._content(entry), self._sources(entry, project_id)
        with self.repository.db.transaction() as sql:
            rows = self._revisions(sql, project_id, item_id)
            if not rows:
                _invalid(f"Unknown knowledge item {item_id}.")
            page = entry.get("page") or rows[-1]["page"]
            if page not in PAGES:
                _invalid(f"Knowledge page must be one of {sorted(PAGES)}.")
            sql.execute(
                "UPDATE knowledge_revisions SET state='superseded' WHERE project_id=? AND item_id=? AND state='candidate'",
                (project_id, item_id),
            )
            return self._insert(
                sql, project_id, item_id, rows[-1]["revision"] + 1, page, content, sources
            )

    def _candidate(self, sql, project_id, item_id, revision):
        row = sql.execute(
            "SELECT * FROM knowledge_revisions WHERE project_id=? AND item_id=? AND revision=?",
            (project_id, item_id, revision),
        ).fetchone()
        if row is None:
            _invalid(f"Unknown knowledge revision {item_id} r{revision}.")
        if row["state"] != "candidate":
            raise DomainError(
                ErrorCode.STALE_VERSION,
                f"{item_id} r{revision} is {row['state']}, not a candidate.",
            )
        return row

    def approve(self, project_id, item_id, revision, actor):
        if not actor:
            raise DomainError(ErrorCode.APPROVAL_REQUIRED, "Explicit approver required.")
        with self.repository.db.transaction() as sql:
            row = self._candidate(sql, project_id, item_id, revision)
            if _body_mismatch(row):
                raise DomainError(
                    ErrorCode.STALE_VERSION,
                    f"{item_id} r{revision} stored body differs from its digest; revise it.",
                )
            verification = dict(
                self.verify(json.loads(row["sources_json"]), project_id),
                checked_at=self.repository.clock(),
            )
            if verification["state"] == "source_changed":
                raise DomainError(
                    ErrorCode.STALE_VERSION,
                    f"{item_id} r{revision} sources changed since proposal; revise it.",
                )
            if verification["state"] != "verified":
                raise DomainError(
                    ErrorCode.VERIFICATION_FAILED,
                    f"{item_id} r{revision} has no final resolution evidence (resolved issue, "
                    "passing phase output or committed file); revise it with evidence.",
                )
            approval = {
                "state": "approved",
                "actor": actor,
                "at": self.repository.clock(),
                "body_digest": row["body_digest"],
                "verification": verification,
            }
            sql.execute(
                "UPDATE knowledge_revisions SET state='superseded' WHERE project_id=? AND item_id=? AND state='active'",
                (project_id, item_id),
            )
            sql.execute(
                "UPDATE knowledge_revisions SET state='active',approval_json=? WHERE id=?",
                (self.repository._json(approval), row["id"]),
            )
            self.repository._event(
                sql,
                project_id,
                "KnowledgeRevisionApproved",
                {"item_id": item_id, "revision": revision, "actor": actor},
            )
        return {"item_id": item_id, "revision": revision, "state": "active"}

    def reject(self, project_id, item_id, revision, actor):
        with self.repository.db.transaction() as sql:
            row = self._candidate(sql, project_id, item_id, revision)
            approval = {"state": "rejected", "actor": actor, "at": self.repository.clock()}
            sql.execute(
                "UPDATE knowledge_revisions SET state='rejected',approval_json=? WHERE id=?",
                (self.repository._json(approval), row["id"]),
            )
            self.repository._event(
                sql,
                project_id,
                "KnowledgeRevisionRejected",
                {"item_id": item_id, "revision": revision, "actor": actor},
            )
        return {"item_id": item_id, "revision": revision, "state": "rejected"}

    def retire(self, project_id, item_id, reason, actor):
        if not reason or not reason.strip():
            _invalid("A retirement reason is required.")
        with self.repository.db.transaction() as sql:
            row = sql.execute(
                "SELECT * FROM knowledge_revisions WHERE project_id=? AND item_id=? AND state='active'",
                (project_id, item_id),
            ).fetchone()
            if row is None:
                _invalid(f"Knowledge item {item_id} has no active revision.")
            approval = dict(
                json.loads(row["approval_json"]),
                retired_by=actor,
                retired_at=self.repository.clock(),
                retired_reason=redact(reason.strip()[:300], self.repository.secrets),
            )
            sql.execute(
                "UPDATE knowledge_revisions SET state='retired',approval_json=? WHERE id=?",
                (self.repository._json(approval), row["id"]),
            )
            self.repository._event(
                sql,
                project_id,
                "KnowledgeRevisionRetired",
                {"item_id": item_id, "revision": row["revision"], "actor": actor},
            )
        return {"item_id": item_id, "revision": row["revision"], "state": "retired"}

    # -- reads -----------------------------------------------------------------

    def items(self, project_id, *, states=None):
        rows = self.repository.db.rows(
            "SELECT * FROM knowledge_revisions WHERE project_id=? ORDER BY item_id,revision",
            (project_id,),
        )
        result = []
        for row in rows:
            if states and row["state"] not in states:
                continue
            sources = json.loads(row["sources_json"])
            verification = (
                {"state": "body_mismatch", "resolved_by": []}
                if _body_mismatch(row)
                else self.verify(sources, project_id)
            )
            result.append(
                {
                    "item_id": row["item_id"],
                    "revision": row["revision"],
                    "page": row["page"],
                    "state": row["state"],
                    "content": json.loads(row["content_json"]),
                    "body_digest": row["body_digest"],
                    "sources": sources,
                    "source_state": "changed"
                    if verification["state"] == "source_changed"
                    else "current",
                    "verification": verification,
                    "approval": json.loads(row["approval_json"]),
                }
            )
        return result

    def active(self, project_id):
        """Approved revisions that still pass the provenance check: the only ones served."""
        return [
            item
            for item in self.items(project_id, states={"active"})
            if item["verification"]["state"] == "verified"
        ]

    def inactive(self, project_id):
        """Why each revision that is not superseded is not served (WIKI-AC-03)."""
        reasons = {"candidate": "not_approved", "rejected": "rejected", "retired": "retired"}
        result = []
        for item in self.items(project_id):
            if item["state"] == "superseded":
                continue
            if item["state"] == "active":
                state = item["verification"]["state"]
                if state != "verified":
                    result.append({"id": _key(item), "reason": state})
            else:
                result.append(
                    {"id": _key(item), "reason": reasons.get(item["state"], item["state"])}
                )
        return result

    # -- snapshot bundle -----------------------------------------------------------

    def bundle(self, project_id, graph, task_text="", module=None):
        """Per-node selection frozen into the task snapshot; retries never re-select.

        A node's module is its own ``module`` or the task's execution module. Items limited
        to other modules are skipped and recorded. Ranking: role-specific before 'all',
        module-specific before generic, importance, tags found in the task text, item id.
        Items over a budget are skipped whole and recorded.
        """
        items = {_key(item): item for item in self.active(project_id)}
        text = (task_text or "").lower()
        selection, excluded = {}, {}
        ranked = {}
        for node in graph.get("nodes", []):
            if node.get("kind") != "role":
                continue
            role = node.get("role_id") or node["id"]
            scope = node.get("module") or module

            def rank(item, role=role, scope=scope):
                content = item["content"]
                return (
                    0 if role in content["roles"] else 1,
                    0 if scope and scope in content.get("modules", []) else 1,
                    IMPORTANCE.index(content.get("importance", "normal")),
                    -sum(1 for tag in content["tags"] if tag in text),
                    item["item_id"],
                )

            matched, other = [], []
            for item in items.values():
                content = item["content"]
                if role not in content["roles"] and "all" not in content["roles"]:
                    continue
                modules = content.get("modules", [])
                if scope and modules and scope not in modules:
                    other.append({"id": _key(item), "reason": "other_module"})
                else:
                    matched.append(item)
            ranked[node["id"]] = ([_key(item) for item in sorted(matched, key=rank)], other)
        for node_id, (keys, other) in ranked.items():
            chosen, chars, skipped = [], 0, list(other)
            for key in keys:
                size = _size(items[key])
                if len(chosen) >= POLICY["role_items"]:
                    skipped.append({"id": key, "reason": "role_item_limit"})
                elif chars + size > POLICY["role_chars"]:
                    skipped.append({"id": key, "reason": "role_char_limit"})
                else:
                    chosen.append(key)
                    chars += size
            selection[node_id], excluded[node_id] = chosen, skipped
        # Session members share one prompt: the union of their items has its own budget.
        sessions = {}
        for node in graph.get("nodes", []):
            if node.get("session") and node["id"] in selection:
                sessions.setdefault(node["session"], []).append(node["id"])
        for members in sessions.values():
            seen, chars = set(), 0
            for node_id in members:
                kept = []
                for key in selection[node_id]:
                    size = 0 if key in seen else _size(items[key])
                    if chars + size > POLICY["session_chars"]:
                        excluded[node_id].append({"id": key, "reason": "session_char_limit"})
                        continue
                    kept.append(key)
                    if key not in seen:
                        seen.add(key)
                        chars += size
                selection[node_id] = kept
        used = {key for keys in selection.values() for key in keys}
        return {
            "project_id": project_id,
            "policy": POLICY,
            # Secrets registered after approval are masked on the way out (W-18).
            "items": redact(
                {
                    key: {
                        **_prompt_entry(item),
                        "body_digest": item["body_digest"],
                        "source_digests": item["sources"],
                        "approval": item["approval"],
                    }
                    for key, item in sorted(items.items())
                    if key in used
                },
                self.repository.secrets,
            ),
            "selection": selection,
            "excluded": {node: rows for node, rows in excluded.items() if rows},
            "inactive": self.inactive(project_id),
        }

    # -- Markdown view -----------------------------------------------------------

    def workspace_project(self):
        """The project this workspace's docs/wiki belongs to (same rule as project setup)."""
        from agent_dev.adapters.storage_registry import workspace_locations

        marker = workspace_locations(self.root).config_root / "workspace.json"
        if marker.is_file():
            try:
                value = json.loads(marker.read_text(encoding="utf-8")).get("project_id")
            except (OSError, ValueError, AttributeError):
                value = None
            if isinstance(value, str) and value:
                return value
        if self.repository.db.rows(
            "SELECT name FROM sqlite_master WHERE name='workspace_settings'"
        ):
            rows = self.repository.db.rows(
                "SELECT settings_json FROM workspace_settings WHERE id=1"
            )
            if rows:
                value = json.loads(rows[0]["settings_json"]).get("project_id")
                if isinstance(value, str) and value:
                    return value
        return slugify(self.root.name)

    def render_dir(self, project_id) -> Path:
        """Where the Markdown view goes: the profile's ``wiki_dir`` or ``docs/wiki``."""
        rows = self.repository.db.rows(
            "SELECT content_json FROM project_profile_versions WHERE project_id=? "
            "ORDER BY version DESC LIMIT 1",
            (project_id,),
        )
        value = json.loads(rows[0]["content_json"]).get("wiki_dir") if rows else None
        return wiki_dir(value) if value else WIKI_DIR

    def render(self, project_id):
        self._project(project_id)
        owner = self.workspace_project()
        if project_id != owner:
            raise DomainError(
                ErrorCode.POLICY_VIOLATION,
                f"docs/wiki belongs to project '{owner}'; render another project from its own workspace.",
            )
        relative = self.render_dir(project_id)
        target = self.root / relative
        current = self.root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise DomainError(ErrorCode.POLICY_VIOLATION, "Wiki directory cannot be a symlink.")
        active = self.items(project_id, states={"active"})
        served = [item for item in active if item["verification"]["state"] == "verified"]
        stale = [item for item in active if item["verification"]["state"] != "verified"]
        pages = {
            name: redact(text, self.repository.secrets)
            for name, text in {
                "index.md": render_index(served, stale),
                **{f"{page}.md": render_page(page, served) for page in PAGES},
            }.items()
        }
        target.mkdir(parents=True, exist_ok=True)
        for name, text in pages.items():
            _write_atomic(target / name, text)
        return {
            "directory": relative.as_posix(),
            "pages": sorted(pages),
            "active": len(served),
            "stale": [item["item_id"] for item in stale],
        }


def wiki_dir(value) -> Path:
    """A project-relative folder for the wiki view; never absolute or outside the project."""
    from pathlib import PurePosixPath

    if not isinstance(value, str) or not value.strip() or len(value) > 300 or "\\" in value:
        raise DomainError(ErrorCode.INVALID_INPUT, "위키 폴더는 프로젝트 기준 상대 경로로 입력해주세요.")
    path = PurePosixPath(value.strip().rstrip("/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise DomainError(ErrorCode.INVALID_INPUT, "위키 폴더는 프로젝트 안의 상대 경로여야 합니다.")
    return Path(*path.parts)


HEADER = (
    "<!-- DevRoster가 승인된 지식에서 생성한 파일이다. 직접 수정하지 않는다. "
    "수정은 `devroster knowledge revise` 후 `approve`, 다시 `render`. -->\n"
)


def _roles(roles):
    return ", ".join("전체" if role == "all" else LABELS.get(role) or role for role in roles)


def _cell(text):
    return " ".join(str(text).split()).replace("|", "\\|")


STALE_REASONS = {
    "source_changed": "근거 파일이 승인 이후 바뀜",
    "resolution_unverified": "최종 해결 증거 없음",
    "body_mismatch": "저장된 본문이 승인 해시와 다름",
}


def _source_cell(source):
    path = _cell(source["path"]).replace("`", "")
    if source.get("kind") == "file":
        return f"`{path}`@{source.get('commit', '')[:12]}"
    return f"`{path}`"


def render_index(served, stale):
    lines = [HEADER, "# 프로젝트 위키", ""]
    lines += ["| 페이지 | 내용 | 활성 항목 |", "|---|---|---:|"]
    for page, title in PAGES.items():
        count = sum(1 for item in served if item["page"] == page)
        lines.append(f"| [{page}.md]({page}.md) | {title} | {count} |")
    if stale:
        lines += [
            "",
            "## 재검증 필요",
            "",
            (
                "근거 검사를 통과하지 못해 역할에게 제공하지 않는 승인 항목이다. "
                "근거나 해결 증거를 보완해 새 revision으로 다시 승인한다."
            ),
            "",
        ]
        lines += [
            f"- {item['item_id']} r{item['revision']}: "
            f"{STALE_REASONS.get(item['verification']['state'], item['verification']['state'])}"
            for item in stale
        ]
    return "\n".join(lines) + "\n"


def render_page(page, served):
    lines = [HEADER, f"# {PAGES[page]}", ""]
    items = [item for item in served if item["page"] == page]
    if not items:
        return "\n".join([*lines, "활성 항목 없음."]) + "\n"
    lines += ["| ID | 대상 역할 | 체크 항목 | 적용 조건 | 근거 |", "|---|---|---|---|---|"]
    for item in items:
        content = item["content"]
        sources = ", ".join(_source_cell(source) for source in item["sources"])
        lines.append(
            f"| {item['item_id']} r{item['revision']} | {_roles(content['roles'])} | "
            f"**{_cell(content['title'])}** {_cell(content['check'])} | "
            f"{_cell(content['condition']) or '-'} | {sources} |"
        )
    return "\n".join(lines) + "\n"


def _write_atomic(path, text):
    handle, temporary = tempfile.mkstemp(prefix="." + path.name, dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
