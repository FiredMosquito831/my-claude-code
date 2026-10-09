"""OpenRouter's live model list as a rung for every provider (7.84.0): store and match.

The rows below are trimmed from the keyless copy of
``https://openrouter.ai/api/v1/models?output_modalities=all`` taken 2026-10-08
(the spec's investigation), keeping the fields the rung reads.
"""

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.application.model_metadata import DeclaredModalities
from my_claude_code.application.openrouter_live import (
    LIVE_MATCH_BARE,
    LIVE_MATCH_BARE_TAGGED,
    LIVE_MATCH_EXACT,
    LIVE_MATCH_TAG_STRIPPED,
)
from my_claude_code.config.settings import Settings
from my_claude_code.providers.runtime import openrouter_catalogue
from my_claude_code.providers.runtime.openrouter_catalogue import (
    OPENROUTER_LIVE_URL,
    build_live_index,
    live_model_from_row,
    match_live_model,
    openrouter_live_catalogue,
    openrouter_live_is_due,
    payload_passes_integrity,
    read_openrouter_live_rows,
    refresh_openrouter_live,
    write_openrouter_live_cache,
)

GLM_5: dict[str, Any] = {
    "id": "z-ai/glm-5",
    "created": 1770829182,
    "description": "GLM-5 is Z.ai's flagship open-source foundation model.",
    "context_length": 204800,
    "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
    "pricing": {
        "prompt": "0.0000006",
        "completion": "0.00000192",
        "input_cache_read": "0.00000012",
    },
    "top_provider": {"context_length": 198000, "max_completion_tokens": 128000},
    "supported_parameters": [
        "max_tokens",
        "reasoning",
        "temperature",
        "tool_choice",
        "tools",
    ],
    "knowledge_cutoff": None,
    "reasoning": {"mandatory": False, "default_enabled": True},
}
MISTRAL_LARGE: dict[str, Any] = {
    "id": "mistralai/mistral-large",
    "created": 1708905600,
    "description": "Mistral AI's flagship model.",
    "context_length": 128000,
    "architecture": {
        "input_modalities": ["text", "file"],
        "output_modalities": ["text"],
    },
    "pricing": {
        "prompt": "0.000002",
        "completion": "0.000006",
        "input_cache_read": "0.0000002",
    },
    "top_provider": {"context_length": 128000, "max_completion_tokens": 102400},
    "supported_parameters": ["max_tokens", "temperature", "tool_choice", "tools"],
    "knowledge_cutoff": "2024-11-30",
}
MING_IMAGE: dict[str, Any] = {
    "id": "inclusionai/ming-image-0.1-design",
    "created": 1790095711,
    "description": "A text-to-image model from inclusionAI.",
    "context_length": 0,
    "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]},
    "pricing": {"prompt": "0", "completion": "0"},
    "top_provider": {"context_length": 0, "max_completion_tokens": 0},
    "supported_parameters": ["max_tokens", "seed", "temperature"],
    "knowledge_cutoff": None,
}
FREE_ROW: dict[str, Any] = {
    "id": "apodex/apodex-1.1-mini:free",
    "created": 1790000000,
    "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
    "pricing": {"prompt": "0", "completion": "0"},
    "supported_parameters": ["max_tokens", "tools"],
}
NO_PARAMETER_LIST: dict[str, Any] = {
    "id": "acme/quiet-1",
    "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
}
ROWS = [GLM_5, MISTRAL_LARGE, MING_IMAGE, FREE_ROW, NO_PARAMETER_LIST]


def _settings(**update: Any) -> Settings:
    return Settings.model_validate(update)


def _store(path: Path, rows: list[dict[str, Any]]) -> Path:
    return write_openrouter_live_cache(rows, path)


# -------------------------------------------------------------------- rows


def test_a_row_is_read_as_the_open_router_provider_reads_its_own() -> None:
    model = live_model_from_row(GLM_5)
    assert model is not None
    assert model.slugs == ("z-ai/glm-5",)
    assert model.modalities == DeclaredModalities(inputs=("text",), outputs=("text",))
    assert model.supports_vision is False
    assert model.can_reason is True
    assert model.supports_tool_calls is True
    # The routed deployment's own window first, as the dialect parser reads it.
    assert model.context_length == 198000
    assert model.max_output_tokens == 128000
    assert model.input_price == pytest.approx(0.6)
    assert model.output_price == pytest.approx(1.92)
    assert model.cache_read_price == pytest.approx(0.12)
    assert model.cache_write_price is None
    assert model.description is not None
    assert model.knowledge_cutoff is None
    # ``created`` is the day OpenRouter LISTED it, kept as a day.
    assert model.listed_at == "2026-02-11"


def test_a_zero_limit_is_unknown_and_a_zero_price_is_free() -> None:
    model = live_model_from_row(MING_IMAGE)
    assert model is not None
    assert model.context_length is None
    assert model.max_output_tokens is None
    assert model.input_price == 0.0
    assert model.modalities == DeclaredModalities(inputs=("text",), outputs=("image",))
    assert model.supports_tool_calls is False


def test_a_row_with_no_parameter_list_states_no_tools_and_no_reasoning() -> None:
    """Silence is not "no": only a published list can say a model takes no tools."""

    model = live_model_from_row(NO_PARAMETER_LIST)
    assert model is not None
    assert model.supports_tool_calls is None
    assert model.can_reason is None


def test_an_unreadable_row_states_nothing_and_never_raises() -> None:
    assert live_model_from_row({"id": ""}) is None
    assert live_model_from_row("not a row") is None
    assert live_model_from_row({"id": "a/b", "pricing": "garbage"}) is not None
    index = build_live_index([{"id": 7}, None, GLM_5])
    assert set(index) == {"z-ai/glm-5", "glm-5"}


# ----------------------------------------------------------------- matching


@pytest.mark.parametrize(
    ("provider", "asked", "match", "slug"),
    [
        # exact: the same slug, and a bare id equal to an OpenRouter tail.
        ("nous_portal", "z-ai/glm-5", LIVE_MATCH_EXACT, "z-ai/glm-5"),
        ("opencode_go", "glm-5", LIVE_MATCH_EXACT, "z-ai/glm-5"),
        # tag stripped: the query's allow-listed tag comes off.
        ("nous_portal", "z-ai/glm-5:free", LIVE_MATCH_TAG_STRIPPED, "z-ai/glm-5"),
        # bare model + tag: another vendor prefix meets the tail.
        ("novita", "zai-org/glm-5", LIVE_MATCH_BARE_TAGGED, "z-ai/glm-5"),
        # bare model: another vendor AND a tag.
        ("commandcode", "zai-org/glm-5-free", LIVE_MATCH_BARE, "z-ai/glm-5"),
    ],
)
def test_the_four_identifier_forms_meet_an_openrouter_slug(
    provider: str, asked: str, match: str, slug: str
) -> None:
    index = build_live_index(ROWS)
    found = match_live_model(index, provider, asked)
    assert found is not None
    assert found.match == match
    assert found.slugs == (slug,)
    assert found.feeds_ladder


def test_no_match_is_none() -> None:
    index = build_live_index(ROWS)
    assert match_live_model(index, "kimi_coding", "kimi-for-coding") is None


def test_ambiguous_rows_answer_only_where_they_all_agree() -> None:
    """One key, two OpenRouter models: a field only where both state the same."""

    other = {
        **GLM_5,
        "id": "other-vendor/glm-5",
        "pricing": {"prompt": "0.000001", "completion": "0.00000192"},
        "top_provider": {"context_length": 198000, "max_completion_tokens": 64000},
    }
    index = build_live_index([GLM_5, other])
    found = match_live_model(index, "custom_x", "glm-5")
    assert found is not None
    assert found.slugs == ("other-vendor/glm-5", "z-ai/glm-5")
    assert found.context_length == 198000
    assert found.output_price == pytest.approx(1.92)
    assert found.modalities == DeclaredModalities(inputs=("text",), outputs=("text",))
    assert found.max_output_tokens is None
    assert found.input_price is None
    # One states a cache price and the other does not: not every row agrees.
    assert found.cache_read_price is None


def test_openrouter_s_own_models_feed_no_existing_field() -> None:
    index = build_live_index(ROWS)
    found = match_live_model(index, "open_router", "z-ai/glm-5")
    assert found is not None
    assert found.own_list
    assert not found.feeds_ladder
    assert found.description is not None


# ------------------------------------------------------------------ binding


def test_the_rung_is_absent_when_off_or_nothing_is_stored(tmp_path: Path) -> None:
    path = tmp_path / "cache" / "openrouter-models.json"
    assert openrouter_live_catalogue(_settings(), path) is None
    _store(path, ROWS)
    off = _settings(MODEL_METADATA_OPENROUTER_LIVE="false")
    assert openrouter_live_catalogue(off, path) is None
    bound = openrouter_live_catalogue(_settings(), path)
    assert bound is not None
    assert bound.rows == len(ROWS)
    assert bound.fetched_at is not None
    stat = path.stat()
    assert bound.mark == f"{stat.st_mtime_ns}:{stat.st_size}"
    found = bound("nous_portal", "mistralai/mistral-large")
    assert found is not None and found.knowledge_cutoff == "2024-11-30"


def test_a_damaged_file_is_absent_not_an_error(tmp_path: Path) -> None:
    path = tmp_path / "openrouter-models.json"
    path.write_text("{not json", encoding="utf-8")
    assert openrouter_live_catalogue(_settings(), path) is None
    path.write_text(json.dumps({"data": "nope"}), encoding="utf-8")
    assert openrouter_live_catalogue(_settings(), path) is None


def test_it_is_due_when_on_and_absent_or_past_the_models_dev_freshness(
    tmp_path: Path,
) -> None:
    path = tmp_path / "openrouter-models.json"
    assert openrouter_live_is_due(_settings(), path)
    assert not openrouter_live_is_due(
        _settings(MODEL_METADATA_OPENROUTER_LIVE="false"), path
    )
    _store(path, ROWS)
    assert not openrouter_live_is_due(_settings(), path)
    old = time.time() - 2 * 86400
    os.utime(path, (old, old))
    assert openrouter_live_is_due(_settings(), path)
    assert not openrouter_live_is_due(
        _settings(MODELS_DEV_CACHE_TTL_SECONDS="604800"), path
    )


# --------------------------------------------------------------- integrity


def test_the_integrity_check() -> None:
    assert payload_passes_integrity({"data": ROWS}, None)
    assert not payload_passes_integrity({"data": []}, None)
    assert not payload_passes_integrity({"data": [{"name": "no id"}]}, None)
    assert not payload_passes_integrity(["not", "an", "object"], None)
    # Fewer than half of what is on disk is one bad answer, not a catalogue.
    assert not payload_passes_integrity({"data": ROWS[:2]}, 5)
    assert payload_passes_integrity({"data": ROWS[:3]}, 5)


def _fetch_answering(payload: Any):
    calls: list[dict[str, Any]] = []

    async def fetch(url: str, *, proxy: str | None, timeout: float) -> Any:
        calls.append({"url": url, "proxy": proxy, "timeout": timeout})
        return payload

    return fetch, calls


def test_a_refresh_stores_what_passed_and_keeps_the_copy_otherwise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "openrouter-models.json"
    fetch, calls = _fetch_answering({"data": ROWS})
    monkeypatch.setattr(openrouter_catalogue, "fetch_openrouter_live", fetch)
    outcome = asyncio.run(refresh_openrouter_live(_settings(), path))
    assert outcome.status == "fetched" and outcome.rows == len(ROWS)
    assert calls == [{"url": OPENROUTER_LIVE_URL, "proxy": None, "timeout": 10.0}]
    stored = read_openrouter_live_rows(path)
    assert stored is not None and len(stored[0]) == len(ROWS)

    shrunk, _ = _fetch_answering({"data": ROWS[:1]})
    monkeypatch.setattr(openrouter_catalogue, "fetch_openrouter_live", shrunk)
    outcome = asyncio.run(refresh_openrouter_live(_settings(), path))
    assert outcome.status == "rejected"
    kept = read_openrouter_live_rows(path)
    assert kept is not None and len(kept[0]) == len(ROWS)


def test_a_failed_fetch_keeps_the_copy_and_never_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _store(tmp_path / "openrouter-models.json", ROWS)

    async def broken(url: str, *, proxy: str | None, timeout: float) -> Any:
        raise OSError("offline")

    monkeypatch.setattr(openrouter_catalogue, "fetch_openrouter_live", broken)
    outcome = asyncio.run(refresh_openrouter_live(_settings(), path))
    assert outcome.status == "failed"
    kept = read_openrouter_live_rows(path)
    assert kept is not None and len(kept[0]) == len(ROWS)


def test_off_means_no_fetch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fetch, calls = _fetch_answering({"data": ROWS})
    monkeypatch.setattr(openrouter_catalogue, "fetch_openrouter_live", fetch)
    outcome = asyncio.run(
        refresh_openrouter_live(
            _settings(MODEL_METADATA_OPENROUTER_LIVE="false"),
            tmp_path / "openrouter-models.json",
        )
    )
    assert outcome.status == "off"
    assert calls == []
    assert not (tmp_path / "openrouter-models.json").exists()


def test_the_fetch_takes_the_open_router_provider_s_static_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No chain: the provider's own ``OPENROUTER_PROXY``, as its requests use."""

    fetch, calls = _fetch_answering({"data": ROWS})
    monkeypatch.setattr(openrouter_catalogue, "fetch_openrouter_live", fetch)
    settings = _settings(OPENROUTER_PROXY="http://proxy.invalid:3128")
    outcome = asyncio.run(
        refresh_openrouter_live(settings, tmp_path / "openrouter-models.json")
    )
    assert outcome.status == "fetched"
    assert calls[0]["proxy"] == "http://proxy.invalid:3128"
