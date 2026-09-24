"""SR3: the upstream ``\\p{...}`` translation, on the Responses surface only.

Decision 3 of the second-pass review, scoped (a)-(c):

(a) wired into the **Responses** converter only -- Chat Completions and the
    native Anthropic passthrough send exactly what they sent in 7.38.0;
(b) run **before** the 7.38.0 dialect's lookaround drop, through the same
    walker, so a pattern whose only problem is ``\\p{}`` is repaired and one
    that also carries lookaround is dropped after the repair -- which is why
    Claude Code's ``Artifact`` pattern is **not** rescued, and its bytes are
    identical to 7.38.0's;
(c) the ~300 ms table build happens on the startup worker thread, never on
    the event loop of a request that carries no ``\\p{``.

No test here makes an upstream call.
"""

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from my_claude_code.core.anthropic.conversion import AnthropicToOpenAIConverter
from my_claude_code.core.anthropic.models import Message, MessagesRequest, Tool
from my_claude_code.core.tool_schema_patterns import (
    reset_unicode_property_ranges_for_tests,
    unicode_property_ranges_ready,
)
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.chatgpt_oauth.conversion import (
    CHATGPT_OAUTH_TOOL_SCHEMA_DIALECT,
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.openai_responses import (
    PERMISSIVE_TOOL_SCHEMA_DIALECT,
    RESPONSES_TOOL_SCHEMA_DIALECT,
    build_responses_request_body,
)
from my_claude_code.providers.openai_responses.tool_schema_dialect import (
    sweep_tool_catalogue,
)
from my_claude_code.providers.recovery import (
    FACT_RESPONSES_TOOL_SCHEMA_KEYWORD,
    TOOL_SCHEMA_PRUNED,
    TOOL_SCHEMA_TRANSLATED,
    LearnedFactStore,
    merge_tool_schema_markers,
)
from my_claude_code.runtime import warmup
from tests.providers.test_responses_tool_schema_dialect import (
    PROVIDER,
    REASONING,
    _accepted,
    _chatgpt_provider,
    _drain,
    _install,
    _params,
    _run,
    _success,
    _transport,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "my_claude_code"

#: Claude Code's Artifact pattern, verbatim (upstream 39b9c272's fixture).
ARTIFACT_PATTERN = r'^(?!__.*__$)[^\p{Cc}\p{Cf}\p{Zl}\p{Zp}"\\./[\]]{1,200}$'
#: The same restriction without the lookahead: the case SR3 actually repairs.
PROPERTY_ONLY_PATTERN = r"^[^\p{Cc}\p{Cf}\p{Zl}\p{Zp}]{1,200}$"
#: A property escape the scanner does not translate (another category).
UNTRANSLATABLE_PATTERN = r"^[^\p{L}]+$"

UNICODE_PROPERTY_REFUSAL = {
    "error": {
        "message": (
            "Invalid JSON schema: regex unicode property classes are not "
            "supported. Found at $.properties.name.pattern."
        ),
        "type": "invalid_request_error",
        "param": "tools",
        "code": "invalid_json_schema",
    }
}


def _wire(value: Any) -> str:
    return json.dumps(value)


def _tool(name: str, pattern: str) -> Tool:
    return Tool(
        name=name,
        description="Save",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string", "pattern": pattern}},
            "required": ["name"],
        },
    )


def _request(*tools: Tool, turns: int = 1) -> MessagesRequest:
    messages = [Message(role="user", content="hi")]
    for turn in range(1, turns):
        messages.append(Message(role="assistant", content=f"answer {turn}"))
        messages.append(Message(role="user", content=f"question {turn}"))
    return MessagesRequest(
        model="gpt-5.6-sol", max_tokens=64, messages=messages, tools=list(tools)
    )


def _pattern_of(body: dict[str, Any], index: int = 0) -> str | None:
    return body["tools"][index]["parameters"]["properties"]["name"].get("pattern")


def _refuses_property_escape(body: dict[str, Any]) -> bool:
    return any(r"\\p{" in _wire(tool["parameters"]) for tool in body["tools"])


# --------------------------------------------------------------------------
# The declaration
# --------------------------------------------------------------------------


def test_the_responses_dialect_translates_and_the_permissive_one_does_not() -> None:
    assert RESPONSES_TOOL_SCHEMA_DIALECT.translate_unicode_properties is True
    assert CHATGPT_OAUTH_TOOL_SCHEMA_DIALECT is RESPONSES_TOOL_SCHEMA_DIALECT
    assert PERMISSIVE_TOOL_SCHEMA_DIALECT.translate_unicode_properties is False


# --------------------------------------------------------------------------
# (b) repaired before the drop
# --------------------------------------------------------------------------


@pytest.mark.parametrize("builder", ["responses", "chatgpt_oauth"])
def test_a_property_only_pattern_is_repaired_and_recorded(builder: str) -> None:
    request = _request(_tool("Artifact", PROPERTY_ONLY_PATTERN))
    before = deepcopy(request.model_dump())
    notes: dict[str, str] = {}
    if builder == "responses":
        body = build_responses_request_body(
            request, reasoning=REASONING, wire_notes=notes
        )
    else:
        body = build_chatgpt_oauth_request_body(
            request, reasoning=REASONING, wire_notes=notes
        )

    translated = _pattern_of(body)
    assert translated is not None
    assert r"\p{" not in translated
    regex = re.compile(translated)
    assert regex.fullmatch("demo")
    assert not regex.fullmatch("bad\u2028name")
    assert not regex.fullmatch("bad\U000e0001name")
    assert body["tools"][0]["parameters"]["required"] == ["name"]
    # The client's request is never mutated.
    assert request.model_dump() == before
    assert notes == {
        TOOL_SCHEMA_TRANSLATED: (
            "translated Unicode property escapes in pattern of 1 tool (declared "
            "responses dialect): Artifact $.properties.name.pattern"
        )
    }
    # Names and paths only, never a regex.
    assert PROPERTY_ONLY_PATTERN not in notes[TOOL_SCHEMA_TRANSLATED]


def test_the_artifact_pattern_is_not_rescued_and_its_bytes_equal_7_38_0s() -> None:
    """The lookahead still goes -- exactly as 7.38.0 dropped it, byte for byte."""

    seven_thirty_eight = type(RESPONSES_TOOL_SCHEMA_DIALECT)(
        name=RESPONSES_TOOL_SCHEMA_DIALECT.name,
        refused=RESPONSES_TOOL_SCHEMA_DIALECT.refused,
    )
    assert seven_thirty_eight.translate_unicode_properties is False
    request = _request(_tool("Artifact", ARTIFACT_PATTERN))
    notes: dict[str, str] = {}
    now = build_responses_request_body(request, reasoning=REASONING, wire_notes=notes)
    then = build_responses_request_body(
        request, reasoning=REASONING, tool_schema_dialect=seven_thirty_eight
    )

    assert _pattern_of(now) is None
    assert _wire(now) == _wire(then)
    # Recorded as the removal it is; the discarded translation is not claimed.
    assert set(notes) == {TOOL_SCHEMA_PRUNED}
    assert notes[TOOL_SCHEMA_PRUNED].endswith(
        "(declared responses dialect): Artifact $.properties.name.pattern"
    )


def test_an_untranslatable_escape_is_sent_as_written() -> None:
    request = _request(_tool("Lettersless", UNTRANSLATABLE_PATTERN))
    notes: dict[str, str] = {}
    body = build_responses_request_body(request, reasoning=REASONING, wire_notes=notes)
    assert request.tools is not None
    assert body["tools"][0]["parameters"] is request.tools[0].input_schema
    assert notes == {}


def test_nothing_to_translate_is_the_very_same_catalogue() -> None:
    tools = [
        {
            "type": "function",
            "name": "plain",
            "description": "",
            "parameters": {"properties": {"a": {"pattern": r"^[a-z]+$"}}},
        }
    ]
    swept = sweep_tool_catalogue(tools, RESPONSES_TOOL_SCHEMA_DIALECT)
    assert swept.tools is tools
    assert swept.wire_marker(RESPONSES_TOOL_SCHEMA_DIALECT) == {}


def test_turn_n_and_turn_n_plus_one_share_a_byte_identical_tools_prefix() -> None:
    tools = (_tool("Artifact", ARTIFACT_PATTERN), _tool("Name", PROPERTY_ONLY_PATTERN))
    for build in (build_responses_request_body, build_chatgpt_oauth_request_body):
        turn_n = build(_request(*tools, turns=1), reasoning=REASONING)
        turn_n_plus_one = build(_request(*tools, turns=3), reasoning=REASONING)
        assert _wire(turn_n["tools"]) == _wire(turn_n_plus_one["tools"])


def test_both_sweeps_are_recorded_side_by_side() -> None:
    request = _request(
        _tool("Artifact", ARTIFACT_PATTERN), _tool("Name", PROPERTY_ONLY_PATTERN)
    )
    notes: dict[str, str] = {}
    build_responses_request_body(request, reasoning=REASONING, wire_notes=notes)
    assert list(notes) == [TOOL_SCHEMA_TRANSLATED, TOOL_SCHEMA_PRUNED]
    assert notes[TOOL_SCHEMA_TRANSLATED].endswith(": Name $.properties.name.pattern")
    assert notes[TOOL_SCHEMA_PRUNED].endswith(": Artifact $.properties.name.pattern")
    # A later sweep's merge keeps the translation line.
    merged = merge_tool_schema_markers(notes, {TOOL_SCHEMA_PRUNED: "rung line"})
    assert merged[TOOL_SCHEMA_TRANSLATED] == notes[TOOL_SCHEMA_TRANSLATED]
    assert merged[TOOL_SCHEMA_PRUNED] == f"{notes[TOOL_SCHEMA_PRUNED]}; rung line"


# --------------------------------------------------------------------------
# With SR1's rung and learned facts, on both senders, no upstream call
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_host_refusing_property_escapes_is_one_call_when_translatable() -> None:
    store = LearnedFactStore()
    transport = _transport(store)
    trace = install_wire_trace()

    def handler(body: dict[str, Any]) -> httpx.Response:
        if _refuses_property_escape(body):
            return httpx.Response(400, json=UNICODE_PROPERTY_REFUSAL)
        return _accepted()

    sent = _install(transport, handler)
    await _run(transport, _request(_tool("Name", PROPERTY_ONLY_PATTERN)))
    await transport.aclose()

    assert len(sent) == 1
    assert _pattern_of(sent[0]) is not None
    params = _params(trace)
    assert TOOL_SCHEMA_TRANSLATED in params
    assert TOOL_SCHEMA_PRUNED not in params
    assert store.facts_for_provider(PROVIDER) == ()


@pytest.mark.asyncio
async def test_the_rung_still_answers_an_escape_the_scanner_leaves_alone() -> None:
    store = LearnedFactStore()
    transport = _transport(store)
    trace = install_wire_trace()

    def handler(body: dict[str, Any]) -> httpx.Response:
        if _refuses_property_escape(body):
            return httpx.Response(400, json=UNICODE_PROPERTY_REFUSAL)
        return _accepted()

    sent = _install(transport, handler)
    await _run(transport, _request(_tool("Letterless", UNTRANSLATABLE_PATTERN)))
    await transport.aclose()

    assert len(sent) == 2
    assert _pattern_of(sent[1]) is None
    assert _params(trace)[TOOL_SCHEMA_PRUNED].startswith(
        "dropped pattern using unicode_property"
    )
    assert [(f.fact_kind, f.detail) for f in store.facts_for_provider(PROVIDER)] == [
        (FACT_RESPONSES_TOOL_SCHEMA_KEYWORD, "pattern:unicode_property")
    ]


@pytest.mark.asyncio
async def test_a_learned_property_fact_finds_the_repaired_pattern_clean() -> None:
    """Translation runs first, so SR1's learned sweep has nothing left to drop."""

    store = LearnedFactStore()
    store.memory_for(PROVIDER).remember_responses_tool_schema_refusal(
        "pattern:unicode_property", evidence="unicode property classes"
    )
    transport = _transport(store)
    trace = install_wire_trace()
    sent = _install(transport, lambda body: _accepted())
    await _run(transport, _request(_tool("Name", PROPERTY_ONLY_PATTERN)))
    await transport.aclose()

    assert len(sent) == 1
    assert _pattern_of(sent[0]) is not None
    assert r"\\p{" not in _wire(sent[0]["tools"])
    assert TOOL_SCHEMA_PRUNED not in _params(trace)


@pytest.mark.asyncio
async def test_chatgpt_oauth_sends_the_repaired_pattern_in_one_call() -> None:
    provider = _chatgpt_provider()
    trace = install_wire_trace()

    async def _send(*, url: str, headers: dict[str, str], body: dict[str, Any]) -> Any:
        if _refuses_property_escape(body):
            request = httpx.Request("POST", url)
            raise httpx.HTTPStatusError(
                "ChatGPT OAuth API error 400",
                request=request,
                response=httpx.Response(
                    400, json=UNICODE_PROPERTY_REFUSAL, request=request
                ),
            )
        return _success()

    provider._send_stream_request = AsyncMock(side_effect=_send)
    chunks = await _drain(provider, _request(_tool("Name", PROPERTY_ONLY_PATTERN)))

    calls = provider._send_stream_request.await_args_list
    assert len(calls) == 1
    assert any("ok" in chunk for chunk in chunks)
    assert _pattern_of(calls[0].kwargs["body"]) is not None
    assert _params(trace)[TOOL_SCHEMA_TRANSLATED].endswith(
        "(declared responses dialect): Name $.properties.name.pattern"
    )


# --------------------------------------------------------------------------
# (a) Chat Completions and native Anthropic are not this change's business
# --------------------------------------------------------------------------


def test_chat_completions_still_sends_the_property_escape_verbatim() -> None:
    request = _request(_tool("Name", PROPERTY_ONLY_PATTERN))
    assert request.tools is not None
    converted = AnthropicToOpenAIConverter.convert_tools(request.tools)
    assert converted[0]["function"]["parameters"] is request.tools[0].input_schema
    assert (
        PROPERTY_ONLY_PATTERN
        in converted[0]["function"]["parameters"]["properties"]["name"]["pattern"]
    )


def test_only_the_responses_dialect_and_the_warm_up_import_the_translator() -> None:
    importers = sorted(
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if "tool_schema_patterns" in path.read_text(encoding="utf-8")
        and path.name != "tool_schema_patterns.py"
    )
    assert importers == [
        "providers/openai_responses/tool_schema_dialect.py",
        "runtime/warmup.py",
    ]


# --------------------------------------------------------------------------
# (c) the table build never lands on the event loop by default
# --------------------------------------------------------------------------


def test_the_startup_warm_up_builds_the_tables() -> None:
    reset_unicode_property_ranges_for_tests()
    try:
        assert not unicode_property_ranges_ready()
        warmup._warm_unicode_property_ranges()
        assert unicode_property_ranges_ready()
    finally:
        reset_unicode_property_ranges_for_tests()


def test_the_warm_up_thread_runs_the_table_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []
    monkeypatch.setattr(warmup, "_warm_openai_sdk", lambda: ran.append("sdk"))
    monkeypatch.setattr(
        warmup, "_warm_models_dev_indexes", lambda _path: ran.append("models")
    )
    monkeypatch.setattr(
        warmup, "_warm_unicode_property_ranges", lambda: ran.append("unicode")
    )
    warmup._warm(None)
    assert ran == ["sdk", "models", "unicode"]


def test_a_request_without_a_property_escape_never_builds_the_tables() -> None:
    reset_unicode_property_ranges_for_tests()
    try:
        request = _request(_tool("Artifactless", r"^(?![\s\S])[a-z]+$"))
        build_responses_request_body(request, reasoning=REASONING)
        build_chatgpt_oauth_request_body(request, reasoning=REASONING)
        assert not unicode_property_ranges_ready()
    finally:
        reset_unicode_property_ranges_for_tests()
