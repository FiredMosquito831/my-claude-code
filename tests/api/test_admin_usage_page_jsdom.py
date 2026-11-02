"""The Usage page (8.3.0, USAGE PR 1), proved in jsdom (scenario: usage_page).

``specs/INVESTIGATION-USAGE-AND-CREDITS.md`` §7 PR 1: a new **Usage** view,
first in OBSERVE, reading ONLY the two payloads MCC already serves -- no new
endpoint, no new credential, **zero new traffic** -- with §4's four honest
states on its two subscription cards and §5.4's one plain sentence for every
other configured provider.

What this file proves, in the order the release's own contract asks for it:

1. **The page exists and is where it was asked for.** First in OBSERVE, above
   Analytics, claiming no settings section.
2. **Opening it adds nothing to the wire.** The harness's fetch stub records
   every URL it answers; ``requests.before == requests.afterOpen ==
   requests.afterRepaint`` is the whole proof, and it is asserted against the
   recorder rather than against a count somebody typed.
3. **The four states, each with a fixture**: measured now, measured at T (stale,
   with its timestamp), not read yet, and the read failed with the reason. §4.2
   also demands that state 3 and state 4 never LOOK alike -- the chip words
   differ, the tones differ, and a failure carries a sentence a reader can act
   on.
4. **Who is on the page.** A provider with a key is listed; one the server does
   not call configured is not on the page at all; the two subscriptions are
   cards rather than "no endpoint" rows; ``openai`` (the same subscription
   behind another name) is not a second row.
5. **Subscription allowance and cash stay apart.** Two blocks, two headings,
   and no element anywhere prints a combined total (§4.3, Q5).
6. **The longest realistic values survive the renderer** (§UI-QUALITY-RULE
   A.3). jsdom has no layout engine, so this half proves the strings; the
   measured overflow table comes from the real-browser run in the release's
   report.

Excluded from the ``pytest`` job and named in the ``jsdom (dashboard)`` job by
``tests/contracts/test_jsdom_ci_contract.py``, which fails if a module that
drives a jsdom harness is not wired in. The two runs are
``MCC_JSDOM_SCENARIO=usage_page`` (``tests/api/admin_jsdom_usage_page.mjs``)
and ``MCC_JSDOM_SCENARIO=usage_views`` (``tests/api/admin_jsdom_usage_views.mjs``),
both through ``tests/api/admin_jsdom_harness.mjs``.
"""

import json
import os
from pathlib import Path

import pytest

from tests.api.test_admin_static_jsdom import _run

#: CI must not pass a suite it never ran. ``_run`` turns a missing node or a
#: missing jsdom into a failure under MCC_CI=1; this module reads the same
#: switch to assert its own run produced measurements rather than skips.
ON_CI = os.environ.get("MCC_CI", "") not in ("", "0", "false")

pytestmark = pytest.mark.xdist_group("admin_static_jsdom")

#: The sentence §5.4 and the user's Q7 fix, character for character.
NO_ENDPOINT_SENTENCE = "publishes no usage endpoint MCC can read."

#: The two admin routes the page draws from. A third would be new traffic.
SOURCE_ROUTES = [
    "/admin/api/anthropic-oauth/sources",
    "/admin/api/chatgpt-oauth/status",
]

#: The base tree (v8.2.0) wrote this, by running the ``usage_views`` scenario
#: against its own ``admin_static`` with the same fixture through the same
#: harness. This release's job is to leave every view it names exactly as it
#: found it.
BASE_BASELINE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "usage_page_dom_8_2_0.json"
)


@pytest.fixture(scope="module")
def usage() -> dict:
    payload = _run(MCC_JSDOM_SCENARIO="usage_page")
    return payload


@pytest.fixture(scope="module")
def usage_views() -> dict:
    views = _run(MCC_JSDOM_SCENARIO="usage_views")
    # Writing the baseline is a deliberate, one-off act: point
    # MCC_JSDOM_USAGE_BASELINE_OUT at a path and run this module against the
    # BASE tree's admin_static. Nothing in CI sets it.
    out = os.environ.get("MCC_JSDOM_USAGE_BASELINE_OUT")
    if out:
        Path(out).write_text(
            json.dumps(
                {
                    "base_commit": "bb85fe79",
                    "written_by": "BASE tree bb85fe79 (v8.2.0), scenario usage_views",
                    "views": views["views"],
                    "nav": [entry["view"] for entry in views["navLabels"]],
                },
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return views


# ------------------------------------------------------------------ clean run


def test_the_usage_scenario_runs_clean(usage) -> None:
    assert usage["fatal"] is None
    assert usage["scenarioFatal"] is None
    assert usage["scriptErrors"] == []
    assert usage["consoleErrors"] == []
    if ON_CI:
        # The other half: the payloads must be real measurements, not an empty
        # dict from a run that never started.
        assert usage["build"]["hasHarness"] is True
        assert usage["cards"]["measured"], "no card was measured at all"


# ------------------------------------------------------------------ position


def test_the_page_is_first_in_observe_above_analytics(usage) -> None:
    """``Usage · Analytics · Token Optimizer · Web Search``, exactly (§5.1).

    Read off the nav the page really drew rather than off ``VIEW_GROUPS``: a
    view in the table that never reached the sidebar would pass a table read
    and fail the only thing the user asked for.
    """

    position = usage["position"]
    assert position["exists"] is True
    assert position["label"] == "Usage"
    assert position["immediatelyBeforeAnalytics"] is True
    assert position["index"] < position["indexOfAnalytics"]
    # And it is the FIRST of the four OBSERVE pages, which is what "first in
    # OBSERVE" means: Get Started and the Setup/Agents/Routing pages stay where
    # the product put them.
    assert position["index"] == usage["viewIds"].index("usage")
    assert position["index"] > usage["viewIds"].index("models")
    assert usage["viewIds"].index("usage") + 1 == usage["viewIds"].index("requests")


def test_the_page_claims_no_settings_section(usage) -> None:
    """It is a readout, not a settings surface.

    A ``sections`` entry would make ``renderSections()`` fill a container on
    this page from the settings payload, which is neither what §7 asks for nor
    what the user approved ("no block on the provider cards").
    """

    assert usage["position"]["claimsNoSection"] is True


def test_the_page_is_a_real_view_with_markup(usage) -> None:
    views = usage["views"]
    assert "usage" in views
    assert views["usage"]["children"] > 0
    assert views["usage"]["length"] > 0


# ------------------------------------------------------- zero new traffic ---


def test_opening_the_page_makes_no_request(usage) -> None:
    """The release's whole contract, measured at the fetch stub.

    ``before`` is the recorder after the page has finished loading, ``afterOpen``
    is after clicking the nav entry, and ``afterRepaint`` is after three more
    renders with a payload already in memory (which is what a theme switch, a
    settings reload or a second account arriving does to the page). All three
    are the same number, and the recorder is the stub that answers every
    request the dashboard makes -- not a counter this scenario keeps itself.
    """

    requests = usage["requests"]
    assert requests["opened"] is True
    assert requests["before"]["calls"] == requests["afterOpen"]["calls"]
    assert requests["before"]["urls"] == requests["afterOpen"]["urls"]
    assert requests["afterOpen"]["calls"] == requests["afterRepaint"]["calls"]
    assert requests["afterOpen"]["urls"] == requests["afterRepaint"]["urls"]
    # A recorder that never recorded anything would pass the equality above
    # vacuously; the page had already made plenty of requests by then.
    assert requests["before"]["calls"] > 0


def test_the_page_names_only_the_two_existing_routes(usage) -> None:
    """No third route can have crept in, because only two are declared."""

    assert usage["requests"]["sources"] == SOURCE_ROUTES


# ------------------------------------------------------------ the four states --


def _cards_by_state(usage: dict) -> dict[str, list[dict]]:
    return usage["cards"]


def test_state_one_a_reading_inside_the_bound_says_measured(usage) -> None:
    cards = _cards_by_state(usage)["measured"]
    assert len(cards) == 2, "a configured subscription with a reading is a card"
    for card in cards:
        assert card["chip"] == "Measured"
        assert card["chipState"] == "measured"
        assert card["chipTone"] == "ok"
        assert card["stateNote"].startswith("Read ")
        assert card["failure"] == ""
        # The number itself, from the provider, with no reformatting that
        # could turn "38.50" into "38.5 %" or into a zero.
        values = [row[1] for block in card["blocks"] for row in block["figures"]]
        assert any(value in {"38.50", "12.00"} for value in values), values


def test_state_two_a_reading_past_the_bound_is_stale_and_says_when(usage) -> None:
    """§4.2: the number, dimmed, with the timestamp and the bound named.

    "Dimmed" is the chip's warn tone; the timestamp is in the note; and the
    note names the bound (30 minutes) rather than saying "stale", which is a
    word a reader would have to decode.
    """

    cards = _cards_by_state(usage)["stale"]
    assert len(cards) == 2
    for card in cards:
        assert card["chip"] == "Stale"
        assert card["chipState"] == "stale"
        assert card["chipTone"] == "warn"
        assert "older than 30 minutes" in card["stateNote"]
        assert "hours ago" in card["stateNote"]
        # The observation instant, in the provider's own clock, is on the card.
        assert "observed " in card["provenance"]
        # And the number is still the provider's number -- stale is not absent.
        values = [row[1] for block in card["blocks"] for row in block["figures"]]
        assert any(value in {"74.00", "41.00"} for value in values), values


def test_state_three_nothing_observed_is_never_a_zero(usage) -> None:
    """The reading case of §4's third state: no header has arrived yet.

    Every figure says "not measured by this provider" and is marked
    ``usage-unmeasured``. The one thing this test is really for is the absence
    of "0", "0 %" or an empty box anywhere on the card.
    """

    cards = _cards_by_state(usage)["unobserved"]
    assert len(cards) == 2
    for card in cards:
        assert card["chip"] == "Not read yet"
        assert card["chipState"] == "not_measured"
        assert card["stateNote"].startswith("Not read yet:")
        assert card["failure"] == ""
        for block in card["blocks"]:
            for _term, value, classes, _title in block["figures"]:
                assert value == "not measured by this provider"
                assert "usage-unmeasured" in classes
                assert value not in {"0", "0 %", "", "—"}
        # The provenance line is mandatory even when there is nothing to show.
        assert card["provenance"].endswith("nothing observed yet")


def test_state_four_a_failed_read_carries_the_reason(usage) -> None:
    """§5.4 "Never": not a blank card, not a spinner, not "no usage API"."""

    cards = _cards_by_state(usage)["failed"]
    assert len(cards) == 2
    reasons = [card["failure"] for card in cards]
    assert any("401 Unauthorized" in reason for reason in reasons), reasons
    assert any("no answer within 30 s" in reason for reason in reasons), reasons
    for card in cards:
        assert card["chip"] == "Read failed"
        assert card["chipState"] == "failed"
        assert card["chipTone"] == "error"
        assert card["failure"].startswith("Could not read usage: ")
        # A failed read is a fact about the read, said out loud, because the
        # opposite reading ("this provider has no usage API") is state 3.
        assert "not about the provider" in card["failure"]
        assert "no reading this time" in card["provenance"]


def test_the_two_failure_states_never_look_like_each_other(usage) -> None:
    """§4.2: "we tried and it broke" is not "it has no usage API".

    Different words, different tones, and a control-free difference a reader
    can see: one card names a reason, the other says nobody has measured.
    """

    sweep = _cards_by_state(usage)
    not_read = sweep["unobserved"][0]
    failed = sweep["failed"][0]
    assert not_read["chip"] != failed["chip"]
    assert not_read["chipTone"] != failed["chipTone"]
    assert not_read["failure"] == "" and failed["failure"] != ""
    assert not_read["stateNote"] != failed["stateNote"]


# ------------------------------------------------------------- who appears ---


def test_every_other_configured_provider_gets_one_sentence(usage) -> None:
    """§5.4/Q7, and only providers holding a credential."""

    providers = usage["providers"]
    assert providers["otherIds"] == ["deepseek", "novita", "cline", "ollama"]
    for item in providers["others"]:
        # One sentence, in the words the user approved.
        assert NO_ENDPOINT_SENTENCE in item["sentence"]
        # No spinner, no retry, no control at all: this is a fact, not a task.
        assert item["controls"] == 0
        assert item["spinner"] == 0


def test_a_provider_with_no_credential_is_not_on_the_page(usage) -> None:
    """The user's constraint: never a row for a provider he does not have.

    ``groq`` and ``cerebras`` are in the fixture's ``provider_status`` with
    ``status: "missing_key"`` -- configured nowhere -- so they must be absent
    from every list on the page rather than present and empty.
    """

    providers = usage["providers"]
    assert "groq" not in providers["otherIds"]
    assert "cerebras" not in providers["otherIds"]
    assert not any("Groq" in title for title in providers["cardTitles"])
    assert not any("Cerebras" in title for title in providers["cardTitles"])


def test_the_subscriptions_are_cards_not_no_endpoint_rows(usage) -> None:
    providers = usage["providers"]
    # Both subscriptions are cards...
    assert len(providers["cardTitles"]) == 2
    # ...and neither is also a "no usage endpoint" row.
    assert "anthropic_oauth" not in providers["otherIds"]
    assert "chatgpt_oauth" not in providers["otherIds"]


def test_the_same_subscription_under_another_name_is_not_a_second_row(
    usage,
) -> None:
    """``openai`` shares CHATGPT_OAUTH_ACCESS_TOKEN with ``chatgpt_oauth``."""

    assert "openai" not in usage["providers"]["otherIds"]


def test_a_provider_listed_after_the_ones_that_report(usage) -> None:
    """§5.2: providers with a reading first, the sentence rows after.

    The markup puts the two sections in that order, and nothing with a reading
    is ever inside the sentence list.
    """

    view_ids = usage["viewIds"]
    assert "usage" in view_ids
    # The list of sentence rows carries no subscription, and the cards carry no
    # sentence row -- which is the same claim read from the DOM.
    providers = usage["providers"]
    for item in providers["others"]:
        assert item["provider"] not in {"anthropic_oauth", "chatgpt_oauth"}


# ------------------------------------------------ allowance and cash, apart ---


def test_a_card_keeps_allowance_and_cash_in_two_blocks(usage) -> None:
    """§4.3/Q5: two blocks, two headings, and NEVER one "remaining" total."""

    blocks = usage["blocks"]
    assert len(blocks) == 2
    by_title = {entry["title"]: entry for entry in blocks}
    chatgpt = next(entry for title, entry in by_title.items() if "ChatGPT" in title)
    # Codex is the case that forces the rule: a percentage window and a credits
    # balance arrive on the same response.
    assert chatgpt["headings"] == ["Subscription allowance", "Credits"]
    anthropic = next(entry for title, entry in by_title.items() if "Anthropic" in title)
    assert anthropic["headings"] == ["Subscription allowance", "Overage"]
    for entry in blocks:
        assert entry["hasSeparateHeadings"] is True
        assert entry["sameHeadingTwice"] is False


def test_no_card_prints_a_combined_remaining_total(usage) -> None:
    """A single "remaining" figure across the two blocks would be the defect
    Part IV.9 exists to prevent, so every card is checked for one."""

    for state in ("measured", "stale", "unobserved", "longest"):
        cards = (
            usage["longest"]["cards"] if state == "longest" else usage["cards"][state]
        )
        for card in cards:
            assert card["hasCombinedTotal"] is False, (state, card["title"])


def test_the_credits_block_says_it_is_not_a_percentage(usage) -> None:
    """A balance has no window, and the words say so rather than the colour."""

    chatgpt = next(
        entry for entry in usage["blocks"] if entry["title"].startswith("ChatGPT")
    )
    # Codex is the card that carries both, so its two notes are the sentence
    # that keeps a percentage and a balance from being read as one figure.
    notes = [
        block["note"]
        for card in usage["cards"]["measured"]
        for block in card["blocks"]
        if card["title"] == chatgpt["title"]
    ]
    assert any("not money" in note for note in notes), notes
    assert any("not added to the windows above" in note for note in notes), notes


# ----------------------------------------------------------------- provenance


def test_every_card_names_its_source_and_when_it_was_observed(usage) -> None:
    """§4.4: a balance read four hours ago is not a live balance."""

    for state in ("measured", "stale", "unobserved", "failed"):
        for card in usage["cards"][state]:
            assert card["provenance"].startswith("Source: ")
            assert (
                "observed " in card["provenance"]
                or "nothing observed yet" in card["provenance"]
                or "no reading this time" in card["provenance"]
            )
    measured = usage["cards"]["measured"][0]
    assert "Anthropic's unified rate-limit response headers" in measured["provenance"]
    assert "observed " in measured["provenance"]


# ---------------------------------------------------------- longest values ---


def test_the_longest_realistic_values_reach_the_page(usage) -> None:
    """§UI-QUALITY-RULE A.3, with the values that rule names.

    A two-decimal percentage, a reset timestamp, ``$4,050.2926`` and a
    60-character account id. A shortened instant keeps its raw value on
    ``title``, so nothing is silently cut.
    """

    longest = usage["longest"]
    assert longest["hasTwoDecimalPercent"] is True
    assert longest["hasDollarFigure"] is True
    assert longest["hasResetTimestamp"] is True
    assert longest["hasLongIdentifier"] is True
    # The widest value the renderer produced, for the report's overflow table.
    assert longest["longestFigureChars"] >= 40


def test_a_long_plan_name_and_a_long_status_wrap_rather_than_break_the_row(
    usage,
) -> None:
    """The renderer must pass them through whole; the CSS wraps them.

    jsdom proves the first half. ``overflow-wrap: anywhere`` on ``dd`` and
    ``min-width: 0`` on the card are what make the second half true, and the
    real-browser run measures it.
    """

    cards = usage["longest"]["cards"]
    values = [
        row[1] for card in cards for block in card["blocks"] for row in block["figures"]
    ]
    assert "max-5x-annual-team-unlimited-seat-enterprise" in values
    assert "$4,050.2926" in values


# ------------------------------------------------- the base-written baseline ---


def test_every_view_the_base_tree_had_is_byte_identical(usage_views) -> None:
    """TODAY MUST RENDER BYTE-IDENTICALLY. This is that proof.

    The baseline is this same scenario run against the BASE tree ``bb85fe79``
    (v8.2.0) with the same fixtures and the same waits. Both digests are
    compared: the raw one, and one with the 8.1.0/7.122.3 whole-page additions
    normalised out on both sides (``usageNormalise``) so a later release's
    strip cannot hide a real change on the page underneath it.

    This release adds ONE page. Everything else is what it was.
    """

    baseline = json.loads(BASE_BASELINE.read_text(encoding="utf-8"))
    assert baseline["base_commit"] == "bb85fe79"
    assert "bb85fe79" in baseline["written_by"]
    base = baseline["views"]
    mine = usage_views["views"]

    #: Views this release DELIBERATELY changed after the baseline was taken.
    #: `guide` is the one: a new page is a documented feature, and the Guide's
    #: task section for it is that documentation (the release's own scope, §7
    #: PR 1: "a page entry in docs/, README.md, USAGE, the Guide task section").
    #: Nothing else may be here.
    DECLARED_MOVES = {"guide"}

    # Exactly one view is new, it is `usage`, and nothing was removed.
    assert sorted(set(mine) - set(base)) == ["usage"]
    assert sorted(set(base) - set(mine)) == []

    moved = sorted(
        view for view in base if mine[view]["sha256"] != base[view]["sha256"]
    )
    assert moved == sorted(DECLARED_MOVES), (
        "these views are not what the base tree drew, and are not declared "
        f"moves: {sorted(set(moved) - DECLARED_MOVES)}"
    )
    # The declared move really is the Guide's new section and nothing else --
    # the normalised digest (which strips the 8.1.0 strip and 7.122.3 chips)
    # moves for the same single view.
    assert sorted(
        view
        for view in base
        if mine[view]["sha256Normalised"] != base[view]["sha256Normalised"]
    ) == sorted(DECLARED_MOVES)


def test_the_equality_comparison_is_not_vacuous(usage_views) -> None:
    """The comparison above would also pass if nothing had a digest."""

    baseline = json.loads(BASE_BASELINE.read_text(encoding="utf-8"))
    assert len(baseline["views"]) == 15
    for view, entry in baseline["views"].items():
        assert entry["length"] > 0, view
        assert len(entry["sha256"]) == 64, view
    # And the branch's own run really carried content, including the new page.
    assert usage_views["views"]["usage"]["length"] > 0
    assert usage_views["views"]["requests"]["length"] > 10000


def test_the_only_navigation_change_is_the_new_entry(usage_views) -> None:
    """One link added, immediately before Analytics, and no other moved."""

    baseline = json.loads(BASE_BASELINE.read_text(encoding="utf-8"))
    base_nav = baseline["nav"]
    mine_nav = [entry["view"] for entry in usage_views["navLabels"]]
    expected = list(base_nav)
    expected.insert(expected.index("requests"), "usage")
    assert mine_nav == expected
