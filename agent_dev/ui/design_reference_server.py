from agent_dev.adapters.design_references import DesignReferences
from agent_dev.ui.local_web import LocalWeb


class DesignReferenceWeb(LocalWeb):
    prefix = "design-references"
    page = "references"

    def __init__(self, root):
        super().__init__(root)
        self.service = DesignReferences(root)

    def get(self, action, query):
        if action == "bootstrap":
            return {
                "references": self.service.list(),
                "connections": [
                    r
                    for r in self.service.integrations.list()
                    if r["provider"] in {"figma", "stitch"}
                ],
            }
        if action == "detail":
            return self.service.get(query["id"])
        return super().get(action, query)

    def post(self, action, payload):
        if action in {"inspect", "reference"}:
            return self.service.inspect(payload["connection_id"], payload["locator"])
        if action == "apply-profile":
            return self.service.apply_profile(payload["ids"], payload["expected_version"])
        if action == "attach":
            from agent_dev.adapters.studio_store import StudioStore

            return self.service.attach(
                StudioStore(self.root), payload["project_id"], payload["version"], payload["ids"]
            )
        return super().post(action, payload)
