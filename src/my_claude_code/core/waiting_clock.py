"""Seconds this request spent waiting rather than waiting *on a model*.

A backoff sleep and a limiter block are MCC's own time, not the upstream's.
The first-token and stall deadlines ask one question -- "is this model
working?" -- and a deadline that expires while MCC is asleep answers a
different one. So the seconds are counted here, credited by the provider
layer where they are actually spent, and handed back by the executor's chunk
wait, which re-arms itself for exactly as long as it was asleep.

Direction matters: this is providers telling the executor what they spent.
Nothing here tells a provider or a credential pool how long it may take --
that is the per-credential deadline subdivision removed in 6.14.0/6.16.0
(``core.attempt_budget``, which must not come back), and the pool still holds
no clock of its own.

It mirrors :mod:`my_claude_code.core.upstream_ladder` exactly, and the shape
is load-bearing rather than stylistic: a **mutable holder** in a
``ContextVar``, not a bare float. The executor waits for the next chunk in a
task of its own (``asyncio.ensure_future``), and a task gets a *copy* of the
context -- so a provider writing a new float into the variable would write it
into the copy, and the executor would never see a single credited second.
Copying the context copies the reference to this holder, so mutating it is
visible on both sides.

Every function is safe outside a request: with no holder installed, crediting
is a no-op and the total reads zero, so a provider exercised directly by a
unit test, by model discovery or by token counting needs no special handling.
"""

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(slots=True)
class WaitingClock:
    """How long this request has spent asleep, so far."""

    seconds: float = 0.0


_WAITED: ContextVar[WaitingClock | None] = ContextVar("fcc_waited", default=None)


def install_waiting_clock() -> WaitingClock:
    """Start this request's waiting accumulator at zero.

    Installed unconditionally per request, unlike the request log's traces:
    what a deadline measures must not depend on whether recording is on.
    """
    clock = WaitingClock()
    _WAITED.set(clock)
    return clock


def credit_waiting(seconds: float) -> None:
    """Add seconds MCC has *already* spent asleep or blocked.

    Called after the sleep, never before: the accumulator describes seconds
    actually spent, so a deadline can only ever be extended by time that
    provably passed with no upstream listening.
    """
    clock = _WAITED.get()
    if clock is None or seconds <= 0:
        return
    clock.seconds += seconds


def waited_seconds() -> float:
    """Total seconds credited to this request so far; 0 outside one."""
    clock = _WAITED.get()
    return 0.0 if clock is None else clock.seconds
