"""Real browser/API/temporary SQLite flows for AUD-04/05/06/09/12."""
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from test_reusable_workspace import approved, workspace

from agent_dev.ui.studio_server import make_server


def test_logic_audit_browser(workspace, tmp_path, monkeypatch):
    module = os.environ.get('AGENT_DEV_TEST_PLAYWRIGHT_MODULE')
    executable = os.environ.get('AGENT_DEV_TEST_BROWSER_EXECUTABLE')
    if not module or not executable or not shutil.which('node'):
        pytest.skip('Opt in to installed Playwright/browser; no installation.')
    monkeypatch.setenv('DEVROSTER_HOME', str(tmp_path / 'app-home'))
    runtime = approved(workspace)
    runtime.store.create_plan('EXAMPLE', 'PLAN2', 'D', 1, [{'id':'T2','title':'Second audit task','system_name':'Example'}])
    runtime.store.approve('plan', 'PLAN2', 1, 'user')
    server = make_server(workspace, port=0)
    studio = server.studio.studio.create('Audit Studio', 'Local audit fixture')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(['node', str(Path(__file__).with_name('logic_audit_browser.cjs')), module, executable,
                                 f'http://127.0.0.1:{server.server_port}', studio['project']['id']],
                                capture_output=True,text=True,timeout=90)
        assert result.returncode == 0, result.stdout + result.stderr
        print(result.stdout)
    finally:
        server.shutdown()
        server.studio.close()
        server.server_close()
        thread.join(timeout=5)
