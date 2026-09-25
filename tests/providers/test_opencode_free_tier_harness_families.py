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
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from my_claude_code.config import settings as config_settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.openai_tool_names import (
    OpenAIToolNameCodec,
    request_tool_names,
)
from my_claude_code.core.anthropic.stream_contracts import parse_sse_text
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
from tests.api.support import create_test_app
from tests.providers.opencode_family_bodies import (
    CLAUDE_FIVE,
    CLAUDE_FULL,
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


def _clear_settings() -> None:
    """Clear the cache production code actually reads.

    Through the module attribute, not an imported name:
    ``tests/config/test_env_aliases.py`` reloads the settings module, which
    rebinds ``get_settings`` in place, and a name imported before that would
    clear a cache nothing reads any more.
    """

    config_settings.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "OPENCODE_CLIENT_IDENTITY",
        "OPENCODE_FREE_TIER_MODELS",
        "OPENCODE_FREE_TIER_CREDENTIAL",
    ):
        monkeypatch.delenv(name, raising=False)
    _clear_settings()
    yield
    _clear_settings()


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
    _clear_settings()
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


# -- 7.50.0: the captured families ----------------------------------------------------

#: ``@openai/codex`` 0.155.1's tools as its request carried them (local recorder,
#: 2026-09-25); ``apply_patch`` is a custom tool, which MCC's Responses door turns
#: into a function taking ``{input: string}``.
CODEX_0_155_1 = [
    "exec_command",
    "write_stdin",
    "request_user_input",
    "apply_patch",
    "view_image",
    "get_goal",
    "create_goal",
    "update_goal",
]
#: The inbound catalogues below were sent by the real CLIs on 2026-09-25, each
#: run once under a scratch home against a scratch MCC whose upstream was a
#: local recorder (zero upstream calls), and read back from that MCC's own
#: ``tool_catalogues``. Headless (``-p``) runs withhold every tool that writes
#: or executes; ``--yolo`` runs send them.
GEMINI_0_58_0_HEADLESS = [
    "update_topic",
    "list_directory",
    "read_file",
    "grep_search",
    "glob",
    "google_web_search",
    "enter_plan_mode",
    "invoke_agent",
]
GEMINI_0_58_0_YOLO = [
    "update_topic",
    "list_directory",
    "read_file",
    "grep_search",
    "glob",
    "replace",
    "write_file",
    "web_fetch",
    "run_shell_command",
    "list_background_processes",
    "read_background_output",
    "google_web_search",
    "enter_plan_mode",
    "invoke_agent",
    "activate_skill",
]
QWEN_0_15_11_HEADLESS = [
    "tool_search",
    "agent",
    "skill",
    "list_directory",
    "read_file",
    "grep_search",
    "glob",
    "todo_write",
    "ask_user_question",
]
QWEN_0_15_11_YOLO = [
    "tool_search",
    "agent",
    "skill",
    "list_directory",
    "read_file",
    "grep_search",
    "glob",
    "edit",
    "write_file",
    "run_shell_command",
    "todo_write",
    "ask_user_question",
]
#: Command Code's request as MCC's request log stored it (harness
#: ``commandcode_cli``).
COMMANDCODE_1_65_0 = [
    "read_file",
    "write_file",
    "edit_file",
    "read_directory",
    "glob",
    "grep",
    "shell_command",
    "powershell",
    "activate_skill",
    "agent",
    "agent_output",
    "ask_user_question",
    "search_tools",
]
#: Every captured catalogue, and the family whose mapping it must be encoded
#: with. Gemini's and Qwen's headless catalogues carry only ``read_file``,
#: ``grep_search`` and ``glob`` of the five, which both families declare
#: identically -- see the tie test below.
CAPTURED: dict[str, tuple[list[str], str]] = {
    "claude_code": (
        [*CLAUDE_FIVE, "Write", "WebFetch", "mcp__exa__web_search_exa"],
        "claude_code",
    ),
    "opencode_1_18_32": (OPENCODE_NATIVE, "opencode_native"),
    "pi_0_82_1": (PI_DEFAULTS, "opencode_native"),
    "codex_0_155_1": (CODEX_0_155_1, "codex"),
    "gemini_0_58_0_headless": (GEMINI_0_58_0_HEADLESS, "gemini_cli"),
    "gemini_0_58_0_yolo": (GEMINI_0_58_0_YOLO, "gemini_cli"),
    "qwen_0_15_11_headless": (QWEN_0_15_11_HEADLESS, "gemini_cli"),
    "qwen_0_15_11_yolo": (QWEN_0_15_11_YOLO, "qwen_code"),
    "commandcode_1_65_0": (COMMANDCODE_1_65_0, "commandcode"),
}


def _roles(names: list[str]) -> int:
    return len(OPENCODE_FIVE & set(names))


@pytest.mark.parametrize("surface", ["chat", "responses", "messages"])
def test_codex_0_155_1_catalogue_maps_exec_command_and_apply_patch(
    surface: str,
) -> None:
    """Two roles, because Codex has two of the five: it reads and searches by shell.

    Since 7.51.0 the other three follow as stand-ins; see below.
    """

    request = tool_request(FREE, CODEX_0_155_1)
    wire = _names(opencode_bodies("opencode", request)[surface])
    assert wire[: len(CODEX_0_155_1)] == [
        "bash",
        "write_stdin",
        "request_user_input",
        "edit",
        "view_image",
        "get_goal",
        "create_goal",
        "update_goal",
    ]
    assert _roles(wire[: len(CODEX_0_155_1)]) == 2


@pytest.mark.parametrize(
    ("catalogue", "roles"),
    [
        (GEMINI_0_58_0_YOLO, 5),
        (GEMINI_0_58_0_HEADLESS, 3),
        (QWEN_0_15_11_YOLO, 5),
        (QWEN_0_15_11_HEADLESS, 3),
        (COMMANDCODE_1_65_0, 5),
    ],
    ids=[
        "gemini_yolo",
        "gemini_headless",
        "qwen_yolo",
        "qwen_headless",
        "commandcode",
    ],
)
@pytest.mark.parametrize("surface", ["chat", "responses", "messages"])
def test_gemini_qwen_and_commandcode_catalogues_on_every_door(
    catalogue: list[str], roles: int, surface: str
) -> None:
    """Five of five when the client sends its whole catalogue.

    Three when it runs headless and withholds its shell and its editor, which
    is every tool of the five it has left to send.
    """

    wire = _names(opencode_bodies("opencode", tool_request(FREE, catalogue))[surface])
    # The client's own tools; a headless run is topped up by stand-ins after them.
    assert _roles(wire[: len(catalogue)]) == roles
    assert _roles(wire) == 5
    folded = [name.casefold() for name in wire]
    assert len(folded) == len(set(folded))


def test_gemini_0_58_0_catalogue_reaches_five() -> None:
    request = tool_request(FREE, GEMINI_0_58_0_YOLO)
    wire = _names(opencode_bodies("opencode", request)["responses"])
    assert wire[:9] == [
        "update_topic",
        "list_directory",
        "read",
        "grep",
        "glob",
        "edit",
        "write_file",
        "web_fetch",
        "bash",
    ]


def test_qwen_0_15_11_catalogue_reaches_five() -> None:
    request = tool_request(FREE, QWEN_0_15_11_YOLO)
    wire = _names(opencode_bodies("opencode", request)["responses"])
    assert wire[3:10] == [
        "list_directory",
        "read",
        "grep",
        "glob",
        "edit",
        "write_file",
        "bash",
    ]


def test_commandcode_1_65_0_catalogue_reaches_five() -> None:
    request = tool_request(FREE, COMMANDCODE_1_65_0)
    wire = _names(opencode_bodies("opencode", request)["responses"])
    assert wire[:7] == [
        "read",
        "write_file",
        "edit",
        "read_directory",
        "glob",
        "grep",
        "bash",
    ]
    # ``powershell`` is a shell too, but not the one OpenCode's ``bash`` is:
    # it keeps its own name rather than claim a job it does differently.
    assert "powershell" in wire


def test_every_captured_catalogue_chooses_the_expected_family() -> None:
    for label, (names, expected) in CAPTURED.items():
        chosen = select_tool_family(frozenset(names), OPENCODE_TOOL_FAMILIES)
        assert chosen is not None, label
        assert chosen.name == expected, label


def test_a_tie_on_any_captured_catalogue_cannot_change_a_byte() -> None:
    """Families that tie at the top map the request's names identically.

    The spec asked for "no two families tie". The real headless Gemini and
    Qwen catalogues do tie -- both carry only ``read_file``, ``grep_search``
    and ``glob``, which both families declare -- so the property worth pinning
    is the one that matters: whichever tied family wins, the wire is the same.
    """

    for label, (names, _expected) in CAPTURED.items():
        present = frozenset(names)
        best = max(family.covers(present) for family in OPENCODE_TOOL_FAMILIES)
        tied = [
            family
            for family in OPENCODE_TOOL_FAMILIES
            if family.covers(present) == best
        ]
        mappings = {tuple(sorted(family.catalogue(present).items())) for family in tied}
        assert len(mappings) == 1, (label, [family.name for family in tied])


def test_the_captured_families_are_declared_in_order() -> None:
    assert [family.name for family in OPENCODE_TOOL_FAMILIES] == [
        "claude_code",
        "opencode_native",
        "codex",
        "gemini_cli",
        "qwen_code",
        "commandcode",
        "droid",
        "crush",
        "kimi_code",
        "goose",
        "cline",
    ]
    provenance = {family.name: family.provenance for family in OPENCODE_TOOL_FAMILIES}
    # Every client installed on the machine the rows were written on was
    # captured; the 7.52.0 rows were read from source or a scratch bundle.
    assert {name for name, how in provenance.items() if how == "captured"} == {
        "claude_code",
        "opencode_native",
        "codex",
        "gemini_cli",
        "qwen_code",
        "commandcode",
    }
    assert set(provenance.values()) == {"captured", "source"}


def test_a_row_nobody_could_cite_is_not_shipped() -> None:
    """Decision 15:00 #2: a guessed spelling fails silently, so it is left out.

    The investigation's table listed Gemini CLI's older grep name and two more
    Codex shell spellings from memory; neither is in the bundle or the capture
    this release cites. (``shell`` has been a cited spelling since 7.52.0 --
    goose's, from its source -- but never Codex's.)
    """

    every_client_spelling = {
        client for family in OPENCODE_TOOL_FAMILIES for client in family.spellings
    }
    assert "search_file_content" not in every_client_spelling
    assert [
        family.name for family in OPENCODE_TOOL_FAMILIES if "shell" in family.spellings
    ] == ["goose"]
    assert set(_family("codex").spellings) == {"exec_command", "apply_patch"}


# -- Codex's custom apply_patch, end to end through the Responses door --------------


def _codex_call_frames(wire: str, arguments: str) -> bytes:
    item = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "call_1",
        "name": wire,
        "arguments": "",
        "status": "in_progress",
    }
    done = {**item, "arguments": arguments, "status": "completed"}
    frames = [
        {"type": "response.created", "response": {"id": "resp_1"}},
        {"type": "response.output_item.added", "output_index": 0, "item": item},
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_1",
            "output_index": 0,
            "delta": arguments,
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "fc_1",
            "output_index": 0,
            "name": wire,
            "arguments": arguments,
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
    return b"".join(f"data: {json.dumps(frame)}\n\n".encode() for frame in frames)


class _ZenResponsesDoor:
    """The real OpenCode Responses transport behind a fake ``/zen/v1/responses``.

    Stands where ``resolve_provider`` would put a provider, so the request the
    API adapter routes is encoded, sent, answered and decoded by the shipped
    transport; only the socket is fake. The fake model calls whatever name the
    outbound body gave Codex's ``apply_patch``, or ``call`` when one is given.
    Stand-ins are added first, as ``_stream_across_surfaces`` adds them.
    """

    credential_label = None

    def __init__(self, patch_text: str, *, call: tuple[str, str] | None = None) -> None:
        self.patch_text = patch_text
        self.call = call
        self.sent: list[dict[str, Any]] = []

    def preflight_stream(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def stream_response(
        self, request: MessagesRequest, **_kwargs: Any
    ) -> AsyncIterator[str]:
        provider = opencode_provider("opencode")
        request = provider.with_stand_ins(request)
        transport = provider._responses
        body, headers = transport.build_body(
            request, reasoning=REASONING, max_output_tokens=256
        )
        if self.call is not None:
            wire, arguments = self.call
        else:
            wire = next(
                tool["name"]
                for tool in body["tools"]
                if "input" in tool["parameters"].get("properties", {})
            )
            arguments = json.dumps({"input": self.patch_text})

        def upstream(outbound: httpx.Request) -> httpx.Response:
            self.sent.append(json.loads(outbound.content))

            async def frames() -> AsyncIterator[bytes]:
                yield _codex_call_frames(wire, arguments)

            return httpx.Response(
                200, content=frames(), headers={"content-type": "text/event-stream"}
            )

        await transport.aclose()
        transport._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        try:
            async for event in transport.stream(
                request,
                input_tokens=0,
                reasoning=REASONING,
                body=body,
                headers=headers,
                surface_label="responses",
            ):
                yield event
        finally:
            await transport.aclose()


def test_codex_apply_patch_custom_tool_round_trips_as_custom() -> None:
    """``edit`` on the wire, a ``custom_tool_call`` named ``apply_patch`` to Codex."""

    door = _ZenResponsesDoor("*** Begin Patch\n*** End Patch")
    tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "name": "exec_command",
            "description": "Runs a command",
            "parameters": {
                "type": "object",
                "properties": {"cmd": {"type": "string"}},
                "required": ["cmd"],
            },
        },
        {
            "type": "custom",
            "name": "apply_patch",
            "description": "Apply a patch",
            "format": {
                "type": "grammar",
                "syntax": "lark",
                "definition": "start: /.+/",
            },
        },
    ]
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=door),
        TestClient(create_test_app()) as client,
    ):
        response = client.post(
            "/v1/responses",
            json={
                "model": f"opencode/{FREE}",
                "input": "Apply the patch",
                "stream": True,
                "tools": tools,
            },
        )

    assert response.status_code == 200
    assert [tool["name"] for tool in door.sent[0]["tools"]][:2] == ["bash", "edit"]
    events = parse_sse_text(response.text)
    call = events[-1].data["response"]["output"][0]
    assert call["type"] == "custom_tool_call"
    assert call["name"] == "apply_patch"
    assert call["input"] == "*** Begin Patch\n*** End Patch"
    assert '"edit"' not in response.text


# -- 7.51.0: stand-ins ------------------------------------------------------------------


def _stand_ins(request: MessagesRequest) -> MessagesRequest:
    return opencode_provider("opencode").with_stand_ins(request)


def _tool_names(request: MessagesRequest) -> list[str]:
    return [tool.name for tool in request.tools or ()]


def test_stand_ins_fill_only_missing_roles_for_a_declaring_family() -> None:
    codex = _stand_ins(tool_request(FREE, CODEX_0_155_1))
    assert _tool_names(codex) == [*CODEX_0_155_1, "read", "glob", "grep"]
    for tool in (codex.tools or [])[len(CODEX_0_155_1) :]:
        assert tool.input_schema == {"type": "object", "properties": {}}
        assert tool.description is not None
        assert tool.description.startswith("Not available in this client")
        assert "`exec_command`" in tool.description

    for headless in (GEMINI_0_58_0_HEADLESS, QWEN_0_15_11_HEADLESS):
        augmented = _stand_ins(tool_request(FREE, headless))
        assert _tool_names(augmented) == [*headless, "bash", "edit"]

    # A client that sends all five of its own gets nothing added.
    for complete in (
        GEMINI_0_58_0_YOLO,
        QWEN_0_15_11_YOLO,
        COMMANDCODE_1_65_0,
        OPENCODE_NATIVE,
        PI_DEFAULTS,
    ):
        request = tool_request(FREE, complete)
        assert _stand_ins(request) is request


@pytest.mark.parametrize("surface", ["chat", "responses", "messages"])
def test_stand_ins_take_codex_and_headless_clients_to_five_on_every_door(
    surface: str,
) -> None:
    for catalogue, added in (
        (CODEX_0_155_1, ["read", "glob", "grep"]),
        (GEMINI_0_58_0_HEADLESS, ["bash", "edit"]),
        (QWEN_0_15_11_HEADLESS, ["bash", "edit"]),
    ):
        wire = _names(
            opencode_bodies("opencode", tool_request(FREE, catalogue))[surface]
        )
        assert _roles(wire) == 5
        assert len(wire) == len(catalogue) + len(added)
        assert wire[len(catalogue) :] == added
        folded = [name.casefold() for name in wire]
        assert len(folded) == len(set(folded))


def test_stand_ins_never_added_to_claude_or_tool_less_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude = tool_request(FREE, CLAUDE_FULL, history=["Bash"])
    assert _stand_ins(claude) is claude
    sub_request = tool_request(FREE, ["Bash"])
    assert _stand_ins(sub_request) is sub_request
    tool_less = tool_request(FREE, [], history=["exec_command"])
    assert _stand_ins(tool_less) is tool_less
    # Outside the scope, whatever the client.
    for model in (PAID, "kimi-k2.6"):
        paid = tool_request(model, CODEX_0_155_1)
        assert _stand_ins(paid) is paid
    go_paid = tool_request("kimi-k2.6", CODEX_0_155_1)
    assert opencode_provider("opencode_go").with_stand_ins(go_paid) is go_paid
    # And the operator's opt-out turns them off with everything else.
    monkeypatch.setenv("OPENCODE_CLIENT_IDENTITY", "mcc")
    _clear_settings()
    opted_out = tool_request(FREE, CODEX_0_155_1)
    assert _stand_ins(opted_out) is opted_out


def test_stand_ins_are_appended_in_fixed_order() -> None:
    """After the client's own tools, in declared order, however the client orders."""

    shuffled = list(reversed(CODEX_0_155_1))
    assert _tool_names(_stand_ins(tool_request(FREE, shuffled)))[-3:] == [
        "read",
        "glob",
        "grep",
    ]
    turn_n = _stand_ins(tool_request(FREE, CODEX_0_155_1))
    turn_n1 = _stand_ins(
        tool_request(
            FREE,
            [*CODEX_0_155_1, "mcp__exa__web_search_exa"],
            history=["exec_command", "read"],
        )
    )
    assert _tool_names(turn_n1) == [
        *CODEX_0_155_1,
        "mcp__exa__web_search_exa",
        "read",
        "glob",
        "grep",
    ]
    for surface in ("chat", "responses", "messages"):
        first = _names(opencode_bodies("opencode", turn_n)[surface])
        later = _names(opencode_bodies("opencode", turn_n1)[surface])
        assert later[: len(CODEX_0_155_1)] == first[: len(CODEX_0_155_1)]


def test_a_called_stand_in_never_changes_the_family() -> None:
    """Three stand-ins called over a session leave Codex encoded as Codex.

    Each call replays as history. Counted as the client's own tools, three of
    them would out-score Codex's two and move the session onto OpenCode's
    family -- a different wire, and a broken prompt cache.
    """

    request = tool_request(
        FREE, CODEX_0_155_1, history=["read", "glob", "grep", "exec_command"]
    )
    augmented = _stand_ins(request)
    assert _tool_names(augmented)[-3:] == ["read", "glob", "grep"]
    provider = opencode_provider("opencode")
    assert dict(provider.tool_catalogue_for_request(augmented)) == {
        "exec_command": "bash",
        "apply_patch": "edit",
    }
    assert dict(provider.tool_catalogue_for_request(request)) == {
        "exec_command": "bash",
        "apply_patch": "edit",
    }


def test_applying_stand_ins_twice_adds_nothing() -> None:
    once = _stand_ins(tool_request(FREE, CODEX_0_155_1))
    assert _stand_ins(once) is once


def test_a_client_tool_that_only_shares_a_stand_ins_name_is_the_clients() -> None:
    """Recognised by name *and* declared description, never by name alone."""

    names = [*CODEX_0_155_1, "read"]
    request = tool_request(FREE, names)
    augmented = _stand_ins(request)
    # The client's own ``read`` stays; no second ``read`` is added.
    assert _tool_names(augmented) == [*names, "glob", "grep"]
    assert OPENCODE_FREE_TIER_CATALOGUE.carried_stand_ins(
        (tool.name, tool.description) for tool in augmented.tools or ()
    ) == frozenset({"glob", "grep"})


def test_tied_families_add_the_same_stand_ins() -> None:
    """Headless Gemini and Qwen tie; whichever wins, the request is the same."""

    present = frozenset(QWEN_0_15_11_HEADLESS)
    tied = [family for family in OPENCODE_TOOL_FAMILIES if family.covers(present) == 3]
    assert [family.name for family in tied] == ["gemini_cli", "qwen_code"]
    assert tied[0].stand_ins is tied[1].stand_ins
    for text in tied[0].stand_ins.values():
        assert "Gemini CLI" in text and "Qwen Code" in text


def test_claude_code_and_opencode_declare_no_stand_ins() -> None:
    assert not _family("claude_code").stand_ins
    assert not _family("opencode_native").stand_ins
    assert not _family("commandcode").stand_ins
    for family in OPENCODE_TOOL_FAMILIES:
        assert set(family.stand_ins) <= OPENCODE_FIVE
    # Codex has no tool at all for the roles it stands in for.
    assert not set(_family("codex").stand_ins) & set(
        _family("codex").spellings.values()
    )


# -- what happens when the model calls a stand-in -----------------------------------


def test_a_stand_in_call_is_passed_through_not_invented() -> None:
    """The client receives a call to a tool it does not have, unchanged.

    Not an invented result, not a shell command MCC made up, not a dropped
    stream: the call to ``read`` reaches the client as ``read`` with the
    model's own arguments, the stream finishes, and answering it with an
    error is the client's business (Codex 0.155.1 answers an unknown function
    with an error output -- proven live in this release's PR).
    """

    provider = opencode_provider("opencode")
    request = provider.with_stand_ins(tool_request(FREE, CODEX_0_155_1))

    chat_codec = provider.tool_name_codec(request)
    assert chat_codec is not None
    assert chat_codec.encode("read") == "read"
    assert chat_codec.decode("read") == "read"
    responses_codec = responses_tool_name_codec(
        request,
        provider._responses.tool_name_max_length,
        provider._responses.tool_catalogue(request),
    )
    assert responses_codec is not None
    assert responses_codec.decode("read") == "read"

    events = asyncio.run(_messages_call(provider, request, "read"))
    assert '"name": "read"' in events or '"name":"read"' in events.replace(" ", "")
    assert "message_stop" in events
    assert "exec_command" not in events


def test_a_stand_in_call_reaches_codex_as_a_function_call() -> None:
    """End to end on the Responses door, as Codex would see it."""

    door = _ZenResponsesDoor("", call=("read", json.dumps({"path": "hello.txt"})))
    tools: list[dict[str, Any]] = [
        {
            "type": "function",
            "name": "exec_command",
            "description": "Runs a command",
            "parameters": {
                "type": "object",
                "properties": {"cmd": {"type": "string"}},
                "required": ["cmd"],
            },
        },
        {
            "type": "custom",
            "name": "apply_patch",
            "description": "Apply a patch",
            "format": {
                "type": "grammar",
                "syntax": "lark",
                "definition": "start: /.+/",
            },
        },
    ]
    with (
        patch("my_claude_code.api.routes.resolve_provider", return_value=door),
        TestClient(create_test_app()) as client,
    ):
        response = client.post(
            "/v1/responses",
            json={
                "model": f"opencode/{FREE}",
                "input": "Read hello.txt",
                "stream": True,
                "tools": tools,
            },
        )

    assert response.status_code == 200
    assert [tool["name"] for tool in door.sent[0]["tools"]] == [
        "bash",
        "edit",
        "read",
        "glob",
        "grep",
    ]
    events = parse_sse_text(response.text)
    completed = events[-1].data["response"]
    assert completed["status"] == "completed"
    call = completed["output"][0]
    assert call["type"] == "function_call"
    assert call["name"] == "read"
    assert json.loads(call["arguments"]) == {"path": "hello.txt"}


# -- 7.52.0: clients read from source ---------------------------------------------------

#: Each list is the client's default tools as its pinned source names them,
#: plus an MCP tool of the kind every real session carries. Only names the
#: cited file:line (or byte offset) shows are used.
FROM_SOURCE: dict[str, tuple[list[str], str, int]] = {
    # Droid 0.227.0: the constants at droid.exe byte 160604141.
    "droid_0_227_0": (
        ["LS", "Read", "Create", "Edit", "Glob", "Grep", "Execute", "TodoWrite"],
        "droid",
        5,
    ),
    # Crush v0.96.1: internal/agent/tools/{bash,view,edit,glob,grep}.go.
    "crush_0_96_1": (["bash", "view", "edit", "glob", "grep", "mcp_x_y"], "crush", 5),
    # Kimi CLI 1.52.0: src/kimi_cli/tools/{shell,file/*}.
    "kimi_1_52_0": (
        ["Shell", "ReadFile", "StrReplaceFile", "Glob", "Grep", "mcp__x__y"],
        "kimi_code",
        5,
    ),
    # goose v1.52.0: developer/mod.rs test at :279.
    "goose_1_52_0": (["write", "edit", "shell", "tree", "read_image"], "goose", 2),
    # Cline CLI 3.0.65: createDefaultTools, definitions.ts:912.
    "cline_3_0_65": (
        [
            "read_files",
            "search_codebase",
            "run_commands",
            "fetch_web_content",
            "editor",
            "skills",
            "ask_question",
        ],
        "cline",
        4,
    ),
    # Kilo CLI 7.8.0: OpenCode's own spellings.
    "kilo_7_8_0": (
        ["bash", "read", "edit", "glob", "grep", "write", "task"],
        "opencode_native",
        5,
    ),
}


@pytest.mark.parametrize("label", sorted(FROM_SOURCE))
def test_each_source_catalogue_chooses_its_own_family(label: str) -> None:
    names, expected, roles = FROM_SOURCE[label]
    request = tool_request(FREE, names)
    chosen = select_tool_family(request_tool_names(request), OPENCODE_TOOL_FAMILIES)
    assert chosen is not None
    assert chosen.name == expected
    wire = _names(opencode_bodies("opencode", request)["responses"])
    assert _roles(wire[: len(names)]) == roles
    # Goose is topped up to five by its stand-ins; nobody else here needs them.
    assert _roles(wire) == (5 if expected == "goose" else roles)


@pytest.mark.parametrize("surface", ["chat", "responses", "messages"])
def test_source_catalogues_on_every_door(surface: str) -> None:
    expected_wire = {
        "droid_0_227_0": ["LS", "read", "Create", "edit", "glob", "grep", "bash"],
        "crush_0_96_1": ["bash", "read", "edit", "glob", "grep"],
        "kimi_1_52_0": ["bash", "read", "edit", "glob", "grep"],
        "goose_1_52_0": ["write", "edit", "bash", "tree", "read_image"],
        "cline_3_0_65": ["read", "grep", "bash", "fetch_web_content", "edit"],
    }
    for label, prefix in expected_wire.items():
        names, _family_name, _roles_expected = FROM_SOURCE[label]
        wire = _names(opencode_bodies("opencode", tool_request(FREE, names))[surface])
        assert wire[: len(prefix)] == prefix, label
        folded = [name.casefold() for name in wire]
        assert len(folded) == len(set(folded)), label


def test_goose_gets_read_glob_and_grep_stand_ins() -> None:
    names, _family_name, _roles_expected = FROM_SOURCE["goose_1_52_0"]
    augmented = _stand_ins(tool_request(FREE, names))
    assert _tool_names(augmented) == [*names, "read", "glob", "grep"]
    for tool in (augmented.tools or [])[len(names) :]:
        assert tool.description is not None
        assert "goose's `shell`" in tool.description


def test_droid_and_kimi_share_claude_codes_spellings_without_taking_its_requests() -> (
    None
):
    """``Read``/``Edit``/``Glob``/``Grep`` mean the same tool in all three.

    Mapped identically, so which family wins never changes those names; and a
    Claude Code request, which carries ``Bash``, still chooses Claude Code's
    family and the static five -- the 7.49.0 golden above holds it byte for
    byte.
    """

    for shared in ("Read", "Edit", "Glob", "Grep"):
        hosts = {
            family.spellings[shared]
            for family in OPENCODE_TOOL_FAMILIES
            if shared in family.spellings
        }
        assert len(hosts) == 1, shared
    for claude in (CLAUDE_FULL, CLAUDE_FIVE, ["Bash"], ["Read"], ["Glob", "Grep"]):
        chosen = select_tool_family(frozenset(claude), OPENCODE_TOOL_FAMILIES)
        assert chosen is not None
        assert chosen.name == "claude_code", claude


def test_a_tie_on_any_source_catalogue_cannot_change_a_byte() -> None:
    for label, (names, _expected, _roles_expected) in FROM_SOURCE.items():
        present = frozenset(names)
        best = max(family.covers(present) for family in OPENCODE_TOOL_FAMILIES)
        tied = [
            family
            for family in OPENCODE_TOOL_FAMILIES
            if family.covers(present) == best
        ]
        assert len({tuple(sorted(f.catalogue(present).items())) for f in tied}) == 1, (
            label
        )
        assert len({tuple(f.stand_ins.items()) for f in tied}) == 1, label


def test_clients_not_shipped_are_named() -> None:
    """Aider sends no tools; Antigravity publishes no source or bundle to read."""

    names = {family.name for family in OPENCODE_TOOL_FAMILIES}
    assert not names & {"aider", "antigravity"}
