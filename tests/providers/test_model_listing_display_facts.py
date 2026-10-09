"""Each list's own dates, cutoff and description, kept for the Models page (7.85.0).

The provider rung of four display rows (spec PR-METADATA-LADDER-REINFORCEMENT
§4.7, user decisions 2026-10-08 20:57-21:00): the day the provider's list says
it published the model, the day it retires it, its knowledge cutoff and its
description. Nothing routes, lists or prices on any of them, so no record field
is filled from them.

The publication day has a data rule, decided over the whole listing rather than
per provider: a list whose every row carries one ``created`` value is stamping
when it was served (NVIDIA NIM, OpenCode Zen and Go, Command Code, Vercel), and
``0`` (HyperCharm, Kilo's routers) states nothing. The OpenRouter dialect's
``created`` is the day OpenRouter listed the model -- Nous Portal and Kilo copy
OpenRouter's value -- and is never read as a publication day.

Rows trimmed from the real keyless listings of 2026-10-08.
"""

from dataclasses import replace
from typing import Any

import pytest

from my_claude_code.application.model_metadata import (
    ProviderModelDeclaration,
    ProviderModelInfo,
)
from my_claude_code.providers.anthropic.models import extract_anthropic_model_infos
from my_claude_code.providers.commandcode.models import extract_commandcode_model_infos
from my_claude_code.providers.model_listing import (
    declared_from_row,
    extract_openai_model_infos,
    extract_openrouter_tool_model_infos,
    listing_date_field,
    record_with_declared,
)

#: Novita: ``created`` differs on every row (121 distinct of 121) -- real dates.
NOVITA_ROWS: list[dict[str, Any]] = [
    {
        "id": "zai-org/glm-5.3-flash",
        "created": 1787754787,
        "description": (
            "GLM-5.3-Flash is a native multimodal model from Z.ai. It is suited "
            "for efficient coding and long-horizon agent tasks."
        ),
        "status": 1,
    },
    {
        "id": "deepseek/deepseek-v4-pro-0813-p",
        "created": 1790471016,
        "description": "",
        "status": 1,
    },
]
#: NVIDIA NIM: one ``created`` on all 80 rows -- a stamp.
NVIDIA_ROWS: list[dict[str, Any]] = [
    {"id": "01-ai/yi-large", "object": "model", "created": 735790403},
    {"id": "meta/llama-3.1-8b-instruct", "object": "model", "created": 735790403},
]
#: HyperCharm: ``created`` is 0 on all 23 rows -- not stated.
HYPERCHARM_ROWS: list[dict[str, Any]] = [
    {"id": "deepseek-v4.1-flash", "created": 0},
    {"id": "inkling", "created": 0},
]
#: Vercel: ``created`` is one stamp (1755815280 on all 412 rows); ``released``
#: (epoch seconds) is the row's own date; ``deprecated_at`` is in milliseconds.
VERCEL_ROWS: list[dict[str, Any]] = [
    {
        "id": "openai/gpt-4o-mini-transcribe",
        "created": 1755815280,
        "released": 1710288000,
        "deprecated_at": 1803600000000,
        "knowledge": "2024-06-01",
        "description": "GPT-4o mini Transcribe is a speech-to-text model.",
        "type": "transcription",
    },
    {
        "id": "openai/whisper-1",
        "created": 1755815280,
        "released": 1663718400,
        "deprecated_at": 1803600000000,
        "knowledge": None,
        "description": "Whisper is a general-purpose speech recognition model.",
    },
]
#: The OpenRouter dialect (OpenRouter, Nous Portal, Kilo).
OPENROUTER_ROW: dict[str, Any] = {
    "id": "qwen/qwen3-vl-30b-a3b-thinking",
    "created": 1759794479,
    "expiration_date": "2026-10-09",
    "knowledge_cutoff": "2025-03-31",
    "description": "Qwen3-VL-30B-A3B-Thinking is a multimodal model.",
    "supported_parameters": ["tools", "tool_choice", "reasoning"],
}
NOUS_ROW: dict[str, Any] = {
    "id": "bytedance-seed/seed-2.0-code",
    "created": 1786550701,
    "expiration_date": "2026-11-11",
    "knowledge_cutoff": None,
    "description": "Seed 2.0 Code is a model from ByteDance Seed.",
    "supported_parameters": ["tools"],
}
#: Command Code: one ``created`` on all 87 rows -- a stamp.
COMMANDCODE_ROWS: list[dict[str, Any]] = [
    {
        "id": "claude-sonnet-5-5",
        "created": 1791464961,
        "name": "Claude Sonnet 5.5",
        "context_length": 1000000,
        "supported_endpoints": ["/messages"],
    },
    {
        "id": "gpt-6-astra",
        "created": 1791464961,
        "name": "GPT-6 Astra",
        "context_length": 1050000,
        "supported_endpoints": ["/chat/completions", "/responses"],
    },
]


def _by_id(infos: frozenset[ProviderModelInfo]) -> dict[str, ProviderModelInfo]:
    return {info.model_id: info for info in infos}


def _declared(info: ProviderModelInfo) -> ProviderModelDeclaration:
    assert info.declared is not None, info.model_id
    return info.declared


# ------------------------------------------------------- the constant-stamp rule


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        (NOVITA_ROWS, "created"),
        (NVIDIA_ROWS, None),
        (HYPERCHARM_ROWS, None),
        (VERCEL_ROWS, None),
        (COMMANDCODE_ROWS, None),
        ([NOVITA_ROWS[0]], None),  # one row: a date and a stamp look the same
        ([], None),
    ],
    ids=[
        "varies",
        "stamp",
        "zeros",
        "vercel-stamp",
        "commandcode-stamp",
        "one",
        "empty",
    ],
)
def test_a_listing_date_is_read_only_where_the_listing_varies(rows, expected) -> None:
    assert listing_date_field(rows) == expected


def test_zeros_are_absent_and_the_rest_still_decide() -> None:
    """Kilo lists 0 for its five routers beside real dates on the other rows."""

    rows = [{"created": 0}, {"created": 1759794479}, {"created": 1786550701}]
    assert listing_date_field(rows) == "created"


def test_created_at_is_the_second_field_the_rule_reads() -> None:
    """Anthropic's list names it ``created_at`` (an RFC 3339 string)."""

    rows = [
        {"id": "a", "created_at": "2025-02-24T00:00:00Z"},
        {"id": "b", "created_at": "2025-05-22T00:00:00Z"},
    ]
    assert listing_date_field(rows) == "created_at"


def test_a_listing_rule_that_cannot_read_its_rows_reads_nothing() -> None:
    assert listing_date_field([None, 7, "x", {"created": True}]) is None


# -------------------------------------------------------------- per parser


def test_novita_s_own_created_is_its_publication_day() -> None:
    infos = _by_id(extract_openai_model_infos({"data": NOVITA_ROWS}, provider_name="N"))
    first = _declared(infos["zai-org/glm-5.3-flash"])
    assert first.published_at == "2026-08-26"
    assert first.description is not None
    assert first.description.startswith("GLM-5.3-Flash is a native")
    second = _declared(infos["deepseek/deepseek-v4-pro-0813-p"])
    assert second.published_at == "2026-09-27"
    assert second.description is None  # an empty string states nothing


def test_a_stamped_list_states_no_publication_day() -> None:
    nvidia = extract_openai_model_infos({"data": NVIDIA_ROWS}, provider_name="NV")
    assert all(info.declared is None for info in nvidia)
    hyper = extract_openai_model_infos({"data": HYPERCHARM_ROWS}, provider_name="H")
    assert all(info.declared is None for info in hyper)


def test_vercel_reads_released_retirement_and_cutoff_never_its_stamp() -> None:
    infos = _by_id(extract_openai_model_infos({"data": VERCEL_ROWS}, provider_name="V"))
    mini = _declared(infos["openai/gpt-4o-mini-transcribe"])
    assert mini.published_at == "2024-03-13"  # released, not the 2025-08-21 stamp
    assert mini.retires_at == "2027-02-26"  # deprecated_at in milliseconds
    assert mini.knowledge_cutoff == "2024-06-01"
    assert mini.description == "GPT-4o mini Transcribe is a speech-to-text model."
    whisper = _declared(infos["openai/whisper-1"])
    assert whisper.published_at == "2022-09-21"
    assert whisper.knowledge_cutoff is None


def test_the_openrouter_dialect_never_reads_created_as_a_publication_day() -> None:
    infos = _by_id(
        extract_openrouter_tool_model_infos(
            {"data": [OPENROUTER_ROW, NOUS_ROW]}, provider_name="O"
        )
    )
    row = _declared(infos["qwen/qwen3-vl-30b-a3b-thinking"])
    assert row.published_at is None
    assert row.retires_at == "2026-10-09"
    assert row.knowledge_cutoff == "2025-03-31"
    assert row.description == "Qwen3-VL-30B-A3B-Thinking is a multimodal model."
    nous = _declared(infos["bytedance-seed/seed-2.0-code"])
    assert nous.published_at is None
    assert nous.retires_at == "2026-11-11"
    assert nous.knowledge_cutoff is None


def test_command_code_s_stamp_states_no_day() -> None:
    infos = _by_id(
        extract_commandcode_model_infos({"data": COMMANDCODE_ROWS}, provider_name="CC")
    )
    assert _declared(infos["claude-sonnet-5-5"]).published_at is None
    assert _declared(infos["gpt-6-astra"]).published_at is None


def test_anthropic_s_created_at_is_kept_verbatim_and_its_epoch_is_unknown() -> None:
    payload = {
        "data": [
            {"id": "claude-opus-5-5", "created_at": "2026-07-01T00:00:00Z"},
            {"id": "claude-haiku-4-5", "created_at": "2025-10-01T00:00:00Z"},
            {"id": "claude-legacy", "created_at": "1970-01-01T00:00:00Z"},
        ]
    }
    infos = _by_id(extract_anthropic_model_infos(payload, provider_name="A"))
    assert _declared(infos["claude-opus-5-5"]).published_at == "2026-07-01T00:00:00Z"
    assert infos["claude-legacy"].declared is None


def test_the_display_facts_fill_no_record_field() -> None:
    """Shown only: the record a 7.84.0 parser built is the record, field for field."""

    infos = extract_openai_model_infos({"data": VERCEL_ROWS}, provider_name="V")
    for info in infos:
        declared = _declared(info)
        bare = replace(
            declared,
            published_at=None,
            retires_at=None,
            knowledge_cutoff=None,
            description=None,
        )
        without = replace(info, declared=bare)
        assert record_with_declared(without) == without
        assert replace(info, declared=None) == replace(without, declared=None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-09", "2026-10-09"),
        ("  2026-10-09 ", "2026-10-09"),
        (1803600000000, "2027-02-26"),
        (1710288000, "2024-03-13"),
        (0, None),
        (-5, None),
        (True, None),
        ("", None),
        ("1970-01-01", None),
        (float("inf"), None),
        (["2026-10-09"], None),
    ],
)
def test_a_retirement_day_is_verbatim_or_an_iso_day(value, expected) -> None:
    declared = declared_from_row({"id": "x", "expiration_date": value})
    assert (None if declared is None else declared.retires_at) == expected


@pytest.mark.parametrize("value", [7, ["2025-03"], {"at": "2025"}, "", "   "])
def test_an_unreadable_cutoff_or_description_states_nothing(value) -> None:
    assert declared_from_row({"knowledge_cutoff": value, "description": value}) is None
