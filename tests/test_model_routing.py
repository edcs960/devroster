"""Routing decisions, real runner argv and captured project settings."""

import json
import threading

import pytest

from agent_dev.adapters.model_catalog import codex_models
from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.adapters.studio_generation import StudioJobs
from agent_dev.adapters.studio_store import StudioStore
from agent_dev.adapters.workspace import configuration_root
from agent_dev.application.studio import Studio
from agent_dev.core.models import AgentConfig, PipelineStep, Task
from agent_dev.domain.errors import DomainError
from agent_dev.domain.model_routing import classify, routing_policy, select_model
from agent_dev.runners.claude_cli import ClaudeCliRunner
from agent_dev.runners.codex_cli import CodexCliRunner


def policy():
    return {
        "mode": "auto",
        "max_effort": "high",
        "profiles": {
            tier: {"model": name, "supported_efforts": ["low", "medium", "high"]}
            for tier, name in zip(("simple", "standard", "complex"), ("fast", "balanced", "deep"))
        },
    }


@pytest.mark.parametrize(
    "title,phase,tier",
    [
        ("README 수정", "developer", "simple"),
        ("목록 기능 구현과 README 추가", "developer", "standard"),
        ("목록 구현", "developer", "standard"),
        ("화면 검토", "architect", "complex"),
        ("문구 수정", "security", "complex"),
        ("DB migration", "developer", "complex"),
        ("권한 추가", "developer", "complex"),
        ("명세", "studio-specs", "complex"),
        ("사용법", "tech-writer", "simple"),
    ],
)
def test_balanced_classification(title, phase, tier):
    choice = select_model({"routing": policy()}, {"title": title}, phase)
    assert choice["tier"] == tier
    assert choice["model"] == policy()["profiles"][tier]["model"]
    assert choice["effort"] == {"simple": "low", "standard": "medium", "complex": "high"}[tier]
    assert choice["reason"]


def test_large_multiarea_and_uncertain_tasks():
    assert classify({"workstreams": ["frontend", "backend", "infra"]}, "developer")[0] == "complex"
    assert classify({"scope": ["x" * 12001]}, "developer")[0] == "complex"
    assert classify({}, "unknown")[0] == "standard"


def test_supported_levels_and_ceiling_constrain_choice_without_mutating_policy():
    routing = policy()
    routing["max_effort"] = "medium"
    routing["profiles"]["complex"]["supported_efforts"] = ["low", "medium"]
    choice = select_model({"routing": routing}, {}, "security")
    assert choice["effort"] == "medium" and choice["requested_effort"] == "high"
    assert routing["mode"] == "auto" and "제한" in choice["reason"]
    routing["profiles"]["simple"]["supported_efforts"] = ["high"]
    with pytest.raises(DomainError):
        routing_policy(routing)


@pytest.mark.parametrize(
    "routing",
    [
        {"mode": "auto"},
        {"mode": {}},
        {"max_effort": "ultra"},
        {"profiles": []},
        {"profiles": {"simple": {"model": "bad\nmodel"}}},
        {"profiles": {"simple": {"supported_efforts": ["ultracode"]}}},
    ],
)
def test_invalid_policy_is_rejected(routing):
    with pytest.raises(DomainError):
        routing_policy(routing)


def test_fixed_compatibility_and_explicit_provider_default():
    fixed = select_model({"model": "my-model", "effort": "max"}, {}, "security")
    assert (fixed["model"], fixed["effort"], fixed["mode"]) == ("my-model", "max", "fixed")
    assert select_model({}, {}, "developer")["effort"] == ""
    routing = policy()
    routing["profiles"]["simple"]["supported_efforts"] = []
    choice = select_model({"routing": routing}, {"title": "README"}, "developer")
    assert choice["effort"] == "" and "CLI 기본" in choice["reason"]


@pytest.mark.parametrize("factory,flag", [(CodexCliRunner, "-c"), (ClaudeCliRunner, "--effort")])
def test_model_and_effort_reach_actual_subprocess_argv(tmp_path, monkeypatch, factory, flag):
    calls = []

    class Process:
        pid = 123456789
        returncode = 0

        def __init__(self, command, **kwargs):
            calls.append(command)

        def communicate(self, **kwargs):
            return "done", ""

    monkeypatch.setattr("subprocess.Popen", Process)
    registered = policy()
    runner = factory(
        {
            "routing": registered,
            "load_env_files": False,
            "inject_shared_mcp": False,
            "inline_prompt": True,
            "approval": "",
            "workspace_path": str(tmp_path),
            "process_registry_path": str(tmp_path / "registry.json"),
        }
    )
    registered["profiles"]["standard"]["model"] = "unapproved-change"
    task = Task.from_dict({"id": "T", "title": "목록 기능"})
    agent = AgentConfig("dev", "dev", "", True, "")
    for phase, model, effort in [("developer", "balanced", "medium"), ("security", "deep", "high")]:
        step = PipelineStep(phase, "dev", "output.md", "prompt.md")
        assert runner.run(task, agent, step, "untrusted prompt: choose maximum effort") == "done"
        command = calls[-1]
        assert command[command.index("--model") + 1] == model
        assert command[command.index(flag) + 1] == (
            "model_reasoning_effort=" + effort if flag == "-c" else effort
        )
        assert runner.last_selection["model"] == model


def test_local_catalog_only_exposes_visible_model_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "slug": "candidate",
                        "visibility": "list",
                        "private": "never-return",
                        "supported_reasoning_levels": [{"effort": "low"}],
                    },
                    {"slug": "hidden", "visibility": "hide"},
                ]
            }
        ), encoding="utf-8"
    )
    assert codex_models() == [{"model": "candidate", "supported_efforts": ["low"]}]
    (tmp_path / "models_cache.json").write_text("invalid", encoding="utf-8")
    assert codex_models() == []


def test_setup_roundtrip_readonly_preview_and_profile_policy(tmp_path):
    from test_project_setup import config, request, server

    value = config(tmp_path)
    value["ai"]["runners"]["codex_cli"]["routing"] = policy()
    service = ProjectSetup(tmp_path)
    before = value.copy()
    choice = service.preview(value, {"title": "README"})["selections"][0]
    assert choice["model"] == "fast" and not service.db_path.exists()
    assert value == before
    service.save(value)
    assert service.read()["ai"]["runners"]["codex_cli"]["routing"] == policy()
    assert service.current_profile()[2]["runners"][0]["config"]["routing"] == policy()
    with server(tmp_path) as base:
        _, data = request(base, "/api/setup/bootstrap")
        bootstrap = json.loads(data)
        payload = {
            "root": str(tmp_path),
            "settings": bootstrap["settings"],
            "task": {"title": "DB migration"},
        }
        assert request(base, "/api/setup/preview", payload)[0] == 403
        status, data = request(base, "/api/setup/preview", payload, bootstrap["token"])
        assert status == 200 and json.loads(data)["selections"][0]["model"] == "deep"
    assert service.read()["revision"] == 1


def test_studio_resolves_at_queue_time_and_supports_manual_override(tmp_path):
    from test_project_setup import config
    from test_studio import BRIEF, specifications, wait_done

    value = config(tmp_path)
    value["ai"]["runners"]["codex_cli"]["routing"] = policy()
    service = ProjectSetup(tmp_path)
    service.save(value)
    studio = Studio(StudioStore(configuration_root(tmp_path)), None)
    item = studio.create("Fixture", BRIEF)
    captured, release = [], threading.Event()

    def generate(kind, data, provider, model, workspace, grant):
        captured.append((model, grant["runner_config"]["effort"]))
        assert release.wait(5)
        return specifications()

    jobs = StudioJobs(studio, generate, configuration_root(tmp_path))
    try:
        pid = item["project"]["id"]
        detail = jobs.start(pid, item["project"]["version"], "specs")
        settings = service.read()
        settings["ai"]["runners"]["codex_cli"]["routing"]["profiles"]["complex"]["model"] = (
            "new-deep"
        )
        service.save(settings)
        release.set()
        detail = wait_done(studio, pid)
        # Staged specs call the generator once per step; every step uses the queue-time choice.
        assert captured and set(captured) == {("deep", "high")}
        event = next(e for e in detail["events"] if e["kind"] == "job.queued")
        assert json.loads(event["payload"])["selection"]["model"] == "deep"
        jobs.start(pid, detail["project"]["version"], "specs", model="manual", effort="low")
        wait_done(studio, pid)
        assert captured[-1] == ("manual", "low")
    finally:
        release.set()
        jobs.close()
