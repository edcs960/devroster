"""Bounded MCP transports. No package installation or server-initiated execution."""

import hashlib
import hmac
import http.client
import ipaddress
import json
import os
import shutil
import socket
import ssl
import threading
import time
from collections import OrderedDict, deque
from typing import NoReturn
from urllib.parse import urlencode, urlsplit

from agent_dev.adapters.stdio_transport import StdioTransport, TransportTimeout
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.mcp_protocol import (
    CLIENT_INFO,
    LEGACY_PROTOCOLS,
    MAX_BYTES,
    MODERN_ERRORS,
    MODERN_PROTOCOLS,
    RpcError,
    decode_object,
    discover_result,
    encode_header,
    header_bindings,
    metadata,
    parameter_headers,
    result,
    same_id,
    stdio_response,
)

PROTOCOLS = LEGACY_PROTOCOLS | set(MODERN_PROTOCOLS)
_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_SALT = os.urandom(32)


def fail(message, code=ErrorCode.EXTERNAL_TRANSIENT) -> NoReturn:
    raise DomainError(code, message)


def remote_address(url, *, local=False, timeout: float = 5):
    parsed = urlsplit(url)
    if parsed.username or parsed.password or parsed.fragment or not parsed.hostname:
        fail("MCP 주소 형식을 확인해주세요.", ErrorCode.INVALID_INPUT)
    if local:
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            fail("로컬 MCP는 명시적인 loopback HTTP 주소만 사용합니다.", ErrorCode.POLICY_VIOLATION)
    elif parsed.scheme != "https":
        fail("원격 MCP는 HTTPS 주소가 필요합니다.", ErrorCode.POLICY_VIOLATION)
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        result = []
        done = threading.Event()

        def resolve():
            try:
                result.append(socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM))
            except OSError:
                result.append(None)
            finally:
                done.set()

        threading.Thread(target=resolve, daemon=True).start()
        if not done.wait(timeout) or not result[0]:
            fail("MCP 서버 DNS 조회 시간이 초과되었거나 주소를 확인할 수 없습니다.")
        addresses = result[0]
    except (ValueError, OSError):
        fail("MCP 서버 주소를 확인할 수 없습니다.")
    ips = {str(row[4][0]) for row in addresses}
    if not ips or any(not ipaddress.ip_address(ip).is_global for ip in ips) and not local:
        fail("원격 MCP의 사설/로컬 네트워크 접근은 허용하지 않습니다.", ErrorCode.POLICY_VIOLATION)
    if local and any(not ipaddress.ip_address(ip).is_loopback for ip in ips):
        fail("로컬 MCP 주소가 loopback 밖을 가리킵니다.", ErrorCode.POLICY_VIOLATION)
    return parsed, min(ips), port


def request_remote(
    url,
    *,
    payload=None,
    form=None,
    headers=None,
    method="GET",
    timeout: float = 30,
    local=False,
    cancel=None,
):
    deadline = time.monotonic() + timeout
    parsed, ip, port = remote_address(url, local=local, timeout=timeout)
    connection = http.client.HTTPConnection(parsed.hostname, port, timeout=timeout)
    timer = None
    finished = threading.Event()
    watcher = None
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            fail("MCP 주소 확인 시간이 초과되었습니다.")
        sock = socket.create_connection((ip, port), timeout=remaining)
        if parsed.scheme == "https":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                sock.close()
                fail("MCP 연결 시간이 초과되었습니다.")
            sock.settimeout(remaining)
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parsed.hostname)
        connection.sock = sock

        def interrupt():
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            if os.name == "nt":
                # Windows does not wake a recv blocked in another thread on shutdown;
                # closing the handle does. Detach first so nothing closes it twice.
                try:
                    socket.close(sock.detach())
                except OSError:
                    pass

        timer = threading.Timer(max(0, deadline - time.monotonic()), interrupt)
        timer.daemon = True
        timer.start()
        if cancel is not None:

            def watch_cancel():
                while not finished.wait(0.02):
                    if cancel.is_set():
                        interrupt()
                        return

            watcher = threading.Thread(target=watch_cancel, name="mcp-http-cancel", daemon=True)
            watcher.start()
        target = parsed.path or "/"
        if parsed.query:
            target += "?" + parsed.query
        body = (
            json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if payload is not None
            else None
        )
        if form is not None:
            body = urlencode(form).encode()
            headers = dict(headers or {}, **{"Content-Type": "application/x-www-form-urlencoded"})
        connection.request(method, target, body=body, headers=headers or {})
        response = connection.getresponse()
        if response.status in {401, 403}:
            fail("MCP 인증/자료 접근 권한을 확인해주세요.", ErrorCode.AUTH_REQUIRED)
        if response.status == 429:
            fail("MCP 호출 한도에 도달했습니다. 잠시 후 다시 확인해주세요.")
        if response.status >= 300 and response.status not in {400, 404, 405}:
            fail("MCP 응답 오류 또는 허용되지 않은 리다이렉트입니다.")
        media = response.getheader("Content-Type", "").split(";")[0]
        response_headers = dict(response.getheaders())
        if media == "text/event-stream":
            chunks, total = [], 0
            while True:
                line = response.readline(MAX_BYTES + 1)
                total += len(line)
                if total > MAX_BYTES:
                    fail("MCP 응답 크기 한도를 초과했습니다.", ErrorCode.INVALID_INPUT)
                if time.monotonic() >= deadline:
                    raise TransportTimeout()
                if not line:
                    break
                if line.startswith(b"data:"):
                    chunks.append(line[5:].strip())
                if line.strip() == b"" and chunks:
                    value = decode_object(b"\n".join(chunks))
                    if "method" in value and "id" in value:
                        fail(
                            "MCP 서버 요청 실행은 허용하지 않습니다.",
                            ErrorCode.CAPABILITY_UNAVAILABLE,
                        )
                    chunks = []
                    if (
                        isinstance(value, dict)
                        and same_id(value.get("id"), (payload or {}).get("id"))
                        and ("result" in value or "error" in value)
                    ):
                        return response.status, response_headers, value
            fail("MCP SSE 응답이 완료되지 않았습니다.")
        raw = response.read(MAX_BYTES + 1)
        if time.monotonic() >= deadline:
            raise TransportTimeout()
        if len(raw) > MAX_BYTES:
            fail("MCP 응답이 자료 한도를 초과했습니다.", ErrorCode.INVALID_INPUT)
        return response.status, response_headers, decode_object(raw) if raw.strip() else {}
    except TimeoutError:
        raise TransportTimeout() from None
    except (OSError, ValueError, http.client.HTTPException):
        if time.monotonic() >= deadline:
            raise TransportTimeout() from None
        fail("MCP 통신 실패. 주소/인증/서버 상태를 확인해주세요.")
    finally:
        finished.set()
        if watcher:
            watcher.join(timeout=0.1)
        if timer:
            timer.cancel()
        connection.close()


def _schema_contract(value):
    """Strip annotations at schema nodes, preserving literal values and property names."""
    if not isinstance(value, dict):
        return value
    return {
        key: (
            {name: _schema_contract(schema) for name, schema in item.items()}
            if key == "properties" and isinstance(item, dict)
            else item
        )
        for key, item in value.items()
        if key != "x-mcp-header"
    }


class McpClient:
    def __init__(self, config, *, credential="", cwd=None, timeout=30):
        self.config, self.timeout, self.cwd = config, timeout, cwd
        self.headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if credential:
            self.headers[
                "X-Goog-Api-Key" if config.get("provider") == "stitch" else "Authorization"
            ] = credential if config.get("provider") == "stitch" else "Bearer " + credential
        self.deadline = time.monotonic() + timeout
        self.next_id = 0
        self._abandoned_ids: deque[int] = deque(maxlen=16)
        self.process = None
        self.transport = None
        self.protocol_version = MODERN_PROTOCOLS[0]
        self.protocol_era = "modern"
        self.closed = False
        self.bindings = {}
        self.tool_schemas = {}
        self.cancelled = threading.Event()
        self.secrets = (credential, *config.get("_environment", {}).values())
        # Only opaque keyed digests are retained. No credentials, env values or schemas in cache.
        material = json.dumps([config, str(cwd), credential], sort_keys=True).encode()
        self.cache_key = hmac.new(_CACHE_SALT, material, hashlib.sha256).hexdigest()

    def __enter__(self):
        try:
            if self.config["transport"] == "stdio":
                argv = self.config["command"]
                binary = shutil.which(argv[0])
                if not binary:
                    fail(
                        "MCP 실행 파일이 없습니다. 설치 준비를 확인해주세요.",
                        ErrorCode.CAPABILITY_UNAVAILABLE,
                    )
                env = {
                    k: os.environ[k]
                    for k in ("PATH", "HOME", "LANG", "TMPDIR", "SYSTEMROOT")
                    if k in os.environ
                }
                env.update(self.config.get("_environment", {}))
                self.transport = StdioTransport(
                    [binary, *argv[1:]],
                    cwd=self.cwd,
                    env=env,
                    deadline=self.deadline,
                    secrets=self.secrets,
                )
                self.process = self.transport.process
            cached = self._cached() if not self.process else None
            if cached:
                self.protocol_era, self.protocol_version = cached
                if self.protocol_era == "legacy":
                    self._initialize()
            else:
                self._negotiate()
            return self
        except BaseException:
            self.close()
            raise

    def _cached(self):
        with _CACHE_LOCK:
            entry = _CACHE.get(self.cache_key)
            if entry and entry[0] > time.monotonic():
                _CACHE.move_to_end(self.cache_key)
                return entry[1:]
            _CACHE.pop(self.cache_key, None)
        return None

    def _remember(self, ttl: float = 600):
        if self.process:
            return  # The selected protocol lives on this client/owned process only.
        with _CACHE_LOCK:
            _CACHE[self.cache_key] = (
                time.monotonic() + min(ttl, 3600),
                self.protocol_era,
                self.protocol_version,
            )
            _CACHE.move_to_end(self.cache_key)
            while len(_CACHE) > 128:
                _CACHE.popitem(last=False)

    def _invalidate(self):
        with _CACHE_LOCK:
            _CACHE.pop(self.cache_key, None)

    def _negotiate(self):
        probe_deadline = min(self.deadline, time.monotonic() + min(1, self.timeout / 4))
        modern_seen = False
        for attempt in range(2):
            try:
                found = self.rpc("server/discover", {}, _deadline=probe_deadline, _probe=True)
            except RpcError as exc:
                if exc.rpc_code in MODERN_ERRORS:
                    modern_seen = True
                    self._invalidate()
                    compatible = next((v for v in MODERN_PROTOCOLS if v in exc.supported), None)
                    if exc.rpc_code == -32022 and compatible and not attempt:
                        self.protocol_version = compatible
                        continue
                    raise
                if modern_seen:
                    raise
                break
            except TransportTimeout:
                if modern_seen:
                    raise
                break
            version = discover_result(found)
            if version:
                self.protocol_version = version
                self._remember(min(found["ttlMs"], 3_600_000) / 1000)
                return
            if modern_seen:
                fail(
                    "MCP modern 서버 discovery 응답이 유효하지 않습니다.",
                    ErrorCode.CAPABILITY_UNAVAILABLE,
                )
            break
        self.protocol_era = "legacy"
        self.protocol_version = "2025-11-25"
        self._initialize()
        self._remember()

    def _initialize(self):
        self.headers.pop("Mcp-Session-Id", None)
        self.headers.pop("MCP-Protocol-Version", None)
        initialized = self.rpc(
            "initialize",
            {
                "protocolVersion": self.protocol_version,
                "capabilities": {},
                "clientInfo": dict(CLIENT_INFO),
            },
        )
        version = initialized.get("protocolVersion")
        if not isinstance(version, str) or version not in LEGACY_PROTOCOLS:
            self._invalidate()
            fail("서버 MCP 프로토콜 버전을 지원하지 않습니다.", ErrorCode.CAPABILITY_UNAVAILABLE)
        self.protocol_version = version
        self.headers["MCP-Protocol-Version"] = version
        self.rpc("notifications/initialized", notification=True)

    def rpc(self, method, params=None, *, notification=False, _deadline=None, _probe=False):
        deadline = min(self.deadline, _deadline) if _deadline is not None else self.deadline
        try:
            try:
                return self._rpc(
                    method, params, notification=notification, deadline=deadline, probe=_probe
                )
            except RpcError as exc:
                if exc.rpc_code in MODERN_ERRORS:
                    self._invalidate()
                    compatible = next((v for v in MODERN_PROTOCOLS if v in exc.supported), None)
                    if (
                        not _probe
                        and self.protocol_era == "modern"
                        and exc.rpc_code == -32022
                        and compatible
                    ):
                        self.protocol_version = compatible
                        return self._rpc(
                            method, params, notification=notification, deadline=deadline
                        )
                raise
        except RpcError:
            raise
        except BaseException:
            if not _probe:
                self.close()
            raise

    def _rpc(self, method, params, *, notification, deadline, probe=False):
        remaining = deadline - time.monotonic()
        if self.closed or remaining <= 0:
            raise TransportTimeout()
        self.next_id += 1
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = dict(params)
        headers = dict(self.headers)
        if self.protocol_era == "modern":
            payload.setdefault("params", {})["_meta"] = metadata(self.protocol_version)
            headers.pop("Mcp-Session-Id", None)
            headers["MCP-Protocol-Version"] = self.protocol_version
            headers["Mcp-Method"] = method
            if method in {"tools/call", "resources/read", "prompts/get"}:
                source = payload["params"].get("name", payload["params"].get("uri"))
                headers["Mcp-Name"] = encode_header(source)
            if method == "tools/call" and not self.process:
                assert params is not None
                headers.update(
                    parameter_headers(
                        self.bindings.get(params["name"], []), params.get("arguments", {})
                    )
                )
        if not notification:
            payload["id"] = self.next_id
        raw = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_BYTES:
            fail("MCP 요청 크기 한도를 초과했습니다.", ErrorCode.INVALID_INPUT)
        if self.transport:
            self.transport.write(raw + b"\n", deadline)
            if notification:
                return {}
            seen = 0
            while True:
                try:
                    value = self.transport.read(deadline)
                except TransportTimeout:
                    if probe:
                        # Only successfully written requests whose response wait timed out.
                        self._abandoned_ids.append(payload["id"])
                    raise
                seen += self.transport.last_frame_bytes
                if seen > MAX_BYTES:
                    fail("MCP stdio 응답 크기 한도를 초과했습니다.", ErrorCode.INVALID_INPUT)
                if not stdio_response(value):
                    continue
                if same_id(value["id"], payload["id"]):
                    return result(value)
                if any(same_id(value["id"], old) for old in self._abandoned_ids):
                    self._abandoned_ids.remove(value["id"])
                    continue
                fail("MCP 응답 요청 ID가 알려진 요청과 일치하지 않습니다.", ErrorCode.INVALID_INPUT)
        status, response_headers, value = request_remote(
            self.config["endpoint"],
            payload=payload,
            headers=headers,
            method="POST",
            timeout=max(0, deadline - time.monotonic()),
            local=self.config.get("local", False),
            cancel=self.cancelled,
        )
        if notification:
            if status >= 400:
                fail("MCP notification endpoint 또는 세션을 확인해주세요.")
            return {}
        if probe and status in {400, 404, 405}:
            # Legacy HTTP rejection bodies need not be JSON-RPC responses. Inspect
            # modern errors before allowing only this discovery probe to fall back.
            error = value.get("error")
            modern_error = (
                isinstance(error, dict)
                and type(error.get("code")) is int
                and error["code"] in MODERN_ERRORS
            )
            if not modern_error:
                if "id" in value and not same_id(value["id"], payload["id"]):
                    fail("MCP 응답 요청 ID가 일치하지 않습니다.", ErrorCode.INVALID_INPUT)
                raise RpcError({"code": -32601})
        if not isinstance(value, dict) or not same_id(value.get("id"), payload["id"]):
            fail("MCP 응답 요청 ID가 일치하지 않습니다.", ErrorCode.INVALID_INPUT)
        if "error" in value:
            return result(value)  # Modern errors are carried in HTTP 400/404 bodies too.
        if status >= 400:
            fail("MCP endpoint 또는 세션을 확인해주세요.")
        if self.protocol_era == "legacy":
            session = next(
                (v for k, v in response_headers.items() if k.lower() == "mcp-session-id"), None
            )
            if session:
                if not all(33 <= ord(c) <= 126 for c in session) or len(session) > 512:
                    fail("잘못된 MCP 세션입니다.")
                self.headers["Mcp-Session-Id"] = session
        return result(value)

    def tools(self):
        rows, cursor, seen, bindings, schemas = [], None, set(), {}, {}
        for _ in range(20):
            response = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
            tools = response.get("tools", [])
            if not isinstance(tools, list) or any(
                not isinstance(t, dict) or not isinstance(t.get("name"), str) for t in tools
            ):
                fail("MCP 도구 목록 형식이 올바르지 않습니다.")
            for tool in tools:
                if self.protocol_era == "modern" and not self.process:
                    try:
                        bindings[tool["name"]] = header_bindings(tool.get("inputSchema", {}))
                    except DomainError:
                        continue
                schemas[tool["name"]] = tool.get("inputSchema", {})
                rows.append(tool)
            if len(rows) > 1000:
                fail("MCP 도구 개수 한도를 초과했습니다.")
            cursor = response.get("nextCursor")
            if not cursor:
                self.bindings, self.tool_schemas = bindings, schemas
                return rows
            if not isinstance(cursor, str) or cursor in seen:
                fail("MCP 도구 목록 cursor가 유효하지 않습니다.")
            seen.add(cursor)
        fail("MCP 도구 목록 페이지 한도를 초과했습니다.")

    def call(self, name, arguments):
        # Callers remain responsible for the existing readonly operation/schema/scope guard.
        if self.protocol_era == "modern" and not self.process and name not in self.bindings:
            self.tools()
            if name not in self.bindings:
                fail(
                    "MCP 도구 헤더 정의가 없거나 유효하지 않습니다.",
                    ErrorCode.CAPABILITY_UNAVAILABLE,
                )
        original_schema = self.tool_schemas.get(name)
        try:
            return self.rpc("tools/call", {"name": name, "arguments": arguments})
        except RpcError as exc:
            if self.protocol_era != "modern" or self.process or exc.rpc_code != -32020:
                raise
            # Refresh a stale binding once. Existing Integrations readonly checks still apply.
            self.tools()
            if _schema_contract(original_schema) != _schema_contract(self.tool_schemas.get(name)):
                fail(
                    "MCP 도구 스키마가 변경되었습니다. 연결을 다시 검사해주세요.",
                    ErrorCode.STALE_VERSION,
                )
            if name not in self.bindings:
                fail("갱신된 MCP 도구 정의가 유효하지 않습니다.", ErrorCode.CAPABILITY_UNAVAILABLE)
            return self.rpc("tools/call", {"name": name, "arguments": arguments})

    def cancel(self):
        if self.transport and not self.closed:
            try:
                notification = {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": self.next_id},
                }
                self.transport.write(
                    json.dumps(notification).encode() + b"\n",
                    min(self.deadline, time.monotonic() + 0.05),
                )
            except DomainError:
                pass
        self.close()

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._abandoned_ids.clear()
        self.cancelled.set()
        if self.transport:
            self.transport.close()
        elif self.protocol_era == "legacy" and self.headers.get("Mcp-Session-Id"):
            remaining = min(3, self.deadline - time.monotonic())
            try:
                if remaining > 0:
                    request_remote(
                        self.config["endpoint"],
                        headers=self.headers,
                        method="DELETE",
                        timeout=remaining,
                        local=self.config.get("local", False),
                    )
            except DomainError:
                pass
            self.headers.pop("Mcp-Session-Id", None)

    def __exit__(self, *args):
        self.close()
