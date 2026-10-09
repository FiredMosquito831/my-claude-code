"""LiteLLM's map states more than prices: modalities, mode, endpoints, deprecation (7.85.0).

Read only while LiteLLM pricing is on and its file is on disk, and matched
exactly as pricing matches it: a key naming the routed provider first, then a
bare key only where its own ``litellm_provider`` agrees -- so Vertex's bare
``gemini-2.5-pro`` row never describes another provider's route. Entries are
trimmed from the pinned map (commit ``02522a54``).
"""

import os
from pathlib import Path

import pytest

from my_claude_code.application.model_metadata import DeclaredModalities
from my_claude_code.config.settings import Settings
from my_claude_code.providers.runtime.litellm_prices import (
    LITELLM_PINNED_URL,
    litellm_model_catalogue,
    litellm_model_facts,
    reset_litellm_model_map_cache,
    write_litellm_cache,
)

_WHISPER = {
    "litellm_provider": "openai",
    "mode": "audio_transcription",
    "supported_endpoints": ["/v1/audio/transcriptions"],
    "input_cost_per_second": 0.0001,
}
_GPT = {
    "litellm_provider": "openai",
    "mode": "chat",
    "supported_endpoints": ["/v1/chat/completions", "/v1/responses", "/v1/batch"],
    "supported_modalities": ["text", "image"],
    "supported_output_modalities": ["text"],
    "deprecation_date": "2026-11-04",
    "input_cost_per_token": 2.5e-06,
}
_VERTEX_GEMINI = {
    "litellm_provider": "vertex_ai-language-models",
    "mode": "chat",
    "supported_modalities": ["text", "image", "audio", "video"],
    "supported_output_modalities": ["text"],
    "deprecation_date": "2026-06-17",
}
_ONE_HALF = {
    "litellm_provider": "novita",
    "mode": "chat",
    "supported_modalities": ["text"],
}
_INDEX = {
    "whisper-1": _WHISPER,
    "gpt-4o": _GPT,
    "gemini-2.5-pro": _VERTEX_GEMINI,
    "novita/acme/half": _ONE_HALF,
    "sample_spec": {"mode": "chat", "deprecation_date": "date in YYYY-MM-DD"},
}


@pytest.fixture(autouse=True)
def _fresh_map() -> None:
    reset_litellm_model_map_cache()


def _seed(path: Path, index: dict) -> Path:
    write_litellm_cache(index, path, etag='"abc"', source_url=LITELLM_PINNED_URL)
    return path


def test_every_statement_comes_from_the_entry_pricing_would_use() -> None:
    gpt = litellm_model_facts(_INDEX, "openai", "gpt-4o")
    assert gpt is not None
    assert gpt.modalities is not None
    assert gpt.modalities.value == DeclaredModalities(
        inputs=("image", "text"), outputs=("text",)
    )
    assert gpt.modalities.key == "gpt-4o"
    assert gpt.words is not None
    assert gpt.words.value == (
        "chat",
        "/v1/batch",
        "/v1/chat/completions",
        "/v1/responses",
    )
    assert gpt.endpoints is not None
    assert gpt.endpoints.value == ("/v1/batch", "/v1/chat/completions", "/v1/responses")
    assert gpt.deprecation_date is not None
    assert gpt.deprecation_date.value == "2026-11-04"
    assert gpt.deprecation_date.tier_label.startswith("LiteLLM, ")


def test_a_bare_key_of_another_seller_describes_nothing() -> None:
    """The ``gemini-2.5-pro``-is-Vertex trap, for facts as for prices."""

    assert litellm_model_facts(_INDEX, "gemini", "gemini-2.5-pro") is None
    vertex = litellm_model_facts(_INDEX, "vertex_ai", "gemini-2.5-pro")
    assert vertex is not None and vertex.deprecation_date is not None


def test_a_prefixed_key_names_its_provider_and_half_a_pair_is_no_pair() -> None:
    half = litellm_model_facts(_INDEX, "novita", "acme/half")
    assert half is not None
    assert half.modalities is None
    assert half.words is not None and half.words.value == ("chat",)
    assert half.words.match == "prefixed key"


def test_a_mode_alone_is_a_coarse_statement() -> None:
    whisper = litellm_model_facts(_INDEX, "openai", "whisper-1")
    assert whisper is not None
    assert whisper.modalities is None
    assert whisper.words is not None
    assert whisper.words.value == ("audio_transcription", "/v1/audio/transcriptions")


def test_nothing_matched_states_nothing() -> None:
    assert litellm_model_facts(_INDEX, "openai", "no-such-model") is None
    assert litellm_model_facts(_INDEX, "openai", "sample_spec") is None


def test_the_catalogue_exists_only_with_litellm_pricing_on(tmp_path: Path) -> None:
    path = _seed(tmp_path / "litellm-prices.json", _INDEX)
    off = Settings.model_validate({"COST_SOURCE_LITELLM_ENABLED": "false"})
    assert litellm_model_catalogue(off, path) is None
    on = Settings.model_validate({"COST_SOURCE_LITELLM_ENABLED": "true"})
    catalogue = litellm_model_catalogue(on, path)
    assert catalogue is not None
    answer = catalogue("openai", "gpt-4o")
    assert answer is not None and answer.deprecation_date is not None
    assert litellm_model_catalogue(on, tmp_path / "absent.json") is None


def test_the_mark_follows_the_file(tmp_path: Path) -> None:
    on = Settings.model_validate({"COST_SOURCE_LITELLM_ENABLED": "true"})
    path = _seed(tmp_path / "litellm-prices.json", _INDEX)
    first = litellm_model_catalogue(on, path)
    again = litellm_model_catalogue(on, path)
    assert first is not None and again is not None
    assert first.mark == again.mark
    _seed(path, {**_INDEX, "gpt-4o-mini": _GPT})
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    changed = litellm_model_catalogue(on, path)
    assert changed is not None
    assert changed.mark != first.mark
    assert changed("openai", "gpt-4o-mini") is not None


def test_an_unreadable_file_is_no_rung(tmp_path: Path) -> None:
    on = Settings.model_validate({"COST_SOURCE_LITELLM_ENABLED": "true"})
    path = tmp_path / "litellm-prices.json"
    path.write_text("{not json", encoding="utf-8")
    assert litellm_model_catalogue(on, path) is None
