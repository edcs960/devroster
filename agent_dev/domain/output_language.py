"""The language the AI writes in: documents, results, commit messages and pull requests."""

import re

from agent_dev.domain.content import redact
from agent_dev.domain.errors import DomainError, ErrorCode

DEFAULT = "한국어"
MAX_LANGUAGE = 40
MAX_COMMIT_MESSAGE = 2000
MAX_COMMIT_TITLE = 100
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def normalize(value) -> str:
    """A project's choice; "" means the default (한국어)."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise DomainError(ErrorCode.INVALID_INPUT, "작업 언어는 글자로 입력해주세요.")
    value = value.strip()
    if len(value) > MAX_LANGUAGE or _CONTROL.search(value) or "\n" in value:
        raise DomainError(ErrorCode.INVALID_INPUT, "작업 언어는 한 줄, 40자 이하로 입력해주세요.")
    return value


def language(resolved: dict) -> str:
    return (resolved or {}).get("output_language") or DEFAULT


def writing_rule(lang: str) -> str:
    return (
        f"Write every human-readable result in {lang}: documents, explanations, summaries, "
        "review notes, commit messages and pull request text. Keep code, identifiers, commands, "
        "file paths, error messages and quoted source text unchanged."
    )


def commit_rule(lang: str) -> str:
    return (
        'If you changed files, add "commit_message" to your Harness Control JSON. Write it in '
        f"{lang} as a conventional commit: the first line starts with feat:, fix:, docs:, refactor:, "
        "test: or chore: and summarizes the change in under 72 characters; after a blank line, "
        "explain why it changed and how it was verified. Never include secrets."
    )


def clean_commit_message(value) -> str | None:
    """An AI-written commit message that is safe to use, else None (the fallback is used)."""
    if not isinstance(value, str):
        return None
    text = _CONTROL.sub("", value.replace("\r\n", "\n")).strip()
    # "****" is what redaction left of a secret: such a message is not used at all.
    if not text or len(text) > MAX_COMMIT_MESSAGE or redact(text) != text or "****" in text:
        return None
    title = text.split("\n", 1)[0].strip()
    if not title or len(title) > MAX_COMMIT_TITLE:
        return None
    return text
