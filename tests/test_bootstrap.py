"""The entry point assembles the layers; adapters never import the ui layer (architecture step 5)."""

import argparse
from pathlib import Path

import pytest

from agent_dev.adapters import cli_registry, command_line
from agent_dev.domain.errors import DomainError


def test_cli_web_screens_come_from_the_entry_point(monkeypatch):
    monkeypatch.setattr(cli_registry, "WEB_SERVERS", {})
    with pytest.raises(DomainError, match="devroster"):
        command_line.web_server("studio")
    from agent_dev import entrypoints

    servers = entrypoints.web_servers()
    assert set(servers) == {"studio", "dashboard", "setup"}
    command_line.register_web_servers(servers)
    assert command_line.web_server("dashboard") is servers["dashboard"]


def test_messenger_command_comes_from_the_entry_point(monkeypatch, tmp_path):
    """The listener runs commands through the command line, so the command line does not import it."""
    monkeypatch.setattr(cli_registry, "COMMANDS", {})
    args = argparse.Namespace(root=Path(tmp_path), data=None, db=None, namespace="messenger", action="status")
    with pytest.raises(DomainError, match="messenger"):
        command_line.dispatch(args)
    from agent_dev import entrypoints

    calls = []
    monkeypatch.setattr("agent_dev.adapters.messenger.run_cli", lambda value: calls.append(value) or {"ok": True})
    cli_registry.register_commands(entrypoints.commands())
    assert command_line.dispatch(args) == {"ok": True} and calls == [args]


def test_every_installed_entry_point_runs_the_messenger_command(tmp_path):
    """devroster, fa, agent-dev and python -m agent_dev all start in entrypoints.main, which registers it."""
    import json
    import os
    import subprocess
    import sys

    env = dict(os.environ, DEVROSTER_HOME=str(tmp_path / "home"), DEVROSTER_VAULT="file")
    (tmp_path / "project").mkdir()
    result = subprocess.run(
        [sys.executable, "-m", "agent_dev", "messenger", "status", "--root", str(tmp_path / "project"), "--json"],
        capture_output=True, text=True, encoding="utf-8", env=env, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "completed"
    from agent_dev import fa

    assert "entrypoints" in Path(fa.__file__).read_text(encoding="utf-8")
