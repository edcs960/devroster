"""Quiescent, integrity-checked portable workspace snapshots; never source or credentials."""

import contextlib
import ctypes
import errno
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import NoReturn
from zipfile import ZIP_DEFLATED, BadZipFile, ZipFile

from agent_dev.adapters.secrets import registered_secrets
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.adapters.workspace import configuration_root, metadata
from agent_dev.domain.content import canonical as canonical_json
from agent_dev.domain.content import content_digest, digest, redact
from agent_dev.domain.errors import DomainError, ErrorCode

MAX_BYTES = 512 * 1024 * 1024
MAX_FILE = 64 * 1024 * 1024
MAX_FILES = 10000
DB_PATHS = {"canonical": ".agent-dev/state.db", "studio": ".agent-dev/studio/studio.db"}
V3_TABLES = {
    "handoff_imports",
    "imported_requirements",
    "requirement_links",
    "preview_sessions",
    "review_threads",
    "review_decisions",
    "requirement_changes",
    "requirement_change_impacts",
}
# Tables added within schema v3; v3 backups taken before them stay restorable.
ADDED_IN_V3 = {"knowledge_revisions"}
# Tables of the additive schema v4; every v4 database has them.
V4_TABLES = {"configuration_provenance", "shared_connection_bindings"}
EXCLUDED = [
    "credentials/.env/.mcp.json/OAuth secrets",
    "source repositories",
    "worktrees",
    "dependencies",
    "process registry",
    "environment values",
    "unregistered caches",
]


# Marker fields that tie a workspace to one device's folders; never portable.
LOCATION_KEYS = ("workspace_id", "source_root", "runtime_root")


def physical_sources(root, workspace):
    """Bundle kind -> physical database, the folder bounding it, and the cache folder.

    Bundles keep the project-local logical paths (``.agent-dev/...``); an external
    workspace resolves them to its configuration folder instead.
    """
    from agent_dev.adapters.storage_registry import workspace_locations
    from agent_dev.core.storage_locations import EXTERNAL

    locations = workspace_locations(root)
    if locations.layout == EXTERNAL:
        databases = {"canonical": locations.state_db, "studio": locations.studio_db}
        return databases, locations.config_root, locations.cache_root
    databases = {kind: workspace / name for kind, name in DB_PATHS.items()}
    return databases, workspace, workspace / ".agent-dev"


def reject(message) -> NoReturn:
    raise DomainError(ErrorCode.POLICY_VIOLATION, message)


def safe_name(name):
    if (
        not isinstance(name, str)
        or not name
        or len(name) > 500
        or any(c in name for c in ("\\", ":", "\x00"))
    ):
        reject("Invalid bundle path.")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
        reject("Bundle path escaped its managed scope.")
    if path.parts[0] == "output" and len(path.parts) >= 3:
        if any(part.startswith(".") for part in path.parts[1:]):
            reject("Hidden files are not portable evidence.")
        return
    if name in {".agent-dev/workspace.json", ".agent-dev/README.md", *DB_PATHS.values()}:
        return
    if (
        len(path.parts) >= 3
        and path.parts[:2]
        in {(".agent-dev", "config"), (".agent-dev", "starter"), (".agent-dev", "data")}
        and path.suffix in {".json", ".md"}
        and not any(part.startswith(".") for part in path.parts[2:])
    ):
        return
    if (
        len(path.parts) == 3
        and path.parts[:2] == (".agent-dev", "design-references")
        and re.fullmatch(r"[a-f0-9]{64}\.json", path.name)
    ):
        return
    if (
        len(path.parts) == 4
        and path.parts[:2] == (".agent-dev", "integration-artifacts")
        and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", path.parts[2])
        and re.fullmatch(r"[a-f0-9]{64}\.json", path.name)
    ):
        return
    reject("Bundle contains files outside configuration and task evidence.")


def changed_secret(text, secrets):
    try:
        value = json.loads(text)
    except ValueError:
        value = text
    return redact(value, secrets) != value


def _regular(path, boundary):
    if (
        not path.is_file()
        or path.is_symlink()
        or not path.resolve().is_relative_to(boundary.resolve())
        or any(parent.is_symlink() for parent in path.parents if parent.is_relative_to(boundary))
    ):
        reject("Backup inputs must be regular files without symlink ancestors.")


def validate_database(path, secrets=(), *, kind="canonical", progress=lambda: None):
    """Validate without initializing, migrating or repairing the supplied database."""
    from agent_dev.adapters.studio_store import SCHEMA

    try:
        with contextlib.closing(
            sqlite3.connect(Path(path).as_uri() + "?mode=ro", uri=True, timeout=10)
        ) as sql:
            sql.set_progress_handler(lambda: (progress(), 0)[1], 10000)
            sql.execute("PRAGMA trusted_schema=OFF")
            if (
                sql.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                or sql.execute("PRAGMA foreign_key_check").fetchone()
            ):
                reject("Backup database integrity check failed.")
            tables = {
                r[0]
                for r in sql.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            schema = (
                SCHEMA
                if kind == "studio"
                else Path(__file__).with_name("state").joinpath("sqlite/schema.sql").read_text(encoding="utf-8")
            )
            with contextlib.closing(sqlite3.connect(":memory:")) as reference:
                reference.executescript(schema)
                known = {
                    r[0]
                    for r in reference.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                if kind == "studio":
                    version = sql.execute("PRAGMA user_version").fetchone()[0]
                    required = known
                    supported = {1}
                else:
                    if "schema_versions" not in tables:
                        reject("Unsupported backup database schema.")
                    version = sql.execute("SELECT MAX(version) FROM schema_versions").fetchone()[0]
                    # v1 predates the workflow graph and workspace settings tables.
                    optional_v1 = {
                        "workspace_settings",
                        "workflow_definitions",
                        "workflow_versions",
                        "workflow_node_runs",
                        "workflow_node_approvals",
                    }
                    optional_before_v3 = V3_TABLES | ADDED_IN_V3 | V4_TABLES
                    required = (
                        known - optional_v1 - optional_before_v3
                        if version == 1
                        else known - optional_before_v3
                        if version == 2
                        else known - ADDED_IN_V3 - V4_TABLES
                        if version == 3
                        else known
                    )
                    supported = {1, 2, 3, 4} if V3_TABLES.issubset(known) else {1, 2}
                if version not in supported or not required.issubset(tables) or tables - known:
                    reject("Unsupported backup database schema or unknown tables.")
                known_objects = {
                    (r[0], r[1]): " ".join((r[2] or "").split())
                    for r in reference.execute(
                        "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                    )
                }
                actual_objects = {
                    (r[0], r[1]): " ".join((r[2] or "").split())
                    for r in sql.execute(
                        "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                    )
                }
                # v1/v2 may omit newer optional tables, never their own required guards.
                required_objects = {
                    (r[0], r[1])
                    for r in reference.execute(
                        "SELECT type,name,tbl_name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                    )
                    if r[2] in tables and not (kind == "canonical" and version < 3 and r[1] in {"immutable_events_v3", "immutable_events_delete_v3"})
                }
                if not required_objects.issubset(actual_objects):
                    reject("Backup database is missing required schema objects.")
                for object_type, name, ddl in sql.execute(
                    "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                ):
                    if known_objects.get((object_type, name)) != " ".join((ddl or "").split()):
                        reject("Unsupported or modified backup schema object.")
                for table in tables:
                    progress()
                    quoted = '"' + table.replace('"', '""') + '"'
                    if (
                        sql.execute(f"PRAGMA table_info({quoted})").fetchall()
                        != reference.execute(f"PRAGMA table_info({quoted})").fetchall()
                    ):
                        reject("Unsupported backup table schema.")
                    for row in sql.execute(f"SELECT * FROM {quoted}"):
                        if any(
                            isinstance(value, str) and changed_secret(value, secrets)
                            for value in row
                        ):
                            reject(
                                "Database contains unredacted secret material; correct the original input before backup."
                            )
            return version
    except sqlite3.Error:
        reject("Backup database integrity/schema check failed.")


def _quiescent(sql, kind, self_request=None):
    own_key, own_resource = "", ""
    now = time.time()
    if self_request is not None:
        if kind != "canonical" or not isinstance(self_request, dict):
            reject("Invalid web backup identity.")
        request = self_request.get("request_id")
        lease = self_request.get("lease", {})
        if not isinstance(request, str) or not isinstance(lease, dict):
            reject("Invalid web backup identity.")
        own_key, own_resource = "web:" + request, "agent-web:web:" + request
        row = sql.execute(
            "SELECT state,result_json FROM commands WHERE key=?", (own_key,)
        ).fetchone()
        actual = sql.execute(
            "SELECT holder,token,expires_at FROM leases WHERE resource=?", (own_resource,)
        ).fetchone()
        job = json.loads(row[1]) if row and row[1] else {}
        if (
            not row
            or row[0] != "running"
            or job.get("state") != "running"
            or job.get("id") != request
            or job.get("operation") != "backup.create"
            or not actual
            or actual[2] <= now
            or lease.get("resource") != own_resource
            or lease.get("holder") != actual[0]
            or lease.get("token") != actual[1]
            or job.get("owner") != actual[0]
        ):
            reject("Invalid or unowned web backup command/lease identity.")
    active = sql.execute(
        "SELECT 1 FROM leases WHERE expires_at>? AND resource<>? LIMIT 1", (now, own_resource)
    ).fetchone()
    if kind == "studio":
        busy = sql.execute(
            "SELECT 1 FROM jobs WHERE state IN('queued','running') LIMIT 1"
        ).fetchone()
    else:
        busy = any(
            sql.execute(query, parameters).fetchone()
            for query, parameters in [
                ("SELECT 1 FROM tasks WHERE state IN('running','verifying') LIMIT 1", ()),
                (
                    "SELECT 1 FROM commands WHERE state IN('queued','running') AND key<>? LIMIT 1",
                    (own_key,),
                ),
                ("SELECT 1 FROM phase_attempts WHERE state IN('queued','running') LIMIT 1", ()),
            ]
        )
        tables = {r[0] for r in sql.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "workflow_node_runs" in tables:
            busy = (
                busy
                or sql.execute(
                    "SELECT 1 FROM workflow_node_runs WHERE state='running' LIMIT 1"
                ).fetchone()
            )
        if "preview_sessions" in tables:
            busy = (
                busy
                or sql.execute(
                    "SELECT 1 FROM preview_sessions WHERE state IN('preparing','active') LIMIT 1"
                ).fetchone()
            )
    if active or busy:
        reject("Workspace has active execution/jobs or leases; finish/recover it before backup.")


def _portable_artifacts(workspace, canonical, *, progress=lambda: None, cache=None):
    """Only cache records with validated content and persisted read provenance are portable.

    Records are keyed by their logical ``.agent-dev/<folder>/...`` bundle path.
    """
    cache = workspace / ".agent-dev" if cache is None else cache
    boundary = workspace if cache.is_relative_to(workspace) else cache
    records, reads, connections = {}, [], {}
    if canonical:
        db = Database(canonical, read_only=True)
        reads = [
            json.loads(r["payload_json"])
            for r in db.rows("SELECT payload_json FROM events WHERE kind='IntegrationRead'")
        ]
        connections = {
            value["id"]: value
            for value in (
                json.loads(r["content_json"])
                for r in db.rows("SELECT content_json FROM tool_connections")
            )
            if value.get("integration") is True and "id" in value
        }
    for directory in ("integration-artifacts", "design-references"):
        base = cache / directory
        if base.is_symlink():
            reject("Artifact directory contains a symlink.")
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            progress()
            if path.is_symlink():
                reject("Artifact contains a symlink.")
            if not path.is_file() or path.suffix != ".json":
                continue
            _regular(path, boundary)
            name = ".agent-dev/" + path.relative_to(cache).as_posix()
            safe_name(name)
            limit = min(MAX_FILE, 100_000 if directory == "design-references" else 3_000_000)
            if path.stat().st_size > limit:
                reject("Artifact exceeds the size limit.")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("trust") != "untrusted_data":
                reject("Artifact provenance is invalid.")
            if directory == "integration-artifacts":
                connection = value.get("connection_id")
                keys = (
                    "provider",
                    "connection_id",
                    "operation",
                    "scope",
                    "trust",
                    "digest",
                    "fetched_at",
                )
                if (
                    path.parent.name != connection
                    or value.get("digest") != path.stem
                    or digest(value.get("content")) != path.stem
                    or connection not in connections
                    or connections[connection].get("provider") != value.get("provider")
                    or not any(all(read.get(k) == value.get(k) for k in keys) for read in reads)
                ):
                    reject("Artifact digest or persisted provenance differs.")
            else:
                source = records.get(
                    ".agent-dev/integration-artifacts/"
                    + str(value.get("connection_id"))
                    + "/"
                    + str(value.get("source_digest"))
                    + ".json"
                )
                if (
                    value.get("id") != path.stem
                    or digest({k: v for k, v in value.items() if k != "id"}) != path.stem
                    or not source
                    or source.get("fetched_at") != value.get("fetched_at")
                    or not (
                        value.get("content") == source.get("content")
                        or value.get("truncated") is True
                        and isinstance(value.get("content"), str)
                        and canonical_json(source.get("content"))
                        .encode()
                        .startswith(value["content"].encode())
                    )
                ):
                    reject("Design reference digest or source provenance differs.")
            records[name] = value
    return records


def create_bundle(root: Path, destination: Path, *, self_request=None, capture_guard=None):
    from agent_dev.adapters.workflow_runtime import project_root
    from agent_dev.core.output_paths import slugify

    if capture_guard is not None and self_request is None:
        reject("Web capture coordination requires a validated web backup identity.")
    root = configuration_root(root)
    workspace = project_root(root)
    if (root / "upgrade-journal.json").exists():
        reject("Recover the interrupted upgrade before creating a backup.")
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink() or destination.suffix.lower() != ".zip":
        reject("Use a fresh .zip backup destination.")
    destination = destination.resolve()
    web_destination = (
        bool(self_request) and destination.parent == workspace / ".agent-dev/web-artifacts"
    )
    if (destination.is_relative_to(root) and not web_destination) or destination.is_relative_to(
        workspace / "output"
    ):
        reject("Store backups outside managed configuration and output directories.")
    sources, database_boundary, cache = physical_sources(root, workspace)
    databases = {
        kind: path for kind, path in sources.items() if path.exists() or path.is_symlink()
    }
    if not databases:
        reject("No canonical or Studio database to back up; initialize the workspace first.")
    for path in databases.values():
        _regular(path, database_boundary)
    if self_request and "canonical" not in databases:
        reject("Web backup identity requires its canonical command database.")
    secrets = registered_secrets(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(
            prefix="agent-dev-backup-", dir=destination.parent
        ) as folder:
            stage, entries, originals, db_manifest = Path(folder), [], [], {}
            payload = stage / "payload"
            payload.mkdir(mode=0o700)
            total = 0
            progress = lambda: None

            def read(source):
                chunks = []
                with source.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        progress()
                        chunks.append(chunk)
                return b"".join(chunks)

            def add(name, content=None, source=None, *, immutable=False, boundary=None):
                nonlocal total
                progress()
                safe_name(name)
                if any(row["path"].casefold() == name.casefold() for row in entries):
                    reject("Duplicate bundle path.")
                maximum = MAX_BYTES if name in DB_PATHS.values() else MAX_FILE
                if source is not None:
                    boundary = boundary or (
                        source.parent if source.is_relative_to(stage) else workspace
                    )
                    _regular(source, boundary)
                    if source.stat().st_size > maximum:
                        reject("Backup file exceeds the size limit.")
                    content = read(source)
                    originals.append((source, content_digest(content), boundary))
                if content is None or len(content) > maximum:
                    reject("Backup file exceeds the size limit.")
                if immutable and changed_secret(content.decode(errors="replace"), secrets):
                    reject(
                        "Immutable evidence contains secret material; correct the input before backup."
                    )
                total += len(content)
                if total > MAX_BYTES or len(entries) >= MAX_FILES:
                    reject("Workspace bundle exceeds the size/file-count limit.")
                target = payload / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(0o600)
                entry = {"path": name, "size": len(content), "sha256": content_digest(content)}
                entries.append(entry)
                return entry

            guard = (
                capture_guard()
                if capture_guard is not None
                else contextlib.nullcontext(lambda: None)
            )
            with guard as alive, contextlib.ExitStack() as locks:
                writers = {}
                renewed_at = [float("-inf")]

                def progress():
                    alive()
                    now = time.time()
                    if self_request and "canonical" in writers and now - renewed_at[0] >= 1:
                        writer = writers["canonical"]
                        # Never resurrect an expired lease or bypass a stolen fencing token.
                        _quiescent(writer, "canonical", self_request)
                        lease = self_request["lease"]
                        writer.execute(
                            "UPDATE leases SET expires_at=?,heartbeat_at=? WHERE resource=? AND holder=? AND token=?",
                            (now + 60, now, lease["resource"], lease["holder"], lease["token"]),
                        )
                        renewed_at[0] = now

                # Global lock order: canonical, then Studio. Keep both while capturing files.
                for kind, source in databases.items():
                    sql = locks.enter_context(
                        contextlib.closing(
                            sqlite3.connect(
                                source.as_uri() + "?mode=rw",
                                uri=True,
                                timeout=10,
                                isolation_level=None,
                            )
                        )
                    )
                    sql.execute("PRAGMA busy_timeout=10000")
                    sql.execute("BEGIN IMMEDIATE")
                    _quiescent(sql, kind, self_request if kind == "canonical" else None)
                    writers[kind] = sql
                    progress()
                for kind, source in databases.items():
                    copied = stage / (kind + ".db")
                    with (
                        contextlib.closing(
                            sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
                        ) as original,
                        contextlib.closing(sqlite3.connect(copied)) as target,
                    ):
                        original.backup(target, pages=128, progress=lambda *_: progress())
                    version = validate_database(copied, secrets, kind=kind, progress=progress)
                    entry = add(DB_PATHS[kind], source=copied)
                    db_manifest[kind] = dict(entry, schema_version=version)
                runtime_path = root / "config/runtime.json"
                if runtime_path.exists() or runtime_path.is_symlink():
                    _regular(runtime_path, root)
                    runtime = json.loads(read(runtime_path))
                elif "canonical" not in databases:
                    runtime = {"system_name": workspace.name}
                    add(".agent-dev/config/runtime.json", json.dumps(runtime).encode())
                else:
                    # A plain raise: newer pyright treats code inside this with-block as
                    # reachable after a NoReturn call.
                    raise DomainError(ErrorCode.POLICY_VIOLATION, "Canonical workspace runtime configuration is missing.")
                marker = metadata(root) or {
                    "format_version": 1,
                    "project_id": slugify(runtime.get("system_name") or workspace.name),
                    "name": runtime.get("system_name") or workspace.name,
                }
                marker = {k: v for k, v in redact(marker, secrets).items() if k not in LOCATION_KEYS}
                add(".agent-dev/workspace.json", json.dumps(marker, ensure_ascii=False).encode())
                for directory in ("config", "starter", "data"):
                    base = root / directory
                    if base.is_symlink():
                        reject("Configuration contains a symlink.")
                    for source in sorted(base.rglob("*")) if base.is_dir() else []:
                        if source.is_symlink():
                            reject("Configuration contains a symlink.")
                        if (
                            not source.is_file()
                            or source.suffix not in {".json", ".md"}
                            or source.name == "process_registry.json"
                        ):
                            continue
                        _regular(source, root)
                        if source.stat().st_size > MAX_FILE:
                            reject("Configuration exceeds the size limit.")
                        raw = read(source)
                        originals.append((source, content_digest(raw), root))
                        value = json.loads(raw) if source.suffix == ".json" else raw.decode()
                        safe = redact(value, secrets)
                        content = (
                            json.dumps(safe, ensure_ascii=False, indent=2)
                            if source.suffix == ".json"
                            else safe
                        ).encode()
                        add(".agent-dev/" + source.relative_to(root).as_posix(), content)
                guide = root / "README.md"
                if guide.exists() or guide.is_symlink():
                    _regular(guide, root)
                    raw = read(guide)
                    originals.append((guide, content_digest(raw), root))
                    add(".agent-dev/README.md", redact(raw.decode(), secrets).encode())
                canonical = stage / "canonical.db" if "canonical" in databases else None
                records = _portable_artifacts(
                    workspace, canonical, progress=progress, cache=cache
                )
                for name, value in records.items():
                    physical = cache / name.removeprefix(".agent-dev/")
                    if json.loads(read(physical)) != value:
                        reject("Artifact changed during capture; retry when idle.")
                    boundary = workspace if cache.is_relative_to(workspace) else cache
                    add(name, source=physical, immutable=True, boundary=boundary)
                evidence_paths = set()
                if canonical:
                    db = Database(canonical, read_only=True)
                    systems = {
                        slugify(
                            json.loads(row["definition_json"]).get("system_name")
                            or row["project_id"]
                        )
                        for row in db.rows("SELECT project_id,definition_json FROM tasks")
                    }
                    for system in systems:
                        base = workspace / "output" / system
                        if base.is_symlink():
                            reject("Evidence escaped the output root.")
                        evidence_paths.update(
                            path for path in base.rglob("*") if path.is_file() or path.is_symlink()
                        )
                    for artifact in db.rows("SELECT path,digest FROM artifacts"):
                        source = workspace / "output" / artifact["path"]
                        _regular(source, workspace / "output")
                        if content_digest(read(source)) != artifact["digest"]:
                            reject("Registered artifact is missing or its digest differs.")
                        evidence_paths.add(source)
                for source in sorted(evidence_paths):
                    _regular(source, workspace / "output")
                    add(source.relative_to(workspace).as_posix(), source=source, immutable=True)
                for source, sha, boundary in originals:
                    _regular(source, boundary)
                    if content_digest(read(source)) != sha:
                        reject(
                            "Backup inputs changed during capture; retry when the workspace is idle."
                        )
                progress()
                if self_request:
                    _quiescent(writers["canonical"], "canonical", self_request)
                for writer in writers.values():
                    writer.commit()
            artifact_entries = [e for e in entries if e["path"] in records]
            manifest = {
                "format_version": 2,
                "project": marker,
                "created_at": time.time(),
                "entries": entries,
                "databases": db_manifest,
                "studio_present": "studio" in databases,
                "artifacts": artifact_entries,
                "excluded": EXCLUDED,
                "restore_policy": "disable_execution_and_invalidate_approvals",
            }
            archive = stage / "bundle.zip"
            with ZipFile(archive, "w", compression=ZIP_DEFLATED) as zipfile:
                zipfile.writestr(
                    "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
                )
                for entry in entries:
                    zipfile.write(payload / entry["path"], entry["path"])
            if self_request:
                with contextlib.closing(
                    Database(databases["canonical"], read_only=True).connect()
                ) as sql:
                    _quiescent(sql, "canonical", self_request)
            with destination.open("xb") as target, archive.open("rb") as source:
                try:
                    shutil.copyfileobj(source, target)
                except BaseException:
                    destination.unlink(missing_ok=True)
                    raise
            destination.chmod(0o600)
    except sqlite3.OperationalError as exc:
        reject("Workspace database is busy; retry when idle. " + str(exc))
    except (ValueError, KeyError, TypeError) as exc:
        reject("Invalid workspace backup input: " + str(exc))
    return {
        "destination": str(destination),
        "files": len(entries),
        "bytes": total,
        "sha256": content_digest(destination.read_bytes()),
        "included": list(db_manifest)
        + ["configuration", "task evidence", "validated design/integration artifacts"],
        "studio_present": manifest["studio_present"],
        "excluded": EXCLUDED,
    }


def _manifest(archive):
    infos = archive.infolist()
    names = [info.filename for info in infos]
    if (
        len(infos) > MAX_FILES + 1
        or len({name.casefold() for name in names}) != len(names)
        or "manifest.json" not in names
    ):
        reject("Invalid or duplicate bundle entries.")
    manifest_info = archive.getinfo("manifest.json")
    if manifest_info.file_size > 4 * 1024 * 1024:
        reject("Manifest exceeds the size limit.")
    manifest = json.loads(archive.read(manifest_info))
    if (
        not isinstance(manifest, dict)
        or type(manifest.get("format_version")) is not int
        or manifest.get("format_version") not in {1, 2}
        or not isinstance(manifest.get("entries"), list)
    ):
        reject("Unsupported bundle manifest.")
    entries = manifest["entries"]
    paths = [entry["path"] for entry in entries]
    if len(set(paths)) != len(paths) or set(names) != {"manifest.json", *paths}:
        reject("Bundle files do not match the manifest.")
    required = {".agent-dev/workspace.json", ".agent-dev/config/runtime.json"}
    if manifest["format_version"] == 1:
        required.add(DB_PATHS["canonical"])
        if DB_PATHS["studio"] in paths or any(
            "/design-references/" in p or "/integration-artifacts/" in p for p in paths
        ):
            reject("Format1 cannot contain Studio or integration caches.")
    else:
        databases = manifest.get("databases")
        if not isinstance(databases, dict) or not databases or set(databases) - DB_PATHS.keys():
            reject("Invalid database manifest.")
        if type(manifest.get("studio_present")) is not bool or manifest["studio_present"] != (
            "studio" in databases
        ):
            reject("Studio presence does not match its database manifest.")
        for kind, path in DB_PATHS.items():
            if (kind in databases) != (path in paths):
                reject("Declared database is missing or undeclared.")
            if kind in databases:
                record = databases[kind]
                entry = next(e for e in entries if e["path"] == path)
                if (
                    not isinstance(record, dict)
                    or any(record.get(k) != entry[k] for k in ("path", "size", "sha256"))
                    or type(record.get("schema_version")) is not int
                ):
                    reject("Database manifest digest/size differs.")
        artifacts = [
            e
            for e in entries
            if e["path"].startswith(
                (".agent-dev/design-references/", ".agent-dev/integration-artifacts/")
            )
        ]
        if (
            manifest.get("artifacts") != artifacts
            or type(manifest.get("created_at")) not in {int, float}
            or not math.isfinite(manifest["created_at"])
        ):
            reject("Invalid artifact manifest or timestamp.")
    if not required.issubset(paths):
        reject("Bundle is missing required workspace files.")
    total = 0
    for entry in entries:
        safe_name(entry["path"])
        info = archive.getinfo(entry["path"])
        mode = info.external_attr >> 16
        maximum = MAX_BYTES if info.filename in DB_PATHS.values() else MAX_FILE
        if (
            info.is_dir()
            or stat.S_ISLNK(mode)
            or stat.S_IFMT(mode) not in {0, stat.S_IFREG}
            or type(entry["size"]) is not int
            or entry["size"] != info.file_size
            or not 0 <= info.file_size <= maximum
        ):
            reject("Unsupported file type or bundle size.")
        total += info.file_size
        if total > MAX_BYTES:
            reject("Expanded bundle exceeds the size limit.")
        sha = hashlib.sha256()
        with archive.open(info) as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                sha.update(chunk)
        if sha.hexdigest() != entry["sha256"]:
            reject("Bundle digest mismatch.")
    return manifest


def _stage(archive, stage):
    manifest = _manifest(archive)
    for entry in manifest["entries"]:
        path = stage / entry["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(entry["path"]) as original, path.open("xb") as target:
            shutil.copyfileobj(original, target)
        path.chmod(0o600)
    metadata(stage / ".agent-dev")
    for kind, name in DB_PATHS.items():
        path = stage / name
        if path.exists():
            version = validate_database(path, kind=kind)
            if (
                manifest["format_version"] == 2
                and manifest["databases"][kind]["schema_version"] != version
            ):
                reject("Database schema differs from its manifest.")
    canonical = stage / DB_PATHS["canonical"]
    if canonical.exists():
        for artifact in Database(canonical, read_only=True).rows(
            "SELECT path,digest FROM artifacts"
        ):
            source = stage / "output" / artifact["path"]
            _regular(source, stage / "output")
            if content_digest(source.read_bytes()) != artifact["digest"]:
                reject("Registered artifact is missing or its digest differs.")
    _portable_artifacts(stage, canonical if canonical.exists() else None)
    for entry in manifest["entries"]:
        if entry["path"] not in DB_PATHS.values() and changed_secret(
            (stage / entry["path"]).read_text(encoding="utf-8", errors="replace"), ()
        ):
            reject("Bundle contains unredacted secret material.")
    return manifest


def inspect_bundle(source: Path):
    try:
        with (
            ZipFile(source) as archive,
            tempfile.TemporaryDirectory(prefix="agent-dev-inspect-") as folder,
        ):
            manifest = _stage(archive, Path(folder))
        return {
            "valid": True,
            "manifest": manifest,
            "studio_present": manifest.get("studio_present", False),
        }
    except (BadZipFile, KeyError, ValueError, TypeError, RuntimeError, sqlite3.Error):
        reject("Invalid workspace bundle.")


def _revoke(path):
    from agent_dev.adapters.state.sqlite.store import WorkflowRepository

    validate_database(path)
    store = WorkflowRepository(Database(path, read_only=True))
    with contextlib.closing(sqlite3.connect(path)) as sql, sql:
        tables = {r[0] for r in sql.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "preview_sessions" in tables:
            sql.execute(
                "UPDATE preview_sessions SET state='failed' WHERE state IN('preparing','active')"
            )
        sql.execute("UPDATE approvals SET state='invalidated' WHERE state='approved'")
        sql.execute("UPDATE capability_grants SET state='consumed'")
        sql.execute("UPDATE leases SET token=token+1,expires_at=0")
        sql.execute("UPDATE commands SET state='interrupted',result_json=NULL")
        sql.execute("UPDATE notification_outbox SET state='dead',claim_until=0 WHERE state<>'sent'")
        sql.execute(
            "UPDATE phase_attempts SET state='interrupted',ended_at=? WHERE state='running'",
            (time.time(),),
        )
        sql.execute(
            "UPDATE tasks SET state='blocked',version=version+1 WHERE state NOT IN ('completed','cancelled')"
        )
        for row in sql.execute("SELECT id,content_json FROM tool_connections").fetchall():
            value = json.loads(row[1])
            value.update(enabled=False, authenticated=False, healthy=False)
            # Restored shared bindings become disabled project-only connections.
            value.pop("shared", None)
            sql.execute(
                "UPDATE tool_connections SET content_json=? WHERE id=?",
                (store._json(value), row[0]),
            )
        for project in sql.execute("SELECT id FROM projects").fetchall():
            profile = sql.execute(
                "SELECT version,content_json FROM project_profile_versions WHERE project_id=? ORDER BY version DESC LIMIT 1",
                (project[0],),
            ).fetchone()
            if profile:
                from agent_dev.domain.content import digest

                value = json.loads(profile[1])
                for runner in value.get("runners", []):
                    runner["authenticated"] = False
                sql.execute(
                    "INSERT INTO project_profile_versions VALUES(?,?,?,?)",
                    (project[0], profile[0] + 1, digest(value), store._json(value)),
                )
                sql.execute("UPDATE projects SET version=version+1 WHERE id=?", (project[0],))
            store._event(
                sql,
                project[0],
                "WorkspaceRestored",
                {"approvals_invalidated": True, "execution_disabled": True},
            )
        # The audit event is historical and must not create a fresh notification.
        sql.execute("UPDATE notification_outbox SET state='dead',claim_until=0 WHERE state<>'sent'")


def _atomic_install(stage: Path, destination: Path):
    """Same-volume directory rename with an atomic no-overwrite guarantee on OS3."""
    if sys.platform == "win32":
        # Windows rename fails if any destination already exists (including empty dirs).
        try:
            os.rename(stage, destination)
        except FileExistsError:
            reject("Restore destination already exists; it will not be overwritten.")
        return
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        if sys.platform == "darwin":
            rename = libc.renamex_np
            rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
            arguments = (os.fsencode(stage), os.fsencode(destination), 0x00000004)  # RENAME_EXCL
        elif sys.platform.startswith("linux"):
            rename = libc.renameat2
            rename.argtypes = [
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint,
            ]
            arguments = (
                -100,
                os.fsencode(stage),
                -100,
                os.fsencode(destination),
                1,
            )  # AT_FDCWD, RENAME_NOREPLACE
        else:
            reject("Atomic no-overwrite directory restore is unavailable on this platform.")
    except AttributeError:
        reject("Atomic no-overwrite directory restore is unavailable on this system.")
    rename.restype = ctypes.c_int
    if rename(*arguments) != 0:
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            reject("Restore destination already exists; it will not be overwritten.")
        raise OSError(error, os.strerror(error), str(destination))


def restore_bundle(source: Path, destination: Path):
    destination = Path(destination).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        reject("Restore requires a new destination; existing folders will not be overwritten.")
    if any(parent.is_symlink() for parent in destination.parents):
        reject("Restore destination contains a symlink.")
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Staging beside the destination guarantees the final directory rename is atomic.
        with (
            ZipFile(source) as archive,
            tempfile.TemporaryDirectory(
                prefix="agent-dev-restore-", dir=destination.parent
            ) as folder,
        ):
            stage = Path(folder) / "workspace"
            stage.mkdir(mode=0o700)
            manifest = _stage(archive, stage)
            managed = stage / ".agent-dev"
            # A restore is a new project-local workspace; it never claims another's folders.
            marker_path = managed / "workspace.json"
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            if set(LOCATION_KEYS) & set(marker):
                marker_path.write_text(
                    json.dumps(
                        {k: v for k, v in marker.items() if k not in LOCATION_KEYS},
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            if (managed / "state.db").exists():
                _revoke(managed / "state.db")
            if (managed / "studio/studio.db").exists():
                with contextlib.closing(sqlite3.connect(managed / "studio/studio.db")) as sql, sql:
                    sql.execute("UPDATE leases SET token=token+1,expires_at=0")
                    sql.execute(
                        "UPDATE jobs SET state='failed',message='Restored: explicitly retry after review' WHERE state IN('queued','running')"
                    )
            runtime_path = managed / "config/runtime.json"
            runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
            runtime["orchestrator_v2"] = {
                "read": True,
                "dual_write": False,
                "execute": False,
                "publish": False,
                "legacy_readonly": False,
            }
            runtime_path.write_text(
                json.dumps(redact(runtime), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            profile_path = managed / "config/profile.json"
            if profile_path.is_file():
                profile = json.loads(profile_path.read_text(encoding="utf-8"))
                for runner in profile.get("runners", []):
                    runner["authenticated"] = False
                profile_path.write_text(
                    json.dumps(redact(profile), ensure_ascii=False, indent=2), encoding="utf-8"
                )
            for kind, name in DB_PATHS.items():
                if (stage / name).exists():
                    validate_database(stage / name, kind=kind)
            _atomic_install(stage, destination)
    except BaseException as exc:
        if isinstance(
            exc, (BadZipFile, KeyError, ValueError, TypeError, RuntimeError, sqlite3.Error)
        ):
            reject("Invalid workspace bundle.")
        raise
    return {
        "restored": str(destination),
        "files": len(manifest["entries"]),
        "execution_disabled": True,
        "approvals_invalidated": "canonical" in manifest.get("databases", {"canonical": {}}),
        "studio_present": manifest.get("studio_present", False),
        "manifest": manifest,
        "operator_actions": [
            "Clone source separately and review/register the destination repository profile.",
            "Create newly approved plans/tasks; historical absolute-path snapshots cannot be resumed.",
            "Reauthenticate provider/tool connections; credentials and worktrees are not restored.",
        ],
    }
