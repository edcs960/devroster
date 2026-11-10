"""Diagram documents: the prompt asks for Mermaid, and the studio shows its source safely."""

import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from test_studio import BRIEF, fixture_generator, specifications

from agent_dev.adapters.studio_generation import DIAGRAMS, prompt_for
from agent_dev.domain.studio import DOCUMENTS
from agent_dev.ui.studio_server import make_server

SEQUENCE = "sequenceDiagram\n  participant U as 사용자\n  participant S as 서버\n  U->>S: 메모 저장\n  S-->>U: 결과"


def test_diagram_documents_are_asked_for_mermaid_blocks():
    prompt = prompt_for("spec_documents", {"brief": BRIEF, "documents_to_write": ["시퀀스다이어그램", "용어사전"]})
    assert "```mermaid" in prompt and '"시퀀스다이어그램":"sequenceDiagram"' in prompt
    assert "용어사전" not in prompt.split("```mermaid", 1)[1].split("Schema:", 1)[0]
    assert "```mermaid" not in prompt_for("spec_documents", {"brief": BRIEF, "documents_to_write": ["용어사전"]})
    whole = prompt_for("specs", {"brief": BRIEF})
    assert all(f'"{name}"' in whole for name in DIAGRAMS) and set(DIAGRAMS) <= set(DOCUMENTS)


def test_studio_shows_mermaid_source_with_a_copy_button(tmp_path):
    module = os.environ.get("AGENT_DEV_TEST_PLAYWRIGHT_MODULE")
    executable = os.environ.get("AGENT_DEV_TEST_BROWSER_EXECUTABLE")
    if not module or not executable or not shutil.which("node"):
        pytest.skip("Opt in to installed Playwright/browser; no installation.")
    server = make_server(tmp_path, 0, fixture_generator)
    studio = server.studio.studio
    project = studio.create("다이어그램", BRIEF)["project"]["id"]
    specs = specifications()
    specs["documents"]["시퀀스다이어그램"] = (
        "# 시퀀스다이어그램\n\nREQ-001 SCR-form\n\n```mermaid\n" + SEQUENCE + "\n```\n\n| 단계 | 설명 |\n|---|---|\n| 저장 | 메모를 저장한다. |"
    )
    with studio.store.transaction() as db:
        studio.store.insert_revision(db, "specs", project, specs, BRIEF)
        studio.store.bump(db, project, "spec.generated", {})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("mermaid_view_browser.cjs")), module, executable,
             f"http://127.0.0.1:{server.server_port}", project],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        server.shutdown()
        server.studio.close()
        server.server_close()
        thread.join(timeout=5)
