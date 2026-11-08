"""Regression evidence for review findings across CLI, transport and workflow boundaries."""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_dev.adapters.integrations import Integrations
from agent_dev.adapters.mcp_client import request_remote
from agent_dev.adapters.source_explorer import SourceExplorer
from agent_dev.adapters.source_jobs import SourceJobs
from agent_dev.adapters.workspace import initialize
from agent_dev.application.workflow_dag import DagExecution
from agent_dev.domain.content import redact
from agent_dev.domain.errors import DomainError

pytest_plugins = ['test_workflow_runtime']


def test_scopes_parent_and_traversal(tmp_path):
    app = Integrations(tmp_path)
    row = {'scopes':['safe'], 'bound_arguments':{}, 'provider':'stitch'}
    assert app._bound(row, {'parent':'projects/safe'}, 'safe')['parent']=='projects/safe'
    for args in [{'parent':'projects/other'}, {'path':'safe/../private'}, {'path':'safe/%2e%2e/private'}]:
        with pytest.raises(DomainError): app._bound(row,args,'safe')


def test_import_custom_reconnect_and_validation_atomic(tmp_path):
    app=Integrations(tmp_path)
    app.configure({'id':'local','provider':'custom','transport':'stdio','command':['installed-mcp'],'operations':{'read':{'tool':'read','capability':'docs.read'}}})
    imported=Integrations(tmp_path/'other')
    result=imported.import_document(app.export())
    assert result['connections'][0]['enabled'] is False
    assert result['connections'][0]['command']==[]
    assert imported.check('local')['state']=='disabled'
    before=app.list()
    with pytest.raises(DomainError):
        app.import_document({'schema_version':1,'connections':[{'id':'first','provider':'context7'}, {'id':'bad','provider':'missing'}]})
    assert app.list()==before


def test_figma_rest_uses_requested_endpoint_and_node_ids(tmp_path, monkeypatch):
    app=Integrations(tmp_path)
    row={'scopes':['AbC'], 'credential_ref':'FIGMA_PAT'}
    monkeypatch.setattr(app,'_credentials',lambda row:'private-pat')
    seen=[]
    def request(url,**kw):seen.append((url,kw));return 200,{}, {'nodes':{}}
    monkeypatch.setattr('agent_dev.adapters.mcp_client.request_remote', request)
    app._figma(row,'AbC','get_nodes',{'ids':['12:34']},1)
    app._figma(row,'AbC','get_images',{'ids':['12:34'],'scale':2},1)
    assert '/files/AbC/nodes?ids=12%3A34' in seen[0][0]
    assert '/images/AbC?' in seen[1][0] and 'scale=2' in seen[1][0]
    assert all(kw['timeout']==1 for _,kw in seen)
    with pytest.raises(DomainError):app._figma(row,'AbC','get_nodes',{})


def test_slow_drip_http_is_bounded():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            raw=b'{"result":{}}'
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers()
            try:
                for item in raw:self.wfile.write(bytes([item]));self.wfile.flush();time.sleep(.04)
            except OSError:pass
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    started=time.monotonic()
    try:
        with pytest.raises(DomainError):request_remote(f'http://127.0.0.1:{server.server_port}',local=True,timeout=.1)
        assert time.monotonic()-started < .7
    finally:server.shutdown();server.server_close();thread.join(2)


def test_tool_cumulative_budget_and_timeout(runtime):
    calls=[]
    dag=DagExecution(SimpleNamespace(store=runtime.store,tools=lambda *a,**kw:calls.append(kw) or {}))
    node={'id':'docs','connection_id':'docs','operation':'read','scope':'S','capability':'docs.read','budget':{'max_calls':1,'timeout_seconds':1}}
    resolved={'tools':[{'connection_id':'docs','operations':['read'],'scope':'S','capabilities':['docs.read']}]}
    dag.tool(node,resolved,'T')
    with pytest.raises(DomainError,match='TOOL_CALL_LIMIT'):dag.tool(node,resolved,'T')
    assert calls==[{'scope':'S','timeout_seconds':1}]


def test_passed_role_recovers_without_duplicate_model_call(runtime):
    from test_workflow_dag import Passing, prepare
    graph={'schema_version':1,'id':'recover','name':'Recover','nodes':[{'id':'developer','kind':'role','role_id':'developer'}],'edges':[]}
    prepare(runtime,graph)
    original=runtime.store.record_node
    def crash(*args,**kw):
        if args[3]=='passed':raise KeyboardInterrupt('crash after attempt commit')
        return original(*args,**kw)
    runner=Passing()
    with patch.object(runtime.store,'record_node',side_effect=crash),pytest.raises(KeyboardInterrupt):runtime.execute('DAG','first',runner=runner)
    assert runtime.store.passed_attempt('DAG','developer',0)
    assert runtime.execute('DAG','resume',runner=runner)['state']=='verifying'
    assert runner.calls==['developer']


def test_graph_reads_each_source_once_and_malformed_build_finishes(tmp_path,monkeypatch):
    initialize(tmp_path);(tmp_path/'src').mkdir();(tmp_path/'src/a.py').write_text('def a(): pass\n', encoding="utf-8")
    app=SourceExplorer(tmp_path)
    with patch.object(app,'read',wraps=app.read) as read:
        result=app.normalize_graph('source',{'nodes':[{'id':str(i),'source_file':'src/a.py'} for i in range(100)]})
        assert len(result['nodes'])==100 and read.call_count==1
    binary=tmp_path/'fake-graphify'
    binary.write_text(f'#!{sys.executable}\nimport pathlib,sys\nout=pathlib.Path(sys.argv[sys.argv.index("--out")+1]);out.mkdir();(out/"graph.json").write_text(\'{{"nodes":["bad"]}}\')\n', encoding="utf-8")
    binary.chmod(0o700)
    monkeypatch.setattr('agent_dev.adapters.source_jobs.shutil.which',lambda _:str(binary))
    jobs=SourceJobs(tmp_path)
    job=jobs.start('source')
    try:
        for _ in range(100):
            result=jobs.status(job['id'])
            if result['state'] not in {'queued','running'}:break
            time.sleep(.05)
        assert result['state']=='error'
        assert not app.graph_path('source').exists()
    finally:jobs.close()


def test_public_policy_survives_redaction_but_secret_cannot_hide():
    assert redact({'token_mode':'balanced','token_measurement':'estimated'})=={'token_mode':'balanced','token_measurement':'estimated'}
    assert redact({'token_mode':'private-value','token':'private-value'})=={'token_mode':'****','token':'****'}


def test_dns_deadline_and_gate_output_budget(tmp_path,monkeypatch):
    from agent_dev.adapters.guarded_verification import run_gate
    from agent_dev.adapters.state.sqlite.database import Database
    from agent_dev.adapters.state.sqlite.store import WorkflowRepository
    from agent_dev.adapters.state.sqlite.safety import ExecutionSafety
    import subprocess
    def delayed(*args,**kwargs):time.sleep(.2);return []
    with monkeypatch.context() as scoped:
        scoped.setattr('agent_dev.adapters.mcp_client.socket.getaddrinfo',delayed)
        start=time.monotonic()
        with pytest.raises(DomainError):request_remote('https://slow.example/mcp',timeout=.02)
        assert time.monotonic()-start<.15
    store=WorkflowRepository(Database(tmp_path/'state.db'))
    safety=ExecutionSafety(store);lease=safety.acquire('gate','test',ttl=30)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            run_gate([sys.executable,'-c',"import sys;sys.stdout.write('x'*2100000)"],tmp_path,{},2,store.db.path,lease)
    finally:safety.release(lease)


def test_integrated_routes_readonly_and_oauth_top_level_callback(tmp_path,monkeypatch):
    from agent_dev.ui.studio_server import make_server
    import http.client
    server=make_server(tmp_path,0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        for page in ['agent','setup','studio','workflows','integrations','references','source']:
            connection=http.client.HTTPConnection('127.0.0.1',server.server_port)
            connection.request('GET','/'+page);response=connection.getresponse()
            assert response.status==200 and b'/navigation.js' in response.read()
            connection.close()
        # Development status is part of the agent workroom; the old address forwards there.
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port)
        connection.request('GET','/dashboard');response=connection.getresponse();response.read()
        assert response.status==302 and response.getheader('Location')=='/agent'
        connection.close()
        assert not (tmp_path/'.agent-dev/state.db').exists()
        # A forged OAuth callback reaches state validation instead of being mistaken for CSRF.
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port)
        connection.request('GET','/api/integrations/callback?state=forged&code=code',headers={'Sec-Fetch-Site':'cross-site','Sec-Fetch-Mode':'navigate'})
        response=connection.getresponse();assert response.status==400
        assert 'state' in response.read().decode().lower()
        connection.close()
        connection=http.client.HTTPConnection('127.0.0.1',server.server_port)
        connection.request('GET','/api/source/bootstrap?module=source&module=other')
        response=connection.getresponse();assert response.status==400;response.read();connection.close()
    finally:server.shutdown();server.studio.close();server.server_close();thread.join(2)


def test_utf8_graph_cache_under_non_utf8_default_locale(tmp_path, monkeypatch):
    from pathlib import Path
    initialize(tmp_path)
    (tmp_path/'src').mkdir()
    (tmp_path/'src/a.py').write_text('def greet(): return "안녕하세요"\n', encoding='utf-8')
    source=SourceExplorer(tmp_path)
    original=Path.read_text
    def locale_read(path,*args,**kwargs):
        kwargs.setdefault('encoding','cp1252')
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',locale_read)
    graph={'nodes':[{'id':'greet','name':'안녕하세요','source_file':'src/a.py'}]}
    source.publish_graph('source',graph,source.manifest('source'))
    assert source.graph('source')['nodes'][0]['label']=='안녕하세요'
