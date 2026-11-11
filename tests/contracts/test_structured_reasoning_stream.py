"""The one response-direction reasoning-detail implementation (8.20.0).

Before this release two implementations carried two different rule sets: the
wired ``_iter_reasoning_detail_events`` in ``providers/openrouter_gateway.py``
and a never-instantiated ``StructuredReasoningStream`` in
``providers/openai_chat/reasoning_details.py``. The live one was narrower in
three measured places, so the wider rules are the ones kept.

Every case here is a fake upstream: a synthetic delta through the real
function, or a scripted ``chat.completions.create`` through the real provider.
No network, no server, no live call.
"""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from my_claude_code.config import settings as config_settings
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES
from my_claude_code.providers.openai_chat.reasoning_details import (
    iter_reasoning_detail_events,
)
from my_claude_code.providers.openrouter_gateway import openrouter_gateway_profile
from tests.providers.request_factory import make_messages_request
from tests.providers.support import passthrough_rate_limiter, reasoning_for

#: The five profiles Q3 names, and the one place each one's flag lives.
FIVE_PROFILES = ("cline", "zenmux", "open_router", "nous_portal", "kilo")


class Delta:
    """An OpenAI chat delta: declared fields as attributes, extras in ``model_extra``.

    The same split a pydantic delta has: ``reasoning_content`` and
    ``reasoning_details`` are attributes when the SDK declares them, and an
    undeclared field is reachable only through ``model_extra``.
    """

    def __init__(
        self, *, model_extra: dict[str, Any] | None = None, **kwargs: Any
    ) -> None:
        self._extra = dict(kwargs)
        self._model_extra = dict(model_extra or {})

    def __getattr__(self, name: str) -> Any:
        try:
            return self._extra[name]
        except KeyError:
            raise AttributeError(name) from None

    @property
    def model_extra(self) -> dict[str, Any]:
        return self._model_extra


class AsyncStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks
        self.closed = False

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for chunk in self._chunks:
            yield chunk


def _chunk(
    *,
    content: str | None = None,
    reasoning_content: str | None = None,
    reasoning: str | None = None,
    reasoning_details: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
) -> Any:
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        reasoning=reasoning,
        tool_calls=None,
    )
    if reasoning_details is not None:
        delta.reasoning_details = reasoning_details
    choice = SimpleNamespace(delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], usage=None)


def _events(
    deltas: list[Delta], *, structured: bool, native: str | None = None
) -> list[str]:
    ledger = AnthropicStreamLedger("msg_test", "m")
    out: list[str] = []
    for delta in deltas:
        out.extend(
            iter_reasoning_detail_events(
                delta,
                ledger,
                native_reasoning=native if native is not None else _native(delta),
                structured=structured,
            )
        )
    return out


def _native(delta: Delta) -> str | None:
    value = getattr(delta, "reasoning_content", None)
    return value if isinstance(value, str) else None


def _payloads(events: list[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for raw in events:
        out.extend(
            json.loads(line[5:].strip())
            for line in raw.splitlines()
            if line.startswith("data:")
        )
    return out


def _thinking_text(events: list[str]) -> str:
    parts: list[str] = []
    for payload in _payloads(events):
        delta = payload.get("delta") or {}
        if delta.get("type") == "thinking_delta":
            parts.append(str(delta.get("thinking", "")))
    return "".join(parts)


def _redacted_blocks(events: list[str]) -> list[str]:
    return [
        str(payload["content_block"]["data"])
        for payload in _payloads(events)
        if payload.get("type") == "content_block_start"
        and (payload.get("content_block") or {}).get("type") == "redacted_thinking"
    ]


@pytest.fixture
def gate(monkeypatch):
    """Set ``CHAT_STRUCTURED_REASONING`` the way an operator's Save would."""

    def _set(value: bool | None) -> None:
        if value is None:
            monkeypatch.delenv("CHAT_STRUCTURED_REASONING", raising=False)
        else:
            monkeypatch.setenv(
                "CHAT_STRUCTURED_REASONING", "true" if value else "false"
            )
        config_settings.get_settings.cache_clear()

    yield _set
    config_settings.get_settings.cache_clear()


def _provider(provider_id: str):
    from my_claude_code.providers.kilo.client import KiloProvider
    from my_claude_code.providers.nous_portal.client import NousPortalProvider
    from my_claude_code.providers.open_router.client import OpenRouterProvider

    config = ProviderConfig(
        api_key=f"test-{provider_id}-key",
        base_url="https://example.invalid/api/v1",
        rate_limit=10,
        rate_window=60,
    )
    limiter = passthrough_rate_limiter()
    if provider_id == "open_router":
        return OpenRouterProvider(config, rate_limiter=limiter)
    if provider_id == "nous_portal":
        return NousPortalProvider(config, rate_limiter=limiter)
    if provider_id == "kilo":
        return KiloProvider(config, rate_limiter=limiter)
    from my_claude_code.providers.openai_chat import create_openai_chat_provider

    return create_openai_chat_provider(provider_id, config, limiter)


async def _stream_events(provider: Any, chunks: list[Any]) -> str:
    request = make_messages_request()
    stream = AsyncStream(chunks)
    with patch.object(
        provider._client.chat.completions,
        "create",
        new_callable=AsyncMock,
        return_value=stream,
    ):
        return "".join(
            [
                event
                async for event in provider.stream_response(
                    request, reasoning=reasoning_for(request)
                )
            ]
        )


# --------------------------------------------------------------------------
# Rule 1 -- a detail carrying a signature
# --------------------------------------------------------------------------


def test_a_signature_bearing_detail_is_preserved_as_a_block():
    """The opaque continuation survives, and the text still reaches the client."""

    detail = {"type": "reasoning.text", "text": "plan", "signature": "SIG"}
    events = _events([Delta(reasoning_details=[detail])], structured=True)

    assert _redacted_blocks(events) == [json.dumps(detail, separators=(",", ":"))]
    assert _thinking_text(events) == "plan"


def test_the_narrow_rules_lose_the_signature_exactly_as_v8190_did():
    """The off state is the old behaviour: text out, entry gone."""

    detail = {"type": "reasoning.text", "text": "plan", "signature": "SIG"}
    events = _events([Delta(reasoning_details=[detail])], structured=False)

    assert _redacted_blocks(events) == []
    assert _thinking_text(events) == "plan"
    assert "SIG" not in "".join(events)


# --------------------------------------------------------------------------
# Rule 2 -- a detail with no text and no marker
# --------------------------------------------------------------------------


def test_a_detail_with_no_text_and_no_marker_is_preserved():
    detail = {"type": "reasoning", "id": "r1"}
    events = _events([Delta(reasoning_details=[detail])], structured=True)

    assert _redacted_blocks(events) == [json.dumps(detail, separators=(",", ":"))]
    assert _thinking_text(events) == ""


def test_the_narrow_rules_drop_a_marker_less_detail_entirely():
    events = _events(
        [Delta(reasoning_details=[{"type": "reasoning", "id": "r1"}])],
        structured=False,
    )
    assert events == []


# --------------------------------------------------------------------------
# Rule 3 -- a summary detail that also carries text
# --------------------------------------------------------------------------


def test_a_summary_detail_that_carries_text_emits_both():
    detail = {"type": "summary", "text": "sum"}
    events = _events([Delta(reasoning_details=[detail])], structured=True)

    assert _redacted_blocks(events) == [json.dumps(detail, separators=(",", ":"))]
    assert _thinking_text(events) == "sum"


def test_the_narrow_rules_keep_only_the_block_for_a_summary_detail():
    events = _events(
        [Delta(reasoning_details=[{"type": "summary", "text": "sum"}])],
        structured=False,
    )
    assert len(_redacted_blocks(events)) == 1
    assert _thinking_text(events) == ""


# --------------------------------------------------------------------------
# The arbitration -- per delta, never latched
# --------------------------------------------------------------------------


def test_the_text_source_is_recomputed_per_delta():
    """Delta 1 native, delta 2 details-text: the details-text IS emitted.

    The dead class latched one source per stream and would drop ``BETA``. That
    is a loss, and it is output the live hook emits today, so the per-delta
    recomputation is the behaviour kept. See the module docstring of
    ``providers/openai_chat/reasoning_details.py``.
    """

    events = _events(
        [
            Delta(reasoning_content="ALPHA"),
            Delta(reasoning_details=[{"type": "reasoning.text", "text": "BETA"}]),
        ],
        structured=True,
    )

    assert _thinking_text(events) == "BETA"


def test_a_delta_carrying_both_sources_emits_the_detail_text_once():
    """The double-emission guard: the caller already emitted the native text."""

    events = _events(
        [
            Delta(
                reasoning_content="ALPHA",
                reasoning_details=[{"type": "reasoning.text", "text": "ALPHA"}],
            )
        ],
        structured=True,
    )

    assert _thinking_text(events) == ""


def test_a_signature_detail_on_a_native_delta_still_keeps_its_block():
    detail = {"type": "reasoning.text", "text": "plan", "signature": "SIG"}
    events = _events(
        [Delta(reasoning_content="ALPHA", reasoning_details=[detail])],
        structured=True,
    )

    assert _redacted_blocks(events) == [json.dumps(detail, separators=(",", ":"))]
    assert _thinking_text(events) == ""


def test_details_are_read_from_model_extra_when_undeclared():
    events = _events(
        [
            Delta(
                model_extra={
                    "reasoning_details": [{"type": "reasoning.text", "text": "plan"}]
                }
            )
        ],
        structured=True,
    )
    assert _thinking_text(events) == "plan"


# --------------------------------------------------------------------------
# The declaration -- all five profiles
# --------------------------------------------------------------------------


@pytest.mark.parametrize("provider_id", FIVE_PROFILES)
def test_every_named_profile_declares_structured_reasoning_details(provider_id):
    if provider_id in {"open_router", "nous_portal", "kilo"}:
        profile = openrouter_gateway_profile(provider_id.upper())
    else:
        profile = OPENAI_CHAT_PROFILES[provider_id]
    assert profile.structured_reasoning_details is True


@pytest.mark.parametrize(
    "details",
    [
        [{"type": "reasoning.text", "text": "plan", "signature": "SIG"}],
        [{"type": "reasoning", "id": "r1"}],
        [{"type": "summary", "text": "sum"}],
        [{"type": "reasoning.encrypted", "data": "opaque"}],
    ],
    ids=["signature", "no-text", "summary-text", "encrypted"],
)
@pytest.mark.parametrize("gate_on", [False, True], ids=["gate-off", "gate-on"])
def test_a_profile_that_declares_nothing_produces_no_events(details, gate_on, gate):
    """Every other Chat host is byte-identical, with the gate on or off.

    The three shapes the wider rules would move, plus the one both rule sets
    already preserve, through a profile that declares nothing: no events at
    all, either way.
    """

    gate(gate_on)
    provider = _provider("zai")
    assert provider._profile.structured_reasoning_details is False
    delta = Delta(reasoning_details=details)
    ledger = AnthropicStreamLedger("msg_test", "m")
    events = list(
        provider._handle_extra_reasoning(
            delta, ledger, output_reasoning=True, native_reasoning=None
        )
    )
    assert events == []


# --------------------------------------------------------------------------
# The gate -- off is v8.19.0, on is the wider rules
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", FIVE_PROFILES)
async def test_the_gate_off_keeps_the_narrow_rules(provider_id, gate):
    gate(False)
    provider = _provider(provider_id)
    detail = {"type": "reasoning.text", "text": "plan", "signature": "SIG"}
    text = await _stream_events(
        provider,
        [
            # ``reasoning`` is the delta field ``cline`` and ``zenmux`` declare;
            # ``reasoning_content`` is the gateways'. Both are sent so the same
            # fixture drives all five.
            _chunk(reasoning="", reasoning_content="", reasoning_details=[detail]),
            _chunk(content="done", finish_reason="stop"),
        ],
    )
    assert "redacted_thinking" not in text
    assert "plan" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_id", FIVE_PROFILES)
async def test_the_gate_on_applies_the_wider_rules(provider_id, gate):
    gate(True)
    provider = _provider(provider_id)
    detail = {"type": "reasoning.text", "text": "plan", "signature": "SIG"}
    text = await _stream_events(
        provider,
        [
            _chunk(reasoning="", reasoning_content="", reasoning_details=[detail]),
            _chunk(content="done", finish_reason="stop"),
        ],
    )
    assert "redacted_thinking" in text
    assert "SIG" in text


@pytest.mark.asyncio
async def test_the_gate_defaults_off(gate):
    gate(None)
    provider = _provider("open_router")
    detail = {"type": "reasoning.text", "text": "plan", "signature": "SIG"}
    text = await _stream_events(
        provider,
        [
            _chunk(reasoning_details=[detail]),
            _chunk(content="done", finish_reason="stop"),
        ],
    )
    assert "redacted_thinking" not in text


@pytest.mark.asyncio
async def test_the_gateway_stream_still_maps_reasoning_content_and_details(gate):
    """The v8.19.0 regression guard, unmodified in substance."""

    gate(None)
    provider = _provider("open_router")
    text = await _stream_events(
        provider,
        [
            _chunk(reasoning_content="plan "),
            _chunk(
                reasoning_details=[{"type": "reasoning.encrypted", "data": "opaque"}]
            ),
            _chunk(content="done", finish_reason="stop"),
        ],
    )
    assert "thinking_delta" in text
    assert "plan " in text
    assert "redacted_thinking" in text
    assert "opaque" in text


@pytest.mark.asyncio
async def test_cline_does_not_emit_its_native_text_twice(gate):
    """``cline`` declares ``reasoning`` as its delta field.

    The hook must de-duplicate against the text the caller actually emitted,
    not against a field this profile never uses -- otherwise a delta carrying
    both ``reasoning`` and a detail with the same text would send it twice.
    """

    gate(False)
    provider = _provider("cline")
    text = await _stream_events(
        provider,
        [
            _chunk(
                reasoning="ALPHA",
                reasoning_details=[{"type": "reasoning.text", "text": "ALPHA"}],
            ),
            _chunk(content="done", finish_reason="stop"),
        ],
    )
    assert text.count("ALPHA") == 1


def test_the_anti_echo_ban_holds_for_every_rule():
    """No ``think`` tag is ever written, on any shape, in either rule set."""

    shapes = [
        [{"type": "reasoning.text", "text": "plan", "signature": "SIG"}],
        [{"type": "reasoning", "id": "r1"}],
        [{"type": "summary", "text": "sum"}],
        [{"type": "reasoning.encrypted", "data": "opaque"}],
    ]
    for structured in (False, True):
        for details in shapes:
            events = _events([Delta(reasoning_details=details)], structured=structured)
            joined = "".join(events)
            assert "<think" not in joined
            assert "</think>" not in joined


def test_the_dead_class_is_gone():
    """One implementation, not two: the duplicate must not come back."""

    import my_claude_code.providers.openrouter_gateway as gateway
    from my_claude_code.providers.openai_chat import reasoning_details

    assert not hasattr(reasoning_details, "StructuredReasoningStream")
    assert not hasattr(gateway, "_iter_reasoning_detail_events")
