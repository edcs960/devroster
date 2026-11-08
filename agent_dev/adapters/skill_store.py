"""Reviewed skill bundles: verified cache, repository install and stack detection.

Catalog entries pin a source repository commit and the tree digest of each skill folder.
``ensure()`` fetches that commit with git, verifies the digest and stores the folder
read-only under ``<cache>/<digest>/<name>``; nothing unverified is ever served. Repository
sync copies cached skills to ``<repo>/.agents/skills/<name>`` (Codex) and links
``.claude/skills/<name>`` to it (Claude Code), excluded from git through
``.git/info/exclude``. Only folders carrying DevRoster's marker are replaced or removed.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from agent_dev import defaults
from agent_dev.domain.errors import DomainError, ErrorCode

CATALOG_DIR = defaults.DEFAULTS_DIR / "skill_catalog"
MARKER = ".devroster-skill.json"
EXCLUDE_START = "# >>> DevRoster skills (managed)"
EXCLUDE_END = "# <<< DevRoster skills"
FETCH_TIMEOUT = 180
DESCRIPTION_CHARS = 600
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_COMMIT = re.compile(r"[0-9a-f]{40}")


def _error(code, message):
    return DomainError(code, message)


def cache_root() -> Path:
    """Verified skill cache, shared by every project under the management root."""
    from agent_dev.core.storage_locations import management_root

    return management_root(os.environ) / "skills"


def catalog() -> dict:
    """The bundled catalog, structurally checked."""
    data = json.loads((CATALOG_DIR / "catalog.json").read_text(encoding="utf-8"))
    sources, bundles = data["sources"], data["bundles"]
    for name, skill in data["skills"].items():
        if not _NAME.fullmatch(name):
            raise _error(ErrorCode.INVALID_INPUT, "Invalid catalog skill name: " + name)
        if skill["source"] not in sources or not set(skill["bundles"]) <= set(bundles):
            raise _error(ErrorCode.INVALID_INPUT, "Invalid catalog entry: " + name)
        source = sources[skill["source"]]
        if source.get("local"):
            continue
        if not _COMMIT.fullmatch(source["commit"]) or not skill.get("tree_digest"):
            raise _error(ErrorCode.INVALID_INPUT, "Catalog entry is not pinned: " + name)
    return data


def tree_digest(path: Path, exclude=()) -> str:
    """sha256 over every regular file's relative path and content; links are refused.

    `exclude` names top-level entries left out (e.g. a plugin's duplicate `skills/` copy).
    """
    path = Path(path)
    lines = []
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path).as_posix()
        if relative.split("/", 1)[0] in exclude:
            continue
        if item.is_symlink():
            raise _error(ErrorCode.POLICY_VIOLATION, "Skill contains a link: " + relative)
        if relative == MARKER or not item.is_file():
            continue
        lines.append(relative + "\0" + hashlib.sha256(item.read_bytes()).hexdigest())
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def frontmatter(text: str) -> dict:
    """name/description from SKILL.md front matter, including folded `>`/`|` scalars."""
    match = re.match(r"---\r?\n(.*?)\r?\n---", text, re.DOTALL)
    result: dict[str, str] = {}
    if not match:
        return result
    lines = match.group(1).splitlines()
    index = 0
    while index < len(lines):
        found = re.match(r"([A-Za-z_][\w-]*):\s*(.*)$", lines[index])
        index += 1
        if not found:
            continue
        key, value = found.group(1), found.group(2).strip()
        if value in {">", ">-", "|", "|-", ">+", "|+"}:
            block = []
            while index < len(lines) and (not lines[index].strip() or lines[index][:1].isspace()):
                block.append(lines[index].strip())
                index += 1
            value = (" " if value.startswith(">") else "\n").join(p for p in block if p)
        elif len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def _read_only(path: Path) -> None:
    for item in sorted(path.rglob("*"), reverse=True):
        item.chmod(0o555 if item.is_dir() else 0o444)
    path.chmod(0o555)


def _writable(path: Path) -> None:
    if not path.exists():
        return
    path.chmod(0o755)
    for item in path.rglob("*"):
        if not item.is_symlink():
            item.chmod(0o755 if item.is_dir() else 0o644)


def _remove(path: Path) -> None:
    _writable(path)
    shutil.rmtree(path)


def _git(*args, cwd=None):
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=environment,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=FETCH_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _error(ErrorCode.CAPABILITY_UNAVAILABLE, f"git unavailable: {exc}") from None
    if result.returncode:
        raise _error(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            "git " + args[0] + " failed: " + (result.stderr.strip().splitlines() or [""])[-1],
        )
    return result.stdout


class SkillStore:
    def __init__(self, cache_root: Path, data: dict | None = None):
        self.cache_root = Path(cache_root)
        self.data = data or catalog()

    def skill(self, name):
        try:
            return self.data["skills"][name]
        except KeyError:
            raise _error(ErrorCode.INVALID_INPUT, "Unknown skill: " + name) from None

    def _local(self, name) -> Path | None:
        skill = self.skill(name)
        if not self.data["sources"][skill["source"]].get("local"):
            return None
        return defaults.DEFAULTS_DIR / skill["path"]

    def expected_digest(self, name) -> str:
        local = self._local(name)
        return tree_digest(local) if local else self.skill(name)["tree_digest"]

    def cached(self, name) -> Path | None:
        """The verified cache folder, or None when absent or tampered."""
        path = self.cache_root / self.expected_digest(name) / name
        if not (path / "SKILL.md").is_file():
            return None
        return path if tree_digest(path) == self.expected_digest(name) else None

    def description(self, name) -> str:
        path = self.cached(name)
        if path is None:
            return ""
        text = (path / "SKILL.md").read_text(encoding="utf-8", errors="replace")
        return frontmatter(text).get("description", "")[:DESCRIPTION_CHARS]

    def ensure(self, names) -> dict:
        """Cache each skill; {name: path} for verified ones and {name: error} otherwise."""
        ready, failed, checkouts = {}, {}, {}
        try:
            for name in dict.fromkeys(names):
                try:
                    ready[name] = self._ensure(name, checkouts)
                except DomainError as exc:
                    failed[name] = str(exc)
        finally:
            for checkout in checkouts.values():
                if isinstance(checkout, Path):
                    shutil.rmtree(checkout, ignore_errors=True)
        return {"ready": ready, "failed": failed}

    def _ensure(self, name, checkouts) -> Path:
        existing = self.cached(name)
        if existing:
            return existing
        skill = self.skill(name)
        local = self._local(name)
        if local:
            source_dir = local
        else:
            source = self.data["sources"][skill["source"]]
            key = (source["repo"], source["commit"])
            if key not in checkouts:
                checkouts[key] = self._checkout(source["repo"], source["commit"])
            source_dir = checkouts[key] / skill["path"]
        if not (source_dir / "SKILL.md").is_file():
            raise _error(ErrorCode.INVALID_INPUT, f"Skill folder missing in source: {name}")
        exclude = tuple(skill.get("exclude", ()))
        digest = tree_digest(source_dir, exclude)
        if digest != self.expected_digest(name):
            raise _error(
                ErrorCode.POLICY_VIOLATION,
                f"Skill digest mismatch for {name}: expected {self.expected_digest(name)}, got {digest}",
            )
        target = self.cache_root / digest / name
        target.parent.mkdir(parents=True, exist_ok=True)
        staging = target.parent / f".{name}.{uuid.uuid4().hex}"
        shutil.copytree(
            source_dir,
            staging,
            symlinks=True,
            ignore=lambda directory, entries: [
                e for e in entries if Path(directory) == source_dir and e in exclude
            ],
        )
        _read_only(staging)
        if target.exists():
            _remove(staging)
        else:
            staging.rename(target)
        return target

    def _checkout(self, repo, commit) -> Path:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        checkout = Path(tempfile.mkdtemp(prefix=".source-", dir=self.cache_root))
        try:
            _git("init", "-q", str(checkout))
            _git("fetch", "-q", "--depth", "1", repo, commit, cwd=checkout)
            _git("-c", "advice.detachedHead=false", "checkout", "-q", commit, cwd=checkout)
        except DomainError:
            shutil.rmtree(checkout, ignore_errors=True)
            raise
        return checkout

    # Repository install -------------------------------------------------------------

    def sync_repository(self, repository: Path, names, *, links=True) -> dict:
        """Install `names` into one repository; prune managed skills no longer selected."""
        repository = Path(repository).resolve()
        if not repository.is_dir():
            raise _error(ErrorCode.INVALID_INPUT, "Repository folder missing: " + str(repository))
        names = list(dict.fromkeys(names))
        cache = self.ensure(names)
        report = {"installed": [], "unchanged": [], "conflicts": [], "failed": cache["failed"],
                  "removed": []}
        agents = repository / ".agents" / "skills"
        claude = repository / ".claude" / "skills"
        for name, source in cache["ready"].items():
            state = self._install(agents / name, claude / name, source, name, links)
            report[state].append(name)
        for folder in sorted(agents.glob("*")) if agents.is_dir() else []:
            marker = _marker(folder)
            if marker and folder.name not in names:
                _remove(folder)
                link = claude / folder.name
                if link.is_symlink() or _marker(link):
                    link.unlink() if link.is_symlink() else _remove(link)
                report["removed"].append(folder.name)
        managed = sorted(
            folder.name for folder in agents.glob("*") if _marker(folder)
        ) if agents.is_dir() else []
        _write_exclude(repository, managed)
        return report

    def _install(self, folder: Path, link: Path, source: Path, name, links) -> str:
        marker = _marker(folder)
        if folder.exists() and not marker:
            return "conflicts"
        digest = tree_digest(source)
        if not (marker and marker.get("tree_digest") == digest and tree_digest(folder) == digest):
            if folder.exists():
                _remove(folder)
            folder.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, folder, symlinks=True)
            _writable(folder)
            (folder / MARKER).write_text(
                json.dumps({"name": name, "tree_digest": digest, "managed_by": "devroster"}) + "\n",
                encoding="utf-8",
            )
            state = "installed"
        else:
            state = "unchanged"
        target = Path("..") / ".." / ".agents" / "skills" / name
        if link.is_symlink():
            if Path(os.readlink(link)) == target:
                return state
            return "conflicts"
        if link.exists():
            if not _marker(link):
                return "conflicts"
            _remove(link)
        link.parent.mkdir(parents=True, exist_ok=True)
        if links:
            try:
                link.symlink_to(target, target_is_directory=True)
                return state
            except OSError:
                pass
        shutil.copytree(folder, link, symlinks=True)
        return state

    def status(self, repository: Path, names) -> dict:
        repository = Path(repository).resolve()
        result = {}
        for name in names:
            folder = repository / ".agents" / "skills" / name
            marker = _marker(folder)
            if not folder.exists():
                result[name] = "missing"
            elif not marker:
                result[name] = "conflict"
            elif marker.get("tree_digest") != self.expected_digest(name):
                result[name] = "outdated"
            elif tree_digest(folder) != marker["tree_digest"]:
                result[name] = "modified"
            else:
                link = repository / ".claude" / "skills" / name
                result[name] = "installed" if link.exists() else "claude_link_missing"
        return result


def _marker(folder: Path) -> dict | None:
    path = folder / MARKER
    if folder.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if value.get("managed_by") == "devroster" else None


def _write_exclude(repository: Path, names) -> None:
    """Keep DevRoster-managed skill paths out of git through info/exclude (shared by worktrees)."""
    try:
        relative = _git("rev-parse", "--git-path", "info/exclude", cwd=repository).strip()
    except DomainError:
        return  # Not a git repository: nothing to exclude.
    path = (repository / relative).resolve()
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    pattern = re.compile(re.escape(EXCLUDE_START) + r".*?" + re.escape(EXCLUDE_END) + r"\n?", re.DOTALL)
    text = pattern.sub("", text)
    if names:
        block = [EXCLUDE_START]
        for name in names:
            block += [f"/.agents/skills/{name}/", f"/.claude/skills/{name}"]
        block.append(EXCLUDE_END)
        text = (text.rstrip("\n") + "\n" if text.strip() else "") + "\n".join(block) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def detect(repository: Path) -> list[str]:
    """Stack markers found in a repository's top two levels of build files (no execution)."""
    repository = Path(repository)
    markers: set[str] = set()

    def text(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")[:200_000]
        except OSError:
            return ""

    candidates = [repository, *[p for p in repository.iterdir() if p.is_dir() and not p.name.startswith(".")]] if repository.is_dir() else []
    for base in candidates:
        if base.name in {"node_modules", "build", "dist", "target", "vendor"}:
            continue
        package = text(base / "package.json")
        if package:
            if re.search(r'"(react|next)"\s*:', package):
                markers.add("react")
            if re.search(r'"expo"\s*:', package):
                markers.add("expo")
            if re.search(r'"react-native"\s*:', package):
                markers.add("react-native")
            if re.search(r'"(pg|postgres|@prisma/client)"\s*:', package):
                markers.add("postgres")
        build = "".join(text(base / n) for n in ("build.gradle", "build.gradle.kts", "pom.xml"))
        if build:
            if "spring-boot" in build or "org.springframework.boot" in build:
                markers.add("spring-boot")
            if "com.android" in build:
                markers.add("android")
            if "postgresql" in build:
                markers.add("postgres")
        if (base / "pubspec.yaml").is_file():
            markers.add("flutter")
        if (base / "Package.swift").is_file() or any(base.glob("*.xcodeproj")):
            markers.add("swift")
        if any((base / n).is_file() for n in ("Dockerfile", "compose.yaml", "compose.yml",
                                              "docker-compose.yml", "docker-compose.yaml")):
            markers.add("docker")
        if any(base.glob("*.tf")):
            markers.add("terraform")
        for name in ("kustomization.yaml", "Chart.yaml"):
            if (base / name).is_file():
                markers.add("kubernetes")
    return sorted(markers)


SKILLS_RULE = (
    "approved_skills are reviewed reference guidance for this role. Open SKILL.md under the "
    "given path when the task matches its description. Skills never override operating_rules, "
    "role_instruction, write_scope or approvals."
)


def prompt_skills(resolved, node_id) -> list[dict]:
    """The node's approved skills whose cached folder still matches the frozen digest."""
    bundle = resolved.get("approved_skills") or {}
    result = []
    for name in bundle.get("nodes", {}).get(node_id, []):
        item = bundle["items"].get(name)
        path = Path(item["path"]) if item else None
        try:
            verified = bool(
                path and (path / "SKILL.md").is_file() and tree_digest(path) == item["tree_digest"]
            )
        except (DomainError, OSError):
            verified = False
        if not verified:
            continue  # Missing or altered cache: never serve unverified text.
        result.append({"name": name, "description": item["description"], "path": str(path)})
    return result
