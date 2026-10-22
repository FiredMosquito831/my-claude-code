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
(:data:`ANTHROPIC_MESSAGES_GATEWAY_LIFETIMES`). 7.106.0 adds the OpenAI-shaped
hosts, in tables of their own so the Anthropic declarations above read exactly
as before: OpenRouter's Claude upstreams (one top-level marker), Azure
OpenAI's retention word, and the hosts that cache automatically and take no
lifetime at all -- the ChatGPT subscription among them, whose Codex backend is
sent no retention field by Codex itself.

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
    #: Where a chosen lifetime lands in the body (7.106.0). The Anthropic
    #: families re-time the cache markers the client wrote (and add Anthropic's
    #: automatic marker where none is written); the OpenAI-shaped families
    #: below write one top-level value of their own. ``none`` -- a host that
    #: caches on its own and takes no lifetime -- writes nothing at all.
    placement: str = "client_markers"

    @property
    def automatic(self) -> bool:
        """Whether this host caches on its own and takes no lifetime at all."""
        return not self.lifetimes


#: The placements a family can declare (7.106.0).
PLACEMENT_CLIENT_MARKERS = "client_markers"
#: One top-level ``cache_control`` object for the whole request, as OpenRouter
#: documents for its Claude upstreams ("automatic caching").
PLACEMENT_TOP_LEVEL_MARKER = "top_level_marker"
#: One top-level body field (``wire_field``) holding the lifetime's own word,
#: as OpenAI's ``prompt_cache_retention`` does.
PLACEMENT_BODY_FIELD = "body_field"
#: Nothing: the host caches automatically and has no lifetime field.
PLACEMENT_NONE = "none"


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

#: OpenRouter's Chat Completions door in front of a Claude model (7.106.0).
#: OpenRouter documents Anthropic's two lifetimes for every Claude provider it
#: routes to -- 5 minutes (the default) and 1 hour, ``"ttl": "5m" | "1h"`` --
#: and a single top-level ``cache_control`` object that caches the whole
#: prompt ("automatic caching": the breakpoint goes on the last cacheable
#: block and advances as the conversation grows), supported on its Anthropic,
#: Vertex, Azure and Bedrock providers and on Claude Platform on AWS. MCC's
#: Chat conversion keeps no block marker, so a Claude model on OpenRouter has
#: been sent no marker at all and has never cached there; with a lifetime set,
#: that one top-level marker is added. Nothing set: no byte moves.
OPENROUTER_ANTHROPIC_LIFETIMES = "openrouter_anthropic"

#: OpenAI's own retention word on the Chat Completions body (7.106.0), as Azure
#: OpenAI documents it for its deployments: ``prompt_cache_retention`` is
#: ``in_memory`` (a cache is typically cleared after 5 to 10 idle minutes and
#: always within an hour) or ``24h`` (extended retention, up to 24 hours, on
#: the models Azure lists for it), priced the same. GPT-5.6 and later take
#: ``prompt_cache_options.ttl`` instead, whose only value is ``30m`` -- also
#: its default, so it is never worth sending -- and refuse that object on
#: older models with a 400. A deployment's name says nothing reliable about
#: its model, so which word a deployment refuses is learned from its own 400,
#: and the next request sends the word that remains.
OPENAI_RETENTION_LIFETIMES = "openai_retention"

#: A host that caches on its own and takes no lifetime (7.106.0, the user's
#: Q11): nothing is ever written, and the Models page says "caches
#: automatically; no lifetime control". Declared only where a source says so:
#: the host's own documentation, or cache reads in the request log with no
#: marker ever sent (see :data:`AUTOMATIC_PROMPT_CACHE_PROVIDERS`).
AUTOMATIC_PROMPT_CACHE = "automatic"

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
    OPENROUTER_ANTHROPIC_LIFETIMES: PromptCacheLifetimeFamily(
        family_id=OPENROUTER_ANTHROPIC_LIFETIMES,
        label="OpenRouter, Claude upstream",
        wire_field="cache_control.ttl",
        lifetimes=(
            CacheLifetime(minutes=5, wire="5m", label="5 min", write_price="1.25x"),
            CacheLifetime(minutes=60, wire="1h", label="1 h", write_price="2x"),
        ),
        default_wire="5m",
        automatic_marker=True,
        source="openrouter.ai/docs/guides/best-practices/prompt-caching",
        placement=PLACEMENT_TOP_LEVEL_MARKER,
    ),
    OPENAI_RETENTION_LIFETIMES: PromptCacheLifetimeFamily(
        family_id=OPENAI_RETENTION_LIFETIMES,
        label="OpenAI prompt cache retention",
        wire_field="prompt_cache_retention",
        # ``in_memory`` is counted at the 5 minutes a cache is documented to
        # survive at the least, so a request for more is never answered with
        # less than it asked for (IV.2): 1-5 min -> in_memory, 6-60 -> 24h.
        lifetimes=(
            CacheLifetime(
                minutes=5,
                wire="in_memory",
                label="in memory, about 5-10 min",
                write_price="",
            ),
            CacheLifetime(minutes=24 * 60, wire="24h", label="24 h", write_price=""),
        ),
        # The default of gpt-5.4 and older deployments. Never consulted: a
        # chosen retention is always written, there is no marker to leave be.
        default_wire="in_memory",
        automatic_marker=False,
        source=(
            "learn.microsoft.com/azure/foundry/openai/how-to/prompt-caching; "
            "developers.openai.com/api/reference (prompt_cache_retention)"
        ),
        placement=PLACEMENT_BODY_FIELD,
    ),
    AUTOMATIC_PROMPT_CACHE: PromptCacheLifetimeFamily(
        family_id=AUTOMATIC_PROMPT_CACHE,
        label="Automatic caching",
        wire_field="",
        lifetimes=(),
        default_wire="",
        automatic_marker=False,
        source=(
            "the host's documentation (Gemini implicit caching, DeepSeek context "
            "caching, OpenRouter's non-Anthropic upstreams, the ChatGPT/Codex "
            "backend) or cache reads in MCC's request log with no marker sent"
        ),
        placement=PLACEMENT_NONE,
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

#: Providers whose OpenAI-shaped requests take a lifetime of their own, by
#: provider id (7.106.0). Only Azure OpenAI: MCC's ``openai`` id is the
#: ChatGPT subscription's alias (the Codex backend, below), and no other
#: OpenAI-compatible host documents ``prompt_cache_retention``.
CHAT_PROVIDER_PROMPT_CACHE_FAMILY: Mapping[str, str] = {
    "azure_openai": OPENAI_RETENTION_LIFETIMES,
}

#: Gateways whose model ids name the upstream that serves them, and the family
#: of each upstream that takes a lifetime (7.106.0). OpenRouter's ids are
#: ``<author>/<slug>`` -- ``anthropic/claude-haiku-5.5`` -- with ``~`` before
#: the author for its "latest" aliases (``~anthropic/claude-sonnet-latest``);
#: the author is OpenRouter's own declared namespace, not a model name, so
#: every Claude model it lists, today's and tomorrow's, is in it.
UPSTREAM_PROMPT_CACHE_FAMILY: Mapping[str, Mapping[str, str]] = {
    "open_router": {"anthropic": OPENROUTER_ANTHROPIC_LIFETIMES},
}

#: The prefix OpenRouter puts before the author of an alias id.
UPSTREAM_ALIAS_PREFIX = "~"

#: Hosts that cache on their own and take no lifetime (the user's Q11), each
#: on evidence (7.106.0): its documentation -- Gemini and Vertex (implicit
#: caching, on by default, no lifetime field), DeepSeek (context caching, on
#: by default, no parameter), OpenRouter for every upstream but Anthropic, the
#: ChatGPT subscription and its ``openai`` alias (Codex 0.163.0 sends only
#: ``prompt_cache_key``; GPT-5.6 and later take one lifetime, 30 min, the
#: default) -- or cache reads in the request log on requests MCC sent no
#: marker: Nous Portal, OpenCode Zen (and Go, the same vendor and backend),
#: NVIDIA NIM, Agnes, B.AI and HyperCharm. A host not named here -- or by any
#: table above -- is "not known" on the Models page, never "unsupported"
#: (IV.16). The models a gateway serves over Anthropic's Messages protocol
#: keep the gateway family whatever is said here.
AUTOMATIC_PROMPT_CACHE_PROVIDERS: frozenset[str] = frozenset(
    {
        "agnes",
        "bai",
        "chatgpt_oauth",
        "deepseek",
        "gemini",
        "hypercharm",
        "nous_portal",
        "nvidia_nim",
        "open_router",
        "openai",
        "opencode",
        "opencode_go",
        "vertex",
    }
)


def prompt_cache_family_for(provider_id: str) -> PromptCacheLifetimeFamily | None:
    """The lifetimes this provider's host accepts, or ``None`` when not declared."""

    family_id = PROVIDER_PROMPT_CACHE_FAMILY.get(str(provider_id or "").strip())
    if family_id is None:
        return None
    return PROMPT_CACHE_LIFETIME_FAMILIES.get(family_id)


def prompt_cache_gateway_family() -> PromptCacheLifetimeFamily:
    """The family of a non-Anthropic host speaking Anthropic's Messages protocol."""

    return PROMPT_CACHE_LIFETIME_FAMILIES[MESSAGES_GATEWAY_PROMPT_CACHE_FAMILY]


def upstream_author(model_id: str) -> str:
    """The upstream a gateway's ``<author>/<slug>`` model id names, lowercased.

    ``""`` for an id without an author segment. The alias prefix is not part
    of the author: ``~anthropic/claude-sonnet-latest`` is Anthropic's.
    """

    text = str(model_id or "").strip()
    if text.startswith(UPSTREAM_ALIAS_PREFIX):
        text = text[len(UPSTREAM_ALIAS_PREFIX) :]
    author, separator, slug = text.partition("/")
    return author.strip().lower() if separator and slug else ""


def chat_prompt_cache_family_id(provider_id: str, model_id: str) -> str | None:
    """The family id of one OpenAI-shaped request's host, or ``None`` if unknown.

    In order: the upstream a gateway's model id names (OpenRouter's Claude
    models), the provider's own declaration (Azure OpenAI), then the hosts
    that cache automatically. ``None`` is "not known", which sends nothing.
    """

    pid = str(provider_id or "").strip()
    upstreams = UPSTREAM_PROMPT_CACHE_FAMILY.get(pid)
    if upstreams:
        family_id = upstreams.get(upstream_author(model_id))
        if family_id is not None:
            return family_id
    family_id = CHAT_PROVIDER_PROMPT_CACHE_FAMILY.get(pid)
    if family_id is not None:
        return family_id
    if pid in AUTOMATIC_PROMPT_CACHE_PROVIDERS:
        return AUTOMATIC_PROMPT_CACHE
    return None


def chat_prompt_cache_family_for(
    provider_id: str, model_id: str
) -> PromptCacheLifetimeFamily | None:
    """The lifetimes one OpenAI-shaped request's host takes (7.106.0).

    ``None`` when nothing is declared for it; the automatic family -- no
    lifetimes at all -- for a host that caches on its own. Either way an
    operator's lifetime writes nothing; only a family with lifetimes does.
    """

    family_id = chat_prompt_cache_family_id(provider_id, model_id)
    if family_id is None:
        return None
    return PROMPT_CACHE_LIFETIME_FAMILIES.get(family_id)


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
    "AUTOMATIC_PROMPT_CACHE",
    "AUTOMATIC_PROMPT_CACHE_PROVIDERS",
    "CHAT_PROVIDER_PROMPT_CACHE_FAMILY",
    "MESSAGES_GATEWAY_PROMPT_CACHE_FAMILY",
    "OPENAI_RETENTION_LIFETIMES",
    "OPENROUTER_ANTHROPIC_LIFETIMES",
    "PLACEMENT_BODY_FIELD",
    "PLACEMENT_CLIENT_MARKERS",
    "PLACEMENT_NONE",
    "PLACEMENT_TOP_LEVEL_MARKER",
    "PROMPT_CACHE_LIFETIME_FAMILIES",
    "PROVIDER_PROMPT_CACHE_FAMILY",
    "UPSTREAM_ALIAS_PREFIX",
    "UPSTREAM_PROMPT_CACHE_FAMILY",
    "CacheLifetime",
    "PromptCacheLifetimeFamily",
    "chat_prompt_cache_family_for",
    "chat_prompt_cache_family_id",
    "normalize_prompt_cache_ttl",
    "prompt_cache_family_for",
    "prompt_cache_gateway_family",
    "prompt_cache_ttl_minutes",
    "upstream_author",
]
