"""ChatGPT OAuth's tool-name ceiling is the one the backend states, never a guess (7.59.0).

The backend declares nothing and accepts long names -- 76-character names went
through 300 of 300 times in 7.18.1's probe -- but it does refuse some. The
request log holds 42 refusals from 2026-08-23, all this one sentence about a
replayed call name::

    {"error": {"message": "Invalid 'input[62].name': string too long.
     Expected a string with maximum length 128, but got a string with length
     252 instead.", "type": "invalid_request_error",
     "param": "input[62].name", "code": "string_above_max_length"}}

``chatgpt_oauth`` has its own recovery ladder, and until 7.59.0 it had no
name-length rung, so every such request failed. The user's rule
(2026-09-27 04:03) sets what the fix may do. If the host states no limit, MCC
sets none. If the host refuses a name, MCC learns the host's own number,
never a blanket 64, and that 64 stays on Zen's Responses transport. What
these tests hold:

* the host's own wording is read for its number: "maximum length 128" and
  "at most 128 characters" both give 128, and a wording with no number gives
  nothing on this backend;
* on a fake ChatGPT-shaped upstream the sequence is: cold, 2 calls (the
  refusal, then the same request aliased at 128); warm, 1 call; after a
  restart, 1 call; after *Forget*, 2 calls again;
* the model's call to an aliased tool comes back under the client's own name;
* with nothing learned, the body is the one 7.58.x built, byte for byte:
  long names go out as they always did.
"""

import json
from pathlib import Path
from typing import Any

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
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth import ChatGPTOAuthProvider
from my_claude_code.providers.chatgpt_oauth.conversion import (
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.chatgpt_oauth.provider import (
    CHATGPT_OAUTH_DEFAULT_BASE,
    CHATGPT_OAUTH_PROVIDER_ID,
)
from my_claude_code.providers.recovery import (
    FACT_RESPONSES_TOOL_NAME_MAX_LENGTH,
    PROVIDER_WIDE_MODEL_ID,
    RUNG_TOOL_NAME_LENGTH,
    RUNG_TOOL_SCHEMA,
    RUNG_TOOLS_COUNT,
    LearnedFactStore,
    refuses_tool_name_length_unstated,
    rejected_tool_name_max_length,
    stated_tool_name_max_length,
)
from my_claude_code.providers.recovery import store as learned_store
from tests.providers.support import ImmediateRetryProviderRateLimiter

REASONING = ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
STATED = 128
#: A real-shaped MCP name past the ceiling (the 08-23 names were 252 and 257).
LONG_TOOL = "mcp__plugin_" + "chrome_devtools_mcp_" * 12 + "take_snapshot"
#: A name the backend has always accepted (7.18.1 measured 76).
MEDIUM_TOOL = "mcp__plugin_chrome-devtools-mcp_chrome-devtools__performance_insight"


def _refusal(message: str, param: str) -> dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": "invalid_request_error",
            "param": param,
            "code": "string_above_max_length",
        }
    }


#: The stored text of the 42 refusals of 2026-08-23, verbatim.
REAL_WORDING = (
    "Invalid 'input[{index}].name': string too long. Expected a string with "
    "maximum length 128, but got a string with length {got} instead."
)
AT_MOST_WORDING = (
    "Invalid 'input[{index}].name': string too long. Expected a string with "
    "at most 128 characters, but got {got}."
)
UNSTATED_WORDING = "Invalid 'input[{index}].name': string too long."


def _error(payload: dict[str, Any]) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://upstream.invalid/codex/responses")
    response = httpx.Response(400, request=request, json=payload)
    return httpx.HTTPStatusError(
        "ChatGPT OAuth API error 400", request=request, response=response
    )


# -- the matcher --------------------------------------------------------------------


def test_the_logged_wording_states_128() -> None:
    error = _error(_refusal(REAL_WORDING.format(index=62, got=252), "input[62].name"))
    assert stated_tool_name_max_length(error) == STATED
    # The Zen transport's reader reads the host's number too now; it used to
    # fall back to 64 on this sentence (no "of" after "maximum length").
    assert rejected_tool_name_max_length(error) == STATED
    assert not refuses_tool_name_length_unstated(error)


def test_at_most_128_characters_states_128() -> None:
    error = _error(
        _refusal(AT_MOST_WORDING.format(index=73, got=257), "input[73].name")
    )
    assert stated_tool_name_max_length(error) == STATED


def test_a_length_refusal_with_no_number_states_nothing_here() -> None:
    error = _error(_refusal(UNSTATED_WORDING.format(index=62), "input[62].name"))
    assert stated_tool_name_max_length(error) is None
    assert refuses_tool_name_length_unstated(error)
    # The Zen transport keeps its declared-host fallback, unchanged.
    assert rejected_tool_name_max_length(error) == 64


@pytest.mark.parametrize(
    "payload",
    [
        {
            "error": {
                "message": (
                    "Invalid 'tools': array too long. Expected an array with "
                    "maximum length 128, but got an array with length 212 instead."
                ),
                "param": "tools",
                "code": "array_above_max_length",
            }
        },
        _refusal(
            "Invalid 'input[3].name': string too long. at most 16 characters", "x"
        ),
        {"error": {"message": "Unknown parameter: 'foo'.", "param": "foo"}},
    ],
    ids=["tools-count", "below-the-alias-floor", "unrelated"],
)
def test_other_refusals_state_no_name_ceiling(payload: dict[str, Any]) -> None:
    error = _error(payload)
    assert stated_tool_name_max_length(error) is None
    assert (
        not refuses_tool_name_length_unstated(error)
        or payload["error"].get("param") == "x"
    )


# -- a fake ChatGPT-shaped upstream -------------------------------------------------


def _request() -> MessagesRequest:
    """Claude Code's shape: tools, and a history that replays the long call."""

    names = ["Read", "Bash", MEDIUM_TOOL, LONG_TOOL]
    return MessagesRequest.model_validate(
        {
            "model": "gpt-5.6-luna",
            "max_tokens": 256,
            "stream": True,
            "system": "You are Claude Code.",
            "messages": [
                {"role": "user", "content": "take a snapshot"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_01",
                            "name": LONG_TOOL,
                            "input": {"page": 1},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_01",
                            "content": "ok",
                        }
                    ],
                },
            ],
            "tools": [
                {
                    "name": name,
                    "description": f"the {name} tool",
                    "input_schema": {
                        "type": "object",
                        "properties": {"page": {"type": "integer"}},
                    },
                }
                for name in names
            ],
        }
    )


def _body_names(body: dict[str, Any]) -> list[tuple[str, str]]:
    names = [(f"tools[{i}].name", tool["name"]) for i, tool in enumerate(body["tools"])]
    names.extend(
        (f"input[{i}].name", item["name"])
        for i, item in enumerate(body["input"])
        if isinstance(item, dict) and item.get("type") == "function_call"
    )
    return names


def _sse_response(name: str) -> httpx.Response:
    """A streamed 200 whose model calls ``name``.

    An async generator body, as the other Responses tests use: a bytes body is
    read at construction and could not be streamed again.
    """

    payload = _stream_calling(name)

    async def _body() -> Any:
        yield payload

    return httpx.Response(
        200, content=_body(), headers={"content-type": "text/event-stream"}
    )


def _stream_calling(name: str) -> bytes:
    frames = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": name,
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "item_id": "fc_1",
            "delta": '{"page": 2}',
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": name,
                "arguments": '{"page": 2}',
            },
        },
        {"type": "response.completed", "response": {"status": "completed"}},
    ]
    return "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames).encode()


class FakeChatGPT:
    """The backend's one rule for names, in the wording it is given."""

    def __init__(self, wording: str = REAL_WORDING) -> None:
        self.wording = wording
        self.sent: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.sent.append(body)
        for where, name in _body_names(body):
            if len(name) > STATED:
                index = where.split("[")[1].split("]")[0]
                return httpx.Response(
                    400,
                    json=_refusal(
                        self.wording.format(index=index, got=len(name)),
                        where,
                    ),
                )
        # The model calls the long tool by whatever name it was offered.
        offered = body["tools"][3]["name"]
        return _sse_response(offered)


def _provider(upstream: FakeChatGPT) -> ChatGPTOAuthProvider:
    provider = ChatGPTOAuthProvider(
        ProviderConfig(
            api_key="test_token",
            base_url=CHATGPT_OAUTH_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
            max_concurrency=5,
        ),
        # The real retry policy without wall-clock backoff: it is what
        # records the ladder rows the modal draws.
        rate_limiter=ImmediateRetryProviderRateLimiter(),
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    return provider


async def _drain(provider: ChatGPTOAuthProvider) -> str:
    chunks = [
        chunk
        async for chunk in provider.stream_response(_request(), reasoning=REASONING)
    ]
    await provider._client.aclose()
    return "".join(chunks)


def _learned(store: LearnedFactStore) -> list[tuple[str, Any]]:
    return [
        (fact.fact_kind, fact.value)
        for fact in store.facts_for_provider(CHATGPT_OAUTH_PROVIDER_ID)
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("wording", [REAL_WORDING, AT_MOST_WORDING])
async def test_cold_warm_restart_forget(wording: str, tmp_path: Path) -> None:
    path = tmp_path / "learned_facts.json"
    store = LearnedFactStore(path=path)
    learned_store.set_learned_fact_store(store)

    # Cold: the refusal, then the same request with names aliased at 128.
    cold = FakeChatGPT(wording)
    install_ladder_trace()
    events = await _drain(_provider(cold))
    assert len(cold.sent) == 2
    assert max(len(name) for _where, name in _body_names(cold.sent[0])) > STATED
    assert all(len(name) <= STATED for _where, name in _body_names(cold.sent[1]))
    assert f'"name":"{LONG_TOOL}"' in events.replace(" ", "")
    ladder = current_ladder()
    assert ladder is not None
    assert ladder_payload(ladder.slot())["tries"][-1]["recovery"] == (
        RUNG_TOOL_NAME_LENGTH
    )
    assert _learned(store) == [(FACT_RESPONSES_TOOL_NAME_MAX_LENGTH, STATED)]
    facts = store.facts_for_provider(CHATGPT_OAUTH_PROVIDER_ID)
    assert "128" in facts[0].evidence

    # Warm: aliased before the first send, the same bytes the retry carried.
    warm = FakeChatGPT(wording)
    events = await _drain(_provider(warm))
    assert len(warm.sent) == 1
    assert json.dumps(warm.sent[0]) == json.dumps(cold.sent[1])
    assert f'"name":"{LONG_TOOL}"' in events.replace(" ", "")

    # A restart: a new store read back from disk, a new provider.
    assert store.flush()
    restarted = LearnedFactStore()
    restarted.enable_persistence(path)
    learned_store.set_learned_fact_store(restarted)
    after_restart = FakeChatGPT(wording)
    await _drain(_provider(after_restart))
    assert len(after_restart.sent) == 1
    assert json.dumps(after_restart.sent[0]) == json.dumps(cold.sent[1])

    # Forget, as the Models page does: the next request pays the 400 again.
    assert restarted.forget(
        CHATGPT_OAUTH_PROVIDER_ID,
        PROVIDER_WIDE_MODEL_ID,
        FACT_RESPONSES_TOOL_NAME_MAX_LENGTH,
    )
    forgotten = FakeChatGPT(wording)
    await _drain(_provider(forgotten))
    assert len(forgotten.sent) == 2


@pytest.mark.asyncio
async def test_a_refusal_that_states_no_number_learns_nothing() -> None:
    store = LearnedFactStore()
    learned_store.set_learned_fact_store(store)
    upstream = FakeChatGPT(UNSTATED_WORDING)

    with pytest.raises(ExecutionFailure) as caught:
        await _drain(_provider(upstream))

    assert len(upstream.sent) == 1
    assert caught.value.kind.value == "model_rejected"
    assert _learned(store) == []


@pytest.mark.asyncio
async def test_nothing_learned_sends_the_7_58_body_byte_for_byte() -> None:
    """No fact: the first body is the one the builder always made, long names and all."""

    learned_store.set_learned_fact_store(LearnedFactStore())
    upstream = FakeChatGPT()
    with pytest.raises(ExecutionFailure):
        # A single-shot upstream that refuses: only the first body matters.
        await _drain(_provider(_refuse_everything(upstream)))

    baseline = build_chatgpt_oauth_request_body(_request(), reasoning=REASONING)
    assert json.dumps(upstream.sent[0]) == json.dumps(baseline)
    assert LONG_TOOL in [name for _where, name in _body_names(upstream.sent[0])]


def _refuse_everything(upstream: FakeChatGPT) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        upstream.sent.append(json.loads(request.content))
        return httpx.Response(
            400, json={"error": {"message": "Unknown parameter: 'x'.", "param": "x"}}
        )

    return handler


@pytest.mark.asyncio
async def test_a_short_catalogue_never_touches_the_rung() -> None:
    """Names the backend accepts: one call, no alias, nothing learned."""

    store = LearnedFactStore()
    learned_store.set_learned_fact_store(store)
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return _sse_response("Read")

    provider = _provider(FakeChatGPT())
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    request = MessagesRequest.model_validate(
        {
            "model": "gpt-5.6-luna",
            "max_tokens": 64,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {"name": MEDIUM_TOOL, "input_schema": {"type": "object"}},
                {"name": "Read", "input_schema": {"type": "object"}},
            ],
        }
    )
    async for _chunk in provider.stream_response(request, reasoning=REASONING):
        pass
    await provider._client.aclose()
    assert len(sent) == 1
    assert [tool["name"] for tool in sent[0]["tools"]] == [MEDIUM_TOOL, "Read"]
    assert _learned(store) == []


@pytest.mark.asyncio
async def test_a_smaller_ceiling_after_a_learned_one_is_not_re_aliased() -> None:
    """Re-aliasing an alias would decode the wrong name: the rung stands aside."""

    store = LearnedFactStore()
    learned_store.set_learned_fact_store(store)
    store.memory_for(CHATGPT_OAUTH_PROVIDER_ID).learn_responses_tool_name_limit(
        STATED, evidence="maximum length 128"
    )
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(
            400,
            json=_refusal(
                "Invalid 'tools[3].name': string too long. Expected a string "
                "with maximum length 100, but got a string with length 128 instead.",
                "tools[3].name",
            ),
        )

    provider = _provider(FakeChatGPT())
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(ExecutionFailure):
        async for _chunk in provider.stream_response(_request(), reasoning=REASONING):
            pass
    await provider._client.aclose()
    assert len(sent) == 1
    assert _learned(store) == [(FACT_RESPONSES_TOOL_NAME_MAX_LENGTH, STATED)]


def test_the_ladder_orders_the_name_rung_after_schema_and_count() -> None:
    provider = _provider(FakeChatGPT())
    ladder = provider._recovery_ladder_for(_request(), None)
    assert [rung.kind for rung in ladder.rungs][:3] == [
        RUNG_TOOL_SCHEMA,
        RUNG_TOOLS_COUNT,
        RUNG_TOOL_NAME_LENGTH,
    ]
    assert len(ladder.rungs) == 4


def test_the_models_page_draws_the_learned_ceiling_on_every_chatgpt_row() -> None:
    from my_claude_code.api.model_admin import facts_for_row, learned_payload

    fact = {
        "fact_kind": FACT_RESPONSES_TOOL_NAME_MAX_LENGTH,
        "value": STATED,
        "model_id": PROVIDER_WIDE_MODEL_ID,
        "source": "rejection",
    }
    learned = {f"{CHATGPT_OAUTH_PROVIDER_ID}/*": [fact]}
    for model in ("gpt-5.6-luna", "gpt-5.6-terra"):
        rows = facts_for_row(learned, CHATGPT_OAUTH_PROVIDER_ID, model)
        assert [row["fact_kind"] for row in rows] == [
            FACT_RESPONSES_TOOL_NAME_MAX_LENGTH
        ]
    assert learned_payload(fact)["fact_label"] == "tool-name limit"
