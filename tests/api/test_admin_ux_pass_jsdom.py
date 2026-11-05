"""The 7.122.3 UX pass, proved in jsdom (scenario: ux_pass) and against the
base tree (scenario: ux_views).

Split out of ``test_admin_static_jsdom.py`` only so the release's own proofs
read as one document. Everything here runs the real ``index.html``,
``admin.js`` and ``admin_css`` in jsdom through ``admin_jsdom_harness.mjs``;
what jsdom cannot prove -- box heights, the sticky Details column, the phone
layout -- is measured in the browser and recorded in ``PROGRESS.md``.
"""

import json
import os
from pathlib import Path

import pytest

from tests.api.test_admin_static_jsdom import (
    UX_CHANGED_VIEWS,
    _run,
)

UX_BASELINE = Path(__file__).with_name("..") / "fixtures" / "ux_pass_dom_7_122_3.json"

#: CI must not pass a suite it never ran. `_run` already turns a missing node
#: or a missing jsdom into a failure under MCC_CI=1; this module reads the same
#: switch to assert its own runs produced measurements rather than skips.
ON_CI = os.environ.get("MCC_CI", "") not in ("", "0", "false")

pytestmark = pytest.mark.xdist_group("admin_static_jsdom")


@pytest.fixture(scope="module")
def ux_pass() -> dict:
    return _run(MCC_JSDOM_SCENARIO="ux_pass")


@pytest.fixture(scope="module")
def ux_views() -> dict:
    views = _run(MCC_JSDOM_SCENARIO="ux_views")
    # Writing the baseline is a deliberate, one-off act: point
    # MCC_JSDOM_UX_BASELINE_OUT at a path and run this module against the
    # BASE tree's admin_static (STATIC_DIR replaced). Nothing in CI sets it.
    out = os.environ.get("MCC_JSDOM_UX_BASELINE_OUT")
    if out:
        Path(out).write_text(
            json.dumps(
                {"written_by": "BASE tree d8833983", "views": views["views"]},
                indent=1,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    return views


def test_ux_scenarios_run_clean(ux_pass, ux_views) -> None:
    for payload in (ux_pass, ux_views):
        assert payload["fatal"] is None
        assert payload["scenarioFatal"] is None
        assert payload["scriptErrors"] == []
        assert payload["consoleErrors"] == []
    if ON_CI:
        # MCC_CI=1 is the switch that fails a missing node/jsdom. This is the
        # other half: the payloads above must be real measurements.
        assert ux_pass["titles"]["requests"] == "Analytics"
        assert ux_views["views"]


def test_ux_pass_leaves_every_other_view_as_the_base_tree_drew_it(ux_views) -> None:
    """Exactly the declared views moved; everything else is byte-identical.

    The baseline is this same scenario pointed at BASE tree d8833983 with the
    same payload. Digests are of the normalised HTML (see
    :func:`tests.api.admin_jsdom_ux_views.uxNormalise`), so the three
    display-only changes this pass makes to every settings control cannot hide
    a real change on the pages they touch.

    Since 8.1.0 a page with four or more named sections also carries the
    per-page section navigator. It is normalised OUT on both sides
    (``uxNormalise``), because the base tree predates it: leaving it in would
    move twelve of fifteen views and say nothing about the page underneath.
    8.1.0 proves the strip against its own base-written baseline in
    ``tests/api/test_admin_design_system_jsdom.py``.

    Get Started is declared separately: 8.1.0 puts the Appearance card there,
    which is that release's own addition and is proved in the same file.

    8.3.0 adds a NEW page (``usage``) that did not exist when this baseline was
    taken. It is declared here by name rather than compared: there is nothing
    on the base tree to compare it to, and every view the baseline DOES name is
    still compared, unchanged, which is the claim this test exists to make.
    ``tests/api/test_admin_usage_page_jsdom.py`` carries the addition's own
    base-written equality against v8.2.0.
    """

    baseline = json.loads(UX_BASELINE.read_text(encoding="utf-8"))
    assert "d8833983" in baseline["written_by"]
    base = {
        name: entry["sha256Normalised"] for name, entry in baseline["views"].items()
    }
    mine = {
        name: entry["sha256Normalised"] for name, entry in ux_views["views"].items()
    }
    assert set(mine) - set(base) == {"usage"}
    assert set(base) - set(mine) == set()
    moved = sorted(name for name, digest in base.items() if mine[name] != digest)
    # The claim this test exists to make: nothing moved that this pass did not
    # declare. UX_CHANGED_VIEWS | {"get_started"} is a CEILING, not an
    # equality -- a declared view whose normalised digest happens to match the
    # base tree's on a given platform is a page that did not change, which is
    # the claim being tested, not a failure. (Before this was a ceiling it was
    # an equality, and it went red on CI whenever a declared view's markup
    # happened to normalise back to the base tree's bytes there.)
    unexpected = sorted(set(moved) - (UX_CHANGED_VIEWS | {"get_started"}))
    assert not unexpected, (
        "these views are not what the base tree drew, and are not declared "
        f"moves: {unexpected}"
    )
    # And the comparison is not vacuous: at least one declared view really did
    # move, so an all-empty digest table cannot pass this test.
    assert moved, "no view moved at all, so nothing was really compared"


def test_ux_pass_the_unmoved_views_are_not_vacuous(ux_views) -> None:
    """The baseline comparison above would also pass if nothing had a digest.

    Every view outside the declared set is compared on a non-empty document,
    and the declared ones really are different in the same run.
    """

    baseline = json.loads(UX_BASELINE.read_text(encoding="utf-8"))
    for name, entry in baseline["views"].items():
        assert entry["length"] > 0, name
    # The raw digest moved on more views than the normalised one did -- that
    # is the settings-wide display change this pass makes, and it is visible.
    raw_moved = {
        name
        for name, entry in baseline["views"].items()
        if ux_views["views"][name]["sha256"] != entry["sha256"]
    }
    assert raw_moved >= UX_CHANGED_VIEWS


# ── U19: the page title is the navigation label ──────────────────────────────


def test_titles_say_what_the_sidebar_says(ux_pass) -> None:
    assert ux_pass["titles"]["requests"] == "Analytics"
    assert ux_pass["titles"]["docs"] == "Docs"


# ── U20: the fold is in the markup and its button works ─────────────────────


def test_the_phone_menu_button_toggles_the_sidebar(ux_pass) -> None:
    sidebar = ux_pass["sidebar"]
    assert sidebar["toggle"] is True
    assert sidebar["controls"] == "appSidebar"
    assert sidebar["expanded"] == "false"
    assert sidebar["opened"] is True
    assert sidebar["closed"] is True


# ── U13: the footer is a settings bar ───────────────────────────────────────


def test_the_footer_is_hidden_on_a_page_with_no_settings(ux_pass) -> None:
    footer = ux_pass["footer"]
    assert footer["guide"] is False
    assert footer["docs"] is False
    # Analytics has settings (the request log and cost cards), Providers has
    # many: the bar stays there.
    assert footer["requests"] is True
    assert footer["providers"] is True


# ── U22: a success message belongs to the page that produced it ─────────────


def test_a_success_message_is_cleared_by_navigating(ux_pass) -> None:
    assert ux_pass["message"]["before"] == "Added a thing"
    assert ux_pass["message"]["after"] == ""


# ── U16 / U15: the rails ─────────────────────────────────────────────────────
#
# 8.1.0 folded these two rails into the per-page section navigator: the
# `<nav>`s are re-parented into the strip, and a rail link whose target the
# navigator already names is dropped so the row never says the same thing
# twice. That is why the rail itself now reports ZERO links — the destinations
# are all still reachable, one row, one name each. The navigator's own suite
# (`test_admin_design_system_jsdom.py`) is where every destination, every
# landing and the hash are proved; these two tests keep pinning what they
# always did, against the row that now carries it.


def test_the_providers_sections_are_all_reachable_from_one_row(ux_pass) -> None:
    rail = ux_pass["providersJump"]
    assert rail is not None
    # The rail's own anchors are all covered by the navigator's buttons now.
    assert rail["links"] == []
    # And the page's six destinations still resolve.
    targets = ux_pass["providersNavigatorTargets"]
    assert len(targets) == 6
    assert all(targets)


def test_the_proxying_page_puts_chains_before_sources(ux_pass) -> None:
    proxying = ux_pass["proxying"]
    # The navigator's own order carries U15's decision: chains first.
    assert proxying["navigator"] == [
        "#proxyingList",
        "#proxyingSourcesSection",
        "#proxyingFeedSection",
    ]
    assert proxying["chainsBeforeSources"] is True
    assert proxying["chainsBeforeFeeds"] is True


# ── U23: the Models page's buttons are styled ───────────────────────────────


def test_the_models_buttons_carry_the_stylesheet_s_class(ux_pass) -> None:
    buttons = ux_pass["modelsButtons"]
    for name, value in buttons.items():
        assert "secondary-button" in value, name


# ── U17: no retired command is named on any card ────────────────────────────


def test_no_card_renders_a_retired_command(ux_pass) -> None:
    legacy = ux_pass["legacyCommands"]
    assert legacy["total"] > 0
    assert legacy["retired"] == []
    assert legacy["legacyRows"] == 0


# ── U12: "default value" instead of "set here" ──────────────────────────────


def test_a_value_equal_to_its_default_reads_as_the_default(ux_pass) -> None:
    chips = ux_pass["chips"]
    assert chips["equal"]["chip"] == "default value"
    # Nothing to undo, so nothing offers to: the button is not rendered.
    assert chips["equal"]["reset"] is False
    assert chips["different"]["chip"] == "set here"


# ── U27/U30: the words a select and its default line wear ───────────────────


def test_a_boolean_select_reads_off_and_on(ux_pass) -> None:
    bench = ux_pass["benchSelect"]
    assert bench is not None
    assert bench["all"][1:] == ["Off", "On"]
    assert bench["first"].startswith("Default (")
    assert bench["meta"] == "default: On"


def test_a_rotation_select_and_its_default_line_agree(ux_pass) -> None:
    rotation = ux_pass["rotationMeta"]
    assert rotation is not None
    assert rotation["firstOption"].startswith("Default (Auto")
    # U30 named the same setting two ways ("Default (Auto…)" above "default:
    # none"). The line now says Auto too -- its first clause, so the narrow
    # column keeps one line (the density rule).
    assert rotation["meta"] == "default: Auto"


# ── U26: the optimizer's opening sentence ───────────────────────────────────


def test_the_optimizer_no_longer_claims_nothing_is_enabled(ux_pass) -> None:
    intro = ux_pass["optimizerIntro"]
    assert "Nothing here is turned on by opening the page" in intro
    assert "Nothing on this page is enabled for you" not in intro


# ── U31: nested brackets ────────────────────────────────────────────────────


def test_start_at_login_has_one_pair_of_brackets(ux_pass) -> None:
    label = ux_pass["autostartLabel"]
    assert label.startswith("Start at Login —")
    assert label.count("(") == 1, label


# ── U6: the Web Search find box matches a curated haystack ──────────────────


def test_the_web_search_box_matches_names_keys_and_nothing_else(ux_pass) -> None:
    search = ux_pass["webSearch"]
    # Two services, one of them keyed and carrying a Rotation label.
    assert search["atRest"]["shown"] == ["tavily", "ddgs"]
    assert search["atRest"]["count"] == "2 services · 2 configured"
    # The word "rotation" is on every keyed card and matches none of them.
    assert search["rotation"]["shown"] == []
    assert search["rotation"]["count"] == "0 of 2"
    assert search["rotation"]["noMatch"] is True
    # A name and a variable both find the service they belong to.
    assert search["tavily"]["shown"] == ["tavily"]
    assert search["tavily"]["count"] == "1 of 2"
    assert search["key"]["shown"] == ["tavily"]
    assert search["nothing"]["noMatch"] is True


# ── U1: LINKFIX, widened ────────────────────────────────────────────────────


def test_a_web_search_pool_link_lands_on_the_web_search_card(ux_pass) -> None:
    landed = ux_pass["linkfixWebSearch"]
    assert landed["found"] is True
    assert landed["view"] == "web_search"
    assert landed["pages"] == ["web_search"]
    assert landed["card"] is True
    assert landed["poolOpen"] is True


def test_a_model_pool_link_lands_on_the_provider_card_opened(ux_pass) -> None:
    landed = ux_pass["linkfixModel"]
    assert landed["found"] is True
    assert landed["view"] == "providers"
    assert landed["card"] is True
    assert landed["cardOpen"] is True
    assert landed["poolOpen"] is True


# ── U18: the rail follows a re-render ───────────────────────────────────────


def test_the_limits_rail_still_marks_a_section_after_a_re_render(ux_pass) -> None:
    assert ux_pass["scrollspy"]["current"] != []


# ── ADDENDUM 2: the Analytics canvas ────────────────────────────────────────


def test_the_analytics_page_reads_tiles_then_in_flight_then_requests(ux_pass) -> None:
    order = ux_pass["analytics"]["order"]
    assert order["tiles"] < order["inflight"] < order["log"]
    assert order["log"] < order["table"]
    # The filters sit above the tiles: the band is what they filter.
    assert order["toolbar"] < order["tiles"]
    # Charts and the secondary panels come after the request list.
    assert order["table"] < order["charts"]
    assert order["charts"] < order["cost"]
    assert order["cost"] < order["maintenance"]
    assert ux_pass["analytics"]["headingTitles"] == {
        "summary": "Usage in this window",
        "log": "Requests",
        "charts": "Charts",
    }


def test_the_summary_band_carries_a_cost_tile_first(ux_pass) -> None:
    """The tile's headline is the WINDOW'S WHOLE COST (ADDENDUM ROUND 4 §4).

    It read ``Cost (reported)`` / ``$1.5000`` until 8.2.0, with the estimated
    half mentioned underneath -- so on a log where most hosts report no price,
    the first number on the page described the smallest slice of the traffic
    (the owner's own dashboard: $0.7097 against $4,050.2926 estimated). What it
    prints now is ``total_usd``: both halves, summed by the server, with each
    half still named in the small print so the estimate stays labelled.

    The naming moved from the visible line to the tile's ``title`` in 8.14.2
    (ANALYTICS-OVERFLOW, USER Q3): the words repeated what the two positions
    already say, and on a real log they made the line four deep. Every number
    is still on the tile, and the full sentence is one hover away -- asserted
    in ``test_the_cost_tile_keeps_every_number_and_moves_the_two_words``.
    """

    tile = ux_pass["analytics"]["costTile"]
    assert tile["exists"] is True
    assert tile["first"] is True
    assert tile["label"] == "Cost"
    assert tile["value"] == "$1.7500"
    assert tile["note"] == "7 of 7 priced · $1.5000 + $0.2500"


def test_the_four_summary_charts_are_drawn_and_readable(ux_pass) -> None:
    charts = {entry["id"]: entry for entry in ux_pass["analytics"]["charts"]}
    assert sorted(charts) == [
        "reqCostChart",
        "reqModelChart",
        "reqSeriesChart",
        "reqTtftChart",
    ]
    for chart_id, chart in charts.items():
        assert chart["exists"] is True, chart_id
        assert chart["tabIndex"] == 0, chart_id
        assert chart["describedBy"] == f"{chart_id}Readout", chart_id
        # The numbers behind the drawing, for a screen reader.
        assert chart["dataTable"] and chart["dataTable"] > 0, chart_id


def test_a_chart_can_be_read_with_the_arrow_keys(ux_pass) -> None:
    keyboard = ux_pass["chartKeyboard"]
    # Focus lands on the first bucket; ArrowRight moves to the second.
    assert keyboard["afterRight"].startswith("10-31T10:15")
    assert "Requests: 4" in keyboard["afterRight"]
    assert "Errors: 0" in keyboard["afterRight"]
    assert keyboard["afterHome"].startswith("10-31T10:00")
    assert "Requests: 3" in keyboard["afterHome"]
    assert "Errors: 1" in keyboard["afterHome"]
    assert keyboard["afterEnd"].startswith("10-31T10:15")


def test_a_chart_shows_a_tooltip_on_hover(ux_pass) -> None:
    tooltip = ux_pass["chartTooltip"]
    assert tooltip["exists"] is True
    assert tooltip["hidden"] is False
    assert "Requests:" in tooltip["text"]


def test_the_destructive_buttons_are_at_the_foot_under_maintenance(ux_pass) -> None:
    filters = ux_pass["analytics"]["filters"]
    assert filters["applyLabel"] == "Apply filters"
    assert filters["destructiveInToolbar"] == 0
    assert filters["destructiveInMaintenance"] == 3


def test_a_row_opens_the_detail_it_belongs_to(ux_pass) -> None:
    opened = ux_pass["rowOpen"]
    assert opened["id"]
    assert opened["modalOpen"] is True
    assert opened["title"].startswith("Request ")


def test_an_empty_log_says_what_to_do_next(ux_pass) -> None:
    empty = ux_pass["emptyState"]
    assert empty["visible"] is True
    assert "No requests yet" in empty["text"]
    # A filter is a different question, and its answer is the table's own.
    assert empty["filtered"]["visible"] is False


# ---------------------------------------------------------------- 8.14.2
# ANALYTICS-OVERFLOW: a tile's figure that is too wide for its card shrinks
# instead of spilling out of it. jsdom has no layout, so the fit itself is
# measured in a browser (the table is in the release report); what is proved
# here is the structure the browser proof depends on.


def test_the_digit_class_is_written_only_where_a_rule_exists(ux_pass) -> None:
    """A tile that already fits keeps the DOM it had.

    The class table starts at 13 digits, so 12 and below must carry no
    attribute at all -- that is what makes the rendered tile byte-identical to
    the base tree below the first rung. `4,050.2926 ms` counts eight digits and
    is deliberately below the rung: the measured guard is what catches it.
    """

    tiles = {tile["value"]: tile for tile in ux_pass["analyticsOverflow"]["tiles"]}
    assert tiles["9,999,999"]["digits"] is None
    assert tiles["10,000,000,000"]["digits"] is None  # 11 digits
    assert tiles["100,000,000,000"]["digits"] is None  # 12 digits
    assert tiles["1,234,567,890,123"]["digits"] == "13"
    assert tiles["12,345,678,901,234,567"]["digits"] == "17"
    assert tiles["4,050.2926 ms"]["digits"] is None


def test_a_sentence_is_not_a_figure_and_keeps_its_own_size(ux_pass) -> None:
    tiles = {tile["value"]: tile for tile in ux_pass["analyticsOverflow"]["tiles"]}
    sentence = tiles["not measured yet"]
    assert sentence["note"] is True
    assert sentence["digits"] is None


def test_a_tile_that_fits_carries_no_inline_size(ux_pass) -> None:
    """The measured guard resets the size and only writes one when it must.

    jsdom reports every box as 0 x 0, so nothing overflows here and the guard
    must leave every figure at the size its CSS gives it -- an inline style on
    a tile that fits would be a typographic change nobody asked for.
    """

    for tile in ux_pass["analyticsOverflow"]["tiles"]:
        assert tile["inlineSize"] is None, tile


def test_the_cost_tile_keeps_every_number_and_moves_the_two_words(ux_pass) -> None:
    """USER Q3 (2026-11-04, "all rec"): the small print drops the repeated
    words into the title. Every number stays, and the full sentence survives."""

    note = ux_pass["analyticsOverflow"]["costNote"]
    assert note["value"] == "$5284.8604"
    assert note["text"] == "465,391 of 683,928 priced · $4050.2926 + $1234.5678"
    assert "reported" not in note["text"]
    assert "est." not in note["text"]
    assert (
        note["title"]
        == "465,391 of 683,928 priced · $4050.2926 reported + $1234.5678 est."
    )
    assert note["inlineSize"] is None
