"""Decide one request's reasoning controls from two independent facts.

The resolved :class:`ReasoningPolicy` says what the user asked for. Two things
then decide what may go on the wire, and they are not the same thing:

* **model capability** (:class:`ModelReasoningCapability`) -- can this model
  reason, and which knobs does the *model* have: an effort scale (with a
  vocabulary), an on/off switch, a numeric budget, is thinking mandatory;
* **host dialect** (:class:`~my_claude_code.core.reasoning.ReasoningDialect`)
  -- which reasoning *fields* the gateway in front of it actually parses for
  this request.

A control is emitted only when the model has that knob **and** the host has a
field for it. Otherwise the nearest thing both can express is sent, and the
returned :class:`ReasoningAdaptation` says exactly what went out. Reading only
the first fact is what produced ``reasoning_effort: "max"`` for a toggle-only
model on Command Code -- gating said "enabling thinking", and the encoder,
having no on/off channel of its own, re-invented a rung.

This module still produces another :class:`ReasoningPolicy` and never a wire
field: provider encoders keep sole ownership of the wire shape.

The overriding rule is that *unknown* never adds a restriction. Most providers
publish no reasoning metadata at all, and a ``None`` on either side leaves the
other side in sole charge; ``None`` on both leaves the policy exactly as it
arrived, byte-identical to the behaviour before any of this existed.
"""

from dataclasses import dataclass, replace

from loguru import logger

from my_claude_code.application.model_metadata import ModelReasoningCapability
from my_claude_code.application.reasoning_budget import (
    bound_budget,
    budget_for_effort,
    effort_for_budget,
)
from my_claude_code.config.constants import REASONING_ANSWER_FLOOR_MAX
from my_claude_code.core.reasoning import (
    EFFORT_RANK,
    MINIMUM_BUDGET_TOKENS,
    ReasoningAdaptation,
    ReasoningAdaptationKind,
    ReasoningControl,
    ReasoningDialect,
    ReasoningEffort,
    ReasoningPolicy,
    combine_reasoning_adaptations,
    effort_intersection,
    nearest_effort,
)

_UNCHANGED = ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)


def adapt_reasoning_policy(
    policy: ReasoningPolicy,
    capability: ModelReasoningCapability | None,
    *,
    dialect: ReasoningDialect | None = None,
    max_tokens: int | None = None,
    output_limit: int | None = None,
    answer_floor_max: int = REASONING_ANSWER_FLOOR_MAX,
    model_ref: str = "",
) -> tuple[ReasoningPolicy, ReasoningAdaptation]:
    """Return the policy this model on this host can be told, and what changed.

    ``capability`` and ``dialect`` both ``None`` -- no models.dev row, no
    provider opinion, no declared dialect -- returns ``policy`` itself,
    unchanged and by identity, so the request built from it is byte-identical
    to the one built before this gating existed.
    """

    if capability is None and dialect is None:
        return policy, _UNCHANGED
    if policy.control is ReasoningControl.OFF:
        # An explicit "off" is already the least surprising thing to send, and
        # every encoder that has an off has a disabled path for it. Never
        # rewrite it -- unless the model is known to reject disabled thinking
        # outright, in which case OFF would fail the whole request and the
        # floor is the honest nearest thing to what was asked for.
        if capability is not None and capability.mandatory is True:
            return _mandatory_off_rewrite(policy, capability, dialect, model_ref)
        return policy, _UNCHANGED
    if capability is not None and capability.can_reason is False:
        return _suppress(policy, model_ref)
    if not policy.requests_reasoning:
        # Nothing was asked for; there is nothing to clamp, and inventing a
        # request here would send reasoning nobody wanted.
        return policy, _UNCHANGED

    if capability is not None and _publishes_no_control(capability):
        # models.dev spells this ``reasoning: true`` with ``reasoning_options:
        # []`` -- "reasoning is on, but the caller has no control" -- and 1,223
        # of its 5,230 reasoning models (23%) carry it. Without this branch
        # every guard below is skipped and the raw effort falls straight
        # through to the wire, into a field the model does not have.
        return _drop_controls(policy, dialect, model_ref)

    working = policy
    adaptations: list[ReasoningAdaptation] = []
    if working.budget_tokens is not None:
        working, adaptation = _adapt_budget(
            working,
            capability,
            dialect,
            max_tokens,
            output_limit,
            answer_floor_max,
            model_ref,
        )
        adaptations.append(adaptation)
    if working.budget_tokens is None:
        working, adaptation = _adapt_effort(
            working,
            capability,
            dialect,
            max_tokens,
            output_limit,
            answer_floor_max,
            model_ref,
        )
        adaptations.append(adaptation)
    return working, combine_reasoning_adaptations(*adaptations)


@dataclass(frozen=True, slots=True)
class _Channels:
    """Which reasoning channels are open once both facts are consulted.

    Built once per request so every branch below reads the same answer, and so
    the asymmetry that matters is stated exactly once: a host's on-value that
    is *written into its effort field* is a default rung, not an on/off
    channel. Command Code's ``NamedEffortReasoning(enabled_value="max")`` is
    the case -- perfect for "reason, I named no level", and completely wrong as
    a stand-in for a level the caller did name, which is how a request for
    ``low`` used to leave as ``max`` -- what PR G adds is that the level the
    caller *did* name is now written into that same field, clamped, instead of
    being discarded. :attr:`toggle` is therefore the channel a *discarded*
    effort may fall back to, :attr:`default_rung` is the wider "this host can
    be told to reason at all", and
    :attr:`on_signal_is_the_effort_field` is the narrower "and the word it uses
    is a rung, so the caller's own rung fits in it". Three overlapping booleans
    describe one host's on-signal and they are not interchangeable: reading the
    wrong one is what shipped the regression this class was added to end, so a
    new consumer must choose between them deliberately.
    """

    effort_values: frozenset[ReasoningEffort] | None
    effort: bool
    effort_stated: bool
    toggle: bool
    budget: bool
    budget_denied: bool
    default_rung: bool
    on_signal_is_the_effort_field: bool
    effort_field: str
    toggle_field: str
    budget_field: str


def _channels(
    capability: ModelReasoningCapability | None, dialect: ReasoningDialect | None
) -> _Channels:
    """Intersect what the MODEL has with what the HOST parses.

    Unknown on either side is permissive: a ``None`` capability leaves the host
    in sole charge, an absent dialect leaves the model in sole charge, and a
    field neither states stays open exactly as it was before dialects existed.
    """

    model_effort = capability.supports_effort_control if capability else None
    model_toggle = capability.supports_toggle_control if capability else None
    model_budget = capability.supports_budget_control if capability else None

    host_effort = dialect is None or dialect.effort_values is not None
    host_effort_stated = dialect is not None and dialect.effort_values is not None
    # ``dialect.toggle`` with the toggle written into the effort field is a
    # default rung, not a switch; see :class:`_Channels`.
    host_toggle = dialect is None or (
        dialect.toggle and dialect.toggle_field != dialect.effort_field
    )
    host_budget = dialect is None or dialect.budget
    host_default_rung = dialect is None or dialect.toggle
    # The host can be told to reason, but the only word it has for "reason" is
    # one of its own effort rungs. That is not an on/off channel -- hence the
    # narrowing at ``host_toggle`` -- but it IS a field the caller's own rung
    # can be written into, which is the one thing the toggle-only branch needs
    # to know and the only reason this is a separate flag from ``toggle``.
    # All four conjuncts are load-bearing: an unknown host must keep falling
    # through untouched rather than clamp against a vocabulary nobody stated; a
    # host that cannot be told to reason at all is never handed a rung it never
    # advertised; a host with a real switch has already taken the toggle
    # branch; and without an enum there is nothing to clamp to and nothing for
    # the encoder to spell.
    host_on_signal_is_effort = (
        dialect is not None
        and dialect.toggle
        and dialect.toggle_field == dialect.effort_field
        and dialect.effort_values is not None
    )

    # An unknown model vocabulary leaves the host's enum in sole charge; a
    # stated one is intersected with it, because a value only one of the two
    # accepts is a 400 or a silent discard.
    model_vocabulary = capability.supported_efforts if capability else None
    if model_effort is None:
        model_vocabulary = None
    host_vocabulary = dialect.effort_values if dialect is not None else None

    # An unstated knob is open, but it is not evidence *for* a channel: a
    # substitution has to land somewhere a side actually named, or a client
    # budget on a model nobody has described would become an effort word out of
    # nowhere. Hence the pair -- ``effort`` is "not ruled out", ``effort_stated``
    # is "somebody said so".
    known_host = dialect is not None
    return _Channels(
        effort_values=effort_intersection(model_vocabulary, host_vocabulary),
        effort=model_effort is not False and host_effort,
        effort_stated=(model_effort is True or (known_host and host_effort_stated))
        and host_effort,
        toggle=(model_toggle is True or (model_toggle is None and known_host))
        and host_toggle,
        budget=(model_budget is True or (model_budget is None and known_host))
        and host_budget,
        budget_denied=model_budget is False or (known_host and not host_budget),
        default_rung=host_default_rung,
        on_signal_is_the_effort_field=host_on_signal_is_effort,
        effort_field=dialect.effort_field if dialect else "",
        toggle_field=dialect.toggle_field if dialect else "",
        budget_field=dialect.budget_field if dialect else "",
    )


def _via(field: str) -> str:
    """Name the wire field an adaptation is about, when the host named one."""

    return f" via `{field}`" if field else ""


def _publishes_no_control(capability: ModelReasoningCapability) -> bool:
    """Return whether the model reasons but exposes no reasoning knob at all.

    All three ``supports_*_control`` flags explicitly ``False`` is a *stated*
    fact, not silence, and must never be confused with the unknown case where
    they are ``None`` -- unknown has to keep passing through untouched. The
    parser preserves that three-state distinction precisely so this branch can
    read it.
    """

    return capability.can_reason is not False and (
        capability.supports_effort_control is False
        and capability.supports_toggle_control is False
        and capability.supports_budget_control is False
    )


def _drop_controls(
    policy: ReasoningPolicy, dialect: ReasoningDialect | None, model_ref: str
) -> tuple[ReasoningPolicy, ReasoningAdaptation]:
    """Keep thinking on for a model that accepts no reasoning control.

    The kind follows the wire, not the intent. Where the host has some field
    to say "reason" with, the level was discarded and thinking stays on: that
    is ``DROPPED``, and a body with no reasoning key would contradict it. Where
    it has none, nothing at all leaves and the model's own default applies:
    that is ``NOTHING_SENT``, and a body with no reasoning key is the outcome.
    One value for both is what badged the correct case as a defect.

    A host with no way to say "reason" at all gets ``provider_default()``
    instead of a bare ON that would encode to nothing: an ON nobody can spell
    is not an instruction, and claiming to have sent one is what made the old
    log line ("enabling thinking") disagree with three providers' actual empty
    bodies.
    """

    can_enable = dialect is None or dialect.toggle
    enabled = ReasoningPolicy.on() if can_enable else ReasoningPolicy.provider_default()
    if policy == enabled:
        return enabled, _UNCHANGED
    if policy.effort is not None:
        asked = f"effort '{policy.effort.value}'"
    elif policy.budget_tokens is not None:
        asked = f"a {policy.budget_tokens}-token thinking budget"
    else:
        asked = "the requested reasoning controls"
    if can_enable:
        outcome = "enabling thinking and discarding"
        if dialect is not None:
            outcome = f"enabling thinking{_via(dialect.toggle_field)} and discarding"
    else:
        outcome = (
            "sending no reasoning instruction -- the model's own default"
            " applies -- and discarding"
        )
    message = (
        f"REASONING LEVEL DROPPED: '{model_ref}' reasons but publishes no"
        f" reasoning control; {outcome} {asked}"
    )
    logger.warning(message)
    kind = (
        ReasoningAdaptationKind.DROPPED
        if can_enable
        else ReasoningAdaptationKind.NOTHING_SENT
    )
    return enabled, ReasoningAdaptation(kind, message)


def _mandatory_off_rewrite(
    policy: ReasoningPolicy,
    capability: ModelReasoningCapability,
    dialect: ReasoningDialect | None,
    model_ref: str,
) -> tuple[ReasoningPolicy, ReasoningAdaptation]:
    """Rewrite an OFF request for a model that cannot disable thinking.

    The model rejects ``effort: "none"`` outright, so an honest "off" on the
    wire would 400. The closest thing that can actually be sent is thinking on
    at the floor of what the model and the host *both* accept -- an effort only
    one of them knows is no more sendable here than the OFF was -- or
    ``ReasoningControl.ADAPTIVE`` when there is no shared vocabulary at all,
    which lets the model itself pick the floor. The SUBSTITUTED warning
    surfaces in the request log so the operator sees why the off they asked for
    is not what was sent.
    """

    vocabulary = effort_intersection(
        capability.supported_efforts,
        dialect.effort_values if dialect is not None else None,
    )
    if vocabulary:
        floor = min(vocabulary, key=lambda effort: EFFORT_RANK[effort])
        rewritten = ReasoningPolicy.on(effort=floor)
        field = _via(dialect.effort_field) if dialect is not None else ""
        message = (
            f"REASONING OFF SUBSTITUTED: '{model_ref}' cannot run with thinking"
            f" disabled; sending effort '{floor.value}' (its lowest){field} instead"
        )
    else:
        rewritten = ReasoningPolicy.adaptive()
        message = (
            f"REASONING OFF SUBSTITUTED: '{model_ref}' cannot run with thinking"
            f" disabled; sending adaptive thinking instead"
        )
    logger.warning(message)
    return rewritten, ReasoningAdaptation(ReasoningAdaptationKind.SUBSTITUTED, message)


def _suppress(
    policy: ReasoningPolicy, model_ref: str
) -> tuple[ReasoningPolicy, ReasoningAdaptation]:
    """Drop every reasoning control for a model known not to reason."""

    suppressed = ReasoningPolicy.provider_default()
    if policy == suppressed:
        return suppressed, _UNCHANGED
    message = (
        f"REASONING SUPPRESSED: '{model_ref}' is known not to support reasoning;"
        f" dropping the requested reasoning controls"
    )
    logger.warning(message)
    return suppressed, ReasoningAdaptation(ReasoningAdaptationKind.SUPPRESSED, message)


def _adapt_budget(
    policy: ReasoningPolicy,
    capability: ModelReasoningCapability | None,
    dialect: ReasoningDialect | None,
    max_tokens: int | None,
    output_limit: int | None,
    answer_floor_max: int,
    model_ref: str,
) -> tuple[ReasoningPolicy, ReasoningAdaptation]:
    """Handle an explicit numeric budget against model and host together."""

    budget = policy.budget_tokens
    if budget is None:
        return policy, _UNCHANGED

    channels = _channels(capability, dialect)
    model_budget = capability.supports_budget_control if capability else None

    if channels.budget_denied and channels.effort_stated:
        # No budget channel, but a stated effort one: no vendor publishes a
        # budget -> effort mapping, so the inverse of FCC's own effort ->
        # budget table is used -- the strongest effort whose budget still fits
        # inside what the client asked for -- then narrowed to what both sides
        # accept, because an effort only one of them knows is no more sendable
        # than the number was.
        derived = effort_for_budget(budget, _effective_output(max_tokens, output_limit))
        if channels.effort_values:
            derived = nearest_effort(derived, channels.effort_values)
        reason = (
            f"the host for '{model_ref}' has no thinking-token budget field"
            if model_budget is not False
            else f"'{model_ref}' has no thinking-token budget"
        )
        message = (
            f"REASONING BUDGET SUBSTITUTED: {reason};"
            f" sending effort '{derived.value}'{_via(channels.effort_field)} instead"
            f" of the requested {budget} tokens"
        )
        logger.warning(message)
        return ReasoningPolicy(
            control=policy.control,
            effort=derived,
            budget_tokens=None,
        ), ReasoningAdaptation(ReasoningAdaptationKind.SUBSTITUTED, message)

    if dialect is not None and not dialect.budget and not channels.effort:
        # Nowhere at all to put a number, and no effort field to translate it
        # into. Sending it anyway is how a gateway 400s or silently discards.
        kept = (
            ReasoningPolicy.on()
            if channels.default_rung
            else (ReasoningPolicy.provider_default())
        )
        outcome = (
            f"enabling thinking{_via(channels.toggle_field)}"
            if channels.default_rung
            else "sending no reasoning instruction"
        )
        message = (
            f"REASONING BUDGET DROPPED: the host for '{model_ref}' parses no"
            f" thinking-token budget field and no effort field; {outcome}"
            f" instead of the requested {budget} tokens"
        )
        logger.warning(message)
        return kept, ReasoningAdaptation(
            ReasoningAdaptationKind.DROPPED
            if channels.default_rung
            else ReasoningAdaptationKind.NOTHING_SENT,
            message,
        )

    # Clamp whenever the model's output limit is known, not only when *budget
    # control* is known. ``supports_budget_control`` is ``None`` for most
    # models, so a client budget was passing through entirely unclamped on
    # every one of them even where ``limit.output`` was published: knowing what
    # the model can emit is already enough to know the budget cannot exceed it.
    if not model_budget and output_limit is None:
        return policy, _UNCHANGED

    clamped = _clamp_budget(budget, output_limit, answer_floor_max)
    if clamped == budget:
        return policy, _UNCHANGED
    message = (
        f"REASONING BUDGET CLAMPED: '{model_ref}' accepts"
        f" {MINIMUM_BUDGET_TOKENS}..{output_limit if output_limit is not None else 'unbounded'}"
        f" thinking tokens; sending {clamped}{_via(channels.budget_field)} instead"
        f" of the requested {budget}"
    )
    logger.warning(message)
    return replace(policy, budget_tokens=clamped), ReasoningAdaptation(
        ReasoningAdaptationKind.CLAMPED, message
    )


def _adapt_effort(
    policy: ReasoningPolicy,
    capability: ModelReasoningCapability | None,
    dialect: ReasoningDialect | None,
    max_tokens: int | None,
    output_limit: int | None,
    answer_floor_max: int,
    model_ref: str,
) -> tuple[ReasoningPolicy, ReasoningAdaptation]:
    """Handle a named effort (or a bare "on") against model and host together."""

    effort = policy.effort
    channels = _channels(capability, dialect)

    if channels.effort:
        supported = channels.effort_values
        if not supported or effort is None or effort in supported:
            return policy, _UNCHANGED
        clamped = nearest_effort(effort, supported)
        message = (
            f"REASONING EFFORT CLAMPED: '{model_ref}' does not accept effort"
            f" '{effort.value}'; sending '{clamped.value}'"
            f"{_via(channels.effort_field)} instead"
        )
        logger.warning(message)
        return replace(policy, effort=clamped), ReasoningAdaptation(
            ReasoningAdaptationKind.CLAMPED, message
        )

    if effort is None:
        # A bare ON: there is no level to lose, and the encoder's own on-value
        # is exactly the right thing for "reason, I named no rung".
        return policy, _UNCHANGED

    if channels.budget and not (
        capability is not None and capability.supports_effort_control is False
    ):
        # The MODEL has an effort knob (or nobody has said otherwise) and only
        # this HOST has no field for it. The effort is still the user's ask, so
        # it is kept exactly as written and only its *pricing* is deferred:
        # ``numeric_budget_tokens`` already turns an effort into a number for
        # budget-only encoders, and ``apply_reasoning_budget`` sizes that number
        # against the model's real output allowance once ``max_tokens`` is
        # final. Writing a number here instead would freeze the flat
        # last-resort table's value -- 1,024 for 'low' against a 32,768-token
        # allowance whose real share is 6,553 -- which is WORKING-NOTES 54's
        # "never under-use" in reverse.
        message = (
            f"REASONING EFFORT SUBSTITUTED: the host for '{model_ref}' parses no"
            f" effort field; effort '{effort.value}' will be sent as a thinking"
            f" budget{_via(channels.budget_field)}"
        )
        logger.warning(message)
        return policy, ReasoningAdaptation(ReasoningAdaptationKind.SUBSTITUTED, message)

    if channels.budget:
        budget = _budget_for_effort(effort, max_tokens, output_limit, answer_floor_max)
        owner = (
            f"'{model_ref}' has no effort control"
            if capability is not None and capability.supports_effort_control is False
            else f"the host for '{model_ref}' parses no effort field"
        )
        message = (
            f"REASONING EFFORT SUBSTITUTED: {owner};"
            f" sending a {budget}-token thinking budget"
            f"{_via(channels.budget_field)} for effort '{effort.value}'"
        )
        logger.warning(message)
        # The effort is kept alongside the budget on purpose: an encoder with
        # no budget field of its own still has the effort to fall back on.
        return ReasoningPolicy(
            control=ReasoningControl.ON,
            effort=effort,
            budget_tokens=budget,
        ), ReasoningAdaptation(ReasoningAdaptationKind.SUBSTITUTED, message)

    if channels.toggle:
        message = (
            f"REASONING LEVEL DROPPED: '{model_ref}' can only switch thinking on"
            f" or off; enabling thinking{_via(channels.toggle_field)} and"
            f" discarding effort '{effort.value}'"
        )
        logger.warning(message)
        return ReasoningPolicy.on(), ReasoningAdaptation(
            ReasoningAdaptationKind.DROPPED, message
        )

    if channels.on_signal_is_the_effort_field and bool(
        capability and capability.supports_toggle_control
    ):
        # The model publishes an on/off switch and no effort scale; this host
        # has no on/off field and spells "reason" with one of its own effort
        # rungs. Nothing at all was the old answer and it cost the whole
        # instruction. The host's ``enabled_value`` is not the answer either --
        # answering a request for 'low' with a stranger's 'max' is the
        # regression the narrowing above removed. What IS honest is the
        # caller's own rung, moved only as far as the host's enum forces: the
        # field exists, the number in it is the number that was asked for, and
        # no rung is invented. A host that forwards it to a model that refuses
        # it pays one 400 and is remembered (openai_chat/reasoning_reject.py).
        #
        # ``channels.effort_values`` is the intersection, which for a
        # self-consistent toggle-only record (no stated vocabulary) reduces to
        # the host's own enum. An empty intersection means the two sides named
        # disjoint vocabularies; the host's enum is then the only one the
        # encoder can spell, so the fallback is deliberately the host's, never
        # the unclamped request.
        supported = channels.effort_values or (
            dialect.effort_values if dialect is not None else None
        )
        sent = (
            effort
            if not supported or effort in supported
            else nearest_effort(effort, supported)
        )
        if sent == effort:
            # INFO, and ``_UNCHANGED``: ``ReasoningAdaptation`` documents
            # ``message is None`` exactly when the kind is ``UNCHANGED``, and
            # nothing about the request changed. The operator still sees the
            # outcome, because the wire capture records the value that left.
            logger.info(
                f"REASONING LEVEL PASSED THROUGH: '{model_ref}' publishes only"
                f" an on/off switch and this host has no on/off field; sending"
                f" effort '{sent.value}'{_via(channels.effort_field)} as the"
                f" host's on-signal"
            )
            return replace(policy, control=ReasoningControl.ON, effort=sent), _UNCHANGED
        message = (
            f"REASONING EFFORT CLAMPED: '{model_ref}' publishes only an on/off"
            f" switch and this host has no on/off field; its on-signal is the"
            f" effort field, so effort '{effort.value}' is sent as"
            f" '{sent.value}'{_via(channels.effort_field)} -- the rung asked"
            f" for, clamped to the host's enum, not the host's own default rung"
        )
        logger.warning(message)
        return replace(policy, control=ReasoningControl.ON, effort=sent), (
            ReasoningAdaptation(ReasoningAdaptationKind.CLAMPED, message)
        )

    if dialect is not None and bool(capability and capability.supports_toggle_control):
        # Same model, but this host has no reasoning field of any kind to write
        # the rung into. Nothing at all remains the honest wire and the model's
        # own default reasoning behaviour stands.
        message = (
            f"REASONING LEVEL DROPPED: '{model_ref}' has an on/off switch only"
            f" and this host has no on/off field; sending no reasoning"
            f" instruction -- the model's own default applies -- instead of"
            f" effort '{effort.value}'"
        )
        logger.warning(message)
        return ReasoningPolicy.provider_default(), ReasoningAdaptation(
            ReasoningAdaptationKind.NOTHING_SENT, message
        )

    return policy, _UNCHANGED


def _budget_for_effort(
    effort: ReasoningEffort,
    max_tokens: int | None,
    output_limit: int | None,
    answer_floor_max: int,
) -> int:
    """Synthesise a thinking budget for one effort, sized to this model."""

    effective_output = _effective_output(max_tokens, output_limit)
    if effective_output is None:
        return effort.budget_tokens
    return budget_for_effort(effort, effective_output, answer_floor_max)


def _effective_output(max_tokens: int | None, output_limit: int | None) -> int | None:
    """Return the output allowance this request has to share, if known.

    ``None`` when nothing publishes one and the client named none either, which
    is what leaves the flat last-resort table in charge.
    """

    candidates = [value for value in (max_tokens, output_limit) if value is not None]
    if not candidates:
        return None
    return min(candidates)


def _clamp_budget(budget: int, output_limit: int | None, answer_floor_max: int) -> int:
    if output_limit is None:
        return max(budget, MINIMUM_BUDGET_TOKENS)
    return bound_budget(budget, output_limit, answer_floor_max)
