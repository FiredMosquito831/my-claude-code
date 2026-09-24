"""Every Responses host sweeps what its validator refuses before the first send.

7.36.0 made a schema refusal cost one 400 per provider: the rung sweeps the
catalogue, retries once, and remembers. 7.38.0 makes the construct everybody
has already met cost **zero**: a declared dialect, run unconditionally at the
one seam every Responses tool definition is built at, drops a ``pattern`` that
carries lookaround before anything is sent.

What these tests hold, in the order the brief states them:

* the vocabulary is data -- a default Responses dialect, overridable by a
  profile's declaration, and nothing branches on a host's name;
* only the offending construct leaves; every other keyword, property, ``type``
  and ``description`` stays, and the client's own dicts are never mutated;
* a catalogue with nothing offending is returned **by identity** and its bytes
  are exactly what they were before this release;
* the real appium ``videoScale`` catalogue costs ONE upstream call on the
  first request, on both senders;
* it composes with what the rung learned, with the 7.18.1 alias codec and with
  the 7.28.0 free-tier catalogue, in a stated order;
* what it removed is recorded under the rung's own ``tool_schema_pruned`` key,
  names and paths only;
* the tools prefix is byte-stable from one turn to the next;
* Chat Completions is not touched.
"""

import ast
import dataclasses
import inspect
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from my_claude_code.core.anthropic.conversion import AnthropicToOpenAIConverter
from my_claude_code.core.anthropic.models import Message, MessagesRequest, Tool
from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth import ChatGPTOAuthProvider
from my_claude_code.providers.chatgpt_oauth.conversion import (
    CHATGPT_OAUTH_TOOL_SCHEMA_DIALECT,
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.chatgpt_oauth.provider import CHATGPT_OAUTH_DEFAULT_BASE
from my_claude_code.providers.openai_chat import (
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
)
from my_claude_code.providers.openai_chat.responses_transport import ResponsesTransport
from my_claude_code.providers.openai_responses import (
    PERMISSIVE_TOOL_SCHEMA_DIALECT,
    RESPONSES_TOOL_SCHEMA_DIALECT,
    ToolSchemaDialect,
    build_responses_request_body,
    responses_tool_name_codec,
)
from my_claude_code.providers.openai_responses.tool_schema_dialect import (
    sweep_tool_catalogue,
)
from my_claude_code.providers.recovery import (
    FACT_RESPONSES_TOOL_SCHEMA_KEYWORD,
    PROVIDER_WIDE_MODEL_ID,
    REGEX_CONSTRUCTS,
    LearnedFactStore,
    SchemaKeywordRefusal,
)
from tests.providers.support import passthrough_rate_limiter

REASONING = ReasoningPolicy.on()
PROVIDER = "muse_gateway"
SRC = Path(__file__).resolve().parents[2] / "src" / "my_claude_code"

#: The real property, verbatim from the recovered appium-mcp catalogue.
VIDEO_SCALE_PATTERN = r"^(?:[1-9]\d{0,4}|-[12]):(?:[1-9]\d{0,4}|-[12])(?![\s\S])"
VIDEO_SCALE_ON_THE_WIRE = json.dumps(VIDEO_SCALE_PATTERN)[1:-1]
OFFENDING_TOOL = "mcp__appium-mcp__appium_screen_recording"
#: The two other patterns in the same 205-tool catalogue, both accepted.
INNOCENT_PATTERNS = (r"^[^\n\r]*$", r"^[\s\S]{0,300}$")

RESPONSES_REFUSAL = {
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


def _wire(value: Any) -> str:
    return json.dumps(value)


def _video_scale_schema() -> dict[str, Any]:
    """The offending tool's schema, as the client sends it."""

    return {
        "type": "object",
        "$schema": "http://json-schema.org/draft-07/schema#",
        "properties": {
            "action": {"type": "string", "enum": ["start", "stop"]},
            "videoScale": {
                "description": (
                    "iOS only. Width:height, each 1-16384 (e.g. 1280:720); one "
                    "may be -1 or -2 to preserve aspect ratio."
                ),
                "type": "string",
                "pattern": VIDEO_SCALE_PATTERN,
            },
        },
        "required": ["action"],
    }


def _send_message_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "to": {
                "allOf": [
                    {"type": "string", "pattern": INNOCENT_PATTERNS[0]},
                    {"type": "string", "pattern": INNOCENT_PATTERNS[1]},
                ]
            }
        },
    }


def _request(
    *,
    offending: bool = True,
    turns: int = 1,
    name: str = OFFENDING_TOOL,
    model: str = "muse-spark-1.3",
) -> MessagesRequest:
    tools = [
        Tool(
            name="SendMessage", description="send", input_schema=_send_message_schema()
        )
    ]
    if offending:
        tools.append(
            Tool(
                name=name,
                description="Start or stop screen recording.",
                input_schema=_video_scale_schema(),
            )
        )
    messages = [Message(role="user", content="hi")]
    for turn in range(1, turns):
        messages.append(Message(role="assistant", content=f"answer {turn}"))
        messages.append(Message(role="user", content=f"question {turn}"))
    return MessagesRequest(model=model, max_tokens=64, messages=messages, tools=tools)


# --------------------------------------------------------------------------
# The vocabulary is data
# --------------------------------------------------------------------------


def test_the_default_dialect_is_pattern_lookaround_from_the_rungs_own_table() -> None:
    (refusal,) = RESPONSES_TOOL_SCHEMA_DIALECT.refused
    assert refusal.keyword == "pattern"
    assert refusal.construct is not None
    # The very row the rung reads a refusal against -- not a second table.
    assert any(refusal.construct is row for row in REGEX_CONSTRUCTS)
    assert refusal.detail == "pattern:lookaround"
    assert CHATGPT_OAUTH_TOOL_SCHEMA_DIALECT is RESPONSES_TOOL_SCHEMA_DIALECT
    assert PERMISSIVE_TOOL_SCHEMA_DIALECT.refused == ()


def test_every_shipped_profile_inherits_the_default_and_a_declaration_wins() -> None:
    for profile_id, profile in OPENAI_CHAT_PROFILES.items():
        assert profile.responses_tool_schema_dialect is None, profile_id
        assert profile.tool_schema_dialect is RESPONSES_TOOL_SCHEMA_DIALECT

    declared = ToolSchemaDialect(name="declared")
    profile = OPENAI_CHAT_PROFILES["opencode"]
    overridden = dataclasses.replace(profile, responses_tool_schema_dialect=declared)
    assert overridden.tool_schema_dialect is declared


def test_a_provider_hands_its_profiles_dialect_to_the_transport() -> None:
    provider = create_openai_chat_provider(
        "opencode",
        ProviderConfig(api_key="sk-test", base_url="https://opencode.ai/zen/v1"),
        passthrough_rate_limiter(),
        profile=OPENAI_CHAT_PROFILES["opencode"],
    )
    assert provider._responses._tool_schema_dialect is RESPONSES_TOOL_SCHEMA_DIALECT


# --------------------------------------------------------------------------
# Only the offence leaves, and nothing is mutated
# --------------------------------------------------------------------------


def test_only_the_lookaround_pattern_leaves_the_real_catalogue() -> None:
    request = _request()
    notes: dict[str, str] = {}
    body = build_responses_request_body(request, reasoning=REASONING, wire_notes=notes)

    send_message, recorder = body["tools"]
    video_scale = recorder["parameters"]["properties"]["videoScale"]
    assert "pattern" not in video_scale
    assert video_scale["type"] == "string"
    assert video_scale["description"].startswith("iOS only.")
    assert recorder["parameters"]["required"] == ["action"]
    assert recorder["parameters"]["$schema"].startswith("http://json-schema.org")
    # The two innocent patterns in the same catalogue survive untouched.
    assert [
        p["pattern"] for p in send_message["parameters"]["properties"]["to"]["allOf"]
    ] == list(INNOCENT_PATTERNS)
    assert VIDEO_SCALE_ON_THE_WIRE not in _wire(body)
    # The client's own schema is never mutated.
    assert request.tools is not None
    client_schema = request.tools[1].input_schema
    assert client_schema is not None
    assert client_schema["properties"]["videoScale"]["pattern"] == VIDEO_SCALE_PATTERN
    # The one tool that lost nothing is still the client's own dict.
    assert send_message["parameters"] is request.tools[0].input_schema


@pytest.mark.parametrize(
    "pattern",
    [r"^(?=.*\d).+$", r"^(?!__).+$", r"(?<=@)\w+", r"(?<!\\)\w+"],
)
def test_every_lookaround_spelling_is_dropped_wherever_it_nests(pattern: str) -> None:
    schema = {
        "type": "object",
        "properties": {
            "list": {"type": "array", "items": {"type": "string", "pattern": pattern}},
            "either": {"anyOf": [{"type": "string", "pattern": pattern}]},
            "ref": {"$ref": "#/$defs/name"},
        },
        "$defs": {"name": {"type": "string", "pattern": pattern}},
    }
    tools = [{"type": "function", "name": "t", "description": "", "parameters": schema}]
    sweep = sweep_tool_catalogue(tools, RESPONSES_TOOL_SCHEMA_DIALECT)
    assert sorted(removal.path for removal in sweep.removals) == [
        "$.$defs.name.pattern",
        "$.properties.either.anyOf[0].pattern",
        "$.properties.list.items.pattern",
    ]
    assert "pattern" not in _wire(sweep.tools)
    assert schema["$defs"]["name"]["pattern"] == pattern


def test_a_property_named_pattern_is_a_property_and_survives() -> None:
    schema = {
        "type": "object",
        "properties": {"pattern": {"type": "string", "description": "a glob"}},
    }
    tools = [
        {"type": "function", "name": "Grep", "description": "", "parameters": schema}
    ]
    sweep = sweep_tool_catalogue(tools, RESPONSES_TOOL_SCHEMA_DIALECT)
    assert sweep.tools is tools
    assert sweep.removals == ()


# --------------------------------------------------------------------------
# Identity and byte equality when nothing offends
# --------------------------------------------------------------------------


def test_a_catalogue_with_nothing_offending_comes_back_by_identity() -> None:
    request = _request(offending=False)
    notes: dict[str, str] = {}
    body = build_responses_request_body(request, reasoning=REASONING, wire_notes=notes)
    assert request.tools is not None
    assert [tool["parameters"] for tool in body["tools"]] == [
        tool.input_schema for tool in request.tools
    ]
    assert all(
        sent["parameters"] is tool.input_schema
        for sent, tool in zip(body["tools"], request.tools, strict=True)
    )
    assert notes == {}

    tools = body["tools"]
    assert sweep_tool_catalogue(tools, RESPONSES_TOOL_SCHEMA_DIALECT).tools is tools


def test_nothing_offending_is_byte_identical_to_the_permissive_build() -> None:
    """The equality contract: the sweep changes bytes only where it removes."""

    request = _request(offending=False)
    for build in (
        lambda dialect: build_responses_request_body(
            request, reasoning=REASONING, tool_schema_dialect=dialect
        ),
        lambda dialect: build_chatgpt_oauth_request_body(
            request, reasoning=REASONING, tool_schema_dialect=dialect
        ),
    ):
        assert _wire(build(RESPONSES_TOOL_SCHEMA_DIALECT)) == _wire(
            build(PERMISSIVE_TOOL_SCHEMA_DIALECT)
        )


def test_the_permissive_dialect_sends_the_lookaround_as_written() -> None:
    body = build_responses_request_body(
        _request(),
        reasoning=REASONING,
        tool_schema_dialect=PERMISSIVE_TOOL_SCHEMA_DIALECT,
    )
    assert VIDEO_SCALE_ON_THE_WIRE in _wire(body)


# --------------------------------------------------------------------------
# One upstream call on the first request, on both senders
# --------------------------------------------------------------------------


def _refuses_lookaround(body: dict[str, Any]) -> bool:
    return any("(?!" in _wire(tool.get("parameters", {})) for tool in body["tools"])


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


def _transport(store: LearnedFactStore, **kwargs: Any) -> ResponsesTransport:
    return ResponsesTransport(
        ProviderConfig(api_key="sk-test", base_url="https://example.invalid/v1"),
        base_url="https://example.invalid/v1",
        provider_name="MUSE",
        identity=None,
        api_key=None,
        rate_limiter=passthrough_rate_limiter(),
        memory=store.memory_for(PROVIDER),
        **kwargs,
    )


def _install(transport: ResponsesTransport, handler: Any) -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []

    def _handler(http_request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(http_request.content))
        return handler(sent[-1])

    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    return sent


async def _run(transport: ResponsesTransport, request: MessagesRequest) -> list[str]:
    notes: dict[str, str] = {}
    body, headers = transport.build_body(
        request, reasoning=REASONING, max_output_tokens=512, wire_notes=notes
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
            wire_notes=notes,
        )
    ]


def _refusing(body: dict[str, Any]) -> httpx.Response:
    if _refuses_lookaround(body):
        return httpx.Response(400, json=RESPONSES_REFUSAL)
    return _accepted()


def _params(trace: Any) -> dict[str, Any]:
    return trace.requests[max(trace.requests)].params


@pytest.mark.asyncio
async def test_the_transport_first_request_is_one_call_and_says_what_left() -> None:
    store = LearnedFactStore()
    transport = _transport(store)
    trace = install_wire_trace()
    sent = _install(transport, _refusing)

    events = await _run(transport, _request())
    await transport.aclose()

    assert len(sent) == 1
    assert any("ok" in event for event in events)
    assert VIDEO_SCALE_ON_THE_WIRE not in _wire(sent[0])
    marker = _params(trace)["tool_schema_pruned"]
    assert marker == (
        "dropped pattern using lookaround from 1 tool (declared responses "
        f"dialect): {OFFENDING_TOOL} $.properties.videoScale.pattern"
    )
    # Names and paths only: never a schema body, never the regex.
    assert VIDEO_SCALE_PATTERN not in marker
    # A request that never paid the 400 teaches nothing.
    assert store.facts_for_provider(PROVIDER) == ()


def _chatgpt_provider() -> ChatGPTOAuthProvider:
    return ChatGPTOAuthProvider(
        ProviderConfig(
            api_key="test_token",
            base_url=CHATGPT_OAUTH_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
            max_concurrency=5,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )


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
async def test_chatgpt_oauths_first_request_is_one_call_and_says_what_left() -> None:
    provider = _chatgpt_provider()
    trace = install_wire_trace()

    async def _send(*, url: str, headers: dict[str, str], body: dict[str, Any]) -> Any:
        if _refuses_lookaround(body):
            request = httpx.Request("POST", url)
            raise httpx.HTTPStatusError(
                "ChatGPT OAuth API error 400",
                request=request,
                response=httpx.Response(400, json=RESPONSES_REFUSAL, request=request),
            )
        return _success()

    provider._send_stream_request = AsyncMock(side_effect=_send)
    chunks = await _drain(provider, _request(model="gpt-5.6-sol"))

    calls = provider._send_stream_request.await_args_list
    assert len(calls) == 1
    assert any("ok" in chunk for chunk in chunks)
    assert VIDEO_SCALE_ON_THE_WIRE not in _wire(calls[0].kwargs["body"])
    assert _params(trace)["tool_schema_pruned"].endswith(
        f"(declared responses dialect): {OFFENDING_TOOL} $.properties.videoScale.pattern"
    )


# --------------------------------------------------------------------------
# Composition: learned facts, the rung, the alias codec, the free tier
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_learned_lookaround_fact_finds_nothing_left_to_sweep() -> None:
    """SR1's fact and SR2's default name the same construct: zero 400s, one row."""

    store = LearnedFactStore()
    store.memory_for(PROVIDER).remember_responses_tool_schema_refusal(
        "pattern:lookaround", evidence="regex lookaround is not supported"
    )
    transport = _transport(store)
    trace = install_wire_trace()
    sent = _install(transport, _refusing)
    await _run(transport, _request())
    await transport.aclose()

    assert len(sent) == 1
    marker = _params(trace)["tool_schema_pruned"]
    assert "(declared responses dialect)" in marker
    assert "learned from this host" not in marker


@pytest.mark.asyncio
async def test_a_learned_fact_beyond_the_dialect_is_applied_and_both_are_recorded() -> (
    None
):
    store = LearnedFactStore()
    store.memory_for(PROVIDER).remember_responses_tool_schema_refusal(
        "$schema:*", evidence="unsupported $schema"
    )
    transport = _transport(store)
    trace = install_wire_trace()
    sent = _install(transport, _refusing)
    await _run(transport, _request())
    await transport.aclose()

    assert len(sent) == 1
    assert "$schema" not in _wire(sent[0])
    declared, learned = _params(trace)["tool_schema_pruned"].split("; ")
    assert declared.startswith("dropped pattern using lookaround")
    assert learned.startswith("dropped every $schema")
    assert "(learned from this host)" in learned


@pytest.mark.asyncio
async def test_the_rung_still_answers_a_construct_the_dialect_does_not_name() -> None:
    """The unknown construct is still one 400 and one retry, never two."""

    backreference = {
        "error": {
            "message": (
                "Invalid JSON schema: regex backreference is not supported. "
                "Found at $.properties.twice.pattern."
            ),
            "type": "invalid_request_error",
            "param": "tools",
            "code": "invalid_json_schema",
        }
    }
    request = _request()
    assert request.tools is not None
    send_message = request.tools[0].input_schema
    assert send_message is not None
    send_message["properties"]["twice"] = {
        "type": "string",
        "pattern": r"^(a)\1$",
    }

    def handler(body: dict[str, Any]) -> httpx.Response:
        if _refuses_lookaround(body):
            return httpx.Response(400, json=RESPONSES_REFUSAL)
        if r"\\1" in _wire(body["tools"]):
            return httpx.Response(400, json=backreference)
        return _accepted()

    store = LearnedFactStore()
    transport = _transport(store)
    trace = install_wire_trace()
    sent = _install(transport, handler)
    await _run(transport, request)
    await transport.aclose()

    assert len(sent) == 2
    declared, rung = _params(trace)["tool_schema_pruned"].split("; ")
    assert "(declared responses dialect)" in declared
    assert rung.startswith("dropped pattern using backreference")
    assert [(f.fact_kind, f.detail) for f in store.facts_for_provider(PROVIDER)] == [
        (FACT_RESPONSES_TOOL_SCHEMA_KEYWORD, "pattern:backreference")
    ]
    assert store.facts_for_provider(PROVIDER)[0].model_id == PROVIDER_WIDE_MODEL_ID


def test_the_alias_codec_runs_first_and_the_removal_names_the_wire_alias() -> None:
    long_name = "mcp__appium-mcp__" + "x" * 60
    request = _request(name=long_name)
    notes: dict[str, str] = {}
    body = build_responses_request_body(
        request, reasoning=REASONING, tool_name_max_length=64, wire_notes=notes
    )
    codec = responses_tool_name_codec(request, 64)
    assert codec is not None
    alias = codec.encode(long_name)
    assert alias != long_name
    assert body["tools"][1]["name"] == alias
    assert "pattern" not in body["tools"][1]["parameters"]["properties"]["videoScale"]
    assert f"{alias} $.properties.videoScale.pattern" in notes["tool_schema_pruned"]
    # And the alias still decodes to the client's own name.
    assert codec.decode(alias) == long_name


def test_the_free_tier_catalogue_is_renamed_first_and_then_swept() -> None:
    provider = create_openai_chat_provider(
        "opencode",
        ProviderConfig(api_key="sk-test", base_url="https://opencode.ai/zen/v1"),
        passthrough_rate_limiter(),
        profile=OPENAI_CHAT_PROFILES["opencode"],
    )
    request = MessagesRequest.model_validate(
        {
            "model": "muse-spark-1.3-contributor-free",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "list the files"}],
            "tools": [
                {
                    "name": "Bash",
                    "description": "run",
                    "input_schema": {"type": "object"},
                },
                {
                    "name": OFFENDING_TOOL,
                    "description": "record",
                    "input_schema": _video_scale_schema(),
                },
            ],
        }
    )
    notes: dict[str, str] = {}
    body, _ = provider._responses.build_body(
        request, reasoning=REASONING, max_output_tokens=64, wire_notes=notes
    )
    names = [tool["name"] for tool in body["tools"]]
    # The free tier's own spelling first (7.28.0), the 64-char alias (7.18.1)...
    assert names[0] == "bash"
    assert len(names[1]) <= 64
    # ...and then the dialect, under the name that actually went on the wire.
    assert VIDEO_SCALE_ON_THE_WIRE not in _wire(body)
    assert f"{names[1]} $.properties.videoScale.pattern" in notes["tool_schema_pruned"]


# --------------------------------------------------------------------------
# Prompt-cache stability
# --------------------------------------------------------------------------


def test_turn_n_and_turn_n_plus_one_share_a_byte_identical_tools_prefix() -> None:
    for build in (
        lambda request: build_responses_request_body(request, reasoning=REASONING),
        lambda request: build_chatgpt_oauth_request_body(request, reasoning=REASONING),
    ):
        turn_n = build(_request(turns=1))
        turn_n_plus_one = build(_request(turns=3))
        assert turn_n["input"] != turn_n_plus_one["input"]
        assert _wire(turn_n["tools"]) == _wire(turn_n_plus_one["tools"])
        assert VIDEO_SCALE_ON_THE_WIRE not in _wire(turn_n["tools"])
        # Key order is the same too, so the serialised prefix is the same.
        assert list(turn_n) == list(turn_n_plus_one)


# --------------------------------------------------------------------------
# Chat Completions is not this change's business
# --------------------------------------------------------------------------


def test_chat_completions_still_sends_the_schema_verbatim() -> None:
    request = _request()
    assert request.tools is not None
    converted = AnthropicToOpenAIConverter.convert_tools(request.tools)
    assert converted[1]["function"]["parameters"] is request.tools[1].input_schema
    assert VIDEO_SCALE_ON_THE_WIRE in _wire(converted)


# --------------------------------------------------------------------------
# The contract: no Responses host can be built around the seam
# --------------------------------------------------------------------------


def _calls_named(name: str) -> list[tuple[Path, ast.Call]]:
    found: list[tuple[Path, ast.Call]] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = (
                func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            )
            if called == name:
                found.append((path, node))
    return found


def test_tools_are_converted_in_one_place_and_it_always_sweeps() -> None:
    sites = _calls_named("_convert_tools")
    assert [path.name for path, _ in sites] == ["conversion.py"]
    assert all("openai_responses" in path.parts for path, _ in sites)
    # The body builder's dialect is never optional, so a caller that passes
    # nothing still gets the default.
    parameter = inspect.signature(build_responses_request_body).parameters[
        "tool_schema_dialect"
    ]
    assert parameter.default is RESPONSES_TOOL_SCHEMA_DIALECT


def test_every_responses_transport_is_handed_a_declared_dialect() -> None:
    sites = _calls_named("ResponsesTransport")
    assert sites, "the transport is constructed somewhere in src"
    for path, call in sites:
        keywords = {keyword.arg for keyword in call.keywords}
        assert "tool_schema_dialect" in keywords, path


def test_a_dialect_entry_is_the_rungs_own_refusal_type() -> None:
    assert all(
        isinstance(refusal, SchemaKeywordRefusal)
        for refusal in RESPONSES_TOOL_SCHEMA_DIALECT.refused
    )
