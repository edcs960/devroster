"""The wiki's Markdown view can live in a chosen project folder (profile ``wiki_dir``)."""

import pytest

from agent_dev.adapters.wiki_knowledge import WIKI_DIR, wiki_dir
from agent_dev.domain.errors import DomainError


def test_wiki_dir_accepts_only_project_relative_folders():
    assert wiki_dir("tools/docs/wiki/").as_posix() == "tools/docs/wiki"
    for bad in ("/abs/wiki", "../out", "docs/../../x", "", "  ", "a\\b", 3):
        with pytest.raises(DomainError):
            wiki_dir(bad)


def test_setup_round_trips_wiki_dir_and_render_uses_it(tmp_path):
    import subprocess

    from agent_dev.adapters.project_setup import ProjectSetup
    from agent_dev.adapters.state.sqlite.database import Database
    from agent_dev.adapters.state.sqlite.store import WorkflowRepository
    from agent_dev.adapters.wiki_knowledge import WikiKnowledge

    project = tmp_path / "p"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    setup = ProjectSetup(project)
    value = setup.read()
    assert value["wiki_dir"] == ""
    value["wiki_dir"] = "tools/engine/docs/wiki"
    setup.save(value)
    assert setup.read()["wiki_dir"] == "tools/engine/docs/wiki"
    store = WorkflowRepository(Database(setup.db_path))
    wiki = WikiKnowledge(store, setup.root)
    result = wiki.render(value["project_id"])
    assert result["directory"] == "tools/engine/docs/wiki"
    assert (project / "tools/engine/docs/wiki/index.md").is_file()
    assert not (project / WIKI_DIR).exists()
    value = setup.read()
    value["wiki_dir"] = ""
    setup.save(value)
    assert wiki.render_dir(value["project_id"]) == WIKI_DIR
    with pytest.raises(DomainError):
        setup.save(dict(setup.read(), wiki_dir="../elsewhere"))
