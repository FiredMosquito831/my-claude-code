"""Per-model media request schemas (7.98.0): the ladder, its states, its guards.

``application/media/schemas.py`` publishes, per (rail model, operation), the
fields that model's request takes, from the operator's statement down to the
endpoint's own contract. Publish-only: these tests hold what it says and where
each statement came from; ``tests/providers/test_media_learned_params.py``
holds that learning one changes nothing a request does.
"""

import ast
import inspect
import typing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.application.media import schemas
from my_claude_code.application.media.schemas import (
    DOOR_FIELDS,
    GEMINI_NATIVE_FORWARDED,
    STATE_NOT_FORWARDED,
    STATE_NOT_KNOWN,
    STATE_REFUSED_LEARNED,
    STATE_REFUSED_STATED,
    STATE_REQUIRED_LEARNED,
    STATE_REQUIRED_STATED,
    STATE_STATED,
    field_acceptance,
    request_schema,
    request_schemas,
)
from my_claude_code.config.media_request_fields import (
    EMPTY_STATEMENTS,
    OperatorFieldStatement,
    with_statement,
)
from my_claude_code.config.media_surfaces import (
    MEDIA_SHAPE_GEMINI_TRANSCRIBE,
    MEDIA_SHAPE_GEMINI_TTS,
    MediaSurface,
    custom_media_surfaces,
    gemini_speech_surface,
    gemini_transcription_surface,
    image_generation_surface,
    speech_surface,
    video_surfaces,
)
from my_claude_code.providers.media import adapters
from my_claude_code.providers.recovery import (
    FACT_MEDIA_PARAM_REFUSED,
    FACT_MEDIA_PARAM_REQUIRED,
)
from my_claude_code.providers.recovery.store import LearnedFactStore

AGNES_VIDEO = "custom_agnes/agnes-video-2.5"
AGNES_IMAGE = "custom_agnes/agnes-image-1"
VIDEO = video_surfaces(content_fallback="videos/{id}/content")[0]


def _learned(kind: str, detail: str, *, days_ago: float = 0, stale=False):
    when = (datetime.now(UTC) - timedelta(days=days_ago)).replace(microsecond=0)
    stamp = when.isoformat().replace("+00:00", "Z")
    return {
        "fact_kind": kind,
        "detail": detail,
        "learned_at": stamp,
        "last_confirmed_at": stamp,
        "stale": stale,
        "evidence": "the host's words",
    }


def _schema(surface: MediaSurface, operation: str, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("operator", {})
    kwargs.setdefault("learned", [])
    return request_schema(
        model_ref=kwargs.pop("model_ref", AGNES_VIDEO),
        operation=operation,
        surface=surface,
        **kwargs,
    )


def _state(schema: dict[str, Any], name: str) -> str:
    return schema["properties"][name]["x-mcc"]["state"]


def test_unknown_field_is_not_known_not_unsupported() -> None:
    """IV.16: a field nobody spoke about is ``forwarded, not known``.

    Never refused, never left out: the endpoint's contract lists it, MCC
    sends it, and no source said whether this model takes it.
    """

    schema = _schema(VIDEO, "video_create")

    assert _state(schema, "seconds") == STATE_NOT_KNOWN
    assert _state(schema, "size") == STATE_NOT_KNOWN
    assert schema["properties"]["size"]["x-mcc"]["source"] == "door"
    assert "not" not in schema["properties"]["size"]
    # What the door itself needs is required, from the door.
    assert _state(schema, "prompt") == STATE_REQUIRED_STATED
    assert schema["required"] == ["prompt"]
    # Anything else a client sends is forwarded unchanged -- also not known.
    assert schema["additionalProperties"]["x-mcc"]["state"] == STATE_NOT_KNOWN
    every = {name: _state(schema, name) for name in schema["properties"]}
    assert not any("refused" in state for state in every.values())


def test_declared_formats_and_voices_become_enums() -> None:
    tts = _schema(gemini_speech_surface(), "speech", model_ref="gemini/tts")

    voice = tts["properties"]["voice"]
    assert voice["x-mcc"]["state"] == STATE_STATED
    assert voice["x-mcc"]["source"] == "provider_declared"
    assert len(voice["enum"]) == 30
    assert voice["x-mcc"]["values"] == voice["enum"]
    assert tts["properties"]["response_format"]["enum"] == ["wav", "pcm"]
    # MCC's own adapter for a native Gemini body cannot carry these.
    assert _state(tts, "instructions") == STATE_NOT_FORWARDED
    assert tts["properties"]["speed"]["x-mcc"]["source"] == "mcc_adapter"
    assert tts["additionalProperties"]["x-mcc"]["state"] == STATE_NOT_FORWARDED

    speech = _schema(
        speech_surface(formats=("mp3", "wav")), "speech", model_ref="together/tts"
    )
    assert speech["properties"]["response_format"]["enum"] == ["mp3", "wav"]
    # No voice list declared: the host judges the name.
    assert _state(speech, "voice") == STATE_NOT_KNOWN


def test_learned_required_param_outranks_declaration() -> None:
    """A host's own refusal outranks the catalogue's declaration (rung 2 > 3).

    The declaration's values still fill the gap the refusal leaves.
    """

    tts = _schema(
        gemini_speech_surface(),
        "speech",
        model_ref="gemini/tts",
        learned=[_learned(FACT_MEDIA_PARAM_REQUIRED, "speech:voice")],
    )
    voice = tts["properties"]["voice"]
    assert voice["x-mcc"]["state"] == STATE_REQUIRED_LEARNED
    assert voice["x-mcc"]["source"] == "learned"
    assert len(voice["enum"]) == 30
    assert voice["x-mcc"]["also"] == [
        {"state": STATE_STATED, "source": "provider_declared", "since": None}
    ]
    assert "voice" in tts["required"]

    # The user's real Agnes refusals: a field no source lists at all.
    video = _schema(
        VIDEO,
        "video_create",
        learned=[_learned(FACT_MEDIA_PARAM_REQUIRED, "video_create:mode")],
    )
    mode = video["properties"]["mode"]
    assert mode["x-mcc"]["state"] == STATE_REQUIRED_LEARNED
    assert mode["x-mcc"]["evidence"] == "the host's words"
    assert mode["x-mcc"]["fact_kind"] == FACT_MEDIA_PARAM_REQUIRED
    assert mode["x-mcc"]["detail"] == "video_create:mode"
    assert video["required"] == ["prompt", "mode"]

    image = _schema(
        image_generation_surface(),
        "image_generate",
        model_ref=AGNES_IMAGE,
        learned=[_learned(FACT_MEDIA_PARAM_REFUSED, "image_generate:image_config")],
    )
    refused = image["properties"]["image_config"]
    assert refused["x-mcc"]["state"] == STATE_REFUSED_LEARNED
    # JSON Schema's own "must be absent".
    assert refused["not"] == {}
    assert "image_config" not in image["required"]


def test_a_learned_fact_is_per_operation() -> None:
    """A refusal on image generation says nothing about image edits."""

    learned = [_learned(FACT_MEDIA_PARAM_REFUSED, "image_generate:image_config")]
    surfaces = custom_media_surfaces(("image_generate", "image_edit"))
    both = request_schemas(
        model_ref=AGNES_IMAGE,
        operations=("image_generate", "image_edit"),
        surfaces=surfaces,
        operator={},
        learned=learned,
    )
    assert "image_config" in both["image_generate"]["properties"]
    assert "image_config" not in both["image_edit"]["properties"]


def test_operator_statement_outranks_learned() -> None:
    """Rung 1 over rung 2 -- and the refusal is still shown beside it."""

    statements = with_statement(
        EMPTY_STATEMENTS,
        AGNES_VIDEO,
        "video_create",
        "mode",
        OperatorFieldStatement(
            state="accepted", values=("std", "pro"), since="2026-10-18"
        ),
    )
    video = _schema(
        VIDEO,
        "video_create",
        operator=statements.for_model(AGNES_VIDEO),
        learned=[_learned(FACT_MEDIA_PARAM_REQUIRED, "video_create:mode")],
    )
    mode = video["properties"]["mode"]
    assert mode["x-mcc"]["state"] == STATE_STATED
    assert mode["x-mcc"]["source"] == "operator"
    assert mode["x-mcc"]["since"] == "2026-10-18"
    assert mode["enum"] == ["std", "pro"]
    assert [entry["source"] for entry in mode["x-mcc"]["also"]] == ["learned"]
    assert video["required"] == ["prompt"]

    refused = with_statement(
        EMPTY_STATEMENTS,
        AGNES_VIDEO,
        "video_create",
        "seconds",
        OperatorFieldStatement(state="refused"),
    )
    video = _schema(VIDEO, "video_create", operator=refused.for_model(AGNES_VIDEO))
    assert _state(video, "seconds") == STATE_REFUSED_STATED
    assert video["properties"]["seconds"]["not"] == {}


def test_a_newer_refusal_wins_a_contradiction() -> None:
    video = _schema(
        VIDEO,
        "video_create",
        learned=[
            _learned(FACT_MEDIA_PARAM_REQUIRED, "video_create:mode", days_ago=3),
            _learned(FACT_MEDIA_PARAM_REFUSED, "video_create:mode", days_ago=1),
        ],
    )
    assert _state(video, "mode") == STATE_REFUSED_LEARNED


def test_a_stale_learned_fact_is_not_applied() -> None:
    """TTL honoured: a stale row is not applied here, as nowhere else."""

    video = _schema(
        VIDEO,
        "video_create",
        learned=[_learned(FACT_MEDIA_PARAM_REQUIRED, "video_create:mode", stale=True)],
    )
    assert "mode" not in video["properties"]
    assert video["required"] == ["prompt"]


def test_forget_retires_a_learned_param() -> None:
    """Forget on one field removes that field's row and no other."""

    store = LearnedFactStore(flush_debounce_seconds=0.0)
    store.record(
        "custom_agnes",
        "agnes-video-2.5",
        FACT_MEDIA_PARAM_REQUIRED,
        True,
        detail="video_create:mode",
        evidence="mode is required",
    )
    store.record(
        "custom_agnes",
        "agnes-video-2.5",
        FACT_MEDIA_PARAM_REQUIRED,
        True,
        detail="video_create:quality",
        evidence="quality is required",
    )

    def rows() -> list[dict[str, Any]]:
        now = datetime.now(UTC)
        return [
            {**fact.as_row(), "detail": fact.detail, "stale": fact.is_stale(now)}
            for fact in store.all_facts()
        ]

    before = _schema(VIDEO, "video_create", learned=rows())
    assert before["required"] == ["prompt", "mode", "quality"]

    forgotten = store.forget(
        "custom_agnes",
        "agnes-video-2.5",
        FACT_MEDIA_PARAM_REQUIRED,
        "video_create:mode",
    )
    assert forgotten == 1
    after = _schema(VIDEO, "video_create", learned=rows())
    assert "mode" not in after["properties"]
    assert after["required"] == ["prompt", "quality"]


def test_the_provider_list_states_only_door_fields_it_names() -> None:
    """Rung 4 fills gaps in the base set; a chat name it lists adds nothing,
    and a door field it leaves out is still not known -- never refused."""

    image = _schema(
        image_generation_surface(),
        "image_generate",
        model_ref=AGNES_IMAGE,
        supported_parameters=frozenset({"size", "n", "temperature", "max_tokens"}),
        provider_list_as_of="2026-10-17T09:00:00+00:00",
    )
    assert _state(image, "size") == STATE_STATED
    assert image["properties"]["size"]["x-mcc"]["source"] == "provider_list"
    assert image["properties"]["size"]["x-mcc"]["since"] == "2026-10-17"
    assert _state(image, "quality") == STATE_NOT_KNOWN
    assert "temperature" not in image["properties"]


def test_openrouter_and_litellm_rungs_are_declared_and_inert() -> None:
    ladder = _schema(VIDEO, "video_create")["x-mcc"]["ladder"]
    by_source = {rung["source"]: rung for rung in ladder}
    assert [rung["source"] for rung in ladder] == [
        "operator",
        "learned",
        "provider_declared",
        "mcc_adapter",
        "provider_list",
        "openrouter_live",
        "litellm",
    ]
    assert by_source["openrouter_live"]["read"] is False
    assert by_source["litellm"]["read"] is False
    assert "models_dev" not in by_source


def test_rail_disagreement_one_entry_each_with_per_field_acceptance() -> None:
    """User decision 2: two models on one rail disagree about ``mode`` --
    each keeps its own schema, and the rail says which accepts, needs,
    refuses or is silent about every field. Never only the primary, never
    only the overlap."""

    needs = request_schemas(
        model_ref=AGNES_VIDEO,
        operations=("video_create",),
        surfaces=custom_media_surfaces(("video",)),
        operator={},
        learned=[_learned(FACT_MEDIA_PARAM_REQUIRED, "video_create:mode")],
    )
    refuses = request_schemas(
        model_ref="open_router/google/veo-3.1",
        operations=("video_create",),
        surfaces=video_surfaces(renames=(("seconds", "duration"),)),
        operator={},
        learned=[_learned(FACT_MEDIA_PARAM_REFUSED, "video_create:mode")],
    )
    images_only = request_schemas(
        model_ref=AGNES_IMAGE,
        operations=("video_create",),
        surfaces=custom_media_surfaces(("image_generate",)),
        operator={},
        learned=[],
    )
    rail = [
        (AGNES_VIDEO, needs),
        ("open_router/google/veo-3.1", refuses),
        (AGNES_IMAGE, images_only),
    ]

    acceptance = field_acceptance("video_create", rail)

    assert acceptance["models"] == [AGNES_VIDEO, "open_router/google/veo-3.1"]
    assert acceptance["not_served_by"] == [AGNES_IMAGE]
    mode = acceptance["fields"]["mode"]
    assert mode["required_by"] == [AGNES_VIDEO]
    assert mode["accepted_by"] == [AGNES_VIDEO]
    assert mode["refused_by"] == ["open_router/google/veo-3.1"]
    seconds = acceptance["fields"]["seconds"]
    # The renamed field is stated by the second model's declaration.
    assert seconds["accepted_by"] == ["open_router/google/veo-3.1"]
    assert seconds["not_known_for"] == [AGNES_VIDEO]
    assert refuses["video_create"]["properties"]["seconds"]["x-mcc"]["sent_as"] == (
        "duration"
    )


def test_the_door_fields_match_the_openai_sdk_types() -> None:
    """The base set is the endpoint's published contract (openai-python).

    Drift guard: when the SDK MCC depends on grows a field, this fails and
    the base set is extended -- never narrowed.
    """

    from openai.types import (
        image_edit_params,
        image_generate_params,
        video_create_params,
    )
    from openai.types.audio import (
        speech_create_params,
        transcription_create_params,
        translation_create_params,
    )

    def fields(cls: type) -> set[str]:
        return set(typing.get_type_hints(cls)) - {"model"}

    gemini_door_video = {"aspect_ratio", "resolution", "negative_prompt", "seed"}
    expected = {
        "image_generate": fields(image_generate_params.ImageGenerateParamsBase)
        | {"stream"},
        "image_edit": fields(image_edit_params.ImageEditParamsBase)
        | {"stream", "images"},
        "speech": fields(speech_create_params.SpeechCreateParams),
        "transcribe": fields(transcription_create_params.TranscriptionCreateParamsBase)
        | {"stream"},
        "translate": fields(translation_create_params.TranslationCreateParams),
        "video_create": fields(video_create_params.VideoCreateParams)
        | gemini_door_video,
    }
    actual = {
        operation: {field.name for field in door}
        for operation, door in DOOR_FIELDS.items()
    }
    for operation, names in expected.items():
        assert names <= actual[operation], (operation, names - actual[operation])


def test_gemini_native_forwarded_mirrors_the_adapter() -> None:
    """The mirror ``application`` keeps of what the adapter sends (it may not
    import ``providers``), pinned to the adapter's own sets."""

    assert GEMINI_NATIVE_FORWARDED[MEDIA_SHAPE_GEMINI_TTS] == (
        adapters._GEMINI_SPEECH_USED | {"voice"}
    )
    assert GEMINI_NATIVE_FORWARDED[MEDIA_SHAPE_GEMINI_TRANSCRIBE] == (
        adapters._GEMINI_TRANSCRIBE_USED | {"file"}
    )
    asr = _schema(gemini_transcription_surface(), "transcribe", model_ref="gemini/asr")
    assert _state(asr, "language") == STATE_NOT_KNOWN
    assert _state(asr, "temperature") == STATE_NOT_FORWARDED


def test_the_fact_kind_names_match_the_store() -> None:
    assert schemas.FACT_MEDIA_PARAM_REQUIRED == FACT_MEDIA_PARAM_REQUIRED
    assert schemas.FACT_MEDIA_PARAM_REFUSED == FACT_MEDIA_PARAM_REFUSED


def test_every_schema_is_valid_json_schema_2020_12() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    video = _schema(
        VIDEO,
        "video_create",
        learned=[
            _learned(FACT_MEDIA_PARAM_REQUIRED, "video_create:mode"),
            _learned(FACT_MEDIA_PARAM_REFUSED, "video_create:image_config"),
        ],
    )
    validator = jsonschema.Draft202012Validator
    validator.check_schema(video)
    checker = validator(video)
    assert checker.is_valid({"model": AGNES_VIDEO, "prompt": "a cat", "mode": "std"})
    # The two Agnes refusals, as the schema now says them.
    assert not checker.is_valid({"model": AGNES_VIDEO, "prompt": "a cat"})
    assert not checker.is_valid(
        {"prompt": "a cat", "mode": "std", "image_config": {"aspect_ratio": "1:1"}}
    )
    for operation, door in DOOR_FIELDS.items():
        surface = MediaSurface(operation=operation, shape="openai_images", path="x")
        validator.check_schema(_schema(surface, operation))
        assert door


def test_no_model_name_branch() -> None:
    """IV.6: the assembler never branches on a model's name.

    Static: no comparison in the module puts a name that holds a model or
    a ref beside a string literal, and no vendor or model name is spelled.
    """

    source = Path(inspect.getfile(schemas)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    def names(node: ast.AST) -> set[str]:
        found: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                found.add(child.id)
            elif isinstance(child, ast.Attribute):
                found.add(child.attr)
        return found

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            parts = [node.left, *node.comparators]
            mentions_model = any(
                "model" in name.lower() for part in parts for name in names(part)
            )
            has_literal = any(
                isinstance(part, ast.Constant) and isinstance(part.value, str)
                for part in parts
            )
            if mentions_model and has_literal:
                offenders.append(ast.unparse(node))
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"startswith", "endswith"}
            and any("model" in name.lower() for name in names(node.func.value))
        ):
            offenders.append(ast.unparse(node))
    assert offenders == []
    # Every string the code holds -- docstrings, which only tell the story,
    # left out -- and every identifier.
    docstrings = {
        id(body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.ClassDef)
        and (body := node.body)
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
    }
    lowered = " ".join(
        [
            str(node.value).lower()
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ]
        + [name.lower() for name in names(tree)]
    )
    for vendor in (
        "agnes",
        "gpt-",
        "dall-e",
        "sora",
        "veo",
        "imagen",
        "flux",
        "whisper",
        "tts-1",
        "grok",
        "gemini-",
    ):
        assert vendor not in lowered, vendor
