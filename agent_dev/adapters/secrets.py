"""Execution boundary for registered environment secrets; never persisted as values."""

import os
from pathlib import Path

from agent_dev.config.settings import is_secret_key


def registered_secrets(root):
    from agent_dev.adapters.environment import read_environment

    values = dict(os.environ)
    path = Path(root) / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    from agent_dev.adapters.credential_store import CredentialStore

    configured = read_environment(root)
    return tuple(
        {value for key, value in values.items() if value and is_secret_key(key)}
        | (set(configured.values()) - {""})
        | set(CredentialStore.shared_values())
    )
