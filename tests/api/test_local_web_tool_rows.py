"""A web search MCC answers itself is a local answer, and only that.

Claude Code sends each web search and web fetch as its own one-tool side
request, and MCC answers it with its own search or fetch: no model, no key, no
attempt. Before 7.69.5 the row kept the name ``set_routing`` had stamped on it
-- the route's first model, paused or not. 697 rows in a real log named a
paused chain head that was never called (Analytics listed paused gpt-6-sol
beside the live space-bunny-alpha under ``claude-fable-5.1``), and $14.38 was
priced on gpt-6-sol for answers it never gave.

The user's question about those rows was whether the relabel could ever hide
a REAL call. These tests are the answer, in both directions:

* a locally answered web search never carries a model, a provider or a price;
* a request that reached a model -- on the same rail, on a turn that also
  involves a web tool, on a paused model named directly -- keeps the model that
  answered and its attempt rows, whatever else is true of it.
"""

from collections.abc import AsyncIterator
from typing import Any, Literal

import pytest

import my_claude_code.api.request_capture as request_capture
import my_claude_code.api.web_tools.outbound as outbound
from my_claude_code.api.handlers import MessagesHandler
from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.api.response_streams import ManagedStreamingResponse
from my_claude_code.api.web_tools.request import selected_server_tool_name
from my_claude_code.application.cost import RateCard
from my_claude_code.application.execution import RouteAttemptRecord
from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.request_log import (
    LOCAL_WEB_TOOL_ANSWERS,
    RequestLogStore,
    get_request_log_store,
)

SOL = "chatgpt_oauth/gpt-6-sol"
BUNNY = "nous_portal/stealth/space-bunny-alpha"
FABLE = "claude-fable-5.1"


def _settings() -> Settings:
    """The Fable rail of the incident: the head is paused, the fallback is live."""
    return Settings.model_validate(
        {
            "MODEL_FABLE": SOL,
            "MODEL_FABLE_FALLBACKS": BUNNY,
            "MODEL_FABLE_PAUSED": SOL,
            "ENABLE_WEB_SERVER_TOOLS": True,
            "REQUEST_LOG_ENABLED": True,
        }
    )


def _priced_cards(
    provider_id: str | None, model_id: str | None, *, litellm_enabled: bool
) -> tuple[RateCard, ...]:
    """Every named route has a price, as gpt-6-sol had one on the user's machine.

    Mirrors ``rate_cards`` itself: no provider or no model, no card. Without a
    price somewhere, "the local row costs NULL" would hold for the wrong reason.
    """
    if not provider_id or not model_id:
        return ()
    return (RateCard(source="models_dev", input_price=1e-6, output_price=2e-6),)


@pytest.fixture(autouse=True)
def _priced_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(request_capture, "rate_cards", _priced_cards)


@pytest.fixture
def searched(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """MCC's own search and fetch, stubbed: no network, a record of each call."""
    calls: list[str] = []

    async def fake_search(query: str, _settings: Any = None, **_kwargs: Any) -> list:
        calls.append(f"search:{query}")
        return [{"title": "Space bunny", "url": "https://example.com/bunny"}]

    async def fake_fetch(url: str, _egress: Any) -> dict[str, str]:
        calls.append(f"fetch:{url}")
        return {
            "url": url,
            "media_type": "text/plain",
            "data": "a page about bunnies",
            "title": "Bunnies",
        }

    monkeypatch.setattr(outbound, "_run_web_search", fake_search)
    monkeypatch.setattr(outbound, "_run_web_fetch", fake_fetch)
    return calls


class _Provider:
    """A model that answers with text, and counts how often it was asked."""

    def __init__(self, calls: list[str], provider_id: str) -> None:
        self._calls = calls
        self._provider_id = provider_id

    def throttle_remaining(self, model: str | None = None) -> float:
        return 0.0

    @property
    def credential_label(self) -> str | None:
        return None

    def preflight_stream(self, request: Any, *, reasoning: Any) -> None:
        return None

    async def cleanup(self) -> None:
        return None

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: Any,
    ) -> AsyncIterator[str]:
        self._calls.append(f"{self._provider_id}/{request.model}")
        for frame in (
            'event: message_start\ndata: {"type":"message_start","message":'
            '{"id":"msg_1","type":"message","role":"assistant","content":[],'
            '"model":"m","stop_reason":null,"usage":{"input_tokens":30,'
            '"output_tokens":0}}}\n\n',
            'event: content_block_start\ndata: {"type":"content_block_start",'
            '"index":0,"content_block":{"type":"text","text":""}}\n\n',
            'event: content_block_delta\ndata: {"type":"content_block_delta",'
            '"index":0,"delta":{"type":"text_delta","text":"answered"}}\n\n',
            'event: content_block_stop\ndata: {"type":"content_block_stop",'
            '"index":0}\n\n',
            'event: message_delta\ndata: {"type":"message_delta","delta":'
            '{"stop_reason":"end_turn"},"usage":{"output_tokens":4}}\n\n',
            'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ):
            yield frame


def _handler(model_calls: list[str]) -> MessagesHandler:
    settings = _settings()
    return MessagesHandler(
        settings,
        provider_resolver=lambda provider_id: _Provider(model_calls, provider_id),
        model_router=ModelRouter(settings),
    )


async def _run(handler: MessagesHandler, request: MessagesRequest, rid: str) -> dict:
    response = await handler.create(request, request_id=rid)
    assert isinstance(response, ManagedStreamingResponse)
    async for _chunk in response.body_iterator:
        pass
    await response.aclose()
    store = get_request_log_store()
    assert store is not None
    store.close()
    row = store.get_request(rid)
    assert row is not None
    return row


def _web_tool_request(tool: str, *, forced: bool = False, model: str = FABLE):
    """Claude Code's side request: one server tool, ``tool_choice`` auto."""
    text = (
        "Perform a web search for the query: space bunny"
        if tool == "web_search"
        else "Fetch https://example.com/bunny and summarise it"
    )
    tool_type = "web_search_20250305" if tool == "web_search" else "web_fetch_20250910"
    return MessagesRequest.model_validate(
        {
            "model": model,
            "max_tokens": 100,
            "stream": True,
            "system": "You are an assistant for performing a web search tool use",
            "messages": [{"role": "user", "content": text}],
            "tools": [{"name": tool, "type": tool_type}],
            "tool_choice": (
                {"type": "tool", "name": tool} if forced else {"type": "auto"}
            ),
        }
    )


def _main_turn_request(model: str = FABLE) -> MessagesRequest:
    """A real Claude Code turn that ALSO involves a web tool.

    The client's own ``WebSearch`` tool is on the list, and the history carries
    the result of an earlier search MCC answered locally -- the turn that reads
    that result is a model call like any other.
    """
    return MessagesRequest.model_validate(
        {
            "model": model,
            "max_tokens": 100,
            "stream": True,
            "messages": [
                {"role": "user", "content": "look up space bunnies"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "WebSearch",
                            "input": {"query": "space bunny"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_1",
                            "content": "Web search results for query: space bunny",
                        }
                    ],
                },
            ],
            "tools": [
                {
                    "name": "WebSearch",
                    "description": "Search the web",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                },
                {
                    "name": "Bash",
                    "description": "Run a command",
                    "input_schema": {
                        "type": "object",
                        "properties": {"command": {"type": "string"}},
                    },
                },
            ],
            "tool_choice": {"type": "auto"},
        }
    )


# -- the inverse: a local web answer never gets a model ------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["web_search", "web_fetch"])
@pytest.mark.parametrize("forced", [False, True], ids=["auto", "forced"])
async def test_a_local_web_answer_on_a_paused_head_names_no_model(
    tool: str, forced: bool, searched: list[str]
) -> None:
    model_calls: list[str] = []
    row = await _run(
        _handler(model_calls), _web_tool_request(tool, forced=forced), "req_web"
    )

    assert model_calls == []  # no model was asked
    assert len(searched) == 1  # MCC's own tool did the work
    assert row["status"] == "success"
    assert row["provider"] is None
    assert row["resolved_model"] is None
    assert row["optimization"] == tool
    assert row["is_local"] == 1
    assert row["key_label"] is None
    # Not priced by anybody -- NULL, never a zero that reads as a price.
    assert row["cost_usd"] is None
    assert row["cost_source"] is None
    # A search avoided no tokens: the saving is not measured, not zero.
    assert row["optimization_tokens_saved"] is None
    assert row["route_attempts"] == []
    # What the request would have used stays answerable.
    assert row["requested_model"] == FABLE
    assert row["route_chain"] == f"{SOL},{BUNNY}"
    assert row["output_text"]


# -- the safety half: a real call is never relabelled --------------------------


@pytest.mark.asyncio
async def test_a_real_call_on_the_same_rail_keeps_its_model_and_attempts(
    searched: list[str],
) -> None:
    model_calls: list[str] = []
    row = await _run(_handler(model_calls), _main_turn_request(), "req_real")

    assert searched == []
    assert model_calls == ["nous_portal/stealth/space-bunny-alpha"]
    assert row["provider"] == "nous_portal"
    assert row["resolved_model"] == "stealth/space-bunny-alpha"
    assert row["optimization"] is None
    assert row["is_local"] == 0
    assert row["cost_usd"] is not None
    assert row["cost_source"] == "models_dev"
    outcomes = [
        (attempt["model_ref"], attempt["outcome"]) for attempt in row["route_attempts"]
    ]
    assert outcomes == [(SOL, "skipped"), (BUNNY, "succeeded")]


@pytest.mark.asyncio
async def test_a_paused_model_named_directly_keeps_its_name(
    searched: list[str],
) -> None:
    """Paused on the Fable rail, named directly: a real call, recorded as one."""
    model_calls: list[str] = []
    row = await _run(_handler(model_calls), _main_turn_request(model=SOL), "req_direct")

    assert searched == []
    assert model_calls == ["chatgpt_oauth/gpt-6-sol"]
    assert row["provider"] == "chatgpt_oauth"
    assert row["resolved_model"] == "gpt-6-sol"
    assert row["optimization"] is None
    assert row["is_local"] == 0
    assert row["cost_usd"] is not None
    assert [
        (attempt["model_ref"], attempt["outcome"]) for attempt in row["route_attempts"]
    ] == [(SOL, "succeeded")]


def _capture(store: RequestLogStore, rid: str) -> RequestCapture:
    return RequestCapture(
        store,
        request_id=rid,
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model=FABLE,
        input_text="hi",
        params=None,
    )


@pytest.mark.parametrize("outcome", ["succeeded", "failed", "skipped"])
def test_the_web_tool_mark_never_hides_an_attempt(
    tmp_path: Any, outcome: Literal["succeeded", "failed", "skipped"]
) -> None:
    """The guard itself: any attempt at all and the row keeps its model.

    Built by hand, because no handler path sets the mark and runs the chain --
    which is exactly why the guard has to hold on its own.
    """
    store = RequestLogStore(tmp_path / "requests.db")
    plan = ModelRouter(_settings()).resolve_messages_plan(
        _main_turn_request(), harness="claude"
    )
    capture = _capture(store, "req_guard")
    capture.set_plan(plan)
    capture.set_routing(plan.attempts[1], 1)
    capture.set_local_web_tool("web_search")
    capture.record_attempt_result(
        RouteAttemptRecord(
            attempt=1,
            provider_id="nous_portal",
            model_ref=BUNNY,
            outcome=outcome,
            duration_ms=12.0,
        )
    )
    capture.finish_success("answered")
    store.close()

    row = store.get_request("req_guard")
    assert row is not None
    assert row["provider"] == "nous_portal"
    assert row["resolved_model"] == "stealth/space-bunny-alpha"
    assert row["optimization"] is None
    assert row["is_local"] == 0
    assert [attempt["model_ref"] for attempt in row["route_attempts"]] == [BUNNY]


@pytest.mark.parametrize("status", ["success", "cancelled"])
def test_the_web_tool_mark_alone_makes_the_row_local(
    tmp_path: Any, status: str
) -> None:
    store = RequestLogStore(tmp_path / "requests.db")
    plan = ModelRouter(_settings()).resolve_messages_plan(
        _web_tool_request("web_search"), harness="claude"
    )
    capture = _capture(store, "req_mark")
    capture.set_plan(plan)
    capture.set_routing(plan.primary, 0)
    assert capture._record.provider == "chatgpt_oauth"  # what routing stamped
    capture.set_local_web_tool("web_search")
    if status == "success":
        capture.finish_success("Search results")
    else:
        capture._finalize("cancelled")
    store.close()

    row = store.get_request("req_mark")
    assert row is not None
    assert row["status"] == status
    assert row["provider"] is None
    assert row["resolved_model"] is None
    assert row["optimization"] == "web_search"
    assert row["is_local"] == 1
    assert row["cost_usd"] is None


def test_every_tool_the_intercept_can_select_is_a_named_local_answer() -> None:
    """The handler records the selected tool's name; the store must know it."""
    for tool in ("web_search", "web_fetch"):
        for forced in (False, True):
            selected = selected_server_tool_name(_web_tool_request(tool, forced=forced))
            assert selected in LOCAL_WEB_TOOL_ANSWERS


# -- what Analytics, cost and totals see ----------------------------------------


@pytest.mark.asyncio
async def test_analytics_cost_and_totals_name_the_paused_head_nowhere(
    searched: list[str],
) -> None:
    model_calls: list[str] = []
    handler = _handler(model_calls)
    await _run(handler, _web_tool_request("web_search"), "req_a_web")
    await _run(handler, _main_turn_request(), "req_a_real")
    store = get_request_log_store()
    assert store is not None

    stats = store.stats(model=FABLE)
    assert stats["served_from"] == "rollup"
    by_model = {row["key"]: row["requests"] for row in stats["by_model"]}
    assert "gpt-6-sol" not in by_model
    assert by_model["stealth/space-bunny-alpha"] == 1
    by_provider = {row["key"]: row["requests"] for row in stats["by_provider"]}
    assert by_provider == {"nous_portal": 1, "local:web_search": 1}

    # The dashboard's default hides local answers: only the model call is left.
    hidden = store.stats(model=FABLE, local="hide")
    assert {row["key"] for row in hidden["by_model"]} == {"stealth/space-bunny-alpha"}
    only = store.stats(model=FABLE, local="only")
    assert {row["key"] for row in only["by_provider"]} == {"local:web_search"}
    assert store.stats(provider="local:web_search")["by_provider"][0]["requests"] == 1

    cost = store.cost_breakdown(model=FABLE)
    assert "gpt-6-sol" not in {row["key"] for row in cost["by_model"]}
    assert cost["totals"]["requests"] == 2
    assert cost["totals"]["priced"] == 1  # the model call; the search is not priced

    lifetime = store.lifetime()
    assert "gpt-6-sol" not in {row["name"] for row in lifetime["by_model"]}

    # Not an optimization rule: the Token Optimizer page is unchanged by it.
    optimizer = store.optimization_stats()
    assert optimizer["rules"] == []
    assert optimizer["answered_locally"] == 0
    assert optimizer["total_requests"] == 2
