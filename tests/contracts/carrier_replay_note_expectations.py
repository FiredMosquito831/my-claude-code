"""Finite, approved wire-note corrections to historical request snapshots.

The original baselines remain unchanged. These expectations were recorded on
this tree before any other edit, from the one change 8.16.0 makes to a *note*
rather than to a body: ``history_thinking_omitted`` now counts a
``redacted_thinking`` block, because that block is reasoning the Responses
surface leaves out and a note that did not count it was under-reporting its own
omission.

Every entry pins both the old value and the independently recorded corrected
value; an unlisted case must still equal its historical baseline. A cell whose
``before`` no longer matches the baseline is a broken anchor and fails, and so
is a cell whose ``after`` equals its ``before`` -- a correction that corrects
nothing is a hole in the guard.

**No body is in this table.** Only ``*_wire_notes`` cells appear, which is the
whole claim the spec makes: the omission-note change is fixture-level and lives
in ``params.wire``, never in a byte that goes upstream.
"""

import json
from pathlib import Path
from typing import Any

PATH = Path(__file__).with_name("carrier_replay_note_expectations.json")


def corrections(group: str) -> dict[str, Any]:
    document = json.loads(PATH.read_text(encoding="utf-8"))
    return document["cases"][group]


def corrected_note(group: str, key: str, historical: Any) -> Any:
    """The approved value for one wire-note cell, or the historical one."""

    entry = corrections(group).get(key)
    if entry is None:
        return historical
    assert entry["before"] == historical, (group, key, "historical bytes changed")
    assert entry["after"] != historical, (group, key, "vacuous correction")
    return entry["after"]
