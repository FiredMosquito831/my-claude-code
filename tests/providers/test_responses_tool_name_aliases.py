"""Tool names past 64 characters on the Responses surface -- only where refused.

The defect: OpenCode Zen serves ``muse-spark-1.3/1.2-contributor-free`` only on
the Responses API, and refuses a tool name longer than 64 characters with
"``name`` must be at most 64 characters, got 68". Claude Code's MCP tools run to
76 (``mcp__plugin_chrome-devtools-mcp_chrome-devtools__...``), and the shared
Responses builder sent them raw: 1,040 failures on 2026-09-16 between 00:08 and
02:09 UTC. The Chat Completions funnel had aliased such names all along.

What these tests hold, lettered as the decision record letters them:

(a) turn N and N+1 share a byte-identical ``tools`` array and input prefix, so
    the vendor's prompt cache still hits;
(b) a tool appearing mid-session never changes an existing alias;
(c) ``chatgpt_oauth`` -- whose backend accepts long names -- sends exactly the
    bytes it sent before this fix (golden captured at 4bad9787);
(d) every name the OpenCode Responses body carries is a legal wire name;
(e) a streamed call to an alias reaches Claude Code under its original name;
(f) a forced ``tool_choice`` names the alias in the Responses spelling, and the
    forced call round-trips;
(g) names that were already legal are untouched on the gated path;
(h) the Chat Completions body is unchanged (golden captured at 4bad9787).
"""

import json
import random
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.openai_tool_names import (
    MIN_TOOL_NAME_MAX_LENGTH,
    OpenAIToolNameCodec,
)
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth.conversion import (
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
)
from my_claude_code.providers.openai_chat.request_policy import (
    build_openai_chat_request_body,
)
from my_claude_code.providers.openai_responses import (
    ResponsesStreamConverter,
    build_responses_request_body,
    responses_tool_call_to_anthropic,
    responses_tool_name_codec,
)
from tests.providers.support import passthrough_rate_limiter

HERE = Path(__file__).parent
WIRE_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
CONSOLE = "mcp__plugin_chrome-devtools-mcp_chrome-devtools__get_console_message"
INSIGHT = "mcp__plugin_chrome-devtools-mcp_chrome-devtools__performance_analyze_insight"
LIST_CONSOLE = "mcp__plugin_chrome-devtools-mcp_chrome-devtools__list_console_messages"
REASONING = ReasoningPolicy.on()


def _fixture() -> dict[str, Any]:
    data = json.loads(
        (HERE / "responses_long_tool_names_request.json").read_text(encoding="utf-8")
    )
    data.pop("_about")
    return data


def _wire(body: dict[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


def _golden(name: str) -> str:
    return (HERE / name).read_text(encoding="utf-8").rstrip("\n")


def _gated(data: dict[str, Any]) -> dict[str, Any]:
    return build_responses_request_body(
        MessagesRequest.model_validate(data),
        reasoning=REASONING,
        tool_name_max_length=64,
    )


def _tool_names(body: dict[str, Any]) -> dict[str, str]:
    """description -> wire name, so a tool is followed without its name."""
    return {tool["description"]: tool["name"] for tool in body["tools"]}


def _opencode_provider(name: str = "opencode") -> Any:
    return create_openai_chat_provider(
        name,
        ProviderConfig(api_key="sk-test", base_url="https://opencode.ai/zen/v1"),
        passthrough_rate_limiter(),
        profile=OPENAI_CHAT_PROFILES[name],
    )


def _turns() -> tuple[dict[str, Any], dict[str, Any]]:
    turn_two = _fixture()
    turn_one = {**turn_two, "messages": turn_two["messages"][:1]}
    return turn_one, turn_two


# -- the declaration ----------------------------------------------------------


def test_only_the_two_opencode_profiles_declare_a_responses_limit() -> None:
    declared = {
        name: profile.responses_tool_name_max_length
        for name, profile in OPENAI_CHAT_PROFILES.items()
        if profile.responses_tool_name_max_length is not None
    }
    assert declared == {"opencode": 64, "opencode_go": 64}


@pytest.mark.parametrize("name", ["opencode", "opencode_go"])
def test_the_opencode_responses_transport_carries_the_declared_limit(name) -> None:
    assert _opencode_provider(name)._responses.tool_name_max_length == 64


def test_a_limit_the_codec_does_not_implement_is_refused() -> None:
    """A ceiling too small to build an alias under is refused, not approximated.

    Until 7.23.0 this read ``responses_tool_name_codec(request, 128)`` raises,
    because the codec implemented exactly one limit. It implements any limit
    from :data:`MIN_TOOL_NAME_MAX_LENGTH` upward now -- a host may *state* one
    in a rejection rather than a profile declaring it -- so the assertion that
    still holds is the one at the bottom of the range: below the floor an
    alias stops naming the tool, and inventing one would trade a visible
    failure for a call the model cannot make.
    """

    request = MessagesRequest.model_validate(_fixture())
    assert responses_tool_name_codec(request, None) is None
    assert responses_tool_name_codec(request, 128) is not None
    with pytest.raises(ValueError, match=str(MIN_TOOL_NAME_MAX_LENGTH)):
        responses_tool_name_codec(request, MIN_TOOL_NAME_MAX_LENGTH - 1)


# -- (a) prompt cache ---------------------------------------------------------


def test_a_turn_two_body_shares_tools_and_input_prefix_with_turn_one() -> None:
    turn_one_data, turn_two_data = _turns()
    one = _gated(turn_one_data)
    two = _gated(turn_two_data)

    assert _wire({"tools": one["tools"]}) == _wire({"tools": two["tools"]})
    assert one["tool_choice"] == two["tool_choice"]
    prefix = len(one["input"])
    assert _wire({"i": two["input"][:prefix]}) == _wire({"i": one["input"]})
    # The fixed key order puts model then input first, so the serialised body
    # itself shares the turn-one prefix up to the end of its input items.
    head = _wire({"model": one["model"], "input": one["input"]})[:-2]
    assert _wire(two).startswith(head)
    # And the replayed call names the same alias the tool list declares.
    call = next(item for item in two["input"] if item["type"] == "function_call")
    assert call["name"] == _tool_names(two)["Get one console message"]


def test_a_replayed_history_re_encodes_to_identical_bytes_every_time() -> None:
    _, turn_two = _turns()
    assert _wire(_gated(turn_two)) == _wire(_gated(turn_two))


# -- (b) aliases are persistent -----------------------------------------------


def test_b_adding_or_reordering_tools_leaves_existing_aliases_unchanged() -> None:
    data = _fixture()
    before = _tool_names(_gated(data))

    grown = _fixture()
    grown["tools"] = [
        {
            "name": "mcp__plugin_chrome-devtools-mcp_chrome-devtools__take_heapsnapshot",
            "description": "New long tool",
            "input_schema": {"type": "object", "properties": {}},
        },
        *reversed(grown["tools"]),
        {
            "name": "Glob",
            "description": "New short tool",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]
    after = _tool_names(_gated(grown))

    for description, alias in before.items():
        assert after[description] == alias


def test_b_an_alias_is_a_pure_function_of_its_name() -> None:
    """The collision loop can only move an alias on an exact 64-bit clash.

    ``_unique_alias`` derives attempt 0 from the name alone and advances only
    when that string is already reserved -- by a portable name spelled exactly
    like it, or by another alias with the same 47-character head *and* the same
    16 hex digits of sha256. So the alias a name gets alone is the alias it gets
    in any company; this checks it over a few thousand realistic neighbours.
    """
    rng = random.Random(7181)
    stems = ["mcp__plugin_chrome-devtools-mcp_chrome-devtools__", "mcp__x.y__"]
    names = [
        rng.choice(stems) + "".join(rng.choices("abcdefgh_", k=rng.randint(10, 40)))
        for _ in range(3000)
    ]
    together = OpenAIToolNameCodec.from_names(names)
    for name in names:
        assert together.encode(name) == OpenAIToolNameCodec.from_names([name]).encode(
            name
        )


# -- (c) chatgpt_oauth unchanged ----------------------------------------------


def test_c_chatgpt_oauth_body_is_byte_identical_to_the_pre_fix_golden() -> None:
    data = _fixture()
    data["model"] = "gpt-5.5"
    body = build_chatgpt_oauth_request_body(
        MessagesRequest.model_validate(data), reasoning=REASONING
    )
    wire = _wire(body)
    assert wire == _golden("chatgpt_oauth_long_tool_names_golden.json")
    assert CONSOLE in wire and INSIGHT in wire  # 68 and 76 characters, raw


def test_c_the_ungated_builder_decodes_nothing() -> None:
    item = {"type": "function_call", "id": "fc_1", "name": CONSOLE, "arguments": "{}"}
    assert responses_tool_call_to_anthropic(item)["name"] == CONSOLE


# -- (d) opencode Responses body ----------------------------------------------


def _json_names(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            if key == "name" and isinstance(inner, str):
                found.append(inner)
            else:
                found.extend(_json_names(inner))
    elif isinstance(value, list):
        for inner in value:
            found.extend(_json_names(inner))
    return found


@pytest.mark.parametrize("provider_name", ["opencode", "opencode_go"])
def test_d_every_wire_name_in_the_opencode_responses_body_is_legal(
    provider_name,
) -> None:
    provider = _opencode_provider(provider_name)
    body, _ = provider._responses.build_body(
        MessagesRequest.model_validate(_fixture()),
        reasoning=REASONING,
        max_output_tokens=512,
    )
    names = _json_names(
        {"tools": body["tools"], "input": body["input"], "c": body["tool_choice"]}
    )
    assert len(names) == 6  # four tools, one replayed call, one forced choice
    for name in names:
        assert WIRE_NAME.fullmatch(name), name
    assert CONSOLE not in _wire(body) and INSIGHT not in _wire(body)


# -- (e) and (f) the round trip -----------------------------------------------


def _sse(frames: list[dict[str, Any]]) -> bytes:
    return b"".join(f"data: {json.dumps(f)}\n\n".encode() for f in frames)


def _call_frames(alias: str) -> list[dict[str, Any]]:
    item = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": alias,
        "arguments": "",
        "status": "in_progress",
    }
    done = {**item, "arguments": '{"msgid": 2}', "status": "completed"}
    return [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "output_index": 0,
            "delta": '{"msgid": 2}',
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "fc_1",
            "output_index": 0,
            "name": alias,
            "arguments": '{"msgid": 2}',
        },
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "output": [done],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        },
    ]


def _tool_use_names(events: list[str]) -> list[str]:
    names = []
    for event in events:
        for line in event.splitlines():
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[len("data: ") :])
            block = payload.get("content_block") or {}
            if block.get("type") == "tool_use":
                names.append(block["name"])
    return names


@pytest.mark.asyncio
async def test_e_f_a_streamed_call_to_an_alias_reaches_the_client_as_the_original():
    provider = _opencode_provider()
    transport = provider._responses
    request = MessagesRequest.model_validate(_fixture())
    body, headers = transport.build_body(
        request, reasoning=REASONING, max_output_tokens=512
    )
    alias = body["tool_choice"]["name"]
    assert alias != CONSOLE and WIRE_NAME.fullmatch(alias)

    sent: list[dict[str, Any]] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(http_request.content))

        async def _body():
            yield _sse(_call_frames(alias))

        return httpx.Response(
            200,
            content=_body(),
            headers={"content-type": "text/event-stream"},
        )

    await transport._client.aclose()
    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    events = [
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
    await transport.aclose()

    assert sent and sent[0]["tool_choice"] == {"type": "function", "name": alias}
    assert _tool_use_names(events) == [CONSOLE]
    assert alias not in "".join(events)


def test_e_the_non_streaming_conversion_decodes_with_the_request_codec() -> None:
    request = MessagesRequest.model_validate(_fixture())
    codec = responses_tool_name_codec(request, 64)
    assert codec is not None
    for original in (CONSOLE, INSIGHT, LIST_CONSOLE, "Read"):
        item = {
            "type": "function_call",
            "id": "fc_9",
            "name": codec.encode(original),
            "arguments": "{}",
        }
        block = responses_tool_call_to_anthropic(item, tool_names=codec)
        assert block["name"] == original


def test_e_the_converter_decodes_the_one_frame_that_opens_a_tool_block() -> None:
    request = MessagesRequest.model_validate(_fixture())
    codec = responses_tool_name_codec(request, 64)
    ledger = AnthropicStreamLedger("msg_t", "m", input_tokens=0)
    converter = ResponsesStreamConverter(ledger, tool_names=codec)
    assert codec is not None
    events = [ledger.message_start()]
    for frame in _call_frames(codec.encode(INSIGHT)):
        events.extend(converter.feed(frame))
    events.extend(converter.finish())
    assert _tool_use_names(events) == [INSIGHT]


def test_f_forced_tool_choice_uses_the_responses_spelling_on_the_gated_path() -> None:
    body = _gated(_fixture())
    alias = _tool_names(body)["Get one console message"]
    assert body["tool_choice"] == {"type": "function", "name": alias}


# -- (g) legal names untouched ------------------------------------------------


def test_g_a_request_of_legal_names_is_byte_identical_gated_or_not() -> None:
    exactly_64 = "mcp__plugin_x__" + "a" * 49
    assert len(exactly_64) == 64
    data = _fixture()
    data["tools"] = [
        {**data["tools"][0]},
        {**data["tools"][1], "name": exactly_64},
    ]
    data["messages"][1]["content"][1]["name"] = exactly_64
    data["tool_choice"] = {"type": "auto"}
    request = MessagesRequest.model_validate(data)

    gated = build_responses_request_body(
        request, reasoning=REASONING, tool_name_max_length=64
    )
    plain = build_responses_request_body(request, reasoning=REASONING)
    assert _wire(gated) == _wire(plain)
    assert [tool["name"] for tool in gated["tools"]] == ["Read", exactly_64]


def test_g_short_names_beside_long_ones_are_untouched() -> None:
    names = _tool_names(_gated(_fixture()))
    assert names["Read a file"] == "Read"


# -- (h) Chat Completions unchanged -------------------------------------------


def test_h_opencode_chat_completions_body_is_byte_identical_to_the_golden() -> None:
    profile = OPENAI_CHAT_PROFILES["opencode"]
    body = build_openai_chat_request_body(
        MessagesRequest.model_validate(_fixture()),
        reasoning=REASONING,
        policy=profile.request_policy,
        postprocessors=profile.request_postprocessors,
    )
    assert _wire(body) == _golden("opencode_chat_long_tool_names_golden.json")


def test_h_one_tool_has_one_alias_on_both_surfaces() -> None:
    profile = OPENAI_CHAT_PROFILES["opencode"]
    chat = build_openai_chat_request_body(
        MessagesRequest.model_validate(_fixture()),
        reasoning=REASONING,
        policy=profile.request_policy,
        postprocessors=profile.request_postprocessors,
    )
    responses = _gated(_fixture())
    assert [t["function"]["name"] for t in chat["tools"]] == [
        t["name"] for t in responses["tools"]
    ]
