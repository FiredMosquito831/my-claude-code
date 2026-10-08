"""The detail view shows the requested policy only when gating changed it.

`admin.js` is not covered by the type checker or the Python tests, and this
one row carries the whole point of recording two policies: a row duplicated on
every request is noise, and a row omitted when the values differ hides the only
thing worth seeing. The helper is extracted from the shipped file and executed,
so the assertion is on behaviour rather than on the presence of a string.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ADMIN_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
    / "admin.js"
)


def test_the_detail_view_lists_the_requested_policy() -> None:
    source = ADMIN_JS.read_text(encoding="utf-8")
    assert '["Reasoning policy", row.reasoning],' in source
    assert '["Requested reasoning", formatRequestedReasoning(row)],' in source


def _helper_source() -> str:
    source = ADMIN_JS.read_text(encoding="utf-8")
    match = re.search(
        r"^function formatRequestedReasoning\(row\) \{.*?^\}",
        source,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "formatRequestedReasoning is no longer a top-level fn"
    return match.group(0)


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        # Gating changed the policy: the reader must see both.
        (
            {
                "reasoning": "control=on,effort=high",
                "requested_reasoning": "control=on,effort=max",
            },
            "control=on,effort=max",
        ),
        # Nothing was clamped: an identical second row is pure noise.
        (
            {
                "reasoning": "control=on,effort=max",
                "requested_reasoning": "control=on,effort=max",
            },
            "",
        ),
        # Written before the column existed: claim nothing.
        ({"reasoning": "control=on,effort=max", "requested_reasoning": None}, ""),
    ],
)
@pytest.mark.local_serial
def test_the_requested_row_appears_only_when_it_differs(row, expected) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    script = (
        f"{_helper_source()}\n"
        f"process.stdout.write(JSON.stringify("
        f"formatRequestedReasoning({json.dumps(row)})));"
    )
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == expected
