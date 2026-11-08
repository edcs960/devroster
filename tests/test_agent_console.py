import json
import time
from pathlib import Path

import pytest

from agent_dev.adapters.agent_console import AgentConsole, catalog
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.adapters.state.sqlite.store import WorkflowRepository
from agent_dev.domain.content import canonical, digest
from agent_dev.domain.errors import DomainError


def fake_dispatch(args):
    if args.namespace == "doctor":
        time.sleep(30)
    if args.namespace == "design":
        return {
            "content": Path(args.source).read_text(encoding="utf-8"),
            "root": str(args.root),
            "actor": args.actor,
        }
    return {"namespace": args.namespace, "action": args.action, "root": str(args.root)}


def checked(console, operation, fields=None, options=None, request="request-1"):
    payload = {"operation": operation, "fields": fields or {}, "options": options or {}}
    payload.update(preview_digest=console.preview(payload)["preview_digest"], request_id=request)
    return payload


def finished(console, request):
    for _ in range(100):
        job = next((j for j in console.history() if j["id"] == request), None)
        if job and job["state"] not in {"queued", "running"}:
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_preview_and_history_never_initialize(tmp_path):
    console = AgentConsole(tmp_path)
    before = list(tmp_path.iterdir())
    assert (
        console.preview({"operation": "preset.list", "fields": {}, "options": {}})["actor"]
        == "local-web-user"
    )
    assert console.history() == []
    assert list(tmp_path.iterdir()) == before
    ops = {op["id"] for op in catalog()["operations"]}
    assert {
        "task.run",
        "task.resume",
        "task.verify",
        "task.cancel",
        "task.publish",
        "task.complete",
        "project.register",
        "plan.create",
        "knowledge.confirm",
        "backup.restore",
    } <= ops


def test_upload_and_server_scopes_durable_result_and_dedup(tmp_path):
    console = AgentConsole(tmp_path, fake_dispatch)
    payload = checked(
        console, "design.refine", {"project_id": "P", "design_id": "D", "source": "# 문서"}
    )
    try:
        first = console.start(payload)
        assert console.start(payload)["id"] == first["id"]
        result = finished(console, first["id"])
        assert result["state"] == "completed"
        assert result["result"]["content"] == "# 문서"
        assert result["result"]["actor"] == "local-web-user"
        files = list(console.uploads.glob("*.md"))
        assert len(files) == 1 and files[0].stat().st_mode & 0o777 == 0o600
        other = AgentConsole(tmp_path)
        assert other.history()[0]["result"] == result["result"]
        assert other.start(payload)["id"] == first["id"]
        changed = checked(
            console, "design.refine", {"project_id": "P", "design_id": "D", "source": "changed"}
        )
        with pytest.raises(DomainError, match="같은 요청 ID"):
            console.start(changed)
    finally:
        console.close()


def test_preview_digest_and_allowlist(tmp_path):
    console = AgentConsole(tmp_path)
    with pytest.raises(DomainError, match="미리 확인"):
        console.start({"operation": "preset.list", "fields": {}, "options": {}})
    for invalid in [
        {"operation": "task.run", "fields": {"task_id": "T"}, "options": {"root": "/tmp"}},
        {"operation": "shell", "fields": {}, "options": {}},
        {"operation": "preset.list", "fields": {}, "options": {}, "root": "/tmp"},
        {
            "operation": "notification.drain",
            "fields": {"project_id": "P"},
            "options": {"apply": True},
        },
    ]:
        with pytest.raises(DomainError):
            console.prepare(invalid)
    assert not console.db_path.exists()


def test_task_precondition_and_path_escape(tmp_path):
    console = AgentConsole(tmp_path)
    with pytest.raises(DomainError):
        console.preview({"operation": "task.run", "fields": {"task_id": "MISSING"}, "options": {}})
    for path in ["../escape.db", "/tmp/escape.db", "a/b.db", "..", "../outside.md"]:
        operation = "knowledge.candidate" if path.endswith(".md") else "refactor.export"
        fields = (
            {"project_id": "P", "source": path, "commit": "HEAD"}
            if operation.startswith("knowledge")
            else {"destination": path}
        )
        with pytest.raises(DomainError):
            console.prepare({"operation": operation, "fields": fields, "options": {}})
    (tmp_path / ".agent-dev").mkdir()
    console.artifacts.symlink_to(tmp_path)
    with pytest.raises(DomainError):
        console.prepare(
            {"operation": "refactor.export", "fields": {"destination": "escape.db"}, "options": {}}
        )


def test_cancel_background_worker(tmp_path):
    console = AgentConsole(tmp_path, fake_dispatch)
    try:
        payload = checked(console, "doctor")
        started = console.start(payload)
        assert started["state"] == "queued"
        assert console.start(payload)["id"] == started["id"]
        job = console.cancel(started["id"])
        assert job["state"] == "cancelled"
        assert not console.processes[job["id"]][0].is_alive()
        assert AgentConsole(tmp_path).history()[0]["state"] == "cancelled"
    finally:
        console.close()


def test_restart_projects_interrupted_without_read_mutation(tmp_path):
    console = AgentConsole(tmp_path)
    store = WorkflowRepository(Database(console.db_path))
    job = {
        "id": "old",
        "operation": "task.run",
        "state": "running",
        "task_id": "T",
        "created_at": 1,
    }
    with store.db.transaction() as con:
        con.execute(
            "INSERT INTO commands VALUES(?,?,'running',?,?)",
            ("web:old", digest({}), canonical(job), 1),
        )
    assert console.history()[0]["state"] == "interrupted"
    assert store.db.rows("SELECT state FROM commands")[0]["state"] == "running"
    assert not console.processes


def test_plan_accepts_array_and_rejects_runner_override(tmp_path):
    console = AgentConsole(tmp_path)
    fields = {
        "project_id": "P",
        "plan_id": "PLAN",
        "design_id": "D",
        "version": "1",
        "source": json.dumps([{"id": "T", "title": "Test"}]),
    }
    assert (
        console.prepare({"operation": "plan.create", "fields": fields, "options": {}})[0]["id"]
        == "plan.create"
    )
    fields["source"] = json.dumps(
        [
            {
                "id": "T",
                "config_override": {
                    "runners": [{"provider": "shell", "config": {"command": "sh"}}]
                },
            }
        ]
    )
    with pytest.raises(DomainError):
        console.prepare({"operation": "plan.create", "fields": fields, "options": {}})


def test_real_canonical_registration_and_design(tmp_path):
    console = AgentConsole(tmp_path)
    profile = {"repositories": {}, "phases": [], "allowed_paths": []}
    try:
        job = console.start(
            checked(
                console, "project.register", {"project_id": "P", "profile": json.dumps(profile)}
            )
        )
        assert finished(console, job["id"])["state"] == "completed"
        job = console.start(
            checked(
                console,
                "design.refine",
                {
                    "project_id": "P",
                    "design_id": "D",
                    "source": "## Scope\nLocal work\n## Acceptance\nVerify\n## Constraints\nKeep scope",
                },
                request="design",
            )
        )
        result = finished(console, job["id"])
        assert result["state"] == "completed"
        assert result["result"]["design_id"] == "D"
    finally:
        console.close()


def test_active_job_limit_and_task_dedup(tmp_path):
    console = AgentConsole(tmp_path, fake_dispatch)
    try:
        console.start(checked(console, "doctor", request="one"))
        console.start(checked(console, "doctor", request="two"))
        with pytest.raises(DomainError, match="두 요청"):
            console.start(checked(console, "doctor", request="three"))
        assert len(console.history()) == 2
    finally:
        console.close()


def test_lost_parent_interrupts_worker_and_releases_lease(tmp_path):
    import multiprocessing

    from agent_dev.adapters.agent_console import _worker
    from agent_dev.adapters.state.sqlite.safety import ExecutionSafety

    console = AgentConsole(tmp_path)
    _, args = console.prepare(
        {"operation": "doctor", "fields": {}, "options": {}}, request_id="orphan"
    )
    store = WorkflowRepository(Database(console.db_path))
    job = {"id": "orphan", "operation": "doctor", "state": "queued", "created_at": time.time()}
    with store.db.transaction() as con:
        con.execute(
            "INSERT INTO commands VALUES(?,?,'queued',?,?)",
            ("web:orphan", digest({}), canonical(job), job["created_at"]),
        )
    lease = ExecutionSafety(store).acquire("agent-web:web:orphan", "test", 60)
    process = multiprocessing.get_context("spawn").Process(
        target=_worker,
        args=(str(console.db_path), "web:orphan", job, args, lease, fake_dispatch, -1),
    )
    try:
        process.start()
        process.join(timeout=5)
        assert not process.is_alive()
        assert console.history()[0]["state"] == "interrupted"
        assert store.db.rows("SELECT expires_at FROM leases")[0]["expires_at"] == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)


def test_portable_root_and_safe_cosmetic_settings(tmp_path):
    from agent_dev.adapters.workspace import initialize

    initialize(tmp_path, name="Portable", project_id="P", paths=[])
    console = AgentConsole(tmp_path)
    assert console.root == tmp_path / ".agent-dev"
    assert console.workspace == tmp_path
    assert console.context()["projects"][0]["id"] == "P"
    _, args = console.prepare(
        {"operation": "view.set-settings", "fields": {"source": '{"theme":"dark"}'}, "options": {}},
        materialize=True,
        request_id="settings",
    )
    content = json.loads(Path(args.source).read_text(encoding="utf-8"))
    assert content["theme"] == "dark"
    with pytest.raises(DomainError):
        console.prepare(
            {
                "operation": "view.set-settings",
                "fields": {"source": '{"codex_cli":{"command":"sh"}}'},
                "options": {},
            }
        )
