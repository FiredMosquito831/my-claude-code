"""One wall-clock deadline, computed once, shared by every stage of a stop.

Before 6.41.0 a stop had four waits (uvicorn's connection drain, the response
cleanup in ``api.response_streams``, ``ProviderRuntimeManager.close``'s drain,
and the ASGI lifespan shutdown) and only the *first* of them was bounded. A
request against a silent upstream therefore meant a server that never exited:
uvicorn cancelled the request task at its bound, the shielded cleanup loop
absorbed that cancellation, the lease was never released, and the lifespan
waited on ``drained`` forever.

This module is the single answer to "how long may a stop take". At the instant
a stop is requested the operator's ``SERVER_GRACEFUL_SHUTDOWN_SECONDS`` becomes
one absolute deadline; every later stage reads the time it has *left* against
that same clock rather than starting a fresh budget of its own. A small fixed
teardown margin sits past the deadline for the forced close, and a watchdog
thread hard-exits the process one beat after that so a stage that ignores its
bound entirely still cannot hang the process.

Deliberately in ``core``: the supervisor (``cli``), the ASGI gate and lifespan
(``runtime``), the provider drain (``runtime``) and the response cleanup
(``api``) all read it, and ``core`` is the only package all four may import.
It holds no configuration of its own -- the budget is handed in by the
supervisor, which is the one component that owns Settings.
"""

import os
import threading
import time
from collections.abc import Callable
from contextlib import suppress

# The operator's budget is clamped to the same range the Settings limit uses
# (config/limits.py: server_graceful_shutdown_seconds). Clamping here as well
# keeps the invariant local to the deadline: whatever reaches ``request`` is a
# usable number of seconds, never 0 (an immediate, no-drain stop) and never a
# value so large it is a hang rather than a budget.
MIN_STOP_BUDGET_SECONDS = 1.0
MAX_STOP_BUDGET_SECONDS = 600.0

# Fixed teardown margin past the operator's budget. Everything after uvicorn's
# connection drain -- abandoning a blocked response cleanup, force-closing the
# provider generations, answering the lifespan -- shares this window, so the
# ordered stop finishes within ``budget + margin`` however the time was spent
# before it.
STOP_TEARDOWN_MARGIN_SECONDS = 3.0

# Never hand a teardown stage a zero or negative timeout: ``wait_for(0)`` fails
# before the awaitable is even scheduled, which turns "close quickly" into
# "close nothing". A stage that starts inside the margin gets at least this.
MIN_TEARDOWN_WAIT_SECONDS = 0.5

# How long after the hard deadline the watchdog waits before ``os._exit``. The
# ordered path is always given the chance to win first; this only fires when a
# stage ignored its bound outright.
HARD_EXIT_GRACE_SECONDS = 1.0

# What a refused request is told to wait before retrying. Short on purpose: the
# harness's own retry is what finds the restarted server, and the restart is
# measured in seconds.
SHUTDOWN_RETRY_AFTER_SECONDS = 5

# Exit status used by the watchdog. Distinct from 0 (clean) and 1 (a refused
# REPLACE_PROCESS) so a supervisor can tell the three apart in a log.
HARD_EXIT_STATUS = 3


def clamp_stop_budget(budget_seconds: float) -> float:
    """Return a usable stop budget in seconds."""

    try:
        value = float(budget_seconds)
    except TypeError, ValueError:
        return MIN_STOP_BUDGET_SECONDS
    if value != value:  # NaN
        return MIN_STOP_BUDGET_SECONDS
    return max(MIN_STOP_BUDGET_SECONDS, min(value, MAX_STOP_BUDGET_SECONDS))


class StopDeadline:
    """The process's single "stop requested at" clock.

    ``request`` is idempotent by design: the first caller fixes the deadline and
    a later, weaker or repeated request cannot move it. That is what makes a new
    request during the drain unable to extend the drain (C5) -- the deadline is
    a property of the stop, not of the work still running under it.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._budget: float | None = None
        self._deadline: float | None = None
        self._hard_deadline: float | None = None
        self._watchdog: threading.Thread | None = None
        self._disarm = threading.Event()

    # -- state -------------------------------------------------------------

    @property
    def requested(self) -> bool:
        """Whether a stop has been requested and not since cleared."""

        return self._deadline is not None

    @property
    def budget(self) -> float | None:
        """The clamped budget this stop was given, or ``None`` before one."""

        return self._budget

    def request(self, budget_seconds: float) -> float:
        """Start the stop clock (first call wins) and return the budget used."""

        with self._lock:
            if self._budget is not None:
                return self._budget
            budget = clamp_stop_budget(budget_seconds)
            now = time.monotonic()
            self._budget = budget
            self._deadline = now + budget
            self._hard_deadline = self._deadline + STOP_TEARDOWN_MARGIN_SECONDS
            return budget

    def clear(self) -> None:
        """Forget the stop. Used by a RELOAD's next generation, and by tests."""

        self.disarm_hard_exit()
        with self._lock:
            self._budget = None
            self._deadline = None
            self._hard_deadline = None

    # -- budgets -----------------------------------------------------------

    def remaining(self) -> float:
        """Seconds left before the operator's budget is spent (never negative)."""

        deadline = self._deadline
        if deadline is None:
            return 0.0
        return max(0.0, deadline - time.monotonic())

    def teardown_remaining(self) -> float:
        """Seconds a teardown stage may take, against the shared hard deadline.

        Every stage after uvicorn's connection drain uses this, so their bounds
        compose: three nested stages cannot each spend the margin, because they
        are all measuring to the same absolute instant.
        """

        hard = self._hard_deadline
        if hard is None:
            return 0.0
        return max(MIN_TEARDOWN_WAIT_SECONDS, hard - time.monotonic())

    def expired(self) -> bool:
        """Whether the operator's budget is already spent."""

        return self.requested and self.remaining() <= 0.0

    # -- watchdog ----------------------------------------------------------

    def arm_hard_exit(self, *, on_exit: Callable[[], None] | None = None) -> None:
        """Guarantee the process leaves, even if a stage ignores its bound.

        Armed only for a terminal stop (STOP / REPLACE_PROCESS). A RELOAD must
        keep the process alive when its drain overruns -- that is C8 -- so the
        supervisor never arms this for one.
        """

        with self._lock:
            if self._hard_deadline is None or self._watchdog is not None:
                return
            self._disarm.clear()
            deadline = self._hard_deadline + HARD_EXIT_GRACE_SECONDS
            thread = threading.Thread(
                target=self._hard_exit_after,
                args=(deadline, on_exit),
                name="mcc-stop-watchdog",
                daemon=True,
            )
            self._watchdog = thread
        thread.start()

    def disarm_hard_exit(self) -> None:
        """Stand the watchdog down; the ordered stop path won."""

        self._disarm.set()
        with self._lock:
            self._watchdog = None

    def _hard_exit_after(
        self, deadline: float, on_exit: Callable[[], None] | None
    ) -> None:
        while True:
            wait = deadline - time.monotonic()
            if wait <= 0:
                break
            if self._disarm.wait(min(wait, 0.5)):
                return
        if self._disarm.is_set():
            return
        if on_exit is not None:
            # Never block the exit on a last-words logger that is itself stuck.
            with suppress(Exception):
                on_exit()
        # os._exit, not sys.exit: the point of this thread is that the ordered
        # path is already stuck, so anything that runs teardown could stick too.
        os._exit(HARD_EXIT_STATUS)


_STOP_DEADLINE = StopDeadline()


def stop_deadline() -> StopDeadline:
    """The process-wide stop deadline."""

    return _STOP_DEADLINE
