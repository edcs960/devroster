import pytest

from agent_dev.adapters.integration_catalog import catalog
from agent_dev.adapters.integrations import Integrations
from agent_dev.domain.errors import DomainError


@pytest.mark.parametrize('provider', [p for p in catalog() if p['operations']], ids=lambda p:p['id'])
def test_catalog_read_fixture_and_unknown_write_denied(tmp_path, monkeypatch, provider):
    monkeypatch.setattr('agent_dev.adapters.integrations.remote_address', lambda *a, **kw: None)
    monkeypatch.setattr('agent_dev.adapters.integrations.shutil.which', lambda cmd: '/installed/'+cmd)
    monkeypatch.setattr(Integrations, '_credentials', lambda *a: 'fixture-token')
    operation = next(name for name in provider['operations'] if name != 'execute_sql')

    class FakeClient:
        def __init__(self,*args,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def tools(self):return [{'name':op['tool'],'inputSchema':{'type':'object','additionalProperties':False}} for op in provider['operations'].values()]
        def call(self,name,args):return {'content':[{'type':'text','text':'fixture-token result'}]}

    app=Integrations(tmp_path,client_factory=FakeClient)
    app.configure({'id':'fixture','provider':provider['id'],'scopes':['fixture'],'supported_client':True})
    assert app.check('fixture')['state']=='partial'
    if provider['group'] == 'browser':
        assert app.check('fixture',operation=operation,scope='fixture')['state']=='error'
        with pytest.raises(DomainError): app.call('fixture',operation,{},scope='fixture')
        return
    assert app.check('fixture',operation=operation,scope='fixture')['state']=='ready'
    bundle=app.call('fixture',operation,{},scope='fixture')
    assert bundle['trust']=='untrusted_data'
    assert 'fixture-token' not in str(bundle)
    with pytest.raises(DomainError):app.call('fixture','arbitrary_execute',{},scope='fixture')
    with pytest.raises(DomainError):app.call('fixture',operation,{},scope='other')


def test_oauth_pkce_state_private_token(tmp_path,monkeypatch):
    from urllib.parse import parse_qs, urlsplit

    from agent_dev.adapters.integration_oauth import IntegrationOAuth
    calls=[]
    def request(url,**kwargs):
        calls.append((url,kwargs))
        if 'oauth-protected-resource' in url:return 200,{}, {'resource':'https://resource.example/mcp','authorization_servers':['https://auth.example']}
        if 'oauth-authorization-server' in url:return 200,{}, {'issuer':'https://auth.example','code_challenge_methods_supported':['S256'],'authorization_endpoint':'https://auth.example/authorize','token_endpoint':'https://auth.example/token'}
        return 200,{}, {'token_type':'Bearer','access_token':'private-fixture-token','expires_in':3600}
    monkeypatch.setattr('agent_dev.adapters.integration_oauth.request_remote',request)
    oauth=IntegrationOAuth(tmp_path)
    row={'id':'docs','endpoint':'https://resource.example/mcp','oauth_client_id':'registered-client','version':1}
    auth=oauth.start(row,'http://127.0.0.1:8768/api/integrations/callback')
    query=parse_qs(urlsplit(auth['authorization_url']).query)
    assert query['code_challenge_method']==['S256']
    with pytest.raises(DomainError):oauth.complete('forged','code')
    result=oauth.complete(query['state'][0],'code')
    assert 'private-fixture-token' not in str(result)
    assert oauth.token('docs')=='private-fixture-token'
    assert oauth.path('docs').stat().st_mode&0o777==0o600
    with pytest.raises(DomainError):oauth.complete(query['state'][0],'replay')


def test_malformed_callback_and_resource_binding_rejected(tmp_path):
    from agent_dev.adapters.integration_oauth import IntegrationOAuth
    with pytest.raises(DomainError):
        IntegrationOAuth(tmp_path).start({'oauth_client_id':'registered'},'https://evil.example/callback')
