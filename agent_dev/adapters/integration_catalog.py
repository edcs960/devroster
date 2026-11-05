"""Optional provider catalog; entries are instructions, never installation claims."""

from copy import deepcopy


def _provider(
    key, name, group, endpoint="", command=(), tools=(), auth="token", docs="", supported=False
):
    return {
        "id": key,
        "name": name,
        "group": group,
        "endpoint": endpoint,
        "transport": "http" if endpoint else "stdio",
        "command": list(command),
        "auth": auth,
        "documentation": docs,
        "supported_client_only": supported,
        "operations": {tool: {"tool": tool, "capability": group + ".read"} for tool in tools},
        "state": "catalog_only",
        "installed": False,
    }


_PROVIDERS = [
    _provider(
        "context7",
        "Context7",
        "docs",
        "https://mcp.context7.com/mcp",
        tools=("resolve-library-id", "query-docs"),
        docs="https://context7.com/docs/resources/all-clients",
    ),
    _provider(
        "github",
        "GitHub",
        "github",
        "https://api.githubcopilot.com/mcp/readonly",
        tools=(
            "get_me",
            "search_repositories",
            "list_pull_requests",
            "pull_request_read",
            "get_file_contents",
            "list_issues",
        ),
        docs="https://github.com/github/github-mcp-server",
    ),
    _provider(
        "stitch",
        "Stitch",
        "design",
        "https://stitch.googleapis.com/mcp",
        tools=("list_projects", "get_project", "list_screens", "get_screen"),
        docs="https://stitch.withgoogle.com/",
    ),
    _provider(
        "figma",
        "Figma",
        "design",
        "https://mcp.figma.com/mcp",
        tools=(
            "get_metadata",
            "get_design_context",
            "get_screenshot",
            "get_variable_defs",
            "get_code_connect_map",
        ),
        auth="oauth",
        supported=True,
        docs="https://developers.figma.com/docs/figma-mcp-server/remote-server-installation/",
    ),
    _provider(
        "playwright",
        "Playwright",
        "browser",
        command=("playwright-mcp",),
        tools=(
            "browser_snapshot",
            "browser_take_screenshot",
            "browser_console_messages",
            "browser_network_requests",
        ),
        auth="none",
        docs="https://github.com/microsoft/playwright-mcp",
    ),
    _provider(
        "chrome-devtools",
        "Chrome DevTools",
        "browser",
        command=("chrome-devtools-mcp",),
        tools=(
            "list_pages",
            "take_snapshot",
            "take_screenshot",
            "list_console_messages",
            "list_network_requests",
        ),
        auth="none",
        docs="https://github.com/ChromeDevTools/chrome-devtools-mcp",
    ),
    _provider(
        "notion",
        "Notion",
        "docs",
        "https://mcp.notion.com/mcp",
        tools=("notion-search", "notion-fetch"),
        auth="oauth",
        docs="https://developers.notion.com/guides/mcp/get-started-with-mcp",
    ),
    _provider(
        "linear",
        "Linear",
        "tasks",
        "https://mcp.linear.app/mcp/readonly",
        tools=("get_issue", "list_issues", "list_projects", "list_teams", "get_document"),
        auth="oauth",
        docs="https://linear.app/docs/mcp",
    ),
    _provider(
        "atlassian",
        "Jira / Confluence",
        "tasks",
        "https://mcp.atlassian.com/v2/mcp",
        tools=(
            "getAccessibleAtlassianResources",
            "getJiraIssue",
            "searchJiraIssuesUsingJql",
            "getConfluencePage",
            "searchConfluenceUsingCql",
        ),
        auth="oauth",
        docs="https://support.atlassian.com/atlassian-ai-gateway/",
    ),
    _provider(
        "dbhub",
        "DBHub",
        "database",
        command=("dbhub",),
        tools=("search_objects", "execute_sql"),
        auth="none",
        docs="https://github.com/bytebase/dbhub",
    ),
    _provider(
        "supabase",
        "Supabase",
        "database",
        "https://mcp.supabase.com/mcp?read_only=true",
        tools=("list_tables", "list_extensions", "get_logs", "search_docs"),
        auth="oauth",
        docs="https://supabase.com/docs/guides/ai-tools/mcp",
    ),
    _provider(
        "neon",
        "Neon",
        "database",
        "https://mcp.neon.tech/mcp?readonly=true",
        tools=("list_projects", "describe_project", "get_database_tables", "describe_table_schema"),
        auth="oauth",
        docs="https://neon.com/docs/ai/neon-mcp-server",
    ),
    _provider(
        "sentry",
        "Sentry",
        "observability",
        "https://mcp.sentry.dev/mcp",
        tools=(
            "find_organizations",
            "find_projects",
            "get_issue_details",
            "search_issues",
            "search_events",
        ),
        auth="oauth",
        docs="https://mcp.sentry.dev/",
    ),
    _provider(
        "snyk",
        "Snyk",
        "security",
        command=("snyk", "mcp", "-t", "stdio"),
        tools=("snyk_test", "snyk_code_test", "snyk_iac_test", "snyk_container_test"),
        auth="token",
        docs="https://snyk.io/articles/snyk-mcp-cheat-sheet/",
    ),
    _provider(
        "vercel",
        "Vercel",
        "deployment",
        "https://mcp.vercel.com",
        tools=(
            "list_projects",
            "get_project",
            "list_deployments",
            "get_deployment",
            "get_deployment_build_logs",
        ),
        auth="oauth",
        supported=True,
        docs="https://vercel.com/docs/agent-resources/vercel-mcp",
    ),
    _provider(
        "railway",
        "Railway",
        "deployment",
        "https://mcp.railway.com",
        tools=("list_projects", "list_services", "get_logs", "list_deployments"),
        auth="oauth",
        docs="https://docs.railway.com/ai/mcp-server",
    ),
    _provider(
        "firecrawl",
        "Firecrawl",
        "web",
        "https://mcp.firecrawl.dev/v2/mcp",
        tools=("firecrawl_scrape", "firecrawl_search", "firecrawl_map"),
        docs="https://docs.firecrawl.dev/mcp-server",
    ),
    _provider(
        "serena",
        "Serena",
        "code",
        command=("serena", "start-mcp-server"),
        tools=(
            "get_symbols_overview",
            "find_symbol",
            "find_referencing_symbols",
            "search_for_pattern",
            "read_file",
        ),
        auth="none",
        docs="https://github.com/oraios/serena",
    ),
    _provider(
        "context-mode",
        "Context Mode",
        "context",
        command=("context-mode",),
        tools=("ctx_index", "ctx_search", "ctx_stats"),
        auth="none",
        docs="https://github.com/mksglu/context-mode",
    ),
    _provider(
        "graphify",
        "Graphify",
        "graph",
        command=("graphify-mcp",),
        tools=("query_graph", "get_node", "get_neighbors", "shortest_path"),
        auth="none",
        docs="https://github.com/Graphify-Labs/graphify",
    ),
    # REA runs locally over stdio (`rea mcp`, npm `rea-agents`, Node 22.23.4+/24.22.1+/26.12.1+).
    # A tool step is one call with fixed arguments, so only tools that read one local path
    # are allowed (REA 6.8.0 contracts). Tools that need an opened engine session, a live
    # browser or an earlier result, and device, instrumentation and debugger tools are not.
    _provider(
        "rea",
        "REA (Reverse Engineer Anything)",
        "analysis",
        command=("rea", "mcp"),
        tools=(
            "analyze_javascript_application",
            "inspect_desktop_application",
            "inspect_native_exports",
            "inspect_pe_imports",
            "inspect_go_binary",
            "inspect_rust_binary",
        ),
        auth="none",
        docs="https://github.com/morluto/rea",
    ),
    _provider(
        "docker-gateway",
        "Docker MCP Gateway",
        "bridge",
        command=("docker", "mcp", "gateway", "run"),
        auth="none",
        docs="https://docs.docker.com/ai/mcp-catalog-and-toolkit/mcp-gateway/",
    ),
]


def catalog():
    return deepcopy(_PROVIDERS)


def provider(provider_id):
    from agent_dev.domain.errors import DomainError, ErrorCode

    for item in catalog():
        if item["id"] == provider_id:
            return item
    if provider_id == "custom":
        return _provider("custom", "사용자 MCP", "custom", auth="token")
    raise DomainError(ErrorCode.INVALID_INPUT, "알 수 없는 MCP 제공자입니다.")
