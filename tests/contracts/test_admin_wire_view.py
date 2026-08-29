"""Static contracts for the request-detail wire pane.

The pane is the only surface that answers "what actually left this process",
and every defect it has had was a rendering rule rather than a data one: a cut
JSON string printed verbatim, a null coerced to zero, a credential invented for
a request that never had one. These assert on the shipped ``admin.js`` text, so
they run everywhere -- including on a machine with no node.
"""

import re
from pathlib import Path

from my_claude_code.core.wire_capture import _SAMPLING_FIELDS

ADMIN_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
    / "admin.js"
).read_text(encoding="utf-8")


def _function_body(name: str) -> str:
    start = ADMIN_JS.index(f"function {name}(")
    depth = 0
    for index in range(ADMIN_JS.index("{", start), len(ADMIN_JS)):
        if ADMIN_JS[index] == "{":
            depth += 1
        elif ADMIN_JS[index] == "}":
            depth -= 1
            if depth == 0:
                return ADMIN_JS[start : index + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def test_the_wire_pane_never_prints_a_raw_preview_as_the_only_body() -> None:
    """Both stored shapes render: the legacy cut string, and parseable JSON."""
    body = _function_body("formatWireBody")
    assert "_preview" in body
    assert "JSON.stringify(body, null, 2)" in body


def test_the_sampling_field_list_matches_the_writer() -> None:
    """The knobs block hard-codes the list; this is its drift guard."""
    match = re.search(r"const WIRE_SAMPLING_FIELDS = \[(.*?)\];", ADMIN_JS, re.DOTALL)
    assert match is not None
    listed = tuple(re.findall(r'"([^"]+)"', match.group(1)))
    assert listed == _SAMPLING_FIELDS


def test_no_analytics_render_coerces_a_null_to_zero() -> None:
    """A dash means not measured; a zero means measured and zero."""
    body = _function_body("renderWebSearchAnalytics")
    assert "?? 0" not in body


def test_the_string_keyless_is_gone() -> None:
    """It invented a fact -- that the provider needed no key."""
    # The comment beside the fix names the removed string, so the assertion
    # is on the rendered value, not on the word appearing anywhere in the file.
    assert '"keyless"' not in ADMIN_JS.replace('not "keyless"', "")


def test_the_contradiction_badge_keys_on_the_adaptation_kind() -> None:
    """Never on the message text, which is prose and gets reworded."""
    body = _function_body("wireContradicts")
    assert "reasoning_adaptation_kind" in body
    assert "SUPPRESS" not in body
    assert "reasoning_adaptation;" not in body


def test_an_empty_body_reads_as_the_model_default() -> None:
    assert "no reasoning instruction sent (model default applies)" in ADMIN_JS


def test_the_wire_pane_is_not_hidden_for_an_unmeasured_attempt() -> None:
    """It used to filter on wire_body and vanish, reading as "no body sent"."""
    body = _function_body("renderWireRequest")
    assert ".filter((a) => a.wire_body)" not in body
    assert "req-wire-unmeasured" in body
