"""What a price marketplace takes, refuses and reports, declared per host (7.109.0).

A marketplace gateway is not one seller. InferHub (https://inferhub.dev) pools
many sellers' upstream subscriptions behind one OpenAI- and Anthropic-shaped
API, and per ``<prefix>/<model>`` there is a handful of *price levels*, each
with a count of seller accounts asking that price. Without a ceiling InferHub
picks "the best-scoring provider regardless of price", up to its platform cap
of half the official price (``specs/RESEARCH-INFERHUB.md`` §5.1, §6.2). The
only lever a client has inside one model is the ceiling it sends with every
request -- so the ceiling is what MCC sends, on every attempt, on every door.

This module is the declaration of *how* one marketplace takes that ceiling,
in the ``config/prompt_cache_lifetimes.py`` pattern: a frozen dataclass per
dialect, a table of dialects, pure lookups. A provider profile switches it on
with one ``None``-default field (``OpenAIChatProfile.marketplace``); every
other profile leaves that field ``None`` and nothing below is ever read for
it, so every other host's request is the request it has always been.

Every fact here carries where it was read, and the status the research gave
it (CONFIRMED in InferHub's own docs, or UNVERIFIED -- the user has no
InferHub key, so nothing keyed has been observed). A fact that turns out
different is a data edit here, never a branch somewhere else.

``config`` is a leaf: this module imports nothing first-party.
"""

from collections.abc import Mapping
from dataclasses import dataclass

#: The profile kinds of spec §6.2, in the words the profile store uses. The
#: first is the default and sends nothing -- today's bytes.
KIND_MARKETPLACE_DECIDES = "inferhub_decides"
KIND_PCT_OF_OFFICIAL = "pct_of_official"
KIND_MAX_PRICE = "max_price"
KIND_CHEAPEST_LEVEL_NOW = "cheapest_level_now"
KIND_CHEAPEST_LEVEL_MIN_ACCOUNTS = "cheapest_level_min_accounts"

PROFILE_KINDS: tuple[str, ...] = (
    KIND_MARKETPLACE_DECIDES,
    KIND_PCT_OF_OFFICIAL,
    KIND_MAX_PRICE,
    KIND_CHEAPEST_LEVEL_NOW,
    KIND_CHEAPEST_LEVEL_MIN_ACCOUNTS,
)

#: The kinds whose ceiling is re-derived from the market on every request.
DERIVED_KINDS: frozenset[str] = frozenset(
    {KIND_CHEAPEST_LEVEL_NOW, KIND_CHEAPEST_LEVEL_MIN_ACCOUNTS}
)


@dataclass(frozen=True, slots=True)
class MarketSource:
    """One document the market snapshot is built from."""

    #: The name the snapshot's ``sources`` block files its status under.
    name: str
    #: Relative to the marketplace's management base (``INFERHUB_MARKET_BASE_URL``).
    path: str
    #: Whether it needs the account's key. Keyless sources are read only while
    #: ``MARKETPLACE_PUBLIC_MARKET`` is on; keyed ones only with a key.
    keyed: bool
    #: What MCC reads it for.
    purpose: str
    #: Where its shape was read.
    source: str


@dataclass(frozen=True, slots=True)
class MarketplaceDialect:
    """How one marketplace takes a price ceiling, refuses, and reports a charge."""

    dialect_id: str
    #: The marketplace's name, as the Models page and every refusal spell it.
    label: str
    #: The two request headers that carry the ceiling, input then output.
    ceiling_headers: tuple[str, str]
    #: The unit of both header values.
    ceiling_unit: str
    #: The request header that excludes upstreams (comma-separated prefixes),
    #: or ``None`` for a marketplace that has none.
    exclude_header: str | None
    #: The status a "nothing under your ceiling" refusal comes back with.
    no_offer_status: int
    #: The structured error code that, and only that, means it. The same
    #: status also means "balance too low" on this host, so the status alone
    #: is never enough (spec §7.3).
    no_offer_code: str
    #: Where the code may sit in the error body. The exact envelope is NOT
    #: FOUND in any public source (RES §13.1), so both spellings the research
    #: names are read: ``error.code`` and a top-level ``code``.
    no_offer_code_paths: tuple[tuple[str, ...], ...]
    #: The two fields that refusal carries: the cheapest input and output asks.
    no_offer_min_fields: tuple[str, str]
    #: Whether a reported ``usage.cost`` is the whole bill (no BYOK split).
    reported_cost_is_whole_bill: bool
    #: Response headers kept verbatim on each attempt, lower-cased.
    response_headers: frozenset[str]
    #: The list row's official prices, input then output, USD per 1M tokens.
    official_price_fields: tuple[str, str]
    #: The list row's per-seller asks, input then output.
    ask_fields: tuple[str, str]
    #: The list row's cheapest asks, input then output.
    min_ask_fields: tuple[str, str]
    #: The object under which a list row keeps the four fields above.
    pricing_field: str
    #: The platform's own cap on any ask, as a fraction of official, and where
    #: it is published (``/api/pricing/config``, mutable server-side). Shown,
    #: never used as a default ceiling (IV.1).
    platform_cap_field: str
    #: The management base the snapshot reads from, absent a setting.
    default_market_base: str
    #: Every document the snapshot is built from.
    market_sources: tuple[MarketSource, ...]
    #: The profile kinds this marketplace honours.
    profile_kinds: tuple[str, ...]
    #: The derived-document name the snapshot is stored under, beside the
    #: catalogue (``<config dir>/cache/derived/<name>.json``).
    snapshot_entry: str
    #: Where each fact was read.
    source: str


#: The research, read once, ``specs/RESEARCH-INFERHUB.md`` (2026-10-24).
_INFERHUB_SOURCE = (
    "https://inferhub.dev/docs lines 139-149 (bid headers, 402, "
    "x-ignore-upstream, usage.cost), https://inferhub.dev/api/openapi.json "
    "(GET /v1/models pricing, GET /market, GET /status, GET /catalog), "
    "api.inferhub.dev Access-Control-Expose-Headers (X-Inferhub-*); the 402 "
    "envelope and every keyed value UNVERIFIED (no key)"
)

INFERHUB_MARKETPLACE = MarketplaceDialect(
    dialect_id="inferhub_bid_headers",
    label="InferHub",
    # CONFIRMED in the docs, not in the OpenAPI (RES §5.2, §12.2).
    ceiling_headers=("x-max-input-price", "x-max-output-price"),
    ceiling_unit="usd_per_mtok",
    # CONFIRMED (RES §5.2): case-insensitive prefixes; all excluded -> 404.
    exclude_header="x-ignore-upstream",
    no_offer_status=402,
    no_offer_code="no_provider_under_bid",
    no_offer_code_paths=(("error", "code"), ("code",)),
    no_offer_min_fields=("min_input_per_mtok", "min_output_per_mtok"),
    # CONFIRMED on chat completions (RES §7): "Only consumer cost is exposed".
    reported_cost_is_whole_bill=True,
    # Names CONFIRMED in Access-Control-Expose-Headers; values UNVERIFIED.
    # The rate-limit headers are recorded, never acted on.
    response_headers=frozenset(
        {
            "x-inferhub-model",
            "x-inferhub-publisher",
            "x-inferhub-request-id",
            "x-inferhub-attempt",
            "x-inferhub-backend",
            "x-ratelimit-limit",
            "x-ratelimit-remaining",
            "x-ratelimit-reset",
        }
    ),
    official_price_fields=("official_in", "official_out"),
    ask_fields=("asks_in", "asks_out"),
    min_ask_fields=("min_ask_in", "min_ask_out"),
    pricing_field="pricing",
    platform_cap_field="maxAskPctOfOfficial",
    default_market_base="https://inferhub.dev/api",
    market_sources=(
        MarketSource(
            "market",
            "/market",
            keyed=False,
            purpose="min / max ask and last rate per model",
            source="GET /market (security: []), RES §4.1",
        ),
        MarketSource(
            "status",
            "/status",
            keyed=False,
            purpose="success rate, TTFT and tokens per second per model",
            source="GET /status, RES §4.1, §4.4",
        ),
        MarketSource(
            "pricing_cache",
            "/pricing/cache",
            keyed=False,
            purpose="prompt-cache hit rate per model over 24 h",
            source="GET /pricing/cache, RES §4.1",
        ),
        MarketSource(
            "pricing_config",
            "/pricing/config",
            keyed=False,
            purpose="the platform cap on any ask (fraction of official)",
            source="GET /pricing/config, RES §4.1, §6.1",
        ),
        MarketSource(
            "catalog",
            "/catalog",
            keyed=True,
            purpose="each upstream's prompt note, word for word",
            source="GET /catalog systemPromptNote, RES §4.2, §8.4",
        ),
        MarketSource(
            "combos",
            "/combos",
            keyed=True,
            purpose="the account's combos, listed read-only",
            source="GET /combos (THIRD-PARTY shape, pi-inferhub), RES §3, §5.2",
        ),
    ),
    profile_kinds=PROFILE_KINDS,
    snapshot_entry="marketplace-inferhub",
    source=_INFERHUB_SOURCE,
)

#: Every declared marketplace, by dialect id.
MARKETPLACE_DIALECTS: Mapping[str, MarketplaceDialect] = {
    INFERHUB_MARKETPLACE.dialect_id: INFERHUB_MARKETPLACE,
}

#: The providers whose profile declares a marketplace, by catalogue id. Kept
#: beside the dialect table so the Models page and the snapshot refresher can
#: ask without importing the provider package; ``tests`` pin it to the
#: profiles' own declarations.
PROVIDER_MARKETPLACE: Mapping[str, str] = {
    "inferhub": INFERHUB_MARKETPLACE.dialect_id,
}


def marketplace_for_provider(provider_id: str) -> MarketplaceDialect | None:
    """The marketplace a provider declares, or ``None`` -- every other host."""

    dialect_id = PROVIDER_MARKETPLACE.get(provider_id)
    if dialect_id is None:
        return None
    return MARKETPLACE_DIALECTS.get(dialect_id)


#: The snapshot document's schema. A writer and a reader of a different
#: schema never meet: the stored key carries it.
SNAPSHOT_SCHEMA = 1


def snapshot_key(dialect: MarketplaceDialect) -> str:
    """The key a stored snapshot is written under and must match to be read."""

    return f"{dialect.dialect_id}:{SNAPSHOT_SCHEMA}"


def upstream_prefix(model_id: str) -> str:
    """The upstream a marketplace model id names (``cc`` of ``cc/claude-…``).

    ``""`` for a bare name (an alias) and for ``combo/…`` and ``free/…`` ids,
    whose first segment is a namespace, not an upstream.
    """

    head, separator, rest = (model_id or "").strip().partition("/")
    if not separator or not rest:
        return ""
    head = head.strip().lower()
    if head in {"combo", "free"}:
        return ""
    return head


__all__ = [
    "DERIVED_KINDS",
    "INFERHUB_MARKETPLACE",
    "KIND_CHEAPEST_LEVEL_MIN_ACCOUNTS",
    "KIND_CHEAPEST_LEVEL_NOW",
    "KIND_MARKETPLACE_DECIDES",
    "KIND_MAX_PRICE",
    "KIND_PCT_OF_OFFICIAL",
    "MARKETPLACE_DIALECTS",
    "PROFILE_KINDS",
    "PROVIDER_MARKETPLACE",
    "SNAPSHOT_SCHEMA",
    "MarketSource",
    "MarketplaceDialect",
    "marketplace_for_provider",
    "snapshot_key",
    "upstream_prefix",
]
