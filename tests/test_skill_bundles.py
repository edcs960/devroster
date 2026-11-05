"""Project skill bundles: catalog, selection, verified cache, repository install, prompts."""

import json
import subprocess
from pathlib import Path

import pytest

from agent_dev import defaults
from agent_dev.adapters import skill_store
from agent_dev.adapters.skill_store import (
    MARKER,
    SkillStore,
    catalog,
    detect,
    frontmatter,
    prompt_skills,
    tree_digest,
)
from agent_dev.domain.errors import DomainError
from agent_dev.domain.role_policy import ROLES
from agent_dev.domain.skill_selection import (
    NODE_SKILLS,
    apply_limits,
    repository_skills,
    role_candidates,
    suggest,
    validate,
)


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def skill_folder(path: Path, name: str, description="Does a thing.", extra=None):
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n")
    for relative, text in (extra or {}).items():
        (path / relative).parent.mkdir(parents=True, exist_ok=True)
        (path / relative).write_text(text)
    return path


@pytest.fixture
def source_repo(tmp_path):
    """A local git repository standing in for a pinned upstream skill source."""
    repo = tmp_path / "upstream"
    skill_folder(repo / "skills" / "alpha", "alpha", extra={"references/a.md": "A"})
    skill_folder(repo / "skills" / "beta", "beta", "Beta skill.")
    git("init", "-q", cwd=repo)
    git("add", ".", cwd=repo)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=repo)
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    return repo, commit


def fixture_catalog(repo, commit):
    return {
        "bundles": {"core": {"description": "core"}, "web-react": {"description": "web"}},
        "sources": {"up": {"repo": str(repo), "commit": commit, "license": "MIT"}},
        "skills": {
            "alpha": {
                "source": "up",
                "path": "skills/alpha",
                "bundles": ["core"],
                "roles": ["planner", "developer"],
                "tree_digest": tree_digest(repo / "skills" / "alpha"),
            },
            "beta": {
                "source": "up",
                "path": "skills/beta",
                "bundles": ["web-react"],
                "roles": ["developer-frontend"],
                "tree_digest": tree_digest(repo / "skills" / "beta"),
            },
        },
    }


# Catalog ------------------------------------------------------------------------------


def test_bundled_catalog_is_pinned_and_uses_known_roles():
    data = catalog()
    assert {"core", "web-react", "spring", "postgres", "container", "mobile-ios", "mobile-android"} <= set(
        data["bundles"]
    )
    for name, skill in data["skills"].items():
        assert set(skill["roles"]) <= ROLES, name
        source = data["sources"][skill["source"]]
        if source.get("local"):
            assert (defaults.DEFAULTS_DIR / skill["path"] / "SKILL.md").is_file(), name
        else:
            assert len(source["commit"]) == 40 and len(skill["tree_digest"]) == 64, name
    assert data["skills"]["dev-pipeline"]["roles"] == []  # Interactive entry only.


def test_bundled_local_skills_are_generic_and_do_not_fetch():
    pipeline = (defaults.DEFAULTS_DIR / "skills/dev-pipeline/SKILL.md").read_text()
    assert "AIAG" not in pipeline and "FMEA 산출물" not in pipeline
    guidelines = (defaults.DEFAULTS_DIR / "skill_catalog/web-design-guidelines/SKILL.md").read_text()
    assert "raw.githubusercontent" not in guidelines and "WebFetch" not in guidelines
    assert (defaults.DEFAULTS_DIR / "skill_catalog/web-design-guidelines/LICENSE").is_file()


def test_frontmatter_reads_folded_and_quoted_values():
    text = '---\nname: "x"\ndescription: >-\n  first line\n  second line\nother: 1\n---\nbody'
    assert frontmatter(text) == {"name": "x", "description": "first line second line", "other": "1"}


# Selection ----------------------------------------------------------------------------


def test_validate_rejects_unknown_bundle_skill_and_module(source_repo):
    data = fixture_catalog(*source_repo)
    with pytest.raises(DomainError):
        validate(data, {"bundles": ["nope"]})
    with pytest.raises(DomainError):
        validate(data, {"exclude": ["nope"]})
    with pytest.raises(DomainError):
        validate(data, {"repositories": {"ghost": {"bundles": []}}}, modules={"frontend"})
    assert validate(data, {}) == {}


def test_role_candidates_follow_repository_bundles_roles_and_exclude(source_repo):
    data = fixture_catalog(*source_repo)
    settings = validate(
        data,
        {"bundles": ["core"], "repositories": {"frontend": {"bundles": ["web-react"]}}},
        modules={"frontend", "backend"},
    )
    assert role_candidates(data, settings, "developer-frontend", "frontend") == ["beta"]
    assert role_candidates(data, settings, "developer-frontend", "backend") == []
    assert role_candidates(data, settings, "planner", "backend") == ["alpha"]
    extra = dict(settings, roles={"custom:perf": ["beta"]}, exclude=["alpha"])
    assert role_candidates(data, extra, "custom:perf") == ["beta"]
    assert role_candidates(data, extra, "planner") == []
    assert repository_skills(data, settings, "frontend") == ["alpha", "beta"]


def test_apply_limits_caps_count_and_description_size():
    names = [f"s{i}" for i in range(NODE_SKILLS + 2)]
    kept, warnings = apply_limits("qa", names)
    assert len(kept) == NODE_SKILLS and len(warnings) == 2
    kept, warnings = apply_limits("qa", ["a", "b"], {"a": "x" * 3000, "b": "y" * 3000})
    assert kept == ["a"] and "b omitted" in warnings[0]


def test_detect_and_suggest_bundles(tmp_path):
    repo = tmp_path / "app"
    (repo / "web").mkdir(parents=True)
    (repo / "web" / "package.json").write_text('{"dependencies": {"react": "19"}}')
    (repo / "build.gradle").write_text("id 'org.springframework.boot'\nruntimeOnly 'org.postgresql:postgresql'")
    (repo / "Dockerfile").write_text("FROM x")
    markers = detect(repo)
    assert markers == ["docker", "postgres", "react", "spring-boot"]
    assert suggest(markers) == ["core", "container", "postgres", "web-react", "spring"]


# Verified cache -----------------------------------------------------------------------


def test_ensure_fetches_pinned_commit_verifies_and_stores_read_only(tmp_path, source_repo):
    store = SkillStore(tmp_path / "cache", fixture_catalog(*source_repo))
    result = store.ensure(["alpha", "beta"])
    assert result["failed"] == {}
    path = result["ready"]["alpha"]
    assert (path / "references" / "a.md").read_text() == "A"
    assert not (path / "SKILL.md").stat().st_mode & 0o222
    assert store.cached("alpha") == path
    assert store.description("beta") == "Beta skill."
    assert not list((tmp_path / "cache").glob(".source-*"))


def test_ensure_refuses_digest_mismatch_and_tampered_cache(tmp_path, source_repo):
    data = fixture_catalog(*source_repo)
    data["skills"]["beta"]["tree_digest"] = "0" * 64
    store = SkillStore(tmp_path / "cache", data)
    result = store.ensure(["alpha", "beta"])
    assert "digest mismatch" in result["failed"]["beta"]
    path = result["ready"]["alpha"]
    path.chmod(0o755)
    (path / "SKILL.md").chmod(0o644)
    (path / "SKILL.md").write_text("tampered")
    assert store.cached("alpha") is None


def test_tree_digest_refuses_links_and_honours_exclude(tmp_path):
    folder = skill_folder(tmp_path / "s", "s")
    base = tree_digest(folder)
    (folder / "skills").mkdir()
    (folder / "skills" / "copy").symlink_to(folder / "SKILL.md")
    with pytest.raises(DomainError):
        tree_digest(folder)
    assert tree_digest(folder, exclude=("skills",)) == base


# Repository install -------------------------------------------------------------------


@pytest.fixture
def target_repo(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    return repo


def test_sync_installs_links_excludes_and_prunes(tmp_path, source_repo, target_repo):
    store = SkillStore(tmp_path / "cache", fixture_catalog(*source_repo))
    report = store.sync_repository(target_repo, ["alpha", "beta"])
    assert report["installed"] == ["alpha", "beta"] and report["conflicts"] == []
    agents = target_repo / ".agents" / "skills"
    assert json.loads((agents / "alpha" / MARKER).read_text())["managed_by"] == "devroster"
    link = target_repo / ".claude" / "skills" / "alpha"
    assert link.is_symlink() and (link / "SKILL.md").is_file()
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=target_repo, capture_output=True, text=True, check=True
    )
    assert status.stdout == ""
    assert store.status(target_repo, ["alpha", "beta"]) == {"alpha": "installed", "beta": "installed"}
    again = store.sync_repository(target_repo, ["alpha"])
    assert again["unchanged"] == ["alpha"] and again["removed"] == ["beta"]
    assert not (agents / "beta").exists() and not (target_repo / ".claude/skills/beta").exists()
    exclude = (target_repo / ".git" / "info" / "exclude").read_text()
    assert "/.agents/skills/alpha/" in exclude and "beta" not in exclude


def test_sync_never_replaces_a_skill_it_does_not_manage(tmp_path, source_repo, target_repo):
    own = skill_folder(target_repo / ".agents" / "skills" / "alpha", "alpha", "mine")
    store = SkillStore(tmp_path / "cache", fixture_catalog(*source_repo))
    report = store.sync_repository(target_repo, ["alpha"])
    assert report["conflicts"] == ["alpha"]
    assert "mine" in (own / "SKILL.md").read_text()
    assert store.status(target_repo, ["alpha"]) == {"alpha": "conflict"}


# Snapshot and prompts -----------------------------------------------------------------


@pytest.fixture
def patched_store(tmp_path, monkeypatch, source_repo):
    data = fixture_catalog(*source_repo)
    monkeypatch.setattr(skill_store, "catalog", lambda: data)
    monkeypatch.setattr(skill_store, "cache_root", lambda: tmp_path / "cache")
    return SkillStore(tmp_path / "cache", data)


def test_approved_skills_freezes_cached_skills_per_node(patched_store):
    from agent_dev.adapters.workflow_runtime import approved_skills

    patched_store.ensure(["alpha"])  # beta selected but not synced
    resolved = {
        "skills": {"bundles": ["core"], "repositories": {"frontend": {"bundles": ["web-react"]}}},
        "repositories": {"frontend": {}},
        "workflow_graph": {
            "nodes": [
                {"id": "plan", "kind": "role", "role_id": "planner"},
                {"id": "fe", "kind": "role", "role_id": "developer-frontend", "module": "frontend"},
                {"id": "gate", "kind": "verification"},
            ]
        },
    }
    bundle = approved_skills(resolved)
    assert bundle["nodes"] == {"plan": ["alpha"], "fe": []}
    assert set(bundle["items"]) == {"alpha"}
    assert any("beta is not synced" in w for w in bundle["warnings"])
    resolved["approved_skills"] = bundle
    assert prompt_skills(resolved, "plan")[0]["name"] == "alpha"
    assert approved_skills({"skills": {}}) == {}


def test_prompt_skills_drops_altered_cache(patched_store):
    from agent_dev.adapters.workflow_runtime import approved_skills

    patched_store.ensure(["alpha"])
    resolved = {
        "skills": {"bundles": ["core"]},
        "repositories": {},
        "workflow_graph": {"nodes": [{"id": "plan", "kind": "role", "role_id": "planner"}]},
    }
    resolved["approved_skills"] = approved_skills(resolved)
    path = Path(resolved["approved_skills"]["items"]["alpha"]["path"])
    path.chmod(0o755)
    (path / "extra.md").write_text("injected")
    assert prompt_skills(resolved, "plan") == []




def test_runner_reads_only_cache_folders(tmp_path, patched_store):
    from agent_dev.adapters.cli_runner import CliRunnerAdapter

    path = patched_store.ensure(["alpha"])["ready"]["alpha"]
    claude = CliRunnerAdapter("claude_cli", {"skill_dirs": [str(path)]}, tmp_path)
    assert path.resolve() in claude.runner.add_dirs
    codex = CliRunnerAdapter("codex_cli", {"skill_dirs": [str(path)]}, tmp_path)
    assert path.resolve() not in codex.runner.add_dirs
    with pytest.raises(DomainError):
        CliRunnerAdapter("claude_cli", {"skill_dirs": [str(tmp_path)]}, tmp_path)


def test_setup_keeps_module_choices_and_stores_them_per_repository(tmp_path, monkeypatch):
    from agent_dev.adapters.project_setup import ProjectSetup
    from agent_dev.adapters.skill_cli import suggestions

    monkeypatch.setenv("DEVROSTER_HOME", str(tmp_path / "home"))
    project = tmp_path / "mono"
    (project / "web").mkdir(parents=True)
    (project / "api").mkdir()
    (project / "web" / "package.json").write_text('{"dependencies": {"react": "19"}}')
    (project / "api" / "build.gradle").write_text("id 'org.springframework.boot'")
    git("init", "-q", cwd=project)
    git("add", ".", cwd=project)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=project)
    setup = ProjectSetup(project)
    value = setup.read()
    gate, module = dict(value["verification"][0]), dict(value["modules"][0])
    value["modules"] = [
        dict(module, id="frontend", path="web", allowed_paths=["web"]),
        dict(module, id="backend", path="api", allowed_paths=["api"]),
    ]
    value["verification"] = [dict(gate, repository="frontend", module="frontend")]
    value["skills"] = {
        "bundles": ["core"],
        "repositories": {"frontend": {"bundles": ["web-react"]}, "backend": {"bundles": ["spring"]}},
    }
    setup.save(value)
    profile = setup.current_profile()[2]
    # Both modules live in one git repository, the unit skills are installed into.
    assert profile["skills"]["repositories"] == {"frontend": {"bundles": ["web-react", "spring"]}}
    assert setup.read()["skills"]["repositories"]["backend"] == {"bundles": ["spring"]}
    found = suggestions(project)
    assert found["frontend"]["suggested"] == ["core", "web-react"]
    assert found["backend"]["suggested"] == ["core", "spring"]
    with pytest.raises(DomainError):
        setup.save(dict(setup.read(), skills={"repositories": {"ghost": {"bundles": ["core"]}}}))
    # Modules still unsaved on the setup screen get suggestions under their own names.
    unsaved = suggestions(project, [{"id": "web", "path": "web"}, {"id": "", "path": "api"}])
    assert list(unsaved) == ["web"] and unsaved["web"]["suggested"] == ["core", "web-react"]
    with pytest.raises(DomainError):
        suggestions(project, [{"id": "web"}])




