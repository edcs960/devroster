"""Built-in code index (#29): import relations when no Graphify graph exists."""

import pytest

from agent_dev.adapters.code_index import CodeIndex, parse, related
from agent_dev.adapters.source_explorer import SourceExplorer
from agent_dev.adapters.source_search import SourceSearch
from agent_dev.adapters.workspace import initialize


@pytest.fixture
def root(tmp_path):
    initialize(tmp_path)
    src = tmp_path / "src"
    files = {
        "app/__init__.py": "",
        "app/core.py": "def run():\n    return 1\n\nclass Engine:\n    pass\n",
        "app/api.py": "from .core import run\nfrom . import util\nimport json\n",
        "app/util.py": "from app.core import Engine\n",
        "tests/test_core.py": "from app.core import run\n",
        "web/main.ts": "import { x } from './lib/x.js';\nimport React from 'react';\n// import './commented';\nexport * from \"./lib/y\";\n",
        "web/lib/x.ts": "export const x = 1;\n",
        "web/lib/y/index.ts": "export const y = 2;\n",
        "web/lib/x.test.ts": "const { x } = require('./x');\n/* import './gone' */\n",
        "java/com/acme/Order.java": "package com.acme;\nimport com.acme.util.Money;\nimport java.util.List;\npublic class Order {}\n",
        "java/com/acme/util/Money.java": "package com.acme.util;\npublic class Money {}\n",
        "broken.py": "def (:\n",
    }
    for name, text in files.items():
        path = src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path


def imports(index, path):
    return index["files"][path]["imports"]


def test_python_script_and_java_imports_resolve_to_module_files(root):
    index = CodeIndex(SourceExplorer(root)).build("source")
    assert index["partial"] is False
    assert imports(index, "src/app/api.py") == ["src/app/core.py", "src/app/util.py"]
    assert imports(index, "src/app/util.py") == ["src/app/core.py"]
    assert imports(index, "src/tests/test_core.py") == ["src/app/core.py"]
    assert imports(index, "src/web/main.ts") == ["src/web/lib/x.ts", "src/web/lib/y/index.ts"]  # not react, not comments
    assert imports(index, "src/web/lib/x.test.ts") == ["src/web/lib/x.ts"]
    assert imports(index, "src/java/com/acme/Order.java") == ["src/java/com/acme/util/Money.java"]
    assert index["files"]["src/app/core.py"]["defines"] == ["run", "Engine"]
    assert imports(index, "src/broken.py") == []  # A syntax error is an empty row, not a failure.


def test_related_lists_both_directions():
    index = {"files": {"a.py": {"imports": ["b.py"]}, "b.py": {"imports": ["a.py"]}, "c.py": {"imports": ["a.py"]}}}
    rows = related(index, "a.py")
    assert rows["b.py"]["direction"] == "mixed" and rows["c.py"]["direction"] == "incoming"
    assert all(row["evidence"] == "INFERRED" for row in rows.values())


def test_rebuild_reparses_only_changed_files(root, monkeypatch):
    explorer = SourceExplorer(root)
    CodeIndex(explorer).build("source")
    seen = []
    import agent_dev.adapters.code_index as module

    original = module.parse
    monkeypatch.setattr(module, "parse", lambda path, text: seen.append(path) or original(path, text))
    (root / "src/app/util.py").write_text("import app.api\n", encoding="utf-8")
    index = CodeIndex(explorer).build("source")
    assert seen == ["src/app/util.py"]
    assert imports(index, "src/app/util.py") == ["src/app/api.py"]


def test_impact_uses_the_index_when_there_is_no_graph(root):
    impact = SourceSearch(SourceExplorer(root), b"k" * 32).impact("source", "src/app/core.py")
    assert impact["state"] == "not_prepared" and impact["source"] == "code_index"
    assert {row["path"] for row in impact["related"]} == {"src/app/api.py", "src/app/util.py", "src/tests/test_core.py"}
    assert impact["tests"] == [{"path": "src/tests/test_core.py", "evidence": "INFERRED", "reason": "import 관계"}]


def test_unknown_languages_are_skipped():
    assert parse("README.md", "# x") is None
