"""Output contract for one runner invocation that serves several role nodes (ADR 0002)."""

import json

# Static context a resumed conversation already received in its first invocation.
RESUME_OMITTED = (
    "operating_rules",
    "project_rules",
    "project_rules_rule",
    "project_skills_rule",
    "context_policy",
    "approved_design",
    "knowledge_verified_data",
    "wiki_verified_data",
    "wiki_items",
    "wiki_item_ids",
    "skills_rule",
    "approved_skills",
)

START = "<<<NODE {}>>>"
END = "<<<END NODE {}>>>"


def instructions(node_ids):
    """Tell the runner how to return one fenced result per node, in order."""
    order = ", ".join(node_ids)
    example = node_ids[0] if node_ids else "node"
    return "\n".join(
        [
            f"Perform these roles in order within this one session: {order}.",
            (
                "Each node in `nodes` carries its own role_instruction; apply only that node's"
                " instruction inside its section."
            ),
            "Return one section per node, in the same order, and nothing outside the sections:",
            START.format(example),
            "<that role's output>",
            '```json\n{"handoff_status":"pass"}\n```',
            END.format(example),
            "Each section ends with exactly one Harness Control JSON block for that node.",
            (
                'If a node changed files, add "changed_paths": "path/a, path/b" (repository-relative)'
                " to that node's Harness Control JSON."
            ),
            "If a node reports an issue, stop after its section; later nodes will run again.",
        ]
    )


def resumed_context(context):
    """Context JSON for a runner that continues its earlier conversation.

    Only the runner that really resumes may drop the static context; a fresh session
    (another provider, or a conversation that is gone) needs the full context.
    """
    try:
        value = json.loads(context)
    except (TypeError, ValueError):
        return context
    if not isinstance(value, dict):
        return context
    for key in RESUME_OMITTED:
        value.pop(key, None)
    # Session contexts keep per-node values (such as each role's wiki items) under nodes.
    for node in value.get("nodes", []) if isinstance(value.get("nodes"), list) else []:
        if isinstance(node, dict):
            for key in RESUME_OMITTED:
                node.pop(key, None)
    value["resumed_session"] = (
        "Continue the earlier conversation. Its operating rules and approved design still "
        "apply; fix the findings and evidence in this context."
    )
    return json.dumps(value, ensure_ascii=False)


def split(output, node_ids):
    """Node id -> its section text, or None when the section is missing or unterminated.

    Sections are read in node order from where the previous one ended, so marker text
    quoted inside an earlier section cannot be taken as a later node's result.
    """
    sections, cursor = {}, 0
    for node_id in node_ids:
        start = output.find(START.format(node_id), cursor)
        end = -1 if start < 0 else output.find(END.format(node_id), start)
        if end < 0:
            sections[node_id] = None
            continue
        sections[node_id] = output[start + len(START.format(node_id)) : end].strip()
        cursor = end + len(END.format(node_id))
    return sections
