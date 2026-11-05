import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_dev.adapters import pipe_ownership as ownership
from agent_dev.adapters.bounded_process import _exited_pid, run_bounded


def alive(pid):
    if sys.platform == 'darwin':
        return len(ownership._mac_info(ownership._mac_library(), pid, 3, 136)) == 136
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ')[1][0] != 'Z'
    except FileNotFoundError:
        return False


def cleanup_record(record):
    if not record.exists():
        return
    value = json.loads(record.read_text())
    if not alive(value['pid']):
        return
    if sys.platform == 'darwin':
        import ctypes
        token = (ctypes.c_uint32 * 8)(0, os.getuid(), os.getgid(), os.getuid(), os.getgid(), value['pid'], 0, value['version'])
        ownership._mac_library().proc_signal_with_audittoken(ctypes.byref(token), signal.SIGKILL)
    else:
        # Fixture records its own process start time; pin PID before validating it.
        fd = os.pidfd_open(value['pid'])
        try:
            start = Path(f"/proc/{value['pid']}/stat").read_text().split(') ')[1].split()[19]
            if start == value['version']:
                signal.pidfd_send_signal(fd, signal.SIGKILL)
        finally:
            os.close(fd)


def child_source(record, closed):
    version = ("import ctypes,struct; b=ctypes.create_string_buffer(56); l=ctypes.CDLL('/usr/lib/libproc.dylib'); "
               "l.proc_pidinfo(os.getpid(),17,0,b,56); version=struct.unpack_from('i',b.raw,32)[0]" if sys.platform == 'darwin' else
               "version=pathlib.Path('/proc/self/stat').read_text().split(') ')[1].split()[19]")
    return ("import os,json,pathlib,time; " + ("os.close(1); os.close(2); " if closed else '') + version + '; '
            f"pathlib.Path({str(record)!r}).write_text(json.dumps(dict(pid=os.getpid(),version=version))); time.sleep(6)")


@pytest.mark.skipif(os.name == 'nt', reason='POSIX native pipe ownership')
@pytest.mark.parametrize('double', [False, True])
@pytest.mark.parametrize('mode', ['timeout', 'overflow', 'normal'])
def test_owned_detached_writer_dies_and_unrelated_survives(tmp_path, double, mode):
    record = tmp_path / 'owned.json'
    child = child_source(record, False)
    launcher = ('import os,subprocess,sys,time,pathlib; '
                + ('p=os.fork();\nif p: sys.exit(0)\nos.setsid();\n' if double else '')
                + f'p=subprocess.Popen([sys.executable,"-c",{child!r}],start_new_session=True); '
                + f'path=pathlib.Path({str(record)!r});\nwhile not path.exists(): time.sleep(.01)\n'
                + ('print("x"*1500); sys.stdout.flush()' if mode == 'overflow' else
                   'time.sleep(10)' if mode == 'timeout' else 'sys.exit(0)'))
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])
    started = time.monotonic()
    try:
        if mode == 'overflow':
            assert run_bounded([sys.executable, '-c', launcher],cwd=tmp_path,env=None,timeout=2,limit=1000).overflow
        else:
            with pytest.raises(subprocess.TimeoutExpired):
                run_bounded([sys.executable, '-c', launcher],cwd=tmp_path,env=None,timeout=2,limit=1000)
        assert time.monotonic()-started < 3
        value = json.loads(record.read_text())
        end = time.monotonic()+.5
        while alive(value['pid']) and time.monotonic() < end:
            time.sleep(.01)
        assert not alive(value['pid'])
        assert unrelated.poll() is None
    finally:
        cleanup_record(record)
        unrelated.kill()
        unrelated.wait()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX closed pipe excluded from ownership')
def test_closed_pipe_daemon_is_outside_selected_scope(tmp_path):
    record = tmp_path / 'closed.json'
    child = child_source(record, True)
    command = f'import subprocess,sys,time,pathlib; subprocess.Popen([sys.executable,"-c",{child!r}],start_new_session=True); p=pathlib.Path({str(record)!r});\nwhile not p.exists():time.sleep(.01)'
    try:
        result = run_bounded([sys.executable, '-c', command],cwd=tmp_path,env=None,timeout=2,limit=1000)
        assert result.returncode == 0
        assert alive(json.loads(record.read_text())['pid'])
    finally:
        cleanup_record(record)


def test_normal_eof_does_not_scan_processes(tmp_path, monkeypatch):
    monkeypatch.setattr(ownership, '_mac_cleanup', lambda fd: pytest.fail('normal path scanned'))
    monkeypatch.setattr(ownership, '_linux_cleanup', lambda fd: pytest.fail('normal path scanned'))
    for _ in range(3):
        started=time.monotonic()
        result=run_bounded([sys.executable,'-c','import sys;sys.stdout.buffer.write(b"x"*5000000)'],cwd=tmp_path,env=None,timeout=2,limit=6000000,tail=12000)
        assert result.returncode==0 and not result.overflow
        assert time.monotonic()-started < .5


@pytest.mark.skipif(os.name != 'nt', reason='Windows kernel Job ownership')
def test_windows_job_kills_detached_writer(tmp_path):
    record=tmp_path/'pid'
    child=f'import os,pathlib,time;pathlib.Path({str(record)!r}).write_text(str(os.getpid()));time.sleep(6)'
    launcher=f'import subprocess,sys;subprocess.Popen([sys.executable,"-c",{child!r}],creationflags=subprocess.DETACHED_PROCESS)'
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded([sys.executable,'-c',launcher],cwd=tmp_path,env=None,timeout=2,limit=1000)
    from agent_dev.adapters.process_guard import parent_alive
    assert not parent_alive(int(record.read_text()))


@pytest.mark.parametrize('foreign,changed', [(True, False), (False, True)])
def test_mac_does_not_signal_unproven_identity(monkeypatch, foreign, changed):
    import struct

    class Fake:
        def proc_listpids(self, kind, uid, buffer, size):
            if buffer is not None:
                buffer[0]=123456
            return 4

        def proc_signal_with_audittoken(self, *args):
            pytest.fail('unproven process signalled')

    monkeypatch.setattr(ownership, '_mac_library', lambda: Fake())
    versions=iter([(10,1),(10,2) if changed else (10,1)])
    monkeypatch.setattr(ownership,'_mac_identity',lambda *a: next(versions))
    uid=os.getuid()+1 if foreign else os.getuid()
    bsd=struct.pack('=12I',0,0,0,123456,0,uid,0,uid,0,0,0,0)+b'\0'*88
    monkeypatch.setattr(ownership,'_mac_info',lambda lib,pid,flavor,size: bsd if flavor==3 else struct.pack('=iI',1,6))
    monkeypatch.setattr(ownership,'_mac_pipe',lambda lib,pid,fd: (1,10,20) if pid==os.getpid() else (2,20,10))
    ownership._mac_cleanup(99)


pytest_plugins = ['test_workflow_runtime']


def wait_until(check, seconds=2):
    deadline = time.monotonic() + seconds
    while not check():
        assert time.monotonic() < deadline, 'owned fixture did not reach expected state'
        time.sleep(.01)


def group_launcher(record, ending, ignore_term=False):
    child = child_source(record, False)
    if ignore_term:
        child = 'import signal;signal.signal(signal.SIGTERM,signal.SIG_IGN);' + child
    return (
        'import os,subprocess,sys,time,pathlib; '
        f'subprocess.Popen([sys.executable,"-c",{child!r}],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
        f'path=pathlib.Path({str(record)!r});\nwhile not path.exists():time.sleep(.01)\n'
        'os.write(1,b"preserved-output");\n' + ending
    )


@pytest.mark.skipif(os.name == 'nt', reason='POSIX unreaped group ownership')
@pytest.mark.parametrize('mode', ['normal', 'timeout', 'overflow'])
def test_same_group_closed_pipe_child_terminated_on_all_paths(tmp_path, mode):
    record = tmp_path / 'group-child.json'
    ending = {'normal': 'sys.exit(7)', 'timeout': 'time.sleep(10)',
              'overflow': 'os.write(1,b"x"*1500)'}[mode]
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(10)'])
    try:
        if mode == 'timeout':
            with pytest.raises(subprocess.TimeoutExpired):
                run_bounded([sys.executable, '-c', group_launcher(record, ending)],
                            cwd=tmp_path, env=None, timeout=.4, limit=1000)
        else:
            result = run_bounded([sys.executable, '-c', group_launcher(record, ending)],
                                 cwd=tmp_path, env=None, timeout=2, limit=1000)
            if mode == 'normal':
                assert result.returncode == 7 and result.output == b'preserved-output'
            else:
                assert result.overflow and result.truncated and len(result.output) == 1000
        wait_until(lambda: not alive(json.loads(record.read_text())['pid']), seconds=.8)
        assert unrelated.poll() is None
    finally:
        cleanup_record(record)
        unrelated.kill()
        unrelated.wait()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX unreaped stdio group ownership')
@pytest.mark.parametrize('leader_exited', [True, False])
def test_stdio_closes_group_after_leader_exit_even_if_child_ignores_term(tmp_path, monkeypatch, leader_exited):
    from agent_dev.adapters.stdio_transport import StdioTransport

    record = tmp_path / 'stdio-group.json'
    ending = 'sys.exit(7)' if leader_exited else 'time.sleep(10)'
    transport = StdioTransport([sys.executable, '-c', group_launcher(record, ending, True)],
                               cwd=tmp_path, env=None, deadline=time.monotonic()+5)
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(10)'])
    signals = []
    killpg = os.killpg

    def owned_signal(pid, sig):
        assert pid == transport.process.pid and transport.process.returncode is None
        # The child is still waitable at the instant the group is signalled.
        _exited_pid(pid)
        signals.append(sig)
        killpg(pid, sig)

    try:
        wait_until(record.exists)
        if leader_exited:
            wait_until(lambda: _exited_pid(transport.process.pid) != 0)
        with monkeypatch.context() as context:
            context.setattr(os, 'killpg', owned_signal)
            transport.close()
        assert signals == [signal.SIGTERM, signal.SIGKILL]
        assert transport.process.returncode == (7 if leader_exited else -signal.SIGTERM)
        wait_until(lambda: not alive(json.loads(record.read_text())['pid']), seconds=.8)
        assert unrelated.poll() is None
    finally:
        transport.close()
        cleanup_record(record)
        unrelated.kill()
        unrelated.wait()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX reaped PID must never be signalled')
def test_reaped_leader_does_not_signal_reusable_group(tmp_path, monkeypatch):
    from agent_dev.adapters import bounded_process

    process = subprocess.Popen([sys.executable, '-c', 'pass'], cwd=tmp_path, start_new_session=True)
    assert process.wait() == 0
    monkeypatch.setattr(os, 'killpg', lambda *args: pytest.fail('reaped group signalled'))
    bounded_process._terminate(process)
    assert process.returncode == 0


@pytest.mark.skipif(os.name == 'nt', reason='POSIX pipe cleanup exception preservation')
def test_cleanup_failure_keeps_timeout_and_logs_no_raw_error(tmp_path, monkeypatch, caplog):
    from agent_dev.adapters import bounded_process
    from agent_dev.domain.errors import DomainError, ErrorCode

    def fail_cleanup(fd):
        raise DomainError(ErrorCode.CAPABILITY_UNAVAILABLE, 'fixture-secret-raw-error')

    monkeypatch.setattr(bounded_process, 'cleanup_pipe', fail_cleanup)
    with pytest.raises(subprocess.TimeoutExpired):
        run_bounded([sys.executable, '-c', 'import time;time.sleep(10)'],
                    cwd=tmp_path, env=None, timeout=.1, limit=1000)
    assert 'Process cleanup could not be proven' in caplog.text
    assert 'fixture-secret-raw-error' not in caplog.text


@pytest.mark.skipif(os.name == 'nt', reason='POSIX pipe cleanup fail closed')
def test_normal_cleanup_failure_still_fails_closed(tmp_path, monkeypatch):
    from agent_dev.adapters import bounded_process
    from agent_dev.domain.errors import DomainError, ErrorCode

    def fail_cleanup(fd):
        raise DomainError(ErrorCode.CAPABILITY_UNAVAILABLE, 'cleanup-not-proven')

    monkeypatch.setattr(bounded_process, 'cleanup_pipe', fail_cleanup)
    with pytest.raises(DomainError, match='cleanup-not-proven') as caught:
        run_bounded([sys.executable, '-c', 'print("original-output")'],
                    cwd=tmp_path, env=None, timeout=2, limit=1000)
    assert caught.value.code == ErrorCode.CAPABILITY_UNAVAILABLE


@pytest.mark.skipif(os.name == 'nt', reason='POSIX injected pipe cleanup failure')
@pytest.mark.parametrize('timeout,expected_code', [(False, 126), (True, 124)])
def test_verification_cleanup_failure_records_gate_and_continues(runtime, monkeypatch, timeout, expected_code):
    from test_workflow_runtime import Runner

    from agent_dev.adapters import bounded_process
    from agent_dev.domain.errors import DomainError, ErrorCode

    profile = json.loads(runtime.store.db.rows(
        'SELECT content_json FROM project_profile_versions ORDER BY version DESC LIMIT 1')[0]['content_json'])
    first = 'import time;time.sleep(3)' if timeout else 'print("original-output")'
    profile['verification'] = {'commands': [
        {'name': 'cleanup-error', 'repository': 'source', 'command': [sys.executable, '-c', first], 'timeout_seconds': 1},
        {'name': 'next', 'repository': 'source', 'command': [sys.executable, '-c', 'print("next-gate-ran")']},
    ], 'required_gates': ['cleanup-error', 'next']}
    runtime.store.revise_profile('P', profile, 0)
    runtime.store.create_plan('P', 'RV6-PLAN', 'D', 1, [{'id': 'RV6', 'title': 'Cleanup evidence', 'system_name': 'Example'}])
    runtime.store.approve('plan', 'RV6-PLAN', 1, 'user')
    runtime.store.create_snapshot('RV6', runtime.resolve_snapshot('RV6'))
    assert runtime.execute('RV6', 'rv6-run', runner=Runner())['state'] == 'verifying'
    original = bounded_process.cleanup_pipe
    count = 0

    def first_cleanup_fails(fd):
        nonlocal count
        count += 1
        if count == 1:
            raise DomainError(ErrorCode.CAPABILITY_UNAVAILABLE, 'fixture-secret-raw-error')
        return original(fd)

    monkeypatch.setattr(bounded_process, 'cleanup_pipe', first_cleanup_fails)
    result = runtime.execute('RV6', 'rv6-verify', verify=True)
    assert not result['passed'] and result['state'] == 'blocked'
    rows = runtime.store.db.rows('SELECT gate,exit_code,evidence FROM verification_results')
    gates = {row['gate']: row for row in rows}
    assert gates['cleanup-error']['exit_code'] == expected_code
    expected = 'Verification timeout.' if timeout else 'Verification process cleanup could not be proven.'
    assert json.loads(gates['cleanup-error']['evidence'])['text'] == expected
    assert gates['next']['exit_code'] == 0
    assert 'next-gate-ran' in json.loads(gates['next']['evidence'])['text']
    assert runtime.store.db.rows('SELECT state FROM verification_runs') == [{'state': 'failed'}]
    assert count == 2 and 'fixture-secret-raw-error' not in json.dumps(rows)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX waitid preflight')
def test_missing_waitid_fails_before_spawning(tmp_path, monkeypatch):
    from agent_dev.adapters import bounded_process
    from agent_dev.domain.errors import DomainError, ErrorCode

    # Neither os.waitid nor the macOS libc fallback: refuse before spawning.
    monkeypatch.delattr(os, 'waitid', raising=False)
    monkeypatch.setattr(bounded_process, '_LIBC_WAITID', None)
    monkeypatch.setattr(bounded_process, 'spawn', lambda *a, **k: pytest.fail('unsafe child spawned'))
    with pytest.raises(DomainError) as caught:
        run_bounded([sys.executable, '-c', 'pass'], cwd=tmp_path, env=None, timeout=1, limit=1000)
    assert caught.value.code == ErrorCode.CAPABILITY_UNAVAILABLE


@pytest.mark.skipif(os.name == 'nt', reason='POSIX zombie-only stdio EPERM')
def test_stdio_zombie_only_eperm_keeps_exit_code_and_closes_pipes(tmp_path, monkeypatch):
    from agent_dev.adapters.stdio_transport import StdioTransport

    transport = StdioTransport([sys.executable, '-c', 'import sys;sys.exit(13)'],
                               cwd=tmp_path, env=None, deadline=time.monotonic()+5)
    try:
        wait_until(lambda: _exited_pid(transport.process.pid) != 0)
        signals = []

        def zombie_group(pid, sig):
            assert pid == transport.process.pid and transport.process.returncode is None
            signals.append(sig)
            raise PermissionError(1, 'synthetic zombie EPERM')

        with monkeypatch.context() as context:
            context.setattr(os, 'killpg', zombie_group)
            transport.close()
        assert signals == [signal.SIGTERM, signal.SIGKILL]
        assert transport.process.returncode == 13
        assert all(pipe.closed for pipe in (transport.process.stdin, transport.process.stdout, transport.process.stderr))
    finally:
        transport.close()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX lock serializes Popen poll')
def test_group_signal_is_pinned_against_concurrent_popen_poll(tmp_path, monkeypatch):
    import threading

    from agent_dev.adapters import bounded_process

    process = subprocess.Popen([sys.executable, '-c', 'import sys;sys.exit(7)'], start_new_session=True)
    try:
        wait_until(lambda: _exited_pid(process.pid) != 0)
        observations = []

        def signal_pinned_group(pid, sig):
            results = []
            thread = threading.Thread(target=lambda: results.append(process.poll()))
            thread.start()
            thread.join(timeout=1)
            assert not thread.is_alive() and results == [None]
            assert _exited_pid(pid) == pid
            observations.append(sig)

        with monkeypatch.context() as context:
            context.setattr(os, 'killpg', signal_pinned_group)
            bounded_process._terminate(process)
        assert observations == [signal.SIGKILL] and process.returncode == 7
    finally:
        process.wait()


def test_verification_does_not_swallow_other_domain_errors(runtime, monkeypatch):
    from test_workflow_runtime import Runner

    from agent_dev.adapters import verification
    from agent_dev.domain.errors import DomainError, ErrorCode

    assert runtime.execute('T', 'rv6-other-run', runner=Runner())['state'] == 'verifying'

    def invalid_auth(*args, **kwargs):
        raise DomainError(ErrorCode.AUTH_REQUIRED, 'Authentication still required.')

    monkeypatch.setattr(verification, 'run_bounded', invalid_auth)
    with pytest.raises(DomainError) as caught:
        runtime.execute('T', 'rv6-other-verify', verify=True)
    assert caught.value.code == ErrorCode.AUTH_REQUIRED
    assert runtime.store.db.rows('SELECT state FROM verification_runs') == []
