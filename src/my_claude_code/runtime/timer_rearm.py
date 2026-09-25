"""Re-arming a settings-driven background loop when the dashboard saves.

The discovery sweep, the proxy checker and the proxy feed refresh are one
shape: a task that reads its switch and interval, sleeps the interval, runs one
pass, and reads them again. Reading them *again* is why a saved value was
always going to be used eventually -- but only after the sleep already in
progress ran out, which for an hourly sweep switched to five minutes is the
better part of an hour, and for a loop that was off is never: a stopped task
reads nothing.

``rearm`` closes both gaps without touching work that is under way:

* a loop that is **asleep** is cancelled and started again, so the new interval
  counts from the save;
* a loop that is **in the middle of a pass** is left alone -- cutting a sweep
  short is not what the operator asked for, and the loop reads both fields
  again before its next sleep;
* a loop that is **not running** is started if the new values switch it on.
"""

import asyncio


class RearmableTimer:
    """The re-arm half the three settings-driven loops share."""

    _task: asyncio.Task[None] | None
    _next_tick_at: float | None
    # Set only while the loop task itself is inside a pass. A pass started
    # from anywhere else (the Fetch button's job slot, a test) does not hold a
    # sleeping loop awake, so it does not stop a re-arm either. Written by the
    # loop task alone: a cancelled sleeper never reaches the ``finally`` that
    # clears it, so it cannot clear the flag of the task that replaced it.
    _loop_in_tick: bool = False

    def start(self) -> bool:  # pragma: no cover - every subclass defines it
        raise NotImplementedError

    async def tick(self) -> object:  # pragma: no cover - every subclass defines it
        raise NotImplementedError

    async def _loop_tick(self) -> None:
        """Run one pass from the loop, marking it as under way."""

        self._loop_in_tick = True
        try:
            await self.tick()
        finally:
            self._loop_in_tick = False

    def rearm(self) -> bool:
        """Adopt the switch and interval now. Returns whether a loop runs."""

        task = self._task
        if task is not None and not task.done():
            if self._loop_in_tick:
                return True
            task.cancel()
            self._task = None
            self._next_tick_at = None
        return self.start()


__all__ = ["RearmableTimer"]
