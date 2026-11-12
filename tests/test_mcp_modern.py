"""Real, bounded dual-era MCP fixtures; no installed providers or account access."""

import base64
import json
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_dev.adapters.mcp_client import MAX_BYTES, McpClient
from agent_dev.domain.errors import DomainError, ErrorCode

MODERN = "2026-07-28"
DISCOVER = {
    "resultType": "complete",
    "supportedVersions": [MODERN],
    "capabilities": {"tools": {}},
    "ttlMs": 0,
    "cacheScope": "private",
}
TOOL = {"name": "read", "inputSchema": {"type": "object"}}


def stdio(tmp_path, body, *, timeout=3, environment=None):
    program = tmp_path / "server.py"
    program.write_text("import json,sys,os,time,subprocess\n" + body, encoding="utf-8")
    return McpClient(
        {
            "transport": "stdio",
            "command": [sys.executable, str(program)],
            "_environment": environment or {},
        },
        cwd=tmp_path,
        timeout=timeout,
    )


def server_loop(result_code):
    return (
        """for line in sys.stdin:
 q=json.loads(line)
 if 'id' not in q: continue
 """
        + result_code
        + """
 sys.stdout.buffer.write((json.dumps({'jsonrpc':'2.0','id':q['id'],'result':r},ensure_ascii=False)+'\\n').encode('utf-8'));sys.stdout.buffer.flush()
"""
    )


@contextmanager
def http_fixture(respond):
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_DELETE(self):
            observed.append(("DELETE", dict(self.headers)))
            self.send_response(204)
            self.end_headers()

        def do_POST(self):
            q = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            observed.append((q, dict(self.headers)))
            status, value = respond(q, self.headers)
            raw = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", "must-ignore-modern")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield (
            {
                "transport": "http",
                "endpoint": f"http://127.0.0.1:{server.server_port}/mcp",
                "local": True,
            },
            observed,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def reply(q, result):
    return 200, {"jsonrpc": "2.0", "id": q["id"], "result": result}


def test_modern_stdio_discovery_before_ambiguous_method(tmp_path):
    with stdio(
        tmp_path,
        server_loop(
            f"assert q['params']['_meta']['io.modelcontextprotocol/protocolVersion']=='{MODERN}'\n"
            " assert q['method'] not in {'initialize','notifications/initialized'}\n"
            f" r={DISCOVER!r} if q['method']=='server/discover' else {{'tools':[{TOOL!r}]}}"
        ),
    ) as client:
        assert client.tools() == [TOOL]
        assert client.protocol_version == MODERN
        assert client.protocol_era == "modern"
        process = client.process
    assert process.poll() is not None


@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
def test_all_legacy_versions_probe_then_initialize(tmp_path, version):
    with stdio(
        tmp_path,
        server_loop(
            f"r={{'protocolVersion':{version!r}}} if q['method']=='initialize' else {{'tools':[]}}"
        ),
    ) as client:
        assert client.protocol_version == version
        assert client.protocol_era == "legacy"
        assert client.tools() == []


@pytest.mark.parametrize("code", [-32020, -32021, -32022])
def test_recognized_modern_errors_never_initialize(tmp_path, code):
    log = tmp_path / "requests"
    program = f"""for line in sys.stdin:
 q=json.loads(line)
 with open({str(log)!r},'a') as out: out.write(q['method']+'\\n')
 print(json.dumps({{'jsonrpc':'2.0','id':q['id'],'error':{{'code':{code},'message':'secret diagnostics','data':{{'supported':['2025-11-25']}}}}}}),flush=True)
"""
    with pytest.raises(DomainError) as error, stdio(tmp_path, program):
        pass
    assert error.value.code == ErrorCode.CAPABILITY_UNAVAILABLE
    assert "secret diagnostics" not in str(error.value)
    assert log.read_text(encoding="utf-8").splitlines() == ["server/discover"]


def test_http_modern_metadata_headers_and_no_session_contract():
    def respond(q, headers):
        assert headers["MCP-Protocol-Version"] == MODERN
        assert headers["Mcp-Method"] == q["method"]
        assert "Mcp-Session-Id" not in headers
        assert q["params"]["_meta"] == {
            "io.modelcontextprotocol/protocolVersion": MODERN,
            "io.modelcontextprotocol/clientInfo": {"name": "devroster", "version": "0.4.8"},
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        assert "_meta" not in q
        return reply(
            q,
            DISCOVER
            if q["method"] == "server/discover"
            else {"tools": [TOOL]}
            if q["method"] == "tools/list"
            else {"content": []},
        )

    with http_fixture(respond) as (config, observed):
        with McpClient(config) as client:
            assert client.tools() == [TOOL]
            client.call("read", {})
        assert [q["method"] for q, _ in observed] == ["server/discover", "tools/list", "tools/call"]


@pytest.mark.parametrize("bad", [True, "1"])
def test_strict_http_request_id_type(bad):
    with (
        http_fixture(lambda q, h: (200, {"jsonrpc": "2.0", "id": bad, "result": DISCOVER})) as (
            config,
            _,
        ),
        pytest.raises(DomainError, match="ID"),
        McpClient(config),
    ):
        pass


@pytest.mark.parametrize("result_type", ["input_required", "task", "unknown"])
def test_unsupported_result_type_fails_closed(tmp_path, result_type):
    with (
        pytest.raises(DomainError) as error,
        stdio(
            tmp_path,
            server_loop(
                f"r={DISCOVER!r} if q['method']=='server/discover' else {{'resultType':{result_type!r},'tools':[]}}"
            ),
        ) as client,
    ):
        client.tools()
    assert error.value.code == ErrorCode.CAPABILITY_UNAVAILABLE


def test_http_valid_header_annotations_and_invalid_tools_excluded():
    good = {
        "name": "읽기",
        "inputSchema": {
            "type": "object",
            "properties": {
                "nested": {
                    "type": "object",
                    "properties": {
                        "region": {"type": "string", "x-mcp-header": "Region"},
                        "enabled": {"type": "boolean", "x-mcp-header": "Enabled"},
                        "count": {"type": "integer", "x-mcp-header": "Count"},
                        "missing": {"type": "string", "x-mcp-header": "Missing"},
                    },
                }
            },
        },
    }
    invalid = [
        {"type": "number", "x-mcp-header": "Number"},
        {"type": "string", "x-mcp-header": "bad header"},
        {"type": "array", "items": {"type": "string", "x-mcp-header": "Item"}},
        {"anyOf": [{"type": "string", "x-mcp-header": "Composed"}]},
        {
            "type": "object",
            "properties": {
                "a": {"type": "string", "x-mcp-header": "A"},
                "b": {"type": "string", "x-mcp-header": "a"},
            },
        },
    ]
    bad = [
        {"name": f"bad{i}", "inputSchema": {"type": "object", "properties": {"v": s}}}
        for i, s in enumerate(invalid)
    ]

    def respond(q, headers):
        if q["method"] == "tools/call":
            assert (
                headers["Mcp-Name"]
                == "=?base64?" + base64.b64encode("읽기".encode()).decode() + "?="
            )
            assert (
                headers["Mcp-Param-Region"]
                == "=?base64?" + base64.b64encode(" 한글\n".encode()).decode() + "?="
            )
            assert headers["Mcp-Param-Enabled"] == "true"
            assert headers["Mcp-Param-Count"] == "-42"
            assert "Mcp-Param-Missing" not in headers
        return reply(
            q,
            DISCOVER
            if q["method"] == "server/discover"
            else {"tools": [good, *bad]}
            if q["method"] == "tools/list"
            else {"content": []},
        )

    with http_fixture(respond) as (config, _), McpClient(config) as client:
        assert client.tools() == [good]
        client.call(
            "읽기",
            {"nested": {"region": " 한글\n", "enabled": True, "count": -42, "missing": None}},
        )
        with pytest.raises(DomainError):
            client.call("읽기", {"nested": {"count": 2**53}})


@pytest.mark.parametrize(
    "wire, text",
    [
        (b"not json\n", "JSON"),
        (b"[]\n", "객체"),
        (b"\xff\n", "UTF-8"),
        (b"{}", "framing"),
        (b"", "종료"),
        (b"x" * (MAX_BYTES + 1), "크기"),
    ],
)
def test_real_stdio_framing_errors_are_not_legacy_fallback(tmp_path, wire, text):
    # Reply to the first request, then exit. Framing corruption cannot start legacy initialize.
    with (
        pytest.raises(DomainError, match=text),
        stdio(
            tmp_path,
            f"sys.stdin.buffer.readline()\nsys.stdout.buffer.write({wire!r})\nsys.stdout.buffer.flush()\n",
        ),
    ):
        pass


def test_fragmented_utf8_ndjson_and_noisy_stderr(tmp_path):
    program = f"""for line in sys.stdin:
 q=json.loads(line)
 r={DISCOVER!r} if q['method']=='server/discover' else {{'tools':[{{'name':'한글'}}]}}
 sys.stderr.write('fixture-secret'*10000);sys.stderr.flush()
 raw=(json.dumps({{'jsonrpc':'2.0','id':q['id'],'result':r}},ensure_ascii=False)+'\\n').encode('utf-8')
 for byte in raw: sys.stdout.buffer.write(bytes([byte]));sys.stdout.buffer.flush()
"""
    with stdio(tmp_path, program, environment={"API_SECRET": "fixture-secret"}) as client:
        assert client.tools()[0]["name"] == "한글"
        assert "fixture-secret" not in client.transport.diagnostics
        assert len(client.transport.diagnostics) <= 4096
    assert all(not t.is_alive() for t in client.transport.threads)


def test_timeout_write_and_read_share_one_deadline(tmp_path):
    # Child consumes discover, responds modern, then stops reading before a pipe-sized write.
    program = f"""q=json.loads(sys.stdin.readline())
print(json.dumps({{'jsonrpc':'2.0','id':q['id'],'result':{DISCOVER!r}}}),flush=True)
time.sleep(30)
"""
    client = stdio(tmp_path, program, timeout=0.5)
    started = time.monotonic()
    with pytest.raises(DomainError), client:
        client.call("read", {"payload": "x" * 1_000_000})
    assert time.monotonic() - started < 2.5
    assert client.process.poll() is not None
    assert all(not thread.is_alive() for thread in client.transport.threads)


def test_bounded_full_queue_cleanup(tmp_path):
    program = f"""q=json.loads(sys.stdin.readline())
print(json.dumps({{'jsonrpc':'2.0','id':q['id'],'result':{DISCOVER!r}}}),flush=True)
while True: os.write(sys.stdout.fileno(),b' '*65536)
"""
    client = stdio(tmp_path, program)
    with client:
        limit = time.monotonic() + 1
        while client.transport.incoming.qsize() < 16 and time.monotonic() < limit:
            time.sleep(0.01)
        assert client.transport.incoming.qsize() == 16
    assert all(not thread.is_alive() for thread in client.transport.threads)


def test_cancel_in_flight_kills_process_and_threads(tmp_path):
    program = server_loop(f"r={DISCOVER!r} if q['method']=='server/discover' else {{'tools':[]}}")
    program = program.replace(" r=", " if q['method']=='tools/list': time.sleep(30)\n r=")
    client = stdio(tmp_path, program, timeout=5)
    client.__enter__()
    errors = []

    def blocked():
        try:
            client.tools()
        except DomainError as exc:
            errors.append(exc)

    thread = threading.Thread(target=blocked)
    thread.start()
    time.sleep(0.1)
    client.cancel()
    thread.join(2)
    assert not thread.is_alive()
    assert errors
    assert client.process.poll() is not None
    assert all(not worker.is_alive() for worker in client.transport.threads)


@pytest.mark.parametrize("early_root_exit", [False, True])
def test_child_grandchild_cleanup_even_after_root_exit(tmp_path, early_root_exit):
    # Descendants hold both inherited pipes open after the MCP root exits.
    child = tmp_path / "child.py"
    grand = tmp_path / "grand.py"
    grand.write_text(
        "import time,os,sys\nopen(sys.argv[1],'w').write(str(os.getpid()))\ntime.sleep(30)\n", encoding="utf-8"
    )
    grand_pid = tmp_path / "grand.pid"
    child_pid = tmp_path / "child.pid"
    child.write_text(
        f"import subprocess,sys,time,os\nopen({str(child_pid)!r},'w').write(str(os.getpid()))\nsubprocess.Popen([sys.executable,{str(grand)!r},{str(grand_pid)!r}])\ntime.sleep(30)\n", encoding="utf-8"
    )
    program = f"""q=json.loads(sys.stdin.readline())
subprocess.Popen([sys.executable,{str(child)!r}])
while not os.path.exists({str(grand_pid)!r}): time.sleep(.01)
print(json.dumps({{'jsonrpc':'2.0','id':q['id'],'result':{DISCOVER!r}}}),flush=True)
""" + ("sys.exit(0)\n" if early_root_exit else "time.sleep(30)\n")
    client = stdio(tmp_path, program)
    with client:
        if early_root_exit:
            client.process.wait(timeout=2)
    from agent_dev.adapters.process_guard import parent_alive

    for path in (child_pid, grand_pid):
        pid = int(path.read_text(encoding="utf-8"))
        limit = time.monotonic() + 2
        while parent_alive(pid) and time.monotonic() < limit:
            time.sleep(0.02)
        assert not parent_alive(pid)
    assert all(not worker.is_alive() for worker in client.transport.threads)


def test_connection_check_displays_negotiated_version(tmp_path, monkeypatch):
    from agent_dev.adapters.integrations import Integrations

    monkeypatch.setattr("agent_dev.adapters.integrations.remote_address", lambda *a, **kw: None)

    class FakeClient:
        protocol_version, protocol_era = MODERN, "modern"

        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def tools(self):
            return [TOOL]

    monkeypatch.setattr(Integrations, "_credentials", lambda *args: "fixture-token")
    app = Integrations(tmp_path, client_factory=FakeClient)
    app.configure(
        {
            "id": "fixture",
            "provider": "custom",
            "transport": "http",
            "endpoint": "https://example.com/mcp",
            "operations": {"read": {"tool": "read", "capability": "docs.read"}},
        }
    )
    result = app.check("fixture")
    assert result["protocol_version"] == MODERN
    assert result["protocol_era"] == "modern"
    assert MODERN in result["message"]


def test_windows_bootstrap_does_not_prefetch_provider_input(tmp_path):

    from agent_dev.adapters import stdio_transport

    provider = tmp_path / "echo.py"
    provider.write_text(
        "import os,sys\nos.write(sys.stdout.fileno(),os.read(sys.stdin.fileno(),1024))\n", encoding="utf-8"
    )
    completed = subprocess.run(
        [sys.executable, stdio_transport.__file__, sys.executable, str(provider)],
        input=b'\n{"method":"server/discover"}\n',
        capture_output=True,
        timeout=2,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout == b'{"method":"server/discover"}\n'


@pytest.mark.parametrize("code", [-32601, -32602, -32000])
@pytest.mark.parametrize("version", ["2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"])
def test_http_legacy_any_nonmodern_rpc_error_falls_back(code, version):
    def respond(q, headers):
        if q["method"] == "server/discover":
            return 400, {"jsonrpc": "2.0", "id": q["id"], "error": {"code": code}}
        if q["method"] == "initialize":
            return reply(q, {"protocolVersion": version})
        return 202, {}

    with http_fixture(respond) as (config, observed), McpClient(config) as client:
        assert client.protocol_era == "legacy"
        assert client.protocol_version == version
        assert [q["method"] for q, _ in observed] == [
            "server/discover",
            "initialize",
            "notifications/initialized",
        ]


@pytest.mark.parametrize("code", [-32020, -32021, -32022])
def test_http_modern_error_body_on_http400_never_initializes(code):
    def respond(q, headers):
        return 400, {
            "jsonrpc": "2.0",
            "id": q["id"],
            "error": {"code": code, "data": {"supported": ["2025-11-25"]}},
        }

    with (
        http_fixture(respond) as (config, observed),
        pytest.raises(DomainError) as error,
        McpClient(config),
    ):
        pass
    assert error.value.code == ErrorCode.CAPABILITY_UNAVAILABLE
    assert [q["method"] for q, _ in observed] == ["server/discover"]


def test_http_modern_version_error_retries_mutual_version_once():
    attempts = []

    def respond(q, headers):
        attempts.append(q)
        if len(attempts) == 1:
            return 400, {
                "jsonrpc": "2.0",
                "id": q["id"],
                "error": {"code": -32022, "data": {"supported": [MODERN, "2025-11-25"]}},
            }
        return reply(q, DISCOVER)

    with http_fixture(respond) as (config, observed), McpClient(config) as client:
        assert client.protocol_version == MODERN
        assert [q["method"] for q, _ in observed] == ["server/discover", "server/discover"]
        assert attempts[1]["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == MODERN


def test_stdio_modern_version_error_retries_mutual_version_once(tmp_path):
    program = f"""count=0
for line in sys.stdin:
 q=json.loads(line);count+=1
 assert q['method']=='server/discover'
 v={{'jsonrpc':'2.0','id':q['id']}}
 if count==1: v['error']={{'code':-32022,'data':{{'supported':[{MODERN!r}]}}}}
 else: v['result']={DISCOVER!r}
 print(json.dumps(v),flush=True)
"""
    with stdio(tmp_path, program) as client:
        assert client.protocol_era == "modern"


@pytest.mark.parametrize(
    "field, bad",
    [
        ("supportedVersions", ["2025-11-25"]),
        ("supportedVersions", "2026-07-28"),
        ("capabilities", []),
        ("ttlMs", -1),
        ("ttlMs", True),
        ("cacheScope", "shared"),
        ("resultType", "input_required"),
    ],
)
def test_malformed_modern_discovery_never_initializes(field, bad):
    discovery = dict(DISCOVER, **{field: bad})
    with (
        http_fixture(lambda q, h: reply(q, discovery)) as (config, observed),
        pytest.raises(DomainError),
        McpClient(config),
    ):
        pass
    assert [q["method"] for q, _ in observed] == ["server/discover"]


@pytest.mark.parametrize("value", [" padded ", "=?base64?literal?=", "a\tb", "a\r\nb", "한글"])
def test_http_header_sentinel_encoding(value):
    tool = {
        "name": value,
        "inputSchema": {
            "type": "object",
            "properties": {"value": {"type": "string", "x-mcp-header": "Value"}},
        },
    }

    def respond(q, headers):
        if q["method"] == "tools/call":
            expected = "=?base64?" + base64.b64encode(value.encode()).decode() + "?="
            assert headers["Mcp-Name"] == expected
            assert headers["Mcp-Param-Value"] == expected
        return reply(
            q,
            DISCOVER
            if q["method"] == "server/discover"
            else {"tools": [tool]}
            if q["method"] == "tools/list"
            else {"content": []},
        )

    with http_fixture(respond) as (config, _), McpClient(config) as client:
        client.call(value, {"value": value})


def test_header_mismatch_refreshes_annotations_once():
    calls, lists = [], []

    def respond(q, headers):
        if q["method"] == "server/discover":
            return reply(q, DISCOVER)
        if q["method"] == "tools/list":
            lists.append(q)
            return reply(
                q,
                {
                    "tools": [
                        {
                            "name": "read",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "region": {
                                        "type": "string",
                                        "x-mcp-header": "Old" if len(lists) == 1 else "Region",
                                    }
                                },
                            },
                        }
                    ]
                },
            )
        calls.append(headers)
        if len(calls) == 1:
            return 400, {"jsonrpc": "2.0", "id": q["id"], "error": {"code": -32020}}
        assert headers["Mcp-Param-Region"] == "seoul"
        assert "Mcp-Param-Old" not in headers
        return reply(q, {"content": []})

    with http_fixture(respond) as (config, _), McpClient(config) as client:
        client.call("read", {"region": "seoul"})
    assert len(calls) == len(lists) == 2


def test_header_refresh_cannot_bypass_approved_schema():
    lists, calls = [], []

    def respond(q, headers):
        if q["method"] == "server/discover":
            return reply(q, DISCOVER)
        if q["method"] == "tools/list":
            lists.append(q)
            return reply(
                q,
                {
                    "tools": [
                        {
                            "name": "read",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "region": {
                                        "type": "string",
                                        "enum": ["seoul"] if len(lists) == 1 else ["secret"],
                                        "x-mcp-header": "Region",
                                    }
                                },
                            },
                        }
                    ]
                },
            )
        calls.append(q)
        return 400, {"jsonrpc": "2.0", "id": q["id"], "error": {"code": -32020}}

    with (
        http_fixture(respond) as (config, _),
        McpClient(config) as client,
        pytest.raises(DomainError) as error,
    ):
        client.call("read", {"region": "seoul"})
    assert error.value.code == ErrorCode.STALE_VERSION
    assert len(calls) == 1


def test_http_cancel_interrupts_active_response():
    ready = threading.Event()

    def respond(q, headers):
        if q["method"] == "server/discover":
            return reply(q, DISCOVER)
        ready.set()
        time.sleep(3)
        return reply(q, {"tools": []})

    with http_fixture(respond) as (config, _), McpClient(config, timeout=5) as client:
        errors = []

        def call():
            try:
                client.tools()
            except DomainError as exc:
                errors.append(exc)

        worker = threading.Thread(target=call)
        worker.start()
        assert ready.wait(1)
        client.cancel()
        worker.join(0.5)
        assert not worker.is_alive()
        assert errors


def test_protocol_cache_is_bounded_credential_isolated_and_config_sensitive():
    from agent_dev.adapters.mcp_client import _CACHE

    discovery = dict(DISCOVER, ttlMs=60_000)

    def respond(q, headers):
        return reply(q, discovery if q["method"] == "server/discover" else {"tools": []})

    with http_fixture(respond) as (config, observed):
        for version, credential in [
            (1, "private-token-A"),
            (1, "private-token-A"),
            (1, "private-token-B"),
            (2, "private-token-B"),
        ]:
            with McpClient(dict(config, version=version), credential=credential) as client:
                client.tools()
        assert [q["method"] for q, _ in observed].count("server/discover") == 3
        assert "private-token" not in str(_CACHE)
        assert len(_CACHE) <= 128


def test_stdio_silent_probe_falls_back_with_remaining_deadline(tmp_path):
    program = """for line in sys.stdin:
 q=json.loads(line)
 if q['method']=='server/discover' or 'id' not in q: continue
 print(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':{'protocolVersion':'2025-11-25'}}),flush=True)
"""
    started = time.monotonic()
    with stdio(tmp_path, program, timeout=1) as client:
        assert client.protocol_era == "legacy"
    assert time.monotonic() - started < 1


def test_utf8_response_limit_counts_wire_bytes_not_ascii_reencoding(tmp_path):
    program = server_loop(
        f"r={DISCOVER!r} if q['method']=='server/discover' else {{'content':[{{'type':'text','text':'한'*400000}}]}}"
    )
    with stdio(tmp_path, program) as client:
        assert len(client.call("read", {})["content"][0]["text"]) == 400000


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_never_legacy_fallback(status):
    with (
        http_fixture(lambda q, h: (status, {})) as (config, observed),
        pytest.raises(DomainError) as error,
        McpClient(config),
    ):
        pass
    assert error.value.code == ErrorCode.AUTH_REQUIRED
    assert [q["method"] for q, _ in observed] == ["server/discover"]


def test_real_stderr_diagnostics_redact_fragmented_secrets_and_remain_bounded(tmp_path):
    program = server_loop(f"r={DISCOVER!r} if q['method']=='server/discover' else {{'tools':[]}}")
    program = program.replace(
        " q=json.loads(line)",
        " sys.stderr.write('prefix fixture-');sys.stderr.flush();time.sleep(.01)\n sys.stderr.write('secret token=unregistered-value\\n');sys.stderr.flush()\n q=json.loads(line)",
    )
    with stdio(tmp_path, program, environment={"API_SECRET": "fixture-secret"}) as client:
        client.tools()
        limit = time.monotonic() + 1
        while "prefix" not in client.transport.diagnostics and time.monotonic() < limit:
            time.sleep(0.01)
        assert "prefix" in client.transport.diagnostics
        assert "fixture-secret" not in client.transport.diagnostics
        assert "unregistered-value" not in client.transport.diagnostics
        assert len(client.transport.diagnostics) <= 4096


@pytest.mark.parametrize(
    "keyword",
    [
        "items",
        "anyOf",
        "oneOf",
        "allOf",
        "not",
        "$defs",
        "if",
        "then",
        "else",
        "additionalProperties",
    ],
)
def test_annotations_outside_static_properties_exclude_tool(keyword):
    annotated = {"type": "string", "x-mcp-header": "Unsafe"}
    schema = {
        "type": "object",
        keyword: [annotated]
        if keyword in {"anyOf", "oneOf", "allOf"}
        else {"value": annotated}
        if keyword == "$defs"
        else annotated,
    }
    bad = {"name": "unsafe", "inputSchema": schema}

    def respond(q, headers):
        return reply(q, DISCOVER if q["method"] == "server/discover" else {"tools": [bad, TOOL]})

    with http_fixture(respond) as (config, _), McpClient(config) as client:
        assert client.tools() == [TOOL]


def test_unresolved_ref_annotation_does_not_expand_schema():
    bad = {
        "name": "unsafe",
        "inputSchema": {
            "type": "object",
            "properties": {"v": {"$ref": {"type": "string", "x-mcp-header": "Ref"}}},
        },
    }

    def respond(q, headers):
        return reply(q, DISCOVER if q["method"] == "server/discover" else {"tools": [bad, TOOL]})

    with http_fixture(respond) as (config, _), McpClient(config) as client:
        assert client.tools() == [TOOL]


def test_modern_stdio_server_requests_never_executed(tmp_path):
    program = """q=json.loads(sys.stdin.readline())
print(json.dumps({'jsonrpc':'2.0','id':100,'method':'sampling/createMessage','params':{}}),flush=True)
time.sleep(30)
"""
    with pytest.raises(DomainError) as error, stdio(tmp_path, program):
        pass
    assert error.value.code == ErrorCode.CAPABILITY_UNAVAILABLE


def test_stdio_bool_response_id_cannot_match_integer(tmp_path):
    program = f"""sys.stdin.readline()
print(json.dumps({{'jsonrpc':'2.0','id':True,'result':{DISCOVER!r}}}),flush=True)
time.sleep(30)
"""
    with pytest.raises(DomainError, match="ID"), stdio(tmp_path, program):
        pass


@pytest.mark.parametrize("sse", [False, True])
def test_modern_http_utf8_and_sse_correlation(sse):
    from agent_dev.adapters.mcp_client import request_remote

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            # A bool ID must never be accepted as integer1, including SSE correlation.
            body = (
                b'data: {"jsonrpc":"2.0","id":true,"result":{}}\n\n'
                if sse
                else b'{"id":1,"result":"\xff"}'
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(DomainError):
            request_remote(
                f"http://127.0.0.1:{server.server_port}",
                local=True,
                method="POST",
                payload={"id": 1},
                timeout=1,
            )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_discovery_cache_expires_and_compatibility_failure_invalidates():
    from agent_dev.adapters.mcp_client import _CACHE

    errors = []
    discovery = dict(DISCOVER, ttlMs=60_000)

    def respond(q, headers):
        if q["method"] == "tools/list" and not errors:
            errors.append(q)
            return 400, {"jsonrpc": "2.0", "id": q["id"], "error": {"code": -32021}}
        return reply(q, discovery if q["method"] == "server/discover" else {"tools": []})

    with http_fixture(respond) as (config, observed):
        with McpClient(config) as client, pytest.raises(DomainError):
            client.tools()
        assert client.cache_key not in _CACHE
        with McpClient(config) as client:
            client.tools()
        assert [q["method"] for q, _ in observed].count("server/discover") == 2


def test_tools_and_discovery_share_one_stdio_deadline(tmp_path):
    program = server_loop(f"r={DISCOVER!r} if q['method']=='server/discover' else {{'tools':[]}}")
    program = program.replace(
        " q=json.loads(line)",
        " q=json.loads(line)\n time.sleep(.05 if q['method']=='server/discover' else .6)",
    )
    started = time.monotonic()
    client = stdio(tmp_path, program, timeout=1)
    with client, pytest.raises(DomainError):
        client.tools()
        client.tools()
    assert time.monotonic() - started < 2
    assert client.process.poll() is not None


def test_header_refresh_preserves_property_named_like_annotation():
    lists, calls = [], []

    def respond(q, headers):
        if q["method"] == "server/discover":
            return reply(q, DISCOVER)
        if q["method"] == "tools/list":
            lists.append(q)
            return reply(
                q,
                {
                    "tools": [
                        {
                            "name": "read",
                            "inputSchema": {
                                "type": "object",
                                "properties": {
                                    "x-mcp-header": {
                                        "type": "string",
                                        "enum": ["old"] if len(lists) == 1 else ["new"],
                                    }
                                },
                            },
                        }
                    ]
                },
            )
        calls.append(q)
        return 400, {"jsonrpc": "2.0", "id": q["id"], "error": {"code": -32020}}

    with (
        http_fixture(respond) as (config, _),
        McpClient(config) as client,
        pytest.raises(DomainError) as error,
    ):
        client.call("read", {"x-mcp-header": "old"})
    assert error.value.code == ErrorCode.STALE_VERSION
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows kernel Job ownership")
def test_windows_parent_crash_closes_job_and_kills_descendants(tmp_path):
    pid_file = tmp_path / "pid"
    provider = tmp_path / "provider.py"
    provider.write_text(f"""import sys,json,os,time
open({str(pid_file)!r},'w').write(str(os.getpid()))
for line in sys.stdin:
 q=json.loads(line)
 print(json.dumps({{'jsonrpc':'2.0','id':q['id'],'result':{DISCOVER!r}}}),flush=True)
 time.sleep(30)
""", encoding="utf-8")
    from agent_dev.adapters import mcp_client

    package_root = str(__import__("pathlib").Path(mcp_client.__file__).parents[2])
    code = f"""import os,sys
sys.path.insert(0,{package_root!r})
from agent_dev.adapters.mcp_client import McpClient
client=McpClient({{'transport':'stdio','command':[{sys.executable!r},{str(provider)!r}]}},timeout=3)
client.__enter__()
os._exit(0)
"""
    completed = subprocess.run([sys.executable, "-c", code], timeout=5, check=False)
    assert completed.returncode == 0
    from agent_dev.adapters.process_guard import parent_alive

    pid = int(pid_file.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while parent_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not parent_alive(pid)


def test_modern_version_error_on_operation_retries_once_without_initializing():
    requests = []

    def respond(q, headers):
        requests.append(q["method"])
        if q["method"] == "tools/list" and requests.count("tools/list") == 1:
            return 400, {
                "jsonrpc": "2.0",
                "id": q["id"],
                "error": {"code": -32022, "data": {"supported": [MODERN]}},
            }
        return reply(q, DISCOVER if q["method"] == "server/discover" else {"tools": [TOOL]})

    with http_fixture(respond) as (config, _), McpClient(config) as client:
        assert client.tools() == [TOOL]
    assert requests == ["server/discover", "tools/list", "tools/list"]


def test_blind_legacy_initialize_success_is_not_discovery(tmp_path):
    program = server_loop("r={'protocolVersion':'2025-11-25','capabilities':{}}")
    with stdio(tmp_path, program) as client:
        assert client.protocol_era == "legacy"
        assert client.protocol_version == "2025-11-25"


def test_discovery_huge_finite_ttl_is_bounded_without_numeric_overflow():
    discovery = dict(DISCOVER, ttlMs=10**1000)
    with http_fixture(lambda q, h: reply(q, discovery)) as (config, _), McpClient(config) as client:
        assert client.protocol_version == MODERN


def test_cancel_wakes_callers_waiting_on_full_writer_queue(tmp_path):
    program = f"""q=json.loads(sys.stdin.readline())
print(json.dumps({{'jsonrpc':'2.0','id':q['id'],'result':{DISCOVER!r}}}),flush=True)
time.sleep(30)
"""
    client = stdio(tmp_path, program, timeout=5)
    errors = []

    def send():
        try:
            client.call("read", {"value": "x" * 1_000_000})
        except DomainError as exc:
            errors.append(exc)

    workers = [threading.Thread(target=send) for _ in range(3)]
    try:
        with client:
            for worker in workers:
                worker.start()
                time.sleep(0.04)
            client.cancel()
            for worker in workers:
                worker.join(0.3)
            assert all(not worker.is_alive() for worker in workers)
            assert len(errors) == 3
    finally:
        client.close()
        for worker in workers:
            worker.join(6)


def test_expired_budget_does_not_spawn_stdio_provider(tmp_path, monkeypatch):
    from agent_dev.adapters import stdio_transport

    original = stdio_transport.subprocess.Popen
    spawned = []

    def spawn(*args, **kwargs):
        spawned.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(stdio_transport.subprocess, "Popen", spawn)
    client = stdio(tmp_path, "time.sleep(30)\n")
    client.deadline = time.monotonic() - 1
    with pytest.raises(DomainError), client:
        pass
    assert not spawned


def test_http_silent_body_probe_times_out_then_initializes_with_remaining_budget():
    methods = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            q = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            methods.append(q["method"])
            if q["method"] == "server/discover":
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                time.sleep(1)
                return
            value = {
                "jsonrpc": "2.0",
                "id": q.get("id"),
                "result": {"protocolVersion": "2025-11-25"},
            }
            raw = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = {
            "transport": "http",
            "local": True,
            "endpoint": f"http://127.0.0.1:{server.server_port}",
        }
        with McpClient(config, timeout=1) as client:
            assert client.protocol_era == "legacy"
        assert methods == ["server/discover", "initialize", "notifications/initialized"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_stdio_json_nesting_overflow_is_normalized(tmp_path):
    raw = b'{"nest":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}\n"
    with (
        pytest.raises(DomainError, match="JSON"),
        stdio(
            tmp_path,
            f"sys.stdin.buffer.readline()\nsys.stdout.buffer.write({raw!r})\nsys.stdout.buffer.flush()\n",
        ),
    ):
        pass


def test_stdio_accepts_message_at_exact_two_mib_limit(tmp_path):
    program = f"""for line in sys.stdin:
 q=json.loads(line)
 r={DISCOVER!r} if q['method']=='server/discover' else {{'content':[{{'text':''}}]}}
 value={{'jsonrpc':'2.0','id':q['id'],'result':r}}
 if q['method']=='tools/call':
  overhead=len(json.dumps(value).encode())
  r['content'][0]['text']='x'*({MAX_BYTES}-overhead)
 os.write(sys.stdout.fileno(),json.dumps(value).encode()+b'\\n')
"""
    with stdio(tmp_path, program) as client:
        assert len(client.call("read", {})["content"][0]["text"]) > MAX_BYTES - 100


@pytest.mark.parametrize("status", [400, 404, 405])
@pytest.mark.parametrize(
    "body",
    [
        b"",
        {},
        {"message": "initialize required"},
        {"id": 1, "message": "initialize required"},
        {"error": {"code": -32600}},
    ],
)
def test_http_probe_legacy_rejection_falls_back_before_rpc_id_validation(status, body):
    def respond(q, headers):
        if q["method"] == "server/discover":
            return status, body
        if q["method"] == "initialize":
            return reply(q, {"protocolVersion": "2025-11-25"})
        return 202, {}

    with http_fixture(respond) as (config, observed), McpClient(config) as client:
        assert client.protocol_era == "legacy"
        assert client.protocol_version == "2025-11-25"
        assert [q["method"] for q, _ in observed] == [
            "server/discover",
            "initialize",
            "notifications/initialized",
        ]


@pytest.mark.parametrize("body", [b"not json", b"\xff", b"[]"])
def test_http_probe_400_framing_corruption_never_initializes(body):
    with (
        http_fixture(lambda q, h: (400, body)) as (config, observed),
        pytest.raises(DomainError),
        McpClient(config),
    ):
        pass
    assert [q["method"] for q, _ in observed] == ["server/discover"]


@pytest.mark.parametrize("status", [401, 403])
def test_empty_http_auth_rejection_never_initializes(status):
    with (
        http_fixture(lambda q, h: (status, b"")) as (config, observed),
        pytest.raises(DomainError) as error,
        McpClient(config),
    ):
        pass
    assert error.value.code == ErrorCode.AUTH_REQUIRED
    assert [q["method"] for q, _ in observed] == ["server/discover"]


@pytest.mark.parametrize("code", [-32020, -32021, -32022])
def test_http_probe_recognized_modern_errors_without_id_never_initialize(code):
    with (
        http_fixture(lambda q, h: (400, {"error": {"code": code}})) as (config, observed),
        pytest.raises(DomainError),
        McpClient(config),
    ):
        pass
    assert [q["method"] for q, _ in observed] == ["server/discover"]


@pytest.mark.parametrize("bad_id", [True, "1", 99])
def test_http_probe_400_response_id_type_and_correlation_remain_strict(bad_id):
    value = {"jsonrpc": "2.0", "id": bad_id, "error": {"code": -32601}}
    with (
        http_fixture(lambda q, h: (400, value)) as (config, observed),
        pytest.raises(DomainError, match="ID"),
        McpClient(config),
    ):
        pass
    assert [q["method"] for q, _ in observed] == ["server/discover"]


@pytest.mark.parametrize("scope", [[], {}])
@pytest.mark.parametrize("transport", ["http", "stdio"])
def test_invalid_discovery_cache_scope_type_is_controlled_error(tmp_path, transport, scope):
    discovery = dict(DISCOVER, cacheScope=scope)
    if transport == "stdio":
        with pytest.raises(DomainError) as error, stdio(tmp_path, server_loop(f"r={discovery!r}")):
            pass
    else:
        with (
            http_fixture(lambda q, h: reply(q, discovery)) as (config, observed),
            pytest.raises(DomainError) as error,
            McpClient(config),
        ):
            pass
        assert [q["method"] for q, _ in observed] == ["server/discover"]
    assert error.value.code == ErrorCode.CAPABILITY_UNAVAILABLE


@pytest.mark.parametrize("scope", [[], {}])
def test_connection_check_records_invalid_discovery_cache_scope(tmp_path, monkeypatch, scope):
    from agent_dev.adapters.integrations import Integrations

    monkeypatch.setattr(Integrations, "_credentials", lambda *args: "fixture-token")
    with http_fixture(lambda q, h: reply(q, dict(DISCOVER, cacheScope=scope))) as (
        config,
        observed,
    ):
        app = Integrations(tmp_path)
        app.configure(
            dict(
                config,
                id="scopecheck",
                provider="custom",
                operations={"read": {"tool": "read", "capability": "docs.read"}},
            )
        )
        checked = app.check("scopecheck")
        assert checked["state"] == "error"
        assert "DiscoverResult" in checked["message"]
        assert app.show("scopecheck")["state"] == "error"
        assert [q["method"] for q, _ in observed] == ["server/discover"]


@pytest.mark.parametrize("version", [[], {}])
def test_invalid_legacy_protocol_version_type_is_controlled_error(tmp_path, version):
    program = server_loop(
        f"r={{'protocolVersion':{version!r}}} if q['method']=='initialize' else {{'tools':[]}}"
    )
    with pytest.raises(DomainError) as error, stdio(tmp_path, program):
        pass
    assert error.value.code == ErrorCode.CAPABILITY_UNAVAILABLE


@pytest.mark.parametrize(
    "message",
    [
        {"jsonrpc": "2.0", "id": 999, "result": DISCOVER},
        {"foo": 1},
        {"jsonrpc": "2.0", "id": 999, "error": {"code": -32021}},
        {"jsonrpc": "2.0", "id": "1", "result": DISCOVER},
        {"jsonrpc": "2.0", "id": None, "result": DISCOVER},
        {"jsonrpc": "2.0", "result": DISCOVER},
        {"jsonrpc": "1.0", "method": "notifications/message"},
        {"jsonrpc": "2.0", "method": 10},
        {"jsonrpc": "2.0", "method": "notifications/message", "params": []},
        {"jsonrpc": "2.0", "id": 999, "result": {}, "error": {"code": -32601}},
        {"jsonrpc": "2.0", "id": 999, "error": "broken error"},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": []}},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": True}},
        {"jsonrpc": "2.0", "id": 1, "error": {}},
        {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": {}}},
    ],
)
def test_stdio_malformed_envelopes_and_unknown_ids_never_fall_back(tmp_path, message):
    log = tmp_path / "requests"
    program = f"""for line in sys.stdin:
 q=json.loads(line)
 with open({str(log)!r},'a') as out: out.write(q['method']+'\\n')
 if q['method']=='server/discover': v={message!r}
 elif q['method']=='initialize': v={{'jsonrpc':'2.0','id':q['id'],'result':{{'protocolVersion':'2025-11-25'}}}}
 else: continue
 print(json.dumps(v),flush=True)
"""
    # A discovery timeout legitimately falls back to initialize; under CI load the fixture
    # server's start alone took over a second, so leave room for it.
    with pytest.raises(DomainError) as error, stdio(tmp_path, program, timeout=5):
        pass
    assert error.value.code == ErrorCode.INVALID_INPUT
    assert log.read_text(encoding="utf-8").splitlines() == ["server/discover"]


def test_stdio_valid_notifications_remain_allowed_before_result(tmp_path):
    program = server_loop(f"r={DISCOVER!r} if q['method']=='server/discover' else {{'tools':[]}}")
    program = program.replace(
        " if 'id' not in q: continue",
        " if 'id' not in q: continue\n print(json.dumps({'jsonrpc':'2.0','method':'notifications/message','params':{'data':'status'}}),flush=True)",
    )
    with stdio(tmp_path, program) as client:
        assert client.protocol_era == "modern"
        assert client.tools() == []


@pytest.mark.parametrize("late_id", [1, "1", True])
def test_stdio_late_known_abandoned_probe_response_requires_exact_id_type(tmp_path, late_id):
    log = tmp_path / "requests"
    program = f"""for line in sys.stdin:
 q=json.loads(line)
 with open({str(log)!r},'a') as out: out.write(q['method']+'\\n')
 if q['method']=='server/discover': continue
 if q['method']=='initialize':
  print(json.dumps({{'jsonrpc':'2.0','id':{late_id!r},'error':{{'code':-32601}}}}),flush=True)
  r={{'protocolVersion':'2025-11-25'}}
 elif q['method']=='tools/list': r={{'tools':[]}}
 else: continue
 print(json.dumps({{'jsonrpc':'2.0','id':q['id'],'result':r}}),flush=True)
"""
    if type(late_id) is int:
        with stdio(tmp_path, program, timeout=2) as client:
            assert client.protocol_era == "legacy"
            assert client.tools() == []
    else:
        with pytest.raises(DomainError, match="ID"), stdio(tmp_path, program, timeout=2):
            pass
        assert "tools/list" not in log.read_text(encoding="utf-8")


def test_stdio_completed_response_replay_is_not_abandoned(tmp_path):
    program = f"""first=None
for line in sys.stdin:
 q=json.loads(line)
 if q['method']=='server/discover':
  first={{'jsonrpc':'2.0','id':q['id'],'result':{DISCOVER!r}}}
  print(json.dumps(first),flush=True)
 elif q['method']=='tools/list':
  print(json.dumps(first),flush=True)
  print(json.dumps({{'jsonrpc':'2.0','id':q['id'],'result':{{'tools':[]}}}}),flush=True)
"""
    with stdio(tmp_path, program) as client, pytest.raises(DomainError, match="ID"):
        client.tools()


def test_stdio_consumed_abandoned_response_cannot_be_replayed(tmp_path):
    program = """for line in sys.stdin:
 q=json.loads(line)
 if q['method']=='server/discover' or 'id' not in q: continue
 print(json.dumps({'jsonrpc':'2.0','id':1,'error':{'code':-32601}}),flush=True)
 r={'protocolVersion':'2025-11-25'} if q['method']=='initialize' else {'tools':[]}
 print(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':r}),flush=True)
"""
    with stdio(tmp_path, program, timeout=2) as client, pytest.raises(DomainError, match="ID"):
        client.tools()


def test_stdio_abandoned_id_knowledge_is_bounded_and_evicted_ids_rejected(tmp_path):
    program = f"""count=0
for line in sys.stdin:
 q=json.loads(line)
 if q['method']=='server/discover':
  count+=1
  if count>1: continue
  print(json.dumps({{'jsonrpc':'2.0','id':q['id'],'result':{DISCOVER!r}}}),flush=True)
 elif q['method']=='tools/list':
  print(json.dumps({{'jsonrpc':'2.0','id':2,'result':{{}}}}),flush=True)
  print(json.dumps({{'jsonrpc':'2.0','id':q['id'],'result':{{'tools':[]}}}}),flush=True)
"""
    with stdio(tmp_path, program, timeout=5) as client:
        for _ in range(18):
            with pytest.raises(DomainError):
                client.rpc("server/discover", {}, _deadline=time.monotonic() + 0.05, _probe=True)
        assert len(client._abandoned_ids) == 16
        with pytest.raises(DomainError, match="ID"):
            client.tools()
