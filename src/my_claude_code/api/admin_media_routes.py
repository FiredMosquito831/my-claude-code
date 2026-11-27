"""Admin reads for the media rails (7.67.0).

Two read-only views, both kept apart from their chat neighbours on purpose:

* ``GET /admin/api/media/models`` -- the Models page's "Media models" section:
  every ``provider/model`` configured on a media rail, where it sits on each
  rail, what its provider *declares* it serves (``config/media_surfaces.py``),
  what its provider's own model list -- or else models.dev -- states it
  produces, its stated kind, whether the MEDIA books have it
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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from my_claude_code.api.model_admin import (
    FACT_KIND_LABELS,
    declared_modalities_lookup,
    declared_model_kind,
    media_output_modalities,
    model_kind_payload,
)
from my_claude_code.application.litellm_model_map import LiteLLMCatalogue
from my_claude_code.application.media.capabilities import (
    MediaCapabilityInputs,
    MediaCapabilityProfile,
    resolve_media_capability_profile,
)
from my_claude_code.application.media.endpoint_ladder import (
    MediaEndpointCandidate,
    ResolvedMediaEndpointLadder,
    resolve_media_endpoint_ladder,
)
from my_claude_code.application.media.executor import media_route_health_registry
from my_claude_code.application.media.rails import rail_refs
from my_claude_code.application.media.request import (
    OPERATION_RAILS,
    RAIL_OPERATIONS,
    RAIL_SETTINGS,
    MediaRail,
)
from my_claude_code.application.media.schemas import (
    MediaSchemaInputs,
    field_acceptance,
    rail_model_schemas,
    schema_inputs,
)
from my_claude_code.application.model_kinds import (
    KindWordsLookup,
    ModalitiesLookup,
    ModelKind,
    media_rail_placements,
)
from my_claude_code.application.openrouter_live import LiveCatalogue
from my_claude_code.application.route_health import RouteHealthRegistry
from my_claude_code.config.admin.status import provider_config_status
from my_claude_code.config.media_overrides import (
    MediaOperationOverride,
    MediaOverridesDocument,
    current_media_overrides,
    effective_media_override,
    is_valid_media_override_field,
    load_media_overrides,
    save_media_overrides,
    with_media_override,
    without_media_override,
)
from my_claude_code.config.media_request_fields import (
    OperatorFieldStatement,
    load_media_request_fields,
    save_media_request_fields,
    statement_problem,
    with_statement,
    without_statement,
)
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_IMAGE_VARIATION,
    MEDIA_OPERATION_OCR,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
    MEDIA_OPERATION_VIDEO_CONTENT,
    MEDIA_OPERATION_VIDEO_CREATE,
    MEDIA_OPERATION_VIDEO_DELETE,
    MEDIA_OPERATION_VIDEO_EDIT,
    MEDIA_OPERATION_VIDEO_RETRIEVE,
    MEDIA_OPERATIONS,
    MediaSurface,
    effective_surfaces_for,
    surface_key,
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
from .dependencies import get_services, get_settings
from .ports import ApiServices

router = APIRouter()

#: The short words a declared operation is shown as, on a chip.
OPERATION_LABELS: dict[str, str] = {
    MEDIA_OPERATION_IMAGE_GENERATE: "image generate",
    MEDIA_OPERATION_IMAGE_EDIT: "image edit",
    MEDIA_OPERATION_IMAGE_VARIATION: "image variation",
    MEDIA_OPERATION_SPEECH: "speech",
    MEDIA_OPERATION_TRANSCRIBE: "transcribe",
    MEDIA_OPERATION_TRANSLATE: "translate",
    MEDIA_OPERATION_VIDEO_CREATE: "video create",
    MEDIA_OPERATION_VIDEO_EDIT: "video edit",
    MEDIA_OPERATION_VIDEO_RETRIEVE: "video status",
    MEDIA_OPERATION_VIDEO_CONTENT: "video content",
    MEDIA_OPERATION_VIDEO_DELETE: "video delete",
    MEDIA_OPERATION_OCR: "ocr",
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
    label = OPERATION_LABELS.get(surface.operation, surface.operation)
    if surface.only_without_url:
        # 7.95.0: asked only when the host's status answer has no file URL.
        label = f"{label} (when no URL)"
    return {
        "operation": surface.operation,
        "label": label,
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


def request_schema_inputs(
    services: ApiServices,
    learned: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    live: LiveCatalogue | None = None,
    litellm: LiteLLMCatalogue | None = None,
) -> MediaSchemaInputs:
    """Bind a rail model's schema inputs from the runtime (7.98.0).

    ``learned`` is taken by the caller ON the event loop (the facts' store is
    written there); the catalogue snapshot and the operator's file are read
    here, so call this on a worker.
    """

    return schema_inputs(
        learned,
        services.requests.cached_prefixed_model_infos(),
        operator=load_media_request_fields(),
        refreshed_at=services.admin.catalogue_refresh_status().get("last_refreshed_at"),
        live=live,
        litellm=litellm,
    )


def ref_capability_profile(
    model_ref: str,
    descriptor: ProviderDescriptor | None,
    inputs: MediaSchemaInputs,
    *,
    models_dev_entry: Mapping[str, Any] | None = None,
    models_dev_as_of: str | None = None,
) -> MediaCapabilityProfile:
    """Resolve one rail model's :class:`MediaCapabilityProfile` from bound inputs."""

    provider_id = parse_provider_type(model_ref)
    model_id = parse_model_name(model_ref)
    live_entry = None if inputs.live is None else inputs.live(provider_id, model_id)
    litellm_entry = (
        None if inputs.litellm is None else inputs.litellm(provider_id, model_id)
    )
    return resolve_media_capability_profile(
        model_ref,
        MediaCapabilityInputs(
            provider_info=inputs.records.get(model_ref),
            surfaces=() if descriptor is None else descriptor.media_surfaces,
            openrouter_live=live_entry,
            models_dev_entry=models_dev_entry,
            litellm_entry=litellm_entry,
            learned_facts=inputs.learned.get(model_ref, ()),
            provider_list_as_of=inputs.records_as_of,
            openrouter_live_as_of=(
                None if inputs.live is None else inputs.live.fetched_at
            ),
            models_dev_as_of=models_dev_as_of,
        ),
    )


def media_capability_summary(profile: MediaCapabilityProfile) -> dict[str, Any]:
    """Serialize a :class:`MediaCapabilityProfile` for admin diagnostics."""

    return {
        "operations": sorted(profile.operations),
        "sync_mode": profile.sync_mode,
        "accepts_encodings": sorted(profile.accepts_encodings),
        "supports_mask": profile.supports_mask,
        "max_input_images": profile.max_input_images,
        "accepted_sizes": (
            None if profile.accepted_sizes is None else list(profile.accepted_sizes)
        ),
        "accepted_aspect_ratios": (
            None
            if profile.accepted_aspect_ratios is None
            else list(profile.accepted_aspect_ratios)
        ),
        "accepted_resolutions": (
            None
            if profile.accepted_resolutions is None
            else list(profile.accepted_resolutions)
        ),
        "accepted_durations": (
            None
            if profile.accepted_durations is None
            else list(profile.accepted_durations)
        ),
        "accepted_formats": (
            None if profile.accepted_formats is None else list(profile.accepted_formats)
        ),
        "accepted_voices": (
            None if profile.accepted_voices is None else list(profile.accepted_voices)
        ),
        "supports_streaming": profile.supports_streaming,
        "supported_generation_methods": sorted(profile.supported_generation_methods),
        "supported_parameters": sorted(profile.supported_parameters),
        "required_params": sorted(profile.required_params),
        "refused_params": sorted(profile.refused_params),
        "field_provenance": {
            key: {"source": source, "as_of": as_of}
            for key, (source, as_of) in sorted(profile.field_provenance.items())
        },
    }


#: Learned fact kinds surfaced on the Media Models inspector.
MEDIA_FACT_KINDS: frozenset[str] = frozenset(
    {
        "media_param_required",
        "media_param_refused",
        "media_endpoint_surface",
        "media_endpoint_refused",
        "media_param_enum",
        "media_encoding_refused",
    }
)


def _serialize_ladder_candidate(candidate: MediaEndpointCandidate) -> dict[str, Any]:
    return {
        "surface_key": surface_key(candidate.surface),
        "operation": candidate.surface.operation,
        "shape": candidate.surface.shape,
        "path": candidate.surface.path,
        "encoding": candidate.surface.encoding,
        "stream": candidate.surface.stream,
        "source": candidate.source,
        "detail": candidate.detail,
        "guessed": candidate.guessed,
        "refused": candidate.refused,
    }


def serialize_media_endpoint_ladder(
    ladder: ResolvedMediaEndpointLadder,
) -> dict[str, Any]:
    """Serialize a :class:`ResolvedMediaEndpointLadder` for the Admin UI."""

    return {
        "operation": ladder.operation,
        "winner": (
            None
            if ladder.winner is None
            else _serialize_ladder_candidate(ladder.winner)
        ),
        "candidates": [
            _serialize_ladder_candidate(candidate) for candidate in ladder.candidates
        ],
    }


def _media_learned_facts_for_row(
    schema: MediaSchemaInputs,
    provider_id: str,
    model_ref: str,
) -> tuple[list[dict[str, Any]], list[Mapping[str, Any]]]:
    """Return active ``FACT_MEDIA_*`` rows matching ``(provider_id, model_id | '*')``."""

    raw_facts: list[Mapping[str, Any]] = []
    seen_keys: set[tuple[str, str, str, str]] = set()
    for lookup_ref in (model_ref, f"{provider_id}/*"):
        for fact in schema.learned.get(lookup_ref, ()):
            fact_kind = str(fact.get("fact_kind") or "").strip()
            if fact_kind not in MEDIA_FACT_KINDS or fact.get("retired"):
                continue
            key = (
                str(fact.get("provider_id") or provider_id),
                str(fact.get("model_id") or ""),
                fact_kind,
                str(fact.get("detail") or ""),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            raw_facts.append(fact)

    serialized = [
        {
            "provider_id": str(fact.get("provider_id") or provider_id),
            "model_id": str(fact.get("model_id") or ""),
            "fact_kind": str(fact.get("fact_kind") or ""),
            "fact_label": FACT_KIND_LABELS.get(
                str(fact.get("fact_kind") or ""),
                str(fact.get("fact_kind") or ""),
            ),
            "detail": str(fact.get("detail") or ""),
            "value": fact.get("value"),
            "evidence": str(fact.get("evidence") or ""),
            "source": str(fact.get("source") or ""),
            "learned_at": fact.get("learned_at"),
            "last_confirmed_at": fact.get("last_confirmed_at"),
            "hits": fact.get("hits", 1),
            "stale": bool(fact.get("stale", False)),
        }
        for fact in raw_facts
    ]
    return serialized, raw_facts


def _media_overrides_for_row(
    overrides_doc: MediaOverridesDocument,
    model_ref: str,
    provider_id: str,
    operations: Sequence[str],
) -> dict[str, dict[str, Any]]:
    raw_model = overrides_doc.for_model(model_ref)
    raw_provider = overrides_doc.for_provider(provider_id)
    all_ops = list(
        dict.fromkeys([*operations, *raw_model.keys(), *raw_provider.keys()])
    )
    effective: dict[str, dict[str, Any]] = {}
    for op in all_ops:
        merged = effective_media_override(overrides_doc, model_ref, op)
        if not merged.is_empty:
            effective[op] = merged.as_row()
    return {
        "effective": effective,
        "model": {op: ov.as_row() for op, ov in raw_model.items()},
        "provider": {op: ov.as_row() for op, ov in raw_provider.items()},
    }


def ref_request_schemas(
    model_ref: str,
    descriptor: ProviderDescriptor | None,
    rails: Iterable[MediaRail],
    inputs: MediaSchemaInputs,
) -> dict[str, dict[str, Any]]:
    """One rail model's request schemas, for the operations its rails route.

    Only the operations its provider declares an endpoint for: the rail steps
    over it uncharged for the rest, so there is no request to describe.
    """

    return rail_model_schemas(
        model_ref,
        None if descriptor is None else descriptor.media_surfaces,
        rails,
        inputs,
    )


def media_models_payload(
    settings: Settings,
    bench: MediaBenchReadout | None = None,
    *,
    kind_modalities: ModalitiesLookup | None = None,
    kind_words: KindWordsLookup | None = None,
    live: LiveCatalogue | None = None,
    litellm: LiteLLMCatalogue | None = None,
    schema: MediaSchemaInputs | None = None,
) -> dict[str, Any]:
    """Everything the Models page's media section renders. Synchronous.

    Run through ``asyncio.to_thread``: the models.dev ladder may build its
    index from the 4.9 MB cache on the first call after a refresh. ``bench``
    is the readout :func:`media_bench_readout` took on the event loop; without
    one it is taken here, on the caller's thread, which is right only for a
    caller that is on the event loop itself.

    ``kind_modalities``/``kind_words`` are the request runtime's kind ladder
    (the provider's own list first, 7.80.0), which the route passes so a row's
    kind here is the one every model list acts on. The lookups only read the
    provider records' dicts, which are replaced and never mutated in place, so
    running them on the worker thread is safe. Without them the kind is read
    from models.dev alone. ``live`` is OpenRouter's live list (7.84.0), bound
    once on the loop; its index is built once and never mutated. ``litellm``
    is LiteLLM's map (7.85.0), present only while LiteLLM pricing is on: the
    same two kind rungs every model list reads.

    ``schema`` (7.98.0) is what each row's ``request_schema`` -- its request
    fields by operation -- is assembled from; without one, nothing learned
    and no catalogue record are known, and the operator's file is read here.
    The key is added after every key a row had before, so each of those is
    exactly what it was.
    """

    if bench is None:
        bench = media_bench_readout(settings)
    if schema is None:
        schema = schema_inputs({}, (), operator=load_media_request_fields())
    overrides_doc = current_media_overrides()
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
    # Each row's stated kind (7.78.2), from the same answer every model list
    # uses, so a chat model someone saved on the Image rail is marked here
    # rather than looking like any other image model.
    rail_placements = media_rail_placements(settings)
    if kind_modalities is None:
        kind_modalities = declared_modalities_lookup()
    kinds: dict[str, ModelKind] = {}
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
                kinds[ref] = declared_model_kind(
                    ref, rail_placements, kind_modalities, kind_words, live, litellm
                )
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
                    "modalities": media_output_modalities(
                        provider_id, model_id, kind_modalities, live
                    ),
                    "kind": model_kind_payload(kinds[ref]),
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
                    # ``None`` when the kind is not known: a mismatch is only
                    # ever claimed against a stated kind.
                    "kind_matches": kinds[ref].states(rail.value),
                }
            )

    # 7.98.0: each row's request fields, by operation -- one more key after
    # every key the row already had. Shown only; nothing routes on it.
    for ref, row in rows.items():
        placed: list[MediaRail] = []
        for placement in placements[ref]:
            rail = MediaRail(placement["rail"])
            if rail not in placed:
                placed.append(rail)
        descriptor = descriptors.get(row["provider_id"])
        row["request_schema"] = ref_request_schemas(ref, descriptor, placed, schema)
        # 8.66.0: endpoint ladder, capability summary, learned media facts, and overrides.
        surfaces = () if descriptor is None else descriptor.media_surfaces
        cap_profile = ref_capability_profile(ref, descriptor, schema)
        serialized_facts, raw_facts = _media_learned_facts_for_row(
            schema, row["provider_id"], ref
        )
        placed_ops: list[str] = []
        for rail in placed:
            for op in RAIL_OPERATIONS[rail]:
                if op not in placed_ops:
                    placed_ops.append(op)
        ladders: dict[str, dict[str, Any]] = {}
        for op in placed_ops:
            ladder = resolve_media_endpoint_ladder(
                ref,
                op,
                surfaces=surfaces,
                capability_profile=cap_profile,
                overrides=overrides_doc,
                learned_facts=raw_facts,
            )
            if ladder.candidates:
                ladders[op] = serialize_media_endpoint_ladder(ladder)
        row["capability"] = media_capability_summary(cap_profile)
        row["endpoint_ladders"] = ladders
        row["learned_facts"] = serialized_facts
        row["overrides"] = _media_overrides_for_row(
            overrides_doc,
            ref,
            row["provider_id"],
            list(ladders) or placed_ops,
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


def media_request_catalogue(
    settings: Settings,
    schema: MediaSchemaInputs,
    endpoints: Mapping[str, str],
) -> dict[str, Any]:
    """``GET /v1/media/models``: every rail model and the fields it takes.

    User decisions 1 and 2 of 2026-10-16: one entry per model on any media
    rail, each with its request schema per operation and the endpoint that
    operation is sent to; and, per rail and operation, which of the rail's
    models accept, need, refuse or say nothing about each field -- never only
    the primary, never only what they all share. ``endpoints`` maps an
    operation to its client path (``api.wire_surfaces``). Synchronous; run it
    on a worker.
    """

    descriptors, disabled = _all_descriptors()
    entries: dict[str, dict[str, Any]] = {}
    rails_placed: dict[str, list[MediaRail]] = {}
    rail_orders: list[tuple[MediaRail, list[str]]] = []
    for rail in MediaRail:
        names = RAIL_SETTINGS[rail]
        primary = str(getattr(settings, names.model_attr, "") or "").strip()
        paused = frozenset(
            parse_model_ref_list(str(getattr(settings, names.paused_attr, "") or ""))
        )
        order: list[str] = []
        fallback = 0
        for index, ref in enumerate(rail_refs(settings, rail)):
            if index == 0 and ref == primary:
                position = "primary"
            else:
                fallback += 1
                position = f"fallback {fallback}"
            provider_id = parse_provider_type(ref)
            if ref not in entries:
                descriptor = descriptors.get(provider_id)
                entries[ref] = {
                    "id": ref,
                    "object": "media.model",
                    "provider": provider_id,
                    "provider_state": (
                        "unknown"
                        if descriptor is None
                        else "disabled"
                        if provider_id in disabled
                        else "known"
                    ),
                    "rails": [],
                }
                rails_placed[ref] = []
            entries[ref]["rails"].append(
                {
                    "rail": rail.value,
                    "position": position,
                    "paused": ref in paused,
                }
            )
            if rail not in rails_placed[ref]:
                rails_placed[ref].append(rail)
            if ref not in order:
                order.append(ref)
        rail_orders.append((rail, order))

    schemas: dict[str, dict[str, dict[str, Any]]] = {}
    for ref, entry in entries.items():
        schemas[ref] = ref_request_schemas(
            ref,
            descriptors.get(entry["provider"]),
            rails_placed[ref],
            schema,
        )
        entry["operations"] = {
            operation: {
                "endpoint": endpoints.get(operation),
                "request_schema": request,
            }
            for operation, request in schemas[ref].items()
        }

    rails: list[dict[str, Any]] = []
    for rail, order in rail_orders:
        on_rail = [(ref, schemas[ref]) for ref in order]
        rails.append(
            {
                "rail": rail.value,
                "label": RAIL_SETTINGS[rail].label,
                "model_env": RAIL_SETTINGS[rail].model_env,
                "models": order,
                "operations": {
                    operation: {
                        "endpoint": endpoints.get(operation),
                        **field_acceptance(operation, on_rail),
                    }
                    for operation in RAIL_OPERATIONS[rail]
                },
            }
        )
    return {"object": "list", "data": list(entries.values()), "rails": rails}


@router.get("/admin/api/media/models")
async def media_models(
    request: Request,
    settings: Settings = Depends(get_settings),
    services: ApiServices = Depends(get_services),
):
    """The Models page's media rows and the providers that can serve them."""

    require_loopback_admin(request)
    # The bench here, on the loop; the rest on a worker. See
    # ``media_bench_readout`` for why the split is where it is.
    bench = media_bench_readout(settings)
    # The learned facts are the loop's too (their store is written on it),
    # so they are copied here and only the copy goes to the worker.
    learned = services.admin.learned_facts_by_model()
    kind_modalities = services.requests.model_modalities_lookup()
    kind_words = services.requests.model_kind_words_lookup()
    live = services.requests.openrouter_live_catalogue()
    litellm = services.requests.litellm_model_catalogue()

    def build() -> dict[str, Any]:
        return media_models_payload(
            settings,
            bench,
            kind_modalities=kind_modalities,
            kind_words=kind_words,
            live=live,
            litellm=litellm,
            schema=request_schema_inputs(services, learned),
        )

    return await asyncio.to_thread(build)


class MediaRequestFieldPayload(BaseModel):
    """One statement the operator types on a media row (7.98.0, decision 5)."""

    model_config = ConfigDict(populate_by_name=True)

    model_ref: str = Field(alias="modelRef")
    operation: str
    field: str
    #: ``accepted`` / ``required`` / ``refused``; ignored by ``remove``.
    state: str = ""
    values: list[str | int | float | bool] | None = None
    type: str | None = None


def _rail_ref_or_404(settings: Settings, model_ref: str) -> str:
    """The ref as a rail names it; a statement is only about a rail model."""

    wanted = model_ref.strip()
    for rail in MediaRail:
        if wanted in rail_refs(settings, rail):
            return wanted
    raise HTTPException(
        status_code=404, detail=f"{wanted or 'That model'} is on no media rail"
    )


@router.post("/admin/api/media/request-fields")
async def set_media_request_field(
    payload: MediaRequestFieldPayload,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """Save what the operator says about one field of one rail model.

    Shown in that model's request schema and on ``GET /v1/media/models``;
    never sent, never checked against a request (user decision 6).
    """

    require_loopback_admin(request)
    model_ref = _rail_ref_or_404(settings, payload.model_ref)
    operation = payload.operation.strip()
    field_name = payload.field.strip()
    state = payload.state.strip()
    field_type = (payload.type or "").strip() or None
    values = payload.values
    if values is not None:
        values = [
            value.strip() if isinstance(value, str) else value for value in values
        ]
        values = [value for value in values if value != ""] or None
    problem = statement_problem(operation, field_name, state, values, field_type)
    if problem is not None:
        raise HTTPException(status_code=400, detail=problem)
    statement = OperatorFieldStatement(
        state=state,
        values=None if values is None else tuple(values),
        type=field_type,
        since=date.today().isoformat(),
    )

    def save() -> None:
        updated = with_statement(
            load_media_request_fields(), model_ref, operation, field_name, statement
        )
        save_media_request_fields(updated)

    await asyncio.to_thread(save)
    return {
        "saved": True,
        "model_ref": model_ref,
        "operation": operation,
        "field": field_name,
        "statement": statement.as_row(),
    }


@router.post("/admin/api/media/request-fields/remove")
async def remove_media_request_field(
    payload: MediaRequestFieldPayload,
    request: Request,
):
    """Remove one of the operator's statements; the rungs below answer again."""

    require_loopback_admin(request)
    model_ref = payload.model_ref.strip()
    operation = payload.operation.strip()
    field_name = payload.field.strip()

    def remove() -> bool:
        updated, removed = without_statement(
            load_media_request_fields(), model_ref, operation, field_name
        )
        if removed:
            save_media_request_fields(updated)
        return removed

    removed = await asyncio.to_thread(remove)
    return {
        "removed": removed,
        "model_ref": model_ref,
        "operation": operation,
        "field": field_name,
    }


class MediaOverridePayload(BaseModel):
    """Operator routing and parameter overrides for one media operation (8.66.0)."""

    model_config = ConfigDict(populate_by_name=True)

    model_ref: str = Field(default="", alias="modelRef")
    provider_id: str = Field(default="", alias="providerId")
    scope: str = "model"
    operation: str
    preferred_surface: str | None = Field(default=None, alias="preferredSurface")
    pin_surface: bool = Field(default=False, alias="pinSurface")
    surface: str | None = None
    param_defaults: dict[str, Any] = Field(default_factory=dict, alias="paramDefaults")
    unset_params: list[str] = Field(default_factory=list, alias="unsetParams")


class MediaOverrideRemovePayload(BaseModel):
    """Remove one provider or model override for a media operation (8.66.0)."""

    model_config = ConfigDict(populate_by_name=True)

    model_ref: str = Field(default="", alias="modelRef")
    provider_id: str = Field(default="", alias="providerId")
    scope: str = "model"
    operation: str


def _resolve_override_target(
    settings: Settings,
    *,
    scope: str,
    model_ref: str,
    provider_id: str,
    require_configured_model: bool,
) -> tuple[str, str, ProviderDescriptor | None]:
    clean_scope = scope.strip().lower()
    if clean_scope not in {"model", "provider"}:
        raise HTTPException(
            status_code=400, detail="scope must be 'model' or 'provider'"
        )
    descriptors, _disabled = _all_descriptors()
    if clean_scope == "model":
        cleaned_ref = (
            _rail_ref_or_404(settings, model_ref)
            if require_configured_model
            else model_ref.strip()
        )
        if not cleaned_ref or "/" not in cleaned_ref:
            raise HTTPException(
                status_code=400, detail="model_ref must be a provider/model ref"
            )
        prov = parse_provider_type(cleaned_ref)
        return cleaned_ref, prov, descriptors.get(prov)
    prov = (provider_id.strip() or parse_provider_type(model_ref)).strip()
    if not prov:
        raise HTTPException(status_code=400, detail="provider_id must not be empty")
    descriptor = descriptors.get(prov)
    if require_configured_model and descriptor is None:
        raise HTTPException(status_code=404, detail=f"Unknown provider: {prov}")
    return prov, prov, descriptor


@router.post("/admin/api/media/overrides")
async def set_media_override(
    payload: MediaOverridePayload,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """Save an operator media endpoint/parameter override in ``media_overrides.json``."""

    require_loopback_admin(request)
    target, provider_id, descriptor = _resolve_override_target(
        settings,
        scope=payload.scope,
        model_ref=payload.model_ref,
        provider_id=payload.provider_id,
        require_configured_model=True,
    )
    operation = payload.operation.strip()
    if operation not in MEDIA_OPERATIONS:
        raise HTTPException(
            status_code=400, detail=f"Unknown media operation: {operation!r}"
        )

    surfaces = () if descriptor is None else descriptor.media_surfaces
    effective = effective_surfaces_for(surfaces, operation)
    allowed_keys: set[str] = set()
    for surf in effective:
        allowed_keys.add(surface_key(surf))
        allowed_keys.add(surf.shape)
        if surf.surface_id:
            allowed_keys.add(surf.surface_id)

    clean_pref = (payload.preferred_surface or "").strip() or None
    clean_surface = (payload.surface or "").strip() or None
    for label, candidate_key in (
        ("preferred_surface", clean_pref),
        ("surface", clean_surface),
    ):
        if candidate_key is not None and candidate_key not in allowed_keys:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{provider_id} does not declare {label}={candidate_key!r} for "
                    f"{operation}. Allowed: {sorted(allowed_keys)}"
                ),
            )

    pin_surface = bool(payload.pin_surface)
    if pin_surface and clean_pref is None and clean_surface is None:
        raise HTTPException(
            status_code=400,
            detail="pin_surface requires preferred_surface or surface",
        )

    clean_defaults: dict[str, Any] = {}
    for raw_key, value in (payload.param_defaults or {}).items():
        key = str(raw_key).strip()
        if not is_valid_media_override_field(key):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid param_defaults field name: {raw_key!r}",
            )
        clean_defaults[key] = value

    clean_unset: list[str] = []
    for raw_name in payload.unset_params or ():
        name = str(raw_name).strip()
        if not is_valid_media_override_field(name):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid unset_params field name: {raw_name!r}",
            )
        if name not in clean_unset:
            clean_unset.append(name)

    override = MediaOperationOverride(
        preferred_surface=clean_pref,
        pin_surface=pin_surface,
        surface=clean_surface,
        param_defaults=clean_defaults,
        unset_params=tuple(clean_unset),
    )

    def save() -> None:
        updated = with_media_override(
            load_media_overrides(), target, operation, override
        )
        save_media_overrides(updated)

    await asyncio.to_thread(save)
    return {
        "saved": True,
        "scope": payload.scope.strip().lower(),
        "target": target,
        "operation": operation,
        "override": override.as_row(),
    }


@router.post("/admin/api/media/overrides/remove")
async def remove_media_override(
    payload: MediaOverrideRemovePayload,
    request: Request,
    settings: Settings = Depends(get_settings),
):
    """Remove an operator media override from ``media_overrides.json``."""

    require_loopback_admin(request)
    target, _provider_id, _descriptor = _resolve_override_target(
        settings,
        scope=payload.scope,
        model_ref=payload.model_ref,
        provider_id=payload.provider_id,
        require_configured_model=False,
    )
    operation = payload.operation.strip()
    if not operation:
        raise HTTPException(status_code=400, detail="operation must not be empty")

    def remove() -> bool:
        updated, removed = without_media_override(
            load_media_overrides(), target, operation
        )
        if removed:
            save_media_overrides(updated)
        return removed

    removed = await asyncio.to_thread(remove)
    return {
        "removed": removed,
        "scope": payload.scope.strip().lower(),
        "target": target,
        "operation": operation,
    }


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
