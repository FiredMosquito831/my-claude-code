"""Zen's free tier from every coding agent, not only Claude Code.

The defect (7.28.0 -> 7.49.0): the free tier reads the request's tool names,
and MCC translated only Claude Code's five (``Bash/Read/Edit/Glob/Grep``). A
client that already spoke OpenCode's own spellings was worse off than one that
spoke none: the collision rule that keeps a stray ``bash`` from shadowing
Claude Code's ``Bash`` hashed the client's *correct* ``bash`` to
``bash_37d2b12d5d9abc2a``, so OpenCode's own catalogue reached OpenCode's free
tier with zero of its names (``specs/PR-ZEN-FREE-TIER-ALL-HARNESSES-SPEC.md``
§3.4, measured on a scratch wire).

What these tests hold:

* a client's spellings are declared as data -- one :class:`ToolFamily` per
  client, with where they were read -- and chosen per request by the names the
  request carries;
* OpenCode's and Pi's own catalogues keep their spellings on all three doors;
* Claude Code, a Claude Code sub-request, a tool-less request, a paid Zen
  model, OpenCode Go, NVIDIA NIM and ChatGPT OAuth send the bytes 7.49.0 sent,
  compared with a golden written by the 7.49.0 code itself;
* the codec is untouched: under the static catalogue a lone ``bash`` is still
  aliased, exactly as ``test_opencode_free_tier_catalogue.py`` says;
* every name round-trips on Chat Completions, Responses and Messages.
"""

import asyncio
import functools
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.config.settings import get_settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.openai_tool_names import (
    OpenAIToolNameCodec,
    request_tool_names,
)
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.providers.openai_chat.opencode_catalogue import (
    CLAUDE_CODE_FAMILY,
    OPENCODE_BUILTIN_TOOL_NAMES,
    OPENCODE_FREE_TIER_CATALOGUE,
    OPENCODE_TOOL_CATALOGUE,
    OPENCODE_TOOL_FAMILIES,
    ToolFamily,
    select_tool_family,
)
from my_claude_code.providers.openai_chat.tool_calls import OpenAIToolCallAssembler
from my_claude_code.providers.openai_responses import (
    ResponsesStreamConverter,
    responses_tool_name_codec,
)
from tests.providers.opencode_family_bodies import (
    CLAUDE_FIVE,
    FREE,
    OPENCODE_NATIVE,
    PAID,
    REASONING,
    canonical,
    golden_cases,
    opencode_bodies,
    opencode_provider,
    tool_request,
)

GOLDEN = json.loads(
    (Path(__file__).parent / "opencode_harness_families_golden.json").read_text(
        encoding="utf-8"
    )
)
OPENCODE_FIVE = frozenset({"bash", "read", "edit", "glob", "grep"})
#: Pi's default tools (``@earendil-works/pi-coding-agent`` 0.82.1,
#: ``dist/core/sdk.js:132``).
PI_DEFAULTS = ["read", "bash", "edit", "write"]
CLAUDE_CASES = ("claude_code_free", "claude_code_sub_request_free", "tool_less_free")


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "OPENCODE_CLIENT_IDENTITY",
        "OPENCODE_FREE_TIER_MODELS",
        "OPENCODE_FREE_TIER_CREDENTIAL",
    ):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _names(body: dict[str, Any]) -> list[str]:
    return [
        tool.get("name") or tool["function"]["name"] for tool in body.get("tools", [])
    ]


def _family(name: str) -> ToolFamily:
    return next(family for family in OPENCODE_TOOL_FAMILIES if family.name == name)


# -- the fix: a client's own OpenCode spellings survive ------------------------


@pytest.mark.parametrize("surface", ["chat", "responses", "messages"])
def test_opencode_own_catalogue_keeps_its_five_spellings(surface: str) -> None:
    """OpenCode 1.18.32's catalogue encodes to itself: 5 of 5, nothing hashed.

    On 7.49.0 this was 0 of 5 -- ``bash_37d2b12d5d9abc2a`` and four more.
    """

    request = tool_request(FREE, OPENCODE_NATIVE, history=["bash", "read"])
    body = opencode_bodies("opencode", request)[surface]
    assert _names(body) == OPENCODE_NATIVE
    assert set(_names(body)) >= OPENCODE_FIVE


@pytest.mark.parametrize("surface", ["chat", "responses", "messages"])
def test_pi_default_catalogue_keeps_read_bash_edit(surface: str) -> None:
    body = opencode_bodies("opencode", tool_request(FREE, PI_DEFAULTS))[surface]
    assert _names(body) == PI_DEFAULTS


def test_the_replayed_history_keeps_the_spelling_too() -> None:
    """A tool_use the client replays must name a tool the catalogue lists."""

    request = tool_request(FREE, OPENCODE_NATIVE, history=["bash"])
    chat = opencode_bodies("opencode", request)["chat"]
    calls = [
        call["function"]["name"]
        for message in chat["messages"]
        for call in message.get("tool_calls") or ()
    ]
    assert calls == ["bash"]


def test_a_lone_lowercase_bash_under_the_static_catalogue_is_still_aliased() -> None:
    """The 7.28.0 golden, restated: the codec itself did not change.

    Through the provider, ``bash`` beside Claude Code's ``Bash`` is still
    aliased away (the two families tie on one role and Claude Code's is
    declared first); only a request whose *own* catalogue is OpenCode's keeps
    it.
    """

    codec = OpenAIToolNameCodec.from_names(["bash"], catalogue=OPENCODE_TOOL_CATALOGUE)
    assert codec.encode("bash") == "bash_37d2b12d5d9abc2a"

    for names in (["Bash", "bash"], [*CLAUDE_FIVE, "bash", "READ"]):
        body = opencode_bodies("opencode", tool_request(FREE, names))["responses"]
        wire = _names(body)
        assert wire[0] == "bash"
        assert "bash_37d2b12d5d9abc2a" in wire
        folded = [name.casefold() for name in wire]
        assert len(folded) == len(set(folded))


# -- byte-identical where it must be -------------------------------------------


def _golden(case: str, surface: str) -> Any:
    return GOLDEN["cases"][case][surface]


@functools.cache
def _now() -> dict[str, dict[str, Any]]:
    """This code's bodies for every golden case, built once per session."""

    return dict(golden_cases())


@pytest.mark.parametrize(
    ("case", "surface"),
    [
        (case, surface)
        for case in CLAUDE_CASES
        for surface in ("chat", "responses", "messages")
    ],
)
def test_claude_code_request_is_byte_identical_to_7_49_0(
    case: str, surface: str
) -> None:
    """Compared with bodies the 7.49.0 code wrote, not with this code's own.

    Claude Code's 105-tool request with replayed calls, its one-tool
    sub-request and a tool-less side request, on all three doors.
    """

    now = _now()[case][surface]
    assert canonical(now) == canonical(_golden(case, surface))


@pytest.mark.parametrize(
    ("case", "surface"),
    [
        (case, surface)
        for case, bodies in GOLDEN["cases"].items()
        if case not in CLAUDE_CASES
        for surface in bodies
    ],
)
def test_paid_model_and_other_providers_unchanged(case: str, surface: str) -> None:
    """OpenCode's own spellings sent where the free-tier catalogue never applies.

    A paid Zen model, an OpenCode Go model, NVIDIA NIM and ChatGPT OAuth: each
    sends exactly the body 7.49.0 sent for the same request.
    """

    now = _now()[case][surface]
    assert canonical(now) == canonical(_golden(case, surface))


def test_the_golden_was_written_by_the_release_before() -> None:
    assert GOLDEN["generated_from"] == "v7.49.0 3fdbca22"
    assert set(GOLDEN["cases"]) == {case for case, _bodies in golden_cases()}


def test_the_paid_catalogue_is_still_empty_whatever_the_names() -> None:
    provider = opencode_provider("opencode")
    for names in (OPENCODE_NATIVE, CLAUDE_FIVE, PI_DEFAULTS):
        assert provider.tool_catalogue_for_request(tool_request(PAID, names)) == {}
    # The per-model answer the older goldens read is untouched.
    assert dict(provider.tool_catalogue_for(FREE)) == dict(OPENCODE_TOOL_CATALOGUE)


# -- stable across turns ---------------------------------------------------------


def test_family_choice_is_stable_across_turns() -> None:
    """Turn N+1 adds an MCP tool and a replayed call; nothing already sent moves."""

    turn_n = tool_request(FREE, OPENCODE_NATIVE)
    turn_n1 = tool_request(
        FREE, [*OPENCODE_NATIVE, "mcp__exa__web_search_exa"], history=["bash"]
    )
    for surface in ("chat", "responses", "messages"):
        first = _names(opencode_bodies("opencode", turn_n)[surface])
        later = _names(opencode_bodies("opencode", turn_n1)[surface])
        assert later[: len(first)] == first

    families = [
        select_tool_family(request_tool_names(request), OPENCODE_TOOL_FAMILIES)
        for request in (turn_n, turn_n1)
    ]
    assert [family.name for family in families if family] == [
        "opencode_native",
        "opencode_native",
    ]


# -- the round trip -----------------------------------------------------------------


_MESSAGES_TOOL_SSE = (
    'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1",'
    '"type":"message","role":"assistant","model":"m","content":[],'
    '"stop_reason":null,"usage":{"input_tokens":3,"output_tokens":0}}}\n\n'
    'event: content_block_start\ndata: {"type":"content_block_start","index":0,'
    '"content_block":{"type":"tool_use","id":"toolu_9","name":"WIRE","input":{}}}\n\n'
    'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
    'event: message_delta\ndata: {"type":"message_delta","delta":'
    '{"stop_reason":"tool_use"},"usage":{"output_tokens":1}}\n\n'
    'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


async def _messages_call(provider: Any, request: MessagesRequest, wire: str) -> str:
    """Send one request through the Messages door; the fake model calls ``wire``."""

    sent: list[dict[str, Any]] = []

    def upstream(outbound: httpx.Request) -> httpx.Response:
        sent.append(json.loads(outbound.content))
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_MESSAGES_TOOL_SSE.replace("WIRE", wire).encode(),
            request=outbound,
        )

    transport = provider._messages
    inner: Any = transport._provider
    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    try:
        events = [
            event
            async for event in transport.stream(
                request, input_tokens=3, reasoning=REASONING
            )
        ]
    finally:
        await inner._client.aclose()
    assert wire in [tool["name"] for tool in sent[0]["tools"]]
    return "".join(events)


@pytest.mark.parametrize("family", OPENCODE_TOOL_FAMILIES, ids=lambda f: f.name)
def test_every_family_name_round_trips_on_chat_responses_and_messages(
    family: ToolFamily,
) -> None:
    """Each client spelling goes out as OpenCode's and comes back as the client's."""

    names = [*family.spellings, "Write", "mcp__exa__web_search_exa"]
    request = tool_request(FREE, names)
    provider = opencode_provider("opencode")
    catalogue = provider.tool_catalogue_for_request(request)
    assert catalogue, family.name

    chat_codec = provider.tool_name_codec(request)
    assert chat_codec is not None
    responses_codec = responses_tool_name_codec(
        request,
        provider._responses.tool_name_max_length,
        provider._responses.tool_catalogue(request),
    )
    assert responses_codec is not None

    for client in family.spellings:
        wire = chat_codec.encode(client)
        assert responses_codec.encode(client) == wire
        if client in catalogue:
            assert wire == catalogue[client]

        assembler = OpenAIToolCallAssembler(tool_names=chat_codec)
        chat_events = "".join(
            assembler.process_tool_call(
                {"index": 0, "id": "c1", "function": {"name": wire, "arguments": ""}},
                AnthropicStreamLedger("msg_1", "m"),
            )
        )
        assert f'"name":"{client}"' in chat_events.replace(" ", "")

        converter = ResponsesStreamConverter(
            AnthropicStreamLedger("msg_1", "m"), tool_names=responses_codec
        )
        responses_events = "".join(
            converter.feed(
                {
                    "type": "response.output_item.added",
                    "item": {"type": "function_call", "id": "c1", "name": wire},
                }
            )
        )
        assert f'"name":"{client}"' in responses_events.replace(" ", "")

        messages_events = asyncio.run(_messages_call(provider, request, wire))
        assert f'"name": "{client}"' in messages_events or (
            f'"name":"{client}"' in messages_events.replace(" ", "")
        )


# -- the opt-out and the data itself -------------------------------------------------


def test_opt_out_mcc_identity_disables_every_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCODE_CLIENT_IDENTITY", "mcc")
    get_settings.cache_clear()
    provider = opencode_provider("opencode")
    for family in OPENCODE_TOOL_FAMILIES:
        request = tool_request(FREE, list(family.spellings))
        assert provider.tool_catalogue_for_request(request) == {}, family.name
        assert (
            OPENCODE_FREE_TIER_CATALOGUE.catalogue_for_request(FREE, family.spellings)
            == {}
        )


def test_the_first_family_is_the_static_catalogue() -> None:
    """Claude Code's family is the 7.28.0 mapping itself, and it breaks ties."""

    first = OPENCODE_TOOL_FAMILIES[0]
    assert first.name == CLAUDE_CODE_FAMILY
    assert first.spellings is OPENCODE_TOOL_CATALOGUE
    # Chosen, it returns the static five whole -- present or not.
    for names in (CLAUDE_FIVE, ["Bash"], ["Bash", "bash"]):
        assert (
            OPENCODE_FREE_TIER_CATALOGUE.catalogue_for_request(FREE, names)
            is OPENCODE_TOOL_CATALOGUE
        )
    # Chosen by nobody, too: a request with no family's names is a no-op.
    assert (
        OPENCODE_FREE_TIER_CATALOGUE.catalogue_for_request(FREE, ["Write", "Task"])
        is OPENCODE_TOOL_CATALOGUE
    )


def test_another_family_returns_only_the_rows_the_request_carries() -> None:
    catalogue = OPENCODE_FREE_TIER_CATALOGUE.catalogue_for_request(FREE, PI_DEFAULTS)
    assert dict(catalogue) == {"read": "read", "bash": "bash", "edit": "edit"}


def test_one_role_is_claimed_by_one_client_spelling() -> None:
    """Two client tools for one role: the first declared row claims it."""

    family = ToolFamily(
        name="t",
        provenance="source",
        cited="test",
        spellings={"shell_a": "bash", "shell_b": "bash", "view": "read"},
    )
    names = frozenset({"shell_b", "shell_a", "view"})
    assert family.covers(names) == 2
    assert dict(family.catalogue(names)) == {"shell_a": "bash", "view": "read"}
    codec = OpenAIToolNameCodec.from_names(names, catalogue=family.catalogue(names))
    assert codec.encode("shell_b") == "shell_b"
    assert [codec.decode(codec.encode(name)) for name in sorted(names)] == sorted(names)


def test_no_client_spelling_maps_to_two_host_spellings() -> None:
    """A spelling two families both declare means the same tool in both."""

    seen: dict[str, tuple[str, str]] = {}
    for family in OPENCODE_TOOL_FAMILIES:
        for client, host in family.spellings.items():
            if client in seen:
                assert seen[client][1] == host, (client, seen[client][0], family.name)
            seen[client] = (family.name, host)


def test_a_family_only_maps_to_opencode_spellings() -> None:
    for family in OPENCODE_TOOL_FAMILIES:
        assert set(family.spellings.values()) <= set(OPENCODE_BUILTIN_TOOL_NAMES)


def test_every_family_says_where_it_was_read() -> None:
    names = [family.name for family in OPENCODE_TOOL_FAMILIES]
    assert len(names) == len(set(names))
    for family in OPENCODE_TOOL_FAMILIES:
        assert family.provenance in ("captured", "source"), family.name
        assert len(family.cited) >= 20, family.name
        assert family.spellings, family.name
