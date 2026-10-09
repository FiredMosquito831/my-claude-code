"""A fallback chain may list a model more than once (7.82.0).

User decision (2026-10-06 18:30, binding): "in the fallback chains we also want
to allow the same model from the same provider to repeat how-many-ever times it
is put in the fallback chain". Every place that reads or writes a chain keeps a
repeated entry; every place that reads a *set* of refs -- a pause list, the
configured-models discovery list -- stays a set. And a chain with no repeat is
read and written byte for byte as before: the equality half of each test below.
"""

import json
import random
from pathlib import Path

import pytest

from my_claude_code.api.admin_harness_routes import _clean_list
from my_claude_code.config.admin.manifest import FIELD_BY_KEY
from my_claude_code.config.admin.route_refs import updates_removing_provider
from my_claude_code.config.admin.values import normalize_field_value
from my_claude_code.config.harness_tiers import HarnessTiers, load_harness_tiers
from my_claude_code.config.model_refs import (
    format_model_ref_list,
    parse_model_ref_chain,
    parse_model_ref_list,
)
from my_claude_code.config.provider_registry import ProviderRegistry
from my_claude_code.config.settings import Settings

A = "nvidia_nim/a"
B = "open_router/b"
C = "groq/c"

#: Every chain setting and every pause setting Settings validates.
CHAIN_FIELDS = (
    "MODEL_FALLBACKS",
    "MODEL_MYTHOS_FALLBACKS",
    "MODEL_FABLE_FALLBACKS",
    "MODEL_OPUS_FALLBACKS",
    "MODEL_SONNET_FALLBACKS",
    "MODEL_HAIKU_FALLBACKS",
    "MODEL_VISION_FALLBACKS",
    "MODEL_IMAGE_FALLBACKS",
    "MODEL_TTS_FALLBACKS",
    "MODEL_ASR_FALLBACKS",
    "MODEL_VIDEO_FALLBACKS",
)
PAUSE_FIELDS = tuple(name.replace("_FALLBACKS", "_PAUSED") for name in CHAIN_FIELDS)

_POOL = (
    "nvidia_nim/a",
    "open_router/b",
    "groq/c",
    "cerebras/d",
    "open_router/x-ai/grok-5",
    "nvidia_nim/nvidia/nemotron-3",
    "groq/moonshotai/kimi-k2",
    "deepinfra/e",
)


def _old_parse(raw: str | None) -> tuple[str, ...]:
    """The 7.81.1 chain parser, verbatim: the reference for the equality proof."""

    if not raw:
        return ()
    refs: list[str] = []
    for candidate in raw.split(","):
        model_ref = candidate.strip()
        if model_ref and model_ref not in refs:
            refs.append(model_ref)
    return tuple(refs)


def _repeat_free_texts(count: int, seed: int = 782) -> list[str]:
    """Chain strings with no repeat, in the spellings a person or a file writes."""

    rng = random.Random(seed)
    texts = ["", " ", ",", " , ,", A]
    for _ in range(count):
        refs = rng.sample(_POOL, rng.randint(1, len(_POOL)))
        parts: list[str] = []
        for ref in refs:
            parts.append(rng.choice(("", " ", "  ")) + ref + rng.choice(("", " ")))
            if rng.random() < 0.15:
                parts.append(rng.choice(("", " ")))
        texts.append(",".join(parts))
    return texts


def test_a_chain_keeps_every_listing_in_order() -> None:
    assert parse_model_ref_chain(f"{A}, {B},{A} ,,{A}") == (A, B, A, A)
    assert parse_model_ref_chain(None) == ()
    assert parse_model_ref_chain("") == ()
    assert parse_model_ref_chain(" , ") == ()


def test_a_set_of_refs_still_reads_each_ref_once() -> None:
    """A pause names a ref: naming it twice means nothing."""

    assert parse_model_ref_list(f"{A},{B},{A}") == (A, B)


def test_without_a_repeat_the_chain_parser_is_the_old_parser() -> None:
    """Equality: 505 repeat-free spellings, both parsers, both renderings."""

    for text in _repeat_free_texts(500):
        assert parse_model_ref_chain(text) == _old_parse(text), text
        assert parse_model_ref_list(text) == _old_parse(text), text
        assert format_model_ref_list(parse_model_ref_chain(text)) == (
            format_model_ref_list(_old_parse(text))
        ), text


@pytest.mark.parametrize("name", CHAIN_FIELDS)
def test_settings_keep_a_repeat_on_every_chain(monkeypatch, name: str) -> None:
    monkeypatch.setenv(name, f" {A},{B} , {A}")

    value = getattr(Settings(), name.lower())

    assert value == f"{A},{B},{A}"


@pytest.mark.parametrize("name", PAUSE_FIELDS)
def test_settings_store_a_pause_list_as_a_set(monkeypatch, name: str) -> None:
    monkeypatch.setenv(name, f"{A},{B},{A}")

    assert getattr(Settings(), name.lower()) == f"{A},{B}"


@pytest.mark.parametrize("name", CHAIN_FIELDS + PAUSE_FIELDS)
def test_settings_store_a_repeat_free_list_as_before(monkeypatch, name: str) -> None:
    for text in _repeat_free_texts(20, seed=sum(map(ord, name))):
        monkeypatch.setenv(name, text)
        old = format_model_ref_list(_old_parse(text)) or None
        assert getattr(Settings(), name.lower()) == old, text


def test_settings_still_refuse_an_unknown_provider_in_a_repeat(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_FALLBACKS", f"{A},not_a_provider/x,{A}")

    with pytest.raises(ValueError, match="not_a_provider"):
        Settings()


def test_apply_writes_a_repeated_chain_as_typed() -> None:
    field = FIELD_BY_KEY["MODEL_OPUS_FALLBACKS"]

    assert normalize_field_value(field, f"{A}, {B},{A}") == f"{A},{B},{A}"
    assert normalize_field_value(field, f"{A},{A}") == f"{A},{A}"


def test_apply_writes_a_repeat_free_chain_as_before() -> None:
    for key in CHAIN_FIELDS:
        field = FIELD_BY_KEY[key]
        assert field.field_type == "model_chain"
        for text in _repeat_free_texts(30):
            old = format_model_ref_list(_old_parse(text))
            assert normalize_field_value(field, text) == old, (key, text)


@pytest.fixture
def acme(monkeypatch, tmp_path: Path) -> None:
    registry = ProviderRegistry(tmp_path / "custom_providers.json")
    registry.add(
        display_name="Acme",
        base_url="https://api.acme.example/v1",
        api_keys=("sk-acme-aaaa1111bbbb",),
    )
    monkeypatch.setattr("my_claude_code.config.provider_registry._registry", registry)


def test_deleting_a_provider_keeps_the_repeats_it_did_not_name(
    monkeypatch, acme: None
) -> None:
    monkeypatch.setenv("MODEL_FALLBACKS", f"{A},custom_acme/m,{A},custom_acme/m")
    monkeypatch.setenv("MODEL_PAUSED", "custom_acme/m")

    updates, removed = updates_removing_provider(Settings(), "custom_acme")

    assert updates == {"MODEL_FALLBACKS": f"{A},{A}", "MODEL_PAUSED": ""}
    assert removed == (
        "MODEL_FALLBACKS=custom_acme/m",
        "MODEL_FALLBACKS=custom_acme/m",
        "MODEL_PAUSED=custom_acme/m",
    )


def test_deleting_a_provider_leaves_a_repeated_chain_it_is_not_on_alone(
    monkeypatch, acme: None
) -> None:
    """Up to 7.81.1 this rewrite silently collapsed the repeat as well."""

    monkeypatch.setenv("MODEL_FALLBACKS", f"{A},{B},{A}")

    updates, removed = updates_removing_provider(Settings(), "custom_acme")

    assert updates == {}
    assert removed == ()


def test_an_agent_override_keeps_a_repeat_and_its_pause_list_stays_a_set(
    tmp_path: Path,
) -> None:
    document = {
        "harnesses": {
            "opencode": {
                "best": {"model": A, "fallbacks": [B, A, B], "paused": [B, B]},
                "cheap": {"fallbacks": f"{C}, {C}"},
            }
        }
    }
    path = tmp_path / "harness_tiers.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    tiers = load_harness_tiers(path)
    best = tiers.override("opencode", "best")
    cheap = tiers.override("opencode", "cheap")

    assert best is not None and cheap is not None
    assert best.fallbacks == (B, A, B)
    assert best.paused == (B,)
    assert cheap.fallbacks == (C, C)
    # And it is written back exactly as it was read.
    assert tiers.as_document()["harnesses"]["opencode"]["best"] == {
        "model": A,
        "fallbacks": [B, A, B],
        "paused": [B],
    }


def test_a_repeat_free_override_document_round_trips_byte_for_byte() -> None:
    rng = random.Random(7820)
    for _ in range(100):
        fallbacks = rng.sample(_POOL, rng.randint(0, 4))
        paused = rng.sample(_POOL, rng.randint(0, 2))
        entry: dict[str, object] = {"model": rng.choice(_POOL)}
        if fallbacks:
            entry["fallbacks"] = fallbacks
        if paused:
            entry["paused"] = paused
        document = {"harnesses": {"crush": {"good": entry}}}
        tiers = HarnessTiers.from_document(document)
        assert json.dumps(tiers.as_document()) == json.dumps(document)


def test_the_agent_tier_route_keeps_a_repeated_fallback() -> None:
    assert _clean_list([A, " ", B, A], keep_repeats=True) == (A, B, A)
    assert _clean_list([A, B, A]) == (A, B)
