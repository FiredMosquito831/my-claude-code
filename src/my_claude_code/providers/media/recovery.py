"""Intra-attempt media recovery ladder and learned-fact preflight (8.62.0).

Strictly ``IV.6``-compliant: zero model-name branching or vendor literals.
Preserves ``test_media_learned_params.py`` semantics under default
``MEDIA_PARAM_RECOVERY_MODE="lossless"``: optional fields such as
``image_config`` are never stripped unless ``mode="strip_optional"``.
"""

import dataclasses
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from my_claude_code.application.media.endpoint_ladder import (
    ResolvedMediaEndpointLadder,
)
from my_claude_code.application.media.request import (
    MediaAttempt,
    MediaRequest,
    MediaUpload,
)
from my_claude_code.config.media_overrides import (
    MediaOperationOverride,
    MediaOverridesDocument,
    effective_media_override,
)
from my_claude_code.config.media_surfaces import (
    MEDIA_ENCODING_JSON,
    MEDIA_OPERATION_IMAGE_EDIT,
    MediaSurface,
    surface_key,
)
from my_claude_code.providers.recovery import (
    FACT_MEDIA_ENCODING_REFUSED,
    FACT_MEDIA_ENDPOINT_REFUSED,
    FACT_MEDIA_ENDPOINT_SURFACE,
    FACT_MEDIA_PARAM_ENUM,
    FACT_MEDIA_PARAM_REFUSED,
    PROVIDER_WIDE_MODEL_ID,
    SOURCE_REJECTION,
    learned_fact_store,
)

from .param_refusal import (
    PASSTHROUGH_SHAPES,
    REFUSED,
    is_media_encoding_refusal,
    is_media_surface_shaped_failure,
    param_refusals,
    parse_param_enum_refusal,
)
from .translation import (
    ASPECT_TO_DEFAULT_SIZE,
    EXACT_SIZE_TO_ASPECT,
    UPLOAD_TRANSLATABLE_OPERATIONS,
    has_decodable_data_uris,
    translate_size_and_aspect,
)

#: Lossless strippable parameter defaults (e.g. ``gpt-image-1`` emits
#: ``b64_json`` natively and rejects ``response_format="b64_json"``).
LOSSLESS_STRIPPABLE_PARAMS: frozenset[str] = frozenset({"response_format"})

#: Optional parameters eligible for stripping under ``strip_optional`` mode.
OPTIONAL_STRIPPABLE_PARAMS: frozenset[str] = frozenset(
    {
        "response_format",
        "style",
        "quality",
        "n",
        "partial_images",
        "image_config",
        "user",
        "stream_options",
        "negative_prompt",
        "moderation",
        "background",
        "output_compression",
    }
)

#: Core request inputs that are NEVER stripped in any recovery mode.
CORE_PROTECTED_PARAMS: frozenset[str] = frozenset(
    {
        "model",
        "prompt",
        "input",
        "file",
        "image",
        "images",
        "document",
        "mask",
    }
)


@dataclass(frozen=True, slots=True)
class MediaRecoveryStep:
    """One intra-attempt recovery action to try on the same leaf."""

    kind: str  # "endpoint" | "encoding" | "param"
    surface: MediaSurface
    body: dict[str, Any]
    encoding_override: str | None = None
    adaptations: tuple[str, ...] = ()
    learned_facts: tuple[tuple[str, Any, str, str], ...] = ()


def _fact_attr_or_key(fact: object, name: str, default: Any = None) -> Any:
    if isinstance(fact, Mapping):
        return fact.get(name, default)
    return getattr(fact, name, default)


def _is_fact_stale(fact: object, now: datetime) -> bool:
    if isinstance(fact, Mapping):
        return bool(fact.get("retired")) or bool(fact.get("stale"))
    if bool(getattr(fact, "retired", False)):
        return True
    checker = getattr(fact, "is_stale", None)
    if callable(checker):
        return bool(checker(now))
    return False


def _collect_media_param_facts(
    learned_facts: Sequence[Mapping[str, Any] | Any],
    *,
    provider_id: str,
    model_id: str,
    model_ref: str,
    operation: str,
) -> tuple[set[str], dict[str, list[str]], set[str]]:
    """Return ``(refused_fields, enum_map, refused_encodings)`` from fresh facts."""

    now = datetime.now(UTC)
    folded_provider = provider_id.strip().casefold()
    folded_model = model_id.strip().casefold()
    folded_ref = model_ref.strip().casefold()
    op_prefix = f"{operation}:"

    refused_fields: set[str] = set()
    enums: dict[str, list[str]] = {}
    refused_encodings: set[str] = set()

    for fact in learned_facts:
        if _is_fact_stale(fact, now):
            continue
        fact_provider = str(_fact_attr_or_key(fact, "provider_id", "") or "").strip()
        if folded_provider and fact_provider.casefold() != folded_provider:
            continue
        fact_model = str(_fact_attr_or_key(fact, "model_id", "") or "").strip()
        folded_fact_model = fact_model.casefold()
        if fact_model != PROVIDER_WIDE_MODEL_ID and folded_fact_model not in {
            folded_model,
            folded_ref,
        }:
            continue
        kind = str(_fact_attr_or_key(fact, "fact_kind", "") or "").strip()
        detail = str(_fact_attr_or_key(fact, "detail", "") or "").strip()
        if not detail.startswith(op_prefix):
            continue
        item = detail[len(op_prefix) :].strip()
        if not item:
            continue
        value = _fact_attr_or_key(fact, "value", None)
        if kind == FACT_MEDIA_PARAM_REFUSED and value:
            refused_fields.add(item)
        elif kind == FACT_MEDIA_PARAM_ENUM and isinstance(value, Sequence):
            values = [str(entry) for entry in value if isinstance(entry, str)]
            if values:
                enums[item] = values
        elif kind == FACT_MEDIA_ENCODING_REFUSED and value:
            refused_encodings.add(item)

    return refused_fields, enums, refused_encodings


def _sent_fields(body: Mapping[str, Any], uploads: tuple[MediaUpload, ...]) -> set[str]:
    fields = {str(name) for name in body}
    fields.add("model")
    for upload in uploads:
        name = upload.field
        fields.add(name[:-2] if name.endswith("[]") else name)
    return fields


def _surface_supports_request(surface: MediaSurface, request: MediaRequest) -> bool:
    if request.stream and not surface.stream:
        return False
    if (
        request.uploads
        and surface.encoding == MEDIA_ENCODING_JSON
        and request.operation not in UPLOAD_TRANSLATABLE_OPERATIONS
    ):
        return False
    named = request.body.get("response_format")
    if surface.formats is not None and isinstance(named, str) and named:
        return named in surface.formats
    return True


def _nearest_enum_value(current: Any, allowed: Sequence[str]) -> str | None:
    if not allowed:
        return None
    current_str = str(current).strip()
    for candidate in allowed:
        if candidate.casefold() == current_str.casefold():
            return candidate
    try:
        current_num = float(current_str)
    except ValueError:
        current_num = None
    if current_num is not None:
        numeric: list[tuple[float, str]] = []
        for candidate in allowed:
            try:
                numeric.append((float(candidate), candidate))
            except ValueError:
                continue
        if numeric:
            return min(numeric, key=lambda pair: abs(pair[0] - current_num))[1]
    return allowed[0]


def apply_media_preflight(
    attempt: MediaAttempt,
    surface: MediaSurface,
    *,
    mode: str = "lossless",
    approximate_mapping: bool = False,
    learned_facts: Sequence[Mapping[str, Any] | Any] = (),
    overrides: MediaOverridesDocument | MediaOperationOverride | None = None,
) -> tuple[MediaAttempt, tuple[str, ...]]:
    """Apply operator overrides and learned facts before sending ``attempt``.

    Returns ``(updated_attempt, adaptations)``.
    """

    body = dict(attempt.request.body)
    adaptations: list[str] = []

    resolved_override: MediaOperationOverride | None = None
    if isinstance(overrides, MediaOverridesDocument):
        resolved_override = effective_media_override(
            overrides,
            attempt.resolved.provider_model_ref,
            attempt.request.operation,
        )
    elif isinstance(overrides, MediaOperationOverride):
        resolved_override = overrides

    if resolved_override is not None and not resolved_override.is_empty:
        for unset_key in resolved_override.unset_params:
            if unset_key not in CORE_PROTECTED_PARAMS and unset_key in body:
                body.pop(unset_key, None)
                adaptations.append(f"override:unset:{unset_key}")
        for def_key, def_val in resolved_override.param_defaults.items():
            if def_key not in CORE_PROTECTED_PARAMS and def_key not in body:
                body[def_key] = def_val
                adaptations.append(f"override:default:{def_key}")

    if mode != "off" and surface.shape in PASSTHROUGH_SHAPES:
        refused_fields, enums, _ = _collect_media_param_facts(
            learned_facts,
            provider_id=attempt.resolved.provider_id,
            model_id=attempt.resolved.provider_model,
            model_ref=attempt.resolved.provider_model_ref,
            operation=attempt.request.operation,
        )
        if (
            "response_format" in refused_fields
            and body.get("response_format") == "b64_json"
        ):
            body.pop("response_format", None)
            adaptations.append("stripped:response_format(b64_json)")

        if "size" in refused_fields and isinstance(body.get("size"), str):
            raw_size = body["size"].strip().lower()
            if raw_size in EXACT_SIZE_TO_ASPECT or approximate_mapping:
                body, size_adaptations, _ = translate_size_and_aspect(
                    body,
                    target_fields=("aspect_ratio", "resolution"),
                    approximate=approximate_mapping,
                )
                adaptations.extend(size_adaptations)
        elif "aspect_ratio" in refused_fields and isinstance(
            body.get("aspect_ratio"), str
        ):
            raw_aspect = body["aspect_ratio"].strip()
            if raw_aspect in ASPECT_TO_DEFAULT_SIZE or approximate_mapping:
                body, aspect_adaptations, _ = translate_size_and_aspect(
                    body,
                    target_fields=("size",),
                    approximate=approximate_mapping,
                )
                adaptations.extend(aspect_adaptations)

        if mode == "strip_optional":
            for field_name in sorted(refused_fields):
                if (
                    field_name in OPTIONAL_STRIPPABLE_PARAMS
                    and field_name not in CORE_PROTECTED_PARAMS
                    and field_name in body
                ):
                    body.pop(field_name, None)
                    adaptations.append(f"stripped:{field_name}")

            if approximate_mapping:
                for field_name, allowed in enums.items():
                    if field_name in CORE_PROTECTED_PARAMS or field_name not in body:
                        continue
                    current = body[field_name]
                    if str(current) in allowed:
                        continue
                    clamped = _nearest_enum_value(current, allowed)
                    if clamped is not None and clamped != str(current):
                        body[field_name] = (
                            int(clamped)
                            if isinstance(current, int)
                            and not isinstance(current, bool)
                            and clamped.isdigit()
                            else clamped
                        )
                        adaptations.append(
                            f"clamped:{field_name}({current}->{clamped})"
                        )

    if body == attempt.request.body and not adaptations:
        return attempt, ()
    updated_request = dataclasses.replace(attempt.request, body=body)
    return dataclasses.replace(attempt, request=updated_request), tuple(adaptations)


def retry_plan_on_media_failure(
    error: BaseException,
    *,
    surface: MediaSurface,
    candidate_ladder: ResolvedMediaEndpointLadder | None,
    tried_shapes: Iterable[tuple[str, str, str]],
    mode: str,
    approximate_mapping: bool,
    current_body: Mapping[str, Any],
    attempt: MediaAttempt,
    is_pinned: bool = False,
    tried_encodings: Iterable[str] = (),
    tried_param_retry: bool = False,
    sent_encoding: str | None = None,
) -> MediaRecoveryStep | None:
    """Decide whether a failed media HTTP call can recover cleanly on this leaf."""

    if not isinstance(error, httpx.HTTPStatusError):
        return None
    status_code = error.response.status_code
    content = error.response.content
    tried_set = set(tried_shapes)
    tried_enc_set = set(tried_encodings)

    # 1) Wrong-endpoint / wrong-shape ladder step.
    if (
        not is_pinned
        and candidate_ladder is not None
        and len(candidate_ladder.candidates) > 1
        and is_media_surface_shaped_failure(status_code, content)
    ):
        for candidate in candidate_ladder.candidates:
            ident = (
                surface_key(candidate.surface),
                candidate.surface.shape,
                candidate.surface.path,
            )
            if ident in tried_set:
                continue
            if not _surface_supports_request(candidate.surface, attempt.request):
                continue
            evidence = content[:400].decode("utf-8", errors="ignore")
            return MediaRecoveryStep(
                kind="endpoint",
                surface=candidate.surface,
                body=dict(current_body),
                adaptations=(
                    f"endpoint:{surface_key(surface)}->{surface_key(candidate.surface)}",
                ),
                learned_facts=(
                    (
                        FACT_MEDIA_ENDPOINT_REFUSED,
                        True,
                        f"{attempt.request.operation}:{surface_key(surface)}",
                        evidence,
                    ),
                ),
            )

    # 2) Multipart <-> JSON encoding refusal retry.
    refused_encoding = is_media_encoding_refusal(
        status_code, content, sent_encoding=sent_encoding
    )
    if refused_encoding is not None:
        evidence = content[:400].decode("utf-8", errors="ignore")
        if (
            refused_encoding == "multipart"
            and MEDIA_ENCODING_JSON not in tried_enc_set
            and attempt.request.uploads
            and attempt.request.operation in UPLOAD_TRANSLATABLE_OPERATIONS
        ):
            return MediaRecoveryStep(
                kind="encoding",
                surface=surface,
                body=dict(current_body),
                encoding_override=MEDIA_ENCODING_JSON,
                adaptations=("encoding:multipart->json",),
                learned_facts=(
                    (
                        FACT_MEDIA_ENCODING_REFUSED,
                        True,
                        f"{attempt.request.operation}:multipart",
                        evidence,
                    ),
                ),
            )
        if (
            refused_encoding == MEDIA_ENCODING_JSON
            and "multipart" not in tried_enc_set
            and attempt.request.operation == MEDIA_OPERATION_IMAGE_EDIT
            and has_decodable_data_uris(current_body)
        ):
            return MediaRecoveryStep(
                kind="encoding",
                surface=surface,
                body=dict(current_body),
                encoding_override="multipart",
                adaptations=("encoding:json->multipart",),
                learned_facts=(
                    (
                        FACT_MEDIA_ENCODING_REFUSED,
                        True,
                        f"{attempt.request.operation}:json",
                        evidence,
                    ),
                ),
            )

    # 3) Parameter refusal recovery.
    if tried_param_retry or mode == "off" or surface.shape not in PASSTHROUGH_SHAPES:
        return None

    renames_back = {wire: client for client, wire in surface.renames}
    sent = _sent_fields(current_body, attempt.request.uploads)
    refusals = param_refusals(status_code, content, sent=sent, renames=renames_back)
    enum_refusals = parse_param_enum_refusal(
        status_code, content, sent=sent, renames=renames_back
    )
    if not refusals and not enum_refusals:
        return None

    refused_map = {
        refusal.field: refusal.message
        for refusal in refusals
        if refusal.direction == REFUSED
    }

    # 3a) Lossless response_format="b64_json" strip.
    if (
        "response_format" in refused_map
        and current_body.get("response_format") == "b64_json"
    ):
        new_body = dict(current_body)
        new_body.pop("response_format", None)
        return MediaRecoveryStep(
            kind="param",
            surface=surface,
            body=new_body,
            adaptations=("stripped:response_format(b64_json)",),
            learned_facts=(
                (
                    FACT_MEDIA_PARAM_REFUSED,
                    True,
                    f"{attempt.request.operation}:response_format",
                    refused_map["response_format"],
                ),
            ),
        )

    # 3b) Lossless exact size <-> aspect_ratio / resolution bijection.
    if "size" in refused_map and isinstance(current_body.get("size"), str):
        raw_size = current_body["size"].strip().lower()
        if raw_size in EXACT_SIZE_TO_ASPECT or approximate_mapping:
            new_body, size_adaptations, _ = translate_size_and_aspect(
                current_body,
                target_fields=("aspect_ratio", "resolution"),
                approximate=approximate_mapping,
            )
            if size_adaptations:
                return MediaRecoveryStep(
                    kind="param",
                    surface=surface,
                    body=new_body,
                    adaptations=size_adaptations,
                    learned_facts=(
                        (
                            FACT_MEDIA_PARAM_REFUSED,
                            True,
                            f"{attempt.request.operation}:size",
                            refused_map["size"],
                        ),
                    ),
                )

    if "aspect_ratio" in refused_map and isinstance(
        current_body.get("aspect_ratio"), str
    ):
        raw_aspect = current_body["aspect_ratio"].strip()
        if raw_aspect in ASPECT_TO_DEFAULT_SIZE or approximate_mapping:
            new_body, aspect_adaptations, _ = translate_size_and_aspect(
                current_body,
                target_fields=("size",),
                approximate=approximate_mapping,
            )
            if aspect_adaptations:
                return MediaRecoveryStep(
                    kind="param",
                    surface=surface,
                    body=new_body,
                    adaptations=aspect_adaptations,
                    learned_facts=(
                        (
                            FACT_MEDIA_PARAM_REFUSED,
                            True,
                            f"{attempt.request.operation}:aspect_ratio",
                            refused_map["aspect_ratio"],
                        ),
                    ),
                )

    if mode != "strip_optional":
        return None

    # 3c) Opt-in strip_optional: strip refused optional fields (never core inputs).
    strippable = [
        (field, msg)
        for field, msg in refused_map.items()
        if field in OPTIONAL_STRIPPABLE_PARAMS
        and field not in CORE_PROTECTED_PARAMS
        and field in current_body
    ]
    if strippable:
        new_body = dict(current_body)
        adaptations: list[str] = []
        facts: list[tuple[str, Any, str, str]] = []
        for field, msg in strippable:
            new_body.pop(field, None)
            adaptations.append(f"stripped:{field}")
            facts.append(
                (
                    FACT_MEDIA_PARAM_REFUSED,
                    True,
                    f"{attempt.request.operation}:{field}",
                    msg,
                )
            )
        return MediaRecoveryStep(
            kind="param",
            surface=surface,
            body=new_body,
            adaptations=tuple(adaptations),
            learned_facts=tuple(facts),
        )

    # 3d) Opt-in strip_optional + approximate_mapping: clamp stated enum refusal.
    if approximate_mapping and enum_refusals:
        for enum_refusal in enum_refusals:
            if (
                enum_refusal.field in CORE_PROTECTED_PARAMS
                or enum_refusal.field not in current_body
            ):
                continue
            current = current_body[enum_refusal.field]
            clamped = _nearest_enum_value(current, enum_refusal.allowed)
            if clamped is not None and str(clamped) != str(current):
                new_body = dict(current_body)
                new_body[enum_refusal.field] = (
                    int(clamped)
                    if isinstance(current, int)
                    and not isinstance(current, bool)
                    and clamped.isdigit()
                    else clamped
                )
                return MediaRecoveryStep(
                    kind="param",
                    surface=surface,
                    body=new_body,
                    adaptations=(
                        f"clamped:{enum_refusal.field}({current}->{clamped})",
                    ),
                    learned_facts=(
                        (
                            FACT_MEDIA_PARAM_ENUM,
                            list(enum_refusal.allowed),
                            f"{attempt.request.operation}:{enum_refusal.field}",
                            enum_refusal.message,
                        ),
                    ),
                )

    return None


def persist_media_recovery_facts(
    *,
    provider_id: str,
    model_id: str,
    operation: str,
    active_surface: MediaSurface,
    switched_endpoint: bool,
    pending_facts: Sequence[tuple[str, Any, str, str]],
) -> None:
    """Persist endpoint/encoding/parameter facts once a recovered attempt answers 200."""

    if not switched_endpoint and not pending_facts:
        return
    store = learned_fact_store()
    for fact_kind, value, detail, evidence in pending_facts:
        store.record(
            provider_id,
            model_id,
            fact_kind,
            value,
            source=SOURCE_REJECTION,
            detail=detail,
            evidence=evidence,
        )
    if switched_endpoint:
        key = surface_key(active_surface)
        store.record(
            provider_id,
            model_id,
            FACT_MEDIA_ENDPOINT_SURFACE,
            [key],
            source=SOURCE_REJECTION,
            detail=operation,
            evidence=f"recovered to {key}",
        )


__all__ = [
    "CORE_PROTECTED_PARAMS",
    "LOSSLESS_STRIPPABLE_PARAMS",
    "OPTIONAL_STRIPPABLE_PARAMS",
    "MediaRecoveryStep",
    "apply_media_preflight",
    "persist_media_recovery_facts",
    "retry_plan_on_media_failure",
]
