"""Bounded source content search (SC-01): order, cursor, exclusions, parity."""

import json
import os
import stat
import subprocess
import sys

import pytest

from agent_dev.adapters.source_explorer import SourceExplorer
from agent_dev.adapters.source_search import SourceSearch, cli_key
from agent_dev.adapters.workspace import initialize
from agent_dev.domain.errors import DomainError, ErrorCode

SECRET = "supersecretvalue123"


@pytest.fixture
def root(tmp_path, monkeypatch):
    initialize(tmp_path)
    monkeypatch.setenv("DEVROSTER_TEST_API_TOKEN", SECRET)
    src = tmp_path / "src"
    (src / "b").mkdir(parents=True)
    (src / "a.py").write_text("def a():\n    return 1\n", encoding="utf-8")
    (src / "kor.md").write_text("한글 검색 테스트\nHello World\n", encoding="utf-8")
    (src / "b/z.txt").write_text("hello from b\n", encoding="utf-8")
    (src / "conf.py").write_text(f"token = '{SECRET}'  # hello\n", encoding="utf-8")
    (src / "binary.bin").write_bytes(b"hello\x00\xff")
    (src / "large.txt").write_bytes(b"hello " * 200_000)
    (tmp_path / "outside.txt").write_text("hello outside\n", encoding="utf-8")
    (src / "link.txt").symlink_to(tmp_path / "outside.txt")
    (src / ".env.local").write_text("hello private\n", encoding="utf-8")
    return tmp_path


def service(root, key=b"k" * 32):
    return SourceSearch(SourceExplorer(root), key)


def paths(page):
    return [(r["path"], r.get("line")) for r in page["results"]]


def test_content_korean_case_and_literal(root):
    search = service(root)
    assert paths(search.search("source", "한글", mode="content")) == [("src/kor.md", 1)]
    found = search.search("source", "HELLO", mode="content")
    assert paths(found) == [("src/b/z.txt", 1), ("src/conf.py", 1), ("src/kor.md", 2)]
    assert paths(search.search("source", "hello", mode="content", case=True)) == [("src/b/z.txt", 1), ("src/conf.py", 1)]
    assert paths(search.search("source", "a()", mode="content")) == [("src/a.py", 1)]
    row = found["results"][0]
    assert row["snippet"] == "hello from b" and len(row["digest"]) == 64


def test_filename_mode_and_path_filter(root):
    search = service(root)
    assert paths(search.search("source", "a.py")) == [("src/a.py", None)]
    assert paths(search.search("source", "hello", mode="content", path="src/b")) == [("src/b/z.txt", 1)]
    assert SourceExplorer(root).search("source", "a.py")["results"][0]["path"] == "src/a.py"


def test_exclusions_secret_and_skip_reasons(root):
    page = service(root).search("source", "hello", mode="content")
    text = json.dumps(page, ensure_ascii=False)
    for hidden in ("binary.bin", "large.txt", "link.txt", "outside", ".env", SECRET):
        assert hidden not in text
    assert page["skipped"] == {"binary": 1, "large": 1}
    assert service(root).search("source", "supersecret", mode="content")["results"] == []


def test_many_matches_in_one_file_resume_same_file_and_line(root):
    (root / "src/many.txt").write_text("".join(f"needle {i}\n" for i in range(1, 251)), encoding="utf-8")
    search = service(root)
    seen, cursor, pages = [], "", 0
    while True:
        page = search.search("source", "needle", mode="content", cursor=cursor)
        pages += 1
        seen += paths(page)
        cursor = page["next_cursor"]
        if not cursor:
            break
        assert page["truncated"] and page["reason"] == "results"
    assert pages == 3 and seen == [("src/many.txt", i) for i in range(1, 251)]


def test_line_budget_and_deadline_are_resumable(root):
    (root / "src/long.txt").write_text("x\n" * 2400 + "needle\n", encoding="utf-8")
    search = service(root)
    page = search.search("source", "needle", mode="content", path="src/long.txt")
    assert page["results"] == [] and page["reason"] == "lines" and page["next_cursor"]
    cursor, found = page["next_cursor"], []
    while cursor:
        page = search.search("source", "needle", mode="content", path="src/long.txt", cursor=cursor)
        found += paths(page)
        cursor = page["next_cursor"]
    assert found == [("src/long.txt", 2401)]
    late = search.search("source", "hello", mode="content", deadline=0)
    assert late["reason"] == "deadline" and late["next_cursor"]
    rest = search.search("source", "hello", mode="content", deadline=0, cursor=late["next_cursor"])
    assert rest["reason"] == "deadline"


def test_tampered_cross_query_stale_file_and_config_cursors(root):
    (root / "src/many.txt").write_text("".join(f"needle {i}\n" for i in range(150)), encoding="utf-8")
    search = service(root)
    cursor = search.search("source", "needle", mode="content")["next_cursor"]
    with pytest.raises(DomainError) as tampered:
        search.search("source", "needle", mode="content", cursor=cursor[:-2] + "00")
    assert tampered.value.code == ErrorCode.INVALID_INPUT
    with pytest.raises(DomainError):
        search.search("source", "other", mode="content", cursor=cursor)
    with pytest.raises(DomainError):
        service(root, b"z" * 32).search("source", "needle", mode="content", cursor=cursor)
    (root / "src/many.txt").write_text("changed\n", encoding="utf-8")
    with pytest.raises(DomainError) as stale:
        search.search("source", "needle", mode="content", cursor=cursor)
    assert stale.value.code == ErrorCode.STALE_VERSION
    (root / "src/many.txt").write_text("".join(f"needle {i}\n" for i in range(150)), encoding="utf-8")
    cursor = search.search("source", "needle", mode="content")["next_cursor"]
    from agent_dev.adapters.project_setup import ProjectSetup

    settings = ProjectSetup(root).read()
    settings["modules"][0]["allowed_paths"] = [*settings["modules"][0]["allowed_paths"], "docs"]
    ProjectSetup(root).save(settings)
    with pytest.raises(DomainError) as changed:
        search.search("source", "needle", mode="content", cursor=cursor)
    assert changed.value.code == ErrorCode.STALE_VERSION


def test_invalid_queries_are_rejected(root):
    for kwargs in ({"query": ""}, {"query": "x" * 121}, {"query": "a", "mode": "regex"}, {"query": "a", "path": "../x"}):
        with pytest.raises(DomainError):
            service(root).search("source", **kwargs)


def cli(root, *args):
    result = subprocess.run(
        [sys.executable, "-m", "agent_dev.fa", "source", *args, "--root", str(root), "--json"],
        text=True, encoding="utf-8", errors="replace", capture_output=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)["result"]


def test_cli_continuation_across_processes_matches_api(root):
    (root / "src/many.txt").write_text("".join(f"needle {i}\n" for i in range(1, 131)), encoding="utf-8")
    key_path = root / ".agent-dev/source-search.key"
    assert not key_path.exists()
    first = cli(root, "search", "needle", "--mode", "content")
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    second = cli(root, "search", "needle", "--mode", "content", "--cursor", first["next_cursor"])
    assert paths(first) + paths(second) == [("src/many.txt", i) for i in range(1, 131)]
    api = SourceSearch(SourceExplorer(root), cli_key(root, create=False))
    assert api.search("source", "needle", mode="content") == first


def test_web_get_does_not_create_signer_and_impact_candidates(root):
    from agent_dev.ui.source_server import SourceWeb

    web = SourceWeb(root)
    page = web.get("search", {"module": "source", "query": "hello", "mode": "content"})
    assert page["results"] and not (root / ".agent-dev/source-search.key").exists()
    (root / "src/test_a.py").write_text("from a import a\n", encoding="utf-8")
    explorer = SourceExplorer(root)
    graph = {
        "nodes": [
            {"id": "a", "name": "a", "source_file": "src/a.py", "line": 1},
            {"id": "z", "name": "z", "source_file": "src/b/z.txt"},
            {"id": "t", "name": "test_a", "source_file": "src/test_a.py"},
        ],
        "links": [
            {"source": "a", "target": "z", "evidence": "EXTRACTED", "directed": True},
            {"source": "t", "target": "a", "evidence": "INFERRED"},
        ],
    }
    explorer.publish_graph("source", graph, explorer.manifest("source"))
    impact = web.get("impact", {"module": "source", "path": "src/a.py"})
    related = {r["path"]: r for r in impact["related"]}
    assert related["src/b/z.txt"]["evidence"] == "EXTRACTED" and related["src/b/z.txt"]["direction"] == "outgoing"
    assert related["src/test_a.py"]["evidence"] == "INFERRED" and related["src/test_a.py"]["direction"] == "unknown"
    assert [t["path"] for t in impact["tests"]] == ["src/test_a.py"]
    assert impact["state"] == "ready" and "후보" in impact["note"]
    os.remove(root / "src/test_a.py")
    stale = web.get("impact", {"module": "source", "path": "src/a.py"})
    assert stale["state"] == "stale"
    web.close()


def bounded(callable_, seconds=10):
    """Run in a thread so a regression to an endless loop fails instead of hanging CI."""
    import threading

    box = {}

    def target():
        try:
            box["value"] = callable_()
        except BaseException as error:  # noqa: BLE001 - re-raised in the test thread
            box["error"] = error

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(seconds)
    assert not thread.is_alive(), "search did not finish (pagination loop?)"
    if "error" in box:
        raise box["error"]
    return box["value"]


def all_pages(search, *args, **kwargs):
    seen, cursor, pages = [], "", 0
    while True:
        page = search.search(*args, cursor=cursor, **kwargs)
        pages += 1
        seen += paths(page)
        cursor = page["next_cursor"]
        if not cursor:
            return seen, pages
        assert pages < 200


@pytest.mark.parametrize("count", [201, 450])
def test_large_folder_lists_every_page_once(root, count):
    folder = root / "src/wide"
    folder.mkdir()
    for index in range(count):
        (folder / f"f{index:04}.txt").write_text("needle\n" if index >= count - 3 else "x\n", encoding="utf-8")
    explorer = SourceExplorer(root)
    calls = []
    original = explorer.listing
    explorer.listing = lambda *a: calls.append(a[1]) or original(*a)
    search = SourceSearch(explorer, b"k" * 32)
    found = bounded(lambda: search.search("source", f"f{count - 1:04}", path="src/wide"))
    assert paths(found) == [(f"src/wide/f{count - 1:04}.txt", None)]
    assert calls == ["src/wide"], "each folder is scanned once per request"
    content, _ = bounded(lambda: all_pages(search, "source", "needle", mode="content", path="src/wide"))
    assert content == [(f"src/wide/f{i:04}.txt", 1) for i in range(count - 3, count)]
    names, pages = bounded(lambda: all_pages(search, "source", "src/wide/f", path="src/wide"))
    assert names == [(f"src/wide/f{i:04}.txt", None) for i in range(count)]
    assert pages == count // 100 + (1 if count % 100 else 0)


def test_empty_folders_under_deadline_still_progress(root):
    for index in range(120):
        (root / f"src/empty-{index:03}").mkdir()
    (root / "src/zz.txt").write_text("needle\n", encoding="utf-8")
    search = service(root)
    found, pages = bounded(lambda: all_pages(search, "source", "needle", mode="content", deadline=0))
    assert found == [("src/zz.txt", 1)]
    assert pages <= 200


PLATFORM_PROBE = r"""
import json, os, sys, threading, urllib.request, urllib.error
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path
for name in ("O_DIRECTORY", "O_NOFOLLOW"):
    if hasattr(os, name):
        delattr(os, name)
from agent_dev.ui.setup_server import make_server as setup_server
from agent_dev.ui.studio_server import make_server as studio_server
root = Path(sys.argv[1])
report = {}
for label, factory in (("setup", setup_server), ("studio", studio_server)):
    server = factory(root, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def get(path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=10) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()
    page = "/setup" if label == "setup" else "/home"
    report[label] = {"page": get(page)[0]}
    if label == "studio":
        status, body = get("/api/source/bootstrap")
        report["bootstrap"] = [status, json.loads(body)]
        status, body = get("/api/source/search?module=source&query=hello&mode=content")
        report["search"] = [status, json.loads(body)]
    server.shutdown()
    server.server_close()
report["signer"] = (root / ".agent-dev/source-search.key").exists()
print(json.dumps(report, ensure_ascii=False))
"""


def test_platform_without_safe_reads_keeps_web_servers_up(root):
    result = subprocess.run(
        [sys.executable, "-c", PLATFORM_PROBE, str(root)],
        text=True, encoding="utf-8", errors="replace", capture_output=True, check=False, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report["setup"]["page"] == 200 and report["studio"]["page"] == 200
    status, bootstrap = report["bootstrap"]
    assert status == 200 and bootstrap["available"] is False and "macOS" in bootstrap["unavailable_reason"]
    status, search = report["search"]
    assert status == 400 and "지원하지 않아" in search["error"]
    assert report["signer"] is False


def test_cli_search_reports_unsupported_platform(root, monkeypatch):
    from agent_dev.adapters import source_search

    monkeypatch.delattr(os, "O_DIRECTORY")
    assert source_search.cli_key(root, create=False) is None
    with pytest.raises(DomainError) as error:
        source_search.cli_key(root, create=True)
    assert error.value.code == ErrorCode.CAPABILITY_UNAVAILABLE
    with pytest.raises(DomainError) as read:
        SourceExplorer(root).tree("source")
    assert read.value.code == ErrorCode.CAPABILITY_UNAVAILABLE


def test_folder_near_the_scan_cap_pages_within_budget(root):
    import time

    folder = root / "src/huge"
    folder.mkdir()
    for index in range(9000):
        (folder / f"n{index:05}.txt").write_text("x\n", encoding="utf-8")
    (folder / "n09000.txt").write_text("needle\n", encoding="utf-8")
    explorer = SourceExplorer(root)
    scans = []
    original = explorer.listing
    explorer.listing = lambda *a: scans.append(a[1]) or original(*a)
    search = SourceSearch(explorer, b"k" * 32)
    started = time.monotonic()
    page = bounded(lambda: search.search("source", "huge/n0", path="src/huge"))
    assert time.monotonic() - started < 5
    assert len(page["results"]) == 100 and page["reason"] == "results"
    assert scans == ["src/huge"]
    content = bounded(lambda: search.search("source", "needle", mode="content", path="src/huge"))
    assert content["files_scanned"] == 50 and content["reason"] in {"files", "deadline"}


def test_nested_pinned_profile_keeps_outer_pin_and_threads_isolated(root):
    import threading

    explorer = SourceExplorer(root)
    reads = []
    original = explorer.setup.current_profile
    explorer.setup.current_profile = lambda: reads.append(1) or original()
    with explorer.pinned_profile() as outer:
        with explorer.pinned_profile() as inner:
            assert inner is outer
        assert explorer._local.profile is outer, "inner exit must not clear the outer pin"
        with pytest.raises(RuntimeError), explorer.pinned_profile():
            raise RuntimeError("inner failure")
        assert explorer._local.profile is outer
        seen = {}
        thread = threading.Thread(target=lambda: seen.update(pin=getattr(explorer._local, "profile", None)))
        thread.start()
        thread.join()
        assert seen["pin"] is None, "pins are per thread"
        # search() pins again on the same explorer: it must reuse the outer read.
        assert SourceSearch(explorer, b"k" * 32).search("source", "hello", mode="content")["results"]
        assert explorer._local.profile is outer
    assert explorer._local.profile is None
    assert len(reads) == 1, "nested pins reuse the outer profile read"


@pytest.fixture
def locked(root):
    blocked = root / "src/b/locked"
    blocked.mkdir()
    (blocked / "inside.txt").write_text("hello hidden\n", encoding="utf-8")
    (root / "src/b/z2.txt").write_text("hello after\n", encoding="utf-8")
    unreadable = root / "src/b/unreadable.txt"
    unreadable.write_text("hello unreadable\n", encoding="utf-8")
    blocked.chmod(0)
    unreadable.chmod(0)
    yield root
    blocked.chmod(0o700)
    unreadable.chmod(0o600)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores permission bits")
def test_unreadable_folder_and_file_are_skipped_and_siblings_continue(locked):
    search = service(locked)
    names = search.search("source", "src/b/")
    assert [p for p, _ in paths(names)] == ["src/b/unreadable.txt", "src/b/z.txt", "src/b/z2.txt"]
    assert "src/b/locked" not in json.dumps(names), "an unreadable folder is never listed as a file"
    assert names["skipped"] == {"unreadable_folder": 1}
    content = search.search("source", "hello", mode="content")
    assert ("src/b/z2.txt", 1) in paths(content) and ("src/b/z.txt", 1) in paths(content)
    assert content["skipped"]["unreadable_folder"] == 1 and content["skipped"]["unreadable"] == 1
    walked, _ = all_pages(search, "source", "hello", mode="content", deadline=0)
    assert sorted(set(walked)) == sorted(walked) == sorted(paths(content))
    with pytest.raises(DomainError):
        search.search("source", "hello", mode="content", path="src/b/locked")
    with pytest.raises(DomainError):
        search.search("source", "hello", mode="content", path="src/b/unreadable.txt")
    assert search.search("source", "hello", mode="content", path="src/b/z2.txt")["results"]
    with pytest.raises(DomainError):
        search.search("source", "hello", path="outside")


def test_special_and_racing_entries_are_skipped_not_fatal(root):
    (root / "src/b/sub").mkdir()
    os.mkfifo(root / "src/b/sub/pipe")
    (root / "src/b/sub/after.txt").write_text("hello sub\n", encoding="utf-8")
    (root / "src/b/odd").mkdir()
    (root / "src/b/odd/back\\slash.txt").write_text("hello odd\n", encoding="utf-8")
    (root / "src/b/odd/ok.txt").write_text("hello ok\n", encoding="utf-8")
    explorer = SourceExplorer(root)
    original = explorer.text

    def swapped(module, name):
        # Race: the file is replaced by a folder after it was listed.
        if name == "src/kor.md":
            (root / "src/kor.md").unlink()
            (root / "src/kor.md").mkdir()
        return original(module, name)

    explorer.text = swapped
    search = SourceSearch(explorer, b"k" * 32)
    page = bounded(lambda: search.search("source", "hello", mode="content"))
    found = paths(page)
    assert ("src/b/sub/after.txt", 1) in found and ("src/b/odd/ok.txt", 1) in found
    assert page["skipped"]["not_regular"] == 1, page["skipped"]  # swapped file; FIFOs are never listed
    names = paths(SourceSearch(SourceExplorer(root), b"k" * 32).search("source", "src/b/"))
    assert ("src/b/odd/ok.txt", None) in names
    assert all("slash" not in p and p != "src/b/sub/pipe" for p, _ in names)
    with pytest.raises(DomainError):
        SourceSearch(SourceExplorer(root), b"k" * 32).search("source", "hello", mode="content", path="src/b/sub/pipe")


def test_broken_configured_scope_still_fails_loudly(root, monkeypatch):
    explorer = SourceExplorer(root)
    original = explorer._module
    monkeypatch.setattr(explorer, "_module", lambda module: (original(module)[0], ["src", "../bad"]))
    with pytest.raises(DomainError):
        explorer.tree("source")
