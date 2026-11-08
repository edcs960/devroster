"""Shared CLI/web setup with atomic settings/profile revisions and private environment."""

import copy
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path
from typing import NoReturn

from agent_dev.adapters.ai_providers import (
    API_PROVIDERS,
    CLI_PROVIDERS,
    PROVIDERS,
    credential_ready,
    defaults,
    normalize,
)
from agent_dev.adapters.environment import MASK, environment_file, read_environment, validate_key
from agent_dev.adapters.presets import PRESETS
from agent_dev.adapters.presets import detect as detect_preset
from agent_dev.adapters.presets import profile as preset_profile
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.adapters.state.sqlite.store import WorkflowRepository
from agent_dev.adapters.storage_registry import workspace_locations
from agent_dev.adapters.workspace import configuration_root, engine_configuration, metadata
from agent_dev.core.io import locked
from agent_dev.core.output_paths import slugify
from agent_dev.core.storage_locations import EXTERNAL
from agent_dev.domain.content import canonical, digest
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.execution_limits import limits
from agent_dev.domain.model_routing import routing_policy, select_model
from agent_dev.domain.project_roles import normalize as normalize_roles
from agent_dev.domain.project_roles import view as roles_view


def invalid(message) -> NoReturn:
    raise DomainError(ErrorCode.INVALID_INPUT, message)


def bounded(value, label, size=200, empty=False):
    if not isinstance(value, str) or len(value) > size or any(ord(c) < 32 for c in value):
        invalid(label + " 형식을 확인해주세요.")
    if not empty and not value.strip():
        invalid(label + "을(를) 입력해주세요.")
    return value.strip()


# "changed" runs only the gates of repositories a task changed.
VERIFICATION_SCOPES = ("all", "changed")

# Profile keys that bound structured role edits, with their settings-screen labels.
STRUCTURED_PATH_KEYS = {"test_paths": "테스트 경로", "document_paths": "문서 경로"}
STRUCTURED_PATH_DEFAULTS = {
    # src/test: Maven/Gradle layouts; the patterns: tests next to their source (React, Vitest).
    "test_paths": ["tests", "src/test", "*.test.*", "*.spec.*"],
    "document_paths": ["docs", "README.md"],
}
# How an approved task closes (ADR 0003): merge locally, or through GitHub PRs and CI checks.
COMPLETION_MODES = {"local", "github"}


def relative(value, label, dot=False):
    value = bounded(value, label, 500)
    path = Path(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or any(p in {".git", ".agent-dev"} for p in path.parts)
        or (not dot and value == ".")
    ):
        invalid(label + "은(는) 소스 폴더 기준의 명시적 상대 경로여야 합니다.")
    return path.as_posix()


def git_root(path):
    try:
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=5,
            check=False,
        )
        return Path(result.stdout.strip()).resolve() if result.returncode == 0 else path
    except (OSError, subprocess.TimeoutExpired):
        return path


def private_directory(path):
    """Create a private folder (0700 on POSIX) without following a symbolic link."""
    path = Path(path)
    if path.is_symlink():
        invalid("비공개 저장 위치에 심볼릭 링크를 사용할 수 없습니다.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        path.chmod(0o700)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        invalid("설정 파일에 심볼릭 링크를 사용할 수 없습니다.")
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=".setup-", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        Path(name).chmod(0o600)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


class ProjectSetup:
    def __init__(self, root):
        from agent_dev.adapters.workflow_runtime import project_root

        # The project folder of a legacy layout keeps its settings under the engine folder.
        # Saving there as a new workspace would write a marker that moves every CLI call.
        self.config = engine_configuration(configuration_root(Path(root)))
        self.root = project_root(self.config)
        self.locations = workspace_locations(self.config)
        # The folder that holds the canonical DB: <source>/.agent-dev, or the external config.
        self.db_path = self.locations.state_db
        self.managed = self.db_path.parent
        self.marker = metadata(self.config)
        self.legacy = self.config != self.root and self.config != self.managed

    def preset(self):
        """The saved preset, or for a project never saved the one its build files suggest."""
        if self.marker:
            return self.marker.get("preset", "generic")
        return "generic" if self.legacy else detect_preset(self.root)

    def current_profile(self, db=None):
        project_id = (self.marker or {}).get("project_id", slugify(self.root.name))
        if db is None and not self.db_path.is_file():
            return project_id, 0, None
        database = db or Database(self.db_path, read_only=True)
        if not self.marker and database.rows(
            "SELECT name FROM sqlite_master WHERE name='workspace_settings'"
        ):
            settings = database.rows("SELECT settings_json FROM workspace_settings WHERE id=1")
            if settings:
                project_id = json.loads(settings[0]["settings_json"])["project_id"]
        rows = database.rows(
            "SELECT version,content_json FROM project_profile_versions "
            "WHERE project_id=? ORDER BY version DESC LIMIT 1",
            (project_id,),
        )
        return (
            (project_id, rows[0]["version"], json.loads(rows[0]["content_json"]))
            if rows
            else (project_id, 0, None)
        )

    def read(self):
        project_id, profile_version, profile = self.current_profile()
        registered = profile is not None
        value = None
        revision = 0
        if self.db_path.is_file():
            db = Database(self.db_path, read_only=True)
            if db.rows("SELECT name FROM sqlite_master WHERE name='workspace_settings'"):
                rows = db.rows("SELECT revision,settings_json FROM workspace_settings WHERE id=1")
                if rows:
                    value = json.loads(rows[0]["settings_json"])
                    value["revision"] = rows[0]["revision"]
                    revision = value["revision"]
                    if value["profile_version"] != profile_version:
                        value = None  # Reflect profiles revised through the existing CLI, too.
        if value is None:
            profile = profile or self.initial_profile()
            runners = {p: defaults(p) for p in PROVIDERS}
            for row in profile.get("runners", []):
                if row.get("provider") in runners:
                    runners[row["provider"]].update(
                        {
                            k: v
                            for k, v in row.get("config", {}).items()
                            if k in runners[row["provider"]]
                        }
                    )
                    runners[row["provider"]]["enabled"] = row.get("enabled", True)
            modules = []
            for key, row in profile.get("repositories", {}).items():
                modules.append(
                    {
                        "id": key,
                        "path": row["path"],
                        "preset": self.preset()
                        if key == "source"
                        else "generic",
                        "allowed_paths": row.get(
                            "allowed_paths", profile.get("allowed_paths", ["src", "tests"])
                        ),
                        "base_ref": row.get("base_ref", "HEAD"),
                    }
                )
            value = {
                "name": (self.marker or {}).get(
                    "name", getattr(self, "legacy_name", self.root.name)
                ),
                "project_id": project_id,
                "revision": revision,
                "modules": modules,
                "ai": {
                    "provider": next(
                        (
                            r["provider"]
                            for r in profile.get("runners", [])
                            if r.get("enabled") and r["provider"] in runners
                        ),
                        "codex_cli",
                    ),
                    "runners": runners,
                },
                "verification": [
                    dict(row, module=row["repository"])
                    for row in profile.get("verification", {}).get("commands", [])
                ],
                "context_policy": profile.get("context_policy", {"token_mode": "balanced", "ponytail": "off"}),
                "limits": limits(profile.get("execution_limits")),
                "environment_keys": profile.get("environment_keys", []),
                "ports": {"setup": 8768, "studio": 8767, "dashboard": 8766},
            }
        value.setdefault("context_policy", (profile or {}).get("context_policy", {"token_mode": "balanced", "ponytail": "off"}))
        # The roster always reflects the latest profile, including /workflows changes.
        value["roles"] = roles_view(profile or self.initial_profile())
        value["preview_services"] = (profile or {}).get("preview_services", [])
        # Structured Test Automation / Tech Writer scopes; always the latest profile.
        for key in STRUCTURED_PATH_KEYS:
            # A new project starts with usable test/document scopes so the structured roles
            # can finish; saved projects keep exactly what they registered.
            value[key] = list(
                (profile or {}).get(key, []) if registered else STRUCTURED_PATH_DEFAULTS[key]
            )
        value["completion_mode"] = (
            ((profile or {}).get("completion") or {}).get("mode", "local") if registered else "local"
        )
        value["verification_scope"] = (profile or {}).get("verification", {}).get("scope", "all")
        value["usage_policy"] = (profile or {}).get("usage_policy")
        # The screen keeps module-keyed choices; the profile stores them per git repository.
        value.setdefault("skills", (profile or {}).get("skills", {}))
        for provider in PROVIDERS:
            value["ai"]["runners"].setdefault(provider, defaults(provider))
        for row in value["ai"]["runners"].values():
            row.setdefault("effort", "")
            row["routing"] = routing_policy(row.get("routing"))
        environment = read_environment(self.config)
        names = sorted(set(value.get("environment_keys", [])) | set(environment))
        # A project saved for the first time starts on the shared tools when they exist.
        value["ai_tools"] = (profile or {}).get("ai_tools") if registered else shared_default()
        value["wiki_dir"] = (profile or {}).get("wiki_dir", "")
        effective = effective_ai(value)
        value.update(
            preparation_mode=not effective["runners"][effective["provider"]]["enabled"],
            root=str(self.root),
            configuration_root=str(self.config),
            profile_version=profile_version,
            environment=[
                {"key": key, "value": MASK if environment.get(key) else ""} for key in names
            ],
        )
        return value

    def initial_profile(self):
        """Import supported legacy fields for review without rewriting their files."""
        profile = preset_profile(self.preset(), self.root)
        path = self.config / "config/runtime.json"
        if not self.legacy:
            from agent_dev.adapters.workflow_definitions import new_project_profile
            return new_project_profile(profile)
        if not path.is_file():
            return profile
        runtime = json.loads(path.read_text(encoding="utf-8"))
        self.legacy_name = (
            runtime.get("output", {}).get("system_name")
            or runtime.get("system_name")
            or self.root.name
        )
        providers = list(CLI_PROVIDERS)
        selected = runtime.get("runner", "codex_cli")
        fallback = runtime.get("runner_failover", {})
        if selected == "hybrid_cli" or fallback.get("enabled"):
            default_order = (
                ["codex_cli", "claude_cli"]
                if selected not in providers
                else [selected, *[p for p in providers if p != selected]]
            )
            order = [
                p for p in fallback.get("order", default_order) if p in providers
            ] or default_order
        else:
            order = [selected] if selected in providers else ["codex_cli"]
        profile["runners"] = [
            {
                "id": p.removesuffix("_cli"),
                "provider": p,
                "capabilities": ["code.read", "code.write"],
                "enabled": p in order,
                "authenticated": False,
                "config": {
                    k: runtime.get(p, {}).get(
                        k,
                        p.removesuffix("_cli")
                        if k == "command"
                        else None
                        if k == "routing"
                        else "",
                    )
                    for k in ["command", "model", "effort", "routing"]
                },
            }
            for p in [*order, *[p for p in providers if p not in order]]
        ]
        repositories = {
            key: {
                "path": row["path"],
                "allowed_paths": row.get("allowed_paths", ["src", "tests"]),
                "base_ref": row.get("base_ref", "HEAD"),
            }
            for key, row in runtime.get("repositories", {}).items()
            if isinstance(row, dict) and isinstance(row.get("path"), str) and row["path"]
        }
        if repositories:
            profile["repositories"] = repositories
            profile["verification"] = {
                "commands": [
                    {
                        "name": "diff_" + key,
                        "repository": key,
                        "command": ["git", "diff", "--check"],
                        "required": True,
                        "timeout_seconds": 30,
                    }
                    for key in repositories
                ],
                "required_gates": ["diff_" + key for key in repositories],
            }
        return profile

    def prepare(self, payload):
        if not isinstance(payload, dict):
            invalid("설정 객체가 필요합니다.")
        value = copy.deepcopy(payload)
        allowed_keys = {
            "name",
            "project_id",
            "revision",
            "profile_version",
            "modules",
            "ai",
            "verification",
            "limits",
            "context_policy",
            "ports",
            "environment",
            "environment_keys",
            "root",
            "configuration_root",
            "create_directories",
            "roles",
            "preview_services",
            "usage_policy",
            "skills",
            "ai_tools",
            "wiki_dir",
            "preparation_mode",
            "test_paths",
            "document_paths",
            "verification_scope",
            "completion_mode",
        }
        if set(value) - allowed_keys:
            invalid("지원하지 않는 설정 항목이 있습니다.")
        value.pop("preparation_mode", None)  # Derived, never trusted or persisted.
        from agent_dev.domain.context_policy import normalize as context_policy
        value["context_policy"] = context_policy(value.get("context_policy"))
        value["name"] = bounded(value.get("name"), "프로젝트 이름", 120)
        value["project_id"] = bounded(value.get("project_id"), "프로젝트 ID", 100)
        if not re.fullmatch(r"[a-zA-Z0-9가-힣][a-zA-Z0-9가-힣_-]{0,99}", value["project_id"]):
            invalid("프로젝트 ID는 한글·영문·숫자·하이픈·밑줄로 입력해주세요.")
        if self.marker and value["project_id"] != self.marker["project_id"]:
            invalid("기존 프로젝트 ID는 바꿀 수 없습니다. 새 프로젝트 폴더를 선택해주세요.")
        for key in ("revision", "profile_version"):
            if type(value.get(key)) is not int or value[key] < 0:
                invalid("최신 설정을 불러온 뒤 저장해주세요.")
        modules = value.get("modules")
        if not isinstance(modules, list) or not 1 <= len(modules) <= 10:
            invalid("소스·프론트·백 중 하나 이상의 모듈을 지정해주세요.")
        repositories, mapped, seen = {}, {}, set()
        for module in modules:
            if not isinstance(module, dict) or set(module) - {
                "id",
                "path",
                "preset",
                "allowed_paths",
                "base_ref",
            }:
                invalid("모듈 설정 형식을 확인해주세요.")
            key = bounded(module.get("id"), "모듈 이름", 60)
            if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", key) or key in seen:
                invalid("모듈 이름은 중복 없이 영문·숫자·하이픈·밑줄로 입력해주세요.")
            seen.add(key)
            module["path"] = bounded(module.get("path"), "모듈 경로", 1000)
            path = (self.root / module["path"]).expanduser().resolve()
            if path == Path(path.anchor) or (path.exists() and not path.is_dir()):
                invalid("프로젝트 소스 폴더를 지정해주세요.")
            if not path.exists() and (
                not value.get("create_directories") or not path.is_relative_to(self.root)
            ):
                invalid("없는 폴더입니다. 프로젝트 내부 폴더 생성 옵션을 선택해주세요.")
            preset = module.get("preset", "generic")
            if preset not in PRESETS:
                invalid("지원하는 기술 프리셋을 선택해주세요.")
            allowed = module.get("allowed_paths")
            if not isinstance(allowed, list) or not 1 <= len(allowed) <= 100:
                invalid("모듈별 수정 허용 경로를 지정해주세요.")
            module["allowed_paths"] = [relative(p, "수정 허용 경로") for p in allowed]
            module["base_ref"] = bounded(module.get("base_ref", "HEAD"), "Git 기준 ref", 100)
            if module["base_ref"].startswith("-"):
                invalid("Git 기준 ref 형식이 올바르지 않습니다.")
            source = git_root(path) if path.is_dir() else path
            binding_key = next(
                (k for k, r in repositories.items() if r["path"] == str(source)), key
            )
            prefix = path.relative_to(source).as_posix()
            binding = repositories.setdefault(
                binding_key,
                {
                    "path": str(source),
                    "allowed_paths": [],
                    "base_ref": module["base_ref"],
                    "publish_required": True,
                },
            )
            if binding["base_ref"] != module["base_ref"]:
                invalid("같은 Git 저장소의 모듈은 동일한 기준 ref를 사용해야 합니다.")
            binding["allowed_paths"].extend(
                (Path(prefix) / p).as_posix() for p in module["allowed_paths"]
            )
            mapped[key] = (binding_key, prefix)
        ai = value.get("ai", {})
        if (
            not isinstance(ai, dict)
            or set(ai) != {"provider", "runners"}
            or not isinstance(ai.get("runners"), dict)
            or set(ai["runners"]) - set(PROVIDERS)
        ):
            invalid("AI 설정 형식을 확인해주세요.")
        if ai.get("provider") not in PROVIDERS:
            invalid("기본 AI 도구를 선택해주세요.")
        rows = []
        for provider in PROVIDERS:
            config = normalize(provider, ai["runners"].get(provider, defaults(provider)))
            if provider in CLI_PROVIDERS and (
                "/" in config["command"]
                or "\\" in config["command"]
                or config["command"].startswith("~")
            ):
                executable = Path(config["command"]).expanduser()
                config["command"] = str((self.root / executable).resolve())
            ai["runners"][provider] = config
            rows.append(
                {
                    "id": provider.removesuffix("_cli"),
                    "provider": provider,
                    "enabled": config["enabled"],
                    "authenticated": False,
                    "capabilities": ["text.generate"]
                    if provider in API_PROVIDERS
                    else ["code.read", "code.write", "text.generate"],
                    "config": {k: v for k, v in config.items() if k != "enabled"},
                }
            )
        rows.sort(key=lambda row: row["provider"] != ai["provider"])
        commands = value.get("verification", [])
        if not isinstance(commands, list) or not 1 <= len(commands) <= 50:
            invalid("필수 검증 명령을 하나 이상 지정해주세요.")
        gates, names = [], set()
        for command in commands:
            if not isinstance(command, dict) or set(command) - {
                "name",
                "module",
                "repository",
                "command",
                "cwd",
                "timeout_seconds",
                "required",
            }:
                invalid("검증 명령 설정 형식을 확인해주세요.")
            name = bounded(command.get("name"), "검증 이름", 80)
            if name in names or command.get("module") not in mapped:
                invalid("검증 이름의 중복과 모듈 연결을 확인해주세요.")
            names.add(name)
            argv = command.get("command")
            if not isinstance(argv, list) or not 1 <= len(argv) <= 100:
                invalid("검증 명령은 실행파일과 인수의 배열이어야 합니다.")
            argv = [bounded(arg, "검증 명령 인수", 1000, True) for arg in argv]
            if not argv[0]:
                invalid("검증 실행파일이 필요합니다.")
            timeout = command.get("timeout_seconds", 900)
            if (
                type(timeout) is not int
                or not 1 <= timeout <= 7200
                or type(command.get("required", True)) is not bool
            ):
                invalid("검증 제한시간은 1~7200초 정수여야 합니다.")
            cwd = relative(command.get("cwd", "."), "검증 작업 폴더", True)
            binding_key, prefix = mapped[command["module"]]
            gates.append(
                {
                    "name": name,
                    "repository": binding_key,
                    "command": argv,
                    "cwd": (Path(prefix) / cwd).as_posix(),
                    "timeout_seconds": timeout,
                    "required": command.get("required", True),
                }
            )
        if not any(row["required"] for row in gates):
            invalid("하나 이상의 검증 명령을 필수로 지정해주세요.")
        value["limits"] = limits(value.get("limits"))
        ports = value.get("ports", {})
        if (
            not isinstance(ports, dict)
            or set(ports) != {"setup", "studio", "dashboard"}
            or any(type(p) is not int or not 1024 <= p <= 65535 for p in ports.values())
            or len(set(ports.values())) != 3
        ):
            invalid("설정·Studio·현황 포트는 서로 다른 1024~65535 값이어야 합니다.")
        environment = value.pop("environment", [])
        if not isinstance(environment, list) or len(environment) > 100:
            invalid("환경변수는 100개까지 설정할 수 있습니다.")
        previous_env, secrets = read_environment(self.config), {}
        for row in environment:
            if not isinstance(row, dict) or set(row) != {"key", "value"}:
                invalid("환경변수 이름과 값을 입력해주세요.")
            key, secret = row.get("key"), row.get("value")
            validate_key(key)
            if (
                key in secrets
                or not isinstance(secret, str)
                or len(secret) > 8000
                or "\x00" in secret
            ):
                invalid("환경변수 중복 이름과 값 형식을 확인해주세요.")
            if secret == MASK:
                if key not in previous_env:
                    invalid("새 환경변수의 실제 값을 입력해주세요.")
                secret = previous_env[key]
            secrets[key] = secret
        _, _, old_profile = self.current_profile()
        result = copy.deepcopy(old_profile or self.initial_profile())
        if value.get("roles") is not None:
            roles = normalize_roles(value["roles"])
            if roles != roles_view(result):
                # Only an edited roster regenerates the workflow; /workflows choices stay intact.
                from agent_dev.adapters.workflow_definitions import roster_graph

                graph = roster_graph(roles)
                previous = result.get("workflow") or {}
                result["roles"] = roles
                result["workflow"] = {
                    "id": graph["id"],
                    "version": previous.get("version", 0) + 1
                    if previous.get("id") == graph["id"]
                    else 1,
                    "digest": digest(graph),
                    "document": graph,
                }
            value["roles"] = roles
        result.update(
            repositories=repositories,
            allowed_paths=sorted({p for r in repositories.values() for p in r["allowed_paths"]}),
            runners=rows,
            verification={
                "commands": gates,
                "required_gates": [r["name"] for r in gates if r["required"]],
            },
            context_policy=value["context_policy"],
            execution_limits=value["limits"],
            environment_keys=sorted(secrets),
        )
        scope = value.get("verification_scope", "all")
        if scope not in VERIFICATION_SCOPES:
            invalid("검증 범위를 확인해주세요.")
        value["verification_scope"] = scope
        if scope != "all":
            result["verification"]["scope"] = scope
        mode = value.get("completion_mode", "local")
        if mode not in COMPLETION_MODES:
            invalid("작업 완료 방식을 확인해주세요.")
        value["completion_mode"] = mode
        result["completion"] = {"mode": mode}
        for key, label in STRUCTURED_PATH_KEYS.items():
            if key in value:
                paths = value[key]
                if not isinstance(paths, list) or len(paths) > 50:
                    invalid(label + "는 경로 목록이어야 합니다.")
                value[key] = list(dict.fromkeys(relative(p, label) for p in paths))
                if value[key]:
                    result[key] = value[key]
                else:
                    result.pop(key, None)
        if value.get("preview_services") is not None:
            from agent_dev.adapters.preview_host import normalize_services
            from agent_dev.adapters.secrets import is_secret_key

            result["preview_services"] = normalize_services(
                value["preview_services"],
                repositories,
                tuple(s for k, s in secrets.items() if is_secret_key(k) and len(s) >= 6),
            )
            value["preview_services"] = result["preview_services"]
        if "usage_policy" in value:
            from agent_dev.domain.usage_policy import normalize_policy

            policy = normalize_policy(value["usage_policy"])
            if policy is None:
                result.pop("usage_policy", None)
            else:
                result["usage_policy"] = policy
            value["usage_policy"] = policy
        if value.get("wiki_dir"):
            from agent_dev.adapters.wiki_knowledge import wiki_dir

            value["wiki_dir"] = result["wiki_dir"] = wiki_dir(value["wiki_dir"]).as_posix()
        else:
            value["wiki_dir"] = ""
            result.pop("wiki_dir", None)
        if value.get("ai_tools") is not None:
            from agent_dev.adapters.global_ai import validate_selection

            value["ai_tools"] = result["ai_tools"] = validate_selection(value["ai_tools"])
        else:
            result.pop("ai_tools", None)
        if value.get("skills") is not None:
            from agent_dev.adapters.skill_store import catalog
            from agent_dev.domain.skill_selection import validate as validate_skills

            data = catalog()
            chosen = validate_skills(data, value["skills"], modules=set(mapped))
            per_repository: dict = {}
            for module, entry in chosen.get("repositories", {}).items():
                bundles = per_repository.setdefault(mapped[module][0], {"bundles": []})["bundles"]
                bundles += [b for b in entry["bundles"] if b not in bundles]
            skills = validate_skills(
                data, dict(chosen, repositories=per_repository), modules=set(repositories)
            )
            if skills.get("bundles") or skills.get("repositories") or skills.get("roles"):
                result["skills"] = skills
            else:
                result.pop("skills", None)
            value["skills"] = chosen
        for key in ("root", "configuration_root", "create_directories"):
            value.pop(key, None)
        value["environment_keys"] = sorted(secrets)
        return value, result, secrets

    def save(self, payload, *, check_auth=False, provenance=None):
        from agent_dev.adapters.global_settings import GlobalSettings
        from agent_dev.domain.settings_resolution import check_limits

        value, profile, secrets = self.prepare(payload)
        check_limits(value["limits"], GlobalSettings().read()["values"])
        _, _, previous = self.current_profile()
        if check_auth:
            from agent_dev.adapters.global_ai import selected_tools

            # Shared tools replace the project's own runners, so those never run: probing their
            # logins would launch CLIs for nothing. Shared logins are checked on the AI page.
            check_auth = selected_tools({"ai_tools": value.get("ai_tools")}) is None
        for runner in profile["runners"]:
            provider, config = runner["provider"], runner["config"]
            if provider in API_PROVIDERS:
                # Readiness only; never claim a live API authentication probe.
                runner["authenticated"] = credential_ready(
                    provider, config, dict(os.environ, **secrets)
                )
            elif provider == "gemini_cli" and runner["enabled"]:
                from agent_dev.adapters.ai_providers import gemini_auth_ready

                runner["authenticated"] = gemini_auth_ready(dict(os.environ, **secrets))
            elif check_auth and runner["enabled"]:
                runner["authenticated"] = auth_probe(provider, config["command"])
                runner["checked_at"] = __import__("time").time()
            else:
                old = next(
                    (r for r in (previous or {}).get("runners", []) if r["provider"] == provider),
                    {},
                )
                if (
                    old.get("config", {}).get("command", provider.removesuffix("_cli"))
                    == config["command"]
                ):
                    runner["authenticated"] = old.get("authenticated") is True
        # Values never enter the profile, event or public settings row.
        public = canonical({"settings": value, "profile": profile})
        if any(secret and len(secret) >= 6 and secret in public for secret in secrets.values()):
            invalid(
                "환경변수 값은 모델·경로·검증 명령에 넣지 말고 환경변수 입력란에만 저장해주세요."
            )
        if self.managed.is_symlink() or self.db_path.is_symlink():
            invalid("관리 폴더와 DB에 심볼릭 링크를 사용할 수 없습니다.")
        self.managed.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.managed.chmod(0o700)
        with locked(self.managed / "setup"):
            db = Database(self.db_path)
            store = WorkflowRepository(db)
            env_path = environment_file(self.config)
            private_directory(env_path.parent)
            old_env = read_environment(self.config)
            try:
                with db.transaction() as conn:
                    current = conn.execute(
                        "SELECT revision FROM workspace_settings WHERE id=1"
                    ).fetchone()
                    if (current[0] if current else 0) != value["revision"]:
                        raise DomainError(
                            ErrorCode.STALE_VERSION,
                            "다른 화면에서 설정이 바뀌었습니다. 다시 불러온 뒤 저장해주세요.",
                        )
                    rows = conn.execute(
                        "SELECT version FROM project_profile_versions WHERE project_id=? ORDER BY version DESC LIMIT 1",
                        (value["project_id"],),
                    ).fetchone()
                    if (rows[0] if rows else 0) != value["profile_version"]:
                        raise DomainError(
                            ErrorCode.STALE_VERSION,
                            "프로젝트 프로필이 바뀌었습니다. 최신 설정을 불러와주세요.",
                        )
                    version = value["profile_version"] + 1
                    if not rows:
                        conn.execute(
                            "INSERT INTO projects VALUES(?,?,?,'draft',0,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                            (value["project_id"], value["name"], value["name"], store.clock()),
                        )
                    else:
                        conn.execute(
                            "UPDATE projects SET name=?,version=version+1 WHERE id=?",
                            (value["name"], value["project_id"]),
                        )
                    conn.execute(
                        "INSERT INTO project_profile_versions VALUES(?,?,?,?)",
                        (value["project_id"], version, digest(profile), canonical(profile)),
                    )
                    if provenance is not None:
                        # Same transaction: a profile never exists without its recorded origin.
                        conn.execute(
                            "INSERT INTO configuration_provenance VALUES(?,?,?,?,?,?)",
                            (
                                value["project_id"],
                                version,
                                provenance["global_revision"],
                                provenance["global_digest"],
                                canonical(provenance["overrides"]),
                                canonical(provenance["origins"]),
                            ),
                        )
                    value.update(revision=value["revision"] + 1, profile_version=version)
                    conn.execute(
                        "INSERT INTO workspace_settings VALUES(1,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,settings_json=excluded.settings_json",
                        (value["revision"], canonical(value)),
                    )
                    store._event(
                        conn,
                        value["project_id"],
                        "ProfileRevised" if rows else "ProjectRegistered",
                        {
                            "profile_version": version,
                            "setup_revision": value["revision"],
                            "environment_keys": sorted(secrets),
                            **(
                                {"global_revision": provenance["global_revision"]}
                                if provenance is not None
                                else {}
                            ),
                        },
                    )
                    atomic_json(env_path, secrets)
            except BaseException:
                atomic_json(env_path, old_env)
                raise
            warnings = []
            try:
                (self.managed / ".gitignore").write_text("*\n", encoding="utf-8")
                if not self.legacy:
                    marker = dict(
                        self.marker or {},
                        format_version=1,
                        project_id=value["project_id"],
                        name=value["name"],
                        engine_version="0.3.0",
                        preset=self.preset(),
                    )
                    if self.locations.layout == EXTERNAL and self.locations.workspace_id:
                        # An external folder names its source so it resolves without app.json.
                        marker.update(
                            workspace_id=self.locations.workspace_id,
                            source_root=str(self.locations.source_root),
                            runtime_root=str(self.locations.runtime_root),
                        )
                    atomic_json(self.managed / "workspace.json", marker)
                    self.config = self.managed
                    self.marker = metadata(self.config)
                    self.locations = workspace_locations(self.config)
                cache = self.config / "config"
                runtime_path = cache / "runtime.json"
                runtime = (
                    json.loads(runtime_path.read_text(encoding="utf-8"))
                    if runtime_path.exists()
                    else {
                        "system_name": value["name"],
                        # A new project has no legacy data to migrate: the approval-based
                        # engine runs and publishes from the start (ADR 0003).
                        "orchestrator_v2": {
                            "read": True,
                            "dual_write": True,
                            "execute": True,
                            "publish": True,
                            "legacy_readonly": False,
                        },
                    }
                )
                if self.legacy:
                    runtime["runner"] = next(
                        (
                            r["provider"]
                            for r in profile["runners"]
                            if r["enabled"] and r["provider"] in CLI_PROVIDERS
                        ),
                        value["ai"]["provider"],
                    )
                    for runner in profile["runners"]:
                        runtime[runner["provider"]] = dict(
                            runtime.get(runner["provider"], {}),
                            **runner["config"],
                            workspace_path=str(self.root),
                            add_dirs=[
                                str((self.root / module["path"]).resolve())
                                for module in value["modules"]
                                if not (self.root / module["path"])
                                .resolve()
                                .is_relative_to(self.root)
                            ],
                        )
                    enabled = [
                        r["provider"]
                        for r in profile["runners"]
                        if r["enabled"] and r["provider"] in CLI_PROVIDERS
                    ]
                    runtime["runner_failover"] = dict(
                        runtime.get("runner_failover", {}), enabled=len(enabled) > 1, order=enabled
                    )
                    runtime["setup_configuration_root"] = str(self.config)
                    runtime["environment_keys"] = profile["environment_keys"]
                    repos = runtime.setdefault("repositories", {})
                    for module in value["modules"]:
                        repos[module["id"]] = dict(
                            repos.get(module["id"], {}),
                            path=str((self.root / module["path"]).resolve()),
                        )
                runtime["system_name"] = value["name"]
                if isinstance(runtime.get("output"), dict):
                    runtime["output"]["system_name"] = value["name"]
                atomic_json(runtime_path, runtime)
                atomic_json(cache / "profile.json", profile)
                if payload.get("create_directories"):
                    for module in value["modules"]:
                        (self.root / module["path"]).mkdir(parents=True, exist_ok=True)
            except OSError:
                warnings.append(
                    "DB의 설정은 저장됐지만 보조 파일·폴더를 만들지 못했습니다. 권한을 확인하고 다시 저장해주세요."
                )
        return {"settings": self.read(), "warnings": warnings, "existing_snapshots_unchanged": True}

    def provenance(self, project_id, profile_version):
        """The shared revision and overrides that produced one profile version, if any."""
        if not profile_version or not self.db_path.is_file():
            return None
        db = Database(self.db_path, read_only=True)
        if not db.rows("SELECT name FROM sqlite_master WHERE name='configuration_provenance'"):
            return None
        rows = db.rows(
            "SELECT * FROM configuration_provenance WHERE project_id=? AND profile_version=?",
            (project_id, profile_version),
        )
        if not rows:
            return None
        return {
            "global_revision": rows[0]["global_revision"],
            "global_digest": rows[0]["global_digest"],
            "overrides": json.loads(rows[0]["override_json"]),
            "origins": json.loads(rows[0]["origins_json"]),
        }

    def effective(self):
        """Read-only: the values the latest shared revision and current overrides would give.

        ``applied`` describes the current profile; ``pending_global`` means a newer shared
        revision exists that the project has not applied. Nothing is written.
        """
        from agent_dev.adapters.global_settings import GlobalSettings
        from agent_dev.domain.settings_resolution import resolve_settings

        shared = GlobalSettings().read()
        project_id, profile_version, _ = self.current_profile()
        applied = self.provenance(project_id, profile_version)
        overrides = applied["overrides"] if applied else {}
        resolved = resolve_settings(shared["values"], overrides)
        return {
            "values": resolved["values"],
            "origins": resolved["origins"],
            "overrides": overrides,
            "global_revision": shared["revision"],
            "global_digest": shared["digest"],
            "project_revision": self.read()["revision"],
            "profile_version": profile_version,
            "applied": None
            if applied is None
            else {k: applied[k] for k in ("global_revision", "global_digest", "origins")},
            "pending_global": shared["revision"] > 0
            and (applied is None or applied["global_revision"] != shared["revision"]),
            "digest": digest(resolved),
        }

    def apply_global(self, global_revision, expected_revision, *, overrides=None, templates=False):
        """Write a new profile revision from a chosen shared revision plus project overrides.

        Only fields set by the shared revision or the project change; fields left at the
        built-in default keep the project's current value. Existing snapshots are untouched.
        """
        from agent_dev.adapters.global_settings import GlobalSettings
        from agent_dev.domain.settings_resolution import resolve_settings, validate_overrides

        if type(global_revision) is not int or global_revision < 1:
            invalid("반영할 공통 설정 revision을 선택해주세요.")
        shared = GlobalSettings().read(global_revision)
        settings = self.read()
        if type(expected_revision) is not int or settings["revision"] != expected_revision:
            raise DomainError(
                ErrorCode.STALE_VERSION,
                "다른 화면에서 설정이 바뀌었습니다. 다시 불러온 뒤 반영해주세요.",
            )
        if overrides is None:
            current = self.provenance(settings["project_id"], settings["profile_version"])
            overrides = current["overrides"] if current else {}
        overrides = validate_overrides(overrides)
        resolved = resolve_settings(shared["values"], overrides)
        origins, ai = resolved["origins"], resolved["values"]["ai"]
        value = copy.deepcopy(settings)
        if origins["ai.provider"] != "default":
            if ai["provider"] not in PROVIDERS:
                invalid("지원하는 기본 AI 도구를 선택해주세요.")
            value["ai"]["provider"] = ai["provider"]
            value["ai"]["runners"][ai["provider"]]["enabled"] = True
        runner = value["ai"]["runners"][value["ai"]["provider"]]
        for key in ("model", "effort"):
            if origins["ai." + key] != "default":
                runner[key] = ai[key]
        value["limits"] = dict(
            value["limits"],
            **{
                key: item
                for key, item in resolved["values"]["limits"].items()
                if origins["limits." + key] != "default"
            },
        )
        for key, cap in shared["values"].get("limit_caps", {}).items():
            # An existing project value the project did not override is bounded by the cap.
            value["limits"][key] = min(value["limits"][key], cap)
        template = shared["values"].get("templates", {}).get("roles")
        if templates and template is not None:
            value["roles"] = template
        result = self.save(
            value,
            provenance={
                "global_revision": shared["revision"],
                "global_digest": shared["digest"],
                "overrides": overrides,
                "origins": origins,
            },
        )
        return dict(
            result,
            profile_version=result["settings"]["profile_version"],
            global_revision=shared["revision"],
        )

    def preview(self, settings, task, phase_id="developer"):
        _, profile, _ = self.prepare(settings)
        if not isinstance(task, dict) or set(task) - {
            "title",
            "scope",
            "workstreams",
            "acceptance_criteria",
        }:
            invalid("미리보기 작업 형식을 확인해주세요.")
        task = copy.deepcopy(task)
        task["title"] = bounded(task.get("title", ""), "작업 제목", 1000, True)
        for key in ("scope", "workstreams", "acceptance_criteria"):
            rows = task.setdefault(key, [])
            if not isinstance(rows, list) or len(rows) > 100:
                invalid("미리보기 작업 범위를 줄여주세요.")
            task[key] = [bounded(row, "작업 범위", 2000, True) for row in rows]
        phase_id = bounded(phase_id, "작업 단계", 100)
        return {
            "selections": [
                dict(select_model(row["config"], task, phase_id), provider=row["provider"])
                for row in profile["runners"]
                if row["enabled"]
            ]
        }

    def preset_gates(self, modules):
        """Default verification commands for the on-screen modules ([{id, preset, path}])."""
        from agent_dev.adapters.presets import available_commands

        if not isinstance(modules, list) or len(modules) > 50 or not all(
            isinstance(m, dict) and all(isinstance(m.get(k), str) for k in ("id", "preset", "path"))
            for m in modules
        ):
            invalid("모듈 목록을 확인해주세요.")
        gates, skipped = [], []
        for module in modules:
            if module["preset"] not in PRESETS:
                invalid("지원하지 않는 기술 프리셋입니다.")
            path = self.root / relative(module["path"], "폴더 경로", dot=True)
            kept, left_out = available_commands(module["preset"], path)
            gates += [
                {"name": f"{module['id']}_{name}", "module": module["id"], "command": command}
                for name, command in kept
            ]
            skipped += [
                {"name": f"{module['id']}_{name}", "reason": reason} for name, reason in left_out
            ]
        return {"gates": gates, "skipped": skipped}

    def check(self, *, check_auth=False):
        from agent_dev.adapters.diagnostics import codex_model_check, diagnose
        from agent_dev.adapters.global_ai import selected_tools

        result = diagnose(self.config, check_auth=False)
        settings = self.read()
        # Check the runners the project would use now; shared tools replace its own runners.
        ai = effective_ai(settings)
        shared = selected_tools({"ai_tools": settings.get("ai_tools")}) is not None
        disabled = {p.removesuffix("_cli") for p, r in ai["runners"].items() if not r["enabled"]}

        def kept(row):
            prefix, _, runner = str(row.get("check", "")).partition(":")
            if prefix == "api":
                return False  # Re-checked below for enabled API runners.
            if prefix in {"runner", "runner_model", "runner_policy", "auth"}:
                # Shared tools: the registered runners are not what runs; login lives in /ai.
                return not (shared or runner in disabled or (check_auth and prefix == "auth"))
            return True

        checks = [row for row in result["checks"] if kept(row)]
        if not any(ai["runners"][p]["enabled"] for p in CLI_PROVIDERS):
            checks.append(
                {
                    "name": "development_cli",
                    "status": "warn",
                    "message": "API 생성만 준비되어 있습니다. 파일 수정 단계에는 개발 CLI가 필요합니다.",
                    "fix": "실제 개발을 시작하려면 Codex·Claude·Gemini CLI 중 하나를 활성화하고 로그인해주세요.",
                }
            )
        for provider, row in ai["runners"].items():
            if row["enabled"]:
                if provider in API_PROVIDERS:
                    ready = credential_ready(
                        provider, row, dict(os.environ, **read_environment(self.config))
                    )
                    checks.append(
                        {
                            "name": "api:" + provider,
                            "status": "warn" if ready else "fail",
                            "message": "API 주소·인증 키 준비됨; 실제 인증·모델 접근은 생성 시 확인합니다."
                            if ready
                            else "API 인증 환경변수 값이 필요합니다.",
                            "fix": "설정의 환경변수에 "
                            + (row["credential_ref"] or "로컬 서버 설정")
                            + "를 등록해주세요.",
                        }
                    )
                    continue
                found = shutil.which(row["command"])
                checks.append(
                    {
                        "name": provider + "_configured_binary",
                        "label": PROVIDERS[provider]["label"] + " 실행파일",
                        "status": "pass" if found else "fail",
                        "message": "지정한 CLI 실행파일을 찾았습니다."
                        if found
                        else "지정한 CLI 실행파일을 찾을 수 없습니다.",
                        "fix": "CLI 설치 후 실행파일 또는 절대 경로를 설정해주세요.",
                    }
                )
                if shared and provider == "codex_cli":
                    state, message = codex_model_check(row)
                    checks.append(
                        {"name": "runner_model:codex", "status": state, "message": message}
                    )
                if check_auth:
                    if provider == "gemini_cli":
                        from agent_dev.adapters.ai_providers import gemini_auth_ready

                        ok = gemini_auth_ready(dict(os.environ, **read_environment(self.config)))
                    else:
                        ok = auth_probe(provider, row["command"])
                    checks.append(
                        {
                            "name": provider + "_login",
                            "status": "pass" if ok else "fail",
                            "message": "Gemini 로컬 인증 준비됨; 실제 접근은 실행 시 확인합니다."
                            if provider == "gemini_cli" and ok
                            else "CLI 로그인 확인 완료"
                            if ok
                            else "로그인 상태를 확인하지 못했습니다.",
                            "fix": "gemini를 실행해 로그인하거나 GEMINI_API_KEY를 설정해주세요."
                            if provider == "gemini_cli"
                            # Runs use the app-private CLI home, not the terminal login.
                            else ("AI 도구 메뉴" if shared else "AI 도구 단계")
                            + "에서 ‘DevRoster 전용 로그인’을 진행해주세요.",
                            "label": PROVIDERS[provider]["label"] + " 로그인",
                        }
                    )
        for module in settings["modules"]:
            for binary in {
                "react": ["node", "npm"],
                "python": ["python"],
                "spring-boot": ["java"],
                "spring-boot-gradle": ["java"],
                "generic": [],
            }[module["preset"]]:
                checks.append(
                    {
                        "name": module["id"] + "_" + binary,
                        "label": f"{module['id']} 모듈 · {binary}",
                        "status": "pass" if shutil.which(binary) else "fail",
                        "message": binary + " 실행파일 확인",
                        "fix": binary + " 설치 및 PATH를 확인해주세요.",
                    }
                )
        for name, port in settings["ports"].items():
            try:
                with socket.socket() as probe:
                    probe.bind(("127.0.0.1", port))
                status = "pass"
            except OSError:
                status = "warn"
            checks.append(
                {
                    "name": "port:" + name,
                    "label": "웹 서버 포트 · "
                    + ({"setup": "설정 화면", "studio": "기획·화면 검토", "dashboard": "개발 현황"}.get(name) or str(name)),
                    "status": status,
                    "message": f"127.0.0.1:{port}",
                    "fix": "해당 서버가 실행 중이면 정상입니다. 다른 앱이 사용 중이면 웹 포트를 바꾸고 새로 실행해주세요.",
                }
            )
        present = read_environment(self.config)
        for key in settings.get("environment_keys", []):
            checks.append(
                {
                    "name": "environment:" + key,
                    "status": "pass" if present.get(key) else "warn" if key in present else "fail",
                    "message": "값은 표시하지 않습니다.",
                    "fix": "이 환경변수의 값을 입력하고 설정을 저장해주세요.",
                }
            )
        for row in checks:
            name = row.get("name", row.get("check", "environment"))
            row["name"] = name
            prefix = name.split(":")[0]
            labels = {
                "python": "Python 버전",
                "git": "Git 설치",
                "workspace": "프로젝트 등록",
                "workspace_version": "설정 버전",
                "configuration": "설정 파일",
                "database": "저장소 무결성",
                "database_integrity": "저장소 무결성",
                "database_schema": "저장소 버전",
                "active_leases": "작업 실행 잠금",
                "recovery": "중단된 작업",
                "ignore": "Git 제외 설정",
                "test_coverage": "테스트 명령",
                "schema": "저장소 버전",
                "leases": "작업 실행 잠금",
                "attempts": "중단된 작업",
                "repository": "소스 저장소·초기 commit",
                "repository_root": "Git 저장소와 모듈 경로",
                "branch": "소스 브랜치",
                "workspace_gitignore": "Git 제외 설정",
                "verification": "필수 검증 명령",
                "verification_strength": "검증 범위",
                "runner": "AI 도구 설치",
                "runner_model": "선택 모델",
                "runner_auth": "CLI 로그인 확인",
                "runner_policy": "로그인 등록",
                "disk": "저장 공간",
                "port": "웹 서버 포트",
                "environment": "환경변수 값",
            }
            suffix = name.partition(":")[2]
            row["label"] = row.get("label") or (
                labels[prefix] + (" · " + suffix if suffix else "") if prefix in labels else name
            )
            row["summary"] = (
                row["label"]
                + {
                    "pass": " 확인 완료",
                    "warn": " 추가 확인이 필요합니다.",
                    "fail": " 준비가 필요합니다.",
                }[row["status"]]
            )
            fixes = {
                "repository": "소스 저장소를 복제하거나 Git 초기 commit을 만들고 설정을 다시 저장해주세요.",
                "repository_root": "Git 구조가 변경되었습니다. 설정을 다시 저장해 모듈 경로를 맞춰주세요.",
                "workspace_gitignore": "소스 저장소 .gitignore에 /.agent-dev/와 /output/을 추가해주세요.",
                "verification_strength": "테스트·빌드 명령을 추가해주세요. 기본 diff 검사는 공백 오류만 확인합니다.",
                "runner_model": "CLI에서 사용할 수 있는 모델인지 확인해주세요. 로컬 목록은 실제 계정 권한을 보장하지 않습니다.",
                "runner_auth": "CLI에서 로그인한 뒤 ‘로그인 상태도 확인’을 선택해 다시 저장해주세요.",
                "runner_policy": "로그인 상태를 확인하며 설정을 다시 저장해주세요.",
                "branch": "현재 브랜치를 확인해주세요. 실제 개발은 작업 전용 브랜치에서 진행합니다.",
            }
            row["action"] = fixes.get(prefix, row.get("fix", ""))
        result["checks"] = checks
        result["ready"] = not any(c["status"] == "fail" for c in checks)
        result["healthy"] = result["ready"]
        result["failed"] = sum(c["status"] == "fail" for c in checks)
        result["warnings"] = sum(c["status"] == "warn" for c in checks)
        result["authentication_probed"] = check_auth
        return result


def auth_probe(provider, command):
    if provider == "gemini_cli":
        # No non-generating login-status command; do not infer auth from --version.
        from agent_dev.adapters.ai_providers import gemini_auth_ready

        return gemini_auth_ready(os.environ)
    args = (
        [command, "login", "status"]
        if provider == "codex_cli"
        else [command, "auth", "status", "--json"]
    )
    from agent_dev.core.cli_home import isolated_environment

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=10,
            check=False,
            env=isolated_environment(),
        )
        if provider == "claude_cli":
            return result.returncode == 0 and json.loads(result.stdout).get("loggedIn") is True
        return result.returncode == 0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def shared_default():
    """A new project starts on the shared AI tools when they exist: their enabled tools in order."""
    from agent_dev.adapters.global_ai import GlobalAi

    try:
        shared = GlobalAi().read()
    except DomainError:
        return None
    if not shared["revision"]:
        return None
    tools = shared["values"]["tools"]
    return {"selected": [p for p in shared["values"]["order"] if tools[p]["enabled"]]}


def ai_defaults(root):
    return effective_ai(ProjectSetup(root).read())


def ai_tool_notes(root):
    """Short per-tool notes for AI pickers: not used by this project, or login needed."""
    from agent_dev.adapters.global_ai import GlobalAi, selected_tools

    value = ProjectSetup(root).read()
    shared = selected_tools({"ai_tools": value.get("ai_tools")}) is not None
    try:
        auth = GlobalAi().auth() if shared else {}
    except DomainError:
        auth = {}
    notes = {}
    for provider, row in effective_ai(value)["runners"].items():
        if not row["enabled"]:
            notes[provider] = "이 프로젝트에서 사용 안 함" if shared else "설정 필요"
        elif provider in CLI_PROVIDERS and auth.get(provider, {}).get("authenticated") is False:
            notes[provider] = "로그인 필요"
    return {"shared": shared, "notes": notes}


def effective_ai(value):
    """The project's AI as it would run now: its own runners, or the chosen shared tools.

    A project that chose shared tools but has no shared AI yet is in preparation mode.
    """
    from agent_dev.adapters.global_ai import GlobalAi, selected_tools

    selected = selected_tools({"ai_tools": value.get("ai_tools")})
    if selected is None:
        return value["ai"]
    try:
        shared = GlobalAi().read()
    except DomainError:
        shared = {"revision": 0, "values": {}}  # Unreadable shared AI: treat as not set up.
    tools = shared["values"].get("tools", {}) if shared["revision"] else {}
    runners = {
        provider: dict(
            tools.get(provider, value["ai"]["runners"][provider]),
            enabled=bool(tools) and provider in selected and tools[provider]["enabled"],
        )
        for provider in PROVIDERS
    }
    usable = [provider for provider in selected if runners[provider]["enabled"]]
    provider = (usable or selected or [value["ai"]["provider"]])[0]
    return {"provider": provider, "runners": runners}
