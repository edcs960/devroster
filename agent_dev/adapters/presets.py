"""Dependency-free, inspectable verification presets; never modify source files."""

import json
import os
from importlib.resources import files
from pathlib import Path

from agent_dev.domain.errors import DomainError, ErrorCode

PRESETS = {
    "generic": {
        "description": "Explicit custom gates (whitespace check only)",
        "paths": ["src", "tests", "docs", "README.md"],
        "commands": [("diff_check", ["git", "diff", "--check"])],
    },
    "python": {
        "description": "pytest, Ruff and Python build; install project dev tools separately",
        "paths": ["src", "tests", "pyproject.toml", "docs", "README.md"],
        "commands": [
            ("unit", ["python", "-m", "pytest", "-q"]),
            ("lint", ["ruff", "check", "."]),
            ("build", ["python", "-m", "build"]),
        ],
    },
    "react": {
        "description": "npm lint/test/build scripts; test must support --run",
        "paths": ["src", "tests", "public", "package.json", "package-lock.json", "docs", "README.md"],
        "commands": [
            ("lint", ["npm", "run", "lint"]),
            ("unit", ["npm", "run", "test", "--", "--run"]),
            ("build", ["npm", "run", "build"]),
        ],
    },
    "spring-boot": {
        "description": "Maven wrapper verify (tests/package); adapt for Gradle projects",
        "paths": ["src", "pom.xml", "docs", "README.md"],
        "commands": [("verify", ["mvnw.cmd" if os.name == "nt" else "./mvnw", "-B", "verify"])],
    },
    "spring-boot-gradle": {
        "description": "Gradle wrapper build (tests/package)",
        "paths": [
            "src",
            "build.gradle",
            "build.gradle.kts",
            "settings.gradle",
            "settings.gradle.kts",
            "docs",
            "README.md",
        ],
        "commands": [("build", ["gradlew.bat" if os.name == "nt" else "./gradlew", "build"])],
    },
}


def available_commands(name, path):
    """The preset's commands that can run in ``path``, and the ones left out with why.

    `npm run <script>` needs that script in package.json (a React project
    without a lint script got `npm run lint`). Without a readable package.json nothing is
    left out, since the screen cannot tell.
    """
    commands = PRESETS[name]["commands"]
    try:
        scripts = json.loads((Path(path) / "package.json").read_text(encoding="utf-8"))["scripts"]
        if not isinstance(scripts, dict):
            raise TypeError
    except (OSError, ValueError, KeyError, TypeError):
        return list(commands), []
    kept, skipped = [], []
    for gate, command in commands:
        if command[:2] == ["npm", "run"] and command[2] not in scripts:
            skipped.append((gate, f"package.json에 `{command[2]}` 스크립트가 없습니다."))
        else:
            kept.append((gate, command))
    return kept, skipped


def select(name, root):
    if name == "auto":
        candidates = []
        root = Path(root)
        if (root / "pyproject.toml").is_file():
            candidates.append("python")
        if (root / "package.json").is_file():
            candidates.append("react")
        if (root / "pom.xml").is_file():
            candidates.append("spring-boot")
        if (root / "build.gradle").is_file() or (root / "build.gradle.kts").is_file():
            candidates.append("spring-boot-gradle")
        if len(candidates) != 1:
            raise DomainError(
                ErrorCode.INVALID_INPUT,
                "Preset inference is ambiguous; choose an explicit --preset.",
            )
        return candidates[0]
    if name not in PRESETS:
        raise DomainError(ErrorCode.INVALID_INPUT, "Unknown preset.")
    return name


def detect(root):
    """The preset the folder's build files point to; generic when none or several match."""
    try:
        return select("auto", root)
    except DomainError:
        return "generic"


def profile(name, root=None):
    name = select(name, root or Path.cwd())
    result = json.loads(files("agent_dev.templates").joinpath("profile.json").read_text())
    spec = dict(PRESETS[name])
    if root is not None:
        # Leave out `npm run` scripts the project does not define.
        spec["commands"] = available_commands(name, root)[0] or PRESETS["generic"]["commands"]
    result["allowed_paths"] = list(spec["paths"])
    result["repositories"]["source"]["allowed_paths"] = list(spec["paths"])
    result["verification"] = {
        "commands": [
            {
                "name": key,
                "repository": "source",
                "command": args,
                "required": True,
                "timeout_seconds": 900,
            }
            for key, args in spec["commands"]
        ],
        "required_gates": [key for key, _ in spec["commands"]],
    }
    from agent_dev.domain.execution_limits import DEFAULT_LIMITS

    result["execution_limits"] = dict(DEFAULT_LIMITS)
    return result


def listing():
    return {
        "presets": [
            {"id": name, "description": row["description"], "allowed_paths": row["paths"]}
            for name, row in PRESETS.items()
        ],
        "auto_detection": "Manifest inference only; explicitly review commands and paths.",
    }


def write(name, destination):
    path = Path(destination)
    value = profile(name)
    try:
        with path.open("x", encoding="utf-8") as output:
            output.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        path.chmod(0o600)
    except FileExistsError:
        raise DomainError(
            ErrorCode.POLICY_VIOLATION, "Preset destination exists; never overwritten."
        ) from None
    return {
        "preset": name,
        "profile": str(path),
        "operator_actions": [
            "Review allowed paths, install dev tools, then apply via devroster project profile PROJECT FILE. Existing snapshots stay unchanged."
        ],
    }
