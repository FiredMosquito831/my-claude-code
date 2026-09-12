"""The Models-page cache key has to name the same catalogue after a restart.

6.77.0 stores the expensive half of the Models page on disk under a digest of
its inputs, so that a restart serves it instead of spending 5.5 s rebuilding it.
The digest took the catalogue in as ``repr(info)`` per model -- and
``ProviderModelInfo`` carries two ``frozenset``s of strings, whose ``repr``
lists members in hash order, which CPython salts per process. The key therefore
changed on every start, and the cache that exists to survive a restart missed on
every one of them.
"""

import os
import pathlib
import subprocess
import sys
from dataclasses import fields
from typing import Any

from my_claude_code.api.models_page_cache import capability_half_key
from my_claude_code.application.model_metadata import (
    ModelListingEvidence,
    ModelListingProvenance,
    ModelReasoningCapability,
    ProviderModelInfo,
    canonical_model_info,
)
from my_claude_code.config.model_overrides import ModelParameterOverrides
from my_claude_code.core.model_visibility import ModelVisibility
from my_claude_code.core.reasoning import ReasoningEffort

_PARAMETERS = (
    "temperature",
    "top_p",
    "top_k",
    "reasoning",
    "max_tokens",
    "stop",
    "seed",
    "logit_bias",
)


def _info(parameters: tuple[str, ...] = _PARAMETERS) -> ProviderModelInfo:
    """One catalogue entry with every optional field actually populated."""

    return ProviderModelInfo(
        model_id="prov/model-1",
        supports_thinking=True,
        supports_vision=False,
        context_length=200_000,
        input_price=1.5,
        output_price=3.0,
        max_output_tokens=64_000,
        supported_parameters=frozenset(parameters),
        default_parameters=(("temperature", 0.7), ("top_p", 0.95)),
        reasoning_capability=ModelReasoningCapability(
            can_reason=True,
            supports_effort_control=True,
            supports_toggle_control=False,
            supports_budget_control=False,
            supported_efforts=frozenset(
                {ReasoningEffort.LOW, ReasoningEffort.HIGH, ReasoningEffort.MEDIUM}
            ),
            mandatory=False,
            default_enabled=True,
        ),
        listing=ModelListingEvidence(
            provenance=ModelListingProvenance.GATEWAY,
            detail="listed by the gateway",
            retirement_at="2027-01-01",
            replacement_model_id="prov/model-2",
            offered_by_default=True,
        ),
    )


def _key(infos: tuple[ProviderModelInfo, ...]) -> str:
    return capability_half_key(
        infos, (), ModelVisibility(allow=(), deny=()), ModelParameterOverrides()
    )


_CHILD = """
import sys
sys.path.insert(0, {tests!r})
from test_models_page_cache_key_stability import _info, _key
print(_key((_info(),)))
"""


def test_the_key_is_the_same_in_two_interpreters() -> None:
    """The real failure, reproduced the way it really happens: two processes.

    ``PYTHONHASHSEED`` is what a restart varies by itself; pinning two different
    values makes the salt deterministic rather than lucky, so this test fails on
    the old key every time rather than four times in five.
    """
    here = str(pathlib.Path(__file__).resolve().parent)
    script = _CHILD.format(tests=here)
    keys = []
    for seed in ("1", "2"):
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
            check=False,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        keys.append(result.stdout.strip())
    assert keys[0] == keys[1], keys


def test_the_key_does_not_move_when_a_set_is_built_in_another_order() -> None:
    """Same members, different insertion order, same catalogue."""
    forwards = _info(_PARAMETERS)
    backwards = _info(tuple(reversed(_PARAMETERS)))
    assert canonical_model_info(forwards) == canonical_model_info(backwards)
    assert _key((forwards,)) == _key((backwards,))


def test_a_catalogue_that_really_changed_gets_a_different_key() -> None:
    """Stability is not the same as blindness."""
    assert _key((_info(),)) != _key((_info(_PARAMETERS[:-1]),))


def test_every_field_of_the_catalogue_entry_reaches_the_digest() -> None:
    """A field the encoding skips is a change the key cannot see."""
    encoded = canonical_model_info(_info())
    for record in (ProviderModelInfo, ModelReasoningCapability, ModelListingEvidence):
        for field in fields(record):
            assert f"{field.name}=" in encoded


def test_an_unencodable_value_is_refused_rather_than_dropped() -> None:
    """Silence about a field is the one failure mode a digest cannot afford."""
    unencodable: Any = object()
    try:
        canonical_model_info(
            ProviderModelInfo(
                model_id="prov/m", default_parameters=(("k", unencodable),)
            )
        )
    except TypeError as exc:
        assert "canonical encoding" in str(exc)
    else:
        raise AssertionError("an unencodable value was encoded anyway")
