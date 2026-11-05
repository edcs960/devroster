"""Freeze narrowly selected integration grants and cached design references."""

from copy import deepcopy

from agent_dev.adapters.design_references import DesignReferences
from agent_dev.adapters.integrations import Integrations
from agent_dev.domain.content import digest
from agent_dev.domain.errors import DomainError


def binding_digest(row):
    keys = {
        "provider",
        "transport",
        "endpoint",
        "command",
        "credential_ref",
        "scopes",
        "enabled",
        "local",
        "bound_arguments",
        "environment_keys",
        "operations",
        "oauth_client_id",
        "supported_client",
        "shared",
    }
    return digest({key: value for key, value in row.items() if key in keys})


def bind(root, resolved):
    app = Integrations(root)
    graph = deepcopy(resolved.get("workflow_graph", {}))
    grants = []
    warnings = []
    if not resolved.get("workflow_legacy", True):
        for role in list(graph.get("nodes", [])):
            if role["kind"] != "role" or not role.get("tools"):
                continue
            previous = None
            for index, connection_id in enumerate(role["tools"][:10]):
                try:
                    row = app.show(connection_id)
                except DomainError:
                    warnings.append(
                        {
                            "node": role["id"],
                            "connection_id": connection_id,
                            "state": "skipped",
                            "reason": "CONNECTION_NOT_REGISTERED",
                        }
                    )
                    continue
                reason = app.shared_state(row)
                if reason:
                    warnings.append(
                        {
                            "node": role["id"],
                            "connection_id": connection_id,
                            "state": "skipped",
                            "reason": reason,
                        }
                    )
                    continue
                operations = list(row["operations"])
                if not operations or not row["scopes"]:
                    warnings.append(
                        {
                            "node": role["id"],
                            "connection_id": connection_id,
                            "state": "skipped",
                            "reason": "OPERATION_OR_SCOPE_NOT_CONFIGURED",
                        }
                    )
                    continue
                operation = operations[0]
                key = "tool-" + digest([role["id"], connection_id, index])[:24]
                node = {
                    "id": key,
                    "kind": "tool",
                    "connection_id": connection_id,
                    "operation": operation,
                    "capability": row["operations"][operation]["capability"],
                    "scope": row["scopes"][0],
                    "arguments": dict(row["bound_arguments"]),
                    "required": False,
                    "budget": {"timeout_seconds": 30},
                }
                graph["nodes"].append(node)
                if previous:
                    graph["edges"].append({"from": previous, "to": key})
                else:
                    for edge in graph["edges"]:
                        if edge["to"] == role["id"]:
                            edge["to"] = key
                previous = key
            if previous:
                graph["edges"].append({"from": previous, "to": role["id"]})
        for node in graph.get("nodes", []):
            if node["kind"] != "tool":
                continue
            try:
                row = app.show(node["connection_id"])
            except DomainError:
                continue
            spec = row["operations"].get(node["operation"])
            if (
                app.shared_state(row)
                or not spec
                or node.get("scope", "") not in row["scopes"]
                or node["capability"] != spec["capability"]
            ):
                continue
            grants.append(
                {
                    "connection_id": row["id"],
                    "operations": [node["operation"]],
                    "capabilities": [spec["capability"]],
                    "scope": node["scope"],
                    "binding_digest": binding_digest(row),
                    "schema_digest": row["schema_digest"],
                }
            )
        from agent_dev.domain.workflow_graph import normalize

        resolved["workflow_graph"] = normalize(graph)
    resolved["tools"] = grants + resolved.get("tools", [])
    resolved["tool_preparation_warnings"] = warnings
    references = resolved.get("design_references", [])
    if isinstance(references, list) and references:
        resolved["design_reference_bundles"] = DesignReferences(root).bundle(references)
    return resolved
