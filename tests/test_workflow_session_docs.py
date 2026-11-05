"""ADR 0002 W7: a graph lookup runs between the verify and docs sessions."""

pytest_plugins = ["test_workflow_runtime"]

from test_workflow_dag import prepare
from test_workflow_sessions import SessionRunner, session_roster

from agent_dev.adapters.integrations import Integrations
from agent_dev.adapters.workflow_definitions import roster_graph
from agent_dev.domain.project_roles import GRAPH_CONNECTION
from agent_dev.domain.workflow_graph import execution_order, session_spans

METADATA = {"workstreams": ["frontend", "backend"], "risk": ["db"]}


def resolved(runtime):
    return runtime.store.snapshot("DAG")["resolved"]


def test_unregistered_graph_connection_is_skipped_with_warning(runtime):
    prepare(runtime, roster_graph(session_roster()), METADATA)
    value = resolved(runtime)
    assert value["tool_preparation_warnings"] == [
        {
            "node": "tech-writer",
            "connection_id": GRAPH_CONNECTION,
            "state": "skipped",
            "reason": "CONNECTION_NOT_REGISTERED",
        }
    ]
    assert not [n for n in value["workflow_graph"]["nodes"] if n["kind"] == "tool"]


def test_registered_graph_connection_runs_between_verify_and_docs(runtime):
    Integrations(runtime.root).configure(
        {"id": GRAPH_CONNECTION, "provider": "graphify", "scopes": ["graphify-out"]}
    )
    prepare(runtime, roster_graph(session_roster()), METADATA)
    graph = resolved(runtime)["workflow_graph"]
    order = [n["id"] for n in execution_order(graph)]
    tools = [n for n in graph["nodes"] if n["kind"] == "tool"]
    assert len(tools) == 1
    assert tools[0]["connection_id"] == GRAPH_CONNECTION
    assert tools[0]["required"] is False
    assert order.index("reviewer") < order.index(tools[0]["id"]) < order.index("tech-writer")
    spans = session_spans(graph)
    assert spans["docs"] == ["tech-writer"]
    assert tools[0]["id"] not in spans["verify"]

    runner = SessionRunner()
    runtime.execute("DAG", "first", runner=runner)
    runs = runtime.store.node_runs("DAG")
    from agent_dev.adapters.workflow_definitions import WorkflowDefinitions

    WorkflowDefinitions(runtime=runtime).approve(
        "DAG", "approve-analysis", "user", expected_version=runs["approve-analysis"]["version"]
    )
    assert runtime.execute("DAG", "second", runner=runner)["state"] == "verifying"
    # The optional lookup never blocks documentation, whether or not the tool host answers.
    runs = runtime.store.node_runs("DAG")
    assert runs[tools[0]["id"]]["state"] == "passed"
    assert runs[tools[0]["id"]]["result"]["state"] == "skipped"  # no tool host in tests
    assert runs["tech-writer"]["state"] == "passed"
    assert runner.calls[-1] == ("tech-writer",)


def test_skipped_optional_tool_does_not_disable_next_role(runtime):
    """Regression (found in W7): an optional tool skip used to skip the role after it."""
    from test_workflow_dag import Passing

    graph = {
        "schema_version": 1,
        "id": "tool",
        "name": "Tool",
        "nodes": [
            {
                "id": "lookup",
                "kind": "tool",
                "connection_id": "missing",
                "operation": "query_graph",
                "capability": "graph.read",
                "required": False,
            },
            {"id": "developer", "kind": "role", "role_id": "developer"},
        ],
        "edges": [{"from": "lookup", "to": "developer"}],
    }
    prepare(runtime, graph)
    runner = Passing()
    assert runtime.execute("DAG", "run", runner=runner)["state"] == "verifying"
    assert runner.calls == ["developer"]
    assert runtime.store.node_runs("DAG")["lookup"]["result"]["state"] == "skipped"
