"""The Usage page's on-demand read (8.5.0, USAGE PR 3), proved in jsdom.

Scenario: ``usage_reads`` (``tests/api/admin_jsdom_usage_reads.mjs``), driven
through ``tests/api/admin_jsdom_harness.mjs`` like every other dashboard suite.
The page's four states and its zero-upstream-traffic property are
``tests/api/test_admin_usage_page_jsdom.py``'s; this file is only what PR 3
adds.

What it proves, in the release's own order:

1. **The control exists where it can work and nowhere else.** A provider this
   release can read gets a live **Read now**; one whose row declares no adapter
   gets a disabled one and a sentence saying why.
2. **The countdown is the server's, not this file's.** The button's label and
   its disabled state come from ``next_read_in_seconds`` / ``retry_in_seconds``
   on the payload -- so two surfaces cannot disagree about when a read may
   happen again.
3. **One press, one POST, one provider id.** The fetch stub records the body,
   so this is a claim about what went out, not about what was rendered.
4. **A second press inside the interval asks for nothing** and cannot be made
   to: the server's own answer keeps the button disabled.
5. **A 429 is a deadline**, shown in the provider's own words, with the button
   disabled until it passes.
6. **``unavailable`` is neither an error nor a zero** -- its own chip, its own
   sentence, and no failure block.
7. **A reading's windows and its balances are two blocks**, and nothing prints
   a combined total.
"""

import json
import os
from pathlib import Path

import pytest

from tests.api.test_admin_static_jsdom import _run

ON_CI = os.environ.get("MCC_CI", "") not in ("", "0", "false")

pytestmark = pytest.mark.xdist_group("admin_static_jsdom")


@pytest.fixture(scope="module")
def reads() -> dict:
    return _run(MCC_JSDOM_SCENARIO="usage_reads")


def test_the_scenario_runs_clean(reads: dict) -> None:
    assert reads["fatal"] is None
    assert reads["scenarioFatal"] is None
    assert reads["scriptErrors"] == []
    assert reads["consoleErrors"] == []
    if ON_CI:
        # A payload that never started is not a pass.
        assert reads["button"]["cards"] == 2


# ------------------------------------------------------------------ the control


def test_a_readable_provider_gets_a_live_button(reads: dict) -> None:
    button = reads["button"]["onReadable"]
    assert button is not None
    assert button["disabled"] is False
    assert button["text"] == "Read now"


def test_the_countdown_comes_from_the_servers_own_number(reads: dict) -> None:
    """27 minutes is ``1620`` seconds, rendered. Nothing was computed here."""

    inside = reads["button"]["disabledInsideInterval"]
    assert inside["disabled"] is True
    assert "27 minutes" in inside["text"]
    # And the reason is on the control, so a reader who hovers learns why.
    assert "less than 30 minutes ago" in inside["title"]


def test_a_providers_own_deadline_disables_the_button_too(reads: dict) -> None:
    assert reads["button"]["disabledByProviderDeadline"]["disabled"] is True
    assert "5 minutes" in reads["button"]["disabledByProviderDeadline"]["text"]


def test_the_wait_vocabulary_is_one_vocabulary(reads: dict) -> None:
    by_seconds = {entry["seconds"]: entry["text"] for entry in reads["button"]["waits"]}
    assert by_seconds[0] == "under a minute"
    assert by_seconds[90] == "2 minutes"
    assert by_seconds[1620] == "27 minutes"
    assert by_seconds[7200] == "2 hours"


def test_an_unreadable_provider_gets_a_disabled_button_and_says_why(
    reads: dict,
) -> None:
    """A control that can only fail must not look like one that works."""

    button = reads["unreadable"]["button"]
    assert button is not None
    assert button["disabled"] is True
    assert "no read for this provider" in button["title"]


# ------------------------------------------------------------------- the press


def test_one_press_sends_exactly_one_post_with_one_provider_id(reads: dict) -> None:
    bodies = reads["press"]["bodies"]
    assert len(bodies) == 1
    assert bodies[0]["method"] == "POST"
    assert bodies[0]["path"] == "/admin/api/usage/read"
    assert bodies[0]["body"] == {"provider_id": "deepseek"}


def test_the_answer_repaints_the_card_with_the_readings_own_figures(
    reads: dict,
) -> None:
    after = reads["press"]["stateAfter"]
    assert after, "the page was empty after the press"
    # By NAME rather than by position: 8.7.0 orders the provider list by state,
    # so "the first card" is not a stable way to name the provider that was
    # read.
    deepseek = next(card for card in after if card["title"] == "DeepSeek")
    assert deepseek["chipState"] == "measured"
    headings = [block["heading"] for block in deepseek["blocks"]]
    assert headings == ["Allowance", "Balance and spend"]
    allowance = deepseek["blocks"][0]["figures"]
    # A used/limit pair is drawn as the two amounts the provider sent, in ONE
    # readable clause with its consumed share beside it — `$2.50 of $8.00`,
    # 31.3 % — rather than as two rows of raw numbers. That is the compaction
    # the user asked for, and both halves are still the provider's own text.
    assert allowance[0][0] == "5-hour window"
    assert allowance[0][1] == "$2.50 of $8.00"
    assert allowance[1][0] == "5-hour window resets"
    balance = deepseek["blocks"][1]["figures"]
    assert balance[0] == ["Balance (USD)", "$12.34"]


def test_a_window_and_a_balance_are_never_one_total(reads: dict) -> None:
    assert reads["blocks"]["combined"] is False
    for card in reads["press"]["stateAfter"]:
        assert card.get("hasCombinedTotal") is not True


# ----------------------------------------------------------- the read interval


def test_the_button_is_disabled_after_the_first_answer(reads: dict) -> None:
    interval = reads["interval"]
    assert interval["stillDisabled"] is True
    assert "30 minutes" in interval["text"]


def test_a_press_the_server_refuses_changes_nothing(reads: dict) -> None:
    """One request went out -- the press was made -- and nothing else happened.

    The route answered ``requested: false`` with the interval's own sentence,
    and the button stayed disabled with the countdown the ANSWER carried, so
    the refusal cannot be circumvented by pressing again.
    """

    interval = reads["interval"]
    assert interval["asked"] == 1
    assert interval["buttonAfter"]["disabled"] is True


# -------------------------------------------------------------- the states


def test_every_state_word_maps_to_a_chip_and_an_unknown_one_never_claims_data(
    reads: dict,
) -> None:
    kinds = {entry["serverState"]: entry["kind"] for entry in reads["states"]}
    assert kinds["measured"] == "measured"
    assert kinds["stale"] == "stale"
    assert kinds["not_measured"] == "not_measured"
    assert kinds["unsupported"] == "unsupported"
    assert kinds["unavailable"] == "unavailable"
    assert kinds["failed"] == "failed"
    # An unknown word must never become a chip that claims a measurement.
    assert kinds["something_new"] == "not_measured"


def test_a_429_deadline_is_shown_with_its_reason_and_disables_the_button(
    reads: dict,
) -> None:
    deadline = reads["deadline"]
    assert deadline["chipState"] == "failed"
    assert "rate-limiting" in deadline["failure"]
    assert deadline["button"]["disabled"] is True
    assert "27 minutes" in deadline["button"]["text"]
    # The deadline is the PROVIDER's, and the control says so rather than
    # blaming the interval.
    assert "asked MCC to wait" in deadline["button"]["title"]


def test_an_expired_plan_reads_as_not_available_rather_than_an_error(
    reads: dict,
) -> None:
    """The user's own requirement about Kimi, generalised to any row."""

    unavailable = reads["unavailable"]
    assert unavailable["chipState"] == "unavailable"
    assert unavailable["chip"] == "Not available"
    assert "EXPIRED" in unavailable["stateNote"]
    # It is NOT the failure tone and it is NOT the failure sentence.
    assert unavailable["failure"] == ""
    assert "Could not read usage" not in unavailable["stateNote"]
    assert unavailable["saysNotAvailable"] is True


def test_an_unreadable_provider_is_not_promised_a_header_that_never_comes(
    reads: dict,
) -> None:
    """``kimi_coding`` sends no header family; its card must not say one will."""

    assert reads["unreadable"]["promisesHeaders"] is False


# -------------------------------------------------------- the DOM baseline note


def test_the_button_does_not_claim_a_class_the_stylesheet_never_defines() -> None:
    """``.secondary`` matched no CSS rule, and only a screenshot caught it.

    The brand contract next door (``test_admin_asset_wiring``) pairs a
    ``render…`` function's classes with a rule in ``admin.css``; this one is a
    literal class string inside a builder, which that guard does not scan. The
    control therefore rendered with no border, no padding and the browser's
    default appearance -- invisible to every jsdom assertion, obvious in the
    1280 px screenshot.
    """

    static = Path(__file__).resolve().parents[2] / "src" / "my_claude_code" / "api"
    admin_js = (static / "admin_static" / "admin.js").read_text(encoding="utf-8")
    css = (static / "admin_static" / "admin.css").read_text(encoding="utf-8")
    assert 'className = "usage-read-button secondary-button"' in admin_js
    assert ".secondary-button {" in css
    assert ".usage-read-button {" in css


def test_a_subscription_card_gets_its_own_read_and_control(reads: dict) -> None:
    """Claude and Codex have a declared read of their own since 8.5.0.

    The route is carried by the OAuth token MCC already holds, so the two
    subscriptions are rows like any other -- and their card shows the read's
    own windows plus the control, on every path including the one where the
    header payload never arrived (which is exactly the card where a read is the
    only way to learn anything).
    """

    subscription = reads["subscription"]
    assert subscription["drawn"] is True
    assert subscription["title"] == "Claude subscription"
    # The reading's own block, under its own heading.
    assert subscription["readsBlock"] is True
    assert "Allowance" in subscription["headings"]
    # And the control, with the server's countdown.
    assert subscription["button"] is not None
    assert "29 minutes" in subscription["button"]["text"]
    assert subscription["button"]["disabled"] is True


def test_a_subscription_read_is_the_same_two_block_model(reads: dict) -> None:
    """Its windows are an allowance, its balances would be cash -- never mixed."""

    subscription = reads["subscription"]
    assert subscription["headings"][0] == "Allowance"


def test_the_scenario_declares_a_dom_baseline_for_the_page_it_changes() -> None:
    """PR 3 changes only the Usage page, and the existing baseline still bites.

    ``test_admin_usage_page_jsdom.py`` compares every view's digest against the
    BASE tree's and refuses an undeclared move. This file adds no view and
    changes no markup outside the Usage blocks, so the one declared move it
    inherits is still ``usage``.
    """

    baseline = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "fixtures"
            / "usage_page_dom_8_3_0.json"
        ).read_text(encoding="utf-8")
    )
    assert "usage" in baseline["views"]
    assert len(baseline["views"]["usage"]["sha256"]) == 64
