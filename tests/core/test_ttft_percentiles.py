"""Overall p50/p95 time-to-first-token, computed exactly.

There were no ttft percentiles anywhere before 7.10.0: `stats()` only averaged
it, and the histogram writer buckets `duration_ms` alone. 7.5.0's per-model
view samples the 20,000 newest attempt rows and computes percentiles in
Python -- and those per-model figures cannot be composed into an overall one,
because percentiles do not average.

The shape chosen (Q4 (a)) is the exact scan, measured at **0.69-0.99 s** over
331,086 measured rows on a 4.5 GB log against 0.68-0.70 s for the `duration_ms`
scan the dashboard already accepts. The equality contract below is what makes
"exact" a claim rather than a word: the store's answer is compared against a
plain Python computation over the same rows.
"""

import random
from typing import Any

import pytest

from my_claude_code.core.request_log import RequestLogStore, RequestRecord

BASE_TS = 1_760_000_000.0


def _record(request_id: str, **overrides: Any) -> RequestRecord:
    defaults: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-sonnet-4-5",
        "provider": "alpha",
        "resolved_model": "vendor/model",
        "stream": True,
        "input_text": "hello",
        "output_text": "world",
        "tokens_in": 10,
        "tokens_out": 20,
        "duration_ms": 500.0,
        "status": "success",
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


def _python_percentiles(values: list[float]) -> dict[str, float | None]:
    """The same interpolation, written out, over the same rows.

    Deliberately a second implementation rather than a call into the first:
    an oracle that shares the code under test proves only that the code is
    consistent with itself.
    """

    ordered = sorted(values)
    if not ordered:
        return {"p50": None, "p95": None}
    count = len(ordered)
    out: dict[str, float | None] = {}
    for name, fraction in (("p50", 0.50), ("p95", 0.95)):
        position = min(count - 1, max(0.0, fraction * (count - 1)))
        lower = int(position)
        upper = min(count - 1, lower + 1)
        weight = position - lower
        out[name] = round(
            ordered[lower] + (ordered[upper] - ordered[lower]) * weight, 2
        )
    return out


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10_000)
    yield store
    store.close()


class TestTheEqualityContract:
    def test_the_store_agrees_with_a_python_computation(self, store) -> None:
        """THE contract. 500 rows, a shuffled spread, both halves compared."""

        rng = random.Random(20260914)
        values = [round(rng.uniform(1.0, 90_000.0), 1) for _ in range(500)]
        for index, value in enumerate(values):
            store.enqueue(
                _record(f"r{index:04d}", ts_epoch=BASE_TS + index, ttft_ms=value)
            )
        store.close()

        answer = store.ttft_percentiles()
        expected = _python_percentiles(values)

        assert answer["measured"] == 500
        assert answer["p50_ttft_ms"] == pytest.approx(expected["p50"], abs=0.01)
        assert answer["p95_ttft_ms"] == pytest.approx(expected["p95"], abs=0.01)

    def test_unmeasured_rows_are_excluded_not_counted_as_zero(self, store) -> None:
        """A NULL is "not measured". Counting it as 0 would drag both down."""

        measured = [100.0, 200.0, 300.0, 400.0]
        for index, value in enumerate(measured):
            store.enqueue(_record(f"m{index}", ts_epoch=BASE_TS + index, ttft_ms=value))
        for index in range(20):
            store.enqueue(_record(f"u{index}", ts_epoch=BASE_TS + 100 + index))
        store.close()

        answer = store.ttft_percentiles()
        expected = _python_percentiles(measured)

        assert answer["measured"] == 4
        assert answer["p50_ttft_ms"] == pytest.approx(expected["p50"], abs=0.01)
        assert answer["p95_ttft_ms"] == pytest.approx(expected["p95"], abs=0.01)

    def test_a_window_with_nothing_measured_says_so(self, store) -> None:
        """`None` percentiles and a zero denominator -- which a reader can tell
        from "the requests were instant"."""

        for index in range(5):
            store.enqueue(_record(f"n{index}", ts_epoch=BASE_TS + index))
        store.close()

        answer = store.ttft_percentiles()

        assert answer == {
            "p50_ttft_ms": None,
            "p95_ttft_ms": None,
            "measured": 0,
        }

    def test_an_empty_log_answers_the_same_way(self, store) -> None:
        store.close()

        assert store.ttft_percentiles()["measured"] == 0

    def test_one_row_is_its_own_p50_and_p95(self, store) -> None:
        store.enqueue(_record("one", ttft_ms=4242.0))
        store.close()

        answer = store.ttft_percentiles()

        assert answer["p50_ttft_ms"] == 4242.0
        assert answer["p95_ttft_ms"] == 4242.0
        assert answer["measured"] == 1


class TestTheFilters:
    def test_it_honours_the_same_filters_as_stats(self, store) -> None:
        for index in range(10):
            store.enqueue(
                _record(
                    f"a{index}",
                    ts_epoch=BASE_TS + index,
                    provider="alpha",
                    ttft_ms=100.0,
                )
            )
        for index in range(10):
            store.enqueue(
                _record(
                    f"b{index}",
                    ts_epoch=BASE_TS + 100 + index,
                    provider="beta",
                    ttft_ms=9000.0,
                )
            )
        store.close()

        everything = store.ttft_percentiles()
        alpha = store.ttft_percentiles(provider="alpha")
        beta = store.ttft_percentiles(provider="beta")

        assert everything["measured"] == 20
        assert alpha["measured"] == 10
        assert beta["measured"] == 10
        assert alpha["p50_ttft_ms"] == 100.0
        assert beta["p50_ttft_ms"] == 9000.0

    def test_the_window_filter_narrows_it(self, store) -> None:
        for index in range(10):
            store.enqueue(
                _record(
                    f"w{index}",
                    ts_epoch=BASE_TS + index,
                    ttft_ms=100.0 if index < 5 else 9000.0,
                )
            )
        store.close()

        later = store.ttft_percentiles(since=BASE_TS + 5)

        assert later["measured"] == 5
        assert later["p50_ttft_ms"] == 9000.0


class TestTheCache:
    def test_the_answer_is_cached_under_a_key_of_its_own_arity(self, store) -> None:
        """It shares ``stats()``'s cache. A collision with the filter tuple
        ``stats()`` keys on would serve one question's answer to the other."""

        store.enqueue(_record("c1", ttft_ms=123.0))
        store.close()

        first = store.ttft_percentiles()
        stats = store.stats()
        second = store.ttft_percentiles()

        assert first == second
        assert "p50_ttft_ms" not in stats, "stats() is unchanged by this feature"
        keys = [key for key in store._stats_cache if isinstance(key, tuple)]
        ours = [key for key in keys if key and key[0] == "ttft_percentiles"]
        assert len(ours) == 1
        assert all(key[0] != "ttft_percentiles" or key is ours[0] for key in keys)

    def test_a_different_filter_is_a_different_entry(self, store) -> None:
        store.enqueue(_record("c2", provider="alpha", ttft_ms=1.0))
        store.enqueue(_record("c3", provider="beta", ttft_ms=9.0))
        store.close()

        store.ttft_percentiles(provider="alpha")
        store.ttft_percentiles(provider="beta")

        ours = [
            key
            for key in store._stats_cache
            if isinstance(key, tuple) and key and key[0] == "ttft_percentiles"
        ]
        assert len(ours) == 2


class TestThePercentileColumnAllowList:
    def test_only_the_two_latency_columns_are_permitted(self) -> None:
        """The column is interpolated into SQL because a column name cannot be
        a bound parameter; the allow-list is what keeps that safe to read."""

        from my_claude_code.core.request_log import _PERCENTILE_COLUMNS

        assert {"duration_ms", "ttft_ms"} == _PERCENTILE_COLUMNS

    def test_anything_else_is_refused(self, store) -> None:
        store.enqueue(_record("x1", ttft_ms=1.0))
        store.close()

        with (
            store._connection() as conn,
            pytest.raises(ValueError, match="Not a percentile column"),
        ):
            store._percentiles(conn, "", [], (0.5,), column="id; DROP TABLE")
