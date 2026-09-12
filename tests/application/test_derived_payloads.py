"""Serving a derived payload from disk, and what a restart is allowed to cost.

The rule these hold: a payload is valid exactly as long as the data that
produced it has not changed, a stale payload is still served (marked) rather
than making the reader wait, and a cache hit is the same answer a fresh
computation would give.
"""

import threading
import time
from pathlib import Path
from typing import Any

from my_claude_code.application import derived_payloads
from my_claude_code.application.derived_payloads import (
    cached_payload,
    cost_breakdown_cache_key,
    cost_breakdown_entry_name,
    derived_cache,
)
from my_claude_code.core.derived_cache import DerivedCache
from my_claude_code.core.request_log import RequestLogStore, RequestRecord


def _record(request_id: str, **overrides: Any) -> RequestRecord:
    defaults: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "provider": "nvidia_nim",
        "resolved_model": "test-model",
        "status": "success",
        "tokens_in": 1,
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


def _settle(name: str) -> None:
    """Wait for the background refresh this module may have started."""

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        with derived_payloads._refresh_lock:
            if name not in derived_payloads._refreshing:
                return
        time.sleep(0.01)
    raise AssertionError(f"refresh of {name} never finished")


# ------------------------------------------------------------- the data mark


def test_the_mark_changes_when_one_request_is_logged(tmp_path: Path) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=1000)
    try:
        store.enqueue(_record("first"))
        store.close()
        before = store.data_mark()

        again = RequestLogStore(tmp_path / "requests.db", max_rows=1000)
        again.enqueue(_record("second"))
        again.close()

        assert again.data_mark() != before
    finally:
        store.close()


def test_the_mark_does_not_change_when_nothing_does(tmp_path: Path) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=1000)
    try:
        store.enqueue(_record("first"))
        store.close()

        assert store.data_mark() == store.data_mark()
        # And a second process reading the same file agrees, which is what
        # makes the mark usable across a restart at all.
        other = RequestLogStore(tmp_path / "requests.db", max_rows=1000)
        other.close()
        assert other.data_mark() == store.data_mark()
    finally:
        store.close()


def test_the_mark_notices_a_prune(tmp_path: Path) -> None:
    """``prune`` deletes from the front, so ``MAX(rowid)`` alone would not."""

    store = RequestLogStore(tmp_path / "requests.db", max_rows=1000)
    try:
        for index in range(5):
            store.enqueue(_record(f"r{index}"))
        store.close()
        before = store.data_mark()

        import sqlite3

        with sqlite3.connect(store.db_path) as conn:
            conn.execute("DELETE FROM requests WHERE id = 'r0'")

        assert store.data_mark() != before
    finally:
        store.close()


def test_an_unreadable_log_marks_as_unavailable(tmp_path: Path) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10)
    try:
        store.close()
        store.db_path.write_text("not a database", encoding="utf-8")

        assert store.data_mark() == "unavailable"
    finally:
        store.close()


# ------------------------------------------------------------- serving policy


def test_a_matching_key_is_served_without_recomputing(tmp_path: Path) -> None:
    cache = DerivedCache(tmp_path / "derived")
    calls: list[int] = []

    def compute() -> dict[str, Any]:
        calls.append(1)
        return {"total": 42}

    first = cached_payload("thing", key="k1", compute=compute, cache=cache)
    second = cached_payload("thing", key="k1", compute=compute, cache=cache)

    assert calls == [1]
    assert first["total"] == second["total"] == 42
    assert first["stale"] is False and second["stale"] is False
    assert second["computed_at"] == first["computed_at"]


def test_a_cache_hit_equals_a_fresh_computation(tmp_path: Path) -> None:
    """The equality oracle: what is stored is what the computation returned."""

    cache = DerivedCache(tmp_path / "derived")
    payload = {"totals": {"reported_usd": None, "priced": 3}, "by_day": [{"key": "x"}]}

    served = cached_payload("thing", key="k1", compute=lambda: payload, cache=cache)
    again = cached_payload("thing", key="k1", compute=lambda: payload, cache=cache)

    for answer in (served, again):
        assert {
            k: v for k, v in answer.items() if k not in {"stale", "computed_at"}
        } == (payload)


def test_a_changed_key_serves_the_stored_answer_marked_stale(tmp_path: Path) -> None:
    cache = DerivedCache(tmp_path / "derived")
    released = threading.Event()

    cached_payload("thing", key="k1", compute=lambda: {"total": 1}, cache=cache)

    def slow() -> dict[str, Any]:
        released.wait(10.0)
        return {"total": 2}

    answer = cached_payload("thing", key="k2", compute=slow, cache=cache)

    # Answered immediately, with the old numbers, saying so.
    assert answer["total"] == 1
    assert answer["stale"] is True
    released.set()
    _settle("thing")

    # And the refreshed answer is what the next request gets, not stale.
    fresh = cached_payload("thing", key="k2", compute=lambda: {"total": 2}, cache=cache)
    assert fresh["total"] == 2
    assert fresh["stale"] is False


def test_a_cold_cache_computes_and_answers(tmp_path: Path) -> None:
    """A fresh install behaves exactly as every release before this one."""

    cache = DerivedCache(tmp_path / "derived")

    answer = cached_payload(
        "thing", key="k1", compute=lambda: {"total": 7}, cache=cache
    )

    assert answer["total"] == 7
    assert answer["stale"] is False
    stored = cache.read("thing")
    assert stored is not None
    assert stored.payload == {"total": 7}


def test_only_one_refresh_runs_at_a_time(tmp_path: Path) -> None:
    cache = DerivedCache(tmp_path / "derived")
    cached_payload("thing", key="k1", compute=lambda: {"total": 1}, cache=cache)
    released = threading.Event()
    started: list[int] = []

    def slow() -> dict[str, Any]:
        started.append(1)
        released.wait(10.0)
        return {"total": 2}

    for _ in range(5):
        cached_payload("thing", key="k2", compute=slow, cache=cache)
    released.set()
    _settle("thing")

    assert started == [1]


def test_a_failing_refresh_leaves_the_stored_answer_alone(tmp_path: Path) -> None:
    cache = DerivedCache(tmp_path / "derived")
    cached_payload("thing", key="k1", compute=lambda: {"total": 1}, cache=cache)

    def explode() -> dict[str, Any]:
        raise RuntimeError("the log went away")

    answer = cached_payload("thing", key="k2", compute=explode, cache=cache)
    _settle("thing")

    assert answer["total"] == 1
    stored = cache.read("thing")
    assert stored is not None
    assert stored.key == "k1"


# ------------------------------------------------------------ the cost wiring


def test_the_cost_key_carries_the_log_mark_and_the_filters(tmp_path: Path) -> None:
    store = RequestLogStore(tmp_path / "requests.db", max_rows=1000)
    try:
        store.enqueue(_record("first"))
        store.close()

        plain = cost_breakdown_cache_key(store, local=None)
        filtered = cost_breakdown_cache_key(store, local="hide")

        assert plain != filtered
        assert plain.startswith(store.data_mark())
    finally:
        store.close()


def test_only_the_page_s_own_question_is_stored_on_disk() -> None:
    assert cost_breakdown_entry_name(local=None) == "cost-breakdown"
    assert cost_breakdown_entry_name(local="hide") == "cost-breakdown-local-hide"
    assert cost_breakdown_entry_name(local=None, provider=None) == "cost-breakdown"
    # Anything a reader typed is answered live, not filed away.
    assert cost_breakdown_entry_name(local=None, provider="groq") is None
    assert cost_breakdown_entry_name(local=None, q="hello") is None
    assert cost_breakdown_entry_name(local="only") is None


def test_the_cache_root_follows_the_config_directory() -> None:
    """Resolved, never hardcoded -- which is what the hermetic guard checks.

    ``MCC_CONFIG_DIR`` is deliberately not set here: the harness unsets it on
    purpose, because it outranks ``HOME`` and a test that sets it leaves the
    next test resolving somebody else's directory. The suite's redirected home
    is the config directory under test, and the assertion is that the cache
    follows it rather than ``Path.home()``.
    """

    from my_claude_code.config.paths import config_dir_path

    root = derived_cache().root
    assert root == config_dir_path() / "cache" / "derived"
    assert root.parent.parent == config_dir_path()
