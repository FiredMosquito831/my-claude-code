"""A stopped search never leaves a half answer behind (7.91.2).

Five readouts degrade to "nothing measured" when their statement fails, so an
unreadable value never takes the Requests page down: the recovery aggregate,
the upstream statuses, the server-session history, the cancelled and the
no-answer breakdowns. Since 7.91.2 the page aborts every superseded load, and
the server interrupts the statements of an answer nobody waits for (7.91.1).
An interrupt is not an unreadable value: degrading on it would log a fault on
every typed prefix and could leave the half answer in the store's five-second
cache, where the next request with the same search would be handed it.

So an interrupt is re-raised at each of the five, and every other failure
degrades exactly as before.
"""

import sqlite3
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from my_claude_code.core.request_log import RequestLogStore, raise_if_interrupted
from tests.support.search_log import build_search_log


class _FailOn:
    """A connection whose statements containing ``marker`` fail with ``error``."""

    def __init__(self, conn: sqlite3.Connection, marker: str, error: str) -> None:
        self._conn = conn
        self._marker = marker
        self._error = error

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:
        if self._marker in sql:
            raise sqlite3.OperationalError(self._error)
        return self._conn.execute(sql, *args)

    def __enter__(self) -> _FailOn:
        self._conn.__enter__()
        return self

    def __exit__(self, *exc: Any) -> Any:
        return self._conn.__exit__(*exc)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


@pytest.fixture
def store(tmp_path) -> Iterator[RequestLogStore]:
    built, _times = build_search_log(tmp_path / "requests.db", rows=60)
    yield built
    built.close()


SEARCH: dict[str, Any] = {"q": "x", "local": "hide"}
SITES: tuple[tuple[str, str, Callable[[RequestLogStore], Any]], ...] = (
    ("recovery", "$.salvages", lambda s: s.stats(**SEARCH)),
    ("upstream statuses", "$.ladder.tries", lambda s: s.stats(**SEARCH)),
    ("session history", "FROM server_sessions", lambda s: s.restart_boundaries()),
    ("cancelled", "GROUP BY reason", lambda s: s.cancelled_breakdown(**SEARCH)),
    ("no answer", "GROUP BY reason", lambda s: s.no_answer_breakdown(**SEARCH)),
)


def _failing(
    store: RequestLogStore, monkeypatch: pytest.MonkeyPatch, marker: str, error: str
) -> None:
    real = store._connect

    def connect() -> Any:
        return _FailOn(real(), marker, error)

    monkeypatch.setattr(store, "_connect", connect)
    with store._stats_lock:
        store._stats_cache.clear()


@pytest.mark.parametrize(("name", "marker", "read"), SITES, ids=[s[0] for s in SITES])
def test_an_interrupted_readout_raises_and_caches_nothing(
    store, monkeypatch, name, marker, read
) -> None:
    _failing(store, monkeypatch, marker, "interrupted")

    with pytest.raises(sqlite3.OperationalError, match=r"^interrupted$"):
        read(store)
    with store._stats_lock:
        assert dict(store._stats_cache) == {}, name


@pytest.mark.parametrize(("name", "marker", "read"), SITES, ids=[s[0] for s in SITES])
def test_any_other_failure_still_degrades_as_before(
    store, monkeypatch, name, marker, read
) -> None:
    _failing(store, monkeypatch, marker, "database disk image is malformed")

    answer = read(store)

    assert answer is not None, name


def test_only_an_interrupt_is_re_raised() -> None:
    with pytest.raises(sqlite3.OperationalError):
        raise_if_interrupted(sqlite3.OperationalError("interrupted"))
    raise_if_interrupted(sqlite3.OperationalError("database is locked"))
    raise_if_interrupted(sqlite3.DatabaseError("interrupted"))
