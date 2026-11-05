"""Independent workspace locations: stable IDs, explicit errors and read-only lookups."""

import json
from pathlib import Path

import pytest

from agent_dev.adapters import storage_registry
from agent_dev.adapters.storage_registry import workspace_locations
from agent_dev.adapters.workflow_runtime import project_root
from agent_dev.adapters.workspace import configuration_root, discover, initialize
from agent_dev.adapters.workspace_registry import configuration_for, register_root
from agent_dev.core import cli_home
from agent_dev.core.storage_locations import (
    EXTERNAL,
    LOCAL,
    PORTABLE,
    bootstrap_file,
    management_root,
)
from agent_dev.domain.errors import DomainError, ErrorCode


def folders(tmp_path, *names):
    paths = [tmp_path / name for name in names]
    for path in paths:
        path.mkdir(parents=True)
    return paths


def test_external_config_keeps_source_identity(tmp_path):
    source, config = folders(tmp_path, "source", "managed")
    workspace_id = storage_registry.register(source, config, source / ".agent-dev")
    locations = workspace_locations(source)
    assert locations.workspace_id == workspace_id
    assert locations.config_root == config
    assert locations.source_root == source
    assert locations.state_db == config / "state.db"
    assert locations.worktree_root == source / ".agent-dev/worktrees"
    assert locations.layout == EXTERNAL
    assert locations.studio_db == config / "studio/studio.db"
    assert locations.role_dir == config / "roles"
    assert locations.output_root == source / "output"
    credentials = management_root() / "credentials/projects" / workspace_id
    assert locations.environment_file == credentials / "environment.json"
    assert locations.oauth_dir == credentials / "oauth"
    # The config and runtime folders resolve to the same workspace.
    assert workspace_locations(config) == locations
    assert workspace_locations(source / ".agent-dev") == locations


def test_lookup_creates_no_folders_databases_or_registry(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    locations = workspace_locations(root)
    assert locations.layout == LOCAL
    assert locations.state_db == root / ".agent-dev/state.db"
    assert not (root / ".agent-dev").exists()
    assert not storage_registry.state_file().exists()
    assert configuration_root(root) == root
    assert not storage_registry.state_file().exists()


def test_portable_workspace_keeps_existing_paths(tmp_path):
    root = tmp_path / "project"
    initialize(root, name="Example", project_id="EXAMPLE", paths=["src"])
    managed = root / ".agent-dev"
    for path in (root, managed):
        locations = workspace_locations(path)
        assert locations.layout == PORTABLE
        assert locations.workspace_id == ""
        assert locations.config_root == managed
        assert locations.source_root == root
        assert locations.state_db == managed / "state.db"
        assert locations.studio_db == managed / "studio/studio.db"
        assert locations.environment_file == managed / "environment.json"
        assert locations.role_dir == managed / "roles"
        assert locations.worktree_root == managed / "worktrees"


def test_relink_preserves_workspace_id_and_rejects_stale_revision(tmp_path):
    source, config = folders(tmp_path, "source", "managed")
    workspace_id = storage_registry.register(source, config, source / ".agent-dev")
    revision = storage_registry.read()["revision"]
    moved = tmp_path / "moved"
    source.rename(moved)
    with pytest.raises(DomainError) as stale:
        storage_registry.relink(workspace_id, moved, revision - 1)
    assert stale.value.code == ErrorCode.STALE_VERSION
    storage_registry.relink(workspace_id, moved, revision)
    locations = workspace_locations(moved)
    assert locations.workspace_id == workspace_id
    assert locations.state_db == config / "state.db"
    assert locations.runtime_root == moved / ".agent-dev"
    assert storage_registry.find(source) is None


def test_same_named_clone_is_never_matched_to_another_workspace(tmp_path):
    first, second, config = folders(tmp_path, "a/project", "b/project", "managed")
    storage_registry.register(first, config, first / ".agent-dev")
    initialize(second, name="Example", project_id="EXAMPLE", paths=["src"])
    clone = workspace_locations(second)
    assert clone.layout == PORTABLE
    assert clone.state_db == second / ".agent-dev/state.db"
    assert clone.state_db != workspace_locations(first).state_db


@pytest.mark.parametrize(
    "case",
    ["same-source", "same-config", "nested-config", "config-in-source", "runtime", "missing"],
)
def test_conflicting_or_unsafe_mappings_are_rejected(tmp_path, case):
    source, other, config = folders(tmp_path, "source", "other", "managed")
    storage_registry.register(source, config, source / ".agent-dev")
    before = storage_registry.read()
    arguments = {
        "same-source": (source, tmp_path / "second", source / ".agent-dev"),
        "same-config": (other, config, other / ".agent-dev"),
        "nested-config": (other, config / "nested", other / ".agent-dev"),
        "config-in-source": (other, other / "config", other / ".agent-dev"),
        "runtime": (other, tmp_path / "second", tmp_path / "runtime"),
        "missing": (tmp_path / "missing", tmp_path / "second", tmp_path / "missing/.agent-dev"),
    }[case]
    with pytest.raises(DomainError):
        storage_registry.register(*arguments)
    assert storage_registry.read() == before


def test_symlinked_config_is_rejected(tmp_path):
    source, real = folders(tmp_path, "source", "real")
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(DomainError):
        storage_registry.register(source, link, source / ".agent-dev")


def test_registered_mapping_takes_priority_over_a_stale_local_marker(tmp_path):
    source = tmp_path / "source"
    initialize(source, name="Example", project_id="EXAMPLE", paths=["src"])
    config = tmp_path / "managed"
    config.mkdir()
    storage_registry.register(source, config, source / ".agent-dev")
    assert configuration_root(source) == config
    assert configuration_root(source / ".agent-dev") == config
    (source / "src").mkdir()
    assert discover(source / "src") == config
    assert project_root(config) == source
    assert configuration_for(source) == config


def test_external_marker_resolves_without_registry(tmp_path, monkeypatch):
    source, config = folders(tmp_path, "source", "managed")
    marker = {
        "format_version": 1,
        "project_id": "EXAMPLE",
        "name": "Example",
        "workspace_id": "w-1",
        "source_root": str(source),
        "runtime_root": str(source / ".agent-dev"),
    }
    (config / "workspace.json").write_text(json.dumps(marker))
    locations = workspace_locations(config)
    assert locations.layout == EXTERNAL
    # A marker alone is not an identity: no workspace ID, no shared credential folder.
    assert locations.workspace_id == ""
    assert locations.environment_file == config / "environment.json"
    assert locations.oauth_dir == config / "oauth"
    assert locations.source_root == source
    assert project_root(config) == source
    monkeypatch.setenv("AGENT_DEV_CONFIG_ROOT", str(config))
    from agent_dev.adapters.workspace import default_root

    assert default_root() == config


def test_marker_and_registry_disagreement_is_an_error(tmp_path):
    source, config = folders(tmp_path, "source", "managed")
    storage_registry.register(source, config, source / ".agent-dev")
    (config / "workspace.json").write_text(
        json.dumps({"format_version": 1, "project_id": "X", "name": "X", "workspace_id": "other"})
    )
    with pytest.raises(DomainError):
        workspace_locations(source)


def test_registry_preserves_project_list_and_unrelated_keys(tmp_path):
    source, config, plain = folders(tmp_path, "source", "managed", "plain")
    state = storage_registry.state_file()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"unrelated": "kept"}))
    storage_registry.register(source, config, source / ".agent-dev")
    register_root(plain)
    value = json.loads(state.read_text())
    assert value["unrelated"] == "kept"
    assert str(plain) in value["roots"]
    assert value["workspace_registry"]["revision"] == 1
    assert storage_registry.find(source) is not None


def test_damaged_registry_is_an_explicit_error(tmp_path):
    state = storage_registry.state_file()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"workspace_registry": {"revision": 1, "workspaces": []}}))
    with pytest.raises(DomainError):
        workspace_locations(tmp_path)


def test_management_root_order_and_invalid_pointers(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    assert management_root({"DEVROSTER_HOME": str(explicit)}) == explicit
    with pytest.raises(DomainError):
        management_root({"DEVROSTER_HOME": "relative/home"})
    (tmp_path / "file").write_text("")
    with pytest.raises(DomainError):
        management_root({"DEVROSTER_HOME": str(tmp_path / "file")})
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "user")
    monkeypatch.setattr(cli_home.sys, "platform", "darwin")
    default = tmp_path / "user/Library/Application Support/DevRoster"
    assert management_root({}) == default
    pointer = bootstrap_file({})
    pointer.parent.mkdir(parents=True)
    selected = tmp_path / "selected"
    pointer.write_text(json.dumps({"schema_version": 1, "management_root": str(selected)}))
    with pytest.raises(DomainError):
        management_root({})  # A missing selected folder never falls back to the default.
    selected.mkdir()
    assert management_root({}) == selected
    monkeypatch.delenv("DEVROSTER_HOME")
    assert cli_home.app_home() == selected
    assert cli_home.cli_homes()["CODEX_HOME"] == selected / "cli/codex"
    pointer.write_text("{broken")
    with pytest.raises(DomainError):
        management_root({})
