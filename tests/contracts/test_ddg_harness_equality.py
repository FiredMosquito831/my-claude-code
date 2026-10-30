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

**7.119.0 (DDG-3) moves exactly twelve cells, on purpose** (spec 5 R2; USER
DECISIONS Q1, Q4, Q9, Q10). The legacy lite hop is re-implemented as declared
data (``config/duckduckgo_profiles.py``): the three result pages gain their
snippets and the edge page its direct-link row; the four bot-check pages are a
``rate_limit`` naming the check instead of an empty answer (D3); 403 / 429 /
500 / 502 / 503 are worded through the shared status mapping (``auth``,
``rate_limit``, ``upstream`` with the status) instead of a raw httpx error.
Each records both answers under ``moved_by_ddg_3`` (``v7.118.2`` before,
``v7.119.0`` after). The empty and garbage pages stay the honest empty answer
their scenarios declare, the two transport failures stay as they were, and
every ``ddgs`` and ``web_fetch`` cell is byte for byte 7.118.2's (the 319
unmoved cells' digest map hashed the same on both files, PROGRESS.md); no
request the fake upstream saw moved.

**7.119.1 (DDG-3b) moves all sixteen ``search:legacy`` cells, on purpose, and
only their request** (spec 5 R2; USER DECISIONS Q12). The hop asks the html
form first -- ``POST https://html.duckduckgo.com/html/`` with ``q`` and ``b``
-- and the lite page only as its second try, when the html page held no
result and no bot check. The fake upstream serves each scenario's page to
every request, so every answer is 7.119.0's byte for byte; what moved is the
request it saw: the html form in all sixteen, followed by 7.119.0's own lite
request on the two pages with nothing (``empty_200``, ``garbage_200``). Each
records both answers under ``moved_by_ddg_3b`` (``v7.119.0`` before,
``v7.119.1`` after); ``moved_by_ddg_3`` stays verbatim, its after-answer being
the new block's before-answer. Every ``ddgs`` and ``web_fetch`` cell is byte
for byte 7.119.0's (the 315 unmoved cells' digest map hashed the same on both
files, PROGRESS.md).

**7.120.0 (DDG-4) moves every ``web_fetch`` cell whose page the extractor
reads, and the two no-charset cells DDG-1 pinned as failures** (spec 5 R3;
USER DECISIONS Q6-Q8/Q12). The default extractor is trafilatura's
markdown-with-links, so 283 pages answer with markdown instead of every text
node; ``fetch_scenarios/html_without_charset`` and ``fetch_scenarios/pdf_bytes``
used to raise ``RuntimeError`` because aiohttp was asked for the encoding
before the body was read, and answer now. Each records both answers under
``moved_by_ddg_4`` (``v7.119.1`` before, ``v7.120.0`` after). The fourteen
unmoved cells are named: four are responses no extractor reads (plain text,
JSON, a 404 and the Cloudflare 403) and ten are HTML pages whose extraction was
under the declared 200-character threshold, where today's parser answered
exactly as before -- R3's "a page never gets worse", checked mechanically
rather than trusted. Every ``search:*`` cell is byte for byte 7.119.1's.
``WEB_FETCH_EXTRACTOR=basic`` reproduces 7.119.1's bytes on every cell but the
two the encoding fix answers (``tests/api/test_web_fetch_extractor.py``).
"""

import copy
import hashlib

import pytest

from my_claude_code.api.web_tools import fetch_extract
from my_claude_code.api.web_tools.constants import _WEB_FETCH_EXTRACTOR_MIN_CHARS
from my_claude_code.api.web_tools.parsers import HTMLTextParser
from tests.support.ddg_harness import baseline, corpus, methods, scoring

#: The cells 7.118.2 moves, as they are labelled in the baseline. A later
#: release that moves one of them again has to update this list and the
#: ``moved_by_ddg_2`` block with it.
MOVED_BY_DDG_2 = ("search:ddgs edge_html_200", "search:ddgs live_html_200")

#: The cells 7.119.0 moves (DDG-3), every one a ``search:legacy`` cell.
MOVED_BY_DDG_3 = tuple(
    f"search:legacy {key}"
    for key in (
        "challenge_html_200",
        "challenge_html_202",
        "challenge_lite_200",
        "challenge_lite_202",
        "edge_html_200",
        "http_403",
        "http_429",
        "http_500",
        "http_502",
        "http_503",
        "lite_results_200",
        "live_html_200",
    )
)

#: The cells 7.119.1 moves (DDG-3b): every ``search:legacy`` cell.
MOVED_BY_DDG_3B = tuple(
    f"search:legacy {key}"
    for key in (
        "challenge_html_200",
        "challenge_html_202",
        "challenge_lite_200",
        "challenge_lite_202",
        "edge_html_200",
        "empty_200",
        "garbage_200",
        "http_403",
        "http_429",
        "http_500",
        "http_502",
        "http_503",
        "lite_results_200",
        "live_html_200",
        "transport_refused",
        "transport_timeout",
    )
)


def _answer_of(document: dict, label: str, release: str) -> dict:
    """A cell's pinned answer as of ``release`` (7.119.0 or 7.119.1)."""

    method, key = label.split(" ", 1)
    later = document[baseline.DDG3B_KEY].get(label)
    if release == baseline.DDG3B_BEFORE and later is not None:
        return later[baseline.DDG3B_BEFORE]
    return document["cells"][method][key]


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
    # D3: the legacy scraper of 7.118.2 and before answered a challenge page
    # with an empty success and surfaced 403/429 as a raw httpx error, its
    # results carrying no snippet (title and url only) -- kept as the
    # before-answers of 7.119.0's moved cells.
    before = {
        label: entry[baseline.DDG3_BEFORE]["shape"]
        for label, entry in document[baseline.DDG3_KEY].items()
    }
    assert before["search:legacy challenge_lite_202"]["outcome"] == "empty"
    assert before["search:legacy http_429"]["error_class"] == "HTTPStatusError"
    assert set(before["search:legacy live_html_200"]["results"][0]) == {
        "title",
        "url",
    }
    # 7.119.0: the same pages read as what they are.
    assert legacy["challenge_lite_202"]["shape"]["kind"] == "rate_limit"
    assert legacy["http_429"]["shape"]["error_class"] == "WebSearchRateLimitError"
    assert set(legacy["live_html_200"]["shape"]["results"][0]) == {
        "title",
        "url",
        "snippet",
    }
    # web_fetch asked aiohttp for the encoding before reading the body, so a
    # response whose Content-Type has no charset failed outright. 7.120.0
    # reads the body first; DDG-1's pin is kept as the before-answer and the
    # page answers now.
    label = "fetch:web_fetch fetch_scenarios/html_without_charset"
    before = document[baseline.DDG4_KEY][label][baseline.DDG4_BEFORE]["shape"]
    assert before["error_class"] == "RuntimeError"
    assert fetch["fetch_scenarios/html_without_charset"]["shape"]["outcome"] == "ok"


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


# --------------------------------------------------------------- 7.119.0 DDG-3


def test_7_119_0_moves_only_the_twelve_legacy_cells_it_names() -> None:
    """Exactly these cells, every one ``search:legacy``, each with both answers.

    No ``ddgs`` and no ``web_fetch`` cell is among them: those are the
    byte-for-byte test's, pinned as 7.118.2 left them.
    """

    document = baseline.load_baseline()
    moved = document[baseline.DDG3_KEY]
    assert tuple(sorted(moved)) == MOVED_BY_DDG_3
    assert all(label.startswith("search:legacy ") for label in moved)
    assert set(moved).isdisjoint(document[baseline.DDG2_KEY])
    for label, entry in moved.items():
        assert set(entry) == {baseline.DDG3_BEFORE, baseline.DDG3_AFTER}, label
        # The after-answer is 7.119.0's: this tree's cell, or -- for a cell
        # 7.119.1 moved again -- the before-answer of ``moved_by_ddg_3b``.
        assert entry[baseline.DDG3_AFTER] == _answer_of(
            document, label, baseline.DDG3B_BEFORE
        ), label
        assert (
            entry[baseline.DDG3_BEFORE]["digest"]
            != entry[baseline.DDG3_AFTER]["digest"]
        ), label
        # The request the fake upstream saw is the one 7.118.2 sent.
        assert (
            entry[baseline.DDG3_BEFORE]["shape"]["requests"]
            == entry[baseline.DDG3_AFTER]["shape"]["requests"]
        ), label
    legacy = document["cells"]["search:legacy"]
    unmoved = {f"search:legacy {key}" for key in legacy} - set(moved)
    assert unmoved == {
        "search:legacy empty_200",
        "search:legacy garbage_200",
        "search:legacy transport_refused",
        "search:legacy transport_timeout",
    }


def test_7_119_0_reads_snippets_wherever_the_page_carries_them() -> None:
    """The three result pages: same results as before, now with snippets.

    The edge page also gains the row whose title link is direct (no
    ``uddg=``), which the old anchor scan could not see; both ads stay out.
    """

    moved = baseline.load_baseline()[baseline.DDG3_KEY]

    def answers(scenario: str) -> tuple[list[dict], list[dict]]:
        entry = moved[f"search:legacy {scenario}"]
        return (
            entry[baseline.DDG3_BEFORE]["shape"]["results"],
            entry[baseline.DDG3_AFTER]["shape"]["results"],
        )

    for scenario, with_snippet in (("live_html_200", 10), ("lite_results_200", 3)):
        before, after = answers(scenario)
        assert [(row["title"], row["url"]) for row in after] == [
            (row["title"], row["url"]) for row in before
        ], scenario
        assert sum(1 for row in after if row["snippet"]) == with_snippet, scenario
    before, after = answers("edge_html_200")
    assert len(before) == 3
    assert [row["url"] for row in after] == [
        "https://example.org/guide",
        "https://example.net/direct",
        "https://example.edu/no-snippet",
        "https://example.com/search?q=a+b&page=2",
    ]
    assert [bool(row["snippet"]) for row in after] == [True, True, False, True]


def test_7_119_0_reports_every_bot_check_and_status_as_what_it_is() -> None:
    """D3 fixed: a bot check is a rate limit naming the check, never an empty
    answer; a status is worded through the shared mapping; an honest empty
    page stays empty; and every scenario ends in the class its corpus declares.
    """

    cells = baseline.load_baseline()["cells"]["search:legacy"]
    for scenario in (
        "challenge_html_200",
        "challenge_html_202",
        "challenge_lite_200",
        "challenge_lite_202",
    ):
        shape = cells[scenario]["shape"]
        assert (shape["kind"], shape["kind_detail"]) == ("rate_limit", "challenge")
        assert "bot check instead of results" in shape["error_text"]
        assert ("HTTP 202" in shape["error_text"]) is scenario.endswith("_202")
    assert cells["http_403"]["shape"]["kind"] == "auth"
    assert cells["http_429"]["shape"]["kind"] == "rate_limit"
    for scenario in ("http_500", "http_502", "http_503"):
        shape = cells[scenario]["shape"]
        assert shape["kind"] == "upstream"
        assert shape["error_text"] == f"legacy: HTTP {scenario.removeprefix('http_')}"
    for scenario in ("empty_200", "garbage_200"):
        assert cells[scenario]["shape"]["outcome"] == "empty"
    by_id = {scenario.id: scenario for scenario in corpus.search_corpus().scenarios}
    assert {
        scenario: scoring.outcome_class(cell["shape"])
        for scenario, cell in cells.items()
    } == {scenario: by_id[scenario].expect for scenario in cells}


# -------------------------------------------------------------- 7.119.1 DDG-3b

#: What the fake upstream saw from 7.119.0: the lite page, once.
_LITE_REQUEST = {
    "method": "GET",
    "url": "https://lite.duckduckgo.com/lite/?q=omarchy+linux",
    "form": [],
}
#: What it sees first from 7.119.1: the html page's own form, q and b.
_HTML_REQUEST = {
    "method": "POST",
    "url": "https://html.duckduckgo.com/html/",
    "form": [["b", ""], ["q", "omarchy linux"]],
}


def test_7_119_1_moves_only_the_sixteen_legacy_cells_it_names() -> None:
    """Every ``search:legacy`` cell, each with both answers; no ``ddgs`` and no
    ``web_fetch`` cell -- those are the byte-for-byte test's, as 7.119.0 left
    them."""

    document = baseline.load_baseline()
    moved = document[baseline.DDG3B_KEY]
    assert tuple(sorted(moved)) == MOVED_BY_DDG_3B
    assert {label.split(" ", 1)[1] for label in moved} == set(
        document["cells"]["search:legacy"]
    )
    assert set(moved).isdisjoint(document[baseline.DDG2_KEY])
    for label, entry in moved.items():
        method, key = label.split(" ", 1)
        assert set(entry) == {baseline.DDG3B_BEFORE, baseline.DDG3B_AFTER}, label
        assert entry[baseline.DDG3B_AFTER] == document["cells"][method][key], label
        assert (
            entry[baseline.DDG3B_BEFORE]["digest"]
            != entry[baseline.DDG3B_AFTER]["digest"]
        ), label
    # 7.119.0's record of the cells it moved ends where this block begins.
    for label, entry in document[baseline.DDG3_KEY].items():
        assert entry[baseline.DDG3_AFTER] == moved[label][baseline.DDG3B_BEFORE]


def test_7_119_1_keeps_every_answer_and_asks_the_html_form_first() -> None:
    """R2: the html form first, the lite page only when it yields nothing.

    Each fake scenario serves its page to every request, so each answer --
    results, snippets, error class and text -- is 7.119.0's byte for byte;
    the request is what moved. The lite page follows the html form only on the
    two pages that hold nothing and no check; after a bot check, an HTTP error
    status or a transport failure on the html form, the hop asks nothing more.
    """

    moved = baseline.load_baseline()[baseline.DDG3B_KEY]
    second_try = {"search:legacy empty_200", "search:legacy garbage_200"}
    for label, entry in moved.items():
        before = entry[baseline.DDG3B_BEFORE]["shape"]
        after = entry[baseline.DDG3B_AFTER]["shape"]
        assert {
            name: value for name, value in before.items() if name != "requests"
        } == {name: value for name, value in after.items() if name != "requests"}, label
        assert before["requests"] == [_LITE_REQUEST], label
        expected = (
            [_HTML_REQUEST, _LITE_REQUEST] if label in second_try else [_HTML_REQUEST]
        )
        assert after["requests"] == expected, label


# -------------------------------------------------------------- 7.120.0 DDG-4

#: The ``web_fetch`` cells 7.120.0 leaves byte-identical, each for one of the
#: two reasons an extractor can leave an answer alone. Four are responses no
#: extractor reads at all; ten are HTML pages whose extraction was under the
#: declared threshold, where today's parser answered exactly as before.
UNMOVED_BY_DDG_4 = (
    # Not HTML, or an error status: decoded (or raised) as they always were.
    "fetch_scenarios/plain_text",
    "fetch_scenarios/json_body",
    "fetch_scenarios/http_404",
    "fetch_scenarios/http_403_cf_challenge",
    # HTML pages the extractor read as less than the declared threshold.
    "ddg_pages/mcc_html_ads_and_edge_cases.html",
    "ddg_pages/mcc_lite_challenge.html",
    "ddg_pages/octos_html_challenge_reconstructed.html",
    "fetch_scenarios/html_with_charset",
    "readability/mozilla/005-unescape-html-entities",
    "readability/mozilla/invalid-attributes",
    "readability/mozilla/js-link-replacement",
    "readability/mozilla/lazy-image-3",
    "readability/mozilla/ol",
    "trafilatura/scmp.com.playbook.html",
)


def test_7_120_0_moves_only_the_web_fetch_cells_it_names() -> None:
    """The moved set is every ``web_fetch`` cell but the fourteen named here.

    The cell universe is pinned by ``test_the_baseline_has_a_cell_for_every_
    fixture_and_method``, so this is an exact claim about which answers
    7.120.0 changes: the pages the extractor reads, plus the two no-charset
    entries DDG-1 pinned as failures. No ``search:*`` cell is among them.
    """

    document = baseline.load_baseline()
    moved = document[baseline.DDG4_KEY]
    fetch = document["cells"]["fetch:web_fetch"]

    assert {label.split(" ", 1)[1] for label in moved} == set(fetch) - set(
        UNMOVED_BY_DDG_4
    )
    assert all(label.startswith("fetch:web_fetch ") for label in moved)
    assert len(moved) == 285, len(moved)
    for key in (baseline.DDG2_KEY, baseline.DDG3_KEY, baseline.DDG3B_KEY):
        assert set(moved).isdisjoint(document[key]), key
    for label, entry in moved.items():
        method, key = label.split(" ", 1)
        assert set(entry) == {baseline.DDG4_BEFORE, baseline.DDG4_AFTER}, label
        # The after-answer is what this tree computes and pins in ``cells``.
        assert entry[baseline.DDG4_AFTER] == document["cells"][method][key], label
        assert (
            entry[baseline.DDG4_BEFORE]["digest"]
            != entry[baseline.DDG4_AFTER]["digest"]
        ), label


def test_7_120_0_the_unmoved_pages_are_the_ones_the_extractor_did_not_beat() -> None:
    """Every unmoved cell is one the new path answers exactly as before.

    "A page never gets worse" is R3's promise, and this is what makes the
    fourteen-name list a rule instead of a pile of exceptions. For every
    unmoved HTML page the new path's own output is recomputed here (fallback,
    threshold and marker included) and must hash to the pinned answer; and the
    reason it did not move is one of the two R3 allows: the extractor read the
    page as shorter than ``_WEB_FETCH_EXTRACTOR_MIN_CHARS`` (so today's parser
    answered), or its markdown is byte-identical to what today's parser
    returns (a page with nothing for markdown to add). A response that is not
    a successful HTML page is decoded or refused before any extraction.
    """

    pages = {page.key: page for page in corpus.all_extraction_pages()}
    pinned = baseline.load_baseline()["cells"]["fetch:web_fetch"]
    for key in UNMOVED_BY_DDG_4:
        page = pages[key]
        if page.status >= 400:
            continue  # refused before any extraction
        if "html" not in page.content_type:
            continue  # plain text / JSON: decoded, never extracted
        body = page.body.decode("utf-8", errors="replace")
        parser = HTMLTextParser()
        parser.feed(body)
        basic = "\n".join(parser.text_parts)
        data = fetch_extract.text_for(
            body, basic, extractor=fetch_extract.EXTRACTOR_TRAFILATURA
        )

        shape = pinned[key]["shape"]
        assert len(data) == shape["data_chars"], key
        assert (
            hashlib.sha256(data.encode("utf-8")).hexdigest() == (shape["data_sha256"])
        ), key

        extracted = fetch_extract.markdown(body) or ""
        assert (
            len(extracted.strip()) < _WEB_FETCH_EXTRACTOR_MIN_CHARS
            or extracted == basic
        ), key


def test_7_120_0_reads_the_pages_ddg_1_pinned_as_failures() -> None:
    """The no-charset fix: ``RuntimeError`` before, an extraction after.

    Both cells answer in both extractor modes -- the extractor's own text is
    shorter than the threshold on each, so today's parser answers and the two
    modes agree byte for byte.
    """

    document = baseline.load_baseline()
    for key in (
        "fetch_scenarios/html_without_charset",
        "fetch_scenarios/pdf_bytes",
    ):
        label = f"fetch:web_fetch {key}"
        before = document[baseline.DDG4_KEY][label][baseline.DDG4_BEFORE]["shape"]
        after = document["cells"]["fetch:web_fetch"][key]["shape"]
        assert before["error_class"] == "RuntimeError", key
        assert "Cannot compute fallback encoding" in before["error_text"], key
        assert after["outcome"] == "ok", key
        assert after["data_chars"] > 0, key


def test_7_120_0_changes_no_search_cell_at_all() -> None:
    """The fetch release leaves both search methods' answers where they were."""

    document = baseline.load_baseline()
    moved = set(document[baseline.DDG4_KEY])
    assert {label for label in moved if label.startswith("search:")} == set()
    assert set(document["cells"]["search:ddgs"]) and set(
        document["cells"]["search:legacy"]
    )
