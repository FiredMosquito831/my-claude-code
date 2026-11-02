"""Write a small models.dev-shaped cache the recogniser can read, and use it.

A test must not depend on the developer's own cache: the recogniser reads a
file, so the file is the fixture. The envelope is the one `read_models_dev_cache`
parses (``{fetched_at, index, etag}``).
"""

import json
from pathlib import Path

INDEX = {
    "aihubmix": {
        "id": "aihubmix",
        "name": "AIHubMix",
        "api": "https://aihubmix.com/v1",
        "env": ["AIHUBMIX_API_KEY"],
        "models": {},
    },
    "shared-a": {
        "id": "shared-a",
        "name": "Shared A",
        "api": "https://shared.example.com/v1",
        "models": {},
    },
    "shared-b": {
        "id": "shared-b",
        "name": "Shared B",
        "api": "https://shared.example.com/v1",
        "models": {},
    },
    "template-only": {
        "id": "template-only",
        "name": "Template only",
        "api": "${some_account}.example.com",
        "models": {},
    },
}


def write_cache(directory: Path) -> Path:
    path = directory / "models-dev.json"
    path.write_text(
        json.dumps(
            {
                "fetched_at": "2026-11-02T00:00:00+00:00",
                "etag": None,
                "index": INDEX,
            }
        ),
        encoding="utf-8",
    )
    return path
