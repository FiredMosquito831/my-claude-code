"""Each media model's request fields, published as a JSON Schema (7.98.0).

The user asked (2026-10-04 21:24) for "publishing schema based on model in chains
for image/video gen": per model on a rail, the fields that model's request
takes, so an agent builds a correct request without trial and error -- and
without the user's 52 Agnes video creates that answered ``mode is required``
and six image requests that answered ``image_config is not supported``.

**Publish-only** (user decision 6 of 2026-10-16). Nothing here is read by a
request, a route, a retry, a fallback or a price; every response that existed
before is byte-identical, and a schema is only ever an extra key beside them.

One schema per (rail model, operation): JSON Schema 2020-12, and on every
property an ``x-mcc`` record of what is known about it and from where:

``state``
    ``stated`` -- a source says this model takes it;
    ``required (stated)`` / ``refused (stated)`` -- the operator said so, or
    (required) MCC's own door will not take a request without it;
    ``required (learned)`` / ``refused (learned)`` -- the host said so in a
    refusal (``providers/media/param_refusal.py``);
    ``not forwarded by MCC`` -- MCC's adapter for this host does not send it;
    ``forwarded, not known`` -- MCC sends it and nobody has said whether the
    model takes it. **Never "unsupported"**: not known is not refused
    (WORKING-NOTES IV.16).
``source``
    the rung that said it (:data:`SOURCE_LABELS`);
``since``
    the day it was said, when the source has one;
``values``
    the values the source listed (formats, voices), also the property's
    ``enum``.

The ladder, provider first and gap-filling down (user decision 3), extended
from the one every other model field is read from and never a model-name
branch -- every rung is declared data or something a host said:

1. the operator's own statement on the Models page
   (``config/media_request_fields.py``);
2. a refusal this host gave for this model and operation (learned facts
   ``media_param_required`` / ``media_param_refused``);
3. the provider's declared endpoint (``config/media_surfaces.py``: formats,
   voices, stream, renames, JSON-only encoding) and MCC's own adapter (what a
   Gemini native body cannot carry);
4. the provider's own ``/models`` row (``supported_parameters``);
5. OpenRouter's live list, then LiteLLM's map -- **declared, inert**. The one
   recorded copy of OpenRouter's list MCC holds
   (``tests/contracts/metadata_ladder_openrouter_live.json``) gives its three
   media-output models only chat sampling names or ``[]``, and no LiteLLM entry
   MCC has recorded carries a media request field; each is wired only after a
   measurement shows it carries one (spec §7, not approved);
6. models.dev: measured empty (369 media models, 0 parameter fields) -- not a
   rung.

Below them all is the base field set: the client door's own published
contract for the operation (openai-python 2.54.0's request types, pinned by a
drift test, plus the fields the Gemini-shaped door maps a Veo body onto).
That is the endpoint the client speaks, not a claim about the model, so every
one of its fields starts ``forwarded, not known``.

Pure: no I/O, no clock unless asked, imports ``config`` and ``core`` only.
"""

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.media_request_fields import (
    OPERATOR_ACCEPTED,
    OPERATOR_REFUSED,
    OPERATOR_REQUIRED,
    MediaRequestFieldStatements,
    OperationStatements,
)
from my_claude_code.config.media_surfaces import (
    MEDIA_ENCODING_JSON,
    MEDIA_OPERATION_IMAGE_EDIT,
    MEDIA_OPERATION_IMAGE_GENERATE,
    MEDIA_OPERATION_SPEECH,
    MEDIA_OPERATION_TRANSCRIBE,
    MEDIA_OPERATION_TRANSLATE,
    MEDIA_OPERATION_VIDEO_CREATE,
    MEDIA_SHAPE_GEMINI_TRANSCRIBE,
    MEDIA_SHAPE_GEMINI_TTS,
    MediaSurface,
    surface_for,
)

from .request import RAIL_OPERATIONS, MediaRail

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

# -- states ------------------------------------------------------------------

STATE_STATED = "stated"
STATE_REQUIRED_STATED = "required (stated)"
STATE_REFUSED_STATED = "refused (stated)"
STATE_REQUIRED_LEARNED = "required (learned)"
STATE_REFUSED_LEARNED = "refused (learned)"
STATE_NOT_FORWARDED = "not forwarded by MCC"
STATE_NOT_KNOWN = "forwarded, not known"

STATES: tuple[str, ...] = (
    STATE_STATED,
    STATE_REQUIRED_STATED,
    STATE_REFUSED_STATED,
    STATE_REQUIRED_LEARNED,
    STATE_REFUSED_LEARNED,
    STATE_NOT_FORWARDED,
    STATE_NOT_KNOWN,
)
REQUIRED_STATES = frozenset({STATE_REQUIRED_STATED, STATE_REQUIRED_LEARNED})
REFUSED_STATES = frozenset({STATE_REFUSED_STATED, STATE_REFUSED_LEARNED})
#: A model "accepts" a field a source states it takes, or says it needs.
ACCEPTED_STATES = frozenset({STATE_STATED}) | REQUIRED_STATES

# -- sources (rungs) ------------------------------------------------------------

SOURCE_OPERATOR = "operator"
SOURCE_LEARNED = "learned"
SOURCE_PROVIDER_DECLARED = "provider_declared"
SOURCE_MCC_ADAPTER = "mcc_adapter"
SOURCE_PROVIDER_LIST = "provider_list"
SOURCE_OPENROUTER_LIVE = "openrouter_live"
SOURCE_LITELLM = "litellm"
SOURCE_DOOR = "door"
SOURCE_RAIL = "rail"

SOURCE_LABELS: dict[str, str] = {
    SOURCE_OPERATOR: "your statement (Models page)",
    SOURCE_LEARNED: "learned from this host's refusal",
    SOURCE_PROVIDER_DECLARED: "the provider's declared endpoint",
    SOURCE_MCC_ADAPTER: "MCC's adapter for this host",
    SOURCE_PROVIDER_LIST: "the provider's own model list",
    SOURCE_OPENROUTER_LIVE: "OpenRouter's live list",
    SOURCE_LITELLM: "LiteLLM's model map",
    SOURCE_DOOR: "the endpoint's own contract",
    SOURCE_RAIL: "the media rail",
}

#: The ladder, top first, with whether each rung is read today and why not.
#: Rungs 5 are declared so the order is fixed now, and inert until a
#: measurement shows the source carries a media request field.
LADDER: tuple[tuple[str, bool, str | None], ...] = (
    (SOURCE_OPERATOR, True, None),
    (SOURCE_LEARNED, True, None),
    (SOURCE_PROVIDER_DECLARED, True, None),
    (SOURCE_MCC_ADAPTER, True, None),
    (SOURCE_PROVIDER_LIST, True, None),
    (
        SOURCE_OPENROUTER_LIVE,
        False,
        "inert: the recorded OpenRouter list gives its media models only chat "
        "sampling names, no media request field",
    ),
    (
        SOURCE_LITELLM,
        False,
        "inert: no LiteLLM entry MCC has recorded carries a media request field",
    ),
)

#: Learned-fact kinds, spelled as ``providers/recovery/facts.py`` spells them
#: (that package is out of this one's reach; a test pins the two together).
FACT_MEDIA_PARAM_REQUIRED = "media_param_required"
FACT_MEDIA_PARAM_REFUSED = "media_param_refused"
_LEARNED_STATES: dict[str, str] = {
    FACT_MEDIA_PARAM_REQUIRED: STATE_REQUIRED_LEARNED,
    FACT_MEDIA_PARAM_REFUSED: STATE_REFUSED_LEARNED,
}

_OPERATOR_STATES: dict[str, str] = {
    OPERATOR_ACCEPTED: STATE_STATED,
    OPERATOR_REQUIRED: STATE_REQUIRED_STATED,
    OPERATOR_REFUSED: STATE_REFUSED_STATED,
}

# -- the base field set: the client door's own contract ----------------------

_STRING: Mapping[str, Any] = {"type": "string"}
_INTEGER: Mapping[str, Any] = {"type": "integer"}
_NUMBER: Mapping[str, Any] = {"type": "number"}
_BOOLEAN: Mapping[str, Any] = {"type": "boolean"}
_FILE: Mapping[str, Any] = {"type": "string", "format": "binary"}
_STRINGS: Mapping[str, Any] = {"type": "array", "items": {"type": "string"}}


@dataclass(frozen=True, slots=True)
class DoorField:
    """One field the client door accepts and forwards for an operation."""

    name: str
    schema: Mapping[str, Any]
    #: MCC's own door refuses the request without it.
    required: bool = False


#: Per operation, the OpenAI door's request fields as openai-python 2.54.0
#: types them (``ImageGenerateParams``, ``ImageEditParams``,
#: ``SpeechCreateParams``, ``TranscriptionCreateParams``,
#: ``TranslationCreateParams``, ``VideoCreateParams``) minus ``model``, which
#: the schema states itself; plus, for a video, the four fields the Gemini
#: door maps a Veo ``:predictLongRunning`` onto (``core/gemini_api/media.py``
#: ``_VIDEO_PARAMETERS``). ``required`` is what MCC's door itself refuses
#: without: the text to work on, and the file for audio.
DOOR_FIELDS: Mapping[str, tuple[DoorField, ...]] = {
    MEDIA_OPERATION_IMAGE_GENERATE: (
        DoorField("prompt", _STRING, required=True),
        DoorField("background", _STRING),
        DoorField("moderation", _STRING),
        DoorField("n", _INTEGER),
        DoorField("output_compression", _INTEGER),
        DoorField("output_format", _STRING),
        DoorField("partial_images", _INTEGER),
        DoorField("quality", _STRING),
        DoorField("response_format", _STRING),
        DoorField("size", _STRING),
        DoorField("stream", _BOOLEAN),
        DoorField("style", _STRING),
        DoorField("user", _STRING),
    ),
    MEDIA_OPERATION_IMAGE_EDIT: (
        DoorField("prompt", _STRING, required=True),
        DoorField("image", _FILE),
        DoorField(
            "images",
            {
                "type": "array",
                "items": {"type": "object"},
            },
        ),
        DoorField("mask", _FILE),
        DoorField("background", _STRING),
        DoorField("input_fidelity", _STRING),
        DoorField("n", _INTEGER),
        DoorField("output_compression", _INTEGER),
        DoorField("output_format", _STRING),
        DoorField("partial_images", _INTEGER),
        DoorField("quality", _STRING),
        DoorField("response_format", _STRING),
        DoorField("size", _STRING),
        DoorField("stream", _BOOLEAN),
        DoorField("user", _STRING),
    ),
    MEDIA_OPERATION_SPEECH: (
        DoorField("input", _STRING, required=True),
        DoorField("voice", {"type": ["string", "object"]}),
        DoorField("instructions", _STRING),
        DoorField("response_format", _STRING),
        DoorField("speed", _NUMBER),
        DoorField("stream_format", _STRING),
    ),
    MEDIA_OPERATION_TRANSCRIBE: (
        DoorField("file", _FILE, required=True),
        DoorField("chunking_strategy", {"type": ["string", "object"]}),
        DoorField("include", _STRINGS),
        DoorField("keywords", _STRINGS),
        DoorField("known_speaker_names", _STRINGS),
        DoorField("known_speaker_references", _STRINGS),
        DoorField("language", _STRING),
        DoorField("languages", _STRINGS),
        DoorField("prompt", _STRING),
        DoorField("response_format", _STRING),
        DoorField("stream", _BOOLEAN),
        DoorField("temperature", _NUMBER),
        DoorField("timestamp_granularities", _STRINGS),
    ),
    MEDIA_OPERATION_TRANSLATE: (
        DoorField("file", _FILE, required=True),
        DoorField("prompt", _STRING),
        DoorField("response_format", _STRING),
        DoorField("temperature", _NUMBER),
    ),
    MEDIA_OPERATION_VIDEO_CREATE: (
        DoorField("prompt", _STRING, required=True),
        DoorField(
            "input_reference",
            {"anyOf": [dict(_FILE), {"type": "object"}]},
        ),
        DoorField("seconds", _STRING),
        DoorField("size", _STRING),
        # The Gemini-shaped door's Veo parameters, by the names Gemini's own
        # OpenAI layer documents for its create.
        DoorField("aspect_ratio", _STRING),
        DoorField("resolution", _STRING),
        DoorField("negative_prompt", _STRING),
        DoorField("seed", _STRING),
    ),
}

#: The operations a schema is published for, in the rails' order.
SCHEMA_OPERATIONS: tuple[str, ...] = tuple(DOOR_FIELDS)

#: The field that asks a surface for a streamed answer, per operation. Only
#: where MCC itself decides whether it is sent: ``stream_format`` (speech) is
#: forwarded as the client sent it unless the whole answer is served instead,
#: so it is left to the rungs.
_STREAM_FIELD: Mapping[str, str] = {
    MEDIA_OPERATION_IMAGE_GENERATE: "stream",
    MEDIA_OPERATION_IMAGE_EDIT: "stream",
    MEDIA_OPERATION_TRANSCRIBE: "stream",
}

#: What MCC's Gemini native adapter sends, per wire shape -- every other field
#: is ``not forwarded by MCC``. Mirrors ``providers/media/adapters.py``
#: (``_GEMINI_SPEECH_USED`` plus the documented voice; ``_GEMINI_TRANSCRIBE_USED``
#: plus the audio file), pinned by ``tests/application/test_media_schemas.py``.
GEMINI_NATIVE_FORWARDED: Mapping[str, frozenset[str]] = {
    MEDIA_SHAPE_GEMINI_TTS: frozenset(
        {"input", "response_format", "stream_format", "voice"}
    ),
    MEDIA_SHAPE_GEMINI_TRANSCRIBE: frozenset(
        {"language", "response_format", "stream", "file"}
    ),
}


# -- one rung's statement about one field -------------------------------------


@dataclass(frozen=True, slots=True)
class _Statement:
    state: str
    source: str
    since: str | None = None
    values: tuple[Any, ...] | None = None
    type: str | None = None
    note: str | None = None
    sent_as: str | None = None
    evidence: str | None = None
    fact_kind: str | None = None
    detail: str | None = None
    #: The moment this statement was last confirmed, for picking the newer of
    #: two contradicting refusals.
    confirmed: str = ""


def _operator_statements(
    operation: str, operator: OperationStatements
) -> dict[str, _Statement]:
    said: dict[str, _Statement] = {}
    for name, statement in (operator.get(operation) or {}).items():
        state = _OPERATOR_STATES.get(statement.state)
        if state is None:
            continue
        said[name] = _Statement(
            state=state,
            source=SOURCE_OPERATOR,
            since=statement.since,
            values=statement.values,
            type=statement.type,
        )
    return said


def _learned_statements(
    operation: str, learned: Iterable[Mapping[str, Any]]
) -> dict[str, _Statement]:
    """This operation's fresh refusals; the newer one wins a contradiction."""

    said: dict[str, _Statement] = {}
    prefix = f"{operation}:"
    for row in learned:
        state = _LEARNED_STATES.get(str(row.get("fact_kind") or ""))
        detail = str(row.get("detail") or "")
        if state is None or not detail.startswith(prefix) or row.get("stale"):
            continue
        name = detail[len(prefix) :]
        if not name:
            continue
        learned_at = str(row.get("learned_at") or "")
        confirmed = str(row.get("last_confirmed_at") or learned_at)
        candidate = _Statement(
            state=state,
            source=SOURCE_LEARNED,
            since=learned_at[:10] or None,
            evidence=str(row.get("evidence") or "") or None,
            fact_kind=str(row.get("fact_kind")),
            detail=detail,
            confirmed=confirmed,
        )
        existing = said.get(name)
        if existing is None or candidate.confirmed > existing.confirmed:
            said[name] = candidate
    return said


def _declared_statements(
    operation: str, surface: MediaSurface, door: Sequence[DoorField]
) -> dict[str, _Statement]:
    """What the provider's declared endpoint and MCC's adapter say."""

    said: dict[str, _Statement] = {}
    if surface.formats is not None:
        said["response_format"] = _Statement(
            state=STATE_STATED,
            source=SOURCE_PROVIDER_DECLARED,
            values=tuple(surface.formats),
        )
    if surface.voices is not None:
        said["voice"] = _Statement(
            state=STATE_STATED,
            source=SOURCE_PROVIDER_DECLARED,
            values=tuple(surface.voices),
            note="a voice outside this list is not sent; the host's default speaks",
        )
    stream_field = _STREAM_FIELD.get(operation)
    if stream_field is not None:
        said[stream_field] = (
            _Statement(
                state=STATE_STATED,
                source=SOURCE_PROVIDER_DECLARED,
                note="this host documents a streamed answer",
            )
            if surface.stream
            else _Statement(
                state=STATE_NOT_FORWARDED,
                source=SOURCE_PROVIDER_DECLARED,
                note="this host documents no stream, so a stream is never asked of it",
            )
        )
    if operation == MEDIA_OPERATION_SPEECH and surface.stream:
        said["stream_format"] = _Statement(
            state=STATE_STATED,
            source=SOURCE_PROVIDER_DECLARED,
            note="this host documents a streamed answer",
        )
    for client, wire in surface.renames:
        said[client] = _Statement(
            state=STATE_STATED,
            source=SOURCE_PROVIDER_DECLARED,
            sent_as=wire,
            note=f"sent to this host as {wire}",
        )
    forwarded = GEMINI_NATIVE_FORWARDED.get(surface.shape)
    if forwarded is not None:
        for field in door:
            if field.name not in forwarded and field.name not in said:
                said[field.name] = _Statement(
                    state=STATE_NOT_FORWARDED,
                    source=SOURCE_MCC_ADAPTER,
                    note="this host's native API has no field that means "
                    "exactly this, so MCC does not send it",
                )
    return said


def _listed_statements(
    door: Sequence[DoorField],
    supported_parameters: frozenset[str] | None,
    as_of: str | None,
) -> dict[str, _Statement]:
    """Door fields the provider's own ``/models`` row names as supported.

    A name the row leaves out is NOT a refusal -- a list of chat parameters
    says nothing about an images body -- and a name it adds that the door does
    not carry is not added: rung 4 fills gaps in the base set only.
    """

    if not supported_parameters:
        return {}
    return {
        field.name: _Statement(
            state=STATE_STATED,
            source=SOURCE_PROVIDER_LIST,
            since=as_of[:10] if as_of else None,
        )
        for field in door
        if field.name in supported_parameters
    }


# -- assembly -----------------------------------------------------------------


def _x_mcc(statement: _Statement) -> dict[str, Any]:
    record: dict[str, Any] = {
        "state": statement.state,
        "source": statement.source,
        "since": statement.since,
    }
    if statement.values is not None:
        record["values"] = list(statement.values)
    if statement.sent_as is not None:
        record["sent_as"] = statement.sent_as
    if statement.note is not None:
        record["note"] = statement.note
    if statement.evidence is not None:
        record["evidence"] = statement.evidence
    if statement.fact_kind is not None:
        record["fact_kind"] = statement.fact_kind
        record["detail"] = statement.detail
    return record


def _property(
    base: Mapping[str, Any] | None,
    said: Sequence[_Statement],
    values: tuple[Any, ...] | None,
    field_type: str | None,
) -> dict[str, Any]:
    winner = said[0]
    if winner.state in REFUSED_STATES:
        # JSON Schema's way of saying "must be absent", with room for x-mcc.
        prop: dict[str, Any] = {"not": {}}
    else:
        prop = dict(base) if base is not None else {}
        if field_type is not None:
            prop.pop("anyOf", None)
            prop.pop("format", None)
            prop["type"] = field_type
        if values is not None:
            prop["enum"] = list(values)
    record = _x_mcc(winner)
    if values is not None and "values" not in record:
        record["values"] = list(values)
    # What the rungs below the one that decided said about the same field:
    # an operator's "accepted" over a learned refusal still shows the
    # refusal, the way "Everything known" shows every source beside the one
    # used. The door's own default is not repeated.
    also = [
        {"state": other.state, "source": other.source, "since": other.since}
        for other in said[1:]
        if other.source != SOURCE_DOOR
    ]
    if also:
        record["also"] = also
    prop["x-mcc"] = record
    return prop


def _door_statement(field: DoorField) -> _Statement:
    if field.required:
        return _Statement(state=STATE_REQUIRED_STATED, source=SOURCE_DOOR)
    return _Statement(state=STATE_NOT_KNOWN, source=SOURCE_DOOR)


def request_schema(
    *,
    model_ref: str,
    operation: str,
    surface: MediaSurface,
    operator: OperationStatements,
    learned: Iterable[Mapping[str, Any]],
    supported_parameters: frozenset[str] | None = None,
    provider_list_as_of: str | None = None,
) -> dict[str, Any]:
    """One model's request schema for one operation it serves.

    ``learned`` is the model's learned-fact rows as the Models page holds them
    (``fact_kind``, ``detail``, ``learned_at``, ``last_confirmed_at``,
    ``stale``, ``evidence``); a stale row is not applied, exactly as a stale
    fact is not applied anywhere else.
    """

    door = DOOR_FIELDS[operation]
    rungs: list[dict[str, _Statement]] = [
        _operator_statements(operation, operator),
        _learned_statements(operation, learned),
        _declared_statements(operation, surface, door),
        _listed_statements(door, supported_parameters, provider_list_as_of),
    ]
    base_schema = {field.name: field.schema for field in door}
    door_said = {field.name: _door_statement(field) for field in door}
    names: list[str] = [field.name for field in door]
    for rung in rungs:
        names.extend(name for name in rung if name not in names)

    properties: dict[str, Any] = {
        "model": {
            "type": "string",
            "const": model_ref,
            "x-mcc": {
                "state": STATE_STATED,
                "source": SOURCE_RAIL,
                "since": None,
                "note": "this ref pins this model when it is on the rail",
            },
        }
    }
    required: list[str] = [field.name for field in door if field.required]
    for name in names:
        said = [rung[name] for rung in rungs if name in rung]
        if name in door_said:
            said.append(door_said[name])
        winner = said[0]
        values = next((s.values for s in said if s.values is not None), None)
        field_type = next((s.type for s in said if s.type is not None), None)
        properties[name] = _property(base_schema.get(name), said, values, field_type)
        if winner.state in REQUIRED_STATES and name not in required:
            required.append(name)

    schema: dict[str, Any] = {
        "$schema": JSON_SCHEMA_DIALECT,
        "type": "object",
        "properties": properties,
        "required": required,
    }
    if operation == MEDIA_OPERATION_IMAGE_EDIT:
        # MCC's door takes the picture to edit as an upload or as JSON
        # ``images``, and refuses a request with neither.
        schema["anyOf"] = [{"required": ["image"]}, {"required": ["images"]}]
    passthrough = surface.shape not in GEMINI_NATIVE_FORWARDED
    schema["additionalProperties"] = {
        "x-mcc": (
            {
                "state": STATE_NOT_KNOWN,
                "source": SOURCE_DOOR,
                "since": None,
                "note": "any other field is sent to the host unchanged",
            }
            if passthrough
            else {
                "state": STATE_NOT_FORWARDED,
                "source": SOURCE_MCC_ADAPTER,
                "since": None,
                "note": "this host's native API takes only the fields above",
            }
        )
    }
    schema["x-mcc"] = {
        "model": model_ref,
        "operation": operation,
        "encoding": (
            "json: a file upload is not sent to this host; a request carrying "
            "one skips this model uncharged"
            if surface.encoding == MEDIA_ENCODING_JSON
            else None
        ),
        "ladder": [
            {
                "source": source,
                "label": SOURCE_LABELS[source],
                "read": read,
                **({"why": why} if why else {}),
            }
            for source, read, why in LADDER
        ],
    }
    return schema


def request_schemas(
    *,
    model_ref: str,
    operations: Iterable[str],
    surfaces: Sequence[MediaSurface],
    operator: OperationStatements,
    learned: Sequence[Mapping[str, Any]],
    supported_parameters: frozenset[str] | None = None,
    provider_list_as_of: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Every schema one rail model has, by operation, for the ones it serves.

    An operation whose provider declares no surface is left out: the rail
    steps over this model for it without sending anything, so there is no
    request for a schema to describe.
    """

    schemas: dict[str, dict[str, Any]] = {}
    for operation in operations:
        if operation not in DOOR_FIELDS or operation in schemas:
            continue
        surface = surface_for(tuple(surfaces), operation)
        if surface is None:
            continue
        schemas[operation] = request_schema(
            model_ref=model_ref,
            operation=operation,
            surface=surface,
            operator=operator,
            learned=learned,
            supported_parameters=supported_parameters,
            provider_list_as_of=provider_list_as_of,
        )
    return schemas


@dataclass(frozen=True, slots=True)
class MediaSchemaInputs:
    """Everything a rail model's schemas are assembled from, bound once.

    A route binds it on the event loop -- the learned facts are the loop's --
    and hands it to a worker; every member is a snapshot, so nothing here can
    change under the assembly.
    """

    #: Learned-fact rows by ``provider/model`` (``learned_facts_by_model``).
    learned: Mapping[str, Sequence[Mapping[str, Any]]]
    #: The operator's own statements (``media_request_fields.json``).
    operator: MediaRequestFieldStatements
    #: The provider records the catalogue holds, by ``provider/model``.
    records: Mapping[str, ProviderModelInfo]
    #: When the catalogue those records came from was read (ISO), if known.
    records_as_of: str | None = None


def schema_inputs(
    learned: Mapping[str, Sequence[Mapping[str, Any]]],
    catalogue: Iterable[ProviderModelInfo],
    *,
    operator: MediaRequestFieldStatements,
    refreshed_at: object = None,
) -> MediaSchemaInputs:
    """Bind the inputs; ``refreshed_at`` is the catalogue's epoch, if known."""

    as_of = (
        datetime.fromtimestamp(float(refreshed_at), UTC).isoformat()
        if isinstance(refreshed_at, int | float) and not isinstance(refreshed_at, bool)
        else None
    )
    return MediaSchemaInputs(
        learned={key: tuple(rows) for key, rows in learned.items()},
        operator=operator,
        records={info.model_id: info for info in catalogue},
        records_as_of=as_of,
    )


def rail_model_schemas(
    model_ref: str,
    surfaces: Sequence[MediaSurface] | None,
    rails: Iterable[MediaRail],
    inputs: MediaSchemaInputs,
) -> dict[str, dict[str, Any]]:
    """One rail model's schemas, for the operations its rails route.

    ``surfaces`` is what its provider declares (``None``: a provider this
    install does not know, which the rail skips -- no schema).
    """

    if surfaces is None:
        return {}
    operations: list[str] = []
    for rail in rails:
        operations.extend(op for op in RAIL_OPERATIONS[rail] if op not in operations)
    info = inputs.records.get(model_ref)
    return request_schemas(
        model_ref=model_ref,
        operations=operations,
        surfaces=surfaces,
        operator=inputs.operator.for_model(model_ref),
        learned=inputs.learned.get(model_ref, ()),
        supported_parameters=None if info is None else info.supported_parameters,
        provider_list_as_of=inputs.records_as_of,
    )


# -- one rail, every model: which models accept each field (decision 2) -------

_BUCKETS: tuple[tuple[str, frozenset[str]], ...] = (
    ("accepted_by", ACCEPTED_STATES),
    ("required_by", REQUIRED_STATES),
    ("refused_by", REFUSED_STATES),
    ("not_forwarded_by", frozenset({STATE_NOT_FORWARDED})),
    ("not_known_for", frozenset({STATE_NOT_KNOWN})),
)


def field_acceptance(
    operation: str,
    models: Sequence[tuple[str, Mapping[str, Mapping[str, Any]]]],
) -> dict[str, Any]:
    """Per field, which of a rail's models accept, need, refuse or are silent.

    ``models`` is every model on the rail, in rail order, with its schemas by
    operation. Never only the primary and never only the overlap (user
    decision 2): a field one model needs and another refuses is listed under
    both, and a model that does not serve the operation at all is named too.
    """

    serving = [
        (ref, schemas[operation]) for ref, schemas in models if operation in schemas
    ]
    names: list[str] = []
    for _ref, schema in serving:
        names.extend(
            name
            for name in schema.get("properties", {})
            if name != "model" and name not in names
        )
    fields: dict[str, dict[str, list[str]]] = {}
    for name in names:
        entry: dict[str, list[str]] = {bucket: [] for bucket, _states in _BUCKETS}
        for ref, schema in serving:
            prop = schema.get("properties", {}).get(name)
            state = (
                prop["x-mcc"]["state"]
                if isinstance(prop, Mapping)
                else schema["additionalProperties"]["x-mcc"]["state"]
            )
            for bucket, states in _BUCKETS:
                if state in states:
                    entry[bucket].append(ref)
        fields[name] = entry
    return {
        "models": [ref for ref, _schema in serving],
        "not_served_by": [ref for ref, schemas in models if operation not in schemas],
        "fields": fields,
    }


def known_field_lines(
    schemas: Mapping[str, Mapping[str, Any]],
    *,
    source: str | None = None,
) -> list[str]:
    """``"video create · mode: required (learned)"`` for every field anything
    but the door said something about -- or, with ``source``, only what that
    rung said. What the "Everything known" view lists for a media model."""

    lines: list[str] = []
    for operation, schema in schemas.items():
        # The Models page's own chip words for the six routed operations.
        label = operation.replace("_", " ")
        for name, prop in schema.get("properties", {}).items():
            if name == "model" or not isinstance(prop, Mapping):
                continue
            record = prop.get("x-mcc") or {}
            if source is None:
                if record.get("source") != SOURCE_DOOR:
                    lines.append(f"{label} · {name}: {record.get('state')}")
                continue
            said = [record, *(record.get("also") or ())]
            lines.extend(
                f"{label} · {name}: {entry.get('state')}"
                for entry in said
                if entry.get("source") == source
            )
    return lines
