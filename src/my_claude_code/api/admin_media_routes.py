"""Admin reads for the media rails (7.67.0).

Two read-only views, both kept apart from their chat neighbours on purpose:

* ``GET /admin/api/media/models`` -- the Models page's "Media models" section:
  every ``provider/model`` configured on a media rail, where it sits on each
  rail, what its provider *declares* it serves (``config/media_surfaces.py``),
  what models.dev catalogues it as producing, whether the MEDIA books have it
  benched and whether its provider has a key. Media refs never join the chat
  model list or ``/v1/models``, so they get their own table.
* ``GET /admin/api/analytics/media`` -- the Analytics page's "Media" block:
  media requests over the page's own time window, per rail and per
  provider/model, plus the states of the video jobs created in it. The chat
  stats and their rollups are not touched; this reads only rows a media
  endpoint wrote.

And one file (7.68.0): ``GET /admin/api/media/{sha}`` serves a file the media
store kept, for the request detail's preview -- loopback only, like the rest.

Nothing here contacts an upstream, builds a provider or writes a file. The
settings are the request runtime's, SQLite is read off the event loop, and
models.dev is read from its disk cache through ``api.model_admin``.
"""

import asyncio
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse

from my_claude_code.api.model_admin import media_output_modalities
from my_claude_code.application.media.executor import media_route_health_registry
from my_claude_code.application.media.rails import rail_refs
from my_claude_code.application.media.request import (
    OPERATION_RAILS,
    RAIL_OPERATIONS,
    RAIL_SETTINGS,
    MediaRail,
)
from my_claude_code.application.route_health import RouteHealthRegistry
from my_claude_code.config.admin.status import provider_config_status
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
    MEDIA_OPERATION_VIDEO_CONTENT,
    MEDIA_OPERATION_VIDEO_CREATE,
    MEDIA_OPERATION_VIDEO_DELETE,
    MEDIA_OPERATION_VIDEO_RETRIEVE,
    MediaSurface,
)
from my_claude_code.config.model_refs import (
    parse_model_name,
    parse_model_ref_list,
    parse_provider_type,
)
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG, ProviderDescriptor
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.settings import Settings
from my_claude_code.core.request_log import store_from_settings

from .admin_proxy_routes import _value_state
from .admin_routes import require_loopback_admin
from .dependencies import get_settings

router = APIRouter()

#: The short words a declared operation is shown as, on a chip.
OPERATION_LABELS: dict[str, str] = {
    MEDIA_OPERATION_IMAGE_GENERATE: "image generate",
    MEDIA_OPERATION_IMAGE_EDIT: "image edit",
    MEDIA_OPERATION_SPEECH: "speech",
    MEDIA_OPERATION_TRANSCRIBE: "transcribe",
    MEDIA_OPERATION_TRANSLATE: "translate",
    MEDIA_OPERATION_VIDEO_CREATE: "video create",
    MEDIA_OPERATION_VIDEO_RETRIEVE: "video status",
    MEDIA_OPERATION_VIDEO_CONTENT: "video content",
    MEDIA_OPERATION_VIDEO_DELETE: "video delete",
}

#: What a request row that never reached a provider is filed under.
UNROUTED_LABEL = "(not routed)"

#: A media store content address: a SHA-256, as 64 hex characters.
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")

#: On every stored file served: never sniffed into another type, and run
#: sandboxed -- no script -- if a browser opens it as a page of its own.
_FILE_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox",
}


def _served_type(mime: str | None) -> str:
    """A picture, a sound or a film as its own type; anything else as bytes.

    The type was recorded from what the host said, so it is not trusted to
    be harmless: only ``image/``, ``audio/`` and ``video/`` types are served
    as themselves, and never an XML one (SVG can carry script).
    """

    base = (mime or "").split(";")[0].strip().lower()
    if base.startswith(("image/", "audio/", "video/")) and "xml" not in base:
        return base
    return "application/octet-stream"


def _declared(descriptor: ProviderDescriptor | None) -> list[dict[str, Any]]:
    """The media surfaces a provider declares, one entry per surface."""

    if descriptor is None:
        return []
    return [_surface_entry(surface) for surface in descriptor.media_surfaces]


def _surface_entry(surface: MediaSurface) -> dict[str, Any]:
    return {
        "operation": surface.operation,
        "label": OPERATION_LABELS.get(surface.operation, surface.operation),
        "path": surface.path,
        "stream": surface.stream,
        "formats": None if surface.formats is None else list(surface.formats),
    }


def _serves(descriptor: ProviderDescriptor | None, rail: MediaRail) -> bool:
    """Whether a provider declares any operation this rail routes."""

    if descriptor is None:
        return False
    wanted = set(RAIL_OPERATIONS[rail])
    return any(surface.operation in wanted for surface in descriptor.media_surfaces)


def _key_state(status: Mapping[str, Any] | None) -> dict[str, Any]:
    """The provider's credential readiness, as the Providers page states it."""

    if status is None:
        return {"status": "unknown", "label": "Unknown provider", "key_count": None}
    return {
        "status": status.get("status"),
        "label": status.get("label"),
        "key_count": status.get("key_count"),
    }


def _health(registry: RouteHealthRegistry, model_ref: str) -> dict[str, Any]:
    """The MEDIA bench readout for one ref -- never the chat books."""

    reason = registry.why(model_ref)
    if reason is None:
        return {"benched": False}
    return {
        "benched": True,
        "reason": reason.sentence(),
        "remaining_seconds": round(reason.remaining_seconds, 1),
    }


@dataclass(frozen=True, slots=True)
class MediaBenchReadout:
    """The MEDIA bench state of every ref on a rail, and whether benching is on."""

    enabled: bool
    health: Mapping[str, dict[str, Any]]


def media_bench_readout(settings: Settings) -> MediaBenchReadout:
    """Read the MEDIA bench for every ref on a rail. Call it on the event loop.

    Reading a bench is not read-only: ``RouteHealthRegistry.why`` asks
    ``is_ejected``, which clears a bench whose time is up, its outcome window
    included. The media requests that record failures into the same books --
    and count that window while they do -- run on the event loop, so a
    readout taken on a worker thread, as it was before 7.72.1, could clear the
    window under a count in progress. It is one lookup per ref, so the route
    takes it on the loop and hands only the rest of the page to a worker. The
    refs are asked in the order the payload has always asked them.
    """

    registry = media_route_health_registry(settings)
    health: dict[str, dict[str, Any]] = {}
    for rail in MediaRail:
        for ref in rail_refs(settings, rail):
            if ref not in health:
                health[ref] = _health(registry, ref)
    return MediaBenchReadout(enabled=registry.enabled, health=health)


def _all_descriptors() -> tuple[dict[str, ProviderDescriptor], set[str]]:
    """Every provider this install knows, disabled custom entries included.

    Returns the descriptors and the ids of the custom entries switched off,
    which a rail may still name (``configurable_ids``) but cannot route to.
    """

    registry = get_provider_registry()
    descriptors: dict[str, ProviderDescriptor] = dict(PROVIDER_CATALOG)
    disabled: set[str] = set()
    for entry in registry.list_custom():
        descriptors[entry.provider_id] = registry.descriptor_for(entry)
        if not entry.enabled:
            disabled.add(entry.provider_id)
    return descriptors, disabled


def media_models_payload(
    settings: Settings, bench: MediaBenchReadout | None = None
) -> dict[str, Any]:
    """Everything the Models page's media section renders. Synchronous.

    Run through ``asyncio.to_thread``: the models.dev ladder may build its
    index from the 4.9 MB cache on the first call after a refresh. ``bench``
    is the readout :func:`media_bench_readout` took on the event loop; without
    one it is taken here, on the caller's thread, which is right only for a
    caller that is on the event loop itself.
    """

    if bench is None:
        bench = media_bench_readout(settings)
    descriptors, disabled = _all_descriptors()
    custom_ids = {
        provider_id
        for provider_id in descriptors
        if provider_id not in PROVIDER_CATALOG
    }
    keys = {
        str(status.get("provider_id")): status
        for status in provider_config_status(_value_state(settings))
    }

    rails: list[dict[str, Any]] = []
    rows: dict[str, dict[str, Any]] = {}
    placements: dict[str, list[dict[str, Any]]] = {}
    for rail in MediaRail:
        names = RAIL_SETTINGS[rail]
        primary = str(getattr(settings, names.model_attr, "") or "").strip()
        paused = frozenset(
            parse_model_ref_list(str(getattr(settings, names.paused_attr, "") or ""))
        )
        refs = rail_refs(settings, rail)
        rails.append(
            {
                "rail": rail.value,
                "label": names.label,
                "model_env": names.model_env,
                "operations": [
                    {"operation": op, "label": OPERATION_LABELS.get(op, op)}
                    for op in RAIL_OPERATIONS[rail]
                ],
                "refs": list(refs),
            }
        )
        fallback = 0
        for index, ref in enumerate(refs):
            if index == 0 and ref == primary:
                position = "primary"
            else:
                fallback += 1
                position = f"fallback {fallback}"
            provider_id = parse_provider_type(ref)
            descriptor = descriptors.get(provider_id)
            if ref not in rows:
                if descriptor is None:
                    provider_state = "unknown"
                elif provider_id in disabled:
                    provider_state = "disabled"
                else:
                    provider_state = "known"
                model_id = parse_model_name(ref)
                placements[ref] = []
                rows[ref] = {
                    "model_ref": ref,
                    "provider_id": provider_id,
                    "provider_name": (
                        descriptor.display_name if descriptor else provider_id
                    ),
                    "model_id": model_id,
                    "custom": provider_id in custom_ids,
                    "provider_state": provider_state,
                    "placements": placements[ref],
                    "declared": _declared(descriptor),
                    "modalities": media_output_modalities(provider_id, model_id),
                    "health": bench.health[ref],
                    "key": _key_state(keys.get(provider_id)),
                }
            placements[ref].append(
                {
                    "rail": rail.value,
                    "label": names.label,
                    "position": position,
                    "index": index,
                    "paused": ref in paused,
                    "served": _serves(descriptor, rail),
                }
            )

    providers: list[dict[str, Any]] = []
    for provider_id, descriptor in descriptors.items():
        if not descriptor.media_surfaces:
            continue
        providers.append(
            {
                "provider_id": provider_id,
                "display_name": descriptor.display_name,
                "custom": provider_id in custom_ids,
                "enabled": provider_id not in disabled,
                "declared": _declared(descriptor),
                "rails": [
                    RAIL_SETTINGS[rail].label
                    for rail in MediaRail
                    if _serves(descriptor, rail)
                ],
                "key": _key_state(keys.get(provider_id)),
            }
        )
    return {
        "rails": rails,
        "models": list(rows.values()),
        "providers": providers,
        "bench_enabled": bench.enabled,
    }


@router.get("/admin/api/media/models")
async def media_models(request: Request, settings: Settings = Depends(get_settings)):
    """The Models page's media rows and the providers that can serve them."""

    require_loopback_admin(request)
    # The bench here, on the loop; the rest on a worker. See
    # ``media_bench_readout`` for why the split is where it is.
    bench = media_bench_readout(settings)
    return await asyncio.to_thread(media_models_payload, settings, bench)


def _empty_group(group: str) -> dict[str, Any]:
    """A rail with no requests in the window: counts 0, every measure unknown."""

    return {
        "group": group,
        "requests": 0,
        "succeeded": 0,
        "failed": 0,
        "cancelled": 0,
        "video_jobs": 0,
        "duration_count": 0,
        "images_out": None,
        "images_out_measured": 0,
        "audio_seconds_out": None,
        "audio_seconds_out_measured": 0,
        "audio_seconds_in": None,
        "audio_seconds_in_measured": 0,
        "video_seconds": None,
        "video_seconds_measured": 0,
        "bytes_out": None,
        "bytes_out_measured": 0,
        "cost_usd": None,
        "cost_usd_measured": 0,
        "cost_reported_usd": None,
        "cost_reported_usd_measured": 0,
        "cost_estimated_usd": None,
        "cost_estimated_usd_measured": 0,
        "cost_unpriced": 0,
        "avg_duration_ms": None,
        "median_duration_ms": None,
    }


def _rail_label(group: str) -> str:
    try:
        return RAIL_SETTINGS[MediaRail(group)].label
    except ValueError:
        # An operation no rail names (a row written by a newer build): shown
        # under its own name rather than dropped.
        return group


def _provider_names(provider_ids: Iterable[str | None]) -> dict[str, str]:
    descriptors, _disabled = _all_descriptors()
    names: dict[str, str] = {}
    for provider_id in provider_ids:
        if provider_id is None or provider_id in names:
            continue
        descriptor = descriptors.get(provider_id)
        names[provider_id] = descriptor.display_name if descriptor else provider_id
    return names


def media_analytics_payload(stats: Mapping[str, Any]) -> dict[str, Any]:
    """Label the store's media aggregates for the Analytics page's Media card.

    Every rail is listed, a rail with no traffic as zero requests and unknown
    measures; a group the store found that no rail names follows them.
    """

    groups = {entry["group"]: dict(entry) for entry in stats.get("groups", [])}
    rails: list[dict[str, Any]] = []
    for rail in MediaRail:
        entry = groups.pop(rail.value, None) or _empty_group(rail.value)
        rails.append({**entry, "rail": rail.value, "label": _rail_label(rail.value)})
    for group, entry in groups.items():
        rails.append({**entry, "rail": group, "label": _rail_label(group)})

    models = list(stats.get("models", []))
    jobs = list(stats.get("jobs", []))
    names = _provider_names(
        [row.get("provider") for row in models] + [job.get("provider") for job in jobs]
    )
    return {
        "total": int(stats.get("total") or 0),
        "rails": rails,
        "models": [
            {
                **row,
                "rail": row["group"],
                "label": _rail_label(row["group"]),
                "provider_name": (
                    UNROUTED_LABEL
                    if row.get("provider") is None
                    else names.get(row["provider"], row["provider"])
                ),
            }
            for row in models
        ],
        "jobs": [
            {
                **job,
                "provider_name": names.get(job["provider"], job["provider"]),
            }
            for job in jobs
        ],
        "job_states": dict(stats.get("job_states", {})),
    }


@router.get("/admin/api/analytics/media")
async def analytics_media(
    request: Request,
    since: float | None = None,
    until: float | None = None,
    settings: Settings = Depends(get_settings),
):
    """Media requests over the Analytics page's window, apart from chat.

    ``since`` / ``until`` are the epoch seconds every other Analytics block is
    sent; nothing else filters this block, and it never changes what the chat
    stats count.
    """

    require_loopback_admin(request)
    store = store_from_settings(settings)
    if store is None:
        return {"enabled": False}
    groups = {operation: rail.value for operation, rail in OPERATION_RAILS.items()}
    stats = await asyncio.to_thread(
        store.media_stats, since=since, until=until, groups=groups
    )
    payload = await asyncio.to_thread(media_analytics_payload, stats)
    return {
        "enabled": True,
        "window": {"since": since, "until": until},
        **payload,
    }


@router.get("/admin/api/media/{sha}")
async def media_file(
    sha: str, request: Request, settings: Settings = Depends(get_settings)
):
    """One file the media store kept, as its own type (the request detail's preview).

    Loopback only. 400 for anything but a SHA-256; 404 unless the media store
    holds that address's file. Declared after ``/admin/api/media/models`` so
    that path is never read as an address.
    """

    require_loopback_admin(request)
    if not _SHA256.fullmatch(sha):
        raise HTTPException(status_code=400, detail="Not a SHA-256 content address")
    store = store_from_settings(settings)
    found = (
        await asyncio.to_thread(store.stored_media_file, sha.lower())
        if store is not None
        else None
    )
    if found is None:
        raise HTTPException(status_code=404, detail="Media file not stored")
    path, mime = found
    return FileResponse(path, media_type=_served_type(mime), headers=_FILE_HEADERS)
