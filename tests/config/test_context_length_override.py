"""The operator's per-model context window, read out of ``model_overrides.json``.

Storage only (7.87.0): absent inherits, ``null`` forces the window unknown, a
positive whole number forces it. Model rows only, and the key is a statement
about a model -- never a field of a request body.
"""

import pytest

from my_claude_code.config.model_overrides import (
    CONTEXT_LENGTH_OVERRIDE,
    NON_BODY_OVERRIDE_PARAMETERS,
    ModelParameterOverrides,
    StatedContextLength,
    apply_model_parameter_overrides,
    context_length_problem,
)


def _table(providers=None, models=None) -> ModelParameterOverrides:
    return ModelParameterOverrides.from_document(
        {"providers": providers or {}, "models": models or {}}
    )


def test_the_three_states_stay_distinguishable() -> None:
    table = _table(
        models={
            "custom_agnes/agnes-3.0-flash": {CONTEXT_LENGTH_OVERRIDE: 1_000_000},
            "custom_agnes/forced-unknown": {CONTEXT_LENGTH_OVERRIDE: None},
            "custom_agnes/other": {"temperature": 0.2},
        }
    )

    assert table.context_length("custom_agnes/agnes-3.0-flash") == (
        StatedContextLength(1_000_000)
    )
    assert table.context_length("custom_agnes/forced-unknown") == (
        StatedContextLength(None)
    )
    assert table.context_length("custom_agnes/other") is None
    assert table.context_length("custom_agnes/never-written") is None


def test_the_model_ref_is_matched_case_folded_like_every_other_key() -> None:
    table = _table(models={"Custom_Agnes/Agnes-3.0-Flash": {"context_length": 4096}})

    assert table.context_length("custom_agnes/agnes-3.0-flash") == (
        StatedContextLength(4096)
    )


@pytest.mark.parametrize("value", [0, -1, -524288, 1.5, 1e6, True, False, "1000000"])
def test_an_unusable_window_is_dropped_at_parse_and_the_ladder_answers(value) -> None:
    table = _table(models={"p/m": {CONTEXT_LENGTH_OVERRIDE: value, "top_p": 0.9}})

    assert table.context_length("p/m") is None
    # The rest of the row is untouched.
    assert table.models["p/m"] == {"top_p": 0.9}
    assert context_length_problem(value) is not None


def test_a_provider_row_window_is_ignored() -> None:
    """One provider serves models with different windows: model rows only."""

    table = _table(
        providers={"custom_agnes": {CONTEXT_LENGTH_OVERRIDE: 1_000_000, "seed": 1}}
    )

    assert table.providers["custom_agnes"] == {"seed": 1}
    assert table.context_length("custom_agnes/agnes-3.0-flash") is None


def test_a_valid_window_passes_the_shared_check() -> None:
    assert context_length_problem(1) is None
    assert context_length_problem(1_000_000) is None


def test_the_window_never_reaches_a_request_body() -> None:
    assert CONTEXT_LENGTH_OVERRIDE in NON_BODY_OVERRIDE_PARAMETERS
    table = _table(models={"p/m": {CONTEXT_LENGTH_OVERRIDE: 1_000_000, "top_p": 0.9}})
    body: dict[str, object] = {"model": "m"}

    applied = apply_model_parameter_overrides(
        body, provider_id="p", model_ref="p/m", overrides=table
    )

    assert applied == {"top_p": 0.9}
    assert body == {"model": "m", "top_p": 0.9}


def test_null_survives_a_document_round_trip() -> None:
    document = {"providers": {}, "models": {"p/m": {CONTEXT_LENGTH_OVERRIDE: None}}}

    again = ModelParameterOverrides.from_document(
        _table(models=document["models"]).as_document()
    )

    assert again.context_length("p/m") == StatedContextLength(None)
