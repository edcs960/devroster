"""#28 alternative: keep an environment variable name instead of a secret, and keep files private."""

import json
import os
import stat

import pytest

from agent_dev.adapters import ai_tools_service, messenger
from agent_dev.adapters.credential_store import CredentialStore, permission_report, shared_directory
from agent_dev.adapters.diagnostics import diagnose
from agent_dev.adapters.workspace import initialize
from agent_dev.domain.errors import DomainError

CONNECTION = "env-fixture"


@pytest.fixture(autouse=True)
def clean():
    yield
    CredentialStore.revoke_shared(CONNECTION)


def stored():
    return (shared_directory() / f"{CONNECTION}.json").read_text(encoding="utf-8")


def test_only_the_variable_name_is_written_and_the_value_is_read_at_use(monkeypatch):
    monkeypatch.delenv("FIXTURE_API_KEY", raising=False)
    CredentialStore.save_shared(CONNECTION, "OPENAI_API_KEY", "env:FIXTURE_API_KEY")
    assert "FIXTURE_API_KEY" in stored()
    assert CredentialStore.shared_entry(CONNECTION) == {}
    assert CredentialStore.shared_sources(CONNECTION) == {"OPENAI_API_KEY": {"env": "FIXTURE_API_KEY", "present": False}}
    monkeypatch.setenv("FIXTURE_API_KEY", "sk-from-environment")
    assert CredentialStore.shared_entry(CONNECTION) == {"OPENAI_API_KEY": "sk-from-environment"}
    assert "sk-from-environment" not in stored()
    assert "sk-from-environment" in CredentialStore.shared_values()  # still redacted in logs
    CredentialStore.save_shared(CONNECTION, "OPENAI_API_KEY", "sk-literal")  # back to a stored value
    assert CredentialStore.shared_sources(CONNECTION) == {}
    assert CredentialStore.shared_entry(CONNECTION) == {"OPENAI_API_KEY": "sk-literal"}
    CredentialStore.save_shared(CONNECTION, "OPENAI_API_KEY", "env:FIXTURE_API_KEY")
    assert "sk-literal" not in stored()
    CredentialStore.remove_shared_key(CONNECTION, "OPENAI_API_KEY")
    assert CredentialStore.shared_sources(CONNECTION) == {} and CredentialStore.shared_entry(CONNECTION) == {}


@pytest.mark.parametrize("value", ["env:", "env:1BAD", "env:A-B", "env:A B"])
def test_bad_variable_names_are_refused(value):
    with pytest.raises(DomainError):
        CredentialStore.save_shared(CONNECTION, "OPENAI_API_KEY", value)


def test_a_reference_names_the_missing_variable(tmp_path, monkeypatch):
    initialize(tmp_path)
    from agent_dev.adapters.storage_registry import workspace_locations

    locations = workspace_locations(tmp_path)
    store = CredentialStore(locations)
    CredentialStore.save_shared(CONNECTION, "TOKEN", "env:FIXTURE_TOKEN")
    monkeypatch.delenv("FIXTURE_TOKEN", raising=False)
    with pytest.raises(DomainError, match="FIXTURE_TOKEN"):
        store.resolve(f"shared:{CONNECTION}:TOKEN", locations.workspace_id, "")
    monkeypatch.setenv("FIXTURE_TOKEN", "t-1")
    assert store.resolve(f"shared:{CONNECTION}:TOKEN", locations.workspace_id, "") == {"TOKEN": "t-1"}


def test_slack_tokens_accept_a_reference_and_check_a_set_value(monkeypatch, tmp_path):
    monkeypatch.delenv("FIXTURE_SLACK", raising=False)
    messenger.save_token("env:FIXTURE_SLACK")  # Not set yet: accepted, checked when it is used.
    assert messenger.status(tmp_path)["token_sources"]["SLACK_BOT_TOKEN"] == {"env": "FIXTURE_SLACK", "present": False}
    monkeypatch.setenv("FIXTURE_SLACK", "not-a-slack-token")
    with pytest.raises(DomainError):
        messenger.save_token("env:FIXTURE_SLACK")
    monkeypatch.setenv("FIXTURE_SLACK", "xoxb-from-env")
    messenger.save_token("env:FIXTURE_SLACK")
    assert messenger.token_saved()
    assert "xoxb-from-env" not in json.dumps(messenger.status(tmp_path))
    messenger.delete_token()


def test_ai_tools_screen_reports_where_a_key_comes_from(monkeypatch):
    monkeypatch.setenv("FIXTURE_OPENAI", "sk-env")
    name = next(iter(ai_tools_service.state()["keys"]), "OPENAI_API_KEY")
    ai_tools_service.set_key(name, "env:FIXTURE_OPENAI")
    try:
        state = ai_tools_service.state()
        assert state["key_sources"][name] == {"env": "FIXTURE_OPENAI", "present": True}
        assert "sk-env" not in json.dumps(state)
    finally:
        ai_tools_service.set_key(name, delete=True)


@pytest.mark.skipif(os.name == "nt", reason="Windows protects credentials with the profile ACL")
def test_loose_credential_files_are_reported_and_repaired(tmp_path):
    CredentialStore.save_shared(CONNECTION, "TOKEN", "secret")
    path = shared_directory() / f"{CONNECTION}.json"
    path.chmod(0o644)
    report = permission_report()
    assert any(row["path"] == str(path) and row["mode"] == "0o644" for row in report["loose"])
    initialize(tmp_path)
    doctor = {c["check"]: c for c in diagnose(tmp_path)["checks"]}
    assert doctor["credential_permissions"]["status"] == "warn"
    fixed = {c["check"]: c for c in diagnose(tmp_path, fix_permissions=True)["checks"]}
    assert fixed["credential_permissions"]["status"] == "pass"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert permission_report()["loose"] == []
