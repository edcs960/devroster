"""Current-evidence code review of a task's registered worktrees (RV-01).

Diffs come only from the task's frozen snapshot bindings, inside the approved
allowed paths, through bounded Git calls that never run external diff or
textconv drivers. Comments are anchored to lines the server itself observed in
the current diff; code decisions bind to the current diff digest and to an
actual current verification proof. Nothing here creates canonical approvals,
moves task state or relaxes publish gates.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import NoReturn

from agent_dev.adapters.bounded_process import run_bounded
from agent_dev.adapters.source_explorer import forbidden
from agent_dev.adapters.state.sqlite.store import WorkflowRepository, identifier
from agent_dev.adapters.worktrees import WorktreeManager
from agent_dev.domain.content import content_digest, digest, redact
from agent_dev.domain.errors import DomainError, ErrorCode

MAX_FILES = 100
MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 1024 * 1024
MAX_NAME_BYTES = 256 * 1024
MAX_BODY = 4000
STAGES = ("committed", "staged", "unstaged", "untracked")
STAGE_LABELS = {
    "committed": "커밋됨",
    "staged": "스테이징",
    "unstaged": "작업 중",
    "untracked": "새 파일",
}
_HUNK = re.compile(rb"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_GIT_OPTIONS = (
    "--no-pager",
    "--literal-pathspecs",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.quotepath=false",
    "-c",
    "color.ui=never",
    "-c",
    "diff.noprefix=false",
    "-c",
    "diff.mnemonicPrefix=false",
    "-c",
    "diff.relative=false",
)
_DIFF = ("--no-ext-diff", "--no-textconv", "--no-color", "-M")


def fail(message, code=ErrorCode.INVALID_INPUT) -> NoReturn:
    raise DomainError(code, message)


def _bounded(root, arguments, limit, timeout=30):
    """Run git and stop reading once ``limit`` bytes arrive (never buffers more)."""
    env = {k: v for k, v in os.environ.items() if k not in {"GIT_EXTERNAL_DIFF", "GIT_PAGER"}}
    try:
        result = run_bounded(
            ["git", *_GIT_OPTIONS, "-C", str(root), *arguments],
            cwd=root, env=env, timeout=timeout, limit=limit, stderr=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        fail("Git 변경 내용 조회 제한시간을 넘었습니다. 다시 시도해주세요.", ErrorCode.POLICY_VIOLATION)
    if result.returncode and not result.truncated:
        fail("Git 변경 내용을 읽지 못했습니다. 작업 저장소 상태를 확인해주세요.", ErrorCode.POLICY_VIOLATION)
    return result.output, result.truncated


def _decode(raw: bytes):
    try:
        return raw.decode("utf-8"), True
    except UnicodeDecodeError:
        return "", False


def _in_scope(path: str, allowed) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/") for p in allowed)


def _private(root: Path, path: str) -> bool:
    if any(forbidden(part) for part in PurePosixPath(path).parts):
        return True
    candidate = root
    for part in PurePosixPath(path).parts:
        candidate = candidate / part
        if candidate.is_symlink():
            return True
    return False


def _name_status(raw: bytes):
    fields = raw.split(b"\0")
    index = 0
    while index < len(fields) and fields[index]:
        status = fields[index].decode("ascii", "replace")
        if status[:1] in {"R", "C"}:
            yield status[:1], fields[index + 1], fields[index + 2]
            index += 3
        else:
            yield status[:1], None, fields[index + 1]
            index += 2


class ReviewContext:
    """Frozen task bindings plus the workspace secrets used for redaction."""

    def __init__(self, db, task_id, secrets=()):
        if not isinstance(task_id, str) or not task_id or len(task_id) > 200:
            fail("작업 ID를 확인해주세요.")
        self.db, self.task_id, self.secrets = db, task_id, tuple(secrets)
        self.store = WorkflowRepository(db, secrets=self.secrets)
        rows = db.rows("SELECT id,title,state,snapshot_id FROM tasks WHERE id=?", (task_id,))
        if not rows:
            fail("등록되지 않은 작업입니다.")
        self.task = rows[0]
        if not self.task["snapshot_id"]:
            fail("승인된 실행 스냅샷이 없는 작업입니다. 설계·계획 승인 후 검토할 수 있습니다.")
        self.snapshot = self.store.snapshot(task_id)
        self.resolved = self.snapshot["resolved"]
        self.bindings = {
            name: binding
            for name, binding in self.resolved.get("repositories", {}).items()
            if isinstance(binding, dict) and binding.get("worktree")
        }

    def binding(self, module):
        if module not in self.bindings:
            fail("작업에 등록된 모듈이 아닙니다.")
        binding = self.bindings[module]
        if not WorktreeManager.validate(binding):
            fail("작업 저장소가 등록된 브랜치와 다릅니다.", ErrorCode.POLICY_VIOLATION)
        allowed = binding.get("allowed_paths") or self.resolved.get("allowed_paths") or []
        return Path(binding["worktree"]).resolve(), binding, allowed

    def public(self, text: str) -> str:
        return redact(text, self.secrets)


def _entries(root, binding):
    base = binding.get("base_commit")
    sources = []
    if base:
        sources.append(("committed", ["diff", *_DIFF, "--name-status", "-z", f"{base}..HEAD", "--"]))
    sources += [
        ("staged", ["diff", *_DIFF, "--cached", "--name-status", "-z", "--"]),
        ("unstaged", ["diff", *_DIFF, "--name-status", "-z", "--"]),
    ]
    budget, truncated = MAX_NAME_BYTES, False
    for stage, arguments in sources:
        raw, cut = _bounded(root, arguments, budget)
        budget -= len(raw)
        truncated |= cut
        for status, old, new in _name_status(raw):
            yield stage, status, old, new
        if cut or budget <= 0:
            break
    else:
        raw, cut = _bounded(root, ["ls-files", "-z", "--others", "--exclude-standard", "--"], budget)
        truncated |= cut
        for name in raw.split(b"\0"):
            if name:
                yield "untracked", "A", None, name
    if truncated:
        yield None, None, None, None


def _patch(root, binding, entry):
    stage, path = entry["stage"], entry["path"]
    paths = [entry["old_path"], path] if entry.get("old_path") else [path]
    if stage == "untracked":
        full = root / path
        try:
            fd = os.open(full, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return b"", False
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            return data[:MAX_FILE_BYTES], True
        if b"\0" in data[:8000]:
            return b"Binary file added\n", False
        lines = data.splitlines(keepends=True) or [b""]
        header = f"@@ -0,0 +1,{len(lines)} @@\n".encode()
        body = b"".join(b"+" + line if line.endswith(b"\n") else b"+" + line + b"\n" for line in lines)
        return header + body, False
    rangearg = {
        "committed": [f"{binding['base_commit']}..HEAD"],
        "staged": ["--cached"],
        "unstaged": [],
    }[stage]
    return _bounded(root, ["diff", *_DIFF, "-U3", *rangearg, "--", *paths], MAX_FILE_BYTES)


def _hunks(patch: bytes):
    hunks, current, old_line, new_line = [], None, 0, 0
    for raw in patch.split(b"\n"):
        match = _HUNK.match(raw)
        if match:
            old_line, new_line = int(match.group(1)), int(match.group(3))
            current = {"header": raw.decode("utf-8", "replace"), "lines": []}
            hunks.append(current)
            continue
        if current is None or not raw[:1] in {b"+", b"-", b" "}:
            continue
        text = raw[1:].decode("utf-8", "replace")
        if raw[:1] == b"+":
            current["lines"].append({"kind": "add", "new": new_line, "text": text})
            new_line += 1
        elif raw[:1] == b"-":
            current["lines"].append({"kind": "del", "old": old_line, "text": text})
            old_line += 1
        else:
            current["lines"].append({"kind": "ctx", "old": old_line, "new": new_line, "text": text})
            old_line += 1
            new_line += 1
    return hunks


def collect(context: ReviewContext, include_patches=False):
    """Bounded, scoped file list for every module plus the overall diff digest."""
    modules, total, complete = [], 0, True
    for module in sorted(context.bindings):
        root, binding, allowed = context.binding(module)
        files, hidden = [], {"out_of_scope": 0, "private": 0, "unsupported": 0}
        for stage, status, old_raw, new_raw in _entries(root, binding):
            if stage is None or new_raw is None:
                complete = False
                continue
            path, ok = _decode(new_raw)
            old_path, old_ok = _decode(old_raw) if old_raw else (None, True)
            if not ok or not old_ok:
                hidden["unsupported"] += 1
                continue
            if not _in_scope(path, allowed) or old_path and not _in_scope(old_path, allowed):
                hidden["out_of_scope"] += 1
                continue
            if _private(root, path) or old_path and _private(root, old_path):
                hidden["private"] += 1
                continue
            if sum(len(m["files"]) for m in modules) + len(files) >= MAX_FILES:
                complete = False
                continue
            entry = {"stage": stage, "status": status, "path": path, "old_path": old_path}
            patch, cut = _patch(root, binding, entry)
            if total + len(patch) > MAX_TOTAL_BYTES:
                patch, cut = patch[: max(0, MAX_TOTAL_BYTES - total)], True
            total += len(patch)
            binary = patch.startswith(b"Binary file") or b"\nBinary files " in b"\n" + patch
            entry.update(
                digest=content_digest(patch),
                binary=binary,
                truncated=cut,
                size=len(patch),
            )
            complete &= not cut
            if include_patches:
                entry["_patch"] = patch
            files.append(entry)
        modules.append(
            {
                "id": module,
                "branch": binding.get("branch", ""),
                "base_commit": binding.get("base_commit", ""),
                "files": files,
                "hidden": hidden,
            }
        )
    summary = [
        [m["id"], m["base_commit"], [[f["stage"], f["path"], f["old_path"], f["digest"]] for f in m["files"]]]
        for m in modules
    ]
    return {"modules": modules, "complete": complete, "digest": digest({"modules": summary, "complete": complete})}


def _find(collected, module, stage, path):
    for item in collected["modules"]:
        if item["id"] == module:
            for entry in item["files"]:
                if entry["stage"] == stage and entry["path"] == path:
                    return entry
    return None


def file_diff(context: ReviewContext, module, stage, path):
    if stage not in STAGES:
        fail("변경 단계를 확인해주세요.")
    collected = collect(context, include_patches=True)
    entry = _find(collected, module, stage, path)
    if entry is None:
        fail("현재 변경 목록에 없는 파일입니다. 목록을 새로고침해주세요.", ErrorCode.STALE_VERSION)
    patch = entry.pop("_patch")
    hunks = [] if entry["binary"] else _hunks(patch)
    for hunk in hunks:
        hunk["header"] = context.public(hunk["header"])
        for line in hunk["lines"]:
            line["text"] = context.public(line["text"])
    return dict(_public_entry(context, entry), module=module, hunks=hunks, review_digest=collected["digest"])


def _public_entry(context, entry):
    return {
        key: (context.public(value) if isinstance(value, str) and key in {"path", "old_path"} else value)
        for key, value in entry.items()
        if not key.startswith("_")
    }


def _anchor_exists(diff, side, line):
    for hunk in diff["hunks"]:
        for item in hunk["lines"]:
            if side == "new" and item.get("new") == line and item["kind"] in {"add", "ctx"}:
                return item
            if side == "old" and item.get("old") == line and item["kind"] in {"del", "ctx"}:
                return item
    return None


def _threads(context, collected):
    rows = context.db.rows(
        "SELECT * FROM review_threads WHERE task_id=? AND kind='diff' ORDER BY created_at,id",
        (context.task_id,),
    )
    result = []
    for row in rows:
        anchor = json.loads(row["anchor_json"])
        entry = _find(collected, anchor.get("module"), anchor.get("stage"), anchor.get("path"))
        current = entry is not None and entry["digest"] == row["evidence_digest"]
        state = row["state"]
        result.append(
            {
                "id": row["id"],
                "anchor": anchor,
                "body": row["body"],
                "resolution": row["resolution"],
                "state": state,
                "display_state": "stale" if state == "open" and not current else state,
                "current": current,
                "version": row["version"],
                "created_at": row["created_at"],
            }
        )
    return result


def verification_state(context: ReviewContext, conn=None):
    """Latest verification proven against the current source, or a reason it is not."""
    if conn is not None:
        return _verification_state(context, conn)
    own = context.db.connect()
    try:
        return _verification_state(context, own)
    finally:
        own.close()


def _verification_state(context, conn):
    from agent_dev.adapters.traceability import _artifact_report
    from agent_dev.adapters.worktrees import commit_fingerprint, evidence_digest

    run = conn.execute(
        "SELECT * FROM verification_runs WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
        (context.task_id,),
    ).fetchone()
    if run is None:
        return {"current": False, "state": "none", "reason": "아직 검증을 실행하지 않았습니다."}
    gates = [
        {"gate": g["gate"], "passed": bool(g["passed"]), "required": bool(g["required"]), "exit_code": g["exit_code"]}
        for g in conn.execute(
            "SELECT gate,passed,required,exit_code FROM verification_results WHERE run_id=? ORDER BY gate",
            (run["id"],),
        ).fetchall()
    ]
    base = {"run_id": run["id"], "state": run["state"], "created_at": run["created_at"], "gates": gates}
    if run["state"] != "passed":
        return dict(base, current=False, reason="최신 검증 실행이 통과하지 않았습니다.")
    try:
        source = evidence_digest(context.bindings)
        revisions = commit_fingerprint(context.bindings)
    except (DomainError, OSError, subprocess.SubprocessError):
        return dict(base, current=False, reason="현재 소스 근거를 확인할 수 없습니다.")
    commands = context.resolved.get("verification", {}).get("commands", [])
    expected = digest({"commands": commands, "source": source})
    if expected != run["policy_digest"]:
        return dict(base, current=False, reason="검증 이후 소스 또는 검증 정책이 바뀌었습니다. 다시 검증해주세요.")
    report = _artifact_report(conn, context.db, context.task_id, run["id"])
    if (
        not report
        or not report.get("passed")
        or report.get("snapshot_digest") != context.snapshot["digest"]
        or report.get("source_digest") != source
        or report.get("source_commits") != revisions
        or report.get("policy_digest") != expected
    ):
        return dict(base, current=False, reason="검증 보고서가 현재 스냅샷·소스와 일치하지 않습니다.")
    return dict(base, current=True, reason="현재 소스에 대한 최신 검증 통과 기록입니다.")


def _decisions(context, collected):
    rows = context.db.rows(
        "SELECT * FROM review_decisions WHERE task_id=? AND kind='code' ORDER BY created_at DESC",
        (context.task_id,),
    )
    return [dict(row, current=row["evidence_digest"] == collected["digest"]) for row in rows]


def _publish(context):
    rows = context.db.rows(
        "SELECT repository,branch,commit_sha,pr_number,ci_state,merge_state FROM publish_records WHERE task_id=?",
        (context.task_id,),
    )
    return [
        dict(row, note="저장된 기록입니다. 원격 CI 최신 상태는 연결된 GitHub에서 명시적으로 다시 확인해야 합니다.")
        for row in rows
    ]


def bootstrap(context: ReviewContext):
    collected = collect(context)
    return {
        "task": {"id": context.task["id"], "title": context.public(context.task["title"]), "state": context.task["state"]},
        "modules": [
            dict(m, files=[_public_entry(context, f) for f in m["files"]]) for m in collected["modules"]
        ],
        "complete": collected["complete"],
        "review_digest": collected["digest"],
        "limits": {"files": MAX_FILES, "file_bytes": MAX_FILE_BYTES, "total_bytes": MAX_TOTAL_BYTES},
        "stages": STAGE_LABELS,
        "threads": _threads(context, collected),
        "decisions": _decisions(context, collected),
    }


def evidence(context: ReviewContext):
    collected = collect(context)
    return {
        "review_digest": collected["digest"],
        "complete": collected["complete"],
        "verification": verification_state(context),
        "publish": _publish(context),
        "decisions": _decisions(context, collected),
    }


def _text(value, label, limit=MAX_BODY, required=True):
    if not isinstance(value, str) or len(value) > limit or required and not value.strip():
        fail(f"{label}을 1–{limit}자로 입력해주세요.")
    return value.strip()


def _actor(payload):
    actor = payload.get("actor", "local-user")
    if not isinstance(actor, str) or not re.fullmatch(r"[A-Za-z0-9가-힣._@-]{1,80}", actor):
        fail("작성자 이름을 확인해주세요.")
    return actor


def add_comment(context: ReviewContext, payload):
    module, stage, path = payload.get("module"), payload.get("stage"), payload.get("path")
    side, line = payload.get("side"), payload.get("line")
    if side not in {"old", "new"} or type(line) is not int or line < 1:
        fail("코멘트 위치를 확인해주세요.")
    body = context.public(_text(payload.get("body"), "코멘트"))
    diff = file_diff(context, module, stage, path)
    if payload.get("review_digest") != diff["review_digest"]:
        fail("변경 내용이 바뀌었습니다. 새로고침한 뒤 다시 작성해주세요.", ErrorCode.STALE_VERSION)
    item = _anchor_exists(diff, side, line)
    if item is None:
        fail("현재 변경 내용에 없는 줄입니다.")
    anchor = {
        "module": module,
        "stage": stage,
        "path": path,
        "old_path": diff.get("old_path"),
        "side": side,
        "line": line,
        "line_digest": content_digest(item["text"].encode()),
    }
    thread_id = identifier()
    with context.db.transaction() as conn:
        conn.execute(
            "INSERT INTO review_threads(id,task_id,kind,module_id,anchor_json,evidence_digest,state,body,resolution,version,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (thread_id, context.task_id, "diff", module, json.dumps(anchor, ensure_ascii=False), diff["digest"], "open", body, "", 0, context.store.clock()),
        )
        context.store._event(conn, context.task_id, "ReviewCommentAdded", {"thread_id": thread_id, "anchor": anchor})
    return {"thread_id": thread_id, "state": "open", "version": 0}


def set_comment_state(context: ReviewContext, payload):
    thread_id, state, expected = payload.get("thread_id"), payload.get("state"), payload.get("expected_version")
    if state not in {"open", "resolved"} or type(expected) is not int:
        fail("코멘트 상태와 버전을 확인해주세요.")
    resolution = context.public(_text(payload.get("resolution", ""), "처리 내용", required=state == "resolved"))
    with context.db.transaction() as conn:
        row = conn.execute(
            "SELECT * FROM review_threads WHERE id=? AND task_id=?", (thread_id, context.task_id)
        ).fetchone()
        if row is None:
            fail("이 작업의 코멘트가 아닙니다.")
        if row["version"] != expected:
            fail("다른 화면에서 코멘트가 바뀌었습니다. 새로고침해주세요.", ErrorCode.STALE_VERSION)
        conn.execute(
            "UPDATE review_threads SET state=?,resolution=?,version=? WHERE id=?",
            (state, resolution, expected + 1, thread_id),
        )
        context.store._event(conn, context.task_id, "ReviewThreadUpdated", {"thread_id": thread_id, "state": state, "version": expected + 1})
    return {"thread_id": thread_id, "state": state, "version": expected + 1}


def decide(context: ReviewContext, payload):
    decision = payload.get("decision")
    if decision not in {"approved", "rejected"}:
        fail("검토 결과를 선택해주세요.")
    note = context.public(_text(payload.get("note", ""), "의견", required=decision == "rejected"))
    actor = _actor(payload)
    collected = collect(context)
    if payload.get("review_digest") != collected["digest"]:
        fail("변경 내용이 바뀌었습니다. 최신 변경을 확인한 뒤 결정해주세요.", ErrorCode.STALE_VERSION)
    # Hashing every source file is slow; prove it before taking the write lock and only
    # confirm inside the transaction that no newer verification run appeared meanwhile.
    proof = verification_state(context) if decision == "approved" else None
    with context.db.transaction() as conn:
        context.store.assert_change_allowed(context.task_id, conn)
        if decision == "approved":
            assert proof is not None
            if not collected["complete"]:
                fail("변경 내용이 검토 한도를 넘어 일부만 표시됩니다. 범위를 줄인 뒤 승인할 수 있습니다.", ErrorCode.POLICY_VIOLATION)
            open_count = conn.execute(
                "SELECT COUNT(*) FROM review_threads WHERE task_id=? AND kind='diff' AND state='open'",
                (context.task_id,),
            ).fetchone()[0]
            if open_count:
                fail(f"처리하지 않은 코멘트 {open_count}개가 있습니다.", ErrorCode.POLICY_VIOLATION)
            if not proof["current"]:
                fail(f"현재 변경에 대한 검증 통과 기록이 필요합니다: {proof['reason']}", ErrorCode.POLICY_VIOLATION)
            latest = conn.execute(
                "SELECT id FROM verification_runs WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
                (context.task_id,),
            ).fetchone()
            if latest is None or latest[0] != proof["run_id"]:
                fail("결정하는 동안 검증이 다시 실행되었습니다. 최신 결과를 확인해주세요.", ErrorCode.STALE_VERSION)
        exists = conn.execute(
            "SELECT 1 FROM review_decisions WHERE task_id=? AND kind='code' AND evidence_digest=? AND actor=?",
            (context.task_id, collected["digest"], actor),
        ).fetchone()
        if exists:
            fail("같은 변경 내용에 이미 결정을 남겼습니다.", ErrorCode.STALE_VERSION)
        decision_id = identifier()
        conn.execute(
            "INSERT INTO review_decisions(id,task_id,evidence_digest,kind,actor,decision,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (decision_id, context.task_id, collected["digest"], "code", actor, decision, note, context.store.clock()),
        )
        context.store._event(
            conn,
            context.task_id,
            "ReviewDecisionRecorded",
            {"decision_id": decision_id, "kind": "code", "decision": decision, "evidence_digest": collected["digest"]},
        )
    return {"decision_id": decision_id, "decision": decision, "review_digest": collected["digest"]}
