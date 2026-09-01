"""A probed vocabulary reaches the wire through the declaration seam.

The defect this pins, from the live install on 2026-09-01: one request to
``custom_b_ai/glm-5.3-flash`` asked for effort ``max``, the host accepted
``max`` (direct probe: an invalid value returned a 400 naming
``low, high, max``; ``high`` and ``max`` both returned 200), models.dev listed
``["high","low","max"]`` for the model -- and MCC sent ``high``, recording the
adaptation "does not accept effort 'max'". It said that because
``GENERIC_OPENAI_PROFILE`` had no way to *spell* ``max``, not because anything
in gating was wrong.

So none of gating is touched here. The learned words are folded into the same
``NamedEffortReasoning`` a static profile declares, and the assertions below
are about what leaves the encoder.
"""

import dataclasses

import pytest

from my_claude_code.config.provider_registry import ProviderRegistry
from my_claude_code.core.reasoning import (
    ReasoningControl,
    ReasoningDialectOrigin,
    ReasoningEffort,
    ReasoningPolicy,
)
from my_claude_code.providers.openai_chat import (
    GENERIC_OPENAI_PROFILE,
    learned_named_effort_reasoning,
    profile_with_learned_dialect,
)


def _encode(profile, effort: ReasoningEffort) -> dict:
    body: dict = {}
    profile.reasoning.encode(
        body, ReasoningPolicy(control=ReasoningControl.ON, effort=effort)
    )
    return body


def test_the_generic_profile_still_cannot_spell_max() -> None:
    """The state of the world before a probe, unchanged and deliberate."""
    assert _encode(GENERIC_OPENAI_PROFILE, ReasoningEffort.MAX) == {
        "reasoning_effort": "high"
    }


def test_a_learned_vocabulary_puts_max_on_the_wire() -> None:
    profile = profile_with_learned_dialect(
        GENERIC_OPENAI_PROFILE, ("low", "high", "max")
    )

    assert _encode(profile, ReasoningEffort.MAX) == {"reasoning_effort": "max"}
    assert _encode(profile, ReasoningEffort.HIGH) == {"reasoning_effort": "high"}
    # No ``medium`` rung on this host, so it clamps to the nearest at or below
    # -- the ordinary rule, applied to a vocabulary the host named itself.
    assert _encode(profile, ReasoningEffort.MEDIUM) == {"reasoning_effort": "low"}


def test_the_learned_dialect_declares_the_hosts_vocabulary() -> None:
    dialect = profile_with_learned_dialect(
        GENERIC_OPENAI_PROFILE, ("low", "high", "max")
    ).reasoning.dialect

    assert dialect.effort_values is not None
    assert sorted(effort.value for effort in dialect.effort_values) == [
        "high",
        "low",
        "max",
    ]
    assert dialect.origin is ReasoningDialectOrigin.LEARNED
    assert dialect.off is False


def test_an_off_word_becomes_the_off_spelling_not_a_rung() -> None:
    encoder = learned_named_effort_reasoning(("none", "low", "high"))

    assert encoder is not None
    assert encoder.disabled_value == "none"
    assert encoder.dialect.off is True
    body: dict = {}
    encoder.encode(body, ReasoningPolicy(control=ReasoningControl.OFF))
    assert body == {"reasoning_effort": "none"}


def test_a_hosts_own_words_are_spread_over_the_scale_in_order() -> None:
    """A gateway whose enum is not FCC's keeps an effort field regardless."""
    encoder = learned_named_effort_reasoning(("brief", "detailed"))

    assert encoder is not None
    assert dict(encoder.efforts)[ReasoningEffort.MINIMAL] == "brief"
    assert dict(encoder.efforts)[ReasoningEffort.MAX] == "detailed"


@pytest.mark.parametrize("words", [(), ("none",), ("off", "disabled")])
def test_a_vocabulary_with_no_rungs_changes_nothing(words: tuple[str, ...]) -> None:
    assert learned_named_effort_reasoning(words) is None
    assert profile_with_learned_dialect(GENERIC_OPENAI_PROFILE, words) is (
        GENERIC_OPENAI_PROFILE
    )


def test_the_seam_is_one_replace_of_the_reasoning_field() -> None:
    """Everything but ``reasoning`` is the profile a custom provider already had.

    Stated as a test because it is the whole safety argument: transport,
    request policy, extra-body validation, the listing shape and the reasoning
    delta field are untouched, so nothing downstream can tell a learned
    dialect from a declared one.
    """
    learned = profile_with_learned_dialect(
        GENERIC_OPENAI_PROFILE, ("low", "high", "max")
    )

    assert (
        dataclasses.replace(learned, reasoning=GENERIC_OPENAI_PROFILE.reasoning)
        == GENERIC_OPENAI_PROFILE
    )


def test_the_registry_carries_the_vocabulary_onto_the_descriptor(tmp_path) -> None:
    registry = ProviderRegistry(tmp_path / "custom_providers.json")
    entry = registry.add(
        display_name="B AI",
        base_url="https://api.example/v1",
        api_keys=("sk-test-key-000011112222",),
    )
    registry.update(entry.provider_id, reasoning_effort_enum=["low", "high", "max"])

    descriptor = registry.all_descriptors()[entry.provider_id]

    assert descriptor.reasoning_effort_enum == ("low", "high", "max")


def test_the_vocabulary_survives_a_reload_from_disk(tmp_path) -> None:
    path = tmp_path / "custom_providers.json"
    registry = ProviderRegistry(path)
    entry = registry.add(
        display_name="B AI",
        base_url="https://api.example/v1",
        api_keys=("sk-test-key-000011112222",),
    )
    registry.update(
        entry.provider_id,
        reasoning_effort_enum=["low", "high", "max"],
        reasoning_probe_status="learned",
        reasoning_probed_at="2026-09-01T00:00:00+00:00",
    )

    reloaded = ProviderRegistry(path).get(entry.provider_id)

    assert reloaded is not None
    assert reloaded.reasoning_effort_enum == ("low", "high", "max")
    assert reloaded.reasoning_probe_status == "learned"
    assert reloaded.reasoning_field_ignored is False
