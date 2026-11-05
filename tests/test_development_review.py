"""Current-evidence code review (RV-01) over real Git worktrees and HTTP."""

import hashlib
import json
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.domain.contracts import PhaseResult
from agent_dev.ui.local_web import make_server
from agent_dev.ui.review_server import ReviewWeb

SECRET = "rv01-synthetic-secret-value-9f3a"


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, text=True, capture_output=True
    ).stdout.strip()


def commit(root, message):
    git(root, "add", "-A")
    git(root, "-c", "user.name=T", "-c", "user.email=t@example.test", "commit", "-qm", message)


class Runner:
    """Writes the task's changes into its registered worktree during run-once."""

    def __init__(self, change):
        self.change = change

    def execute(self, request):
        root = Path(json.loads(request.context)["repositories"]["source"]["worktree"])
        self.change(root)
        return PhaseResult('```json\n{"handoff_status":"pass"}\n```')


def baseline_change(root):
    (root / "src").mkdir(exist_ok=True)
    (root / "src/app.py").write_text("print('one')\nprint('two')\nprint('three')\n")
    commit(root, "committed change")
    (root / "src/staged.py").write_text("staged = 1\n")
    git(root, "add", "src/staged.py")
    (root / "src/keep.py").write_text("keep = 2\n")
    (root / "src/new_file.py").write_text(f"token = '{SECRET}'\n")


@pytest.fixture
def review(tmp_path):
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    (source / "src/keep.py").write_text("keep = 1\n")
    (source / "src/old_name.py").write_text("renamed = True\n" * 5)
    (source / "src/gone.py").write_text("deleted = True\n")
    git(source, "init", "-q")
    commit(source, "base")
    root = tmp_path / "app"
    (root / "config").mkdir(parents=True)
    (root / "config/runtime.json").write_text(
        json.dumps({"orchestrator_v2": {"execute": True, "publish": False}})
    )
    (root / ".env").write_text(f"REVIEW_API_TOKEN={SECRET}\n")
    runtime = WorkflowRuntime(root)
    runtime.store.register_project(
        "P",
        "Project",
        {
            "repositories": {"source": {"path": str(source), "allowed_paths": ["src"]}},
            "phases": [{"id": "developer", "capabilities": ["code.write"]}],
            "verification": {
                "commands": [
                    {
                        "name": "unit",
                        "repository": "source",
                        "command": [sys.executable, "-c", 'print("verified")'],
                    }
                ],
                "required_gates": ["unit"],
            },
        },
    )
    runtime.store.design(
        "P", "D", "## Scope\nReview\n## Acceptance\nTests pass\n## Constraints\nApproved paths"
    )
    runtime.store.approve("design", "D", 1, "user")
    runtime.store.create_plan("P", "PLAN", "D", 1, [{"id": "T", "title": "Review task"}])
    runtime.store.approve("plan", "PLAN", 1, "user")
    runtime.store.create_snapshot("T", runtime.resolve_snapshot("T"))
    worktree = Path(runtime.store.snapshot("T")["resolved"]["repositories"]["source"]["worktree"])

    def change(path):
        (path / "src/old_name.py").rename(path / "src/new_name.py")
        (path / "src/gone.py").unlink()
        baseline_change(path)  # commits the rename/delete with src/app.py

    assert runtime.execute("T", "run-once", runner=Runner(change))["state"] == "verifying"
    # Written outside the role run (the role may not touch them); verification covers them.
    (worktree / "src/.env").write_text("PRIVATE=1\n")
    (worktree / "outside.txt").write_text("out of scope\n")
    assert runtime.execute("T", "verify-once", verify=True)["passed"]
    web = ReviewWeb(root)
    server = make_server(web)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield runtime, worktree, server, web
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


def call(server, path, payload=None, token=None, raw=False):
    url = f"http://127.0.0.1:{server.server_port}{path}"
    headers = {"Host": f"127.0.0.1:{server.server_port}"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
        if token:
            headers["X-Local-Token"] = token
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read()
            return response.status, body if raw else json.loads(body)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def files_by(bootstrap):
    return {(f["stage"], f["path"]): f for m in bootstrap["modules"] for f in m["files"]}


def test_bootstrap_lists_every_stage_within_scope_and_hides_private(review):
    _, _, server, _ = review
    status, data = call(server, "/api/review/bootstrap?task=T")
    assert status == 200, data
    files = files_by(data)
    stages = {key[0] for key in files}
    assert stages == {"committed", "untracked"} | ({"staged", "unstaged"} & stages)
    assert ("committed", "src/app.py") in files
    assert any(f["status"] == "R" and f["old_path"] == "src/old_name.py" for f in files.values())
    assert any(f["status"] == "D" and f["path"] == "src/gone.py" for f in files.values())
    assert ("untracked", "src/.env") not in files and ("untracked", "outside.txt") not in files
    hidden = data["modules"][0]["hidden"]
    assert hidden["private"] >= 1
    assert data["complete"] is True and len(data["review_digest"]) == 64
    assert data["token"]


def test_staged_and_unstaged_changes_after_verification_are_separate(review):
    _, worktree, server, _ = review
    (worktree / "src/later.py").write_text("later = 1\n")
    git(worktree, "add", "src/later.py")
    (worktree / "src/app.py").write_text("print('one')\nprint('2')\nprint('three')\n")
    _, data = call(server, "/api/review/bootstrap?task=T")
    files = files_by(data)
    assert ("staged", "src/later.py") in files and ("unstaged", "src/app.py") in files


def test_diff_redacts_secrets_and_returns_line_anchors(review):
    _, _, server, _ = review
    status, diff = call(server, "/api/review/diff?task=T&module=source&stage=untracked&path=src/new_file.py")
    assert status == 200, diff
    text = json.dumps(diff)
    assert SECRET not in text and "****" in text
    line = diff["hunks"][0]["lines"][0]
    assert line["kind"] == "add" and line["new"] == 1


def test_binary_and_large_files_are_summaries_and_block_approval(review):
    _, worktree, server, web = review
    (worktree / "src/image.bin").write_bytes(b"\x00\x01\x02" * 100)
    (worktree / "src/huge.py").write_text("x = 1\n" * 60000)
    _, data = call(server, "/api/review/bootstrap?task=T")
    files = files_by(data)
    assert files[("untracked", "src/image.bin")]["binary"] is True
    assert files[("untracked", "src/huge.py")]["truncated"] is True
    assert data["complete"] is False
    status, result = call(
        server,
        "/api/review/decision",
        {"task": "T", "decision": "approved", "review_digest": data["review_digest"]},
        web.token,
    )
    assert status == 400 and "한도" in result["error"]


def test_literal_pathspec_and_no_external_diff_or_textconv(review, tmp_path):
    _, worktree, server, _ = review
    marker = tmp_path / "executed"
    tool = tmp_path / "tool.sh"
    tool.write_text(f"#!/bin/sh\ntouch {marker}\ncat \"$1\"\n")
    tool.chmod(0o755)
    git(worktree, "config", "diff.external", str(tool))
    git(worktree, "config", "diff.leak.textconv", str(tool))
    (worktree / ".gitattributes").write_text("*.py diff=leak\n")
    special = ":(glob)*"
    (worktree / "src" / special).write_text("literal = 1\n")
    (worktree / "src/app.py").write_text("print('changed')\n")
    _, data = call(server, "/api/review/bootstrap?task=T")
    files = files_by(data)
    assert ("untracked", f"src/{special}") in files
    status, diff = call(server, "/api/review/diff?task=T&module=source&stage=unstaged&path=src/app.py")
    assert status == 200 and diff["path"] == "src/app.py"
    assert not marker.exists()


def test_comment_must_anchor_on_observed_line_and_turns_stale_when_code_changes(review):
    _, worktree, server, web = review
    _, boot = call(server, "/api/review/bootstrap?task=T")
    base = {"task": "T", "module": "source", "stage": "committed", "path": "src/app.py"}
    status, _ = call(
        server,
        "/api/review/comment",
        dict(base, side="new", line=99, body="없는 줄", review_digest=boot["review_digest"]),
        web.token,
    )
    assert status == 400
    status, _ = call(
        server,
        "/api/review/comment",
        dict(base, side="new", line=2, body="두 번째 줄 확인 " + SECRET, review_digest="0" * 64),
        web.token,
    )
    assert status == 409
    status, created = call(
        server,
        "/api/review/comment",
        dict(base, side="new", line=2, body="두 번째 줄 확인 " + SECRET, review_digest=boot["review_digest"]),
        web.token,
    )
    assert status == 200, created
    _, boot = call(server, "/api/review/bootstrap?task=T")
    thread = boot["threads"][0]
    assert thread["display_state"] == "open" and SECRET not in thread["body"]
    (worktree / "src/app.py").write_text("print('rewritten')\n")
    commit(worktree, "rewrite")
    _, boot = call(server, "/api/review/bootstrap?task=T")
    assert boot["threads"][0]["display_state"] == "stale"
    assert boot["threads"][0]["anchor"]["line"] == 2


def test_comment_state_requires_version_and_task_ownership(review):
    _, _, server, web = review
    _, boot = call(server, "/api/review/bootstrap?task=T")
    _, created = call(
        server,
        "/api/review/comment",
        {"task": "T", "module": "source", "stage": "committed", "path": "src/app.py", "side": "new", "line": 1, "body": "확인", "review_digest": boot["review_digest"]},
        web.token,
    )
    update = {"task": "T", "thread_id": created["thread_id"], "state": "resolved", "resolution": "수정함"}
    assert call(server, "/api/review/comment-state", dict(update, expected_version=5), web.token)[0] == 409
    assert call(server, "/api/review/comment-state", dict(update, task="OTHER", expected_version=0), web.token)[0] == 400
    status, result = call(server, "/api/review/comment-state", dict(update, expected_version=0), web.token)
    assert status == 200 and result["version"] == 1


def test_approval_needs_resolved_comments_and_current_verification(review):
    runtime, worktree, server, web = review
    _, boot = call(server, "/api/review/bootstrap?task=T")
    _, evidence = call(server, "/api/review/evidence?task=T")
    assert evidence["verification"]["current"] is True, evidence["verification"]
    _, created = call(
        server,
        "/api/review/comment",
        {"task": "T", "module": "source", "stage": "committed", "path": "src/app.py", "side": "new", "line": 1, "body": "확인", "review_digest": boot["review_digest"]},
        web.token,
    )
    approve = {"task": "T", "decision": "approved", "review_digest": boot["review_digest"]}
    status, result = call(server, "/api/review/decision", approve, web.token)
    assert status == 400 and "코멘트" in result["error"]
    call(server, "/api/review/comment-state", {"task": "T", "thread_id": created["thread_id"], "state": "resolved", "resolution": "확인함", "expected_version": 0}, web.token)
    status, result = call(server, "/api/review/decision", approve, web.token)
    assert status == 200, result
    assert call(server, "/api/review/decision", approve, web.token)[0] == 409
    rows = runtime.store.db.rows("SELECT * FROM approvals") if runtime.store.db.rows("SELECT name FROM sqlite_master WHERE name='approvals'") else []
    assert all(row.get("subject_kind") != "review" for row in rows)
    (worktree / "src/keep.py").write_text("keep = 3\n")
    _, boot = call(server, "/api/review/bootstrap?task=T")
    assert boot["decisions"][0]["current"] is False
    _, evidence = call(server, "/api/review/evidence?task=T")
    assert evidence["verification"]["current"] is False
    status, result = call(server, "/api/review/decision", dict(approve, review_digest=boot["review_digest"]), web.token)
    assert status == 400 and "검증" in result["error"]


def test_get_never_writes_and_post_requires_token(review):
    runtime, _, server, _ = review
    database = runtime.store.db.path
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    for path in ("bootstrap", "evidence"):
        assert call(server, f"/api/review/{path}?task=T")[0] == 200
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert call(server, "/api/review/decision", {"task": "T", "decision": "approved"}, "wrong")[0] == 403
    assert call(server, "/api/review/bootstrap?task=missing")[0] == 400
    assert call(server, "/api/review/unknown?task=T")[0] == 400
    status, page = call(server, "/review", raw=True)
    assert status == 200 and b"review.js" in page


def test_special_filenames_are_listed_and_diffed_exactly(review):
    _, worktree, server, _ = review
    names = ["src/-dash.py", "src/back\\slash.py", "src/new\nline.py"]
    for name in names:
        (worktree / name).write_text(f"name = {name!r}\n")
    _, data = call(server, "/api/review/bootstrap?task=T")
    files = files_by(data)
    for name in names:
        assert ("untracked", name) in files, name
        query = urllib.parse.urlencode({"task": "T", "module": "source", "stage": "untracked", "path": name})
        status, diff = call(server, "/api/review/diff?" + query)
        assert status == 200 and diff["path"] == name
        assert diff["hunks"][0]["lines"][0]["text"] == f"name = {name!r}"


def test_thread_of_another_task_cannot_be_changed(review):
    runtime, _, server, web = review
    with runtime.store.db.transaction() as conn:
        conn.execute(
            "INSERT INTO tasks(id,project_id,title,priority,state,definition_json) VALUES('T2','P','other','normal','planned','{}')"
        )
        conn.execute(
            "INSERT INTO review_threads(id,task_id,kind,module_id,anchor_json,evidence_digest,state,body,version,created_at) "
            "VALUES('foreign','T2','diff','source','{}','x','open','other task',0,0)"
        )
    status, result = call(
        server,
        "/api/review/comment-state",
        {"task": "T", "thread_id": "foreign", "state": "resolved", "resolution": "x", "expected_version": 0},
        web.token,
    )
    assert status == 400 and "코멘트가 아닙니다" in result["error"]
    assert runtime.store.db.rows("SELECT state FROM review_threads WHERE id='foreign'")[0]["state"] == "open"


def test_newer_verification_run_during_decision_is_stale(review, monkeypatch):
    from agent_dev.adapters import development_review

    _, _, server, web = review
    _, boot = call(server, "/api/review/bootstrap?task=T")
    # The proof is computed before the write lock; a newer run must invalidate it.
    monkeypatch.setattr(development_review, "verification_state", lambda context, conn=None: {"current": True, "run_id": "older-run", "reason": ""})
    status, result = call(server, "/api/review/decision", {"task": "T", "decision": "approved", "review_digest": boot["review_digest"]}, web.token)
    assert status == 409 and "다시 실행" in result["error"]
