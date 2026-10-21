"""Which prompt-cache lifetimes each host accepts, and how they are spelled (7.104.0).

A cache lifetime is decided by the host, and every host's list is short and
fixed: Anthropic keeps a prompt cache for five minutes or for one hour and for
nothing in between. So the minutes an operator asks for on Model Config
(``PROMPT_CACHE_TTL`` and ``PROMPT_CACHE_TTL_<TIER>``) are never sent as a
number. They are adapted to the host's own list by one rule, applied only when
MCC is the one choosing: the shortest lifetime that is at least the minutes
asked for, else the host's longest (IV.2: clamp to capability, never refuse).

This module is the declared half of that, kept as data (IV.6): per provider
family, the lifetimes the host accepts, the field that carries one, and what a
write at each costs. No metadata source MCC reads publishes a list of
lifetimes -- models.dev states none, and a 1-hour write price in LiteLLM or an
OpenRouter-dialect list proves only that 1 h is sold -- so the table is what
decides what is sent, and a host's own refusal narrows it per model
(``providers/recovery/facts.py``: ``prompt_cache_lifetime_refused``).

A provider that is not in :data:`PROVIDER_PROMPT_CACHE_FAMILY` gets nothing: no
marker is re-timed, none is added, and the Models page says "lifetime control
not known for this host" -- unknown, which is not the same as unsupported
(IV.16). In 7.104.0 only the two first-party Anthropic paths are declared.
7.105.0 adds the hosts that speak Anthropic's Messages protocol without being
Anthropic -- their lifetimes unknown, sent only when an override row sets one
(:data:`ANTHROPIC_MESSAGES_GATEWAY_LIFETIMES`). The OpenRouter, OpenAI and
ChatGPT families join this table later, in their own rows.

Who decides, per request and per attempt (the user's order, 2026-10-20 Q5):
the model's override row, then its provider's row (``prompt_cache_ttl`` in
``model_overrides.json``), then the tier setting, then ``PROMPT_CACHE_TTL``,
then the client.

``config`` is a leaf: nothing here imports first-party code outside it.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from .constants import (
    PROMPT_CACHE_TTL_CLIENT,
    PROMPT_CACHE_TTL_INHERIT,
    PROMPT_CACHE_TTL_MAX_MINUTES,
    PROMPT_CACHE_TTL_MIN_MINUTES,
)


@dataclass(frozen=True, slots=True)
class CacheLifetime:
    """One lifetime a host accepts: its length, its wire spelling, its price."""

    #: Its length in minutes, which is what a request is compared with.
    minutes: int
    #: The value the host's field takes (``"5m"``, ``"1h"``).
    wire: str
    #: What a person reads (``"5 min"``, ``"1 h"``).
    label: str
    #: The write price the host documents for it, relative to input. Shown,
    #: never used to price a request (IV.9: no hand-entered prices).
    write_price: str


@dataclass(frozen=True, slots=True)
class PromptCacheLifetimeFamily:
    """The lifetimes one wire protocol's hosts accept, and where they go."""

    family_id: str
    #: The family's name on the Models page.
    label: str
    #: Where a lifetime is written, in the host's own field path.
    wire_field: str
    #: Shortest first. The adaptation rule depends on that order.
    lifetimes: tuple[CacheLifetime, ...]
    #: The lifetime a marker carrying no lifetime field already has. A marker
    #: the client wrote without one is left without one when that is the
    #: lifetime chosen: the bytes stay the client's and the meaning is the same.
    default_wire: str
    #: Whether the host documents a single top-level marker that caches the
    #: whole prompt ("automatic caching"). Only then may MCC add one to a
    #: request that carries no marker at all.
    automatic_marker: bool
    #: Where this declaration was read.
    source: str
    #: Whether the host itself publishes ``lifetimes`` (7.105.0). ``False`` for
    #: a host that speaks the protocol but documents no lifetime at all: its
    #: list is then only the protocol's own spelling, and one refusal from it
    #: means it takes none of them -- nothing is sent to it again until the
    #: refusal is forgotten.
    lifetimes_known: bool = True
    #: Whether only an override row (the model's, then its provider's) may
    #: choose a lifetime for this host (7.105.0). Model Config's tier and global
    #: lifetimes never reach it: they were set for Anthropic, and a host whose
    #: lifetimes are unknown gets one only where the operator named it.
    rows_only: bool = False
    #: Whether a refusal is learned for the whole host rather than for the one
    #: model that answered it (7.105.0): a gateway's request validator stands in
    #: front of every model it serves, as its tool-name ceiling does.
    refusal_per_host: bool = False


#: Anthropic's Messages protocol: the Claude API (Console key) and the Claude
#: subscription. ``cache_control: {"type": "ephemeral", "ttl": "5m" | "1h"}``
#: on a block, or once at the top level of the request, where Anthropic places
#: it on the last cacheable block itself. ``ttl`` is a field of the generally
#: available ``cache_control`` object; no beta header is named for it.
ANTHROPIC_MESSAGES_LIFETIMES = "anthropic_messages"

#: Anthropic's Messages protocol spoken by a host that is not Anthropic
#: (7.105.0): Command Code's ``claude-*`` models, OpenCode Zen's models on its
#: Messages door, and every custom provider that declares that door. None of
#: them documents a cache lifetime -- Command Code's Provider API page says its
#: bodies "follow the Anthropic Messages schema" and names no ``ttl``; Zen's
#: page lists cache read and write prices and no lifetime -- so whether one is
#: passed on is not known. Such a host gets a lifetime only when an override
#: row sets one, written in Anthropic's own spelling of the nearest Anthropic
#: lifetime (the same rule: the shortest at least the minutes asked, else the
#: longest). It gets no marker added, and the first refusal stops lifetimes
#: for the whole host until it is forgotten on the Models page.
ANTHROPIC_MESSAGES_GATEWAY_LIFETIMES = "anthropic_messages_gateway"

PROMPT_CACHE_LIFETIME_FAMILIES: Mapping[str, PromptCacheLifetimeFamily] = {
    ANTHROPIC_MESSAGES_LIFETIMES: PromptCacheLifetimeFamily(
        family_id=ANTHROPIC_MESSAGES_LIFETIMES,
        label="Anthropic Messages",
        wire_field="cache_control.ttl",
        lifetimes=(
            CacheLifetime(minutes=5, wire="5m", label="5 min", write_price="1.25x"),
            CacheLifetime(minutes=60, wire="1h", label="1 h", write_price="2x"),
        ),
        default_wire="5m",
        automatic_marker=True,
        source="platform.claude.com/docs/en/build-with-claude/prompt-caching",
    ),
    ANTHROPIC_MESSAGES_GATEWAY_LIFETIMES: PromptCacheLifetimeFamily(
        family_id=ANTHROPIC_MESSAGES_GATEWAY_LIFETIMES,
        label="Anthropic Messages gateway",
        wire_field="cache_control.ttl",
        # Anthropic's spelling, because that is the protocol the host speaks;
        # no write price, because no such host publishes one per lifetime.
        lifetimes=(
            CacheLifetime(minutes=5, wire="5m", label="5 min", write_price=""),
            CacheLifetime(minutes=60, wire="1h", label="1 h", write_price=""),
        ),
        default_wire="5m",
        automatic_marker=False,
        source=(
            "no lifetime published by the gateway (commandcode.ai/docs/provider, "
            "opencode.ai/docs/zen); Anthropic's spelling from "
            "platform.claude.com/docs/en/build-with-claude/prompt-caching"
        ),
        lifetimes_known=False,
        rows_only=True,
        refusal_per_host=True,
    ),
}

#: Which providers declare which family. A provider id that is not here gets
#: no lifetime control at all -- except where it sends a model over the
#: Anthropic Messages protocol, which is declared by the protocol rather than
#: by the id (:data:`MESSAGES_GATEWAY_PROMPT_CACHE_FAMILY`).
PROVIDER_PROMPT_CACHE_FAMILY: Mapping[str, str] = {
    "anthropic": ANTHROPIC_MESSAGES_LIFETIMES,
    "anthropic_oauth": ANTHROPIC_MESSAGES_LIFETIMES,
}

#: The family every non-Anthropic host gets on the requests it receives in
#: Anthropic's Messages protocol (7.105.0). Declared per wire protocol, so a
#: gateway added later -- or a custom provider given the Messages door -- is in
#: it with no change here.
MESSAGES_GATEWAY_PROMPT_CACHE_FAMILY = ANTHROPIC_MESSAGES_GATEWAY_LIFETIMES


def prompt_cache_family_for(provider_id: str) -> PromptCacheLifetimeFamily | None:
    """The lifetimes this provider's host accepts, or ``None`` when not declared."""

    family_id = PROVIDER_PROMPT_CACHE_FAMILY.get(str(provider_id or "").strip())
    if family_id is None:
        return None
    return PROMPT_CACHE_LIFETIME_FAMILIES.get(family_id)


def prompt_cache_gateway_family() -> PromptCacheLifetimeFamily:
    """The family of a non-Anthropic host speaking Anthropic's Messages protocol."""

    return PROMPT_CACHE_LIFETIME_FAMILIES[MESSAGES_GATEWAY_PROMPT_CACHE_FAMILY]


def normalize_prompt_cache_ttl(value: object, *, tier: bool) -> str:
    """One setting's value in its stored form, or ``ValueError``.

    Blank is the field's default -- ``client`` for the global setting,
    ``inherit`` for a tier -- because the admin UI writes ``KEY=`` for a
    cleared field, which is not a typo. Minutes are whole numbers from 1 to
    60, stored without padding; anything else is refused rather than guessed
    at, exactly as an unknown reasoning word is.
    """

    text = str(value if value is not None else "").strip().lower()
    if not text:
        return PROMPT_CACHE_TTL_INHERIT if tier else PROMPT_CACHE_TTL_CLIENT
    if text == PROMPT_CACHE_TTL_CLIENT:
        return text
    if text == PROMPT_CACHE_TTL_INHERIT:
        if tier:
            return text
        raise ValueError("PROMPT_CACHE_TTL cannot inherit; use client or 1-60")
    if text.isdigit():
        minutes = int(text)
        if PROMPT_CACHE_TTL_MIN_MINUTES <= minutes <= PROMPT_CACHE_TTL_MAX_MINUTES:
            return str(minutes)
    allowed = (
        f"{PROMPT_CACHE_TTL_INHERIT}, {PROMPT_CACHE_TTL_CLIENT}"
        if tier
        else PROMPT_CACHE_TTL_CLIENT
    )
    raise ValueError(
        f"Unknown prompt cache lifetime: {value!r}. Known values: {allowed}, or "
        f"whole minutes from {PROMPT_CACHE_TTL_MIN_MINUTES} to "
        f"{PROMPT_CACHE_TTL_MAX_MINUTES}"
    )


def prompt_cache_ttl_minutes(value: str) -> int | None:
    """The minutes a stored value asks for; ``None`` for ``client``/``inherit``."""

    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


__all__ = [
    "ANTHROPIC_MESSAGES_GATEWAY_LIFETIMES",
    "ANTHROPIC_MESSAGES_LIFETIMES",
    "MESSAGES_GATEWAY_PROMPT_CACHE_FAMILY",
    "PROMPT_CACHE_LIFETIME_FAMILIES",
    "PROVIDER_PROMPT_CACHE_FAMILY",
    "CacheLifetime",
    "PromptCacheLifetimeFamily",
    "normalize_prompt_cache_ttl",
    "prompt_cache_family_for",
    "prompt_cache_gateway_family",
    "prompt_cache_ttl_minutes",
]
