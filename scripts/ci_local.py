"""Reproduce the quality job of .github/workflows/refactor-quality.yml on this machine.

GitHub Actions does not run for this repository, so pull requests are checked
locally. The lint and typing scopes are read from the workflow file itself, so the two
cannot drift apart. Steps, in workflow order:

    ruff (CI scope) -> pyright --pythonplatform Linux -> pytest -> wheel build -> scripts/smoke_wheel.py

Usage:
    python scripts/ci_local.py                      # use the current interpreter
    python scripts/ci_local.py --python python3.11  # fresh virtualenv with that Python
    python scripts/ci_local.py --step lint --step typing

The ubuntu/macos/windows ``portable`` job is not reproduced; record
it as Not Tested unless it was run on those platforms.
"""

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "refactor-quality.yml"
STEPS = ("lint", "typing", "tests", "wheel")


def quality_commands(text):
    """The ``run:`` command lines of the quality job, in order."""
    commands, in_quality = [], False
    for line in text.splitlines():
        if line.startswith("  ") and not line.startswith("   ") and line.strip().endswith(":"):
            in_quality = line.strip() == "quality:"
            continue
        stripped = line.strip()
        if in_quality and stripped.startswith(("run:", "- run:")):
            commands.append(stripped.split("run:", 1)[1].strip())
    return commands


def scope(commands, tool):
    for command in commands:
        words = shlex.split(command)
        if words and words[0] == tool:
            return words[2:] if tool == "ruff" else words[1:]
    raise SystemExit(f"{tool} step not found in {WORKFLOW}")


def environment_python(requested, workdir):
    if not requested:
        return Path(sys.executable)
    target = workdir / "venv"
    subprocess.run([requested, "-m", "venv", str(target)], check=True)
    python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "-q",
            "-e",
            f"{ROOT}[dev,ci]",
        ],
        check=True,
    )
    return python


def tool(python, name):
    candidate = python.parent / (name + (".exe" if os.name == "nt" else ""))
    return [str(candidate)] if candidate.exists() else [str(python), "-m", name]


def main(argv=None):
    parser = argparse.ArgumentParser(description=(__doc__ or "ci").splitlines()[0])
    parser.add_argument("--python", help="Create a fresh virtualenv with this interpreter.")
    parser.add_argument("--step", action="append", choices=STEPS, help="Run only these steps.")
    args = parser.parse_args(argv)
    commands = quality_commands(WORKFLOW.read_text(encoding="utf-8"))
    selected = args.step or list(STEPS)
    results = []
    with tempfile.TemporaryDirectory(prefix="devroster-ci-") as directory:
        workdir = Path(directory)
        python = environment_python(args.python, workdir)
        version = subprocess.run(
            [str(python), "-c", "import sys;print(sys.version.split()[0])"],
            check=True,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        ).stdout.strip()
        dist = workdir / "dist"
        plan = {
            "lint": [tool(python, "ruff") + ["check", *scope(commands, "ruff")]],
            "typing": [
                tool(python, "pyright") + ["--pythonplatform", "Linux", *scope(commands, "pyright")]
            ],
            "tests": [[str(python), "-m", "pytest", "-q"]],
            "wheel": [[str(python), "-m", "build", "--wheel", "--outdir", str(dist)]],
        }
        for step in STEPS:
            if step not in selected:
                continue
            for command in plan[step]:
                print(f"== {step} (Python {version}): {' '.join(command[:4])} ...", flush=True)
                code = subprocess.run(command, cwd=ROOT, check=False).returncode
                results.append((step, code))
                if code:
                    break
            if step == "wheel" and results[-1] == ("wheel", 0):
                wheels = sorted(dist.glob("*.whl"))
                code = subprocess.run(
                    [str(python), "scripts/smoke_wheel.py", *map(str, wheels)],
                    cwd=ROOT,
                    check=False,
                ).returncode
                results.append(("smoke_wheel", code))
    print(f"== summary (Python {version})")
    for step, code in results:
        print(f"{step}: {'pass' if code == 0 else f'FAIL ({code})'}")
    return 1 if any(code for _, code in results) else 0


if __name__ == "__main__":
    sys.exit(main())
