"""Execute the real admin.js in jsdom and assert on what it rendered.

This is the only test in the suite that runs the dashboard's JavaScript. It
proves the script evaluates, every nav entry still renders, and the Token
Optimizer page shows honest empty states on a fresh install.

What it does NOT prove, because jsdom has no layout engine: spacing, overflow,
contrast, focus rings, breakpoints, or anything else that needs a box to have a
size. Those remain unverified by any automated check in this repo.
"""

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NoReturn, cast

import pytest

from my_claude_code.api import raw_diagnostics as raw_reader
from my_claude_code.api.admin_harness_routes import _not_yet_entry
from my_claude_code.api.marketplace_page import (
    match_charged,
    offers_payload,
    page_payload,
    recent_rows,
    remember_account,
    reset_account_memory,
    reset_market_memory,
)
from my_claude_code.api.model_admin import (
    PROMPT_CACHE_WRITE_1H_NOTE,
    prompt_cache_gateway_models,
    prompt_cache_lifetimes_payload,
)
from my_claude_code.application import marketplace as application_marketplace
from my_claude_code.application.marketplace import market_snapshot_from_payload
from my_claude_code.application.prompt_cache import adapt_prompt_cache_minutes
from my_claude_code.config import paths
from my_claude_code.config.admin.manifest import (
    FIELD_BY_KEY,
    FIELDS,
    SECTIONS,
    ConfigOptionSpec,
)
from my_claude_code.config.admin.persistence import (
    commit_prepared_admin_update,
    prepare_admin_update,
)
from my_claude_code.config.admin.values import ROUTE_PAUSE_KEYS
from my_claude_code.config.harnesses import not_yet_cards
from my_claude_code.config.marketplace_profiles import (
    MarketplaceProfiles,
    parse_profile,
)
from my_claude_code.config.model_overrides import ModelParameterOverrides
from my_claude_code.config.model_refs import ConfiguredChatModelRef
from my_claude_code.config.prompt_cache_lifetimes import (
    ANTHROPIC_MESSAGES_LIFETIMES,
    PROMPT_CACHE_LIFETIME_FAMILIES,
)
from my_claude_code.config.settings import Settings
from my_claude_code.config.websearch_catalog import WEBSEARCH_CATALOG
from my_claude_code.core.upstream_ladder import _TIMES
from tests.support.select_blank_payload import (
    FIXTURE_ENV,
    ROTATION_EDIT_VALUE,
    build_payload,
    build_payload_after_save,
    rotation_edit_values,
    websearch_rotation_keys,
)
from tests.support.websearch_page_fixture import PROBE as WS_PROBE
from tests.support.websearch_page_fixture import websearch_page_fixture

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
    # 7.122.3 (U27): a select whose options are exactly false/true reads
    # Off/On, like every other switch on the page. The value is unchanged.
    assert control["firstOptionLabel"].startswith("Default (On)")
    # The whole point: a form nobody touched has nothing to save.
    assert rendered["fields"]["dirtyOnLoad"] == "No changes"


def test_every_field_says_what_its_default_is(rendered) -> None:
    defaults = rendered["fields"]["fieldDefaults"]

    # 7.122.3 (U27/U30): the default is named in the words the control uses.
    assert defaults["FALLBACK_BENCH_ENABLED"] == "default: On"
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


GATEWAY_TRACE = "b5162a9f0c1d4e2f8a7b6c5d4e3f2a1b"


def test_a_gateway_refusal_with_no_reason_says_so_and_shows_the_trace_id(
    rendered,
) -> None:
    """7.105.2: Command Code passed on only a trace id; the row says exactly that."""
    detail = rendered["requestDetail"]["gatewayNoReason"]

    label = f"the gateway gave no reason (trace id {GATEWAY_TRACE})"
    first, second = detail["endedCells"]
    assert first["value"] == label
    assert first["title"].startswith(f"{label}. The gateway answered")
    assert "kept below exactly as it arrived" in first["title"]
    # The other attempt is untouched: its cell carries no title of its own.
    assert second == {"value": "answered", "title": ""}
    # The host's text is still shown whole, kind first, as before.
    assert detail["chainReasons"][0].startswith(
        "model_rejectedUpstream provider COMMANDCODE returned HTTP 400."
    )
    assert f"invalid request error trace_id: {GATEWAY_TRACE}" in (
        detail["chainReasons"][0].replace('\\"', '"')
    )
    # And the id is on its own line, selectable, with a Copy control.
    (line,) = detail["traceLines"]
    assert line["code"] == GATEWAY_TRACE
    assert line["text"] == f"The gateway gave no reason. Trace id {GATEWAY_TRACE}Copy"
    assert line["button"] == f"Copy {GATEWAY_TRACE}"


def test_the_trace_id_copy_button_copies_the_id(rendered) -> None:
    copied = rendered["requestDetail"]["gatewayCopied"]

    assert copied["written"] == [GATEWAY_TRACE]
    assert copied["label"] == "Copied"


def test_a_gateway_refusal_with_a_reason_renders_as_before(rendered) -> None:
    """No ``gateway_trace_id`` key: the kind under "ended", no trace line."""
    detail = rendered["requestDetail"]["gatewayWithReason"]

    assert [cell["value"] for cell in detail["endedCells"]] == [
        "model_rejected",
        "answered",
    ]
    assert all(cell["title"] == "" for cell in detail["endedCells"])
    assert detail["traceLines"] == []
    assert "is not a" in detail["chainReasons"][0]


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
        # 7.88.0: Exit where Folder was (Folder is in the chip and the detail).
        "Session",
        "Exit",
        "Origin",
        "Provider",
        "Key",
        "Requested model",
        "Model",
        "Status",
        "Turn",
        "Tokens",
        "Cost",
        # 7.122.2: named for what it measures -- the first *visible byte*.
        "First byte",
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


#: The repeat chip's sign (7.82.0), spelled by code point for RUF001.
REPEAT_SIGN = chr(0xD7)


def test_a_copy_onto_a_chain_that_already_has_the_ref_adds_a_second_listing(
    rendered,
) -> None:
    """7.82.0: a model listed twice is tried twice, and Apply keeps both."""
    routing = rendered["routing"]

    assert routing["repeatChipsAtLoad"] == 0
    assert routing["repeatChipsBeforeDuplicate"] == []
    assert routing["duplicateOccurrences"] == 2
    before = routing["opusBeforeDuplicate"].split(",")
    after = routing["opusAfterDuplicateCopy"].split(",")
    # The first copy landed at position 1, the second in front of row 5.
    assert [index for index, ref in enumerate(after) if ref == "p1/s1"] == [0, 4]
    assert [ref for ref in after if ref != "p1/s1"] == before
    assert routing["duplicateSentence"].startswith(
        "Copied 1 model into the Opus chain, at position 5."
    )
    assert "moved instead of copied" not in routing["duplicateSentence"]
    assert routing["repeatChipsAfterDuplicate"] == [
        ["p1/s1", f"{REPEAT_SIGN}2"],
        ["p1/s1", f"{REPEAT_SIGN}2"],
    ]
    assert "listed 2 times on this route" in routing["repeatChipTitle"]
    assert "one more try" in routing["repeatChipTitle"]
    # Taking one listing away takes the chips away with it -- and leaves the
    # rail exactly where the pre-7.82.0 "moved instead of copied" left it.
    removed = routing["opusAfterFirstListingRemoved"].split(",")
    assert removed == [*before[:3], "p1/s1", *before[3:]]
    assert routing["repeatChipsAfterRemoval"] == []


def test_a_copy_of_the_target_primary_lands_as_another_listing(rendered) -> None:
    """7.82.0: the route's own model listed again further down is allowed."""
    routing = rendered["routing"]

    assert routing["sonnetUnchangedByRefusal"] is False
    assert routing["sonnetAfterPrimaryRefCopy"] == (
        "p1/s1," + routing["sonnetBeforePrimaryRefCopy"]
    )
    assert routing["refusalSentence"].startswith(
        "Copied 1 model into the Sonnet chain, at position 1."
    )
    assert "already routes to" not in routing["refusalSentence"]
    assert routing["primaryRefCopyChips"] == [
        ["p1/s1", f"{REPEAT_SIGN}2"],
        ["p1/s1", f"{REPEAT_SIGN}2"],
    ]
    assert (
        routing["sonnetAfterPrimaryRefCopyUndo"]
        == routing["sonnetBeforePrimaryRefCopy"]
    )


def test_a_same_card_drop_moves_and_ctrl_copies(rendered) -> None:
    routing = rendered["routing"]

    assert routing["sonnetBeforeSameRail"] == "p1/s0,p1/s2"
    assert routing["sonnetAfterSameRailPlainDrop"] == "p1/s2,p1/s0"
    assert routing["sonnetAfterSameRailCopy"] == "p1/s2,p1/s0,p1/s2"
    assert routing["sameRailCopySentence"].startswith(
        "Copied 1 model inside the Sonnet chain, at position 1."
    )
    assert "more than once" in routing["sameRailCopySentence"]
    assert routing["sameRailCopyChips"] == [
        ["p1/s2", f"{REPEAT_SIGN}2"],
        ["p1/s2", f"{REPEAT_SIGN}2"],
    ]
    assert routing["sonnetAfterSecondCopy"] == "p1/s2,p1/s2,p1/s0,p1/s2"
    assert routing["secondCopyChips"] == [
        ["p1/s2", f"{REPEAT_SIGN}3"],
        ["p1/s2", f"{REPEAT_SIGN}3"],
        ["p1/s2", f"{REPEAT_SIGN}3"],
    ]


def test_a_rail_rebuilt_from_a_saved_repeat_shows_it(rendered) -> None:
    """The undo path rebuilds rows from a stored value, as a page load does."""
    routing = rendered["routing"]

    assert routing["sonnetRebuiltWithRepeat"] == "p1/s2,p1/s0,p1/s2"
    assert routing["rebuiltRepeatChips"] == [
        ["p1/s2", f"{REPEAT_SIGN}2"],
        ["p1/s2", f"{REPEAT_SIGN}2"],
    ]
    assert routing["sonnetAfterEditingARepeat"] == "p1/s9,p1/s0,p1/s2"
    assert routing["chipsAfterEditingARepeat"] == []


def test_ctrl_drops_a_copy_of_the_primary_into_its_own_chain(rendered) -> None:
    routing = rendered["routing"]

    assert (
        routing["sonnetPrimaryAfterPrimaryCopy"]
        == (routing["sonnetPrimaryBeforePrimaryCopy"])
    )
    assert routing["sonnetPrimaryAfterPrimaryCopy"] == "p1/s1"
    assert routing["sonnetAfterPrimaryCopy"] == "p1/s9,p1/s1,p1/s0,p1/s2"
    assert "traded places" not in routing["primaryCopySentence"]
    # The primary node and its new row both carry the chip.
    assert routing["primaryCopyChips"] == [
        ["p1/s1", f"{REPEAT_SIGN}2"],
        ["p1/s1", f"{REPEAT_SIGN}2"],
    ]


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


def test_the_models_page_draws_what_the_provider_list_says(rendered) -> None:
    """7.79.0: three rows in the provider's own words, badged like every row."""

    rows = {row[0]: row[1:] for row in rendered["models"]["declaredRows"]}

    modalities = rows["accepts → produces"]
    assert modalities[0] == "image, text → text"
    assert "provider /models" in modalities[1]
    assert "tier 1 of 11" in modalities[1]
    assert rows["model type (provider's word)"][0] == "chat"
    # 7.85.0: the row is "endpoints"; its badge names whose words they are.
    endpoints = rows["endpoints"]
    assert endpoints[0] == "not reported"
    assert endpoints[1] == "unknown"


def test_the_models_page_says_which_rung_stated_the_kind(rendered) -> None:
    """7.80.0: a "kind" row, badged by the source, the rung in its note."""

    rows = rendered["models"]["kindRows"]

    listed = rows["providerList"]
    assert listed["cells"][0] == "kind"
    assert listed["cells"][1] == "Chat + Transcription"
    assert "the provider's model list" in listed["cells"][2]
    assert "provider /models, exact id" in listed["cells"][2]
    assert "models-source-provider" in listed["badgeClass"]

    words = rows["providerWords"]
    assert words["cells"][1] == "Image"
    assert "the provider's model type or endpoints" in words["cells"][2]
    assert "tag stripped" in words["cells"][2]

    voted = rows["voted"]
    assert "models.dev modalities — guessed" in voted["cells"][2]
    assert "models-source-approximate" in voted["badgeClass"]

    unknown = rows["unknown"]
    assert unknown["cells"][1] == "not known"
    assert "offered in every list" in unknown["cells"][2]


def test_the_models_page_draws_openrouter_live_and_both_statements(
    rendered,
) -> None:
    """7.84.0: the live rung's badge, its three rows, and a second statement."""

    rows = {row["cells"][0]: row for row in rendered["models"]["liveRows"]}

    vision = rows["reads images"]
    assert vision["cells"][1] == "yes"
    assert "also stated: no (OpenRouter live, exact id)" in vision["cells"][2]
    assert "models-source-models_dev" in vision["badge"]

    cache = rows["cache read price (USD / 1M)"]
    assert cache["cells"][1] == "$0.05"
    assert "OpenRouter live" in cache["cells"][2]
    assert "models-source-openrouter_live" in cache["badge"]

    # 7.85.0: "description" -- the provider's list, OpenRouter or models.dev.
    assert rows["description"]["cells"][1] == "A model."
    assert rows["knowledge cutoff"]["cells"][1] == "not reported"
    listed = rows["listed on OpenRouter"]
    assert listed["cells"][1] == "2026-02-11"
    assert "not the model's release date" in listed["cells"][2]

    kind = rows["kind"]
    assert kind["cells"][1] == "Chat + Transcription"
    assert "also stated: Chat (OpenRouter live, exact id)" in kind["cells"][2]


def test_the_models_page_draws_dates_cutoff_retirement_and_litellm(rendered) -> None:
    """7.85.0: four display rows badged by their rung, LiteLLM's badge and kind."""

    rows = {row["cells"][0]: row for row in rendered["models"]["datesRows"]}

    published = rows["published"]
    assert published["cells"][1] == "2026-08-26"
    assert "provider /models" in published["cells"][2]
    assert "models-source-provider" in published["badge"]

    retires = rows["retires"]
    assert retires["cells"][1] == "2026-06-17"
    assert "LiteLLM model map" in retires["cells"][2]
    assert "models-source-litellm" in retires["badge"]

    cutoff = rows["knowledge cutoff"]
    assert cutoff["cells"][1] == "2025-06"
    assert "models-source-models_dev" in cutoff["badge"]

    description = rows["description"]
    assert description["cells"][1] == "OpenRouter's words."
    assert "also stated: The provider's own words." in description["cells"][2]

    endpoints = rows["endpoints"]
    assert "/v1/chat/completions" in endpoints["cells"][1]
    assert "models-source-litellm" in endpoints["badge"]

    kind = rows["kind"]
    assert kind["cells"][1] == "Chat + Transcription"
    assert "LiteLLM's model map" in kind["cells"][2]
    assert "models-source-litellm" in kind["badge"]
    # No listing day was given, so that row is not drawn at all.
    assert "listed on OpenRouter" not in rows


def test_everything_known_loads_once_when_opened_and_marks_each_statement(
    rendered,
) -> None:
    """7.86.0: the lazy disclosure, its table, its marks and each source's row."""

    known = rendered["models"]["knowledge"]
    assert known["summary"] == "Everything known"
    # Nothing is fetched until it is opened; opened twice, it is fetched once.
    assert known["fetchedClosed"] == 0
    assert known["urls"] == ["/admin/api/models/knowledge?ref=novita%2Facme%2Fmodel"]
    # One column per source that states something, after the value in use.
    assert known["header"] == [
        "field",
        "MCC uses",
        "provider's own list",
        "OpenRouter live",
        "models.dev, cross-provider vote",
    ]
    description, price = known["rows"]
    assert description[0] == "description"
    assert description[1] == "Novita's words. (provider /models)"
    assert description[2] == "Novita's words. — used"
    assert description[3] == "OpenRouter's words."
    assert price[1] == "$0.30 (provider /models)"
    assert price[4] == "$0.50"
    assert known["usedCells"] == ["Novita's words. — used", "$0.30 — used"]
    assert known["differsCells"] == ["OpenRouter's words.", "$0.50"]
    assert known["unreadCells"] == ["$0.50"]
    assert "OpenRouter live, exact id (acme/model)" in known["titles"]
    assert "cross-provider, exact id · not read for this provider" in known["titles"]
    assert known["rowSummaries"] == [
        "provider's own list — acme/model — exact id; as the last sweep read it",
        "models.dev, cross-provider vote — one/acme/model — cross-provider, exact id "
        "— not read for this provider",
    ]
    assert known["unreadRows"] == 1
    assert known["firstRowPaths"] == [
        ["id", '"acme/model"'],
        ["architecture.tokenizer", '"Other"'],
        ["tags", '["chat"]'],
    ]
    # A failed load says so, and the next open asks again.
    assert "Could not read it: busy" in known["failedText"]
    assert known["retriedTable"] is True
    assert known["totalFetches"] == 3


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
        "mcc-rtk enable claude",
    ]
    assert [line["kind"] for line in claude["commandLines"]] == [
        "primary",
        "flag",
        "flag",
        "rtk",
    ]
    # 7.122.3 (U17): the retired alias the registry still lists is not drawn.
    # The card answers "what can I type" with commands that work.
    assert all(
        not line["command"].lower().startswith("fcc-")
        for card in rendered["codingAgents"]["cards"]
        for line in card["commandLines"]
    )
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


def test_the_subscription_warning_names_every_client_the_gate_admits(
    rendered: dict,
) -> None:
    """7.97.0: since 7.92.1 the gate admits the Claude desktop app as well as
    Claude Code and the Agent SDK; the card's warning says so."""
    text = rendered["anthropicOAuthWarning"]["text"]

    assert "does not permit" in text
    assert (
        "did not come from Claude Code, the Claude Agent SDK or the Claude "
        "desktop app." in text
    )
    assert "ANTHROPIC-SUBSCRIPTION.md" in text


def test_the_shared_row_explains_why_it_is_read_only() -> None:
    """7.69.1: a shared row says who owns it and why MCC will not renew it.

    Rendered by its own small harness (``admin_oauth_ownership_jsdom_harness
    .mjs``) from the payload below, the one source of truth: the renderer is
    a pure function of ``/sources``, so this needs no page start-up.
    """

    node = shutil.which("node")
    if node is None:
        _missing("node is not on PATH")
    far = 4_102_444_800
    common = {
        "available": True,
        "subscription_type": "max",
        "source": "mcc",
        "expires_at": far,
        "scopes": ["user:inference"],
        "has_inference_scope": True,
        "windows": {"observed": False},
    }
    sources = {
        "claude_code": {"available": False},
        "mcc": {"available": False},
        "accounts": [
            {
                **common,
                "account_id": "uuid-read-only",
                "name": "shared@example.test",
                "origin": "claude-code",
                "origin_path": "C:/Users/someone/.claude/.credentials.json",
                "write_back": True,
                "write_back_effective": False,
                "mode": "shared",
                "read_only_reason": "write-back-off",
                "last_decision": "",
                "last_decision_at": None,
                "pending_write_back": False,
            },
            {
                **common,
                "account_id": "uuid-pending",
                "name": "pending@example.test",
                "origin": "claude-code",
                "origin_path": "C:/Users/someone/.claude/.credentials.json",
                "write_back": True,
                "write_back_effective": True,
                "mode": "shared",
                "read_only_reason": "",
                "last_decision": "shared:writeback-pending",
                "last_decision_at": 1_790_000_000,
                "pending_write_back": True,
            },
            {
                **common,
                "account_id": "uuid-native",
                "name": "native@example.test",
                "origin": "mcc",
                "mode": "native",
                "read_only_reason": "",
                "last_decision": "",
                "last_decision_at": None,
                "pending_write_back": False,
            },
        ],
        "windows": {"observed": False},
    }
    result = subprocess.run(
        [
            node,
            str(HARNESS.with_name("admin_oauth_ownership_jsdom_harness.mjs")),
            str(STATIC_DIR),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=TIMEOUT_SECONDS,
        env={**os.environ, "OAUTH_SOURCES_JSON": json.dumps(sources)},
    )
    if result.returncode != 0:
        if "Cannot find package 'jsdom'" in result.stderr:
            _missing("jsdom is not installed")
        pytest.fail(f"harness failed: {result.stderr[-2000:]}")
    card = json.loads(result.stdout)
    assert card["fatal"] is None
    assert card["scriptErrors"] == []
    rows = {row["accountId"]: row for row in card["rows"]}
    assert list(rows) == ["uuid-read-only", "uuid-pending", "uuid-native"]

    def terms(row: dict) -> dict[str, dict]:
        return {entry["term"]: entry for entry in row["terms"]}

    # The read-only shared row: its button re-reads, and it says why MCC will
    # never renew it, flagged rather than merely printed.
    read_only = rows["uuid-read-only"]
    assert read_only["refresh"] == "Re-read from Claude Code"
    details = terms(read_only)
    assert details["Owner"]["value"].startswith("Claude Code (shared)")
    assert "Read-only" in details
    assert "write-back is off" in details["Read-only"]["value"]
    assert "never refresh this shared credential" in details["Read-only"]["value"]
    assert details["Read-only"]["className"] == "value-expired"
    assert "Write-back pending" not in details
    assert read_only["pending"] == []

    # A token MCC rotated but has not written back yet: amber, and the same
    # re-read button.
    pending = rows["uuid-pending"]
    assert pending["refresh"] == "Re-read from Claude Code"
    details = terms(pending)
    assert "Read-only" not in details
    assert details["Write-back pending"]["className"] == "value-pending"
    assert "not yet written it back" in details["Write-back pending"]["value"]
    assert details["Last decision"]["value"].startswith("shared:writeback-pending")
    assert details["Last decision"]["className"] == "value-pending"
    assert pending["pending"]

    # A native row keeps today's button and claims nothing about any file.
    native = rows["uuid-native"]
    assert native["refresh"] == "Refresh now"
    details = terms(native)
    assert details["Owner"]["value"].startswith("MCC (signed in here)")
    assert "Read-only" not in details
    assert "Write-back pending" not in details
    assert native["pending"] == []


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


def test_the_lifetime_panel_says_how_far_the_history_conversion_is(rendered) -> None:
    """7.74.0: one line of text while older history is recompressed.

    Read-only, from the storage readout the panel already fetches; no control.
    """

    span = rendered["logReadout"]["lifetimeSpan"]

    assert "4.19 GB on disk · compressing older history (bodies 42%)" in span


def test_the_history_note_names_the_metadata_part(rendered) -> None:
    """7.75.0: the part that stores repeated request metadata once says so.

    The earlier parts keep their words; an unknown part from a newer server
    still reads as a conversion, never as nothing.
    """

    notes = rendered["logReadout"]["historyNotes"]

    assert notes["metadata"] == " · compressing older history (request metadata 7%)"
    assert notes["snapshots"] == " · compressing older history (snapshots 10%)"
    assert notes["bodies"] == " · compressing older history (bodies 42%)"
    assert notes["space"] == " · handing freed space back to the disk 99%"
    assert notes["done"] == ""
    assert notes["future"] == " · compressing older history (5%)"


def test_the_history_note_names_the_skipped_attempts_part(rendered) -> None:
    """7.76.0: the part that stores skipped route attempts compactly says so."""

    notes = rendered["logReadout"]["historyNotes"]

    assert notes["skipped"] == (
        " · compressing older history (skipped route attempts 63%)"
    )


def test_the_history_note_names_the_prompt_chunk_part_and_a_pause(rendered) -> None:
    """7.93.0: prompts stored as chunks say so, and a paused conversion says it is."""

    notes = rendered["logReadout"]["historyNotes"]

    assert notes["prompts"] == (
        " · compressing older history (prompts, stored as chunks 37%)"
    )
    assert notes["paused"] == " · compressing older history paused at 37%"
    assert notes["pausedSpace"] == " · compressing older history paused at 80%"
    # Nothing left to do: a pause has nothing to say.
    assert notes["pausedDone"] == ""


def test_the_size_counts_the_chunk_store_once_it_holds_prompts(rendered) -> None:
    """7.93.0: chunked prompts live in the search index file, so its size is shown."""

    notes = rendered["logReadout"]["chunkNotes"]

    assert notes["none"] == ""
    assert notes["older"] == ""
    assert notes["some"] == (
        ", plus 2.12 GB in the search index file, which holds the prompts stored as"
        " chunks"
    )


def test_the_history_conversion_can_be_paused_and_continued(rendered) -> None:
    """7.93.0: Pause / Continue beside the log's size, only while there is work."""

    button = rendered["logReadout"]["historyButton"]

    assert button["atOpen"] == {"present": True, "hidden": False, "text": "Pause"}
    assert button["paused"]["text"] == "Continue"
    assert button["paused"]["hidden"] is False
    assert button["done"]["hidden"] is True
    assert button["space"] == {"present": True, "hidden": False, "text": "Pause"}
    assert button["pausePosts"] == ["POST /admin/api/requests/history-conversion/pause"]
    assert button["afterPause"]["text"] == "Continue"
    assert "compressing older history paused at 37%" in button["spanAfterPause"]
    assert button["continuePosts"] == [
        "POST /admin/api/requests/history-conversion/continue"
    ]
    assert button["afterContinue"]["text"] == "Pause"


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


def test_jsdom_the_generation_rate_and_the_throughput_are_two_numbers(
    rendered,
) -> None:
    """One measured window, and one whole request: both are shown, and they differ.

    The winner produced 340 tokens between its first and last delta -- 500 ms
    to 1,900 ms into the attempt -- so its generation rate is 242.9 tok/s. The
    whole request took 6,812 ms for the same 340 tokens: 49.9 tok/s. The
    retired number, 188.9, was neither: it divided the tokens by
    ``duration - ttft`` of the attempt (2,100 - 300 ms), a window that starts
    when MCC released the first byte rather than when the model produced one.
    """

    pairs = dict(rendered["latencyViews"]["detailPairs"])

    assert pairs["Generation rate"] == "242.9 tok/s"
    assert pairs["Throughput (whole request)"] == "49.9 tok/s"
    assert pairs["Duration"] == "6812 ms"
    assert pairs["First visible byte (winner)"] == "300 ms"
    assert pairs["First visible byte (incl. fallbacks)"] == "4712 ms"
    assert pairs["Lost to fallbacks"].startswith("4412 ms")
    # The retired label is gone from the dialog entirely.
    assert "Output rate" not in pairs


def test_jsdom_request_chain_renders_per_attempt_latency(rendered) -> None:
    """Each attempt's own clock, on the attempt's own row."""

    views = rendered["latencyViews"]
    assert views["chainHidden"] is False
    first, second, winner, _skipped = views["attemptMetrics"]

    # The model that timed out never produced a first byte, and a dash is the
    # only honest thing to print for one.
    assert dict(first)["first byte"] == "\u2014"
    assert dict(first)["ended"] == "upstream_timeout"
    assert dict(second)["first byte"] == "1.4s"
    assert dict(winner) == {
        "first byte": "300 ms",
        "first reasoning": "460 ms",
        "after first byte": "1.8s",
        "tokens out": "340",
        # 340 tokens over the measured 1.4 s window ...
        "generation": "242.9 tok/s",
        # ... and the same tokens over this attempt's whole 2,100 ms.
        "throughput": "161.9 tok/s",
        "ended": "answered",
    }
    # A failed attempt produced no answer to count, so both rates are refused
    # rather than computed from the request's tokens -- those belong to
    # whichever model answered.
    assert dict(second)["generation"] == "\u2014"
    assert dict(second)["throughput"] == "\u2014"
    assert dict(second)["tokens out"] == "\u2014"


def test_jsdom_skipped_attempt_renders_dashes_and_its_bench_reason(rendered) -> None:
    """A model that was never asked has no latency, and says why it was not."""

    views = rendered["latencyViews"]
    skipped = dict(views["attemptMetrics"][3])

    assert [
        skipped[name] for name in ("first byte", "first reasoning", "after first byte")
    ] == [
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
        "First byte measured",
        "p50 first byte",
        "Avg first reasoning",
        "Avg after first byte",
        "Tokens out",
        "Generation rate",
        "Throughput",
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
    # A group rate is computed in SQL and refused when part of the group has
    # no measurement; the tokens are what the group reported upstream.
    assert failed[9] == "\u2014"
    assert failed[10] == "\u2014"
    assert answered[9] == "255.0 tok/s"
    assert answered[10] == "170.0 tok/s"


def test_jsdom_a_group_window_below_the_minimum_says_so(rendered) -> None:
    """Every attempt measured, every window under 500 ms: not a number."""

    panel = rendered["latencyViews"]["panel"]
    short = next(row for row in panel["rows"] if row[0] == "gamma/model-03")

    assert short[4] == "5"
    assert short[9] == "not measured \u2014 shorter than the timing window"
    # Throughput needs no window, so the group still has a rate to show.
    assert short[10] == "41.2 tok/s"


def test_jsdom_a_group_written_before_the_clock_keeps_its_throughput(rendered) -> None:
    """Legacy rows are not given a generation rate; they are given the one they have."""

    panel = rendered["latencyViews"]["panel"]
    legacy = next(row for row in panel["rows"] if row[0] == "delta/model-04")

    assert legacy[9] == "\u2014"
    assert legacy[10] == "22.5 tok/s"


def test_jsdom_the_display_rules_for_the_rows_that_cannot_carry_a_rate(
    rendered,
) -> None:
    """Short window, no clock, and an estimate: each says which it is.

    These are the same functions the dialog calls, driven with one row each,
    because the three cases cannot all be the fallback fixture at once.
    """

    rules = rendered["latencyViews"]["rateRules"]

    assert rules["short"] == "not measured \u2014 shorter than the timing window"
    assert rules["legacy"] == "not measured \u2014 no generation clock before 7.122.2"
    # The legacy row still carries a throughput, which is the whole reason the
    # second number exists.
    assert rules["legacyThroughput"] == "161.9 tok/s"
    # Without the server's minimums the page refuses to compute a rate rather
    # than keeping a private copy of them.
    assert rules["noMinimums"] == "\u2014"
    # An estimated count suppresses both rates and marks the number instead.
    assert rules["estimated"] == "not measured \u2014 the token count is an estimate"
    assert rules["estimatedThroughput"] == (
        "not measured \u2014 the token count is an estimate"
    )
    assert rules["estimatedTokens"] == "~1,240 est."
    assert rules["attemptRate"] == "242.9 tok/s"
    assert rules["attemptThroughput"] == "161.9 tok/s"


def test_jsdom_unmeasured_latency_renders_dashes_not_zeroes(rendered) -> None:
    """Every attempt written before 7.4.0 is unmeasured and unbackfillable."""

    views = rendered["latencyViews"]
    unmeasured = next(
        row for row in views["panel"]["rows"] if row[0] == "beta/model-07"
    )

    assert unmeasured[2] == "4,416"
    assert unmeasured[4] == "0"
    # p50, avg first reasoning, avg after first byte, tokens out, generation
    # rate and throughput: six cells with nothing measured behind any of them.
    assert unmeasured[5:] == ["\u2014"] * 6
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
        "last 7d: p50 first byte 312 ms, p95 1.4s over 14 of 15 attempts"
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


def test_jsdom_the_card_says_how_its_provider_routes_right_now(rendered) -> None:
    """7.78.8: refused by its own Direct-fallback rule, and saved-not-routing.

    Each is the server's own sentence, shown as given; a card the server says
    nothing about carries no note at all.
    """

    proxying = rendered["proxying"]
    notes = proxying["routingNotes"]

    assert [note["cls"] for note in notes] == [
        "proxy-routing-note proxy-routing-refused",
        "proxy-routing-note proxy-routing-stale",
    ]
    assert notes[0]["text"].startswith("Not sent: NVIDIA NIM's proxy chain")
    assert "Direct fallback is off" in notes[0]["text"]
    assert notes[1]["text"].startswith("Saved -- not routing yet")
    assert proxying["routingNotesOnTheOtherCard"] == 0


def test_jsdom_each_card_says_what_its_provider_can_see(rendered) -> None:
    """7.79.2 (spec C.5): through the chain, never through it, and Direct."""

    box = rendered["proxying"]["visibility"]
    assert box is not None
    assert box["title"] == "What NVIDIA NIM can see"
    lines = {line["cls"].split()[-1]: line["text"] for line in box["lines"]}
    direct = lines["proxy-visibility-direct"]
    assert direct.startswith("Direct fallback on: NVIDIA NIM sees this machine")
    assert "only once every proxy in this chain is unhealthy" in direct
    assert "through the system proxy corp-proxy.test:3128" in direct
    assert lines["proxy-visibility-through"].startswith("Through it: model requests")
    assert "model listing (hourly and Test)" in lines["proxy-visibility-through"]
    never = lines["proxy-visibility-never"]
    assert "web search and web fetch" in never
    assert "signing in" not in never  # an API key, not a sign-in
    assert "corp-proxy.test:3128" in lines["proxy-visibility-system"]

    other = rendered["proxying"]["visibilityOnTheOtherCard"]
    assert other is not None
    texts = [line["text"] for line in other["lines"]]
    assert texts[0] == ("Not in use: ChatGPT (OAuth) sees this machine's own address.")
    assert any(text.startswith("Never through it: signing in") for text in texts)


def test_jsdom_an_unreadable_chain_file_raises_the_red_banner(rendered) -> None:
    banner = rendered["proxying"]["storeBanner"]

    assert banner["present"] is True
    assert banner["hidden"] is False
    assert banner["title"] == "The proxy chain file cannot be read."
    assert "proxy_chains.json cannot be parsed" in banner["text"]
    assert "does not rewrite the file" in banner["text"]


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


def test_jsdom_the_two_proxy_speed_fields_render_with_their_help(rendered) -> None:
    """7.94.0: the keep-alive and the recency half-life, each on the card the
    other proxy tunables live on, with a number input, its range and its help.
    """

    fields = rendered["limits"]["proxySpeedFields"]
    for key in PROXY_SPEED_FIELDS:
        shown = fields[key]
        shipped = FIELD_BY_KEY[key]
        assert shown is not None, f"{key} did not render"
        # What renders is what the manifest ships, word for word.
        assert shown["card"] == f"section-{shipped.section_id}"
        assert shown["label"] == shipped.label
        assert shown["help"] == " ".join(shipped.description.split())
        assert shown["inputType"] == "number"
        assert shown["min"] == f"{shipped.minimum:g}"
        assert shown["max"] == f"{shipped.maximum:g}"
        assert shown["advancedTag"]
        assert f"desc-{key}" in shown["describedBy"].split()
    assert "60 is recommended" in fields["PROXY_KEEPALIVE_SECONDS"]["help"]
    recency = fields["PROXY_ORDER_RECENCY_HALF_LIFE_MINUTES"]["help"]
    assert recency.startswith(
        "0 -- what ships -- orders a chain by measured speed only"
    )


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
    """Headed groups, decisions first: the limits and the reasoning word are
    statements about a decision, and the nine below them are fields of a
    request body (7.101.0: groups of one table, under its four column heads).
    """

    editor = rendered["models"]["preferencesWithGrid"]

    assert editor["heads"] == [
        "Field",
        "Value MCC uses",
        "Where it came from",
        "Your override · default",
    ]
    assert editor["groups"] == ["limits", "reasoning", "request"]
    assert editor["groupHeads"] == [
        "Limits",
        "Reasoning",
        "Request parameters (sent in the body)",
    ]
    assert editor["fields"] == [
        "context_length",
        "max_output_tokens",
        "reasoning_preference",
        "temperature",
    ]
    # Three-state, the same idiom as every other row on the form -- the
    # context window (7.87.0) is on model rows only.
    assert rendered["models"]["preferences"]["0"]["modes"] == [
        "inherit",
        "inherit",
        "inherit",
    ]
    assert rendered["models"]["providerPreferences"]["modes"] == [
        "inherit",
        "inherit",
    ]


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
    # 7.101.0: the limit and its rung are the row's default line.
    assert editor["defaults"]["output"][0] == (
        "default 40,960 — models.dev bucket, exact id"
    )


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


# The context window (7.87.0): one more three-state row on a model's editor,
# beside max_output_tokens, with the extracted window and its rung shown.


def test_jsdom_the_context_row_shows_the_extracted_window_and_its_rung(
    rendered,
) -> None:
    ladder = rendered["models"]["contextWindow"]["ladder"]

    assert ladder["present"] is True
    assert ladder["field"] == "context_length"
    assert ladder["group"] == "limits"
    assert ladder["label"] == "context length"
    assert ladder["fileKey"] == "context_length"
    # The two numbers that bound a model, side by side in Limits.
    assert ladder["next"] == "max_output_tokens"
    assert ladder["mode"] == "inherit"
    assert ladder["max"] is None  # no upper bound, unlike the output cap
    # 7.101.0: "Also stated" is the default line, under the control.
    assert ladder["defaults"] == ["default 524,288 — provider /models or models.dev"]
    assert ladder["cells"][1] == "524,288"
    assert "Force value replaces it for routing" in ladder["note"]


def test_jsdom_a_forced_window_keeps_the_extracted_one_beside_it(rendered) -> None:
    forced = rendered["models"]["contextWindow"]["forced"]
    unknown = rendered["models"]["contextWindow"]["unknown"]
    nothing = rendered["models"]["contextWindow"]["nothing"]

    assert forced["mode"] == "value"
    assert forced["value"] == "1000000"
    assert forced["forced"] is True
    assert forced["cells"][1] == "1,000,000"
    assert forced["defaults"] == ["default 524,288 — provider /models or models.dev"]
    assert unknown["mode"] == "unset"
    assert unknown["defaults"] == ["default 524,288 — provider /models or models.dev"]
    assert nothing["defaults"] == [
        "default not reported — asked: provider /models, models.dev "
        "(own bucket, reference, vote), OpenRouter live"
    ]


def test_jsdom_the_context_row_is_on_model_rows_only(rendered) -> None:
    assert rendered["models"]["contextWindow"]["provider"]["present"] is False


def test_jsdom_a_fractional_window_is_refused_before_saving(rendered) -> None:
    refused = rendered["models"]["contextWindow"]["ladder"]["refused"]

    assert "context_length must be a whole number of tokens" in refused


def test_jsdom_the_panel_badges_the_operator_and_shows_the_extracted_window(
    rendered,
) -> None:
    panel = rendered["models"]["contextWindowPanel"]

    assert panel is not None
    assert panel["cells"][1] == "1,000,000"
    assert "models-source-operator" in panel["badge"]
    assert "operator override" in panel["cells"][2]
    assert "also stated: 524,288 (provider /models or models.dev)" in panel["cells"][2]


# --------------------------------------------------------------------------- #
# One table per model (7.101.0, X2)
#
# The user, 2026-10-10: every field, the overridable ones included, looks like
# the read-only "What MCC knows about this model" table -- field, value, rung
# badge, provenance -- with the override control inline and the provider's or
# the ladder's DEFAULT always visible beside it. One run of
# admin_jsdom_models_fields.mjs (MCC_JSDOM_SCENARIO=models_fields) drives it on
# a payload in the server's own shapes. The same file was run once against the
# 7.100.0 page; what that page drew and sent is
# tests/fixtures/models_fields_7_100_0.json, and the new page must still draw
# every row of it and send every body of it byte for byte.
# --------------------------------------------------------------------------- #

MODELS_FIELDS_BEFORE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "models_fields_7_100_0.json"
    ).read_text(encoding="utf-8")
)
REF_A = "custom_agnes/agnes-3.0-flash"
REF_B = "custom_agnes/agnes-2.5-flash"
REF_C = "custom_agnes/agnes-1.5-pro"
NINE = [
    "frequency_penalty",
    "min_p",
    "presence_penalty",
    "repetition_penalty",
    "seed",
    "stop",
    "temperature",
    "top_k",
    "top_p",
]
OVERRIDABLE = ["context_length", "max_output_tokens", "reasoning_preference", *NINE]


@pytest.fixture(scope="module")
def models_fields() -> dict:
    return _run(MCC_JSDOM_SCENARIO="models_fields")


def _field_rows(models_fields: dict, ref: str) -> dict[str, dict]:
    table = models_fields["models"][ref]["table"]
    assert table is not None, f"{ref}: no models-fields table drawn"
    return {row["field"]: row for row in table["rows"]}


def _rung_once(note: str) -> str:
    """F3: "(provider /models, provider /models, exact id)" names its rung once."""

    return re.sub(r"\(([^,()]+), (\1[^()]*)\)", r"(\2)", note)


def test_jsdom_the_models_fields_scenario_runs_clean(models_fields) -> None:
    assert models_fields["fatal"] is None
    assert models_fields["scenarioFatal"] is None
    assert models_fields["scriptErrors"] == []
    assert models_fields["consoleErrors"] == []


def test_jsdom_every_capability_row_is_still_drawn_in_the_fields_table(
    models_fields,
) -> None:
    """C1/C8.4: every row 7.100.0's "What MCC knows" panel drew is still drawn,
    with the same value text, badge class and text, rung line, guessed warning,
    resolver sentence and second statement (that one naming its rung once).

    The one row whose value cells now hold something else is an output limit
    you capped: the cells say your cap (C2), and the row the panel drew moves,
    whole, under the default line of its control.
    """

    compared = moved = 0
    for ref, old_rows in MODELS_FIELDS_BEFORE["capability_rows"].items():
        assert old_rows, f"{ref}: vacuous golden"
        new_rows = models_fields["models"][ref]["table"]["rows"]
        by_label = {row["label"]: row for row in new_rows}
        for old in old_rows:
            new = by_label.get(old["label"])
            assert new is not None, f"{ref}: {old['label']!r} is no longer drawn"
            if (
                new["field"] == "max_output_tokens"
                and new["badgeClass"] != old["badgeClass"]
            ):
                assert "models-source-operator" in new["badgeClass"], (ref, new)
                assert any(old["value"] in line for line in new["defaults"]), (ref, new)
                assert {
                    "className": old["badgeClass"],
                    "text": old["badgeText"],
                } in new["defaultSources"], (ref, new)
                for note in old["notes"]:
                    assert _rung_once(note) in new["defaultNotes"], (ref, note, new)
                moved += 1
                continue
            assert new["value"] == old["value"], (ref, old, new)
            assert new["badgeClass"] == old["badgeClass"], (ref, old, new)
            assert new["badgeText"] == old["badgeText"], (ref, old, new)
            assert new["notes"] == [_rung_once(note) for note in old["notes"]], (
                ref,
                old,
                new,
            )
            compared += 1
    # Neither branch of the comparison may be vacuous: the fixture caps the
    # output limit on all three models (A on its own row, B and C through the
    # provider row) and draws 67 other rows.
    assert moved == 3
    assert compared == 67


def test_jsdom_what_the_effective_table_said_is_on_each_row(models_fields) -> None:
    """7.100.0's "What this forces onto the request", row for row: the value
    (or "removed from the body") and which row forced it."""

    for ref, old_rows in MODELS_FIELDS_BEFORE["effective_rows"].items():
        rows = _field_rows(models_fields, ref)
        assert old_rows, f"{ref}: vacuous golden"
        for name, value, origin in old_rows:
            assert rows[name]["value"] == value, (ref, name)
            assert rows[name]["notes"] == [origin], (ref, name)
            assert rows[name]["forced"] is True, (ref, name)


def test_jsdom_each_overridable_row_shows_its_default_in_every_mode(
    models_fields,
) -> None:
    """C2: the default line is drawn whatever the mode select says, for the
    context window, the output limit, the reasoning word and each of the nine
    -- on a model with the provider's default pairs, one whose provider
    published none and one whose list pins nothing -- and an override never
    hides it."""

    sweep = models_fields["modeSweep"]
    seen = {(entry["ref"], entry["field"], entry["mode"]) for entry in sweep}
    for ref in (REF_A, REF_B, REF_C):
        for field in OVERRIDABLE:
            for mode in ("inherit", "unset", "value"):
                assert (ref, field, mode) in seen, (ref, field, mode)
    for entry in sweep:
        assert entry["defaults"], entry
        assert entry["defaults"][0].startswith("default "), entry
        assert entry["allShown"] is True, entry
        assert entry["boxDisabled"] is (entry["mode"] != "value"), entry
    # The line does not depend on the mode: one text per row.
    by_row: dict[tuple[str, str], set[tuple[str, ...]]] = {}
    for entry in sweep:
        by_row.setdefault((entry["ref"], entry["field"]), set()).add(
            tuple(entry["defaults"])
        )
    assert all(len(texts) == 1 for texts in by_row.values()), by_row


def test_jsdom_a_sampling_default_comes_from_the_gateway_pairs(models_fields) -> None:
    rows = _field_rows(models_fields, REF_A)

    assert rows["temperature"]["defaults"][0] == (
        "default 0.7 — provider /models (default_parameters)"
    )
    assert rows["top_p"]["defaults"][0] == (
        "default 0.95 — provider /models (default_parameters)"
    )
    # Your 0.6 is what MCC sends; the provider's 0.7 is still beside it.
    assert rows["temperature"]["value"] == "0.6"


def test_jsdom_a_sampling_row_with_no_pair_says_not_reported_and_where_it_asked(
    models_fields,
) -> None:
    no_list = _field_rows(models_fields, REF_B)
    empty_list = _field_rows(models_fields, REF_C)
    unpaired = _field_rows(models_fields, REF_A)

    for name in NINE:
        assert no_list[name]["defaults"][0] == (
            "default not reported — only the provider's /models states one"
        )
        assert empty_list[name]["defaults"][0] == (
            "default not reported — provider /models (default_parameters) "
            "pins no value for it"
        )
    assert unpaired["seed"]["defaults"][0] == empty_list["seed"]["defaults"][0]


def test_jsdom_supported_parameters_annotate_the_sampling_rows(models_fields) -> None:
    listed = _field_rows(models_fields, REF_A)
    no_list = _field_rows(models_fields, REF_B)

    assert listed["temperature"]["defaults"][1] == (
        "this provider lists it as accepted (shown, never acted on)"
    )
    assert listed["seed"]["defaults"][1] == (
        "this provider does not list it (shown, never acted on)"
    )
    # No list, no sentence.
    assert all(len(no_list[name]["defaults"]) == 1 for name in NINE)


def test_jsdom_the_output_default_is_the_routing_limit_and_openrouter_live_is_a_second_line(
    models_fields,
) -> None:
    capped = _field_rows(models_fields, REF_A)["max_output_tokens"]
    bucket = _field_rows(models_fields, REF_B)["max_output_tokens"]
    live = _field_rows(models_fields, REF_C)["max_output_tokens"]

    assert capped["defaults"][0] == (
        "default not reported — asked: provider /models, models.dev "
        "(own bucket, reference, vote)"
    )
    assert bucket["defaults"][0] == "default 40,960 — models.dev bucket, exact id"
    # The number routing bounds by, then OpenRouter's, which is only shown.
    assert live["defaults"][0] == "default 32,768 — provider /models, exact id"
    assert live["defaults"][1] == (
        "OpenRouter live also states 131,072 (shown, never acted on)"
    )
    # The cap sentence is kept under the control.
    assert capped["controlNotes"] == [
        "Nothing publishes an output limit for this model, so your number "
        "becomes the limit."
    ]
    assert bucket["controlNotes"][0].startswith("A cap, not a request")


def test_jsdom_save_posts_the_same_body_in_the_same_key_order(models_fields) -> None:
    """F1/C4/C8.1: the overrides file keeps the order a body names NEW keys
    in, so the body must be 7.100.0's byte for byte -- the preferences, the
    context window (model rows), then the nine -- whatever the drawing order.
    """

    assert models_fields["posts"] == MODELS_FIELDS_BEFORE["posts"]
    body = json.loads(models_fields["posts"][1]["body"])
    assert body["scope"] == "model"
    assert body["key"] == REF_A
    assert list(body["updates"]) == [
        "max_output_tokens",
        "reasoning_preference",
        "context_length",
        *NINE,
    ]
    assert body["updates"]["temperature"] == 0.8
    assert body["updates"]["top_p"] is None
    assert body["updates"]["context_length"] == 200000
    assert body["updates"]["reasoning_preference"] == "high"
    provider = json.loads(models_fields["posts"][2]["body"])
    assert list(provider["updates"]) == [
        "max_output_tokens",
        "reasoning_preference",
        *NINE,
    ]


def test_jsdom_refused_saves_say_what_they_said_before(models_fields) -> None:
    refused = models_fields["refused"]

    assert {name: answer["status"] for name, answer in refused.items()} == (
        MODELS_FIELDS_BEFORE["refused"]
    )
    assert models_fields["postsAfterRefusals"] == 0


def test_jsdom_forced_rows_are_marked_and_keep_their_default(models_fields) -> None:
    a = _field_rows(models_fields, REF_A)
    b = _field_rows(models_fields, REF_B)

    forced = [
        a["temperature"],
        a["max_output_tokens"],
        a["top_k"],
        b["context_length"],
        b["top_p"],
    ]
    for row in forced:
        assert row["forced"] is True, row
        assert "models-source-operator" in row["badgeClass"], row
        assert row["defaults"] and row["defaults"][0].startswith("default "), row
    assert a["top_p"]["forced"] is False
    assert a["context_length"]["forced"] is False
    assert b["top_p"]["value"] == "removed from the body"
    assert b["context_length"]["value"] == "1,000,000"
    assert b["context_length"]["defaults"] == [
        "default 1,048,576 — provider /models, exact id"
    ]
    # The cap came from the provider's row, and says so.
    assert b["max_output_tokens"]["value"] == "8,000"
    assert "this provider's override row" in b["max_output_tokens"]["notes"][0]


def test_jsdom_the_provider_table_has_no_context_row_and_says_per_model(
    models_fields,
) -> None:
    provider = models_fields["provider"]
    table = provider["table"]
    rows = {row["field"]: row for row in table["rows"]}

    assert provider["summary"] == "Parameter overrides for every custom_agnes model"
    assert table["head"][1] == "Set on this provider"
    assert list(rows) == ["max_output_tokens", "reasoning_preference", *NINE]
    assert "context_length" not in rows
    for row in rows.values():
        assert row["defaults"] == [
            "default per model — open a model to see its default"
        ]
        assert row["overridable"] is True
    assert rows["top_k"]["value"] == "40"
    assert rows["max_output_tokens"]["value"] == "8,000"
    assert rows["temperature"]["value"] == "not set"
    # The provider's own controls, under 7.100.0's ids and modes.
    assert [
        {"label": row["label"], "modeId": row["modeId"], "mode": row["mode"]}
        for row in table["rows"]
    ] == [
        {
            **old,
            "label": old["label"]
            if old["label"] != "max_output_tokens"
            else "output limit",
        }
        for old in MODELS_FIELDS_BEFORE["provider_editor_controls"]
    ]
    # A save repaints the provider's own column from the server's answer.
    after = {
        row["field"]: row for row in models_fields["afterSaveProvider"]["table"]["rows"]
    }
    assert after["temperature"]["value"] == "0.5"
    assert after["max_output_tokens"]["value"] == "forced unset"
    assert models_fields["saveProvider"]["status"] == "Saved"


def test_jsdom_labels_ids_and_aria_are_kept(models_fields) -> None:
    """C6: each mode select keeps 7.100.0's id and is labelled by its row's
    header; each value box keeps its aria-label; the default line describes
    both; mode comes before value in the tab order; Force value focuses the
    box; group headers head their row group."""

    labels = {
        "context_length": "context window",
        "max_output_tokens": "max output tokens",
        "reasoning_preference": "reasoning preference",
    }
    for ref in (REF_A, REF_B, REF_C):
        table = models_fields["models"][ref]["table"]
        rows = [row for row in table["rows"] if row["overridable"]]
        assert [row["field"] for row in rows] == OVERRIDABLE
        old_ids = {
            row["modeId"] for row in MODELS_FIELDS_BEFORE["editor_controls"][ref]
        }
        assert {row["modeId"] for row in rows} == old_ids
        old_modes = {
            row["modeId"]: row["mode"]
            for row in MODELS_FIELDS_BEFORE["editor_controls"][ref]
        }
        for row in rows:
            assert row["thScope"] == "row", row
            assert row["labelFor"] == row["modeId"], row
            assert row["labelForResolves"] is True, row
            assert row["mode"] == old_modes[row["modeId"]], row
            assert row["boxAria"] == labels.get(
                row["field"], f"{row['field']} value"
            ), row
            assert row["boxId"] == re.sub(r"-mode$", "-value", row["modeId"]), row
            assert row["tabOrder"][:2] == [row["modeId"], row["boxId"]], row
            described = " ".join(row["defaults"])
            assert row["boxDescribedBy"] == [described], row
            assert row["modeDescribedBy"] == [described], row
        assert table["headScopes"] == ["col", "col", "col", "col"]
        assert {group["headScope"] for group in table["groups"]} == {"rowgroup"}
    assert (
        models_fields["focusAfterForceValue"]
        == "ov-model-custom_agnes-agnes-1-5-pro-seed-value"
    )
    assert models_fields["focusBoxDisabled"] is False
    assert models_fields["focusBoxDisabledAgain"] is True


def test_jsdom_read_only_rows_are_grouped_below_and_say_so(models_fields) -> None:
    """C3 and the user's decision 4: prices, dates and what the model is sit
    in the same table, grouped below the override rows, each with a "read-only"
    a screen reader hears; only the endpoint pin has a control of its own."""

    table = models_fields["models"][REF_A]["table"]
    assert [group["group"] for group in table["groups"]] == [
        "limits",
        "reasoning",
        "request",
        "surface",
        "model",
        "prices",
        "dates",
        "lists",
    ]
    assert [group["head"] for group in table["groups"]][3:] == [
        "Wire surface",
        "What the model is",
        "Prices",
        "Dates",
        "Gateway lists",
    ]
    for row in table["rows"]:
        if row["overridable"]:
            assert row["srOnly"] == [], row
        elif row["field"] == "response_surface":
            assert row["surfaceEditor"] is True
        else:
            assert row["readonly"] is True, row
            assert row["srOnly"] == ["read-only"], row
            assert row["defaults"] == [], row


def test_jsdom_a_collapsed_provider_renders_no_field_tables(models_fields) -> None:
    """C7, the 186,290-node lesson: nothing is built for a provider or a model
    nobody opened."""

    collapsed = models_fields["collapsed"]
    assert collapsed["providers"] == ["custom_agnes", "zeta"]
    assert collapsed["selects"] == 0
    assert collapsed["fieldTables"] == 0
    opened = models_fields["openProvider"]
    assert opened["modelRows"] == 3
    assert opened["filledBodies"] == 0
    assert opened["modelSelects"] == 0
    assert opened["modelFieldTables"] == 0
    assert opened["zetaSelects"] == 0
    assert opened["zetaTables"] == 0


def test_jsdom_also_stated_names_its_rung_once(models_fields) -> None:
    """F3: 7.100.0 wrote "(provider /models, provider /models, exact id)"."""

    a = _field_rows(models_fields, REF_A)
    b = _field_rows(models_fields, REF_B)

    assert (
        "also stated: The provider's own words. (provider /models, exact id)"
        in (a["description"]["notes"])
    )
    assert (
        "also stated: 1,048,576 (provider /models, exact id)"
        in b["context_length"]["notes"]
    )
    for ref in (REF_A, REF_B, REF_C):
        for row in models_fields["models"][ref]["table"]["rows"]:
            for text in [*row["notes"], *row["defaults"]]:
                assert not re.search(r"\(([^,()]+), \1", text), (ref, text)


def test_jsdom_the_override_chip_counts_every_forced_key(models_fields) -> None:
    """The user's decision 2 (2026-10-18): the chip counts every key you set
    that reaches the model -- the output cap, the context window and the
    reasoning word too, from the model's row and the provider's. 7.100.0
    counted the nine body parameters only."""

    assert MODELS_FIELDS_BEFORE["chips"] == {
        REF_A: "2 override(s) active",
        REF_B: "2 override(s) active",
        REF_C: "1 override(s) active",
    }
    chips = {ref: entry["chip"] for ref, entry in models_fields["models"].items()}
    assert chips == {
        # temperature, top_k (provider row), max_output_tokens
        REF_A: "3 override(s) active",
        # top_p, top_k (provider row), context_length, max_output_tokens (provider row)
        REF_B: "4 override(s) active",
        # top_k (provider row), reasoning_preference, max_output_tokens (provider row)
        REF_C: "3 override(s) active",
    }
    # After model A's save: max_output_tokens, temperature, reasoning_preference,
    # context_length, top_p, and the provider's top_k.
    assert models_fields["afterSaveA"]["model"]["chip"] == "6 override(s) active"


def test_jsdom_a_save_repaints_the_values_and_keeps_the_controls(models_fields) -> None:
    after = models_fields["afterSaveA"]
    rows = {row["field"]: row for row in after["model"]["table"]["rows"]}

    assert models_fields["saveA"]["status"] == "Saved"
    assert models_fields["saveA"]["statusConnected"] is True
    assert after["sameBody"] is True
    assert after["sameModeControl"] is True
    assert rows["temperature"]["value"] == "0.8"
    assert rows["top_p"]["value"] == "removed from the body"
    assert rows["context_length"]["value"] == "200,000"
    assert rows["context_length"]["defaults"] == [
        "default 524,288 — provider /models, exact id"
    ]
    assert rows["reasoning_preference"]["value"] == "High"


def test_jsdom_the_surface_pin_keeps_its_own_button_and_write(models_fields) -> None:
    """The user's decision 1: "Which endpoint to use" keeps its own button,
    inside the Wire surface row, and writes through its own route."""

    surface = models_fields["surface"]
    assert surface["present"] is True
    assert surface["inFieldRow"] is True
    assert surface["button"] == {
        "text": "Pin endpoint",
        "className": "ghost-button models-surface-save",
    }
    assert surface["options"] == ["", "chat_completions", "responses"]
    assert models_fields["posts"][-1] == {
        "path": "/admin/api/model-admin/surface",
        "body": '{"key":"custom_agnes/agnes-3.0-flash","surface":"responses"}',
    }
    # Its cell survives the repaint, so its "Saved" is on screen (7.100.0
    # wrote it into a node the repaint had discarded).
    assert surface["status"] == "Saved"
    assert surface["statusConnected"] is True


def test_jsdom_the_media_block_and_the_mcp_notes_are_untouched(models_fields) -> None:
    """7.98.0's media request fields block and 7.99.0/7.100.0's MCP notes are
    drawn by code this change never touches: byte for byte what 7.100.0 drew
    for the same payload."""

    assert (
        models_fields["mediaSection"]["sha256"]
        == (MODELS_FIELDS_BEFORE["media_section_sha256"])
    )
    assert models_fields["mcpNotes"] == MODELS_FIELDS_BEFORE["mcp_notes"]
    assert models_fields["mcpNotes"]["count"] == 4


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


# ------------------------------------------------------- provider card faces
# 7.78.0: a keyless OpenCode Zen serves its free models on the anonymous
# credential, and a fresh install routes four Claude tiers there, so its card
# says so instead of "Missing key" in amber.


def test_a_keyless_zen_card_says_free_models_are_ready(rendered) -> None:
    face = rendered["providerFaces"]["freeReady"]

    assert face["pill"] == "Free models ready"
    assert face["pillClass"] == "status-pill ok"
    assert face["meta"] == (
        "Configured by default: free models work with no key. A key is needed "
        "only for paid models and for OpenCode Go."
    )
    # A sentence, so it is set as prose rather than as a variable name.
    assert face["metaClass"] == "provider-meta provider-meta-prose"
    assert face["configured"] == "true"


def test_the_opt_out_and_a_key_keep_their_old_card_faces(rendered) -> None:
    faces = rendered["providerFaces"]

    assert faces["optedOut"] == {
        "pill": "Missing key",
        "pillClass": "status-pill warn",
        "meta": "OPENCODE_API_KEY",
        "metaClass": "provider-meta",
        "configured": "false",
    }
    assert faces["keyed"] == {
        "pill": "Configured",
        "pillClass": "status-pill ok",
        "meta": "1 key",
        "metaClass": "provider-meta",
        "configured": "true",
    }


def test_only_configured_keeps_a_free_ready_card(rendered) -> None:
    faces = rendered["providerFaces"]

    assert faces["onlyConfigured"] == ["Free models ready", "Configured"]
    assert faces["onlyConfiguredCount"] == "2 of 3"
    assert faces["allCount"] == "3 providers · 2 configured"


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


# ------------------------------------------------- Key filter by name (7.78.1)
# The user (2026-10-06): searching requests by key showed the masked key even
# for keys they had named -- "for unnamed keys keep their key and for named
# ones show the name". The rows were already named; the Key box was not. What
# leaves the page must stay the mask: a row stores `key_label`, never a name,
# and the store matches it with `key_label = ?`.

KF_A = "nvap\u20261111"  # "Work laptop"
KF_B = "nvap\u20262222"  # unnamed
KF_C = "nvap\u20263333"  # "Team old", renamed "Team shared"
KF_D = "nvap\u20264444"  # "Team shared"


def _texts(choices: list[dict]) -> dict[str, str]:
    return {choice["label"]: choice["text"] for choice in choices}


def test_the_key_choice_list_names_named_keys_and_keeps_masks(rendered) -> None:
    unit = rendered["keyFilter"]["unit"]

    assert _texts(unit["plain"]) == {
        KF_A: "Work laptop",
        KF_B: KF_B,
        KF_C: "Team old",
    }
    # No names at all: every choice is exactly the mask it always was.
    assert _texts(unit["noNames"]) == {KF_A: KF_A, KF_B: KF_B, "unknown": "unknown"}
    assert [choice["label"] for choice in unit["duplicatesAndBlanks"]] == [KF_A, KF_B]


def test_keys_that_would_read_the_same_stay_distinguishable(rendered) -> None:
    unit = rendered["keyFilter"]["unit"]

    # Two keys given one name read as the Key breakdown reads them.
    assert _texts(unit["sharedName"]) == {
        KF_A: "Work laptop",
        KF_B: KF_B,
        KF_C: f"Team shared ({KF_C})",
        KF_D: f"Team shared ({KF_D})",
    }
    # A name that is another key's mask cannot take that mask's place.
    assert _texts(unit["nameIsAMask"]) == {KF_A: f"{KF_B} ({KF_A})", KF_B: KF_B}
    assert _texts(unit["nameIsItsOwnMask"]) == {KF_A: KF_A}
    # Even a name spelled like a disambiguated choice leaves every text unique.
    for case in unit.values():
        texts = [choice["text"] for choice in case]
        assert len(texts) == len(set(texts)), case
    assert _texts(unit["nameLooksDisambiguated"]) == {
        KF_A: KF_A,
        KF_C: KF_C,
        KF_D: f"Team shared ({KF_D})",
    }


def test_the_key_box_offers_names_and_queries_the_mask(rendered) -> None:
    kf = rendered["keyFilter"]

    # The rendered list: names for named keys, the mask for the unnamed one,
    # and no mask riding along as a label.
    assert sorted(kf["options"]) == sorted(
        [[KF_B, None], ["Team old", None], ["Team shared", None], ["Work laptop", None]]
    )
    assert kf["unfilteredKey"] is None
    picked = kf["picked"]
    # Equality with before: the base offered the mask itself as the value, so
    # it sent `key=<mask>`. Each pick now sends that same mask -- the same
    # query, so the same rows.
    assert picked["named"] == {
        "box": "Work laptop",
        "title": KF_A,
        "key": KF_A,
        "persisted": KF_A,
    }
    assert picked["unnamed"] == {
        "box": KF_B,
        "title": None,
        "key": KF_B,
        "persisted": KF_B,
    }
    assert picked["renamedBefore"]["key"] == KF_C
    assert picked["renamedBefore"]["box"] == "Team old"


def test_a_key_renamed_while_it_is_the_filter_stays_the_filter(rendered) -> None:
    kf = rendered["keyFilter"]

    after = kf["renamedWhileActive"]
    assert after["key"] == KF_C
    assert after["box"] == f"Team shared ({KF_C})"
    assert after["title"] == KF_C
    assert sorted(after["options"]) == sorted(
        [
            [KF_B, None],
            [f"Team shared ({KF_C})", None],
            [f"Team shared ({KF_D})", None],
            ["Work laptop", None],
        ]
    )
    # Two keys that share a name are separately selectable.
    assert kf["picked"]["renamedAfter"]["key"] == KF_C
    assert kf["picked"]["sharedOther"]["key"] == KF_D


def test_typed_saved_and_exported_key_filters_still_mean_the_mask(rendered) -> None:
    kf = rendered["keyFilter"]

    # A full mask typed by hand queries it, then reads as the key's name.
    assert kf["typedMask"]["key"] == KF_A
    assert kf["typedMaskAfterLoad"] == {"box": "Work laptop", "title": KF_A}
    # Unknown text goes out as typed, exactly as before.
    assert kf["halfTyped"]["key"] == "Work"
    # A saved view (always a mask) is restored, queried and then named.
    assert kf["restoredRaw"] == KF_A
    assert kf["restored"] == {"box": "Work laptop", "key": KF_A}
    assert kf["exportKey"] == KF_A
    assert kf["cleared"] == {"box": "", "title": None, "key": None, "persisted": None}


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
#: 7.94.0's two proxy speed settings ship ``advanced`` too, and the harness
#: renders them on the same card, so the card's advanced set is both groups.
PROXY_SPEED_FIELDS = (
    "PROXY_KEEPALIVE_SECONDS",
    "PROXY_ORDER_RECENCY_HALF_LIFE_MINUTES",
)
ADVANCED_ON_THE_CARD = (*COOLDOWN_FIELDS, *PROXY_SPEED_FIELDS)
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


MEDIA_SHA = {seed: (seed * 32)[:64] for seed in ("a1", "b2", "c3", "d4", "e5")}


def test_jsdom_request_modal_media_metadata_always_preview_only_when_stored(
    rendered,
) -> None:
    """7.68.0: every media row states its facts; only a stored file is shown."""

    media = rendered["requestMedia"]
    assert media["present"] is True
    assert media["hidden"] is False
    assert media["heading"] == "Media (6)"
    items = media["items"]
    assert [item["direction"] for item in items] == [
        "in",
        "out",
        "out",
        "out",
        "out",
        "out",
    ]
    assert [item["preview"] for item in items] == [
        None,  # an upload: never stored
        "img",
        "audio",
        "video",
        None,  # stored, but not a picture, a sound or a film
        None,  # not a content address: nothing is fetched for it
    ]
    image, sound, film = items[1], items[2], items[3]
    assert image["src"] == f"/admin/api/media/{MEDIA_SHA['b2']}"
    assert image["loading"] == "lazy"
    assert sound["src"] == f"/admin/api/media/{MEDIA_SHA['c3']}"
    assert (sound["preload"], sound["controls"]) == ("none", True)
    assert film["src"] == f"/admin/api/media/{MEDIA_SHA['d4']}"
    assert (film["preload"], film["controls"]) == ("none", True)
    assert items[0]["missing"] == "file not stored"
    assert items[4]["missing"] == "no preview for this type"
    # The facts on every row -- 0 bytes is a size, not a blank.
    assert items[0]["caption"] == f"Input 1 · image/png · 2 KB · {MEDIA_SHA['a1'][:12]}"
    assert items[1]["caption"] == f"Output 1 · image/png · 0 B · {MEDIA_SHA['b2'][:12]}"
    assert items[3]["caption"] == (
        f"Output 3 · video/mp4 · 3.3 MB · {MEDIA_SHA['d4'][:12]}"
    )


def test_jsdom_request_modal_media_is_never_inlined_or_listed(rendered) -> None:
    media = rendered["requestMedia"]
    assert media["dataUris"] == 0
    # The browser fetches a preview itself; the page's own fetches are the
    # detail row and, since 7.121.0, one summary of the request's raw
    # diagnostics (the boxes paint when it arrives, never before).
    assert media["fetched"] == [
        "/admin/api/requests/req-media",
        "/admin/api/requests/raw?id=req-media",
    ]
    assert media["afterClose"] == {"hidden": True, "children": 0}
    assert media["pageSourcesAfterClose"] == 0
    assert media["chatRowHidden"] is True
    assert media["chatRowChildren"] == 0


def test_jsdom_a_priced_media_row_shows_its_cost_like_a_chat_row(rendered) -> None:
    """7.69.0: the modal's Cost line, the est. badge and the rung that priced it."""

    cost = rendered["requestMedia"]["cost"]
    assert cost == {
        "amount": "$0.0400",
        "badge": "est.",
        "note": "estimated from LiteLLM",
    }


def test_jsdom_the_old_hiding_class_is_applied_nowhere(rendered) -> None:
    advanced = rendered["advancedFields"]
    assert advanced["showAdvancedInScript"] is False
    assert advanced["showAdvancedClassAnywhere"] == 0


def test_jsdom_advanced_fields_sort_after_the_common_ones(rendered) -> None:
    """`advanced` moves a field down the card. That is all it does to order."""

    order = rendered["advancedFields"]["section"]["all"]
    tagged = set(rendered["advancedFields"]["section"]["tagged"])

    assert tagged == set(ADVANCED_ON_THE_CARD)
    first_advanced = min(order.index(key) for key in tagged)
    assert all(key in tagged for key in order[first_advanced:]), (
        f"a common field sits below an advanced one: {order}"
    )
    # ...and the common ones keep the order the manifest gave them.
    assert order[:first_advanced] == [key for key in order if key not in tagged]


def test_jsdom_an_advanced_field_says_it_is_advanced(rendered) -> None:
    assert set(rendered["advancedFields"]["section"]["tagged"]) == set(
        ADVANCED_ON_THE_CARD
    )
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
        key for key in section["all"] if key not in ADVANCED_ON_THE_CARD
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


# ------------------------------------------------- success sub-labels (7.57.0)
#
# "Success" keeps meaning the client received a valid message. The few that
# carried no answer say which of two shapes they were, on every surface the
# cancelled sub-label reaches.


def test_jsdom_a_no_answer_success_row_carries_its_chip(rendered) -> None:
    rows = rendered["successViews"]["rows"]

    assert [row["chip"] for row in rows] == [
        "thought only",
        "empty",
        # An ordinary success carries no chip at all, not an empty one.
        None,
        None,
    ]
    assert [row["reason"] for row in rows[:2]] == ["thought_only", "empty"]
    # The status word is unchanged and still in its own cell.
    assert [row["status"] for row in rows] == [
        "success",
        "success",
        "success",
        "cancelled",
    ]
    # A cancelled row keeps its own chip and gains none.
    assert rows[3]["cancelChip"] == "stopped mid-answer"
    assert [row["cancelChip"] for row in rows[:3]] == [None, None, None]
    assert "The model reasoned and the turn ended there" in rows[0]["title"]
    assert "nothing in it" in rows[1]["title"]


def test_jsdom_the_status_filter_offers_the_two_success_sub_labels(rendered) -> None:
    options = rendered["successViews"]["filterOptions"]
    values = [value for value, _ in options]

    # Every old value is still there, in its old order.
    old = [value for value in values if not value.startswith("success:")]
    assert old == [
        "",
        "success",
        "error",
        "cancelled",
        "cancelled:client_gave_up_waiting",
        "cancelled:committed_then_silent",
        "cancelled:stopped_mid_answer",
        "cancelled:server_restart",
    ]
    assert values[values.index("success") + 1 : values.index("success") + 3] == [
        "success:thought_only",
        "success:empty",
    ]
    assert dict(options)["success:thought_only"] == (
        "success — no answer, thought only"
    )
    assert dict(options)["success:empty"] == "success — no answer, empty"


def test_jsdom_the_modal_names_it_in_a_line_and_a_sentence(rendered) -> None:
    views = rendered["successViews"]

    assert "No answer" in views["detailLabels"]
    assert views["modalChip"] == "thought only"
    assert views["modalSentence"].startswith(
        "The model reasoned and the turn ended there"
    )
    # A cancelled request's modal says why it was cancelled, and nothing here.
    assert "Cancelled because" in views["cancelledModalLabels"]
    assert "No answer" not in views["cancelledModalLabels"]


def test_jsdom_the_thinking_line_tells_withheld_from_absent(rendered) -> None:
    """7.103.0: "no thinking text" is four different answers, never one."""

    thinking = rendered["successViews"]["thinking"]

    assert thinking["req-thought"] == "thinking shown (18,800 chars)"
    assert thinking["req-withheld"] == (
        "thought — text withheld by the provider (≈511 reasoning tokens reported)"
    )
    assert thinking["req-encrypted"] == (
        "thought — no summary sent; text withheld by the provider"
    )
    assert thinking["req-did-not-think"] == (
        "did not think (the provider reported 0 reasoning tokens)"
    )
    assert thinking["req-unmeasured"] == "not measured"
    # Only an answered request has a line at all.
    assert thinking["req-cancelled"] is None


def test_jsdom_the_shape_pane_lists_what_the_reply_said_about_reasoning(
    rendered,
) -> None:
    rows = dict(rendered["successViews"]["withheldShape"])

    assert rows["thinking blocks"] == "1"
    assert rows["thinking blocks, text withheld"] == "1"
    assert rows["reasoning tokens (reported)"] == "511"
    assert rows["signature"] == "1 deltas, 2,872 chars"


def test_jsdom_the_models_chip_adds_the_split_only_when_it_is_known(rendered) -> None:
    unchanged, split = rendered["successViews"]["outcomeTails"]

    assert unchanged == ""
    assert split == (
        ", thought with text withheld 6/10, did not think 1/10, not measured 1/10"
    )


def test_jsdom_the_reasoning_tile_counts_visible_reasoning(rendered) -> None:
    labels = [row[1] for row in rendered["requestCards"]]

    assert "Turns with visible reasoning" in labels
    assert "Turns with reasoning" not in labels


def test_jsdom_the_page_load_asks_for_the_breakdown_with_its_filters(
    rendered,
) -> None:
    views = rendered["successViews"]

    assert len(views["noAnswerUrls"]) == 1
    # The page's own filters ride along, sub-label included.
    assert "status=success%3Athought_only" in views["noAnswerUrls"][0]
    assert "status=success%3Athought_only" in views["listUrls"][-1]


def test_jsdom_the_no_answer_panel_sums_to_what_it_claims(rendered) -> None:
    panel = rendered["successViews"]["panel"]

    assert panel["headers"] == [
        "What came back",
        "Requests",
        "Share of successes",
        "What it means",
    ]
    assert [row[0] for row in panel["rows"]] == [
        "thought only",
        "empty",
        "no answer, in all",
    ]
    assert [row[1] for row in panel["rows"]] == ["199", "2", "201"]
    # Shares of the 116,679 successes, two decimals because they are small.
    assert [row[2] for row in panel["rows"]] == ["0.17%", "0.00%", "0.17%"]
    assert panel["rows"][2][3] == "Of 116,679 successful requests in this range."


def test_jsdom_the_success_rate_card_carries_the_one_line_version(rendered) -> None:
    card = rendered["successViews"]["successCard"]

    assert card is not None
    assert card["note"] == "no answer 201: thought only 199 · empty 2"


def test_jsdom_a_page_narrowed_to_one_label_keeps_both_rows_and_drops_the_note(
    rendered,
) -> None:
    """The breakdown still counts both; the card would contradict itself."""

    views = rendered["successViews"]

    assert [row[1] for row in views["selectedPanel"]["rows"]] == ["199", "2", "201"]
    assert views["selectedCard"]["note"] is None


def test_jsdom_a_range_with_none_keeps_the_rows_and_drops_the_note(rendered) -> None:
    views = rendered["successViews"]

    assert [row[1] for row in views["zeroPanel"]["rows"]] == ["0", "0", "0"]
    assert views["zeroCard"]["note"] is None
    # The log being off is a different answer from nothing being counted.
    assert views["disabledPanel"]["rows"] == [["No successful requests in this range."]]
    assert views["disabledCard"] is None or views["disabledCard"]["note"] is None


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
    # A listed request opens its own detail, and that detail asks for its raw
    # diagnostics summary the same way every other open does (7.121.0).
    assert tools["carrierOpens"] == [
        "/admin/api/requests/req-tools",
        "/admin/api/requests/raw?id=req-tools",
    ]


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
    # 7.122.2: named for what it measures -- the first *visible byte*.
    "First byte",
    "Duration",
    "Details",
]


def test_requests_table_keeps_all_existing_columns(rendered) -> None:
    """All thirteen, still in their order, with nothing between them removed."""

    headers = rendered["harnessAttr"]["headers"]
    positions = [headers.index(name) for name in EXISTING_REQUEST_COLUMNS]
    assert positions == sorted(positions)
    assert len(headers) == len(EXISTING_REQUEST_COLUMNS) + 4


def test_search_index_line_says_what_the_index_covers(rendered) -> None:
    """7.92.0: the line under the Requests filters, in each state the route reports."""

    index = rendered["searchIndex"]
    assert index["present"] is True
    assert index["inRequestsView"] is True
    # The harness's route: a complete index, no button.
    assert index["atOpen"]["hidden"] is False
    assert index["atOpen"]["text"] == "Search index covers 480 of 480 rows."
    assert index["atOpen"]["build"] is False
    assert index["partial"]["text"] == (
        "Search index covers 412,000 of 598,683 rows. Rows logged before 7.92.0"
        " are searched the slow way until the index is built."
    )
    assert index["partial"]["build"] is True
    assert index["partial"]["buildText"] == "Build search index"
    assert index["partial"]["pause"] is False
    assert index["uncounted"]["text"].startswith("Rows logged before 7.92.0")
    assert index["complete"]["text"] == "Search index covers 598,683 of 598,683 rows."
    assert index["complete"]["build"] is False
    # Built, and still short: the rest is said plainly, and Build stays offered.
    assert index["doneIncomplete"]["text"] == (
        "Search index covers 598,641 of 598,683 rows; the rest are searched the slow way."
    )
    assert index["doneIncomplete"]["build"] is True


def test_search_index_build_shows_progress_eta_and_pause(rendered) -> None:
    index = rendered["searchIndex"]
    assert index["running"]["text"] == (
        "Build search index: 41 % · 3 min left — Search index covers 412,000 of"
        " 598,683 rows."
    )
    assert index["running"]["pause"] is True
    assert index["running"]["build"] is False
    assert index["runningLong"]["text"].startswith(
        "Build search index: 3 % · 1 h 12 min left"
    )
    assert index["runningSoon"]["text"].startswith(
        "Build search index: 99 % · 12 s left"
    )
    assert index["paused"]["text"] == (
        "Build search index paused at 41 % — Search index covers 412,000 of"
        " 598,683 rows."
    )
    assert index["paused"]["build"] is True
    assert index["paused"]["buildText"] == "Continue"
    assert index["paused"]["pause"] is False


def test_search_index_buttons_post_to_their_routes(rendered) -> None:
    index = rendered["searchIndex"]
    assert index["buildPosts"] == ["POST /admin/api/requests/search-index/build"]
    assert index["afterBuild"]["pause"] is True
    assert index["pausePosts"] == ["POST /admin/api/requests/search-index/pause"]
    assert index["afterPause"]["buildText"] == "Continue"
    # The page follows a running build until it ends.
    assert index["afterPoll"]["text"] == "Search index covers 598,683 of 598,683 rows."


def test_search_index_line_says_when_this_server_cannot_keep_one(rendered) -> None:
    index = rendered["searchIndex"]
    assert index["off"]["hidden"] is False
    assert index["off"]["text"].startswith(
        "Search index off: This Python's SQLite (3.31.1)"
    )
    assert index["off"]["text"].endswith(
        "(no such tokenizer: trigram), so searches read every stored request, as before."
    )
    assert index["off"]["build"] is False
    assert index["off"]["pause"] is False
    assert index["logOff"]["hidden"] is True


def test_requests_table_adds_three_columns(rendered) -> None:
    """Session, Exit (Folder until 7.88.0), Requested model -- plus the Origin
    chip that stands in for Session and Folder below 1200 px (CSS decides
    which is shown)."""

    classes = dict(rendered["requestOrigin"]["headerClasses"])
    added = [name for name in classes if name not in EXISTING_REQUEST_COLUMNS]
    assert added == ["Session", "Exit", "Origin", "Requested model"]
    assert classes["Session"] == "req-col-session"
    assert classes["Exit"] == "req-col-exit"
    assert "Folder" not in classes
    assert classes["Origin"] == "req-col-origin"
    assert classes["Requested model"] == "req-col-requested-model"


def test_every_row_has_one_cell_per_header_in_header_order(rendered) -> None:
    origin = rendered["requestOrigin"]
    width = len(origin["headerClasses"])

    assert origin["cellCounts"] == [width, width]
    by_header = dict(origin["withOrigin"]["cellClassesByHeader"])
    assert by_header["Session"] == "req-col-session"
    assert by_header["Exit"] == "req-col-exit"
    assert by_header["Origin"] == "req-col-origin"
    assert by_header["Requested model"] == "req-col-requested-model"
    assert by_header["Status"] == "req-col-status"


def test_the_origin_cells_show_short_forms_with_the_full_value_in_the_tooltip(
    rendered,
) -> None:
    """Folder left the table in 7.88.0 and nothing else of it did: the Session
    cell's title and the narrow Origin chip still name it."""

    row = rendered["requestOrigin"]["withOrigin"]

    assert row["session"] == "0f3c2a1bsubagent"
    assert row["subagentBadge"] is True
    assert "Session: 0f3c2a1b-6d5e-4f70-9a8b-1c2d3e4f5a6b" in row["sessionTitle"]
    assert "Folder: C:\\Users\\devuser\\Projects\\demo" in row["sessionTitle"]
    assert row["folderCell"] is False
    assert row["chip"] == "Projects\\demo · 0f3c2a1b"
    assert "Folder: C:\\Users\\devuser\\Projects\\demo" in row["chipTitle"]
    assert row["requested"] == "claude-opus-4"


def test_a_row_with_no_origin_draws_dashes_not_none(rendered) -> None:
    row = rendered["requestOrigin"]["bare"]

    assert (row["session"], row["exit"], row["chip"], row["requested"]) == (
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
    """Below 1200 px Session folds into the Origin chip (which carries the
    folder too); the Exit column that took Folder's place stays visible."""

    css = (STATIC_DIR / "admin.css").read_text(encoding="utf-8")
    narrow = css.split("@media (max-width: 1199px) {", 1)[1].split("\n}\n", 1)[0]

    assert ".requests-table .req-col-session" in narrow
    assert "display: none;" in narrow
    assert ".requests-table .req-col-origin {\n    display: table-cell;" in narrow
    assert ".requests-table .req-col-origin {\n  display: none;\n}" in css
    assert "req-col-exit" not in narrow


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


# ------------------------------------------------------- Exit column (7.88.0)

#: (case, text the cell shows, its title must contain) -- one per kind of
#: stored exit. Every address is a documentation address or a made-up name.
EXIT_CELL_CASES = [
    ("chainEntry", "Tokyo exit", "Answered through Tokyo exit."),
    ("oneEntry", "203.0.113.7:1080", "Answered through 203.0.113.7:1080."),
    ("staticProxy", "198.51.100.4:3128", "Went out through 198.51.100.4:3128."),
    ("direct", "direct", "this computer's own address"),
    (
        "systemProxy",
        "direct via system proxy 127.0.0.1:7890",
        "operating system's proxy (127.0.0.1:7890) carried",
    ),
    ("none", "—", "no chain and no proxy"),
    ("missingField", "—", "no chain and no proxy"),
    ("videoJob", "Tokyo exit", "Answered through Tokyo exit."),
]


@pytest.mark.parametrize(("case", "text", "title"), EXIT_CELL_CASES)
def test_the_exit_cell_shows_the_stored_exit_as_such(
    rendered, case: str, text: str, title: str
) -> None:
    cell = rendered["requestExitCell"]["cells"][case]

    assert cell["text"] == text
    assert title in cell["title"]
    assert cell["more"] is None
    if text == "—":
        assert cell["label"] is None
    else:
        assert cell["label"] == text


def test_a_null_exit_is_a_dash_never_a_guessed_direct(rendered) -> None:
    cell = rendered["requestExitCell"]["cells"]["none"]

    assert cell["text"] == "—"
    assert "direct" not in cell["text"]
    assert cell["title"].startswith("No exit recorded: no chain and no proxy.")
    # Honest about older rows: a one-entry chain or a static proxy was not
    # recorded before 7.79.2, so an old dash may hide one.
    assert "Before 7.79.2" in cell["title"]


def test_a_media_row_with_no_exit_says_why(rendered) -> None:
    cell = rendered["requestExitCell"]["cells"]["media"]

    assert cell["text"] == "—"
    assert "media request keeps its exit only on a video job" in cell["title"]
    assert "no chain and no proxy" not in cell["title"]


def test_a_rotated_request_shows_the_answering_exit_plus_the_others(rendered) -> None:
    cell = rendered["requestExitCell"]["cells"]["rotated"]

    assert cell["label"] == "exit-c.example:1080"
    assert cell["more"] == "+2"
    assert cell["moreAria"] == "2 more exits"
    assert cell["text"] == "exit-c.example:1080+2"
    assert (
        "Exits tried, in order: exit-a.example:1080, exit-b.example:1080,"
        " exit-c.example:1080"
    ) in cell["title"]


def test_no_answering_exit_still_counts_the_exits_tried(rendered) -> None:
    cell = rendered["requestExitCell"]["cells"]["noneThenOthers"]

    assert cell["text"] == "—+1"
    assert "no chain and no proxy" in cell["title"]
    assert "Exits tried, in order: exit-a.example:1080" in cell["title"]


def test_a_credential_in_a_label_is_masked_like_the_proxying_page(rendered) -> None:
    cells = rendered["requestExitCell"]["cells"]
    masked = cells["masked"]
    system = cells["maskedSystem"]

    assert masked["label"] == "203.0.113.9:1080"
    assert masked["more"] == "+1"
    assert (
        "Exits tried, in order: 198.51.100.2:8080, 203.0.113.9:1080"
        in (masked["title"])
    )
    assert system["label"] == "direct via system proxy 10.0.0.1:8080"
    for cell in (masked, system):
        for secret in ("alice", "s3cretpw", "bob", "hunter2x", "carol", "pw9x", "@"):
            assert secret not in cell["html"], secret
            assert secret not in cell["title"], secret
    options = rendered["requestExitCell"]["options"]
    assert not any("@" in option for option in options)


def test_the_exit_filter_suggests_the_exits_on_the_page(rendered) -> None:
    options = rendered["requestExitCell"]["options"]

    assert options[:2] == ["Tokyo exit", "203.0.113.7:1080"]
    assert "exit-b.example:1080" in options
    assert "direct via system proxy 127.0.0.1:7890" in options
    assert len(options) == len(set(options))


# ------------------------------------------------ exit refusal facts (7.96.0)

#: The chip's count sign, spelled as a named escape for the linter.
TIMES = "\N{MULTIPLICATION SIGN}"


def test_the_exit_cell_title_counts_the_exit_refusals(rendered) -> None:
    cells = rendered["exitFacts"]["cells"]

    assert cells["refused"]["text"] == "exit-c.example:1080+2"
    assert f"Exit refusals: country refused {TIMES}2." in cells["refused"]["title"]
    assert "not the key" in cells["refused"]["title"]
    assert (
        f"Exit refusals: country refused {TIMES}1 · challenged {TIMES}1."
        in cells["both"]["title"]
    )
    # No exit recorded (no chain, no proxy) and still refused for the country:
    # this computer's own address was refused, and the title says so too.
    assert cells["noExit"]["text"] == "—"
    assert f"Exit refusals: country refused {TIMES}1." in cells["noExit"]["title"]


def test_an_exit_cell_without_facts_is_what_it_was(rendered) -> None:
    cell = rendered["exitFacts"]["cells"]["old"]

    assert cell["text"] == "203.0.113.7:1080"
    assert cell["title"] == "Answered through 203.0.113.7:1080."
    assert "refus" not in cell["title"]


def test_the_request_detail_shows_the_facts_on_the_attempt_that_met_them(
    rendered,
) -> None:
    detail = rendered["exitFacts"]["detail"]

    (chip,) = detail["chips"]
    assert chip["text"] == f"country refused {TIMES}1 · challenged {TIMES}2"
    assert chip["className"] == "req-chain-summary req-chain-exitfact"
    assert "about the address the request left from, not the key" in chip["title"]
    assert chip["text"] in detail["ladderSummaries"]


def test_an_old_row_renders_no_fact_chip(rendered) -> None:
    """R7: a stored ladder served without ``exit_facts`` draws nothing new."""
    old = rendered["exitFacts"]["oldDetail"]

    assert old["chips"] == []
    assert "country refused" not in old["chainText"]
    assert "challenged" not in old["chainText"]


def test_the_table_has_no_folder_cell_and_the_detail_keeps_folder(rendered) -> None:
    exit_cells = rendered["requestExitCell"]

    assert exit_cells["folderCells"] == 0
    detail = dict(exit_cells["detail"])
    assert detail["Folder"] == (
        "C:\\Users\\devuser\\Projects\\demo (read from the prompt's environment block)"
    )


def test_the_exit_filter_follows_the_folder_filters_wiring(rendered) -> None:
    exit_filter = rendered["exitFilter"]

    assert exit_filter["label"].startswith("Exit")
    assert "exit=" not in exit_filter["unfilteredListUrl"]
    assert "offset=25" in exit_filter["pagedUrl"]
    assert exit_filter["loadsWhileTyping"] == 0
    assert exit_filter["loadsAfterPause"] == 1
    assert "offset=0" in exit_filter["urls"]["list"]
    for name, url in exit_filter["urls"].items():
        assert "exit=Tokyo+exit" in url, (name, url)
    assert exit_filter["persisted"] == "Tokyo exit"
    assert exit_filter["restored"] == "Tokyo exit"
    assert "exit=Tokyo+exit" in exit_filter["exportUrl"]
    assert exit_filter["cleared"] == ""
    assert "exit=" not in exit_filter["clearedListUrl"]
    assert "exit" not in exit_filter["clearedPersisted"]


def test_the_export_window_offers_the_exit_group_opt_in() -> None:
    """Offered beside Request origin, not a default: an export that does not
    ask for it is byte-identical to one made before 7.88.0."""

    source = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
    requests_fields = source.split("const EXPORT_FIELDS = {", 1)[1].split("],", 1)[0]
    assert '{ id: "exit", label: "Exit" }' in requests_fields
    assert requests_fields.index('id: "exit"') < requests_fields.index('id: "origin"')
    defaults = source.split("const EXPORT_DEFAULT_FIELDS = {", 1)[1].split("]),", 1)[0]
    assert '"exit"' not in defaults


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
    # 7.88.0: Exit where Folder was. The in-flight table below keeps Folder.
    "Exit",
    "Origin",
    "Provider",
    "Key",
    "Requested model",
    "Model",
    "Status",
    "Turn",
    "Tokens",
    "Cost",
    # 7.122.2: named for what it measures -- the first *visible byte*.
    "First byte",
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
    """Driven by a manual clock in the harness: the page's one-second ticker is
    captured when polling turns it on and run after the clock moves 1.1 s --
    never by sleeping and hoping the interval fired (that failed on loaded CI
    runners with ``'5s' != '5s'``)."""

    five = rendered["inflight"]["five"]

    assert five["tickersWhenOn"] == 1
    # 1.1 s on a whole-second display always changes the reading, whatever
    # real time had already passed since the snapshot.
    assert five["ageBefore"] != five["ageAfter"]
    assert five["tickersWhenOff"] == 0
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


def test_a_refused_save_says_so_and_keeps_the_edit(rendered) -> None:
    """7.69.7: a save the server refuses (the settings file was busy) is shown.

    The Save button's handler dropped the rejected promise, so a 503 left the
    page silent. Driven through the real button: the server's sentence is the
    banner, as an error; the edited control keeps its value and is still the
    one unsaved change; nothing reloads; and the same click saves once the
    file is free.
    """

    refused = rendered["refusedSave"]
    assert refused["controlFound"] is True
    assert refused["edited"]
    assert refused["editTook"] is True
    assert refused["dirtyBefore"] != refused["dirtyAtStart"]
    assert refused["message"] == refused["sentence"]
    assert refused["message"].startswith("Not saved: the settings file was busy")
    assert refused["messageIsError"] is True
    assert refused["valueKept"] is True
    assert refused["dirtyAfter"] == refused["dirtyBefore"]
    assert "LOG_LEVEL" in refused["submitted"]
    assert refused["reloads"] == 0
    assert refused["retryMessage"] == "Applied"
    assert refused["retryReloads"] == 1


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


#: "hh:mm:ss" in the reader's locale: "14:02:40", or "2:02:40 PM" (the space
#: before the AM/PM is a narrow no-break space in current ICU builds).
AS_OF_CLOCK = r"\d{1,2}:\d\d:\d\d(?:\s?[AP]M)?"


def _window_part(caption: str) -> str:
    """A caption without its "· as of hh:mm:ss" (7.69.4), checked as a whole."""

    window, _, as_of = caption.partition(" · as of ")
    assert not as_of or re.fullmatch(AS_OF_CLOCK, as_of), caption
    return window


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
        # 7.69.4: once its numbers land, a caption also says when they were
        # computed ("· as of hh:mm:ss"); the window it names is unchanged.
        assert _window_part(all_time[heading]) == "Window: all stored rows", heading
        assert _window_part(last_day[heading]) == "Window: last 24h", heading
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


# ------------------------------- 7.56.0 keep the fastest healthy proxy first


def test_existing_chain_shows_switch_off(rendered) -> None:
    """A chain stored before the switch existed reads OFF, and one click is
    one write of the flag -- nothing moves at the click."""

    order = rendered["proxying"]["speedOrder"]
    existing = order["existing"]
    assert existing["present"] is True
    assert existing["hasKey"] is False  # the fixture is a pre-7.56.0 chain
    assert existing["checked"] is False
    assert existing["disabled"] is False
    assert existing["label"] == "Keep the fastest healthy proxy first"
    assert existing["note"].startswith("Off: this chain keeps the order you gave it")
    assert order["writesOnRender"] == []

    on = order["switchedOn"]
    assert on["writes"] == [
        {
            "path": "/admin/api/proxy-chains/order",
            "method": "POST",
            "body": {"provider": "nvidia_nim", "order_by_speed": True},
        }
    ]
    assert on["checked"] is True
    # The click reorders nothing: the server's loop does, later, by margin.
    assert on["labelsAfter"] == existing["labelsBefore"]
    assert "Nothing has moved yet" in on["announcement"], on["announcement"]
    assert on["note"].startswith("On."), on["note"]

    # Offered for failover and single only: round_robin shows it off and
    # disabled, with the reason.
    rr = order["roundRobin"]
    assert rr["checked"] is False
    assert rr["disabled"] is True
    assert "failover and single" in rr["note"]


def test_sort_now_button(rendered) -> None:
    sort = rendered["proxying"]["speedOrder"]["sortNow"]
    assert sort["present"] is True
    assert sort["text"] == "Sort by speed now"
    assert sort["writes"] == [
        {
            "path": "/admin/api/proxy-chains/sort",
            "method": "POST",
            "body": {"provider": "nvidia_nim"},
        }
    ]
    # The page redraws the order the server wrote.
    assert sort["labelsAfter"][:2] == ["198.51.100.9:8080", "203.0.113.7:1080"]
    assert sort["announcement"].startswith("Sorted NVIDIA NIM by speed."), sort
    assert "198.51.100.9:8080 is now the first healthy address" in sort["announcement"]
    assert "(was 203.0.113.7:1080)" in sort["announcement"]


def test_pause_all_but_fastest_n_is_explicit(rendered) -> None:
    pause = rendered["proxying"]["speedOrder"]["pause"]
    assert pause["defaultKeep"] == "3"
    assert pause["defaultText"] == "Pause all but the fastest 3"
    assert pause["text"] == "Pause all but the fastest 1"
    # Choosing N sends nothing: only the press does.
    assert pause["writesBeforeClick"] == []
    assert pause["writes"] == [
        {
            "path": "/admin/api/proxy-chains/pause-fastest",
            "method": "POST",
            "body": {"provider": "nvidia_nim", "keep": 1},
        }
    ]
    assert len(pause["pausedRows"]) == 2, pause
    # Reversible with each row's ordinary Resume.
    assert pause["resumeButtons"] == 2
    assert pause["announcement"].startswith("Paused 2 addresses on NVIDIA NIM"), pause
    assert "Resume" in pause["announcement"]


def test_the_image_rail_is_its_own_card(rendered) -> None:
    """A media rail (7.60.0) is drawn with the ordinary rail editor, apart
    from the chat tiers, and none of its settings lands in the leftovers."""
    rail = rendered["imageRail"]
    assert rail["card"] is True
    assert rail["heading"] == "Image rail"
    assert rail["primaryInCard"] is True
    assert rail["inTheLeftovers"] == []
    # 7.62.0: the Speech rail is the second media card, same editor.
    assert rail["headings"] == [
        "Image rail",
        "Speech rail",
        "Transcription rail",
        "Video rail",
    ]
    assert rail["speechPrimaryInCard"] is True


def test_the_upload_limit_has_its_own_card_under_the_media_rails(rendered) -> None:
    """MEDIA_UPLOAD_MAX_MB (7.95.0): one setting shared by every media rail,
    drawn as its own card straight after the four rails -- never a bare box
    in the leftovers grid."""
    uploads = rendered["mediaUploads"]
    assert uploads["card"] is True
    assert uploads["heading"] == "Media uploads"
    assert uploads["input"] is True
    assert uploads["inTheLeftovers"] is False
    assert uploads["afterTheRails"] is True
    assert "0 sets no limit" in uploads["note"]


def test_requests_table_labels_media_rows(rendered) -> None:
    """User decision 3 of 2026-10-04 (7.95.0): a media row says what it did.

    After every chip a row already had, from the list's media columns; a row
    that names an operation and produced nothing says only what it was.
    """
    media = rendered["turnChips"]["media"]

    def chips(name: str) -> list[list[str]]:
        return media[name]["chips"]

    assert chips("imageOne") == [["media", "1 image generated"]]
    # An edit keeps its input-image chip first; what came back follows.
    assert chips("imageMany") == [["image", "image"], ["media", "3 images generated"]]
    assert chips("imageRefused") == [["media", "image request"]]
    assert chips("speechTimed") == [["media", "speech · 4.3s"]]
    assert chips("speechUntimed") == [["media", "speech"]]
    assert chips("speechRefused") == [["media", "speech request"]]
    assert chips("transcribed") == [
        ["response", "reply"],
        ["media", "transcribed · 31s"],
    ]
    assert chips("translationRefused") == [["media", "translation request"]]
    assert chips("videoJob") == [["media", "video job"]]
    assert chips("videoTimed") == [["media", "video job · 8s"]]
    assert chips("videoRefused") == [["media", "video request"]]
    assert not any(row["empty"] for row in media.values())


def test_request_detail_names_the_rate_each_cache_write_took(rendered) -> None:
    """7.111.0: the 5 min / 1 h split and the rate each part was priced at.

    A row whose host reported no split shows no new line and the cost cell it
    always had; a row whose 1-hour writes no source prices says why.
    """
    split = rendered["costWriteSplit"]
    assert split["noSplit"]["split"] is None
    assert split["noSplit"]["rate"] is None
    assert split["fiveOnly"]["split"] == "5 min: 900 · 1 h: 0"
    assert split["fiveOnly"]["rate"] == "Cache writes: 900 at the 5 min rate."
    assert split["oneHour"]["split"] == "5 min: 600 · 1 h: 300"
    assert split["oneHour"]["rate"] == (
        "Cache writes: 600 at the 5 min rate, 300 at the 1 h rate from "
        "LiteLLM, prefixed key (anthropic/claude-sonnet-5-5) ($6.0000 / 1M tokens)."
    )
    assert split["unpriced"]["cellText"] == "—"
    # The provider as the dashboard labels it everywhere else.
    assert split["unpriced"]["cellTitle"].startswith(
        "not priced: no source states the 1 h write rate for claude-sonnet-5-5 on "
    )
    # An ordinary unpriced row keeps the tooltip it always had.
    assert split["plainUnpriced"]["cellTitle"] == (
        "not priced — no source published a rate for this model"
    )


def test_chat_row_turn_chips_unchanged(rendered) -> None:
    """Equality: a chat row's Turn cell is exactly what it was before 7.95.0."""
    chat = rendered["turnChips"]["chat"]
    assert chat["bare"] == {"empty": True, "text": "—", "chips": []}
    assert chat["full"]["chips"] == [
        ["image", "2 images"],
        ["thinking", "thinking"],
        ["tools", "3 tools"],
        ["response", "reply"],
    ]
    assert chat["oneTool"]["chips"] == [["tools", "1 tool"]]


# ------------------------------------------------------ 7.67.0 media dashboard

_ADMIN_CSS = (STATIC_DIR / "admin.css").read_text(encoding="utf-8")


def _css_rule(selector: str) -> str:
    match = re.search(rf"(?m)^{re.escape(selector)} \{{([^}}]*)\}}", _ADMIN_CSS)
    assert match, f"admin.css has no rule for {selector}"
    return match.group(1)


def test_the_models_page_lists_media_models_in_their_own_section(rendered) -> None:
    media = rendered["mediaDashboard"]

    assert media["modelsHeaders"] == [
        "Model",
        "Rails",
        "Provider declares",
        "models.dev output",
        "Health",
        "Key",
    ]
    assert media["modelRefs"] == [
        "together/flux-schnell",
        "groq/not-an-image-model",
        "groq/playai-tts",
        "deepinfra/whisper-large-v3",
    ]
    assert media["statusHidden"] is True
    # Never merged into the chat tree above it.
    assert media["treeHasMediaRef"] is False


def test_a_media_row_says_rail_position_pause_and_what_is_declared(rendered) -> None:
    chips = rendered["mediaDashboard"]["modelChips"]
    texts = [[[chip["text"] for chip in cell] for cell in row] for row in chips]

    flux, groq_image, groq_tts, whisper = texts
    assert flux[0] == ["Image · primary"]
    assert flux[1] == ["image generate", "speech mp3/wav/raw"]
    assert flux[2] == ["image"]
    # Listed twice on its rail (7.82.0): one position chip per listing, and
    # what is true of the model on the whole rail is said once.
    assert groq_tts[0] == ["Speech · primary", "Speech: paused", "Speech · fallback 1"]
    # models.dev's list is advice: an approximate rung is marked as such.
    assert groq_tts[2] == ["≈ audio"]
    assert whisper[0] == ["Transcription · primary"]
    # The declared endpoint's path rides in the tooltip.
    assert chips[0][1][0]["title"] == "images/generations"
    assert groq_image[1] == ["speech", "transcribe"]


def test_a_provider_that_declares_nothing_for_the_rail_is_flagged(rendered) -> None:
    chips = rendered["mediaDashboard"]["modelChips"]
    rails = chips[1][0]

    assert [chip["text"] for chip in rails] == [
        "Image · fallback 1",
        "Image: not served — skipped uncharged",
    ]
    assert "media-chip-warn" in rails[1]["className"]
    assert "without charging it a failure" in rails[1]["title"]


def test_unknown_modalities_say_unknown_never_text(rendered) -> None:
    chips = rendered["mediaDashboard"]["modelChips"]

    (unknown,) = chips[1][2]
    assert unknown["text"] == "unknown"
    assert "does not describe" in unknown["title"]


def test_the_media_bench_and_a_missing_key_are_shown(rendered) -> None:
    chips = rendered["mediaDashboard"]["modelChips"]

    (benched,) = chips[2][3]
    assert benched["text"] == "benched (media)"
    assert benched["title"].startswith("benched: 3 consecutive failures")
    assert [chip["text"] for chip in chips[0][3]] == ["not benched"]
    (missing,) = chips[3][4]
    assert missing["text"] == "Missing key"
    assert "media-chip-warn" in missing["className"]
    assert [chip["text"] for chip in chips[0][4]] == ["Configured · 2 keys"]


def test_the_models_page_lists_providers_with_media_endpoints(rendered) -> None:
    media = rendered["mediaDashboard"]

    assert media["providersHeaders"] == [
        "Provider",
        "Can serve",
        "Declared endpoints",
        "Key",
    ]
    together, acme = media["providerRows"]
    assert together[0] == "Together AI"
    assert together[1] == "ImageSpeechTranscription"
    assert acme[0] == "Acmecustom"
    assert acme[1] == "Speech"


def test_an_install_with_no_media_rail_says_so(rendered) -> None:
    media = rendered["mediaDashboard"]

    assert media["emptyModelsText"].startswith("No model is on a media rail.")
    assert "MODEL_IMAGE" in media["emptyModelsText"]
    assert media["emptyProvidersText"] == "No provider declares a media endpoint."


# ------------------------------------------------- 7.98.0 media request fields


def test_a_row_without_a_request_schema_renders_as_before(rendered) -> None:
    """Equality: a payload from before 7.98.0 draws no request-fields row."""
    assert rendered["mediaDashboard"]["fieldsRowsWithoutSchema"] == 0


def test_each_media_row_gets_its_request_fields_block(rendered) -> None:
    fields = rendered["mediaDashboard"]["requestFields"]

    assert fields["rowCount"] == 2
    assert fields["followsItsModel"] is True
    assert fields["colSpan"] == 6
    assert fields["startsClosed"] is True
    assert fields["summary"] == "Request fields · custom_agnes/agnes-video-2.5"
    # One chip per state, in the ladder's own order of states.
    assert fields["summaryChips"] == [
        "1 stated",
        "1 required (stated)",
        "1 required (learned)",
        "1 not forwarded by MCC",
        "2 forwarded, not known",
    ]
    assert fields["head"] == "video create — required: prompt, mode"


def test_request_fields_show_each_state_and_who_said_it(rendered) -> None:
    fields = rendered["mediaDashboard"]["requestFields"]

    header, *rows = fields["table"]
    assert header == ["Field", "State", "From", "Values", "Since", ""]
    assert rows == [
        [
            "prompt",
            "required (stated)",
            "the endpoint's own contract",
            "string",
            "—",
            "",
        ],
        ["quality", "stated", "your statement", "std, pro", "2026-10-18", "Remove"],
        [
            "speed",
            "not forwarded by MCC",
            "MCC's adapter for this host",
            "number",
            "—",
            "",
        ],
        [
            "mode",
            "required (learned)",
            "learned from this host's refusal",
            "—",
            "2026-10-12",
            "Forget",
        ],
    ]
    # The host's own words ride in the learned state's tooltip.
    assert "mode is required" in fields["stateTitles"][3]
    # Nobody spoke about these: forwarded, not known -- never "unsupported".
    assert fields["unknown"].startswith(
        "Forwarded, not known (no source says whether this model takes them): seconds, size."
    )
    assert "unsupported" not in fields["unknown"]
    assert (fields["forgetButtons"], fields["removeButtons"]) == (1, 1)
    assert fields["emptyText"].endswith(
        "Its provider serves none of this rail's operations, so no request is ever sent to it."
    )


def test_forget_on_a_learned_field_names_that_field_only(rendered) -> None:
    fields = rendered["mediaDashboard"]["requestFields"]

    (post,) = fields["forgetPosts"]
    assert post["path"] == "/admin/api/model-admin/learned/forget"
    assert post["method"] == "POST"
    assert post["body"] == {
        "providerId": "custom_agnes",
        "modelId": "agnes-video-2.5",
        "factKind": "media_param_required",
        "detail": "video_create:mode",
    }


def test_the_operator_editor_saves_one_statement(rendered) -> None:
    fields = rendered["mediaDashboard"]["requestFields"]

    assert fields["editorControls"] == [
        "Operation",
        "Field",
        "State",
        "Values",
        "Type",
        "Save",
    ]
    assert fields["operationOptions"] == ["video_create"]
    # Nothing is sent for a statement with no field.
    assert fields["emptySavePosts"] == 0
    (post,) = fields["savePosts"]
    assert post["path"] == "/admin/api/media/request-fields"
    assert post["body"] == {
        "modelRef": "custom_agnes/agnes-video-2.5",
        "operation": "video_create",
        "field": "mode",
        "state": "required",
        "values": ["std", "pro"],
        "type": "string",
    }
    # A save reloads the section and keeps the block the operator was in open.
    assert fields["reopenedAfterSave"] is True


def test_the_analytics_media_card_is_sent_the_window_only(rendered) -> None:
    media = rendered["mediaDashboard"]

    assert media["analyticsUrls"], media
    for url in media["analyticsUrls"]:
        assert "since=" in url
        assert "provider" not in url
        assert "local" not in url
    # Its caption names the window and does not claim the page's filter.
    assert _window_part(media["caption"]) == "Window: last 24h"
    assert _window_part(media["otherCaption"]) == "Window: last 24h, filtered"
    assert media["headingCount"] == 1


def test_the_analytics_media_card_renders_per_rail_and_per_model(rendered) -> None:
    media = rendered["mediaDashboard"]

    assert media["railHeaders"] == [
        "Rail",
        "Requests",
        "Succeeded",
        "Failed",
        "Cancelled",
        "Output",
        "Bytes out",
        "Median",
        "Average",
        "Cost",
    ]
    image, speech, asr, video = media["railRows"]
    assert image == [
        "Image",
        "3",
        "2",
        "1",
        "0",
        "3 images",
        "1.46 KB",
        "400 ms",
        "500 ms",
        "$0.0400 · $0.0800est. · 1 unpriced",
    ]
    # Speech: bytes measured, seconds not -- a dash, never "0 s".
    assert speech[5] == "—"
    assert speech[6] == "2.00 KB"
    # Transcription had no traffic: counted 0, every measure a dash.
    assert asr[1:5] == ["0", "0", "0", "0"]
    assert asr[5:] == ["—", "—", "—", "—", "—"]
    assert video[5] == "1 job · 8 s"
    assert video[6] == "—"
    assert media["modelHeaders"][:2] == ["Rail", "Provider / model"]
    assert [row[:2] for row in media["modelRows"]] == [
        ["Image", "Together AI / flux"],
        ["Video", "OpenRouter / veo"],
    ]
    assert media["noteHidden"] is True


def test_not_measured_is_a_dash_that_says_so(rendered) -> None:
    media = rendered["mediaDashboard"]

    assert media["dashes"]
    assert all(dash == ["—", "not measured"] for dash in media["dashes"])
    cells = [cell for row in media["railRows"] for cell in row]
    assert "0 s" not in cells


_UNPRICED_TITLE = (
    "no source published a rate for these requests; they are not counted as $0"
)


def test_the_media_cost_column_keeps_reported_estimated_and_unpriced_apart(
    rendered,
) -> None:
    """7.69.0: never one merged total, and an unpriced request is a count, not $0."""

    media = rendered["mediaDashboard"]
    image_rail = media["costCells"][0]
    assert image_rail["amounts"] == ["$0.0400", "$0.0800"]
    assert image_rail["badges"] == 1
    # The speech rail priced nothing: a count with its reason, no amount.
    assert media["railRows"][1][-1] == "2 unpriced"
    assert ["2 unpriced", _UNPRICED_TITLE] in media["unpriced"]
    # The video rail's payload named no unpriced count: requests minus priced.
    assert media["railRows"][3][-1] == "2 unpriced"
    # A model row priced whole: one estimate, nothing unpriced.
    assert media["modelRows"][0][-1] == "$0.0800est."
    cells = [cell for row in media["railRows"] for cell in row]
    assert "$0.00" not in cells
    assert "color: var(--muted)" in _css_rule(".media-unpriced")
    assert "flex-wrap: wrap" in _css_rule(".media-cost")


def test_the_export_window_offers_cost_and_media_opt_in() -> None:
    """7.69.0: both offered, neither a default, so a default export is unchanged."""

    source = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
    requests_fields = source.split("const EXPORT_FIELDS = {", 1)[1].split("],", 1)[0]
    assert '{ id: "cost", label: "Cost" }' in requests_fields
    assert '{ id: "media", label: "Media" }' in requests_fields
    defaults = source.split("const EXPORT_DEFAULT_FIELDS = {", 1)[1].split("]),", 1)[0]
    assert '"cost"' not in defaults
    assert '"media"' not in defaults


def test_video_job_states_are_their_own_table(rendered) -> None:
    media = rendered["mediaDashboard"]

    assert media["jobHeaders"] == [
        "Provider / model",
        "Queued",
        "In progress",
        "Completed",
        "Failed",
        "Other",
    ]
    assert media["jobRows"] == [["OpenRouter / veo", "0", "1", "1", "0", "1"]]
    assert media["jobOtherTitle"] == "unknown: 1"


def test_an_empty_window_and_a_disabled_log_say_so(rendered) -> None:
    media = rendered["mediaDashboard"]

    assert media["emptyNote"] == "No media requests in this window."
    assert media["emptyTables"] == 0
    assert media["disabledNote"] == "Request log disabled (REQUEST_LOG_ENABLED=false)."


def test_media_tables_scroll_inside_their_card_at_420px(rendered) -> None:
    """jsdom has no box model: the claim is the class contract it rests on."""

    media = rendered["mediaDashboard"]

    assert "table-scroll" in media["modelsScrollClass"].split()
    assert "table-scroll" in media["providersScrollClass"].split()
    assert all(
        "table-scroll" in cls.split() for cls in media["analyticsScrollClasses"]
    ), media["analyticsScrollClasses"]
    assert "overflow-x: auto" in _css_rule(".table-scroll")
    assert "flex-wrap: wrap" in _css_rule(".media-chip-row")
    assert "overflow-wrap: anywhere" in _css_rule(".media-ref")


def test_the_custom_card_offers_media_endpoints_beside_the_wire_apis(
    rendered,
) -> None:
    card = rendered["customProviders"]

    assert card["mediaLabels"] == [
        "Image generation (/images/generations)",
        "Image edits (/images/edits)",
        "Speech (/audio/speech)",
        "Transcription (/audio/transcriptions)",
        "Translation (/audio/translations)",
        "Video jobs (/videos, /videos/{id})",
    ]
    assert card["mediaChecked"] == ["speech"]
    assert "skips a provider that declares nothing" in (card["mediaHelp"] or "")
    assert " · media speech" in card["detailsBefore"]


def test_ticking_a_media_endpoint_submits_it_and_a_create_sends_none(
    rendered,
) -> None:
    card = rendered["customProviders"]

    assert card["patchedMedia"] == ["speech", "transcribe"]
    assert card["createdMedia"] == []


# ------------------------------------- 7.69.4 pause buttons and the poll diet
#
# One run of admin_jsdom_pause_polls.mjs (MCC_JSDOM_SCENARIO=pause_polls): the
# real admin.js on a manual clock, a server emulated for the routes under test,
# and a second tab. The fixture has the rows every guard walks: a Fable rail
# whose top two rows are paused (p1/f0-sol, p1/f1-muse) above a live
# p1/f2-bunny, and Codex's own Sonnet-tier rail (p1/a0 paused, a1, a2 live).

FABLE_PAUSED = {"p1/f0-sol", "p1/f1-muse"}
AGENT_PAUSED = {"p1/a0"}
INFLIGHT_PATH = "GET /admin/api/requests/in-flight"
HEAVY_ANALYTICS = (
    "GET /admin/api/requests/cost",
    "GET /admin/api/requests/latency",
    "GET /admin/api/requests/ttft",
    "GET /admin/api/requests/no-answer",
    "GET /admin/api/requests/origin",
    "GET /admin/api/analytics/media",
    "GET /admin/api/requests/stats",
)


@pytest.fixture(scope="module")
def pause_polls() -> dict:
    return _run(MCC_JSDOM_SCENARIO="pause_polls")


def _gaps(starts: list[int]) -> list[int]:
    return [later - earlier for earlier, later in pairwise(starts)]


def _assert_rows_tell_the_truth(
    rows: list[dict], paused: set[str], moment: str
) -> None:
    assert rows, f"{moment}: no rows to check -- the guard would be vacuous"
    for row in rows:
        want = row["ref"] in paused
        assert row["label"] == ("Resume" if want else "Pause"), (moment, row)
        assert row["chipShown"] is want, (moment, row)
        assert row["pressed"] == ("true" if want else "false"), (moment, row)


def test_the_pause_scenario_runs_clean(pause_polls) -> None:
    assert pause_polls["fatal"] is None
    assert pause_polls["scriptErrors"] == []
    assert pause_polls["clockErrors"] == []
    assert pause_polls["tab2Errors"] == []
    assert pause_polls["consoleErrors"] == []


def test_a_paused_row_reads_resume_from_the_moment_the_page_loads(pause_polls) -> None:
    _assert_rows_tell_the_truth(pause_polls["A0"]["fable"], FABLE_PAUSED, "A0")
    _assert_rows_tell_the_truth(pause_polls["A0"]["agent"], AGENT_PAUSED, "A0")


def test_a_paused_row_still_reads_resume_while_a_save_is_loading(pause_polls) -> None:
    """A1: the config is re-read and every rail rebuilt; the Coding agents
    calls behind it are still out. Before 7.69.4 every row read "Pause" here,
    for five seconds on the live server and forever if a call failed."""

    a1 = pause_polls["A1"]
    _assert_rows_tell_the_truth(a1["fable"], FABLE_PAUSED, "A1")
    _assert_rows_tell_the_truth(a1["agent"], AGENT_PAUSED, "A1")
    assert all(row["inDocument"] for row in a1["fable"] + a1["agent"])


def test_a_click_sends_the_opposite_of_what_the_button_said(pause_polls) -> None:
    """Never `{"paused": false}` from a button that read "Pause", and never
    `{"paused": true}` from one that read "Resume"."""

    clicks = [pause_polls["A2"], *pause_polls["staleClicks"]]
    sent_any = False
    for click in clicks:
        for body in click["sent"]:
            sent_any = True
            expected = "Pause" if body["paused"] else "Resume"
            assert click["labelAtClick"] == expected, click
    assert sent_any, "no click sent anything -- the check above would be vacuous"
    # A2 read "Resume" on a paused row, so its Resume is the reader's own.
    assert pause_polls["A2"]["labelAtClick"] == "Resume"
    assert pause_polls["A2"]["sent"] == [
        {"model_key": "MODEL_FABLE", "model_ref": "p1/f0-sol", "paused": False}
    ]


def test_a_stale_label_sends_nothing_and_is_repainted(pause_polls) -> None:
    stale = {click["ref"]: click for click in pause_polls["staleClicks"]}

    assert set(stale) == {"p1/f0-sol", "p1/f2-bunny"}
    for ref, click in stale.items():
        assert click["sent"] == [], click
        assert click["after"]["label"] == ("Resume" if ref in FABLE_PAUSED else "Pause")
        assert "nothing was sent" in click["status"], click


def test_an_agent_tier_click_during_a_save_keeps_its_rail(pause_polls) -> None:
    """The settings reload used to drop the agent rail's editor and its pause
    list: a Pause clicked there posted no fallbacks and lost the paused ref."""

    agent = pause_polls["A2agent"]
    assert agent["labelAtClick"] == "Pause"
    assert agent["sent"] == [
        {
            "harness": "codex",
            "tier": "medium",
            "override": True,
            "model": "p1/a0",
            "fallbacks": ["p1/a1", "p1/a2"],
            "paused": ["p1/a0", "p1/a1"],
        }
    ]


def test_a_failed_call_during_a_save_leaves_the_chips_true(pause_polls) -> None:
    """B1: a Save whose local-status call fails. The repaint used to come only
    after it, so it never came."""

    b1 = pause_polls["B1"]
    assert b1["applyError"] == "simulated timeout under load"
    _assert_rows_tell_the_truth(b1["fable"], FABLE_PAUSED, "B1")
    _assert_rows_tell_the_truth(b1["agent"], AGENT_PAUSED | {"p1/a1"}, "B1")


def test_a_failed_pause_write_leaves_the_chips_true(pause_polls) -> None:
    rows = pause_polls["failedPauseWrite"]
    _assert_rows_tell_the_truth(rows["fable"], FABLE_PAUSED, "failed write")


def test_a_page_load_makes_no_validate_call(pause_polls) -> None:
    """Its answer was discarded, and it cost a full settings prepare on the
    server's event loop. Seven loads happen in this run; none validates."""

    urls = pause_polls["loadUrls"]
    assert "GET /admin/api/config" in urls
    assert not [url for url in urls if "/config/validate" in url]
    assert pause_polls["validateCalls"] == 0


def test_a_finished_request_does_not_reload_the_analytics(pause_polls) -> None:
    """61 s on the Requests view with a request finishing every second.

    Before 7.69.4 every 3 s in-flight tick that saw a finish fired the pulse
    and nine analytics queries: 260 requests in this minute, 24 of each heavy
    query. Now the table follows the 15 s interval and the heavy totals come
    once, at the minute."""

    ticks = pause_polls["finishTicks"]
    counts = ticks["counts"]
    assert ticks["seconds"] == 61
    assert 19 <= counts[INFLIGHT_PATH] <= 21
    assert counts["GET /admin/api/requests/pulse"] == 4
    assert counts["GET /admin/api/requests"] == 4
    assert counts["GET /admin/api/requests/lifetime"] == 4
    for path in HEAVY_ANALYTICS:
        assert counts.get(path, 0) == 1, path
    assert ticks["requests"] <= 45


def test_the_chosen_interval_drives_the_table(pause_polls) -> None:
    counts = pause_polls["chosenInterval"]["counts"]

    # 20 s at the 5 s interval: four table refreshes, no heavy query.
    assert counts["GET /admin/api/requests/pulse"] == 4
    assert counts["GET /admin/api/requests"] == 4
    for path in HEAVY_ANALYTICS:
        assert path not in counts, path


def test_the_totals_say_when_they_were_computed(pause_polls) -> None:
    as_of = pause_polls["asOf"]

    assert re.fullmatch(
        rf"Updated {AS_OF_CLOCK} · totals as of {AS_OF_CLOCK}", as_of["lastUpdated"]
    ), as_of["lastUpdated"]
    assert as_of["captions"]
    for caption in as_of["captions"]:
        assert re.search(rf"· as of {AS_OF_CLOCK}$", caption), caption


def test_the_badge_off_the_requests_view_reads_every_ten_seconds(pause_polls) -> None:
    badge = pause_polls["badgeOffView"]
    starts = [entry["t"] for entry in badge["urls"]]

    assert badge["count"] == 6
    assert set(_gaps(starts)) == {10_000}
    assert {entry["url"] for entry in badge["urls"]} == {
        "/admin/api/requests/in-flight?limit=1"
    }

    panel = pause_polls["panelOnView"]
    assert panel["count"] == 3
    assert set(_gaps([entry["t"] for entry in panel["urls"]])) == {3_000}
    assert {entry["url"] for entry in panel["urls"]} == {
        "/admin/api/requests/in-flight?limit=50"
    }


def test_a_busy_answer_backs_the_poll_off_and_a_clean_one_resets_it(
    pause_polls,
) -> None:
    backoff = pause_polls["busyBackoff"]
    busy = _gaps(backoff["busyStarts"])
    calm = _gaps(backoff["calmStarts"])

    # x-mcc-busy on every answer for 60 s: 3 s doubles to 6, 12, 24.
    assert busy == [6_000, 12_000, 24_000]
    # The first clean answer puts it back on the chosen 3 s.
    assert calm and set(calm) == {3_000}


def test_an_error_backs_the_poll_off_up_to_the_cap(pause_polls) -> None:
    # The pulse at 15 s, failing: 30 s, then the 60 s cap.
    assert _gaps(pause_polls["errorBackoff"]["pulseStarts"]) == [30_000, 60_000]


def test_a_held_poll_is_never_doubled_and_gives_up(pause_polls) -> None:
    held = pause_polls["heldPoll"]

    assert held["maxConcurrent"] == 1
    assert held["urls"], "no poll went out -- the check above would be vacuous"
    for entry in held["urls"]:
        assert entry["outcome"] == "aborted", entry
        # Twice the 3 s interval, floored at 5 s.
        assert entry["end"] - entry["t"] == 6_000, entry


def test_only_one_tab_polls_and_the_other_paints_its_readings(pause_polls) -> None:
    tabs = pause_polls["twoTabs"]

    assert tabs["tab1Polls"] == 0
    assert tabs["tab2Polls"] >= 9
    # The polling tab asks for what the listening tab shows: its open panel.
    assert set(tabs["tab2InflightUrls"]) == {"/admin/api/requests/in-flight?limit=50"}
    assert tabs["tab1Badge"] == f"{tabs['serverRows']} in flight"
    assert tabs["tab1PanelRows"] == tabs["serverRows"]
    assert {"claim", "beat", "inflight", "need"} <= set(pause_polls["channel"]["types"])


def test_a_hidden_polling_tab_hands_over_at_once(pause_polls) -> None:
    hidden = pause_polls["hiddenLeader"]

    assert hidden["tab2Polls"] == 0
    assert hidden["tab1Polls"] >= 5
    # A resign on hiding, not on the next 2 s beat: the same instant here.
    assert hidden["tab1FirstPollAfter"] - hidden["hiddenAt"] <= 50


def test_a_dead_polling_tab_is_taken_over_within_the_lease(pause_polls) -> None:
    dead = pause_polls["deadLeader"]

    assert dead["tab2Polls"] == 0
    assert dead["tab1Polls"] >= 8
    # The 6 s lease, checked on the 2 s beat.
    assert dead["takeoverMs"] is not None
    assert dead["takeoverMs"] <= 8_000


def test_each_model_picker_offers_only_its_own_kind(rendered) -> None:
    """7.78.2: chat routes chat models, each media rail its own kind.

    A model of unknown kind stays inline on a chat route and is grouped under
    "Kind not known" on a media rail; a saved value of another kind heads its
    own picker, marked, and is never dropped.
    """

    kinds = rendered["modelKinds"]
    assert kinds["pickerKind"] == {
        "opus": "chat",
        "vision": "chat",
        "image": "image",
        "imageChainRow": "image",
        "tts": "tts",
        "asrChain": "asr",
        "video": "video",
        "agentTier": "chat",
    }
    assert kinds["chat"]["groups"] == [
        {
            "label": None,
            "options": ["p/chat-a"],
        },
        {"label": None, "options": ["p/chat-and-draw"]},
        {"label": None, "options": ["p/chat-b"]},
        {"label": None, "options": ["p/quiet"]},
    ]
    assert kinds["chatOptional"]["groups"][0] == {"label": None, "options": ["None"]}
    assert [group["options"][0] for group in kinds["chatOptional"]["groups"][1:]] == [
        "p/chat-a",
        "p/chat-and-draw",
        "p/chat-b",
        "p/quiet",
    ]
    assert kinds["image"]["groups"] == [
        {"label": "Image models", "options": ["p/chat-and-draw", "p/draw"]},
        {"label": "Kind not known", "options": ["p/quiet"]},
    ]
    assert kinds["image"]["filtered"] == ["p/chat-and-draw", "p/draw"]
    assert kinds["imageHoldingChat"]["groups"][0] == {
        "label": "Saved on this route, stated as another kind",
        "options": ["p/chat-a"],
    }
    assert kinds["imageHoldingChat"]["badges"] == ["Chat"]
    assert kinds["chatHoldingFilm"]["groups"][0] == {
        "label": "Saved on this route, stated as another kind",
        "options": ["p/film"],
    }
    assert kinds["chatHoldingFilm"]["groups"][1]["label"] == "Chat models"
    assert kinds["transcription"]["groups"] == [
        {"label": "Transcription models", "options": ["p/chat-b", "p/hear"]},
        {"label": "Kind not known", "options": ["p/quiet"]},
    ]
    assert kinds["agentTier"]["groups"][1] == {
        "label": "Saved on this route, stated as another kind",
        "options": ["p/speak"],
    }
    for picker in ("chat", "image", "imageHoldingChat", "transcription", "agentTier"):
        assert kinds[picker]["uniqueIds"], picker


def test_the_models_page_filters_and_marks_by_kind(rendered) -> None:
    """7.78.2: one facet per kind plus "Kind not known", and a chip on a row
    whose stated kind is not chat. Kinds overlap; unknown never does."""

    facets = {
        label.rsplit(" ", 1)[0]: int(label.rsplit(" ", 1)[1])
        for label, _pressed in rendered["models"]["facets"]
    }
    assert facets["Chat"] == 1
    assert facets["Image"] == 2
    assert facets["Speech"] == 0
    assert facets["Transcription"] == 0
    assert facets["Video"] == 0
    assert facets["Kind not known"] == 3 * 45 - 3
    assert rendered["models"]["kindChips"] == ["Image model", "Image model"]


# --------------------------- 7.81.0 keep trying exits until one answers


def test_a_chain_stored_before_the_box_reads_ticked_and_one_switched_off_unticked(
    rendered,
) -> None:
    served = rendered["proxying"]["untilServed"]
    existing = served["existing"]
    assert existing["hasKey"] is False  # the fixture is a pre-7.81.0 chain
    assert existing["present"] is True
    assert existing["checked"] is True  # 7.81.1: absent reads on
    assert existing["label"] == "Keep trying exits until one answers"
    assert existing["title"].startswith("For a provider whose limit follows")
    assert existing["forget"] is True
    assert existing["chips"] == [None, None, None, None]
    off = served["off"]
    assert off["checked"] is False
    assert off["title"].startswith("Off: this chain moves on the failures")
    assert off["forget"] is False


def test_a_ticked_chain_shows_what_it_remembers_and_offers_forget(rendered) -> None:
    ticked = rendered["proxying"]["untilServed"]["ticked"]
    assert ticked["checked"] is True
    assert 'It still stops after "Switches per request" refusals' in ticked["title"]
    assert "PROXY_COOLDOWN_SECONDS" in ticked["title"]
    assert ticked["forget"] == "Forget exit memory (1)"
    assert "NVIDIA NIM" in ticked["forgetTitle"]
    first, *others = ticked["chips"]
    assert first["text"] == f"remembered spent until {ticked['untilClock']}"
    assert "chat: spent (rate_limit) for key nvap…6789" in first["title"]
    assert "stated 300 s" in first["title"]
    assert others == [None, None, None]


def test_forget_posts_the_provider_only_and_clears_the_chip(rendered) -> None:
    forgot = rendered["proxying"]["untilServed"]["forgot"]
    assert forgot["writes"] == [
        {
            "path": "/admin/api/proxy-chains/forget",
            "method": "POST",
            "body": {"provider": "nvidia_nim"},
        }
    ]
    assert forgot["chips"] == [None, None, None, None]
    assert forgot["forget"] == "Forget exit memory"
    assert forgot["announcement"].startswith("NVIDIA NIM: forgot 1 remembered exit;")


def test_saving_always_names_the_box(rendered) -> None:
    saved = rendered["proxying"]["untilServed"]["saved"]
    assert saved is not None
    assert saved["body"]["until_served"] is False
    # The first save in this file, of the pre-7.81.0 chain, names it too --
    # ticked, the reading of a chain with no key since 7.81.1.
    assert rendered["proxying"]["saved"]["body"]["until_served"] is True


def test_a_dial_says_what_is_remembered_and_the_attempt_what_it_skipped(
    rendered,
) -> None:
    detail = rendered["requestDetail"]["proxyExitMemory"]
    assert detail["dialTitles"] == [
        "2 proxy dials · 1 switch · 4 skipped from memory",
        "No exit dialled · 3 skipped from memory (spent, blocked for their "
        "country, or unreachable)",
    ]
    assert detail["dials"] == [
        "#1 · 203.0.113.1:1080 · 429 after 1.2s · switched in 4 ms"
        " · remembered spent until 14:05:12 UTC (stated 412 s)",
        "#2 · 203.0.113.2:1080 · 403 after 900 ms · not switched"
        " · remembered blocked for its country until 14:05:12 UTC (300 s)",
    ]
    assert detail["ladderRootCauses"] == [
        "Every exit this request could use in Zen's proxy chain refused or"
        " failed it: tried 2."
    ]


# --------------------------------------------------- 7.89.0 (PR-S1/S2/S3)


def test_nothing_new_shows_until_exit_identity_or_a_scan(rendered) -> None:
    before = rendered["proxying"]["sourcesAndExits"]["before"]
    assert before["groups"] is False
    assert before["direct"] is False
    assert before["identityButton"] == "Use each provider's own trace"
    assert "It contacts nobody new." in before["identity"]
    assert before["sourcesHeading"] == "Proxies on this computer"
    assert before["sourcesStatus"] == "Not scanned yet."
    assert before["scan"] == "Scan this computer"
    assert before["sourceRows"] == 0


def test_one_click_turns_the_provider_trace_on(rendered) -> None:
    applied = rendered["proxying"]["sourcesAndExits"]["applied"]
    assert applied == [{"values": {"PROXY_CHECK_EXIT_IP_URL": "provider"}}]


def test_exit_identity_on_the_rows_and_the_group_on_the_card(rendered) -> None:
    on = rendered["proxying"]["sourcesAndExits"]["on"]
    assert on["identity"].startswith("Exit identity is on: each Test also asks")
    assert "Each provider's own host is also asked" in on["checker"]
    assert "short digest of this one's login" in on["labelTitle"]
    assert "exit 104.28.1.2 · NL · WARP" in on["readout"]
    assert "The provider's own host saw 104.28.1.2, in NL." in on["readoutTitle"]
    assert "Cloudflare WARP exit" in on["readoutTitle"]
    assert on["groups"].startswith(
        "2 entries share exit 104.28.1.2 (NL, WARP): 1. 203.0.113.7:1080#1a2b, "
        "2. 198.51.100.9:8080. The provider sees one address for all of them."
    )
    assert on["keep"] == "Keep one per exit"
    assert on["direct"].startswith(
        "Direct (this computer's own address): not checked yet."
    )


def test_keep_one_per_exit_pauses_the_duplicate(rendered) -> None:
    kept = rendered["proxying"]["sourcesAndExits"]["kept"]
    assert kept["posts"] == [
        {
            "path": "/admin/api/proxy-chains/keep-one-per-exit",
            "body": {"provider": "nvidia_nim"},
        }
    ]
    assert kept["paused"][:2] == [False, True]
    assert "Paused 1 entry on NVIDIA NIM" in kept["announcement"]


def test_the_direct_readout_is_asked_on_a_click(rendered) -> None:
    direct = rendered["proxying"]["sourcesAndExits"]["direct"]
    assert direct["posts"] == [
        {
            "path": "/admin/api/proxy-chains/direct-exit",
            "body": {"provider": "nvidia_nim"},
        }
    ]
    assert direct["line"].startswith(
        "Direct comes out of 185.159.157.13 (NL), as integrate.api.nvidia.com saw it"
    )


def test_a_scan_lists_three_listeners_with_their_offers(rendered) -> None:
    scanned = rendered["proxying"]["sourcesAndExits"]["scanned"]
    assert scanned["posts"][0] == "/admin/api/proxy-sources/scan"
    assert scanned["status"].endswith("3 ports answer on 127.0.0.1.")
    assert scanned["rescan"] == "Scan again"
    assert [row["port"] for row in scanned["rows"]] == ["1080", "9050", "40000"]
    tor = scanned["rows"][1]
    assert tor["name"] == "Local SOCKS5 · 127.0.0.1:9050"
    assert tor["usually"] == "usually Tor"
    assert tor["state"] == "SOCKS5 · in NVIDIA NIM"
    assert tor["add"] is True
    assert "Tor Project asks" in tor["sees"]
    first = scanned["rows"][0]
    assert first["state"] == "SOCKS5 · needs a username and password · on offer"
    assert first["login"] is True
    assert "Found 3 proxy listeners on this computer" in scanned["announcement"]


def test_an_offer_goes_through_the_bulk_add(rendered) -> None:
    added = rendered["proxying"]["sourcesAndExits"]["added"]
    assert len(added) == 1
    assert added[0]["action"] == "add"
    assert added[0]["provider"] == "nvidia_nim"
    assert added[0]["proxies"] == ["px_local9050"]


def test_a_login_is_sent_once_and_never_kept_on_the_page(rendered) -> None:
    login = rendered["proxying"]["sourcesAndExits"]["login"]
    assert login["puts"] == [
        {
            "method": "PUT",
            "body": {
                "source": "src_local",
                "credentials": [
                    {
                        "port": 1080,
                        "username": "me-user1",
                        "password": "pw-secret-9",
                        "clear": False,
                    }
                ],
            },
        }
    ]
    assert login["state"] == "SOCKS5 · login set (me-u…ser1) · on offer"
    assert login["pageHasPassword"] is False
    assert login["passwordField"] == ""


# --------------------------------------------- 7.90.0: a tor you run yourself


def test_the_tor_form_is_there_before_anything_is_scanned(rendered) -> None:
    empty = rendered["proxying"]["tor"]["empty"]
    assert empty["title"] == "Tor you run yourself"
    assert empty["intro"].startswith("MCC never downloads or starts tor.")
    assert "9050, 9052 and 9150 sit in a reserved range" in empty["intro"]
    assert empty["form"] is True
    assert empty["cancel"] is False
    assert empty["cards"] == 0
    # The scan's own part of the section is what it was.
    assert empty["sourceRows"] == 0
    assert empty["localStatus"] == "Not scanned yet."
    assert empty["oneEntry"].startswith(
        "A chain with only one proxy in it never switches and never falls back"
    )


def test_a_tor_form_with_a_bad_port_sends_nothing(rendered) -> None:
    bad = rendered["proxying"]["tor"]["badForm"]
    assert bad["puts"] == 0
    assert bad["announcement"].startswith("Give your tor's SOCKS ports as numbers")


def test_saving_tor_ports_sends_them_once_and_shows_the_card(rendered) -> None:
    tor = rendered["proxying"]["tor"]
    assert tor["passwordHiddenAtFirst"] is True
    assert tor["passwordShown"] is True
    saved = tor["saved"]
    assert saved["puts"] == [
        {
            "method": "PUT",
            "body": {
                "source": "",
                "kind": "tor",
                "tor": {
                    "socks_ports": [19250, 19251, 19252],
                    "control_port": 19260,
                    "auth": "password",
                    "password": "tor-pw-secret-1",
                },
            },
        }
    ]
    assert saved["pageHasPassword"] is False
    assert saved["name"] == "Tor · control port 127.0.0.1:19260"
    assert saved["login"] == "· logs in with the control password (stored, never shown)"
    assert saved["status"].startswith("Not checked yet.")
    assert [row["name"] for row in saved["ports"]] == [
        "Tor · 127.0.0.1:19250",
        "Tor · 127.0.0.1:19251",
        "Tor · 127.0.0.1:19252",
    ]
    assert all(row["state"] == "on offer" and row["add"] for row in saved["ports"])
    assert saved["addAll"] == "Add all to chain"
    assert saved["torrc"].splitlines()[1:] == [
        "SocksPort 127.0.0.1:19250",
        "SocksPort 127.0.0.1:19251",
        "SocksPort 127.0.0.1:19252",
        "ControlPort 127.0.0.1:19260",
        "CookieAuthentication 1",
    ]
    assert saved["copy"] == "Copy torrc lines"
    assert "Tor Project asks" in saved["sees"]
    assert saved["buttons"] == [
        "New Tor identity",
        "Check Tor",
        "Change ports",
        "Remove",
    ]
    assert "MCC never sends one by itself" in saved["newnymTitle"]
    assert saved["another"] == "Add another tor"
    assert saved["form"] is False
    assert saved["announcement"].startswith("Saved. Paste the torrc lines")


def test_check_tor_shows_its_status_and_which_ports_tor_listens_on(rendered) -> None:
    checked = rendered["proxying"]["tor"]["checked"]
    assert checked["posts"] == [
        {"path": "/admin/api/proxy-sources/tor/status", "body": {"source": "src_tor"}}
    ]
    assert checked["status"].startswith(
        "Tor 0.4.8.13 · circuit established · logged in with the cookie (SAFECOOKIE)."
    )
    assert checked["ports"] == [
        "tor listens here · on offer",
        "tor listens here · on offer",
        "tor does not list this port -- paste the lines below and restart tor "
        "· on offer",
    ]


def test_a_second_new_identity_shows_the_seconds_left(rendered) -> None:
    tor = rendered["proxying"]["tor"]
    assert tor["newnym"]["posts"] == [
        {"path": "/admin/api/proxy-sources/tor/newnym", "body": {"source": "src_tor"}}
    ]
    assert tor["newnym"]["announcement"].startswith("Tor accepted: new requests")
    assert tor["newnym"]["note"].endswith("Tor allows the next one in 10 s.")
    assert tor["again"]["announcement"] == (
        "Tor allows one new identity per 10 seconds: try again in 7 s. "
        "Nothing was sent."
    )
    assert tor["again"]["note"].endswith("Tor allows the next one in 7 s.")


def test_every_tor_port_goes_into_a_chain_through_the_one_bulk_add(rendered) -> None:
    added = rendered["proxying"]["tor"]["addedAll"]
    assert len(added) == 1
    assert added[0]["action"] == "add"
    assert added[0]["provider"] == "nvidia_nim"
    assert added[0]["proxies"] == ["px_tor19250", "px_tor19251", "px_tor19252"]


def test_changing_tor_ports_starts_from_what_is_saved(rendered) -> None:
    editing = rendered["proxying"]["tor"]["editing"]
    assert editing == {
        "socks": "19250, 19251, 19252",
        "control": "19260",
        "auth": "password",
        "passwordPlaceholder": "stored -- type to replace",
        "cancel": True,
    }


def test_removing_the_tor_source_brings_the_form_back(rendered) -> None:
    removed = rendered["proxying"]["tor"]["removed"]
    assert removed["puts"] == [{"source": "src_tor", "remove": True}]
    assert removed["cards"] == 0
    assert removed["form"] is True


# 7.91.0: VPN accounts, commercial gateways, proxy lists -------------------


def test_the_vendor_blocks_are_there_and_ask_for_nothing_until_a_form_opens(
    rendered,
) -> None:
    empty = rendered["proxying"]["vendors"]["empty"]
    assert empty["titles"] == [
        "VPN accounts",
        "Commercial proxy gateways",
        "Proxy lists from your provider",
    ]
    assert empty["adds"] == ["Add a VPN account", "Add a gateway", "Add a proxy list"]
    assert empty["cards"] == 0
    # Presets are data the page asks for only when a form opens.
    assert empty["presetGets"] == 0
    assert any(line.startswith("No VPN vendor found allows") for line in empty["terms"])


def test_a_vpn_account_form_is_prefilled_from_the_vendor_preset(rendered) -> None:
    form = rendered["proxying"]["vendors"]["accountForm"]
    assert form["presetGets"] == 1
    assert form["preset"] == "nordvpn"
    assert form["choices"] == [
        "NordVPN",
        "Mullvad (inside its tunnel)",
        "Another vendor (type it yourself)",
    ]
    assert form["hosts"].splitlines() == [
        "nl.socks.nordhold.net",
        "se.socks.nordhold.net",
        "us.socks.nordhold.net",
    ]
    assert (form["scheme"], form["port"]) == ("socks5h", "1080")
    assert form["listUrl"].startswith("https://api.nordvpn.com/v1/servers")
    assert "port 89" in form["note"] and "untested" in form["note"]
    assert form["doc"] == "https://support.nordvpn.com/hc/en-us/articles/20195967385745"
    assert form["passwordType"] == "password"


def test_saving_a_vpn_account_sends_it_once_and_keeps_no_secret_on_the_page(
    rendered,
) -> None:
    saved = rendered["proxying"]["vendors"]["accountSaved"]
    assert len(saved["puts"]) == 1
    body = saved["puts"][0]
    assert (body["source"], body["kind"]) == ("", "account")
    sent = body["account"]
    assert sent["preset"] == "nordvpn"
    assert (sent["username"], sent["password"]) == (
        "service-user-1",
        "nord-secret-pass-7",
    )
    assert sent["countries"] == ["nl", "SE"]
    assert (sent["fetch_via"], sent["refresh_hours"]) == ("", 0)
    assert saved["pageHasPassword"] is False
    assert saved["pageHasUser"] is False
    assert saved["head"].startswith("NordVPN · SOCKS5 · logs in as serv…ce-1")
    assert saved["status"].startswith(
        "Server list at https://api.nordvpn.com (kept: nl, SE) -- fetched only "
        "when you press Fetch now, from this computer. Not fetched yet."
    )
    assert saved["offers"] == [
        "NordVPN · nl.socks.nordhold.net",
        "NordVPN · se.socks.nordhold.net",
        "NordVPN · us.socks.nordhold.net",
    ]
    assert saved["buttons"] == ["Fetch now", "Change", "Remove"]
    assert saved["form"] is False
    assert saved["announcement"].startswith("Saved: 3 on offer.")


def test_fetch_now_reads_the_server_list_and_shows_its_countries(rendered) -> None:
    fetched = rendered["proxying"]["vendors"]["accountFetched"]
    assert fetched["posts"] == [
        {"path": "/admin/api/proxy-sources/fetch", "body": {"source": "src_v_account"}}
    ]
    assert fetched["offers"] == [
        "NordVPN · socks-nl1.nordvpn.com",
        "NordVPN · socks-nl2.nordvpn.com",
    ]
    assert fetched["states"] == ["NL, Amsterdam · on offer"] * 2
    assert "Fetched: 2 SOCKS5 servers (NL); 2 kept for NL." in fetched["status"]
    assert fetched["announcement"] == "Fetched: 2 SOCKS5 servers (NL); 2 kept for NL."


def test_a_bright_data_residential_zone_is_refused_with_the_reason(rendered) -> None:
    vendors = rendered["proxying"]["vendors"]
    form = vendors["gatewayForm"]
    assert form["presetGets"] == 1  # asked once, then kept
    assert form["preset"] == "brightdata"
    assert form["choices"] == ["Bright Data", "Oxylabs"]
    assert (form["host"], form["port"]) == ("brd.superproxy.io", "22228")
    assert form["zoneTypes"] == [
        "Datacenter zone",
        "ISP zone",
        "Residential zone (refused)",
        "Mobile zone (refused)",
    ]
    refused = vendors["gatewayRefused"]
    assert refused["puts"] == 1
    assert refused["announcement"].startswith(
        "Bright Data's residential and mobile zones need Bright Data's own "
        "certificate authority"
    )
    assert refused["formStillOpen"] is True


def test_a_gateway_is_n_sessions_each_its_own_address(rendered) -> None:
    vendors = rendered["proxying"]["vendors"]
    assert vendors["oxylabsForm"] == {
        "host": "pr.oxylabs.io",
        "port": "7777",
        "minutes": "30",
        "zone": False,
    }
    saved = vendors["gatewaySaved"]
    sent = saved["puts"][0]["gateway"]
    assert sent["preset"] == "oxylabs"
    assert (sent["user"], sent["password"], sent["count"], sent["minutes"]) == (
        "acme-user",
        "oxy-secret-pass-5",
        3,
        30,
    )
    assert saved["pageHasPassword"] is False
    assert saved["head"] == (
        "Oxylabs · pr.oxylabs.io:7777 · 3 sessions · 30 min sessions · logs in "
        "as acme…user (stored, never shown)"
    )
    assert saved["offers"] == [f"Oxylabs · session first00{n}" for n in (1, 2, 3)]
    assert saved["buttons"] == ["New sessions", "Change", "Remove"]
    assert saved["addAll"] == "Add all to chain"


def test_every_gateway_session_goes_into_a_chain_through_the_one_bulk_add(
    rendered,
) -> None:
    added = rendered["proxying"]["vendors"]["gatewayAddedAll"]
    assert len(added) == 1
    assert (added[0]["action"], added[0]["provider"]) == ("add", "nvidia_nim")
    assert len(added[0]["proxies"]) == 3


def test_new_sessions_sends_no_password_and_asks_for_renewal(rendered) -> None:
    renewed = rendered["proxying"]["vendors"]["gatewayRenewed"]
    assert len(renewed["puts"]) == 1
    sent = renewed["puts"][0]["gateway"]
    assert sent["renew"] is True
    assert "password" not in sent and "user" not in sent
    assert sent["count"] == 3
    assert renewed["offers"] == [f"Oxylabs · session renew00{n}" for n in (1, 2, 3)]


def test_a_proxy_list_link_is_write_only_and_the_schedule_starts_off(rendered) -> None:
    vendors = rendered["proxying"]["vendors"]
    form = vendors["listForm"]
    assert form["preset"] == "webshare"
    assert form["urlType"] == "password"
    assert form["refresh"] == [
        "Only when I press Fetch now",
        "Every 6 hours",
        "Every 24 hours",
        "Every 7 days",
    ]
    assert form["refreshValue"] == "0"
    assert form["via"][0] == "Fetch from this computer"
    assert all(option.startswith("Fetch through ") for option in form["via"][1:])
    saved = vendors["listSaved"]
    sent = saved["puts"][0]["proxy_list"]
    assert "JSDOM-TOKEN-77" in sent["url"]
    assert sent["refresh_hours"] == 0
    assert saved["pageHasToken"] is False
    assert "download link at https://proxy.webshare.io" in saved["head"]
    assert saved["none"] == "Nothing on offer yet: press Fetch now to read the list."
    fetched = vendors["listFetched"]
    assert fetched["posts"] == [
        {"path": "/admin/api/proxy-sources/fetch", "body": {"source": "src_v_list"}}
    ]
    assert fetched["offers"] == [
        "Webshare · 198.51.100.10:6540",
        "Webshare · 198.51.100.11:6541",
    ]


def test_removing_a_vendor_source_sends_one_put(rendered) -> None:
    removed = rendered["proxying"]["vendors"]["removed"]
    assert removed["puts"] == [{"source": "src_v_gateway", "remove": True}]
    assert removed["gatewayCards"] == 0
    assert removed["presetGets"] == 1


# ---------------------------- 7.91.2 the Requests page's half of the search fix
#
# One run of admin_jsdom_search_page.mjs (MCC_JSDOM_SCENARIO=search_page): the
# real admin.js on a manual clock, opening on the Requests view with an
# all-time "too long" saved from an earlier visit, against a fetch layer that
# holds every search answer until the scenario ends its pass and ends a
# request "aborted" when its signal is. The user's decisions of 2026-10-10:
# Q1 the saved search is restored, not run; Q2 one pass with a progress line,
# the list first, count and cards together; Q11 typing aborts the previous
# load; Q12 auto-refresh stays on, checking only new rows.

DASH = chr(0x2014)
DOT = chr(0xB7)
ELLIPSIS = chr(0x2026)
EN_DASH = chr(0x2013)
# What a load with a search asks while its pass reads: the list (answered
# early), and the count and the cards (answered when it ends). The four panels
# the pass also answers ask once it has ended, so that a browser's six
# connections to the server are never all held by one search.
DURING_PASS = sorted(
    (
        "/admin/api/requests",
        "/admin/api/requests/count",
        "/admin/api/requests/stats",
    )
)
AFTER_PASS = sorted(
    (
        "/admin/api/requests/cost",
        "/admin/api/requests/ttft",
        "/admin/api/requests/no-answer",
        "/admin/api/requests/origin",
    )
)
SEARCH_ANSWERS = sorted((*DURING_PASS, *AFTER_PASS))
SAVED_NOTICE = f"Saved search 'too long' (all time) {DASH} Run {DOT} Clear"
# The user's own line: "searched back to 12 Aug · 412,000 of 598,683 rows".
PROGRESS_SEP_11 = f"searched back to 11 Sep {DOT} 100,000 of 598,683 rows"
PROGRESS_AUG_12 = f"searched back to 12 Aug {DOT} 412,000 of 598,683 rows"
POLL_PATHS = ("/admin/api/requests/in-flight", "/admin/api/requests/pulse")


@pytest.fixture(scope="module")
def search_page() -> dict:
    return _run(MCC_JSDOM_SCENARIO="search_page")


def _paths(entries: list[dict]) -> list[str]:
    return sorted(entry["path"] for entry in entries)


def test_the_search_page_scenario_runs_clean(search_page) -> None:
    assert search_page["fatal"] is None
    assert search_page["scriptErrors"] == []
    assert search_page["consoleErrors"] == []
    assert search_page["clockErrors"] == []
    # Run and Clear exist: a page without them runs the saved search at once.
    assert search_page["missingControls"] == []


def test_a_saved_search_is_restored_but_not_run_at_start_up(search_page) -> None:
    """Q1: the text and a notice, and not one request carrying the search."""

    assert search_page["activeView"] == "requests"
    start = search_page["startup"]
    assert start["box"] == "too long"
    assert start["noticeShown"] is True
    assert start["actionsShown"] is True
    assert start["notice"] == SAVED_NOTICE
    # Nothing else about the saved state changed.
    assert start["persistedSearch"] == "too long"
    # The view loaded -- the table and the cards -- without the search.
    loaded = {entry["path"] for entry in search_page["startupRequests"]}
    assert {"/admin/api/requests", "/admin/api/requests/stats"} <= loaded
    assert search_page["startupSearchRequests"] == []
    assert all("q=" not in entry["url"] for entry in search_page["startupRequests"])
    assert start["cards"] > 0
    assert start["countingCards"] == 0
    # A minute of auto-refresh later: pulses, and still no search.
    idle = search_page["idleMinute"]
    assert idle["pulses"] >= 3
    assert idle["searchRequests"] == []
    assert idle["after"]["notice"] == SAVED_NOTICE


def test_run_starts_the_saved_search_and_every_search_request_can_be_aborted(
    search_page,
) -> None:
    run = search_page["run"]
    searching = [entry for entry in run["requests"] if entry["q"]]
    assert _paths(searching) == DURING_PASS
    assert all(entry["q"] == "too long" for entry in searching)
    assert all(entry["signal"] for entry in searching)
    # The requests without the search are as they were: no signal.
    assert all(not entry["signal"] for entry in run["requests"] if not entry["q"])
    after = run["afterClick"]
    assert after["actionsShown"] is False
    assert after["notice"] == f"Searching 'too long' (all time){ELLIPSIS}"
    # The four panels, once the pass has ended: same search, same signal.
    panels = search_page["panelsAfterPass"]
    assert _paths(panels) == AFTER_PASS
    assert all(e["q"] == "too long" and e["signal"] for e in panels)
    assert all(e["outcome"] == "ok" for e in panels)


def test_the_list_fills_first_and_the_progress_line_follows_the_pass(
    search_page,
) -> None:
    """Q2: rows at once; then where it said "counting…", how far it has read."""

    first = search_page["run"]["listFilledFirst"]
    assert first["tableRows"] == 3
    assert first["countOpen"] is True
    assert first["countingCards"] == search_page["run"]["afterClick"]["cards"] > 0

    one = search_page["progress1"]
    two = search_page["progress2"]
    assert one["notice"] == f"Searching 'too long' (all time) {DASH} {PROGRESS_SEP_11}"
    assert one["pager"] == f"1{EN_DASH}3 {DOT} {PROGRESS_SEP_11}"
    assert two["notice"] == f"Searching 'too long' (all time) {DASH} {PROGRESS_AUG_12}"
    assert two["pager"] == f"1{EN_DASH}3 {DOT} {PROGRESS_AUG_12}"
    assert two["countingCards"] == two["cards"]
    polls = search_page["progressRequests"]
    assert [entry["t"] for entry in polls] == [
        polls[0]["t"],
        polls[0]["t"] + 1000,
    ]
    assert all(entry["q"] == "too long" and entry["signal"] for entry in polls)


def test_no_pulse_is_sent_while_the_pass_runs(search_page) -> None:
    """Q12: a pulse with the search would wait for that same pass."""

    during = search_page["duringPass"]
    assert during["pulses"] == []
    assert during["countsOrStats"] == []
    # Thirty-one seconds of the progress line instead, once a second.
    assert 30 <= during["progressPolls"] <= 32
    assert during["after"]["pager"] == f"1{EN_DASH}3 {DOT} {PROGRESS_AUG_12}"


def test_the_count_and_the_cards_land_together_when_the_pass_ends(search_page) -> None:
    half = search_page["countAnsweredStatsNot"]
    # The count has answered and the stats have not: nothing is painted yet.
    assert half["pager"] == f"1{EN_DASH}3 {DOT} {PROGRESS_AUG_12}"
    assert half["countingCards"] == half["cards"]
    ended = search_page["passEnded"]
    assert ended["pager"] == f"1{EN_DASH}7 of 7"
    assert ended["countingCards"] == 0
    assert ended["noticeShown"] is False
    # And the progress line stops asking.
    assert search_page["afterPassProgressPolls"] == 0


def test_auto_refresh_after_the_pass_reads_only_the_table_and_the_pulse(
    search_page,
) -> None:
    """Q12: after the pass, a tick is the pulse; a change re-reads the table.

    Both carry the search, and the server answers both from the finished
    pass plus the rows written since (``test_search_pool``); nothing asks
    for the count, the cards or a new pass.
    """

    auto = search_page["autoAfterPass"]
    baseline = [e for e in auto["baseline"] if e["path"] != POLL_PATHS[0]]
    changed = [e for e in auto["changed"] if e["path"] != POLL_PATHS[0]]
    assert [e["path"] for e in baseline] == ["/admin/api/requests/pulse"]
    assert baseline[0]["q"] == "too long"
    assert _paths(changed) == sorted(
        (
            "/admin/api/requests/pulse",
            "/admin/api/requests",
            "/admin/api/requests/lifetime",
        )
    )
    assert all(
        e["q"] == "too long"
        for e in changed
        if e["path"] != "/admin/api/requests/lifetime"
    )
    assert all(e["outcome"] == "ok" for e in changed)


def test_typing_aborts_every_search_request_of_the_load_it_replaces(
    search_page,
) -> None:
    """Q11: three prefixes 400 ms apart; only the last one's requests live."""

    typing = search_page["typing"]
    loads = typing["loads"]
    assert [load["prefix"] for load in loads] == ["zq", "zqx", "zqxj"]
    starts = [min(entry["t"] for entry in load["requests"]) for load in loads]
    assert [later - earlier for earlier, later in pairwise(starts)] == [400, 400]
    for superseded in loads[:2]:
        # Aborted, and its four panels never asked at all.
        assert _paths(superseded["requests"]) == DURING_PASS
        assert all(entry["signal"] for entry in superseded["requests"])
        assert all(entry["outcome"] == "aborted" for entry in superseded["requests"])
    last = loads[2]
    assert _paths(last["requests"]) == DURING_PASS
    assert all(entry["outcome"] == "pending" for entry in last["requests"])
    assert typing["abortCalls"] >= 2
    finished = [
        entry
        for entry in typing["lastLoadAfterPass"]
        if entry["path"] != "/admin/api/requests/search-progress"
    ]
    assert _paths(finished) == SEARCH_ANSWERS
    assert all(entry["outcome"] == "ok" for entry in finished)
    assert typing["afterPass"]["noticeShown"] is False


def test_the_cards_say_counting_from_the_start_of_a_search(search_page) -> None:
    """A word few rows contain: the list waits for the pass, the cards do not.

    Here the list is held until the pass ends. The cards say "counting…" at
    once instead of the last view's numbers, under the progress line.
    """

    during = search_page["typing"]["progressLine"]
    assert during["notice"] == f"Searching 'zqxj' (all time){ELLIPSIS}"
    assert during["countingCards"] == during["cards"] > 0
    # Before a second is up the last view's rows stay (a common word's page
    # is in by then); after it, the table says it is searching instead.
    assert during["tableRows"] == 3
    table = search_page["typing"]["searchingTable"]
    assert table["rows"] == 1
    assert table["firstCell"].startswith(f"Searching{ELLIPSIS} the matching requests")
    assert table["pager"] == f"counting{ELLIPSIS}"


def test_clear_empties_the_box_and_forgets_the_saved_search(search_page) -> None:
    again = search_page["restoredAgain"]
    assert (
        again["notice"] == f"Saved search 'too long' (last 7d) {DASH} Run {DOT} Clear"
    )
    clear = search_page["clear"]
    assert clear["after"]["box"] == ""
    assert clear["after"]["noticeShown"] is False
    assert clear["after"]["persistedSearch"] is None
    loads = [entry for entry in clear["requests"] if entry["path"] not in POLL_PATHS]
    assert loads == []


def test_enter_runs_a_saved_search(search_page) -> None:
    enter = search_page["enter"]
    assert _paths(enter["searchRequests"]) == SEARCH_ANSWERS
    assert all(entry["q"] == "too long" for entry in enter["searchRequests"])
    assert enter["after"]["persistedSearch"] == "too long"


def test_the_export_still_carries_the_search(search_page) -> None:
    assert "q=too+long" in search_page["heldExportUrl"]
    assert "q=too+long" in search_page["runExportUrl"]


# ------------------------------------------------- MCC media server (7.99.0)
# The user's 2026-10-18 12:53 amendment: an explicit Add MCP server / Remove
# MCP server button on each agent card, showing the current state; the server
# referenced on Get Started and below each media rail on Model Config.


def test_each_agent_card_shows_its_mcp_server_button_state(rendered: dict) -> None:
    before = rendered["mcpMedia"]["before"]

    off = before["claude"]
    assert off["hidden"] is False
    assert off["button"] == "Add MCP server"
    assert off["action"] == "add"
    assert off["state"].startswith("No MCP server")

    on = before["codex"]
    assert on["button"] == "Remove MCP server"
    assert on["action"] == "remove"
    assert on["state"].startswith("MCP server added")
    assert on["notes"][0].startswith("Codex asks before")

    # No MCP client: the card says so and offers nothing to press.
    aider = before["aider"]
    assert aider["button"] is None
    assert aider["state"] == "MCP server: not possible, no MCP client"

    # The desktop card carries its own button, and says what is still to do.
    desktop = before["claude_desktop"]
    assert desktop["inDesktopCard"] is True
    assert desktop["button"] == "Remove MCP server"
    assert desktop["pending"].startswith("Not written yet")


def test_add_mcp_server_posts_one_app_and_flips_the_button(rendered: dict) -> None:
    media = rendered["mcpMedia"]
    assert media["posted"] == [
        {"method": "POST", "body": {"app": "claude", "add": True}}
    ]
    assert media["after"]["claude"]["button"] == "Remove MCP server"
    assert media["after"]["claude"]["state"].startswith("MCP server added")
    # Every other card is as it was.
    assert media["after"]["codex"] == media["before"]["codex"]
    # The setting at the foot of the page follows the buttons and is not left
    # reading as an unsaved edit.
    assert media["fieldValueAfter"] == "claude,codex,claude_desktop"
    assert media["fieldOriginalAfter"] == media["fieldValueAfter"]
    assert media["messageAfter"] == "Added to claude."


def test_mcc_media_server_card_shows_status_bytes_and_copy_line(rendered: dict) -> None:
    media = rendered["mcpMedia"]
    assert media["present"] is True
    assert media["serverPill"] == "Installed"
    assert "mcc-mcp.exe" in media["serverCard"]
    assert "about 11.5 KB of tool text" in media["serverCard"]
    assert "Added to: Codex CLI, Claude Desktop." in media["serverCard"]
    assert media["rails"] == [
        "Image: p1/i0 -> generate_image, edit_image",
        "Speech: p1/t0 -> speak",
        "Transcription: empty",
        "Video: p1/v0 -> create_video, get_video",
    ]
    assert len(media["copyBlocks"]) == 2
    assert media["copyBlocks"][0].endswith("--app <your-app>")
    assert media["guideAnchor"] == "guide-media-mcp"
    assert media["guideLabel"] == "Guide"
    assert media["fieldRendered"] is True
    assert media["fieldValue"] == "codex,claude_desktop"


def test_model_config_media_rails_say_how_an_agent_reaches_them(rendered: dict) -> None:
    rail = rendered["imageRail"]
    notes = {note["rail"]: note for note in rail["mcpNotes"]}
    assert list(notes) == ["image", "tts", "asr", "video"]
    assert all(note["inMediaCard"] for note in notes.values())
    assert "as generate_image and edit_image" in notes["image"]["text"]
    assert "as speak" in notes["tts"]["text"]
    assert "as transcribe and translate_audio" in notes["asr"]["text"]
    assert "as create_video and get_video" in notes["video"]["text"]
    # Counted from MCP_MEDIA_APPS (the fixture's two agents); display only.
    assert "2 agents have it." in notes["image"]["text"]
    assert notes["image"]["link"] == "Add or remove it on Coding agents"
    assert rail["mcpLinkView"] == "coding_agents"


def test_get_started_points_at_the_add_mcp_server_button(
    rendered: dict, monkeypatch, tmp_path: Path
) -> None:
    from my_claude_code.config.onboarding import build_state

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    server_step = next(
        step
        for step in build_state(
            claude_settings_configured=False, has_requests=False
        ).steps
        if step.id == "media_tools"
    )
    (step,) = rendered["getStartedMedia"]["steps"]
    # The fixture's words are the server's step, so this is the real item --
    # rendered inside the "Media & agent tools" card since 7.122.0, whose
    # "How to do it" block carries the step's label, instructions and Guide
    # link. The essentials list holds the four essentials only.
    assert step["label"] == server_step.label
    assert step["instructions"] == list(server_step.instructions[:2])
    assert server_step.view == "coding_agents"
    assert server_step.target == "#mcpMediaServerCard"
    assert rendered["getStartedMedia"]["cards"] == 1
    assert step["expanded"] is False
    assert rendered["getStartedMedia"]["mediaStepInEssentialsList"] is False
    assert step["guides"] == [
        {"anchor": "guide-media-mcp", "text": "Guide: media server"}
    ]


# ------------------------------------- MCC media server in your own files (7.100.0)
# Decision 11: the Desktop apps cards add the server to the user's own file,
# with the agent cards' Add / Remove MCP server wording, the plan diff before
# anything is written, a one-time backup and Undo.


def test_desktop_cards_show_the_media_server_in_the_apps_own_file(
    rendered: dict,
) -> None:
    files = rendered["desktopApps"]["mcpFiles"]

    codex = files["codex_desktop"]
    assert codex["inCard"] is True
    assert codex["pill"] == "No MCP server"
    assert codex["buttons"] == [
        {"label": "Add MCP server", "action": "add", "disabled": False}
    ]
    assert codex["metaTerms"] == ["MCP file", "MCC owns", "Backup"]
    assert codex["metaValues"][:2] == ["~/.codex/config.toml", "mcp_servers.mcc_media"]
    assert "taken once" in codex["metaValues"][2]
    assert codex["notes"][0].startswith("Codex asks before")

    opencode = files["opencode_desktop"]
    assert opencode["pill"] == "MCP server added"
    assert opencode["buttons"] == [
        {"label": "Remove MCP server", "action": "remove", "disabled": False}
    ]
    assert "permission.mcc_media_*" in opencode["metaValues"][1]
    assert opencode["metaValues"][2].startswith("/home/u/opencode.json.mcc-backup")

    # Claude Desktop: MCC's own document stays the primary (its PR 1 button is
    # on the same card); this is the fallback, and it says what it costs.
    claude = files["claude_desktop"]
    assert claude["pill"] == "No MCP server (fallback)"
    assert claude["buttons"][0]["label"] == "Add MCP server (fallback)"
    assert claude["pending"].startswith("Fallback, 60 s tool limit there.")

    # An entry MCC did not write: named, never offered a button.
    foreign = files["codex_removed"]
    assert foreign["pill"] == "An mcc_media entry MCC did not add"
    assert foreign["buttons"] == []
    assert "It is yours" in foreign["state"]

    # Not measured yet: a dated line, no button.
    assert files["roo_code"]["buttons"] == []
    assert files["roo_code"]["notes"][0].startswith("MCP server: not measured yet")
    # A card with no MCP file at all has no block.
    assert files["goose_desktop"] is None


def test_add_mcp_server_on_a_desktop_card_shows_the_plan_before_writing(
    rendered: dict,
) -> None:
    flow = rendered["desktopApps"]["mcpFlow"]

    # The first press asks for the plan and writes nothing.
    assert flow["planPosts"] == [{"method": "POST", "body": {"add": True}}]
    assert flow["writesBeforeConfirm"] == 0
    assert flow["previewHidden"] is False
    assert flow["previewText"].startswith("--- /home/u/.codex/config.toml")
    assert "+[mcp_servers.mcc_media]" in flow["previewText"]
    assert (
        "-> Before writing, MCC copies the file once to "
        "/home/u/.codex/config.toml.mcc-backup."
    ) in flow["previewText"]
    assert flow["previewText"].endswith(
        "-> Nothing is written until you press Write it."
    )
    assert flow["confirmHidden"] is False
    assert flow["confirmLabel"] == "Write it"

    # The second press is the one write, and the card follows it.
    assert flow["writes"] == [{"method": "POST", "body": {"add": True}}]
    assert flow["after"]["pill"] == "MCP server added"
    assert flow["after"]["buttons"][0]["label"] == "Remove MCP server"
    assert flow["status"] == (
        "Added MCC's media server to /home/u/.codex/config.toml. Your original "
        "is at /home/u/.codex/config.toml.mcc-backup. Restart Codex desktop to "
        "load it."
    )


# ------------------------------------------------- Install buttons (7.102.0)
# The user's words (2026-10-06): buttons on the Coding agents tab for each
# agent, CLI and desktop, to download and install it, and Node.js and npm too
# when they are missing. One run of admin_jsdom_agent_installs.mjs
# (MCC_JSDOM_SCENARIO=agent_installs) on payloads in the routes' own shapes.


@pytest.fixture(scope="module")
def agent_installs() -> dict:
    return _run(MCC_JSDOM_SCENARIO="agent_installs")


def test_jsdom_the_agent_installs_scenario_runs_clean(agent_installs) -> None:
    assert agent_installs["fatal"] is None
    assert agent_installs["scenarioFatal"] is None
    assert agent_installs["scriptErrors"] == []
    assert agent_installs["consoleErrors"] == []


def test_jsdom_install_button_only_when_not_installed(agent_installs) -> None:
    cards = agent_installs["cards"]

    claude = cards["claude"]
    assert claude["hidden"] is False
    assert claude["inCard"] is True
    assert claude["button"] == "Install"
    # A picker where this OS has more than one method; the vendor's own
    # installer is the one selected.
    assert [o["value"] for o in claude["picker"]] == ["native", "winget", "npm"]
    assert [o["value"] for o in claude["picker"] if o["selected"]] == ["native"]

    # Installed: the path and the version, and no button of any kind.
    codex = cards["codex"]
    assert codex["button"] is None
    assert codex["picker"] is None
    assert codex["copy"] is None
    assert codex["state"] == (
        "Installed C:\\Users\\u\\.local\\bin\\codex.exe · codex-cli 0.163.0"
    )

    # One method: no picker, still a button.
    gemini = cards["gemini_cli"]
    assert gemini["button"] == "Install"
    assert gemini["picker"] is None

    # Kimi waits for its check (decision 6) and says so; no button.
    kimi = cards["kimi_code"]
    assert kimi["button"] is None
    assert "has not yet checked that mcc-kimi works" in kimi["text"]

    # No direct download (decision 8): the vendor's page, never a button.
    warp = cards["warp"]
    assert warp["button"] is None
    assert warp["link"] == {
        "href": "https://www.warp.dev/download",
        "text": "Warp's download page",
    }
    # A desktop card that is a CLI: installed from that card, once.
    crush = cards["crush_desktop"]
    assert crush["button"] is None
    assert "same program as the Crush card" in crush["text"]


def test_jsdom_copy_command_beside_the_button(agent_installs) -> None:
    claude = agent_installs["cards"]["claude"]
    assert claude["copyBesideButton"] is True
    assert claude["copy"] == {
        "text": "Copy command",
        "data": "irm https://claude.ai/install.ps1 | iex",
        "disabled": False,
    }
    # The copy line follows the picker, and so does the line under it.
    assert agent_installs["copyAfterPick"]["data"] == (
        "npm install -g @anthropic-ai/claude-code"
    )
    assert "Needs Node.js 22 or newer" in agent_installs["detailsAfterPick"]
    assert "installs it first" in agent_installs["detailsAfterPick"]
    assert agent_installs["copied"] == [
        "irm https://claude.ai/install.ps1 | iex",
        "npm install -g @anthropic-ai/claude-code",
    ]


def test_jsdom_plan_dialog_states_elevation_and_profile_changes(agent_installs) -> None:
    claude = agent_installs["claudePlan"]
    assert claude["role"] == "dialog"
    assert claude["heading"] == (
        "Install Claude Code with Anthropic's installer (recommended)"
    )
    assert claude["changes"] == [
        "puts claude in ~/.local/bin and adds that folder to your PATH",
        "Claude Code then updates itself in the background",
    ]
    assert any(
        "-ExecutionPolicy Bypass -File <downloaded file>" in step
        for step in claude["steps"]
    )
    assert claude["confirm"] == "Install now"
    assert claude["back"] == "Not now"
    assert any("never runs sudo or runas" in note for note in claude["notes"])
    # Asking for the plan ran nothing: one plan request, no run, no job.
    assert [r["url"] for r in agent_installs["requestsAfterClaudePlan"]] == [
        "/admin/api/agent-installs",
        "/admin/api/agent-installs/claude/plan",
    ]

    gemini = agent_installs["geminiPlan"]
    assert gemini["node"] == [
        "Node.js LTS will be installed first (the newest LTS nodejs.org lists "
        "when you press Install), then @google/gemini-cli."
    ]
    assert gemini["elevation"] == [
        "Windows will ask for administrator permission (the Node.js installer)."
    ]
    assert (
        "adds that folder and your npm folder (%APPDATA%\\npm) to PATH"
        in (gemini["changes"])
    )
    assert gemini["steps"][1].endswith(
        "msiexec.exe /i <downloaded file> /passive /norestart"
    )


def test_jsdom_progress_and_failure_panel(agent_installs) -> None:
    runs = [r for r in agent_installs["requests"] if "/run" in r["url"]]
    assert runs == [
        {
            "url": "/admin/api/agent-installs/gemini_cli/run?confirm=install-gemini_cli",
            "method": "POST",
            "body": {"method": "npm", "plan_token": "b" * 64, "dry_run": False},
        }
    ]

    progress = agent_installs["progress"]
    assert progress["role"] == "status"
    assert progress["log"] == (
        "Node.js is missing: installing it first.\n"
        "download https://nodejs.org/dist/index.json"
    )
    assert progress["cancel"] == "Cancel install"

    failure = agent_installs["failure"]
    assert failure["message"] == "npm.cmd exited 243."
    assert failure["facts"] == [
        "What went wrong: Permission: the system refused a write "
        "(EACCES / EPERM / access denied)",
        "Exit code: 243",
        "Log: C:\\Users\\u\\.mcc\\logs\\agent-installs\\"
        "gemini_cli-20261019-010203-abc123.log",
    ]
    assert "npm ERR! code EACCES" in failure["tail"]
    assert failure["command"] == "npm install -g @google/gemini-cli"
    assert failure["copy"] == "npm install -g @google/gemini-cli"
    assert (
        failure["link"] == "https://github.com/google-gemini/gemini-cli#-installation"
    )
    assert "Nothing is retried on its own." in failure["notes"]
    assert any(note.startswith("The command that failed:") for note in failure["notes"])
    # The card is still not installed, so Install is offered again.
    assert agent_installs["geminiAfter"]["button"] == "Install"
    assert agent_installs["polls"] == 2


# --------------------------------------------------------------------------- #
# The prompt-cache lifetime on the dashboard (7.104.0)
#
# One run of admin_jsdom_prompt_cache.mjs (MCC_JSDOM_SCENARIO=prompt_cache) on
# the REAL manifest's Reasoning section and the real declared lifetime table:
# Model Config's "Prompt cache lifetime" subheading, a Save, and the Models
# page's read-only row for a declared provider, a learned refusal and an
# undeclared provider.
# --------------------------------------------------------------------------- #

PROMPT_CACHE_KEYS = [
    "PROMPT_CACHE_TTL",
    "PROMPT_CACHE_TTL_MYTHOS",
    "PROMPT_CACHE_TTL_FABLE",
    "PROMPT_CACHE_TTL_OPUS",
    "PROMPT_CACHE_TTL_SONNET",
    "PROMPT_CACHE_TTL_HAIKU",
]


def _served_field(key: str) -> dict:
    """One manifest field as ``/admin/api/config`` serves it, at its default."""

    field = FIELD_BY_KEY[key]
    return {
        "key": field.key,
        "label": field.label,
        "section": field.section_id,
        "provider": None,
        "type": field.field_type,
        "value": field.default,
        "effective": field.default,
        "default": field.default,
        "set": False,
        "configured": bool(field.default),
        "source": "default",
        "locked": False,
        "secret": field.secret,
        "advanced": field.advanced,
        "restart_required": field.restart_required,
        "session_sensitive": field.session_sensitive,
        "options": [
            {"value": option.value, "label": option.label}
            if isinstance(option, ConfigOptionSpec)
            else {"value": option, "label": option}
            for option in field.options
        ],
        "description": field.description,
        "minimum": field.minimum,
        "maximum": field.maximum,
        "range_hint": field.range_hint,
    }


def _prompt_cache_table() -> dict:
    """The server's own table, for the routes and gateways the scenario draws.

    ``settings`` is the one part a real ``Settings`` object would supply: the
    stored words of the six Model Config lifetimes (Opus at 30 minutes, every
    other tier inheriting the global "from client").
    """

    configured = [
        ConfiguredChatModelRef(
            "anthropic_oauth/claude-haiku-5-5",
            "anthropic_oauth",
            "claude-haiku-5-5",
            ("MODEL_OPUS",),
        ),
        ConfiguredChatModelRef(
            "anthropic_oauth/claude-opus-5-5",
            "anthropic_oauth",
            "claude-opus-5-5",
            ("MODEL_HAIKU", "MODEL_FALLBACKS"),
        ),
    ]
    page = {
        "providers": [
            {
                "provider_id": "commandcode",
                "models": [
                    {"model_ref": f"commandcode/{model_id}", "model_id": model_id}
                    for model_id in (
                        "claude-opus-5-5",
                        "claude-sonnet-5-5",
                        "deepseek/deepseek-v4-flash",
                    )
                ],
            },
            {
                "provider_id": "opencode",
                "models": [
                    {
                        "model_ref": "opencode/claude-fable-5-1",
                        "model_id": "claude-fable-5-1",
                        "capabilities": {"response_surface": {"value": "messages"}},
                    }
                ],
            },
        ]
    }
    table = prompt_cache_lifetimes_payload(
        None, configured, prompt_cache_gateway_models(page)
    )
    table["settings"] = {
        "PROMPT_CACHE_TTL": "client",
        "PROMPT_CACHE_TTL_MYTHOS": "inherit",
        "PROMPT_CACHE_TTL_FABLE": "inherit",
        "PROMPT_CACHE_TTL_OPUS": "30",
        "PROMPT_CACHE_TTL_SONNET": "inherit",
        "PROMPT_CACHE_TTL_HAIKU": "inherit",
    }
    # 7.111.0: the 1-hour write rates the server lists beside the table, in
    # its own shape (``write_1h_price_statements``): two sources for one
    # model, none for another; every other model is not listed at all.
    table["write_1h_prices"] = {
        "anthropic_oauth/claude-sonnet-5-5": [
            {
                "usd_per_million": 6.0,
                "source": "litellm",
                "label": "LiteLLM, bare (claude-sonnet-5-5)",
            },
            {
                "usd_per_million": 4.0,
                "source": "openrouter_live",
                "label": "OpenRouter live, bare model (anthropic/claude-sonnet-5-5)",
            },
        ],
        "anthropic_oauth/claude-opus-5-5": [],
    }
    table["write_1h_note"] = PROMPT_CACHE_WRITE_1H_NOTE
    return table


@pytest.fixture(scope="module")
def prompt_cache(tmp_path_factory: pytest.TempPathFactory) -> dict:
    section = next(spec for spec in SECTIONS if spec.section_id == "reasoning")
    fixture = {
        "section": {
            "id": section.section_id,
            "label": section.label,
            "description": section.description,
            "advanced": section.advanced,
        },
        "fields": [
            _served_field(field.key)
            for field in FIELDS
            if field.section_id == "reasoning"
        ],
        "prompt_cache_lifetimes": _prompt_cache_table(),
    }
    path = tmp_path_factory.mktemp("prompt_cache") / "fixture.json"
    path.write_text(json.dumps(fixture), encoding="utf-8")
    return _run(
        MCC_JSDOM_SCENARIO="prompt_cache", MCC_JSDOM_PROMPT_CACHE_FIXTURE=str(path)
    )


def test_jsdom_the_prompt_cache_scenario_runs_clean(prompt_cache) -> None:
    assert prompt_cache["fatal"] is None
    assert prompt_cache["scenarioFatal"] is None
    assert prompt_cache["scriptErrors"] == []
    assert prompt_cache["consoleErrors"] == []


def test_jsdom_the_lifetimes_sit_under_their_own_subheading_in_reasoning(
    prompt_cache,
) -> None:
    section = prompt_cache["section"]
    assert section["heading"] == "Reasoning"
    # The thinking levels are drawn first, exactly the fields they always were.
    assert section["firstGridKeys"] == [
        "REASONING_POLICY",
        "REASONING_MYTHOS",
        "REASONING_FABLE",
        "REASONING_OPUS",
        "REASONING_SONNET",
        "REASONING_HAIKU",
    ]
    assert section["subInsideSection"] is True
    sub = prompt_cache["subsection"]
    assert sub["heading"] == "Prompt cache lifetime"
    assert "5 min or 1 h" in sub["lead"]
    assert "From client changes nothing" in sub["lead"]
    # Beside the thinking levels: the same tiers, in the same order.
    assert sub["keys"] == PROMPT_CACHE_KEYS


def test_jsdom_each_lifetime_defaults_to_choosing_nothing(prompt_cache) -> None:
    fields = {field["key"]: field for field in prompt_cache["subsection"]["fields"]}
    # Every select leads with the page's own "Default (...)" entry.
    assert fields["PROMPT_CACHE_TTL"]["value"] == "client"
    assert fields["PROMPT_CACHE_TTL"]["optionCount"] == 62
    assert fields["PROMPT_CACHE_TTL"]["firstOptions"][:3] == [
        {"value": "", "text": "Default (From client)"},
        {"value": "client", "text": "From client"},
        {"value": "1", "text": "1 min"},
    ]
    assert fields["PROMPT_CACHE_TTL"]["lastOption"] == "60 min"
    for key in PROMPT_CACHE_KEYS[1:]:
        assert fields[key]["value"] == "inherit", key
        assert fields[key]["optionCount"] == 63, key
        assert fields[key]["firstOptions"] == [
            {"value": "", "text": "Default (Inherit)"},
            {"value": "inherit", "text": "Inherit"},
            {"value": "client", "text": "From client"},
            {"value": "1", "text": "1 min"},
        ], key
        assert fields[key]["lastOption"] == "60 min", key
    assert fields["PROMPT_CACHE_TTL_OPUS"]["label"].startswith(
        "Opus Prompt Cache Lifetime"
    )


def test_jsdom_a_save_posts_exactly_the_two_lifetimes_edited(prompt_cache) -> None:
    assert prompt_cache["dirtyBefore"] == "No changes"
    assert prompt_cache["edited"] == [True, True]
    assert prompt_cache["dirtyAfter"] == "2 unsaved changes"
    assert prompt_cache["applyEnabled"] is True
    assert prompt_cache["applied"] == [
        {"values": {"PROMPT_CACHE_TTL": "30", "PROMPT_CACHE_TTL_OPUS": "3"}}
    ]


def test_jsdom_a_declared_host_shows_its_lifetimes_on_the_overridable_row(
    prompt_cache,
) -> None:
    """7.104.0's read-only row is now the 14th overridable key's row (7.105.0)."""

    entry = prompt_cache["models"]["plain"]
    plain = entry["row"]
    assert entry["oldRow"] is False
    assert plain["label"] == "prompt cache lifetime"
    assert plain["fileKey"] == "prompt_cache_ttl"
    assert plain["overridable"] is True
    assert plain["readonly"] is False
    assert plain["badge"] == "declared for the Anthropic Messages family"
    assert "models-source-declared" in plain["badgeClass"]
    assert plain["group"] == "request"
    assert plain["lastInGroup"] is True
    # Everything the read-only row said is still said, on the source column.
    assert any(
        "This host takes: 5 min (writes 1.25x), 1 h (writes 2x)." in note
        and "shortest that is at least" in note
        for note in plain["notes"]
    )


def test_jsdom_the_provider_row_decides_and_the_tier_is_the_default(prompt_cache):
    plain = prompt_cache["models"]["plain"]["row"]
    # The provider row (45 min) decides; the Opus route's 30 min is the default.
    assert plain["value"] == "1 h (asked 45 min)"
    assert plain["forced"] is True
    assert plain["mode"] == "inherit"
    assert plain["boxDisabled"] is True
    assert any(
        note.startswith("From this provider's override row: asked 45 min → sent 1 h.")
        for note in plain["notes"]
    )
    assert plain["defaults"][0] == "default 30 min — PROMPT_CACHE_TTL_OPUS → sent 1 h"
    assert "chosen by the model name the client sends" in plain["defaults"][1]
    assert plain["describedBy"].startswith("default 30 min — PROMPT_CACHE_TTL_OPUS")


def test_jsdom_a_learned_refusal_is_named_with_its_forget(prompt_cache) -> None:
    refused = prompt_cache["models"]["refused"]["row"]
    # Model row 10 min; the host refused 1 h, so the next one it takes: 5 min.
    assert refused["value"] == "5 min (asked 10 min)"
    assert refused["mode"] == "value"
    assert refused["boxValue"] == "10"
    assert any(
        "This host takes: 5 min (writes 1.25x), 1 h — refused by this host." in note
        for note in refused["notes"]
    )
    assert "refused 1h for this model — learned from its own 400" in refused["notes"]
    assert refused["forgets"] == ["Forget"]
    # Two routes name it: Haiku (inheriting the global) and the default route.
    assert refused["defaults"][:2] == [
        "default From client — PROMPT_CACHE_TTL_HAIKU inherits PROMPT_CACHE_TTL; "
        "the client's own markers go as sent",
        "default From client — PROMPT_CACHE_TTL; the client's own markers go as sent",
    ]


def test_jsdom_force_unset_on_a_model_row_is_from_client(prompt_cache) -> None:
    unset = prompt_cache["models"]["unset"]["row"]
    assert unset["value"] == "From client"
    assert unset["mode"] == "unset"
    assert unset["forced"] is True
    assert any(
        note.startswith("Forced unset on this model's override row")
        for note in unset["notes"]
    )
    assert unset["defaults"][0] == (
        "default From client — PROMPT_CACHE_TTL; the client's own markers go as sent"
    )


def test_jsdom_the_lifetime_row_names_the_one_hour_write_price(prompt_cache) -> None:
    """7.111.0: the 1-hour write price and its source, last under the control."""
    unset = prompt_cache["models"]["unset"]["row"]
    assert unset["defaults"][-1] == (
        "1 h write price: $6.0000 / 1M tokens — from LiteLLM, bare "
        "(claude-sonnet-5-5); also stated: $4.0000 by OpenRouter live, bare model "
        "(anthropic/claude-sonnet-5-5)."
    )
    refused = prompt_cache["models"]["refused"]["row"]
    assert refused["defaults"][-1].startswith(
        "1 h write price: no source states it — a request that writes 1-hour "
        "cache entries here is stored as not priced"
    )
    # A model the server did not list carries no such line.
    for name in ("plain", "undeclared"):
        row = prompt_cache["models"][name]["row"]
        assert not any(line.startswith("1 h write price") for line in row["defaults"])


def test_jsdom_an_undeclared_host_says_lifetime_control_is_not_known(
    prompt_cache,
) -> None:
    row = prompt_cache["models"]["undeclared"]["row"]
    assert row["value"] == "From client"
    assert row["badge"] == "not declared"
    assert row["overridable"] is True
    assert any(
        "lifetime control not known for this host" in note
        and "changes nothing on its requests" in note
        for note in row["notes"]
    )
    assert row["defaults"] == [
        "default From client — no lifetime control known for this host"
    ]


def test_jsdom_a_gateway_says_lifetimes_unknown_and_takes_one_only_from_a_row(
    prompt_cache,
) -> None:
    models = prompt_cache["models"]
    plain, with_row = models["gatewayPlain"]["row"], models["gatewayRow"]["row"]
    for row in (plain, with_row):
        assert row["badge"] == "lifetimes unknown — sent only when set on this row"
        assert row["defaults"] == [
            "default From client — Model Config's lifetimes never reach this host; "
            "only this row or its provider's row sends one"
        ]
        assert any(
            "This host takes: 5 min, 1 h." in note
            and "Anthropic's own spelling of the nearest Anthropic lifetime" in note
            and "the first refusal stops lifetimes for the whole host" in note
            for note in row["notes"]
        )
    # Model Config asks 30 min on the Opus route; the gateway still gets nothing.
    assert plain["value"] == "From client"
    assert with_row["value"] == "1 h (asked 20 min)"
    # Command Code's open models are not on the Messages protocol at all.
    assert models["gatewayOpen"]["row"]["badge"] == "not declared"


def test_jsdom_a_host_wide_refusal_stops_a_gateway_and_forget_names_the_host(
    prompt_cache,
) -> None:
    row = prompt_cache["models"]["gatewayRefused"]["row"]
    assert row["value"] == "nothing sent (asked 20 min)"
    assert "refused 1h for this whole host — learned from its own 400" in row["notes"]
    assert row["forgets"] == ["Forget"]
    assert prompt_cache["forgetPosted"] == [
        {
            "path": "/admin/api/model-admin/learned/forget",
            "body": {
                "providerId": "opencode",
                "modelId": "*",
                "factKind": "prompt_cache_lifetime_refused",
            },
        }
    ]


def test_jsdom_the_rule_on_the_page_is_the_servers_rule(prompt_cache) -> None:
    lifetimes = PROMPT_CACHE_LIFETIME_FAMILIES[ANTHROPIC_MESSAGES_LIFETIMES].lifetimes
    for minutes, shown in prompt_cache["edges"].items():
        sent = adapt_prompt_cache_minutes(int(minutes), lifetimes)
        assert sent is not None
        assert shown == f"{sent.label} (asked {minutes} min)", minutes


def test_jsdom_the_provider_editor_carries_the_same_row(prompt_cache) -> None:
    providers = prompt_cache["providers"]
    declared = providers["declared"]
    assert declared["value"] == "45 min"
    assert declared["badge"] == "operator override"
    assert declared["mode"] == "value"
    assert declared["modeId"] == "pref-provider-anthropic_oauth-prompt_cache_ttl-mode"
    assert declared["defaults"] == [
        "default per model — open a model to see its default",
        "this host: declared for the Anthropic Messages family",
    ]
    assert (
        "Force unset is From client for the whole provider."
        in (declared["controlNotes"][0])
    )
    assert providers["alpha"]["value"] == "not set"
    assert providers["alpha"]["defaults"][1] == (
        "this host: lifetime control not known for this host; a value here "
        "changes nothing"
    )
    for gateway in ("commandcode", "opencode"):
        assert providers[gateway]["defaults"][1] == (
            "its Anthropic Messages models: lifetimes unknown — sent only when "
            "set on this row"
        )


def test_jsdom_claude_on_openrouter_is_declared_by_its_upstream(prompt_cache) -> None:
    """7.106.0: OpenRouter's Claude models, alias ids too, take 5 min or 1 h."""

    hosts = prompt_cache["hosts"]
    claude, alias = hosts["orClaude"]["row"], hosts["orAlias"]["row"]
    for row in (claude, alias):
        assert row["badge"] == "declared for the OpenRouter, Claude upstream family"
        assert any(
            "This host takes: 5 min (writes 1.25x), 1 h (writes 2x)." in note
            for note in row["notes"]
        )
        assert row["defaults"][0] == (
            "default From client — PROMPT_CACHE_TTL; the client's own markers go as sent"
        )
    assert claude["value"] == "1 h (asked 30 min)"
    assert alias["value"] == "5 min (asked 3 min)"


def test_jsdom_azure_shows_its_retention_words(prompt_cache) -> None:
    hosts = prompt_cache["hosts"]
    azure, small = hosts["azure"]["row"], hosts["azureSmall"]["row"]
    for row in (azure, small):
        assert row["badge"] == "declared for the OpenAI prompt cache retention family"
        assert any(
            "This host takes: in memory, about 5-10 min, 24 h." in note
            for note in row["notes"]
        )
    assert azure["value"] == "24 h (asked 30 min)"
    assert small["value"] == "in memory, about 5-10 min (asked 4 min)"


def test_jsdom_an_automatic_host_says_it_caches_automatically(prompt_cache) -> None:
    """The user's Q11: "caches automatically; no lifetime control", never unknown."""

    hosts = prompt_cache["hosts"]
    for name in ("orOpen", "gemini", "geminiRow"):
        row = hosts[name]["row"]
        assert row["badge"] == "caches automatically; no lifetime control", name
        assert row["defaults"] == [
            "default From client — this host caches automatically; no lifetime control"
        ], name
        assert any(
            "caches prompts on its own and takes no cache lifetime" in note
            for note in row["notes"]
        ), name
    assert hosts["gemini"]["row"]["value"] == "From client"
    assert hosts["orOpen"]["row"]["value"] == "From client (asked 30 min)"
    assert any(
        "but this host caches automatically and takes no lifetime" in note
        for note in hosts["geminiRow"]["row"]["notes"]
    )


def test_jsdom_the_new_hosts_provider_rows_name_their_family(prompt_cache) -> None:
    providers = prompt_cache["hostProviders"]
    assert providers["open_router"]["defaults"][1] == (
        "its anthropic/… models: declared for the OpenRouter, Claude upstream "
        "family; every other model: caches automatically; no lifetime control"
    )
    assert providers["azure_openai"]["defaults"][1] == (
        "this host: declared for the OpenAI prompt cache retention family"
    )
    assert providers["gemini"]["defaults"][1] == (
        "this host: caches automatically; no lifetime control; a value here "
        "changes nothing"
    )


def test_jsdom_the_override_chip_counts_the_lifetime_key(prompt_cache) -> None:
    """A provider row's lifetime alone is one key set on its models (X2 7.101.0)."""

    models = prompt_cache["models"]
    assert models["plain"]["chip"] == "1 override(s) active"
    assert models["gatewayRefused"]["chip"] == "1 override(s) active"
    assert models["gatewayRow"]["chip"] == "1 override(s) active"
    assert models["gatewayPlain"]["chip"] is None


def test_jsdom_a_lifetime_save_is_validated_and_sent_last(prompt_cache) -> None:
    saves = prompt_cache["saves"]
    for refused in ("zero", "tooMany", "fraction"):
        assert saves[refused]["posted"] == [], refused
        assert saves[refused]["status"] == (
            "prompt_cache_ttl must be whole minutes from 1 to 60."
        )
        assert saves[refused]["statusClass"] == "models-status error"
    for name, value in (("twenty", 20), ("unset", None)):
        posted = saves[name]["posted"]
        assert saves[name]["status"] == "Saved"
        assert len(posted) == 1
        updates = posted[0]["updates"]
        # The harness payload names its preferences in this order; the body
        # keeps the server's order and puts the lifetime after everything.
        assert list(updates) == [
            "reasoning_preference",
            "max_output_tokens",
            "context_length",
            "temperature",
            "prompt_cache_ttl",
        ]
        assert updates["prompt_cache_ttl"] == value


# --------------------------------------------------------------------------- #
# The 14th key against the 7.104.0 page (7.105.0)
#
# tests/fixtures/models_fields_7_104_0.json is the models_fields scenario run
# once against the v7.104.0 admin_static (2ab06ec9). Replayed here twice: on a
# 7.104.0-shaped payload the 7.105.0 page draws and sends exactly that; on the
# 7.105.0 payload -- the new key's kind, its control on every row, the server's
# table -- it draws every other row identically, keeps the media block and the
# MCP notes byte for byte, and its POST bodies differ only by the new key,
# sent last as "inherit" (which writes nothing: test_prompt_cache_ttl_admin).
# --------------------------------------------------------------------------- #

MODELS_FIELDS_7_104_0 = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "models_fields_7_104_0.json"
    ).read_text(encoding="utf-8")
)
_VOLATILE_CAPTURE_KEYS = {"harnessWallMs", "scriptErrors", "consoleErrors", "_captured"}


def _capture(payload: dict) -> dict:
    return {k: v for k, v in payload.items() if k not in _VOLATILE_CAPTURE_KEYS}


def _without_the_lifetime(node: object) -> object:
    """Every trace of the 14th key's row and of its key in a POST body."""

    if isinstance(node, dict):
        if node.get("field") == "prompt_cache_ttl":
            return None
        out: dict = {}
        for key, value in node.items():
            if key == "fields" and isinstance(value, list):
                out[key] = [field for field in value if field != "prompt_cache_ttl"]
            elif key == "body" and isinstance(value, str) and value.startswith("{"):
                body = json.loads(value)
                if isinstance(body.get("updates"), dict):
                    assert list(body["updates"])[-1] == "prompt_cache_ttl"
                    assert body["updates"].pop("prompt_cache_ttl") == "inherit"
                out[key] = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
            else:
                out[key] = _without_the_lifetime(value)
        return out
    if isinstance(node, list):
        kept = (_without_the_lifetime(item) for item in node)
        return [item for item in kept if item is not None]
    return node


@pytest.fixture(scope="module")
def models_fields_with_lifetime(tmp_path_factory: pytest.TempPathFactory) -> dict:
    path = tmp_path_factory.mktemp("models_fields_lifetime") / "table.json"
    path.write_text(json.dumps(prompt_cache_lifetimes_payload()), encoding="utf-8")
    return _run(
        MCC_JSDOM_SCENARIO="models_fields",
        MCC_JSDOM_MODELS_FIELDS_PROMPT_CACHE=str(path),
    )


def test_jsdom_a_7_104_0_payload_draws_and_sends_what_7_104_0_did(
    models_fields,
) -> None:
    assert _capture(models_fields) == _capture(MODELS_FIELDS_7_104_0)


def test_jsdom_the_14th_key_moves_no_other_row_body_or_block(
    models_fields_with_lifetime,
) -> None:
    run = models_fields_with_lifetime
    assert run["scriptErrors"] == []
    assert run["consoleErrors"] == []
    # The new row is there on every opened model and on the provider table...
    for ref in (REF_A, REF_B, REF_C):
        fields = [row["field"] for row in run["models"][ref]["table"]["rows"]]
        assert "prompt_cache_ttl" in fields, ref
    provider_fields = [row["field"] for row in run["provider"]["table"]["rows"]]
    assert "prompt_cache_ttl" in provider_fields
    # ...and with it taken out, everything is the 7.104.0 capture, byte for byte:
    # every row, every default line, every POST body, the media block's hash and
    # the MCP notes.
    assert _without_the_lifetime(_capture(run)) == _capture(MODELS_FIELDS_7_104_0)
    assert run["mediaSection"] == MODELS_FIELDS_7_104_0["mediaSection"]
    assert run["mcpNotes"] == MODELS_FIELDS_7_104_0["mcpNotes"]


# --------------------------------------------------------------------------
# 7.110.0: the InferHub page. One run of admin_jsdom_marketplaces.mjs
# (MCC_JSDOM_SCENARIO=marketplaces) on payloads the real page builders
# (api/marketplace_page.py) produce from in-memory stores -- no file, no
# network. The snapshot reader the ceiling computation asks is pointed at the
# same in-memory snapshot for the length of the build.

MKT_KEY = "sk-airo-" + "j" * 24
MKT_OPUS = "cc/claude-opus-4-6"
MKT_ASTRA = "cx/gpt-6-astra"
MKT_THINKING = "ag/claude-opus-4-6-thinking"
MKT_SNAPSHOT: dict[str, Any] = {
    "fetched_at": "2026-10-24T01:07:35Z",
    "sources": {
        "listing": "ok",
        "market": "ok",
        "status": "ok",
        "pricing_cache": "ok",
        "pricing_config": "ok",
        "catalog": "ok",
        "combos": "ok",
    },
    "platform": {"max_ask_pct_of_official": 0.5},
    "notes": {
        "cc": "A one-line Claude Code client header is added. <b>bold?</b>",
        "cx": "Your system prompt is sent as a developer message.",
    },
    "combos": [
        {"id": "combo/fast", "members": [MKT_OPUS, MKT_ASTRA], "selection": "fastest"}
    ],
    "models": {
        MKT_OPUS: {
            "official": [5.0, 25.0],
            "levels_in": [[0.75, 38], [1.0, 10], [2.5, 3]],
            "levels_out": [[3.75, 38], [5.0, 10], [12.5, 3]],
            "min": [0.75, 3.75],
            "max": [2.5, 12.5],
            "last": 1.9,
            "perf": {"success_pct": 99.1, "ttft_ms": 812.0, "tps": 61.5},
            "cache_hit": 0.62,
        },
        MKT_ASTRA: {
            "official": [1.25, 10.0],
            "levels_in": [[0.3, 10], [0.6, 4]],
            "levels_out": [[2.4, 10], [4.8, 4]],
            "min": [0.3, 2.4],
        },
        MKT_THINKING: {
            "official": [5.0, 25.0],
            "levels_in": [[0.005, 1052], [2.5, 3]],
            "levels_out": [[0.025, 1052], [12.5, 3]],
        },
    },
}
MKT_PROFILES = {
    "cap20": {"kind": "pct_of_official", "percent": 20},
    "tight": {"kind": "max_price", "max_input_usd": 0.5, "max_output_usd": 2.5},
    "follow": {"kind": "cheapest_level_now"},
}
MKT_ACCOUNT = {
    "state": "read",
    "reads": {
        "usage": {
            "status": "ok",
            "body": {
                "currency": "USDC",
                "balance": {"amount_usdc": "1.000000"},
                "window": {"kind": "day", "spend_usdc": "0.012000", "requests": 3},
                "all_time": {"spend_usdc": "0.500000", "requests": 40},
            },
        },
        "keys": {"status": "ok"},
        "budgets": {"status": "http_403", "code": "insufficient_key_scope"},
        "budget_aliases": {"status": "http_403", "code": "insufficient_key_scope"},
        "budget_default": {"status": "http_403", "code": "insufficient_key_scope"},
    },
    "key_kind": {"kind": "inference", "how": "key_list"},
}


def _mkt_profiles(documents: dict[str, Any]) -> MarketplaceProfiles:
    parsed = {}
    for name, document in documents.items():
        profile, problem = parse_profile(name, document)
        assert profile is not None, problem
        parsed[name] = profile
    return MarketplaceProfiles(profiles=parsed)


def _mkt_recent() -> list[dict[str, Any]]:
    ceiling = {
        "profile": "cap20",
        "kind": "pct_of_official",
        "sent": {"x-max-input-price": "1", "x-max-output-price": "5"},
    }
    headers = {
        "x-inferhub-model": MKT_OPUS,
        "x-inferhub-publisher": "6b652a8e5328",
        "x-inferhub-request-id": "08a9befdcacfb82b",
        "x-inferhub-attempt": "1",
    }

    def request(request_id: str, ts: float) -> dict[str, Any]:
        return {
            "id": request_id,
            "ts_epoch": ts,
            "ts_iso": "2026-10-24T01:20:00Z",
            "resolved_model": f"inferhub/{MKT_OPUS}",
            "status": "success",
            "tokens_in": 1000,
            "tokens_out": 200,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }

    def attempt(request_id: str, marketplace: dict[str, Any]) -> dict[str, Any]:
        return {
            "request_id": request_id,
            # The request log counts attempts from 0: the primary is 0.
            "attempt": 0,
            "attempt_provider": "inferhub",
            "outcome": "succeeded",
            "_attempt_params": {"marketplace": marketplace},
        }

    return recent_rows(
        "inferhub",
        [
            request("req-over", 1792800300.0),
            request("req-silent", 1792800200.0),
            request("req-bare", 1792800100.0),
        ],
        [
            attempt(
                "req-over",
                {"ceiling": ceiling, "headers": headers, "charged_usd": 0.0031},
            ),
            attempt("req-silent", {"ceiling": ceiling}),
            attempt("req-bare", {"headers": {"x-inferhub-model": MKT_OPUS}}),
        ],
        "_attempt_params",
    )


def _marketplaces_fixture() -> dict[str, Any]:
    snapshot = market_snapshot_from_payload(MKT_SNAPSHOT)
    profiles = _mkt_profiles(MKT_PROFILES)
    overrides = ModelParameterOverrides(
        providers={"inferhub": {"marketplace_profile": "cap20"}},
        models={
            f"inferhub/{MKT_ASTRA}": {"marketplace_profile": "tight"},
            f"inferhub/{MKT_THINKING}": {"marketplace_profile": None},
        },
    )
    catalogue = {"inferhub": frozenset({MKT_OPUS, MKT_ASTRA, MKT_THINKING})}
    store_raw = json.dumps(profiles.as_document()).encode("utf-8")
    keyed = cast(
        Settings,
        SimpleNamespace(inferhub_api_key=MKT_KEY, marketplace_public_market=True),
    )
    bare = cast(
        Settings, SimpleNamespace(inferhub_api_key="", marketplace_public_market=True)
    )
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            application_marketplace,
            "current_market_snapshot",
            lambda *_args, **_kwargs: snapshot,
        )
        reset_account_memory()
        reset_market_memory()
        remember_account("inferhub", MKT_ACCOUNT)

        def page(settings: Settings, documents: dict[str, Any]) -> dict[str, Any]:
            chosen = _mkt_profiles(documents)
            return page_payload(
                settings=settings,
                profiles=chosen,
                store_raw=json.dumps(chosen.as_document()).encode("utf-8"),
                overrides=overrides,
                catalogue=catalogue,
                snapshots={"inferhub_bid_headers": snapshot},
            )

        keyed_page = page_payload(
            settings=keyed,
            profiles=profiles,
            store_raw=store_raw,
            overrides=overrides,
            catalogue=catalogue,
            snapshots={"inferhub_bid_headers": snapshot},
        )
        account = keyed_page["providers"][0]["account"]
        offers = {
            model: offers_payload(
                provider_id="inferhub",
                model_id=model,
                profiles=profiles,
                overrides=overrides,
                snapshot=snapshot,
                account=account,
            )
            for model in (MKT_OPUS, MKT_ASTRA, MKT_THINKING)
        }
        rows = _mkt_recent()
        logs = [
            {
                "id": "08a9befdcacfb82b",
                "ts": "2026-10-24T01:25:00Z",
                "model": MKT_OPUS,
                "completion_tokens": 200,
                "ask_input_per_mtok": "0.750000",
                "ask_output_per_mtok": "3.750000",
                "cost_consumer_usdc": "0.001500",
            }
        ]
        saved = page(
            keyed,
            {**MKT_PROFILES, "opus-cap": {"kind": "pct_of_official", "percent": 10}},
        )
        reset_account_memory()
        reset_market_memory()
        nokey_page = page(bare, MKT_PROFILES)
        reset_account_memory()
        reset_market_memory()
    views = keyed_page["providers"]
    return {
        "models": {"cc": MKT_OPUS, "cx": MKT_ASTRA, "ag": MKT_THINKING},
        "keyed": {
            "summary": {
                "views": [
                    {
                        k: v
                        for k, v in views[0].items()
                        if k
                        in (
                            "provider_id",
                            "dialect_id",
                            "label",
                            "display_name",
                            "has_key",
                            "account_reads",
                        )
                    }
                ]
            },
            "page": keyed_page,
            "saved": saved,
            "offers": offers,
            "recent": {"enabled": True, "rows": rows, "limit": 20},
            "charged": {
                "state": "read",
                "words": "Read 1 usage-log rows (1 pages); InferHub keeps them 7 days.",
                "rows": match_charged(rows, logs),
            },
        },
        "nokey": {
            "summary": {
                "views": [
                    {
                        **{
                            k: v
                            for k, v in nokey_page["providers"][0].items()
                            if k
                            in (
                                "provider_id",
                                "dialect_id",
                                "label",
                                "display_name",
                                "has_key",
                                "account_reads",
                            )
                        }
                    }
                ]
            },
            "page": nokey_page,
            "recent": {"enabled": True, "rows": [], "limit": 20},
        },
    }


@pytest.fixture(scope="module")
def marketplaces(tmp_path_factory: pytest.TempPathFactory) -> dict:
    path = tmp_path_factory.mktemp("marketplaces") / "fixture.json"
    path.write_text(json.dumps(_marketplaces_fixture()), encoding="utf-8")
    return _run(
        MCC_JSDOM_SCENARIO="marketplaces", MCC_JSDOM_MARKETPLACES_FIXTURE=str(path)
    )


def test_jsdom_the_marketplaces_scenario_runs_clean(marketplaces) -> None:
    assert marketplaces["fatal"] is None
    assert marketplaces["scenarioFatal"] is None
    assert marketplaces["scriptErrors"] == []
    assert marketplaces["consoleErrors"] == []


def test_jsdom_the_page_is_in_the_sidebar_only_while_a_key_is_set(marketplaces) -> None:
    assert marketplaces["nav"] == {"hidden": False, "label": "InferHub"}
    assert marketplaces["view"]["hidden"] is False
    assert marketplaces["view"]["title"] == "InferHub"
    assert marketplaces["noKeyNav"] == {"hidden": True}
    # Opening the page is what reads the account (and nothing on its own).
    assert "/admin/api/marketplaces?account=true" in marketplaces["gets"]
    assert "/admin/api/marketplaces?summary=true" in marketplaces["gets"]


def test_jsdom_the_blocks_render_in_the_specified_order(marketplaces) -> None:
    assert marketplaces["view"]["blockOrder"] == [
        "marketplaceHonesty",
        "marketplaceAccount",
        "marketplaceProfiles",
        "marketplaceAssignments",
        "marketplaceExplorer",
        "marketplaceRecent",
        "marketplaceSnapshot",
    ]


def test_jsdom_route_notes_are_quoted_as_text_never_markup(marketplaces) -> None:
    notes = marketplaces["routeNotes"]
    assert notes["markupInside"] == 0
    assert notes["quotes"] == [
        "cc/ A one-line Claude Code client header is added. <b>bold?</b>",
        "cx/ Your system prompt is sent as a developer message.",
    ]
    honesty = marketplaces["honesty"]
    assert "run by an individual" in honesty
    assert "regardless of price" in honesty
    assert "x-max-input-price" in honesty and "x-max-output-price" in honesty
    assert "current ask" in honesty
    assert "Not verified on a real account" in honesty


def test_jsdom_the_account_says_unverified_and_not_readable_never_zero(
    marketplaces,
) -> None:
    facts = marketplaces["account"]["facts"]
    assert (
        facts["Balance"] == "1.000000 USDC UNVERIFIED — not verified on a real account"
    )
    assert facts["Spend (day)"] == "0.012000 USDC 3 requests"
    assert facts["Key kind"] == "inference (inference + management) from your key list"
    assert facts["InferHub budgets"].startswith("not readable with this key (403")
    assert facts["Alias groups"].startswith("not readable with this key (403")
    assert facts["Combos"] == "combo/fast (fastest)"
    no_key = marketplaces["noKey"]["account"]
    assert no_key["text"].startswith("No InferHub key is set: nothing is read.")
    assert no_key["facts"] == {}
    assert not re.search(r"\b0(?:\.0+)? USDC", no_key["text"])


def test_jsdom_profiles_show_would_match_now_and_follow_the_market_warning(
    marketplaces,
) -> None:
    profiles = {profile["name"]: profile for profile in marketplaces["profiles"]}
    cap = profiles["cap20"]
    assert cap["rows"] == "Named on: provider inferhub"
    assert cap["counts"].startswith("Would match now: 1 match · 0 match nothing")
    assert cap["matches"] == [
        {
            "cls": "mkt-match mkt-match-matches",
            "text": "cc/claude-opus-4-6 $1 / $5 per Mtok: 48 of 51 accounts ask at or "
            "under it (cheapest $0.75, 38 accounts)",
        }
    ]
    assert profiles["tight"]["matches"][0]["text"].startswith(
        "cx/gpt-6-astra $0.5 / $2.5"
    )
    assert profiles["follow"]["follows"] == "follows the market — add a cap to be sure"
    assert profiles["follow"]["rows"] == "Not assigned to any row yet."


def test_jsdom_the_editor_validates_before_sending_and_saves_the_whole_store(
    marketplaces,
) -> None:
    assert marketplaces["editorOpened"]["hidden"] is False
    invalid = marketplaces["editorInvalid"]["problems"]
    assert any(problem.startswith("Name: 1-64 characters") for problem in invalid)
    assert "At most N % of official needs the percent." in invalid
    assert marketplaces["invalidSaveSent"] == 0
    assert marketplaces["editorFollows"]["follows"] == (
        "follows the market — add a cap to be sure"
    )
    assert marketplaces["editorFollows"]["fields"] == [
        "cap_input_usd",
        "cap_output_usd",
        "cap_percent",
        "exclude_upstreams",
        "max_usd_per_request",
    ]
    assert marketplaces["editorCapped"]["follows"] == ""
    assert marketplaces["editorValid"]["problems"] == []
    put = marketplaces["savedPut"]
    assert put["path"] == "/admin/api/marketplaces/profiles"
    assert put["body"]["profiles"] == {
        **MKT_PROFILES,
        "opus-cap": {"kind": "pct_of_official", "percent": 10},
    }
    assert put["body"]["drop_unusable"] is False
    assert put["body"]["confirm_removal"] is False
    assert marketplaces["editorAfterSave"]["hidden"] is True
    assert marketplaces["statusAfterSave"] == "Saved profile opus-cap."


def test_jsdom_assignments_post_the_15th_key_through_the_models_route(
    marketplaces,
) -> None:
    rows = {row["key"]: row for row in marketplaces["assignments"]}
    assert rows["inferhub"]["value"] == "cap20"
    assert rows[f"inferhub/{MKT_ASTRA}"]["value"] == "tight"
    assert rows[f"inferhub/{MKT_THINKING}"]["value"] == "__decides__"
    assert (
        rows[f"inferhub/{MKT_THINKING}"]["note"]
        == "Forced: InferHub decides — no ceiling."
    )
    assert marketplaces["assignPosts"] == [
        {
            "scope": "provider",
            "key": "inferhub",
            "updates": {"marketplace_profile": "cap20"},
        },
        {
            "scope": "provider",
            "key": "inferhub",
            "updates": {"marketplace_profile": "inherit"},
        },
        # The first model row in ref order is the forced-unset one; its
        # select already says "InferHub decides", and a Save sends null.
        {
            "scope": "model",
            "key": f"inferhub/{MKT_THINKING}",
            "updates": {"marketplace_profile": None},
        },
    ]


def test_jsdom_the_explorer_draws_the_ceiling_line_on_the_ladder(marketplaces) -> None:
    assert marketplaces["groups"] == ["ag", "cc", "cx"]
    assert marketplaces["searchedGroups"] == [["cx", 1]]
    cc = marketplaces["explorerCc"]
    assert cc["title"] == MKT_OPUS
    assert cc["who"] == "Ceiling: profile cap20 (from the provider row)."
    assert [entry["cls"] for entry in cc["ladder"]] == [
        "mkt-ladder-level",
        "mkt-ladder-level",
        "mkt-ladder-ceiling",
        "mkt-ladder-level mkt-ladder-over",
    ]
    assert cc["ladder"][2]["text"] == (
        "Your ceiling $1 / $5 — 48 of 51 accounts at or under it"
    )
    assert cc["ladder"][0]["width"] == "100%"
    assert cc["facts"]["Platform cap"] == "50 % of official = $2.5 / $12.5"
    assert cc["facts"]["Success"] == "99.1 % last 30 min, this model"
    assert cc["facts"]["In your combos"] == "combo/fast (fastest)"
    assert cc["facts"]["In alias groups"].startswith("not readable with this key")
    assert cc["note"].startswith("cc/ A one-line Claude Code client header")
    cx = marketplaces["explorerCx"]
    assert cx["who"] == "Ceiling: profile tight (from this model's row)."
    assert [entry["cls"] for entry in cx["ladder"]] == [
        "mkt-ladder-level",
        "mkt-ladder-ceiling",
        "mkt-ladder-level mkt-ladder-over",
    ]
    assert cx["facts"]["Dearest now"] == "—"
    assert cx["facts"]["Prompt-cache hit"] == "— 24 h, all traffic"


def test_jsdom_the_last_requests_draw_null_as_a_dash_never_zero(marketplaces) -> None:
    recent = marketplaces["recent"]
    assert recent["headers"] == [
        "Time",
        "Model",
        "X-Inferhub-Model",
        "X-Inferhub-Publisher",
        "Attempt",
        "Ceiling sent",
        "Charged",
        "vs ceiling",
        "Charged ask",
    ]
    rows = {row["id"]: row for row in recent["rows"]}
    over = rows["req-over"]["cells"]
    assert over[2:8] == [
        MKT_OPUS,
        "6b652a8e5328",
        "#1 (InferHub hop 1)",
        "$1 / $5 (cap20)",
        "$0.003100",
        "over the ceiling",
    ]
    assert rows["req-over"]["classes"][7] == "mkt-flag-over"
    silent = rows["req-silent"]["cells"]
    assert silent[6] == "—" and silent[7] == "—" and silent[3] == "—"
    assert rows["req-bare"]["cells"][5] == "none"
    assert all(
        "$0.000000" not in cell for row in recent["rows"] for cell in row["cells"]
    )


def test_jsdom_the_charged_ask_arrives_only_on_its_button(marketplaces) -> None:
    assert marketplaces["chargedBeforePress"] == 0
    assert marketplaces["chargedPosts"] == [
        {"request_ids": ["req-over", "req-silent", "req-bare"]}
    ]
    after = {
        row["id"]: row["cells"][8] for row in marketplaces["recentAfterCharged"]["rows"]
    }
    assert after["req-over"] == "$0.750000 / $3.750000 (by request id)"
    assert after["req-silent"] == "not in InferHub's log"
    assert marketplaces["recentAfterCharged"]["words"].startswith(
        "Read 1 usage-log rows"
    )


def test_jsdom_the_snapshot_block_names_each_source_and_the_setting(
    marketplaces,
) -> None:
    sources = {
        entry["name"]: entry["cells"] for entry in marketplaces["snapshot"]["sources"]
    }
    assert sources["listing"][1:] == [
        "your key",
        "your key's model list (price levels, official prices)",
        "read",
    ]
    assert sources["market"][1] == "public"
    assert marketplaces["snapshot"]["publicMarket"] is True
    assert "MARKETPLACE_PUBLIC_MARKET" in marketplaces["snapshot"]["text"]
    assert (
        "Platform cap on any ask: 50 % of official" in marketplaces["snapshot"]["text"]
    )


def test_jsdom_with_no_key_every_keyed_control_is_off(marketplaces) -> None:
    no_key = marketplaces["noKey"]
    assert no_key["warningHidden"] is True
    assert no_key["refreshDisabled"] is True
    assert no_key["chargedDisabled"] is True
    assert no_key["recent"]["text"].endswith(
        "No request has been served by InferHub yet."
    )


# --------------------------------------------------------------------------
# 7.113.0: the Web Search page's search chain, key health words and Reset, the
# Context7 card, and the Analytics tries ladder. One run of
# admin_jsdom_websearch_chain.mjs (MCC_JSDOM_SCENARIO=websearch_chain) on the
# payloads tests/support/websearch_page_fixture.py builds from this tree's own
# manifest. The chain-unset DOM is compared with the one the BASE tree
# (7.112.1, 0f0d7908) drew from the same fixture built by ITS manifest.

WS_BASELINE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "websearch_page_dom_7_112_1.json"
)
#: The ddgs drawer's four help texts, which 7.120.2 corrects (RESEARCH-DDG-
#: WEBSEARCH.md R5: what an empty region and safe search really do, which two
#: options also reach the keyless DuckDuckGo hop, which backend engines are
#: switched off). Corrected rather than added, so they are taken out of BOTH
#: sides -- this tree's page and the base tree's recording -- and every other
#: node is still compared; tests/websearch/test_ddgs_option_values.py pins
#: their text.
WS_CORRECTED_HELP = tuple(
    re.compile(rf'<div class="field-description" id="desc-{key}">[^<]*</div>')
    for key in ("DDGS_BACKEND", "DDGS_REGION", "DDGS_TIMELIMIT", "DDGS_SAFESEARCH")
)


def _without_corrected_help(dom: str) -> str:
    for pattern in WS_CORRECTED_HELP:
        dom = pattern.sub("", dom)
    return dom


# 7.122.3 (U6): the Web Search find box matches a curated haystack instead of
# the card's rendered text, and the haystack travels as two data attributes on
# the card. They are this release's only change to the grid's markup, and the
# test below removes exactly them from both sides after asserting they are
# there -- the same "the exclusion can never quietly match nothing" rule the
# corrected help texts follow.
WS_HAYSTACK_ATTRS = re.compile(r' data-ws-(?:search|configured)="[^"]*"')


# 7.122.3 (U17): a card's retired command row, as rendered by 7.113.2.
RETIRED_COMMAND_ROW = re.compile(
    r'<li class="agent-command-row" data-kind="legacy">.*?</li>', re.DOTALL
)


def _without_retired_commands(dom):
    """Strip the retired command rows from one HTML string or a list of them.

    Both captures in this module store a view's children as a list of strings.
    """

    if isinstance(dom, list):
        return [_without_retired_commands(entry) for entry in dom]
    return RETIRED_COMMAND_ROW.sub("", dom)


def _count_retired_command_rows(dom) -> int:
    parts = dom if isinstance(dom, list) else [dom]
    return sum(len(RETIRED_COMMAND_ROW.findall(part)) for part in parts)


def _without_ux_pass_nodes(dom: str) -> str:
    """The grid with this pass's settings-wide display changes undone.

    Mirrors ``uxNormalise`` in ``tests/api/admin_jsdom_ux_views.mjs``: the
    provenance chip is removed (U12 moved it and reworded it), and the words a
    select uses for its default are normalised (U27/U30). What either says is
    proved by the ux_pass scenario; this comparison is about everything else.
    """

    dom = WS_HAYSTACK_ATTRS.sub("", _without_corrected_help(dom))
    dom = re.sub(r'<span class="field-source">[^<]*</span>', "", dom)
    dom = re.sub(r'<option value="">Default \([^<]*\)</option>', "", dom)
    dom = re.sub(r'class="field-default">default: [^<]*<', "", dom)
    return dom


WS_CHOICES = [
    "ddgs",
    "ollama",
    "exa",
    "tavily",
    "brave",
    "searxng",
    "jina",
    "serper",
    "firecrawl",
    "linkup",
    "perplexity",
    "parallel",
    "searchapi",
    "serpapi",
    "context7",
    "legacy",
]


@pytest.fixture(scope="module")
def websearch_chain(tmp_path_factory: pytest.TempPathFactory) -> dict:
    path = tmp_path_factory.mktemp("websearch_chain") / "fixture.json"
    path.write_text(json.dumps(websearch_page_fixture()), encoding="utf-8")
    return _run(
        MCC_JSDOM_SCENARIO="websearch_chain", MCC_JSDOM_WEBSEARCH_FIXTURE=str(path)
    )


def test_jsdom_the_websearch_chain_scenario_runs_clean(websearch_chain) -> None:
    assert websearch_chain["fatal"] is None
    assert websearch_chain["scenarioFatal"] is None
    assert websearch_chain["scriptErrors"] == []
    assert websearch_chain["consoleErrors"] == []
    assert websearch_chain["hasChainCard"] is True


def test_jsdom_with_no_chain_the_web_search_page_is_the_base_trees(
    websearch_chain,
) -> None:
    """E4: the route summary, the provider cards (Exa's key manager open), the
    settings section and the Recent requests table, as text, equal what
    7.112.1 drew -- once this release's additive nodes (the Search chain card
    and its status panel, the Context7 card, field and option) and 7.112.0's
    two plain chain fields are taken out on both sides."""

    baseline = json.loads(WS_BASELINE.read_text(encoding="utf-8"))
    assert "BASE tree 0f0d7908" in baseline["written_by"]
    unset = websearch_chain["unset"]
    for pattern in WS_CORRECTED_HELP:
        # Each corrected help text is one node of the provider grid, on both
        # sides: the exclusion below can never quietly match nothing.
        assert len(pattern.findall(unset["dom"]["grid"])) == 1, pattern.pattern
        assert len(pattern.findall(baseline["unset"]["dom"]["grid"])) == 1, (
            pattern.pattern
        )
    for name, base_dom in baseline["unset"]["dom"].items():
        if name == "grid":
            # 7.122.3 (U6): two attributes per rendered card, and nothing else.
            cards = unset["dom"][name].count("data-websearch-provider=")
            assert cards == 14, cards
            assert len(WS_HAYSTACK_ATTRS.findall(unset["dom"][name])) == cards * 2
            assert WS_HAYSTACK_ATTRS.findall(base_dom) == []
        assert _without_ux_pass_nodes(unset["dom"][name]) == (
            _without_ux_pass_nodes(base_dom)
        ), name
    assert unset["dirty"] == baseline["unset"]["dirty"] == "No changes"
    assert unset["recent"] == baseline["unset"]["recent"]
    assert unset["keys"] == baseline["unset"]["keys"]
    # No chain row on the page: the table keeps its twelve columns.
    assert len(unset["recent"]["headers"]) == 12


def test_jsdom_the_search_chain_card_sits_above_the_grid_and_owns_both_settings(
    websearch_chain,
) -> None:
    chain = websearch_chain["unset"]["chain"]
    assert chain["exists"] is True
    assert chain["inSection"] is True
    assert chain["pill"] == "Not set"
    assert chain["add"] == "Add search service"
    assert chain["rows"] == []
    assert chain["value"] == ""
    # The pause list has no control of its own: a hidden input on Apply's path.
    assert chain["pausedType"] == "hidden"
    assert "WEB_SEARCH_CHAIN" not in chain["gridKeys"]
    assert "WEB_SEARCH_CHAIN_PAUSED" not in chain["gridKeys"]
    assert chain["gridKeys"][:2] == [
        "WEB_SEARCH_PROVIDER",
        "WEB_SEARCH_FALLBACK_POLICY",
    ]


def test_jsdom_the_chain_editor_keeps_order_and_repeats(websearch_chain) -> None:
    edited = websearch_chain["edited"]
    # A repeat is a second try at its place: never collapsed.
    assert edited["value"] == "exa,context7,tavily,exa"
    assert [row["value"] for row in edited["rows"]] == [
        "exa",
        "context7",
        "tavily",
        "exa",
    ]
    # The chip reads "x2" with a multiplication sign (U+00D7).
    twice = chr(0xD7) + "2"
    assert [row["repeat"] for row in edited["rows"]] == [twice, "", "", twice]
    assert [row["index"] for row in edited["rows"]] == ["1", "2", "3", "4"]
    assert all(row["grip"] for row in edited["rows"])
    # Every catalogue provider and the old scraper, nothing else.
    assert edited["rows"][0]["options"] == ["", *WS_CHOICES]
    assert edited["pill"] == "4 entries"
    assert websearch_chain["editedDirty"] == "1 unsaved change"


def test_jsdom_while_a_chain_is_set_the_summary_shows_it_and_says_what_it_replaces(
    websearch_chain,
) -> None:
    summary = websearch_chain["editedSummary"]
    assert summary["headline"] == "Exa"
    assert summary["pill"] == "Ready"
    assert summary["path"] == "Search chain: Exa → Context7 → Tavily → Exa"
    assert (
        "Web Search Provider and Fallback Policy are not used while it is"
        in summary["note"]
    )
    cards = websearch_chain["editedCards"]
    assert cards["exa"] == "Search chain #1, #4 · EXA_API_KEY"
    assert cards["context7"] == "Search chain #2 · CONTEXT7_API_KEY"
    assert cards["brave"] == "Not in the search chain · BRAVE_SEARCH_API_KEY"
    assert cards["exaActive"] is True


def test_jsdom_a_pause_writes_the_paused_setting_for_every_listing(
    websearch_chain,
) -> None:
    paused = websearch_chain["pausedOnce"]
    assert paused["paused"] == "tavily"
    assert paused["value"] == "exa,context7,tavily,exa"
    assert [row["paused"] for row in paused["rows"]] == [False, False, True, False]
    assert [row["pause"] for row in paused["rows"]] == [
        "Pause",
        "Pause",
        "Resume",
        "Pause",
    ]
    assert paused["pill"] == "4 entries · 1 paused"
    assert websearch_chain["pausedStatus"].startswith(
        "Paused Tavily in the search chain"
    )
    assert websearch_chain["pausedSummary"]["skipped"] == ["Tavily (paused)"]


def test_jsdom_the_arrows_the_drag_and_its_undo_reorder_the_chain(
    websearch_chain,
) -> None:
    assert websearch_chain["afterDown"] == "context7,exa,tavily,exa"
    assert websearch_chain["afterUp"] == "context7,exa,exa,tavily"
    assert websearch_chain["afterDrag"] == "tavily,context7,exa,exa"
    assert websearch_chain["dragStatus"] == (
        "Moved 1 search service inside the Web search chain, at position 1. "
        "Nothing was saved yet -- press Apply."
    )
    assert websearch_chain["afterUndo"] == "context7,exa,exa,tavily"
    assert websearch_chain["afterRemove"] == "context7,exa,tavily"


def test_jsdom_apply_writes_the_two_chain_settings_and_nothing_else(
    websearch_chain,
) -> None:
    assert websearch_chain["finalDirty"] == "2 unsaved changes"
    assert websearch_chain["applyPosts"] == [
        {
            "values": {
                "WEB_SEARCH_CHAIN": "context7,exa,tavily",
                "WEB_SEARCH_CHAIN_PAUSED": "tavily",
            }
        }
    ]


def test_jsdom_a_saved_chain_renders_unchanged_and_unsaved_nothing(
    websearch_chain,
) -> None:
    saved = websearch_chain["saved"]
    # An unchanged Save would send nothing: no field reads as edited.
    assert saved["dirty"] == "No changes"
    assert saved["chain"]["value"] == "exa,context7,tavily"
    assert [row["value"] for row in saved["chain"]["rows"]] == [
        "exa",
        "context7",
        "tavily",
    ]
    assert saved["summary"]["path"] == "Search chain: Exa → Context7 → Tavily"
    assert saved["summary"]["headline"] == "Exa"
    assert saved["cards"]["exa"] == "Search chain #1 · EXA_API_KEY"
    assert saved["cards"]["exaActive"] is True


def test_jsdom_disabled_still_disables_with_a_chain_set(websearch_chain) -> None:
    disabled = websearch_chain["disabled"]
    assert disabled["headline"] == "Web search disabled"
    assert disabled["pill"] == "Search disabled"
    assert "search chain is not used" in disabled["note"]
    assert disabled["primary"] == ""


def test_jsdom_key_health_speaks_the_model_rules_words(websearch_chain) -> None:
    words = websearch_chain["words"]
    assert words["note"].startswith("Health from the search chain")
    assert [row["health"] for row in words["rows"]] == [
        "benched 60 s: rate limited · 4 req · 1 err",
        "benched 30 s: out of credits · 4 req · 1 err",
        "locked out, tier 2 of 3 (1h left) · 4 req · 1 err",
        "benched 45 s: circuit open after repeated failures · 4 req · 1 err",
        "healthy · 3 req · 0 err",
    ]
    # Only a benched key offers Reset.
    assert [row["reset"] for row in words["rows"]] == [True, True, True, True, False]


def test_jsdom_reset_posts_to_the_reset_route_with_the_key_guard(
    websearch_chain,
) -> None:
    assert websearch_chain["resetPosts"] == [
        {
            "path": (
                "/admin/api/websearch/credentials/EXA_API_KEY/keys/0/reset"
                "?id=sha256%3Aexa_000000000000"
            ),
            "method": "POST",
            "body": {},
        }
    ]
    after = websearch_chain["afterReset"]["rows"]
    assert after[0] == {"health": "healthy · 3 req · 0 err", "reset": False}


def test_jsdom_context7_has_a_card_and_auto_never_picks_it(websearch_chain) -> None:
    card = websearch_chain["context7"]
    assert card["label"] == "Context7"
    assert card["pill"] == "Configured"
    assert card["meta"] == "Library docs · never picked by Auto · CONTEXT7_API_KEY"
    assert card["buttons"] == ["Use provider", "Test provider", "Manage keys"]
    assert websearch_chain["cardOrder"][-1] == "context7"
    # Auto with Exa, Tavily and Context7 keyed: Exa, as the server resolves it.
    assert websearch_chain["autoActive"] == ["exa"]


def test_jsdom_analytics_shows_position_key_tries_and_failure_kind(
    websearch_chain,
) -> None:
    recent = websearch_chain["chainRecent"]
    assert recent["headers"] == [
        "Time",
        "Route",
        "Attempt",
        "Position",
        "Provider",
        "Key",
        "Key tries",
        "Query",
        "Results",
        "Latency",
        "Status",
        "Error",
        "Failure kind",
        "Cost",
        "Details",
    ]
    position = [row[3] for row in recent["rows"]]
    tries = [row[6] for row in recent["rows"]]
    kinds = [row[12] for row in recent["rows"]]
    # Rows: tavily served, context7 skipped, exa failed, a 7.112.0 row with
    # no chain (the columns NULL), a row from before 7.112.0 (no columns).
    assert position == ["3", "2", "1", "—", "—"]
    assert kinds == ["—", "—", "upstream", "—", "—"]
    assert tries[0].startswith("1 try")
    assert tries[1] == "—"
    assert tries[2].startswith("3 tries")
    assert tries[3:] == ["—", "—"]
    # NULL is a dash, never a zero.
    assert "0" not in position


def test_jsdom_the_key_tries_expand_to_the_request_ladder(websearch_chain) -> None:
    cells = websearch_chain["triesCells"]
    assert [cell["summary"] for cell in cells] == ["1 try", "3 tries"]
    assert cells[0]["lines"] == [
        {
            "cls": "req-chain-try is-served",
            "text": "#1 · key 0 tvly-…0001 · served · 80ms",
            "decision": "",
        }
    ]
    lines = cells[1]["lines"]
    assert [line["cls"] for line in lines] == [
        "req-chain-try is-429",
        "req-chain-try is-503",
        "req-chain-try is-served",
    ]
    assert lines[0]["decision"] == (
        f"key 0 {WS_PROBE} — benched 7s (rate_limit): 429 with Retry-After 7 → next key"
    )
    assert lines[1]["text"] == "#2 · key 1 exa-…0002 · 503 · 13ms"
    assert lines[2]["text"] == "#3 · key 1 exa-…0002 · served · 30ms · waited 500ms"


def test_jsdom_the_detail_modal_renders_the_ladder_and_null_as_a_dash(
    websearch_chain,
) -> None:
    failed = websearch_chain["detailFailed"]
    assert failed["meta"]["Chain position"] == "1"
    assert failed["meta"]["Outcome"] == "failed"
    assert failed["meta"]["Failure kind"] == "upstream"
    assert failed["triesHidden"] is False
    assert len(failed["tries"]) == 3
    skipped = websearch_chain["detailSkipped"]
    assert skipped["meta"]["Failure kind"] == "—"
    assert skipped["triesHidden"] is False
    assert skipped["triesText"] == "—"
    # A row without a chain shows none of it.
    for name in ("detailNoChain", "detailOld"):
        row = websearch_chain[name]
        assert row["triesHidden"] is True
        assert "Chain position" not in row["meta"]
        assert "Outcome" not in row["meta"]


def test_jsdom_third_party_text_on_the_chain_surfaces_stays_text(
    websearch_chain,
) -> None:
    """Key labels and kinds in tries_json, a provider's error, a documentation
    excerpt: markup-shaped text renders as characters, never as elements."""

    assert websearch_chain["recentMarkup"] == 0
    assert websearch_chain["detailFailed"]["markup"] == 0
    assert websearch_chain["xssRan"] is False
    assert WS_PROBE in websearch_chain["detailFailed"]["summary"]
    assert WS_PROBE in websearch_chain["detailFailed"]["meta"]["Error"]
    assert WS_PROBE in websearch_chain["triesCells"][1]["lines"][0]["text"]


# --------------------------------------------------------------------------
# 7.113.3: the Coding agents page's filter box. One run of
# admin_jsdom_coding_agents_filter.mjs (MCC_JSDOM_SCENARIO=coding_agents_filter)
# on the harness's own payloads -- 15 CLI agents and 10 desktop apps -- and the
# empty-query DOM compared with the one the BASE tree (7.113.2, aa4afc78) drew
# from the same scenario pointed at its own admin_static.

CA_BASELINE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "coding_agents_page_dom_7_113_2.json"
)
CA_CLI = [
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
CA_DESKTOP = [
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


@pytest.fixture(scope="module")
def coding_agents_filter() -> dict:
    return _run(MCC_JSDOM_SCENARIO="coding_agents_filter")


def test_jsdom_coding_agents_filter_scenario_runs_clean(
    coding_agents_filter,
) -> None:
    assert coding_agents_filter["fatal"] is None
    assert coding_agents_filter["scenarioFatal"] is None
    assert coding_agents_filter["scriptErrors"] == []
    assert coding_agents_filter["consoleErrors"] == []
    assert coding_agents_filter["baseTree"] is False
    assert coding_agents_filter["harnessRoutes"] == [
        "/admin/api/harnesses",
        "/admin/api/desktop-apps",
    ]


def test_jsdom_coding_agents_filter_box_is_first_and_at_rest_shows_every_card(
    coding_agents_filter,
) -> None:
    empty = coding_agents_filter["empty"]
    assert empty["cli"] == CA_CLI
    assert empty["desktop"] == CA_DESKTOP
    assert empty["total"] == 25
    assert empty["shown"] == 25
    assert empty["hidden"] == []
    assert empty["noteHidden"] is True
    assert empty["filtering"] is False
    assert empty["barClass"] == "coding-agents-filter"
    assert empty["value"] == ""
    # The box reads first: it is the first control on the page, above both
    # card groups and every card's buttons.
    assert coding_agents_filter["firstFocusableId"] == "codingAgentsFilter"


def test_jsdom_coding_agents_filter_empty_query_is_the_base_trees_page_exactly(
    coding_agents_filter,
) -> None:
    """The view's children, bar apart, are what 7.113.2 drew.

    The baseline was captured by this same scenario file pointed at the BASE
    tree's admin_static, where the box does not exist: so the equality is the
    empty box's whole footprint on the page -- one wrapper, one input, one
    hidden note -- and nothing else.
    """

    baseline = json.loads(CA_BASELINE.read_text(encoding="utf-8"))
    assert "BASE tree aa4afc78" in baseline["written_by"]
    assert baseline["dom"]["viewChildCount"] == 4
    assert baseline["dom"]["filterBar"] is None
    dom = coding_agents_filter["dom"]
    assert dom["viewChildCount"] == 5
    assert dom["filterBar"].count("<input") == 1
    assert 'id="codingAgentsFilter"' in dom["filterBar"]
    # 7.122.3 (U17): the only other difference from 7.113.2 is the retired
    # command rows, which this release stops drawing. The exclusion is stated
    # as a count on both sides, so it can never quietly match nothing.
    base_without_legacy = _without_retired_commands(baseline["dom"]["children"])
    mine_without_legacy = _without_retired_commands(dom["children"])
    assert _count_retired_command_rows(baseline["dom"]["children"]) > 0
    assert _count_retired_command_rows(dom["children"]) == 0
    assert mine_without_legacy == base_without_legacy


def test_jsdom_coding_agents_filter_keeps_exactly_the_cards_that_render_it(
    coding_agents_filter,
) -> None:
    codex = coding_agents_filter["codex"]
    assert codex["cli"] == ["codex"]
    # Both Codex desktop cards: the live one and the second machine's.
    assert codex["desktop"] == ["codex_desktop", "codex_removed"]
    assert codex["shown"] == 3
    assert codex["noteHidden"] is True
    assert codex["filtering"] is True
    assert codex["barClass"] == "coding-agents-filter coding-agents-filtering"
    # What is visible is exactly what renders the query -- read independently
    # from each card's own text -- and everything else is hidden, not gone.
    assert (
        codex["cli"] + codex["desktop"]
        == coding_agents_filter["codexHits"]
        == [
            "codex",
            "codex_desktop",
            "codex_removed",
        ]
    )
    assert len(codex["hidden"]) == 22
    assert set(codex["hidden"]) == (set(CA_CLI) | set(CA_DESKTOP)) - {
        "codex",
        "codex_desktop",
        "codex_removed",
    }


def test_jsdom_coding_agents_filter_matches_a_name_a_command_and_no_retired_alias(
    coding_agents_filter,
) -> None:
    # Kilo's own summary calls it "a fork of OpenCode", so Kilo is an honest
    # match for that word; the desktop app is in the other group.
    assert coding_agents_filter["opencode"]["cli"] == ["opencode", "kilo"]
    assert coding_agents_filter["opencode"]["desktop"] == ["opencode_desktop"]
    assert coding_agents_filter["opencode"]["shown"] == 3
    # Roo Code's card names the policy that manages it ("...Policies\Claude").
    assert coding_agents_filter["claude"]["cli"] == ["claude"]
    assert coding_agents_filter["claude"]["desktop"] == ["claude_desktop", "roo_code"]
    # Typed in capitals and still a match: the comparison is case-insensitive.
    assert coding_agents_filter["claude"]["value"] == "CLAUDE"
    # 7.122.3 (U17): the retired alias is not rendered, so it matches nothing.
    # This was the one card it used to match; U17 removes the mention itself.
    assert coding_agents_filter["legacyAlias"]["cli"] == []
    assert coding_agents_filter["legacyAlias"]["desktop"] == []
    assert coding_agents_filter["legacyAlias"]["shown"] == 0


def test_jsdom_coding_agents_filter_nothing_matches_and_no_control_leaves(
    coding_agents_filter,
) -> None:
    nothing = coding_agents_filter["nothing"]
    assert nothing["shown"] == 0
    assert nothing["cli"] == []
    assert nothing["desktop"] == []
    assert len(nothing["hidden"]) == 25
    assert nothing["noteHidden"] is False
    assert nothing["noteText"] == "No agents match."
    assert nothing["filtering"] is True
    # Hiding a card never detaches a control: the page's [data-key] count is
    # the same at rest, filtered and cleared, which is what keeps
    # changedValues() and every Save working.
    assert coding_agents_filter["dataKeyCountEmpty"] == 164
    assert coding_agents_filter["dataKeyCountFiltered"] == 164
    assert coding_agents_filter["dataKeyCountCleared"] == 164


def test_jsdom_coding_agents_filter_typing_sends_nothing_and_clearing_restores(
    coding_agents_filter,
) -> None:
    """No request per keystroke, and clearing leaves the page it started as."""

    assert coding_agents_filter["typingFetchCalls"] == []
    assert coding_agents_filter["typingState"]["shown"] == 3
    cleared = coding_agents_filter["cleared"]
    assert cleared["cli"] == CA_CLI
    assert cleared["desktop"] == CA_DESKTOP
    assert cleared["hidden"] == []
    assert cleared["noteHidden"] is True
    assert cleared["filtering"] is False
    assert cleared["value"] == ""
    # Not "visible again": the same markup, byte for byte.
    assert coding_agents_filter["clearedDom"] == coding_agents_filter["dom"]


def test_jsdom_coding_agents_filter_markup_in_the_query_never_reaches_the_page(
    coding_agents_filter,
) -> None:
    """The query is compared, never rendered: no element, no attribute, no
    script, and the note says the same sentence it always says."""

    xss = coding_agents_filter["xss"]
    assert xss["typed"] == (
        '<img src=x onerror="window.__agentXss=1"><script>window.__agentXss=2</script>'
    )
    assert xss["imagesAfter"] == xss["imagesBefore"]
    assert xss["scriptsAfter"] == xss["scriptsBefore"]
    assert xss["elementsAfter"] == xss["elementsBefore"]
    assert xss["flag"] == ""
    assert xss["viewMarkup"] is False
    assert xss["shown"] == 0
    assert xss["noteText"] == "No agents match."


def test_admin_coding_agents_filter_functions_cannot_fetch_or_write_markup() -> None:
    """A future edit that adds a request or an HTML sink fails here, not in a
    review: the filter is a comparison over text that is already rendered."""

    source = (STATIC_DIR / "admin.js").read_text(encoding="utf-8")
    start = source.index("function codingAgentHaystack(")
    end = source.index("\nfunction initCodingAgentsFilter(", start)
    body = source[start:end]
    for forbidden in (
        "fetch(",
        "api(",
        "XMLHttpRequest",
        ".innerHTML",
        "insertAdjacentHTML",
        "outerHTML",
    ):
        assert forbidden not in body, forbidden


# --------------------------------------------------------------------------- #
# admin_jsdom_in_loop_chip.mjs (MCC_JSDOM_SCENARIO=in_loop_chip)
# on the harness's own Models payload -- 45 models per provider -- plus one
# /admin/api/requests/in-loop answer of four rows covering a stop, a measured
# zero and an unmeasured tail. The scenario then makes that route fail and
# re-loads the page once.
# --------------------------------------------------------------------------- #

IN_LOOP_CHIP = "last 7d: 8/86 in-loop turns ended with text only"
IN_LOOP_ZERO = "last 7d: 0/4 in-loop turns ended with text only"
IN_LOOP_UNMEASURED = "last 7d: 3/12 in-loop turns ended with text only (2 not measured)"


@pytest.fixture(scope="module")
def in_loop_chip() -> dict:
    return _run(MCC_JSDOM_SCENARIO="in_loop_chip")


def _in_loop_chips(payload: dict, phase: str) -> dict:
    rows = payload[phase]
    return {row["ref"]: row["chips"] for row in rows}


def test_jsdom_in_loop_chip_scenario_runs_clean(in_loop_chip) -> None:
    assert in_loop_chip["fatal"] is None
    assert in_loop_chip.get("scenarioError") is None
    assert in_loop_chip["scriptErrors"] == []
    assert in_loop_chip["consoleErrors"] == []
    assert in_loop_chip["harnessRoutes"] == ["/admin/api/requests/in-loop"]


def test_jsdom_in_loop_chip_draws_one_chip_per_measured_model(in_loop_chip) -> None:
    chips = _in_loop_chips(in_loop_chip, "loaded")

    assert [chip["text"] for chip in chips["alpha/model-01"]] == [IN_LOOP_CHIP]
    assert [chip["className"] for chip in chips["alpha/model-01"]] == [
        "models-chip models-chip-measured"
    ]
    # The measured zero is drawn as a zero; the unmeasured tail is named
    # beside the count instead of being folded into it or dropped.
    assert [chip["text"] for chip in chips["alpha/model-03"]] == [IN_LOOP_ZERO]
    assert [chip["text"] for chip in chips["alpha/model-02"]] == [IN_LOOP_UNMEASURED]
    # A model the endpoint did not name draws no chip at all: never measured
    # is not the same fact as measured zero.
    assert chips["alpha/model-00:free"] == []
    assert chips["alpha/model-04"] == []


def test_jsdom_in_loop_chip_says_what_the_turn_is(in_loop_chip) -> None:
    title = _in_loop_chips(in_loop_chip, "loaded")["alpha/model-01"][0]["title"]

    assert "followed a tool result" in title
    assert "called no tool" in title
    assert "visible text" in title
    assert "Observation only" in title
    # The window the count covers is named, not implied by the page's own
    # reasoning chip beside it.
    assert "last 7 days" in title


def test_jsdom_in_loop_chip_is_fetched_beside_the_page(in_loop_chip) -> None:
    """One call on the first load, one more on the reload; the page's own
    payload carries no in-loop field on any row."""

    assert in_loop_chip["callsBeforeFailure"] == 1
    assert in_loop_chip["callsAfterFailure"] == 2
    assert not [key for key in in_loop_chip["pageRowKeys"] if "in_loop" in str(key)], (
        in_loop_chip["pageRowKeys"]
    )


def test_jsdom_in_loop_chip_failure_leaves_the_page_intact(in_loop_chip) -> None:
    """The chip is a readout: losing it costs the chip, never the tree."""

    assert in_loop_chip["treeStillRendered"] is True
    assert in_loop_chip["statusText"] == ""
    after = _in_loop_chips(in_loop_chip, "afterFailure")
    assert after, "the model rows must still be there"
    assert all(chips == [] for chips in after.values()), after


# --------------------------------------------------------------------------
# 7.118.1: the Coding agents page's "not yet" card for @deepseek-ai/dsh.
# One run of admin_jsdom_not_yet_card.mjs (MCC_JSDOM_SCENARIO=not_yet_card) on
# the registry's own payload -- the server's `_not_yet_entry` output, written to
# a file and put on the harness's `/admin/api/harnesses` payload under the new
# `not_yet` key -- and the page compared, byte for byte, with the one the BASE
# tree (16bc7b23, v7.118.0) drew from the same scenario and the same payload.
# Its admin.js does not know the key, so it draws no card: that is the equality
# baseline, and it is what proves the card is the only thing this release adds.

NOT_YET_BASELINE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "coding_agents_not_yet_card_dom_7_118_0.json"
)


@pytest.fixture(scope="module")
def not_yet_card(tmp_path_factory) -> dict:
    payload = [_not_yet_entry(card) for card in not_yet_cards()]
    assert payload, "the registry carries no not-yet card, so nothing would render"
    path = tmp_path_factory.mktemp("not_yet_card") / "not_yet.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return _run(MCC_JSDOM_SCENARIO="not_yet_card", MCC_JSDOM_NOT_YET_FILE=str(path))


def test_jsdom_not_yet_card_scenario_runs_clean(not_yet_card) -> None:
    assert not_yet_card["fatal"] is None
    assert not_yet_card["scenarioFatal"] is None
    assert not_yet_card["scriptErrors"] == []
    assert not_yet_card["consoleErrors"] == []
    assert not_yet_card["baseTree"] is False


def test_jsdom_not_yet_card_is_drawn_with_its_date_and_its_reason(not_yet_card) -> None:
    """The card is the whole feature, so its text is the contract.

    A "not yet" without a date, a measurement and the condition that would
    reverse it is indistinguishable from an agent nobody got round to, and it
    cannot be re-checked. Every one of those is asserted on the rendered page.
    """

    card = not_yet_card["card"]
    assert card["id"] == "dsh"
    assert card["title"] == "DeepSeek Harness"
    assert card["badge"] == "Not yet"
    assert not_yet_card["notYetIds"] == ["dsh"]
    # What it is, when it was assessed, and what the measurement found.
    assert "2026-10-30" in card["text"]
    assert "@deepseek-ai/dsh" in card["text"]
    assert "replaces the targeted row's whole config" in card["text"]
    assert "was gone" in card["text"]
    # Why that matters, and the one thing that would change the decision.
    assert "hides a provider you configured" in card["text"]
    assert "What would change this" in card["text"]
    assert "merges into the existing provider map" in card["text"]
    # The facts as rows, not only as prose.
    assert card["rows"] == ["Assessed", "Package", "Launcher", "Package page"]
    assert card["values"][0] == "2026-10-30"
    assert card["values"][1] == "@deepseek-ai/dsh"
    assert card["values"][2] == "None - MCC publishes no command for this agent"
    assert card["links"] == ["https://www.npmjs.com/package/@deepseek-ai/dsh"]


def test_jsdom_not_yet_card_offers_nothing_to_press(not_yet_card) -> None:
    """It explains; it does not offer. No Configure, no Install, no Undo.

    The four host divs and the `[data-key]` controls are how every other card
    on this page gets its buttons, so their absence is the mechanical form of
    "this agent is not configured by MCC".
    """

    card = not_yet_card["card"]
    assert card["controls"] == []
    assert card["installHosts"] == 0
    assert card["mcpHosts"] == 0
    assert card["dataKeys"] == 0
    assert card["hasTiers"] is False
    # And the two words that would read as support are not on it at all.
    assert "Install" not in card["text"]
    assert "Configure" not in card["text"]


def test_jsdom_not_yet_card_is_the_only_thing_this_release_added(
    not_yet_card,
) -> None:
    """Every other card, and the whole page around them, is byte-identical.

    The baseline was captured by this same scenario file pointed at the BASE
    tree's admin_static with the same payload, so the comparison is between one
    page and the same page plus one card -- no other card, section, host or
    label moved, and the view gained no child of its own (the card lives inside
    the CLI list, and the capture takes it out of a clone).
    """

    baseline = json.loads(NOT_YET_BASELINE.read_text(encoding="utf-8"))
    assert "BASE tree 16bc7b23" in baseline["written_by"]
    assert baseline["dom"]["viewChildCount"] == 5
    assert not_yet_card["dom"]["viewChildCount"] == 5
    # 7.122.3 (U17): the retired command rows are the only other difference,
    # normalised out on both sides with a count on each.
    assert _count_retired_command_rows(baseline["dom"]["children"]) > 0
    assert _count_retired_command_rows(not_yet_card["dom"]["children"]) == 0
    assert _without_retired_commands(not_yet_card["dom"]["children"]) == (
        _without_retired_commands(baseline["dom"]["children"])
    )
    assert [
        {"id": card["id"], "title": card["title"]} for card in not_yet_card["cards"]
    ] == baseline["cards"]
    assert len(not_yet_card["cards"]) == 25


def test_jsdom_not_yet_card_is_still_findable_by_the_filter(not_yet_card) -> None:
    """The find box matches rendered text, so the new card is searchable too."""

    assert not_yet_card["deepseekHits"] == ["dsh"]
    assert not_yet_card["deepseek"]["visibleCli"] == ["dsh"]
    assert not_yet_card["deepseek"]["noteHidden"] is True
    assert not_yet_card["dshHits"] == ["dsh"]
    assert not_yet_card["dsh"]["visibleCli"] == ["dsh"]
    # Clearing shows every card again, the not-yet one included.
    assert "dsh" in not_yet_card["cleared"]["visibleCli"]


# --------------------------------------------------------------------------
# 7.121.0: the request detail's raw diagnostics. One run of
# admin_jsdom_raw_diagnostics.mjs (MCC_JSDOM_SCENARIO=raw_diagnostics) over
# answers the server's own reader produced from FAKE log files written here --
# the summary, every page and every normalised answer, keyed the way the
# scenario's fetch stub looks them up -- so the page renders the server's real
# sentences. Request 1 has a kind in each of the four states; request 2 has a
# payload repr (pretty-print), seven SSE frames over three pages, an exception
# type in server.log and the CLI box. The card with the new section taken out
# is compared with the one the BASE tree (53d78dab, v7.120.2) drew from the
# same scenario: the equality that proves the section is the only addition.

RAWV_FIRST = "req_" + "e" * 32
RAWV_SECOND = "req_" + "f" * 32
RAWV_RAW_MARKER = "RAW-SSE-ONLY-7121"
RAWV_NORMALIZED_MARKER = "NORMALIZED-ONLY-7121"
RAWV_PROMPT_MARKER = "NORMALIZED-PROMPT-7121"
#: A payload repr with everything the pretty-print must carry exactly: a
#: quote of each kind, escapes, a NUL, non-ASCII, an integer no JavaScript
#: number holds, an exponent, a tuple, empties, True/False/None.
RAWV_PAYLOAD: dict[str, Any] = {
    "model": "fake-model",
    "max_tokens": 12345678901234567890123,
    "temperature": 0.7,
    "top_p": 1e-05,
    "stream": True,
    "stop": None,
    "metadata": {"empty": {}, "list": [], "flag": False},
    "messages": [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": 'It\'s "quoted" \\ back\nslash ünïcode ⟦x⟧\ttab\x00nul',
                }
            ],
        }
    ],
    "pair": (1, 2.5, "three"),
}
RAWV_BASELINE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "request_detail_dom_7_120_2.json"
)


def _rawv_sink(marker: str, request_id: str, message: str) -> bytes:
    text = f"{marker} {request_id} {message}"
    return text.replace("\r", "\\r").replace("\n", "\\n").encode("utf-8")


def _rawv_frame(request_id: str, text: str) -> bytes:
    frame = (
        "event: content_block_delta\n"
        f'data: {{"type":"content_block_delta","delta":{{"type":"text_delta","text":"{text}"}}}}'
    )
    return _rawv_sink(
        "RAW_SSE", request_id, f"SSE_EVENT: content_block_delta - {frame}"
    )


def _rawv_file(path: Path, lines: list[bytes], mtime: float) -> Path:
    path.write_bytes(b"".join(line + b"\r\n" for line in lines))
    os.utime(path, (mtime, mtime))
    return path


def _rawv_answers(folder: Path) -> dict[str, Any]:
    """Every answer the page will ask for, from the real reader over fake files."""

    now = time.time()
    logs = folder / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    _rawv_file(
        logs / "raw-diagnostics.2026-10-30_09-00-00_000001.log",
        [
            _rawv_frame(RAWV_FIRST, f"<thinking>{RAWV_RAW_MARKER} {n}</thinking>")
            for n in range(2)
        ],
        now - 3000,
    )
    _rawv_file(
        logs / "raw-diagnostics.2026-10-30_09-30-00_000002.log",
        [_rawv_sink("RAW_CLI", "-", "Non-JSON output: starting") for _ in range(2)],
        now - 1800,
    )
    active = _rawv_file(
        logs / "raw-diagnostics.log",
        [
            _rawv_sink(
                "RAW_PAYLOAD",
                RAWV_SECOND,
                f"FULL_PAYLOAD [{RAWV_SECOND}]: {RAWV_PAYLOAD!r}",
            ),
            *[_rawv_frame(RAWV_SECOND, f"frame {n}") for n in range(7)],
        ],
        now - 5,
    )
    error = {
        "time": "2026-10-30 10:00:00.000000+03:00",
        "level": "ERROR",
        "message": f"Messages request failed request_id={RAWV_SECOND} exc_type=ValueError",
        "module": "my_claude_code.api.request_errors",
        "function": "log_unexpected_api_exception",
        "line": 0,
        "request_id": RAWV_SECOND,
    }
    server = _rawv_file(
        logs / "server.log", [json.dumps(error).encode("utf-8")], now - 5
    )
    sources = raw_reader.RawSources(raw_log=active, server_log=server)
    # The harness's own "req-fallback" row, opened for the fallback half of the
    # DOM equality. Its full payload is the harness's; this file writes only
    # the fields the raw section reads, and none of these lines is its.
    fallback = {
        "id": "req-fallback",
        "ts_epoch": now - 86_400,
        "status": "success",
        "output_text": "the fallback row's answer",
    }
    rows = {
        fallback["id"]: fallback,
        RAWV_FIRST: {
            "id": RAWV_FIRST,
            "ts_epoch": now - 7200,
            "ts_iso": "2026-10-30T07:00:00+00:00",
            "endpoint": "/v1/messages",
            "protocol": "anthropic",
            "provider": "fake",
            "requested_model": "fake/model",
            "resolved_model": "fake/model",
            "status": "success",
            "input_text": RAWV_PROMPT_MARKER,
            "input_chars": len(RAWV_PROMPT_MARKER),
            "output_text": RAWV_NORMALIZED_MARKER,
            "output_chars": len(RAWV_NORMALIZED_MARKER),
        },
        RAWV_SECOND: {
            "id": RAWV_SECOND,
            "ts_epoch": now - 60,
            "ts_iso": "2026-10-30T08:59:00+00:00",
            "endpoint": "/v1/messages",
            "protocol": "anthropic",
            "provider": "fake",
            "requested_model": "fake/model",
            "resolved_model": "fake/model",
            "status": "error",
            "error_kind": "api_error",
            "error_message": "boom",
            "output_text": "a partial answer",
            "output_chars": 16,
        },
    }
    settings = {
        "req-fallback": raw_reader.RawSettings(
            enabled={"payloads": True, "sse": True, "traceback": True, "cli": False},
            retain_files=2,
        ),
        RAWV_FIRST: raw_reader.RawSettings(
            enabled={"payloads": True, "sse": True, "traceback": True, "cli": False},
            retain_files=2,
        ),
        RAWV_SECOND: raw_reader.RawSettings(
            enabled=dict.fromkeys(raw_reader.RAW_KINDS, True), retain_files=2
        ),
    }
    answers: dict[str, Any] = {}
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(raw_reader, "PAGE_MAX_LINES", 3)
        for request_id, row in rows.items():
            flags = settings[request_id]
            answers[f"{request_id}|summary"] = raw_reader.raw_summary(
                sources, request_id, row, flags
            )
            for kind in raw_reader.RAW_KINDS:
                answers[f"{request_id}|{kind}|normalized"] = raw_reader.normalized_view(
                    request_id, kind, row, flags
                )
                start, anchor = 0, None
                while True:
                    page = raw_reader.raw_page(
                        sources,
                        request_id,
                        kind,
                        row,
                        flags,
                        start=start,
                        anchor=anchor,
                    )
                    answers[f"{request_id}|{kind}|raw|{start}"] = page
                    if page.get("next_start") is None:
                        break
                    start, anchor = page["next_start"], page["anchor"]
    return {
        "order": [RAWV_FIRST, RAWV_SECOND],
        "rows": rows,
        "answers": answers,
        "exportFilename": f"mcc-request-{RAWV_SECOND}-csv-2026-10-30T09-00-00.csv",
        "exportFailure": {
            "status": 422,
            "detail": "XLSX cannot hold this export without cutting it: Excel keeps at "
            "most 32,767 characters in a cell.",
        },
    }


@pytest.fixture(scope="module")
def raw_diagnostics(tmp_path_factory) -> dict:
    folder = tmp_path_factory.mktemp("raw_diagnostics")
    data = _rawv_answers(folder)
    path = folder / "raw_answers.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return _run(MCC_JSDOM_SCENARIO="raw_diagnostics", MCC_JSDOM_RAW_FILE=str(path))


def _rawv_jsonable(value: Any) -> Any:
    if isinstance(value, tuple | list):
        return [_rawv_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _rawv_jsonable(item) for key, item in value.items()}
    return value


def test_jsdom_raw_diagnostics_scenario_runs_clean(raw_diagnostics) -> None:
    assert raw_diagnostics["fatal"] is None
    assert raw_diagnostics["scenarioFatal"] is None
    assert raw_diagnostics["scriptErrors"] == []
    assert raw_diagnostics["consoleErrors"] == []
    assert raw_diagnostics["baseTree"] is False
    assert raw_diagnostics["missing"] == [], "the page asked for an answer nobody wrote"


def test_jsdom_raw_section_is_last_collapsed_and_names_what_it_shows(
    raw_diagnostics,
) -> None:
    assert raw_diagnostics["sectionHidden"] is False
    assert raw_diagnostics["sectionIsLast"] is True
    assert "nothing is masked" in raw_diagnostics["warning"]
    boxes = raw_diagnostics["collapsed"]
    assert [box["kind"] for box in boxes] == ["payloads", "sse", "traceback", "cli"]
    assert [box["label"] for box in boxes] == [
        "API payloads",
        "SSE events",
        "API error tracebacks",
        "CLI diagnostics",
    ]
    assert all(box["open"] is False for box in boxes)
    assert all(
        box["pressed"] == [["raw", "true"], ["normalized", "false"]] for box in boxes
    )
    facts = {box["kind"]: box["facts"] for box in boxes}
    assert facts["payloads"] == "pruned by rotation"
    assert facts["sse"].startswith("2 lines · ")
    assert facts["traceback"] == "no error logged"
    assert facts["cli"] == "not captured · LOG_RAW_CLI_DIAGNOSTICS is off"


def test_jsdom_raw_shows_the_four_honest_states(raw_diagnostics) -> None:
    raw = raw_diagnostics["rawFirst"]
    # present
    assert raw["sse"]["pre"].count(RAWV_RAW_MARKER) == 2
    assert raw["sse"]["hasCopy"] is True
    assert any("pruned by rotation" in note for note in raw["sse"]["notes"])
    # pruned by rotation
    assert raw["payloads"]["stateHeadline"] == "No raw lines remain."
    assert "keeps 2 rotated files of 50 MB" in raw["payloads"]["state"]
    assert "only in normalised form" in raw["payloads"]["state"]
    assert raw["payloads"]["pre"] is None
    # nothing to capture
    assert raw["traceback"]["stateHeadline"] == "No error logged."
    # the setting is off: which one, where it is, and when it applies
    cli = raw["cli"]["state"]
    assert raw["cli"]["stateHeadline"] == "Not captured."
    # The server writes the name in backticks; the page draws them as <code>,
    # so the text reads as the bare name.
    assert "LOG_RAW_CLI_DIAGNOSTICS is off" in cli
    assert raw["cli"]["codeSpans"] == 1
    assert "Limits & Resilience → Diagnostics → “Log Raw CLI Diagnostics”" in cli
    assert raw["cli"]["pre"] is None


def test_jsdom_raw_and_normalized_never_show_each_others_text(raw_diagnostics) -> None:
    raw = raw_diagnostics["rawFirst"]
    normalized = raw_diagnostics["normalizedFirst"]
    for view in raw.values():
        text = f"{view['state']} {view['pre'] or ''} {' '.join(view['notes'])}"
        assert RAWV_NORMALIZED_MARKER not in text
        assert RAWV_PROMPT_MARKER not in text
        assert view["source"].startswith("Raw: ")
    assert RAWV_NORMALIZED_MARKER in normalized["sse"]["pre"]
    assert RAWV_RAW_MARKER not in normalized["sse"]["pre"]
    assert RAWV_PROMPT_MARKER in normalized["payloads"]["pre"]
    assert normalized["sse"]["source"].startswith(
        "Normalized: stored in the request log"
    )
    assert "requests.output_text" in normalized["sse"]["source"]
    assert normalized["cli"]["stateHeadline"] == "No normalised form."
    assert all(view["view"] == "normalized" for view in normalized.values())


def test_jsdom_the_view_choice_is_remembered_and_blocked_storage_reads_raw(
    raw_diagnostics,
) -> None:
    normalized = [["raw", "false"], ["normalized", "true"]]
    raw = [["raw", "true"], ["normalized", "false"]]
    assert raw_diagnostics["storedAfterNormalized"] == "normalized"
    assert raw_diagnostics["pressedAfterNormalized"] == [normalized] * 4
    assert raw_diagnostics["pressedOnReopen"] == [normalized] * 4
    # Raw is the default and is stored as absence.
    assert raw_diagnostics["storedAfterRaw"] is None
    assert raw_diagnostics["blockedStore"] is True, raw_diagnostics.get("blockError")
    assert raw_diagnostics["pressedWhenBlocked"] == [raw] * 4


def test_jsdom_payload_pretty_print_is_labelled_and_lossless(raw_diagnostics) -> None:
    line = f"FULL_PAYLOAD [{RAWV_SECOND}]: {RAWV_PAYLOAD!r}"
    assert raw_diagnostics["payloadVerbatim"] == line.replace("\n", "\\n")
    assert raw_diagnostics["prettyButton"] == "Pretty-print as JSON"
    assert raw_diagnostics["prettyButtonOn"] == "Show the stored line"
    assert any(
        "nothing is evaluated" in note for note in raw_diagnostics["prettyNotes"]
    )
    prefix, pretty = raw_diagnostics["prettyPre"].split("\n", 1)
    assert prefix == f"FULL_PAYLOAD [{RAWV_SECOND}]:"
    literal = line.split("]: ", 1)[1]
    assert json.loads(pretty) == _rawv_jsonable(ast.literal_eval(literal))
    # Digits copied, never rounded through a JavaScript number.
    assert '"max_tokens": 12345678901234567890123' in pretty
    assert '"top_p": 1e-05' in pretty
    assert raw_diagnostics["prettyOffPre"] == raw_diagnostics["payloadVerbatim"]


def test_jsdom_copy_copies_exactly_what_the_box_shows(raw_diagnostics) -> None:
    assert raw_diagnostics["copied"] == [
        raw_diagnostics["prettyPre"],
        raw_diagnostics["prettyOffPre"],
    ]


def test_jsdom_paging_loads_every_line_and_sends_the_anchor_back(
    raw_diagnostics,
) -> None:
    pages = raw_diagnostics["ssePages"]
    assert [page["pre"].count("frame ") for page in pages] == [3, 6, 7]
    # The page writes an en dash between the two numbers.
    dash = chr(0x2013)
    assert pages[0]["more"].startswith(f"Showing lines 1{dash}3 of 7.")
    assert pages[-1]["more"] == "All 7 recorded lines are shown."
    assert [f"frame {n}" in pages[-1]["pre"] for n in range(7)] == [True] * 7
    asked = [
        url
        for url in raw_diagnostics["rawUrls"]
        if "kind=sse" in url and "start=" in url
    ]
    assert len(asked) == 2
    assert all("anchor=" in url and "anchor=&" not in url for url in asked)


def test_jsdom_the_traceback_and_cli_boxes_say_what_they_hold(raw_diagnostics) -> None:
    traceback = raw_diagnostics["traceback"]
    assert "exc_type=ValueError" in traceback["pre"]
    assert any(
        "LOG_API_ERROR_TRACEBACKS was off" in note for note in traceback["notes"]
    )
    cli = raw_diagnostics["cli"]
    assert cli["stateHeadline"] == "Not tied to this request."
    assert "(2 such lines in the retained window)" in cli["state"]


def test_jsdom_the_per_request_export_picker(raw_diagnostics) -> None:
    assert raw_diagnostics["exportPanelHiddenAtFirst"] is True
    assert raw_diagnostics["exportPanelOpened"] == {"hidden": False, "expanded": "true"}
    assert raw_diagnostics["exportKinds"] == [
        ["payloads", True, "API payloads"],
        ["sse", True, "SSE events"],
        ["traceback", True, "API error tracebacks"],
        ["cli", True, "CLI diagnostics"],
    ]
    assert [value for value, _label in raw_diagnostics["exportViews"]] == [
        "both",
        "raw",
        "normalized",
    ]
    assert raw_diagnostics["exportFormats"] == ["json", "csv", "xlsx", "txt"]
    (url,) = raw_diagnostics["exportUrls"][:1]
    assert url.startswith("/admin/api/requests/raw/export?")
    assert f"id={RAWV_SECOND}" in url
    assert "format=csv" in url and "view=raw" in url
    assert "kinds=payloads%2Csse%2Ctraceback" in url
    assert raw_diagnostics["downloads"] == [
        {
            "filename": f"mcc-request-{RAWV_SECOND}-csv-2026-10-30T09-00-00.csv",
            "size": 13,
        }
    ]
    assert raw_diagnostics["exportHint"] == "Export downloaded."
    assert raw_diagnostics["exportFailureHint"].startswith(
        "Not exported: XLSX cannot hold this export without cutting it"
    )
    assert raw_diagnostics["exportEmptyHint"] == "Tick at least one kind to export."
    assert raw_diagnostics["exportEmptyFetched"] == 0
    assert raw_diagnostics["exportPanelOnNextOpen"] == {
        "hidden": True,
        "expanded": "false",
        "hint": "",
    }


#: One ``<dt>label</dt><dd>value</dd>`` pair of the request dialog's meta list.
_META_PAIR = re.compile(r"<dt>(.*?)</dt><dd>(.*?)</dd>", re.S)

#: The two rows 7.122.2 renamed, with the values they carry unchanged.
_RELABELLED_META_ROWS = {
    "TTFT (winner)": "First visible byte (winner)",
    "TTFT (incl. fallbacks)": "First visible byte (incl. fallbacks)",
}

#: The two rows 7.122.2 added, in the order they are drawn.
_ADDED_META_ROWS = ["Generation rate", "Throughput (whole request)"]


def _meta_pairs(html: str) -> list[tuple[str, str]]:
    return [(match.group(1), match.group(2)) for match in _META_PAIR.finditer(html)]


def _child_with_id(children: list[str], element_id: str) -> int:
    for index, html in enumerate(children):
        if f'id="{element_id}"' in html[:240]:
            return index
    raise AssertionError(f"no child carries id={element_id}")


def test_jsdom_the_raw_section_is_the_only_thing_the_request_detail_gained(
    raw_diagnostics,
) -> None:
    """The card with the raw section taken out of a clone is the base card.

    The baseline was captured by this same scenario file pointed at the BASE
    tree's admin_static (53d78dab, v7.120.2): the same two rows, the same
    harness, the same clicks. Every child of the card -- route trace, images,
    media, wire panes, the whole transcript -- must be byte-identical.

    7.122.2 changes exactly two of them, both named here: the meta list gained
    a generation rate and a throughput and renamed its two first-byte rows
    (values untouched), and the route timeline swapped its one "rate" cell for
    the generation and throughput pair. Every other child is still the base
    tree's bytes.
    """

    baseline = json.loads(RAWV_BASELINE.read_text(encoding="utf-8"))
    assert "BASE tree 53d78dab" in baseline["written_by"]
    for key in ("domFirst", "domFallback"):
        mine = raw_diagnostics[key]
        base = baseline[key]
        assert base["childCount"] == 8
        assert mine["childCount"] == base["childCount"] + 1
        before, after = base["children"], mine["children"]
        meta = _child_with_id(before, "reqDetailMeta")
        chain = _child_with_id(before, "reqDetailChain")
        assert _child_with_id(after, "reqDetailMeta") == meta
        assert _child_with_id(after, "reqDetailChain") == chain

        # Every pre-existing fact is still there, unchanged, in its order.
        # The one row 7.122.2 *removed* is named rather than ignored: it was
        # replaced by the two rates below, and its old value is the number the
        # entire fix exists because of.
        was = _meta_pairs(before[meta])
        assert any(label == "Output rate" for label, _value in was), key
        expected = [
            (_RELABELLED_META_ROWS.get(label, label), value)
            for label, value in was
            if label != "Output rate"
        ]
        pairs = _meta_pairs(after[meta])
        labels = {label for label, _ in expected}
        assert [
            (label, value) for label, value in pairs if label in labels
        ] == expected, key
        # And the only rows that were added are the two rates.
        assert [
            label for label, _ in pairs if label not in labels
        ] == _ADDED_META_ROWS, key

        # The attempt timeline: the retired "rate" cell, replaced by the two.
        if key == "domFallback":
            assert ">rate<" in before[chain]
            assert ">rate<" not in after[chain]
            assert ">generation<" in after[chain]
            assert ">throughput<" in after[chain]
        for index, (was, now) in enumerate(zip(before, after, strict=True)):
            if index in (meta, chain):
                continue
            assert now == was, (key, index)


def test_the_help_text_names_the_two_rates_and_the_first_byte() -> None:
    """The page's own prose, which the baseline comparisons exclude.

    7.122.2 replaced one rate with two and renamed a measurement it had been
    mislabelling. The help is part of the product: it says what each number
    divides by, and it no longer claims a first *token* anywhere.
    """

    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    assert "Generation rate" in html
    assert "Throughput" in html
    assert "first visible byte" in html
    # The measured evidence, which is what makes the change credible.
    assert "429,152 tok/s" in html
    # The claims the fix retired, and the wording that went with them.
    assert "Output rate</em> is measured from the winner" not in html
    assert "p50 TTFT" not in html
    assert "tokens out, tok/s and what ended" not in html


# ==================================================================== 7.121.1
# Every saved value shown, and only real edits saved.
#
# A value outside a control's options rendered blank, a number box could not
# hold "7_200", the dirty count read the untouched control as changed, and
# Apply posted "" for it -- which the server reads as "delete the line". With
# no edit at all, one Save deleted five settings (the investigation's table:
# NVIDIA_NIM_API_KEY_ROTATION, DDGS_BACKEND, LOG_LEVEL, PROXY_CHECK_ENABLED,
# MODEL_DISCOVERY_REFRESH_SECONDS). The scenario renders the server's own
# /admin/api/config answer for a scratch .env holding exactly those values
# (tests/support/select_blank_payload.py) and measures what the page shows,
# what it would submit, and the exact Apply body.

SELECT_BLANK_BASELINE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "select_blank_dom_7_121_0.json"
)

#: 7.122.2 rewords the user-facing help in exactly these two views -- "TTFT"
#: becomes the time to the first visible byte, and the single "Output rate"
#: becomes a generation rate with a throughput beside it. They are excluded
#: from the byte-for-byte comparisons against earlier baselines and checked by
#: :func:`test_the_help_text_names_the_two_rates_and_the_first_byte` instead,
#: the same way 7.122.0 excluded the view it redrew.
RELABELLED_VIEWS = frozenset({"guide", "requests"})

#: The saved values no option carries -- the measured wipe sources.
UNLISTED_SAVED = {
    "NVIDIA_NIM_API_KEY_ROTATION": "on_error",
    "DDGS_BACKEND": "brave,mojeek",
    "LOG_LEVEL": "debug",
    "PROXY_CHECK_ENABLED": "True",
}

#: The saved number no `<input type=number>` can hold.
SAVED_NUMBER_KEY = "MODEL_DISCOVERY_REFRESH_SECONDS"
SAVED_NUMBER = "7_200"

#: A websearch rotation select that had no control at all until 7.121.2 and
#: now sits on its card showing its unlisted saved value. It is in the file and
#: must never be posted by a save nobody made.
TAVILY_ROTATION = "TAVILY_API_KEY_ROTATION"

#: 7.121.2's additions, which the 7.121.0 comparison below names: one rotation
#: field on each Web Search card, for the thirteen key pools the page had no
#: control for. Their 7.121.0 control is None (there was none).
WEBSEARCH_ROTATIONS = websearch_rotation_keys()

#: 7.122.3 (U27): the one field declared as a `select` whose options are
#: exactly false/true. It now reads Off/On like every other switch; its value
#: is untouched, so only the option text moves.
BOOLEAN_SELECT_KEY = "FALLBACK_BENCH_ENABLED"

#: Fields whose control is not a form control because the page edits them some
#: other way: the rail pause buttons write their own endpoint, and the three
#: oauth_login fields are buttons that start a login. Each group is asserted
#: below against the code that really renders it.
PAUSE_LIST_FIELDS = frozenset(
    {
        "MODEL_PAUSED",
        "MODEL_FABLE_PAUSED",
        "MODEL_HAIKU_PAUSED",
        "MODEL_IMAGE_PAUSED",
        "MODEL_MYTHOS_PAUSED",
        "MODEL_OPUS_PAUSED",
        "MODEL_ASR_PAUSED",
        "MODEL_SONNET_PAUSED",
        "MODEL_TTS_PAUSED",
        "MODEL_VIDEO_PAUSED",
        "MODEL_VISION_PAUSED",
    }
)
OAUTH_ACTION_FIELDS = frozenset(
    {"ANTHROPIC_OAUTH_MANAGE", "CHATGPT_OAUTH_IMPORT_CODEX", "CHATGPT_OAUTH_LOGIN"}
)


def _marked_label(value: str) -> str:
    return f"{value} — saved, not in today's list"


@pytest.fixture(scope="module")
def select_blank(tmp_path_factory) -> dict:
    """The real settings page, rendered from the server's own payload.

    The .env and the payload are built here rather than committed: the payload
    is `_config_response()` over a scratch managed file, so the page sees the
    manifests' own 547 fields and cannot drift from them.
    """

    root = tmp_path_factory.mktemp("select_blank")
    payload_path = root / "config.json"
    payload = build_payload(payload_path)
    assert len(payload["fields"]) == len(FIELDS), "the payload is not the manifest"
    # 7.121.2: what the server answers after the thirteen rotation edits are
    # saved -- through the real save path, from the body the page must send.
    after_path = root / "config-after.json"
    build_payload_after_save(after_path, rotation_edit_values())
    return _run(
        MCC_JSDOM_SCENARIO="select_blank",
        MCC_JSDOM_CONFIG_FILE=str(payload_path),
        MCC_JSDOM_CONFIG_AFTER_FILE=str(after_path),
    )


def test_jsdom_the_select_blank_scenario_runs_clean(select_blank) -> None:
    assert select_blank["fatal"] is None
    assert select_blank["scenarioFatal"] is None
    assert select_blank["scriptErrors"] == []
    assert select_blank["consoleErrors"] == []
    # The tree this runs on has the fix: the marked option exists.
    assert select_blank["baseTree"] is False


def test_jsdom_an_unlisted_saved_value_is_its_own_selected_option(select_blank) -> None:
    """The value the file holds is shown, selected, and still selectable.

    Listed last on purpose: a value that IS in today's list renders exactly the
    options it always did, in the order it always had them.
    """

    for key, saved in UNLISTED_SAVED.items():
        cell = select_blank["fields"][key]
        assert cell["present"] is True, key
        assert cell["tag"] == "select", key
        assert cell["optionValues"][-1] == saved, key
        assert cell["optionLabels"][-1] == _marked_label(saved), key
        assert cell["savedOption"] == _marked_label(saved), key
        assert cell["selectedIndex"] == len(cell["optionValues"]) - 1, key
        # The control holds the saved value, so the page is not blank and the
        # value compares equal to what was loaded.
        assert cell["value"] == saved, key
        assert cell["original"] == saved, key
        assert cell["shown"] == _marked_label(saved), key
        # Still the deliberate clear, still first, still enabled.
        assert cell["optionLabels"][0].startswith("Default ("), key
        assert cell["disabled"] is False, key


def test_jsdom_a_listed_saved_value_renders_exactly_as_before(select_blank) -> None:
    """Nothing is marked where the saved value is a listed option."""

    safesearch = select_blank["fields"]["DDGS_SAFESEARCH"]
    assert safesearch["shown"] == "strict (older spelling, read as on)"
    assert safesearch["savedOption"] is None
    assert safesearch["optionValues"] == ["", "", "on", "moderate", "off", "strict"]
    assert safesearch["selectedIndex"] == 5

    extractor = select_blank["fields"]["WEB_FETCH_EXTRACTOR"]
    assert extractor["shown"] == "Every text node, as before"
    assert extractor["savedOption"] is None

    # A number the box CAN hold keeps its number box, its bounds and its value.
    rows = select_blank["fields"]["REQUEST_LOG_MAX_ROWS"]
    assert (rows["tag"], rows["type"]) == ("input", "number")
    assert rows["value"] == "20000"
    assert rows["savedNote"] is None

    # A plain text value was never at risk; it still shows what is saved.
    language = select_blank["fields"]["BRAVE_SEARCH_LANG"]
    assert (language["tag"], language["type"], language["value"]) == (
        "input",
        "text",
        "en",
    )
    assert language["savedNote"] is None


def test_jsdom_a_number_box_that_cannot_hold_the_saved_value_shows_it_as_text(
    select_blank,
) -> None:
    """`7_200` is a legal setting and an illegal number-input value.

    The browser sanitises it to "", which is how the number vanished. Rendered
    as text, marked with the reason, it is visible and untouched.
    """

    cell = select_blank["fields"][SAVED_NUMBER_KEY]

    assert (cell["tag"], cell["type"]) == ("input", "text")
    assert cell["value"] == SAVED_NUMBER
    assert cell["original"] == SAVED_NUMBER
    assert cell["shown"] == SAVED_NUMBER
    assert cell["savedNote"] == (
        f'Saved as "{SAVED_NUMBER}" — not a number this box can hold. '
        "Shown exactly as stored; retype it as a number to change it."
    )
    assert cell["submits"] is None


def test_jsdom_an_untouched_page_has_no_unsaved_changes(select_blank) -> None:
    """Seven values the page could not display used to read as five edits."""

    assert select_blank["footerOnLoad"] == "No changes"
    assert select_blank["changedOnLoad"] == []


def test_jsdom_apply_with_no_edit_posts_nothing(select_blank) -> None:
    """The body that used to carry five `""` is not sent at all."""

    applied = select_blank["applyOnLoad"]

    assert applied["clickable"] is True
    assert applied["disabled"] is True
    assert applied["body"] is None
    assert select_blank["footerAfterEmptyApply"] == "No changes"


def test_jsdom_one_edit_posts_exactly_one_key(select_blank) -> None:
    """Typing one region sends one region -- not that plus five blanks."""

    edit = select_blank["afterEdit"]

    assert edit["typed"] == "uk-en"
    assert edit["footer"] == "1 unsaved change"
    assert edit["submitted"] == ["DDGS_REGION"]
    assert select_blank["applyAfterEdit"]["body"] == {
        "values": {"DDGS_REGION": "uk-en"}
    }


def test_jsdom_no_unlisted_value_is_ever_submitted_untouched(select_blank) -> None:
    """Every value the page cannot display stays out of every Apply body."""

    bodies = [
        select_blank["applyOnLoad"]["body"],
        select_blank["applyAfterEdit"]["body"],
    ]
    for body in bodies:
        if body is None:
            continue
        for key in (*UNLISTED_SAVED, SAVED_NUMBER_KEY, TAVILY_ROTATION):
            assert key not in body["values"], key
            # 7.121.3: nor named as cleared -- a mark the page fabricated
            # would be read by the server as a deliberate clear.
            assert key not in body.get("cleared", []), key
    for key in (*UNLISTED_SAVED, SAVED_NUMBER_KEY, TAVILY_ROTATION):
        assert select_blank["fields"][key].get("submits") is None, key

    # The one body that carries a blank (the deliberate "Use default" in the
    # 7.121.3 tests) carries only the key a person cleared, in both fields.
    clear_body = select_blank["clear"]["body"]
    assert set(clear_body["values"]) == set(clear_body["cleared"]) == {"LOG_LEVEL"}


def test_jsdom_a_control_nobody_touched_is_never_posted(select_blank) -> None:
    """The rule itself, isolated from every display fix.

    The marked option and the saved-number text remove today's fabricated
    differences, so a value-diff against the loaded config would pass every
    page-level test above -- and would still be the wrong rule. The probe is a
    real control in the document whose value genuinely differs from what was
    loaded and that nobody has touched: it is not a change until an edit event
    says so. That is what makes the event, not the difference, the evidence.
    """

    probe = select_blank["untouchedProbe"]

    assert probe["valueDiffersFromOriginal"] is True
    assert probe["beforeTouch"] is True
    assert probe["afterTouch"] == "different"


def test_jsdom_the_page_is_the_base_page_with_the_additions_taken_out(
    select_blank,
) -> None:
    """The DOM this arc adds is the only DOM that moved since 7.121.0.

    The baseline is the same scenario, the same harness and the same payload
    run against the BASE tree f6de93ad: every settings view with the arc's
    additions normalised out of a clone (7.121.1's marked options, saved-number
    note and saved-number control; 7.121.2's rotation field on each Web Search
    card), and every one of the 547 controls' own markup. The controls allowed
    to move are named: the number box that becomes a text box (7.121.1), and
    the thirteen web-search rotation selects that did not exist (7.121.2).
    Everything else is byte-identical.

    7.122.2 rewords the user-facing help -- "TTFT" becomes the time to the
    first visible byte, and the single "Output rate" becomes a generation rate
    and a throughput -- so the two views that hold that prose are excluded
    here and checked directly by
    :func:`test_the_help_text_names_the_two_rates_and_the_first_byte`.
    """

    baseline = json.loads(SELECT_BLANK_BASELINE.read_text(encoding="utf-8"))
    base = baseline["base"]

    # 7.122.0 redraws Get Started itself, which this baseline digests along
    # with the other fourteen views; every OTHER view is still byte-identical.
    # The Get Started page has its own base-written baseline now
    # (get_started_dom_7_122_0.json, test_jsdom_get_started_*).
    # 7.122.3: this pass restructures some views and changes the words every
    # settings control wears (UX_CHANGED_VIEWS / UX_SETTINGS_ONLY_VIEWS); those
    # are compared, with the display-only changes normalised out, against this
    # pass's own base-written baseline in test_admin_ux_pass_jsdom.py. Every
    # other view is still byte-identical here.
    excluded = (
        RELABELLED_VIEWS | UX_CHANGED_VIEWS | UX_SETTINGS_ONLY_VIEWS | {"get_started"}
    )
    assert {
        key: value
        for key, value in select_blank["views"].items()
        if key not in excluded
    } == {key: value for key, value in base["views"].items() if key not in excluded}

    moved = sorted(
        key
        for key, html in base["controls"].items()
        if select_blank["controls"].get(key) != html
    )
    # 7.122.3 (U27): a select whose options are exactly false/true now wears
    # the On/Off labels every other switch uses. Its value is untouched; the
    # option text is the whole of the change.
    assert moved == sorted([SAVED_NUMBER_KEY, *WEBSEARCH_ROTATIONS, BOOLEAN_SELECT_KEY])
    for key in WEBSEARCH_ROTATIONS:
        assert base["controls"][key] is None, key
        assert select_blank["controls"][key].startswith("<select"), key
        assert f'id="field-{key}"' in select_blank["controls"][key], key

    before = base["controls"][SAVED_NUMBER_KEY]
    after = select_blank["controls"][SAVED_NUMBER_KEY]
    assert 'type="number"' in before
    assert 'type="text"' in after
    assert 'data-saved-number="true"' in after
    assert "data-saved-number" not in before


def test_jsdom_only_the_cells_this_release_names_moved(select_blank) -> None:
    """The base tree's five broken cells, and the one 7.121.2 adds, moved.

    The five are 7.121.1's (blank -> shown); the sixth is the Tavily rotation,
    which had no control at all before 7.121.2 and now shows its unlisted saved
    value as its own option. Nothing else moved.
    """

    baseline = json.loads(SELECT_BLANK_BASELINE.read_text(encoding="utf-8"))
    base = baseline["base"]
    record = baseline["moved_by_7_121_1"]

    moved = sorted(
        key
        for key, cell in base["fields"].items()
        if select_blank["fields"].get(key) != cell
    )
    assert moved == sorted([*UNLISTED_SAVED, SAVED_NUMBER_KEY, TAVILY_ROTATION])
    assert sorted(record["fields"]) == sorted([*UNLISTED_SAVED, SAVED_NUMBER_KEY])
    for key, entry in record["fields"].items():
        assert entry["before"] == base["fields"][key], key
        assert entry["after"] == select_blank["fields"][key], key
    assert base["fields"][TAVILY_ROTATION] == {"present": False}
    tavily = select_blank["fields"][TAVILY_ROTATION]
    assert tavily["savedOption"] == _marked_label("on_error")
    assert tavily["value"] == tavily["original"] == "on_error"

    # The base page said "5 unsaved changes" on load, and its Apply posted the
    # five ""; this tree says none, and posts nothing.
    assert record["footerOnLoad"] == {
        "before": "5 unsaved changes",
        "after": "No changes",
    }
    assert record["changedOnLoad"]["before"] == sorted(
        [*UNLISTED_SAVED, SAVED_NUMBER_KEY]
    )
    assert record["changedOnLoad"]["after"] == []
    assert record["applyOnLoad"]["before"]["body"] == {
        "values": dict.fromkeys([*UNLISTED_SAVED, SAVED_NUMBER_KEY], "")
    }
    assert record["applyOnLoad"]["after"] == select_blank["applyOnLoad"]
    # The base tree posted a control nobody had touched; this one does not.
    assert record["untouchedProbe"] == {
        "before": {
            "valueDiffersFromOriginal": True,
            "beforeTouch": False,
            "afterTouch": "different",
        },
        "after": {
            "valueDiffersFromOriginal": True,
            "beforeTouch": True,
            "afterTouch": "different",
        },
    }
    assert record["views"] == []


def test_jsdom_the_saved_fixture_matches_the_committed_baseline() -> None:
    """A fixture edit that forgets to regenerate the baseline fails here.

    Regenerate with the base tree on purpose, never to make a run pass: the
    baseline is what pins the page as it was before this release.
    """

    baseline = json.loads(SELECT_BLANK_BASELINE.read_text(encoding="utf-8"))

    assert (
        baseline["fixture_env_sha256"]
        == hashlib.sha256(FIXTURE_ENV.encode("utf-8")).hexdigest()
    )
    assert baseline["payload_field_count"] == len(FIELDS)
    assert "f6de93ad" in baseline["written_by"]


def test_jsdom_every_setting_without_a_form_control_is_named_and_hosted(
    select_blank,
) -> None:
    """A setting the page cannot edit is a setting nobody can change.

    Two groups have no `[data-key]` form control, each because the page edits
    it some other way. Naming them here is what makes a NEW setting without a
    control a failure instead of a silent gap:

    * the rail pause buttons write ``ROUTE_PAUSE_KEYS`` through their own
      endpoint -- there is a control per model row, and a second form control
      for the same list would be a second way to write it;
    * the three ``oauth_login`` fields are buttons that start a sign-in.

    The thirteen web-search ``_ROTATION`` selects were the third group until
    7.121.2, with no control anywhere; each is now on its provider's card.
    """

    without = set(select_blank["coverage"]["withoutControl"])

    assert without == PAUSE_LIST_FIELDS | OAUTH_ACTION_FIELDS
    assert len(WEBSEARCH_ROTATIONS) == 13
    assert without.isdisjoint(WEBSEARCH_ROTATIONS)

    # Each pause list is exactly one the route-pause endpoint writes.
    assert {
        paused_key for _model_key, _chain_key, paused_key in ROUTE_PAUSE_KEYS
    } == PAUSE_LIST_FIELDS
    # Each "no control" oauth field is the button-rendering type.
    for key in OAUTH_ACTION_FIELDS:
        assert FIELD_BY_KEY[key].field_type == "oauth_login", key

    assert select_blank["coverage"]["fieldCount"] == len(FIELDS)
    assert select_blank["coverage"]["controlCount"] == len(FIELDS) - len(without)


# ==================================================================== 7.121.2
# Every setting a control on its own page. Thirteen web-search key-pool
# rotation settings (TAVILY_API_KEY_ROTATION and twelve more) could be saved in
# .env and seen nowhere: the generic grid skips every `_ROTATION` field because
# a provider card hosts its own, and no web-search card ever did. Each card now
# carries its pool's rotation field. tests/contracts/settings_control_map.json
# lists every setting with its page and control id; the test below proves the
# file is what the page renders, and the static contracts fail a new setting
# that has no entry.

SELECT_BLANK_BASELINE_7_121_1 = (
    Path(__file__).resolve().parents[1] / "fixtures" / "select_blank_dom_7_121_1.json"
)
SETTINGS_CONTROL_MAP = (
    Path(__file__).resolve().parents[1] / "contracts" / "settings_control_map.json"
)


def _sha256(text: str | None) -> str | None:
    return None if text is None else hashlib.sha256(text.encode("utf-8")).hexdigest()


def _card_for_rotation(key: str) -> str:
    """The web-search provider whose key pool a rotation setting belongs to."""

    owners = [
        provider_id
        for provider_id, descriptor in WEBSEARCH_CATALOG.items()
        if descriptor.credential_env and f"{descriptor.credential_env}_ROTATION" == key
    ]
    assert len(owners) == 1, (key, owners)
    return owners[0]


def test_jsdom_every_websearch_rotation_is_a_control_on_its_providers_card(
    select_blank,
) -> None:
    """Thirteen settings that had no control each have one, where they belong.

    On the Web Search page, inside the card of the provider whose keys they
    rotate, built by the same renderField every field uses: id `field-<KEY>`,
    the manifest's label.
    """

    rotation = select_blank["rotation"]

    assert rotation["keys"] == list(WEBSEARCH_ROTATIONS)
    assert rotation["present"] == len(WEBSEARCH_ROTATIONS) == 13
    for key, cell in rotation["before"].items():
        assert cell is not None, key
        assert cell["view"] == "web_search", key
        assert cell["card"] == _card_for_rotation(key), key
        assert cell["id"] == f"field-{key}", key
        assert cell["label"] == FIELD_BY_KEY[key].label, key


def test_jsdom_the_rotation_controls_round_trip_a_save(select_blank) -> None:
    """Value shown -> edited -> saved -> shown, for all thirteen.

    Shown: an unset pool reads its Default, and the fixture's hand-written
    `TAVILY_API_KEY_ROTATION=on_error` reads as its own marked option. Edited:
    each moved to Round robin. Saved: the body is exactly the thirteen keys,
    and the page reloads from what the server answers after the REAL save path
    saved that same body (`build_payload_after_save`). Shown: each now reads
    Round robin, unmarked, and nothing is left unsaved.
    """

    rotation = select_blank["rotation"]
    expected = rotation_edit_values()

    for key, cell in rotation["before"].items():
        if key == TAVILY_ROTATION:
            assert cell["value"] == "on_error"
            assert cell["shown"] == _marked_label("on_error")
            assert cell["marked"] is True
        else:
            assert cell["value"] == "", key
            assert cell["shown"].startswith("Default ("), key
            assert cell["marked"] is False, key
    assert rotation["footerBeforeEdits"] == "No changes"

    assert rotation["footerAfterEdits"] == "13 unsaved changes"
    assert rotation["submitted"] == expected
    assert rotation["apply"]["body"] == {"values": expected}

    for key, cell in rotation["after"].items():
        assert cell["value"] == ROTATION_EDIT_VALUE, key
        assert cell["shown"] == "Round robin", key
        assert cell["marked"] is False, key
    assert rotation["footerAfterSave"] == "No changes"


def test_jsdom_the_settings_control_map_is_what_the_page_renders(select_blank) -> None:
    """The committed map is the page, entry for entry.

    The static contracts read tests/contracts/settings_control_map.json to fail
    a setting with no control; this is what stops the file from claiming a
    control the page does not render.
    """

    committed = json.loads(SETTINGS_CONTROL_MAP.read_text(encoding="utf-8"))

    assert select_blank["controlMap"] == committed["fields"]
    assert set(committed["fields"]) == {field.key for field in FIELDS}


def test_jsdom_7_121_2_adds_the_rotation_fields_and_moves_nothing_else(
    select_blank,
) -> None:
    """The page is 7.121.1's page plus thirteen rotation fields.

    The baseline is this scenario run on the BASE tree 5591e829 (v7.121.1) on
    the same payload. Every view is byte-identical with the card rotation
    fields taken out of a clone; every one of the 534 controls that existed
    keeps its exact markup (sha256); the moved cells are the thirteen new
    controls, their control-map entries, and the Tavily cell that now has a
    control to show its value in.
    """

    baseline = json.loads(SELECT_BLANK_BASELINE_7_121_1.read_text(encoding="utf-8"))
    base = baseline["base"]
    record = baseline["moved_by_7_121_2"]

    assert "5591e829" in baseline["written_by"]
    assert (
        baseline["fixture_env_sha256"]
        == hashlib.sha256(FIXTURE_ENV.encode("utf-8")).hexdigest()
    )

    # 7.122.0 (see the 7.121.0 test above): Get Started is redrawn by its own
    # release; every other view is unchanged. 7.122.2 rewords the help in the
    # Guide and on the Requests page, which are checked directly below.
    # 7.122.3: this pass restructures some views and changes the words every
    # settings control wears (UX_CHANGED_VIEWS / UX_SETTINGS_ONLY_VIEWS); those
    # are compared, with the display-only changes normalised out, against this
    # pass's own base-written baseline in test_admin_ux_pass_jsdom.py. Every
    # other view is still byte-identical here.
    excluded = (
        RELABELLED_VIEWS | UX_CHANGED_VIEWS | UX_SETTINGS_ONLY_VIEWS | {"get_started"}
    )
    assert {
        key: value
        for key, value in select_blank["views"].items()
        if key not in excluded
    } == {key: value for key, value in base["views"].items() if key not in excluded}
    assert record["views"] == []

    mine = {key: _sha256(html) for key, html in select_blank["controls"].items()}
    moved = sorted(
        key
        for key, digest in base["controls_sha256"].items()
        if mine.get(key) != digest
    )
    # 7.122.3 (U27): the one select stored as false/true wears On/Off now; its
    # value and every other control's markup are untouched.
    assert moved == sorted([*WEBSEARCH_ROTATIONS, BOOLEAN_SELECT_KEY])
    assert record["controls"] == list(WEBSEARCH_ROTATIONS)
    for key in WEBSEARCH_ROTATIONS:
        assert base["controls_sha256"][key] is None, key
    # 7.122.3 (U27) moved one more control's markup (its option labels), so
    # 533 of the 547 keep their exact bytes; the fourteen movers are named.
    assert len(base["controls_sha256"]) - len(moved) == 533

    moved_fields = sorted(
        key
        for key, cell in base["fields"].items()
        if select_blank["fields"].get(key) != cell
    )
    assert moved_fields == [TAVILY_ROTATION] == sorted(record["fields"])
    assert record["fields"][TAVILY_ROTATION]["before"] == {"present": False}
    assert (
        record["fields"][TAVILY_ROTATION]["after"]
        == select_blank["fields"][TAVILY_ROTATION]
    )

    moved_map = sorted(
        key
        for key, entry in base["controlMap"].items()
        if select_blank["controlMap"].get(key) != entry
    )
    assert moved_map == list(WEBSEARCH_ROTATIONS) == sorted(record["controlMap"])
    for key in WEBSEARCH_ROTATIONS:
        assert base["controlMap"][key] == {"page": "web_search", "control": None}
        assert select_blank["controlMap"][key] == {
            "page": "web_search",
            "control": f"field-{key}",
        }

    assert base["rotation"]["present"] == 0
    # 7.121.1's behaviour is untouched: clean on load, nothing posted untouched.
    assert select_blank["footerOnLoad"] == base["footerOnLoad"] == "No changes"
    assert select_blank["applyOnLoad"] == base["applyOnLoad"]


# ==================================================================== 7.121.3
# The clear travels with the request. The server refuses to blank a setting the
# request did not name in `cleared` (tests/api/test_admin_config_unmarked_clear.py
# proves that side), so the one gesture the refusal exists to protect -- "Use
# default" -- has to put the key in that list, and a save that clears nothing
# must not grow the field at all.


def test_jsdom_a_page_that_cleared_nothing_sends_no_cleared_list(select_blank) -> None:
    """The wire is unchanged for every save that clears nothing.

    Both bodies measured before the clear (no edit at all -- never sent -- and
    one typed edit) carry `values` and nothing else, so an Apply that clears
    nothing is byte-identical to 7.121.2's.
    """

    assert select_blank["clear"]["onLoad"] == []
    assert select_blank["applyAfterEdit"]["body"] == {
        "values": {"DDGS_REGION": "uk-en"}
    }


def test_jsdom_use_default_sends_the_key_it_clears(select_blank) -> None:
    """ "Use default" is a deliberate clear, so the request says which key it is.

    Without this the server would refuse the page's own clear -- and with the
    mark withdrawn on the next edit, the list names only what is actually
    being cleared, so a key sent with a value is never in it.
    """

    clear = select_blank["clear"]

    assert clear["buttonFound"] is True
    assert clear["value"] == ""
    assert clear["footer"] == "1 unsaved change"
    assert clear["keys"] == ["LOG_LEVEL"]
    assert clear["body"] == {
        "values": {"LOG_LEVEL": ""},
        "cleared": ["LOG_LEVEL"],
    }

    # Changing your mind before saving: the key carries a value, so it is not
    # listed as cleared -- the list names what is being cleared NOW, not
    # everything a control ever emptied.
    assert clear["afterChoosing"]["keys"] == []
    assert clear["afterChoosing"]["submitted"] == {"LOG_LEVEL": "INFO"}
    assert clear["afterChoosing"]["body"] == {"values": {"LOG_LEVEL": "INFO"}}
    assert clear["afterChoosing"]["footer"] == "1 unsaved change"
    assert clear["valueBody"] == {"values": {"LOG_LEVEL": "INFO"}}

    # And the clear, applied through the real button.
    assert clear["secondButtonFound"] is True
    assert clear["appliedClearBody"] == {
        "values": {"LOG_LEVEL": ""},
        "cleared": ["LOG_LEVEL"],
    }
    assert clear["footerAfterApply"] == "No changes"


def test_jsdom_emptying_a_chain_is_a_clear_the_request_names(select_blank) -> None:
    """The rail writes the chain's value itself, so its mark has to be made
    against the value the chain ends up holding.

    A chain's last row leaving is a real gesture -- it is how a rail is
    emptied -- and it is written by ``ModelChainEditor`` with no change event.
    If the edit were recorded before the value moved, the blank would reach the
    server unlisted and the save would be refused; recorded after, it is a
    clear like any other. Built with the page's own ``buildFieldControl``.
    """

    chain = select_blank["chainClear"]

    assert chain["loaded"] == "a,b"
    assert chain["rows"] == 2
    assert chain["submittedBefore"] is True
    assert chain["emptied"]["value"] == ""
    assert chain["emptied"]["submitted"] == ""
    assert chain["emptied"]["marked"] is True


def _scratch_managed_env(tmp_path, monkeypatch) -> Path:
    """A scratch config directory holding the fixture .env, and nothing else."""

    monkeypatch.setenv("MCC_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.delenv("MCC_ENV_FILE", raising=False)
    monkeypatch.chdir(tmp_path)
    paths.reset_config_dir_cache()
    managed = tmp_path / "config" / ".env"
    managed.parent.mkdir(parents=True)
    managed.write_text(FIXTURE_ENV, encoding="utf-8")
    return managed


def _valued_lines(text: str) -> dict[str, str]:
    """The file's `KEY=value` lines by key, verbatim, so bytes are compared."""

    lines: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", line)
        if match:
            lines[match.group(1)] = line
    return lines


def test_the_one_edit_body_changes_one_key_and_nothing_else(
    select_blank, tmp_path, monkeypatch
) -> None:
    """The page's own Apply body, through the server's real save path.

    The first save normalises the whole file layout (the 7.113.0 caveat), so it
    is done once before anything is compared; from there every other key's line
    has to survive byte for byte.
    """

    body = select_blank["applyAfterEdit"]["body"]
    assert body == {"values": {"DDGS_REGION": "uk-en"}}

    managed = _scratch_managed_env(tmp_path, monkeypatch)
    first = prepare_admin_update({})
    assert first.valid, first.errors
    commit_prepared_admin_update(first)
    normalised = managed.read_bytes()
    before = _valued_lines(normalised.decode("utf-8"))

    prepared = prepare_admin_update(body["values"])
    assert prepared.valid, prepared.errors
    commit_prepared_admin_update(prepared)

    written = managed.read_bytes()
    after = _valued_lines(written.decode("utf-8"))

    # Exactly one key appeared, and no value was removed or rewritten.
    assert set(after) - set(before) == {"DDGS_REGION"}
    assert set(before) <= set(after)
    for key, line in before.items():
        assert after[key] == line, key
    assert after["DDGS_REGION"] == "DDGS_REGION=uk-en"

    # The five values the unfixed page deleted are still in the file, and the
    # unmanaged line was never its to write.
    for key, saved in UNLISTED_SAVED.items():
        assert after[key] == f"{key}={saved}", key
    assert after[SAVED_NUMBER_KEY] == f"{SAVED_NUMBER_KEY}={SAVED_NUMBER}"
    assert "MCC_SMOKE_MARKER=keep-me" in after.values()

    # One rolling copy, holding exactly what the save replaced.
    assert (managed.parent / ".env.previous").read_bytes() == normalised


def test_the_same_body_saved_twice_is_a_no_op(
    select_blank, tmp_path, monkeypatch
) -> None:
    """Equality has a trap: the first Save normalises the layout. The second
    save of the same body must not move a byte."""

    body = select_blank["applyAfterEdit"]["body"]
    managed = _scratch_managed_env(tmp_path, monkeypatch)

    for _ in range(2):
        prepared = prepare_admin_update(body["values"])
        assert prepared.valid, prepared.errors
        commit_prepared_admin_update(prepared)
    settled = managed.read_bytes()

    again = prepare_admin_update(body["values"])
    assert again.valid, again.errors
    commit_prepared_admin_update(again)

    assert managed.read_bytes() == settled


# ------------------------------------------------- Get Started (7.122.0)
# The redesigned page: four essentials in four states, nine status cards, the
# collapsed filterable niche list, the collapsed read-only advanced section,
# the raw-log warning, "Not for me" and Show hidden through the server's
# skipped list, Dismiss with its Undo, and every Open button landing on the
# element it declares. The payloads are the server's own answers, written to a
# world file here and read by tests/api/admin_jsdom_get_started.mjs.

GET_STARTED_BASELINE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "get_started_dom_7_122_0.json"
)
#: The view this release redraws; every OTHER view is compared with the base.
GET_STARTED_VIEW = "get_started"


def _get_started_agents(*, traffic: bool = True, configured_desktop: bool = True):
    from my_claude_code.config.onboarding import OnboardingAgent

    return (
        OnboardingAgent(
            id="claude",
            display_name="Claude Code",
            kind="cli",
            installed=True,
            command="mcc-claude",
            requests_7d=5 if traffic else 0,
        ),
        OnboardingAgent(
            id="codex",
            display_name="Codex CLI",
            kind="cli",
            installed=True,
            command="mcc-codex",
            requests_7d=1 if traffic else 0,
        ),
        OnboardingAgent(
            id="opencode",
            display_name="OpenCode",
            kind="cli",
            installed=True,
            command="mcc-opencode",
        ),
        OnboardingAgent(
            id="claude_desktop",
            display_name="Claude Desktop",
            kind="desktop",
            installed=True,
            state="configured" if configured_desktop else "installed",
        ),
    )


def _get_started_onboarding() -> dict:
    """The four payloads the scenario renders, built by the real code.

    ``build_state`` decides every step and ``_onboarding_state_response``
    serialises it exactly as ``GET /admin/api/onboarding`` answers, so the
    words on the page are the server's.
    """
    from my_claude_code.api.admin_routes import _onboarding_state_response
    from my_claude_code.config.onboarding import build_state

    fresh_log = {"enabled": True, "total": 0, "last": None}
    launcher_log = {
        "enabled": True,
        "total": 5,
        "last": {
            "id": "r-launcher",
            "ts_epoch": time.time() - 120,
            "status": "success",
            "provider": "p1",
            "resolved_model": "p1/fake-small",
            "requested_model": "claude-sonnet-5",
            "harness": "claude",
            "optimization": None,
        },
    }
    variants = {}
    variants["fresh"] = _onboarding_state_response(
        build_state(
            claude_settings_configured=False,
            has_requests=False,
            agents=_get_started_agents(traffic=False, configured_desktop=False),
        ),
        dict(fresh_log),
    )
    variants["launcher"] = _onboarding_state_response(
        build_state(
            claude_settings_configured=False,
            has_requests=True,
            agents=_get_started_agents(),
        ),
        dict(launcher_log),
    )
    variants["wsl"] = _onboarding_state_response(
        build_state(
            claude_settings_configured=True,
            has_requests=False,
            agents=_get_started_agents(traffic=False, configured_desktop=False),
        ),
        dict(fresh_log),
    )
    variants["log_off"] = _onboarding_state_response(
        build_state(
            claude_settings_configured=True,
            has_requests=False,
            agents=_get_started_agents(traffic=False, configured_desktop=False),
            request_log_enabled=False,
        ),
        {"enabled": False, "total": 0, "last": None},
    )
    variants["empty"] = {
        "dismissed": True,
        "steps": [],
        "required_total": 0,
        "required_done": 0,
        "complete": True,
        "skipped": [],
        "request_log": {"enabled": True, "total": 0, "last": None},
    }
    return variants


def _get_started_onboarding_in_a_clean_shell() -> dict:
    """``_get_started_onboarding`` with no credential in the environment.

    A fresh install is a machine with no provider key set anywhere, and the
    process environment outranks every file, so this is the only way the four
    variants stay the same on a developer machine, on CI and on a base tree.
    """
    from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
    from my_claude_code.config.websearch_catalog import WEBSEARCH_CATALOG

    names = {"TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN", "MODEL"}
    for descriptor in PROVIDER_CATALOG.values():
        if descriptor.credential_env:
            names.add(descriptor.credential_env)
    for descriptor in WEBSEARCH_CATALOG.values():
        if descriptor.credential_env:
            names.add(descriptor.credential_env)
    with pytest.MonkeyPatch.context() as patch:
        for name in names:
            patch.delenv(name, raising=False)
        return _get_started_onboarding()


def _get_started_world(root: Path, *, raw_logs_on: bool = False) -> Path:
    """Write the world file the scenario renders, and return its path.

    The config payload is the server's own answer for a fake managed ``.env``
    (``tests/support/select_blank_payload.py``); this adds the three values the
    page reads off it that a fixture file cannot state -- provider status,
    route status and the manual-only web-search ids -- and, for the raw run,
    switches one raw-log setting on.
    """
    root.mkdir(parents=True, exist_ok=True)
    payload = build_payload(root / "config.json")
    by_key = {field["key"]: field for field in payload["fields"]}

    def set_value(key: str, value: str) -> None:
        field = by_key[key]
        field["value"] = value
        field["configured"] = bool(value.strip())
        field["set"] = True
        field["source"] = "managed_env"

    # A working setup: Zen's free tiers on four routes, one fallback chain, a
    # vision adapter, one media rail, and no key for nvidia_nim (which the
    # Haiku route names).
    set_value("MODEL_FABLE", "opencode/muse-spark-1.3-contributor-free")
    set_value("MODEL_OPUS", "opencode/muse-spark-1.3-contributor-free")
    set_value("MODEL_MYTHOS", "opencode/muse-spark-1.3-contributor-free")
    set_value("MODEL_SONNET", "opencode/muse-spark-1.3-contributor-free")
    set_value("MODEL_FALLBACKS", "groq/llama-3.3-70b")
    set_value("MODEL_VISION", "groq/llama-4-scout")
    set_value("MODEL_IMAGE", "p1/image-1")
    set_value("MCP_MEDIA_APPS", "claude,codex")
    set_value("ENABLE_WEB_SERVER_TOOLS", "true")
    set_value("LOG_RAW_SSE_EVENTS", "true" if raw_logs_on else "false")

    world = {
        "config": payload,
        "world": {
            "provider_status": [
                {
                    "provider_id": "opencode",
                    "display_name": "OpenCode Zen",
                    "status": "free_ready",
                    "label": "Free models ready",
                    "credential_env": "OPENCODE_API_KEY",
                    "key_count": 0,
                    "kind": "remote",
                },
                {
                    "provider_id": "groq",
                    "display_name": "Groq",
                    "status": "configured",
                    "label": "Configured",
                    "credential_env": "GROQ_API_KEY",
                    "key_count": 2,
                    "kind": "remote",
                },
                {
                    "provider_id": "nvidia_nim",
                    "display_name": "NVIDIA NIM",
                    "status": "missing_key",
                    "label": "Missing key",
                    "credential_env": "NVIDIA_NIM_API_KEY",
                    "key_count": 0,
                    "kind": "remote",
                },
            ],
            "route_status": {
                "providers": {
                    "nvidia_nim": {
                        "state": "no_credentials",
                        "action": "add_key",
                        "display_name": "NVIDIA NIM",
                    }
                }
            },
            "websearch_manual_only": ["context7"],
            "harness_tiers": {
                "tiers": [
                    {"id": "best", "label": "Best"},
                    {"id": "good", "label": "Good"},
                ],
                "harnesses": {
                    "claude": {
                        "best": {"override": None},
                        "good": {"override": None},
                    },
                    "codex": {
                        "best": {"override": "groq/llama-3.3-70b"},
                        "good": {"override": None},
                    },
                },
            },
            "marketplaces": {"views": []},
            "raw_logs_on": raw_logs_on,
        },
        # The four onboarding payloads are built with every provider and
        # web-search credential cleared: `load_value_state` treats the process
        # environment as its highest layer, and a test shell that exports one
        # (the suite itself does, for hermeticity) would otherwise change which
        # essentials the fixture reports as done.
        "onboarding": _get_started_onboarding_in_a_clean_shell(),
    }
    path = root / ("world-raw.json" if raw_logs_on else "world.json")
    path.write_text(json.dumps(world, ensure_ascii=False), encoding="utf-8")
    return path


def _get_started_run(tmp_path: Path, *, raw_logs_on: bool, mode: str) -> dict:
    with pytest.MonkeyPatch.context() as patch:
        home = tmp_path / "home"
        home.mkdir()
        patch.setenv("HOME", str(home))
        patch.setenv("USERPROFILE", str(home))
        patch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
        patch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
        patch.delenv("MCC_CONFIG_DIR", raising=False)
        patch.delenv("MCC_ENV_FILE", raising=False)
        world = _get_started_world(tmp_path, raw_logs_on=raw_logs_on)
    return _run(
        MCC_JSDOM_SCENARIO="get_started",
        MCC_JSDOM_GET_STARTED_FILE=str(world),
        MCC_JSDOM_GET_STARTED_MODE=mode,
    )


@pytest.fixture(scope="module")
def get_started(tmp_path_factory) -> dict:
    return _get_started_run(
        tmp_path_factory.mktemp("get_started"), raw_logs_on=False, mode="full"
    )


@pytest.fixture(scope="module")
def get_started_raw(tmp_path_factory) -> dict:
    return _get_started_run(
        tmp_path_factory.mktemp("get_started_raw"), raw_logs_on=True, mode="raw"
    )


def test_get_started_scenario_runs_clean(get_started) -> None:
    assert get_started["fatal"] is None
    assert get_started["scenarioFatal"] is None
    assert get_started["scriptErrors"] == []
    assert get_started["consoleErrors"] == []
    assert get_started["baseTree"] is False


def test_get_started_draws_four_essentials_and_every_card(get_started) -> None:
    fresh = get_started["fresh"]

    assert [step["id"] for step in fresh["steps"]] == [
        "provider",
        "models",
        "client",
        "analytics",
    ]
    assert fresh["progress"] == "0 of 4 essentials done"
    assert [step["label"] for step in fresh["steps"]] == [
        "Add a model provider",
        "Choose a model for each tier",
        "Connect a coding agent",
        "Check it worked — see your first request in Analytics",
    ]
    # The one expanded is the first unfinished essential.
    assert [step["expanded"] for step in fresh["steps"]] == [True, False, False, False]
    assert get_started["cardCount"] == get_started["mainCards"] == 9
    seen = {card["id"]: card for card in fresh["cards"]}
    assert set(seen) == {
        "fallback_chains",
        "tiers_per_agent",
        "reasoning",
        "web_search",
        "images",
        "media_tools",
        "analytics_cost",
        "several_keys",
        "proxying",
    }
    for card in seen.values():
        assert card["title"], card
        assert card["line"], card
        assert card["opens"], card
    # The one collapsed niche list, and the one collapsed advanced section.
    assert fresh["moreDisclosureOpen"] is False
    assert fresh["advancedOpen"] is False
    assert fresh["moreCount"] == get_started["moreRows"] == 22
    # The lead is one short paragraph, and the long essay is folded away.
    assert len(fresh["lead"].split()) < 70
    assert fresh["about"].startswith("Server or desktop app?")


def test_get_started_statuses_come_from_the_loaded_payload(get_started) -> None:
    cards = {card["id"]: card for card in get_started["fresh"]["cards"]}

    assert cards["fallback_chains"]["pill"] == "In use · 1 chain"
    assert cards["fallback_chains"]["state"] == "in_use"
    assert cards["tiers_per_agent"]["pill"] == "Custom for 1 agent"
    assert cards["reasoning"]["pill"] == "Default"
    assert cards["web_search"]["pill"] == "Ready · DuckDuckGo, no key"
    assert cards["web_search"]["state"] == "ready"
    assert cards["images"]["pill"] == "Vision adapter: llama-4-scout"
    assert cards["media_tools"]["pill"] == "1 of 4 rails"
    assert cards["media_tools"]["detail"] == "Tools in 2 agents"
    assert cards["analytics_cost"]["pill"] == "No requests yet"
    assert cards["several_keys"]["pill"] == "1 provider with 2+ keys"
    assert cards["proxying"]["state"] == "not_checked"


def test_get_started_essential_3_is_done_for_a_launcher_user(get_started) -> None:
    """U2: mcc-claude sent five requests and no settings.json points here."""

    steps = {step["id"]: step for step in get_started["launcher"]["steps"]}

    assert steps["client"]["pill"] == "Done"
    assert steps["client"]["state"] == "done"
    assert (
        steps["client"]["line"]
        == "Connected this week: Claude Code (5) · Codex CLI (1)"
    )
    assert steps["analytics"]["pill"] == "Done"
    assert "Last request" in steps["analytics"]["line"]
    assert "answered by fake-small" in steps["analytics"]["line"]


def test_get_started_essential_3_is_done_from_a_wsl_settings_file(get_started) -> None:
    steps = {step["id"]: step for step in get_started["wsl"]["steps"]}

    assert steps["client"]["pill"] == "Done"
    # Nothing is connected this week, and the line does not pretend otherwise.
    assert steps["client"]["line"] == ""


def test_get_started_step_4_is_not_measured_when_logging_is_off(get_started) -> None:
    steps = {step["id"]: step for step in get_started["logOff"]["steps"]}

    assert steps["analytics"]["pill"] == "Not measured"
    assert steps["analytics"]["state"] == "not_measured"
    # Three essentials are counted, not four: the unmeasurable one is out.
    assert get_started["logOff"]["progress"] == "1 of 3 essentials done"
    assert any(
        "Request logging is off" in text
        for text in get_started["logOff"]["analyticsBody"]
    )


def test_get_started_renders_a_payload_with_no_steps(get_started) -> None:
    empty = get_started["empty"]

    assert empty["progressHidden"] is True
    assert empty["steps"] == 0
    # The cards are the page's own: they render whatever the server sent.
    assert empty["cards"] == 9
    assert empty["scriptErrors"] == []


def test_get_started_filter_matches_titles_and_one_liners_only(get_started) -> None:
    frames = {frame["query"]: frame for frame in get_started["filter"]["frames"]}

    assert frames[""]["count"] == "22 of 22"
    assert frames["cache"]["rows"] == ["prompt_cache_lifetime"]
    assert frames["cache"]["count"] == "1 of 22"
    assert frames["cache"]["empty"]["hidden"] is True
    assert "Nothing matches" in frames["rotation"]["empty"]["text"]
    # A word that appears only in a control's label, never in the row's copy,
    # finds nothing -- the lesson of the Web Search find box (U6).
    assert frames["rotation"]["rows"] == []
    assert frames["zzz-nothing"]["rows"] == []
    assert get_started["filter"]["count"] == "22 of 22"


def test_get_started_advanced_section_is_read_only_and_shows_current_values(
    get_started,
) -> None:
    advanced = get_started["advanced"]

    assert advanced["open"] is True
    groups = {group["key"]: group for group in advanced["groups"]}
    assert set(groups) == {"limits", "raw", "testing"}
    assert len(groups["limits"]["rows"]) == 8
    assert len(groups["raw"]["rows"]) == 9
    assert len(groups["testing"]["rows"]) == 11
    deadlines = next(
        row for row in groups["limits"]["rows"] if row["id"] == "deadlines"
    )
    assert [chip["key"] for chip in deadlines["values"]] == [
        "FALLBACK_FIRST_TOKEN_TIMEOUT",
        "FALLBACK_TOTAL_TIMEOUT",
        "FALLBACK_STALL_TIMEOUT",
    ]
    assert len(deadlines["opens"]) == 1
    # Every row names the setting's current value or its default, never a
    # value MCC would like the reader to pick.
    assert all(
        chip["text"]
        for group in groups.values()
        for row in group["rows"]
        for chip in row["values"]
    )


def test_get_started_does_not_show_a_suggested_value(get_started) -> None:
    """The advanced rows never recommend a value (decision 4)."""

    rows = [row for group in get_started["advanced"]["groups"] for row in group["rows"]]
    for row in rows:
        text = (
            f"{row['title']} {' '.join(chip['text'] for chip in row['values'])}".lower()
        )
        for nudge in ("set it to", "recommended", "we suggest", "you should"):
            assert nudge not in text, (row["id"], nudge)


def test_get_started_raw_warning_only_when_a_switch_is_on(
    get_started, get_started_raw
) -> None:
    assert get_started["fresh"]["rawWarning"] == {"present": False}

    warning = get_started_raw["rawOnly"]["warning"]
    assert warning["present"] is True
    assert warning["count"] == "1"
    assert "Raw logs are ON (1)" in warning["text"]
    assert "prompt text" in warning["text"]
    landed = get_started_raw["rawOnly"]["landed"]
    assert landed["dataKey"] == "LOG_RAW_SSE_EVENTS"
    assert landed["view"] == "limits"
    assert landed["hidden"] is False
    assert "onboarding-highlight" in landed["className"]


def test_get_started_every_open_button_lands_on_its_target(get_started) -> None:
    opens = get_started["opens"]

    assert get_started["openButtonCount"] >= 22
    assert [entry["key"] for entry in opens if not entry["found"]] == []
    assert [entry["key"] for entry in opens if not entry["isTarget"]] == []
    assert [entry["key"] for entry in opens if entry["inCollapsedFold"]] == []
    assert [entry["key"] for entry in opens if entry["element"]["hidden"]] == []


def test_get_started_not_for_me_round_trips_through_the_server(get_started) -> None:
    hidden = get_started["hidden"]

    assert "fallback_chains" in hidden["before"]
    assert "fallback_chains" not in hidden["afterHide"]
    assert hidden["afterShow"] == hidden["before"]
    assert hidden["showHiddenLabelSeen"] == "Show hidden (1)"
    # The page sent the server a skipped list, and kept nothing locally.
    assert {"skipped": ["fallback_chains"]} in hidden["posts"]
    assert {"skipped": []} in hidden["posts"]
    # Nothing about the hidden list is kept in the browser: the only place it
    # lives is the server's onboarding file (decision 5).
    local = [key for key in get_started["localStorageKeys"] if "started" in key.lower()]
    assert local == []
    assert [
        key for key in get_started["localStorageKeys"] if "skipped" in key.lower()
    ] == []


def test_get_started_dismiss_has_an_undo(get_started) -> None:
    dismiss = get_started["dismiss"]

    assert dismiss["dismissedFooter"]["dismiss"] == "Undo dismiss"
    assert dismiss["dismissedFooter"]["dismissed"] == "true"
    assert "Undo brings it back" in dismiss["dismissedFooter"]["message"]
    assert dismiss["dismissedFooter"]["messageHidden"] is False
    assert dismiss["undoneFooter"]["dismiss"] == "Don't open this on start"
    assert dismiss["undoneFooter"]["dismissed"] == "false"
    assert {"dismissed": True} in dismiss["posts"]
    assert {"dismissed": False} in dismiss["posts"]


def test_get_started_guide_link_opens_the_guide_and_records_the_visit(
    get_started,
) -> None:
    link = get_started["guideLink"]

    assert link["guideVisible"] is True
    assert link["purpose"]
    assert link["anchor"].startswith("guide-")
    assert link["visitedPosted"] is True


def _declared_opens_by_row() -> dict:
    """The catalogue's own table: row id -> the targets it declares, in order."""
    from tests.contracts.test_get_started_catalogue import _entries

    table = {}
    for entry in _entries():
        keys = []
        for target in entry["opens"]:
            kind, value = target.get("kind"), target.get("value")
            keys.append(f"{target['view']}:{kind}:{value}" if kind else target["view"])
        table[entry["id"]] = keys
    return table


def test_get_started_every_niche_row_is_a_door(get_started) -> None:
    """The user's 2026-10-31 feedback: every row of "More things MCC can do"
    carries a button that takes the reader to its page AND card, says where it
    goes, and (decision 5) a "Not for me".

    The table below is read from the page's own catalogue, so a row that lost
    its button, changed its target or has no hide control fails here.
    """

    declared = _declared_opens_by_row()
    rows = {row["id"]: row for row in get_started["filter"]["rows"]}

    assert len(rows) == 22
    wrong = []
    for row_id, row in rows.items():
        if not row["opens"]:
            wrong.append(f"{row_id}: no Open button")
            continue
        if row["hide"] != "Not for me":
            wrong.append(f"{row_id}: no Not for me ({row['hide']!r})")
        got = [button["key"] for button in row["opens"]]
        want = declared[row_id]
        if got != want:
            wrong.append(f"{row_id}: targets {got} != declared {want}")
        for button in row["opens"]:
            label = button["label"]
            if not label.startswith("Open "):
                wrong.append(f"{row_id}: label {label!r} does not open")
            elif want and " → " not in label and any(":" in key for key in want[:1]):
                wrong.append(f"{row_id}: label {label!r} does not name the card")
    assert wrong == []


def test_get_started_every_advanced_row_is_a_door_too(get_started) -> None:
    rows = [row for group in get_started["advanced"]["groups"] for row in group["rows"]]
    declared = _declared_opens_by_row()

    assert len(rows) == 28
    assert [
        row["id"]
        for row in rows
        if [button["key"] for button in row["opens"]] != declared[row["id"]]
    ] == []
    assert [row["id"] for row in rows if row["hide"] != "Not for me"] == []
    assert [
        f"{row['id']}: {button['label']!r}"
        for row in rows
        for button in row["opens"]
        if not button["label"].startswith("Open ")
    ] == []


def test_get_started_never_writes_a_setting(get_started, get_started_raw) -> None:
    """This page reads and links; it never calls the settings save."""

    assert get_started["applyCalls"] == 0
    assert get_started["applyWrites"] == []
    assert get_started_raw["applyCalls"] == 0


def test_get_started_leaves_every_other_view_as_the_base_tree_drew_it(
    get_started,
) -> None:
    """The base-written baseline: the same scenario on the BASE tree 12357c96.

    Only Get Started is that release's subject, so its view is excluded; every
    other view -- the Docs included -- is byte-identical to the page the base
    tree drew for the same payload. 7.122.2 rewords the help in the Guide and
    on the Requests page (see :data:`RELABELLED_VIEWS`), so those two are
    excluded here as well and checked directly.

    Since 7.122.3 the pass restructures views by design (:data:`UX_CHANGED_VIEWS`)
    and changes the words every settings control wears
    (:data:`UX_SETTINGS_ONLY_VIEWS`). Both are excluded here; the second set is
    compared, with those display-only changes normalised out, against this
    pass's own base-written baseline in ``test_admin_ux_pass_jsdom.py``. This
    test asserts nothing else moved.
    """

    baseline = json.loads(GET_STARTED_BASELINE.read_text(encoding="utf-8"))
    assert "12357c96" in baseline["written_by"]
    base = baseline["views"]

    mine = dict(get_started["viewsOnLoad"])
    excluded = (
        RELABELLED_VIEWS
        | UX_CHANGED_VIEWS
        | UX_SETTINGS_ONLY_VIEWS
        | {GET_STARTED_VIEW}
    )
    base_without = {key: value for key, value in base.items() if key not in excluded}
    mine_without = {key: value for key, value in mine.items() if key not in excluded}
    assert set(base_without) == set(mine_without)
    moved = sorted(
        view for view, digest in base_without.items() if mine_without[view] != digest
    )
    assert moved == []
    # And Get Started really did change (the baseline is not vacuous).
    assert mine[GET_STARTED_VIEW] != base[GET_STARTED_VIEW]


#: Every view this pass changes on purpose, and why. The test above proves
#: nothing outside this set drifted; each entry is checked by its own test in
#: the ux_pass scenario below.
UX_CHANGED_VIEWS = frozenset(
    {
        "requests",  # the Analytics canvas: order, cost tile, charts
        "providers",  # U16's rail and the heading ids it points at
        "proxying",  # U15's rail, chains above sources, setting links
        "coding_agents",  # U17: retired command rows are not drawn
        "models",  # U23: four buttons get the class the stylesheet carries
        "optimizer",  # U26's sentence and the RTK cross-link
        "web_search",  # U6's count line and curated haystack
        "claude",  # U4's token wording
        "guide",  # U4, the retired-command paragraph, the migration note
    }
)

#: Views the settings-wide display changes reach, which the Get Started
#: baseline above can only compare raw. ``test_ux_pass_leaves_every_other_view``
#: compares them against this pass's own base-written baseline with those
#: changes normalised out, which is the stronger check for them.
UX_SETTINGS_ONLY_VIEWS = frozenset({"limits", "messaging", "model_config"})
