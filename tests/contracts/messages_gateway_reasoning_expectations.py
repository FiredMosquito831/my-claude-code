"""Finite, recorded Messages gateway moves for 8.41.0 (PR 2).

The original ``marketplace_equality_baseline.json`` remains unchanged. These
expectations record only the exact 216 ``messages/{corpus_custom,opencode,
opencode_go}/*/on_high/*`` cells moved by ``ANTHROPIC_GATEWAY_REASONING_DIALECT``
(``effort_capable=True``, emitting ``thinking: {"type": "adaptive"}`` +
``output_config: {"effort": "high"}`` instead of ``thinking: {"type": "enabled",
"budget_tokens": ...}``). Every other cell across ``chat/*``, ``responses/*``,
and non-``on_high`` ``messages/*`` remains 100% byte-identical.
"""

import json
from pathlib import Path
from typing import Any

PATH = Path(__file__).with_name("messages_gateway_reasoning_expectations.json")


def messages_gateway_corrections() -> dict[str, dict[str, Any]]:
    """Return ``messages/<host>/<key> -> {before, after}`` for PR 2."""

    document: dict[str, Any] = json.loads(PATH.read_text(encoding="utf-8"))
    entries: dict[str, dict[str, Any]] = document["cases"]["market"]
    return entries
