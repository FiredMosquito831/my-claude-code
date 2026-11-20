"""Mistral wide per-model reasoning dialect, retry persistence, and fidelity tests."""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import openai
import pytest

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.config.model_overrides import (
    ModelParameterOverrides,
    save_model_overrides,
)
from my_claude_code.config.provider_catalog import MISTRAL_DEFAULT_BASE
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.reasoning import (
    ReasoningDialectOrigin,
    ReasoningEffort,
    ReasoningPolicy,
    narrow_dialect_by_rejections,
)
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.mistral import MistralProvider
from my_claude_code.providers.mistral.client import MISTRAL_REASONING_DIALECT
from my_claude_code.providers.mistral.reasoning import (
    apply_mistral_reasoning_request_shape,
    normalize_mistral_chunk,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.recovery import (
    FACT_EFFORT_VALUE_REJECTED,
    FACT_HISTORY_THINKING_REJECTED,
    FACT_REASONING_FIELD_REJECTED,
)
from my_claude_code.providers.recovery.store import (
    LearnedFactStore,
    set_learned_fact_store,
)
from tests.providers.request_factory import make_messages_request
from tests.providers.support import REASONING_OFF

_MODEL = "fixture-wide-model"
_OTHER_MODEL = "fixture-other-model"


def _config() -> ProviderConfig:
    return ProviderConfig(
        api_key="test_mistral_key",
        base_url=MISTRAL_DEFAULT_BASE,
        rate_limit=10,
        rate_window=60,
    )


def _provider(provider_id: str = "mistral") -> MistralProvider:
    return MistralProvider(
        _config(),
        rate_limiter=ProviderRateLimiter(rate_limit=100, rate_window=60),
        provider_id=provider_id,
    )


def _bad_request(message: str, *, status_code: int = 400) -> Exception:
    request = httpx.Request("POST", "https://api.mistral.ai/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    body = {"error": {"message": message}}
    if status_code == 422:
        return openai.UnprocessableEntityError(message, response=response, body=body)
    return openai.BadRequestError(message, response=response, body=body)


class _FakeCreate:
    """Scripted async completions.create double recording outbound kwargs."""

    def __init__(self, outcomes: list[Exception | list[Any]]) -> None:
        self._outcomes = outcomes
        self.bodies: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> Any:
        self.bodies.append(dict(kwargs))
        index = len(self.bodies) - 1
        outcome = self._outcomes[index] if index < len(self._outcomes) else []
        if isinstance(outcome, Exception):
            raise outcome
        chunks = outcome

        async def _stream() -> Any:
            for chunk in chunks:
                yield chunk

        return _stream()


def _ok_chunk(text: str = "ok") -> SimpleNamespace:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=text,
                    reasoning_content=None,
                    tool_calls=None,
                ),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2),
    )


def _install(provider: MistralProvider, create: _FakeCreate) -> None:
    client: Any = provider._client
    client.chat.completions.create = create


@pytest.fixture
def durable_store(tmp_path):
    opened = LearnedFactStore(flush_debounce_seconds=0.0)
    opened.enable_persistence(tmp_path / "learned_facts.json")
    set_learned_fact_store(opened)
    yield opened
    set_learned_fact_store(LearnedFactStore())


# ---------------------------------------------------------------------------
# 1. Wide 6-rung dialect + per-model capability & learned narrowing
# ---------------------------------------------------------------------------


def test_mistral_dialect_declares_all_six_rungs_and_off() -> None:
    assert MISTRAL_REASONING_DIALECT.effort_values == frozenset(ReasoningEffort)
    assert MISTRAL_REASONING_DIALECT.off is True
    assert MISTRAL_REASONING_DIALECT.effort_field == "reasoning_effort"
    assert MISTRAL_REASONING_DIALECT.origin is ReasoningDialectOrigin.DECLARED


@pytest.mark.parametrize("effort", list(ReasoningEffort))
def test_mistral_provider_emits_every_effort_rung_natively(
    effort: ReasoningEffort,
) -> None:
    provider = _provider()
    req = make_messages_request(_MODEL)
    body = provider._build_request_body(
        req, reasoning=ReasoningPolicy.on(effort=effort)
    )
    assert body["reasoning_effort"] == effort.value


def test_mistral_provider_preserves_baseline_for_on_without_effort_off_and_adaptive() -> (
    None
):
    provider = _provider()
    req = make_messages_request(_MODEL)

    assert (
        provider._build_request_body(req, reasoning=ReasoningPolicy.on())[
            "reasoning_effort"
        ]
        == "high"
    )
    assert (
        provider._build_request_body(req, reasoning=REASONING_OFF)["reasoning_effort"]
        == "none"
    )
    assert "reasoning_effort" not in provider._build_request_body(
        req, reasoning=ReasoningPolicy.provider_default()
    )
    assert "reasoning_effort" not in provider._build_request_body(
        req, reasoning=ReasoningPolicy.adaptive()
    )


# ---------------------------------------------------------------------------
# 2. "none"-only narrowing & RecoveryMemory persistence across rebuild
# ---------------------------------------------------------------------------


def test_narrow_dialect_none_only_clears_off_while_keeping_all_efforts() -> None:
    narrowed = narrow_dialect_by_rejections(
        MISTRAL_REASONING_DIALECT, {}, {"reasoning_effort": {"none"}}
    )
    assert narrowed.off is False
    assert narrowed.effort_field == "reasoning_effort"
    assert narrowed.effort_values == frozenset(ReasoningEffort)
    assert narrowed.effort_values is not None
    assert ReasoningEffort.MAX in narrowed.effort_values
    assert ReasoningEffort.XHIGH in narrowed.effort_values
    assert narrowed.origin is ReasoningDialectOrigin.LEARNED


def test_narrow_dialect_none_plus_unknown_word_still_falls_back_to_coarse() -> None:
    narrowed = narrow_dialect_by_rejections(
        MISTRAL_REASONING_DIALECT, {}, {"reasoning_effort": {"none", "detailed"}}
    )
    assert narrowed.off is False
    assert narrowed.effort_values is None
    assert narrowed.effort_field == ""


def test_narrow_dialect_coarse_field_rejection_clears_off_too() -> None:
    narrowed = narrow_dialect_by_rejections(
        MISTRAL_REASONING_DIALECT, {"reasoning_effort": "2026-11-20"}
    )
    assert narrowed.off is False
    assert narrowed.effort_values is None
    assert narrowed.effort_field == ""


@pytest.mark.asyncio
async def test_mistral_persists_xhigh_value_rejection_per_model_across_rebuild(
    durable_store, tmp_path
) -> None:
    provider = _provider("mistral")
    req = make_messages_request(_MODEL)
    body = provider._build_request_body(
        req, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.XHIGH)
    )
    assert body["reasoning_effort"] == "xhigh"

    create = _FakeCreate(
        [
            _bad_request("Invalid value 'xhigh' for reasoning_effort"),
            [_ok_chunk()],
        ]
    )
    _install(provider, create)

    await provider._create_stream(body)
    assert len(create.bodies) == 2
    assert "reasoning_effort" not in create.bodies[1]
    durable_store.flush()

    reopened = LearnedFactStore(flush_debounce_seconds=0.0)
    reopened.enable_persistence(tmp_path / "learned_facts.json")
    set_learned_fact_store(reopened)

    rebuilt = _provider("mistral")
    narrowed = rebuilt.reasoning_dialect(_MODEL)
    assert narrowed.effort_values is not None
    assert ReasoningEffort.XHIGH not in narrowed.effort_values
    assert ReasoningEffort.HIGH in narrowed.effort_values
    assert ReasoningEffort.MAX in narrowed.effort_values
    assert rebuilt.reasoning_dialect(_OTHER_MODEL) is MISTRAL_REASONING_DIALECT

    turn2_body = rebuilt._build_request_body(
        req, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.XHIGH)
    )
    assert turn2_body["reasoning_effort"] == "high"
    assert any(
        f.fact_kind == FACT_EFFORT_VALUE_REJECTED
        and f.model_id == _MODEL
        and f.detail == "reasoning_effort=xhigh"
        for f in reopened.all_facts()
    )


@pytest.mark.asyncio
async def test_mistral_persists_none_rejection_without_losing_max_or_high(
    durable_store, tmp_path
) -> None:
    provider = _provider("mistral")
    req = make_messages_request(_MODEL)
    off_body = provider._build_request_body(req, reasoning=REASONING_OFF)
    assert off_body["reasoning_effort"] == "none"

    create = _FakeCreate(
        [
            _bad_request("Invalid value 'none' for reasoning_effort"),
            [_ok_chunk()],
        ]
    )
    _install(provider, create)
    await provider._create_stream(off_body)
    durable_store.flush()

    reopened = LearnedFactStore(flush_debounce_seconds=0.0)
    reopened.enable_persistence(tmp_path / "learned_facts.json")
    set_learned_fact_store(reopened)
    rebuilt = _provider("mistral")

    assert rebuilt.reasoning_dialect(_MODEL).off is False
    assert "reasoning_effort" not in rebuilt._build_request_body(
        req, reasoning=REASONING_OFF
    )
    assert (
        rebuilt._build_request_body(
            req, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX)
        )["reasoning_effort"]
        == "max"
    )
    assert (
        rebuilt._build_request_body(
            req, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
        )["reasoning_effort"]
        == "high"
    )


@pytest.mark.asyncio
async def test_mistral_persists_coarse_field_rejection_and_omits_on_turn_2(
    durable_store, tmp_path
) -> None:
    provider = _provider("mistral")
    req = make_messages_request(_MODEL)
    body = provider._build_request_body(
        req, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
    )
    create = _FakeCreate(
        [
            _bad_request("Unsupported field: reasoning_effort"),
            [_ok_chunk()],
        ]
    )
    _install(provider, create)
    await provider._create_stream(body)
    durable_store.flush()

    reopened = LearnedFactStore(flush_debounce_seconds=0.0)
    reopened.enable_persistence(tmp_path / "learned_facts.json")
    set_learned_fact_store(reopened)
    rebuilt = _provider("mistral")

    assert any(
        f.fact_kind == FACT_REASONING_FIELD_REJECTED and f.model_id == _MODEL
        for f in reopened.all_facts()
    )
    assert "reasoning_effort" not in rebuilt._build_request_body(
        req, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
    )
    assert "reasoning_effort" not in rebuilt._build_request_body(
        req, reasoning=REASONING_OFF
    )
    assert rebuilt.reasoning_dialect(_OTHER_MODEL) is MISTRAL_REASONING_DIALECT


@pytest.mark.asyncio
async def test_mistral_persists_enum_list_rejection_across_rebuild(
    durable_store, tmp_path
) -> None:
    provider = _provider("mistral")
    req = make_messages_request(_MODEL)
    body = provider._build_request_body(
        req, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX)
    )
    create = _FakeCreate(
        [
            _bad_request(
                "Invalid value 'max' for reasoning_effort. "
                "Supported values are: 'low', 'medium', 'high'"
            ),
            [_ok_chunk()],
        ]
    )
    _install(provider, create)
    await provider._create_stream(body)
    durable_store.flush()

    reopened = LearnedFactStore(flush_debounce_seconds=0.0)
    reopened.enable_persistence(tmp_path / "learned_facts.json")
    set_learned_fact_store(reopened)
    rebuilt = _provider("mistral")

    dialect = rebuilt.reasoning_dialect(_MODEL)
    assert dialect.effort_values == frozenset(
        {ReasoningEffort.LOW, ReasoningEffort.MEDIUM, ReasoningEffort.HIGH}
    )
    assert (
        rebuilt._build_request_body(
            req, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX)
        )["reasoning_effort"]
        == "high"
    )


@pytest.mark.asyncio
async def test_mistral_persists_history_thinking_rejection_and_omits_upfront_on_turn_2(
    durable_store, tmp_path
) -> None:
    provider = _provider("mistral")
    req = make_messages_request(
        _MODEL,
        system=None,
        messages=[
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "Internal step."},
                    {"type": "text", "text": "Visible answer."},
                ],
            },
            {"role": "user", "content": "Continue."},
        ],
    )
    turn1_body = provider._build_request_body(
        req, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
    )
    assert turn1_body["messages"][0]["content"][0]["type"] == "thinking"

    create = _FakeCreate(
        [
            _bad_request("Reasoning input is not enabled for this model"),
            [_ok_chunk("Turn 1 ok")],
        ]
    )
    _install(provider, create)
    events = [
        e
        async for e in provider.stream_response(
            req, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
        )
    ]
    assert len(create.bodies) == 2
    assert any("Turn 1 ok" in e for e in events)
    durable_store.flush()

    reopened = LearnedFactStore(flush_debounce_seconds=0.0)
    reopened.enable_persistence(tmp_path / "learned_facts.json")
    set_learned_fact_store(reopened)
    assert any(
        f.fact_kind == FACT_HISTORY_THINKING_REJECTED and f.model_id == _MODEL
        for f in reopened.all_facts()
    )

    rebuilt = _provider("mistral")
    turn2_body = rebuilt._build_request_body(
        req, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
    )
    assert turn2_body["messages"][0]["content"] == "Visible answer."
    # reasoning_effort itself was NOT rejected and stays intact.
    assert turn2_body["reasoning_effort"] == "high"
    # Other models on the same provider keep history replay intact.
    other_body = rebuilt._build_request_body(
        make_messages_request(
            _OTHER_MODEL,
            system=None,
            messages=req.model_dump()["messages"],
        ),
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
    )
    assert other_body["messages"][0]["content"][0]["type"] == "thinking"


@pytest.mark.asyncio
async def test_mistral_failed_retry_teaches_nothing(durable_store) -> None:
    provider = _provider("mistral")
    req = make_messages_request(_MODEL)
    body = provider._build_request_body(
        req, reasoning=ReasoningPolicy.on(effort=ReasoningEffort.XHIGH)
    )
    create = _FakeCreate(
        [
            _bad_request("Invalid value 'xhigh' for reasoning_effort"),
            _bad_request("Invalid value 'xhigh' for reasoning_effort"),
        ]
    )
    _install(provider, create)
    with pytest.raises(openai.BadRequestError):
        await provider._create_stream(body)

    assert durable_store.all_facts() == ()
    assert provider.reasoning_dialect(_MODEL) is MISTRAL_REASONING_DIALECT


# ---------------------------------------------------------------------------
# 3. include_extra_body=True + canonical field validator
# ---------------------------------------------------------------------------


def test_mistral_extra_body_passthrough_and_canonical_guard() -> None:
    provider = _provider()

    empty_req = make_messages_request(_MODEL)
    empty_body = provider._build_request_body(empty_req)
    assert "extra_body" not in empty_body

    custom_req = make_messages_request(
        _MODEL, extra_body={"safe_prompt": True, "frequency_penalty": 0.4}
    )
    custom_body = provider._build_request_body(custom_req)
    assert custom_body["extra_body"] == {
        "safe_prompt": True,
        "frequency_penalty": 0.4,
    }

    for canonical_key in ("temperature", "reasoning_effort", "model"):
        bad_req = make_messages_request(_MODEL, extra_body={canonical_key: "override"})
        with pytest.raises(InvalidRequestError):
            provider._build_request_body(bad_req)


# ---------------------------------------------------------------------------
# 4. Empty list delta.content normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "empty_content",
    [
        [],
        [{"type": "text", "text": ""}],
        [{"type": "thinking", "thinking": []}],
    ],
)
def test_normalize_mistral_chunk_normalizes_empty_list_content_to_none(
    empty_content: list[Any],
) -> None:
    chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=empty_content,
                    reasoning_content=None,
                    tool_calls=None,
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )
    normalized = normalize_mistral_chunk(chunk)
    delta = normalized.choices[0].delta
    assert delta.content is None
    assert delta.reasoning_content is None


@pytest.mark.asyncio
async def test_stream_response_empty_list_delta_content_streams_and_records_cleanly() -> (
    None
):
    provider = _provider()
    req = make_messages_request(_MODEL)
    empty_chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=[],
                    reasoning_content=None,
                    tool_calls=None,
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )

    async def _stream() -> Any:
        yield empty_chunk
        yield SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=[{"type": "thinking", "thinking": []}],
                        reasoning_content=None,
                        tool_calls=None,
                    ),
                    finish_reason=None,
                )
            ],
            usage=None,
        )
        yield _ok_chunk("After empty list.")

    trace = install_wire_trace()
    with patch.object(
        provider._client.chat.completions, "create", new_callable=AsyncMock
    ) as mock_create:
        mock_create.return_value = _stream()
        events = [e async for e in provider.stream_response(req)]

    assert any("After empty list." in e for e in events)
    assert trace.responses[0]["fields"]["content"] == {
        "deltas": 1,
        "chars": len("After empty list."),
    }


# ---------------------------------------------------------------------------
# 5. temperature=0 + top_p guard & model_overrides precedence
# ---------------------------------------------------------------------------


def test_mistral_greedy_temperature_zero_drops_top_p_unless_overridden(
    durable_store,
) -> None:
    del durable_store
    provider = _provider()

    req_zero_float = make_messages_request(_MODEL, temperature=0.0, top_p=0.9)
    body_zero_float = provider._build_request_body(req_zero_float)
    assert body_zero_float["temperature"] == 0.0
    assert "top_p" not in body_zero_float

    req_zero_int = make_messages_request(_MODEL, temperature=0, top_p=0.8)
    body_zero_int = provider._build_request_body(req_zero_int)
    assert body_zero_int["temperature"] == 0
    assert "top_p" not in body_zero_int

    req_nonzero = make_messages_request(_MODEL, temperature=0.5, top_p=0.9)
    body_nonzero = provider._build_request_body(req_nonzero)
    assert body_nonzero["temperature"] == 0.5
    assert body_nonzero["top_p"] == 0.9

    save_model_overrides(
        ModelParameterOverrides.from_document(
            {"models": {f"mistral/{_MODEL}": {"top_p": 0.95}}}
        )
    )
    body_pinned = provider._build_request_body(req_zero_float)
    assert body_pinned["temperature"] == 0.0
    assert body_pinned["top_p"] == 0.95


def test_apply_mistral_reasoning_request_shape_direct_strip_history_thinking() -> None:
    body: dict[str, Any] = {
        "model": _MODEL,
        "messages": [
            {
                "role": "assistant",
                "reasoning_content": "Secret step",
                "content": [
                    {"type": "thinking", "thinking": [{"type": "text", "text": "Old"}]},
                    {"type": "text", "text": "Answer"},
                ],
            }
        ],
    }
    apply_mistral_reasoning_request_shape(
        body,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MAX),
        dialect=MISTRAL_REASONING_DIALECT,
        strip_history_thinking=True,
    )
    assert body["reasoning_effort"] == "max"
    assert "reasoning_content" not in body["messages"][0]
    assert body["messages"][0]["content"] == "Answer"


def test_vibe_and_studio_isolate_learned_facts_on_same_model_id(
    durable_store,
) -> None:
    del durable_store
    studio = _provider("mistral")
    vibe = _provider("mistral_vibe")

    studio._recovery_memory.remember_rejection(_MODEL, "reasoning_effort")
    assert studio.reasoning_dialect(_MODEL).effort_values is None
    assert vibe.reasoning_dialect(_MODEL) is MISTRAL_REASONING_DIALECT
    assert (
        MessagesRequest(
            model=_MODEL, messages=[Message(role="user", content="hi")]
        ).model
        == _MODEL
    )
