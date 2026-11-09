"""Read registered source through descriptor-relative, non-symlink paths.

Windows has no descriptor-relative opens, so there every step of a path is checked to be
a plain folder (no symlink or junction), a file is opened and compared with the file that
was checked, and a listed folder is re-checked after its scan.
"""

import contextlib
import errno
import json
import os
import re
import stat
import threading
import time
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from agent_dev.adapters.project_setup import ProjectSetup, atomic_json
from agent_dev.adapters.secrets import registered_secrets
from agent_dev.domain.content import content_digest, digest, redact
from agent_dev.domain.errors import DomainError, ErrorCode

EXCLUDED = {
    ".git",
    ".agent-dev",
    ".venv",
    "venv",
    "node_modules",
    "build",
    "dist",
    "__pycache__",
    ".cache",
    "credentials",
    "secrets",
    ".ssh",
}


def forbidden(name):
    return (
        name in EXCLUDED
        or name.lower().startswith(".env")
        or name.lower().endswith((".pem", ".key", ".p12", ".pfx"))
        or name.lower() in {"id_rsa", "id_ed25519", "credentials.json", "service-account.json"}
    )


UNSUPPORTED = (
    "이 운영체제에서는 안전한 소스 읽기(폴더 기준 열기·심볼릭 링크 차단)를 지원하지 않아 "
    "소스 탐색·검색을 사용할 수 없습니다. macOS나 Linux에서 사용해주세요."
)


# Windows: verified path reads (see _open_verified_path). Tests switch this on elsewhere.
PATH_READS = os.name == "nt"
REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
NAME_SURROGATE = 0x20000000  # Reparse tags that redirect a name: symlinks, junctions, mounts.


def safe_reads_supported():
    """Descriptor-relative no-follow reads, or Windows verified path reads."""
    return descriptor_reads() or PATH_READS


def redirects(info):
    """A symlink or junction-like entry; OneDrive placeholders and similar tags are plain files."""
    if stat.S_ISLNK(info.st_mode):
        return True
    if not getattr(info, "st_file_attributes", 0) & REPARSE_POINT:
        return False
    tag = getattr(info, "st_reparse_tag", 0)
    return not tag or bool(tag & NAME_SURROGATE)


def _plain(path, directory):
    info = os.lstat(path)
    if redirects(info):
        raise UnreadableEntry(
            "심볼릭 링크나 정션은 따라가지 않습니다.", None, ErrorCode.POLICY_VIOLATION
        )
    if directory and not stat.S_ISDIR(info.st_mode):
        raise UnreadableEntry("폴더가 아닙니다.", errno.ENOTDIR)
    if not directory and not stat.S_ISREG(info.st_mode):
        raise UnreadableEntry(
            "일반 소스 파일/폴더만 조회할 수 있습니다.", None, ErrorCode.POLICY_VIOLATION
        )
    return info


def _entry_redirects(entry):
    if entry.is_symlink():
        return True
    if os.name != "nt":
        return False
    try:
        return redirects(entry.stat(follow_symlinks=False))  # Cached from the folder scan.
    except OSError:
        return True  # Vanished or unreadable: not listed.


def _same(info, other):
    return (info.st_dev, info.st_ino) == (other.st_dev, other.st_ino)


def descriptor_reads():
    """Descriptor-relative, no-follow reads (macOS, Linux)."""
    return (
        hasattr(os, "O_DIRECTORY")
        and hasattr(os, "O_NOFOLLOW")
        and os.open in os.supports_dir_fd
        and os.scandir in os.supports_fd
    )


class UnreadableEntry(DomainError):
    """A permitted entry the OS refused, that vanished, or that is not a regular file/folder.

    ``errno`` is kept for callers; ``code`` keeps each case's public error code.
    """

    def __init__(self, message, errno_value=None, code=ErrorCode.CAPABILITY_UNAVAILABLE):
        super().__init__(code, message)
        self.errno = errno_value


def invalid(message, code=ErrorCode.POLICY_VIOLATION) -> NoReturn:
    raise DomainError(code, message)


class SourceExplorer:
    def __init__(self, root):
        self.setup = ProjectSetup(root)
        self.root = self.setup.root
        self.secrets = registered_secrets(self.setup.config)
        self._local = threading.local()

    def _current(self):
        held = getattr(self._local, "profile", None)
        return held if held is not None else self.setup.current_profile()

    @contextlib.contextmanager
    def pinned_profile(self):
        """Read the profile once for a bounded multi-file operation on this thread.

        Nested pins reuse the outer profile and restore it on exit, even after errors.
        """
        previous = getattr(self._local, "profile", None)
        self._local.profile = previous if previous is not None else self.setup.current_profile()
        try:
            yield self._local.profile
        finally:
            self._local.profile = previous

    def modules(self):
        project, version, profile = self._current()
        modules = []
        for key, row in (profile or {}).get("repositories", {}).items():
            scopes = row.get("read_paths", row.get("allowed_paths", []))
            modules.append(
                {"id": key, "name": row.get("name", key), "scopes": scopes, "revision": version}
            )
        available = safe_reads_supported()
        return {
            "project_id": project,
            "profile_version": version,
            "modules": modules,
            "available": available,
            "unavailable_reason": "" if available else UNSUPPORTED,
        }

    def _module(self, module):
        _, _, profile = self._current()
        row = (profile or {}).get("repositories", {}).get(module)
        if not row:
            invalid("등록된 소스 모듈을 선택해주세요.", ErrorCode.INVALID_INPUT)
        relative = row.get("path", ".")
        if Path(relative).is_absolute():
            target = Path(relative)
            self._relative(str(target).lstrip("/"), dot=True)
        else:
            self._relative(relative, dot=True)
            target = self.root / relative
        if not target.is_dir():
            invalid("모듈 폴더가 없습니다.", ErrorCode.CAPABILITY_UNAVAILABLE)
        return relative, row.get("read_paths", row.get("allowed_paths", []))

    @staticmethod
    def _relative(value, dot=False):
        if (
            not isinstance(value, str)
            or len(value) > 1000
            or "\\" in value
            or "\x00" in value
            or value.startswith("/")
            or any(
                p in {".", ".."} or forbidden(p)
                for p in value.split("/")
                if not (dot and value in {"", "."})
            )
        ):
            invalid("허용된 소스 상대 경로가 필요합니다.")
        return PurePosixPath(value)

    def permitted(self, module, path, directory=False):
        self._relative(path, dot=True)
        _, scopes = self._module(module)
        if path in {"", "."} and directory:
            return True
        for scope in scopes:
            self._relative(scope, dot=True)
            if (
                scope == "."
                or path == scope
                or path.startswith(scope.rstrip("/") + "/")
                or directory
                and scope.startswith(path.rstrip("/") + "/")
            ):
                return True
        return False

    @contextlib.contextmanager
    def _open(self, module, path, directory=False) -> Iterator[tuple[Any, Any]]:
        # (descriptor, stat); on Windows a folder comes as its verified path.
        if not safe_reads_supported():
            invalid(UNSUPPORTED, ErrorCode.CAPABILITY_UNAVAILABLE)
        if not self.permitted(module, path, directory):
            invalid("등록된 읽기 범위 밖입니다.")
        relative, _ = self._module(module)
        if not descriptor_reads():
            with self._open_verified_path(relative, path, directory) as opened:
                yield opened
            return
        descriptors = []
        try:
            module_path = Path(relative)
            base = Path("/") if module_path.is_absolute() else self.root
            fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(fd)
            parts = [p for p in PurePosixPath(relative, path).parts if p not in {".", "/"}]
            for index, part in enumerate(parts):
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if index < len(parts) - 1 or directory:
                    flags |= os.O_DIRECTORY
                fd = os.open(part, flags, dir_fd=fd)
                descriptors.append(fd)
            info = os.fstat(fd)
            if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
                # FIFOs, sockets or a file swapped for a folder: entry-level, not a scope error.
                raise UnreadableEntry(
                    "일반 소스 파일/폴더만 조회할 수 있습니다.", None, ErrorCode.POLICY_VIOLATION
                )
            yield fd, info
        except OSError as error:
            raise UnreadableEntry("파일이 없거나 안전하게 읽을 수 없습니다.", error.errno) from None
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    @contextlib.contextmanager
    def _open_verified_path(self, relative, path, directory) -> Iterator[tuple[Any, Any]]:
        """Windows: every step a plain folder; the opened file is the file that was checked.

        A folder is yielded as its path (os.scandir cannot take a descriptor there) and the
        whole chain is checked again after the scan, so names read through a folder swapped
        for a junction meanwhile are discarded with an error instead of being returned.
        """
        if Path(relative).is_absolute():
            invalid("Windows에서는 프로젝트 폴더 안의 모듈만 읽을 수 있습니다.")
        parts = [p for p in PurePosixPath(relative, path).parts if p not in {".", "/"}]
        chain = [self.root, *(self.root.joinpath(*parts[: i + 1]) for i in range(len(parts)))]
        try:
            checked = [
                _plain(step, directory=index < len(chain) - 1 or directory)
                for index, step in enumerate(chain)
            ]
            if directory:
                yield str(chain[-1]), checked[-1]
                for step, before in zip(chain, checked, strict=True):
                    if not _same(_plain(step, directory=True), before):
                        raise UnreadableEntry("읽는 중 폴더가 바뀌었습니다. 다시 조회해주세요.")
                return
            fd = os.open(chain[-1], os.O_RDONLY | getattr(os, "O_BINARY", 0))
            try:
                opened = os.fstat(fd)
                if not stat.S_ISREG(opened.st_mode) or not _same(opened, checked[-1]):
                    raise UnreadableEntry(
                        "확인한 파일과 연 파일이 다릅니다. 다시 조회해주세요.",
                        None,
                        ErrorCode.POLICY_VIOLATION,
                    )
                yield fd, opened
            finally:
                os.close(fd)
        except OSError as error:
            raise UnreadableEntry("파일이 없거나 안전하게 읽을 수 없습니다.", error.errno) from None

    def listing(self, module, path=""):
        """One bounded scan (10000 entries) of a folder: permitted rows, truncated, version."""
        with self._open(module, path, True) as (fd, info):
            rows = []
            scanned = 0
            with os.scandir(fd) as entries:
                for entry in entries:
                    scanned += 1
                    if scanned > 10000:
                        break
                    name = entry.name
                    if forbidden(name) or _entry_redirects(entry):
                        continue
                    child = (path.rstrip("/") + "/" + name).lstrip("/")
                    directory = entry.is_dir(follow_symlinks=False)
                    if not directory and not entry.is_file(follow_symlinks=False):
                        continue  # FIFOs, sockets and devices are never source files.
                    try:
                        self._relative(child)
                    except DomainError:
                        continue  # Names that cannot form a safe relative path are never listed.
                    # A broken configured scope still raises here instead of hiding entries.
                    if self.permitted(module, child, directory):
                        rows.append(
                            {
                                "name": name,
                                "path": child,
                                "type": "directory" if directory else "file",
                            }
                        )
            return rows, scanned > 10000, digest([info.st_dev, info.st_ino, info.st_mtime_ns])

    def tree(self, module, path="", cursor=0):
        cursor = int(cursor)
        if not 0 <= cursor <= 10000:
            invalid("목록 위치를 확인해주세요.", ErrorCode.INVALID_INPUT)
        rows, truncated, version = self.listing(module, path)
        rows.sort(key=lambda row: (row["type"] != "directory", row["name"].casefold()))
        return {
            "path": path,
            "entries": rows[cursor : cursor + 200],
            "next_cursor": cursor + 200 if len(rows) > cursor + 200 else None,
            "truncated": truncated,
            "source_version": version,
        }

    def read(self, module, path, start_line=1, expected_digest=""):
        start_line = int(start_line)
        if not 1 <= start_line <= 1_000_000:
            invalid("줄 번호를 확인해주세요.", ErrorCode.INVALID_INPUT)
        with self._open(module, path) as (fd, info):
            result = {
                "path": path,
                "size": info.st_size,
                "start_line": start_line,
                "lines": [],
                "truncated": False,
                "redacted": False,
            }
            if info.st_size > 1_048_576:
                return dict(result, reason="1MiB를 넘는 파일은 미리보기를 제공하지 않습니다.")
            raw = os.read(fd, 1_048_577)
            after = os.fstat(fd)
            if info.st_mtime_ns != after.st_mtime_ns or info.st_size != after.st_size:
                invalid(
                    "읽는 중 파일이 변경되었습니다. 다시 조회해주세요.", ErrorCode.STALE_VERSION
                )
            stamp = content_digest(raw)
            if expected_digest and stamp != expected_digest:
                invalid("원문이 변경되었습니다. 그래프를 갱신해주세요.", ErrorCode.STALE_VERSION)
            try:
                content = raw.decode("utf-8")
            except UnicodeError:
                return dict(result, reason="UTF-8 텍스트 파일만 미리볼 수 있습니다.")
            if "\x00" in content:
                return dict(result, reason="바이너리 파일은 미리볼 수 없습니다.")
            safe = redact(content, self.secrets)
            lines = safe.splitlines()
            selected = lines[start_line - 1 : start_line + 499]
            size = 0
            bounded = []
            for line in selected:
                size += len(line.encode())
                if size > 262144:
                    break
                bounded.append(line)
            return dict(
                result,
                lines=bounded,
                total_lines=len(lines),
                content_digest=stamp,
                source_version=stamp,
                redacted=safe != content,
                truncated=start_line - 1 + len(bounded) < len(lines),
            )

    def text(self, module, path):
        """(redacted text, raw digest, skip reason) for bounded search; never cached."""
        with self._open(module, path) as (fd, info):
            if info.st_size > 1_048_576:
                return None, None, "large"
            raw = os.read(fd, 1_048_577)
            after = os.fstat(fd)
            if info.st_mtime_ns != after.st_mtime_ns or info.st_size != after.st_size:
                return None, None, "changed"
        try:
            content = raw.decode("utf-8")
        except UnicodeError:
            return None, None, "binary"
        if "\x00" in content:
            return None, None, "binary"
        # Redact the whole file first so a multi-line secret is never split across lines.
        return redact(content, self.secrets), content_digest(raw), None

    def search(self, module, query):
        from agent_dev.adapters.source_search import SourceSearch

        return SourceSearch(self).search(module, query)

    def manifest(self, module):
        pending = [""]
        rows = []
        deadline = time.monotonic() + 10
        while pending:
            directory = pending.pop()
            cursor = 0
            while True:
                page = self.tree(module, directory, cursor)
                if page["truncated"]:
                    invalid("소스 항목 수가 빌드 한도를 넘습니다. 읽기 범위를 줄여주세요.")
                for row in page["entries"]:
                    if len(rows) > 10000 or time.monotonic() > deadline:
                        invalid("소스 조회 예산을 넘었습니다. 읽기 범위를 줄여주세요.")
                    if row["type"] == "directory":
                        pending.append(row["path"])
                    else:
                        item = self.read(module, row["path"])
                        if "content_digest" in item:
                            rows.append({"path": row["path"], "digest": item["content_digest"]})
                if page["next_cursor"] is None:
                    break
                cursor = page["next_cursor"]
        return sorted(rows, key=lambda row: row["path"])

    def graph_path(self, module):
        self._module(module)
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", module):
            invalid("모듈 ID를 확인해주세요.")
        path = self.setup.locations.cache_root / "source-graphs" / module / "current.json"
        if any(
            p.is_symlink()
            for p in (path, path.parent, path.parent.parent, path.parent.parent.parent)
        ):
            invalid("그래프 경로에 심볼릭 링크를 사용할 수 없습니다.")
        return path

    def graph(self, module, node="", depth=1, query="", include_inferred=True, fresh=None):
        path = self.graph_path(module)
        if not path.is_file():
            return {
                "state": "not_prepared",
                "nodes": [],
                "edges": [],
                "message": "코드 관계 그래프를 아직 만들지 않았습니다.",
            }
        if path.stat().st_size > 20_000_000:
            invalid("그래프 크기가 한도를 넘습니다.")
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            data = record["graph"]
            manifest = record["manifest"]
            if digest(data) != record["graph_digest"]:
                raise ValueError("digest")
            normalized = self.normalize_graph(module, data)
        except (ValueError, KeyError, TypeError):
            return {
                "state": "error",
                "nodes": [],
                "edges": [],
                "message": "그래프가 손상되었습니다. 다시 준비해주세요.",
            }
        if fresh is None:  # Callers making several reads in one request pass it once.
            fresh = digest(self.manifest(module)) == digest(manifest)
        depth = int(depth)
        if not 0 <= depth <= 3 or len(query) > 120:
            invalid("그래프 탐색 범위를 확인해주세요.")
        selected = {
            row["id"]
            for row in normalized["nodes"]
            if (not node or row["id"] == node)
            and (not query or query.casefold() in row["label"].casefold()
                 or query.casefold() in (row.get("source") or {}).get("path", "").casefold())
        }
        if not node:
            selected = set(sorted(selected)[:200])
        edges = [e for e in normalized["edges"] if include_inferred or e["evidence"] == "EXTRACTED"]
        for _ in range(depth if node else 0):
            selected.update(
                endpoint
                for edge in edges
                if edge["source"] in selected or edge["target"] in selected
                for endpoint in (edge["source"], edge["target"])
            )
            if len(selected) > 200:
                break
        ordered = sorted(selected)
        selected = set(ordered[:200])
        result_nodes = [
            dict(row, source_stale=not fresh)
            for row in normalized["nodes"]
            if row["id"] in selected
        ]
        result_edges = [e for e in edges if e["source"] in selected and e["target"] in selected]
        return {
            "state": "ready" if fresh else "stale",
            "nodes": result_nodes,
            "edges": result_edges[:500],
            "truncated": len(ordered) > 200 or len(result_edges) > 500,
            "graph_digest": record["graph_digest"],
            "source_manifest_digest": digest(manifest),
            "generated_at": record["generated_at"],
            "generation": record["generation"],
            "message": "원문 기반 코드 관계 (실제 실행/영향 범위의 완전성을 보장하지 않습니다.)"
            if fresh
            else "소스가 변경되어 그래프 갱신이 필요합니다.",
        }

    def normalize_graph(self, module, data):
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("nodes"), list)
            or len(data["nodes"]) > 10000
        ):
            raise ValueError("graph nodes")
        nodes = []
        ids = set()
        source_cache = {}
        deadline = time.monotonic() + 10
        for item in data["nodes"]:
            if time.monotonic() > deadline:
                raise DomainError(ErrorCode.EXTERNAL_TRANSIENT, "그래프 탐색 시간 한도를 초과했습니다. 범위를 줄여주세요.")
            if not isinstance(item, dict):
                raise TypeError("node")
            key = str(item.get("id", ""))
            if not key or len(key) > 500 or key in ids:
                raise ValueError("node ID")
            ids.add(key)
            source = item.get("source_file", item.get("file", item.get("path", "")))
            source_ref = None
            try:
                if source and self.permitted(module, source):
                    if source not in source_cache:
                        source_cache[source] = self.read(module, source)
                    file = source_cache[source]
                    if "content_digest" in file:
                        source_ref = {
                            "path": source,
                            "line": max(
                                1, min(int(item.get("line", item.get("line_start", 1))), 1_000_000)
                            ),
                            "digest": file["content_digest"],
                        }
            except (DomainError, ValueError, TypeError):
                pass
            nodes.append(
                {
                    "id": key,
                    "label": str(item.get("label", item.get("name", key)))[:500],
                    "type": str(item.get("type", "symbol"))[:80],
                    "source": source_ref,
                }
            )
        raw_edges = data.get("links", data.get("edges", []))
        if not isinstance(raw_edges, list) or len(raw_edges) > 50000:
            raise ValueError("edges")
        edges = []
        for item in raw_edges:
            if not isinstance(item, dict):
                raise TypeError("edge")
            source, target = str(item.get("source", "")), str(item.get("target", ""))
            if source not in ids or target not in ids:
                raise ValueError("edge reference")
            evidence = str(item.get("evidence", item.get("confidence", "EXTRACTED"))).upper()
            edges.append(
                {
                    "source": source,
                    "target": target,
                    "type": str(item.get("type", item.get("relation", "related")))[:100],
                    "evidence": "EXTRACTED" if evidence == "EXTRACTED" else "INFERRED",
                    "directed": item.get("directed", data.get("directed", False)) is True,
                }
            )
        return {"nodes": nodes, "edges": edges}

    def publish_graph(self, module, data, manifest, tool_version="fixture"):
        self.normalize_graph(module, data)
        path = self.graph_path(module)
        previous = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        record = {
            "graph": data,
            "graph_digest": digest(data),
            "manifest": manifest,
            "generated_at": time.time(),
            "generation": previous.get("generation", 0) + 1,
            "tool_version": tool_version,
        }
        atomic_json(path, record)
        return {
            "state": "ready",
            "generation": record["generation"],
            "graph_digest": record["graph_digest"],
        }

    def build(self, module):
        from agent_dev.adapters.source_jobs import SourceJobs

        return SourceJobs.for_root(self.setup.config).start(module)
