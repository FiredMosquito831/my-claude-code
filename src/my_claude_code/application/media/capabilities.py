"""Media model capability profile resolution across metadata rungs (8.60.0).

Resolves a unified :class:`MediaCapabilityProfile` per ``(model_ref)`` from:
1. Operator overrides
2. Learned deployment facts
3. Shipped / measured metadata (when supplied)
4. Provider's own ``ProviderModelInfo`` (``declared``, ``supported_parameters``,
   raw ``published_row`` fields such as ``supportedGenerationMethods`` and
   ``capabilities`` flags)
5. OpenRouter's live model list (``LiveModel``)
6. ``models.dev`` entry metadata
7. ``LiteLLM`` model map entry metadata
8. Provider ``MediaSurface`` defaults

Pure: no I/O, no clock unless supplied, imports ``config`` and ``core`` only.
Strictly ``IV.6``-compliant: zero model-name comparisons or vendor literals.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from my_claude_code.application.litellm_model_map import LiteLLMModel
from my_claude_code.application.model_kinds import kind_word_key, modality_word
from my_claude_code.application.model_metadata import (
    DeclaredModalities,
    ProviderModelInfo,
)
from my_claude_code.application.openrouter_live import LiveModel
from my_claude_code.config.media_surfaces import (
    MEDIA_ENCODING_JSON,
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_OCR,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
    MEDIA_OPERATION_VIDEO_CREATE,
    MEDIA_SHAPE_GEMINI_NATIVE_OCR,
    MEDIA_SHAPE_GEMINI_TRANSCRIBE,
    MEDIA_SHAPE_GEMINI_TTS,
    MEDIA_SHAPE_MISTRAL_IMAGE_TOOL,
    MEDIA_SHAPE_MISTRAL_OCR,
    MEDIA_SHAPE_VISION_OCR_CHAT,
    MediaSurface,
)

SOURCE_OPERATOR = "operator"
SOURCE_LEARNED = "learned"
SOURCE_MEASURED = "measured"
SOURCE_PROVIDER_LIST = "provider_list"
SOURCE_OPENROUTER_LIVE = "openrouter_live"
SOURCE_MODELS_DEV = "models_dev"
SOURCE_LITELLM = "litellm"
SOURCE_PROVIDER_DEFAULT = "provider_default"

FACT_MEDIA_PARAM_REQUIRED = "media_param_required"
FACT_MEDIA_PARAM_REFUSED = "media_param_refused"
FACT_MEDIA_PARAM_ENUM = "media_param_enum"
FACT_MEDIA_ENCODING_REFUSED = "media_encoding_refused"


class MediaEndpointSource(StrEnum):
    """Provenance rung identifiers ordered strongest-first."""

    OPERATOR = SOURCE_OPERATOR
    LEARNED = SOURCE_LEARNED
    MEASURED = SOURCE_MEASURED
    PROVIDER_LIST = SOURCE_PROVIDER_LIST
    OPENROUTER_LIVE = SOURCE_OPENROUTER_LIVE
    MODELS_DEV = SOURCE_MODELS_DEV
    LITELLM = SOURCE_LITELLM
    PROVIDER_DEFAULT = SOURCE_PROVIDER_DEFAULT


_CAPABILITY_OPERATIONS: frozenset[str] = frozenset(
    {
        MEDIA_OPERATION_IMAGE_GENERATE,
        MEDIA_OPERATION_IMAGE_EDIT,
        MEDIA_OPERATION_VIDEO_CREATE,
        MEDIA_OPERATION_SPEECH,
        MEDIA_OPERATION_TRANSCRIBE,
        MEDIA_OPERATION_TRANSLATE,
        MEDIA_OPERATION_OCR,
    }
)

_SYNC_OPERATIONS: frozenset[str] = frozenset(
    {
        MEDIA_OPERATION_IMAGE_GENERATE,
        MEDIA_OPERATION_IMAGE_EDIT,
        MEDIA_OPERATION_SPEECH,
        MEDIA_OPERATION_TRANSCRIBE,
        MEDIA_OPERATION_TRANSLATE,
        MEDIA_OPERATION_OCR,
    }
)

_WORD_TO_OPERATIONS: Mapping[str, frozenset[str]] = {
    **dict.fromkeys(
        (
            "image",
            "image-generation",
            "image_generation",
            "images/generations",
            "text-to-image",
            "images",
            "predict",
        ),
        frozenset({MEDIA_OPERATION_IMAGE_GENERATE}),
    ),
    **dict.fromkeys(
        (
            "image_edit",
            "image-edit",
            "image_editing",
            "images/edits",
            "image-to-image",
        ),
        frozenset({MEDIA_OPERATION_IMAGE_EDIT}),
    ),
    **dict.fromkeys(
        (
            "video",
            "openai-video",
            "video_generation",
            "video-generation",
            "videos",
            "text-to-video",
            "image-to-video",
            "predictlongrunning",
        ),
        frozenset({MEDIA_OPERATION_VIDEO_CREATE}),
    ),
    **dict.fromkeys(
        ("speech", "audio_speech", "audio/speech", "text-to-speech", "tts"),
        frozenset({MEDIA_OPERATION_SPEECH}),
    ),
    **dict.fromkeys(
        (
            "transcription",
            "audio_transcription",
            "audio/transcriptions",
            "automatic-speech-recognition",
            "asr",
            "stt",
            "transcribe",
            "realtime_transcription",
            "realtime/transcription_sessions",
        ),
        frozenset({MEDIA_OPERATION_TRANSCRIBE}),
    ),
    **dict.fromkeys(
        (
            "audio/translations",
            "translation",
            "translate",
            "audio_translation",
        ),
        frozenset({MEDIA_OPERATION_TRANSLATE}),
    ),
    **dict.fromkeys(
        ("ocr", "document-ocr", "document_ocr"),
        frozenset({MEDIA_OPERATION_OCR}),
    ),
    **dict.fromkeys(
        ("audio",),
        frozenset({MEDIA_OPERATION_SPEECH, MEDIA_OPERATION_TRANSCRIBE}),
    ),
}

_CAPABILITY_FLAG_TO_OPERATIONS: tuple[tuple[str, str], ...] = (
    ("ocr", MEDIA_OPERATION_OCR),
    ("audio_speech", MEDIA_OPERATION_SPEECH),
    ("audio_transcription", MEDIA_OPERATION_TRANSCRIBE),
    ("realtime_transcription", MEDIA_OPERATION_TRANSCRIBE),
    ("image_generation", MEDIA_OPERATION_IMAGE_GENERATE),
)

_JSON_ONLY_SHAPES: frozenset[str] = frozenset(
    {
        MEDIA_SHAPE_GEMINI_TTS,
        MEDIA_SHAPE_GEMINI_TRANSCRIBE,
        MEDIA_SHAPE_MISTRAL_IMAGE_TOOL,
        MEDIA_SHAPE_MISTRAL_OCR,
        MEDIA_SHAPE_GEMINI_NATIVE_OCR,
        MEDIA_SHAPE_VISION_OCR_CHAT,
    }
)
_MULTIPART_OPERATIONS: frozenset[str] = frozenset(
    {
        MEDIA_OPERATION_TRANSCRIBE,
        MEDIA_OPERATION_TRANSLATE,
        MEDIA_OPERATION_VIDEO_CREATE,
        MEDIA_OPERATION_OCR,
    }
)


@dataclass(frozen=True, slots=True)
class MediaCapabilityInputs:
    """All metadata inputs consulted when resolving one ref's capability profile."""

    provider_info: ProviderModelInfo | None = None
    surfaces: Sequence[MediaSurface] = ()
    openrouter_live: LiveModel | None = None
    models_dev_entry: Mapping[str, Any] | None = None
    litellm_entry: LiteLLMModel | None = None
    learned_facts: Sequence[Mapping[str, Any]] = ()
    operator_overrides: Mapping[str, Any] = field(default_factory=dict)
    provider_list_as_of: str | None = None
    openrouter_live_as_of: str | None = None
    models_dev_as_of: str | None = None
    litellm_as_of: str | None = None


@dataclass(frozen=True, slots=True)
class MediaCapabilityProfile:
    """Resolved media capabilities and field-level provenance for one ref."""

    operations: frozenset[str] = frozenset()
    sync_mode: str = "unknown"
    accepts_encodings: frozenset[str] = frozenset()
    supports_mask: bool | None = None
    max_input_images: int | None = None
    accepted_sizes: tuple[str, ...] | None = None
    accepted_aspect_ratios: tuple[str, ...] | None = None
    accepted_resolutions: tuple[str, ...] | None = None
    accepted_durations: tuple[int | float, ...] | None = None
    accepted_formats: tuple[str, ...] | None = None
    accepted_voices: tuple[str, ...] | None = None
    supports_streaming: bool | None = None
    supported_generation_methods: frozenset[str] = frozenset()
    supported_parameters: frozenset[str] = frozenset()
    required_params: frozenset[str] = frozenset()
    refused_params: frozenset[str] = frozenset()
    field_provenance: Mapping[str, tuple[str, str | None]] = field(default_factory=dict)


def _safe_published_row(info: ProviderModelInfo | None) -> Mapping[str, Any]:
    if info is None or not info.published_row:
        return {}
    try:
        parsed = json.loads(info.published_row)
    except ValueError, TypeError:
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _string_tuple(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return None
    items = [
        entry.strip() for entry in value if isinstance(entry, str) and entry.strip()
    ]
    return tuple(dict.fromkeys(items)) if items else None


def _number_tuple(value: Any) -> tuple[int | float, ...] | None:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return None
    numbers: list[int | float] = []
    for entry in value:
        if isinstance(entry, bool) or not isinstance(entry, int | float):
            continue
        if entry not in numbers:
            numbers.append(entry)
    return tuple(numbers) if numbers else None


def _generation_methods_from_row(row: Mapping[str, Any]) -> frozenset[str]:
    for key in ("supportedGenerationMethods", "supported_generation_methods"):
        raw = row.get(key)
        if isinstance(raw, Sequence) and not isinstance(raw, str | bytes | bytearray):
            cleaned = frozenset(
                item.strip() for item in raw if isinstance(item, str) and item.strip()
            )
            if cleaned:
                return cleaned
    return frozenset()


def _operations_from_modalities(
    modalities: DeclaredModalities | None,
) -> frozenset[str]:
    if modalities is None:
        return frozenset()
    inputs = {modality_word(word) for word in modalities.inputs}
    outputs = {modality_word(word) for word in modalities.outputs}
    ops: set[str] = set()
    if "image" in outputs:
        ops.add(MEDIA_OPERATION_IMAGE_GENERATE)
        if "image" in inputs:
            ops.add(MEDIA_OPERATION_IMAGE_EDIT)
    if "video" in outputs:
        ops.add(MEDIA_OPERATION_VIDEO_CREATE)
    if "audio" in outputs:
        ops.add(MEDIA_OPERATION_SPEECH)
    if "audio" in inputs and "text" in outputs:
        ops.add(MEDIA_OPERATION_TRANSCRIBE)
    if "ocr" in outputs:
        ops.add(MEDIA_OPERATION_OCR)
    return frozenset(ops)


def _operations_from_words(words: Iterable[str] | None) -> frozenset[str]:
    if words is None:
        return frozenset()
    ops: set[str] = set()
    for word in words:
        if not isinstance(word, str):
            continue
        mapped = _WORD_TO_OPERATIONS.get(kind_word_key(word))
        if mapped:
            ops.update(mapped)
    return frozenset(ops)


def _modalities_from_mapping(
    entry: Mapping[str, Any] | None,
) -> DeclaredModalities | None:
    if not isinstance(entry, Mapping):
        return None
    for in_vals, out_vals in (
        (
            (entry.get("architecture") or {}).get("input_modalities")
            if isinstance(entry.get("architecture"), Mapping)
            else None,
            (entry.get("architecture") or {}).get("output_modalities")
            if isinstance(entry.get("architecture"), Mapping)
            else None,
        ),
        (entry.get("input_modalities"), entry.get("output_modalities")),
        (
            (entry.get("modalities") or {}).get("input")
            if isinstance(entry.get("modalities"), Mapping)
            else None,
            (entry.get("modalities") or {}).get("output")
            if isinstance(entry.get("modalities"), Mapping)
            else None,
        ),
    ):
        inputs = _string_tuple(in_vals)
        outputs = _string_tuple(out_vals)
        if inputs is not None and outputs is not None:
            return DeclaredModalities(
                inputs=tuple(word.lower() for word in inputs),
                outputs=tuple(word.lower() for word in outputs),
            )
    return None


def _row_words(row: Mapping[str, Any]) -> tuple[str, ...]:
    words: list[str] = []
    for key in (
        "model_type",
        "type",
        "reported_type",
        "mode",
        "task",
        "sub_type",
        "pipeline_tag",
    ):
        val = row.get(key)
        if isinstance(val, str) and val.strip():
            words.append(val.strip())
    for key in (
        "supported_endpoint_types",
        "supported_endpoints",
        "endpoints",
        "supportedGenerationMethods",
        "supported_generation_methods",
    ):
        seq = _string_tuple(row.get(key))
        if seq:
            words.extend(seq)
    return tuple(words)


def _operations_from_capabilities(row: Mapping[str, Any]) -> frozenset[str]:
    caps = row.get("capabilities")
    if not isinstance(caps, Mapping):
        return frozenset()
    ops: set[str] = set()
    for flag, operation in _CAPABILITY_FLAG_TO_OPERATIONS:
        if caps.get(flag) is True:
            ops.add(operation)
    if caps.get("audio") is True and caps.get("completion_chat") is True:
        ops.add(MEDIA_OPERATION_TRANSCRIBE)
    return frozenset(ops)


def _operations_from_provider_info(
    info: ProviderModelInfo | None,
    row: Mapping[str, Any],
    methods: frozenset[str],
) -> frozenset[str]:
    if info is None and not row:
        return frozenset()
    ops: set[str] = set()
    declared = None if info is None else info.declared
    if declared is not None:
        ops.update(_operations_from_modalities(declared.modalities))
        if declared.model_type is not None:
            ops.update(_operations_from_words((declared.model_type,)))
        ops.update(_operations_from_words(declared.endpoints))
    if row:
        ops.update(_operations_from_modalities(_modalities_from_mapping(row)))
        ops.update(_operations_from_words(_row_words(row)))
        ops.update(_operations_from_capabilities(row))
    ops.update(_operations_from_words(methods))
    return frozenset(ops)


def _operations_from_models_dev(entry: Mapping[str, Any] | None) -> frozenset[str]:
    if not isinstance(entry, Mapping):
        return frozenset()
    ops: set[str] = set(_operations_from_modalities(_modalities_from_mapping(entry)))
    words: list[str] = []
    for key in ("mode", "type"):
        val = entry.get(key)
        if isinstance(val, str) and val.strip():
            words.append(val.strip())
    ops.update(_operations_from_words(words))
    return frozenset(ops)


def _operations_from_litellm(entry: LiteLLMModel | None) -> frozenset[str]:
    if entry is None:
        return frozenset()
    ops: set[str] = set()
    if entry.modalities is not None:
        ops.update(_operations_from_modalities(entry.modalities.value))
    if entry.words is not None:
        ops.update(_operations_from_words(entry.words.value))
    if entry.endpoints is not None:
        ops.update(_operations_from_words(entry.endpoints.value))
    return frozenset(ops)


def _surface_encodings(surface: MediaSurface) -> frozenset[str]:
    if surface.encoding == MEDIA_ENCODING_JSON:
        return frozenset({"json"})
    if surface.shape in _JSON_ONLY_SHAPES:
        return frozenset({"json"})
    if surface.operation == MEDIA_OPERATION_IMAGE_EDIT:
        return frozenset({"multipart", "json"})
    if surface.operation in _MULTIPART_OPERATIONS:
        return frozenset({"multipart"})
    return frozenset({"json"})


def _param_facts(
    learned_facts: Sequence[Mapping[str, Any]],
) -> tuple[frozenset[str], frozenset[str], dict[str, tuple[str, ...]], frozenset[str]]:
    """Extract non-stale learned param constraints and refused encodings."""
    latest: dict[str, tuple[str, str]] = {}
    enums: dict[str, tuple[str, ...]] = {}
    refused_encodings: set[str] = set()
    for row in learned_facts:
        if row.get("stale"):
            continue
        fact_kind = str(row.get("fact_kind") or "")
        detail = str(row.get("detail") or "")
        confirmed = str(row.get("last_confirmed_at") or row.get("learned_at") or "")
        _, sep, field_name = detail.partition(":")
        if fact_kind in {FACT_MEDIA_PARAM_REQUIRED, FACT_MEDIA_PARAM_REFUSED}:
            target = field_name if sep and field_name else detail
            if not target:
                continue
            prev = latest.get(target)
            if prev is None or confirmed > prev[1]:
                latest[target] = (fact_kind, confirmed)
        elif fact_kind == FACT_MEDIA_PARAM_ENUM and sep and field_name:
            vals = _string_tuple(row.get("value"))
            if vals:
                enums[field_name] = vals
        elif fact_kind == FACT_MEDIA_ENCODING_REFUSED:
            encoding_name = field_name if sep and field_name else detail
            if encoding_name:
                refused_encodings.add(encoding_name.strip().lower())
    required = frozenset(
        name for name, (kind, _) in latest.items() if kind == FACT_MEDIA_PARAM_REQUIRED
    )
    refused = frozenset(
        name for name, (kind, _) in latest.items() if kind == FACT_MEDIA_PARAM_REFUSED
    )
    return required, refused, enums, frozenset(refused_encodings)


def _matching_surfaces(
    surfaces: Sequence[MediaSurface], operations: frozenset[str]
) -> tuple[MediaSurface, ...]:
    if not surfaces:
        return ()
    if not operations:
        return tuple(surfaces)
    matched = tuple(s for s in surfaces if s.operation in operations)
    return matched if matched else ()


def resolve_media_capability_profile(
    ref: str,
    inputs: MediaCapabilityInputs,
) -> MediaCapabilityProfile:
    """Resolve one ref's unified :class:`MediaCapabilityProfile` across all rungs."""

    del ref  # IV.6: never branch on the model ref or model name.
    provenance: dict[str, tuple[str, str | None]] = {}
    info = inputs.provider_info
    row = _safe_published_row(info)
    overrides = inputs.operator_overrides or {}
    methods = _generation_methods_from_row(row)
    if methods:
        provenance["supported_generation_methods"] = (
            SOURCE_PROVIDER_LIST,
            inputs.provider_list_as_of,
        )

    # -- supported_parameters -------------------------------------------------
    supported_params: frozenset[str] = frozenset()
    if info is not None and info.supported_parameters is not None:
        supported_params = info.supported_parameters
        provenance["supported_parameters"] = (
            SOURCE_PROVIDER_LIST,
            inputs.provider_list_as_of,
        )
    else:
        row_params = _string_tuple(row.get("supported_parameters"))
        if row_params is not None:
            supported_params = frozenset(row_params)
            provenance["supported_parameters"] = (
                SOURCE_PROVIDER_LIST,
                inputs.provider_list_as_of,
            )
        elif (
            inputs.openrouter_live is not None
            and inputs.openrouter_live.supported_parameters is not None
        ):
            supported_params = inputs.openrouter_live.supported_parameters
            provenance["supported_parameters"] = (
                SOURCE_OPENROUTER_LIVE,
                inputs.openrouter_live_as_of,
            )

    # -- operations ladder (operator -> provider -> openrouter -> models.dev -> litellm -> surfaces)
    operations: frozenset[str] = frozenset()
    override_ops = _string_tuple(overrides.get("operations"))
    if override_ops is not None:
        operations = frozenset(
            op for op in override_ops if op in _CAPABILITY_OPERATIONS
        )
        if operations:
            provenance["operations"] = (SOURCE_OPERATOR, None)

    if not operations:
        provider_ops = _operations_from_provider_info(info, row, methods)
        if provider_ops:
            operations = provider_ops
            provenance["operations"] = (
                SOURCE_PROVIDER_LIST,
                inputs.provider_list_as_of,
            )

    if not operations and inputs.openrouter_live is not None:
        live_ops = _operations_from_modalities(inputs.openrouter_live.modalities)
        if live_ops:
            operations = live_ops
            provenance["operations"] = (
                SOURCE_OPENROUTER_LIVE,
                inputs.openrouter_live_as_of,
            )

    if not operations and inputs.models_dev_entry is not None:
        md_ops = _operations_from_models_dev(inputs.models_dev_entry)
        if md_ops:
            operations = md_ops
            provenance["operations"] = (SOURCE_MODELS_DEV, inputs.models_dev_as_of)

    if not operations and inputs.litellm_entry is not None:
        ll_ops = _operations_from_litellm(inputs.litellm_entry)
        if ll_ops:
            operations = ll_ops
            provenance["operations"] = (SOURCE_LITELLM, inputs.litellm_as_of)

    if not operations and inputs.surfaces:
        surface_ops = frozenset(
            s.operation
            for s in inputs.surfaces
            if s.operation in _CAPABILITY_OPERATIONS
        )
        if surface_ops:
            operations = surface_ops
            provenance["operations"] = (SOURCE_PROVIDER_DEFAULT, None)

    # -- sync_mode ------------------------------------------------------------
    sync_mode = "unknown"
    if isinstance(overrides.get("sync_mode"), str) and overrides["sync_mode"].strip():
        sync_mode = overrides["sync_mode"].strip()
        provenance["sync_mode"] = (SOURCE_OPERATOR, None)
    elif MEDIA_OPERATION_VIDEO_CREATE in operations or any(
        m.lower() == "predictlongrunning" for m in methods
    ):
        sync_mode = "async_job"
        provenance["sync_mode"] = provenance.get(
            "operations", (SOURCE_PROVIDER_DEFAULT, None)
        )
    elif operations & _SYNC_OPERATIONS:
        sync_mode = "sync"
        provenance["sync_mode"] = provenance.get(
            "operations", (SOURCE_PROVIDER_DEFAULT, None)
        )

    matched_surfaces = _matching_surfaces(inputs.surfaces, operations)
    learned_required, learned_refused, learned_enums, refused_encodings = _param_facts(
        inputs.learned_facts
    )

    # -- encodings ------------------------------------------------------------
    accepts_encodings: frozenset[str] = frozenset()
    override_encodings = _string_tuple(overrides.get("accepts_encodings"))
    if override_encodings is not None:
        accepts_encodings = frozenset(enc.lower() for enc in override_encodings)
        provenance["accepts_encodings"] = (SOURCE_OPERATOR, None)
    elif matched_surfaces:
        encs: set[str] = set()
        for surface in matched_surfaces:
            encs.update(_surface_encodings(surface))
        encs.difference_update(refused_encodings)
        accepts_encodings = frozenset(encs)
        if accepts_encodings:
            provenance["accepts_encodings"] = (SOURCE_PROVIDER_DEFAULT, None)

    # -- streaming ------------------------------------------------------------
    supports_streaming: bool | None = None
    if isinstance(overrides.get("supports_streaming"), bool):
        supports_streaming = overrides["supports_streaming"]
        provenance["supports_streaming"] = (SOURCE_OPERATOR, None)
    elif matched_surfaces:
        supports_streaming = any(s.stream for s in matched_surfaces)
        provenance["supports_streaming"] = (SOURCE_PROVIDER_DEFAULT, None)

    # -- formats & voices -----------------------------------------------------
    accepted_formats: tuple[str, ...] | None = _string_tuple(
        overrides.get("accepted_formats")
    )
    if accepted_formats is not None:
        provenance["accepted_formats"] = (SOURCE_OPERATOR, None)
    elif "response_format" in learned_enums:
        accepted_formats = learned_enums["response_format"]
        provenance["accepted_formats"] = (SOURCE_LEARNED, None)
    elif row_formats := _string_tuple(
        row.get("supported_formats") or row.get("formats")
    ):
        accepted_formats = row_formats
        provenance["accepted_formats"] = (
            SOURCE_PROVIDER_LIST,
            inputs.provider_list_as_of,
        )
    else:
        surface_formats = [
            fmt for s in matched_surfaces if s.formats for fmt in s.formats
        ]
        if surface_formats:
            accepted_formats = tuple(dict.fromkeys(surface_formats))
            provenance["accepted_formats"] = (SOURCE_PROVIDER_DEFAULT, None)

    accepted_voices: tuple[str, ...] | None = _string_tuple(
        overrides.get("accepted_voices")
    )
    if accepted_voices is not None:
        provenance["accepted_voices"] = (SOURCE_OPERATOR, None)
    elif "voice" in learned_enums:
        accepted_voices = learned_enums["voice"]
        provenance["accepted_voices"] = (SOURCE_LEARNED, None)
    elif row_voices := _string_tuple(row.get("supported_voices") or row.get("voices")):
        accepted_voices = row_voices
        provenance["accepted_voices"] = (
            SOURCE_PROVIDER_LIST,
            inputs.provider_list_as_of,
        )
    else:
        surface_voices = [
            voice for s in matched_surfaces if s.voices for voice in s.voices
        ]
        if surface_voices:
            accepted_voices = tuple(dict.fromkeys(surface_voices))
            provenance["accepted_voices"] = (SOURCE_PROVIDER_DEFAULT, None)

    # -- sizes, aspect_ratios, resolutions, durations, mask, max_input_images -
    accepted_sizes = _resolve_string_list_field(
        "accepted_sizes",
        overrides,
        learned_enums.get("size"),
        row,
        ("supported_sizes", "sizes"),
        provenance,
        inputs.provider_list_as_of,
    )
    accepted_aspect_ratios = _resolve_string_list_field(
        "accepted_aspect_ratios",
        overrides,
        learned_enums.get("aspect_ratio"),
        row,
        ("supported_aspect_ratios", "aspect_ratios"),
        provenance,
        inputs.provider_list_as_of,
    )
    accepted_resolutions = _resolve_string_list_field(
        "accepted_resolutions",
        overrides,
        learned_enums.get("resolution"),
        row,
        ("supported_resolutions", "resolutions"),
        provenance,
        inputs.provider_list_as_of,
    )

    accepted_durations = _number_tuple(overrides.get("accepted_durations"))
    if accepted_durations is not None:
        provenance["accepted_durations"] = (SOURCE_OPERATOR, None)
    elif row_durations := _number_tuple(
        row.get("supported_durations") or row.get("durations")
    ):
        accepted_durations = row_durations
        provenance["accepted_durations"] = (
            SOURCE_PROVIDER_LIST,
            inputs.provider_list_as_of,
        )

    supports_mask: bool | None = None
    if isinstance(overrides.get("supports_mask"), bool):
        supports_mask = overrides["supports_mask"]
        provenance["supports_mask"] = (SOURCE_OPERATOR, None)
    elif isinstance(row.get("supports_mask"), bool):
        supports_mask = row["supports_mask"]
        provenance["supports_mask"] = (
            SOURCE_PROVIDER_LIST,
            inputs.provider_list_as_of,
        )

    max_input_images: int | None = None
    raw_max_images = overrides.get("max_input_images")
    if (
        isinstance(raw_max_images, int)
        and not isinstance(raw_max_images, bool)
        and raw_max_images > 0
    ):
        max_input_images = raw_max_images
        provenance["max_input_images"] = (SOURCE_OPERATOR, None)
    else:
        row_max_images = row.get("max_input_images")
        if (
            isinstance(row_max_images, int)
            and not isinstance(row_max_images, bool)
            and row_max_images > 0
        ):
            max_input_images = row_max_images
            provenance["max_input_images"] = (
                SOURCE_PROVIDER_LIST,
                inputs.provider_list_as_of,
            )

    # -- required_params / refused_params -------------------------------------
    op_req = frozenset(_string_tuple(overrides.get("required_params")) or ())
    op_ref = frozenset(_string_tuple(overrides.get("refused_params")) or ())
    required_params = op_req | (learned_required - op_ref)
    refused_params = op_ref | (learned_refused - op_req)
    if op_req:
        provenance["required_params"] = (SOURCE_OPERATOR, None)
    elif learned_required:
        provenance["required_params"] = (SOURCE_LEARNED, None)
    if op_ref:
        provenance["refused_params"] = (SOURCE_OPERATOR, None)
    elif learned_refused:
        provenance["refused_params"] = (SOURCE_LEARNED, None)

    return MediaCapabilityProfile(
        operations=operations,
        sync_mode=sync_mode,
        accepts_encodings=accepts_encodings,
        supports_mask=supports_mask,
        max_input_images=max_input_images,
        accepted_sizes=accepted_sizes,
        accepted_aspect_ratios=accepted_aspect_ratios,
        accepted_resolutions=accepted_resolutions,
        accepted_durations=accepted_durations,
        accepted_formats=accepted_formats,
        accepted_voices=accepted_voices,
        supports_streaming=supports_streaming,
        supported_generation_methods=methods,
        supported_parameters=supported_params,
        required_params=required_params,
        refused_params=refused_params,
        field_provenance=provenance,
    )


def _resolve_string_list_field(
    field_name: str,
    overrides: Mapping[str, Any],
    learned_values: tuple[str, ...] | None,
    row: Mapping[str, Any],
    row_keys: tuple[str, ...],
    provenance: dict[str, tuple[str, str | None]],
    provider_list_as_of: str | None,
) -> tuple[str, ...] | None:
    override_vals = _string_tuple(overrides.get(field_name))
    if override_vals is not None:
        provenance[field_name] = (SOURCE_OPERATOR, None)
        return override_vals
    if learned_values is not None:
        provenance[field_name] = (SOURCE_LEARNED, None)
        return learned_values
    for key in row_keys:
        row_vals = _string_tuple(row.get(key))
        if row_vals is not None:
            provenance[field_name] = (SOURCE_PROVIDER_LIST, provider_list_as_of)
            return row_vals
    return None


__all__ = [
    "FACT_MEDIA_ENCODING_REFUSED",
    "FACT_MEDIA_PARAM_ENUM",
    "MEDIA_OPERATION_OCR",
    "SOURCE_LEARNED",
    "SOURCE_LITELLM",
    "SOURCE_MEASURED",
    "SOURCE_MODELS_DEV",
    "SOURCE_OPENROUTER_LIVE",
    "SOURCE_OPERATOR",
    "SOURCE_PROVIDER_DEFAULT",
    "SOURCE_PROVIDER_LIST",
    "MediaCapabilityInputs",
    "MediaCapabilityProfile",
    "MediaEndpointSource",
    "resolve_media_capability_profile",
]
