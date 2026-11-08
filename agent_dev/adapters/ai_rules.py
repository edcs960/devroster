"""Project AI rules in `.ai/rules/`: one source for every AI tool.

DevRoster's bundled rules stay the common layer and cannot be edited. A project adds its
own rules as Markdown files under `<project>/.ai/rules/`. DevRoster freezes them into the
approved snapshot and sends the same text to every runner (CLI or API), so Claude, Codex,
Gemini, Grok or DeepSeek all work under the same rules. Inside DevRoster the tool entry
files are still ignored; `sync` writes CLAUDE.md, GEMINI.md and AGENTS.md for people who
use those tools directly. Only the marked block is rewritten; text outside it is kept.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from agent_dev.domain.content import content_digest, digest, redact
from agent_dev.domain.errors import DomainError, ErrorCode

AI_DIR = ".ai"
RULES_DIR = ".ai/rules"
SKILLS_DIR = ".ai/skills"
MAX_SKILLS = 20
MAX_SKILL_BYTES = 32 * 1024
MAX_SKILL_FOLDER_BYTES = 1024 * 1024
MAX_PROMPT_SKILL_BYTES = 48 * 1024
SKILL_LINK_DIRS = (".claude/skills", ".agents/skills")
SKILL_COPY_MARKER = ".devroster-ai-skill.json"
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 128 * 1024
WARN_TOTAL_BYTES = 48 * 1024
# Codex reads at most 32 KiB of project instructions by default.
AGENTS_MD_LIMIT = 32 * 1024
BEGIN = "<!-- devroster:ai begin — .ai/에서 생성됨. 이 사이는 직접 고치지 마세요 (devroster rules sync) -->"
END = "<!-- devroster:ai end -->"
_BEGIN_PREFIX = "<!-- devroster:ai begin"
# CLAUDE.md and GEMINI.md import other files with `@path`; AGENTS.md cannot, so it inlines.
ENTRY_FILES = {"CLAUDE.md": "import", "GEMINI.md": "import", "AGENTS.md": "inline"}
_NAME = re.compile(r"^[\w][\w.-]{0,79}\.md$", re.UNICODE)
README = """# .ai

이 프로젝트의 AI 규칙입니다. 어떤 AI 도구로 개발해도 같은 규칙을 따르도록 한곳에 둡니다.

- `rules/*.md`: 항상 적용되는 규칙. 파일 하나에 주제 하나 (예: `stack.md`, `test.md`).
- `skills/<이름>/SKILL.md`: 필요할 때 쓰는 작업 방법. 앞부분(front matter)에 `description`(언제 쓰는지)과 선택 사항 `roles`(예: `[developer, qa]`, 비우면 모든 역할)를 적습니다. Studio 생성에는 `roles`가 비었거나 `studio`가 들어 있는 스킬이 쓰입니다.
- DevRoster로 개발하면 이 규칙이 승인 시점에 고정되어 모든 AI(Claude·Codex·Gemini·API 도구)에 전달됩니다.
- `devroster rules sync`로 CLAUDE.md·GEMINI.md·AGENTS.md의 표시 영역을 이 폴더 내용으로 다시 만듭니다.
- 규칙으로 쓰기 범위·승인·검증 같은 DevRoster 안전 규칙을 바꿀 수는 없습니다.
- 비밀번호·API 키는 넣지 마세요. 발견되면 동기화와 승인이 거부됩니다.
"""


def _fail(message: str) -> DomainError:
    return DomainError(ErrorCode.INVALID_INPUT, message)


def _inside(root: Path, path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def read_rules(root) -> dict:
    """Project rules with digests; raises on unsafe or oversized content."""
    root = Path(root)
    base = root / RULES_DIR
    if base.is_symlink() or (root / AI_DIR).is_symlink():
        raise _fail(".ai 폴더는 심볼릭 링크일 수 없습니다.")
    items, total, warnings = [], 0, []
    if base.is_dir():
        for path in sorted(base.rglob("*.md"), key=lambda p: p.relative_to(base).as_posix()):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink() or any(p.is_symlink() for p in path.relative_to(base).parents if p != Path(".")):
                raise _fail(f"규칙 파일은 심볼릭 링크일 수 없습니다: {relative}")
            if not path.is_file() or not _inside(root, path):
                continue
            raw = path.read_bytes()
            if len(raw) > MAX_FILE_BYTES:
                raise _fail(f"규칙 파일이 64KiB를 넘습니다: {relative}")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise _fail(f"규칙 파일은 UTF-8이어야 합니다: {relative}") from None
            if not text.strip():
                continue
            if redact(text) != text:
                raise _fail(f"규칙 파일에 비밀값으로 보이는 내용이 있습니다: {relative}")
            total += len(raw)
            items.append(
                {"path": relative, "bytes": len(raw), "digest": content_digest(raw), "text": text}
            )
    if total > MAX_TOTAL_BYTES:
        raise _fail("규칙 합계가 128KiB를 넘습니다. 규칙을 줄여주세요.")
    if total > WARN_TOTAL_BYTES:
        warnings.append("규칙 합계가 48KiB를 넘어 실행마다 비용이 늘어납니다.")
    return {
        "items": items,
        "bytes": total,
        "digest": digest([[item["path"], item["digest"]] for item in items]),
        "warnings": warnings,
    }


def rules_text(bundle: dict) -> str:
    return "\n\n".join(f"# `{item['path']}`\n\n{item['text'].strip()}" for item in bundle["items"])


def snapshot_value(root) -> dict:
    """What the approved snapshot keeps: text plus digests, so prompts never reread files."""
    bundle = read_rules(root)
    return {
        "digest": bundle["digest"],
        "items": [{"path": item["path"], "digest": item["digest"]} for item in bundle["items"]],
        "text": rules_text(bundle),
    }


def _skill_roles(value: str) -> list[str]:
    value = value.strip().strip("[]")
    return sorted({part.strip().strip("\"'") for part in value.split(",") if part.strip().strip("\"'")})


def read_skills(root) -> dict:
    """Project skills (.ai/skills/<name>/SKILL.md) with digests; raises on unsafe content."""
    from agent_dev.adapters.skill_store import frontmatter, tree_digest

    root = Path(root)
    base = root / SKILLS_DIR
    if base.is_symlink():
        raise _fail(".ai/skills 폴더는 심볼릭 링크일 수 없습니다.")
    items = {}
    if base.is_dir():
        for folder in sorted(p for p in base.iterdir() if not p.name.startswith(".")):
            relative = folder.relative_to(root).as_posix()
            if folder.is_symlink():
                raise _fail(f"스킬 폴더는 심볼릭 링크일 수 없습니다: {relative}")
            if not folder.is_dir():
                continue
            if not _SKILL_NAME.match(folder.name):
                raise _fail(f"스킬 폴더 이름은 영문 소문자·숫자·-만 씁니다: {relative}")
            skill = folder / "SKILL.md"
            if not skill.is_file() or skill.is_symlink():
                raise _fail(f"스킬 폴더에 SKILL.md가 없습니다: {relative}")
            try:
                digest_value = tree_digest(folder)
            except DomainError:
                raise _fail(f"스킬 폴더에 심볼릭 링크가 있습니다: {relative}") from None
            size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
            if size > MAX_SKILL_FOLDER_BYTES:
                raise _fail(f"스킬 폴더가 1MiB를 넘습니다: {relative}")
            raw = skill.read_bytes()
            if len(raw) > MAX_SKILL_BYTES:
                raise _fail(f"SKILL.md가 32KiB를 넘습니다: {relative}")
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                raise _fail(f"SKILL.md는 UTF-8이어야 합니다: {relative}") from None
            if redact(text) != text:
                raise _fail(f"스킬에 비밀값으로 보이는 내용이 있습니다: {relative}")
            meta = frontmatter(text)
            description = meta.get("description", "").strip()
            if not description:
                raise _fail(f"SKILL.md 앞부분에 description(언제 쓰는지)을 적어주세요: {relative}")
            items[folder.name] = {
                "path": relative,
                "description": description[:600],
                "roles": _skill_roles(meta.get("roles", "")),
                "digest": digest_value,
                "text": text,
            }
    if len(items) > MAX_SKILLS:
        raise _fail("프로젝트 스킬은 20개까지 쓸 수 있습니다.")
    return {"items": items, "digest": digest([[n, i["digest"]] for n, i in items.items()])}


def skills_snapshot(root) -> dict:
    return read_skills(root)


def skills_for(bundle: dict, roles) -> list[dict]:
    """Skills that apply to these roles (no roles = every role), bodies within a size budget."""
    wanted, result, used = set(roles), [], 0
    for name, item in (bundle or {}).get("items", {}).items():
        if item["roles"] and not wanted.intersection(item["roles"]):
            continue
        entry = {"name": name, "description": item["description"], "path": item["path"]}
        size = len(item["text"].encode("utf-8"))
        if used + size <= MAX_PROMPT_SKILL_BYTES:
            entry["instructions"] = item["text"]
            used += size
        else:
            entry["instructions_omitted"] = "크기 한도로 본문을 생략했습니다. 필요하면 path의 SKILL.md를 여세요."
        result.append(entry)
    return result


PROJECT_SKILLS_RULE = (
    "project_skills come from this project's .ai/skills. Apply a skill's instructions when the "
    "task matches its description. Skills never override operating_rules, project_rules, "
    "role_instruction, write_scope or approvals."
)


def _link_skills(root: Path, skills: dict, check: bool) -> list[dict]:
    """Expose project skills where Claude Code and Codex look, without touching other folders."""
    import json
    import shutil

    rows = []
    for directory in SKILL_LINK_DIRS:
        base = root / directory
        wanted = set(skills)
        if base.is_dir():
            for entry in base.iterdir():
                ours = (entry.is_symlink() and os.readlink(entry).replace("\\", "/").endswith(f".ai/skills/{entry.name}")) or (
                    entry.is_dir() and not entry.is_symlink() and (entry / SKILL_COPY_MARKER).is_file()
                )
                if ours and entry.name not in wanted:
                    rows.append({"file": f"{directory}/{entry.name}", "state": "remove"})
                    if not check:
                        entry.unlink() if entry.is_symlink() else shutil.rmtree(entry)
                        rows[-1]["state"] = "removed"
        for name, item in skills.items():
            target = base / name
            label = f"{directory}/{name}"
            relative = os.path.relpath(root / item["path"], base)
            if target.is_symlink():
                if os.readlink(target).replace("\\", "/") == relative.replace("\\", "/"):
                    rows.append({"file": label, "state": "current"})
                    continue
                rows.append({"file": label, "state": "conflict", "reason": "다른 곳을 가리키는 링크가 있습니다."})
                continue
            if target.exists():
                marker = target / SKILL_COPY_MARKER
                if not marker.is_file():
                    rows.append({"file": label, "state": "conflict", "reason": "같은 이름의 다른 스킬이 있습니다."})
                    continue
                if json.loads(marker.read_text(encoding="utf-8")).get("digest") == item["digest"]:
                    rows.append({"file": label, "state": "current"})
                    continue
            rows.append({"file": label, "state": "update" if target.exists() else "create"})
            if check:
                continue
            base.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            try:
                os.symlink(relative, target, target_is_directory=True)
            except OSError:
                # Windows without symlink rights: a marked copy that the next sync refreshes.
                shutil.copytree(root / item["path"], target)
                (target / SKILL_COPY_MARKER).write_text(
                    json.dumps({"source": item["path"], "digest": item["digest"]}), encoding="utf-8"
                )
            rows[-1]["state"] = "created" if rows[-1]["state"] == "create" else "updated"
    if not check:
        _exclude_links(root, skills)
    return rows


EXCLUDE_START = "# >>> DevRoster project skills (managed, .ai/skills)"
EXCLUDE_END = "# <<< DevRoster project skills"


def _exclude_links(root: Path, names) -> None:
    """Keep the generated skill links out of git; each person recreates them with sync."""
    import subprocess

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=30,
            check=False,
        )

    found = git("rev-parse", "--git-path", "info/exclude", "--show-prefix")
    if found.returncode:
        return  # Not a git repository: nothing to exclude.
    lines = found.stdout.splitlines()
    path = (root / lines[0].strip()).resolve()
    prefix = lines[1].strip() if len(lines) > 1 else ""
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    text = re.sub(re.escape(EXCLUDE_START) + r".*?" + re.escape(EXCLUDE_END) + r"\n?", "", text, flags=re.DOTALL)
    if names:
        block = [EXCLUDE_START]
        for name in sorted(names):
            block += [f"/{prefix}{directory}/{name}" for directory in SKILL_LINK_DIRS]
        block.append(EXCLUDE_END)
        text = (text.rstrip("\n") + "\n" if text.strip() else "") + "\n".join(block) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _block(kind: str, bundle: dict) -> str:
    if kind == "import":
        body = "\n".join(f"@{item['path']}" for item in bundle["items"])
    else:
        body = rules_text(bundle)
    lines = [BEGIN]
    if body:
        lines.append(body)
    lines.append(END)
    return "\n".join(lines) + "\n"


def _split(text: str):
    """(before, after) around the managed block, None when absent; raises when broken."""
    starts = [m.start() for m in re.finditer(re.escape(_BEGIN_PREFIX), text)]
    ends = [m.start() for m in re.finditer(re.escape(END), text)]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise _fail("표시 영역(devroster:ai begin/end)이 깨졌습니다. 직접 고친 뒤 다시 동기화해주세요.")
    end = ends[0] + len(END)
    if text[end : end + 1] == "\n":
        end += 1
    return text[: starts[0]], text[end:]


def plan_sync(root) -> list[dict]:
    root = Path(root)
    bundle = read_rules(root)
    rows = []
    for name, kind in ENTRY_FILES.items():
        path = root / name
        if path.is_symlink():
            rows.append({"file": name, "state": "conflict", "reason": "심볼릭 링크는 쓰지 않습니다."})
            continue
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        block = _block(kind, bundle)
        if current is None:
            # Nothing to say and no file yet: do not create an empty entry file.
            rows.append({"file": name, "state": "absent" if not bundle["items"] else "create", "content": block})
            continue
        try:
            parts = _split(current)
        except DomainError as exc:
            rows.append({"file": name, "state": "conflict", "reason": str(exc)})
            continue
        if parts is None and not bundle["items"]:
            content = current  # No rules yet: leave a hand-written file untouched.
        elif parts is None:
            content = block + ("\n" + current if current.strip() else "")
        else:
            content = parts[0] + block + parts[1]
        rows.append({"file": name, "state": "current" if content == current else "update", "content": content})
    agents = next(r for r in rows if r["file"] == "AGENTS.md")
    if "content" in agents and len(agents["content"].encode("utf-8")) > AGENTS_MD_LIMIT:
        agents["warning"] = "AGENTS.md가 32KiB를 넘어 Codex가 뒷부분을 읽지 않을 수 있습니다."
    return rows


def sync(root, check: bool = False) -> dict:
    """Write (or with check, only compare) the entry files. Conflicts are never written."""
    root = Path(root)
    rows = plan_sync(root)
    skills = read_skills(root)["items"]
    for row in rows:
        if check or row["state"] not in {"create", "update"}:
            continue
        target = root / row["file"]
        temporary = target.with_name(f".{row['file']}.devroster-tmp")
        temporary.write_text(row["content"], encoding="utf-8", newline="\n")
        os.replace(temporary, target)
        row["state"] = "created" if row["state"] == "create" else "updated"
    public = [{k: v for k, v in row.items() if k != "content"} for row in rows]
    public += _link_skills(root, skills, check)
    stale = [r["file"] for r in public if r["state"] in {"create", "update", "remove", "conflict"}]
    return {"files": public, "stale": stale, "ok": not stale if check else not any(r["state"] == "conflict" for r in public)}


def init(root) -> dict:
    root = Path(root)
    ai = root / AI_DIR
    if ai.is_symlink():
        raise _fail(".ai 폴더는 심볼릭 링크일 수 없습니다.")
    (ai / "rules").mkdir(parents=True, exist_ok=True)
    created = []
    readme = ai / "README.md"
    if not readme.exists():
        readme.write_text(README, encoding="utf-8", newline="\n")
        created.append(readme.relative_to(root).as_posix())
    return {"created": created, "rules": RULES_DIR}


def attach(root, name: str, content: str, replace: bool = False) -> dict:
    """Store a rule file the user attached; it applies to this project only."""
    root = Path(root)
    name = Path(str(name)).name
    if not _NAME.match(name):
        raise _fail("규칙 파일 이름은 영문·한글·숫자·-·_로 된 .md 파일이어야 합니다.")
    if not isinstance(content, str) or not content.strip():
        raise _fail("규칙 내용이 비어 있습니다.")
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        raise _fail("규칙 파일이 64KiB를 넘습니다.")
    if redact(content) != content:
        raise _fail("비밀값으로 보이는 내용이 있어 저장하지 않았습니다.")
    init(root)
    target = root / RULES_DIR / name
    if target.exists() and not replace:
        raise DomainError(ErrorCode.STALE_VERSION, f"같은 이름의 규칙이 있습니다: {name}")
    target.write_text(content, encoding="utf-8", newline="\n")
    read_rules(root)  # Total size and safety still hold with the new file.
    return {"path": target.relative_to(root).as_posix()}


def remove(root, name: str) -> dict:
    root = Path(root)
    name = Path(str(name)).name
    target = root / RULES_DIR / name
    if not _NAME.match(name) or not target.is_file() or target.is_symlink():
        raise _fail("규칙 파일을 찾을 수 없습니다.")
    target.unlink()
    return {"removed": f"{RULES_DIR}/{name}"}


def overview(root) -> dict:
    """Screen data: bundled rules (read only), project rules, entry file status."""
    from agent_dev import defaults

    root = Path(root)
    try:
        bundle = read_rules(root)
        skills = read_skills(root)["items"]
        error = ""
    except DomainError as exc:
        bundle, error = {"items": [], "bytes": 0, "digest": "", "warnings": []}, str(exc)
        skills = {}
    files = [] if error else sync(root, check=True)["files"]
    return {
        "builtin": [label for label, _text in defaults.global_rules()],
        "items": [{k: v for k, v in item.items() if k != "text"} for item in bundle["items"]],
        "skills": [
            {"name": name, "path": item["path"], "description": item["description"], "roles": item["roles"]}
            for name, item in skills.items()
        ],
        "preview": rules_text(bundle),
        "bytes": bundle["bytes"],
        "warnings": bundle["warnings"],
        "error": error,
        "files": files,
        "initialized": (root / AI_DIR).is_dir(),
    }
