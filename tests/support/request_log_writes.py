"""Waiting for the request log's writer thread: on what it did, not on a clock.

The store writes on a thread of its own. Two shapes of wait were flaky under
a loaded machine, and both are replaced here by a wait on the writer itself:

* ``store.close()`` waits at most ``max(10 s, backlog x 10 ms)`` and returns
  with the writer still running when that runs out ("Request log writer still
  draining at shutdown; 275 records unwritten" -- the v7.109.0 dictionary
  refresh failure; the attempt export read 0 rows the same way). A test that
  closes a store in order to read its rows from another one needs every row
  committed: :func:`close_and_drain` closes and then waits for the thread to
  END, which is the moment the last batch is committed.
* a 5 s poll for a row the writer has queued (the vision describe tests, eight
  hits): :class:`FlushWatch` records each id the moment the batch holding it
  has been committed, and :meth:`FlushWatch.wait` waits for that.

Both keep a bound, but only as a hang guard: it is far beyond any wait a
working writer needs, so reaching it means the row was never written.
"""

import threading
from typing import Any

import pytest

from my_claude_code.core.request_log import RequestLogStore

#: A hang guard, not a timing: a healthy writer commits within a second.
HANG_BOUND_SECONDS = 120.0


def close_and_drain(
    store: RequestLogStore, *, seconds: float = HANG_BOUND_SECONDS
) -> None:
    """Close ``store`` and return once its writer thread has ended."""

    store.close()
    writer = store._writer
    writer.join(seconds)
    assert not writer.is_alive(), (
        f"the request log writer was still draining {seconds} s after close()"
    )


class FlushWatch:
    """The ids every store in this test has committed, as they are committed."""

    def __init__(self) -> None:
        self._committed: set[str] = set()
        self._changed = threading.Condition()

    def install(self, monkeypatch: pytest.MonkeyPatch) -> FlushWatch:
        real_flush = RequestLogStore._flush
        watch = self

        def flush_and_note(store: RequestLogStore, batch: list[Any], conn: Any) -> None:
            ids = [record.id for record in batch]
            real_flush(store, batch, conn)
            with watch._changed:
                watch._committed.update(ids)
                watch._changed.notify_all()

        monkeypatch.setattr(RequestLogStore, "_flush", flush_and_note)
        return self

    def wait(self, request_id: str, *, seconds: float = HANG_BOUND_SECONDS) -> None:
        """Return once the batch holding ``request_id`` has been committed."""

        with self._changed:
            committed = self._changed.wait_for(
                lambda: request_id in self._committed, timeout=seconds
            )
        assert committed, f"{request_id} was never written to the request log"
