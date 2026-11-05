import copy

import pytest

from agent_dev.domain.errors import DomainError
from agent_dev.domain.workflow_graph import descendants, normalize, preview


def graph():
    return {
        "schema_version": 1,
        "id": "branch",
        "name": "Branch",
        "nodes": [
            {"id": "start", "kind": "condition", "field": "workstream", "equals": "frontend"},
            {"id": "ui", "kind": "role", "role_id": "designer"},
            {"id": "api", "kind": "role", "role_id": "architect"},
            {"id": "merge", "kind": "merge"},
            {"id": "verify", "kind": "verification", "gate_ids": ["unit"]},
        ],
        "edges": [
            {"from": "start", "to": "ui", "when": True},
            {"from": "start", "to": "api", "when": False},
            {"from": "ui", "to": "merge"},
            {"from": "api", "to": "merge"},
            {"from": "merge", "to": "verify"},
        ],
    }


def test_branch_merge_and_skipped_evidence():
    result = preview(normalize(graph(), gates=["unit"]), {"workstreams": ["frontend"]})
    assert result["nodes"]["ui"]["state"] == "passed"
    assert result["nodes"]["api"]["state"] == "skipped"
    assert result["nodes"]["merge"]["state"] == "passed"
    assert descendants(graph(), "ui") == {"ui", "merge", "verify"}


@pytest.mark.parametrize(
    "mutation",
    [
        lambda g: g["nodes"].append(copy.deepcopy(g["nodes"][1])),
        lambda g: g["edges"].append({"from": "merge", "to": "start"}),
        lambda g: g["nodes"].append({"id": "alone", "kind": "merge"}),
        lambda g: g["nodes"][1].update(role_id="unknown"),
        lambda g: g["nodes"][0].update(field="eval"),
        lambda g: g["edges"][0].update(when="true"),
        lambda g: g["nodes"][0].update(script="evil"),
        lambda g: g.update(schema_version=True),
        lambda g: g["nodes"][1].update(allowed_paths=["../escape"]),
    ],
)
def test_reject_invalid_graphs(mutation):
    value = graph()
    mutation(value)
    with pytest.raises(DomainError):
        normalize(value, gates=["unit"])


def test_explicit_false_does_not_hide_database_or_fmea_clues():
    result = preview(
        normalize(graph()),
        {"db_impact": False, "fmea_impact": False, "title": "FMEA schema migration"},
    )
    assert result["missing_roles"] == ["dba", "fmea-expert"]


def test_design_alias_and_unknown_gate():
    value = graph()
    value["nodes"][1]["role_id"] = "design"
    assert normalize(value)["nodes"][1]["role_id"] == "designer"
    with pytest.raises(DomainError):
        normalize(value, gates=["lint"])


@pytest.mark.parametrize(
    "mutation",
    [
        lambda g: g["nodes"][0].update(kind=[]),
        lambda g: g["nodes"][1].update(role_id=[]),
        lambda g: g["nodes"][1].update(effort={}),
        lambda g: g["edges"][0].update(**{"from": []}),
        lambda g: g["edges"][0].update(when=[]),
    ],
)
def test_malformed_field_types_are_safe_domain_errors(mutation):
    value = graph()
    mutation(value)
    with pytest.raises(DomainError):
        normalize(value)


@pytest.mark.parametrize(
    "change, location",
    [
        (lambda g: g["nodes"][1].update(module="web"), {"node": "ui", "field": "module"}),
        (lambda g: g["nodes"][1].update(id="bad id"), {"node": "bad id", "field": "id"}),
        (lambda g: g["nodes"][1].update(role_id="nobody"), {"node": "ui", "field": "role_id"}),
        (lambda g: g["nodes"][2].update(allowed_paths=["../x"]), {"node": "api", "field": "allowed_paths"}),
        (lambda g: g["nodes"][4].update(gate_ids=["missing"]), {"node": "verify", "field": "gate_ids"}),
        (lambda g: g["edges"].append({"from": "ui", "to": "ui"}), {"edge": 5}),
        (lambda g: g.update(layout={"ui": {"x": "1", "y": 1}}), {"node": "ui", "field": "layout"}),
    ],
)
def test_validation_error_location(change, location):
    from agent_dev.domain.workflow_graph import WorkflowValidationError

    value = graph()
    change(value)
    with pytest.raises(WorkflowValidationError) as error:
        normalize(value, modules={"backend"}, gates={"unit"})
    assert error.value.location == location


def test_first_of_several_errors_is_located():
    from agent_dev.domain.workflow_graph import WorkflowValidationError

    value = graph()
    value["nodes"][1]["module"] = "web"
    value["nodes"][4]["gate_ids"] = ["missing"]
    value["edges"].append({"from": "ui", "to": "ui"})
    with pytest.raises(WorkflowValidationError) as error:
        normalize(value, modules={"backend"}, gates={"unit"})
    assert error.value.location == {"node": "ui", "field": "module"}
