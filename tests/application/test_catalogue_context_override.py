"""Agent catalogues publish the operator's context window above every rung (7.87.0).

One overlay at the end of ``_resolve``: after the provider's record, the ladder
and OpenRouter's live list, so a forced unknown is never refilled from below.
Every other model, and every other field, is exactly what it was.
"""

from my_claude_code.application.catalogue_model import (
    OPERATOR_PROVENANCE,
    CatalogueFieldProvenance,
    CatalogueModel,
    build_catalogue_models,
)
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.application.openrouter_live import (
    LIVE_MATCH_EXACT,
    LiveCatalogue,
    LiveModel,
)
from my_claude_code.config.model_overrides import (
    EMPTY_MODEL_OVERRIDES,
    ModelParameterOverrides,
)
from my_claude_code.config.settings import Settings
from tests.application.test_catalogue_model import FakeRuntime

AGNES = "custom_agnes/agnes-3.0-flash"
OTHER = "custom_agnes/agnes-2.5-flash"


def _settings() -> Settings:
    return Settings().model_copy(update={"model": AGNES})


def _table(value: object) -> ModelParameterOverrides:
    return ModelParameterOverrides.from_document(
        {"models": {AGNES: {"context_length": value}}}
    )


def _runtime() -> FakeRuntime:
    return FakeRuntime(
        settings=_settings(),
        cached_infos=(
            ProviderModelInfo(AGNES, context_length=524_288),
            ProviderModelInfo(OTHER, context_length=512_000),
        ),
        context_lengths={AGNES: 524_288, OTHER: 512_000},
        output_limits={AGNES: 65_536},
    )


def _by_id(models: tuple[CatalogueModel, ...]) -> dict[str, CatalogueModel]:
    return {model.gateway_id: model for model in models}


def test_a_forced_window_reaches_both_variants_and_the_tier_aliases() -> None:
    runtime = _runtime()
    base = _by_id(
        build_catalogue_models(
            _settings(), runtime, model_overrides=EMPTY_MODEL_OVERRIDES
        )
    )
    forced = _by_id(
        build_catalogue_models(_settings(), runtime, model_overrides=_table(1_000_000))
    )

    changed = {
        gateway_id for gateway_id, model in forced.items() if model != base[gateway_id]
    }
    assert set(forced) == set(base)
    for gateway_id in changed:
        assert forced[gateway_id].context_length == 1_000_000
        assert base[gateway_id].context_length == 524_288
        # Nothing but the window moved.
        assert (
            forced[gateway_id].max_output_tokens == base[gateway_id].max_output_tokens
        )
    assert "anthropic/custom_agnes/agnes-3.0-flash" in changed
    # The other model of the same provider is untouched.
    assert forced["anthropic/custom_agnes/agnes-2.5-flash"].context_length == 512_000
    assert all(
        forced[gateway_id] == base[gateway_id] for gateway_id in set(forced) - changed
    )


def test_null_forces_unknown_and_live_never_refills_it() -> None:
    answer = LiveModel(
        slugs=("agnes/agnes-3.0-flash",),
        match=LIVE_MATCH_EXACT,
        context_length=2_000_000,
    )
    live = LiveCatalogue(
        mark="1:1", fetched_at=None, rows=1, lookup=lambda _p, _m: answer
    )
    runtime = FakeRuntime(
        settings=_settings(),
        cached_infos=(ProviderModelInfo(AGNES),),
        live=live,
    )

    inherited = _by_id(
        build_catalogue_models(
            _settings(), runtime, model_overrides=EMPTY_MODEL_OVERRIDES
        )
    )
    unknown = _by_id(
        build_catalogue_models(_settings(), runtime, model_overrides=_table(None))
    )

    gateway_id = "anthropic/custom_agnes/agnes-3.0-flash"
    assert inherited[gateway_id].context_length == 2_000_000  # the live gap fill
    assert unknown[gateway_id].context_length is None


def test_provenance_names_the_operator_only_where_provenance_was_asked_for() -> None:
    ladder = CatalogueFieldProvenance(
        source="provider", source_label="provider /models"
    )
    runtime = _runtime()

    with_provenance = _by_id(
        build_catalogue_models(
            _settings(),
            runtime,
            provenance=lambda _p, _m, _i: {"context_length": ladder},
            model_overrides=_table(1_000_000),
        )
    )
    without = _by_id(
        build_catalogue_models(_settings(), runtime, model_overrides=_table(1_000_000))
    )

    agnes = "anthropic/custom_agnes/agnes-3.0-flash"
    other = "anthropic/custom_agnes/agnes-2.5-flash"
    assert (
        with_provenance[agnes].field_provenance["context_length"] == OPERATOR_PROVENANCE
    )
    assert with_provenance[other].field_provenance["context_length"] == ladder
    assert without[agnes].field_provenance == {}
