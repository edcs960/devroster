import json
import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from agent_dev import app
from agent_dev.adapters import cli_login
from agent_dev.adapters.environment import process_environment
from agent_dev.core import cli_home
from agent_dev.core.models import AgentConfig, PipelineStep, Task
from agent_dev.domain.errors import DomainError


def homes():
    base = Path(os.environ["DEVROSTER_HOME"]) / "cli"
    return {
        "CLAUDE_CONFIG_DIR": str(base / "claude"),
        "CODEX_HOME": str(base / "codex"),
        "GEMINI_CLI_HOME": str(base / "gemini"),
    }


def test_isolated_environment_points_clis_at_app_homes(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/personal/.claude")
    env = cli_home.isolated_environment({"PATH": "/bin", "CODEX_HOME": "/personal/.codex"})
    assert {k: env[k] for k in homes()} == homes()
    assert env["PATH"] == "/bin"
    assert all(Path(path).is_dir() for path in homes().values())


def test_app_home_defaults_per_platform(monkeypatch):
    monkeypatch.delenv("DEVROSTER_HOME")
    monkeypatch.setattr(cli_home.sys, "platform", "darwin")
    assert cli_home.app_home().parts[-3:] == ("Library", "Application Support", "DevRoster")


def test_process_environment_is_isolated(tmp_path):
    env = process_environment(tmp_path, [])
    assert {k: env[k] for k in homes()} == homes()


def test_entry_point_isolates_the_whole_process(monkeypatch):
    from agent_dev import fa

    monkeypatch.setattr("agent_dev.entrypoints.main", lambda args: None)
    fa.main(["doctor", "--root", "."])
    assert {k: os.environ[k] for k in homes()} == homes()


def _capture(monkeypatch, calls):
    class Process:
        pid = 123456789
        returncode = 0

        def __init__(self, command, **kwargs):
            calls.append((command, kwargs.get("env") or {}))

        def communicate(self, **kwargs):
            return "done", ""

    monkeypatch.setattr("subprocess.Popen", Process)


def _run(runner, tmp_path):
    task = Task.from_dict({"id": "T", "title": "t"})
    step = PipelineStep("developer", "dev", "output.md", "prompt.md")
    return runner.run(task, AgentConfig("dev", "dev", "", True, ""), step, "prompt")


def test_codex_runner_ignores_project_agents_md_and_uses_app_home(tmp_path, monkeypatch):
    from agent_dev.runners.codex_cli import CodexCliRunner

    calls = []
    _capture(monkeypatch, calls)
    runner = CodexCliRunner(
        {
            "load_env_files": False,
            "inject_shared_mcp": False,
            "inline_prompt": True,
            "approval": "",
            "workspace_path": str(tmp_path),
            "process_registry_path": str(tmp_path / "registry.json"),
        }
    )
    _run(runner, tmp_path)
    command, env = calls[-1]
    assert command[command.index("project_doc_max_bytes=0") - 1] == "-c"
    assert env["CODEX_HOME"] == homes()["CODEX_HOME"]


def test_claude_runner_uses_only_devroster_mcp_and_app_home(tmp_path, monkeypatch):
    from agent_dev.runners import claude_cli

    monkeypatch.setattr(claude_cli, "PROJECT_ROOT", tmp_path)
    (tmp_path / ".mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    calls = []
    _capture(monkeypatch, calls)
    runner = claude_cli.ClaudeCliRunner(
        {
            "load_env_files": False,
            "inline_prompt": True,
            "workspace_path": str(tmp_path),
            "process_registry_path": str(tmp_path / "registry.json"),
        }
    )
    runner._help_text = "--setting-sources"
    _run(runner, tmp_path)
    command, env = calls[-1]
    settings = json.loads(Path(command[command.index("--settings") + 1]).read_text(encoding="utf-8"))
    assert f"{tmp_path.resolve().as_posix()}/**" in settings["claudeMdExcludes"]
    assert command[command.index("--append-system-prompt") + 1] == cli_home.RUNNER_NOTE
    assert "--strict-mcp-config" in command
    assert command[command.index("--mcp-config") + 1] == str(tmp_path / ".mcp.json")
    assert command[command.index("--setting-sources") + 1] == "user"
    assert env["CLAUDE_CONFIG_DIR"] == homes()["CLAUDE_CONFIG_DIR"]


def test_cli_login_streams_output_with_isolated_environment(tmp_path, monkeypatch):
    script = tmp_path / "fake-cli"
    script.write_text(
        f"#!{sys.executable}\nimport os\nprint('\\x1b[94mhttps://auth.example/device\\x1b[0m')\n"
        "print('CODE ' + os.environ['CODEX_HOME'])\n", encoding="utf-8"
    )
    script.chmod(0o755)
    job = cli_login.start("codex_cli", str(script))
    for _ in range(50):
        job = cli_login.status(job["job_id"])
        if job["done"]:
            break
        time.sleep(0.1)
    assert job["done"] and job["returncode"] == 0
    assert "https://auth.example/device" in job["output"] and "\x1b" not in job["output"]
    assert homes()["CODEX_HOME"] in job["output"]


def test_cli_login_rejects_unsupported_provider():
    with pytest.raises(DomainError):
        cli_login.start("gemini_cli", "gemini")
    with pytest.raises(DomainError):
        cli_login.status("missing")


def test_duplicate_login_is_atomic_and_owner_scoped(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_login, "_JOBS", {})
    script = tmp_path / "fake-cli"
    script.write_text(f"#!{sys.executable}\nimport time\nprint('ready',flush=True)\ntime.sleep(10)\n", encoding="utf-8")
    script.chmod(0o700)
    first = cli_login.start("codex_cli", str(script), owner="first-server")
    try:
        results = []
        thread = threading.Thread(
            target=lambda: results.append(cli_login.start("codex_cli", str(script), owner="first-server"))
        )
        thread.start()
        # Generous under full-suite load, still far below the fake CLI's 10s sleep:
        # a duplicate start must return the existing job, not wait for the process.
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert results[0]["job_id"] == first["job_id"]
        with pytest.raises(DomainError):
            cli_login.status(first["job_id"], owner="other-server")
        with pytest.raises(DomainError):
            cli_login.cancel(first["job_id"], owner="other-server")
        with pytest.raises(DomainError):
            cli_login.start("codex_cli", str(script), owner="other-server")
    finally:
        cli_login.shutdown("first-server")
    # shutdown() stops the process; the reader thread marks the job done right after it exits.
    deadline = time.monotonic() + 15
    while not cli_login.status(first["job_id"], owner="first-server")["done"]:
        assert time.monotonic() < deadline, "login job did not finish after shutdown"
        time.sleep(0.02)


def test_login_stream_is_bounded_and_keeps_visible_device_text(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_login, "_JOBS", {})
    script = tmp_path / "fake-cli"
    release = tmp_path / "release"
    script.write_text(
        f"#!{sys.executable}\nimport os,time\nfrom pathlib import Path\n"
        "os.write(1,b'\\x1b[94mhttps://auth.example/device\\x1b[0m\\n')\n"
        "os.write(1,'코드 ABCD'.encode())\n"
        f"while not Path({str(release)!r}).exists(): time.sleep(.01)\n"
        "os.write(1,b'x'*300000)\n", encoding="utf-8"
    )
    script.chmod(0o700)
    job = cli_login.start("codex_cli", str(script))
    # Loops exit as soon as the condition holds; the bound only absorbs slow process start.
    live_deadline = time.monotonic() + 15
    while "코드 ABCD" not in job["output"] and time.monotonic() < live_deadline:
        time.sleep(.02)
        job = cli_login.status(job["job_id"])
    try:
        assert "코드 ABCD" in job["output"] and not job["done"]
    finally:
        release.touch()
    deadline = time.monotonic() + 15
    while not job["done"] and time.monotonic() < deadline:
        time.sleep(.02)
        job = cli_login.status(job["job_id"])
    assert job["done"] and job["truncated"]
    assert len(job["output"].encode()) <= cli_login.MAX_OUTPUT_BYTES + 3
    assert "https://auth.example/device" in job["output"]
    assert "코드 ABCD" in job["output"]
    assert "\x1b" not in job["output"]


def test_login_timeout_and_shutdown_stop_owned_processes(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_login, "_JOBS", {})
    monkeypatch.setattr(cli_login, "TIMEOUT_SECONDS", .15)
    script = tmp_path / "fake-cli"
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(10)\n", encoding="utf-8")
    script.chmod(0o700)
    first = cli_login.start("codex_cli", str(script), owner="server")
    deadline = time.monotonic() + 3
    while not cli_login.status(first["job_id"], owner="server")["done"] and time.monotonic() < deadline:
        time.sleep(.02)
    assert cli_login.status(first["job_id"], owner="server")["done"]
    monkeypatch.setattr(cli_login, "TIMEOUT_SECONDS", 10)
    second = cli_login.start("codex_cli", str(script), owner="server")
    cli_login.shutdown("server")
    deadline = time.monotonic() + 3
    while not cli_login.status(second["job_id"], owner="server")["done"] and time.monotonic() < deadline:
        time.sleep(.02)
    assert cli_login.status(second["job_id"], owner="server")["done"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group descendant probe")
def test_login_cancel_stops_descendant_holding_stdout(tmp_path, monkeypatch):
    monkeypatch.setattr(cli_login, "_JOBS", {})
    pidfile = tmp_path / "descendant.pid"
    script = tmp_path / "fake-cli"
    script.write_text(
        f"#!{sys.executable}\nimport subprocess,sys,time\nfrom pathlib import Path\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(20)'])\n"
        f"Path({str(pidfile)!r}).write_text(str(child.pid))\n"
        "print('waiting',flush=True)\ntime.sleep(20)\n", encoding="utf-8"
    )
    script.chmod(0o700)
    job = cli_login.start("codex_cli", str(script), owner="server")
    child_pid = None

    def gone(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    def wait_for(condition, seconds=15):
        # Two interpreter start-ups under full-suite load can take several seconds;
        # the wait ends as soon as the condition holds.
        deadline = time.monotonic() + seconds
        while not condition() and time.monotonic() < deadline:
            time.sleep(.02)
        return condition()

    try:
        assert wait_for(lambda: pidfile.exists() and pidfile.read_text(encoding="utf-8").strip())
        child_pid = int(pidfile.read_text(encoding="utf-8"))
        cli_login.cancel(job["job_id"], owner="server")
        assert wait_for(lambda: cli_login.status(job["job_id"], owner="server")["done"])
        # The re-parented descendant is reaped asynchronously after the group kill.
        assert wait_for(lambda: gone(child_pid))
    finally:
        cli_login.shutdown("server")
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_login_home_and_saved_command_are_frozen(tmp_path, monkeypatch):
    from agent_dev.adapters import project_setup
    from agent_dev.ui.setup_server import cli_login_request

    monkeypatch.setattr(cli_login, "_JOBS", {})
    script = tmp_path / "fake-cli"
    script.write_text(f"#!{sys.executable}\nprint('complete')\n", encoding="utf-8")
    script.chmod(0o700)

    class Service:
        def __init__(self):
            self.saved = str(script)
            self.root = tmp_path

        def read(self):
            return {"ai": {"runners": {"codex_cli": {"command": self.saved}}}}

    service = Service()
    calls = []
    monkeypatch.setattr(
        project_setup, "auth_probe", lambda provider, command: calls.append(command) or True
    )
    job = cli_login_request(service, "cli-login", {"provider": "codex_cli"}, owner="server")
    deadline = time.monotonic() + 3
    while not job["done"] and time.monotonic() < deadline:
        time.sleep(.02)
        job = cli_login_request(service, "cli-login-status", {"job_id": job["job_id"]}, owner="server")
    service.saved = "newer-command"
    result = cli_login_request(service, "cli-login-status", {"job_id": job["job_id"]}, owner="server")
    assert result["logged_in"] and calls[-1] == str(script)
    cli_login_request(service, "cli-login-status", {"job_id": job["job_id"]}, owner="server")
    assert calls == [str(script)]
    service.root = tmp_path / "other-project"
    with pytest.raises(DomainError):
        cli_login_request(service, "cli-login-status", {"job_id": job["job_id"]}, owner="server")
    service.root = tmp_path
    monkeypatch.setenv("DEVROSTER_HOME", str(tmp_path / "other-home"))
    with pytest.raises(DomainError):
        cli_login.status(job["job_id"], owner="server")


def test_completed_login_job_retention_is_bounded(monkeypatch):
    monkeypatch.setattr(cli_login, "_JOBS", {})
    now = time.time()
    with cli_login._LOCK:
        for index in range(cli_login.MAX_COMPLETED_JOBS + 3):
            name = str(index)
            cli_login._JOBS[name] = {
                "id": name, "provider": "codex_cli", "command": "fake",
                "output": [], "done": True, "returncode": 0,
                "finished_at": now - index,
            }
        cli_login._prune_locked()
    assert len(cli_login._JOBS) == cli_login.MAX_COMPLETED_JOBS


def test_claude_canonical_mcp_opt_out_keeps_legacy_shared_config(tmp_path, monkeypatch):
    from agent_dev.adapters.cli_runner import CliRunnerAdapter
    from agent_dev.runners import claude_cli

    (tmp_path / ".mcp.json").write_text('{"mcpServers":{"unapproved":{}}}', encoding="utf-8")
    monkeypatch.setattr(claude_cli, "PROJECT_ROOT", tmp_path)
    canonical = CliRunnerAdapter("claude_cli", {"process_env": {"PATH": "/bin"}}, tmp_path)
    canonical.runner._help_text = "--setting-sources"
    assert "--mcp-config" not in canonical.runner.isolation_args()
    legacy = claude_cli.ClaudeCliRunner({"load_env_files": False, "workspace_path": str(tmp_path)})
    legacy._help_text = "--setting-sources"
    assert legacy.isolation_args()[1] == str(tmp_path / ".mcp.json")


def test_codex_canonical_mcp_inspects_effective_private_home(tmp_path, monkeypatch):
    from agent_dev.adapters.cli_runner import CliRunnerAdapter
    from agent_dev.core.cli_home import isolated_environment

    ambient = tmp_path / "ambient"
    ambient.mkdir()
    (ambient / "config.toml").write_text('[mcp_servers.ambient_only]\ncommand="unused"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(ambient))
    private = Path(isolated_environment({"PATH": "/bin"})["CODEX_HOME"])
    (private / "config.toml").write_text('[mcp_servers.private_only]\ncommand="unused"\n', encoding="utf-8")
    canonical = CliRunnerAdapter("codex_cli", {"process_env": {"PATH": "/bin"}}, tmp_path)
    assert "mcp_servers.private_only.enabled=false" in canonical.runner.extra_args
    assert "mcp_servers.ambient_only.enabled=false" not in canonical.runner.extra_args


def test_launcher_remembers_root_and_opens_setup_for_new_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    assert app.resolve_root(None) == Path.home()
    app.remember_root(project)
    assert app.resolve_root(None) == project.resolve()
    assert app.start_path(project) == "/setup"


def test_launcher_serves_setup_and_falls_back_when_port_busy(tmp_path):
    import socket
    import threading
    import urllib.request

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        server = app.make_server(tmp_path, busy.getsockname()[1])
    try:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/setup"
        with urllib.request.urlopen(url, timeout=5) as page:
            assert "settings-form" in page.read().decode("utf-8")
    finally:
        server.shutdown()
        server.studio.close()
        server.server_close()


def test_gemini_home_and_system_settings_load_only_internal_note():
    env = cli_home.isolated_environment({})
    assert env["GEMINI_CLI_HOME"] == homes()["GEMINI_CLI_HOME"]
    settings = json.loads(Path(env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]).read_text(encoding="utf-8"))
    assert settings["context"]["fileName"] == [cli_home.GEMINI_CONTEXT_FILE]
    note = Path(env["GEMINI_CLI_HOME"]) / ".gemini" / cli_home.GEMINI_CONTEXT_FILE
    assert note.read_text(encoding="utf-8").strip() == cli_home.RUNNER_NOTE


def test_claude_excludes_cover_project_tree_and_ancestor_instruction_files(tmp_path):
    project = tmp_path / "a" / "repo"
    patterns = cli_home.claude_memory_excludes(project)
    root = project.resolve().as_posix()
    assert f"{root}/**" in patterns
    parent = project.resolve().parent.as_posix()
    for name in ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", ".claude/rules/**"):
        assert f"{parent}/{name}" in patterns
    assert cli_home.claude_settings_file(project) == cli_home.claude_settings_file(project)


def test_gemini_runner_uses_isolated_home():
    from agent_dev.runners.gemini_cli import GeminiCliRunner

    runner = GeminiCliRunner({"process_env": {"PATH": "/bin"}})
    assert runner.process_env["GEMINI_CLI_HOME"] == homes()["GEMINI_CLI_HOME"]


def test_app_reports_restart_when_its_code_changes(tmp_path, monkeypatch):
    """A running app kept the code of its start after a pull removed modules; every
    screen now asks for a restart once the installed files change."""
    import threading
    import urllib.request

    from agent_dev.ui import code_stamp as stamps

    package = tmp_path / "package"
    package.mkdir()
    (package / "module.py").write_text("A = 1\n", encoding="utf-8")
    first = stamps.code_stamp(package)
    assert stamps.code_stamp(package) == first
    (package / "module.py").write_text("A = 22\n", encoding="utf-8")
    assert stamps.code_stamp(package) != first

    current = {"stamp": "start"}
    monkeypatch.setattr(stamps, "code_stamp", lambda root=None: current["stamp"])
    server = app.make_server(tmp_path, 0)
    try:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/api/app/status"

        def status():
            with urllib.request.urlopen(url, timeout=5) as response:
                return json.loads(response.read())

        assert status() == {"restart_required": False}
        current["stamp"] = "pulled"
        assert status() == {"restart_required": True}
    finally:
        server.shutdown()
        server.studio.close()
        server.server_close()


def test_every_parser_namespace_reaches_the_parser(capsys):
    """After the legacy CLI was removed, `devroster settings ...` and `devroster feedback ...`
    printed "Unknown command" because only the legacy CLI used to take them."""
    from agent_dev import entrypoints
    from agent_dev.adapters import command_line

    spaces = next(a for a in command_line.parser()._actions if a.dest == "namespace")
    assert set(spaces.choices) == command_line.NAMESPACES
    for namespace in ("settings", "feedback"):
        with pytest.raises(SystemExit) as exit_info:
            entrypoints.main([namespace, "--help"])
        assert exit_info.value.code == 0
        assert "usage: devroster " + namespace in capsys.readouterr().out


@pytest.mark.parametrize(
    ("provider", "output", "limited"),
    [
        (
            "claude_cli",
            '{"type":"result","is_error":true,"result":"You\'ve hit your session limit · resets 2:50am"}',
            True,
        ),
        ("codex_cli", "ERROR: You've hit your usage limit. Try again later.", True),
        ("claude_cli", "Error: invalid model name", False),
        ("codex_cli", "error: unexpected argument '--foo'", False),
    ],
)
def test_cli_limit_messages_become_capacity_errors(tmp_path, monkeypatch, provider, output, limited):
    """04 item 6: a subscription limit must reach the capacity path (wait or the next tool),
    while other CLI failures stay ordinary errors."""
    from agent_dev.runners import claude_cli, codex_cli

    class Process:
        pid = 123456789
        returncode = 1

        def __init__(self, command, **kwargs):
            pass

        def communicate(self, **kwargs):
            return output, ""

    monkeypatch.setattr("subprocess.Popen", Process)
    config = {
        "load_env_files": False,
        "inject_shared_mcp": False,
        "inline_prompt": True,
        "approval": "",
        "workspace_path": str(tmp_path),
        "process_registry_path": str(tmp_path / "registry.json"),
    }
    if provider == "claude_cli":
        runner, error = claude_cli.ClaudeCliRunner(config), claude_cli.ClaudeUsageLimitError
        runner._help_text = "--setting-sources"
    else:
        runner, error = codex_cli.CodexCliRunner(config), codex_cli.CodexUsageLimitError
    with pytest.raises(error if limited else RuntimeError) as raised:
        _run(runner, tmp_path)
    assert isinstance(raised.value, error) is limited


def test_bare_command_prints_a_readable_overview(capsys):
    """`devroster` alone dumped every namespace in one brace list with no descriptions and
    no mention of `devroster app`, the main way in."""
    from agent_dev import entrypoints
    from agent_dev.adapters import command_line

    entrypoints.main([])
    out = capsys.readouterr().out
    assert "devroster app" in out and "{" not in out.split("명령:")[1]
    for namespace, summary in command_line.SUMMARIES.items():
        assert f"{namespace}" in out and summary in out
    with pytest.raises(SystemExit):
        entrypoints.main(["task"])
    assert "devroster task --help" in capsys.readouterr().err
