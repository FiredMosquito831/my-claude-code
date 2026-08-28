"""Provider-neutral reasoning intent."""

from dataclasses import dataclass
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


_EFFORT_BUDGET_TOKENS = {
    ReasoningEffort.MINIMAL: 1_024,
    ReasoningEffort.LOW: 1_024,
    ReasoningEffort.MEDIUM: 1_024,
    ReasoningEffort.HIGH: 2_048,
    ReasoningEffort.XHIGH: 4_096,
    ReasoningEffort.MAX: 8_192,
}


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

    def __post_init__(self) -> None:
        if self.budget_tokens is not None and (
            not isinstance(self.budget_tokens, int)
            or isinstance(self.budget_tokens, bool)
            or self.budget_tokens <= 0
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
        """Express this intent as an exact or FCC-mapped numeric budget."""

        if self.control is ReasoningControl.OFF:
            return None
        if self.budget_tokens is not None:
            return self.budget_tokens
        if self.effort is None:
            return None
        return self.effort.budget_tokens


class ReasoningAdaptationKind(StrEnum):
    """What per-model capability gating did to a requested reasoning policy.

    ``message`` on :class:`ReasoningAdaptation` carries the operator-facing
    warning; this enum is the programmatic signal a UI can style on.
    """

    UNCHANGED = "unchanged"
    SUBSTITUTED = "substituted"
    CLAMPED = "clamped"
    DROPPED = "dropped"
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


DEFAULT_REASONING_POLICY = ReasoningPolicy.provider_default()
