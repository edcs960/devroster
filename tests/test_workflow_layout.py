"""Workflow layout (WF-01): optional coordinates never change execution meaning."""

import copy

import pytest

from agent_dev.adapters.workflow_definitions import preset
from agent_dev.domain.content import digest
from agent_dev.domain.errors import DomainError
from agent_dev.domain.workflow_graph import (
    layout_digest,
    normalize,
    semantic_digest,
    version_diff,
)
from tests.test_workflow_definitions import service  # noqa: F401  (pytest fixture)
from tests.test_workflow_graph import graph


def laid_out():
    value = graph()
    value["layout"] = {"ui": {"x": 240, "y": 120}}
    return value


def test_layout_is_retained_and_does_not_change_semantic_digest():
    plain, placed = normalize(graph(), gates={"unit"}), normalize(laid_out(), gates={"unit"})
    assert placed["layout"] == {"ui": {"x": 240, "y": 120}}
    assert semantic_digest(placed) == semantic_digest(plain)
    assert layout_digest(placed) != layout_digest(plain)
    moved = laid_out()
    moved["layout"]["ui"]["x"] = 300
    assert semantic_digest(normalize(moved, gates={"unit"})) == semantic_digest(placed)


def test_legacy_graph_digest_is_unchanged():
    plain = normalize(graph(), gates={"unit"})
    assert "layout" not in plain
    assert semantic_digest(plain) == digest(plain)


@pytest.mark.parametrize(
    "layout",
    [
        {"missing": {"x": 1, "y": 1}},
        {"ui": {"x": 1}},
        {"ui": {"x": 1, "y": 1, "z": 1}},
        {"ui": {"x": "1", "y": 1}},
        {"ui": {"x": True, "y": 1}},
        {"ui": {"x": 200001, "y": 1}},
        {"ui": [1, 2]},
        [],
    ],
)
def test_invalid_layout_is_rejected(layout):
    value = graph()
    value["layout"] = layout
    with pytest.raises(DomainError):
        normalize(value, gates={"unit"})


def test_version_diff_separates_nodes_edges_config_and_layout():
    before = normalize(laid_out(), gates={"unit"})
    after = copy.deepcopy(before)
    after["name"] = "Branch v2"
    after["nodes"][1]["model"] = "gpt-x"
    after["nodes"] = [n for n in after["nodes"] if n["id"] != "api"] + [{"id": "qa", "kind": "role", "role_id": "qa"}]
    after["edges"] = [e for e in after["edges"] if "api" not in (e["from"], e["to"])] + [{"from": "merge", "to": "qa"}]
    after["layout"] = {"ui": {"x": 10, "y": 20}, "qa": {"x": 0, "y": 0}}
    diff = version_diff(before, after)
    assert diff["nodes"] == {"added": ["qa"], "removed": ["api"], "changed": ["ui"]}
    assert {"from": "merge", "to": "qa"} in diff["edges"]["added"]
    assert {"from": "start", "to": "api", "when": False} in diff["edges"]["removed"]
    assert diff["config"] == ["name"]
    assert diff["layout"] == {"moved": ["ui"], "added": ["qa"], "removed": []}
    assert diff["semantic_changed"] is True
    only_layout = copy.deepcopy(before)
    only_layout["layout"] = {"ui": {"x": 1, "y": 1}}
    quiet = version_diff(before, only_layout)
    assert quiet["semantic_changed"] is False and quiet["layout"]["moved"] == ["ui"]


def test_saved_layout_only_version_keeps_semantic_digest_and_diffs(service):  # noqa: F811
    document = preset("quick-fix")
    document["id"] = "mine"
    first = service.save("P", document)
    document["layout"] = {document["nodes"][0]["id"]: {"x": 40, "y": 80}}
    second = service.save("P", document, first["version"])
    assert second["semantic_digest"] == first["semantic_digest"]
    assert second["layout_digest"] != first["layout_digest"]
    assert first["digest"] == digest(service.show("P", "mine", 1)["document"])
    diff = service.diff("P", "mine", 1, 2)["diff"]
    assert diff["semantic_changed"] is False and diff["layout"]["added"] == [document["nodes"][0]["id"]]
