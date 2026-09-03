"""One neutral model record every harness catalogue is serialised from.

Before this module existed, each generated CLI catalogue invented its own
numbers. Codex's builder emitted ``"context_window": 200000`` and the same
four reasoning rungs for *every* model, and Pi's bundled extension emitted
``contextWindow: 128000`` and zero costs for every model, because both were
built from ``GET /v1/models`` -- whose payload carries an id, a display name
and nothing else. Neither had any way to reach the resolution ladder, so
neither could have told the truth even in principle.

:class:`CatalogueModel` is that missing input: the ladder's own answer for one
routable (provider, model), in MCC's vocabulary rather than any CLI's. The
per-CLI serialisers under ``application/catalogues`` are pure functions of a
tuple of these records, so a capability MCC knows reaches every CLI that has a
field for it, and a capability MCC does *not* know is visibly absent rather
than silently invented.

**Unknown stays unknown.** Every capability field is ``X | None`` and ``None``
means "no source stated this", which the ladder is careful to keep distinct
from a source stating the model lacks the capability. Nothing in this module
substitutes a number for a ``None``; where a CLI's schema forces a value, its
own serialiser supplies that CLI's documented default and records the
substitution. See ``application/catalogues/base.py``.

The record is built alongside :func:`my_claude_code.api.model_catalog.build_models_list_response`
from the same visibility filter, the same ref enumeration and the same
two-variant projection, so a model can never appear in ``/v1/models`` and not
in a harness catalogue, or the reverse.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace

from my_claude_code.application.model_metadata import (
    ModelDefaultParameters,
    ModelReasoningCapability,
    ProviderModelInfo,
)
from my_claude_code.application.ports import RequestRuntimePort
from my_claude_code.application.tier_chains import resolve_tier_chain
from my_claude_code.config.harness_tiers import EMPTY_HARNESS_TIERS, HarnessTiers
from my_claude_code.config.model_refs import (
    configured_chat_model_refs,
    parse_model_name,
    parse_provider_type,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.gateway_model_ids import (
    gateway_model_id,
    no_thinking_gateway_model_id,
)
from my_claude_code.core.model_visibility import ModelVisibility
from my_claude_code.core.tier_refs import TIER_LABELS, TIER_ORDER, tier_ref

#: Request parameters whose presence in a gateway's published
#: ``supported_parameters`` list means the model accepts tool calls. Derived,
#: never assumed: a gateway that publishes no list at all leaves
#: ``supports_tool_calls`` ``None``, because "did not say" is not "cannot".
TOOL_CALL_PARAMETERS = frozenset({"tools", "tool_choice"})


@dataclass(frozen=True, slots=True)
class CatalogueFieldProvenance:
    """Which rung of the resolution ladder stated one capability field.

    The same provenance the admin Models page renders, carried into the
    catalogue layer so a dashboard card and a generated file can agree about
    whether a number is the provider's own answer or a cross-provider vote.
    """

    source: str
    source_label: str
    tier: int | None = None
    tier_label: str | None = None
    approximate: bool = False


#: ``(provider_id, model_id, info) -> {field: provenance}``. Injected rather
#: than imported: the ladder's tier bookkeeping lives behind the admin
#: capability inspector, which ``application`` may not import.
type CapabilityProvenanceLookup = Callable[
    [str, str, ProviderModelInfo | None], Mapping[str, CatalogueFieldProvenance]
]


@dataclass(frozen=True, slots=True)
class CatalogueModel:
    """One routable model, as MCC's ladder resolves it, for any CLI catalogue."""

    # -- identity ---------------------------------------------------------
    #: The id ``/v1/models`` advertises, e.g. ``anthropic/openrouter/gpt-5``.
    gateway_id: str
    #: ``<provider>/<model>`` with no gateway prefix.
    provider_model_ref: str
    display_name: str
    #: True for the ``claude-3-freecc-no-thinking/`` variant, which exists to
    #: trip Claude Code's client-side "claude-3- means no thinking" heuristic.
    force_no_thinking: bool = False
    #: True for the ref named by ``MODEL`` -- the route MCC itself starts on.
    #: A CLI that must pin one model to open a session (Cline, Crush, Goose)
    #: should pin this one: it is the route the operator chose and the one
    #: MCC's own default chain is built around, where "the first entry" is an
    #: enumeration artefact that can just as easily be a dead free tier.
    is_primary_route: bool = False

    # -- capabilities: every one None means "the ladder does not know" -----
    context_length: int | None = None
    max_output_tokens: int | None = None
    supports_vision: bool | None = None
    supports_tool_calls: bool | None = None
    reasoning: ModelReasoningCapability | None = None
    input_price: float | None = None
    output_price: float | None = None
    # The two cached rates, which no provider ``/models`` payload publishes:
    # they reach the record from models.dev alone, down the same ladder. A CLI
    # with a cache-rate field used to be told nothing for every model.
    cache_read_price: float | None = None
    cache_write_price: float | None = None
    supported_parameters: frozenset[str] | None = None
    default_parameters: ModelDefaultParameters | None = None

    # -- provenance, for the report and the dashboard ----------------------
    field_provenance: Mapping[str, CatalogueFieldProvenance] = field(
        default_factory=dict
    )

    @property
    def provider_id(self) -> str:
        """Return the routing provider id."""

        return parse_provider_type(self.provider_model_ref)

    @property
    def provider_model_id(self) -> str:
        """Return the provider-native model id."""

        return parse_model_name(self.provider_model_ref)


def derive_supports_tool_calls(
    supported_parameters: frozenset[str] | None,
) -> bool | None:
    """Return tool-call support as the gateway's parameter list states it.

    ``None`` when no list was published: a gateway that says nothing has not
    said "no". Only a published list can produce ``False``.
    """

    if supported_parameters is None:
        return None
    return bool(supported_parameters & TOOL_CALL_PARAMETERS)


def build_catalogue_models(
    settings: Settings,
    runtime: RequestRuntimePort,
    provenance: CapabilityProvenanceLookup | None = None,
    *,
    harness_id: str | None = None,
    harness_tiers: HarnessTiers | None = None,
) -> tuple[CatalogueModel, ...]:
    """Resolve every visible routable model into the neutral catalogue record.

    Mirrors ``build_models_list_response`` exactly -- same visibility filter,
    same configured-then-discovered enumeration, same normal/no-thinking
    projection -- minus the eight fixed Claude protocol aliases, which are
    protocol names rather than routable refs and carry no capabilities to
    publish.

    ``harness_id`` names the coding agent this document is being built for, so
    the five tier aliases at the head of the list reflect *that* agent's
    overrides. It is optional because ``/admin/api/catalogue-models`` and the
    tests build one neutral list for nobody in particular, which is exactly the
    global chain.
    """

    visibility = ModelVisibility.from_raw(
        settings.model_visibility_allow, settings.model_visibility_deny
    )
    primary_ref = settings.model.strip()
    infos_by_ref: dict[str, ProviderModelInfo] = {}
    for info in runtime.cached_prefixed_model_infos():
        infos_by_ref.setdefault(info.model_id, info)

    models: list[CatalogueModel] = []
    seen: set[str] = set()

    for ref in configured_chat_model_refs(settings):
        if not visibility.is_visible(ref.model_ref):
            continue
        _append_variants(
            models,
            seen,
            ref.model_ref,
            primary_ref=primary_ref,
            runtime=runtime,
            info=infos_by_ref.get(ref.model_ref),
            provenance=provenance,
        )

    for info in runtime.cached_prefixed_model_infos():
        if not visibility.is_visible(info.model_id):
            continue
        _append_variants(
            models,
            seen,
            info.model_id,
            primary_ref=primary_ref,
            runtime=runtime,
            info=info,
            provenance=provenance,
        )

    aliases = _tier_alias_models(
        settings,
        harness_tiers if harness_tiers is not None else EMPTY_HARNESS_TIERS,
        harness_id,
        models,
    )
    if not aliases:
        return tuple(models)
    # Aliases first. Ordering is the only discoverability lever this layer has:
    # it puts the tiers at the top of OpenCode's and Qwen's pickers and gives
    # them priority 0-4 in Codex. And Best carries ``is_primary_route``, so
    # ``select_starting_index`` -- unchanged -- seeds Cline's session model and
    # Crush's ``models.large`` on ``mcc/best``, which means those two open on
    # the route rather than on a ref that is frozen into their config file.
    demoted = [
        replace(model, is_primary_route=False) if model.is_primary_route else model
        for model in models
    ]
    return (*aliases, *demoted)


def _tier_alias_models(
    settings: Settings,
    harness_tiers: HarnessTiers,
    harness_id: str | None,
    models: list[CatalogueModel],
) -> tuple[CatalogueModel, ...]:
    """Build the five tier records, each a copy of the model it points at.

    **The primary's metadata, verbatim.** Every capability field, every
    ``field_provenance`` entry and every ``None`` travels with it untouched; only
    ``gateway_id`` and ``display_name`` are replaced. That is what lets no
    serialiser know a tier exists, and it is why the alternative -- a MIN across
    the chain -- is rejected: a minimum over two providers' answers is a number
    no source ever stated, which is precisely the invented number this module's
    docstring forbids. It would also silently downgrade the common case, since a
    fallback is only ever reached on a failure, and it would flip whenever the
    operator reordered a rail.

    **Both wire spellings, from one record.** ``provider_model_ref`` is the
    alias itself (``mcc/best``) and ``gateway_id`` its gateway form
    (``anthropic/mcc/best``), because the fleet is genuinely split: Codex,
    Command Code, OpenCode, Kilo, Pi and Kimi write the bare ref, while Cline,
    Crush, Droid, Gemini CLI, Qwen and Aider write the gateway id -- and the
    router accepts both. Carrying the *primary's* ref here instead would collide
    with the primary's own entry in every bare-ref serialiser, which dedupes by
    that id, and the alias would silently vanish from six of the thirteen
    pickers. ``mcc/best`` still splits into two non-empty segments, so
    ``parse_model_name`` never sees a slashless id and Pi's bundled extension
    -- which requires at least two segments after the gateway prefix -- accepts
    ``anthropic/mcc/best`` unchanged.

    A tier whose chain resolves to a model this catalogue does not list gets no
    entry: an alias pointing at something absent from the same document is a
    picker entry that cannot be selected.
    """

    if not settings.harness_tier_aliases:
        return ()
    by_ref: dict[str, CatalogueModel] = {}
    for model in models:
        if not model.force_no_thinking:
            by_ref.setdefault(model.provider_model_ref, model)
    aliases: list[CatalogueModel] = []
    for tier in TIER_ORDER:
        chain = resolve_tier_chain(settings, harness_tiers, harness_id, tier)
        primary_ref = chain.primary
        if primary_ref is None:
            continue
        record = by_ref.get(primary_ref)
        if record is None:
            continue
        aliases.append(
            replace(
                record,
                gateway_id=gateway_model_id(tier_ref(tier)),
                provider_model_ref=tier_ref(tier),
                display_name=f"{TIER_LABELS[tier]} ({primary_ref})",
                force_no_thinking=False,
                is_primary_route=tier is TIER_ORDER[0],
            )
        )
    return tuple(aliases)


def _append_variants(
    models: list[CatalogueModel],
    seen: set[str],
    provider_model_ref: str,
    *,
    primary_ref: str,
    runtime: RequestRuntimePort,
    info: ProviderModelInfo | None,
    provenance: CapabilityProvenanceLookup | None,
) -> None:
    provider_id = parse_provider_type(provider_model_ref)
    model_id = parse_model_name(provider_model_ref)
    supports_thinking = runtime.cached_model_supports_thinking(provider_id, model_id)

    resolved = _resolve(
        provider_model_ref,
        provider_id=provider_id,
        model_id=model_id,
        runtime=runtime,
        info=info,
        provenance=provenance,
    )

    if supports_thinking is not False:
        _append_unique(
            models,
            seen,
            _variant(
                resolved,
                gateway_id=gateway_model_id(provider_model_ref),
                display_name=provider_model_ref,
                force_no_thinking=False,
                is_primary_route=provider_model_ref == primary_ref,
            ),
        )
    _append_unique(
        models,
        seen,
        _variant(
            resolved,
            gateway_id=no_thinking_gateway_model_id(provider_model_ref),
            display_name=f"{provider_model_ref} (no thinking)",
            force_no_thinking=True,
            is_primary_route=provider_model_ref == primary_ref,
        ),
    )


def _append_unique(
    models: list[CatalogueModel], seen: set[str], model: CatalogueModel
) -> None:
    if model.gateway_id in seen:
        return
    seen.add(model.gateway_id)
    models.append(model)


def _variant(
    resolved: CatalogueModel,
    *,
    gateway_id: str,
    display_name: str,
    force_no_thinking: bool,
    is_primary_route: bool,
) -> CatalogueModel:
    return CatalogueModel(
        gateway_id=gateway_id,
        provider_model_ref=resolved.provider_model_ref,
        display_name=display_name,
        force_no_thinking=force_no_thinking,
        is_primary_route=is_primary_route,
        context_length=resolved.context_length,
        max_output_tokens=resolved.max_output_tokens,
        supports_vision=resolved.supports_vision,
        supports_tool_calls=resolved.supports_tool_calls,
        # The no-thinking variant exists precisely to run without reasoning,
        # so it carries no reasoning capability to advertise. That is a
        # statement about the variant, not an unknown about the model.
        reasoning=None if force_no_thinking else resolved.reasoning,
        input_price=resolved.input_price,
        output_price=resolved.output_price,
        cache_read_price=resolved.cache_read_price,
        cache_write_price=resolved.cache_write_price,
        supported_parameters=resolved.supported_parameters,
        default_parameters=resolved.default_parameters,
        field_provenance=resolved.field_provenance,
    )


def _first_stated[T](cached: T | None, resolved: T | None) -> T | None:
    """The routed deployment's own record first, the ladder only after it.

    Stated once here rather than at four call sites, and stated the same way
    ``api/model_admin._laddered`` states it for the Models page, because these
    are the two surfaces that must never answer the same question differently.
    ``False`` and ``0.0`` are stated answers and are kept; only ``None`` --
    nobody said -- defers.
    """

    return resolved if cached is None else cached


def _resolve(
    provider_model_ref: str,
    *,
    provider_id: str,
    model_id: str,
    runtime: RequestRuntimePort,
    info: ProviderModelInfo | None,
    provenance: CapabilityProvenanceLookup | None,
) -> CatalogueModel:
    reasoning: ModelReasoningCapability | None = runtime.model_reasoning_capability(
        provider_id, model_id
    )
    supported = info.supported_parameters if info is not None else None
    # Every field below walks the same ten rungs the output limit already
    # walked, through the same runtime port, so a catalogue and the Models
    # page cannot answer the same question differently. ``derive_supports_
    # tool_calls`` still speaks first and ``None`` still means "nobody said":
    # only a published statement, on any rung, can produce ``False``.
    tool_calls = derive_supports_tool_calls(supported)
    if tool_calls is None:
        tool_calls = runtime.model_tool_call_tiered(provider_id, model_id)[0]
    prices = runtime.model_prices_tiered(provider_id, model_id)
    return CatalogueModel(
        gateway_id=gateway_model_id(provider_model_ref),
        provider_model_ref=provider_model_ref,
        display_name=provider_model_ref,
        context_length=_first_stated(
            None if info is None else info.context_length,
            runtime.model_context_length_tiered(provider_id, model_id)[0],
        ),
        max_output_tokens=runtime.model_output_limit(provider_id, model_id),
        supports_vision=_first_stated(
            None if info is None else info.supports_vision,
            runtime.model_vision_tiered(provider_id, model_id)[0],
        ),
        supports_tool_calls=tool_calls,
        reasoning=reasoning,
        input_price=_first_stated(
            None if info is None else info.input_price, prices["input_price"][0]
        ),
        output_price=_first_stated(
            None if info is None else info.output_price, prices["output_price"][0]
        ),
        # No provider row carries a cache rate, so these two have no first
        # answer to prefer.
        cache_read_price=prices["cache_read_price"][0],
        cache_write_price=prices["cache_write_price"][0],
        supported_parameters=supported,
        default_parameters=info.default_parameters if info is not None else None,
        field_provenance=(
            {} if provenance is None else dict(provenance(provider_id, model_id, info))
        ),
    )
