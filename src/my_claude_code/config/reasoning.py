"""User-configurable reasoning policy values."""

from enum import StrEnum


class ReasoningPreference(StrEnum):
    """Configuration choice applied before provider translation."""

    INHERIT = "inherit"
    OFF = "off"
    CLIENT = "client"
    ADAPTIVE = "adaptive"
    # The rungs, lowest first. ``minimal`` is the mirror of
    # ``core.reasoning.ReasoningEffort.MINIMAL``, which 74 catalogued models
    # publish and every effort encoder already spells; without it the
    # configuration vocabulary could not name a rung the wire has always
    # been able to carry. ``ultra`` is deliberately NOT here: it is a wire
    # word a host dialect produces from ``max``, never a stored choice.
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


ROOT_REASONING_PREFERENCES = tuple(
    preference
    for preference in ReasoningPreference
    if preference is not ReasoningPreference.INHERIT
)
ROUTE_REASONING_PREFERENCES = tuple(ReasoningPreference)
