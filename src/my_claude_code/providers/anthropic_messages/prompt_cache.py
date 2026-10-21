"""What an Anthropic-protocol host's 400 says about a cache lifetime (7.104.0).

When an operator's setting chose a lifetime and the host answered 400, this
reads the host's own sentence and, only when that sentence names the field
the chosen lifetime went into, writes down "this model's host refuses that
lifetime" (``prompt_cache_lifetime_refused``, 30 days, Forget on the Models
page). While the fact is fresh the lifetime is not sent to that model again
and the operator's minutes are adapted to the lifetimes that remain.

**Recording only, on the failure path.** :func:`note_prompt_cache_refusal`
runs before the failure is classified, never raises, never awaits and never
changes the error it is handed: the refused request is retried, classified
and failed over exactly as any other 400 is (IV.8). No new retry exists.

What counts as naming the lifetime, and nothing else (the field path, matched
exactly):

* a dotted field path in the host's message that ends ``cache_control.ttl``
  -- Anthropic also spells a union's tag in its paths, so
  ``cache_control.ephemeral.ttl`` too -- whose location (``messages.N.content.M``
  with any block-type segments, ``system.N``, ``tools.N``, or the top level)
  holds, in the body that was sent, a marker carrying the chosen lifetime;
* the top-level ``cache_control`` itself, refused as a field, when MCC added
  that marker to a request that carried none.

A 400 that mentions ``cache_control`` for any other reason -- four markers too
many, a marker on a block that cannot carry one, a client's marker MCC never
gave a lifetime to -- is not about the lifetime and records nothing.
"""

import json
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger

from my_claude_code.application.prompt_cache import PromptCacheLifetimeChoice
from my_claude_code.providers.recovery import (
    FACT_PROMPT_CACHE_LIFETIME_REFUSED,
    SOURCE_REJECTION,
    learned_fact_store,
)

#: Only a validator's refusal is about a field; never a 429, a 5xx or an auth
#: answer.
_REFUSAL_STATUS = 400

#: A refusal body larger than this is not a validator's sentence.
_MAX_BODY_BYTES = 64 * 1024

#: A dotted field path inside a sentence (``messages.2.content.0.text.cache_control.ttl``).
_PATH_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_.])[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+"
)
#: The top-level marker named as a field: ``cache_control: Extra inputs ...``.
_TOP_LEVEL_FIELD = re.compile(r"(?<![A-Za-z0-9_.])['\"`]?cache_control['\"`]?\s*:")

#: The path segments that locate a marker; every other segment of a location
#: is a union tag the host spells in its paths (``text``, ``tool_result``).
_LOCATING_SEGMENTS = frozenset({"messages", "content", "system", "tools"})

_MARKER = "cache_control"
_LIFETIME_FIELD = "ttl"


def refused_prompt_cache_lifetimes(provider_id: str, model_id: str) -> frozenset[str]:
    """The lifetimes this model's host refused and that are still applied."""

    if not provider_id or not model_id:
        return frozenset()
    now = datetime.now(UTC)
    return frozenset(
        fact.detail
        for fact in learned_fact_store().facts_for_model(provider_id, model_id)
        if fact.fact_kind == FACT_PROMPT_CACHE_LIFETIME_REFUSED
        and fact.detail
        and not fact.is_stale(now)
    )


def _sentences(document: object) -> list[str]:
    """Every sentence a refusal body carries, each with its field path if any."""

    if not isinstance(document, Mapping):
        return []
    said: list[str] = []
    error = document.get("error")
    if isinstance(error, Mapping) and isinstance(error.get("message"), str):
        said.append(error["message"])
    elif isinstance(error, str):
        said.append(error)
    message = document.get("message")
    if isinstance(message, str):
        said.append(message)
    detail = document.get("detail")
    if isinstance(detail, list):
        for entry in detail:
            if not isinstance(entry, Mapping):
                continue
            loc = entry.get("loc")
            if isinstance(loc, list) and loc:
                path = ".".join(str(part) for part in loc)
                said.append(f"{path}: {entry.get('msg') or ''}")
    elif isinstance(detail, str):
        said.append(detail)
    return said


def _marker_at(body: Mapping[str, Any], location: Sequence[str]) -> Any:
    """The marker at one located object of the body, or ``None``."""

    node: Any = body
    index = 0
    while index < len(location):
        name = location[index]
        if name.isdigit() or not isinstance(node, Mapping):
            return None
        node = node.get(name)
        following = location[index + 1] if index + 1 < len(location) else None
        if following is not None and following.isdigit():
            position = int(following)
            if not isinstance(node, list) or position >= len(node):
                return None
            node = node[position]
            index += 2
        else:
            index += 1
    if not isinstance(node, Mapping):
        return None
    marker = node.get(_MARKER)
    return marker if isinstance(marker, Mapping) else None


def _names_the_lifetime(
    sentence: str, body: Mapping[str, Any], wire: str, marker_added: bool
) -> bool:
    for token in _PATH_TOKEN.findall(sentence):
        segments = token.split(".")
        if segments[0] == "body":
            segments = segments[1:]
        if _MARKER not in segments:
            continue
        at = len(segments) - 1 - segments[::-1].index(_MARKER)
        location = [s for s in segments[:at] if s.isdigit() or s in _LOCATING_SEGMENTS]
        suffix = segments[at + 1 :]
        if suffix and suffix[-1] == _LIFETIME_FIELD and len(suffix) <= 2:
            marker = _marker_at(body, location)
            if marker is not None and marker.get(_LIFETIME_FIELD) == wire:
                return True
        elif not location and len(suffix) <= 1 and marker_added:
            return True
    return marker_added and _TOP_LEVEL_FIELD.search(sentence) is not None


def prompt_cache_refusal(
    status_code: int,
    content: bytes,
    *,
    body: Mapping[str, Any],
    choice: PromptCacheLifetimeChoice | None,
    marker_added: bool,
) -> tuple[str, str] | None:
    """``(refused lifetime, the host's sentence)``, or ``None``. Pure."""

    if choice is None or choice.sent is None or status_code != _REFUSAL_STATUS:
        return None
    if not content or len(content) > _MAX_BODY_BYTES:
        return None
    try:
        document = json.loads(content)
    except ValueError:
        return None
    wire = choice.sent.wire
    for sentence in _sentences(document):
        if _names_the_lifetime(sentence, body, wire, marker_added):
            return wire, sentence
    return None


def note_prompt_cache_refusal(
    error: BaseException,
    *,
    provider_id: str,
    model_id: str,
    body: Mapping[str, Any],
    choice: PromptCacheLifetimeChoice | None,
    note: Mapping[str, Any] | None,
) -> str | None:
    """Record what one refused attempt said about its lifetime; never raises.

    Returns the lifetime it recorded as refused, or ``None``. Called on the
    failure path before the failure is classified, so anything it could
    raise is swallowed here with one DEBUG line: an error while writing a
    note must never become the request's error.
    """

    try:
        if choice is None or choice.sent is None or not provider_id or not model_id:
            return None
        if not isinstance(error, httpx.HTTPStatusError):
            return None
        response = error.response
        found = prompt_cache_refusal(
            response.status_code,
            response.content,
            body=body,
            choice=choice,
            marker_added=bool(note and note.get("marker_added")),
        )
        if found is None:
            return None
        refused, sentence = found
        learned_fact_store().record(
            provider_id,
            model_id,
            FACT_PROMPT_CACHE_LIFETIME_REFUSED,
            True,
            source=SOURCE_REJECTION,
            detail=refused,
            evidence=sentence,
        )
        logger.warning(
            "PROMPT CACHE: {}/{} refused the {} lifetime; later requests adapt "
            "to the lifetimes it takes (shown on the Models page, with Forget). "
            "This request fails over as any 400 does.",
            provider_id,
            model_id,
            refused,
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
    "note_prompt_cache_refusal",
    "prompt_cache_refusal",
    "refused_prompt_cache_lifetimes",
]
