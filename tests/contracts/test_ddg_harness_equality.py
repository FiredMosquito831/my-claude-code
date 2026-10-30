"""Today's DuckDuckGo search and page-extraction outputs, pinned byte for byte.

``ddg_harness_baseline.json`` was generated on ``7b9b87b5`` (v7.113.1) by
``tests/support/ddg_harness/baseline.py``: for every fixture under
``tests/fixtures/ddg`` and ``tests/fixtures/extraction``, what the ``ddgs``
provider path, the legacy lite scraper and ``web_fetch`` return today, as a
readable shape and the sha256 of the canonical outcome. It is the base every
later DDG PR proves itself against: a PR names the cells it is meant to move
and every other cell must stay byte-identical. Recorded pages and loopback
only (``tests/support/ddg_harness/methods.py``); no test here reaches the
network.

The baseline pins today's DEFECTS as they are -- that is the point of it
(RESEARCH-DDG-WEBSEARCH.md 1.4-1.5; the readable checks at the bottom name
them). Regenerate it only on purpose, never to make this test pass.

**7.118.2 (DDG-2) moves exactly two cells, on purpose** (spec 5 R1; USER
DECISIONS 2026-10-25 Q2). The ``ddgs`` adapter now reports the destination a
DuckDuckGo ``/l/?uddg=`` redirect names instead of the redirect, and drops the
scheme-relative ``y.js`` ad row the engine leaked. Both cells are
``search:ddgs`` -- the live-recorded page's ten D1 redirect URLs and the edge
page's one leaked ad -- and each records both answers under
``moved_by_ddg_2``, ``v7.118.1`` before and ``v7.118.2`` after. Every other cell
is DDG-1's answer byte for byte: the live-recorded and synthetic pages are the
only fixtures the DuckDuckGo engine returns rows for (the lite pages are
answered with the engine's own "No results found."), the legacy hop and
``web_fetch`` are untouched, and no request the fake upstream saw moved.
"""

import copy

import pytest

from tests.support.ddg_harness import baseline, corpus, methods

#: The cells 7.118.2 moves, as they are labelled in the baseline. A later
#: release that moves one of them again has to update this list and the
#: ``moved_by_ddg_2`` block with it.
MOVED_BY_DDG_2 = ("search:ddgs edge_html_200", "search:ddgs live_html_200")


@pytest.mark.local_serial
def test_todays_methods_reproduce_the_baseline_byte_for_byte() -> None:
    """Every (method, fixture) cell of today's code equals the pinned digest."""

    cells = methods.run_sync(baseline.compute_cells())
    diff = baseline.moved_cells(baseline.load_baseline(), cells)

    assert diff == {"moved": [], "missing": [], "added": []}, (
        "the DDG harness baseline moved. A DDG PR lists the cells its purpose "
        "changes and leaves every other cell byte-identical; regenerate the "
        "baseline only on purpose (tests/support/ddg_harness/baseline.py). "
        f"{diff}"
    )


def test_a_changed_baseline_byte_is_reported_as_that_cell_only() -> None:
    """The comparison catches a one-character change and names its cell."""

    pinned = baseline.load_baseline()
    as_computed = copy.deepcopy(pinned["cells"])
    mutated = copy.deepcopy(pinned)
    cell = mutated["cells"]["search:ddgs"]["live_html_200"]
    cell["digest"] = ("0" if cell["digest"][0] != "0" else "1") + cell["digest"][1:]

    assert baseline.moved_cells(pinned, as_computed) == {
        "moved": [],
        "missing": [],
        "added": [],
    }
    assert baseline.moved_cells(mutated, as_computed) == {
        "moved": ["search:ddgs live_html_200"],
        "missing": [],
        "added": [],
    }


def test_the_baseline_has_a_cell_for_every_fixture_and_method() -> None:
    """A fixture or method added without regenerating the baseline is caught."""

    cells = baseline.load_baseline()["cells"]
    scenario_ids = {scenario.id for scenario in corpus.search_corpus().scenarios}
    page_keys = {page.key for page in corpus.all_extraction_pages()}

    assert set(cells) == {
        *(f"search:{name}" for name in methods.SEARCH_METHODS),
        *(f"fetch:{name}" for name in methods.FETCH_METHODS),
    }
    for name in methods.SEARCH_METHODS:
        assert set(cells[f"search:{name}"]) == scenario_ids
    for name in methods.FETCH_METHODS:
        assert set(cells[f"fetch:{name}"]) == page_keys
    assert len(corpus.trafilatura_pages()) == 106
    assert len(corpus.readability_pages()) == 181


def test_the_baseline_records_todays_findings_as_they_are() -> None:
    """D1-D3 of the research and the no-charset fetch failure, read from the pins."""

    document = baseline.load_baseline()
    cells = document["cells"]
    ddgs = cells["search:ddgs"]
    legacy = cells["search:legacy"]
    fetch = cells["fetch:web_fetch"]

    # D1: 7.113.1's ddgs path returned DuckDuckGo's redirect links, not the
    # destinations -- kept as the before-answer -- and 7.118.2 reports the
    # destination; the legacy parser decoded the same page all along.
    live = ddgs["live_html_200"]["shape"]["results"]
    assert len(live) == 10
    assert all(row["url"].startswith("https://") for row in live)
    before = document[baseline.DDG2_KEY]["search:ddgs live_html_200"]
    assert all(
        row["url"].startswith("//duckduckgo.com/l/?uddg=")
        for row in before[baseline.DDG2_BEFORE]["shape"]["results"]
    )
    assert all(
        row["url"].startswith("https://")
        for row in legacy["live_html_200"]["shape"]["results"]
    )
    # D2: every non-transport failure of the ddgs path is the same upstream error.
    for scenario in ("challenge_html_202", "http_403", "http_429", "empty_200"):
        shape = ddgs[scenario]["shape"]
        assert (shape["kind"], shape["error_text"]) == (
            "upstream",
            "ddgs: ddgs search failed: No results found.",
        )
    # D3: the legacy scraper answers a challenge page with an empty success and
    # surfaces 403/429 as a raw httpx error.
    assert legacy["challenge_lite_202"]["shape"]["outcome"] == "empty"
    assert legacy["http_429"]["shape"]["error_class"] == "HTTPStatusError"
    # The legacy results carry no snippet at all (title and url only).
    assert set(legacy["live_html_200"]["shape"]["results"][0]) == {"title", "url"}
    # web_fetch asks aiohttp for the encoding before reading the body, so a
    # response whose Content-Type has no charset fails outright.
    no_charset = fetch["fetch_scenarios/html_without_charset"]["shape"]
    assert no_charset["error_class"] == "RuntimeError"


# --------------------------------------------------------------- 7.118.2 DDG-2


def test_7_118_2_moves_only_the_two_cells_it_names() -> None:
    """The moved set is exactly these cells, and the record says both answers.

    The rest of the file is DDG-1's answer: a cell that moved without being
    named here would have had to be regenerated, and this test's list is where
    a later release has to say so.
    """

    document = baseline.load_baseline()
    moved = document[baseline.DDG2_KEY]
    assert tuple(sorted(moved)) == MOVED_BY_DDG_2
    for label, entry in moved.items():
        method, key = label.split(" ", 1)
        assert set(entry) == {baseline.DDG2_BEFORE, baseline.DDG2_AFTER}, label
        # The after-answer is what this tree computes and pins in ``cells``.
        assert entry[baseline.DDG2_AFTER] == document["cells"][method][key], label
        assert (
            entry[baseline.DDG2_BEFORE]["digest"]
            != entry[baseline.DDG2_AFTER]["digest"]
        ), label
    # No other cell carries a before/after pair: nothing else was marked moved.
    paired = [
        f"{method} {key}"
        for method, group in document["cells"].items()
        for key, cell in group.items()
        if set(cell) != {"shape", "digest"}
    ]
    assert paired == []


def test_7_118_2_unwraps_the_recorded_redirects_and_drops_the_leaked_ad() -> None:
    """D1 and the leaked ad, from the recorded before-answer to this tree's own.

    The live-recorded 2026-09-28 page: ten redirect links before, ten
    destinations after, the same titles and snippets in the same order. The
    synthetic edge page: the scheme-relative ``y.js`` ad row gone, the three
    organic rows untouched and in order, and a destination whose own query
    string survives whole (the engine decodes it once, so ``&page=2`` is part
    of the destination and only ``&rut=`` is dropped).
    """

    document = baseline.load_baseline()
    moved = document[baseline.DDG2_KEY]

    live = moved["search:ddgs live_html_200"]
    before_live = live[baseline.DDG2_BEFORE]["shape"]["results"]
    after_live = live[baseline.DDG2_AFTER]["shape"]["results"]
    assert len(before_live) == 10
    assert all(
        row["url"].startswith("//duckduckgo.com/l/?uddg=") for row in before_live
    )
    assert [row["title"] for row in after_live] == [row["title"] for row in before_live]
    assert [row["snippet"] for row in after_live] == [
        row["snippet"] for row in before_live
    ]
    assert [row["url"] for row in after_live] == [
        "https://en.wikipedia.org/wiki/Omarchy",
        "https://omarchy.org/",
        "https://omarchy.us/",
        "https://omarchy-official.org/",
        "https://omarchy.org/manual/",
        "https://github.com/omacom/omarchy",
        "https://omarchy.archi/",
        "https://tedium.co/2025/10/13/omarchy-linux-distro-commentary/",
        "https://distrowatch.com/omarchy",
        "https://tbreak.com/omarchy-linux-distro-agentic-ai/",
    ]

    edge = moved["search:ddgs edge_html_200"]
    before_edge = edge[baseline.DDG2_BEFORE]["shape"]["results"]
    after_edge = edge[baseline.DDG2_AFTER]["shape"]["results"]
    assert before_edge[0]["url"].startswith("//duckduckgo.com/y.js?ad_domain=")
    assert len(before_edge) == 4
    assert len(after_edge) == 3
    assert all("duckduckgo.com" not in row["url"] for row in after_edge)
    # The organic rows are the same three, in the same order, untouched.
    assert [row["title"] for row in after_edge] == [
        row["title"] for row in before_edge[1:]
    ]
    assert [row["snippet"] for row in after_edge] == [
        row["snippet"] for row in before_edge[1:]
    ]
    assert after_edge[2]["url"] == "https://example.com/search?q=a+b&page=2"
