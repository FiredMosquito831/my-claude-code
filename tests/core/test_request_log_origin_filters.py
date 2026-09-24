"""The Session and Folder filters and the two origin breakdowns (7.43.0).

Three things are held here:

- **The predicates.** A session is a prefix of the stored id; a folder is an
  exact path when it looks like one and a case-insensitive part of a path
  otherwise; LIKE's wildcards in a typed folder are literal.
- **The arity contract** -- the 6.13.0 trap. Every method that takes the
  filter set takes *all* of it, forwards all of it to ``_where``, and keys its
  cache on all of it; every route forwards all of it. The filter list is read
  off ``_where``'s own signature, so adding a filter there and forgetting it
  anywhere else fails this file.
- **Nothing changes when both are unset.** The SQL, the arguments and every
  view are the ones 7.42.0 produced.

Every path and id in this file is fake.
"""

import ast
import inspect
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.api import admin_export_routes, admin_routes
from my_claude_code.application.derived_payloads import (
    cost_breakdown_cache_key,
    cost_breakdown_entry_name,
)
from my_claude_code.core import request_log as request_log_module
from my_claude_code.core.export import REQUEST_FIELD_IDS, request_detail_columns
from my_claude_code.core.request_log import RequestLogStore, RequestRecord
from my_claude_code.core.request_origin import folder_filter, session_filter

S1 = "0f3c2a1b-6d5e-4f70-9a8b-1c2d3e4f5a6b"
S2 = "7d9e1f20-3a4b-4c5d-8e6f-a0b1c2d3e4f5"
S3 = "c4d5e6f7-0819-4a2b-9c3d-4e5f60718293"
A1 = "a7b8c9d0-1e2f-4a3b-8c4d-5e6f7a8b9c0d"
A2 = "b1c2d3e4-f5a6-4b7c-8d9e-0f1a2b3c4d5e"
DEMO = "C:\\Users\\devuser\\Projects\\demo"
DEMO_OLD = "C:\\Users\\devuser\\Projects\\demo-old"
GAMES = "D:\\work\\games\\Phone games"
ODD = "C:\\Users\\devuser\\Projects\\100%_done!"
BASE_TS = 1_789_000_000.0

#: The filter set, read off the one predicate builder every view shares.
FILTERS: tuple[str, ...] = tuple(
    name
    for name in inspect.signature(RequestLogStore._where).parameters
    if name != "self"
)
#: A value per filter that no other filter's value equals, so a key slot can
#: be told apart from every other slot.
SAMPLE: dict[str, Any] = {
    "provider": "p-sample",
    "model": "m-sample",
    "status": "error",
    "endpoint": "/e-sample",
    "key": "k-sample",
    "since": 11.0,
    "until": 2_000_000_000.0,
    "q": "needle-sample",
    "local": "only",
    "harness": "h-sample",
    "session": "s-sample",
    "folder": "f-sample",
}
#: The rollup's own translation of the predicate. The one exception, on
#: purpose: the rollup has no ``q``, session or folder dimension, and
#: ``stats`` never calls it when any of the three is set.
ROLLUP_TRANSLATIONS = frozenset({"_rollup_where", "_stats_from_rollup"})
#: Callers whose ``store`` is the web-search log, whose iterators share the
#: request log's method names and none of its filters.
OTHER_STORES = frozenset({"_websearch_export"})
#: The store methods that take the filter set and cache their answer.
CACHED_METHODS = (
    "stats",
    "cost_breakdown",
    "ttft_percentiles",
    "cancelled_breakdown",
    "origin_breakdown",
)


def _record(request_id: str, offset: int, **overrides: Any) -> RequestRecord:
    values: dict[str, Any] = {
        "id": request_id,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "ts_epoch": BASE_TS + offset,
        "requested_model": "claude-sonnet-4-5",
        "provider": "nvidia_nim",
        "resolved_model": "test-model",
        "harness": "claude",
        "stream": True,
        "tokens_in": 10,
        "tokens_out": 20,
        "duration_ms": 100.0 + offset,
        "ttft_ms": 30.0,
        "status": "success",
        "input_text": "hi",
    }
    values.update(overrides)
    return RequestRecord(**values)


#: (id, session, agent, folder, status)
ROWS = (
    ("p1", S1, None, DEMO, "success"),
    ("p2", S1, None, DEMO, "error"),
    ("c1", S1, A1, DEMO, "success"),
    ("c2", S1, A1, DEMO, "success"),
    ("c3", S1, A2, DEMO, "success"),
    ("g1", S2, None, GAMES, "success"),
    ("g2", S2, None, GAMES, "success"),
    ("o1", S3, None, DEMO_OLD, "success"),
    ("x1", None, None, ODD, "success"),
    ("sdk", S3, None, None, "success"),
    ("bare", None, None, None, "success"),
)


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10_000)
    for index, (request_id, session, agent, folder, status) in enumerate(ROWS):
        store.enqueue(
            _record(
                request_id,
                index,
                session_id=session,
                agent_id=agent,
                parent_session_id=session if agent else None,
                project_dir=folder,
                status=status,
            )
        )
    store.close()
    yield store
    store.close()


def _ids(store: RequestLogStore, **filters: Any) -> set[str]:
    rows, total, _more = store.list_requests_page(limit=500, **filters)
    assert total == len(rows)
    return {row["id"] for row in rows}


class TestWhereSessionFolderPredicates:
    def test_a_session_is_a_prefix_of_the_stored_id(self, store) -> None:
        s1 = {"p1", "p2", "c1", "c2", "c3"}
        assert _ids(store, session=S1[:8]) == s1
        assert _ids(store, session=S1) == s1
        assert _ids(store, session=f"  {S1[:4]} ") == s1
        assert _ids(store, session=S3[:8]) == {"o1", "sdk"}
        assert _ids(store, session="ffffffff") == set()

    def test_a_full_path_is_that_folder_and_nothing_beside_it(self, store) -> None:
        demo = {"p1", "p2", "c1", "c2", "c3"}
        assert _ids(store, folder=DEMO) == demo
        # A trailing separator is stripped the way a stored value's was.
        assert _ids(store, folder=DEMO + "\\") == demo
        assert _ids(store, folder=GAMES) == {"g1", "g2"}

    def test_anything_else_is_part_of_a_path_in_any_ascii_case(self, store) -> None:
        assert _ids(store, folder="demo") == {"p1", "p2", "c1", "c2", "c3", "o1"}
        assert _ids(store, folder="PHONE games") == {"g1", "g2"}
        assert _ids(store, folder="Projects\\demo-") == {"o1"}

    def test_like_wildcards_typed_into_the_folder_box_are_literal(self, store) -> None:
        assert _ids(store, folder="100%_done!") == {"x1"}
        assert _ids(store, folder="%") == {"x1"}
        assert _ids(store, folder="_") == {"x1"}
        assert _ids(store, folder="!") == {"x1"}

    def test_the_two_combine_with_each_other_and_every_other_filter(
        self, store
    ) -> None:
        assert _ids(store, session=S1[:8], folder=GAMES) == set()
        assert _ids(store, session=S1[:8], status="error") == {"p2"}
        assert _ids(store, folder="demo", since=BASE_TS + 3) == {"c2", "c3", "o1"}
        assert _ids(store, folder="demo", until=BASE_TS + 1) == {"p1", "p2"}

    def test_blank_values_are_no_filter(self, store) -> None:
        everything = {row[0] for row in ROWS}
        assert _ids(store, session="", folder="") == everything
        assert _ids(store, session="   ", folder="  ") == everything
        assert session_filter("  ") is None
        assert folder_filter("") is None
        assert folder_filter(DEMO) == ("exact", DEMO)
        assert folder_filter("games") == ("contains", "games")

    def test_unset_filters_leave_the_sql_as_it_was(self, store) -> None:
        """The origin clauses are appended last and only when set, so every
        query that sets neither is the 7.42.0 SQL, argument for argument."""

        common: dict[str, Any] = {
            "provider": "a,b",
            "model": "m",
            "status": "cancelled",
            "harness": "claude",
            "local": "hide",
            "since": 1.0,
        }
        baseline = store._where(**common)
        assert store._where(**common, session=None, folder=None) == baseline
        assert store._where(**common, session=" ", folder="") == baseline
        assert "rowid IN" not in baseline[0]
        where, args = store._where(**common, session="abc", folder="demo")
        assert where.startswith(baseline[0])
        assert args[: len(baseline[1])] == baseline[1]


class TestTheIndex:
    def test_origin_index_is_partial_and_serves_the_filter_subquery(
        self, store
    ) -> None:
        connection = sqlite3.connect(store.db_path)
        try:
            (sql,) = connection.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'idx_requests_origin_v1'"
            ).fetchone()
            assert "WHERE project_dir IS NOT NULL OR session_id IS NOT NULL" in sql
            where, args = store._where(local="hide", folder="demo", session="0f")
            plan = " | ".join(
                str(row[3])
                for row in connection.execute(
                    f"EXPLAIN QUERY PLAN SELECT COUNT(*) FROM requests{where}", args
                )
            )
        finally:
            connection.close()
        assert "idx_requests_origin_v1" in plan

    def test_it_is_built_on_the_writer_thread_not_in_the_constructor(self) -> None:
        init_source = inspect.getsource(RequestLogStore._init_db)
        writer_source = inspect.getsource(RequestLogStore._writer_loop)
        assert "_ensure_partial_indexes" not in init_source
        assert "_ensure_partial_indexes" in writer_source
        assert any(
            "idx_requests_origin_v1" in statement
            for statement in request_log_module._PARTIAL_INDEXES
        )


class TestTheViewsAgree:
    @pytest.mark.parametrize(
        ("filters", "expected"),
        [
            ({"session": S1[:8]}, 5),
            ({"folder": DEMO}, 5),
            ({"folder": "demo"}, 6),
            ({"session": S1[:8], "status": "error"}, 1),
        ],
    )
    def test_every_view_counts_the_same_rows(self, store, filters, expected) -> None:
        stats = store.stats(**filters)
        assert stats["served_from"] == "rows"
        assert stats["total"] == expected
        assert store.count_requests(**filters) == expected
        assert store.pulse(**filters)["total"] == expected
        assert store.cost_breakdown(**filters)["totals"]["requests"] == expected
        assert store.ttft_percentiles(**filters)["measured"] == expected
        exported = list(
            store.iter_export_rows(
                columns=request_detail_columns(["origin"]),
                need_bodies=False,
                **filters,
            )
        )
        assert len(exported) == expected
        attempts = list(store.iter_export_attempt_rows(**filters))
        assert {row["request_id"] for row in attempts} <= {
            row["id"] for row in store.list_requests_page(limit=500, **filters)[0]
        }

    def test_unfiltered_views_are_byte_identical_with_the_filters_named(
        self, store
    ) -> None:
        def views(**extra: Any) -> str:
            store._stats_cache.clear()
            export = list(
                store.iter_export_rows(
                    columns=request_detail_columns(REQUEST_FIELD_IDS),
                    need_bodies=False,
                    **extra,
                )
            )
            return json.dumps(
                {
                    "stats": store.stats(**extra),
                    "pulse": store.pulse(**extra),
                    "cost": store.cost_breakdown(**extra),
                    "ttft": store.ttft_percentiles(**extra),
                    "cancelled": store.cancelled_breakdown(**extra),
                    "page": store.list_requests_page(limit=100, **extra)[0],
                    "export": export,
                },
                sort_keys=True,
                default=str,
            )

        baseline = views()
        assert views(session=None, folder=None) == baseline
        assert views(session="", folder="  ") == baseline
        assert json.loads(baseline)["stats"]["served_from"] == "rollup"


class TestTheOriginBreakdown:
    def test_breakdowns_count_only_rows_that_carry_the_value(self, store) -> None:
        result = store.origin_breakdown()
        folders = {row["key"]: row for row in result["by_folder"]}
        assert set(folders) == {DEMO, GAMES, DEMO_OLD, ODD}
        assert folders[DEMO]["requests"] == 5
        assert folders[DEMO]["sessions"] == 1
        assert folders[DEMO]["errors"] == 1
        assert folders[DEMO]["short"].startswith("Projects\\demo · #")
        assert folders[ODD]["sessions"] == 0
        assert result["by_folder_truncated"] is False

    def test_sessions_are_flat_with_subagent_counts(self, store) -> None:
        result = store.origin_breakdown()
        sessions = {row["key"]: row for row in result["by_session"]}
        assert set(sessions) == {S1, S2, S3}
        first = sessions[S1]
        assert first["requests"] == 5
        assert first["subagent_requests"] == 3
        assert first["subagents"] == 2
        assert first["folder"] == DEMO
        assert first["folders"] == 1
        assert first["short"] == S1[:8]
        assert first["last_ts"] == BASE_TS + 4
        # The Agent SDK row states a session and no folder.
        assert sessions[S3]["folders"] == 1
        assert sessions[S3]["requests"] == 2
        # Flat: one row per stated id, never a child folded into a parent.
        assert len(result["by_session"]) == 3

    def test_it_honours_every_filter_including_its_own(self, store) -> None:
        narrowed = store.origin_breakdown(folder="demo")
        assert {row["key"] for row in narrowed["by_folder"]} == {DEMO, DEMO_OLD}
        assert {row["key"] for row in narrowed["by_session"]} == {S1, S3}
        errors = store.origin_breakdown(status="error")
        assert [row["key"] for row in errors["by_session"]] == [S1]
        assert store.origin_breakdown(since=BASE_TS + 100)["by_folder"] == []

    def test_breakdown_by_folder_truncation_flag(self, tmp_path) -> None:
        limit = request_log_module._BREAKDOWN_LIMIT
        store = RequestLogStore(tmp_path / "many.db", max_rows=10_000)
        try:
            for index in range(limit + 3):
                store.enqueue(
                    _record(
                        f"r{index}",
                        index,
                        project_dir=f"C:\\work\\folder{index:03d}",
                        session_id=f"{index:08d}-0000-4000-8000-000000000000",
                    )
                )
            store.close()
            result = store.origin_breakdown()
        finally:
            store.close()
        assert len(result["by_folder"]) == limit
        assert result["by_folder_truncated"] is True
        assert len(result["by_session"]) == limit
        assert result["by_session_truncated"] is True

    def test_a_cached_answer_is_a_copy(self, store) -> None:
        first = store.origin_breakdown()
        first["by_folder"][0]["requests"] = -1
        first["by_folder"].clear()
        again = store.origin_breakdown()
        assert again["by_folder"] and again["by_folder"][0]["requests"] > 0


class TestEveryStatsCallerPassesFullFilterArity:
    """The 6.13.0 guard, reflected rather than spelled out.

    ``FILTERS`` comes from ``_where``'s signature, so adding a filter there
    without threading it through every caller, every cache key and every
    route fails one of these.
    """

    def test_the_filter_set_is_what_this_release_expects(self) -> None:
        assert tuple(SAMPLE) == FILTERS

    def test_every_filter_taking_store_method_takes_all_of_them(self) -> None:
        takers = []
        for name, member in inspect.getmembers(RequestLogStore, inspect.isfunction):
            parameters = set(inspect.signature(member).parameters)
            if name in ROLLUP_TRANSLATIONS:
                continue
            if "harness" in parameters and "local" in parameters:
                takers.append(name)
                missing = set(FILTERS) - parameters
                assert not missing, f"{name} lacks {sorted(missing)}"
        assert "origin_breakdown" in takers
        assert "stats" in takers

    @staticmethod
    def _calls(path: Path) -> list[tuple[str, str, set[str], bool]]:
        """(enclosing function, callee, keyword names, has **kwargs) per call."""

        tree = ast.parse(path.read_text(encoding="utf-8"))
        found: list[tuple[str, str, set[str], bool]] = []

        def callee_name(node: ast.Call) -> str | None:
            target = node.func
            if isinstance(target, ast.Attribute):
                return target.attr
            if isinstance(target, ast.Name):
                return target.id
            return None

        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.Call):
                    continue
                name = callee_name(node)
                # ``asyncio.to_thread(store.stats, ...)`` calls ``stats``.
                if (
                    name == "to_thread"
                    and node.args
                    and isinstance(node.args[0], ast.Attribute)
                ):
                    name = node.args[0].attr
                if name is None:
                    continue
                keywords = {keyword.arg for keyword in node.keywords if keyword.arg}
                splat = any(keyword.arg is None for keyword in node.keywords)
                found.append((function.name, name, keywords, splat))
        return found

    def test_every_call_site_forwards_the_whole_set(self) -> None:
        full_takers = {
            name
            for name, member in inspect.getmembers(RequestLogStore, inspect.isfunction)
            if set(FILTERS) <= set(inspect.signature(member).parameters)
        }
        full_takers.add("_where")
        checked = 0
        for module in (request_log_module, admin_routes, admin_export_routes):
            path = Path(inspect.getfile(module))
            for caller, callee, keywords, splat in self._calls(path):
                if callee not in full_takers or splat or caller in OTHER_STORES:
                    continue
                if callee == "_where" and caller == "optimization_stats":
                    # Deliberately window-only: the optimization panel is not
                    # filtered by the Analytics toolbar at all.
                    assert keywords == {"since", "until"}
                    continue
                if not (keywords & (set(FILTERS) - {"since", "until"})):
                    # A call that passes no toolbar filter at all is a
                    # different question (``stats()`` for a card elsewhere).
                    continue
                missing = set(FILTERS) - keywords
                assert not missing, (
                    f"{path.name}:{caller} calls {callee} without {sorted(missing)}"
                )
                checked += 1
        # 13 in the store (12 ``_where`` callers and ``stats`` into the row
        # scan), 7 routes, 3 export helpers. The cost route forwards
        # ``**filters``, whose keys the next test but one pins.
        assert checked >= 23

    def test_every_route_that_takes_a_filter_takes_all_of_them(self) -> None:
        for module in (admin_routes, admin_export_routes):
            for name, member in inspect.getmembers(module, inspect.isfunction):
                if member.__module__ != module.__name__:
                    continue
                parameters = set(inspect.signature(member).parameters)
                if "harness" not in parameters:
                    continue
                missing = {"session", "folder"} - parameters
                assert not missing, f"{module.__name__}.{name} lacks {missing}"

    def test_the_cost_route_keys_its_stored_breakdown_on_every_filter(self) -> None:
        tree = ast.parse(Path(inspect.getfile(admin_routes)).read_text("utf-8"))
        (route,) = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "request_log_cost"
        ]
        (filters,) = [
            node.value
            for node in ast.walk(route)
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "filters"
        ]
        assert isinstance(filters, ast.Dict)
        keys = {key.value for key in filters.keys if isinstance(key, ast.Constant)}
        assert keys == set(FILTERS)

    @pytest.mark.parametrize("method", CACHED_METHODS)
    def test_every_filter_owns_a_slot_in_every_cache_key(self, store, method) -> None:
        call = getattr(store, method)
        store._stats_cache.clear()
        call()
        with store._stats_lock:
            (unset_key,) = list(store._stats_cache)
        for name in FILTERS:
            store._stats_cache.clear()
            call(**{name: SAMPLE[name]})
            with store._stats_lock:
                (key,) = list(store._stats_cache)
            assert len(key) == len(unset_key), (method, name)
            differing = [
                index
                for index, (left, right) in enumerate(zip(unset_key, key, strict=True))
                if left != right
            ]
            assert len(differing) == 1, (method, name, key)
            assert key[differing[0]] == SAMPLE[name], (method, name)

    def test_cost_breakdown_cache_key_includes_origin_filters(self, store) -> None:
        unset = dict.fromkeys(FILTERS)
        baseline = cost_breakdown_cache_key(store, **unset)
        seen = {baseline}
        for name in FILTERS:
            key = cost_breakdown_cache_key(store, **{**unset, name: SAMPLE[name]})
            assert key not in seen, name
            seen.add(key)
        assert "session=" in baseline and "folder=" in baseline
        # Only the page's opening question is stored on disk; a session or a
        # folder is a question asked once, answered live.
        assert cost_breakdown_entry_name(**unset) == "cost-breakdown"
        assert cost_breakdown_entry_name(**{**unset, "local": "hide"}) == (
            "cost-breakdown-local-hide"
        )
        for name in ("session", "folder"):
            assert cost_breakdown_entry_name(**{**unset, name: SAMPLE[name]}) is None
