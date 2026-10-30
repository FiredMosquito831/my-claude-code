"""``web_fetch``'s extractor (7.120.0): trafilatura, its fallback, and its costs.

RESEARCH-DDG-WEBSEARCH.md R3 and USER DECISIONS 2026-10-25 Q6-Q8/Q12 bind this
file:

* the default extractor is trafilatura, ``extract(html, output_format=
  "markdown", include_links=True, include_tables=True, include_comments=False)``,
  imported on first use only;
* a page whose extraction is empty or shorter than
  ``_WEB_FETCH_EXTRACTOR_MIN_CHARS`` falls back to today's parser, so a page
  never gets worse;
* the 24,000-character cap stays, and the page ends with a one-line marker
  naming the extracted total;
* the request sends an ``Accept`` header that prefers markdown and MCC's
  product User-Agent, and a Cloudflare challenge (403 + ``cf-mitigated:
  challenge``) is retried exactly once, on that answer only;
* ``WEB_FETCH_EXTRACTOR=basic`` reproduces the bytes every release before this
  one returned and loads no trafilatura at all.

Everything here is loopback and the committed corpora; no test reaches the
network (``tests/support/ddg_harness/methods.py``).
"""

import contextlib
import subprocess
import sys
from collections.abc import AsyncIterator, Callable, Iterator

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from pydantic import ValidationError

from my_claude_code.api.web_tools import fetch_extract, outbound
from my_claude_code.api.web_tools.constants import (
    _CLOUDFLARE_CHALLENGE_HEADER,
    _CLOUDFLARE_CHALLENGE_STATUS,
    _CLOUDFLARE_CHALLENGE_VALUE,
    _MAX_FETCH_CHARS,
    _WEB_FETCH_EXTRACTOR_MIN_CHARS,
    _WEB_FETCH_USER_AGENT,
)
from my_claude_code.api.web_tools.parsers import HTMLTextParser
from my_claude_code.config.settings import Settings, get_settings
from my_claude_code.core.version import package_version
from tests.support.ddg_harness import baseline, corpus, methods, offline, scoring

LOOPBACK = methods.LOOPBACK_EGRESS
TRAFILATURA = fetch_extract.EXTRACTOR_TRAFILATURA
BASIC = "basic"

ARTICLE = """<!DOCTYPE html><html><head><title>An article</title></head><body>
<nav>Home | About | Contact</nav>
<article>
  <h1>Batteries and the grid</h1>
  <p>Storage is the hinge of the transition. A <a href="https://example.org/report">report</a>
  from the operator explains why the curve bends.</p>
  <ul><li>First point</li><li>Second point</li></ul>
  <table><tr><th>Year</th><th>GWh</th></tr><tr><td>2025</td><td>120</td></tr></table>
</article>
<footer>Copyright 2026 Example Media. All rights reserved.</footer>
</body></html>"""


def basic_text(html: str) -> str:
    """Today's parser's answer for ``html`` (what every earlier release sent)."""

    parser = HTMLTextParser()
    parser.feed(html)
    return "\n".join(parser.text_parts)


@pytest.fixture
def extractor(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[str | None], None]]:
    """Choose ``WEB_FETCH_EXTRACTOR`` and forget the cached process settings."""

    def choose(value: str | None) -> None:
        if value is None:
            monkeypatch.delenv("WEB_FETCH_EXTRACTOR", raising=False)
        else:
            monkeypatch.setenv("WEB_FETCH_EXTRACTOR", value)
        get_settings.cache_clear()

    choose(None)
    yield choose
    get_settings.cache_clear()


# --------------------------------------------------------------------------- #
# The setting


def test_the_extractor_defaults_to_trafilatura_with_basic_as_the_escape_hatch() -> None:
    assert Settings.model_validate({}).web_fetch_extractor == TRAFILATURA
    assert (
        Settings.model_validate({"WEB_FETCH_EXTRACTOR": "basic"}).web_fetch_extractor
        == BASIC
    )
    assert (
        Settings.model_validate({"WEB_FETCH_EXTRACTOR": " BASIC "}).web_fetch_extractor
        == BASIC
    )
    # A cleared select is not a typo: it falls back to the shipped default.
    assert (
        Settings.model_validate({"WEB_FETCH_EXTRACTOR": ""}).web_fetch_extractor
        == TRAFILATURA
    )


def test_an_unknown_extractor_is_refused_at_load() -> None:
    with pytest.raises(ValidationError, match="WEB_FETCH_EXTRACTOR must be one of"):
        Settings.model_validate({"WEB_FETCH_EXTRACTOR": "readability"})


# --------------------------------------------------------------------------- #
# What the text is


def test_the_default_extractor_keeps_the_main_content_as_markdown() -> None:
    """R3's call: markdown, links and tables kept, what surrounds them gone."""

    data = fetch_extract.text_for(ARTICLE, basic_text(ARTICLE), extractor=TRAFILATURA)

    assert "# Batteries and the grid" in data
    assert "[report](https://example.org/report)" in data
    assert "| Year | GWh |" in data
    assert "- First point" in data
    # The page's surroundings are what R3 takes away.
    assert "Home | About" not in data
    assert "Copyright 2026" not in data


def test_a_page_the_extractor_cannot_read_falls_back_to_the_basic_parser() -> None:
    """R3: fewer than the declared characters is "nothing", and today answers."""

    thin = (
        "<html><head><title>t</title></head><body>"
        "<nav>Home | About | Contact</nav><p>Hi.</p>"
        "<footer>Copyright 2026 Example Media.</footer></body></html>"
    )
    extracted = fetch_extract.markdown(thin)
    # The extractor answers something, just not enough of it: the threshold,
    # not only a None answer, is what sends the page back to today's parser --
    # and the two answers differ, so a threshold that is ignored is visible.
    assert extracted is not None
    assert len(extracted.strip()) < _WEB_FETCH_EXTRACTOR_MIN_CHARS
    assert extracted != basic_text(thin)

    assert fetch_extract.text_for(
        thin, basic_text(thin), extractor=TRAFILATURA
    ) == basic_text(thin)


def test_a_page_at_the_threshold_is_kept() -> None:
    """The boundary belongs to the extractor, not to the fallback."""

    at_threshold = "x" * _WEB_FETCH_EXTRACTOR_MIN_CHARS
    kept = fetch_extract.capped(at_threshold)
    assert (
        fetch_extract.text_for("<html></html>", at_threshold, extractor=TRAFILATURA)
        == at_threshold
    )
    assert kept == at_threshold


def test_a_long_page_is_cut_at_the_cap_with_a_marker_naming_the_total() -> None:
    """R3/Q8: the 24,000 cap stays and the page says what it was cut from."""

    long_html = (
        "<html><head><title>Long</title></head><body><article>"
        + "".join(
            f"<p>Paragraph {index} with enough words to be kept by the "
            "extractor and to push the page past the cap.</p>"
            for index in range(600)
        )
        + "</article></body></html>"
    )
    extracted = fetch_extract.markdown(long_html)
    assert extracted is not None
    total = len(extracted)
    assert total > _MAX_FETCH_CHARS

    data = fetch_extract.text_for(
        long_html, basic_text(long_html), extractor=TRAFILATURA
    )

    assert len(data) <= _MAX_FETCH_CHARS
    assert data.startswith(extracted[:100])
    assert data.endswith(
        f"[web_fetch: truncated to {_MAX_FETCH_CHARS} of {total} characters extracted]"
    )
    assert data.count("[web_fetch: truncated") == 1


def test_the_basic_extractor_keeps_the_silent_cut_it_always_had() -> None:
    """``basic``'s bytes are the old bytes, marker included in that promise."""

    long_text = "y" * (_MAX_FETCH_CHARS + 500)
    data = fetch_extract.text_for("<html></html>", long_text, extractor=BASIC)

    assert data == long_text[:_MAX_FETCH_CHARS]
    assert "truncated" not in data


@pytest.mark.asyncio
async def test_a_non_html_body_is_still_cut_at_the_cap() -> None:
    """The extractor changed the HTML path only; plain text keeps its cap."""

    body = b"z" * (_MAX_FETCH_CHARS + 5_000)

    async with _loopback(lambda _n: (200, {}, body, "text/plain; charset=utf-8")) as (
        url,
        _requests,
    ):
        result = await outbound._run_web_fetch(url, LOOPBACK)

    assert result["data"] == "z" * _MAX_FETCH_CHARS
    assert "truncated" not in result["data"]


# --------------------------------------------------------------------------- #
# The request


@contextlib.asynccontextmanager
async def _loopback(
    responder: Callable[[int], tuple[int, dict[str, str], bytes, str]],
) -> AsyncIterator[tuple[str, list[dict[str, str]]]]:
    """Serve what ``responder`` answers and record every request's headers."""

    requests: list[dict[str, str]] = []

    async def handle(request: web.Request) -> web.Response:
        requests.append(
            {name.lower(): value for name, value in request.headers.items()}
        )
        status, headers, body, content_type = responder(len(requests))
        response = web.Response(status=status, body=body)
        response.headers["Content-Type"] = content_type
        for name, value in headers.items():
            response.headers[name] = value
        return response

    app = web.Application()
    app.router.add_get("/{tail:.*}", handle)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        yield f"http://127.0.0.1:{server.port}/page", requests
    finally:
        await server.close()


def _html(body: str) -> tuple[int, dict[str, str], bytes, str]:
    return 200, {}, body.encode("utf-8"), "text/html; charset=utf-8"


@pytest.mark.asyncio
async def test_the_request_asks_for_markdown_first_and_names_the_product() -> None:
    """R3's ``Accept`` and Q12's product User-Agent, on the wire."""

    async with _loopback(lambda _n: _html(ARTICLE)) as (url, requests):
        result = await outbound._run_web_fetch(url, LOOPBACK)

    (headers,) = requests
    assert headers["accept"].split(",")[0].strip() == "text/markdown;q=1.0"
    assert "text/html;q=0.7" in headers["accept"]
    assert headers["accept"].index("text/markdown") < headers["accept"].index(
        "text/html"
    )
    assert headers["user-agent"] == _WEB_FETCH_USER_AGENT
    assert headers["user-agent"] == f"my-claude-code/{package_version()}"
    assert "free-claude-code" not in headers["user-agent"]
    # And the answer is the extractor's, end to end.
    assert result["data"].startswith("# Batteries and the grid")


@pytest.mark.asyncio
async def test_a_cloudflare_challenge_is_retried_exactly_once() -> None:
    """R3/opencode: 403 + ``cf-mitigated: challenge`` gets one second request."""

    def challenge(_n: int) -> tuple[int, dict[str, str], bytes, str]:
        return (
            _CLOUDFLARE_CHALLENGE_STATUS,
            {_CLOUDFLARE_CHALLENGE_HEADER: _CLOUDFLARE_CHALLENGE_VALUE},
            b"<html><body>Just a moment...</body></html>",
            "text/html; charset=utf-8",
        )

    async with _loopback(challenge) as (url, requests):
        with pytest.raises(aiohttp.ClientResponseError):
            await outbound._run_web_fetch(url, LOOPBACK)

    # Two: the challenge, then the one honest retry. Never a third.
    assert len(requests) == 2
    # Both as MCC's product -- a browser is never impersonated (Q3/Q12).
    assert {headers["user-agent"] for headers in requests} == {_WEB_FETCH_USER_AGENT}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "headers"),
    [
        (403, {}),
        (403, {_CLOUDFLARE_CHALLENGE_HEADER: "managed"}),
        (429, {_CLOUDFLARE_CHALLENGE_HEADER: _CLOUDFLARE_CHALLENGE_VALUE}),
        (503, {}),
        (404, {}),
    ],
    ids=["403-plain", "403-managed", "429-with-the-header", "503", "404"],
)
async def test_only_the_cloudflare_challenge_is_retried(
    status: int, headers: dict[str, str]
) -> None:
    """Every other refusal is answered as it always was: one request, no retry."""

    def refuse(_n: int) -> tuple[int, dict[str, str], bytes, str]:
        return (
            status,
            headers,
            b"<html><body>no</body></html>",
            "text/html; charset=utf-8",
        )

    async with _loopback(refuse) as (url, requests):
        with pytest.raises(aiohttp.ClientResponseError):
            await outbound._run_web_fetch(url, LOOPBACK)

    assert len(requests) == 1


# --------------------------------------------------------------------------- #
# The no-charset fix DDG-1 pinned


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        (
            "text/html",
            b"<html><head><title>No charset</title></head><body>"
            b"<p>Served without a charset.</p></body></html>",
        ),
        ("application/pdf", b"%PDF-1.4\n%%EOF\n"),
    ],
    ids=["text-html", "application-pdf"],
)
async def test_a_response_without_a_charset_is_read_rather_than_refused(
    content_type: str, body: bytes
) -> None:
    """DDG-1 pinned a ``RuntimeError`` here; 7.120.0 answers utf-8, the body read."""

    def no_charset(_n: int) -> tuple[int, dict[str, str], bytes, str]:
        return 200, {}, body, content_type

    async with _loopback(no_charset) as (url, _requests):
        result = await outbound._run_web_fetch(url, LOOPBACK)

    assert result["data"]
    assert "�" not in result["data"] or content_type == "application/pdf"


# --------------------------------------------------------------------------- #
# The equality bar: basic is the old bytes


@pytest.mark.local_serial
def test_the_basic_extractor_reproduces_the_pinned_bytes(
    extractor: Callable[[str | None], None],
) -> None:
    """``WEB_FETCH_EXTRACTOR=basic`` answers 7.119.1's bytes on every page.

    The two no-charset cells are the exception, and the point: they *failed*
    before this release and answer in both modes now (``pdf_bytes`` decodes to
    replacement characters, exactly as a PDF did whenever a charset was
    declared). Every other cell is the pinned digest byte for byte, marker and
    all.
    """

    extractor(BASIC)
    pages = corpus.all_extraction_pages()
    with offline.guard():
        runs = methods.run_sync(methods.run_fetch_method("web_fetch", pages))
    document = baseline.load_baseline()
    # 7.119.1's answers: the DDG-4 block records them for every cell this
    # release moved; a cell it did not move is still its own pin.
    old = {
        label.split(" ", 1)[1]: entry[baseline.DDG4_BEFORE]
        for label, entry in document[baseline.DDG4_KEY].items()
    }
    pins = document["cells"]["fetch:web_fetch"]

    fixed = {
        "fetch_scenarios/html_without_charset",
        "fetch_scenarios/pdf_bytes",
    }
    moved = []
    for page in pages:
        if page.key in fixed:
            continue
        before = old.get(page.key, pins[page.key])
        digest = baseline.digest(runs[page.key].outcome)
        if digest != before["digest"]:
            moved.append(page.key)
    assert moved == [], f"basic must keep the old bytes; these moved: {moved}"

    for key in fixed:
        assert runs[key].outcome["outcome"] == "ok", key
        # The same answer the default extractor gives (the extractor's own
        # text is shorter than the threshold on both pages).
        assert len(runs[key].outcome["data"]) > 0, key


#: A fresh interpreter: import the extraction module, use the basic path, and
#: report what it loaded. ``python -c`` from a test is the startup import
#: contract's own shape (``tests/contracts/test_startup_import_cost.py``).
_BASIC_PROBE = "\n".join(
    (
        "import sys",
        "from my_claude_code.api.web_tools import fetch_extract",
        "data = fetch_extract.text_for(",
        "    '<html><body><p>hi</p></body></html>', 'hi', extractor='basic'",
        ")",
        "names = [n for n in sys.modules if n.split('.')[0] in {'trafilatura', 'lxml'}]",
        "print(data, names)",
    )
)


@pytest.mark.local_serial
def test_the_basic_path_loads_no_trafilatura(tmp_path) -> None:
    """The engine is imported by the trafilatura path only, and never at startup."""

    import os

    environment = dict(os.environ)
    environment["MCC_CONFIG_DIR"] = str(tmp_path / "config")
    (tmp_path / "config").mkdir()
    environment.pop("WEB_FETCH_EXTRACTOR", None)

    completed = subprocess.run(
        [sys.executable, "-c", _BASIC_PROBE],
        capture_output=True,
        check=False,
        text=True,
        env=environment,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert completed.stdout.strip() == "hi []", completed.stdout


# --------------------------------------------------------------------------- #
# Quality, on the corpus DDG-1 committed


@pytest.mark.local_serial
def test_the_extractor_reaches_the_research_quality_on_the_committed_corpus(
    extractor: Callable[[str | None], None], record_property: Callable[..., None]
) -> None:
    """The measured answer, against DDG-1's recorded 0.639 baseline.

    DDG-1 pinned what ``web_fetch`` extracted before this release on this
    corpus (``tests/contracts/ddg_harness_baseline.json``, 106 annotated
    pages): precision 0.487, recall 0.927, F1 0.639, 8,155 median characters.
    This release's default -- R3's own call, markdown with links and tables,
    capped at 24,000 -- is asserted below. The uncapped run is asserted beside
    it: the difference between the two is the cap, not the extractor (the
    research measured this call at F1 0.901 uncapped, 1.5's markdown row).
    """

    extractor(None)
    pages = corpus.trafilatura_pages()
    with offline.guard():
        runs = methods.run_sync(methods.run_fetch_method("web_fetch", pages))
    capped = scoring.summarise_extraction("trafilatura", pages, runs)

    uncapped_runs = {}
    for page in pages:
        markdown = fetch_extract.markdown(page.body.decode("utf-8", errors="replace"))
        uncapped_runs[page.key] = methods.Run(
            outcome={"outcome": "ok", "data": markdown or ""}, ms=0.0
        )
    uncapped = scoring.summarise_extraction("trafilatura", pages, uncapped_runs)

    for name, summary in (("capped", capped), ("uncapped", uncapped)):
        record_property(f"{name}_f1", round(summary.f1, 4))
        record_property(f"{name}_precision", round(summary.precision, 4))
        record_property(f"{name}_recall", round(summary.recall, 4))
        record_property(f"{name}_median_chars", summary.median_chars)
        print(
            f"trafilatura corpus, {name}: P {summary.precision:.3f} "
            f"R {summary.recall:.3f} F1 {summary.f1:.3f} "
            f"median {summary.median_chars} chars, under 200: "
            f"{summary.under_200_chars}, failures: {summary.failures}"
        )

    # Nothing fails, and the answer is in the research's class, not the old one.
    assert capped.failures == 0
    assert capped.f1 >= 0.87
    assert capped.precision >= 0.88
    assert capped.recall >= 0.86
    assert uncapped.f1 >= 0.89
    # About 55 % fewer characters for the same page (1.5's comparison): the
    # median must be far below DDG-1's recorded 8,155, and the cap must not be
    # what makes it small.
    assert capped.median_chars <= 5_000
    assert capped.under_200_chars == 0
