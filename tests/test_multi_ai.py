"""Provider HTTP contracts, credentials, capability boundaries and CLI policy regression."""

import copy
import json
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from types import SimpleNamespace

import pytest

from agent_dev.adapters.ai_providers import PROVIDERS, defaults, endpoint, normalize
from agent_dev.adapters.api_runner import ApiRunnerAdapter
from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.domain.errors import DomainError, ErrorCode
from agent_dev.runners.gemini_cli import GeminiCliRunner
from tests.test_project_setup import config


def response(provider, text="안녕하세요"):
    if provider == "openai_api":
        return {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
            "usage": {"input_tokens": 3, "output_tokens": 5},
        }
    if provider == "anthropic_api":
        return {
            "stop_reason": "end_turn",
            "content": [{"type": "text", "text": text}],
            "usage": {"input_tokens": 3, "output_tokens": 5},
        }
    if provider == "gemini_api":
        return {
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}],
            "usageMetadata": {"promptTokenCount": 3, "candidatesTokenCount": 5},
        }
    return {
        "choices": [{"finish_reason": "stop", "message": {"content": text}}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 5},
    }


@pytest.fixture
def api_server():
    state = SimpleNamespace(
        calls=[],
        status=200,
        payload=response("compatible_api"),
        raw=None,
        started=threading.Event(),
        release=threading.Event(),
        hold=False,
    )

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            state.calls.append(
                (
                    self.path,
                    {k.lower(): v for k, v in self.headers.items()},
                    json.loads(self.rfile.read(int(self.headers["Content-Length"]))),
                )
            )
            state.started.set()
            if state.hold:
                assert state.release.wait(10)
            self.send_response(state.status)
            if state.status == 302:
                self.send_header("Location", "/credential-leak")
            self.end_headers()
            self.wfile.write(
                state.raw if state.raw is not None else json.dumps(state.payload).encode()
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}/v1"
    try:
        yield state
    finally:
        state.release.set()
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "provider",
    ["openai_api", "anthropic_api", "gemini_api", "xai_api", "exaone_api", "compatible_api"],
)
def test_api_actual_http_contract_and_effort(api_server, provider):
    api_server.payload = response(provider)
    effort = "high" if provider == "exaone_api" else "medium"  # K-EXAONE has no medium.
    cfg = dict(
        defaults(provider),
        enabled=True,
        model="fixture-model",
        base_url=api_server.url,
        credential_ref="FIXTURE_AUTH",
        effort=effort,
        reasoning="native",
        process_env={"FIXTURE_AUTH": "fixture-private"},
    )
    runner = ApiRunnerAdapter(provider, cfg)
    result = runner.generate("한글 기획서", {"title": "기능 개발"}, "developer")
    assert result.output == "안녕하세요" and result.usage["total_tokens"] == 8
    assert result.selection["effort"] == effort
    path, headers, body = api_server.calls[0]
    assert "tools" not in body and "fixture-private" not in json.dumps(body)
    if provider == "anthropic_api":
        assert path == "/v1/messages" and headers["x-api-key"] == "fixture-private"
        assert body["output_config"]["effort"] == "medium"
    elif provider == "gemini_api":
        assert (
            path.endswith("/models/fixture-model:generateContent")
            and headers["x-goog-api-key"] == "fixture-private"
        )
        assert body["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "MEDIUM"
    else:
        assert headers["authorization"] == "Bearer fixture-private"
        assert path == ("/v1/responses" if provider == "openai_api" else "/v1/chat/completions")
        assert (
            body["reasoning"]["effort"] if provider == "openai_api" else body["reasoning_effort"]
        ) == effort


def api_config(provider, url):
    return dict(
        defaults(provider),
        enabled=True,
        model="fixture-model",
        base_url=url,
        credential_ref="FIXTURE_AUTH",
        process_env={"FIXTURE_AUTH": "fixture-private"},
    )


def test_anthropic_marks_the_repeated_prompt_part_for_its_cache(api_server):
    from agent_dev.domain.prompt_cache import CALL_SECTION

    api_server.payload = response("anthropic_api")
    runner = ApiRunnerAdapter("anthropic_api", api_config("anthropic_api", api_server.url))
    prompt = "규칙과 명세" + CALL_SECTION + "이번 관점"
    runner.generate(prompt, {"title": "검토"}, "studio-review")
    runner.generate("나뉘지 않는 요청", {"title": "검토"}, "studio-review")
    marked, plain = (call[2]["messages"][0]["content"] for call in api_server.calls)
    assert marked == [
        {"type": "text", "text": "규칙과 명세" + CALL_SECTION, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": "이번 관점"},
    ]
    assert "".join(block["text"] for block in marked) == prompt  # Same text, only split.
    assert plain == "나뉘지 않는 요청"


@pytest.mark.parametrize("provider", ["openai_api", "gemini_api", "xai_api", "exaone_api", "compatible_api"])
def test_other_providers_receive_the_prompt_unmarked(api_server, provider):
    from agent_dev.domain.prompt_cache import CALL_SECTION

    api_server.payload = response(provider)
    prompt = "규칙과 명세" + CALL_SECTION + "이번 관점"
    ApiRunnerAdapter(provider, api_config(provider, api_server.url)).generate(prompt, {"title": "검토"}, "studio-review")
    body = api_server.calls[0][2]
    sent = (
        body["input"]
        if provider == "openai_api"
        else body["contents"][0]["parts"][0]["text"]
        if provider == "gemini_api"
        else body["messages"][0]["content"]
    )
    assert sent == prompt and "cache_control" not in json.dumps(body)


@pytest.mark.parametrize(
    ("provider", "usage_key", "usage", "expected"),
    [
        (
            "openai_api",
            "usage",
            {"input_tokens": 1000, "output_tokens": 50, "input_tokens_details": {"cached_tokens": 800}},
            {"input_tokens": 1000, "output_tokens": 50, "cached_input_tokens": 800, "total_tokens": 1050},
        ),
        (
            "anthropic_api",
            "usage",
            {"input_tokens": 100, "cache_creation_input_tokens": 60, "cache_read_input_tokens": 800, "output_tokens": 50},
            # Reads are counted beside input; writes are input the model processed.
            {"input_tokens": 160, "output_tokens": 50, "cached_input_tokens": 800, "total_tokens": 1010},
        ),
        (
            "gemini_api",
            "usageMetadata",
            {"promptTokenCount": 1000, "candidatesTokenCount": 50, "cachedContentTokenCount": 800},
            {"input_tokens": 1000, "output_tokens": 50, "cached_input_tokens": 800, "total_tokens": 1050},
        ),
        (
            "xai_api",
            "usage",
            {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 800}},
            {"input_tokens": 1000, "output_tokens": 50, "cached_input_tokens": 800, "total_tokens": 1050},
        ),
        (
            "compatible_api",
            "usage",
            {"prompt_tokens": 1000, "completion_tokens": 50, "prompt_tokens_details": {"cached_tokens": 0}},
            {"input_tokens": 1000, "output_tokens": 50, "cached_input_tokens": 0, "total_tokens": 1050},
        ),
        # Not reported, malformed or impossible counts stay unknown instead of becoming a number.
        ("exaone_api", "usage", {"prompt_tokens": 1000, "completion_tokens": 50}, {"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050}),
        ("openai_api", "usage", {"input_tokens": 1000, "output_tokens": 50, "input_tokens_details": "n/a"}, {"input_tokens": 1000, "output_tokens": 50, "total_tokens": 1050}),
        ("gemini_api", "usageMetadata", {"promptTokenCount": 10, "candidatesTokenCount": 5, "cachedContentTokenCount": 800}, {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
        ("anthropic_api", "usage", {"input_tokens": 100, "output_tokens": 50}, {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}),
    ],
)
def test_input_read_from_the_provider_cache_is_recorded(provider, usage_key, usage, expected):
    from agent_dev.domain.usage_policy import call_cost

    runner = ApiRunnerAdapter(provider, {"model": "fixture"})
    _text, result = runner.decode(dict(response(provider), **{usage_key: usage}))
    assert result == expected
    if expected.get("cached_input_tokens") == 800:
        # Cached input is charged once, at the cached rate: 200 (or 160) fresh + 800 cached + 50 out.
        policy = {"price_book": [{"provider": provider, "model": "fixture", "valid_from": "2000-01-01", "input_per_million": 10.0, "cached_input_per_million": 1.0, "output_per_million": 20.0}]}
        cost, reason = call_cost(policy, {"provider": provider, "model": "fixture", "usage": result})
        fresh = 160 if provider == "anthropic_api" else 200
        assert reason == "estimated" and cost == pytest.approx((fresh * 10 + 800 * 1 + 50 * 20) / 1_000_000)


def test_exaone_defaults_and_reasoning_stay_out_of_output(api_server):
    """K-EXAONE always reasons: reasoning_content and the leading blank lines are not output."""
    row = defaults("exaone_api")
    assert row["base_url"] == "https://api.exaone.ai/v1"
    assert row["credential_ref"] == "EXAONE_API_KEY" and row["model"] == "k-exaone_v2.5"
    assert row["max_output_tokens"] == 32768 and row["reasoning"] == "native"
    api_server.payload = {
        "object": "chat.completion",
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "\n\n안녕하세요",
                    "reasoning_content": "사용자가 인사를 했다.",
                    "tool_calls": None,
                },
            }
        ],
        "usage": {
            "prompt_tokens": 22,
            "completion_tokens": 218,
            "total_tokens": 240,
            "reasoning_tokens": 209,
        },
    }
    cfg = dict(
        row,
        base_url=api_server.url,
        process_env={"EXAONE_API_KEY": "fixture-private"},
    )
    result = ApiRunnerAdapter("exaone_api", cfg).generate("인사", {"title": "기능"}, "developer")
    assert result.output == "안녕하세요" and "인사를 했다" not in result.output
    assert result.usage == {"input_tokens": 22, "output_tokens": 218, "total_tokens": 240}
    path, headers, body = api_server.calls[0]
    assert path == "/v1/chat/completions"
    assert headers["authorization"] == "Bearer fixture-private"
    assert body["model"] == "k-exaone_v2.5" and body["max_tokens"] == 32768
    assert "reasoning_effort" not in body  # Empty effort keeps the service default.


@pytest.mark.parametrize(
    "status,code",
    [
        (401, ErrorCode.AUTH_REQUIRED),
        (403, ErrorCode.AUTH_REQUIRED),
        (429, ErrorCode.CAPACITY_EXHAUSTED),
        (503, ErrorCode.EXTERNAL_TRANSIENT),
        (400, ErrorCode.CAPABILITY_UNAVAILABLE),
        (302, ErrorCode.CAPABILITY_UNAVAILABLE),
    ],
)
def test_api_errors_are_safe_and_redirect_never_replays_credentials(api_server, status, code):
    api_server.status, api_server.raw = status, b"server-private-secret"
    runner = ApiRunnerAdapter("compatible_api", {"model": "fixture", "base_url": api_server.url})
    with pytest.raises(DomainError) as caught:
        runner.generate("prompt-private", {}, "planner")
    assert caught.value.code == code and "private" not in str(caught.value)
    assert len(api_server.calls) == 1


@pytest.mark.parametrize(
    "provider,payload",
    [
        ("openai_api", {"status": "incomplete"}),
        ("anthropic_api", {"stop_reason": "max_tokens"}),
        ("gemini_api", {"candidates": [{"finishReason": "MAX_TOKENS"}]}),
        ("compatible_api", {"choices": [{"finish_reason": "length"}]}),
        (
            "compatible_api",
            {
                "choices": [
                    {"finish_reason": "stop", "message": {"content": "", "tool_calls": [{}]}}
                ]
            },
        ),
    ],
)
def test_incomplete_or_tool_only_output_is_not_success(provider, payload):
    runner = ApiRunnerAdapter(provider, {"model": "fixture"})
    with pytest.raises(DomainError):
        runner.decode(payload)


def test_api_missing_auth_and_capability_never_call(api_server):
    runner = ApiRunnerAdapter(
        "anthropic_api", {"model": "fixture", "base_url": api_server.url, "process_env": {}}
    )
    with pytest.raises(DomainError) as caught:
        runner.generate("prompt", {}, "planner")
    assert caught.value.code == ErrorCode.AUTH_REQUIRED and not api_server.calls
    for capabilities in [{"code.read"}, {"code.write"}]:
        with pytest.raises(DomainError) as caught:
            runner.execute(SimpleNamespace(capabilities=capabilities))
        assert caught.value.code == ErrorCode.CAPABILITY_UNAVAILABLE


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/v1",
        "https://user:secret@example.com/v1",
        "https://example.com/v1?key=secret",
        "https://example.com/#secret",
        "http://169.254.169.254",
        "https://10.0.0.2",
        "https://example.com:99999",
        "https://example.com/a/../b",
        "https://example.com/%2e%2e",
        "https://example.com\\evil",
    ],
)
def test_unsafe_api_address_is_rejected(url):
    with pytest.raises(DomainError):
        endpoint(url)


def test_disabled_defaults_and_old_two_provider_import(tmp_path):
    service = ProjectSetup(tmp_path)
    settings = config(tmp_path)
    settings["ai"]["runners"] = {
        p: r for p, r in settings["ai"]["runners"].items() if p in {"codex_cli", "claude_cli"}
    }
    saved = service.save(settings)["settings"]
    assert set(saved["ai"]["runners"]) == set(PROVIDERS)
    assert all(
        not saved["ai"]["runners"][p]["enabled"]
        for p in PROVIDERS
        if p not in {"codex_cli", "claude_cli"}
    )


def test_api_setup_masks_keys_keeps_capability_and_backup(tmp_path):
    from agent_dev.adapters.backup import create_bundle

    service = ProjectSetup(tmp_path)
    settings = config(tmp_path)
    settings["ai"]["provider"] = "anthropic_api"
    settings["ai"]["runners"]["anthropic_api"].update(enabled=True, model="fixture-model")
    settings["environment"] = [{"key": "ANTHROPIC_API_KEY", "value": "fixture-private-key-value"}]
    saved = service.save(settings)["settings"]
    assert saved["environment"][0]["value"] != "fixture-private-key-value"
    profile = service.current_profile()[2]
    api = profile["runners"][0]
    assert (
        api["capabilities"] == ["text.generate"]
        and api["config"]["credential_ref"] == "ANTHROPIC_API_KEY"
    )
    assert "fixture-private-key-value" not in json.dumps(profile)
    create_bundle(tmp_path, tmp_path.parent / (tmp_path.name + ".zip"))


def test_gemini_cli_actual_policy_and_argv(tmp_path, monkeypatch):
    from agent_dev.core.models import AgentConfig, PipelineStep, Task

    captured = []
    monkeypatch.setattr(
        "agent_dev.runners.gemini_cli.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="--admin-policy"),
    )
    monkeypatch.setattr(GeminiCliRunner, "preflight", lambda self: None)

    class Process:
        pid, returncode = 991099, 0

        def __init__(self, argv, **kwargs):
            captured.append(
                (
                    argv,
                    kwargs,
                    json.loads(
                        __import__("pathlib")
                        .Path(kwargs["env"]["GEMINI_CLI_SYSTEM_SETTINGS_PATH"])
                        .read_text(encoding="utf-8")
                    ),
                    __import__("pathlib").Path(argv[argv.index("--admin-policy") + 1]).read_text(encoding="utf-8"),
                )
            )

        def communicate(self, **kwargs):
            return json.dumps({"response": "generated"}), ""

    monkeypatch.setattr("agent_dev.runners.gemini_cli.subprocess.Popen", Process)
    runner = GeminiCliRunner(
        {
            "model": "fixture-model",
            "workspace_path": str(tmp_path),
            "process_registry_path": str(tmp_path / "processes.json"),
        }
    )
    task, step = (
        Task.from_dict({"id": "T-fixture", "title": "개발"}),
        PipelineStep("dev", "dev", "output", "prompt"),
    )
    assert runner.run(task, AgentConfig("dev", "dev", "", True, ""), step, "prompt") == "generated"
    argv, _, settings, policy = captured[-1]
    assert argv[argv.index("--model") + 1] == "fixture-model"
    assert (
        settings["hooksConfig"]["enabled"] is False and settings["admin"]["mcp"]["enabled"] is False
    )
    assert (
        'decision = "deny"' in policy
        and "write_file" in policy
        and "run_shell_command" not in policy
    )
    runner.no_tools = True
    runner.run(task, AgentConfig("dev", "dev", "", False, ""), step, "prompt")
    assert captured[-1][2]["tools"]["core"] == [] and 'decision = "allow"' not in captured[-1][3]


def test_unsupported_reasoning_is_not_silently_ignored():
    for provider, row in [
        ("gemini_cli", {"effort": "high"}),
        ("gemini_api", {"effort": "xhigh"}),
        ("xai_api", {"effort": "max"}),
        ("exaone_api", {"effort": "medium"}),
        ("exaone_api", {"effort": "xhigh"}),
        ("compatible_api", {"effort": "high"}),
    ]:
        with pytest.raises(DomainError):
            normalize(provider, row)
    before = defaults("anthropic_api")
    copy_before = copy.deepcopy(before)
    normalize("anthropic_api", before)
    assert before == copy_before


def test_real_api_studio_specs_and_mock_follow_saved_defaults(tmp_path, api_server):
    from agent_dev.adapters.studio_generation import StudioJobs
    from agent_dev.adapters.studio_store import StudioStore
    from agent_dev.application.studio import Studio
    from tests.test_studio import BRIEF, mockup, specifications, wait_done

    service = ProjectSetup(tmp_path)
    settings = config(tmp_path)
    settings["ai"]["provider"] = "compatible_api"
    settings["ai"]["runners"]["compatible_api"].update(
        enabled=True, model="local-fixture", base_url=api_server.url
    )
    service.save(settings)
    studio = Studio(StudioStore(tmp_path), files("agent_dev.ui").joinpath("assets"))
    jobs = StudioJobs(studio, config_root=tmp_path)
    item = studio.create("API 목업", BRIEF)
    pid = item["project"]["id"]
    api_server.payload = response("compatible_api", json.dumps(specifications()))
    jobs.start(pid, item["project"]["version"], "specs")
    item = wait_done(studio, pid)
    assert item["spec"] and item["jobs"][0]["state"] == "done"
    spec_calls = len(api_server.calls)  # requirements, then document groups (staged)
    assert spec_calls >= 2
    item = studio.approve_specs(pid, item["project"]["version"])
    api_server.payload = response("compatible_api", json.dumps(mockup()))
    jobs.start(pid, item["project"]["version"], "mock")
    item = wait_done(studio, pid)
    assert item["mock"] and len(api_server.calls) == spec_calls + 1
    queued = next(e for e in item["events"] if e["kind"] == "job.queued")
    assert json.loads(queued["payload"])["selection"]["provider"] == "compatible_api"
    # Every generation call leaves one usage row for the dashboard, outside the screen history.
    import sqlite3

    with sqlite3.connect(studio.store.path) as db:
        rows = [json.loads(r[0]) for r in db.execute("SELECT payload FROM events WHERE kind='job.usage'")]
    assert len(rows) == len(api_server.calls)
    assert {"spec_requirements", "mock"} <= {r["step"] for r in rows}
    assert all(
        r["provider"] == "compatible_api"
        and r["model"] == "local-fixture"
        and r["usage"] == {"input_tokens": 3, "output_tokens": 5}
        for r in rows
    )
    assert all(e["kind"] != "job.usage" for e in item["events"])
    jobs.close()


def test_doctor_reports_missing_private_api_values(tmp_path):
    from agent_dev.adapters.diagnostics import diagnose
    from agent_dev.adapters.environment import environment_file

    settings = config(tmp_path)
    settings["ai"]["provider"] = "anthropic_api"
    settings["ai"]["runners"]["anthropic_api"].update(enabled=True, model="fixture")
    settings["environment"] = [{"key": "ANTHROPIC_API_KEY", "value": "fixture-private"}]
    ProjectSetup(tmp_path).save(settings)
    environment_file(tmp_path).unlink()
    report = diagnose(tmp_path)
    assert not report["healthy"]
    assert any(
        row["check"] == "environment" and row["status"] == "fail" for row in report["checks"]
    )
    assert "fixture-private" not in json.dumps(report)


def test_api_auto_routing_uses_registered_complex_model(api_server):
    cfg = defaults("compatible_api")
    cfg.update(
        base_url=api_server.url,
        reasoning="native",
        routing={
            "mode": "auto",
            "max_effort": "high",
            "profiles": {
                tier: {"model": "fixture-" + tier, "supported_efforts": ["low", "medium", "high"]}
                for tier in ("simple", "standard", "complex")
            },
        },
    )
    result = ApiRunnerAdapter("compatible_api", cfg).generate(
        "schema", {"title": "보안 권한 설계"}, "security"
    )
    assert result.selection["model"] == "fixture-complex"
    assert result.selection["effort"] == "high"
    assert api_server.calls[0][2]["model"] == "fixture-complex"
    assert api_server.calls[0][2]["reasoning_effort"] == "high"


def test_workflow_api_registry_preserves_auth_and_file_boundaries(
    tmp_path, api_server, monkeypatch
):
    from agent_dev.adapters.workflow_runtime import WorkflowRuntime

    settings = config(tmp_path)
    settings["ai"]["provider"] = "compatible_api"
    for row in settings["ai"]["runners"].values():
        row["enabled"] = False
    settings["ai"]["runners"]["compatible_api"].update(
        enabled=True, model="fixture", base_url=api_server.url
    )
    setup = ProjectSetup(tmp_path)
    setup.save(settings)
    pid, _, profile = setup.current_profile()
    profile["repositories"] = {"source": {"worktree": str(tmp_path)}}
    runtime = WorkflowRuntime(tmp_path)
    task = SimpleNamespace(project_id=pid, definition={"title": "문서 생성"})
    monkeypatch.setattr(runtime.store, "task", lambda _: task)
    monkeypatch.setattr(runtime.store, "snapshot", lambda _: {"resolved": profile})
    request = SimpleNamespace(
        task=task,
        capabilities=frozenset({"text.generate"}),
        context="approved text",
        phase_id="planner",
        timeout_seconds=3,
    )
    assert runtime.runners("T").execute(request).output == "안녕하세요"
    row = next(r for r in profile["runners"] if r["provider"] == "compatible_api")
    row["authenticated"] = False
    with pytest.raises(DomainError) as caught:
        runtime.runners("T").execute(request)
    assert caught.value.code == ErrorCode.AUTH_REQUIRED
    row["authenticated"] = True
    request.capabilities = frozenset({"code.write"})
    with pytest.raises(DomainError) as caught:
        runtime.runners("T").execute(request)
    assert caught.value.code == ErrorCode.CAPABILITY_UNAVAILABLE
    assert len(api_server.calls) == 1


def test_claude_api_rejects_non_native_xhigh():
    with pytest.raises(DomainError):
        normalize("anthropic_api", {"effort": "xhigh"})


def test_gemini_private_key_is_recognized_by_setup_check(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    settings = config(tmp_path)
    for row in settings["ai"]["runners"].values():
        row["enabled"] = False
    settings["ai"]["provider"] = "gemini_cli"
    settings["ai"]["runners"]["gemini_cli"]["enabled"] = True
    settings["environment"] = [{"key": "GEMINI_API_KEY", "value": "fixture-private"}]
    setup = ProjectSetup(tmp_path)
    setup.save(settings)
    report = setup.check(check_auth=True)
    row = next(row for row in report["checks"] if row["name"] == "gemini_cli_login")
    assert row["status"] == "pass" and "실제 접근은 실행 시" in row["message"]
    assert "fixture-private" not in json.dumps(report)


def test_cancelled_api_response_cannot_save_spec(tmp_path, api_server):
    from agent_dev.adapters.studio_generation import StudioJobs
    from agent_dev.adapters.studio_store import StudioStore
    from agent_dev.application.studio import Studio
    from tests.test_studio import BRIEF, specifications

    settings = config(tmp_path)
    settings["ai"]["provider"] = "compatible_api"
    settings["ai"]["runners"]["compatible_api"].update(
        enabled=True, model="fixture", base_url=api_server.url
    )
    ProjectSetup(tmp_path).save(settings)
    studio = Studio(StudioStore(tmp_path), files("agent_dev.ui").joinpath("assets"))
    jobs = StudioJobs(studio, config_root=tmp_path)
    item = studio.create("취소", BRIEF)
    pid = item["project"]["id"]
    api_server.payload = response("compatible_api", json.dumps(specifications()))
    api_server.hold = True
    try:
        jobs.start(pid, item["project"]["version"], "specs")
        assert api_server.started.wait(3)
        item = studio.store.detail(pid)
        studio.cancel_job(pid, item["project"]["version"], item["jobs"][0]["id"])
        api_server.release.set()
        deadline = time.monotonic() + 3
        while jobs.owned and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not jobs.owned
        assert studio.store.detail(pid)["spec"] is None
    finally:
        api_server.release.set()
        jobs.close()


def test_api_invalid_json_and_response_limit(api_server):
    runner = ApiRunnerAdapter("compatible_api", {"model": "fixture", "base_url": api_server.url})
    for raw in [b"not-json", b"x" * 2_000_001]:
        api_server.raw = raw
        with pytest.raises(DomainError) as caught:
            runner.generate("prompt", {}, "planner")
        assert caught.value.code == ErrorCode.INVALID_INPUT


def test_gemini_policy_version_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent_dev.runners.gemini_cli.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="older CLI"),
    )
    with pytest.raises(DomainError) as caught:
        GeminiCliRunner({"workspace_path": str(tmp_path)}).preflight()
    assert caught.value.code == ErrorCode.CAPABILITY_UNAVAILABLE


def test_gemini_timeout_terminates_process_and_cleans_registry(tmp_path, monkeypatch):
    from agent_dev.core.models import AgentConfig, PipelineStep, Task
    from agent_dev.storage.process_registry import ProcessRegistry

    calls = []

    class Process:
        pid = 991098

        def __init__(self, *args, **kwargs):
            pass

        def communicate(self, **kwargs):
            calls.append("communicate")
            if len(calls) == 1:
                raise subprocess.TimeoutExpired("gemini", 1)
            return "", ""

        def terminate(self):
            calls.append("terminate")

    monkeypatch.setattr(GeminiCliRunner, "preflight", lambda self: None)
    monkeypatch.setattr("agent_dev.runners.gemini_cli.subprocess.Popen", Process)
    path = tmp_path / "processes.json"
    runner = GeminiCliRunner({"workspace_path": str(tmp_path), "process_registry_path": str(path)})
    with pytest.raises(subprocess.TimeoutExpired):
        runner.run(
            Task.from_dict({"id": "T", "title": "timeout fixture"}),
            AgentConfig("dev", "dev", "", True, ""),
            PipelineStep("dev", "dev", "output", "prompt"),
            "prompt",
        )
    assert calls == ["communicate", "terminate", "communicate"]
    assert ProcessRegistry(path).read()["processes"] == []
