"""Generic bundled rules with domain packs and change-gated browser e2e."""

import re


from agent_dev import defaults

DOMAIN_TERMS = re.compile(r"FMEA|AIAG|fe-fmea|be-fmea|GoJS|Spring Boot|MobX|DFMEA|PFMEA")
# Files that name the FMEA role as a catalog entry or example, not as an instruction.
CATALOG_ONLY = {"roles/fmea-expert.md", "phases/06-fmea-expert.md", "phases/README.md",
                "skills/dev-pipeline/SKILL.md"}


def generic_files():
    for folder in ("roles", "phases", "prompts", "skills"):
        for path in sorted((defaults.DEFAULTS_DIR / folder).rglob("*.md")):
            relative = path.relative_to(defaults.DEFAULTS_DIR).as_posix()
            if relative not in CATALOG_ONLY:
                yield relative, path


def test_generic_rules_carry_no_domain_instructions():
    assert not DOMAIN_TERMS.search(defaults.agents_text())
    leaks = [rel for rel, path in generic_files() if DOMAIN_TERMS.search(path.read_text())]
    assert leaks == []


def test_fmea_pack_keeps_its_rules_and_role_texts():
    rules = defaults.agents_text("fmea")
    assert "## 12. 도메인 규칙: AIAG-VDA FMEA" in rules
    assert "반드시 FMEA Expert 역할을 포함한다" in rules and "`agent/fmea-expert.md`" in rules
    assert "AIAG-VDA FMEA 시스템의 Frontend" in defaults.role_text("agent/developer-frontend.md", domain="fmea")
    assert "AIAG-VDA FMEA" not in defaults.role_text("agent/developer-frontend.md")
    # Roles without a domain copy fall back to the generic text.
    assert defaults.role_path("agent/planner.md", "fmea") == defaults.role_path("agent/planner.md")
    phases = dict(defaults.phase_guides("11-qa", domain="fmea"))
    assert "프론트엔드 저장소 구성" in phases["phases/11-qa.md"]
    assert "프론트엔드 저장소 구성" not in dict(defaults.phase_guides("11-qa"))["phases/11-qa.md"]


def test_unknown_or_unsafe_domains_are_generic():
    for value in (None, "", "nope", "../roles", "FMEA", 3):
        assert defaults.domain_name(value) == defaults.GENERIC
    assert defaults.agents_text("../x") == defaults.agents_text()


def test_phase_prompts_drop_interactive_sections_but_keep_the_domain_section():
    (label, text), *_ = defaults.global_rules("qa", domain="fmea")
    assert label == "AGENTS.md" and "## 12. 도메인 규칙" in text
    assert "### 7.1 도구 인벤토리" not in text
    assert "## 12. 도메인 규칙" in defaults.operating_rules("fmea")
    assert "## 12. 도메인 규칙" not in defaults.operating_rules()


def test_snapshot_domain_follows_roster_or_fmea_expert_node():
    from agent_dev.adapters.workflow_runtime import project_domain

    assert project_domain({"roles": {"domain": "fmea"}}) == "fmea"
    assert project_domain({"roles": {"domain": "generic"}, "workflow_graph": {"nodes": [{"role_id": "fmea-expert"}]}}) == "generic"
    assert project_domain({"workflow_graph": {"nodes": [{"role_id": "fmea-expert"}]}}) == "fmea"
    assert project_domain({"workflow_graph": {"nodes": [{"role_id": "planner"}]}}) == "generic"




def test_sync_composes_the_requested_domain(tmp_path):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts/sync_defaults.py"
    spec = importlib.util.spec_from_file_location("sync_defaults", path)
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)
    assert sync.main(["--root", str(tmp_path), "--domain", "fmea"]) == 0
    assert "## 12. 도메인 규칙" in (tmp_path / "AGENTS.md").read_text()
    assert "AIAG-VDA FMEA" in (tmp_path / "agent/dba.md").read_text() or "FMEA" in (tmp_path / "agent/dba.md").read_text()
    assert sync.main(["--root", str(tmp_path), "--domain", "fmea", "--check"]) == 0
    # The generic composition differs, so a generic check reports drift.
    assert sync.main(["--root", str(tmp_path), "--check"]) == 1








def test_wiki_view_is_never_edited_by_roles():
    rules = defaults.agents_text("fmea")
    writer = defaults.role_text("agent/tech-writer.md")
    for text in (rules, writer):
        assert "codebase-map" not in text and "adr-index" not in text
    assert "devroster knowledge propose" in writer and "직접 만들거나 고치지 않는다" in writer
