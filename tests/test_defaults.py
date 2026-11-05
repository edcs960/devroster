import importlib.util
from pathlib import Path

import pytest

from agent_dev import defaults

RULE_DIRS = ("roles", "phases", "skills", "prompts")


def bundled_rule_files():
    yield defaults.DEFAULTS_DIR / "AGENTS.md"
    for name in RULE_DIRS:
        yield from sorted((defaults.DEFAULTS_DIR / name).rglob("*.md"))




def test_designer_is_canonical_and_design_is_not_bundled():
    assert not (defaults.DEFAULTS_DIR / "roles/design.md").exists()
    assert defaults.role_path("agent/design.md") == defaults.role_path("agent/designer.md")


def test_bundled_rules_do_not_hardcode_project_documents():
    for path in bundled_rule_files():
        text = path.read_text(encoding="utf-8")
        assert "deliverables/" not in text, path
        assert ".vscode/mcp.json" not in text, path
        assert "agent-dev-fmea/" not in text, path






def test_operating_rules_bundle_every_global_rule():
    text = defaults.operating_rules()
    for label, relative in defaults.GLOBAL_RULE_FILES:
        assert f"# `{label}`" in text
        assert (defaults.DEFAULTS_DIR / relative).read_text(encoding="utf-8")[:200] in text


def test_profiles_and_templates_fall_back_to_bundled_defaults(tmp_path):
    local = tmp_path / "config/templates/fmea"
    assert defaults.workspace_or_default(tmp_path, "config/templates/fmea", defaults.DEFAULTS_DIR) == (
        defaults.DEFAULTS_DIR / "config/templates/fmea"
    )
    local.mkdir(parents=True)
    assert defaults.workspace_or_default(tmp_path, "config/templates/fmea", defaults.DEFAULTS_DIR) == local


def _sync_module():
    path = Path(__file__).resolve().parents[1] / "scripts/sync_defaults.py"
    spec = importlib.util.spec_from_file_location("sync_defaults", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sync_writes_root_copies_and_reports_no_drift(tmp_path):
    sync = _sync_module()
    assert sync.main(["--root", str(tmp_path), "--check"]) == 1
    assert sync.main(["--root", str(tmp_path)]) == 0
    assert sync.main(["--root", str(tmp_path), "--check"]) == 0
    assert (tmp_path / "agent/designer.md").is_file()
    assert (tmp_path / ".agents/skills/dev-pipeline/handoff-checkpoint.md").is_file()


def test_development_root_copies_match_bundled_rules():
    sync = _sync_module()
    if not (sync.DEFAULT_ROOT / "AGENTS.md").is_file():
        pytest.skip("not running inside the development repository layout")
    assert sync.drift(sync.DEFAULT_ROOT) == []


def test_claude_md_delegates_to_agents_md():
    assert (defaults.DEFAULTS_DIR / "CLAUDE.md").read_text(encoding="utf-8").strip() == "@AGENTS.md"




def test_phase_rules_drop_interactive_sections_without_truncation():
    full = (defaults.DEFAULTS_DIR / "AGENTS.md").read_text(encoding="utf-8")
    rules = dict(defaults.global_rules("dba"))["AGENTS.md"]
    assert "## 4. 작업 모드" not in rules and "## 10. 자동화 사용 권장" not in rules
    assert "### 7.1" not in rules and "## 7. 컨텍스트 수집 규칙" in rules
    assert "## 6. 디자인 참고 자료" not in rules
    assert "## 6. 디자인 참고 자료" in dict(defaults.global_rules("ux_ui"))["AGENTS.md"]
    assert "## 11. 에이전트 핸드오프 규칙" in rules and "[truncated]" not in rules
    assert full.split("## 11.")[1] in rules
    assert "## 6." in defaults.operating_rules() and "## 4. 작업 모드" not in defaults.operating_rules()


def test_section_filter_ignores_headings_inside_code_fences():
    text = "## 1. A\nkeep\n## 4. B\n```txt\n## 9. fake\n```\ndrop\n## 5. C\nkeep2"
    assert defaults.select_sections(text, ["4"]) == "## 1. A\nkeep\n## 5. C\nkeep2"






def test_root_claude_md_keeps_tool_sections(tmp_path):
    sync = _sync_module()
    assert sync.main(["--root", str(tmp_path)]) == 0
    claude = tmp_path / "CLAUDE.md"
    claude.write_text("@AGENTS.md\n\n## graphify\n\nUse the graph.\n", encoding="utf-8")
    assert sync.main(["--root", str(tmp_path), "--check"]) == 0
    claude.write_text("@OLD.md\n\n## graphify\n\nUse the graph.\n", encoding="utf-8")
    assert sync.main(["--root", str(tmp_path), "--check"]) == 1
    assert sync.main(["--root", str(tmp_path)]) == 0
    assert claude.read_text(encoding="utf-8") == "@AGENTS.md\n\n## graphify\n\nUse the graph.\n"
    # Other root copies stay byte-for-byte.
    (tmp_path / "AGENTS.md").write_text("changed", encoding="utf-8")
    assert sync.main(["--root", str(tmp_path), "--check"]) == 1
