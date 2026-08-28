"""Reasoning-effort vocabularies a provider publishes for its whole API.

Most providers say nothing per model, and their gateway ``/models`` payload or
their models.dev rows answer for them. A few instead document one vocabulary
for the *endpoint*, identical for every model behind it. That statement is a
real capability fact, and without somewhere to put it the only place it can be
expressed is the provider's request encoder -- where an effort the endpoint
cannot serve is flattened silently and the request log shows nothing.

The two entries here are the endpoints that document a single accepted value.
Sending anything else would be refused, so flattening is correct (WORKING-NOTES
54: map to the nearest legal value, never refuse, never forward an out-of-range
value); what was wrong is that it happened invisibly. Declared as a capability,
the gating layer performs the clamp instead and records it, so a ``low`` request
against Mistral shows up in the request log as clamped to ``high`` rather than
vanishing.

Only ``supports_effort_control`` and ``supported_efforts`` are stated.
``can_reason`` is deliberately left unknown: these are endpoint-wide facts and
say nothing about whether one particular model behind the endpoint reasons at
all. This tier is merged last, so anything a gateway or models.dev publishes
per model wins over it.
"""

from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.core.reasoning import ReasoningEffort

# Mistral La Plateforme: its own OpenAPI spec declares
# ``reasoning_effort`` as ``enum: [high, none]`` -- "high" enables comprehensive
# reasoning traces, "none" disables reasoning effort. There is no middle value
# to map minimal/low/medium onto, and mapping them to "none" would disable the
# thinking the caller asked for.
# https://github.com/mistralai/platform-docs-public/blob/main/openapi.yaml
_MISTRAL_EFFORTS = frozenset({ReasoningEffort.HIGH})

# Cohere: the documented per-request reasoning controls are ``thinking.type``
# (enabled/disabled) and ``thinking.token_budget`` -- https://docs.cohere.com/docs/reasoning.
# Cohere publishes no effort vocabulary at all, so the compatibility endpoint's
# ``reasoning_effort`` field is treated as the single on-value FCC already
# sends, rather than being given an invented low/medium/high scale.
_COHERE_EFFORTS = frozenset({ReasoningEffort.HIGH})

PROVIDER_REASONING_VOCABULARY: dict[str, ModelReasoningCapability] = {
    "mistral": ModelReasoningCapability(
        supports_effort_control=True, supported_efforts=_MISTRAL_EFFORTS
    ),
    "cohere": ModelReasoningCapability(
        supports_effort_control=True, supported_efforts=_COHERE_EFFORTS
    ),
}


def provider_reasoning_vocabulary(
    provider_id: str,
) -> ModelReasoningCapability | None:
    """Return what this provider's API documents for every model behind it."""

    return PROVIDER_REASONING_VOCABULARY.get(provider_id)
