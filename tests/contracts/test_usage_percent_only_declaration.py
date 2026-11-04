"""SPEC §9.1: the percentage rule is DECLARED DATA, and only where it applies.

`specs/PR-USAGE-LAYOUTS-SPEC.md` §9.1 is unusually specific about the *shape* of
the answer, not only its effect:

    It is a **provider-scoped presentation rule in declared data** (a flag on
    the provider's usage row), not an ``if provider == "commandcode"`` branch in
    a renderer, and not a heuristic on the shape of the numbers.

A behavioural test cannot see the difference: a renderer that branched on the
id and a renderer that read a flag draw the same pixels today, and diverge the
day a second provider needs the rule — or the day Command Code's row is
edited. So the shape is pinned HERE, statically, in five claims:

1. **Exactly one row declares it, and it is the one the user named.** A second
   row appearing is the "do not generalise this rule" clause failing.
2. **The route publishes it on the row**, so the page can only learn it from
   declared data.
3. **No usage-window renderer names a provider id.** This is the mutation
   control for "branch on the id instead of the flag": the branch would compile,
   the page would look right, and this file would fail.
4. **The renderer does not infer the rule from the numbers.** A grep for the
   shapes a heuristic would use (a `used`/`limit` test inside the rule's own
   function) is narrow enough to be honest and wide enough to catch the mistake.
5. **The sentence §9.3 asks for is the sentence the page prints** — one string
   in one place, not a renderer composing its own.

The static halves are greps over the SHIPPED files, which is the only way to
see a branch that a test's own fixture might not exercise (a test that cannot
fail is not a test — IX.1).
"""

import re
from pathlib import Path

from my_claude_code.config.usage_sources import (
    PROVIDER_SOURCES,
    source_for,
)

REPO = Path(__file__).resolve().parents[2]
ADMIN_STATIC = REPO / "src" / "my_claude_code" / "api" / "admin_static"
ADMIN_JS = ADMIN_STATIC / "admin.js"
ROUTES = REPO / "src" / "my_claude_code" / "api" / "admin_usage_routes.py"
DECLARATION = REPO / "src" / "my_claude_code" / "config" / "usage_sources.py"
ADAPTER = REPO / "src" / "my_claude_code" / "core" / "usage" / "commandcode.py"
JSDOM_FIXTURE = REPO / "tests" / "api" / "admin_jsdom_usage_layouts.mjs"

#: The one provider the user's decision names (`USER 2026-11-03`: "also the %
#: thing only applies for the cmdc sub the other providers remain as is").
THE_ONE_PROVIDER = "commandcode"


def test_exactly_one_row_declares_the_rule_and_it_is_the_one_the_user_named() -> None:
    """§9.1's "do not generalise this rule" clause, as a count."""

    declared = [row.provider_id for row in PROVIDER_SOURCES if row.percent_only_windows]
    assert declared == [THE_ONE_PROVIDER], declared


def test_no_other_row_carries_a_note_about_a_rule_it_does_not_have() -> None:
    """A sentence explaining an omission on a row with no omission is a lie.

    The note is the §9.3 sentence ("there is no monthly percentage"), and it is
    meaningless unless the row also declares the rule it explains.
    """

    for row in PROVIDER_SOURCES:
        if row.percent_only_note:
            assert row.percent_only_windows, row.provider_id


def test_the_note_is_one_sentence_about_the_monthly_grant() -> None:
    """§9.3's own requirement, spelled: the note says WHY, not "unavailable"."""

    row = source_for(THE_ONE_PROVIDER)
    note = row.percent_only_note
    assert note, "the PR must say so in one sentence on the card"
    assert "monthly" in note.lower()
    assert "grant" in note.lower()
    # It is bounded prose, not a paragraph that could wrap a card.
    assert 40 < len(note) < 400, len(note)


def test_the_flag_is_a_declared_default_and_not_derived() -> None:
    """`percent_only_windows` is a plain field with a `False` default.

    A property computing it from `mechanisms`, from the endpoint's host, or
    from anything else would be exactly the "heuristic" §9.1 forbids, wearing a
    field's clothes.
    """

    source = DECLARATION.read_text(encoding="utf-8")
    assert "percent_only_windows: bool = False" in source, (
        "the flag must be a declared field with a False default"
    )
    # It must not be a @property: no `def percent_only_windows(` anywhere.
    assert "def percent_only_windows" not in source
    assert "def percent_only_note" not in source


def test_the_route_publishes_the_flag_on_the_row_it_already_sends() -> None:
    """The page can only learn the rule from declared data, so it travels."""

    source = ROUTES.read_text(encoding="utf-8")
    assert '"percent_only_windows": declared.percent_only_windows' in source
    assert '"percent_only_note": declared.percent_only_note' in source


#: What a provider-id branch looks like in this page's JavaScript. Deliberately
#: a pattern over quoted literals rather than a parse: the failure it catches is
#: somebody typing `"commandcode"` inside the rule, and every spelling of that
#: is a quoted string.
_ID_LITERAL = re.compile(r"""["']([a-z0-9_]+)["']""")

#: The ids that could plausibly appear in a WRONG version of these functions.
#: Taken from the declared table so the list cannot go stale, plus the two
#: spellings Command Code's own catalogue uses.
_PROVIDER_IDS = {row.provider_id for row in PROVIDER_SOURCES} | {
    "commandcode",
    "command_code",
    "command-code",
}


def _function_body(name: str) -> str:
    """One top-level function's source, by brace matching from its declaration.

    Brace counting is enough here: these are ordinary functions in a classic
    script with no template-literal braces in their signature, and the failure
    mode of a slightly-long body is a NARROWER search than intended — which
    still finds a provider id, never hides one.
    """

    text = ADMIN_JS.read_text(encoding="utf-8")
    start = text.index(f"function {name}(")
    depth = 0
    opened = False
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
            opened = True
        elif char == "}":
            depth -= 1
            if opened and depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"{name} has no closing brace")


def test_no_renderer_in_the_rule_names_a_provider() -> None:
    """**§9.1: "not an `if provider == "commandcode"` branch in a renderer".**

    This is the contract test the mutation control reverts to. A branch added
    to either of these functions makes the page look identical today and makes
    this file fail.
    """

    for name in ("usageWindowFigure", "usageMarkPercentOnly", "usagePercentOnlyNote"):
        body = _function_body(name)
        named = sorted(
            literal
            for literal in (match.group(1) for match in _ID_LITERAL.finditer(body))
            if literal in _PROVIDER_IDS
        )
        assert not named, f"{name} names a provider: {named}"


def test_the_rule_is_read_from_the_window_and_never_from_the_numbers() -> None:
    """§9.1: "not a heuristic on the shape of the numbers".

    The rule's own branch is `win.percent_only === true` and nothing else. The
    denominator check inside it is §9.3's separate guard ("no percentage
    without a cap"); a version that GATED the rule on a used/limit pair would
    be the heuristic, and it would also break §9.3's omission.
    """

    body = _function_body("usageWindowFigure")
    assert "win.percent_only === true" in body
    # The stamp is on the datum, once, and every layout reads the datum.
    stamp = _function_body("usageMarkPercentOnly")
    assert "percent_only: true" in stamp
    assert "declared !== true" in stamp, (
        "an absent flag must mean 'no rule', never a truthy guess"
    )


def test_every_layout_reads_the_stamped_window_rather_than_re_deciding() -> None:
    """A layout that re-decided would be a sixth answer to a question with one.

    The rule is honoured by the ONE formatter (`usageWindowFigure`) and the ONE
    complete-form list (`usageFigureList`). The five layouts must go through
    them: a renderer that read `row.percentOnlyWindows` itself would be a
    second implementation of the rule, and the two would drift the day one
    changed.

    The check is per-layout rather than a global count. `one_screen`,
    `ledger_rows`, `meter_board`, `dial_cards` and `master_detail` are each
    brace-matched out of the shipped file and must not read the ROW-level flag
    — they draw a window, and the window already knows. What they may do is
    stamp `win.percent_only` onto the DOM as a hook, which is a label on the
    datum rather than a second decision about it.
    """

    text = ADMIN_JS.read_text(encoding="utf-8")
    # The five renderers live inside the `MCCUsageLayouts` IIFE. Bounding the
    # search there keeps `usageDetailBody` and the model builders — which
    # legitimately read the row — out of it.
    start = text.index("window.MCCUsageLayouts = (function () {")
    layouts_block = text[start:]
    # The failure this catches is a LAYOUT READING THE ROW. Calling the shared
    # helper (`usagePercentOnlyNote(row)`, `usageWindowFigure(win)`) is the
    # correct thing and is not matched; naming the flag off the row is.
    offenders = [
        match.group(0)
        for match in re.finditer(
            r"row\.percentOnly(?:Windows|Note)|row\.percent_only|"
            r"\.provider_id === \"commandcode\"|"
            r"\.id === \"commandcode\"",
            layouts_block,
        )
    ]
    assert not offenders, (
        "a layout re-decides the rule instead of reading the window: " + str(offenders)
    )
    # Not vacuous: the renderers really are inside the block that was searched.
    for name in ("function oneScreen", "function ledgerRows", "function dialCards"):
        assert name in layouts_block, name


def test_the_page_and_the_server_agree_on_the_sentence() -> None:
    """One string, declared once. A jsdom fixture that paraphrased it would let
    the card and the docs describe the same omission two ways."""

    js = JSDOM_FIXTURE.read_text(encoding="utf-8")
    declared = source_for(THE_ONE_PROVIDER).percent_only_note
    # The fixture builds the sentence from concatenated literals, so compare
    # the words rather than the source text.
    words = [word for word in declared.split() if len(word) > 3]
    missing = [word for word in words if word not in js]
    assert not missing, missing


def test_the_adapter_explains_why_there_is_no_monthly_grant() -> None:
    """§9.3 requires the implementer to have LOOKED, and to say where.

    The adapter's docstring is where that record lives: the vendor's own field
    names it read, the absence of a grant under eleven candidate spellings, and
    the one figure that exists only as a client-side price table — which the
    spec forbids using. A later agent that "finds" a grant must delete this
    paragraph and its test together.
    """

    docstring = ADAPTER.read_text(encoding="utf-8").split('"""')[1]
    assert "projectUsageView" in docstring
    assert "windowLimits" in docstring
    assert "1.88.2" in docstring, "the bundle version that was read"
    assert "price table" in docstring.lower()
    # The spec's own prohibition, quoted so it cannot be softened by paraphrase.
    assert "Never:" in docstring or "never" in docstring.lower()
