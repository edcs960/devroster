"""Local project-folder registry; never stores project data or credentials."""

import json
import sqlite3
from pathlib import Path

from agent_dev.adapters.project_setup import ProjectSetup
from agent_dev.adapters.workspace_locator import remembered_configuration
from agent_dev.core.cli_home import app_home
from agent_dev.core.io import atomic_json, locked
from agent_dev.domain.errors import DomainError

LIMIT = 100


def root_path(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 4096:
        raise ValueError("프로젝트 폴더 경로를 입력해주세요.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("최상위 폴더의 절대 경로를 입력해주세요.")
    return path.resolve()


def state_file():
    return app_home() / "app.json"


def read_state():
    try:
        value = json.loads(state_file().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise TypeError("앱의 프로젝트 목록 형식을 확인해주세요.")
    return value


def roots():
    value = read_state()
    entries = value.get("roots", [])
    if not isinstance(entries, list):
        raise TypeError("앱의 프로젝트 목록 형식을 확인해주세요.")
    result = []
    for entry in [value.get("root"), *entries]:
        if not isinstance(entry, str):
            continue
        path = str(root_path(entry))
        try:
            path = str(ProjectSetup(path).root)
        except (OSError, ValueError, DomainError):
            pass  # Keep damaged project entries visible with their own error state.
        if path not in result:
            result.append(path)
    return result[:LIMIT]


def register_root(root, *, remember=False):
    service = ProjectSetup(root_path(str(root)))
    root = str(service.root)
    if not Path(root).is_dir():
        raise ValueError("존재하는 프로젝트 폴더를 선택해주세요.")
    with locked(state_file()):
        value = read_state()
        entries = roots()
        if root not in entries and len(entries) >= LIMIT:
            raise ValueError("등록 가능한 프로젝트는 최대 100개입니다.")
        entries = [root, *(entry for entry in entries if entry != root)]
        configurations = value.setdefault("configurations", {})
        if not isinstance(configurations, dict):
            raise TypeError("앱의 프로젝트 설정 위치 목록 형식을 확인해주세요.")
        # A registration is not a configuration move. Older app.json files may
        # only have the last-used engine folder, so preserve that mapping too.
        previous = value.get("root")
        if isinstance(previous, str):
            try:
                previous_path = root_path(previous)
                previous_root = str(ProjectSetup(previous_path).root)
            except (OSError, ValueError, DomainError):
                pass  # A damaged remembered folder must not prevent registering another.
            else:
                configurations.setdefault(previous_root, str(previous_path))
        existing = configurations.get(root)
        try:
            existing_path = root_path(existing) if existing is not None else None
        except ValueError:
            existing_path = None
        if (
            existing_path is None
            or not existing_path.exists()
            or service.config == service.managed
        ):
            configurations[root] = str(service.config)
        value.update(version=1, roots=entries)
        if remember:
            value["root"] = root
        atomic_json(state_file(), value)


def configuration_for(root):
    def remembered():
        value = read_state()
        return value.get("configurations", {}), value.get("root")

    return remembered_configuration(root_path(str(root)), remembered, checked=root_path)


def project_list(current):
    current_service = ProjectSetup(current)
    current = str(current_service.root)
    paths = [current, *(path for path in roots() if path != current)]
    projects = []
    for path in paths:
        row = {
            "root": path,
            "name": Path(path).name,
            "current": path == current,
            "available": False,
            "configured": False,
        }
        try:
            if not Path(path).is_dir():
                raise ValueError("폴더가 없거나 이동되었습니다.")
            settings = (
                current_service if path == current else ProjectSetup(configuration_for(path))
            ).read()
            row.update(
                name=settings["name"],
                configuration_root=settings["configuration_root"],
                available=True,
                configured=settings["revision"] > 0,
            )
        except (OSError, ValueError, KeyError, RuntimeError, DomainError, sqlite3.Error) as exc:
            row["error"] = str(exc)
        projects.append(row)
    return {"active_root": current, "projects": projects}
