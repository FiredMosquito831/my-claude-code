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
from dataclasses import dataclass, field

from my_claude_code.application.model_metadata import (
    ModelDefaultParameters,
    ModelReasoningCapability,
    ProviderModelInfo,
)
from my_claude_code.application.ports import RequestRuntimePort
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

    # -- capabilities: every one None means "the ladder does not know" -----
    context_length: int | None = None
    max_output_tokens: int | None = None
    supports_vision: bool | None = None
    supports_tool_calls: bool | None = None
    reasoning: ModelReasoningCapability | None = None
    input_price: float | None = None
    output_price: float | None = None
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
) -> tuple[CatalogueModel, ...]:
    """Resolve every visible routable model into the neutral catalogue record.

    Mirrors ``build_models_list_response`` exactly -- same visibility filter,
    same configured-then-discovered enumeration, same normal/no-thinking
    projection -- minus the eight fixed Claude protocol aliases, which are
    protocol names rather than routable refs and carry no capabilities to
    publish.
    """

    visibility = ModelVisibility.from_raw(
        settings.model_visibility_allow, settings.model_visibility_deny
    )
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
            runtime=runtime,
            info=info,
            provenance=provenance,
        )

    return tuple(models)


def _append_variants(
    models: list[CatalogueModel],
    seen: set[str],
    provider_model_ref: str,
    *,
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
) -> CatalogueModel:
    return CatalogueModel(
        gateway_id=gateway_id,
        provider_model_ref=resolved.provider_model_ref,
        display_name=display_name,
        force_no_thinking=force_no_thinking,
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
        supported_parameters=resolved.supported_parameters,
        default_parameters=resolved.default_parameters,
        field_provenance=resolved.field_provenance,
    )


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
    return CatalogueModel(
        gateway_id=gateway_model_id(provider_model_ref),
        provider_model_ref=provider_model_ref,
        display_name=provider_model_ref,
        context_length=runtime.model_context_length(provider_id, model_id),
        max_output_tokens=runtime.model_output_limit(provider_id, model_id),
        supports_vision=runtime.cached_model_supports_vision(provider_id, model_id),
        supports_tool_calls=derive_supports_tool_calls(supported),
        reasoning=reasoning,
        input_price=info.input_price if info is not None else None,
        output_price=info.output_price if info is not None else None,
        supported_parameters=supported,
        default_parameters=info.default_parameters if info is not None else None,
        field_provenance=(
            {} if provenance is None else dict(provenance(provider_id, model_id, info))
        ),
    )
