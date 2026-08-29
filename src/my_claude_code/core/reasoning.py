"""Provider-neutral reasoning intent."""

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum


class ReasoningControl(StrEnum):
    """Whether a request explicitly controls reasoning computation."""

    DEFAULT = "default"
    OFF = "off"
    ON = "on"
    ADAPTIVE = "adaptive"
    """Let the upstream model decide how much to think, per request.

    This is a distinct intent from ``DEFAULT`` (no opinion was expressed) and
    from ``ON`` (reasoning was demanded, optionally at a named effort). Only a
    provider that publishes an adaptive channel of its own can encode it; every
    other encoder sees a policy that asks for no representable control and
    therefore sends the provider's own default, exactly as ``DEFAULT`` does.
    """


class ReasoningEffort(StrEnum):
    """Named reasoning effort understood at the FCC application boundary."""

    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"

    @property
    def budget_tokens(self) -> int:
        """Return FCC's numeric token budget for this effort."""

        return _EFFORT_BUDGET_TOKENS[self]


EFFORT_BY_VALUE: dict[str, ReasoningEffort] = {
    member.value: member for member in ReasoningEffort
}
"""Wire value -> member, for sources that publish effort names as strings.

Owned here so every parser that reads an upstream effort vocabulary drops
exactly the same non-member strings; a second copy elsewhere would let two
sources disagree about what "default" or "none" means.
"""


MINIMUM_BUDGET_TOKENS = 1_024
"""Smallest thinking budget any FCC-supported dialect accepts.

Anthropic's documented extended-thinking minimum, and the same floor Cohere
and the OpenAI-dialect ``thinking`` encoders already use. Owned here so the
gating layer and the wire encoders cannot drift apart about it.
"""


_EFFORT_BUDGET_TOKENS = {
    ReasoningEffort.MINIMAL: 1_024,
    ReasoningEffort.LOW: 1_024,
    ReasoningEffort.MEDIUM: 1_024,
    ReasoningEffort.HIGH: 2_048,
    ReasoningEffort.XHIGH: 4_096,
    ReasoningEffort.MAX: 8_192,
}
"""LAST-RESORT flat effort -> budget map, for a model with no known limit.

This table is model-independent and therefore cannot be the primary answer:
"high effort" must not mean 2,048 tokens on a 230,400-output model and 2,048
on a 16,384-output one. The capability-aware derivation in
``application.reasoning_budget`` is the single source of truth whenever
anything at all publishes the model's output allowance, and it writes its
result onto :attr:`ReasoningPolicy.effort_budget_tokens`. These numbers are
reached only when nothing does -- no provider ``/models`` limit, no models.dev
row, and no configured unknown-default.
"""


@dataclass(frozen=True, slots=True)
class ReasoningPolicy:
    """Resolved client and configuration intent passed to one provider.

    ``control`` and ``effort`` remain independent because clients may set an
    overall effort while separately disabling extended thinking. Providers
    translate the representable subset without changing the original intent.
    """

    control: ReasoningControl = ReasoningControl.DEFAULT
    effort: ReasoningEffort | None = None
    budget_tokens: int | None = None
    effort_budget_tokens: int | None = None
    """Capability-derived token cost of :attr:`effort`, when one is known.

    Deliberately *not* ``budget_tokens``: that field means "the caller named
    this exact number of thinking tokens", and every encoder that can express
    both a budget and an effort checks it first. Writing a derived value there
    would flip every effort-capable provider onto its budget channel. This
    field only replaces the flat ``_EFFORT_BUDGET_TOKENS`` fallback inside
    :attr:`numeric_budget_tokens`, so encoders that have no budget field are
    unaffected and encoders that only have one get a model-sized number
    instead of a model-independent one.
    """

    def __post_init__(self) -> None:
        for name in ("budget_tokens", "effort_budget_tokens"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value <= 0
            ):
                raise ValueError("Reasoning budget must be a positive integer.")
        if self.budget_tokens is not None and self.control is not ReasoningControl.ON:
            raise ValueError("A reasoning budget requires reasoning control to be on.")

    @classmethod
    def provider_default(cls) -> ReasoningPolicy:
        """Leave reasoning computation to the provider."""

        return cls()

    @classmethod
    def off(cls) -> ReasoningPolicy:
        """Explicitly disable reasoning computation and output."""

        return cls(control=ReasoningControl.OFF)

    @classmethod
    def adaptive(cls) -> ReasoningPolicy:
        """Ask the model itself to choose how much to think."""

        return cls(control=ReasoningControl.ADAPTIVE)

    @classmethod
    def on(
        cls,
        *,
        effort: ReasoningEffort | None = None,
        budget_tokens: int | None = None,
    ) -> ReasoningPolicy:
        """Explicitly enable reasoning with optional client controls."""

        return cls(
            control=ReasoningControl.ON,
            effort=effort,
            budget_tokens=budget_tokens,
        )

    @property
    def output_enabled(self) -> bool:
        """Return whether provider reasoning may be exposed to the client."""

        return self.control is not ReasoningControl.OFF

    @property
    def requests_reasoning(self) -> bool:
        """Return whether the request explicitly asks the provider to reason.

        ``ADAPTIVE`` deliberately answers ``False``: it names no effort and no
        budget, so there is nothing for a generic encoder to send. Encoders
        that understand adaptive check ``control`` directly.
        """

        return self.control is not ReasoningControl.OFF and (
            self.control is ReasoningControl.ON
            or self.effort is not None
            or self.budget_tokens is not None
        )

    @property
    def numeric_budget_tokens(self) -> int | None:
        """Express this intent as an exact or FCC-mapped numeric budget.

        Resolution order, worst source last:

        1. the caller's own ``budget_tokens``;
        2. ``effort_budget_tokens`` -- this effort priced against the routed
           model's real output allowance (``application.reasoning_budget``);
        3. the flat ``_EFFORT_BUDGET_TOKENS`` table, reached only when nothing
           publishes an output allowance for this model.
        """

        if self.control is ReasoningControl.OFF:
            return None
        if self.budget_tokens is not None:
            return self.budget_tokens
        if self.effort_budget_tokens is not None:
            return self.effort_budget_tokens
        if self.effort is None:
            return None
        return self.effort.budget_tokens


# Declaration order is the documented ordering:
# minimal < low < medium < high < xhigh < max.
EFFORT_ORDER: tuple[ReasoningEffort, ...] = tuple(ReasoningEffort)
EFFORT_RANK: dict[ReasoningEffort, int] = {
    effort: rank for rank, effort in enumerate(EFFORT_ORDER)
}


def nearest_effort(
    requested: ReasoningEffort, supported: frozenset[ReasoningEffort]
) -> ReasoningEffort:
    """Return the closest supported effort at or below ``requested``.

    A request below everything on offer clamps *up* to the lowest supported
    effort: the caller asked for reasoning, and only an explicit OFF may take
    it away (WORKING-NOTES 54). ``supported`` must be non-empty.
    """

    at_or_below = [
        effort for effort in supported if EFFORT_RANK[effort] <= EFFORT_RANK[requested]
    ]
    if at_or_below:
        return max(at_or_below, key=lambda effort: EFFORT_RANK[effort])
    return min(supported, key=lambda effort: EFFORT_RANK[effort])


def effort_intersection(
    first: frozenset[ReasoningEffort] | None,
    second: frozenset[ReasoningEffort] | None,
) -> frozenset[ReasoningEffort] | None:
    """Intersect two effort vocabularies, treating ``None`` as "no opinion".

    Unknown never adds a restriction, so ``None`` on either side yields the
    other side untouched, and ``None`` on both stays unknown. An intersection
    that comes out genuinely empty is returned as the empty set -- "these two
    vocabularies share nothing" is a stated fact, not silence, and the caller
    has to decide what to do about it.
    """

    if first is None:
        return second
    if second is None:
        return first
    return first & second


class ReasoningDialectOrigin(StrEnum):
    """Where a :class:`ReasoningDialect` came from.

    Provenance, not confidence. A declared dialect is a claim someone probed
    and wrote down; the default is the OpenAI Chat Completions standard, which
    an OpenAI-compatible host either reads or ignores; a learned dialect is the
    host's own 400 overruling both.
    """

    DEFAULT = "default"
    """The OpenAI Chat Completions standard field, assumed of every host."""

    DECLARED = "declared"
    """This profile states what its gateway was measured parsing."""

    LEARNED = "learned"
    """Narrowed by a rejection the host itself sent."""


@dataclass(frozen=True, slots=True)
class ReasoningDialect:
    """Which reasoning fields one HOST parses for one request.

    Deliberately not a model capability. A model's knobs (can it reason, does
    it have an effort scale, a toggle, a budget) and the fields the gateway in
    front of it will actually read are two independent facts, and a control may
    only be emitted when both agree. Command Code parses ``reasoning_effort``
    and nothing else; the model behind it may have only an on/off switch. One
    fact alone cannot decide the wire.

    Every field defaults to "this host has no such field", so a partially
    described dialect never claims a channel it does not have. ``None`` for
    :attr:`effort_values` means there is no effort field at all -- distinct
    from an empty frozenset, which no encoder produces.
    """

    effort_values: frozenset[ReasoningEffort] | None = None
    """The effort words this host accepts, or ``None`` for no effort field."""

    toggle: bool = False
    """Whether an on/off channel exists (enabled_value, thinking object, flag)."""

    budget: bool = False
    """Whether a numeric thinking-budget field exists."""

    off: bool = False
    """Whether OFF can be spelled at all (disabled_value, thinking disabled)."""

    adaptive: bool = False
    """Whether the host has a channel for "let the model decide" (Anthropic)."""

    effort_field: str = ""
    """Wire name of the effort field, for adaptation messages only."""

    toggle_field: str = ""
    """Wire name of the on/off field, for adaptation messages only."""

    budget_field: str = ""
    """Wire name of the budget field, for adaptation messages only."""

    origin: ReasoningDialectOrigin = ReasoningDialectOrigin.DECLARED
    """Where this dialect came from: the OpenAI standard, a profile's own
    declaration, or a 400 the host itself answered."""

    learned_rejections: tuple[tuple[str, str], ...] = ()
    """``(field, ISO date)`` for each field this host refused, newest last."""


def narrow_dialect_by_rejections(
    dialect: ReasoningDialect, rejections: Mapping[str, str]
) -> ReasoningDialect:
    """Remove from a dialect every channel whose wire field was refused.

    A learned rejection outranks a declaration: the profile is a claim about
    the gateway, a 400 is the gateway itself, and where they disagree the
    gateway is right. Narrowing only ever removes -- it can never invent a
    channel -- so it composes with the manager's own gateway narrowing in
    either order.

    A dotted field name (``reasoning.effort``) is matched on its first segment
    too, because that is the key a body actually carries and therefore the key
    a strip removes.
    """

    if not rejections:
        return dialect

    def refers_to(field: str, declared: str) -> bool:
        if not declared:
            return False
        return field in (declared, declared.split(".", 1)[0])

    effort_values = dialect.effort_values
    effort_field = dialect.effort_field
    toggle = dialect.toggle
    off = dialect.off
    toggle_field = dialect.toggle_field
    budget = dialect.budget
    budget_field = dialect.budget_field

    for field in rejections:
        if refers_to(field, effort_field):
            effort_values = None
            effort_field = ""
        if refers_to(field, toggle_field):
            toggle = False
            off = False
            toggle_field = ""
        if refers_to(field, budget_field):
            budget = False
            budget_field = ""

    return replace(
        dialect,
        effort_values=effort_values,
        effort_field=effort_field,
        toggle=toggle,
        off=off,
        toggle_field=toggle_field,
        budget=budget,
        budget_field=budget_field,
        origin=ReasoningDialectOrigin.LEARNED,
        learned_rejections=tuple(sorted(rejections.items())),
    )


class ReasoningAdaptationKind(StrEnum):
    """What per-model capability gating did to a requested reasoning policy.

    ``message`` on :class:`ReasoningAdaptation` carries the operator-facing
    warning; this enum is the programmatic signal a UI can style on.

    :attr:`DROPPED` and :attr:`NOTHING_SENT` were one value until 6.6.0 and the
    conflation cost a live false positive: ``DROPPED`` means the requested
    *level* was discarded while thinking was still switched on through a field
    the host does have, so a body with no reasoning key contradicts it;
    ``NOTHING_SENT`` means no reasoning instruction of any kind left the proxy
    and the model's own default applies, so a body with no reasoning key is the
    outcome, not a fault. A dashboard that cannot tell them apart badges the
    correct case as a defect, which is exactly what the shared value did.
    """

    UNCHANGED = "unchanged"
    SUBSTITUTED = "substituted"
    CLAMPED = "clamped"
    DROPPED = "dropped"
    NOTHING_SENT = "nothing_sent"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True, slots=True)
class ReasoningAdaptation:
    """What per-model gating changed about one reasoning request.

    Returned alongside the adapted policy so the request log and admin UI can
    surface the warning that gating currently only emits to the server log.
    ``message`` is ``None`` exactly when ``kind`` is ``UNCHANGED``.
    """

    kind: ReasoningAdaptationKind
    message: str | None


# Severity order for merging several sub-adaptations into one descriptor. A
# request that is both substituted and clamped is reported at the worse of the
# two, so the admin UI never under-represents what happened to it.
_ADAPTATION_SEVERITY: dict[ReasoningAdaptationKind, int] = {
    ReasoningAdaptationKind.UNCHANGED: 0,
    ReasoningAdaptationKind.SUBSTITUTED: 1,
    ReasoningAdaptationKind.CLAMPED: 2,
    ReasoningAdaptationKind.DROPPED: 3,
    # Sending nothing at all is a larger departure from the request than
    # sending a different level, and a smaller one than the host refusing the
    # field outright, so it sits between them.
    ReasoningAdaptationKind.NOTHING_SENT: 4,
    ReasoningAdaptationKind.SUPPRESSED: 5,
}


def combine_reasoning_adaptations(
    *adaptations: ReasoningAdaptation,
) -> ReasoningAdaptation:
    """Collapse several adaptations of one request into a single descriptor.

    One request can be adapted twice in two different places -- capability
    gating at routing time, then budget reconciliation once ``max_tokens`` is
    final -- and the request log has room for one verdict. Every message is
    kept, joined in the order they happened, under the most severe ``kind``.
    """

    stated = [
        adaptation
        for adaptation in adaptations
        if adaptation.kind is not ReasoningAdaptationKind.UNCHANGED
    ]
    if not stated:
        return ReasoningAdaptation(ReasoningAdaptationKind.UNCHANGED, None)
    messages = [adaptation.message for adaptation in stated if adaptation.message]
    kind = max(stated, key=lambda a: _ADAPTATION_SEVERITY[a.kind]).kind
    return ReasoningAdaptation(kind, " ".join(messages) or None)


DEFAULT_REASONING_POLICY = ReasoningPolicy.provider_default()
