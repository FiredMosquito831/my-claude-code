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
"""

import copy

import pytest

from tests.support.ddg_harness import baseline, corpus, methods


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

    cells = baseline.load_baseline()["cells"]
    ddgs = cells["search:ddgs"]
    legacy = cells["search:legacy"]
    fetch = cells["fetch:web_fetch"]

    # D1: on the live-recorded page the ddgs path returns DuckDuckGo's redirect
    # links, not the destinations; the legacy parser decodes the same page.
    live = ddgs["live_html_200"]["shape"]["results"]
    assert len(live) == 10
    assert all(row["url"].startswith("//duckduckgo.com/l/?uddg=") for row in live)
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
