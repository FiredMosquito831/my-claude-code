"""What one provider instance has learned from its host's own rejections.

Two tables, one lifetime. Both are written only from something the upstream
itself said, both only ever narrow what MCC will ask for next time, and both
are deliberately **per process and per provider instance**: a cap or a refusal
is a fact about one deployment behind one credential, and nothing here is
durable enough to be worth persisting across a restart (see ``docs/USAGE.md``,
"learned from the host's own rejection").
"""

from dataclasses import dataclass, field
from datetime import date


@dataclass(slots=True)
class RecoveryMemory:
    """Per-model caps and refused reasoning fields learned from upstream 400s."""

    #: Bare model id -> the smallest output-token maximum this host has stated.
    output_caps: dict[str, int] = field(default_factory=dict)

    #: Bare model id -> {refused reasoning field: ISO date it was learned}.
    #: The date is what the Models page shows next to "learned from the host's
    #: own rejection".
    rejected_reasoning_fields: dict[str, dict[str, str]] = field(default_factory=dict)

    def cap_for(self, model: str) -> int | None:
        """Return the learned output-token cap for a model, if one is known."""
        return self.output_caps.get(model)

    def learn_cap(self, model: str, cap: int) -> int:
        """Record a stated cap, keeping the smallest ever seen, and return it.

        Monotonically narrowing: a host that states a lower maximum on a later
        request has revised its own answer downward, and a higher one does not
        contradict the number already proven to work.
        """
        previous = self.output_caps.get(model)
        cap = cap if previous is None else min(previous, cap)
        self.output_caps[model] = cap
        return cap

    def rejections_for(self, model: str) -> dict[str, str] | None:
        """Return the refused reasoning fields for a model, if any."""
        return self.rejected_reasoning_fields.get(model)

    def remember_rejection(self, model: str, rejected_field: str) -> bool:
        """Record a proven refusal; ``False`` when it was already known.

        Reached only after the stripped body was actually accepted, so the
        strip is what fixed it. A rejection is an inference, not a stated fact
        the way a cap is, and writing it before the retry succeeded would teach
        the process to stop asking for thinking on a model that was never the
        problem.
        """
        rejections = self.rejected_reasoning_fields.setdefault(model, {})
        if rejected_field in rejections:
            return False
        rejections[rejected_field] = date.today().isoformat()
        return True
