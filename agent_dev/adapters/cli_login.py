"""App-private, process-local CLI login jobs for the settings screen."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
import uuid

from agent_dev.adapters.windows_launch import windows_command
from agent_dev.core.cli_home import cli_homes, isolated_environment
from agent_dev.domain.errors import DomainError, ErrorCode

LOGIN_ARGS = {"codex_cli": ["login", "--device-auth"], "claude_cli": ["auth", "login"]}
TIMEOUT_SECONDS = 15 * 60
MAX_LINES = 200
MAX_OUTPUT_BYTES = 64 * 1024
MAX_COMPLETED_JOBS = 32
COMPLETED_TTL_SECONDS = 60 * 60
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()


def _home(provider: str) -> str:
    key = "CODEX_HOME" if provider == "codex_cli" else "CLAUDE_CONFIG_DIR"
    return str(cli_homes()[key])


def _command(provider: str, command: str) -> list[str]:
    if provider not in LOGIN_ARGS:
        raise DomainError(ErrorCode.INVALID_INPUT, "웹 로그인은 Codex·Claude CLI만 지원합니다.")
    if not isinstance(command, str) or not command.strip() or len(command) > 500:
        raise DomainError(ErrorCode.INVALID_INPUT, "CLI 실행파일을 먼저 설정해주세요.")
    return [command.strip(), *LOGIN_ARGS[provider]]


def _prune_locked() -> None:
    now = time.time()
    completed = sorted(
        (job for job in _JOBS.values() if job["done"]),
        key=lambda job: job.get("finished_at", job.get("started_at", 0)),
        reverse=True,
    )
    for job in completed[MAX_COMPLETED_JOBS:]:
        _JOBS.pop(job["id"], None)
    for job in completed[:MAX_COMPLETED_JOBS]:
        if now - job.get("finished_at", job.get("started_at", now)) > COMPLETED_TTL_SECONDS:
            _JOBS.pop(job["id"], None)


def _snapshot_locked(job: dict) -> dict:
    chunks = job["output"]
    raw = (
        b"".join(chunks).decode("utf-8", errors="replace")
        if chunks and isinstance(chunks[0], bytes)
        else "\n".join(chunks)
    )
    lines = _ANSI.sub("", raw).splitlines()
    return {
        "job_id": job["id"],
        "provider": job["provider"],
        "output": "\n".join(lines[:MAX_LINES]).strip(),
        "done": job["done"],
        "returncode": job["returncode"],
        "truncated": bool(job.get("truncated") or len(lines) > MAX_LINES),
    }


def start(provider: str, command: str, owner: str | tuple[str, str] | None = None) -> dict:
    argv = _command(provider, command)
    with _LOCK:
        _prune_locked()
        running = next(
            (job for job in _JOBS.values() if job["provider"] == provider and not job["done"]),
            None,
        )
        if running:
            if (
                running["command"] != argv[0]
                or running.get("owner") != owner
                or running.get("home", _home(provider)) != _home(provider)
            ):
                raise DomainError(ErrorCode.STALE_VERSION, "다른 로그인 작업이 진행 중입니다.")
            return _snapshot_locked(running)
        try:
            process = subprocess.Popen(
                windows_command(argv),
                env=isolated_environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
        except OSError as exc:
            raise DomainError(
                ErrorCode.CAPABILITY_UNAVAILABLE, f"CLI를 실행하지 못했습니다: {argv[0]}"
            ) from exc
        job = {
            "id": uuid.uuid4().hex,
            "provider": provider,
            "command": argv[0],
            "owner": owner,
            "home": _home(provider),
            "process": process,
            "output": [],
            "output_bytes": 0,
            "truncated": False,
            "done": False,
            "returncode": None,
            "started_at": time.time(),
        }
        _JOBS[job["id"]] = job
    threading.Thread(target=_collect, args=(job,), daemon=True).start()
    return view(job)


def _stop(job: dict) -> None:
    """Stop only the private process group created for this login."""
    process = job["process"]
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            if process.poll() is None:
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _collect(job: dict) -> None:
    process = job["process"]
    timer = threading.Timer(TIMEOUT_SECONDS, _stop, args=(job,))
    timer.daemon = True
    timer.start()
    try:
        assert process.stdout is not None
        while chunk := os.read(process.stdout.fileno(), 4096):
            with _LOCK:
                available = MAX_OUTPUT_BYTES - job["output_bytes"]
                if available > 0:
                    kept = chunk[:available]
                    job["output"].append(kept)
                    job["output_bytes"] += len(kept)
                if len(chunk) > available:
                    job["truncated"] = True
        process.wait()
    finally:
        timer.cancel()
        if process.stdout is not None:
            process.stdout.close()
        with _LOCK:
            job["done"] = True
            job["returncode"] = process.poll()
            job["finished_at"] = time.time()
            _prune_locked()


def _job_locked(job_id: str, owner: str | tuple[str, str] | None) -> dict:
    _prune_locked()
    job = _JOBS.get(job_id) if isinstance(job_id, str) else None
    if (
        job is None
        or job.get("owner") != owner
        or job.get("home", _home(job["provider"])) != _home(job["provider"])
    ):
        raise DomainError(ErrorCode.INVALID_INPUT, "로그인 작업을 찾을 수 없습니다. 다시 시작해주세요.")
    return job


def auth_result(job_id: str, probe, owner: str | tuple[str, str] | None = None) -> bool:
    """Probe the frozen command once for a completed job."""
    with _LOCK:
        job = _job_locked(job_id, owner)
        auth_lock = job.setdefault("auth_lock", threading.Lock())
    with auth_lock:
        if "logged_in" not in job:
            result = probe(job["provider"], job["command"])
            with _LOCK:
                job["logged_in"] = result
        return job["logged_in"]


def status(job_id: str, owner: str | tuple[str, str] | None = None) -> dict:
    with _LOCK:
        return _snapshot_locked(_job_locked(job_id, owner))


def cancel(job_id: str, owner: str | tuple[str, str] | None = None) -> dict:
    with _LOCK:
        job = _job_locked(job_id, owner)
        running = not job["done"]
    if running:
        _stop(job)
    return view(job)


def shutdown(owner: str) -> None:
    with _LOCK:
        running = [
            job for job in _JOBS.values()
            if not job["done"]
            and (
                job.get("owner") == owner
                or isinstance(job.get("owner"), tuple) and job["owner"][0] == owner
            )
        ]
    for job in running:
        _stop(job)


def view(job: dict) -> dict:
    with _LOCK:
        return _snapshot_locked(job)
