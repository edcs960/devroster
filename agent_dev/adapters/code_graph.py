"""Refresh the project's Graphify code graph once merged code is the new baseline."""

import os
import shutil
import subprocess
from pathlib import Path

GRAPH = Path("graphify-out/graph.json")
TIMEOUT_SECONDS = 600


def graphify_binary():
    found = shutil.which("graphify")
    if found:
        return found
    # `uv tool install graphifyy` puts the script here, which a GUI-launched PATH can miss.
    fallback = Path.home() / ".local/bin/graphify"
    return str(fallback) if fallback.is_file() and os.access(fallback, os.X_OK) else None


def refresh(project_root):
    """Re-extract code into an existing graph; never raises, the result is only evidence.

    Only a project that already keeps a graph is refreshed, and only code is re-read
    (`graphify update` needs no LLM).
    """
    root = Path(project_root).resolve()
    if not (root / GRAPH).is_file():
        return {"state": "skipped", "reason": "no_graph"}
    binary = graphify_binary()
    if not binary:
        return {"state": "skipped", "reason": "graphify_not_installed"}
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
    try:
        result = subprocess.run(
            [binary, "update", str(root)],
            cwd=root,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"state": "failed", "reason": type(exc).__name__}
    if result.returncode != 0:
        return {"state": "failed", "reason": f"exit {result.returncode}"}
    return {"state": "updated", "graph": GRAPH.as_posix()}
