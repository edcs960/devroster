"""Behavioral tests for DevRoster product identity (Task 2c)."""

import http.client
import json as _json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from contextlib import contextmanager
from html.parser import HTMLParser
from importlib.resources import files
from pathlib import Path

import pytest

ASSETS = files("agent_dev.ui").joinpath("assets")
PAGES = ("studio", "setup", "agent", "dashboard", "workflows", "integrations", "references", "source")

PRODUCT_NAME = "DevRoster"
PRODUCT_TAGLINE = "나만의 AI 개발팀, 하나의 작업대."


class HeadParser(HTMLParser):
    """Collects <title>, <link>, and <script> tags from the <head>."""

    def __init__(self, html):
        super().__init__()
        self.title = ""
        self.links = []
        self.scripts = []
        self._in_title = False
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "title":
            self._in_title = True
        elif tag == "link":
            self.links.append(d)
        elif tag == "script":
            self.scripts.append(d)

    def handle_data(self, data):
        if self._in_title:
            self.title += data

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False


def _nav_harness(
    branding_src: str | None,
    nav_src: str,
    *,
    page_title: str = "테스트페이지",
    has_header: bool = True,
    brand_slots: int = 0,
) -> dict:
    """Compact Node VM harness with real header + data-brand-name support.
    Returns dict: {title, menuHeading, brandSlots, crashed}.
    """
    if not shutil.which("node"):
        pytest.skip("Node required.")
    extra_slots = "\n".join(
        f"const s{i}=new El('span'); s{i}.attributes['data-brand-name']=''; body.children.push(s{i});"
        for i in range(brand_slots)
    )
    header_setup = (
        f"const hdr=new El('header'); hdr.attributes['class']='app-header';"
        f" hdr.dataset={{pageTitle:'{page_title}'}}; body.children.push(hdr);"
        if has_header
        else ""
    )
    brand_run = f"vm.runInNewContext({_json.dumps(branding_src)},ctx);" if branding_src else ""
    nav_run = f"vm.runInNewContext({_json.dumps(nav_src)},ctx);"
    harness = f"""
'use strict';
const vm=require('vm');
class El{{
  constructor(t){{this.tag=t;this.children=[];this.attributes={{}};this.classList={{add(){{}} ,remove(){{}} }};this.dataset={{}};this._txt='';}}
  append(...n){{this.children.push(...n);}}
  prepend(...n){{this.children.unshift(...n);}}
  setAttribute(k,v){{this.attributes[k]=v;}}
  addEventListener(){{}}
  getBoundingClientRect(){{return {{height:64}};}}
  focus(){{}}
  get textContent(){{return this._txt;}}
  set textContent(v){{this._txt=v;}}
}}
const body=new El('body');
{header_setup}
{extra_slots}
let _title='';
const doc={{
  body,
  documentElement:{{style:{{setProperty(){{}}}}}},
  getElementById(){{return null;}},
  querySelector(sel){{
    if(sel==='body > .app-header') return body.children.find(c=>c.attributes['class']==='app-header')||null;
    return null;
  }},
  querySelectorAll(sel){{
    if(sel==='[data-brand-name]'){{
      const out=[];
      (function coll(n){{if('data-brand-name' in n.attributes) out.push(n); n.children.forEach(coll);}})(body);
      return {{forEach(fn){{out.forEach(fn);}}}};
    }}
    return {{forEach(){{}} }};
  }},
  createElement(t){{return new El(t);}},
  get title(){{return _title;}},
  set title(v){{_title=v;}}
}};
const ctx={{location:{{pathname:'/studio'}},document:doc,window:{{addEventListener(){{}}}}}};
{brand_run}
{nav_run}
const brandSlots=[];
(function coll(n){{if('data-brand-name' in n.attributes) brandSlots.push(n._txt); n.children.forEach(coll);}})(body);
const allStrongs=[];
(function coll(n){{if(n.tag==='strong') allStrongs.push(n._txt); n.children.forEach(coll);}})(body);
console.log(JSON.stringify({{title:_title,allStrongs,brandSlots}}));
"""
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(harness)
        tmp = f.name
    try:
        r = subprocess.run(["node", tmp], capture_output=True, text=True, check=False, timeout=10)
        assert r.returncode == 0, f"harness crashed: {r.stderr}"
        return _json.loads(r.stdout.strip())
    finally:
        os.unlink(tmp)


# ── HTML static contract ────────────────────────────────────────────────────────

@pytest.mark.parametrize("page", PAGES)
def test_page_html_title_is_generic_no_product_name(page):
    """HTML title must be a generic screen name with NO hardcoded product name."""
    parsed = HeadParser(ASSETS.joinpath(page + ".html").read_text())
    assert PRODUCT_NAME not in parsed.title, (
        f"{page}: HTML title must not hardcode '{PRODUCT_NAME}'; "
        "document.title is set at runtime by navigation.js"
    )
    assert parsed.title.strip(), f"{page}: HTML title must not be empty"


@pytest.mark.parametrize("page", PAGES)
def test_page_html_title_has_no_brand_separator(page):
    """HTML title must not contain '· DevRoster' brand separator — that is runtime-only."""
    parsed = HeadParser(ASSETS.joinpath(page + ".html").read_text())
    title = parsed.title.strip()
    assert "· " + PRODUCT_NAME not in title, (
        f"{page}: static HTML title '{title}' must not embed brand separator"
    )


@pytest.mark.parametrize("page", PAGES)
def test_page_has_favicon_link(page):
    """All product pages must declare brand-mark.svg as favicon."""
    parsed = HeadParser(ASSETS.joinpath(page + ".html").read_text())
    icons = [
        lnk for lnk in parsed.links
        if lnk.get("rel") == "icon" and lnk.get("href") == "/brand-mark.svg"
    ]
    assert icons, f"{page}: missing <link rel='icon' href='/brand-mark.svg'>"
    assert icons[0].get("type") == "image/svg+xml", f"{page}: favicon must be type='image/svg+xml'"


@pytest.mark.parametrize("page", PAGES)
def test_branding_script_loads_before_navigation_script(page):
    """branding.js must appear in <head> before navigation.js."""
    parsed = HeadParser(ASSETS.joinpath(page + ".html").read_text())
    srcs = [s.get("src", "") for s in parsed.scripts]
    assert "/branding.js" in srcs, f"{page}: missing <script src='/branding.js'>"
    assert "/navigation.js" in srcs, f"{page}: missing <script src='/navigation.js'>"
    brand_idx = next(i for i, s in enumerate(srcs) if s == "/branding.js")
    nav_idx = next(i for i, s in enumerate(srcs) if s == "/navigation.js")
    assert brand_idx < nav_idx, f"{page}: branding.js must appear before navigation.js"


def test_dashboard_footer_has_brand_name_slot():
    """dashboard.html footer must contain a data-brand-name slot, not a hardcoded product name."""
    html = ASSETS.joinpath("dashboard.html").read_text()
    assert "Agent Dev" not in html, (
        "dashboard.html must not contain hardcoded 'Agent Dev' old product name"
    )
    assert "data-brand-name" in html, (
        "dashboard.html must contain a data-brand-name slot for the footer brand identity"
    )


# ── SVG contract ────────────────────────────────────────────────────────────────

def test_brand_mark_svg_no_text_or_font_elements():
    """brand-mark.svg must not use <text> elements or font-family attributes."""
    svg = ASSETS.joinpath("brand-mark.svg").read_text()
    assert "<text" not in svg.lower(), (
        "brand-mark.svg must not use <text> elements; use vector paths/polylines instead"
    )
    assert "font-family" not in svg.lower(), (
        "brand-mark.svg must not reference font-family; brackets must be pure vector geometry"
    )
    assert "font-size" not in svg.lower(), (
        "brand-mark.svg must not set font-size; no text rendering allowed"
    )


def test_brand_mark_svg_has_minimum_stroke_width():
    """brand-mark.svg vector strokes must be at least 2px in the 32-unit viewBox."""
    svg = ASSETS.joinpath("brand-mark.svg").read_text()
    stroke_widths = re.findall(r'stroke-width=["\']([0-9.]+)["\']', svg)
    for w in stroke_widths:
        assert float(w) >= 2.0, f"stroke-width {w} is below 2px minimum in 32-unit viewBox"


def test_brand_mark_svg_has_no_external_references():
    """brand-mark.svg must be self-contained: no external hrefs, scripts, or fonts."""
    svg = ASSETS.joinpath("brand-mark.svg").read_text()
    assert "<script" not in svg.lower(), "SVG must not contain <script>"
    assert "font-face" not in svg.lower(), "SVG must not embed @font-face"
    for attr in ("href=", "src=", "xlink:href="):
        if attr in svg:
            pattern = re.compile(attr + r'["\']https?://', re.IGNORECASE)
            assert not pattern.search(svg), f"SVG must not use external {attr} URL"


def test_brand_mark_svg_xml_valid():
    """brand-mark.svg must parse as valid XML with the expected root element."""
    import xml.etree.ElementTree as ET
    svg_text = ASSETS.joinpath("brand-mark.svg").read_text()
    root = ET.fromstring(svg_text)
    assert root.tag in ("{http://www.w3.org/2000/svg}svg", "svg"), "root must be <svg>"
    # Must have no text elements anywhere in the tree
    for el in root.iter():
        local = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        assert local != "text", f"SVG must not contain <text> elements, found: {ET.tostring(el)}"
        for attr_name in el.attrib:
            assert "font-family" not in attr_name.lower(), (
                f"SVG must not use font-family attribute on <{local}>"
            )
            assert "font-size" not in attr_name.lower(), (
                f"SVG must not use font-size attribute on <{local}>"
            )


# ── branding.js contract ────────────────────────────────────────────────────────

def test_branding_js_defines_frozen_app_brand():
    """branding.js must define AppBrand with required fields."""
    if not shutil.which("node"):
        pytest.skip("Node required.")
    branding_src = ASSETS.joinpath("branding.js").read_text()
    check = (
        branding_src
        + "\nconst b = globalThis.AppBrand;"
        + "\nif (!b) throw new Error('AppBrand not defined');"
        + "\nif (b.name !== 'DevRoster') throw new Error('name: ' + b.name);"
        + "\nif (!b.tagline) throw new Error('tagline missing');"
        + "\nif (!b.logo) throw new Error('logo missing');"
        + "\ntry { b.name = 'X'; } catch(e) {}"
        + "\nif (b.name !== 'DevRoster') throw new Error('must be frozen');"
        + "\nconsole.log('ok');"
    )
    result = subprocess.run(
        ["node", "-e", check],
        capture_output=True, text=True, check=False, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_branding_js_no_external_references():
    """branding.js must not contain network calls or eval."""
    src = ASSETS.joinpath("branding.js").read_text()
    assert "fetch(" not in src
    assert "XMLHttpRequest" not in src
    assert "eval(" not in src
    assert "import(" not in src


# ── navigation.js brand contract (Node harness) ─────────────────────────────────

def test_branding_js_graceful_fallback_without_app_brand():
    """navigation.js must run without error when AppBrand is not defined."""
    if not shutil.which("node"):
        pytest.skip("Node required.")
    nav_src = ASSETS.joinpath("navigation.js").read_text()
    result = _nav_harness(None, nav_src, has_header=False)
    # Just reaching here means it didn't crash.
    assert result is not None


def test_fallback_uses_generic_label_not_old_brand():
    """Fallback product label (no AppBrand) must not be the old 'Agent Dev' name."""
    if not shutil.which("node"):
        pytest.skip("Node required.")
    nav_src = ASSETS.joinpath("navigation.js").read_text()
    result = _nav_harness(None, nav_src, has_header=True)
    strongs = result.get("allStrongs", [])
    for text in strongs:
        assert "Agent Dev" not in text, (
            f"Fallback must not use old brand name 'Agent Dev' in any strong element, got: {strongs}"
        )


def test_navigation_js_uses_product_name_from_app_brand():
    """With AppBrand loaded, a <strong> in the menu heading must contain AppBrand.name."""
    if not shutil.which("node"):
        pytest.skip("Node required.")
    branding_src = ASSETS.joinpath("branding.js").read_text()
    nav_src = ASSETS.joinpath("navigation.js").read_text()
    result = _nav_harness(branding_src, nav_src, has_header=True)
    assert PRODUCT_NAME in result["allStrongs"], (
        f"menu heading strong must be '{PRODUCT_NAME}', got strongs: {result['allStrongs']}"
    )


def test_navigation_js_sets_document_title_with_brand():
    """navigation.js must set document.title = pageTitle + ' · ' + AppBrand.name when brand present."""
    if not shutil.which("node"):
        pytest.skip("Node required.")
    branding_src = ASSETS.joinpath("branding.js").read_text()
    nav_src = ASSETS.joinpath("navigation.js").read_text()
    page_title = "테스트화면"
    result = _nav_harness(branding_src, nav_src, page_title=page_title, has_header=True)
    expected = page_title + " · " + PRODUCT_NAME
    assert result["title"] == expected, (
        f"document.title must be '{expected}', got: {result['title']!r}"
    )


def test_navigation_js_title_not_set_without_brand():
    """Without AppBrand, navigation.js must not set document.title (generic HTML title stays)."""
    if not shutil.which("node"):
        pytest.skip("Node required.")
    nav_src = ASSETS.joinpath("navigation.js").read_text()
    result = _nav_harness(None, nav_src, page_title="화면", has_header=True)
    assert result["title"] == "", (
        f"Without AppBrand, document.title must remain unset (empty), got: {result['title']!r}"
    )


def test_navigation_js_populates_brand_name_slots():
    """navigation.js must fill all [data-brand-name] elements with productName via textContent."""
    if not shutil.which("node"):
        pytest.skip("Node required.")
    branding_src = ASSETS.joinpath("branding.js").read_text()
    nav_src = ASSETS.joinpath("navigation.js").read_text()
    result = _nav_harness(branding_src, nav_src, has_header=True, brand_slots=2)
    slots = result.get("brandSlots", [])
    assert len(slots) == 2, f"Expected 2 brand-name slots filled, got {slots}"
    for text in slots:
        assert text == PRODUCT_NAME, f"brand-name slot must be '{PRODUCT_NAME}', got: {text!r}"


def test_alternate_app_brand_name_changes_header_menu_title():
    """Changing AppBrand.name to an alternate value must update header, menu, title, and slots."""
    if not shutil.which("node"):
        pytest.skip("Node required.")
    alt_brand_src = """
globalThis.AppBrand = Object.freeze({
  name: 'AltBrand',
  koreanName: '대체브랜드',
  descriptor: '테스트',
  tagline: '대체 태그라인.',
  logo: '/brand-mark.svg'
});
"""
    nav_src = ASSETS.joinpath("navigation.js").read_text()
    result = _nav_harness(alt_brand_src, nav_src, page_title="화면명", has_header=True, brand_slots=1)
    assert "AltBrand" in result["allStrongs"], (
        f"menu heading strong must reflect alternate brand, got strongs: {result['allStrongs']}"
    )
    assert result["title"] == "화면명 · AltBrand", (
        f"document.title must reflect alternate brand, got: {result['title']!r}"
    )
    slots = result.get("brandSlots", [])
    assert slots and slots[0] == "AltBrand", (
        f"brand-name slot must reflect alternate brand, got: {slots}"
    )


# ── HTTP boundary tests ────────────────────────────────────────────────────────

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
    conn = http.client.HTTPConnection(*server.server_address, timeout=5)
    conn.request(method, path, headers=headers or {})
    resp = conn.getresponse()
    result = resp.status, dict(resp.getheaders()), resp.read()
    conn.close()
    return result


@pytest.mark.parametrize(
    "entry",
    ["studio", "agent", "workflow", "setup", "source", "integrations", "references", "dashboard"],
)
def test_brand_assets_served_with_correct_mime_and_identical_bytes(tmp_path, monkeypatch, entry):
    """Every web entry point must serve branding.js and brand-mark.svg correctly."""
    from agent_dev.ui.agent_server import AgentWeb
    from agent_dev.ui.dashboard import make_server as dashboard_server
    from agent_dev.ui.design_reference_server import DesignReferenceWeb
    from agent_dev.ui.integration_server import IntegrationWeb
    from agent_dev.ui.local_web import make_server as local_server
    from agent_dev.ui.setup_server import SetupWeb
    from agent_dev.ui.source_server import SourceWeb
    from agent_dev.ui.studio_server import StudioWeb
    from agent_dev.ui.workflow_server import WorkflowWeb

    def forbidden(*args, **kwargs):
        pytest.fail("brand assets must not open a database or contact external services")

    monkeypatch.setattr("sqlite3.connect", forbidden)
    monkeypatch.setattr("subprocess.Popen", forbidden)
    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    root = tmp_path / "no-workspace"
    factories = {
        "agent": AgentWeb,
        "integrations": IntegrationWeb,
        "references": DesignReferenceWeb,
        "source": SourceWeb,
        "workflow": WorkflowWeb,
        "setup": SetupWeb,
        "studio": StudioWeb,
    }
    if entry == "dashboard":
        server = dashboard_server(root, 0)
        ctx = running_server(server)
    else:
        web = factories[entry](root)
        server = local_server(web, 0)
        ctx = running_server(server)

    with ctx as srv:
        branding_bytes = ASSETS.joinpath("branding.js").read_bytes()
        svg_bytes = ASSETS.joinpath("brand-mark.svg").read_bytes()

        status, headers, body = request(srv, "/branding.js")
        assert status == 200, f"{entry}: /branding.js returned {status}"
        assert "javascript" in headers.get("Content-Type", ""), \
            f"{entry}: wrong MIME for branding.js: {headers.get('Content-Type')}"
        assert body == branding_bytes, f"{entry}: branding.js bytes mismatch"

        status, headers, body = request(srv, "/brand-mark.svg")
        assert status == 200, f"{entry}: /brand-mark.svg returned {status}"
        assert headers.get("Content-Type", "").startswith("image/svg+xml"), \
            f"{entry}: wrong MIME for brand-mark.svg: {headers.get('Content-Type')}"
        assert body == svg_bytes, f"{entry}: brand-mark.svg bytes mismatch"

    assert not root.exists()


def test_brand_assets_reject_cross_origin(tmp_path, monkeypatch):
    """Brand assets must honour the same origin guard."""
    from agent_dev.ui.local_web import make_server as local_server
    from agent_dev.ui.workflow_server import WorkflowWeb

    monkeypatch.setattr("sqlite3.connect", lambda *a, **kw: pytest.fail("no DB"))
    root = tmp_path / "no-workspace"
    web = WorkflowWeb(root)
    server = local_server(web, 0)
    with running_server(server) as srv:
        for bad_header in (
            {"Origin": "https://evil.example"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            assert request(srv, "/branding.js", bad_header)[0] == 403, \
                f"branding.js must be rejected for {bad_header}"
            assert request(srv, "/brand-mark.svg", bad_header)[0] == 403, \
                f"brand-mark.svg must be rejected for {bad_header}"

    assert not root.exists()


def test_navigation_assets_dict_rejects_non_allowlisted_paths():
    """NAVIGATION_ASSETS exact-match must reject traversal and decorated paths."""
    from agent_dev.ui.shared_assets import NAVIGATION_ASSETS

    for path in (
        "/branding.js/",
        "/%62randing.js",
        "/brand-mark.svg/",
        "/../branding.js",
        "/assets/branding.js",
    ):
        assert path not in NAVIGATION_ASSETS, f"traversal path {path!r} must not be in NAVIGATION_ASSETS"


def test_navigation_assets_allowlist_contains_brand_entries():
    """NAVIGATION_ASSETS must export exactly the brand entry points."""
    from agent_dev.ui.shared_assets import NAVIGATION_ASSETS

    assert "/branding.js" in NAVIGATION_ASSETS
    assert "/brand-mark.svg" in NAVIGATION_ASSETS
    name, mime = NAVIGATION_ASSETS["/branding.js"]
    assert name == "branding.js"
    assert mime == "text/javascript"
    name, mime = NAVIGATION_ASSETS["/brand-mark.svg"]
    assert name == "brand-mark.svg"
    assert mime == "image/svg+xml"


def test_navigation_web_matches_brand_assets():
    """NavigationWeb.matches() must cover the new brand assets."""
    from agent_dev.ui.navigation_server import NavigationWeb

    assert NavigationWeb.matches("/branding.js")
    assert NavigationWeb.matches("/brand-mark.svg")
    assert NavigationWeb.matches("/navigation.js")
    assert NavigationWeb.matches("/navigation.css")
    assert not NavigationWeb.matches("/evil.js")
    assert not NavigationWeb.matches("/brand-mark.svg/")


@pytest.mark.parametrize("page", PAGES)
def test_page_still_loads_shared_styles_first(page):
    """After branding changes, shared CSS order must be preserved."""
    class Links(HTMLParser):
        def __init__(self, html):
            super().__init__()
            self.paths = []
            self.feed(html)
        def handle_starttag(self, tag, attrs):
            d = dict(attrs)
            if tag == "link" and d.get("rel") == "stylesheet":
                self.paths.append(d["href"])

    paths = Links(ASSETS.joinpath(page + ".html").read_text()).paths
    SHARED = ["/ui-tokens.css", "/ui-base.css", "/ui-components.css"]
    for i, shared in enumerate(SHARED):
        assert paths[i] == shared, f"{page}: shared CSS[{i}] must be {shared}, got {paths[i]}"


# ── Package data (importlib) ────────────────────────────────────────────────────

def test_importlib_brand_mark_svg_accessible():
    """brand-mark.svg must be importlib-accessible from the installed package."""
    svg_bytes = ASSETS.joinpath("brand-mark.svg").read_bytes()
    assert svg_bytes, "brand-mark.svg must not be empty"
    via_importlib = files("agent_dev.ui").joinpath("assets", "brand-mark.svg").read_bytes()
    assert via_importlib == svg_bytes, "importlib bytes must match filesystem bytes"


def test_importlib_branding_js_accessible():
    """branding.js must be importlib-accessible from the installed package."""
    js_bytes = ASSETS.joinpath("branding.js").read_bytes()
    assert js_bytes
    via_importlib = files("agent_dev.ui").joinpath("assets", "branding.js").read_bytes()
    assert via_importlib == js_bytes


def test_wheel_build_contains_brand_mark_svg(tmp_path):
    """Wheel build must include brand-mark.svg as package data (verify via zipfile inspection)."""
    # Locate the build prerequisites without importing them: importing pip and then
    # setuptools in this process trips setuptools' _distutils_hack on Python 3.11.
    import importlib.util
    import zipfile

    for name in ("pip", "setuptools"):
        if importlib.util.find_spec(name) is None:
            pytest.skip(f"{name} not available — wheel build prerequisite missing")

    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps",
         "--no-build-isolation", "-q",
         str(repo_root),
         "-w", str(tmp_path)],
        capture_output=True, text=True, check=False, timeout=60,
        cwd=str(tmp_path),
    )
    wheels = list(tmp_path.glob("*.whl"))
    assert result.returncode == 0 and wheels, (
        f"wheel build failed (rc={result.returncode}): {result.stderr[:400]}"
    )
    with zipfile.ZipFile(wheels[0]) as zf:
        names = zf.namelist()
        svg_entries = [n for n in names if n.endswith("brand-mark.svg")]
        assert svg_entries, f"brand-mark.svg not found in wheel {wheels[0].name}: {names[:20]}"
        whl_bytes = zf.read(svg_entries[0])
    source_bytes = ASSETS.joinpath("brand-mark.svg").read_bytes()
    assert whl_bytes == source_bytes, "wheel brand-mark.svg bytes must match source"
