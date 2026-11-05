from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any


@contextlib.contextmanager
def locked(path: Path) -> Iterator[None]:
    """Advisory cross-process file lock so read-modify-write sequences on the
    same JSON store (e.g. two Orchestrator instances) don't lose each other's
    updates. Held for the duration of the `with` block."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            # Windows locks a byte range; reserve the same byte for every caller.
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as file:
            return json.load(file)
    except json.JSONDecodeError:
        backup_path = path.with_suffix(path.suffix + ".bak")
        if backup_path.exists():
            with backup_path.open("r", encoding="utf-8") as file:
                return json.load(file)
        raise


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        try:
            backup_path.write_bytes(path.read_bytes())
        except OSError:
            pass
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)
        file.write("\n")
    os.replace(tmp_path, path)


def read_text(path: Path, limit_chars: int | None = None) -> str:
    text = path.read_text(encoding="utf-8")
    if limit_chars is None or len(text) <= limit_chars:
        return text
    return text[:limit_chars] + "\n\n[truncated]\n"


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    os.replace(tmp_path, path)
