"""DevRoster entry points retain the existing wrapper and workspace contracts."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import tomllib
from inspect import unwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]












def test_installed_scripts_share_wrapper_and_keep_legacy_entrypoint():
    scripts = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts["devroster"] == scripts["fa"] == "agent_dev.fa:main"
    assert scripts["agent-dev"] == "agent_dev.entrypoints:main"










def test_help_outside_workspace_creates_no_files(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    result = subprocess.run(
        [sys.executable, "-m", "agent_dev.fa", "--help"],
        cwd=tmp_path,
        env=env,
        text=True, encoding="utf-8", errors="replace",
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage: devroster ")
    assert list(tmp_path.iterdir()) == []








def test_installed_browser_setup_and_dashboard_commands(tmp_path):
    """Real assets + real local APIs; clipboard is mocked, no command is executed."""
    from test_reusable_workspace import approved
    from test_reusable_workspace import workspace as fixture

    from agent_dev.adapters.project_setup import ProjectSetup
    from agent_dev.ui.dashboard import make_server

    module = os.environ.get("AGENT_DEV_TEST_PLAYWRIGHT_MODULE")
    executable = os.environ.get("AGENT_DEV_TEST_BROWSER_EXECUTABLE")
    if not shutil.which("node") or not module or not executable:
        pytest.skip("Opt in with an installed Playwright module and browser; no auto install.")
    assert module is not None and executable is not None
    assert Path(module).exists() and Path(executable).is_file()
    quoted_parent = tmp_path / "team's workspace"
    root = unwrap(fixture)(quoted_parent)
    setup = ProjectSetup(root)
    setup.save(setup.read())
    approved(root)
    server = make_server(root, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    script = r"""
const assert=require('assert');
const {chromium}=require(process.argv[1]);
(async()=>{
 const browser=await chromium.launch({executablePath:process.argv[2],headless:true});
 try {
  const page=await browser.newPage({viewport:{width:390,height:844}});
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{
   globalThis.copied=[];
   Object.defineProperty(navigator,'clipboard',{value:{writeText:async value=>copied.push(value)}});
  });
  const base=process.argv[3];
  assert.equal((await page.goto(base+'/setup')).status(),200);
  await page.locator('#setup-steps a[href="#diagnosis"]').click();
  await page.locator('#next-actions').waitFor({state:'visible'});
  const setupCommands=(await page.locator('#commands').textContent()).split('\n');
  assert.equal(setupCommands.length,3);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.route('**/api/setup/bootstrap',async route=>{
   const response=await route.fetch(), data=await response.json();
   data.settings.root="C:\\Team's workspace";
   await route.fulfill({response,json:data});
  });
  await page.reload();
  await page.waitForFunction(()=>document.getElementById('commands').textContent.includes('C:'));
  const windowsCommands=(await page.locator('#commands').textContent()).split('\n');
  assert.deepEqual(windowsCommands,[
   "devroster studio --root 'C:\\Team''s workspace'",
   "devroster dashboard --root 'C:\\Team''s workspace'",
   "devroster setup --check --check-auth --root 'C:\\Team''s workspace'"
  ]);
  assert.equal((await page.goto(base+'/dashboard#task=T')).status(),200);
  const button=page.locator('#actions button[data-command]').last();
  await button.waitFor({state:'visible'});
  const command=await button.getAttribute('data-command');
  assert.equal(command,'devroster task run T');
  await button.click();
  await page.waitForFunction(()=>copied.length===1);
  assert.deepEqual(await page.evaluate(()=>copied),[command]);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.route('**/api/doctor',r=>r.abort());
  await page.locator('#doctor').click();
  await page.waitForFunction(()=>document.getElementById('message').textContent.includes('devroster doctor'));
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({setupCommands,windowsCommands,dashboardCommand:command,width:390,overflow:0,pageErrors:errors}));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
    try:
        result = subprocess.run(
            [
                "node",
                "-e",
                script,
                module,
                executable,
                f"http://127.0.0.1:{server.server_address[1]}",
            ],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=45,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        commands = [shlex.split(command) for command in report["setupCommands"]]
        assert commands == [
            ["devroster", "studio", "--root", str(root)],
            ["devroster", "dashboard", "--root", str(root)],
            ["devroster", "setup", "--check", "--check-auth", "--root", str(root)],
        ]
        print(json.dumps(report, ensure_ascii=False))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
