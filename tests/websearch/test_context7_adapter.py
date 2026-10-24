"""The ``context7`` adapter on recorded-shape responses (7.113.0).

Every response comes from ``tests/fixtures/context7/responses.json``, whose
``provenance`` block says where each field name was read (Context7's own MCP
server and SDK, packed from npm) and that no live call was made. The HTTP
layer is ``httpx.MockTransport``: nothing here reaches a network.

What is pinned:

* the two calls -- the library search, then the documentation of the best
  match -- with Bearer auth, their paths and query parameters;
* the result shape -- title = library + section, url = the source Context7
  returns, snippet = the excerpt, cut with a visible marker;
* each failure lands on the ``WebSearchError`` kind the shared HTTP mapping
  gives every adapter, so PR A's translation (``translate_search_error``) and
  Q8's judge (``no_chain_key_charge``) read it unchanged.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.api.web_tools.search_books import translate_search_error
from my_claude_code.api.web_tools.search_key_charge import no_chain_key_charge
from my_claude_code.config.settings import Settings
from my_claude_code.core.failures import FailureKind
from my_claude_code.core.rate_limit import DEFAULT_RATE_LIMIT_COOLDOWN
from my_claude_code.websearch.adapters.context7 import (
    TRUNCATION_MARKER,
    Context7WebSearchProvider,
    bounded_excerpt,
)
from my_claude_code.websearch.errors import (
    KIND_DETAIL_BAD_JSON,
    WebSearchAuthError,
    WebSearchError,
    WebSearchInvalidRequestError,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
)
from my_claude_code.websearch.registry import (
    build_provider,
    resolve_provider_id,
    resolve_search_route,
)
from tests.support.websearch_credentials import forget_web_search_credentials
from tests.websearch.support import attach_mock_client, build_config

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "context7" / "responses.json"
)
#: Context7 keys start with ``ctx7sk`` (both clients say so); this one is fake.
KEY_1 = "ctx7sk-fake0000-0000-4000-8000-000000000001"
KEY_2 = "ctx7sk-fake0000-0000-4000-8000-000000000002"
LONG_CODE = "y" * 1500
QUERY = "next.js cache a fetch request"


def _responses() -> dict[str, dict[str, Any]]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return data["responses"]


def _http(name: str) -> httpx.Response:
    recorded = _responses()[name]
    headers = dict(recorded["headers"])
    if "body" in recorded:
        text = json.dumps(recorded["body"]).replace("LONG_CODE_PLACEHOLDER", LONG_CODE)
    else:
        text = recorded["body_text"]
    return httpx.Response(recorded["status"], headers=headers, text=text)


def _provider(keys: tuple[str, ...] = (KEY_1,)) -> Context7WebSearchProvider:
    return Context7WebSearchProvider(
        build_config(api_keys=keys, base_url="https://c7.test/api")
    )


def _route(search: str, context: str = "context_ok"):
    """A handler answering the library search and the documentation call."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/libs/search"):
            return _http(search)
        if request.url.path.endswith("/v2/context"):
            return _http(context)
        return httpx.Response(599, text="unexpected path")

    return handle


async def _search(provider: Context7WebSearchProvider, **kwargs: Any):
    try:
        return await provider.search(QUERY, **kwargs)
    finally:
        await provider.close()


def test_the_fixture_says_where_it_came_from() -> None:
    provenance = json.loads(FIXTURE.read_text(encoding="utf-8"))["provenance"]
    assert "0 calls were made" in provenance["what"]
    joined = " ".join(provenance["read_from"])
    assert "@upstash/context7-mcp 4.4.4" in joined
    assert "@upstash/context7-sdk 0.6.0" in joined


@pytest.mark.asyncio
async def test_one_search_is_the_library_search_then_its_documentation() -> None:
    provider = _provider()
    requests = attach_mock_client(provider, _route("libs_search_ok"))
    response = await _search(provider, max_results=10)

    assert [request.url.path for request in requests] == [
        "/api/v2/libs/search",
        "/api/v2/context",
    ]
    assert all(request.method == "GET" for request in requests)
    assert all(
        request.headers["authorization"] == f"Bearer {KEY_1}" for request in requests
    )
    assert dict(requests[0].url.params) == {"query": QUERY, "libraryName": QUERY}
    # The best (first) match is the one whose documentation is read.
    assert dict(requests[1].url.params) == {
        "query": QUERY,
        "libraryId": "/vercel/next.js",
        "type": "json",
    }
    assert response.provider == "context7"
    assert response.key_index == 0
    assert response.cost_usd is None
    assert response.answer is None


@pytest.mark.asyncio
async def test_results_are_library_plus_section_the_source_url_and_the_excerpt() -> (
    None
):
    provider = _provider()
    attach_mock_client(provider, _route("libs_search_ok"))
    response = await _search(provider, max_results=10)

    titles = [item.title for item in response.results]
    # Code snippets first, then documentation (the SDK's own order); the
    # snippet with no source is dropped -- it cannot be cited.
    assert titles == [
        "Next.js — Cache a fetch request",
        "Next.js — A long example",
        "Next.js — Caching > Revalidation",
        "Next.js — Documentation",
    ]
    first = response.results[0]
    assert first.url == (
        "https://github.com/vercel/next.js/blob/canary/docs/fetch.mdx#snippet-1"
    )
    assert first.snippet == (
        "Opt one fetch into the data cache (invented example).\n\n"
        "```typescript\nconst res = await fetch(url, { cache: 'force-cache' })\n```"
    )
    assert first.content == first.snippet
    assert first.published is None
    info = response.results[2]
    assert info.url == (
        "https://github.com/vercel/next.js/blob/canary/docs/revalidate.mdx"
    )
    assert (
        info.snippet == "Revalidation refreshes cached data on an interval (invented)."
    )


@pytest.mark.asyncio
async def test_a_long_excerpt_is_cut_with_a_marker_and_its_content_kept_longer() -> (
    None
):
    provider = _provider()
    attach_mock_client(provider, _route("libs_search_ok"))
    response = await _search(provider, max_results=10)

    long = response.results[1]
    assert len(long.snippet) == 1000
    assert long.snippet.endswith(TRUNCATION_MARKER)
    assert long.content is not None
    assert LONG_CODE in long.content
    assert not long.content.endswith(TRUNCATION_MARKER)


def test_the_excerpt_bound_counts_the_marker() -> None:
    assert bounded_excerpt("abc", 3) == "abc"
    cut = bounded_excerpt("a" * 80, 40)
    assert len(cut) == 40
    assert cut.endswith(TRUNCATION_MARKER)


@pytest.mark.asyncio
async def test_max_results_bounds_the_results() -> None:
    provider = _provider()
    attach_mock_client(provider, _route("libs_search_ok"))
    response = await _search(provider, max_results=2)
    assert len(response.results) == 2


@pytest.mark.asyncio
async def test_no_matching_library_is_an_empty_answer_with_one_call() -> None:
    provider = _provider()
    requests = attach_mock_client(provider, _route("libs_search_empty"))
    response = await _search(provider)
    assert response.results == ()
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_a_library_with_no_matching_documentation_is_an_empty_answer() -> None:
    provider = _provider()
    requests = attach_mock_client(provider, _route("libs_search_ok", "context_empty"))
    response = await _search(provider)
    assert response.results == ()
    assert len(requests) == 2


async def _error(search: str, context: str = "context_ok") -> WebSearchError:
    provider = _provider()
    attach_mock_client(provider, _route(search, context))
    with pytest.raises(WebSearchError) as caught:
        await _search(provider)
    return caught.value


@pytest.mark.asyncio
async def test_a_401_is_a_rejected_key() -> None:
    error = await _error("unauthorized")
    assert isinstance(error, WebSearchAuthError)
    assert error.status_code == 401
    assert "ctx7sk" in error.message


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["search", "context"])
async def test_a_429_is_a_rate_limit_with_the_published_wait(step: str) -> None:
    error = await (
        _error("rate_limited")
        if step == "search"
        else _error("libs_search_ok", "rate_limited")
    )
    # Not "upstream": the key pool and the chain read the wait from here.
    assert isinstance(error, WebSearchRateLimitError)
    assert error.kind == "rate_limit"
    assert error.status_code == 429
    assert error.retry_after_seconds == 7.0


@pytest.mark.asyncio
async def test_a_5xx_is_an_upstream_failure() -> None:
    error = await _error("server_error")
    assert type(error) is WebSearchUpstreamError
    assert error.status_code == 503


@pytest.mark.asyncio
async def test_an_unknown_library_is_a_refused_query() -> None:
    error = await _error("libs_search_ok", "not_found")
    assert isinstance(error, WebSearchInvalidRequestError)
    assert error.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("search", "context"),
    [
        ("malformed_not_json", "context_ok"),
        ("malformed_shape", "context_ok"),
        ("libs_search_ok", "malformed_not_json"),
        ("libs_search_ok", "malformed_shape"),
    ],
)
async def test_an_unreadable_answer_is_a_bad_body(search: str, context: str) -> None:
    error = await _error(search, context)
    assert type(error) is WebSearchUpstreamError
    assert error.kind_detail == KIND_DETAIL_BAD_JSON


@pytest.mark.asyncio
async def test_the_failure_kinds_read_through_the_chain_and_the_judge_unchanged() -> (
    None
):
    cases = {
        "unauthorized": (FailureKind.AUTHENTICATION, "auth"),
        "rate_limited": (FailureKind.RATE_LIMIT, "rate_limit"),
        "server_error": (FailureKind.UPSTREAM, None),
        "malformed_not_json": (FailureKind.UPSTREAM, None),
    }
    for name, (kind, charge) in cases.items():
        error = await _error(name)
        failure = translate_search_error(error, cooldown=DEFAULT_RATE_LIMIT_COOLDOWN)
        assert failure is not None
        assert (name, failure.kind) == (name, kind)
        assert (name, no_chain_key_charge(error)) == (name, charge)
    limited = await _error("rate_limited")
    failure = translate_search_error(limited, cooldown=DEFAULT_RATE_LIMIT_COOLDOWN)
    assert failure is not None
    assert failure.retry_after_seconds == 7.0


@pytest.mark.asyncio
async def test_a_rejected_first_key_rotates_to_the_second() -> None:
    provider = _provider((KEY_1, KEY_2))

    def handle(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == f"Bearer {KEY_1}":
            return _http("unauthorized")
        return _route("libs_search_ok")(request)

    requests = attach_mock_client(provider, handle)
    response = await _search(provider, key_charge=no_chain_key_charge)
    assert response.key_index == 1
    assert len(response.results) == 4
    assert len(requests) == 3


def _settings(monkeypatch, **env: str) -> Settings:
    monkeypatch.setitem(Settings.model_config, "env_file", ())
    forget_web_search_credentials(monkeypatch)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings()


def test_the_key_setting_builds_the_adapter_on_context7s_api(monkeypatch) -> None:
    settings = _settings(monkeypatch, CONTEXT7_API_KEY=f"{KEY_1},{KEY_2}")
    provider = build_provider(settings, "context7")
    assert isinstance(provider, Context7WebSearchProvider)
    assert provider.config.api_keys == (KEY_1, KEY_2)
    assert provider.config.base_url == "https://context7.com/api"
    assert provider.config.credential_rotation == "failover"


def test_its_rotation_setting_is_read_like_every_provider(monkeypatch) -> None:
    settings = _settings(
        monkeypatch,
        CONTEXT7_API_KEY=f"{KEY_1},{KEY_2}",
        CONTEXT7_API_KEY_ROTATION="round_robin",
    )
    assert build_provider(settings, "context7").config.credential_rotation == (
        "round_robin"
    )


def test_auto_never_picks_it_but_a_name_does(monkeypatch) -> None:
    only = _settings(monkeypatch, CONTEXT7_API_KEY=KEY_1)
    assert resolve_provider_id(only) == "ddgs"
    with_exa = _settings(monkeypatch, CONTEXT7_API_KEY=KEY_1, EXA_API_KEY="exa-k1")
    assert resolve_provider_id(with_exa) == "exa"
    named = _settings(
        monkeypatch, CONTEXT7_API_KEY=KEY_1, WEB_SEARCH_PROVIDER="context7"
    )
    assert resolve_provider_id(named) == "context7"
    assert resolve_search_route(named).provider_ids == ("context7",)


def test_it_is_a_chain_entry_like_any_other(monkeypatch) -> None:
    settings = _settings(
        monkeypatch,
        CONTEXT7_API_KEY=KEY_1,
        WEB_SEARCH_CHAIN="exa,context7,tavily,context7",
    )
    assert settings.web_search_chain == "exa,context7,tavily,context7"
