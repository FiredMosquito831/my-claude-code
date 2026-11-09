"""Native Anthropic Messages request serialization for upstream providers."""

import json
from copy import deepcopy
from enum import StrEnum
from typing import Any

from loguru import logger

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.application.prompt_cache import PromptCacheLifetimeChoice
from my_claude_code.config.settings import configured_default_max_output_tokens
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.openai_responses import is_carrier
from my_claude_code.core.reasoning import (
    MINIMUM_BUDGET_TOKENS,
    ReasoningControl,
    ReasoningPolicy,
)
from my_claude_code.providers.openai_responses import (
    HISTORY_CARRIER_FOREIGN,
    HISTORY_CARRIER_STRIPPED,
    carrier_foreign_note,
    carrier_stripped_note,
)

_INTERNAL_FIELDS = frozenset(
    {
        "original_model",
        "resolved_provider_model",
        "extra_body",
        "betas",
    }
)
_CANONICAL_FIELDS = frozenset(
    {
        "model",
        "messages",
        "system",
        "max_tokens",
        "stream",
        "thinking",
    }
)

#: The ``params.wire`` key recording that thinking another model wrote was
#: left out of the body sent to an Anthropic-protocol host. Counts only, never
#: text -- the rule ``history_thinking_omitted`` keeps on the Responses
#: surface. Deliberately not spelled ``thinking_*``: the wire recorder reads
#: any key with that prefix as a reasoning *instruction* (``is_reasoning_key``),
#: and a note about the history would then count as reasoning sent.
FOREIGN_THINKING_DROPPED = "foreign_thinking_dropped"

#: The ``params.wire`` key recording the prompt-cache lifetime MCC chose for an
#: attempt (7.104.0): what was asked, what was sent, what the host takes.
#: Present only when a lifetime setting applied to the request.
PROMPT_CACHE_TTL_NOTE = "prompt_cache_ttl"


def build_anthropic_messages_body(
    request: MessagesRequest,
    *,
    reasoning: ReasoningPolicy,
    wire_notes: dict[str, Any] | None = None,
    forward_thinking_display: bool = False,
    prompt_cache: PromptCacheLifetimeChoice | None = None,
    signs_thinking: bool = True,
) -> dict[str, Any]:
    """Build one native Messages request without exposing FCC-only fields.

    ``wire_notes`` is a caller's mapping to receive what this build took out
    of the client's history -- ``foreign_thinking_dropped``, counts only, and
    the two carrier lines ``history_carrier_foreign`` and
    ``history_carrier_stripped`` -- so the sender records it beside the body it
    describes. Left untouched when nothing was removed.

    ``signs_thinking`` states whether this host mints and checks signatures on
    the thinking blocks it returns (8.12.2). ``True`` -- ``api.anthropic.com``,
    the Claude subscription, Command Code and every other Messages caller --
    is the behaviour every release before it had, byte for byte. ``False`` is
    a host that answers the same protocol and signs nothing: OpenCode Go's
    ``/messages`` door tallied a ``signature`` beside 28 of 3,439 thinking
    responses in 16,783 requests, so MCC's "unsigned means another model's"
    rule mislabels that host's *own* thinking as foreign, drops it, and then
    suppresses the reasoning field for the turn after it. On such a host an
    unsigned ``thinking`` block is kept, and the tool-follow-up suppression
    below -- whose premise is Anthropic's own signature rule -- never fires.

    ``forward_thinking_display`` puts the client's own ``thinking.display``
    choice on the outgoing ``thinking``. Only the Claude-subscription provider
    asks for it: it is the one path that also forwards the client's betas,
    which ``display: "updates"`` needs. ``False`` -- every other caller --
    builds exactly what it always did.

    ``prompt_cache`` is the lifetime an operator's setting chose for this host
    (7.104.0); see :func:`apply_prompt_cache_lifetime`. ``None`` -- every
    request while nothing is set, and every caller but the two first-party
    Anthropic providers -- never reaches that step, so the body is the one
    this function always built.
    """
    body = request.model_dump(exclude_none=True)
    for field in _INTERNAL_FIELDS:
        body.pop(field, None)
    native = [_native_message(message) for message in request.messages]
    carrier_counts: dict[str, int] = {}
    messages, dropped_blocks, dropped_chars = drop_foreign_thinking(
        native, signs_thinking=signs_thinking, carrier_counts=carrier_counts
    )
    body["messages"] = messages
    body["stream"] = True
    if body.get("max_tokens") is None:
        # The operator's last resort, reached only when routing never bound
        # this request to a published limit. 0 means "send none at all", which
        # leaves the answer's length to Anthropic's own default.
        configured = configured_default_max_output_tokens()
        if configured is None:
            body.pop("max_tokens", None)
        else:
            body["max_tokens"] = configured
    _apply_reasoning(body, request, reasoning)
    # Manual thinking makes Anthropic require its own thinking at the start of
    # the final assistant turn of a tool loop. When that turn's thinking was
    # another model's and has just been dropped, no such block can exist, so
    # this one request goes without the ``thinking`` field -- the answer the
    # DeepSeek builder gives a tool follow-up whose thinking it cannot replay
    # (``providers/deepseek/compat.py``). Adaptive thinking has no such rule.
    #
    # 8.12.2: the premise is Anthropic's own signature rule, so it holds only
    # where the host signs. A host that signs nothing has no block to require
    # and nothing to refuse, and suppressing the field there is what made MCC
    # ask for no reasoning on 76.9 % of the turns that followed a visible one.
    carriers_dropped = carrier_counts.get(HISTORY_CARRIER_FOREIGN, 0)
    carriers_stripped = carrier_counts.get(HISTORY_CARRIER_STRIPPED, 0)
    unsigned_tool_turn = (
        signs_thinking
        and bool(dropped_blocks or carriers_dropped)
        and _final_tool_turn_lost_its_thinking(native, signs_thinking=signs_thinking)
    )
    if unsigned_tool_turn and _is_manual_thinking(body.get("thinking")):
        _apply_reasoning(body, request, ReasoningPolicy.off())
    else:
        unsigned_tool_turn = False
    if forward_thinking_display:
        _carry_client_display(body, request)
    if dropped_blocks and wire_notes is not None:
        noun = "block" if dropped_blocks == 1 else "blocks"
        rule = (
            "Anthropic accepts only its own signed thinking"
            if signs_thinking
            # A host that signs nothing still refuses a block that is MCC's own
            # wrapper rather than any host's thinking, which is the only thing
            # this build can drop for it.
            else "this host accepts only the thinking it wrote itself"
        )
        wire_notes[FOREIGN_THINKING_DROPPED] = (
            f"dropped {dropped_blocks} thinking {noun} ({dropped_chars:,} chars) "
            f"made by other models from earlier turns; {rule}"
            + (
                "; this tool follow-up was sent without the thinking field, "
                "because manual thinking needs Anthropic's own thinking ahead "
                "of a tool call and another model made this one"
                if unsigned_tool_turn
                else ""
            )
        )
    if wire_notes is not None:
        # Its own key, beside ``foreign_thinking_dropped`` and never inside its
        # sentence: a carrier is not thinking another model wrote, it is an
        # opaque envelope one host minted, and an operator reading the request
        # detail must be able to tell the two apart. Absent, not ``0``, when
        # there were none -- so every other request's recorded params are
        # exactly what they were.
        wire_notes.update(carrier_foreign_note(carriers_dropped))
        # And its own key again (8.16.3), for the opposite fact: this history's
        # envelope was removed from a block whose text was **kept**, because
        # this host signs nothing and accepts an unsigned ``thinking`` block.
        # Folding a strip into ``history_carrier_foreign`` would make that
        # key's own sentence -- "dropped" -- false. Absent, not ``0``, here too.
        wire_notes.update(carrier_stripped_note(carriers_stripped))
    _merge_extra_body(body, request.extra_body)
    if prompt_cache is not None:
        body = apply_prompt_cache_lifetime(body, prompt_cache, wire_notes)
    return body


def apply_prompt_cache_lifetime(
    body: dict[str, Any],
    choice: PromptCacheLifetimeChoice | None,
    wire_notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Put the chosen lifetime on the body's cache markers; the same body back.

    ``None`` returns ``body`` itself, untouched: the identity is the contract.
    With a lifetime chosen (Q2 "replace", Q3, user decisions 2026-10-20):

    * every ``cache_control`` marker the client wrote -- on a tool, a system
      block, a message block or a block nested in a ``tool_result``, or the
      top-level one -- carries that lifetime. Every other key of a marker
      (``scope``, ``evict_on_complete``) is kept, and no marker is added or
      removed, so the four-breakpoint limit holds and, with one lifetime on
      every marker, so does Anthropic's "longer lifetimes first" rule. A
      marker with no lifetime field already has the host's default one, so
      when that is the lifetime chosen it is left exactly as written;
    * a request with no marker at all, on a host that documents automatic
      caching, gets that single top-level marker with the lifetime -- placed
      by the host on the last cacheable block;
    * a choice with nothing to send (the host refused every lifetime it was
      declared to take) changes no byte; the note says so.

    The body is the builder's own: every container in it was dumped or
    deep-copied for this attempt, so a marker is replaced in place and the
    client's request object is never touched.
    """

    if choice is None:
        return body
    note = choice.note()
    lifetime = choice.sent
    if lifetime is not None:
        owners = _cache_marker_owners(body)
        retimed = 0
        for owner in owners:
            marker = owner["cache_control"]
            current = marker.get("ttl")
            if current == lifetime.wire:
                continue
            if current is None and lifetime.wire == choice.family.default_wire:
                continue
            owner["cache_control"] = {**marker, "ttl": lifetime.wire}
            retimed += 1
        added = not owners and choice.family.automatic_marker
        if added:
            body["cache_control"] = {"type": "ephemeral", "ttl": lifetime.wire}
        note["markers"] = len(owners)
        note["retimed"] = retimed
        note["marker_added"] = added
    if wire_notes is not None:
        wire_notes[PROMPT_CACHE_TTL_NOTE] = note
    return body


def _cache_marker_owners(body: dict[str, Any]) -> list[dict[str, Any]]:
    """Every object in the body that carries a ``cache_control`` marker."""

    owners: list[dict[str, Any]] = []
    if isinstance(body.get("cache_control"), dict):
        owners.append(body)
    for key in ("tools", "system"):
        items = body.get(key)
        if isinstance(items, list):
            for item in items:
                _collect_marker_owners(item, owners)
    messages = body.get("messages")
    if isinstance(messages, list):
        for message in messages:
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in content:
                    _collect_marker_owners(block, owners)
    return owners


def _collect_marker_owners(block: Any, owners: list[dict[str, Any]]) -> None:
    if not isinstance(block, dict):
        return
    if isinstance(block.get("cache_control"), dict):
        owners.append(block)
    nested = block.get("content")
    if isinstance(nested, list):
        for inner in nested:
            _collect_marker_owners(inner, owners)


def _native_message(message: Message) -> dict[str, Any]:
    role = "user" if message.role == "system" else message.role
    return {
        "role": role,
        "content": _native_content(message.content),
    }


def _native_content(content: Any) -> Any:
    if not isinstance(content, list):
        return content
    return [
        {
            key: deepcopy(value)
            for key, value in block.model_dump(exclude_none=True).items()
            if key != "reasoning_content"
        }
        for block in content
    ]


def drop_foreign_thinking(
    messages: list[dict[str, Any]],
    *,
    signs_thinking: bool = True,
    carrier_counts: dict[str, int] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Drop, from assistant turns, the thinking Anthropic did not write.

    Returns ``(messages, dropped_blocks, dropped_chars)``. Three kinds of block
    are provably not Anthropic's, and Anthropic answers a 400 for either of the
    first two:

    * a ``thinking`` block whose ``signature`` is absent, not a string, or
      blank. Anthropic signs every thinking block it mints; MCC opens the
      thinking of every other model with no signature at all, and Claude Code
      replays that as ``signature: ""``. A stream cut before its
      ``signature_delta`` lands here too, and is refused just the same;
    * a ``redacted_thinking`` block whose ``data`` is a JSON object or array.
      That is MCC's own wrapper for an OpenRouter-dialect reasoning detail;
      Anthropic's ``data`` is opaque ciphertext and never parses as one.
    * **a carrier**: a ``thinking`` block whose ``signature``, or a
      ``redacted_thinking`` block whose ``data``, starts with
      ``mcc:reasoning:v1:`` (8.16.1). That is an envelope one Responses host
      minted for itself, and no Messages host -- Anthropic's own API, the
      Claude subscription, Command Code, OpenCode Go's ``/messages`` door --
      may be sent it. The verdict needs no ``accepts()`` call: a v1 envelope
      always writes ``"protocol": "responses"``, and a ``messages`` destination
      can never match that field, so the answer is ``False`` for every possible
      value of the other four. Evaluating it once at design time rather than
      per request is the spec's Q2, and the day a Messages-scoped carrier kind
      exists it mints under its own prefix and this guard does not fire on it.

    **8.16.3 splits the carrier verdict in three**, because a carrier and the
    readable summary beside it are not the same thing and do not have the same
    destination:

    * a carrier in a ``thinking`` block's ``signature`` **with** readable text,
      on a host that **signs nothing** -- the envelope is removed from a NEW
      dict and the block, its text and its position are **kept**. This is the
      only case where reasoning survives a hop, and it is the whole release;
    * the same block on a host that **signs** -- dropped whole, exactly as
      8.16.1 dropped it. An unsigned ``thinking`` block is a 400 at Anthropic,
      so stripping there would only move the drop one rule later;
    * a carrier with **no** readable text, or one in a ``redacted_thinking``
      block's ``data`` -- dropped whole. There is no text field to keep.

    ``carrier_counts``, when given, receives the number of carriers **dropped**
    under ``history_carrier_foreign`` and the number **stripped** under
    ``history_carrier_stripped``. They are counted **separately** from
    ``dropped_blocks``/``dropped_chars`` and never folded into either: a
    carrier is not thinking text, its length is not a character count of
    anything a model wrote, and the facts belong on their own lines of the
    request detail. A stripped block is never ``foreign``, so it never reaches
    ``foreign_thinking_dropped``'s sentence and never contributes to
    ``dropped_chars``. The returned tuple keeps its arity so every existing
    caller and test is untouched.

    ``signs_thinking`` is the first rule's premise and, since 8.16.3, also the
    choice between ``STRIP_SIGNATURE`` and ``DROP`` for a carrier-bearing block
    that has text (8.12.2). ``True`` -- every host that signs -- is the
    behaviour every release before it had. ``False`` is a Messages host that
    signs nothing: there, an unsigned ``thinking`` block is that host's own
    thinking, not another model's, so it is kept. The ``redacted_thinking``
    rule is about the SHAPE of ``data`` and not about any host's signature, so
    it still applies: a JSON object there is MCC's wrapper and no Messages host
    minted it.

    Everything else is kept in its order, every non-blank signature included:
    a signature that is not Anthropic's cannot be told from one that is, and
    an unclassifiable block is kept. User turns are never read. An assistant
    turn left with no blocks is omitted -- it held only thinking, so no
    ``tool_result`` can reference it, and the Messages API combines the two
    turns either side of it into one.

    A pure function of the history, so the trimmed prefix is the same on every
    later turn and the prompt cache stays byte-stable. When nothing matches,
    the very list it was given comes back, so the body is byte-identical. The
    strip is a property of the **destination** and never of the history: it is
    written into the outbound copy only, so a hop through a non-signing host
    cannot change what a later turn to the signing model sends.
    """

    kept: list[dict[str, Any]] | None = None
    dropped_blocks = 0
    dropped_chars = 0
    carriers = 0
    carriers_stripped = 0
    for index, message in enumerate(messages):
        content = message.get("content")
        foreign: list[Any] = []
        remaining: list[Any] = []
        stripped = 0
        if message.get("role") == "assistant" and isinstance(content, list):
            for block in content:
                verdict = _thinking_verdict(block, signs_thinking=signs_thinking)
                if verdict is _ThinkingVerdict.KEEP:
                    remaining.append(block)
                elif verdict is _ThinkingVerdict.STRIP_SIGNATURE:
                    # Decision 9: the carrier is host-bound, the text is not.
                    # A NEW dict, never ``block.pop``: this function is
                    # documented as a pure function of the history, and making
                    # that depend on a caller's copy discipline is the latent
                    # coupling that breaks two releases later. The block keeps
                    # its position, its text and every other key; only the
                    # envelope goes. Counted on its own line, never as a drop.
                    remaining.append(
                        {
                            key: value
                            for key, value in block.items()
                            if key != "signature"
                        }
                    )
                    stripped += 1
                elif _carries_carrier(block):
                    # Counted on its own line, never as thinking text.
                    foreign.append(block)
                    carriers += 1
                else:
                    foreign.append(block)
                    dropped_blocks += 1
                    dropped_chars += _thinking_chars(block)
        # ``foreign`` alone is not enough: a turn whose only block was
        # **stripped** has nothing foreign in it, and appending the message
        # unchanged would put the envelope back on the wire. ``stripped`` is
        # what makes the rebuilt message reach ``kept``.
        if not foreign and not stripped:
            if kept is not None:
                kept.append(message)
            continue
        if kept is None:
            kept = list(messages[:index])
        if remaining:
            kept.append({**message, "content": remaining})
        carriers_stripped += stripped
    if carrier_counts is not None:
        if carriers:
            carrier_counts[HISTORY_CARRIER_FOREIGN] = carriers
        if carriers_stripped:
            carrier_counts[HISTORY_CARRIER_STRIPPED] = carriers_stripped
    if kept is None:
        return messages, 0, 0
    return kept, dropped_blocks, dropped_chars


def _carries_carrier(block: Any) -> bool:
    """Whether this block's opaque field is one of MCC's own envelopes.

    Exactly two fields can hold one (8.16.0): a ``thinking`` block's
    ``signature`` and a ``redacted_thinking`` block's ``data``. A ``thinking``
    block's ``thinking`` text is the host's own summary -- portable by decision
    9 and never read here. The test is
    :func:`~my_claude_code.core.openai_responses.is_carrier` and nothing else:
    the prefix at byte offset 0, no decode, no length test.
    """

    if not isinstance(block, dict):
        return False
    kind = block.get("type")
    if kind == "thinking":
        return is_carrier(block.get("signature"))
    if kind == "redacted_thinking":
        return is_carrier(block.get("data"))
    return False


class _ThinkingVerdict(StrEnum):
    """What one block of an assistant turn is, for this destination.

    Three answers, because there are three facts and two of them are not
    "keep" (8.16.3):

    * ``KEEP`` -- the block goes out exactly as the client sent it;
    * ``STRIP_SIGNATURE`` -- the block goes out **without its ``signature``
      key**, keeping its text and its position. This is the case the whole
      release exists for: a carrier is host-bound, the readable summary beside
      it is not, and a host that signs nothing accepts an unsigned block;
    * ``DROP`` -- the block does not go out at all.

    ``STRIP_SIGNATURE`` is only ever returned for a destination that declares
    ``signs_thinking=False``. On a host that signs, the same block is ``DROP``:
    an unsigned ``thinking`` block is a 400 there, so stripping would only move
    the drop one rule later.
    """

    KEEP = "keep"
    STRIP_SIGNATURE = "strip_signature"
    DROP = "drop"


def _has_readable_thinking(block: dict[str, Any]) -> bool:
    """Whether this ``thinking`` block carries text a model could read.

    A block whose ``thinking`` is absent, not a string, empty or **whitespace
    only** has nothing to keep. The Messages API refuses a whitespace-only
    ``thinking`` block outright ("each thinking block must contain
    non-whitespace thinking"), so such a block is not a candidate for the strip
    even on a host that signs nothing -- it is dropped, which is what 9router's
    own branch does with the same test (``!String(block.thinking ?? "").trim()``).
    """

    text = block.get("thinking")
    return isinstance(text, str) and bool(text.strip())


def _thinking_verdict(block: Any, *, signs_thinking: bool = True) -> _ThinkingVerdict:
    """The one predicate: what to do with ``block`` at this destination.

    **The carrier test runs FIRST and unconditionally.** A carrier is not a
    signature, and ``signs_thinking`` is a statement about signatures, so a host
    that mints none must not short-circuit it -- that ordering is the whole of
    mutation M2, and without it OpenCode Go's ``/messages`` door forwards an
    envelope it never minted.

    Three carrier cases (8.16.3), and the two non-carrier rules exactly as
    PR-3 left them:

    * **(a)** a carrier in a ``thinking`` block's ``signature`` **with**
      readable text -- ``STRIP_SIGNATURE`` where the host signs nothing (the
      text survives), ``DROP`` where it signs (an unsigned block is a 400
      there, so the drop is only moved, not avoided);
    * **(b)** the same carrier with **no** readable text -- ``DROP``: there is
      nothing to keep, and the envelope cannot go;
    * **(c)** a carrier in a ``redacted_thinking`` block's ``data`` --
      ``DROP``: that block type has no text field at all.

    Everything else is the two rules this function replaced, unchanged: a
    ``thinking`` block whose ``signature`` is absent, not a string or blank is
    dropped (unless the host signs nothing, where an unsigned block is its
    own); a ``redacted_thinking`` whose ``data`` parses as a JSON object or
    array is dropped. A block that is neither type is kept.
    """

    if not isinstance(block, dict):
        return _ThinkingVerdict.KEEP
    kind = block.get("type")
    if kind == "thinking":
        if is_carrier(block.get("signature")):
            # (b) nothing readable to keep -> drop the block.
            if not _has_readable_thinking(block):
                return _ThinkingVerdict.DROP
            # (a) readable text present. A host that signs refuses an unsigned
            #     block, so stripping there would only move the drop one rule
            #     later -- and would corrupt ``foreign_thinking_dropped``'s
            #     sentence with a character count that is not thinking text.
            if signs_thinking:
                return _ThinkingVerdict.DROP
            return _ThinkingVerdict.STRIP_SIGNATURE
        # ---- existing rule 1, unchanged ------------------------------------
        if not signs_thinking:
            # This host mints no signature, so an unsigned block is its own.
            return _ThinkingVerdict.KEEP
        signature = block.get("signature")
        if not isinstance(signature, str) or not signature.strip():
            return _ThinkingVerdict.DROP
        return _ThinkingVerdict.KEEP
    if kind == "redacted_thinking":
        # (c) there is no text field on this block type -> drop.
        if is_carrier(block.get("data")):
            return _ThinkingVerdict.DROP
        # ---- existing rule 2, unchanged ------------------------------------
        if _is_json_container(block.get("data")):
            return _ThinkingVerdict.DROP
        return _ThinkingVerdict.KEEP
    return _ThinkingVerdict.KEEP


def _is_foreign_thinking(block: Any, *, signs_thinking: bool = True) -> bool:
    """Whether ``block`` does not go out as the client sent it.

    Kept under its own name and its own boolean contract because
    :func:`_final_tool_turn_lost_its_thinking` calls it and must read the same
    verdict the drop was made with. A block that is **stripped** is *kept* --
    it is in the body, minus one key -- so it reads as not-foreign here, which
    is correct: the turn did not lose its thinking.
    """

    return _thinking_verdict(block, signs_thinking=signs_thinking) is not (
        _ThinkingVerdict.KEEP
    )


def _is_json_container(data: Any) -> bool:
    """Whether ``data`` is a JSON object or array -- never Anthropic ciphertext."""

    if not isinstance(data, str) or data.lstrip()[:1] not in {"{", "["}:
        return False
    try:
        parsed = json.loads(data)
    except ValueError, RecursionError:
        return False
    return isinstance(parsed, dict | list)


def _thinking_chars(block: dict[str, Any]) -> int:
    text = block.get("thinking" if block.get("type") == "thinking" else "data")
    return len(text) if isinstance(text, str) else 0


def _final_tool_turn_lost_its_thinking(
    messages: list[dict[str, Any]],
    *,
    signs_thinking: bool = True,
) -> bool:
    """Whether the final assistant turn is a tool call left without its thinking.

    Read on the history as the client sent it. A turn of nothing but foreign
    thinking is omitted from the body, so it is skipped and the turn before it
    is the final one. True only when that turn lost a foreign block, still
    holds a ``tool_use``, and no longer begins with a thinking block --
    exactly the turn Anthropic's manual mode refuses.

    ``signs_thinking`` must be the same value the drop was made with, or this
    reads a different history from the one that was sent.
    """

    for message in reversed(messages):
        content = message.get("content")
        if message.get("role") != "assistant":
            continue
        if not isinstance(content, list):
            return False
        kept = [
            block
            for block in content
            if not _is_foreign_thinking(block, signs_thinking=signs_thinking)
        ]
        if content and not kept:
            continue
        if len(kept) == len(content):
            return False
        first = kept[0] if isinstance(kept[0], dict) else {}
        return first.get("type") not in {"thinking", "redacted_thinking"} and any(
            isinstance(block, dict) and block.get("type") == "tool_use"
            for block in kept
        )
    return False


def _is_manual_thinking(thinking: Any) -> bool:
    return isinstance(thinking, dict) and thinking.get("type") == "enabled"


def _carry_client_display(body: dict[str, Any], request: MessagesRequest) -> None:
    """Put the client's own ``display`` choice on the outgoing ``thinking``.

    Whatever object goes out -- the client's, passed through, or one MCC
    built from a reasoning tier -- as long as it asks Anthropic to think
    (``adaptive`` or ``enabled``); ``display`` means nothing on ``disabled``
    and there is no ``thinking`` to put it on when the field was dropped.
    MCC never picks a display of its own: no client choice, no key. A value
    that is not a non-empty string is ignored, as every value was before.
    """

    requested = request.thinking
    display = requested.display if requested is not None else None
    thinking = body.get("thinking")
    if (
        not isinstance(display, str)
        or not display.strip()
        or not isinstance(thinking, dict)
        or thinking.get("type") not in {"adaptive", "enabled"}
    ):
        return
    body["thinking"] = {**thinking, "display": display}


def _drop_mcc_enabled_default(body: dict[str, Any]) -> None:
    """Take MCC's own ``enabled`` key out of a passed-through ``thinking``.

    ``core.anthropic.models.ThinkingConfig`` defaults ``enabled`` to ``True``
    and other dialects read it, so the client's object is dumped carrying a
    key the client never sent. No variant of Anthropic's ``thinking`` --
    ``enabled``, ``disabled``, ``adaptive`` -- has that key, and from
    2026-10-08 on Anthropic answered every such body ``thinking.<type>.enabled:
    Extra inputs are not permitted``. Only an object with a ``type`` is
    touched, and only that key; the rest of the object goes out exactly as it
    did before.
    """

    thinking = body.get("thinking")
    if isinstance(thinking, dict) and "type" in thinking and "enabled" in thinking:
        body["thinking"] = {
            key: value for key, value in thinking.items() if key != "enabled"
        }


def _apply_reasoning(
    body: dict[str, Any],
    request: MessagesRequest,
    policy: ReasoningPolicy,
) -> None:
    if policy.control is ReasoningControl.OFF:
        body.pop("thinking", None)
        return
    if policy.control is ReasoningControl.ADAPTIVE:
        # An explicit adaptive tier overrides whatever the client asked for.
        body["thinking"] = {"type": "adaptive"}
        return
    if not policy.requests_reasoning:
        # The client's own object passes through.
        _drop_mcc_enabled_default(body)
        return

    budget = policy.numeric_budget_tokens
    if budget is not None:
        bounded = _budget_within_max_tokens(body, budget)
        if bounded is None:
            # No legal budget exists inside this allowance. Sending the answer
            # without thinking is the only outcome that is neither a 400 nor a
            # max_tokens the client never asked for.
            body.pop("thinking", None)
            return
        body["thinking"] = {"type": "enabled", "budget_tokens": bounded}
        return

    requested = request.thinking
    if requested is not None and requested.type in {"adaptive", "enabled"}:
        # The client's own object passes through.
        _drop_mcc_enabled_default(body)
        return
    body["thinking"] = {"type": "adaptive"}


def _budget_within_max_tokens(body: dict[str, Any], budget: int) -> int | None:
    """Enforce Anthropic's ``budget_tokens < max_tokens`` on the wire body.

    Asserted here rather than only at gating time because the two numbers are
    still moving after gating: user configuration, per-model overrides and the
    per-model output budget are all applied later, and a violation at *this*
    point is what the provider answers with a 400. The commit boundary is where
    the protocol adapter owns the invariant.

    **The thinking budget is what yields, always.** ``max_tokens`` is never
    touched here. Raising it to ``budget + 1`` was the behaviour before 5.64.0
    and it did so without consulting the model's own published limit, which is
    how a request ends up asking a 16,384-token model for more output than it
    can emit; the last remaining raise -- to 1,025 whenever the allowance could
    not admit Anthropic's documented 1,024-token minimum -- went the same way in
    6.47.0. This function has no access to the routed model's limit, so any
    raise it performs is unbounded by capability by construction, and there is
    no bounded version of it to keep.

    ``None`` comes back for that case instead: an allowance at or below 1,024
    admits no legal budget at all, since the budget must be both at least 1,024
    and strictly smaller than ``max_tokens``. The caller drops ``thinking``
    rather than send a body the API refuses. Reaching it means the allowance
    moved after ``application.reasoning_budget.bound_budget`` reconciled the
    two, which on a routed request it does not: ``bound_budget`` already
    guarantees ``budget <= max_tokens - 1``.
    """

    max_tokens = body.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool):
        return budget
    if max_tokens > budget:
        return budget
    if max_tokens > MINIMUM_BUDGET_TOKENS:
        return max_tokens - 1
    logger.debug(
        "ANTHROPIC_REQUEST: max_tokens={} admits no thinking budget (Anthropic's"
        " minimum is {} and the budget must stay below max_tokens); sending the"
        " request without thinking rather than raising an allowance the client"
        " set",
        max_tokens,
        MINIMUM_BUDGET_TOKENS,
    )
    return None


def _merge_extra_body(body: dict[str, Any], extra_body: Any) -> None:
    if extra_body in (None, {}):
        return
    if not isinstance(extra_body, dict):
        raise InvalidRequestError(
            "Anthropic Messages extra_body must be an object when provided."
        )
    conflicts = sorted(str(key) for key in extra_body if key in _CANONICAL_FIELDS)
    if conflicts:
        raise InvalidRequestError(
            "Anthropic Messages extra_body cannot override canonical fields: "
            f"{conflicts}."
        )
    body.update({str(key): deepcopy(value) for key, value in extra_body.items()})
