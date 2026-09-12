"""The provider catalogue, written by the sweep and read by the next start.

What these hold: a catalogue survives the round trip unchanged (which is the
equality contract -- a restored catalogue must be the catalogue, not a
reasonable approximation of it), every field of every record actually travels,
a sweep that learned nothing does not rewrite the file, and a document that
describes a different installation is ignored rather than trusted.
"""

import json
import time
from dataclasses import fields
from typing import Any

from my_claude_code.application import model_metadata
from my_claude_code.application.model_metadata import (
    ModelListingEvidence,
    ModelListingProvenance,
    ModelReasoningCapability,
    ProviderModelInfo,
    canonical_model_info,
    model_info_document,
    model_info_from_document,
)
from my_claude_code.core.derived_cache import DerivedCache
from my_claude_code.core.reasoning import ReasoningEffort
from my_claude_code.runtime.catalogue_store import (
    CATALOGUE_ENTRY,
    catalogue_document,
    catalogue_scope_key,
    catalogues_from_document,
    read_stored_catalogue,
    store_catalogue,
)


def _rich(model_id: str) -> ProviderModelInfo:
    """One entry with every optional field populated, including both sets."""

    return ProviderModelInfo(
        model_id=model_id,
        supports_thinking=True,
        supports_vision=False,
        context_length=200_000,
        input_price=1.5,
        output_price=3.0,
        max_output_tokens=64_000,
        supported_parameters=frozenset(
            {"temperature", "top_p", "top_k", "reasoning", "max_tokens"}
        ),
        default_parameters=(("temperature", 0.7), ("stream", True), ("top_k", 40)),
        reasoning_capability=ModelReasoningCapability(
            can_reason=True,
            supports_effort_control=True,
            supports_toggle_control=False,
            supports_budget_control=None,
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
            replacement_model_id=f"{model_id}-2",
            offered_by_default=True,
        ),
    )


def _bare(model_id: str) -> ProviderModelInfo:
    """And one with nothing stated at all, because NULL has to survive too."""

    return ProviderModelInfo(model_id=model_id)


def _catalogues() -> dict[str, tuple[ProviderModelInfo, ...]]:
    return {
        "nvidia_nim": (_rich("a/one"), _bare("a/two"), _rich("a/three")),
        "openrouter": (_bare("b/one"),),
    }


def test_one_entry_round_trips_unchanged() -> None:
    """Equality on the dataclass, not on a summary of it."""
    for info in (_rich("x/y"), _bare("x/z")):
        assert model_info_from_document(model_info_document(info)) == info


def test_a_whole_catalogue_round_trips_unchanged() -> None:
    """The equality contract: restored *is* the catalogue."""
    catalogues = _catalogues()
    restored = catalogues_from_document(catalogue_document(catalogues))
    assert restored == catalogues
    for provider_id, infos in catalogues.items():
        assert [canonical_model_info(info) for info in infos] == [
            canonical_model_info(info) for info in restored[provider_id]
        ]


def test_the_order_a_provider_listed_its_models_in_survives() -> None:
    """It is what ``/v1/models`` and the Models page put first."""
    catalogues = {"p": tuple(_bare(f"p/m{index}") for index in range(12))}
    restored = catalogues_from_document(catalogue_document(catalogues))
    assert [info.model_id for info in restored["p"]] == [
        info.model_id for info in catalogues["p"]
    ]


def test_every_field_is_carried_by_the_codec() -> None:
    """A field the codec forgets is a field every restart silently drops."""
    assert {field.name for field in fields(ProviderModelInfo)} == set(
        model_metadata._MODEL_INFO_FIELDS
    )
    assert {field.name for field in fields(ModelReasoningCapability)} == set(
        model_metadata._REASONING_FIELDS
    )
    assert {field.name for field in fields(ModelListingEvidence)} == set(
        model_metadata._LISTING_FIELDS
    )


def test_the_document_is_json_and_deterministic() -> None:
    """Deterministic is what lets an unchanged sweep skip the write."""
    first = json.dumps(catalogue_document(_catalogues()))
    second = json.dumps(catalogue_document(_catalogues()))
    assert first == second


def test_a_sweep_that_changes_nothing_does_not_rewrite_the_file(tmp_path) -> None:
    """Twelve documents rewritten hourly to say the same thing is churn."""
    cache = DerivedCache(tmp_path)
    key = catalogue_scope_key(("nvidia_nim", "openrouter"))
    assert store_catalogue(_catalogues(), key, computed_at=100.0, cache=cache)
    written = cache.path_for(CATALOGUE_ENTRY).stat().st_mtime_ns

    assert not store_catalogue(_catalogues(), key, computed_at=200.0, cache=cache)
    assert cache.path_for(CATALOGUE_ENTRY).stat().st_mtime_ns == written

    changed = dict(_catalogues())
    changed["nvidia_nim"] = (*changed["nvidia_nim"], _bare("a/four"))
    assert store_catalogue(changed, key, computed_at=300.0, cache=cache)


def test_a_stored_catalogue_reports_the_age_it_was_written_with(tmp_path) -> None:
    """ "as of 12:57", not "just swept"."""
    cache = DerivedCache(tmp_path)
    key = catalogue_scope_key(("nvidia_nim", "openrouter"))
    written_at = time.time() - 3600.0
    assert store_catalogue(_catalogues(), key, computed_at=written_at, cache=cache)

    stored = read_stored_catalogue(key, cache=cache)
    assert stored is not None
    restored, reported_at = stored
    assert restored == _catalogues()
    assert reported_at == written_at


def test_a_document_written_for_another_provider_scope_is_not_loaded(tmp_path) -> None:
    """A different set of providers is a different installation."""
    cache = DerivedCache(tmp_path)
    store_catalogue(
        _catalogues(),
        catalogue_scope_key(("nvidia_nim", "openrouter")),
        computed_at=100.0,
        cache=cache,
    )
    assert (
        read_stored_catalogue(catalogue_scope_key(("nvidia_nim",)), cache=cache) is None
    )


def test_an_unreadable_document_loads_nothing_and_raises_nothing(tmp_path) -> None:
    """The answer to a broken cache is the sweep, never a failed start."""
    cache = DerivedCache(tmp_path)
    key = catalogue_scope_key(("nvidia_nim",))
    cache.write(
        CATALOGUE_ENTRY, key=key, payload={"providers": "nonsense"}, computed_at=1.0
    )
    assert read_stored_catalogue(key, cache=cache) is None

    assert catalogues_from_document(None) == {}
    assert catalogues_from_document({"providers": [{"provider_id": 7}]}) == {}
    assert model_info_from_document({"model_id": ""}) is None
    assert model_info_from_document("not a document") is None


def test_a_listing_with_no_usable_provenance_is_dropped_not_invented() -> None:
    """Provenance is the one field with no honest default."""
    document = model_info_document(_rich("x/y"))
    listing: Any = document["listing"]
    assert isinstance(listing, dict)
    listing["provenance"] = "something-nobody-ships"
    restored = model_info_from_document(document)
    assert restored is not None
    assert restored.listing is None


def test_a_value_of_the_wrong_type_is_read_as_unstated() -> None:
    """Unstated is the honest reading of a field nobody can parse."""
    document = model_info_document(_bare("x/y"))
    document["context_length"] = "lots"
    document["supports_vision"] = 1
    restored = model_info_from_document(document)
    assert restored is not None
    assert restored.context_length is None
    assert restored.supports_vision is None
