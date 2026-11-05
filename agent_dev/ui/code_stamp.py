"""Detect that the installed code changed under a running app (a pull or an upgrade).

The app keeps the modules it imported at start; files read lazily later (removed
modules, new assets) may no longer match, so the screens ask for a restart.
"""

import hashlib
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]


def code_stamp(root=PACKAGE):
    """Digest of every source and asset file's path, size and modification time."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts or not path.is_file() or path.suffix == ".pyc":
            continue
        stat = path.stat()
        digest.update(f"{path.relative_to(root)}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode())
    return digest.hexdigest()
