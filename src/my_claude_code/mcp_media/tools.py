"""The media tools, as a pure function of ``GET /v1/media/models`` (7.99.0).

User decisions 3 and 4 of 2026-10-18: ``media_status`` always; ``generate_image``,
``edit_image``, ``speak``, ``transcribe``, ``translate_audio`` and
``create_video`` (with ``get_video``) only for an operation whose rail has at
least one model; one tool per job, the rail's models as an optional choice,
and every field any of those models takes, labelled with which models take,
need or refuse it -- read from the per-rail field lists 7.98.0 publishes, never
from a hand-written provider list and never from a model name (IV.6).

**Byte-stable** (IV.7, risk R1). :func:`derive_tools` reads nothing but its
argument: no clock, no randomness, no environment. Tools come in the order the
catalogue lists its rails and their operations, fields in the order the
catalogue lists them, so the same catalogue gives the same bytes in every
call, every process and every session -- and an app's prompt cache keeps its
prefix for as long as the rails do not change.

**A plain JSON Schema subset** (risk R9). A model API that reads a tool's
schema sees ``type``, ``enum``, ``items``, ``properties``, ``required`` and
``description`` only: no ``x-mcc``, ``$schema``, ``not``, ``const``,
``anyOf``, ``format`` or date. What the published schema says with those
keywords is said in the field's description instead.

The only hand-written data here is per *operation* -- which door fields the
media server sets itself, and which file fields become local paths -- because
those are facts about MCC's own doors, not about any model.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from my_claude_code.config.mcp_media import (
    TOOL_CREATE_IMAGE_VARIATION,
    TOOL_CREATE_VIDEO,
    TOOL_DELETE_VIDEO,
    TOOL_EDIT_IMAGE,
    TOOL_EDIT_VIDEO,
    TOOL_GENERATE_IMAGE,
    TOOL_GET_VIDEO,
    TOOL_LIST_VIDEOS,
    TOOL_LIST_VOICES,
    TOOL_MEDIA_STATUS,
    TOOL_OCR_DOCUMENT,
    TOOL_SPEAK,
    TOOL_TRANSCRIBE,
    TOOL_TRANSLATE_AUDIO,
)
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_IMAGE_VARIATION,
    MEDIA_OPERATION_OCR,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
    MEDIA_OPERATION_VIDEO_CREATE,
    MEDIA_OPERATION_VIDEO_EDIT,
)

#: Per routed operation: the tool's name and what it does, in one line.
OPERATION_TOOLS: Mapping[str, tuple[str, str]] = {
    MEDIA_OPERATION_IMAGE_GENERATE: (
        TOOL_GENERATE_IMAGE,
        "Generate images from a text prompt through MCC's {rail} rail. The "
        "provider bills each call. Saves each picture as a file and returns "
        "its path and the picture.",
    ),
    MEDIA_OPERATION_IMAGE_EDIT: (
        TOOL_EDIT_IMAGE,
        "Edit or combine local image files with a text prompt through MCC's "
        "{rail} rail. The provider bills each call. Saves each picture as a "
        "file and returns its path and the picture.",
    ),
    MEDIA_OPERATION_IMAGE_VARIATION: (
        TOOL_CREATE_IMAGE_VARIATION,
        "Create variations of a local image file through MCC's {rail} rail. "
        "The provider bills each call. Saves each picture as a file and "
        "returns its path and the picture.",
    ),
    MEDIA_OPERATION_SPEECH: (
        TOOL_SPEAK,
        "Turn text into speech through MCC's {rail} rail. The provider bills "
        "each call. Saves the audio as a file and returns its path.",
    ),
    MEDIA_OPERATION_TRANSCRIBE: (
        TOOL_TRANSCRIBE,
        "Transcribe a local audio file to text through MCC's {rail} rail. The "
        "provider bills each call. Returns the transcript.",
    ),
    MEDIA_OPERATION_TRANSLATE: (
        TOOL_TRANSLATE_AUDIO,
        "Translate a local audio file into English text through MCC's {rail} "
        "rail. The provider bills each call. Returns the text.",
    ),
    MEDIA_OPERATION_VIDEO_CREATE: (
        TOOL_CREATE_VIDEO,
        "Start a video job through MCC's {rail} rail. The provider bills the "
        "job. Returns its id at once (or waits up to wait_seconds); call "
        "get_video with it to see progress and save the video when the job "
        "is done.",
    ),
    MEDIA_OPERATION_VIDEO_EDIT: (
        TOOL_EDIT_VIDEO,
        "Edit or extend a video through MCC's {rail} rail. The provider bills "
        "the job. Returns its id at once (or waits up to wait_seconds); call "
        "get_video with it to see progress and save the video when the job "
        "is done.",
    ),
    MEDIA_OPERATION_OCR: (
        TOOL_OCR_DOCUMENT,
        "Extract text and markdown from a local document, image, or URL "
        "through MCC's {rail} rail. The provider bills each call. Returns the "
        "extracted markdown (and saves extracted markdown/images when asked).",
    ),
}

#: Door fields the media server itself decides, per operation: how the
#: answer travels (a stream, partial pictures, a link or bytes) and who is
#: asking. They are door data, not model facts, and are never offered.
SERVER_OWNED_FIELDS: Mapping[str, frozenset[str]] = {
    MEDIA_OPERATION_IMAGE_GENERATE: frozenset(
        {"stream", "partial_images", "response_format", "user"}
    ),
    MEDIA_OPERATION_IMAGE_EDIT: frozenset(
        {"stream", "partial_images", "response_format", "user", "images"}
    ),
    MEDIA_OPERATION_IMAGE_VARIATION: frozenset({"stream", "response_format", "user"}),
    MEDIA_OPERATION_SPEECH: frozenset({"stream_format"}),
    MEDIA_OPERATION_TRANSCRIBE: frozenset({"stream"}),
    MEDIA_OPERATION_TRANSLATE: frozenset(),
    MEDIA_OPERATION_VIDEO_CREATE: frozenset(),
    MEDIA_OPERATION_VIDEO_EDIT: frozenset(),
    MEDIA_OPERATION_OCR: frozenset(),
}


@dataclass(frozen=True, slots=True)
class FileArgument:
    """A door's file field, taken from the agent as a local path."""

    #: The argument the agent passes.
    argument: str
    #: The door's multipart field.
    door_field: str
    #: An array of paths (several pictures to edit) or one path.
    many: bool
    description: str


#: Door file field -> the local-path argument the agent passes instead. The
#: media server reads the file and uploads it; the result names each path.
FILE_ARGUMENTS: Mapping[str, FileArgument] = {
    "image": FileArgument(
        "image_paths",
        "image",
        True,
        "Local image file(s) to edit or combine, as paths. Each file is "
        "uploaded to MCC.",
    ),
    "mask": FileArgument(
        "mask_path",
        "mask",
        False,
        "A local PNG mask; its transparent areas are the ones to change.",
    ),
    "file": FileArgument(
        "audio_path",
        "file",
        False,
        "The local audio file to read, as a path. It is uploaded to MCC.",
    ),
    "input_reference": FileArgument(
        "input_reference_path",
        "input_reference",
        False,
        "A local image or video the video starts from, as a path.",
    ),
}

_OPERATION_FILE_ARGUMENTS: Mapping[tuple[str, str], FileArgument] = {
    (MEDIA_OPERATION_IMAGE_VARIATION, "image"): FileArgument(
        "image_path",
        "image",
        False,
        "The local image file to create variations of, as a path. It is uploaded to MCC.",
    ),
    (MEDIA_OPERATION_VIDEO_EDIT, "video"): FileArgument(
        "video_path",
        "video",
        False,
        "A local video file to edit or extend, as a path. It is uploaded to MCC.",
    ),
    (MEDIA_OPERATION_OCR, "file"): FileArgument(
        "file_path",
        "file",
        False,
        "The local document or image file to OCR, as a path. It is uploaded to MCC.",
    ),
}

#: Operations whose door refuses a request without one of its file fields,
#: and which file argument that is. Edits take the picture as an upload here
#: (``images`` is the JSON variant the media server does not use).
REQUIRED_FILE_ARGUMENTS: Mapping[str, str] = {
    MEDIA_OPERATION_IMAGE_EDIT: "image_paths",
    MEDIA_OPERATION_IMAGE_VARIATION: "image_path",
}

#: Operations whose result is a file, so the agent may say where it goes.
SAVES_FILES: frozenset[str] = frozenset(
    {
        MEDIA_OPERATION_IMAGE_GENERATE,
        MEDIA_OPERATION_IMAGE_EDIT,
        MEDIA_OPERATION_IMAGE_VARIATION,
        MEDIA_OPERATION_SPEECH,
        MEDIA_OPERATION_OCR,
    }
)

SAVE_TO_ARGUMENT = "save_to"
SAVE_IMAGES_ARGUMENT = "save_images"
MODEL_ARGUMENT = "model"
OPERATION_ARGUMENT = "operation"
VIDEO_ID_ARGUMENT = "video_id"
WAIT_SECONDS_ARGUMENT = "wait_seconds"
POLL_INTERVAL_SECONDS_ARGUMENT = "poll_interval_seconds"
VARIANT_ARGUMENT = "variant"
PROVIDER_ARGUMENT = "provider"
LIMIT_ARGUMENT = "limit"
AFTER_ARGUMENT = "after"
ORDER_ARGUMENT = "order"

_SAVE_TO: Mapping[str, Any] = {
    "type": "string",
    "description": (
        "Optional folder, or file name, for the result. A relative path is "
        "resolved against the folder the app started the media server in. An "
        "existing file is never overwritten: a new name is chosen instead."
    ),
}

_WAIT_SECONDS_PROPERTY: Mapping[str, Any] = {
    "type": "integer",
    "description": (
        "Optional. Seconds to wait for the video job to complete before "
        "returning (0 to 45; default 0 returns immediately). If it finishes "
        "within the wait window, the video is saved to disk."
    ),
}

_POLL_INTERVAL_PROPERTY: Mapping[str, Any] = {
    "type": "integer",
    "description": (
        "Optional. Seconds between status checks while waiting (1 to 15; default 5)."
    ),
}

#: Spending tools: not read-only, so an app that asks before a tool asks
#: before these (user decision 8; Codex asks unless a tool is read-only).
SPENDING_ANNOTATIONS: Mapping[str, Any] = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": True,
}
_STATUS_ANNOTATIONS: Mapping[str, Any] = {
    "readOnlyHint": True,
    "openWorldHint": False,
}
_GET_VIDEO_ANNOTATIONS: Mapping[str, Any] = {
    "readOnlyHint": True,
    "openWorldHint": True,
}
_DELETE_VIDEO_ANNOTATIONS: Mapping[str, Any] = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": True,
}

_BUCKET_LABELS: tuple[tuple[str, str], ...] = (
    ("required_by", "required by"),
    ("accepted_by", "taken by"),
    ("refused_by", "refused by"),
    ("not_forwarded_by", "not sent to"),
    ("not_known_for", "not known for"),
)


@dataclass(frozen=True, slots=True)
class MediaTool:
    """One tool: what the app is shown, and how a call is carried to MCC."""

    name: str
    #: The MCP ``Tool`` as the wire carries it: name, description,
    #: inputSchema, annotations. Plain data; never mutated.
    definition: Mapping[str, Any]
    #: The routed operation, or ``""`` for ``media_status`` / ``get_video``.
    operation: str = ""
    #: The door the call goes to, as the catalogue names it.
    endpoint: str = ""
    rail: str = ""
    #: The rail's models serving this operation, in rail order.
    models: tuple[str, ...] = ()
    #: Argument -> door file field, for the arguments that are local paths.
    file_arguments: Mapping[str, FileArgument] = field(default_factory=dict)
    #: Video jobs base endpoint (e.g. ``/v1/videos``) for polling from ``edit_video``.
    jobs_endpoint: str = ""

    @property
    def spends(self) -> bool:
        return bool(self.operation)


_CAPABILITY_FIELD_HINTS: Mapping[str, tuple[str, str]] = {
    "size": ("accepted_sizes", "accepted sizes"),
    "aspect_ratio": ("accepted_aspect_ratios", "accepted aspect ratios"),
    "resolution": ("accepted_resolutions", "accepted resolutions"),
    "seconds": ("accepted_durations", "accepted durations (s)"),
    "response_format": ("accepted_formats", "accepted formats"),
    "output_format": ("accepted_formats", "accepted formats"),
    "voice": ("accepted_voices", "accepted voices"),
}


def _append_capability_hints(
    properties: dict[str, Any],
    serving: Sequence[str],
    caps_by_ref: Mapping[str, Mapping[str, Any]],
) -> None:
    if not any(caps_by_ref.get(ref) for ref in serving):
        return
    for arg_name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        desc = str(prop.get("description") or "")
        notes: list[str] = []
        if arg_name == "mask_path":
            yes = [
                ref
                for ref in serving
                if caps_by_ref.get(ref, {}).get("supports_mask") is True
            ]
            no = [
                ref
                for ref in serving
                if caps_by_ref.get(ref, {}).get("supports_mask") is False
            ]
            if yes:
                notes.append(f"mask supported by {', '.join(yes)}")
            if no:
                notes.append(f"mask not supported by {', '.join(no)}")
        elif arg_name == "image_paths":
            limits = [
                f"{ref}: {caps_by_ref[ref]['max_input_images']}"
                for ref in serving
                if isinstance(caps_by_ref.get(ref, {}).get("max_input_images"), int)
            ]
            if limits:
                notes.append("max input images: " + ", ".join(limits))
        elif (
            arg_name in _CAPABILITY_FIELD_HINTS
            and "enum" not in prop
            and "values stated:" not in desc
        ):
            cap_key, label = _CAPABILITY_FIELD_HINTS[arg_name]
            collected: list[str] = []
            for ref in serving:
                raw_vals = caps_by_ref.get(ref, {}).get(cap_key)
                if isinstance(raw_vals, list):
                    for item in raw_vals:
                        text = str(item)
                        if text not in collected:
                            collected.append(text)
            if collected:
                shown = ", ".join(collected[:12])
                if len(collected) > 12:
                    shown += f", ... (+{len(collected) - 12} more)"
                notes.append(f"{label}: {shown}")
        if notes:
            joined = "; ".join(notes)
            prop["description"] = f"{desc}; {joined}" if desc else joined


def _as_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _bucket_text(entry: Mapping[str, Any]) -> str:
    required = set(_as_list(entry.get("required_by")))
    parts: list[str] = []
    for key, label in _BUCKET_LABELS:
        refs = _as_list(entry.get(key))
        if key == "accepted_by":
            refs = [ref for ref in refs if ref not in required]
        if refs:
            parts.append(f"{label} {', '.join(refs)}")
    return "; ".join(parts)


def _plain_type(value: object) -> str | None:
    """One JSON type name; a list of types becomes its first (R9)."""

    if isinstance(value, str):
        return value
    if isinstance(value, list) and value and isinstance(value[0], str):
        return value[0]
    return None


def _plain_items(value: object) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    kind = _plain_type(value.get("type"))
    return {"type": kind} if kind else None


def _model_properties(
    schemas: Sequence[Mapping[str, Any]], name: str
) -> list[Mapping[str, Any]]:
    """Each serving model's own property for ``name``, where it states one."""

    found: list[Mapping[str, Any]] = []
    for schema in schemas:
        properties = schema.get("properties")
        prop = properties.get(name) if isinstance(properties, Mapping) else None
        if isinstance(prop, Mapping) and "not" not in prop:
            found.append(prop)
    return found


def _field_property(
    entry: Mapping[str, Any],
    stated: Sequence[Mapping[str, Any]],
    serving_count: int,
) -> dict[str, Any]:
    prop: dict[str, Any] = {}
    kind = next(
        (k for k in (_plain_type(p.get("type")) for p in stated) if k is not None),
        None,
    )
    if kind:
        prop["type"] = kind
        if kind == "array":
            items = next(
                (i for i in (_plain_items(p.get("items")) for p in stated) if i),
                None,
            )
            if items:
                prop["items"] = items
    values = [tuple(p["enum"]) for p in stated if isinstance(p.get("enum"), list)]
    every_model_states_the_same = (
        len(values) == serving_count and len(values) > 0 and len(set(values)) == 1
    )
    description = _bucket_text(entry)
    if every_model_states_the_same:
        prop["enum"] = list(values[0])
    elif values:
        listed: list[Any] = []
        for stated_values in values:
            listed.extend(v for v in stated_values if v not in listed)
        stated_text = "values stated: " + ", ".join(
            json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
            for v in listed
        )
        description = f"{description}; {stated_text}" if description else stated_text
    if description:
        prop["description"] = description
    return prop


def _operation_tool(
    rail: Mapping[str, Any],
    operation: str,
    info: Mapping[str, Any],
    schemas_by_model: Mapping[str, Mapping[str, Any]],
    caps_by_ref: Mapping[str, Mapping[str, Any]] | None = None,
    jobs_endpoint: str = "",
) -> MediaTool | None:
    serving = tuple(_as_list(info.get("models")))
    if not serving or operation not in OPERATION_TOOLS:
        return None
    name, purpose = OPERATION_TOOLS[operation]
    label = str(rail.get("label") or rail.get("rail") or "media")
    schemas = [schemas_by_model.get(ref, {}) for ref in serving]
    properties: dict[str, Any] = {
        MODEL_ARGUMENT: {
            "type": "string",
            "enum": list(serving),
            "description": (
                "Optional. Leave it out to use the rail in its order (primary "
                "first, then its fallbacks); naming one ref uses only that "
                "entry."
            ),
        }
    }
    required: list[str] = []
    file_arguments: dict[str, FileArgument] = {}
    fields = info.get("fields")
    owned = SERVER_OWNED_FIELDS.get(operation, frozenset())
    for field_name, entry in (fields if isinstance(fields, Mapping) else {}).items():
        if field_name in owned or not isinstance(entry, Mapping):
            continue
        unable = set(_as_list(entry.get("refused_by"))) | set(
            _as_list(entry.get("not_forwarded_by"))
        )
        if set(serving) <= unable:
            # No model on the rail can take it: refused by, or not sent to,
            # every one of them. Offering it would only invite a refusal.
            continue
        file_argument = _OPERATION_FILE_ARGUMENTS.get(
            (operation, field_name), FILE_ARGUMENTS.get(field_name)
        )
        if file_argument is not None:
            argument = file_argument.argument
            prop: dict[str, Any] = (
                {"type": "array", "items": {"type": "string"}}
                if file_argument.many
                else {"type": "string"}
            )
            buckets = _bucket_text(entry)
            prop["description"] = (
                f"{file_argument.description} ({buckets})"
                if buckets
                else file_argument.description
            )
            file_arguments[argument] = file_argument
        else:
            argument = field_name
            prop = _field_property(
                entry, _model_properties(schemas, field_name), len(serving)
            )
        properties[argument] = prop
        required_by = set(_as_list(entry.get("required_by")))
        if serving and set(serving) <= required_by and argument not in required:
            required.append(argument)
    required_file = REQUIRED_FILE_ARGUMENTS.get(operation)
    if (
        operation == MEDIA_OPERATION_OCR
        and "file_path" in properties
        and not any(k in properties for k in ("document_url", "image_url", "document"))
    ):
        required_file = "file_path"
    if required_file and required_file in properties and required_file not in required:
        required.append(required_file)
    if caps_by_ref:
        _append_capability_hints(properties, serving, caps_by_ref)
    if operation in (MEDIA_OPERATION_VIDEO_CREATE, MEDIA_OPERATION_VIDEO_EDIT):
        properties[WAIT_SECONDS_ARGUMENT] = dict(_WAIT_SECONDS_PROPERTY)
        properties[POLL_INTERVAL_SECONDS_ARGUMENT] = dict(_POLL_INTERVAL_PROPERTY)
    if operation == MEDIA_OPERATION_OCR:
        properties[SAVE_IMAGES_ARGUMENT] = {
            "type": "boolean",
            "description": (
                "Optional. When true (default when save_to is set or "
                "include_image_base64 is true), save extracted page images to "
                "disk and return their paths."
            ),
        }
    if operation in SAVES_FILES:
        properties[SAVE_TO_ARGUMENT] = dict(_SAVE_TO)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    definition = {
        "name": name,
        "description": (
            purpose.format(rail=label)
            + " Models on the rail, in order: "
            + ", ".join(serving)
            + ". A field not listed here is passed to the provider unchanged."
        ),
        "inputSchema": schema,
        "annotations": dict(SPENDING_ANNOTATIONS),
    }
    return MediaTool(
        name=name,
        definition=definition,
        operation=operation,
        endpoint=str(info.get("endpoint") or ""),
        rail=str(rail.get("rail") or ""),
        models=serving,
        file_arguments=file_arguments,
        jobs_endpoint=jobs_endpoint,
    )


def _status_tool() -> MediaTool:
    return MediaTool(
        name=TOOL_MEDIA_STATUS,
        definition={
            "name": TOOL_MEDIA_STATUS,
            "description": (
                "Free. Which of MCC's media rails are configured and their "
                "models in order, the MCC address this server uses and whether "
                "it answers, where results are saved, and every result of this "
                "session -- including a call that finished after the app "
                "stopped waiting for it. Pass optional model and/or operation "
                "to inspect accepted, required, and refused parameters and "
                "learned constraints with zero spend."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    MODEL_ARGUMENT: {
                        "type": "string",
                        "description": (
                            "Optional. Model ref or id to inspect capabilities "
                            "and parameter schema for."
                        ),
                    },
                    OPERATION_ARGUMENT: {
                        "type": "string",
                        "description": (
                            "Optional. Media operation name to inspect (e.g. "
                            "image_generate, image_edit, speech, video_create, ocr)."
                        ),
                    },
                },
            },
            "annotations": dict(_STATUS_ANNOTATIONS),
        },
    )


def _list_voices_tool(speech: MediaTool, endpoint: str) -> MediaTool:
    return MediaTool(
        name=TOOL_LIST_VOICES,
        definition={
            "name": TOOL_LIST_VOICES,
            "description": (
                "Free. List available TTS voices across configured Speech rail "
                "models, optionally filtered by provider or model."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    PROVIDER_ARGUMENT: {
                        "type": "string",
                        "description": "Optional provider id filter.",
                    },
                    MODEL_ARGUMENT: {
                        "type": "string",
                        "description": "Optional ref or id filter.",
                    },
                },
            },
            "annotations": dict(_STATUS_ANNOTATIONS),
        },
        endpoint=endpoint,
        rail=speech.rail,
        models=speech.models,
    )


def _get_video_tool(create: MediaTool) -> MediaTool:
    return MediaTool(
        name=TOOL_GET_VIDEO,
        definition={
            "name": TOOL_GET_VIDEO,
            "description": (
                "Free. Check a video job started with create_video or "
                "edit_video. While it runs this reports its progress (or "
                "waits up to wait_seconds); once it is done it saves the "
                "video as a file and returns its path."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    VIDEO_ID_ARGUMENT: {
                        "type": "string",
                        "description": "The id create_video or edit_video returned.",
                    },
                    WAIT_SECONDS_ARGUMENT: dict(_WAIT_SECONDS_PROPERTY),
                    POLL_INTERVAL_SECONDS_ARGUMENT: dict(_POLL_INTERVAL_PROPERTY),
                    VARIANT_ARGUMENT: {
                        "type": "string",
                        "description": (
                            "Optional asset variant to download when the job "
                            "completes (when the provider publishes variants)."
                        ),
                    },
                    SAVE_TO_ARGUMENT: dict(_SAVE_TO),
                },
                "required": [VIDEO_ID_ARGUMENT],
            },
            "annotations": dict(_GET_VIDEO_ANNOTATIONS),
        },
        endpoint=create.endpoint,
        rail=create.rail,
        models=create.models,
        jobs_endpoint=create.jobs_endpoint or create.endpoint,
    )


def _list_videos_tool(create: MediaTool, endpoint: str) -> MediaTool:
    return MediaTool(
        name=TOOL_LIST_VIDEOS,
        definition={
            "name": TOOL_LIST_VIDEOS,
            "description": (
                "Free. List MCC's recorded video jobs across sessions, "
                "newest first by default."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    LIMIT_ARGUMENT: {
                        "type": "integer",
                        "description": "Optional maximum number of video jobs to return.",
                    },
                    AFTER_ARGUMENT: {
                        "type": "string",
                        "description": "Optional cursor video_id for pagination.",
                    },
                    ORDER_ARGUMENT: {
                        "type": "string",
                        "enum": ["asc", "desc"],
                        "description": "Optional sort order by creation time.",
                    },
                },
            },
            "annotations": dict(_GET_VIDEO_ANNOTATIONS),
        },
        endpoint=endpoint,
        rail=create.rail,
        models=create.models,
        jobs_endpoint=endpoint,
    )


def _delete_video_tool(create: MediaTool, endpoint: str) -> MediaTool:
    return MediaTool(
        name=TOOL_DELETE_VIDEO,
        definition={
            "name": TOOL_DELETE_VIDEO,
            "description": (
                "Delete a video job upstream (where supported) and from MCC's "
                "job store. Destructive."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    VIDEO_ID_ARGUMENT: {
                        "type": "string",
                        "description": "The video_id to delete.",
                    },
                },
                "required": [VIDEO_ID_ARGUMENT],
            },
            "annotations": dict(_DELETE_VIDEO_ANNOTATIONS),
        },
        endpoint=endpoint,
        rail=create.rail,
        models=create.models,
        jobs_endpoint=endpoint,
    )


def derive_tools(catalogue: Mapping[str, Any] | None) -> tuple[MediaTool, ...]:
    """Every tool the catalogue supports, ``media_status`` first.

    ``None`` (no catalogue: MCC not answering and nothing saved) gives
    ``media_status`` alone, which says why.
    """

    tools: list[MediaTool] = [_status_tool()]
    if not isinstance(catalogue, Mapping):
        return tuple(tools)
    schemas_by_model: dict[str, dict[str, Mapping[str, Any]]] = {}
    capabilities_by_model: dict[str, dict[str, Mapping[str, Any]]] = {}
    for entry in catalogue.get("data") or ():
        if not isinstance(entry, Mapping):
            continue
        operations = entry.get("operations")
        if not isinstance(operations, Mapping):
            continue
        ref = str(entry.get("id"))
        schemas_by_model[ref] = {
            str(op): value.get("request_schema") or {}
            for op, value in operations.items()
            if isinstance(value, Mapping)
        }
        capabilities_by_model[ref] = {
            str(op): value.get("capabilities") or {}
            for op, value in operations.items()
            if isinstance(value, Mapping)
            and isinstance(value.get("capabilities"), Mapping)
        }
    create_video: MediaTool | None = None
    video_jobs_endpoint = ""
    video_delete_endpoint = ""
    seen: set[str] = set()
    for rail in catalogue.get("rails") or ():
        if not isinstance(rail, Mapping):
            continue
        operations = rail.get("operations")
        if not isinstance(operations, Mapping):
            continue
        raw_jobs = (
            operations.get(MEDIA_OPERATION_VIDEO_CREATE, {}).get("jobs_endpoint")
            if isinstance(operations.get(MEDIA_OPERATION_VIDEO_CREATE), Mapping)
            else None
        ) or rail.get("jobs_endpoint")
        if isinstance(raw_jobs, str) and raw_jobs.strip():
            video_jobs_endpoint = raw_jobs.strip()
        for operation, info in operations.items():
            if operation in seen or not isinstance(info, Mapping):
                continue
            refs = _as_list(info.get("models"))
            by_model = {
                ref: schemas_by_model.get(ref, {}).get(operation, {}) for ref in refs
            }
            caps_by_ref = {
                ref: capabilities_by_model.get(ref, {}).get(operation, {})
                for ref in refs
            }
            op_jobs = (
                video_jobs_endpoint
                if operation
                in (MEDIA_OPERATION_VIDEO_CREATE, MEDIA_OPERATION_VIDEO_EDIT)
                else ""
            )
            tool = _operation_tool(
                rail,
                str(operation),
                info,
                by_model,
                caps_by_ref=caps_by_ref,
                jobs_endpoint=op_jobs,
            )
            if tool is None:
                continue
            seen.add(str(operation))
            tools.append(tool)
            if operation == MEDIA_OPERATION_SPEECH:
                voices_ep = info.get("voices_endpoint") or rail.get("voices_endpoint")
                if isinstance(voices_ep, str) and voices_ep.strip():
                    tools.append(_list_voices_tool(tool, voices_ep.strip()))
            if operation == MEDIA_OPERATION_VIDEO_CREATE:
                create_video = tool
                raw_del = (
                    info.get("delete_endpoint")
                    or info.get("video_delete")
                    or rail.get("delete_endpoint")
                    or rail.get("video_delete")
                )
                if isinstance(raw_del, str) and raw_del.strip():
                    video_delete_endpoint = raw_del.strip()
    if create_video is not None:
        tools.append(_get_video_tool(create_video))
        if video_jobs_endpoint:
            tools.append(_list_videos_tool(create_video, video_jobs_endpoint))
        if video_delete_endpoint:
            tools.append(_delete_video_tool(create_video, video_delete_endpoint))
    return tuple(tools)


def tool_definitions(tools: Sequence[MediaTool]) -> list[Mapping[str, Any]]:
    """The ``tools/list`` entries, in order."""

    return [tool.definition for tool in tools]


def tool_list_bytes(tools: Sequence[MediaTool]) -> int:
    """What the tool list costs on the wire: its compact JSON, in bytes.

    The figure the MCC media server card shows: every app that has the server
    on sends roughly this much tool text with each of its requests.
    """

    return len(
        json.dumps(
            tool_definitions(tools), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    )
