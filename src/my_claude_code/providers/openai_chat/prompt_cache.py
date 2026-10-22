"""A prompt-cache lifetime on an OpenAI-shaped request (7.106.0).

The Chat Completions conversion keeps no cache marker -- every block-level
``cache_control`` the client wrote is dropped on the way in, and that stays
so -- which is why, with nothing set, nothing here runs and every body is the
one MCC has always built. When an operator's lifetime applies to a host whose
family takes one (``config/prompt_cache_lifetimes.py``), it is written once,
at the top level of the body, in the host's own spelling:

* **OpenRouter, Claude upstream** -- one ``cache_control`` object for the
  whole request, ``{"type": "ephemeral", "ttl": "5m" | "1h"}``, which
  OpenRouter documents as automatic caching (the breakpoint is placed on the
  last cacheable block and advances as the conversation grows). It goes in
  the body's ``extra_body``, which is how the OpenAI SDK sends a field it does
  not name; one the client's own ``extra_body`` already carries is re-timed
  instead, every other key of it kept.
* **Azure OpenAI** -- ``prompt_cache_retention``, ``"in_memory"`` or
  ``"24h"``, a field the SDK names (or, where the client's ``extra_body``
  already sets it, there, because ``extra_body`` is what the SDK sends last).

Every other host -- the ones that cache automatically and the ones nothing is
known about -- gets nothing, and so does a request for which nothing asks.

A refusal is learned exactly as on the Anthropic path: a 400 whose own words
name the field the lifetime went into writes ``prompt_cache_lifetime_refused``
for that model (30 days, Forget on the Models page), and the next request
sends the host's next accepted lifetime (the user's decision 2026-10-21
13:21). **Recording only**: the refused request is classified and failed over
exactly as any other 400 is (IV.8).
"""

import json
import re
from collections.abc import Mapping
from typing import Any

from loguru import logger

from my_claude_code.application.prompt_cache import (
    PromptCacheLifetimeChoice,
    choose_prompt_cache_lifetime,
    prompt_cache_ask_for_attempt,
)
from my_claude_code.config.prompt_cache_lifetimes import (
    PLACEMENT_BODY_FIELD,
    PLACEMENT_TOP_LEVEL_MARKER,
    chat_prompt_cache_family_for,
)
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.providers.anthropic_messages import (
    refused_prompt_cache_lifetimes,
)
from my_claude_code.providers.recovery import (
    FACT_PROMPT_CACHE_LIFETIME_REFUSED,
    PROVIDER_WIDE_MODEL_ID,
    SOURCE_REJECTION,
    complaint_evidence_snippet,
    learned_fact_store,
    upstream_complaint,
    upstream_error_payload,
    upstream_status_code,
)

#: The ``params.wire`` key, the same one the Anthropic path records.
PROMPT_CACHE_TTL_NOTE = "prompt_cache_ttl"

#: Where the OpenAI SDK takes a body field it does not name.
_EXTRA_BODY = "extra_body"
_MARKER = "cache_control"
_LIFETIME_FIELD = "ttl"

#: Only a validator's refusal is about a field.
_REFUSAL_STATUS = 400

#: A gateway's copy of its upstream's refusal larger than this is not a sentence.
_MAX_RAW_CHARS = 64 * 1024


def chat_prompt_cache_choice(
    request: MessagesRequest, *, provider_id: str
) -> PromptCacheLifetimeChoice | None:
    """The lifetime one OpenAI-shaped attempt sends, or ``None`` to send none.

    Cheapest test first: a host with no family, or one that caches on its own,
    is ``None`` before the override file or the learned facts are read. Then,
    exactly as the Anthropic path does it: this attempt's override rows over
    what the door stamped from Model Config, adapted to the family's
    lifetimes less the ones this host refused.
    """

    family = chat_prompt_cache_family_for(provider_id, request.model)
    if family is None or family.automatic:
        return None
    ask = prompt_cache_ask_for_attempt(
        request.prompt_cache_ask,
        family,
        provider_id=provider_id,
        model_id=request.model,
    )
    if ask is None:
        return None
    return choose_prompt_cache_lifetime(
        ask,
        family,
        refused=refused_prompt_cache_lifetimes(
            provider_id, request.model, host_wide=family.refusal_per_host
        ),
    )


def apply_chat_prompt_cache_lifetime(
    body: dict[str, Any],
    choice: PromptCacheLifetimeChoice | None,
    wire_notes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the chosen lifetime into a built Chat body; the same body back.

    ``None`` returns ``body`` itself, untouched: the identity is the contract.
    A choice with nothing to send (the host refused every lifetime it takes)
    changes no byte and only records why.
    """

    if choice is None:
        return body
    note = choice.note()
    lifetime = choice.sent
    placement = choice.family.placement
    if lifetime is not None and placement == PLACEMENT_TOP_LEVEL_MARKER:
        extra = body.get(_EXTRA_BODY)
        extra = dict(extra) if isinstance(extra, Mapping) else {}
        marker = extra.get(_MARKER)
        if isinstance(marker, Mapping):
            # The client's own top-level marker, carried in its extra_body.
            current = marker.get(_LIFETIME_FIELD)
            keep = current == lifetime.wire or (
                current is None and lifetime.wire == choice.family.default_wire
            )
            if not keep:
                extra[_MARKER] = {**marker, _LIFETIME_FIELD: lifetime.wire}
                body[_EXTRA_BODY] = extra
            note["markers"] = 1
            note["retimed"] = 0 if keep else 1
            note["marker_added"] = False
        else:
            extra[_MARKER] = {"type": "ephemeral", _LIFETIME_FIELD: lifetime.wire}
            body[_EXTRA_BODY] = extra
            note["markers"] = 0
            note["retimed"] = 0
            note["marker_added"] = True
    elif lifetime is not None and placement == PLACEMENT_BODY_FIELD:
        field = choice.family.wire_field
        extra = body.get(_EXTRA_BODY)
        if isinstance(extra, Mapping) and field in extra:
            body[_EXTRA_BODY] = {**extra, field: lifetime.wire}
        else:
            body[field] = lifetime.wire
        note["field"] = field
    if wire_notes is not None:
        wire_notes[PROMPT_CACHE_TTL_NOTE] = note
    return body


def _raw_upstream_words(payload: Any) -> list[str]:
    """A gateway's copy of its upstream's own refusal (OpenRouter's ``raw``)."""

    error = payload.get("error") if isinstance(payload, Mapping) else None
    container = error if isinstance(error, Mapping) else payload
    metadata = container.get("metadata") if isinstance(container, Mapping) else None
    raw = metadata.get("raw") if isinstance(metadata, Mapping) else None
    if not isinstance(raw, str) or not raw or len(raw) > _MAX_RAW_CHARS:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return [raw.lower()]
    words: list[str] = []
    _collect_words(parsed, words)
    return words


def _collect_words(value: Any, words: list[str]) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).lower() in ("message", "msg", "param", "loc", "detail"):
                if isinstance(item, str):
                    words.append(item.lower())
                elif isinstance(item, list):
                    words.append(".".join(str(part) for part in item).lower())
            if isinstance(item, Mapping | list):
                _collect_words(item, words)
    elif isinstance(value, list):
        for item in value:
            _collect_words(item, words)


def _field_token(field: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![a-z0-9_]){re.escape(field.lower())}(?![a-z0-9_])")


def chat_prompt_cache_refusal(
    error: BaseException, choice: PromptCacheLifetimeChoice | None
) -> tuple[str, str] | None:
    """``(refused lifetime, the host's words)``, or ``None``. Pure.

    A refusal is a 400 whose words -- the host's complaint with any echo of
    the request pruned, and a gateway's copy of its upstream's own answer --
    name the field this choice wrote: ``cache_control`` for the top-level
    marker (the only one in a Chat body, since the conversion keeps none of
    the client's block markers), ``prompt_cache_retention`` for the retention
    word. Anything else says nothing about the lifetime.
    """

    if choice is None or choice.sent is None or not isinstance(error, Exception):
        return None
    if upstream_status_code(error) != _REFUSAL_STATUS:
        return None
    field = choice.family.wire_field.split(".", 1)[0]
    if not field:
        return None
    token = _field_token(field)
    for words in (
        upstream_complaint(error),
        *_raw_upstream_words(upstream_error_payload(error)),
    ):
        if token.search(words):
            return choice.sent.wire, complaint_evidence_snippet(words)
    return None


def note_chat_prompt_cache_refusal(
    error: BaseException,
    *,
    provider_id: str,
    model_id: str,
    choice: PromptCacheLifetimeChoice | None,
) -> str | None:
    """Record what one refused attempt said about its lifetime; never raises.

    Returns the lifetime recorded as refused, or ``None``. Called on the
    failure path before the failure is classified; whatever goes wrong while
    writing the note is swallowed with one DEBUG line, so it can never become
    the request's error.
    """

    try:
        if choice is None or choice.sent is None or not provider_id or not model_id:
            return None
        found = chat_prompt_cache_refusal(error, choice)
        if found is None:
            return None
        refused, words = found
        host_wide = choice.family.refusal_per_host
        learned_fact_store().record(
            provider_id,
            PROVIDER_WIDE_MODEL_ID if host_wide else model_id,
            FACT_PROMPT_CACHE_LIFETIME_REFUSED,
            True,
            source=SOURCE_REJECTION,
            detail=refused,
            evidence=words,
        )
        logger.warning(
            "PROMPT CACHE: {}/{} refused the {} lifetime ({}); later requests adapt "
            "to the lifetimes it takes (shown on the Models page, with Forget). "
            "This request fails over as any 400 does.",
            provider_id,
            model_id,
            refused,
            choice.family.wire_field,
        )
        return refused
    except Exception as exc:
        logger.debug(
            "PROMPT CACHE: could not read a refusal from {}: {}",
            provider_id,
            type(exc).__name__,
        )
        return None


__all__ = [
    "PROMPT_CACHE_TTL_NOTE",
    "apply_chat_prompt_cache_lifetime",
    "chat_prompt_cache_choice",
    "chat_prompt_cache_refusal",
    "note_chat_prompt_cache_refusal",
]
