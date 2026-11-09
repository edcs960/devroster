"""Windows command resolution: npm.cmd / gradlew.bat found like a shell, batch arguments quoted."""

import pytest

from agent_dev.adapters.windows_launch import (
    UnsafeBatchArgument,
    batch_command_line,
    find_executable,
    windows_command,
)

COMSPEC = r"C:\Windows\System32\cmd.exe"


def msvcrt_split(line):
    """How a Windows program (MSVCRT / CommandLineToArgvW, 2008+) splits its command line."""
    args, current, quoted, index, started = [], [], False, 0, False
    while index < len(line):
        char = line[index]
        if char == "\\":
            run = 0
            while index < len(line) and line[index] == "\\":
                run += 1
                index += 1
            if index < len(line) and line[index] == '"':
                current.append("\\" * (run // 2))
                if run % 2:
                    current.append('"')
                    index += 1
                started = True
                continue
            current.append("\\" * run)
            started = True
            continue
        if char == '"':
            if quoted and line[index + 1 : index + 2] == '"':
                current.append('"')
                index += 2
                continue
            quoted = not quoted
            started = True
            index += 1
            continue
        if char in " \t" and not quoted:
            if started:
                args.append("".join(current))
                current, started = [], False
            index += 1
            continue
        current.append(char)
        started = True
        index += 1
    if started:
        args.append("".join(current))
    return args


def cmd_sees(line):
    """The part of the line after /c, with the outer pair cmd strips removed."""
    marker = '" /d /e:ON /v:OFF /c "'
    assert line.startswith('"') and marker in line and line.endswith('"')
    return line.split(marker, 1)[1][:-1]


def cmd_metacharacters_outside_quotes(inner):
    """Characters cmd.exe would act on: & | < > ^ ( ) outside a quoted span."""
    quoted, found = False, []
    for char in inner:
        if char == '"':
            quoted = not quoted
        elif not quoted and char in "&|<>^()":
            found.append(char)
    return found


def test_other_systems_keep_the_command():
    assert windows_command(["npm", "test"], nt=False) == ["npm", "test"]


def test_batch_file_on_path_runs_through_quoted_cmd(tmp_path):
    (tmp_path / "npm.cmd").write_text("@echo off\n", encoding="utf-8")
    line = windows_command(["npm", "run", "a&calc"], path=str(tmp_path), nt=True)
    assert isinstance(line, str)
    inner = cmd_sees(line)
    assert msvcrt_split(inner) == [str(tmp_path / "npm.cmd"), "run", "a&calc"]
    assert cmd_metacharacters_outside_quotes(inner) == []


@pytest.mark.parametrize(
    "value",
    [
        "a&calc",
        'say "hi" & exit',
        "C:\\dir with space\\",
        'a\\"b',
        "x^y|z>out<in",
        "",
        'sandbox_mode="workspace-write"',
        'writable_roots=["C:\\work\\repo"]',
        "\\\\server\\share\\",
    ],
)
def test_batch_arguments_survive_cmd_and_reach_the_program_unchanged(value):
    inner = cmd_sees(batch_command_line(r"C:\tools\codex.cmd", ["exec", value], comspec=COMSPEC))
    assert msvcrt_split(inner) == [r"C:\tools\codex.cmd", "exec", value]
    assert cmd_metacharacters_outside_quotes(inner) == []


@pytest.mark.parametrize("value", ["%PATH%", "50%", "line\nbreak", "a\rb"])
def test_values_cmd_would_expand_are_refused(value):
    with pytest.raises(UnsafeBatchArgument):
        batch_command_line(r"C:\tools\npm.cmd", [value], comspec=COMSPEC)
    assert issubclass(UnsafeBatchArgument, OSError)  # callers already report OSError as "cannot run"


def test_executable_found_by_pathext_stays_a_list(tmp_path):
    (tmp_path / "graphify.exe").write_bytes(b"")
    assert windows_command(["graphify", "update"], path=str(tmp_path), nt=True) == [
        str(tmp_path / "graphify.exe"),
        "update",
    ]


def test_relative_script_is_found_from_the_working_folder(tmp_path):
    (tmp_path / "gradlew.bat").write_text("@echo off\n", encoding="utf-8")
    line = windows_command(["./gradlew", "test"], cwd=str(tmp_path), path="", nt=True)
    assert isinstance(line, str) and "gradlew.bat" in line
    assert find_executable("./gradlew", cwd=str(tmp_path), path="") == str(tmp_path / "gradlew.bat")


def test_unknown_command_is_left_for_the_normal_error(tmp_path):
    assert windows_command(["nothing-here", "x"], path=str(tmp_path), nt=True) == ["nothing-here", "x"]
