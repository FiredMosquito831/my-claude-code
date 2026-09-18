"""The Responses surface learns what a host refuses, and never sends it twice.

Two refusals, both measured live on ``opencode.ai/zen/v1/responses`` against
``muse-spark-1.3-contributor-free`` on 2026-09-17:

* a 68-character tool name draws ``400`` ``param: "name"`` -- "``name`` must be
  at most 64 characters, got 68" -- while the 64-character alias draws ``200``;
* **any** ``tool_choice`` other than ``auto`` draws ``400``
  ``param: "tool_choice"`` -- "only ``\"auto\"`` is supported for
  ``tool_choice``".

7.18.1 fixed the first for the two profiles that *declare* the limit. These
tests hold the general net: a host that has never declared anything pays each
refusal exactly once, and every request after that is shaped correctly from the
first try. Nothing changes for a host that has not refused -- which is the
whole opt-in-by-evidence rule, and why the declared-limit goldens in
``test_responses_tool_name_aliases.py`` are the other half of this file's
proof.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.core.upstream_ladder import (
    current_ladder,
    install_ladder_trace,
    ladder_payload,
)
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat.responses_transport import ResponsesTransport
from my_claude_code.providers.recovery import (
    FACT_RESPONSES_TOOL_CHOICE_AUTO_ONLY,
    FACT_RESPONSES_TOOL_NAME_MAX_LENGTH,
    INFERRED_FACT_TTL_SECONDS,
    PROVIDER_WIDE_MODEL_ID,
    STATED_FACT_TTL_SECONDS,
    LearnedFactStore,
)
from tests.providers.support import (
    ImmediateRetryProviderRateLimiter,
    passthrough_rate_limiter,
)

HERE = Path(__file__).parent
CONSOLE = "mcp__plugin_chrome-devtools-mcp_chrome-devtools__get_console_message"
REASONING = ReasoningPolicy.on()
PROVIDER = "muse_gateway"


def _fixture() -> dict[str, Any]:
    """The same 68-character-tool-name request the declared-limit tests use."""

    data = json.loads(
        (HERE / "responses_long_tool_names_request.json").read_text(encoding="utf-8")
    )
    data.pop("_about")
    return data


def _request() -> MessagesRequest:
    return MessagesRequest.model_validate(_fixture())


def _store() -> LearnedFactStore:
    """An in-memory store: persistence is opt-in and nothing turns it on here."""

    return LearnedFactStore()


def _transport(
    store: LearnedFactStore,
    *,
    declared: int | None = None,
    limiter: Any = None,
) -> ResponsesTransport:
    """A Responses transport for a host that declares no tool-name limit."""

    return ResponsesTransport(
        ProviderConfig(api_key="sk-test", base_url="https://example.invalid/v1"),
        base_url="https://example.invalid/v1",
        provider_name="MUSE",
        identity=None,
        api_key=None,
        rate_limiter=limiter or passthrough_rate_limiter(),
        tool_name_max_length=declared,
        memory=store.memory_for(PROVIDER),
    )


def _refusal(message: str, param: str) -> httpx.Response:
    """A 400 in the shape opencode.ai's Console gateway actually sends."""

    return httpx.Response(
        400,
        json={
            "model": "muse-spark-1.3-contributor-free",
            "error": {
                "param": param,
                "type": "invalid_request_error",
                "message": (
                    f"Error from provider (Console): Upstream request failed: "
                    f"[invalid_request_error] {message}"
                ),
            },
        },
    )


NAME_TOO_LONG = "`name` must be at most 64 characters, got 68"
TOOL_CHOICE_AUTO_ONLY = (
    'only `"auto"` is supported for `tool_choice`. `"none"`, `"required"`, '
    "and named function choices are not currently supported"
)


def _accepted(alias: str) -> httpx.Response:
    """A 200 whose SSE stream is a single tool call under ``alias``.

    The body is an async generator rather than bytes: ``send(stream=True)``
    treats an in-hand ``content=`` as already read, and the transport iterates
    it with ``aiter_raw``.
    """

    frames = _sse_call(alias)

    async def _body():
        yield frames

    return httpx.Response(
        200, content=_body(), headers={"content-type": "text/event-stream"}
    )


def _sse_call(alias: str) -> bytes:
    frames = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": alias,
                "arguments": "",
            },
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": alias,
                "arguments": '{"index":0}',
            },
        },
        {"type": "response.completed", "response": {"output": []}},
    ]
    return "".join(
        f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n" for frame in frames
    ).encode()


def _tool_use_names(events: list[str]) -> list[str]:
    names: list[str] = []
    for event in events:
        for line in event.splitlines():
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[len("data: ") :])
            block = payload.get("content_block") or {}
            if block.get("type") == "tool_use":
                names.append(block["name"])
    return names


def _install(transport: ResponsesTransport, handler: Any) -> list[dict[str, Any]]:
    """Point one transport at a mock and collect the bodies it sends."""

    sent: list[dict[str, Any]] = []

    def _handler(http_request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(http_request.content))
        return handler(len(sent), sent[-1])

    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return sent


async def _run(transport: ResponsesTransport, request: MessagesRequest) -> list[str]:
    body, headers = transport.build_body(
        request, reasoning=REASONING, max_output_tokens=512
    )
    return [
        event
        async for event in transport.stream(
            request,
            input_tokens=0,
            reasoning=REASONING,
            body=body,
            headers=headers,
            surface_label="responses",
        )
    ]


# -- the tool-name-length net -------------------------------------------------


@pytest.mark.asyncio
async def test_a_stated_tool_name_ceiling_is_retried_once_and_remembered() -> None:
    store = _store()
    transport = _transport(store)
    request = _request()
    alias_holder: list[str] = []

    def handler(attempt: int, body: dict[str, Any]) -> httpx.Response:
        if attempt == 1:
            assert any(len(tool["name"]) > 64 for tool in body["tools"])
            return _refusal(NAME_TOO_LONG, "name")
        alias_holder.append(body["tool_choice"]["name"])
        return _accepted(alias_holder[0])

    sent = _install(transport, handler)
    events = await _run(transport, request)
    await transport.aclose()

    assert len(sent) == 2
    # The retry is legal everywhere a name appears, and the model's call comes
    # back to the client under the name the client actually sent.
    assert all(len(tool["name"]) <= 64 for tool in sent[1]["tools"])
    assert len(sent[1]["tool_choice"]["name"]) <= 64
    assert _tool_use_names(events) == [CONSOLE]

    facts = store.facts_for_provider(PROVIDER)
    assert [(f.fact_kind, f.model_id, f.value) for f in facts] == [
        (FACT_RESPONSES_TOOL_NAME_MAX_LENGTH, PROVIDER_WIDE_MODEL_ID, 64)
    ]
    assert facts[0].ttl_seconds == STATED_FACT_TTL_SECONDS
    assert facts[0].source == "rejection"
    assert "at most 64 characters" in facts[0].evidence


@pytest.mark.asyncio
async def test_the_next_request_aliases_from_the_first_try() -> None:
    """The second request is the first one's retry body, byte for byte."""

    store = _store()
    first = _transport(store)
    request = _request()

    def handler(attempt: int, body: dict[str, Any]) -> httpx.Response:
        if attempt == 1:
            return _refusal(NAME_TOO_LONG, "name")
        return _accepted(body["tool_choice"]["name"])

    sent = _install(first, handler)
    await _run(first, request)
    await first.aclose()

    # A brand-new transport, as a config apply builds: the memory is the
    # store's, so what the host said survives the rebuild.
    second = _transport(store)
    assert second.tool_name_max_length == 64
    body, _ = second.build_body(request, reasoning=REASONING, max_output_tokens=512)
    await second.aclose()
    assert body == sent[1]


@pytest.mark.asyncio
async def test_a_ceiling_the_host_states_below_64_is_honoured() -> None:
    store = _store()
    transport = _transport(store)

    def handler(attempt: int, body: dict[str, Any]) -> httpx.Response:
        if attempt == 1:
            return _refusal("`name` must be at most 48 characters, got 68", "name")
        return _accepted(body["tool_choice"]["name"])

    sent = _install(transport, handler)
    await _run(transport, _request())
    await transport.aclose()

    assert all(len(tool["name"]) <= 48 for tool in sent[1]["tools"])
    assert store.memory_for(PROVIDER).responses_tool_name_max_length == 48


@pytest.mark.asyncio
async def test_an_unreadable_number_falls_back_to_64() -> None:
    store = _store()
    transport = _transport(store)

    def handler(attempt: int, body: dict[str, Any]) -> httpx.Response:
        if attempt == 1:
            return _refusal("`name` is too long for this deployment", "name")
        return _accepted(body["tool_choice"]["name"])

    sent = _install(transport, handler)
    await _run(transport, _request())
    await transport.aclose()

    assert all(len(tool["name"]) <= 64 for tool in sent[1]["tools"])
    assert store.memory_for(PROVIDER).responses_tool_name_max_length == 64


@pytest.mark.asyncio
async def test_a_host_that_keeps_refusing_fails_visibly_after_one_retry() -> None:
    store = _store()
    transport = _transport(store)

    def handler(attempt: int, body: dict[str, Any]) -> httpx.Response:
        return _refusal(NAME_TOO_LONG, "name")

    sent = _install(transport, handler)
    with pytest.raises(ExecutionFailure) as caught:
        await _run(transport, _request())
    await transport.aclose()

    assert len(sent) == 2
    assert "400" in str(caught.value) or "400" in repr(caught.value)
    # Nothing was proven, so nothing is remembered.
    assert store.facts_for_provider(PROVIDER) == ()


def test_a_declared_limit_still_outranks_a_learned_one() -> None:
    store = _store()
    store.memory_for(PROVIDER).responses_tool_name_max_length = 48
    transport = _transport(store, declared=64)
    assert transport.tool_name_max_length == 64


# -- the tool_choice net ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_tool_choice_refusal_is_retried_once_without_the_field() -> None:
    store = _store()
    transport = _transport(store, declared=64)
    request = _request()

    def handler(attempt: int, body: dict[str, Any]) -> httpx.Response:
        if attempt == 1:
            assert "tool_choice" in body
            return _refusal(TOOL_CHOICE_AUTO_ONLY, "tool_choice")
        return _accepted(body["tools"][0]["name"])

    sent = _install(transport, handler)
    await _run(transport, request)
    await transport.aclose()

    assert len(sent) == 2
    assert "tool_choice" not in sent[1]
    # Everything else about the retry is the request that was refused.
    assert {k: v for k, v in sent[0].items() if k != "tool_choice"} == sent[1]

    facts = store.facts_for_provider(PROVIDER)
    assert [(f.fact_kind, f.model_id, f.value) for f in facts] == [
        (FACT_RESPONSES_TOOL_CHOICE_AUTO_ONLY, request.model, True)
    ]
    assert facts[0].ttl_seconds == INFERRED_FACT_TTL_SECONDS


@pytest.mark.asyncio
async def test_the_next_request_omits_tool_choice_and_marks_the_wire() -> None:
    store = _store()
    memory = store.memory_for(PROVIDER)
    request = _request()
    memory.remember_responses_tool_choice_refusal(request.model, evidence="only auto")

    transport = _transport(store, declared=64)

    def handler(attempt: int, body: dict[str, Any]) -> httpx.Response:
        return _accepted(body["tools"][0]["name"])

    sent = _install(transport, handler)
    wire = install_wire_trace()
    await _run(transport, request)
    await transport.aclose()

    assert len(sent) == 1
    assert "tool_choice" not in sent[0]
    marker = wire.requests[0].params["tool_choice_dropped"]
    assert marker.startswith("this model accepts only auto (learned ")


@pytest.mark.asyncio
async def test_a_tool_choice_400_for_another_reason_does_not_trigger_the_net():
    """Echo safety: the refused body itself names ``tool_choice`` and ``auto``."""

    store = _store()
    transport = _transport(store, declared=64)

    def handler(attempt: int, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "error": {
                    "param": "tool_choice",
                    "type": "invalid_request_error",
                    "message": "tool_choice names a function that is not in tools",
                },
                # The whole submitted request, echoed back the way a
                # pydantic-style validator does -- including the literal word
                # ``auto`` and an "is supported" sentence planted to prove the
                # matcher never reads this half.
                "input": {
                    **body,
                    "note": 'only `"auto"` is supported for `tool_choice`',
                },
            },
        )

    sent = _install(transport, handler)
    with pytest.raises(ExecutionFailure):
        await _run(transport, _request())
    await transport.aclose()

    assert len(sent) == 1
    assert store.facts_for_provider(PROVIDER) == ()


# -- the ladder ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_ladder_rows_name_the_rung_that_produced_the_retry() -> None:
    store = _store()
    transport = _transport(
        store, declared=64, limiter=ImmediateRetryProviderRateLimiter()
    )

    def handler(attempt: int, body: dict[str, Any]) -> httpx.Response:
        if attempt == 1:
            return _refusal(TOOL_CHOICE_AUTO_ONLY, "tool_choice")
        return _accepted(body["tools"][0]["name"])

    _install(transport, handler)
    install_ladder_trace()
    await _run(transport, _request())
    await transport.aclose()

    trace = current_ladder()
    assert trace is not None
    rows = ladder_payload(trace.slot())["tries"]
    assert [row.get("status") for row in rows] == [400, None]
    assert "recovery" not in rows[0]
    assert rows[1]["recovery"] == "responses_tool_choice"


# -- the Models page ----------------------------------------------------------


def test_every_learnable_fact_kind_has_a_name_an_operator_can_read() -> None:
    """A kind with no label renders as its own identifier, which is a bug."""

    from my_claude_code.api.model_admin import FACT_KIND_LABELS
    from my_claude_code.providers.recovery import ALLOWED_FACT_KINDS

    assert set(ALLOWED_FACT_KINDS) <= set(FACT_KIND_LABELS)


def test_the_two_new_facts_render_in_the_learned_column() -> None:
    """Origin, expiry and the host's own words, on the row the page draws."""

    from my_claude_code.api.model_admin import learned_payload

    store = _store()
    memory = store.memory_for(PROVIDER)
    memory.learn_responses_tool_name_limit(64, evidence=NAME_TOO_LONG)
    memory.remember_responses_tool_choice_refusal(
        "muse-spark-1.3-contributor-free", evidence=TOOL_CHOICE_AUTO_ONLY
    )

    rendered = {
        row["fact_kind"]: row
        for row in (
            learned_payload(
                {
                    "fact_kind": fact.fact_kind,
                    "value": fact.value,
                    "detail": fact.detail,
                    "source": fact.source,
                    "learned_at": fact.learned_at,
                    "last_confirmed_at": fact.last_confirmed_at,
                    "ttl_seconds": fact.ttl_seconds,
                    "evidence": fact.evidence,
                    "hits": fact.hits,
                }
            )
            for fact in store.facts_for_provider(PROVIDER)
        )
    }

    limit = rendered[FACT_RESPONSES_TOOL_NAME_MAX_LENGTH]
    assert limit["fact_label"] == "tool-name limit"
    assert limit["source_label"] == "the host's own rejection"
    assert limit["value"] == 64
    assert limit["ttl_seconds"] == STATED_FACT_TTL_SECONDS
    assert "at most 64 characters" in limit["evidence"]

    choice = rendered[FACT_RESPONSES_TOOL_CHOICE_AUTO_ONLY]
    assert choice["fact_label"] == "tool_choice auto only"
    assert choice["source_label"] == "the host's own rejection"
    assert choice["ttl_seconds"] == INFERRED_FACT_TTL_SECONDS


def test_a_host_wide_tool_name_limit_is_drawn_on_every_model_of_that_host():
    """And carries its own subject, so *Forget* removes it once, not per row.

    A fact stored under ``PROVIDER_WIDE_MODEL_ID`` had no row anywhere on the
    page before 7.23.0 -- ``models_etag`` is the only other one, and it is
    catalogue plumbing nobody was looking for. A tool-name ceiling decides
    what the next body carries, so it is drawn beside the model it shapes.
    """

    from my_claude_code.api.model_admin import facts_for_row, learned_payload

    learned = {
        "opencode/*": [
            {
                "fact_kind": FACT_RESPONSES_TOOL_NAME_MAX_LENGTH,
                "value": 64,
                "model_id": PROVIDER_WIDE_MODEL_ID,
                "source": "rejection",
            },
            {
                "fact_kind": "models_etag",
                "value": {"etag": "x"},
                "model_id": PROVIDER_WIDE_MODEL_ID,
                "source": "observation",
            },
        ],
        "opencode/muse-spark-1.3-contributor-free": [
            {
                "fact_kind": FACT_RESPONSES_TOOL_CHOICE_AUTO_ONLY,
                "value": True,
                "model_id": "muse-spark-1.3-contributor-free",
                "source": "rejection",
            },
        ],
    }
    rows = facts_for_row(learned, "opencode", "muse-spark-1.3-contributor-free")
    assert [row["fact_kind"] for row in rows] == [
        FACT_RESPONSES_TOOL_CHOICE_AUTO_ONLY,
        FACT_RESPONSES_TOOL_NAME_MAX_LENGTH,
    ]
    assert learned_payload(rows[1])["model_id"] == PROVIDER_WIDE_MODEL_ID
    # Another model of the same host gets the host-wide row too.
    other = facts_for_row(learned, "opencode", "mimo-v2.5-free")
    assert [row["fact_kind"] for row in other] == [FACT_RESPONSES_TOOL_NAME_MAX_LENGTH]


def test_the_models_page_copy_of_the_provider_wide_id_matches_the_store() -> None:
    """``api`` may not import ``providers``; the mirror must not drift."""

    from my_claude_code.api import model_admin

    assert model_admin.PROVIDER_WIDE_MODEL_ID == PROVIDER_WIDE_MODEL_ID
