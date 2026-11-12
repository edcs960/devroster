"""Project-scoped MCP application service with explicit read operations and evidence."""

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import unquote, urlencode, urlsplit

from agent_dev.adapters.environment import read_environment, validate_key
from agent_dev.adapters.integration_catalog import catalog, provider
from agent_dev.adapters.integration_store import IntegrationStore
from agent_dev.adapters.mcp_client import McpClient, remote_address
from agent_dev.domain.content import canonical, digest, redact
from agent_dev.domain.errors import DomainError, ErrorCode

IDENTIFIER = re.compile(r"[a-zA-Z][a-zA-Z0-9_-]{0,79}")
FIELDS = {
    "id",
    "provider",
    "name",
    "transport",
    "endpoint",
    "command",
    "credential_ref",
    "scopes",
    "enabled",
    "local",
    "operations",
    "bound_arguments",
    "environment_keys",
    "supported_client",
    "token_mode",
    "oauth_client_id",
}
RESOURCE_KEYS = {
    "parent",
    "file_key",
    "fileKey",
    "fileId",
    "project_id",
    "projectId",
    "project_ref",
    "project_path",
    "relative_path",
    "path",
    "repo",
    "repository",
    "owner",
    "url",
    "name",
    "cloudId",
    "organization_slug",
}


def invalid(message, code=ErrorCode.INVALID_INPUT) -> NoReturn:
    raise DomainError(code, message)


def validate_arguments(schema, value, depth=0):
    """Validate the safe JSON-schema subset and refuse unsupported constraints."""
    if depth > 12 or not isinstance(schema, dict):
        invalid("MCP 입력 스키마를 지원하지 않습니다.")
    if "$ref" in schema:
        invalid("참조형 입력 스키마는 도구 권한 매핑 전에 정규화해주세요.")
    alternatives = schema.get("anyOf") or schema.get("oneOf")
    if alternatives:
        matches = 0
        for option in alternatives:
            try:
                validate_arguments(option, value, depth + 1)
                matches += 1
            except DomainError:
                pass
        if not matches or "oneOf" in schema and matches != 1:
            invalid("MCP 입력값이 도구 스키마와 일치하지 않습니다.")
        return
    kind = schema.get("type")
    if isinstance(kind, list):
        for item in kind:
            try:
                validate_arguments(dict(schema, type=item), value, depth + 1)
                return
            except DomainError:
                pass
        invalid("MCP 입력 타입을 확인해주세요.")
    good = {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "null": value is None,
    }
    if kind and not good.get(kind, False) or "enum" in schema and value not in schema["enum"]:
        invalid("MCP 입력 타입/선택값을 확인해주세요.")
    if "const" in schema and value != schema["const"]:
        invalid("MCP 고정 입력값이 다릅니다.")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            invalid("MCP 도구의 필수 입력값을 확인해주세요.")
        for key, item in value.items():
            if key in properties:
                validate_arguments(properties[key], item, depth + 1)
            elif schema.get("additionalProperties") is False:
                invalid("MCP 도구에 알 수 없는 입력 필드입니다.")
    elif isinstance(value, list):
        if len(value) > min(schema.get("maxItems", 1000), 1000) or len(value) < schema.get(
            "minItems", 0
        ):
            invalid("MCP 배열 입력 개수를 확인해주세요.")
        for item in value:
            validate_arguments(schema.get("items", {}), item, depth + 1)
    elif isinstance(value, str):
        if len(value) > min(schema.get("maxLength", 8000), 8000) or len(value) < schema.get(
            "minLength", 0
        ):
            invalid("MCP 문자열 길이를 확인해주세요.")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            invalid("MCP 문자열 형식을 확인해주세요.")
    elif (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and (
            value < schema.get("minimum", float("-inf"))
            or value > schema.get("maximum", float("inf"))
        )
    ):
        invalid("MCP 숫자 범위를 확인해주세요.")


class Integrations:
    def __init__(self, root, *, client_factory=McpClient):
        self.store, self.client_factory = IntegrationStore(root), client_factory
        self.root, self.project = self.store.root, self.store.project
        from agent_dev.adapters.integration_oauth import IntegrationOAuth

        self.oauth = IntegrationOAuth(self.store.config)

    def catalog(self):
        return catalog()

    def list(self):
        return self.store.list()

    def show(self, key):
        return self.store.get(key)

    def configure(self, config, expected_version=0, *, _persist=True):
        row = self.connection_row(config, self.project)
        return self.store.save(row, expected_version) if _persist else row

    @staticmethod
    def connection_row(config, project):
        """A validated, credential-free connection row (project or shared definition)."""
        if not isinstance(config, dict) or set(config) - FIELDS:
            invalid("연결 설정에는 등록된 필드와 credential 참조만 입력해주세요.")
        key, provider_id = config.get("id", ""), config.get("provider", "")
        if not isinstance(key, str) or not IDENTIFIER.fullmatch(key):
            invalid("연결 ID는 영문으로 시작하는 이름이 필요합니다.")
        base = provider(provider_id)
        row: dict[str, Any] = dict(
            config,
            id=key,
            provider=provider_id,
            project_id=project,
            name=config.get("name", base["name"]),
            transport=config.get("transport", base["transport"]),
            endpoint=config.get("endpoint", base["endpoint"]),
            command=config.get("command", base["command"]),
            scopes=config.get("scopes", []),
            credential_ref=config.get("credential_ref", ""),
            enabled=config.get("enabled", True),
            local=config.get("local", False),
            bound_arguments=config.get("bound_arguments", {}),
            environment_keys=config.get("environment_keys", []),
            token_mode=config.get("token_mode", "balanced"),
            state="configured",
            checked_at=None,
            schema_digest="",
            tool_schemas={},
        )
        if (
            not isinstance(row["enabled"], bool)
            or not isinstance(row["local"], bool)
            or row["token_mode"] not in {"off", "balanced", "aggressive"}
        ):
            invalid("활성화/토큰 절약 설정을 확인해주세요.")
        if (
            not isinstance(row["name"], str)
            or len(row["name"]) > 120
            or not isinstance(row["bound_arguments"], dict)
        ):
            invalid("연결 이름/고정 입력값을 확인해주세요.")
        for label in ("scopes", "command", "environment_keys"):
            if (
                not isinstance(row[label], list)
                or len(row[label]) > 100
                or any(
                    not isinstance(v, str) or not v or len(v) > 1000 or "\x00" in v
                    for v in row[label]
                )
            ):
                invalid("MCP 범위/실행 인수/환경변수 이름을 확인해주세요.")
        for name in [
            *row["environment_keys"],
            *([row["credential_ref"]] if row["credential_ref"] else []),
        ]:
            validate_key(name)
        if row["transport"] in {"http", "api"}:
            if row["transport"] == "api" and provider_id != "figma":
                invalid("이 제공자의 REST 대체 연결을 지원하지 않습니다.")
            if row["transport"] == "http":
                parsed = urlsplit(row["endpoint"])
                if (
                    not row["local"]
                    and base["endpoint"]
                    and parsed.hostname != urlsplit(base["endpoint"]).hostname
                ):
                    invalid("제공자의 공식 MCP 주소를 사용해주세요.")
                remote_address(row["endpoint"], local=row["local"])
        elif row["transport"] == "stdio":
            if not row["command"] and row["enabled"]:
                invalid("설치된 MCP 실행 파일과 인수가 필요합니다.")
            if row["command"] and Path(row["command"][0]).name in {"npx", "uvx", "pip", "npm", "curl", "sh", "bash"}:
                invalid("실행 중 설치/쉘 실행 대신 설치된 MCP 실행 파일을 등록해주세요.")
        else:
            invalid("지원되는 MCP 연결 방식이 아닙니다.")
        if provider_id == "figma" and row["transport"] == "api":
            base["operations"] = {
                name: {"tool": name, "capability": "design.read"}
                for name in ("get_file", "get_nodes", "get_images")
            }
        operations = config.get("operations", base["operations"])
        if not isinstance(operations, dict) or len(operations) > 100:
            invalid("명시적인 도구 권한 매핑이 필요합니다.")
        for name, spec in operations.items():
            if (
                not isinstance(name, str)
                or len(name) > 100
                or not isinstance(spec, dict)
                or set(spec) - {"tool", "capability", "fixed_arguments"}
            ):
                invalid("도구 이름/권한 매핑을 확인해주세요.")
            if provider_id != "custom" and name not in base["operations"]:
                invalid("이 제공자의 검증된 읽기 도구만 등록할 수 있습니다.")
            if (
                not isinstance(spec.get("tool"), str)
                or not isinstance(spec.get("capability"), str)
                or not spec["capability"].endswith(".read")
            ):
                invalid("읽기 기능과 실제 도구 이름을 명시해주세요.")
            if provider_id != "custom" and (
                spec["tool"] != base["operations"][name]["tool"]
                or spec["capability"] != base["operations"][name]["capability"]
            ):
                invalid("검증된 도구 이름을 변경할 수 없습니다.")
        row["operations"] = operations
        if len(canonical(row).encode()) > 100_000:
            invalid("연결 설정이 너무 큽니다.")
        return row

    def shared_state(self, row):
        """None when usable; otherwise why a shared binding is blocked right now."""
        if not row.get("shared"):
            return None
        from agent_dev.adapters.shared_integrations import binding_state

        return binding_state(row["shared"])

    def _require_shared(self, row):
        reason = self.shared_state(row)
        if reason:
            invalid(
                "공통 연결이 비활성화되었거나 없어 사용할 수 없습니다: " + reason,
                ErrorCode.CAPABILITY_UNAVAILABLE,
            )

    def _credentials(self, row):
        if row.get("shared"):
            # Shared bindings resolve only their selected reference, inside this workspace.
            reference = row["shared"].get("credential", "")
            if not reference:
                return ""
            from agent_dev.adapters.credential_store import CredentialStore

            locations = self.store.locations
            values = CredentialStore(locations).resolve(
                reference, locations.workspace_id, row["endpoint"]
            )
            return next(iter(values.values()))
        values = read_environment(self.store.config)
        return (
            values.get(row["credential_ref"], os.environ.get(row["credential_ref"], ""))
            if row["credential_ref"]
            else ""
        ) or self.oauth.token(row["id"], row["endpoint"])

    def _credential_or_empty(self, row):
        try:
            return self._credentials(row)
        except DomainError as error:
            if error.code != ErrorCode.AUTH_REQUIRED:
                raise
            return ""

    def launch(self, key):
        """What starting this stdio connection runs here, for the user to confirm.

        The name check in ``connection_row`` only steers people away from install-on-run
        launchers; it cannot tell a safe program from ``python3 -c``. The boundary is this:
        a stdio server starts only after the user confirmed the resolved executable, its
        arguments and the environment names it receives. Any change to them asks again.
        """
        row = key if isinstance(key, dict) else self.show(key)
        if row.get("transport") != "stdio" or not row.get("command"):
            invalid("로컬 실행(stdio) 연결만 실행 확인이 필요합니다.")
        program = row["command"][0]
        if os.path.isabs(program):
            found = program if os.path.isfile(program) else ""
        elif os.sep in program or (os.altsep and os.altsep in program):
            candidate = Path(self.root) / program
            found = str(candidate) if candidate.is_file() else ""
        else:
            found = shutil.which(program) or ""
        executable = os.path.abspath(found) if found else ""
        subject = {
            "executable": executable,
            "command": list(row["command"]),
            "environment_keys": sorted(row["environment_keys"]),
        }
        from agent_dev.adapters import workspace_trust

        value = digest(subject)
        return {
            "id": row["id"],
            "name": row["name"],
            "executable": executable,
            "real_path": os.path.realpath(executable) if executable else "",
            "arguments": list(row["command"][1:]),
            "environment_keys": subject["environment_keys"],
            "digest": value,
            "found": bool(executable),
            "approved": bool(executable)
            and workspace_trust.command_approval(self.store.config, row["id"]) == value,
        }

    def approve_launch(self, key, digest_value, actor="local-user"):
        from agent_dev.adapters import workspace_trust

        launch = self.launch(key)
        if not launch["found"]:
            invalid("MCP 실행 파일을 찾을 수 없습니다. 먼저 설치하거나 경로를 확인해주세요.")
        if digest_value != launch["digest"]:
            invalid(
                "확인한 뒤에 실행 명령이 바뀌었습니다. 다시 열어 확인해주세요.",
                ErrorCode.STALE_VERSION,
            )
        workspace_trust.approve_command(self.store.config, launch["id"], launch["digest"], actor)
        return self.launch(key)

    def _client(self, row, timeout=30):
        if row.get("transport") == "stdio":
            from agent_dev.adapters import workspace_trust

            if not row.get("shared"):
                # A project stdio server is a command from the project's own settings.
                workspace_trust.require(self.store.config, "MCP 서버 실행")
            if not self.launch(row)["approved"]:
                raise DomainError(
                    ErrorCode.APPROVAL_REQUIRED,
                    "이 MCP 서버가 실제로 실행할 명령을 아직 확인하지 않았습니다. 외부 도구 연결 "
                    "화면의 '실행 확인'(또는 `devroster integration launch <연결 ID>`)에서 실행 파일 경로와 "
                    "인수를 확인해주세요.",
                )
        values = read_environment(self.store.config)
        config = dict(
            row,
            _environment={
                key: values.get(key, os.environ.get(key, "")) for key in row["environment_keys"]
            },
        )
        return self.client_factory(config, credential=self._credentials(row), cwd=self.root, timeout=timeout)

    def check(self, key, *, operation="", arguments=None, scope=""):
        row, base = self.show(key), provider(self.show(key)["provider"])
        if not row["enabled"]:
            state, message = "disabled", "연결이 비활성화되어 있습니다."
        elif self.shared_state(row):
            state, message = "disabled", "공통 연결이 비활성화되었거나 없습니다."
        elif (
            base["supported_client_only"]
            and row["transport"] == "http"
            and not row.get("supported_client")
        ):
            state, message = (
                "auth_required",
                "지원 MCP 클라이언트 인증을 확인하거나 Figma REST 연결을 선택해주세요.",
            )
        elif row["transport"] == "stdio" and not self.launch(row)["found"]:
            state, message = "installation_required", "MCP 실행 파일을 먼저 설치해주세요."
        elif row["transport"] == "stdio" and not self.launch(row)["approved"]:
            state = "approval_required"
            message = (
                "실제로 실행할 명령을 아직 확인하지 않았습니다. '실행 확인'에서 실행 파일 경로와 "
                "인수를 확인해주세요."
            )
        elif (
            row["transport"] in {"http", "api"}
            and base["auth"] != "none"
            and not self._credential_or_empty(row)
        ):
            state, message = (
                "auth_required",
                "환경 설정에서 credential 참조 또는 OAuth 인증을 연결해주세요.",
            )
        else:
            try:
                if row["transport"] == "api":
                    if not scope:
                        invalid("실제 접근할 Figma 파일 key를 입력해주세요.")
                    result = self._figma(row, scope)
                    row["state"], row["checked_at"], row["message"] = (
                        "ready",
                        time.time(),
                        "선택한 Figma 파일 읽기 성공",
                    )
                    self.store.save(row, row["version"])
                    return dict(self.show(key), sample=redact(result, (self._credentials(row),)))
                with self._client(row) as client:
                    tools = client.tools()
                    row['protocol_version'] = getattr(client, 'protocol_version', '')
                    row['protocol_era'] = getattr(client, 'protocol_era', '')
                selected = {
                    tool["name"]: tool.get("inputSchema", {"type": "object"})
                    for tool in tools
                    if tool["name"] in {v["tool"] for v in row["operations"].values()}
                }
                row["tool_schemas"] = selected
                row["schema_digest"] = digest(selected)
                state, message = (
                    "partial",
                    "연결 및 도구 목록 확인 완료. 실제 읽기 검사를 실행해주세요.",
                )
                if not selected:
                    message = (
                        "허용 도구가 서버 목록과 다릅니다. 공식 스키마/권한 매핑을 확인해주세요."
                    )
                if row.get('protocol_version'):
                    message += f" MCP {row['protocol_version']} ({row['protocol_era']})."
                row.update(state=state, checked_at=time.time(), message=message)
                self.store.save(row, row["version"])
                if operation:
                    bundle = self.call(key, operation, arguments or {}, scope=scope, _checking=True)
                    row = self.show(key)
                    row.update(
                        state="ready", checked_at=time.time(), message=(
                            "등록 범위의 실제 읽기 성공" +
                            (f" MCP {row['protocol_version']} ({row['protocol_era']})."
                             if row.get('protocol_version') else '')
                        ),
                        last_read_digest=bundle["artifact_digest"]
                    )
                    return self.store.save(row, row["version"])
                return self.show(key)
            except (DomainError, OSError, ValueError) as exc:
                row = self.show(key)
                state = (
                    "auth_required"
                    if isinstance(exc, DomainError) and exc.code == ErrorCode.AUTH_REQUIRED
                    else "error"
                )
                message = (
                    str(exc)
                    if isinstance(exc, DomainError)
                    else "연결 검사에 실패했습니다. 설치/입력/서버 상태를 확인해주세요."
                )
        row.update(state=state, checked_at=time.time(), message=message)
        return self.store.save(row, row["version"])

    def _bound(self, row, arguments, scope):
        if not isinstance(arguments, dict) or len(canonical(arguments)) > 20_000:
            invalid("도구 입력은 제한된 JSON 객체가 필요합니다.")
        if scope not in row["scopes"]:
            invalid("이 연결에 등록된 자료 범위를 선택해주세요.", ErrorCode.POLICY_VIOLATION)
        fixed = dict(row["bound_arguments"])
        for key, value in arguments.items():
            if key in RESOURCE_KEYS and isinstance(value, str):
                decoded = unquote(value)
                if "\\" in decoded or any(part in {".", ".."} for part in decoded.split("/")):
                    invalid("자료 범위에 경로 이동을 사용할 수 없습니다.", ErrorCode.POLICY_VIOLATION)
            if key in fixed and value != fixed[key]:
                invalid("등록된 도구 자료 범위를 변경할 수 없습니다.", ErrorCode.POLICY_VIOLATION)
            if (
                key in RESOURCE_KEYS
                and key not in fixed
                and (
                    not isinstance(value, str)
                    or not (
                        value == scope
                        or value.startswith(scope.rstrip("/") + "/")
                        or key in {"name", "parent"}
                        and (value == "projects/" + scope.rstrip("/")
                             or value.startswith("projects/" + scope.rstrip("/") + "/"))
                    )
                )
            ):
                invalid(
                    "도구 입력 자료가 등록 범위를 벗어납니다. 고정 입력값을 등록해주세요.",
                    ErrorCode.POLICY_VIOLATION,
                )
        fixed.update(arguments)
        if (
            row["provider"] == "github"
            and "method" in fixed
            and fixed["method"]
            not in {
                "get",
                "get_diff",
                "get_files",
                "get_comments",
                "get_reviews",
                "get_status",
                "get_review_comments",
            }
        ):
            invalid("GitHub 읽기 메서드만 사용할 수 있습니다.", ErrorCode.POLICY_VIOLATION)
        if row["provider"] == "dbhub" and any(k.lower() in {"sql", "query"} for k in fixed):
            invalid(
                "DBHub는 metadata 조회를 사용하세요. SQL 실행에는 별도 read-only 검증 경로가 필요합니다.",
                ErrorCode.POLICY_VIOLATION,
            )
        return fixed

    def call(
        self, connection_id, operation, arguments, *, scope="", _checking=False, expected_grant=None,
        timeout_seconds=30
    ):
        if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 30:
            invalid("도구 실행 제한은 1~30초입니다.")
        row = self.show(connection_id)
        self._require_shared(row)
        if not row["enabled"] or not _checking and row["state"] != "ready":
            invalid("실제 읽기 검사를 완료한 연결이 필요합니다.", ErrorCode.CAPABILITY_UNAVAILABLE)
        if expected_grant is not None:
            from agent_dev.adapters.snapshot_tools import binding_digest

            if binding_digest(row) != expected_grant.get("binding_digest") or row[
                "schema_digest"
            ] != expected_grant.get("schema_digest"):
                invalid(
                    "승인 후 연결 설정/도구 스키마가 변경되었습니다. 새 작업 snapshot이 필요합니다.",
                    ErrorCode.STALE_VERSION,
                )
        spec = row["operations"].get(operation)
        if spec is None:
            invalid("허용된 읽기 도구를 선택해주세요.", ErrorCode.POLICY_VIOLATION)
        if spec["capability"] == "browser.read":
            invalid("브라우저 읽기는 격리 세션·현재 페이지 origin 검증을 지원하는 호스트가 필요합니다. 현재 제품 연결은 도구 목록만 확인합니다.", ErrorCode.CAPABILITY_UNAVAILABLE)
        operation_fixed = spec.get("fixed_arguments", {})
        if not isinstance(operation_fixed, dict) or any(
            key in arguments and arguments[key] != value for key, value in operation_fixed.items()
        ):
            invalid("도구의 고정 입력값을 변경할 수 없습니다.", ErrorCode.POLICY_VIOLATION)
        args = self._bound(row, dict(arguments, **operation_fixed), scope)
        if row["transport"] == "api":
            content = self._figma(row, scope, operation, args, timeout_seconds)
        else:
            with self._client(row, timeout_seconds) as client:
                tools = client.tools()
                schemas = {
                    t["name"]: t.get("inputSchema", {"type": "object"})
                    for t in tools
                    if t["name"] in row["tool_schemas"]
                }
                if digest(schemas) != row["schema_digest"]:
                    invalid(
                        "도구 스키마가 변경되었습니다. 연결을 다시 검사해주세요.",
                        ErrorCode.STALE_VERSION,
                    )
                schema = row["tool_schemas"].get(spec["tool"])
                if schema is None:
                    invalid(
                        "이 서버에서 허용 도구를 사용할 수 없습니다.",
                        ErrorCode.CAPABILITY_UNAVAILABLE,
                    )
                validate_arguments(schema, args)
                content = client.call(spec["tool"], args)
        secret = self._credentials(row)
        from agent_dev.adapters.secrets import registered_secrets

        content = redact(
            content, (*registered_secrets(self.store.config), *((secret,) if secret else ()))
        )
        bundle = {
            "provider": row["provider"],
            "connection_id": connection_id,
            "operation": operation,
            "scope": scope,
            "trust": "untrusted_data",
            "content": content,
            "digest": digest(content),
            "fetched_at": time.time(),
        }
        self.store.event(
            "IntegrationRead", {key: value for key, value in bundle.items() if key != "content"}
        )
        cache = self.store.locations.cache_root
        directory = cache / "integration-artifacts" / connection_id
        from agent_dev.adapters.project_setup import atomic_json

        for ancestor in (cache, directory.parent, directory):
            if ancestor.is_symlink():
                invalid("자료 저장 위치에 심볼릭 링크를 사용할 수 없습니다.")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (bundle["digest"] + ".json")
        if not path.exists():
            atomic_json(path, bundle)
        encoded = canonical(content)
        from agent_dev.adapters.project_setup import ProjectSetup
        _project, _version, profile = ProjectSetup(self.root).current_profile()
        token_mode = (profile or {}).get("context_policy", {}).get("token_mode", row["token_mode"])
        limit = (
            20_000
            if token_mode == "off"
            else 12_000
            if token_mode == "balanced"
            else 6000
        )
        bundle.update(
            artifact_digest=bundle["digest"],
            original_bytes=len(encoded.encode()),
            delivered_bytes=min(len(encoded.encode()), limit),
            token_measurement="estimated",
            truncated=len(encoded.encode()) > limit,
        )
        if bundle["truncated"]:
            bundle["content"] = encoded.encode()[:limit].decode(errors="ignore")
        return bundle

    def _figma(self, row, file_key, operation="get_file", arguments=None, timeout=30):
        from agent_dev.adapters.mcp_client import request_remote

        if not re.fullmatch(r"[A-Za-z0-9]{1,120}", file_key) or file_key not in row["scopes"]:
            invalid("등록된 Figma 파일 key가 필요합니다.")
        token = self._credentials(row)
        if not token:
            invalid("Figma PAT를 환경 설정에서 연결해주세요.", ErrorCode.AUTH_REQUIRED)
        arguments = dict(arguments or {})
        allowed = {"file_key", "fileKey", "ids", "depth", "format", "scale"}
        if set(arguments) - allowed:
            invalid("Figma REST 읽기 입력값을 확인해주세요.")
        query = {}
        if "depth" in arguments:
            if type(arguments["depth"]) is not int or not 1 <= arguments["depth"] <= 10:
                invalid("Figma 탐색 깊이는 1~10입니다.")
            query["depth"] = arguments["depth"]
        if operation in {"get_nodes", "get_images"}:
            ids = arguments.get("ids", [])
            if isinstance(ids, str):
                ids = ids.split(",")
            if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(
                not isinstance(item, str) or not re.fullmatch(r"[0-9]+:[0-9]+", item) for item in ids
            ):
                invalid("Figma node ID 목록이 필요합니다. 예: 12:34")
            query["ids"] = ",".join(ids)
        if operation == "get_images":
            image_format = arguments.get("format", "png")
            scale = arguments.get("scale", 1)
            if image_format not in {"png", "jpg", "svg", "pdf"} or type(scale) not in {int, float} or not 0.01 <= scale <= 4:
                invalid("Figma 이미지 형식/배율을 확인해주세요.")
            query.update(format=image_format, scale=scale)
            url = "https://api.figma.com/v1/images/" + file_key
        else:
            url = "https://api.figma.com/v1/files/" + file_key
            if operation == "get_nodes":
                url += "/nodes"
            elif operation != "get_file":
                invalid("지원하지 않는 Figma 읽기 기능입니다.")
        if query:
            url += "?" + urlencode(query)
        status, _, value = request_remote(url, headers={"X-Figma-Token": token}, timeout=timeout)
        if status != 200:
            invalid("Figma 파일 읽기에 실패했습니다.")
        return value

    def disable(self, key):
        row = self.show(key)
        row.update(enabled=False, state="disabled")
        return self.store.save(row, row["version"])

    def export(self):
        rows = self.list()
        for row in rows:
            row["operations"] = {
                name: {"tool": spec["tool"], "capability": spec["capability"]}
                for name, spec in row["operations"].items()
            }
        return {
            "schema_version": 1,
            "connections": [
                {
                    k: v
                    for k, v in row.items()
                    if k in {"id", "provider", "name", "transport", "operations", "token_mode"}
                }
                | {"enabled": False}
                for row in rows
            ],
        }

    def import_document(self, document):
        if (
            not isinstance(document, dict)
            or document.get("schema_version") != 1
            or not isinstance(document.get("connections"), list)
            or len(document["connections"]) > 100
        ):
            invalid("MCP 가져오기 문서 형식을 확인해주세요.")
        prepared = []
        used = {row["id"] for row in self.list()}
        for source in document["connections"]:
            if not isinstance(source, dict) or set(source) - {
                "id",
                "provider",
                "name",
                "transport",
                "operations",
                "token_mode",
                "enabled",
            }:
                invalid("가져오기에는 credential/자료 범위/경로를 포함할 수 없습니다.")
            item: dict[str, Any] = dict(source, scopes=[], enabled=False)
            key = item["id"]
            suffix = 1
            while key in used:
                key = item["id"] + "-copy-" + str(suffix)
                suffix += 1
            item["id"] = key
            used.add(key)
            prepared.append(item)
        validated = [self.configure(item, _persist=False) for item in prepared]
        rows = self.store.import_rows(validated)
        return {"imported": len(rows), "connections": rows, "reconnect_required": True}

    def doctor(self):
        return {
            "connections": self.list(),
            "catalog_count": len(catalog()),
            "live_calls_performed": False,
        }

    def cached(self, connection_id, artifact_digest):
        import json
        self.show(connection_id)
        if not re.fullmatch(r"[a-f0-9]{64}", artifact_digest):
            invalid("자료 digest를 확인해주세요.")
        path = (
            self.store.locations.cache_root
            / "integration-artifacts"
            / connection_id
            / (artifact_digest + ".json")
        )
        if any(p.is_symlink() for p in [path, *path.parents[:3]]) or path.stat().st_size > 3_000_000:
            invalid("원문 자료 위치/크기를 확인해주세요.")
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("connection_id") != connection_id or value.get("digest") != artifact_digest or digest(value.get("content")) != artifact_digest:
            invalid("원문 자료 무결성 검사에 실패했습니다.")
        from agent_dev.adapters.secrets import registered_secrets
        return redact(value, registered_secrets(self.store.config))
