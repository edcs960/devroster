"""Verify a wheel outside the source tree, without legacy dependencies or live providers."""

import json
import os
import subprocess
import sys
import tempfile
import venv
from pathlib import Path


def verify(wheel: Path):
    with tempfile.TemporaryDirectory(prefix="agent-dev-wheel-") as directory:
        root = Path(directory).resolve()  # macOS /var is a symlink; restore rejects symlinked parents
        environment = root / "venv"
        venv.EnvBuilder(with_pip=True).create(environment)
        binary = environment / ("Scripts" if os.name == "nt" else "bin")
        python = binary / ("python.exe" if os.name == "nt" else "python")
        cli = binary / ("devroster.exe" if os.name == "nt" else "devroster")
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env.pop("AGENT_DEV_CONFIG_ROOT", None)
        env.pop("AGENT_DEV_PROJECT_ROOT", None)
        env["PATH"] = str(binary) + os.pathsep + env.get("PATH", "")
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        # Keep the smoke's workspaces out of the user's app.json (92 stale roots).
        env["DEVROSTER_HOME"] = str(root / "devroster-home")

        def run(command, cwd=root):
            result = subprocess.run(
                [str(arg) for arg in command],
                cwd=cwd,
                env=env,
                check=False,
                capture_output=True,
                text=True,
                timeout=90,
            )
            if result.returncode:
                raise RuntimeError(
                    f"Wheel smoke failed: {command[:3]}\n{result.stdout}\n{result.stderr}"
                )
            return result.stdout

        run([python, "-m", "pip", "install", "--no-deps", "--disable-pip-version-check", wheel])
        assert "init" in run([cli, "--help"])
        assert "--port" in run([cli, "studio", "--help"])
        assert "--web" in run([cli, "setup", "--help"])
        assert (
            run(
                [
                    python,
                    "-I",
                    "-c",
                    "import importlib.util; assert importlib.util.find_spec('openai') is None; print('dependency-free')",
                ]
            ).strip()
            == "dependency-free"
        )
        run(
            [
                python,
                "-I",
                "-c",
                (
                    "from importlib.resources import files; "
                    "a=files('agent_dev.ui').joinpath('assets'); "
                    "assert 'task-list' in a.joinpath('dashboard.html').read_text(encoding='utf-8'); "
                    "assert '기획서 작성' in a.joinpath('studio.html').read_text(encoding='utf-8'); "
                    "assert 'textContent' in a.joinpath('mock_renderer.js').read_text(encoding='utf-8'); "
                    "assert '프로젝트명' in files('agent_dev.templates').joinpath('planning-brief.md').read_text(encoding='utf-8'); "
                    "from agent_dev.ui.presentation import phase_label; "
                    "assert phase_label('08-developer-frontend')=='화면 개발'"
                ),
            ]
        )
        run(
            [
                python,
                "-I",
                "-c",
                (
                    "from agent_dev import defaults; "
                    "assert defaults.workspace_or_default(defaults.DEFAULTS_DIR, 'config/templates/fmea', defaults.DEFAULTS_DIR).is_dir(); "
                    "assert 'deliverables/' not in defaults.operating_rules()"
                ),
            ]
        )
        presets = json.loads(run([cli, "preset", "list", "--json"]))["result"]["presets"]
        assert sorted(preset["id"] for preset in presets) == [
            "generic", "python", "react", "spring-boot", "spring-boot-gradle",
        ]
        gradle_file = root / "gradle-profile.json"
        run([cli, "preset", "write", "spring-boot-gradle", gradle_file, "--json"])
        gradle_profile = json.loads(gradle_file.read_text(encoding="utf-8"))
        assert "build.gradle" in gradle_profile["allowed_paths"]
        assert "build.gradle.kts" in gradle_profile["allowed_paths"]
        assert gradle_profile["verification"]["required_gates"] == ["build"]
        assert gradle_profile["verification"]["commands"][0]["command"] == [
            "gradlew.bat" if os.name == "nt" else "./gradlew", "build",
        ]
        preset_file = root / "react-profile.json"
        run([cli, "preset", "write", "react", preset_file, "--json"])
        assert json.loads(preset_file.read_text(encoding='utf-8'))["verification"]["required_gates"] == [
            "lint",
            "unit",
            "build",
        ]
        assert "doctor" in run([cli])
        projects = []
        for name in ("Alpha", "Beta"):
            project = root / name
            (project / "src").mkdir(parents=True)
            (project / "src/source.txt").write_text("baseline")
            (project / ".gitignore").write_text("/.agent-dev/\n/output/\n")
            run(["git", "init", "-q"], project)
            run(["git", "config", "user.name", "Wheel Fixture"], project)
            run(["git", "config", "user.email", "fixture@example.test"], project)
            run(["git", "add", "--", "src/source.txt", ".gitignore"], project)
            run(
                [
                    "git",
                    "-c",
                    "commit.gpgsign=false",
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    "commit",
                    "-qm",
                    "baseline",
                ],
                project,
            )
            result = json.loads(
                run(
                    [
                        cli,
                        "init",
                        "--root",
                        project,
                        "--name",
                        name,
                        "--allow-path",
                        "src",
                        "--json",
                    ]
                )
            )
            assert result["result"]["project_id"] == name
            listing = json.loads(run([cli, "task", "list", "--json"], project / "src"))
            assert listing["result"]["tasks"] == []
            projects.append(project)
        setup_program = """
import json,threading,urllib.request
from pathlib import Path
from agent_dev.ui.setup_server import make_server
from agent_dev.adapters.project_setup import ProjectSetup
root=Path.cwd();server=make_server(root,0)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
url='http://127.0.0.1:'+str(server.server_port)
try:
    with urllib.request.urlopen(url+'/setup') as response:
        assert '프로젝트 폴더' in response.read().decode('utf-8')
    with urllib.request.urlopen(url+'/api/setup/bootstrap') as response:
        boot=json.loads(response.read())
    assert len(boot['providers'])==8 and {'gemini_cli','xai_api'} <= set(boot['providers'])
    assert set(boot['settings']['ai']['runners'])==set(boot['providers'])
    value=boot['settings'];value['ai']['runners']['codex_cli']['model']='installed-fixture-model'
    request=urllib.request.Request(url+'/api/setup/save',data=json.dumps({'root':str(root),'settings':value}).encode(),headers={'Content-Type':'application/json','X-Setup-Token':boot['token']})
    with urllib.request.urlopen(request) as response:
        assert json.loads(response.read())['settings']['revision']==1
    assert ProjectSetup(root).read()['ai']['runners']['codex_cli']['model']=='installed-fixture-model'
finally:
    server.shutdown();server.server_close();thread.join()
print('Installed setup HTTP/profile persistence passed')
"""
        run([python, "-I", "-c", setup_program], projects[1])
        assert json.loads(run([cli, "setup", "--show", "--json"], projects[1]))["result"]["revision"] == 1
        studio_program = """
import json,threading,urllib.request
from pathlib import Path
from agent_dev.ui.studio_server import make_server
server=make_server(Path.cwd(),0)
thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
url='http://127.0.0.1:'+str(server.server_port)
try:
    with urllib.request.urlopen(url+'/studio') as response:
        assert '기획서 작성' in response.read().decode('utf-8')
    with urllib.request.urlopen(url+'/api/studio/bootstrap') as response:
        data=json.loads(response.read());assert len(data['documents'])==22
    assert len(data['providers'])==8 and {'compatible_api','xai_api'} <= set(data['providers'])
    from agent_dev.adapters.api_runner import ApiRunnerAdapter
    request=urllib.request.Request(url+'/api/studio/create',data=json.dumps({'title':'Installed Studio','brief':'A user can create and search a memo.'}).encode(),headers={'Content-Type':'application/json','X-Studio-Token':data['token']})
    with urllib.request.urlopen(request) as response:
        assert json.loads(response.read())['project']['title']=='Installed Studio'
finally:
    server.shutdown();server.studio.close();server.server_close();thread.join()
assert (Path.cwd()/'.agent-dev/studio/studio.db').is_file()
print('Installed Studio HTTP/template/storage passed')
"""
        run([python, "-I", "-c", studio_program], projects[1])
        # Fake phase runner exercises installed code/worktree/artifact/verification,
        # not authentication or model generation.
        program = """
import json
from pathlib import Path
from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.adapters.command_line import main as command
from agent_dev.domain.contracts import PhaseResult
root=Path.cwd()
runtime=WorkflowRuntime(root)
profile=json.loads((root/'.agent-dev/config/profile.json').read_text(encoding='utf-8'))
profile['runners'][0]['authenticated']=True
# One runner call per role keeps this fake runner simple; sessions are covered by unit tests.
from agent_dev.adapters.workflow_definitions import roster_graph
from agent_dev.domain.content import digest
roles={k:v for k,v in profile['roles'].items() if k!='session_mode'};profile['roles']=roles
graph=roster_graph(roles);profile['workflow']={'id':graph['id'],'version':1,'digest':digest(graph),'document':graph}
profile['verification']={'commands':[{'name':'unit','repository':'source','command':['python','-c', "from pathlib import Path; assert Path('src/source.txt').read_text(encoding='utf-8')=='implemented'"],'required':True}],'required_gates':['unit']}
profile_path=root/'.agent-dev/config/profile.json';profile_path.write_text(json.dumps(profile))
command(['project','profile','Alpha',str(profile_path),'--root',str(root),'--json'])
design=root/'.agent-dev/starter/design.md';design.write_text('## Scope\\nChange src\\n## Acceptance\\nunit passes\\n## Constraints\\nOnly src')
command(['design','refine','Alpha','D',str(design),'--root',str(root),'--json'])
command(['design','approve','D','1','--root',str(root),'--json'])
tasks=root/'.agent-dev/starter/tasks.json';tasks.write_text(json.dumps([{'id':'T','title':'Installed wheel test','system_name':'Alpha'}]))
command(['plan','create','Alpha','PLAN','D','1',str(tasks),'--root',str(root),'--json'])
command(['plan','approve','PLAN','--root',str(root),'--json'])
command(['refactor','configure','execute','--root',str(root),'--json'])
class Runner:
    def execute(self,request):
        if request.phase_id=='developer':
            binding=json.loads(request.context)['repositories']['source']
            (Path(binding['worktree'])/'src/source.txt').write_text('implemented')
        return PhaseResult('```json\\n{"handoff_status":"pass"}\\n```')
assert runtime.execute('T','run',runner=Runner())['state']=='verifying'
command(['task','verify','T','--root',str(root),'--json'])
assert runtime.store.task('T').state=='pr_ready'
assert (root/'src/source.txt').read_text(encoding='utf-8')=='baseline'
print('Installed phase/verification fixture passed')
"""
        run([python, "-I", "-c", program], projects[0])
        report = json.loads(run([cli, "task", "explain", "T", "--json"], projects[0]))["result"]
        assert report["gates"][0]["passed"]
        assert len(report["attempts"]) >= 3
        assert {"planner", "developer", "qa", "reviewer"} <= {a["phase_id"] for a in report["attempts"]}
        assert json.loads(run([cli, "task", "list", "--json"], projects[1]))["result"]["total"] == 0
        usage = json.loads(run([cli, "task", "usage", "T", "--json"], projects[0]))["result"]
        assert (
            usage["usage"]["attempts"] == len(report["attempts"]) and usage["usage"]["measurements"][0]["value"] is None
        )
        assert (
            json.loads(
                run(
                    [cli, "task", "list", "--search", "Installed", "--project", "Alpha", "--json"],
                    projects[0],
                )
            )["result"]["filtered_total"]
            == 1
        )
        marker_path = projects[0] / ".agent-dev/workspace.json"
        marker = json.loads(marker_path.read_text(encoding='utf-8'))
        marker.pop("engine_version", None)
        marker_path.write_text(json.dumps(marker))
        assert (
            json.loads(run([cli, "upgrade", "--json"], projects[0]))["result"]["from_version"]
            == "0.2.0"
        )
        assert json.loads(
            run(
                [cli, "upgrade", "--apply", "--backup", root / "upgrade.zip", "--json"], projects[0]
            )
        )["result"]["applied"]
        assert json.loads(run([cli, "task", "explain", "T", "--json"], projects[0]))["result"][
            "approvals"
        ]["design"]
        archive = root / "backup.zip"
        run([cli, "backup", "create", archive, "--json"], projects[0])
        assert json.loads(run([cli, "backup", "inspect", archive, "--json"], projects[0]))[
            "result"
        ]["valid"]
        # Restore only installs into a new folder; source Git is prepared separately.
        clone = root / "restored"
        restored = json.loads(
            run([cli, "backup", "restore", archive, clone, "--json"], projects[0])
        )["result"]
        assert restored["execution_disabled"]
        state = json.loads(run([cli, "task", "explain", "T", "--json"], clone))["result"]
        assert not state["approvals"]["design"]
        assert state["task"]["state"] == "blocked"
        # Missing provider binaries may produce a failed doctor report; it still
        # must be a bounded structured response, not a legacy import error.
        doctor = subprocess.run(
            [str(cli), "doctor", "--json"],
            cwd=projects[1],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert isinstance(json.loads(doctor.stdout)["result"]["checks"], list)
        return {
            "wheel": wheel.name,
            "independent_projects": 2,
            "installed_phase_verification": "passed (fake runner)",
            "backup_restore": "passed",
            "cwd_discovery": "passed",
            "profile_design_plan_configure_cli": "passed",
            "live_provider_calls": 0,
            "presets_upgrade_usage_filters": "passed",
            "base_dependency_free": True,
            "installed_web_resources": "passed",
            "installed_studio_http_template_storage": "passed",
            "installed_setup_http_profile_persistence": "passed",
        }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/smoke_wheel.py DIST-WHEEL.whl")
    print(json.dumps(verify(Path(sys.argv[1]).resolve()), indent=2))
