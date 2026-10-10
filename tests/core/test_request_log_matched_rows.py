"""A search's matched rows answer every read exactly as the predicate does (7.91.1).

Until 7.91.1 every answer for a free-text search ran the body predicate itself:
27 passes over every stored body in the window for one page load. Now a search
is read once (``RequestLogStore.match_rows``) and every answer is handed the
rows it found (``MatchedRows``) in the predicate's place. That is only
acceptable if nothing a search finds changes, so this file pins, for a corpus
of searches (common, rare, absent, mixed case, wildcards, quotes, backslashes,
tool-call-only, 1-2 characters, non-ASCII) over windows and filters:

* every read method returns the identical answer -- the same rows in the same
  order, the same counts, the same payload -- from the rows as from the
  predicate;
* the search's rows replace the predicate in its place and nothing else moves,
  and the query plan's outer loop is unchanged (``+rowid``), which is what
  keeps rows that share a timestamp in the same order;
* the copy of the rows ``_rows_once`` makes for a many-statement answer is the
  same answer again.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest

from my_claude_code.application.search_jobs import _base_status
from my_claude_code.core import export as export_engine
from my_claude_code.core.request_log import (
    _MATCHED_ROWS_SQL,
    _MATCHED_TABLE_SQL,
    MatchedRows,
    RequestLogStore,
    normalized_search,
)
from tests.support.search_log import CORPUS, build_search_log

_PASS_FILTERS = (
    "provider",
    "model",
    "endpoint",
    "key",
    "local",
    "harness",
    "session",
    "folder",
)

FILTERS: tuple[dict[str, Any], ...] = (
    {"local": "hide"},
    {},
    {"local": "only"},
    {"local": "hide", "status": "error"},
    {"local": "hide", "status": "success:empty"},
    {"local": "hide", "status": "cancelled:client_gave_up_waiting"},
    {"provider": "zen,anthropic_oauth", "harness": "claude"},
    {"key": "sk-a…1111", "endpoint": "/v1/messages"},
    {"exit": "tor"},
    {"folder": "alpha"},
    {"session": "sess-03"},
)


@pytest.fixture(scope="module")
def log(tmp_path_factory) -> Iterator[tuple[RequestLogStore, list[float]]]:
    path = tmp_path_factory.mktemp("matched") / "requests.db"
    store, times = build_search_log(path, rows=160)
    yield store, times
    store.close()


def _matched(store: RequestLogStore, q: str, filters: dict[str, Any]) -> MatchedRows:
    """The rows a search pass finds, the way ``SearchJobs`` asks for them."""

    picked: dict[str, Any] = {name: filters.get(name) for name in _PASS_FILTERS}
    rows = store.match_rows(
        q=q,
        **picked,
        status=_base_status(filters.get("status")),
        since=filters.get("since"),
        until=filters.get("until"),
    )
    return MatchedRows(
        q=normalized_search(q), rowids=json.dumps(sorted({rowid for rowid, _ in rows}))
    )


def _fresh(store: RequestLogStore) -> None:
    # The five-second answer cache would hand the second call the first
    # call's answer; each side must compute its own.
    with store._stats_lock:
        store._stats_cache.clear()


def _answers(
    store: RequestLogStore, filters: dict[str, Any], matched: MatchedRows | None
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for limit, offset in ((25, 0), (7, 3), (50, 25)):
        rows, total, has_more = store.list_requests_page(
            limit=limit,
            offset=offset,
            include_total=False,
            include_exits=True,
            matched=matched,
            **filters,
        )
        out[f"list{limit}/{offset}"] = (rows, total, has_more)
    for name in (
        "count_requests",
        "stats",
        "cost_breakdown",
        "ttft_percentiles",
        "cancelled_breakdown",
        "no_answer_breakdown",
        "pulse",
        "origin_breakdown",
    ):
        _fresh(store)
        out[name] = getattr(store, name)(matched=matched, **filters)
    out["export"] = list(
        store.iter_export_rows(
            columns=export_engine.request_detail_columns(
                list(export_engine.DEFAULT_REQUEST_FIELDS)
            ),
            need_bodies=False,
            need_exits=True,
            page_size=9,
            matched=matched,
            **filters,
        )
    )
    out["attempts"] = list(
        store.iter_export_attempt_rows(page_size=9, matched=matched, **filters)
    )
    out["grouped"] = list(
        store.iter_export_aggregates(
            select="provider, status, COUNT(*) AS n",
            names=["provider", "status", "n"],
            group_by=["provider", "status"],
            matched=matched,
            **filters,
        )
    )
    return out


def _windows(times: list[float]) -> list[float | None]:
    newest = max(times)
    return [None, newest - 86400 * 0.3, newest - 86400 * 0.15]


@pytest.mark.parametrize("q", CORPUS)
def test_every_answer_is_the_predicates_own_for_the_dashboard_filters(log, q) -> None:
    store, times = log
    for since in _windows(times):
        filters = {"q": q, "local": "hide", "since": since}
        expected = _answers(store, filters, None)
        actual = _answers(store, filters, _matched(store, q, filters))
        assert actual == expected, (q, since)


@pytest.mark.parametrize("extra", FILTERS, ids=lambda extra: json.dumps(extra))
@pytest.mark.parametrize("q", ("too long", "x", "ș", "toolu_", "zqxjvkw"))
def test_every_answer_is_the_predicates_own_under_every_filter(log, q, extra) -> None:
    store, times = log
    for since in _windows(times)[:2]:
        filters = {"q": q, "since": since, **extra}
        expected = _answers(store, filters, None)
        actual = _answers(store, filters, _matched(store, q, filters))
        assert actual == expected, (q, extra, since)


def test_the_rows_hold_a_search_that_matches_somewhere_and_not_everywhere(
    log,
) -> None:
    """The corpus is not vacuous: the identity above compared real answers."""

    store, _times = log
    total = store.count_requests()
    counts = {q: store.count_requests(q=q) for q in CORPUS}
    assert counts["zqxjvkw"] == 0
    assert 0 < counts["too long"] < total
    assert 0 < counts["Ă"] < counts["ăsta"] + counts["Ă"]
    assert counts["README.md"] == counts["ReadMe.MD"]
    assert counts["  too   long  "] == counts["too long"]
    assert counts["C:\\Users\\dev\\proj"] > 0
    assert sum(1 for count in counts.values() if 0 < count < total) >= 15


def test_the_rows_stand_in_the_predicates_place_and_nothing_else_moves(log) -> None:
    store, times = log
    filters = {
        "q": "too long",
        "local": "hide",
        "provider": "zen",
        "since": min(times),
        "folder": "alpha",
        "exit": "tor",
    }
    matched = MatchedRows(q="too long", rowids="[1, 2, 3]")
    where, args = store._where(**filters)
    swapped, swapped_args = store._where(**filters, matched=matched)
    head, _, _rest = where.partition("((")
    assert swapped.startswith(head)
    assert swapped.count(_MATCHED_ROWS_SQL) == 1
    position = swapped[: swapped.index(_MATCHED_ROWS_SQL)].count("?")
    assert swapped_args[position] is matched.rowids
    # Every argument before and after the predicate's own is where it was.
    assert swapped_args[:position] == args[:position]
    predicate_args = 2 * 4 + 1
    assert swapped_args[position + 1 :] == args[position + predicate_args :]
    # A set for another search is never used for this one.
    other = MatchedRows(q="too", rowids="[]")
    assert store._where(**filters, matched=other) == (where, args)


def _plan(store: RequestLogStore, sql: str, args: list[Any]) -> list[str]:
    with store._connection() as conn:
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}", args).fetchall()
    # The outer loop only: the lines of the predicate's own subquery, or of the
    # rows' lookup, are what was swapped. "COVERING" is not a different walk:
    # the same index, in the same order; the rows' lookup reads only the
    # rowid, so it can skip the table row the body predicate had to open.
    return [
        str(row[3]).replace("USING COVERING INDEX", "USING INDEX")
        for row in rows
        if int(row[1]) == 0
    ]


@pytest.mark.parametrize(
    "extra",
    ({"local": "hide"}, {}, {"provider": "zen"}, {"harness": "codex"}, {"folder": "a"}),
)
def test_the_outer_query_plan_is_the_one_the_predicate_had(log, extra) -> None:
    """``+rowid``: the rows never drive the plan, so ties keep their order."""

    store, times = log
    filters = {"q": "too long", "since": min(times), **extra}
    where, args = store._where(**filters)
    swapped, swapped_args = store._where(
        **filters, matched=_matched(store, "too long", filters)
    )
    for shape in (
        "SELECT id FROM requests{w} ORDER BY ts_epoch DESC LIMIT 26 OFFSET 0",
        "SELECT COUNT(*) FROM requests{w}",
        "SELECT provider, COUNT(*) FROM requests{w} GROUP BY provider",
    ):
        before = _plan(store, shape.format(w=where), args)
        after = _plan(store, shape.format(w=swapped), swapped_args)
        assert [line for line in after if "SUBQUERY" not in line] == [
            line for line in before if "SUBQUERY" not in line
        ], shape


def test_the_copy_for_many_statements_is_the_same_answer(log) -> None:
    store, times = log
    filters = {"q": "x", "local": "hide", "since": min(times)}
    matched = _matched(store, "x", filters)
    where, args = store._where(**filters, matched=matched)
    with store._connection() as conn:
        copied, copied_args = store._rows_once(conn, where, args, matched)
        assert _MATCHED_TABLE_SQL in copied and _MATCHED_ROWS_SQL not in copied
        assert len(copied_args) == len(args) - 1
        listed = [
            row[0]
            for row in conn.execute(
                f"SELECT id FROM requests{where} ORDER BY ts_epoch DESC", args
            )
        ]
        from_copy = [
            row[0]
            for row in conn.execute(
                f"SELECT id FROM requests{copied} ORDER BY ts_epoch DESC", copied_args
            )
        ]
    assert from_copy == listed
    assert len(listed) == store.count_requests(**filters)
    # Without a set the predicate comes back exactly as it went in.
    plain, plain_args = store._where(**filters)
    with store._connection() as conn:
        assert store._rows_once(conn, plain, plain_args, None) == (plain, plain_args)


def test_a_pass_reads_newest_first_and_continues_below_a_point(log) -> None:
    store, _times = log
    everything = list(store.match_rows(q="x"))
    stamps = [ts for _rowid, ts in everything]
    assert stamps == sorted(stamps, reverse=True)
    middle = stamps[len(stamps) // 2]
    upper = list(store.match_rows(q="x", since=middle))
    lower = list(store.match_rows(q="x", below=middle))
    assert sorted(upper + lower) == sorted(everything)
    assert all(ts >= middle for _rowid, ts in upper)
    assert all(ts < middle for _rowid, ts in lower)
    # Inclusive below: the rows at the point itself are read again.
    again = list(store.match_rows(q="x", below=middle, below_inclusive=True))
    assert {row for row in again if row[1] == middle} == {
        row for row in upper if row[1] == middle
    }
    # The rows written after a pass: a rowid range, any time.
    top = store.max_rowid()
    assert list(store.match_rows(q="x", rowid_after=top)) == []
    assert sorted(store.match_rows(q="x", rowid_after=0, rowid_through=top)) == sorted(
        everything
    )


def test_a_clear_moves_the_generation_the_rows_are_kept_under(tmp_path) -> None:
    store, _times = build_search_log(tmp_path / "requests.db", rows=12)
    before = store.clear_generation
    store.clear()
    assert store.clear_generation == before + 1
    assert store.max_rowid() == 0


def test_the_search_mark_names_only_what_the_filters_read(log) -> None:
    store, _times = log
    assert store.search_mark({}) == ""
    assert store.search_mark({"provider": "zen", "local": "all"}) == ""
    assert store.search_mark({"local": "hide"}) != ""
    assert store.search_mark({"harness": "claude"}) != ""
    assert store.search_mark({"folder": "alpha"}) != store.search_mark(
        {"local": "hide"}
    )
