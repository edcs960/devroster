"""Built-in code index: which files import which, for when no Graphify graph exists.

Python is parsed with ``ast``; JavaScript/TypeScript and Java imports are read with
patterns after comments and (for JS) strings other than module names are removed. Only
imports that resolve to a file inside the same module are kept, so the result answers
"what else might this change touch" and nothing more. Relations are candidates
(``INFERRED``), like Graphify's inferred edges: dynamic imports, reflection and
re-exports through packages are not followed.

Files are read through the source explorer (permitted scopes, safe descriptor reads,
redaction). The parse results are cached by content digest next to the source graph, so
a rebuild re-parses only changed files.
"""

import ast
import json
import re
import time
from pathlib import PurePosixPath

from agent_dev.adapters.project_setup import atomic_json
from agent_dev.domain.errors import DomainError

PYTHON = {".py"}
SCRIPT = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"}
JAVA = {".java"}
MAX_FILES = 5000
DEADLINE = 8.0
VERSION = 1

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"(?m)(^|[^:\\])//.*$")
_SCRIPT_IMPORT = re.compile(
    r"""(?:\bimport\s+(?:[\w*{}\s,$]+?\s+from\s+)?|\bexport\s+[\w*{}\s,$]*?\s+from\s+|\brequire\s*\(\s*|\bimport\s*\(\s*)(["'])([^"'\n]{1,300})\1"""
)
_JAVA_PACKAGE = re.compile(r"(?m)^\s*package\s+([\w.]+)\s*;")
_JAVA_IMPORT = re.compile(r"(?m)^\s*import\s+(?:static\s+)?([\w.]+?)(?:\.\*)?\s*;")


def _python(text):
    """(absolute module names, relative (level, module, names)) and top-level definitions."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return [], []
    found, defined = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(["abs", alias.name, []] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            found.append(["rel" if node.level else "abs", node.module or "", [a.name for a in node.names], node.level])
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.append(node.name)
    return found, defined


def _script(text):
    stripped = _LINE_COMMENT.sub(r"\1", _BLOCK_COMMENT.sub(" ", text))
    return [["spec", match.group(2), []] for match in _SCRIPT_IMPORT.finditer(stripped)
            if match.group(2).startswith(".")], []


def _java(text):
    stripped = _LINE_COMMENT.sub(r"\1", _BLOCK_COMMENT.sub(" ", text))
    package = _JAVA_PACKAGE.search(stripped)
    found = [["java", name, []] for name in _JAVA_IMPORT.findall(stripped)]
    return found, [package.group(1)] if package else []


def parse(path, text):
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in PYTHON:
        return _python(text)
    if suffix in SCRIPT:
        return _script(text)
    if suffix in JAVA:
        return _java(text)
    return None


class _Resolver:
    """Maps import names to indexed file paths of the same module."""

    def __init__(self, paths):
        self.paths = set(paths)
        self.python = {}
        self.java = {}
        for path in self.paths:
            pure = PurePosixPath(path)
            if pure.suffix == ".py":
                parts = list(pure.with_suffix("").parts)
                if parts[-1] == "__init__":
                    parts = parts[:-1]
                # Every suffix of the dotted name, so src/ layouts and nested roots resolve.
                for start in range(len(parts)):
                    self.python.setdefault(".".join(parts[start:]), []).append(path)
            elif pure.suffix == ".java":
                parts = list(pure.with_suffix("").parts)
                for start in range(len(parts)):
                    self.java.setdefault(".".join(parts[start:]), []).append(path)

    def _python_name(self, name):
        rows = self.python.get(name, [])
        # The shortest path is the most direct match (a.b over x/y/a/b when both exist).
        return sorted(rows, key=lambda p: (len(PurePosixPath(p).parts), p))[:1]

    def resolve(self, source, row):
        kind, name, names = row[0], row[1], row[2]
        # "from pkg import mod" points at the submodules; the package itself only when the
        # imported names are not modules of their own.
        if kind == "abs":
            submodules = [hit for n in names for hit in self._python_name(f"{name}.{n}") if name]
            return submodules or self._python_name(name)
        if kind == "rel":
            level = row[3] if len(row) > 3 else 1
            base = PurePosixPath(source).parent
            for _ in range(level - 1):
                base = base.parent
            prefix = list(base.parts) + (name.split(".") if name else [])

            def module_file(parts):
                for candidate in ("/".join(parts) + ".py", "/".join([*parts, "__init__.py"])):
                    if candidate in self.paths:
                        return [candidate]
                return []

            submodules = [hit for n in names for hit in module_file([*prefix, n])]
            return submodules or module_file(prefix)
        if kind == "spec":
            base = PurePosixPath(source).parent
            joined = _normalize(base / name)
            if joined is None:
                return []
            options = [joined] + [joined + ext for ext in sorted(SCRIPT)] + [
                joined + "/index" + ext for ext in sorted(SCRIPT)
            ]
            # TypeScript sources import "./x.js" for x.ts.
            if PurePosixPath(joined).suffix in SCRIPT:
                stem = joined[: -len(PurePosixPath(joined).suffix)]
                options += [stem + ext for ext in sorted(SCRIPT)]
            return [option for option in options if option in self.paths][:1]
        if kind == "java":
            return sorted(self.java.get(name, []))[:1]
        return []


def _normalize(path):
    parts = []
    for part in path.parts:
        if part == "..":
            if not parts:
                return None
            parts.pop()
        elif part != ".":
            parts.append(part)
    return "/".join(parts)


class CodeIndex:
    def __init__(self, explorer):
        self.explorer = explorer

    def _cache_path(self, module):
        return self.explorer.graph_path(module).with_name("code-index.json")

    def _files(self, module, deadline):
        pending, files = [""], []
        while pending:
            directory = pending.pop()
            rows, truncated, _ = self.explorer.listing(module, directory)
            for row in rows:
                if time.monotonic() > deadline or len(files) >= MAX_FILES:
                    return files, True
                if row["type"] == "directory":
                    pending.append(row["path"])
                elif PurePosixPath(row["path"]).suffix.lower() in PYTHON | SCRIPT | JAVA:
                    files.append(row["path"])
            if truncated:
                return files, True
        return files, False

    def build(self, module):
        """{"files": {path: {"imports": [...], "defines": [...]}}, "partial": bool}."""
        deadline = time.monotonic() + DEADLINE
        cache_path = self._cache_path(module)
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            parsed = cached.get("parsed", {}) if cached.get("version") == VERSION else {}
        except (OSError, ValueError, AttributeError):
            parsed = {}
        with self.explorer.pinned_profile():
            paths, partial = self._files(module, deadline)
            fresh = {}
            for path in paths:
                if time.monotonic() > deadline:
                    partial = True
                    break
                try:
                    text, stamp, _skip = self.explorer.text(module, path)
                except DomainError:
                    continue
                if text is None:
                    continue
                previous = parsed.get(path)
                if previous and previous.get("digest") == stamp:
                    fresh[path] = previous
                    continue
                result = parse(path, text)
                if result is not None:
                    fresh[path] = {"digest": stamp, "found": result[0], "defines": result[1]}
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(cache_path, {"version": VERSION, "parsed": fresh})
        except (OSError, DomainError):
            pass  # The cache only saves work; the answer below does not depend on it.
        resolver = _Resolver(fresh)
        files = {}
        for path, row in fresh.items():
            imports = sorted({hit for item in row["found"] for hit in resolver.resolve(path, item) if hit != path})
            files[path] = {"imports": imports, "defines": row["defines"]}
        return {"files": files, "partial": partial}


def related(index, path):
    """Files ``path`` imports (outgoing) and files that import it (incoming)."""
    rows = {}
    for target in index["files"].get(path, {}).get("imports", []):
        rows[target] = {"path": target, "evidence": "INFERRED", "direction": "outgoing", "relations": ["imports"]}
    for source, row in index["files"].items():
        if path in row["imports"]:
            entry = rows.setdefault(
                source, {"path": source, "evidence": "INFERRED", "direction": "incoming", "relations": ["imports"]}
            )
            if entry["direction"] != "incoming":
                entry["direction"] = "mixed"
    return rows
