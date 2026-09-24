"""A Responses host refuses one schema keyword; both senders answer it once.

The incident these tests replay, measured 216 times on 2026-09-20 against
``chatgpt_oauth/gpt-5.6-sol``: one MCP tool out of 212 carries

    "pattern": "^(?:[1-9]\\\\d{0,4}|-[12]):(?:[1-9]\\\\d{0,4}|-[12])(?![\\\\s\\\\S])"

-- the V8 spelling of "end of input" -- and OpenAI's Rust-backed validator,
which has no lookaround at all, refuses **the whole request**:

    {"error": {"message": "Invalid JSON schema: regex lookaround is not
     supported. Found at $.properties.videoScale.pattern.",
     "type": "invalid_request_error", "param": "tools",
     "code": "invalid_json_schema"}}

Three things have to be true together for that to stop costing a first attempt
per request, and each of them is a section below:

* the matcher reads the host's **structured** words and nothing else -- the
  request it is refusing quotes the offending regex and the word ``tools``, so
  a flat-string matcher would fire on its own payload;
* the rewrite is a **sweep** of the whole catalogue for the keyword class,
  because the host names one offence at a time and the ladder gives the rung
  one firing;
* what is learned is keyed ``(provider, keyword, construct)``, so the 400 is
  paid once per host and never once per catalogue.

And one thing must **not** change: a schema this host never refused is sent
byte for byte as it always was, and a refusal the sweep cannot fix still
reaches routing as the same ``model_rejected`` that falls through to the next
model today.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from my_claude_code.core.anthropic.models import Message, MessagesRequest, Tool
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
    build_chatgpt_oauth_request_body,
)
from my_claude_code.providers.chatgpt_oauth.provider import CHATGPT_OAUTH_DEFAULT_BASE
from my_claude_code.providers.openai_chat.responses_transport import ResponsesTransport
from my_claude_code.providers.openai_responses import PERMISSIVE_TOOL_SCHEMA_DIALECT
from my_claude_code.providers.recovery import (
    DROPPABLE_KEYWORDS,
    FACT_RESPONSES_TOOL_SCHEMA_KEYWORD,
    PROVIDER_WIDE_MODEL_ID,
    REGEX_CONSTRUCTS,
    RUNG_TOOL_SCHEMA,
    STATED_FACT_TTL_SECONDS,
    LearnedFactStore,
    SchemaKeywordRefusal,
    prune_tool_catalogue,
    refusal_from_detail,
    rejected_tool_schema_keyword,
    tool_schema_recovery,
)
from my_claude_code.providers.recovery import store as learned_store
from my_claude_code.providers.recovery.tool_schema_refusal import (
    construct_named_by,
    pattern_uses,
)
from tests.providers.support import (
    ImmediateRetryProviderRateLimiter,
    passthrough_rate_limiter,
)

HERE = Path(__file__).parent
PROVIDER = "muse_gateway"
REASONING = ReasoningPolicy.on()

#: The real property, verbatim from the recovered session catalogue.
VIDEO_SCALE_PATTERN = r"^(?:[1-9]\d{0,4}|-[12]):(?:[1-9]\d{0,4}|-[12])(?![\s\S])"
OFFENDING_TOOL = "mcp__appium-mcp__appium_screen_recording"

#: The two patterns from the same catalogue that this validator accepted, and
#: which therefore must survive every sweep below.
INNOCENT_PATTERNS = (r"^[^\n\r]*$", r"^[\s\S]{0,300}$")

#: The offending pattern as it appears *on the wire*, where every backslash of
#: the Python literal has become two. Asserting the literal against serialised
#: JSON would silently never match and prove nothing.
VIDEO_SCALE_ON_THE_WIRE = json.dumps(VIDEO_SCALE_PATTERN)[1:-1]


def _wire(body: Any) -> str:
    """One body as it is actually serialised, backslashes and all."""

    return json.dumps(body)


def _wire_params(trace: Any) -> dict[str, Any]:
    """The params of the last body this trace recorded.

    ``WireTrace.requests`` is keyed by attempt number rather than a list: the
    point of the record is which try carried which body.
    """

    return trace.requests[max(trace.requests)].params


# --------------------------------------------------------------------------
# The two real wordings, and the errors that carry them
# --------------------------------------------------------------------------

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

CHAT_REFUSAL = {
    "error": {
        "message": (
            "Invalid schema for function 'Artifact': "
            "'^(?!__.*__$)[^\\p{Cc}\\p{Cf}]{1,200}$' is not a 'regex'."
        ),
        "type": "invalid_request_error",
        "param": "tools",
        "code": "invalid_function_parameters",
    }
}


def _error(payload: Any, status: int = 400) -> httpx.HTTPStatusError:
    """An error shaped exactly like either Responses sender raises."""

    request = httpx.Request("POST", "https://upstream.invalid/responses")
    response = httpx.Response(status, request=request, json=payload)
    return httpx.HTTPStatusError(
        f"Responses API error {status}", request=request, response=response
    )


def _schema(pattern: str = VIDEO_SCALE_PATTERN) -> dict[str, Any]:
    """The offending tool's schema, as the client actually sends it."""

    return {
        "type": "object",
        "$schema": "http://json-schema.org/draft-07/schema#",
        "properties": {
            "action": {"type": "string", "enum": ["start", "stop"]},
            "videoScale": {
                "description": "iOS only. Width:height, each 1-16384.",
                "type": "string",
                "pattern": pattern,
            },
        },
        "required": ["action"],
    }


def _tools(*, offending: bool = True) -> list[dict[str, Any]]:
    """A Responses tool array: one plain tool, one that offends or does not."""

    return [
        {
            "type": "function",
            "name": "SendMessage",
            "description": "send",
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {
                        "allOf": [
                            {"type": "string", "pattern": INNOCENT_PATTERNS[0]},
                            {"type": "string", "pattern": INNOCENT_PATTERNS[1]},
                        ]
                    }
                },
            },
        },
        {
            "type": "function",
            "name": OFFENDING_TOOL,
            "description": "record",
            "parameters": _schema(VIDEO_SCALE_PATTERN if offending else r"^\d+:\d+$"),
        },
    ]


# --------------------------------------------------------------------------
# The matcher: both wordings, and everything it must refuse to answer
# --------------------------------------------------------------------------


def test_the_responses_wording_names_the_keyword_and_the_construct() -> None:
    refusal = rejected_tool_schema_keyword(_error(RESPONSES_REFUSAL))
    assert refusal is not None
    assert refusal.keyword == "pattern"
    assert refusal.construct is not None
    assert refusal.construct.name == "lookaround"
    assert refusal.detail == "pattern:lookaround"


def test_the_chat_wording_names_no_path_and_falls_back_to_every_pattern() -> None:
    """``Invalid schema for function 'Artifact': '…' is not a 'regex'.``

    No path, no construct named. All this proves is "a regex keyword is
    wrong", so every ``pattern`` goes -- and nothing else does, because the
    same validator accepted the catalogue's 134 ``$schema`` keys and 33
    ``minimum`` keys on the same request.
    """

    refusal = rejected_tool_schema_keyword(_error(CHAT_REFUSAL))
    assert refusal is not None
    assert refusal.keyword == "pattern"
    assert refusal.construct is None
    assert refusal.detail == "pattern:*"


def test_an_echoed_request_body_carrying_the_same_words_does_not_match() -> None:
    """The lesson ``complaint.py`` exists for, restated for this rung.

    A pydantic-style validator echoes the whole submitted request back, and
    *this* request contains the word ``invalid_json_schema`` (in a tool
    description), the word ``tools`` and the offending regex itself. Reading
    the error as one flat string would answer an unrelated 400 by deleting
    every pattern in the catalogue.
    """

    echoed = {
        "detail": [
            {
                "type": "value_error",
                "msg": "Validation: top_p is immutable for this model",
                "input": {
                    "tools": _tools(),
                    "system": (
                        "Invalid JSON schema: regex lookaround is not supported. "
                        "Found at $.properties.videoScale.pattern. "
                        "Invalid schema for function 'Artifact'."
                    ),
                },
            }
        ]
    }
    assert rejected_tool_schema_keyword(_error(echoed)) is None


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {
                "error": {
                    "param": "name",
                    "type": "invalid_request_error",
                    "message": "`name` must be at most 64 characters, got 68",
                }
            },
            id="tool-name-length",
        ),
        pytest.param(
            {
                "error": {
                    "param": "tool_choice",
                    "type": "invalid_request_error",
                    "message": 'only `"auto"` is supported for `tool_choice`',
                }
            },
            id="tool-choice-auto-only",
        ),
        pytest.param(
            {
                "error": {
                    "param": "tools",
                    "code": "array_above_max_length",
                    "message": (
                        "Invalid 'tools': array too long. Expected an array "
                        "with maximum length 128"
                    ),
                }
            },
            id="tools-count-cap",
        ),
        pytest.param(
            {
                "error": {
                    "message": "Validation: top_p is immutable for this model",
                    "type": "invalid_request_error",
                }
            },
            id="sampling-knob",
        ),
    ],
)
def test_another_hosts_400_is_left_for_another_rung(payload: dict[str, Any]) -> None:
    assert rejected_tool_schema_keyword(_error(payload)) is None


def test_a_non_400_is_never_this_rungs_business() -> None:
    assert rejected_tool_schema_keyword(_error(RESPONSES_REFUSAL, status=500)) is None


def test_a_path_naming_another_keyword_drops_that_keyword_everywhere() -> None:
    payload = {
        "error": {
            "message": (
                "Invalid JSON schema: 'uri-reference' is not a supported "
                "format. Found at $.properties.target.format."
            ),
            "type": "invalid_request_error",
            "param": "tools",
            "code": "invalid_json_schema",
        }
    }
    refusal = rejected_tool_schema_keyword(_error(payload))
    assert refusal is not None
    # The host named a keyword whose value is not a regex, so no construct
    # narrows it: every occurrence of ``format`` goes.
    assert (refusal.keyword, refusal.construct) == ("format", None)


def test_a_path_whose_tail_is_a_property_name_falls_back_to_pattern() -> None:
    """``$.properties.videoScale`` names a property, not a keyword.

    Reading a tail as a keyword because it sits where one would is how a sweep
    starts deleting a client's own property called ``format``.
    """

    payload = {
        "error": {
            "message": (
                "Invalid JSON schema: regex lookaround is not supported. "
                "Found at $.properties.videoScale."
            ),
            "type": "invalid_request_error",
            "param": "tools",
            "code": "invalid_json_schema",
        }
    }
    refusal = rejected_tool_schema_keyword(_error(payload))
    assert refusal is not None
    assert refusal.keyword == "pattern"
    assert refusal.construct is not None and refusal.construct.name == "lookaround"


# --------------------------------------------------------------------------
# The construct table
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "words", "carrier", "innocent"),
    [
        ("lookaround", "regex lookaround is not supported", r"a(?=b)", r"a\(\?=b"),
        ("lookaround", "lookbehind is not supported", r"(?<=a)b", r"[a-z]+"),
        ("backreference", "backreferences are not supported", r"(a)\1", r"\d{2}"),
        (
            "unicode_property",
            "unicode property escapes are not supported",
            r"[\p{Cc}]",
            r"[[:cntrl:]]",
        ),
        ("possessive", "possessive quantifiers are not supported", r"a*+", r"a*"),
        (
            "named_group",
            "named groups are not supported",
            r"(?<year>\d{4})",
            r"(\d{4})",
        ),
        ("inline_flags", "inline flags are not supported", r"(?i)abc", r"[aA]bc"),
        ("text_anchor", "the \\z anchor is not supported", r"abc\z", r"abc$"),
    ],
)
def test_each_construct_is_named_by_the_host_and_found_in_a_pattern(
    name: str, words: str, carrier: str, innocent: str
) -> None:
    """Both halves of one table row, declared together and proven together.

    A construct MCC can recognise in a host's words but not find in a pattern
    would sweep every pattern in the catalogue, so neither half is allowed to
    drift from the other.
    """

    construct = construct_named_by(words)
    assert construct is not None and construct.name == name
    assert pattern_uses(carrier, construct)
    assert not pattern_uses(innocent, construct)


def test_a_lookbehind_is_not_read_as_a_named_group() -> None:
    """``(?<`` opens both; only the next character says which."""

    named = next(c for c in REGEX_CONSTRUCTS if c.name == "named_group")
    assert not pattern_uses(r"(?<=a)b", named)
    assert not pattern_uses(r"(?<!a)b", named)
    assert pattern_uses(r"(?<n>a)", named)


def test_every_construct_name_round_trips_through_a_stored_detail() -> None:
    for construct in REGEX_CONSTRUCTS:
        refusal = SchemaKeywordRefusal(keyword="pattern", construct=construct)
        assert refusal_from_detail(refusal.detail) == refusal
    assert refusal_from_detail("pattern:*") == SchemaKeywordRefusal(keyword="pattern")


@pytest.mark.parametrize("detail", ["", "pattern", "type:lookaround", "pattern:nope"])
def test_a_detail_no_version_of_this_code_wrote_is_not_acted_on(detail: str) -> None:
    assert refusal_from_detail(detail) is None


def test_the_droppable_keywords_are_constraints_and_never_structure() -> None:
    """A tool keeps everything that tells the model how to call it."""

    structural = {
        "type",
        "properties",
        "required",
        "items",
        "anyOf",
        "oneOf",
        "allOf",
        "description",
        "enum",
        "additionalProperties",
        "$defs",
        "definitions",
        "$ref",
    }
    assert not structural & set(DROPPABLE_KEYWORDS.values())


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------

LOOKAROUND = SchemaKeywordRefusal(
    keyword="pattern",
    construct=next(c for c in REGEX_CONSTRUCTS if c.name == "lookaround"),
)
EVERY_PATTERN = SchemaKeywordRefusal(keyword="pattern")


def test_only_the_offending_pattern_leaves_the_catalogue() -> None:
    tools = _tools()
    pruned, removals = prune_tool_catalogue(tools, LOOKAROUND)

    assert [removal.tool for removal in removals] == [OFFENDING_TOOL]
    assert removals[0].path == "$.properties.videoScale.pattern"
    assert removals[0].keyword == "pattern"

    video = pruned[1]["parameters"]["properties"]["videoScale"]
    assert "pattern" not in video
    # The property, its type and its description survive: the model still
    # knows the field and what it is for.
    assert video["type"] == "string"
    assert video["description"].startswith("iOS only.")
    # And every innocent pattern in the same catalogue is untouched.
    assert [
        branch["pattern"]
        for branch in pruned[0]["parameters"]["properties"]["to"]["allOf"]
    ] == list(INNOCENT_PATTERNS)


def test_a_catalogue_that_does_not_offend_comes_back_by_identity() -> None:
    """The byte-equality contract, at the level that decides the bytes."""

    tools = _tools(offending=False)
    pruned, removals = prune_tool_catalogue(tools, LOOKAROUND)
    assert pruned is tools
    assert removals == ()


def test_the_sweep_never_mutates_the_objects_it_was_handed() -> None:
    """The converted tools are the client's own dicts and are shared.

    ``_convert_tools`` copies ``parameters`` by reference, and one route hands
    the same catalogue to every model it tries. A sweep that mutated in place
    would corrupt the *next* model's request, which would look like a
    completely unrelated provider bug.
    """

    tools = _tools()
    before = json.dumps(tools, sort_keys=True)
    pruned, _ = prune_tool_catalogue(tools, LOOKAROUND)
    assert json.dumps(tools, sort_keys=True) == before
    assert pruned is not tools
    # The untouched tool is the very same object, not a copy of one.
    assert pruned[0] is tools[0]


def test_the_sweep_reaches_every_nested_schema_position() -> None:
    nested = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "pattern": r"x(?=y)"},
            # A *property* called "pattern" is a property, not a keyword.
            "pattern": {"type": "string", "pattern": r"q(?!r)"},
        },
        "items": {"pattern": r"i(?=j)"},
        "prefixItems": [{"pattern": r"p(?<=q)"}],
        "anyOf": [{"oneOf": [{"allOf": [{"pattern": r"n(?!m)"}]}]}],
        "$defs": {"d": {"pattern": r"d(?=e)"}},
        "definitions": {"e": {"pattern": r"e(?=f)"}},
        "additionalProperties": {"pattern": r"ap(?=x)"},
        "patternProperties": {"^k$": {"pattern": r"pp(?=x)"}},
        "propertyNames": {"pattern": r"pn(?=x)"},
        "not": {"pattern": r"no(?=x)"},
        "if": {"pattern": r"if(?=x)"},
        "then": {"pattern": r"th(?=x)"},
        "else": {"pattern": r"el(?=x)"},
        "contains": {"pattern": r"co(?=x)"},
        "dependentSchemas": {"s": {"pattern": r"ds(?=x)"}},
    }
    tools = [{"type": "function", "name": "deep", "parameters": nested}]
    pruned, removals = prune_tool_catalogue(tools, LOOKAROUND)

    assert len(removals) == 16
    # No keyword survives anywhere. The one remaining ``pattern`` token in the
    # serialised schema is the *property* named ``pattern``, whose value is
    # now a bare ``{"type": "string"}``.
    assert "(?" not in json.dumps(pruned)
    assert json.dumps(pruned).count('"pattern"') == 1
    # The map keys are names, never keywords: the patternProperties key
    # survives as the key it is.
    assert "^k$" in pruned[0]["parameters"]["patternProperties"]
    # The property literally called "pattern" is still a property.
    assert "pattern" in pruned[0]["parameters"]["properties"]
    assert set(pruned[0]["parameters"]) == set(nested)


def test_the_path_free_fallback_drops_every_pattern_and_nothing_else() -> None:
    tools = _tools()
    pruned, removals = prune_tool_catalogue(tools, EVERY_PATTERN)
    assert len(removals) == 3
    assert '"pattern"' not in json.dumps(pruned)
    # Nothing else moved: $schema, enum, required, type, description all stay.
    assert pruned[1]["parameters"]["$schema"].startswith("http")
    assert pruned[1]["parameters"]["required"] == ["action"]
    assert pruned[1]["parameters"]["properties"]["action"]["enum"] == ["start", "stop"]


def test_a_keyword_the_catalogue_does_not_carry_is_not_a_recovery() -> None:
    """Removing nothing cannot be what fixes a 400, so it is not retried."""

    body = {"model": "m", "tools": _tools(offending=False)}
    assert tool_schema_recovery(_error(RESPONSES_REFUSAL), body) is None
    assert tool_schema_recovery(_error(RESPONSES_REFUSAL), {"model": "m"}) is None


def test_the_wire_marker_names_tools_and_paths_and_never_schema_bodies() -> None:
    recovery = tool_schema_recovery(
        _error(RESPONSES_REFUSAL), {"model": "m", "tools": _tools()}
    )
    assert recovery is not None
    marker = recovery.marker["tool_schema_pruned"]
    assert marker == (
        "dropped pattern using lookaround from 1 tool: "
        f"{OFFENDING_TOOL} $.properties.videoScale.pattern"
    )
    # The regex itself is never recorded: a request row is not a place to
    # archive a client's own schema.
    assert VIDEO_SCALE_PATTERN not in marker


# --------------------------------------------------------------------------
# The rung on the shared Responses transport
# --------------------------------------------------------------------------


def _messages_request(*, long_name: bool = False) -> MessagesRequest:
    name = ("z" * 68) if long_name else OFFENDING_TOOL
    return MessagesRequest(
        model="muse-spark-1.3",
        max_tokens=64,
        messages=[Message(role="user", content="hi")],
        tools=[
            Tool(
                name="SendMessage", description="send", input_schema={"type": "object"}
            ),
            Tool(name=name, description="record", input_schema=_schema()),
        ],
    )


def _transport(store: LearnedFactStore, *, limiter: Any = None) -> ResponsesTransport:
    # A host that declares its validator refuses nothing: since 7.38.0 the
    # Responses default sweeps lookaround before the first send, so the only
    # way a lookaround still reaches the wire -- which is what the rung exists
    # to answer -- is a host whose declared dialect does not cover it.
    return ResponsesTransport(
        ProviderConfig(api_key="sk-test", base_url="https://example.invalid/v1"),
        base_url="https://example.invalid/v1",
        provider_name="MUSE",
        identity=None,
        api_key=None,
        # The ladder rows this file reads are written by the limiter's retry
        # frame, so the test that asserts them needs the real one.
        rate_limiter=limiter or passthrough_rate_limiter(),
        tool_name_max_length=None,
        memory=store.memory_for(PROVIDER),
        tool_schema_dialect=PERMISSIVE_TOOL_SCHEMA_DIALECT,
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


def _refuses_lookaround(attempt: int, body: dict[str, Any]) -> httpx.Response:
    """A fake upstream with the real validator's one rule."""

    if any("(?!" in json.dumps(tool.get("parameters", {})) for tool in body["tools"]):
        return httpx.Response(400, json=RESPONSES_REFUSAL)
    return _accepted()


@pytest.mark.asyncio
async def test_the_transport_sweeps_retries_once_and_remembers() -> None:
    store = LearnedFactStore()
    transport = _transport(store, limiter=ImmediateRetryProviderRateLimiter())
    trace = install_wire_trace()
    install_ladder_trace()

    sent = _install(transport, _refuses_lookaround)
    events = await _run(transport, _messages_request())
    await transport.aclose()

    assert len(sent) == 2
    assert VIDEO_SCALE_ON_THE_WIRE in _wire(sent[0])
    assert "pattern" not in _wire(sent[1])
    # Everything else about the catalogue is the same array of the same tools.
    assert [tool["name"] for tool in sent[1]["tools"]] == [
        tool["name"] for tool in sent[0]["tools"]
    ]
    assert any("ok" in event for event in events)

    ladder = current_ladder()
    assert ladder is not None
    rows = ladder_payload(ladder.slot())["tries"]
    assert [row.get("status") for row in rows] == [400, None]
    assert "recovery" not in rows[0]
    assert rows[1]["recovery"] == RUNG_TOOL_SCHEMA
    assert OFFENDING_TOOL in _wire_params(trace)["tool_schema_pruned"]

    facts = store.facts_for_provider(PROVIDER)
    assert [(f.fact_kind, f.model_id, f.detail, f.value) for f in facts] == [
        (
            FACT_RESPONSES_TOOL_SCHEMA_KEYWORD,
            PROVIDER_WIDE_MODEL_ID,
            "pattern:lookaround",
            True,
        )
    ]
    assert facts[0].source == "rejection"
    assert facts[0].ttl_seconds == STATED_FACT_TTL_SECONDS
    assert "lookaround" in facts[0].evidence


@pytest.mark.asyncio
async def test_the_next_request_is_already_clean_and_costs_one_call() -> None:
    store = LearnedFactStore()
    first = _transport(store)
    _install(first, _refuses_lookaround)
    await _run(first, _messages_request())
    await first.aclose()

    second = _transport(store)
    trace = install_wire_trace()
    sent = _install(second, _refuses_lookaround)
    await _run(second, _messages_request())
    await second.aclose()

    assert len(sent) == 1
    assert "pattern" not in _wire(sent[0])
    assert "learned from this host" in _wire_params(trace)["tool_schema_pruned"]


@pytest.mark.asyncio
async def test_a_restart_reloads_the_fact_and_a_forget_re_pays_the_400(
    tmp_path: Path,
) -> None:
    path = tmp_path / "learned_facts.json"
    store = LearnedFactStore(path=path)
    transport = _transport(store)
    _install(transport, _refuses_lookaround)
    await _run(transport, _messages_request())
    await transport.aclose()
    assert store.flush()
    assert path.exists()

    # A restart: a brand-new store reading the same file, exactly as
    # ``runtime/application.py`` builds one.
    restarted = LearnedFactStore()
    restarted.enable_persistence(path)
    after_restart = _transport(restarted)
    sent = _install(after_restart, _refuses_lookaround)
    await _run(after_restart, _messages_request())
    await after_restart.aclose()
    assert len(sent) == 1

    # Forget reaches the live provider, not only the page: the memory is
    # rebuilt in place, so the very next request pays the 400 again.
    assert restarted.forget(
        PROVIDER, PROVIDER_WIDE_MODEL_ID, FACT_RESPONSES_TOOL_SCHEMA_KEYWORD
    )
    forgotten = _transport(restarted)
    sent = _install(forgotten, _refuses_lookaround)
    await _run(forgotten, _messages_request())
    await forgotten.aclose()
    assert len(sent) == 2


@pytest.mark.asyncio
async def test_a_host_that_refuses_the_swept_catalogue_too_fails_unchanged() -> None:
    """The binding non-decision: the 400 keeps falling through.

    ``model_rejected`` is what lets routing try the next model, and this user's
    ``FALLBACK_SKIP_KINDS=invalid_request`` would end the route at tier one if
    a schema refusal were ever reclassified. A rung that cannot fix it must
    leave the failure exactly as it found it.
    """

    store = LearnedFactStore()
    transport = _transport(store)
    install_ladder_trace()

    def always_refuses(attempt: int, body: dict[str, Any]) -> httpx.Response:
        return httpx.Response(400, json=RESPONSES_REFUSAL)

    sent = _install(transport, always_refuses)
    with pytest.raises(ExecutionFailure) as caught:
        await _run(transport, _messages_request())
    await transport.aclose()

    # Exactly two sends: the rung fires once and is never offered again.
    assert len(sent) == 2
    assert caught.value.kind.value == "model_rejected"
    assert caught.value.status_code == 400
    # And nothing was learned from a rewrite that fixed nothing.
    assert store.facts_for_provider(PROVIDER) == ()


@pytest.mark.asyncio
async def test_a_request_needing_name_length_and_schema_recovery_succeeds() -> None:
    """Two rungs, two rewrites, one request -- each firing at most once."""

    store = LearnedFactStore()
    transport = _transport(store)
    request = _messages_request(long_name=True)

    def handler(attempt: int, body: dict[str, Any]) -> httpx.Response:
        if any(
            "(?!" in json.dumps(tool.get("parameters", {})) for tool in body["tools"]
        ):
            return httpx.Response(400, json=RESPONSES_REFUSAL)
        if any(len(tool["name"]) > 64 for tool in body["tools"]):
            return httpx.Response(
                400,
                json={
                    "error": {
                        "param": "name",
                        "type": "invalid_request_error",
                        "message": "`name` must be at most 64 characters, got 68",
                    }
                },
            )
        return _accepted()

    sent = _install(transport, handler)
    events = await _run(transport, request)
    await transport.aclose()

    assert len(sent) == 3
    assert "pattern" not in _wire(sent[2])
    assert all(len(tool["name"]) <= 64 for tool in sent[2]["tools"])
    assert any("ok" in event for event in events)
    # The alias survived the schema sweep and the sweep survived the aliasing:
    # both rewrites are in the final body.
    assert {f.fact_kind for f in store.facts_for_provider(PROVIDER)} == {
        FACT_RESPONSES_TOOL_SCHEMA_KEYWORD,
        "responses_tool_name_max_length",
    }


@pytest.mark.asyncio
async def test_a_host_that_never_refuses_sends_the_bytes_it_always_did() -> None:
    """The equality contract, on the sender rather than on the pruner."""

    store = LearnedFactStore()
    transport = _transport(store)
    request = _messages_request()
    baseline, _ = transport.build_body(
        request, reasoning=REASONING, max_output_tokens=512
    )

    sent = _install(transport, lambda attempt, body: _accepted())
    await _run(transport, request)
    await transport.aclose()

    assert len(sent) == 1
    assert _wire(sent[0]) == _wire(baseline)
    assert VIDEO_SCALE_ON_THE_WIRE in _wire(sent[0])


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
    # See ``_transport``: the rung is exercised on a host whose declared
    # dialect lets the lookaround through.
    provider._tool_schema_dialect = PERMISSIVE_TOOL_SCHEMA_DIALECT
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
async def test_chatgpt_oauth_answers_the_same_refusal_on_its_own_ladder() -> None:
    provider = _chatgpt_provider(limiter=ImmediateRetryProviderRateLimiter())
    trace = install_wire_trace()
    install_ladder_trace()
    provider._send_stream_request = AsyncMock(
        side_effect=[_error(RESPONSES_REFUSAL), _success()]
    )

    chunks = await _drain(provider, _messages_request())

    assert any("ok" in chunk for chunk in chunks)
    calls = provider._send_stream_request.await_args_list
    assert len(calls) == 2
    assert VIDEO_SCALE_ON_THE_WIRE in _wire(calls[0].kwargs["body"])
    assert "pattern" not in _wire(calls[1].kwargs["body"])
    assert OFFENDING_TOOL in _wire_params(trace)["tool_schema_pruned"]
    ladder = current_ladder()
    assert ladder is not None
    rows = ladder_payload(ladder.slot())["tries"]
    assert rows[-1]["recovery"] == RUNG_TOOL_SCHEMA

    facts = learned_store.learned_fact_store().facts_for_provider("chatgpt_oauth")
    assert [(f.fact_kind, f.detail) for f in facts] == [
        (FACT_RESPONSES_TOOL_SCHEMA_KEYWORD, "pattern:lookaround")
    ]


@pytest.mark.asyncio
async def test_chatgpt_oauths_second_request_is_swept_before_the_first_send() -> None:
    first = _chatgpt_provider()
    first._send_stream_request = AsyncMock(
        side_effect=[_error(RESPONSES_REFUSAL), _success()]
    )
    await _drain(first, _messages_request())

    second = _chatgpt_provider()
    second._send_stream_request = AsyncMock(side_effect=[_success()])
    await _drain(second, _messages_request())

    calls = second._send_stream_request.await_args_list
    assert len(calls) == 1
    assert "pattern" not in _wire(calls[0].kwargs["body"])


@pytest.mark.asyncio
async def test_chatgpt_oauth_keeps_its_reasoning_rung_and_its_order() -> None:
    """6.33.0's rung is untouched: a reasoning 400 is still answered by it."""

    provider = _chatgpt_provider()
    request = MessagesRequest(
        model="gpt-5",
        max_tokens=64,
        messages=[Message(role="user", content="hi")],
    )
    reasoning_400 = httpx.HTTPStatusError(
        "error 400",
        request=httpx.Request("POST", "https://upstream.invalid/responses"),
        response=httpx.Response(
            400,
            json={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "reasoning: Extra inputs are not permitted",
                },
            },
        ),
    )
    provider._send_stream_request = AsyncMock(side_effect=[reasoning_400, _success()])

    chunks = await _drain(provider, request)

    assert any("ok" in chunk for chunk in chunks)
    calls = provider._send_stream_request.await_args_list
    assert "reasoning" in calls[0].kwargs["body"]
    assert "reasoning" not in calls[1].kwargs["body"]


@pytest.mark.asyncio
async def test_chatgpt_oauth_keeps_its_fall_through_when_the_sweep_does_not_help() -> (
    None
):
    provider = _chatgpt_provider()
    install_ladder_trace()
    provider._send_stream_request = AsyncMock(
        side_effect=[_error(RESPONSES_REFUSAL), _error(RESPONSES_REFUSAL)]
    )

    with pytest.raises(ExecutionFailure) as caught:
        await _drain(provider, _messages_request())

    assert len(provider._send_stream_request.await_args_list) == 2
    assert caught.value.kind.value == "model_rejected"
    assert learned_store.learned_fact_store().facts_for_provider("chatgpt_oauth") == ()


@pytest.mark.asyncio
async def test_a_chatgpt_oauth_body_with_nothing_to_sweep_is_byte_identical() -> None:
    """The equality contract on the surface that has a byte golden.

    With no learned fact and no offending keyword, the first body this
    provider sends is character for character the one
    ``build_chatgpt_oauth_request_body`` produces on its own -- which is what
    ``chatgpt_oauth_long_tool_names_golden.json`` pins, unmodified by this
    change.
    """

    request = _messages_request()
    baseline = build_chatgpt_oauth_request_body(
        request,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
        tool_schema_dialect=PERMISSIVE_TOOL_SCHEMA_DIALECT,
    )

    provider = _chatgpt_provider()
    provider._send_stream_request = AsyncMock(side_effect=[_success()])
    await _drain(provider, request)

    sent = provider._send_stream_request.await_args_list[0].kwargs["body"]
    assert _wire(sent) == _wire(baseline)
    assert VIDEO_SCALE_ON_THE_WIRE in _wire(sent)
