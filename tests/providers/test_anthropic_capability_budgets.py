"""Capability-aware Anthropic Messages encoding against models.dev metadata.

A gated reasoning policy must travel through the channel the model is *known*
to speak -- native ``output_config.effort`` passthrough for effort models, the
documented budget floor for budget models -- while every unknown model keeps
the historical body byte for byte (the Part IV identity property).
"""

import json
from typing import Any

import httpx
import pytest

from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.application.reasoning_gating import MINIMUM_BUDGET_TOKENS
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.reasoning import (
    ReasoningControl,
    ReasoningEffort,
    ReasoningPolicy,
)
from my_claude_code.providers.anthropic_messages import (
    AnthropicMessagesProvider,
    build_anthropic_messages_body,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.rate_limit import ProviderRateLimiter

EFFORT_KNOWN = ModelReasoningCapability(
    can_reason=True,
    supports_effort_control=True,
    supports_toggle_control=False,
    supports_budget_control=False,
    # The current-generation Claude effort vocabulary: low..max. "minimal"
    # is deliberately absent, exactly as models.dev publishes it.
    supported_efforts=frozenset(
        {
            ReasoningEffort.LOW,
            ReasoningEffort.MEDIUM,
            ReasoningEffort.HIGH,
            ReasoningEffort.XHIGH,
            ReasoningEffort.MAX,
        }
    ),
)
BUDGET_KNOWN = ModelReasoningCapability(
    can_reason=True,
    supports_effort_control=False,
    supports_toggle_control=False,
    supports_budget_control=True,
    supported_efforts=None,
)
DUAL_KNOWN = ModelReasoningCapability(
    can_reason=True,
    supports_effort_control=True,
    supports_toggle_control=False,
    supports_budget_control=True,
    supported_efforts=EFFORT_KNOWN.supported_efforts,
)

ALL_EFFORT_POLICIES = tuple(
    ReasoningPolicy.on(effort=effort) for effort in ReasoningEffort
)


def _request(**overrides: Any) -> MessagesRequest:
    payload: dict[str, Any] = {
        "model": "claude-opus-5",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": "hello"}],
    }
    payload.update(overrides)
    return MessagesRequest.model_validate(payload)


# ---------------------------------------------------------------------------
# Known native effort channel.
# ---------------------------------------------------------------------------


def test_known_effort_model_receives_the_native_effort_channel() -> None:
    body = build_anthropic_messages_body(
        _request(),
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.XHIGH),
        capability=EFFORT_KNOWN,
    )

    assert body["output_config"] == {"effort": "xhigh"}
    assert body["thinking"] == {"type": "adaptive"}
    assert body["max_tokens"] == 4096
    assert "budget_tokens" not in json.dumps(body)


def test_native_effort_preserves_other_output_config_keys_and_wins_the_effort() -> None:
    request = _request(output_config={"format": {"type": "text"}, "effort": "low"})

    body = build_anthropic_messages_body(
        request,
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH),
        capability=EFFORT_KNOWN,
    )

    assert body["output_config"] == {
        "format": {"type": "text"},
        "effort": "high",
    }


def test_configuration_effort_on_a_default_request_uses_the_native_channel() -> None:
    policy = ReasoningPolicy(
        control=ReasoningControl.DEFAULT, effort=ReasoningEffort.HIGH
    )

    body = build_anthropic_messages_body(
        _request(), reasoning=policy, capability=EFFORT_KNOWN
    )

    assert body["output_config"] == {"effort": "high"}
    assert body["thinking"] == {"type": "adaptive"}


def test_a_bare_on_policy_adds_no_effort_for_an_effort_model() -> None:
    body = build_anthropic_messages_body(
        _request(), reasoning=ReasoningPolicy.on(), capability=EFFORT_KNOWN
    )

    assert "output_config" not in body
    assert body["thinking"] == {"type": "adaptive"}


def test_effort_outside_the_published_vocabulary_keeps_the_legacy_budget() -> None:
    """An ungated MINIMAL is outside this vocabulary: nothing may be invented."""

    body = build_anthropic_messages_body(
        _request(),
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MINIMAL),
        capability=EFFORT_KNOWN,
    )

    assert body["thinking"] == {"type": "enabled", "budget_tokens": 512}
    assert "output_config" not in body


# ---------------------------------------------------------------------------
# Known thinking-token budget channel.
# ---------------------------------------------------------------------------


def test_known_budget_model_gets_the_documented_floor() -> None:
    body = build_anthropic_messages_body(
        _request(),
        reasoning=ReasoningPolicy.on(budget_tokens=512),
        capability=BUDGET_KNOWN,
    )

    assert body["thinking"] == {
        "type": "enabled",
        "budget_tokens": MINIMUM_BUDGET_TOKENS,
    }
    assert body["max_tokens"] == 4096


def test_known_budget_floor_bumps_max_tokens_too() -> None:
    body = build_anthropic_messages_body(
        _request(max_tokens=1000),
        reasoning=ReasoningPolicy.on(budget_tokens=512),
        capability=BUDGET_KNOWN,
    )

    assert body["max_tokens"] == MINIMUM_BUDGET_TOKENS + 1


def test_dual_capability_honors_an_explicit_budget_over_the_effort_channel() -> None:
    body = build_anthropic_messages_body(
        _request(),
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.HIGH, budget_tokens=3000),
        capability=DUAL_KNOWN,
    )

    assert body["thinking"] == {"type": "enabled", "budget_tokens": 3000}
    assert "output_config" not in body


def test_dual_capability_routes_a_named_effort_through_the_native_channel() -> None:
    """Metadata retires the invented map wherever a channel is confirmed."""

    body = build_anthropic_messages_body(
        _request(),
        reasoning=ReasoningPolicy.on(effort=ReasoningEffort.MEDIUM),
        capability=DUAL_KNOWN,
    )

    assert body["output_config"] == {"effort": "medium"}
    assert "budget_tokens" not in json.dumps(body)


# ---------------------------------------------------------------------------
# The identity property: unknown means byte-identical.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "policy",
    (*ALL_EFFORT_POLICIES, ReasoningPolicy.on(budget_tokens=2048)),
)
def test_unknown_capability_is_byte_identical_to_the_legacy_body(
    policy: ReasoningPolicy,
) -> None:
    request = _request()
    omitted = build_anthropic_messages_body(request, reasoning=policy)
    explicit_none = build_anthropic_messages_body(
        request, reasoning=policy, capability=None
    )
    partial = build_anthropic_messages_body(
        request,
        reasoning=policy,
        capability=ModelReasoningCapability(can_reason=True),
    )

    assert omitted == explicit_none
    assert omitted == partial

    if policy.effort is not None:
        assert omitted["thinking"] == {
            "type": "enabled",
            "budget_tokens": policy.effort.budget_tokens,
        }
        assert "output_config" not in omitted


@pytest.mark.parametrize(
    ("policy", "capability"),
    (
        (ReasoningPolicy.off(), EFFORT_KNOWN),
        (ReasoningPolicy.adaptive(), BUDGET_KNOWN),
    ),
)
def test_off_and_adaptive_controls_ignore_capability(
    policy: ReasoningPolicy,
    capability: ModelReasoningCapability,
) -> None:
    body = build_anthropic_messages_body(
        _request(), reasoning=policy, capability=capability
    )

    assert "output_config" not in body
    if policy.control is ReasoningControl.OFF:
        assert "thinking" not in body
    else:
        assert body["thinking"] == {"type": "adaptive"}


# ---------------------------------------------------------------------------
# Wiring: the provider resolves the capability into the real wire body.
# ---------------------------------------------------------------------------

_WIRING_KEY = "sk-ant-user-secret-key"
_UPSTREAM_SSE = (
    b'event: message_start\r\ndata: {"type":"message_start","message":{"id":"m"}}'
    b"\r\n\r\n"
    b'event: message_stop\r\ndata: {"type":"message_stop"}\r\n\r\n'
)


def _wiring_provider() -> AnthropicMessagesProvider:
    return AnthropicMessagesProvider(
        ProviderConfig(
            api_key=_WIRING_KEY,
            base_url="https://api.anthropic.com",
            rate_limit=100,
            rate_window=60,
            max_concurrency=2,
            retry_attempts=0,
            early_retry_attempts=0,
            commit_holdback_seconds=0,
        ),
        provider_name="ANTHROPIC",
        rate_limiter=ProviderRateLimiter(
            rate_limit=100,
            rate_window=60,
            max_concurrency=2,
            max_retries=0,
        ),
    )


def _capability_fake(mapping: dict[str, ModelReasoningCapability | None]) -> Any:
    def fake(
        provider_id: str, model_id: str, path: Any = None
    ) -> ModelReasoningCapability | None:
        assert provider_id == "anthropic"
        return mapping.get(model_id)

    return fake


async def _captured_wire_body(
    monkeypatch: pytest.MonkeyPatch,
    *,
    model: str,
    policy: ReasoningPolicy,
    mapping: dict[str, ModelReasoningCapability | None],
) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, content=_UPSTREAM_SSE)

    monkeypatch.setattr(
        "my_claude_code.providers.runtime.models_dev."
        "model_reasoning_capability_from_models_dev",
        _capability_fake(mapping),
    )
    provider = _wiring_provider()
    try:
        provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        request = MessagesRequest(
            model=model,
            max_tokens=4096,
            messages=[Message(role="user", content="ping")],
            stream=True,
        )
        _ = [
            chunk async for chunk in provider.stream_response(request, reasoning=policy)
        ]
    finally:
        await provider.cleanup()
    return seen["body"]


@pytest.mark.asyncio
async def test_provider_resolves_models_dev_capability_into_the_wire_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = await _captured_wire_body(
        monkeypatch,
        model="claude-opus-5",
        policy=ReasoningPolicy.on(effort=ReasoningEffort.XHIGH),
        mapping={"claude-opus-5": EFFORT_KNOWN},
    )

    assert body["output_config"] == {"effort": "xhigh"}
    assert body["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in json.dumps(body)


@pytest.mark.asyncio
async def test_a_model_without_metadata_keeps_the_legacy_wire_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = await _captured_wire_body(
        monkeypatch,
        model="totally-unknown-model",
        policy=ReasoningPolicy.on(effort=ReasoningEffort.MAX),
        mapping={},
    )

    assert body["thinking"] == {"type": "enabled", "budget_tokens": 8192}
    assert "output_config" not in body
