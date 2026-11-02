"""The design-shell loader and the first real design, proved in jsdom.

Scenario: ``MCC_JSDOM_SCENARIO=design_shell`` (``admin_jsdom_design_shell.mjs``).

`test_jsdom_ci_contract.py` finds the modules that drive a jsdom harness by
looking for the harness's own file name in them, so the name is spelled out in
full here: ``tests/api/admin_jsdom_design_shell.mjs`` — the scenario this file
runs through ``tests/api/admin_jsdom_harness.mjs``. That is also what puts this
module in the ``jsdom`` CI job rather than in the pytest job's ``--ignore``
hole.

8.2.0's S1 is the machinery every later design depends on, so it is proved the
way the requirement is written rather than by inspection:

* **the round trip** — ``today`` → ``hybrid`` → ``today`` leaves the page as it
  began: the chrome outside the app shell, ``<html>``'s design attributes,
  ``<body>``'s classes, the rail's own markup, and every place a shell's names
  could survive are all identical, and a second trip changes nothing more;
* **nothing attached twice** — the masthead, its nodes, the rail's numbers and
  headings and the version indicator all appear exactly once;
* **the product's navigation is untouched** — the rail keeps every link, and
  the per-page section navigator is present once per page, never hidden and
  never duplicated;
* **the palette** — it is a dialog, it finds a setting by name, an Escape closes
  it and returns focus, and a section item switches the page and lands on the
  section;
* **the g-keys** — ``g l`` and ``g r`` navigate, and a ``g`` typed into a field
  does not;
* **a burst of switches** — three switches with nothing awaited in between end
  with exactly one shell: the last one asked for;
* **the failure path** — a shell whose module throws leaves the page working,
  one honest line in the Appearance card, and no residue at all.
"""

import itertools
import json
import os
import re
from pathlib import Path

import pytest

from tests.api.test_admin_static_jsdom import _run

REPO_ROOT = Path(__file__).resolve().parents[2]
ADMIN_STATIC = REPO_ROOT / "src/my_claude_code/api/admin_static"
ADMIN_JS = (ADMIN_STATIC / "admin.js").read_text(encoding="utf-8")
DESIGNS_JS = (ADMIN_STATIC / "designs.js").read_text(encoding="utf-8")
HYBRID_CSS = (ADMIN_STATIC / "designs" / "hybrid.css").read_text(encoding="utf-8")
HYBRID_JS = (ADMIN_STATIC / "designs" / "hybrid.js").read_text(encoding="utf-8")
FOUNDATION_CSS = (ADMIN_STATIC / "foundation.css").read_text(encoding="utf-8")
ADMIN_ROUTES = (REPO_ROOT / "src/my_claude_code/api/admin_routes.py").read_text(
    encoding="utf-8"
)

pytestmark = pytest.mark.xdist_group("admin_static_jsdom")


def _code_only(source: str) -> str:
    """The file's code, with its comments taken out.

    Three assertions below are about what a file DOES. Each of these files
    explains in prose what it must not do — `hybrid.css` names the preview's
    `data-dir`, `hybrid.js` names the admin.js globals it may not touch — and a
    substring test over the whole file would fail on the explanation instead of
    on the code. Comments are stripped first, the way
    `test_admin_design_system_jsdom.py` strips them from the loader it reads.
    """

    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*//.*$", "", without_block)


@pytest.fixture(scope="module")
def shell() -> dict:
    return _run(MCC_JSDOM_SCENARIO="design_shell")


# ------------------------------------------------------------------ clean run ---


def test_the_shell_scenario_runs_clean(shell) -> None:
    """The scenario itself is clean, apart from the ONE line the failure path
    deliberately logs.

    The failure probe makes a shell throw on purpose, and the loader's contract
    is to log exactly one line about it — so `consoleErrors` is expected to hold
    that line and nothing else. Its shape is asserted below, in the failure
    section; here it is only required to be the only one.

    The `MCC_CI` block is not decoration. `_run` fails a missing node or jsdom
    under `MCC_CI=1`, but a run that STARTED and returned nothing would still
    pass an assertion set over an empty payload — so on CI the payload must be
    proved to carry measurements. This module is in the jsdom job
    (`test_jsdom_ci_contract.py` asserts that), and this is its half of the
    promise that the job is not green over a run that never happened.
    """

    assert shell["fatal"] is None
    assert shell["scenarioFatal"] is None
    assert shell["scriptErrors"] == []
    assert len(shell["consoleErrors"]) == 1, shell["consoleErrors"]
    assert "the shell failed to start" in shell["consoleErrors"][0]
    if os.environ.get("MCC_CI", "") not in ("", "0", "false"):
        assert shell["hybrid"]["masthead"] is True
        assert shell["roundTrip"]["before"]["views"]
        assert shell["failure"]["line"]


# ---------------------------------------------------------------- round trip ---


def test_today_renders_identically_after_a_trip_through_hybrid(shell) -> None:
    """The governing proof: a design that is not selected changes nothing.

    The comparison is on every place a shell can leave a mark — the chrome
    outside the app shell, ``<html>``'s design attributes, ``<body>``'s classes,
    the rail's own markup with the decoration stripped, and every ``hy-`` name
    in the document — taken before the first switch and after the last one.
    """

    trip = shell["roundTrip"]
    assert trip["equal"] is True, json.dumps(
        {
            "before": {
                "chrome": trip["before"]["chrome"],
                "attrs": trip["before"]["htmlAttrs"],
                "body": trip["before"]["bodyClass"],
                "railBare": trip["before"]["railBare"],
                "residue": trip["before"]["residue"],
            },
            "after": {
                "chrome": trip["after"]["chrome"],
                "attrs": trip["after"]["htmlAttrs"],
                "body": trip["after"]["bodyClass"],
                "railBare": trip["after"]["railBare"],
                "residue": trip["after"]["residue"],
            },
        },
        indent=2,
    )


def test_the_round_trip_leaves_no_shell_residue(shell) -> None:
    """Named separately, because it is the sentence a reader cares about."""

    for side in ("residueBefore", "residueAfter", "residueSecond"):
        residue = shell["roundTrip"][side]
        assert residue["attributes"] == 0, side
        assert residue["nodes"] == 0, side
        assert residue["classes"] == [], side


def test_a_second_trip_changes_nothing_more(shell) -> None:
    """A leak that only accumulates after two switches is still a leak."""

    assert shell["roundTrip"]["idempotent"] is True


def test_the_round_trip_is_not_vacuous(shell) -> None:
    """The digest must have had a page to digest, and the design must have run."""

    before = shell["roundTrip"]["before"]
    assert len(before["views"]) == 16
    assert len(before["chrome"]) > 0  # the action bar and banners live out here
    assert before["htmlAttrs"], "no <html> attributes were captured at all"
    # ...and the switch really did happen: the running shell is Hybrid's.
    assert shell["hybrid"]["running"]["id"] == "hybrid"
    assert shell["hybrid"]["masthead"] is True


# --------------------------------------------------------------- the design ---


def test_hybrid_builds_its_masthead_once(shell) -> None:
    hybrid = shell["hybrid"]
    assert hybrid["brand"] == "My Claude Code"
    assert hybrid["sub"] == "Server control"
    assert hybrid["searchLabel"] == "Search pages, sections and settings"
    # The version indicator MOVES into the masthead, and is not copied.
    assert hybrid["versionInMasthead"] is True
    assert hybrid["versionInSidebar"] is False


def test_the_rail_is_numbered_and_grouped_in_the_products_own_order(shell) -> None:
    """SETUP · AGENTS · ROUTING · OBSERVE · SYSTEM over all sixteen pages.

    The grouped structure is the thing the user said he likes (LEFT RAIL,
    binding). What must also hold is that the NUMBERING is the product's own
    order — a design reorders nothing — and that every page keeps its link.

    8.3.0 adds the sixteenth page (``usage``), first in OBSERVE. The counts here
    move with it; the ORDER assertion below is what proves the design did not
    reorder anything to accommodate it.
    """

    hybrid = shell["hybrid"]
    assert hybrid["groups"] == ["Setup", "Agents", "Routing", "Observe", "System"]
    # Sixteen links, one of which (InferHub) has no marketplace key in this
    # fixture and is therefore hidden — the number counts what is VISIBLE, so
    # 01…15 over 16 links is the correct reading, not a defect.
    assert hybrid["numbers"] == [f"{index:02d}" for index in range(1, 16)]
    assert hybrid["sectionNavButtons"] == 16
    order = hybrid["groupOrder"]
    # Every heading sits directly before the page it opens, and the pages are
    # the product's order: get_started … docs, with no page moved.
    pages = [entry for entry in order if not entry.startswith("#")]
    assert pages == [
        "get_started",
        "providers",
        "proxying",
        "marketplaces",
        "claude",
        "coding_agents",
        "model_config",
        "models",
        "messaging",
        # 8.3.0: the new page lands before Analytics, which is the position the
        # user asked for; nothing else moved.
        "usage",
        "requests",
        "optimizer",
        "web_search",
        "limits",
        "guide",
        "docs",
    ]
    for heading, page in itertools.pairwise(order):
        if heading.startswith("#"):
            assert not page.startswith("#"), f"two headings in a row: {heading} {page}"


def test_the_section_navigator_is_the_products_and_is_neither_hidden_nor_duplicated(
    shell,
) -> None:
    """The strip 8.1.0 ships must survive the design unchanged.

    Round 4's rebuild DELETED the preview kit's own jump bars because the
    product renders the navigator; the port must not bring them back, hide the
    product's, or draw a second one. One strip per page that has sections, and
    none on the two pages that have fewer than three.
    """

    navigators = shell["hybrid"]["navigatorsByPage"]
    for view, count in navigators.items():
        assert count <= 1, f"{view} carries {count} section navigators"
    # The twelve pages the 8.1.0 table covers each get exactly one; Get
    # Started and Messaging have fewer than three named sections, so no strip.
    assert sorted(view for view, count in navigators.items() if count == 1) == [
        "claude",
        "coding_agents",
        "docs",
        "guide",
        "limits",
        "marketplaces",
        "model_config",
        "models",
        "optimizer",
        "providers",
        "proxying",
        "requests",
        "web_search",
    ]
    assert navigators["get_started"] == 0
    assert navigators["messaging"] == 0


def test_the_design_declares_its_variant_attributes(shell) -> None:
    drawer = shell["drawer"]
    assert drawer["detailAttr"] == "drawer"
    assert drawer["sectionsAttr"] == "ruled"
    assert drawer["fieldsAttr"] == "rows"
    assert drawer["analyticsAttr"] == "list-first"
    # Compact is the design's declared default density, resolved through the
    # registry row — not a value anyone typed into an attribute.
    assert drawer["densityAttr"] == "compact"


# ------------------------------------------------------------------ palette ---


def test_the_palette_is_a_dialog_over_pages_sections_and_settings(shell) -> None:
    palette = shell["palette"]
    assert palette["present"] is True
    assert palette["role"] == "dialog"
    assert palette["modal"] == "true"
    assert palette["labelled"] == "Command palette"
    # It opens on the pages — one per page, which is the whole rail.
    assert palette["itemsOnOpen"] == 16
    assert set(palette["hintsOnOpen"]) == {"page"}
    assert palette["expandedOnButton"] == "true"


def test_the_palette_finds_a_setting_by_its_label(shell) -> None:
    """``--design-system``-grade search: a label, not a key, reaches the field."""

    items = shell["palette"]["afterQuery"]["items"]
    assert items, "searching for a setting returned nothing"
    assert any(item["code"] == "MAX_OUTPUT_TOKENS_CEILING" for item in items), items
    assert any("Limits" in item["hint"] for item in items), items


def test_escape_closes_the_palette_and_returns_focus(shell) -> None:
    after = shell["paletteAfterEscape"]
    assert after["activeBeforeEscape"] is True, "the probe never had focus to lose"
    assert after["present"] is False
    assert after["expanded"] == "false"
    assert after["focusReturned"] is True, after["activeElement"]


def test_a_section_item_switches_the_page_and_lands_on_its_section(shell) -> None:
    section = shell["paletteSection"]
    assert section["found"] is True
    assert section["view"] == "limits"
    assert section["paletteGone"] is True
    # The product's own navigator is what marks where you are, and the jump set
    # the page anchor so a reload keeps the position.
    assert section["hash"] == "#section-diagnostics"
    assert section["stripCurrent"] == ["Diagnostics"]


# ---------------------------------------------------------------- shortcuts ---


def test_the_g_keys_navigate(shell) -> None:
    assert shell["shortcuts"]["afterGL"] == "limits"
    assert shell["shortcuts"]["afterGR"] == "requests"


def test_a_g_key_typed_into_a_field_types_instead_of_navigating(shell) -> None:
    assert shell["shortcuts"]["typingIgnored"] is True


# ------------------------------------------------------- switching under load ---


def test_a_burst_of_switches_ends_with_exactly_one_shell(shell) -> None:
    """Three switches in one tick is a reader clicking twice, fast.

    What must be true afterwards: the design is the last one asked for, and its
    shell is attached exactly once. Nothing may be left from the switch before.
    """

    burst = shell["switchUnderLoad"]
    assert burst["design"] == "hybrid"
    assert burst["state"]["running"]["id"] == "hybrid"
    assert burst["mastheads"] == 1
    assert burst["shellNodes"] == 1
    assert burst["numbers"] == 15
    assert burst["groups"] == 5
    assert burst["palettes"] == 0
    assert burst["versionIndicators"] == 1


# ------------------------------------------------------------- the failure path ---


def test_a_throwing_shell_leaves_a_working_dashboard(shell) -> None:
    """Proof item 3: a shell that throws must not break the dashboard.

    Six things are asserted, because "it still works" is six claims: the page
    is still navigable, the current view still renders, no leftover node
    remains, the chrome and rail are byte-identical to before the failed
    attempt, the version indicator is back where it belongs, and ONE line was
    logged rather than a stack per retry.
    """

    failure = shell["failure"]
    assert failure["design"] == "hybrid"
    assert failure["shellState"]["running"] is None
    assert failure["navWorks"] is True
    assert failure["viewsRendered"] == [True]
    assert failure["mastheads"] == 0
    assert failure["shellNodes"] == 0
    assert failure["railNumbers"] == 0
    assert failure["chromeMatches"] is True
    assert failure["railMatches"] is True
    assert failure["versionInSidebar"] is True
    assert failure["versionCount"] == 1
    # The design's CHROME is gone — that is what failed. Its STYLESHEET and its
    # layout attributes are NOT: `applyAppearance` loaded the CSS and wrote the
    # `data-*` set before the shell chain ran, and a failed shell does not
    # un-dress a page. Asserting they were gone would pin a lie: the sentence a
    # reader sees says the chrome is missing and the layout is unchanged, and
    # both halves of that have to be true together.
    assert "data-sections=ruled" in failure["htmlAttrs"]
    assert "data-detail=drawer" in failure["htmlAttrs"]
    # ...but the body class the shell adds is gone with the shell.
    assert "hy-mode" not in failure["bodyClass"]
    # One line, and it is a sentence that describes what actually happened.
    assert failure["consoleErrorsAdded"] == 1
    assert failure["lineRole"] == "status"
    assert "chrome could not start" in failure["line"]
    assert "Every page, control and setting still works" in failure["line"]
    assert "layout and colours are unchanged" in failure["line"]
    # It must NOT claim the page fell back to another design's layout: the CSS
    # is still Hybrid's, so that sentence would be false.
    assert "using Today's layout" not in failure["line"]


# ------------------------------------------------------- the URL and the route ---


def test_both_halves_are_asked_for_on_the_one_route_that_serves_designs(shell) -> None:
    """The 8.1.1 lesson, applied to the shell as well as the stylesheet.

    A design is two files. Both must be fetched from `/admin/<the registry's
    path>` — the route that answers designs — and neither from `/admin/assets/`,
    which serves the five fixed names and 404s everything else. Pinned from both
    ends: the URL the code builds, and the route declaration that answers it.
    """

    urls = shell["shellUrls"]
    assert urls["css"] == "/admin/designs/hybrid.css"
    assert urls["shell"] == "/admin/designs/hybrid.js"
    # The loader actually asked for the shell's URL, and only that one.
    assert shell["transports"], "the loader never fetched a shell module"
    assert set(shell["transports"]) == {"/admin/designs/hybrid.js"}
    # ...and the route that serves it is declared, under the same prefix.
    assert '@router.get("/admin/designs/{filename}"' in ADMIN_ROUTES
    # The loader builds the URL from the registry's own relative path, so a
    # rename on either side fails here rather than in a browser.
    assert "`/admin/${wantedPath}`" in ADMIN_JS
    assert (
        "`/admin/assets/`" not in ADMIN_JS.split("function loadDesignShell")[1][:4000]
    )


def test_the_loader_handed_the_shell_a_namespace_limited_to_allowlisted_attributes() -> (
    None
):
    """A design may only declare the variant attributes the foundation styles.

    A document-wide `data-*` a design invented would be a selector the NEXT
    design inherits by accident, which is how two designs end up quietly
    coupled. The allowlist is one set in admin.js.
    """

    assert "DESIGN_ATTRIBUTE_ALLOWLIST" in ADMIN_JS
    for name in ("sections", "fields", "detail", "analytics", "shell"):
        assert f'"{name}"' in ADMIN_JS.split("DESIGN_ATTRIBUTE_ALLOWLIST")[1][:400]


# --------------------------------------------------------- the CSS contract ---


def test_the_hybrid_stylesheet_selects_on_the_products_attribute() -> None:
    """Every scoped rule names `data-design="hybrid"`, never the preview's key.

    `html[data-dir="hybrid"]` was the PREVIEW's attribute — a name its kit wrote
    and nothing else read. Ported literally, every rule in this file would be
    dead in the product and the design would render as Today.
    """

    assert 'html[data-design="hybrid"]' in HYBRID_CSS
    assert "data-dir=" not in _code_only(HYBRID_CSS)


def test_the_hybrid_stylesheet_does_not_redeclare_a_foundation_token() -> None:
    """The shared skin has one home: foundation.css.

    A second declaration in a design's file loads LAST, so it would silently win
    the cascade and make every later change to the shared rhythm invisible to
    this one design. The design keeps its own `--hy-*` names and nothing else.
    """

    shared = [
        "--font-display",
        "--font-ui",
        "--font-num",
        "--body-size",
        "--title-size",
        "--title-weight",
        "--title-tracking",
        "--h3-size",
        "--h3-weight",
        "--section-pad",
        "--section-gap",
        "--section-radius",
        "--card-radius",
        "--control-radius",
        "--control-h",
        "--field-gap",
        "--sidebar-w",
    ]
    for token in shared:
        assert f"{token}:" not in _code_only(HYBRID_CSS), (
            f"hybrid.css redeclares the shared token {token}; foundation.css "
            "owns it and this file would win the cascade"
        )


def test_the_rail_obeys_the_left_rail_floor() -> None:
    """LEFT RAIL, binding: size, not colour. Nothing is darkened.

    The preview shrank the rail to 12.5 px links, 30 px rows and a 10 px index,
    which is what the user called unreadable. The floors are 14 / 36 / 11, and
    contrast was already fine (8.31 / 15.61 / 5.94 on Velvet dark) so no colour
    may be changed to "fix" it.
    """

    # The floors are the tokens' values in the foundation.
    assert "--rail-link-size: 14px;" in FOUNDATION_CSS
    assert "--rail-row-h: 40px;" in FOUNDATION_CSS
    assert "--rail-index-size: 11px;" in FOUNDATION_CSS
    assert "--rail-group-size: 11.5px;" in FOUNDATION_CSS
    assert "--rail-group-tracking: 0.08em;" in FOUNDATION_CSS
    # The design consumes them rather than restating numbers.
    for token in (
        "var(--rail-link-size)",
        "var(--rail-row-h)",
        "var(--rail-index-size)",
        "var(--rail-group-size)",
        "var(--rail-group-tracking)",
    ):
        assert token in HYBRID_CSS, token
    # The preview's numbers are gone: no 12.5 px links, no 30 px rows.
    rail_block = HYBRID_CSS.split("4. the rail (LEFT RAIL floor)")[1].split(
        "5. the page title"
    )[0]
    assert "12.5px" not in rail_block
    assert "min-height: 30px" not in rail_block
    # Active is weight + the accent border, and sets no colour of its own.
    active = rail_block.split(".sidebar .nav-link.active {")[1].split("}")[0]
    assert "font-weight: 700" in active
    assert "border-left-color: var(--accent)" in active
    assert "color: var(--text-strong)" in active  # an existing token, not a new colour


def test_both_r3_styling_rules_the_rebuild_dropped_are_restored() -> None:
    """The round-4 preview lost two of Hybrid's own rules by accident.

    `design-previews-r3-20261031/option-19-hybrid/direction.css` has
    `.mcc-hero-value { font-size: 44px }` and a borderless `.mcc-chart` with a
    hairline top rule. The rebuild dropped both because 7.122.3 had replaced the
    kit's hero and charts with the product's own, and the selectors stopped
    matching — a REBUILD slip, not a decision. The design's own look is restored
    against the product's real selectors: the summary band's figures and the
    four chart panels.
    """

    assert "#reqStatsCards .requests-card strong" in HYBRID_CSS
    assert "font-size: 44px" in HYBRID_CSS
    assert ".analytics-panel.requests-chart" in HYBRID_CSS
    chart_rule = HYBRID_CSS.split(".analytics-panel.requests-chart,")[1].split("}")[0]
    assert "border-top: 1px solid var(--hy-rule-strong)" in chart_rule
    assert "border-radius: 0" in chart_rule


def test_the_shell_module_reaches_the_page_only_through_its_surface() -> None:
    """A shell gets the loader's deliberate minimum and nothing else.

    The module may not touch admin.js's internals: it is an ES module, so it
    cannot SEE classic-script globals — and this test closes the other door, the
    one where somebody reaches for `window.state` or `window.setActiveView` and
    gets away with it because admin.js happened to publish them.
    """

    forbidden = (
        "window.state",
        "window.setActiveView",
        "window.renderSections",
        "window.byId",
        "window.api(",
        "document.body.innerHTML",
        ".innerHTML =",
    )
    for name in forbidden:
        assert name not in _code_only(HYBRID_JS), (
            f"designs/hybrid.js reaches into the page with {name!r}; a design's "
            "shell must use the api it is handed"
        )
    # ...and it does use that surface.
    assert "api.mount(" in HYBRID_JS
    assert "api.listen(" in HYBRID_JS
    assert "api.onTeardown(" in HYBRID_JS
    assert "api.setActiveView(" in HYBRID_JS
    # Published under the key the loader looks up, which is the registry's path.
    assert 'window.MCCDesignShells["designs/hybrid.js"]' in HYBRID_JS
    assert 'shell: "designs/hybrid.js"' in DESIGNS_JS


def test_the_shell_does_not_restore_the_previews_jump_bars() -> None:
    """The round-4 rebuild deleted `jumpBars` on purpose; the port keeps that.

    The product renders the per-page section navigator (8.1.0), and its three
    old topbar rails are folded INTO it. A design that also asked the kit for
    jump bars would draw a second mechanism — which the requirement forbids by
    name ("neither hidden nor duplicated").
    """

    assert "jumpBars" not in HYBRID_JS
    assert "hostJumpBars" not in HYBRID_JS
    assert "addJumpBars" not in HYBRID_JS
