"""models.dev metadata fallback for custom and thin built-in providers.

Fetches https://models.dev/api.json (10s timeout) and caches it at
``config_dir_path()/cache/models-dev.json`` with a ``fetched_at`` timestamp.
The cache is used when it is fresh (<24h); a stale cache is still used while a
background refresh is scheduled. Everything is fully silent when offline:
discovery never fails because models.dev is unreachable.

Refreshes are conditional. models.dev serves a strong ``ETag`` (and no
``Last-Modified``), so the stored ETag is replayed as ``If-None-Match`` and an
unchanged index answers ``304`` instead of re-downloading ~4.4 MB. A ``304``
leaves the payload on disk untouched and only stamps the file's mtime, which is
what freshness is measured from.
"""

import asyncio
import json
import os
import threading
import uuid
from collections import Counter
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from my_claude_code.application.model_metadata import (
    ModelReasoningCapability,
    ProviderModelInfo,
)
from my_claude_code.config.paths import config_dir_path
from my_claude_code.core.model_ids import (
    STRIPPABLE_MODEL_ID_TAGS,
    ResolutionTier,
    candidate_ladder,
    normalize_candidates,
    retagged_model_ids,
    strip_model_id_tag,
)
from my_claude_code.core.reasoning import EFFORT_BY_VALUE, ReasoningEffort

MODELS_DEV_URL = "https://models.dev/api.json"
MODELS_DEV_CACHE_TTL_SECONDS = 24 * 60 * 60
MODELS_DEV_FETCH_TIMEOUT_SECONDS = 10.0
MODELS_DEV_CACHE_DIRNAME = "cache"
MODELS_DEV_CACHE_FILENAME = "models-dev.json"


def models_dev_cache_path() -> Path:
    """Return the default on-disk cache path for the models.dev index."""
    return config_dir_path() / MODELS_DEV_CACHE_DIRNAME / MODELS_DEV_CACHE_FILENAME


@dataclass(frozen=True, slots=True)
class ModelsDevCache:
    """Parsed models.dev cache payload with freshness."""

    index: Mapping[str, Any]
    fetched_at: datetime
    fresh: bool
    # The ``ETag`` models.dev served with this payload, replayed as
    # ``If-None-Match`` on the next refresh. ``None`` for a cache written
    # before ETags were stored, or by a response that carried none.
    etag: str | None = None
    # When the payload was last *confirmed current* upstream: the file's mtime,
    # which a ``304`` advances without rewriting 4.4 MB. Freshness is measured
    # from this, not from ``fetched_at``, or a validated-but-unchanged index
    # would look stale forever and schedule a refresh on every lookup.
    # ``fetched_at`` stays as the provenance of the bytes on disk.
    validated_at: datetime | None = None


def read_models_dev_cache(path: Path | None = None) -> ModelsDevCache | None:
    """Return the cached models.dev index, or None when absent/corrupt."""
    cache_path = path if path is not None else models_dev_cache_path()
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
    raw_etag = payload.get("etag")
    validated_at = _cache_file_mtime(cache_path) or fetched_at
    age = (datetime.now(UTC) - validated_at).total_seconds()
    return ModelsDevCache(
        index=index,
        fetched_at=fetched_at,
        fresh=age < MODELS_DEV_CACHE_TTL_SECONDS,
        etag=raw_etag if isinstance(raw_etag, str) and raw_etag else None,
        validated_at=validated_at,
    )


def _cache_file_mtime(cache_path: Path) -> datetime | None:
    try:
        return datetime.fromtimestamp(cache_path.stat().st_mtime, UTC)
    except OSError, OverflowError, ValueError:
        return None


def models_dev_provider_model_ids(
    provider: str, path: Path | None = None
) -> frozenset[str]:
    """Return the model ids models.dev publishes for one provider.

    Empty when the cache is missing or does not know the provider, so a caller
    can fall back to whatever it knows statically rather than losing models on
    a fresh install with no network.
    """

    cache = read_models_dev_cache(path)
    if cache is None:
        return frozenset()
    bucket = cache.index.get(provider)
    if not isinstance(bucket, Mapping):
        return frozenset()
    models = bucket.get("models")
    if not isinstance(models, Mapping):
        return frozenset()
    return frozenset(
        model_id for model_id in models if isinstance(model_id, str) and model_id
    )


def write_models_dev_cache(
    index: Mapping[str, Any], path: Path | None = None, etag: str | None = None
) -> Path:
    """Atomically persist the models.dev index with a fetch timestamp."""
    cache_path = path if path is not None else models_dev_cache_path()
    payload: dict[str, Any] = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "index": index,
    }
    if etag:
        payload["etag"] = etag
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(f".{cache_path.name}.{uuid.uuid4().hex}.tmp")
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=True) + "\n", encoding="utf-8"
    )
    temp_path.replace(cache_path)
    return cache_path


@dataclass(frozen=True, slots=True)
class ModelsDevFetch:
    """Outcome of one conditional GET against models.dev."""

    index: Mapping[str, Any] | None = None
    etag: str | None = None
    # True when the server answered 304: the cached payload is still current
    # and no body was transferred.
    not_modified: bool = False


async def fetch_models_dev_index(etag: str | None = None) -> ModelsDevFetch | None:
    """Conditionally fetch the models.dev index; None silently on any failure.

    ``etag`` is the value stored beside the cached payload. models.dev sends
    ``Cache-Control: max-age=0, must-revalidate`` and no ``Last-Modified``, so
    ``If-None-Match`` is the only conditional request it honours.
    """
    headers = {"If-None-Match": etag} if etag else {}
    try:
        async with httpx.AsyncClient(
            timeout=MODELS_DEV_FETCH_TIMEOUT_SECONDS
        ) as client:
            response = await client.get(MODELS_DEV_URL, headers=headers)
            if response.status_code == 304:
                return ModelsDevFetch(not_modified=True, etag=etag)
            response.raise_for_status()
            payload = response.json()
            response_etag = response.headers.get("etag")
    except Exception as exc:
        logger.debug("models.dev fetch failed silently: {}", exc)
        return None
    if not isinstance(payload, dict):
        return None
    return ModelsDevFetch(
        index=payload,
        etag=response_etag if isinstance(response_etag, str) else None,
    )


async def refresh_models_dev_cache(path: Path | None = None) -> bool:
    """Fetch and persist the models.dev index; never raises."""
    cache_path = path if path is not None else models_dev_cache_path()
    cached = read_models_dev_cache(cache_path)
    fetched = await fetch_models_dev_index(cached.etag if cached else None)
    if fetched is None:
        return False
    if fetched.not_modified:
        return _touch_models_dev_cache(cache_path)
    if fetched.index is None:
        return False
    try:
        write_models_dev_cache(fetched.index, cache_path, fetched.etag)
    except OSError as exc:
        logger.debug("models.dev cache write failed silently: {}", exc)
        return False
    return True


def _touch_models_dev_cache(cache_path: Path) -> bool:
    """Record that an unchanged payload was revalidated, without rewriting it.

    Bumping the mtime is the whole point of the conditional GET: the 4.4 MB
    body stays on disk exactly as it was, and freshness (which reads the mtime)
    restarts. The parsed-index memos key on the same mtime, so they rebuild
    once per revalidation rather than on every lookup.
    """
    try:
        os.utime(cache_path, None)
    except OSError as exc:
        logger.debug("models.dev cache touch failed silently: {}", exc)
        return False
    return True


def schedule_models_dev_refresh(path: Path | None = None) -> None:
    """Fire-and-forget background refresh; a later run picks up the cache."""
    try:
        task = asyncio.get_running_loop().create_task(refresh_models_dev_cache(path))
    except RuntimeError:
        return
    task.add_done_callback(_swallow_refresh_outcome)


def _swallow_refresh_outcome(task: asyncio.Task[bool]) -> None:
    if task.cancelled():
        return
    task.exception()


@dataclass(frozen=True, slots=True)
class _ModelsDevModelMetadata:
    context_length: int | None
    input_price: float | None
    output_price: float | None
    supports_vision: bool | None


def _flatten_index(index: Mapping[str, Any]) -> dict[str, _ModelsDevModelMetadata]:
    """Flatten models.dev providers into normalized model-id match keys.

    Used only as the fallback for a model whose own provider models.dev does
    not describe, which is most of what a gateway resells.

    Entries accumulate field by field rather than first-writer-wins. On the
    live 2026-08 index 3,757 model ids are claimed by more than one provider,
    so with `setdefault` the answer for a shared name was decided by whichever
    order models.dev happened to serialise its JSON -- including 234 ids whose
    providers disagree about whether the model reads images. Accumulating
    cannot remove anything and does not depend on that order for whether a
    field is populated at all.
    """
    flattened: dict[str, _ModelsDevModelMetadata] = {}
    for provider_bucket in index.values():
        if not isinstance(provider_bucket, Mapping):
            continue
        models = provider_bucket.get("models")
        if not isinstance(models, Mapping):
            continue
        for model_id, metadata in models.items():
            if not isinstance(model_id, str) or not isinstance(metadata, Mapping):
                continue
            parsed = _parse_models_dev_metadata(metadata)
            for candidate in normalize_candidates(model_id):
                existing = flattened.get(candidate)
                flattened[candidate] = (
                    parsed if existing is None else _merge_metadata(existing, parsed)
                )
    return flattened


def _parse_models_dev_metadata(
    metadata: Mapping[str, Any],
) -> _ModelsDevModelMetadata:
    cost = metadata.get("cost")
    limit = metadata.get("limit")
    input_price = (
        _float_or_none(cost.get("input")) if isinstance(cost, Mapping) else None
    )
    output_price = (
        _float_or_none(cost.get("output")) if isinstance(cost, Mapping) else None
    )
    # models.dev's schema permits ``limit.context: 0`` and ``limit.output: 0``
    # despite its own error text claiming they must be positive. Measured on
    # the live 2026-08 feed: 132 models publish ``limit.context == 0`` and 195
    # publish ``limit.output == 0`` -- overwhelmingly image/video entries plus
    # real holes on ``vercel`` (91) and ``poe`` (44). Zero means "not
    # applicable / unknown" and must read as absent everywhere, never as a
    # ceiling of zero.
    context_length = (
        _positive_int_or_none(limit.get("context"))
        if isinstance(limit, Mapping)
        else None
    )
    return _ModelsDevModelMetadata(
        context_length=context_length,
        input_price=input_price,
        output_price=output_price,
        supports_vision=_accepts_image_input(metadata),
    )


def _accepts_image_input(metadata: Mapping[str, Any]) -> bool | None:
    """Read image support from a models.dev entry, or None when unstated."""
    modalities = metadata.get("modalities")
    if isinstance(modalities, Mapping):
        inputs = modalities.get("input")
        if isinstance(inputs, list):
            return any(
                isinstance(item, str) and item.strip().lower() == "image"
                for item in inputs
            )
    # Older entries predate ``modalities`` and only carry ``attachment``, which
    # is broader than images but is the only signal those rows have.
    attachment = metadata.get("attachment")
    return attachment if isinstance(attachment, bool) else None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _positive_int_or_none(value: Any) -> int | None:
    """Read a limit, treating a non-positive published value as unreported."""
    parsed = _int_or_none(value)
    return parsed if parsed is not None and parsed > 0 else None


def _provider_bucket_metadata(
    index: Mapping[str, Any], provider_id: str | None
) -> dict[str, _ModelsDevModelMetadata]:
    """Parse just one provider's models, keyed the same way as the flat index.

    The flat index is every provider's models in one namespace, first writer
    wins, and the winner is decided by the order models.dev happens to serialise
    its JSON. On a 2026-08 snapshot 984 of 3,873 keys were claimed by more than
    one provider and 240 of those disagreed about whether the model can read
    images -- so a model could be told it was blind because a different
    provider hosts something with the same name.

    Looking here first makes a model's own provider the authority on it.
    """
    if not provider_id:
        return {}
    # MCC's provider ids and models.dev's do not always agree (open_router vs
    # openrouter, fireworks vs fireworks-ai); the alias map is the same one the
    # reasoning-capability lookup uses.
    bucket = index.get(provider_id)
    if not isinstance(bucket, Mapping):
        alias = PROVIDER_ID_ALIASES.get(provider_id)
        bucket = index.get(alias) if alias else None
    if not isinstance(bucket, Mapping):
        return {}
    models = bucket.get("models")
    if not isinstance(models, Mapping):
        return {}
    scoped: dict[str, _ModelsDevModelMetadata] = {}
    for model_id, metadata in models.items():
        if not isinstance(model_id, str) or not isinstance(metadata, Mapping):
            continue
        parsed = _parse_models_dev_metadata(metadata)
        for candidate in normalize_candidates(model_id):
            scoped.setdefault(candidate, parsed)
    return scoped


def _match_metadata(
    table: Mapping[str, _ModelsDevModelMetadata], model_id: str
) -> _ModelsDevModelMetadata | None:
    """Longest normalized candidate wins, so the most specific name matches."""
    return next(
        (
            table[candidate]
            for candidate in sorted(
                normalize_candidates(model_id), key=len, reverse=True
            )
            if candidate in table
        ),
        None,
    )


def _prefer_own_provider(
    scoped: _ModelsDevModelMetadata | None,
    fallback: _ModelsDevModelMetadata | None,
) -> _ModelsDevModelMetadata | None:
    """Merge two matches field by field, the model's own provider winning.

    Not "scoped or fallback": a provider that publishes a model but describes
    it sparsely would then strip fields the flat index could still supply.
    Measured against the live 2026-08 index, choosing wholesale cost 11 models
    metadata they have today, which is a regression however much more correct
    the source is.

    Per field, the model's own provider is authoritative where it says
    anything at all, and a namesake elsewhere fills only the gaps.
    """
    if scoped is None:
        return fallback
    if fallback is None:
        return scoped
    return _merge_metadata(scoped, fallback)


def _merge_metadata(
    primary: _ModelsDevModelMetadata, secondary: _ModelsDevModelMetadata
) -> _ModelsDevModelMetadata:
    """Field-by-field merge of two known matches; ``primary`` wins each field."""
    return _ModelsDevModelMetadata(
        supports_vision=(
            primary.supports_vision
            if primary.supports_vision is not None
            else secondary.supports_vision
        ),
        context_length=primary.context_length or secondary.context_length,
        input_price=(
            primary.input_price
            if primary.input_price is not None
            else secondary.input_price
        ),
        output_price=(
            primary.output_price
            if primary.output_price is not None
            else secondary.output_price
        ),
    )


def enrich_model_infos(
    model_infos: Iterable[ProviderModelInfo],
    index: Mapping[str, Any],
    provider_id: str | None = None,
) -> tuple[ProviderModelInfo, ...]:
    """Fill models.dev metadata on model infos via name-normalized matching.

    The model's own provider is consulted first and the cross-provider index
    only as a fallback. Deliberately a fallback rather than a replacement: some
    models match only in another provider's bucket, and dropping that would
    take metadata away from models that have it today. Nothing loses
    information here -- some models stop being described by a namesake hosted
    somewhere else.
    """
    scoped = _provider_bucket_metadata(index, provider_id)
    flattened = _flatten_index(index)
    if not scoped and not flattened:
        return tuple(model_infos)
    enriched: list[ProviderModelInfo] = []
    for info in model_infos:
        metadata = _prefer_own_provider(
            _match_metadata(scoped, info.model_id),
            _match_metadata(flattened, info.model_id),
        )
        if metadata is None:
            enriched.append(info)
            continue
        enriched.append(
            replace(
                info,
                supports_vision=(
                    info.supports_vision
                    if info.supports_vision is not None
                    else metadata.supports_vision
                ),
                context_length=info.context_length or metadata.context_length,
                input_price=(
                    info.input_price
                    if info.input_price is not None
                    else metadata.input_price
                ),
                output_price=(
                    info.output_price
                    if info.output_price is not None
                    else metadata.output_price
                ),
            )
        )
    return tuple(enriched)


async def enrich_provider_model_infos(
    model_infos: Iterable[ProviderModelInfo],
    path: Path | None = None,
    provider_id: str | None = None,
) -> tuple[ProviderModelInfo, ...]:
    """Enrich model infos from the models.dev cache; schedule a refresh.

    Never performs blocking network I/O: a missing or stale cache schedules a
    fire-and-forget refresh and the current infos pass through unchanged.
    """
    infos = tuple(model_infos)
    cache = read_models_dev_cache(path)
    if cache is None or not cache.fresh:
        schedule_models_dev_refresh(path)
    if cache is None:
        return infos
    return enrich_model_infos(infos, cache.index, provider_id)


# --------------------------------------------------------------------------
# Reasoning capability lookup (data + lookup only; no request-building code
# reads this yet — that is a later PR).
# --------------------------------------------------------------------------

# This project's provider ids don't always match models.dev's provider ids.
# This is the single place that maps one onto the other; extend it here, not
# with a parallel matcher elsewhere. Providers absent from this map are
# assumed to share their id with models.dev (checked first, so an alias entry
# is only needed when the ids genuinely differ).
PROVIDER_ID_ALIASES: dict[str, str] = {
    "open_router": "openrouter",
    "nvidia_nim": "nvidia",
    "fireworks": "fireworks-ai",
    "together": "togetherai",
    "novita": "novita-ai",
    "bedrock": "amazon-bedrock",
    "gemini": "google",
    "vertex": "google-vertex",
    "azure_openai": "azure",
    "cline": "cline-pass",
    "kimi_coding": "kimi-for-coding",
    "alibaba_cn": "alibaba-cn",
    "alibaba_coding": "alibaba-coding-plan",
    "alibaba_coding_cn": "alibaba-coding-plan-cn",
    "ollama_cloud": "ollama-cloud",
    "chatgpt_oauth": "openai",
    "anthropic_oauth": "anthropic",
    "github_models": "github-copilot",
    "opencode_go": "opencode-go",
    "wafer": "wafer.ai",
    # Our KIMI_DEFAULT_BASE is https://api.moonshot.ai/v1, which is exactly
    # what models.dev files under "moonshotai" (name "Moonshot AI").
    "kimi": "moonshotai",
    # Our CLOUDFLARE_AI_REST_ROOT is https://api.cloudflare.com/client/v4 --
    # the Workers AI REST root, not the AI Gateway host
    # (gateway.ai.cloudflare.com) -- so "cloudflare-workers-ai" is the match.
    "cloudflare": "cloudflare-workers-ai",
}

# Deliberately NOT aliased. These were audited against the live models.dev
# index and rejected as different products. An unknown provider means "behave
# exactly as today", so a wrong match is strictly worse than no match: it
# would inject another product's reasoning capability data.
#   llamacpp        -> llama          models.dev "llama" is Meta's hosted API;
#                                     llamacpp is a local server.
#   ollama          -> ollama-cloud   local Ollama serves whatever the user
#                                     pulled, not the cloud catalogue.
#   nararoute       -> orcarouter     unrelated service; name-distance artifact.
#   tokenrouter     -> openrouter     different company.
#   qwencloud       -> ebcloud        unrelated.
#   mistral_codestral -> mistral      investigated and refused, not deferred:
#                                     https://codestral.mistral.ai/v1/models
#                                     answers HTTP 404 ("no Route matched with
#                                     those values"), so the endpoint publishes
#                                     no model list and what it serves cannot
#                                     be verified. models.dev's "mistral" entry
#                                     holds exactly one codestral model,
#                                     "codestral-latest", reporting
#                                     reasoning=False. The alias would
#                                     therefore cover a single model, and the
#                                     only thing it would assert is a
#                                     SUPPRESSING claim (turn reasoning off)
#                                     about a deployment we cannot inspect.
#                                     Wrong in that direction is worse than
#                                     unknown, which behaves exactly as today.
# These six have no models.dev bucket at all: agnes, commandcode,
# featherless, nous_portal, qwencloud_coding, sambanova. For them -- and only
# for them -- the approximate cross-provider tier below supplies an answer;
# see ``_cross_provider_match``. A provider that HAS a bucket is still never
# allowed to read outside it, because a wrong same-name row would then
# override its own provider's authoritative one.


def _single_tagged_variant[T](bucket: Mapping[str, T], model_id: str) -> T | None:
    """Find the one allow-list-tagged variant of an untagged ``model_id``.

    The reverse of :func:`strip_model_id_tag`: a user who configures
    ``foo`` should still match an index that only lists ``foo:free``. Used ONLY
    when exactly one such variant exists in this bucket -- with both
    ``foo:free`` and ``foo:nitro`` present, picking either would assert a
    variant the user did not write, so the answer stays unknown.

    Costs at most one dict lookup per allow-listed tag per candidate, so no
    side index is needed: this stays cheap enough to run per request.
    """
    for candidate in sorted(normalize_candidates(model_id), key=len, reverse=True):
        if ":" in candidate:
            # A query that already carries a tag is the forward case.
            continue
        matches = [
            bucket[key]
            for tag in sorted(STRIPPABLE_MODEL_ID_TAGS)
            for separator in (":", "-")
            if (key := f"{candidate}{separator}{tag}") in bucket
        ]
        if len(matches) > 1:
            return None
        if matches:
            return matches[0]
    return None


def _lookup_in_bucket_tiered[T](
    bucket: Mapping[str, T], model_id: str
) -> tuple[T, ResolutionTier] | None:
    """Find ``model_id`` in one provider bucket, reporting the rung that hit.

    Tier 3 is the id exactly as routed; tier 4 is the same id with its
    pricing/routing tag stripped, and is also where the reverse case (an
    untagged query against an index that only lists a tagged variant) lands.
    Never looks outside ``bucket``; an exact hit always beats either fallback,
    so "x" and "x:free" coexisting keep their own distinct entries.
    """
    stripped = strip_model_id_tag(model_id)
    rungs: tuple[tuple[ResolutionTier, set[str]], ...] = (
        (ResolutionTier.MODELS_DEV_BUCKET_EXACT, normalize_candidates(model_id)),
        (
            ResolutionTier.MODELS_DEV_BUCKET_TAG_STRIPPED,
            normalize_candidates(stripped) if stripped is not None else set(),
        ),
    )
    for tier, candidates in rungs:
        for candidate in sorted(candidates, key=len, reverse=True):
            found = bucket.get(candidate)
            if found is not None:
                return found, tier
    variant = _single_tagged_variant(bucket, model_id)
    if variant is None:
        return None
    return variant, ResolutionTier.MODELS_DEV_BUCKET_TAG_STRIPPED


_REFERENCE_TIER_FOR_BUCKET_TIER: dict[ResolutionTier, ResolutionTier] = {
    ResolutionTier.MODELS_DEV_BUCKET_EXACT: ResolutionTier.OPENROUTER_EXACT,
    ResolutionTier.MODELS_DEV_BUCKET_TAG_STRIPPED: (
        ResolutionTier.OPENROUTER_TAG_STRIPPED
    ),
}

REFERENCE_BUCKET_ID = "openrouter"
"""models.dev bucket consulted as the reference rung (tiers 5-6).

The largest curated catalogue in the index -- one editorial source describing
the model itself, rather than a modal value across strangers who merely share
its name. It sits below the provider's own answer and above the vote for
exactly that reason, and it is never consulted for OpenRouter itself, where
the same rows already ARE tier 3.
"""


def _lookup_in_reference_bucket[T](
    bucket: Mapping[str, T], model_id: str
) -> tuple[T, ResolutionTier] | None:
    """Find ``model_id`` in the reference catalogue, reporting tier 5 or 6.

    Tries the id as routed first, then the same id with its routing tag
    respelled: ``minimax/minimax-m3-free`` and ``minimax/minimax-m3:free`` are
    one routing variant of one model written by two gateways, and a respelling
    is still an exact match on the model -- the vendor and the model name are
    untouched, only the punctuation between the name and the tag differs. Only
    then does the tag come off, which is a genuine loosening and reports as
    such.
    """
    found = _lookup_in_bucket_tiered(bucket, model_id)
    if found is not None:
        return found[0], _REFERENCE_TIER_FOR_BUCKET_TIER[found[1]]
    for respelled in retagged_model_ids(model_id):
        alternative = _lookup_in_bucket_tiered(bucket, respelled)
        if alternative is not None:
            return (
                alternative[0],
                _REFERENCE_TIER_FOR_BUCKET_TIER[alternative[1]],
            )
    return None


def _reference_bucket[T](
    index: Mapping[str, Mapping[str, T]], provider_id: str
) -> Mapping[str, T] | None:
    """The reference catalogue, unless ``provider_id`` IS that catalogue."""
    if provider_id == REFERENCE_BUCKET_ID or (
        PROVIDER_ID_ALIASES.get(provider_id) == REFERENCE_BUCKET_ID
    ):
        return None
    return index.get(REFERENCE_BUCKET_ID)


def _lookup_in_bucket[T](bucket: Mapping[str, T], model_id: str) -> T | None:
    """The value half of :func:`_lookup_in_bucket_tiered`."""
    found = _lookup_in_bucket_tiered(bucket, model_id)
    return None if found is None else found[0]


def _parse_reasoning_capability(
    metadata: Mapping[str, Any],
) -> ModelReasoningCapability:
    """Parse ``reasoning``/``reasoning_options`` off one models.dev model entry."""
    raw_can_reason = metadata.get("reasoning")
    can_reason = raw_can_reason if isinstance(raw_can_reason, bool) else None

    # models.dev publishes no "reasoning cannot be disabled" flag today. Read
    # a conventional key so the field lights up the day one appears, and stay
    # None (unknown) otherwise -- a wrong mandatory=True would rewrite every
    # OFF request, so only an explicit True from the source may set it.
    # Parsed before the options check: a bare ``reasoning: true`` with no
    # options list hits the early return below and must still carry it.
    raw_mandatory = metadata.get("reasoning_mandatory")
    mandatory = raw_mandatory if isinstance(raw_mandatory, bool) else None

    options = metadata.get("reasoning_options")
    if not isinstance(options, list):
        # No (or malformed) options list: control styles are unknown, not
        # known-false. ``can_reason`` may still be known from the flag above.
        return ModelReasoningCapability(can_reason=can_reason, mandatory=mandatory)

    supports_effort = False
    supports_toggle = False
    supports_budget = False
    supported_efforts: frozenset[ReasoningEffort] | None = None
    for option in options:
        if not isinstance(option, Mapping):
            continue
        option_type = option.get("type")
        if option_type == "effort":
            supports_effort = True
            values = option.get("values")
            supported_efforts = (
                frozenset(
                    EFFORT_BY_VALUE[value]
                    for value in values
                    if isinstance(value, str) and value in EFFORT_BY_VALUE
                )
                if isinstance(values, list)
                else frozenset()
            )
        elif option_type == "toggle":
            supports_toggle = True
        elif option_type == "budget_tokens":
            supports_budget = True

    return ModelReasoningCapability(
        can_reason=can_reason,
        supports_effort_control=supports_effort,
        supports_toggle_control=supports_toggle,
        supports_budget_control=supports_budget,
        supported_efforts=supported_efforts,
        mandatory=mandatory,
    )


def _build_reasoning_index(
    index: Mapping[str, Any],
) -> dict[str, dict[str, ModelReasoningCapability]]:
    """Build ``{models.dev provider id: {normalized model id: capability}}``."""
    built: dict[str, dict[str, ModelReasoningCapability]] = {}
    for provider_id, bucket in index.items():
        if not isinstance(provider_id, str) or not isinstance(bucket, Mapping):
            continue
        models = bucket.get("models")
        if not isinstance(models, Mapping):
            continue
        per_model: dict[str, ModelReasoningCapability] = {}
        for model_id, metadata in models.items():
            if not isinstance(model_id, str) or not isinstance(metadata, Mapping):
                continue
            capability = _parse_reasoning_capability(metadata)
            for candidate in normalize_candidates(model_id):
                per_model.setdefault(candidate, capability)
        if per_model:
            built[provider_id] = per_model
    return built


_reasoning_index_lock = threading.Lock()
# Path -> (source mtime, built index) so a 4MB parse happens at most once per
# on-disk cache generation, not once per lookup/request.
_reasoning_index_cache: dict[Path, tuple[float, dict[str, dict[str, Any]]]] = {}


def _cached_reasoning_index(
    path: Path | None,
) -> dict[str, dict[str, ModelReasoningCapability]]:
    cache_path = path if path is not None else models_dev_cache_path()
    try:
        mtime = cache_path.stat().st_mtime
    except OSError:
        # No on-disk cache yet (fresh install, offline): unknown, not an
        # error. A background refresh (elsewhere) will populate it later.
        return {}
    with _reasoning_index_lock:
        cached = _reasoning_index_cache.get(cache_path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    cache = read_models_dev_cache(cache_path)
    built = _build_reasoning_index(cache.index) if cache is not None else {}
    with _reasoning_index_lock:
        _reasoning_index_cache[cache_path] = (mtime, built)
    return built


def model_reasoning_capability_from_models_dev(
    provider_id: str, model_id: str, path: Path | None = None
) -> ModelReasoningCapability | None:
    """Return the models.dev-reported reasoning capability, or None if unknown.

    None means "no data at all" (provider or model absent from the index),
    which is distinct from a returned :class:`ModelReasoningCapability` whose
    fields are individually ``None``/``False``. Reads the disk-cached index
    only (never the in-memory :class:`ProviderModelCache`), so this works
    before any admin refresh has ever run, and it is a pure, cheap, memoized
    lookup: safe to call per request.
    """
    return model_reasoning_capability_tiered(provider_id, model_id, path)[0]


def model_reasoning_capability_tiered(
    provider_id: str, model_id: str, path: Path | None = None
) -> tuple[ModelReasoningCapability | None, Mapping[str, ResolutionTier]]:
    """As above, plus the ladder rung that stated each field.

    Tiers 3-4 when models.dev describes this provider (the whole record comes
    off one row, so every stated field shares that rung); the reference
    catalogue at tiers 5-6 and then the cross-provider vote at tiers 7-10, per
    field, when it does not.
    """
    reasoning_index = _cached_reasoning_index(path)
    bucket = reasoning_index.get(provider_id)
    if bucket is None:
        alias = PROVIDER_ID_ALIASES.get(provider_id)
        if alias is not None:
            bucket = reasoning_index.get(alias)
    if bucket is None:
        if _has_models_dev_bucket(_cached_raw_index(path), provider_id):
            return None, {}
        return _reference_then_vote_capability(
            reasoning_index, provider_id, model_id, path
        )
    found = _lookup_in_bucket_tiered(bucket, model_id)
    if found is None:
        return None, {}
    capability, tier = found
    return capability, {
        name: tier
        for name in (*_BOOLEAN_CAPABILITY_FIELDS, "supported_efforts")
        if getattr(capability, name) is not None
    }


def _reference_then_vote_capability(
    reasoning_index: Mapping[str, Mapping[str, ModelReasoningCapability]],
    provider_id: str,
    model_id: str,
    path: Path | None,
) -> tuple[ModelReasoningCapability | None, Mapping[str, ResolutionTier]]:
    """Resolve a bucket-less provider: reference catalogue first, then the vote.

    Per field, not per source. A field it leaves unstated falls straight
    through to the vote, which is the same "first stated wins" rule every other
    layer uses. Either half alone is a complete answer when the other misses.

    Where both state a *reasoning control* -- effort, toggle or budget -- the
    more capable record wins rather than the more authoritative one. That is
    the same direction :func:`_modal` already takes inside a rung ("do not be
    the source that shrinks a model below its declared capability"), extended
    across rungs for the three fields where "more" is a ladder. Every other
    field keeps reference-first: ``can_reason`` is not a ladder -- inverting a
    curated "this model does not reason" sends reasoning to a model that has
    none -- and ``mandatory`` and ``default_enabled`` describe a deployment,
    where ``True`` is a different fact rather than a richer one. Numeric limits
    are resolved by a different function entirely and are untouched: a limit is
    a deployment property and belongs at the tightest rung.

    The vote has already cleared its own quorum before it is a candidate here,
    so this chooses between two records that each earned their place; it never
    lowers a bar.
    """
    reference = _reference_bucket(reasoning_index, provider_id)
    found = (
        None if reference is None else _lookup_in_reference_bucket(reference, model_id)
    )
    match = cross_provider_match(provider_id, model_id, path)
    if found is None:
        if match is None:
            return None, {}
        return match.capability, match.capability_tiers

    capability, reference_tier = found
    voted = match.capability if match is not None else None
    if voted is None:
        tiers = {
            name: reference_tier
            for name in (*_BOOLEAN_CAPABILITY_FIELDS, "supported_efforts")
            if getattr(capability, name) is not None
        }
        return capability, tiers
    return _richer_of_reference_and_vote(
        capability,
        reference_tier,
        voted,
        match.capability_tiers if match is not None else {},
    )


# The reasoning-control fields, and only these: for each one "stated True"
# is strictly more capable than "stated False", so a disagreement between two
# stated records has a defensible winner. ``can_reason``, ``mandatory`` and
# ``default_enabled`` are deliberately absent -- see
# :func:`_reference_then_vote_capability`.
_RICHER_WINS: tuple[str, ...] = (
    "supports_effort_control",
    "supports_toggle_control",
    "supports_budget_control",
)


def _vocabulary_rank(value: frozenset[ReasoningEffort]) -> tuple[int, list[str]]:
    """Order two effort vocabularies the way the vote's own tie-break does."""

    return len(value), sorted(effort.value for effort in value)


def _richer_of_reference_and_vote(
    reference: ModelReasoningCapability,
    reference_tier: ResolutionTier,
    voted: ModelReasoningCapability,
    voted_tiers: Mapping[str, ResolutionTier],
) -> tuple[ModelReasoningCapability, dict[str, ResolutionTier]]:
    """Merge a reference row with a vote, field by field, and say which won.

    The reported rung follows the value: a field the vote won is labelled at
    the vote's rung (7-10), not at the reference's (5-6). Reporting the more
    authoritative rung for a value that did not come from it is the same class
    of untruth this whole ladder exists to remove.
    """

    values: dict[str, bool | None] = {}
    tiers: dict[str, ResolutionTier] = {}
    for name in _BOOLEAN_CAPABILITY_FIELDS:
        from_reference = getattr(reference, name)
        from_vote = getattr(voted, name)
        if from_reference is None and from_vote is None:
            values[name] = None
            continue
        if from_reference is None:
            values[name] = from_vote
            if name in voted_tiers:
                tiers[name] = voted_tiers[name]
            continue
        if (
            from_vote is None
            or name not in _RICHER_WINS
            or from_reference
            or not from_vote
        ):
            # Reference-first everywhere except a stated ``False`` that a
            # stated ``True`` can outrank; on an equal value the reference
            # keeps the rung, because the answer is the same and the more
            # authoritative source should be the one on screen.
            values[name] = from_reference
            tiers[name] = reference_tier
            continue
        values[name] = from_vote
        if name in voted_tiers:
            tiers[name] = voted_tiers[name]

    vocabulary = reference.supported_efforts
    if vocabulary is None:
        vocabulary = voted.supported_efforts
        if vocabulary is not None and "supported_efforts" in voted_tiers:
            tiers["supported_efforts"] = voted_tiers["supported_efforts"]
    elif voted.supported_efforts is not None and _vocabulary_rank(
        voted.supported_efforts
    ) > _vocabulary_rank(vocabulary):
        vocabulary = voted.supported_efforts
        if "supported_efforts" in voted_tiers:
            tiers["supported_efforts"] = voted_tiers["supported_efforts"]
    else:
        tiers["supported_efforts"] = reference_tier

    # Two records that were each self-consistent can merge into one that is
    # not: a reference ``False`` outranked by the vote leaves a flag from one
    # rung beside a vocabulary from another. Reconcile once, here, exactly as
    # the vote does inside itself.
    values["supports_effort_control"], vocabulary = _reconcile_effort_statement(
        values["supports_effort_control"], vocabulary, tiers
    )
    if vocabulary is None:
        tiers.pop("supported_efforts", None)
    for name in (*_BOOLEAN_CAPABILITY_FIELDS, "supported_efforts"):
        if name != "supported_efforts" and values[name] is None:
            tiers.pop(name, None)
    return ModelReasoningCapability(supported_efforts=vocabulary, **values), tiers


@dataclass(frozen=True, slots=True)
class _CrossProviderRow:
    """One models.dev row, stripped to what the approximate tier votes on."""

    capability: ModelReasoningCapability
    output_limit: int | None


@dataclass(frozen=True, slots=True)
class CrossProviderMatch:
    """An approximate answer assembled from same-named rows in other buckets.

    Only ever consulted for a provider models.dev does not describe at all, and
    only after the provider's own ``/models`` payload and an exact models.dev
    row have had their say. Every field is the *mode* across the matching rows:
    not the minimum, which would under-use a model against WORKING-NOTES 54,
    and not the maximum, which produces hard 400s.
    """

    capability: ModelReasoningCapability
    output_limit: int | None
    match_count: int
    # Share of the rows reporting an output limit that agreed on the modal
    # value. ``None`` when too few of them reported one to vote at all.
    output_agreement: float | None
    # How many of the matched rows published an output limit / an effort
    # vocabulary. These are the denominators of the two agreement ratios and
    # the quantities the minimum-sample guard is applied to -- ``match_count``
    # counts rows that merely share the name, most of which state neither.
    output_reporters: int
    efforts_reporters: int
    efforts_agreement: float | None
    # The tightest rung of the ladder that matched any rows at all (tier 7-10).
    tier: ResolutionTier
    # The rung that actually supplied each field. ``None``/absent means the
    # minimum-sample guard withheld it and the field is unknown, which is not
    # the same as a rung stating it is unsupported.
    output_tier: ResolutionTier | None
    capability_tiers: Mapping[str, ResolutionTier]


def _build_cross_provider_index(
    index: Mapping[str, Any],
) -> dict[str, tuple[_CrossProviderRow, ...]]:
    """Collect every models.dev row per normalized model id, across providers.

    Deliberately keeps all of them rather than one: the answer is a vote, so
    the losing rows are the denominator of the agreement ratio that gets
    logged.
    """
    built: dict[str, list[_CrossProviderRow]] = {}
    for bucket in index.values():
        if not isinstance(bucket, Mapping):
            continue
        models = bucket.get("models")
        if not isinstance(models, Mapping):
            continue
        for model_id, metadata in models.items():
            if not isinstance(model_id, str) or not isinstance(metadata, Mapping):
                continue
            limit = metadata.get("limit")
            row = _CrossProviderRow(
                capability=_parse_reasoning_capability(metadata),
                output_limit=(
                    _positive_int_or_none(limit.get("output"))
                    if isinstance(limit, Mapping)
                    else None
                ),
            )
            for candidate in normalize_candidates(model_id):
                built.setdefault(candidate, []).append(row)
    return {candidate: tuple(rows) for candidate, rows in built.items()}


# How many matching rows must actually state a field before the approximate
# tier is allowed to answer with it.
#
# Numbers and vocabularies: three. One reporter is not a vote -- it is a
# transcription, and it always "agrees" with itself, which is how a single
# models.dev row credited one gateway's free model with a 1,048,576-token
# output limit at "100% agreement" when the modal value across the ~51 hosts
# that actually serve it is 512,000 and NVIDIA serves it at 16,384. Two cannot
# break a tie from evidence. Three is the smallest sample where the modal
# value can be a real majority rather than an arbitrary pick, so below it the
# honest answer is unknown and the caller falls through to its own default.
MIN_APPROXIMATE_NUMERIC_REPORTERS = 3
MIN_APPROXIMATE_VOCABULARY_REPORTERS = 3
# Booleans: three, same as everything else. They were one, on the reasoning
# that same-named rows are near-unanimous about whether a model reasons at all
# (28/28, 32/32, 50/51 on the live 2026-08 index). Near-unanimous is not the
# point: one row is a transcription, not a vote, and it always "agrees" with
# itself. It also let a single foreign row *veto* a capability -- one row
# saying ``supports_effort_control: false`` decided the field for
# ``minimax/minimax-m3-free`` at tier 5 while twelve rows at tier 8 published
# an effort vocabulary for the same name, so the record contradicted itself
# and gating discarded the caller's effort on the strength of the veto.
# Three is the smallest sample where the modal value can be a real majority.
MIN_APPROXIMATE_BOOLEAN_REPORTERS = 3


@dataclass(frozen=True, slots=True)
class _Vote[T]:
    """A modal value plus the evidence behind it."""

    value: T
    agreement: float
    reporters: int


def _modal[T: Hashable](
    reported: Sequence[T], tie_break: Callable[[T], Any], minimum: int
) -> _Vote[T] | None:
    """Return the most common value, the agreement, and the sample size.

    ``reported`` is only the rows that stated the field; ``None`` comes back
    when fewer than ``minimum`` of them did, because an under-sampled field is
    unknown, never a guess.

    Ties are broken by ``tie_break`` descending, which every caller points at
    the more capable option (True over False, the larger limit, the larger
    effort vocabulary). That direction is the one WORKING-NOTES 54 asks for:
    when the evidence is split, do not be the source that shrinks a model
    below its declared capability.
    """
    if len(reported) < minimum:
        return None
    counts = Counter(reported)
    winner = max(counts, key=lambda value: (counts[value], tie_break(value)))
    return _Vote(winner, counts[winner] / len(reported), len(reported))


_BOOLEAN_CAPABILITY_FIELDS: tuple[str, ...] = (
    "can_reason",
    "supports_effort_control",
    "supports_toggle_control",
    "supports_budget_control",
    "mandatory",
    "default_enabled",
)


def _vote_across_rungs[T: Hashable](
    rungs: tuple[tuple[ResolutionTier, tuple[_CrossProviderRow, ...]], ...],
    reader: Callable[[_CrossProviderRow], T | None],
    tie_break: Callable[[T], Any],
    minimum: int,
) -> tuple[_Vote[T], ResolutionTier] | None:
    """Walk the rungs for ONE field and answer from the first that can.

    The ladder runs per field, not per source. A rung that matched rows but
    whose rows are all silent about this field -- or too few of them to clear
    the guard -- has not answered it, so the next looser rung gets its turn for
    that field alone while the fields it did answer stay pinned to it.
    """
    for tier, rows in rungs:
        reported = [value for row in rows if (value := reader(row)) is not None]
        vote = _modal(reported, tie_break, minimum)
        if vote is not None:
            return vote, tier
    return None


def _reconcile_effort_statement(
    supports_effort_control: bool | None,
    vocabulary: frozenset[ReasoningEffort] | None,
    tiers: dict[str, ResolutionTier],
) -> tuple[bool | None, frozenset[ReasoningEffort] | None]:
    """Make one record's effort flag and effort vocabulary agree.

    Two fields resolved independently can disagree about the same fact, and a
    record that says "this model has no effort knob" while also listing the
    effort words it accepts is not a fact about anything. The vocabulary and
    the flag are one statement, so they are reconciled wherever both are held,
    together with the rung each came from -- ``tiers`` is updated in place so
    the Models page never reports a rung for a field that was withdrawn.

    Called from the cross-provider vote, which has always done this inline, and
    from the reference-then-vote rung, which merges two already-reconciled
    records into one that could be contradictory again.
    """

    if supports_effort_control is False:
        # The flag is the stronger claim: it says the knob is absent, which no
        # list of words the knob would accept can survive.
        tiers.pop("supported_efforts", None)
        return supports_effort_control, None
    if supports_effort_control is None and vocabulary:
        # And the converse: a stated vocabulary IS the statement that an effort
        # knob exists, so an unstated flag takes it from the same rung rather
        # than staying unknown beside it.
        vocabulary_tier = tiers.get("supported_efforts")
        if vocabulary_tier is not None:
            tiers["supports_effort_control"] = vocabulary_tier
        return True, vocabulary
    return supports_effort_control, vocabulary


def _cross_provider_capability(
    rungs: tuple[tuple[ResolutionTier, tuple[_CrossProviderRow, ...]], ...],
) -> tuple[
    ModelReasoningCapability,
    dict[str, ResolutionTier],
    tuple[_Vote[frozenset[ReasoningEffort]], ResolutionTier] | None,
]:
    """Vote each capability field independently, and say which rung won it.

    Guarded per field: a boolean needs
    :data:`MIN_APPROXIMATE_BOOLEAN_REPORTERS` reporter, an effort vocabulary
    needs :data:`MIN_APPROXIMATE_VOCABULARY_REPORTERS`.
    """
    tiers: dict[str, ResolutionTier] = {}
    values: dict[str, bool | None] = {}
    for name in _BOOLEAN_CAPABILITY_FIELDS:
        won = _vote_across_rungs(
            rungs,
            lambda row, name=name: getattr(row.capability, name),
            lambda value: value,
            MIN_APPROXIMATE_BOOLEAN_REPORTERS,
        )
        values[name] = won[0].value if won is not None else None
        if won is not None:
            tiers[name] = won[1]

    efforts = _vote_across_rungs(
        rungs,
        lambda row: row.capability.supported_efforts,
        lambda value: (len(value), sorted(effort.value for effort in value)),
        MIN_APPROXIMATE_VOCABULARY_REPORTERS,
    )
    if efforts is not None:
        tiers["supported_efforts"] = efforts[1]

    vocabulary = efforts[0].value if efforts is not None else None
    values["supports_effort_control"], vocabulary = _reconcile_effort_statement(
        values["supports_effort_control"], vocabulary, tiers
    )

    capability = ModelReasoningCapability(
        supported_efforts=vocabulary,
        **values,
    )
    return capability, tiers, efforts


_cross_provider_index_lock = threading.Lock()
_cross_provider_index_cache: dict[
    Path, tuple[float, dict[str, tuple[_CrossProviderRow, ...]]]
] = {}


def _cached_cross_provider_index(
    path: Path | None,
) -> dict[str, tuple[_CrossProviderRow, ...]]:
    cache_path = path if path is not None else models_dev_cache_path()
    try:
        mtime = cache_path.stat().st_mtime
    except OSError:
        return {}
    with _cross_provider_index_lock:
        cached = _cross_provider_index_cache.get(cache_path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    cache = read_models_dev_cache(cache_path)
    built = _build_cross_provider_index(cache.index) if cache is not None else {}
    with _cross_provider_index_lock:
        _cross_provider_index_cache[cache_path] = (mtime, built)
    return built


_cross_provider_match_lock = threading.Lock()
_cross_provider_match_cache: dict[
    Path, tuple[float, dict[tuple[str, str], CrossProviderMatch | None]]
] = {}


def _walk_cross_provider_ladder(
    index: Mapping[str, tuple[_CrossProviderRow, ...]],
    provider_id: str,
    model_id: str,
) -> CrossProviderMatch | None:
    """Assemble tiers 7-10 and resolve every field down them, tightest first.

    The rungs loosen one thing each, tag before vendor prefix, so the widest
    set of same-named rows is only ever consulted for a field no narrower
    reading of the id could answer. That ordering is why
    ``minimax/minimax-m3-free`` takes anything the vendor-qualified name can
    state before falling back to every host of the bare ``minimax-m3``, and
    why a bare name never overrides a vendor-qualified hit.
    """
    rungs = tuple(
        (tier, rows)
        for tier, candidate in candidate_ladder(model_id)
        if (rows := index.get(candidate))
    )
    if not rungs:
        return None
    capability, capability_tiers, efforts = _cross_provider_capability(rungs)
    output = _vote_across_rungs(
        rungs,
        lambda row: row.output_limit,
        lambda value: value,
        MIN_APPROXIMATE_NUMERIC_REPORTERS,
    )
    match = CrossProviderMatch(
        capability=capability,
        output_limit=output[0].value if output is not None else None,
        match_count=_answering_row_count(rungs, output),
        output_agreement=output[0].agreement if output is not None else None,
        output_reporters=(
            output[0].reporters
            if output is not None
            else _best_stated_count(rungs, lambda row: row.output_limit)
        ),
        output_tier=output[1] if output is not None else None,
        efforts_reporters=(
            efforts[0].reporters
            if efforts is not None
            else _best_stated_count(rungs, lambda row: row.capability.supported_efforts)
        ),
        efforts_agreement=efforts[0].agreement if efforts is not None else None,
        capability_tiers=capability_tiers,
        tier=rungs[0][0],
    )
    _log_cross_provider_match(provider_id, model_id, match)
    return match


def _answering_row_count(
    rungs: tuple[tuple[ResolutionTier, tuple[_CrossProviderRow, ...]], ...],
    output: tuple[_Vote[int], ResolutionTier] | None,
) -> int:
    """How many same-named rows stand behind the headline number.

    The rung that answered the output limit, not the tightest rung that
    matched anything: on the live index ``minimax/minimax-m3-free`` matches one
    row at tier 7 whose limit the guard rejects, and the 512,000 actually
    reported comes off the twelve rows at tier 8. Saying "one match" beside
    that number would misdescribe it in the same direction the old code did.
    """
    if output is not None:
        return next(len(rows) for tier, rows in rungs if tier is output[1])
    return len(rungs[0][1])


def _best_stated_count(
    rungs: tuple[tuple[ResolutionTier, tuple[_CrossProviderRow, ...]], ...],
    reader: Callable[[_CrossProviderRow], object | None],
) -> int:
    """The most rows any single rung had stating a field it could not answer.

    Kept only so a withheld field can say how far short of the guard it fell.
    Zero means nothing anywhere published it, which is a different sentence.
    """
    return max(
        (sum(1 for row in rows if reader(row) is not None) for _tier, rows in rungs),
        default=0,
    )


def _log_cross_provider_match(
    provider_id: str, model_id: str, match: CrossProviderMatch
) -> None:
    """Say which rung answered each field, on what sample, and how it agreed.

    Never reports an agreement ratio for a field the minimum-sample guard
    rejected: "100% agreement" off one row is the claim this ladder exists to
    stop making.
    """
    logger.info(
        "models.dev has no bucket for {}; APPROXIMATE cross-provider match for "
        "{}: matched at tier {} ({}) across {} rows; output limit {} from {} "
        "({}); efforts {} from {} ({})",
        provider_id,
        model_id,
        int(match.tier),
        match.tier.name.lower(),
        match.match_count,
        match.output_limit,
        _tier_note(match.output_tier),
        _sample_note(
            match.output_reporters,
            match.output_agreement,
            MIN_APPROXIMATE_NUMERIC_REPORTERS,
        ),
        (
            "unknown"
            if match.capability.supported_efforts is None
            else sorted(effort.value for effort in match.capability.supported_efforts)
        ),
        _tier_note(match.capability_tiers.get("supported_efforts")),
        _sample_note(
            match.efforts_reporters,
            match.efforts_agreement,
            MIN_APPROXIMATE_VOCABULARY_REPORTERS,
        ),
    )


def _tier_note(tier: ResolutionTier | None) -> str:
    """Name the rung that supplied one field, or say none did."""
    return "no tier" if tier is None else f"tier {int(tier)} ({tier.name.lower()})"


def _sample_note(reporters: int, agreement: float | None, minimum: int) -> str:
    """Human-readable evidence for one voted field."""
    if agreement is None:
        if reporters == 0:
            return "unreported"
        return f"withheld: {reporters} of the required {minimum} rows reported one"
    return f"{agreement:.0%} agreement across {reporters} reporting rows"


def cross_provider_match(
    provider_id: str, model_id: str, path: Path | None = None
) -> CrossProviderMatch | None:
    """Resolve a model by name across every models.dev bucket.

    Memoized per on-disk cache generation, which is also what keeps the
    "this answer is approximate" log line to one per model per refresh instead
    of one per request.
    """
    cache_path = path if path is not None else models_dev_cache_path()
    try:
        mtime = cache_path.stat().st_mtime
    except OSError:
        return None
    key = (provider_id, model_id)
    with _cross_provider_match_lock:
        cached = _cross_provider_match_cache.get(cache_path)
        if cached is not None and cached[0] == mtime and key in cached[1]:
            return cached[1][key]

    match = _walk_cross_provider_ladder(
        _cached_cross_provider_index(cache_path), provider_id, model_id
    )

    with _cross_provider_match_lock:
        cached = _cross_provider_match_cache.get(cache_path)
        if cached is None or cached[0] != mtime:
            cached = (mtime, {})
            _cross_provider_match_cache[cache_path] = cached
        cached[1][key] = match
    return match


_raw_index_ids_lock = threading.Lock()
_raw_index_ids_cache: dict[Path, tuple[float, frozenset[str]]] = {}


def _cached_raw_index(path: Path | None) -> frozenset[str]:
    """The models.dev provider ids present on disk, memoized per generation."""
    cache_path = path if path is not None else models_dev_cache_path()
    try:
        mtime = cache_path.stat().st_mtime
    except OSError:
        return frozenset()
    with _raw_index_ids_lock:
        cached = _raw_index_ids_cache.get(cache_path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    cache = read_models_dev_cache(cache_path)
    built = frozenset(cache.index) if cache is not None else frozenset()
    with _raw_index_ids_lock:
        _raw_index_ids_cache[cache_path] = (mtime, built)
    return built


def _has_models_dev_bucket(index: frozenset[str], provider_id: str) -> bool:
    """Whether models.dev describes this provider under its id or its alias."""
    if provider_id in index:
        return True
    alias = PROVIDER_ID_ALIASES.get(provider_id)
    return alias is not None and alias in index


def models_dev_describes_provider(provider_id: str, path: Path | None = None) -> bool:
    """Whether models.dev has a bucket of its own for this provider.

    The public form of the check every lookup here already makes internally.
    It exists because "models.dev answered" and "the approximate cross-provider
    tier answered" are the same return value from
    :func:`model_output_limit_from_models_dev`, and a reader that has to tell
    an authoritative row from a one-sample vote across foreign buckets cannot
    do it from the value alone.
    """

    return _has_models_dev_bucket(_cached_raw_index(path), provider_id)


def _build_output_limit_index(
    index: Mapping[str, Any],
) -> dict[str, dict[str, int]]:
    """Build ``{models.dev provider id: {normalized model id: limit.output}}``.

    models.dev publishes ``limit.output`` for the overwhelming majority of its
    rows; a model without one is simply absent here, which callers must read
    as "unknown limit", never as "no limit".
    """
    built: dict[str, dict[str, int]] = {}
    for provider_id, bucket in index.items():
        if not isinstance(provider_id, str) or not isinstance(bucket, Mapping):
            continue
        models = bucket.get("models")
        if not isinstance(models, Mapping):
            continue
        per_model: dict[str, int] = {}
        for model_id, metadata in models.items():
            if not isinstance(model_id, str) or not isinstance(metadata, Mapping):
                continue
            limit = metadata.get("limit")
            if not isinstance(limit, Mapping):
                continue
            output = _positive_int_or_none(limit.get("output"))
            if output is None:
                continue
            for candidate in normalize_candidates(model_id):
                per_model.setdefault(candidate, output)
        if per_model:
            built[provider_id] = per_model
    return built


_output_limit_index_lock = threading.Lock()
_output_limit_index_cache: dict[Path, tuple[float, dict[str, dict[str, int]]]] = {}


def _cached_output_limit_index(path: Path | None) -> dict[str, dict[str, int]]:
    cache_path = path if path is not None else models_dev_cache_path()
    try:
        mtime = cache_path.stat().st_mtime
    except OSError:
        return {}
    with _output_limit_index_lock:
        cached = _output_limit_index_cache.get(cache_path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    cache = read_models_dev_cache(cache_path)
    built = _build_output_limit_index(cache.index) if cache is not None else {}
    with _output_limit_index_lock:
        _output_limit_index_cache[cache_path] = (mtime, built)
    return built


def model_output_limit_from_models_dev(
    provider_id: str, model_id: str, path: Path | None = None
) -> int | None:
    """Return the model's published output-token limit, or None when unknown.

    Same disk-cache-only, memoized lookup contract as
    :func:`model_reasoning_capability_from_models_dev`; safe per request.
    """
    return model_output_limit_tiered(provider_id, model_id, path)[0]


def model_output_limit_tiered(
    provider_id: str, model_id: str, path: Path | None = None
) -> tuple[int | None, ResolutionTier | None]:
    """As above, plus the ladder rung the number came from (tier 3-10).

    ``(None, None)`` covers both "no row anywhere" and "the approximate tier
    matched but too few of its rows published a limit to vote": either way the
    limit is unknown and the caller must fall through to its own default.
    """
    limit_index = _cached_output_limit_index(path)
    bucket = limit_index.get(provider_id)
    if bucket is None:
        alias = PROVIDER_ID_ALIASES.get(provider_id)
        if alias is not None:
            bucket = limit_index.get(alias)
    if bucket is None:
        if _has_models_dev_bucket(_cached_raw_index(path), provider_id):
            return None, None
        reference = _reference_bucket(limit_index, provider_id)
        found = (
            None
            if reference is None
            else _lookup_in_reference_bucket(reference, model_id)
        )
        if found is not None and found[0] is not None:
            return found
        match = cross_provider_match(provider_id, model_id, path)
        if match is None:
            return None, None
        return match.output_limit, match.output_tier
    found = _lookup_in_bucket_tiered(bucket, model_id)
    return (None, None) if found is None else found


# --------------------------------------------------------------- field ladder
#
# Siblings of :func:`model_output_limit_tiered`, one per remaining models.dev
# field the catalogue publishes. They walk the SAME ten rungs in the SAME
# order with the SAME quorum guards; nothing above is modified. The output
# limit keeps its own hand-written index because it is also the field the
# cross-provider vote was built around, and rewriting it in terms of these
# generics would change the code that currently answers 124 of 128 models.
#
# Why the whole ladder rather than the flat ``enrich_model_infos`` match: that
# match is provider-blind and un-tiered, so a model whose own provider has no
# models.dev bucket, or whose id carries a routing tag, resolves to nothing at
# all. Those are exactly the models a gateway resells, and a catalogue that
# says "unknown" for them is what a CLI turns into its own invented default.
#
# The enrichment path still answers first everywhere: these lookups are only
# consulted where the provider's own record left the field ``None``.

type _MetadataReader[T] = Callable[[Mapping[str, Any]], T | None]


@dataclass(frozen=True, slots=True)
class _LadderField[T]:
    """One models.dev field, and how the ladder is allowed to answer it.

    ``tie_break`` is applied descending by :func:`_modal` exactly as the output
    limit's is, so a split vote resolves toward the more capable reading rather
    than the one that would shrink a model below its declared capability
    (WORKING-NOTES 54). Prices are the one field where "more capable" has no
    meaning, so their tie-break points at the *lower* number: between two
    equally-attested rates, MCC must not be the source that overstates a cost.
    """

    name: str
    reader: _MetadataReader[T]
    tie_break: Callable[[T], Any]
    minimum: int


def _models_dev_context_length(metadata: Mapping[str, Any]) -> int | None:
    limit = metadata.get("limit")
    if not isinstance(limit, Mapping):
        return None
    return _positive_int_or_none(limit.get("context"))


def _models_dev_tool_call(metadata: Mapping[str, Any]) -> bool | None:
    """Read models.dev's own per-model ``tool_call`` boolean, or None.

    A row that omits the key has not said "no"; only a published ``false``
    can produce ``False``, exactly as :func:`derive_supports_tool_calls`
    treats an absent ``supported_parameters`` list.
    """

    value = metadata.get("tool_call")
    return value if isinstance(value, bool) else None


def _models_dev_price(key: str) -> _MetadataReader[float]:
    def read(metadata: Mapping[str, Any]) -> float | None:
        cost = metadata.get("cost")
        if not isinstance(cost, Mapping):
            return None
        return _float_or_none(cost.get(key))

    return read


CONTEXT_LENGTH_FIELD: _LadderField[int] = _LadderField(
    name="context_length",
    reader=_models_dev_context_length,
    tie_break=lambda value: value,
    minimum=MIN_APPROXIMATE_NUMERIC_REPORTERS,
)
VISION_FIELD: _LadderField[bool] = _LadderField(
    name="supports_vision",
    reader=_accepts_image_input,
    tie_break=lambda value: value,
    minimum=MIN_APPROXIMATE_BOOLEAN_REPORTERS,
)
TOOL_CALL_FIELD: _LadderField[bool] = _LadderField(
    name="tool_call",
    reader=_models_dev_tool_call,
    tie_break=lambda value: value,
    minimum=MIN_APPROXIMATE_BOOLEAN_REPORTERS,
)

#: The four price rates, in the catalogue's own vocabulary. models.dev
#: publishes all four under ``cost``; ``ProviderModelInfo`` carries only the
#: first two, so the cache rates have no provider rung and resolve from
#: tier 3 down or not at all.
PRICE_FIELDS: tuple[_LadderField[float], ...] = tuple(
    _LadderField(
        name=name,
        reader=_models_dev_price(key),
        tie_break=lambda value: -value,
        minimum=MIN_APPROXIMATE_NUMERIC_REPORTERS,
    )
    for name, key in (
        ("input_price", "input"),
        ("output_price", "output"),
        ("cache_read_price", "cache_read"),
        ("cache_write_price", "cache_write"),
    )
)


def _build_field_index[T](
    index: Mapping[str, Any], reader: _MetadataReader[T]
) -> dict[str, dict[str, T]]:
    """``{models.dev provider id: {normalized model id: value}}`` for one field.

    Shaped exactly like :func:`_build_output_limit_index`: a model that does
    not state the field is simply absent, which every caller reads as
    "unknown", never as a stated absence.
    """

    built: dict[str, dict[str, T]] = {}
    for provider_id, bucket in index.items():
        if not isinstance(provider_id, str) or not isinstance(bucket, Mapping):
            continue
        models = bucket.get("models")
        if not isinstance(models, Mapping):
            continue
        per_model: dict[str, T] = {}
        for model_id, metadata in models.items():
            if not isinstance(model_id, str) or not isinstance(metadata, Mapping):
                continue
            value = reader(metadata)
            if value is None:
                continue
            for candidate in normalize_candidates(model_id):
                per_model.setdefault(candidate, value)
        if per_model:
            built[provider_id] = per_model
    return built


def _build_field_cross_index[T](
    index: Mapping[str, Any], reader: _MetadataReader[T]
) -> dict[str, tuple[T, ...]]:
    """Every stated value per normalized model id, across all providers.

    The denominator of the cross-provider vote for one field, built the same
    way :func:`_build_cross_provider_index` builds the reasoning one. Only
    stated values are kept, because the guard counts *reporters* and a silent
    row is not one.
    """

    built: dict[str, list[T]] = {}
    for bucket in index.values():
        if not isinstance(bucket, Mapping):
            continue
        models = bucket.get("models")
        if not isinstance(models, Mapping):
            continue
        for model_id, metadata in models.items():
            if not isinstance(model_id, str) or not isinstance(metadata, Mapping):
                continue
            value = reader(metadata)
            if value is None:
                continue
            for candidate in normalize_candidates(model_id):
                built.setdefault(candidate, []).append(value)
    return {candidate: tuple(values) for candidate, values in built.items()}


_field_index_lock = threading.Lock()
_field_index_cache: dict[tuple[Path, str], tuple[float, dict[str, dict[str, Any]]]] = {}
_field_cross_index_cache: dict[
    tuple[Path, str], tuple[float, dict[str, tuple[Any, ...]]]
] = {}


def _cached_field_index[T](
    field: _LadderField[T], path: Path | None
) -> dict[str, dict[str, T]]:
    cache_path = path if path is not None else models_dev_cache_path()
    key = (cache_path, field.name)
    try:
        mtime = cache_path.stat().st_mtime
    except OSError:
        return {}
    with _field_index_lock:
        cached = _field_index_cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    cache = read_models_dev_cache(cache_path)
    built = _build_field_index(cache.index, field.reader) if cache is not None else {}
    with _field_index_lock:
        _field_index_cache[key] = (mtime, built)
    return built


def _cached_field_cross_index[T](
    field: _LadderField[T], path: Path | None
) -> dict[str, tuple[T, ...]]:
    cache_path = path if path is not None else models_dev_cache_path()
    key = (cache_path, field.name)
    try:
        mtime = cache_path.stat().st_mtime
    except OSError:
        return {}
    with _field_index_lock:
        cached = _field_cross_index_cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
    cache = read_models_dev_cache(cache_path)
    built = (
        _build_field_cross_index(cache.index, field.reader) if cache is not None else {}
    )
    with _field_index_lock:
        _field_cross_index_cache[key] = (mtime, built)
    return built


def _field_cross_vote[T: Hashable](
    field: _LadderField[T], model_id: str, path: Path | None
) -> tuple[T, ResolutionTier] | None:
    """Tiers 7-10 for one field: the modal value, guarded by the same quorum.

    Walks :func:`candidate_ladder` tightest rung first and answers from the
    first rung whose reporters clear ``field.minimum``, which is the rule
    :func:`_vote_across_rungs` applies to the reasoning fields and the output
    limit. Below the quorum the answer stays unknown; it never becomes a guess.
    """

    index = _cached_field_cross_index(field, path)
    for tier, candidate in candidate_ladder(model_id):
        reported = index.get(candidate)
        if not reported:
            continue
        vote = _modal(reported, field.tie_break, field.minimum)
        if vote is not None:
            return vote.value, tier
    return None


def _model_field_tiered[T: Hashable](
    field: _LadderField[T], provider_id: str, model_id: str, path: Path | None
) -> tuple[T | None, ResolutionTier | None]:
    """One field, resolved down the same rungs :func:`model_output_limit_tiered` walks.

    Rung for rung: this provider's own models.dev bucket exact (tier 3) and
    tag-stripped (tier 4); failing a bucket of its own, the OpenRouter
    reference catalogue (tiers 5-6); failing that, the approximate
    cross-provider vote (tiers 7-10). A provider that HAS a bucket is never
    allowed to read outside it, so a wrong same-name row cannot override its
    own catalogue's answer.
    """

    field_index = _cached_field_index(field, path)
    bucket = field_index.get(provider_id)
    if bucket is None:
        alias = PROVIDER_ID_ALIASES.get(provider_id)
        if alias is not None:
            bucket = field_index.get(alias)
    if bucket is None:
        if _has_models_dev_bucket(_cached_raw_index(path), provider_id):
            return None, None
        reference = _reference_bucket(field_index, provider_id)
        found = (
            None
            if reference is None
            else _lookup_in_reference_bucket(reference, model_id)
        )
        if found is not None:
            return found
        vote = _field_cross_vote(field, model_id, path)
        return (None, None) if vote is None else vote
    found = _lookup_in_bucket_tiered(bucket, model_id)
    return (None, None) if found is None else found


def model_context_length_tiered(
    provider_id: str, model_id: str, path: Path | None = None
) -> tuple[int | None, ResolutionTier | None]:
    """models.dev's context window for one model, plus the rung it came from.

    A published ``limit.context`` of ``0`` reads as absent, exactly as it does
    everywhere else here: models.dev's schema permits it and means "not
    applicable or unknown" by it, never a window of zero tokens.
    """

    return _model_field_tiered(CONTEXT_LENGTH_FIELD, provider_id, model_id, path)


def model_vision_tiered(
    provider_id: str, model_id: str, path: Path | None = None
) -> tuple[bool | None, ResolutionTier | None]:
    """models.dev's image-input support for one model, plus its rung."""

    return _model_field_tiered(VISION_FIELD, provider_id, model_id, path)


def model_tool_call_tiered(
    provider_id: str, model_id: str, path: Path | None = None
) -> tuple[bool | None, ResolutionTier | None]:
    """models.dev's ``tool_call`` boolean for one model, plus its rung."""

    return _model_field_tiered(TOOL_CALL_FIELD, provider_id, model_id, path)


def model_prices_tiered(
    provider_id: str, model_id: str, path: Path | None = None
) -> dict[str, tuple[float | None, ResolutionTier | None]]:
    """The four published rates for one model, each with its own rung.

    Per field, not per source, for the same reason the reasoning capability is
    resolved per field: a bucket that publishes ``input``/``output`` but no
    cache rates has answered two of the four, and forcing the other two down
    to the same rung would either invent them or discard the two it did state.
    """

    return {
        field.name: _model_field_tiered(field, provider_id, model_id, path)
        for field in PRICE_FIELDS
    }


def merge_reasoning_capabilities(
    primary: ModelReasoningCapability | None,
    secondary: ModelReasoningCapability | None,
) -> ModelReasoningCapability | None:
    """Merge two capability records field by field; ``primary`` wins each field.

    Per field, not per source. Choosing a source wholesale is what the previous
    implementation did and it silently dropped every field the winning source
    had no opinion about -- ``mandatory`` among them, which is why the
    mandatory-model handling shipped dead.

    A ``False`` in ``primary`` is a real answer and is kept; only ``None``
    (nothing stated) defers to ``secondary``.
    """
    if primary is None:
        return secondary
    if secondary is None:
        return primary
    return ModelReasoningCapability(
        can_reason=_first_stated(primary.can_reason, secondary.can_reason),
        supports_effort_control=_first_stated(
            primary.supports_effort_control, secondary.supports_effort_control
        ),
        supports_toggle_control=_first_stated(
            primary.supports_toggle_control, secondary.supports_toggle_control
        ),
        supports_budget_control=_first_stated(
            primary.supports_budget_control, secondary.supports_budget_control
        ),
        supported_efforts=_first_stated(
            primary.supported_efforts, secondary.supported_efforts
        ),
        mandatory=_first_stated(primary.mandatory, secondary.mandatory),
        default_enabled=_first_stated(
            primary.default_enabled, secondary.default_enabled
        ),
    )


def _first_stated[T](primary: T | None, secondary: T | None) -> T | None:
    return primary if primary is not None else secondary


def resolve_model_reasoning_capability(
    provider_id: str,
    model_id: str,
    provider_capability: ModelReasoningCapability | None,
    path: Path | None = None,
) -> ModelReasoningCapability | None:
    """Layer the provider's own reported capability over the models.dev one.

    Gateway first, field by field: whatever the provider's ``/models`` payload
    stated about this model wins, and models.dev fills only the fields it left
    unstated. models.dev's own answer may itself come from the approximate
    cross-provider tier, which therefore can never outrank the gateway -- the
    ordering that matters, since for ``tencent/hy3:free`` the cross-provider
    modal output limit is 64,000 while the gateway reports 128,000.

    There used to be a third and lowest layer -- a hardcoded table of the
    effort vocabularies two endpoints (Mistral, Cohere) document for every
    model behind them. That was a statement about the *host*, not the model,
    and it now lives where host statements belong: those providers' own
    ``reasoning_dialect``, which gating intersects with whatever the model
    turns out to support. One fact, one owner.

    Returns ``None`` only when no layer has any data at all.
    """
    return merge_reasoning_capabilities(
        provider_capability,
        model_reasoning_capability_from_models_dev(provider_id, model_id, path),
    )
