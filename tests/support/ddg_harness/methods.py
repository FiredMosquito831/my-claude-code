"""MCC's real search and fetch code, driven over recorded pages with no network.

Three methods exist today; later DDG PRs register theirs in ``SEARCH_METHODS``
or ``FETCH_METHODS`` and the table and baseline pick them up:

* ``ddgs`` -- the ``ddgs`` provider path: MCC's real ``DdgsWebSearchProvider``
  (``websearch/adapters/ddgs.py``) with ``DDGS_BACKEND=duckduckgo``, the real
  ``ddgs`` package and its real ``HttpClient2.request`` (status handling and
  exception mapping). Only the HTTP transport underneath is replaced: every
  ``HttpClient2`` gets an ``httpx.MockTransport`` that serves the scenario.
  The backend is pinned because ``auto`` draws engines at random and every
  recorded page is a DuckDuckGo page.
* ``legacy`` -- the last hop of the ``auto`` route as that route runs it,
  ``outbound._legacy_route_scrape``: the lite-endpoint request of
  ``outbound._legacy_web_search_scrape`` read by the declared page profiles
  (``api/web_tools/duckduckgo_lite.py``, ``config/duckduckgo_profiles.py``;
  7.119.0, DDG-3 -- before it, ``SearchResultParser`` alone), with an HTTP
  error status worded through the shared status mapping, and
  ``httpx.AsyncClient`` handed a ``MockTransport``; a failure is classified
  with the route's own ``outbound._search_error_kind``.
* ``web_fetch`` -- ``outbound._run_web_fetch`` end to end with real aiohttp,
  against a loopback ``aiohttp`` server that serves each page with its status,
  ``Content-Type`` and headers; the egress policy allows the loopback target
  and nothing else (``http`` only).

Every outcome is a plain JSON-able dict, so ``baseline`` can digest it; the
loopback origin is replaced by ``FIXTURE_ORIGIN`` so a digest never depends on
the port. Wall-clock times are returned beside the outcome, never inside it.
"""

import asyncio
import contextlib
import hashlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from ddgs import http_client2

from my_claude_code.api.web_tools import outbound
from my_claude_code.api.web_tools.egress import WebFetchEgressPolicy
from my_claude_code.websearch.adapters.ddgs import DdgsWebSearchProvider
from my_claude_code.websearch.base import WebSearchProviderConfig
from my_claude_code.websearch.errors import WebSearchError

from .corpus import ExtractionPage, SearchScenario, search_corpus

#: What the loopback origin (``http://127.0.0.1:<port>``) reads as in outcomes.
FIXTURE_ORIGIN = "http://fixture.test"
#: The registry's constant ddgs timeout (``websearch/registry.py``).
DDGS_HTTP_TIMEOUT_S = 20.0
#: ``web_fetch`` with the loopback fixture server as its only reachable target.
LOOPBACK_EGRESS = WebFetchEgressPolicy(
    allow_private_network_targets=True, allowed_schemes=frozenset({"http"})
)

type Outcome = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Run:
    outcome: Outcome
    ms: float


# --------------------------------------------------------------------------- #
# The fake upstream for both search paths


def _fake_handler(
    scenario: SearchScenario, requests: list[dict[str, Any]]
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        form = (
            sorted(parse_qsl(request.content.decode("utf-8"), keep_blank_values=True))
            if request.content
            else []
        )
        requests.append(
            {"method": request.method, "url": str(request.url), "form": form}
        )
        if scenario.transport == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if scenario.transport == "refused":
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(
            scenario.status or 200,
            content=scenario.body,
            headers={"content-type": "text/html; charset=utf-8"},
            request=request,
        )

    return handler


@contextlib.contextmanager
def _ddgs_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Iterator[None]:
    """Every ``ddgs`` ``HttpClient2`` built inside gets the mock transport."""

    def init(
        self: http_client2.HttpClient2,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
        timeout: int | None = 10,
        *,
        verify: bool | str = True,
    ) -> None:
        # The real constructor only builds an httpx.Client (proxy, TLS context,
        # HTTP/2); the mock transport stands in for all of it. ``request`` --
        # the status and exception handling -- stays the library's own.
        del proxy, verify
        self.client = httpx.Client(
            headers=headers,
            timeout=timeout,
            follow_redirects=False,
            transport=httpx.MockTransport(handler),
        )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(http_client2.HttpClient2, "__init__", init)
        yield


@contextlib.contextmanager
def _legacy_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Iterator[None]:
    real_async_client = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(outbound.httpx, "AsyncClient", factory)
        yield


def _error_fields(error: BaseException, kind: str) -> Outcome:
    detail = error.kind_detail if isinstance(error, WebSearchError) else None
    return {
        "outcome": "error",
        "kind": kind,
        "kind_detail": detail,
        "error_class": type(error).__name__,
        "error_text": str(error),
    }


async def run_ddgs(scenario: SearchScenario, query: str, max_results: int) -> Run:
    requests: list[dict[str, Any]] = []
    config = WebSearchProviderConfig(
        api_keys=(),
        credential_rotation="single",
        base_url=None,
        proxy=None,
        http_timeout=DDGS_HTTP_TIMEOUT_S,
        options={"DDGS_BACKEND": "duckduckgo"},
    )
    started = time.perf_counter()
    with _ddgs_transport(_fake_handler(scenario, requests)):
        try:
            response = await DdgsWebSearchProvider(config).search(
                query, max_results=max_results
            )
        except WebSearchError as error:
            outcome = _error_fields(error, error.kind)
        else:
            results = [
                {
                    "title": item.title,
                    "url": item.url,
                    "snippet": item.snippet,
                    "content": item.content,
                    "published": item.published,
                }
                for item in response.results
            ]
            outcome = {"outcome": "results" if results else "empty", "results": results}
    ms = (time.perf_counter() - started) * 1000.0
    outcome["requests"] = requests
    return Run(outcome=outcome, ms=ms)


async def run_legacy(scenario: SearchScenario, query: str, max_results: int) -> Run:
    del max_results  # the scraper applies MCC's own cap (constants._MAX_SEARCH_RESULTS)
    requests: list[dict[str, Any]] = []
    started = time.perf_counter()
    with _legacy_transport(_fake_handler(scenario, requests)):
        try:
            rows = await outbound._legacy_route_scrape(query)
        except Exception as error:
            outcome = _error_fields(error, outbound._search_error_kind(error))
        else:
            results = [dict(row) for row in rows]
            outcome = {"outcome": "results" if results else "empty", "results": results}
    ms = (time.perf_counter() - started) * 1000.0
    outcome["requests"] = requests
    return Run(outcome=outcome, ms=ms)


type SearchMethod = Callable[[SearchScenario, str, int], Awaitable[Run]]

SEARCH_METHODS: dict[str, SearchMethod] = {
    "ddgs": run_ddgs,
    "legacy": run_legacy,
}


# --------------------------------------------------------------------------- #
# The loopback fetch server


def page_path(page: ExtractionPage) -> str:
    """A stable, ASCII path for a page (corpus names hold non-ASCII characters)."""

    return f"/{page.corpus}/{hashlib.sha256(page.name.encode()).hexdigest()[:20]}"


@dataclass(slots=True)
class FetchServer:
    origin: str
    pages: dict[str, ExtractionPage]

    def url_for(self, page: ExtractionPage) -> str:
        return f"{self.origin}{page_path(page)}"

    def normalise(self, value: str) -> str:
        return value.replace(self.origin, FIXTURE_ORIGIN)


@contextlib.asynccontextmanager
async def fetch_server(pages: tuple[ExtractionPage, ...]) -> AsyncIterator[FetchServer]:
    """Serve ``pages`` on 127.0.0.1 (an ephemeral port) for ``web_fetch``."""

    by_path = {page_path(page): page for page in pages}

    async def handle(request: web.Request) -> web.Response:
        page = by_path.get(request.path)
        if page is None:
            return web.Response(status=599, text="harness: unknown fixture path")
        response = web.Response(status=page.status, body=page.body)
        response.headers["Content-Type"] = page.content_type
        for name, value in page.headers:
            response.headers[name] = value
        return response

    app = web.Application()
    app.router.add_get("/{tail:.*}", handle)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        yield FetchServer(origin=f"http://127.0.0.1:{server.port}", pages=by_path)
    finally:
        await server.close()


async def run_web_fetch(page: ExtractionPage, server: FetchServer) -> Run:
    started = time.perf_counter()
    try:
        result = await outbound._run_web_fetch(server.url_for(page), LOOPBACK_EGRESS)
    except Exception as error:
        outcome: Outcome = {
            "outcome": "error",
            "error_class": type(error).__name__,
            "error_text": server.normalise(str(error)),
        }
    else:
        outcome = {
            "outcome": "ok",
            "url": server.normalise(result["url"]),
            "title": server.normalise(result["title"]),
            "media_type": result["media_type"],
            "data": server.normalise(result["data"]),
        }
    ms = (time.perf_counter() - started) * 1000.0
    return Run(outcome=outcome, ms=ms)


type FetchMethod = Callable[[ExtractionPage, FetchServer], Awaitable[Run]]

FETCH_METHODS: dict[str, FetchMethod] = {
    "web_fetch": run_web_fetch,
}


# --------------------------------------------------------------------------- #
# Running a whole corpus


async def run_search_method(name: str) -> dict[str, Run]:
    corpus = search_corpus()
    method = SEARCH_METHODS[name]
    return {
        scenario.id: await method(scenario, corpus.query, corpus.max_results)
        for scenario in corpus.scenarios
    }


async def run_fetch_method(
    name: str, pages: tuple[ExtractionPage, ...]
) -> dict[str, Run]:
    method = FETCH_METHODS[name]
    runs: dict[str, Run] = {}
    async with fetch_server(pages) as server:
        for page in pages:
            runs[page.key] = await method(page, server)
    return runs


def run_sync[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Run a harness coroutine from synchronous code (a test, a script)."""

    return asyncio.run(coroutine)
