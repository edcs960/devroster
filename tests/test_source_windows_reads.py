"""Windows source reads by verified path: no links or junctions, the opened file is the checked one.

The descriptor-free mode runs here on every OS (forced), so its checks are tested where
symlinks stand in for Windows junctions; native Windows runs it for real.
"""

import os
import stat
from types import SimpleNamespace

import pytest

from agent_dev.adapters import source_explorer, source_search
from agent_dev.adapters.source_explorer import SourceExplorer, redirects
from agent_dev.adapters.workspace import initialize
from agent_dev.domain.errors import DomainError, ErrorCode


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(source_explorer, "descriptor_reads", lambda: False)
    monkeypatch.setattr(source_explorer, "PATH_READS", True)
    initialize(tmp_path)
    src = tmp_path / "src"
    (src / "pkg").mkdir(parents=True)
    (src / "pkg" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    (outside / "secret.txt").write_text("outside\n", encoding="utf-8")
    return tmp_path, outside


def test_plain_folders_and_files_are_read(root):
    project, _ = root
    explorer = SourceExplorer(project)
    assert explorer.modules()["available"] is True
    names = [row["name"] for row in explorer.tree("source", "src/pkg")["entries"]]
    assert names == ["app.py"]
    assert explorer.read("source", "src/pkg/app.py")["lines"] == ["print('hello')"]


def test_linked_folder_on_the_way_is_refused(root):
    project, outside = root
    try:
        (project / "src" / "linked").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot create links")
    explorer = SourceExplorer(project)
    assert "linked" not in [row["name"] for row in explorer.tree("source", "src")["entries"]]
    for call in (lambda: explorer.tree("source", "src/linked"), lambda: explorer.read("source", "src/linked/secret.txt")):
        with pytest.raises(DomainError) as error:
            call()
        assert error.value.code == ErrorCode.POLICY_VIOLATION


def test_linked_file_is_refused(root):
    project, outside = root
    try:
        (project / "src" / "pkg" / "peek.txt").symlink_to(outside / "secret.txt")
    except OSError:
        pytest.skip("this account cannot create links")
    with pytest.raises(DomainError) as error:
        SourceExplorer(project).read("source", "src/pkg/peek.txt")
    assert error.value.code == ErrorCode.POLICY_VIOLATION


def test_file_swapped_between_check_and_open_is_refused(root, monkeypatch):
    project, _ = root
    real_fstat = os.fstat

    def other_file(fd):
        info = real_fstat(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino + 1,
                               st_size=info.st_size, st_mtime_ns=info.st_mtime_ns)

    monkeypatch.setattr(source_explorer.os, "fstat", other_file)
    with pytest.raises(DomainError) as error:
        SourceExplorer(project).read("source", "src/pkg/app.py")
    assert error.value.code == ErrorCode.POLICY_VIOLATION


def test_folder_swapped_during_a_scan_discards_the_listing(root, monkeypatch):
    project, outside = root
    folder = project / "src" / "pkg"
    real_scandir = os.scandir

    def swap_then_scan(path):
        # The folder is replaced by a link to somewhere else after it was checked.
        folder.rename(project / "src" / "pkg-old")
        try:
            folder.symlink_to(outside, target_is_directory=True)
        except OSError:
            pytest.skip("this account cannot create links")
        return real_scandir(path)

    monkeypatch.setattr(source_explorer.os, "scandir", swap_then_scan)
    with pytest.raises(DomainError):
        SourceExplorer(project).tree("source", "src/pkg")


def _info(mode=stat.S_IFREG, attributes=0, tag=0):
    return SimpleNamespace(st_mode=mode, st_file_attributes=attributes, st_reparse_tag=tag)


def test_only_name_redirecting_reparse_points_are_links():
    reparse = 0x400
    assert redirects(_info(attributes=reparse, tag=0xA0000003))  # junction (mount point)
    assert redirects(_info(attributes=reparse, tag=0xA000000C))  # symlink
    assert redirects(_info(attributes=reparse))  # unknown tag: refuse
    assert not redirects(_info(attributes=reparse, tag=0x9000601A))  # OneDrive placeholder
    assert not redirects(_info())
    assert redirects(_info(mode=stat.S_IFLNK))


def test_cli_search_key_is_kept_as_a_plain_file(root):
    project, _ = root
    first = source_search.cli_key(project)
    assert len(first) == 32 and source_search.cli_key(project, create=False) == first
