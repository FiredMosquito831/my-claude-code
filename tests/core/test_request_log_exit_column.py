"""The Requests table's Exit column (7.88.0), read from what the log holds.

The Folder column became Exit. Nothing new is captured and nothing is written:
the exit is read from ``request_attempts.proxy_label`` (the exit each attempt
ended on), ``params.ladder.dials`` (every exit a chain dialled on the way, kept
since 7.45.1 and walked by the 7.81.0 rotation) and, for a media request whose
attempts record none, its video job's ``media_jobs.proxy_label``.

Held here:

- **The row field.** ``list_requests_page(include_exits=True)`` adds one key,
  ``exit`` = ``{"label", "tried"}``, and changes no other key of any row.
  Without the flag the rows are exactly what they were.
- **Every kind of stored exit** comes back as the log wrote it: a chain
  entry's name, a one-entry chain's or static proxy's ``host:port``,
  ``direct``, ``direct via system proxy host:port``; NULL stays ``None``.
- **Masking.** A credential that ever reached a label never leaves.
- **The filter** matches any exit a request went out through, applies to
  every view the toolbar drives, and leaves the SQL alone when unset.
- **The export** gains the Exit columns only when asked.

Every address is a documentation address (RFC 5737) or a made-up name, and
every "credential" here is fake.
"""

import sqlite3
from typing import Any

import pytest

from my_claude_code.core.export import (
    DEFAULT_REQUEST_FIELDS,
    REQUEST_FIELD_IDS,
    request_detail_columns,
    request_detail_derived_columns,
    request_detail_headers,
)
from my_claude_code.core.proxy_attribution import (
    MASKED_EXIT_LABEL,
    exit_filter,
    masked_exit_label,
)
from my_claude_code.core.request_log import (
    MediaJobRecord,
    RequestLogStore,
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
)

BASE_TS = 1_790_000_000.0
SYSTEM = "direct via system proxy 127.0.0.1:7890"


def _dials(*labels: str) -> dict[str, Any]:
    """An attempt's params as the capture writes them: dials on the ladder."""
    rows = [
        {"at_try": index, "proxy": label, "outcome": "switched"}
        for index, label in enumerate(labels)
    ]
    if rows:
        rows[-1]["outcome"] = "answered"
    return {
        "ladder": {
            "tries": [
                {"source": "upstream", "status": 200, "proxy": labels[-1]},
            ],
            "summary": {"tries": 1},
            "credentials": [],
            "dials": rows,
        }
    }


def _attempt(
    index: int,
    label: str | None,
    *,
    outcome: RouteAttemptOutcome = RouteAttemptOutcome.SUCCEEDED,
    params: dict[str, Any] | None = None,
) -> RouteAttempt:
    return RouteAttempt(
        attempt=index,
        provider="opencode",
        model_ref=f"opencode/model-{index}",
        outcome=outcome,
        params=params,
        proxy_label=label,
    )


def _record(
    request_id: str,
    offset: int,
    attempts: tuple[RouteAttempt, ...] = (),
    **overrides: Any,
) -> RequestRecord:
    values: dict[str, Any] = {
        "id": request_id,
        "ts_epoch": BASE_TS + offset,
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "requested_model": "claude-sonnet-4-5",
        "provider": "opencode",
        "resolved_model": "model",
        "harness": "claude",
        "stream": True,
        "tokens_in": 10,
        "tokens_out": 20,
        "duration_ms": 100.0 + offset,
        "ttft_ms": 30.0,
        "status": "success",
        "route_attempt": attempts[-1].attempt if attempts else None,
        "attempts": attempts,
    }
    values.update(overrides)
    return RequestRecord(**values)


#: request id -> (expected label, expected tried)
EXPECTED: dict[str, tuple[str | None, list[str]]] = {
    "chain": ("Tokyo exit", ["Tokyo exit"]),
    "one-entry": ("203.0.113.7:1080", ["203.0.113.7:1080"]),
    "static": ("198.51.100.4:3128", ["198.51.100.4:3128"]),
    "direct": ("direct", ["direct"]),
    "system": (SYSTEM, [SYSTEM]),
    "none": (None, []),
    "bare": (None, []),
    "rotated": (
        "exit-c.example:1080",
        ["exit-a.example:1080", "exit-b.example:1080", "exit-c.example:1080"],
    ),
    "fallback": (None, ["exit-a.example:1080"]),
    "masked": ("203.0.113.9:1080", ["198.51.100.2:8080", "203.0.113.9:1080"]),
    "media": ("Tokyo exit", ["Tokyo exit"]),
    "media-none": (None, []),
    "skipped-first": ("203.0.113.7:1080", ["203.0.113.7:1080"]),
    # A dial that never completed: no try, so no stored label, on a failed
    # attempt; the next model answered with no chain.
    "hung": (None, ["exit-h.example:1080"]),
}

HUNG_DIAL = {
    "ladder": {
        "tries": [],
        "summary": {"tries": 0},
        "credentials": [],
        "dials": [{"at_try": 0, "proxy": "exit-h.example:1080", "outcome": "dialing"}],
    }
}


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db", max_rows=10_000)
    records = [
        _record("chain", 0, (_attempt(0, "Tokyo exit", params=_dials("Tokyo exit")),)),
        _record("one-entry", 1, (_attempt(0, "203.0.113.7:1080"),)),
        _record(
            "static",
            2,
            (_attempt(0, "198.51.100.4:3128", outcome=RouteAttemptOutcome.FAILED),),
            status="error",
        ),
        _record("direct", 3, (_attempt(0, "direct"),)),
        _record("system", 4, (_attempt(0, SYSTEM),)),
        _record("none", 5, (_attempt(0, None),)),
        _record("bare", 6),
        _record(
            "rotated",
            7,
            (
                _attempt(
                    0,
                    "exit-c.example:1080",
                    params=_dials(
                        "exit-a.example:1080",
                        "exit-b.example:1080",
                        "exit-c.example:1080",
                    ),
                ),
            ),
        ),
        # Attempt 0 through a chain failed; attempt 1 on a provider with no
        # chain answered. The answering exit is "none recorded".
        _record(
            "fallback",
            8,
            (
                _attempt(
                    0,
                    "exit-a.example:1080",
                    outcome=RouteAttemptOutcome.FAILED,
                    params=_dials("exit-a.example:1080"),
                ),
                _attempt(1, None),
            ),
        ),
        # A label that should never have been written with a credential in it.
        _record(
            "masked",
            9,
            (
                _attempt(
                    0,
                    "alice:s3cretpw@203.0.113.9:1080",
                    params=_dials(
                        "socks5h://bob:hunter2x@198.51.100.2:8080",
                        "alice:s3cretpw@203.0.113.9:1080",
                    ),
                ),
            ),
        ),
        _record(
            "media",
            10,
            (_attempt(0, None),),
            endpoint="/v1/videos",
            params={"media": {"operation": "video"}},
        ),
        _record(
            "media-none",
            11,
            (_attempt(0, None),),
            endpoint="/v1/images/generations",
            params={"media": {"operation": "image"}},
        ),
        _record(
            "skipped-first",
            12,
            (
                _attempt(0, None, outcome=RouteAttemptOutcome.SKIPPED),
                _attempt(1, "203.0.113.7:1080"),
            ),
        ),
        _record(
            "hung",
            13,
            (
                _attempt(0, None, outcome=RouteAttemptOutcome.FAILED, params=HUNG_DIAL),
                _attempt(1, None),
            ),
        ),
    ]
    for record in records:
        store.enqueue(record)
    store.close()
    store.insert_media_job(
        MediaJobRecord(
            job_id="job-1",
            request_id="media",
            provider="opencode",
            model="video-model",
            upstream_id="up-1",
            created_at=BASE_TS + 10,
            proxy_label="Tokyo exit",
        )
    )
    yield store
    store.close()


def _page(store: RequestLogStore, **filters: Any) -> list[dict[str, Any]]:
    rows, _total, _more = store.list_requests_page(
        limit=500, include_exits=True, **filters
    )
    return rows


def _ids(store: RequestLogStore, **filters: Any) -> set[str]:
    rows, total, _more = store.list_requests_page(limit=500, **filters)
    assert total == len(rows)
    return {row["id"] for row in rows}


class TestTheRowField:
    def test_every_kind_of_stored_exit_comes_back_as_written(self, store) -> None:
        exits = {row["id"]: row["exit"] for row in _page(store)}

        assert set(exits) == set(EXPECTED)
        for request_id, (label, tried) in EXPECTED.items():
            assert exits[request_id] == {"label": label, "tried": tried}, request_id

    def test_the_flag_adds_one_key_and_changes_no_other(self, store) -> None:
        """The equality half: the page the table read before 7.88.0 is the
        page it reads now, plus ``exit`` on each row."""

        plain, total, more = store.list_requests_page(limit=500)
        with_exits, total_x, more_x = store.list_requests_page(
            limit=500, include_exits=True
        )

        assert (total, more) == (total_x, more_x)
        assert all("exit" not in row for row in plain)
        assert [
            {key: value for key, value in row.items() if key != "exit"}
            for row in with_exits
        ] == plain

    def test_no_credential_leaves_the_store(self, store) -> None:
        text = repr(_page(store))

        for secret in ("alice", "s3cretpw", "bob", "hunter2x", "@", "socks5h://"):
            assert secret not in text, secret

    def test_an_unreadable_params_value_reads_as_no_dials(self, store) -> None:
        connection = sqlite3.connect(store.db_path)
        try:
            connection.execute(
                "UPDATE request_attempts SET params = ? WHERE request_id = 'rotated'",
                ('{"ladder": {"dials": [broken',),
            )
            connection.commit()
        finally:
            connection.close()

        exits = {row["id"]: row["exit"] for row in _page(store)}

        assert exits["rotated"] == {
            "label": "exit-c.example:1080",
            "tried": ["exit-c.example:1080"],
        }
        assert _ids(store, exit="exit-b") == set()
        assert _ids(store, exit="exit-c") == {"rotated"}


class TestTheFilter:
    def test_it_matches_any_exit_the_request_went_out_through(self, store) -> None:
        assert _ids(store, exit="Tokyo") == {"chain", "media"}
        # Only ever a dial on the way: the stored label is exit-c.
        assert _ids(store, exit="exit-b") == {"rotated"}
        # A failed attempt's exit, on a request another attempt answered.
        assert _ids(store, exit="exit-a") == {"rotated", "fallback"}
        assert _ids(store, exit="direct") == {"direct", "system"}
        assert _ids(store, exit="system proxy") == {"system"}
        # A dial that never completed is found too: a failed attempt's JSON is
        # read even though it stored no exit.
        assert _ids(store, exit="exit-h") == {"hung"}
        assert _ids(store, exit="1080") == {
            "rotated",
            "fallback",
            "one-entry",
            "masked",
            "skipped-first",
            "hung",
        }

    def test_it_ignores_case_and_treats_wildcards_literally(self, store) -> None:
        assert _ids(store, exit="TOKYO EXIT") == {"chain", "media"}
        assert _ids(store, exit="%") == set()
        assert _ids(store, exit="_") == set()

    def test_blank_is_no_filter(self, store) -> None:
        everything = set(EXPECTED)
        assert _ids(store, exit="") == everything
        assert _ids(store, exit="   ") == everything
        assert exit_filter("  ") is None
        assert exit_filter(None) is None
        assert exit_filter(" Tokyo ") == "Tokyo"

    def test_it_combines_with_the_window_and_every_other_filter(self, store) -> None:
        assert _ids(store, exit="1080", since=BASE_TS + 9) == {
            "masked",
            "skipped-first",
            "hung",
        }
        assert _ids(store, exit="198.51.100.4", status="error") == {"static"}
        assert _ids(store, exit="198.51.100.4", status="success") == set()

    def test_every_view_counts_the_same_rows(self, store) -> None:
        for value, expected in (("Tokyo", 2), ("1080", 6), ("direct", 2)):
            stats = store.stats(exit=value)
            assert stats["served_from"] == "rows"
            assert stats["total"] == expected
            assert store.count_requests(exit=value) == expected
            assert store.pulse(exit=value)["total"] == expected
            assert store.cost_breakdown(exit=value)["totals"]["requests"] == expected
            assert store.ttft_percentiles(exit=value)["measured"] == expected

    def test_unset_leaves_the_sql_as_it_was(self, store) -> None:
        common: dict[str, Any] = {
            "provider": "a,b",
            "status": "cancelled",
            "local": "hide",
            "since": 1.0,
            "folder": "demo",
        }
        baseline = store._where(**common)
        assert store._where(**common, exit=None) == baseline
        assert store._where(**common, exit="  ") == baseline
        assert "request_attempts" not in baseline[0]
        where, args = store._where(**common, exit="tokyo")
        assert where.startswith(baseline[0])
        assert args[: len(baseline[1])] == baseline[1]


class TestTheExport:
    def test_the_exit_columns_are_offered_and_opt_in(self) -> None:
        assert "exit" in REQUEST_FIELD_IDS
        assert "exit" not in DEFAULT_REQUEST_FIELDS
        # Beside Request origin, which stays the last group.
        assert REQUEST_FIELD_IDS[-2:] == ("exit", "origin")
        assert request_detail_columns(["exit"]) == request_detail_columns([])
        derived = request_detail_derived_columns(["exit"])
        assert derived[-2:] == ["exit", "exits"]
        assert request_detail_headers(["exit", "exits"]) == ["Exit", "Exits tried"]
        assert "exit" not in request_detail_derived_columns(DEFAULT_REQUEST_FIELDS)
        # Folder stays where it was.
        assert "project_dir" in request_detail_columns(["origin"])
        assert request_detail_headers(["project_dir"]) == ["Folder"]

    def test_an_export_that_asks_carries_the_same_exits_as_the_table(
        self, store
    ) -> None:
        rows = {
            row["id"]: row
            for row in store.iter_export_rows(
                columns=request_detail_columns(["exit", "origin"]),
                need_bodies=False,
                need_exits=True,
            )
        }

        assert rows["rotated"]["exit"] == "exit-c.example:1080"
        assert rows["rotated"]["exits"] == (
            "exit-a.example:1080; exit-b.example:1080; exit-c.example:1080"
        )
        assert rows["system"]["exit"] == SYSTEM
        assert rows["none"]["exit"] == ""
        assert rows["none"]["exits"] == ""
        assert rows["fallback"]["exit"] == ""
        assert rows["fallback"]["exits"] == "exit-a.example:1080"
        assert rows["masked"]["exit"] == "203.0.113.9:1080"
        assert "project_dir" in rows["chain"]

    def test_an_export_that_does_not_ask_is_unchanged(self, store) -> None:
        columns = request_detail_columns(DEFAULT_REQUEST_FIELDS)
        plain = list(store.iter_export_rows(columns=columns, need_bodies=False))
        assert all("exit" not in row and "exits" not in row for row in plain)

    def test_the_export_follows_the_exit_filter(self, store) -> None:
        rows = list(
            store.iter_export_rows(
                columns=request_detail_columns(["exit"]),
                need_bodies=False,
                need_exits=True,
                exit="exit-b",
            )
        )
        assert [row["id"] for row in rows] == ["rotated"]
        attempts = list(store.iter_export_attempt_rows(exit="exit-b"))
        assert {row["request_id"] for row in attempts} == {"rotated"}


class TestTheMask:
    @pytest.mark.parametrize(
        ("stored", "shown"),
        [
            ("Tokyo exit", "Tokyo exit"),
            ("203.0.113.7:1080", "203.0.113.7:1080"),
            ("direct", "direct"),
            (SYSTEM, SYSTEM),
            ("alice:s3cretpw@203.0.113.9:1080", "203.0.113.9:1080"),
            ("socks5h://bob:hunter2x@198.51.100.2:8080", "198.51.100.2:8080"),
            ("http://203.0.113.9:3128/path", "203.0.113.9:3128"),
            (
                "direct via system proxy carol:pw9x@10.0.0.1:8080",
                "direct via system proxy 10.0.0.1:8080",
            ),
            ("bob:hunter2x@", MASKED_EXIT_LABEL),
            (None, None),
        ],
    )
    def test_it_masks_as_the_proxying_page_does(
        self, stored: str | None, shown: str | None
    ) -> None:
        assert masked_exit_label(stored) == shown
