"""Fleet-wide recovery from an upstream's own request rejections.

Two safety nets used to live on ``providers/openai_chat`` alone: *learn the
output cap a host stated in a 400 and clamp to it*, and *strip the reasoning
field a host named in a 400, then remember the refusal*. Every provider that
subclasses ``BaseProvider`` directly -- the Anthropic Messages family, ChatGPT
OAuth, Command Code's Anthropic half -- bypassed both, so the same 400 that a
custom OpenAI-compatible provider recovered from was a hard failure there.

This package is the one implementation. It owns the echo-safe complaint matcher
(:mod:`.complaint`), the two matchers built on it (:mod:`.output_cap`,
:mod:`.reasoning_reject`), what a provider has learned (:mod:`.memory`) and the
:class:`~my_claude_code.providers.recovery.ladder.RecoveryLadder` a provider's
retry loop consults (:mod:`.ladder`). Nothing here knows a protocol: the body
keys a dialect uses are arguments, and the matchers read a complaint the same
way whether it arrived on an OpenAI SDK exception or an ``httpx`` response.
"""

from .complaint import (
    complaint_evidence_snippet,
    is_bad_request,
    is_echo_key,
    matched_token,
    sampling_parameter_evidence,
    upstream_complaint,
    upstream_error_payload,
    upstream_status_code,
)
from .facts import (
    ALLOWED_FACT_KINDS,
    ALLOWED_FACT_SOURCES,
    FACT_EFFORT_ENUM,
    FACT_MODEL_WITHHELD,
    FACT_MODELS_ETAG,
    FACT_OUTPUT_CAP,
    FACT_REASONING_FIELD_REJECTED,
    FACT_STREAM_USAGE_UNSUPPORTED,
    FACT_TOOL_CALLS_UNSUPPORTED,
    FACT_VISION_UNSUPPORTED,
    INFERRED_FACT_TTL_SECONDS,
    MAX_EVIDENCE_CHARS,
    MAX_FACT_ROWS,
    PROVIDER_WIDE_MODEL_ID,
    SOURCE_OBSERVATION,
    SOURCE_PROBE,
    SOURCE_REJECTION,
    STATED_FACT_TTL_SECONDS,
    WITHHELD_FACT_TTL_SECONDS,
    FactSink,
    LearnedFact,
    bounded_evidence,
    utc_now_iso,
)
from .ladder import (
    GIVE_UP,
    OutputCapRecovery,
    ProviderRecovery,
    ReasoningStripRecovery,
    RecoveryDecision,
    RecoveryLadder,
    RecoveryRung,
)
from .memory import RecoveryMemory
from .output_cap import (
    ANTHROPIC_OUTPUT_FIELDS,
    OPENAI_CHAT_OUTPUT_FIELDS,
    RESPONSES_OUTPUT_FIELDS,
    clamp_output_tokens,
    parse_output_token_cap,
)
from .reasoning_reject import (
    clone_body_without_reasoning_field,
    rejected_reasoning_field,
)
from .store import (
    LearnedFactStore,
    learned_fact_store,
    reset_learned_fact_store,
    set_learned_fact_store,
)

__all__ = [
    "ALLOWED_FACT_KINDS",
    "ALLOWED_FACT_SOURCES",
    "ANTHROPIC_OUTPUT_FIELDS",
    "FACT_EFFORT_ENUM",
    "FACT_MODELS_ETAG",
    "FACT_MODEL_WITHHELD",
    "FACT_OUTPUT_CAP",
    "FACT_REASONING_FIELD_REJECTED",
    "FACT_STREAM_USAGE_UNSUPPORTED",
    "FACT_TOOL_CALLS_UNSUPPORTED",
    "FACT_VISION_UNSUPPORTED",
    "GIVE_UP",
    "INFERRED_FACT_TTL_SECONDS",
    "MAX_EVIDENCE_CHARS",
    "MAX_FACT_ROWS",
    "OPENAI_CHAT_OUTPUT_FIELDS",
    "PROVIDER_WIDE_MODEL_ID",
    "RESPONSES_OUTPUT_FIELDS",
    "SOURCE_OBSERVATION",
    "SOURCE_PROBE",
    "SOURCE_REJECTION",
    "STATED_FACT_TTL_SECONDS",
    "WITHHELD_FACT_TTL_SECONDS",
    "FactSink",
    "LearnedFact",
    "LearnedFactStore",
    "OutputCapRecovery",
    "ProviderRecovery",
    "ReasoningStripRecovery",
    "RecoveryDecision",
    "RecoveryLadder",
    "RecoveryMemory",
    "RecoveryRung",
    "bounded_evidence",
    "clamp_output_tokens",
    "clone_body_without_reasoning_field",
    "complaint_evidence_snippet",
    "is_bad_request",
    "is_echo_key",
    "learned_fact_store",
    "matched_token",
    "parse_output_token_cap",
    "rejected_reasoning_field",
    "reset_learned_fact_store",
    "sampling_parameter_evidence",
    "set_learned_fact_store",
    "upstream_complaint",
    "upstream_error_payload",
    "upstream_status_code",
    "utc_now_iso",
]
