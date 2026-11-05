"""Regression cases from DevRoster AUD-01/02/03/08/10 (real local resources)."""

import json
import os
import subprocess
import sys
import time

import pytest
from test_workspace_switching import running

from agent_dev.adapters.bounded_process import run_bounded
from agent_dev.adapters.development_review import _bounded
from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.adapters.workspace_registry import configuration_for, register_root
from agent_dev.domain.errors import DomainError


def test_re_registration_keeps_legacy_settings_and_revision(tmp_path, monkeypatch):
    from agent_dev.core import paths

    root, engine = tmp_path / 'legacy', tmp_path / 'legacy/engine'
    (engine / 'config').mkdir(parents=True)
    (engine / 'config/runtime.json').write_text(json.dumps({'system_name': 'Legacy'}))
    monkeypatch.setattr(paths, 'AGENT_DEV_ROOT', engine)
    monkeypatch.setattr(paths, 'PROJECT_ROOT', root)
    monkeypatch.setenv('DEVROSTER_HOME', str(tmp_path / 'home'))
    register_root(engine, remember=True)
    revision = ProjectSetup(engine).read()['revision']
    for remember in (False, True, False):
        register_root(root, remember=remember)
        assert configuration_for(root) == engine
        assert ProjectSetup(configuration_for(root)).read()['revision'] == revision
    other = tmp_path / 'other'
    other.mkdir()
    register_root(other, remember=True)
    register_root(root, remember=True)
    assert configuration_for(root) == engine


def test_migrates_legacy_last_used_mapping_when_registering_other_root(tmp_path, monkeypatch):
    from agent_dev.core import paths

    root, engine = tmp_path / 'legacy', tmp_path / 'legacy/engine'
    (engine / 'config').mkdir(parents=True)
    (engine / 'config/runtime.json').write_text('{}')
    monkeypatch.setattr(paths, 'AGENT_DEV_ROOT', engine)
    monkeypatch.setattr(paths, 'PROJECT_ROOT', root)
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('DEVROSTER_HOME', str(home))
    (home / 'app.json').write_text(json.dumps({'root': str(engine)}))
    other = tmp_path / 'other'
    other.mkdir()
    register_root(other, remember=True)
    register_root(root, remember=True)
    assert configuration_for(root) == engine


@pytest.mark.parametrize('failure_at', [1, 2])
def test_registry_write_failure_rolls_back_only_new_session(tmp_path, monkeypatch, failure_at):
    from agent_dev.ui import workspace_sessions

    monkeypatch.setenv('DEVROSTER_HOME', str(tmp_path / 'home'))
    a, b = tmp_path / 'a', tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    register_root(b)
    with running(a) as (server, _):
        manager = server.studio.workspace_sessions
        original = workspace_sessions.register_root
        count = 0

        def fail_write(*args, **kwargs):
            nonlocal count
            count += 1
            if count == failure_at:
                raise OSError('synthetic write failure')
            return original(*args, **kwargs)

        with monkeypatch.context() as context:
            context.setattr(workspace_sessions, 'register_root', fail_write)
            with pytest.raises(OSError):
                manager.open(str(b), a)
        assert str(b) not in manager.servers
        assert not manager.children
        assert manager.servers[str(a)] is server
        manager.open(str(b), a)
        child = manager.servers[str(b)]
        with monkeypatch.context() as context:
            context.setattr(workspace_sessions, 'register_root', lambda *a, **k: (_ for _ in ()).throw(OSError()))
            with pytest.raises(OSError):
                manager.open(str(b), a)
        assert manager.servers[str(b)] is child
        assert len(manager.children) == 1


@pytest.mark.parametrize('script', [
    'import time; time.sleep(10)',
    'import time; print("start", flush=True); time.sleep(10)',
    'import sys,time; sys.stderr.write("x"*200000); sys.stderr.flush(); time.sleep(10)',
])
def test_git_deadline_covers_stream_reads(tmp_path, monkeypatch, script):
    from agent_dev.adapters import bounded_process

    popen = subprocess.Popen
    processes = []

    def substitute(command, **kwargs):
        p = popen([sys.executable, '-c', script], **kwargs)
        processes.append(p)
        return p

    monkeypatch.setattr(bounded_process.subprocess, 'Popen', substitute)
    start = time.monotonic()
    with pytest.raises(DomainError, match='제한시간'):
        _bounded(tmp_path, ['status'], 1000, timeout=0.1)
    assert time.monotonic() - start < 1
    assert processes[0].poll() is not None
    assert processes[0].stdout.closed


@pytest.mark.parametrize('tail', [None, 12000])
def test_output_limit_and_tail_are_bounded(tmp_path, tail):
    result = run_bounded([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(b"x"*3000000)'],
                         cwd=tmp_path, env=None, timeout=5, limit=2000000, tail=tail)
    assert result.overflow and result.truncated
    assert len(result.output) == (tail or 2000000)


def test_keeps_stderr_and_marks_tail_truncation_without_failing(tmp_path):
    result = run_bounded([sys.executable, '-c', 'import sys; sys.stderr.write("x"*20000+"END")'],
                         cwd=tmp_path, env=None, timeout=5, limit=2000000, tail=12000)
    assert result.returncode == 0 and not result.overflow and result.truncated
    assert len(result.output) == 12000 and result.output.endswith(b'END')


@pytest.mark.skipif(os.name == 'nt', reason='POSIX process-group cleanup')
def test_deadline_kills_descendants_holding_the_output_pipe(tmp_path):
    marker = tmp_path / 'child-ran'
    child = f'import time,pathlib; time.sleep(0.5); pathlib.Path({str(marker)!r}).write_text("bad")'
    script = f'import subprocess,sys; subprocess.Popen([sys.executable,"-c",{child!r}])'
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded([sys.executable, '-c', script], cwd=tmp_path, env=None, timeout=0.1, limit=1000)
    time.sleep(0.6)
    assert not marker.exists()




pytest_plugins = ['test_workflow_runtime']


@pytest.mark.parametrize('size,expected_code', [(20000, 0), (3000000, 125), (2000500, 125)])
def test_verification_output_budget_preserves_gate_and_audit_contract(runtime, size, expected_code):
    from test_workflow_runtime import Runner

    profile = json.loads(runtime.store.db.rows('SELECT content_json FROM project_profile_versions ORDER BY version DESC LIMIT 1')[0]['content_json'])
    profile['verification']['commands'][0]['command'] = [sys.executable, '-c', f'import sys; sys.stdout.write("x"*{size}+"END")']
    runtime.store.revise_profile('P', profile, 0)
    runtime.store.create_plan('P', 'AUD-PLAN', 'D', 1, [{'id':'AUD','title':'Console text','system_name':'Example'}])
    runtime.store.approve('plan', 'AUD-PLAN', 1, 'user')
    runtime.store.create_snapshot('AUD', runtime.resolve_snapshot('AUD'))
    assert runtime.execute('AUD', 'audit-run', runner=Runner())['state'] == 'verifying'
    result = runtime.execute('AUD', 'audit-verify', verify=True)
    assert result['passed'] == (expected_code == 0)
    rows = runtime.store.db.rows("SELECT exit_code,evidence FROM verification_results WHERE gate='unit'")
    assert rows[0]['exit_code'] == expected_code
    evidence = json.loads(rows[0]['evidence'])['text']
    assert 'Output truncated' in evidence
    assert len(evidence) < 12300
    if expected_code == 0:
        assert evidence.endswith('END')
    else:
        assert 'Verification output limit exceeded' in evidence
    assert runtime.store.db.rows("SELECT state FROM verification_runs")[0]['state'] == ('passed' if expected_code == 0 else 'failed')




def test_small_immediate_overflow_repeatedly_reaps_and_closes(tmp_path, monkeypatch):
    from agent_dev.adapters import bounded_process

    popen = subprocess.Popen
    processes = []

    def tracked(command, **kwargs):
        process = popen(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(bounded_process.subprocess, 'Popen', tracked)
    for _ in range(20):
        result = run_bounded(
            [sys.executable, '-c', 'import sys; sys.stdout.write("x"*1500)'],
            cwd=tmp_path, env=None, timeout=5, limit=1000,
        )
        assert result.overflow and result.truncated
        assert len(result.output) == 1000
    assert all(p.poll() is not None and p.stdout.closed for p in processes)


def test_git_diff_larger_than_limit_returns_truncated_prefix(tmp_path):
    def git(*args):
        return subprocess.run(
            ['git', '-C', str(tmp_path), *args], capture_output=True, check=True
        ).stdout

    git('init', '-q')
    source = tmp_path / 'source.txt'
    source.write_text('baseline\n')
    git('add', 'source.txt')
    git('-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'base')
    source.write_text(''.join(f'line {i}\n' for i in range(4000)))
    whole, cut = _bounded(tmp_path, ['diff', '--no-ext-diff', '--no-textconv'], 100000)
    assert not cut
    limit = len(whole) - 100
    output, cut = _bounded(tmp_path, ['diff', '--no-ext-diff', '--no-textconv'], limit)
    assert cut and len(output) == limit and output == whole[:limit]


@pytest.mark.skipif(os.name == 'nt', reason='POSIX killpg fallback')
@pytest.mark.parametrize('already_exited', [False, True])
def test_permission_error_cleanup_reaps_leader(tmp_path, monkeypatch, already_exited):
    from agent_dev.adapters import bounded_process

    popen = subprocess.Popen
    processes = []

    def tracked(command, **kwargs):
        process = popen(command, **kwargs)
        processes.append(process)
        return process

    def permission_denied(pid, signal):
        raise PermissionError(1, 'synthetic EPERM')

    monkeypatch.setattr(bounded_process.subprocess, 'Popen', tracked)
    monkeypatch.setattr(bounded_process.os, 'killpg', permission_denied)
    if already_exited:
        result = run_bounded([sys.executable, '-c', 'pass'], cwd=tmp_path, env=None, timeout=5, limit=1000)
        assert result.returncode == 0
    else:
        with pytest.raises(subprocess.TimeoutExpired):
            run_bounded([sys.executable, '-c', 'import time; time.sleep(10)'],
                        cwd=tmp_path, env=None, timeout=0.1, limit=1000)
    assert processes[0].poll() is not None and processes[0].stdout.closed


def legacy_registry(tmp_path, monkeypatch):
    from agent_dev.core import paths

    root, engine = tmp_path / 'legacy', tmp_path / 'legacy/engine'
    (engine / 'config').mkdir(parents=True)
    (engine / 'config/runtime.json').write_text('{}')
    monkeypatch.setattr(paths, 'AGENT_DEV_ROOT', engine)
    monkeypatch.setattr(paths, 'PROJECT_ROOT', root)
    monkeypatch.setenv('DEVROSTER_HOME', str(tmp_path / 'home'))
    register_root(engine, remember=True)
    assert configuration_for(root) == engine
    return root, engine


def test_managed_configuration_supersedes_live_legacy_mapping(tmp_path, monkeypatch):
    root, engine = legacy_registry(tmp_path, monkeypatch)
    managed = root / '.agent-dev'
    managed.mkdir()
    (managed / 'workspace.json').write_text(json.dumps({'format_version': 1, 'project_id': 'p', 'name': 'p'}))
    assert engine.exists() and ProjectSetup(root).config == managed
    register_root(root, remember=True)
    assert configuration_for(root) == managed
    register_root(engine)
    assert configuration_for(root) == managed


def test_deleted_legacy_configuration_is_replaced(tmp_path, monkeypatch):
    import shutil

    root, engine = legacy_registry(tmp_path, monkeypatch)
    shutil.rmtree(engine)
    assert not configuration_for(root).exists()
    register_root(root, remember=True)
    assert configuration_for(root) == ProjectSetup(root).config == root
    assert configuration_for(root).exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX detached pipe owner')
def test_detached_pipe_owner_does_not_extend_deadline(tmp_path, monkeypatch):
    import signal
    import threading

    from agent_dev.adapters import bounded_process

    child_pid = tmp_path / 'detached-pid'
    child = 'import time; time.sleep(6)'
    script = (
        'import subprocess,sys,pathlib; '
        f'p=subprocess.Popen([sys.executable,"-c",{child!r}],start_new_session=True); '
        f'pathlib.Path({str(child_pid)!r}).write_text(str(p.pid))'
    )
    popen = subprocess.Popen
    processes = []

    def tracked(command, **kwargs):
        process = popen(command, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(bounded_process.subprocess, 'Popen', tracked)
    before = set(threading.enumerate())
    start = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            run_bounded([sys.executable, '-c', script], cwd=tmp_path, env=None, timeout=2, limit=1000)
        elapsed = time.monotonic() - start
        assert elapsed < 3, f'deadline=2s, actual={elapsed:.3f}s'
        assert processes[0].poll() is not None and processes[0].stdout.closed
        assert not [t for t in threading.enumerate() if t not in before and '(collect)' in t.name]
    finally:
        # The child deliberately escaped the group; clean up only this owned fixture.
        if child_pid.exists():
            try:
                os.kill(int(child_pid.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize('damaged', [123, 'relative/path', ''])
def test_corrupt_mapping_is_repaired_on_registration(tmp_path, monkeypatch, damaged):
    from agent_dev.adapters.workspace_registry import state_file

    monkeypatch.setenv('DEVROSTER_HOME', str(tmp_path / 'home'))
    root = tmp_path / 'project'
    root.mkdir()
    register_root(root, remember=True)
    path = state_file()
    value = json.loads(path.read_text())
    value['configurations'][str(root)] = damaged
    path.write_text(json.dumps(value))
    register_root(root, remember=True)
    assert configuration_for(root) == ProjectSetup(root).config
