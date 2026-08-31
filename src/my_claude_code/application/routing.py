"""Model routing for Claude-compatible requests."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

from loguru import logger

from my_claude_code.application.errors import UnknownProviderError
from my_claude_code.config.model_refs import (
    parse_model_name,
    parse_model_ref_list,
    parse_provider_type,
)
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import (
    MessagesRequest,
    TokenCountRequest,
    request_carries_image,
)
from my_claude_code.core.gateway_model_ids import decode_gateway_model_id
from my_claude_code.core.reasoning import (
    ReasoningAdaptation,
    ReasoningAdaptationKind,
    ReasoningControl,
    ReasoningDialect,
    ReasoningPolicy,
    combine_reasoning_adaptations,
)

from .model_metadata import ModelReasoningCapability
from .output_tokens import (
    UNKNOWN_OUTPUT_TOKEN_LIMITS,
    OutputTokenLimits,
    resolve_max_output_tokens,
)
from .reasoning import resolve_reasoning_policy
from .reasoning_budget import reconcile_reasoning_budget
from .reasoning_gating import adapt_reasoning_policy

_ROUTE_SETTINGS = (
    ("fable", "model_fable", "reasoning_fable", "model_fable_fallbacks"),
    ("opus", "model_opus", "reasoning_opus", "model_opus_fallbacks"),
    ("haiku", "model_haiku", "reasoning_haiku", "model_haiku_fallbacks"),
    ("sonnet", "model_sonnet", "reasoning_sonnet", "model_sonnet_fallbacks"),
)


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    original_model: str
    provider_id: str
    provider_model: str
    provider_model_ref: str
    reasoning_preference: ReasoningPreference


@dataclass(frozen=True, slots=True)
class RoutedMessagesRequest:
    """One request bound to one resolved model, with both reasoning policies.

    ``reasoning`` is the *applied* policy -- what is actually sent to the
    provider, after per-model capability gating. Every downstream consumer
    reads it for exactly that, and that meaning must not be flipped again.
    ``requested_reasoning`` is the same policy *before* gating: what the client
    and the tier configuration asked for. The two are equal whenever the model
    could accept the request unchanged, and differ exactly when the model's
    published capability forced a clamp, a substitution, or a suppression.

    ``output_limits`` carries everything known about how many tokens this model
    can emit, resolved here because the lookups live in this layer and
    ``core/anthropic/conversion.py`` -- where the body is built -- cannot reach
    settings or the model catalogue. It is only the *inputs* to the decision:
    the budget itself needs the prompt's token count, which is not known until
    execution, so :func:`apply_output_token_budget` finishes the job there.

    ``reasoning_dialect`` is the HOST fact gating already looks up and then
    throws away: which reasoning fields this gateway parses for this model. It
    is kept because two later decisions need it and neither can re-derive it --
    whether ``ADAPTIVE`` is spellable here at all (and therefore whether this
    attempt is really a thinking attempt, see :func:`_will_reason`), and, for
    the request log, which channel the rung left through. Carried as the
    dialect rather than as a boolean because the next consumer wants the
    channel's name, not just its existence.

    ``output_widened_from`` is the client's own ``max_tokens``, recorded only
    when the reasoning widening actually raised the number that will be sent.
    ``None`` means "the client's ask is the wire value's origin", which is the
    common case and needs no row in the request log.
    """

    request: MessagesRequest
    resolved: ResolvedModel
    reasoning: ReasoningPolicy
    requested_reasoning: ReasoningPolicy
    reasoning_adaptation: ReasoningAdaptation
    output_limits: OutputTokenLimits = UNKNOWN_OUTPUT_TOKEN_LIMITS
    reasoning_dialect: ReasoningDialect | None = None
    output_widened_from: int | None = None


@dataclass(frozen=True, slots=True)
class RoutedTokenCountRequest:
    request: TokenCountRequest
    resolved: ResolvedModel


class RouteDiversion(StrEnum):
    """Why a plan does not start where the route's own model points.

    ``VISION_UNAVAILABLE`` is the exception: nothing moved, because there was
    nowhere to move to. It is recorded anyway -- an image sent to a route with
    no sighted model on it is the one case the operator most needs to see, and
    without a marker it is indistinguishable from an ordinary request.
    """

    VISION = "vision"
    VISION_UNAVAILABLE = "vision_unavailable"


@dataclass(frozen=True, slots=True)
class RoutedMessagesPlan:
    """One request and the ordered alternates to try if it cannot be served.

    ``attempts[0]`` is what the route resolves to today; everything after it is
    a configured fallback. A plan with a single attempt behaves exactly like the
    unchained routing it replaces.

    ``diverted_from`` and ``diversion`` record a policy that replaced the head
    of the chain -- today only the vision adapter. Without them a diverted
    request is indistinguishable in the log from a route that simply points at
    that model, so nobody can tell the adapter is doing anything.

    ``probe_candidates`` maps a provider id to one model the operator has
    already configured on it. It is the executor's answer to "is this 429
    about the model or about the key?" when the chain holds no second model
    on the rate-limited provider: one 16-token question to a model already
    paid for settles it. Resolved here, where routes live, so the executor
    needs no router of its own -- and a provider with no configured route
    simply gets no entry, which is how "skip the probe" is expressed.
    """

    attempts: tuple[RoutedMessagesRequest, ...]
    diverted_from: str | None = None
    diversion: RouteDiversion | None = None
    probe_candidates: Mapping[str, ResolvedModel] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.attempts:
            raise ValueError("A routed messages plan needs at least one attempt.")

    @property
    def primary(self) -> RoutedMessagesRequest:
        return self.attempts[0]

    @property
    def has_fallbacks(self) -> bool:
        return len(self.attempts) > 1

    def model_refs(self) -> tuple[str, ...]:
        return tuple(attempt.resolved.provider_model_ref for attempt in self.attempts)


VisionCapabilityLookup = Callable[[str, str], bool | None]
# What one resolved model is known to accept for reasoning, and how many output
# tokens it can produce. Both are injected rather than looked up here so the
# application layer stays free of disk IO and provider imports, exactly as
# ``vision_lookup`` already is. Both default to absent, and an absent lookup
# means every model is unknown -- which by design changes nothing at all.
ReasoningCapabilityLookup = Callable[[str, str], ModelReasoningCapability | None]
ReasoningDialectLookup = Callable[[str, str], ReasoningDialect | None]
OutputLimitLookup = Callable[[str, str], int | None]
ContextLengthLookup = Callable[[str, str], int | None]


def apply_output_token_budget(
    routed: RoutedMessagesRequest, input_tokens: int
) -> RoutedMessagesRequest:
    """Bind one routed request's ``max_tokens`` to what its model can emit.

    Called once the prompt has been counted, because the context-headroom half
    of the decision needs that count. The resolved value is written onto the
    routed request itself, which is how it reaches
    ``build_base_request_body`` -- and every other provider dialect -- without
    ``core`` having to look anything up.

    An attempt that is going to think starts from the model's own published
    limit rather than from the client's ask, because the thinking and the
    answer are spent from this one number (WORKING-NOTES 54). The clamps are
    unchanged and still run in the same order afterwards, so the ceiling and
    the context headroom have the last word either way.
    """

    requested = routed.request.max_tokens
    resolved = resolve_max_output_tokens(
        requested,
        limits=routed.output_limits,
        input_tokens=input_tokens,
        model_ref=routed.resolved.provider_model_ref,
        for_reasoning=_will_reason(routed),
    )
    if resolved == requested:
        return routed
    return replace(
        routed,
        request=routed.request.model_copy(update={"max_tokens": resolved}),
        # Derived from the FINAL value, after the ceiling and the headroom have
        # had their say: a widening one of them immediately took back is not a
        # widening anybody should be told about. A ``requested is None`` case is
        # not a widening either -- the model's full limit for a client that
        # named nothing is what this function already did before any of this.
        output_widened_from=(
            requested
            if requested is not None and resolved is not None and resolved > requested
            else None
        ),
    )


def _will_reason(routed: RoutedMessagesRequest) -> bool:
    """Whether this attempt is going to ask the provider to think.

    ``requests_reasoning`` (core/reasoning.py) answers ``False`` for ADAPTIVE
    on purpose -- it names no effort and no budget, so a generic encoder sends
    nothing, and widening an allowance for an instruction that never leaves
    would be inventing a limit in the other direction. On the one host that
    *does* have an adaptive channel (``ReasoningDialect.adaptive``), ADAPTIVE
    really is a thinking request, so the dialect is consulted rather than the
    policy alone.
    """

    policy = routed.reasoning
    if policy.requests_reasoning:
        return True
    return (
        policy.control is ReasoningControl.ADAPTIVE
        and routed.reasoning_dialect is not None
        and routed.reasoning_dialect.adaptive
    )


def apply_reasoning_budget(routed: RoutedMessagesRequest) -> RoutedMessagesRequest:
    """Fit this request's thinking budget inside the answer it has to leave.

    Runs *after* :func:`apply_output_token_budget`, on purpose and in that
    order: the reasoning budget has to be reconciled against the very number
    that becomes ``max_tokens``, and that number is not final until the output
    budget has been resolved against the prompt's token count. Reconciling at
    gating time instead would use the client's raw ask, which the output budget
    may then lower -- or, since 6.8.0, raise.

    Nothing here knows about that raise and nothing here needs to. The rung's
    ratio is priced against ``routed.request.max_tokens``, so a widened
    allowance grows the thinking budget and the answer reserve
    (``min(answer_floor_max, output // 2)``) together, by arithmetic rather
    than by a second rule. On an effort-only host the rung itself is untouched
    and only the answer stops starving, which is the whole point.
    """

    limits = routed.output_limits
    effective_output = routed.request.max_tokens
    if effective_output is None:
        effective_output = limits.limit
    reasoning, adaptation = reconcile_reasoning_budget(
        routed.reasoning,
        effective_output=effective_output,
        floor_max=limits.answer_floor_max,
        model_ref=routed.resolved.provider_model_ref,
    )
    if reasoning == routed.reasoning:
        return routed
    return replace(
        routed,
        reasoning=reasoning,
        reasoning_adaptation=combine_reasoning_adaptations(
            routed.reasoning_adaptation, adaptation
        ),
    )


class ModelRouter:
    """Resolve incoming Claude model names to configured provider/model pairs."""

    def __init__(
        self,
        settings: Settings,
        *,
        vision_lookup: VisionCapabilityLookup | None = None,
        reasoning_capability_lookup: ReasoningCapabilityLookup | None = None,
        reasoning_dialect_lookup: ReasoningDialectLookup | None = None,
        output_limit_lookup: OutputLimitLookup | None = None,
        context_length_lookup: ContextLengthLookup | None = None,
    ):
        self._settings = settings
        self._vision_lookup = vision_lookup
        self._reasoning_capability_lookup = reasoning_capability_lookup
        self._reasoning_dialect_lookup = reasoning_dialect_lookup
        self._output_limit_lookup = output_limit_lookup
        self._context_length_lookup = context_length_lookup
        # Resolved once per router: the routes it reads are fixed for the
        # life of one settings generation, and a request should not re-walk
        # five chains to answer a question it may never ask.
        self._probe_candidate_cache: dict[str, ResolvedModel] | None = None

    def resolve(self, claude_model_name: str) -> ResolvedModel:
        (
            direct_provider_id,
            direct_provider_model,
            force_reasoning_off,
        ) = self._direct_provider_model(claude_model_name)
        if direct_provider_id is not None and direct_provider_model is not None:
            reasoning_preference = (
                ReasoningPreference.OFF
                if force_reasoning_off
                else self._settings.reasoning_policy
            )
            logger.debug(
                "MODEL DIRECT: '{}' -> provider='{}' model='{}' reasoning={}",
                claude_model_name,
                direct_provider_id,
                direct_provider_model,
                reasoning_preference.value,
            )
            return ResolvedModel(
                original_model=claude_model_name,
                provider_id=direct_provider_id,
                provider_model=direct_provider_model,
                provider_model_ref=claude_model_name,
                reasoning_preference=reasoning_preference,
            )

        provider_model_ref = self._resolve_model_ref(claude_model_name)
        reasoning_preference = self._resolve_reasoning_preference(claude_model_name)
        provider_id = parse_provider_type(provider_model_ref)
        self._validate_provider_id(provider_id)
        provider_model = parse_model_name(provider_model_ref)
        if provider_model != claude_model_name:
            logger.debug(
                "MODEL MAPPING: '{}' -> '{}'", claude_model_name, provider_model
            )
        return ResolvedModel(
            original_model=claude_model_name,
            provider_id=provider_id,
            provider_model=provider_model,
            provider_model_ref=provider_model_ref,
            reasoning_preference=reasoning_preference,
        )

    @staticmethod
    def _validate_provider_id(provider_id: str) -> None:
        descriptors = get_provider_registry().all_descriptors()
        if provider_id not in descriptors:
            raise UnknownProviderError.for_provider(provider_id, descriptors)

    def _direct_provider_model(
        self, model_name: str
    ) -> tuple[str | None, str | None, bool]:
        supported_ids = get_provider_registry().supported_ids()
        decoded = decode_gateway_model_id(model_name)
        if decoded is not None:
            if decoded.provider_id not in supported_ids:
                return None, None, False
            return (
                decoded.provider_id,
                decoded.provider_model,
                decoded.force_reasoning_off,
            )

        provider_id, separator, provider_model = model_name.partition("/")
        if not separator:
            return None, None, False
        if provider_id not in supported_ids:
            return None, None, False
        if not provider_model:
            return None, None, False
        return provider_id, provider_model, False

    def resolve_chain(self, claude_model_name: str) -> tuple[ResolvedModel, ...]:
        """Resolve a Claude model name to its ordered primary/fallback chain.

        A client that names a provider and model directly gets exactly what it
        asked for: overriding an explicit choice with a configured fallback
        would silently answer a different question than the one asked.
        """

        primary = self.resolve(claude_model_name)
        direct_provider_id, _model, _off = self._direct_provider_model(
            claude_model_name
        )
        if direct_provider_id is not None:
            return (primary,)

        reasoning_preference = self._resolve_reasoning_preference(claude_model_name)
        resolved = [primary]
        seen = {primary.provider_model_ref}
        for model_ref in self._fallback_model_refs(claude_model_name):
            if model_ref in seen:
                continue
            seen.add(model_ref)
            provider_id = parse_provider_type(model_ref)
            try:
                self._validate_provider_id(provider_id)
            except UnknownProviderError:
                # A chain is a resilience feature: one unusable entry must not
                # take down a route whose primary is perfectly healthy.
                logger.warning(
                    "MODEL FALLBACK SKIPPED: '{}' names unknown provider '{}'",
                    model_ref,
                    provider_id,
                )
                continue
            resolved.append(
                ResolvedModel(
                    original_model=claude_model_name,
                    provider_id=provider_id,
                    provider_model=parse_model_name(model_ref),
                    provider_model_ref=model_ref,
                    reasoning_preference=reasoning_preference,
                )
            )
        return tuple(resolved)

    def _fallback_model_refs(self, claude_model_name: str) -> tuple[str, ...]:
        """Return the fallback chain that sits next to this route's primary."""

        route = self._matched_route(claude_model_name)
        if route is not None and isinstance(getattr(self._settings, route[1]), str):
            return parse_model_ref_list(getattr(self._settings, route[3]))
        return parse_model_ref_list(self._settings.model_fallbacks)

    def _resolve_model_ref(self, claude_model_name: str) -> str:
        """Resolve a Claude model name to the configured provider/model ref."""

        route = self._matched_route(claude_model_name)
        if route is not None:
            model = getattr(self._settings, route[1])
            if isinstance(model, str):
                return model
        return self._settings.model

    def _resolve_reasoning_preference(
        self, claude_model_name: str
    ) -> ReasoningPreference:
        """Resolve a route override without inspecting the provider model."""

        route = self._matched_route(claude_model_name)
        if route is not None:
            preference = getattr(self._settings, route[2])
            if preference is not ReasoningPreference.INHERIT:
                return preference
        return self._settings.reasoning_policy

    @staticmethod
    def _matched_route(model_name: str) -> tuple[str, str, str, str] | None:
        normalized = model_name.lower()
        return next(
            (route for route in _ROUTE_SETTINGS if route[0] in normalized),
            None,
        )

    def resolve_messages_request(
        self, request: MessagesRequest
    ) -> RoutedMessagesRequest:
        """Return an internal routed request context."""
        return self._route_for(request, self.resolve(request.model))

    def resolve_messages_plan(self, request: MessagesRequest) -> RoutedMessagesPlan:
        """Return the primary routed request plus its configured fallbacks."""
        route_chain = self.resolve_chain(request.model)
        chain, vision_unavailable = self._apply_vision_policy(request, route_chain)
        diverted = chain[0].provider_model_ref != route_chain[0].provider_model_ref
        if diverted:
            diversion = RouteDiversion.VISION
        elif vision_unavailable:
            diversion = RouteDiversion.VISION_UNAVAILABLE
        else:
            diversion = None
        plan = RoutedMessagesPlan(
            tuple(self._route_for(request, resolved) for resolved in chain),
            diverted_from=(route_chain[0].provider_model_ref if diverted else None),
            diversion=diversion,
            probe_candidates=self._probe_candidates(),
        )
        if plan.has_fallbacks or diverted:
            logger.debug(
                "MODEL CHAIN: '{}' -> {}",
                request.model,
                " -> ".join(plan.model_refs()),
            )
        return plan

    #: The order the probe walks the operator's own routes in. Haiku first
    #: because it is the declared small/cheap model; the provider's ``/models``
    #: catalogue is never consulted, because that is where hidden models and
    #: paid-tier surprises live.
    _PROBE_ROUTE_ORDER = ("haiku", "sonnet", "default", "fable", "opus")

    def _probe_candidates(self) -> dict[str, ResolvedModel]:
        """One configured model per provider, for the diagnostic probe.

        Every entry comes from a route the operator wrote down -- the five
        model settings and their fallback lists -- resolved through the same
        ``resolve_chain`` the request itself uses. The first model that lands
        on a provider wins; a provider named by no route gets no entry at all,
        and the executor answers that by skipping the probe and doing exactly
        what 6.19.0 did.
        """
        if self._probe_candidate_cache is not None:
            return self._probe_candidate_cache
        candidates: dict[str, ResolvedModel] = {}
        for route_name in self._PROBE_ROUTE_ORDER:
            try:
                chain = self.resolve_chain(route_name)
            except Exception:
                # A route naming an unknown provider must not take down the
                # probe for every other route, exactly as one bad fallback
                # entry does not take down a chain.
                continue
            for resolved in chain:
                candidates.setdefault(resolved.provider_id, resolved)
        self._probe_candidate_cache = candidates
        return candidates

    def _apply_vision_policy(
        self, request: MessagesRequest, chain: tuple[ResolvedModel, ...]
    ) -> tuple[tuple[ResolvedModel, ...], bool]:
        """Keep an image-carrying request away from models known to be blind.

        Only a model *known* to reject images is diverted. An unknown
        capability is left alone: most providers publish no modality metadata
        at all, and rerouting on silence would move traffic away from models
        that handle images perfectly well.

        ``MODEL_VISION`` is the preferred destination, but it is not the only
        one. When it is unset, a chain entry that can see is still a better
        answer than sending the image to a model documented not to accept it --
        which either fails outright or, worse, answers about an image it never
        received.
        """
        if not request_carries_image(request):
            return chain, False
        if self._supports_vision(chain[0]) is not False:
            return chain, False

        vision_chain = self._vision_adapter_chain(chain[0])
        vision_model = vision_chain[0] if vision_chain else None
        vision_refs = {resolved.provider_model_ref for resolved in vision_chain}
        # A fallback that is itself known to be blind would answer a question
        # about an image it cannot see, which is worse than failing. The
        # adapter's own chain leads, then whatever on the route can still see.
        sighted = (
            *vision_chain[1:],
            *(
                resolved
                for resolved in chain
                if resolved.provider_model_ref not in vision_refs
                and self._supports_vision(resolved) is not False
            ),
        )
        if vision_model is not None:
            logger.info(
                "VISION ROUTE: '{}' carries an image and '{}' cannot read it;"
                " using '{}'",
                chain[0].original_model,
                chain[0].provider_model_ref,
                vision_model.provider_model_ref,
            )
            return (vision_model, *sighted), False
        if sighted:
            logger.info(
                "VISION ROUTE: '{}' carries an image and '{}' cannot read it;"
                " no MODEL_VISION is set, so the chain's '{}' leads instead",
                chain[0].original_model,
                chain[0].provider_model_ref,
                sighted[0].provider_model_ref,
            )
            return sighted, False
        logger.warning(
            "VISION ROUTE UNAVAILABLE: '{}' carries an image and no model on"
            " this route is known to accept one; trying '{}' anyway",
            chain[0].original_model,
            chain[0].provider_model_ref,
        )
        return chain, True

    def _vision_adapter_chain(
        self, primary: ResolvedModel
    ) -> tuple[ResolvedModel, ...]:
        """Resolve ``MODEL_VISION`` plus its own fallbacks into routable models.

        The adapter is a route like any other and gets the same safety net: a
        single unreachable vision model would otherwise lose every image on the
        machine. An entry known to reject images is dropped -- putting a blind
        model in a *vision* chain is always a mistake, not a preference.
        """
        if not self._settings.model_vision:
            return ()
        resolved: list[ResolvedModel] = []
        seen: set[str] = set()
        candidates = (
            self._settings.model_vision,
            *parse_model_ref_list(self._settings.model_vision_fallbacks),
        )
        for model_ref in candidates:
            if model_ref in seen:
                continue
            seen.add(model_ref)
            provider_id = parse_provider_type(model_ref)
            try:
                self._validate_provider_id(provider_id)
            except UnknownProviderError:
                logger.warning(
                    "VISION ROUTE SKIPPED: '{}' names unknown provider '{}'",
                    model_ref,
                    provider_id,
                )
                continue
            candidate = ResolvedModel(
                original_model=primary.original_model,
                provider_id=provider_id,
                provider_model=parse_model_name(model_ref),
                provider_model_ref=model_ref,
                reasoning_preference=primary.reasoning_preference,
            )
            if self._supports_vision(candidate) is False:
                logger.warning(
                    "VISION ROUTE SKIPPED: '{}' is known not to accept images",
                    model_ref,
                )
                continue
            resolved.append(candidate)
        return tuple(resolved)

    def _supports_vision(self, resolved: ResolvedModel) -> bool | None:
        if self._vision_lookup is None:
            return None
        return self._vision_lookup(resolved.provider_id, resolved.provider_model)

    def _route_for(
        self, request: MessagesRequest, resolved: ResolvedModel
    ) -> RoutedMessagesRequest:
        routed = request.model_copy(deep=True)
        routed.model = resolved.provider_model
        policy = resolve_reasoning_policy(routed, resolved.reasoning_preference)
        # Looked up once and handed to both consumers: gating decides what may
        # be sent with it, and the routed request carries it onward so the
        # execution layer can answer "is this really a thinking attempt" for
        # ADAPTIVE without a second lookup.
        dialect = self._dialect_for(resolved)
        reasoning, reasoning_adaptation = self._gate_reasoning(
            policy, routed, resolved, dialect
        )
        return RoutedMessagesRequest(
            request=routed,
            resolved=resolved,
            reasoning=reasoning,
            requested_reasoning=policy,
            reasoning_adaptation=reasoning_adaptation,
            output_limits=self._output_limits(resolved),
            reasoning_dialect=dialect,
        )

    def _output_limits(self, resolved: ResolvedModel) -> OutputTokenLimits:
        """Collect what is known about this model's output capacity.

        An absent lookup means every model is unknown, which leaves the
        operator's fallback in charge -- the same "changes nothing at all"
        default the vision and reasoning lookups already have.
        """

        return OutputTokenLimits(
            limit=self._model_output_limit(resolved),
            context_length=self._model_context_length(resolved),
            unknown_default=self._settings.max_output_tokens_unknown_default,
            ceiling=self._settings.max_output_tokens_ceiling,
            context_margin=self._settings.max_output_tokens_context_margin,
            context_floor=self._settings.max_output_tokens_context_floor,
            answer_floor_max=self._settings.reasoning_answer_floor_max,
        )

    def _dialect_for(self, resolved: ResolvedModel) -> ReasoningDialect | None:
        """Which reasoning fields the HOST parses for this model.

        A fact the model's own capability record cannot state, because one
        gateway can parse a different set per model. An absent lookup means
        unknown, which leaves the model's capability in sole charge.
        """

        if self._reasoning_dialect_lookup is None:
            return None
        return self._reasoning_dialect_lookup(
            resolved.provider_id, resolved.provider_model
        )

    def _model_output_limit(self, resolved: ResolvedModel) -> int | None:
        if self._output_limit_lookup is None:
            return None
        return self._output_limit_lookup(resolved.provider_id, resolved.provider_model)

    def _model_context_length(self, resolved: ResolvedModel) -> int | None:
        if self._context_length_lookup is None:
            return None
        return self._context_length_lookup(
            resolved.provider_id, resolved.provider_model
        )

    def _gate_reasoning(
        self,
        policy: ReasoningPolicy,
        request: MessagesRequest,
        resolved: ResolvedModel,
        dialect: ReasoningDialect | None,
    ) -> tuple[ReasoningPolicy, ReasoningAdaptation]:
        """Narrow one resolved policy to what the resolved model accepts.

        ``dialect`` is passed in rather than looked up here so the route
        resolves it exactly once: the routed request keeps it too.
        """

        if (
            self._reasoning_capability_lookup is None
            and self._reasoning_dialect_lookup is None
        ):
            return policy, ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)
        capability = (
            self._reasoning_capability_lookup(
                resolved.provider_id, resolved.provider_model
            )
            if self._reasoning_capability_lookup is not None
            else None
        )
        output_limit = self._model_output_limit(resolved)
        return adapt_reasoning_policy(
            policy,
            capability,
            dialect=dialect,
            max_tokens=request.max_tokens,
            output_limit=output_limit,
            answer_floor_max=self._settings.reasoning_answer_floor_max,
            model_ref=resolved.provider_model_ref,
        )

    def resolve_token_count_request(
        self, request: TokenCountRequest
    ) -> RoutedTokenCountRequest:
        """Return an internal token-count request context."""
        resolved = self.resolve(request.model)
        routed = request.model_copy(
            update={"model": resolved.provider_model}, deep=True
        )
        return RoutedTokenCountRequest(request=routed, resolved=resolved)
