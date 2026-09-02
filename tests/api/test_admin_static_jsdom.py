"""Execute the real admin.js in jsdom and assert on what it rendered.

This is the only test in the suite that runs the dashboard's JavaScript. It
proves the script evaluates, every nav entry still renders, and the Token
Optimizer page shows honest empty states on a fresh install.

What it does NOT prove, because jsdom has no layout engine: spacing, overflow,
contrast, focus rings, breakpoints, or anything else that needs a box to have a
size. Those remain unverified by any automated check in this repo.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from my_claude_code.core.upstream_ladder import _TIMES

HARNESS = Path(__file__).with_name("admin_jsdom_harness.mjs")
STATIC_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
)


def _run(**env_extra) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not on PATH")
    result = subprocess.run(
        [node, str(HARNESS), str(STATIC_DIR)],
        capture_output=True,
        text=True,
        # Explicit: the page is full of em dashes and middots, and on Windows
        # the default console codec turns every one of them into U+FFFD --
        # which would quietly make an "is this an em dash" assertion untestable.
        encoding="utf-8",
        timeout=180,
        env={**os.environ, **env_extra},
    )
    if result.returncode != 0:
        if "Cannot find package 'jsdom'" in result.stderr:
            pytest.skip("jsdom is not installed")
        pytest.fail(f"harness failed: {result.stderr[-2000:]}")
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def rendered() -> dict:
    return _run()


@pytest.fixture(scope="module")
def fresh_install() -> dict:
    return _run(EMPTY="1")


def test_the_dashboard_script_evaluates_without_error(rendered) -> None:
    assert rendered["fatal"] is None
    assert rendered["scriptErrors"] == []


# Views whose entire content comes from settings fields. This fixture supplies
# no fields for `model_config` or `messaging`, so they legitimately render
# empty here -- they do on unmodified main under the same payload, which is why
# they are named rather than silently included in a blanket assertion.
# `requests` now owns the request-log storage section as well as its tables.
VIEWS_WITH_CONTENT = (
    "get_started",
    "providers",
    "claude",
    "requests",
    "optimizer",
    "web_search",
    "limits",
    "guide",
    "docs",
)


def test_every_nav_entry_has_markup_to_render_into(rendered) -> None:
    """A view that renders 0 where it should render n is the bug this catches.

    Adding a view group with no settings container used to make byId(null)
    return null and take every other tab down with it -- every tab, not just
    the new one. So this asserts across all of them, not across the new page.
    """
    for view_id, view in rendered["views"].items():
        assert view["exists"], f"{view_id} has a nav entry and no markup"
    for view_id in VIEWS_WITH_CONTENT:
        assert rendered["views"][view_id]["text"] > 0, (
            f"{view_id} rendered nothing at all"
        )


def test_the_settings_views_still_render_their_sections(rendered) -> None:
    """Exact counts, driven by the payload above rather than by a floor.

    ">= 1" survived a six-way section split that could have rendered one card
    and dropped five. The numbers are what this fixture's SECTIONS claims.
    """
    views = rendered["views"]
    assert views["providers"]["sections"] == 4
    assert views["limits"]["sections"] == 6
    assert views["limits"]["fieldInputs"] >= 1
    assert views["requests"]["sections"] == 1
    assert views["optimizer"]["sections"] >= 1


def test_stream_recovery_tiles_render_from_the_stats_payload(rendered) -> None:
    """The three transparent-recovery counters surface as request tiles."""
    cards = {row[1]: row[0] for row in rendered["requestCards"]}

    assert cards.get("Early retries") == "41"
    assert cards.get("Midstream recoveries") == "7"
    assert cards.get("Salvages") == "3"


def test_the_optimizer_view_is_registered(rendered) -> None:
    assert "Token Optimizer" in rendered["navLabels"]
    assert rendered["optimizer"]["present"] is True


def test_the_ledger_renders_its_four_headline_figures(rendered) -> None:
    assert rendered["optimizer"]["kpis"] == 4


def test_trimming_reads_as_off_because_nothing_changed_its_default(rendered) -> None:
    trimming = [text for text in rendered["optimizer"]["kpiText"] if "trimming" in text]
    assert len(trimming) == 1
    assert "off" in trimming[0]
    assert "master switch is off" in trimming[0]


def test_rtk_absent_says_so_instead_of_showing_zero(rendered) -> None:
    rtk = [text for text in rendered["optimizer"]["kpiText"] if "RTK" in text]
    assert len(rtk) == 1
    assert "not installed" in rtk[0]
    assert "—" in rtk[0]
    assert "0" not in rtk[0].replace("RTK savings", "")


def test_a_rule_that_never_fired_shows_a_dash_not_a_zero_saving(rendered) -> None:
    rows = {
        row[0].split("The suggested")[0]: row
        for row in rendered["optimizer"]["ruleRows"]
    }
    suggestion = next(row for key, row in rows.items() if "Suggestion" in key)

    # Fired is a real measured zero. Tokens avoided was never measured.
    assert suggestion[2] == "0"
    assert suggestion[3] == "—"


def test_a_rule_shows_the_literal_string_it_answers_with(rendered) -> None:
    joined = " ".join(" ".join(row) for row in rendered["optimizer"]["ruleRows"])
    assert '"Conversation"' in joined
    assert "(nothing shown)" in joined


def test_a_provider_reporting_no_cache_figures_renders_an_em_dash(rendered) -> None:
    rows = {row[0]: row for row in rendered["optimizer"]["cacheRows"]}

    assert rows["chatgpt_oauth"][2] == "—"
    assert "reports no cache figures" in rows["chatgpt_oauth"][3]
    # A provider that did report is still shown as a percentage.
    assert rows["nous_portal"][2].endswith("%")


def test_locally_answered_traffic_is_named_not_labelled_unknown(rendered) -> None:
    labels = [row[0] for row in rendered["optimizer"]["cacheRows"]]

    assert "answered locally · title generation skip" in labels
    assert "(unknown)" not in labels
    assert not any(label.startswith("local:") for label in labels)


def test_every_sparkline_has_a_companion_data_table(rendered) -> None:
    optimizer = rendered["optimizer"]
    assert optimizer["sparklines"] >= 1
    assert optimizer["dataTables"] >= optimizer["sparklines"]


def test_per_tool_controls_are_disabled_while_the_master_switch_is_off(
    rendered,
) -> None:
    assert rendered["optimizer"]["segControls"] == 3
    assert rendered["optimizer"]["segDisabledWhileMasterOff"] is True


def test_one_setting_counts_as_one_unsaved_change_not_two(rendered) -> None:
    """The visible switch and the hidden manifest input are one setting."""
    assert rendered["optimizer"]["dirtyAfterToggle"] == "1 unsaved change"


def test_the_trimming_warning_states_the_measured_result(rendered) -> None:
    warning = rendered["optimizer"]["warning"]

    assert "Measured harmful at your cache rates" in warning
    assert "unvalidated" not in warning.lower()
    assert "10.9%" in warning
    assert "3.8%" in warning
    assert "107,797" in warning
    assert "90.9%" in warning
    assert "Observe" in warning


# ------------------------------------------------- unset fields and defaults
# The dashboard used to have no way to say "nobody chose this". A select fell
# back to its first option, `dataset.original` stayed empty, and the next Save
# submitted the option it happened to be showing -- which is how installs got
# `FALLBACK_BENCH_ENABLED=false` written into a managed .env nobody had edited.


def test_an_unset_select_loads_clean_and_shows_its_default(rendered) -> None:
    control = rendered["fields"]["unsetSelect"]

    assert control["tag"] == "select"
    assert control["value"] == ""
    assert control["original"] == ""
    assert control["optionValues"][0] == ""
    assert control["firstOptionLabel"].startswith("Default (true)")
    # The whole point: a form nobody touched has nothing to save.
    assert rendered["fields"]["dirtyOnLoad"] == "No changes"


def test_every_field_says_what_its_default_is(rendered) -> None:
    defaults = rendered["fields"]["fieldDefaults"]

    assert defaults["FALLBACK_BENCH_ENABLED"] == "default: true"
    assert defaults["LOG_LEVEL"] == "default: INFO"


def test_only_a_field_someone_set_offers_to_go_back_to_the_default(rendered) -> None:
    buttons = rendered["fields"]["resetButtons"]

    assert buttons["LOG_LEVEL"] is True
    assert buttons["FALLBACK_BENCH_ENABLED"] is False


def test_use_default_marks_the_form_dirty_and_submits_empty(rendered) -> None:
    """Empty is the wire value that means "drop the line", not "store INFO"."""

    use_default = rendered["fields"]["useDefault"]

    assert use_default is not None
    assert use_default["value"] == ""
    assert use_default["dirty"] == "1 unsaved change"


def test_boolean_fields_render_three_states(rendered) -> None:
    """On, off, and never chosen -- a checkbox can only show two of them."""

    control = rendered["fields"]["booleanControl"]

    assert control["tag"] == "select"
    assert control["optionValues"] == ["", "true", "false"]
    assert control["firstOptionLabel"] == "Default (Off)"


# ----------------------------------------------------------- fresh install


def test_a_fresh_install_renders_without_error(fresh_install) -> None:
    assert fresh_install["fatal"] is None
    assert fresh_install["scriptErrors"] == []


def test_a_fresh_install_shows_no_fabricated_zeros(fresh_install) -> None:
    """No requests, no RTK, logging off: every measurement is unknown."""
    for row in fresh_install["optimizer"]["ruleRows"]:
        assert row[2] == "—", row
        assert row[3] == "—", row


def test_a_fresh_install_still_names_every_rule_and_its_state(fresh_install) -> None:
    joined = " ".join(" ".join(row) for row in fresh_install["optimizer"]["ruleRows"])

    assert "Title generation skip" in joined
    assert "Suggestion mode skip" in joined
    assert "on" in joined


def test_a_fresh_install_does_not_break_the_other_views(fresh_install) -> None:
    for view_id, view in fresh_install["views"].items():
        assert view["exists"], view_id
    for view_id in VIEWS_WITH_CONTENT:
        assert fresh_install["views"][view_id]["text"] > 0, view_id


# --------------------------------------------------------------------- docs
# The Docs page. The markdown is rendered on the server; what these check is
# that the page places that HTML and wires the navigation beside it. Nothing
# here parses markdown, and jsdom cannot tell whether any of it is legible.


def test_registering_the_docs_view_did_not_break_the_other_views(rendered) -> None:
    """The settings render loop empties `byId(view.containerId)` for every
    entry. A static view whose containerId is not null-guarded makes that
    lookup return null and the whole render throws -- killing every tab, not
    just the new one. These are the counts unmodified main produces.
    """

    expected = {
        "get_started": 1,
        "providers": 4,
        "claude": 3,
        "requests": 1,
        "optimizer": 6,
        "web_search": 1,
        "limits": 6,
        "guide": 0,
        "docs": 0,
    }
    actual = {name: rendered["views"][name]["sections"] for name in expected}
    assert actual == expected


def test_the_docs_view_is_registered_in_the_nav(rendered) -> None:
    assert "Docs" in [label.strip() for label in rendered["navLabels"]]
    assert rendered["views"]["docs"]["exists"] is True


def test_the_docs_page_lists_every_bundled_document(rendered) -> None:
    assert rendered["docs"]["docLinks"] == ["README", "Usage"]


def test_the_first_document_opens_without_being_asked_for(rendered) -> None:
    docs = rendered["docs"]
    assert docs["title"] == "README"
    assert docs["currentDoc"] == "README"
    # The loading line must get out of the way once something loaded.
    assert docs["statusHidden"] is True


def test_a_long_document_gets_a_table_of_contents(rendered) -> None:
    """The README is over a thousand lines; a page that long without one is
    a scroll bar and nothing else."""

    assert rendered["docs"]["headingLinks"] == [
        "docs-heading-top:Install",
        "docs-heading-sub:Windows",
    ]


def test_every_heading_the_contents_links_to_exists_in_the_document(rendered) -> None:
    anchors = set(rendered["docs"]["anchorIds"])
    assert {"install", "windows"} <= anchors


def test_the_document_carries_a_link_to_the_latest_on_github(rendered) -> None:
    href = rendered["docs"]["githubHref"]
    assert href.startswith("https://github.com/FiredMosquito831/my-claude-code/blob/")
    assert href.endswith("README.md")


def test_every_table_sits_in_its_own_scroll_box(rendered) -> None:
    """A wide table is the one thing in a document that can push the page
    body sideways. jsdom has no layout engine, so this proves the box exists
    around every table -- not that anything actually scrolls.
    """

    docs = rendered["docs"]
    assert docs["tables"] > 0, "fixture rendered no table to check"
    assert docs["scrollBoxes"] == docs["tables"]
    assert docs["unwrappedTables"] == 0


def test_a_cross_reference_to_another_document_is_intercepted(rendered) -> None:
    assert rendered["docs"]["crossLinks"] == 1


def test_a_fresh_install_still_renders_the_docs_page(fresh_install) -> None:
    assert fresh_install["docs"]["present"] is True
    assert fresh_install["docs"]["docLinks"] == ["README", "Usage"]


# ---------------------------------------------------------------------------
# Limits & Resilience. The page was one flat grid of 37 fields mixing six
# concerns, and the number that actually decides a handover -- the request
# budget divided by the models still to try -- was shown nowhere.

LIMITS_CARDS = [
    "section-budgets",
    "section-deadlines",
    "section-benching",
    "section-provider_retries",
    "section-credential_health",
    "section-diagnostics",
]


def test_the_limits_view_renders_one_card_per_section(rendered) -> None:
    assert rendered["limits"]["cardIds"] == LIMITS_CARDS


def test_every_limits_card_states_what_it_decides(rendered) -> None:
    """A card with a title and no sentence under it is a heading, not a card."""
    limits = rendered["limits"]
    assert len(limits["cardDescriptions"]) == len(LIMITS_CARDS)
    for text in limits["cardDescriptions"]:
        assert text.strip()


def test_no_limits_card_hides_every_control_behind_show_advanced(rendered) -> None:
    """The trap the six-way split walks into, caught at render time.

    Both of ``credential_health``'s fields shipped ``advanced``, which would
    render a heading, a description, a toggle and nothing else.
    """
    for card, visible in zip(
        rendered["limits"]["cardIds"],
        rendered["limits"]["cardVisibleFields"],
        strict=True,
    ):
        assert visible >= 1, f"{card} renders no field without a click"


def test_the_calculator_divides_the_budget_by_the_longest_chain(rendered) -> None:
    """600s over a ten-model chain is 60s each, not the 120s the box says."""
    headline = rendered["limits"]["calcHeadline"]
    assert "Opus" in headline
    assert "10 models" in headline
    assert "60 s" in headline


def test_the_calculator_shows_the_first_token_deadline_for_a_one_model_route(
    rendered,
) -> None:
    """min(120, 600/1) is the deadline, not the whole budget."""
    rows = {row[0]: row[2] for row in rendered["limits"]["calcRows"][1:]}
    assert rows["Haiku"] == "120 s"


def test_a_three_model_chain_is_bounded_by_the_first_token_deadline(rendered) -> None:
    """min(120, 600/3 = 200) is 120."""
    rows = {row[0]: row[2] for row in rendered["limits"]["calcRows"][1:]}
    assert rows["Sonnet"] == "120 s"


def test_a_route_with_no_model_of_its_own_is_not_counted(rendered) -> None:
    """Fable is empty on both halves: it falls back to MODEL, so counting it
    would double-count the Default route."""
    labels = [row[0] for row in rendered["limits"]["calcRows"][1:]]
    assert "Fable" not in labels
    assert labels == ["Default", "Opus", "Sonnet", "Haiku", "Vision"]


def test_the_calculator_says_the_first_token_deadline_is_inert(rendered) -> None:
    limits = rendered["limits"]
    assert limits["calcWarningHidden"] is False
    assert "1200 s" in limits["calcWarning"]
    assert limits["calcWarning"].startswith("Warning:"), (
        "the word carries the meaning; the colour is redundant"
    )


def test_the_calculator_shows_its_working(rendered) -> None:
    formula = rendered["limits"]["calcFormula"]
    assert "600 ÷ 10 = 60 s" in formula
    assert "120 s" in formula


def test_the_calculator_recomputes_when_a_deadline_is_edited(rendered) -> None:
    """No reload: raising the budget to 1200 gives each of ten models 120s."""
    headline = rendered["limits"]["calcHeadlineAfterEdit"]
    assert "120 s" in headline
    assert "60 s" not in headline


def test_the_calculator_names_the_floor_when_the_floor_is_what_decides(
    rendered,
) -> None:
    """The share line has to say which number produced it.

    "600 ÷ 10 = 60 s" alone, on a page where the answer is 180, reads as a
    typo. Naming the floor beside the division is what turns the calculator
    back into an explanation of this route.
    """
    after = rendered["limits"]["afterFloorRaised"]

    assert "600 ÷ 10 = 60 s" in after["calcFormula"]
    assert "raised to the 180 s silent-attempt floor" in after["calcFormula"]
    assert "the first-token deadline (120 s)" in after["calcFormula"]


def test_the_floor_lifts_a_short_share_up_to_the_first_token_deadline(
    rendered,
) -> None:
    """min(120, max(600 ÷ 10, 180)) = 120: the box becomes the number you get.

    Without the floor this same payload gives Opus 60 s -- asserted three tests
    above, on the unraised load. The pair together is the whole point of the
    setting.
    """
    after = rendered["limits"]["afterFloorRaised"]
    rows = {row[0]: row[2] for row in after["calcRows"][1:]}

    assert rows["Opus"] == "120 s"
    assert "120 s" in after["calcHeadline"]
    assert "60 s" not in after["calcHeadline"]


def test_the_calculator_warns_that_the_floor_cannot_fit_the_budget(rendered) -> None:
    """N x floor > total is the cost of the floor, stated rather than hidden.

    Ten models at 180 s want 1,800 s of a 600 s budget, so only the first three
    silent models can use the whole floor. An operator who raises the floor is
    entitled to know that before a request proves it.
    """
    after = rendered["limits"]["afterFloorRaised"]

    assert after["calcWarningHidden"] is False
    assert after["calcWarning"].startswith("Warning:")
    assert "10 models at the 180 s floor add up to 1800 s" in after["calcWarning"]
    assert "more than the 600 s budget" in after["calcWarning"]
    assert "first 3 silent models" in after["calcWarning"]
    assert "then nothing" in after["calcWarning"]


def test_a_blank_deadline_is_read_as_its_default_not_as_no_limit(rendered) -> None:
    """The placeholder is the value the server is using; the box is just empty.

    Every field ships blank until someone saves it, so a calculator that read
    blank as 0 would tell a fresh install it had no first-token deadline at all
    -- and would switch the silent-attempt floor off on the page for exactly
    the installs that have never touched it.
    """
    after = rendered["limits"]["afterFirstTokenCleared"]

    assert "the first-token deadline (120 s)" in after["calcFormula"]
    assert "No first-token deadline is set" not in after["calcHeadline"]


def test_the_calculator_says_no_limit_when_every_deadline_is_zero(
    rendered,
) -> None:
    """The shipped state since 6.16.0, and the one it must not print 0 s for.

    An operator looking at a fresh install has to be able to read "nothing
    here will end a silent model" off the card. A table of "0 s" says the
    opposite, and a NaN says nothing at all.
    """
    after = rendered["limits"]["afterAllDeadlinesZeroed"]
    rows = {row[0]: row[2] for row in after["calcRows"][1:]}

    assert set(rows.values()) == {"no limit"}
    assert "No first-token deadline is set" in after["calcHeadline"]
    # It still names what is left: the transport, not MCC, ends this request.
    assert "HTTP read timeout" in after["calcHeadline"]


def test_no_budget_warning_fires_when_there_is_no_budget(rendered) -> None:
    """Every warning on this card describes a budget being carved up.

    With the total at 0 there is nothing to carve, so a warning about the
    floor not fitting, or the deadline being undercut, would be describing a
    machine that does not exist.
    """
    after = rendered["limits"]["afterAllDeadlinesZeroed"]

    assert after["calcWarningHidden"] is True
    assert after["calcFormula"] == ""


def test_the_floor_warning_fires_on_six_models_and_not_on_three(rendered) -> None:
    """600 s floor, 1800 s budget: three chains fit exactly, six do not.

    The boundary is the interesting part. At six models the floor is asking
    for 3600 s of an 1800 s budget and only the first three silent models can
    have it; at three it adds up to exactly the budget and the trade the
    warning describes is not being made.
    """
    six = rendered["limits"]["floorAgainstBudget"]["six"]
    three = rendered["limits"]["floorAgainstBudget"]["three"]

    assert six["calcWarningHidden"] is False
    assert "6 models at the 600 s floor add up to 3600 s" in six["calcWarning"]
    assert "more than the 1800 s budget" in six["calcWarning"]
    assert "first 3 silent models" in six["calcWarning"]

    # Three does raise the unrelated transport warning -- HTTP_READ_TIMEOUT is
    # 300 s against a 600 s allowance -- which is the point: the card shows one
    # warning at a time, most severe first, and the floor is not one of them
    # here because the floor fits.
    assert "floor add up to" not in three["calcWarning"]


def test_the_calculator_never_interpolates_a_model_name_into_markup(rendered) -> None:
    """The vision route's primary model in this payload is an <img> tag.

    The table names routes, not models, so the string never reaches the DOM at
    all -- and the rows it does build come from createElement/textContent, so
    a route label could not produce an element either. The static guard in
    tests/contracts/test_admin_limits_view.py pins the second half.
    """
    limits = rendered["limits"]
    assert "<img" not in limits["calcTableHtml"]
    assert "onerror" not in limits["calcTableHtml"]
    assert [row[0] for row in limits["calcRows"][1:]] == [
        "Default",
        "Opus",
        "Sonnet",
        "Haiku",
        "Vision",
    ]


def test_switching_eject_mode_disables_the_other_modes_knobs(rendered) -> None:
    groups = {g["mode"]: g for g in rendered["limits"]["afterLegacy"]["benchGroups"]}
    assert groups["rate_based"]["inert"] is True
    assert groups["rate_based"]["disabled"] is True
    assert "rate_based" in groups["rate_based"]["note"] or groups["rate_based"]["note"]
    assert groups["legacy"]["inert"] is False
    assert groups["legacy"]["disabled"] is False


def test_an_inert_knob_is_present_but_not_submitted(rendered) -> None:
    """Kept, so a value typed before the switch is not lost; disabled, so
    ``changedValues()`` skips it and the unused mode is never saved."""
    after = rendered["limits"]["afterLegacy"]
    assert after["windowStillInDom"] is True
    assert after["windowValue"] == "10"
    assert "FALLBACK_EJECT_WINDOW" not in after["submitted"]
    assert after["submitted"] == ["FALLBACK_BEHAVIOR"]


def test_one_setting_counts_as_one_unsaved_change(rendered) -> None:
    """The mode, not the mode plus the three knobs it just disabled."""
    assert rendered["limits"]["afterLegacy"]["dirty"] == "1 unsaved change"


def test_turning_benching_off_makes_every_eject_knob_inert(rendered) -> None:
    groups = rendered["limits"]["afterBenchOff"]["benchGroups"]
    assert [g["inert"] for g in groups] == [True, True]
    for group in groups:
        assert "benching is off" in group["note"], (
            "an inert group says why in words, not only by dimming"
        )


def test_the_benching_card_points_at_where_skip_kinds_lives(rendered) -> None:
    """A setting rendered on two pages can show two answers.

    Two cross-links now: one sending the reader to FALLBACK_SKIP_KINDS, which
    renders only on Model Config, and one naming Model Config as the other
    place the master switch is reachable. The switch is the deliberate
    exception -- one manifest field, one saved key, two controls mirrored by
    ``syncSharedControls`` -- and FALLBACK_SKIP_KINDS is still not.
    """
    limits = rendered["limits"]
    assert limits["crosslinks"] == 2
    assert "Model Config" in limits["crosslinkText"]
    assert limits["skipKindsOnLimits"] == 0


def test_the_master_switch_renders_on_both_pages_as_one_value(rendered) -> None:
    """Two controls, one setting.

    Chain benching is a routing decision, so it reads on Model Config beside
    the routes it governs; it also gates the Limits card, so it stays there
    too. That is the one field this project renders twice, and the rule that
    makes it safe is that both controls are bound to the same manifest key and
    mirrored on edit -- otherwise the page shows two answers and
    ``changedValues()`` submits whichever it walked last.
    """
    mirror = rendered["limits"]["benchMirror"]

    assert mirror["controls"] == 2
    assert mirror["onModelConfig"] == "true"
    assert mirror["onLimits"] == "true"
    # One key, not two controls' worth. (FALLBACK_BEHAVIOR is dirty too: an
    # earlier step in the harness switched the card to legacy mode.)
    assert mirror["submitted"].count("FALLBACK_BENCH_ENABLED") == 1
    assert sorted(mirror["submitted"]) == [
        "FALLBACK_BEHAVIOR",
        "FALLBACK_BENCH_ENABLED",
    ]
    # The Limits card followed: the mode's own group came back to life.
    assert [group["inert"] for group in mirror["benchGroups"]] == [True, False]


def test_the_master_switch_links_to_where_the_tuning_lives(rendered) -> None:
    mirror = rendered["limits"]["benchMirror"]

    assert mirror["label"] == "Chain benching"
    assert mirror["crosslink"] == (
        "Tuning (window, rate, duration) lives on Limits & Resilience → Chain benching."
    )
    assert mirror["markup"] is False


def test_a_benched_row_says_why_it_was_benched(rendered) -> None:
    """ "Benched after recent consecutive failures" was one sentence for every
    skip, in a build whose default mode has been rate-based since 5.61.0.
    """
    detail = rendered["requestDetail"]["benchReason"]

    assert detail["chainReasons"] == [
        "ejectedbenched: 5 upstream errors in the last 10 attempts"
        " (rate_based >= 50%), 22 s left"
    ]
    assert detail["benchReasons"] == [
        "5 counted failures in the last 10 attempts of at least 50%"
        " · last: 502 upstream · 22s left · benched 8s ago"
    ]


def test_the_chain_panel_counts_the_models_that_were_benched(rendered) -> None:
    """The incident in one line: capable models removed before the request ran."""
    detail = rendered["requestDetail"]["benchReason"]

    assert (
        "1 model was benched and never tried on this request."
        in (detail["ladderRootCauses"])
    )


def test_each_numeric_limit_shows_its_range_beside_the_input(rendered) -> None:
    limits = rendered["limits"]
    assert limits["ranges"]["count"] >= 1
    assert limits["ranges"]["FALLBACK_FIRST_TOKEN_TIMEOUT"] == (
        "Accepts 0 to 3600 (0 waits indefinitely for the first token)"
    )


def test_a_numeric_input_points_at_its_range_and_its_help(rendered) -> None:
    described = rendered["limits"]["ranges"]["describedBy"].split()
    assert "range-FALLBACK_FIRST_TOKEN_TIMEOUT" in described
    assert "desc-FALLBACK_FIRST_TOKEN_TIMEOUT" in described


def test_the_eject_window_says_how_many_failures_bench_a_model(rendered) -> None:
    assert rendered["limits"]["hints"]["FALLBACK_EJECT_WINDOW"] == (
        "benched after 5 of the last 10 requests fail"
    )


def test_the_lockout_ladder_is_spelled_out_in_words(rendered) -> None:
    hint = rendered["limits"]["hints"]["CREDENTIAL_LOCKOUT_TIERS"]
    for part in ("5m", "1h", "1d", "and after"):
        assert part in hint


def test_the_in_page_rail_links_to_a_section_that_exists(rendered) -> None:
    """The rail is static markup and its targets are rendered: a card that
    fails to render leaves a link that scrolls nowhere, silently."""
    limits = rendered["limits"]
    assert limits["tocLinks"] == [f"#{card}" for card in LIMITS_CARDS]
    assert limits["deadLinks"] == 0


def test_request_log_settings_moved_to_analytics(rendered) -> None:
    """The page that shows the consequence owns the control."""
    cards = rendered["limits"]["requestLogCards"]
    assert cards == [{"id": "section-request_log", "fields": 9}]
    assert "section-request_log" not in rendered["limits"]["cardIds"]


def test_desktop_settings_moved_to_providers(rendered) -> None:
    """Two pages, one subsystem: the live desktop panel is already there."""
    assert rendered["limits"]["desktopCardView"] == "providers"


# --------------------------------------------------------------------------- #
# The request-detail wire pane. Every case here is a stored shape the pane has
# to render honestly: what left, what did not, and what was never measured.
# --------------------------------------------------------------------------- #


def test_the_wire_pane_shows_a_knobs_block_from_params_wire(rendered) -> None:
    """params.wire is never truncated, so the knobs survive a degraded body."""
    detail = rendered["requestDetail"]["degraded"]
    assert "reasoning_effort" in detail["knobKeys"]
    assert "max" in detail["knobs"]
    assert "temperature" in detail["knobKeys"]
    assert "0.7" in detail["knobs"]


def test_the_wire_pane_names_the_allowance_a_thinking_turn_was_widened_to(
    rendered,
) -> None:
    """A max_tokens nobody asked for looks invented until the line explains it."""
    detail = rendered["requestDetail"]["widened"]
    assert "max_tokens 131,072" in detail["text"]
    assert "raised from 64,000 for reasoning" in detail["text"]
    assert "output_widened_from" in detail["knobKeys"]
    assert "64000" in detail["knobs"]


def test_the_wire_pane_renders_a_parameter_it_was_never_taught_by_name(
    rendered,
) -> None:
    """A hard-coded knob list hid every parameter a newer dialect sends.

    The values were captured and stored the whole time; only the rendering
    dropped them, so the pane quietly answered "what left this process" with a
    subset. Every key params.wire carries now gets a row.
    """
    detail = rendered["requestDetail"]["unusualKnobs"]
    for name in (
        "top_k",
        "min_p",
        "repetition_penalty",
        "parallel_tool_calls",
        "response_format",
        "tool_choice",
        "extra_body.chat_template_kwargs",
    ):
        assert name in detail["knobKeys"]
    assert "40" in detail["knobs"]
    assert "0.05" in detail["knobs"]
    assert "1.05" in detail["knobs"]
    assert "false" in detail["knobs"]
    # The nested reasoning container is read out key by key, never printed as
    # a JSON blob under the name "reasoning".
    assert "reasoning" not in detail["knobKeys"]
    assert "reasoning_effort" in detail["knobKeys"]


def test_an_unwidened_attempt_shows_no_widening_row(rendered) -> None:
    """Absence is the finding here, exactly as it is for every other wire knob."""
    detail = rendered["requestDetail"]["degraded"]
    assert "output_widened_from" not in detail["knobKeys"]
    assert "raised from" not in detail["text"]


def test_a_degraded_body_renders_as_parseable_json(rendered) -> None:
    json.loads(rendered["requestDetail"]["degraded"]["pre"])


def test_the_wire_pane_is_visible_when_no_attempt_was_measured(rendered) -> None:
    """It used to vanish, which read as "no request body was sent"."""
    detail = rendered["requestDetail"]["unmeasured"]
    assert detail["hidden"] is False
    assert detail["unmeasured"] == 1
    assert "Not measured" in detail["text"]


def test_an_empty_body_reads_as_the_model_default_not_as_a_fault(rendered) -> None:
    badge = rendered["requestDetail"]["contradiction"]["reasoningBadge"]
    assert badge == "no reasoning instruction sent (model default applies)"


def test_a_contradiction_between_gating_and_the_wire_is_badged(rendered) -> None:
    assert rendered["requestDetail"]["contradiction"]["contradictions"] == 1


def test_a_suppressed_adaptation_with_an_empty_body_is_not_a_contradiction(
    rendered,
) -> None:
    """Gating intended nothing and nothing was sent: the two agree."""
    assert rendered["requestDetail"]["suppressed"]["contradictions"] == 0


def test_an_adaptation_written_before_the_kind_column_badges_nothing(
    rendered,
) -> None:
    """Not measured is not a finding, so an old row raises no badge."""
    assert rendered["requestDetail"]["unkinded"]["contradictions"] == 0


def test_a_nothing_sent_row_is_not_badged_as_a_wire_contradiction(rendered) -> None:
    """No instruction was sent because none was meant to be. That is not a fault.

    ``nothing_sent`` names the case where gating decided the request needed no
    reasoning field at all, so an empty body is the outcome the row describes,
    not evidence against it.
    """
    assert rendered["requestDetail"]["nothingSent"]["contradictions"] == 0


def test_a_legacy_dropped_row_is_not_badged_as_a_wire_contradiction(rendered) -> None:
    """The live false positive, removed: a stored ``dropped`` is ambiguous.

    Until 6.6.0 one value covered both "the level was discarded and thinking
    was switched on some other way" and "nothing was sent at all". Stored rows
    are deliberately not migrated -- a row means what it meant when it was
    written -- so every pre-6.6.0 ``dropped`` row could be either. On the
    running 6.4.0 server one route had 38 such rows, all with
    ``reasoning_emitted=0``, all of them the correct "nothing was sent" case,
    and every one of them carried the badge. Flagging working behaviour as a
    defect is worse than missing the rarer real contradiction, which the
    adaptation message describes in full anyway.
    """
    assert rendered["requestDetail"]["legacyDropped"]["contradictions"] == 0


def test_a_clamped_row_with_no_wire_reasoning_is_still_badged(rendered) -> None:
    """The true positive survives, because ``clamped`` is not ambiguous.

    ``clamped`` names a value gating chose to put on the wire, in both the old
    version and the new one, so a body that carries no reasoning key really
    does contradict it. Narrowing the badge must not turn it off.
    """
    detail = rendered["requestDetail"]["contradiction"]
    assert detail["contradictions"] == 1


def test_an_old_truncated_body_still_renders_with_its_note(rendered) -> None:
    detail = rendered["requestDetail"]["legacyTruncated"]
    assert "Truncated at 8,000 of 41,000 characters" in detail["text"]
    assert detail["pre"].startswith('{"messages"')


def test_a_benched_out_pool_renders_its_sentinel_not_a_key(rendered) -> None:
    keys = rendered["requestDetail"]["benched"]["chainKeys"]
    assert "no key available" in keys
    assert "ab...cd" in keys


def test_an_unmeasured_number_is_a_dash_not_a_zero(rendered) -> None:
    assert rendered["requestDetail"]["unmeasuredNumber"] == "—"
    assert (
        rendered["requestDetail"]["reasoningRow"] == "not sent (model default applies)"
    )


def test_the_dialect_panel_renders_the_origin_pill(rendered) -> None:
    """Provenance is on the panel, in the operator's words, for all three.

    "This host parses reasoning_effort" reads very differently depending on
    whether someone probed it, whether it is the standard assumed of every
    OpenAI-compatible host, or whether the host itself said no.
    """
    panels = rendered["dialectPanels"]

    assert panels["default"]["origin"] == "default OpenAI dialect"
    assert panels["declared"]["origin"] == "declared by this provider"
    assert panels["learned"]["origin"] == "learned from the host's own rejection"
    # An undeclared dialect has no provenance to show, and says so in prose.
    assert panels["unknown"]["origin"] is None
    assert "Not declared for this provider" in panels["unknown"]["notes"][0]
    assert (
        panels["default"]["subhead"]
        == "What this host parses (declared or learned, never voted)default OpenAI dialect"
    )


def test_the_dialect_panel_lists_learned_rejections(rendered) -> None:
    """A learned rejection is dated and attributed to the host's own 400."""
    panels = rendered["dialectPanels"]

    assert panels["default"]["notes"] == [
        "effort via reasoning_effort: high, low, medium, minimal · no on/off "
        "field · no thinking-budget field · cannot be switched off"
    ]
    learned = panels["learned"]["notes"]
    assert len(learned) == 2
    assert learned[1] == (
        "Not sent since 2026-08-29: reasoning_effort — this host answered "
        "400 naming it."
    )


# ------------------------------------------------------------------- models
#
# The Models page had no jsdom coverage at all until this suite: the harness's
# route table had no /admin/api/model-admin entry, so loadModelsView() had
# never been exercised in a DOM. These are its first behavioural tests.


def test_the_models_view_renders_its_providers_without_expanding_them(
    rendered,
) -> None:
    """The lazy-fill budget: 135 models, zero rows until something is opened."""

    models = rendered["models"]
    assert models["providerCount"] == 3
    assert models["collapsedBodies"] == 3
    assert models["rowsWhileCollapsed"] == 0


def test_opening_a_provider_renders_one_page_of_rows_not_all_of_them(
    rendered,
) -> None:
    models = rendered["models"]
    assert models["rowsAfterOpen"] == 40
    assert models["moreLabel"] == "Show 5 more of 5"


def test_every_row_has_one_selection_box_and_one_visibility_readout(
    rendered,
) -> None:
    """One control per row and one readout of the result, not two checkboxes.

    "there seem to be 2 overlapping functions for showing/hiding" was the
    report, and two visually similar checkboxes on one line was where it began.
    """

    models = rendered["models"]
    assert models["selectBoxes"] == models["rowsAfterOpen"]
    assert models["visibilityReadouts"] == models["rowsAfterOpen"]
    assert models["readoutInputs"] == 0


def test_the_visibility_readout_says_a_state_and_never_an_imperative(
    rendered,
) -> None:
    """ "Show" in the slot that reports what is true read as a control that had
    not responded, which is the literal symptom the user described."""

    words = rendered["models"]["readoutWords"]
    assert words == ["Shown"]
    assert "Show" not in words


def test_the_readout_has_three_states_and_none_of_them_is_a_control(
    rendered,
) -> None:
    models = rendered["models"]
    assert models["readoutShown"] == "Shown"
    assert models["readoutOwnHidden"] == "Hidden"
    assert models["readoutGlobHidden"] == "Hidden by *:free"
    assert models["readoutsHaveNoInput"] is True


def test_a_glob_overruled_row_names_the_pattern_with_an_accessible_name(
    rendered,
) -> None:
    """D12: the old visibility checkbox had no accessible name of its own."""

    models = rendered["models"]
    assert models["readoutGlobPattern"] == "*:free"
    assert models["readoutGlobIsButton"] == "BUTTON"
    assert models["readoutGlobAria"] == (
        "beta/c:free is hidden by *:free. Review it in the pattern editor."
    )


def test_clicking_the_pattern_offers_to_remove_it_rather_than_doing_nothing(
    rendered,
) -> None:
    """A row a glob dictates must not be a dead end, and must not be silently
    disabled either."""

    assert rendered["models"]["patternOffer"] == "remove *:free?"


def test_a_single_row_write_goes_through_the_bulk_endpoint(rendered) -> None:
    """One write path. The per-row tick used to POST /visibility/toggle and
    then GET the whole 3.4 MB catalogue back."""

    models = rendered["models"]
    assert models["soloBulkCalls"] == 1
    assert models["soloToggleCalls"] == 0
    assert models["soloBody"]["body"]["scope"] == "selection"
    assert len(models["soloBody"]["body"]["model_refs"]) == 1


def test_a_single_row_write_does_not_refetch_the_whole_payload(rendered) -> None:
    """D4/D5: two owners of one state is what made the page unstable."""

    assert rendered["models"]["soloRefetches"] == 0


def test_single_toggle_updates_both_the_box_and_its_word(rendered) -> None:
    """The D1 regression, in the words of the report: "the hide button tick
    when unticked doesn't change to show or the other way around"."""

    models = rendered["models"]
    assert models["soloWordBefore"] == "Shown"
    assert models["soloWordAfter"] == "Hidden"


def test_a_single_row_write_repaints_the_provider_head_too(rendered) -> None:
    """D10: after four per-row ticks the head still read "2 hidden" with 7
    actually hidden, because the single path called neither of the two
    functions the bulk path calls."""

    assert rendered["models"]["soloHeadAfter"] == "1 hidden"


def test_the_action_bar_says_how_much_of_the_selection_is_already_done(
    rendered,
) -> None:
    """No tri-state control -- the count instead."""

    labels = rendered["models"]["soloBarLabels"]
    assert "Hide 1 selected" in labels
    assert "Show 1 selected (1 already shown)" in labels


def test_a_selection_covering_a_whole_provider_is_offered_one_glob(
    rendered,
) -> None:
    """Offered, not taken: automatic promotion is lossy the way Hide all was."""

    assert rendered["models"]["promoteOffer"] == "Hide all as one pattern, alpha/*"


def test_a_glob_overruled_row_keeps_a_persistent_explanation(rendered) -> None:
    """Not a toast: the row still names the pattern after the panel is
    dismissed, which is the only form of the message that is actionable."""

    models = rendered["models"]
    assert models["blockedRowText"] == "Hidden by *:free"
    assert models["blockedRowPattern"] == "*:free"
    assert models["blockedRowSurvivesDismiss"] == "*:free"


def test_the_glob_migration_previews_before_it_writes(rendered) -> None:
    """994 exact patterns and no globs is worth folding, but not silently."""

    models = rendered["models"]
    assert models["migrateBody"]["body"] == {"apply": False}
    assert "Would fold 2 exact pattern(s)" in models["migrateText"]
    assert "3 pattern(s) become 2" in models["migrateText"]
    assert "9 model(s) hidden before, 9 after" in models["migrateText"]
    assert models["migrateOffersWrite"] is True


def test_selecting_rows_shows_the_bulk_bar_with_a_whole_sentence(rendered) -> None:
    models = rendered["models"]
    assert models["barHidden"] is False
    assert models["barSentence"].startswith("9 selected across 1 provider(s)")


def test_the_provider_checkbox_is_indeterminate_when_some_rows_are_selected(
    rendered,
) -> None:
    models = rendered["models"]
    assert models["indeterminateWhenPartial"] is True
    assert models["selectAllChecked"] is True
    assert models["selectAllIndeterminate"] is False


def test_shift_clicking_selects_the_range_between_two_rows(rendered) -> None:
    assert rendered["models"]["afterShiftClick"] == 9


def test_shift_arrow_extends_the_range_and_walking_back_shrinks_it(
    rendered,
) -> None:
    """WCAG 2.2 asks for a keyboard alternative to an author-controlled drag."""

    models = rendered["models"]
    assert models["afterArrowDown"] == 3
    assert models["afterArrowUp"] == 2


def test_dragging_across_five_rows_selects_five_rows(rendered) -> None:
    assert rendered["models"]["afterDrag"] == 5


def test_a_drag_that_starts_outside_the_gutter_selects_nothing(rendered) -> None:
    """Pressing on a model ref and moving is a text gesture, not a selection."""

    assert rendered["models"]["afterNonGutterDrag"] == 0


def test_a_selection_survives_typing_in_the_filter(rendered) -> None:
    """renderModelsTree() empties the tree on every keystroke."""

    models = rendered["models"]
    assert models["barAfterTyping"].startswith("45 selected")


def test_hide_all_posts_one_bulk_request_with_no_model_refs(rendered) -> None:
    models = rendered["models"]
    assert models["bulkCalls"] == 1
    assert models["bulkBody"]["body"] == {
        "scope": "provider",
        "action": "hide",
        "provider_id": "alpha",
        "model_refs": [],
    }


def test_hide_all_under_a_filter_posts_the_filtered_refs(rendered) -> None:
    """A narrowed view is a selection, not a standing policy about a provider."""

    body = rendered["models"]["filteredBody"]["body"]
    assert body["model_refs"]
    assert all(ref.startswith("alpha/model-1") for ref in body["model_refs"])


def test_a_bulk_action_does_not_refetch_the_whole_catalogue(rendered) -> None:
    """The headline: one gesture must not cost the 3.4 MB payload."""

    assert rendered["models"]["catalogueRefetches"] == 0


def test_a_partly_honored_bulk_result_names_the_pattern_once_not_per_model(
    rendered,
) -> None:
    models = rendered["models"]
    assert models["patternMentions"] == 1
    assert "12 of them did not change" in models["partialText"]
    assert "Routing is unaffected either way." in models["partialText"]


def test_the_result_panel_offers_undo_and_undo_posts_the_previous_patterns(
    rendered,
) -> None:
    models = rendered["models"]
    assert models["hasUndo"] is True
    assert models["undoBody"]["body"] == {"allow": "", "deny": "*:free"}


def test_undo_is_gone_after_it_is_used(rendered) -> None:
    assert rendered["models"]["undoGoneAfterUse"] is True


def test_a_facet_narrows_the_tree_and_the_count_sentence_says_so(rendered) -> None:
    assert 'Showing only "hidden"' in rendered["models"]["hiddenFacetSummary"]


def test_select_all_selects_every_match_across_providers(rendered) -> None:
    models = rendered["models"]
    assert models["selectMatchesLabel"] == "Select all 3"
    assert models["crossProviderSelection"].startswith("3 selected across 3")


def test_escape_clears_the_selection(rendered) -> None:
    assert rendered["models"]["barHiddenAfterEscape"] is True


def test_the_measured_badge_still_renders_in_the_new_row(rendered) -> None:
    """The 6.3.0-6.6.0 additions must survive the redesign of the row."""

    models = rendered["models"]
    assert models["measuredBadges"] == 1
    assert models["openBodies"] == 1


def test_the_bulk_bar_and_the_result_panel_are_toggled_by_hidden_not_by_style(
    rendered,
) -> None:
    assert rendered["models"]["toggledByHidden"] is True


def test_the_provider_header_is_the_pages_one_sticky_element(rendered) -> None:
    """One per rendered provider, where there were none at all."""

    assert rendered["models"]["stickyHeads"] == 3


# --------------------------------------------------------- the retry ladder --


def test_ladder_headline_and_root_cause_render_in_the_chain(rendered) -> None:
    """The row said one status; the panel now says every one of them."""
    detail = rendered["requestDetail"]["ladder"]

    assert detail["chainHidden"] is False
    assert detail["ladderSummaries"] == [
        "2 tries · 1\N{MULTIPLICATION SIGN}429, 1\N{MULTIPLICATION SIGN}502 · 2 keys · 3s sleeping · 52s on the provider block"
    ]
    assert detail["ladderRootCauses"] == [
        "2 tries across 2 keys: 1\N{MULTIPLICATION SIGN}429, 1\N{MULTIPLICATION SIGN}502 — 2s of the 107s were MCC backoff sleeps"
    ]


def test_ladder_try_rows_render_one_per_try_with_status_and_wait(rendered) -> None:
    tries = rendered["requestDetail"]["ladder"]["ladderTries"]

    assert len(tries) == 3
    assert tries[0].startswith("#1 · key 0 aa...bb · 429 · 410ms · waited 2700ms")
    assert "retry-after 12s" in tries[1]
    # The wait rows keep their place in the sequence rather than vanishing.
    assert tries[2] == "#3 · limiter_wait · waited 51900ms"


def test_missing_ladder_numbers_render_as_a_dash_not_zero(rendered) -> None:
    """A term nobody measured is omitted, never printed as ``0``."""
    tries = rendered["requestDetail"]["ladder"]["ladderTries"]

    # The 502 try had no recorded wait; it must not claim "waited 0ms".
    assert "waited" not in tries[1]
    # The 429 try published no Retry-After; it must not claim "retry-after 0s".
    assert "retry-after" not in tries[0]


def test_ladder_credential_decisions_name_the_bench_and_the_non_bench(
    rendered,
) -> None:
    decisions = rendered["requestDetail"]["ladder"]["ladderDecisions"]

    assert decisions == [
        "key 0 aa...bb — benched 60s (rate_limit): 429, no Retry-After -- "
        "operator cooldown 60s",
        "key 2 cc...dd — health unchanged: 502 is not credential-shaped",
    ]


def test_the_redacted_upstream_body_is_shown_per_try(rendered) -> None:
    assert rendered["requestDetail"]["ladder"]["ladderBodies"] == [
        '{"detail":"Too many requests"}'
    ]


def test_a_truncated_attempt_says_how_far_the_answer_got(rendered) -> None:
    """The row reads "timeout" either way; only this says the reader got text.

    Also the panel-visibility case: one attempt with no ladder used to hide the
    whole chain, which is the only place the sentence is ever shown.
    """
    detail = rendered["requestDetail"]["truncated"]

    assert detail["chainHidden"] is False
    assert detail["truncations"] == [
        "ended early after 1,333 chars; the answer is incomplete"
        " (sent to the client as max_tokens)"
    ]


def test_a_continued_attempt_names_the_model_that_stalled_and_the_char_count(
    rendered,
) -> None:
    """The stream has no seam on purpose, so the row carries the whole story."""
    detail = rendered["requestDetail"]["continued"]

    assert detail["chainHidden"] is False
    assert detail["continuations"] == [
        "continued here after commandcode/z-ai/glm-5.3-flash stalled at 1,333 chars"
    ]


def test_a_continuation_that_was_not_usable_does_not_claim_a_rescue(
    rendered,
) -> None:
    """ "Accepted: false" is the reader getting the short message after all."""
    detail = rendered["requestDetail"]["continuedUnusable"]

    assert detail["continuations"] == [
        "commandcode/z-ai/glm-5.3-flash stalled at 1,333 chars;"
        " the continuation was not usable"
    ]


def test_a_truncated_tool_call_says_it_could_not_be_completed(rendered) -> None:
    """The one case that still errors has to explain itself, not look identical."""
    detail = rendered["requestDetail"]["truncatedTool"]

    assert detail["truncations"] == ["stalled inside a tool call — cannot be completed"]


def test_a_single_try_attempt_renders_no_ladder(rendered) -> None:
    """Nothing was hidden, so the panel adds nothing -- and stays hidden."""
    detail = rendered["requestDetail"]["singleTry"]

    assert detail["chainHidden"] is True
    assert detail["ladderSummaries"] == []
    assert detail["ladderRootCauses"] == []
    assert detail["ladderTries"] == []


def test_a_single_try_with_a_probe_still_shows_its_ladder(rendered) -> None:
    """The routed-around 429 is the case the operator most needs to see.

    One upstream try hides nothing, so the panel stays shut -- but one try
    plus a diagnostic probe is the whole story of why the request went
    somewhere else, and the gate read only summary.tries.
    """
    detail = rendered["requestDetail"]["singleTryWithProbe"]

    assert detail["chainHidden"] is False
    # The census renders the real multiplication sign; imported rather than
    # spelled, so the linter's confusable check stays on for this file.
    assert detail["ladderSummaries"] == [f"1 try · 1 probe · 1{_TIMES}429 · 1 keys"]
    assert len(detail["ladderTries"]) == 2
    assert "429" in detail["ladderTries"][0]
    assert (
        "probe — the key is healthy, the model is limited" in detail["ladderTries"][1]
    )
    assert detail["ladderDecisions"] == [
        "key 0 aa...bb — moonshotai/kimi-k3 benched 60s (rate_limit):"
        " 429, no Retry-After -- moonshotai/kimi-k3 benched 60s on this key"
    ]


def test_the_local_answers_filter_defaults_to_hide(rendered) -> None:
    """The store's default is "all"; only the dashboard prefers "hide"."""
    analytics = rendered["analytics"]
    assert analytics["defaultLocal"] == "hide"
    assert "local=hide" in analytics["loadSendsLocal"]


def test_a_select_applies_itself_and_returns_to_page_one(rendered) -> None:
    """One load per change, no Apply click, and the offset reset with it."""
    analytics = rendered["analytics"]
    assert "offset=25" in analytics["pagedUrl"]
    assert analytics["statusChangeLoads"] == 1
    assert "status=error" in analytics["statusChangeUrl"]
    assert "offset=0" in analytics["listUrlAfterStatusChange"]
    assert analytics["localChangeLoads"] == 1
    assert "local=only" in analytics["localChangeUrl"]


def test_typing_reloads_once_after_the_pause_and_not_per_keystroke(rendered) -> None:
    analytics = rendered["analytics"]
    assert analytics["loadsWhileTyping"] == 0
    assert analytics["loadsAfterTypingPause"] == 1
    assert "q=abc" in analytics["typedUrl"]


def test_enter_applies_immediately_and_the_debounce_does_not_fire_again(
    rendered,
) -> None:
    analytics = rendered["analytics"]
    assert analytics["loadsRightAfterEnter"] == 1
    assert analytics["loadsAfterEnterAndPause"] == 1


def test_clear_filters_restores_hide_and_reloads_once(rendered) -> None:
    analytics = rendered["analytics"]
    assert analytics["clearLoads"] == 1
    assert analytics["localAfterClear"] == "hide"
    assert analytics["searchAfterClear"] == ""
    assert "local=hide" in analytics["clearUrl"]
    assert "q=" not in analytics["clearUrl"]


def test_the_filter_choice_round_trips_through_persisted_state(rendered) -> None:
    analytics = rendered["analytics"]
    assert analytics["persisted"]["local"] == "only"
    assert analytics["persisted"]["search"] == "abc"
    assert analytics["persistedAfterClear"]["local"] == "hide"
    assert "search" not in analytics["persistedAfterClear"]


def test_a_key_with_model_benches_renders_the_model_sub_line(rendered) -> None:
    """The operator asking "why is my key benched" gets the answer on the row.

    A tooltip would not: the whole point of the (key, model) bench is that the
    key reads HEALTHY, so nothing invites a hover.
    """
    rows = rendered["keyManager"]["scoped"]

    assert len(rows) == 2
    line = rows[0]["benchLine"]
    # Capped at three, with the count of what was left out.
    assert line.startswith("moonshotai/kimi-k3 ")
    assert "nvidia/nemotron-3-ultra" in line
    assert "minimaxai/minimax-m2" in line
    assert "openai/gpt-oss-120b" not in line
    assert line.endswith("+1 more")
    assert "still serves" in rows[0]["benchTitle"]
    # A slot the engine keeps HEALTHY must not read as plain "HEALTHY".
    assert rows[0]["badge"] == "HEALTHY (4 models)"
    assert "rate-limited for moonshotai/kimi-k3" in rows[0]["badgeTitle"]
    # A benched key shows both facts: the key's own window and the models.
    assert rows[1]["badge"] == "COOLDOWN (1 model)"
    assert rows[1]["benchLine"].startswith("moonshotai/kimi-k3 ")


def test_a_key_benched_for_credits_says_so_on_the_row(rendered) -> None:
    """ "COOLDOWN — back in 55s" reads as a throttle that lifts on its own.

    It does not. The only thing that clears an exhausted balance is a top-up,
    so the pool publishes the reason and the badge prints it.
    """
    rows = rendered["keyManager"]["credits"]

    assert len(rows) == 1
    assert rows[0]["badge"] == "COOLDOWN — credits exhausted"
    assert "benched: credits exhausted, 55s left" in rows[0]["badgeTitle"]
    # Additive: a bench with no published reason is untouched (below).
    assert rows[0]["benchLine"] == ""


def test_a_healthy_key_with_no_model_benches_renders_exactly_as_before(
    rendered,
) -> None:
    """Additive only: no model benches, no sub-line, no badge suffix."""
    rows = rendered["keyManager"]["plain"]

    assert len(rows) == 1
    assert rows[0]["benchLine"] == ""
    assert "key-model-benches" not in rows[0]["html"]
    assert rows[0]["badge"] == "HEALTHY"
    assert rows[0]["badgeTitle"] == "HEALTHY \u2014 7 requests, 0 failures"


# --------------------------------------------------------------- route rails
# Drag, multi-select, cross-tier copy and move, the primary swap, undo, and the
# per-entry pause -- driven through the same document the dashboard ships.


def test_a_chain_row_has_a_drag_grip_and_keeps_its_arrows(rendered) -> None:
    """The grip is added; the arrows stay as the WCAG 2.2 keyboard equivalent."""
    routing = rendered["routing"]

    assert routing["present"]
    # One grip and one pause toggle per draggable node, primaries included.
    assert routing["gripCount"] == routing["railNodes"]
    assert routing["pauseButtons"] == routing["railNodes"]
    assert routing["primaryHasGrip"]
    # Two arrows per fallback row, untouched.
    assert routing["arrowsKept"] == routing["opusRows"] * 2


def test_a_drag_that_starts_outside_the_grip_reorders_nothing(rendered) -> None:
    """Pressing the row itself is a click, not a handle."""
    routing = rendered["routing"]

    assert routing["opusAfterStray"] == routing["opusBeforeStray"]


def test_a_touch_scrolls_the_card_unless_it_starts_on_the_grip(rendered) -> None:
    """`touch-action: none` is scoped to the grip so the page still scrolls."""
    routing = rendered["routing"]

    assert routing["touchOnRowStartsADrag"] is False
    assert routing["opusAfterTouchScroll"]
    assert routing["touchOnGripStartsADrag"] is True


def test_dragging_a_row_down_one_reorders_the_chain(rendered) -> None:
    routing = rendered["routing"]

    assert routing["opusBeforeStray"].startswith("p1/o1,p1/o2,")
    assert routing["opusAfterReorder"].startswith("p1/o2,p1/o1,")


def test_ctrl_clicking_adds_a_second_row_to_the_selection(rendered) -> None:
    routing = rendered["routing"]

    assert routing["afterPlainClick"] == 1
    assert routing["afterCtrlClick"] == 2


def test_shift_clicking_selects_the_range_within_one_rail(rendered) -> None:
    routing = rendered["routing"]

    assert routing["afterShiftClick"] == 4


def test_shift_arrow_extends_the_selection_and_walking_back_shrinks_it(
    rendered,
) -> None:
    """Walking back must not leave a trail of selected rows behind the cursor."""
    routing = rendered["routing"]

    assert routing["afterShiftArrowDown"] == 5
    assert routing["afterShiftArrowBack"] == 4


def test_escape_clears_the_route_selection(rendered) -> None:
    routing = rendered["routing"]

    assert routing["afterEscape"] == 0


def test_dragging_a_group_keeps_rail_order_not_click_order(rendered) -> None:
    """Picked bottom-up, landed top-down: what the reader is looking at."""
    routing = rendered["routing"]

    assert routing["groupSelected"] == 2
    assert routing["opusAfterGroupCopy"].startswith("p1/s1,p1/s2,")


def test_dragging_to_another_tier_copies_and_leaves_the_source_intact(
    rendered,
) -> None:
    routing = rendered["routing"]

    assert "p1/s1" in routing["opusAfterGroupCopy"]
    assert routing["sonnetAfterGroupCopy"] == routing["sonnetBeforeGroup"]
    assert "still in the Sonnet chain" in routing["groupSentence"]


def test_shift_dragging_to_another_tier_moves_and_empties_the_source(
    rendered,
) -> None:
    """An empty chain is legal; only an empty primary is refused."""
    routing = rendered["routing"]

    assert routing["sonnetAfterMove"] == ""
    assert routing["opusAfterMove"].startswith("p1/s1,p1/s2,")
    assert "still in" not in routing["moveSentence"]


def test_a_cross_tier_move_marks_both_chains_unsaved(rendered) -> None:
    routing = rendered["routing"]

    assert routing["keysAfterCrossTierMove"] == [
        "MODEL_OPUS_FALLBACKS",
        "MODEL_SONNET_FALLBACKS",
    ]


def test_a_copy_onto_a_chain_that_already_has_the_ref_moves_it_instead(
    rendered,
) -> None:
    """Duplicates are dropped on save, so a second row would just vanish."""
    routing = rendered["routing"]

    assert routing["duplicateOccurrences"] == 1
    assert "already in the Opus chain" in routing["duplicateSentence"]
    assert "moved instead of copied" in routing["duplicateSentence"]


def test_a_copy_is_refused_when_the_target_primary_is_that_ref(rendered) -> None:
    """A chain entry equal to its own primary could never fire."""
    routing = rendered["routing"]

    assert routing["sonnetUnchangedByRefusal"]
    assert routing["refusalSentence"] == (
        "Sonnet already routes to p1/s1 first, so it was not added to its own chain."
    )


def test_dropping_on_the_primary_slot_swaps_and_demotes_the_old_primary(
    rendered,
) -> None:
    routing = rendered["routing"]

    assert routing["haikuPrimaryAfterDrop"] == routing["haikuPromoted"]
    assert routing["haikuChainAfterDrop"].startswith(routing["haikuDemoted"])
    assert "is now the Haiku route" in routing["primarySwapSentence"]
    assert "is fallback 1" in routing["primarySwapSentence"]


def test_a_primary_swap_counts_two_unsaved_changes(rendered) -> None:
    """Both halves of the rail are settings; a swap writes both."""
    routing = rendered["routing"]

    assert routing["dirtyAfterPrimarySwap"] == 2


def test_a_primary_is_never_shift_moved_out_of_its_own_rail(rendered) -> None:
    """An empty MODEL fails validation and the server refuses to start."""
    routing = rendered["routing"]

    assert routing["haikuPrimarySurvivedSteal"]
    assert routing["opusUnchangedBySteal"]
    assert routing["strandedSentence"] == (
        "Haiku needs a model of its own -- drag a copy instead, "
        "or promote a fallback first."
    )


def test_dragging_a_primary_onto_its_own_first_fallback_swaps_them(
    rendered,
) -> None:
    """The same swap the down arrow already performs, reached by drag."""
    routing = rendered["routing"]

    assert routing["sonnetPrimaryAfterOwnDrop"] == "p1/s1"
    assert routing["sonnetAfterOwnPrimaryDrop"].startswith("p1/s0")
    assert "traded places" in routing["swapSentence"]


def test_ctrl_z_undoes_the_last_drag_and_only_the_last(rendered) -> None:
    """Depth one: one drag is one entry, and the entry is spent when used."""
    routing = rendered["routing"]

    assert routing["opusAfterUndo"] == routing["opusBeforeStray"]
    assert routing["opusAfterSecondUndo"] == routing["opusAfterUndo"]


def test_ctrl_z_inside_a_model_combobox_does_not_undo_the_drag(rendered) -> None:
    """Native text undo belongs to whoever is typing."""
    routing = rendered["routing"]

    assert routing["opusAfterTypingUndo"] == routing["opusBeforeTypingUndo"]


def test_undo_restores_both_chains_after_a_cross_tier_move(rendered) -> None:
    routing = rendered["routing"]

    assert routing["sonnetAfterMoveUndo"] == "p1/s1,p1/s2"
    assert routing["opusAfterMoveUndo"] == routing["opusBeforeGroup"]


def test_the_route_status_panel_is_a_whole_sentence_and_is_toggled_by_hidden(
    rendered,
) -> None:
    """Never a bare number, and `hidden`, never `style.display`."""
    routing = rendered["routing"]

    assert routing["statusHiddenAttr"] is False
    assert routing["reorderSentence"].endswith("press Apply.")
    assert routing["reorderSentence"].startswith("Moved 1 model inside the Opus chain")
    assert routing["statusHiddenAfterDismiss"] is True
    assert routing["statusInlineDisplay"] == ""


def test_pausing_a_fallback_posts_one_key_and_leaves_a_dirty_drag_dirty(
    rendered,
) -> None:
    """The highest-risk interaction: an immediate write beside an unsaved drag.

    The commit renders the whole file from the values on disk plus this
    update, so a key the build did not return is written back unchanged --
    which is why the drag survives. Proven on the wire: one call, and the body
    names one route entry and nothing else.
    """
    routing = rendered["routing"]

    assert routing["pauseCalls"] == 1
    assert routing["pauseBodyKeys"] == ["model_key", "model_ref", "paused"]
    assert routing["pauseBody"] == {
        "model_key": "MODEL_OPUS",
        "model_ref": routing["pausedRef"],
        "paused": True,
    }
    assert routing["dirtyUnchangedByPause"]
    assert routing["dirtyAfterPause"] > 0


def test_a_paused_row_stays_visible_with_a_resume_button(rendered) -> None:
    """Pausing is not hiding: the ref stays on screen, whole and undoable."""
    routing = rendered["routing"]

    assert routing["pausedRowHidden"] is False
    assert routing["pausedRowClass"] is True
    assert routing["pausedRowRef"] == routing["pausedRef"]
    assert routing["pausedButtonLabel"] == "Resume"
    assert routing["pausedAriaPressed"] == "true"
    assert routing["pausedChipShown"] is True


def test_the_status_panel_offers_undo_after_a_pause(rendered) -> None:
    routing = rendered["routing"]

    assert routing["pauseOffersUndo"] == ["Undo", "Dismiss"]
    assert routing["pauseSentence"].startswith("Paused ")
    assert "without spending an attempt" in routing["pauseSentence"]
    assert routing["resumedButtonLabel"] == "Pause"
    assert routing["resumeSentence"].startswith("Resumed ")


def test_a_failed_pause_announces_a_failure_in_the_route_status_panel(
    rendered,
) -> None:
    """A refused pause used to be silent where the operator was looking.

    ``showMessage`` writes into #messageArea at the top of the page; the pause
    control speaks through #routeStatus beside the rail. Reporting only into
    the first left the row snapping back with no explanation at all.
    """
    routing = rendered["routing"]

    assert routing["refusedPauseWasPausedBefore"] is False
    assert routing["refusedPauseSentence"].startswith("Could not pause ")
    assert "nothing changed" in routing["refusedPauseSentence"]
    assert "read-only" in routing["refusedPauseSentence"]
    # No Undo: nothing happened, so there is nothing to undo.
    assert routing["refusedPausePanelButtons"] == ["Dismiss"]
    # The top-of-page message area still gets it too.
    assert "read-only" in routing["refusedPauseMessageArea"]


def test_a_failed_pause_leaves_the_row_in_its_previous_state(rendered) -> None:
    """The row tells the truth about the server, both ways it can fail."""
    routing = rendered["routing"]

    assert routing["refusedPauseRowStillUnpaused"] is True
    assert routing["refusedPauseButtonLabel"] == "Pause"
    assert routing["refusedPauseButtonDisabled"] is False

    assert routing["failedPauseSentence"].startswith("Could not pause ")
    assert "the server is restarting" in routing["failedPauseSentence"]
    assert routing["failedPausePanelButtons"] == ["Dismiss"]
    assert routing["failedPauseRowStillUnpaused"] is True
    assert routing["failedPauseButtonLabel"] == "Pause"


def test_undo_disables_the_row_control_while_it_is_in_flight(rendered) -> None:
    """Undo used to pass ``button: null``, leaving the row's toggle live.

    Two writes for the same ref could then be in flight together and land in
    either order -- the exact race the first click's disable guard exists to
    prevent.
    """
    routing = rendered["routing"]

    assert routing["beforeUndoRowPaused"] is True
    assert routing["rowToggleDisabledDuringUndo"] is True
    assert routing["rowToggleEnabledAfterUndo"] is False
    assert routing["afterUndoRowPaused"] is False


def test_the_deadline_calculator_stops_counting_a_paused_model(rendered) -> None:
    """The calculator claims to reproduce the server for your own routes."""
    routing = rendered["routing"]

    assert routing["chainLengthWhilePaused"] == routing["chainLengthBeforePause"] - 1


def test_a_paused_primary_is_dropped_from_the_count_and_stays_on_screen(
    rendered,
) -> None:
    routing = rendered["routing"]

    assert routing["haikuPrimaryPaused"] is True
    assert routing["haikuPrimaryStillShowsItsRef"] == routing["haikuPromoted"]
    # Haiku is primary + one demoted fallback; pausing the primary leaves one.
    assert routing["haikuChainLengthWithPausedPrimary"] == 1
    assert routing["haikuPrimaryResumed"] is True


def test_the_arrow_buttons_still_reorder_after_the_drag_shipped(rendered) -> None:
    routing = rendered["routing"]

    assert routing["arrowsStillReorder"]


def test_a_drag_leaves_no_indicator_or_ghost_nodes_behind(rendered) -> None:
    """One indicator instance is reused, and it is removed when the drag ends."""
    routing = rendered["routing"]

    assert routing["strayIndicators"] == 0


def test_custom_provider_card_labels_its_refresh_and_updates_the_count(
    rendered,
) -> None:
    """The card's only discovery affordance must be findable and honest.

    It read "Test" while the identical call on a static remote card read
    "Refresh models", and the count line was written once at render time -- so
    the card could say "0 models" straight after a refresh that returned three.
    """
    card = rendered["customProviders"]
    assert card["present"] is True
    assert card["buttonLabel"] == "Refresh models"
    assert card["detailsBefore"].endswith("0 models")
    assert card["detailsAfter"].endswith("3 models")
    assert card["pillAfter"] == "3 models"


def test_a_create_with_a_failed_discovery_does_not_render_a_healthy_card(
    rendered,
) -> None:
    card = rendered["customProviders"]
    assert card["failedPill"] == "PermissionDeniedError"
    assert "PermissionDeniedError" in card["failedMeta"]
    assert card["failedDetails"].endswith("0 models")
    assert "model discovery failed" in card["message"]
    assert "Refresh models" in card["message"]


def test_the_custom_card_shows_the_dialect_its_host_was_measured_speaking(
    rendered,
) -> None:
    """The fact that decides whether ``max`` can leave the process.

    A static provider declares its effort vocabulary in a profile; a custom one
    could not, so every custom host was assumed to speak the four standard
    OpenAI words and a request for ``max`` went out as ``high``.
    """
    card = rendered["customProviders"]

    assert card["dialectLabel"] == (
        "reasoning dialect: learned {low, high, max} on 2026-09-01"
    )
    assert card["dialectValue"] == "low, high, max"
    assert card["probeButton"] == "Probe reasoning dialect"


def test_the_custom_card_offers_disable_as_a_gesture(rendered) -> None:
    assert rendered["customProviders"]["toggleLabel"] == "Disable"


def test_a_custom_pool_shows_per_key_health_like_a_static_one(rendered) -> None:
    """``key_health()`` had exactly one caller, and no custom pool could reach it.

    The machinery was always shared -- custom pools rotate, bench and cool down
    on the same engine -- so this is the readout arriving, not the behaviour.
    """
    card = rendered["customProviders"]

    assert card["keyRowCount"] == 2
    assert card["keyHealthStates"] == ["HEALTHY", "HEALTHY (1 model)"]
    assert card["keyBenches"] == ["m1 42s"]


def test_the_wire_pane_shows_the_shape_of_what_came_back(rendered) -> None:
    """ "reasoning requested 1, returned 0" is two measurements; this is the second."""
    detail = rendered["requestDetail"]["responseShape"]

    assert detail["shapePanes"] == 1
    assert detail["shapeTerms"] == [
        "content",
        "finish_reason",
        "usage",
        "first chunk",
        "chunks",
    ]
    assert detail["shapeValues"][0] == "7 deltas, 135 chars"
    assert detail["shapeValues"][1] == "stop"
    assert detail["shapeValues"][2] == "completion_tokens, prompt_tokens"
    assert detail["shapeValues"][3] == "14700 ms"
    # A reasoning field went out and no reasoning delta came back -- exactly
    # the ambiguity this pane exists to settle.
    assert "reasoning_content" not in detail["shapeTerms"]


def test_an_unmeasured_attempt_renders_no_shape_pane_rather_than_an_empty_one(
    rendered,
) -> None:
    """NULL is "not measured", which is not the same as "nothing came back"."""
    assert rendered["requestDetail"]["responseShapeAbsent"]["shapePanes"] == 0


# --------------------------------------------------------------- coding agents


def test_coding_agents_view_renders_one_card_per_harness(rendered: dict) -> None:
    agents = rendered["codingAgents"]

    assert agents["present"] is True
    assert agents["cardCount"] == 15
    assert [card["id"] for card in agents["cards"]] == [
        "claude",
        "codex",
        "pi",
        "opencode",
        "kilo",
        "commandcode_cli",
        "kimi_code",
        "qwen_code",
        "crush",
        "cline_cli",
        "goose",
        "aider",
        "droid",
        "gemini_cli",
        "antigravity",
    ]
    assert [card["title"] for card in agents["cards"]] == [
        "Claude Code",
        "Codex CLI",
        "Pi",
        "OpenCode",
        "Kilo CLI",
        "Command Code",
        "Kimi Code",
        "Qwen Code",
        "Crush",
        "Cline",
        "Goose",
        "Aider",
        "Droid",
        "Gemini CLI",
        "Antigravity",
    ]
    assert [card["command"] for card in agents["cards"]] == [
        "mcc-claude",
        "mcc-codex",
        "mcc-pi",
        "mcc-opencode",
        "mcc-kilo",
        "mcc-commandcode",
        "mcc-kimi",
        "mcc-qwen",
        "mcc-crush",
        "mcc-cline",
        "mcc-goose",
        "mcc-aider",
        "mcc-droid",
        "mcc-gemini",
        # Antigravity publishes no command at all, and the card says so rather
        # than printing one that does not exist.
        None,
    ]


def test_a_gemini_harness_names_googles_protocol_on_its_card(
    rendered: dict,
) -> None:
    """The third door, and the card is where a user finds out which one.

    It matters for the same reason the chat-completions cards do: the Requests
    page labels these rows ``gemini`` rather than ``anthropic``, and the
    endpoint it shows is a path with a model in it.
    """

    cards = {card["id"]: card for card in rendered["codingAgents"]["cards"]}

    assert "/v1beta/models" in cards["gemini_cli"]["meta"]
    assert "GEMINI_CLI_SYSTEM_SETTINGS_PATH" in cards["gemini_cli"]["meta"]
    assert cards["gemini_cli"]["unavailable"] is False
    assert cards["gemini_cli"]["state"] == "Installed"


def test_an_unservable_harness_states_the_reason_and_offers_no_command(
    rendered: dict,
) -> None:
    """ "Not servable" is a different fact from "Not installed".

    One is something a user fixes by installing the CLI; the other is
    something MCC measured and cannot fix. Printing an ``mcc-`` command for
    the second would be a lie the page cannot walk back, so the card carries
    the dated reason instead and says the launcher does not exist.
    """

    cards = {card["id"]: card for card in rendered["codingAgents"]["cards"]}
    antigravity = cards["antigravity"]

    assert antigravity["state"] == "Not servable"
    assert antigravity["unavailable"] is True
    assert antigravity["command"] is None
    assert antigravity["commandLines"] == []
    assert antigravity["installHint"] is None
    assert "verified 2026-09-02" in antigravity["unavailableReason"]
    assert "agy 1.0.14" in antigravity["unavailableReason"]
    assert "MCC publishes no command" in antigravity["meta"]


def test_coding_agents_card_lists_every_command_with_a_copy_button(
    rendered: dict,
) -> None:
    """The page answers "what can I type" without sending anyone to the docs."""

    claude = next(
        card for card in rendered["codingAgents"]["cards"] if card["id"] == "claude"
    )

    assert [line["command"] for line in claude["commandLines"]] == [
        "mcc-claude",
        "mcc-claude --discover-models",
        "mcc-claude-old",
        "fcc-claude",
        "mcc-rtk enable claude",
    ]
    assert [line["kind"] for line in claude["commandLines"]] == [
        "primary",
        "flag",
        "flag",
        "legacy",
        "rtk",
    ]
    assert all(line["hasCopy"] for line in claude["commandLines"])
    assert all(line["help"] for line in claude["commandLines"])


def test_a_config_owning_harness_names_the_variable_it_is_pointed_with(
    rendered: dict,
) -> None:
    """OpenCode's card has to say MCC owns a file, not that it edits theirs."""

    opencode = next(
        card for card in rendered["codingAgents"]["cards"] if card["id"] == "opencode"
    )

    assert "OPENCODE_CONFIG" in opencode["meta"]
    assert "your own config file is never edited" in opencode["meta"]
    assert "opencode-config.json" in opencode["meta"]
    assert 'mcc-opencode run "<prompt>"' in [
        line["command"] for line in opencode["commandLines"]
    ]


def test_a_merging_harness_names_the_users_file_and_the_one_key_mcc_writes(
    rendered: dict,
) -> None:
    """Command Code publishes no override, so its card has to be honest about it.

    The card is the only place a user sees that MCC edited a document they
    wrote, which key it owns, and that a backup was taken first.
    """

    card = next(
        entry
        for entry in rendered["codingAgents"]["cards"]
        if entry["id"] == "commandcode_cli"
    )

    assert "Config file" in card["meta"]
    assert ".commandcode/providers.json" in card["meta"].replace("\\", "/")
    assert "provider.mcc" in card["meta"]
    assert "every other key is left byte-for-byte" in card["meta"]
    assert "backed up before the first edit" in card["meta"]
    assert "Models9" in card["meta"]
    assert card["defaulted"] is not None and "2 model(s)" in card["defaulted"]
    assert "mcc-commandcode --disconnect" in [
        line["command"] for line in card["commandLines"]
    ]


def test_a_flag_owning_harness_names_the_flag_it_is_pointed_with(
    rendered: dict,
) -> None:
    """Kimi Code's card has to say MCC owns a file, not that it edits theirs.

    Same guarantee as OpenCode's row above, told with the lever Kimi Code
    actually publishes: it takes ``--config-file`` on the command line where
    OpenCode reads a variable, and neither one touches the user's document.
    """

    kimi = next(
        card for card in rendered["codingAgents"]["cards"] if card["id"] == "kimi_code"
    )

    assert "--config-file" in kimi["meta"]
    assert "passed for this launch only" in kimi["meta"]
    assert "your own config file is never edited" in kimi["meta"]
    assert "kimi-code-config.toml" in kimi["meta"]
    # A card that owns its own file must never also claim a merged key: that
    # row is the one that says MCC edited a document the user wrote.
    assert "Merged key" not in kimi["meta"]
    assert "Models7" in kimi["meta"].replace(" ", "")
    assert kimi["defaulted"] is not None and "1 model(s)" in kimi["defaulted"]
    assert "mcc-kimi -m mcc/<provider>/<model>" in [
        line["command"] for line in kimi["commandLines"]
    ]


def test_coding_agents_card_shows_not_installed_state_without_crashing(
    rendered: dict,
) -> None:
    """A missing CLI is a normal state, and the card offers the vendor's line."""

    pi = next(card for card in rendered["codingAgents"]["cards"] if card["id"] == "pi")
    codex = next(
        card for card in rendered["codingAgents"]["cards"] if card["id"] == "codex"
    )

    assert pi["state"] == "Not installed"
    assert pi["installed"] is False
    assert pi["installHint"].startswith("Install Pi with:")
    assert codex["state"] == "Installed"
    # MCC never installs a coding agent, so an installed card offers no hint
    # and no card offers a button that would run one.
    assert codex["installHint"] is None
    assert rendered["scriptErrors"] == []


def test_coding_agents_card_shows_defaulted_capability_badge(rendered: dict) -> None:
    codex = next(
        card for card in rendered["codingAgents"]["cards"] if card["id"] == "codex"
    )

    assert codex["defaulted"] is not None
    assert "3 model(s)" in codex["defaulted"]
    assert "no provider published one" in codex["defaulted"]
    assert "codex-model-catalog.json" in codex["meta"]
    assert "2026-09-01T09:12:44Z" in codex["meta"]
    assert "Models12" in codex["meta"].replace(" ", "")


def test_a_process_local_catalogue_names_no_file_on_disk(rendered: dict) -> None:
    pi = next(card for card in rendered["codingAgents"]["cards"] if card["id"] == "pi")
    claude = next(
        card for card in rendered["codingAgents"]["cards"] if card["id"] == "claude"
    )

    assert "no file on disk" in pi["meta"]
    assert "Fetched by the agent itself" in claude["meta"]


def test_the_coding_agents_page_separates_a_harness_from_a_provider(
    rendered: dict,
) -> None:
    """The one paragraph that stops `opencode` meaning two things at once."""

    note = rendered["codingAgents"]["gatewayNote"]

    assert "A coding agent is not a provider." in note
    assert "downstream" in note
    assert "upstream" in note


def test_a_variable_owning_harness_names_the_document_mcc_owns(
    rendered: dict,
) -> None:
    """Qwen Code and Crush both take a variable, and neither edits a user file.

    Qwen's variable names a settings *file*; Crush's names a config
    *directory*. Both cards have to say the same thing OpenCode's does -- MCC
    owns a document of its own -- because that is the guarantee, not the
    mechanism.
    """

    cards = {card["id"]: card for card in rendered["codingAgents"]["cards"]}

    qwen = cards["qwen_code"]
    assert "QWEN_CODE_SYSTEM_SETTINGS_PATH" in qwen["meta"]
    assert "your own config file is never edited" in qwen["meta"]
    assert "qwen-code-settings.json" in qwen["meta"]
    assert 'mcc-qwen "<prompt>"' in [line["command"] for line in qwen["commandLines"]]

    crush = cards["crush"]
    assert "CRUSH_GLOBAL_CONFIG" in crush["meta"]
    assert "your own config file is never edited" in crush["meta"]
    assert 'mcc-crush run "<prompt>"' in [
        line["command"] for line in crush["commandLines"]
    ]


def test_a_harness_whose_binary_is_missing_still_renders(rendered: dict) -> None:
    """Crush is not installed in the fixture, and its card must survive that.

    A not-installed harness is the common case for a new one, so the card has
    to render its commands and print that CLI's own install line rather than
    disappearing or throwing.
    """

    crush = next(
        card for card in rendered["codingAgents"]["cards"] if card["id"] == "crush"
    )

    assert crush["installed"] is False
    assert crush["command"] == "mcc-crush"
    assert crush["installHint"] is not None
    assert "@charmland/crush" in crush["installHint"]
    assert len(crush["commandLines"]) == 3


def test_a_chat_completions_harness_names_that_protocol_on_its_card(
    rendered: dict,
) -> None:
    """Three of the four newest agents arrive through a different door.

    The card is where a user finds out which one, and it matters: the
    Requests page labels those rows ``openai_chat`` rather than ``anthropic``.
    """

    cards = {card["id"]: card for card in rendered["codingAgents"]["cards"]}

    for harness_id in ("cline_cli", "goose", "aider"):
        assert "chat/completions" in cards[harness_id]["meta"], harness_id
    # Droid is the exception and its card has to say so, or the page would
    # imply a translation layer that is not there.
    assert "/v1/messages" in cards["droid"]["meta"]


def test_a_flag_owning_openai_harness_names_the_flag_and_the_file(
    rendered: dict,
) -> None:
    cards = {card["id"]: card for card in rendered["codingAgents"]["cards"]}

    cline = cards["cline_cli"]
    assert "--config" in cline["meta"]
    assert "your own config file is never edited" in cline["meta"]
    assert "providers.json" in cline["meta"]

    aider = cards["aider"]
    assert "--model-metadata-file" in aider["meta"]
    assert "aider-model-metadata.json" in aider["meta"]

    droid = cards["droid"]
    assert "--settings" in droid["meta"]
    assert "droid-settings.json" in droid["meta"]


def test_a_harness_with_no_catalogue_at_all_still_renders(rendered: dict) -> None:
    """Goose has no generated file, and the card must not imply one.

    ``catalogue: null`` used to mean only "the agent fetches its own model
    list" (Claude Code). Goose is the first harness where it also means "MCC
    writes nothing anywhere", so the card has to render without a path, a
    timestamp or a model count -- and without throwing.
    """

    goose = next(
        card for card in rendered["codingAgents"]["cards"] if card["id"] == "goose"
    )

    assert goose["installed"] is False
    assert goose["command"] == "mcc-goose"
    assert goose["installHint"] is not None
    assert "github.com/block/goose" in goose["installHint"]
    assert len(goose["commandLines"]) == 3
    assert goose["defaulted"] is None


def test_rtk_checkboxes_render_from_harness_list(rendered: dict) -> None:
    toggles = rendered["rtkToggles"]

    assert [toggle["harness"] for toggle in toggles] == ["claude", "codex", "pi"]
    assert [toggle["id"] for toggle in toggles] == [
        "rtkAgent-claude",
        "rtkAgent-codex",
        "rtkAgent-pi",
    ]
    assert [toggle["label"] for toggle in toggles] == ["Claude Code", "Codex CLI", "Pi"]
    # The checked state comes from /admin/api/rtk, not from the harness list.
    assert [toggle["checked"] for toggle in toggles] == [False, True, False]


def test_a_fresh_install_renders_the_coding_agents_page_empty_not_broken(
    fresh_install: dict,
) -> None:
    assert fresh_install["codingAgents"]["cardCount"] == 0
    assert fresh_install["rtkToggles"] == []
    assert fresh_install["scriptErrors"] == []


def test_a_harness_whose_file_cannot_carry_the_record_says_so_on_its_card(
    rendered: dict,
) -> None:
    """Kilo CLI's validator rejects unknown top-level keys.

    So its generated config carries no ``_mcc_defaulted`` block, and a card
    built from what is on disk would otherwise report "0 models defaulted" --
    a measurement MCC never took. The card names the real reason and points at
    the launch summary, which reads the counts from the catalogue route rather
    than from the file.
    """

    cards = {card["id"]: card for card in rendered["codingAgents"]["cards"]}

    assert "rejects unknown keys" in cards["kilo"]["defaulted"]
    assert "launch summary on stderr" in cards["kilo"]["defaulted"]
    # The agent that *can* carry it still reports the count, not the excuse.
    assert cards["codex"]["defaulted"] == (
        "3 model(s) carry a value Codex CLI supplied because no provider published one"
    )
