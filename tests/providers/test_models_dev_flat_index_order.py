"""The flat index must answer the same whatever order models.dev was serialised in (8.18.0, Q4).

`_flatten_index_uncached` accumulates field by field, and `_merge_metadata`
keeps the FIRST writer's value — so for a provider with no models.dev bucket of
its own, *which* value the flat index answers was decided by whichever bucket
appeared first in the document's serialisation order.

Measured on the operator's catalogue before the fix: 168 of 4,931 flat-index
keys have carriers that disagree about vision, and **31** of their records flip
their answer when the bucket list is reversed. This test is the direct proof of
the property the investigation used to find them: resolve every key twice, once
with the index as stored and once with its buckets reversed, and require the
same answer.

Two things this is NOT, both measured and rejected:

* **a True-favouring OR** — it moves 24 records ``False -> True`` on the
  operator's catalogue, including ``gpt-3.5-turbo``, ``o3-mini`` and
  ``gpt-3.5-turbo-instruct``, which are certainly text-only. A stranger's
  ``True`` is not evidence.
* **withholding everything a namesake says** — a key whose carriers AGREE is
  still answered, and that is most of the index.
"""

from collections.abc import Mapping
from typing import Any

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.providers.runtime import models_dev
from my_claude_code.providers.runtime.models_dev import (
    _flatten_index_uncached,
    enrich_model_infos,
)

#: Two buckets that claim one id and disagree about vision, plus a third that
#: claims it and agrees with the first. The third is what makes a two-writer
#: implementation look order-independent while still being order-decided.
DISAGREEING_INDEX: dict[str, Any] = {
    "alpha": {"models": {"shared-name": {"modalities": {"input": ["text", "image"]}}}},
    "beta": {"models": {"shared-name": {"modalities": {"input": ["text"]}}}},
    "gamma": {"models": {"shared-name": {"modalities": {"input": ["text", "image"]}}}},
}


def _reversed(index: Mapping[str, Any]) -> dict[str, Any]:
    return {key: index[key] for key in reversed(list(index))}


def _answer(index: Mapping[str, Any], model_id: str) -> Any:
    metadata = models_dev._match_metadata(_flatten_index_uncached(index), model_id)
    return None if metadata is None else metadata.supports_vision


def test_a_disagreement_is_withheld_under_every_order() -> None:
    for index in (DISAGREEING_INDEX, _reversed(DISAGREEING_INDEX)):
        assert _answer(index, "shared-name") is None


def test_the_property_holds_over_every_permutation() -> None:
    """Not just the reversed order: every order gives the same answer."""

    import itertools

    names = list(DISAGREEING_INDEX)
    answers = set()
    for permutation in itertools.permutations(names):
        index = {name: DISAGREEING_INDEX[name] for name in permutation}
        answers.add(_answer(index, "shared-name"))
    assert answers == {None}


def test_carriers_that_agree_are_still_answered() -> None:
    """Withholding is for a disagreement, never for a namesake."""

    agreeing = {
        "alpha": {
            "models": {"shared-name": {"modalities": {"input": ["text", "image"]}}}
        },
        "beta": {
            "models": {"shared-name": {"modalities": {"input": ["text", "image"]}}}
        },
    }
    for index in (agreeing, _reversed(agreeing)):
        assert _answer(index, "shared-name") is True

    silent = {"alpha": {"models": {"shared-name": {}}}}
    assert _answer(silent, "shared-name") is None


def test_one_silent_carrier_does_not_withhold() -> None:
    """A carrier that states nothing is not a disagreement."""

    index = {
        "alpha": {
            "models": {"shared-name": {"modalities": {"input": ["text", "image"]}}}
        },
        "beta": {"models": {"shared-name": {"limit": {"context": 8_000}}}},
    }
    for order in (index, _reversed(index)):
        assert _answer(order, "shared-name") is True


def test_the_other_fields_keep_the_first_writer_rule() -> None:
    """Only vision is order-independent in this release, deliberately.

    A number that two strangers report differently is a different question with
    its own proof to make, so the three numeric fields keep exactly the rule
    they have always had and this release does not move them.
    """

    index = {
        "alpha": {"models": {"shared-name": {"limit": {"context": 8_000}}}},
        "beta": {"models": {"shared-name": {"limit": {"context": 128_000}}}},
    }
    forward = models_dev._match_metadata(_flatten_index_uncached(index), "shared-name")
    backward = models_dev._match_metadata(
        _flatten_index_uncached(_reversed(index)), "shared-name"
    )
    assert forward is not None and backward is not None
    assert (forward.context_length, backward.context_length) == (8_000, 128_000)


def test_a_record_still_reads_the_withheld_answer_as_unknown() -> None:
    """End to end: a provider with no bucket of its own gets ``None``, not a guess."""

    (info,) = enrich_model_infos(
        (ProviderModelInfo(model_id="shared-name"),),
        DISAGREEING_INDEX,
        "some-gateway",
    )
    assert info.supports_vision is None
