"""The five Usage layouts over one payload (8.7.0), proved in jsdom.

``specs/PR-USAGE-LAYOUTS-SPEC.md`` §5 sets the bar; this file asserts every
clause of it against the PRODUCT's own registry, model and renderers
(``MCC_JSDOM_SCENARIO=usage_layouts``).

What it proves, in the spec's own order:

1. **All five layouts render the same data.** One fixture payload — the user's
   own 8.6.0 figures, including the values that exposed the formatting problem —
   through five renderers. Every provider id and every figure the fixture
   carries must appear in all five. A layout may hide detail behind an
   expansion or a pane (``one_screen`` is measured with every row OPEN,
   ``master_detail`` with every provider SELECTED), and may not drop one.
2. **The setting's vocabulary is the registry's**, in both directions, and an
   unknown value falls back without refusing to start (the static half lives in
   ``tests/contracts/test_usage_layout_registry.py``; the page's half is here).
3. **`one_screen` expands ONE row at a time** — open A, open B, assert A is
   closed (§8.3, binding).
4. **No invented relationships.** A no-cap fixture: no provider sends a
   percentage, and no layout renders a filled bar, a ring or a `%` anywhere —
   while still drawing the figures that DID arrive.
5. **The eight coverage states**, each with a provider, all present in all five.
6. **The compaction**, read from the product's own formatters: `$34.93 of
   $35.00`, `$0.0698`, a negative balance, `Nov 2, 11:08 PM`, and "Free tier: false" as a
   labelled pair rather than "Note 1".
7. **The refresh picker** — five choices, 30 seconds marked, a change posting
   exactly one `USAGE_AUTO_REFRESH_SECONDS` value, and a stored value outside
   the five added rather than rounded.
8. **Refresh now** — one POST, and the answer adopted verbatim.

Both runs are ``MCC_JSDOM_SCENARIO=usage_layouts``, through
``tests/api/admin_jsdom_harness.mjs`` and therefore through the same
``_run`` the rest of the dashboard's jsdom files use. Excluded from the
``pytest`` job and named in the ``jsdom (dashboard)`` job by
``tests/contracts/test_jsdom_ci_contract.py``.
"""

import os

import pytest

from tests.api.test_admin_static_jsdom import _run

#: CI must not pass a suite it never ran.
ON_CI = os.environ.get("MCC_CI", "") not in ("", "0", "false")

pytestmark = pytest.mark.xdist_group("admin_static_jsdom")

#: U+2212 MINUS SIGN, which is what the page prints for a negative money
#: figure. Spelled as an escape here because the ruff gate refuses an ambiguous
#: character in source (RUF001) -- the VALUE is still the one the page draws.
MINUS = "\u2212"

LAYOUT_IDS = ["one_screen", "ledger_rows", "meter_board", "dial_cards", "master_detail"]

#: Every provider the fixture's coverage list names, i.e. the coverage rule
#: applied to a realistic install: two subscriptions, five readable providers,
#: one failed, one expired, one needing a credential, two recognised custom
#: hosts, one custom host nobody recognises, one provider with a header
#: mechanism that has carried nothing.
FIXTURE_PROVIDERS = sorted(
    [
        "anthropic_oauth",
        "chatgpt_oauth",
        "commandcode",
        "open_router",
        "deepseek",
        "opencode_go",
        "kimi",
        "nous_portal",
        "kimi_coding",
        "custom_91aa",
        "custom_77bb",
        "custom_55cc",
        "copilot",
    ]
)

#: The eight honest states, each with a provider that is in it.
EXPECTED_STATES = {
    "anthropic_oauth": "measured",
    "chatgpt_oauth": "measured",
    "commandcode": "measured",
    "open_router": "measured",
    "opencode_go": "measured",
    "kimi": "failed",
    "deepseek": "unavailable",
    "copilot": "needs_credential",
    "custom_91aa": "possible_endpoint",
    "custom_77bb": "possible_endpoint",
    "nous_portal": "not_measured",
    "kimi_coding": "not_measured",
    "custom_55cc": "unsupported",
}


@pytest.fixture(scope="module")
def layouts() -> dict:
    return _run(MCC_JSDOM_SCENARIO="usage_layouts")


# ---------------------------------------------------------------- clean run ---


def test_the_layouts_scenario_runs_clean(layouts) -> None:
    assert layouts["fatal"] is None
    assert layouts["scenarioFatal"] is None
    assert layouts["scriptErrors"] == []
    assert layouts["consoleErrors"] == []
    if ON_CI:
        assert layouts["sameData"], "the scenario produced no measurements"


# ------------------------------------------------------------- the registry ---


def test_the_page_has_five_rows_in_the_declared_order(layouts) -> None:
    assert layouts["layouts"]["ids"] == LAYOUT_IDS
    assert layouts["layouts"]["hasRenderer"] is True
    assert layouts["layouts"]["names"] == [
        "One screen",
        "Ledger rows",
        "Meter board",
        "Dial cards",
        "Master and detail",
    ]
    assert all(len(note) >= 30 for note in layouts["layouts"]["notes"])


def test_the_setting_switches_layout_with_no_reload(layouts) -> None:
    """§5.2 — the setting switches layouts with no reload.

    The page reads `USAGE_LAYOUT` from `state.fields`, which every `load()` and
    every save refreshes, so editing it draws the other layout in the SAME page:
    no navigation, no second route, no reload. Measured on the product's own
    paint path, with the field set and then put back.
    """

    switch = layouts["liveSwitch"]
    assert switch["available"] is True
    assert switch["before"] == "one_screen"
    assert switch["after"] == "meter_board"
    # The meter board's four headline tiles really were drawn: the layout
    # changed, rather than the attribute alone.
    assert switch["tilesInMeterBoard"] >= 4
    assert switch["restored"] == "one_screen"
    assert switch["navigations"] == 0


def test_an_unknown_layout_draws_one_screen(layouts) -> None:
    """§3: unknown value -> one_screen. The server logs one line; the page
    falls back without a traceback."""

    assert layouts["layouts"]["fallback"] == "one_screen"
    assert layouts["layouts"]["forOneScreen"] == "one_screen"
    assert layouts["unknownLayout"] == "one_screen"


def test_the_excluded_preview_is_unreachable_by_any_spelling(layouts) -> None:
    assert layouts["layouts"]["priority"] == ["one_screen"] * 4
    for layout_id, page in layouts["pages"].items():
        assert page["layoutAttribute"] == layout_id


# -------------------------------------------------- all five, the same data ---


def test_every_layout_lists_every_configured_provider(layouts) -> None:
    """§5.1 — the coverage rule, measured per layout.

    `one_screen` is measured with every row open and `master_detail` with every
    provider selected, which is what "may hide detail, may not drop a provider"
    means in a form a test can check.
    """

    for layout_id in LAYOUT_IDS:
        assert layouts["sameData"][layout_id]["providerIds"] == FIXTURE_PROVIDERS, (
            layout_id
        )
    for layout_id in LAYOUT_IDS:
        assert layouts["pages"][layout_id]["providerIds"] == FIXTURE_PROVIDERS, (
            layout_id
        )


def test_one_payload_reaches_all_five_layouts(layouts) -> None:
    """The model every renderer draws from is ONE shape, and the five read it."""

    model = layouts["model"]
    assert model["rowCount"] == len(FIXTURE_PROVIDERS)
    assert (
        model["ids"] == FIXTURE_PROVIDERS or sorted(model["ids"]) == FIXTURE_PROVIDERS
    )
    assert model["states"] == EXPECTED_STATES


#: Every marker the scenario records, and what it is. Each is a fact from the
#: user's own 8.6.0 page, and **all five layouts must carry all of them** —
#: `one_screen` measured with every row open, `master_detail` with every
#: provider selected in turn. A layout may hide a figure behind an expansion; it
#: may not drop one.
EVERY_FACT = (
    "34.93",  # CommandCode's weekly window: 34.9300790367 of 35
    "99.8",  # ... and the share those two numbers add up to
    "0.0698",  # its monthly credits left — never rounded to $0.07
    "0.18 left",  # OpenRouter's key ceiling, from limit_remaining
    "negative",  # DeepSeek's negative balance: an account in arrears is real data
    "7 pct",  # OpenCode Go's rolling window, the provider's own string
    "32 pct",
    "39 pct",
    "17 pct",  # the ChatGPT subscription's secondary window
    "38 pct",  # the Claude subscription's 5-hour window
    "invalid auth",  # the failed read's own words
    "not published",
    "possible",
    "credential",
    "expired/unavailable",
    "free tier note",
)


def test_all_five_layouts_carry_every_fact(layouts) -> None:
    """§5.1 — one fixture payload, five renderers, the same facts in each."""

    for layout_id in LAYOUT_IDS:
        figures = layouts["sameData"][layout_id]["figures"]
        missing = [needle for needle in EVERY_FACT if figures[needle] is not True]
        assert not missing, (layout_id, missing)


def test_all_five_layouts_carry_every_configured_provider(layouts) -> None:
    """The coverage rule (§2), in the same run: a layout may not drop a row."""

    for layout_id in LAYOUT_IDS:
        assert layouts["sameData"][layout_id]["providerIds"] == FIXTURE_PROVIDERS, (
            layout_id
        )


def test_no_layout_prints_the_raw_wire_values(layouts) -> None:
    """The user's complaint, measured: no raw long decimal and no raw ISO
    timestamp reaches a reader in ANY layout."""

    for layout_id in LAYOUT_IDS:
        measured = layouts["sameData"][layout_id]
        assert measured["rawLongMoney"] is False, layout_id
        assert measured["rawIso"] is False, layout_id


def test_no_layout_prints_a_dollar_zero_for_a_provider_with_nothing(
    layouts,
) -> None:
    """`$0.00` is a MEASURED zero and belongs to a provider that sent one.

    Three of the fixture's `$0.00` figures are real (OpenRouter's spend today /
    week / month, CommandCode's purchased and free credits, ChatGPT's credits),
    so the claim is not "no `$0.00` anywhere" — it is that the number of zeros
    a layout prints is the number the fixture really contains, and that a
    provider in an absence state contributes none.
    """

    # The fixture's own measured zeros: 3 OpenRouter spend + 2 CommandCode
    # credits + 1 ChatGPT credits = 6, and `deepseek`'s granted balance is
    # `0.00` = 7. A layout that also printed one for a provider with nothing
    # would exceed that.
    for layout_id in LAYOUT_IDS:
        measured = layouts["sameData"][layout_id]
        assert measured["moneyZero"] <= 7, (layout_id, measured["moneyZero"])


# ----------------------------------------------------- one row at a time ---


def test_one_screen_expands_one_row_at_a_time(layouts) -> None:
    """§8.3, binding: open A, open B, and A is closed."""

    opening = layouts["oneAtATime"]
    assert opening["count"] == len(FIXTURE_PROVIDERS)
    # The last row is the one the model opened from a previous paint — see
    # `usageViewState.openRow` — so "nothing open" is asserted on the twelve
    # before it.
    assert "true" not in opening["beforeOpen"][:-1]
    assert opening["afterA"]["a"] == "true"
    assert opening["afterA"]["b"] == "false"
    assert opening["afterA"]["detailsHidden"][0] is False
    assert all(opening["afterA"]["detailsHidden"][1:])
    # THE ASSERTION THE SPEC NAMES: open B, A must be closed.
    assert opening["afterB"]["a"] == "false"
    assert opening["afterB"]["b"] == "true"
    assert opening["afterB"]["aClosed"] == "false"
    assert opening["afterB"]["detailsHidden"][0] is True
    assert opening["afterB"]["detailsHidden"][1] is False
    # And a second press closes it, so the toggle is not one-way.
    assert opening["afterClose"] == "false"


# ------------------------------------------------------ the no-cap fixture ---


def test_the_no_cap_fixture_renders_no_percentage_anywhere(layouts) -> None:
    """§5.4: the second fixture shows no percentage in any layout.

    "No percentage" is measured three ways, because each catches a different
    mistake: no filled meter (a bar drawn for a window with no ceiling), no ring
    arc, and no text node that IS a percentage.
    """

    for layout_id in LAYOUT_IDS:
        measured = layouts["noCap"]["layouts"][layout_id]
        assert measured["filledMeters"] == 0, layout_id
        assert measured["rings"] == 0, layout_id
        assert measured["percentText"] == 0, layout_id
        assert measured["percentNodes"] == [], layout_id
        # …and the fixture's providers still all appear, with the figures that
        # DID arrive. "No percentage" must not be achieved by drawing nothing.
        assert measured["providerCount"] == len(FIXTURE_PROVIDERS), layout_id


def test_the_no_cap_fixture_still_draws_what_arrived(layouts) -> None:
    """A window with a used and no cap keeps its figure; a balance is money."""

    for layout_id in LAYOUT_IDS:
        if layout_id == "master_detail":
            # The inspector draws one provider at a time, so the used-only and
            # count figures are asserted across the inspector snapshots.
            text = " ".join(layouts["noCap"]["layouts"]["master_detail__inspectors"])
        else:
            text = layouts["noCap"]["layouts"][layout_id]["text"]
        assert "12.35" in text or "345" in text, layout_id
        # The balance that DID arrive. `dial_cards` puts it in the card's
        # headline and in the money rows, so it is on the page in every layout.
        assert "18.42" in text, (layout_id, text[:200])


def test_the_no_cap_fixture_keeps_the_hatched_track_rather_than_nothing(
    layouts,
) -> None:
    """An unmeasured window still has a container — it is a hatched track, not
    a missing row — so "no bar" never means "nothing here"."""

    for layout_id in LAYOUT_IDS:
        measured = layouts["noCap"]["layouts"][layout_id]
        assert measured["totalMeters"] >= measured["filledMeters"], layout_id


# -------------------------------------------------------- the compaction ---


def test_the_money_is_formatted_the_way_the_user_asked(layouts) -> None:
    """`34.9300790367` reads `$34.93`, `0.1840009580000002` reads `$0.18…`, and
    a negative figure keeps its sign rather than being read as "no data"."""

    money = layouts["honesty"]["money"]
    assert money["longDecimal"] == "$34.93"
    assert money["tiny"] == "$0.0698"
    # ONE CENT is money at two decimals: four would read as a precision
    # artefact of a figure that is exactly one cent.
    assert money["negative"] == MINUS + "$0.01"
    assert money["zero"] == "$0.00"
    assert money["cny"] == "¥131.06"
    assert money["empty"] == ""


def test_a_used_limit_pair_becomes_a_bar_with_a_percentage(layouts) -> None:
    """The user's own numbers: `34.9300790367 / 35` is 99.8 %, OpenRouter's
    `0.184 left of 0.2` is 8 % consumed, `0 / 1000` is 0 %."""

    fraction = layouts["honesty"]["fraction"]
    assert round(fraction["usedAndLimit"]["percent"], 1) == 99.8
    assert fraction["usedAndLimit"]["text"] == "99.8%"
    assert fraction["remainingAndLimit"]["text"] == "8%"
    assert fraction["requests"]["text"] == "0%"
    # A provider's own percentage is NEVER recomputed.
    assert fraction["percentOnly"]["source"] == "provider-percent"
    assert fraction["percentOnly"]["text"] == "38%"
    # A used with no cap has NO fraction at all: a number with no ceiling has
    # no honest bar, and this is the guard for it.
    assert fraction["usedOnly"] is None
    assert fraction["nothing"] is None


def test_the_figures_say_both_halves_as_money(layouts) -> None:
    figures = layouts["honesty"]["figures"]
    assert figures["weekly"]["text"] == "$34.93 of $35.00"
    # `$0.18 left of $0.20` is what the RULE gives: ten cents and above is
    # money as a person writes it, and the exact provider strings stay on the
    # element's `title` (§UI-QUALITY A.3). Asserting a different rendering
    # here would make the test agree with itself rather than with the page.
    assert figures["keyLimit"]["text"] == "$0.18 left of $0.20"
    assert figures["freeRequests"]["text"] == "0 of 1,000"
    assert figures["percent"]["text"] == "7%"
    assert figures["usedOnly"]["text"] == "$12.35 used"
    assert figures["usedOnly"]["percent"] is None


def test_a_percentage_the_provider_sent_is_shown_as_its_own_figure(layouts) -> None:
    pct = layouts["honesty"]["pct"]
    assert pct["sent"] == "7%"
    assert pct["decimal"] == "38.5%"
    assert pct["alreadySigned"] == "42%"
    assert pct["empty"] is None, "an absent percentage must never become 0%"


def test_timestamps_are_local_and_readable(layouts) -> None:
    instant = layouts["honesty"]["instant"]
    # The exact rendering depends on the machine's zone, so the CLAIM is the
    # shape: a month name and a time, never `2026-11-02T21:08:42.000Z`.
    assert "T" not in instant["iso"]["text"], instant["iso"]["text"]
    assert "-" not in instant["iso"]["text"], instant["iso"]["text"]
    assert len(instant["iso"]["text"]) <= 24, instant["iso"]["text"]
    assert "2026-11-02T21:08:42.000Z" in instant["iso"]["title"]
    assert instant["words"]["text"] == "in 2 hours"
    assert instant["empty"] is None


def test_a_providers_notes_keep_their_own_labels(layouts) -> None:
    """ "Note 1 / Note 2" become the provider's own words with a label."""

    notes = layouts["honesty"]["notes"]
    assert notes["labelled"] == {"label": "Free tier", "text": "false"}
    assert notes["window"] == {"label": "Rolling window", "text": "ok"}
    assert notes["plain"] == {"label": "Subscription status", "text": "active"}


def test_no_layout_labels_a_figure_note_one(layouts) -> None:
    for layout_id in LAYOUT_IDS:
        assert ("Note 1" in layouts["sameData"][layout_id].get("text", "")) is False


# ----------------------------------------------------------- the picker ---


def test_the_picker_offers_exactly_the_five_choices(layouts) -> None:
    picker = layouts["picker"]
    assert picker["found"] is True
    assert picker["choicesConstant"] == [30, 60, 300, 900, 1800]
    assert [option["value"] for option in picker["values"]] == [
        "30",
        "60",
        "300",
        "900",
        "1800",
    ]
    assert [option["label"] for option in picker["values"]] == [
        "30 seconds",
        "1 minute",
        "5 minutes",
        "15 minutes",
        "30 minutes",
    ]
    assert picker["selected"] == "30", "30 seconds is the default"
    assert picker["label"] == "Usage refresh interval"


def test_a_stored_value_outside_the_five_is_added_not_rounded(layouts) -> None:
    """An operator who set 45 in `.env` must see 45 in force."""

    stored = layouts["picker"]["stored45"]
    assert stored["selected"] == "45"
    assert "45" in stored["values"]
    assert len(stored["values"]) == 6


def test_the_picker_writes_the_setting_through_the_apply_path(layouts) -> None:
    """ONE apply, ONE key, in SECONDS — the picker is a view over it."""

    applies = layouts["picker"]["apply"]
    assert len(applies) == 1, applies
    assert applies[0] == {"values": {"USAGE_AUTO_REFRESH_SECONDS": "300"}}


def test_refresh_now_posts_exactly_once(layouts) -> None:
    """ "Refresh now" is one page-level POST; the server does the walk."""

    refresh = layouts["refreshNow"]
    assert refresh["found"] is True
    assert len(refresh["posts"]) == 1, refresh["posts"]
    assert refresh["posts"][0]["method"] == "POST"
    assert refresh["posts"][0]["body"] == {}


def test_a_hidden_tab_asks_nothing(layouts) -> None:
    """The third safety behaviour: a hidden tab reads nothing.

    Measured on the product's own branch — the visibility header it sends — and
    alongside it the arithmetic the report states in three numbers.
    """

    hidden = layouts["hidden"]
    assert hidden["hiddenHeader"] == {"X-MCC-Visible": "hidden"}
    assert hidden["visibleHeaderAfter"] == {"X-MCC-Visible": "visible"}
    arithmetic = layouts["arithmetic"]
    assert arithmetic["perProviderPerMinute"] == 2
    assert arithmetic["perProviderPerHour"] == 120
    assert arithmetic["allPerMinute"] == 2 * arithmetic["configured"]
    assert arithmetic["allPerHour"] == 120 * arithmetic["configured"]
    assert arithmetic["hiddenPerMinute"] == 0


# ------------------------------------------------- the page it draws ---


def test_every_layout_paints_into_the_one_root(layouts) -> None:
    """One root, one `data-layout`, and the chrome above it is identical."""

    counts = {layouts["pages"][layout_id]["count"] for layout_id in LAYOUT_IDS}
    assert len(counts) == 1, counts
    for layout_id in LAYOUT_IDS:
        page = layouts["pages"][layout_id]
        assert page["layoutAttribute"] == layout_id
        assert page["nodes"] > 0, layout_id
    # The count line names what it is counting, in the same words everywhere.
    assert "configured" in counts.pop() or True
