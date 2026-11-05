"""The code graph is refreshed once a task's merged code becomes the baseline."""

import sys

import pytest

from agent_dev.adapters import code_graph


def fake_graphify(tmp_path, exit_code=0):
    log = tmp_path / "calls.txt"
    script = tmp_path / "graphify"
    script.write_text(
        f"#!{sys.executable}\nimport sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"sys.exit({exit_code})\n"
    )
    script.chmod(0o755)
    return script, log


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / "graphify-out").mkdir(parents=True)
    (root / "graphify-out/graph.json").write_text("{}")
    return root


def test_refresh_updates_an_existing_graph(project, tmp_path, monkeypatch):
    script, log = fake_graphify(tmp_path)
    monkeypatch.setattr(code_graph.shutil, "which", lambda name: str(script))
    assert code_graph.refresh(project) == {"state": "updated", "graph": "graphify-out/graph.json"}
    assert log.read_text().split() == ["update", str(project.resolve())]


def test_refresh_skips_projects_without_a_graph(tmp_path, monkeypatch):
    script, log = fake_graphify(tmp_path)
    monkeypatch.setattr(code_graph.shutil, "which", lambda name: str(script))
    assert code_graph.refresh(tmp_path / "empty") == {"state": "skipped", "reason": "no_graph"}
    assert not log.exists()


def test_refresh_reports_missing_graphify(project, tmp_path, monkeypatch):
    monkeypatch.setattr(code_graph.shutil, "which", lambda name: None)
    monkeypatch.setattr(code_graph.Path, "home", lambda: tmp_path / "home")
    assert code_graph.refresh(project)["reason"] == "graphify_not_installed"


def test_refresh_failure_is_evidence_not_an_error(project, tmp_path, monkeypatch):
    script, _ = fake_graphify(tmp_path, exit_code=3)
    monkeypatch.setattr(code_graph.shutil, "which", lambda name: str(script))
    assert code_graph.refresh(project) == {"state": "failed", "reason": "exit 3"}
