"""Turn harness outcomes into scores (pure functions over ``methods`` outcomes).

Search, per scenario (``score_search_cell``) and summed over the corpus
(``summarise_search``):

* results returned, against the page's real result count;
* destination URL correct -- decoded (not a ``duckduckgo.com/l/`` redirect or a
  ``y.js`` ad link), absolute, ``http``/``https``;
* title and snippet non-empty;
* ads excluded -- no result is a ``y.js`` link or lands on a page's declared ad
  host;
* challenge detected exactly when the page is a challenge (classified
  ``rate_limit``, USER DECISIONS Q4) and never on a normal page;
* the outcome class equals the class ``scenarios.json`` asks for.

Extraction, per corpus (``summarise_extraction``): precision / recall / F1
(trafilatura's with/without strings, as RESEARCH-DDG-WEBSEARCH.md 1.5 measured
them; python-readability's ideal summaries as a token multiset, micro-averaged,
the way that project's benchmark scores), median and p95 ms, median output
size, pages under 200 characters, failures.
"""

import math
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .corpus import ExtractionPage, SearchScenario
from .methods import Run

UNDER_CHARS = 200
_WHITESPACE = re.compile(r"\s+")
#: A word, with inner apostrophes (ASCII or U+2019) kept inside it.
_TOKEN = re.compile("\\w+(?:['\N{RIGHT SINGLE QUOTATION MARK}]\\w+)*", re.UNICODE)


# --------------------------------------------------------------------------- #
# Search


def destination_ok(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or not host:
        return False
    if host == "duckduckgo.com" or host.endswith(".duckduckgo.com"):
        return not (parts.path.startswith("/l/") or parts.path.startswith("/y.js"))
    return True


def is_ad(url: str, ad_hosts: tuple[str, ...]) -> bool:
    if "duckduckgo.com/y.js" in url:
        return True
    host = (urlsplit(url).hostname or "").lower()
    if host in ad_hosts:
        return True
    # An ad link that kept its redirect form still names its destination.
    for value in parse_qs(urlsplit(url).query).get("u3", []):
        if (urlsplit(value).hostname or "").lower() in ad_hosts:
            return True
    return False


def outcome_class(outcome: dict[str, Any]) -> str:
    """``results`` / ``empty`` for an answer, else the error's kind."""

    if outcome["outcome"] in {"results", "empty"}:
        return str(outcome["outcome"])
    return str(outcome.get("kind") or "error")


@dataclass(frozen=True, slots=True)
class SearchCell:
    scenario: str
    expect: str
    observed: str
    real_results: int
    returned: int
    url_ok: int
    titles: int
    snippets: int
    ads: int
    challenge_page: bool
    challenge_detected: bool
    ms: float

    @property
    def class_ok(self) -> bool:
        return self.observed == self.expect

    @property
    def challenge_ok(self) -> bool:
        return self.challenge_detected == self.challenge_page


def score_search_cell(scenario: SearchScenario, run: Run) -> SearchCell:
    outcome = run.outcome
    results = outcome.get("results", [])
    ad_hosts = scenario.page.ad_destination_hosts if scenario.page else ()
    observed = outcome_class(outcome)
    challenge_page = bool(scenario.page and scenario.page.challenge)
    return SearchCell(
        scenario=scenario.id,
        expect=scenario.expect,
        observed=observed,
        real_results=scenario.page.real_results if scenario.page else 0,
        returned=len(results),
        url_ok=sum(1 for row in results if destination_ok(str(row.get("url", "")))),
        titles=sum(1 for row in results if str(row.get("title") or "").strip()),
        snippets=sum(1 for row in results if str(row.get("snippet") or "").strip()),
        ads=sum(1 for row in results if is_ad(str(row.get("url", "")), ad_hosts)),
        challenge_page=challenge_page,
        challenge_detected=observed == "rate_limit" and not results,
        ms=run.ms,
    )


@dataclass(frozen=True, slots=True)
class SearchSummary:
    scenarios: int
    real_results: int
    returned: int
    url_ok: int
    titles: int
    snippets: int
    ads_leaked: int
    challenge_ok: int
    challenge_pages: int
    challenge_detected: int
    class_ok: int
    median_ms: float


def summarise_search(cells: list[SearchCell]) -> SearchSummary:
    return SearchSummary(
        scenarios=len(cells),
        real_results=sum(cell.real_results for cell in cells),
        returned=sum(cell.returned for cell in cells),
        url_ok=sum(cell.url_ok for cell in cells),
        titles=sum(cell.titles for cell in cells),
        snippets=sum(cell.snippets for cell in cells),
        ads_leaked=sum(cell.ads for cell in cells),
        challenge_ok=sum(1 for cell in cells if cell.challenge_ok),
        challenge_pages=sum(1 for cell in cells if cell.challenge_page),
        challenge_detected=sum(
            1 for cell in cells if cell.challenge_page and cell.challenge_detected
        ),
        class_ok=sum(1 for cell in cells if cell.class_ok),
        median_ms=statistics.median(cell.ms for cell in cells) if cells else 0.0,
    )


# --------------------------------------------------------------------------- #
# Extraction


def normalise_text(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


class _IdealText(HTMLParser):
    """The text of an ideal summary, ``<h1>`` left out (as the benchmark does)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"h1", "script", "style"}:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"h1", "script", "style"} and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def ideal_tokens(ideal_html: str) -> list[str]:
    parser = _IdealText()
    parser.feed(ideal_html)
    parser.close()
    return tokens(" ".join(parser.parts))


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)] if ordered else 0.0


def _prf(
    true_positive: int, false_positive: int, false_negative: int
) -> tuple[float, float, float]:
    precision = (
        true_positive / (true_positive + false_positive)
        if true_positive + false_positive
        else 0.0
    )
    recall = (
        true_positive / (true_positive + false_negative)
        if true_positive + false_negative
        else 0.0
    )
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return precision, recall, f1


@dataclass(frozen=True, slots=True)
class ExtractionSummary:
    corpus: str
    pages: int
    precision: float
    recall: float
    f1: float
    median_ms: float
    p95_ms: float
    median_chars: int
    under_200_chars: int
    failures: int
    true_positive: int
    false_positive: int
    false_negative: int


def summarise_extraction(
    corpus: str, pages: tuple[ExtractionPage, ...], runs: dict[str, Run]
) -> ExtractionSummary:
    """Score one corpus. Strings for trafilatura's set, tokens for readability's."""

    true_positive = false_positive = false_negative = 0
    lengths: list[int] = []
    times: list[float] = []
    failures = under = 0
    for page in pages:
        run = runs[page.key]
        times.append(run.ms)
        ok = run.outcome["outcome"] == "ok"
        output = normalise_text(str(run.outcome.get("data", ""))) if ok else ""
        if not ok:
            failures += 1
        lengths.append(len(output))
        if len(output) < UNDER_CHARS:
            under += 1
        if page.ideal_html is not None:
            actual = Counter(tokens(output))
            ideal = Counter(ideal_tokens(page.ideal_html))
            overlap = sum((actual & ideal).values())
            true_positive += overlap
            false_positive += sum(actual.values()) - overlap
            false_negative += sum(ideal.values()) - overlap
        else:
            for wanted in page.with_strings:
                if normalise_text(wanted) in output:
                    true_positive += 1
                else:
                    false_negative += 1
            for unwanted in page.without_strings:
                if normalise_text(unwanted) in output:
                    false_positive += 1
    precision, recall, f1 = _prf(true_positive, false_positive, false_negative)
    return ExtractionSummary(
        corpus=corpus,
        pages=len(pages),
        precision=precision,
        recall=recall,
        f1=f1,
        median_ms=statistics.median(times) if times else 0.0,
        p95_ms=_p95(times),
        median_chars=int(statistics.median(lengths)) if lengths else 0,
        under_200_chars=under,
        failures=failures,
        true_positive=true_positive,
        false_positive=false_positive,
        false_negative=false_negative,
    )
