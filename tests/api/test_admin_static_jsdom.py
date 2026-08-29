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
    views = rendered["views"]
    assert views["providers"]["sections"] >= 1
    assert views["limits"]["sections"] >= 1
    assert views["limits"]["fieldInputs"] >= 1
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
        "requests": 0,
        "optimizer": 6,
        "web_search": 1,
        "limits": 1,
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
