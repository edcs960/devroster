"""Web servers start on every supported OS; source reads report their capability.

Runs in the portable CI matrix (Linux, macOS, native Windows).
"""

import http.client
import json
import threading

from agent_dev.adapters.source_explorer import safe_reads_supported
from agent_dev.adapters.workspace import initialize
from agent_dev.ui.setup_server import make_server as setup_server
from agent_dev.ui.studio_server import make_server as studio_server


def fetch(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    connection.request("GET", path)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, body


def serve(factory, root):
    server = factory(root, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def test_setup_and_studio_start_and_source_reports_capability(tmp_path):
    initialize(tmp_path)
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src/app.py").write_text("print('hello')\n", encoding="utf-8")
    setup = serve(setup_server, tmp_path)
    studio = serve(studio_server, tmp_path)
    try:
        assert fetch(setup.server_address[1], "/setup")[0] == 200
        port = studio.server_address[1]
        assert fetch(port, "/home")[0] == 200
        status, body = fetch(port, "/api/source/bootstrap")
        data = json.loads(body)
        assert status == 200 and data["available"] is safe_reads_supported()
        status, body = fetch(port, "/api/source/search?module=source&query=hello&mode=content")
        result = json.loads(body)
        if safe_reads_supported():
            assert status == 200 and result["results"][0]["path"] == "src/app.py"
        else:
            assert status == 400 and result["error"] == data["unavailable_reason"]
        assert not (tmp_path / ".agent-dev/source-search.key").exists()
    finally:
        for server in (setup, studio):
            server.shutdown()
            server.server_close()
