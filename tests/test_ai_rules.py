"""Project AI rules (.ai/rules): read, freeze into the snapshot, send to every runner, sync files."""

import json
import os
import subprocess
import sys

import pytest
from test_reusable_workspace import approved, workspace  # noqa: F401 - fixture

from agent_dev.adapters import ai_rules
from agent_dev.domain.errors import DomainError

RULE = "# 기술\n\n- React 18 + TypeScript\n- 화면 문구는 한국어\n"


def write_rule(root, name, text):
    path = root / ".ai/rules" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_rules_are_read_in_path_order_and_empty_files_skipped(tmp_path):
    write_rule(tmp_path, "b.md", "B rule")
    write_rule(tmp_path, "a.md", "A rule")
    write_rule(tmp_path, "empty.md", "  \n")
    write_rule(tmp_path, "nested/c.md", "C rule")
    bundle = ai_rules.read_rules(tmp_path)
    assert [i["path"] for i in bundle["items"]] == [
        ".ai/rules/a.md",
        ".ai/rules/b.md",
        ".ai/rules/nested/c.md",
    ]
    assert ai_rules.rules_text(bundle).startswith("# `.ai/rules/a.md`\n\nA rule")
    assert ai_rules.read_rules(tmp_path / "missing")["items"] == []


@pytest.mark.parametrize(
    "setup",
    [
        lambda r: write_rule(r, "big.md", "x" * (64 * 1024 + 1)),
        lambda r: write_rule(r, "secret.md", "token ghp_" + "a" * 30),
        lambda r: write_rule(r, "bin.md", "").write_bytes(b"\xff\xfe"),
        lambda r: (r / ".ai/rules").mkdir(parents=True)
        or os.symlink(r / "outside.md", r / ".ai/rules/link.md"),
    ],
    ids=["too-big", "secret", "not-utf8", "symlink"],
)
def test_unsafe_rules_are_refused(tmp_path, setup):
    (tmp_path / "outside.md").write_text("outside", encoding="utf-8")
    setup(tmp_path)
    with pytest.raises(DomainError):
        ai_rules.read_rules(tmp_path)


def test_total_size_warns_then_refuses(tmp_path):
    for n in range(3):
        write_rule(tmp_path, f"r{n}.md", "가" * 6000)  # 18 KB each
    assert ai_rules.read_rules(tmp_path)["warnings"]
    for n in range(3, 8):
        write_rule(tmp_path, f"r{n}.md", "가" * 6000)
    with pytest.raises(DomainError):
        ai_rules.read_rules(tmp_path)


def test_sync_keeps_user_text_and_check_detects_changes(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# 내 메모\n직접 쓴 내용\n", encoding="utf-8")
    assert ai_rules.sync(tmp_path)["files"] == [
        {"file": "CLAUDE.md", "state": "current"},
        {"file": "GEMINI.md", "state": "absent"},
        {"file": "AGENTS.md", "state": "absent"},
    ]  # No rules: nothing to add and no empty files created.
    write_rule(tmp_path, "stack.md", RULE)
    assert not ai_rules.sync(tmp_path, check=True)["ok"]
    result = ai_rules.sync(tmp_path)
    assert result["ok"] and {f["state"] for f in result["files"]} == {"updated", "created"}
    claude = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "@.ai/rules/stack.md" in claude and claude.endswith("# 내 메모\n직접 쓴 내용\n")
    assert "@.ai/rules/stack.md" in (tmp_path / "GEMINI.md").read_text(encoding="utf-8")
    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "React 18 + TypeScript" in agents and "@.ai" not in agents  # AGENTS.md inlines.
    assert ai_rules.sync(tmp_path, check=True)["ok"]
    # Text the user adds below the block survives the next sync.
    (tmp_path / "CLAUDE.md").write_text(claude + "추가 메모\n", encoding="utf-8")
    write_rule(tmp_path, "test.md", "npm test가 통과해야 완료")
    ai_rules.sync(tmp_path)
    claude = (tmp_path / "CLAUDE.md").read_text(encoding="utf-8")
    assert "@.ai/rules/test.md" in claude and claude.endswith("추가 메모\n")
    assert claude.count(ai_rules.END) == 1


def test_broken_markers_are_never_overwritten(tmp_path):
    write_rule(tmp_path, "stack.md", RULE)
    broken = ai_rules.BEGIN + "\n@.ai/rules/stack.md\n(끝 표시를 지움)\n"
    (tmp_path / "CLAUDE.md").write_text(broken, encoding="utf-8")
    result = ai_rules.sync(tmp_path)
    assert not result["ok"] and "CLAUDE.md" in result["stale"]
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8") == broken


def test_agents_md_over_codex_limit_warns(tmp_path):
    for n in range(3):
        write_rule(tmp_path, f"r{n}.md", "x" * 12000)
    agents = next(r for r in ai_rules.plan_sync(tmp_path) if r["file"] == "AGENTS.md")
    assert "32KiB" in agents["warning"]


def test_attach_and_remove_stay_inside_the_project(tmp_path):
    assert ai_rules.attach(tmp_path, "팀-규칙.md", RULE)["path"] == ".ai/rules/팀-규칙.md"
    assert (tmp_path / ".ai/README.md").is_file()
    with pytest.raises(DomainError):
        ai_rules.attach(tmp_path, "팀-규칙.md", RULE)  # Same name needs replace.
    ai_rules.attach(tmp_path, "팀-규칙.md", "바뀐 규칙", replace=True)
    for name in ("rule.txt", ".hidden.md", ""):
        with pytest.raises(DomainError):
            ai_rules.attach(tmp_path, name, RULE)
    # A path is reduced to its file name, so nothing is written outside .ai/rules.
    assert ai_rules.attach(tmp_path, "../escape.md", RULE)["path"] == ".ai/rules/escape.md"
    with pytest.raises(DomainError):
        ai_rules.attach(tmp_path, "secret.md", "sk-" + "a" * 30)
    assert ai_rules.remove(tmp_path, "팀-규칙.md")["removed"] == ".ai/rules/팀-규칙.md"
    with pytest.raises(DomainError):
        ai_rules.remove(tmp_path, "팀-규칙.md")


def test_snapshot_freezes_rules_and_every_prompt_gets_them(workspace):  # noqa: F811
    write_rule(workspace, "stack.md", RULE)
    runtime = approved(workspace)
    resolved = runtime.store.snapshot("T")["resolved"]
    assert resolved["project_rules"]["items"][0]["path"] == ".ai/rules/stack.md"
    context = json.loads(runtime.artifacts.context("T", "developer"))
    assert "React 18 + TypeScript" in context["project_rules"]
    assert "cannot expand permissions" in context["project_rules_rule"]
    # Editing the file later does not change the approved task.
    write_rule(workspace, "stack.md", "바뀐 규칙")
    assert "React 18" in json.loads(runtime.artifacts.context("T", "developer"))["project_rules"]


def test_without_rules_the_prompt_is_unchanged(workspace):  # noqa: F811
    runtime = approved(workspace)
    assert "project_rules" not in runtime.store.snapshot("T")["resolved"]
    assert "project_rules" not in json.loads(runtime.artifacts.context("T", "developer"))


def test_rules_cli_check_fails_until_synced(tmp_path):
    write_rule(tmp_path, "stack.md", RULE)

    def run(action):
        return subprocess.run(
            [sys.executable, "-m", "agent_dev.fa", "rules", action, "--root", str(tmp_path), "--json"],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=60,
            check=False,
        )

    assert run("check").returncode != 0
    assert run("sync").returncode == 0
    assert run("check").returncode == 0
    shown = json.loads(run("show").stdout)["result"]
    assert shown["items"][0]["path"] == ".ai/rules/stack.md" and "AGENTS.md" in shown["builtin"]


def test_setup_screen_attaches_lists_syncs_and_removes_rules(tmp_path):
    from test_project_setup import request, server

    with server(tmp_path) as base:
        token = json.loads(request(base, "/api/setup/bootstrap")[1])["token"]
        call = lambda path, payload: request(base, "/api/setup/" + path, payload, token)
        payload = {"root": str(tmp_path)}
        assert request(base, "/api/setup/rules", payload)[0] == 403  # Token required.
        status, data = call("rules", payload)
        assert status == 200 and json.loads(data)["items"] == []
        status, data = call("rules/attach", dict(payload, name="팀.md", content=RULE))
        assert status == 200 and json.loads(data)["items"][0]["path"] == ".ai/rules/팀.md"
        assert call("rules/attach", dict(payload, name="팀.md", content=RULE))[0] == 409
        assert call("rules/attach", dict(payload, name="x.md", content="sk-" + "a" * 30))[0] == 400
        status, data = call("rules/sync", payload)
        assert status == 200 and {f["state"] for f in json.loads(data)["files"]} == {"current"}
        assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8").count("React 18") == 1
        status, data = call("rules/remove", dict(payload, name="팀.md"))
        assert status == 200 and json.loads(data)["items"] == []


def test_rules_cli_writes_into_the_given_plain_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    result = subprocess.run(
        [sys.executable, "-m", "agent_dev.fa", "rules", "init", "--root", str(repo)],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert (repo / ".ai/README.md").is_file() and not (tmp_path / ".ai").exists()


def write_skill(root, name, description="화면을 만들 때", roles="", body="1. 순서대로 만든다"):
    folder = root / ".ai/skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    front = f"---\nname: {name}\ndescription: {description}\n" + (f"roles: {roles}\n" if roles else "") + "---\n"
    (folder / "SKILL.md").write_text(front + body + "\n", encoding="utf-8")
    return folder


def test_skills_read_roles_and_filter_by_role(tmp_path):
    write_skill(tmp_path, "component", roles="[developer, ux-ui]")
    write_skill(tmp_path, "release", description="배포 전 확인", roles="qa")
    write_skill(tmp_path, "glossary", description="용어")
    items = ai_rules.read_skills(tmp_path)["items"]
    assert items["component"]["roles"] == ["developer", "ux-ui"] and items["glossary"]["roles"] == []
    names = [s["name"] for s in ai_rules.skills_for({"items": items}, {"developer"})]
    assert names == ["component", "glossary"]  # Folder order; no roles = every role.
    assert all("instructions" in s for s in ai_rules.skills_for({"items": items}, {"qa"}))


def test_skill_prompt_budget_omits_bodies_beyond_the_limit(tmp_path):
    for n in range(3):
        write_skill(tmp_path, f"big-{n}", body="가" * 9000)  # ~27 KB each
    rows = ai_rules.skills_for(ai_rules.read_skills(tmp_path), {"developer"})
    assert "instructions" in rows[0] and "instructions_omitted" in rows[-1]


@pytest.mark.parametrize(
    "setup",
    [
        lambda r: write_skill(r, "Bad_Name"),
        lambda r: write_skill(r, "nodesc", description=""),
        lambda r: write_skill(r, "secret", body="ghp_" + "a" * 30),
        lambda r: write_skill(r, "huge", body="x" * (32 * 1024)),
        lambda r: os.symlink(r / "outside.md", write_skill(r, "linked") / "extra.md"),
        lambda r: (r / ".ai/skills/empty").mkdir(parents=True),
    ],
    ids=["name", "description", "secret", "size", "link-inside", "no-skill-md"],
)
def test_unsafe_skills_are_refused(tmp_path, setup):
    (tmp_path / "outside.md").write_text("outside", encoding="utf-8")
    setup(tmp_path)
    with pytest.raises(DomainError):
        ai_rules.read_skills(tmp_path)


def test_sync_links_skills_for_claude_code_and_codex(tmp_path):
    write_skill(tmp_path, "component")
    other = tmp_path / ".agents/skills/catalog-skill"
    other.mkdir(parents=True)  # A DevRoster catalog skill or the user's own: never touched.
    (other / "SKILL.md").write_text("---\ndescription: x\n---\n", encoding="utf-8")
    assert not ai_rules.sync(tmp_path, check=True)["ok"]
    assert ai_rules.sync(tmp_path)["ok"]
    for directory in ai_rules.SKILL_LINK_DIRS:
        link = tmp_path / directory / "component"
        assert (link / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert ai_rules.sync(tmp_path, check=True)["ok"] and other.is_dir()
    # A clashing folder that is not ours is reported, not replaced.
    clash = write_skill(tmp_path, "catalog-skill")
    result = ai_rules.sync(tmp_path)
    assert not result["ok"] and ".agents/skills/catalog-skill" in result["stale"]
    assert (other / "SKILL.md").read_text(encoding="utf-8") == "---\ndescription: x\n---\n"
    import shutil

    shutil.rmtree(clash)
    # Removing a project skill removes only its links.
    shutil.rmtree(tmp_path / ".ai/skills/component")
    ai_rules.sync(tmp_path)
    assert not (tmp_path / ".claude/skills/component").exists() and other.is_dir()


def test_snapshot_sends_each_role_only_its_project_skills(workspace):  # noqa: F811
    write_skill(workspace, "only-qa", description="검증 체크리스트", roles="[qa]")
    write_skill(workspace, "everyone", description="용어 사전")
    runtime = approved(workspace)
    assert set(runtime.store.snapshot("T")["resolved"]["project_skills"]["items"]) == {"everyone", "only-qa"}
    developer = json.loads(runtime.artifacts.context("T", "developer"))
    qa = json.loads(runtime.artifacts.context("T", "qa"))
    assert [s["name"] for s in developer["project_skills"]] == ["everyone"]
    assert [s["name"] for s in qa["project_skills"]] == ["everyone", "only-qa"]
    assert "never override" in qa["project_skills_rule"]


def test_studio_prompt_carries_project_rules_and_studio_skills(workspace):  # noqa: F811
    from agent_dev.adapters.studio_generation import project_guidance, prompt_for

    write_rule(workspace, "terms.md", "고객은 '회원'이라고 부른다")
    write_skill(workspace, "screen-copy", description="화면 문구", roles="[studio]")
    write_skill(workspace, "dev-only", description="개발 전용", roles="[developer]")
    guidance = project_guidance(workspace, "specs")
    assert "회원" in guidance["rules"] and [s["name"] for s in guidance["skills"]] == ["screen-copy"]
    prompt = prompt_for("specs", {"brief": "기획서", "project_guidance": guidance})
    rules_at, data_at = prompt.index("PROJECT RULES"), prompt.index("PRODUCT DATA")
    assert rules_at < data_at and "project_guidance" not in prompt[data_at:]
    assert "PROJECT RULES" not in prompt_for("specs", {"brief": "기획서"})


def test_skill_links_stay_out_of_git(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    write_skill(tmp_path, "component")
    ai_rules.sync(tmp_path)
    status = subprocess.run(
        ["git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True,
        text=True, encoding="utf-8", errors="replace",
        check=True,
    ).stdout
    assert ".ai/skills/component/SKILL.md" in status
    assert ".claude/skills" not in status and ".agents/skills" not in status
