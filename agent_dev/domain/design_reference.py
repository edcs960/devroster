"""Strict design locators; remote URLs never become executable assets."""

import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from agent_dev.domain.errors import DomainError, ErrorCode


def figma_locator(value):
    if not isinstance(value, str) or len(value) > 1500:
        raise DomainError(ErrorCode.INVALID_INPUT, "Figma 파일 링크 또는 파일 key를 입력해주세요.")
    if re.fullmatch(r"[A-Za-z0-9]{1,120}", value):
        return {
            "provider": "figma",
            "file_key": value,
            "node_id": "",
            "url": "https://www.figma.com/design/" + value,
        }
    parsed = urlsplit(value)
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"figma.com", "www.figma.com"}
        or parsed.username
        or parsed.port
        or len(parts) < 2
        or parts[0] not in {"file", "design", "proto", "board"}
        or not re.fullmatch(r"[A-Za-z0-9]{1,120}", parts[1])
    ):
        raise DomainError(ErrorCode.INVALID_INPUT, "공식 Figma 파일 링크를 입력해주세요.")
    node = parse_qs(parsed.query).get("node-id", [""])[0].replace("-", ":")
    if node and not re.fullmatch(r"\d{1,20}:\d{1,20}", node):
        raise DomainError(ErrorCode.INVALID_INPUT, "Figma node-id 형식을 확인해주세요.")
    return {
        "provider": "figma",
        "file_key": parts[1],
        "node_id": node,
        "url": urlunsplit(("https", "www.figma.com", "/design/" + parts[1], "", "")),
    }


def stitch_locator(project_id, screen_id=""):
    if (
        not all(
            isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_-]{0,120}", v)
            for v in (project_id, screen_id)
        )
        or not project_id
    ):
        raise DomainError(ErrorCode.INVALID_INPUT, "Stitch 프로젝트/화면 ID를 확인해주세요.")
    return {"provider": "stitch", "project_id": project_id, "screen_id": screen_id}
