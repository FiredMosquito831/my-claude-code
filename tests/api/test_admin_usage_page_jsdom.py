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

#: The base tree (v8.3.0) wrote this, by running the ``usage_views`` scenario
#: against its own ``admin_static`` with the same fixture and the same harness
#: run (``MCC_JSDOM_SCENARIO=usage_views``, output captured, ``views`` and
#: ``navLabels`` written verbatim). 8.4.0's job is to leave every view it names
#: exactly as it found it, with the ONE declared exception below.
#:
#: Re-taken from 8.3.0 rather than 8.2.0 because the Usage page itself is 8.3.0's
#: work: comparing against 8.2.0 would report the page's own existence as a
#: change and hide whatever this release did to it.
BASE_BASELINE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "usage_page_dom_8_3_0.json"
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
    # BASE tree's admin_static. Nothing in CI sets it. (8.4.0 wrote
    # ``usage_page_dom_8_3_0.json`` this way from the v8.3.0 release worktree.)
    out = os.environ.get("MCC_JSDOM_USAGE_BASELINE_OUT")
    if out:
        Path(out).write_text(
            json.dumps(
                {
                    "base_commit": "84d82c5a",
                    "written_by": "BASE tree 84d82c5a (v8.3.0), scenario usage_views",
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


def test_opening_the_page_makes_exactly_one_local_request(usage) -> None:
    """The contract, measured at the fetch stub, updated for 8.4.0.

    8.3.0's promise was "opening this page adds no request". 8.4.0 adds one and
    only one: ``GET /admin/api/usage``, which reads THIS SERVER'S MEMORY and
    sends nothing upstream. The promise that survives unchanged is the one that
    mattered -- the page never *asks a provider* for anything -- and it is what
    the assertions below hold to: exactly one URL on the first open, the usage
    route and nothing else, and none at all on the repaints that follow, so a
    theme switch or a settings reload costs nothing.
    """

    requests = usage["requests"]
    assert requests["opened"] is True
    assert requests["addedOnOpen"] == ["/admin/api/usage"]
    assert requests["addedOnRepaint"] == []
    # And nothing else moved, so the one new URL is the whole difference.
    assert requests["before"]["calls"] + 1 == requests["afterOpen"]["calls"], requests
    assert requests["afterOpen"]["calls"] == requests["afterRepaint"]["calls"]
    assert requests["afterOpen"]["urls"] == requests["afterRepaint"]["urls"]
    # A recorder that never recorded anything would pass the equality above
    # vacuously; the page had already made plenty of requests by then.
    assert requests["before"]["calls"] > 0


def test_the_page_names_only_the_two_existing_routes(usage) -> None:
    """The two SUBSCRIPTION payloads are still the only ones the page reuses.

    8.4.0 adds a third source of figures, and it is a different thing: a read of
    this server's own memory rather than a payload the Providers page already
    pulls. That one is asserted separately, by ``addedOnOpen`` in
    ``test_opening_the_page_makes_exactly_one_local_request`` -- so this list
    staying at two is the claim that no subscription reading gained a route of
    its own.
    """

    assert usage["requests"]["sources"] == SOURCE_ROUTES


# ------------------------------------------------------------ the four states --


#: The two subscriptions' titles as the fixture renders them. The sweep below
#: is about THEM: 8.7.0 draws every configured provider in one list, so "the
#: cards in the measured state" is no longer exactly two — the fixture also
#: configures DeepSeek, Cline, Novita and Ollama.
SUBSCRIPTION_TITLES = (
    "Anthropic Claude subscription (OAuth, Caution)",
    "ChatGPT OAuth (experimental)",
)


def _subscription_cards(usage: dict, state: str) -> list[dict]:
    return [
        card for card in usage["cards"][state] if card["title"] in SUBSCRIPTION_TITLES
    ]


def _cards_by_state(usage: dict) -> dict[str, list[dict]]:
    return usage["cards"]


def test_state_one_a_reading_inside_the_bound_says_measured(usage) -> None:
    cards = _subscription_cards(usage, "measured")
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
        assert any(value in {"38.50%", "12.00%"} for value in values), values


def test_state_two_a_reading_past_the_bound_is_stale_and_says_when(usage) -> None:
    """§4.2: the number, dimmed, with the timestamp and the bound named.

    "Dimmed" is the chip's warn tone; the timestamp is in the note; and the
    note names the bound (30 minutes) rather than saying "stale", which is a
    word a reader would have to decode.
    """

    cards = _subscription_cards(usage, "stale")
    assert len(cards) == 2
    for card in cards:
        assert card["chip"] == "Stale"
        assert card["chipState"] == "stale"
        assert card["chipTone"] == "warn"
        assert "older than 30 minutes" in card["stateNote"]
        assert "hours ago" in card["stateNote"]
        # The observation instant, in words: "read 4 hours ago".
        assert "read " in card["provenance"] and "ago" in card["provenance"]
        # And the number is still the provider's number -- stale is not absent.
        values = [row[1] for block in card["blocks"] for row in block["figures"]]
        assert any(value in {"74.00%", "41.00%"} for value in values), values


def test_state_three_nothing_observed_is_never_a_zero(usage) -> None:
    """The reading case of §4's third state: no header has arrived yet.

    Every figure says "not measured by this provider" and is marked
    ``usage-unmeasured``. The one thing this test is really for is the absence
    of "0", "0 %" or an empty box anywhere on the card.
    """

    cards = _subscription_cards(usage, "unobserved")
    assert len(cards) == 2
    for card in cards:
        assert card["chip"] == "Not read yet"
        assert card["chipState"] == "not_measured"
        assert card["stateNote"].startswith("Not read yet")
        assert card["failure"] == ""
        for block in card["blocks"]:
            for _term, value, classes, _title in block["figures"]:
                assert value == "not measured by this provider"
                assert "usage-unmeasured" in classes
                assert value not in {"0", "0 %", "", "—"}
        # The provenance line is mandatory even when there is nothing to show.
        assert card["provenance"].endswith("nothing read yet")


def test_state_four_a_failed_read_carries_the_reason(usage) -> None:
    """§5.4 "Never": not a blank card, not a spinner, not "no usage API"."""

    cards = _subscription_cards(usage, "failed")
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

    not_read = _subscription_cards(usage, "unobserved")[0]
    failed = _subscription_cards(usage, "failed")[0]
    assert not_read["chip"] != failed["chip"]
    assert not_read["chipTone"] != failed["chipTone"]
    assert not_read["failure"] == "" and failed["failure"] != ""
    assert not_read["stateNote"] != failed["stateNote"]


# ------------------------------------------------------------- who appears ---


def test_every_other_configured_provider_gets_one_sentence(usage) -> None:
    """§5.4/Q7, and only providers holding a credential.

    ``deepseek`` left this list in 8.4.0: it has a header mechanism, so it is
    a CARD in the "From your own traffic" block rather than a sentence here.
    One row per provider, and the row that exists is the one with something to
    say. The rest are still exactly one sentence each, with no control of any
    kind.
    """

    providers = usage["providers"]
    assert providers["otherIds"] == ["cline", "novita", "ollama"]
    for item in providers["others"]:
        # One sentence, in the words the user approved. Since 8.7.0 it is the
        # row's state sentence rather than a separate sentence span -- the same
        # words, in the page's one vocabulary.
        assert NO_ENDPOINT_SENTENCE in item["sentence"], item["provider"]
        # No spinner, and no control that could be pressed: a provider with
        # nothing to read draws a DISABLED Read now (which says why) and
        # nothing else.
        assert item["spinner"] == 0
        # …AND the one control it draws is disabled with a reason. 8.7.0
        # gives every row a Read now so the control exists everywhere; a row
        # with nothing to read gets it disabled and says why in its `title`.
        assert item["enabled_controls"] <= 1
        assert item["disabled_read_now"] is True
        assert item["spinner"] == 0


def test_a_provider_with_a_mechanism_is_a_card_not_a_sentence(usage) -> None:
    """The one row per provider rule, from the other side of the split."""

    captured = usage["captured"]
    # EVERY configured provider is drawn now (8.7.0's coverage rule), and
    # DeepSeek is one of them rather than the only one.
    assert "DeepSeek" in captured["shownFor"]
    assert "deepseek" not in usage["providers"]["otherIds"]
    # A reading the server holds for a provider this install does not have is
    # NOT drawn: the page is about what you configured.
    assert "Nous Portal" not in captured["titles"]


def test_the_captured_block_renders_every_state_the_server_can_send(usage) -> None:
    """The four states plus an unknown word, mapped rather than guessed."""

    captured = usage["captured"]
    mapping = {item["serverState"]: item["kind"] for item in captured["stateFixtures"]}
    assert mapping["measured"] == "measured"
    assert mapping["stale"] == "stale"
    assert mapping["not_measured"] == "not_measured"
    assert mapping["unsupported"] == "unsupported"
    # A word the page has never seen must not be rendered as a measurement.
    assert mapping["something_new"] == "not_measured"


def test_a_provider_that_sent_nothing_says_not_read_rather_than_zero(usage) -> None:
    card = next(
        entry
        for entry in usage["captured"]["unreadCard"]
        if entry["chipState"] == "not_measured"
    )
    assert card["chip"] == "Not read yet"
    assert card["chipState"] == "not_measured"
    # No block at all, because there is no value to put in one -- and no "0".
    assert card["blocks"] == []
    # Either sentence is honest, and which one depends on whether this
    # release has a read for the provider: "fills in on the next request" for
    # a header mechanism, "no read in this release" for one with none.
    assert "Not read yet" in card["stateNote"]


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
    # Both subscriptions are among the cards. Since 8.7.0 EVERY configured
    # provider is one, so the count is no longer the claim -- presence is.
    for spec in ("Claude", "ChatGPT"):
        assert any(spec in title for title in providers["cardTitles"]), spec
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
    # EVERY configured provider is a card now (8.7.0's coverage rule), so the
    # claim is not "two cards" -- it is that the card carrying BOTH a window
    # and a balance keeps them in two blocks.
    by_title = {entry["title"]: entry for entry in blocks}
    chatgpt = next(entry for title, entry in by_title.items() if "ChatGPT" in title)
    # Codex is the case that forces the rule: a percentage window and a credits
    # balance arrive on the same response.
    # The page's own two headings. "Allowance" rather than "subscription
    # allowance" is deliberate and long-standing: a card cannot claim a
    # subscription its provider never named.
    assert chatgpt["headings"] == ["Allowance", "Balance and spend"]
    anthropic = next(entry for title, entry in by_title.items() if "Anthropic" in title)
    # "Allowance", not "subscription allowance": the page cannot claim a
    # subscription its provider never named. The overage figures live in the
    # cash block and in the provider's notes, which is where a reader finds
    # them (8.3.0 gave them a heading of their own; 8.7.0 keeps the facts and
    # lets every layout draw them).
    assert anthropic["headings"] == ["Allowance"]
    # A card only has "separate headings" when it HAS two kinds of figure.
    # Every card that does keeps them apart; no card ever repeats a heading.
    for entry in blocks:
        assert entry["sameHeadingTwice"] is False, entry["title"]
    assert chatgpt["hasSeparateHeadings"] is True


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
    # The money block says, in words, that it is money and that nothing
    # there is added to the allowance. The exact sentence is the page's; the
    # claim is that the words are present.
    joined = " ".join(notes)
    assert "not a percentage" in joined, notes
    assert "added to the allowance" in joined or "added to the windows" in joined, notes


# ----------------------------------------------------------------- provenance


def test_every_card_names_its_source_and_when_it_was_observed(usage) -> None:
    """§4.4: a balance read four hours ago is not a live balance."""

    for state in ("measured", "stale", "unobserved", "failed"):
        for card in usage["cards"][state]:
            # The source, then when it was read. 8.7.0 dropped the literal
            # word "Source:" for the source itself, which is the same fact with
            # one less label a reader has to parse.
            assert card["provenance"], (state, card["title"])
            # A source is always named: a route (`host/path`) or the header
            # family that carried the figures. The page's own words for the
            # two subscriptions are "Anthropic's unified rate-limit response
            # headers" and "Codex's own response headers".
            assert (
                "/" in card["provenance"]
                or "header" in card["provenance"]
                or "publishes no usage endpoint" in card["stateNote"]
                or card["provenance"].startswith("no read for this provider")
                or "nothing" in card["provenance"]
            ), (state, card["title"], card["provenance"])
            # WHEN, in words: "read 2 minutes ago", or the honest
            # "no reading this time" for a read that did not happen.
            assert (
                "ago" in card["provenance"]
                or "no reading this time" in card["provenance"]
                or "nothing read yet" in card["provenance"]
            ), (state, card["provenance"])
    measured = usage["cards"]["measured"][0]
    assert "Anthropic's unified rate-limit response headers" in measured["provenance"]
    # WHEN, in words: "read 2 minutes ago". The raw epoch is not what a
    # reader asked for, and 8.7.0 stopped printing it.
    assert "read " in measured["provenance"] and "ago" in measured["provenance"]


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
    # A reset is drawn in the reader's own clock on the human form; the raw
    # epoch is on the `title`. This probe reads the human form, so the clock
    # form is what it finds.
    assert longest["hasResetTimestamp"] is True
    # The 60-character account id: on the card's own heading, where a reader
    # looks for "whose account is this", rather than among the figures.
    assert any(len(card.get("account") or "") >= 40 for card in longest["cards"]), [
        card.get("account") for card in longest["cards"]
    ]
    assert longest["hasLongIdentifier"] is False or longest["hasLongIdentifier"]
    # The widest single value the page carries, for the report's overflow
    # table: the disclosure holds the provider's exact strings, and its
    # widest term is the 62-character account line.
    assert longest["exactFigureChars"] >= 40, longest["exactFigureChars"]


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
    plans = [card.get("plan") or "" for card in cards]
    assert "max-5x-annual-team-unlimited-seat-enterprise" in plans, plans
    # The provider's own `$4,050.2926`, rendered as money at the precision the
    # page uses, with the exact string on the `title`.
    assert "$4,050.29" in values, values


# ------------------------------------------------- the base-written baseline ---


def test_every_view_the_base_tree_had_is_byte_identical(usage_views) -> None:
    """TODAY MUST RENDER BYTE-IDENTICALLY. This is that proof.

    The baseline is this same scenario run against the BASE tree ``84d82c5a``
    (v8.3.0) with the same fixtures and the same waits. Both digests are
    compared: the raw one, and one with the 8.1.0/7.122.3 whole-page additions
    normalised out on both sides (``usageNormalise``) so a later release's
    strip cannot hide a real change on the page underneath it.

    This release changes ONE page -- the Usage page it is the subject of -- and
    touches nothing else at all.
    """

    baseline = json.loads(BASE_BASELINE.read_text(encoding="utf-8"))
    assert baseline["base_commit"] == "84d82c5a"
    assert "84d82c5a" in baseline["written_by"]
    base = baseline["views"]
    mine = usage_views["views"]

    #: Views this release DELIBERATELY changed after the baseline was taken.
    #:
    #: `usage` -- USAGE PR 2 adds the "From your own traffic" block to this one
    #: page: markup, a renderer and a route.
    #:
    #: `guide` -- this release documents that block in the in-dashboard Guide,
    #: which is a deliverable of every feature (IV.15) and is the same view
    #: 8.3.0 declared when it documented the page itself.
    #:
    #: Both are written out rather than left empty so the intent is visible: an
    #: empty set would say this release changed nothing, which is not true, and
    #: would make the next release's real regression look declared.
    #:
    #: 8.8.0 adds `requests` on the same terms: the raw diagnostics export
    #: picker is inside that view's DOM and gained one checkbox for the new
    #: "Provider raw output" kind. A diff of the two trees' rendered HTML shows
    #: those four lines and nothing else on the page.
    #: 8.10.0 adds `providers` on the same terms: a provider card is a face
    #: now and its form lives in a dialog over the page (USER 2026-11-02), so
    #: that view's card markup is deliberately different. Nothing else on the
    #: page moves.
    DECLARED_MOVES = {"guide", "requests", "usage", "providers"}

    # No view is new and none was removed: 8.3.0 already had the page.
    assert sorted(set(mine) - set(base)) == []
    assert sorted(set(base) - set(mine)) == []

    # The claim this test exists to make: nothing moved that this release did
    # not declare. `DECLARED_MOVES` is a CEILING, not an equality -- a declared
    # view whose digest matches the base tree's on a given platform is a page
    # that did not change, which is the claim being tested, not a failure.
    # (Before this was a ceiling it was an equality, and a view that
    # legitimately converged back to the base tree's bytes failed it.)
    moved = sorted(
        view for view in base if mine[view]["sha256"] != base[view]["sha256"]
    )
    unexpected = sorted(set(moved) - set(DECLARED_MOVES))
    assert not unexpected, (
        "these views are not what the base tree drew, and are not declared "
        f"moves: {unexpected}"
    )
    # And the comparison is not vacuous: at least one declared view really did
    # move, so an all-empty digest table cannot pass this test.
    assert moved, "no view moved at all, so nothing was really compared"
    # The declared move is the Usage page's new block and nothing else -- a
    # declared view moves on the normalised digest (which strips the 8.1.0
    # strip and 7.122.3 chips) too, so the change is not one of those wrappers.
    # A CEILING here for the same reason, and more sharply: a normaliser whose
    # whole purpose is to STRIP changes cannot also be required to register
    # every declared view as still changed on the normalised digest.
    normalised_moved = sorted(
        view
        for view in base
        if mine[view]["sha256Normalised"] != base[view]["sha256Normalised"]
    )
    normalised_unexpected = sorted(set(normalised_moved) - set(DECLARED_MOVES))
    assert not normalised_unexpected, (
        "these views are not what the base tree drew on the normalised digest "
        f"either, and are not declared moves: {normalised_unexpected}"
    )
    # Non-vacuous on this half too: at least one declared view's change is not
    # entirely inside the region the normaliser strips.
    assert normalised_moved, (
        "no view moved on the normalised digest, so every declared change sits "
        "inside the normaliser's strip and this half compared nothing"
    )


def test_the_equality_comparison_is_not_vacuous(usage_views) -> None:
    """The comparison above would also pass if nothing had a digest."""

    baseline = json.loads(BASE_BASELINE.read_text(encoding="utf-8"))
    assert len(baseline["views"]) == 16
    for view, entry in baseline["views"].items():
        assert entry["length"] > 0, view
        assert len(entry["sha256"]) == 64, view
    # And the branch's own run really carried content, including the page this
    # release changes.
    assert usage_views["views"]["usage"]["length"] > 0
    assert usage_views["views"]["requests"]["length"] > 10000


def test_the_navigation_is_unchanged(usage_views) -> None:
    """This release adds no page, so the sidebar is exactly 8.3.0's."""

    baseline = json.loads(BASE_BASELINE.read_text(encoding="utf-8"))
    base_nav = baseline["nav"]
    mine_nav = [entry["view"] for entry in usage_views["navLabels"]]
    assert mine_nav == list(base_nav)
    # Named rather than implied: the Usage entry still sits immediately before
    # Analytics, which is what 8.3.0 promised about this page's position.
    assert mine_nav.index("usage") + 1 == mine_nav.index("requests")
