"""A marketplace's market snapshot: fetched, merged and handed to the store (7.109.0).

"1,000 offers per model" are a handful of price levels with a count of seller
accounts at each (RES §1). The snapshot keeps exactly that, for every model of
one marketplace, in one small document beside the catalogue (spec §8: about
0.1 MB at the median of 4 levels, 0.3 MB at the maximum of 29):

* the **keyed list's own levels** -- each list row's ``asks_*``, regrouped into
  levels by the provider's parser before the row was stored -- and the official
  and cheapest prices on the row (``official_*``, ``min_ask_*``);
* while ``MARKETPLACE_PUBLIC_MARKET`` is on, the **keyless public pages**
  (``config/marketplaces.py`` ``market_sources``): cheapest/dearest ask and last
  rate per model, success / TTFT / tokens per second per model, the prompt-cache
  hit rate, the platform's cap on any ask;
* with the key, the account's **combos** and each upstream's **prompt note**,
  word for word (the user's Q7, Q15), shown read-only beside the Models page.

Discipline, the OpenRouter live list's:

* **Only while the marketplace has a key.** An install without one reads
  nothing at all -- not even the public pages.
* **Background only.** Fetched after a catalogue sweep (hourly, and on the
  operator's models refresh), never on a request path; a request only reads
  the stored document.
* **Masked.** Every read leaves through the provider's own proxy chain, the
  one exit :func:`~my_claude_code.providers.runtime.config.masked_exit_for`
  picks, as the Providers card's probes do; nothing is sent where the chain
  says no. TLS is never weakened.
* **A failed source keeps what it said last.** The previous document's part
  for that source is carried over, never dropped (the shrink-guard rule).

The page scrape that would name per-level counts publicly (``/pricing``) is
not read (the user's Q9): undocumented, 1 MB, breaks on a redeploy.
"""

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from loguru import logger

from my_claude_code.config.credentials import parse_credential_keys
from my_claude_code.config.marketplaces import MarketplaceDialect
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import Settings
from my_claude_code.providers.socks_deadline import bound_socks_handshake

from .config import masked_exit_for, string_setting

#: Fields each source contributes to a model's entry, so a failed source's
#: part can be carried over from the previous document.
_MODEL_FIELDS_BY_SOURCE: Mapping[str, tuple[str, ...]] = {
    # The keyed list's own part: carried over when no list rows were at hand.
    "listing": ("official", "levels_in", "levels_out", "listing_min"),
    "market": ("market_min", "max", "last"),
    "status": ("perf",),
    "pricing_cache": ("cache_hit",),
}
_TOP_FIELDS_BY_SOURCE: Mapping[str, str] = {
    "pricing_config": "platform",
    "catalog": "notes",
    "combos": "combos",
}


@dataclass(frozen=True, slots=True)
class MarketRefresh:
    """What one refresh did, for the log and the tests."""

    status: str
    sources: Mapping[str, str] = field(default_factory=dict)
    payload: Mapping[str, Any] | None = None
    proxy_exit: str | None = None


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _pair(first: Any, second: Any) -> list[float] | None:
    left, right = _number(first), _number(second)
    if left is None and right is None:
        return None
    return [left, right] if left is not None and right is not None else None


def _levels(value: Any) -> list[list[float | int]] | None:
    """The summarised ``{"levels": [[price, count]...]}`` a list row carries."""

    levels = value.get("levels") if isinstance(value, Mapping) else None
    if not isinstance(levels, list):
        return None
    out: list[list[float | int]] = []
    for entry in levels:
        if (
            isinstance(entry, Sequence)
            and not isinstance(entry, str)
            and len(entry) == 2
        ):
            price, count = _number(entry[0]), entry[1]
            if (
                price is not None
                and isinstance(count, int)
                and not isinstance(count, bool)
            ):
                out.append([price, count])
    return out or None


def listing_models(
    dialect: MarketplaceDialect, rows: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Each keyed list row's market part: official, levels, cheapest ask."""

    models: dict[str, dict[str, Any]] = {}
    for model_id, row in sorted(rows.items()):
        pricing = row.get(dialect.pricing_field)
        if not isinstance(pricing, Mapping):
            continue
        entry: dict[str, Any] = {}
        official = _pair(
            pricing.get(dialect.official_price_fields[0]),
            pricing.get(dialect.official_price_fields[1]),
        )
        if official is not None:
            entry["official"] = official
        levels_in = _levels(pricing.get(dialect.ask_fields[0]))
        levels_out = _levels(pricing.get(dialect.ask_fields[1]))
        if levels_in:
            entry["levels_in"] = levels_in
        if levels_out:
            entry["levels_out"] = levels_out
        minimum = _pair(
            pricing.get(dialect.min_ask_fields[0]),
            pricing.get(dialect.min_ask_fields[1]),
        )
        if minimum is not None:
            entry["listing_min"] = minimum
        if entry:
            models[model_id] = entry
    return models


def _items(payload: Any, key: str) -> list[Mapping[str, Any]]:
    if isinstance(payload, Mapping):
        payload = payload.get(key)
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, Mapping)]


def _merge_public(
    models: dict[str, dict[str, Any]], name: str, payload: Any
) -> dict[str, Any]:
    """Fold one public or keyed source into the document; return its top part."""

    top: dict[str, Any] = {}
    if name == "market":
        for item in _items(payload, "models"):
            slug = item.get("slug")
            if not isinstance(slug, str):
                continue
            entry = models.setdefault(slug, {})
            minimum = _pair(item.get("minAskIn"), item.get("minAskOut"))
            maximum = _pair(item.get("maxAskIn"), item.get("maxAskOut"))
            if minimum is not None:
                entry["market_min"] = minimum
            if maximum is not None:
                entry["max"] = maximum
            last = _number(item.get("lastRate"))
            if last is not None:
                entry["last"] = last
    elif name == "status":
        for item in _items(payload, "models"):
            model_id = item.get("id")
            if not isinstance(model_id, str):
                continue
            perf = {
                key: value
                for key, value in (
                    ("success_pct", _number(item.get("successRatePct"))),
                    ("ttft_ms", _number(item.get("avgTtftMs"))),
                    ("tps", _number(item.get("avgTps"))),
                )
                if value is not None
            }
            if perf:
                models.setdefault(model_id, {})["perf"] = perf
    elif name == "pricing_cache":
        for item in _items(payload, "models"):
            slug, rate = item.get("slug"), _number(item.get("hitRate"))
            if isinstance(slug, str) and rate is not None:
                models.setdefault(slug, {})["cache_hit"] = rate
    elif name == "pricing_config":
        cap = (
            _number(payload.get("maxAskPctOfOfficial"))
            if isinstance(payload, Mapping)
            else None
        )
        top["platform"] = {} if cap is None else {"max_ask_pct_of_official": cap}
    elif name == "catalog":
        notes: dict[str, str] = {}
        for item in (
            _items(payload, "upstreams")
            or _items(payload, "data")
            or (
                [item for item in payload if isinstance(item, Mapping)]
                if isinstance(payload, list)
                else []
            )
        ):
            prefix, note = item.get("prefix"), item.get("systemPromptNote")
            if isinstance(prefix, str) and isinstance(note, str) and note.strip():
                notes[prefix.strip().lower()] = note
        top["notes"] = dict(sorted(notes.items()))
    elif name == "combos":
        combos: list[dict[str, Any]] = []
        entries = (
            _items(payload, "combos")
            or _items(payload, "data")
            or (
                [item for item in payload if isinstance(item, Mapping)]
                if isinstance(payload, list)
                else []
            )
        )
        for item in entries:
            slug = item.get("slug")
            if not isinstance(slug, str) or not slug.strip():
                continue
            members: list[str] = []
            for member in item.get("members") or []:
                if isinstance(member, str):
                    members.append(member)
                elif isinstance(member, Mapping):
                    named = (
                        member.get("model") or member.get("alias") or member.get("id")
                    )
                    if isinstance(named, str):
                        members.append(named)
            combo: dict[str, Any] = {"id": f"combo/{slug.strip()}", "members": members}
            selection = item.get("selection")
            if isinstance(selection, str):
                combo["selection"] = selection
            combos.append(combo)
        top["combos"] = sorted(combos, key=lambda combo: combo["id"])
    return top


def _finish(models: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each model's ``min``: its own row's cheapest ask, else the public market's."""

    finished: dict[str, dict[str, Any]] = {}
    for model_id in sorted(models):
        entry = dict(models[model_id])
        minimum = entry.get("listing_min") or entry.get("market_min")
        if minimum is not None:
            entry["min"] = minimum
        finished[model_id] = {key: entry[key] for key in sorted(entry)}
    return finished


def build_snapshot(
    dialect: MarketplaceDialect,
    rows: Mapping[str, Mapping[str, Any]],
    fetched: Mapping[str, Any],
    statuses: Mapping[str, str],
    previous: Mapping[str, Any] | None,
    *,
    at: str,
) -> dict[str, Any]:
    """The snapshot document: deterministic, and a failed source carried over."""

    models = listing_models(dialect, rows)
    top: dict[str, Any] = {}
    for source in dialect.market_sources:
        if statuses.get(source.name) == "ok":
            top.update(_merge_public(models, source.name, fetched.get(source.name)))
    old_models = previous.get("models") if isinstance(previous, Mapping) else None
    for source_name, fields in _MODEL_FIELDS_BY_SOURCE.items():
        if statuses.get(source_name) == "ok" or not isinstance(old_models, Mapping):
            continue
        for model_id, old in old_models.items():
            if not isinstance(old, Mapping):
                continue
            for name in fields:
                if name in old:
                    models.setdefault(str(model_id), {}).setdefault(name, old[name])
    for source_name, name in _TOP_FIELDS_BY_SOURCE.items():
        if (
            source_name not in top
            and isinstance(previous, Mapping)
            and name in previous
            and statuses.get(source_name) != "ok"
        ):
            top[name] = previous[name]
    return {
        "dialect": dialect.dialect_id,
        "fetched_at": at,
        "sources": {name: statuses[name] for name in sorted(statuses)},
        "platform": top.get("platform", {}),
        "notes": top.get("notes", {}),
        "combos": top.get("combos", []),
        "models": _finish(models),
    }


async def fetch_marketplace_source(
    url: str, *, proxy: str | None, timeout: float, key: str | None
) -> Any:
    """One GET of one source; raises on any failure."""

    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    client = bound_socks_handshake(
        httpx.AsyncClient(proxy=proxy or None, timeout=timeout)
    )
    try:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()
    finally:
        await client.aclose()


async def refresh_market_snapshot(
    settings: Settings,
    provider_id: str,
    dialect: MarketplaceDialect,
    rows: Mapping[str, Mapping[str, Any]],
    previous: Mapping[str, Any] | None,
) -> MarketRefresh:
    """Read every source through the provider's chain and build the document.

    Never raises. ``rows`` are the provider's stored list rows (already
    summarised); ``previous`` the stored document, for carried-over parts.
    """

    descriptor = PROVIDER_CATALOG.get(provider_id)
    if descriptor is None:
        return MarketRefresh(status="off")
    keys = parse_credential_keys(string_setting(settings, descriptor.credential_attr))
    if not keys:
        return MarketRefresh(status="no_key")
    key = keys[0]
    try:
        exit_ = masked_exit_for(
            provider_id,
            string_setting(settings, descriptor.proxy_attr) or None,
            settings,
            name=descriptor.display_name,
        )
    except Exception as exc:
        logger.debug("Market snapshot: no exit resolved: {}", type(exc).__name__)
        return MarketRefresh(status="failed")
    if exit_.refused:
        logger.info("{} market snapshot not fetched: {}", dialect.label, exit_.refused)
        return MarketRefresh(status="not_sent")
    base = (
        string_setting(settings, "inferhub_market_base_url")
        or dialect.default_market_base
    ).rstrip("/")
    public = bool(getattr(settings, "marketplace_public_market", True))
    timeout = float(settings.models_dev_fetch_timeout_seconds)
    statuses: dict[str, str] = {}
    fetched: dict[str, Any] = {}
    for source in dialect.market_sources:
        if not source.keyed and not public:
            statuses[source.name] = "off"
            continue
        try:
            fetched[source.name] = await fetch_marketplace_source(
                f"{base}{source.path}",
                proxy=exit_.proxy,
                timeout=timeout,
                key=key if source.keyed else None,
            )
            statuses[source.name] = "ok"
        except asyncio.CancelledError:
            raise
        except httpx.HTTPStatusError as exc:
            # A key that may not read management data (``inference_only``)
            # answers 403: "not readable with this key", never a zero.
            statuses[source.name] = f"http_{exc.response.status_code}"
        except Exception as exc:
            statuses[source.name] = f"failed:{type(exc).__name__}"
    statuses["listing"] = "ok" if rows else "empty"
    payload = build_snapshot(dialect, rows, fetched, statuses, previous, at=_now())
    logger.info(
        "{} market snapshot built: {} models ({})",
        dialect.label,
        len(payload["models"]),
        ", ".join(f"{name} {state}" for name, state in sorted(statuses.items())),
    )
    return MarketRefresh(
        status="built", sources=statuses, payload=payload, proxy_exit=exit_.label
    )


__all__ = [
    "MarketRefresh",
    "build_snapshot",
    "fetch_marketplace_source",
    "listing_models",
    "refresh_market_snapshot",
]
