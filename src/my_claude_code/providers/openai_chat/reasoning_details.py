"""OpenRouter-format structured reasoning replay and stream conversion."""

import json
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, Literal

from my_claude_code.core.anthropic import (
    is_synthetic_openai_tool_turn_boundary,
)
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.openai_responses import is_carrier
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.openai_responses import carrier_foreign_note


def apply_reasoning_details_replay(
    body: dict[str, Any], request: MessagesRequest, _policy: ReasoningPolicy
) -> None:
    """Replay opaque reasoning details on their converted assistant messages."""
    assistant_details = _assistant_reasoning_details(request.messages)
    if not assistant_details:
        return
    messages = body.get("messages")
    if not isinstance(messages, list):
        return

    cursor = 0
    for details in assistant_details:
        for index in range(cursor, len(messages)):
            message = messages[index]
            if (
                not isinstance(message, dict)
                or message.get("role") != "assistant"
                or is_synthetic_openai_tool_turn_boundary(message)
            ):
                continue
            existing = message.get("reasoning_details")
            if isinstance(existing, list):
                existing.extend(details)
            else:
                message["reasoning_details"] = list(details)
            cursor = index + 1
            break


class StructuredReasoningStream:
    """Reconcile alternate plaintext reasoning representations for one stream."""

    def __init__(self) -> None:
        self._text_source: Literal["native", "details"] | None = None

    def events(
        self,
        delta: Any,
        ledger: AnthropicStreamLedger,
        *,
        native_reasoning: str | None,
    ) -> Iterator[str]:
        """Emit plaintext once while preserving every opaque reasoning detail."""
        details = _reasoning_details(delta)
        if self._text_source is None:
            if native_reasoning:
                self._text_source = "native"
            elif any(_reasoning_detail_text(detail) for detail in details):
                self._text_source = "details"

        if self._text_source == "native" and native_reasoning:
            yield from ledger.ensure_thinking_block()
            yield ledger.emit_thinking_delta(native_reasoning)

        for detail in details:
            if self._text_source == "details":
                text = _reasoning_detail_text(detail)
                if text:
                    yield from ledger.ensure_thinking_block()
                    yield ledger.emit_thinking_delta(text)

            preserved = _preserved_reasoning_detail(detail)
            if preserved:
                yield from ledger.close_content_blocks()
                index = ledger.blocks.allocate_index()
                yield ledger.content_block_start(
                    index,
                    "redacted_thinking",
                    data=preserved,
                )
                yield ledger.content_block_stop(index)


def _reasoning_details(delta: Any) -> Sequence[Any]:
    details = _field(delta, "reasoning_details")
    if details is None:
        extra = _field(delta, "model_extra")
        if isinstance(extra, Mapping):
            details = extra.get("reasoning_details")
    return details if _is_sequence(details) else ()


def _assistant_reasoning_details(messages: Any) -> list[list[dict[str, Any]]]:
    if not _is_sequence(messages):
        return []
    result: list[list[dict[str, Any]]] = []
    for message in messages:
        if _field(message, "role") != "assistant":
            continue
        details = _redacted_reasoning_details(_field(message, "content"))
        if details:
            result.append(details)
    return result


def carrier_foreign_note_for_history(messages: Any) -> dict[str, str]:
    """The ``history_carrier_foreign`` record for one Chat history, or ``{}``.

    A **pure function of the request**, the shape ``empty_system_messages_note``
    has: the runner merges it into its ``_wire_notes`` before the body is built,
    and the count it writes is the count
    :func:`_redacted_reasoning_details` leaves out of ``reasoning_details`` --
    so the line and the body cannot disagree.

    Why the Chat door drops every carrier, and why that needs no ``accepts()``
    call: a v1 envelope always writes ``"protocol": "responses"``, and a
    ``chat`` destination can never match that field, so the verdict is ``False``
    for every possible value of the other four. Evaluating it once at design
    time rather than per request is the spec's Q2. The guard is
    :func:`~my_claude_code.core.openai_responses.is_carrier` and nothing else --
    the prefix at byte offset 0, no decode, no length test -- so a carrier
    minted by an MCC with a higher size guard is still dropped rather than
    forwarded because it was "too big to be ours".

    Only assistant turns are read, and only the two fields that can hold an
    envelope: a ``thinking`` block's ``signature`` and a ``redacted_thinking``
    block's ``data``. A ``thinking`` block's ``thinking`` text is the host's own
    summary -- portable across models and providers by decision 9 -- and is
    never read here.

    ``{}`` when the history carries none, which keeps every other request's
    recorded params exactly as they were.
    """

    if not _is_sequence(messages):
        return {}
    count = 0
    for message in messages:
        if _field(message, "role") != "assistant":
            continue
        content = _field(message, "content")
        if not _is_sequence(content):
            continue
        for block in content:
            kind = _field(block, "type")
            if kind == "thinking":
                value = _field(block, "signature")
            elif kind == "redacted_thinking":
                value = _field(block, "data")
            else:
                continue
            if is_carrier(value):
                count += 1
    return carrier_foreign_note(count)


def _redacted_reasoning_details(content: Any) -> list[dict[str, Any]]:
    if not _is_sequence(content):
        return []
    details: list[dict[str, Any]] = []
    for block in content:
        if _field(block, "type") != "redacted_thinking":
            continue
        data = _field(block, "data")
        if not isinstance(data, str) or not data:
            continue
        if is_carrier(data):
            # A carrier minted for another host must not reach a Chat upstream.
            # Before 8.16.1 this line was the leak: the block fell through to
            # the ``reasoning.encrypted`` branch below and the envelope went out
            # verbatim, because ``convert_messages`` had already dropped it and
            # this second pass never learned about it. The prefix test cannot
            # collide with the JSON branch: a carrier starts ``mcc:``, a JSON
            # container starts ``{`` or ``[``. Counted by
            # :func:`carrier_foreign_note_for_history`, in the same pass.
            continue
        parsed = _json_payload(data)
        if isinstance(parsed, list):
            details.extend(item for item in parsed if isinstance(item, dict))
        elif isinstance(parsed, dict):
            details.append(parsed)
        else:
            details.append({"type": "reasoning.encrypted", "data": data})
    return details


def _reasoning_detail_text(detail: Any) -> str | None:
    kind = str(_field(detail, "type") or "").lower()
    if "encrypted" in kind or "redacted" in kind:
        return None
    for key in ("text", "content", "reasoning"):
        value = _field(detail, key)
        if isinstance(value, str) and value:
            return value
    return None


def _preserved_reasoning_detail(detail: Any) -> str | None:
    if not isinstance(detail, Mapping):
        return None
    kind = str(_field(detail, "type") or "").lower()
    signature = _field(detail, "signature")
    if (
        "encrypted" in kind
        or "redacted" in kind
        or "summary" in kind
        or isinstance(signature, str)
        or _reasoning_detail_text(detail) is None
    ):
        return json.dumps(dict(detail), separators=(",", ":"))
    return None


def _json_payload(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _field(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    return getattr(item, name, None)


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, str | bytes | bytearray
    )
