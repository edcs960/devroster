"""CLI facades reuse the same web services without editing Python configuration."""

import json
from pathlib import Path

from agent_dev.domain.errors import DomainError, ErrorCode


def run(args):
    namespace, action, root = args.namespace, args.action, args.root
    from agent_dev.adapters.integrations import Integrations
    from agent_dev.adapters.project_setup import ProjectSetup

    if namespace == "integration":
        app = Integrations(root)
        if action == "catalog":
            return {"catalog": app.catalog()}
        if action == "list":
            return {"connections": app.list()}
        if action == "show":
            return app.show(args.connection_id)
        if action == "doctor":
            return app.doctor()
        if action == "configure":
            return app.configure(read(args.source), args.expected_version)
        if action == "check":
            return app.check(
                args.connection_id,
                operation=args.operation,
                arguments=read(args.arguments) if args.arguments else {},
                scope=args.scope,
            )
        if action == "read":
            return app.call(
                args.connection_id,
                args.operation,
                read(args.arguments) if args.arguments else {},
                scope=args.scope,
            )
        if action == "disable":
            return app.disable(args.connection_id)
        if action == "export":
            return app.export()
        if action == "import":
            return app.import_document(read(args.source))
        if action == "prepare":
            from agent_dev.adapters.integration_catalog import provider

            return dict(provider(args.provider), automatic_install=False)
        if action == "auth":
            return {
                "operator_actions": [
                    "devroster studio --root " + str(root),
                    "http://127.0.0.1:8767/integrations → 해당 연결의 인증 선택",
                ],
                "message": "PKCE callback은 로컬 웹 서버에서 처리합니다. 등록 OAuth client ID가 필요합니다.",
            }
    if namespace == "workflow":
        from agent_dev.adapters.workflow_definitions import WorkflowDefinitions

        app = WorkflowDefinitions(root)
        project = args.project or ProjectSetup(root).current_profile()[0]
        if action == "presets":
            return {"presets": app.presets()}
        if action == "list":
            return {"workflows": app.list(project)}
        if action == "show":
            return app.show(project, args.workflow_id, args.version)
        if action == "save":
            return app.save(project, read(args.source), args.expected_version)
        if action == "validate":
            return app.validate(read(args.source), project)
        if action == "preview":
            return app.preview(read(args.source), project_id=project)
        if action == "apply":
            return app.apply(project, args.workflow_id, args.expected_version, args.version)
        if action == "export":
            return app.export(project, args.workflow_id, args.version)
        if action == "import":
            return app.import_document(
                project, read(args.source), read(args.remap) if args.remap else None
            )
        if action == "approve":
            return app.approve(
                args.task_id, args.node_id, args.actor, args.decision, args.expected_version
            )
        if action == "state":
            return app.execution_state(args.task_id)
    if namespace in {"figma", "stitch"}:
        from agent_dev.adapters.design_references import DesignReferences

        app = DesignReferences(root)
        connection = args.connection or namespace
        if app.integrations.show(connection)["provider"] != namespace:
            raise DomainError(ErrorCode.INVALID_INPUT, "선택한 제공자의 연결 ID가 필요합니다.")
        if action == "check":
            return app.integrations.check(
                connection,
                operation=args.operation,
                arguments=read(args.arguments) if args.arguments else {},
                scope=args.scope,
            )
        if action == "cache":
            return {"references": app.list()}
        if action in {"inspect", "reference"}:
            locator = (
                args.locator
                if namespace == "figma"
                else {"project_id": args.locator, "screen_id": args.screen}
            )
            return app.inspect(connection, locator)
        if action in {"projects", "screens"}:
            return app.integrations.call(
                connection,
                "list_projects" if action == "projects" else "list_screens",
                {} if action == "projects" else {"parent": "projects/" + args.scope},
                scope=args.scope,
            )
    if namespace == "source":
        from agent_dev.adapters.source_explorer import SourceExplorer

        app = SourceExplorer(root)
        if action == "modules":
            return app.modules()
        if action == "tree":
            return app.tree(args.module, args.path)
        if action == "file":
            return app.read(args.module, args.path)
        if action in {"search", "impact"}:
            from agent_dev.adapters.source_search import SourceSearch, cli_key

            if action == "impact":
                return SourceSearch(app).impact(args.module, args.path)
            # Explicit CLI search may create the private signer so a later process can resume.
            return SourceSearch(app, cli_key(root)).search(
                args.module,
                args.query,
                mode=args.mode,
                path=args.path,
                case=args.case,
                cursor=args.cursor,
            )
        if action == "graph":
            return app.graph(args.module)
        if action == "build":
            import time

            from agent_dev.adapters.source_jobs import SourceJobs

            jobs = SourceJobs.for_root(root)
            result = jobs.start(args.module)
            try:
                while result.get("state") in {"queued", "running"}:
                    time.sleep(0.1)
                    result = jobs.status(result["id"])
                return result
            except KeyboardInterrupt:
                if result.get("id"):
                    jobs.cancel(result["id"])
                raise
            finally:
                jobs.close()
    raise DomainError(ErrorCode.INVALID_INPUT, "지원하지 않는 기능입니다.")


def read(path):
    path = Path(path)
    if path.stat().st_size > 1_000_000:
        raise DomainError(ErrorCode.INVALID_INPUT, "JSON 파일은 1MB 이하로 줄여주세요.")
    return json.loads(path.read_text(encoding="utf-8"))
