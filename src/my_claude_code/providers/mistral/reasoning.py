"""Mistral La Plateforme reasoning compatibility helpers."""

from collections.abc import AsyncIterator, Mapping, Sequence
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from loguru import logger

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import (
    ADAPTIVE_MEANING,
    EFFORT_RANK,
    ReasoningAdaptationKind,
    ReasoningControl,
    ReasoningDialect,
    ReasoningDialectOrigin,
    ReasoningEffort,
    ReasoningPolicy,
    dialect_declares_a_reasoning_channel,
    nearest_effort,
    top_stated_effort,
)
from my_claude_code.core.wire_capture import record_reasoning_adaptation
from my_claude_code.providers.http import maybe_await_aclose
from my_claude_code.providers.recovery import (
    is_bad_request,
    is_echo_key,
    upstream_complaint,
    upstream_error_payload,
)

# Default fallback effort when reasoning is enabled without a named level.
MISTRAL_REASONING_EFFORT = "high"

_DEFAULT_MISTRAL_DIALECT = ReasoningDialect(
    effort_values=frozenset(ReasoningEffort),
    off=True,
    effort_field="reasoning_effort",
    origin=ReasoningDialectOrigin.DECLARED,
)

_REASONING_FIELD_NAMES = frozenset(
    {
        "reasoning_content",
        "reasoning_effort",
        "thinkchunk",
    }
)
_REJECTION_WORDS = ("unsupported", "unknown", "invalid", "forbidden", "extra")


def apply_mistral_reasoning_request_shape(
    body: dict[str, Any],
    *,
    reasoning: ReasoningPolicy,
    dialect: ReasoningDialect | None = None,
    strip_history_thinking: bool = False,
) -> None:
    """Apply Mistral's native reasoning request shape in-place."""
    active_dialect = dialect if dialect is not None else _DEFAULT_MISTRAL_DIALECT
    if reasoning.control is ReasoningControl.OFF:
        if active_dialect.off:
            body["reasoning_effort"] = "none"
        else:
            body.pop("reasoning_effort", None)
    elif reasoning.control is ReasoningControl.ADAPTIVE:
        model = body.get("model")
        model_label = model if isinstance(model, str) else "this model"
        if reasoning.effort is not None:
            if active_dialect.effort_values:
                body["reasoning_effort"] = nearest_effort(
                    reasoning.effort, active_dialect.effort_values
                ).value
            else:
                body.pop("reasoning_effort", None)
                if dialect_declares_a_reasoning_channel(active_dialect):
                    record_reasoning_adaptation(
                        ReasoningAdaptationKind.NOTHING_SENT,
                        f"REASONING ADAPTIVE NOTHING SENT: '{model_label}' parses a "
                        f"reasoning field but none that can carry 'let the model "
                        f"choose'; nothing was sent and the model's own default "
                        f"applies",
                    )
        else:
            top = top_stated_effort(active_dialect.effort_values)
            if top is not None:
                body["reasoning_effort"] = top.value
                record_reasoning_adaptation(
                    ReasoningAdaptationKind.SUBSTITUTED,
                    f"REASONING ADAPTIVE SUBSTITUTED: '{model_label}' has no adaptive "
                    f"channel; sending effort '{top.value}' via `reasoning_effort` -- "
                    f"its highest published rung -- because a host that can only be "
                    f"told a level cannot be told to choose one. Adaptive asks to "
                    f"{ADAPTIVE_MEANING}.",
                )
                logger.info(
                    "REASONING ADAPTIVE: '{}' has no adaptive channel; sent effort '{}' "
                    "via `reasoning_effort` (its top published rung)",
                    model_label,
                    top.value,
                )
            else:
                body.pop("reasoning_effort", None)
                if dialect_declares_a_reasoning_channel(active_dialect):
                    record_reasoning_adaptation(
                        ReasoningAdaptationKind.NOTHING_SENT,
                        f"REASONING ADAPTIVE NOTHING SENT: '{model_label}' parses a "
                        f"reasoning field but none that can carry 'let the model "
                        f"choose'; nothing was sent and the model's own default "
                        f"applies",
                    )
    elif reasoning.requests_reasoning:
        if active_dialect.effort_values:
            if reasoning.effort is not None:
                body["reasoning_effort"] = nearest_effort(
                    reasoning.effort, active_dialect.effort_values
                ).value
            elif ReasoningEffort.HIGH in active_dialect.effort_values:
                body["reasoning_effort"] = MISTRAL_REASONING_EFFORT
            else:
                body["reasoning_effort"] = max(
                    active_dialect.effort_values,
                    key=lambda effort: EFFORT_RANK[effort],
                ).value
        else:
            body.pop("reasoning_effort", None)
    else:
        body.pop("reasoning_effort", None)

    messages = body.get("messages")
    if not isinstance(messages, list):
        return

    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        replayed_reasoning = _clean_text(message.pop("reasoning_content", None))
        if strip_history_thinking:
            stripped_content, removed = _strip_mistral_thinking_content(
                message.get("content")
            )
            if removed:
                message["content"] = stripped_content
            continue
        if replayed_reasoning:
            message["content"] = _content_with_prepended_thinking(
                message.get("content"), replayed_reasoning
            )


def body_has_mistral_thinking(body: Mapping[str, Any]) -> bool:
    """Return whether ``body`` carries Mistral thinking chunks or ``reasoning_content``."""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        if message.get("reasoning_content") is not None:
            return True
        content = message.get("content")
        if _is_sequence(content) and any(
            _is_thinking_chunk_type(_chunk_type(chunk)) for chunk in content
        ):
            return True
    return False


def strip_top_p_on_greedy_temperature(
    body: dict[str, Any],
    request: MessagesRequest,
    reasoning: ReasoningPolicy,
) -> None:
    """Drop ``top_p`` when greedy sampling (``temperature == 0``) is requested.

    Mistral rejects ``top_p`` alongside ``temperature=0``. Running as a
    postprocessor before ``apply_model_parameter_overrides`` keeps any
    operator-pinned ``top_p`` in ``model_overrides.json`` authoritative.
    """
    del request, reasoning
    temperature = body.get("temperature")
    if (
        isinstance(temperature, int | float)
        and not isinstance(temperature, bool)
        and temperature == 0
    ):
        body.pop("top_p", None)


def clone_body_without_mistral_reasoning(
    body: dict[str, Any],
) -> dict[str, Any] | None:
    """Return a body clone with Mistral reasoning fields removed."""
    cloned = deepcopy(body)
    removed = cloned.pop("reasoning_effort", None) is not None

    messages = cloned.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.pop("reasoning_content", None) is not None:
                removed = True
            content = message.get("content")
            stripped_content, content_removed = _strip_mistral_thinking_content(content)
            if content_removed:
                message["content"] = stripped_content
                removed = True

    if not removed:
        return None
    return cloned


def is_mistral_reasoning_rejection(error: Exception) -> bool:
    """Return whether an upstream error rejects Mistral reasoning request fields.

    Echo-safe since 6.33.0. This used to walk the whole error object and then
    fall back to a flat dump of it, which meant a pydantic validation error
    echoing the submitted request under ``input``/``body``/``ctx`` answered
    "yes" purely because the request it echoed contained ``reasoning_effort``.
    The evidence is now taken through the fleet-wide matcher, which prunes
    those keys first. The rejection-phrase requirement below is unchanged, and
    it is what keeps an unrelated 400 from costing the request its thinking.
    """
    if not is_bad_request(error):
        return False
    if _contains_reasoning_rejection(upstream_error_payload(error)):
        return True
    return _reasoning_rejection_text(upstream_complaint(error))


def normalize_mistral_stream(stream: Any) -> AsyncIterator[Any]:
    """Yield OpenAI-chat chunks with Mistral native thinking chunks normalized."""

    async def _iter() -> AsyncIterator[Any]:
        try:
            async for chunk in stream:
                yield normalize_mistral_chunk(chunk)
        finally:
            await maybe_await_aclose(stream)

    return _iter()


def normalize_mistral_chunk(chunk: Any) -> Any:
    """Normalize one Mistral OpenAI-compatible stream chunk."""
    choices = getattr(chunk, "choices", None)
    if not choices:
        return chunk

    normalized_choices: list[Any] = []
    changed = False
    for choice in choices:
        delta = getattr(choice, "delta", None)
        normalized_delta = _normalize_mistral_delta(delta)
        if normalized_delta is delta:
            normalized_choices.append(choice)
            continue
        changed = True
        normalized_choices.append(
            SimpleNamespace(
                delta=normalized_delta,
                finish_reason=getattr(choice, "finish_reason", None),
            )
        )

    if not changed:
        return chunk
    return SimpleNamespace(
        choices=normalized_choices,
        usage=getattr(chunk, "usage", None),
    )


def _normalize_mistral_delta(delta: Any) -> Any:
    if delta is None:
        return delta

    content = _field(delta, "content")
    text, reasoning, changed = _split_mistral_content(content)
    native_reasoning = _extract_native_reasoning(delta)
    if native_reasoning:
        reasoning = "\n".join(part for part in (reasoning, native_reasoning) if part)
        if isinstance(content, str):
            text = content
        changed = True

    existing_reasoning = _field(delta, "reasoning_content")
    if isinstance(existing_reasoning, str) and existing_reasoning:
        reasoning = "\n".join(part for part in (existing_reasoning, reasoning) if part)

    if not changed:
        return delta

    return SimpleNamespace(
        content=text,
        reasoning_content=reasoning or None,
        tool_calls=_field(delta, "tool_calls"),
    )


def _content_with_prepended_thinking(
    content: Any, reasoning: str
) -> list[dict[str, Any]]:
    chunks = [_thinking_chunk(reasoning)]
    chunks.extend(_content_to_mistral_text_chunks(content))
    return chunks


def _strip_mistral_thinking_content(content: Any) -> tuple[Any, bool]:
    if not _is_sequence(content):
        return content, False

    text_parts: list[str] = []
    kept_chunks: list[Any] = []
    removed = False
    can_collapse_to_text = True

    for chunk in content:
        chunk_type = _chunk_type(chunk)
        if _is_thinking_chunk_type(chunk_type):
            removed = True
            continue
        if chunk_type == "text":
            text = _chunk_text(chunk)
            if text:
                text_parts.append(text)
            continue
        kept_chunks.append(chunk)
        can_collapse_to_text = False

    if can_collapse_to_text:
        return "".join(text_parts), removed
    return kept_chunks, removed


def _content_to_mistral_text_chunks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [_text_chunk(content)] if content else []
    if not _is_sequence(content):
        text = _clean_text(content)
        return [_text_chunk(text)] if text else []

    chunks: list[dict[str, Any]] = []
    for chunk in content:
        chunk_type = _chunk_type(chunk)
        if _is_thinking_chunk_type(chunk_type):
            continue
        text = _chunk_text(chunk)
        if text:
            chunks.append(_text_chunk(text))
    return chunks


def _split_mistral_content(content: Any) -> tuple[str | None, str | None, bool]:
    if not _is_sequence(content):
        return None, None, False

    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    for chunk in content:
        chunk_type = _chunk_type(chunk)
        if _is_thinking_chunk_type(chunk_type):
            reasoning = _chunk_reasoning(chunk)
            if reasoning:
                reasoning_parts.append(reasoning)
            continue
        text = _chunk_text(chunk)
        if text:
            text_parts.append(text)

    return (
        "".join(text_parts) or None,
        "".join(reasoning_parts) or None,
        True,
    )


def _extract_native_reasoning(delta: Any) -> str | None:
    for name in ("thinking", "reasoning"):
        value = _field(delta, name)
        text = _chunk_reasoning(value)
        if text:
            return text
    return None


def _contains_reasoning_rejection(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).lower()
            # The echoed request names fields *we sent*, never one the host
            # objected to, so it is not evidence and must not be walked.
            if is_echo_key(key_text):
                continue
            if key_text in _REASONING_FIELD_NAMES:
                return True
            if key_text in {"loc", "param"} and _is_reasoning_field_path(item):
                return True
            if _contains_reasoning_rejection(item):
                return True
        return False
    if _is_sequence(value):
        if _is_reasoning_field_path(value):
            return True
        return any(_contains_reasoning_rejection(item) for item in value)
    if isinstance(value, str):
        return _reasoning_rejection_text(value)
    return False


def _is_reasoning_field_path(value: Any) -> bool:
    if not _is_sequence(value):
        return False
    parts = [str(part).lower() for part in value]
    if any(part in _REASONING_FIELD_NAMES for part in parts):
        return True
    return "thinking" in parts and bool(
        {"body", "messages", "assistant", "content"} & set(parts)
    )


def _reasoning_rejection_text(value: str) -> bool:
    lowered = value.lower()
    if "reasoning input" in lowered and any(
        phrase in lowered
        for phrase in (
            "not enabled",
            "not supported",
            "unsupported",
            "disabled",
        )
    ):
        return True
    if any(field in lowered for field in _REASONING_FIELD_NAMES):
        return True
    if "thinking" not in lowered or not any(
        word in lowered for word in _REJECTION_WORDS
    ):
        return False
    return any(
        marker in lowered
        for marker in (
            "body.messages",
            "assistant.thinking",
            "messages.assistant",
            "content.thinking",
            "field: thinking",
            "field 'thinking'",
            'field "thinking"',
            "property: thinking",
            "property 'thinking'",
            'property "thinking"',
            "parameter: thinking",
            "parameter 'thinking'",
            'parameter "thinking"',
            "param: thinking",
            "param 'thinking'",
            'param "thinking"',
        )
    )


def _thinking_chunk(reasoning: str) -> dict[str, Any]:
    return {
        "type": "thinking",
        "thinking": [_text_chunk(reasoning)],
    }


def _text_chunk(text: str) -> dict[str, str]:
    return {"type": "text", "text": text}


def _chunk_reasoning(chunk: Any) -> str | None:
    if isinstance(chunk, str):
        return chunk or None
    thinking = _field(chunk, "thinking")
    if thinking is not None:
        text = _chunk_text(thinking)
        if text:
            return text
    text = _chunk_text(chunk)
    return text or None


def _chunk_text(chunk: Any) -> str:
    if isinstance(chunk, str):
        return chunk
    if _is_sequence(chunk):
        return "".join(_chunk_text(part) for part in chunk)
    text = _field(chunk, "text")
    if isinstance(text, str):
        return text
    content = _field(chunk, "content")
    if isinstance(content, str):
        return content
    return ""


def _chunk_type(chunk: Any) -> str | None:
    value = _field(chunk, "type")
    if isinstance(value, str):
        return value
    return None


def _is_thinking_chunk_type(chunk_type: str | None) -> bool:
    return chunk_type in {"thinking", "reasoning"}


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str):
        return value if value else None
    return None


def _field(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, str | bytes | bytearray
    )
