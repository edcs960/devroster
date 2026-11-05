"""Read only public model capability metadata; never probe accounts or call AI."""

import json
import os
from pathlib import Path

from agent_dev.domain.errors import DomainError
from agent_dev.domain.model_routing import EFFORTS, model_text


def codex_models():
    path = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "models_cache.json"
    try:
        if path.is_symlink() or path.stat().st_size > 8_000_000:
            return []
        models = json.loads(path.read_text(encoding="utf-8")).get("models", [])
        result = []
        for row in models[:100]:
            if row.get("visibility", "list") != "list":
                continue
            name = model_text(row.get("slug", ""))
            if name:
                result.append(
                    {
                        "model": name,
                        "supported_efforts": [
                            level["effort"]
                            for level in row.get("supported_reasoning_levels", [])
                            if isinstance(level, dict) and level.get("effort") in EFFORTS
                        ],
                    }
                )
        return result
    except (OSError, ValueError, TypeError, AttributeError, DomainError):
        return []
