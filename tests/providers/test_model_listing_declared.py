"""What a provider's own ``/models`` row says a model is, kept on the record (7.79.0).

One generic reader, ``declared_from_row``, runs inside every listing parser and
keeps three statements as the provider published them: the modality pair, the
model-type word and the endpoint words. The rows below are trimmed from the
real keyless listings of 2026-10-08 (OpenRouter, Nous Portal, Kilo, Novita,
Vercel, Command Code, NVIDIA NIM, OpenCode Go, HyperCharm, Cline); only keys a
parser reads were kept.

What these hold: each provider's own shape is read by the same table, a row
that says nothing yields ``None``, a malformed optional field is "not stated"
and never a failed sweep, and nothing any parser produced before this reader
existed changes.
"""

from dataclasses import replace
from typing import Any

import pytest
from openai.types import Model

from my_claude_code.application.model_metadata import (
    DeclaredModalities,
    ProviderModelDeclaration,
    ProviderModelInfo,
)
from my_claude_code.providers.anthropic.models import extract_anthropic_model_infos
from my_claude_code.providers.commandcode.models import extract_commandcode_model_infos
from my_claude_code.providers.model_listing import (
    declared_from_row,
    extract_openai_model_infos,
    extract_openrouter_tool_model_infos,
    extract_tool_capable_model_infos,
)

OPENROUTER_ROW: dict[str, Any] = {
    "id": "stepfun/step-5-preview",
    "architecture": {
        "modality": "text+image+video->text",
        "input_modalities": ["text", "image", "video"],
        "output_modalities": ["text"],
        "tokenizer": "Other",
        "instruct_type": None,
    },
    "supported_parameters": ["reasoning", "tools"],
}
#: Nous Portal publishes an empty ``architecture`` for models it has not described.
NOUS_UNDESCRIBED_ROW: dict[str, Any] = {
    "id": "anthropic/claude-sonnet-5",
    "architecture": {},
    "supported_parameters": ["tools"],
}
KILO_ROW: dict[str, Any] = {
    "id": "kilo-auto/efficient",
    "architecture": {
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "tokenizer": "Other",
    },
    "supported_parameters": ["tools"],
}
NOVITA_ROWS: list[dict[str, Any]] = [
    {
        "id": "zai-org/glm-5.3-flash",
        "model_type": "chat",
        "input_modalities": ["text", "image", "video"],
        "output_modalities": ["text"],
        "endpoints": ["chat/completions", "anthropic", "responses"],
    },
    # Novita declares it chat and every kind; that stays as Novita says it.
    {
        "id": "ming-image-0.1-design",
        "model_type": "chat",
        "input_modalities": ["text", "image", "video", "audio"],
        "output_modalities": ["text", "image", "video", "audio"],
        "endpoints": None,
    },
]
VERCEL_ROWS: list[dict[str, Any]] = [
    {
        "id": "alibaba/qwen-3-14b",
        "type": "language",
        "modalities": {"input": ["text"], "output": ["text"]},
    },
    {
        "id": "bfl/flux-2-flex",
        "type": "image",
        "modalities": {"input": ["text"], "output": ["image"]},
    },
    {
        "id": "alibaba/qwen3-embedding-0.6b",
        "type": "embedding",
        "modalities": {"input": ["text"], "output": ["text"]},
    },
    {
        "id": "fish-audio/transcribe-1",
        "type": "transcription",
        "modalities": {"input": ["audio"], "output": ["text"]},
    },
]
COMMANDCODE_ROWS: list[dict[str, Any]] = [
    {
        "id": "claude-sonnet-5-5",
        "object": "model",
        "created": 1791464961,
        "owned_by": "command-code",
        "name": "Claude Sonnet 5.5",
        "context_length": 1000000,
        "supported_endpoints": ["/messages"],
    },
    {
        "id": "gpt-6-astra",
        "object": "model",
        "created": 1791464961,
        "owned_by": "command-code",
        "name": "GPT-6 Astra",
        "context_length": 1050000,
        "supported_endpoints": ["/chat/completions", "/responses"],
    },
    {
        "id": "deepseek/deepseek-v4-flash-fast",
        "object": "model",
        "created": 1791464961,
        "owned_by": "command-code",
        "name": "DeepSeek V4 Flash Fast",
        "context_length": 1000000,
        "supported_endpoints": ["/chat/completions"],
    },
]
#: Rows of lists that publish none of the three statements.
SILENT_ROWS: list[dict[str, Any]] = [
    {
        "id": "01-ai/yi-large",
        "object": "model",
        "created": 735790403,
        "owned_by": "01-ai",
    },
    {
        "id": "minimax-m3",
        "object": "model",
        "created": 1791464958,
        "owned_by": "opencode",
    },
    {
        "id": "deepseek-v4.1-flash",
        "object": "model",
        "created": 0,
        "owned_by": "hyper",
        "display_name": "DeepSeek V4.1 Flash",
        "context_window": 1048576,
        "max_output_tokens": 262144,
        "capabilities": {"vision": True},
    },
    {
        "id": "cline-pass/deepseek-v4.1-flash",
        "name": "cline-pass/deepseek-v4.1-flash",
        "description": "Smarter and more efficient, with 1M context window",
        "tags": [],
    },
]


def _by_id(infos: frozenset[ProviderModelInfo]) -> dict[str, ProviderModelInfo]:
    return {info.model_id: info for info in infos}


def _pair(inputs: tuple[str, ...], outputs: tuple[str, ...]) -> DeclaredModalities:
    return DeclaredModalities(inputs=inputs, outputs=outputs)


def test_the_openrouter_dialect_keeps_both_modality_lists() -> None:
    info = _by_id(
        extract_openrouter_tool_model_infos(
            {"data": [OPENROUTER_ROW]}, provider_name="T"
        )
    )["stepfun/step-5-preview"]

    assert info.declared == ProviderModelDeclaration(
        modalities=_pair(("image", "text", "video"), ("text",))
    )
    # Everything the dialect read before is read exactly as before.
    assert info.supports_vision is True
    assert info.supports_thinking is True


def test_a_kilo_row_is_read_by_the_same_table() -> None:
    infos = extract_tool_capable_model_infos({"data": [KILO_ROW]}, provider_name="K")

    (info,) = infos
    assert info.declared == ProviderModelDeclaration(
        modalities=_pair(("image", "text"), ("text",))
    )
    # This entry point never read vision, and still does not.
    assert info.supports_vision is None


def test_an_empty_architecture_states_nothing() -> None:
    """Nous publishes ``{}`` (and sometimes ``[]`` lists) for undescribed models."""

    info = _by_id(
        extract_openrouter_tool_model_infos(
            {"data": [NOUS_UNDESCRIBED_ROW]}, provider_name="N"
        )
    )["anthropic/claude-sonnet-5"]
    assert info.declared is None

    empty_lists = {
        **NOUS_UNDESCRIBED_ROW,
        "architecture": {"input_modalities": [], "output_modalities": ["text"]},
    }
    assert declared_from_row(empty_lists) is None


def test_novita_states_all_three_and_ming_stays_as_novita_declares_it() -> None:
    infos = _by_id(
        extract_openai_model_infos({"data": NOVITA_ROWS}, provider_name="NV")
    )

    assert infos["zai-org/glm-5.3-flash"].declared == ProviderModelDeclaration(
        modalities=_pair(("image", "text", "video"), ("text",)),
        model_type="chat",
        endpoints=("anthropic", "chat/completions", "responses"),
    )
    assert infos["ming-image-0.1-design"].declared == ProviderModelDeclaration(
        modalities=_pair(
            ("audio", "image", "text", "video"), ("audio", "image", "text", "video")
        ),
        model_type="chat",
        endpoints=None,
    )


def test_novita_rows_read_through_the_sdk_objects_the_same_way() -> None:
    """Novita's list is fetched through the OpenAI SDK, whose rows are objects."""

    rows = [
        Model.model_validate(
            {"created": 0, "object": "model", "owned_by": "novita", **row}
        )
        for row in NOVITA_ROWS
    ]
    from_objects = _by_id(
        extract_openai_model_infos({"data": rows}, provider_name="NV")
    )
    from_dicts = _by_id(
        extract_openai_model_infos({"data": NOVITA_ROWS}, provider_name="NV")
    )
    assert from_objects == from_dicts


@pytest.mark.parametrize(
    ("row", "model_type", "pair"),
    [
        (VERCEL_ROWS[0], "language", (("text",), ("text",))),
        (VERCEL_ROWS[1], "image", (("text",), ("image",))),
        (VERCEL_ROWS[2], "embedding", (("text",), ("text",))),
        (VERCEL_ROWS[3], "transcription", (("audio",), ("text",))),
    ],
)
def test_vercel_states_a_type_word_and_a_modality_pair(
    row: dict[str, Any], model_type: str, pair: tuple[tuple[str, ...], ...]
) -> None:
    (info,) = extract_openai_model_infos({"data": [row]}, provider_name="V")

    assert info.declared == ProviderModelDeclaration(
        modalities=_pair(*pair), model_type=model_type
    )


def test_command_code_keeps_its_endpoint_words_and_nothing_else_moves() -> None:
    infos = _by_id(
        extract_commandcode_model_infos({"data": COMMANDCODE_ROWS}, provider_name="CC")
    )

    assert infos["claude-sonnet-5-5"].declared == ProviderModelDeclaration(
        endpoints=("/messages",)
    )
    assert infos["gpt-6-astra"].declared == ProviderModelDeclaration(
        endpoints=("/chat/completions", "/responses")
    )
    assert infos["deepseek/deepseek-v4-flash-fast"].declared == (
        ProviderModelDeclaration(endpoints=("/chat/completions",))
    )
    assert replace(infos["gpt-6-astra"], declared=None) == ProviderModelInfo(
        model_id="gpt-6-astra", context_length=1050000
    )


@pytest.mark.parametrize("row", SILENT_ROWS, ids=lambda row: row["id"])
def test_a_row_that_states_nothing_yields_none(row: dict[str, Any]) -> None:
    assert declared_from_row(row) is None
    (info,) = extract_openai_model_infos({"data": [row]}, provider_name="S")
    assert info.declared is None
    assert info == ProviderModelInfo(model_id=row["id"])


def test_cline_rows_under_their_own_collection_are_read_too() -> None:
    (info,) = extract_openai_model_infos(
        {"clinePass": [SILENT_ROWS[3]]}, provider_name="C", collection_field="clinePass"
    )
    assert info.declared is None


def test_an_anthropic_row_keeps_its_type_word_verbatim() -> None:
    """The Anthropic list's ``type`` is recorded as published, never interpreted."""

    (info,) = extract_anthropic_model_infos(
        {
            "data": [
                {
                    "type": "model",
                    "id": "claude-opus-5-5",
                    "display_name": "Claude Opus 5.5",
                    "created_at": "2026-06-01T00:00:00Z",
                }
            ]
        },
        provider_name="A",
    )
    assert info.declared == ProviderModelDeclaration(model_type="model")


def test_the_summary_string_is_read_only_when_no_list_pair_exists() -> None:
    assert declared_from_row(
        {"id": "a", "architecture": {"modality": "text+image->text"}}
    ) == ProviderModelDeclaration(modalities=_pair(("image", "text"), ("text",)))
    # The lists are the richer statement: they win over their own abbreviation.
    assert declared_from_row(OPENROUTER_ROW) == ProviderModelDeclaration(
        modalities=_pair(("image", "text", "video"), ("text",))
    )
    for summary in ("text", "text->", "->text", "text->image->text", 7):
        assert declared_from_row({"architecture": {"modality": summary}}) is None


def test_words_are_lower_cased_deduplicated_and_sorted_and_nothing_else() -> None:
    declared = declared_from_row(
        {
            "input_modalities": ["Text", " text ", "IMAGE"],
            "output_modalities": ["text"],
            "type": " Language ",
            "supported_endpoints": ["/Responses", "/chat/completions", "/responses"],
        }
    )
    assert declared == ProviderModelDeclaration(
        modalities=_pair(("image", "text"), ("text",)),
        model_type="language",
        endpoints=("/chat/completions", "/responses"),
    )


def test_a_malformed_path_defers_to_the_next_path_that_states_it() -> None:
    declared = declared_from_row(
        {
            "architecture": {"input_modalities": [None], "output_modalities": ["text"]},
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "model_type": 42,
            "type": "chat",
            "supported_endpoint_types": "openai",
            "supported_endpoints": [],
            "endpoints": ["chat/completions"],
        }
    )
    assert declared == ProviderModelDeclaration(
        modalities=_pair(("text",), ("text",)),
        model_type="chat",
        endpoints=("chat/completions",),
    )


def _poisoned(row: dict[str, Any]) -> dict[str, Any]:
    return {
        **row,
        "architecture": {
            **(row.get("architecture") or {}),
            "input_modalities": [None, 7],
            "output_modalities": "text",
            "modality": "text->->text",
        },
        "input_modalities": {"text": True},
        "output_modalities": [],
        "modalities": ["text", "image"],
        "model_type": 42,
        "type": {"kind": "chat"},
        "reported_type": "",
        "supported_endpoint_types": "chat",
        "supported_endpoints": [None],
        "endpoints": [{"path": "/chat/completions"}],
    }


def test_every_declared_path_malformed_never_fails_a_sweep() -> None:
    """Optional fields nobody required must never turn into a failed sweep."""

    generic_rows = [*NOVITA_ROWS, *VERCEL_ROWS, *SILENT_ROWS[:3]]
    poisoned_generic = extract_openai_model_infos(
        {"data": [_poisoned(row) for row in generic_rows]}, provider_name="G"
    )
    assert sorted(info.model_id for info in poisoned_generic) == sorted(
        row["id"] for row in generic_rows
    )
    assert all(info.declared is None for info in poisoned_generic)

    dialect_rows = [OPENROUTER_ROW, NOUS_UNDESCRIBED_ROW, KILO_ROW]
    poisoned_dialect = extract_openrouter_tool_model_infos(
        {"data": [_poisoned(row) for row in dialect_rows]}, provider_name="O"
    )
    assert {info.model_id for info in poisoned_dialect} == {
        row["id"] for row in dialect_rows
    }
    assert all(info.declared is None for info in poisoned_dialect)

    poisoned_cc = extract_commandcode_model_infos(
        {"data": [_poisoned(row) for row in COMMANDCODE_ROWS]}, provider_name="CC"
    )
    assert all(info.declared is None for info in poisoned_cc)


def test_an_attribute_that_raises_is_not_stated() -> None:
    class Hostile:
        id = "x"

        def __getattr__(self, name: str) -> Any:
            raise RuntimeError(name)

    assert declared_from_row(Hostile()) is None


def test_an_alias_states_what_its_row_states() -> None:
    """xAI lists aliases as their own ids; each is the same row under a name."""

    infos = _by_id(
        extract_openai_model_infos(
            {
                "models": [
                    {
                        "id": "grok-5",
                        "aliases": ["grok-5-latest"],
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                    }
                ]
            },
            provider_name="X",
            collection_field="models",
            aliases_field="aliases",
        )
    )
    assert infos["grok-5"].declared == infos["grok-5-latest"].declared
    assert infos["grok-5"].declared == ProviderModelDeclaration(
        modalities=_pair(("image", "text"), ("text",))
    )


def test_membership_filters_are_unchanged_by_the_reader() -> None:
    """chutes' required lists decide the row; the reader only reads included rows."""

    rows = [
        {
            "id": "keep",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "supported_features": ["tools"],
        },
        {
            "id": "drop-image-out",
            "input_modalities": ["text"],
            "output_modalities": ["image"],
            "supported_features": ["tools"],
        },
    ]
    infos = extract_openai_model_infos(
        {"data": rows},
        provider_name="CH",
        required_sequence_items=(
            ("input_modalities", "text"),
            ("output_modalities", "text"),
            ("supported_features", "tools"),
        ),
        exclude_missing_sequence_fields=True,
        tags_field="supported_features",
    )
    assert [info.model_id for info in infos] == ["keep"]
