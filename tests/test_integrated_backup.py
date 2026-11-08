"""Portable snapshots use real SQLite stores; no provider/service calls."""

import json
import sqlite3
import time
from zipfile import ZipFile

import pytest

from agent_dev.adapters import backup
from agent_dev.adapters.studio_store import StudioStore
from agent_dev.adapters.workspace import initialize
from agent_dev.domain.content import content_digest, digest
from agent_dev.domain.errors import DomainError


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    initialize(root, name="한글 프로젝트")
    return root


def studio(root):
    store = StudioStore(root)
    with store.transaction() as sql:
        sql.execute("INSERT INTO projects VALUES('P','화면','요구사항',1,0,0,?)", (time.time(),))
        for revision in (1, 2):
            store.insert_revision(sql, "specs", "P", {"title": f"명세 {revision}"}, "user")
            store.insert_revision(
                sql, "mocks", "P", {"screens": [{"id": "S", "title": f"화면 {revision}"}]}, revision
            )
        sql.execute(
            "INSERT INTO comments VALUES('C','P',2,'S','E','미해결 의견','open','',0,NULL,?)",
            (time.time(),),
        )
        sql.execute("INSERT INTO approvals VALUES('P',2,'S','hash','확정',?)", (time.time(),))
    return store


def rewrite(source, destination, edit):
    with ZipFile(source) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    manifest = json.loads(files.pop("manifest.json"))
    edit(manifest, files)
    with ZipFile(destination, "w") as archive:
        archive.writestr("manifest.json", json.dumps(manifest))
        for name, data in files.items():
            archive.writestr(name, data)
    return destination


def test_two_database_roundtrip(workspace, tmp_path):
    store = studio(workspace)
    before = store.detail("P")
    archive = tmp_path / "bundle.zip"
    result = backup.create_bundle(workspace, archive)
    manifest = backup.inspect_bundle(archive)["manifest"]
    assert manifest["format_version"] == 2
    assert manifest["studio_present"] is True
    assert set(manifest["databases"]) == {"canonical", "studio"}
    assert result["included"]
    restored = tmp_path / "restored"
    report = backup.restore_bundle(archive, restored)
    after = StudioStore(restored).detail("P")
    for field in ("spec", "mock", "comments", "approvals", "spec_versions", "mock_versions"):
        assert after[field] == before[field]
    assert report["studio_present"] is True
    assert report["execution_disabled"]


def test_studio_only(workspace, tmp_path):
    studio(workspace)
    (workspace / ".agent-dev/state.db").unlink()
    archive = tmp_path / "studio.zip"
    backup.create_bundle(workspace, archive)
    manifest = backup.inspect_bundle(archive)["manifest"]
    assert set(manifest["databases"]) == {"studio"}
    restored = tmp_path / "restored"
    backup.restore_bundle(archive, restored)
    assert not (restored / ".agent-dev/state.db").exists()
    assert StudioStore(restored).detail("P")["comments"][0]["body"] == "미해결 의견"


def test_format1_remains_readable(workspace, tmp_path):
    source = tmp_path / "bundle.zip"
    backup.create_bundle(workspace, source)

    def legacy(manifest, _files):
        manifest["format_version"] = 1
        for key in ("databases", "studio_present", "artifacts"):
            manifest.pop(key, None)

    archive = rewrite(source, tmp_path / "legacy.zip", legacy)
    assert backup.inspect_bundle(archive)["valid"]
    report = backup.restore_bundle(archive, tmp_path / "restored")
    assert report["studio_present"] is False


@pytest.mark.parametrize("kind", ["queued", "running", "lease"])
def test_studio_activity_rejected(workspace, tmp_path, kind):
    store = studio(workspace)
    with store.transaction() as sql:
        if kind == "lease":
            sql.execute("INSERT INTO leases VALUES('preview','other',1,?)", (time.time() + 100,))
        else:
            sql.execute("INSERT INTO jobs VALUES('J','P','mock',?,1,'',?)", (kind, time.time()))
    with pytest.raises(DomainError, match="active|idle|execution"):
        backup.create_bundle(workspace, tmp_path / "bundle.zip")
    assert not (tmp_path / "bundle.zip").exists()


def web_job(workspace, request="self", operation="backup.create", owner="owner"):
    from agent_dev.adapters.state.sqlite.database import Database
    from agent_dev.adapters.state.sqlite.safety import ExecutionSafety
    from agent_dev.adapters.state.sqlite.store import WorkflowRepository

    store = WorkflowRepository(Database(workspace / ".agent-dev/state.db"))
    job = {"id": request, "operation": operation, "owner": owner, "state": "running"}
    with store.db.transaction() as sql:
        sql.execute(
            "INSERT INTO commands VALUES(?, 'digest','running',?,?)",
            ("web:" + request, json.dumps(job), time.time()),
        )
    lease = ExecutionSafety(store).acquire("agent-web:web:" + request, owner, ttl=60)
    return {"request_id": request, "lease": lease}


def test_owned_web_backup_can_exclude_only_itself(workspace, tmp_path):
    identity = web_job(workspace)
    backup.create_bundle(workspace, tmp_path / "self.zip", self_request=identity)
    web_job(workspace, request="other", operation="task.execute")
    with pytest.raises(DomainError):
        backup.create_bundle(workspace, tmp_path / "unrelated.zip", self_request=identity)


@pytest.mark.parametrize("fault", ["owner", "token", "operation", "request"])
def test_invalid_self_identity_rejected(workspace, tmp_path, fault):
    identity = web_job(
        workspace, operation="task.execute" if fault == "operation" else "backup.create"
    )
    if fault == "owner":
        identity["lease"]["holder"] = "impostor"
    if fault == "token":
        identity["lease"]["token"] += 1
    if fault == "request":
        identity["request_id"] = "other"
    with pytest.raises(DomainError):
        backup.create_bundle(workspace, tmp_path / "bundle.zip", self_request=identity)


@pytest.mark.parametrize("state", ["queued", "running"])
def test_canonical_pending_commands_rejected(workspace, tmp_path, state):
    with sqlite3.connect(workspace / ".agent-dev/state.db") as sql:
        sql.execute("INSERT INTO commands VALUES('other','digest',?,NULL,?)", (state, time.time()))
    with pytest.raises(DomainError):
        backup.create_bundle(workspace, tmp_path / "bundle.zip")


@pytest.mark.parametrize("fault", ["missing", "corrupt", "schema", "unknown-table", "fk"])
def test_studio_manifest_and_database_corruption_rejected(workspace, tmp_path, fault):
    studio(workspace)
    source = tmp_path / "bundle.zip"
    backup.create_bundle(workspace, source)

    def mutate(manifest, files):
        path = ".agent-dev/studio/studio.db"
        if fault == "missing":
            files.pop(path)
            manifest["entries"] = [e for e in manifest["entries"] if e["path"] != path]
            return
        raw = tmp_path / "damaged.db"
        raw.write_bytes(files[path])
        if fault == "corrupt":
            raw.write_bytes(b"not sqlite")
        else:
            with sqlite3.connect(raw) as sql:
                if fault == "schema":
                    sql.execute("PRAGMA user_version=99")
                elif fault == "unknown-table":
                    sql.execute("CREATE TABLE unexpected(secret TEXT)")
                else:
                    sql.execute("UPDATE comments SET project_id='missing'")
                sql.commit()
                sql.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        files[path] = raw.read_bytes()
        entry = next(e for e in manifest["entries"] if e["path"] == path)
        entry.update(size=len(files[path]), sha256=content_digest(files[path]))
        manifest["databases"]["studio"].update(size=entry["size"], sha256=entry["sha256"])

    corrupt = rewrite(source, tmp_path / "bad.zip", mutate)
    for check in (
        lambda: backup.inspect_bundle(corrupt),
        lambda: backup.restore_bundle(corrupt, tmp_path / "restored"),
    ):
        with pytest.raises(DomainError):
            check()
    assert not (tmp_path / "restored").exists()


def artifacts(workspace):
    connection = "figma"
    content = {"title": "설계 참고"}
    sha = digest(content)
    integration = {
        "provider": "figma",
        "connection_id": connection,
        "operation": "get_metadata",
        "scope": "file",
        "trust": "untrusted_data",
        "content": content,
        "digest": sha,
        "fetched_at": time.time(),
    }
    reference = {
        "connection_id": connection,
        "source_digest": sha,
        "content": content,
        "trust": "untrusted_data",
        "fetched_at": integration["fetched_at"],
        "locator": {"file_key": "file"},
        "truncated": False,
    }
    reference["id"] = digest(reference)
    from agent_dev.adapters.state.sqlite.database import Database

    with Database(workspace / ".agent-dev/state.db").transaction() as sql:
        project = sql.execute("SELECT id FROM projects").fetchone()[0]
        sql.execute(
            "INSERT INTO tool_connections VALUES(?,?,?)",
            (
                project + ":figma",
                project,
                json.dumps({"id": "figma", "provider": "figma", "integration": True}),
            ),
        )
        sql.execute(
            "INSERT INTO events VALUES(?,?,?,?,?,?)",
            (
                "integration-read",
                project,
                99,
                "IntegrationRead",
                json.dumps({k: v for k, v in integration.items() if k != "content"}),
                time.time(),
            ),
        )
    integration_path = workspace / ".agent-dev/integration-artifacts" / connection / (sha + ".json")
    reference_path = workspace / ".agent-dev/design-references" / (reference["id"] + ".json")
    for path, value in ((integration_path, integration), (reference_path, reference)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return integration_path, reference_path


def test_provenance_artifacts_roundtrip(workspace, tmp_path):
    paths = artifacts(workspace)
    archive = tmp_path / "bundle.zip"
    backup.create_bundle(workspace, archive)
    manifest = backup.inspect_bundle(archive)["manifest"]
    assert len(manifest["artifacts"]) == 2
    destination = tmp_path / "restored"
    backup.restore_bundle(archive, destination)
    for path in paths:
        assert (destination / path.relative_to(workspace)).read_bytes() == path.read_bytes()


@pytest.mark.parametrize("fault", ["digest", "provenance", "symlink", "secret", "oversize"])
def test_invalid_artifact_rejected(workspace, tmp_path, monkeypatch, fault):
    path, _reference = artifacts(workspace)
    if fault == "symlink":
        original = tmp_path / "original.json"
        path.rename(original)
        path.symlink_to(original)
    elif fault == "oversize":
        monkeypatch.setattr(backup, "MAX_FILE", 20)
    else:
        value = json.loads(path.read_text(encoding="utf-8"))
        if fault == "digest":
            value["content"]["title"] = "changed"
        elif fault == "provenance":
            value["operation"] = "unregistered"
        else:
            value["content"]["api_key"] = "sensitive-value"
        path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(DomainError):
        backup.create_bundle(workspace, tmp_path / "bad.zip")
    assert not (tmp_path / "bad.zip").exists()


def test_restore_refuses_any_existing_destination(workspace, tmp_path):
    archive = tmp_path / "bundle.zip"
    backup.create_bundle(workspace, archive)
    destination = tmp_path / "existing"
    destination.mkdir()
    with pytest.raises(DomainError):
        backup.restore_bundle(archive, destination)
    assert list(destination.iterdir()) == []


def test_web_backup_and_restore_check(workspace):
    from agent_dev.adapters.agent_console import AgentConsole
    from tests.test_agent_console import checked, finished

    studio(workspace)
    console = AgentConsole(workspace)
    try:
        payload = {
            "operation": "backup.create",
            "fields": {"destination": "portable.zip"},
            "options": {},
        }
        preview = console.preview(payload)
        assert "studio" in preview["included"]
        assert preview["excluded"]
        console.start(checked(console, "backup.create", payload["fields"], request="backup-web"))
        created = finished(console, "backup-web")
        assert created["state"] == "completed", created
        restored_fields = {"source": "portable.zip", "destination": "restore-check"}
        preview = console.preview(
            {"operation": "backup.restore", "fields": restored_fields, "options": {}}
        )
        assert preview["restore_check"] is True
        console.start(checked(console, "backup.restore", restored_fields, request="restore-web"))
        restored = finished(console, "restore-web")
        assert restored["state"] == "completed", restored
        assert restored["result"]["manifest"]["studio_present"]
        assert StudioStore(workspace).detail("P")["comments"][0]["state"] == "open"
    finally:
        console.close()


@pytest.mark.parametrize("kind", ["canonical", "studio"])
def test_busy_writer_timeout_is_clear_and_leaves_no_archive(workspace, tmp_path, kind):
    store = studio(workspace)
    path = workspace / ".agent-dev/state.db" if kind == "canonical" else store.path
    import contextlib

    with contextlib.closing(sqlite3.connect(path)) as sql:
        sql.execute("BEGIN IMMEDIATE")
        with pytest.raises(DomainError, match="busy.*retry"):
            backup.create_bundle(workspace, tmp_path / "busy.zip")
    assert not (tmp_path / "busy.zip").exists()


@pytest.mark.parametrize(
    "fault", ["missing-manifest", "bad-manifest", "duplicates", "oversize", "schema-digest"]
)
def test_invalid_manifests_are_rejected_before_install(workspace, tmp_path, monkeypatch, fault):
    source = tmp_path / "original.zip"
    backup.create_bundle(workspace, source)
    bad = tmp_path / "bad.zip"
    if fault in {"missing-manifest", "bad-manifest", "duplicates"}:
        with ZipFile(source) as archive, ZipFile(bad, "w") as target:
            for name in archive.namelist():
                if name == "manifest.json" and fault == "missing-manifest":
                    continue
                target.writestr(
                    name,
                    b"not json"
                    if name == "manifest.json" and fault == "bad-manifest"
                    else archive.read(name),
                )
            if fault == "duplicates":
                with pytest.warns(UserWarning, match="Duplicate name"):
                    target.writestr(".agent-dev/workspace.json", b"{}")
    else:

        def edit(manifest, _files):
            if fault == "schema-digest":
                manifest["databases"]["canonical"]["sha256"] = "0" * 64

        rewrite(source, bad, edit)
        if fault == "oversize":
            monkeypatch.setattr(backup, "MAX_BYTES", 1)
    with pytest.raises(DomainError):
        backup.restore_bundle(bad, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_backup_rejects_symlink_database_parent(workspace, tmp_path):
    store = studio(workspace)
    directory = store.directory
    outside = tmp_path / "external-studio"
    directory.rename(outside)
    directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(DomainError):
        backup.create_bundle(workspace, tmp_path / "bad.zip")


def test_canonical_schema3_requires_installed_schema(workspace, tmp_path):
    with sqlite3.connect(workspace / ".agent-dev/state.db") as sql:
        assert sql.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0] == 4
        sql.execute("DROP TABLE requirement_change_impacts")
    archive = tmp_path / "v3.zip"
    with pytest.raises(DomainError, match="schema"):
        backup.create_bundle(workspace, archive)
    assert not archive.exists()


def test_bare_studio_only_workspace(tmp_path):
    root = tmp_path / "studio-project"
    studio(root)
    archive = tmp_path / "studio.zip"
    backup.create_bundle(root, archive)
    destination = tmp_path / "restored"
    backup.restore_bundle(archive, destination)
    assert StudioStore(destination).detail("P")["project"]["title"] == "화면"


def test_changed_artifact_during_capture_rejected(workspace, tmp_path, monkeypatch):
    path, _reference = artifacts(workspace)
    original = backup._portable_artifacts

    def capture(root, canonical, **options):
        result = original(root, canonical, **options)
        value = json.loads(path.read_text(encoding="utf-8"))
        value["content"]["title"] = "changed concurrently"
        path.write_text(json.dumps(value), encoding="utf-8")
        return result

    monkeypatch.setattr(backup, "_portable_artifacts", capture)
    with pytest.raises(DomainError, match="changed"):
        backup.create_bundle(workspace, tmp_path / "bad.zip")


def test_hostile_trigger_rejected_before_restore_mutations(workspace, tmp_path):
    source = tmp_path / "original.zip"
    backup.create_bundle(workspace, source)

    def inject(manifest, files):
        name = ".agent-dev/state.db"
        raw = tmp_path / "injected.db"
        raw.write_bytes(files[name])
        with sqlite3.connect(raw) as sql:
            sql.execute(
                "CREATE TRIGGER grant_auth AFTER UPDATE ON commands BEGIN UPDATE tool_connections SET content_json='{}'; END"
            )
            sql.commit()
            sql.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        files[name] = raw.read_bytes()
        entry = next(e for e in manifest["entries"] if e["path"] == name)
        entry.update(size=len(files[name]), sha256=content_digest(files[name]))
        manifest["databases"]["canonical"].update(size=entry["size"], sha256=entry["sha256"])

    hostile = rewrite(source, tmp_path / "hostile.zip", inject)
    with pytest.raises(DomainError):
        backup.restore_bundle(hostile, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_staged_evidence_must_match_database_artifact_digest(workspace, tmp_path):
    from agent_dev.adapters.state.sqlite.database import Database

    evidence = workspace / "output/Example/T/notes.md"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("검증된 근거", encoding="utf-8")
    with Database(workspace / ".agent-dev/state.db").transaction() as sql:
        project = sql.execute("SELECT id FROM projects").fetchone()[0]
        sql.execute(
            "INSERT INTO artifacts VALUES(?,?,NULL,NULL,?,?,?, ?,?)",
            (
                "A",
                project,
                "Example/T/notes.md",
                content_digest(evidence.read_bytes()),
                "evidence",
                "trusted",
                time.time(),
            ),
        )
    source = tmp_path / "original.zip"
    backup.create_bundle(workspace, source)

    def mutate(manifest, files):
        name = "output/Example/T/notes.md"
        files[name] = b"tampered evidence"
        entry = next(e for e in manifest["entries"] if e["path"] == name)
        entry.update(size=len(files[name]), sha256=content_digest(files[name]))

    bad = rewrite(source, tmp_path / "tampered.zip", mutate)
    with pytest.raises(DomainError):
        backup.restore_bundle(bad, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()


def test_design_reference_content_must_derive_from_source(workspace, tmp_path):
    _integration, path = artifacts(workspace)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["content"] = {"title": "unrelated content"}
    value["id"] = digest({key: item for key, item in value.items() if key != "id"})
    path.unlink()
    path.with_name(value["id"] + ".json").write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(DomainError):
        backup.create_bundle(workspace, tmp_path / "bad.zip")


def test_valid_truncated_design_reference_is_portable(workspace, tmp_path):
    from agent_dev.domain.content import canonical

    _integration, path = artifacts(workspace)
    value = json.loads(path.read_text(encoding="utf-8"))
    value["content"] = canonical(value["content"])[:10]
    value["truncated"] = True
    value["id"] = digest({key: item for key, item in value.items() if key != "id"})
    path.unlink()
    path.with_name(value["id"] + ".json").write_text(json.dumps(value), encoding="utf-8")
    archive = tmp_path / "truncated.zip"
    backup.create_bundle(workspace, archive)
    assert backup.inspect_bundle(archive)["valid"]


def test_windows_restore_installs_into_nonexistent_destination(workspace, tmp_path, monkeypatch):
    import sys

    source = tmp_path / "original.zip"
    backup.create_bundle(workspace, source)
    destination = tmp_path / "restored"
    real_rename = backup.os.rename
    calls = []

    def windows_rename(source, target):
        assert not target.exists(), "Windows directory rename requires a nonexistent target"
        calls.append((source, target))
        return real_rename(source, target)

    def windows_replace(source, target):
        if target.exists():
            raise PermissionError("Windows cannot replace an existing directory")
        return real_rename(source, target)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(backup.os, "rename", windows_rename)
    monkeypatch.setattr(backup.os, "replace", windows_replace)
    backup.restore_bundle(source, destination)
    assert calls
    assert (destination / ".agent-dev/state.db").is_file()


@pytest.mark.parametrize("kind", ["empty", "nonempty", "symlink"])
def test_atomic_install_never_overwrites_racing_destination(workspace, tmp_path, monkeypatch, kind):
    source = tmp_path / "original.zip"
    backup.create_bundle(workspace, source)
    destination = tmp_path / "restored"
    real_install = getattr(backup, "_atomic_install", None)
    assert real_install is not None, "Restore needs a single atomic no-replace install boundary"

    def raced_install(stage, target):
        if kind == "symlink":
            external = tmp_path / "external"
            external.mkdir()
            target.symlink_to(external, target_is_directory=True)
        else:
            target.mkdir()
            if kind == "nonempty":
                (target / "owned-by-other").write_text("preserve", encoding="utf-8")
        return real_install(stage, target)

    monkeypatch.setattr(backup, "_atomic_install", raced_install)
    with pytest.raises(DomainError, match="exists|overwritten"):
        backup.restore_bundle(source, destination)
    if kind == "nonempty":
        assert (destination / "owned-by-other").read_text(encoding="utf-8") == "preserve"
    elif kind == "symlink":
        assert destination.is_symlink()
    else:
        assert list(destination.iterdir()) == []
    assert not (destination / ".agent-dev").exists()


def slow_backup_dispatch(args):
    """Real dispatch with a deterministic slow capture, used by a spawned worker."""
    from agent_dev.adapters.command_line import dispatch

    original = backup._portable_artifacts

    def slow_capture(*values, **options):
        time.sleep(23)
        return original(*values, **options)

    backup._portable_artifacts = slow_capture
    return dispatch(args)


def test_actual_web_worker_survives_capture_longer_than_twenty_seconds(workspace, monkeypatch):
    from agent_dev.adapters.agent_console import AgentConsole
    from tests.test_agent_console import checked

    console = AgentConsole(workspace, dispatcher=slow_backup_dispatch)
    try:
        console.start(
            checked(console, "backup.create", {"destination": "slow.zip"}, request="slow")
        )
        deadline = time.monotonic() + 10
        status = None
        while time.monotonic() < deadline:
            status = getattr(console, "capture_progress", {}).get("slow")
            if status is not None and status.value > 0:
                break
            time.sleep(0.05)
        assert status is not None and status.value > 0, (
            "Own capture progress must stay visible across TTL"
        )
        parent_clock = time.time
        monkeypatch.setattr(time, "time", lambda: parent_clock() + 120)
        assert next(row for row in console.history() if row["id"] == "slow")["state"] == "running"
        monkeypatch.setattr(time, "time", parent_clock)
        deadline = time.monotonic() + 40
        job = {}
        while time.monotonic() < deadline:
            job = next((row for row in console.history() if row["id"] == "slow"), {})
            if job.get("state") not in {"queued", "running"}:
                break
            time.sleep(0.1)
        assert job.get("state") == "completed", job
        assert (console.artifacts / "slow.zip").is_file()
    finally:
        console.close()


@pytest.mark.parametrize("fault", ["expired", "stolen"])
def test_web_backup_rejects_ownership_loss_during_capture(workspace, tmp_path, monkeypatch, fault):
    identity = web_job(workspace)
    original = backup._quiescent
    calls = []

    def lose_ownership(sql, kind, self_request=None):
        if kind == "canonical" and self_request:
            calls.append(kind)
            if len(calls) == 2:
                if fault == "expired":
                    sql.execute(
                        "UPDATE leases SET expires_at=0 WHERE resource=?",
                        (identity["lease"]["resource"],),
                    )
                else:
                    sql.execute(
                        "UPDATE leases SET holder='other',token=token+1 WHERE resource=?",
                        (identity["lease"]["resource"],),
                    )
        return original(sql, kind, self_request)

    monkeypatch.setattr(backup, "_quiescent", lose_ownership)
    with pytest.raises(DomainError, match="identity|owned"):
        backup.create_bundle(workspace, tmp_path / "lost.zip", self_request=identity)
    assert not (tmp_path / "lost.zip").exists()


def test_capture_progress_renews_own_lease_beyond_initial_ttl(workspace, tmp_path, monkeypatch):
    import contextlib

    identity = web_job(workspace)
    clock = [time.time()]
    initial = clock[0]
    monkeypatch.setattr(backup.time, "time", lambda: clock[0])

    @contextlib.contextmanager
    def capture_guard():
        def progress():
            clock[0] += 20

        yield progress

    archive = tmp_path / "long.zip"
    backup.create_bundle(workspace, archive, self_request=identity, capture_guard=capture_guard)
    assert clock[0] > initial + 60
    with sqlite3.connect(workspace / ".agent-dev/state.db") as sql:
        assert (
            sql.execute(
                "SELECT expires_at FROM leases WHERE resource=?", (identity["lease"]["resource"],)
            ).fetchone()[0]
            > clock[0]
        )
    assert backup.inspect_bundle(archive)["valid"]


@pytest.mark.parametrize("fault", ["index", "trigger", "duplicate"])
def test_missing_schema_objects_fail_create_inspect_and_restore(workspace, tmp_path, fault):
    archive = tmp_path / "good.zip"
    backup.create_bundle(workspace, archive)

    def damage(sql):
        if fault == "trigger":
            sql.execute("DROP TRIGGER immutable_snapshots")
        else:
            sql.execute("DROP INDEX attempt_idempotency")
            if fault == "duplicate":
                for number in range(2):
                    sql.execute(
                        "INSERT INTO phase_attempts VALUES(?,NULL,?,1,0,?,?,?,?,?,?)",
                        (
                            str(number),
                            "developer",
                            "passed",
                            "same-key",
                            "task:T",
                            1,
                            time.time(),
                            time.time(),
                        ),
                    )

    def mutate(manifest, files):
        name = ".agent-dev/state.db"
        path = tmp_path / "damaged.db"
        path.write_bytes(files[name])
        with sqlite3.connect(path) as sql:
            damage(sql)
            sql.commit()
            sql.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        files[name] = path.read_bytes()
        entry = next(row for row in manifest["entries"] if row["path"] == name)
        entry.update(size=len(files[name]), sha256=content_digest(files[name]))
        manifest["databases"]["canonical"].update(size=entry["size"], sha256=entry["sha256"])

    hostile = rewrite(archive, tmp_path / "bad.zip", mutate)
    with pytest.raises(DomainError):
        backup.inspect_bundle(hostile)
    with pytest.raises(DomainError):
        backup.restore_bundle(hostile, tmp_path / "restored")
    assert not (tmp_path / "restored").exists()
    with sqlite3.connect(workspace / ".agent-dev/state.db") as sql:
        damage(sql)
    with pytest.raises(DomainError):
        backup.create_bundle(workspace, tmp_path / "rejected.zip")


def test_capture_watchdog_rejects_stalled_progress(workspace, monkeypatch):
    from agent_dev.adapters import backup_capture
    from agent_dev.adapters.state.sqlite.database import Database
    from agent_dev.adapters.state.sqlite.safety import ExecutionSafety
    from agent_dev.adapters.state.sqlite.store import WorkflowRepository

    identity = web_job(workspace)
    safety = ExecutionSafety(WorkflowRepository(Database(workspace / ".agent-dev/state.db")))
    guard = backup_capture.WebBackupCapture(safety, identity["lease"])
    clock = [0.0]
    monkeypatch.setattr(backup_capture.time, "monotonic", lambda: clock[0])
    with guard.hold():
        clock[0] = 31
        with pytest.raises(DomainError, match="progress|stalled"):
            guard.heartbeat()


def test_active_web_capture_remains_cancellable_after_visible_ttl(workspace, monkeypatch):
    from agent_dev.adapters.agent_console import AgentConsole
    from tests.test_agent_console import checked

    console = AgentConsole(workspace, dispatcher=slow_backup_dispatch)
    try:
        console.start(
            checked(console, "backup.create", {"destination": "cancelled.zip"}, request="cancel")
        )
        deadline = time.monotonic() + 10
        status = None
        while time.monotonic() < deadline:
            status = console.capture_progress.get("cancel")
            if status is not None and status.value > 0:
                break
            time.sleep(0.05)
        assert status is not None and status.value > 0
        parent_clock = time.time
        monkeypatch.setattr(time, "time", lambda: parent_clock() + 120)
        job = console.cancel("cancel")
        assert job["state"] == "cancelled"
        assert not console.processes["cancel"][0].is_alive()
        assert not (console.artifacts / "cancelled.zip").exists()
    finally:
        console.close()


def test_version1_can_omit_genuine_newer_table_objects(workspace, tmp_path):
    with sqlite3.connect(workspace / ".agent-dev/state.db") as sql:
        for table in (
            "workspace_settings",
            "workflow_node_approvals",
            "workflow_node_runs",
            "workflow_versions",
            "workflow_definitions",
        ):
            sql.execute("DROP TABLE " + table)
        sql.execute("DELETE FROM schema_versions WHERE version > 1")
    archive = tmp_path / "legacy-v1.zip"
    backup.create_bundle(workspace, archive)
    assert backup.inspect_bundle(archive)["valid"]
    backup.restore_bundle(archive, tmp_path / "restored")


def test_history_does_not_mix_running_command_with_completed_lease(workspace, monkeypatch):
    from agent_dev.adapters.agent_console import AgentConsole
    from agent_dev.adapters.state.sqlite.database import Database

    console = AgentConsole(workspace)
    identity = web_job(workspace)
    rows = Database.rows
    raced = [False]

    def finish_between_reads(db, query, parameters=()):
        result = rows(db, query, parameters)
        if "FROM commands" in query and not raced[0]:
            raced[0] = True
            with sqlite3.connect(db.path) as sql:
                sql.execute("UPDATE commands SET state='completed' WHERE key='web:self'")
                sql.execute(
                    "UPDATE leases SET expires_at=0 WHERE resource=?",
                    (identity["lease"]["resource"],),
                )
        return result

    monkeypatch.setattr(Database, "rows", finish_between_reads)
    assert console.history()[0]["state"] in {"running", "completed"}
    assert console.history()[0]["state"] == "completed"
