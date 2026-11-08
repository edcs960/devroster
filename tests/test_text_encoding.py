"""Text files and process pipes are UTF-8 on every platform.

Windows defaults to a legacy code page (cp1252, cp949), so a call that relies on the
platform default breaks on Korean profiles, prompts and git output there.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SOURCES = sorted((ROOT / "agent_dev").rglob("*.py")) + sorted((ROOT / "scripts").rglob("*.py"))


def _default_encoding_calls(path):
    found = []
    for call in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(call, ast.Call):
            continue
        keywords = {k.arg: k.value for k in call.keywords}
        if "encoding" in keywords or None in keywords:
            continue
        func = call.func
        if isinstance(func, ast.Attribute) and func.attr == "read_text" and not call.args or isinstance(func, ast.Attribute) and func.attr == "write_text" and len(call.args) == 1:
            found.append(call.lineno)
        elif isinstance(func, ast.Name) and func.id == "open" and len(call.args) <= 2:
            mode = call.args[1] if len(call.args) == 2 else keywords.get("mode")
            if mode is None or (isinstance(mode, ast.Constant) and "b" not in mode.value):
                found.append(call.lineno)
        elif (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
            and any(
                isinstance(keywords.get(name), ast.Constant) and keywords[name].value is True
                for name in ("text", "universal_newlines")
            )
        ):
            found.append(call.lineno)
    return found


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_text_io_names_its_encoding(path):
    assert _default_encoding_calls(path) == []


def test_cli_prints_korean_help_through_a_non_utf8_pipe():
    import os
    import subprocess
    import sys

    env = dict(os.environ, PYTHONIOENCODING="ascii", PYTHONUTF8="0")
    result = subprocess.run(
        [sys.executable, "-m", "agent_dev.fa", "--help"],
        capture_output=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    assert "<명령>" in result.stdout.decode("utf-8")
