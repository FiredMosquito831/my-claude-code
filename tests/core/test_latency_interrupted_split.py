"""A client hang-up is not a failure of the model that was streaming.

``latency_by_model`` measures ``outcome IN ('succeeded','failed')``, and an
attempt the client cancelled is stored as ``failed`` with
``error_kind='interrupted'``. Every hang-up was therefore charged to whichever
model happened to be streaming, as a failed attempt with a 300-600 s latency --
the client's own idle watchdog, measured as if it were the model's.

The fix is a third group, not a narrower ``WHERE``: ``_LATENCY_OUTCOMES_SQL``
is the documented seek on the leading ``outcome`` column of
``idx_request_attempts_ts_v1`` (1.772 s -> 0.871 s all time on the real table),
and an extra ``error_kind`` predicate there would break that. These tests pin
both halves of that: the answer, and the fact that the ``WHERE`` did not move.
"""

import inspect
from typing import Any

import pytest

from my_claude_code.core import request_log as request_log_module
from my_claude_code.core.request_log import (
    _LATENCY_OUTCOME_GROUP_SQL,
    _LATENCY_OUTCOMES_SQL,
    INTERRUPTED_ERROR_KIND,
    LATENCY_INTERRUPTED_OUTCOME,
    RequestLogStore,
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
)


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db", max_rows=500)
    yield store
    store.close()


def _record(request_id: str, **overrides: Any) -> RequestRecord:
    defaults: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-sonnet-4-5",
        "provider": "nvidia_nim",
        "resolved_model": "test-model",
        "stream": True,
        "status": "success",
        "ttft_ms": 12.5,
        "duration_ms": 120.0,
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


def _attempt(**overrides: Any) -> RouteAttempt:
    defaults: dict[str, Any] = {
        "attempt": 0,
        "provider": "nvidia_nim",
        "model_ref": "nvidia_nim/fast",
        "outcome": RouteAttemptOutcome.SUCCEEDED,
        "duration_ms": 1_000.0,
        "ttft_ms": 200.0,
        "first_reasoning_ms": 50.0,
        "tokens_out": 400,
    }
    defaults.update(overrides)
    return RouteAttempt(**defaults)


def _failed(ttft_ms: float) -> RouteAttempt:
    return _attempt(
        attempt=1,
        outcome=RouteAttemptOutcome.FAILED,
        error_kind="upstream",
        ttft_ms=ttft_ms,
        tokens_out=None,
    )


def _interrupted(ttft_ms: float) -> RouteAttempt:
    return _attempt(
        attempt=2,
        outcome=RouteAttemptOutcome.FAILED,
        error_kind=INTERRUPTED_ERROR_KIND,
        error_message="client cancelled before the stream finished",
        ttft_ms=ttft_ms,
        tokens_out=None,
    )


def _by_outcome(store: RequestLogStore) -> dict[str, dict[str, Any]]:
    return {str(row["outcome"]): row for row in store.latency_by_model()}


def test_the_where_clause_is_untouched() -> None:
    """The documented index seek, asserted rather than trusted.

    ``IN ('succeeded','failed')`` is deliberately written that way rather than
    as the ``!= 'skipped'`` it is equivalent to, because an inequality on a
    leading index column cannot seek. If this string ever gains an
    ``error_kind`` predicate the measured 1.772 s -> 0.871 s goes with it.
    """

    assert _LATENCY_OUTCOMES_SQL == "a.outcome IN ('succeeded', 'failed')"
    assert "error_kind" not in _LATENCY_OUTCOMES_SQL


def test_the_split_is_a_projection_not_a_filter() -> None:
    assert _LATENCY_OUTCOME_GROUP_SQL == (
        "CASE WHEN a.error_kind = 'interrupted' THEN 'interrupted' ELSE a.outcome END"
    )
    source = inspect.getsource(RequestLogStore.latency_by_model)
    # Both queries, or the aggregate's buckets and the percentile pull's would
    # not line up and every interrupted row would come back without one.
    assert source.count("_LATENCY_OUTCOME_GROUP_SQL") == 3
    assert source.count("_LATENCY_OUTCOMES_SQL") == 2


def test_an_interrupted_attempt_does_not_move_the_models_failed_p50(store) -> None:
    """The whole point, as a number.

    The same two genuine failures, with and without a 600 s client hang-up
    beside them. Before the split the hang-up was a third ``failed`` sample and
    dragged the median from 300 ms to 400 ms; it must now leave it alone.
    """

    store.enqueue(
        _record("req_a", attempts=(_attempt(), _failed(300.0))),
    )
    store.enqueue(
        _record("req_b", attempts=(_attempt(attempt=0), _failed(500.0))),
    )
    store.close()
    before = _by_outcome(store)["failed"]

    store_two = RequestLogStore(store.db_path.parent / "two.db", max_rows=500)
    try:
        store_two.enqueue(_record("req_a", attempts=(_attempt(), _failed(300.0))))
        store_two.enqueue(
            _record(
                "req_b",
                attempts=(
                    _attempt(attempt=0),
                    _failed(500.0),
                    _interrupted(600_000.0),
                ),
            )
        )
        store_two.close()
        after = _by_outcome(store_two)
    finally:
        store_two.close()

    assert after["failed"]["p50_ttft_ms"] == before["p50_ttft_ms"] == 300.0
    assert after["failed"]["p95_ttft_ms"] == before["p95_ttft_ms"] == 500.0
    assert after["failed"]["attempts"] == before["attempts"] == 2
    assert after["failed"]["avg_ttft_ms"] == before["avg_ttft_ms"] == 400.0
    # And the hang-up is not lost -- it is a row of its own.
    assert after[LATENCY_INTERRUPTED_OUTCOME]["attempts"] == 1
    assert after[LATENCY_INTERRUPTED_OUTCOME]["p50_ttft_ms"] == 600_000.0


def test_the_aggregate_and_the_percentile_pull_agree_on_group_keys(store) -> None:
    """If only one of the two grouped this way the buckets would not line up."""

    store.enqueue(
        _record(
            "req_all",
            attempts=(_attempt(), _failed(500.0), _interrupted(600_000.0)),
        )
    )
    store.close()
    rows = store.latency_by_model()
    assert {str(row["outcome"]) for row in rows} == {
        "succeeded",
        "failed",
        LATENCY_INTERRUPTED_OUTCOME,
    }
    # Every group that counted a measured attempt got percentiles, which only
    # happens when the two queries bucket on the same key.
    for row in rows:
        assert row["ttft_measured"] == 1
        assert row["p50_ttft_ms"] is not None
        assert row["p95_ttft_ms"] is not None


def test_a_skipped_attempt_is_still_excluded(store) -> None:
    """The ``WHERE`` did not move, so what it excluded stays excluded."""

    store.enqueue(
        _record(
            "req_skip",
            attempts=(
                _attempt(),
                _attempt(
                    attempt=1,
                    model_ref="nvidia_nim/never",
                    outcome=RouteAttemptOutcome.SKIPPED,
                    duration_ms=None,
                    ttft_ms=None,
                    first_reasoning_ms=None,
                    tokens_out=None,
                    error_kind=INTERRUPTED_ERROR_KIND,
                ),
            ),
        )
    )
    store.close()
    assert [row["model_ref"] for row in store.latency_by_model()] == ["nvidia_nim/fast"]


def test_the_latency_cache_key_shape_changed_so_stored_answers_do_not_survive(
    tmp_path,
) -> None:
    """``data_mark`` alone could not invalidate the documents on disk.

    A log that has not been written to since the upgrade has the mark it had
    before it, so an installed dashboard would have gone on serving pre-split
    rows -- hang-ups still folded into ``failed`` -- until the next request
    arrived.
    """

    from my_claude_code.application.derived_payloads import latency_by_model_cache_key

    store = RequestLogStore(tmp_path / "key.db", max_rows=10)
    try:
        key = latency_by_model_cache_key(store, since=None)
    finally:
        store.close()
    assert key.endswith("|shape=v2-interrupted")
    assert key.split("|")[1] == "since="


# -------------------------------------------------- the cache keys, reflected


def test_every_breakdown_filter_is_part_of_its_cache_key() -> None:
    """The 6.13.0 arity lesson, asserted by reflection rather than by reading.

    A filter that is not in the key is a filter that serves the previous
    caller's answer for five seconds. Reflection rather than a hand-written
    list, so a filter added later cannot be forgotten here.
    """

    signature = inspect.signature(RequestLogStore.cancelled_breakdown)
    filters = [
        name for name in signature.parameters if name not in {"self", "kwargs", "args"}
    ]
    assert filters, "the reflection found nothing, which would pass vacuously"
    source = inspect.getsource(RequestLogStore.cancelled_breakdown)
    key_block = source.split("cache_key = (", 1)[1].split(")", 1)[0]
    for name in filters:
        assert name in key_block, f"{name} is not part of the cache key"
    # Led by a literal string and of its own arity, so it can never collide
    # with the bare ten-tuple ``stats()`` uses or the twelve ``cost_breakdown``
    # does -- a user really can filter on ``provider=cancelled_breakdown``.
    assert '"cancelled_breakdown"' in key_block
    assert len(filters) + 1 not in {10, 12}


def test_each_filter_gets_its_own_cached_answer(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "cache.db", max_rows=100)
    try:
        store.enqueue(
            _record(
                "c1",
                status="cancelled",
                ttft_ms=None,
                duration_ms=600_000.0,
                output_chars=0,
            )
        )
        store.close()
        everything = store.cancelled_breakdown()
        narrowed = store.cancelled_breakdown(provider="nobody")
        assert everything["total"] == 1
        assert narrowed["total"] == 0
    finally:
        store.close()


def test_a_caller_cannot_edit_the_cached_breakdown(tmp_path) -> None:
    store = RequestLogStore(tmp_path / "copy.db", max_rows=100)
    try:
        store.close()
        first = store.cancelled_breakdown()
        first["total"] = 99
        assert store.cancelled_breakdown()["total"] == 0
    finally:
        store.close()


def test_the_dashboard_and_the_store_agree_on_the_sub_labels() -> None:
    """Two spellings of one vocabulary, in two languages.

    ``admin.js`` cannot import the Python constants, so the only thing keeping
    the chip's words and the store's values from drifting is this assertion.
    """

    from pathlib import Path

    from my_claude_code.core.cancelled_reasons import (
        CANCELLED_SUB_LABEL_EXPLANATION,
        CANCELLED_SUB_LABEL_TEXT,
    )

    admin_js = (
        Path(request_log_module.__file__).parent.parent
        / "api"
        / "admin_static"
        / "admin.js"
    ).read_text(encoding="utf-8")
    for label, text in CANCELLED_SUB_LABEL_TEXT.items():
        assert f'{label}: "{text}"' in admin_js, label
    for label in CANCELLED_SUB_LABEL_EXPLANATION:
        assert f"  {label}:" in admin_js, label
    assert 'interrupted: "client hung up"' in admin_js
