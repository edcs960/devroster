"""Opt-in bounded real provider flow in a disposable Git repository, never production."""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def verify(provider, root, model=""):
    from agent_dev.adapters.task_queries import TaskQueries
    from agent_dev.adapters.workflow_runtime import WorkflowRuntime
    from agent_dev.adapters.workspace import initialize

    project = root / "project"
    (project / "src").mkdir(parents=True)
    (project / "tests").mkdir()
    (project / "src/value.py").write_text("def value():\n    return 1\n", encoding="utf-8")
    (project / "tests/test_value.py").write_text(
        "import unittest\nfrom src.value import value\nclass ValueTest(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(value(), 2)\n", encoding="utf-8"
    )
    (project / ".gitignore").write_text("/.agent-dev/\n/output/\n__pycache__/\n", encoding="utf-8")
    for args in [
        ("init", "-q"),
        ("config", "user.name", "Agent Dev Fixture"),
        ("config", "user.email", "fixture@example.test"),
        ("add", "--", "src", "tests", ".gitignore"),
        ("commit", "-qm", "baseline"),
    ]:
        subprocess.run(
            ["git", "-C", str(project), *args], check=True, capture_output=True, timeout=20
        )
    initialize(project, name="LiveFixture", project_id="LIVE", runner=provider, paths=["src"])
    runtime = WorkflowRuntime(project)
    profile_path = project / ".agent-dev/config/profile.json"
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    profile["runners"][0]["authenticated"] = True
    if model:
        profile["runners"][0]["config"]["model"] = model
    # The default roster runs as sessions (analysis, develop, verify, docs): about four
    # calls, plus room for one format retry.
    profile["execution_limits"] = {
        "max_attempts": 6,
        "max_runner_seconds": 900,
        "phase_timeout_seconds": 300,
        "max_reentries": 0,
    }
    profile["verification"] = {
        "commands": [
            {
                "name": "unit",
                "repository": "source",
                "command": [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                "required": True,
                "timeout_seconds": 30,
            }
        ],
        "required_gates": ["unit"],
    }
    for role in profile["role_context"]:
        profile["role_context"][role] += (
            " Keep this fixture phase under 100 words. Inspect only src/value.py and tests/test_value.py; no networking, external tools, credentials, publishing or commits. Planner/QA must not write source. Developer changes only return 1 to return 2 in src/value.py. Return final Harness Control fenced JSON with handoff_status pass if completed."
        )
    profile_path.write_text(json.dumps(profile), encoding="utf-8")
    runtime.store.revise_profile("LIVE", profile, 0)
    runtime.store.design(
        "LIVE",
        "D",
        "## Scope\nChange src/value.py return value from1 to2.\n## Acceptance\nunittest passes, source checkout preserved.\n## Constraints\nOnly src/value.py; no networking, credentials, commits or external services.",
    )
    runtime.store.approve("design", "D", 1, "fixture")
    runtime.store.create_plan(
        "LIVE",
        "PLAN",
        "D",
        1,
        [{"id": "LIVE-001", "title": "Return2 fixture", "system_name": "LiveFixture"}],
    )
    runtime.store.approve("plan", "PLAN", 1, "fixture")
    runtime.store.create_snapshot("LIVE-001", runtime.resolve_snapshot("LIVE-001"))
    runtime_file = project / ".agent-dev/config/runtime.json"
    config = json.loads(runtime_file.read_text(encoding="utf-8"))
    config["orchestrator_v2"]["execute"] = True
    runtime_file.write_text(json.dumps(config), encoding="utf-8")
    from agent_dev.runners.claude_cli import ClaudeCliRunner
    from agent_dev.runners.codex_cli import CodexCliRunner

    failure_hints = []
    selected = CodexCliRunner if provider == "codex_cli" else ClaudeCliRunner
    original_run = selected.run

    def diagnostic_run(self, *args, **kwargs):
        try:
            return original_run(self, *args, **kwargs)
        except Exception as error:
            # Only fixed diagnostic categories; never retain account/provider output.
            message = str(error).lower()
            markers = {
                "unsupported_argument": "unexpected argument",
                "unsupported_model": "not supported",
                "invalid_configuration": "error loading",
                "transport_failure": "stream disconnected",
                "missing_output": "no such file",
                "python_scope_error": "local variable",
                "provider_error": "error:",
                "quota": "usage limit",
                "auth": "unauthorized",
            }
            failure_hints.extend(key for key, value in markers.items() if value in message)
            raise

    selected.run = diagnostic_run
    from agent_dev.adapters.workflow_definitions import WorkflowDefinitions

    approved = []
    try:
        execution = runtime.execute("LIVE-001", "bounded-live-run")
        # The default workflow stops at its analysis approval; the fixture approves it.
        for attempt in range(3):
            if execution.get("reason") != "AWAITING_WORKFLOW_APPROVAL":
                break
            workflow = WorkflowDefinitions(project)
            for node_id, row in workflow.execution_state("LIVE-001")["nodes"].items():
                if row["state"] == "awaiting_approval":
                    workflow.approve(
                        "LIVE-001", node_id, "fixture", "approved", row["version"]
                    )
                    approved.append(node_id)
            execution = runtime.execute("LIVE-001", f"bounded-live-resume-{attempt}")
    finally:
        selected.run = original_run
    verification = (
        runtime.execute("LIVE-001", "bounded-live-verify", verify=True)
        if execution.get("state") == "verifying"
        else None
    )
    preserved = (project / "src/value.py").read_text(encoding="utf-8") == "def value():\n    return 1\n"
    report = {
        "provider": provider,
        "selected_model": model or "inherited CLI configuration",
        "failure_hints": failure_hints,
        "approved_gates": approved,
        "execution": execution,
        "verification_passed": bool(verification and verification.get("passed")),
        "source_checkout_preserved": preserved,
        "publication_performed": False,
        "usage": TaskQueries(project).usage("LIVE-001"),
        "passed": bool(verification and verification.get("passed") and preserved),
    }
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="Explicitly authorize up to 6 short model calls using the selected logged-in CLI",
    )
    parser.add_argument("--provider", choices=["codex_cli", "claude_cli"], default="codex_cli")
    parser.add_argument("--report", type=Path)
    parser.add_argument(
        "--model",
        default="",
        help="Optional model for this disposable fixture only; global config is unchanged",
    )
    args = parser.parse_args()
    if not args.run:
        parser.error("--run is required; default verification never calls a provider")
    with tempfile.TemporaryDirectory(prefix="agent-dev-live-") as folder:
        try:
            report = verify(args.provider, Path(folder), args.model)
        except Exception as exc:  # noqa: BLE001 -- do not expose provider stderr/identity
            report = {
                "passed": False,
                "provider": args.provider,
                "error_type": type(exc).__name__,
                "raw_provider_output": "withheld",
            }
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if args.report:
        args.report.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
