"""Bounded declarative DAGs, deterministic scheduling and trusted metadata conditions."""

import json
import re
from copy import deepcopy
from itertools import pairwise
from typing import NoReturn

from agent_dev.domain.content import digest
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.project_roles import is_custom
from agent_dev.domain.role_policy import ROLES, role_id

KINDS = {"role", "condition", "approval", "verification", "merge", "tool"}
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
COMMON = {"id", "kind", "label", "position"}
FIELDS = {
    "role": {
        "role_id",
        "module",
        "allowed_paths",
        "model",
        "effort",
        "provider",
        "timeout_seconds",
        "cross_check",
        "reentry_targets",
        "design_reference",
        "tools",
        "role_file",
        "session",
    },
    "condition": {"field", "equals"},
    "approval": {"actor"},
    "verification": {"gate_ids"},
    "merge": set(),
    "tool": {
        "connection_id",
        "operation",
        "arguments",
        "capability",
        "scope",
        "required",
        "budget",
    },
}


class WorkflowValidationError(DomainError):
    """INVALID_INPUT with a safe location so editors can focus the offending input."""

    def __init__(self, message, *, node=None, field=None, edge=None):
        super().__init__(ErrorCode.INVALID_INPUT, message)
        self.location = {
            key: value
            for key, value in {"node": node, "field": field, "edge": edge}.items()
            if value is not None
        }


def invalid(message, *, node=None, field=None, edge=None) -> NoReturn:
    raise WorkflowValidationError(message, node=node, field=field, edge=edge)


def identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        invalid("Workflow identifier must be 1–80 safe characters.")
    return value


def strings(value, field):
    if not isinstance(value, list) or any(
        not isinstance(x, str) or not x or len(x) > 500 for x in value
    ):
        invalid("Invalid string list: " + field, field=field)


def normalize(document, *, modules=None, gates=None, roles=ROLES):
    try:
        return _normalize(document, modules=modules, gates=gates, roles=roles)
    except (TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        invalid("Malformed Workflow field type or excessive nesting.")


def _normalize(document, *, modules=None, gates=None, roles=ROLES):
    if not isinstance(document, dict):
        invalid("Workflow document must be an object.")
    try:
        encoded = json.dumps(document, allow_nan=False).encode()
    except (ValueError, TypeError):
        invalid("Workflow must contain finite JSON values.")
    if len(encoded) > 1_000_000:
        invalid("Workflow document exceeds 1MB.")
    if set(document) - {"schema_version", "id", "name", "description", "nodes", "edges", "layout"}:
        invalid("Unknown Workflow document field.")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        invalid("Workflow schema_version 1 required.")
    identifier(document.get("id"))
    if not isinstance(document.get("name"), str) or not 1 <= len(document["name"]) <= 200:
        invalid("Workflow name must be 1–200 characters.")
    if (
        not isinstance(document.get("description", ""), str)
        or len(document.get("description", "")) > 4000
    ):
        invalid("Workflow description exceeds limit.")
    nodes, edges = document.get("nodes"), document.get("edges")
    if (
        not isinstance(nodes, list)
        or not 1 <= len(nodes) <= 60
        or not isinstance(edges, list)
        or len(edges) > 120
    ):
        invalid("Workflow requires 1–60 nodes and at most 120 edges.")
    result = deepcopy(document)
    ids = set()
    for node in result["nodes"]:
        try:
            if not isinstance(node, dict) or node.get("kind") not in KINDS:
                invalid("Unknown node kind.", field="kind")
            if not isinstance(node.get("id"), str) or not _ID.fullmatch(node["id"]):
                invalid("Workflow identifier must be 1–80 safe characters.", field="id")
            if node["id"] in ids:
                invalid("Duplicate node ID: " + node["id"], field="id")
            ids.add(node["id"])
            if set(node) - (COMMON | FIELDS[node["kind"]]):
                invalid("Unknown node field: " + node["id"])
            if "label" in node and (not isinstance(node["label"], str) or len(node["label"]) > 200):
                invalid("Invalid node label.", field="label")
            if "position" in node and (
                not isinstance(node["position"], dict)
                or set(node["position"]) != {"x", "y"}
                or any(
                    type(v) not in {int, float} or abs(v) > 100000 for v in node["position"].values()
                )
            ):
                invalid("Invalid node position.", field="position")
            kind = node["kind"]
            if kind == "role":
                try:
                    node["role_id"] = role_id(node.get("role_id"))
                except DomainError as error:
                    invalid(str(error), field="role_id")
                # Custom roles are checked against the project roster where the profile is known.
                if node["role_id"] not in roles and not is_custom(node["role_id"]):
                    invalid("Unregistered role.", field="role_id")
                if "module" in node and (
                    not isinstance(node["module"], str)
                    or modules is not None
                    and node["module"] not in modules
                ):
                    invalid("Unknown module: " + str(node["module"])[:80], field="module")
                for field in ("allowed_paths", "reentry_targets", "tools"):
                    if field in node:
                        strings(node[field], field)
                for path in node.get("allowed_paths", []):
                    if path.startswith("/") or ".." in path.split("/") or "\\" in path:
                        invalid("Node path escapes project.", field="allowed_paths")
                for field in ("model", "provider", "role_file"):
                    if field in node and (not isinstance(node[field], str) or len(node[field]) > 500):
                        invalid("Invalid role setting: " + field, field=field)
                if node.get("effort", "auto") not in {
                    "auto",
                    "low",
                    "medium",
                    "high",
                    "xhigh",
                    "max",
                    "ultra",
                }:
                    invalid("Unknown reasoning effort.", field="effort")
                if "timeout_seconds" in node and (
                    type(node["timeout_seconds"]) is not int or not 1 <= node["timeout_seconds"] <= 7200
                ):
                    invalid("Invalid node timeout.", field="timeout_seconds")
                if node.get("design_reference", "none") not in {"none", "stitch", "figma", "both"}:
                    invalid("Invalid design reference.", field="design_reference")
                if "cross_check" in node and node["cross_check"] not in {"off", "optional", "required"}:
                    invalid("Invalid cross-check policy.", field="cross_check")
                if "session" in node and (
                    not isinstance(node["session"], str) or not _ID.fullmatch(node["session"])
                ):
                    invalid("Invalid session identifier.", field="session")
            elif kind == "condition":
                if (
                    node.get("field") not in {"workstream", "risk", "task_type"}
                    or not isinstance(node.get("equals"), str)
                    or not 1 <= len(node["equals"]) <= 80
                ):
                    invalid(
                        "Condition requires trusted field and string equals.",
                        field="field" if node.get("field") not in {"workstream", "risk", "task_type"} else "equals",
                    )
            elif kind == "verification":
                strings(node.get("gate_ids"), "gate_ids")
                if gates is not None and set(node["gate_ids"]) - set(gates):
                    invalid(
                        "Unknown verification gate: " + ", ".join(sorted(set(node["gate_ids"]) - set(gates)))[:200],
                        field="gate_ids",
                    )
            elif kind == "approval":
                if "actor" in node and (
                    not isinstance(node["actor"], str) or not 1 <= len(node["actor"]) <= 120
                ):
                    invalid("Invalid approval actor.", field="actor")
            elif kind == "tool":
                for field in ("connection_id", "operation", "capability"):
                    if not isinstance(node.get(field), str) or not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}", node[field]
                    ):
                        invalid("Invalid tool identity: " + field, field=field)
                if (
                    not isinstance(node.get("arguments", {}), dict)
                    or not isinstance(node.get("scope", ""), str)
                    or len(node.get("scope", "")) > 1000
                ):
                    invalid("Invalid tool arguments or scope.", field="scope")
                if type(node.get("required", True)) is not bool:
                    invalid("Tool required must be boolean.", field="required")
                if "budget" in node and (
                    not isinstance(node["budget"], dict)
                    or set(node["budget"]) - {"max_calls", "timeout_seconds"}
                    or any(type(v) is not int or not 1 <= v <= 7200 for v in node["budget"].values())
                ):
                    invalid("Invalid tool budget.", field="budget")
        except WorkflowValidationError as error:
            if "node" not in error.location and isinstance(node, dict) and isinstance(node.get("id"), str):
                error.location["node"] = node["id"][:200]
            raise
    if "layout" in result:
        _layout(result["layout"], ids)
    incoming = {key: [] for key in ids}
    outgoing = {key: [] for key in ids}
    seen = set()
    by_id = {n["id"]: n for n in result["nodes"]}
    for index, edge in enumerate(edges):
        if (
            not isinstance(edge, dict)
            or set(edge) - {"from", "to", "when"}
            or edge.get("from") not in ids
            or edge.get("to") not in ids
            or edge["from"] == edge["to"]
        ):
            invalid("Unknown, self, or malformed edge.", edge=index)
        key = (edge["from"], edge["to"], edge.get("when"))
        if key in seen:
            invalid("Duplicate edge.", edge=index)
        seen.add(key)
        if by_id[edge["from"]]["kind"] == "condition":
            if type(edge.get("when")) is not bool:
                invalid("Condition edges require true/false routes.", edge=index)
        elif "when" in edge:
            invalid("Only condition edges may have when.", edge=index)
        incoming[edge["to"]].append(edge)
        outgoing[edge["from"]].append(edge)
    for node in result["nodes"]:
        if node["kind"] == "condition" and sorted(e["when"] for e in outgoing[node["id"]]) != [
            False,
            True,
        ]:
            invalid("Condition requires exactly one true and one false route.", node=node["id"])
        if set(node.get("reentry_targets", [])) - ids:
            invalid("Unknown reentry target.", node=node["id"], field="reentry_targets")
    entries = [key for key in ids if not incoming[key]]
    if len(entries) != 1:
        invalid("Workflow requires exactly one entry and no disconnected nodes.")
    visited, active = set(), set()

    def visit(key):
        if key in active:
            invalid("Workflow cycle.")
        if key in visited:
            return
        active.add(key)
        for edge in outgoing[key]:
            visit(edge["to"])
        active.remove(key)
        visited.add(key)

    visit(entries[0])
    if visited != ids:
        invalid("Workflow has unreachable nodes or a disconnected cycle.")
    _sessions(result)
    return result


# Settings that must match so one runner invocation can serve every node of a session.
SESSION_SHARED = ("module", "provider", "model")
SESSION_GAPS = {"condition", "merge"}


def execution_order(graph):
    """Node order the serial scheduler follows when every node completes (list order wins)."""
    parents = {n["id"]: set() for n in graph["nodes"]}
    for edge in graph["edges"]:
        parents[edge["to"]].add(edge["from"])
    order, done = [], set()
    while len(order) < len(graph["nodes"]):
        node = next(
            n for n in graph["nodes"] if n["id"] not in done and parents[n["id"]] <= done
        )
        order.append(node)
        done.add(node["id"])
    return order


def session_spans(graph):
    """Session id -> node ids from its first to last role, including condition/merge gaps."""
    order = execution_order(graph)
    positions = {}
    for index, node in enumerate(order):
        if node["kind"] == "role" and node.get("session"):
            positions.setdefault(node["session"], []).append(index)
    return {
        session: [n["id"] for n in order[min(found) : max(found) + 1]]
        for session, found in positions.items()
    }


def _sessions(graph):
    by_id = {n["id"]: n for n in graph["nodes"]}
    for session, span in session_spans(graph).items():
        members = []
        for key in span:
            node = by_id[key]
            if node["kind"] == "role" and node.get("session") == session:
                members.append(node)
            elif node["kind"] not in SESSION_GAPS:
                invalid("Session must be contiguous: " + session, node=key, field="session")
        for field in SESSION_SHARED:
            values = {n[field] for n in members if n.get(field)}
            if len(values) > 1:
                invalid(
                    "Session nodes must share " + field + ": " + session,
                    node=members[0]["id"],
                    field=field,
                )


def _layout(layout, ids):
    """Editor-only coordinates keyed by node ID; never read by the scheduler."""
    if not isinstance(layout, dict) or len(layout) > 60:
        invalid("Workflow layout must map node IDs to coordinates.")
    for node_id, point in layout.items():
        if node_id not in ids:
            invalid("Layout references unknown node: " + str(node_id)[:80], field="layout")
        if (
            not isinstance(point, dict)
            or set(point) != {"x", "y"}
            or any(type(v) not in {int, float} or abs(v) > 100000 for v in point.values())
        ):
            invalid("Invalid layout coordinates: " + node_id, node=node_id, field="layout")


def semantic_digest(graph):
    """Digest of execution meaning; equals digest(graph) for graphs without layout."""
    return digest({k: v for k, v in graph.items() if k != "layout"})


def layout_digest(graph):
    return digest(graph.get("layout") or {})


def _edge_key(edge):
    return json.dumps(edge, sort_keys=True)


def version_diff(before, after):
    """Node/edge/config/layout changes between two normalized graphs."""
    old = {n["id"]: n for n in before["nodes"]}
    new = {n["id"]: n for n in after["nodes"]}
    old_edges = {_edge_key(e): e for e in before["edges"]}
    new_edges = {_edge_key(e): e for e in after["edges"]}
    old_layout, new_layout = before.get("layout") or {}, after.get("layout") or {}
    return {
        "nodes": {
            "added": sorted(new.keys() - old.keys()),
            "removed": sorted(old.keys() - new.keys()),
            "changed": sorted(k for k in old.keys() & new.keys() if old[k] != new[k]),
        },
        "edges": {
            "added": [new_edges[k] for k in sorted(new_edges.keys() - old_edges.keys())],
            "removed": [old_edges[k] for k in sorted(old_edges.keys() - new_edges.keys())],
        },
        "config": sorted(
            k for k in ("name", "description") if before.get(k, "") != after.get(k, "")
        ),
        "layout": {
            "moved": sorted(
                k for k in old_layout.keys() & new_layout.keys() if old_layout[k] != new_layout[k]
            ),
            "added": sorted(new_layout.keys() - old_layout.keys()),
            "removed": sorted(old_layout.keys() - new_layout.keys()),
        },
        "semantic_changed": semantic_digest(before) != semantic_digest(after),
    }


def legacy(phases):
    if not isinstance(phases, list) or not phases:
        invalid("Approved legacy phases required.")
    nodes = []
    for phase in phases:
        if isinstance(phase, str):
            phase = {"id": phase}
        if not isinstance(phase, dict):
            invalid("Invalid legacy phase.")
        identifier(phase.get("id"))
        # Legacy IDs and instructions remain in phases; graph only supplies topology.
        nodes.append(
            {
                "id": phase["id"],
                "kind": "role",
                "role_id": phase.get("role_id", ALIASES_LEGACY.get(phase["id"], phase["id"])),
            }
        )
    graph = {
        "schema_version": 1,
        "id": "legacy-linear",
        "name": "Legacy linear",
        "nodes": nodes,
        "edges": [{"from": a["id"], "to": b["id"]} for a, b in pairwise(nodes)],
    }
    if len({n["id"] for n in nodes}) != len(nodes):
        invalid("Duplicate legacy phase ID.")
    return graph


ALIASES_LEGACY = {
    "design": "designer",
    "developer-common": "developer",
    "frontend-developer": "developer-frontend",
    "backend-developer": "developer-backend",
    "infra-developer": "developer-infra",
    "security-reviewer": "security",
    "fmea": "fmea-expert",
}


def condition(node, metadata):
    value = metadata.get(node["field"])
    if node["field"] == "workstream":
        value = metadata.get("workstreams", value)
    return node["equals"] in value if isinstance(value, list) else value == node["equals"]


def schedule(graph, states, metadata):
    """Return next node and active flag; wait for every predecessor, including merges."""
    for node in graph["nodes"]:
        key = node["id"]
        if states.get(key, {}).get("state") in {"passed", "skipped"}:
            continue
        parents = [e for e in graph["edges"] if e["to"] == key]
        if any(
            states.get(e["from"], {}).get("state") not in {"passed", "skipped"} for e in parents
        ):
            continue
        enabled = not parents
        by_id = {n["id"]: n for n in graph["nodes"]}
        for edge in parents:
            parent = states[edge["from"]]
            if parent["state"] == "passed" and (
                by_id[edge["from"]]["kind"] != "condition"
                or condition(by_id[edge["from"]], metadata) == edge["when"]
            ):
                enabled = True
        return node, enabled
    return None


def descendants(graph, node_id):
    found = {node_id}
    while True:
        expanded = found | {e["to"] for e in graph["edges"] if e["from"] in found}
        if expanded == found:
            return found
        found = expanded


def impact(metadata):
    text = json.dumps(metadata, ensure_ascii=False).lower()
    return {
        "db": metadata.get("db_impact") is True
        or any(
            x in text
            for x in (
                "migration",
                "schema",
                "database",
                "repository",
                "audit",
                "revision",
                "스키마",
                "마이그레이션",
                "데이터 모델",
                "감사 로그",
            )
        ),
        "fmea": metadata.get("fmea_impact") is True
        or any(
            x in text
            for x in (
                "fmea",
                "aiag",
                "pfmea",
                "dfmea",
                "control plan",
                "rpn",
                "failure chain",
                "function chain",
            )
        ),
    }


def preview(graph, metadata):
    states = {}
    while next_node := schedule(graph, states, metadata):
        node, enabled = next_node
        states[node["id"]] = {
            "state": "passed" if enabled else "skipped",
            "kind": node["kind"],
            "role_id": node.get("role_id", ""),
        }
    required = impact(metadata)
    active_roles = {x["role_id"] for x in states.values() if x["state"] == "passed"}
    missing = [
        role
        for flag, role in [("db", "dba"), ("fmea", "fmea-expert")]
        if required[flag] and role not in active_roles
    ]
    return {"nodes": states, "impact": required, "missing_roles": missing, "digest": digest(graph)}
