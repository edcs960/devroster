import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_dev.adapters.mcp_client import McpClient, remote_address
from agent_dev.domain.errors import DomainError


@pytest.mark.parametrize("sse", [False, True])
def test_http_handshake_pagination_call_and_session_close(sse):
    observed = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_DELETE(self):
            observed.append(("DELETE", self.headers.get("Mcp-Session-Id")))
            self.send_response(204)
            self.end_headers()

        def do_POST(self):
            value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            observed.append(value["method"])
            if "id" not in value:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if value['method'] == 'server/discover':
                answer = json.dumps({'jsonrpc': '2.0', 'id': value['id'],
                                     'error': {'code': -32601, 'message': 'Unknown method'}}).encode()
                self.send_response(400)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(answer)))
                self.end_headers()
                self.wfile.write(answer)
                return
            if value["method"] == "initialize":
                result = {"protocolVersion": "2025-06-18", "capabilities": {}}
            elif value["method"] == "tools/list":
                result = (
                    {"tools": [{"name": "read", "inputSchema": {"type": "object"}}]}
                    if value.get("params", {}).get("cursor")
                    else {"tools": [], "nextCursor": "next"}
                )
            else:
                assert self.headers["Mcp-Session-Id"] == "session-1"
                result = {"content": [{"type": "text", "text": "hello"}]}
            answer = json.dumps({"jsonrpc": "2.0", "id": value["id"], "result": result})
            body = (
                ('data: {"jsonrpc":"2.0","id":-1,"result":{}}\n\ndata: ' + answer + "\n\n").encode()
                if sse
                else answer.encode()
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream" if sse else "application/json")
            self.send_header("Mcp-Session-Id", "session-1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with McpClient(
            {
                "transport": "http",
                "endpoint": f"http://127.0.0.1:{server.server_port}/mcp",
                "local": True,
            }
        ) as client:
            assert client.tools()[0]["name"] == "read"
            assert client.call("read", {})["content"][0]["text"] == "hello"
        assert observed == [
            "server/discover",
            "initialize",
            "notifications/initialized",
            "tools/list",
            "tools/list",
            "tools/call",
            ("DELETE", "session-1"),
        ]
    finally:
        server.shutdown()
        server.server_close()


def test_stdio_and_process_cleanup(tmp_path):
    program = tmp_path / "fixture.py"
    program.write_text("""import json,sys,os
for line in sys.stdin:
    q=json.loads(line)
    if 'id' not in q: continue
    r={'protocolVersion':'2025-06-18'} if q['method']=='initialize' else {'tools':[{'name':'read','inputSchema':{'type':'object'}}]} if q['method']=='tools/list' else {'content':[{'type':'text','text':os.environ.get('UNREGISTERED_SECRET','absent')}]}
    print(json.dumps({'jsonrpc':'2.0','id':q['id'],'result':r}),flush=True)
""", encoding="utf-8")
    with McpClient(
        {"transport": "stdio", "command": [sys.executable, str(program)]}, cwd=tmp_path
    ) as client:
        process = client.process
        assert client.tools()[0]["name"] == "read"
        assert client.call("read", {})["content"][0]["text"] == "absent"
    assert process.poll() is not None


def test_private_remote_and_invalid_local_rejected():
    for url, local in [
        ("https://127.0.0.1", False),
        ("http://example.com", True),
        ("https://user:secret@example.com", False),
    ]:
        with pytest.raises(DomainError):
            remote_address(url, local=local)
