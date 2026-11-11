"""Preparation persistence, server execution boundaries and real browser flows."""

import http.client
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from test_reusable_workspace import Runner, approved
from test_reusable_workspace import workspace as _workspace
from test_studio import specifications

from agent_dev.adapters.agent_console import AgentConsole
from agent_dev.adapters.preparation import MESSAGE, PreparationModeError, require_ai
from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.app import start_path
from agent_dev.ui.studio_server import make_server

workspace = _workspace

def save_mode(root, enabled):
    service = ProjectSetup(root)
    value = service.read()
    value['create_directories'] = True
    value['ai']['runners'][value['ai']['provider']]['enabled'] = enabled
    return service.save(value)['settings']


def test_disabled_settings_revision_reopen_and_derived_mode(tmp_path, monkeypatch):
    monkeypatch.setenv('DEVROSTER_HOME', str(tmp_path / 'app-home'))
    root = tmp_path / 'project'
    first = save_mode(root, False)
    assert first['revision'] == 1 and first['preparation_mode']
    reopened = ProjectSetup(root).read()
    assert reopened['preparation_mode'] and start_path(root) == '/agent'
    reopened['preparation_mode'] = False  # Client state cannot override the policy.
    second = ProjectSetup(root).save(reopened)['settings']
    assert second['revision'] == 2 and second['preparation_mode']
    from agent_dev.adapters.state.sqlite.database import Database

    db = Database(ProjectSetup(root).db_path, read_only=True)
    assert 'preparation_mode' not in db.rows('SELECT settings_json FROM workspace_settings')[0]['settings_json']
    assert not save_mode(root, True)['preparation_mode']
    require_ai(root)


@pytest.mark.parametrize('size', [0, 51])
def test_verification_bounds_unchanged_in_preparation(tmp_path, size):
    value = ProjectSetup(tmp_path).read()
    value['create_directories'] = True
    value['ai']['runners'][value['ai']['provider']]['enabled'] = False
    value['verification'] = value['verification'] * size
    from agent_dev.domain.errors import DomainError

    with pytest.raises(DomainError, match='필수 검증 명령'):
        ProjectSetup(tmp_path).save(value)


@pytest.mark.parametrize('operation', ['task.run', 'task.resume'])
def test_submit_denied_before_registration(workspace, operation):
    approved(workspace)
    save_mode(workspace, False)
    console = AgentConsole(workspace)
    payload = {'operation': operation, 'fields': {'task_id': 'T'}, 'options': {}}
    payload['preview_digest'] = console.preview(payload)['preview_digest']
    payload['request_id'] = 'preparation-denied'
    try:
        with pytest.raises(PreparationModeError, match='준비 모드'):
            console.start(payload)
        assert console.history() == []
    finally:
        console.close()


def test_current_preparation_blocks_old_snapshot_then_reenable(workspace):
    runtime = approved(workspace)
    save_mode(workspace, False)
    with pytest.raises(PreparationModeError):
        runtime.execute('T', 'denied', runner=Runner())
    assert runtime.store.task('T').state == 'ready'
    save_mode(workspace, True)
    result = runtime.execute('T', 'reenabled', runner=Runner())
    assert result['state'] == 'verifying'




def request(server, path, payload=None, headers=None):
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
    values = {'Content-Type': 'application/json', **(headers or {})}
    try:
        conn.request('POST' if payload is not None else 'GET', path,
                     json.dumps(payload) if payload is not None else None, values)
        response = conn.getresponse()
        return response.status, json.loads(response.read())
    finally:
        conn.close()


def test_real_http_denial_non_ai_edit_csrf_and_reenable(workspace):
    approved(workspace)
    save_mode(workspace, False)
    calls = []

    def generate(*args):
        calls.append(args)
        return specifications()

    server = make_server(workspace, port=0, generator=generate)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, bootstrap = request(server, '/api/studio/bootstrap')
        assert status == 200 and bootstrap['preparation_mode']
        studio_headers = {'X-Studio-Token': bootstrap['token']}
        status, item = request(server, '/api/studio/create',
                               {'title': 'Preparation', 'brief': 'Editable brief'}, studio_headers)
        assert status == 200
        payload = {'id': item['project']['id'], 'version': item['project']['version'], 'kind': 'specs'}
        # Picking another enabled provider cannot bypass the disabled default.
        payload['provider'] = 'claude_cli'
        status, denied = request(server, '/api/studio/generate', payload, studio_headers)
        assert status == 403 and denied['code'] == 'POLICY_VIOLATION' and denied['error'] == MESSAGE
        assert calls == []
        for headers in [{}, {**studio_headers, 'Origin': 'https://other.test'}]:
            assert request(server, '/api/studio/generate', payload, headers)[0] == 403
        status, agent = request(server, '/api/agent/bootstrap')
        assert status == 200 and agent['context']['preparation_mode']
        agent_headers = {'X-Local-Token': agent['token']}
        run = {'operation': 'task.run', 'fields': {'task_id': 'T'}, 'options': {}}
        status, preview = request(server, '/api/agent/preview', run, agent_headers)
        assert status == 200
        run.update(preview_digest=preview['preview_digest'], request_id='denied-http')
        status, denied = request(server, '/api/agent/submit', run, agent_headers)
        assert status == 403 and denied['code'] == 'POLICY_VIOLATION' and denied['error'] == MESSAGE
        for page in ['/api/source/bootstrap', '/api/review/bootstrap?task=T', '/api/design-references/bootstrap']:
            assert request(server, page)[0] == 200
        save_mode(workspace, True)
        payload.pop('provider')
        assert request(server, '/api/studio/generate', payload, studio_headers)[0] == 200
        for _ in range(100):
            if calls:
                break
            time.sleep(.02)
        assert len(calls) == 1
    finally:
        server.shutdown()
        server.studio.close()
        server.server_close()
        thread.join(timeout=5)


def test_preparation_browser(workspace, tmp_path, monkeypatch):
    module = os.environ.get('AGENT_DEV_TEST_PLAYWRIGHT_MODULE')
    executable = os.environ.get('AGENT_DEV_TEST_BROWSER_EXECUTABLE')
    if not module or not executable or not shutil.which('node'):
        pytest.skip('Opt in to installed Playwright/browser; no installation.')
    monkeypatch.setenv('DEVROSTER_HOME', str(tmp_path / 'app-home'))
    # A fresh management folder knows no workspace yet; trust the fixture explicitly.
    from agent_dev.adapters import workspace_trust
    workspace_trust.trust(workspace)
    approved(workspace)
    save_mode(workspace, False)
    server = make_server(workspace, port=0)
    item = server.studio.studio.create('Preparation browser', 'Editable brief')
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(['node', str(Path(__file__).with_name('preparation_mode_browser.cjs')),
                                 module, executable, f'http://127.0.0.1:{server.server_port}',
                                 item['project']['id'], str(tmp_path)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
        print(result.stdout)
    finally:
        server.shutdown()
        server.studio.close()
        server.server_close()
        thread.join(timeout=5)
