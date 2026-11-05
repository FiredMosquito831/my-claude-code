"""The net 8.12.2 assumed existed: a refused history block stops replaying (8.12.3).

8.12.2 stopped dropping a non-signing host's unsigned ``thinking`` blocks from
the conversation history, and the accepted risk was stated in the
investigation's Q2 as *"if the host 400s it is exactly what the existing
reasoning-strip ladder is for -- the next request learns and the block goes"*.

**It does not, and this module pins both halves of that:**

* ``clone_body_without_reasoning_field`` pops a **top-level body key**, so the
  ladder's one retry re-sends the very history block the host refused and 400s
  again -- the request fails as a classified 400, exactly as on v8.12.2
  (:func:`test_a_refused_history_block_costs_this_request_and_is_learned`);
* what the host said is now remembered for that ``(provider, model)``, and the
  **next** request leaves its unsigned blocks out and is accepted
  (:func:`test_the_next_request_to_that_model_leaves_the_block_out`).

What is deliberately NOT here, and must stay absent: a second retry, a
pre-emptive strip, a fallback, or any change to the ladder's decisions. The
fact is written before ``RecoveryLadder.next_body`` is consulted and changes
nothing about the request that pays for it.

The evidence bar is four conditions, each with its own negative below -- a
validation rejection, a fault located at ``messages.N.``, a complaint that
names a thinking block, and a history that actually carried an unsigned one --
plus the host's own declaration that it signs nothing. A host that signs keeps
every byte it had.
"""

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.anthropic_messages import AnthropicMessagesProvider
from my_claude_code.providers.anthropic_messages.request import (
    build_anthropic_messages_body,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import create_openai_chat_provider
from my_claude_code.providers.openai_chat.messages_transport import MessagesTransport
from my_claude_code.providers.recovery import (
    ALLOWED_FACT_KINDS,
    FACT_HISTORY_THINKING_REJECTED,
    INFERRED_FACT_TTL_SECONDS,
    LearnedFactStore,
    reset_learned_fact_store,
    set_learned_fact_store,
)
from my_claude_code.providers.recovery.reasoning_reject import (
    complaint_refuses_a_history_thinking_block,
    history_carries_unsigned_thinking,
)
from tests.providers.support import passthrough_rate_limiter

GO_BASE_URL = "https://opencode.ai/go/v1"

#: The 400 a Messages host sends when it will not take a replayed block back.
#: Anthropic's own spelling, which is what a gateway speaking this protocol
#: copies: a fault located in the history that names the block.
HISTORY_REFUSAL = (
    "messages.1.content.0: Invalid `signature` in `thinking` block: "
    "this deployment does not accept replayed thinking"
)

_MESSAGES_SSE = (
    b'event: message_start\ndata: {"type":"message_start"}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def _signed(text: str = "Anthropic's own reasoning.") -> dict[str, Any]:
    return {"type": "thinking", "thinking": text, "signature": "EqQBCkYIBxgC"}


def _unsigned(text: str = "Go's own reasoning.") -> dict[str, Any]:
    return {"type": "thinking", "thinking": text}


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _tool_use(tool_id: str = "toolu_go") -> dict[str, Any]:
    return {"type": "tool_use", "id": tool_id, "name": "Read", "input": {"p": "a"}}


def _tool_result(tool_id: str = "toolu_go") -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": tool_id, "content": "done"}


def _user(content: Any) -> dict[str, Any]:
    return {"role": "user", "content": content}


def _assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"role": "assistant", "content": list(blocks)}


#: The 4,434 shape 8.12.2 restored: a tool loop whose final assistant turn
#: carries an unsigned ``thinking`` block and a ``tool_use``.
GO_TOOL_LOOP = [
    _user("Read main.py."),
    _assistant(_unsigned(), _tool_use()),
    _user([_tool_result()]),
]


def _request(messages: list[dict[str, Any]], **extra: Any) -> MessagesRequest:
    return MessagesRequest.model_validate(
        {"model": "claude-haiku-5-5", "max_tokens": 4096, "messages": messages, **extra}
    )


def _go_transport() -> MessagesTransport:
    """The transport the real provider builds for ``opencode_go``."""

    provider = create_openai_chat_provider(
        "opencode_go",
        ProviderConfig(api_key="sk-test", base_url=GO_BASE_URL),
        passthrough_rate_limiter(),
    )
    return provider._messages


def _refusal(message: str) -> httpx.Response:
    return httpx.Response(
        400,
        json={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": message},
        },
    )


def _http_400(message: str) -> httpx.HTTPStatusError:
    """The exception the transport raises for a 400, as the writer sees it."""

    request = httpx.Request("POST", f"{GO_BASE_URL}/messages")
    response = httpx.Response(
        400,
        json={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": message},
        },
        request=request,
    )
    return httpx.HTTPStatusError("400", request=request, response=response)


def _accepted() -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=_MESSAGES_SSE,
    )


def _replays_an_unsigned_block(body: dict[str, Any]) -> bool:
    return any(
        block.get("type") == "thinking" and not block.get("signature")
        for message in body.get("messages", [])
        if message.get("role") == "assistant"
        for block in message.get("content", [])
        if isinstance(block, dict)
    )


async def _drive(
    transport: MessagesTransport,
    request: MessagesRequest,
    *,
    refusal: str = HISTORY_REFUSAL,
    refuses: Any = None,
) -> tuple[list[dict[str, Any]], Exception | None]:
    """Send one request through the real transport; return what went out.

    ``refuses`` decides, per body, whether the fake host answers 400. The
    default is the honest one for this module: it refuses exactly the bodies
    that replay an unsigned history block.
    """

    sent: list[dict[str, Any]] = []
    decide = refuses if refuses is not None else _replays_an_unsigned_block

    def handler(http_request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(http_request.content))
        return _refusal(refusal) if decide(sent[-1]) else _accepted()

    inner: Any = transport._provider
    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    raised: Exception | None = None
    try:
        [
            frame
            async for frame in transport.stream(
                request,
                input_tokens=3,
                reasoning=ReasoningPolicy.on(budget_tokens=2048),
            )
        ]
    except Exception as error:  # the failure IS the observation
        raised = error
    finally:
        await inner.cleanup()
    return sent, raised


# --------------------------------------------------------------------------
# The regression, and the net
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refused_history_block_costs_this_request_and_is_learned() -> None:
    """The v8.12.2 failure, unchanged, plus the fact it now records.

    One retry that strips the top-level field, the same history, a classified
    400 -- and the host's own sentence remembered. That is the whole of this
    release's effect on the request that pays for it.
    """

    transport = _go_transport()
    sent, raised = await _drive(transport, _request(GO_TOOL_LOOP))

    assert raised is not None
    assert "400" in str(raised)
    # Exactly the ladder's one retry: no third attempt, and the retry differs
    # from the first only by the removed field.
    assert len(sent) == 2
    assert "thinking" in sent[0] and "thinking" not in sent[1]
    assert sent[0]["messages"] == sent[1]["messages"]
    # The fact is recorded for this model, from the host's own words.
    assert transport._provider._recovery_memory.history_thinking_refused_for(
        "claude-haiku-5-5"
    )


@pytest.mark.asyncio
async def test_the_next_request_to_that_model_leaves_the_block_out() -> None:
    """The net: request two carries no unsigned history block and is accepted."""

    transport = _go_transport()
    first, raised = await _drive(transport, _request(GO_TOOL_LOOP))
    assert raised is not None
    assert len(first) == 2

    second, raised = await _drive(transport, _request(GO_TOOL_LOOP))

    assert raised is None
    assert len(second) == 1
    # The block is gone from the history, and the body is the one every release
    # before 8.12.2 built for this host -- the tool-follow-up suppression
    # included, because the drop is what it depends on. That is the whole
    # meaning of "treat it as a host that signs": one flag, both halves, the
    # pre-8.12.2 bytes.
    assert second[0]["messages"][1]["content"] == [_tool_use()]
    assert "thinking" not in second[0]


@pytest.mark.asyncio
async def test_the_learned_refusal_is_scoped_to_the_model_that_refused() -> None:
    """A refusal on model A does not change model B on the same provider."""

    transport = _go_transport()
    await _drive(transport, _request(GO_TOOL_LOOP))

    sent, raised = await _drive(
        transport, _request(GO_TOOL_LOOP, model="deepseek-v4.1-flash")
    )

    assert raised is not None
    assert "400" in str(raised)
    assert sent[0]["messages"][1]["content"] == [_unsigned(), _tool_use()]


@pytest.mark.asyncio
async def test_a_host_that_signs_learns_nothing_and_moves_no_byte() -> None:
    """The premise is a host that signs nothing; everywhere else it is moot.

    A host that signs already drops these blocks, so a row for it could not
    change a body -- and writing one would record a fact about a decision MCC
    never made.
    """

    provider = AnthropicMessagesProvider(
        ProviderConfig(api_key="sk-test", base_url=GO_BASE_URL),
        provider_name="ANTHROPIC_TEST",
        rate_limiter=passthrough_rate_limiter(),
        provider_id="anthropic",
    )
    sent: list[dict[str, Any]] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(http_request.content))
        return _refusal(HISTORY_REFUSAL)

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(Exception) as raised:
        [
            frame
            async for frame in provider.stream_response(
                _request(GO_TOOL_LOOP),
                reasoning=ReasoningPolicy.on(budget_tokens=2048),
            )
        ]
    await provider.cleanup()

    assert "400" in str(raised.value)
    # One attempt: the build had already dropped the block, so the ladder found
    # no top-level field to strip and there was nothing to retry.
    assert len(sent) == 1
    assert sent[0]["messages"][1]["content"] == [_tool_use()]
    assert not provider._recovery_memory.history_thinking_refused_for(
        "claude-haiku-5-5"
    )


def test_the_writer_refuses_to_learn_for_a_host_that_signs() -> None:
    """The declaration is checked first, and it is what makes the fact inert.

    A signing host's build has already dropped these blocks, so the body
    handed in here cannot come out of the builder -- which is the point: the
    guard is the host's own declaration, not the shape of the body it happened
    to send. Without it, a row would be written about a decision MCC never
    made for that host, and the Models page would show a refusal that changed
    nothing.
    """

    provider = AnthropicMessagesProvider(
        ProviderConfig(api_key="sk-test", base_url=GO_BASE_URL),
        provider_name="ANTHROPIC_TEST",
        rate_limiter=passthrough_rate_limiter(),
        provider_id="anthropic",
    )
    provider._remember_refused_history_thinking(
        _http_400(HISTORY_REFUSAL),
        {"model": "claude-haiku-5-5", "messages": GO_TOOL_LOOP},
    )

    assert not provider._recovery_memory.history_thinking_refused_for(
        "claude-haiku-5-5"
    )


def test_a_signing_hosts_body_is_byte_identical_with_and_without_the_fact() -> None:
    """The reader short-circuits on the declaration, so 8.12.2's bytes stand.

    Even a row recorded against a signing host's model changes nothing: the
    first thing ``_signs_thinking_for`` reads is the profile's own answer.
    """

    store = LearnedFactStore(flush_debounce_seconds=0.0)
    store.record("anthropic", "claude-haiku-5-5", FACT_HISTORY_THINKING_REJECTED, True)
    set_learned_fact_store(store)
    try:
        provider = AnthropicMessagesProvider(
            ProviderConfig(api_key="sk-test", base_url=GO_BASE_URL),
            provider_name="ANTHROPIC_TEST",
            rate_limiter=passthrough_rate_limiter(),
            provider_id="anthropic",
        )
        for policy in (
            ReasoningPolicy.on(budget_tokens=2048),
            ReasoningPolicy.provider_default(),
            ReasoningPolicy.off(),
        ):
            assert provider._signs_thinking_for("claude-haiku-5-5") is True
            assert build_anthropic_messages_body(
                _request(GO_TOOL_LOOP),
                reasoning=policy,
                signs_thinking=provider._signs_thinking_for("claude-haiku-5-5"),
            ) == build_anthropic_messages_body(
                _request(GO_TOOL_LOOP),
                reasoning=policy,
                signs_thinking=True,
            )
    finally:
        reset_learned_fact_store()


# --------------------------------------------------------------------------
# The evidence bar, one negative per condition
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_complaint_outside_the_history_learns_nothing() -> None:
    """The ``messages.N.`` location is the evidence, not the word ``thinking``.

    A top-level field refusal is what the strip ladder already answers; turning
    it into this fact would drop the host's own thinking for nothing.
    """

    transport = _go_transport()
    sent: list[dict[str, Any]] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(http_request.content))
        if "thinking" in sent[-1]:
            return _refusal("thinking: Extra inputs are not permitted")
        return _accepted()

    inner: Any = transport._provider
    inner._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    frames = [
        frame
        async for frame in transport.stream(
            _request(GO_TOOL_LOOP),
            input_tokens=3,
            reasoning=ReasoningPolicy.on(budget_tokens=2048),
        )
    ]
    await inner.cleanup()

    # The ladder still recovers this one, exactly as it always did.
    assert any("message_stop" in frame for frame in frames)
    assert len(sent) == 2
    assert not inner._recovery_memory.history_thinking_refused_for("claude-haiku-5-5")


@pytest.mark.asyncio
async def test_a_history_complaint_that_names_something_else_learns_nothing() -> None:
    """``messages.N.`` alone is not evidence about a thinking block."""

    transport = _go_transport()
    sent, raised = await _drive(
        transport,
        _request(GO_TOOL_LOOP),
        refusal="messages.1.content.0: unexpected `tool_use` id",
    )

    assert raised is not None
    # One attempt: the ladder names no top-level reasoning field here, so it
    # does not even retry -- unchanged behaviour, and no fact either way.
    assert len(sent) == 1
    assert not transport._provider._recovery_memory.history_thinking_refused_for(
        "claude-haiku-5-5"
    )


@pytest.mark.asyncio
async def test_a_complaint_about_a_block_that_was_not_there_learns_nothing() -> None:
    """``Expected `thinking` but found `text``` is the opposite complaint.

    It is located in the history and it names the block, so the first two
    conditions hold -- and dropping blocks is exactly the wrong answer to it.
    Requiring the history MCC sent to have carried an unsigned block is what
    keeps the fact meaning "this host refused what we sent".
    """

    transport = _go_transport()
    history = [
        _user("Read main.py."),
        _assistant(_text("no thinking here"), _tool_use()),
        _user([_tool_result()]),
    ]
    sent, raised = await _drive(
        transport,
        _request(history),
        refusal="messages.1.content.0.type: Expected `thinking` but found `text`",
        refuses=lambda body: True,
    )

    assert raised is not None
    assert sent
    assert not transport._provider._recovery_memory.history_thinking_refused_for(
        "claude-haiku-5-5"
    )


# --------------------------------------------------------------------------
# The matcher itself
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("complaint", "expected"),
    [
        ("messages.1.content.0: Invalid `signature` in `thinking` block", True),
        ("messages.1.content.0: `thinking` blocks are not accepted", True),
        ("messages.12.content.2: thinking block signature is required", True),
        # ``_`` is a word character, so ``redacted_thinking`` has no boundary
        # before ``thinking`` and is deliberately not matched: its ``data`` is
        # opaque and no signature rule would answer a refusal of one.
        ("messages.1.content.0: invalid `signature` in `redacted_thinking`", False),
        ("thinking: Extra inputs are not permitted", False),
        ("thinking.adaptive.enabled: Extra inputs are not permitted", False),
        ("messages.1.content.0: unexpected `tool_use` id", False),
    ],
)
def test_the_matcher_needs_a_history_location_and_a_thinking_block(
    complaint: str, expected: bool
) -> None:
    assert complaint_refuses_a_history_thinking_block(complaint) is expected


@pytest.mark.parametrize(
    ("messages", "expected"),
    [
        ([_assistant(_unsigned())], True),
        ([_assistant({"type": "thinking", "thinking": "x", "signature": ""})], True),
        ([_assistant({"type": "thinking", "thinking": "x", "signature": "  "})], True),
        ([_assistant(_signed())], False),
        ([_user(_unsigned())], False),
        ([_assistant(_text("plain"))], False),
        ([{"role": "assistant", "content": "plain"}], False),
        (None, False),
    ],
)
def test_the_history_must_actually_carry_an_unsigned_block(
    messages: Any, expected: bool
) -> None:
    assert history_carries_unsigned_thinking(messages) is expected


# --------------------------------------------------------------------------
# The fact: durable, scoped, expiring, visible
# --------------------------------------------------------------------------


def test_the_fact_survives_a_restart_and_reaches_the_history_trim() -> None:
    """Durable, per ``(provider, model)``, and read back into the trim."""

    store = LearnedFactStore(flush_debounce_seconds=0.0)
    store.record(
        "opencode_go",
        "deepseek-v4.1-flash",
        FACT_HISTORY_THINKING_REJECTED,
        True,
        evidence="messages.1.content.0: invalid `signature` in `thinking` block",
    )
    reopened = LearnedFactStore(flush_debounce_seconds=0.0)
    reopened.load_document(store.as_document())
    set_learned_fact_store(reopened)
    try:
        provider = create_openai_chat_provider(
            "opencode_go",
            ProviderConfig(api_key="sk-test", base_url=GO_BASE_URL),
            passthrough_rate_limiter(),
        )._messages._provider
        assert provider._signs_thinking_for("deepseek-v4.1-flash") is True
        assert provider._signs_thinking_for("another-model") is False
        # And the body it builds is the pre-8.12.2 one again.
        body = build_anthropic_messages_body(
            _request(GO_TOOL_LOOP, model="deepseek-v4.1-flash"),
            reasoning=ReasoningPolicy.on(budget_tokens=2048),
            signs_thinking=provider._signs_thinking_for("deepseek-v4.1-flash"),
        )
        assert body["messages"][1]["content"] == [_tool_use()]
        assert "thinking" not in body
    finally:
        reset_learned_fact_store()


def test_forgetting_the_fact_restores_the_8_12_2_body() -> None:
    """Forget is the operator's way back, and it reaches the live provider."""

    store = LearnedFactStore(flush_debounce_seconds=0.0)
    # The production order: the document is loaded before any provider is
    # built, so ``memory_for`` is populated from it.
    store.record(
        "opencode_go", "deepseek-v4.1-flash", FACT_HISTORY_THINKING_REJECTED, True
    )
    set_learned_fact_store(store)
    try:
        provider = create_openai_chat_provider(
            "opencode_go",
            ProviderConfig(api_key="sk-test", base_url=GO_BASE_URL),
            passthrough_rate_limiter(),
        )._messages._provider
        assert provider._signs_thinking_for("deepseek-v4.1-flash") is True

        assert store.forget("opencode_go", "deepseek-v4.1-flash") == 1

        # Forget reaches the live provider, not just the file.
        assert provider._signs_thinking_for("deepseek-v4.1-flash") is False
    finally:
        reset_learned_fact_store()


def test_a_stale_fact_stops_applying() -> None:
    """Expiry is the self-healing property, not deletion."""

    store = LearnedFactStore(flush_debounce_seconds=0.0)
    fact = store.record(
        "opencode_go", "deepseek-v4.1-flash", FACT_HISTORY_THINKING_REJECTED, True
    )
    assert fact is not None
    assert fact.ttl_seconds == INFERRED_FACT_TTL_SECONDS
    now = datetime.now(UTC)
    assert fact.is_stale(now + timedelta(days=6)) is False
    assert fact.is_stale(now + timedelta(days=8)) is True


def test_the_kind_is_allowed_and_labelled() -> None:
    """A kind that lands in the document must be readable back and named."""

    from my_claude_code.api.model_admin import (
        FACT_KIND_LABELS,
        PAGE_TABLE_UNLISTED_FACT_KINDS,
    )

    assert FACT_HISTORY_THINKING_REJECTED in ALLOWED_FACT_KINDS
    assert FACT_HISTORY_THINKING_REJECTED in FACT_KIND_LABELS
    # The page payload's ``fact_labels`` table is pinned byte for byte; a
    # learned row still carries its own label from the full table above.
    assert FACT_HISTORY_THINKING_REJECTED in PAGE_TABLE_UNLISTED_FACT_KINDS


def test_the_learned_row_is_what_the_page_renders() -> None:
    """The Models page's Learned column shows this row like any other."""

    from my_claude_code.api.model_admin import learned_payload

    store = LearnedFactStore(flush_debounce_seconds=0.0)
    fact = store.record(
        "opencode_go", "deepseek-v4.1-flash", FACT_HISTORY_THINKING_REJECTED, True
    )
    assert fact is not None
    payload = learned_payload(fact.as_row() | {"age_seconds": 1.0})

    assert payload["fact_kind"] == FACT_HISTORY_THINKING_REJECTED
    assert payload["fact_label"] == "history thinking refused"
    assert payload["model_id"] == "deepseek-v4.1-flash"
    assert payload["source_label"] == "the host's own rejection"
