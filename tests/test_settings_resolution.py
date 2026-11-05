"""Field-by-field composition of shared defaults and project overrides."""

import pytest

from agent_dev.domain.errors import DomainError
from agent_dev.domain.execution_limits import DEFAULT_LIMITS
from agent_dev.domain.project_roles import default_roles
from agent_dev.domain.settings_resolution import (
    resolve_settings,
    validate_global,
    validate_overrides,
)


def test_explicit_empty_model_differs_from_inheritance():
    base = {"ai": {"model": "registered-model"}}
    inherited = resolve_settings(base, {})
    overridden = resolve_settings(base, {"ai": {"model": ""}})
    assert inherited["values"]["ai"]["model"] == "registered-model"
    assert inherited["origins"]["ai.model"] == "global"
    assert overridden["values"]["ai"]["model"] == ""
    assert overridden["origins"]["ai.model"] == "project"


def test_missing_values_fall_back_to_built_in_defaults():
    result = resolve_settings({}, {})
    assert result["values"]["limits"] == DEFAULT_LIMITS
    assert result["values"]["ai"] == {"provider": "", "model": "", "effort": ""}
    assert set(result["origins"].values()) == {"default"}
    assert result["origins"]["limits.max_attempts"] == "default"


def test_scalars_resolve_independently():
    result = resolve_settings(
        {"ai": {"provider": "claude_cli", "effort": "high"}, "limits": {"max_attempts": 5}},
        {"ai": {"effort": "low"}, "limits": {"max_reentries": 3}},
    )
    assert result["values"]["ai"] == {"provider": "claude_cli", "model": "", "effort": "low"}
    assert result["origins"]["ai.provider"] == "global"
    assert result["origins"]["ai.effort"] == "project"
    assert result["values"]["limits"]["max_attempts"] == 5
    assert result["values"]["limits"]["max_reentries"] == 3
    assert result["origins"]["limits.phase_timeout_seconds"] == "default"


@pytest.mark.parametrize(
    "overrides",
    [
        {"ai": {"model": None}},  # null is never "inherit"; delete the key instead
        {"ai": None},
        {"ai": {"unknown": "x"}},
        {"limits": {"max_attempts": "5"}},
        {"limits": {"max_attempts": True}},
        {"limits": {"max_attempts": 0}},
        {"limits": {"max_attempts": 1001}},
        {"limit_caps": {"max_attempts": 3}},  # only shared settings set caps
        {"templates": {"roles": {}}},
        {"allowed_paths": ["src"]},  # execution scope is never inherited or overridden here
        {"repositories": {}},
        {"verification": {}},
        {"orchestrator_v2": {"execute": True}},
        {"environment": [{"key": "A", "value": "b"}]},
        {"ai": {"model": "x" * 201}},
        {"ai": {"model": "bad\nmodel"}},
        [],
    ],
)
def test_invalid_overrides_are_rejected(overrides):
    with pytest.raises(DomainError):
        validate_overrides(overrides)


@pytest.mark.parametrize(
    "values",
    [
        {"ai": {"model": None}},
        {"limits": {"max_attempts": 2000}},
        {"limit_caps": {"unknown": 1}},
        {"limits": {"max_attempts": 30}, "limit_caps": {"max_attempts": 10}},
        {"allowed_paths": ["src"]},
        {"credential_ref": "TOKEN"},
    ],
)
def test_invalid_global_values_are_rejected(values):
    with pytest.raises(DomainError):
        validate_global(values)


def test_project_override_above_the_shared_cap_is_rejected():
    shared = {"limits": {"max_attempts": 5}, "limit_caps": {"max_attempts": 10}}
    assert (
        resolve_settings(shared, {"limits": {"max_attempts": 10}})["values"]["limits"][
            "max_attempts"
        ]
        == 10
    )
    with pytest.raises(DomainError):
        resolve_settings(shared, {"limits": {"max_attempts": 11}})


def test_arrays_replace_and_templates_are_not_merged():
    roster = dict(default_roles(), session_mode=True)
    validate_global({"templates": {"roles": roster}})
    result = resolve_settings({"templates": {"roles": roster}}, {})
    # Templates are copied into new profiles explicitly; they never enter effective values.
    assert "templates" not in result["values"]


def test_resolution_does_not_mutate_inputs():
    shared = {"ai": {"model": "m"}, "limits": {"max_attempts": 4}}
    overrides = {"ai": {"effort": "low"}}
    before = (repr(shared), repr(overrides))
    resolve_settings(shared, overrides)
    assert (repr(shared), repr(overrides)) == before
