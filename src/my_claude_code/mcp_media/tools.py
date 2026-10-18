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
    TOOL_CREATE_VIDEO,
    TOOL_EDIT_IMAGE,
    TOOL_GENERATE_IMAGE,
    TOOL_GET_VIDEO,
    TOOL_MEDIA_STATUS,
    TOOL_SPEAK,
    TOOL_TRANSCRIBE,
    TOOL_TRANSLATE_AUDIO,
)
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
    MEDIA_OPERATION_VIDEO_CREATE,
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
        "job. Returns its id at once; call get_video with it to see progress "
        "and save the video when it is done.",
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
    MEDIA_OPERATION_SPEECH: frozenset({"stream_format"}),
    MEDIA_OPERATION_TRANSCRIBE: frozenset({"stream"}),
    MEDIA_OPERATION_TRANSLATE: frozenset(),
    MEDIA_OPERATION_VIDEO_CREATE: frozenset(),
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

#: Operations whose door refuses a request without one of its file fields,
#: and which file argument that is. Edits take the picture as an upload here
#: (``images`` is the JSON variant the media server does not use).
REQUIRED_FILE_ARGUMENTS: Mapping[str, str] = {
    MEDIA_OPERATION_IMAGE_EDIT: "image_paths",
}

#: Operations whose result is a file, so the agent may say where it goes.
SAVES_FILES: frozenset[str] = frozenset(
    {MEDIA_OPERATION_IMAGE_GENERATE, MEDIA_OPERATION_IMAGE_EDIT, MEDIA_OPERATION_SPEECH}
)

SAVE_TO_ARGUMENT = "save_to"
MODEL_ARGUMENT = "model"
VIDEO_ID_ARGUMENT = "video_id"

_SAVE_TO: Mapping[str, Any] = {
    "type": "string",
    "description": (
        "Optional folder, or file name, for the result. A relative path is "
        "resolved against the folder the app started the media server in. An "
        "existing file is never overwritten: a new name is chosen instead."
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

    @property
    def spends(self) -> bool:
        return bool(self.operation)


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
                "first, then its fallbacks); naming one model uses only that "
                "model."
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
        file_argument = FILE_ARGUMENTS.get(field_name)
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
    if required_file and required_file in properties and required_file not in required:
        required.append(required_file)
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
                "stopped waiting for it."
            ),
            "inputSchema": {"type": "object", "properties": {}},
            "annotations": dict(_STATUS_ANNOTATIONS),
        },
    )


def _get_video_tool(create: MediaTool) -> MediaTool:
    return MediaTool(
        name=TOOL_GET_VIDEO,
        definition={
            "name": TOOL_GET_VIDEO,
            "description": (
                "Free. Check a video job started with create_video. While it "
                "runs this reports its progress; once it is done it saves the "
                "video as a file and returns its path."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    VIDEO_ID_ARGUMENT: {
                        "type": "string",
                        "description": "The id create_video returned.",
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
    for entry in catalogue.get("data") or ():
        if not isinstance(entry, Mapping):
            continue
        operations = entry.get("operations")
        if not isinstance(operations, Mapping):
            continue
        schemas_by_model[str(entry.get("id"))] = {
            str(op): value.get("request_schema") or {}
            for op, value in operations.items()
            if isinstance(value, Mapping)
        }
    create_video: MediaTool | None = None
    seen: set[str] = set()
    for rail in catalogue.get("rails") or ():
        if not isinstance(rail, Mapping):
            continue
        operations = rail.get("operations")
        if not isinstance(operations, Mapping):
            continue
        for operation, info in operations.items():
            if operation in seen or not isinstance(info, Mapping):
                continue
            by_model = {
                ref: schemas_by_model.get(ref, {}).get(operation, {})
                for ref in _as_list(info.get("models"))
            }
            tool = _operation_tool(rail, str(operation), info, by_model)
            if tool is None:
                continue
            seen.add(str(operation))
            tools.append(tool)
            if operation == MEDIA_OPERATION_VIDEO_CREATE:
                create_video = tool
    if create_video is not None:
        tools.append(_get_video_tool(create_video))
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
