"""OpenRouter-format structured reasoning replay and stream conversion."""

import json
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.openai_responses import is_carrier
from my_claude_code.providers.openai_responses import carrier_foreign_note


def assistant_reasoning_details(messages: Any) -> dict[int, list[dict[str, Any]]]:
    """Every assistant turn's replayed details, keyed by its source index.

    The key is the index into ``messages`` -- the **source** history the client
    sent -- because that is the only index a caller can hand to
    :func:`~my_claude_code.core.anthropic.conversion.build_base_request_body`
    and have resolved onto the converted message the turn actually became.

    A turn that yields no detail is simply **absent**. That is correct now, and
    it was the defect before: the old walk appended only non-empty lists and
    then attached them to the next assistant message *in order*, so an absent
    turn shifted every later turn's details up by one. With the index in the
    key there is nothing to shift -- an absent turn has no entry, and every
    other turn keeps its own.
    """

    if not _is_sequence(messages):
        return {}
    result: dict[int, list[dict[str, Any]]] = {}
    for index, message in enumerate(messages):
        if _field(message, "role") != "assistant":
            continue
        details = _redacted_reasoning_details(_field(message, "content"))
        if details:
            result[index] = details
    return result


def iter_reasoning_detail_events(
    delta: Any,
    ledger: AnthropicStreamLedger,
    *,
    native_reasoning: str | None,
    structured: bool,
) -> Iterator[str]:
    """Emit a delta's plaintext reasoning once while preserving every opaque detail.

    **The one response-direction implementation** (8.20.0). Before it, two
    implementations carried two different rule sets: the wired
    ``_iter_reasoning_detail_events`` in ``providers/openrouter_gateway.py``
    and the never-instantiated ``StructuredReasoningStream`` here. The wider
    rules were the dead class's; they are the ones kept, because the live hook
    was narrower in three measured places:

    1. **a detail carrying a ``signature``** -- the live hook emitted its text
       and threw the entry away, so the opaque continuation could never be
       replayed on the next turn;
    2. **a detail with no text and no marker** -- the live hook produced
       nothing at all;
    3. **a ``summary``-typed detail that also carries a text key** -- the live
       hook emitted the block and skipped the text.

    Two things did **not** come across from the class, and both are deliberate:

    * its ``native_reasoning`` emission. The caller owns that
      (``_OpenAIChatStreamRunner.run`` emits ``reasoning_content`` before this
      runs), so emitting it here would send the same sentence twice.
    * its per-stream ``_text_source`` latch. The latch chose one source for the
      whole stream; this recomputes per delta. The latch would DROP detail text
      that arrives after a native delta -- output the live hook emits today --
      and, driven with the native text suppressed, it cannot express the
      double-emission guard at all. Per-delta suppresses only within a delta
      that itself carries native text, which is a strict superset of emission
      and therefore loses nothing.

    ``native_reasoning`` is the caller's own ``reasoning_delta(delta)``, not a
    re-read of ``reasoning_content``: a profile may declare ``reasoning`` as its
    delta field (``cline``, ``zenmux``), and re-reading the wrong field would
    emit the same text twice on those hosts.

    ``structured`` selects the rule set. ``False`` is v8.19.0's live hook, byte
    for byte -- the operator gate ``CHAT_STRUCTURED_REASONING`` ships off, and
    off must restore every byte it sent. ``True`` is the wider set above.
    """

    details = _reasoning_details(delta)
    if not details:
        return
    has_native_reasoning = isinstance(native_reasoning, str) and bool(native_reasoning)
    narrow = not structured
    for detail in details:
        preserved = (
            _reasoning_detail_encrypted(detail)
            if narrow
            else _preserved_reasoning_detail(detail)
        )
        # The narrow rule treats a preserved detail as an ALTERNATIVE to its
        # text -- v8.19.0's ``continue`` -- so a summary detail carrying a text
        # key emits only its block. The wide rule emits both: nothing a host
        # sent is dropped where the client can take it.
        if not has_native_reasoning and not (narrow and preserved):
            text = _reasoning_detail_text(detail)
            if text:
                yield from ledger.ensure_thinking_block()
                yield ledger.emit_thinking_delta(text)
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

    Only assistant turns are read, and **only the one field this door can send**:
    a ``redacted_thinking`` block's ``data``. That is deliberate, and it is why
    the count is honest rather than merely plausible.

    A ``thinking`` block's ``signature`` is a carrier surface on the **Messages**
    door, where the block goes out whole -- and the Messages trim counts it
    there. This door never sends a signature at all: ``_convert_assistant_message``
    reads a ``thinking`` block's **text** (as ``reasoning_content`` or think
    tags) and nothing else, so a carrier in a signature has no path to the wire
    here and counting it would make the line claim a drop that never happened.
    A ``thinking`` block's ``thinking`` text is the host's own summary --
    portable across models and providers by decision 9 -- and is never read
    here either.

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
            if _field(block, "type") != "redacted_thinking":
                continue
            if is_carrier(_field(block, "data")):
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


def _reasoning_detail_encrypted(detail: Any) -> str | None:
    """v8.19.0's preservation predicate, kept as the narrow rule set.

    A detail whose kind names an opaque form, dumped whole. It is a strict
    subset of :func:`_preserved_reasoning_detail` -- which also keeps a
    signature-bearing detail and one with no text at all -- and it is the rule
    every release before 8.20.0 applied on the OpenRouter-dialect gateways.
    """

    kind = str(_field(detail, "type") or "").lower()
    if "encrypted" not in kind and "redacted" not in kind and "summary" not in kind:
        return None
    if isinstance(detail, Mapping):
        return json.dumps(dict(detail), separators=(",", ":"))
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
