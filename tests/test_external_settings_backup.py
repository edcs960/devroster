"""Portable bundles of an external workspace: logical paths, no credentials, new identity."""

import json
from zipfile import ZipFile

import pytest
from test_external_workspace_storage import Runner, approved, external  # noqa: F401
from test_shared_integrations import SHARED_TOKEN, FakeClient

from agent_dev.adapters.backup import create_bundle, inspect_bundle, restore_bundle
from agent_dev.adapters.credential_store import CredentialStore
from agent_dev.adapters.integrations import Integrations
from agent_dev.adapters.shared_integrations import SharedIntegrations
from agent_dev.adapters.studio_store import StudioStore
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.adapters.workspace_locator import workspace_locations
from agent_dev.application.studio import Studio
from agent_dev.core.storage_locations import PORTABLE
from agent_dev.domain.content import content_digest
from agent_dev.domain.errors import DomainError

PRIVATE = "external-private-value"


@pytest.fixture
def populated(external, monkeypatch):  # noqa: F811
    """External workspace with an executed task, Studio data and a shared-binding read."""
    source, managed = external
    monkeypatch.setattr("agent_dev.adapters.integrations.remote_address", lambda *a, **k: None)
    runtime = approved(source, managed)
    runtime.execute("T", "run", runner=Runner())
    runtime.execute("T", "verify", verify=True)
    Studio(StudioStore(managed), None).create("Managed idea", "Studio document in config")
    shared = SharedIntegrations(source)
    shared.register(
        "github-personal",
        {"provider": "github", "scopes": ["repo-A"], "credential_ref": "GITHUB_TOKEN"},
        expected_version=0,
    )
    CredentialStore.save_shared("github-personal", "GITHUB_TOKEN", SHARED_TOKEN)
    workspace_id = workspace_locations(source).workspace_id
    shared.bind(workspace_id, "github", "github-personal", 1, ["repo-A"], ["get_me"], 0)
    app = Integrations(source, client_factory=FakeClient)
    assert app.check("github", operation="get_me", scope="repo-A")["state"] == "ready"
    app.call("github", "get_me", {}, scope="repo-A")
    return source, managed, runtime


def test_bundle_uses_logical_paths_and_excludes_credentials(populated, tmp_path):
    source, _, _ = populated
    locations = workspace_locations(source)
    artifacts = list((locations.cache_root / "integration-artifacts/github").glob("*.json"))
    assert len(artifacts) == 1
    bundle = tmp_path / "bundle.zip"
    create_bundle(source, bundle)
    with ZipFile(bundle) as archive:
        names = archive.namelist()
        assert ".agent-dev/state.db" in names
        assert ".agent-dev/studio/studio.db" in names
        assert ".agent-dev/integration-artifacts/github/" + artifacts[0].name in names
        assert all("credentials/" not in name and "environment.json" not in name for name in names)
        assert not any(name.startswith(("cache/", "studio/", "roles/")) for name in names)
        marker = json.loads(archive.read(".agent-dev/workspace.json"))
        for name in names:
            content = archive.read(name)
            assert PRIVATE.encode() not in content and SHARED_TOKEN.encode() not in content
    assert not {"workspace_id", "source_root", "runtime_root"} & set(marker)
    assert inspect_bundle(bundle)["valid"]


def test_restore_is_a_new_disabled_workspace_with_identical_evidence(populated, tmp_path):
    source, managed, runtime = populated
    original = runtime.store.snapshot("T")["digest"]
    reads = runtime.store.db.rows("SELECT id FROM events WHERE kind='IntegrationRead'")
    bundle = tmp_path / "bundle.zip"
    create_bundle(source, bundle)
    destination = tmp_path / "restored"
    report = restore_bundle(bundle, destination)
    assert report["execution_disabled"]
    locations = workspace_locations(destination)
    assert locations.layout == PORTABLE and locations.workspace_id == ""
    assert locations.source_root == destination  # never the original source folder
    restored = WorkflowRuntime(destination)
    assert restored.store.snapshot("T")["digest"] == original
    assert not restored.store.approved("T")
    assert restored.store.db.rows("SELECT id FROM events WHERE kind='IntegrationRead'") == reads
    assert restored.store.db.rows("PRAGMA foreign_key_check") == []
    for row in restored.store.db.rows("SELECT path,digest FROM artifacts"):
        assert content_digest((destination / "output" / row["path"]).read_bytes()) == row["digest"]
    connection = Integrations(destination).show("github")
    assert connection["enabled"] is False and "shared" not in connection
    with pytest.raises(DomainError):
        Integrations(destination, client_factory=FakeClient).call(
            "github", "get_me", {}, scope="repo-A"
        )
    assert [p["title"] for p in StudioStore(destination).projects()] == ["Managed idea"]
    assert not (destination / ".agent-dev/environment.json").exists()
    # The original external workspace keeps working and keeps its approvals.
    assert runtime.store.approved("T")
    assert workspace_locations(source).config_root == managed


def test_restore_strips_location_fields_from_a_crafted_marker(populated, tmp_path):
    source, _, _ = populated
    bundle = tmp_path / "bundle.zip"
    create_bundle(source, bundle)
    crafted = tmp_path / "crafted.zip"
    with ZipFile(bundle) as original, ZipFile(crafted, "w") as target:
        manifest = json.loads(original.read("manifest.json"))
        marker = dict(
            json.loads(original.read(".agent-dev/workspace.json")),
            workspace_id="w",
            source_root=str(source),
        )
        content = json.dumps(marker).encode()
        for entry in manifest["entries"]:
            if entry["path"] == ".agent-dev/workspace.json":
                entry.update(size=len(content), sha256=content_digest(content))
        target.writestr("manifest.json", json.dumps(manifest))
        for name in original.namelist():
            if name != "manifest.json":
                target.writestr(
                    name, content if name == ".agent-dev/workspace.json" else original.read(name)
                )
    destination = tmp_path / "restored"
    restore_bundle(crafted, destination)
    assert workspace_locations(destination).source_root == destination
    assert "source_root" not in json.loads((destination / ".agent-dev/workspace.json").read_text(encoding="utf-8"))
