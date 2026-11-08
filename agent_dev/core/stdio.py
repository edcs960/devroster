"""UTF-8 console output on every platform.

Windows writes redirected output in the ANSI code page (cp1252 and the like), which
cannot encode Korean, so help and status text crashed when piped or captured.
"""

from __future__ import annotations

import sys


def use_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        encoding = (getattr(stream, "encoding", None) or "").lower().replace("-", "").replace("_", "")
        reconfigure = getattr(stream, "reconfigure", None)
        if encoding == "utf8" or reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass  # Replaced or detached streams keep their own encoding.
