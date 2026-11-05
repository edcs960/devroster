"""Optional Graphify builds operate on a bounded, sanitized source snapshot."""

import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import ClassVar

from agent_dev.adapters.project_setup import atomic_json
from agent_dev.adapters.source_explorer import SourceExplorer
from agent_dev.adapters.state.sqlite.safety import ExecutionSafety
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.domain.content import content_digest, digest, redact
from agent_dev.domain.errors import DomainError, ErrorCode


class SourceJobs:
    instances: ClassVar[dict] = {}
    instance_lock = threading.Lock()

    @classmethod
    def for_root(cls, root):
        key = str(Path(root).resolve())
        with cls.instance_lock:
            if key not in cls.instances:
                cls.instances[key] = cls(root)
            return cls.instances[key]

    def __init__(self, root):
        self.root = root
        self.access = SourceExplorer(root)
        self.lock = threading.Lock()
        self.jobs = {}
        self.cancelled = set()
        self.processes = {}

    def start(self, module):
        self.access._module(module)
        binary = shutil.which("graphify")
        if not binary:
            return {
                "state": "installation_required",
                "message": "격리 환경에 graphifyy[mcp]를 버전 고정하여 설치하고 graphify 실행 파일을 PATH에 등록해주세요.",
                "official_documentation": "https://github.com/Graphify-Labs/graphify",
            }
        with self.lock:
            if any(
                j["module"] == module and j["state"] in {"queued", "running"}
                for j in self.jobs.values()
            ):
                raise DomainError(ErrorCode.LEASE_CONFLICT, "이 모듈의 그래프를 준비 중입니다.")
            key = uuid.uuid4().hex
            row = {
                "id": key,
                "module": module,
                "state": "queued",
                "created_at": time.time(),
                "message": "소스 범위 확인 중",
                "llm_calls": 0,
            }
            self.jobs[key] = row
        threading.Thread(target=self._run, args=(key, module, binary), daemon=True).start()
        return dict(row)

    def status(self, key):
        with self.lock:
            if key not in self.jobs:
                raise DomainError(ErrorCode.INVALID_INPUT, "그래프 작업 ID를 확인해주세요.")
            return dict(self.jobs[key])

    def cancel(self, key):
        with self.lock:
            if key not in self.jobs:
                raise DomainError(ErrorCode.INVALID_INPUT, "그래프 작업 ID를 확인해주세요.")
            self.cancelled.add(key)
            process = self.processes.get(key)
            if process and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except OSError:
                    pass
        return self.status(key)

    def _state(self, key, **values):
        with self.lock:
            self.jobs[key].update(values)

    def _run(self, key, module, binary):
        try:
            runtime = WorkflowRuntime(Path(self.root))
            with ExecutionSafety(runtime.store).lease("source-graph:" + module):
                self._state(
                    key, state="running", message="허용된 소스만 복사하여 코드 관계 추출 중"
                )
                manifest = self.access.manifest(module)
                directory = self.access.graph_path(module).parent
                directory.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="build-", dir=directory) as temp:
                    stage = Path(temp)
                    source = stage / "source"
                    source.mkdir()
                    output = stage / "output"
                    total = 0
                    for item in manifest:
                        if key in self.cancelled:
                            raise DomainError(
                                ErrorCode.EXTERNAL_TRANSIENT, "그래프 준비를 취소했습니다."
                            )
                        with self.access._open(module, item["path"]) as (fd, _info):
                            raw = os.read(fd, 1_048_577)
                        if content_digest(raw) != item["digest"]:
                            raise DomainError(
                                ErrorCode.STALE_VERSION, "소스가 변경되었습니다. 다시 준비해주세요."
                            )
                        total += len(raw)
                        if total > 50_000_000:
                            raise DomainError(
                                ErrorCode.INVALID_INPUT,
                                "소스가 50MB를 넘습니다. 읽기 범위를 줄여주세요.",
                            )
                        target = source / item["path"]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(redact(raw.decode("utf-8"), self.access.secrets), encoding="utf-8")
                    env = {
                        k: os.environ[k]
                        for k in ("PATH", "HOME", "LANG", "TMPDIR")
                        if k in os.environ
                    }
                    env["GRAPHIFY_HOME"] = str(stage / "home")
                    with tempfile.TemporaryFile() as log:
                        process = subprocess.Popen(
                            [
                                binary,
                                "extract",
                                str(source),
                                "--code-only",
                                "--no-cluster",
                                "--out",
                                str(output),
                            ],
                            cwd=stage,
                            env=env,
                            stdout=log,
                            stderr=log,
                            start_new_session=True,
                        )
                        with self.lock:
                            self.processes[key] = process
                        deadline = time.monotonic() + 900
                        try:
                            while process.poll() is None:
                                if (
                                    key in self.cancelled
                                    or time.monotonic() > deadline
                                    or log.tell() > 2_000_000
                                ):
                                    os.killpg(process.pid, signal.SIGTERM)
                                    try:
                                        process.wait(timeout=2)
                                    except subprocess.TimeoutExpired:
                                        os.killpg(process.pid, signal.SIGKILL)
                                        process.wait(timeout=2)
                                    raise DomainError(
                                        ErrorCode.EXTERNAL_TRANSIENT,
                                        "그래프 준비가 취소되었거나 실행 예산을 초과했습니다.",
                                    )
                                time.sleep(0.1)
                            if process.returncode != 0:
                                raise DomainError(
                                    ErrorCode.EXTERNAL_TRANSIENT,
                                    "Graphify 추출 실패. 설치 버전과 지원 언어를 확인해주세요.",
                                )
                        finally:
                            with self.lock:
                                self.processes.pop(key, None)
                    path = output / "graph.json"
                    if not path.is_file() or path.is_symlink() or path.stat().st_size > 20_000_000:
                        raise DomainError(
                            ErrorCode.INVALID_INPUT, "유효한 graph.json을 생성하지 못했습니다."
                        )
                    graph = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(graph, dict) or not isinstance(graph.get("nodes"), list) or any(
                        not isinstance(node, dict) for node in graph["nodes"]
                    ):
                        raise ValueError("malformed graph nodes")
                    for node in graph.get("nodes", []):
                        file = node.get("source_file", "")
                        if file and Path(file).is_absolute():
                            if Path(file).is_relative_to(source):
                                node["source_file"] = str(Path(file).relative_to(source))
                            else:
                                node.pop("source_file", None)
                    if digest(self.access.manifest(module)) != digest(manifest):
                        raise DomainError(
                            ErrorCode.STALE_VERSION,
                            "빌드 중 원문이 변경되었습니다. 이전 정상 그래프를 유지합니다.",
                        )
                    if key in self.cancelled:
                        raise DomainError(
                            ErrorCode.EXTERNAL_TRANSIENT, "그래프 준비를 취소했습니다."
                        )
                    result = self.access.publish_graph(
                        module, graph, manifest, tool_version=str(Path(binary).name)
                    )
                    self._state(
                        key,
                        **result,
                        message="코드 관계 그래프 준비 완료",
                        source_bytes=total,
                        finished_at=time.time(),
                    )
                    atomic_json(directory / "last-build.json", self.status(key))
        except (DomainError, OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
            self._state(
                key,
                state="cancelled" if key in self.cancelled else "error",
                message=str(exc)
                if isinstance(exc, DomainError)
                else "그래프를 준비하지 못했습니다. 설치·폴더 권한을 확인해주세요.",
                finished_at=time.time(),
            )

    def close(self):
        for key in list(self.processes):
            self.cancel(key)
