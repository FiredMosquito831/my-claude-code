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
    """A setting rendered on two pages can show two answers."""
    limits = rendered["limits"]
    assert limits["crosslinks"] == 1
    assert "Model Config" in limits["crosslinkText"]
    assert limits["skipKindsOnLimits"] == 0


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
