"""Schema v3 migration, backup, and Studio handoff acceptance tests."""

import contextlib
import json
import sqlite3
import time

import pytest

from agent_dev.adapters import backup as backup_mod
from agent_dev.adapters import studio_handoff
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.adapters.state.sqlite.store import WorkflowRepository
from agent_dev.adapters.studio_store import StudioStore
from agent_dev.domain.content import digest
from agent_dev.domain.errors import DomainError, ErrorCode

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def canonical_db(tmp_path):
    db = Database(tmp_path / "state.db")
    repo = WorkflowRepository(db)
    repo.register_project(
        "P1",
        "테스트 프로젝트",
        {
            "repositories": {
                "backend": {"path": str(tmp_path / "backend"), "allowed_paths": ["src/**"]}
            },
            "verification": {
                "commands": [
                    {"name": "unit", "repository": "backend", "command": ["python", "-m", "pytest"]}
                ]
            },
        },
    )
    return db


@pytest.fixture
def studio_root(tmp_path):
    studio_dir = tmp_path / ".agent-dev/studio"
    studio_dir.mkdir(parents=True)
    store = StudioStore(tmp_path)
    _setup_studio_project(store, "STUDIO-001")
    return tmp_path


def _setup_studio_project(store, project_id, *, open_comment=False, orphaned_open=False):
    """Create a Studio project with approved spec + mock; optionally leave open comments."""
    with store.transaction() as db:
        db.execute(
            "INSERT INTO projects(id,title,brief,version,spec_revision,mock_revision,created) "
            "VALUES(?,?,?,1,0,0,?)",
            (project_id, "Test Project", "A brief", time.time()),
        )
        payload = {
            "requirements": [
                {
                    "id": "REQ-001",
                    "title": "메모 저장",
                    "description": "저장 기능",
                    "source_quote": "저장",
                    "priority": "must",
                    "acceptance": ["빈 제목 차단", "저장 확인"],
                },
                {
                    "id": "REQ-002",
                    "title": "백엔드 only",
                    "description": "API 제한",
                    "source_quote": "API",
                    "priority": "must",
                    "acceptance": ["인증 필요"],
                },
            ],
            "screens": [
                {
                    "id": "SCR-001",
                    "title": "메모 목록",
                    "purpose": "목록",
                    "requirement_ids": ["REQ-001"],
                },
            ],
            "questions": [],
            "documents": {},
        }
        mock_payload = {
            "tone": "indigo",
            "screens": [{"id": "SCR-001", "title": "메모 목록", "elements": []}],
        }
        spec_json = json.dumps(payload, sort_keys=True)
        spec_dg = digest(payload)
        db.execute(
            "INSERT INTO specs(project_id,revision,payload,source,digest,approved,created) "
            "VALUES(?,1,?,?,?,1,?)",
            (project_id, spec_json, "", spec_dg, time.time()),
        )
        db.execute("UPDATE projects SET spec_revision=1 WHERE id=?", (project_id,))

        mock_json = json.dumps(mock_payload, sort_keys=True)
        mock_dg = digest(mock_payload)
        db.execute(
            "INSERT INTO mocks(project_id,revision,spec_revision,payload,digest,created) "
            "VALUES(?,1,1,?,?,?)",
            (project_id, mock_json, mock_dg, time.time()),
        )
        db.execute("UPDATE projects SET mock_revision=1 WHERE id=?", (project_id,))

        db.execute(
            "INSERT INTO approvals(project_id,mock_revision,screen_id,digest,note,created) "
            "VALUES(?,1,'SCR-001',?,?,?)",
            (
                project_id,
                digest({"mock": mock_dg, "spec": spec_dg, "screen": "SCR-001"}),
                "확정",
                time.time(),
            ),
        )

        if open_comment:
            db.execute(
                "INSERT INTO comments(id,project_id,mock_revision,screen_id,element_id,"
                "body,state,resolution,orphaned,origin_id,created) VALUES(?,?,1,'SCR-001','E1',"
                "'미해결 의견','open','',0,NULL,?)",
                ("C1", project_id, time.time()),
            )
        if orphaned_open:
            db.execute(
                "INSERT INTO comments(id,project_id,mock_revision,screen_id,element_id,"
                "body,state,resolution,orphaned,origin_id,created) VALUES(?,?,1,'SCR-001','DELETED',"
                "'고아 미해결','open','',1,NULL,?)",
                ("C2", project_id, time.time()),
            )
    return store


# ---------------------------------------------------------------------------
# Schema v3 migration tests
# ---------------------------------------------------------------------------


def test_schema_v3_new_database_has_all_8_tables(tmp_path):
    db = Database(tmp_path / "state.db")
    conn = db.connect()
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    conn.close()
    for table in backup_mod.V3_TABLES:
        assert table in tables, f"v3 table missing: {table}"
    assert "schema_versions" in tables
    version = db.rows("SELECT MAX(version) AS v FROM schema_versions")[0]["v"]
    assert version == 4  # v4 added configuration provenance on top of the v3 tables


def authentic_v2(path):
    from pathlib import Path

    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.executescript(
            Path(__file__).with_name("fixtures").joinpath("canonical-v2.sql").read_text(encoding="utf-8")
        )
        conn.executemany("INSERT INTO schema_versions VALUES(?,?)", [(1, 1.0), (2, 2.0)])
        conn.execute("INSERT INTO projects VALUES('OLD','old','old','draft',0,1.0)")
        conn.commit()


def test_authentic_v2_upgrade_and_backup(tmp_path):
    path = tmp_path / "state.db"
    authentic_v2(path)
    assert backup_mod.validate_database(path) == 2
    db = Database(path)
    assert db.rows("SELECT * FROM projects")[0]["id"] == "OLD"
    assert backup_mod.validate_database(path) == 4
    saved = list(tmp_path.glob("state.db.pre-v3-backup*"))
    assert len(saved) == 1
    assert backup_mod.validate_database(saved[0]) == 2
    Database(path)
    assert len(list(tmp_path.glob("state.db.pre-v3-backup*"))) == 1
    assert not db.rows("PRAGMA foreign_key_check")
    assert db.rows("PRAGMA integrity_check")[0]["integrity_check"] == "ok"


def test_migration_preserves_comment_prefixed_ddl(tmp_path):
    conn = sqlite3.connect(":memory:")
    Database._run_statements(
        conn,
        "-- guard\nCREATE TABLE protected(id TEXT);\n-- trigger\nCREATE TRIGGER frozen BEFORE UPDATE ON protected BEGIN SELECT RAISE(ABORT,'immutable'); END;",
    )
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('protected','frozen')"
        ).fetchone()[0]
        == 2
    )
    conn.close()


def test_migration_rollback_on_ddl_fault(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    authentic_v2(path)
    original = Database._run_statements

    def fault(conn, sql):
        original(conn, sql)
        raise RuntimeError("DDL fault")

    monkeypatch.setattr(Database, "_run_statements", staticmethod(fault))
    with pytest.raises(RuntimeError, match="DDL fault"):
        Database(path)
    assert backup_mod.validate_database(path) == 2
    with sqlite3.connect(path) as conn:
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='handoff_imports'"
        ).fetchone()


def test_migration_preexisting_backup_never_silently_reused(tmp_path):
    path = tmp_path / "state.db"
    authentic_v2(path)
    old = tmp_path / "state.db.pre-v3-backup"
    old.write_bytes(b"untrusted stale artifact")
    Database(path)
    assert old.read_bytes() == b"untrusted stale artifact"
    assert any(
        backup_mod.validate_database(p) == 2 for p in tmp_path.glob("state.db.pre-v3-backup.*")
    )


def test_migration_writer_serialization(tmp_path, monkeypatch):
    import threading

    path = tmp_path / "state.db"
    authentic_v2(path)
    original = Database._run_statements
    checked = []

    def inspect(conn, sql):
        def writer():
            with sqlite3.connect(path, timeout=0.03) as other:
                try:
                    other.execute("UPDATE projects SET name='raced' WHERE id='OLD'")
                except sqlite3.OperationalError as exc:
                    checked.append("locked" in str(exc))

        worker = threading.Thread(target=writer)
        worker.start()
        worker.join()
        original(conn, sql)

    monkeypatch.setattr(Database, "_run_statements", staticmethod(inspect))
    Database(path)
    assert checked == [True]
    assert Database(path, read_only=True).rows("SELECT name FROM projects")[0]["name"] == "old"


def test_schema_newer_than_v3_is_rejected_ro_and_rw(tmp_path):
    path = tmp_path / "state.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            "CREATE TABLE schema_versions(version INTEGER PRIMARY KEY, applied_at REAL); INSERT INTO schema_versions VALUES(5,1);"
        )
    before = path.read_bytes()
    for ro in (True, False):
        with pytest.raises(DomainError):
            Database(path, read_only=ro)
    assert path.read_bytes() == before


def test_read_only_missing_has_no_side_effect(tmp_path):
    path = tmp_path / "missing" / "state.db"
    with pytest.raises(FileNotFoundError):
        Database(path, read_only=True)
    assert not path.parent.exists()


def test_backup_requires_old_and_new_guards(tmp_path):
    path = tmp_path / "state.db"
    authentic_v2(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER immutable_design")
    with pytest.raises(DomainError):
        backup_mod.validate_database(path)
    Database(path)
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TRIGGER immutable_imported_requirements_delete")
    with pytest.raises(DomainError):
        backup_mod.validate_database(path)


def test_fk_constraints_enforced(canonical_db, tmp_path):
    with pytest.raises(sqlite3.IntegrityError), canonical_db.transaction() as conn:
        conn.execute(
            "INSERT INTO handoff_imports VALUES("
            "'HI1','NONEXISTENT','SID','sd','md','bd',NULL,NULL,NULL,'rk1',1.0)"
        )


# Studio handoff behavior, using actual finalized source and immutable profile rows.


def selection(db, root, **changes):
    params = {"project_id": "P1", "source_studio_id": "STUDIO-001"}
    params.update(changes)
    return studio_handoff.preview(db, root, params)


def register(db, root, prepared, request_id="REQUEST-1", **changes):
    params = dict(
        prepared["selection"], preview_digest=prepared["preview_digest"], request_id=request_id
    )
    params.update(changes)
    return studio_handoff.commit(db, root, params)


def test_preview_binds_entire_selection(canonical_db, studio_root):
    first = selection(canonical_db, studio_root)
    assert first["requirement_count"] == 2
    assert first["mappings"][1]["backend_only"]
    changed = dict(first["selection"])
    changed["tasks"][0] = dict(changed["tasks"][0], title="changed")
    second = selection(canonical_db, studio_root, **changed)
    assert first["preview_digest"] != second["preview_digest"]
    with pytest.raises(DomainError) as exc:
        register(canonical_db, studio_root, first, tasks=changed["tasks"])
    assert exc.value.code == ErrorCode.STALE_VERSION
    assert not canonical_db.rows("SELECT * FROM handoff_imports")


def test_handoff_draft_explicit_approvals_and_all_requirements(canonical_db, studio_root):
    prepared = selection(canonical_db, studio_root)
    result = register(canonical_db, studio_root, prepared)
    assert not canonical_db.rows("SELECT * FROM tasks")
    assert not canonical_db.rows("SELECT * FROM approvals")
    reqs = canonical_db.rows("SELECT * FROM imported_requirements")
    assert {r["requirement_id"] for r in reqs} == {"REQ-001", "REQ-002"}
    assert json.loads(reqs[0]["content_json"])["criteria_ids"] == ["REQ-001:AC-01", "REQ-001:AC-02"]
    repo = WorkflowRepository(canonical_db)
    with pytest.raises(DomainError):
        repo.approve("plan", result["plan_id"], 1, "user")
    repo.approve("design", result["design_id"], 1, "user")
    repo.approve("plan", result["plan_id"], 1, "user")
    assert len(canonical_db.rows("SELECT * FROM tasks")) == 2
    assert len(canonical_db.rows("SELECT * FROM requirement_links WHERE kind='task'")) == 2
    assert len(canonical_db.rows("SELECT * FROM requirement_links WHERE kind='plan_task'")) == 2


def test_idempotent_retry_and_source_dedup(canonical_db, studio_root):
    prepared = selection(canonical_db, studio_root)
    first = register(canonical_db, studio_root, prepared)
    assert register(canonical_db, studio_root, prepared) == first
    assert register(canonical_db, studio_root, prepared, request_id="retry-key") == first
    bad = dict(prepared["selection"], design_id="OTHER")
    with pytest.raises(DomainError):
        register(canonical_db, studio_root, prepared, **bad)
    assert len(canonical_db.rows("SELECT * FROM handoff_imports")) == 1
    assert len(canonical_db.rows("SELECT * FROM task_plans")) == 1


@pytest.mark.parametrize(
    "orphaned,state,blocked", [(0, "open", True), (1, "open", True), (1, "resolved", False)]
)
def test_open_comments_including_orphans(canonical_db, studio_root, orphaned, state, blocked):
    with StudioStore(studio_root).transaction() as conn:
        conn.execute(
            "INSERT INTO comments VALUES('C1','STUDIO-001',1,'SCR-001','old','comment',?,'resolved reason',?,NULL,1)",
            (state, orphaned),
        )
    if blocked:
        with pytest.raises(DomainError):
            selection(canonical_db, studio_root)
    else:
        assert selection(canonical_db, studio_root)["requirement_count"] == 2


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE specs SET approved=0",
        "UPDATE mocks SET spec_revision=9",
        "UPDATE approvals SET digest='fake'",
        "UPDATE specs SET digest='fake'",
        "INSERT INTO jobs VALUES('J','STUDIO-001','spec','running',1,'',1)",
    ],
)
def test_unfinalized_or_corrupt_source_rejected(canonical_db, studio_root, sql):
    with sqlite3.connect(studio_handoff._studio_db_path(studio_root)) as conn:
        conn.execute(sql)
    with pytest.raises(DomainError):
        selection(canonical_db, studio_root)


def test_profile_stale_rejected(canonical_db, studio_root):
    prepared = selection(canonical_db, studio_root)
    WorkflowRepository(canonical_db).revise_profile("P1", {}, 0)
    with pytest.raises(DomainError) as exc:
        register(canonical_db, studio_root, prepared)
    assert exc.value.code == ErrorCode.STALE_VERSION


def test_source_disappearance_preserves_snapshot(canonical_db, studio_root):
    prepared = selection(canonical_db, studio_root)
    result = register(canonical_db, studio_root, prepared)
    studio_handoff._studio_db_path(studio_root).unlink()
    assert register(canonical_db, studio_root, prepared) == result
    assert len(canonical_db.rows("SELECT * FROM imported_requirements")) == 2
    assert (
        "brief" in canonical_db.rows("SELECT content FROM design_documents")[0]["content"].lower()
    )


def test_atomic_fault_rolls_back_every_business_row(canonical_db, studio_root, monkeypatch):
    prepared = selection(canonical_db, studio_root)
    original = studio_handoff._write_handoff

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("after write")

    monkeypatch.setattr(studio_handoff, "_write_handoff", fail)
    before = canonical_db.rows("SELECT * FROM events")
    with pytest.raises(RuntimeError):
        register(canonical_db, studio_root, prepared)
    for table in (
        "design_documents",
        "task_plans",
        "handoff_imports",
        "imported_requirements",
        "requirement_links",
        "commands",
    ):
        assert not canonical_db.rows("SELECT * FROM " + table)
    assert canonical_db.rows("SELECT * FROM events") == before


def test_studio_writer_lock_held_through_canonical_commit(canonical_db, studio_root, monkeypatch):
    import threading

    prepared = selection(canonical_db, studio_root)
    original = studio_handoff._write_handoff
    outcomes = []
    backup_outcomes = []
    sqlite_connect = sqlite3.connect

    def short_connect(*args, **kwargs):
        kwargs["timeout"] = 0.03
        return sqlite_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", short_connect)

    def backup_attempt():
        destination = studio_root.parent / (studio_root.name + "-during-handoff.zip")
        try:
            backup_mod.create_bundle(studio_root, destination)
        except DomainError as exc:
            backup_outcomes.append("busy" in str(exc))
        assert not destination.exists()

    def writer():
        with sqlite3.connect(studio_handoff._studio_db_path(studio_root), timeout=0.03) as conn:
            try:
                conn.execute("UPDATE projects SET brief='changed'")
            except sqlite3.OperationalError as exc:
                outcomes.append("locked" in str(exc))

    def during_write(*args, **kwargs):
        original(*args, **kwargs)
        worker = threading.Thread(target=writer)
        worker.start()
        worker.join()
        backup_worker = threading.Thread(target=backup_attempt)
        backup_worker.start()
        backup_worker.join()

    monkeypatch.setattr(studio_handoff, "_write_handoff", during_write)
    # SQL trace callback runs on the actual COMMIT, before ownership is released.
    original_connect = canonical_db.connect

    def traced():
        conn = original_connect()

        def trace(sql):
            if sql == "COMMIT":
                worker = threading.Thread(target=writer)
                worker.start()
                worker.join()
                backup_worker = threading.Thread(target=backup_attempt)
                backup_worker.start()
                backup_worker.join()

        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(canonical_db, "connect", traced)
    register(canonical_db, studio_root, prepared)
    assert outcomes == [True, True]
    assert backup_outcomes == [True, True]
    writer()
    assert outcomes == [True, True]


def test_event_immutability_blocks_tamper(canonical_db, studio_root):
    prepared = selection(canonical_db, studio_root)
    register(canonical_db, studio_root, prepared)
    with pytest.raises(sqlite3.IntegrityError), canonical_db.transaction() as conn:
        conn.execute("UPDATE events SET payload_json='{}' WHERE kind='PlanDraftCreated'")


def test_source_read_only_no_constructors(canonical_db, studio_root, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("RW Studio constructor used")

    monkeypatch.setattr(StudioStore, "__init__", forbidden)
    assert selection(canonical_db, studio_root)["requirement_count"] == 2


def test_two_initializers_make_only_original_v2_backup(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    path = tmp_path / "state.db"
    authentic_v2(path)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda _: Database(path), range(2)))
    originals = [
        p for p in tmp_path.glob("state.db.pre-v3-backup*") if not p.name.endswith(("-wal", "-shm"))
    ]
    assert len(originals) == 1
    assert backup_mod.validate_database(originals[0]) == 2


def test_module_scope_freezes_profile_and_blocks_same_relative_path(tmp_path, monkeypatch):
    import subprocess
    from pathlib import Path

    from agent_dev.adapters.workflow_runtime import WorkflowRuntime
    from agent_dev.domain.contracts import PhaseResult

    roots = {}
    for module in ("frontend", "backend"):
        path = tmp_path / module
        path.mkdir()
        (path / "shared.py").write_text("base", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(path),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.test",
                "commit",
                "-qm",
                "base",
            ],
            check=True,
        )
        roots[module] = {"path": str(path), "allowed_paths": ["shared.py"]}
    app = tmp_path / "app"
    runtime = WorkflowRuntime(app)
    (app / "config").mkdir()
    (app / "config/runtime.json").write_text(json.dumps({"orchestrator_v2": {"execute": True}}), encoding="utf-8")
    runtime.store.register_project(
        "P1",
        "project",
        {
            "repositories": roots,
            "phases": [{"id": "developer", "capabilities": ["code.write"]}],
            "runners": [
                {
                    "id": "local",
                    "provider": "codex_cli",
                    "enabled": True,
                    "authenticated": True,
                    "capabilities": ["code.write"],
                    "config": {"command": "python3"},
                }
            ],
        },
    )
    _setup_studio_project(StudioStore(app), "STUDIO-001")
    prepared = selection(runtime.store.db, app)
    tasks = [
        dict(t, module_id="frontend", allowed_paths=["shared.py"])
        for t in prepared["selection"]["tasks"]
    ]
    prepared = selection(runtime.store.db, app, tasks=tasks)
    result = register(runtime.store.db, app, prepared)
    runtime.store.approve("design", result["design_id"], 1, "user")
    runtime.store.approve("plan", result["plan_id"], 1, "user")
    runtime.store.revise_profile(
        "P1",
        {
            "repositories": {"backend": roots["backend"]},
            "phases": [{"id": "developer", "capabilities": ["code.write"]}],
        },
        0,
    )
    resolved = runtime.resolve_snapshot(prepared["selection"]["tasks"][0]["id"])
    assert resolved["profile_version"] == 1
    assert set(resolved["repositories"]) == {"frontend"}
    assert resolved["workflow_graph"]["nodes"][0]["module"] == "frontend"
    task_id = prepared["selection"]["tasks"][0]["id"]
    runtime.store.create_snapshot(task_id, resolved)
    native = []

    def native_adapter(provider, config, workspace, lease):
        native.append((workspace, config["approved_add_dirs"]))
        return object()

    monkeypatch.setattr("agent_dev.adapters.workflow_runtime.CliRunnerAdapter", native_adapter)
    runtime.runners(task_id)
    assert native == [(Path(resolved["repositories"]["frontend"]["worktree"]), [])]
    allowed_task = prepared["selection"]["tasks"][1]["id"]
    runtime.store.create_snapshot(allowed_task, runtime.resolve_snapshot(allowed_task))

    class SelectedModuleWriter:
        def execute(self, request):
            context = json.loads(request.context)
            Path(context["repositories"]["frontend"]["worktree"]).joinpath("shared.py").write_text(
                "authorized", encoding="utf-8"
            )
            return PhaseResult('```json\n{"handoff_status":"pass"}\n```')

    assert (
        runtime.execute(allowed_task, "selected-module-write", runner=SelectedModuleWriter())[
            "state"
        ]
        == "verifying"
    )

    class OtherModuleReader:
        def execute(self, request):
            context = json.loads(request.context)
            # The other module gets no worktree, so the role cannot reach it.
            assert set(context["repositories"]) == {"frontend"}
            assert context["node_config"]["module"] == "frontend"
            return PhaseResult('```json\n{"handoff_status":"pass"}\n```')

    assert runtime.execute(task_id, "other-module", runner=OtherModuleReader())["state"] == (
        "verifying"
    )
    assert Path(roots["backend"]["path"]).joinpath("shared.py").read_text(encoding="utf-8") == "base"


def test_verification_mapping_requires_registered_target(canonical_db, studio_root):
    first = selection(canonical_db, studio_root)
    tasks = first["selection"]["tasks"]
    tasks[0]["verification_mapping"] = [
        {
            "requirement_id": "REQ-001",
            "criteria_ids": ["REQ-001:AC-01"],
            "gate": "unit",
            "target": "tests/fake.py::test_fake",
        }
    ]
    with pytest.raises(DomainError):
        selection(canonical_db, studio_root, tasks=tasks)


def test_same_source_different_intent_is_explicit_conflict(canonical_db, studio_root):
    first = selection(canonical_db, studio_root)
    registered = register(canonical_db, studio_root, first)
    changed = json.loads(json.dumps(first["selection"]))
    changed["tasks"][0]["title"] = "different requested work"
    with pytest.raises(DomainError) as exc:
        selection(canonical_db, studio_root, **changed)
    assert exc.value.code == ErrorCode.STALE_VERSION
    with pytest.raises(DomainError) as exc:
        studio_handoff.commit(
            canonical_db,
            studio_root,
            dict(changed, request_id="DIFFERENT-INTENT", preview_digest=first["preview_digest"]),
        )
    assert exc.value.code == ErrorCode.STALE_VERSION
    assert not canonical_db.rows("SELECT * FROM commands WHERE key='handoff:DIFFERENT-INTENT'")
    assert register(canonical_db, studio_root, first, request_id="SAME-INTENT") == registered


def test_concurrent_initializers_repeat_threads_and_processes(tmp_path):
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor

    for round_ in range(12):
        folder = tmp_path / f"threads-{round_}"
        folder.mkdir()
        path = folder / "state.db"
        authentic_v2(path)
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(Database, [path] * 4))
        originals = [p for p in folder.glob("state.db.pre-v3-backup*") if not p.name.endswith(("-wal", "-shm"))]
        assert len(originals) == 1 and backup_mod.validate_database(originals[0]) == 2
        assert Database(path).rows("SELECT MAX(version) AS v FROM schema_versions")[0]["v"] == 4
        assert Database(path).rows("PRAGMA foreign_key_check") == []
    folder = tmp_path / "processes"
    folder.mkdir()
    path = folder / "state.db"
    authentic_v2(path)
    code = "import sys; from agent_dev.adapters.state.sqlite.database import Database; Database(sys.argv[1])"
    workers = [subprocess.Popen([sys.executable, "-c", code, str(path)], stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace") for _ in range(3)]
    for worker in workers:
        _, error = worker.communicate(timeout=60)
        assert worker.returncode == 0, error
    assert len([p for p in folder.glob("state.db.pre-v3-backup*") if not p.name.endswith(("-wal", "-shm"))]) == 1
    assert Database(path, read_only=True).rows("SELECT MAX(version) AS v FROM schema_versions")[0]["v"] == 4


def test_wal_switch_gives_up_with_the_real_lock_error(tmp_path):
    import sqlite3

    path = tmp_path / "rollback.db"
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("CREATE TABLE t(x)")
    holder.execute("BEGIN EXCLUSIVE")
    waiting = sqlite3.connect(path, timeout=0.05, isolation_level=None)
    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            Database._enable_wal(waiting, timeout=0.3)
    finally:
        waiting.close()
        holder.rollback()
        holder.close()
    future = tmp_path / "future.db"
    sql = sqlite3.connect(future)
    sql.execute("CREATE TABLE schema_versions(version INTEGER PRIMARY KEY, applied_at REAL)")
    sql.execute("INSERT INTO schema_versions VALUES(5,0)")
    sql.commit()
    sql.close()
    with pytest.raises(DomainError):
        Database(future)


def test_wal_switch_wall_budget_includes_busy_waits(tmp_path):
    import sqlite3

    path = tmp_path / "budget.db"
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("CREATE TABLE t(x)")
    holder.execute("BEGIN EXCLUSIVE")
    waiting = sqlite3.connect(path, timeout=10, isolation_level=None)
    waiting.execute("PRAGMA busy_timeout=10000")
    try:
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            Database._enable_wal(waiting, timeout=0.5)
        assert time.monotonic() - started < 2.0, "busy waits must count against the budget"
        assert waiting.execute("PRAGMA busy_timeout").fetchone()[0] == 10000, "busy_timeout restored"
    finally:
        waiting.close()
        holder.rollback()
        holder.close()
    free = sqlite3.connect(path, isolation_level=None)
    free.execute("PRAGMA busy_timeout=7000")
    Database._enable_wal(free, timeout=0.5)
    assert free.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert free.execute("PRAGMA busy_timeout").fetchone()[0] == 7000
    free.close()


def test_default_tasks_follow_screens_and_carry_workstreams(canonical_db, studio_root):
    """One task per requirement ran the whole roster each time, and handoff
    tasks had no workstream, so UI and server roles never ran."""
    prepared = selection(canonical_db, studio_root)
    tasks = prepared["selection"]["tasks"]
    assert [t["requirement_ids"] for t in tasks] == [["REQ-001"], ["REQ-002"]]
    assert tasks[0]["workstreams"] == ["frontend"]  # REQ-001 is on screen SCR-001
    assert tasks[1]["workstreams"] == []
    assert prepared["runs_per_task"] == 0  # this profile registers no workflow
    # A preview echoed back with recomputed fields is accepted.
    again = selection(canonical_db, studio_root, **prepared["selection"])
    assert again["preview_digest"] == prepared["preview_digest"]


def test_default_tasks_split_ui_and_server_modules(tmp_path):
    """Every suggested task went to the first module (the server), and the
    server task got the frontend roles because its requirements appear on a screen."""
    web, api = tmp_path / "web", tmp_path / "api"
    web.mkdir()
    api.mkdir()
    (web / "package.json").write_text('{"dependencies": {"react": "19"}}', encoding="utf-8")
    (api / "build.gradle").write_text("plugins { id 'org.springframework.boot' }", encoding="utf-8")
    bundle = {
        "requirements": [
            {"id": "REQ-001", "title": "add"},
            {"id": "REQ-002", "title": "list"},
            {"id": "REQ-003", "title": "export"},
        ],
        "spec_payload": {
            "screens": [
                {"id": "SCR-001", "requirement_ids": ["REQ-001"]},
                {"id": "SCR-002", "requirement_ids": ["REQ-002"]},
            ]
        },
    }
    repositories = {"backend": {"path": str(api)}, "frontend": {"path": str(web)}}
    assert studio_handoff.default_tasks(bundle, repositories) == [
        ("[backend] add 외 2건", ["REQ-001", "REQ-002", "REQ-003"], "backend"),
        ("[frontend] add", ["REQ-001"], "frontend"),
        ("[frontend] list", ["REQ-002"], "frontend"),
    ]
    assert studio_handoff.workstreams(bundle, ["REQ-001"], str(api)) == (["backend"], [])
    assert studio_handoff.workstreams(bundle, ["REQ-001"], str(web)) == (["frontend"], [])
    # One module keeps the screen grouping without a module prefix.
    assert studio_handoff.default_tasks(bundle, {"frontend": {"path": str(web)}}) == [
        ("add", ["REQ-001"], "frontend"),
        ("list", ["REQ-002"], "frontend"),
        ("export", ["REQ-003"], "frontend"),
    ]


def test_runs_per_task_counts_sessions_once():
    profile = {
        "workflow": {
            "document": {
                "nodes": [
                    {"id": "planner", "kind": "role", "session": "analysis"},
                    {"id": "architect", "kind": "role", "session": "analysis"},
                    {"id": "gate", "kind": "approval"},
                    {"id": "developer", "kind": "role"},
                ]
            }
        }
    }
    assert studio_handoff.runs_per_task(profile) == 2


def test_criteria_tests_become_requirement_links(canonical_db, studio_root, monkeypatch):
    """Tests named per criterion link to their requirements after verification."""
    from agent_dev.adapters import traceability

    result = register(canonical_db, studio_root, selection(canonical_db, studio_root))
    repo = WorkflowRepository(canonical_db)
    repo.approve("design", result["design_id"], 1, "user")
    repo.approve("plan", result["plan_id"], 1, "user")
    task = canonical_db.rows("SELECT id FROM tasks ORDER BY id")[0]["id"]
    assert traceability.link_criteria_tests(canonical_db, task, "RUN-1") == 0
    repo.event(
        task,
        "CriteriaTestsNamed",
        {
            "node_id": "test-automation",
            "criteria_tests": {
                "REQ-001:AC-01": "src/test_memo.py::test_blank_title",
                "REQ-001:AC-02": "src/test_memo.py::test_saved",
                "REQ-002:AC-01": "src/test_api.py::test_auth",  # not this task's requirement
            },
        },
    )
    calls = []
    monkeypatch.setattr(traceability, "create_link", lambda db, params: calls.append(params))
    assert traceability.link_criteria_tests(canonical_db, task, "RUN-1") == 1
    assert calls[0]["kind"] == "file" and calls[0]["target_ref"] == "src/test_memo.py"
    assert calls[0]["criteria_ids"] == ["REQ-001:AC-01", "REQ-001:AC-02"]
    assert calls[0]["requirement_id"] == "REQ-001"


def test_marker_reviews_can_be_skipped_per_task(canonical_db, studio_root, monkeypatch):
    """A Dockerfile or PostgreSQL dependency added infra and DB reviews to every
    task, with no way to drop them for a feature that does not touch either."""
    monkeypatch.setattr(
        studio_handoff, "workstreams", lambda *_: (["backend", "infra"], ["db"])
    )
    prepared = selection(canonical_db, studio_root)
    task = prepared["selection"]["tasks"][0]
    assert (task["workstreams"], task["risk"], task["skip_reviews"]) == (
        ["backend", "infra"],
        ["db"],
        [],
    )
    tasks = [dict(t, skip_reviews=["infra", "db"]) for t in prepared["selection"]["tasks"]]
    narrowed = selection(canonical_db, studio_root, tasks=tasks)
    task = narrowed["selection"]["tasks"][0]
    assert (task["workstreams"], task["risk"], task["skip_reviews"]) == (
        ["backend"],
        [],
        ["db", "infra"],
    )
    assert narrowed["preview_digest"] != prepared["preview_digest"]
    # The skip survives the echo of its own preview.
    again = selection(canonical_db, studio_root, **narrowed["selection"])
    assert again["preview_digest"] == narrowed["preview_digest"]
    # Frontend and backend follow the stack and cannot be skipped.
    tasks = [dict(t, skip_reviews=["backend"]) for t in prepared["selection"]["tasks"]]
    with pytest.raises(DomainError) as exc:
        selection(canonical_db, studio_root, tasks=tasks)
    assert exc.value.code == ErrorCode.INVALID_INPUT
