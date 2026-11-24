"""Finite, approved Chat reasoning replay and MiniMax OFF corrections (8.40.0).

The original equality baselines remain unchanged. These expectations record
only the exact cells moved by PR 1 (CHAT-REASONING-REPLAY-AND-DIALECT-ALIGNMENT):

* ``history``: 12 ``commandcode(standalone)`` thinking-history cells
  (``reasoning`` -> ``reasoning_content``) and 8 ``minimax/*/off`` cells
  (omitting ``extra_body.reasoning_split`` on ``ReasoningControl.OFF``);
* ``empty``: 1 ``commandcode_chat`` cell in ``fold_every_block``
  (``reasoning`` -> ``reasoning_content``);
* ``market``: 68 ``chat/minimax/*/off/*`` cells (omitting
  ``extra_body.reasoning_split`` on ``ReasoningControl.OFF``).
"""

import json
from pathlib import Path
from typing import Any

PATH = Path(__file__).with_name("chat_reasoning_replay_expectations.json")


def chat_corrections(group: str) -> dict[str, Any]:
    """Return ``key -> {before, after}`` for one corpus group."""

    document = json.loads(PATH.read_text(encoding="utf-8"))
    return document["cases"][group]


def chat_corrected_expectation(group: str, key: str, historical: Any) -> Any:
    """Return the expected value for ``key``, verifying ``before`` when moved."""

    entry = chat_corrections(group).get(key)
    if entry is None:
        return historical
    assert entry["before"] == historical, (group, key, "historical bytes changed")
    assert entry["after"] != historical, (group, key, "vacuous correction")
    return entry["after"]
