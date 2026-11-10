import pytest

from agent_dev.adapters.source_explorer import SourceExplorer
from agent_dev.adapters.workspace import initialize
from agent_dev.domain.errors import DomainError


@pytest.fixture
def source(tmp_path):
    initialize(tmp_path)
    (tmp_path/'src').mkdir()
    (tmp_path/'src/a.py').write_text('def a():\n    return 1\n', encoding="utf-8")
    return SourceExplorer(tmp_path)


def test_tree_file_search_and_no_graph_dependency(source):
    assert source.tree('source')['entries'][0]['name']=='src'
    file=source.read('source','src/a.py')
    assert file['lines'][0]=='def a():'
    assert source.search('source','a.py')['results'][0]['path']=='src/a.py'
    assert source.graph('source')['state']=='not_prepared'


@pytest.mark.parametrize('path',['../escape','/etc/passwd','src/../a.py','src/.env','src/secrets/token','src/id_rsa','src/a\\b.py'])
def test_private_and_traversal_rejected(source,path):
    with pytest.raises(DomainError):source.read('source',path)


def test_symlink_and_outside_scope_rejected(source,tmp_path):
    (tmp_path/'secret.txt').write_text('private', encoding="utf-8")
    (tmp_path/'src/link').symlink_to(tmp_path/'secret.txt')
    assert all(row['name']!='link' for row in source.tree('source','src')['entries'])
    for path in ['src/link','secret.txt']:
        with pytest.raises(DomainError):source.read('source',path)


def test_graph_normalization_stale_and_digest(source,tmp_path):
    graph={'nodes':[{'id':'a','name':'a','source_file':'src/a.py','line':1},{'id':'b','name':'b','source_file':'../escape'}], 'links':[{'source':'a','target':'b','evidence':'INFERRED'}]}
    source.publish_graph('source',graph,source.manifest('source'))
    read=source.graph('source','a')
    assert read['state']=='ready'
    assert read['nodes'][1]['source'] is None
    assert read['edges'][0]['evidence']=='INFERRED'
    assert read['edges'][0]['directed'] is False
    assert source.graph('source','a',include_inferred=False)['edges']==[]
    (tmp_path/'src/a.py').write_text('def a():\n    return 2\n', encoding="utf-8")
    assert source.graph('source')['state']=='stale'
    with pytest.raises(DomainError):source.read('source','src/a.py',expected_digest=read['nodes'][0]['source']['digest'])
    source.graph_path('source').write_text('broken', encoding="utf-8")
    assert source.graph('source')['state']=='error'


def test_pagination_and_binary_metadata(source,tmp_path):
    for index in range(205):(tmp_path/f'src/file-{index}.txt').write_text(str(index), encoding="utf-8")
    page=source.tree('source','src')
    assert len(page['entries'])==200 and page['next_cursor']==200
    assert len(source.tree('source','src',200)['entries'])==6
    (tmp_path/'src/binary').write_bytes(b'\x00\xff')
    assert source.read('source','src/binary')['lines']==[]
    (tmp_path/'src/large').write_bytes(b'x'*1_048_577)
    assert 'reason' in source.read('source','src/large')
