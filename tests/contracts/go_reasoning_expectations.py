"""Finite, approved Go corrections to historical request snapshots.

The original baselines remain unchanged. These expectations were recorded on
5161a7b6 before any production edit, using only the approved Go declaration
preview. Every entry pins both the old value and the independently recorded
corrected value; an unlisted case must still equal its historical baseline.
"""

import json
from pathlib import Path
from typing import Any

PATH = Path(__file__).with_name("opencode_go_reasoning_expectations.json")


def corrections(group: str) -> dict[str, Any]:
    document = json.loads(PATH.read_text(encoding="utf-8"))
    return document["cases"][group]


def corrected_expectation(group: str, key: str, historical: Any) -> Any:
    entry = corrections(group).get(key)
    if entry is None:
        return historical
    assert entry["before"] == historical, (group, key, "historical bytes changed")
    assert entry["after"] != historical, (group, key, "vacuous correction")
    return entry["after"]
