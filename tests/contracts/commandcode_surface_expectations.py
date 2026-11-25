"""Finite, scoped CommandCode ``response_surface`` expectations for v8.47.0.

The original ``metadata_ladder_equality_baseline.json`` remains unchanged.
When ``SPECIAL_SURFACE_PROFILES["commandcode"]`` declares
``(CHAT_COMPLETIONS, RESPONSES)`` and ``resolve_surface_ladder()`` consults
``declared.endpoints``, only the two non-Claude CommandCode capability panels
and the digested page/provenance outputs embedding them move from ``null`` to
``"chat_completions"``. ``commandcode/claude-sonnet-5-5`` (delegated via
``_anthropic`` ``/messages``) and all other 84 rows, kinds, prices, routing
lookups, and serialisers remain 100% byte-identical.
"""

import json
from pathlib import Path
from typing import Any

PATH = Path(__file__).with_name("commandcode_surface_expectations.json")


def commandcode_surface_cases(group: str) -> dict[str, dict[str, Any]]:
    """Return the finite ``key -> {before, after}`` map for ``group``."""

    document: dict[str, Any] = json.loads(PATH.read_text(encoding="utf-8"))
    cases: dict[str, dict[str, Any]] = document["cases"][group]
    return cases


def commandcode_surface_expectation(group: str, key: str, historical: Any) -> Any:
    """Return the v8.47.0 answer for a moved CommandCode key, else ``historical``."""

    entry = commandcode_surface_cases(group).get(key)
    if entry is None:
        return historical
    assert entry["before"] == historical, (group, key, "historical bytes changed")
    assert entry["after"] != historical, (group, key, "vacuous correction")
    return entry["after"]
