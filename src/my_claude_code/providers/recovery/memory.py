"""What one provider has learned from its host's own rejections.

Three tables, one shape. All are written only from something the upstream
itself said, and all only ever narrow what MCC will ask for next time.

Since 6.52.0 they are also **durable**. They used to be per process and per
provider instance on purpose -- a restart or a config apply forgot everything,
so a host that was briefly misconfigured healed by itself. That property is not
given up, only moved onto a clock: the store behind :attr:`sink`
(:mod:`my_claude_code.providers.recovery.store`) writes each fact to
``~/.mcc/learned_facts.json`` with the time it was last confirmed, and stops
applying it once its evidence class has expired. Self-healing on a TTL rather
than on a restart, and visible on the Models page either way.

A bare ``RecoveryMemory()`` with no sink is still exactly what it always was:
a per-instance memory that persists nothing.
"""

from dataclasses import dataclass, field
from datetime import date

from .facts import (
    FACT_OUTPUT_CAP,
    FACT_REASONING_FIELD_REJECTED,
    FACT_STREAM_USAGE_UNSUPPORTED,
    FactSink,
)


@dataclass(slots=True)
class RecoveryMemory:
    """Per-model caps and refused fields learned from upstream 400s."""

    #: Bare model id -> the smallest output-token maximum this host has stated.
    output_caps: dict[str, int] = field(default_factory=dict)

    #: Bare model id -> {refused reasoning field: ISO date it was learned}.
    #: The date is what the Models page shows next to "learned from the host's
    #: own rejection".
    rejected_reasoning_fields: dict[str, dict[str, str]] = field(default_factory=dict)

    #: Bare model ids whose host answered ``stream_options.include_usage``
    #: with a 400. Before 6.52.0 nothing recorded this at all, so every single
    #: request to such a host paid a failed try and a retry -- not once per
    #: process, once per request.
    stream_usage_unsupported: set[str] = field(default_factory=set)

    #: Where a newly learned fact is written through to, or ``None`` for a
    #: memory that persists nothing.
    sink: FactSink | None = None

    def cap_for(self, model: str) -> int | None:
        """Return the learned output-token cap for a model, if one is known."""
        return self.output_caps.get(model)

    def learn_cap(self, model: str, cap: int, *, evidence: str = "") -> int:
        """Record a stated cap, keeping the smallest ever seen, and return it.

        Monotonically narrowing: a host that states a lower maximum on a later
        request has revised its own answer downward, and a higher one does not
        contradict the number already proven to work.
        """
        previous = self.output_caps.get(model)
        cap = cap if previous is None else min(previous, cap)
        self.output_caps[model] = cap
        # Written through even when the number did not move: a re-statement is
        # the host confirming the cap today, which is what keeps it out of the
        # stale bucket.
        if self.sink is not None:
            self.sink(FACT_OUTPUT_CAP, model, cap, "", evidence)
        return cap

    def rejections_for(self, model: str) -> dict[str, str] | None:
        """Return the refused reasoning fields for a model, if any."""
        return self.rejected_reasoning_fields.get(model)

    def remember_rejection(
        self, model: str, rejected_field: str, *, evidence: str = ""
    ) -> bool:
        """Record a proven refusal; ``False`` when it was already known.

        Reached only after the stripped body was actually accepted, so the
        strip is what fixed it. A rejection is an inference, not a stated fact
        the way a cap is, and writing it before the retry succeeded would teach
        the process to stop asking for thinking on a model that was never the
        problem.
        """
        rejections = self.rejected_reasoning_fields.setdefault(model, {})
        already_known = rejected_field in rejections
        rejections[rejected_field] = date.today().isoformat()
        if self.sink is not None:
            self.sink(
                FACT_REASONING_FIELD_REJECTED, model, True, rejected_field, evidence
            )
        return not already_known

    def stream_usage_refused(self, model: str) -> bool:
        """Whether this host has been proven to reject streamed usage here."""
        return model in self.stream_usage_unsupported

    def remember_stream_usage_refusal(self, model: str, *, evidence: str = "") -> bool:
        """Record that streamed usage was refused; ``False`` when already known.

        Learned the same way a reasoning refusal is -- only once the rewritten
        body was accepted -- because a 400 that merely mentioned the option is
        not proof that removing it is what fixed the request.
        """
        already_known = model in self.stream_usage_unsupported
        self.stream_usage_unsupported.add(model)
        if self.sink is not None:
            self.sink(FACT_STREAM_USAGE_UNSUPPORTED, model, True, "", evidence)
        return not already_known
