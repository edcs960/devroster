"""Owned browser behavior regressions for existing product screen corrections."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_project_journey import prepared, workspace
from test_shared_styles import running_server

from agent_dev.ui.studio_server import make_server


def test_product_screen_behavior_with_installed_browser(tmp_path):
    module = os.environ.get("AGENT_DEV_TEST_PLAYWRIGHT_MODULE")
    executable = os.environ.get("AGENT_DEV_TEST_BROWSER_EXECUTABLE")
    if not shutil.which("node") or not module or not executable:
        pytest.skip("Opt in with an installed Playwright module and browser; no auto install.")
    if not Path(module).exists() or not Path(executable).is_file():
        pytest.skip("Configured installed browser runtime unavailable.")
    fixture = workspace.__wrapped__(tmp_path)  # pyright: ignore[reportAttributeAccessIssue]
    prepared(fixture)
    server = make_server(fixture[0], port=0)
    try:
        with running_server(server):
            result = subprocess.run(
                ["node", str(Path(__file__).with_name("task3c_ui_behavior.cjs")),
                 f"http://127.0.0.1:{server.server_port}", module, executable,
                 str(tmp_path / "browser-evidence")],
                capture_output=True, text=True, timeout=90, check=False,
            )
            assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.studio.close()
