"""The provider/model catalogue, kept from one start to the next.

`ProviderModelCache` is a dict in memory, so every start rediscovers every
provider's `/models` over the network before the first Models page or the first
`/v1/models` can be answered -- measured at 3.2-6.6 s, and it is the single
largest thing a restart re-derives. Nothing about a catalogue is per-process:
it is what a set of gateways published, and the sweep that runs a second later
is what corrects it.

So the sweep becomes the *writer* of a stored catalogue and the start becomes a
*reader* of it. Two guards, both load-bearing:

- **The stored catalogue carries an age and the page shows it.** A loaded
  catalogue reports the moment it was written, not the moment it was loaded, so
  the Models page says "last refreshed 40 min ago" rather than pretending a
  sweep just happened.
- **A sweep that learned nothing new does not rewrite the file.** The document
  is built deterministically -- sets sorted, providers and models in the order
  they were listed -- so an unchanged catalogue serialises byte for byte to
  what is already on disk, and the write is skipped.

Scope, not content, is what invalidates the file: a document written when a
different set of providers was configured is ignored rather than filtered,
because the cheap thing to do about a stale catalogue is the sweep that was
going to run anyway.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from loguru import logger

from my_claude_code.application.model_metadata import (
    ProviderModelInfo,
    model_info_document,
    model_info_from_document,
)
from my_claude_code.config.paths import config_dir_path
from my_claude_code.core.derived_cache import DerivedCache, DerivedEntry
from my_claude_code.core.version import package_version

#: The entry name under ``<config dir>/cache/derived/``.
CATALOGUE_ENTRY = "provider-catalogue"

#: Where the derived documents live. The same two path segments
#: ``application.derived_payloads`` uses; repeated rather than imported because
#: ``runtime`` reaching into that module for two strings would couple the
#: catalogue's storage to the dashboard's.
_CACHE_DIRNAME = "cache"
_CACHE_SUBDIR = "derived"


def catalogue_cache() -> DerivedCache:
    """The store, under the *resolved* config dir.

    Resolved per call, not captured at import: the config directory is itself
    resolved per process and the hermetic test guard redirects it.
    """

    return DerivedCache(config_dir_path() / _CACHE_DIRNAME / _CACHE_SUBDIR)


def catalogue_scope_key(provider_ids: Iterable[str]) -> str:
    """What makes a stored catalogue applicable to this process at all.

    The provider scope, and the running version -- the latter so that a release
    which changes what discovery records never loads a document written by the
    release before it.
    """

    digest = hashlib.sha256()
    digest.update(package_version().encode("utf-8"))
    digest.update(b"\x00providers\x00")
    for provider_id in sorted(provider_ids):
        digest.update(provider_id.encode("utf-8"))
        digest.update(b"\x00")
    return f"catalogue-{digest.hexdigest()[:32]}"


def catalogue_document(
    catalogues: Mapping[str, tuple[ProviderModelInfo, ...]],
) -> dict[str, Any]:
    """The whole cache as JSON-safe data, deterministically.

    Providers sorted, models left in the order their provider listed them: the
    first is what a reader's eye lands on and what ``/v1/models`` puts first,
    and re-sorting it would be a change nobody asked for.
    """

    return {
        "providers": [
            {
                "provider_id": provider_id,
                "models": [
                    model_info_document(info) for info in catalogues[provider_id]
                ],
            }
            for provider_id in sorted(catalogues)
        ]
    }


def catalogues_from_document(
    document: Any,
) -> dict[str, tuple[ProviderModelInfo, ...]]:
    """Rebuild the catalogues, dropping anything that is not one.

    An entry that cannot be read is skipped rather than raised on: this is a
    cache, and the answer to an unreadable one is the sweep, never a failed
    start.
    """

    if not isinstance(document, dict):
        return {}
    providers = document.get("providers")
    if not isinstance(providers, list):
        return {}
    restored: dict[str, tuple[ProviderModelInfo, ...]] = {}
    for entry in providers:
        if not isinstance(entry, dict):
            continue
        provider_id = entry.get("provider_id")
        models = entry.get("models")
        if not isinstance(provider_id, str) or not isinstance(models, list):
            continue
        infos = tuple(
            info
            for info in (model_info_from_document(model) for model in models)
            if info is not None
        )
        if infos:
            restored[provider_id] = infos
    return restored


def read_stored_catalogue(
    key: str, *, cache: DerivedCache | None = None
) -> tuple[dict[str, tuple[ProviderModelInfo, ...]], float] | None:
    """The stored catalogues and the moment they were written, or ``None``."""

    store = catalogue_cache() if cache is None else cache
    entry = store.read(CATALOGUE_ENTRY)
    if entry is None or not entry.matches(key):
        return None
    restored = catalogues_from_document(entry.payload)
    if not restored:
        return None
    return restored, entry.computed_at


def store_catalogue(
    catalogues: Mapping[str, tuple[ProviderModelInfo, ...]],
    key: str,
    *,
    computed_at: float,
    cache: DerivedCache | None = None,
) -> bool:
    """Write the catalogue, unless the stored one already says exactly this.

    Returns whether anything was written. The comparison is on the document, not
    on a timestamp: twelve catalogue documents rewritten every hour to say what
    they already said is churn the sweep has no reason to cause.
    """

    store = catalogue_cache() if cache is None else cache
    document = catalogue_document(catalogues)
    existing = store.read(CATALOGUE_ENTRY)
    if (
        existing is not None
        and existing.matches(key)
        and _same_document(existing, document)
    ):
        return False
    return store.write(
        CATALOGUE_ENTRY,
        key=key,
        payload=document,
        computed_at=computed_at,
        compact=True,
    )


def _same_document(entry: DerivedEntry, document: dict[str, Any]) -> bool:
    try:
        return json.dumps(entry.payload, sort_keys=False) == json.dumps(
            document, sort_keys=False
        )
    except (TypeError, ValueError) as exc:
        logger.debug("Stored catalogue could not be compared: {}", exc)
        return False


__all__ = [
    "CATALOGUE_ENTRY",
    "catalogue_cache",
    "catalogue_document",
    "catalogue_scope_key",
    "catalogues_from_document",
    "read_stored_catalogue",
    "store_catalogue",
]
