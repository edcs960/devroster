"""Read-only design bundles shared by Studio and workflow snapshots."""

import json
import time
from pathlib import Path

from agent_dev.adapters.integrations import Integrations
from agent_dev.adapters.project_setup import atomic_json
from agent_dev.domain.content import canonical, digest
from agent_dev.domain.design_reference import figma_locator, stitch_locator
from agent_dev.domain.errors import DomainError, ErrorCode


class DesignReferences:
    def __init__(self, root, *, integrations=None):
        self.integrations = integrations or Integrations(root)
        self.root = self.integrations.root
        from agent_dev.adapters.workspace import configuration_root
        from agent_dev.adapters.workspace_locator import workspace_locations

        cache = workspace_locations(configuration_root(Path(self.root))).cache_root
        self.directory = cache / "design-references"

    def _safe(self):
        if self.directory.is_symlink() or self.directory.parent.is_symlink():
            raise DomainError(
                ErrorCode.POLICY_VIOLATION,
                "화면 참고 자료 폴더에 심볼릭 링크를 사용할 수 없습니다.",
            )

    def list(self):
        self._safe()
        if not self.directory.exists():
            return []
        return [self.get(path.stem) for path in sorted(self.directory.glob("*.json"))[:100]]

    def get(self, key):
        self._safe()
        if (
            not isinstance(key, str)
            or len(key) != 64
            or any(c not in "0123456789abcdef" for c in key)
        ):
            raise DomainError(ErrorCode.INVALID_INPUT, "참고 자료 ID를 확인해주세요.")
        path = self.directory / (key + ".json")
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 100_000:
            raise DomainError(
                ErrorCode.CAPABILITY_UNAVAILABLE,
                "참고 자료 캐시가 없습니다. 연결을 다시 검사해주세요.",
            )
        row = json.loads(path.read_text(encoding="utf-8"))
        if digest({k: v for k, v in row.items() if k != "id"}) != key:
            raise DomainError(ErrorCode.POLICY_VIOLATION, "참고 자료의 내용이 변경되었습니다.")
        return row

    def inspect(self, connection_id, locator):
        row = self.integrations.show(connection_id)
        if row["provider"] == "figma":
            location = figma_locator(locator)
            if row["transport"] == "api":
                operation, arguments = next(iter(row["operations"])), {}
            else:
                operation, arguments = (
                    "get_metadata",
                    {"fileKey": location["file_key"], "nodeId": location["node_id"] or "0:1"},
                )
            bundle = self.integrations.call(
                connection_id, operation, arguments, scope=location["file_key"]
            )
        elif row["provider"] == "stitch":
            if not isinstance(locator, dict):
                raise DomainError(ErrorCode.INVALID_INPUT, "Stitch 프로젝트/화면을 선택해주세요.")
            location = stitch_locator(locator.get("project_id", ""), locator.get("screen_id", ""))
            name = "projects/" + location["project_id"]
            if location["screen_id"]:
                name += "/screens/" + location["screen_id"]
            bundle = self.integrations.call(
                connection_id,
                "get_screen" if location["screen_id"] else "get_project",
                {"name": name},
                scope=location["project_id"],
            )
        else:
            raise DomainError(ErrorCode.INVALID_INPUT, "Stitch 또는 Figma 연결을 선택해주세요.")
        result = {
            "connection_id": connection_id,
            "locator": location,
            "source_digest": bundle["digest"],
            "fetched_at": bundle["fetched_at"],
            "trust": "untrusted_data",
            "content": bundle["content"],
            "truncated": bundle["truncated"],
            "policy": "참고 자료. 요구사항과 기존 디자인 시스템을 우선하며 원본을 수정하지 않습니다.",
        }
        result["id"] = digest(result)
        self._safe()
        atomic_json(self.directory / (result["id"] + ".json"), result)
        return result

    def bundle(self, ids):
        if not isinstance(ids, list) or len(ids) > 20:
            raise DomainError(ErrorCode.INVALID_INPUT, "참고 자료는 20개 이하로 선택해주세요.")
        rows = [self.get(key) for key in ids]
        total = 0
        for row in rows:
            available = max(0, 20_000 - total)
            encoded = canonical(row["content"])
            row["content"] = encoded[:available]
            row["truncated"] = row["truncated"] or len(encoded) > available
            total += len(row["content"])
        return rows

    def attach(self, studio_store, project_id, expected_version, ids):
        rows = self.bundle(ids)
        with studio_store.transaction() as db:
            studio_store.project(db, project_id, expected_version)
            studio_store.bump(
                db, project_id, "design.references", {"references": rows, "updated_at": time.time()}
            )
        return studio_store.detail(project_id)

    def apply_profile(self, ids, expected_version):
        self.bundle(ids)
        from agent_dev.adapters.workflow_definitions import WorkflowDefinitions

        service = WorkflowDefinitions(self.integrations.store.config)
        profile = service._profile(self.integrations.project)
        profile["design_references"] = ids
        return {
            "profile_version": service.store.revise_profile(
                self.integrations.project, profile, expected_version
            )
        }

    @staticmethod
    def studio_bundle(store, project_id):
        import contextlib

        with contextlib.closing(store.connect()) as db:
            row = db.execute(
                "SELECT payload FROM events WHERE project_id=? AND kind='design.references' ORDER BY id DESC LIMIT 1",
                (project_id,),
            ).fetchone()
        return json.loads(row["payload"])["references"] if row else []
