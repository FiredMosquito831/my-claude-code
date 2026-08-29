"""Payload construction for the admin Models page.

Three questions the dashboard could not answer before this module existed:

1. *Which models are shown, and why is this one missing?* Visibility is two
   glob lists (``core.model_visibility``), and an explicit model pick is just
   an exact-match pattern written into one of them. Nothing here invents a
   second store.
2. *What will actually be sent for this model?* Parameter overrides
   (``config.model_overrides``) have three states per parameter -- inherit,
   force-unset, force a value -- and the resolved answer is a merge of the
   provider row under the model row, decided per parameter.
3. *Where did MCC learn this model's limits?* Capability metadata is resolved
   from up to four layers of decreasing authority, and a value taken from the
   approximate cross-provider tier is a vote across same-named rows in *other*
   providers' buckets. That tier is why one gateway's free model is credited
   with a 1,048,576-token output limit off a single match. It must never look
   like a published number, so every field carries the tier it came from.

Read-only for capabilities; the visibility and override sections write through
the existing owners (``apply_admin_config`` and ``save_model_overrides``).
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import replace
from typing import Any

from my_claude_code.application.model_metadata import (
    ModelReasoningCapability,
    ProviderModelInfo,
)
from my_claude_code.config.model_overrides import (
    ALLOWED_OVERRIDE_PARAMETERS,
    OWNED_ELSEWHERE_PARAMETERS,
    ModelParameterOverrides,
    normalize_override_key,
)
from my_claude_code.config.model_refs import (
    ConfiguredChatModelRef,
    parse_model_name,
    parse_provider_type,
)
from my_claude_code.core.model_ids import ResolutionTier
from my_claude_code.core.model_visibility import (
    MODEL_PATTERN_SEPARATOR,
    ModelVisibility,
)
from my_claude_code.core.reasoning import ReasoningDialect
from my_claude_code.providers.runtime.models_dev import (
    cross_provider_match,
    model_output_limit_tiered,
    model_reasoning_capability_tiered,
    models_dev_describes_provider,
)

# Where one capability field came from, most authoritative first. The strings
# are part of the admin API: the page renders a badge per field from them.
SOURCE_PROVIDER = "provider"
SOURCE_PROVIDER_OR_MODELS_DEV = "provider_or_models_dev"
SOURCE_MODELS_DEV = "models_dev"
SOURCE_APPROXIMATE = "approximate"
SOURCE_HOST_DIALECT = "host_dialect"

ReasoningDialectLookup = Callable[[str, str], ReasoningDialect | None]
SOURCE_UNKNOWN = "unknown"

SOURCE_LABELS: dict[str, str] = {
    SOURCE_PROVIDER: "provider /models",
    SOURCE_PROVIDER_OR_MODELS_DEV: "provider /models or models.dev",
    SOURCE_MODELS_DEV: "models.dev",
    SOURCE_APPROXIMATE: "approximate cross-provider",
    SOURCE_HOST_DIALECT: "host dialect",
    SOURCE_UNKNOWN: "unknown",
}

# What each rung of the ladder means, for the page's per-field tier column.
TIER_LABELS: dict[ResolutionTier, str] = {
    ResolutionTier.PROVIDER_EXACT: "provider /models, exact id",
    ResolutionTier.PROVIDER_TAG_STRIPPED: "provider /models, tag stripped",
    ResolutionTier.MODELS_DEV_BUCKET_EXACT: "models.dev bucket, exact id",
    ResolutionTier.MODELS_DEV_BUCKET_TAG_STRIPPED: ("models.dev bucket, tag stripped"),
    ResolutionTier.OPENROUTER_EXACT: "OpenRouter catalogue, exact id",
    ResolutionTier.OPENROUTER_TAG_STRIPPED: "OpenRouter catalogue, tag stripped",
    ResolutionTier.CROSS_PROVIDER_EXACT: "cross-provider, exact id",
    ResolutionTier.CROSS_PROVIDER_TAG_STRIPPED: "cross-provider, tag stripped",
    ResolutionTier.CROSS_PROVIDER_BARE_TAGGED: "cross-provider, bare model + tag",
    ResolutionTier.CROSS_PROVIDER_BARE_UNTAGGED: "cross-provider, bare model",
    ResolutionTier.FALLBACK_DEFAULT: "fallback default",
}

REASONING_FIELDS: tuple[str, ...] = (
    "can_reason",
    "supports_effort_control",
    "supports_toggle_control",
    "supports_budget_control",
    "supported_efforts",
    "mandatory",
    "default_enabled",
)

# The wire sentinel that deletes a parameter from an override row. JSON gives
# exactly two of the three states for free -- a value, and ``null`` for force
# unset -- so "inherit" needs a third token. ``null`` cannot double as "clear",
# because ``null`` is already the force-unset state, which is the point of the
# file.
INHERIT_SENTINEL = "inherit"

PROVIDER_SCOPE = "provider"
MODEL_SCOPE = "model"

# What the page must say out loud, because both are surprising.
HIDE_ONLY_NOTICE = (
    "Hiding a model only removes it from /v1/models and the admin pickers. "
    "A hidden model named in MODEL, MODEL_OPUS or a MODEL_*_FALLBACKS chain "
    "still resolves and still serves requests."
)
CONTEXT_LENGTH_SOURCE_NOTE = (
    "Context length is filled in from models.dev only when the provider left "
    "it unset, and the merged value keeps no record of which one won."
)


def _sourced(
    value: Any, source: str, tier: ResolutionTier | None = None, **extra: Any
) -> dict[str, Any]:
    """One capability field plus the rung of the ladder that stated it.

    ``source`` is the coarse badge the page has always rendered; ``tier`` is
    the exact rung (1-11) the resolver actually stopped at, which is what tells
    a "provider /models" answer that matched the id exactly from one that only
    matched after its pricing tag was stripped, and an approximate answer that
    kept the vendor prefix from one that fell back to the bare model name.
    """

    field: dict[str, Any] = {
        "value": value,
        "source": source,
        "source_label": SOURCE_LABELS[source],
        "approximate": source == SOURCE_APPROXIMATE,
        "reference": tier is not None and tier.is_reference,
        "tier": None if tier is None else int(tier),
        "tier_label": None if tier is None else TIER_LABELS[tier],
    }
    field.update(extra)
    return field


def _reasoning_field_value(capability: ModelReasoningCapability, name: str) -> Any:
    value = getattr(capability, name)
    if name == "supported_efforts" and value is not None:
        return sorted(effort.value for effort in value)
    return value


def _reasoning_source(
    name: str,
    provider: ModelReasoningCapability | None,
    from_models_dev: ModelReasoningCapability | None,
    models_dev_tiers: Mapping[str, ResolutionTier],
    *,
    provider_tier: ResolutionTier | None,
) -> tuple[str, ResolutionTier | None]:
    """Which layer and which rung stated one reasoning field.

    ``resolve_model_reasoning_capability`` layers provider over models.dev over
    the provider's documented vocabulary, field by field, so the first layer
    with a stated value is the one whose value is on screen. The rung comes
    from the resolver itself rather than being re-derived here: models.dev's
    answer may have come off this provider's own bucket (tiers 3-4), off the
    OpenRouter reference catalogue (tiers 5-6), or off the
    reference catalogue (tiers 5-6) or the approximate cross-provider vote
    (tiers 7-10), and only the resolver knows
    which, per field.
    """

    if provider is not None and getattr(provider, name) is not None:
        return SOURCE_PROVIDER, provider_tier
    if from_models_dev is not None and getattr(from_models_dev, name) is not None:
        tier = models_dev_tiers.get(name)
        source = (
            SOURCE_APPROXIMATE
            if tier is not None and tier.is_approximate
            else SOURCE_MODELS_DEV
        )
        return source, tier
    return SOURCE_UNKNOWN, None


def dialect_payload(dialect: ReasoningDialect | None) -> dict[str, Any]:
    """What the HOST parses for this model, beside what the MODEL supports.

    Two visibly different statements, which the page used to conflate: "this
    model can reason" is a vote across catalogues, "this host parses
    reasoning_effort" is a declaration by the code that will build the body (or
    by the gateway's own ``supported_parameters``, per model). A control
    reaches the wire only when both say yes, so an operator looking at a model
    that sends nothing needs to see which half said no.
    """

    if dialect is None:
        return {
            "known": False,
            "source": SOURCE_UNKNOWN,
            "source_label": SOURCE_LABELS[SOURCE_UNKNOWN],
        }
    return {
        "known": True,
        "source": SOURCE_HOST_DIALECT,
        "source_label": SOURCE_LABELS[SOURCE_HOST_DIALECT],
        "effort_field": dialect.effort_field or None,
        "effort_values": (
            None
            if dialect.effort_values is None
            else sorted(effort.value for effort in dialect.effort_values)
        ),
        "toggle": dialect.toggle,
        "toggle_field": dialect.toggle_field or None,
        "budget": dialect.budget,
        "budget_field": dialect.budget_field or None,
        "off": dialect.off,
        "adaptive": dialect.adaptive,
    }


def capability_payload(
    provider_id: str,
    model_id: str,
    info: ProviderModelInfo | None,
    provider_tier: ResolutionTier | None = None,
    dialect: ReasoningDialect | None = None,
) -> dict[str, Any]:
    """Read-only capability record for one model, tier-tagged per field.

    ``provider_tier`` is the rung the caller's own ``info`` was found at --
    tier 1 for an exact id, tier 2 when the pricing tag had to be stripped.
    It defaults to tier 1 because every existing caller looks the model up by
    its exact id.
    """

    described = models_dev_describes_provider(provider_id)
    provider_capability = info.reasoning_capability if info is not None else None
    if provider_tier is None and info is not None:
        provider_tier = ResolutionTier.PROVIDER_EXACT
    models_dev_capability, models_dev_tiers = model_reasoning_capability_tiered(
        provider_id, model_id
    )
    match = None if described else cross_provider_match(provider_id, model_id)

    provider_output = info.max_output_tokens if info is not None else None
    if provider_output is not None:
        output = _sourced(provider_output, SOURCE_PROVIDER, provider_tier)
    else:
        published, output_tier = model_output_limit_tiered(provider_id, model_id)
        if published is None:
            # Includes the case where the approximate tier matched but too few
            # of its rows published a limit to vote: unknown, not a guess.
            output = _sourced(None, SOURCE_UNKNOWN)
        elif output_tier is not None and output_tier.is_approximate:
            # The number the user most needs warned about: a mode across rows
            # that merely share a name. The counts reported are the ones the
            # vote was actually taken over, never the raw match count.
            output = _sourced(
                published,
                SOURCE_APPROXIMATE,
                output_tier,
                match_count=None if match is None else match.match_count,
                agreement=None if match is None else match.output_agreement,
                reporters=None if match is None else match.output_reporters,
            )
        else:
            output = _sourced(published, SOURCE_MODELS_DEV, output_tier)

    layers: dict[str, ModelReasoningCapability | None] = {
        SOURCE_PROVIDER: provider_capability,
        SOURCE_MODELS_DEV: models_dev_capability,
        SOURCE_APPROXIMATE: models_dev_capability,
    }
    reasoning: dict[str, Any] = {}
    for name in REASONING_FIELDS:
        source, tier = _reasoning_source(
            name,
            provider_capability,
            models_dev_capability,
            models_dev_tiers,
            provider_tier=provider_tier,
        )
        layer = layers.get(source)
        value = None if layer is None else _reasoning_field_value(layer, name)
        extra: dict[str, Any] = {}
        if source == SOURCE_APPROXIMATE and match is not None:
            extra = {"match_count": match.match_count}
        reasoning[name] = _sourced(value, source, tier, **extra)

    context_length = info.context_length if info is not None else None
    supports_vision = info.supports_vision if info is not None else None
    supported = info.supported_parameters if info is not None else None
    defaults = info.default_parameters if info is not None else None
    return {
        "max_output_tokens": output,
        "context_length": _sourced(
            context_length,
            SOURCE_UNKNOWN if context_length is None else SOURCE_PROVIDER_OR_MODELS_DEV,
            note=CONTEXT_LENGTH_SOURCE_NOTE,
        ),
        "supports_vision": _sourced(
            supports_vision,
            SOURCE_UNKNOWN
            if supports_vision is None
            else SOURCE_PROVIDER_OR_MODELS_DEV,
        ),
        # Only a gateway publishes these two; models.dev has no equivalent, so
        # there is no second tier they could have come from.
        "supported_parameters": _sourced(
            None if supported is None else sorted(supported),
            SOURCE_UNKNOWN if supported is None else SOURCE_PROVIDER,
        ),
        "default_parameters": _sourced(
            None if defaults is None else [list(pair) for pair in defaults],
            SOURCE_UNKNOWN if defaults is None else SOURCE_PROVIDER,
        ),
        "reasoning": reasoning,
        "reasoning_dialect": dialect_payload(dialect),
    }


def exact_pattern(model_ref: str) -> str:
    """The pattern that matches exactly one model ref and nothing else."""

    return normalize_override_key(model_ref)


def apply_visibility_toggle(
    visibility: ModelVisibility, model_ref: str, *, visible: bool
) -> ModelVisibility:
    """Add or remove the exact-match pattern that shows or hides one model.

    An explicit tick is deliberately stored as a one-model glob rather than in
    a list of its own: a second store would have to be merged with the globs at
    every read, and the two would disagree the first time somebody edited one.

    A glob the user wrote can still win afterwards -- ticking a model back on
    while ``*:free`` sits in the deny list changes nothing visible -- so the
    caller reports the recomputed visibility rather than assuming the toggle
    took effect.
    """

    pattern = exact_pattern(model_ref)
    if not pattern:
        return visibility
    deny = tuple(entry for entry in visibility.deny if entry != pattern)
    allow = tuple(entry for entry in visibility.allow if entry != pattern)
    if not visible:
        return ModelVisibility(allow=allow, deny=(*deny, pattern))
    # An opt-in allow list hides everything it does not name, so showing a
    # model means naming it there too.
    if allow and not ModelVisibility(allow=allow, deny=deny).is_visible(model_ref):
        allow = (*allow, pattern)
    return ModelVisibility(allow=allow, deny=deny)


def render_patterns(patterns: Iterable[str]) -> str:
    """Render a pattern tuple back into its comma-separated env form."""

    return MODEL_PATTERN_SEPARATOR.join(patterns)


def visibility_payload(
    visibility: ModelVisibility,
    model_refs: Iterable[str],
    configured: Iterable[ConfiguredChatModelRef],
) -> dict[str, Any]:
    """The visibility section: the two lists, and what they currently hide."""

    refs = tuple(model_refs)
    hidden = [ref for ref in refs if not visibility.is_visible(ref)]
    # A configured route that is hidden is not broken -- it still serves -- but
    # it is the one case where the page would otherwise look like a model
    # vanished from the catalogue.
    hidden_routes = [
        {"model_ref": ref.model_ref, "sources": list(ref.sources)}
        for ref in configured
        if not visibility.is_visible(ref.model_ref)
    ]
    return {
        "allow": list(visibility.allow),
        "deny": list(visibility.deny),
        "allow_raw": render_patterns(visibility.allow),
        "deny_raw": render_patterns(visibility.deny),
        "hides_anything": visibility.hides_anything,
        "hidden_count": len(hidden),
        "visible_count": len(refs) - len(hidden),
        "hidden_model_refs": hidden,
        "hidden_route_refs": hidden_routes,
        "hide_only_notice": HIDE_ONLY_NOTICE,
    }


def effective_parameters(
    overrides: ModelParameterOverrides, provider_id: str, model_ref: str
) -> list[dict[str, Any]]:
    """What each editable parameter resolves to for one model.

    ``resolve`` merges per parameter, so the answer is not "the model row if it
    exists": a model row can force one parameter off while inheriting the rest
    of the provider row.
    """

    provider_row = overrides.providers.get(normalize_override_key(provider_id), {})
    model_row = overrides.models.get(normalize_override_key(model_ref), {})
    resolved = overrides.resolve(provider_id, model_ref)
    rows: list[dict[str, Any]] = []
    for name in sorted(ALLOWED_OVERRIDE_PARAMETERS):
        if name in resolved:
            action = "unset" if resolved[name] is None else "force"
        else:
            action = "inherit"
        rows.append(
            {
                "name": name,
                "action": action,
                "value": resolved.get(name),
                "from": (
                    MODEL_SCOPE
                    if name in model_row
                    else (PROVIDER_SCOPE if name in provider_row else None)
                ),
            }
        )
    return rows


def _row_state(row: Mapping[str, Any]) -> dict[str, Any]:
    """Render one override row so absent, null and value stay distinguishable.

    A row is only ever built from keys that are present, so an absent key is
    absent here too; the two states that survive JSON are spelled out rather
    than left for the reader to infer from a ``null``.
    """

    return {
        name: {"state": "unset" if row[name] is None else "value", "value": row[name]}
        for name in sorted(row)
    }


def overrides_payload(overrides: ModelParameterOverrides) -> dict[str, Any]:
    """The whole override table, plus what the editor is allowed to touch."""

    return {
        "providers": {key: _row_state(row) for key, row in overrides.providers.items()},
        "models": {key: _row_state(row) for key, row in overrides.models.items()},
        "editable_parameters": sorted(ALLOWED_OVERRIDE_PARAMETERS),
        "owned_elsewhere": dict(sorted(OWNED_ELSEWHERE_PARAMETERS.items())),
        "inherit_sentinel": INHERIT_SENTINEL,
    }


def _model_entry(
    model_ref: str,
    info: ProviderModelInfo | None,
    *,
    visibility: ModelVisibility,
    overrides: ModelParameterOverrides,
    configured_refs: frozenset[str],
    dialect_lookup: ReasoningDialectLookup | None = None,
) -> dict[str, Any]:
    provider_id = parse_provider_type(model_ref)
    model_id = parse_model_name(model_ref) if "/" in model_ref else model_ref
    model_row = overrides.models.get(normalize_override_key(model_ref), {})
    return {
        "model_ref": model_ref,
        "model_id": model_id,
        "visible": visibility.is_visible(model_ref),
        "configured": model_ref in configured_refs,
        "has_metadata": info is not None,
        "override": _row_state(model_row),
        "effective": effective_parameters(overrides, provider_id, model_ref),
        "capabilities": capability_payload(
            provider_id,
            model_id,
            info,
            dialect=(
                None
                if dialect_lookup is None
                else dialect_lookup(provider_id, model_id)
            ),
        ),
    }


def build_models_page_payload(
    model_infos: Iterable[ProviderModelInfo],
    configured: Iterable[ConfiguredChatModelRef],
    visibility: ModelVisibility,
    overrides: ModelParameterOverrides,
    dialect_lookup: ReasoningDialectLookup | None = None,
) -> dict[str, Any]:
    """Everything the Models page renders, in one request.

    Discovered models are unioned with configured route refs, and *neither* set
    is filtered by visibility: this is the one page where a hidden model has to
    stay listed, or it could never be un-hidden.
    """

    configured_list = tuple(configured)
    configured_refs = frozenset(ref.model_ref for ref in configured_list)
    by_ref: dict[str, ProviderModelInfo | None] = {}
    for info in model_infos:
        by_ref.setdefault(info.model_id, info)
    for ref in sorted(configured_refs, key=str.casefold):
        by_ref.setdefault(ref, None)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for model_ref in sorted(by_ref, key=str.casefold):
        grouped.setdefault(parse_provider_type(model_ref), []).append(
            _model_entry(
                model_ref,
                by_ref[model_ref],
                visibility=visibility,
                overrides=overrides,
                configured_refs=configured_refs,
                dialect_lookup=dialect_lookup,
            )
        )

    provider_rows = [
        {
            "provider_id": provider_id,
            "override": _row_state(
                overrides.providers.get(normalize_override_key(provider_id), {})
            ),
            "model_count": len(models),
            "hidden_count": sum(1 for model in models if not model["visible"]),
            "models": models,
        }
        for provider_id, models in sorted(grouped.items(), key=lambda item: item[0])
    ]
    return {
        "providers": provider_rows,
        "visibility": visibility_payload(visibility, tuple(by_ref), configured_list),
        "overrides": overrides_payload(overrides),
        "source_labels": dict(SOURCE_LABELS),
    }


def merged_override_row(
    existing: Mapping[str, Any], updates: Mapping[str, Any]
) -> dict[str, Any]:
    """Apply one editor submission to one override row.

    Anything outside :data:`ALLOWED_OVERRIDE_PARAMETERS` is dropped here as
    well as in the store: the allow-list is a security boundary and a value
    that reaches an upstream body must have passed it more than once.
    """

    row = dict(existing)
    for name, value in updates.items():
        if name not in ALLOWED_OVERRIDE_PARAMETERS:
            continue
        if value == INHERIT_SENTINEL:
            row.pop(name, None)
            continue
        row[name] = value
    return row


def with_override_row(
    overrides: ModelParameterOverrides,
    *,
    scope: str,
    key: str,
    updates: Mapping[str, Any],
) -> ModelParameterOverrides:
    """Return the table with one provider or model row rewritten."""

    folded = normalize_override_key(key)
    source = overrides.providers if scope == PROVIDER_SCOPE else overrides.models
    table = {name: dict(row) for name, row in source.items()}
    row = merged_override_row(table.get(folded, {}), updates)
    if row:
        table[folded] = row
    else:
        table.pop(folded, None)
    if scope == PROVIDER_SCOPE:
        return replace(overrides, providers=table)
    return replace(overrides, models=table)
