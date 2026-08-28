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
from collections.abc import Callable, Hashable, Iterable, Mapping
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


def _normalize_candidates(model_id: str) -> set[str]:
    """Return normalized match keys for one model id."""
    lowered = model_id.strip().lower()
    if not lowered:
        return set()
    candidates = {lowered}
    _prefix, separator, remainder = lowered.partition("/")
    if separator and remainder:
        candidates.add(remainder)
    last_segment = lowered.rsplit("/", 1)[-1]
    if last_segment:
        candidates.add(last_segment)
    return candidates


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
            for candidate in _normalize_candidates(model_id):
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
        for candidate in _normalize_candidates(model_id):
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
                _normalize_candidates(model_id), key=len, reverse=True
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

# Pricing/routing/capability tags some providers accept in a model ref but
# models.dev does not list (e.g. "deepseek/deepseek-r1:free"). Every tag here
# alters price, routing, or a non-reasoning capability and leaves thinking
# behaviour untouched, so stripping it is safe by the same logic as ":free".
# ":online" and ":extended" are OpenRouter request suffixes a user may type;
# ":discounted" appears in the upstream index itself.
#
# Strictly allow-listed, because the excluded tags ARE the reasoning
# difference: ":thinking" (69 occurrences upstream), numeric budget tags
# (":32000", ":32768", ":8192", ":1024", ":64000") and effort tags (":low",
# ":medium", ":high", ":max") are NEVER stripped -- models.dev ships
# "nano-gpt/claude-opus-4-thinking:32000" and ":32768", and
# "gemini-2.5-flash-preview:thinking" -- so stripping those would be wrong in
# exactly the dimension configured here.
_STRIPPABLE_MODEL_ID_TAGS: frozenset[str] = frozenset(
    {"free", "nitro", "floor", "online", "extended", "discounted"}
)


def _tag_stripped_candidates(model_id: str) -> set[str]:
    """Return match keys for ``model_id`` minus a trailing pricing/routing tag.

    Empty when the id carries no tag, or carries a tag outside the allow-list.
    """
    head, separator, tag = model_id.strip().lower().rpartition(":")
    if not separator or "/" in tag:
        return set()
    if tag not in _STRIPPABLE_MODEL_ID_TAGS or not head:
        return set()
    return _normalize_candidates(head)


def _single_tagged_variant[T](bucket: Mapping[str, T], model_id: str) -> T | None:
    """Find the one allow-list-tagged variant of an untagged ``model_id``.

    The reverse of :func:`_tag_stripped_candidates`: a user who configures
    ``foo`` should still match an index that only lists ``foo:free``. Used ONLY
    when exactly one such variant exists in this bucket -- with both
    ``foo:free`` and ``foo:nitro`` present, picking either would assert a
    variant the user did not write, so the answer stays unknown.

    Costs at most one dict lookup per allow-listed tag per candidate, so no
    side index is needed: this stays cheap enough to run per request.
    """
    for candidate in sorted(_normalize_candidates(model_id), key=len, reverse=True):
        if ":" in candidate:
            # A query that already carries a tag is the forward case.
            continue
        matches = [
            bucket[key]
            for tag in sorted(_STRIPPABLE_MODEL_ID_TAGS)
            if (key := f"{candidate}:{tag}") in bucket
        ]
        if len(matches) > 1:
            return None
        if matches:
            return matches[0]
    return None


def _lookup_in_bucket[T](bucket: Mapping[str, T], model_id: str) -> T | None:
    """Find ``model_id`` in one provider bucket.

    Exact first, then tag-stripped, then the single allow-list-tagged variant
    of an untagged query. Never looks outside ``bucket``; an exact hit always
    beats either fallback, so "x" and "x:free" coexisting keep their own
    distinct entries.
    """
    for candidates in (
        _normalize_candidates(model_id),
        _tag_stripped_candidates(model_id),
    ):
        for candidate in sorted(candidates, key=len, reverse=True):
            found = bucket.get(candidate)
            if found is not None:
                return found
    return _single_tagged_variant(bucket, model_id)


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
            for candidate in _normalize_candidates(model_id):
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
    reasoning_index = _cached_reasoning_index(path)
    bucket = reasoning_index.get(provider_id)
    if bucket is None:
        alias = PROVIDER_ID_ALIASES.get(provider_id)
        if alias is not None:
            bucket = reasoning_index.get(alias)
    if bucket is None:
        if _has_models_dev_bucket(_cached_raw_index(path), provider_id):
            return None
        match = cross_provider_match(provider_id, model_id, path)
        return match.capability if match is not None else None
    return _lookup_in_bucket(bucket, model_id)


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
    # value. ``None`` when none of them reported one.
    output_agreement: float | None


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
            for candidate in _normalize_candidates(model_id):
                built.setdefault(candidate, []).append(row)
    return {candidate: tuple(rows) for candidate, rows in built.items()}


def _modal[T: Hashable](
    values: Iterable[T | None], tie_break: Callable[[T], Any]
) -> tuple[T, float] | None:
    """Return the most common non-None value and how many reporters agreed.

    Ties are broken by ``tie_break`` descending, which every caller points at
    the more capable option (True over False, the larger limit, the larger
    effort vocabulary). That direction is the one WORKING-NOTES 54 asks for:
    when the evidence is split, do not be the source that shrinks a model
    below its declared capability.
    """
    reported = [value for value in values if value is not None]
    if not reported:
        return None
    counts = Counter(reported)
    winner = max(counts, key=lambda value: (counts[value], tie_break(value)))
    return winner, counts[winner] / len(reported)


def _cross_provider_capability(
    rows: tuple[_CrossProviderRow, ...],
) -> ModelReasoningCapability:
    """Vote each capability field independently across the matching rows."""
    capabilities = [row.capability for row in rows]

    def vote_bool(reader: Callable[[ModelReasoningCapability], bool | None]):
        result = _modal((reader(item) for item in capabilities), lambda value: value)
        return result[0] if result else None

    efforts = _modal(
        (item.supported_efforts for item in capabilities),
        lambda value: (len(value), sorted(effort.value for effort in value)),
    )
    return ModelReasoningCapability(
        can_reason=vote_bool(lambda item: item.can_reason),
        supports_effort_control=vote_bool(lambda item: item.supports_effort_control),
        supports_toggle_control=vote_bool(lambda item: item.supports_toggle_control),
        supports_budget_control=vote_bool(lambda item: item.supports_budget_control),
        supported_efforts=efforts[0] if efforts else None,
        mandatory=vote_bool(lambda item: item.mandatory),
        default_enabled=vote_bool(lambda item: item.default_enabled),
    )


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

    rows = _lookup_in_bucket(_cached_cross_provider_index(cache_path), model_id)
    match: CrossProviderMatch | None = None
    if rows:
        output = _modal((row.output_limit for row in rows), lambda value: value)
        match = CrossProviderMatch(
            capability=_cross_provider_capability(rows),
            output_limit=output[0] if output else None,
            match_count=len(rows),
            output_agreement=output[1] if output else None,
        )
        logger.info(
            "models.dev has no bucket for {}; APPROXIMATE cross-provider match "
            "for {}: matches={}, output limit {} ({} agreement), efforts {}",
            provider_id,
            model_id,
            match.match_count,
            match.output_limit,
            (
                "unreported"
                if match.output_agreement is None
                else f"{match.output_agreement:.0%}"
            ),
            (
                "unknown"
                if match.capability.supported_efforts is None
                else sorted(
                    effort.value for effort in match.capability.supported_efforts
                )
            ),
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
            for candidate in _normalize_candidates(model_id):
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
    limit_index = _cached_output_limit_index(path)
    bucket = limit_index.get(provider_id)
    if bucket is None:
        alias = PROVIDER_ID_ALIASES.get(provider_id)
        if alias is not None:
            bucket = limit_index.get(alias)
    if bucket is None:
        if _has_models_dev_bucket(_cached_raw_index(path), provider_id):
            return None
        match = cross_provider_match(provider_id, model_id, path)
        return match.output_limit if match is not None else None
    return _lookup_in_bucket(bucket, model_id)


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

    Returns ``None`` only when neither layer has any data at all.
    """
    return merge_reasoning_capabilities(
        provider_capability,
        model_reasoning_capability_from_models_dev(provider_id, model_id, path),
    )
