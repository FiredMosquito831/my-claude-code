"""LiteLLM's price map as a second live pricing source, behind a toggle.

``model_prices_and_context_window.json`` is the widest published price table
there is -- 3,850 keys at the pinned commit, against models.dev's per-provider
buckets -- and it is the only one of the three sources that publishes a
*reasoning* rate. It covers models.dev's misses, so it sits directly below the
models.dev rung and above the cross-provider vote.

It is **fetched, never baked**. §25 forbids a hardcoded price table, and a
2.3 MB JSON compiled into the wheel would be exactly that wearing a filename.
The file is fetched with the same discipline ``models_dev`` uses -- one
conditional GET, a strong ``ETag`` replayed as ``If-None-Match``, an atomic
write, freshness measured from the cache file's mtime so a ``304`` costs no
bytes -- and nothing at all happens unless the operator turns the source on.

Three traps, all of them measured, all of them handled here:

* **Units are the opposite of models.dev's.** LiteLLM publishes USD per *single
  token* (``claude-sonnet-4-5`` -> ``input_cost_per_token: 3e-06``); models.dev
  publishes USD per *million*. Normalisation happens here, at the fetcher, so
  no call site can ship a 1,000,000x error.
* **A key's prefix is not its provider.** 598 of the keys are bare and 3,220 are
  slashed at depths of one to four, and bare ``gemini-2.5-pro`` is the *Vertex*
  entry (``litellm_provider: vertex_ai-language-models``). Splitting a key on
  ``/`` to infer a provider would price a Google route from Vertex's card. The
  entry's own ``litellm_provider`` field is read instead, and a bare key is
  accepted only when it agrees with the routed provider.
* **``main`` is not stable.** The raw URL is Fastly-cached for five minutes,
  the file changes about a hundred times a week, and it has served broken JSON
  for a twenty-minute window in the past. Every refresh must pass LiteLLM's own
  integrity check -- a minimum entry count, and no shrink past half the copy
  already on disk -- before it may replace anything; a payload that fails it is
  discarded and the cache is kept. Where there is no cache to keep, the fetch
  falls back to a pinned immutable commit, which is what "SHA-pinned" buys: a
  floor that is a real artefact rather than a snapshot shipped in the package.
"""

import asyncio
import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from my_claude_code.application.cost import SOURCE_LITELLM, RateCard
from my_claude_code.config.paths import config_dir_path
from my_claude_code.core.model_ids import bare_model_id, candidate_ladder

#: The commit this integration was written and measured against. Immutable, so
#: it is the one URL that can never serve a half-written file, and it is the
#: floor a first fetch falls back to when ``main`` is unusable.
LITELLM_PINNED_COMMIT = "02522a5441a1aabc7791304a31a9cdcae6db4a37"

#: What the pinned commit actually served, for the record: 3,850 keys and
#: 2,336,737 bytes, measured 2026-09-07.
LITELLM_PINNED_ENTRY_COUNT = 3850

_RAW_BASE = "https://raw.githubusercontent.com/BerriAI/litellm"
_PRICES_FILENAME = "model_prices_and_context_window.json"

LITELLM_PRICES_URL = f"{_RAW_BASE}/main/{_PRICES_FILENAME}"
LITELLM_PINNED_URL = f"{_RAW_BASE}/{LITELLM_PINNED_COMMIT}/{_PRICES_FILENAME}"

#: The integrity floor, copied from LiteLLM's own loader. Well below the 3,850
#: at the pinned commit and far above anything a truncated body would parse to.
LITELLM_MIN_ENTRIES = 3_000

#: A refresh may not shrink the table past this fraction of what is on disk.
LITELLM_MAX_SHRINK_RATIO = 0.5

LITELLM_CACHE_TTL_SECONDS = 24 * 60 * 60
LITELLM_FETCH_TIMEOUT_SECONDS = 10.0
LITELLM_CACHE_DIRNAME = "cache"
LITELLM_CACHE_FILENAME = "litellm-prices.json"

#: Keys that are not models. ``sample_spec`` is the schema documentation and
#: prices everything at zero, which would otherwise make an unknown model look
#: free -- the exact failure this whole feature exists to avoid.
_NON_MODEL_KEYS = frozenset({"sample_spec", "fallback_generalizations"})

#: LiteLLM field -> :class:`RateCard` field. Every value is USD per token
#: already, so the conversion below is identity; it is written out rather than
#: assumed so the *other* source's division by 1e6 has a visible counterpart.
_RATE_FIELDS: tuple[tuple[str, str], ...] = (
    ("input_cost_per_token", "input_price"),
    ("output_cost_per_token", "output_price"),
    ("cache_read_input_token_cost", "cache_read_price"),
    ("cache_creation_input_token_cost", "cache_write_price"),
    ("output_cost_per_reasoning_token", "reasoning_price"),
)

#: The shortest provider name that may be matched by prefix. Below this,
#: "ai" would agree with half the catalogue.
_MIN_PROVIDER_PREFIX = 4


def litellm_cache_path() -> Path:
    """Return the default on-disk cache path for the LiteLLM price map."""
    return config_dir_path() / LITELLM_CACHE_DIRNAME / LITELLM_CACHE_FILENAME


@dataclass(frozen=True, slots=True)
class LiteLLMPriceCache:
    """Parsed LiteLLM price map with freshness."""

    index: Mapping[str, Any]
    fetched_at: datetime
    fresh: bool
    etag: str | None = None
    source_url: str | None = None


def read_litellm_cache(path: Path | None = None) -> LiteLLMPriceCache | None:
    """Return the cached price map, or ``None`` when absent, corrupt or short."""
    cache_path = path if path is not None else litellm_cache_path()
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    index = payload.get("index")
    fetched_raw = payload.get("fetched_at")
    if not isinstance(index, dict) or not isinstance(fetched_raw, str):
        return None
    try:
        fetched_at = datetime.fromisoformat(fetched_raw)
    except ValueError:
        return None
    if fetched_at.tzinfo is None:
        fetched_at = fetched_at.replace(tzinfo=UTC)
    validated_at = _cache_file_mtime(cache_path) or fetched_at
    age = (datetime.now(UTC) - validated_at).total_seconds()
    etag = payload.get("etag")
    source = payload.get("source_url")
    return LiteLLMPriceCache(
        index=index,
        fetched_at=fetched_at,
        fresh=age < LITELLM_CACHE_TTL_SECONDS,
        etag=etag if isinstance(etag, str) and etag else None,
        source_url=source if isinstance(source, str) and source else None,
    )


def _cache_file_mtime(cache_path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(cache_path.stat().st_mtime, UTC)
    except OSError, OverflowError, ValueError:
        return None


def write_litellm_cache(
    index: Mapping[str, Any],
    path: Path | None = None,
    etag: str | None = None,
    source_url: str | None = None,
) -> Path:
    """Atomically persist the price map with its provenance."""
    cache_path = path if path is not None else litellm_cache_path()
    payload: dict[str, Any] = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "index": index,
    }
    if etag:
        payload["etag"] = etag
    if source_url:
        payload["source_url"] = source_url
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    temp_path.replace(cache_path)
    return cache_path


def payload_passes_integrity(
    payload: Any, previous: Mapping[str, Any] | None = None
) -> bool:
    """LiteLLM's own check: a floor on entries, and no halving of the table.

    The upstream loader applies exactly these two rules before it will accept a
    refresh, for exactly the reason we need them: the file is served from a
    five-minute Fastly cache off a branch that changes a hundred times a week,
    and a truncated or mid-write body parses as a small, valid JSON object.
    """
    if not isinstance(payload, dict):
        return False
    if len(payload) < LITELLM_MIN_ENTRIES:
        return False
    return not (previous and len(payload) < len(previous) * LITELLM_MAX_SHRINK_RATIO)


@dataclass(frozen=True, slots=True)
class LiteLLMFetch:
    """Outcome of one conditional GET against the price map."""

    index: Mapping[str, Any] | None = None
    etag: str | None = None
    not_modified: bool = False
    source_url: str | None = None


async def fetch_litellm_prices(
    etag: str | None = None, url: str = LITELLM_PRICES_URL
) -> LiteLLMFetch | None:
    """Conditionally fetch the price map; ``None`` silently on any failure."""
    headers = {"If-None-Match": etag} if etag else {}
    try:
        async with httpx.AsyncClient(
            timeout=LITELLM_FETCH_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 304:
                return LiteLLMFetch(not_modified=True, etag=etag, source_url=url)
            response.raise_for_status()
            payload = response.json()
            response_etag = response.headers.get("etag")
    except Exception as exc:
        logger.debug("LiteLLM price fetch failed silently: {}", exc)
        return None
    if not isinstance(payload, dict):
        return None
    return LiteLLMFetch(
        index=payload,
        etag=response_etag if isinstance(response_etag, str) else None,
        source_url=url,
    )


async def refresh_litellm_cache(path: Path | None = None) -> bool:
    """Fetch, integrity-check and persist the price map; never raises.

    A payload that fails the integrity check is discarded and whatever is on
    disk is kept: a stale price is a knowable error, a truncated table is a
    silent one. With nothing on disk to keep, the pinned immutable commit is
    tried once as the floor.
    """
    cache_path = path if path is not None else litellm_cache_path()
    cached = read_litellm_cache(cache_path)
    previous = cached.index if cached else None
    fetched = await fetch_litellm_prices(cached.etag if cached else None)
    if fetched is not None and fetched.not_modified:
        return _touch_litellm_cache(cache_path)
    if fetched is not None and payload_passes_integrity(fetched.index, previous):
        return _store(cache_path, fetched)
    if fetched is not None:
        logger.debug(
            "LiteLLM price payload rejected by the integrity check; keeping the cache"
        )
    if previous is not None:
        return False
    pinned = await fetch_litellm_prices(None, LITELLM_PINNED_URL)
    if pinned is None or not payload_passes_integrity(pinned.index, None):
        return False
    return _store(cache_path, pinned)


def _store(cache_path: Path, fetched: LiteLLMFetch) -> bool:
    if fetched.index is None:
        return False
    try:
        write_litellm_cache(fetched.index, cache_path, fetched.etag, fetched.source_url)
    except OSError as exc:
        logger.debug("LiteLLM cache write failed silently: {}", exc)
        return False
    return True


def _touch_litellm_cache(cache_path: Path) -> bool:
    try:
        os.utime(cache_path, None)
    except OSError as exc:
        logger.debug("LiteLLM cache touch failed silently: {}", exc)
        return False
    return True


def schedule_litellm_refresh(path: Path | None = None) -> None:
    """Fire-and-forget background refresh; a later lookup picks up the cache."""
    try:
        task = asyncio.get_running_loop().create_task(refresh_litellm_cache(path))
    except RuntimeError:
        return
    task.add_done_callback(_swallow_refresh_outcome)


def _swallow_refresh_outcome(task: asyncio.Task[bool]) -> None:
    if task.cancelled():
        return
    task.exception()


# --------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------


def _normalize_provider(name: str) -> str:
    """Reduce a provider name to letters and digits, lowercased.

    MCC spells it ``open_router``; LiteLLM spells it ``openrouter``. Neither is
    wrong and neither is going to change, so the comparison is made on the one
    thing they agree about.
    """
    return "".join(character for character in name.lower() if character.isalnum())


def _provider_agrees(routed_provider: str, entry_provider: Any) -> bool:
    """Whether a bare key's own provider is the provider we routed to.

    LiteLLM qualifies some provider names with a catalogue suffix
    (``vertex_ai-language-models``), so the head before the first hyphen is
    what is compared, and a prefix match is accepted in either direction --
    ``vertex`` against ``vertexai``. Below four characters nothing matches by
    prefix, because at three "ai" would agree with most of the catalogue.
    """
    if not isinstance(entry_provider, str) or not entry_provider:
        return False
    routed = _normalize_provider(routed_provider)
    entry = _normalize_provider(entry_provider.split("-", 1)[0])
    if not routed or not entry:
        return False
    if routed == entry:
        return True
    if min(len(routed), len(entry)) < _MIN_PROVIDER_PREFIX:
        return False
    return routed.startswith(entry) or entry.startswith(routed)


def _rate_card(key: str, entry: Mapping[str, Any], label: str) -> RateCard | None:
    """Build a per-token rate card from one LiteLLM entry."""
    rates: dict[str, float | None] = {}
    for source_field, card_field in _RATE_FIELDS:
        value = entry.get(source_field)
        if isinstance(value, bool) or not isinstance(value, int | float):
            rates[card_field] = None
            continue
        # Already USD per single token. models.dev divides by 1e6 here; this
        # is the other half of the pair the unit test pins together.
        rates[card_field] = float(value)
    card = RateCard(source=SOURCE_LITELLM, tier_label=f"{label} ({key})", **rates)
    return None if card.is_empty else card


def litellm_rate_card(
    provider_id: str, model_id: str, path: Path | None = None
) -> RateCard | None:
    """Return LiteLLM's rates for one routed (provider, model), or ``None``.

    Prefixed keys are tried first -- 3,220 of the 3,850 keys are prefixed, so
    prefix-first is the higher-hit-rate order, and a key that names the provider
    needs no further agreement check. Only then are the bare rungs tried, and a
    bare key must agree with the routed provider before its price is accepted.
    """
    cache = read_litellm_cache(path)
    if cache is None:
        return None
    index = cache.index
    routed = provider_id.strip().lower()
    bare = bare_model_id(model_id)
    normalized = _normalize_provider(routed)

    for prefix in dict.fromkeys((routed, normalized)):
        if not prefix:
            continue
        for name in dict.fromkeys((model_id.strip().lower(), bare)):
            entry = index.get(f"{prefix}/{name}")
            if isinstance(entry, Mapping):
                card = _rate_card(f"{prefix}/{name}", entry, "prefixed key")
                if card is not None:
                    return card

    for tier, candidate in candidate_ladder(model_id):
        if candidate in _NON_MODEL_KEYS:
            continue
        entry = index.get(candidate)
        if not isinstance(entry, Mapping):
            continue
        if not _provider_agrees(routed, entry.get("litellm_provider")):
            # The ``gemini-2.5-pro``-is-Vertex trap. A same-name row from
            # another seller is a different deployment at a different price,
            # and accepting it here would launder it into this provider's bill.
            continue
        card = _rate_card(candidate, entry, tier.name.lower())
        if card is not None:
            return card
    return None


__all__ = [
    "LITELLM_MAX_SHRINK_RATIO",
    "LITELLM_MIN_ENTRIES",
    "LITELLM_PINNED_COMMIT",
    "LITELLM_PINNED_URL",
    "LITELLM_PRICES_URL",
    "LiteLLMPriceCache",
    "litellm_cache_path",
    "litellm_rate_card",
    "payload_passes_integrity",
    "read_litellm_cache",
    "refresh_litellm_cache",
    "schedule_litellm_refresh",
    "write_litellm_cache",
]
