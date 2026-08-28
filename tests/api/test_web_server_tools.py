import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.responses import JSONResponse, StreamingResponse

import my_claude_code.api.web_tools.constants as web_tool_constants
from my_claude_code.api.handlers import MessagesHandler
from my_claude_code.api.web_tools import egress as web_egress
from my_claude_code.api.web_tools.egress import (
    WebFetchEgressPolicy,
    WebFetchEgressViolation,
    enforce_web_fetch_egress,
)
from my_claude_code.api.web_tools.outbound import (
    _drain_response_body_capped,
    _read_response_body_capped,
    _run_web_fetch,
    _run_web_search,
    _web_search_response_items,
    _web_tool_client_error_summary,
)
from my_claude_code.api.web_tools.request import (
    WebSearchToolOptions,
    is_web_server_tool_request,
    web_search_tool_options,
)
from my_claude_code.api.web_tools.streaming import (
    _format_page_age,
    _search_summary,
    _web_search_error_code,
    stream_web_server_tool_response,
)
from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.application.routing import (
    ModelRouter,
    ResolvedModel,
    RoutedMessagesRequest,
)
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest, Tool
from my_claude_code.core.anthropic.stream_contracts import (
    assert_anthropic_stream_contract,
    parse_sse_text,
    text_content,
)
from my_claude_code.core.reasoning import (
    ReasoningAdaptation,
    ReasoningAdaptationKind,
    ReasoningPolicy,
)
from my_claude_code.core.version import package_version
from my_claude_code.core.websearch.models import (
    WebSearchResponse,
    WebSearchResultItem,
)
from my_claude_code.messaging.event_parser import parse_cli_event
from my_claude_code.websearch.errors import (
    WebSearchConfigError,
    WebSearchInvalidRequestError,
    WebSearchQuotaError,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
)
from my_claude_code.websearch.registry import SearchOutcome, SearchRouteOutcome

_STRICT_EGRESS = WebFetchEgressPolicy(
    allow_private_network_targets=False,
    allowed_schemes=frozenset({"http", "https"}),
)
_PROVIDER_IDS = tuple(PROVIDER_CATALOG)


@pytest.fixture(autouse=True)
def _disable_default_websearch_analytics(monkeypatch):
    """Keep routing tests hermetic; dedicated tests install capture recorders."""

    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.emit_search_outcome",
        lambda _outcome: None,
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.emit_route_outcome",
        lambda _outcome: None,
    )


def test_web_tool_user_agent_reports_installed_package_version() -> None:
    assert {
        "User-Agent": (f"Mozilla/5.0 compatible; free-claude-code/{package_version()}")
    } == web_tool_constants._WEB_TOOL_HTTP_HEADERS


class FixedProviderModelRouter(ModelRouter):
    """Test double that pins provider identity."""

    def __init__(self, settings: Settings, provider_id: str) -> None:
        super().__init__(settings)
        self._fixed_provider_id = provider_id

    def resolve_messages_request(
        self, request: MessagesRequest
    ) -> RoutedMessagesRequest:
        resolved = ResolvedModel(
            original_model=request.model,
            provider_id=self._fixed_provider_id,
            provider_model=request.model,
            provider_model_ref=f"{self._fixed_provider_id}/{request.model}",
            reasoning_preference=ReasoningPreference.OFF,
        )
        routed = request.model_copy(deep=True)
        routed.model = resolved.provider_model
        return RoutedMessagesRequest(
            request=routed,
            resolved=resolved,
            reasoning=ReasoningPolicy.off(),
            requested_reasoning=ReasoningPolicy.off(),
            reasoning_adaptation=ReasoningAdaptation(
                ReasoningAdaptationKind.UNCHANGED, None
            ),
        )


def test_web_server_tool_not_detected_when_tool_only_listed():
    """Listing web_search without a tool choice must not skip the upstream provider."""
    request = MessagesRequest(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[Message(role="user", content="search")],
        tools=[Tool(name="web_search", type="web_search_20250305")],
    )

    assert not is_web_server_tool_request(request)


@pytest.mark.parametrize(
    ("name", "tool_type"),
    [
        ("web_search", "web_search_20250305"),
        ("web_fetch", "web_fetch_20250910"),
    ],
)
def test_web_server_tool_detected_for_single_auto_server_tool(
    name: str, tool_type: str
):
    request = MessagesRequest(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[Message(role="user", content="use the web")],
        tools=[Tool(name=name, type=tool_type)],
        tool_choice={"type": "auto"},
    )

    assert is_web_server_tool_request(request)


@pytest.mark.parametrize(
    "tools",
    [
        [
            Tool(name="web_search", type="web_search_20250305"),
            Tool(name="read_file", input_schema={"type": "object"}),
        ],
        [
            Tool(name="web_search", type="web_search_20250305"),
            Tool(name="web_fetch", type="web_fetch_20250910"),
        ],
    ],
)
def test_web_server_tool_not_detected_for_ambiguous_auto_tools(tools: list[Tool]):
    request = MessagesRequest(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[Message(role="user", content="search")],
        tools=tools,
        tool_choice={"type": "auto"},
    )

    assert not is_web_server_tool_request(request)


def test_web_server_tool_detected_when_tool_choice_forces_it():
    request = MessagesRequest(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[Message(role="user", content="search")],
        tools=[Tool(name="web_search", type="web_search_20250305")],
        tool_choice={"type": "tool", "name": "web_search"},
    )

    assert is_web_server_tool_request(request)


def test_web_server_tool_not_detected_when_forced_name_missing_from_tools():
    request = MessagesRequest(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[Message(role="user", content="hi")],
        tools=[Tool(name="other", type="function")],
        tool_choice={"type": "tool", "name": "web_search"},
    )

    assert not is_web_server_tool_request(request)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", _PROVIDER_IDS)
@pytest.mark.parametrize(
    "tool_choice",
    [
        {"type": "tool", "name": "web_search"},
        {"type": "auto"},
    ],
)
async def test_service_rejects_selected_server_tool_when_local_handler_is_disabled(
    provider_id: str, tool_choice: dict[str, str]
):
    """Every provider needs MCC's local handler for selected server tools."""
    settings = Settings.model_validate({"ENABLE_WEB_SERVER_TOOLS": False})
    assert settings.enable_web_server_tools is False
    service = MessagesHandler(
        settings,
        provider_resolver=lambda _: MagicMock(),
        model_router=FixedProviderModelRouter(settings, provider_id),
    )
    request = MessagesRequest(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[
            Message(
                role="user",
                content="Perform a web search for the query: DeepSeek V4 model release 2026",
            )
        ],
        tools=[Tool(name="web_search", type="web_search_20250305")],
        tool_choice=tool_choice,
    )
    with pytest.raises(InvalidRequestError, match="ENABLE_WEB_SERVER_TOOLS"):
        await service.create(request)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://192.168.1.1/",
        "http://10.0.0.1/",
        "http://[::1]/",
        "http://localhost/foo",
        "http://mybox.local/",
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data/",
    ],
)
def test_enforce_web_fetch_egress_blocks_internal_or_disallowed(url: str):
    with pytest.raises(WebFetchEgressViolation):
        enforce_web_fetch_egress(url, _STRICT_EGRESS)


def test_enforce_web_fetch_egress_allows_global_literal_ip():
    enforce_web_fetch_egress("http://8.8.8.8/", _STRICT_EGRESS)


def test_enforce_web_fetch_egress_skips_private_checks_when_opted_in():
    enforce_web_fetch_egress(
        "http://127.0.0.1/",
        WebFetchEgressPolicy(
            allow_private_network_targets=True,
            allowed_schemes=frozenset({"http", "https"}),
        ),
    )


def _cm(mock_client: MagicMock) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=mock_client)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _stream_cm(response: httpx.Response) -> MagicMock:
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


def _json_body(response: JSONResponse) -> dict[str, Any]:
    payload = json.loads(bytes(response.body).decode("utf-8"))
    assert isinstance(payload, dict)
    return payload


async def _streaming_body_text(response: StreamingResponse) -> str:
    parts = [
        chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
        async for chunk in response.body_iterator
    ]
    return "".join(parts)


def _aiohttp_response(
    status: int,
    *,
    url: str = "http://8.8.8.8/",
    location: str | None = None,
    body: bytes = b"hello world",
) -> MagicMock:
    r = MagicMock()
    r.status = status
    r.url = url
    hdrs: dict[str, str] = {}
    if location is not None:
        hdrs["location"] = location
    r.headers = hdrs
    r.get_encoding = MagicMock(return_value="utf-8")
    r.raise_for_status = MagicMock()
    r.request_info = MagicMock()
    r.history = ()

    async def iter_chunked(_n: int) -> Any:
        yield body

    r.content.iter_chunked = MagicMock(side_effect=iter_chunked)
    return r


def _aiohttp_client_session_patch(
    *responses: MagicMock,
) -> tuple[MagicMock, MagicMock]:
    """Build ``ClientSession`` mock that serves ``responses`` to successive ``get`` calls."""
    queue = list(responses)
    n = 0

    def get_side(*_a: Any, **_k: Any) -> Any:
        nonlocal n
        resp = queue[n] if n < len(queue) else queue[-1]
        n += 1
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        return cm

    session = MagicMock()
    session.get = MagicMock(side_effect=get_side)

    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=session)
    client_cm.__aexit__ = AsyncMock(return_value=None)
    return client_cm, session


def test_enforce_web_fetch_egress_documents_connect_time_pinning():
    assert enforce_web_fetch_egress.__doc__ and "resolved addresses" in (
        enforce_web_fetch_egress.__doc__ or ""
    )
    assert (
        web_egress.get_validated_stream_addrinfos_for_egress.__doc__
        and "pinning"
        in (web_egress.get_validated_stream_addrinfos_for_egress.__doc__ or "")
    )
    assert "DNS-pinned" in (_run_web_fetch.__doc__ or "")


@pytest.mark.asyncio
async def test_run_web_fetch_follows_redirect_when_each_hop_is_allowed():
    res_redirect = _aiohttp_response(
        302, url="http://8.8.8.8/start", location="/final", body=b""
    )
    res_ok = _aiohttp_response(200, url="http://8.8.8.8/final", body=b"hello world")
    client_cm, session = _aiohttp_client_session_patch(res_redirect, res_ok)
    with patch(
        "my_claude_code.api.web_tools.outbound.ClientSession", return_value=client_cm
    ):
        out = await _run_web_fetch("http://8.8.8.8/start", _STRICT_EGRESS)

    assert out["data"] == "hello world"
    assert session.get.call_count == 2


@pytest.mark.asyncio
async def test_run_web_fetch_truncates_large_body_to_byte_cap(monkeypatch):
    huge = b"x" * 5000
    res_ok = _aiohttp_response(200, url="http://8.8.8.8/big", body=huge)
    client_cm, _ = _aiohttp_client_session_patch(res_ok)
    monkeypatch.setattr(web_tool_constants, "_MAX_WEB_FETCH_RESPONSE_BYTES", 100)
    with patch(
        "my_claude_code.api.web_tools.outbound.ClientSession", return_value=client_cm
    ):
        out = await _run_web_fetch("http://8.8.8.8/big", _STRICT_EGRESS)

    assert len(out["data"]) <= 100
    assert out["data"] == "x" * 100


@pytest.mark.asyncio
async def test_run_web_fetch_redirect_to_blocked_host_raises():
    res_redirect = _aiohttp_response(
        302,
        url="http://8.8.8.8/start",
        location="http://127.0.0.1/secret",
        body=b"",
    )
    client_cm, session = _aiohttp_client_session_patch(res_redirect)
    with (
        patch(
            "my_claude_code.api.web_tools.outbound.ClientSession",
            return_value=client_cm,
        ),
        pytest.raises(WebFetchEgressViolation),
    ):
        await _run_web_fetch("http://8.8.8.8/start", _STRICT_EGRESS)

    session.get.assert_called_once()


@pytest.mark.asyncio
async def test_run_web_fetch_redirect_without_location_raises():
    res_bad = _aiohttp_response(302, url="http://8.8.8.8/here", body=b"")
    client_cm, _ = _aiohttp_client_session_patch(res_bad)
    with (
        patch(
            "my_claude_code.api.web_tools.outbound.ClientSession",
            return_value=client_cm,
        ),
        pytest.raises(WebFetchEgressViolation, match="missing Location"),
    ):
        await _run_web_fetch("http://8.8.8.8/here", _STRICT_EGRESS)


@pytest.mark.asyncio
async def test_run_web_fetch_excess_redirects_raises():
    res1 = _aiohttp_response(302, url="http://8.8.8.8/a", location="/b", body=b"")
    res2 = _aiohttp_response(302, url="http://8.8.8.8/b", location="/c", body=b"")
    client_cm, _ = _aiohttp_client_session_patch(res1, res2)
    with (
        patch("my_claude_code.api.web_tools.constants._MAX_WEB_FETCH_REDIRECTS", 1),
        patch(
            "my_claude_code.api.web_tools.outbound.ClientSession",
            return_value=client_cm,
        ),
        pytest.raises(WebFetchEgressViolation, match="exceeded maximum redirects"),
    ):
        await _run_web_fetch("http://8.8.8.8/a", _STRICT_EGRESS)


@pytest.mark.asyncio
async def test_streams_web_search_server_tool_result(monkeypatch):
    async def fake_search(
        query: str, _settings: Settings, **_kwargs: object
    ) -> list[dict[str, str]]:
        assert query == "DeepSeek V4 model release 2026"
        return [{"title": "DeepSeek V4 Released", "url": "https://example.com/v4"}]

    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._run_web_search", fake_search
    )
    request = MessagesRequest(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[
            Message(
                role="user",
                content=(
                    "Perform a web search for the query: DeepSeek V4 model release 2026"
                ),
            )
        ],
        tools=[Tool(name="web_search", type="web_search_20250305")],
        tool_choice={"type": "tool", "name": "web_search"},
    )

    raw = "".join(
        [
            event
            async for event in stream_web_server_tool_response(
                request, input_tokens=42, web_fetch_egress=_STRICT_EGRESS
            )
        ]
    )
    events = parse_sse_text(raw)
    assert_anthropic_stream_contract(events)
    starts = [e for e in events if e.event == "content_block_start"]
    assert starts[0].data["content_block"]["type"] == "server_tool_use"
    assert starts[0].data["content_block"]["name"] == "web_search"
    tool_use_id = starts[0].data["content_block"]["id"]
    assert starts[1].data["content_block"]["type"] == "web_search_tool_result"
    assert starts[1].data["content_block"]["tool_use_id"] == tool_use_id
    assert starts[1].data["content_block"]["content"][0]["url"] == (
        "https://example.com/v4"
    )
    text_deltas = [
        e
        for e in events
        if e.event == "content_block_delta"
        and e.data.get("delta", {}).get("type") == "text_delta"
    ]
    assert text_deltas, "summary must be streamed as text_delta"
    assert "example.com" in text_content(events)
    cli_text: list[str] = []
    for ev in events:
        cli_text.extend(
            str(p.get("text", ""))
            for p in parse_cli_event(ev.data)
            if p.get("type") == "text_delta"
        )
    assert "example.com" in "".join(cli_text)
    deltas = [e for e in events if e.event == "message_delta"]
    assert deltas[-1].data["usage"]["server_tool_use"] == {"web_search_requests": 1}


@pytest.mark.asyncio
async def test_disabled_web_search_streams_clear_error_without_outbound(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", ())
    monkeypatch.setenv("WEB_SEARCH_PROVIDER", "disabled")
    runtime_provider = AsyncMock()
    legacy = AsyncMock()
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.runtime_provider", runtime_provider
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._legacy_web_search_scrape", legacy
    )
    request = MessagesRequest(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[Message(role="user", content="Search for nothing")],
        tools=[Tool(name="web_search", type="web_search_20250305")],
        tool_choice={"type": "tool", "name": "web_search"},
    )

    raw = "".join(
        [
            event
            async for event in stream_web_server_tool_response(
                request,
                input_tokens=1,
                web_fetch_egress=_STRICT_EGRESS,
            )
        ]
    )

    events = parse_sse_text(raw)
    assert_anthropic_stream_contract(events)
    assert "web search is disabled by WEB_SEARCH_PROVIDER=disabled" in text_content(
        events
    )
    result_blocks = [
        event.data["content_block"]
        for event in events
        if event.event == "content_block_start"
        and event.data["content_block"]["type"] == "web_search_tool_result"
    ]
    assert result_blocks[0]["content"] == {
        "type": "web_search_tool_result_error",
        "error_code": "unavailable",
    }
    runtime_provider.assert_not_called()
    legacy.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_choice",
    [
        {"type": "tool", "name": "web_search"},
        {"type": "auto"},
    ],
)
async def test_service_streams_selected_web_search_locally(
    monkeypatch, tool_choice: dict[str, str]
):
    async def fake_search(
        _query: str, _settings: Settings, **_kwargs: object
    ) -> list[dict[str, str]]:
        return [{"title": "DeepSeek V4 Released", "url": "https://example.com/v4"}]

    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._run_web_search", fake_search
    )
    settings = Settings.model_validate(
        {
            "ENABLE_WEB_SERVER_TOOLS": True,
            "WEB_SEARCH_PROVIDER": "auto",
            "WEB_SEARCH_FALLBACK_POLICY": "auto",
        }
    )
    provider_resolver = MagicMock()
    service = MessagesHandler(
        settings,
        provider_resolver=provider_resolver,
        model_router=FixedProviderModelRouter(settings, _PROVIDER_IDS[0]),
    )
    request = MessagesRequest(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        stream=True,
        messages=[Message(role="user", content="Search for DeepSeek V4")],
        tools=[Tool(name="web_search", type="web_search_20250305")],
        tool_choice=tool_choice,
    )

    response = await service.create(request)

    assert isinstance(response, StreamingResponse)
    assert response.media_type == "text/event-stream"
    raw = await _streaming_body_text(response)
    assert "event: message_start" in raw
    assert "DeepSeek V4 Released" in raw
    provider_resolver.assert_not_called()


@pytest.mark.asyncio
async def test_service_aggregates_forced_web_search_when_stream_false(monkeypatch):
    async def fake_search(
        _query: str, _settings: Settings, **_kwargs: object
    ) -> list[dict[str, str]]:
        return [{"title": "DeepSeek V4 Released", "url": "https://example.com/v4"}]

    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._run_web_search", fake_search
    )
    settings = Settings.model_validate({"ENABLE_WEB_SERVER_TOOLS": True})
    provider_resolver = MagicMock()
    service = MessagesHandler(
        settings,
        provider_resolver=provider_resolver,
        model_router=FixedProviderModelRouter(settings, _PROVIDER_IDS[0]),
    )
    request = MessagesRequest(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[Message(role="user", content="Search for DeepSeek V4")],
        stream=False,
        tools=[Tool(name="web_search", type="web_search_20250305")],
        tool_choice={"type": "tool", "name": "web_search"},
    )

    response = await service.create(request)

    assert isinstance(response, JSONResponse)
    assert response.headers["content-type"].startswith("application/json")
    body = _json_body(response)
    assert [block["type"] for block in body["content"]] == [
        "server_tool_use",
        "web_search_tool_result",
        "text",
    ]
    assert body["content"][1]["content"][0]["url"] == "https://example.com/v4"
    assert "DeepSeek V4 Released" in body["content"][2]["text"]
    assert body["usage"]["server_tool_use"] == {"web_search_requests": 1}
    provider_resolver.assert_not_called()


@pytest.mark.asyncio
async def test_forced_web_fetch_ignores_stale_url_from_prior_user_turns(monkeypatch):
    """Only the latest user message supplies the URL (not earlier transcript text)."""
    target = "https://new-only.example.com/page"

    async def fake_fetch(url: str, _egress: WebFetchEgressPolicy) -> dict[str, str]:
        assert url == target
        return {
            "url": url,
            "title": "T",
            "media_type": "text/plain",
            "data": "x",
        }

    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._run_web_fetch", fake_fetch
    )
    request = MessagesRequest(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[
            Message(
                role="user",
                content="Earlier turn https://stale.com/old-article ignore this",
            ),
            Message(role="assistant", content="ok"),
            Message(
                role="user",
                content=f"Please fetch {target} for the summary",
            ),
        ],
        tools=[Tool(name="web_fetch", type="web_fetch_20250910")],
        tool_choice={"type": "tool", "name": "web_fetch"},
    )

    raw = "".join(
        [
            event
            async for event in stream_web_server_tool_response(
                request, input_tokens=1, web_fetch_egress=_STRICT_EGRESS
            )
        ]
    )
    assert target in raw


@pytest.mark.asyncio
async def test_service_aggregates_forced_web_fetch_when_stream_false(monkeypatch):
    async def fake_fetch(url: str, _egress: WebFetchEgressPolicy) -> dict[str, str]:
        return {
            "url": url,
            "title": "Example Article",
            "media_type": "text/plain",
            "data": "Article body",
        }

    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._run_web_fetch", fake_fetch
    )
    settings = Settings.model_validate({"ENABLE_WEB_SERVER_TOOLS": True})
    provider_resolver = MagicMock()
    service = MessagesHandler(
        settings,
        provider_resolver=provider_resolver,
        model_router=FixedProviderModelRouter(settings, _PROVIDER_IDS[0]),
    )
    request = MessagesRequest(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[Message(role="user", content="Fetch https://example.com/article")],
        stream=False,
        tools=[Tool(name="web_fetch", type="web_fetch_20250910")],
        tool_choice={"type": "tool", "name": "web_fetch"},
    )

    response = await service.create(request)

    assert isinstance(response, JSONResponse)
    assert response.headers["content-type"].startswith("application/json")
    body = _json_body(response)
    assert [block["type"] for block in body["content"]] == [
        "server_tool_use",
        "web_fetch_tool_result",
        "text",
    ]
    assert body["content"][1]["content"]["content"]["title"] == "Example Article"
    assert body["content"][2]["text"] == "Article body"
    assert body["usage"]["server_tool_use"] == {"web_fetch_requests": 1}
    provider_resolver.assert_not_called()


@pytest.mark.asyncio
async def test_streams_web_fetch_server_tool_result(monkeypatch):
    async def fake_fetch(url: str, _egress: WebFetchEgressPolicy) -> dict[str, str]:
        assert url == "https://example.com/article"
        return {
            "url": url,
            "title": "Example Article",
            "media_type": "text/plain",
            "data": "Article body",
        }

    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._run_web_fetch", fake_fetch
    )
    request = MessagesRequest(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[
            Message(role="user", content="Fetch https://example.com/article please")
        ],
        tools=[Tool(name="web_fetch", type="web_fetch_20250910")],
        tool_choice={"type": "tool", "name": "web_fetch"},
    )

    raw = "".join(
        [
            event
            async for event in stream_web_server_tool_response(
                request, input_tokens=42, web_fetch_egress=_STRICT_EGRESS
            )
        ]
    )
    events = parse_sse_text(raw)
    assert_anthropic_stream_contract(events)
    starts = [e for e in events if e.event == "content_block_start"]
    assert starts[0].data["content_block"]["type"] == "server_tool_use"
    tool_use_id = starts[0].data["content_block"]["id"]
    assert starts[1].data["content_block"]["type"] == "web_fetch_tool_result"
    assert starts[1].data["content_block"]["tool_use_id"] == tool_use_id
    assert starts[1].data["content_block"]["content"]["content"]["title"] == (
        "Example Article"
    )
    assert any(
        e.event == "content_block_delta"
        and e.data.get("delta", {}).get("type") == "text_delta"
        for e in events
    )
    assert "Article body" in text_content(events)
    cli_text: list[str] = []
    for ev in events:
        cli_text.extend(
            str(p.get("text", ""))
            for p in parse_cli_event(ev.data)
            if p.get("type") == "text_delta"
        )
    assert "Article body" in "".join(cli_text)
    deltas = [e for e in events if e.event == "message_delta"]
    assert deltas[-1].data["usage"]["server_tool_use"] == {"web_fetch_requests": 1}


@pytest.mark.asyncio
async def test_streams_web_fetch_error_summary_generic_by_default(monkeypatch):
    secret = "sensitive-upstream-token"

    async def boom(_url: str, _egress: WebFetchEgressPolicy) -> dict[str, str]:
        raise ValueError(secret)

    monkeypatch.setattr("my_claude_code.api.web_tools.outbound._run_web_fetch", boom)
    request = MessagesRequest(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[
            Message(
                role="user",
                content="Fetch https://example.com/sensitive-path?x=1 please",
            )
        ],
        tools=[Tool(name="web_fetch", type="web_fetch_20250910")],
        tool_choice={"type": "tool", "name": "web_fetch"},
    )

    with patch("my_claude_code.api.web_tools.outbound.logger.warning") as log_warn:
        raw = "".join(
            [
                event
                async for event in stream_web_server_tool_response(
                    request,
                    input_tokens=1,
                    web_fetch_egress=_STRICT_EGRESS,
                    verbose_client_errors=False,
                )
            ]
        )

    assert secret not in raw
    assert "ValueError" not in raw
    assert "Web tool request failed." in raw
    err_events = parse_sse_text(raw)
    assert_anthropic_stream_contract(err_events)
    assert any(
        e.event == "content_block_delta"
        and e.data.get("delta", {}).get("type") == "text_delta"
        for e in err_events
    )
    cli_err_text: list[str] = []
    for ev in err_events:
        cli_err_text.extend(
            str(p.get("text", ""))
            for p in parse_cli_event(ev.data)
            if p.get("type") == "text_delta"
        )
    assert "Web tool request failed." in "".join(cli_err_text)
    log_blob = " ".join(str(a) for c in log_warn.call_args_list for a in c.args)
    assert secret not in log_blob
    assert "example.com" in log_blob
    assert "/sensitive-path" not in log_blob


@pytest.mark.asyncio
async def test_streams_web_fetch_error_summary_verbose_includes_exception_class(
    monkeypatch,
):
    async def boom(_url: str, _egress: WebFetchEgressPolicy) -> dict[str, str]:
        raise OSError(5, "oops")

    monkeypatch.setattr("my_claude_code.api.web_tools.outbound._run_web_fetch", boom)
    request = MessagesRequest(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[Message(role="user", content="Fetch https://example.com/x")],
        tools=[Tool(name="web_fetch", type="web_fetch_20250910")],
        tool_choice={"type": "tool", "name": "web_fetch"},
    )

    raw = "".join(
        [
            event
            async for event in stream_web_server_tool_response(
                request,
                input_tokens=1,
                web_fetch_egress=_STRICT_EGRESS,
                verbose_client_errors=True,
            )
        ]
    )
    assert "OSError" in raw


@pytest.mark.asyncio
async def test_read_response_body_capped_truncates_single_oversized_chunk():
    cap = 500

    async def aiter_bytes(chunk_size=None):
        yield b"z" * (cap * 20)

    response = MagicMock()
    response.aiter_bytes = aiter_bytes

    out = await _read_response_body_capped(response, cap)
    assert len(out) == cap
    assert out == b"z" * cap


@pytest.mark.asyncio
async def test_drain_response_body_capped_stops_after_first_chunk_when_oversized():
    cap = 300
    chunk_calls = {"n": 0}

    async def aiter_bytes(chunk_size=None):
        chunk_calls["n"] += 1
        yield b"y" * (cap * 10)

    response = MagicMock()
    response.aiter_bytes = aiter_bytes

    await _drain_response_body_capped(response, cap)
    assert chunk_calls["n"] == 1


def _web_search_response(*titles: str) -> WebSearchResponse:
    return WebSearchResponse(
        provider="exa",
        query="q",
        results=tuple(
            WebSearchResultItem(
                title=title,
                url=f"https://example.com/{index}",
                snippet="",
                content=None,
                published=None,
            )
            for index, title in enumerate(titles)
        ),
        key_index=0,
        cost_usd=None,
    )


@pytest.mark.asyncio
async def test_run_web_search_routes_through_configured_provider(monkeypatch):
    settings = Settings.model_validate({"EXA_API_KEY": "k1-aaaa1111bbbb"})
    provider = MagicMock()
    runtime_provider = AsyncMock(return_value=provider)
    search_with_logging = AsyncMock(return_value=_web_search_response("One", "Two"))
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.runtime_provider", runtime_provider
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.search_with_logging",
        search_with_logging,
    )

    results = await _run_web_search("test query", settings)

    assert results == [
        {
            "title": "One",
            "url": "https://example.com/0",
            "snippet": "",
            "content": "",
            "published": "",
            "answer": "",
            "provider": "exa",
        },
        {
            "title": "Two",
            "url": "https://example.com/1",
            "snippet": "",
            "content": "",
            "published": "",
            "answer": "",
            "provider": "exa",
        },
    ]
    runtime_provider.assert_awaited_once_with(settings, "exa")
    assert search_with_logging.await_count == 1
    awaited = search_with_logging.await_args
    assert awaited is not None
    assert awaited.args == (provider, "test query")
    assert awaited.kwargs["max_results"] == web_tool_constants._MAX_SEARCH_RESULTS
    assert awaited.kwargs["attempt_number"] == 1
    assert isinstance(awaited.kwargs["route_id"], str)
    assert awaited.kwargs["route_context"] == {
        "selected_provider": "auto",
        "fallback_policy": "auto",
        "resolved_provider_path": ["exa", "ddgs", "legacy"],
        "legacy_fallback": True,
        "disabled": False,
        "max_results": web_tool_constants._MAX_SEARCH_RESULTS,
        "digest_chars": settings.websearch_digest_chars,
        "digest_answer": settings.websearch_digest_answer,
    }


@pytest.mark.asyncio
async def test_run_web_search_falls_back_to_ddgs_after_provider_error(monkeypatch):
    settings = Settings.model_validate({"EXA_API_KEY": "k1-aaaa1111bbbb"})
    requested: list[str] = []

    async def fake_runtime_provider(_settings: Settings, provider_id: str):
        requested.append(provider_id)
        return MagicMock()

    search_with_logging = AsyncMock(
        side_effect=[
            WebSearchUpstreamError("exa", "boom"),
            _web_search_response("Fallback"),
        ]
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.runtime_provider",
        fake_runtime_provider,
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.search_with_logging",
        search_with_logging,
    )

    results = await _run_web_search("test query", settings)

    assert requested == ["exa", "ddgs"]
    assert results == [
        {
            "title": "Fallback",
            "url": "https://example.com/0",
            "snippet": "",
            "content": "",
            "published": "",
            "answer": "",
            "provider": "exa",
        }
    ]


@pytest.mark.asyncio
async def test_fallback_success_emits_one_correlated_route(monkeypatch):
    settings = Settings.model_validate({"EXA_API_KEY": "k1-aaaa1111bbbb"})
    search_with_logging = AsyncMock(
        side_effect=[
            WebSearchUpstreamError("exa", "boom"),
            _web_search_response("Fallback"),
        ]
    )
    routes: list[SearchRouteOutcome] = []
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.runtime_provider",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.search_with_logging",
        search_with_logging,
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.emit_route_outcome", routes.append
    )

    await _run_web_search("test query", settings)

    assert [
        call.kwargs["attempt_number"] for call in search_with_logging.await_args_list
    ] == [1, 2]
    route_ids = {
        call.kwargs["route_id"] for call in search_with_logging.await_args_list
    }
    assert len(route_ids) == 1
    (route,) = routes
    assert route.route_id == route_ids.pop()
    assert route.primary_provider == "exa"
    assert route.terminal_provider == "ddgs"
    assert route.provider_path == ("exa", "ddgs")
    assert route.attempt_count == 2
    assert route.fallback_used is True
    assert route.status == "success"
    assert route.results_count == 1


@pytest.mark.asyncio
async def test_run_web_search_falls_back_to_legacy_scrape_when_providers_fail(
    monkeypatch,
):
    settings = Settings.model_validate({"EXA_API_KEY": "k1-aaaa1111bbbb"})
    requested: list[str] = []

    async def fake_runtime_provider(_settings: Settings, provider_id: str):
        requested.append(provider_id)
        return MagicMock()

    search_with_logging = AsyncMock(side_effect=WebSearchUpstreamError("exa", "boom"))
    legacy = AsyncMock(return_value=[{"title": "Legacy", "url": "https://legacy.test"}])
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.runtime_provider",
        fake_runtime_provider,
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.search_with_logging",
        search_with_logging,
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._legacy_web_search_scrape", legacy
    )

    results = await _run_web_search("test query", settings)

    assert requested == ["exa", "ddgs"]
    assert results == [
        {
            "title": "Legacy",
            "url": "https://legacy.test",
            "provider": "legacy",
        }
    ]
    legacy.assert_awaited_once_with("test query")


@pytest.mark.asyncio
async def test_legacy_success_emits_terminal_attempt_and_route(monkeypatch):
    settings = Settings.model_validate({"WEB_SEARCH_PROVIDER": "off"})
    attempts: list[SearchOutcome] = []
    routes: list[SearchRouteOutcome] = []
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._legacy_web_search_scrape",
        AsyncMock(return_value=[{"title": "Legacy", "url": "https://legacy.test"}]),
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.emit_search_outcome", attempts.append
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.emit_route_outcome", routes.append
    )

    results = await _run_web_search("test query", settings)

    assert results[0]["provider"] == "legacy"
    (attempt,) = attempts
    (route,) = routes
    assert attempt.provider == "legacy"
    assert attempt.attempt_number == 1
    assert attempt.status == "success"
    assert attempt.route_id == route.route_id
    assert attempt.input_payload == {
        "query": "test query",
        "max_results": web_tool_constants._MAX_SEARCH_RESULTS,
        "allowed_domains": [],
        "blocked_domains": [],
    }
    assert attempt.output_payload == {
        "provider": "legacy",
        "query": "test query",
        "answer": None,
        "results": [
            {
                "title": "Legacy",
                "url": "https://legacy.test",
                "provider": "legacy",
            }
        ],
        "result_count": 1,
        "key_index": 0,
        "cost_usd": None,
    }
    assert attempt.provider_config is not None
    assert attempt.provider_config["provider_id"] == "legacy"
    route_config = attempt.provider_config["route"]
    assert isinstance(route_config, dict)
    assert route_config.get("selected_provider") == "off"
    assert route.provider_path == ("legacy",)
    assert route.primary_provider == "legacy"
    assert route.terminal_provider == "legacy"
    assert route.fallback_used is False
    assert route.status == "success"


@pytest.mark.asyncio
async def test_legacy_terminal_failure_emits_correlated_error_route(monkeypatch):
    settings = Settings.model_validate(
        {
            "WEB_SEARCH_PROVIDER": "exa",
            "WEB_SEARCH_FALLBACK_POLICY": "legacy",
            "EXA_API_KEY": "k1-aaaa1111bbbb",
        }
    )
    attempts: list[SearchOutcome] = []
    routes: list[SearchRouteOutcome] = []
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.runtime_provider",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.search_with_logging",
        AsyncMock(side_effect=WebSearchUpstreamError("provider", "failed")),
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._legacy_web_search_scrape",
        AsyncMock(side_effect=httpx.ConnectError("legacy unavailable " * 100)),
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.emit_search_outcome", attempts.append
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.emit_route_outcome", routes.append
    )

    with pytest.raises(httpx.ConnectError, match="legacy unavailable"):
        await _run_web_search("test query", settings)

    (legacy_attempt,) = attempts
    (route,) = routes
    assert legacy_attempt.provider == "legacy"
    assert legacy_attempt.attempt_number == 3
    assert legacy_attempt.status == "error"
    assert legacy_attempt.error_kind == "upstream"
    assert legacy_attempt.route_id == route.route_id
    assert legacy_attempt.output_payload is not None
    error_payload = legacy_attempt.output_payload["error"]
    assert isinstance(error_payload, dict)
    assert error_payload.get("type") == "ConnectError"
    assert len(str(error_payload.get("message"))) == 500
    assert len(legacy_attempt.error_message or "") == 500
    assert legacy_attempt.provider_config is not None
    route_config = legacy_attempt.provider_config["route"]
    assert isinstance(route_config, dict)
    assert route_config.get("resolved_provider_path") == [
        "exa",
        "ddgs",
        "legacy",
    ]
    assert route.provider_path == ("exa", "ddgs", "legacy")
    assert route.attempt_count == 3
    assert route.fallback_used is True
    assert route.status == "error"
    assert route.error_kind == "upstream"
    assert route.terminal_provider == "legacy"


@pytest.mark.asyncio
async def test_explicit_provider_is_strict_under_default_auto_policy(monkeypatch):
    settings = Settings.model_validate(
        {
            "WEB_SEARCH_PROVIDER": "exa",
            "EXA_API_KEY": "k1-aaaa1111bbbb",
        }
    )
    requested: list[str] = []

    async def fake_runtime_provider(_settings: Settings, provider_id: str):
        requested.append(provider_id)
        return MagicMock()

    search_with_logging = AsyncMock(side_effect=WebSearchUpstreamError("exa", "boom"))
    legacy = AsyncMock()
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.runtime_provider",
        fake_runtime_provider,
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.search_with_logging",
        search_with_logging,
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._legacy_web_search_scrape", legacy
    )

    with pytest.raises(WebSearchUpstreamError, match="exa: boom"):
        await _run_web_search("test query", settings)

    assert requested == ["exa"]
    legacy.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_missing_credentials_surfaces_config_error(monkeypatch):
    settings = Settings.model_validate(
        {
            "WEB_SEARCH_PROVIDER": "exa",
            "WEB_SEARCH_FALLBACK_POLICY": "legacy",
        }
    )
    legacy = AsyncMock()
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._legacy_web_search_scrape", legacy
    )

    with pytest.raises(WebSearchConfigError, match="EXA_API_KEY"):
        await _run_web_search("test query", settings)

    legacy.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_ddgs_policy_stops_after_ddgs_failure(monkeypatch):
    settings = Settings.model_validate(
        {
            "WEB_SEARCH_PROVIDER": "exa",
            "WEB_SEARCH_FALLBACK_POLICY": "ddgs",
            "EXA_API_KEY": "k1-aaaa1111bbbb",
        }
    )
    requested: list[str] = []

    async def fake_runtime_provider(_settings: Settings, provider_id: str):
        requested.append(provider_id)
        return MagicMock()

    search_with_logging = AsyncMock(
        side_effect=[
            WebSearchUpstreamError("exa", "primary failed"),
            WebSearchUpstreamError("ddgs", "fallback failed"),
        ]
    )
    legacy = AsyncMock()
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.runtime_provider",
        fake_runtime_provider,
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.search_with_logging",
        search_with_logging,
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._legacy_web_search_scrape", legacy
    )

    with pytest.raises(WebSearchUpstreamError, match="ddgs: fallback failed"):
        await _run_web_search("test query", settings)

    assert requested == ["exa", "ddgs"]
    legacy.assert_not_called()


@pytest.mark.asyncio
async def test_explicit_legacy_policy_runs_complete_fallback_chain(monkeypatch):
    settings = Settings.model_validate(
        {
            "WEB_SEARCH_PROVIDER": "exa",
            "WEB_SEARCH_FALLBACK_POLICY": "legacy",
            "EXA_API_KEY": "k1-aaaa1111bbbb",
        }
    )
    requested: list[str] = []

    async def fake_runtime_provider(_settings: Settings, provider_id: str):
        requested.append(provider_id)
        return MagicMock()

    search_with_logging = AsyncMock(
        side_effect=WebSearchUpstreamError("search", "boom")
    )
    legacy = AsyncMock(return_value=[{"title": "Legacy", "url": "https://legacy.test"}])
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.runtime_provider",
        fake_runtime_provider,
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.search_with_logging",
        search_with_logging,
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._legacy_web_search_scrape", legacy
    )

    results = await _run_web_search("test query", settings)

    assert requested == ["exa", "ddgs"]
    assert results == [
        {
            "title": "Legacy",
            "url": "https://legacy.test",
            "provider": "legacy",
        }
    ]
    legacy.assert_awaited_once_with("test query")


@pytest.mark.asyncio
async def test_run_web_search_ddgs_failure_skips_second_ddgs_attempt(monkeypatch):
    settings = Settings.model_validate({})
    requested: list[str] = []

    async def fake_runtime_provider(_settings: Settings, provider_id: str):
        requested.append(provider_id)
        return MagicMock()

    search_with_logging = AsyncMock(side_effect=WebSearchUpstreamError("ddgs", "boom"))
    legacy = AsyncMock(return_value=[])
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.runtime_provider",
        fake_runtime_provider,
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.search_with_logging",
        search_with_logging,
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._legacy_web_search_scrape", legacy
    )

    results = await _run_web_search("test query", settings)

    assert requested == ["ddgs"]
    assert results == []
    legacy.assert_awaited_once_with("test query")


@pytest.mark.asyncio
async def test_run_web_search_disabled_rejects_without_outbound_search(monkeypatch):
    settings = Settings.model_validate({"WEB_SEARCH_PROVIDER": "disabled"})
    runtime_provider = AsyncMock()
    legacy = AsyncMock()
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.runtime_provider", runtime_provider
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._legacy_web_search_scrape", legacy
    )

    with pytest.raises(WebSearchConfigError, match="WEB_SEARCH_PROVIDER=disabled"):
        await _run_web_search("test query", settings)

    runtime_provider.assert_not_called()
    legacy.assert_not_called()


def test_web_search_config_error_summary_is_actionable_without_verbose_mode():
    error = WebSearchConfigError(
        "disabled",
        "web search is disabled by WEB_SEARCH_PROVIDER=disabled",
    )

    summary = _web_tool_client_error_summary(
        "web_search",
        error,
        verbose=False,
    )

    assert summary == (
        "web_search unavailable: web search is disabled by WEB_SEARCH_PROVIDER=disabled"
    )


@pytest.mark.asyncio
async def test_run_web_search_off_uses_legacy_scrape_only(monkeypatch):
    settings = Settings.model_validate(
        {
            "WEB_SEARCH_PROVIDER": "off",
            "WEB_SEARCH_FALLBACK_POLICY": "none",
        }
    )
    runtime_provider = AsyncMock()
    legacy = AsyncMock(return_value=[{"title": "Legacy", "url": "https://legacy.test"}])
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.runtime_provider", runtime_provider
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._legacy_web_search_scrape", legacy
    )

    results = await _run_web_search("test query", settings)

    assert results == [
        {
            "title": "Legacy",
            "url": "https://legacy.test",
            "provider": "legacy",
        }
    ]
    runtime_provider.assert_not_called()
    legacy.assert_awaited_once_with("test query")


@pytest.mark.asyncio
async def test_run_web_search_builds_settings_when_not_passed(monkeypatch):
    monkeypatch.setitem(Settings.model_config, "env_file", ())
    monkeypatch.setenv("EXA_API_KEY", "k1-aaaa1111bbbb")
    monkeypatch.delenv("WEB_SEARCH_PROVIDER", raising=False)
    provider = MagicMock()
    runtime_provider = AsyncMock(return_value=provider)
    search_with_logging = AsyncMock(return_value=_web_search_response("Env"))
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.runtime_provider", runtime_provider
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.search_with_logging",
        search_with_logging,
    )

    results = await _run_web_search("test query")

    assert results == [
        {
            "title": "Env",
            "url": "https://example.com/0",
            "snippet": "",
            "content": "",
            "published": "",
            "answer": "",
            "provider": "exa",
        }
    ]
    assert runtime_provider.await_args_list[0].args[1] == "exa"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", _PROVIDER_IDS)
async def test_service_rejects_listed_server_tools_for_every_provider(
    provider_id: str,
) -> None:
    settings = Settings()
    service = MessagesHandler(
        settings,
        provider_resolver=lambda _: MagicMock(),
        model_router=FixedProviderModelRouter(settings, provider_id),
    )
    request = MessagesRequest(
        model="m",
        max_tokens=20,
        messages=[Message(role="user", content="q")],
        tools=[Tool(name="web_search", type="web_search_20250305")],
    )
    with pytest.raises(InvalidRequestError, match="cannot pass ambiguous Anthropic"):
        await service.create(request)


# ==================== Rich digest pipeline ====================


def _rich_response() -> WebSearchResponse:
    return WebSearchResponse(
        provider="tavily",
        query="q",
        results=(
            WebSearchResultItem(
                title="Alpha",
                url="https://example.com/a",
                snippet="Alpha snippet",
                content=None,
                published="2026-01-02",
            ),
            WebSearchResultItem(
                title="Beta",
                url="https://example.com/b",
                snippet="",
                content=None,
                published=None,
            ),
        ),
        key_index=0,
        cost_usd=None,
        answer="Provider answer lead.",
    )


def test_web_search_response_items_pass_richness_through() -> None:
    items = _web_search_response_items(_rich_response())
    assert items == [
        {
            "title": "Alpha",
            "url": "https://example.com/a",
            "snippet": "Alpha snippet",
            "content": "",
            "published": "2026-01-02",
            "answer": "Provider answer lead.",
            "provider": "tavily",
        },
        {
            "title": "Beta",
            "url": "https://example.com/b",
            "snippet": "",
            "content": "",
            "published": "",
            "answer": "Provider answer lead.",
            "provider": "tavily",
        },
    ]


class TestFormatPageAge:
    def test_iso_date_becomes_human_string(self) -> None:
        assert _format_page_age("2026-07-22") == "July 22, 2026"

    def test_iso_datetime_with_z_becomes_human_string(self) -> None:
        assert _format_page_age("2026-01-02T10:20:30Z") == "January 2, 2026"

    def test_non_iso_passes_through(self) -> None:
        assert _format_page_age("Jan 2, 2026") == "Jan 2, 2026"


class TestSearchSummaryDigest:
    def test_rich_digest_format_with_answer_lead_and_dates(self) -> None:
        settings = Settings.model_validate({})
        summary = _search_summary(
            "q",
            [
                {
                    "title": "Alpha",
                    "url": "https://example.com/a",
                    "snippet": "Alpha snippet",
                    "content": "",
                    "published": "2026-01-02",
                    "answer": "Provider answer lead.",
                },
                {
                    "title": "Beta",
                    "url": "https://example.com/b",
                    "snippet": "Beta snippet",
                    "content": "",
                    "published": "",
                    "answer": "Provider answer lead.",
                },
            ],
            settings,
        )
        assert summary == (
            "Search results for: q\n\n"
            "Provider answer lead.\n\n"
            "1. Alpha (January 2, 2026)\nhttps://example.com/a\nAlpha snippet\n\n"
            "2. Beta\nhttps://example.com/b\nBeta snippet"
        )

    def test_answer_lead_disabled_by_setting(self) -> None:
        settings = Settings.model_validate({"WEBSEARCH_DIGEST_ANSWER": False})
        summary = _search_summary(
            "q",
            [
                {
                    "title": "Alpha",
                    "url": "https://example.com/a",
                    "snippet": "S",
                    "content": "",
                    "published": "",
                    "answer": "Provider answer lead.",
                }
            ],
            settings,
        )
        assert "Provider answer lead." not in summary
        assert summary.startswith("Search results for: q\n\n1. Alpha")

    def test_excerpt_capped_at_digest_chars(self) -> None:
        settings = Settings.model_validate({"WEBSEARCH_DIGEST_CHARS": 10})
        summary = _search_summary(
            "q",
            [
                {
                    "title": "Alpha",
                    "url": "https://example.com/a",
                    "snippet": "x" * 100,
                    "content": "",
                    "published": "",
                    "answer": "",
                }
            ],
            settings,
        )
        assert summary.endswith("\n" + "x" * 10)
        assert "x" * 11 not in summary

    def test_content_used_when_snippet_missing(self) -> None:
        settings = Settings.model_validate({})
        summary = _search_summary(
            "q",
            [
                {
                    "title": "Alpha",
                    "url": "https://example.com/a",
                    "content": "fuller text",
                    "published": "",
                    "answer": "",
                }
            ],
            settings,
        )
        assert summary.endswith("\nfuller text")

    def test_legacy_title_url_only_shape_unchanged(self) -> None:
        settings = Settings.model_validate({})
        results = [
            {"title": "One", "url": "https://a.io"},
            {"title": "Two", "url": "https://b.io"},
        ]
        assert _search_summary("q", results, settings) == (
            "Search results for: q\n\n1. One\nhttps://a.io\n\n2. Two\nhttps://b.io"
        )

    def test_no_results_message_unchanged(self) -> None:
        settings = Settings.model_validate({})
        assert (
            _search_summary("q", [], settings) == "No web search results found for: q"
        )


@pytest.mark.asyncio
async def test_stream_emits_page_age_and_rich_digest(monkeypatch):
    async def fake_search(
        _query: str, _settings: Settings, **_kwargs: object
    ) -> list[dict[str, str]]:
        return [
            {
                "title": "Alpha",
                "url": "https://example.com/a",
                "snippet": "Alpha snippet",
                "content": "",
                "published": "2026-01-02",
                "answer": "Provider answer lead.",
                "provider": "tavily",
            },
            {
                "title": "Beta",
                "url": "https://example.com/b",
                "snippet": "",
                "content": "",
                "published": "",
                "answer": "Provider answer lead.",
                "provider": "tavily",
            },
        ]

    monkeypatch.setitem(Settings.model_config, "env_file", ())
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._run_web_search", fake_search
    )
    request = MessagesRequest(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[
            Message(role="user", content="Perform a web search for the query: q")
        ],
        tools=[Tool(name="web_search", type="web_search_20250305")],
        tool_choice={"type": "tool", "name": "web_search"},
    )

    raw = "".join(
        [
            event
            async for event in stream_web_server_tool_response(
                request, input_tokens=42, web_fetch_egress=_STRICT_EGRESS
            )
        ]
    )
    events = parse_sse_text(raw)
    starts = [e for e in events if e.event == "content_block_start"]
    content = starts[1].data["content_block"]["content"]
    assert content[0] == {
        "type": "web_search_result",
        "title": "Alpha",
        "url": "https://example.com/a",
        "page_age": "January 2, 2026",
    }
    assert content[1] == {
        "type": "web_search_result",
        "title": "Beta",
        "url": "https://example.com/b",
    }
    text = text_content(events)
    assert "Source provider: tavily" in text
    assert "Provider answer lead." in text
    assert "1. Alpha (January 2, 2026)\nhttps://example.com/a\nAlpha snippet" in text
    assert "2. Beta\nhttps://example.com/b" in text


@pytest.mark.asyncio
async def test_stream_digest_honors_chars_and_answer_env(monkeypatch):
    async def fake_search(
        _query: str, _settings: Settings, **_kwargs: object
    ) -> list[dict[str, str]]:
        return [
            {
                "title": "Alpha",
                "url": "https://example.com/a",
                "snippet": "y" * 100,
                "content": "",
                "published": "",
                "answer": "Provider answer lead.",
                "provider": "tavily",
            }
        ]

    monkeypatch.setitem(Settings.model_config, "env_file", ())
    monkeypatch.setenv("WEBSEARCH_DIGEST_CHARS", "10")
    monkeypatch.setenv("WEBSEARCH_DIGEST_ANSWER", "false")
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound._run_web_search", fake_search
    )
    request = MessagesRequest(
        model="claude-haiku-4-5-20251001",
        max_tokens=100,
        messages=[
            Message(role="user", content="Perform a web search for the query: q")
        ],
        tools=[Tool(name="web_search", type="web_search_20250305")],
        tool_choice={"type": "tool", "name": "web_search"},
    )

    raw = "".join(
        [
            event
            async for event in stream_web_server_tool_response(
                request, input_tokens=42, web_fetch_egress=_STRICT_EGRESS
            )
        ]
    )
    text = text_content(parse_sse_text(raw))
    assert "Provider answer lead." not in text
    assert "y" * 10 in text
    assert "y" * 11 not in text


def _content_response() -> WebSearchResponse:
    """A provider that returns extracted page text alongside the snippet.

    Exa (``EXA_CONTENTS``), Tavily (``TAVILY_INCLUDE_RAW_CONTENT``), Firecrawl,
    Jina, Brave and Parallel all populate ``content`` when configured to.
    """

    return WebSearchResponse(
        provider="exa",
        query="q",
        results=(
            WebSearchResultItem(
                title="Alpha",
                url="https://example.com/a",
                snippet="short snippet",
                content="Full extracted page text that the operator paid to retrieve.",
                published=None,
            ),
        ),
        key_index=0,
        cost_usd=None,
    )


def test_web_search_response_items_forward_extracted_content() -> None:
    """``content`` must survive the hop into the digest.

    Seven adapters populate it and the digest already reads it, but it was
    never copied across -- so enabling the paid content options changed
    nothing the model could see.
    """

    (item,) = _web_search_response_items(_content_response())
    assert item["content"] == (
        "Full extracted page text that the operator paid to retrieve."
    )


def test_search_summary_prefers_extracted_content_over_snippet() -> None:
    settings = Settings.model_validate({"WEBSEARCH_DIGEST_CONTENT_CHARS": 200})
    summary = _search_summary(
        "q", _web_search_response_items(_content_response()), settings
    )
    assert "Full extracted page text" in summary


class TestWebSearchToolOptions:
    """Anthropic declares search parameters on the tool, not the tool call."""

    def _request(self, tool_extra: dict[str, Any]) -> MessagesRequest:
        return MessagesRequest.model_validate(
            {
                "model": "m",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "query: python 3.14"}],
                "tools": [
                    {"type": "web_search_20250305", "name": "web_search", **tool_extra}
                ],
                "tool_choice": {"type": "tool", "name": "web_search"},
            }
        )

    def test_reads_allowed_domains_and_max_uses(self) -> None:
        options = web_search_tool_options(
            self._request(
                {
                    "allowed_domains": ["docs.python.org", " peps.python.org "],
                    "max_uses": 3,
                }
            )
        )
        assert options.allowed_domains == ("docs.python.org", "peps.python.org")
        assert options.blocked_domains == ()
        assert options.max_uses == 3

    def test_reads_blocked_domains(self) -> None:
        options = web_search_tool_options(
            self._request({"blocked_domains": ["spam.example"]})
        )
        assert options.blocked_domains == ("spam.example",)
        assert options.allowed_domains == ()

    def test_allow_list_wins_when_a_client_sends_both(self) -> None:
        """Anthropic rejects both; honour the allow list rather than intersecting."""

        options = web_search_tool_options(
            self._request(
                {
                    "allowed_domains": ["good.example"],
                    "blocked_domains": ["bad.example"],
                }
            )
        )
        assert options.allowed_domains == ("good.example",)
        assert options.blocked_domains == ()

    def test_absent_parameters_yield_empty_options(self) -> None:
        options = web_search_tool_options(self._request({}))
        assert options == WebSearchToolOptions()


class TestWebSearchErrorCodes:
    """Only ``unavailable`` was ever emitted; clients act on the distinction."""

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (WebSearchRateLimitError("exa", "slow down"), "too_many_requests"),
            (WebSearchQuotaError("exa", "plan exhausted"), "too_many_requests"),
            (WebSearchInvalidRequestError("exa", "bad query"), "invalid_tool_input"),
            (WebSearchUpstreamError("exa", "boom"), "unavailable"),
            (RuntimeError("unexpected"), "unavailable"),
        ],
    )
    def test_error_maps_to_documented_code(self, error, expected) -> None:
        assert _web_search_error_code(error) == expected


@pytest.mark.asyncio
async def test_run_web_search_forwards_domain_filters_to_provider(monkeypatch):
    """The registry and every adapter already accept these; nothing sent them.

    ``SUPPORTS_DOMAINS`` was effectively dead in production because the API
    layer never read the filters off the client's tool definition.
    """

    settings = Settings.model_validate({"EXA_API_KEY": "k1-aaaa1111bbbb"})
    search_with_logging = AsyncMock(return_value=_web_search_response("One"))
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.runtime_provider",
        AsyncMock(return_value=MagicMock()),
    )
    monkeypatch.setattr(
        "my_claude_code.api.web_tools.outbound.search_with_logging",
        search_with_logging,
    )

    await _run_web_search(
        "test query",
        settings,
        allowed_domains=("docs.python.org",),
        blocked_domains=(),
    )

    await_args = search_with_logging.await_args
    assert await_args is not None
    kwargs = await_args.kwargs
    assert kwargs["allowed_domains"] == ("docs.python.org",)
    assert kwargs["blocked_domains"] == ()
