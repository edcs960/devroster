"""Test/document paths for structured roles are editable in project settings."""

import pytest

from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.domain.errors import DomainError


def saved_profile(root):
    _, _, profile = ProjectSetup(root).current_profile()
    return profile


def test_paths_round_trip_through_settings(tmp_path):
    root = tmp_path / "project"
    value = ProjectSetup(root).read()
    value["create_directories"] = True
    value["test_paths"] = ["tests", "src/test", "tests"]
    value["document_paths"] = ["README.md", "docs"]
    ProjectSetup(root).save(value)
    profile = saved_profile(root)
    assert profile["test_paths"] == ["tests", "src/test"]
    assert profile["document_paths"] == ["README.md", "docs"]
    loaded = ProjectSetup(root).read()
    assert loaded["test_paths"] == ["tests", "src/test"]
    assert loaded["document_paths"] == ["README.md", "docs"]


def test_empty_list_removes_the_scope(tmp_path):
    root = tmp_path / "project"
    value = ProjectSetup(root).read()
    value["create_directories"] = True
    value["test_paths"] = ["tests"]
    ProjectSetup(root).save(value)
    loaded = ProjectSetup(root).read()
    loaded["test_paths"] = []
    ProjectSetup(root).save(loaded)
    assert "test_paths" not in saved_profile(root)


@pytest.mark.parametrize("path", ["../outside", "/abs/docs", ".git/hooks", ".agent-dev/roles", "."])
def test_unsafe_paths_are_rejected(tmp_path, path):
    root = tmp_path / "project"
    value = ProjectSetup(root).read()
    value["create_directories"] = True
    value["document_paths"] = [path]
    with pytest.raises(DomainError, match="문서 경로"):
        ProjectSetup(root).save(value)


def test_test_path_patterns_cover_colocated_tests_only(tmp_path):
    """React tests live next to their source (src/App.test.jsx), outside tests/."""
    from agent_dev.adapters.project_setup import STRUCTURED_PATH_DEFAULTS
    from agent_dev.domain.role_policy import allowed_path, path_matches

    assert path_matches("src/App.test.jsx", "*.test.*")
    assert path_matches("src/test/java/AppTest.java", "src/test")
    assert not path_matches("src/App.jsx", "*.test.*")
    assert not path_matches("src/x.test.d/evil.js", "*.test.*")  # a name pattern, not a folder
    policy = {
        "mode": "code",
        "structured_kind": "tests",
        "scopes": [["src", "tests"], STRUCTURED_PATH_DEFAULTS["test_paths"]],
    }
    assert allowed_path(tmp_path, "src/App.test.jsx", policy)
    assert allowed_path(tmp_path, "tests/test_app.py", policy)
    assert not allowed_path(tmp_path, "src/App.jsx", policy)
    assert not allowed_path(tmp_path, "public/App.test.jsx", policy)  # still inside the module
