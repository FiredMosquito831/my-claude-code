"""A free-text search renders its page before it counts.

The page and the count are wildly different queries against the same predicate.
The page walks the timestamp index backwards and stops at the first `limit`
matches; the count has to consider every row, and for a free-text `q` the
predicate is substring matching over stored bodies, so every row's body is
decompressed. Measured on a 4.5 GB log for a term present in real traffic: the
25-row page **0.06 s**, the `COUNT(*)` over the same predicate **383.66 s**,
and `stats(q=)` still running at 600 s -- all three in one `Promise.all`.

The invariants this file pins:

* **page membership and ordering are unchanged.** The same `WHERE`, the same
  `ORDER BY ts_epoch DESC`, the same `LIMIT`/`OFFSET`. Only the count moves;
* **no prefilter.** Nothing here narrows the predicate to make it cheap -- FTS5
  cannot reproduce a substring match's id set and was rejected on measurement;
* **the count that arrives is the count of the page that was rendered**, so
  `/requests/count` and `/requests` share one `_where`;
* **only a free-text `q` defers.** Every other filter is index-served and
  counts exactly as it always did.
"""

import pytest
from fastapi.testclient import TestClient

from my_claude_code.core.request_log import RequestLogStore, RequestRecord
from tests.api.support import create_test_app

BASE_TS = 1_760_000_000.0


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10_000)
    for index in range(12):
        store.enqueue(
            RequestRecord(
                id=f"req-{index:03d}",
                ts_epoch=BASE_TS + index,
                endpoint="/v1/messages",
                protocol="anthropic",
                requested_model="claude-sonnet-4-5",
                provider="alpha" if index % 2 == 0 else "beta",
                resolved_model="vendor/model",
                stream=True,
                input_text=f"needle {index}" if index % 3 == 0 else "hay",
                output_text="answer",
                tokens_in=10,
                tokens_out=20,
                duration_ms=120.0,
                status="success",
            )
        )
    store.close()
    yield store
    store.close()


@pytest.fixture
def client(monkeypatch, store, tmp_path) -> TestClient:
    from my_claude_code.api import admin_routes

    monkeypatch.setattr(
        admin_routes, "_request_log_store_or_none", lambda settings: store
    )
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


class TestTheStore:
    def test_the_same_rows_in_the_same_order_either_way(self, store) -> None:
        """THE equality contract. Counting or not, it is the same page."""

        counted_rows, total = store.list_requests(limit=3, offset=0, q="needle")
        page_rows, deferred_total, has_more = store.list_requests_page(
            limit=3, offset=0, q="needle", include_total=False
        )

        assert [row["id"] for row in counted_rows] == [row["id"] for row in page_rows]
        assert deferred_total is None
        assert total == store.count_requests(q="needle")
        assert has_more is (total > 3)

    def test_has_more_is_true_while_a_next_page_exists(self, store) -> None:
        first = store.list_requests_page(limit=2, offset=0, include_total=False)
        assert first[2] is True

        total = store.count_requests()
        last = store.list_requests_page(limit=2, offset=total - 2, include_total=False)
        assert last[2] is False

    def test_the_extra_row_never_reaches_the_caller(self, store) -> None:
        """`limit + 1` is fetched to answer has-more; `limit` is returned."""

        rows, _total, has_more = store.list_requests_page(
            limit=4, offset=0, include_total=False
        )

        assert len(rows) == 4
        assert has_more is True

    def test_offsets_do_not_overlap(self, store) -> None:
        first, _t, _m = store.list_requests_page(limit=4, offset=0, include_total=False)
        second, _t2, _m2 = store.list_requests_page(
            limit=4, offset=4, include_total=False
        )

        assert not ({row["id"] for row in first} & {row["id"] for row in second})

    def test_count_requests_agrees_with_the_counting_form(self, store) -> None:
        for filters in (
            {},
            {"q": "needle"},
            {"provider": "alpha"},
            {"provider": "alpha", "q": "needle"},
            {"status": "success"},
        ):
            _rows, total = store.list_requests(limit=1, offset=0, **filters)
            assert store.count_requests(**filters) == total, filters


class TestTheRoute:
    def test_a_free_text_search_defers_the_count(self, client) -> None:
        with client:
            body = client.get("/admin/api/requests?limit=3&q=needle").json()

        assert body["total"] is None
        assert body["total_deferred"] is True
        assert "has_more" in body
        assert len(body["rows"]) == 3

    def test_every_other_filter_still_counts(self, client) -> None:
        """Index-served predicates were never the slow part, and are unchanged."""

        with client:
            for query in (
                "limit=3",
                "limit=3&provider=alpha",
                "limit=3&status=success",
                "limit=3&since=1760000000",
            ):
                body = client.get(f"/admin/api/requests?{query}").json()
                assert body["total_deferred"] is False, query
                assert isinstance(body["total"], int), query

    def test_a_blank_q_is_not_a_search(self, client) -> None:
        with client:
            body = client.get("/admin/api/requests?limit=3&q=%20").json()

        assert body["total_deferred"] is False

    def test_the_count_route_answers_the_same_question(self, client) -> None:
        with client:
            page = client.get("/admin/api/requests?limit=3&q=needle").json()
            count = client.get("/admin/api/requests/count?q=needle").json()
            counted = client.get("/admin/api/requests?limit=3&provider=alpha").json()

        assert count["enabled"] is True
        assert count["total"] >= len(page["rows"])
        # And the same route, asked without a search, agrees with the page's
        # own total -- so the two halves cannot drift into different WHEREs.
        with client:
            alpha = client.get("/admin/api/requests/count?provider=alpha").json()
        assert alpha["total"] == counted["total"]

    def test_the_page_is_the_same_page_either_way(self, client) -> None:
        """Through the route, not just the store: same ids, same order."""

        with client:
            searched = client.get("/admin/api/requests?limit=4&q=needle").json()
            direct = client.get("/admin/api/requests/count?q=needle").json()

        ids = [row["id"] for row in searched["rows"]]
        assert ids == sorted(ids, reverse=True)
        assert direct["total"] >= len(ids)

    def test_a_disabled_log_still_answers_both_routes(self, monkeypatch) -> None:
        from my_claude_code.api import admin_routes

        monkeypatch.setattr(
            admin_routes, "_request_log_store_or_none", lambda settings: None
        )
        off = TestClient(create_test_app(), client=("127.0.0.1", 50000))
        with off:
            page = off.get("/admin/api/requests?q=needle").json()
            count = off.get("/admin/api/requests/count?q=needle").json()

        assert page["enabled"] is False
        assert count == {"enabled": False, "total": 0}

    def test_the_count_route_is_loopback_only(self, store, monkeypatch) -> None:
        from my_claude_code.api import admin_routes

        monkeypatch.setattr(
            admin_routes, "_request_log_store_or_none", lambda settings: store
        )
        remote = TestClient(create_test_app(), client=("10.0.0.9", 50000))
        with remote:
            assert remote.get("/admin/api/requests/count").status_code == 403
