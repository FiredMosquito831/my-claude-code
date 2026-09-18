"""The two non-body preference keys, read out of ``model_overrides.json``.

Storage-level only: nothing here builds a policy or a request. What is under
test is that the file's existing three-state contract -- absent, ``null``,
a value -- carries two new keys without either of them ever becoming a field
of a request body.
"""

from my_claude_code.config.model_overrides import (
    ALLOWED_OVERRIDE_PARAMETERS,
    MAX_OUTPUT_TOKENS_OVERRIDE,
    NON_BODY_OVERRIDE_PARAMETERS,
    OWNED_ELSEWHERE_PARAMETERS,
    PREFERENCE_OVERRIDE_PARAMETERS,
    REASONING_PREFERENCE_OVERRIDE,
    ModelParameterOverrides,
    apply_model_parameter_overrides,
)


def overrides(providers=None, models=None):
    return ModelParameterOverrides.from_document(
        {"providers": providers or {}, "models": models or {}}
    )


def test_a_model_row_reasoning_preference_beats_the_provider_row():
    table = overrides(
        providers={"open_router": {REASONING_PREFERENCE_OVERRIDE: "medium"}},
        models={"open_router/z-ai/glm-5": {REASONING_PREFERENCE_OVERRIDE: "max"}},
    )

    assert table.reasoning_preference("open_router", "open_router/z-ai/glm-5") == "max"
    assert table.reasoning_preference("open_router", "open_router/other") == "medium"


def test_a_null_reasoning_preference_on_the_model_row_falls_back_to_the_route():
    """``null`` means "I have stopped having an opinion", not "inherit down"."""

    table = overrides(
        providers={"open_router": {REASONING_PREFERENCE_OVERRIDE: "medium"}},
        models={"open_router/z-ai/glm-5": {REASONING_PREFERENCE_OVERRIDE: None}},
    )

    assert table.reasoning_preference("open_router", "open_router/z-ai/glm-5") is None


def test_the_word_inherit_reads_as_no_opinion():
    table = overrides(models={"p/m": {REASONING_PREFERENCE_OVERRIDE: "inherit"}})

    assert table.reasoning_preference("p", "p/m") is None


def test_a_reasoning_preference_is_trimmed_and_case_folded():
    table = overrides(models={"p/m": {REASONING_PREFERENCE_OVERRIDE: "  MAX  "}})

    assert table.reasoning_preference("p", "p/m") == "max"


def test_an_unparsable_reasoning_preference_is_ignored_and_logged():
    """Storage keeps the word; the vocabulary is checked where the enum lives.

    The store cannot reject it without importing ``ReasoningPreference`` and
    ceasing to be a leaf, so what it guarantees is only that a non-string
    never reaches the layer that parses one.
    """

    table = overrides(models={"p/m": {REASONING_PREFERENCE_OVERRIDE: 7}})

    assert table.reasoning_preference("p", "p/m") is None


def test_a_non_integer_max_output_tokens_is_ignored():
    table = overrides(models={"p/m": {MAX_OUTPUT_TOKENS_OVERRIDE: "4096"}})

    assert table.max_output_tokens("p", "p/m") is None


def test_a_zero_or_negative_max_output_tokens_is_ignored():
    for value in (0, -1):
        table = overrides(models={"p/m": {MAX_OUTPUT_TOKENS_OVERRIDE: value}})
        assert table.max_output_tokens("p", "p/m") is None


def test_a_boolean_is_not_accepted_as_max_output_tokens():
    """``True`` is an ``int`` in Python and would become a one-token budget."""

    table = overrides(models={"p/m": {MAX_OUTPUT_TOKENS_OVERRIDE: True}})

    assert table.max_output_tokens("p", "p/m") is None


def test_a_model_row_output_cap_beats_the_provider_row():
    table = overrides(
        providers={"p": {MAX_OUTPUT_TOKENS_OVERRIDE: 8192}},
        models={"p/m": {MAX_OUTPUT_TOKENS_OVERRIDE: 4096}},
    )

    assert table.max_output_tokens("p", "p/m") == 4096
    assert table.max_output_tokens("p", "p/other") == 8192


def test_neither_preference_key_is_ever_written_into_a_request_body():
    table = overrides(
        models={
            "p/m": {
                REASONING_PREFERENCE_OVERRIDE: "max",
                MAX_OUTPUT_TOKENS_OVERRIDE: 4096,
                "temperature": 0.5,
            }
        }
    )
    body: dict[str, object] = {"model": "m"}
    applied = apply_model_parameter_overrides(
        body,
        provider_id="p",
        model_ref="p/m",
        overrides=table,
    )

    assert applied == {"temperature": 0.5}
    assert body == {"model": "m", "temperature": 0.5}


def test_reasoning_effort_and_max_tokens_stay_owned_elsewhere():
    """The body spellings still name their owner rather than silently working."""

    for name in ("reasoning", "reasoning_effort", "max_tokens", "thinking"):
        assert name in OWNED_ELSEWHERE_PARAMETERS
        assert name not in PREFERENCE_OVERRIDE_PARAMETERS

    table = overrides(models={"p/m": {"reasoning": "max", "max_tokens": 4096}})

    assert table.models == {}


def test_the_new_keys_do_not_widen_the_body_allow_list():
    assert PREFERENCE_OVERRIDE_PARAMETERS <= NON_BODY_OVERRIDE_PARAMETERS
    assert not PREFERENCE_OVERRIDE_PARAMETERS & ALLOWED_OVERRIDE_PARAMETERS
    assert {
        REASONING_PREFERENCE_OVERRIDE,
        MAX_OUTPUT_TOKENS_OVERRIDE,
    } == PREFERENCE_OVERRIDE_PARAMETERS


def test_the_keys_survive_a_document_round_trip():
    document = {
        "providers": {"p": {REASONING_PREFERENCE_OVERRIDE: "low"}},
        "models": {"p/m": {MAX_OUTPUT_TOKENS_OVERRIDE: 4096}},
    }

    assert ModelParameterOverrides.from_document(document).as_document() == document
