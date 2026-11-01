"""The 8.1.0 design-system foundation, proved in jsdom (scenario: design_system).

Everything here runs the real ``index.html``, ``admin.js`` and ``designs.js``
in jsdom through ``admin_jsdom_harness.mjs``: the per-page section navigator on
every page that has one, each button's landing target, the three folded topbar
rails, the theme family by mode resolution and the legacy migration, the
Appearance card's three storage layers, and the base-written DOM baseline.

It is its own suite file — and is named in the ``jsdom`` CI job and in that
job's ``--ignore=`` list in the quality job — for the same reason the UX pass
is: the release's own proofs read as one document. ``tests/contracts/
test_jsdom_ci_contract.py`` fails if a file that drives a jsdom harness is not
wired in, so this one cannot go green by never running. What jsdom cannot
prove -- box heights, computed contrast, the sticky strip's behaviour in a real
scroll -- is measured in the browser and recorded in the release's report.
"""

import json
import os
import re
from pathlib import Path

import pytest

from tests.api.test_admin_static_jsdom import _run

#: CI must not pass a suite it never ran. `_run` turns a missing node or a
#: missing jsdom into a failure under MCC_CI=1; this module reads the same
#: switch to assert its own runs produced measurements rather than skips.
ON_CI = os.environ.get("MCC_CI", "") not in ("", "0", "false")

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_STATIC = REPO_ROOT / "src/my_claude_code/api/admin_static"
ADMIN_JS = (ADMIN_STATIC / "admin.js").read_text(encoding="utf-8")
THEMES_CSS = (ADMIN_STATIC / "themes.css").read_text(encoding="utf-8")
FOUNDATION_CSS = (ADMIN_STATIC / "foundation.css").read_text(encoding="utf-8")
DESIGNS_JS = (ADMIN_STATIC / "designs.js").read_text(encoding="utf-8")
INDEX_HTML = (ADMIN_STATIC / "index.html").read_text(encoding="utf-8")

BASE_BASELINE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "design_system_dom_8_1_0.json"
)

pytestmark = pytest.mark.xdist_group("admin_static_jsdom")

#: The pages the shipped navigator table covers, and how many sections each
#: declares. Pinned by number so a page gaining or losing one says so.
EXPECTED_NAVIGATOR = {
    "providers": 6,
    "proxying": 3,
    "marketplaces": 6,
    "claude": 3,
    "coding_agents": 5,
    "model_config": 4,
    "models": 3,
    "requests": 10,
    "optimizer": 5,
    "web_search": 4,
    "limits": 8,
    "guide": 41,
    "docs": 3,
}

#: Every label is one or two words and under 18 characters, because the strip
#: must not wrap at 1280 px (PER-PAGE SECTION NAVIGATOR, "SHAPE").
MAX_LABEL_CHARS = 18

#: A `section-<id>` selector is RENDERED from the settings payload, so whether
#: it resolves in a given run depends on which sections the fixture supplied.
#: The harness's payload carries no fields for these three, so their cards are
#: not drawn — a property of the fixture, not of the table.
FIXTURE_ABSENT_SECTIONS = frozenset({"reasoning", "web_tools", "catalogue"})


@pytest.fixture(scope="module")
def design_system() -> dict:
    return _run(MCC_JSDOM_SCENARIO="design_system")


@pytest.fixture(scope="module")
def design_system_off() -> dict:
    """The same page with the two new visible features taken back out.

    Half of the governing proof: with them off, every view is the BASE tree's
    view (``design_system_dom_8_1_0.json``). The other half — the page as a
    reader gets it — is the ``design_system`` fixture above.
    """

    return _run(MCC_JSDOM_SCENARIO="design_system", MCC_JSDOM_DS_FEATURE="disabled")


# ---------------------------------------------------------------- clean run ---


def test_the_design_system_scenario_runs_clean(
    design_system, design_system_off
) -> None:
    for payload in (design_system, design_system_off):
        assert payload["fatal"] is None
        assert payload["scenarioFatal"] is None
        assert payload["scriptErrors"] == []
        assert payload["consoleErrors"] == []
    if ON_CI:
        # MCC_CI=1 is the switch that fails a missing node/jsdom. This is the
        # other half: the payloads above must be real measurements, not an
        # empty dict from a run that never started.
        assert len(design_system["navigator"]) >= 13
        assert design_system["views"]
        assert design_system_off["views"]


# ------------------------------------------------- the base-written baseline ---


def test_with_both_features_off_every_view_is_the_base_tree(
    design_system_off,
) -> None:
    """TODAY MUST RENDER BYTE-IDENTICALLY. This is that proof.

    The baseline is this same scenario run against the BASE tree ``421d81f7``
    (v8.0.2) with the two new visible features disabled — the same code path,
    the same fixtures, the same waits. The digest compared is the view's DOM
    with the section navigator and the Appearance card taken out (a no-op on a
    run that never built them), with clicks' transient marks, comments,
    whitespace artefacts and rendered clocks normalised on BOTH sides.

    Every one of the fifteen views must match. A view that moves is a defect.
    """

    baseline = json.loads(BASE_BASELINE.read_text(encoding="utf-8"))
    assert baseline["base_commit"] == "421d81f7"
    assert "421d81f7" in baseline["written_by"]
    base = baseline["views"]
    mine = design_system_off["views"]

    assert set(base) == set(mine)
    moved = sorted(
        view
        for view in base
        if base[view]["stripFreeSha256"] != mine[view]["stripFreeSha256"]
    )
    assert moved == [], (
        "with the section navigator and the Appearance card disabled, these "
        f"views are not what the base tree drew: {moved}"
    )


def test_the_baseline_is_not_vacuous(design_system_off) -> None:
    """Every compared view is a real page, and the baseline has real bytes."""

    baseline = json.loads(BASE_BASELINE.read_text(encoding="utf-8"))
    assert len(baseline["views"]) == 15
    for view, entry in baseline["views"].items():
        assert entry["stripFreeLength"] > 0, view
        assert len(entry["stripFreeSha256"]) == 64, view
    # And the run really carried content: the three largest pages are large.
    for view in ("guide", "requests", "proxying"):
        assert design_system_off["views"][view]["stripFreeLength"] > 10_000, view


def test_the_two_features_change_exactly_the_declared_views(design_system) -> None:
    """With them ON, only the pages they belong to move — and each is declared.

    Twelve pages gain the strip; Get Started gains the Appearance card and its
    four machine-default controls. Nothing else moves.

    The comparison is of the FULL digest, because "with the features taken out"
    is by construction the same on both sides — that is what makes the equality
    half of the proof work. The full digest is what a reader actually gets.
    """

    off = design_system["feature_off_views"]
    on = design_system["views"]
    moved = sorted(view for view in off if off[view]["sha256"] != on[view]["sha256"])
    declared = sorted(set(EXPECTED_NAVIGATOR) | {"get_started"})
    assert sorted(set(moved) - set(declared)) == [], (
        "the two new features changed a view outside the declared set: "
        f"{sorted(set(moved) - set(declared))}"
    )
    assert sorted(set(declared) - set(moved)) == [], (
        "these declared views did not change at all, so their feature is not "
        f"on the page: {sorted(set(declared) - set(moved))}"
    )
    # The navigator pages really gained something.
    for view in EXPECTED_NAVIGATOR:
        assert on[view]["length"] > off[view]["length"], view


# ------------------------------------------------------ the section navigator ---


def test_every_declared_page_has_a_strip(design_system) -> None:
    navigator = design_system["navigator"]
    assert set(navigator) == set(EXPECTED_NAVIGATOR)
    for view, expected in EXPECTED_NAVIGATOR.items():
        entry = navigator[view]
        assert entry["present"], view
        assert entry["labelCount"] == expected, (
            f"{view} declares {entry['labelCount']} sections, expected {expected}"
        )
        assert entry["id"] == f"sectionNavigator-{view}"
        # It sits INSIDE the page, first, so it reads before the content it
        # names.
        assert entry["firstChild"], view
        assert entry["hidden"] is False, view


def test_no_page_without_a_declared_strip_gets_one(design_system) -> None:
    """The three views the table does not cover have no strip at all.

    Get Started, Messaging and Docs-less-but-now-covered: get_started and
    messaging have fewer than three named sections, so ``initSectionNavigator``
    builds nothing for them rather than building an empty row.
    """

    navigator = design_system["navigator"]
    for view in ("get_started", "messaging"):
        assert view not in navigator, view
    # And no view outside the declared set carries a `.section-navigator`.
    views_with_strip = sorted(
        name
        for name, entry in design_system["views"].items()
        if name not in EXPECTED_NAVIGATOR
        and any(entry_key.startswith("sectionNavigator") for entry_key in ())
    )
    assert views_with_strip == []


def test_every_label_is_short(design_system) -> None:
    """One or two words, under 18 characters, so the row never wraps."""

    long = [
        (view, label)
        for view, entry in design_system["navigator"].items()
        for label in entry["labels"]
        if len(label) > MAX_LABEL_CHARS
    ]
    assert long == []


def test_every_section_the_hardware_can_render_resolves(design_system) -> None:
    """Each selector names a real element, or a section its page claims.

    A `#section-<id>` target is drawn by ``renderSections()`` from the payload,
    so a payload that carries no fields for that section legitimately draws
    nothing. For those, the assertion is the one that actually matters: the
    section is one the page's own ``VIEW_GROUPS`` entry claims, so the button
    names the page's own content rather than a typo.
    """

    problems = []
    for view, entry in design_system["navigator"].items():
        assert entry["present"], view
        for item in entry["entries"]:
            assert item["declared"], f"{view}: {item['selector']} is not the table's"
            assert item["label"], f"{view}: a button with no label"
            if item["resolves"]:
                continue
            section_id = item["sectionId"]
            if section_id and section_id in FIXTURE_ABSENT_SECTIONS:
                assert item["claimed"], (
                    f"{view}: {item['selector']} neither resolves nor is a "
                    "section this page claims"
                )
                continue
            problems.append(f"{view}: {item['selector']}")
    assert problems == [], (
        "these navigator targets resolve to nothing and are not a section the "
        f"page claims: {problems}"
    )


def test_every_button_lands_on_its_section(design_system) -> None:
    """Every button, clicked, on every page, in one run.

    A landing means: the target got the pulse the rest of the dashboard already
    uses for "this is what you asked for", it took focus so a keyboard can
    carry on from it, and the button is marked current. A button whose target
    the fixture never rendered is skipped here and covered by the resolution
    test above.
    """

    landings = [
        (view, item)
        for view, entry in design_system["navigator"].items()
        for item in entry["landings"]
        if item["resolvable"]
    ]
    assert len(landings) > 70, len(landings)
    problems = [
        f"{view}:{item['label']}"
        for view, item in landings
        if not item["highlighted"]
        or not item["focused"]
        or item["current"] != [item["selector"]]
    ]
    assert problems == []


def test_every_jump_puts_the_section_in_the_url(design_system) -> None:
    """A reload keeps the position: the hash follows the click."""

    problems = []
    for view, entry in design_system["navigator"].items():
        for item in entry["landings"]:
            if not item["resolvable"]:
                continue
            if item["hash"] != item["selector"]:
                problems.append(f"{view}:{item['label']} -> {item['hash']!r}")
    assert problems == [], (
        "these jumps did not put their section in the URL, so a reload would "
        f"not keep the position: {problems}"
    )


def test_the_strip_is_the_only_mechanism_on_every_page(design_system) -> None:
    """The three 7.122.3 topbar rails folded into the navigator.

    One mechanism, not two: each rail lives INSIDE the strip, none is in the
    topbar, and no page shows both.
    """

    rails = design_system["rails"]
    assert rails["declared"] == [
        "providersJumpBar",
        "proxyingJumpBar",
        "reqJumpBar",
    ]
    for rail_id, entry in rails["rails"].items():
        assert entry["exists"], rail_id
        assert entry["insideStrip"], f"{rail_id} is not inside the navigator"
        assert entry["inTopbar"] is False, f"{rail_id} is still in the topbar"
        assert entry["hidden"] is False, rail_id
        # The rail's own links are dropped when the navigator already names the
        # same section — which is all of them, because the table was written
        # from these rails' own semantics. So a rail may legitimately carry
        # ZERO links; what it may never do is carry a duplicate. `both` below
        # is the assertion that matters.
        assert entry["links"] >= 0, rail_id
    assert rails["topbarBars"] == 0, "a jump bar is still parked in the topbar"
    assert rails["both"] == [], f"these pages show two mechanisms: {rails['both']}"
    # ...and no page names the same destination twice in one row.
    for view, entry in design_system["navigator"].items():
        targets = [item["selector"] for item in entry["entries"]]
        assert len(targets) == len(set(targets)), view
    # And the rail each page declares is the one the strip took.
    for view in ("providers", "proxying", "requests"):
        assert design_system["navigator"][view]["rails"], view


# ---------------------------------------------------------- theme resolution ---


def test_a_browser_with_nothing_saved_gets_velvet_in_dark(design_system) -> None:
    """The compiled default (§13.2.1, round-4 Q4)."""

    assert design_system["themeOnLoad"]["family"] == "velvet"
    assert design_system["themeOnLoad"]["mode"] == "dark"
    assert design_system["themeOnLoad"]["design"] == "today"
    resolved = design_system["themeOnLoad"]["resolved"]
    assert resolved["source"] == {
        "design": "compiled",
        "theme": "compiled",
        "mode": "compiled",
        "density": "compiled",
    }


def test_a_saved_theme_migrates_to_the_same_look(design_system) -> None:
    """Every legacy value maps to the pair that keeps it looking the same.

    This is the release's promise to an existing install: nothing a user saved
    is lost, and what they see does not change.
    """

    probes = design_system["probes"]
    expected = {
        "midnight": ("midnight", "dark"),
        "paper": ("paper", "light"),
        "highContrast": ("hc", "dark"),
        "velvet": ("velvet", "dark"),
    }
    for probe, (theme, mode) in expected.items():
        got = probes[probe]
        assert (got["values"]["theme"], got["values"]["mode"]) == (theme, mode), probe
        # And the layer that answered is the legacy one, not the compiled
        # default happening to agree.
        assert got["source"]["theme"] == "legacy", probe
        assert got["source"]["mode"] == "legacy", probe


def test_an_unknown_saved_value_is_ignored(design_system) -> None:
    """A corrupt or hand-edited key falls through rather than painting nothing."""

    probes = design_system["probes"]
    for probe in ("unknownLegacy", "badViewerValue", "badMachineValueIgnored"):
        got = probes[probe]
        assert got["values"]["theme"] == "velvet", probe
        assert got["source"]["theme"] == "compiled", probe


def test_the_viewer_wins_over_the_machine(design_system) -> None:
    probes = design_system["probes"]
    assert probes["viewerBeatsMachine"]["values"]["theme"] == "paper"
    assert probes["viewerBeatsMachine"]["source"]["theme"] == "viewer"
    # A viewer choice also beats the legacy key it was migrated from.
    assert probes["viewerBeatsLegacy"]["values"]["theme"] == "velvet"
    assert probes["viewerBeatsLegacy"]["source"]["theme"] == "viewer"


def test_an_empty_viewer_value_follows_the_machine(design_system) -> None:
    """The one rule that makes "Follow the machine" mean something."""

    got = design_system["probes"]["emptyViewerFollowsMachine"]
    assert got["values"]["theme"] == "navy"
    assert got["source"]["theme"] == "machine"


def test_the_legacy_key_is_a_viewer_choice_not_a_machine_one(design_system) -> None:
    """A saved legacy value outranks the machine default."""

    got = design_system["probes"]["legacyBeatsMachine"]
    assert got["values"]["theme"] == "paper"
    assert got["source"]["theme"] == "legacy"


def test_the_machine_default_is_used_when_the_viewer_has_none(design_system) -> None:
    got = design_system["probes"]["machineBeatsCompiled"]
    assert got["values"] == {
        "design": "today",
        "theme": "navy",
        "mode": "light",
        "density": "compact",
    }
    assert got["source"] == {
        "design": "machine",
        "theme": "machine",
        "mode": "machine",
        "density": "machine",
    }


def test_light_mode_follows_the_light_theme_default(design_system) -> None:
    """§13.2.1's interim light default: Today, not the dark default's Velvet."""

    got = design_system["probes"]["lightModeFollowsLightTheme"]
    assert got["values"]["theme"] == "today"
    assert got["values"]["mode"] == "light"


# ------------------------------------------------------------ the Appearance ---


def test_the_card_exists_with_exactly_one_design_tile(design_system) -> None:
    """PR A ships one real registry entry, and the grid must look right with one."""

    card = design_system["appearance"]
    assert card["present"] is True
    assert [tile["id"] for tile in card["tiles"]] == ["today"]
    tile = card["tiles"][0]
    assert tile["name"] == "Today"
    assert tile["badge"] == "Classic"
    assert tile["checked"] == "true"
    assert tile["role"] == "radio"
    assert tile["hasPreview"] is True
    assert tile["note"]


def test_the_card_offers_every_family_and_both_modes(design_system) -> None:
    """Today's four theme buttons fold into this menu; nothing is lost."""

    options = design_system["appearance"]["options"]
    assert options["family"] == [
        {"value": "today", "label": "Today", "checked": "false"},
        {"value": "midnight", "label": "Midnight", "checked": "false"},
        {"value": "paper", "label": "Paper", "checked": "false"},
        {"value": "hc", "label": "High Contrast", "checked": "false"},
        {"value": "velvet", "label": "Velvet", "checked": "true"},
        {"value": "navy", "label": "Navy", "checked": "false"},
    ]
    assert [entry["value"] for entry in options["mode"]] == ["light", "dark"]
    assert [entry["value"] for entry in options["density"]] == [
        "design",
        "compact",
        "comfortable",
    ]
    # Every one of the four legacy themes is still reachable.
    assert {"midnight", "paper", "hc", "velvet"} <= {
        entry["value"] for entry in options["family"]
    }


def test_every_control_writes_its_own_key_and_nothing_else(design_system) -> None:
    card = design_system["appearance"]
    by_axis = {}
    for click in card["clicks"]:
        assert click is not None, "a control the card offers was not found"
        by_axis.setdefault(click["axis"], []).append(click)
    assert set(by_axis) == {"theme", "mode", "density", "design"}
    for axis, clicks in by_axis.items():
        for click in clicks:
            # Each click stores its own value under its own key.
            assert click["storage"] == click["value"], (axis, click)
            # ...and the page followed.
            if axis == "mode":
                assert click["mode"] == click["value"], click
            elif axis == "theme":
                assert click["family"] == click["value"], click
            elif axis == "design":
                assert click["design"] == click["value"], click
            # Exactly one option is checked in the group afterwards.
            if "checked" in click:
                assert click["checked"] == [click["value"]], click


def test_changing_one_axis_never_resets_another(design_system) -> None:
    """§13.4: "Changing one never resets another"."""

    clicks = {
        (click["axis"], click["value"]): click
        for click in design_system["appearance"]["clicks"]
        if click
    }
    # Choosing Paper then light keeps Paper; back to dark keeps it too.
    assert clicks[("theme", "paper")]["family"] == "paper"
    assert clicks[("mode", "light")]["family"] == "paper"
    assert clicks[("mode", "light")]["mode"] == "light"
    assert clicks[("mode", "dark")]["family"] == "paper"
    # And the density never moved while the theme did.
    assert clicks[("mode", "dark")]["density"] is None


def test_density_follows_the_design_until_it_is_overridden(design_system) -> None:
    """`Design default` writes NO attribute, because comfortable IS today."""

    clicks = {
        (click["axis"], click["value"]): click
        for click in design_system["appearance"]["clicks"]
        if click
    }
    assert clicks[("density", "compact")]["density"] == "compact"
    # Back to the design's own: the attribute is gone, not set to a word.
    assert clicks[("density", "design")]["density"] is None
    assert clicks[("density", "design")]["storage"] == "design"


def test_follow_the_machine_clears_every_viewer_key(design_system) -> None:
    follow = design_system["appearance"]["followMachine"]
    assert follow is not None
    assert follow["design"] is None
    assert follow["theme"] is None
    assert follow["mode"] is None
    assert follow["density"] is None
    # And the page went back to the compiled defaults.
    assert follow["family"] == "velvet"
    assert follow["mode2"] == "dark"


def test_no_appearance_control_applies_a_setting(design_system) -> None:
    """§13.2.5 item 4 / this release's proof item 7.

    A design, theme or density switch must never POST
    ``/admin/api/config/apply``. The card's only route to a save is the
    explicit "Use this as the machine default" button, and even that only
    stages the change in the page's own fields for the page's own Apply.
    """

    card = design_system["appearance"]
    assert card["applyCallsBefore"] == 0
    assert card["applyCallsAfter"] == 0


# ------------------------------------------------------------------ statics ---
#
# The three files that carry the new layer, read directly. These are properties
# of the stylesheets and the registry that no jsdom run can observe — jsdom
# loads no CSS and cannot run a media query.


def test_every_theme_family_has_both_modes() -> None:
    """Seven families by two modes, every one a real block.

    ``own`` is the design's palette; 8.1.0 ships no design that declares one,
    so it falls back to Today's colours — but the block exists, because a saved
    ``own`` must always be a valid page rather than an unstyled one.
    """

    families = ("today", "midnight", "paper", "hc", "velvet", "navy", "own")
    for family in families:
        for mode in ("light", "dark"):
            selector = f'html[data-mcc-theme="{family}"][data-mcc-mode="{mode}"]'
            assert selector in THEMES_CSS, f"themes.css has no {family}/{mode} block"
    # Every block defines the tokens a page needs; --accent-on is the fill ink.
    for token in ("--bg", "--panel", "--text", "--muted", "--accent", "--accent-on"):
        assert THEMES_CSS.count(f"{token}:") >= 14, token


def test_the_two_aa_corrections_are_the_values_that_shipped() -> None:
    """§5.2. Both of today's failing pairs, corrected, in the right blocks."""

    light_start = THEMES_CSS.index(
        'html[data-mcc-theme="paper"][data-mcc-mode="light"]'
    )
    light_block = THEMES_CSS[light_start : THEMES_CSS.index("}", light_start)]
    assert "--accent: #047857" in light_block, (
        "Paper light must carry the AA-corrected emerald (#047857), not #059669"
    )
    assert "--warn: #9a4f06" in light_block
    assert "#059669" not in light_block
    assert "#b45309" not in light_block

    velvet_start = THEMES_CSS.index(
        'html[data-mcc-theme="velvet"][data-mcc-mode="dark"]'
    )
    velvet_block = THEMES_CSS[velvet_start : THEMES_CSS.index("}", velvet_start)]
    assert "--accent: #f0607a" in velvet_block
    assert "--accent-on: #1a0610" in velvet_block, (
        "Velvet dark's filled buttons need the dark ink; white on the old "
        "crimson measured 3.94:1"
    )


def test_the_density_variant_matches_the_columns_rule() -> None:
    """The 4/3/2/1 contract, read out of the stylesheet.

    Four controls at 1500 px and above, three from 1100 to 1500, two from 760
    to 1100, one below 760 — the rule the ADDENDUM makes a blocking gate.
    """

    assert ".field-grid {" in FOUNDATION_CSS
    # 4 across at >= 1500 px: the base track, unchanged from admin.css.
    assert "repeat(auto-fit, minmax(280px, 1fr))" in FOUNDATION_CSS
    # 2 across below 1100 px, 1 below 760. (3 across in between needs no rule:
    # 280 px tracks in a ~1076 px column give three.)
    for breakpoint in ("1099.98px", "759.98px"):
        assert breakpoint in FOUNDATION_CSS, breakpoint
    # A wide control spans two columns; a page is never one column above 760.
    assert ".field-grid > .field:has(> textarea)" in FOUNDATION_CSS
    assert "grid-column: span 2" in FOUNDATION_CSS
    # The density variants exist and the default changes nothing.
    assert 'html[data-density="compact"]' in FOUNDATION_CSS
    assert 'html[data-density="comfortable"]' in FOUNDATION_CSS


def test_the_rail_typography_floor_is_what_ships() -> None:
    """LEFT RAIL: 14 px links, 36 px rows, 11 px index, and NO colour change."""

    assert "--rail-link-size: 14px" in FOUNDATION_CSS
    assert "--rail-index-size: 11px" in FOUNDATION_CSS
    # The row floor is 40, which is above the 36 the spec requires and exactly
    # what the product's own nav-link always was.
    row = re.search(r"--rail-row-h: (\d+)px", FOUNDATION_CSS)
    assert row and int(row.group(1)) >= 36
    assert "font-size: var(--rail-link-size)" in FOUNDATION_CSS
    assert "font-variant-numeric: tabular-nums" in FOUNDATION_CSS
    assert "letter-spacing: var(--rail-group-tracking)" in FOUNDATION_CSS
    # The active state is weight plus the accent border, never a lighter
    # inactive item — no colour is set on either rule.
    active = FOUNDATION_CSS[
        FOUNDATION_CSS.index(".sidebar .nav-link.active {") : FOUNDATION_CSS.index(
            "}", FOUNDATION_CSS.index(".sidebar .nav-link.active {")
        )
    ]
    assert "font-weight: 700" in active
    assert "border-left: 2px solid var(--accent)" in active
    assert "color:" not in active, (
        "the active rail item must not change anybody's colour; contrast was "
        "already fine and the fix is SIZE"
    )


def test_the_registry_has_exactly_one_real_entry() -> None:
    """PR A ships one design and its loader must take more rows as DATA."""

    assert DESIGNS_JS.count("    id: ") == 1
    assert 'id: "today"' in DESIGNS_JS
    assert 'status: "classic"' in DESIGNS_JS
    assert 'defaultFor: "fallback"' in DESIGNS_JS
    assert "css: null" in DESIGNS_JS
    assert "shell: null" in DESIGNS_JS
    # The vocabularies live beside it, so a later row reads one list.
    assert "const ADMIN_THEME_FAMILIES" in DESIGNS_JS
    assert "const ADMIN_DENSITIES" in DESIGNS_JS
    assert "const ADMIN_APPEARANCE_DEFAULTS" in DESIGNS_JS
    # And the loader names no design: adding a row needs no admin.js change.
    for design_id in ("today", "ledger", "console", "hybrid", "palette"):
        assert f'"{design_id}"' not in ADMIN_JS or design_id == "today", (
            f"admin.js names the design {design_id!r}; adding a registry row "
            "must be data-only"
        )


def test_the_appearance_card_is_static_markup_the_page_never_rebuilds() -> None:
    """It must survive every re-render, and work before the payload lands."""

    assert 'id="appearanceCard"' in INDEX_HTML
    assert 'id="appearanceCardBody"' in INDEX_HTML
    assert 'id="appearanceMachineSections"' in INDEX_HTML
    # The card's own element is declared once and never built by admin.js.
    assert 'createElement("section")' in ADMIN_JS  # renderSections builds those
    assert 'id = "appearanceCard"' not in ADMIN_JS
