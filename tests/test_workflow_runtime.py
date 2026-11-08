import ast
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_dev.adapters.publishing import PublishAdapter
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.domain.contracts import PhaseResult
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.domain.status import TaskStatus
from agent_dev.domain.transitions import TransitionContext


def git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, text=True, encoding="utf-8", errors="replace", capture_output=True
    ).stdout.strip()


@pytest.fixture
def runtime(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-q")
    git(source, "config", "user.name", "Test")
    git(source, "config", "user.email", "test@example.test")
    (source / "source.txt").write_text("base", encoding="utf-8")
    git(source, "add", "--", "source.txt")
    git(source, "commit", "-qm", "base")
    root = tmp_path / "app"
    (root / "config").mkdir(parents=True)
    (root / "config/runtime.json").write_text(
        json.dumps({"orchestrator_v2": {"execute": True, "publish": False}}), encoding="utf-8"
    )
    runtime = WorkflowRuntime(root)
    runtime.store.register_project(
        "P",
        "Project",
        {
            "repositories": {"source": {"path": str(source), "allowed_paths": ["source.txt"]}},
            "phases": [{"id": "developer", "capabilities": ["code.write"]}],
            "verification": {
                "commands": [
                    {
                        "name": "unit",
                        "repository": "source",
                        "command": [sys.executable, "-c", 'print("verified")'],
                    }
                ],
                "required_gates": ["unit"],
            },
        },
    )
    runtime.store.design(
        "P",
        "D",
        "## Scope\nApproved design\n## Acceptance\nTests pass\n## Constraints\nApproved paths only",
    )
    runtime.store.approve("design", "D", 1, "user")
    runtime.store.create_plan(
        "P", "PLAN", "D", 1, [{"id": "T", "title": "Implement", "system_name": "Topic"}]
    )
    runtime.store.approve("plan", "PLAN", 1, "user")
    runtime.store.create_snapshot("T", runtime.resolve_snapshot("T"))
    return runtime


class Runner:
    def __init__(self):
        self.calls = 0

    def execute(self, request):
        self.calls += 1
        context = json.loads(request.context)
        root = Path(context["repositories"]["source"]["worktree"])
        (root / "source.txt").write_text("implemented", encoding="utf-8")
        return PhaseResult('```json\n{"handoff_status":"pass"}\n```')


def verified(runtime):
    runner = Runner()
    assert runtime.execute("T", "run-once", runner=runner)["state"] == "verifying"
    assert runtime.execute("T", "run-once", runner=runner)["state"] == "verifying"
    assert runner.calls == 1
    assert runtime.execute("T", "verify-once", verify=True)["passed"]
    return runtime.store.snapshot("T")["resolved"]["repositories"]["source"]


def test_runtime_isolated_worktree_and_idempotency(runtime):
    binding = verified(runtime)
    assert Path(binding["path"]).joinpath("source.txt").read_text(encoding="utf-8") == "base"
    assert Path(binding["worktree"]).joinpath("source.txt").read_text(encoding="utf-8") == "implemented"
    assert runtime.store.task("T").state == TaskStatus.PR_READY
    preview = PublishAdapter(runtime).publish("T", "source", "dry", dry_run=True)
    assert preview["dry_run"]
    assert not git(Path(binding["worktree"]), "diff", "--cached", "--name-only")
    with pytest.raises(DomainError):
        PublishAdapter(runtime).publish("T", "source", "publish")


def test_publish_rejects_changes_after_verification(runtime):
    binding = verified(runtime)
    Path(binding["worktree"]).joinpath("source.txt").write_text("unverified", encoding="utf-8")
    with pytest.raises(DomainError) as error:
        PublishAdapter(runtime).publish("T", "source", "dry", dry_run=True)
    assert error.value.code == ErrorCode.VERIFICATION_FAILED


def test_feedback_original_task_does_not_regress(runtime):
    before = runtime.store.task("T")
    first = runtime.governance.feedback("T", "Fix user feedback", source_key="pr-comment-1")
    second = runtime.governance.feedback("T", "Same source updated", source_key="pr-comment-1")
    assert first == second
    planned = runtime.governance.plan_feedback(first, "FBPLAN", "D", 1)
    assert runtime.store.task(planned["task_id"]).state == TaskStatus.APPROVAL_PENDING
    assert runtime.store.task("T") == before


def test_dependency_state_is_read_from_database(runtime):
    runtime.store.create_plan(
        "P",
        "SECOND",
        "D",
        1,
        [
            {"id": "A", "title": "Dependency"},
            {"id": "B", "title": "Dependent", "depends_on": ["A"]},
        ],
    )
    runtime.store.approve("plan", "SECOND", 1, "user")
    runtime.store.create_snapshot("B", runtime.resolve_snapshot("B"))
    with runtime.safety.lease("task:B") as lease, pytest.raises(DomainError):
        runtime.store.transition(
            "B",
            TaskStatus.RUNNING,
            TransitionContext(
                expected_version=1,
                dependencies_completed=True,
                preflight_passed=True,
                lease_acquired=True,
            ),
            lease,
        )
    assert runtime.store.task("B").state == TaskStatus.READY


def test_application_has_only_domain_and_port_dependencies():
    root = Path(__file__).parents[1] / "agent_dev/application"
    for path in root.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                assert module.startswith(
                    ("agent_dev.domain", "agent_dev.ports")
                ) or not module.startswith("agent_dev.")
            if isinstance(node, ast.Import):
                assert not any(
                    alias.name in {"subprocess", "sqlite3", "os"} for alias in node.names
                )


def test_process_guard_stops_provider_when_parent_is_killed(runtime, tmp_path):
    marker = tmp_path / "late-write"
    guard = Path(__file__).parents[1] / "agent_dev/adapters/process_guard.py"
    driver = """import os,subprocess,sys,time
from pathlib import Path
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
runtime=WorkflowRuntime(Path(sys.argv[1]))
with runtime.safety.lease('task:T') as lease:
    subprocess.Popen([sys.executable,sys.argv[2],'--parent',str(os.getpid()),'--db',str(runtime.store.db.path),'--resource',lease['resource'],'--holder',lease['holder'],'--token',str(lease['token']),'--',sys.executable,'-c',sys.argv[3]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    print('started',flush=True)
    time.sleep(20)
"""
    child = f"import time;from pathlib import Path;time.sleep(1.5);Path({str(marker)!r}).write_text('unsafe')"
    process = subprocess.Popen(
        [sys.executable, "-c", driver, str(runtime.root), str(guard), child],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        assert process.stdout.readline().strip() == "started"
        process.kill()
        process.communicate(timeout=5)
        time.sleep(1.8)
        assert not marker.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()




def test_design_sufficiency_and_snapshot_binding(runtime):
    runtime.store.design("P", "INCOMPLETE", "Only a title")
    with pytest.raises(DomainError) as error:
        runtime.store.approve("design", "INCOMPLETE", 1, "user")
    assert error.value.code == ErrorCode.APPROVAL_REQUIRED
    runtime.store.create_plan("P", "ALTERED", "D", 1, [{"id": "ALTERED", "title": "Altered"}])
    runtime.store.approve("plan", "ALTERED", 1, "user")
    with pytest.raises(DomainError):
        runtime.store.create_snapshot("ALTERED", {"design": "unapproved instructions"})
    assert runtime.store.task("ALTERED").snapshot_id is None


def test_checkpoint_and_prompt_have_owner_and_digest(runtime):
    verified(runtime)
    with runtime.safety.lease("task:T") as lease:
        path = runtime.artifacts.checkpoint("T", lease)
    content = Path(path).read_text(encoding="utf-8")
    assert "## Harness Control" in content
    assert "## Handoff" in content
    assert runtime.store.snapshot("T")["digest"] in content
    kinds = {row["kind"] for row in runtime.store.db.rows("SELECT * FROM artifacts")}
    assert kinds >= {"phase_prompt", "phase_output", "verification", "checkpoint"}
    assert all(row["attempt_id"] for row in runtime.store.db.rows("SELECT * FROM artifacts"))


def test_process_crash_recovers_fenced_attempt(runtime):
    snapshot = runtime.store.snapshot("T")
    from agent_dev.domain.content import digest

    key = digest(["T", "developer", snapshot["digest"], 0])
    script = """import sys,time
from pathlib import Path
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.domain.status import TaskStatus
from agent_dev.domain.transitions import TransitionContext
runtime=WorkflowRuntime(Path(sys.argv[1]))
lease=runtime.safety.acquire('task:T','crash-process',ttl=0.5)
runtime.store.transition('T',TaskStatus.RUNNING,TransitionContext(expected_version=1,preflight_passed=True,lease_acquired=True),lease)
runtime.store.begin_attempt('T','developer',sys.argv[2],lease)
print('started',flush=True)
time.sleep(20)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(runtime.root), key],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        assert process.stdout.readline().strip() == "started"
        process.kill()
        process.communicate(timeout=5)
        time.sleep(0.55)
        assert runtime.execute("T", "recover", runner=Runner())["state"] == "verifying"
        attempts = runtime.store.db.rows("SELECT * FROM phase_attempts ORDER BY attempt_no")
        assert [a["state"] for a in attempts] == ["interrupted", "passed"]
        assert attempts[1]["lease_token"] > attempts[0]["lease_token"]
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


@pytest.mark.parametrize("ci", ["SUCCESS", "FAILURE", "SKIPPED", None])
def test_publish_pr_idempotency_and_named_ci_gate(runtime, tmp_path, ci):
    # A second immutable plan carries explicit CI requirements.
    profile = json.loads(
        runtime.store.db.rows("SELECT content_json FROM project_profile_versions")[0][
            "content_json"
        ]
    )
    profile["repositories"]["source"]["required_ci_checks"] = ["unit"]
    runtime.store.revise_profile("P", profile, 0)
    runtime.store.create_plan("P", "PUBLISH", "D", 1, [{"id": "PUB", "title": "Publish"}])
    runtime.store.approve("plan", "PUBLISH", 1, "user")
    runtime.store.create_snapshot("PUB", runtime.resolve_snapshot("PUB"))
    assert runtime.execute("PUB", "pub-run", runner=Runner())["state"] == "verifying"
    assert runtime.execute("PUB", "pub-verify", verify=True)["passed"]
    binding = runtime.store.snapshot("PUB")["resolved"]["repositories"]["source"]
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(bare)], check=True)
    git(Path(binding["path"]), "remote", "add", "origin", str(bare))
    config = runtime.root / "config/runtime.json"
    config.write_text(json.dumps({"orchestrator_v2": {"execute": True, "publish": True}}), encoding="utf-8")
    created = []

    def gh(arguments):
        if arguments[:2] == ["pr", "create"]:
            created.append(1)
            return "https://example.test/pull/1"
        sha = git(Path(binding["worktree"]), "rev-parse", "HEAD")
        if arguments[:2] == ["pr", "list"]:
            return json.dumps([{"number": 1, "headRefOid": sha}] if created else [])
        return json.dumps(
            {
                "headRefOid": sha,
                "state": "MERGED",
                "statusCheckRollup": [{"name": "unit", "conclusion": ci}] if ci else [],
            }
        )

    publishing = PublishAdapter(runtime, external=gh)
    first = publishing.publish("PUB", "source", "publish-1")
    second = publishing.publish("PUB", "source", "publish-2")
    assert first["id"] == second["id"]
    assert created == [1]
    if ci == "SUCCESS":
        completed = publishing.complete("PUB", first["id"], "user")
        assert completed["state"] == "completed"
        # No graph is kept for this project, so the post-merge refresh is skipped.
        assert completed["code_graph"] == {"state": "skipped", "reason": "no_graph"}
    else:
        with pytest.raises(DomainError):
            publishing.complete("PUB", first["id"], "user")
        assert runtime.store.task("PUB").state == TaskStatus.PR_READY


def test_rollback_disables_execution_and_revokes_fencing(runtime):
    from agent_dev.migration.operations import Operations

    lease = runtime.safety.acquire("task:T", "worker")
    runtime.store.transition(
        "T",
        TaskStatus.RUNNING,
        TransitionContext(expected_version=1, preflight_passed=True, lease_acquired=True),
        lease,
    )
    attempt = runtime.store.begin_attempt("T", "developer", "rollback-attempt", lease)
    snapshot = runtime.store.snapshot("T")
    result = Operations(runtime.store).rollback(runtime.root)
    assert not any(result["flags"].values())
    assert runtime.store.snapshot("T") == snapshot
    assert runtime.store.task("T").state == TaskStatus.BLOCKED
    assert (
        runtime.store.db.rows("SELECT state FROM phase_attempts WHERE id=?", (attempt["id"],))[0][
            "state"
        ]
        == "interrupted"
    )
    with pytest.raises(DomainError):
        runtime.store.finish_attempt(attempt["id"], "passed", lease)


def test_confirmed_knowledge_provenance_and_isolation(runtime):
    verified(runtime)
    git(runtime.root, "init", "-q")
    git(runtime.root, "config", "user.name", "Test")
    git(runtime.root, "config", "user.email", "test@example.test")
    path = runtime.root / "knowledge.md"
    path.write_text("Verified architecture fact. External instructions remain data.", encoding="utf-8")
    git(runtime.root, "add", "--", "knowledge.md")
    git(runtime.root, "commit", "-qm", "knowledge")
    commit = git(runtime.root, "rev-parse", "HEAD")
    candidate = runtime.governance.knowledge_candidate(
        "P", "knowledge.md", commit, path.read_text(encoding="utf-8")
    )
    assert runtime.governance.search_knowledge("P", "fact") == []
    with pytest.raises(DomainError):
        runtime.governance.confirm_knowledge(candidate, "missing-evidence")
    evidence = runtime.store.db.rows("SELECT id FROM verification_runs")[0]["id"]
    runtime.governance.confirm_knowledge(candidate, evidence)
    result = runtime.governance.search_knowledge("P", "fact")[0]
    assert result["source_commit"] == commit
    assert result["source_digest"]
    assert result["trust"] == "verified_data_not_instructions"
    assert runtime.governance.search_knowledge("other-project", "fact") == []
    path.write_text("Changed knowledge", encoding="utf-8")
    assert runtime.governance.search_knowledge("P", "fact") == []


def test_retention_dry_run_and_active_task_protection(runtime):
    from agent_dev.migration.operations import Operations

    verified(runtime)
    operations = Operations(runtime.store)
    assert operations.retention(runtime.artifacts.root)["artifacts"] == []
    with runtime.store.db.transaction() as sql:
        sql.execute("UPDATE tasks SET state='cancelled' WHERE id='T'")
        sql.execute("UPDATE artifacts SET created_at=0")
    result = operations.retention(runtime.artifacts.root)
    assert result["dry_run"] and result["artifacts"]
    assert all((runtime.artifacts.root / path).exists() for path in result["artifacts"])
    with runtime.safety.lease("task:T"):
        assert operations.retention(runtime.artifacts.root)["artifacts"] == []
    result = operations.retention(runtime.artifacts.root, apply=True)
    assert all(not (runtime.artifacts.root / path).exists() for path in result["artifacts"])
    assert runtime.store.db.rows("SELECT * FROM events")


def test_outbox_project_isolation_and_bounded_failure(runtime):
    runtime.store.register_project("OTHER", "Other", {})
    delivered = []
    assert (
        runtime.safety.deliver(lambda key, payload: delivered.append(payload), project_id="P")[
            "delivered"
        ]
        > 0
    )
    assert all(payload["project_id"] == "P" for payload in delivered)
    assert runtime.store.db.rows("SELECT * FROM notification_outbox WHERE state='pending'")
    clock = [time.time()]
    runtime.store.clock = lambda: clock[0]
    runtime.safety.clock = runtime.store.clock

    def unavailable(key, payload):
        raise OSError("network unavailable")

    for _ in range(3):
        assert runtime.safety.deliver(unavailable, project_id="OTHER")["failed"] == 1
        clock[0] += 10
    assert runtime.store.db.rows("SELECT * FROM notification_outbox WHERE state='dead'")
    assert runtime.safety.deliver(unavailable, project_id="OTHER")["failed"] == 0




def test_runner_selection_is_recorded_without_changing_snapshot(runtime):
    from agent_dev.domain.model_routing import select_model

    original = runtime.store.snapshot("T")
    selected = select_model({"model": "approved-model", "effort": "medium"}, {}, "developer")

    class SelectedRunner:
        def execute(self, request):
            return PhaseResult('```json\n{"handoff_status":"pass"}\n```', "codex_cli", {}, selected)

    assert runtime.execute("T", "routed-run", runner=SelectedRunner())["state"] == "verifying"
    rows = runtime.store.db.rows("SELECT payload_json FROM events WHERE kind='RunnerUsage'")
    assert json.loads(rows[0]["payload_json"])["selection"] == selected
    assert runtime.store.snapshot("T") == original


def test_snapshot_binds_bundled_rules_and_ignores_project_role_files(runtime):
    from agent_dev import defaults

    resolved = runtime.store.snapshot("T")["resolved"]
    assert resolved["operating_rules"] == defaults.operating_rules()
    assert json.loads(runtime.artifacts.context("T", "developer"))["operating_rules"] == (
        defaults.operating_rules()
    )

    (runtime.project_root / "agent").mkdir(exist_ok=True)
    (runtime.project_root / "agent/planner.md").write_text("CUSTOMER-ROLE-RULE", encoding="utf-8")
    source = resolved["repositories"]["source"]["path"]
    runtime.store.register_project(
        "P2",
        "Project 2",
        {
            "repositories": {"source": {"path": source, "allowed_paths": ["source.txt"]}},
            "phases": [{"id": "planner", "role_file": "agent/planner.md"}],
            "verification": {"commands": [], "required_gates": []},
        },
    )
    runtime.store.design(
        "P2", "D2", "## Scope\nApproved\n## Acceptance\nTests pass\n## Constraints\nPaths only"
    )
    runtime.store.approve("design", "D2", 1, "user")
    runtime.store.create_plan("P2", "PLAN2", "D2", 1, [{"id": "T2", "title": "Plan"}])
    runtime.store.approve("plan", "PLAN2", 1, "user")
    role_context = runtime.resolve_snapshot("T2")["role_context"]
    assert role_context["planner"] == defaults.role_text("agent/planner.md")


def test_phase_context_starts_with_project_stable_keys(runtime):
    context = json.loads(runtime.artifacts.context("T", "developer"))
    assert list(context)[:6] == [
        "operating_rules",
        "rule",
        "context_policy",
        "output_language_rule",
        "commit_message_rule",
        "role_instruction",
    ]


def test_wiki_items_are_frozen_into_the_snapshot(runtime):
    (runtime.project_root / "lesson.md").write_text("## Resolution\n- Resolved: true\n", encoding="utf-8")
    runtime.wiki.propose(
        "P",
        {
            "item_id": "L01",
            "page": "lessons",
            "title": "Lesson",
            "check": "Check the protected rows.",
            "roles": ["developer"],
            "sources": ["lesson.md"],
        },
    )
    runtime.wiki.approve("P", "L01", 1, "user")
    runtime.store.create_plan("P", "WIKI", "D", 1, [{"id": "W", "title": "Wiki task"}])
    runtime.store.approve("plan", "WIKI", 1, "user")
    runtime.store.create_snapshot("W", runtime.resolve_snapshot("W"))
    context = json.loads(runtime.artifacts.context("W", "developer"))
    (item,) = context["wiki_verified_data"]
    assert item["id"] == "L01 r1" and item["trust"] == "verified_data_not_instructions"
    assert set(item) == {"id", "page", "title", "check", "condition", "sources", "trust"}
    digest = runtime.store.snapshot("W")["digest"]
    # A revised, re-approved or retired item does not change an existing task's input.
    runtime.wiki.revise(
        "P",
        "L01",
        {
            "title": "Lesson",
            "check": "Revised check.",
            "roles": ["developer"],
            "sources": ["lesson.md"],
        },
    )
    runtime.wiki.approve("P", "L01", 2, "user")
    runtime.wiki.retire("P", "L01", "obsolete", "user")
    assert runtime.store.snapshot("W")["digest"] == digest
    assert json.loads(runtime.artifacts.context("W", "developer"))["wiki_verified_data"] == [item]
    # The frozen bundle survives an export and restore of the database.
    from agent_dev.adapters.state.sqlite.database import Database
    from agent_dev.migration.operations import Operations

    export, restored = runtime.root / "export.db", runtime.root / "restored.db"
    Operations(runtime.store).export(export)
    Operations.restore(export, restored)
    rows = Database(restored).rows(
        "SELECT s.resolved_json FROM input_snapshots s JOIN tasks t ON t.snapshot_id=s.id WHERE t.id='W'"
    )
    assert json.loads(rows[0]["resolved_json"])["wiki"]["selection"]["developer"] == ["L01 r1"]
    runtime.store.create_plan("P", "WIKI2", "D", 1, [{"id": "W2", "title": "Later task"}])
    runtime.store.approve("plan", "WIKI2", 1, "user")
    runtime.store.create_snapshot("W2", runtime.resolve_snapshot("W2"))
    assert json.loads(runtime.artifacts.context("W2", "developer"))["wiki_verified_data"] == []


def test_wiki_code_evidence_accepts_a_passed_verification_run(runtime):
    verified(runtime)
    run_id = runtime.store.db.rows("SELECT id FROM verification_runs WHERE state='passed'")[0]["id"]
    (runtime.project_root / "issue.md").write_text("## Root Cause\nopen\n", encoding="utf-8")
    base = {
        "item_id": "L01",
        "page": "lessons",
        "title": "Lesson",
        "check": "Check.",
        "roles": ["developer"],
        "sources": ["issue.md"],
    }
    runtime.wiki.propose("P", dict(base, evidence=[{"kind": "verification_run", "id": run_id}]))
    runtime.wiki.approve("P", "L01", 1, "user")
    assert runtime.wiki.active("P")[0]["approval"]["verification"]["resolved_by"] == [run_id]
    runtime.store.register_project("OTHER", "Other", {})
    with pytest.raises(DomainError):
        runtime.wiki.propose(
            "OTHER",
            dict(base, item_id="L02", evidence=[{"kind": "verification_run", "id": run_id}]),
        )


def test_wiki_prompt_masks_secrets_registered_after_the_snapshot(runtime):
    value = "registered-later-secret-77"
    (runtime.project_root / "lesson.md").write_text("## Resolution\n- Resolved: true\n", encoding="utf-8")
    runtime.wiki.propose(
        "P",
        {
            "item_id": "L01",
            "page": "lessons",
            "title": "Lesson",
            "check": f"never print {value}",
            "roles": ["developer"],
            "sources": ["lesson.md"],
        },
    )
    runtime.wiki.approve("P", "L01", 1, "user")
    runtime.store.create_plan("P", "WIKI", "D", 1, [{"id": "W", "title": "Wiki task"}])
    runtime.store.approve("plan", "WIKI", 1, "user")
    runtime.store.create_snapshot("W", runtime.resolve_snapshot("W"))
    runtime.store.secrets = (*runtime.store.secrets, value)
    context = runtime.artifacts.context("W", "developer")
    assert value not in context
    assert json.loads(context)["wiki_verified_data"][0]["id"] == "L01 r1"


@pytest.mark.parametrize("work", ["", "change", "commit"])
def test_cancel_removes_only_worktrees_without_work(runtime, capsys, work):
    from agent_dev.adapters.command_line import main

    binding = runtime.store.snapshot("T")["resolved"]["repositories"]["source"]
    worktree = Path(binding["worktree"])
    if work:
        (worktree / "source.txt").write_text("unsaved work", encoding="utf-8")
    if work == "commit":
        git(worktree, "-c", "user.name=T", "-c", "user.email=t@t", "commit", "-qam", "work")
    main(["task", "cancel", "T", "--root", str(runtime.root), "--json"])
    result = json.loads(capsys.readouterr().out)["result"]
    assert result["state"] == "cancelled"
    branches = git(Path(binding["path"]), "branch", "--list", binding["branch"])
    if work:
        assert result["worktrees"]["source"].startswith("kept")
        assert worktree.is_dir() and branches
    else:
        assert result["worktrees"] == {"source": "removed"}
        assert not worktree.exists() and not branches


@pytest.mark.parametrize("checkout", ["clean", "dirty", "elsewhere"])
def test_local_completion_fast_forwards_the_default_branch(runtime, checkout):
    """ADR 0003: without named CI checks a task closes on this machine."""
    binding = verified(runtime)
    source = Path(binding["path"])
    git(source, "branch", "-M", "main")
    (runtime.root / "config/runtime.json").write_text(
        json.dumps({"orchestrator_v2": {"execute": True, "publish": True}}), encoding="utf-8"
    )
    publishing = PublishAdapter(runtime, external=lambda arguments: pytest.fail("no GitHub call"))
    assert publishing.publish("T", "source", "local-dry", dry_run=True)["completion"] == "local"
    record = publishing.publish("T", "source", "local-1")
    assert record["pr_number"] is None
    assert publishing.publish("T", "source", "local-2")["id"] == record["id"]
    assert publishing.review("T") == {
        "task_id": "T",
        "state": "user_review",
        "ci_passed": True,
        "merged": False,
    }
    if checkout == "dirty":
        (source / "source.txt").write_text("user edit", encoding="utf-8")
        with pytest.raises(DomainError, match="커밋하지 않은 변경"):
            publishing.complete("T", record["id"], "user")
        assert runtime.store.task("T").state == TaskStatus.USER_REVIEW
        return
    if checkout == "elsewhere":
        git(source, "switch", "-q", "-c", "other")
    completed = publishing.complete("T", record["id"], "user")
    assert completed["state"] == "completed"
    assert git(source, "rev-parse", "main") == record["commit_sha"]
    if checkout == "clean":
        assert (source / "source.txt").read_text(encoding="utf-8") == "implemented"


def test_named_ci_checks_keep_github_completion():
    from agent_dev.adapters.publishing import completion_mode

    assert completion_mode({"repositories": {"a": {}}}) == "local"
    assert completion_mode({"repositories": {"a": {"required_ci_checks": ["unit"]}}}) == "github"
    assert completion_mode({"completion": {"mode": "github"}, "repositories": {}}) == "github"
    assert (
        completion_mode(
            {"completion": {"mode": "local"}, "repositories": {"a": {"required_ci_checks": ["u"]}}}
        )
        == "local"
    )


def test_dry_run_rejection_without_root_prints_the_envelope(capsys, monkeypatch, tmp_path):
    """A rejection before root resolution crashed while redacting secrets."""
    from agent_dev.adapters.command_line import main

    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit):
        main(["task", "resume", "T", "--dry-run", "--json"])
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "INVALID_INPUT"
    assert "--dry-run" in error["message"]


def test_worktree_links_installed_dependencies_hidden_from_git(tmp_path):
    """A fresh worktree had no node_modules, so `npm run test` found no vitest."""
    from contextlib import contextmanager

    from agent_dev.adapters.worktrees import WorktreeManager

    class Safety:
        @contextmanager
        def lease(self, resource):
            yield

    source = tmp_path / "web"
    (source / "node_modules/.bin").mkdir(parents=True)
    (source / ".gitignore").write_text("node_modules/\n", encoding="utf-8")  # directories only, not links
    git(source.parent, "init", "-q", str(source))
    git(source, "add", ".gitignore")
    git(source, "-c", "user.name=T", "-c", "user.email=t@t", "commit", "-qm", "init")
    manager = WorktreeManager(tmp_path / "worktrees", Safety())
    binding = manager.prepare("T1", {"frontend": {"path": str(source)}})["frontend"]
    link = Path(binding["worktree"]) / "node_modules"
    assert link.is_symlink() and link.resolve() == (source / "node_modules").resolve()
    assert git(Path(binding["worktree"]), "status", "--porcelain").strip() == ""
    assert git(source, "status", "--porcelain").strip() == ""
    # Preparing again keeps one link; an untouched worktree is still removable.
    manager.prepare("T1", {"frontend": {"path": str(source)}})
    assert manager.release(binding) == "removed"
    assert (source / "node_modules/.bin").is_dir()
