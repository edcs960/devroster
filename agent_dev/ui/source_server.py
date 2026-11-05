from agent_dev.adapters.source_explorer import SourceExplorer
from agent_dev.adapters.source_jobs import SourceJobs
from agent_dev.adapters.source_search import SourceSearch, cli_key
from agent_dev.ui.local_web import LocalWeb


class SourceWeb(LocalWeb):
    prefix = page = "source"

    def __init__(self, root):
        super().__init__(root)
        self.service = SourceExplorer(root)
        self.jobs = SourceJobs.for_root(root)
        # Read-only: reuse an existing CLI signer, never create one from a GET.
        self.search = SourceSearch(self.service, cli_key(root, create=False))

    def get(self, action, query):
        module = query.get("module", "source")
        if action == "bootstrap":
            return self.service.modules()
        if action == "tree":
            return self.service.tree(module, query.get("path", ""), query.get("cursor", 0))
        if action == "file":
            return self.service.read(
                module, query["path"], query.get("line", 1), query.get("digest", "")
            )
        if action == "search":
            return self.search.search(
                module,
                query.get("query", ""),
                mode=query.get("mode", "filename"),
                path=query.get("path", ""),
                case=query.get("case") == "true",
                cursor=query.get("cursor", ""),
            )
        if action == "impact":
            return self.search.impact(module, query.get("path", ""))
        if action in {"graph", "status", "query", "node"}:
            return self.service.graph(
                module,
                query.get("node", ""),
                query.get("depth", 1),
                query.get("query", ""),
                query.get("inferred", "true") == "true",
            )
        if action == "job":
            return self.jobs.status(query["id"])
        return super().get(action, query)

    def post(self, action, payload):
        if action == "build":
            return self.jobs.start(payload["module"])
        if action == "cancel":
            return self.jobs.cancel(payload["id"])
        return super().post(action, payload)

    def close(self):
        self.jobs.close()
