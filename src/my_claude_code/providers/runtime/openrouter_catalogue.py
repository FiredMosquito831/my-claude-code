"""OpenRouter's live model list: fetched, stored and matched for every provider (7.84.0).

``GET https://openrouter.ai/api/v1/models?output_modalities=all`` is public and
keyless. It answers ~665 rows, about 1 MB, with no ``ETag`` and no
``Last-Modified`` (only ``Cache-Control: max-age=120``), so a conditional GET
is impossible and every refresh is a full download. The ``output_modalities``
query matters: the default listing -- the one MCC's own ``open_router``
provider reads -- is text-output models only, and would hide every image,
speech and video model from this rung.

Discipline, the same as models.dev's and LiteLLM's caches:

* **Background only.** Fetched at the models.dev cadence
  (``MODELS_DEV_CACHE_TTL_SECONDS`` measured from the file's mtime, within
  ``MODELS_DEV_FETCH_TIMEOUT_SECONDS``) from the catalogue sweep, never on a
  request path; a lookup only ever reads the file.
* **Integrity.** A payload that is not a list of rows with ids, or that has
  fewer than half the rows already on disk, is discarded and the cache kept
  (the discovery shrink guard's and LiteLLM's rule).
* **Masked.** openrouter.ai is a provider host, so the fetch leaves through the
  ``open_router`` provider's own proxy chain: the one exit
  :func:`~my_claude_code.providers.runtime.config.masked_exit_for` picks, as
  the Providers card's probes do. With every exit unusable and Direct fallback
  off nothing is sent and the cache is kept; with no chain it goes the way
  models.dev's fetch does.
* **Off means absent.** ``MODEL_METADATA_OPENROUTER_LIVE=false`` stops the
  fetch and makes :func:`openrouter_live_catalogue` answer ``None``, which
  every consumer reads as "this rung does not exist" -- the build before
  7.84.0, byte for byte. So does a missing or unreadable file.

**Matching** (spec §10.1). Each OpenRouter row is indexed under its
``normalize_candidates`` keys -- its full id and its bare tail -- exactly as the
cross-provider vote's index is built, and the asking id walks the vote's own
``candidate_ladder``: exact, tag stripped, bare model + tag, bare model. The
first rung with a hit answers. Where one key meets several OpenRouter rows, a
field is answered only when every one of them states the same value.

**Parsing.** Each row goes through the parser MCC's own ``open_router``
provider uses (``openrouter_row_model_info``: the dialect reader plus the
generic declaration reader), without its tool-capable filter, so a field here
means exactly what the same field means on an OpenRouter row of the catalogue.
Its ``supported_parameters`` list is OpenRouter's gateway dialect, not a
model fact: it is read only to derive tool support and whether the model
reasons, never stored as another host's parameter list.
"""

import asyncio
import json
import threading
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.application.openrouter_live import (
    LIVE_MATCH_BARE,
    LIVE_MATCH_BARE_TAGGED,
    LIVE_MATCH_EXACT,
    LIVE_MATCH_TAG_STRIPPED,
    LiveCatalogue,
    LiveModel,
)
from my_claude_code.config.paths import config_dir_path
from my_claude_code.config.provider_catalog import (
    OPENROUTER_DEFAULT_BASE,
    PROVIDER_CATALOG,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.model_ids import (
    ResolutionTier,
    candidate_ladder,
    normalize_candidates,
)
from my_claude_code.providers.model_listing import (
    listed_price_per_million,
    openrouter_row_model_info,
    published_parameters_from_row,
)
from my_claude_code.providers.socks_deadline import bound_socks_handshake

from .config import masked_exit_for, string_setting

#: The one provider whose own list this IS: its rows feed no existing field.
OPENROUTER_PROVIDER_ID = "open_router"

#: Every model OpenRouter serves, whatever it outputs.
OPENROUTER_LIVE_URL = f"{OPENROUTER_DEFAULT_BASE}/models?output_modalities=all"

OPENROUTER_LIVE_CACHE_DIRNAME = "cache"
OPENROUTER_LIVE_CACHE_FILENAME = "openrouter-models.json"

#: A refresh may not shrink the list past this fraction of what is on disk.
OPENROUTER_LIVE_MAX_SHRINK_RATIO = 0.5

#: The cross-provider rungs ``candidate_ladder`` returns, in this rung's words.
_MATCH_LABELS: dict[ResolutionTier, str] = {
    ResolutionTier.CROSS_PROVIDER_EXACT: LIVE_MATCH_EXACT,
    ResolutionTier.CROSS_PROVIDER_TAG_STRIPPED: LIVE_MATCH_TAG_STRIPPED,
    ResolutionTier.CROSS_PROVIDER_BARE_TAGGED: LIVE_MATCH_BARE_TAGGED,
    ResolutionTier.CROSS_PROVIDER_BARE_UNTAGGED: LIVE_MATCH_BARE,
}

#: The fields one row states, merged across rows that share a key.
_FACT_FIELDS: tuple[str, ...] = tuple(
    field.name
    for field in fields(LiveModel)
    if field.name not in {"slugs", "match", "own_list"}
)

_TOOL_PARAMETERS = frozenset({"tools", "tool_choice"})


def openrouter_live_cache_path() -> Path:
    """Where the live list is stored, beside ``models-dev.json``."""

    return (
        config_dir_path()
        / OPENROUTER_LIVE_CACHE_DIRNAME
        / OPENROUTER_LIVE_CACHE_FILENAME
    )


# --------------------------------------------------------------- the store


@dataclass(frozen=True, slots=True)
class _Stored:
    """The parsed file, its rows indexed, for one on-disk generation."""

    mark: str
    fetched_at: str | None
    rows: int
    index: Mapping[str, tuple[LiveModel, ...]]


_store_lock = threading.Lock()
#: path -> the parsed generation. One path in production; a few in a test run.
_stored: dict[Path, _Stored] = {}
_STORE_MAX_PATHS = 4


def reset_openrouter_live_cache() -> None:
    """Forget every parsed generation (tests; a new file is noticed anyway)."""

    with _store_lock:
        _stored.clear()


def _file_mark(path: Path) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def read_openrouter_live_rows(path: Path) -> tuple[list[Any], str | None] | None:
    """The stored rows and when they were fetched, or ``None``."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list):
        return None
    fetched = payload.get("fetched_at")
    return data, fetched if isinstance(fetched, str) else None


def _stored_generation(path: Path) -> _Stored | None:
    mark = _file_mark(path)
    if mark is None:
        return None
    with _store_lock:
        cached = _stored.get(path)
        if cached is not None and cached.mark == mark:
            return cached
    read = read_openrouter_live_rows(path)
    if read is None:
        return None
    rows, fetched_at = read
    built = _Stored(
        mark=mark,
        fetched_at=fetched_at,
        rows=len(rows),
        index=build_live_index(rows),
    )
    with _store_lock:
        if path not in _stored and len(_stored) >= _STORE_MAX_PATHS:
            _stored.pop(next(iter(_stored)))
        _stored[path] = built
    return built


def write_openrouter_live_cache(
    rows: Sequence[Any], path: Path, source_url: str = OPENROUTER_LIVE_URL
) -> Path:
    """Atomically persist the rows as fetched, with when and from where."""

    payload = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "source_url": source_url,
        "data": list(rows),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=True) + "\n", encoding="utf-8")
    temp.replace(path)
    return path


def payload_passes_integrity(payload: Any, previous_rows: int | None) -> bool:
    """A list of rows with ids, and no halving of what is already on disk."""

    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return False
    if not any(isinstance(row, Mapping) and _row_id(row) for row in data):
        return False
    return not (
        previous_rows and len(data) < previous_rows * OPENROUTER_LIVE_MAX_SHRINK_RATIO
    )


# ------------------------------------------------------------- parsing rows


def _row_id(row: Mapping[str, Any]) -> str | None:
    value = row.get("id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _listed_day(value: Any) -> str | None:
    """OpenRouter's ``created`` (epoch seconds) as ``YYYY-MM-DD``, UTC."""

    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(float(value), UTC).date().isoformat()
    except OverflowError, OSError, ValueError:
        return None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def live_model_from_row(row: Any) -> LiveModel | None:
    """What one OpenRouter row states, as the rung reads it. Never raises."""

    if not isinstance(row, Mapping):
        return None
    slug = _row_id(row)
    info = openrouter_row_model_info(row)
    if slug is None or info is None:
        return None
    try:
        return _facts(row, slug, info)
    except Exception:
        return None


def _facts(row: Mapping[str, Any], slug: str, info: ProviderModelInfo) -> LiveModel:
    published = published_parameters_from_row(row)
    declared = info.declared
    can_reason: bool | None
    tools: bool | None
    if published is not None:
        # The provider's own fold (``ProviderModelCache``): the reasoning
        # block's answer, else the thinking flag the parameter list implies.
        capability = info.reasoning_capability
        can_reason = (
            capability.can_reason
            if capability is not None and capability.can_reason is not None
            else info.supports_thinking
        )
        tools = bool(published & _TOOL_PARAMETERS)
    else:
        # No list at all: silence, not "no". Only what the row says elsewhere.
        can_reason = None if declared is None else declared.reasoning
        tools = None if declared is None else declared.tool_calls
    pricing = row.get("pricing")
    rates: Mapping[str, Any] = pricing if isinstance(pricing, Mapping) else {}
    return LiveModel(
        slugs=(slug,),
        match=LIVE_MATCH_EXACT,
        modalities=None if declared is None else declared.modalities,
        supports_vision=info.supports_vision,
        can_reason=can_reason,
        supports_tool_calls=tools,
        context_length=info.context_length,
        max_output_tokens=info.max_output_tokens,
        input_price=info.input_price,
        output_price=info.output_price,
        description=_text(row.get("description")),
        knowledge_cutoff=_text(row.get("knowledge_cutoff")),
        listed_at=_listed_day(row.get("created")),
        cache_read_price=listed_price_per_million(rates.get("input_cache_read")),
        cache_write_price=listed_price_per_million(rates.get("input_cache_write")),
        reasoning_price=listed_price_per_million(rates.get("internal_reasoning")),
    )


def build_live_index(rows: Iterable[Any]) -> dict[str, tuple[LiveModel, ...]]:
    """Every row under each of its match keys: the full id and its bare tail."""

    built: dict[str, list[LiveModel]] = {}
    for row in rows:
        model = live_model_from_row(row)
        if model is None:
            continue
        for key in normalize_candidates(model.slugs[0]):
            built.setdefault(key, []).append(model)
    return {key: tuple(models) for key, models in built.items()}


def _merged(models: Sequence[LiveModel], match: str, own_list: bool) -> LiveModel:
    """One answer from the rows one key met: a field only where they all agree."""

    if len(models) == 1:
        only = models[0]
        return LiveModel(
            slugs=only.slugs,
            match=match,
            own_list=own_list,
            **{name: getattr(only, name) for name in _FACT_FIELDS},
        )
    agreed: dict[str, Any] = {}
    for name in _FACT_FIELDS:
        values = {_hashable(getattr(model, name)) for model in models}
        agreed[name] = getattr(models[0], name) if len(values) == 1 else None
    slugs = tuple(sorted({slug for model in models for slug in model.slugs}))
    return LiveModel(slugs=slugs, match=match, own_list=own_list, **agreed)


def _hashable(value: Any) -> Any:
    return value if not isinstance(value, list | dict) else repr(value)


def match_live_model(
    index: Mapping[str, tuple[LiveModel, ...]], provider_id: str, model_id: str
) -> LiveModel | None:
    """The rung's answer for one asking ``(provider, model)``, or ``None``."""

    for tier, key in candidate_ladder(model_id):
        found = index.get(key)
        if found:
            return _merged(
                found,
                _MATCH_LABELS[tier],
                own_list=provider_id == OPENROUTER_PROVIDER_ID,
            )
    return None


# ----------------------------------------------------------------- binding


def openrouter_live_enabled(settings: Settings) -> bool:
    """Whether the operator has the rung on (``MODEL_METADATA_OPENROUTER_LIVE``)."""

    return bool(getattr(settings, "model_metadata_openrouter_live", False))


def openrouter_live_catalogue(
    settings: Settings, path: Path | None = None
) -> LiveCatalogue | None:
    """The stored live list, bound for one listing; ``None`` when off or absent."""

    if not openrouter_live_enabled(settings):
        return None
    stored = _stored_generation(
        path if path is not None else openrouter_live_cache_path()
    )
    if stored is None or not stored.index:
        return None
    index = stored.index

    def lookup(provider_id: str, model_id: str) -> LiveModel | None:
        return match_live_model(index, provider_id, model_id)

    return LiveCatalogue(
        mark=stored.mark,
        fetched_at=stored.fetched_at,
        rows=stored.rows,
        lookup=lookup,
    )


def openrouter_live_is_due(settings: Settings, path: Path) -> bool:
    """Whether the sweep should fetch: on, and the file absent or past its TTL."""

    if not openrouter_live_enabled(settings):
        return False
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return True
    age = datetime.now(UTC).timestamp() - mtime
    return age >= float(settings.models_dev_cache_ttl_seconds)


# ------------------------------------------------------------------ fetching


@dataclass(frozen=True, slots=True)
class OpenRouterLiveRefresh:
    """What one refresh did, for the log and the masking contract.

    ``status``: ``fetched`` (stored), ``not_sent`` (the chain allows no exit and
    Direct fallback is off -- ``detail`` is the sentence a probe gets),
    ``rejected`` (the payload failed the integrity check; cache kept),
    ``failed`` (the GET or the write did not land; cache kept), ``off``.
    ``proxy_exit`` is the masked name of the exit it went through, ``"direct"``
    for this computer's own address chosen by a chain, ``None`` with no chain.
    """

    status: str
    detail: str = ""
    proxy_exit: str | None = None
    rows: int = 0


async def fetch_openrouter_live(url: str, *, proxy: str | None, timeout: float) -> Any:
    """One keyless GET of the live list; raises on any failure."""

    client = bound_socks_handshake(
        httpx.AsyncClient(proxy=proxy or None, timeout=timeout)
    )
    try:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()
    finally:
        await client.aclose()


async def refresh_openrouter_live(
    settings: Settings,
    path: Path | None = None,
    url: str = OPENROUTER_LIVE_URL,
) -> OpenRouterLiveRefresh:
    """Fetch through the ``open_router`` chain, check, and store; never raises."""

    if not openrouter_live_enabled(settings):
        return OpenRouterLiveRefresh(status="off")
    cache_path = path if path is not None else openrouter_live_cache_path()
    descriptor = PROVIDER_CATALOG[OPENROUTER_PROVIDER_ID]
    try:
        exit_ = masked_exit_for(
            OPENROUTER_PROVIDER_ID,
            string_setting(settings, descriptor.proxy_attr) or None,
            settings,
            name=descriptor.display_name,
        )
    except Exception as exc:
        logger.debug("OpenRouter live list: no exit resolved: {}", type(exc).__name__)
        return OpenRouterLiveRefresh(status="failed", detail=type(exc).__name__)
    if exit_.refused:
        logger.info("OpenRouter live model list not fetched: {}", exit_.refused)
        return OpenRouterLiveRefresh(status="not_sent", detail=exit_.refused)
    try:
        payload = await fetch_openrouter_live(
            url,
            proxy=exit_.proxy,
            timeout=float(settings.models_dev_fetch_timeout_seconds),
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("OpenRouter live list fetch failed: {}", type(exc).__name__)
        return OpenRouterLiveRefresh(
            status="failed", detail=type(exc).__name__, proxy_exit=exit_.label
        )
    previous = read_openrouter_live_rows(cache_path)
    if not payload_passes_integrity(
        payload, None if previous is None else len(previous[0])
    ):
        logger.info(
            "OpenRouter live model list rejected by the integrity check; "
            "the copy on disk is kept"
        )
        return OpenRouterLiveRefresh(status="rejected", proxy_exit=exit_.label)
    rows = payload["data"]
    try:
        write_openrouter_live_cache(rows, cache_path, url)
    except OSError as exc:
        logger.debug("OpenRouter live list write failed: {}", type(exc).__name__)
        return OpenRouterLiveRefresh(
            status="failed", detail=type(exc).__name__, proxy_exit=exit_.label
        )
    logger.info(
        "OpenRouter live model list stored: {} models (exit: {})",
        len(rows),
        exit_.label or "no proxy chain",
    )
    return OpenRouterLiveRefresh(
        status="fetched", proxy_exit=exit_.label, rows=len(rows)
    )


__all__ = [
    "OPENROUTER_LIVE_CACHE_FILENAME",
    "OPENROUTER_LIVE_URL",
    "OPENROUTER_PROVIDER_ID",
    "OpenRouterLiveRefresh",
    "build_live_index",
    "fetch_openrouter_live",
    "live_model_from_row",
    "match_live_model",
    "openrouter_live_cache_path",
    "openrouter_live_catalogue",
    "openrouter_live_enabled",
    "openrouter_live_is_due",
    "payload_passes_integrity",
    "read_openrouter_live_rows",
    "refresh_openrouter_live",
    "reset_openrouter_live_cache",
    "write_openrouter_live_cache",
]
