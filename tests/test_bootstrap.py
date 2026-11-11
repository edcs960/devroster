"""The entry point assembles the layers; adapters never import the ui layer (architecture step 5)."""

import pytest

from agent_dev.adapters import command_line
from agent_dev.domain.errors import DomainError


def test_cli_web_screens_come_from_the_entry_point(monkeypatch):
    monkeypatch.setattr(command_line, "WEB_SERVERS", {})
    with pytest.raises(DomainError, match="devroster"):
        command_line.web_server("studio")
    from agent_dev import entrypoints

    servers = entrypoints.web_servers()
    assert set(servers) == {"studio", "dashboard", "setup"}
    command_line.register_web_servers(servers)
    assert command_line.web_server("dashboard") is servers["dashboard"]


def test_slack_requests_start_a_real_studio_spec_generation(tmp_path):
    """The Slack listener used the kind "spec", which Studio rejects; it is "specs"."""
    from agent_dev.adapters.messenger import StudioService

    service = StudioService(tmp_path)
    item = service.studio.create("요청", "본문")
    data = service.studio.generation_input(item["project"]["id"], item["project"]["version"], "specs")
    assert data["brief"] == "본문"
    service.jobs.close()


def test_the_start_page_is_decided_in_the_ui_layer(tmp_path):
    from agent_dev import app
    from agent_dev.ui.studio_server import start_path

    assert start_path(tmp_path) == app.start_path(tmp_path) == "/setup"
