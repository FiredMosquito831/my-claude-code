"""WebSearchLogStore round-trips, rollups, retention, and the shared singleton."""

import sqlite3
import threading
from datetime import UTC, datetime

import pytest

from my_claude_code.websearch.analytics import (
    WebSearchLogStore,
    _ensure_auto_vacuum,
    default_websearch_db_path,
    get_shared_store,
    record_search,
    record_search_route,
    reset_analytics_state,
)
from my_claude_code.websearch.registry import (
    SearchOutcome,
    SearchRouteOutcome,
    search,
)
from tests.websearch.support import StubWebSearchProvider, build_config

_BASE_TS = datetime(2026, 6, 15, 12, 0, tzinfo=UTC).timestamp()


def _ts(iso_text: str) -> float:
    return datetime.fromisoformat(iso_text).timestamp()


def _outcome(
    *,
    ts_epoch: float = _BASE_TS,
    provider: str = "exa",
    key_index: int = 0,
    key_label: str = "exak…1234",
    query: str = "query",
    results_count: int = 3,
    duration_ms: float = 12.5,
    status: str = "success",
    error_kind: str | None = None,
    error_message: str | None = None,
    cost_usd: float | None = None,
    route_id: str | None = None,
    attempt_number: int = 1,
    input_payload: dict[str, object] | None = None,
    output_payload: dict[str, object] | None = None,
    provider_config: dict[str, object] | None = None,
) -> SearchOutcome:
    return SearchOutcome(
        ts_epoch=ts_epoch,
        ts_iso=datetime.fromtimestamp(ts_epoch, tz=UTC).isoformat(),
        provider=provider,
        key_index=key_index,
        key_label=key_label,
        query=query,
        results_count=results_count,
        duration_ms=duration_ms,
        status=status,
        error_kind=error_kind,
        error_message=error_message,
        cost_usd=cost_usd,
        route_id=route_id,
        attempt_number=attempt_number,
        input_payload=input_payload,
        output_payload=output_payload,
        provider_config=provider_config,
    )


def _route_outcome(
    *,
    route_id: str = "route-1",
    ts_epoch: float = _BASE_TS,
    query: str = "query",
    primary_provider: str = "exa",
    terminal_provider: str = "exa",
    provider_path: tuple[str, ...] = ("exa",),
    attempt_count: int = 1,
    fallback_used: bool = False,
    duration_ms: float = 25.0,
    status: str = "success",
    results_count: int = 3,
    cost_usd: float | None = None,
    error_kind: str | None = None,
    error_message: str | None = None,
) -> SearchRouteOutcome:
    return SearchRouteOutcome(
        route_id=route_id,
        ts_epoch=ts_epoch,
        ts_iso=datetime.fromtimestamp(ts_epoch, tz=UTC).isoformat(),
        query=query,
        primary_provider=primary_provider,
        terminal_provider=terminal_provider,
        provider_path=provider_path,
        attempt_count=attempt_count,
        fallback_used=fallback_used,
        duration_ms=duration_ms,
        status=status,
        results_count=results_count,
        cost_usd=cost_usd,
        error_kind=error_kind,
        error_message=error_message,
    )


@pytest.fixture(autouse=True)
def _isolated_analytics_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("WEBSEARCH_LOG_ENABLED", raising=False)
    monkeypatch.delenv("WEBSEARCH_LOG_MAX_ROWS", raising=False)
    monkeypatch.delenv("WEBSEARCH_LOG_CAPTURE_CONTENT", raising=False)
    monkeypatch.delenv("WEBSEARCH_LOG_CONTENT_MAX_CHARS", raising=False)
    reset_analytics_state()
    yield
    reset_analytics_state()


@pytest.fixture
def store(tmp_path):
    store = WebSearchLogStore(tmp_path / "logs" / "websearch.db")
    yield store
    store.close()


class TestRoundTrip:
    def test_full_io_and_redacted_config_are_detail_only(self, store) -> None:
        store.record(
            _outcome(
                query="full query",
                input_payload={
                    "query": "full query",
                    "max_results": 10,
                    "api_key": "must-not-persist",
                },
                output_payload={
                    "provider": "exa",
                    "answer": "A synthesized answer",
                    "results": [
                        {
                            "title": "Result",
                            "url": "https://example.com",
                            "snippet": "Short excerpt",
                            "content": "Fuller provider content",
                            "published": "2026-06-15",
                        }
                    ],
                },
                provider_config={
                    "provider_id": "exa",
                    "credential_count": 2,
                    "access_token": "must-not-persist",
                    "options": {"EXA_SEARCH_TYPE": "deep"},
                },
            )
        )
        store.flush()

        (summary,) = store.requests()["items"]
        assert "input" not in summary
        assert "output" not in summary
        assert "provider_config" not in summary
        assert summary["content_captured"] is True
        assert summary["input_chars"] > 0
        assert summary["output_chars"] > 0
        assert len(summary["input_sha256"]) == 64
        assert len(summary["output_sha256"]) == 64

        detail = store.request(summary["id"])
        assert detail is not None
        assert detail["input"]["query"] == "full query"
        assert detail["input"]["api_key"] == "[REDACTED]"
        assert detail["output"]["answer"] == "A synthesized answer"
        assert detail["output"]["results"][0]["content"] == "Fuller provider content"
        assert detail["provider_config"]["access_token"] == "[REDACTED]"
        assert detail["provider_config"]["options"] == {"EXA_SEARCH_TYPE": "deep"}

        (exported,) = store.requests(include_content=True)["items"]
        assert exported == detail
        assert "must-not-persist" not in str(detail)

    def test_capture_disabled_keeps_lengths_and_hashes(self, tmp_path) -> None:
        store = WebSearchLogStore(
            tmp_path / "hashes.db",
            capture_content=False,
        )
        store.record(
            _outcome(
                input_payload={"query": "private query"},
                output_payload={"answer": "private answer", "results": []},
                provider_config={"provider_id": "exa"},
            )
        )
        store.flush()

        (summary,) = store.requests()["items"]
        detail = store.request(summary["id"])
        assert detail is not None
        assert detail["content_captured"] is False
        assert detail["input"] is None
        assert detail["output"] is None
        assert detail["input_chars"] > 0
        assert detail["output_chars"] > 0
        assert len(detail["input_sha256"]) == 64
        assert len(detail["output_sha256"]) == 64
        assert detail["provider_config"] == {"provider_id": "exa"}
        assert store.stats()["capture_content"] is False
        store.close()

    def test_oversized_output_uses_valid_truncation_envelope(self, tmp_path) -> None:
        store = WebSearchLogStore(
            tmp_path / "truncated.db",
            max_content_chars=512,
        )
        store.record(
            _outcome(
                output_payload={"answer": "x" * 2000, "results": []},
            )
        )
        store.flush()

        (summary,) = store.requests()["items"]
        detail = store.request(summary["id"])
        assert detail is not None
        assert detail["output"]["_truncated"] is True
        assert detail["output"]["original_chars"] == summary["output_chars"]
        assert detail["output"]["sha256"] == summary["output_sha256"]
        assert isinstance(detail["output"]["preview"], str)
        store.close()

    def test_full_output_is_stored_untruncated(self, store) -> None:
        """The default cap (2M chars) retains real provider output in full.

        A ~60k-char output exceeds the old 50k default and would previously have
        been stored as a truncation envelope; at the new default it round-trips
        byte-for-byte.
        """
        big_answer = "answer " + "x" * 60_000
        store.record(
            _outcome(
                output_payload={"answer": big_answer, "results": []},
            )
        )
        store.flush()

        (summary,) = store.requests()["items"]
        detail = store.request(summary["id"])
        assert detail is not None
        # output_chars is the serialized JSON length, so it must exceed the raw
        # answer by the JSON envelope; the point is nothing was truncated.
        assert summary["output_chars"] > len(big_answer)
        assert "_truncated" not in detail["output"]
        assert detail["output"]["answer"] == big_answer
        store.close()

    def test_recorded_outcomes_round_trip_all_fields(self, store) -> None:
        ts = _ts("2026-06-15T08:30:00+00:00")
        store.record(
            _outcome(
                ts_epoch=ts,
                provider="exa",
                key_index=1,
                key_label="exak…wxyz",
                query="best tacos",
                results_count=7,
                duration_ms=42.25,
                cost_usd=0.003,
            )
        )
        store.record(
            _outcome(
                ts_epoch=ts + 1,
                provider="tavily",
                query="failed query",
                results_count=0,
                duration_ms=5.0,
                status="error",
                error_kind="rate_limit",
                error_message="429 slow down",
            )
        )
        store.flush()

        page = store.requests()
        assert page["total"] == 2
        newest, oldest = page["items"]
        assert newest["provider"] == "tavily"
        assert newest["status"] == "error"
        assert newest["error_kind"] == "rate_limit"
        assert newest["error_message"] == "429 slow down"
        assert newest["results_count"] == 0
        assert newest["cost_usd"] is None
        assert oldest["provider"] == "exa"
        assert oldest["key_index"] == 1
        assert oldest["key_label"] == "exak…wxyz"
        assert oldest["query"] == "best tacos"
        assert oldest["results_count"] == 7
        assert oldest["duration_ms"] == 42.25
        assert oldest["cost_usd"] == 0.003
        assert oldest["ts_epoch"] == ts
        assert oldest["ts_iso"] == "2026-06-15T08:30:00+00:00"
        assert oldest["status"] == "success"
        assert oldest["error_kind"] is None
        assert oldest["error_message"] is None
        assert oldest["id"] > 0

    def test_query_and_error_message_are_capped(self, store) -> None:
        store.record(
            _outcome(
                query="x" * 1000,
                status="error",
                error_kind="upstream",
                error_message="e" * 1000,
                results_count=0,
            )
        )
        store.flush()

        (row,) = store.requests()["items"]
        assert len(row["query"]) == 256
        assert len(row["error_message"]) == 500

    @pytest.mark.asyncio
    async def test_recorded_label_is_the_masked_key(self, store) -> None:
        provider = StubWebSearchProvider(build_config(api_keys=("sk-live-0001wxyz",)))

        response = await search(provider, "hello", recorder=store.record)

        assert response.results
        store.flush()
        (row,) = store.requests()["items"]
        assert row["key_label"] == "sk-l…wxyz"
        assert "sk-live-0001wxyz" not in row["key_label"]
        assert row["status"] == "success"
        assert row["results_count"] == 1

    def test_attempt_correlation_fields_round_trip(self, store) -> None:
        store.record(_outcome(route_id="route-123", attempt_number=2))
        store.flush()

        (row,) = store.requests()["items"]

        assert row["route_id"] == "route-123"
        assert row["attempt_number"] == 2


class TestRetention:
    def test_prunes_to_max_rows_keeping_newest(self, tmp_path) -> None:
        store = WebSearchLogStore(tmp_path / "websearch.db", max_rows=5, prune_every=1)
        for index in range(7):
            store.record(_outcome(query=f"q{index}", ts_epoch=_BASE_TS + index))
        store.flush()

        items = store.requests(limit=50)["items"]
        assert [row["query"] for row in items] == ["q6", "q5", "q4", "q3", "q2"]
        store.close()

    def test_route_retention_tracks_surviving_attempt_window(self, tmp_path) -> None:
        """Routes must not outlive the attempts they summarize.

        Each logical route writes more than one attempt row, so ``search_log``
        reaches ``max_rows`` first. Pruning both tables to the same row count
        would leave old routes describing a window whose attempt detail is
        already gone, and the admin UI shows the two tables side by side as if
        they covered the same period.
        """

        store = WebSearchLogStore(tmp_path / "websearch.db", max_rows=6, prune_every=1)
        for index in range(6):
            route_id = f"route-{index}"
            ts = _BASE_TS + index
            # Two attempts per route (a fallback hop), one route row.
            store.record(_outcome(ts_epoch=ts, route_id=route_id, attempt_number=1))
            store.record(_outcome(ts_epoch=ts, route_id=route_id, attempt_number=2))
            store.record_route(_route_outcome(route_id=route_id, ts_epoch=ts))
        store.flush()

        attempts = store.requests(limit=50)["items"]
        oldest_attempt = min(row["ts_epoch"] for row in attempts)

        with sqlite3.connect(tmp_path / "websearch.db") as connection:
            oldest_route, surviving_routes = connection.execute(
                "SELECT MIN(ts_epoch), COUNT(*) FROM search_route_log"
            ).fetchone()

        assert surviving_routes, "expected surviving route rows"

        assert oldest_route is not None
        assert oldest_route >= oldest_attempt, (
            "route rows survive older than the oldest retained attempt: "
            f"route={oldest_route} attempt={oldest_attempt}"
        )
        store.close()


class TestQueryCapture:
    def test_query_text_is_withheld_when_content_capture_is_off(self, tmp_path) -> None:
        """``WEBSEARCH_LOG_CAPTURE_CONTENT=false`` must also cover the query.

        The setting exists so operators can stop persisting search content;
        the query string is search content too, and previously bypassed it.
        """

        store = WebSearchLogStore(
            tmp_path / "websearch.db",
            max_rows=100,
            prune_every=1,
            capture_content=False,
        )
        store.record(
            _outcome(
                query="internal-hostname prod token rotation",
                input_payload={"query": "internal-hostname prod token rotation"},
                output_payload={"results": []},
            )
        )
        store.flush()

        (row,) = store.requests()["items"]
        assert row["content_captured"] == 0
        assert "internal-hostname" not in (row["query"] or "")

        detail = store.request(row["id"])
        assert detail is not None
        assert detail["input"] is None
        assert "internal-hostname" not in (detail["query"] or "")
        store.close()


def _record_boundary_rows(store: WebSearchLogStore) -> None:
    # 2025-12-28 (Sun) is ISO week 2025-W52; 2025-12-29 (Mon) is ISO week 2026-W01.
    store.record(_outcome(ts_epoch=_ts("2025-12-28T12:00:00+00:00"), provider="exa"))
    store.record(_outcome(ts_epoch=_ts("2025-12-29T12:00:00+00:00"), provider="exa"))
    store.record(
        _outcome(
            ts_epoch=_ts("2026-01-01T00:30:00+00:00"),
            provider="tavily",
            results_count=0,
            status="error",
            error_kind="quota",
            error_message="quota exceeded",
        )
    )
    store.flush()


class TestStats:
    def test_weekly_series_uses_iso_weeks_across_year_boundary(self, store) -> None:
        _record_boundary_rows(store)

        stats = store.stats("weekly")

        assert stats["period"] == "weekly"
        assert stats["series"] == [
            {
                "bucket": "2025-W52",
                "provider": "exa",
                "requests": 1,
                "errors": 0,
                "results": 3,
            },
            {
                "bucket": "2026-W01",
                "provider": "exa",
                "requests": 1,
                "errors": 0,
                "results": 3,
            },
            {
                "bucket": "2026-W01",
                "provider": "tavily",
                "requests": 1,
                "errors": 1,
                "results": 0,
            },
        ]

    def test_monthly_series_buckets_by_month_across_year_boundary(self, store) -> None:
        _record_boundary_rows(store)

        stats = store.stats("monthly")

        assert stats["period"] == "monthly"
        assert stats["series"] == [
            {
                "bucket": "2025-12",
                "provider": "exa",
                "requests": 2,
                "errors": 0,
                "results": 6,
            },
            {
                "bucket": "2026-01",
                "provider": "tavily",
                "requests": 1,
                "errors": 1,
                "results": 0,
            },
        ]

    @pytest.mark.parametrize(
        ("period", "expected_buckets"),
        (
            ("hourly", ["2026-06-15T12:00", "2026-06-16T13:00"]),
            ("daily", ["2026-06-15", "2026-06-16"]),
        ),
    )
    def test_hourly_and_daily_series_buckets(
        self, store, period, expected_buckets
    ) -> None:
        store.record(
            _outcome(ts_epoch=_ts("2026-06-15T12:15:00+00:00"), provider="exa")
        )
        store.record(
            _outcome(ts_epoch=_ts("2026-06-15T12:45:00+00:00"), provider="exa")
        )
        store.record(
            _outcome(ts_epoch=_ts("2026-06-16T13:00:00+00:00"), provider="exa")
        )
        store.flush()

        stats = store.stats(period)

        assert [entry["bucket"] for entry in stats["series"]] == expected_buckets
        assert [entry["requests"] for entry in stats["series"]] == [2, 1]

    def test_by_provider_and_totals_aggregation(self, store) -> None:
        store.record(_outcome(provider="exa", duration_ms=10.0, results_count=4))
        store.record(
            _outcome(
                provider="exa",
                duration_ms=20.0,
                results_count=0,
                status="error",
                error_kind="upstream",
                error_message="boom",
            )
        )
        store.record(
            _outcome(
                provider="tavily", duration_ms=30.0, results_count=2, cost_usd=0.01
            )
        )
        store.flush()

        stats = store.stats()

        assert stats["totals"] == {
            "requests": 3,
            "successes": 2,
            "errors": 1,
            "avg_duration_ms": 20.0,
            "results": 6,
            "cost_usd": 0.01,
        }
        exa, tavily = stats["by_provider"]
        assert exa == {
            "provider": "exa",
            "requests": 2,
            "errors": 1,
            "avg_duration_ms": 15.0,
            "results": 4,
            "cost_usd": None,
        }
        assert tavily["provider"] == "tavily"
        assert tavily["cost_usd"] == 0.01
        assert stats["top_errors"] == [
            {"error_kind": "upstream", "error_message": "boom", "count": 1}
        ]

    def test_filters_apply_to_every_rollup_and_report_bounds(self, store) -> None:
        since = _ts("2026-06-15T12:00:00+00:00")
        until = _ts("2026-06-15T13:00:00+00:00")
        store.record(
            _outcome(
                ts_epoch=since + 10,
                provider="exa",
                key_label="exak…selected",
                query="needle selected",
                results_count=0,
                duration_ms=25.0,
                status="error",
                error_kind="selected_error",
                error_message="selected failure",
            )
        )
        store.record(
            _outcome(
                ts_epoch=since + 20,
                provider="exa",
                query="different query",
                results_count=0,
                status="error",
                error_kind="wrong_query",
                error_message="excluded",
            )
        )
        store.record(
            _outcome(
                ts_epoch=since + 30,
                provider="exa",
                query="needle success",
                status="success",
            )
        )
        store.record(
            _outcome(
                ts_epoch=since + 40,
                provider="tavily",
                query="needle other provider",
                results_count=0,
                status="error",
                error_kind="wrong_provider",
                error_message="excluded",
            )
        )
        store.record(
            _outcome(
                ts_epoch=until + 1,
                provider="exa",
                query="needle outside window",
                results_count=0,
                status="error",
                error_kind="outside_window",
                error_message="excluded",
            )
        )
        store.flush()

        stats = store.stats(
            "hourly",
            provider="exa",
            status="error",
            q="needle",
            since_epoch=since,
            until_epoch=until,
        )

        assert stats["filters"] == {
            "provider": "exa",
            "status": "error",
            "q": "needle",
            "since_epoch": since,
            "until_epoch": until,
        }
        assert stats["window"] == {
            "since_epoch": since,
            "until_epoch": until,
        }
        assert stats["dropped_records"] == 0
        assert stats["totals"] == {
            "requests": 1,
            "successes": 0,
            "errors": 1,
            "avg_duration_ms": 25.0,
            "results": 0,
            "cost_usd": None,
        }
        assert stats["by_provider"] == [
            {
                "provider": "exa",
                "requests": 1,
                "errors": 1,
                "avg_duration_ms": 25.0,
                "results": 0,
                "cost_usd": None,
            }
        ]
        assert stats["by_key"] == [
            {
                "provider": "exa",
                "key_label": "exak…selected",
                "requests": 1,
                "errors": 1,
                "avg_duration_ms": 25.0,
                "results": 0,
            }
        ]
        assert stats["top_errors"] == [
            {
                "error_kind": "selected_error",
                "error_message": "selected failure",
                "count": 1,
            }
        ]
        assert stats["series"] == [
            {
                "bucket": "2026-06-15T12:00",
                "provider": "exa",
                "requests": 1,
                "errors": 1,
                "results": 0,
            }
        ]

    def test_by_key_groups_provider_and_key_label(self, store) -> None:
        store.record(_outcome(provider="exa", key_index=0, key_label="exak…aaaa"))
        store.record(
            _outcome(
                provider="exa",
                key_index=1,
                key_label="exak…bbbb",
                status="error",
                error_kind="auth",
                error_message="401 denied",
                results_count=0,
            )
        )
        store.record(_outcome(provider="tavily", key_index=0, key_label="exak…aaaa"))
        store.flush()

        stats = store.stats()

        assert stats["by_key"] == [
            {
                "provider": "exa",
                "key_label": "exak…aaaa",
                "requests": 1,
                "errors": 0,
                "avg_duration_ms": 12.5,
                "results": 3,
            },
            {
                "provider": "exa",
                "key_label": "exak…bbbb",
                "requests": 1,
                "errors": 1,
                "avg_duration_ms": 12.5,
                "results": 0,
            },
            {
                "provider": "tavily",
                "key_label": "exak…aaaa",
                "requests": 1,
                "errors": 0,
                "avg_duration_ms": 12.5,
                "results": 3,
            },
        ]

    def test_stats_on_empty_database(self, store) -> None:
        stats = store.stats()

        expected_attempt_totals = {
            "requests": 0,
            "successes": 0,
            "errors": 0,
            "avg_duration_ms": None,
            "results": 0,
            "cost_usd": None,
        }
        assert stats["period"] == "weekly"
        assert stats["window"] == {"since_epoch": None, "until_epoch": None}
        assert stats["totals"] == expected_attempt_totals
        assert stats["attempts"]["totals"] == expected_attempt_totals
        assert stats["routes"]["totals"] == {
            "searches": 0,
            "successes": 0,
            "errors": 0,
            "fallbacks": 0,
            "fallback_rate": 0.0,
            "avg_attempts": None,
            "avg_duration_ms": None,
            "results": 0,
            "cost_usd": None,
        }
        assert stats["routes"]["series"] == []
        assert stats["last_route"] is None
        assert store.requests() == {"total": 0, "limit": 50, "offset": 0, "items": []}

    def test_unknown_period_rejected(self, store) -> None:
        with pytest.raises(ValueError, match="unknown stats period"):
            store.stats("yearly")


class TestRouteStats:
    def test_fallback_attempts_are_one_logical_search(self, store) -> None:
        store.record(
            _outcome(
                route_id="route-fallback",
                attempt_number=1,
                provider="exa",
                status="error",
                results_count=0,
                error_kind="upstream",
                error_message="primary failed",
            )
        )
        store.record(
            _outcome(
                route_id="route-fallback",
                attempt_number=2,
                provider="ddgs",
                status="success",
                results_count=4,
            )
        )
        store.record_route(
            _route_outcome(
                route_id="route-fallback",
                primary_provider="exa",
                terminal_provider="ddgs",
                provider_path=("exa", "ddgs"),
                attempt_count=2,
                fallback_used=True,
                duration_ms=50.0,
                results_count=4,
            )
        )
        store.flush()

        stats = store.stats("daily")

        assert stats["attempts"]["totals"]["requests"] == 2
        assert stats["attempts"]["totals"]["errors"] == 1
        assert stats["routes"]["totals"] == {
            "searches": 1,
            "successes": 1,
            "errors": 0,
            "fallbacks": 1,
            "fallback_rate": 1.0,
            "avg_attempts": 2.0,
            "avg_duration_ms": 50.0,
            "results": 4,
            "cost_usd": None,
        }
        assert stats["routes"]["series"] == [
            {
                "bucket": "2026-06-15",
                "provider": "ddgs",
                "searches": 1,
                "errors": 0,
                "fallbacks": 1,
                "results": 4,
            }
        ]
        assert stats["routes"]["by_primary_provider"][0]["provider"] == "exa"
        assert stats["routes"]["by_primary_provider"][0]["searches"] == 1
        assert stats["routes"]["by_terminal_provider"][0]["provider"] == "ddgs"
        assert stats["routes"]["by_terminal_provider"][0]["fallbacks"] == 1
        assert stats["last_route"]["route_id"] == "route-fallback"
        assert stats["last_route"]["providers"] == ["exa", "ddgs"]
        assert stats["last_route"]["fallback_used"] is True

    @pytest.mark.parametrize("provider", ("exa", "ddgs"))
    def test_provider_filter_matches_any_provider_in_route(
        self, store, provider
    ) -> None:
        store.record_route(
            _route_outcome(
                route_id="route-chain",
                primary_provider="exa",
                terminal_provider="ddgs",
                provider_path=("exa", "ddgs"),
                attempt_count=2,
                fallback_used=True,
            )
        )
        store.record_route(
            _route_outcome(
                route_id="route-other",
                primary_provider="tavily",
                terminal_provider="tavily",
                provider_path=("tavily",),
            )
        )
        store.flush()

        routes = store.stats(provider=provider)["routes"]

        assert routes["totals"]["searches"] == 1
        assert routes["last_route"]["route_id"] == "route-chain"

    def test_route_filters_are_case_insensitive_and_isolate_errors(self, store) -> None:
        store.record_route(
            _route_outcome(
                route_id="selected",
                query="Needle Query",
                status="error",
                results_count=0,
                error_kind="quota",
                error_message="selected failure",
            )
        )
        store.record_route(
            _route_outcome(
                route_id="wrong-query",
                query="different",
                status="error",
                results_count=0,
                error_kind="upstream",
                error_message="excluded",
            )
        )
        store.record_route(
            _route_outcome(route_id="wrong-status", query="NEEDLE success")
        )
        store.flush()

        routes = store.stats(status="error", q="needle")["routes"]

        assert routes["totals"]["searches"] == 1
        assert routes["top_errors"] == [
            {
                "error_kind": "quota",
                "error_message": "selected failure",
                "count": 1,
            }
        ]
        assert routes["last_route"]["route_id"] == "selected"

    def test_attempt_query_filter_is_case_insensitive(self, store) -> None:
        store.record(_outcome(query="Mixed CASE Query"))
        store.flush()

        assert store.requests(q="case")["total"] == 1
        assert store.stats(q="mixed case")["attempts"]["totals"]["requests"] == 1

    def test_filter_matches_captured_provider_output(self, store) -> None:
        store.record(
            _outcome(
                query="unrelated query",
                route_id="captured-route",
                input_payload={"query": "unrelated query"},
                output_payload={
                    "answer": "Unique Provider Summary",
                    "results": [],
                },
            )
        )
        store.record_route(
            _route_outcome(
                route_id="captured-route",
                query="unrelated query",
            )
        )
        store.flush()

        assert store.requests(q="provider summary")["total"] == 1
        assert store.stats(q="PROVIDER SUMMARY")["attempts"]["totals"]["requests"] == 1
        assert store.stats(q="provider summary")["routes"]["totals"]["searches"] == 1


class TestRecordSearch:
    def test_disabled_logging_skips_persistence(self, monkeypatch) -> None:
        monkeypatch.setenv("WEBSEARCH_LOG_ENABLED", "false")

        record_search(_outcome())

        assert not default_websearch_db_path().exists()

    def test_enabled_logging_persists_to_default_path(self) -> None:
        record_search(_outcome(query="shared query"))
        record_search_route(_route_outcome(query="shared query"))

        store = get_shared_store()
        store.flush()
        page = store.requests()
        assert page["total"] == 1
        assert page["items"][0]["query"] == "shared query"
        assert store.stats()["routes"]["totals"]["searches"] == 1
        assert default_websearch_db_path().is_file()
        assert default_websearch_db_path().parent.name == "logs"
        assert default_websearch_db_path().parent.parent.name == ".mcc"

    def test_shared_store_honors_max_rows_setting(self, monkeypatch) -> None:
        monkeypatch.setenv("WEBSEARCH_LOG_MAX_ROWS", "3")
        store = get_shared_store()

        for index in range(100):
            store.record(_outcome(query=f"bulk {index}", ts_epoch=_BASE_TS + index))
        store.flush()
        # Prune cadence (default 100 inserts) fired: trimmed to the newest 3.
        assert store.requests(limit=500)["total"] == 3

        for index in range(100, 105):
            store.record(_outcome(query=f"bulk {index}", ts_epoch=_BASE_TS + index))
        store.flush()

        page = store.requests(limit=500)
        assert page["total"] == 8
        assert page["items"][0]["query"] == "bulk 104"


class TestSchemaMigration:
    def test_existing_attempt_database_is_migrated_without_data_loss(
        self, tmp_path
    ) -> None:
        db_path = tmp_path / "legacy-websearch.db"
        connection = sqlite3.connect(db_path)
        connection.executescript(
            """
            CREATE TABLE search_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_epoch REAL NOT NULL,
                ts_iso TEXT NOT NULL,
                provider TEXT NOT NULL,
                key_index INTEGER NOT NULL,
                key_label TEXT NOT NULL,
                query TEXT NOT NULL,
                results_count INTEGER NOT NULL,
                duration_ms REAL NOT NULL,
                status TEXT NOT NULL,
                error_kind TEXT,
                error_message TEXT,
                cost_usd REAL
            );
            """
        )
        connection.execute(
            "INSERT INTO search_log ("
            "ts_epoch, ts_iso, provider, key_index, key_label, query,"
            "results_count, duration_ms, status, error_kind, error_message, cost_usd"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _BASE_TS,
                datetime.fromtimestamp(_BASE_TS, tz=UTC).isoformat(),
                "exa",
                0,
                "",
                "historical",
                1,
                10.0,
                "success",
                None,
                None,
                None,
            ),
        )
        connection.commit()
        connection.close()

        store = WebSearchLogStore(db_path)
        store.record(
            _outcome(
                query="correlated",
                route_id="new-route",
                attempt_number=2,
            )
        )
        store.record_route(_route_outcome(route_id="new-route", query="correlated"))
        store.flush()

        rows = store.requests(limit=10)["items"]
        historical = next(row for row in rows if row["query"] == "historical")
        correlated = next(row for row in rows if row["query"] == "correlated")
        assert historical["route_id"] is None
        assert historical["attempt_number"] == 1
        assert correlated["route_id"] == "new-route"
        assert correlated["attempt_number"] == 2
        assert store.stats()["routes"]["totals"]["searches"] == 1

        with sqlite3.connect(db_path) as migrated:
            columns = {
                row[1] for row in migrated.execute("PRAGMA table_info(search_log)")
            }
            route_table = migrated.execute(
                "SELECT name FROM sqlite_master"
                " WHERE type = 'table' AND name = 'search_route_log'"
            ).fetchone()
        assert {
            "route_id",
            "attempt_number",
            "input_json",
            "output_json",
            "provider_config_json",
            "input_chars",
            "output_chars",
            "input_sha256",
            "output_sha256",
            "content_captured",
        } <= columns
        assert route_table == ("search_route_log",)
        store.close()


class TestStorageReclamation:
    @staticmethod
    def _auto_vacuum_mode(path) -> int:
        connection = sqlite3.connect(path)
        try:
            return int(connection.execute("PRAGMA auto_vacuum").fetchone()[0])
        finally:
            connection.close()

    @staticmethod
    def _size_after_checkpoint(path) -> int:
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            connection.close()
        return path.stat().st_size

    @staticmethod
    def _page_count(path) -> int:
        connection = sqlite3.connect(path)
        try:
            return int(connection.execute("PRAGMA page_count").fetchone()[0])
        finally:
            connection.close()

    @staticmethod
    def _freelist_count(path) -> int:
        connection = sqlite3.connect(path)
        try:
            return int(connection.execute("PRAGMA freelist_count").fetchone()[0])
        finally:
            connection.close()

    def test_legacy_database_converts_to_incremental_auto_vacuum(
        self, tmp_path
    ) -> None:
        """An existing auto_vacuum=0 database converts in place, keeping rows."""
        db_path = tmp_path / "legacy-websearch.db"
        seed = WebSearchLogStore(db_path)
        seed.record(_outcome(query="historical"))
        seed.close()

        connection = sqlite3.connect(db_path)
        try:
            connection.isolation_level = None
            connection.execute("PRAGMA auto_vacuum=NONE")
            connection.execute("VACUUM")
        finally:
            connection.close()
        assert self._auto_vacuum_mode(db_path) == 0

        store = WebSearchLogStore(db_path)
        try:
            store.record(_outcome(query="post-migration"))
            store.flush()
            assert self._auto_vacuum_mode(db_path) == 2
            queries = [row["query"] for row in store.requests(limit=10)["items"]]
            assert "historical" in queries
            assert "post-migration" in queries
            connection = sqlite3.connect(db_path)
            try:
                names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            finally:
                connection.close()
            # ANALYZE ran after the rebuild, repopulating planner statistics.
            assert "sqlite_stat1" in names
        finally:
            store.close()

    def test_prune_reclaims_file_space_on_seeded_copy(self, tmp_path) -> None:
        """Row-cap pruning must return bytes, not just grow the freelist."""
        db_path = tmp_path / "websearch.db"
        body = "x" * 20_000
        # Baseline: the steady-state footprint of exactly max_rows fat rows,
        # built independently so the main store's final size has a reference.
        baseline_path = tmp_path / "baseline.db"
        baseline = WebSearchLogStore(baseline_path, max_rows=5, prune_every=1)
        try:
            for _index in range(5):
                baseline.record(_outcome(query=body[:256], input_payload={"raw": body}))
            baseline.flush()
            baseline_pages = self._page_count(baseline_path)
        finally:
            baseline.close()
        store = WebSearchLogStore(db_path, max_rows=5, prune_every=1)
        sizes: list[int] = []
        try:
            for _cycle in range(4):
                for _index in range(30):
                    store.record(
                        _outcome(query=body[:256], input_payload={"raw": body})
                    )
                store.flush()
                assert self._auto_vacuum_mode(db_path) == 2
                sizes.append(self._size_after_checkpoint(db_path))
                # incremental_vacuum drains whatever the prune just freed;
                # SQLite retains only a tiny engine-managed residual.
                assert self._freelist_count(db_path) <= 8
        finally:
            store.close()
        # Later cycles must not keep ratcheting the file upward...
        assert sizes[-1] <= sizes[0] * 2
        # ...and pruning plus incremental_vacuum must pull the file back down
        # toward the steady-state footprint instead of pinning the bulk-phase
        # high-water mark (undrained, it stays ~6x the 5-row baseline).
        assert self._page_count(db_path) <= baseline_pages * 3


class TestEnsureAutoVacuum:
    """Pin ``_ensure_auto_vacuum``'s statement contract on a real connection."""

    def test_already_incremental_skips_the_rebuild(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            statements: list[str] = []
            connection.set_trace_callback(statements.append)

            _ensure_auto_vacuum(connection)

            assert statements == ["PRAGMA auto_vacuum"]
        finally:
            connection.close()

    def test_legacy_database_converts_and_restores_isolation(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            assert int(connection.execute("PRAGMA auto_vacuum").fetchone()[0]) == 0
            previous_isolation = connection.isolation_level
            traced: list[str] = []
            connection.set_trace_callback(traced.append)

            _ensure_auto_vacuum(connection)

            # ``--``-prefixed entries are engine-internal statements (SQLite
            # reads back sqlite_stat1 while implementing ANALYZE), not
            # statements issued by the helper.
            statements = [sql for sql in traced if not sql.startswith("--")]
            assert statements == [
                "PRAGMA auto_vacuum",
                "PRAGMA auto_vacuum=INCREMENTAL",
                "VACUUM",
                "ANALYZE",
            ]
            assert connection.isolation_level == previous_isolation
        finally:
            connection.close()


class TestWriterBehavior:
    def test_full_queue_drops_new_records(self, monkeypatch, tmp_path) -> None:
        release = threading.Event()
        monkeypatch.setattr(
            WebSearchLogStore,
            "_writer_main",
            lambda self: release.wait(5),
        )
        store = WebSearchLogStore(tmp_path / "websearch.db", queue_cap=1)

        assert store.record(_outcome()) is True
        assert store.record(_outcome()) is False
        assert store.dropped == 1

        release.set()
        store.close()

    def test_close_drains_pending_records(self, tmp_path) -> None:
        db_path = tmp_path / "websearch.db"
        store = WebSearchLogStore(db_path)
        for index in range(3):
            store.record(_outcome(query=f"drain {index}", ts_epoch=_BASE_TS + index))

        assert store.close(timeout=5.0) is True
        assert store.record(_outcome()) is False

        connection = sqlite3.connect(db_path)
        try:
            count = connection.execute("SELECT COUNT(*) FROM search_log").fetchone()[0]
        finally:
            connection.close()
        assert count == 3

    def test_flush_after_close_is_a_noop(self, store) -> None:
        store.close()
        store.flush()
