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
import re
import shutil
import subprocess
from pathlib import Path
from typing import NoReturn

import pytest

from my_claude_code.core.upstream_ladder import _TIMES

HARNESS = Path(__file__).with_name("admin_jsdom_harness.mjs")
LOCK = Path(__file__).with_name(".admin_jsdom.lock")
STATIC_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
)

# Every test in this module reads one of two module-scoped harness runs. Under
# `--dist loadgroup` (the repo's addopts) an ungrouped module is spread over
# every worker, and each worker then builds its own copy of the fixture: four
# workers meant four concurrent jsdom runs, which starve each other of CPU,
# miss their own debounce windows and report the resulting half-rendered page
# as three hundred script errors. The group pins the whole file to one worker,
# so it costs exactly the two runs it looks like it costs.
pytestmark = pytest.mark.xdist_group("admin_static_jsdom")

# The bound is generous on purpose and settable, because it is the one number
# here that is about the machine rather than about the page: the harness takes
# about a minute on a GitHub runner and three to four on a loaded laptop, and
# the old hard-coded 180 s was under the local figure. `harnessWallMs` in the
# payload is what a tuned value should be argued from.
TIMEOUT_SECONDS = float(os.environ.get("MCC_JSDOM_TIMEOUT_SECONDS", "900"))

# CI must not quietly pass a suite it never ran. The jsdom job sets MCC_CI=1;
# there, a missing node or a missing jsdom is the job's own bug and fails.
ON_CI = os.environ.get("MCC_CI", "") not in ("", "0", "false")


def _missing(reason: str) -> NoReturn:
    if ON_CI:
        pytest.fail(
            f"MCC_CI=1 and {reason}. This suite is the only thing that runs "
            "admin.js; skipping it on CI is how a dashboard that lied to the "
            "server shipped. Install Node and `npm ci --prefix tests`."
        )
    pytest.skip(reason)


def _run(**env_extra) -> dict:
    node = shutil.which("node")
    if node is None:
        _missing("node is not on PATH")
    result = subprocess.run(
        [node, str(HARNESS), str(STATIC_DIR)],
        capture_output=True,
        text=True,
        # Explicit: the page is full of em dashes and middots, and on Windows
        # the default console codec turns every one of them into U+FFFD --
        # which would quietly make an "is this an em dash" assertion untestable.
        encoding="utf-8",
        timeout=TIMEOUT_SECONDS,
        env={**os.environ, **env_extra},
    )
    if result.returncode != 0:
        if "Cannot find package 'jsdom'" in result.stderr:
            _missing("jsdom is not installed")
        pytest.fail(f"harness failed: {result.stderr[-2000:]}")
    payload = json.loads(result.stdout)
    # Printed rather than asserted on: the number is evidence for the bound,
    # not a property of the page. `-s` or a failure shows it.
    print(f"jsdom harness wall time: {payload.get('harnessWallMs')} ms")
    return payload


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
    # Providers carries four static cards in index.html -- version, Deployment
    # (added with the desktop server-ownership modes), other servers on this
    # machine, and the token-optimizer card -- plus the settings sections this
    # fixture gives it fields for: `desktop`, and since 7.34.0 the `providers`
    # section itself, which now has the two OpenCode Zen fields that hold the
    # free-tier credential toggle. `runtime` still has no fields here.
    assert views["providers"]["sections"] == 6
    # 8 since 7.46.0: the Stream keepalive card sits between Deadlines and
    # Chain benching.
    assert views["limits"]["sections"] == 8
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
        # 6 since 7.34.0: the fixture now supplies the OpenCode Zen card, so
        # the `providers` settings section renders beside the static four.
        "providers": 6,
        "claude": 3,
        "requests": 1,
        "optimizer": 6,
        "web_search": 1,
        "limits": 8,
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
    "section-stream_keepalive",
    "section-benching",
    "section-provider_retries",
    "section-credential_health",
    # `loop_health` is claimed by the Limits view in admin.js and declared in
    # config/admin/manifest.py; the rail in index.html links to it. It is
    # listed here because a rail link with no card is exactly what
    # test_the_in_page_rail_links_to_a_section_that_exists is for.
    "section-loop_health",
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
    assert labels == ["Default", "Mythos", "Opus", "Sonnet", "Haiku", "Vision"]


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
        "Mythos",
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


def test_the_cooldown_mode_offers_exactly_three_answers(rendered) -> None:
    """Both new fields render on Credential health, and the select is the
    three modes and nothing else."""
    limits = rendered["limits"]
    # The leading blank is the page's "leave it unset" option, which every
    # select renders; the three answers follow it in manifest order.
    assert limits["cooldownModeOptions"] == ["", "provider", "fixed", "off"]
    assert limits["cooldownMaxRange"] == "Accepts 0 to 86400"


def test_the_credential_health_rule_follows_the_cooldown_mode(rendered) -> None:
    """The card's one sentence has to say what this install actually does."""
    limits = rendered["limits"]
    assert "the provider's Retry-After or the cooldown above" in limits["cooldownRule"]
    assert (
        "the cooldown above, whatever the provider published"
        in (limits["afterCooldownFixed"]["cooldownRule"])
    )
    off = limits["afterCooldownOff"]["cooldownRule"]
    assert "a 429 benches nothing at all" in off
    assert "still rotates to the next key" in off
    assert "Retry-After" not in off


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


def test_the_response_head_renders_on_the_try_that_had_one(rendered) -> None:
    """The measured bug's evidence, on the row it belongs to.

    Recorded only where the body could not be decoded, so exactly one of this
    ladder's three rows carries one and the other two render as they did
    before 7.20.0 -- which is what makes an old stored ladder safe.
    """

    detail = rendered["requestDetail"]["ladder"]
    assert detail["ladderHeadLabels"] == ["Response head"]
    head = detail["ladderHeads"][0]
    assert "status: 502" in head
    assert "content-encoding: gzip" in head
    assert "cf-ray: a3c9a1dc-ORD" in head
    assert "retry-after: 25517" in head
    assert "decode error: Error -3 while decompressing data" in head
    # Hex, not text: a head is read to decide what KIND of thing answered.
    assert "first bytes: 3c68746d6c3e" in head
    # ``0`` is a fact about the reply, not a missing term.
    assert "content-length: 0" in head


def test_a_try_with_no_recorded_head_renders_exactly_as_before(rendered) -> None:
    """Every row a release before 7.20.0 wrote has no head at all."""

    detail = rendered["requestDetail"]["ladder"]
    assert len(detail["ladderTries"]) == 3
    assert len(detail["ladderHeads"]) == 1


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


# ---------------------------------------------------------------------------
# Harness attribution: which client sent the request
# ---------------------------------------------------------------------------


def test_the_request_table_names_the_client_that_sent_each_row(rendered) -> None:
    """The column exists, and every row wears its harness as a labelled chip."""

    harness = rendered["harnessAttr"]

    assert harness["headers"] == [
        "Time",
        "Endpoint",
        "Harness",
        # 7.42.0: where it came from. Origin is the narrow-width chip that
        # stands in for Session and Folder; CSS shows one form or the other.
        "Session",
        "Folder",
        "Origin",
        "Provider",
        "Key",
        "Requested model",
        "Model",
        "Status",
        "Turn",
        "Tokens",
        "Cost",
        "TTFT",
        "Duration",
        "Details",
    ]
    # The display name, not the id: the ids are wire values, and the reader
    # never has to learn that `claude` means Claude Code.
    assert harness["chips"] == [
        {"text": "OpenCode", "harness": "opencode"},
        {"text": "Claude Code", "harness": "claude"},
        {"text": "Unknown", "harness": "unknown"},
    ]
    # Beside Endpoint, before the columns about what MCC did with the request.
    assert harness["harnessCellIndex"] == 2


def test_the_empty_request_table_spans_every_column_it_declares(rendered) -> None:
    """A hardcoded colspan is how the empty state drifts one column short."""

    harness = rendered["harnessAttr"]

    assert harness["emptyText"] == "No requests match the current filters."
    assert harness["emptyColSpan"] == len(harness["headers"])


def test_the_detail_modal_says_how_the_harness_was_identified(rendered) -> None:
    """An explicit header from our launcher is a fact; a user-agent is a guess.

    The modal has to keep them apart, because a client is free to send any
    user-agent it likes and none at all is also a valid answer.
    """

    harness = rendered["harnessAttr"]

    assert harness["detail_explicit"] == "OpenCode 1.18.26 (explicit header)"
    assert harness["detail_inferred"] == "Claude Code 2.0.14 (from user-agent)"
    assert harness["detail_unidentified"] == "Unknown (no client identification)"


def test_the_harness_filter_reloads_once_from_page_one(rendered) -> None:
    """Same contract as every other text filter: debounced, and back to page 1."""

    harness = rendered["harnessAttr"]

    assert "offset=25" in harness["pagedUrl"]
    assert harness["loadsWhileTyping"] == 0
    assert harness["loadsAfterTypingPause"] == 1
    assert "harness=opencode" in harness["typedStatsUrl"]
    assert "harness=opencode" in harness["typedListUrl"]
    assert "offset=0" in harness["typedListUrl"]


def test_clear_filters_empties_the_harness_box(rendered) -> None:
    harness = rendered["harnessAttr"]

    assert harness["clearedValue"] == ""
    assert "harness=" not in harness["clearUrl"]
    assert "harness" not in harness["persistedAfterClear"]


def test_the_harness_filter_round_trips_through_persisted_state(rendered) -> None:
    harness = rendered["harnessAttr"]

    assert harness["persisted"]["harness"] == "opencode"


def test_the_harness_filter_offers_the_harnesses_the_window_saw(rendered) -> None:
    """The datalist is built from the payload; the page carries no registry."""

    harness = rendered["harnessAttr"]

    assert harness["datalist"] == [
        ["claude", "Claude Code"],
        ["opencode", "OpenCode"],
        ["unknown", "Unknown"],
    ]


def test_requests_by_harness_names_every_harness_in_the_payload(rendered) -> None:
    """One row per `by_harness` entry, under its display name."""

    harness = rendered["harnessAttr"]

    assert harness["breakdownHeaders"] == [
        "Harness",
        "Requests",
        "Error rate",
        "Tokens in",
        "Tokens out",
        "Avg latency",
    ]
    assert [row[0] for row in harness["breakdown"]] == [
        "OpenCode",
        "Claude Code",
        "Unknown",
    ]
    assert harness["breakdown"][0][1] == "90,210"
    assert harness["breakdown"][2][2] == "0.2%"
    # avg_duration_ms is the one genuinely NULL-able column.
    assert harness["breakdown"][2][5] == "—"


def test_a_coding_agent_card_reports_its_own_seven_day_traffic(rendered) -> None:
    """An agent that sent nothing says 0, because that zero was measured.

    An em dash would claim the number is unknown, which is only true when the
    request log is off -- and then every card says so, not just the quiet ones.
    """

    cards = {card["id"]: card for card in rendered["codingAgents"]["cards"]}

    def requests_7d(card: dict) -> str:
        index = card["metaTerms"].index("Requests (7d)")
        return card["metaValues"][index]

    assert requests_7d(cards["opencode"]) == "12,480"
    assert requests_7d(cards["claude"]) == "3,120"
    # Present in the counts with a real zero, and absent from them entirely:
    # both are "no traffic", and both read as 0 rather than as unknown.
    assert requests_7d(cards["codex"]) == "0"
    assert requests_7d(cards["pi"]) == "0"
    # An agent MCC cannot launch can still have sent requests of its own, so
    # the early-return half of harnessMeta carries the row too.
    assert cards["antigravity"]["metaTerms"] == [
        "Protocol",
        "Requests (7d)",
        "Launcher",
    ]


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


def test_the_custom_card_offers_every_wire_api_the_build_speaks(rendered) -> None:
    """7.33.0: the card says which doors this host serves, and can change it."""

    card = rendered["customProviders"]

    assert card["surfaceLabels"] == [
        "Chat Completions (/chat/completions)",
        "Responses (/responses)",
        "Messages (/messages)",
    ]
    # Exactly what the entry declares, not everything and not the default.
    assert card["surfaceChecked"] == ["chat_completions", "responses"]


def test_the_wire_api_field_carries_its_own_help_text(rendered) -> None:
    """The 7.29.1 contract, kept by hand for a form outside the manifest."""

    help_text = rendered["customProviders"]["surfaceHelp"] or ""

    assert len(help_text) > 20
    assert "routes each model" in help_text


def test_a_created_provider_declares_chat_completions_by_default(rendered) -> None:
    """Nothing changes for an operator who never opens the control."""

    assert rendered["customProviders"]["createdSurfaces"] == ["chat_completions"]


def test_ticking_a_door_on_the_card_submits_it(rendered) -> None:
    assert rendered["customProviders"]["patchedSurfaces"] == [
        "chat_completions",
        "responses",
        "messages",
    ]


def test_the_models_page_draws_the_wire_surface_row(rendered) -> None:
    """The 6.74.0 row, which had no jsdom coverage until 7.33.0."""

    assert "wire surface" in rendered["models"]["surfaceMultiDoor"]["rows"]


def test_the_surface_override_offers_only_the_doors_the_host_declares(
    rendered,
) -> None:
    """A surface the host does not serve would fold back to unservable."""

    panel = rendered["models"]["surfaceMultiDoor"]

    assert panel["hasEditor"] is True
    assert panel["options"] == ["", "chat_completions", "responses"]
    assert panel["selected"] == ""


def test_a_single_surface_model_is_offered_no_choice_at_all(rendered) -> None:
    """39 of the 41 providers, and every custom entry that did not opt in."""

    panel = rendered["models"]["surfaceSingleDoor"]

    assert panel["hasEditor"] is False
    assert "wire surface" not in panel["rows"]


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


# ---------------------------------------------------------------------------
# The Claude subscription card
# ---------------------------------------------------------------------------


def test_subscription_card_reports_the_plan_and_both_expiries(
    rendered: dict,
) -> None:
    live = rendered["anthropicOAuthCard"]["live"]

    assert live["hidden"] is False
    # ``textContent`` runs each term straight into its value, so these read as
    # one word: the point is that the term and its value are both present and
    # adjacent, which is what the definition list renders.
    assert "Planmax" in live["text"]
    assert "Rate-limit tierdefault_claude_max_5x" in live["text"]
    assert "Refresh token expires" in live["text"]
    assert "5-hour window used1.0" in live["text"]
    # A reset arrives as unix seconds; the card shows the instant and keeps the
    # raw value beside it, so nothing is silently reinterpreted.
    assert "(1788393000)" in live["text"]


def test_subscription_card_repeats_anthropics_own_limit_message(
    rendered: dict,
) -> None:
    """Only ever repeated from a header Anthropic actually sent."""
    live = rendered["anthropicOAuthCard"]["live"]

    assert "You hit your 5-hour window" in live["text"]
    assert any("5-hour window" in value for value in live["expired"])


def test_subscription_card_says_not_yet_observed_before_any_response(
    rendered: dict,
) -> None:
    unobserved = rendered["anthropicOAuthCard"]["unobserved"]

    assert "not yet observed" in unobserved["text"]
    assert "5-hour window used" not in unobserved["text"]
    assert (
        "no Anthropic response has carried a rate-limit header yet"
        in (unobserved["text"])
    )
    # An expired access token and a credential without user:inference are both
    # flagged rather than merely printed.
    assert len(unobserved["expired"]) >= 2
    assert "cannot answer requests" in unobserved["text"]


def test_subscription_card_hides_itself_when_there_is_no_credential(
    rendered: dict,
) -> None:
    assert rendered["anthropicOAuthCard"]["absent"]["hidden"] is True
    assert rendered["anthropicOAuthCard"]["absent"]["text"] == ""


def test_the_subscription_card_renders_one_row_per_account(rendered: dict) -> None:
    """Two accounts, two rows, each named by its own account."""
    rows = rendered["anthropicOAuthCard"]["twoAccounts"]["rows"]

    assert [row["accountId"] for row in rows] == ["uuid-one", "uuid-two"]
    assert [row["name"] for row in rows] == [
        "first@example.test",
        "second@example.test",
    ]
    # Each row carries its own plan, not the primary's repeated twice.
    assert "Planmax" in rows[0]["text"]
    assert "Planpro" in rows[1]["text"]


def test_every_account_row_has_its_own_refresh_and_disconnect(
    rendered: dict,
) -> None:
    rows = rendered["anthropicOAuthCard"]["twoAccounts"]["rows"]

    assert [row["refresh"] for row in rows] == ["Refresh now", "Refresh now"]
    assert [row["disconnect"] for row in rows] == ["Disconnect", "Disconnect"]


def test_an_account_row_says_where_a_refresh_of_it_is_written(
    rendered: dict,
) -> None:
    """The imported row says write-back is on and names the file."""
    rows = rendered["anthropicOAuthCard"]["twoAccounts"]["rows"]

    assert "Added fromclaude-code" in rows[1]["text"]
    assert ".credentials.json" in rows[1]["text"]
    # And the account MCC signed in itself claims nothing about any file.
    assert "Write-back" not in rows[0]["text"]


def test_the_windows_are_reported_on_the_row_that_observed_them(
    rendered: dict,
) -> None:
    rows = rendered["anthropicOAuthCard"]["twoAccounts"]["rows"]

    assert "5-hour window used1.0" in rows[0]["text"]
    assert "5-hour window used" not in rows[1]["text"]
    assert "not yet observed" in rows[1]["text"]


def test_an_unnamed_account_row_falls_back_to_todays_masked_label(
    rendered: dict,
) -> None:
    """The same fallback every other credential row uses."""
    rows = rendered["anthropicOAuthCard"]["unobserved"]["rows"]

    assert [row["name"] for row in rows] == ["sk-a…z9"]


def test_sign_in_becomes_sign_in_another_account_once_one_is_stored(
    rendered: dict,
) -> None:
    labels = rendered["anthropicOAuthSignInLabel"]

    assert labels["empty"]["label"] == "Sign in with Anthropic"
    assert labels["stored"]["label"] == "Sign in another account"
    assert "1 account(s) stored." in labels["stored"]["status"]
    # And the card's own Refresh/Disconnect only come alive once there is
    # something of MCC's own to act on.
    assert labels["empty"]["managedEnabled"] is False
    assert labels["stored"]["managedEnabled"] is True


# --------------------------------------------------------------- agent tiers
#
# The Tiers section of each Coding agents card. jsdom proves what a gesture did
# to the DOM and which body went out on the wire; it cannot prove the rail's
# grid, because it has no box model -- that is checked in a real browser and
# recorded in the PR.


def test_the_tiers_block_renders_five_rows_per_agent_with_a_picker(
    rendered: dict,
) -> None:
    """Five, in picker order, for every agent that has a generated catalogue.

    And none for the three that have not: Claude Code self-discovers through
    /v1/models and speaks the claude-* names, so it has no tier to override.
    """
    tiers = rendered["codingAgents"]["tiers"]

    assert tiers["labels"] == ["Best", "Good", "Medium", "Cheap", "Vision"]
    assert tiers["refs"] == [
        "mcc/best",
        "mcc/good",
        "mcc/medium",
        "mcc/cheap",
        "mcc/vision",
    ]
    with_picker = {agent: rows for agent, rows in tiers["rowsPerAgent"].items() if rows}
    assert set(with_picker.values()) == {5}
    assert tiers["rowsPerAgent"]["claude"] == 0


def test_an_inherited_tier_says_which_global_route_it_follows(rendered: dict) -> None:
    """The collapse, named rather than hidden.

    A picker showing five entries that are the same model is only confusing if
    nothing says why, so the row names both the route and the setting that
    would move it.
    """
    tiers = rendered["codingAgents"]["tiers"]

    assert tiers["inheritedChip"] == "Same as global"
    assert tiers["inheritedReadout"] == (
        "Same as global Sonnet \u2014 currently nvidia_nim/one. "
        "MODEL_SONNET is unset, so it follows MODEL."
    )


def test_override_reveals_the_shared_rail_component(rendered: dict) -> None:
    """The Model Config rail, not a second one built for this page.

    A rail that reordered differently from the one beside it would read as a
    bug rather than as a distinction, so the grip, the chain editor and the
    Pause button all have to be the same ones.
    """
    tiers = rendered["codingAgents"]["tiers"]

    assert tiers["railsBeforeOverride"] == 0
    assert tiers["railsAfterOverride"] == 1
    assert tiers["hasGrip"] is True
    assert tiers["hasPauseButton"] is True
    assert tiers["chainRows"] == 1
    assert tiers["overrideChip"] == "This agent's own chain"
    assert tiers["actionLabels"] == ["Save", "Revert to global"]
    # Seeded from what the tier resolves to today: pressing Override must not
    # move the agent onto something the operator did not choose.
    assert tiers["primaryValue"] == "nvidia_nim/one"


def test_a_tier_rail_never_joins_the_settings_diff(rendered: dict) -> None:
    """These keys are not env vars.

    Collected by ``changedValues()`` they would be posted to
    /admin/api/config/apply as unknown keys on every Apply, and the page would
    read dirty from a section the dirty count cannot explain.
    """
    tiers = rendered["codingAgents"]["tiers"]

    assert not any(key.startswith("HARNESS_TIER") for key in tiers["changedValuesKeys"])


def test_a_pointer_drag_moves_a_fallback_within_a_harness_rail(rendered: dict) -> None:
    """Synthesised as MouseEvent: jsdom has neither PointerEvent nor DataTransfer.

    Dragging the fallback onto the primary swaps them, which is the promote
    gesture the Model Config rails already have -- and it has to reach the
    editor through ``state.routeRails``, which this section registers itself.
    """
    tiers = rendered["codingAgents"]["tiers"]

    assert tiers["dragBefore"] == ["nvidia_nim/one", "open_router/two"]
    assert tiers["dragAfter"] == ["open_router/two", "nvidia_nim/one"]


def test_revert_to_global_clears_the_entry(rendered: dict) -> None:
    """Deleted, not emptied: the two are different states of the store."""
    tiers = rendered["codingAgents"]["tiers"]

    assert tiers["railsAfterRevert"] == 0
    # "Default", because Best names MODEL and that is what the Model Config
    # page calls that card. The row reads in the operator's vocabulary, not the
    # tier's.
    assert tiers["readoutAfterRevert"].startswith("Same as global Default")
    assert tiers["posts"] == [
        {
            "harness": "codex",
            "tier": "best",
            "override": True,
            "model": "nvidia_nim/one",
            "fallbacks": ["open_router/two"],
            "paused": [],
        },
        {"harness": "codex", "tier": "best", "override": False},
    ]


def test_the_tiers_section_makes_no_console_errors(rendered: dict) -> None:
    assert rendered["consoleErrors"] == []


def test_every_routing_rail_heading_carries_its_harness_alias(rendered) -> None:
    """The id another coding agent has to ask for, beside the rail it reaches.

    Claude Code never names a model -- it asks for ``claude-sonnet-5`` and gets
    whatever Sonnet points at. Every other agent had to name a concrete
    ``provider/model`` ref, so the ``mcc/*`` aliases exist to close that
    gap; and until now the page where routes are edited did not say which alias
    named which rail, so a user who had just moved a model onto the Sonnet rail
    had no way to see that ``mcc/medium`` is what their Codex session must ask
    for.

    The map is joined onto the config payload from ``core/tier_refs.py``. There
    is deliberately no second list of aliases in ``admin.js``: the first
    thing a second copy would do is disagree with the first.
    """

    headings = rendered["routing"]["tierHeadings"]

    for expected in (
        "Default (mcc/best)",
        "Mythos (mcc/cyber)",
        "Opus (mcc/good)",
        "Sonnet (mcc/medium)",
        "Haiku (mcc/cheap)",
        "Vision adapter (mcc/vision)",
    ):
        assert expected in headings, headings

    # Fable gets none, and that is the point rather than an oversight:
    # ``MODEL_FABLE`` is a Claude alias, not a tier. ``mcc/best`` resolves
    # through ``MODEL`` -- the Default rail -- so printing it beside Fable
    # would tell the reader that editing that rail moves ``mcc/best``, which it
    # does not.
    assert "Fable" in headings
    assert not any(heading.startswith("Fable (") for heading in headings)

    # One chip per alias the payload carries, no more. Mythos (7.2.0) is one
    # of them: its rail is the newest and is rendered by exactly the same path.
    assert len(rendered["routing"]["aliasChips"]) == 6


# ------------------------------------------------- the vision adapter's mode


def test_the_adapter_mode_renders_inside_the_adapter_card(rendered) -> None:
    """A mode is unreadable away from the chain it applies to.

    Left unclaimed by the routing renderer it would land in the leftovers grid
    under the route cards -- a bare select saying "Vision Adapter Mode" with
    nothing around it to say which adapter or what the modes mean.
    """
    mode = rendered["visionMode"]
    assert mode["insideTheCard"] is True
    assert mode["inTheLeftovers"] is False
    assert mode["value"] == "route", "the default must load as today's behaviour"
    assert "describe" in mode["options"]


def test_the_mode_sits_under_the_rail_and_above_the_summary(rendered) -> None:
    order = rendered["visionMode"]["order"]
    assert order.index("route-vision-mode-wrap") > order.index("field")
    assert order.index("route-vision-mode-wrap") < order.index("route-vision-summary")


def test_the_hop_says_something_different_in_each_mode(rendered) -> None:
    """The arrow means two different things, so it may not read the same.

    In route mode the request goes to the vision model. In describe mode only
    the picture does, and it comes back as words -- the tier's own model still
    answers, which is the whole reason to choose the mode.
    """
    mode = rendered["visionMode"]
    assert "cannot read them" in mode["hopSentenceRoute"]
    assert "still answers" not in mode["hopSentenceRoute"]
    assert "come back as words" in mode["hopSentenceDescribe"]
    assert "still answers" in mode["hopSentenceDescribe"]
    # An unset adapter still says the honest thing in either mode.
    assert "will fail here" in mode["hopSentenceUnset"]


def test_switching_the_mode_rewrites_the_summary_without_a_reload(rendered) -> None:
    mode = rendered["visionMode"]
    assert mode["summaryInRoute"]
    assert mode["summaryInDescribe"]


# --------------------------------------- the description beside the thumbnail


def test_a_described_image_shows_the_words_the_model_was_given(rendered) -> None:
    """Q15: without this there is no way to judge whether describe mode pays."""
    fresh = rendered["describedImages"]["fresh"]
    assert "Described by another model" in fresh["delivery"]
    assert fresh["bodies"] == ["A terminal showing ModuleNotFoundError."]
    assert fresh["summaries"] == ["Described by groq/eyes · fresh"]


def test_a_cached_description_says_so(rendered) -> None:
    """No describe attempt on this request means nobody paid for these words."""
    assert rendered["describedImages"]["cached"]["summaries"] == [
        "Described by groq/eyes · cached"
    ]


def test_an_image_that_travelled_as_an_image_gets_no_description_block(
    rendered,
) -> None:
    plain = rendered["describedImages"]["plain"]
    assert plain["delivery"] == "Sent to the model as an image."
    assert plain["summaries"] == []


# --------------------------------------------------------------- learned facts


def test_the_models_page_draws_a_chip_per_learned_fact(rendered: dict) -> None:
    """Persisting an invisible fact would make an invisible fact permanent."""

    models = rendered["models"]
    assert models["learnedChips"] >= 2
    # Stale is a state the row has to show, not a reason to hide it: the
    # operator needs to see what MCC used to believe and why it stopped.
    assert models["learnedStaleChips"] >= 1


def test_a_probe_that_contradicts_the_catalogue_is_marked_without_a_click(
    rendered: dict,
) -> None:
    """The disagreement is the finding; burying it in a panel would waste it."""

    assert rendered["models"]["learnedDisagreeChips"] >= 1


def test_the_models_page_has_a_learned_facet(rendered: dict) -> None:
    assert rendered["models"]["learnedFacet"], (
        '"show me every model MCC has learned something about" must be one click'
    )


def test_the_models_page_says_when_the_catalogue_was_last_refreshed(
    rendered: dict,
) -> None:
    readout = rendered["models"]["refreshReadout"]
    assert "last refreshed" in readout
    assert "next in" in readout


def test_a_swept_catalogue_still_reads_as_a_sweep(rendered: dict) -> None:
    readout = rendered["catalogueReadout"]["swept"]
    assert "last refreshed" in readout
    assert "next in" in readout
    assert "as of" not in readout


def test_a_catalogue_read_from_disk_does_not_claim_a_sweep(rendered: dict) -> None:
    """ "as of 40 min ago", not "last refreshed 40 min ago".

    The age is real -- it is when the stored catalogue was written -- but no
    sweep has happened in this process yet, and a page that says otherwise is
    claiming a network call it never made.
    """
    readout = rendered["catalogueReadout"]["stored"]
    assert "as of" in readout
    assert "last refreshed" not in readout
    assert "refreshing now" in readout
    # A sweep in flight is the next thing that happens; a countdown to the one
    # after it is not what the reader is waiting for.
    assert "next in" not in readout


def test_the_readout_still_names_the_setting_when_the_sweep_is_off(
    rendered: dict,
) -> None:
    assert "MODEL_DISCOVERY_REFRESH_SECONDS=0" in rendered["catalogueReadout"]["off"]


# ---------------------------------------------------------------- theme picker


def test_every_theme_option_sits_inside_the_picker(rendered: dict) -> None:
    """The fourth theme used to render outside the segmented pill.

    jsdom cannot see the overflow itself -- it has no box model -- but it can
    see the structural invariant the CSS fix has to preserve: every option is a
    child of the control, so a future fifth theme is a layout question and
    never an orphaned button.
    """

    picker = rendered["themePicker"]
    assert picker["present"]
    assert picker["optionCount"] >= 4
    assert picker["allInsidePill"], (
        "a theme option rendered outside #themeSwitch: " + ", ".join(picker["labels"])
    )
    assert len(picker["checked"]) == 1


# --------------------------------------------------------------- desktop apps
#
# The cards MCC draws for applications it does not launch. What is worth
# asserting here is not that the fetch happened -- the API tests cover that --
# but that the server's six-state answer became six visibly different cards,
# that a card with no button offers none, and that the undo picker offers both
# modes and disables the one that would have nothing to restore.


def _desktop_card(rendered: dict, app_id: str) -> dict:
    cards = rendered["desktopApps"]["cards"]
    return next(card for card in cards if card["id"] == app_id)


def test_the_desktop_apps_group_renders_one_card_per_app(rendered: dict) -> None:
    apps = rendered["desktopApps"]

    assert apps["present"] is True
    assert apps["cardCount"] == 10
    assert [card["id"] for card in apps["cards"]] == [
        "codex_desktop",
        "opencode_desktop",
        "goose_desktop",
        "crush_desktop",
        "claude_desktop",
        "roo_code",
        "goose_unresolved",
        "codex_removed",
        "antigravity",
        "warp",
    ]


def test_each_probe_state_reaches_the_badge_in_its_own_words(rendered: dict) -> None:
    """Nine states, nine labels. Collapsing any two hides a real difference."""

    badges = {card["id"]: card["badge"] for card in rendered["desktopApps"]["cards"]}
    assert badges["codex_desktop"] == "Configured by MCC"
    assert badges["opencode_desktop"] == "Configured but drifted"
    assert badges["goose_desktop"] == "Installed, not configured"
    assert badges["crush_desktop"] == "Not installed"
    assert badges["warp"] == "Not routable"
    # New in 6.56.0: a source outranks the file MCC would write.
    assert badges["roo_code"] == "Managed by your organisation"
    assert badges["claude_desktop"] == "Configured by MCC"
    # New in 6.84.0. Both of these used to be reported as something else:
    # the first as "Configured by MCC" while the app got a 401, the second as
    # "Installed, not configured" whoever had removed the keys.
    assert badges["goose_unresolved"] == "Written, but the key cannot resolve"
    assert badges["codex_removed"] == "The app removed MCC's configuration"
    assert len(set(badges.values())) == 8


def test_a_credential_that_cannot_resolve_is_not_a_configured_card(
    rendered: dict,
) -> None:
    """The badge says it, and the card names the variable to export."""

    card = _desktop_card(rendered, "goose_unresolved")

    assert card["badgeState"] == "credential_unresolved"
    assert card["drifted"] is True
    assert "MCC_AUTH_TOKEN" in card["driftNote"]
    assert "does not set variables for you" in card["driftNote"]
    # It is still configurable: the file is right, so Re-apply is offered.
    assert card["hasConfigure"] is True
    assert card["hasUndo"] is True


def test_keys_the_application_removed_are_not_reported_as_never_configured(
    rendered: dict,
) -> None:
    """The third history, which used to be indistinguishable from the first."""

    card = _desktop_card(rendered, "codex_removed")

    assert card["badgeState"] == "removed_by_app"
    assert "without anyone pressing Undo" in card["driftNote"]
    assert card["hasConfigure"] is True


def test_an_instructions_only_card_says_why_it_has_no_button(
    rendered: dict,
) -> None:
    """A button that disappears between releases has to explain itself."""

    card = _desktop_card(rendered, "antigravity")

    assert card["hasConfigure"] is False
    assert card["hasUndo"] is False
    assert card["unavailableReason"].startswith("2026-09-12: ")
    assert "GEMINI_API_KEY" in card["unavailableReason"]
    assert "GEMINI_API_KEY" in card["instructionLabels"]
    assert "GOOGLE_GEMINI_BASE_URL" in card["instructionLabels"]


def test_a_drifted_card_says_what_drift_means_and_offers_a_re_apply(
    rendered: dict,
) -> None:
    card = _desktop_card(rendered, "opencode_desktop")

    assert card["drifted"] is True
    assert "changed since MCC wrote it" in card["driftNote"]
    assert card["configureLabel"] == "Re-apply"


def test_a_configured_card_offers_both_undo_modes(rendered: dict) -> None:
    """Both, because both are right answers to different questions."""

    card = _desktop_card(rendered, "codex_desktop")

    assert card["hasUndo"] is True
    assert [mode["value"] for mode in card["undoModes"]] == ["keys_only", "restore"]
    assert (
        card["undoModes"][0]["label"]
        == "Remove MCC's keys (and put back what it replaced)"
    )
    assert card["undoModes"][1]["label"] == "Restore the original values exactly"
    assert card["undoModes"][1]["disabled"] is False


def test_restore_is_disabled_where_mcc_replaced_nothing(rendered: dict) -> None:
    """Offering it would promise a value that was never recorded."""

    card = _desktop_card(rendered, "opencode_desktop")
    restore = next(mode for mode in card["undoModes"] if mode["value"] == "restore")

    assert restore["disabled"] is True
    assert "replaced nothing" in restore["label"]


def test_an_unconfigured_card_offers_configure_but_no_undo(rendered: dict) -> None:
    card = _desktop_card(rendered, "goose_desktop")

    assert card["hasConfigure"] is True
    assert card["hasUndo"] is False
    assert card["undoModes"] == []


def test_a_not_installed_card_says_so_and_offers_no_button(rendered: dict) -> None:
    """The server refuses the write, so a button could only produce an error.

    It used to be offered, and it used to *succeed*: MCC wrote a provider into
    a file no program on the machine reads and the card went green. Crush is
    the row that proved it -- its marker was ``%LOCALAPPDATA%\\crush``, a
    directory MCC's own ``mcc-crush.exe`` launcher had created.
    """

    card = _desktop_card(rendered, "crush_desktop")

    assert card["badge"] == "Not installed"
    assert card["hasConfigure"] is False
    assert card["hasUndo"] is False
    assert "cannot find" in card["unavailableReason"]
    assert "the program itself" in card["unavailableReason"]


def test_a_not_routable_card_states_the_reason_and_offers_no_buttons(
    rendered: dict,
) -> None:
    """A dated explanation, and no button that would imply there might be one."""

    card = _desktop_card(rendered, "warp")

    assert card["unavailable"] is True
    assert "2026-09-07" in card["unavailableReason"]
    assert card["hasConfigure"] is False
    assert card["hasPreview"] is False
    assert card["hasUndo"] is False


def test_the_claude_desktop_card_is_a_button_and_names_what_it_writes(
    rendered: dict,
) -> None:
    """The 6.55.0 card was a "Not installed" badge over six copy buttons.

    Anthropic's MDM page documents the local configuration library, so this is
    a Configure button like every other servable card -- and the card states
    the file, the one foreign key it merges, the settings its own document
    carries, and what would outrank it.
    """

    card = _desktop_card(rendered, "claude_desktop")

    assert card["hasConfigure"] is True
    assert card["badge"] == "Configured by MCC"
    meta = dict(zip(card["metaTerms"], card["metaValues"], strict=False))
    assert "configLibrary/_meta.json" in meta["Config file"]
    assert meta["Replaces"] == "appliedId"
    assert "inferenceGatewayBaseUrl" in meta["It writes"]
    assert "inferenceGatewayApiKey" in meta["It writes"]
    assert r"Policies\Claude" in meta["Outranked by"]
    # The instruction table is the fallback, and this file exists.
    assert card["instructionLabels"] == []
    # And nothing anywhere still claims Anthropic documents no file.
    assert not any("does not guess" in note for note in card["notes"])


def test_a_managed_card_has_no_button_and_names_what_is_enforcing_it(
    rendered: dict,
) -> None:
    """A Configure that wrote a file the app ignores would report success."""

    card = _desktop_card(rendered, "roo_code")

    assert card["hasConfigure"] is False
    assert card["hasPreview"] is False
    assert "Machine policy" in card["unavailableReason"]
    assert "inferenceProvider" in card["unavailableReason"]


def test_a_card_names_the_file_the_owned_key_and_what_it_replaces(
    rendered: dict,
) -> None:
    card = _desktop_card(rendered, "codex_desktop")

    assert "Config file" in card["metaTerms"]
    assert "MCC owns" in card["metaTerms"]
    assert "Replaces" in card["metaTerms"]
    assert "~/.codex/config.toml" in card["metaValues"]
    assert "model_providers.mcc" in card["metaValues"]
    assert "model_provider, model" in card["metaValues"]


def test_a_card_reports_whether_the_token_variable_is_exported(rendered: dict) -> None:
    """MCC never sets it, so the card has to be able to say it is missing."""

    exported = _desktop_card(rendered, "codex_desktop")
    missing = _desktop_card(rendered, "opencode_desktop")

    assert any("exported where the server" in v for v in exported["metaValues"])
    assert any("not exported yet" in v for v in missing["metaValues"])


def test_no_card_offers_the_default_model_checkbox(rendered: dict) -> None:
    """Removed in 7.6.6, and the assertion is inverted rather than deleted.

    It rendered on every servable card that was not Codex -- and Codex is the
    only spec whose document declares a ``model`` key, which it writes
    unconditionally. So the box appeared on six rows and could not change a
    byte on any of them.
    """

    cards = rendered["desktopApps"]["cards"]
    assert cards, "no desktop cards rendered at all"
    assert [card["id"] for card in cards if card["hasDefaultModelCheckbox"]] == []


def test_the_preview_shows_the_real_diff_and_no_credential(rendered: dict) -> None:
    preview = rendered["desktopApps"]["preview"]

    assert preview["hidden"] is False
    assert "[model_providers.mcc]" in preview["text"]
    assert "Replaces existing values at: model_provider, model" in preview["text"]
    assert "-> Restart Codex desktop to pick this up." in preview["text"]
    # The server masks before sending; nothing token-shaped reaches the page.
    assert "sk-" not in preview["text"]


def test_the_group_states_the_ownership_promise_and_the_two_undo_modes(
    rendered: dict,
) -> None:
    note = rendered["desktopApps"]["ownershipNote"]

    assert "touches only its own keys" in note
    assert ".mcc-backup" in note
    assert "Restore the original values" in note


# ------------------------------------------------------------- guide links
# Every dashboard surface that the Guide explains now carries a small link
# into the section that explains it. Two failures are invisible without a
# check: an anchor that names a heading somebody renamed, and a click that
# switches the view but never scrolls.


def test_the_guide_links_render_and_every_one_of_them_resolves(rendered) -> None:
    """The anchor table itself is checked in ``test_admin_asset_wiring``.

    What this adds is the DOM: that the links are actually appended by the
    code that draws each surface, and that each rendered link's anchor is a
    real element on the page rather than a heading somebody renamed.
    """

    rendered_links = rendered["guideLinks"]["rendered"]

    assert rendered_links, "no Guide links rendered anywhere on the page"
    dead = [entry["anchor"] for entry in rendered_links if not entry["resolves"]]
    assert not dead, f"rendered Guide links point at missing anchors: {dead}"

    purposes = {entry["purpose"] for entry in rendered_links}
    # The surfaces this fixture's payload actually draws. The billed/est chip
    # needs a provider with a measured image estimate, which this payload does
    # not supply, so it is covered by the source-level guard instead.
    assert {"cli", "desktop_apps", "agent_tiers", "images", "cost"} <= purposes


def test_clicking_a_guide_link_opens_the_guide_and_scrolls_to_the_section(
    rendered,
) -> None:
    clicked = rendered["guideLinks"]["clicked"]

    assert clicked is not None
    assert clicked["guideViewVisible"] is True
    assert clicked["navActive"] is True
    assert clicked["anchor"] in clicked["scrolledAnchors"]


class TestDesktopAppUpdateBanner:
    """The version panel now answers for the window as well as for the wheel.

    Until 6.60.0 the desktop app's pin was enforced by a process that need not
    be running, so the dashboard could tell a user they were up to date while
    the window they were reading it in was fifteen releases old (BUG-0).
    """

    def test_a_stale_desktop_app_gets_its_own_banner(self, rendered) -> None:
        banner = rendered["desktopAppBanner"]["stale"]["banners"]

        assert "Desktop app v6.60.0 available" in banner
        # The sentence has to say when it takes effect, because the answer is
        # "not now" and a banner that does not say so reads as a failure.
        assert "the next time you restart the app" in banner
        assert "v6.43.0" in banner, "it names what is actually running"

    def test_the_panel_names_both_ends_of_the_move(self, rendered) -> None:
        panel = rendered["desktopAppBanner"]["stale"]["panel"]

        assert "Desktop app" in panel
        assert "v6.43.0" in panel and "v6.60.0" in panel
        assert "on next restart" in panel

    def test_a_current_desktop_app_says_nothing(self, rendered) -> None:
        state = rendered["desktopAppBanner"]["current"]

        assert "Desktop app v" not in state["banners"]
        assert "available" not in state["banners"]
        # But the panel still reports what is installed, so a person can check.
        assert "v6.60.0" in state["panel"]


def test_analytics_paints_while_the_cost_breakdown_is_still_loading(rendered) -> None:
    """The slowest panel on the page must not hold the other three hostage.

    Measured on a 4.5 GB log: the cost breakdown takes 9.0 s, the stats panel
    0.11 s, the request list 0.15 s and the lifetime totals 0.004 s. Inside one
    ``Promise.all`` that made opening Analytics a nine-second wait for numbers
    that were ready in a tenth of a second. Here the cost route is held open,
    and the page must already be drawn when it is.
    """

    panel = rendered["costPanel"]

    assert panel["statCardsWhileCostPending"] > 0
    assert panel["noteWhileCostPending"] == "Working out what this traffic cost..."
    # Started with the others, not after them: the wait moved, not the request.
    assert panel["costRequested"] >= 1
    assert panel["noteAfterCostLands"] != panel["noteWhileCostPending"]


# The pager's own punctuation, spelled by code point so the source stays ASCII.
PAGER_OF_7 = f"1{chr(0x2013)}7 of 7"
PAGER_COUNTING = f"1{chr(0x2013)}3 of counting{chr(0x2026)}"


def test_a_search_paint_never_overwrites_deferred_answers_that_landed_first(
    rendered,
) -> None:
    """The placeholder paint is older than the deferred stats and count.

    A free-text search fires its real stats and its real count before the
    paint's own ``Promise.all`` and paints from an empty placeholder. When the
    two deferred answers came back first, that paint redrew every breakdown as
    "No ... activity in this range", every card as "counting…" and the pager
    back to "counting…", and nothing repainted them until the next reload.
    """

    race = rendered["deferredRace"]
    landed = race["deferredFirst_beforePaint"]
    after = race["deferredFirst_afterPaint"]

    # The deferred answers did land first: real rows, real cards, real count.
    assert landed["providerRows"][0].startswith("nous_portal")
    assert landed["countingCards"] == 0
    assert landed["pager"] == PAGER_OF_7

    # And the paint that followed kept them.
    assert after["providerRows"] == landed["providerRows"]
    assert after["harnessRows"] == landed["harnessRows"]
    assert after["keyRows"] == landed["keyRows"]
    assert after["countingCards"] == 0
    assert after["cards"] == landed["cards"]
    assert after["pager"] == PAGER_OF_7
    assert after["tableRows"] == 3


def test_a_search_paint_still_says_counting_until_its_deferred_answers_land(
    rendered,
) -> None:
    """The ordinary order is unchanged: placeholder first, then the numbers."""

    race = rendered["deferredRace"]
    waiting = race["paintFirst_beforeDeferred"]
    landed = race["paintFirst_afterDeferred"]

    assert waiting["providerRows"] == ["No provider activity in this range."]
    assert waiting["countingCards"] == waiting["cards"] > 0
    assert waiting["pager"] == PAGER_COUNTING

    assert landed["providerRows"] == race["deferredFirst_beforePaint"]["providerRows"]
    assert landed["countingCards"] == 0
    assert landed["pager"] == PAGER_OF_7


def test_the_lifetime_panel_says_what_the_log_costs(rendered) -> None:
    """No capping was the decision; showing the size is what replaced it.

    A four-gigabyte log reached that size unremarked because nothing on the
    page ever mentioned it. This is that number, in the panel that is already
    about the log as a whole.
    """

    span = rendered["logReadout"]["lifetimeSpan"]

    assert "333,838 rows kept" in span
    assert "4.19 GB on disk" in span
    # And it did not replace what was there.
    assert "2026-08-01 to 2026-09-12" in span


def test_the_cost_card_ships_the_denominator_behind_its_totals(rendered) -> None:
    """A window where most models are unpriced must not read as a cheap week."""

    note = rendered["logReadout"]["costNote"]

    assert "Priced: 179,897 of 333,838 requests (53.9%)" in note
    # The provenance line it has always carried is still there.
    assert "Priced by" in note


# ---------------------------------------------------------------------------
# Per-model latency: the winner's number, and the time the chain lost
# ---------------------------------------------------------------------------


def test_jsdom_request_row_shows_winner_ttft_and_fallback_loss(rendered) -> None:
    """The row must report the answering model, not its predecessors' stalls.

    The fixture is the regression in one line: the winner's first token came
    in 300 ms and the client waited 4,712 ms because two models ahead of it
    did not answer. The row used to print 4,712 ms against the model that
    rescued the request.
    """

    fallback, frame, legacy, unmeasured = rendered["latencyViews"]["rowCells"]

    assert fallback["text"] == "300 ms +4.4 s"
    assert fallback["lost"] == [" +4.4 s"]
    assert "took 300 ms" in fallback["title"]
    assert "4712 ms" in fallback["title"]
    # A millisecond apart is the ordinary single-model request: the winner's
    # own clock starts one frame after the request's, and a `+0.0 s` on every
    # row would make the suffix meaningless where it matters.
    assert frame["text"] == "305 ms"
    assert frame["lost"] == []
    # Written before 7.4.0: no winner time exists, so the row is exactly what
    # it has always been, and the title says which number it is.
    assert legacy["text"] == "410 ms"
    assert "predates per-attempt measurement" in legacy["title"]
    assert unmeasured["text"] == "\u2014"


def test_jsdom_output_rate_uses_the_winner_ttft(rendered) -> None:
    """Both halves of the rate must come from the same model.

    The winner produced 340 tokens over (2,100 - 300) ms: 188.9 tok/s. The old
    form -- the request's 6,812 ms minus the request's 4,712 ms TTFT -- read
    161.9 because the predecessors' stall had been taken out of the
    denominator; putting the winner's TTFT into the request's duration would
    read 52.2 for the same reason in reverse. Neither is a rate any model
    achieved.
    """

    pairs = dict(rendered["latencyViews"]["detailPairs"])

    assert pairs["Output rate"] == "188.9 tok/s"
    assert pairs["Duration"] == "6812 ms"
    assert pairs["TTFT (winner)"] == "300 ms"
    assert pairs["TTFT (incl. fallbacks)"] == "4712 ms"
    assert pairs["Lost to fallbacks"].startswith("4412 ms")


def test_jsdom_request_chain_renders_per_attempt_latency(rendered) -> None:
    """Each attempt's own clock, on the attempt's own row."""

    views = rendered["latencyViews"]
    assert views["chainHidden"] is False
    first, second, winner, _skipped = views["attemptMetrics"]

    # The model that timed out never produced a first token, and a dash is the
    # only honest thing to print for one.
    assert dict(first)["TTFT"] == "\u2014"
    assert dict(first)["ended"] == "upstream_timeout"
    assert dict(second)["TTFT"] == "1.4s"
    assert dict(winner) == {
        "TTFT": "300 ms",
        "first reasoning": "460 ms",
        "generating": "1.8s",
        "tokens out": "340",
        "rate": "188.9 tok/s",
        "ended": "answered",
    }
    # A failed attempt produced no answer to count, so its rate is refused
    # rather than computed from the request's tokens -- those belong to
    # whichever model answered.
    assert dict(second)["rate"] == "\u2014"
    assert dict(second)["tokens out"] == "\u2014"


def test_jsdom_skipped_attempt_renders_dashes_and_its_bench_reason(rendered) -> None:
    """A model that was never asked has no latency, and says why it was not."""

    views = rendered["latencyViews"]
    skipped = dict(views["attemptMetrics"][3])

    assert [skipped[name] for name in ("TTFT", "first reasoning", "generating")] == [
        "\u2014",
        "\u2014",
        "\u2014",
    ]
    assert skipped["ended"] == "benched (upstream_status)"
    # The sentence underneath survives: the row scans, the reason still reads.
    assert views["benchReasons"]
    assert "12 counted failures" in views["benchReasons"][0]
    assert views["chainModels"][3] == "commandcode/benched-four"


def test_jsdom_model_latency_breakdown_keeps_failures_apart(rendered) -> None:
    """One model, two outcomes, two rows: the separation is the feature."""

    panel = rendered["latencyViews"]["panel"]

    assert panel["headers"] == [
        "Model",
        "Outcome",
        "Attempts",
        "Share",
        "TTFT measured",
        "p50 TTFT",
        "Avg first reasoning",
        "Avg generating",
        "Tokens out",
        "tok/s",
    ]
    answered = next(row for row in panel["rows"] if row[1] == "answered")
    failed = next(
        row
        for row in panel["rows"]
        if row[0] == "alpha/model-02" and row[1] == "failed"
    )
    assert answered[5] == "312 ms"
    assert failed[5] == "9.0s"
    # p50 leads, p95 is in the title: one 120 s stall moves a mean by seconds.
    assert "p95 1.4s" in panel["p50Titles"][1]
    # The denominator travels with the number, always.
    assert answered[4] == "12"
    assert "12 of 12 attempts carry a measurement" in panel["p50Titles"][1]
    # Sums over a group only part of which was measured are not a rate.
    assert failed[9] == "\u2014"
    assert answered[9] == "212.5 tok/s"


def test_jsdom_unmeasured_latency_renders_dashes_not_zeroes(rendered) -> None:
    """Every attempt written before 7.4.0 is unmeasured and unbackfillable."""

    views = rendered["latencyViews"]
    unmeasured = next(
        row for row in views["panel"]["rows"] if row[0] == "beta/model-07"
    )

    assert unmeasured[2] == "4,416"
    assert unmeasured[4] == "0"
    assert unmeasured[5:] == ["\u2014"] * 5
    # And the panel says so in words rather than leaving an empty column.
    note = views["notMeasured"]["note"]
    assert note.startswith("Nothing measured yet")
    assert "cannot be given one" in note
    # The panel also names the question it answered, which is not the one the
    # filters above it ask.
    assert "Not narrowed by the filters above." in views["panel"]["note"]
    assert "close rather than exact" in views["panel"]["note"]


def test_jsdom_models_page_latency_chip_is_absent_when_unmeasured(rendered) -> None:
    """A chip for a model with 4,416 unmeasured attempts would be a fiction."""

    chips = rendered["latencyViews"]["modelChips"]

    assert len(chips) == 1
    assert chips[0]["text"] == (
        "last 7d: p50 TTFT 312 ms, p95 1.4s over 14 of 15 attempts"
    )
    assert "failed ones included" in chips[0]["title"]
    # Both outcomes in the tooltip: the headline is the answering group, and
    # the chip never pretends the failures did not happen.
    assert "failed: 3 attempts" in chips[0]["title"]
    # Fetched beside the Models page rather than folded into its payload.
    assert rendered["latencyViews"]["latencyFetches"] >= 1


def test_jsdom_export_attempts_button_opens_the_attempt_scope(rendered) -> None:
    """The whole feature is one line of wiring, and it fails silently.

    A button that opened the modal on the request scope would look exactly
    like one that worked, so this drives the real click and reads back the
    scope, the field list that scope renders, and the Group by control that
    the attempt scope does not offer.
    """

    window = rendered["exportWindow"]

    assert window["scopes"] == ["requests", "attempts", "websearch"]

    attempts = window["attempts"]
    assert attempts["scope"] == "attempts"
    assert attempts["fields"] == [
        "Failure and skip reason",
        "Attempt tokens",
        "Attempt cost",
        "Upstream retry ladder",
        "Wire surface and credential",
        "Stream recovery counters",
    ]
    assert attempts["checkedFields"] == ["failure", "tokens", "cost", "ladder"]
    # Detail-only: the control is gone rather than offering a shape the server
    # answers with a 400.
    assert attempts["groupByHidden"] is True


def test_jsdom_the_request_export_button_still_opens_the_request_scope(
    rendered,
) -> None:
    """Opened after the attempt scope, so it also proves the modal resets."""

    requests = rendered["exportWindow"]["requests"]

    assert requests["scope"] == "requests"
    assert requests["groupByHidden"] is False
    assert "Upstream retry ladder" in requests["fields"]
    assert "Attempt tokens" not in requests["fields"]


# --------------------------------------------------------------- proxying
# The Proxying page is backed by a JSON store rather than settings keys, so
# none of the settings-page machinery covers it. These drive the three
# gestures that change a chain without saving, and the one that saves.


def test_jsdom_the_proxying_page_renders_one_card_per_configured_provider(
    rendered,
) -> None:
    proxying = rendered["proxying"]

    assert proxying["present"] is True
    assert proxying["cards"] == 2


def test_jsdom_the_honest_paragraph_is_on_the_page_once(rendered) -> None:
    """Said once, above the cards -- not repeated per card or in a modal."""

    honesty = rendered["proxying"]["honesty"]

    assert "strict certificate verification" in honesty
    assert "it cannot read or change your API keys" in honesty
    # The metadata half, which is the part that is easy to leave out.
    assert "which provider you are contacting" in honesty
    assert "the choice is yours" in honesty


def test_jsdom_the_policy_help_says_what_failover_actually_costs(rendered) -> None:
    """The practical difference is the whole point of the feature here.

    Four proxies under `failover` still use one address until it fails; only
    `round_robin` multiplies a per-address allowance. A page that shows four
    policy names and not that sentence has not explained the feature.
    """

    assert "until it fails" in rendered["proxying"]["policyHelp"]


def test_jsdom_the_chain_says_it_replaces_the_env_var_without_rewriting_it(
    rendered,
) -> None:
    inherited = rendered["proxying"]["inherited"]

    assert "NVIDIA_NIM_PROXY" in inherited
    assert "never rewritten" in inherited


def test_jsdom_exactly_two_chips_are_refused_and_three_are_on(rendered) -> None:
    chips = rendered["proxying"]["chips"]

    assert [chip["id"] for chip in chips if chip["disabled"]] == [
        "authentication",
        "permission",
    ]
    assert [chip["id"] for chip in chips if chip["on"]] == [
        "quota",
        "rate_limit",
        "timeout",
    ]
    assert len(chips) == 11


def test_jsdom_a_refused_chip_does_not_toggle_when_it_is_clicked(rendered) -> None:
    """Inert, not merely styled as inert.

    A chip that looks refused and arms anyway is the worst of both: the page
    says the choice is unavailable and the store gets it.
    """

    assert (
        rendered["proxying"]["chipsAfterRefusedClick"] == rendered["proxying"]["chips"]
    )


def test_jsdom_move_down_reorders_the_chain_and_announces_it(rendered) -> None:
    """The keyboard and button path to the reorder, which jsdom can drive.

    The pointer drag over the same list cannot be driven here -- jsdom has no
    layout, so `elementFromPoint` has nothing to answer with -- and it is
    exercised in a real browser instead.
    """

    proxying = rendered["proxying"]

    assert proxying["order"] == [
        "203.0.113.7:1080",
        "198.51.100.9:8080",
        "192.0.2.44:3128",
        "Direct (no proxy)",
    ]
    assert proxying["orderAfterMoveDown"] == [
        "198.51.100.9:8080",
        "203.0.113.7:1080",
        "192.0.2.44:3128",
        "Direct (no proxy)",
    ]
    assert "is now entry 2 of 4" in proxying["announcementAfterMove"]
    assert "Press Save to keep it" in proxying["announcementAfterMove"]


def test_jsdom_the_recommended_set_restores_the_three_defaults(rendered) -> None:
    assert [
        chip["id"] for chip in rendered["proxying"]["chipsAfterReset"] if chip["on"]
    ] == [
        "quota",
        "rate_limit",
        "timeout",
    ]


def test_jsdom_a_paused_entry_stays_in_the_chain(rendered) -> None:
    """Pause is not delete: the entry keeps its place and its position number.

    The same semantics a paused route entry has, which is why the operator
    does not lose an order they spent time on to debug one address.
    """

    proxying = rendered["proxying"]

    assert proxying["pausedRows"] == 1
    entries = proxying["saved"]["body"]["entries"]
    assert len(entries) == 4
    assert [entry["paused"] for entry in entries] == [True, False, False, False]


def test_jsdom_each_entry_shows_the_health_the_pools_measured(rendered) -> None:
    """Live per-entry health, and "not checked yet" only where it is true.

    The release that shipped this page had no runtime and said "not checked
    yet" on every row, which was honest then and would be a lie now that a
    request goes through these addresses. An address nothing has used still
    says it: no measurement is a different fact from a bad one, and colouring
    it would claim one that was never taken.
    """

    states = rendered["proxying"]["entryStates"]

    # "unreachable 5m" up to 7.18, which read as a countdown to being usable
    # again. It was not one even then, and since 7.19.0 it is emphatically not:
    # the window running out makes an address due for a re-CHECK, and only a
    # check that passes puts it back. The row now says which of the two states
    # it is in and why, and "refused" leads the intercepted row because a
    # refusal is a prohibition rather than a measurement.
    assert [state["text"] for state in states] == [
        "healthy",
        "unhealthy -- next check in 5m (ConnectTimeout -- benched 300s)",
        "refused -- TLS intercepted",
        "not checked yet",
    ]
    assert "proxy-entry-state-healthy" in states[0]["className"]
    assert "proxy-entry-state-unreachable" in states[1]["className"]
    assert "proxy-entry-state-intercepted" in states[2]["className"]
    assert "proxy-entry-state-unknown" in states[3]["className"]
    assert "8 of 9 requests answered" in states[0]["title"]
    assert "ConnectTimeout" in states[1]["title"]
    assert "certificate validation" in states[2]["title"]
    assert "No request has gone through this address yet" in states[3]["title"]


def test_jsdom_saving_sends_ids_and_never_a_proxy_url(rendered) -> None:
    """The page holds no URL for an entry it did not just type.

    Referencing an untouched entry by id is what makes a password on the
    client impossible rather than merely avoided.
    """

    body = rendered["proxying"]["saved"]["body"]

    assert body["provider"] == "nvidia_nim"
    assert body["policy"] == "failover"
    assert body["scope"] == "provider"
    assert body["max_switches"] == 2
    assert [entry["url"] for entry in body["entries"]] == ["", "", "", ""]
    assert body["entries"][0]["proxy"].startswith("px_")
    assert body["entries"][3]["direct"] is True


def test_jsdom_a_tick_alone_never_claims_the_server_knows_about_it(
    rendered,
) -> None:
    """The regression this file did not have a test for.

    Ticking a feed changes only the page's copy; the ingest route reads the
    store. Before this, the button counted ticks and relabelled itself
    "Fetch 7 feeds now" while the server had been told about none -- and the
    server answered, correctly, that no feeds were switched on. The label now
    says what the press will DO, and an unsaved selection says so.
    """

    before = rendered["proxying"]["feeds"]
    after = rendered["proxying"]["feedsAfterTick"]

    # The fixture arrives agreeing with its store, so nothing advertises a
    # save. One readable feed is on, so Fetch is a plain read.
    assert before["saveDisabled"] is True
    assert before["unsaved"] == ""
    assert before["fetchDisabled"] is False

    # One tick of a feed that was off: the press is now a save AND a read, and
    # the label says so rather than implying the server already knows.
    assert after["fetchLabel"] == "Save and fetch 2 feeds"
    assert after["fetchDisabled"] is False
    assert after["saveDisabled"] is False
    assert "not saved yet" in after["unsaved"]


def test_jsdom_a_feed_whose_format_mcc_cannot_read_is_never_counted(
    rendered,
) -> None:
    """The 7.16.0 trap in its 7.18.0 shape.

    A stored feed can name a reader this install does not ship -- a hand
    edit, or a format retired in a later release. Its switch is dead and the
    row says why, because the alternative is a feed that looks on, fetches
    nothing, and gives the operator no way to find out which of those it is.
    The Fetch button must not count it either: the server would not read it,
    so counting it would be the same lie in a new place.
    """

    feeds = rendered["proxying"]["feeds"]

    # The fixture's third row. Its box is the only disabled one.
    assert feeds["unreadableDisabled"] == [2]
    assert feeds["warnings"], "the unreadable row explained nothing"
    assert "no reader for this list's format" in feeds["warnings"][0]

    # Two feeds are switched on in the fixture, but only one is readable, and
    # that is the number the button offers to fetch.
    assert feeds["fetchLabel"] == "Fetch 1 feed now"


def test_jsdom_a_feed_row_shows_the_url_the_operator_typed(rendered) -> None:
    """Unlike a proxy URL, which is masked everywhere on this page.

    A proxy URL can carry user:pass. A feed URL is a public list the operator
    typed themselves, and hiding it would leave them unable to tell two lists
    apart or see what they pointed MCC at.
    """

    urls = rendered["proxying"]["feeds"]["urls"]

    assert urls == [
        "https://lists.example.com/socks5.json",
        "https://databay.com/api/v1/proxy-list?protocol=socks5&ssl=strict&limit=300",
        "https://lists.example.com/mystery.txt",
    ]


def test_jsdom_the_format_picker_is_shown_before_anything_is_detected(
    rendered,
) -> None:
    """The binding decision of this release, asserted at the DOM.

    The user asked for the picker *and* auto-detection. Detection proposes;
    it never decides silently. So the picker exists, offers every reader, and
    is empty before any detection has run -- there is no path on which a
    format is chosen without the operator seeing the control that chose it.
    """

    form = rendered["proxying"]["feedAddForm"]

    assert form["hasName"] is True
    assert form["hasUrl"] is True
    # The blank prompt plus all seven readers.
    assert form["pickerOptions"][0] == ""
    assert len(form["pickerOptions"]) == 8
    assert "lines" in form["pickerOptions"]
    assert form["pickerValue"] == ""
    # Nothing to read and nothing to add until a URL is typed.
    assert form["detectDisabled"] is True
    assert form["addDisabled"] is True
    assert "press Detect format" in form["note"]


def test_jsdom_detection_moves_the_picker_and_leaves_every_choice_open(
    rendered,
) -> None:
    """One read of the URL, a proposal, and a picker that still offers all of
    them.

    A detection that "succeeded" must not collapse the choice: the operator
    may know something about their own list that a trial parse does not.
    """

    detect = rendered["proxying"]["feedDetect"]

    assert detect["request"] is not None, "Detect format read nothing"
    assert detect["request"]["method"] == "POST"
    assert detect["request"]["body"] == {"url": "https://lists.example.com/fresh.json"}
    # The proposal moved the picker...
    assert detect["pickerValue"] == "geonode"
    assert "137 addresses" in detect["note"]
    assert "change it if you know better" in detect["note"]
    # ...and every reader is still on offer.
    assert len(detect["pickerOptions"]) == 8


def test_jsdom_the_operator_can_override_the_proposal_and_that_is_what_is_saved(
    rendered,
) -> None:
    """Detection proposes, the operator decides, and the save carries the
    operator's answer.

    This is the one that would catch a page that quietly re-applied the
    detected format on submit -- which would make the picker decoration.
    """

    override = rendered["proxying"]["feedOverride"]
    added = rendered["proxying"]["feedAfterAdd"]
    saved = rendered["proxying"]["feedSaveAfterAdd"]

    assert override["pickerValue"] == "monosans"
    # The note follows the picker rather than the proposal, so an override
    # does not read as if it had not registered.
    assert 'keyed by "host"' in override["note"]

    assert "A list I found" in added["rows"]
    # Added switched OFF: adding a list and reading it are separate acts.
    assert added["ticked"][-1] is False
    assert added["saveDisabled"] is False
    # The form is cleared, so a second press cannot silently repeat the first.
    assert added["urlValue"] == ""

    assert saved is not None, "the row was never saved"
    assert saved["method"] == "PUT"
    row = [
        feed
        for feed in saved["body"]["feeds"]
        if feed["url"] == "https://lists.example.com/fresh.json"
    ]
    assert row, "the saved list did not carry the new row"
    assert row[0]["parser"] == "monosans", "the override was not what was saved"
    assert row[0]["name"] == "A list I found"
    assert row[0]["id"] == "", "a new row must travel without an id"
    assert row[0]["enabled"] is False


def test_jsdom_removing_a_feed_keeps_the_addresses_it_supplied(rendered) -> None:
    """Removing a list is not discarding what it offered.

    A candidate is an independent fact with its own source_count and its own
    test result, and often the whole reason the list was added. Dropping the
    rows an operator may be halfway through choosing from is not what "remove
    this list" means.
    """

    removal = rendered["proxying"]["feedRemoval"]

    assert len(removal["after"]) == len(removal["before"]) - 1
    assert removal["before"][0] not in removal["after"]
    assert removal["candidatesStillShown"] > 0
    assert "stay on offer" in removal["announcement"]
    assert "Save feed list" in removal["announcement"]


def test_jsdom_pressing_fetch_saves_the_selection_before_it_reads_anything(
    rendered,
) -> None:
    """The save has to be first, and has to carry the tick.

    Order is the claim. An ingest that runs before the save asks the store
    about a selection it has never been told, which is precisely the 422 an
    operator hit with all seven boxes ticked on screen.
    """

    paths = rendered["proxying"]["feedFetchCalls"]
    save = rendered["proxying"]["feedSaveBody"]

    assert paths == [
        "/admin/api/proxy-chains/feeds",
        "/admin/api/proxy-chains/ingest",
    ], paths
    assert save is not None, "the press never saved the selection"
    assert save["method"] == "PUT"
    assert save["body"]["feeds"], "the save carried no feed ids"


def test_jsdom_a_saved_selection_stops_advertising_a_save(rendered) -> None:
    """Once the store agrees, the button is a plain read again."""

    after = rendered["proxying"]["feedsAfterFetch"]

    # Two readable feeds are on now: the one the fixture ships switched on, and
    # the one the tick above turned on and the press then saved.
    assert after["fetchLabel"] == "Fetch 2 feeds now"
    assert after["unsaved"] == ""
    assert after["saveDisabled"] is True


# ------------------------------------------------- the fetch job (7.21.0)


def test_jsdom_pressing_fetch_starts_a_job_and_shows_what_it_is_doing(
    rendered,
) -> None:
    """A fetch that tests hundreds of addresses cannot be one open request.

    The press starts a job on the server and comes straight back; the page
    shows the progress and a Stop. A spinner with no numbers and no way out
    would be the whole of what an operator saw for several minutes.
    """

    started = rendered["proxying"]["fetchStarted"]

    assert started["job"], "the press did not start a job"
    assert started["visible"] is True, "the progress panel is not visible"
    assert started["stopVisible"] is True, "there is no way to stop it"
    # And the button that starts it is out of action while one runs.
    assert started["refetchDisabled"] is True
    # And it says, from the first frame, what is about to happen to the
    # addresses those lists offered -- against which provider's own host, and
    # that only the ones that verify it are kept.
    assert "NVIDIA NIM's own host" in started["line"]
    # Since 7.22.2 what is about to happen is a tunnel and a verified
    # certificate rather than a request, and the line says which.
    assert "the certificate is verified through it" in started["line"]
    assert "no request is sent to NVIDIA NIM" in started["line"]


def test_jsdom_a_second_fetch_while_one_runs_is_refused_by_the_server(
    rendered,
) -> None:
    """Not merely a disabled button: the route answers 409."""

    assert rendered["proxying"]["fetchSecondPress"] == 409


def test_jsdom_the_progress_line_counts_what_was_tested_and_what_worked(
    rendered,
) -> None:
    """The sentence the user asked for, in the server's own numbers.

    "Tested 212 of 834 - 41 working - 163 dead - 2 refused". The page keeps no
    count of its own, which is how the line cannot drift from what was
    measured.
    """

    progress = rendered["proxying"]["fetchProgress"]
    line = progress["afterTwoPolls"]

    assert progress["moved"] is True, f"the counter never moved: {line}"
    assert re.search(r"Tested \d+ of 834", line), line
    assert re.search(r"\d+ working", line), line
    assert re.search(r"\d+ dead", line), line
    assert re.search(r"\d+ refused", line), line
    # The destination is named: a verdict is about one host.
    assert "NVIDIA NIM's own host" in line


def test_jsdom_stopping_keeps_what_had_already_passed(rendered) -> None:
    """Stop means "that is enough addresses", never "throw the work away"."""

    stopped = rendered["proxying"]["fetchStopped"]

    assert stopped["state"] == "stopped"
    assert stopped["testedKept"] is True
    assert stopped["workingKept"] is True
    assert stopped["stopVisible"] is False, "Stop is still offered after stopping"
    assert "stopping kept them" in stopped["line"]
    # The addresses that passed are still on the page to be used.
    assert stopped["offered"] > 0


def test_jsdom_arriving_at_the_page_re_attaches_to_a_running_fetch(rendered) -> None:
    """A reload must not lose a sweep that is minutes into its work.

    Leaving the view and coming back runs the same load path a browser reload
    does: it asks the server what is running and picks it up, timer and all.
    """

    again = rendered["proxying"]["fetchReattached"]

    assert again["visible"] is True, "the progress panel did not come back"
    assert again["stopVisible"] is True, "the re-attached run cannot be stopped"
    assert "Tested" in again["line"]
    assert again["stillPolling"] is True, "it re-rendered once and then went quiet"


def test_jsdom_a_finished_fetch_says_what_it_found(rendered) -> None:
    finished = rendered["proxying"]["fetchFinished"]

    assert finished["state"] == "done"
    assert "Tested 834 of 834" in finished["line"]
    assert "none is in a chain" in finished["line"]


def test_jsdom_the_page_says_the_pace_it_resolved_and_that_nothing_was_sent(
    rendered,
) -> None:
    """Two sentences a person can act on, in the server's own numbers.

    A percentage is a number the operator did not type, so the page says what
    it worked out to; and the default depth changes what the provider sees, so
    the page says that too rather than leaving it to the release notes. The
    stale-number check is the other half: the page must not still be saying 32.
    """

    line = rendered["proxying"]["fetchFinished"]["line"]
    running = rendered["proxying"]["fetchProgress"]["afterTwoPolls"]

    assert "Testing 96 at a time (6% of 1,592)." in line, line
    assert "without a request being sent to NVIDIA NIM" in line, line
    assert "Testing 96 at a time (6% of 1,592)." in running, running
    assert "no request is sent to NVIDIA NIM" in running, running
    for stale in ("32 at a time", "testing 32", "4 at a time"):
        assert stale not in line and stale not in running


def test_jsdom_a_candidate_row_says_how_it_was_proven(rendered) -> None:
    """A verified handshake and an answered request are both passes.

    They are not the same evidence, though, and a row that said only "working"
    would leave the operator to guess which one this was -- which matters
    exactly when they are deciding whether to put a stranger's machine in front
    of a credential.
    """

    texts = [row["text"] for row in rendered["proxying"]["candidateMeasured"]]

    assert any("tunnel + certificate verified" in text for text in texts), texts
    assert any("HTTPS request answered" in text for text in texts), texts


def test_jsdom_the_fetch_summary_shows_working_slow_confirmed_dead_refused(
    rendered,
) -> None:
    """Slow is not dead, and dead is only what failed every re-test.

    `working` counts every pass, of which `slow` and `flaky` are labelled
    parts, so the plain-working number is what is left; `dead` counts every
    address not kept, of which `confirmed_dead` failed every confirm round and
    the rest refused the connection at the dial. A status without a confirm
    stage (an older server) keeps the old sentence.
    """

    confirm = rendered["proxying"]["fetchConfirm"]
    done = confirm["done"]

    assert "Tested 1000 of 1000" in done, done
    for part in (
        "126 working",
        "190 slow",
        "12 flaky",
        "540 confirmed dead",
        "65 refused",
        "67 not listening",
    ):
        assert part in done, (part, done)
    assert "607 dead" not in done, done

    quiet = confirm["doneQuiet"]
    assert "4 working · 1 slow · 5 confirmed dead · 0 refused" in quiet, quiet
    assert "flaky" not in quiet, quiet
    assert "not listening" not in quiet, quiet

    legacy = confirm["legacy"]
    assert "Tested 20 of 20 · 4 working · 15 dead · 1 refused" in legacy, legacy
    assert "confirmed dead" not in legacy and "slow" not in legacy, legacy

    running = confirm["running"]
    assert "confirming 612 addresses (attempt 2 of 3)" in running, running
    assert "nothing is called dead until every re-test has failed" in running, running

    paused = confirm["paused"]
    assert paused.startswith(
        "Your own connection to opencode.ai is failing -- paused."
    ), paused
    assert "Nothing is marked dead while it is paused" in paused, paused


def test_jsdom_candidate_rows_carry_a_state_chip(rendered) -> None:
    """Every pass is kept; the chip says how it passed, and nothing is guessed."""

    chips = rendered["proxying"]["candidateStateChips"]

    for state in ("working", "slow", "flaky"):
        chip = chips[state]
        assert chip is not None, state
        assert chip["text"] == state, chip
        assert "proxy-state-chip" in chip["className"].split(), chip
        assert f"proxy-state-{state}" in chip["className"].split(), chip
    assert "3500 ms" in chips["slow"]["title"], chips["slow"]
    assert "PROXY_CHECK_SLOW_MS" in chips["slow"]["title"], chips["slow"]
    assert "passed on try 4 of 4" in chips["flaky"]["title"], chips["flaky"]
    assert chips["none"] is None
    assert chips["absent"] is None


def test_jsdom_the_unhealthy_wording_names_the_round(rendered) -> None:
    """A bench is the result of a round of tries, and the row says which."""

    round_ = rendered["proxying"]["unhealthyRound"]
    clock = round_["expectedClock"]

    with_tries = round_["withTries"]
    assert f"failed 3 of 3 tries at {clock}" in with_tries, with_tries
    assert "next round in 59m" in with_tries, with_tries
    assert with_tries.endswith("(no answer)"), with_tries

    due = round_["dueWithTries"]
    assert f"failed 3 of 3 tries at {clock}; due for a new round" in due, due

    # Without a tries count, today's wording, exactly.
    assert round_["withoutTries"] == "unhealthy -- next check in 59m (no answer)"
    # An interception is never a count of tries.
    assert "tries" not in round_["intercepted"], round_["intercepted"]


def test_jsdom_the_two_fetch_selects_are_on_limits_and_resilience(rendered) -> None:
    """Every tunable is reachable from the dashboard, on the page it belongs to."""

    limits = rendered["limits"]

    assert limits["fetchConcurrencyModeOptions"] == ["", "fixed", "percent"]
    assert limits["fetchCheckDepthOptions"] == ["", "tls", "request"]
    assert "4 to 500" in limits["fetchConcurrencyRange"]


def test_jsdom_one_press_adds_every_working_address_to_the_chosen_provider(
    rendered,
) -> None:
    """The point of testing them up front: using them is one gesture.

    Five of the fixture's six candidates are working; the sixth is refused and
    is not one of them.
    """

    button = rendered["proxying"]["addAllWorking"]

    assert button["visible"] is True
    assert button["disabled"] is False
    # Four, not five: the bulk-add block earlier in the harness already moved
    # one of the working addresses into a chain, so it is no longer on offer.
    assert button["label"] == "Add all 4 working to NVIDIA NIM"


def test_jsdom_a_candidate_row_says_what_mcc_measured_and_for_whom(rendered) -> None:
    """ "Working" with no "for whom" would be a claim about hosts nobody tested.

    And a row stored by 7.18-7.20, which offered addresses without testing
    them, must never read as working: it says so instead, and the next fetch
    replaces it.
    """

    rows = rendered["proxying"]["candidateMeasured"]
    texts = [row["text"] for row in rows]

    assert any(text.startswith("working for NVIDIA NIM") for text in texts), texts
    assert "not tested -- fetch again" in texts, texts
    working = [row for row in rows if row["text"].startswith("working for")]
    assert all("proxy-candidate-working" in row["className"] for row in working)
    untested = [row for row in rows if row["text"] == "not tested -- fetch again"]
    assert all("proxy-candidate-untested" in row["className"] for row in untested)


def test_jsdom_the_refused_addresses_can_be_looked_at(rendered) -> None:
    """A count with nothing behind it is not a report.

    A fetch says "12 refused" and, until 7.35.1, showed none of them: the
    operator could not see which address it meant or whether the verdict was an
    hour old or from March. The toggle is off by default -- these are not
    offers -- and turning it on lists them with the reason and the age.
    """

    refused = rendered["proxying"]["refusedList"]

    assert refused["collapsed"]["toggle"] == "Show the 2 refused"
    assert refused["collapsed"]["expanded"] == "false"
    assert refused["collapsed"]["rows"] == 0, "refused rows were shown unasked"

    shown = refused["expanded"]
    assert shown["toggle"] == "Hide the 2 refused"
    assert shown["ariaExpanded"] == "true"
    assert shown["labels"] == [
        "http://198.51.100.70:8080",
        "socks5h://198.51.100.71:1080",
    ]
    assert all("certificate validation" in reason for reason in shown["reasons"])
    assert shown["whens"] == [
        "Refused 30m ago against NVIDIA NIM",
        "Refused 2h ago against NVIDIA NIM",
    ]
    assert "a later test that succeeds is what clears one" in shown["note"].lower()
    assert refused["reCollapsed"]["rows"] == 0


def test_jsdom_a_refused_address_is_offered_no_way_into_a_chain(rendered) -> None:
    """The page's half of a refusal the server already enforces.

    Every path that could add an address re-checks it and refuses an
    intercepting one, so a button here could only ever produce a 422 -- but an
    operator who presses it has been invited to try, which is worse than not
    showing the row at all. So the rows carry nothing pressable: measured, not
    promised in a comment.
    """

    shown = rendered["proxying"]["refusedList"]["expanded"]

    assert shown["controls"] == 0, "a refused row carried a control"


def test_jsdom_an_empty_offer_list_says_which_emptiness_it_is(rendered) -> None:
    """ "None passed" and "all of them are in a chain" are opposite outcomes.

    Until 7.35.1 the panel printed the first sentence for both, so the ordinary
    end of a *successful* sweep -- every address that passed promoted into a
    chain -- read as a sweep that found nothing. Three states, three sentences,
    and the one that was always true is still there for when it is true.
    """

    empty = rendered["proxying"]["emptyOffer"]

    assert empty["allInAChain"].startswith("5 passed -- all of them are already")
    assert "nothing left here to choose from" in empty["allInAChain"]

    assert empty["allDiscarded"].startswith("5 passed, and none of them is still")
    assert "goes into a chain or is discarded" in empty["allDiscarded"]

    assert empty["nonePassed"].startswith("None of the addresses those lists offered")

    # And the progress line above stopped contradicting it: "every address on
    # offer below verified ..." is a claim about rows, and there are none.
    assert "Every address on offer below" not in empty["progressLine"]
    assert "none is in a chain" not in empty["progressLine"]
    assert empty["progressLine"].startswith("Tested 834 of 834")
    assert "5 working" in empty["progressLine"]

    # And the refused list is reachable from the empty panel, which is the
    # state an operator most often meets it in.
    assert empty["refusedToggle"] == "Hide the 2 refused"


def test_jsdom_the_subscription_card_says_what_it_risks_and_starts_unticked(
    rendered,
) -> None:
    """Said on the OAuth cards only, and exactly once there."""

    oauth = rendered["proxying"]["oauth"]

    assert oauth is not None
    assert oauth["acknowledged"] is False
    assert "personal subscription" in oauth["note"]
    assert "more likely to be flagged" in oauth["note"]


def test_jsdom_the_page_says_the_checker_is_off_and_what_a_test_does(
    rendered,
) -> None:
    """Three facts, and the first is the one an operator cannot guess.

    "Not tested" beside no explanation reads like a page that failed to load.
    The note says the loop is off, what pressing Test actually opens, and that
    no exit-IP URL is set -- so nobody has to wonder who MCC is contacting.
    """

    note = rendered["proxying"]["checkerNote"]

    assert "Background checking is off" in note
    assert "until you press Test" in note
    assert "strict certificate verification" in note
    assert "TLS intercepted and refused" in note
    assert "No exit-IP URL is set, so MCC contacts nobody but the provider" in note


def test_jsdom_each_row_reads_back_what_the_checker_measured(rendered) -> None:
    """Latency, the TLS verdict and how long ago -- and "not tested" when nothing was.

    A separate line from the health beside it, because they answer different
    questions: health is what the running pools saw carrying real traffic,
    this is what a deliberate test found.
    """

    readouts = rendered["proxying"]["checkReadouts"]

    assert len(readouts) == 4
    assert readouts[0]["text"].startswith("412 ms")
    assert "TLS strict" in readouts[0]["text"]
    assert "2m ago" in readouts[0]["text"]
    assert readouts[1]["text"].startswith("no answer")
    assert readouts[2]["text"].startswith("TLS intercepted")
    # Direct has no address to dial, and says so rather than borrowing a
    # verdict it could not have.
    assert readouts[3]["text"] == "no proxy to test"


def test_jsdom_an_intercepting_address_is_struck_through_and_kept_on_the_card(
    rendered,
) -> None:
    """Refused, not hidden.

    An operator whose chain just got shorter has to be able to see which
    address left it and why, so the row stays, drawn as the refusal it is.
    """

    proxying = rendered["proxying"]

    assert proxying["refusedRows"] == ["192.0.2.44:3128"]
    assert proxying["refusedRowsAfterTest"] == ["192.0.2.44:3128"]


def test_jsdom_only_a_saved_address_offers_a_check_now_button(rendered) -> None:
    """The check dials the stored URL, which the page has never been told.

    So the three saved addresses can be checked and the Direct rung cannot, and
    the button says so by not being there rather than by failing when pressed.
    The word is "Check now" since 7.19.0: an address that failed comes back
    only when a check passes, so this is how an operator asks for that rather
    than a diagnostic they might reasonably skip.
    """

    assert rendered["proxying"]["testButtons"] == [True, True, True, False]
    assert rendered["proxying"]["testAll"] == "Test all (3)"


def test_jsdom_pressing_test_posts_one_proxy_id_and_re_reads_the_verdict(
    rendered,
) -> None:
    """One address per press, named by id, and the row updates from the answer.

    The response is the whole refreshed payload, so the card drops its draft:
    a check writes the store, and a card still holding a pre-check draft would
    keep showing the old latency beside a row that had just been measured.
    """

    proxying = rendered["proxying"]

    assert proxying["checkPost"]["body"] == {
        "provider": "nvidia_nim",
        "proxy": "px_aaaa1111",
    }
    assert proxying["readoutsAfterTest"][0] == "377 ms · TLS strict · just now"
    assert "answered in 377 ms" in proxying["announcementAfterTest"]
    assert "certificate verified" in proxying["announcementAfterTest"]


def test_jsdom_a_refused_test_says_what_a_refusal_means(rendered) -> None:
    """Not "the request failed": an interception is a finding, not an outage.

    The sentence has to carry both halves of the consequence -- it cannot be
    saved into a chain, and it is already out of the ones it is in -- because
    that is the whole of what the control does.
    """

    said = rendered["proxying"]["announcementAfterRefusedTest"]

    assert "192.0.2.44:3128 breaks certificate validation and is refused" in said
    assert "reading the traffic rather than relaying it" in said
    assert "cannot be saved into a chain" in said
    assert "held out of the ones it is already in" in said


# ------------------------------------------- bulk management of the offers


def test_jsdom_the_offer_list_has_one_destination_and_not_one_per_row(
    rendered,
) -> None:
    """The whole ask, in one assertion.

    Every row used to carry its own provider ``<select>`` and its own button,
    which with 1,572 addresses after a seven-feed fetch is 1,572 dropdowns.
    There is now one picker, in the action bar, for the whole selection.
    """

    candidates = rendered["proxying"]["candidates"]

    assert candidates["rows"] == 6
    assert candidates["perRowPickers"] == 0
    assert candidates["selectAll"] is True
    # Only a provider with an https base URL to verify a tunnel against.
    assert candidates["destinations"] == ["NVIDIA NIM"]
    # And the row-level reason for the one that is not offered survives, once,
    # where it can be acted on.
    assert "ChatGPT (OAuth) is not offered" in candidates["note"]
    assert "no https base URL" in candidates["note"]


def test_jsdom_the_bar_says_how_much_of_that_chain_is_already_spoken_for(
    rendered,
) -> None:
    """A chain holds twelve entries, and a bulk add has to say so up front.

    This is the fact that shapes a bulk add more than any other, and the page
    used to leave it to be discovered by a 422 on the thirteenth press.
    """

    capacity = rendered["proxying"]["candidates"]["capacity"]

    assert "NVIDIA NIM has 4 of 12 entries" in capacity
    assert "8 more will fit" in capacity


def test_jsdom_the_offers_sort_and_filter_and_select_all_means_the_filtered(
    rendered,
) -> None:
    """Filter-then-apply-to-filtered, the Models page's rule (6.7.0)."""

    candidates = rendered["proxying"]["candidates"]

    # Feeds agreeing first by default: the one field that is evidence rather
    # than a claim copied from one publisher.
    assert candidates["order"][0] == "203.0.113.21:8080"
    assert candidates["orderByLatency"][0] == "203.0.113.25:8080"
    assert candidates["socksOnly"] == ["203.0.113.22:1080", "203.0.113.24:1080"]
    # "Select all" under a filter means everything the filter matches.
    assert candidates["selectedWhileFiltered"] == "2 selected of 2 shown"
    # And dropping the filter does not drop what was selected under it.
    assert candidates["selectedAfterFilterCleared"] == "2 selected of 6 shown"


def test_jsdom_escape_clears_the_selection_and_ranges_work_without_a_pointer(
    rendered,
) -> None:
    """WCAG 2.2: a range gesture must have a keyboard equivalent.

    6.7.0 records this as a hard requirement rather than a nicety, so the
    Shift+Arrow walk is tested beside the Shift+click it mirrors.
    """

    candidates = rendered["proxying"]["candidates"]

    assert candidates["afterEscape"] == "6 shown, none selected"
    # Three contiguous rows from one Shift+click.
    assert candidates["afterShiftClick"] == [
        "px_cand0005",
        "px_cand0001",
        "px_cand0006",
    ]
    # And the same gesture from the keyboard alone.
    assert candidates["afterShiftArrow"] == ["px_cand0005", "px_cand0001"]


def test_jsdom_one_press_sends_one_batched_request_for_the_whole_selection(
    rendered,
) -> None:
    """One request, one route, one destination -- not one request per address.

    A per-address loop is the read-modify-write race 6.7.0 found on the Models
    page, and the reason the write here is batched on both sides of the wire.
    """

    candidates = rendered["proxying"]["candidates"]

    assert candidates["addLabel"] == "Test and add 3 selected"
    assert candidates["addCalls"] == ["/admin/api/proxy-chains/candidates/bulk"]
    assert candidates["addBody"]["body"] == {
        "action": "add",
        "provider": "nvidia_nim",
        "proxies": ["px_cand0001", "px_cand0003", "px_cand0004"],
        "undo_token": "",
        # 7.27.0: which batch owns the republish. A selection that fits in one
        # batch is its own last batch, so it republishes exactly as it always
        # did; a longer one sends false until the end, which is what turns
        # thirty generation replaces for three hundred addresses into one.
        "republish": True,
    }


def test_jsdom_a_partial_result_reads_as_the_normal_case_it_is(rendered) -> None:
    """Added, benched and refused in one press, reported as three groups.

    These are strangers' machines read from public lists: a mixed outcome is
    the ordinary one, and reporting it as a failed request would be a lie
    about eleven addresses out of twelve.
    """

    candidates = rendered["proxying"]["candidates"]
    said = candidates["summary"]

    assert "2 of 3 address(es) went into NVIDIA NIM's chain" in said
    assert "still off until you enable it" in said
    assert "1 answered and the destination's certificate verified" in said
    assert "1 did not answer, so they were added benched" in said
    assert "not a failure of this press" in said
    assert "1 break certificate validation and were refused" in said
    assert "no credential went near them" in said
    # The per-row half of the same vocabulary: the refused address stays on
    # offer, marked, and the two that landed have left the list.
    refused = [row for row in candidates["outcomes"] if row["proxy"] == "px_cand0003"]
    assert refused and refused[0]["refused"] is True
    assert refused[0]["badge"] == "TLS intercepted"
    # Said once, not twice: the standing badge and this press's outcome are the
    # same finding in the same words, so only the badge is drawn.
    assert refused[0]["outcome"] == ""
    assert candidates["rowsAfterAdd"] == 4
    assert candidates["chainAfterAdd"][-2:] == [
        "203.0.113.21:8080",
        "203.0.113.24:1080",
    ]
    # What stays selected is exactly what did not land.
    assert candidates["selectionAfterAdd"] == ["px_cand0003"]


def test_jsdom_the_add_is_undone_through_the_panel_that_reported_it(
    rendered,
) -> None:
    """One `role=status` panel with Undo, never a toast that vanishes.

    A summary this long cannot be read in the life of a toast, and the action
    it offers has to be reachable from where it was read.
    """

    candidates = rendered["proxying"]["candidates"]

    assert candidates["undoOffered"] is True
    assert candidates["rowsAfterUndo"] == 6
    assert candidates["chainAfterUndo"] == [
        "203.0.113.7:1080",
        "198.51.100.9:8080",
        "192.0.2.44:3128",
        "Direct (no proxy)",
    ]
    assert "Put back as it was before that action" in candidates["undoSentence"]
    # A refusal is a measurement, not a change: undo does not un-measure it.
    assert "keeps its verdict" in candidates["undoSentence"]


def test_jsdom_discarding_offers_touches_no_chain(rendered) -> None:
    """ "Same for remove" -- and the two removes are different acts.

    Discarding an offer says "stop showing me this". Removing a chain entry
    takes an address out of the rotation. Conflating them is how an operator
    loses a working proxy by tidying a list.
    """

    after = rendered["proxying"]["candidates"]["afterDiscard"]

    assert after["rows"] == 5
    assert "203.0.113.22:1080" not in after["labels"]
    assert "1 address(es) are no longer offered" in after["sentence"]
    assert "No chain was touched" in after["sentence"]
    assert after["chain"] == 4


def test_jsdom_the_selection_and_the_filters_survive_a_reload(rendered) -> None:
    """Persistence: what was picked, and what the page was sorted by.

    Choosing forty addresses out of 1,572 is work, and an F5 is not a decision
    to throw it away. Stored the way the dashboard stores its Analytics
    filters -- best effort, and the page is correct without it.
    """

    stored = json.loads(rendered["proxying"]["candidates"]["stored"])

    assert "px_cand0003" in stored["selected"]
    assert stored["view"]["sort"] == "latency"


def test_jsdom_chain_entries_are_removed_in_bulk_from_the_card(rendered) -> None:
    """The symmetric gesture on the other side of the page.

    And the per-row Remove is this action with one element, not a second path
    through the draft.
    """

    proxying = rendered["proxying"]

    assert proxying["entrySelectBoxes"] == 4
    assert proxying["entryBulkRemoveOffered"] is True
    assert proxying["entriesAfterBulkRemove"]["before"] == 4
    assert proxying["entriesAfterBulkRemove"]["after"] == 2
    said = proxying["entriesAfterBulkRemove"]["sentence"]
    assert "Removed 2 entries from NVIDIA NIM" in said
    # A draft change, not a write: the chain on disk is untouched until Save.
    assert "nothing has changed on disk yet" in said


# --------------------------------------------------------------------------- #
# Per-model reasoning and output preferences (7.25.0)
#
# The whole point of the control is that it offers what THIS model reports and
# says why it withholds the rest, so these assert the drawing rule against the
# five capability shapes the catalogue actually contains.
# --------------------------------------------------------------------------- #


def test_jsdom_the_preference_controls_are_drawn_above_the_parameter_grid(
    rendered,
) -> None:
    """Two headed sections, preferences first: they are statements about a
    decision, and the nine below them are fields of a request body."""

    editor = rendered["models"]["preferences"]["0"]

    assert editor["heads"][0] == "PreferenceWhat to decideValue"
    assert editor["heads"][1] == "ParameterWhat to sendValue"
    # Three-state, the same idiom as every other row on the form.
    assert editor["modes"] == ["inherit", "inherit"]


def test_jsdom_the_reasoning_select_offers_exactly_what_the_row_published(
    rendered,
) -> None:
    editor = rendered["models"]["preferences"]["0"]

    assert [option[0] for option in editor["options"]] == [
        "client",
        "off",
        "adaptive",
        "low",
        "high",
    ]


def test_jsdom_off_is_disabled_on_a_mandatory_model_and_says_why(rendered) -> None:
    options = {
        option[0]: option
        for option in rendered["models"]["preferences"]["1"]["options"]
    }

    assert options["off"][1] is True
    assert "cannot run with thinking disabled" in options["off"][2]


def test_jsdom_a_model_that_does_not_reason_has_the_control_disabled(
    rendered,
) -> None:
    editor = rendered["models"]["preferences"]["2"]

    assert editor["selectDisabled"] is True
    assert editor["options"][0][1] is True


def test_jsdom_an_unknown_vocabulary_offers_every_rung_marked_unverified(
    rendered,
) -> None:
    editor = rendered["models"]["preferences"]["3"]

    assert [option[0] for option in editor["options"]][3:] == [
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert all("unverified" in option[2] for option in editor["options"])
    assert "clamped to what it accepts" in editor["notes"][0]


def test_jsdom_a_host_with_no_effort_field_says_a_level_sends_nothing(
    rendered,
) -> None:
    options = {
        option[0]: option
        for option in rendered["models"]["preferences"]["4"]["options"]
    }

    assert "nothing is sent" in options["high"][2]


def test_jsdom_the_output_control_is_bounded_by_the_published_limit(
    rendered,
) -> None:
    editor = rendered["models"]["preferences"]["0"]

    assert editor["numberType"] == "number"
    assert editor["numberMax"] == "40960"
    assert "40,960" in editor["notes"][1]
    assert "models.dev bucket, exact id" in editor["notes"][1]


def test_jsdom_a_model_with_no_published_limit_still_offers_the_control(
    rendered,
) -> None:
    editor = rendered["models"]["preferences"]["5"]

    assert editor["numberMax"] is None
    assert "becomes the limit" in editor["notes"][1]


def test_jsdom_a_stored_value_the_catalogue_dropped_is_kept_and_badged(
    rendered,
) -> None:
    """Never rewritten on disk: the catalogue may move back."""

    editor = rendered["models"]["preferences"]["6"]

    assert editor["selectValue"] == "xhigh"
    assert "no longer offered" in [option[0] for option in editor["options"]] or any(
        option[0] == "xhigh" for option in editor["options"]
    )
    assert "no longer in this model's vocabulary" in editor["notes"][0]


def test_jsdom_the_provider_card_offers_the_whole_vocabulary(rendered) -> None:
    editor = rendered["models"]["providerPreferences"]

    assert [option[0] for option in editor["options"]] == [
        "off",
        "client",
        "adaptive",
        "minimal",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    ]
    assert "each model clamps this" in editor["notes"][0]


def test_jsdom_the_value_control_is_disabled_until_force_value_is_chosen(
    rendered,
) -> None:
    """The three states stay distinguishable: a box you can type in while the
    mode says Inherit would make "inherit" and "force" look the same."""

    assert rendered["models"]["preferences"]["0"]["valueDisabledWhileInherit"] is True


# ------------------------------------------------------------- key pool rail
# The pool is an ordered list and the order is the failover order, so it is a
# rail. jsdom cannot prove the drag (no PointerEvent) or the layout (no boxes);
# it proves the structure, the keyboard equivalent, what goes out on the wire
# and what the status line says.


def test_the_key_pool_renders_a_reorder_grip_and_move_buttons(rendered) -> None:
    rows = rendered["keyRail"]["rows"]

    assert len(rows) == 3
    assert all(row["grip"] for row in rows)
    assert [row["moveLabels"] for row in rows] == [["Move up", "Move down"]] * 3
    assert rows[0]["gripLabel"] == "Reorder sk-one...1111"


def test_the_first_key_cannot_move_up_and_the_last_cannot_move_down(
    rendered,
) -> None:
    """The ends are the whole reason a rail needs buttons as well as a drag."""

    rows = rendered["keyRail"]["rows"]

    assert rows[0]["moveDisabled"] == [True, False]
    assert rows[1]["moveDisabled"] == [False, False]
    assert rows[2]["moveDisabled"] == [False, True]


def test_an_unnamed_key_reads_as_its_masked_label_alone(rendered) -> None:
    """A key nobody renamed renders exactly as it did before this release."""

    rows = rendered["keyRail"]["rows"]

    assert rows[0]["keyText"] == "sk-one...1111"
    assert rows[0]["keyTitle"] == ""
    assert rows[0]["nameValue"] == ""
    assert rows[0]["display"] == "sk-one...1111"


def test_a_named_key_reads_as_its_name_with_the_mask_on_hover(rendered) -> None:
    rows = rendered["keyRail"]["rows"]

    assert rows[1]["keyText"] == "Personal <b>card</b>"
    assert rows[1]["keyTitle"] == "sk-two...2222"
    assert rows[1]["nameValue"] == "Personal <b>card</b>"
    assert rows[1]["nameMax"] == 60


def test_a_name_containing_markup_is_rendered_as_text(rendered) -> None:
    """The name is user input landing in a page that also renders log rows."""

    assert rendered["keyRail"]["namedRowHtml"] == "Personal &lt;b&gt;card&lt;/b&gt;"


def test_the_add_form_offers_an_optional_name(rendered) -> None:
    assert rendered["keyRail"]["addNamePlaceholder"] == "Name (optional)"


def test_moving_a_key_puts_the_new_id_order(rendered) -> None:
    rail = rendered["keyRail"]
    put = rail["orderPut"]

    assert put["method"] == "PUT"
    assert put["path"] == "/admin/api/credentials/NAMED_API_KEY/keys/order"
    # The last key moved up one: ids, never positions.
    assert put["body"]["order"] == [
        "sha256:aaaaaaaaaaaaaaaa",
        "sha256:cccccccccccccccc",
        "sha256:bbbbbbbbbbbbbbbb",
    ]


def test_moving_a_key_announces_its_new_position_and_says_health_restarts(
    rendered,
) -> None:
    """Q1: the sentence that explains a badge which just went back to HEALTHY."""

    rail = rendered["keyRail"]

    # The line is created only when there is something to say.
    assert rail["statusBefore"] is None
    assert rail["statusRole"] == "status"
    assert rail["statusLive"] == "polite"
    assert rail["statusText"] == (
        "Moved sk-thr...3333 to position 2 of 3. "
        "Reordering rebuilds this pool, so its health counters start again."
    )
    assert rail["undoLabel"] == "Undo"


def test_undo_puts_the_order_back(rendered) -> None:
    put = rendered["keyRail"]["undoPut"]

    assert put["method"] == "PUT"
    assert put["body"]["order"] == [
        "sha256:aaaaaaaaaaaaaaaa",
        "sha256:bbbbbbbbbbbbbbbb",
        "sha256:cccccccccccccccc",
    ]


def test_the_keyboard_equivalent_makes_the_same_move(rendered) -> None:
    """WCAG 2.2: the drag is not the only way to reorder."""

    put = rendered["keyRail"]["keyboardPut"]

    assert put["method"] == "PUT"
    assert put["body"]["order"] == [
        "sha256:bbbbbbbbbbbbbbbb",
        "sha256:aaaaaaaaaaaaaaaa",
        "sha256:cccccccccccccccc",
    ]


def test_renaming_a_key_puts_only_the_name(rendered) -> None:
    """A rename is store-only: it must never touch the order route."""

    rail = rendered["keyRail"]
    put = rail["renamePut"]

    assert put["method"] == "PUT"
    assert put["path"] == (
        "/admin/api/credentials/NAMED_API_KEY/keys/sha256%3Aaaaaaaaaaaaaaaaa/name"
    )
    assert put["body"] == {"name": "Work laptop"}
    assert "stored on this machine only" in rail["renameStatus"]


# ------------------------------------------------------------ the one rail
# 7.34.1. A user reported that "custom provider cards overflow the options for
# adding a key with name etc." 7.29.0 had given every key row a grip, a name
# box and two Move buttons, but each of the three pools built its own row and
# add-form markup against its own copy of four CSS rules -- and only the
# env-key copy carried `flex-wrap: wrap`. Measured in Chrome at a 1500px
# viewport: `.cp-key-row` wanted 538px inside a 228px card, `.ws-key-row` 536.
#
# The three now share one builder and one rule set, so the guard is not "the
# custom card wraps" -- that would pass again the day someone forks it -- but
# "the three render the same classes and the same add form".


def test_every_key_pool_renders_the_same_rail(rendered) -> None:
    """One builder, three pools: same list, row, label and add-form classes."""

    rail = rendered["sharedRail"]
    env, websearch, custom = rail["env"], rail["websearch"], rail["custom"]

    assert custom is not None, "the custom_acme card did not render"
    for pool in (env, websearch, custom):
        assert pool["list"] is not None
        assert pool["row"] is not None
        assert pool["label"] is not None
        # The class the stylesheet addresses, on all three.
        assert "key-manager-list" in pool["list"]
        assert "key-manager-row" in pool["row"]
        assert "key-manager-key" in pool["label"]
        assert "key-manager-add" in pool["addClasses"]
        # And the same element, so one ellipsis rule covers all three.
        assert pool["labelTag"] == "CODE"

    # The pre-7.34.1 names ride along, so selectors written against them --
    # in this repo's own tests, and in anyone's user stylesheet -- resolve.
    assert websearch["list"] == ["key-manager-list", "ws-key-list"]
    assert websearch["row"] == ["key-manager-row", "ws-key-row"]
    assert websearch["label"] == ["key-manager-key", "ws-key-label"]
    assert websearch["addClasses"] == ["key-manager-add", "ws-key-add"]
    assert custom["list"] == ["cp-key-list", "key-manager-list"]
    assert custom["row"] == ["cp-key-row", "key-manager-row"]
    assert custom["label"] == ["cp-key-label", "key-manager-key"]
    assert custom["addClasses"] == ["cp-key-add", "key-manager-add"]


def test_every_key_pool_offers_the_same_add_form(rendered) -> None:
    """The secret, then the optional name, then the button. Everywhere."""

    rail = rendered["sharedRail"]
    shape = [
        "INPUT|password|key-add-secret",
        "INPUT|text|key-name-input key-add-name",
        "BUTTON|button|secondary-button key-add-submit",
    ]

    assert rail["env"]["addShape"] == shape
    assert rail["websearch"]["addShape"] == shape
    assert rail["custom"]["addShape"] == shape
    for pool in ("env", "websearch", "custom"):
        assert rail[pool]["addNamePlaceholder"] == "Name (optional)"


def test_every_key_pool_row_carries_the_reorder_controls(rendered) -> None:
    """The rail's gestures, not just its markup, are the same on all three."""

    rail = rendered["sharedRail"]
    for pool in ("env", "websearch", "custom"):
        assert rail[pool]["grip"] is True, pool
        assert rail[pool]["nameBox"] is True, pool
        assert rail[pool]["moves"] == ["Move up", "Move down"], pool


def test_a_custom_card_showing_a_rail_says_so(rendered) -> None:
    """`has-key-rail` is what gives the card the whole grid row.

    A `minmax(240px, 1fr)` column cannot hold six controls; a built-in card
    has taken the whole row since `.pv-card.pv-open` existed, and this is the
    same rule for the card that renders its rail inline.
    """

    assert rendered["sharedRail"]["customIsRailHost"] is True


def test_the_health_badge_sits_where_it_does_on_a_built_in_row(rendered) -> None:
    """Between the key and the Move buttons, not after Move down."""

    rail = rendered["sharedRail"]
    assert rail["customRowOrder"] is not None
    assert rail["customRowOrder"] == [
        "key-drag-grip",
        "key-name-input",
        "key-manager-key",
        "cp-key-health",
        "ghost-button",
        "ghost-button",
        "ghost-button",
    ]
    # The env row it is copying: grip, name, key, then the Move buttons.
    assert rail["envRowOrder"][:3] == [
        "key-drag-grip",
        "key-name-input",
        "key-manager-key",
    ]


# ----------------------------------------------------------- capability row
# 7.33.0 added `field.note` to the Models capability table and appended it
# twice, so a row carrying one read its sentence out two times.


def test_a_capability_note_is_rendered_once(rendered) -> None:
    """One note, one element -- beside the badge it explains."""

    row = rendered["capabilityRow"]
    assert row["noteCount"] == 1
    assert row["notes"][0] == "the npm package selected this door"
    # The tier line and the guessed-from line are still there, and still once.
    assert sum(1 for text in row["notes"] if text.startswith("matched at tier")) == 1
    assert sum(1 for text in row["notes"] if text.startswith("guessed from")) == 1


def test_the_display_join_shows_a_name_instead_of_a_mask(rendered) -> None:
    """One resolver feeds the log row, the modal, the ladder and the breakdown."""

    names = rendered["keyNames"]

    assert names["named"] == "Personal"
    assert names["unnamed"] == "sk-o\u20261111"
    assert names["missing"] == ""
    # The breakdown groups by label, so it keeps the label in view.
    assert names["breakdownNamed"] == "Personal (sk-t\u20262222)"
    assert names["breakdownUnnamed"] == "sk-o\u20261111"
    # The ladder keeps `key <index>` untouched and renders the name after it.
    assert "key 1 Personal" in names["ladderNamed"]
    assert "key 0 sk-o…1111" in names["ladderUnnamed"]
    # With no index, every surface is byte-identical to before.
    assert names["afterClear"] == "sk-t\u20262222"


# --------------------------------------------------------- advanced fields
# 7.29.1. A user could not find RATE_LIMIT_COOLDOWN_MODE or
# RATE_LIMIT_COOLDOWN_MAX_SECONDS on Limits & Resilience. Both existed; both
# carried `advanced`, which rendered as `display: none` behind a "Show
# advanced" button, while CREDENTIAL_LOCKOUT_TIERS beside them did not -- so
# the card looked whole with two thirds of it missing. On the Providers page
# there was no button at all: `renderSections` skipped that section, and the
# per-card control it deferred to had never been written, so 110 provider
# fields were unreachable from the dashboard entirely.
#
# The static half of the contract is in
# `tests/contracts/test_no_setting_is_hidden_by_default.py`. What these add is
# the DOM: the field is really in the document on first paint, the tag is
# really on it, and the collapse a reader chooses really comes back.

COOLDOWN_FIELDS = ("RATE_LIMIT_COOLDOWN_MAX_SECONDS", "RATE_LIMIT_COOLDOWN_MODE")
SECTION_STORAGE_KEY = "mcc.advancedCollapsed.section:credential_health"


def test_jsdom_every_field_in_a_card_renders_without_a_click(rendered) -> None:
    """The bug, stated as a test: shown-on-load == every field the card owns."""

    section = rendered["advancedFields"]["section"]

    assert section is not None, "the Credential health card did not render"
    assert section["shownOnLoad"] == section["all"]
    for key in COOLDOWN_FIELDS:
        assert key in section["shownOnLoad"], (
            f"{key} is still not on the page until something is clicked -- "
            "which is exactly how it came to look like it did not exist"
        )


def test_jsdom_the_old_hiding_class_is_applied_nowhere(rendered) -> None:
    advanced = rendered["advancedFields"]
    assert advanced["showAdvancedInScript"] is False
    assert advanced["showAdvancedClassAnywhere"] == 0


def test_jsdom_advanced_fields_sort_after_the_common_ones(rendered) -> None:
    """`advanced` moves a field down the card. That is all it does to order."""

    order = rendered["advancedFields"]["section"]["all"]
    tagged = set(rendered["advancedFields"]["section"]["tagged"])

    assert tagged == set(COOLDOWN_FIELDS)
    first_advanced = min(order.index(key) for key in tagged)
    assert all(key in tagged for key in order[first_advanced:]), (
        f"a common field sits below an advanced one: {order}"
    )
    # ...and the common ones keep the order the manifest gave them.
    assert order[:first_advanced] == [key for key in order if key not in tagged]


def test_jsdom_an_advanced_field_says_it_is_advanced(rendered) -> None:
    assert set(rendered["advancedFields"]["section"]["tagged"]) == set(COOLDOWN_FIELDS)
    assert rendered["advancedFields"]["provider"]["tagged"] == ["HARNESS_PROXY"]


def test_jsdom_the_collapse_control_starts_expanded(rendered) -> None:
    section = rendered["advancedFields"]["section"]

    assert section["collapsedOnLoad"] is False
    assert section["storedOnLoad"] is None, (
        "a reader who never touched the control should leave no stored state"
    )
    assert section["toggleLabel"] == "Collapse advanced"


def test_jsdom_collapsing_hides_only_the_advanced_fields(rendered) -> None:
    section = rendered["advancedFields"]["section"]
    collapsed = rendered["advancedFields"]["afterCollapse"]

    assert collapsed["collapsed"] is True
    assert collapsed["label"] == "Show advanced"
    assert collapsed["shown"] == [
        key for key in section["all"] if key not in COOLDOWN_FIELDS
    ]


def test_jsdom_the_collapse_survives_a_reload_and_the_expand_clears_it(
    rendered,
) -> None:
    advanced = rendered["advancedFields"]
    collapsed = advanced["afterCollapse"]
    reloaded = advanced["afterReload"]
    expanded = advanced["afterExpand"]

    assert collapsed["stored"] == "1"
    # A re-render is what a reload is: same page, same stored choice.
    assert reloaded["collapsed"] is True
    assert reloaded["label"] == "Show advanced"
    assert reloaded["shown"] == collapsed["shown"]
    # The fields are still in the document even while collapsed -- they are
    # hidden, not dropped, or `changedValues()` would stop finding them.
    assert reloaded["all"] == advanced["section"]["all"]

    assert expanded["collapsed"] is False
    assert expanded["label"] == "Collapse advanced"
    assert expanded["shown"] == advanced["section"]["all"]
    assert expanded["stored"] is None, "expanded is the default, stored as absence"


def test_jsdom_a_provider_card_shows_its_advanced_fields_and_can_collapse_them(
    rendered,
) -> None:
    """The section that had no control at all now has one per card."""

    provider = rendered["advancedFields"]["provider"]

    assert provider is not None, "the provider card did not render"
    assert provider["shownOnLoad"] == provider["order"]
    assert "HARNESS_PROXY" in provider["shownOnLoad"]
    assert provider["order"][-1] == "HARNESS_PROXY", "the advanced field sorts last"
    assert provider["collapsedOnLoad"] is False
    assert provider["toggleLabel"] == "Collapse advanced"

    collapsed = rendered["advancedFields"]["providerAfterCollapse"]
    assert collapsed["collapsed"] is True
    assert collapsed["shown"] == ["HARNESS_API_KEY", "HARNESS_BASE_URL"]
    # Namespaced per card: collapsing one provider must not collapse 34 others.
    assert collapsed["stored"] == "1"


# --------------------------------------------------------------- the lock
# Two harness runs against one tree do not merely take twice as long: starved
# of CPU, each misses its own debounce windows, reads a page that has not
# caught up, and reports it. That produced roughly three hundred spurious
# "script error" lines the one time it happened. The second run now refuses.


def test_a_second_concurrent_harness_run_refuses_instead_of_cascading() -> None:
    """The lock is held here, so the harness must decline and say why."""

    node = shutil.which("node")
    if node is None:
        _missing("node is not on PATH")

    if LOCK.exists():
        pytest.skip("a harness run is already holding the lock")

    LOCK.write_text("0 held by the test\n", encoding="utf-8")
    try:
        result = subprocess.run(
            [node, str(HARNESS), str(STATIC_DIR)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            # If the guard is gone this runs the whole harness instead, which
            # is why the bound is the real one rather than a short one.
            timeout=TIMEOUT_SECONDS,
            env=os.environ,
        )
    finally:
        LOCK.unlink(missing_ok=True)

    if "Cannot find package 'jsdom'" in result.stderr:
        _missing("jsdom is not installed")

    assert result.returncode == 3, (
        f"the harness ran anyway: rc={result.returncode} stderr={result.stderr[-500:]}"
    )
    assert "already holding" in result.stderr
    assert "MCC_JSDOM_ALLOW_CONCURRENT" in result.stderr
    # It must not have produced a payload a caller could mistake for a run.
    assert result.stdout.strip() == ""


def test_the_lock_is_released_so_the_next_run_is_not_blocked(rendered) -> None:
    """A lock left behind would fail every later run on the machine.

    `rendered` is requested so this runs after a real harness run rather than
    before one.
    """

    assert rendered["fatal"] is None
    assert not LOCK.exists(), f"{LOCK} survived the run that took it"


def test_the_loop_lag_readout_states_what_each_gesture_cost(rendered) -> None:
    """The follow-up table, read off the dashboard instead of a harness.

    Two gestures, newest first, each with the worst event-loop lateness
    measured while it ran -- and the one over the busy threshold marked as
    such, because that is the one a ``/health`` probe would have been told
    about.
    """

    readout = rendered["limits"]["loopLag"]
    assert readout["present"] is True
    rows = readout["rows"]
    assert [row["reason"] for row in rows] == [
        "the model catalogue is being refreshed",
        "a route is being paused",
    ]
    assert [row["lag"] for row in rows] == ["2755 ms", "84 ms"]
    assert [row["took"] for row in rows] == ["4700 ms", "121 ms"]
    assert [row["overBudget"] for row in rows] == [True, False]


# ------------------------------------------- OpenCode Zen free-tier credential
# 7.34.0: free Zen models go out on OpenCode's shared `public` credential, so
# the operator's own key stops paying for them. That is a decision about whose
# allowance is spent, so it has to be visible on the card whose key it is about
# -- not in a release note and not only in `.env`.


def test_the_opencode_card_offers_the_free_tier_credential_choice(rendered) -> None:
    control = rendered["fields"]["opencodeCredential"]

    assert control is not None, "the toggle must render on the OpenCode Zen card"
    assert control["tag"] == "select"
    assert control["cardHasApiKey"], "the switch belongs beside the key it spends"
    # Never chosen is its own state: an untouched form saves nothing.
    assert control["value"] == ""
    assert control["original"] == ""
    assert control["optionValues"] == ["", "public", "key"]
    # The dashboard writes "Default (<the default option's own label>)", so
    # what proves the default is `public` is that option's wording, not the
    # bare word -- the labels are written for an operator, not for a parser.
    assert control["optionLabels"][0].startswith("Default (")
    assert "shared anonymous credential" in control["optionLabels"][0]
    assert control["optionLabels"][2] == "Use my own key for everything"


def test_the_opencode_credential_toggle_explains_the_shared_bucket(rendered) -> None:
    """The 7.29.1 contract, and the one caveat that cannot be left implicit."""

    help_text = rendered["fields"]["opencodeCredential"]["help"]

    assert "metered per credential" in help_text
    assert "shared anonymous credential" in help_text
    assert "your own key's free allowance is not spent" in help_text
    assert "Paid Zen models always use your key" in help_text


# ------------------------------------------------- cancelled sub-labels
#
# "Cancelled" means the client stopped reading before the stream finished, and
# that one word covered four unrelated events. Every surface below is driven
# through the real renderer, so a change to the words in one place and not the
# other fails here.


def test_jsdom_a_cancelled_row_says_which_kind_of_cancelled(rendered) -> None:
    rows = rendered["cancelledViews"]["rows"]

    assert [row["chip"] for row in rows] == [
        "client gave up waiting",
        "committed, then silent",
        "stopped mid-answer",
        "server restart",
        # A successful request carries no chip at all, not an empty one.
        None,
    ]
    # The status word itself is unchanged and still in its own cell, so the
    # colour rules that key on it keep describing the status.
    assert [row["status"] for row in rows] == [
        "cancelled",
        "cancelled",
        "cancelled",
        "cancelled",
        "success",
    ]
    assert [row["reason"] for row in rows[:4]] == [
        "client_gave_up_waiting",
        "committed_then_silent",
        "stopped_mid_answer",
        "server_restart",
    ]


def test_jsdom_the_chip_carries_its_explanation_in_the_tooltip(rendered) -> None:
    rows = rendered["cancelledViews"]["rows"]

    assert "Nothing the client could read ever arrived" in rows[0]["title"]
    assert "MCC had sent the start of the message" in rows[1]["title"]
    assert "Part of the answer had already been delivered" in rows[2]["title"]
    assert "MCC stopped or restarted" in rows[3]["title"]


def test_jsdom_the_modal_says_it_in_a_sentence_with_the_silence_measured(
    rendered,
) -> None:
    """The one number the label is about: how long nothing arrived."""

    views = rendered["cancelledViews"]

    assert views["modalChip"] == "committed, then silent"
    assert "MCC had sent the start of the message" in views["modalSentence"]
    # 313.4 s - 16.7 s = 296.7 s of silence after the first frame.
    assert (
        "Nothing arrived for 4m 57s after that first frame." in (views["modalSentence"])
    )
    assert ["Cancelled because", None] not in views["detailPairs"]
    assert any(label == "Cancelled because" for label, _ in views["detailPairs"])


def test_jsdom_an_interrupted_attempt_is_badged_as_the_client_hanging_up(
    rendered,
) -> None:
    """Stored as `failed`; what ended it was the other end of the connection."""

    views = rendered["cancelledViews"]

    assert views["chainOutcomes"] == ["failed", "client hung up"]
    assert "is-failed" in views["chainClasses"][0]
    assert "is-interrupted" in views["chainClasses"][1]


def test_jsdom_the_cancelled_card_breaks_down_into_four(rendered) -> None:
    breakdown = rendered["cancelledViews"]["breakdown"]

    assert breakdown["headers"] == ["Why", "Requests", "Share", "What it means"]
    assert [row[0] for row in breakdown["rows"]] == [
        "client gave up waiting",
        "committed, then silent",
        "stopped mid-answer",
        "server restart",
    ]
    assert [row[1] for row in breakdown["rows"]] == ["189", "78", "608", "60"]
    assert [row[2] for row in breakdown["rows"]] == [
        "20.2%",
        "8.3%",
        "65.0%",
        "6.4%",
    ]
    # Every row carries the sentence, so the panel explains itself.
    assert all(len(row[3]) > 40 for row in breakdown["rows"])


def test_jsdom_a_zero_row_stays_rather_than_vanishing(rendered) -> None:
    """ "None of these were restarts" is an answer; a missing row is not."""

    empty = rendered["cancelledViews"]["emptyBreakdown"]

    assert len(empty["rows"]) == 4
    assert [row[1] for row in empty["rows"]] == ["0", "0", "0", "0"]
    # No denominator, so no share is claimed.
    assert [row[2] for row in empty["rows"]] == ["—"] * 4
    # The log being off is a different answer from nothing being cancelled.
    assert rendered["cancelledViews"]["disabledBreakdown"]["rows"] == [
        ["Nothing was cancelled in this range."]
    ]


def test_jsdom_the_cancelled_card_carries_the_one_line_version(rendered) -> None:
    card = rendered["cancelledViews"]["cancelledCard"]

    assert card["value"] == "9"
    assert card["note"] == (
        "client gave up waiting 4 · committed, then silent 1"
        " · stopped mid-answer 3 · server restart 1"
    )


def test_jsdom_the_latency_panel_keeps_hang_ups_out_of_the_failed_row(
    rendered,
) -> None:
    """The third group, beside the other two on the same model."""

    rows = rendered["cancelledViews"]["latencyPanel"]

    assert [row[1] for row in rows] == ["answered", "failed", "client hung up"]
    # The failed row is the three genuine failures, not the five attempts that
    # were `outcome='failed'` in the table.
    failed = rows[1]
    hung_up = rows[2]
    assert failed[2] == "3"
    assert failed[5] == "9.0s"
    assert hung_up[2] == "2"
    assert hung_up[5] == "10m 0s"
    # Shares are of the model's own attempts, and the new group takes its own.
    assert [row[3] for row in rows] == ["66.7%", "20.0%", "13.3%"]


# --------------------------------------------------------- tool catalogue (7.40.0)


def test_the_modal_shows_the_tool_catalogue_hash_count_and_names(rendered) -> None:
    """The hash (short, full on hover), the count, and the names behind an
    expander that starts closed -- a Claude Code session carries 200+ tools."""

    tools = rendered["toolCatalogue"]
    assert tools["present"] is True
    assert tools["sha"] == "ab12" * 4
    assert tools["shaTitle"].endswith("ab12" * 16)
    assert tools["count"] == "3 tools"
    assert tools["collapsedByDefault"] is True
    assert tools["summary"] == "Show 3 tool names"
    assert tools["names"] == [
        "Bash",
        "mcp__appium-mcp__appium_screen_recording",
        "Artifact",
    ]
    assert tools["nameTitles"][1] == "Definition SHA-256: " + "0b" * 32
    assert tools["notes"][0].startswith("This exact array was sent with 216 requests")


def test_choosing_a_tool_name_lists_the_requests_that_carried_it(rendered) -> None:
    tools = rendered["toolCatalogue"]
    assert tools["carriersHiddenBeforeClick"] is True
    assert tools["carrierUrls"] == [
        "/admin/api/tool-catalogues/requests"
        "?name=mcp__appium-mcp__appium_screen_recording&limit=25"
    ]
    assert tools["carriersHidden"] is False
    assert tools["carriersHeading"] == (
        "Requests that carried mcp__appium-mcp__appium_screen_recording"
    )
    assert [row["title"] for row in tools["carrierRows"]] == ["req-tools", "req-older"]
    assert tools["carrierRows"][0]["text"].endswith("gpt-5.6-sol · success")
    # No resolved model: the requested one is named rather than a blank.
    assert tools["carrierRows"][1]["text"].endswith("claude-opus-4 · error")
    assert tools["carrierNotes"][0].startswith(
        "216 requests · 2 tools arrays · 1 definition · "
    )
    assert tools["carrierNotes"][1] == "Showing the newest 2."
    # A listed request opens its own detail.
    assert tools["carrierOpens"] == ["/admin/api/requests/req-tools"]


def test_a_request_older_than_the_recording_says_so(rendered) -> None:
    """Carried tools, no hash: "not recorded", never an empty row that would
    read as "no tools"."""

    tools = rendered["toolCatalogue"]
    assert tools["legacyText"] == (
        "212 tools, not recorded"
        "Which tools a request carried is recorded from 7.40.0 on;"
        " this request is older."
    )
    assert tools["legacyHasNames"] == 0


def test_a_request_without_tools_has_no_catalogue_row(rendered) -> None:
    assert rendered["toolCatalogue"]["noToolsRow"] is False


def test_the_export_window_offers_the_catalogue_column_opt_in() -> None:
    """Offered, and not among the defaults: an export that does not ask for it
    is byte-identical to one made before 7.40.0."""

    source = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
    requests_fields = source.split("const EXPORT_FIELDS = {", 1)[1].split("],", 1)[0]
    assert '{ id: "tool_catalogue", label: "Tool catalogue" }' in requests_fields
    defaults = source.split("const EXPORT_DEFAULT_FIELDS = {", 1)[1].split("]),", 1)[0]
    assert "tool_catalogue" not in defaults


# ---------------------------------------------------------------------------
# Request origin (7.42.0): Session, Folder, Requested model, and the backfill
# ---------------------------------------------------------------------------

# The thirteen columns the Requests table had before 7.42.0. Additions never
# remove one, and never reorder them.
EXISTING_REQUEST_COLUMNS = [
    "Time",
    "Endpoint",
    "Harness",
    "Provider",
    "Key",
    "Model",
    "Status",
    "Turn",
    "Tokens",
    "Cost",
    "TTFT",
    "Duration",
    "Details",
]


def test_requests_table_keeps_all_existing_columns(rendered) -> None:
    """All thirteen, still in their order, with nothing between them removed."""

    headers = rendered["harnessAttr"]["headers"]
    positions = [headers.index(name) for name in EXISTING_REQUEST_COLUMNS]
    assert positions == sorted(positions)
    assert len(headers) == len(EXISTING_REQUEST_COLUMNS) + 4


def test_requests_table_adds_three_columns(rendered) -> None:
    """Session, Folder, Requested model -- plus the Origin chip that stands in
    for the first two below 1200 px (CSS decides which is shown)."""

    classes = dict(rendered["requestOrigin"]["headerClasses"])
    added = [name for name in classes if name not in EXISTING_REQUEST_COLUMNS]
    assert added == ["Session", "Folder", "Origin", "Requested model"]
    assert classes["Session"] == "req-col-session"
    assert classes["Folder"] == "req-col-folder"
    assert classes["Origin"] == "req-col-origin"
    assert classes["Requested model"] == "req-col-requested-model"


def test_every_row_has_one_cell_per_header_in_header_order(rendered) -> None:
    origin = rendered["requestOrigin"]
    width = len(origin["headerClasses"])

    assert origin["cellCounts"] == [width, width]
    by_header = dict(origin["withOrigin"]["cellClassesByHeader"])
    assert by_header["Session"] == "req-col-session"
    assert by_header["Folder"] == "req-col-folder"
    assert by_header["Origin"] == "req-col-origin"
    assert by_header["Requested model"] == "req-col-requested-model"
    assert by_header["Status"] == "req-col-status"


def test_the_origin_cells_show_short_forms_with_the_full_value_in_the_tooltip(
    rendered,
) -> None:
    row = rendered["requestOrigin"]["withOrigin"]

    assert row["session"] == "0f3c2a1bsubagent"
    assert row["subagentBadge"] is True
    assert "Session: 0f3c2a1b-6d5e-4f70-9a8b-1c2d3e4f5a6b" in row["sessionTitle"]
    assert row["folder"] == "Projects\\demo · #3f9a21"
    assert row["folderTitle"] == "C:\\Users\\devuser\\Projects\\demo"
    assert row["chip"] == "Projects\\demo · 0f3c2a1b"
    assert "Folder: C:\\Users\\devuser\\Projects\\demo" in row["chipTitle"]
    assert row["requested"] == "claude-opus-4"


def test_a_row_with_no_origin_draws_dashes_not_none(rendered) -> None:
    row = rendered["requestOrigin"]["bare"]

    assert (row["session"], row["folder"], row["chip"], row["requested"]) == (
        "—",
        "—",
        "—",
        "—",
    )
    assert row["subagentBadge"] is False


def test_the_status_colour_follows_the_status_cell_not_a_position(rendered) -> None:
    """``td:nth-child(6)`` had silently pointed at Model since the Harness
    column was added; the rule now keys on the Status cell's own class."""

    row = rendered["requestOrigin"]["withOrigin"]
    assert row["statusClass"] is True
    assert row["statusText"] == "error"
    css = (STATIC_DIR / "admin.css").read_text(encoding="utf-8")
    assert ".req-status-error td.req-col-status" in css
    assert ".req-status-cancelled td.req-col-status" in css
    assert "td:nth-child(6)" not in css


def test_the_chip_replaces_session_and_folder_below_1200px() -> None:
    css = (STATIC_DIR / "admin.css").read_text(encoding="utf-8")
    narrow = css.split("@media (max-width: 1199px) {", 1)[1].split("\n}\n", 1)[0]

    assert ".requests-table .req-col-session" in narrow
    assert ".requests-table .req-col-folder" in narrow
    assert "display: none;" in narrow
    assert ".requests-table .req-col-origin {\n    display: table-cell;" in narrow
    assert ".requests-table .req-col-origin {\n  display: none;\n}" in css


def test_the_modal_shows_origin_with_its_source(rendered) -> None:
    detail = dict(rendered["requestOrigin"]["detail"])

    assert detail["Session"] == (
        "0f3c2a1b-6d5e-4f70-9a8b-1c2d3e4f5a6b"
        " (stated by the x-claude-code-session-id header)"
    )
    assert detail["Subagent"] == (
        "a7b8c9d0-1e2f-4a3b-8c4d-5e6f7a8b9c0d"
        " (stated by the x-claude-code-agent-id header)"
    )
    assert detail["Folder"] == (
        "C:\\Users\\devuser\\Projects\\demo (read from the prompt's environment block)"
    )
    labels = [label for label, _ in rendered["requestOrigin"]["detail"]]
    assert labels.index("Harness") < labels.index("Session") < labels.index("Protocol")


def test_the_modal_omits_origin_lines_it_has_no_value_for(rendered) -> None:
    labels = rendered["requestOrigin"]["bareDetailLabels"]

    for label in ("Session", "Subagent", "Parent session", "Folder"):
        assert label not in labels
    assert "Harness" in labels


def test_the_backfill_is_a_button_on_the_request_log_card(rendered) -> None:
    origin = rendered["requestOrigin"]

    assert origin["cardPresent"] is True
    assert origin["cardInRequestLogSection"] is True
    assert origin["buttonText"] == "Fill in folders for older requests"
    assert origin["posted"] == ["POST"]
    assert origin["runningText"] == (
        "Filling in folders… 1,500 older rows read, 1,421 folders found so far."
    )
    assert origin["buttonDisabledWhileRunning"] is True
    assert origin["doneText"] == "Done: 1,500 older rows read, 1,421 folders filled in."
    assert "REQUEST_LOG_CAPTURE_FOLDER=false" in origin["offText"]
    assert origin["buttonDisabledWhenOff"] is True


def test_the_export_window_offers_the_origin_group_opt_in() -> None:
    source = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
    requests_fields = source.split("const EXPORT_FIELDS = {", 1)[1].split("],", 1)[0]
    assert '{ id: "origin", label: "Request origin" }' in requests_fields
    defaults = source.split("const EXPORT_DEFAULT_FIELDS = {", 1)[1].split("]),", 1)[0]
    assert "origin" not in defaults


# ------------------------------------------------- origin filters (7.43.0)

DEMO_PATH = "C:\\Users\\devuser\\Projects\\demo"
DEMO_QUERY = "folder=C%3A%5CUsers%5Cdevuser%5CProjects%5Cdemo"
SESSION_ID = "0f3c2a1b-6d5e-4f70-9a8b-1c2d3e4f5a6b"


def test_origin_filters_autoapply_from_offset_zero(rendered) -> None:
    """Session and Folder follow every other text box: debounced, one load
    after the pause, from page 1, and every panel asks the same question."""

    origin = rendered["originFilters"]
    for name, value in (
        ("session", "session=0f3c2a1b"),
        ("folder", "folder=Phone+games"),
    ):
        typed = origin[name]
        assert "offset=25" in typed["pagedUrl"]
        assert typed["loadsWhileTyping"] == 0
        assert typed["loadsAfterPause"] == 1
        assert "offset=0" in typed["listUrl"]
        for url in ("listUrl", "statsUrl", "originUrl", "costUrl", "ttftUrl"):
            assert value in typed[url], (name, url, typed[url])


def test_origin_filters_persist_across_reload(rendered) -> None:
    origin = rendered["originFilters"]

    assert origin["session"]["persisted"] == "0f3c2a1b"
    assert origin["folder"]["persisted"] == "Phone games"
    assert origin["restored"] == {"session": SESSION_ID, "folder": DEMO_PATH}


def test_a_breakdown_row_filters_the_page_to_it(rendered) -> None:
    """The full value goes into the box -- an exact folder, one session id --
    and the page reloads from page 1."""

    origin = rendered["originFilters"]

    assert origin["clickedFolderValue"] == DEMO_PATH
    assert DEMO_QUERY in origin["clickListUrl"]
    assert "offset=0" in origin["clickListUrl"]
    assert origin["clickPersisted"] == DEMO_PATH
    assert origin["clickedSessionValue"] == SESSION_ID


def test_the_export_carries_the_origin_filters(rendered) -> None:
    url = rendered["originFilters"]["exportUrl"]

    assert f"session={SESSION_ID}" in url
    assert DEMO_QUERY in url


def test_clear_filters_empties_and_forgets_both_origin_boxes(rendered) -> None:
    origin = rendered["originFilters"]

    assert origin["cleared"] == ["", ""]
    assert "session=" not in origin["clearedListUrl"]
    assert "folder=" not in origin["clearedListUrl"]
    assert "session" not in origin["clearedPersisted"]
    assert "folder" not in origin["clearedPersisted"]


def test_an_unset_origin_filter_leaves_the_query_string_alone(rendered) -> None:
    """Unset means absent, so every URL -- and so every cache key and the
    pulse signature -- is the one 7.42.0 sent."""

    url = rendered["originFilters"]["unfilteredOriginUrl"]

    assert url.startswith("/admin/api/requests/origin?")
    assert "session=" not in url
    assert "folder=" not in url


def test_requests_by_folder_and_by_session_render_the_payload(rendered) -> None:
    origin = rendered["originFilters"]

    assert origin["folderHeaders"] == [
        "Folder",
        "Requests",
        "Sessions",
        "Error rate",
        "Tokens in",
        "Tokens out",
        "Last seen",
    ]
    assert [row[:4] for row in origin["folderRows"]] == [
        ["Projects\\demo \u00b7 #76b11b", "9", "2", "11.1%"],
        ["games\\Phone games \u00b7 #8fa073", "3", "1", "0.0%"],
    ]
    assert origin["folderButtons"] == 2
    assert origin["folderTitle"] == f"{DEMO_PATH}\nShow only this folder"
    assert origin["sessionHeaders"][:5] == [
        "Session",
        "Folder",
        "Requests",
        "By subagents",
        "Subagents",
    ]
    assert [row[:5] for row in origin["sessionRows"]] == [
        ["0f3c2a1b", "Projects\\demo \u00b7 #76b11b +1", "9", "5", "2"],
        ["c4d5e6f7", "\u2014", "3", "0", "0"],
    ]
    assert origin["folderOptions"][0] == [DEMO_PATH, "Projects\\demo \u00b7 #76b11b"]
    assert origin["sessionOptions"][0] == [
        SESSION_ID,
        "0f3c2a1b \u00b7 Projects\\demo \u00b7 #76b11b",
    ]


def test_sessions_say_they_are_flat_and_why(rendered) -> None:
    """The parent/child signal was not confirmed on real traffic, so the
    panel must not look like it grouped subagents under a parent."""

    origin = rendered["originFilters"]

    assert origin["sessionNote"].startswith("Listed flat, one row per session id.")
    assert "not grouped under a parent session" in origin["sessionNote"]
    assert "Showing the 50 busiest sessions" in origin["sessionNote"]
    assert (
        "Only requests whose agent named its working folder" in (origin["folderNote"])
    )


def test_a_failed_origin_breakdown_stays_in_its_panel(rendered) -> None:
    origin = rendered["originFilters"]

    assert origin["errorNote"] == "Could not count folders: boom"
    assert origin["disabledRows"] == 0


# ----------------------------------------------------------- in flight (7.45.0)
# The panel above the Requests table, driven through the emulated registry in
# the harness (`INFLIGHT`), which honours `limit` the way the route does.
REQUESTS_TABLE_HEADERS = [
    "Time",
    "Endpoint",
    "Harness",
    "Session",
    "Folder",
    "Origin",
    "Provider",
    "Key",
    "Requested model",
    "Model",
    "Status",
    "Turn",
    "Tokens",
    "Cost",
    "TTFT",
    "Duration",
    "Details",
]


def test_inflight_panel_empty_state(rendered) -> None:
    empty = rendered["inflight"]["empty"]

    assert empty["emptyHidden"] is False
    assert empty["tableHidden"] is True
    assert empty["emptyText"] == (
        "Nothing in flight. Requests appear here the moment they arrive and move "
        "to the table below when they finish."
    )
    assert empty["status"] == "Nothing in flight"
    # No count, no badge: the link reads exactly as it did before 7.45.0.
    assert empty["badgeHidden"] is True
    assert empty["navLabel"] == "Analytics"


def test_inflight_panel_sorts_by_age(rendered) -> None:
    """Served newest first on purpose: the panel orders oldest first itself."""

    five = rendered["inflight"]["five"]

    assert five["order"] == ["req_0001", "req_0002", "req_0003", "req_0004", "req_0005"]
    assert five["status"] == "5 requests in flight"
    assert five["oldest"].endswith("oldest 1h 06m")
    assert five["emptyHidden"] is True


def test_inflight_panel_columns_and_origin_forms(rendered) -> None:
    """Session and folder in the O1/O2 display forms, the masked key and proxy
    as the server sent them, and "not measured" never drawn as zero."""

    five = rendered["inflight"]["five"]
    cells = five["cells"]

    assert five["headers"] == [
        "Age",
        "Phase",
        "Harness",
        "Session",
        "Folder",
        "Origin",
        "Requested model",
        "Provider / model",
        "Attempt",
        "Streamed",
        "Key / proxy",
        "Details",
    ]
    first, second, third, fourth, fifth = cells
    assert first[3] == "0f3c2a1b"
    assert first[4] == "Projects\\demo · #76b11b"
    assert first[5] == "Projects\\demo · 0f3c2a1b"
    assert first[8] == "Fallback 12 tries, last 429 rate_limit"
    assert second[8] == "Primary"
    assert second[10] == "sk-8…Kofxproxy-3…a1"
    assert third[9] == "1,234 chars + 56 reasoning"
    # A Claude Code folder is only read at finalize: pending, not a guess.
    assert fourth[4] == "pending"
    assert fourth[3] == "0f3c2a1bsubagent"
    assert fourth[7] == "not routed yet"
    assert fourth[8] == "—"
    # Log off: streamed text is not counted, and the row says so.
    assert fifth[9] == "not observed"
    assert fifth[3] == fifth[4] == fifth[5] == "—"
    assert all(row[11] == "Live" for row in cells)


def test_inflight_panel_stuck_is_the_300s_client_watchdog_floor(rendered) -> None:
    five = rendered["inflight"]["five"]

    # 301 s in phase is stuck; 299 s is not; 400 s of streaming with a chunk
    # 2 s ago is not either -- the floor is an idle deadline.
    assert five["stuck"] == [True, False, False, False, False]
    assert five["stuckBadgeVisible"] == [True, False, False, False, False]
    assert "more than 300 s" in five["stuckTitle"]
    assert "watchdog floor" in five["stuckTitle"]
    stuck = rendered["inflight"]["many"]["stuckOnPage1"]
    assert [row_id for row_id, _ in stuck] == ["req_0001", "req_0002"]
    assert stuck[0][1].startswith("In this phase for more than 300 s.")
    assert stuck[1][1].startswith("No new chunk for more than 300 s.")


def test_inflight_panel_derives_backing_off_from_two_snapshots(rendered) -> None:
    """One reading never names it; the second does, from `waited_s` alone."""

    five = rendered["inflight"]["five"]

    assert five["firstSnapshotChips"]["req_0001"] == "Attempt started"
    assert five["firstSnapshotChips"]["req_0002"] == "Attempt started"
    assert five["secondSnapshotChips"] == {
        "req_0001": "Backing off",
        "req_0002": "Waiting for upstream",
        "req_0003": "Streaming",
        "req_0004": "Routing",
        "req_0005": "Streaming",
    }
    assert "from 2.0 s to 6.5 s" in five["backingOffTitle"]


def test_inflight_panel_timers_tick_between_refreshes_and_stop_when_off(
    rendered,
) -> None:
    five = rendered["inflight"]["five"]

    assert five["ageBefore"] != five["ageAfter"]
    assert five["ageFrozenWhenOff"] is True


def test_inflight_panel_single_live_region(rendered) -> None:
    """One polite status line, never the rows, and silent while the count
    holds -- a live region over a 50-row table would read it every tick."""

    regions = rendered["inflight"]["liveRegions"]

    assert regions["inPanel"] == 1
    assert regions["inRows"] == 0
    assert regions["statusRole"] == "status"
    assert regions["statusLive"] == "polite"
    assert (
        regions["caption"] == "Requests this server is serving right now, oldest first"
    )
    assert rendered["inflight"]["five"]["statusMutationsOnSameCount"] == 0


def test_inflight_panel_finishing_row_survives_one_tick(rendered) -> None:
    finishing = rendered["inflight"]["finishing"]

    assert finishing["ids"] == [
        "req_0001",
        "req_0002",
        "req_0003",
        "req_0004",
        "req_0005",
    ]
    assert finishing["finishingIds"] == ["req_0003"]
    assert finishing["chip"] == "Finished"
    assert finishing["status"] == "4 requests in flight"
    assert finishing["idsAfterOneMoreTick"] == [
        "req_0001",
        "req_0002",
        "req_0004",
        "req_0005",
    ]
    # The open detail says what happened and hands over to the finished record.
    assert finishing["detailState"].startswith("Finished.")
    assert finishing["openFinishedVisible"] is True

    drain = rendered["inflight"]["drain"]
    assert drain["finishingRows"] == 4
    assert drain["emptyHidden"] is True
    assert drain["rowsAfter"] == 0
    assert drain["emptyHiddenAfter"] is False
    assert drain["badgeHidden"] is True


def test_inflight_panel_caps_at_fifty_with_overflow_note(rendered) -> None:
    many = rendered["inflight"]["many"]

    page1, page2, page3 = many["page1"], many["page2"], many["page3"]
    assert many["status"] == "120 requests in flight"
    assert page1["rows"] == 50
    assert (page1["first"], page1["last"]) == ("req_0001", "req_0050")
    assert page1["note"] == "…and 70 more — showing the 50 oldest."
    assert page1["info"] == "1\N{EN DASH}50 of 120, oldest first"
    assert page1["url"].endswith("?limit=50")
    assert page1["prevDisabled"] is True
    assert page1["nextDisabled"] is False
    assert (page2["rows"], page2["first"], page2["last"]) == (
        50,
        "req_0051",
        "req_0100",
    )
    assert page2["url"].endswith("?limit=100")
    assert (page3["rows"], page3["first"], page3["last"]) == (
        20,
        "req_0101",
        "req_0120",
    )
    assert page3["nextDisabled"] is True
    assert many["backToPage1"]["first"] == "req_0001"
    # The DOM budget: never more than one page of rows.
    assert max(page1["rows"], page2["rows"], page3["rows"]) <= 50


def test_inflight_panel_live_detail_and_keyboard_path(rendered) -> None:
    detail = rendered["inflight"]["detail"]

    assert detail["open"] is True
    assert detail["title"] == "In flight req_0002"
    assert detail["focusOnClose"] is True
    assert "PhaseWaiting for upstream" in detail["text"]
    assert "Proxyproxy-3…a1" in detail["text"]
    assert detail["attempts"] == [
        "Primary — in progress: waiting for upstream on opencode/qwen3-coder, "
        "0 tries finished on it."
    ]
    # Live: the next refresh moves the open detail with it.
    assert "PhaseAwaiting content" in detail["afterRefresh"]
    assert detail["closedByEscape"] is True
    assert detail["focusReturned"] == "req_0002"
    assert detail["rowClickOpens"] == "In flight req_0003"


def test_inflight_panel_does_not_touch_requests_table(rendered) -> None:
    inflight = rendered["inflight"]

    assert inflight["requestsTableUnchanged"] is True
    assert inflight["requestsHeadersBefore"] == REQUESTS_TABLE_HEADERS
    assert inflight["requestsHeadersAfter"] == REQUESTS_TABLE_HEADERS


def test_inflight_panel_has_its_own_refresh_control(rendered) -> None:
    """Default 3 s, independent of the table's auto-refresh, persisted."""

    inflight = rendered["inflight"]

    assert inflight["defaultInterval"] == "3000"
    assert inflight["intervalOptions"] == [
        "0",
        "1000",
        "3000",
        "5000",
        "10000",
        "30000",
    ]
    assert inflight["persistedInterval"] == "0"


def test_inflight_panel_collapses_to_one_line(rendered) -> None:
    collapsed = rendered["inflight"]["collapsed"]

    assert collapsed["bodyHidden"] is True
    assert collapsed["expanded"] == "false"
    assert collapsed["url"].endswith("?limit=1")
    assert collapsed["status"] == "120 requests in flight"
    assert collapsed["oldest"].endswith("oldest 1h 06m")
    assert collapsed["persisted"] is True
    assert collapsed["reopenedUrl"].endswith("?limit=50")


def test_inflight_panel_says_when_the_view_is_off(rendered) -> None:
    off = rendered["inflight"]["off"]

    assert off["status"] == "In-flight list is off"
    assert "REQUEST_INFLIGHT_ENABLED" in off["note"]
    assert off["tableHidden"] is True
    assert off["emptyHidden"] is True
    assert off["badgeHidden"] is True


def test_inflight_count_in_the_sidebar_on_every_page(rendered) -> None:
    inflight = rendered["inflight"]

    assert inflight["five"]["badge"] == "5 in flight"
    assert inflight["five"]["badgeHidden"] is False
    # Off Analytics the badge still counts, asking for a single row.
    assert inflight["otherPage"]["url"].endswith("?limit=1")
    assert inflight["otherPage"]["badge"] == "5 in flight"
    assert inflight["otherPage"]["badgeTitle"] == "5 requests in flight"
    # The run ends with nothing in flight, and the label is back to itself.
    labels = rendered["navLabels"]
    assert "Analytics" in labels


def test_inflight_panel_css_keeps_the_origin_switch_and_its_own_table() -> None:
    """Its own table class, so `.requests-table` selectors (the request log's
    header reader among them) can never pick the panel up."""

    css = (STATIC_DIR / "admin.css").read_text(encoding="utf-8")
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert '<table class="inflight-table">' in html
    assert html.index('id="reqInflightPanel"') < html.index('class="requests-table"')
    assert ".inflight-table .req-col-origin {\n  display: none;\n}" in css
    narrow = css[css.index("@media (max-width: 1199px) {\n  .inflight-table") :]
    assert ".inflight-table .req-col-origin {\n    display: table-cell;" in narrow
    assert ".inflight-panel [hidden]" in css


# ------------------------------------------------------------ proxy dials --


def test_every_proxy_dial_renders_as_a_row_under_its_attempt(rendered) -> None:
    """Three dials on the first attempt, one on the fallback, in order."""

    detail = rendered["requestDetail"]["proxyDials"]

    assert detail["chainHidden"] is False
    assert detail["dialTitles"] == ["3 proxy dials · 2 switches", "1 proxy dial"]
    assert detail["dials"] == [
        "#1 · 173.249.24.121:1080 · connect 42 ms · handshake 310 ms"
        " · 429 after 3.8s · switched in 12 ms",
        "#2 · 45.77.244.108:1080 · connect 88 ms · ConnectTimeout after 10.0s"
        " · switched in 3 ms",
        "#3 · Direct (no proxy) · never completed — open 47m 0s",
        "#1 · 10.0.0.9:3128 · answered in 900 ms",
    ]
    assert detail["dialClasses"] == [
        "req-chain-dial is-switched",
        "req-chain-dial is-switched",
        "req-chain-dial is-dialing",
        "req-chain-dial is-answered",
    ]


def test_the_tries_are_drawn_exactly_as_before_beside_the_dials(rendered) -> None:
    detail = rendered["requestDetail"]["proxyDials"]
    assert len(detail["ladderTries"]) == 2
    assert detail["ladderSummaries"][0].startswith("2 tries")


def test_one_dial_is_enough_to_show_the_panel(rendered) -> None:
    """One try hides nothing -- but where it went, and that it never moved on,
    is said nowhere else."""

    detail = rendered["requestDetail"]["proxyOneDial"]
    assert detail["chainHidden"] is False
    assert detail["ladderTries"] == []
    assert detail["dials"] == [
        "#1 · 173.249.24.121:1080 · 429 after 3.8s · then 47m 0s without a switch"
    ]
    assert detail["dialClasses"] == ["req-chain-dial is-failed"]


def test_an_attempt_with_no_chain_draws_no_dial_rows(rendered) -> None:
    for name in ("ladder", "singleTry", "singleTryWithProbe"):
        detail = rendered["requestDetail"][name]
        assert detail["dials"] == []
        assert detail["dialTitles"] == []


# ---------------------------------------------------------------------------
# Stream keepalive (7.46.0): the card's "which clock fires first" warning. It
# appears when a watched deadline is 0 (no limit) or longer than Claude Code's
# 300 s idle floor, hides otherwise, and never proposes a value.


def test_the_watchdog_card_lives_in_the_keepalive_section(rendered) -> None:
    watchdog = rendered["limits"]["watchdog"]["loaded"]
    assert watchdog["present"] is True
    assert watchdog["inKeepaliveSection"] is True


def test_the_watchdog_card_is_hidden_while_every_deadline_fits(rendered) -> None:
    """120 / 120 / 300: nothing outlasts a 300 s client floor."""
    assert rendered["limits"]["watchdog"]["loaded"]["hidden"] is True


def test_the_watchdog_card_appears_when_a_deadline_is_no_limit(rendered) -> None:
    shown = rendered["limits"]["watchdog"]["stallZero"]
    assert shown["hidden"] is False
    assert shown["lead"].startswith("Warning:")
    assert "300 s" in shown["lead"]
    assert shown["items"] == ["FALLBACK_STALL_TIMEOUT: 0 (no limit)"]


def test_the_watchdog_card_hides_again_at_120(rendered) -> None:
    assert rendered["limits"]["watchdog"]["stallBackTo120"]["hidden"] is True


def test_the_watchdog_card_says_plainly_what_keepalive_does_for_claude_code(
    rendered,
) -> None:
    on = rendered["limits"]["watchdog"]["stallZero"]["keepalive"]
    assert "drops ping" in on
    assert "_CLAUDE_CODE_ASSUME_FIRST_PARTY_BASE_URL=1" in on
    assert "after 30 s" in on and "300 s" in on
    off = rendered["limits"]["watchdog"]["keepaliveOff"]["keepalive"]
    assert off.startswith("Stream keepalive is off.")


def test_the_watchdog_card_follows_the_keepalive_mode(rendered) -> None:
    """7.47.0: in frames mode the Claude Code line says what frames do, and
    where they do not: only inside the model's own open block."""
    ping = rendered["limits"]["watchdog"]["stallZero"]["keepalive"]
    assert "Keepalive mode is frames" in ping
    frames = rendered["limits"]["watchdog"]["framesMode"]["keepalive"]
    assert "In frames mode it does move Claude Code's timer" in frames
    assert "text or tool-call block is open" in frames
    assert "while the model is thinking it is still a ping" in frames
    assert "changes nothing" in frames


def test_the_request_detail_shows_keepalive_frames_only_when_recorded(
    rendered,
) -> None:
    harness = rendered["harnessAttr"]
    assert harness["keepalive_explicit"] == (
        "4 empty deltas while the model was silent inside a block"
    )
    assert harness["keepalive_inferred"].startswith("0 (frames mode on;")
    assert harness["keepalive_unidentified"] is None


def test_the_watchdog_card_never_proposes_a_value(rendered) -> None:
    """The deadlines are the operator's choice: the card states, it never asks."""
    shown = rendered["limits"]["watchdog"]["stallZero"]
    text = " ".join([shown["lead"], *shown["items"], shown["keepalive"]]).lower()
    for phrase in (
        "set ",
        "raise",
        "lower",
        "should",
        "recommend",
        "change it",
        "try ",
    ):
        assert phrase not in text, phrase
    assert "changes nothing" in text
    assert rendered["limits"]["watchdog"]["pendingAfter"] == []


def test_the_watchdog_card_builds_no_markup_from_a_label(rendered) -> None:
    """Labels come from the manifest; they are written with textContent."""
    html = rendered["limits"]["watchdog"]["stallZero"]["html"]
    assert "<li>FALLBACK_STALL_TIMEOUT: 0 (no limit)</li>" in html


def test_the_apply_banner_names_only_the_fields_that_need_a_restart(rendered) -> None:
    """7.48.0: a restart is the exception, so the banner says who asked for it.

    The automatic restart used to say only "Restarting server..." -- which,
    when almost every field restarted, told the reader nothing they could act
    on. It now names the fields the server reported, the manual fallback keeps
    naming them, and a hot apply names nothing.
    """

    banner = rendered["applyBanner"]
    assert banner["automatic"] == "Applied. Restarting server for: PORT, LOG_LEVEL..."
    assert banner["manual"] == "Applied. Restart my-claude-code to use: HOST"
    assert banner["hot"] == "Applied"


# ------------------------------------------------- rail credential hints (7.49.0)


def test_a_rail_entry_whose_provider_cannot_serve_says_so(rendered) -> None:
    """The 2026-09-20 case: MODEL_FABLE on chatgpt_oauth with nothing signed in
    rendered like any working rail. Now the row says why it cannot serve."""

    hints = rendered["credHints"]
    by_ref = {row["ref"]: row for row in hints["rows"]}

    fable = by_ref["chatgpt_oauth/gpt-5.6-sol"]
    assert fable["text"] == "no usable credentials \u2014 sign in"
    assert fable["link"] == "Open provider"
    assert "ChatGPT OAuth (experimental)" in fable["title"]

    benched = by_ref["nvidia_nim/m-benched"]
    assert benched["text"] == f"every key benched until {hints['expectedClock']}"


def test_the_hint_covers_primaries_and_fallbacks_and_nothing_healthy(rendered) -> None:
    hints = rendered["credHints"]

    assert sorted(row["provider"] for row in hints["rows"]) == [
        "chatgpt_oauth",
        "nvidia_nim",
    ]
    # A bench that has already run out is not shown, even before a reload.
    assert "zai/m-expired" not in {row["ref"] for row in hints["rows"]}
    assert hints["healthyRowsWithHint"] == 0
    assert hints["markedNodes"] == 2


def test_the_hint_is_the_rows_last_child(rendered) -> None:
    """The rails are explicit grids: a hint anywhere but last would take a
    control's column (CLAUDE.md, "Adding a child to a rendered row")."""

    assert all(row["lastChild"] for row in rendered["credHints"]["rows"])


def test_the_hint_follows_an_edit_before_any_save(rendered) -> None:
    hints = rendered["credHints"]

    assert (
        "cerebras: no usable credentials \u2014 add a key"
        in hints["afterEditToKeyless"]
    )
    assert hints["afterEditToHealthy"] == ["nvidia_nim"]


def test_the_hint_link_opens_the_providers_page(rendered) -> None:
    assert rendered["credHints"]["viewAfterLink"] == "providers"


def test_the_hint_costs_no_request_of_its_own(rendered) -> None:
    """It rides the config payload the page already loads."""

    fetched = rendered["credHints"]["reloadFetches"]
    assert "/admin/api/config" in fetched
    assert not [path for path in fetched if "credentials" in path or "oauth" in path]


def test_a_status_reload_clears_the_hints(rendered) -> None:
    assert rendered["credHints"]["afterHealthyReload"] == 0


def test_every_analytics_aggregate_names_its_window(rendered) -> None:
    hints = rendered["credHints"]
    all_time = dict(hints["captionsAllTime"])
    last_day = dict(hints["captions24h"])

    for heading in (
        "Requests over time",
        "Tokens by model",
        "Model latency",
        "Requests by harness",
        "Top errors",
        "Upstream statuses",
    ):
        assert all_time[heading] == "Window: all stored rows", heading
        assert last_day[heading] == "Window: last 24h", heading
    # The lifetime counters and the in-flight panel are not history windows.
    assert hints["lifetimeCaptions"] == 0
    # One caption for the Cost panel, none on each of its four tables.
    assert [heading for heading, _ in hints["captionsAllTime"]].count(
        "Cost by model"
    ) == 0


def test_route_widgets_also_name_the_last_settings_save(rendered) -> None:
    captions = dict(rendered["credHints"]["captionsAllTime"])

    for heading in ("Failover", "Vision adapter", "Provider performance"):
        assert captions[heading].startswith(
            "Window: all stored rows \u00b7 settings last saved "
        ), heading
    assert "settings last saved" not in captions["Top errors"]


# ------------------------------------------------ 7.54.0 measured proxy speed


def _hosts(labels: list[str]) -> list[int]:
    return [int(label.split(":")[0].rsplit(".", 1)[1]) for label in labels]


def test_candidate_sort_by_measured_setup(rendered) -> None:
    """MCC's own median setup, fastest first; an unmeasured row last."""

    speed = rendered["proxying"]["speedSort"]
    values = [option["value"] for option in speed["options"]]
    for value in ("setup", "rate", "rank"):
        assert value in values, values
    labels = {option["value"]: option["text"] for option in speed["options"]}
    assert labels["setup"] == "measured setup (MCC)"
    assert _hosts(speed["bySetup"]) == [3, 2, 4, 1, 5]
    # Success rate, best first; rank (expected setup x live factor), lowest first.
    assert _hosts(speed["byRate"]) == [4, 2, 1, 3, 5]
    assert _hosts(speed["byRank"]) == [2, 4, 1, 3, 5]
    # The column: measured setup and k of n ok.
    assert "setup 4200 ms · 3 of 3 ok" in speed["speedCells"], speed["speedCells"]
    assert "not measured yet" in speed["speedCells"], speed["speedCells"]


def test_max_setup_filter(rendered) -> None:
    speed = rendered["proxying"]["speedSort"]
    # At most 1,600 ms: the slow one and the never-timed one are hidden.
    assert _hosts(speed["maxSetup1600"]) == [3, 2, 4]
    assert _hosts(speed["maxSetupNoFlaky"]) == [2, 4]
    assert speed["maxSetupCleared"] == 5
    assert _hosts(speed["hideSlow"]) == [3, 2, 4, 5]


def test_select_fastest_n(rendered) -> None:
    """It ticks the N best-ranked rows for the bulk buttons, and adds nothing."""

    fastest = rendered["proxying"]["speedSort"]["fastest"]
    assert fastest["selected"] == ["px_spd2", "px_spd4"]
    assert fastest["count"].startswith("2 selected"), fastest["count"]
    assert fastest["writes"] == []
    assert "Nothing is added until you press Add" in fastest["announcement"]


def test_feed_latency_sort_is_labelled_as_feed(rendered) -> None:
    speed = rendered["proxying"]["speedSort"]
    assert speed["feedLabel"] == "latency the feed published"
    # Sorted by the feed's claim, which is not MCC's order.
    assert _hosts(speed["byFeed"]) == [5, 1, 2, 3, 4]


def test_chain_entry_shows_speed_readout_and_chip(rendered) -> None:
    entry = rendered["proxying"]["speedSort"]["entry"]
    assert entry is not None
    assert entry["text"].startswith(
        "≈1.8 s · 4/5 ok · 12 samples · first token 1.4\u00d7"
    ), entry
    assert "proxy-state-working" in entry["chip"], entry
    assert "1200 ms when it works; fails 1 in 5" in entry["title"], entry
