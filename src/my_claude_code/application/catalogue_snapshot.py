"""The resolved catalogue records, held for the life of the process.

``GET /v1/models`` enumerates exactly the models
:func:`~my_claude_code.application.catalogue_model.build_catalogue_models`
resolves -- same visibility filter, same kind filter, same refs -- but the
ladder walk behind that record costs 2.0-2.9 s on a 2,059-model catalogue
(measured, 8.21.0's spec §4). The route is an ``async def`` handler, so paying
it per request would hold the event loop for seconds at a time, which is what
``/v1beta/models`` already does and what this module exists to stop happening
twice.

**One tuple, written where it is already built.** The harness fan-out publisher
calls ``build_catalogue_models`` once per publish
(``runtime/harness_catalogues.py``). That call is the snapshot's writer: the
tuple it produced is recorded here beside the key it was produced from, so
filling the snapshot costs one assignment. A request never builds; it looks the
key up, and on a miss rebuilds once, off the loop, behind a single-flight lock.

**No TTL, because a clock is not an input.** Every fact the record depends on
is named in the key below, and an entry is only ever served for the inputs that
produced it:

============================  ===============================================
provider catalogue            ``ProviderModelCache.revision`` -- incremented by
                              every write that changes the stored metadata
settings                      the manager's generation id -- a settings apply
                              replaces the generation
harness tier overrides        the resolved ``HarnessTiers`` value itself, not
                              its file: a value is what the build read
operator model overrides      ``model_overrides.json``'s own ``(mtime_ns,
                              size)`` -- the file that carries the per-model
                              ``context_length`` rung
models.dev index              its ``(mtime_ns, size)`` -- a refreshed index
                              changes the ladder's answers with no publish
OpenRouter live list,         both reach the records through a publish, which
LiteLLM map                   rewrites this snapshot as it rewrites the
                              catalogues
============================  ===============================================

Anything else -- a stale key, a different manager, an input that moved while a
build was running -- is a miss, and a miss rebuilds. The staleness bound is
therefore "the first request after an input changed", never a number of
seconds; a TTL would bound it by a clock instead, and a catalogue that is
wrong for a minute is the IV.16 lie this layer exists to avoid.

**Why this module is in ``application`` and not in ``runtime``.** The import
contract (``tests/contracts/test_import_boundaries.py``) lets ``runtime`` import
``api`` and not the reverse, so a module ``api/model_catalog`` reads may not
live in ``runtime``. It needs ``build_catalogue_models`` and ``CatalogueModel``
(``application``), ``HarnessTiers`` and the overrides path (``config``), and
nothing from ``providers`` -- the models.dev signature rides in on the runtime
port -- which is exactly the ``application`` dependency set.

**The snapshot remembers which runtime built it.** One process runs one
``ProviderRuntimeManager``, but a test process runs many, and two of them can
share a generation id, a revision and every file signature. The owner is
compared by identity, so a snapshot can never be served to a manager that did
not produce it; the alternative -- a per-instance serial in the key -- would
put a test-only fact into a production key.
"""

import asyncio
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from my_claude_code.application.catalogue_model import (
    CatalogueModel,
    build_catalogue_models,
)
from my_claude_code.application.ports import RequestRuntimePort
from my_claude_code.config.harness_tiers import HarnessTiers, current_harness_tiers
from my_claude_code.config.paths import model_overrides_path
from my_claude_code.config.settings import Settings

#: One ``(harness id, tier)`` row of the operator's per-harness tier table, as
#: the value the build actually read: the override's model, fallbacks and
#: paused list. ``None`` is "this harness inherits the global primary".
type CatalogueTierRow = tuple[
    str, str, tuple[str | None, tuple[str, ...], tuple[str, ...]]
]

#: Everything the resolved records depend on, and nothing else. Every part is
#: an int, a string or a stat -- no object whose ``repr`` could be salted per
#: process, and no clock.
type CatalogueSnapshotKey = tuple[
    int,  # the manager's generation id
    int,  # ProviderModelCache.revision
    tuple[CatalogueTierRow, ...],  # the resolved harness-tier table
    tuple[str, int, int] | None,  # model_overrides.json: (path, mtime_ns, size)
    tuple[int, int] | None,  # models.dev index: (mtime_ns, size)
]


@dataclass(frozen=True, slots=True)
class CatalogueSnapshot:
    """One build of the resolved records, under the key it was built from."""

    key: CatalogueSnapshotKey
    #: The runtime that produced it. Compared by identity, never by equality:
    #: two managers are interchangeable only if they are the same manager.
    owner: RequestRuntimePort
    records: tuple[CatalogueModel, ...]
    #: The same records, indexed by the id ``/v1/models`` advertises. Built
    #: once per rebuild rather than once per request.
    by_gateway_id: Mapping[str, CatalogueModel]


#: The process-wide snapshot. Rebound, never mutated, so a reader either sees
#: the previous tuple or the new one and never a half-written pair.
_SNAPSHOT: CatalogueSnapshot | None = None

#: Single-flight for the rebuild. A ``threading`` lock rather than an
#: ``asyncio`` one on purpose: the rebuild runs in a worker thread
#: (``asyncio.to_thread``), several event loops exist across a test session,
#: and a module-level ``asyncio.Lock`` would bind itself to whichever loop
#: happened to touch it first.
_REBUILD_LOCK = threading.Lock()


def catalogue_snapshot_key(
    runtime: RequestRuntimePort, harness_tiers: HarnessTiers
) -> CatalogueSnapshotKey:
    """Return the key the current inputs spell. Cheap: three stats and a walk."""

    inputs = runtime.catalogue_snapshot_inputs()
    return (
        inputs.generation_id,
        inputs.model_cache_revision,
        _tier_rows(harness_tiers),
        _file_signature(model_overrides_path()),
        inputs.models_dev_generation,
    )


def records_by_gateway_id(
    records: tuple[CatalogueModel, ...],
) -> dict[str, CatalogueModel]:
    """Index the records by the id ``/v1/models`` advertises for each.

    ``gateway_id`` is unique by construction (``_append_unique`` dedupes on
    it), so the first entry for an id is the only one. The bare ``mcc/<tier>``
    spelling is *not* here: the catalogue carries its gateway form, and the
    route resolves the second spelling through
    :func:`~my_claude_code.core.gateway_model_ids.gateway_model_id`.
    """

    by_id: dict[str, CatalogueModel] = {}
    for record in records:
        by_id.setdefault(record.gateway_id, record)
    return by_id


def store_catalogue_snapshot(
    runtime: RequestRuntimePort,
    harness_tiers: HarnessTiers,
    key: CatalogueSnapshotKey,
    records: tuple[CatalogueModel, ...],
) -> CatalogueSnapshot | None:
    """Record ``records`` under ``key``, unless an input moved meanwhile.

    ``key`` must be the one computed *before* the build. Recomputing it here is
    what keeps the snapshot honest: a sweep that rewrote the model cache, or a
    tier edit, while the ladder was walking means these records describe inputs
    that no longer hold, and storing them under the new key would serve a stale
    catalogue for ever. The records are dropped instead and the next request
    rebuilds -- the honest cost of a rare race.

    Returns the stored snapshot, or ``None`` when it was dropped.
    """

    if catalogue_snapshot_key(runtime, harness_tiers) != key:
        return None
    global _SNAPSHOT
    snapshot = CatalogueSnapshot(
        key=key,
        owner=runtime,
        records=records,
        by_gateway_id=records_by_gateway_id(records),
    )
    _SNAPSHOT = snapshot
    return snapshot


def read_catalogue_snapshot(runtime: RequestRuntimePort) -> CatalogueSnapshot | None:
    """Return the snapshot for the current inputs, or ``None`` on a miss."""

    snapshot = _SNAPSHOT
    if snapshot is None or snapshot.owner is not runtime:
        return None
    if snapshot.key != catalogue_snapshot_key(runtime, current_harness_tiers()):
        return None
    return snapshot


async def current_catalogue_records(
    settings: Settings, runtime: RequestRuntimePort
) -> Mapping[str, CatalogueModel]:
    """Return the records for the current inputs, rebuilding off the loop.

    A hit costs the key (three stats) and returns the index. A miss rebuilds
    through ``asyncio.to_thread`` under the single-flight lock, so N concurrent
    misses produce one ladder walk and the event loop is never the thread that
    walks it.

    A rebuild that raises returns no records rather than propagating: this is a
    model *listing* every OpenAI-shaped client reads, and an unanswerable
    listing is a worse outcome than one whose ``metadata`` says "not resolved
    for this request". Every capability field stays ``None`` in that case --
    unknown, never a zero (IV.16) -- and the failure is logged.
    """

    harness_tiers = current_harness_tiers()
    key = catalogue_snapshot_key(runtime, harness_tiers)
    snapshot = _SNAPSHOT
    if snapshot is not None and snapshot.owner is runtime and snapshot.key == key:
        return snapshot.by_gateway_id
    try:
        return await asyncio.to_thread(_rebuild, settings, runtime, harness_tiers, key)
    except Exception as exc:
        logger.warning(
            "Catalogue snapshot rebuild failed: exc_type={}", type(exc).__name__
        )
        return {}


def reset_catalogue_snapshot() -> None:
    """Forget the snapshot. Tests only -- the process keeps one for its life."""

    global _SNAPSHOT
    _SNAPSHOT = None


def _rebuild(
    settings: Settings,
    runtime: RequestRuntimePort,
    harness_tiers: HarnessTiers,
    key: CatalogueSnapshotKey,
) -> Mapping[str, CatalogueModel]:
    """Walk the ladder once for every caller waiting on this key.

    Runs on a worker thread. The lock is taken *inside* the thread, so the
    callers that queued behind the first one find the stored snapshot here and
    return it instead of walking the ladder again.
    """

    with _REBUILD_LOCK:
        snapshot = _SNAPSHOT
        if snapshot is not None and snapshot.owner is runtime and snapshot.key == key:
            return snapshot.by_gateway_id
        records = build_catalogue_models(settings, runtime, harness_tiers=harness_tiers)
        stored = store_catalogue_snapshot(runtime, harness_tiers, key, records)
        if stored is not None:
            # The stored index, not a fresh one: the callers that queued behind
            # this build must be handed the same mapping object the next request
            # will read, or each of them would rebuild the derived index the API
            # layer memoises against it.
            return stored.by_gateway_id
        return records_by_gateway_id(records)


def _tier_rows(harness_tiers: HarnessTiers) -> tuple[CatalogueTierRow, ...]:
    """The tier table as a comparable value, sorted so it always spells the same."""

    rows: list[CatalogueTierRow] = [
        (
            harness_id,
            tier,
            (override.model, override.fallbacks, override.paused),
        )
        for harness_id, per_tier in harness_tiers.harnesses.items()
        for tier, override in per_tier.items()
    ]
    rows.sort(key=lambda row: (row[0], row[1]))
    return tuple(rows)


def _file_signature(path: Path) -> tuple[str, int, int] | None:
    """One file's ``(path, mtime_ns, size)``, or ``None`` when it is absent.

    The same signature ``config/model_overrides.py`` and
    ``config/harness_tiers.py`` already key their own parse caches on, so a
    file that is edited on this machine is seen without a restart.
    """

    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size)
