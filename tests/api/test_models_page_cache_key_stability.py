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
from dataclasses import fields, replace
from typing import Any

import pytest

from my_claude_code.api.models_page_cache import capability_half_key
from my_claude_code.application import model_metadata
from my_claude_code.application.model_metadata import (
    DeclaredModalities,
    ModelListingEvidence,
    ModelListingProvenance,
    ModelReasoningCapability,
    ProviderModelDeclaration,
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
        declared=ProviderModelDeclaration(
            modalities=DeclaredModalities(inputs=("image", "text"), outputs=("text",)),
            model_type="chat",
            endpoints=("/chat/completions", "/responses"),
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


@pytest.mark.local_serial
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
    """A field the encoding skips is a change the key cannot see.

    The one field left out is declared so on the dataclass (7.86.0): the
    provider's verbatim list row, which the page never shows -- it is read
    only by the on-demand "Everything known" view -- and whose volatile keys
    would otherwise recompute the page for nothing it draws.
    """
    encoded = canonical_model_info(_info())
    for record in (
        ProviderModelInfo,
        ModelReasoningCapability,
        ModelListingEvidence,
        ProviderModelDeclaration,
        DeclaredModalities,
    ):
        for field in fields(record):
            if field.metadata.get("canonical", True) is False:
                assert field.name in model_metadata._MODEL_INFO_STORED_ELSEWHERE
                assert f"{field.name}=" not in encoded
                continue
            assert f"{field.name}=" in encoded


def test_the_list_row_a_record_keeps_never_moves_the_key() -> None:
    """Two catalogues that differ only in the rows' own words are one page."""
    info = _info()
    assert _key((info,)) == _key((replace(info, published_row='{"id":"x"}'),))


def test_a_changed_declaration_is_a_different_catalogue() -> None:
    """What the provider says a model is reaches the key like every other field."""
    info = _info()
    assert info.declared is not None
    retyped = replace(info, declared=replace(info.declared, model_type="language"))
    assert _key((info,)) != _key((retyped,))


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


def test_openrouter_s_live_list_is_part_of_the_key() -> None:
    """7.84.0: the rung on with a list, a newer list, and the rung off are three pages."""

    infos = (_info(),)
    base = capability_half_key(
        infos, (), ModelVisibility(allow=(), deny=()), ModelParameterOverrides()
    )
    assert base == _key(infos)

    def with_mark(mark: str | None) -> str:
        return capability_half_key(
            infos,
            (),
            ModelVisibility(allow=(), deny=()),
            ModelParameterOverrides(),
            live_mark=mark,
        )

    assert with_mark(None) == base
    assert with_mark("1:100") != base
    assert with_mark("1:100") == with_mark("1:100")
    assert with_mark("2:100") != with_mark("1:100")
