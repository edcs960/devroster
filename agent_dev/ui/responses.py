"""The one HTTP response writer of the local screens: JSON or file bytes with the security headers."""

import json


def send(handler, data, kind="application/json", status=200, filename=None):
    if kind == "application/json":
        data = json.dumps(data, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header(
        "Content-Type", kind + ("; charset=utf-8" if kind != "application/zip" else "")
    )
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
    )
    if filename:
        handler.send_header("Content-Disposition", 'attachment; filename="' + filename + '"')
    handler.end_headers()
    handler.wfile.write(data)
