"""Import-local evidence coverage. Graph inference and stdout never prove acceptance."""

import contextlib
import json
import subprocess
from pathlib import Path, PurePosixPath
from typing import NoReturn
from urllib.parse import urlencode

from agent_dev.adapters import query_cursor
from agent_dev.adapters.pytest_attestation import canonical_selector, selector_passed
from agent_dev.adapters.source_explorer import SourceExplorer, forbidden
from agent_dev.adapters.state.sqlite.store import WorkflowRepository, identifier
from agent_dev.adapters.worktrees import commit_fingerprint, evidence_digest
from agent_dev.domain import criteria_tests
from agent_dev.domain.content import canonical, content_digest, digest, redact
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.role_policy import path_matches

VALID_KINDS = {"screen", "plan_task", "task", "file", "test", "commit"}
VALID_STATES = {"mapped", "verified", "stale", "historical", "retired", "unchanged_reference"}


def fail(message, code=ErrorCode.INVALID_INPUT) -> NoReturn:
    raise DomainError(code, message)


def _file(root, relative):
    path = PurePosixPath(relative.split("::", 1)[0].split("#L", 1)[0])
    if (
        path.is_absolute()
        or ".." in path.parts
        or any(forbidden(p) for p in path.parts)
        or "\\" in relative
    ):
        fail("대상 경로가 등록된 모듈 밖에 있습니다.")
    root = Path(root).resolve()
    candidate = root / path
    if any(
        p.is_symlink() for p in (candidate, *candidate.parents) if p.is_relative_to(root)
    ) or not candidate.resolve().is_relative_to(root):
        fail("대상 경로에 심볼릭 링크를 사용할 수 없습니다.")
    return candidate


def _test_selector(link, definition, policy):
    mappings = criteria_tests.mappings(link, definition)
    gates = (
        {m["gate"] for m in mappings}
        if mappings
        else set(definition["handoff_scope"]["verification_gates"])
    )
    commands = criteria_tests.registered_commands(link, policy.get("verification", {}).get("commands", []), gates)
    try:
        targets = {canonical_selector(link["target_ref"], c.get("cwd", ".")) for c in commands}
        if not targets:
            # Unregistered manual links keep their existing module-relative file contract.
            return canonical_selector(link["target_ref"])
    except (TypeError, ValueError):
        fail("등록 테스트 selector의 작업 폴더·파일 경로를 확인해주세요.")
    if len(targets) != 1:
        fail(
            "등록된 검증의 작업 폴더가 달라 테스트 대상이 모호합니다. 승인 계획의 검증 매핑을 확인해주세요."
        )
    return next(iter(targets))


def _context(conn, link):
    imported = conn.execute(
        "SELECT * FROM handoff_imports WHERE id=?", (link["import_id"],)
    ).fetchone()
    req = conn.execute(
        "SELECT * FROM imported_requirements WHERE import_id=? AND requirement_id=?",
        (link["import_id"], link["requirement_id"]),
    ).fetchone()
    if not imported or not req:
        fail("해당 인계의 요구사항을 선택해주세요.")
    content = json.loads(req["content_json"])
    if digest(content) != req["content_digest"]:
        fail("요구사항 스냅샷 해시가 일치하지 않습니다.")
    criteria = link.get("criteria_ids", [])
    if not isinstance(criteria, list) or set(criteria) - set(content.get("criteria_ids", [])):
        fail("해당 요구사항의 수용 기준만 선택해주세요.")
    task = None
    resolved = {}
    binding = {}
    if link.get("task_id"):
        task = conn.execute(
            "SELECT * FROM tasks WHERE id=? AND project_id=? AND plan_id=?",
            (link["task_id"], imported["project_id"], imported["plan_id"]),
        ).fetchone()
        if not task:
            fail("task_id: 같은 프로젝트·인계의 작업을 선택해주세요.")
        definition = json.loads(task["definition_json"])
        module = link.get("module_id")
        if module != definition.get("handoff_scope", {}).get("module_id") or link[
            "requirement_id"
        ] not in definition.get("requirement_ids", []):
            fail("module_id: 요구사항에 연결된 승인 대상 모듈을 선택해주세요.")
        scope = definition["handoff_scope"]
        profile = conn.execute(
            "SELECT * FROM project_profile_versions WHERE project_id=? AND version=?",
            (imported["project_id"], scope["profile_version"]),
        ).fetchone()
        if not profile or profile["digest"] != scope["profile_digest"]:
            fail("인계 프로파일을 검증할 수 없습니다.")
        config = json.loads(profile["content_json"])
        binding = config.get("repositories", {}).get(module, {})
        if task["snapshot_id"]:
            snapshot = conn.execute(
                "SELECT * FROM input_snapshots WHERE id=?", (task["snapshot_id"],)
            ).fetchone()
            resolved = json.loads(snapshot["resolved_json"])
            if digest(resolved) != snapshot["digest"]:
                fail("승인 스냅샷 해시가 일치하지 않습니다.")
            binding = resolved.get("repositories", {}).get(module, {})
        if link["kind"] in {"file", "test"}:
            target = link["target_ref"]
            if link["kind"] == "test":
                target = _test_selector(link, definition, resolved or config)
                binding = dict(binding, _trace_test_selector=target)
            candidate = _file(binding.get("worktree", binding.get("path", "")), target)
            relative = str(
                candidate.relative_to(
                    Path(binding.get("worktree", binding.get("path", ""))).resolve()
                )
            )
            if not any(
                p == "." or relative == p.rstrip("/") or relative.startswith(p.rstrip("/") + "/")
                for p in scope["allowed_paths"]
            ):
                fail("대상 파일이 승인 경로 밖에 있습니다.")
    elif link["kind"] in {"task", "file", "test", "commit"}:
        fail("task_id: 해당 인계의 작업과 모듈을 선택해주세요.")
    if link["kind"] == "task" and link["target_ref"] != link.get("task_id"):
        fail("task 대상과 task_id가 같아야 합니다.")
    if (
        link["kind"] in {"screen", "plan_task"}
        and not conn.execute(
            "SELECT 1 FROM requirement_links WHERE import_id=? AND requirement_id=? AND kind=? AND target_ref=?",
            (link["import_id"], link["requirement_id"], link["kind"], link["target_ref"]),
        ).fetchone()
    ):
        fail("현재 인계의 화면·계획 작업을 선택해주세요.")
    return imported, content, task, resolved, binding


class FrozenSourceReader(SourceExplorer):
    """Reuse descriptor-relative privacy guards against the approved binding, never latest setup."""

    def __init__(self, binding, secrets=()):
        self.binding = binding
        self.root = Path("/")
        self.secrets = secrets

    def _module(self, module):
        return str(
            Path(self.binding.get("worktree", self.binding.get("path", ""))).resolve()
        ), self.binding.get("allowed_paths", ["."])


def _bytes(root, relative, maximum=1_000_000):
    import os

    reader = FrozenSourceReader({"path": str(root), "allowed_paths": ["."]})
    with reader._open("frozen", relative) as (fd, info):
        if info.st_size > maximum:
            fail("파일 크기 제한을 초과했습니다.")
        raw = os.read(fd, maximum + 1)
        after = os.fstat(fd)
        if info.st_mtime_ns != after.st_mtime_ns or info.st_size != after.st_size:
            fail("읽는 중 원본이 변경되었습니다.", ErrorCode.STALE_VERSION)
        return raw


def _secrets(db):
    from agent_dev.adapters.secrets import registered_secrets
    from agent_dev.adapters.workspace import configuration_root

    return tuple(registered_secrets(configuration_root(db.path.parent.parent)))


def _artifact_report(conn, db, task_id, run_id):
    rows = conn.execute(
        "SELECT * FROM artifacts WHERE task_id=? AND kind='verification' AND trust='trusted_gate_result' ORDER BY created_at DESC",
        (task_id,),
    ).fetchall()
    root = db.path.parent.parent / "output"
    for row in rows:
        path = _file(root, row["path"])
        if not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        payload = _bytes(root, row["path"])
        if content_digest(payload) != row["digest"]:
            continue
        try:
            report = json.loads(payload.decode().split("\n\n", 1)[1])
        except (ValueError, IndexError):
            continue
        if report.get("run_id") == run_id:
            return report
    return None


def _proof(conn, db, link, context, cache=None):
    _imported, _content, task, resolved, _binding = context
    if (
        link["kind"] != "test"
        or task is None
        or not link.get("verification_run_id")
        or not link.get("criteria_ids")
        or not resolved
    ):
        return None, "수동·AI 연결입니다. 실제 검증 근거가 필요합니다."
    definition = json.loads(task["definition_json"])
    matches = criteria_tests.mappings(link, definition)
    commands = resolved.get("verification", {}).get("commands", [])
    matching = criteria_tests.registered_commands(link, commands, {m["gate"] for m in matches})
    if not matching:
        return None, "승인 계획에 수용 기준과 등록된 테스트 대상의 명시적 연결이 없습니다."
    if not WorkflowRepository(db)._approved(conn, task["id"]):
        return None, "현재 설계·계획 승인이 유효하지 않습니다."
    cache = {} if cache is None else cache
    latest_key = ("latest_verification", task["id"])
    if latest_key not in cache:
        # Canonical transaction insertion order also disambiguates equal/frozen clocks.
        cache[latest_key] = conn.execute(
            "SELECT * FROM verification_runs WHERE task_id=? ORDER BY rowid DESC LIMIT 1",
            (task["id"],),
        ).fetchone()
    run = cache[latest_key]
    if run is None or run["state"] != "passed":
        return None, "최신 검증 실행이 통과하지 않았습니다. 다시 검증한 뒤 최신 실행을 연결해주세요."
    if run["id"] != link["verification_run_id"]:
        return None, "과거 검증 실행입니다. 최신 통과 실행을 명시적으로 다시 연결해주세요."
    source_key = ("source", task["id"], task["snapshot_id"])
    if source_key not in cache:
        cache[source_key] = None
        cache[source_key] = evidence_digest(resolved.get("repositories", {}))
    source = cache[source_key]
    if source is None:
        return None, "현재 소스 근거를 검증할 수 없습니다."
    revision_key = ("revisions", task["id"], task["snapshot_id"])
    if revision_key not in cache:
        cache[revision_key] = None
        cache[revision_key] = commit_fingerprint(resolved.get("repositories", {}))
    if cache[revision_key] is None:
        return None, "현재 커밋 근거를 검증할 수 없습니다."
    expected = digest({"commands": commands, "source": source})
    if expected != run["policy_digest"]:
        return None, "검증 이후 소스·커밋·검증 정책이 변경되었습니다."
    report = _artifact_report(conn, db, task["id"], run["id"])
    snapshot = conn.execute(
        "SELECT digest FROM input_snapshots WHERE id=?", (task["snapshot_id"],)
    ).fetchone()
    if (
        not report
        or not report.get("passed")
        or report.get("snapshot_digest") != snapshot["digest"]
        or report.get("source_digest") != source
        or report.get("source_commits") != cache[revision_key]
        or report.get("policy_digest") != expected
    ):
        return None, "검증 보고서·승인 스냅샷·현재 소스 근거가 일치하지 않습니다."
    for command in matching:
        result = conn.execute(
            "SELECT * FROM verification_results WHERE run_id=? AND gate=? AND passed=1 AND exit_code=0",
            (run["id"], command["name"]),
        ).fetchone()
        if result and any(
            g["name"] == command["name"]
            and g["passed"]
            and g.get("exit_code") == 0
            and selector_passed(
                g.get("test_results"),
                link["target_ref"],
                command["command"],
                cwd=command.get("cwd", "."),
            )
            for g in report.get("gates", [])
        ):
            return digest(
                {
                    "run_id": run["id"],
                    "policy_digest": expected,
                    "target": link["target_ref"],
                    "criteria": sorted(link["criteria_ids"]),
                    "revisions": cache[revision_key],
                }
            ), "등록된 테스트 대상의 실제 통과 근거입니다. 기능 보장을 뜻하지 않습니다."
    return (
        None,
        "지원되는 실제 테스트 대상의 실행·통과 근거가 없어 연결 상태로 유지합니다. 수집·제외·건너뜀·예상 실패는 검증 근거에 포함하지 않습니다.",
    )


def _mapped_digest(link, binding):
    if link["kind"] in {"file", "test"} and binding:
        target = (
            binding.get("_trace_test_selector", link["target_ref"])
            if link["kind"] == "test"
            else link["target_ref"]
        )
        path = _file(binding.get("worktree", binding.get("path", "")), target)
        return (
            content_digest(_bytes(path.parent, path.name, 10_000_000))
            if path.is_file() and path.stat().st_size <= 10_000_000
            else "missing"
        )
    if link["kind"] == "commit" and binding:
        if not __import__("re").fullmatch(r"[a-fA-F0-9]{40}", link["target_ref"]):
            fail("commit: 전체 Git SHA가 필요합니다.")
        process = subprocess.run(
            ["git", "-C", binding.get("worktree", binding.get("path", "")), "rev-parse", "HEAD"],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=10,
            check=True,
        )
        return process.stdout.strip()
    return ""


def _source_stale(db, imported):
    from agent_dev.adapters import studio_handoff

    path = None
    try:
        path = studio_handoff._studio_db_path(studio_handoff.studio_root_for(db))
        with contextlib.closing(studio_handoff._open(path)) as studio:
            studio.execute("BEGIN")
            bundle = studio_handoff._read_bundle(studio, imported["source_studio_id"])
        return bundle["bundle_digest"] != imported["bundle_digest"]
    except DomainError:
        # A missing external Studio preserves the canonical immutable snapshot.
        return path is not None and path.is_file()


def _change_record(conn, import_id, incoming, cache):
    key = ("applied_change", import_id, incoming)
    if key not in cache:
        column = "target_import_id" if incoming else "source_import_id"
        row = conn.execute(
            "SELECT source_import_id,target_import_id,proposal_json FROM requirement_changes WHERE " + column + "=? AND state='applied' ORDER BY applied_at DESC LIMIT 1",
            (import_id,),
        ).fetchone()
        cache[key] = dict(row, proposal=json.loads(row["proposal_json"])) if row else None
    return cache[key]


def _requirement_origin(conn, import_id, requirement_id, cache=None):
    """Walk unchanged revisions to the immutable import that owns actual evidence."""
    cache = {} if cache is None else cache
    current, seen = import_id, set()
    while current not in seen:
        seen.add(current)
        change = _change_record(conn, current, True, cache)
        if not change or change["proposal"]["delta"].get(requirement_id) != "unchanged":
            return current
        current = change["source_import_id"]
    fail("요구사항 개정 이력 순환을 확인해주세요.", ErrorCode.POLICY_VIOLATION)


def _change_provenance(conn, import_id, requirement_id, cache=None):
    """Applied canonical revision chains outrank external Studio availability."""
    cache = {} if cache is None else cache
    key = ("requirement_provenance", import_id, requirement_id)
    if key in cache:
        return cache[key]
    current, seen, unchanged = import_id, set(), False
    while current not in seen:
        seen.add(current)
        change = _change_record(conn, current, False, cache)
        if not change:
            break
        disposition = change["proposal"]["delta"].get(requirement_id)
        if disposition in {"changed", "removed"}:
            result = {"current": False, "state": "retired" if disposition == "removed" else "historical", "applied_disposition": disposition,
                      "impact_decisions": [{"disposition": i["disposition"], "exclusion_reason": i["exclusion_reason"], "task_id": i["task_id"]} for i in change["proposal"]["impacts"] if i["requirement_id"] == requirement_id]}
            cache[key] = result
            return result
        unchanged = unchanged or disposition == "unchanged"
        current = change["target_import_id"]
    origin = _requirement_origin(conn, import_id, requirement_id, cache)
    result = {"current": origin == import_id}
    if origin != import_id:
        result.update(state="unchanged_reference", applied_disposition="unchanged", evidence_import_id=origin)
    elif unchanged:
        result["applied_disposition"] = "unchanged"
    cache[key] = result
    return result


def _evaluate(conn, db, row, cache=None):
    link = dict(row)
    link["criteria_ids"] = json.loads(link.pop("criteria_json", "[]"))
    cache = {} if cache is None else cache
    provenance = _change_provenance(conn, link["import_id"], link["requirement_id"], cache)
    link["change_provenance"] = provenance
    link["current"] = provenance["current"]
    if provenance.get("state") in {"historical", "retired", "unchanged_reference"}:
        link["recorded_state"] = row["state"]
        link["state"] = provenance["state"]
        link["explanation"] = "적용된 요구사항 변경의 이전 근거입니다. 실제 이력은 보존되며 현재 수용 기준 검증에 합산하지 않습니다."
        return link
    try:
        context = _context(conn, link)
        cache = {} if cache is None else cache
        proof, explanation = _proof(conn, db, link, context, cache)
        source_key = ("studio", context[0]["id"])
        if source_key not in cache:
            cache[source_key] = _source_stale(db, context[0])
        if cache[source_key] and provenance.get("applied_disposition") != "unchanged":
            proof = None
            explanation = "Studio 원본 명세서·목업·결정 이력이 변경되었습니다."
            link["state"] = "stale"
        elif link["state"] == "verified":
            link["state"] = "verified" if proof == link["evidence_digest"] else "stale"
        elif (
            link["evidence_digest"]
            and link["kind"] in {"file", "test", "commit"}
            and _mapped_digest(link, context[-1]) != link["evidence_digest"]
        ):
            link["state"] = "stale"
            explanation = "연결 이후 파일·커밋 근거가 변경되었습니다."
        link["explanation"] = explanation
    except (DomainError, OSError, ValueError, KeyError, subprocess.SubprocessError):
        link["state"] = (
            "stale" if row["state"] == "verified" or row["evidence_digest"] else "mapped"
        )
        link["explanation"] = "현재 근거를 검증할 수 없습니다. 등록 범위와 원본을 확인해주세요."
    return link


def create_link(db, params):
    request = params.get("request_id")
    if (
        not isinstance(request, str)
        or not request
        or len(request) > 100
        or type(params.get("expected_version")) is not int
        or params["expected_version"] < 0
    ):
        fail("request_id와 expected_version이 필요합니다.")
    if (
        params.get("kind") not in VALID_KINDS
        or not isinstance(params.get("target_ref"), str)
        or not params["target_ref"]
    ):
        fail("유효한 연결 종류와 대상이 필요합니다.")
    key = "traceability:" + request
    payload_digest = digest(params)
    with db.transaction() as conn:
        command = conn.execute("SELECT * FROM commands WHERE key=?", (key,)).fetchone()
        if command:
            if command["request_digest"] != payload_digest:
                fail("요청 키가 다른 입력에 사용되었습니다.")
            return json.loads(command["result_json"])
        context = _context(conn, params)
        existing = conn.execute(
            "SELECT * FROM requirement_links WHERE import_id=? AND requirement_id=? AND kind=? AND target_ref=?",
            (params["import_id"], params["requirement_id"], params["kind"], params["target_ref"]),
        ).fetchone()
        if (existing and existing["version"] != params["expected_version"]) or (
            not existing and params["expected_version"] != 0
        ):
            fail("링크 버전이 변경되었습니다.", ErrorCode.STALE_VERSION)
        proof, explanation = _proof(conn, db, params, context)
        state = "verified" if proof else "mapped"
        evidence = proof or _mapped_digest(params, context[-1])
        link_id = existing["id"] if existing else identifier()
        version = existing["version"] + 1 if existing else 0
        values = (
            params.get("task_id"),
            params.get("module_id"),
            evidence,
            params.get("verification_run_id"),
            canonical(params.get("criteria_ids", [])),
            state,
            version,
        )
        if existing:
            conn.execute(
                "UPDATE requirement_links SET task_id=?,module_id=?,evidence_digest=?,verification_run_id=?,criteria_json=?,state=?,version=? WHERE id=?",
                (*values, link_id),
            )
        else:
            conn.execute(
                "INSERT INTO requirement_links VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    link_id,
                    params["import_id"],
                    params["requirement_id"],
                    params["kind"],
                    params["target_ref"],
                    *values,
                ),
            )
        result = {
            "id": link_id,
            "import_id": params["import_id"],
            "requirement_id": params["requirement_id"],
            "kind": params["kind"],
            "target_ref": params["target_ref"],
            "state": state,
            "version": version,
            "criteria_ids": params.get("criteria_ids", []),
            "explanation": explanation,
        }
        repo = WorkflowRepository(db)
        repo._event(
            conn,
            context[0]["project_id"],
            "RequirementLinkRecorded",
            dict(result, request_id=request),
        )
        conn.execute(
            "INSERT INTO commands VALUES(?,?,'done',?,?)",
            (key, payload_digest, canonical(result), repo.clock()),
        )
        return result


def _changed_tests(db, task, definition):
    """Without named tests, link the test files this task changed to each of its
    requirements (criteria unknown, so the link stays "mapped")."""
    snapshot = db.rows("SELECT resolved_json FROM input_snapshots WHERE id=?", (task["snapshot_id"],))
    if not snapshot:
        return {}
    resolved = json.loads(snapshot[0]["resolved_json"])
    binding = resolved.get("repositories", {}).get(
        definition.get("handoff_scope", {}).get("module_id"), {}
    )
    worktree, base = binding.get("worktree"), binding.get("base_commit")
    patterns = [p for p in resolved.get("test_paths", []) if p]
    if not worktree or not base or not patterns:
        return {}
    changed = subprocess.run(
        ["git", "-C", worktree, "diff", "--name-only", base],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        timeout=30,
        check=False,
    ).stdout.split() + subprocess.run(
        ["git", "-C", worktree, "ls-files", "--others", "--exclude-standard"],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        timeout=30,
        check=False,
    ).stdout.split()
    tests = sorted({p for p in changed if any(path_matches(p, x) for x in patterns)})
    return {
        f"{requirement}:*{index}": path
        for requirement in definition.get("requirement_ids", [])
        for index, path in enumerate(tests)
    }


def link_criteria_tests(db, task_id, run_id):
    """After verification passes, link the tests Test Automation named per criterion.

    Each requirement gets one "file" link per test file, carrying the criteria that file
    covers. Links stay "mapped" until a registered selector proves them. Returns the count.
    """
    task = db.rows("SELECT * FROM tasks WHERE id=?", (task_id,))
    if not task or not task[0]["plan_id"]:
        return 0
    definition = json.loads(task[0]["definition_json"])
    module = definition.get("handoff_scope", {}).get("module_id")
    imported = db.rows(
        "SELECT id FROM handoff_imports WHERE plan_id=? ORDER BY created_at DESC LIMIT 1",
        (task[0]["plan_id"],),
    )
    latest = db.rows(
        "SELECT payload_json FROM events WHERE aggregate_id=? AND kind='CriteriaTestsNamed' ORDER BY version DESC LIMIT 1",
        (task_id,),
    )
    named = json.loads(latest[0]["payload_json"]).get("criteria_tests", {}) if latest else {}
    if not imported or not module:
        return 0
    if not named:
        named = _changed_tests(db, task[0], definition)
    grouped = criteria_tests.grouped(named, definition.get("requirement_ids", []))
    linked = 0
    for (requirement, path), criteria in sorted(grouped.items()):
        existing = db.rows(
            "SELECT version FROM requirement_links WHERE import_id=? AND requirement_id=? AND kind='file' AND target_ref=?",
            (imported[0]["id"], requirement, path),
        )
        try:
            create_link(
                db,
                {
                    "request_id": f"auto-test:{run_id}:{requirement}:{path}"[:100],
                    "expected_version": existing[0]["version"] if existing else 0,
                    "import_id": imported[0]["id"],
                    "requirement_id": requirement,
                    "kind": "file",
                    "target_ref": path,
                    "task_id": task_id,
                    "module_id": module,
                    "criteria_ids": sorted(criteria),
                    "verification_run_id": run_id,
                },
            )
            linked += 1
        except DomainError:
            continue  # Unknown criterion or a path outside the approved scope stays unlinked.
    return linked


def query(db, params):
    conditions = []
    args = []
    for key, column in (
        ("project_id", "hi.project_id"),
        ("import_id", "hi.id"),
        ("requirement_id", "rl.requirement_id"),
    ):
        if params.get(key):
            conditions.append(column + "=?")
            args.append(params[key])
    state = params.get("state", "")
    if state and state not in VALID_STATES:
        fail("상태 필터를 확인해주세요.")
    limit = int(params.get("limit", 50))
    if not 1 <= limit <= 200:
        fail("조회 개수는 1~200입니다.")
    base = " AND ".join(conditions) or "1=1"
    with contextlib.closing(db.connect()) as conn:
        conn.execute("BEGIN")
        if (
            params.get("project_id")
            and not conn.execute(
                "SELECT 1 FROM projects WHERE id=?", (params["project_id"],)
            ).fetchone()
        ):
            fail("등록된 프로젝트를 선택해주세요.")
        if (
            params.get("import_id")
            and not conn.execute(
                "SELECT 1 FROM handoff_imports WHERE id=?", (params["import_id"],)
            ).fetchone()
        ):
            fail("등록된 인계를 선택해주세요.")
        if params.get("import_id") and params.get("project_id"):
            owner = conn.execute(
                "SELECT project_id FROM handoff_imports WHERE id=?", (params["import_id"],)
            ).fetchone()
            if owner["project_id"] != params["project_id"]:
                fail("이 인계는 선택한 프로젝트에 속하지 않습니다.")
        link_revision = conn.execute(
            "SELECT COUNT(*),COALESCE(SUM(rl.version),0) FROM requirement_links rl JOIN handoff_imports hi ON hi.id=rl.import_id WHERE "
            + base,
            args,
        ).fetchone()
        total_links = link_revision[0]
        revision = list(link_revision)
        scope = {
            key: params.get(key, "")
            for key in ("project_id", "import_id", "requirement_id", "state", "task_id")
        }
        scope["database"] = str(db.path)
        position = ""
        if params.get("cursor"):
            position = query_cursor.decode(params["cursor"], "links", scope, revision)
            if (
                not isinstance(position, str)
                or not conn.execute(
                    "SELECT 1 FROM requirement_links rl JOIN handoff_imports hi ON hi.id=rl.import_id WHERE "
                    + base
                    + " AND rl.id=?",
                    (*args, position),
                ).fetchone()
            ):
                fail("이어보기 위치를 찾을 수 없습니다.", ErrorCode.STALE_VERSION)
        budget = min(800, limit * 4) if state else limit
        rows = conn.execute(
            "SELECT rl.*,hi.project_id,hi.source_studio_id FROM requirement_links rl JOIN handoff_imports hi ON hi.id=rl.import_id WHERE "
            + base
            + " AND rl.id>? ORDER BY rl.id LIMIT ?",
            (*args, position, budget + 1),
        ).fetchall()
        links, page, cache = [], [], {}
        last_scanned = position
        for row in rows[:budget]:
            link = _evaluate(conn, db, row, cache)
            links.append(link)
            last_scanned = row["id"]
            if not state or link["state"] == state:
                page.append(link)
            if len(page) == limit:
                break
        more = len(rows) > len(links)
        next_cursor = query_cursor.encode("links", scope, last_scanned, revision) if more else None
        req_args = []
        req_where = []
        for key, column in (
            ("project_id", "hi.project_id"),
            ("import_id", "hi.id"),
            ("requirement_id", "ir.requirement_id"),
        ):
            if params.get(key):
                req_where.append(column + "=?")
                req_args.append(params[key])
        req_base = " AND ".join(req_where) or "1=1"
        req_count = conn.execute(
            "SELECT COUNT(*) FROM imported_requirements ir JOIN handoff_imports hi ON hi.id=ir.import_id WHERE "
            + req_base,
            req_args,
        ).fetchone()[0]
        req_position = ["", ""]
        if params.get("requirements_cursor"):
            req_position = query_cursor.decode(
                params["requirements_cursor"], "requirements", scope, req_count
            )
            if (
                not isinstance(req_position, list)
                or len(req_position) != 2
                or not all(isinstance(v, str) for v in req_position)
                or not conn.execute(
                    "SELECT 1 FROM imported_requirements ir JOIN handoff_imports hi ON hi.id=ir.import_id WHERE "
                    + req_base
                    + " AND (ir.import_id,ir.requirement_id)=(?,?)",
                    (*req_args, *req_position),
                ).fetchone()
            ):
                fail("요구사항 이어보기 위치를 찾을 수 없습니다.", ErrorCode.STALE_VERSION)
        reqs = conn.execute(
            "SELECT ir.*,hi.project_id,hi.design_id,hi.plan_id,hi.source_studio_id FROM imported_requirements ir JOIN handoff_imports hi ON hi.id=ir.import_id WHERE "
            + req_base
            + " AND (ir.import_id,ir.requirement_id)>(?,?)"
            + " ORDER BY ir.import_id,ir.requirement_id LIMIT 201",
            (*req_args, *req_position),
        ).fetchall()
        requirements_next = (
            query_cursor.encode(
                "requirements",
                scope,
                [reqs[199]["import_id"], reqs[199]["requirement_id"]],
                req_count,
            )
            if len(reqs) > 200
            else None
        )
        requirements = []
        verified = set()
        total = set()
        unknown = 0
        for req in reqs[:200]:
            content = json.loads(req["content_json"])
            criteria = content.get("criteria_ids", [])
            provenance = _change_provenance(conn, req["import_id"], req["requirement_id"], cache)
            if provenance["current"]:
                unknown += not bool(criteria)
                total.update((req["import_id"], c) for c in criteria)
            matching = [
                l
                for l in links
                if l["import_id"] == req["import_id"]
                and l["requirement_id"] == req["requirement_id"]
            ]
            for link in matching:
                if link["state"] == "verified":
                    verified.update((req["import_id"], c) for c in link["criteria_ids"])
            has_link = (
                bool(matching)
                or conn.execute(
                    "SELECT 1 FROM requirement_links WHERE import_id=? AND requirement_id=? LIMIT 1",
                    (req["import_id"], req["requirement_id"]),
                ).fetchone()
                is not None
            )
            requirements.append(
                {
                    "import_id": req["import_id"],
                    "requirement_id": req["requirement_id"],
                    "content": content,
                    "current": provenance["current"],
                    "change_provenance": provenance,
                    "state": provenance["state"] if not provenance["current"] else "verified"
                    if criteria and all((req["import_id"], c) in verified for c in criteria)
                    else "stale"
                    if any(l["state"] == "stale" for l in matching)
                    else "mapped"
                    if has_link
                    else "missing",
                    "coverage_pending": total_links > len(links),
                    "source_url": "/studio?" + urlencode({"project_id": req["source_studio_id"]}),
                    "design_id": req["design_id"],
                    "plan_id": req["plan_id"],
                }
            )
        task_args = []
        task_where = []
        for key, column in (("project_id", "hi.project_id"), ("import_id", "hi.id")):
            if params.get(key):
                task_where.append(column + "=?")
                task_args.append(params[key])
        if params.get("requirement_id"):
            task_where.append(
                "EXISTS (SELECT 1 FROM requirement_links tr WHERE tr.import_id=hi.id AND tr.task_id=t.id AND tr.requirement_id=?)"
            )
            task_args.append(params["requirement_id"])
        task_base = " AND ".join(task_where) or "1=1"
        task_from = (
            " FROM tasks t JOIN handoff_imports hi ON hi.plan_id=t.plan_id WHERE " + task_base
        )
        task_revision = list(
            conn.execute(
                "SELECT COUNT(*),COALESCE(SUM(t.version),0)" + task_from, task_args
            ).fetchone()
        )
        task_position = ""
        if params.get("tasks_cursor"):
            task_position = query_cursor.decode(
                params["tasks_cursor"], "tasks", scope, task_revision
            )
            if (
                not isinstance(task_position, str)
                or not conn.execute(
                    "SELECT 1" + task_from + " AND t.id=?", (*task_args, task_position)
                ).fetchone()
            ):
                fail("작업 이어보기 위치를 찾을 수 없습니다.", ErrorCode.STALE_VERSION)
        task_rows = conn.execute(
            "SELECT t.*,hi.id AS import_id" + task_from + " AND t.id>? ORDER BY t.id LIMIT 201",
            (*task_args, task_position),
        ).fetchall()
        tasks = []
        for task in task_rows[:200]:
            definition = json.loads(task["definition_json"])
            tasks.append(
                {
                    "id": task["id"],
                    "title": task["title"],
                    "import_id": task["import_id"],
                    "module_id": definition.get("handoff_scope", {}).get("module_id"),
                    "requirement_ids": definition.get("requirement_ids", []),
                }
            )
        tasks_next = (
            query_cursor.encode("tasks", scope, task_rows[199]["id"], task_revision)
            if len(task_rows) > 200
            else None
        )
        run_args = list(task_args)
        run_from = (
            " FROM verification_runs vr JOIN tasks t ON t.id=vr.task_id JOIN handoff_imports hi ON hi.plan_id=t.plan_id WHERE "
            + task_base
        )
        if params.get("task_id"):
            if not conn.execute(
                "SELECT 1" + task_from + " AND t.id=?", (*task_args, params["task_id"])
            ).fetchone():
                fail("선택한 요구사항 범위의 작업을 확인해주세요.")
            run_from += " AND vr.task_id=?"
            run_args.append(params["task_id"])
        run_count = conn.execute("SELECT COUNT(*)" + run_from, run_args).fetchone()[0]
        run_position = None
        if params.get("runs_cursor"):
            run_position = query_cursor.decode(params["runs_cursor"], "runs", scope, run_count)
            if (
                not isinstance(run_position, list)
                or len(run_position) != 2
                or not conn.execute(
                    "SELECT 1" + run_from + " AND vr.created_at=? AND vr.id=?",
                    (*run_args, *run_position),
                ).fetchone()
            ):
                fail("검증 실행 이어보기 위치를 찾을 수 없습니다.", ErrorCode.STALE_VERSION)
        run_rows = conn.execute(
            "SELECT vr.*"
            + run_from
            + (" AND (vr.created_at,vr.id)<(?,?)" if run_position else "")
            + " ORDER BY vr.created_at DESC,vr.id DESC LIMIT 101",
            (*run_args, *(run_position or [])),
        ).fetchall()
        runs = [dict(r) for r in run_rows[:100]]
        runs_next = (
            query_cursor.encode(
                "runs", scope, [run_rows[99]["created_at"], run_rows[99]["id"]], run_count
            )
            if len(run_rows) > 100
            else None
        )
        verified.intersection_update(total)
        partial = total_links > len(links) or req_count > len(requirements)
        return redact(
            {
                "tasks": tasks,
                "runs": runs,
                "links": redact(page),
                "requirements": redact(requirements),
                "next_cursor": next_cursor,
                "total": total_links,
                "coverage_partial": partial,
                "coverage_scope": "current_page" if partial else "complete",
                "unchecked_links": max(0, total_links - len(links)),
                "truncated": bool(next_cursor or requirements_next or tasks_next or runs_next),
                "requirements_next_cursor": requirements_next,
                "tasks_next_cursor": tasks_next,
                "runs_next_cursor": runs_next,
                "coverage": {
                    "verified": len(verified),
                    "total": len(total),
                    "unknown": unknown,
                    "missing": len(total - verified),
                    "stale": sum(l["state"] == "stale" for l in links),
                },
            },
            _secrets(db),
        )


def coverage(db, import_id):
    return query(db, {"import_id": import_id})["coverage"]


def detail(db, link_id):
    with contextlib.closing(db.connect()) as conn:
        conn.execute("BEGIN")
        row = conn.execute("SELECT * FROM requirement_links WHERE id=?", (link_id,)).fetchone()
        if row is None:
            fail("연결을 찾을 수 없습니다.")
        link = _evaluate(conn, db, row)
        imported, req, task, _resolved, binding = _context(conn, link)
        design = conn.execute(
            "SELECT content FROM design_documents WHERE id=? AND version=?",
            (imported["design_id"], imported["design_version"]),
        ).fetchone()
        result = {
            "link": link,
            "requirement": req,
            "original_snapshot": design["content"],
            "task": dict(task) if task else None,
        }
        if link["kind"] == "test" and link.get("verification_run_id") and task is not None:
            result["verification"] = _artifact_report(
                conn, db, task["id"], link["verification_run_id"]
            )
        if link["kind"] == "file":
            root = binding.get("worktree", binding.get("path", ""))
            path = _file(root, link["target_ref"])
            line = int(link["target_ref"].split("#L", 1)[1]) if "#L" in link["target_ref"] else 1
            result["file_text"] = FrozenSourceReader(binding, _secrets(db)).read(
                "frozen", str(path.relative_to(Path(root).resolve())), line
            )
            relative = str(path.relative_to(Path(root).resolve()))
            result["diff"] = subprocess.run(
                [
                    "git",
                    "-C",
                    root,
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "HEAD",
                    "--",
                    relative,
                ],
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=10,
                check=True,
            ).stdout[:30000]
        if link["kind"] == "commit":
            result["commit"] = subprocess.run(
                [
                    "git",
                    "-C",
                    binding.get("worktree", binding.get("path", "")),
                    "show",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--format=fuller",
                    "--stat",
                    link["target_ref"],
                ],
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=10,
                check=True,
            ).stdout[:30000]
        return redact(result, _secrets(db))
