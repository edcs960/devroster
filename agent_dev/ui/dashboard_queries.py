"""Read-only task and doctor views shared by the dashboard and the Studio server."""

from urllib.parse import parse_qs

from agent_dev.adapters.task_queries import TaskQueries
from agent_dev.adapters.workspace import metadata
from agent_dev.domain.content import redact


def read_view(root, parsed):
    """Shared read-only API used by both local web entry points."""
    query = TaskQueries(root)
    if parsed.path == "/api/tasks":
        params = parse_qs(parsed.query)
        allowed = {"limit", "page", "search", "state", "project"}
        if set(params) - allowed or any(len(values) != 1 for values in params.values()):
            raise ValueError("Invalid filters")
        result = query.tasks(
            int(params.get("limit", ["100"])[0]),
            page=int(params.get("page", ["1"])[0]),
            search=params.get("search", [""])[0],
            state=params.get("state", [""])[0],
            project=params.get("project", [""])[0],
        )
        result["project"] = redact(metadata(root) or {"name": query.workspace.name}, query.secrets)
        return result
    if parsed.path == "/api/task":
        task_id = parse_qs(parsed.query).get("id", [""])[0]
        if not task_id or len(task_id) > 200:
            raise ValueError("Select a valid task ID.")
        from agent_dev.ui.presentation import task_display

        result = query.explain(task_id)
        result["display"] = task_display(result)
        return result
    if parsed.path == "/api/doctor":
        from agent_dev.adapters.diagnostics import diagnose

        return diagnose(root)
    raise ValueError("Unknown view.")
