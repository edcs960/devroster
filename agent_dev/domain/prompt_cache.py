"""Prompt order for provider prompt caching: what repeats across calls comes first.

Providers reuse a prompt by prefix: only the text identical to an earlier call, from the
first character on, is read from their cache. Prompt builders therefore put what repeats
first and open the per-call part with ``CALL_SECTION``. OpenAI, xAI and Gemini find the
shared prefix on their own; Anthropic reuses it only when the request marks where it ends
(adapters/api_runner.py). Order and markers never change what the model is asked.
"""

# Opens the per-call part of a prompt. Untrusted data before it is serialized as one line of
# JSON, where a line break is always escaped, so data cannot contain this line.
CALL_SECTION = "\nTHIS CALL:\n"

# Workflow context keys that are the same for every phase of a task, most widely shared
# first: the project's rules, then the approved design, then the task itself.
STABLE_CONTEXT_KEYS = (
    "operating_rules",
    "project_rules",
    "project_rules_rule",
    "rule",
    "context_policy",
    "output_language_rule",
    "commit_message_rule",
    "approved_design",
    "knowledge_verified_data",
    "task",
    "snapshot_digest",
    "write_scope",
    "repositories",
    "tool_preparation_warnings",
)


def stable_first(payload):
    """``payload`` with the keys every phase of the task repeats moved to the front."""
    head = {key: payload[key] for key in STABLE_CONTEXT_KEYS if key in payload}
    return {**head, **{key: value for key, value in payload.items() if key not in head}}


def stable_length(prompt):
    """Leading characters of ``prompt`` that later calls repeat; 0 without a per-call part."""
    index = prompt.find(CALL_SECTION) if isinstance(prompt, str) else -1
    end = index + len(CALL_SECTION)
    return end if index > 0 and end < len(prompt) else 0
