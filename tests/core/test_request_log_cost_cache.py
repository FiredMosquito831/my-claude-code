"""The cost breakdown is cached like its neighbours, and says the same thing.

``cost_breakdown`` was the only aggregate in this module with no cache at all:
six full scans of ``requests`` on every call, and the Analytics page asks for
it on every open and every refresh. Measured at 9.0 s on a 4.5 GB log.

What is *not* here, deliberately: a test for collapsing the six scans into one.
That was measured and rejected -- see the class docstring on the cache key --
because folding per-combination sums into per-dimension sums re-associates
floating-point addition and changed nine dollar figures in their last bits.
"""

import time
from typing import Any

from my_claude_code.core import request_log as request_log_module
from my_claude_code.core.request_log import RequestLogStore, RequestRecord


def _record(request_id: str, **overrides: Any) -> RequestRecord:
    defaults: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-sonnet-4-5",
        "provider": "nvidia_nim",
        "resolved_model": "test-model",
        "harness": "claude",
        "stream": True,
        "tokens_in": 10,
        "tokens_out": 20,
        "duration_ms": 120.0,
        "status": "success",
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


def _seeded(path) -> RequestLogStore:
    store = RequestLogStore(path, max_rows=10_000)
    now = time.time()
    for index in range(12):
        store.enqueue(
            _record(
                f"priced-{index}",
                ts_epoch=now - index * 3600,
                cost_usd=0.001 * (index + 1),
                cost_source="models_dev",
            )
        )
    for index in range(3):
        store.enqueue(
            _record(
                f"reported-{index}",
                ts_epoch=now - index * 3600,
                provider="open_router",
                cost_usd=0.5,
                cost_source="provider",
            )
        )
    for index in range(5):
        store.enqueue(_record(f"unpriced-{index}", ts_epoch=now - index * 3600))
    store.close()
    return store


def test_cost_breakdown_is_cached_for_five_seconds(tmp_path) -> None:
    store = _seeded(tmp_path / "requests.db")
    try:
        first = store.cost_breakdown()
        assert len(store._stats_cache) == 1

        # A write the cache cannot see: the cached answer must be returned
        # unchanged inside the TTL, which is what "cached" means.
        with request_log_module.sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "INSERT INTO requests (id, ts_epoch, ts_iso, endpoint, protocol,"
                " status, stream, cost_usd, cost_source) VALUES ('sneaked', 1.0,"
                " 'then', '/v1/messages', 'anthropic', 'success', 0, 9.0,"
                " 'models_dev')"
            )

        assert store.cost_breakdown() == first
    finally:
        store.close()


def test_the_cost_cache_expires(tmp_path, monkeypatch) -> None:
    store = _seeded(tmp_path / "requests.db")
    try:
        first = store.cost_breakdown()
        monkeypatch.setattr(request_log_module, "_STATS_CACHE_TTL_SECONDS", -1.0)
        with request_log_module.sqlite3.connect(store.db_path) as conn:
            conn.execute(
                "INSERT INTO requests (id, ts_epoch, ts_iso, endpoint, protocol,"
                " status, stream, cost_usd, cost_source) VALUES ('later', 1.0,"
                " 'then', '/v1/messages', 'anthropic', 'success', 0, 9.0,"
                " 'models_dev')"
            )

        second = store.cost_breakdown()

        assert second != first
        assert second["totals"]["requests"] == first["totals"]["requests"] + 1
    finally:
        store.close()


def test_a_cached_answer_equals_the_uncached_one(tmp_path) -> None:
    """The equality oracle: the cache may not reshape what it stores."""

    store = _seeded(tmp_path / "requests.db")
    try:
        cached = store.cost_breakdown()
        store._stats_cache.clear()
        fresh = store.cost_breakdown()

        assert cached == fresh
        # And the numbers are the ones the invariants demand.
        assert fresh["totals"]["reported_usd"] == 1.5
        assert fresh["totals"]["priced"] == 15
        assert fresh["totals"]["requests"] == 20
    finally:
        store.close()


def test_every_filter_gets_its_own_cache_entry(tmp_path) -> None:
    """A filtered call inside the TTL must not be served the unfiltered answer."""

    store = _seeded(tmp_path / "requests.db")
    try:
        everything = store.cost_breakdown()
        one_provider = store.cost_breakdown(provider="open_router")

        assert everything["totals"]["requests"] == 20
        assert one_provider["totals"]["requests"] == 3
        assert len(store._stats_cache) == 2
    finally:
        store.close()


def test_the_cost_cache_key_cannot_collide_with_stats(tmp_path) -> None:
    """Arity is the contract: ten elements for stats, twelve for cost.

    A user can genuinely type ``cost_breakdown`` into the provider filter, so
    the string prefix alone is not enough -- the lengths have to differ.
    """

    store = _seeded(tmp_path / "requests.db")
    try:
        store.stats(provider="cost_breakdown")
        store.cost_breakdown(provider="cost_breakdown")

        keys = list(store._stats_cache)
        assert len(keys) == 2
        assert len({len(key) for key in keys}) == 2, keys
        assert all(key[0] == "cost_breakdown" for key in keys)
    finally:
        store.close()


def test_a_caller_cannot_edit_the_cached_payload(tmp_path) -> None:
    store = _seeded(tmp_path / "requests.db")
    try:
        first = store.cost_breakdown()
        first["totals"]["reported_usd"] = 999.0
        first["by_provider"].clear()

        second = store.cost_breakdown()

        assert second["totals"]["reported_usd"] == 1.5
        assert second["by_provider"]
    finally:
        store.close()


def test_the_cost_cache_respects_the_shared_entry_bound(tmp_path) -> None:
    store = _seeded(tmp_path / "requests.db")
    try:
        for index in range(request_log_module._STATS_CACHE_MAX_ENTRIES + 10):
            store.cost_breakdown(provider=f"provider-{index}")

        assert len(store._stats_cache) <= request_log_module._STATS_CACHE_MAX_ENTRIES
    finally:
        store.close()
