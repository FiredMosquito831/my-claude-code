"""The 7.122.3 UX pass's structural promises, read out of the shipped files.

The jsdom scenarios prove behaviour; these read the three assets directly, the
way ``test_admin_asset_wiring.py`` does, because each of them is a property of
the markup or the stylesheet that no DOM run can observe:

* no retired command name anywhere in the dashboard (the user's Q13, "fully
  retire old fcc commands and any mentions");
* the request-detail modal stacked above the action bar (U5);
* the Details column pinned inside the request table's scroller (U35);
* the Analytics blocks in the order ADDENDUM 2 asks for;
* the four summary charts declared with a keyboard and a described-by.
"""

import re
from pathlib import Path

ADMIN_STATIC = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "my_claude_code"
    / "api"
    / "admin_static"
)
ADMIN_JS = ADMIN_STATIC / "admin.js"
INDEX_HTML = ADMIN_STATIC / "index.html"
ADMIN_CSS = ADMIN_STATIC / "admin.css"

_JS_SOURCE = ADMIN_JS.read_text(encoding="utf-8")
_HTML_SOURCE = INDEX_HTML.read_text(encoding="utf-8")
_CSS_SOURCE = ADMIN_CSS.read_text(encoding="utf-8")


def _css_block(selector: str) -> str:
    """The declaration block of one rule, by its exact selector text."""

    match = re.search(
        rf"(?m)^{re.escape(selector)}\s*\{{(.*?)^\}}", _CSS_SOURCE, re.DOTALL
    )
    assert match, f"admin.css has no rule for {selector!r}"
    return match.group(1)


# ── Q13: no retired command name, and no legacy product name, anywhere ──────
#: The dashboard files that carry text. The images under ``img/`` are
#: screenshots: PNG bytes are not text, and the Guide's shots are re-taken by
#: the documents PR. The gate is over what the dashboard *says*.
_DASHBOARD_TEXT_FILES = (ADMIN_JS, INDEX_HTML, ADMIN_CSS)


def test_the_dashboard_names_no_retired_command_or_legacy_product_name() -> None:
    """The user's Q13, as a gate: "fully retire old fcc commands and any
    mentions". The commands themselves are removed by the retirement PR; this
    one guarantees the dashboard stopped naming them first.
    """

    offenders = []
    for path in _DASHBOARD_TEXT_FILES:
        text = path.read_text(encoding="utf-8")
        for pattern in ("fcc", "free-claude-code", "free_claude_code"):
            for line_number, line in enumerate(text.splitlines(), start=1):
                if pattern in line.lower():
                    offenders.append(f"{path.name}:{line_number}: {line.strip()[:90]}")
    assert offenders == [], (
        "the dashboard still names a retired command or the legacy product name: "
        f"{offenders}"
    )


def test_the_docs_page_payload_names_none_either() -> None:
    """The Docs page is rendered by the server from bundled files; its module
    is not where those words may live either."""

    source = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "my_claude_code"
        / "api"
        / "docs_content.py"
    ).read_text(encoding="utf-8")
    lowered = source.lower()
    assert "fcc" not in lowered
    assert "free-claude-code" not in lowered


# ── U5: a modal is above every page control while it is open ───────────────


def test_the_request_modal_stacks_above_the_action_bar() -> None:
    modal = _css_block(".requests-modal")
    bar = _css_block(".action-bar")
    modal_z = re.search(r"z-index:\s*(\d+)", modal)
    bar_z = re.search(r"z-index:\s*(\d+)", bar)
    assert modal_z and bar_z
    assert int(modal_z.group(1)) > int(bar_z.group(1)), (
        "the modal is drawn behind the action bar: its last lines and "
        "'Export this request…' were covered (U5)"
    )


# ── U35: the Details column stays reachable ────────────────────────────────


def test_the_details_column_is_pinned_in_the_request_table() -> None:
    block = _css_block(".requests-table th:last-child,\n.requests-table td:last-child")
    assert "position: sticky" in block
    assert re.search(r"right:\s*0", block)
    # Pinned over a scrolling table needs its own surface, or the cells
    # behind it show through the text.
    assert "background:" in block


# ── ADDENDUM 2: the Analytics canvas, in the order the user asked for ──────


def _html_position(marker: str) -> int:
    index = _HTML_SOURCE.find(marker)
    assert index >= 0, f"index.html has no {marker}"
    return index


def test_the_analytics_blocks_read_tiles_then_in_flight_then_requests() -> None:
    order = [
        'id="reqSummaryHeading"',  # (a) the usage/stat tiles
        'id="reqStatsCards"',
        'id="reqInflightPanel"',  # (b) in flight
        'id="reqLogHeading"',  # (c) the request list
        'id="reqLogTableWrap"',
        'id="reqChartsHeading"',  # then the summary charts
    ]
    positions = [_html_position(marker) for marker in order]
    assert positions == sorted(positions), (
        "the Analytics canvas is not in the order ADDENDUM 2 asks for: "
        "tiles, then in flight, then the request list, then the charts"
    )
    assert positions[0] < _html_position('id="reqChartsHeading"')


def test_the_request_list_carries_its_own_filters() -> None:
    """The filters travel with the list they filter, not 4,530 px above it."""

    toolbar = _html_position('class="requests-toolbar"')
    table = _html_position('id="reqLogTableWrap"')
    charts = _html_position('id="reqChartsHeading"')
    assert toolbar < table < charts
    assert 'id="reqApplyFilters"' in _HTML_SOURCE


def test_the_four_summary_charts_are_declared_with_a_keyboard_and_a_reading() -> None:
    for canvas_id in (
        "reqSeriesChart",
        "reqTtftChart",
        "reqCostChart",
        "reqModelChart",
    ):
        match = re.search(
            rf'<canvas[^>]*id="{canvas_id}"[^>]*>', _HTML_SOURCE, re.DOTALL
        )
        assert match, f"{canvas_id} is not declared"
        tag = match.group(0)
        assert 'tabindex="0"' in tag, f"{canvas_id} cannot take focus"
        assert f'aria-describedby="{canvas_id}Readout"' in tag, (
            f"{canvas_id} has no live readout for a keyboard"
        )
        assert f'id="{canvas_id}Readout"' in _HTML_SOURCE


def test_the_cost_tile_is_offered_by_the_page() -> None:
    """It is built by JS (paintRequestCostTile), so the markup names the
    renderer's hook and the chart it also fills."""

    assert "paintRequestCostTile" in _JS_SOURCE
    assert 'id="reqCostChart"' in _HTML_SOURCE
