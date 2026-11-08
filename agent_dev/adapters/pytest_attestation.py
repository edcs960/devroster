"""Host pytest hook observations, separate from general exit-code gate success.

Only this host interpreter's ``python -m pytest`` is supported. Reports are
observations of trusted local test execution, not a sandbox for hostile tests.
"""

import contextlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path, PurePosixPath, PureWindowsPath

from agent_dev.domain.content import digest

MAX_REPORT = 1_000_000
PLUGIN = """
import json, os
_selected, _excluded, _reports = [], [], []
def _item(item):
    return {"nodeid": item.nodeid, "path": str(item.path.resolve())}
def pytest_deselected(items):
    _excluded.extend(_item(item) for item in items)
def pytest_collection_finish(session):
    _selected.extend(_item(item) for item in session.items)
def pytest_runtest_logreport(report):
    _reports.append({"nodeid": report.nodeid, "when": report.when,
                     "outcome": report.outcome, "xfail": hasattr(report, "wasxfail")})
def pytest_sessionfinish(session, exitstatus):
    value = {"nonce": NONCE, "exit_code": int(exitstatus), "selected": _selected,
             "excluded": _excluded, "reports": _reports}
    with open(DESTINATION, "x", encoding="utf-8") as stream:
        os.chmod(DESTINATION, 0o600)
        json.dump(value, stream)
"""


def _read(path, nonce, code, root, command):
    """Validate raw observations before incorporating them into trusted evidence."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_REPORT:
                return None
            raw = stream.read(MAX_REPORT + 1)
        if len(raw) > MAX_REPORT:
            return None
        value = json.loads(raw)
        if value["nonce"] != nonce or value["exit_code"] != code:
            return None
        selected, excluded, reports = value["selected"], value["excluded"], value["reports"]
        if not all(isinstance(v, list) and len(v) <= 10_000 for v in (selected, excluded, reports)):
            return None
        if any(
            not isinstance(item, dict)
            or not isinstance(item.get("nodeid"), str)
            or not isinstance(item.get("path"), str)
            for item in selected + excluded
        ) or any(
            not isinstance(r, dict)
            or not isinstance(r.get("nodeid"), str)
            or r.get("when") not in {"setup", "call", "teardown"}
            or r.get("outcome") not in {"passed", "failed", "skipped"}
            or type(r.get("xfail")) is not bool
            for r in reports
        ):
            return None
        normalized = []
        seen = set()
        for item in selected + excluded:
            node = item["nodeid"]
            path = Path(item["path"])
            if not isinstance(node, str) or len(node) > 2000 or node in seen:
                return None
            seen.add(node)
            if not path.is_absolute() or not path.resolve().is_relative_to(root):
                return None
            suffix = node.partition("::")[2]
            canonical_id = path.resolve().relative_to(root).as_posix() + (
                "::" + suffix if suffix else ""
            )
            phases = [r for r in reports if r.get("nodeid") == node]
            passed = (
                item in selected
                and len(phases) == 3
                and {r["when"] for r in phases} == {"setup", "call", "teardown"}
                and all(r["outcome"] == "passed" and r["xfail"] is False for r in phases)
            )
            normalized.append({"nodeid": canonical_id, "passed": passed})
        if any(not isinstance(r, dict) or r.get("nodeid") not in seen for r in reports):
            return None
        return {"runner": "host-pytest-v1", "command_digest": digest(command), "nodes": normalized}
    except (OSError, ValueError, KeyError, TypeError):
        return None


@contextlib.contextmanager
def observe(command, environment, root, cwd, source_roots=None):
    executable = shutil.which(
        command[0], path=environment.get("PATH", os.defpath) if environment is not None else None
    )
    supported = (
        len(command) >= 3
        and command[1:3] == ["-m", "pytest"]
        and executable is not None
        and Path(executable).resolve() == Path(sys.executable).resolve()
        and not any(
            Path(tempfile.gettempdir()).resolve().is_relative_to(Path(path).resolve())
            for path in (source_roots or [root])
        )
    )
    if not supported:
        yield environment, lambda code: None
        return
    # Outside all source bindings: imports/results never change executable source evidence.
    with tempfile.TemporaryDirectory(prefix="devroster-pytest-") as folder:
        directory = Path(folder)
        nonce = os.urandom(24).hex()
        module = "_devroster_observe_" + nonce
        destination = directory / "observations.json"
        plugin = directory / (module + ".py")
        plugin.write_text(
            "NONCE = " + repr(nonce) + "\nDESTINATION = " + repr(str(destination)) + "\n" + PLUGIN, encoding="utf-8"
        )
        plugin.chmod(0o600)
        env = dict(os.environ if environment is None else environment)
        env["PYTHONPATH"] = str(directory) + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        env["PYTEST_PLUGINS"] = ",".join(filter(None, [env.get("PYTEST_PLUGINS", ""), module]))

        def read(code):
            proof = _read(destination, nonce, code, Path(root).resolve(), command)
            if proof is not None:
                proof["cwd"] = Path(cwd).resolve().relative_to(Path(root).resolve()).as_posix()
            return proof

        yield env, read


def canonical_selector(target, cwd="."):
    """Normalize only the selector file path, using a server-approved gate cwd."""
    if not isinstance(target, str) or not isinstance(cwd, str):
        raise TypeError("Invalid test selector path.")
    filename, separator, suffix = target.partition("::")
    for value in (filename, cwd):
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or PureWindowsPath(value).drive
            or ".." in path.parts
            or "\\" in value
            or "\0" in value
        ):
            raise ValueError("Test selector escaped its module.")
    if PurePosixPath(filename) == PurePosixPath("."):
        raise ValueError("Test selector needs a file.")
    return (PurePosixPath(cwd) / filename).as_posix() + separator + suffix


def selector_passed(proof, target, command, *, cwd=None):
    """All collected instances in the selector scope must actually pass all phases."""
    if (
        not isinstance(proof, dict)
        or proof.get("runner") != "host-pytest-v1"
        or proof.get("command_digest") != digest(command)
    ):
        return False
    try:
        actual_target = canonical_selector(target, proof.get("cwd", "."))
        target = canonical_selector(target, cwd) if cwd is not None else actual_target
        if target != actual_target:
            return False
    except (TypeError, ValueError):
        return False
    nodes = proof.get("nodes", [])
    matching = [
        n
        for n in nodes
        if n["nodeid"] == target
        or n["nodeid"].startswith(target + "::")
        or ("[" not in target and n["nodeid"].startswith(target + "["))
    ]
    return bool(matching) and all(n["passed"] is True for n in matching)
