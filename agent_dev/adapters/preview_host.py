"""Registered application preview sessions and element comments (RV-02).

Services come only from the task's frozen snapshot (``preview_services``).
Connecting to an already running service never takes ownership of it; an
explicit launch runs the frozen argv under ``process_guard`` with a fenced
lease, so stop, parent loss or lease expiry end only that process tree.
Screens are captured by a fixed Node Playwright worker in a fresh browser
context restricted to the registered origins; nothing from the app is
proxied into the Agent UI origin.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from importlib.resources import files
from pathlib import Path, PurePosixPath
from typing import NoReturn

from agent_dev.adapters import development_review as review
from agent_dev.adapters.state.sqlite.store import identifier
from agent_dev.domain.content import content_digest, digest
from agent_dev.domain.errors import DomainError, ErrorCode

SERVICE_KEYS = {
    "id",
    "module",
    "argv",
    "cwd",
    "env_refs",
    "host",
    "port",
    "readiness_path",
    "startup_timeout",
    "allowed_origins",
}
LOOPBACK = {"127.0.0.1", "localhost"}
LEASE_TTL = 30.0
MAX_CAPTURES = 50
MAX_ELEMENTS = 200
BROWSER_KEYS = ("DEVROSTER_PLAYWRIGHT_MODULE", "DEVROSTER_BROWSER_EXECUTABLE")
_OWNED: dict[str, dict] = {}
_LOCK = threading.Lock()


def fail(message, code=ErrorCode.INVALID_INPUT) -> NoReturn:
    raise DomainError(code, message)


def _origin_ok(value) -> bool:
    match = re.fullmatch(r"http://(127\.0\.0\.1|localhost):(\d{2,5})", value or "")
    return bool(match) and 1024 <= int(match.group(2)) <= 65535


def normalize_services(value, repositories, secrets=()) -> list[dict]:
    """Validated preview service registrations (profile data, never request input)."""
    from agent_dev.adapters.environment import validate_key

    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 10:
        fail("미리보기 서비스는 10개까지 등록할 수 있습니다.")
    result, seen = [], set()
    for row in value:
        if not isinstance(row, dict) or set(row) - SERVICE_KEYS:
            fail("미리보기 서비스 항목을 확인해주세요.")
        service_id = row.get("id")
        if not isinstance(service_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", service_id) or service_id in seen:
            fail("미리보기 서비스 ID는 영문 소문자로 시작하는 고유 이름이어야 합니다.")
        seen.add(service_id)
        if row.get("module") not in repositories:
            fail(f"'{service_id}' 서비스의 모듈이 등록되지 않았습니다.")
        argv = row.get("argv", [])
        if not isinstance(argv, list) or len(argv) > 30 or any(not isinstance(a, str) or not a or len(a) > 500 for a in argv):
            fail(f"'{service_id}' 실행 명령은 실행파일과 인수 배열로 입력해주세요.")
        if any(secret and secret in arg for secret in secrets for arg in argv):
            fail(f"'{service_id}' 실행 명령에 비밀 값을 넣지 말고 환경변수 참조를 사용해주세요.")
        cwd = row.get("cwd", ".")
        if not isinstance(cwd, str) or PurePosixPath(cwd).is_absolute() or ".." in PurePosixPath(cwd).parts or "\\" in cwd:
            fail(f"'{service_id}' 실행 폴더는 모듈 안의 상대 경로여야 합니다.")
        env_refs = row.get("env_refs", [])
        if not isinstance(env_refs, list) or len(env_refs) > 30:
            fail(f"'{service_id}' 환경변수 참조를 확인해주세요.")
        for key in env_refs:
            validate_key(key)
        host = row.get("host", "127.0.0.1")
        port = row.get("port")
        if host not in LOOPBACK or type(port) is not int or not 1024 <= port <= 65535:
            fail(f"'{service_id}'은 이 컴퓨터의 주소(127.0.0.1)와 1024–65535 포트만 사용할 수 있습니다.")
        readiness = row.get("readiness_path", "/")
        if not isinstance(readiness, str) or not readiness.startswith("/") or readiness.startswith("//") or len(readiness) > 300:
            fail(f"'{service_id}' 준비 확인 경로는 /로 시작해야 합니다.")
        timeout = row.get("startup_timeout", 30)
        if type(timeout) is not int or not 1 <= timeout <= 180:
            fail(f"'{service_id}' 시작 대기 시간은 1–180초입니다.")
        origins = row.get("allowed_origins", [])
        if not isinstance(origins, list) or len(origins) > 10 or not all(_origin_ok(o) for o in origins):
            fail(f"'{service_id}' 허용 origin은 http://127.0.0.1:포트 형식만 가능합니다.")
        result.append(
            {
                "id": service_id,
                "module": row["module"],
                "argv": list(argv),
                "cwd": cwd,
                "env_refs": list(env_refs),
                "host": host,
                "port": port,
                "readiness_path": readiness,
                "startup_timeout": timeout,
                "allowed_origins": list(origins),
            }
        )
    return result


class PreviewContext(review.ReviewContext):
    """Review context plus the frozen preview services and capture folder."""

    def __init__(self, db, task_id, secrets=(), config_root=None):
        super().__init__(db, task_id, secrets)
        if Path(task_id).name != task_id or task_id in {".", ".."}:
            fail("작업 ID를 확인해주세요.")
        self.project_root = Path(db.path).resolve().parent.parent
        self.config_root = Path(config_root or self.project_root)
        runtime_root = self.project_root / ".agent-dev"
        if config_root is not None:
            from agent_dev.adapters.storage_registry import workspace_locations

            locations = workspace_locations(self.config_root)
            self.project_root, runtime_root = locations.source_root, locations.runtime_root
        self.services = {
            s["id"]: s
            for s in normalize_services(
                self.resolved.get("preview_services"), self.resolved.get("repositories", {}), self.secrets
            )
        }
        self.capture_dir = runtime_root / "preview" / task_id

    def service(self, service_id):
        if service_id not in self.services:
            fail("작업 승인 시 등록된 미리보기 서비스가 아닙니다.")
        return self.services[service_id]


def _origin(service):
    return f"http://{service['host']}:{service['port']}"


def _port_open(host, port) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False


def _ready(service):
    """GET the readiness path; redirects and non-2xx fail (origin must not change)."""
    connection = http.client.HTTPConnection(service["host"], service["port"], timeout=2)
    try:
        connection.request("GET", service["readiness_path"], headers={"Host": f"{service['host']}:{service['port']}"})
        response = connection.getresponse()
        response.read(65536)
        if 300 <= response.status < 400:
            return False, "준비 확인 주소가 다른 주소로 이동시켰습니다. 등록 주소를 확인해주세요."
        if not 200 <= response.status < 300:
            return False, f"준비 확인 응답이 {response.status}입니다."
        return True, ""
    except OSError:
        return False, "서비스에 연결할 수 없습니다."
    finally:
        connection.close()


def _confirm(context, service):
    return digest({"task": context.task_id, "snapshot": context.snapshot["digest"], "service": service})


def _public_service(service):
    return dict(service, origin=_origin(service))


def prepare(context: PreviewContext, payload, agent_port):
    service = context.service(payload.get("service"))
    if service["port"] == agent_port:
        fail("미리보기 서비스는 DevRoster 화면과 다른 포트를 사용해야 합니다.")
    return {
        "service": _public_service(service),
        "confirm_digest": _confirm(context, service),
        "running": _port_open(service["host"], service["port"]),
        "notice": "기본은 이미 실행 중인 서비스에 연결합니다. 직접 시작하면 위 명령을 작업 폴더에서 실행하고, 종료 시 이 화면이 시작한 프로세스만 정리합니다.",
    }


def _environment(context, service):
    from agent_dev.adapters.environment import read_environment

    values = read_environment(context.config_root)
    missing = [key for key in service["env_refs"] if key not in values and key not in os.environ]
    if missing:
        fail("프로젝트 환경변수를 먼저 등록해주세요: " + ", ".join(missing))
    from agent_dev.adapters.process_launcher import environment

    return environment(
        "preview",
        extra={key: values.get(key, os.environ.get(key, "")) for key in service["env_refs"]},
    )


def _heartbeat(safety, lease, stop):
    while not stop.wait(LEASE_TTL / 3):
        try:
            safety.heartbeat(lease, LEASE_TTL)
        except DomainError:
            return


def start(context: PreviewContext, payload, agent_port, owner):
    from agent_dev.adapters import process_guard
    from agent_dev.adapters.state.sqlite.safety import ExecutionSafety

    service = context.service(payload.get("service"))
    mode = payload.get("mode", "connect")
    if mode not in {"connect", "launch"}:
        fail("연결 방식을 확인해주세요.")
    if service["port"] == agent_port:
        fail("미리보기 서비스는 DevRoster 화면과 다른 포트를 사용해야 합니다.")
    if payload.get("confirm_digest") != _confirm(context, service):
        fail("확인한 실행 정보가 바뀌었습니다. 다시 준비해주세요.", ErrorCode.STALE_VERSION)
    context.store.assert_change_allowed(context.task_id)
    session_id, lease, guard, stop = identifier(), None, None, None
    if mode == "launch":
        from agent_dev.adapters import workspace_trust

        workspace_trust.require(context.config_root, "미리보기 서비스 실행")
        if _port_open(service["host"], service["port"]):
            fail("포트가 이미 사용 중입니다. 실행 중인 서비스에 연결하거나 포트를 비워주세요.", ErrorCode.POLICY_VIOLATION)
        root, _, _ = context.binding(service["module"])
        cwd = (root / service["cwd"]).resolve()
        if not cwd.is_relative_to(root) or not cwd.is_dir():
            fail("실행 폴더가 작업 저장소 안에 없습니다.", ErrorCode.POLICY_VIOLATION)
        safety = ExecutionSafety(context.store)
        lease = safety.acquire(f"preview:{context.task_id}:{service['id']}", session_id, LEASE_TTL)
        try:
            guard = subprocess.Popen(
                [
                    sys.executable,
                    process_guard.__file__,
                    "--parent",
                    str(os.getpid()),
                    "--db",
                    str(context.db.path),
                    "--resource",
                    lease["resource"],
                    "--holder",
                    lease["holder"],
                    "--token",
                    str(lease["token"]),
                    "--",
                    *service["argv"],
                ],
                cwd=cwd,
                env=_environment(context, service),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=os.name != "nt",
            )
        except (OSError, DomainError):
            safety.release(lease)
            raise
        stop = threading.Event()
        threading.Thread(target=_heartbeat, args=(safety, lease, stop), daemon=True).start()
        deadline = time.monotonic() + service["startup_timeout"]
        ready, reason = False, "시작 대기 시간이 지났습니다."
        while time.monotonic() < deadline:
            if guard.poll() is not None:
                reason = "서비스가 시작 중 종료되었습니다."
                break
            ready, reason = _ready(service)
            if ready or "이동" in reason:
                break
            time.sleep(0.2)
        if not ready:
            _terminate(safety, lease, guard, stop)
            fail("서비스를 시작하지 못했습니다: " + reason, ErrorCode.CAPABILITY_UNAVAILABLE)
    else:
        ready, reason = _ready(service)
        if not ready:
            fail("서비스에 연결하지 못했습니다: " + reason, ErrorCode.CAPABILITY_UNAVAILABLE)
    source = review.collect(context)["digest"]
    with context.db.transaction() as conn:
        generation = conn.execute(
            "SELECT COALESCE(MAX(generation),0)+1 FROM preview_sessions WHERE task_id=?",
            (context.task_id,),
        ).fetchone()[0]
        # Earlier sessions end here: connected ones, and launched ones whose process this
        # server no longer owns (for example after a restart).
        for row in conn.execute(
            "SELECT id,lease_resource FROM preview_sessions WHERE task_id=? AND state='active'",
            (context.task_id,),
        ).fetchall():
            if row[1] is None or not _alive(row[0]):
                conn.execute("UPDATE preview_sessions SET state='stopped' WHERE id=?", (row[0],))
        conn.execute(
            "INSERT INTO preview_sessions(id,task_id,profile_digest,source_digest,origin,state,generation,lease_resource,lease_token,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                session_id,
                context.task_id,
                digest(service),
                source,
                _origin(service),
                "active",
                generation,
                lease["resource"] if lease else None,
                str(lease["token"]) if lease else None,
                context.store.clock(),
            ),
        )
        context.store._event(
            conn,
            context.task_id,
            "PreviewStarted",
            {"session_id": session_id, "service": service["id"], "mode": mode, "generation": generation},
        )
    if lease:
        with _LOCK:
            _OWNED[session_id] = {"lease": lease, "guard": guard, "stop": stop, "owner": owner, "db": context.db}
    return {"session_id": session_id, "generation": generation, "mode": mode, "origin": _origin(service), "owned": bool(lease)}


def _terminate(safety, lease, guard, stop):
    if stop is not None:
        stop.set()
    try:
        safety.release(lease)
    except DomainError:
        pass
    try:
        guard.wait(timeout=5)
    except subprocess.TimeoutExpired:
        from agent_dev.adapters.process_guard import terminate_tree

        terminate_tree(guard)
        guard.wait(timeout=5)


def _alive(session_id):
    """True only for a process this server launched and still runs (lost on restart)."""
    with _LOCK:
        owned = _OWNED.get(session_id)
    return owned is not None and owned["guard"].poll() is None


def _stop_owned(session_id):
    from agent_dev.adapters.state.sqlite.safety import ExecutionSafety
    from agent_dev.adapters.state.sqlite.store import WorkflowRepository

    with _LOCK:
        owned = _OWNED.pop(session_id, None)
    if owned:
        _terminate(ExecutionSafety(WorkflowRepository(owned["db"])), owned["lease"], owned["guard"], owned["stop"])
    return bool(owned)


def stop(context: PreviewContext, payload):
    session_id = payload.get("session_id")
    rows = context.db.rows(
        "SELECT * FROM preview_sessions WHERE id=? AND task_id=?", (session_id, context.task_id)
    )
    if not rows:
        fail("이 작업의 미리보기 세션이 아닙니다.")
    stopped_process = _stop_owned(session_id)
    with context.db.transaction() as conn:
        conn.execute("UPDATE preview_sessions SET state='stopped' WHERE id=?", (session_id,))
        context.store._event(conn, context.task_id, "PreviewStopped", {"session_id": session_id})
    return {"session_id": session_id, "state": "stopped", "process_stopped": stopped_process}


def shutdown(owner):
    with _LOCK:
        sessions = [key for key, value in _OWNED.items() if value["owner"] == owner]
    for session_id in sessions:
        _stop_owned(session_id)


def _session(context, session_id, generation):
    rows = context.db.rows(
        "SELECT * FROM preview_sessions WHERE id=? AND task_id=?", (session_id, context.task_id)
    )
    if not rows or rows[0]["state"] != "active":
        fail("활성 미리보기 세션이 아닙니다. 다시 연결해주세요.", ErrorCode.STALE_VERSION)
    latest = context.db.rows(
        "SELECT MAX(generation) AS g FROM preview_sessions WHERE task_id=?", (context.task_id,)
    )[0]["g"]
    if generation != rows[0]["generation"] or generation != latest:
        fail("미리보기 세션이 새로 시작되었습니다. 최신 세션으로 다시 시도해주세요.", ErrorCode.STALE_VERSION)
    with _LOCK:
        owned = _OWNED.get(session_id)
    if rows[0]["lease_resource"] and (owned is None or owned["guard"].poll() is not None):
        fail("이 화면이 시작한 서비스가 종료되었습니다. 다시 시작해주세요.", ErrorCode.STALE_VERSION)
    return rows[0]


def browser_config(context):
    from agent_dev.adapters.environment import read_environment

    values = dict(read_environment(context.config_root))
    module, executable = (os.environ.get(k) or values.get(k, "") for k in BROWSER_KEYS)
    node = shutil.which("node")
    if not module or not executable or not node:
        return None
    if not Path(module).is_dir() or not Path(executable).is_file():
        return None
    return {"module": module, "executable": executable, "node": node}


def _manifests(context):
    if not context.capture_dir.is_dir():
        return []
    rows = []
    for path in sorted(context.capture_dir.glob("*.json"))[-MAX_CAPTURES:]:
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return sorted(rows, key=lambda r: r.get("created_at", 0))


def _key(manifest):
    return (manifest["service"], manifest["path"], manifest["viewport"]["width"], manifest["viewport"]["height"])


def _current_captures(context, source):
    latest = {}
    for manifest in _manifests(context):
        latest[_key(manifest)] = manifest
    return {
        m["id"]: m for m in latest.values() if m["source_digest"] == source
    }


def capture(context: PreviewContext, payload):
    session_id, generation = payload.get("session_id"), payload.get("generation")
    path, viewport = payload.get("path", "/"), payload.get("viewport", {"width": 1280, "height": 800})
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//") or len(path) > 500 or any(c in path for c in "\\\r\n"):
        fail("화면 경로는 /로 시작하는 등록 서비스 안의 경로여야 합니다.")
    if (
        not isinstance(viewport, dict)
        or set(viewport) != {"width", "height"}
        or type(viewport["width"]) is not int
        or type(viewport["height"]) is not int
        or not 320 <= viewport["width"] <= 1920
        or not 320 <= viewport["height"] <= 2000
    ):
        fail("화면 크기는 320–1920 × 320–2000입니다.")
    session = _session(context, session_id, generation)
    service = next((s for s in context.services.values() if _origin(s) == session["origin"]), None)
    if service is None:
        fail("세션의 서비스 등록이 바뀌었습니다. 다시 연결해주세요.", ErrorCode.STALE_VERSION)
    context.store.assert_change_allowed(context.task_id)
    ready, reason = _ready(service)
    if not ready:
        fail("서비스 상태를 확인하지 못했습니다: " + reason, ErrorCode.STALE_VERSION)
    config = browser_config(context)
    if config is None:
        return {
            "available": False,
            "reason": "설치된 브라우저 실행기가 설정되지 않았습니다. 프로젝트 환경변수 "
            + " / ".join(BROWSER_KEYS)
            + "에 설치된 Playwright 모듈 폴더와 Chrome 실행파일을 등록하면 화면을 캡처할 수 있습니다. 자동 설치는 하지 않습니다.",
        }
    context.capture_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=context.capture_dir) as folder:
        shot = Path(folder) / "shot.png"
        request = {
            "module": config["module"],
            "executable": config["executable"],
            "origin": session["origin"],
            "url": session["origin"] + path,
            "allowed_origins": service["allowed_origins"],
            "viewport": viewport,
            "screenshot": str(shot),
            "timeout_ms": 20000,
            "max_elements": MAX_ELEMENTS,
        }
        worker = files("agent_dev.adapters").joinpath("preview_worker.cjs")
        try:
            result = subprocess.run(
                [config["node"], str(worker)],
                input=json.dumps(request),
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            fail("화면 캡처 시간이 초과되었습니다.", ErrorCode.CAPABILITY_UNAVAILABLE)
        output: dict = {"error": "worker"}
        try:
            output = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            pass
        if result.returncode or output.get("error") or not shot.is_file():
            fail("화면을 캡처하지 못했습니다: " + str(output.get("error", "브라우저 오류")), ErrorCode.CAPABILITY_UNAVAILABLE)
        image = shot.read_bytes()
    capture_id = identifier()
    elements = [
        {
            "index": index,
            "testid": context.public(str(e.get("testid", ""))[:200]),
            "tag": str(e.get("tag", ""))[:20],
            "role": str(e.get("role", ""))[:40],
            "text": context.public(str(e.get("text", ""))[:120]),
            "path": str(e.get("path", ""))[:300],
            "rect": {k: round(float(e.get("rect", {}).get(k, 0)), 1) for k in ("x", "y", "width", "height")},
        }
        for index, e in enumerate(output.get("elements", [])[:MAX_ELEMENTS])
    ]
    manifest = {
        "id": capture_id,
        "session_id": session_id,
        "generation": generation,
        "service": service["id"],
        "path": path,
        "final_url": context.public(str(output.get("final_url", ""))[:500]),
        "viewport": viewport,
        "screen_digest": content_digest(image),
        "source_digest": review.collect(context)["digest"],
        "elements": elements,
        "blocked_origins": [context.public(str(o))[:100] for o in output.get("blocked", [])][:20],
        "created_at": context.store.clock(),
    }
    manifest["capture_digest"] = digest(manifest)
    (context.capture_dir / f"{capture_id}.png").write_bytes(image)
    (context.capture_dir / f"{capture_id}.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    with context.db.transaction() as conn:
        context.store._event(
            conn,
            context.task_id,
            "PreviewCaptured",
            {"capture_id": capture_id, "capture_digest": manifest["capture_digest"], "path": path, "viewport": viewport},
        )
    return dict(manifest, available=True)


def load_capture(context: PreviewContext, capture_id):
    if not isinstance(capture_id, str) or not re.fullmatch(r"[0-9a-f]{32}", capture_id):
        fail("캡처 ID를 확인해주세요.")
    try:
        manifest = json.loads((context.capture_dir / f"{capture_id}.json").read_text(encoding="utf-8"))
        image = (context.capture_dir / f"{capture_id}.png").read_bytes()
    except (OSError, ValueError):
        fail("캡처를 찾을 수 없습니다.")
    body = {k: v for k, v in manifest.items() if k != "capture_digest"}
    if digest(body) != manifest.get("capture_digest") or content_digest(image) != manifest.get("screen_digest"):
        fail("캡처 파일이 변경되었습니다.", ErrorCode.POLICY_VIOLATION)
    return manifest, image


def _threads(context, current):
    rows = context.db.rows(
        "SELECT * FROM review_threads WHERE task_id=? AND kind='preview' ORDER BY created_at,id",
        (context.task_id,),
    )
    result = []
    for row in rows:
        anchor = json.loads(row["anchor_json"])
        is_current = anchor.get("capture_id") in current and current[anchor["capture_id"]]["capture_digest"] == row["evidence_digest"]
        result.append(
            {
                "id": row["id"],
                "anchor": anchor,
                "body": row["body"],
                "resolution": row["resolution"],
                "state": row["state"],
                "display_state": "stale" if row["state"] == "open" and not is_current else row["state"],
                "current": is_current,
                "version": row["version"],
                "created_at": row["created_at"],
            }
        )
    return result


def bootstrap(context: PreviewContext, agent_port):
    source = review.collect(context)["digest"]
    current = _current_captures(context, source)
    sessions = context.db.rows(
        "SELECT id,origin,state,generation,lease_resource IS NOT NULL AS owned,created_at FROM preview_sessions WHERE task_id=? ORDER BY generation DESC LIMIT 20",
        (context.task_id,),
    )
    for row in sessions:
        # Read-only: report a launched process lost after restart without writing on GET.
        row["process_lost"] = bool(row["state"] == "active" and row["owned"] and not _alive(row["id"]))
        if row["process_lost"]:
            row["state"] = "ended"
    captures = [
        {k: v for k, v in m.items() if k != "elements"} | {"current": m["id"] in current, "element_count": len(m["elements"])}
        for m in reversed(_manifests(context))
    ]
    decisions = context.db.rows(
        "SELECT * FROM review_decisions WHERE task_id=? AND kind='screen' ORDER BY created_at DESC",
        (context.task_id,),
    )
    current_digests = {m["capture_digest"] for m in current.values()}
    return {
        "services": [_public_service(s) | {"agent_port_conflict": s["port"] == agent_port} for s in context.services.values()],
        "sessions": sessions,
        "captures": captures,
        "threads": _threads(context, current),
        "decisions": [dict(d, current=d["evidence_digest"] in current_digests) for d in decisions],
        "browser_available": browser_config(context) is not None,
        "source_digest": source,
    }


def capture_detail(context: PreviewContext, capture_id):
    manifest, _ = load_capture(context, capture_id)
    current = _current_captures(context, review.collect(context)["digest"])
    return dict(manifest, current=capture_id in current)


def add_comment(context: PreviewContext, payload):
    manifest, _ = load_capture(context, payload.get("capture_id"))
    current = _current_captures(context, review.collect(context)["digest"])
    if manifest["id"] not in current:
        fail("최신 캡처가 아닙니다. 다시 캡처한 화면에 코멘트해주세요.", ErrorCode.STALE_VERSION)
    _session(context, manifest["session_id"], manifest["generation"])
    index = payload.get("element")
    if type(index) is not int or not 0 <= index < len(manifest["elements"]):
        fail("캡처에 없는 요소입니다.")
    body = context.public(review._text(payload.get("body"), "코멘트"))
    element = manifest["elements"][index]
    anchor = {
        "capture_id": manifest["id"],
        "service": manifest["service"],
        "path": manifest["path"],
        "viewport": manifest["viewport"],
        "element": {k: element[k] for k in ("index", "testid", "path", "rect", "text")},
    }
    thread_id = identifier()
    with context.db.transaction() as conn:
        conn.execute(
            "INSERT INTO review_threads(id,task_id,kind,module_id,anchor_json,evidence_digest,state,body,resolution,version,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (thread_id, context.task_id, "preview", context.services[manifest["service"]]["module"], json.dumps(anchor, ensure_ascii=False), manifest["capture_digest"], "open", body, "", 0, context.store.clock()),
        )
        context.store._event(conn, context.task_id, "PreviewCommentAdded", {"thread_id": thread_id, "capture_id": manifest["id"]})
    return {"thread_id": thread_id, "state": "open", "version": 0}


def decide(context: PreviewContext, payload):
    decision = payload.get("decision")
    if decision not in {"approved", "rejected"}:
        fail("화면 검토 결과를 선택해주세요.")
    note = context.public(review._text(payload.get("note", ""), "의견", required=decision == "rejected"))
    actor = review._actor(payload)
    manifest, _ = load_capture(context, payload.get("capture_id"))
    source = review.collect(context)["digest"]
    current = _current_captures(context, source)
    if manifest["id"] not in current:
        fail("최신 캡처·현재 소스 기준 화면이 아닙니다. 다시 캡처한 뒤 결정해주세요.", ErrorCode.STALE_VERSION)
    _session(context, manifest["session_id"], manifest["generation"])
    with context.db.transaction() as conn:
        context.store.assert_change_allowed(context.task_id, conn)
        if decision == "approved":
            rows = conn.execute(
                "SELECT anchor_json FROM review_threads WHERE task_id=? AND kind='preview' AND state='open'",
                (context.task_id,),
            ).fetchall()
            same = [r for r in rows if json.loads(r["anchor_json"]).get("service") == manifest["service"] and json.loads(r["anchor_json"]).get("path") == manifest["path"]]
            if same:
                fail(f"이 화면에 처리하지 않은 코멘트 {len(same)}개가 있습니다.", ErrorCode.POLICY_VIOLATION)
        if conn.execute(
            "SELECT 1 FROM review_decisions WHERE task_id=? AND kind='screen' AND evidence_digest=? AND actor=?",
            (context.task_id, manifest["capture_digest"], actor),
        ).fetchone():
            fail("같은 화면 캡처에 이미 결정을 남겼습니다.", ErrorCode.STALE_VERSION)
        decision_id = identifier()
        conn.execute(
            "INSERT INTO review_decisions(id,task_id,evidence_digest,kind,actor,decision,note,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (decision_id, context.task_id, manifest["capture_digest"], "screen", actor, decision, note, context.store.clock()),
        )
        context.store._event(conn, context.task_id, "ReviewDecisionRecorded", {"decision_id": decision_id, "kind": "screen", "decision": decision, "evidence_digest": manifest["capture_digest"]})
    return {"decision_id": decision_id, "decision": decision, "capture_digest": manifest["capture_digest"]}
