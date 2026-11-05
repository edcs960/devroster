"""The settings screen computes role sessions with the same rule as the server."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_dev.domain import project_roles

SETUP_JS = Path(__file__).resolve().parents[1] / "agent_dev/ui/assets/setup.js"


def custom(mode, name):
    return {"id": f"custom-{mode}-{name}", "name": name, "mode": mode, "instruction": "x"}


def rosters():
    fmea = project_roles.default_roles("fmea")
    moved = project_roles.default_roles("generic")
    moved["order"].remove("security")
    moved["order"].insert(moved["order"].index("developer"), "security")
    mixed = project_roles.default_roles("generic")
    mixed["custom"] = [
        custom("readonly", "intro"),
        custom("code", "perf"),
        custom("documents", "gloss"),
    ]
    mixed["order"].insert(0, "custom-readonly-intro")
    mixed["order"].insert(mixed["order"].index("qa"), "custom-code-perf")
    mixed["order"].append("custom-documents-gloss")
    split = project_roles.default_roles("generic")
    split["order"].remove("planner")
    split["order"].insert(split["order"].index("developer") + 1, "planner")
    return [fmea, moved, mixed, split]


def js_sessions(roster):
    source = SETUP_JS.read_text(encoding="utf-8")
    function = re.search(r"function roleSessions\(\) \{.*?return out;\}", source, re.DOTALL).group(
        0
    )
    catalog = {row["id"]: row["session"] for row in project_roles.catalog()}
    script = (
        f"const state={{roles:{json.dumps(roster)}}};"
        f"const catalog={json.dumps(catalog)};"
        "function roleInfo(id){return {session:catalog[id]||''};}"
        f"{function}"
        "process.stdout.write(JSON.stringify(roleSessions()));"
    )
    result = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the UI rule")
@pytest.mark.parametrize("index", range(4))
def test_setup_screen_sessions_match_server_rule(index):
    roster = rosters()[index]
    assert js_sessions(roster) == project_roles.sessions(project_roles.normalize(roster))


def test_legacy_phase_readme_matches_pipeline():
    """The phase table injected into legacy prompts follows pipeline.json."""
    from agent_dev.defaults import DEFAULTS_DIR

    steps = json.loads((DEFAULTS_DIR / "config/pipeline.json").read_text(encoding="utf-8"))["steps"]
    readme = (DEFAULTS_DIR / "phases/README.md").read_text(encoding="utf-8")
    listed = re.findall(r"^\| \d+ \| `([0-9a-z-]+)` \|", readme, re.MULTILINE)
    assert listed == [step["id"] for step in steps]
    assert all((DEFAULTS_DIR / "phases" / f"{step['id']}.md").is_file() for step in steps)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for the UI rule")
def test_execution_screen_session_card_text():
    """The session card summarises execution_view sessions."""
    source = (SETUP_JS.parent / "agent.js").read_text(encoding="utf-8")
    names = re.search(r"const sessionNames=.*?;\n", source).group(0)
    card = re.search(r"function sessionCard\(rows\)\{.*?\}\n", source, re.DOTALL).group(0)
    rows = [
        {"id": "analysis", "state": "passed", "roles": 4},
        {"id": "develop", "state": "running", "roles": 3},
        {"id": "verify-2", "state": "not_started", "roles": 2},
        {"id": "docs", "state": "skipped", "roles": 1},
    ]
    script = (
        "function card(title,value,note){return {title,value,note};}"
        f"{names}{card}"
        f"process.stdout.write(JSON.stringify(sessionCard({json.dumps(rows)})));"
    )
    result = json.loads(
        subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    )
    assert result == {
        "title": "세션",
        "value": "2 / 4 완료",
        "note": "분석 완료 · 개발 진행 중 · 검증 2 시작 전 · 문서 건너뜀",
    }
