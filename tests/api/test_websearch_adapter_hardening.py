"""Regression and equality contract tests for Track 5 PR1 (v8.32.1).

Covers:
1. Authoritative search quota benching (402, 432, 433, WebSearchQuotaError) in
   both chain mode (SearchKeyBooks + drive_keys) and no-chain mode.
2. SearchAPI / SerpAPI HTTP-200 JSON error classification and key rotation.
3. SearXNG 403 upstream_body preservation and DDGS unexpected-exception wrap.
4. Parametrized JSON null / non-dict row hardening across all JSON adapters.
"""

from typing import Any

import httpx
import pytest

from my_claude_code.api.web_tools.search_books import (
    SearchBooksPolicy,
    SearchKeyBooks,
)
from my_claude_code.api.web_tools.search_chain import drive_keys
from my_claude_code.api.web_tools.search_key_charge import no_chain_key_charge
from my_claude_code.config.settings import Settings
from my_claude_code.core.websearch.models import WebSearchRequestOptions
from my_claude_code.websearch.adapters.brave import BraveWebSearchProvider
from my_claude_code.websearch.adapters.ddgs import DdgsWebSearchProvider
from my_claude_code.websearch.adapters.deepseek import DeepSeekWebSearchProvider
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
from my_claude_code.websearch.base import BaseWebSearchProvider, WebSearchProviderConfig
from my_claude_code.websearch.errors import (
    WebSearchAuthError,
    WebSearchUpstreamError,
)
from tests.contracts.search_chat_parity_world import fake_limiter_time
from tests.support.websearch_credentials import NO_WEB_SEARCH_CREDENTIALS
from tests.websearch.support import attach_mock_client, build_config, json_response


def _settings(**values: str) -> Settings:
    return Settings.model_validate({**NO_WEB_SEARCH_CREDENTIALS, **values})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_cls", "provider_id", "options", "payload"),
    [
        (BraveWebSearchProvider, "brave", {}, {"web": None}),
        (BraveWebSearchProvider, "brave", {}, {"web": {"results": None}}),
        (
            BraveWebSearchProvider,
            "brave",
            {"BRAVE_SEARCH_MODE": "llm-context"},
            {"grounding": None},
        ),
        (
            BraveWebSearchProvider,
            "brave",
            {"BRAVE_SEARCH_MODE": "llm-context"},
            {"grounding": {"generic": None}},
        ),
        (DeepSeekWebSearchProvider, "deepseek", {}, {"results": None}),
        (
            ExaWebSearchProvider,
            "exa",
            {},
            {"results": None, "costDollars": None},
        ),
        (
            ExaWebSearchProvider,
            "exa",
            {},
            {"results": ["bad", None], "costDollars": {"total": None}},
        ),
        (FirecrawlWebSearchProvider, "firecrawl", {}, {"data": None}),
        (
            FirecrawlWebSearchProvider,
            "firecrawl",
            {"FIRECRAWL_SOURCES": "web,news,images"},
            {"data": {"web": None, "news": None, "images": None}},
        ),
        (JinaWebSearchProvider, "jina", {}, {"data": None}),
        (LinkupWebSearchProvider, "linkup", {}, {"results": None}),
        (
            LinkupWebSearchProvider,
            "linkup",
            {"LINKUP_OUTPUT_TYPE": "sourcedAnswer"},
            {"answer": "ok", "sources": None},
        ),
        (OllamaWebSearchProvider, "ollama", {}, {"results": None}),
        (ParallelWebSearchProvider, "parallel", {}, {"results": None}),
        (PerplexityWebSearchProvider, "perplexity", {}, {"results": None}),
        (SearchApiWebSearchProvider, "searchapi", {}, {"organic_results": None}),
        (SearxngWebSearchProvider, "searxng", {}, {"results": None}),
        (SerpApiWebSearchProvider, "serpapi", {}, {"organic_results": None}),
        (
            SerperWebSearchProvider,
            "serper",
            {},
            {"organic": None, "peopleAlsoAsk": None},
        ),
        (
            TavilyWebSearchProvider,
            "tavily",
            {},
            {"results": None, "usage": None},
        ),
    ],
)
async def test_json_adapters_handle_null_lists_and_nested_nulls(
    adapter_cls: type[BaseWebSearchProvider],
    provider_id: str,
    options: dict[str, str],
    payload: dict[str, Any],
) -> None:
    config = build_config(
        api_keys=() if provider_id == "searxng" else ("test-key-11112222",),
        rotation="single",
        base_url="https://mock.test" if provider_id == "searxng" else None,
        options=options,
    )
    provider = adapter_cls(config)
    attach_mock_client(provider, lambda request: json_response(payload))
    try:
        response = await provider.search("null json test")
        assert response.provider == provider_id
        assert response.results == ()
        if provider_id == "deepseek":
            assert response.cost_usd == 0.00075
        else:
            assert response.cost_usd is None
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_linkup_sourced_answer_with_null_sources_via_request_options() -> None:
    provider = LinkupWebSearchProvider(build_config())
    attach_mock_client(
        provider, lambda request: json_response({"answer": "hi", "sources": None})
    )
    try:
        response = await provider.search(
            "q", request_options=WebSearchRequestOptions(include_answer=True)
        )
        assert response.results == ()
        assert response.answer == "hi"
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_cls", "provider_id", "fail_status", "fail_body"),
    [
        (
            ExaWebSearchProvider,
            "exa",
            402,
            {
                "error": (
                    "You have exceeded your credits limit. Please top up to keep "
                    "using Exa at dashboard.exa.ai"
                )
            },
        ),
        (
            TavilyWebSearchProvider,
            "tavily",
            432,
            {"detail": {"error": "plan usage limit exceeded"}},
        ),
        (
            TavilyWebSearchProvider,
            "tavily",
            433,
            {"detail": {"error": "pay-as-you-go usage limit reached"}},
        ),
    ],
)
async def test_chain_books_bench_quota_key_and_route_directly_to_key1(
    adapter_cls: type[BaseWebSearchProvider],
    provider_id: str,
    fail_status: int,
    fail_body: dict[str, Any],
) -> None:
    keys = (f"{provider_id}-dead-key-0000", f"{provider_id}-live-key-1111")
    seen_keys: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if provider_id == "exa":
            sent = request.headers["x-api-key"]
        else:
            sent = request.headers["authorization"].removeprefix("Bearer ")
        idx = keys.index(sent)
        seen_keys.append(idx)
        if idx == 0:
            return httpx.Response(fail_status, json=fail_body, request=request)
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "ok",
                        "url": "https://example.invalid/ok",
                        "content": "ok",
                        "highlights": ["ok"],
                    }
                ]
            },
            request=request,
        )

    provider = adapter_cls(
        WebSearchProviderConfig(
            api_keys=keys,
            credential_rotation="failover",
            base_url=f"https://{provider_id}.test.invalid",
            proxy=None,
            http_timeout=20.0,
        )
    )
    attach_mock_client(provider, handler)
    policy = SearchBooksPolicy.from_settings(_settings(), "failover")
    books = SearchKeyBooks(
        provider_id,
        keys,
        tuple(provider.key_label(i) for i in range(len(keys))),
        policy,
    )

    try:
        with fake_limiter_time():
            r1 = await drive_keys(
                books,
                lambda idx, key: provider.search_once("q", key, idx, max_results=5),
            )
            assert r1.error is None
            assert r1.key_index == 1
            assert seen_keys == [0, 1]
            metrics = books.state.get_metrics()
            assert metrics[0]["cooldown_remaining"] > 0
            assert metrics[1]["cooldown_remaining"] == 0

            r2 = await drive_keys(
                books,
                lambda idx, key: provider.search_once("q2", key, idx, max_results=5),
            )
            assert r2.error is None
            assert r2.key_index == 1
            # Key 0 was benched on search 1, so search 2 must NEVER hit Key 0.
            assert seen_keys == [0, 1, 1]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_no_chain_pool_benches_exa_402_and_skips_dead_key() -> None:
    keys = ("exa-dead-key-0000", "exa-live-key-1111")
    seen_keys: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        idx = keys.index(request.headers["x-api-key"])
        seen_keys.append(idx)
        if idx == 0:
            return httpx.Response(
                402,
                json={"error": "You have exceeded your credits limit."},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "ok",
                        "url": "https://example.invalid/ok",
                        "highlights": ["ok"],
                    }
                ]
            },
            request=request,
        )

    provider = ExaWebSearchProvider(build_config(api_keys=keys, rotation="failover"))
    attach_mock_client(provider, handler)
    try:
        res1 = await provider.search("q1", key_charge=no_chain_key_charge)
        assert res1.key_index == 1
        res2 = await provider.search("q2", key_charge=no_chain_key_charge)
        assert res2.key_index == 1
        assert seen_keys == [0, 1, 1]
        snap = provider.key_pool.snapshot()
        assert snap["keys"][0]["state"] != "healthy"
        assert snap["keys"][1]["state"] == "healthy"
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_cls", "provider_id"),
    [
        (SearchApiWebSearchProvider, "searchapi"),
        (SerpApiWebSearchProvider, "serpapi"),
    ],
)
@pytest.mark.parametrize(
    ("error_msg", "expected_charge"),
    [
        ("Invalid API key", "auth"),
        ("Your account has run out of searches", "quota"),
        ("Rate limit exceeded", "rate_limit"),
    ],
)
async def test_searchapi_serpapi_200_error_rotates_and_benches_in_chain_and_no_chain(
    adapter_cls: type[BaseWebSearchProvider],
    provider_id: str,
    error_msg: str,
    expected_charge: str,
) -> None:
    keys = (f"{provider_id}-dead-0000", f"{provider_id}-live-1111")
    seen_keys: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        idx = keys.index(request.url.params["api_key"])
        seen_keys.append(idx)
        if idx == 0:
            return httpx.Response(200, json={"error": error_msg}, request=request)
        return httpx.Response(
            200,
            json={
                "organic_results": [
                    {
                        "title": "ok",
                        "link": "https://example.invalid/ok",
                        "snippet": "ok",
                    }
                ]
            },
            request=request,
        )

    provider = adapter_cls(
        WebSearchProviderConfig(
            api_keys=keys,
            credential_rotation="failover",
            base_url=f"https://{provider_id}.test.invalid",
            proxy=None,
            http_timeout=20.0,
        )
    )
    attach_mock_client(provider, handler)
    policy = SearchBooksPolicy.from_settings(_settings(), "failover")
    books = SearchKeyBooks(
        provider_id,
        keys,
        tuple(provider.key_label(i) for i in range(len(keys))),
        policy,
    )
    try:
        with fake_limiter_time():
            r1 = await drive_keys(
                books,
                lambda idx, key: provider.search_once("q", key, idx, max_results=5),
            )
            assert r1.error is None
            assert r1.key_index == 1
            assert [row["charged"] for row in r1.tries if "charged" in row] == [
                expected_charge
            ]

            r2 = await drive_keys(
                books,
                lambda idx, key: provider.search_once("q2", key, idx, max_results=5),
            )
            assert r2.error is None
            assert r2.key_index == 1
            assert seen_keys[-1] == 1
            assert seen_keys.count(1) == 2
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_no_chain_searchapi_200_quota_benches_key0_and_routes_to_key1() -> None:
    keys = ("searchapi-dead-0000", "searchapi-live-1111")
    seen_keys: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        idx = keys.index(request.url.params["api_key"])
        seen_keys.append(idx)
        if idx == 0:
            return httpx.Response(
                200,
                json={"error": "Your account has run out of searches"},
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "organic_results": [
                    {
                        "title": "ok",
                        "link": "https://example.invalid/ok",
                        "snippet": "ok",
                    }
                ]
            },
            request=request,
        )

    provider = SearchApiWebSearchProvider(
        build_config(api_keys=keys, rotation="failover")
    )
    attach_mock_client(provider, handler)
    try:
        res1 = await provider.search("q1", key_charge=no_chain_key_charge)
        assert res1.key_index == 1
        res2 = await provider.search("q2", key_charge=no_chain_key_charge)
        assert res2.key_index == 1
        assert seen_keys == [0, 1, 1]
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("adapter_cls", "provider_id"),
    [
        (SearchApiWebSearchProvider, "searchapi"),
        (SerpApiWebSearchProvider, "serpapi"),
    ],
)
async def test_searchapi_serpapi_generic_200_error_retries_same_key_in_chain(
    adapter_cls: type[BaseWebSearchProvider],
    provider_id: str,
) -> None:
    keys = (f"{provider_id}-key-0000",)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200, json={"error": "Temporary backend failure"}, request=request
            )
        return httpx.Response(
            200,
            json={
                "organic_results": [
                    {
                        "title": "ok",
                        "link": "https://example.invalid/ok",
                        "snippet": "ok",
                    }
                ]
            },
            request=request,
        )

    provider = adapter_cls(
        WebSearchProviderConfig(
            api_keys=keys,
            credential_rotation="failover",
            base_url=f"https://{provider_id}.test.invalid",
            proxy=None,
            http_timeout=20.0,
        )
    )
    attach_mock_client(provider, handler)
    policy = SearchBooksPolicy.from_settings(
        _settings(PROVIDER_RETRY_ATTEMPTS="2"), "failover"
    )
    books = SearchKeyBooks(
        provider_id,
        keys,
        (provider.key_label(0),),
        policy,
    )
    try:
        with fake_limiter_time():
            res = await drive_keys(
                books,
                lambda idx, key: provider.search_once("q", key, idx, max_results=5),
            )
            assert res.error is None
            assert calls == 2
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_searxng_403_preserves_upstream_body_and_ddgs_wraps_runtime_error(
    monkeypatch,
) -> None:
    sx = SearxngWebSearchProvider(
        build_config(api_keys=(), rotation="single", base_url="https://sx.test")
    )
    attach_mock_client(
        sx,
        lambda request: httpx.Response(
            403,
            json={"error": "format json disabled", "detail": "forbidden"},
            request=request,
        ),
    )
    try:
        with pytest.raises(WebSearchAuthError) as caught:
            await sx.search("q")
        assert caught.value.status_code == 403
        assert caught.value.upstream_body == {
            "error": "format json disabled",
            "detail": "forbidden",
        }
    finally:
        await sx.close()

    class BrokenDDGS:
        def __init__(self, proxy=None, timeout=None) -> None:
            pass

        def text(self, query, **kwargs):
            raise ValueError("unexpected primp decode error")

    monkeypatch.setattr("my_claude_code.websearch.adapters.ddgs.DDGS", BrokenDDGS)
    ddgs = DdgsWebSearchProvider(build_config(api_keys=(), rotation="single"))
    with pytest.raises(WebSearchUpstreamError, match="ValueError"):
        await ddgs.search("q")
