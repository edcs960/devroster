"""Shared MCP connections: explicit per-workspace binding, pinned versions and revocation."""

import json
from typing import ClassVar

import pytest

from agent_dev.adapters.credential_store import CredentialStore
from agent_dev.adapters.global_settings import GlobalStore
from agent_dev.adapters.integrations import Integrations
from agent_dev.adapters.shared_integrations import SharedIntegrations
from agent_dev.adapters.state.sqlite.database import Database
from agent_dev.adapters.workspace import initialize
from agent_dev.domain.errors import DomainError, ErrorCode

SHARED_TOKEN = "shared-github-token-value"
PROJECT_TOKEN = "project-b-token-value"
DEFINITION = {
    "provider": "github",
    "name": "GitHub (personal)",
    "scopes": ["repo-A", "repo-B"],
    "credential_ref": "GITHUB_TOKEN",
}


class FakeClient:
    calls: ClassVar[list] = []

    def __init__(self, config, *, credential="", cwd=None, timeout=30):
        self.credential = credential

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def tools(self):
        return [
            {"name": name, "inputSchema": {"type": "object"}}
            for name in ("get_me", "list_issues", "search_repositories")
        ]

    def call(self, name, args):
        FakeClient.calls.append((name, self.credential))
        return {"content": [{"type": "text", "text": f"result {self.credential}"}]}


@pytest.fixture
def projects(tmp_path, monkeypatch):
    monkeypatch.setattr("agent_dev.adapters.integrations.remote_address", lambda *a, **k: None)
    FakeClient.calls = []
    a, b = tmp_path / "a", tmp_path / "b"
    initialize(a, name="Alpha", project_id="ALPHA")
    initialize(b, name="Beta", project_id="BETA")
    (b / ".agent-dev/environment.json").write_text(json.dumps({"B_TOKEN": PROJECT_TOKEN}), encoding="utf-8")
    shared = SharedIntegrations(a)
    registered = shared.register("github-personal", DEFINITION, expected_version=0)
    CredentialStore.save_shared("github-personal", "GITHUB_TOKEN", SHARED_TOKEN)
    return a, b, registered


def app(root):
    return Integrations(root, client_factory=FakeClient)


def bind_both(a, b):
    SharedIntegrations(a).bind(
        "", "github", "github-personal", 1, ["repo-A"], ["get_me"], expected_version=0
    )
    SharedIntegrations(b).bind(
        "",
        "github",
        "github-personal",
        1,
        ["repo-B"],
        ["list_issues"],
        expected_version=0,
        credential_ref="B_TOKEN",
    )
    for root, operation, scope in ((a, "get_me", "repo-A"), (b, "list_issues", "repo-B")):
        assert app(root).check("github", operation=operation, scope=scope)["state"] == "ready"


def test_registration_is_versioned_and_secret_free(projects):
    a, _, registered = projects
    assert registered["version"] == 1 and registered["enabled"] is True
    with pytest.raises(DomainError):
        SharedIntegrations(a).register("github-personal", DEFINITION, expected_version=0)
    with pytest.raises(DomainError):
        SharedIntegrations(a).register(
            "leaky", dict(DEFINITION, token=SHARED_TOKEN), expected_version=0
        )
    second = SharedIntegrations(a).register(
        "github-personal", dict(DEFINITION, scopes=["repo-A"]), expected_version=1
    )
    assert second["version"] == 2
    store = GlobalStore()
    content = store.path.read_bytes()
    assert SHARED_TOKEN.encode() not in content
    listed = SharedIntegrations(a).definitions()
    assert [(row["connection_id"], row["version"]) for row in listed] == [("github-personal", 2)]


def test_unbound_shared_connection_cannot_be_called(projects):
    a, _, _ = projects
    with pytest.raises(DomainError):
        app(a).call("github", "get_me", {}, scope="repo-A")


def test_bound_projects_use_the_intersection_and_their_own_credential(projects):
    a, b, _ = projects
    bind_both(a, b)
    assert app(a).call("github", "get_me", {}, scope="repo-A")["trust"] == "untrusted_data"
    assert app(b).call("github", "list_issues", {}, scope="repo-B")["trust"] == "untrusted_data"
    assert FakeClient.calls[-2:] == [("get_me", SHARED_TOKEN), ("list_issues", PROJECT_TOKEN)]
    for root, operation, scope in (
        (a, "get_me", "repo-B"),  # outside A's binding
        (a, "list_issues", "repo-A"),  # operation not selected for A
        (b, "get_me", "repo-B"),
    ):
        with pytest.raises(DomainError):
            app(root).call("github", operation, {}, scope=scope)
    rows = Database(a / ".agent-dev/state.db", read_only=True).rows(
        "SELECT * FROM shared_connection_bindings"
    )
    assert len(rows) == 1
    assert rows[0]["local_connection_id"] == "ALPHA:github"
    assert rows[0]["global_connection_id"] == "github-personal"
    assert json.loads(rows[0]["scopes_json"]) == ["repo-A"]
    assert SHARED_TOKEN not in json.dumps(rows) and PROJECT_TOKEN not in json.dumps(rows)


@pytest.mark.parametrize(
    "arguments",
    [
        {"scopes": ["repo-C"]},
        {"operations": ["delete_repository"]},
        {"operations": []},
        {"version": 9},
        {"expected_version": 3},
        {"workspace_id": "another-workspace"},
        {"local_id": "../escape"},
    ],
)
def test_widening_or_stale_bindings_are_rejected(projects, arguments):
    a, _, _ = projects
    values = {
        "workspace_id": "",
        "local_id": "github",
        "global_id": "github-personal",
        "version": 1,
        "scopes": ["repo-A"],
        "operations": ["get_me"],
        "expected_version": 0,
    }
    values.update(arguments)
    with pytest.raises(DomainError):
        SharedIntegrations(a).bind(**values)
    assert app(a).list() == []


def test_bound_row_cannot_be_reconfigured_as_a_project_connection(projects):
    a, b, _ = projects
    bind_both(a, b)
    row = app(a).show("github")
    with pytest.raises(DomainError):
        app(a).configure(
            {"id": "github", "provider": "github", "scopes": ["repo-B", "repo-C"]},
            expected_version=row["version"],
        )


def test_shared_update_keeps_pinned_version_until_explicit_rebind(projects):
    a, b, _ = projects
    bind_both(a, b)
    SharedIntegrations(a).register(
        "github-personal", dict(DEFINITION, scopes=["repo-B"]), expected_version=1
    )
    row = app(a).show("github")
    assert row["shared"]["version"] == 1 and row["scopes"] == ["repo-A"]
    assert app(a).call("github", "get_me", {}, scope="repo-A")
    with pytest.raises(DomainError):  # repo-A is no longer shared by version 2
        SharedIntegrations(a).bind(
            "",
            "github",
            "github-personal",
            2,
            ["repo-A"],
            ["get_me"],
            expected_version=row["version"],
        )


def test_disable_blocks_every_binding_even_with_a_pinned_version(projects):
    a, b, _ = projects
    bind_both(a, b)
    state = SharedIntegrations(a).definitions()[0]["state_revision"]
    with pytest.raises(DomainError) as stale:
        SharedIntegrations(a).disable("github-personal", expected_revision=state + 1)
    assert stale.value.code == ErrorCode.STALE_VERSION
    SharedIntegrations(a).disable(global_id="github-personal", expected_revision=state)
    for root, operation, scope in ((a, "get_me", "repo-A"), (b, "list_issues", "repo-B")):
        with pytest.raises(DomainError):
            app(root).call(connection_id="github", operation=operation, arguments={}, scope=scope)
        assert app(root).check("github")["state"] == "disabled"
    # Disabled definitions stay as tombstones for history; nothing is deleted.
    assert SharedIntegrations(a).definitions()[0]["enabled"] is False
    with pytest.raises(DomainError):
        SharedIntegrations(a).bind(
            "", "other", "github-personal", 1, ["repo-A"], ["get_me"], expected_version=0
        )


def test_revoked_shared_credential_blocks_only_shared_credential_users(projects):
    a, b, _ = projects
    bind_both(a, b)
    CredentialStore.revoke_shared("github-personal")
    with pytest.raises(DomainError) as blocked:
        app(a).call("github", "get_me", {}, scope="repo-A")
    assert blocked.value.code == ErrorCode.AUTH_REQUIRED
    assert app(b).call("github", "list_issues", {}, scope="repo-B")


def test_exports_events_and_logs_carry_no_secret(projects):
    a, b, _ = projects
    bind_both(a, b)
    app(a).call("github", "get_me", {}, scope="repo-A")
    exported = json.dumps(app(a).export())
    events = json.dumps(
        Database(a / ".agent-dev/state.db", read_only=True).rows("SELECT * FROM events")
    )
    global_events = json.dumps(
        [
            list(row)
            for row in __import__("sqlite3")
            .connect(GlobalStore().path)
            .execute("SELECT * FROM global_events")
        ]
    )
    for text in (exported, events, global_events):
        assert SHARED_TOKEN not in text and PROJECT_TOKEN not in text
    assert "shared" not in json.loads(exported)["connections"][0]


def test_graph_lookup_survives_a_shared_binding_with_the_graph_id(projects, monkeypatch):
    a, _, _ = projects
    from agent_dev.domain.project_roles import GRAPH_CONNECTION

    SharedIntegrations(a).register(
        "graphify-shared",
        {"provider": "graphify", "scopes": ["graphify-out"]},
        expected_version=0,
    )
    SharedIntegrations(a).bind(
        "", GRAPH_CONNECTION, "graphify-shared", 1, ["graphify-out"], ["query_graph"], 0
    )
    row = app(a).show(GRAPH_CONNECTION)
    assert row["id"] == GRAPH_CONNECTION
    assert list(row["operations"]) == ["query_graph"]
