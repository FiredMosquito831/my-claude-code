"""The five tier aliases, as every coding agent's generated document sees them.

The alias is one more ``CatalogueModel`` built by copying the model the tier
points at and replacing two fields. That is the whole design, and it is what
lets no serialiser know a tier exists -- so these tests are mostly about
proving the copy is faithful and that nothing per-CLI was needed to make it
work.
"""

import json
from dataclasses import fields, replace
from typing import Any

import pytest

from my_claude_code.application.catalogue_model import (
    CatalogueModel,
    build_catalogue_models,
)
from my_claude_code.application.catalogues import SERIALISERS, model_entries, serialise
from my_claude_code.application.catalogues.base import starting_model, visible_entries
from my_claude_code.application.model_metadata import (
    ModelReasoningCapability,
    ProviderModelInfo,
)
from my_claude_code.config.harness_tiers import HarnessTierOverride, HarnessTiers
from my_claude_code.config.settings import Settings
from my_claude_code.core.gateway_model_ids import gateway_model_id
from my_claude_code.core.reasoning import ReasoningEffort
from my_claude_code.core.tier_refs import TIER_ORDER, ModelTier, tier_ref
from tests.application.test_catalogue_model import FakeRuntime

PRIMARY = "nvidia_nim/primary"
CHEAP = "open_router/cheap"
OTHER = "open_router/other"


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {"model": PRIMARY, "MODEL_HAIKU": CHEAP}
    values.update(overrides)
    return Settings(**values)


#: One fully-stated model, so an alias copying it copies real numbers rather
#: than a record of Nones that would pass a "verbatim" check trivially.
_REASONING = ModelReasoningCapability(
    can_reason=True,
    supports_effort_control=True,
    supported_efforts=frozenset({ReasoningEffort.LOW, ReasoningEffort.HIGH}),
)
_REFS = (PRIMARY, CHEAP, OTHER)


def _models(settings: Settings | None = None, **kwargs) -> tuple[CatalogueModel, ...]:
    resolved = settings if settings is not None else _settings()
    runtime = FakeRuntime(
        settings=resolved,
        cached_infos=tuple(ProviderModelInfo(ref) for ref in _REFS),
        context_lengths=dict.fromkeys(_REFS, 262_144),
        output_limits=dict.fromkeys(_REFS, 32_768),
        vision=dict.fromkeys(_REFS, True),
        thinking=dict.fromkeys(_REFS, True),
        reasoning=dict.fromkeys(_REFS, _REASONING),
        tool_calls=dict.fromkeys(_REFS, True),
        prices={ref: {"input_price": 1.0, "output_price": 2.0} for ref in _REFS},
    )
    return build_catalogue_models(resolved, runtime, **kwargs)


@pytest.mark.parametrize("format_id", sorted(SERIALISERS))
def test_every_catalogue_lists_the_five_aliases(format_id: str) -> None:
    """All thirteen agents, including the four with no vision field at all.

    ``mcc/vision`` is not only an image route: it is the route the operator
    reserved for the strongest multimodal model, and a text prompt sent to it
    routes there perfectly well. Suppressing it per CLI would put capability
    logic back into a layer that is deliberately a pure function of the records.
    """

    document, _defaulted = serialise(format_id, _models())
    rendered = json.dumps(document)

    for tier in TIER_ORDER:
        assert tier_ref(tier) in rendered, f"{format_id} is missing {tier_ref(tier)}"


@pytest.mark.parametrize("format_id", sorted(SERIALISERS))
def test_an_alias_never_invents_a_number(format_id: str) -> None:
    """An alias entry adds no key the entry it copied does not have.

    The alias is the primary's record with two fields replaced, so every
    serialiser writes exactly the same shape for both -- which is the mechanical
    reason ``test_no_serialiser_hard_codes_a_limit`` stays unaffected.
    """

    document, _defaulted = serialise(format_id, _models())
    entries = model_entries(format_id, document)
    keyed = [entry for entry in entries if isinstance(entry, dict)]
    if not keyed:
        pytest.skip(f"{format_id} entries are not objects")
    shapes = {frozenset(entry) for entry in keyed}
    # Every entry in a document has the same key set, alias or not.
    assert len(shapes) == 1


def test_an_alias_carries_the_primarys_metadata_verbatim() -> None:
    """Field by field, ``None``s and provenance included.

    The alternative -- a MIN across the chain -- is a number no source ever
    stated: arithmetic MCC performed on two providers' answers. It would also
    silently downgrade the common case, since a fallback is only ever reached on
    a failure, and it would flip whenever the operator reordered a rail.
    """

    models = _models()
    best = next(model for model in models if model.provider_model_ref == "mcc/best")
    primary = next(
        model
        for model in models
        if model.provider_model_ref == PRIMARY and not model.force_no_thinking
    )

    copied = {
        field.name
        for field in fields(CatalogueModel)
        if field.name
        not in {"gateway_id", "provider_model_ref", "display_name", "is_primary_route"}
    }
    for name in copied:
        assert getattr(best, name) == getattr(primary, name), name

    # Only the identity moved, and the display name says both the promise and
    # today's answer.
    assert best.gateway_id == gateway_model_id("mcc/best")
    assert best.display_name == f"Best ({PRIMARY})"


def test_an_alias_keeps_both_wire_spellings_apart_from_the_model_it_names() -> None:
    """Carrying the primary's ref would collide in six of the thirteen agents.

    Codex, Command Code, OpenCode, Kilo, Pi and Kimi key their documents on the
    bare ref and skip a duplicate, so an alias carrying ``nvidia_nim/primary``
    would be silently dropped from all six pickers.
    """

    models = _models()
    refs = [model.provider_model_ref for model in models]

    assert refs.count("mcc/best") == 1
    assert refs.count(PRIMARY) >= 1
    assert PRIMARY != "mcc/best"


def test_the_aliases_sort_first() -> None:
    """The only discoverability lever this layer has.

    It puts the tiers at the top of OpenCode's and Qwen's pickers and gives them
    priority 0-4 in Codex, whose ``priority`` is a bare enumeration index.
    """

    refs = [model.provider_model_ref for model in _models()]

    assert refs[:5] == [tier_ref(tier) for tier in TIER_ORDER]


def test_best_is_the_only_record_marked_as_the_primary_route() -> None:
    """``select_starting_index`` returns the first marked entry.

    Leaving the mark on the raw ``MODEL`` record as well would make which of the
    two a session opened on depend on enumeration order, which is the exact
    class of bug that function exists to remove.
    """

    marked = [model.provider_model_ref for model in _models() if model.is_primary_route]

    assert marked == ["mcc/best"]


def test_crush_seeds_large_on_best_and_small_on_cheap() -> None:
    """Crush's two-role split, answered honestly for the first time.

    ``small`` used to repeat ``large`` because inventing a cheap model by
    matching on a name would have been MCC guessing. ``mcc/cheap`` is not a
    guess: it is the route the operator labelled cheap.
    """

    document, _defaulted = serialise("crush", _models())

    assert document["models"]["large"]["model"] == gateway_model_id("mcc/best")
    assert document["models"]["small"]["model"] == gateway_model_id("mcc/cheap")


def test_crush_small_falls_back_to_large_without_the_aliases() -> None:
    """Exactly the behaviour before this change, when the switch is off."""

    models = _models(_settings(HARNESS_TIER_ALIASES=False))
    document, _defaulted = serialise("crush", models)

    assert document["models"]["small"] == document["models"]["large"]


def test_cline_seeds_its_session_model_on_best() -> None:
    """Cline opens on exactly one model and freezes it into its own config."""

    document, _defaulted = serialise("cline", _models())

    provider = next(iter(document["providers"].values()))
    assert provider["settings"]["model"] == gateway_model_id("mcc/best")


def test_gemini_cli_seeds_through_starting_model_not_first_entry() -> None:
    """A live instance of the enumeration-artefact bug, fixed here.

    Gemini CLI never called ``starting_model`` and never read ``MODEL``: it
    pinned whichever entry happened to enumerate first. With the aliases in
    front it would land on ``mcc/best`` by accident; going through the shared
    rule makes that a decision instead.
    """

    models = _models()
    document, _defaulted = serialise("gemini_cli", models)
    assert document["model"]["name"] == gateway_model_id("mcc/best")

    # And with the aliases switched off it lands on MODEL rather than on
    # whatever enumerated first -- which is the half of the fix that the
    # aliases would otherwise have hidden.
    plain = _models(_settings(HARNESS_TIER_ALIASES=False))
    document, _defaulted = serialise("gemini_cli", plain)
    chosen = starting_model(visible_entries(plain))
    assert chosen is not None
    assert document["model"]["name"] == chosen.gateway_id


def test_kimi_key_is_mcc_slash_mcc_slash_best_and_the_wire_value_is_mcc_slash_best() -> (
    None
):
    """Ugly in one CLI's --model help, correct everywhere.

    Kimi builds its key as ``mcc/<ref>``. Special-casing it would break the
    "pure function of the records" property that makes this layer testable.
    """

    document, _defaulted = serialise("kimi", _models())

    assert document["models"]["mcc/mcc/best"]["model"] == "mcc/best"


def test_pi_extension_accepts_the_alias_id() -> None:
    """Pi's bundled extension needs two non-empty segments after the prefix.

    ``providerModelRef`` in ``cli/launchers/pi_extension.ts`` strips
    ``anthropic/`` and rejects anything that then splits into fewer than two
    non-empty parts -- which is why the alias is two segments and not three.
    """

    for tier in TIER_ORDER:
        gateway = gateway_model_id(tier_ref(tier))
        parts = gateway.removeprefix("anthropic/").split("/")
        assert len(parts) >= 2
        assert all(parts)


def test_HARNESS_TIER_ALIASES_false_removes_every_alias() -> None:
    """The supported way to shorten an already-long picker.

    It removes the entries; it does not stop the router resolving one a client
    sends anyway, because an id that used to work must keep working.
    """

    models = _models(_settings(HARNESS_TIER_ALIASES=False))
    refs = {model.provider_model_ref for model in models}

    assert not any(ref.startswith("mcc/") for ref in refs)
    assert {model.provider_model_ref for model in models if model.is_primary_route} == {
        PRIMARY
    }


def test_a_per_harness_override_changes_only_that_agents_document() -> None:
    """The picker and the router must never be able to disagree.

    The alias's display name promises a model; if the document were built from
    the global chain while the router served the override, the picker would be
    lying on every request.
    """

    tiers = HarnessTiers(
        harnesses={"opencode": {"best": HarnessTierOverride(model=OTHER)}}
    )
    settings = _settings()

    for_opencode = _models(settings, harness_id="opencode", harness_tiers=tiers)
    for_crush = _models(settings, harness_id="crush", harness_tiers=tiers)

    best_opencode = next(
        model for model in for_opencode if model.provider_model_ref == "mcc/best"
    )
    best_crush = next(
        model for model in for_crush if model.provider_model_ref == "mcc/best"
    )

    assert best_opencode.display_name == f"Best ({OTHER})"
    assert best_crush.display_name == f"Best ({PRIMARY})"


def test_a_tier_pointing_at_an_unlisted_model_gets_no_entry() -> None:
    """An alias the picker cannot select is worse than no alias.

    The catalogue is the list of routable models; a tier resolving to something
    absent from it would render an entry whose metadata MCC does not have.
    """

    settings = _settings(MODEL_VISIBILITY_DENY=CHEAP)
    models = _models(settings)
    refs = {model.provider_model_ref for model in models}

    assert "mcc/cheap" not in refs
    assert "mcc/best" in refs


def test_an_alias_is_not_a_no_thinking_variant() -> None:
    """``visible_entries`` re-projects a surviving no-thinking twin.

    An alias marked ``force_no_thinking`` would be rewritten onto its own plain
    ref by that pass and lose the reasoning capability it copied.
    """

    aliases = [
        model for model in _models() if model.provider_model_ref.startswith("mcc/")
    ]

    assert aliases
    assert not any(model.force_no_thinking for model in aliases)
    assert all(model in visible_entries(_models()) for model in aliases)


def test_the_no_thinking_twin_never_shadows_an_alias() -> None:
    """Dedup is by gateway id, and the alias's is unique by construction."""

    ids = [model.gateway_id for model in _models()]

    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("tier", list(TIER_ORDER))
def test_replacing_only_two_fields_is_what_the_builder_does(tier: ModelTier) -> None:
    """Pinned as an equality against a hand-built copy, per tier.

    If the builder ever started adjusting a capability field to "fit" the tier,
    this is the test that says which field and which tier.
    """

    models = _models()
    alias = next(
        model for model in models if model.provider_model_ref == tier_ref(tier)
    )
    source_ref = alias.display_name.split("(", 1)[1].rstrip(")")
    source = next(
        model
        for model in models
        if model.provider_model_ref == source_ref and not model.force_no_thinking
    )

    assert alias == replace(
        source,
        gateway_id=alias.gateway_id,
        provider_model_ref=alias.provider_model_ref,
        display_name=alias.display_name,
        is_primary_route=alias.is_primary_route,
        force_no_thinking=False,
    )
