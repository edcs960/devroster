"""Read-only local readiness checks; authentication probes require an explicit option."""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from agent_dev.adapters.secrets import registered_secrets
from agent_dev.adapters.state.sqlite.database import SCHEMA_VERSION, Database
from agent_dev.adapters.workspace import configuration_root, metadata
from agent_dev.domain.content import redact
from agent_dev.domain.errors import DomainError


def codex_model_check(config):
    """Local cache is a hint, never live account authorization."""
    import tomllib

    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    selected = config.get("model", "")
    try:
        if not selected:
            source = home / "config.toml"
            if source.is_file() and source.stat().st_size <= 1_000_000:
                selected = tomllib.loads(source.read_text(encoding="utf-8")).get("model", "")
        if not selected:
            # No fixed model: the CLI runs its own default, nothing to choose (first-use review).
            return "pass", "모델을 고정하지 않아 CLI 기본 모델로 실행합니다."
        source = home / "models_cache.json"
        if not source.is_file() or source.stat().st_size > 4_000_000:
            return (
                "warn",
                "모델 사용 가능 여부를 확인하지 않았습니다. CLI 계정에서 지원하는 모델을 선택해주세요.",
            )
        names = {
            row.get("slug")
            for row in json.loads(source.read_text(encoding="utf-8")).get("models", [])
            if isinstance(row, dict)
        }
        return (
            ("pass", "선택한 모델이 로컬 목록에 있습니다. 실제 접근 권한은 확인하지 않았습니다.")
            if selected in names
            else (
                "warn",
                "선택한 모델이 로컬 CLI 목록에 없습니다. 지원하지 않는 모델이거나 목록이 오래되었을 수 있습니다.",
            )
        )
    except (OSError, ValueError, TypeError, AttributeError):
        return "warn", "로컬 모델 설정·목록을 확인하지 못했습니다."


def diagnose(root: Path, *, check_auth=False):
    from agent_dev.adapters.workflow_runtime import project_root

    root = configuration_root(root)
    workspace = project_root(root)
    checks = []

    def add(name, status, message, fix=""):
        checks.append({"check": name, "status": status, "message": message, "fix": fix})

    add(
        "python",
        "pass" if sys.version_info >= (3, 11) else "fail",
        sys.version.split()[0],
        "Python 3.11 이상을 사용해주세요.",
    )
    add(
        "git",
        "pass" if shutil.which("git") else "fail",
        "Git을 찾았습니다." if shutil.which("git") else "Git을 찾을 수 없습니다.",
        "Git을 설치하고 커밋이 있는 소스 저장소를 준비해주세요.",
    )
    marker = None
    try:
        marker = metadata(root)
        add(
            "workspace",
            "pass" if marker else "warn",
            "프로젝트 폴더에 등록된 작업 공간입니다." if marker else "이전 방식이거나 아직 준비되지 않은 폴더입니다.",
            "새 프로젝트는 devroster init --root PROJECT 로 준비해주세요.",
        )
    except (ValueError, OSError, DomainError):
        add(
            "workspace",
            "fail",
            "작업 공간 표시 파일이 올바르지 않습니다.",
            "workspace.json 의 format_version·project_id·name 을 복원하거나 고쳐주세요.",
        )
    if (root / "upgrade-journal.json").exists():
        add(
            "upgrade_recovery",
            "fail",
            "중단된 업그레이드 기록이 있어 실행이 막혀 있습니다.",
            "devroster upgrade --recover 실행 후 devroster doctor 로 다시 확인해주세요.",
        )
    if marker and marker.get("engine_version", "0.2.0") != "0.3.0":
        add(
            "workspace_version",
            "warn",
            "작업 공간 설정이 현재 버전보다 오래되었습니다.",
            "devroster upgrade 로 사전 점검하고, --apply 로 백업 후 설정을 옮겨주세요.",
        )
    config = {}
    try:
        config = json.loads((root / "config/runtime.json").read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise TypeError()
        add("configuration", "pass", "실행 설정을 읽었습니다.")
    except (ValueError, TypeError, OSError):
        add(
            "configuration",
            "fail",
            "실행 설정이 없거나 올바르지 않습니다.",
            "새 작업 공간이면 devroster init 을 실행하고, 아니면 config/runtime.json 을 복원해주세요.",
        )
    from agent_dev.adapters.storage_registry import workspace_locations

    db_path = workspace_locations(root).state_db
    profile = {}
    tools = []
    if db_path.exists():
        try:
            db = Database(db_path, read_only=True)
            valid = db.rows("PRAGMA quick_check")[0]["quick_check"] == "ok" and not db.rows(
                "PRAGMA foreign_key_check"
            )
            add(
                "database_integrity",
                "pass" if valid else "fail",
                "저장소 무결성과 참조 관계를 확인했습니다.",
                "검증된 백업을 확인해주세요. 손상된 저장소를 덮어쓰지 마세요.",
            )
            version = db.rows("SELECT MAX(version) AS version FROM schema_versions")[0]["version"]
            add(
                "database_schema",
                "pass" if version == SCHEMA_VERSION else "fail",
                f"저장소 버전: {version}.",
                "이 저장소와 맞는 DevRoster 버전을 사용해주세요.",
            )
            rows = db.rows(
                "SELECT content_json FROM project_profile_versions ORDER BY version DESC LIMIT 1"
            )
            if rows:
                profile = json.loads(rows[0]["content_json"])
            tools = [
                json.loads(row["content_json"])
                for row in db.rows("SELECT content_json FROM tool_connections")
            ]
            active = db.rows(
                "SELECT resource,expires_at FROM leases WHERE expires_at>?", (time.time(),)
            )
            add(
                "active_leases",
                "warn" if active else "pass",
                f"실행 중 잠금: {len(active)}개.",
                "백업·이동 전에 실행이 끝나기를 기다려주세요.",
            )
            stale = db.rows(
                "SELECT p.id FROM phase_attempts p LEFT JOIN leases l ON l.resource=p.lease_resource WHERE p.state='running' AND (l.resource IS NULL OR l.token<>p.lease_token OR l.expires_at<=?)",
                (time.time(),),
            )
            add(
                "recovery",
                "warn" if stale else "pass",
                f"중단된 실행: {len(stale)}개.",
                "devroster task explain TASK-ID 로 확인한 뒤 승인된 작업을 이어서 실행해주세요.",
            )
        except (sqlite3.Error, ValueError, OSError, KeyError):
            add(
                "database",
                "fail",
                "저장소를 확인하지 못했습니다.",
                "저장소 버전 호환성과 검증된 백업을 확인해주세요.",
            )
    else:
        add(
            "database",
            "warn",
            "저장소가 아직 준비되지 않았습니다. 진단은 저장소를 만들지 않습니다.",
            "devroster init 또는 안내된 이전 절차로 준비해주세요.",
        )
    profile_path = root / "config/profile.json"
    if profile_path.exists():
        try:
            local = json.loads(profile_path.read_text(encoding="utf-8"))
            if not isinstance(local, dict):
                raise TypeError()
            if profile and local != profile:
                add(
                    "profile_registration",
                    "warn",
                    "설정 파일이 등록된 프로필과 다릅니다.",
                    "내용을 확인하고 새 작업 승인 전에 devroster project profile PROJECT-ID PROFILE-PATH 로 등록해주세요.",
                )
        except (ValueError, TypeError, OSError):
            add(
                "profile_file",
                "fail",
                "프로필 JSON 형식이 올바르지 않습니다.",
                "등록 전에 config/profile.json 을 고쳐주세요.",
            )
    repositories = profile.get("repositories", {})
    if not repositories:
        add(
            "repositories",
            "warn",
            "등록된 저장소 프로필이 없습니다.",
            "설정 화면에서 저장소와 단계를 저장해주세요.",
        )
    for name, binding in repositories.items():
        source = (workspace / binding.get("path", ".")).resolve()
        try:
            probe = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "--verify", "HEAD"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            add(
                f"repository:{name}",
                "pass" if probe.returncode == 0 else "fail",
                "커밋이 있는 소스 저장소를 찾았습니다."
                if probe.returncode == 0
                else "저장소에 읽을 수 있는 커밋(HEAD)이 없습니다.",
                "소스 저장소를 복제하거나 초기화하고 첫 커밋을 만들어주세요.",
            )
            top = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if top.returncode == 0 and Path(top.stdout.strip()).resolve() != source:
                add(
                    f"repository_root:{name}",
                    "fail",
                    "등록한 경로가 Git 저장소 안쪽 폴더입니다. 모듈 경로 정리가 필요합니다.",
                    "Git 준비 후 작업 승인 전에 설정을 다시 저장해주세요.",
                )
            branch = subprocess.run(
                ["git", "-C", str(source), "branch", "--show-current"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.strip()
            add(
                f"branch:{name}",
                "pass" if branch else "warn",
                f"소스 브랜치: {branch or 'detached'}. 실행은 작업 전용 worktree에서 합니다.",
            )
            if marker:
                ignored = subprocess.run(
                    ["git", "-C", str(source), "check-ignore", "-q", ".agent-dev/state.db"],
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
                add(
                    f"ignore:{name}",
                    "pass" if ignored.returncode == 0 else "warn",
                    "작업 공간 폴더가 Git에서 제외되어 있습니다."
                    if ignored.returncode == 0
                    else "작업 공간 폴더가 Git에 추가될 수 있습니다.",
                    "/.agent-dev/ 와 생성 결과 폴더를 .gitignore 에 추가해주세요.",
                )
        except (OSError, subprocess.TimeoutExpired):
            add(
                f"repository:{name}",
                "fail",
                "저장소 확인에 실패했거나 시간이 초과되었습니다.",
                "저장소 경로와 Git 설치를 확인해주세요.",
            )
        if not binding.get("allowed_paths"):
            add(
                f"allow_paths:{name}",
                "fail",
                "수정 허용 경로가 지정되지 않았습니다.",
                "모듈의 수정 허용 경로를 소스·테스트 폴더로 좁혀주세요.",
            )
    verification = profile.get("verification", {})
    commands = verification.get("commands", [])
    required = verification.get("required_gates", [])
    configured = {item.get("name") for item in commands}
    gate_valid = bool(required) and all(name in configured for name in required)
    add(
        "verification",
        "pass" if gate_valid else "warn",
        "필수 검증 명령이 등록되어 있습니다."
        if gate_valid
        else "필수 검증 명령이 부족합니다.",
        "실제 테스트·lint·빌드 명령을 검증 명령으로 추가하고 필수로 지정해주세요.",
    )
    if configured == {"diff_check"}:
        add(
            "test_coverage",
            "warn",
            "기본 diff 검사는 공백 오류만 확인하고 동작은 검사하지 않습니다.",
            "실행·게시 전에 프로젝트의 실제 테스트 명령을 등록해주세요.",
        )
    probes = {
        "codex_cli": ["codex", "login", "status"],
        "claude_cli": ["claude", "auth", "status", "--json"],
    }
    from agent_dev.adapters.ai_providers import API_PROVIDERS, credential_ready
    from agent_dev.adapters.environment import process_environment

    try:
        api_environment = process_environment(root, profile.get("environment_keys", []))
    except (DomainError, ValueError, OSError):
        api_environment = {}
        add(
            "environment",
            "fail",
            "등록한 비공개 환경변수 값을 읽지 못했습니다.",
            "실행 전에 설정 화면에서 빠진 환경변수 값을 다시 입력해주세요.",
        )
    runners = profile.get("runners", [])
    for runner in runners:
        provider = runner.get("provider", "")
        if provider in API_PROVIDERS:
            ready = credential_ready(provider, runner.get("config", {}), api_environment)
            add(
                f"api:{runner.get('id', provider)}",
                "warn" if ready else "fail" if runner.get("enabled") else "warn",
                "API 인증 값이 있습니다. 실제 인증·모델 접근은 확인하지 않았습니다."
                if ready
                else "API 인증 값이 없습니다.",
                "등록한 환경변수 값을 입력해주세요. API는 텍스트 생성만 하며, 파일 수정은 CLI 도구가 필요합니다.",
            )
            continue
        if provider == "gemini_cli":
            command = [runner.get("config", {}).get("command", "gemini"), "--version"]
        else:
            command = list(probes[provider]) if provider in probes else None
        if command:
            command[0] = runner.get("config", {}).get("command", command[0])
        available = bool(command and shutil.which(command[0]))
        enabled = runner.get("enabled") is True
        add(
            f"runner:{runner.get('id', provider)}",
            "pass" if available else "fail" if enabled else "warn",
            f"{provider}: {'설치됨' if available else '찾을 수 없음'} · {'사용' if enabled else '사용 안 함'}.",
            "선택한 CLI를 설치하거나 이 도구를 꺼주세요.",
        )
        if provider == "codex_cli":
            model_state, model_message = codex_model_check(runner.get("config", {}))
            add(
                f"runner_model:{runner.get('id', provider)}",
                model_state,
                model_message,
                "CLI에서 사용할 수 있는 모델을 확인해 설정에서 모델을 고른 뒤 저장하고, 새 작업부터 적용해주세요.",
            )
        state, message = (
            "warn",
            "로그인 상태를 확인하지 않았습니다. 프로필의 로그인 표시는 신고값입니다.",
        )
        if check_auth and available and command and provider != "gemini_cli":
            try:
                from agent_dev.core.cli_home import isolated_environment

                probe = subprocess.run(
                    command,
                    cwd=workspace,
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                    env=isolated_environment(),
                )
                authenticated = probe.returncode == 0
                if provider == "claude_cli" and authenticated:
                    authenticated = json.loads(probe.stdout).get("loggedIn") is True
                state = "pass" if authenticated else "fail"
                message = (
                    "CLI가 로그인 상태라고 응답했습니다."
                    if authenticated
                    else "CLI가 로그인 상태를 확인해주지 않았습니다."
                )
            except (OSError, ValueError, subprocess.TimeoutExpired):
                state, message = "fail", "로그인 확인에 실패했거나 시간이 초과되었습니다."
        add(
            f"auth:{runner.get('id', provider)}",
            state,
            message,
            "DevRoster 전용 로그인을 진행한 뒤 상태를 다시 확인해주세요.",
        )
        # A failed login is already reported above; one item per problem (first-use review).
        if runner.get("authenticated") is not True and state != "fail":
            add(
                f"runner_policy:{runner.get('id', provider)}",
                "warn",
                "로그인은 확인됐지만 저장된 설정에 아직 반영되지 않았습니다."
                if state == "pass"
                else "등록된 프로필에 이 도구의 로그인이 꺼져 있습니다.",
                "로그인을 확인한 뒤 설정을 다시 저장해주세요.",
            )
    if not runners:
        add(
            "runners",
            "warn",
            "등록된 AI 도구가 없습니다.",
            "설정 화면에서 사용할 AI 도구를 선택해주세요.",
        )
    for tool in tools:
        ready = all(tool.get(key) is True for key in ("enabled", "authenticated", "healthy"))
        ref = tool.get("credential_ref", "")
        credential = not ref or bool(os.environ.get(ref))
        add(
            f"tool:{tool.get('connection_id', '')}",
            "pass" if ready and credential else "warn",
            "등록된 준비 상태만 확인했습니다. 실제 도구 인증은 확인하지 않았습니다.",
            "연결의 권한·범위와 인증 환경변수를 확인해주세요.",
        )
    try:
        free = shutil.disk_usage(workspace if workspace.exists() else workspace.parent).free
        add(
            "disk",
            "pass" if free >= 100 * 1024 * 1024 else "warn",
            f"남은 디스크: {free // (1024 * 1024)} MiB.",
            "worktree 생성·백업 전에 디스크 공간을 확보해주세요.",
        )
    except OSError:
        add("disk", "warn", "디스크 사용량을 확인하지 못했습니다.")
    failed = sum(check["status"] == "fail" for check in checks)
    return redact(
        {
            "healthy": failed == 0,
            "failed": failed,
            "warnings": sum(check["status"] == "warn" for check in checks),
            "project": marker,
            "configuration_root": str(root),
            "workspace": str(workspace),
            "flags": config.get("orchestrator_v2", {}),
            "authentication_probed": check_auth,
            "checks": checks,
        },
        registered_secrets(root),
    )
