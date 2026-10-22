"""Command Code's Chat route drops a tool ``pattern`` carrying ``\\0`` (7.105.1).

CC-DEEPSEEK-RESUMED-400 (``INVESTIGATION-COMMANDCODE-DEEPSEEK-RESUMED-400.md``
§14-§18, decisions of 2026-10-21 01:01 and 21:33). Claude Code's newer
built-in ``Artifact`` tool constrains ``file_paths`` items with ``^[^\\0]*$``.
Command Code's DeepSeek back end refused every request carrying it -- 445 of
445 refusals, 0 of 3,677 successes -- mostly behind ``invalid request error
trace_id: ...``. The user's probe pair on the real 33-tool catalogue
(``tests/fixtures/commandcode_schema/``): unmodified 400, the same body
without only that pattern 200. Pinned here:

* the ``nul_escape`` construct is the ``\\0`` escape, not one string, and
  never a literal backslash-zero, and no refusal wording can name it;
* a Chat profile declares a schema dialect or gets no sweep at all, and the
  sweep hands back the very same objects when nothing offends;
* only Command Code declares one; the 33-tool catalogue loses exactly
  ``Artifact $.properties.file_paths.items.pattern`` there, recorded under
  ``tool_schema_pruned``, and the six patterns working requests carry stay;
* Agnes-shaped, native DeepSeek and every other Chat host send it unchanged;
* the Responses route keeps its lookaround-only default.
"""

import copy
import json
from dataclasses import replace
from functools import cache
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from my_claude_code.core.anthropic import AnthropicToOpenAIConverter, MessagesRequest
from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.commandcode import CommandCodeProvider
from my_claude_code.providers.commandcode.client import (
    _PROFILE as COMMANDCODE_PROFILE,
)
from my_claude_code.providers.commandcode.client import (
    COMMANDCODE_CHAT_TOOL_SCHEMA_DIALECT,
)
from my_claude_code.providers.deepseek import DeepSeekProvider
from my_claude_code.providers.mistral import MistralProvider
from my_claude_code.providers.openai_chat import (
    GENERIC_OPENAI_PROFILE,
    OPENAI_CHAT_PROFILES,
    OpenAIChatProvider,
    create_openai_chat_provider,
)
from my_claude_code.providers.openai_chat.request_policy import (
    chat_tool_schema_note,
    sweep_chat_tool_schemas,
)
from my_claude_code.providers.openai_responses import (
    RESPONSES_TOOL_SCHEMA_DIALECT,
    build_responses_request_body,
    regex_construct,
)
from my_claude_code.providers.recovery import REGEX_CONSTRUCTS, TOOL_SCHEMA_PRUNED
from my_claude_code.providers.recovery.tool_schema_refusal import (
    construct_named_by,
    pattern_uses,
)
from tests.providers.support import passthrough_rate_limiter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ARTIFACT_33 = FIXTURES / "commandcode_schema" / "claude_code_artifact_33_tools.json"
REASONING = ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
NUL = regex_construct("nul_escape")

#: The one pattern Command Code refuses, as it sits in the schema.
NUL_PATTERN = "^[^\\0]*$"
#: The six patterns the same catalogue carries and Command Code accepted
#: beside it (the p7 probe, HTTP 200), two of them in thousands of working
#: requests: the Artifact ``contract`` version (3,536) and the Workflow run id
#: (731). Every one of them must still be on the wire.
ACCEPTED_PATTERNS = (
    "^[A-Za-z0-9_=-]{1,4096}$",
    "^[0-9a-f]{32}$",
    "^(0|[1-9]\\d{0,3})\\.(0|[1-9]\\d{0,4})\\.(0|[1-9]\\d{0,5})$",
    "^wf_[a-z0-9-]{6,}$",
    "^[^\\n\\r]*$",
    "^[\\s\\S]{0,300}$",
)
REMOVED_PATH = "Artifact $.properties.file_paths.items.pattern"
NOTE = (
    "dropped pattern using nul_escape from 1 tool (declared commandcode-chat "
    f"dialect): {REMOVED_PATH}"
)


# --------------------------------------------------------------------------
# Inputs
# --------------------------------------------------------------------------


def _artifact_tools() -> list[dict[str, Any]]:
    """The user-run probe's 33 real schemas, invented descriptions (p6)."""
    return json.loads(ARTIFACT_33.read_text(encoding="utf-8"))["tools"]


def _without_nul_pattern(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The p7 probe's catalogue: p6 without only the ``\\0`` pattern."""
    stripped = copy.deepcopy(tools)
    artifact = next(tool for tool in stripped if tool["name"] == "Artifact")
    del artifact["input_schema"]["properties"]["file_paths"]["items"]["pattern"]
    return stripped


def _corpus() -> dict[str, list[dict[str, Any]]]:
    """Real-shaped tool lists with no ``\\0`` pattern, one per harness."""
    corpus = {
        path.stem: json.loads(path.read_text(encoding="utf-8"))["tools"]
        for path in sorted((FIXTURES / "tool_catalogues").glob("*.json"))
    }
    corpus["claude_code_artifact_33_without_nul"] = _without_nul_pattern(
        _artifact_tools()
    )
    return corpus


def _request(
    tools: list[dict[str, Any]], model: str = "deepseek/deepseek-v4.1-flash"
) -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": model,
            "max_tokens": 1024,
            "stream": True,
            "system": "You are a test assistant. Keep every answer to one word.",
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "tools": tools,
        }
    )


def _config(base_url: str = "https://chat.invalid/v1") -> ProviderConfig:
    return ProviderConfig(
        api_key="sk-test",
        base_url=base_url,
        rate_limit=1_000_000,
        rate_window=60,
        commit_holdback_seconds=0.0,
        commit_holdback_chars=0,
    )


@cache
def _chat_senders() -> dict[str, OpenAIChatProvider]:
    """Every Chat Completions sender whose body the shared builder makes."""
    limiter = passthrough_rate_limiter()
    senders: dict[str, OpenAIChatProvider] = {
        provider_id: create_openai_chat_provider(provider_id, _config(), limiter)
        for provider_id in sorted(OPENAI_CHAT_PROFILES)
    }
    senders["custom_agnes_shaped"] = OpenAIChatProvider(
        _config(),
        profile=GENERIC_OPENAI_PROFILE,
        rate_limiter=limiter,
        provider_id="custom_agnes",
    )
    senders["deepseek"] = DeepSeekProvider(_config(), rate_limiter=limiter)
    senders["mistral"] = MistralProvider(_config(), rate_limiter=limiter)
    senders["commandcode_chat"] = CommandCodeProvider(
        _config(), rate_limiter=limiter
    )._openai
    return senders


def _patterns(node: Any) -> list[str]:
    """Every ``pattern`` value in a schema tree, in document order."""
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "pattern" and isinstance(value, str):
                found.append(value)
            found.extend(_patterns(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_patterns(item))
    return found


def _wire(body: dict[str, Any]) -> str:
    return json.dumps(body, ensure_ascii=False)


# --------------------------------------------------------------------------
# The construct
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern",
    [
        NUL_PATTERN,
        "[\\0-\\x1f]",
        "\\0",
        "\\00",
        "abc\\0",
        "^[^\\0\\n]+$",
        # An escaped backslash, then the escape: ``\\`` then ``\0``.
        "\\\\\\0",
    ],
)
def test_a_pattern_carrying_the_escape_is_found(pattern: str) -> None:
    assert pattern_uses(pattern, NUL)


@pytest.mark.parametrize(
    "pattern",
    [
        *ACCEPTED_PATTERNS,
        # A literal backslash followed by ``0`` -- the backslash is escaped.
        "\\\\0",
        "C:\\\\0",
        "\\x00",
        "\\u0000",
        "[0-9]+",
        "0",
        "",
        "\\",
        "abc\\",
        "\\d0",
    ],
)
def test_a_pattern_without_the_escape_is_left_alone(pattern: str) -> None:
    assert not pattern_uses(pattern, NUL)


@pytest.mark.parametrize(
    "complaint",
    [
        # The host's only stated wording, both quotings of it.
        'invalid schema for function \'artifact\': "^[^\\\\0]*$" is not a "regex"',
        "invalid schema for function 'artifact': '^[^\\0]*$' is not a 'regex'.",
        "invalid request error trace_id: 6577d5a6796678f042cc940fcb64df39",
    ],
)
def test_no_refusal_wording_names_the_construct(complaint: str) -> None:
    """Reachable by declaration only: the rung's reading of every 400 is as before."""
    construct = construct_named_by(complaint)
    assert construct is None or construct.name != "nul_escape"


def test_the_construct_is_the_last_row_so_earlier_rows_match_first() -> None:
    assert REGEX_CONSTRUCTS[-1] is NUL
    assert NUL.host_words == ()


def test_the_responses_default_is_still_lookaround_only() -> None:
    assert [
        (refusal.keyword, refusal.construct and refusal.construct.name)
        for refusal in RESPONSES_TOOL_SCHEMA_DIALECT.refused
    ] == [("pattern", "lookaround")]


# --------------------------------------------------------------------------
# The sweep: identity when nothing offends
# --------------------------------------------------------------------------


@pytest.mark.parametrize("case", sorted(_corpus()))
def test_the_sweep_returns_the_same_objects_when_nothing_offends(case: str) -> None:
    converted = AnthropicToOpenAIConverter.convert_tools(
        _request(_corpus()[case]).tools or []
    )
    swept, note = sweep_chat_tool_schemas(
        converted, COMMANDCODE_CHAT_TOOL_SCHEMA_DIALECT
    )
    assert swept is converted
    assert all(new is old for new, old in zip(swept, converted, strict=True))
    assert note == {}


def test_a_tool_without_a_function_object_passes_through() -> None:
    tools: list[Any] = [{"type": "web_search"}, "not-a-tool"]
    swept, note = sweep_chat_tool_schemas(tools, COMMANDCODE_CHAT_TOOL_SCHEMA_DIALECT)
    assert swept is tools
    assert note == {}


def test_the_sweep_copies_on_write_and_never_mutates_the_client() -> None:
    tools = _artifact_tools()
    before = copy.deepcopy(tools)
    converted = AnthropicToOpenAIConverter.convert_tools(_request(tools).tools or [])
    swept, note = sweep_chat_tool_schemas(
        converted, COMMANDCODE_CHAT_TOOL_SCHEMA_DIALECT
    )

    assert swept is not converted
    changed = [
        old["function"]["name"]
        for new, old in zip(swept, converted, strict=True)
        if new is not old
    ]
    assert changed == ["Artifact"]
    assert note == {TOOL_SCHEMA_PRUNED: NOTE}
    assert _patterns(converted) == _patterns([tool["input_schema"] for tool in before])
    assert tools == before, "the client's own schemas are never touched"


# --------------------------------------------------------------------------
# The builder, on every Chat host
# --------------------------------------------------------------------------


#: Senders that hand the client's schema dicts to the wire by reference. Native
#: DeepSeek copies its body itself (``deepseek/compat.py``), before this
#: change as after, so identity is not a property it ever had; its bytes are
#: pinned by the equality corpora and by ``test_only_command_code_drops``.
_COPYING_SENDERS = frozenset({"deepseek"})


@pytest.mark.parametrize("case", sorted(_corpus()))
@pytest.mark.parametrize("sender", sorted(set(_chat_senders()) - _COPYING_SENDERS))
def test_every_chat_builder_sends_the_clients_own_schemas(
    sender: str, case: str
) -> None:
    """Without the construct, on every host -- Command Code included -- the
    body's ``parameters`` are the very dicts the client sent."""

    request = _request(_corpus()[case])
    body = _chat_senders()[sender]._build_request_body(request, reasoning=REASONING)
    sent = [tool["function"]["parameters"] for tool in body["tools"]]
    client = [tool.input_schema for tool in request.tools or []]
    assert all(new is old for new, old in zip(sent, client, strict=True))


@pytest.mark.parametrize("sender", sorted(_chat_senders()))
def test_only_command_code_drops_the_pattern(sender: str) -> None:
    request = _request(_artifact_tools())
    body = _chat_senders()[sender]._build_request_body(request, reasoning=REASONING)
    patterns = _patterns(body["tools"])
    if sender == "commandcode_chat":
        assert NUL_PATTERN not in patterns
    else:
        assert NUL_PATTERN in patterns
    for accepted in ACCEPTED_PATTERNS:
        assert accepted in patterns, accepted


def test_command_code_loses_exactly_one_key_and_nothing_else() -> None:
    """The p6 catalogue on Command Code is the p7 catalogue, byte for byte."""

    sender = _chat_senders()["commandcode_chat"]
    p6 = sender._build_request_body(_request(_artifact_tools()), reasoning=REASONING)
    p7 = sender._build_request_body(
        _request(_without_nul_pattern(_artifact_tools())), reasoning=REASONING
    )
    assert _wire(p6) == _wire(p7)
    assert len(_wire(p6)) == len(_wire_unswept()) - len('"pattern": "^[^\\\\0]*$", ')


def _wire_unswept() -> str:
    """The p6 body from Command Code's own profile with the declaration taken out."""
    sender = OpenAIChatProvider(
        _config(),
        profile=replace(COMMANDCODE_PROFILE, chat_tool_schema_dialect=None),
        rate_limiter=passthrough_rate_limiter(),
        provider_id="commandcode",
    )
    return _wire(
        sender._build_request_body(_request(_artifact_tools()), reasoning=REASONING)
    )


def test_command_code_without_the_declaration_is_todays_body() -> None:
    """The declaration is the only difference: with it gone the body is the
    unswept body, so nothing else in this change reaches Command Code."""

    sender = _chat_senders()["commandcode_chat"]
    swept = _wire(
        sender._build_request_body(_request(_artifact_tools()), reasoning=REASONING)
    )
    unswept = _wire_unswept()
    assert NUL_PATTERN.replace("\\", "\\\\") in unswept
    assert swept == unswept.replace('"pattern": "^[^\\\\0]*$", ', "", 1)


def test_the_same_catalogue_gives_the_same_bytes_every_turn() -> None:
    sender = _chat_senders()["commandcode_chat"]
    first = _wire(
        sender._build_request_body(_request(_artifact_tools()), reasoning=REASONING)
    )
    second = _wire(
        sender._build_request_body(_request(_artifact_tools()), reasoning=REASONING)
    )
    assert first == second


def test_the_note_names_the_one_removal_and_only_on_command_code() -> None:
    request = _request(_artifact_tools())
    assert chat_tool_schema_note(request, COMMANDCODE_CHAT_TOOL_SCHEMA_DIALECT) == {
        TOOL_SCHEMA_PRUNED: NOTE
    }
    for name, sender in _chat_senders().items():
        expected = {TOOL_SCHEMA_PRUNED: NOTE} if name == "commandcode_chat" else {}
        assert sender.tool_schema_wire_note(request) == expected, name
    clean = _request(_without_nul_pattern(_artifact_tools()))
    assert _chat_senders()["commandcode_chat"].tool_schema_wire_note(clean) == {}


def test_only_command_code_declares_a_chat_dialect() -> None:
    declared = {
        provider_id
        for provider_id, profile in OPENAI_CHAT_PROFILES.items()
        if profile.chat_tool_schema_dialect is not None
    }
    assert declared == set()
    assert GENERIC_OPENAI_PROFILE.chat_tool_schema_dialect is None
    assert COMMANDCODE_PROFILE.chat_tool_schema_dialect is (
        COMMANDCODE_CHAT_TOOL_SCHEMA_DIALECT
    )


def test_the_responses_route_still_sends_the_pattern() -> None:
    body = build_responses_request_body(
        _request(_artifact_tools(), model="gpt-test"), reasoning=REASONING
    )
    assert NUL_PATTERN in _patterns(body["tools"])


# --------------------------------------------------------------------------
# On the wire: the recorded note, and a host that refuses the pattern
# --------------------------------------------------------------------------

_CHAT_SSE = (
    b'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"m",'
    b'"choices":[{"index":0,"delta":{"role":"assistant","content":"OK"},'
    b'"finish_reason":null}]}\n\n'
    b'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"m",'
    b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)

#: Command Code's answer to a catalogue carrying the pattern, as the p6 probe
#: recorded it (the trace id is the probe's own).
_REFUSAL = {
    "message": json.dumps(
        {
            "message": "invalid request error trace_id: "
            "6577d5a6796678f042cc940fcb64df39",
            "type": "invalid_request_error",
        }
    )
    + "\n",
    "type": "invalid_request_error",
}


def _recording_client(sent: list[dict[str, Any]], *, refuse: bool) -> AsyncOpenAI:
    """An upstream that records every body; Command Code-shaped when ``refuse``
    (400 on ``\\0`` in any pattern, as the p6 probe was answered), else OK."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        sent.append(body)
        if refuse and any(
            pattern_uses(pattern, NUL)
            for tool in body.get("tools", [])
            for pattern in _patterns(tool)
        ):
            return httpx.Response(400, json=_REFUSAL, request=request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_CHAT_SSE,
            request=request,
        )

    return AsyncOpenAI(
        api_key="sk-test",
        base_url="http://chat.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


async def _stream(
    outer: Any,
    sender: OpenAIChatProvider,
    request: MessagesRequest,
    *,
    refuse: bool = True,
) -> tuple[list[str], list[dict[str, Any]], dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    sender._client = _recording_client(sent, refuse=refuse)
    trace = install_wire_trace()
    try:
        events = [
            event async for event in outer.stream_response(request, reasoning=REASONING)
        ]
    finally:
        await outer.cleanup()
    return events, sent, trace.requests[max(trace.requests)].params


@pytest.mark.asyncio
async def test_command_code_is_answered_and_records_the_note() -> None:
    outer = CommandCodeProvider(_config(), rate_limiter=passthrough_rate_limiter())
    events, sent, params = await _stream(
        outer, outer._openai, _request(_artifact_tools())
    )

    assert len(sent) == 1, "answered on the first send, no retry"
    assert any('"OK"' in event for event in events)
    assert params[TOOL_SCHEMA_PRUNED] == NOTE
    assert TOOL_SCHEMA_PRUNED not in sent[0]
    assert NUL_PATTERN not in _patterns(sent[0]["tools"])
    for accepted in ACCEPTED_PATTERNS:
        assert accepted in _patterns(sent[0]["tools"])


@pytest.mark.asyncio
async def test_command_code_without_the_pattern_records_no_note() -> None:
    outer = CommandCodeProvider(_config(), rate_limiter=passthrough_rate_limiter())
    _events, sent, params = await _stream(
        outer, outer._openai, _request(_without_nul_pattern(_artifact_tools()))
    )
    assert len(sent) == 1
    assert TOOL_SCHEMA_PRUNED not in params


@pytest.mark.asyncio
async def test_a_host_without_the_declaration_still_sends_the_pattern() -> None:
    """Agnes-shaped: no dialect, the pattern goes out and no note is written."""

    sender = OpenAIChatProvider(
        _config(),
        profile=GENERIC_OPENAI_PROFILE,
        rate_limiter=passthrough_rate_limiter(),
        provider_id="custom_agnes",
    )
    events, sent, params = await _stream(
        sender,
        sender,
        _request(_artifact_tools(), model="agnes-3.0-flash"),
        refuse=False,
    )
    assert any('"OK"' in event for event in events)
    assert NUL_PATTERN in _patterns(sent[0]["tools"])
    assert TOOL_SCHEMA_PRUNED not in params
