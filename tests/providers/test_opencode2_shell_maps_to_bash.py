"""OpenCode 2 calls its shell tool ``shell``; Zen's free tier looks for ``bash``.

Measured 2026-09-27 (``specs/INVESTIGATION-ZEN-SPOOF-PER-HARNESS.md`` §2, F5):
``mcc-opencode2`` (``@opencode-ai/cli`` 0.0.0-beta-18866) sent twelve tools --
``edit, glob, grep, question, read, shell, skill, subagent, webfetch,
websearch, write, execute`` -- and the ``opencode_native`` family, whose rows
are OpenCode 1's ``bash/read/edit/glob/grep``, found four of them. ``shell``
went to Zen as ``shell``: four of OpenCode's five names, one tightening away
from a 403.

7.58.3 declares OpenCode 2's own row (``shell -> bash`` plus the four names it
shares with OpenCode 1). What these tests hold:

* OpenCode 2's catalogue chooses the new row and reaches five of five on every
  door, with the same tool count it sent and no case-fold collision;
* a model's ``bash`` call comes back to OpenCode 2 as ``shell`` on Chat
  Completions, Responses and Messages, and a replayed ``shell`` goes out as
  ``bash``;
* OpenCode 1's ``bash`` still goes out and comes back as ``bash``;
* the new row changes no family choice any earlier catalogue made -- the tie
  rule (declaration order) is untouched, and the row is declared last so it
  can win only where it finds strictly more roles.

Claude Code's bytes are held by the 7.49.0 goldens in
``test_opencode_free_tier_harness_families.py``, which this change leaves
unmodified.
"""

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

from my_claude_code.config import settings as config_settings
from my_claude_code.core.anthropic.openai_tool_names import request_tool_names
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.providers.openai_chat.opencode_catalogue import (
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
    FREE,
    OPENCODE_NATIVE,
    PAID,
    opencode_bodies,
    opencode_provider,
    tool_request,
)
from tests.providers.test_opencode_free_tier_harness_families import (
    CAPTURED,
    CODEX_0_155_1,
    FROM_SOURCE,
    _messages_call,
)

#: OpenCode 2's catalogue as the scratch MCC's own ``tool_catalogues`` stored
#: it (harness ``opencode2``, ``/v1/messages``, 2026-09-27, two requests).
OPENCODE2_BETA_18866 = [
    "edit",
    "glob",
    "grep",
    "question",
    "read",
    "shell",
    "skill",
    "subagent",
    "webfetch",
    "websearch",
    "write",
    "execute",
]
OPENCODE_FIVE = frozenset({"bash", "read", "edit", "glob", "grep"})
SURFACES = ("chat", "responses", "messages")


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "OPENCODE_CLIENT_IDENTITY",
        "OPENCODE_FREE_TIER_MODELS",
        "OPENCODE_FREE_TIER_CREDENTIAL",
    ):
        monkeypatch.delenv(name, raising=False)
    config_settings.get_settings.cache_clear()
    yield
    config_settings.get_settings.cache_clear()


def _names(body: dict[str, Any]) -> list[str]:
    return [
        tool.get("name") or tool["function"]["name"] for tool in body.get("tools", [])
    ]


def _replayed(body: dict[str, Any], surface: str) -> list[str]:
    """The tool-call names a body replays from history, per door."""

    if surface == "chat":
        return [
            call["function"]["name"]
            for message in body["messages"]
            for call in message.get("tool_calls") or ()
        ]
    if surface == "responses":
        return [
            item["name"]
            for item in body["input"]
            if isinstance(item, dict) and item.get("type") == "function_call"
        ]
    return [
        block["name"]
        for message in body["messages"]
        if isinstance(message.get("content"), list)
        for block in message["content"]
        if block.get("type") == "tool_use"
    ]


def _without_opencode2() -> tuple[ToolFamily, ...]:
    return tuple(f for f in OPENCODE_TOOL_FAMILIES if f.name != "opencode2")


# -- OpenCode 2's own catalogue -------------------------------------------------


def test_opencode2_catalogue_chooses_its_own_row() -> None:
    chosen = select_tool_family(frozenset(OPENCODE2_BETA_18866), OPENCODE_TOOL_FAMILIES)
    assert chosen is not None
    assert chosen.name == "opencode2"
    assert chosen.provenance == "captured"
    assert dict(chosen.catalogue(frozenset(OPENCODE2_BETA_18866))) == {
        "shell": "bash",
        "read": "read",
        "edit": "edit",
        "glob": "glob",
        "grep": "grep",
    }
    # Before 7.58.3 the same catalogue chose OpenCode 1's row and found four.
    before = select_tool_family(frozenset(OPENCODE2_BETA_18866), _without_opencode2())
    assert before is not None
    assert before.name == "opencode_native"
    assert before.covers(frozenset(OPENCODE2_BETA_18866)) == 4


@pytest.mark.parametrize("surface", SURFACES)
def test_opencode2_reaches_five_of_five_on_every_door(surface: str) -> None:
    request = tool_request(FREE, OPENCODE2_BETA_18866, history=["shell", "read"])
    body = opencode_bodies("opencode", request)[surface]
    wire = _names(body)
    assert wire == [
        "bash" if name == "shell" else name for name in OPENCODE2_BETA_18866
    ]
    assert set(wire) >= OPENCODE_FIVE
    # Same count as the client sent: no stand-in, nothing dropped.
    assert len(wire) == len(OPENCODE2_BETA_18866)
    folded = [name.casefold() for name in wire]
    assert len(folded) == len(set(folded))
    # A replayed ``shell`` call names the tool the catalogue lists.
    assert _replayed(body, surface) == ["bash", "read"]


def test_opencode2_paid_model_is_untouched() -> None:
    """Outside the free tier nothing is renamed, OpenCode 2 or not."""

    for surface, body in opencode_bodies(
        "opencode", tool_request(PAID, OPENCODE2_BETA_18866)
    ).items():
        assert _names(body) == OPENCODE2_BETA_18866, surface


def test_opencode2_bytes_are_stable_across_turns() -> None:
    """Turn N+1 adds an MCP tool and a replayed call; nothing already sent moves."""

    turn_n = tool_request(FREE, OPENCODE2_BETA_18866)
    turn_n1 = tool_request(
        FREE, [*OPENCODE2_BETA_18866, "mcp__exa__web_search_exa"], history=["shell"]
    )
    for surface in SURFACES:
        first = _names(opencode_bodies("opencode", turn_n)[surface])
        again = _names(opencode_bodies("opencode", turn_n)[surface])
        later = _names(opencode_bodies("opencode", turn_n1)[surface])
        assert again == first
        assert later[: len(first)] == first
    for request in (turn_n, turn_n1):
        chosen = select_tool_family(request_tool_names(request), OPENCODE_TOOL_FAMILIES)
        assert chosen is not None
        assert chosen.name == "opencode2"


# -- the round trip -------------------------------------------------------------------


def _decoded_on_every_door(names: list[str], client: str) -> dict[str, str]:
    """Encode ``client`` for one request, let the model call it, decode it back."""

    request = tool_request(FREE, names)
    provider = opencode_provider("opencode")
    chat_codec = provider.tool_name_codec(request)
    assert chat_codec is not None
    responses_codec = responses_tool_name_codec(
        request,
        provider._responses.tool_name_max_length,
        provider._responses.tool_catalogue(request),
    )
    assert responses_codec is not None
    wire = chat_codec.encode(client)
    assert responses_codec.encode(client) == wire

    assembler = OpenAIToolCallAssembler(tool_names=chat_codec)
    chat = "".join(
        assembler.process_tool_call(
            {"index": 0, "id": "c1", "function": {"name": wire, "arguments": ""}},
            AnthropicStreamLedger("msg_1", "m"),
        )
    ).replace(" ", "")
    converter = ResponsesStreamConverter(
        AnthropicStreamLedger("msg_1", "m"), tool_names=responses_codec
    )
    responses = "".join(
        converter.feed(
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "id": "c1", "name": wire},
            }
        )
    ).replace(" ", "")
    messages = asyncio.run(_messages_call(provider, request, wire)).replace(" ", "")
    return {"wire": wire, "chat": chat, "responses": responses, "messages": messages}


def test_a_bash_call_comes_back_to_opencode2_as_shell() -> None:
    seen = _decoded_on_every_door(OPENCODE2_BETA_18866, "shell")
    assert seen["wire"] == "bash"
    for surface in SURFACES:
        assert '"name":"shell"' in seen[surface], surface
        assert '"name":"bash"' not in seen[surface], surface


@pytest.mark.parametrize("client", ["read", "edit", "glob", "grep", "execute"])
def test_opencode2_other_names_round_trip_unchanged(client: str) -> None:
    seen = _decoded_on_every_door(OPENCODE2_BETA_18866, client)
    assert seen["wire"] == client
    for surface in SURFACES:
        assert f'"name":"{client}"' in seen[surface], surface


def test_opencode1_bash_still_comes_back_as_bash() -> None:
    chosen = select_tool_family(frozenset(OPENCODE_NATIVE), OPENCODE_TOOL_FAMILIES)
    assert chosen is not None
    assert chosen.name == "opencode_native"
    seen = _decoded_on_every_door(OPENCODE_NATIVE, "bash")
    assert seen["wire"] == "bash"
    for surface in SURFACES:
        assert '"name":"bash"' in seen[surface], surface


# -- the tie rule ------------------------------------------------------------------


#: Catalogues an extra ``shell`` row could steal on a tie: goose with only its
#: shell, and a Codex that names its shell ``shell`` (synthetic; no captured
#: Codex release sends it, ``test_a_row_nobody_could_cite_is_not_shipped``).
SHELL_TIES: dict[str, list[str]] = {
    "goose_shell_only": ["shell", "tree"],
    "codex_old_shell": ["shell", "apply_patch", "view_image"],
    "codex_0_155_1_plus_shell": [*CODEX_0_155_1, "shell"],
    "opencode1_and_2_mixed": [*OPENCODE_NATIVE, "shell"],
}


def test_opencode2_row_changes_no_earlier_choice() -> None:
    """Every catalogue chooses what it chose before the row existed.

    Declaration order still breaks ties; the new row is last, so it takes only
    a request where it finds strictly more roles than every earlier row --
    OpenCode 2's own, five against four.
    """

    assert OPENCODE_TOOL_FAMILIES[-1].name == "opencode2"
    catalogues = {
        **{label: names for label, (names, _family) in CAPTURED.items()},
        **{label: names for label, (names, _family, _roles) in FROM_SOURCE.items()},
        **SHELL_TIES,
    }
    for label, names in catalogues.items():
        present = frozenset(names)
        now = _choice(present, OPENCODE_TOOL_FAMILIES)
        assert now == _choice(present, _without_opencode2()), label
        assert now is not None, label


def _choice(
    present: frozenset[str], families: tuple[ToolFamily, ...]
) -> tuple[str, tuple[tuple[str, str], ...], tuple[tuple[str, str], ...]] | None:
    """Everything a family choice puts on the wire: name, mapping, stand-ins."""

    family = select_tool_family(present, families)
    if family is None:
        return None
    return (
        family.name,
        tuple(sorted(family.catalogue(present).items())),
        tuple(family.stand_ins.items()),
    )


def test_goose_and_old_codex_keep_their_stand_ins() -> None:
    """The reason the row is its own and last: a tie must not strip stand-ins."""

    provider = opencode_provider("opencode")
    for label, expected in (
        ("goose_shell_only", ["read", "glob", "grep"]),
        ("codex_old_shell", ["read", "glob", "grep"]),
    ):
        names = SHELL_TIES[label]
        augmented = provider.with_stand_ins(tool_request(FREE, names))
        added = [tool.name for tool in augmented.tools or []][len(names) :]
        assert added == expected, label
