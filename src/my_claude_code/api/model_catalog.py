"""Model-list response construction for Claude-compatible clients."""

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel

from my_claude_code.application.catalogue_model import CatalogueModel
from my_claude_code.application.model_kinds import chat_listing_filter
from my_claude_code.application.ports import RequestRuntimePort
from my_claude_code.application.tier_chains import global_tier_chain
from my_claude_code.config.model_refs import configured_chat_model_refs
from my_claude_code.config.settings import Settings
from my_claude_code.core.gateway_model_ids import (
    gateway_model_id,
    no_thinking_gateway_model_id,
)
from my_claude_code.core.model_visibility import ModelVisibility
from my_claude_code.core.tier_refs import (
    TIER_FAMILY_TIERS,
    TIER_LABELS,
    TIER_ORDER,
    tier_ref,
)
from my_claude_code.core.version import NATIVE_DISTRIBUTION

DISCOVERED_MODEL_CREATED_AT = "1970-01-01T00:00:00Z"


def settings_model_visibility(settings: Settings) -> ModelVisibility:
    """Build the shared hide-only model filter from configuration.

    The two env values are raw text; every presentation boundary that lists
    models goes through this so the pickers and `/v1/models` cannot drift
    apart on what "visible" means.
    """

    return ModelVisibility.from_raw(
        settings.model_visibility_allow, settings.model_visibility_deny
    )


class ModelReasoningResponse(BaseModel):
    """What a model is known to support for reasoning control (8.21.0).

    Every field is ``None`` when no rung of the ladder stated an opinion, which
    is deliberately distinct from a stated ``False``: "unknown" is not "cannot"
    (IV.16). The names are ``_model_payload``'s -- the shape every generated
    harness document is serialised from -- so the wire and the catalogues can
    never spell the same fact two ways.
    """

    can_reason: bool | None = None
    supports_effort_control: bool | None = None
    supports_toggle_control: bool | None = None
    supports_budget_control: bool | None = None
    #: Sorted rung names (``low``, ``medium``, ``high``, ``xhigh``, ``max``).
    #: ``None`` is unknown; an empty list means a vocabulary was published with
    #: no usable value, which is a different answer.
    supported_efforts: list[str] | None = None
    mandatory: bool | None = None
    default_enabled: bool | None = None


class ModelMetadataResponse(BaseModel):
    """The resolution ladder's record for one model, on the listing (8.21.0).

    Until 8.21.0 ``GET /v1/models`` carried nine keys and no capability at all,
    while the same models' records -- resolved by the same ladder, from the same
    enumeration -- were already serialised into sixteen harness documents and
    ``GET /admin/api/catalogue-models``. This is that record, under that
    payload's field names, so an OpenAI-shaped client learns what MCC actually
    resolved instead of an id and a display name.

    **Prices are flat and per million tokens**, exactly as ``_model_payload``
    spells them. Not OpenRouter's ``pricing.prompt`` (a per-token *string*):
    same word, different unit, and a client reading both would be wrong by
    10^6.

    **Every field is ``X | None`` and ``None`` means "no source stated this".**
    A stated ``0.0`` price and an unknown one are different answers and stay
    different on the wire (IV.16).
    """

    context_length: int | None = None
    max_output_tokens: int | None = None
    supports_vision: bool | None = None
    supports_tool_calls: bool | None = None
    reasoning: ModelReasoningResponse | None = None
    input_price: float | None = None
    output_price: float | None = None
    cache_read_price: float | None = None
    cache_write_price: float | None = None
    #: The provider's own published parameter list, sorted. ``None`` when the
    #: provider publishes none -- never models.dev's, never a dialect's.
    supported_parameters: list[str] | None = None
    #: ``[name, value]`` pairs the provider pins for this model, sorted by name.
    default_parameters: list[list[str | int | float | bool]] | None = None


class ModelResponse(BaseModel):
    object: Literal["model"] = "model"
    created: int = 0
    # MCC's own package name, from the one constant that also names the
    # distribution: the product is My Claude Code and nothing it emits names the
    # retired tool owner. Until 8.0.2 this carried that owner's name -- the last
    # user-visible occurrence after 8.0.0 removed the command family -- because
    # two byte-identity contracts pin the field: the metadata-ladder equality
    # snapshot and the MCP-media-off baseline. 8.0.2 recaptured both (only these
    # cells moved, see tests/contracts/), so the field is now the brand's own
    # name and the grep gate in tests/contracts/test_no_legacy_fcc_surface.py
    # has no allow-list entry for this module.
    owned_by: str = NATIVE_DISTRIBUTION
    created_at: str
    display_name: str
    id: str
    type: Literal["model"] = "model"
    # Claude Desktop's gateway mode filters auto-discovered models down to ids
    # that are "recognizably Claude" and opts an opaque alias in when the
    # gateway states its Claude family tier here
    # (https://claude.com/docs/third-party/claude-desktop/gateway, "Models").
    # Set on the six ``mcc/*`` tiers only: every other id is either already
    # a Claude protocol name or a provider ref MCC makes no such claim about.
    # ``None`` on the rest, and serialised as ``null``, which is not a tier
    # name and so filters exactly as the absent field did.
    anthropic_family_tier: str | None = None
    is_family_default: bool | None = None
    #: The ladder's record for this model (8.21.0). Appended LAST so the nine
    #: keys above keep their positions, values and meanings for every client
    #: that reads them. ``null`` on the ten fixed Claude protocol aliases,
    #: which are names for a route rather than routable refs of their own --
    #: publishing a tier's numbers under a protocol name would be a claim that
    #: moves the moment the operator re-points the rail.
    metadata: ModelMetadataResponse | None = None


class ModelsListResponse(BaseModel):
    object: Literal["list"] = "list"
    data: list[ModelResponse]
    first_id: str | None
    has_more: bool
    last_id: str | None


SUPPORTED_CLAUDE_MODELS = [
    # Both spellings of the Mythos name. ``claude-mythos-5`` is the id
    # models.dev carries; ``claude-mythos-5.1`` is the one a working Claude
    # Desktop gateway entry names, and neither the desktop app nor Claude Code
    # checks either against a catalogue -- they are display aliases MCC
    # resolves itself, and the router matches both on the substring ``mythos``.
    # Advertised so that the name MCC tells an operator to configure is a name
    # MCC also lists.
    ModelResponse(
        id="claude-mythos-5.1",
        display_name="Claude Mythos 5.1",
        created_at="2026-06-09T00:00:00Z",
    ),
    ModelResponse(
        id="claude-mythos-5",
        display_name="Claude Mythos 5",
        created_at="2026-06-09T00:00:00Z",
    ),
    ModelResponse(
        id="claude-fable-5",
        display_name="Claude Fable 5",
        created_at="2026-06-09T00:00:00Z",
    ),
    ModelResponse(
        id="claude-opus-4-20250514",
        display_name="Claude Opus 4",
        created_at="2025-05-14T00:00:00Z",
    ),
    ModelResponse(
        id="claude-sonnet-4-20250514",
        display_name="Claude Sonnet 4",
        created_at="2025-05-14T00:00:00Z",
    ),
    ModelResponse(
        id="claude-haiku-4-20250514",
        display_name="Claude Haiku 4",
        created_at="2025-05-14T00:00:00Z",
    ),
    ModelResponse(
        id="claude-3-opus-20240229",
        display_name="Claude 3 Opus",
        created_at="2024-02-29T00:00:00Z",
    ),
    ModelResponse(
        id="claude-3-5-sonnet-20241022",
        display_name="Claude 3.5 Sonnet",
        created_at="2024-10-22T00:00:00Z",
    ),
    ModelResponse(
        id="claude-3-haiku-20240307",
        display_name="Claude 3 Haiku",
        created_at="2024-03-07T00:00:00Z",
    ),
    ModelResponse(
        id="claude-3-5-haiku-20241022",
        display_name="Claude 3.5 Haiku",
        created_at="2024-10-22T00:00:00Z",
    ),
]


def build_models_list_response(
    settings: Settings,
    runtime: RequestRuntimePort,
    *,
    records: Mapping[str, CatalogueModel] | None = None,
) -> ModelsListResponse:
    """Return configured, cached, and compatibility model ids.

    ``records`` is the resolved catalogue, indexed by the id this listing
    advertises (``application.catalogue_snapshot.records_by_gateway_id``); each
    entry's ``metadata`` is the record with the same id (8.21.0). It is a
    keyword because the enumeration below is the same enumeration the catalogue
    layer runs -- this function never resolves a capability itself, and a
    caller that has no catalogue to hand (a unit test of the listing) gets
    ``metadata: null`` on every entry, which is the honest "not resolved for
    this listing" rather than an invented number.
    """
    models: list[ModelResponse] = []
    seen: set[str] = set()
    visibility = settings_model_visibility(settings)
    # The predicate every harness catalogue applies too (7.78.2): a discovered
    # model whose stated kind is not chat -- an image, speech or video model --
    # is not an id a chat client may send. Configured refs are never filtered.
    # OpenRouter's live list is one more rung of the same kind ladder (7.84.0),
    # and LiteLLM's map two more while LiteLLM pricing is on (7.85.0).
    chat_listable = chat_listing_filter(
        settings,
        runtime.model_modalities_lookup(),
        kind_words=runtime.model_kind_words_lookup(),
        live=runtime.openrouter_live_catalogue(),
        litellm=runtime.litellm_model_catalogue(),
    )

    for ref in configured_chat_model_refs(settings):
        if not visibility.is_visible(ref.model_ref):
            continue
        supports_thinking = runtime.cached_model_supports_thinking(
            ref.provider_id, ref.model_id
        )
        _append_provider_model_variants(
            models,
            seen,
            ref.model_ref,
            supports_thinking=supports_thinking,
            records=records,
        )

    for model_info in runtime.cached_prefixed_model_infos():
        if not visibility.is_visible(model_info.model_id):
            continue
        if not chat_listable(model_info.model_id):
            continue
        _append_provider_model_variants(
            models,
            seen,
            model_info.model_id,
            supports_thinking=model_info.supports_thinking,
            records=records,
        )

    # The Claude aliases are protocol names, not provider refs: Claude Code
    # asks for `claude-opus-4-...` and MCC maps it onto whatever MODEL_OPUS
    # points at. Filtering them would not hide a model, it would break the
    # client, so the visibility lists never see them.
    for model in SUPPORTED_CLAUDE_MODELS:
        _append_unique_model(models, seen, model)

    # The six coding-agent tier aliases, on the same reasoning and therefore
    # under the same exemption. They are names for MCC's own routes, so
    # filtering them would not hide a model either -- it would remove the id an
    # agent's config file already names and break that agent's next session.
    # Both spellings, because Pi's bundled extension only accepts the gateway
    # form (it requires two segments after the ``anthropic/`` prefix) while
    # OpenCode and Codex send the bare one.
    if settings.harness_tier_aliases:
        for tier in TIER_ORDER:
            chain = global_tier_chain(settings, tier)
            ref = tier_ref(tier)
            label = (
                f"{TIER_LABELS[tier]} ({chain.primary})"
                if chain.primary
                else TIER_LABELS[tier]
            )
            family_tier, family_default = TIER_FAMILY_TIERS[tier]
            # Both spellings carry the same record, because they are the same
            # route: the catalogue stores the gateway form, so the bare id
            # resolves through ``gateway_model_id`` in ``_metadata_for``.
            _append_unique_model(
                models,
                seen,
                _discovered_model_response(
                    ref,
                    display_name=label,
                    family_tier=family_tier,
                    family_default=family_default,
                    metadata=_metadata_for(ref, records),
                ),
            )
            _append_unique_model(
                models,
                seen,
                _discovered_model_response(
                    gateway_model_id(ref),
                    display_name=label,
                    family_tier=family_tier,
                    # Both spellings are the same route, so only one of them
                    # may claim to be the family's default entry: two defaults
                    # for one tier is a picker that opens on whichever the
                    # client happened to read last.
                    family_default=False,
                    metadata=_metadata_for(gateway_model_id(ref), records),
                ),
            )

    return ModelsListResponse(
        data=models,
        first_id=models[0].id if models else None,
        has_more=False,
        last_id=models[-1].id if models else None,
    )


def _metadata_for(
    model_id: str, records: Mapping[str, CatalogueModel] | None
) -> ModelMetadataResponse | None:
    """The record this entry's id names, or ``None`` when nothing names it.

    One lookup covers every class: the catalogue's ``gateway_id`` *is* the id
    this listing advertises, for a provider ref and for its
    ``claude-3-freecc-no-thinking/`` twin alike -- the twin's own record
    already carries ``reasoning: null``, because ``_variant`` builds it that
    way. The second lookup is for the one spelling the catalogue does not
    store: ``mcc/<tier>`` is the bare form of ``anthropic/mcc/<tier>``, and the
    two must never disagree. The ten Claude protocol aliases match neither and
    keep ``metadata: null``.
    """

    if records is None:
        return None
    index = _metadata_index(records)
    record = index.get(model_id)
    if record is None:
        record = index.get(gateway_model_id(model_id))
    return record


#: The wire metadata for one snapshot's records, converted once per snapshot.
#: Building 3,908 validated pydantic models costs ~57 ms on the user's
#: catalogue -- about half the route's whole steady-state budget -- while the
#: records themselves change only when the snapshot does. The memo is keyed on
#: the identity of the mapping the snapshot hands out, so a new build can never
#: be answered with an old conversion.
_METADATA_INDEX: (
    tuple[Mapping[str, CatalogueModel], dict[str, ModelMetadataResponse]] | None
) = None


def reset_model_metadata_index() -> None:
    """Forget the memoised wire metadata. Tests only."""

    global _METADATA_INDEX
    _METADATA_INDEX = None


def _metadata_index(
    records: Mapping[str, CatalogueModel],
) -> dict[str, ModelMetadataResponse]:
    global _METADATA_INDEX
    memo = _METADATA_INDEX
    if memo is not None and memo[0] is records:
        return memo[1]
    built = {
        model_id: model_metadata_response(record)
        for model_id, record in records.items()
    }
    _METADATA_INDEX = (records, built)
    return built


def model_metadata_response(record: CatalogueModel) -> ModelMetadataResponse:
    """The wire shape of one catalogue record (8.21.0).

    A pure function of the record -- no rung is re-read, no number is derived,
    and a ``None`` stays a ``None``. ``supported_parameters`` and
    ``supported_efforts`` are frozensets and are sorted here so two builds of
    the same catalogue always spell the same bytes.
    """

    reasoning = record.reasoning
    return ModelMetadataResponse(
        context_length=record.context_length,
        max_output_tokens=record.max_output_tokens,
        supports_vision=record.supports_vision,
        supports_tool_calls=record.supports_tool_calls,
        reasoning=(
            None
            if reasoning is None
            else ModelReasoningResponse(
                can_reason=reasoning.can_reason,
                supports_effort_control=reasoning.supports_effort_control,
                supports_toggle_control=reasoning.supports_toggle_control,
                supports_budget_control=reasoning.supports_budget_control,
                supported_efforts=(
                    None
                    if reasoning.supported_efforts is None
                    else sorted(effort.value for effort in reasoning.supported_efforts)
                ),
                mandatory=reasoning.mandatory,
                default_enabled=reasoning.default_enabled,
            )
        ),
        input_price=record.input_price,
        output_price=record.output_price,
        cache_read_price=record.cache_read_price,
        cache_write_price=record.cache_write_price,
        supported_parameters=(
            None
            if record.supported_parameters is None
            else sorted(record.supported_parameters)
        ),
        default_parameters=(
            None
            if record.default_parameters is None
            else [list(pair) for pair in record.default_parameters]
        ),
    )


def _discovered_model_response(
    model_id: str,
    *,
    display_name: str,
    family_tier: str | None = None,
    family_default: bool | None = None,
    metadata: ModelMetadataResponse | None = None,
) -> ModelResponse:
    return ModelResponse(
        id=model_id,
        display_name=display_name,
        created_at=DISCOVERED_MODEL_CREATED_AT,
        anthropic_family_tier=family_tier,
        is_family_default=family_default,
        metadata=metadata,
    )


def _append_unique_model(
    models: list[ModelResponse], seen: set[str], model: ModelResponse
) -> None:
    if model.id in seen:
        return
    seen.add(model.id)
    models.append(model)


def _append_provider_model_variants(
    models: list[ModelResponse],
    seen: set[str],
    provider_model_ref: str,
    *,
    supports_thinking: bool | None = None,
    records: Mapping[str, CatalogueModel] | None = None,
) -> None:
    if supports_thinking is not False:
        model_id = gateway_model_id(provider_model_ref)
        _append_unique_model(
            models,
            seen,
            _discovered_model_response(
                model_id,
                display_name=provider_model_ref,
                metadata=_metadata_for(model_id, records),
            ),
        )
    twin_id = no_thinking_gateway_model_id(provider_model_ref)
    _append_unique_model(
        models,
        seen,
        _discovered_model_response(
            twin_id,
            display_name=f"{provider_model_ref} (no thinking)",
            # The twin's own record already carries ``reasoning: None``: the
            # catalogue builds it that way because the id exists precisely to
            # run without reasoning. Nothing is overridden here.
            metadata=_metadata_for(twin_id, records),
        ),
    )
