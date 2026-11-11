"""Studio-owned SQLite state. Canonical execution state and user documents stay separate."""

import contextlib
import json
import sqlite3
import time
from pathlib import Path

from agent_dev.domain.studio import StudioError, serialize

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects(
 id TEXT PRIMARY KEY,title TEXT NOT NULL,brief TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 1,
 spec_revision INTEGER NOT NULL DEFAULT 0,mock_revision INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS specs(
 project_id TEXT REFERENCES projects(id),revision INTEGER,payload TEXT NOT NULL,source TEXT NOT NULL,
 digest TEXT NOT NULL,approved INTEGER NOT NULL DEFAULT 0,created REAL NOT NULL,
 PRIMARY KEY(project_id,revision));
CREATE TABLE IF NOT EXISTS mocks(
 project_id TEXT REFERENCES projects(id),revision INTEGER,spec_revision INTEGER NOT NULL,
 payload TEXT NOT NULL,digest TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(project_id,revision),
 FOREIGN KEY(project_id,spec_revision) REFERENCES specs(project_id,revision));
CREATE TABLE IF NOT EXISTS comments(
 id TEXT PRIMARY KEY,project_id TEXT NOT NULL,mock_revision INTEGER NOT NULL,screen_id TEXT NOT NULL,
 element_id TEXT NOT NULL,body TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN('open','resolved')),
 resolution TEXT NOT NULL DEFAULT '',orphaned INTEGER NOT NULL DEFAULT 0,origin_id TEXT,
 created REAL NOT NULL,FOREIGN KEY(project_id,mock_revision) REFERENCES mocks(project_id,revision));
CREATE INDEX IF NOT EXISTS comment_anchor ON comments(project_id,mock_revision,screen_id);
CREATE TABLE IF NOT EXISTS approvals(
 project_id TEXT NOT NULL,mock_revision INTEGER NOT NULL,screen_id TEXT NOT NULL,digest TEXT NOT NULL,
 note TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(project_id,mock_revision,screen_id),
 FOREIGN KEY(project_id,mock_revision) REFERENCES mocks(project_id,revision));
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY,project_id TEXT REFERENCES projects(id),kind TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN('queued','running','done','failed')),version INTEGER NOT NULL,
 message TEXT NOT NULL DEFAULT '',created REAL NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_live_job ON jobs(project_id) WHERE state IN('queued','running');
CREATE TABLE IF NOT EXISTS leases(resource TEXT PRIMARY KEY,holder TEXT,token INTEGER,expires_at REAL);
CREATE TABLE IF NOT EXISTS events(
 id INTEGER PRIMARY KEY,project_id TEXT REFERENCES projects(id),kind TEXT,payload TEXT,created REAL);
CREATE TABLE IF NOT EXISTS reviews(
 id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id),spec_revision INTEGER NOT NULL,
 lenses TEXT NOT NULL,reviewers TEXT NOT NULL,calls INTEGER NOT NULL,failed INTEGER NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS findings(
 id TEXT PRIMARY KEY,review_id TEXT NOT NULL REFERENCES reviews(id),project_id TEXT NOT NULL REFERENCES projects(id),
 spec_revision INTEGER NOT NULL,lens TEXT NOT NULL,severity TEXT NOT NULL CHECK(severity IN('high','medium','low')),
 anchor_kind TEXT NOT NULL,anchor_id TEXT NOT NULL,title TEXT NOT NULL,detail TEXT NOT NULL,suggestion TEXT NOT NULL,
 reviewers TEXT NOT NULL,agreement INTEGER NOT NULL,
 state TEXT NOT NULL CHECK(state IN('open','accepted','rebutted','resolved')),note TEXT NOT NULL DEFAULT '',
 created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS finding_project ON findings(project_id,state);
"""


def job_estimates(db, recent=5):
    """Median seconds of the latest completed generations per kind, for the progress bar.

    AI tools report no progress, so the screen compares elapsed time with past runs.
    """
    queued, durations = {}, {}
    for kind, payload, created in db.execute(
        "SELECT kind,payload,created FROM events WHERE kind IN('job.queued','job.completed') ORDER BY id"
    ):
        data = json.loads(payload or "{}")
        if kind == "job.queued":
            queued[data.get("id")] = (data.get("kind"), created)
        elif data.get("id") in queued:
            job_kind, started = queued.pop(data["id"])
            durations.setdefault(job_kind, []).append(created - started)
    estimates = {}
    for kind, values in durations.items():
        latest = sorted(values[-recent:])
        estimates[kind] = round(latest[len(latest) // 2])
    return estimates


class StudioStore:
    def __init__(self, root):
        from agent_dev.adapters.storage_registry import workspace_locations

        root = Path(root).expanduser().resolve()
        locations = workspace_locations(root)
        self.directory = locations.studio_db.parent
        if self.directory.is_symlink():
            raise StudioError("Studio 상태 폴더에 심볼릭 링크를 사용할 수 없습니다.")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        if not self.directory.resolve().is_relative_to(locations.config_root):
            raise StudioError("Studio 상태 폴더가 프로젝트 밖에 있습니다.")
        self.path = locations.studio_db
        if self.path.is_symlink():
            raise StudioError("Studio DB에 심볼릭 링크를 사용할 수 없습니다.")
        with contextlib.closing(self.connect()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            if db.execute("PRAGMA user_version").fetchone()[0] not in {0, 1}:
                raise StudioError("지원하지 않는 Studio DB 버전입니다.")
            db.executescript(SCHEMA)
            db.execute("PRAGMA user_version=1")
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextlib.contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def project(self, db, project_id, expected=None, idle=True):
        project = db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not project:
            raise StudioError("프로젝트를 찾을 수 없습니다.", 404)
        if expected is not None and (type(expected) is not int or project["version"] != expected):
            raise StudioError("다른 변경이 저장되었습니다. 새로 불러온 뒤 다시 시도해주세요.", 409)
        if (
            idle
            and db.execute(
                "SELECT 1 FROM jobs WHERE project_id=? AND state IN('queued','running')",
                (project_id,),
            ).fetchone()
        ):
            raise StudioError("AI 생성 중입니다. 완료 후 변경해주세요.", 409)
        return dict(project)

    def bump(self, db, project_id, kind, payload):
        db.execute("UPDATE projects SET version=version+1 WHERE id=?", (project_id,))
        db.execute(
            "INSERT INTO events(project_id,kind,payload,created) VALUES(?,?,?,?)",
            (project_id, kind, serialize(payload), time.time()),
        )

    def revision(self, db, table, project_id, revision):
        if table not in {"specs", "mocks"}:
            raise StudioError("잘못된 버전 종류입니다.")
        row = db.execute(
            f"SELECT * FROM {table} WHERE project_id=? AND revision=?", (project_id, revision)
        ).fetchone()
        if not row:
            raise StudioError("검토할 버전을 찾을 수 없습니다.", 404)
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        from agent_dev.domain.content import digest

        if digest(item["payload"]) != item["digest"]:
            raise StudioError(
                "저장된 검토 자료의 해시가 일치하지 않습니다. 백업과 저장소를 확인해주세요.", 409
            )
        return item

    def insert_revision(self, db, table, project_id, payload, source):
        from agent_dev.domain.content import digest

        if table not in {"specs", "mocks"}:
            raise StudioError("잘못된 버전 종류입니다.")
        number = db.execute(
            f"SELECT COALESCE(MAX(revision),0)+1 FROM {table} WHERE project_id=?", (project_id,)
        ).fetchone()[0]
        if table == "specs":
            db.execute(
                "INSERT INTO specs VALUES(?,?,?,?,?,0,?)",
                (project_id, number, serialize(payload), source, digest(payload), time.time()),
            )
            db.execute(
                "UPDATE projects SET spec_revision=?,mock_revision=0 WHERE id=?",
                (number, project_id),
            )
        elif table == "mocks":
            db.execute(
                "INSERT INTO mocks VALUES(?,?,?,?,?,?)",
                (project_id, number, source, serialize(payload), digest(payload), time.time()),
            )
            db.execute("UPDATE projects SET mock_revision=? WHERE id=?", (number, project_id))
        else:
            raise StudioError("잘못된 버전 종류입니다.")
        return number

    def detail(self, project_id, mock_revision=None, spec_revision=None):
        with contextlib.closing(self.connect()) as db:
            # Keep project revisions, job state and history in one WAL read snapshot.
            db.execute("BEGIN")
            project = self.project(db, project_id, idle=False)
            if spec_revision is not None and mock_revision is not None:
                raise StudioError("명세서와 목업 버전을 동시에 선택할 수 없습니다.")
            spec = (
                self.revision(db, "specs", project_id, project["spec_revision"])
                if project["spec_revision"]
                else None
            )
            revision = mock_revision if mock_revision is not None else project["mock_revision"]
            mock = self.revision(db, "mocks", project_id, revision) if revision else None
            if mock_revision is not None and mock:
                spec = self.revision(db, "specs", project_id, mock["spec_revision"])
            if spec_revision is not None:
                spec = self.revision(db, "specs", project_id, spec_revision)
                if mock and mock["spec_revision"] != spec_revision:
                    mock, revision = None, 0
            readonly = bool(
                (spec and spec["revision"] != project["spec_revision"])
                or (mock and mock["revision"] != project["mock_revision"])
            )
            comments = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM comments WHERE project_id=? AND mock_revision=? ORDER BY created",
                    (project_id, revision),
                )
            ]
            approvals = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM approvals WHERE project_id=? AND mock_revision=?",
                    (project_id, revision),
                )
            ]
            jobs = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM jobs WHERE project_id=? ORDER BY created DESC LIMIT 5",
                    (project_id,),
                )
            ]
            estimates = job_estimates(db)
            spec_versions = [
                dict(row)
                for row in db.execute(
                    "SELECT revision,approved,created FROM specs WHERE project_id=? ORDER BY revision DESC",
                    (project_id,),
                )
            ]
            versions = [
                dict(row)
                for row in db.execute(
                    "SELECT revision,spec_revision,created FROM mocks WHERE project_id=? ORDER BY revision DESC",
                    (project_id,),
                )
            ]
            findings = [
                dict(row, reviewers=json.loads(row["reviewers"]))
                for row in db.execute(
                    "SELECT * FROM findings WHERE project_id=? ORDER BY "
                    "CASE state WHEN 'open' THEN 0 ELSE 1 END,"
                    "CASE severity WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,agreement DESC,created",
                    (project_id,),
                )
            ]
            reviews = [
                dict(row, lenses=json.loads(row["lenses"]), reviewers=json.loads(row["reviewers"]))
                for row in db.execute(
                    "SELECT * FROM reviews WHERE project_id=? ORDER BY created DESC LIMIT 5", (project_id,)
                )
            ]
            events = [
                dict(row)
                for row in db.execute(
                    # Usage rows feed the usage dashboard, not the screen history.
                    "SELECT kind,payload,created FROM events WHERE project_id=? AND kind!='job.usage' "
                    "ORDER BY id DESC LIMIT 30",
                    (project_id,),
                )
            ]
        return {
            "project": project,
            "readonly": readonly,
            "spec": spec,
            "mock": mock,
            "comments": comments,
            "approvals": approvals,
            "jobs": jobs,
            "job_estimates": estimates,
            "spec_versions": spec_versions,
            "mock_versions": versions,
            "events": events,
            "findings": findings,
            "reviews": reviews,
        }

    def projects(self):
        with contextlib.closing(self.connect()) as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT id,title,version,created FROM projects ORDER BY created DESC LIMIT 200"
                )
            ]
