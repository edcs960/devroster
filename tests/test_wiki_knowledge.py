import json

import pytest

from agent_dev.adapters.workflow_runtime import WorkflowRuntime
from agent_dev.domain.errors import DomainError, ErrorCode


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / "app"
    (root / "config").mkdir(parents=True)
    (root / "config/runtime.json").write_text(json.dumps({"orchestrator_v2": {}}), encoding="utf-8")
    runtime = WorkflowRuntime(root)
    runtime.store.register_project("P", "Project", {})
    (root / "issues").mkdir()
    (root / "issues/issue-001.md").write_text(RESOLVED_ISSUE.format("migration updated rows"), encoding="utf-8")
    (root / "issues/issue-002.md").write_text(RESOLVED_ISSUE.format("empty list passed"), encoding="utf-8")
    # docs/wiki belongs to the workspace project.
    with runtime.store.db.transaction() as sql:
        sql.execute(
            "INSERT INTO workspace_settings VALUES(1,?,?)", (1, json.dumps({"project_id": "P"}))
        )
    return runtime


RESOLVED_ISSUE = "## Root Cause\n{}\n\n## Resolution\n- Resolved: true\n"


def entry(item_id="L01", **overrides):
    value = {
        "item_id": item_id,
        "page": "lessons",
        "title": "보호 행 무갱신",
        "check": "마이그레이션은 RELEASED 행을 갱신하지 않는다.",
        "condition": "Flyway 마이그레이션 추가",
        "roles": ["dba", "architect"],
        "tags": ["migration"],
        "sources": ["issues/issue-001.md"],
    }
    value.update(overrides)
    return value


def test_candidate_is_served_only_after_approval(runtime):
    wiki = runtime.wiki
    assert wiki.propose("P", [entry()]) == [{"item_id": "L01", "revision": 1, "state": "candidate"}]
    assert wiki.active("P") == []
    wiki.approve("P", "L01", 1, "local-user")
    (item,) = wiki.active("P")
    assert item["revision"] == 1
    assert item["approval"]["actor"] == "local-user"
    assert item["approval"]["body_digest"] == item["body_digest"]
    with pytest.raises(DomainError) as error:
        wiki.approve("P", "L01", 1, "local-user")
    assert error.value.code == ErrorCode.STALE_VERSION


def test_revision_needs_its_own_approval_and_supersedes_on_approval(runtime):
    wiki = runtime.wiki
    wiki.propose("P", entry())
    wiki.approve("P", "L01", 1, "u")
    assert wiki.revise("P", "L01", entry(check="개정된 체크 항목."))["revision"] == 2
    # The approved body stays served until the new body is approved.
    assert [item["revision"] for item in wiki.active("P")] == [1]
    wiki.approve("P", "L01", 2, "u")
    assert [item["revision"] for item in wiki.active("P")] == [2]
    states = {item["revision"]: item["state"] for item in wiki.items("P")}
    assert states == {1: "superseded", 2: "active"}


def test_a_newer_candidate_supersedes_an_unapproved_one(runtime):
    wiki = runtime.wiki
    wiki.propose("P", entry())
    wiki.revise("P", "L01", entry(check="두 번째 초안."))
    with pytest.raises(DomainError):
        wiki.approve("P", "L01", 1, "u")
    wiki.approve("P", "L01", 2, "u")


def test_changed_source_blocks_approval_and_stops_serving(runtime):
    wiki = runtime.wiki
    source = runtime.project_root / "issues/issue-001.md"
    wiki.propose("P", [entry(), entry("L02", sources=["issues/issue-002.md"])])
    source.write_text("edited after proposal", encoding="utf-8")
    with pytest.raises(DomainError) as error:
        wiki.approve("P", "L01", 1, "u")
    assert error.value.code == ErrorCode.STALE_VERSION
    wiki.approve("P", "L02", 1, "u")
    (runtime.project_root / "issues/issue-002.md").write_text("edited after approval", encoding="utf-8")
    assert wiki.active("P") == []
    result = wiki.render("P")
    assert result["stale"] == ["L02"] and result["active"] == 0
    index = (runtime.project_root / "docs/wiki/index.md").read_text(encoding="utf-8")
    assert "재검증 필요" in index and "L02 r1" in index


def test_reject_and_retire(runtime):
    wiki = runtime.wiki
    wiki.propose("P", [entry(), entry("L02", sources=["issues/issue-002.md"])])
    wiki.reject("P", "L01", 1, "u")
    wiki.approve("P", "L02", 1, "u")
    with pytest.raises(DomainError):
        wiki.retire("P", "L02", " ", "u")
    wiki.retire("P", "L02", "정책 변경", "u")
    assert wiki.active("P") == []
    assert {item["item_id"]: item["state"] for item in wiki.items("P")} == {
        "L01": "rejected",
        "L02": "retired",
    }


def test_projects_are_isolated(runtime):
    runtime.store.register_project("OTHER", "Other", {})
    runtime.wiki.propose("P", entry())
    runtime.wiki.approve("P", "L01", 1, "u")
    assert runtime.wiki.active("OTHER") == []
    with pytest.raises(DomainError):
        runtime.wiki.approve("OTHER", "L01", 1, "u")
    with pytest.raises(DomainError):
        runtime.wiki.propose("MISSING", entry())


@pytest.mark.parametrize(
    "overrides",
    [
        {"roles": ["unknown-role"]},
        {"roles": []},
        {"sources": []},
        {"sources": ["issues/missing.md"]},
        {"sources": ["/etc/hosts"]},
        {"sources": ["../outside.md"]},
        {"page": "codebase-map"},
        {"item_id": "lower"},
        {"check": ""},
        {"tags": ["Not A Slug"]},
        {"title": "x" * 121},
    ],
)
def test_invalid_entries_are_rejected(runtime, overrides):
    (runtime.project_root.parent / "outside.md").write_text("outside", encoding="utf-8")
    with pytest.raises(DomainError) as error:
        runtime.wiki.propose("P", entry(**overrides))
    assert error.value.code == ErrorCode.INVALID_INPUT
    assert runtime.wiki.items("P") == []


def test_symlinked_source_and_duplicate_batch_are_rejected(runtime):
    (runtime.project_root / "link.md").symlink_to(runtime.project_root / "issues/issue-001.md")
    with pytest.raises(DomainError):
        runtime.wiki.propose("P", entry(sources=["link.md"]))
    with pytest.raises(DomainError):
        runtime.wiki.propose("P", [entry(), entry()])
    runtime.wiki.propose("P", entry())
    with pytest.raises(DomainError):
        runtime.wiki.propose("P", entry())
    assert len(runtime.wiki.items("P")) == 1


def test_registered_secrets_are_redacted(tmp_path):
    root = tmp_path / "app"
    (root / "config").mkdir(parents=True)
    (root / "config/runtime.json").write_text("{}", encoding="utf-8")
    (root / "issue.md").write_text(RESOLVED_ISSUE.format("evidence"), encoding="utf-8")
    runtime = WorkflowRuntime(root)
    runtime.store.secrets = ("hunter2-secret-value",)
    runtime.store.register_project("P", "Project", {})
    runtime.wiki.propose(
        "P", entry(check="계정 비밀번호 hunter2-secret-value 사용", sources=["issue.md"])
    )
    (item,) = runtime.wiki.items("P")
    assert "hunter2-secret-value" not in json.dumps(item, ensure_ascii=False)


def test_render_writes_generated_pages(runtime):
    wiki = runtime.wiki
    wiki.propose(
        "P",
        [
            entry(),
            entry(
                "T01",
                page="testing",
                title="E2E 실행",
                check="e2e profile 백엔드로 실행한다 | 표 구분자 포함",
                roles=["qa", "all"],
                sources=["issues/issue-002.md"],
            ),
        ],
    )
    wiki.approve("P", "L01", 1, "u")
    wiki.approve("P", "T01", 1, "u")
    result = wiki.render("P")
    assert result == {
        "directory": "docs/wiki",
        "pages": ["index.md", "lessons.md", "testing.md"],
        "active": 2,
        "stale": [],
    }
    folder = runtime.project_root / "docs/wiki"
    lessons = (folder / "lessons.md").read_text(encoding="utf-8")
    testing = (folder / "testing.md").read_text(encoding="utf-8")
    assert "직접 수정하지 않는다" in lessons
    assert "| L01 r1 | Architect, DBA |" in lessons and "`issues/issue-001.md`" in lessons
    assert "전체, QA" in testing and "\\| 표 구분자" in testing
    assert "[lessons.md](lessons.md) | 재발 방지 목록 | 1" in (folder / "index.md").read_text(encoding="utf-8")


def test_render_refuses_a_symlinked_wiki_directory(runtime, tmp_path):
    (runtime.project_root / "docs").mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (runtime.project_root / "docs/wiki").symlink_to(elsewhere)
    with pytest.raises(DomainError) as error:
        runtime.wiki.render("P")
    assert error.value.code == ErrorCode.POLICY_VIOLATION
    assert list(elsewhere.iterdir()) == []


def test_cli_propose_approve_list_render(runtime, capsys):
    from agent_dev.adapters.command_line import main

    root = runtime.root
    (runtime.project_root / "entries.json").write_text(
        json.dumps([entry()], ensure_ascii=False), encoding="utf-8"
    )

    def run(*arguments):
        main(["knowledge", *arguments, "--root", str(root), "--json"])
        return json.loads(capsys.readouterr().out)

    assert run("propose", "P", "entries.json")["status"] == "completed"
    assert run("approve", "P", "L01", "1", "--actor", "reviewer")["status"] == "completed"
    listed = run("list", "P")
    assert listed["status"] == "completed"
    rendered = run("render", "P")
    assert rendered["status"] == "completed"
    assert (runtime.project_root / "docs/wiki/lessons.md").is_file()
    (item,) = WorkflowRuntime(root).wiki.active("P")
    assert item["approval"]["actor"] == "reviewer"


def test_backup_from_before_knowledge_revisions_still_validates(tmp_path):
    import sqlite3

    from agent_dev.adapters import backup
    from agent_dev.adapters.state.sqlite.database import Database

    path = tmp_path / "state.db"
    Database(path)
    assert backup.validate_database(path) == 4
    with sqlite3.connect(path) as connection:
        # Recreate a v3 database: v4 only added two tables.
        connection.execute("DELETE FROM schema_versions WHERE version=4")
        connection.execute("DROP TABLE configuration_provenance")
        connection.execute("DROP TABLE shared_connection_bindings")
    assert backup.validate_database(path) == 3
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE knowledge_revisions")
    assert backup.validate_database(path) == 3


def graph(*nodes):
    return {"nodes": [{"kind": "role", **node} for node in nodes]}


def approve_all(wiki, entries):
    wiki.propose("P", entries)
    for value in entries:
        wiki.approve("P", value["item_id"], 1, "u")


def test_bundle_ranks_role_items_before_shared_and_by_task_tags(runtime):
    approve_all(
        runtime.wiki,
        [
            entry("L01", roles=["all"]),
            entry("L02", roles=["dba"], tags=["schema"]),
            entry("L03", roles=["dba"], tags=["migration"]),
            entry("L04", roles=["qa"]),
        ],
    )
    result = runtime.wiki.bundle(
        "P",
        graph({"id": "dba-node", "role_id": "dba"}, {"id": "custom", "role_id": "x"}),
        "Flyway migration for revisions",
    )
    assert result["selection"] == {
        "dba-node": ["L03 r1", "L02 r1", "L01 r1"],
        "custom": ["L01 r1"],
    }
    assert sorted(result["items"]) == ["L01 r1", "L02 r1", "L03 r1"]
    assert result["items"]["L03 r1"]["body_digest"]
    assert result["excluded"] == {}


def test_bundle_skips_whole_items_over_budgets(runtime, monkeypatch):
    from agent_dev.adapters import wiki_knowledge

    approve_all(runtime.wiki, [entry(f"L{n:02}", roles=["qa"]) for n in range(1, 11)])
    result = runtime.wiki.bundle("P", graph({"id": "qa", "role_id": "qa"}))
    assert len(result["selection"]["qa"]) == 8
    assert {row["reason"] for row in result["excluded"]["qa"]} == {"role_item_limit"}
    size = wiki_knowledge._size(runtime.wiki.active("P")[0])
    monkeypatch.setitem(wiki_knowledge.POLICY, "role_chars", size * 2 + 1)
    monkeypatch.setitem(wiki_knowledge.POLICY, "session_chars", size * 3)
    result = runtime.wiki.bundle(
        "P",
        graph(
            {"id": "qa", "role_id": "qa", "session": "verify"},
            {"id": "qa2", "role_id": "qa", "session": "verify"},
        ),
    )
    assert result["selection"]["qa"] == ["L01 r1", "L02 r1"]
    # The shared items count once; the session budget leaves no room for new ones.
    assert result["selection"]["qa2"] == ["L01 r1", "L02 r1"]
    assert {row["reason"] for row in result["excluded"]["qa"]} == {"role_char_limit"}


def test_bundle_serves_only_current_active_items(runtime):
    approve_all(runtime.wiki, [entry(), entry("L02", sources=["issues/issue-002.md"])])
    runtime.wiki.propose("P", entry("L03"))
    (runtime.project_root / "issues/issue-002.md").write_text("changed", encoding="utf-8")
    result = runtime.wiki.bundle("P", graph({"id": "dba", "role_id": "dba"}))
    assert result["selection"] == {"dba": ["L01 r1"]}


def test_prompt_items_and_resume_trimming():
    from agent_dev.adapters.wiki_knowledge import prompt_items
    from agent_dev.domain.session_output import resumed_context

    resolved = {
        "wiki": {
            "items": {
                "L01 r1": {
                    **dict.fromkeys(
                        ("id", "page", "title", "check", "condition", "sources", "trust"), "v"
                    ),
                    "body_digest": "d",
                    "approval": {},
                }
            },
            "selection": {"dba": ["L01 r1"]},
        }
    }
    assert prompt_items(resolved, "dba") == [
        dict.fromkeys(("id", "page", "title", "check", "condition", "sources", "trust"), "v")
    ]
    assert prompt_items(resolved, "qa") == [] and prompt_items({}, "dba") == []
    trimmed = json.loads(
        resumed_context(
            json.dumps(
                {"wiki_verified_data": [1], "nodes": [{"wiki_verified_data": [1], "id": "a"}]}
            )
        )
    )
    assert "wiki_verified_data" not in trimmed and trimmed["nodes"] == [{"id": "a"}]


def git(root, *arguments, input_text=None):
    import subprocess

    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        input=input_text,
    ).stdout.strip()


@pytest.mark.parametrize(
    "text",
    [
        "## Root Cause\nonly a cause\n",
        "## Root Cause\nx\n\n## Resolution\n- Resolved: false\n",
        "## Root Cause\nx\n\n## Resolution\n- Resolved: true\n- Resolved: false\n",
    ],
)
def test_approval_requires_a_final_resolution(runtime, text):
    (runtime.project_root / "issues/open.md").write_text(text, encoding="utf-8")
    runtime.wiki.propose("P", entry(sources=["issues/open.md"]))
    with pytest.raises(DomainError) as error:
        runtime.wiki.approve("P", "L01", 1, "u")
    assert error.value.code == ErrorCode.VERIFICATION_FAILED
    (item,) = runtime.wiki.items("P")
    assert item["state"] == "candidate"
    assert item["verification"]["state"] == "resolution_unverified"


def test_passing_phase_output_is_resolution_evidence(runtime):
    root = runtime.project_root
    (root / "issues/open.md").write_text("## Root Cause\nx\n", encoding="utf-8")
    (root / "out-pass.md").write_text('```json\n{"handoff_status": "pass"}\n```\n', encoding="utf-8")
    (root / "out-issue.md").write_text('```json\n{"handoff_status": "issue"}\n```\n', encoding="utf-8")
    runtime.wiki.propose(
        "P",
        [
            entry(
                sources=["issues/open.md"],
                evidence=[{"kind": "phase_output", "path": "out-pass.md"}],
            ),
            entry(
                "L02",
                sources=["issues/open.md"],
                evidence=[{"kind": "phase_output", "path": "out-issue.md"}],
            ),
        ],
    )
    runtime.wiki.approve("P", "L01", 1, "u")
    with pytest.raises(DomainError):
        runtime.wiki.approve("P", "L02", 1, "u")
    (item,) = runtime.wiki.active("P")
    assert item["approval"]["verification"]["resolved_by"] == ["out-pass.md"]


@pytest.mark.parametrize(
    "text",
    [
        # A real "issue" result followed by an example "pass" block (W-15).
        '```json\n{"handoff_status": "issue"}\n```\nExample:\n```json\n{"handoff_status": "pass"}\n```\n',
        # Not JSON at all.
        'handoff_status: "pass" and "handoff_status": "pass"\n',
        '```json\n{"handoff_status": "pass",}\n```\n',
        '```json\n{"handoff_status": "passed"}\n```\n',
    ],
)
def test_phase_output_needs_one_parseable_pass_block(runtime, text):
    root = runtime.project_root
    (root / "issues/open.md").write_text("## Root Cause\nx\n", encoding="utf-8")
    (root / "out.md").write_text(text, encoding="utf-8")
    runtime.wiki.propose(
        "P",
        entry(sources=["issues/open.md"], evidence=[{"kind": "phase_output", "path": "out.md"}]),
    )
    with pytest.raises(DomainError) as error:
        runtime.wiki.approve("P", "L01", 1, "u")
    assert error.value.code == ErrorCode.VERIFICATION_FAILED


def test_file_evidence_is_pinned_to_its_commit(runtime):
    root = runtime.project_root
    repo = root / "module"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@example.test")
    (repo / "Dto.java").write_text("@Size(min = 1)", encoding="utf-8")
    git(repo, "add", "Dto.java")
    git(repo, "commit", "-qm", "fix")
    commit = git(repo, "rev-parse", "HEAD")
    (root / "issues/open.md").write_text("## Root Cause\nx\n", encoding="utf-8")
    (root / "out-pass.md").write_text('```json\n{"handoff_status": "pass"}\n```\n', encoding="utf-8")
    code = [{"kind": "file", "path": "module/Dto.java"}]
    # A committed file alone is where a fix lives, not proof that it works (W-17).
    runtime.wiki.propose("P", entry("L09", sources=["issues/open.md"], evidence=code))
    with pytest.raises(DomainError):
        runtime.wiki.approve("P", "L09", 1, "u")
    evidence = [*code, {"kind": "phase_output", "path": "out-pass.md"}]
    runtime.wiki.propose("P", entry(sources=["issues/open.md"], evidence=evidence))
    runtime.wiki.approve("P", "L01", 1, "u")
    (item,) = runtime.wiki.active("P")
    assert [s.get("commit") for s in item["sources"]] == [None, commit, None]
    # Later code changes do not invalidate evidence pinned to the fixing commit.
    (repo / "Dto.java").write_text("refactored", encoding="utf-8")
    git(repo, "commit", "-qam", "later")
    assert [i["item_id"] for i in runtime.wiki.active("P")] == ["L01"]
    runtime.wiki.render("P")
    assert f"`module/Dto.java`@{commit[:12]}" in (root / "docs/wiki/lessons.md").read_text(encoding="utf-8")
    # Uncommitted bytes cannot be pinned.
    (repo / "Dto.java").write_text("dirty", encoding="utf-8")
    with pytest.raises(DomainError) as error:
        runtime.wiki.propose("P", entry("L02", sources=["issues/open.md"], evidence=evidence))
    assert error.value.code == ErrorCode.INVALID_INPUT


def test_bundle_records_why_items_are_not_served(runtime):
    root = runtime.project_root
    for name in ("a", "b", "c", "d"):
        (root / f"issues/{name}.md").write_text(RESOLVED_ISSUE.format(name), encoding="utf-8")
    wiki = runtime.wiki
    wiki.propose(
        "P",
        [entry(f"L0{n}", sources=[f"issues/{name}.md"]) for n, name in enumerate("abcd", 1)],
    )
    wiki.approve("P", "L01", 1, "u")
    wiki.reject("P", "L02", 1, "u")
    wiki.approve("P", "L03", 1, "u")
    wiki.retire("P", "L03", "obsolete", "u")
    wiki.approve("P", "L04", 1, "u")
    (root / "issues/d.md").write_text("edited", encoding="utf-8")
    wiki.revise("P", "L01", entry("L01", check="개정안.", sources=["issues/a.md"]))
    result = wiki.bundle("P", graph({"id": "dba", "role_id": "dba"}))
    assert result["selection"] == {"dba": ["L01 r1"]}
    assert result["inactive"] == [
        {"id": "L01 r2", "reason": "not_approved"},
        {"id": "L02 r1", "reason": "rejected"},
        {"id": "L03 r1", "reason": "retired"},
        {"id": "L04 r1", "reason": "source_changed"},
    ]


def test_items_approved_without_resolution_stop_being_served(runtime):
    """Revisions approved before the resolution check are re-verified on every read."""
    (runtime.project_root / "issues/open.md").write_text("## Root Cause\nx\n", encoding="utf-8")
    runtime.wiki.propose("P", entry(sources=["issues/open.md"]))
    with runtime.store.db.transaction() as sql:
        sql.execute(
            "UPDATE knowledge_revisions SET state='active',"
            "approval_json=json_object('state','approved','actor','u','body_digest',body_digest)"
        )
    assert runtime.wiki.active("P") == []
    result = runtime.wiki.render("P")
    assert result["stale"] == ["L01"]
    assert "최종 해결 증거 없음" in (runtime.project_root / "docs/wiki/index.md").read_text(encoding="utf-8")


def test_render_belongs_to_the_workspace_project(runtime, tmp_path):
    runtime.store.register_project("OTHER", "Other", {})
    runtime.wiki.propose("P", entry())
    runtime.wiki.approve("P", "L01", 1, "u")
    runtime.wiki.render("P")
    with pytest.raises(DomainError) as error:
        runtime.wiki.render("OTHER")
    assert error.value.code == ErrorCode.POLICY_VIOLATION
    assert "L01 r1" in (runtime.project_root / "docs/wiki/lessons.md").read_text(encoding="utf-8")
    # Without settings or a marker the folder name decides, as in project setup.
    root = tmp_path / "My Project"
    (root / "config").mkdir(parents=True)
    (root / "config/runtime.json").write_text("{}", encoding="utf-8")
    assert WorkflowRuntime(root).wiki.workspace_project() == "My-Project"


@pytest.mark.parametrize("name", ["a`b.md", "a|b.md"])
def test_paths_that_break_markdown_are_rejected(runtime, name):
    (runtime.project_root / name).write_text(RESOLVED_ISSUE.format("x"), encoding="utf-8")
    with pytest.raises(DomainError):
        runtime.wiki.propose("P", entry(sources=[name]))


def test_bundle_scopes_items_by_module_and_ranks_importance(runtime):
    approve_all(
        runtime.wiki,
        [
            entry("L01", roles=["qa"], importance="low"),
            entry("L02", roles=["qa"], importance="high"),
            entry("L03", roles=["qa"], modules=["backend"]),
            entry("L04", roles=["qa"], modules=["frontend"]),
        ],
    )
    nodes = graph(
        {"id": "qa-be", "role_id": "qa", "module": "backend"},
        {"id": "qa-task", "role_id": "qa"},
        {"id": "qa-any", "role_id": "qa"},
    )
    result = runtime.wiki.bundle("P", nodes, module="frontend")
    # Module-specific first, then importance; other modules are recorded, not silently lost.
    assert result["selection"]["qa-be"] == ["L03 r1", "L02 r1", "L01 r1"]
    assert result["excluded"]["qa-be"] == [{"id": "L04 r1", "reason": "other_module"}]
    assert result["selection"]["qa-task"] == ["L04 r1", "L02 r1", "L01 r1"]
    # Without any module every item is eligible.
    unscoped = runtime.wiki.bundle("P", graph({"id": "qa", "role_id": "qa"}))
    assert unscoped["selection"]["qa"] == ["L02 r1", "L03 r1", "L04 r1", "L01 r1"]


def test_custom_roles_can_be_targeted(runtime):
    custom = "custom-code-migration-bot"
    approve_all(runtime.wiki, [entry("L01", roles=[custom]), entry("L02", roles=["all"])])
    result = runtime.wiki.bundle(
        "P",
        graph({"id": "bot", "role_id": custom}, {"id": "other", "role_id": "custom-readonly-x"}),
    )
    assert result["selection"] == {"bot": ["L01 r1", "L02 r1"], "other": ["L02 r1"]}
    (item,) = [i for i in runtime.wiki.items("P") if i["item_id"] == "L01"]
    assert item["content"]["roles"] == [custom]


@pytest.mark.parametrize(
    "overrides",
    [{"importance": "urgent"}, {"modules": ["bad module"]}, {"roles": ["custom-x"]}],
)
def test_invalid_scope_metadata_is_rejected(runtime, overrides):
    with pytest.raises(DomainError):
        runtime.wiki.propose("P", entry(**overrides))


def test_secrets_never_reach_bundle_prompt_or_pages(runtime):
    secret = "s3cr3t-token-value-123"
    runtime.store.secrets = (secret,)
    runtime.wiki.propose("P", entry(check=f"토큰 {secret} 사용 금지", condition=secret))
    runtime.wiki.approve("P", "L01", 1, "u")
    bundle = runtime.wiki.bundle("P", graph({"id": "dba", "role_id": "dba"}))
    from agent_dev.adapters.wiki_knowledge import prompt_items

    runtime.wiki.render("P")
    pages = "".join(p.read_text(encoding="utf-8") for p in (runtime.project_root / "docs/wiki").glob("*.md"))
    dumped = json.dumps([bundle, prompt_items({"wiki": bundle}, "dba")], ensure_ascii=False)
    assert secret not in dumped and secret not in pages


def test_pipes_in_text_cells_are_escaped(runtime):
    runtime.wiki.propose("P", entry(title="a|b", condition="c|d"))
    runtime.wiki.approve("P", "L01", 1, "u")
    runtime.wiki.render("P")
    row = next(
        line
        for line in (runtime.project_root / "docs/wiki/lessons.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("| L01")
    )
    assert "a\\|b" in row and "c\\|d" in row
    assert row.count(" | ") == 4  # five columns stay five columns


def committed_repo(root, name="module", text="@Size(min = 1)"):
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "T")
    git(repo, "config", "user.email", "t@example.test")
    (repo / "Dto.java").write_text(text, encoding="utf-8")
    git(repo, "add", "Dto.java")
    git(repo, "commit", "-qm", "fix")
    return repo


def approved_code_item(runtime):
    root = runtime.project_root
    committed_repo(root)
    (root / "issues/open.md").write_text("## Root Cause\nx\n", encoding="utf-8")
    (root / "out-pass.md").write_text('```json\n{"handoff_status": "pass"}\n```\n', encoding="utf-8")
    evidence = [
        {"kind": "file", "path": "module/Dto.java"},
        {"kind": "phase_output", "path": "out-pass.md"},
    ]
    runtime.wiki.propose("P", entry(sources=["issues/open.md"], evidence=evidence))
    runtime.wiki.approve("P", "L01", 1, "u")
    assert len(runtime.wiki.active("P")) == 1


def tamper_sources(runtime, change):
    row = runtime.store.db.rows("SELECT id,sources_json FROM knowledge_revisions")[0]
    sources = json.loads(row["sources_json"])
    change(sources)
    with runtime.store.db.transaction() as sql:
        sql.execute(
            "UPDATE knowledge_revisions SET sources_json=? WHERE id=?",
            (json.dumps(sources), row["id"]),
        )


def test_tampered_commit_is_never_passed_to_git(runtime, tmp_path):
    """W-14: a restored or edited record cannot inject git options."""
    approved_code_item(runtime)
    target = tmp_path / "pwned"

    def inject(sources):
        sources[1]["commit"] = f"--output={target}"

    tamper_sources(runtime, inject)
    assert runtime.wiki.active("P") == []
    assert runtime.wiki.inactive("P") == [{"id": "L01 r1", "reason": "source_changed"}]
    assert not list(tmp_path.glob("pwned*"))


@pytest.mark.parametrize(
    "change",
    [
        lambda s: s.__setitem__(0, "issues/open.md"),
        lambda s: s[0].__setitem__("kind", "shell"),
        lambda s: s[0].pop("digest"),
        lambda s: s[0].__setitem__("path", "/etc/hosts"),
    ],
)
def test_malformed_records_are_not_served(runtime, change):
    approved_code_item(runtime)
    tamper_sources(runtime, change)
    assert runtime.wiki.active("P") == []


def test_pinned_file_digest_is_checked_and_replace_refs_ignored(runtime):
    """W-19: the committed bytes must still match the recorded SHA-256."""
    approved_code_item(runtime)
    repo = runtime.project_root / "module"
    commit = git(repo, "rev-parse", "HEAD")
    fake = git(repo, "hash-object", "-w", "--stdin", input_text="forged")
    original = git(repo, "rev-parse", f"{commit}:Dto.java")
    git(repo, "replace", original, fake)
    # Replacement refs are ignored, so the pinned commit still shows the approved bytes.
    assert len(runtime.wiki.active("P")) == 1
    tamper_sources(runtime, lambda s: s[1].__setitem__("digest", "0" * 64))
    assert runtime.wiki.active("P") == []


@pytest.mark.parametrize(
    "resolution",
    [
        "- Resolved: trueish\n",
        "Resolution: accepted_pending\n",
        "```\n- Resolved: true\n```\n",
        "- Resolved: true-ish\n",
    ],
)
def test_resolution_values_must_match_exactly(runtime, resolution):
    """W-16: look-alike values and examples in code blocks are not resolutions."""
    (runtime.project_root / "issues/x.md").write_text(
        f"## Root Cause\nx\n\n## Resolution\n{resolution}", encoding="utf-8"
    )
    runtime.wiki.propose("P", entry(sources=["issues/x.md"]))
    with pytest.raises(DomainError):
        runtime.wiki.approve("P", "L01", 1, "u")


def test_superseded_issue_resolves_only_through_a_resolved_successor(runtime):
    root = runtime.project_root
    (root / "issues/issue-001.md").write_text(
        "## Root Cause\nx\n\n## Resolution\n- Resolved: true\n- 최신 `issue-003.md`로 대체되었습니다.\n", encoding="utf-8"
    )
    (root / "issues/issue-003.md").write_text("## Root Cause\nx\n", encoding="utf-8")
    wiki = runtime.wiki
    wiki.propose(
        "P",
        [
            entry("L01", sources=["issues/issue-001.md"]),
            entry("L02", sources=["issues/issue-001.md", "issues/issue-003.md"]),
        ],
    )
    for item_id in ("L01", "L02"):
        with pytest.raises(DomainError):
            wiki.approve("P", item_id, 1, "u")
    (root / "issues/issue-003.md").write_text(RESOLVED_ISSUE.format("fixed"), encoding="utf-8")
    wiki.revise("P", "L02", entry("L02", sources=["issues/issue-001.md", "issues/issue-003.md"]))
    wiki.approve("P", "L02", 2, "u")
    (item,) = wiki.active("P")
    assert item["approval"]["verification"]["resolved_by"] == [
        "issues/issue-001.md",
        "issues/issue-003.md",
    ]


def test_secrets_registered_after_approval_are_masked_on_output(runtime):
    """W-18: masking happens when knowledge leaves the store, not only on entry."""
    value = "late-registered-secret-42"
    runtime.wiki.propose("P", entry(check=f"use {value} here"))
    runtime.wiki.approve("P", "L01", 1, "u")
    runtime.store.secrets = (value,)
    bundle = runtime.wiki.bundle("P", graph({"id": "dba", "role_id": "dba"}))
    runtime.wiki.render("P")
    pages = "".join(p.read_text(encoding="utf-8") for p in (runtime.project_root / "docs/wiki").glob("*.md"))
    assert value not in json.dumps(bundle, ensure_ascii=False) and value not in pages


def test_stored_body_must_match_its_digest(runtime):
    """W-20: masking on save is part of the digest; a changed body is not served."""
    runtime.store.secrets = ("migration",)
    runtime.wiki.propose("P", entry(tags=["migration"]))
    runtime.wiki.approve("P", "L01", 1, "u")
    (item,) = runtime.wiki.active("P")
    assert "migration" not in json.dumps(item["content"])
    with runtime.store.db.transaction() as sql:
        sql.execute(
            "UPDATE knowledge_revisions SET content_json=json_set(content_json,'$.check','edited')"
        )
    assert runtime.wiki.active("P") == []
    assert runtime.wiki.inactive("P") == [{"id": "L01 r1", "reason": "body_mismatch"}]


def test_session_budget_excludes_new_items_when_the_union_is_full(runtime, monkeypatch):
    """W-09: the session union budget actually removes items, with its reason."""
    from agent_dev.adapters import wiki_knowledge

    approve_all(
        runtime.wiki,
        [entry(f"L0{n}", roles=[role]) for n, role in enumerate(("qa", "dba", "security"), 1)],
    )
    size = wiki_knowledge._size(runtime.wiki.active("P")[0])
    monkeypatch.setitem(wiki_knowledge.POLICY, "session_chars", size * 2)
    result = runtime.wiki.bundle(
        "P",
        graph(
            {"id": "qa", "role_id": "qa", "session": "verify"},
            {"id": "dba", "role_id": "dba", "session": "verify"},
            {"id": "security", "role_id": "security", "session": "verify"},
        ),
    )
    assert result["selection"] == {"qa": ["L01 r1"], "dba": ["L02 r1"], "security": []}
    assert result["excluded"]["security"] == [{"id": "L03 r1", "reason": "session_char_limit"}]


@pytest.mark.parametrize(
    ("report", "passes"),
    [
        ('<testsuite tests="3" failures="0" errors="0" skipped="0"/>', True),
        ('<testsuites><testsuite tests="2" failures="0" errors="0"/></testsuites>', True),
        ('<testsuite tests="2" failures="1" errors="0"/>', False),
        ('<testsuite tests="1" failures="0" errors="0" skipped="1"/>', False),
        ('<testsuite tests="0" failures="0" errors="0"/>', False),
        ('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><testsuite tests="1"/>', False),
        ("not xml", False),
    ],
)
def test_junit_report_is_test_evidence(runtime, report, passes):
    root = runtime.project_root
    committed_repo(root)
    (root / "issues/open.md").write_text("## Root Cause\nx\n", encoding="utf-8")
    (root / "TEST-report.xml").write_text(report, encoding="utf-8")
    evidence = [
        {"kind": "file", "path": "module/Dto.java"},
        {"kind": "test_report", "path": "TEST-report.xml"},
    ]
    runtime.wiki.propose("P", entry(sources=["issues/open.md"], evidence=evidence))
    if passes:
        runtime.wiki.approve("P", "L01", 1, "u")
        assert runtime.wiki.active("P")[0]["approval"]["verification"]["resolved_by"] == [
            "TEST-report.xml"
        ]
    else:
        with pytest.raises(DomainError):
            runtime.wiki.approve("P", "L01", 1, "u")
