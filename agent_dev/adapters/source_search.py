"""Bounded filename/content search over registered source (SC-01).

One service backs the web endpoint and the ``source search`` CLI. Files are
visited in relative-path order (sorted path parts) and lines in file order, so a
signed cursor (query digest, module binding revision, last path/line, file
digest) resumes exactly where a page stopped. Each page reads at most 50 files,
1000 lines and returns 100 results within a 2 second budget; files over 1MiB,
non UTF-8/binary, private, symlinked or out-of-scope paths are never read.
Registered secrets are redacted on the whole file before lines are split, and
no source text, index or cache is persisted.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import secrets
import stat
import time
from pathlib import PurePosixPath

from agent_dev.adapters import query_cursor
from agent_dev.adapters.source_explorer import UnreadableEntry
from agent_dev.domain.content import digest
from agent_dev.domain.errors import DomainError, ErrorCode

MAX_FILES = 50
MAX_LINES = 1000
MAX_RESULTS = 100
DEADLINE = 2.0
KEY_NAME = "source-search.key"
TEST_NAME = re.compile(r"(^|/)tests?/|(^|/)test_[^/]*$|_test\.[^/]+$|\.(test|spec)\.[^/]+$")
NOTE = "변경 영향과 테스트 경로는 후보입니다. 실제 실행 범위나 필수 테스트를 확정하지 않습니다."


class _Stop(Exception):
    """The page budget ran out while walking folders."""


def _invalid(message, code=ErrorCode.INVALID_INPUT):
    raise DomainError(code, message)


def cli_key(root, create=True):
    """Private 0600 signer for cross-process CLI cursors; GET callers pass create=False."""
    from agent_dev.adapters.project_setup import ProjectSetup
    from agent_dev.adapters.source_explorer import UNSUPPORTED, safe_reads_supported

    if not (
        safe_reads_supported()
        and hasattr(os, "getuid")
        and os.link in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
    ):
        # No safe private signer here: GET callers fall back to a process-local key.
        if create:
            _invalid(UNSUPPORTED, ErrorCode.CAPABILITY_UNAVAILABLE)
        return None
    base = ProjectSetup(root).managed
    try:
        directory = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        if create:
            _invalid("프로젝트 작업 공간을 먼저 준비해주세요.", ErrorCode.CAPABILITY_UNAVAILABLE)
        return None
    try:
        for _ in range(2):
            try:
                fd = os.open(KEY_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
            except FileNotFoundError:
                if not create:
                    return None
                temporary = f".{KEY_NAME}.{secrets.token_hex(8)}"
                fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                try:
                    os.write(fd, secrets.token_bytes(32))
                    os.fsync(fd)
                finally:
                    os.close(fd)
                # link() never replaces an existing key, so concurrent creators converge.
                with contextlib.suppress(FileExistsError):
                    os.link(temporary, KEY_NAME, src_dir_fd=directory, dst_dir_fd=directory)
                os.unlink(temporary, dir_fd=directory)
                continue
            try:
                info = os.fstat(fd)
                key = os.read(fd, 33)
            finally:
                os.close(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_uid != os.getuid()
                or len(key) != 32
            ):
                _invalid("검색 이어보기 키 파일이 안전하지 않습니다.", ErrorCode.POLICY_VIOLATION)
            return key
    except OSError:
        _invalid("검색 이어보기 키를 준비하지 못했습니다.", ErrorCode.CAPABILITY_UNAVAILABLE)
    finally:
        os.close(directory)
    return None


def _parts(path):
    return tuple(p for p in PurePosixPath(path).parts if p not in {"", "."})


def _snippet(line, index):
    if len(line) <= 300:
        return line
    start = max(0, index - 100)
    return ("…" if start else "") + line[start : start + 300] + "…"


class SourceSearch:
    def __init__(self, explorer, key=None):
        self.explorer = explorer
        self.key = key

    def _revision(self, module):
        _, version, profile = self.explorer._current()
        return digest({"version": version, "binding": (profile or {}).get("repositories", {}).get(module)})

    def _entries(self, module, directory, state):
        # One scan per folder per request: paging tree() would rescan the folder each page.
        state["check"]()
        rows, limited, _ = self.explorer.listing(module, directory)
        return sorted(rows, key=lambda row: row["name"]), limited

    def _walk(self, module, start, after, after_dir, state, top=True):
        """Yield ("file", path) and ("dir", path) once a subtree is finished, in path order.

        Entries at or before ``after`` are skipped; ancestors of ``after`` are descended.
        A folder that cannot be read during traversal is counted and skipped so its
        siblings continue; the explicit start path is a file only when the OS says it
        is not a directory, otherwise its error is raised.
        """
        try:
            entries, limited = self._entries(module, start, state)
        except UnreadableEntry as error:
            if not top:
                state["skipped"]["unreadable_folder"] = state["skipped"].get("unreadable_folder", 0) + 1
                return
            if start and error.errno == errno.ENOTDIR and self.explorer.permitted(module, start):
                if not after or _parts(start) >= after:
                    yield "file", start
                return
            raise
        state["directory_limit"] = state["directory_limit"] or limited
        for row in entries:
            parts = _parts(row["path"])
            ancestor = len(parts) < len(after) and after[: len(parts)] == parts
            if not ancestor and after and (parts < after or (parts == after and after_dir)):
                continue
            if row["type"] == "directory":
                yield from self._walk(
                    module, row["path"], after if ancestor else (), after_dir and ancestor, state, top=False
                )
                yield "dir", row["path"]
            else:
                yield "file", row["path"]

    def search(self, module, query, mode="filename", path="", case=False, cursor="", deadline=DEADLINE):
        with self.explorer.pinned_profile():
            return self._search(module, query, mode, path, case, cursor, deadline)

    def _search(self, module, query, mode, path, case, cursor, deadline):
        if not isinstance(query, str) or not 1 <= len(query) <= 120 or "\n" in query:
            _invalid("검색어는 1~120자로 입력해주세요.")
        if mode not in {"filename", "content"}:
            _invalid("검색 방식은 filename 또는 content입니다.")
        if not isinstance(path, str):
            _invalid("검색 경로를 확인해주세요.")
        path = path.strip("/")
        self.explorer._relative(path, dot=True)
        case = case is True
        scope = {"module": module, "mode": mode, "case": case, "path": path, "query": digest(query)}
        revision = self._revision(module)
        position = (
            query_cursor.decode(cursor, "source-search", scope, revision, self.key) if cursor else None
        )
        after = _parts(position["path"]) if position else ()
        after_dir = bool(position and position.get("dir"))
        partial = position if position and position.get("line") is not None else None
        needle = query if case else query.casefold()
        budget = time.monotonic() + max(0.0, float(deadline))
        results, skipped = [], {}
        files = lines = 0
        reason, stop_at = None, None
        last_done = {"path": position["path"], "line": None, "digest": None, "dir": after_dir} if position and partial is None else None
        progressed = False

        def check():
            # Listing a huge folder can take long; stop only after this page made progress.
            if progressed and time.monotonic() > budget:
                raise _Stop

        state = {"directory_limit": False, "check": check, "skipped": skipped}

        def encode(where):
            return query_cursor.encode("source-search", scope, where, revision, self.key)

        start_path = path if path not in {"", "."} else ""
        walker = self._walk(module, start_path, after, after_dir, state)
        try:
            for kind, name in walker:
                parts = _parts(name)
                if partial is not None and (kind != "file" or parts != after):
                    # Resuming mid-file: that file must come first, else it vanished or moved.
                    _invalid("검색 이후 소스가 변경되었습니다. 처음부터 다시 검색해주세요.", ErrorCode.STALE_VERSION)
                if kind == "dir":
                    progressed = True
                    last_done = {"path": name, "line": None, "digest": None, "dir": True}
                    check()
                    continue
                if partial is None and position and parts == after:
                    continue  # Completed on the previous page.
                check()
                if mode == "filename":
                    progressed = True
                    if needle in (name if case else name.casefold()):
                        results.append({"path": name, "name": name.rsplit("/", 1)[-1], "line": None})
                    last_done = {"path": name, "line": None, "digest": None}
                    if len(results) >= MAX_RESULTS:
                        reason = "results"
                        break
                    continue
                if files >= MAX_FILES:
                    reason = "files"
                    break
                files += 1
                progressed = True
                try:
                    text, stamp, problem = self.explorer.text(module, name)
                except UnreadableEntry as error:
                    if name == start_path:
                        raise  # The one file the user asked for: report it.
                    # Vanished/refused, or no longer a regular file (FIFO, swapped for a folder).
                    problem = "not_regular" if error.code == ErrorCode.POLICY_VIOLATION else "unreadable"
                    text, stamp = None, None
                start = 0
                if partial is not None and parts == after:
                    if stamp != partial.get("digest"):
                        _invalid("검색 이후 파일이 변경되었습니다. 처음부터 다시 검색해주세요.", ErrorCode.STALE_VERSION)
                    start = partial["line"]
                partial = None
                if problem:
                    skipped[problem] = skipped.get(problem, 0) + 1
                    last_done = {"path": name, "line": None, "digest": None}
                    continue
                assert text is not None
                rows = text.splitlines()
                for number in range(start + 1, len(rows) + 1):
                    if lines >= MAX_LINES:
                        reason = "lines"
                    elif number % 100 == 0 and time.monotonic() > budget:
                        reason = "deadline"
                    if reason:
                        stop_at = {"path": name, "line": number - 1, "digest": stamp}
                        break
                    lines += 1
                    line = rows[number - 1]
                    found = (line if case else line.casefold()).find(needle)
                    if found >= 0:
                        results.append({"path": name, "line": number, "snippet": _snippet(line, found), "digest": stamp})
                        if len(results) >= MAX_RESULTS:
                            reason = "results"
                            stop_at = {"path": name, "line": number, "digest": stamp}
                            break
                if reason:
                    break
                last_done = {"path": name, "line": None, "digest": None}
            else:
                if partial is not None:
                    _invalid("검색 이후 소스가 변경되었습니다. 처음부터 다시 검색해주세요.", ErrorCode.STALE_VERSION)
        except _Stop:
            reason = "deadline"
        finally:
            walker.close()
        if reason and stop_at is None:
            # Stopped between entries: at least one file or folder finished on this page.
            stop_at = last_done
        next_cursor = encode(stop_at) if reason else None
        return {
            "mode": mode,
            "results": results,
            "truncated": bool(reason) or state["directory_limit"],
            "reason": reason,
            "next_cursor": next_cursor,
            "skipped": skipped,
            "files_scanned": files,
            "directory_limit": state["directory_limit"],
        }

    def impact(self, module, path):
        """Related files and test candidates from the code graph plus naming rules."""
        if not isinstance(path, str) or not path:
            _invalid("파일 경로를 선택해주세요.")
        self.explorer._relative(path)
        if not self.explorer.permitted(module, path):
            _invalid("등록된 읽기 범위 밖입니다.", ErrorCode.POLICY_VIOLATION)
        overview = self.explorer.graph(module, query=path)
        related = {}
        if overview["state"] in {"ready", "stale"}:
            mine = [n["id"] for n in overview["nodes"] if (n.get("source") or {}).get("path") == path][:20]
            for node_id in mine:
                view = self.explorer.graph(
                    module, node=node_id, depth=1, fresh=overview["state"] == "ready"
                )
                labels = {n["id"]: n for n in view["nodes"]}
                for edge in view["edges"]:
                    if node_id not in (edge["source"], edge["target"]):
                        continue
                    other = labels.get(edge["target"] if edge["source"] == node_id else edge["source"])
                    target = (other or {}).get("source") or {}
                    if not target.get("path") or target["path"] == path:
                        continue
                    direction = (
                        "unknown" if not edge["directed"] else "outgoing" if edge["source"] == node_id else "incoming"
                    )
                    row = related.setdefault(
                        target["path"],
                        {"path": target["path"], "evidence": "INFERRED", "direction": direction, "relations": []},
                    )
                    if edge["evidence"] == "EXTRACTED":
                        row["evidence"] = "EXTRACTED"
                    if row["direction"] != direction:
                        row["direction"] = "mixed"
                    if edge["type"] not in row["relations"]:
                        row["relations"].append(edge["type"])
        tests = [
            {"path": row["path"], "evidence": row["evidence"], "reason": "관계 그래프"}
            for row in related.values()
            if TEST_NAME.search(row["path"])
        ]
        stem = PurePosixPath(path).stem
        named = re.compile(rf"^(test_)?{re.escape(stem)}(_test)?(\.(test|spec))?\.[^.]+$")
        if stem and not TEST_NAME.search(path):
            found = self.search(module, stem, mode="filename")
            for row in found["results"]:
                if (
                    row["path"] != path
                    and named.match(row["name"])
                    and TEST_NAME.search(row["path"])
                    and all(t["path"] != row["path"] for t in tests)
                ):
                    tests.append({"path": row["path"], "evidence": "INFERRED", "reason": "이름 규칙"})
        return {
            "path": path,
            "state": overview["state"],
            "message": overview.get("message", ""),
            "related": sorted(related.values(), key=lambda row: row["path"]),
            "tests": sorted(tests, key=lambda row: row["path"]),
            "note": NOTE,
        }
