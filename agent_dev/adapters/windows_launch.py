"""Start npm, gradlew or an npm-installed AI CLI on Windows the way a shell finds it.

CreateProcess only appends ".exe", so ``["npm", "test"]`` or ``["codex", ...]`` (npm.cmd,
codex.cmd) fail with FileNotFoundError. A .bat/.cmd file always runs through cmd.exe,
which parses the command line again: an argument such as ``a&calc`` would start another
program (CVE-2024-24576, "BatBadBut"). Batch files are therefore started explicitly as
``cmd.exe /d /e:ON /v:OFF /c "..."`` with every argument quoted for cmd and for the
program behind it, following the rules Rust's standard library adopted for that issue.
"%" and line breaks cannot be quoted for cmd and are refused.

Standard library only: the Windows bootstrap scripts import this before the package.
On other systems the command is returned unchanged.
"""

import os
from pathlib import Path

DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"
BATCH_SUFFIXES = {".bat", ".cmd"}
_UNQUOTABLE = ("%", "\r", "\n", "\x00")


class UnsafeBatchArgument(OSError):
    """An argument that cmd.exe would expand or split however it is quoted."""


def find_executable(name, *, cwd=None, path=None, pathext=None):
    """The file Windows would start for ``name``, trying PATHEXT suffixes; None if absent."""
    name = str(name)
    suffixes = [s.lower() for s in (pathext or os.environ.get("PATHEXT") or DEFAULT_PATHEXT).split(";") if s]
    names = [name] if Path(name).suffix.lower() in suffixes else [name + s for s in suffixes]
    if "/" in name or "\\" in name:
        base = Path(name) if Path(name).is_absolute() else Path(cwd or os.getcwd()) / name
        candidates = [base.with_name(Path(n).name) for n in names]
    else:
        folders = (os.environ.get("PATH", "") if path is None else path).split(os.pathsep)
        candidates = [Path(folder) / n for folder in folders if folder for n in names]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _batch_quote(value):
    value = str(value)
    if any(mark in value for mark in _UNQUOTABLE):
        raise UnsafeBatchArgument(
            "배치 파일(.bat/.cmd)에는 %나 줄바꿈이 든 인자를 안전하게 넘길 수 없습니다: " + value[:80]
        )
    out, backslashes = ['"'], 0
    for char in value:
        if char == "\\":
            backslashes += 1
            continue
        if char == '"':
            # 2n backslashes keep their meaning before a quote; "" is a literal quote for
            # both cmd (quoting stays on) and the program's argument parser.
            out.append("\\" * (2 * backslashes) + '""')
        else:
            out.append("\\" * backslashes + char)
        backslashes = 0
    out.append("\\" * (2 * backslashes) + '"')
    return "".join(out)


def batch_command_line(script, arguments, comspec=None):
    """One command line that runs ``script`` with ``arguments`` through cmd.exe, quoted."""
    shell = comspec or os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
    inner = " ".join(_batch_quote(part) for part in [script, *arguments])
    # The outer quotes are the pair /c strips, so the inner quoting reaches the script intact.
    return f'"{shell}" /d /e:ON /v:OFF /c "{inner}"'


def windows_command(command, *, cwd=None, path=None, nt=None):
    """``command`` ready for subprocess on Windows: a list, or a str for a batch file."""
    command = [str(part) for part in command]
    if not (os.name == "nt" if nt is None else nt) or not command:
        return command
    found = find_executable(command[0], cwd=cwd, path=path)
    if found is None:
        return command
    if Path(found).suffix.lower() in BATCH_SUFFIXES:
        return batch_command_line(found, command[1:])
    return [found, *command[1:]]
