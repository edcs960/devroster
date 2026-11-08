"""Shared chrome contract at the HTML, HTTP and portable export boundaries."""

import http.client
import io
import threading
from contextlib import contextmanager
from html.parser import HTMLParser
from importlib.resources import files
from zipfile import ZipFile

import pytest

ASSETS = files("agent_dev.ui").joinpath("assets")
SHARED = ("ui-tokens.css", "ui-base.css", "ui-components.css")
PAGES = (
    "studio",
    "setup",
    "agent",
    "dashboard",
    "workflows",
    "integrations",
    "references",
    "source",
    "wiki",
    "messengers",
)


class Stylesheets(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.paths = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.paths.append(attrs["href"])


@pytest.mark.parametrize("page", PAGES)
def test_product_html_loads_shared_styles_before_page_styles(page):
    paths = Stylesheets(ASSETS.joinpath(page + ".html").read_text(encoding="utf-8")).paths
    assert paths[:4] == ["/" + name for name in (*SHARED, page + ".css")]


def test_exact_allowlist_rejects_traversal_and_page_api_paths():
    from agent_dev.ui.shared_assets import SHARED_CSS, shared_asset_name

    assert SHARED_CSS == SHARED
    for name in SHARED:
        assert shared_asset_name("/" + name) == name
    for path in (
        "/studio.css",
        "/api/agent/ui-base.css",
        "/assets/ui-base.css",
        "/../ui-base.css",
        "/ui-base.css/",
        "/%75i-base.css",
        "/ui-base.css?x=1",
    ):
        assert shared_asset_name(path) is None




@contextmanager
def running_server(server):
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request(server, path, headers=None, method="GET"):
    connection = http.client.HTTPConnection(*server.server_address, timeout=5)
    connection.request(method, path, headers=headers or {})
    response = connection.getresponse()
    result = response.status, dict(response.getheaders()), response.read()
    connection.close()
    return result




def test_offline_mock_export_includes_every_referenced_asset(tmp_path):
    from test_studio import seed

    from agent_dev.adapters.studio_store import StudioStore
    from agent_dev.application.studio import Studio

    studio = Studio(StudioStore(tmp_path), ASSETS)
    data = seed(studio)
    pid = data["project"]["id"]
    for screen in data["mock"]["payload"]["screens"]:
        data = studio.approve_screen(pid, data["project"]["version"], 1, screen["id"])
    with ZipFile(io.BytesIO(studio.bundle(pid, True))) as zipped:
        html = zipped.read("mockup/index.html").decode()
        paths = Stylesheets(html).paths
        assert paths == [*SHARED, "studio.css", "mock.css"]
        for name in paths:
            assert zipped.read("mockup/" + name) == ASSETS.joinpath(name).read_bytes()


def test_studio_lazy_resources_initialize_once_and_unused_close_does_not_create(
    tmp_path, monkeypatch
):
    from concurrent.futures import ThreadPoolExecutor

    from agent_dev.ui.studio_server import StudioWeb

    created = {"studio": [], "jobs": []}
    generator = object()

    def store(root):
        return root

    def studio(store, assets):
        instance = object()
        created["studio"].append(instance)
        return instance

    class Jobs:
        def __init__(self, studio, supplied_generator, config_root):
            assert supplied_generator is generator
            assert studio is created["studio"][0]
            created["jobs"].append(self)

        def close(self):
            pass

    monkeypatch.setattr("agent_dev.ui.studio_server.StudioStore", store)
    monkeypatch.setattr("agent_dev.ui.studio_server.Studio", studio)
    monkeypatch.setattr("agent_dev.ui.studio_server.StudioJobs", Jobs)
    unused = StudioWeb(tmp_path / "unused", generator)
    unused.close()
    assert created == {"studio": [], "jobs": []}
    assert not (tmp_path / "unused").exists()
    web = StudioWeb(tmp_path / "used", generator)
    with ThreadPoolExecutor(max_workers=8) as pool:
        resources = list(pool.map(lambda _: (web.studio, web.jobs), range(24)))
    assert len(created["studio"]) == len(created["jobs"]) == 1
    assert all(pair == resources[0] for pair in resources)
    web.close()


@pytest.mark.parametrize("page", PAGES)
def test_product_pages_offer_the_shared_header_identity_and_action_slots(page):
    class HeaderSlots(HTMLParser):
        def __init__(self):
            super().__init__()
            self.headers = []
            self.action_slots = 0

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == "header":
                self.headers.append(attrs)
            if attrs.get("class") == "app-header-actions":
                self.action_slots += 1

    parsed = HeaderSlots()
    parsed.feed(ASSETS.joinpath(page + ".html").read_text(encoding="utf-8"))
    assert len(parsed.headers) == 1
    assert parsed.headers[0]["class"] == "app-header"
    assert parsed.headers[0]["data-page-title"]
    assert parsed.action_slots == 1


def test_navigation_identifies_canonical_and_supported_trailing_slash_routes():
    import shutil
    import subprocess
    from pathlib import Path

    if not shutil.which("node"):
        pytest.skip("Node required for navigation DOM interaction regression.")
    root = Path(__file__).parents[1]
    subprocess.run(
        [
            "node",
            str(Path(__file__).with_name("navigation_ui_regression.js")),
            str(root / "agent_dev/ui/assets/navigation.js"),
        ],
        check=True,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
    )


def test_installed_browser_drawer_styles_match_studio_on_every_product_page(tmp_path):
    import os
    import shutil
    import subprocess
    from pathlib import Path

    from test_reusable_workspace import workspace

    from agent_dev.ui.studio_server import make_server

    module = os.environ.get("AGENT_DEV_TEST_PLAYWRIGHT_MODULE")
    executable = os.environ.get("AGENT_DEV_TEST_BROWSER_EXECUTABLE")
    if not shutil.which("node") or not module or not executable:
        pytest.skip("Opt in with an installed Playwright module and browser executable; no auto install.")
    if not Path(module).exists() or not Path(executable).is_file():
        pytest.skip("Configured optional browser or Playwright module is unavailable.")
    root = workspace.__wrapped__(tmp_path)
    server = make_server(root, 0)
    try:
        with running_server(server):
            result = subprocess.run(
                [
                    "node",
                    str(Path(__file__).with_name("shared_styles_browser_regression.js")),
                    f"http://127.0.0.1:{server.server_address[1]}",
                    module,
                    executable,
                ],
                check=False,
                capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=90,
            )
            assert result.returncode == 0, result.stderr
    finally:
        server.studio.close()
