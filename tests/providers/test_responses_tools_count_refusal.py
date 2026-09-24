"""A Responses host caps how many tools one request may carry; both senders fit it once.

The refusal these tests replay is OpenAI's documented one, reproduced publicly
against Claude Code catalogues (``musistudio/claude-code-router`` #686,
``zed-industries/zed`` #42393, ``code-yeongyu/oh-my-openagent`` #2848):

    {"error": {"message": "Invalid 'tools': array too long. Expected an array
     with maximum length 128, but got an array with length 212 instead.",
     "type": "invalid_request_error", "param": "tools",
     "code": "array_above_max_length"}}

Four things have to be true together, and each is a section below:

* the matcher reads the host's **structured** words, names the ``tools`` array
  specifically, and answers only a number the host **stated**;
* the cut never removes a tool the conversation used, the tool ``tool_choice``
  forces, or a tool without a name -- it trims the rest **from the end**, in
  the client's order, deterministically;
* the stated number is remembered per provider, so the next request is cut
  before the first send, a restart keeps it and *Forget* drops it;
* a refusal no cut can satisfy leaves the failure exactly as it was: the same
  ``model_rejected`` that falls through to the next model today.

And one thing must **not** change: a host that has stated no cap -- every host
today, since every declared dialect says ``None`` -- sends the catalogue byte
for byte as it always did, however long it is.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.core.upstream_ladder import (
    current_ladder,
    install_ladder_trace,
    ladder_payload,
)
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth import ChatGPTOAuthProvider
from my_claude_code.providers.chatgpt_oauth.conversion import (
    CHATGPT_OAUTH_TOOL_SCHEMA_DIALECT,
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.chatgpt_oauth.provider import CHATGPT_OAUTH_DEFAULT_BASE
from my_claude_code.providers.openai_chat.profiles import OpenAIChatProfile
from my_claude_code.providers.openai_chat.responses_transport import ResponsesTransport
from my_claude_code.providers.openai_responses import (
    PERMISSIVE_TOOL_SCHEMA_DIALECT,
    RESPONSES_TOOL_SCHEMA_DIALECT,
    ToolSchemaDialect,
)
from my_claude_code.providers.recovery import (
    FACT_RESPONSES_TOOLS_MAX_COUNT,
    PROVIDER_WIDE_MODEL_ID,
    RUNG_TOOL_SCHEMA,
    RUNG_TOOLS_COUNT,
    STATED_FACT_TTL_SECONDS,
    TOOLS_TRIMMED,
    LearnedFactStore,
    RecoveryMemory,
    apply_tools_max_count,
    effective_tools_max_count,
    forced_tool_names,
    history_tool_names,
    rejected_tool_name_max_length,
    rejected_tool_schema_keyword,
    rejected_tools_max_count,
    tools_count_recovery,
    trim_tool_catalogue,
)
from my_claude_code.providers.recovery import store as learned_store
from tests.providers.support import (
    ImmediateRetryProviderRateLimiter,
    passthrough_rate_limiter,
)

PROVIDER = "muse_gateway"
REASONING = ReasoningPolicy.on()

#: The catalogue size every 212-tool request that died on 2026-09-20 carried.
CATALOGUE_SIZE = 212
STATED_MAXIMUM = 128

COUNT_REFUSAL = {
    "error": {
        "message": (
            "Invalid 'tools': array too long. Expected an array with maximum "
            "length 128, but got an array with length 212 instead."
        ),
        "type": "invalid_request_error",
        "param": "tools",
        "code": "array_above_max_length",
    }
}

#: The same code about a *different* array. Removing tools cannot fix it.
INPUT_REFUSAL = {
    "error": {
        "message": (
            "Invalid 'input': array too long. Expected an array with maximum "
            "length 2048, but got an array with length 3000 instead."
        ),
        "type": "invalid_request_error",
        "param": "input",
        "code": "array_above_max_length",
    }
}

SCHEMA_REFUSAL = {
    "error": {
        "message": (
            "Invalid JSON schema: regex lookaround is not supported. "
            "Found at $.properties.videoScale.pattern."
        ),
        "type": "invalid_request_error",
        "param": "tools",
        "code": "invalid_json_schema",
    }
}


def _error(payload: Any, status: int = 400) -> httpx.HTTPStatusError:
    """An error shaped exactly like either Responses sender raises."""

    request = httpx.Request("POST", "https://upstream.invalid/responses")
    response = httpx.Response(status, request=request, json=payload)
    return httpx.HTTPStatusError(
        f"Responses API error {status}", request=request, response=response
    )


def _refusal(maximum: int, got: int) -> dict[str, Any]:
    return {
        "error": {
            "message": (
                "Invalid 'tools': array too long. Expected an array with maximum "
                f"length {maximum}, but got an array with length {got} instead."
            ),
            "type": "invalid_request_error",
            "param": "tools",
            "code": "array_above_max_length",
        }
    }


def _name(index: int) -> str:
    return f"mcp__srv__tool_{index:03d}"


def _wire(body: Any) -> str:
    return json.dumps(body)


def _wire_params(trace: Any) -> dict[str, Any]:
    return trace.requests[max(trace.requests)].params


# --------------------------------------------------------------------------
# The matcher
# --------------------------------------------------------------------------


def test_the_documented_wording_states_the_maximum() -> None:
    assert rejected_tools_max_count(_error(COUNT_REFUSAL)) == STATED_MAXIMUM


def test_a_host_that_omits_param_but_names_tools_in_the_message_is_read() -> None:
    payload = {"error": {"message": COUNT_REFUSAL["error"]["message"]}}
    assert rejected_tools_max_count(_error(payload)) == STATED_MAXIMUM


def test_the_same_code_about_another_array_is_not_this_rungs_business() -> None:
    assert rejected_tools_max_count(_error(INPUT_REFUSAL)) is None


def test_a_count_refusal_with_no_stated_number_is_not_guessed_at() -> None:
    payload = {
        "error": {
            "message": "Invalid 'tools': array too long.",
            "param": "tools",
            "code": "array_above_max_length",
        }
    }
    assert rejected_tools_max_count(_error(payload)) is None


def test_a_stated_maximum_of_zero_is_not_a_count_to_cut_to() -> None:
    assert rejected_tools_max_count(_error(_refusal(0, 3))) is None


def test_a_non_400_is_never_this_rungs_business() -> None:
    assert rejected_tools_max_count(_error(COUNT_REFUSAL, status=500)) is None


def test_an_echoed_request_carrying_the_same_words_does_not_match() -> None:
    """The request may *contain* the sentence; only the host's words count."""

    payload = {
        "error": {"message": "Bad request", "type": "invalid_request_error"},
        "input": {
            "tools": [
                {
                    "name": "x",
                    "description": COUNT_REFUSAL["error"]["message"]
                    + " array_above_max_length",
                    "param": "tools",
                }
            ]
        },
    }
    assert rejected_tools_max_count(_error(payload)) is None


def test_the_neighbouring_matchers_leave_the_count_refusal_alone() -> None:
    """Order is total, not conventional: nobody else reads this 400."""

    assert rejected_tool_schema_keyword(_error(COUNT_REFUSAL)) is None
    assert rejected_tool_name_max_length(_error(COUNT_REFUSAL)) is None
    # And this matcher leaves the schema refusal to its own rung.
    assert rejected_tools_max_count(_error(SCHEMA_REFUSAL)) is None


# --------------------------------------------------------------------------
# The cut
# --------------------------------------------------------------------------


def _tool(index: int) -> dict[str, Any]:
    return {
        "type": "function",
        "name": _name(index),
        "description": f"tool {index}",
        "parameters": {"type": "object"},
    }


def _catalogue(count: int) -> list[dict[str, Any]]:
    return [_tool(index) for index in range(count)]


def _body(
    tools: list[Any], *, used: tuple[str, ...] = (), choice: Any = None
) -> dict[str, Any]:
    items: list[dict[str, Any]] = [{"type": "message", "role": "user", "content": "hi"}]
    for index, name in enumerate(used):
        items.append(
            {
                "type": "function_call",
                "call_id": f"call_{index}",
                "name": name,
                "arguments": "{}",
            }
        )
        items.append(
            {"type": "function_call_output", "call_id": f"call_{index}", "output": "ok"}
        )
    body: dict[str, Any] = {"model": "m", "input": items, "tools": tools}
    if choice is not None:
        body["tool_choice"] = choice
    return body


def test_a_catalogue_that_fits_is_not_cut() -> None:
    tools = _catalogue(5)
    assert trim_tool_catalogue(tools, 5, _body(tools)) is None
    assert trim_tool_catalogue(tools, 6, _body(tools)) is None


def test_the_cut_trims_from_the_end_in_the_clients_order() -> None:
    tools = _catalogue(10)
    trim = trim_tool_catalogue(tools, 6, _body(tools))
    assert trim is not None
    assert [tool["name"] for tool in trim.tools] == [_name(i) for i in range(6)]
    assert trim.dropped == tuple(_name(i) for i in range(6, 10))
    # The kept definitions are the client's own dicts, so their bytes cannot
    # have moved, and the handed-in list is untouched.
    assert all(
        kept is original for kept, original in zip(trim.tools, tools[:6], strict=True)
    )
    assert len(tools) == 10


def test_a_tool_the_conversation_used_is_never_dropped() -> None:
    tools = _catalogue(10)
    body = _body(tools, used=(_name(9), _name(8)))
    trim = trim_tool_catalogue(tools, 6, body)
    assert trim is not None
    kept = [tool["name"] for tool in trim.tools]
    assert _name(9) in kept and _name(8) in kept
    # Still the client's order: the protected ones stay where they were, and
    # the room left over goes to the earliest unprotected tools.
    assert kept == [_name(i) for i in (0, 1, 2, 3, 8, 9)]
    assert trim.dropped == tuple(_name(i) for i in (4, 5, 6, 7))
    assert trim.protected_count == 2


@pytest.mark.parametrize(
    "choice",
    [
        {"type": "function", "name": _name(9)},
        {"type": "function", "function": {"name": _name(9)}},
        {"type": "allowed_tools", "tools": [{"type": "function", "name": _name(9)}]},
    ],
)
def test_the_tool_a_forced_choice_names_is_never_dropped(choice: Any) -> None:
    tools = _catalogue(10)
    trim = trim_tool_catalogue(tools, 3, _body(tools, choice=choice))
    assert trim is not None
    assert [tool["name"] for tool in trim.tools] == [_name(0), _name(1), _name(9)]


def test_required_and_auto_pin_nothing() -> None:
    assert forced_tool_names("required") == frozenset()
    assert forced_tool_names("auto") == frozenset()
    assert forced_tool_names({"type": "auto"}) == frozenset()


def test_a_tool_without_a_name_is_never_a_candidate() -> None:
    tools: list[Any] = [*_catalogue(4), {"type": "web_search"}]
    trim = trim_tool_catalogue(tools, 3, _body(tools))
    assert trim is not None
    assert trim.tools[-1] == {"type": "web_search"}
    assert trim.dropped == (_name(2), _name(3))


def test_when_the_protected_tools_alone_exceed_the_cap_there_is_no_cut() -> None:
    tools = _catalogue(10)
    body = _body(
        tools, used=(_name(7), _name(8)), choice={"type": "function", "name": _name(9)}
    )
    assert trim_tool_catalogue(tools, 2, body) is None
    # Exactly at the cap is a cut that keeps only the protected tools.
    trim = trim_tool_catalogue(tools, 3, body)
    assert trim is not None
    assert [tool["name"] for tool in trim.tools] == [_name(7), _name(8), _name(9)]


def test_history_names_come_from_every_replayed_call() -> None:
    items = [
        {"type": "function_call", "call_id": "a", "name": "one", "arguments": "{}"},
        {"type": "custom_tool_call", "call_id": "b", "name": "two", "input": ""},
        {"type": "function_call_output", "call_id": "a", "output": "x"},
        {"type": "message", "role": "assistant", "name": "not-a-tool"},
    ]
    assert history_tool_names(items) == frozenset({"one", "two"})
    assert history_tool_names(None) == frozenset()


def test_the_cut_is_stable_as_the_conversation_uses_kept_tools() -> None:
    """The prompt-cache property: using a kept tool does not reshuffle the set."""

    tools = _catalogue(20)
    first = trim_tool_catalogue(tools, 8, _body(tools))
    assert first is not None
    for used in ((_name(3),), (_name(3), _name(0)), (_name(3), _name(0), _name(7))):
        later = trim_tool_catalogue(tools, 8, _body(tools, used=used))
        assert later is not None
        assert _wire(later.tools) == _wire(first.tools)


def test_the_cut_is_deterministic() -> None:
    tools = _catalogue(CATALOGUE_SIZE)
    body = _body(tools, used=(_name(200),))
    one = trim_tool_catalogue(tools, STATED_MAXIMUM, body)
    two = trim_tool_catalogue(list(tools), STATED_MAXIMUM, dict(body))
    assert one is not None and two is not None
    assert _wire(one.tools) == _wire(two.tools)
    assert len(one.tools) == STATED_MAXIMUM


def test_the_record_names_every_dropped_tool_and_nothing_else() -> None:
    tools = _catalogue(CATALOGUE_SIZE)
    recovery = tools_count_recovery(_error(COUNT_REFUSAL), _body(tools))
    assert recovery is not None
    line = recovery.marker[TOOLS_TRIMMED]
    assert line.startswith(
        "dropped 84 of 212 tools from the end to fit a maximum of 128"
    )
    assert "(stated by this host)" in line
    for index in range(STATED_MAXIMUM, CATALOGUE_SIZE):
        assert _name(index) in line
    assert _name(STATED_MAXIMUM - 1) not in line
    # Names only: no description, no schema.
    assert "tool 200" not in line
    assert "parameters" not in line


def test_the_effective_cap_is_the_smaller_known_number() -> None:
    assert effective_tools_max_count(None, None) == (None, "")
    assert effective_tools_max_count(128, None) == (128, "declared")
    assert effective_tools_max_count(None, 100) == (100, "learned")
    assert effective_tools_max_count(128, 100) == (100, "learned")
    assert effective_tools_max_count(100, 128) == (100, "declared")


def test_no_cap_leaves_the_same_tools_list() -> None:
    tools = _catalogue(CATALOGUE_SIZE)
    body = _body(tools)
    applied, marker = apply_tools_max_count(body, None, "")
    assert applied["tools"] is tools
    assert marker == {}
    applied, marker = apply_tools_max_count(body, 500, "learned from this host")
    assert applied["tools"] is tools
    assert marker == {}


def test_every_declared_dialect_says_unknown_which_is_no_cap() -> None:
    assert RESPONSES_TOOL_SCHEMA_DIALECT.tools_max_count is None
    assert PERMISSIVE_TOOL_SCHEMA_DIALECT.tools_max_count is None
    assert CHATGPT_OAUTH_TOOL_SCHEMA_DIALECT.tools_max_count is None


def test_the_memory_keeps_the_narrowest_stated_count() -> None:
    written: list[tuple[Any, ...]] = []
    memory = RecoveryMemory(sink=lambda *row: written.append(row))
    assert memory.learn_responses_tools_max_count(128) == 128
    assert memory.learn_responses_tools_max_count(200) == 128
    assert memory.learn_responses_tools_max_count(100) == 100
    assert written[-1][:3] == (
        FACT_RESPONSES_TOOLS_MAX_COUNT,
        PROVIDER_WIDE_MODEL_ID,
        100,
    )


# --------------------------------------------------------------------------
# The rung on the shared Responses transport
# --------------------------------------------------------------------------


def _request(
    count: int = CATALOGUE_SIZE,
    *,
    used: tuple[int, ...] = (),
    forced: int | None = None,
) -> MessagesRequest:
    messages: list[dict[str, Any]] = [{"role": "user", "content": "hi"}]
    for number, index in enumerate(used):
        messages.append(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": f"toolu_{number}",
                        "name": _name(index),
                        "input": {},
                    }
                ],
            }
        )
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": f"toolu_{number}",
                        "content": "ok",
                    }
                ],
            }
        )
    payload: dict[str, Any] = {
        "model": "muse-spark-1.3",
        "max_tokens": 64,
        "messages": messages,
        "tools": [
            {
                "name": _name(index),
                "description": f"tool {index}",
                "input_schema": {"type": "object"},
            }
            for index in range(count)
        ],
    }
    if forced is not None:
        payload["tool_choice"] = {"type": "tool", "name": _name(forced)}
    return MessagesRequest.model_validate(payload)


def _transport(
    store: LearnedFactStore,
    *,
    limiter: Any = None,
    dialect: ToolSchemaDialect = PERMISSIVE_TOOL_SCHEMA_DIALECT,
) -> ResponsesTransport:
    return ResponsesTransport(
        ProviderConfig(api_key="sk-test", base_url="https://example.invalid/v1"),
        base_url="https://example.invalid/v1",
        provider_name="MUSE",
        identity=None,
        api_key=None,
        rate_limiter=limiter or passthrough_rate_limiter(),
        tool_name_max_length=None,
        memory=store.memory_for(PROVIDER),
        tool_schema_dialect=dialect,
    )


def _accepted() -> httpx.Response:
    frames = [
        {"type": "response.output_text.delta", "delta": "ok"},
        {"type": "response.completed", "response": {"output": []}},
    ]
    payload = "".join(
        f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n" for frame in frames
    ).encode()

    async def _body():
        yield payload

    return httpx.Response(
        200, content=_body(), headers={"content-type": "text/event-stream"}
    )


def _install(transport: ResponsesTransport, handler: Any) -> list[dict[str, Any]]:
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


def _caps_at(maximum: int) -> Any:
    """A fake upstream with the platform validator's one rule."""

    def handler(attempt: int, body: dict[str, Any]) -> httpx.Response:
        if len(body["tools"]) > maximum:
            return httpx.Response(400, json=_refusal(maximum, len(body["tools"])))
        return _accepted()

    return handler


def _names(body: dict[str, Any]) -> list[str]:
    return [tool["name"] for tool in body["tools"]]


@pytest.mark.asyncio
async def test_the_transport_cuts_retries_once_and_remembers() -> None:
    store = LearnedFactStore()
    transport = _transport(store, limiter=ImmediateRetryProviderRateLimiter())
    trace = install_wire_trace()
    install_ladder_trace()

    sent = _install(transport, _caps_at(STATED_MAXIMUM))
    events = await _run(transport, _request(used=(211, 150)))
    await transport.aclose()

    assert len(sent) == 2
    assert len(sent[0]["tools"]) == CATALOGUE_SIZE
    assert len(sent[1]["tools"]) == STATED_MAXIMUM
    kept = _names(sent[1])
    assert _name(211) in kept and _name(150) in kept
    assert kept == [_name(i) for i in range(126)] + [_name(150), _name(211)]
    assert any("ok" in event for event in events)

    ladder = current_ladder()
    assert ladder is not None
    rows = ladder_payload(ladder.slot())["tries"]
    assert [row.get("status") for row in rows] == [400, None]
    assert rows[1]["recovery"] == RUNG_TOOLS_COUNT

    line = _wire_params(trace)[TOOLS_TRIMMED]
    assert "dropped 84 of 212 tools" in line
    assert _name(126) in line and _name(210) in line
    assert _name(211) not in line and _name(150) not in line

    facts = store.facts_for_provider(PROVIDER)
    assert [(f.fact_kind, f.model_id, f.value) for f in facts] == [
        (FACT_RESPONSES_TOOLS_MAX_COUNT, PROVIDER_WIDE_MODEL_ID, STATED_MAXIMUM)
    ]
    assert facts[0].source == "rejection"
    assert facts[0].ttl_seconds == STATED_FACT_TTL_SECONDS
    assert "array too long" in facts[0].evidence


@pytest.mark.asyncio
async def test_the_next_request_is_already_cut_and_costs_one_call() -> None:
    store = LearnedFactStore()
    first = _transport(store)
    _install(first, _caps_at(STATED_MAXIMUM))
    await _run(first, _request())
    await first.aclose()

    second = _transport(store)
    trace = install_wire_trace()
    sent = _install(second, _caps_at(STATED_MAXIMUM))
    await _run(second, _request())
    await second.aclose()

    assert len(sent) == 1
    assert len(sent[0]["tools"]) == STATED_MAXIMUM
    assert "(learned from this host)" in _wire_params(trace)[TOOLS_TRIMMED]


@pytest.mark.asyncio
async def test_a_restart_reloads_the_count_and_a_forget_re_pays_the_400(
    tmp_path: Path,
) -> None:
    path = tmp_path / "learned_facts.json"
    store = LearnedFactStore(path=path)
    transport = _transport(store)
    _install(transport, _caps_at(STATED_MAXIMUM))
    await _run(transport, _request())
    await transport.aclose()
    assert store.flush()

    restarted = LearnedFactStore()
    restarted.enable_persistence(path)
    after_restart = _transport(restarted)
    sent = _install(after_restart, _caps_at(STATED_MAXIMUM))
    await _run(after_restart, _request())
    await after_restart.aclose()
    assert len(sent) == 1

    assert restarted.forget(
        PROVIDER, PROVIDER_WIDE_MODEL_ID, FACT_RESPONSES_TOOLS_MAX_COUNT
    )
    assert restarted.memory_for(PROVIDER).responses_tools_max_count is None
    forgotten = _transport(restarted)
    sent = _install(forgotten, _caps_at(STATED_MAXIMUM))
    await _run(forgotten, _request())
    await forgotten.aclose()
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_a_host_that_refuses_the_cut_catalogue_too_fails_unchanged() -> None:
    """The binding non-decision: the 400 keeps falling through, nothing learned."""

    store = LearnedFactStore()
    transport = _transport(store)
    install_ladder_trace()

    def always_refuses(attempt: int, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(400, json=COUNT_REFUSAL)

    sent = _install(transport, always_refuses)
    with pytest.raises(ExecutionFailure) as caught:
        await _run(transport, _request())
    await transport.aclose()

    assert len(sent) == 2
    assert caught.value.kind.value == "model_rejected"
    assert caught.value.status_code == 400
    assert store.facts_for_provider(PROVIDER) == ()


@pytest.mark.asyncio
async def test_when_no_cut_keeps_the_rules_the_original_400_falls_through() -> None:
    """Three protected tools and a stated maximum of two: one send, no rewrite."""

    store = LearnedFactStore()
    transport = _transport(store)
    sent = _install(transport, _caps_at(2))

    with pytest.raises(ExecutionFailure) as caught:
        await _run(transport, _request(count=5, used=(1, 2), forced=4))
    await transport.aclose()

    assert len(sent) == 1
    assert caught.value.kind.value == "model_rejected"
    assert caught.value.status_code == 400
    assert store.facts_for_provider(PROVIDER) == ()


@pytest.mark.asyncio
async def test_a_host_that_never_refuses_sends_the_bytes_it_always_did() -> None:
    """The equality contract: no cap known, 212 tools, one identical send."""

    store = LearnedFactStore()
    transport = _transport(store, dialect=RESPONSES_TOOL_SCHEMA_DIALECT)
    request = _request()
    baseline, _ = transport.build_body(
        request, reasoning=REASONING, max_output_tokens=512
    )
    trace = install_wire_trace()

    sent = _install(transport, lambda attempt, body: _accepted())
    await _run(transport, request)
    await transport.aclose()

    assert len(sent) == 1
    assert _wire(sent[0]) == _wire(baseline)
    assert len(sent[0]["tools"]) == CATALOGUE_SIZE
    assert TOOLS_TRIMMED not in _wire_params(trace)


@pytest.mark.asyncio
async def test_a_declared_cap_is_cut_to_before_the_first_send() -> None:
    store = LearnedFactStore()
    dialect = ToolSchemaDialect(name="capped", tools_max_count=100)
    transport = _transport(store, dialect=dialect)
    trace = install_wire_trace()
    sent = _install(transport, _caps_at(100))

    await _run(transport, _request())
    await transport.aclose()

    assert len(sent) == 1
    assert len(sent[0]["tools"]) == 100
    assert "(declared capped dialect)" in _wire_params(trace)[TOOLS_TRIMMED]
    # Declared, not learned: nothing was refused, so nothing is written down.
    assert store.facts_for_provider(PROVIDER) == ()


@pytest.mark.asyncio
async def test_a_host_stating_less_than_its_declared_cap_is_believed() -> None:
    store = LearnedFactStore()
    dialect = ToolSchemaDialect(name="capped", tools_max_count=128)
    first = _transport(store, dialect=dialect)
    sent = _install(first, _caps_at(90))
    await _run(first, _request())
    await first.aclose()
    assert [len(body["tools"]) for body in sent] == [128, 90]

    second = _transport(store, dialect=dialect)
    sent = _install(second, _caps_at(90))
    await _run(second, _request())
    await second.aclose()
    assert [len(body["tools"]) for body in sent] == [90]


@pytest.mark.asyncio
async def test_a_request_needing_schema_and_count_recovery_succeeds() -> None:
    """Two rungs, two rewrites, one request -- each firing at most once."""

    store = LearnedFactStore()
    transport = _transport(store, limiter=ImmediateRetryProviderRateLimiter())
    request = _request()
    schema = {
        "type": "object",
        "properties": {"v": {"type": "string", "pattern": "^a(?![\\s\\S])"}},
    }
    assert request.tools is not None
    request.tools[0].input_schema = schema

    def handler(attempt: int, body: dict[str, Any]) -> httpx.Response:
        if "(?!" in json.dumps(body["tools"]):
            return httpx.Response(400, json=SCHEMA_REFUSAL)
        if len(body["tools"]) > STATED_MAXIMUM:
            return httpx.Response(400, json=COUNT_REFUSAL)
        return _accepted()

    install_ladder_trace()
    sent = _install(transport, handler)
    events = await _run(transport, request)
    await transport.aclose()

    assert len(sent) == 3
    assert len(sent[2]["tools"]) == STATED_MAXIMUM
    assert "(?!" not in json.dumps(sent[2]["tools"])
    assert any("ok" in event for event in events)
    ladder = current_ladder()
    assert ladder is not None
    rows = ladder_payload(ladder.slot())["tries"]
    assert [row.get("recovery") for row in rows[1:]] == [
        RUNG_TOOL_SCHEMA,
        RUNG_TOOLS_COUNT,
    ]


def test_every_profile_reaches_the_transport_with_no_cap() -> None:
    """Unknown means no cap: no shipped profile declares a number."""

    from my_claude_code.providers.openai_chat import profiles

    declared = {
        name: value.tool_schema_dialect.tools_max_count
        for name, value in vars(profiles).items()
        if isinstance(value, OpenAIChatProfile)
    }
    assert declared
    assert set(declared.values()) == {None}


# --------------------------------------------------------------------------
# The same rung on chatgpt_oauth, whose ladder is its own
# --------------------------------------------------------------------------


def _chatgpt_provider(*, limiter: Any = None) -> ChatGPTOAuthProvider:
    provider = ChatGPTOAuthProvider(
        ProviderConfig(
            api_key="test_token",
            base_url=CHATGPT_OAUTH_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
            max_concurrency=5,
        ),
        rate_limiter=limiter or passthrough_rate_limiter(),
    )
    return provider


def _success() -> MagicMock:
    async def _raw_stream():
        yield b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
        yield b'data: {"type":"response.completed","response":{}}\n\n'

    response = MagicMock(status_code=200)
    response.aiter_raw = _raw_stream
    response.aclose = AsyncMock()
    return response


async def _drain(provider: ChatGPTOAuthProvider, request: MessagesRequest) -> list[str]:
    return [
        chunk
        async for chunk in provider.stream_response(
            request, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
        )
    ]


@pytest.mark.asyncio
async def test_chatgpt_oauth_answers_the_count_refusal_on_its_own_ladder() -> None:
    provider = _chatgpt_provider(limiter=ImmediateRetryProviderRateLimiter())
    trace = install_wire_trace()
    install_ladder_trace()
    provider._send_stream_request = AsyncMock(
        side_effect=[_error(COUNT_REFUSAL), _success()]
    )

    chunks = await _drain(provider, _request(used=(205,), forced=210))

    assert any("ok" in chunk for chunk in chunks)
    calls = provider._send_stream_request.await_args_list
    assert len(calls) == 2
    assert len(calls[0].kwargs["body"]["tools"]) == CATALOGUE_SIZE
    kept = _names(calls[1].kwargs["body"])
    assert len(kept) == STATED_MAXIMUM
    assert kept[-2:] == [_name(205), _name(210)]
    assert "(stated by this host)" in _wire_params(trace)[TOOLS_TRIMMED]
    ladder = current_ladder()
    assert ladder is not None
    rows = ladder_payload(ladder.slot())["tries"]
    assert rows[-1]["recovery"] == RUNG_TOOLS_COUNT

    facts = learned_store.learned_fact_store().facts_for_provider("chatgpt_oauth")
    assert [(f.fact_kind, f.value) for f in facts] == [
        (FACT_RESPONSES_TOOLS_MAX_COUNT, STATED_MAXIMUM)
    ]


@pytest.mark.asyncio
async def test_chatgpt_oauths_second_request_is_cut_before_the_first_send() -> None:
    first = _chatgpt_provider()
    first._send_stream_request = AsyncMock(
        side_effect=[_error(COUNT_REFUSAL), _success()]
    )
    await _drain(first, _request())

    second = _chatgpt_provider()
    trace = install_wire_trace()
    second._send_stream_request = AsyncMock(side_effect=[_success()])
    await _drain(second, _request())

    calls = second._send_stream_request.await_args_list
    assert len(calls) == 1
    assert len(calls[0].kwargs["body"]["tools"]) == STATED_MAXIMUM
    assert "(learned from this host)" in _wire_params(trace)[TOOLS_TRIMMED]


@pytest.mark.asyncio
async def test_chatgpt_oauth_keeps_its_fall_through_when_the_cut_does_not_help() -> (
    None
):
    provider = _chatgpt_provider()
    provider._send_stream_request = AsyncMock(
        side_effect=[_error(COUNT_REFUSAL), _error(COUNT_REFUSAL)]
    )

    with pytest.raises(ExecutionFailure) as caught:
        await _drain(provider, _request())

    assert len(provider._send_stream_request.await_args_list) == 2
    assert caught.value.kind.value == "model_rejected"
    assert learned_store.learned_fact_store().facts_for_provider("chatgpt_oauth") == ()


@pytest.mark.asyncio
async def test_chatgpt_oauth_with_no_cut_possible_raises_the_original_400() -> None:
    provider = _chatgpt_provider()
    provider._send_stream_request = AsyncMock(side_effect=[_error(_refusal(2, 5))])

    with pytest.raises(ExecutionFailure) as caught:
        await _drain(provider, _request(count=5, used=(0, 1), forced=4))

    assert len(provider._send_stream_request.await_args_list) == 1
    assert caught.value.kind.value == "model_rejected"


@pytest.mark.asyncio
async def test_a_chatgpt_oauth_body_with_no_cap_is_byte_identical() -> None:
    """212 tools, nothing learned: the very body the converter produced."""

    request = _request()
    baseline = build_chatgpt_oauth_request_body(
        request, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
    )

    provider = _chatgpt_provider()
    provider._send_stream_request = AsyncMock(side_effect=[_success()])
    await _drain(provider, request)

    sent = provider._send_stream_request.await_args_list[0].kwargs["body"]
    assert _wire(sent) == _wire(baseline)
    assert len(sent["tools"]) == CATALOGUE_SIZE


# --------------------------------------------------------------------------
# The Models page: a label, a row on every model, and Forget
# --------------------------------------------------------------------------


def test_the_count_is_labelled_and_drawn_on_every_model_of_the_host() -> None:
    from my_claude_code.api.model_admin import facts_for_row, learned_payload

    fact = {
        "fact_kind": FACT_RESPONSES_TOOLS_MAX_COUNT,
        "value": STATED_MAXIMUM,
        "model_id": PROVIDER_WIDE_MODEL_ID,
        "source": "rejection",
    }
    learned = {f"{PROVIDER}/*": [fact]}
    for model in ("gpt-5.6-sol", "gpt-5.6-luna"):
        rows = facts_for_row(learned, PROVIDER, model)
        assert [row["fact_kind"] for row in rows] == [FACT_RESPONSES_TOOLS_MAX_COUNT]
    rendered = learned_payload(fact)
    assert rendered["fact_label"] == "tools-count limit"
    assert rendered["model_id"] == PROVIDER_WIDE_MODEL_ID
