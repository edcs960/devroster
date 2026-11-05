"""Folder-name helper for the user-facing ``output/`` tree."""

from __future__ import annotations

import re


def slugify(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z가-힣_-]+", "-", value.strip())
    normalized = re.sub(r"-+", "-", normalized).strip("-")
    return normalized[:80] or "new-product"
