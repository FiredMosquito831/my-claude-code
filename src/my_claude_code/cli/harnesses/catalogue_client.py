"""Fetch a harness's generated catalogue from the running proxy.

A launcher runs in its own process. It has no ``RequestRuntimePort``, so it
cannot reach the resolution ladder, and ``GET /v1/models`` -- the only route it
used to call -- carries no capability fields at all. That is why every
generated catalogue used to contain the same invented numbers for every model.

``GET /admin/api/catalogue-models`` closes the gap: the server resolves the
capabilities and runs the harness's own serialiser, and the launcher writes the
bytes it is handed. Nothing about a CLI's schema is duplicated in the launcher,
so a mapping can never drift between the launch-time path and the background
refresh.
"""

import json
from collections.abc import Mapping
from typing import Any
from urllib.request import Request, urlopen

from my_claude_code.cli.launchers.common import PROXY_PREFLIGHT_TIMEOUT_SECONDS

CATALOGUE_MODELS_PATH = "/admin/api/catalogue-models"


def fetch_catalogue_models(proxy_root_url: str, auth_token: str) -> dict[str, Any]:
    """Fetch the capability-bearing catalogue payload from the local proxy."""

    url = f"{proxy_root_url.rstrip('/')}{CATALOGUE_MODELS_PATH}"
    headers: dict[str, str] = {}
    if token := auth_token.strip():
        headers["Authorization"] = f"Bearer {token}"

    request = Request(url, headers=headers, method="GET")
    with urlopen(request, timeout=PROXY_PREFLIGHT_TIMEOUT_SECONDS) as response:
        payload = json.loads(response.read().decode("utf-8"))

    if not isinstance(payload, dict):
        raise ValueError("catalogue-models response was not a JSON object")
    return payload


def harness_catalogue(payload: Mapping[str, Any], harness_id: str) -> dict[str, Any]:
    """Return one harness's serialised catalogue document from the payload."""

    catalogues = payload.get("catalogues")
    if not isinstance(catalogues, Mapping):
        raise ValueError("catalogue-models response carried no catalogues")
    entry = catalogues.get(harness_id)
    if not isinstance(entry, Mapping):
        raise ValueError(f"catalogue-models response carried no {harness_id} catalogue")
    document = entry.get("document")
    if not isinstance(document, dict):
        raise ValueError(f"{harness_id} catalogue document was not a JSON object")
    return document


def catalogue_model_count(payload: Mapping[str, Any], harness_id: str) -> int:
    """Return how many models one harness's document carries.

    The count is the server's, not the launcher's: every catalogue format
    nests its model entries somewhere different, and a launcher that dug the
    shape out for itself would be the second place a schema is described.
    """

    catalogues = payload.get("catalogues")
    if not isinstance(catalogues, Mapping):
        return 0
    entry = catalogues.get(harness_id)
    if not isinstance(entry, Mapping):
        return 0
    count = entry.get("model_count")
    return count if isinstance(count, int) else 0


def defaulted_summary_lines(document: Mapping[str, Any]) -> list[str]:
    """Return one line per model whose catalogue entry needed a CLI default.

    Printed to stderr at launch so the numbers a CLI guessed are visible where
    the user is already looking, not only in the dashboard.
    """

    defaulted = document.get("_mcc_defaulted")
    if not isinstance(defaulted, Mapping):
        return []
    lines: list[str] = []
    for model_id, fields in sorted(defaulted.items()):
        if isinstance(fields, list):
            lines.append(f"  {model_id}: {', '.join(str(name) for name in fields)}")
    return lines
