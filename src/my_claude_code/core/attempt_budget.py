"""The running model attempt's first-token deadline, published down the stack.

The executor gives every model attempt an equal share of the request budget in
which to produce a first token, so the models behind it in the fallback chain
still get a turn. A provider that rotates across several credentials has to
subdivide that share again: otherwise the first credential it tries can spend
the whole attempt on a stalled connection and the keys behind it are never
tried, which makes a configured rotation pool look like it is being ignored.

Widening ``stream_response`` with a deadline argument would touch every
provider for a value exactly one of them reads, so the executor publishes it
in a context variable for the duration of one attempt instead. The flow is
parent-to-child only -- the executor writes, the rotating provider reads --
so a plain immutable ``ContextVar`` is enough: a child task copies the context
after the write and therefore sees it. (Credential *attribution* runs the
other way, child-to-parent, which is why it needs a mutable slot.)

Absent an executor -- direct provider use in tests, the token-count path --
the value is ``None`` and no per-credential bound applies.
"""

from contextvars import ContextVar

_ATTEMPT_DEADLINE: ContextVar[float | None] = ContextVar(
    "fcc_attempt_deadline", default=None
)


def set_attempt_deadline(deadline: float | None) -> None:
    """Publish the monotonic first-token deadline for the attempt starting now."""
    _ATTEMPT_DEADLINE.set(deadline)


def current_attempt_deadline() -> float | None:
    """The running attempt's first-token deadline, or ``None`` when unbounded."""
    return _ATTEMPT_DEADLINE.get()
