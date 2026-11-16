"""Adapter plumb, non-mutation (M1), stub compat (M2), and additive metadata tests."""

import asyncio
from typing import Any

import pytest

from my_claude_code.core.websearch.models import WebSearchRequestOptions
from my_claude_code.websearch.adapters.brave import BraveWebSearchProvider
from my_claude_code.websearch.adapters.context7 import Context7WebSearchProvider
from my_claude_code.websearch.adapters.ddgs import DdgsWebSearchProvider
from my_claude_code.websearch.adapters.exa import ExaWebSearchProvider
from my_claude_code.websearch.adapters.firecrawl import FirecrawlWebSearchProvider
from my_claude_code.websearch.adapters.jina import JinaWebSearchProvider
from my_claude_code.websearch.adapters.linkup import LinkupWebSearchProvider
from my_claude_code.websearch.adapters.ollama import OllamaWebSearchProvider
from my_claude_code.websearch.adapters.parallel import ParallelWebSearchProvider
from my_claude_code.websearch.adapters.perplexity import PerplexityWebSearchProvider
from my_claude_code.websearch.adapters.searchapi import SearchApiWebSearchProvider
from my_claude_code.websearch.adapters.searxng import SearxngWebSearchProvider
from my_claude_code.websearch.adapters.serpapi import SerpApiWebSearchProvider
from my_claude_code.websearch.adapters.serper import SerperWebSearchProvider
from my_claude_code.websearch.adapters.tavily import TavilyWebSearchProvider
from tests.websearch.support import (
    StubWebSearchProvider,
    attach_mock_client,
    build_config,
    json_response,
    request_json_body,
)


@pytest.mark.asyncio
async def test_stub_provider_works_with_and_without_request_options() -> None:
    """M2 guard: legacy 6-arg _search_with_key doubles never receive TypeError."""
    stub = StubWebSearchProvider(build_config())
    default_resp = await stub.search("plain query")
    override_resp = await stub.search(
        "override query",
        request_options=WebSearchRequestOptions(time_range="week", country="us"),
    )
    assert len(default_resp.results) == 1
    assert len(override_resp.results) == 1
    assert len(stub.calls) == 2


@pytest.mark.asyncio
async def test_concurrent_and_followup_requests_never_mutate_cached_config() -> None:
    """M1 guard: cached Exa adapter preserves config options across interleaved requests."""
    config = build_config(
        options={
            "EXA_SEARCH_TYPE": "fast",
            "EXA_CONTENTS": "highlights",
            "EXA_USER_LOCATION": "US",
        }
    )
    provider = ExaWebSearchProvider(config)
    requests = attach_mock_client(
        provider,
        lambda req: json_response(
            {
                "requestId": "exa-req-1",
                "searchTime": 142.5,
                "costDollars": {"total": 0.005},
                "results": [
                    {
                        "title": "Exa Hit",
                        "url": "https://example.org/exa",
                        "highlights": ["snippet 1", "snippet 2"],
                        "highlightScores": [0.91, 0.77],
                        "score": 0.88,
                        "author": "Alice",
                        "favicon": "https://example.org/fav.ico",
                        "text": "Full extracted body",
                    }
                ],
            }
        ),
    )
    try:
        override = WebSearchRequestOptions(
            search_depth="deep",
            topic="news",
            country="de",
            include_content=True,
            start_date="2026-01-01",
            end_date="2026-02-01",
        )
        r_default_before, r_override, r_default_after = await asyncio.gather(
            provider.search("q0", max_results=3),
            provider.search("q1", max_results=3, request_options=override),
            provider.search("q2", max_results=3),
        )
    finally:
        await provider.close()

    bodies = {
        request_json_body(req)["query"]: request_json_body(req) for req in requests
    }
    assert bodies["q0"]["type"] == "fast"
    assert bodies["q0"]["userLocation"] == "US"
    assert bodies["q0"]["contents"] == {"highlights": True}
    assert "category" not in bodies["q0"]

    assert bodies["q1"]["type"] == "deep"
    assert bodies["q1"]["category"] == "news"
    assert bodies["q1"]["userLocation"] == "DE"
    assert bodies["q1"]["contents"] == {"highlights": True, "text": True}
    assert bodies["q1"]["startPublishedDate"] == "2026-01-01"
    assert bodies["q1"]["endPublishedDate"] == "2026-02-01"

    assert bodies["q2"] == {**bodies["q0"], "query": "q2"}
    assert config.options == {
        "EXA_SEARCH_TYPE": "fast",
        "EXA_CONTENTS": "highlights",
        "EXA_USER_LOCATION": "US",
    }

    item = r_override.results[0]
    assert item.score == 0.88
    assert item.author == "Alice"
    assert item.favicon == "https://example.org/fav.ico"
    assert item.highlights == ("snippet 1", "snippet 2")
    assert item.highlight_scores == (0.91, 0.77)
    assert r_override.request_id == "exa-req-1"
    assert r_override.provider_response_time_ms == 142.5
    assert r_default_before.results[0].score == 0.88
    assert r_default_after.results[0].site_name is None


@pytest.mark.asyncio
async def test_tavily_request_options_and_additive_metadata() -> None:
    provider = TavilyWebSearchProvider(
        build_config(
            options={"TAVILY_SEARCH_DEPTH": "basic", "TAVILY_TOPIC": "general"}
        )
    )
    requests = attach_mock_client(
        provider,
        lambda _req: json_response(
            {
                "request_id": "tv-123",
                "response_time": "1.67",
                "usage": {"credits": 2},
                "images": ["https://example.org/img.png"],
                "answer": "Synthesized answer",
                "results": [
                    {
                        "title": "Tavily Hit",
                        "url": "https://example.org/t",
                        "content": "Snippet",
                        "raw_content": "Full raw text",
                        "score": 0.92,
                        "favicon": "https://example.org/f.png",
                    }
                ],
            }
        ),
    )
    try:
        resp = await provider.search(
            "tavily query",
            max_results=4,
            request_options=WebSearchRequestOptions(
                search_depth="deep",
                topic="finance",
                time_range="week",
                country="gb",
                include_answer=True,
                raw_content_format="text",
                start_date="2026-01-01",
                end_date="2026-01-15",
            ),
        )
        default_resp = await provider.search("plain", max_results=4)
    finally:
        await provider.close()

    override_body = request_json_body(requests[0])
    default_body = request_json_body(requests[1])
    assert override_body["search_depth"] == "advanced"
    assert override_body["topic"] == "finance"
    assert override_body["time_range"] == "week"
    assert override_body["country"] == "gb"
    assert override_body["include_answer"] is True
    assert override_body["include_raw_content"] == "text"
    assert override_body["start_date"] == "2026-01-01"
    assert override_body["end_date"] == "2026-01-15"

    assert default_body["search_depth"] == "basic"
    assert default_body["topic"] == "general"
    assert "include_answer" not in default_body
    assert "include_raw_content" not in default_body

    assert resp.request_id == "tv-123"
    assert resp.provider_response_time_ms == 1670.0
    assert resp.usage_credits == 2.0
    assert resp.images == ("https://example.org/img.png",)
    assert resp.results[0].score == 0.92
    assert resp.results[0].favicon == "https://example.org/f.png"
    assert resp.results[0].published is None
    assert default_resp.results[0].published is None


@pytest.mark.asyncio
async def test_other_adapters_honor_options_without_mutating_defaults() -> None:
    opts = WebSearchRequestOptions(
        time_range="month",
        search_depth="deep",
        topic="news",
        country="ca",
        language="fr",
        include_content=True,
        include_answer=True,
        start_date="2026-01-05",
        end_date="2026-02-05",
    )

    brave = BraveWebSearchProvider(build_config())
    brave_reqs = attach_mock_client(
        brave,
        lambda _req: json_response(
            {
                "web": {
                    "results": [
                        {
                            "title": "B",
                            "url": "https://b.io",
                            "description": "D",
                            "extra_snippets": ["e1"],
                            "meta_url": {"favicon": "https://b.io/fav.ico"},
                        }
                    ]
                }
            }
        ),
    )

    linkup = LinkupWebSearchProvider(build_config())
    linkup_reqs = attach_mock_client(
        linkup,
        lambda _req: json_response(
            {
                "answer": "A",
                "sources": [{"name": "L", "url": "https://l.io", "snippet": "S"}],
            }
        ),
    )

    parallel = ParallelWebSearchProvider(build_config())
    parallel_reqs = attach_mock_client(
        parallel,
        lambda _req: json_response(
            {
                "search_id": "par_1",
                "results": [{"title": "P", "url": "https://p.io", "excerpts": ["x1"]}],
            }
        ),
    )

    perplexity = PerplexityWebSearchProvider(build_config())
    perplexity_reqs = attach_mock_client(
        perplexity,
        lambda _req: json_response(
            {
                "id": "pplx_1",
                "results": [{"title": "X", "url": "https://x.io", "snippet": "S"}],
            }
        ),
    )

    firecrawl = FirecrawlWebSearchProvider(build_config())
    firecrawl_reqs = attach_mock_client(
        firecrawl,
        lambda _req: json_response(
            {
                "data": {
                    "web": [{"title": "F", "url": "https://f.io", "description": "S"}]
                }
            }
        ),
    )

    jina = JinaWebSearchProvider(build_config())
    jina_reqs = attach_mock_client(
        jina,
        lambda _req: json_response(
            {"data": [{"title": "J", "url": "https://j.io", "content": "C"}]}
        ),
    )

    serper = SerperWebSearchProvider(build_config())
    serper_reqs = attach_mock_client(
        serper,
        lambda _req: json_response(
            {
                "credits": 1,
                "organic": [{"title": "S", "link": "https://s.io", "snippet": "D"}],
            }
        ),
    )

    searxng = SearxngWebSearchProvider(build_config(base_url="http://searx.local"))
    searxng_reqs = attach_mock_client(
        searxng,
        lambda _req: json_response(
            {
                "results": [
                    {
                        "title": "SX",
                        "url": "https://sx.io",
                        "content": "C",
                        "score": 1.5,
                    }
                ]
            }
        ),
    )

    searchapi = SearchApiWebSearchProvider(build_config())
    searchapi_reqs = attach_mock_client(
        searchapi,
        lambda _req: json_response(
            {
                "search_metadata": {"id": "sa_1", "total_time_taken": 0.42},
                "organic_results": [
                    {
                        "title": "SA",
                        "link": "https://sa.io",
                        "snippet": "D",
                        "favicon": "https://sa.io/fav.ico",
                        "source": "SA Source",
                    }
                ],
            }
        ),
    )

    serpapi = SerpApiWebSearchProvider(build_config())
    serpapi_reqs = attach_mock_client(
        serpapi,
        lambda _req: json_response(
            {
                "search_metadata": {"id": "sp_1", "total_time_taken": 0.55},
                "organic_results": [
                    {"title": "SP", "link": "https://sp.io", "snippet": "D"}
                ],
            }
        ),
    )

    context7 = Context7WebSearchProvider(build_config())
    attach_mock_client(
        context7,
        lambda req: json_response(
            {"results": [{"id": "/lib", "title": "Lib"}]}
            if "libs" in req.url.path
            else {
                "codeSnippets": [
                    {
                        "codeTitle": "Doc",
                        "codeId": "https://c7.io/doc",
                        "codeDescription": "D",
                        "codeList": [],
                    }
                ],
                "infoSnippets": [],
            }
        ),
    )

    ollama = OllamaWebSearchProvider(build_config())
    attach_mock_client(
        ollama,
        lambda _req: json_response(
            {"results": [{"title": "O", "url": "https://o.io", "content": "C"}]}
        ),
    )

    try:
        b_resp = await brave.search("q", request_options=opts)
        l_resp = await linkup.search("q", request_options=opts)
        par_resp = await parallel.search("q", request_options=opts)
        pplx_resp = await perplexity.search("q", request_options=opts)
        fc_resp = await firecrawl.search("q", request_options=opts)
        j_resp = await jina.search("q", request_options=opts)
        sp_resp = await serper.search("q", request_options=opts)
        sx_resp = await searxng.search("q", request_options=opts)
        sa_resp = await searchapi.search("q", request_options=opts)
        serp_resp = await serpapi.search("q", request_options=opts)
        c7_resp = await context7.search("q", request_options=opts)
        ol_resp = await ollama.search("q", request_options=opts)
        # Follow-up default search on each confirms zero config leakage.
        await brave.search("q2")
        await serper.search("q2")
    finally:
        for prov in (
            brave,
            linkup,
            parallel,
            perplexity,
            firecrawl,
            jina,
            serper,
            searxng,
            searchapi,
            serpapi,
            context7,
            ollama,
        ):
            await prov.close()

    assert brave_reqs[0].url.params["freshness"] == "pm"
    assert brave_reqs[0].url.params["country"] == "CA"
    assert brave_reqs[0].url.params["search_lang"] == "fr"
    assert brave_reqs[0].url.params["extra_snippets"] == "true"
    assert "freshness" not in brave_reqs[1].url.params
    assert b_resp.results[0].favicon == "https://b.io/fav.ico"
    assert b_resp.results[0].highlights == ("e1",)

    assert request_json_body(linkup_reqs[0])["depth"] == "deep"
    assert request_json_body(linkup_reqs[0])["outputType"] == "sourcedAnswer"
    assert l_resp.answer == "A"

    assert request_json_body(parallel_reqs[0])["mode"] == "advanced"
    assert request_json_body(parallel_reqs[0])["advanced_settings"]["location"] == "CA"
    assert par_resp.request_id == "par_1"
    assert par_resp.results[0].highlights == ("x1",)

    pplx_body = request_json_body(perplexity_reqs[0])
    assert pplx_body["search_recency_filter"] == "month"
    assert pplx_body["search_context_size"] == "high"
    assert pplx_body["country"] == "CA"
    assert pplx_body["search_language_filter"] == ["fr"]
    assert pplx_body["search_after_date_filter"] == "01/05/2026"
    assert pplx_body["search_before_date_filter"] == "02/05/2026"
    assert pplx_resp.request_id == "pplx_1"

    fc_body = request_json_body(firecrawl_reqs[0])
    assert fc_body["country"] == "CA"
    assert fc_body["tbs"] == "qdr:m"
    assert fc_body["scrapeOptions"] == {"formats": [{"type": "markdown"}]}
    assert fc_resp.results[0].url == "https://f.io"

    assert jina_reqs[0].url.params["gl"] == "ca"
    assert j_resp.results[0].url == "https://j.io"

    assert request_json_body(serper_reqs[0])["gl"] == "ca"
    assert request_json_body(serper_reqs[0])["hl"] == "fr"
    assert request_json_body(serper_reqs[0])["tbs"] == "qdr:m"
    assert "gl" not in request_json_body(serper_reqs[1])
    assert sp_resp.usage_credits == 1.0

    assert searxng_reqs[0].url.params["time_range"] == "month"
    assert searxng_reqs[0].url.params["language"] == "fr"
    assert searxng_reqs[0].url.params["categories"] == "news"
    assert sx_resp.results[0].score == 1.5

    assert searchapi_reqs[0].url.params["time_period"] == "last_month"
    assert searchapi_reqs[0].url.params["gl"] == "ca"
    assert searchapi_reqs[0].url.params["hl"] == "fr"
    assert sa_resp.request_id == "sa_1"
    assert sa_resp.provider_response_time_ms == 420.0
    assert sa_resp.results[0].site_name == "SA Source"

    assert serpapi_reqs[0].url.params["tbs"] == "qdr:m"
    assert serpapi_reqs[0].url.params["gl"] == "ca"
    assert serpapi_reqs[0].url.params["hl"] == "fr"
    assert serp_resp.request_id == "sp_1"
    assert serp_resp.provider_response_time_ms == 550.0

    assert c7_resp.results[0].url == "https://c7.io/doc"
    assert ol_resp.results[0].url == "https://o.io"


@pytest.mark.asyncio
async def test_ddgs_request_options_and_zero_override_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[Any, ...], Any]] = []

    def fake_text(
        self: DdgsWebSearchProvider, query: str, max_results: int
    ) -> list[dict]:
        calls.append(((query, max_results), None))
        return [{"title": "D0", "href": "https://d0.io", "body": "B0"}]

    def fake_text_opts(
        self: DdgsWebSearchProvider,
        query: str,
        max_results: int,
        request_options: WebSearchRequestOptions | None = None,
    ) -> list[dict]:
        calls.append(((query, max_results), request_options))
        return [{"title": "D1", "href": "https://d1.io", "body": "B1"}]

    monkeypatch.setattr(DdgsWebSearchProvider, "_run_text_search", fake_text)
    monkeypatch.setattr(
        DdgsWebSearchProvider, "_run_text_search_options", fake_text_opts
    )

    ddgs = DdgsWebSearchProvider(build_config())
    r0 = await ddgs.search("zero", max_results=5)
    opts = WebSearchRequestOptions(time_range="day")
    r1 = await ddgs.search("one", max_results=5, request_options=opts)

    assert r0.results[0].url == "https://d0.io"
    assert r1.results[0].url == "https://d1.io"
    assert calls == [(("zero", 5), None), (("one", 5), opts)]
