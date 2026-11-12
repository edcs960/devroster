"""MCP era detection and safe, statically bound HTTP request metadata."""

import base64
import json
import math
import re
from typing import NoReturn

from agent_dev.domain.errors import DomainError, ErrorCode

MODERN_PROTOCOLS = ("2026-07-28",)
LEGACY_PROTOCOLS = frozenset({"2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"})
MODERN_ERRORS = frozenset({-32020, -32021, -32022})
CLIENT_INFO = {"name": "devroster", "version": "0.4.8"}
MAX_BYTES = 2 * 1024 * 1024
TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")


def invalid(message, code=ErrorCode.INVALID_INPUT) -> NoReturn:
    raise DomainError(code, message)


class RpcError(DomainError):
    """Keep only structured compatibility facts, never server diagnostic strings."""

    def __init__(self, error):
        code = error.get("code")
        self.rpc_code = code if type(code) is int else None
        data = error.get("data")
        supported = data.get("supported", []) if isinstance(data, dict) else []
        self.supported = (
            supported
            if isinstance(supported, list) and all(isinstance(v, str) for v in supported)
            else []
        )
        super().__init__(
            ErrorCode.CAPABILITY_UNAVAILABLE
            if self.rpc_code in MODERN_ERRORS
            else ErrorCode.EXTERNAL_TRANSIENT,
            "MCP 프로토콜/클라이언트 기능이 호환되지 않습니다."
            if self.rpc_code in MODERN_ERRORS
            else "MCP 요청 오류. 도구/입력 스키마를 확인해주세요.",
        )


def metadata(version):
    return {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientInfo": dict(CLIENT_INFO),
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def same_id(actual, expected):
    return type(actual) is type(expected) and actual == expected


def decode_object(raw):
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        invalid("MCP 응답 UTF-8 형식이 올바르지 않습니다.")
    try:
        value = json.loads(
            text, parse_constant=lambda _: invalid("MCP JSON 숫자가 유효하지 않습니다.")
        )
    except (ValueError, RecursionError):
        invalid("MCP 응답 JSON 형식이 올바르지 않습니다.")
    if not isinstance(value, dict):
        invalid("MCP 응답은 JSON 객체여야 합니다.")
    pending: list[tuple[object, int]] = [(value, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > 64:
            invalid("MCP JSON 중첩 한도를 초과했습니다.")
        if isinstance(node, dict):
            pending.extend((item, depth + 1) for item in node.values())
        elif isinstance(node, list):
            pending.extend((item, depth + 1) for item in node)
    return value


def stdio_response(value):
    """Validate every envelope before response correlation or notification discard."""
    if value.get("jsonrpc") != "2.0":
        invalid("MCP JSON-RPC 응답 형식이 올바르지 않습니다.")
    if "method" in value:
        if (
            not isinstance(value["method"], str)
            or not value["method"]
            or "params" in value
            and not isinstance(value["params"], dict)
            or "result" in value
            or "error" in value
        ):
            invalid("MCP JSON-RPC notification 형식이 올바르지 않습니다.")
        if "id" in value:
            invalid("MCP 서버 요청 실행은 허용하지 않습니다.", ErrorCode.CAPABILITY_UNAVAILABLE)
        return False
    if ("result" in value) == ("error" in value):
        invalid("MCP JSON-RPC 응답 형식이 올바르지 않습니다.")
    if "id" not in value or type(value["id"]) not in (int, str):
        invalid("MCP 응답 요청 ID 타입이 올바르지 않습니다.")
    if "result" in value and not isinstance(value["result"], dict):
        invalid("MCP JSON-RPC result 형식이 올바르지 않습니다.")
    if "error" in value:
        error = value["error"]
        if (
            not isinstance(error, dict)
            or type(error.get("code")) is not int
            or "message" in error
            and not isinstance(error["message"], str)
        ):
            invalid("MCP JSON-RPC 오류 형식이 올바르지 않습니다.")
    return True


def result(value):
    if value.get("jsonrpc") != "2.0" or ("result" in value) == ("error" in value):
        invalid("MCP JSON-RPC 응답 형식이 올바르지 않습니다.")
    if "error" in value:
        if not isinstance(value["error"], dict):
            invalid("MCP JSON-RPC 오류 형식이 올바르지 않습니다.")
        raise RpcError(value["error"])
    output = value["result"]
    if not isinstance(output, dict) or output.get("isError"):
        invalid("MCP 도구 호출에 실패했습니다.", ErrorCode.EXTERNAL_TRANSIENT)
    if output.get("resultType", "complete") != "complete":
        invalid(
            "MCP 추가 입력/작업/서버 실행 기능은 지원하지 않습니다.",
            ErrorCode.CAPABILITY_UNAVAILABLE,
        )
    return output


def discover_result(value):
    """A blind legacy unknown-method success isn't evidence of modern semantics."""
    markers = {"supportedVersions", "ttlMs", "cacheScope", "resultType"}
    if not markers.intersection(value):
        return None
    versions, ttl = value.get("supportedVersions"), value.get("ttlMs")
    if (
        not isinstance(versions, list)
        or not versions
        or any(not isinstance(v, str) for v in versions)
        or not isinstance(value.get("capabilities"), dict)
        or type(ttl) not in (int, float)
        or isinstance(ttl, float)
        and not math.isfinite(ttl)
        or ttl < 0
        or not isinstance(value.get("cacheScope"), str)
        or value["cacheScope"] not in {"public", "private"}
        or value.get("resultType") != "complete"
    ):
        invalid("MCP DiscoverResult 형식이 올바르지 않습니다.", ErrorCode.CAPABILITY_UNAVAILABLE)
    for version in MODERN_PROTOCOLS:
        if version in versions:
            return version
    invalid("서버와 호환되는 modern MCP 버전이 없습니다.", ErrorCode.CAPABILITY_UNAVAILABLE)


def encode_header(value):
    if type(value) is bool:
        text = "true" if value else "false"
    elif type(value) is int and abs(value) <= 2**53 - 1:
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        invalid("MCP 헤더 입력은 안전 범위의 integer/string/boolean이어야 합니다.")
    if (
        text != text.strip()
        or any(ord(c) < 32 or ord(c) > 126 for c in text)
        or text.startswith("=?base64?")
        and text.endswith("?=")
    ):
        return "=?base64?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="
    return text


def header_bindings(schema):
    """Inspect all schema nodes: annotations outside a properties-only path invalidate tools."""
    bindings, names = [], set()

    def visit(node, path, reachable, depth=0):
        if depth > 32:
            invalid("MCP 헤더 스키마 중첩 한도를 초과했습니다.")
        if isinstance(node, list):
            for child in node:
                visit(child, path, False, depth + 1)
        elif isinstance(node, dict):
            if "x-mcp-header" in node:
                name, kind = node["x-mcp-header"], node.get("type")
                if (
                    not reachable
                    or not path
                    or not isinstance(name, str)
                    or not TOKEN.fullmatch(name)
                    or name.lower() in names
                    or kind not in ("integer", "string", "boolean")
                ):
                    invalid("MCP x-mcp-header 도구 정의가 유효하지 않습니다.")
                names.add(name.lower())
                bindings.append((path, name, kind))
            for key, child in node.items():
                if key == "properties" and isinstance(child, dict):
                    for prop, subschema in child.items():
                        visit(subschema, (*path, prop), reachable, depth + 1)
                elif isinstance(child, (dict, list)):
                    visit(child, path, False, depth + 1)

    visit(schema, (), True)
    return bindings


def parameter_headers(bindings, arguments):
    headers = {}
    for path, name, kind in bindings:
        value = arguments
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        if value is None:
            continue
        expected = {"integer": int, "string": str, "boolean": bool}[kind]
        if type(value) is not expected:
            invalid("MCP 헤더 입력 타입이 스키마와 일치하지 않습니다.")
        headers["Mcp-Param-" + name] = encode_header(value)
    return headers
