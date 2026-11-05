"""Immutable built-ins and project-local, optimistic Workflow versions."""

import json
from copy import deepcopy

from agent_dev.domain.content import canonical, digest
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.role_policy import ROLES
from agent_dev.domain.workflow_graph import (
    WorkflowValidationError,
    impact,
    layout_digest,
    normalize,
    preview,
    semantic_digest,
    version_diff,
)

PRESET_ROLES = {
    "quick-fix": ["developer", "qa", "reviewer"],
    "full-feature": [
        "planner",
        "fmea-expert",
        "architect",
        "dba",
        "ux-ui",
        "designer",
        "developer",
        "developer-frontend",
        "developer-backend",
        "developer-infra",
        "qa",
        "test-automation",
        "security",
        "reviewer",
        "tech-writer",
    ],
    "frontend-ui": [
        "planner",
        "ux-ui",
        "designer",
        "developer-frontend",
        "qa",
        "test-automation",
        "reviewer",
    ],
    "backend-api": [
        "planner",
        "architect",
        "dba",
        "developer-backend",
        "qa",
        "test-automation",
        "security",
        "reviewer",
    ],
    "db-migration": [
        "planner",
        "architect",
        "dba",
        "developer-backend",
        "developer-infra",
        "qa",
        "test-automation",
        "security",
        "reviewer",
        "tech-writer",
    ],
    "docs-only": ["planner", "tech-writer", "reviewer"],
    "fmea-feature": [
        "planner",
        "fmea-expert",
        "architect",
        "dba",
        "ux-ui",
        "designer",
        "developer",
        "developer-frontend",
        "developer-backend",
        "developer-infra",
        "qa",
        "test-automation",
        "security",
        "reviewer",
        "tech-writer",
    ],
}
LABELS = {
    "quick-fix": "빠른 수정",
    "full-feature": "기능 개발",
    "frontend-ui": "화면 개선",
    "backend-api": "API 개발",
    "db-migration": "DB 변경",
    "docs-only": "문서 작성",
    "fmea-feature": "FMEA 기능",
}


def preset(preset_id):
    if preset_id not in PRESET_ROLES:
        raise DomainError(ErrorCode.INVALID_INPUT, "Unknown Workflow preset.")

    def condition(role):
        if preset_id in {"full-feature", "fmea-feature"} and role in {
            "ux-ui",
            "designer",
            "developer-frontend",
        }:
            return ("workstream", "frontend")
        if preset_id in {"full-feature", "fmea-feature"} and role == "developer-backend":
            return ("workstream", "backend")
        if preset_id in {"full-feature", "fmea-feature"} and role == "developer-infra":
            return ("workstream", "infra")
        if role == "dba" and preset_id in {"full-feature", "backend-api"}:
            return ("risk", "db")
        if role == "fmea-expert" and preset_id == "full-feature":
            return ("risk", "fmea")
        return None

    return linear_graph(
        preset_id,
        LABELS[preset_id],
        " → ".join(PRESET_ROLES[preset_id]),
        PRESET_ROLES[preset_id],
        condition,
    )


ROSTER_WORKFLOW_ID = "project-roles"


def roster_graph(roles):
    """Workflow generated from the project role roster (settings screen order)."""
    from agent_dev.domain.project_roles import GRAPH_CONNECTION
    from agent_dev.domain.project_roles import LABELS as ROLE_LABELS
    from agent_dev.domain.project_roles import gates as session_gates
    from agent_dev.domain.project_roles import normalize as normalize_roles
    from agent_dev.domain.project_roles import sessions as project_sessions

    roles = normalize_roles(roles)
    names = {row["id"]: row["name"] for row in roles["custom"]}

    def condition(role):
        if role in {"ux-ui", "designer", "developer-frontend"}:
            return ("workstream", "frontend")
        if role == "developer-backend":
            return ("workstream", "backend")
        if role == "developer-infra":
            return ("workstream", "infra")
        if role == "dba":
            return ("risk", "db")
        if role == "fmea-expert" and roles["domain"] != "fmea":
            return ("risk", "fmea")
        return None

    session_mode = roles.get("session_mode", False)
    return linear_graph(
        ROSTER_WORKFLOW_ID,
        "프로젝트 역할 순서",
        " → ".join(ROLE_LABELS.get(r) or names.get(r, r) for r in roles["order"]),
        roles["order"],
        condition,
        sessions=project_sessions(roles) if session_mode else None,
        approvals=session_gates(roles) if session_mode else (),
        # Unregistered connections are skipped with a warning when the snapshot binds tools.
        role_tools={"tech-writer": [GRAPH_CONNECTION]} if session_mode else None,
    )


def role_node(role, session=None, tools=None):
    node = {"id": role, "kind": "role", "role_id": role}
    if session:
        node["session"] = session
    if tools:
        node["tools"] = list(tools)
    return node


def linear_graph(
    graph_id, name, description, role_order, condition, sessions=None, approvals=(), role_tools=None
):
    """Linear role chain; ``sessions`` (role -> session) groups roles and ``approvals``
    names base sessions that end with an approval node."""
    nodes = []
    edges = []
    last = None
    sessions = sessions or {}
    role_tools = role_tools or {}
    current = None

    def append(node):
        nonlocal last
        nodes.append(node)
        if last:
            edges.append({"from": last, "to": node["id"]})
        last = node["id"]

    def close(session):
        if session and session.split("-")[0] in approvals:
            append({"id": "approve-" + session, "kind": "approval"})

    for role in role_order:
        session = sessions.get(role)
        if session != current:
            close(current)
            current = session
        optional = condition(role)
        if optional:
            append(
                {
                    "id": "if-" + role,
                    "kind": "condition",
                    "field": optional[0],
                    "equals": optional[1],
                }
            )
            nodes.append(role_node(role, session, role_tools.get(role)))
            edges.append({"from": last, "to": role, "when": True})
            nodes.append({"id": "join-" + role, "kind": "merge"})
            edges.extend(
                [
                    {"from": last, "to": "join-" + role, "when": False},
                    {"from": role, "to": "join-" + role},
                ]
            )
            last = "join-" + role
        else:
            append(role_node(role, session, role_tools.get(role)))
    close(current)
    role_ids = [n["id"] for n in nodes if n["kind"] == "role"]
    for node in nodes:
        if node["kind"] == "role":
            target = (
                "developer"
                if "developer" in role_ids
                else next(
                    (key for key in role_ids if key.startswith("developer-")),
                    "tech-writer" if "tech-writer" in role_ids else node["id"],
                )
            )
            node["reentry_targets"] = [
                target
                if node["role_id"] in {"qa", "reviewer", "security", "test-automation"}
                else node["id"]
            ]
    append({"id": "verify", "kind": "verification", "gate_ids": []})
    return normalize(
        {
            "schema_version": 1,
            "id": graph_id,
            "name": name,
            "description": description,
            "nodes": nodes,
            "edges": edges,
        }
    )


def custom_role_names(profile):
    from agent_dev.domain.project_roles import view

    return {row["id"]: row for row in view(profile)["custom"]}


def role_choices(profile=None):
    return sorted(ROLES) + sorted(custom_role_names(profile)) if profile else sorted(ROLES)


def check_custom_roles(graph, profile):
    from agent_dev.domain.project_roles import is_custom

    registered = custom_role_names(profile)
    unknown = [
        n["role_id"]
        for n in graph["nodes"]
        if n.get("kind") == "role" and is_custom(n["role_id"]) and n["role_id"] not in registered
    ]
    if unknown:
        raise DomainError(
            ErrorCode.INVALID_INPUT, "Unregistered project role: " + ", ".join(unknown)
        )


def trusted_metadata(metadata):
    value = deepcopy(metadata or {})
    impacts = impact(value)
    risk = value.get("risk", [])
    risks = [risk] if isinstance(risk, str) else risk if isinstance(risk, list) else []
    value["risk"] = list(
        dict.fromkeys(risks + [key for key, required in impacts.items() if required])
    )
    return value


class WorkflowDefinitions:
    def __init__(self, config_root=None, *, runtime=None):
        if runtime is None:
            from agent_dev.adapters.workflow_runtime import WorkflowRuntime

            if config_root is None:
                raise DomainError(ErrorCode.INVALID_INPUT, "Configuration root required.")
            from pathlib import Path

            runtime = WorkflowRuntime(Path(config_root))
        self.runtime = runtime
        self.store = runtime.store

    @staticmethod
    def presets():
        return [dict(preset(key), builtin=True, version=1) for key in PRESET_ROLES]

    def _profile(self, project_id):
        rows = self.store.db.rows(
            "SELECT * FROM project_profile_versions WHERE project_id=? ORDER BY version DESC LIMIT 1",
            (project_id,),
        )
        if not rows:
            raise DomainError(ErrorCode.INVALID_INPUT, "Unknown project.")
        return json.loads(rows[0]["content_json"])

    def projects(self):
        projects = self.store.db.rows("SELECT id,name,version FROM projects ORDER BY name,id")
        for project in projects:
            workflow = self._profile(project["id"]).get("workflow")
            project["workflow"] = (
                {key: workflow[key] for key in ("id", "version", "document") if key in workflow}
                if isinstance(workflow, dict)
                else None
            )
        return projects

    def list(self, project_id):
        self._profile(project_id)
        return self.store.db.rows(
            "SELECT * FROM workflow_definitions WHERE project_id=? ORDER BY name,id", (project_id,)
        )

    def show(self, project_id, workflow_id, version=None):
        self._profile(project_id)
        if workflow_id in PRESET_ROLES:
            if version not in {None, 1}:
                raise DomainError(ErrorCode.INVALID_INPUT, "Unknown preset version.")
            return {
                "document": preset(workflow_id),
                "version": 1,
                "digest": digest(preset(workflow_id)),
                "builtin": True,
            }
        rows = self.store.db.rows(
            "SELECT * FROM workflow_versions WHERE project_id=? AND workflow_id=? AND (? IS NULL OR version=?) ORDER BY version DESC LIMIT 1",
            (project_id, workflow_id, version, version),
        )
        if not rows:
            raise DomainError(ErrorCode.INVALID_INPUT, "Unknown Workflow version.")
        row = rows[0]
        versions = self.store.db.rows(
            "SELECT version,digest,created_at FROM workflow_versions WHERE project_id=? AND workflow_id=? ORDER BY version DESC",
            (project_id, workflow_id),
        )
        document = json.loads(row["document_json"])
        return {
            "document": document,
            "version": row["version"],
            "digest": row["digest"],
            "semantic_digest": semantic_digest(document),
            "layout_digest": layout_digest(document),
            "builtin": False,
            "versions": versions,
        }

    def diff(self, project_id, workflow_id, base, version):
        """Compare two saved immutable versions (WF-01); read-only."""
        before = self.show(project_id, workflow_id, base)
        after = self.show(project_id, workflow_id, version)
        return {
            "base": before["version"],
            "version": after["version"],
            "diff": version_diff(before["document"], after["document"]),
        }

    def validate(self, document, project_id=None):
        profile = self._profile(project_id) if project_id else {}
        gates = {c["name"] for c in profile.get("verification", {}).get("commands", [])}
        graph = normalize(
            document,
            modules=set(profile.get("repositories", {})) if project_id else None,
            gates=gates if project_id else None,
        )
        if project_id:
            from agent_dev.adapters.ai_providers import normalize as runner_config

            check_custom_roles(graph, profile)

            registered = {r["provider"]: r for r in profile.get("runners", []) if r.get("enabled")}
            for node in graph["nodes"]:
                if node["kind"] != "role":
                    continue
                provider = node.get("provider")
                if provider and provider not in registered:
                    raise WorkflowValidationError(
                        "Node provider must be registered and enabled.", node=node["id"], field="provider"
                    )
                fixed = node.get("model") not in {None, "", "auto"} or node.get("effort") not in {
                    None,
                    "",
                    "auto",
                }
                if fixed:
                    if not provider:
                        raise WorkflowValidationError(
                            "Fixed model/effort requires an explicit registered provider.",
                            node=node["id"],
                            field="model" if node.get("model") not in {None, "", "auto"} else "effort",
                        )
                    config = dict(
                        registered[provider].get("config", {}),
                        enabled=True,
                        routing={"mode": "fixed"},
                    )
                    for key in ("model", "effort"):
                        if node.get(key) not in {None, "", "auto"}:
                            config[key] = node[key]
                    runner_config(provider, config)
        return {"valid": True, "document": graph, "digest": digest(graph)}

    def preview(self, document, metadata=None, project_id=None):
        result = self.validate(document, project_id)
        result.update(preview(result["document"], trusted_metadata(metadata)))
        result["required_gates"] = (
            self._profile(project_id).get("verification", {}).get("required_gates", [])
            if project_id
            else []
        )
        result["roles"] = role_choices(self._profile(project_id) if project_id else None)
        return result

    def save(self, project_id, document, expected_version=0):
        graph = self.validate(document, project_id)["document"]
        if graph["id"] in PRESET_ROLES:
            raise DomainError(
                ErrorCode.POLICY_VIOLATION, "Built-in preset is immutable. Clone with a new ID."
            )
        if type(expected_version) is not int or expected_version < 0:
            raise DomainError(ErrorCode.INVALID_INPUT, "Expected Workflow version required.")
        with self.store.db.transaction() as sql:
            row = sql.execute(
                "SELECT current_version FROM workflow_definitions WHERE project_id=? AND id=?",
                (project_id, graph["id"]),
            ).fetchone()
            current = row[0] if row else 0
            if current != expected_version:
                raise DomainError(
                    ErrorCode.STALE_VERSION, "Workflow changed in another tab; draft preserved."
                )
            version = current + 1
            sql.execute(
                "INSERT INTO workflow_definitions VALUES(?,?,?,?) ON CONFLICT(project_id,id) DO UPDATE SET name=excluded.name,current_version=excluded.current_version",
                (project_id, graph["id"], graph["name"], version),
            )
            sql.execute(
                "INSERT INTO workflow_versions VALUES(?,?,?,?,?,?)",
                (
                    project_id,
                    graph["id"],
                    version,
                    digest(graph),
                    canonical(graph),
                    self.store.clock(),
                ),
            )
            self.store._event(
                sql,
                project_id,
                "WorkflowSaved",
                {"workflow_id": graph["id"], "version": version, "digest": digest(graph)},
            )
        return self.show(project_id, graph["id"], version)

    def apply(self, project_id, workflow_id, expected_profile_version, version=None):
        selected = self.show(project_id, workflow_id, version)
        profile = self._profile(project_id)
        # revise_profile checks project optimistic version; hold the selected immutable version.
        profile["workflow"] = {
            "id": workflow_id,
            "version": selected["version"],
            "digest": selected["digest"],
            "document": selected["document"],
        }
        applied = self.store.revise_profile(project_id, profile, expected_profile_version)
        return {
            "profile_version": applied,
            "workflow_id": workflow_id,
            "workflow_version": selected["version"],
        }

    def export(self, project_id, workflow_id, version=None):
        document = deepcopy(self.show(project_id, workflow_id, version)["document"])
        for node in document["nodes"]:
            for field in ("allowed_paths", "role_file", "tools", "connection_id", "scope"):
                node.pop(field, None)
            if node["kind"] == "tool":
                node["connection_id"] = "reconnect-required"
                node["scope"] = ""
                node["arguments"] = {}
        return document

    def import_document(self, project_id, document, remap=None):
        value = deepcopy(document)
        remap = remap or {}
        if value.get("id") in PRESET_ROLES:
            value["id"] = value["id"] + "-copy"
        for node in value.get("nodes", []):
            for field in ("module", "role_id", "connection_id"):
                if field in node:
                    node[field] = remap.get(field, {}).get(node[field], node[field])
            if "gate_ids" in node:
                node["gate_ids"] = [remap.get("gate_ids", {}).get(g, g) for g in node["gate_ids"]]
        return self.preview(value, project_id=project_id)

    def approve(self, task_id, node_id, actor, decision="approved", expected_version=None, note=""):
        from agent_dev.adapters.state.sqlite.safety import ExecutionSafety

        with ExecutionSafety(self.store).lease("task:" + task_id) as lease:
            return self.store.approve_node(
                task_id, node_id, actor, decision, expected_version, lease, note
            )

    def execution_state(self, task_id):
        snapshot = self.store.snapshot(task_id)
        return {
            "task_id": task_id,
            "state": self.store.task(task_id).state.value,
            "generation": self.store.generation(task_id),
            "snapshot_digest": snapshot["digest"],
            "document": snapshot["resolved"].get("workflow_graph"),
            "nodes": self.store.node_runs(task_id),
            "artifacts": self.store.db.rows(
                "SELECT path,digest,kind FROM artifacts WHERE task_id=? ORDER BY created_at",
                (task_id,),
            ),
        }


def new_project_profile(profile):
    """Only called for new projects; legacy profile loaders remain untouched."""
    from agent_dev.domain.project_roles import default_roles
    from agent_dev.domain.role_policy import instruction

    value = deepcopy(profile)
    # New projects run consecutive roles as sessions by default: one AI invocation per
    # session cut input tokens by about half in measurements. Setup can turn it off.
    value["roles"] = dict(default_roles(), session_mode=True)
    graph = roster_graph(value["roles"])
    value["workflow"] = {
        "id": graph["id"],
        "version": 1,
        "digest": digest(graph),
        "document": graph,
    }
    ids = [n["id"] for n in graph["nodes"]]
    value["role_context"] = {
        n["id"]: instruction(n["role_id"], ids) for n in graph["nodes"] if n["kind"] == "role"
    }
    value["test_paths"] = ["tests"]
    value["document_paths"] = ["README.md", "docs", "deliverables", "output"]
    return value
