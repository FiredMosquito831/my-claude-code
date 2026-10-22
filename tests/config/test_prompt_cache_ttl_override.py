"""The 14th override key, ``prompt_cache_ttl`` (7.105.0), in the override file.

Declared exactly like the other thirteen: absent = inherit, ``null`` = force
unset ("from client" at that row), whole minutes 1-60 = force them; on a
provider row as well as a model row; a bad value dropped with a log line; the
file's key order kept; never written into a request body.
"""

import hashlib
import json
from pathlib import Path

import pytest

from my_claude_code.config.model_overrides import (
    ALLOWED_OVERRIDE_PARAMETERS,
    NON_BODY_OVERRIDE_PARAMETERS,
    PROMPT_CACHE_TTL_OVERRIDE,
    ModelParameterOverrides,
    StatedPromptCacheTtl,
    apply_model_parameter_overrides,
    load_model_overrides,
    prompt_cache_ttl_problem,
    save_model_overrides,
)


def _table(**sections: dict) -> ModelParameterOverrides:
    return ModelParameterOverrides.from_document(sections)


def test_the_key_is_a_non_body_key_and_never_widens_the_body_list() -> None:
    assert PROMPT_CACHE_TTL_OVERRIDE == "prompt_cache_ttl"
    assert PROMPT_CACHE_TTL_OVERRIDE in NON_BODY_OVERRIDE_PARAMETERS
    assert PROMPT_CACHE_TTL_OVERRIDE not in ALLOWED_OVERRIDE_PARAMETERS


@pytest.mark.parametrize("minutes", [1, 5, 6, 30, 59, 60])
def test_whole_minutes_from_one_to_sixty_are_kept(minutes: int) -> None:
    assert prompt_cache_ttl_problem(minutes) is None
    table = _table(models={"p/m": {"prompt_cache_ttl": minutes}})

    assert table.prompt_cache_ttl_rows("p", "p/m") == (
        StatedPromptCacheTtl(minutes),
        None,
    )


@pytest.mark.parametrize("value", [0, 61, -5, 1.5, "30", True, False, [30], 1000])
def test_anything_else_is_dropped_with_a_log_line(value: object) -> None:
    assert prompt_cache_ttl_problem(value) is not None
    table = _table(
        models={"p/m": {"prompt_cache_ttl": value, "temperature": 0.2}},
        providers={"p": {"prompt_cache_ttl": value}},
    )

    assert table.prompt_cache_ttl_rows("p", "p/m") == (None, None)
    # The rest of the row is untouched by the bad key.
    assert table.models == {"p/m": {"temperature": 0.2}}


def test_null_is_force_unset_on_either_row() -> None:
    table = _table(
        models={"p/m": {"prompt_cache_ttl": None}},
        providers={"p": {"prompt_cache_ttl": None}},
    )

    assert table.prompt_cache_ttl_rows("p", "p/m") == (
        StatedPromptCacheTtl(None),
        StatedPromptCacheTtl(None),
    )


def test_absent_is_inherit_and_the_two_rows_stay_apart() -> None:
    table = _table(providers={"p": {"prompt_cache_ttl": 45}})

    assert table.prompt_cache_ttl_rows("p", "p/m") == (None, StatedPromptCacheTtl(45))
    assert table.prompt_cache_ttl_rows("q", "q/m") == (None, None)


def test_a_provider_row_is_as_valid_as_a_model_row() -> None:
    """Unlike the context window, which is model-only (Q6, 2026-10-20)."""

    table = _table(providers={"p": {"prompt_cache_ttl": 20}})

    assert table.providers == {"p": {"prompt_cache_ttl": 20}}


def test_it_never_reaches_a_body() -> None:
    table = _table(
        models={"p/m": {"prompt_cache_ttl": 30}},
        providers={"p": {"prompt_cache_ttl": None, "top_p": 0.9}},
    )
    body = {"model": "m", "prompt_cache_ttl": "kept-as-the-builder-made-it"}

    applied = apply_model_parameter_overrides(
        body, provider_id="p", model_ref="p/m", overrides=table
    )

    assert applied == {"top_p": 0.9}
    assert body == {
        "model": "m",
        "prompt_cache_ttl": "kept-as-the-builder-made-it",
        "top_p": 0.9,
    }


def test_the_file_round_trips_with_its_key_order(tmp_path: Path) -> None:
    path = tmp_path / "model_overrides.json"
    document = {
        "providers": {
            "p": {"top_k": 40, "prompt_cache_ttl": 45, "max_output_tokens": 8}
        },
        "models": {
            "p/m": {"temperature": 0.1, "prompt_cache_ttl": None, "context_length": 9}
        },
    }
    path.write_bytes((json.dumps(document, indent=2) + "\n").encode("utf-8"))
    before = hashlib.sha256(path.read_bytes()).hexdigest()

    save_model_overrides(load_model_overrides(path), path)

    assert json.loads(path.read_text(encoding="utf-8")) == document
    assert list(json.loads(path.read_text(encoding="utf-8"))["providers"]["p"]) == [
        "top_k",
        "prompt_cache_ttl",
        "max_output_tokens",
    ]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
