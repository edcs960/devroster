"""Planning, immutable reviews, anchored decisions and development handoff use cases."""

import contextlib
import copy
import io
import time
from zipfile import ZIP_DEFLATED, ZipFile

from agent_dev.domain import spec_review
from agent_dev.domain.content import digest
from agent_dev.domain.studio import (
    DOCUMENTS,
    StudioError,
    elements,
    new_id,
    serialize,
    text,
    validate_mock,
    validate_specs,
)


class Studio:
    def __init__(self, store, assets):
        self.store = store
        self.assets = assets

    def create(self, title, brief):
        title, brief = text(title, "프로젝트 이름", 120), text(brief, "기획서")
        project_id = new_id("STUDIO-")
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO projects(id,title,brief,created) VALUES(?,?,?,?)",
                (project_id, title, brief, time.time()),
            )
            self.store.bump(db, project_id, "project.created", {})
        return self.store.detail(project_id)

    def save_brief(self, project_id, version, brief, title):
        brief, title = text(brief, "기획서"), text(title, "프로젝트 이름", 120)
        with self.store.transaction() as db:
            project = self.store.project(db, project_id, version)
            if brief != project["brief"] or title != project["title"]:
                db.execute(
                    "UPDATE projects SET title=?,brief=?,spec_revision=0,mock_revision=0 WHERE id=?",
                    (title, brief, project_id),
                )
                self.store.bump(db, project_id, "brief.updated", {"digest": digest(brief)})
        return self.store.detail(project_id)

    def save_specs(self, project_id, version, payload):
        with self.store.transaction() as db:
            project = self.store.project(db, project_id, version)
            current = self.store.revision(db, "specs", project_id, project["spec_revision"])
            payload = validate_specs(copy.deepcopy(payload), current["source"])
            revision = self.store.insert_revision(
                db, "specs", project_id, payload, current["source"]
            )
            self.store.bump(db, project_id, "spec.edited", {"revision": revision})
        return self.store.detail(project_id)

    def approve_specs(self, project_id, version):
        with self.store.transaction() as db:
            project = self.store.project(db, project_id, version)
            spec = self.store.revision(db, "specs", project_id, project["spec_revision"])
            if any(not q.get("answer", "").strip() for q in spec["payload"]["questions"]):
                raise StudioError(
                    "확인 질문에 답변을 작성하고 저장한 뒤 명세서를 확정해주세요.", 409
                )
            validate_specs(spec["payload"], spec["source"])
            findings = [
                dict(row)
                for row in db.execute("SELECT * FROM findings WHERE project_id=?", (project_id,))
            ]
            blocking = spec_review.blocking(findings)
            if blocking:
                raise StudioError(
                    f"독립 검토의 심각한 지적 {len(blocking)}건이 열려 있습니다. 명세서에 반영하거나, "
                    "이유를 적어 수용·반박한 뒤 확정해주세요.",
                    409,
                )
            if not spec["approved"]:
                db.execute(
                    "UPDATE specs SET approved=1 WHERE project_id=? AND revision=?",
                    (project_id, spec["revision"]),
                )
                # The approval records which known problems were knowingly accepted or rebutted.
                decided = [
                    {"id": f["id"], "state": f["state"], "severity": f["severity"], "title": f["title"]}
                    for f in findings
                    if f["state"] in {"accepted", "rebutted"}
                ]
                self.store.bump(
                    db,
                    project_id,
                    "spec.approved",
                    {"revision": spec["revision"], "digest": spec["digest"], "accepted_findings": decided},
                )
        return self.store.detail(project_id)

    def carry_mock(self, project_id, version):
        """Reuse the last mock for a newly confirmed spec version without AI.

        Re-handing off a changed scope needs a new spec version, which detaches the mock;
        the last mock carries over when it still matches the confirmed spec's screens.
        """
        with self.store.transaction() as db:
            project = self.store.project(db, project_id, version)
            spec = self.store.revision(db, "specs", project_id, project["spec_revision"])
            if not spec["approved"]:
                raise StudioError("명세서를 확정한 뒤 이전 목업을 이어서 쓸 수 있습니다.", 409)
            if project["mock_revision"]:
                raise StudioError("이미 현재 명세서의 목업이 있습니다.", 409)
            last = db.execute(
                "SELECT revision FROM mocks WHERE project_id=? ORDER BY revision DESC LIMIT 1",
                (project_id,),
            ).fetchone()
            if last is None:
                raise StudioError("이어서 쓸 이전 목업이 없습니다. 목업을 생성해주세요.", 409)
            previous = self.store.revision(db, "mocks", project_id, last[0])
            try:
                payload = validate_mock(copy.deepcopy(previous["payload"]), spec["payload"])
            except StudioError:
                raise StudioError(
                    "이전 목업의 화면이 현재 명세서와 맞지 않습니다. 목업을 새로 생성해주세요.", 409
                ) from None
            number = self.store.insert_revision(db, "mocks", project_id, payload, spec["revision"])
            self.store.bump(
                db, project_id, "mock.carried", {"revision": number, "previous": previous["revision"]}
            )
        return self.store.detail(project_id)

    def current_mock(self, db, project, requested):
        if type(requested) is not int or project["mock_revision"] != requested:
            raise StudioError("최신 목업 버전에서 작업해주세요.", 409)
        mock = self.store.revision(db, "mocks", project["id"], requested)
        if mock["spec_revision"] != project["spec_revision"]:
            raise StudioError("명세서가 변경되었습니다. 새 목업을 생성해주세요.", 409)
        return mock

    def screen_editable(self, db, project, revision, screen_id):
        if db.execute(
            "SELECT 1 FROM approvals WHERE project_id=? AND mock_revision=? AND screen_id=?",
            (project["id"], revision, screen_id),
        ).fetchone():
            raise StudioError("확정된 화면입니다. 새 검토 버전을 만든 뒤 변경해주세요.", 409)

    def record_review(self, db, project_id, revision, lenses, results, failed):
        """Store one independent review: the merged findings of every reviewer call."""
        review_id = new_id("REVIEW-")
        merged = spec_review.merge(results)
        reviewers = sorted({(r["provider"], r.get("model", ""), r["family"]) for r in results})
        db.execute(
            "INSERT INTO reviews VALUES(?,?,?,?,?,?,?,?)",
            (
                review_id,
                project_id,
                revision,
                serialize(lenses),
                serialize([{"provider": p, "model": m, "family": f} for p, m, f in reviewers]),
                len(results) + failed,
                failed,
                time.time(),
            ),
        )
        now = time.time()
        for row in merged:
            db.execute(
                "INSERT INTO findings(id,review_id,project_id,spec_revision,lens,severity,anchor_kind,anchor_id,"
                "title,detail,suggestion,reviewers,agreement,state,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'open',?)",
                (
                    new_id("FINDING-"),
                    review_id,
                    project_id,
                    revision,
                    row["lens"],
                    row["severity"],
                    row["anchor_kind"],
                    row["anchor_id"],
                    row["title"],
                    row["detail"],
                    row["suggestion"],
                    serialize(row["reviewers"]),
                    row["agreement"],
                    now,
                ),
            )
        self.store.bump(
            db,
            project_id,
            "review.completed",
            {"id": review_id, "revision": revision, "findings": len(merged), "failed": failed},
        )
        return review_id

    def process_finding(self, project_id, version, finding_id, state, note=""):
        """resolved = fixed in the spec; accepted = known risk kept; rebutted = the reviewer is wrong."""
        if state not in spec_review.STATES:
            raise StudioError("지적 처리 상태가 잘못되었습니다.")
        note = text(note or "", "처리 메모", 4000, state in {"accepted", "rebutted"})
        with self.store.transaction() as db:
            self.store.project(db, project_id, version)
            finding = db.execute(
                "SELECT id FROM findings WHERE id=? AND project_id=?", (finding_id, project_id)
            ).fetchone()
            if not finding:
                raise StudioError("검토 지적을 찾을 수 없습니다.", 404)
            db.execute("UPDATE findings SET state=?,note=? WHERE id=?", (state, note, finding_id))
            self.store.bump(db, project_id, "finding.processed", {"id": finding_id, "state": state})
        return self.store.detail(project_id)

    def review_input(self, project_id, version, lens, language, running=False):
        with contextlib.closing(self.store.connect()) as db:
            # The review job itself is running while its reviewers read the spec.
            project = self.store.project(db, project_id, version, idle=not running)
            if not project["spec_revision"]:
                raise StudioError("먼저 명세서를 생성해주세요.", 409)
            spec = self.store.revision(db, "specs", project_id, project["spec_revision"])
            mock = (
                self.store.revision(db, "mocks", project_id, project["mock_revision"])
                if project["mock_revision"]
                else None
            )
        return {
            "revision": spec["revision"],
            "anchors": spec_review.anchors(spec["payload"], mock["payload"] if mock else None),
            "data": spec_review.review_input(spec["payload"], mock["payload"] if mock else None, lens, language),
            "has_mock": mock is not None,
        }

    def add_comment(self, project_id, version, revision, screen_id, element_id, body):
        body = text(body, "코멘트", 8000)
        with self.store.transaction() as db:
            project = self.store.project(db, project_id, version)
            mock = self.current_mock(db, project, revision)
            if element_id not in elements(mock["payload"], screen_id):
                raise StudioError("선택한 요소를 이 목업에서 찾을 수 없습니다.", 404)
            self.screen_editable(db, project, revision, screen_id)
            comment_id = new_id("COMMENT-")
            db.execute(
                "INSERT INTO comments(id,project_id,mock_revision,screen_id,element_id,body,state,created) VALUES(?,?,?,?,?,?,'open',?)",
                (comment_id, project_id, revision, screen_id, element_id, body, time.time()),
            )
            self.store.bump(
                db,
                project_id,
                "comment.created",
                {"id": comment_id, "screen_id": screen_id, "element_id": element_id},
            )
        return self.store.detail(project_id)

    def process_comment(self, project_id, version, revision, comment_id, state, resolution=""):
        if state not in {"open", "resolved"}:
            raise StudioError("코멘트 처리 상태가 잘못되었습니다.")
        resolution = text(resolution, "처리 메모", 8000, state == "resolved")
        with self.store.transaction() as db:
            project = self.store.project(db, project_id, version)
            self.current_mock(db, project, revision)
            comment = db.execute(
                "SELECT * FROM comments WHERE id=? AND project_id=? AND mock_revision=?",
                (comment_id, project_id, revision),
            ).fetchone()
            if not comment:
                raise StudioError("코멘트를 찾을 수 없습니다.", 404)
            self.screen_editable(db, project, revision, comment["screen_id"])
            db.execute(
                "UPDATE comments SET state=?,resolution=? WHERE id=?",
                (state, resolution, comment_id),
            )
            self.store.bump(
                db,
                project_id,
                "comment.processed",
                {"id": comment_id, "state": state, "resolution": resolution},
            )
        return self.store.detail(project_id)

    def carry_comments(self, db, project_id, previous, current, payload):
        anchors = {(s["id"], e) for s in payload["screens"] for e in elements(payload, s["id"])}
        for comment in db.execute(
            "SELECT * FROM comments WHERE project_id=? AND mock_revision=? AND state='open'",
            (project_id, previous),
        ).fetchall():
            db.execute(
                "INSERT INTO comments VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    new_id("COMMENT-"),
                    project_id,
                    current,
                    comment["screen_id"],
                    comment["element_id"],
                    comment["body"],
                    "open",
                    "",
                    int((comment["screen_id"], comment["element_id"]) not in anchors),
                    comment["origin_id"] or comment["id"],
                    time.time(),
                ),
            )

    def save_mock(self, project_id, version, revision, payload=None):
        with self.store.transaction() as db:
            project = self.store.project(db, project_id, version)
            mock = self.current_mock(db, project, revision)
            spec = self.store.revision(db, "specs", project_id, project["spec_revision"])
            proposed = copy.deepcopy(mock["payload"] if payload is None else payload)
            if proposed.get("design_references", []) != mock["payload"].get(
                "design_references", []
            ):
                raise StudioError(
                    "새 디자인 출처는 참고 자료 연결 후 목업을 재생성하여 반영해주세요."
                )
            payload = validate_mock(proposed, spec["payload"])
            number = self.store.insert_revision(db, "mocks", project_id, payload, spec["revision"])
            self.carry_comments(db, project_id, revision, number, payload)
            self.store.bump(
                db, project_id, "mock.revised", {"revision": number, "previous": revision}
            )
        return self.store.detail(project_id)

    def approve_screen(self, project_id, version, revision, screen_id, note=""):
        note = text(note, "확정 메모", 8000, False)
        with self.store.transaction() as db:
            project = self.store.project(db, project_id, version)
            mock = self.current_mock(db, project, revision)
            elements(mock["payload"], screen_id)
            if db.execute(
                "SELECT 1 FROM comments WHERE project_id=? AND mock_revision=? AND state='open' AND (screen_id=? OR orphaned=1)",
                (project_id, revision, screen_id),
            ).fetchone():
                raise StudioError("미해결 코멘트 또는 삭제된 요소의 의견을 먼저 처리해주세요.", 409)
            spec = self.store.revision(db, "specs", project_id, project["spec_revision"])
            if not spec["approved"]:
                raise StudioError("명세서를 먼저 확정해주세요.", 409)
            stamp = digest({"mock": mock["digest"], "spec": spec["digest"], "screen": screen_id})
            if not db.execute(
                "SELECT 1 FROM approvals WHERE project_id=? AND mock_revision=? AND screen_id=?",
                (project_id, revision, screen_id),
            ).fetchone():
                db.execute(
                    "INSERT INTO approvals VALUES(?,?,?,?,?,?)",
                    (project_id, revision, screen_id, stamp, note, time.time()),
                )
                self.store.bump(
                    db,
                    project_id,
                    "screen.approved",
                    {"screen_id": screen_id, "revision": revision, "digest": stamp},
                )
        return self.store.detail(project_id)

    def bundle(self, project_id, final=False):
        # Read all related rows in one transaction so an export cannot mix revisions.
        with self.store.transaction() as db:
            project = self.store.project(db, project_id)
            spec = self.store.revision(db, "specs", project_id, project["spec_revision"])
            mock = (
                self.store.revision(db, "mocks", project_id, project["mock_revision"])
                if project["mock_revision"]
                else None
            )
            approvals = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM approvals WHERE project_id=? AND mock_revision=?",
                    (project_id, project["mock_revision"]),
                )
            ]
            comments = [
                dict(row)
                for row in db.execute(
                    "SELECT * FROM comments WHERE project_id=? AND mock_revision=?",
                    (project_id, project["mock_revision"]),
                )
            ]
            if final and (
                not mock
                or not spec["approved"]
                or mock["spec_revision"] != spec["revision"]
                or {a["screen_id"] for a in approvals}
                != {s["id"] for s in mock["payload"]["screens"]}
                or any(c["state"] == "open" for c in comments)
            ):
                raise StudioError(
                    "모든 화면을 확정하고 미해결 의견을 처리한 뒤 개발 인계를 받을 수 있습니다.",
                    409,
                )
            if (
                final
                and mock is not None
                and any(
                    a["digest"]
                    != digest(
                        {"mock": mock["digest"], "spec": spec["digest"], "screen": a["screen_id"]}
                    )
                    for a in approvals
                )
            ):
                raise StudioError("화면 확정 근거가 현재 명세·목업과 다릅니다.", 409)
        output = io.BytesIO()
        with ZipFile(output, "w", ZIP_DEFLATED) as zipped:
            for name in DOCUMENTS:
                zipped.writestr(
                    "documents/" + name + ".md", spec["payload"]["documents"][name].encode("utf-8")
                )
            zipped.writestr("specifications.json", serialize(spec["payload"]).encode())
            if final:
                assert mock is not None
                zipped.writestr("mockup/mock.json", serialize(mock["payload"]).encode())
                assets = self.assets
                for name in (
                    "mock_renderer.js",
                    "mock_preview.js",
                    "ui-tokens.css",
                    "ui-base.css",
                    "ui-components.css",
                    "studio.css",
                    "mock.css",
                ):
                    zipped.writestr("mockup/" + name, assets.joinpath(name).read_bytes())
                data = serialize(mock["payload"]).replace("<", "\\u003c")
                html = (
                    '<!doctype html><html lang="ko"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                    '<title>확정 화면 목업</title><link rel="stylesheet" href="ui-tokens.css"><link rel="stylesheet" href="ui-base.css"><link rel="stylesheet" href="ui-components.css"><link rel="stylesheet" href="studio.css"><link rel="stylesheet" href="mock.css"><body class="page-studio"><main class="export-preview">'
                    '<h1>확정 화면 목업</h1><p>샘플 데이터로 동작하는 검토용 화면입니다.</p><label>화면 <select id="screens"></select></label>'
                    '<div id="canvas" class="mock-canvas"></div><script id="mock-data" type="application/json">'
                    + data
                    + '</script><script src="mock_renderer.js"></script><script src="mock_preview.js"></script></main></body></html>'
                )
                zipped.writestr("mockup/index.html", html.encode())
                provenance = {
                    "project": project_id,
                    "spec_revision": spec["revision"],
                    "spec_digest": spec["digest"],
                    "mock_revision": mock["revision"],
                    "mock_digest": mock["digest"],
                    "approvals": approvals,
                    "comments": comments,
                }
                zipped.writestr("decisions.json", serialize(provenance).encode())
                scope = "\n".join(
                    "- " + r["id"] + ": " + r["description"]
                    for r in spec["payload"]["requirements"]
                )
                acceptance = "\n".join(
                    "- " + r["id"] + ": " + criterion
                    for r in spec["payload"]["requirements"]
                    for criterion in r["acceptance"]
                )
                handoff = f"# {project['title']} 개발 인계\n\n## Scope\n{scope}\n\n## Acceptance\n{acceptance}\n\n## Constraints\n- 확정 명세·목업 버전만 구현. 범위 변경은 재검토.\n- code/publish 권한은 별도 profile와 plan 승인을 따른다.\n\n## 확정 근거\n```json\n{serialize(provenance)}\n```\n\n## 확정 목업\n```json\n{serialize(mock['payload'])}\n```\n"
                for name, content in spec["payload"]["documents"].items():
                    handoff += f"\n## {name}\n{content}\n"
                zipped.writestr("development-handoff.md", handoff.encode())
                tasks = [
                    {
                        "id": project_id[:19] + "-" + screen["id"],
                        "title": screen["title"],
                        "system_name": project["title"],
                        "requirement_ids": screen["requirement_ids"],
                        "scope": [
                            screen["purpose"],
                            "development-handoff.md의 확정 목업을 구현한다.",
                        ],
                        "acceptance_criteria": [
                            c
                            for r in spec["payload"]["requirements"]
                            if r["id"] in screen["requirement_ids"]
                            for c in r["acceptance"]
                        ],
                    }
                    for screen in spec["payload"]["screens"]
                ]
                covered = {
                    item
                    for screen in spec["payload"]["screens"]
                    for item in screen["requirement_ids"]
                }
                remaining = [r for r in spec["payload"]["requirements"] if r["id"] not in covered]
                if remaining:
                    tasks.append(
                        {
                            "id": project_id[:19] + "-BACKEND",
                            "title": "화면 외 요구사항 구현",
                            "system_name": project["title"],
                            "requirement_ids": [r["id"] for r in remaining],
                            "scope": [r["description"] for r in remaining],
                            "acceptance_criteria": [c for r in remaining for c in r["acceptance"]],
                        }
                    )
                zipped.writestr("tasks.json", serialize(tasks).encode())
                zipped.writestr(
                    "README.md",
                    (
                        "# 개발 인계 사용\n\n1. ZIP을 프로젝트 내 검토 자료 폴더에 풉니다.\n"
                        "2. mockup/index.html을 열고 확정 화면·decisions.json을 확인합니다.\n"
                        "3. devroster design refine PROJECT-ID DESIGN-ID development-handoff.md\n"
                        "4. devroster design approve DESIGN-ID 1\n"
                        "5. devroster plan create PROJECT-ID PLAN-ID DESIGN-ID 1 tasks.json\n"
                        "6. devroster plan approve PLAN-ID\n"
                        "7. runner/profile/test gate를 확인한 뒤 devroster refactor configure execute, devroster task run TASK-ID를 사용합니다.\n"
                        "\n이 패키지 생성은 실제 개발 실행·커밋·푸시·배포를 시작하지 않습니다.\n"
                    ).encode(),
                )
        return output.getvalue()

    def recover_jobs(self):
        with self.store.transaction() as db:
            for job in db.execute(
                "SELECT j.* FROM jobs j LEFT JOIN leases l ON l.resource='studio:'||j.id WHERE j.state IN('queued','running') AND (l.expires_at IS NULL OR l.expires_at<?)",
                (time.time(),),
            ).fetchall():
                db.execute(
                    "UPDATE jobs SET state='failed',message=? WHERE id=?",
                    ("생성 세션이 중단되었습니다. 다시 생성해주세요.", job["id"]),
                )
                self.store.bump(db, job["project_id"], "job.interrupted", {"id": job["id"]})

    def cancel_job(self, project_id, version, job_id):
        with self.store.transaction() as db:
            self.store.project(db, project_id, version, idle=False)
            job = db.execute(
                "SELECT * FROM jobs WHERE id=? AND project_id=? AND state IN('queued','running')",
                (job_id, project_id),
            ).fetchone()
            if not job:
                raise StudioError("실행 중인 생성 작업을 찾을 수 없습니다.", 404)
            db.execute(
                "UPDATE jobs SET state='failed',message=? WHERE id=?",
                ("사용자가 생성을 중단했습니다.", job_id),
            )
            db.execute("UPDATE leases SET expires_at=0 WHERE resource=?", ("studio:" + job_id,))
            self.store.bump(db, project_id, "job.cancelled", {"id": job_id})
        return self.store.detail(project_id)

    def generation_input(self, project_id, version, kind):
        with contextlib.closing(self.store.connect()) as db:
            project = self.store.project(db, project_id, version)
            if kind == "specs":
                spec = (
                    self.store.revision(db, "specs", project_id, project["spec_revision"])
                    if project["spec_revision"]
                    else None
                )
                return {
                    "brief": project["brief"],
                    "title": project["title"],
                    "previous": spec["payload"] if spec else None,
                }
            if kind != "mock":
                raise StudioError("생성할 자료 종류가 잘못되었습니다.")
            spec = self.store.revision(db, "specs", project_id, project["spec_revision"])
            if not spec["approved"] or not spec["payload"]["screens"]:
                raise StudioError("화면이 정의된 명세서를 먼저 확정해주세요.", 409)
            previous = self.store.detail(project_id)
            if previous["mock"] is None:
                latest = db.execute(
                    "SELECT MAX(revision) FROM mocks WHERE project_id=?", (project_id,)
                ).fetchone()[0]
                if latest:
                    previous = self.store.detail(project_id, latest)
            return {
                "spec": spec,
                "previous_mock": previous["mock"],
                "comments": previous["comments"],
            }
