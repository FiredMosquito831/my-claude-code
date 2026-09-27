"""Every harness's real tool catalogue survives the round trip on every surface.

A guard, not a feature. MCC rewrites tool names on the way out:
- on the Zen free tier it renames each client's shell/read/edit/glob/grep to
  OpenCode's spelling and adds labelled stand-ins;
- on every Chat Completions host it aliases names past 64 characters or
  outside ``[A-Za-z0-9_-]``;
- on Zen's Responses door it aliases at 64;
- since 7.59.0 it aliases on ChatGPT OAuth at the limit the backend stated.

On the way back it must undo exactly that. The guard exists because any of
three events could break the round trip silently:
- a new harness;
- a new Claude Code built-in (``Artifact``, ``ArtifactData``);
- a change to a free-tier gate.

The fixtures are one REAL catalogue per harness, definitions only
(``tests/fixtures/tool_catalogues/*.json``; each file says where it was read).
For each catalogue x surface x (free Zen on / off), every test:

1. builds the outbound request through the real provider code and records the
   bytes a fake upstream received;
2. has the fake model call **every** client tool by its outbound name, with
   one argument per schema property, as a structured call;
3. asserts the name the client receives is its own and the argument keys are
   unchanged;
4. asserts the outbound tool count is the client's, plus exactly the declared
   stand-ins and only on the free tier;
5. asserts no two outbound names collide, case-insensitively included, unless
   the client itself sent that pair;
6. asserts the outbound tools and the history prefix are byte-identical across
   two consecutive turns (the prompt cache).

If a case here fails, the translation broke: fix the code, never this file.
"""

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Iterator
from compression import zstd
from pathlib import Path
from typing import Any

import httpx
import pytest
from openai import AsyncOpenAI

from my_claude_code.config import settings as config_settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.stream_contracts import parse_sse_text
from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.core.request_log import pack_bodies
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth import ChatGPTOAuthProvider
from my_claude_code.providers.chatgpt_oauth.provider import (
    CHATGPT_OAUTH_DEFAULT_BASE,
    CHATGPT_OAUTH_PROVIDER_ID,
)
from my_claude_code.providers.openai_chat.opencode_catalogue import (
    OPENCODE_TOOL_FAMILIES,
)
from my_claude_code.providers.recovery import LearnedFactStore
from my_claude_code.providers.recovery import store as learned_store
from tests.contracts.tool_log_check import check
from tests.providers.opencode_family_bodies import (
    FREE,
    PAID,
    REASONING,
    opencode_provider,
)
from tests.providers.support import passthrough_rate_limiter

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "tool_catalogues"
HARNESSES = (
    "claude_code",
    "codex",
    "opencode",
    "opencode2",
    "gemini_cli",
    "qwen_code",
    "pi",
)
ZEN_SURFACES = ("chat", "responses", "messages")
#: The path each door posts to, so a case cannot pass on the wrong door.
ENDPOINTS = {
    "chat": "/chat/completions",
    "responses": "/responses",
    "messages": "/messages",
    "chatgpt_oauth": "/codex/responses",
}
#: What the free tier appends for each harness (the family's declared
#: stand-ins for the roles its real catalogue lacks); nothing anywhere else.
EXPECTED_STAND_INS = {
    "codex": ["read", "glob", "grep"],
    "gemini_cli": ["bash", "edit"],
    "qwen_code": ["bash", "edit"],
}
#: Every stand-in any family declares: the only tools MCC may ever add.
DECLARED_STAND_INS = frozenset(
    (role, text)
    for family in OPENCODE_TOOL_FAMILIES
    for role, text in family.stand_ins.items()
)
_SAMPLE: dict[str, Any] = {
    "string": "x",
    "integer": 1,
    "number": 1,
    "boolean": True,
    "array": [],
    "object": {},
}


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


def _catalogue(harness: str) -> list[dict[str, Any]]:
    document = json.loads((FIXTURES / f"{harness}.json").read_text(encoding="utf-8"))
    assert document["harness"] == harness
    assert document["source"]
    tools = document["tools"]
    assert len(tools) == document["tool_count"]
    return tools


def _arguments(tool: dict[str, Any]) -> dict[str, Any]:
    """One value per declared property, of the declared type where there is one."""

    properties = (tool.get("input_schema") or {}).get("properties") or {}
    out: dict[str, Any] = {}
    for key, spec in properties.items():
        kind = spec.get("type") if isinstance(spec, dict) else None
        out[key] = _SAMPLE.get(kind if isinstance(kind, str) else "string", "x")
    return out


def _request(
    model: str, tools: list[dict[str, Any]], *, turn: int = 1
) -> MessagesRequest:
    """Turn 1 asks; turn 2 replays turn 1's call of the longest tool, then asks again."""

    messages: list[dict[str, Any]] = [{"role": "user", "content": "do the task"}]
    if turn == 2:
        longest = max(tools, key=lambda tool: len(tool["name"]))
        messages += [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_turn1",
                        "name": longest["name"],
                        "input": _arguments(longest),
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_turn1",
                        "content": "ok",
                    }
                ],
            },
        ]
    return MessagesRequest.model_validate(
        {
            "model": model,
            "max_tokens": 256,
            "stream": True,
            "system": "You are a coding agent.",
            "messages": messages,
            "tools": tools,
        }
    )


# -- what the client received --------------------------------------------------------


def _tool_uses(sse: str) -> list[tuple[str, dict[str, Any]]]:
    """``(name, input)`` for every tool_use block, in stream order."""

    names: dict[int, str] = {}
    parts: dict[int, str] = {}
    for event in parse_sse_text(sse):
        data = event.data
        if event.event == "content_block_start":
            block = data["content_block"]
            if block.get("type") == "tool_use":
                names[data["index"]] = block["name"]
                parts[data["index"]] = ""
        elif event.event == "content_block_delta":
            delta = data["delta"]
            if delta.get("type") == "input_json_delta" and data["index"] in parts:
                parts[data["index"]] += delta.get("partial_json") or ""
    return [
        (names[index], json.loads(parts[index]) if parts[index] else {})
        for index in sorted(names)
    ]


# -- one fake upstream per surface ------------------------------------------------------


def _chat_answer(calls: list[tuple[str, dict[str, Any]]]) -> bytes:
    def frame(delta: dict[str, Any], finish: str | None = None) -> str:
        chunk = {
            "id": "chatcmpl-contract",
            "object": "chat.completion.chunk",
            "created": 1790000000,
            "model": "m",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(chunk)}\n\n"

    frames = [frame({"role": "assistant", "content": ""})]
    for index, (name, arguments) in enumerate(calls):
        frames.append(
            frame(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": f"call_{index}",
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ]
                }
            )
        )
    frames.append(frame({}, "tool_calls"))
    frames.append("data: [DONE]\n\n")
    return "".join(frames).encode()


def _responses_answer(calls: list[tuple[str, dict[str, Any]]]) -> bytes:
    frames: list[dict[str, Any]] = [
        {"type": "response.created", "response": {"id": "r"}}
    ]
    for index, (name, arguments) in enumerate(calls):
        item = {
            "type": "function_call",
            "id": f"fc_{index}",
            "call_id": f"call_{index}",
            "name": name,
            "arguments": "",
        }
        frames += [
            {"type": "response.output_item.added", "output_index": index, "item": item},
            {
                "type": "response.function_call_arguments.delta",
                "output_index": index,
                "item_id": f"fc_{index}",
                "delta": json.dumps(arguments),
            },
            {
                "type": "response.output_item.done",
                "output_index": index,
                "item": {**item, "arguments": json.dumps(arguments)},
            },
        ]
    frames.append({"type": "response.completed", "response": {"id": "r"}})
    return "".join(f"data: {json.dumps(f)}\n\n" for f in frames).encode()


def _messages_answer(calls: list[tuple[str, dict[str, Any]]]) -> bytes:
    events: list[tuple[str, dict[str, Any]]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "m",
                    "content": [],
                    "stop_reason": None,
                    "usage": {"input_tokens": 3, "output_tokens": 0},
                },
            },
        )
    ]
    for index, (name, arguments) in enumerate(calls):
        events += [
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {
                        "type": "tool_use",
                        "id": f"toolu_{index}",
                        "name": name,
                        "input": {},
                    },
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(arguments),
                    },
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": index}),
        ]
    events += [
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "tool_use"},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {e}\ndata: {json.dumps(d)}\n\n" for e, d in events).encode()


def _sse_response(payload: bytes, request: httpx.Request) -> httpx.Response:
    async def body() -> Any:
        yield payload

    return httpx.Response(
        200,
        content=body(),
        headers={"content-type": "text/event-stream"},
        request=request,
    )


def _outbound_names(body: dict[str, Any]) -> list[str]:
    return [
        tool.get("name") or tool["function"]["name"] for tool in body.get("tools") or ()
    ]


class Upstream:
    """Records every body; the model calls each client tool by its outbound name."""

    def __init__(self, surface: str, tools: list[dict[str, Any]]) -> None:
        self.surface = surface
        self.tools = tools
        self.bodies: list[dict[str, Any]] = []
        self.paths: list[str] = []

    def calls(self, body: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
        # The client's own tools are the first len(tools) outbound entries, in
        # the client's order; stand-ins only ever follow them.
        wire = _outbound_names(body)[: len(self.tools)]
        return [
            (name, _arguments(tool))
            for name, tool in zip(wire, self.tools, strict=True)
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        self.paths.append(request.url.path)
        calls = self.calls(body)
        if self.surface == "chat":
            return httpx.Response(
                200,
                content=_chat_answer(calls),
                headers={"content-type": "text/event-stream"},
                request=request,
            )
        if self.surface == "messages":
            return _sse_response(_messages_answer(calls), request)
        return _sse_response(_responses_answer(calls), request)


async def _through_zen(
    surface: str, request: MessagesRequest, upstream: Upstream
) -> str:
    """One request through the real OpenCode provider on one door."""

    provider = opencode_provider("opencode")
    if surface == "chat":
        provider._client = AsyncOpenAI(
            api_key="sk-test",
            base_url="https://opencode.ai/zen/v1",
            max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
        )
        try:
            frames = [
                frame
                async for frame in provider.stream_response(
                    request, 11, request_id="req-contract", reasoning=REASONING
                )
            ]
        finally:
            await provider.cleanup()
        return "".join(frames)
    shaped = provider.with_stand_ins(request)
    if surface == "responses":
        transport = provider._responses
        transport._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        body, headers = transport.build_body(
            shaped, reasoning=REASONING, max_output_tokens=256
        )
        frames = [
            frame
            async for frame in transport.stream(
                shaped,
                input_tokens=3,
                reasoning=REASONING,
                body=body,
                headers=headers,
                surface_label="responses",
            )
        ]
        await transport.aclose()
        return "".join(frames)
    messages = provider._messages
    inner: Any = messages._provider
    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    try:
        frames = [
            frame
            async for frame in messages.stream(
                shaped, input_tokens=3, reasoning=REASONING
            )
        ]
    finally:
        await inner._client.aclose()
    return "".join(frames)


async def _through_chatgpt(request: MessagesRequest, upstream: Upstream) -> str:
    provider = ChatGPTOAuthProvider(
        ProviderConfig(
            api_key="test_token",
            base_url=CHATGPT_OAUTH_DEFAULT_BASE,
            rate_limit=10,
            rate_window=60,
            max_concurrency=5,
        ),
        rate_limiter=passthrough_rate_limiter(),
    )
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    try:
        frames = [
            frame
            async for frame in provider.stream_response(
                request, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
            )
        ]
    finally:
        await provider._client.aclose()
    return "".join(frames)


# -- the four statements, checked the same way on every surface ---------------------------


def _casefold_pairs(names: list[str]) -> set[str]:
    seen: dict[str, str] = {}
    pairs: set[str] = set()
    for name in names:
        folded = name.casefold()
        if folded in seen and seen[folded] != name:
            pairs.add(folded)
        seen[folded] = name
    return pairs


def _assert_round_trip(
    label: str,
    tools: list[dict[str, Any]],
    body: dict[str, Any],
    received: list[tuple[str, dict[str, Any]]],
    *,
    stand_ins_allowed: bool,
) -> None:
    client = [tool["name"] for tool in tools]
    outbound = _outbound_names(body)

    # 1. every call comes back under the client's own name, keys unchanged.
    assert [name for name, _input in received] == client, label
    for (name, got), tool in zip(received, tools, strict=True):
        assert set(got) == set(_arguments(tool)), (label, name)

    # 2. count: the client's, plus only declared stand-ins, only where allowed.
    added = body["tools"][len(client) :]
    for tool in added:
        name = tool.get("name") or tool["function"]["name"]
        text = tool.get("description") or tool.get("function", {}).get("description")
        assert (name, text) in DECLARED_STAND_INS, (label, name)
    if not stand_ins_allowed:
        assert not added, (label, [t.get("name") for t in added])
    assert len(outbound) == len(client) + len(added), label

    # 3. no collision MCC made: exact, or differing only in case.
    assert len(set(outbound)) == len(outbound), label
    assert _casefold_pairs(outbound) <= _casefold_pairs(client), label


def _history(body: dict[str, Any]) -> list[Any]:
    return body.get("messages") or body.get("input") or []


def _assert_cache_prefix(
    label: str, first: dict[str, Any], second: dict[str, Any]
) -> None:
    """Turn 2's tools and history prefix are turn 1's, byte for byte."""

    assert _canonical(first.get("tools")) == _canonical(second.get("tools")), label
    for key in ("instructions", "system"):
        assert _canonical(first.get(key)) == _canonical(second.get(key)), (label, key)
    head = _history(first)
    # Everything turn 1 sent in its history reappears unchanged at the start
    # of turn 2's.
    assert _canonical(_history(second)[: len(head)]) == _canonical(head), label


def _canonical(value: Any) -> str:
    """The exact bytes compared: key order kept, nothing normalised away."""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


# -- the contract ----------------------------------------------------------------------


@pytest.mark.parametrize("free", [True, False], ids=["free_zen", "paid_zen"])
@pytest.mark.parametrize("surface", ZEN_SURFACES)
@pytest.mark.parametrize("harness", HARNESSES)
def test_every_tool_round_trips_through_zen(
    harness: str, surface: str, free: bool
) -> None:
    tools = _catalogue(harness)
    model = FREE if free else PAID
    label = f"{harness}/{surface}/{'free' if free else 'paid'}"

    first = Upstream(surface, tools)
    received = _tool_uses(
        asyncio.run(_through_zen(surface, _request(model, tools), first))
    )
    assert len(first.bodies) == 1, label
    assert first.paths[0].endswith(ENDPOINTS[surface]), (label, first.paths)
    _assert_round_trip(label, tools, first.bodies[0], received, stand_ins_allowed=free)
    added = _outbound_names(first.bodies[0])[len(tools) :]
    assert added == (EXPECTED_STAND_INS.get(harness, []) if free else []), label

    second = Upstream(surface, tools)
    asyncio.run(_through_zen(surface, _request(model, tools, turn=2), second))
    _assert_cache_prefix(label, first.bodies[0], second.bodies[0])


#: ``None``: nothing stated, so nothing may be renamed. ``64``: a stated limit
#: these real catalogues exceed (Claude Code's longest name is 76), so the
#: aliasing path runs; the backend itself has stated 128, which no real
#: catalogue here reaches.
@pytest.mark.parametrize("learned", [None, 64], ids=["nothing_learned", "learned_64"])
@pytest.mark.parametrize("harness", HARNESSES)
def test_every_tool_round_trips_through_chatgpt_oauth(
    harness: str, learned: int | None
) -> None:
    tools = _catalogue(harness)
    store = LearnedFactStore()
    learned_store.set_learned_fact_store(store)
    if learned is not None:
        store.memory_for(CHATGPT_OAUTH_PROVIDER_ID).learn_responses_tool_name_limit(
            learned, evidence=f"maximum length {learned}"
        )
    label = f"{harness}/chatgpt_oauth/{learned}"

    first = Upstream("responses", tools)  # the ChatGPT backend speaks Responses
    received = _tool_uses(
        asyncio.run(_through_chatgpt(_request("gpt-5.6-luna", tools), first))
    )
    assert len(first.bodies) == 1, label
    _assert_round_trip(label, tools, first.bodies[0], received, stand_ins_allowed=False)
    outbound = _outbound_names(first.bodies[0])
    if learned is None:
        # Nothing stated, nothing invented: every name leaves as written.
        assert outbound == [tool["name"] for tool in tools], label
    else:
        assert all(len(name) <= learned for name in outbound), label

    second = Upstream("responses", tools)
    asyncio.run(_through_chatgpt(_request("gpt-5.6-luna", tools, turn=2), second))
    _assert_cache_prefix(label, first.bodies[0], second.bodies[0])


def test_the_fixtures_cover_what_the_contract_promises() -> None:
    claude = [tool["name"] for tool in _catalogue("claude_code")]
    assert {"Artifact", "ArtifactData", "Bash", "Read", "Edit", "Glob", "Grep"} <= set(
        claude
    )
    assert len([n for n in claude if n.startswith("mcp__") and len(n) > 64]) >= 3
    assert "shell" in [tool["name"] for tool in _catalogue("opencode2")]
    for harness in HARNESSES:
        assert _catalogue(harness), harness


# -- the negative: a case variant of a catalogue name -------------------------------------


@pytest.mark.parametrize("surface", ZEN_SURFACES)
def test_a_case_variant_of_a_catalogue_name_never_collides_silently(
    surface: str,
) -> None:
    """Claude Code plus MCP tools literally named ``bash`` and ``READ``.

    On the free tier ``Bash`` becomes ``bash``, so the client's own ``bash``
    must be aliased away rather than sent twice, and ``READ`` must not land
    beside ``read``. Each still comes back under its own name.
    """

    decoys = [
        {
            "name": "bash",
            "description": "an MCP shell",
            "input_schema": {
                "type": "object",
                "properties": {"cmd": {"type": "string"}},
            },
        },
        {
            "name": "READ",
            "description": "an MCP reader",
            "input_schema": {
                "type": "object",
                "properties": {"uri": {"type": "string"}},
            },
        },
    ]
    tools = [*_catalogue("claude_code"), *decoys]
    for model in (FREE, PAID):
        label = f"decoys/{surface}/{model}"
        upstream = Upstream(surface, tools)
        received = _tool_uses(
            asyncio.run(_through_zen(surface, _request(model, tools), upstream))
        )
        _assert_round_trip(
            label, tools, upstream.bodies[0], received, stand_ins_allowed=model == FREE
        )
        outbound = _outbound_names(upstream.bodies[0])
        if model == FREE:
            assert "bash" in outbound and "read" in outbound, label
            folded = [name.casefold() for name in outbound]
            assert len(folded) == len(set(folded)), label


# -- the log half: received names must be a subset of what was sent ----------------------


def _log_with(
    tmp_path: Path, sent: list[str], received: list[str], *, blob: bool
) -> Path:
    """The smallest request log the checker reads: one request, its catalogue, its calls."""

    path = tmp_path / "requests.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE tool_schemas (sha BLOB PRIMARY KEY, name TEXT NOT NULL, definition TEXT);
        CREATE TABLE tool_catalogues (sha BLOB PRIMARY KEY, tool_count INTEGER NOT NULL,
            member_shas BLOB NOT NULL, first_seen REAL, last_seen REAL, seen INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE requests (id TEXT PRIMARY KEY, ts_epoch REAL, harness TEXT, provider TEXT,
            resolved_model TEXT, tool_catalogue_sha BLOB, tool_calls TEXT);
        CREATE TABLE request_bodies (request_id TEXT PRIMARY KEY, sha TEXT, input_sha TEXT);
        CREATE TABLE body_blobs (sha TEXT PRIMARY KEY, dict_id INTEGER, payload BLOB NOT NULL);
        CREATE TABLE body_dictionaries (id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL,
            content BLOB NOT NULL);
        """
    )
    members = b""
    for name in sent:
        sha = hashlib.sha256(name.encode()).digest()
        members += sha
        con.execute("INSERT INTO tool_schemas VALUES (?, ?, NULL)", (sha, name))
    catalogue = hashlib.sha256(members).digest()
    con.execute(
        "INSERT INTO tool_catalogues VALUES (?, ?, ?, 0, 0, 1)",
        (catalogue, len(sent), members),
    )
    calls = [{"name": name, "input": {}} for name in received]
    inline = None if blob else json.dumps(calls)
    con.execute(
        "INSERT INTO requests VALUES ('req_1', 1.0, 'claude', 'opencode', 'm', ?, ?)",
        (catalogue, inline),
    )
    if blob:
        payload = zstd.compress(pack_bodies({"tool_calls": calls}))
        con.execute("INSERT INTO request_bodies VALUES ('req_1', 'b1', NULL)")
        con.execute("INSERT INTO body_blobs VALUES ('b1', NULL, ?)", (payload,))
    con.commit()
    con.close()
    return path


@pytest.mark.parametrize("blob", [False, True], ids=["inline", "blob"])
def test_the_log_check_passes_names_the_client_sent(tmp_path: Path, blob: bool) -> None:
    path = _log_with(tmp_path, ["Bash", "Read"], ["Read", "Bash", "Read"], blob=blob)
    report = check(path)
    assert (report.examined, report.with_calls, report.calls) == (1, 1, 3)
    assert report.findings == []


@pytest.mark.parametrize("blob", [False, True], ids=["inline", "blob"])
def test_the_log_check_flags_a_leaked_wire_name_and_a_stand_in(
    tmp_path: Path, blob: bool
) -> None:
    path = _log_with(
        tmp_path,
        ["Bash", "Read"],
        ["read", "grep", "mcp__x_0123456789abcdef"],
        blob=blob,
    )
    report = check(path)
    assert [(f.name, f.kind) for f in report.findings] == [
        ("read", "stand-in"),
        ("grep", "stand-in"),
        ("mcp__x_0123456789abcdef", "not offered"),
    ]
