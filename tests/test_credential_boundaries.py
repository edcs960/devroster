"""Credential references resolve only inside their own workspace and only at execution."""

import json

import pytest

pytest_plugins = ["test_workflow_runtime"]

from test_workflow_dag import prepare
from test_workflow_sessions import session_roster

from agent_dev.adapters.credential_store import CredentialStore
from agent_dev.adapters.secrets import registered_secrets
from agent_dev.adapters.shared_integrations import SharedIntegrations
from agent_dev.adapters.storage_registry import workspace_locations
from agent_dev.adapters.workflow_definitions import roster_graph
from agent_dev.adapters.workspace import initialize
from agent_dev.core.storage_locations import management_root
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.project_roles import GRAPH_CONNECTION

METADATA = {"workstreams": ["frontend", "backend"], "risk": ["db"]}


@pytest.fixture
def workspaces(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    initialize(a, name="Alpha", project_id="ALPHA")
    initialize(b, name="Beta", project_id="BETA")
    (a / ".agent-dev/environment.json").write_text(json.dumps({"A_TOKEN": "alpha-secret"}))
    (b / ".agent-dev/environment.json").write_text(json.dumps({"B_TOKEN": "beta-secret"}))
    return a, b


def test_project_reference_resolves_only_in_its_workspace(workspaces):
    a, _ = workspaces
    store_a = CredentialStore(workspace_locations(a / ".agent-dev"))
    assert store_a.resolve("project:A_TOKEN", "", "") == {"A_TOKEN": "alpha-secret"}
    with pytest.raises(DomainError) as missing:
        store_a.resolve("project:B_TOKEN", "", "")  # another workspace's key is never found
    assert missing.value.code == ErrorCode.AUTH_REQUIRED
    with pytest.raises(DomainError):
        store_a.resolve("project:A_TOKEN", "another-workspace", "")


@pytest.mark.parametrize("reference", ["", "A_TOKEN", "project:", "shared:x", "env:A_TOKEN", 3])
def test_malformed_references_are_rejected(workspaces, reference):
    a, _ = workspaces
    with pytest.raises(DomainError):
        CredentialStore(workspace_locations(a)).resolve(reference, "", "")


def test_shared_credentials_are_private_resource_bound_and_redacted(workspaces):
    a, _ = workspaces
    CredentialStore.save_shared("docs", "DOC_TOKEN", "shared-doc-secret", resource="https://x/mcp")
    path = management_root() / "credentials/shared/docs.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    store = CredentialStore(workspace_locations(a))
    assert store.resolve("shared:docs:DOC_TOKEN", "", "https://x/mcp") == {
        "DOC_TOKEN": "shared-doc-secret"
    }
    with pytest.raises(DomainError):
        store.resolve("shared:docs:DOC_TOKEN", "", "https://other/mcp")
    with pytest.raises(DomainError):
        store.resolve("shared:docs:OTHER", "", "https://x/mcp")
    assert "shared-doc-secret" in registered_secrets(a / ".agent-dev")
    CredentialStore.revoke_shared("docs")
    assert not path.exists()
    with pytest.raises(DomainError) as revoked:
        store.resolve("shared:docs:DOC_TOKEN", "", "https://x/mcp")
    assert revoked.value.code == ErrorCode.AUTH_REQUIRED
    with pytest.raises(DomainError):
        CredentialStore.save_shared("../escape", "DOC_TOKEN", "x")


def test_shared_graph_binding_keeps_the_lookup_until_disabled(runtime):
    shared = SharedIntegrations(runtime.root)
    shared.register(
        "graphify-shared", {"provider": "graphify", "scopes": ["graphify-out"]}, expected_version=0
    )
    shared.bind("", GRAPH_CONNECTION, "graphify-shared", 1, ["graphify-out"], ["query_graph"], 0)
    shared.disable("graphify-shared", expected_revision=1)
    prepare(runtime, roster_graph(session_roster()), METADATA)
    value = runtime.store.snapshot("DAG")["resolved"]
    assert not [n for n in value["workflow_graph"]["nodes"] if n["kind"] == "tool"]
    assert value["tool_preparation_warnings"] == [
        {
            "node": "tech-writer",
            "connection_id": GRAPH_CONNECTION,
            "state": "skipped",
            "reason": "SHARED_CONNECTION_DISABLED",
        }
    ]


def test_enabled_shared_graph_binding_inserts_the_lookup(runtime):
    shared = SharedIntegrations(runtime.root)
    shared.register(
        "graphify-shared", {"provider": "graphify", "scopes": ["graphify-out"]}, expected_version=0
    )
    shared.bind("", GRAPH_CONNECTION, "graphify-shared", 1, ["graphify-out"], ["query_graph"], 0)
    prepare(runtime, roster_graph(session_roster()), METADATA)
    tools = [
        n
        for n in runtime.store.snapshot("DAG")["resolved"]["workflow_graph"]["nodes"]
        if n["kind"] == "tool"
    ]
    assert [n["connection_id"] for n in tools] == [GRAPH_CONNECTION]
