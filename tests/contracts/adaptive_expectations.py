"""Finite, recorded moves that 8.13.0's adaptive change makes to two corpora.

The historical baselines stay exactly as they were written. Each call this
release moves is recorded here with BOTH answers -- ``before`` is the digest
the committed baseline holds and ``after`` is the one this tree produces -- so
a test can prove the historical record is intact and the new answer is the
measured one, rather than regenerating a baseline to go green.

The precedent is ``opencode_go_reasoning_expectations.json`` (7.113.2), which
records the Go reasoning correction the same way for the same reason.

What moved, and why. ``ReasoningPolicy.adaptive()`` used to leave no trace on
a Chat Completions body: the encoder had no adaptive channel and emitted
nothing. 8.13.0 makes every door either express adaptive or say it cannot, so
on a Chat host an ADAPTIVE policy now writes the host's own level-less on
(``thinking``, ``reasoning.enabled``, ``include_thoughts``, a chat-template
flag) or, where the host's only word for "reason" is a rung, its highest
published rung. That is a real body change and these are its cells.

What did NOT move: every call whose policy is not ADAPTIVE. Those are still
compared against the committed baseline, byte for byte, on every host.
"""

import json
from pathlib import Path
from typing import Any

PATH = Path(__file__).with_name("adaptive_every_door_expectations.json")


def moved(group: str) -> dict[str, dict[str, str]]:
    """``host/corpus-key -> {before, after}`` for one corpus."""

    document: dict[str, Any] = json.loads(PATH.read_text(encoding="utf-8"))
    entries: dict[str, dict[str, str]] = document[group]
    return entries


def expected_digest(
    group: str, key: str, historical: str, entries: dict[str, dict[str, str]]
) -> str:
    """The digest this release answers for one call, from the historical one.

    An unlisted call must still equal its historical baseline; a listed one
    must have moved, and its recorded ``before`` must be the baseline's own
    value -- so a baseline that silently changed fails here rather than
    passing quietly.
    """

    entry = entries.get(key)
    if entry is None:
        return historical
    assert entry["before"] == historical, (group, key, "historical digest changed")
    assert entry["after"] != historical, (group, key, "vacuous correction")
    return entry["after"]
