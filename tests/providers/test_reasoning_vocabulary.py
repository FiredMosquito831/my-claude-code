"""Endpoint-wide effort vocabularies, and the clamp they make visible.

Mistral and Cohere each document exactly one on-value for reasoning, so a
``low`` request has to be sent as that value -- mapping it to "none" would
disable the thinking the caller asked for. What was wrong before was that the
flattening happened inside the request encoder, where nothing records it.
Declaring the vocabulary as a capability moves the clamp into gating, which
returns a ``ReasoningAdaptation`` the request log can show.
"""

from pathlib import Path
from typing import Any

import pytest

from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.application.reasoning_gating import adapt_reasoning_policy
from my_claude_code.core.reasoning import (
    ReasoningAdaptationKind,
    ReasoningEffort,
    ReasoningPolicy,
)
from my_claude_code.providers.mistral.reasoning import (
    MISTRAL_REASONING_EFFORT,
    apply_mistral_reasoning_request_shape,
)
from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES
from my_claude_code.providers.reasoning_vocabulary import (
    PROVIDER_REASONING_VOCABULARY,
    provider_reasoning_vocabulary,
)
from my_claude_code.providers.runtime.models_dev import (
    resolve_model_reasoning_capability,
)

SINGLE_VALUED = tuple(sorted(PROVIDER_REASONING_VOCABULARY))
BELOW_HIGH = (
    ReasoningEffort.MINIMAL,
    ReasoningEffort.LOW,
    ReasoningEffort.MEDIUM,
)


@pytest.mark.parametrize("provider_id", SINGLE_VALUED)
def test_a_declared_vocabulary_states_only_what_the_endpoint_documents(
    provider_id: str,
) -> None:
    """``can_reason`` stays unknown on purpose.

    The vocabulary is a fact about the endpoint, not about whether one
    particular model behind it reasons at all.
    """

    capability = provider_reasoning_vocabulary(provider_id)

    assert capability is not None
    assert capability.can_reason is None
    assert capability.mandatory is None
    assert capability.supports_effort_control is True
    assert capability.supported_efforts == frozenset({ReasoningEffort.HIGH})


def test_a_provider_with_nothing_declared_stays_unknown() -> None:
    assert provider_reasoning_vocabulary("groq") is None


@pytest.mark.parametrize("provider_id", SINGLE_VALUED)
@pytest.mark.parametrize("effort", BELOW_HIGH)
def test_a_low_request_is_clamped_and_recorded_rather_than_flattened(
    provider_id: str, effort: ReasoningEffort
) -> None:
    adapted, adaptation = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=effort),
        provider_reasoning_vocabulary(provider_id),
        model_ref=f"{provider_id}/a-model",
    )

    assert adapted.effort is ReasoningEffort.HIGH
    assert adaptation.kind is ReasoningAdaptationKind.CLAMPED
    assert adaptation.message is not None
    assert effort.value in adaptation.message
    assert "REASONING EFFORT CLAMPED" in adaptation.message


@pytest.mark.parametrize("provider_id", SINGLE_VALUED)
def test_a_high_request_is_not_an_adaptation(provider_id: str) -> None:
    policy = ReasoningPolicy.on(effort=ReasoningEffort.HIGH)
    adapted, adaptation = adapt_reasoning_policy(
        policy, provider_reasoning_vocabulary(provider_id)
    )

    assert adapted is policy
    assert adaptation.kind is ReasoningAdaptationKind.UNCHANGED


def test_the_declared_tier_never_outranks_a_per_model_source(
    tmp_path: Path,
) -> None:
    """Lowest priority: a gateway that publishes more for one model wins."""

    richer = ModelReasoningCapability(
        can_reason=True,
        supports_effort_control=True,
        supported_efforts=frozenset({ReasoningEffort.LOW, ReasoningEffort.HIGH}),
    )
    resolved = resolve_model_reasoning_capability(
        "mistral", "a-model", richer, tmp_path / "missing.json"
    )

    assert resolved is not None
    assert resolved.supported_efforts == frozenset(
        {ReasoningEffort.LOW, ReasoningEffort.HIGH}
    )


def test_the_declared_tier_answers_when_nothing_else_does(tmp_path: Path) -> None:
    resolved = resolve_model_reasoning_capability(
        "mistral", "a-model", None, tmp_path / "missing.json"
    )

    assert resolved is not None
    assert resolved.supported_efforts == frozenset({ReasoningEffort.HIGH})
    assert resolved.can_reason is None


def test_an_undeclared_provider_is_still_wholly_unknown(tmp_path: Path) -> None:
    assert (
        resolve_model_reasoning_capability(
            "ollama", "a-model", None, tmp_path / "missing.json"
        )
        is None
    )


# ---------------------------------------------------------------------------
# The wire shape itself is unchanged: the value sent was already the only legal
# one. Only its visibility changed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("effort", (*BELOW_HIGH, ReasoningEffort.HIGH))
def test_mistral_sends_its_single_documented_on_value(
    effort: ReasoningEffort,
) -> None:
    body: dict[str, Any] = {"messages": []}
    adapted, _ = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=effort), provider_reasoning_vocabulary("mistral")
    )
    apply_mistral_reasoning_request_shape(body, reasoning=adapted)

    assert body["reasoning_effort"] == MISTRAL_REASONING_EFFORT == "high"


def test_mistral_still_disables_reasoning_on_an_off_policy() -> None:
    body: dict[str, Any] = {"messages": []}
    apply_mistral_reasoning_request_shape(body, reasoning=ReasoningPolicy.off())

    assert body["reasoning_effort"] == "none"


@pytest.mark.parametrize("effort", (*BELOW_HIGH, ReasoningEffort.HIGH))
def test_cohere_sends_its_single_on_value(effort: ReasoningEffort) -> None:
    body: dict[str, Any] = {"model": "a-model", "messages": []}
    adapted, _ = adapt_reasoning_policy(
        ReasoningPolicy.on(effort=effort), provider_reasoning_vocabulary("cohere")
    )
    OPENAI_CHAT_PROFILES["cohere"].reasoning.encode(body, adapted)

    assert body["reasoning_effort"] == "high"
