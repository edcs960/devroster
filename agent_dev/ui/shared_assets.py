"""Exact, workspace-independent allowlist for the product's shared styles."""

SHARED_CSS = ("ui-tokens.css", "ui-base.css", "ui-components.css")
_SHARED_PATHS = frozenset("/" + name for name in SHARED_CSS)


def shared_asset_name(path: str) -> str | None:
    return path[1:] if path in _SHARED_PATHS else None


NAVIGATION_ASSETS = {
    "/journey.css": ("journey.css", "text/css"),
    "/navigation.css": ("navigation.css", "text/css"),
    "/navigation.js": ("navigation.js", "text/javascript"),
    "/ui-progress.js": ("ui-progress.js", "text/javascript"),
    "/branding.js": ("branding.js", "text/javascript"),
    "/brand-mark.svg": ("brand-mark.svg", "image/svg+xml"),
}
