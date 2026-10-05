"""Web search usage analytics: SQLite log store with configurable rollups.

Recording is non-blocking: :meth:`WebSearchLogStore.record` enqueues onto a
bounded queue drained by a single background writer thread (WAL,
``synchronous=NORMAL``, batched inserts). Reads (``stats``/``requests``) use
short-lived connections so they never block the writer. Retention prunes both
attempt and logical-route tables to ``max_rows`` newest records; incremental
auto-vacuum returns those pruned pages to the filesystem.

Import direction: this module may import ``config``/``core`` and the sibling
``registry`` (for the outcome contracts); nothing in
``core/websearch`` or ``registry`` imports this module statically — the
registry reaches the recorders through dynamic import seams.
"""

import contextlib
import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from compression import zstd
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any

from loguru import logger

from my_claude_code.config.paths import LOGS_DIRNAME, config_dir_path
from my_claude_code.config.settings import Settings

from .registry import SearchOutcome, SearchRouteOutcome

WEBSEARCH_DB_FILENAME = "websearch.db"
QUERY_LOG_CHARS = 256
ERROR_MESSAGE_LOG_CHARS = 500
STATS_PERIODS: tuple[str, ...] = ("hourly", "daily", "weekly", "monthly")

_DEFAULT_MAX_ROWS = 50000
_QUEUE_CAP = 2048
_BATCH_SIZE = 64
_POLL_SECONDS = 0.2
_PRUNE_EVERY_INSERTS = 100
_MAX_LIMIT = 500
_TOP_ERRORS_LIMIT = 10
_BUSY_TIMEOUT_MS = 5000
_DEFAULT_MAX_CONTENT_CHARS = 2_000_000
_CONFIG_JSON_MAX_CHARS = 20000
_REDACTED = "[REDACTED]"

# Decoded export column name -> actual DB column holding the JSON. The export
# registry projects "input"/"output"/"provider_config" as the row keys; the
# search_log table stores them in the *_json columns.
_EXPORT_JSON_COLUMNS: dict[str, str] = {
    "input": "input_json",
    "output": "output_json",
    "provider_config": "provider_config_json",
}

# Captured search results are stored compressed (7.77.0). On a real
# websearch.db ``output_json`` held 480.0 of its 485.9 MB: 2,467 payloads of up
# to 2.1 MB of page text each. A compressed payload is a BLOB in the same
# column a plain one is TEXT in, so the storage class says which encoding a
# row holds: TEXT is the JSON as written, BLOB is this envelope -- byte 0 the
# envelope version, then one zstd frame of the JSON's UTF-8 bytes. A version
# this code does not know reads as no payload, never as bytes passed through.
# No dictionary: the payloads are large enough that one trained on 256 of them
# made 400 others only 1.4% smaller (level 9), not worth a table and a trainer.
_PAYLOAD_ENVELOPE_V1 = 1
# The smallest result the user asked for, at a cost only the background writer
# pays: on all 2,467 real payloads level 9 wrote 107.9 MB at 21.7 MB/s, 17
# wrote 101.8 MB at 2.2 MB/s and 19 wrote 101.2 MB at 1.0 MB/s -- about 0.2 s
# per result on average. Decoding is the same 1.6-1.8 s for all of them.
_PAYLOAD_COMPRESSION_LEVEL = 19
# The SQL function the content search matches a compressed payload with; see
# ``_payload_matcher``.
_PAYLOAD_SQL_FUNCTION = "mcc_websearch_match"
# ``input_json`` (0.37 MB) and ``provider_config_json`` (0.99 MB) stay plain:
# compressed they measured 0.36 MB smaller together, and an older version would
# show a converted one as empty.

# History conversion (7.77.0); see ``_run_history_conversion``. One JSON
# document in ``search_log_meta``, written in the same transaction as the rows
# each step changes, so a step either happened with its bookkeeping or not at
# all. Versioned name per the marker rule.
_HISTORY_CONVERSION_KEY = "payload_conversion_v1"
# Writer time one step may take before it commits and looks at the queue
# again: the longest a search's row can wait behind the conversion.
_HISTORY_STEP_SECONDS = 0.25
# Rows read per query inside a step. A payload can be 2 MB, so a few at a
# time; the step itself ends on the time budget above, never on a row count.
_HISTORY_FETCH_ROWS = 4
# The longest the conversion keeps the writer's idle branch before it polls
# the queue once more.
_HISTORY_IDLE_SLICE_SECONDS = 5.0
# At most one progress line per this many seconds.
_HISTORY_PROGRESS_LOG_SECONDS = 300.0
# ``PRAGMA incremental_vacuum(N)`` step bounds, the request log's: a step adapts
# N to fit ``_HISTORY_STEP_SECONDS``, at most doubling from one to the next.
_SPACE_STEP_PAGES_MIN = 16
_SPACE_STEP_PAGES_START = 256
_SPACE_STEP_PAGES_MAX = 1_024
# Retried after this long when another process held the write lock.
_HISTORY_RETRY_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class _PayloadCapture:
    stored_json: str | None
    original_chars: int
    sha256: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS search_log (
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
    cost_usd REAL,
    route_id TEXT,
    attempt_number INTEGER NOT NULL DEFAULT 1,
    input_json TEXT,
    output_json TEXT,
    provider_config_json TEXT,
    input_chars INTEGER,
    output_chars INTEGER,
    input_sha256 TEXT,
    output_sha256 TEXT,
    content_captured INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS search_route_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    route_id TEXT NOT NULL UNIQUE,
    ts_epoch REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    query TEXT NOT NULL,
    primary_provider TEXT NOT NULL,
    terminal_provider TEXT NOT NULL,
    provider_path TEXT NOT NULL,
    attempt_count INTEGER NOT NULL,
    fallback_used INTEGER NOT NULL,
    duration_ms REAL NOT NULL,
    status TEXT NOT NULL,
    results_count INTEGER NOT NULL,
    cost_usd REAL,
    error_kind TEXT,
    error_message TEXT
);
CREATE TABLE IF NOT EXISTS search_log_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_search_log_ts ON search_log (ts_epoch);
CREATE INDEX IF NOT EXISTS idx_search_log_provider_ts ON search_log (provider, ts_epoch);
CREATE INDEX IF NOT EXISTS idx_search_route_log_ts
    ON search_route_log (ts_epoch);
CREATE INDEX IF NOT EXISTS idx_search_route_log_terminal_ts
    ON search_route_log (terminal_provider, ts_epoch);
"""

_POST_MIGRATION_SCHEMA = """
CREATE INDEX IF NOT EXISTS idx_search_log_route_id ON search_log (route_id);
"""

_INSERT_SQL = """
INSERT INTO search_log (
    ts_epoch, ts_iso, provider, key_index, key_label, query,
    results_count, duration_ms, status, error_kind, error_message, cost_usd,
    route_id, attempt_number, input_json, output_json, provider_config_json,
    input_chars, output_chars, input_sha256, output_sha256, content_captured
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_ROUTE_SQL = """
INSERT INTO search_route_log (
    route_id, ts_epoch, ts_iso, query, primary_provider, terminal_provider,
    provider_path, attempt_count, fallback_used, duration_ms, status,
    results_count, cost_usd, error_kind, error_message
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_PRUNE_SQL = """
DELETE FROM search_log
WHERE id NOT IN (SELECT id FROM search_log ORDER BY id DESC LIMIT ?)
"""

_PRUNE_ROUTES_SQL = """
DELETE FROM search_route_log
WHERE id NOT IN (SELECT id FROM search_route_log ORDER BY id DESC LIMIT ?)
"""

# One logical route writes one row here but >=1 rows into ``search_log`` (an
# extra row per fallback hop), so the attempt table reaches ``max_rows`` first.
# Pruning both to the same row count would leave routes summarizing a window
# whose attempt detail is already gone -- and the admin dashboard renders the
# route and attempt tables side by side as if they covered the same period.
# Trim routes back to the window the surviving attempts still cover.
_PRUNE_ORPHAN_ROUTES_SQL = """
DELETE FROM search_route_log
WHERE (SELECT COUNT(*) FROM search_log) > 0
  AND ts_epoch < (SELECT MIN(ts_epoch) FROM search_log)
"""

_REQUEST_COLUMNS = (
    "id, ts_epoch, ts_iso, provider, key_index, key_label, query, results_count,"
    " duration_ms, status, error_kind, error_message, cost_usd, route_id,"
    " attempt_number, input_chars, output_chars, input_sha256, output_sha256,"
    " content_captured"
)

_REQUEST_DETAIL_COLUMNS = (
    f"{_REQUEST_COLUMNS}, input_json, output_json, provider_config_json"
)

_ROUTE_COLUMNS = (
    "id, route_id, ts_epoch, ts_iso, query, primary_provider, terminal_provider,"
    " provider_path, attempt_count, fallback_used, duration_ms, status,"
    " results_count, cost_usd, error_kind, error_message"
)


def default_websearch_db_path() -> Path:
    """Default analytics database path: ``~/.mcc/logs/websearch.db``."""

    return config_dir_path() / LOGS_DIRNAME / WEBSEARCH_DB_FILENAME


class _Control:
    """Writer-thread control message: drain barrier (``clear=False``) or clear."""

    __slots__ = ("clear", "deleted", "done")

    def __init__(self, *, clear: bool) -> None:
        self.clear = clear
        self.done = threading.Event()
        self.deleted = 0


class WebSearchLogStore:
    """Durable per-search usage log with rollup stats.

    ``record`` never blocks the caller (records are dropped with a warning
    when the queue is full). ``close`` drains pending records before the
    writer stops. Instances are safe to share across threads.
    """

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        max_rows: int = _DEFAULT_MAX_ROWS,
        queue_cap: int = _QUEUE_CAP,
        prune_every: int = _PRUNE_EVERY_INSERTS,
        capture_content: bool = True,
        max_content_chars: int = _DEFAULT_MAX_CONTENT_CHARS,
    ) -> None:
        self._db_path = db_path if db_path is not None else default_websearch_db_path()
        self._max_rows = max(0, max_rows)
        self._prune_every = max(1, prune_every)
        self._capture_content = capture_content
        self._max_content_chars = max(512, max_content_chars)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: Queue[SearchOutcome | SearchRouteOutcome | _Control] = Queue(
            maxsize=max(1, queue_cap)
        )
        self._state_lock = threading.Lock()
        self._stopping = threading.Event()
        self._closed = False
        self._dropped = 0
        self._inserts_since_prune = 0
        # History conversion state; writer thread only. See
        # ``_run_history_conversion``.
        self._history_done = False
        self._history_retry_at = 0.0
        self._history_announced = False
        self._history_logged_at = 0.0
        self._history_space_mode: int | None = None
        self._space_step_pages = _SPACE_STEP_PAGES_START
        self._writer = threading.Thread(
            target=self._writer_main,
            name="websearch-log-writer",
            daemon=True,
        )
        self._writer.start()

    def retune(
        self, *, max_rows: int, capture_content: bool, max_content_chars: int
    ) -> None:
        """Adopt a saved configuration without reopening the database.

        Three plain values the writer reads per row and per prune, so a single
        assignment each is the whole change; the writer, its connection and
        every queued record carry on.
        """

        self._max_rows = max(0, int(max_rows))
        self._capture_content = bool(capture_content)
        self._max_content_chars = max(512, int(max_content_chars))

    def __enter__(self) -> WebSearchLogStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def dropped(self) -> int:
        """Records discarded because the queue was full."""

        return self._dropped

    def record(self, outcome: SearchOutcome) -> bool:
        """Enqueue one provider-attempt outcome."""

        return self._enqueue(outcome)

    def record_route(self, outcome: SearchRouteOutcome) -> bool:
        """Enqueue one logical route outcome."""

        return self._enqueue(outcome)

    def _enqueue(self, outcome: SearchOutcome | SearchRouteOutcome) -> bool:
        with self._state_lock:
            if self._closed:
                return False
            try:
                self._queue.put_nowait(outcome)
            except Full:
                self._dropped += 1
                if self._dropped == 1 or self._dropped % 100 == 0:
                    logger.warning(
                        "websearch analytics queue full; dropped {} record(s) total",
                        self._dropped,
                    )
                return False
            return True

    def flush(self, timeout: float = 5.0) -> None:
        """Block until every queued record has been written."""

        with self._state_lock:
            if self._closed:
                return
        self._send_control(_Control(clear=False), timeout)

    def clear(self, timeout: float = 10.0) -> int:
        """Delete every recorded request; returns the number of rows removed."""

        control = _Control(clear=True)
        self._send_control(control, timeout)
        return control.deleted

    def close(self, timeout: float = 5.0) -> bool:
        """Stop the writer after draining queued records; True when drained."""

        with self._state_lock:
            if self._closed:
                return True
            self._closed = True
            self._stopping.set()
        self._writer.join(timeout)
        return not self._writer.is_alive()

    def stats(
        self,
        period: str = "weekly",
        *,
        provider: str | None = None,
        status: str | None = None,
        q: str | None = None,
        since_epoch: float | None = None,
        until_epoch: float | None = None,
    ) -> dict[str, Any]:
        """Aggregate a consistently filtered row set using ``period`` buckets."""

        if period not in STATS_PERIODS:
            raise ValueError(f"unknown stats period: {period!r}")
        attempt_where, attempt_params = _attempt_filter_where(
            provider=provider,
            status=status,
            q=q,
            since_epoch=since_epoch,
            until_epoch=until_epoch,
        )
        route_where, route_params = _route_filter_where(
            provider=provider,
            status=status,
            q=q,
            since_epoch=since_epoch,
            until_epoch=until_epoch,
        )
        connection = self._connect_reader()
        try:
            attempts = _attempt_stats(connection, attempt_where, attempt_params, period)
            routes = _route_stats(connection, route_where, route_params, period)
        finally:
            connection.close()
        route_window = routes["window"]
        attempt_window = attempts["window"]
        return {
            "period": period,
            "filters": {
                "provider": provider,
                "status": status,
                "q": q,
                "since_epoch": since_epoch,
                "until_epoch": until_epoch,
            },
            "window": {
                "since_epoch": (
                    since_epoch
                    if since_epoch is not None
                    else (
                        route_window["since_epoch"]
                        if route_window["since_epoch"] is not None
                        else attempt_window["since_epoch"]
                    )
                ),
                "until_epoch": (
                    until_epoch
                    if until_epoch is not None
                    else (
                        route_window["until_epoch"]
                        if route_window["until_epoch"] is not None
                        else attempt_window["until_epoch"]
                    )
                ),
            },
            "dropped_records": self.dropped,
            "capture_content": self._capture_content,
            "max_content_chars": self._max_content_chars,
            # Compatibility aliases: these remain provider-attempt metrics.
            "totals": attempts["totals"],
            "by_provider": attempts["by_provider"],
            "by_key": attempts["by_key"],
            "series": attempts["series"],
            "top_errors": attempts["top_errors"],
            # Explicit layers for new clients.
            "attempts": attempts,
            "routes": routes,
            "last_route": routes["last_route"],
        }

    def requests(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        provider: str | None = None,
        status: str | None = None,
        q: str | None = None,
        since_epoch: float | None = None,
        until_epoch: float | None = None,
        include_content: bool = False,
    ) -> dict[str, Any]:
        """Page recorded requests (newest first) with optional filters."""

        limit = min(max(1, limit), _MAX_LIMIT)
        offset = max(0, offset)
        where, params = _attempt_filter_where(
            provider=provider,
            status=status,
            q=q,
            since_epoch=since_epoch,
            until_epoch=until_epoch,
        )
        connection = self._connect_reader()
        try:
            total = connection.execute(
                f"SELECT COUNT(*) FROM search_log {where}", params
            ).fetchone()[0]
            columns = _REQUEST_DETAIL_COLUMNS if include_content else _REQUEST_COLUMNS
            items = [
                _attempt_dict(row)
                for row in connection.execute(
                    f"SELECT {columns} FROM search_log {where}"
                    " ORDER BY ts_epoch DESC, id DESC LIMIT ? OFFSET ?",
                    (*params, limit, offset),
                ).fetchall()
            ]
        finally:
            connection.close()
        return {"total": int(total), "limit": limit, "offset": offset, "items": items}

    def iter_export_rows(
        self,
        *,
        columns: list[str],
        include_content: bool,
        provider: str | None = None,
        status: str | None = None,
        q: str | None = None,
        since_epoch: float | None = None,
        until_epoch: float | None = None,
        page_size: int = 1_000,
    ) -> Iterator[dict[str, Any]]:
        """Yield every matching attempt for an export, bypassing the 500-row cap.

        Keyset-paginates over ``(ts_epoch, id)`` so a full export stays O(n),
        and keeps one reader connection open (closed in ``finally`` so a
        generator abandoned mid-stream leaks nothing). Decodes captured
        ``input_json``/``output_json``/``provider_config_json`` when
        ``include_content`` is true.
        """
        where, params = _attempt_filter_where(
            provider=provider,
            status=status,
            q=q,
            since_epoch=since_epoch,
            until_epoch=until_epoch,
        )
        # Keyset pagination needs ts_epoch + id; _attempt_dict needs
        # content_captured unconditionally. Force-include all three.
        required = {"ts_epoch", "id", "content_captured"}
        select_columns = list(columns)
        for column in required:
            if column not in select_columns:
                select_columns.append(column)
        # The registry's decoded names ("input"/"output"/"provider_config")
        # are not DB columns; the stored JSON lives in the *_json columns and
        # _attempt_dict renames them back to the decoded names on yield.
        select = ", ".join(
            f"{_EXPORT_JSON_COLUMNS.get(column, column)} AS {column}"
            if column in _EXPORT_JSON_COLUMNS
            else column
            for column in select_columns
        )
        connection = self._connect_reader()
        try:
            cursor: tuple[float, int] | None = None
            while True:
                page_where = where
                page_params: list[Any] = list(params)
                if cursor is not None:
                    last_ts, last_id = cursor
                    page_where = f"{where}{' AND' if where else 'WHERE'}"
                    page_where += " (ts_epoch, id) < (?, ?)"
                    page_params.extend([last_ts, last_id])
                rows = connection.execute(
                    f"SELECT {select} FROM search_log {page_where}"
                    " ORDER BY ts_epoch DESC, id DESC LIMIT ?",
                    (*page_params, page_size),
                ).fetchall()
                if not rows:
                    return
                for row in rows:
                    yield _attempt_dict(row)
                cursor = (float(rows[-1]["ts_epoch"]), int(rows[-1]["id"]))
        finally:
            connection.close()

    def iter_export_aggregates(
        self,
        *,
        select: str,
        names: list[str],
        group_by: list[str],
        provider: str | None = None,
        status: str | None = None,
        q: str | None = None,
        since_epoch: float | None = None,
        until_epoch: float | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield the aggregated (grouped) websearch records for an export."""
        where, params = _attempt_filter_where(
            provider=provider,
            status=status,
            q=q,
            since_epoch=since_epoch,
            until_epoch=until_epoch,
        )
        group_sql = ", ".join(group_by)
        order_sql = ", ".join(group_by)
        sql = (
            f"SELECT {select} FROM search_log {where}"
            f" GROUP BY {group_sql} ORDER BY {order_sql}"
        )
        connection = self._connect_reader()
        try:
            for row in connection.execute(sql, params).fetchall():
                yield {name: row[name] for name in names}
        finally:
            connection.close()

    def request(self, request_id: int) -> dict[str, Any] | None:
        """Return one provider-attempt record with captured I/O and configuration."""

        connection = self._connect_reader()
        try:
            row = connection.execute(
                f"SELECT {_REQUEST_DETAIL_COLUMNS} FROM search_log WHERE id = ?",
                (request_id,),
            ).fetchone()
        finally:
            connection.close()
        return _attempt_dict(row) if row is not None else None

    def _send_control(self, control: _Control, timeout: float) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("websearch log store is closed")
            try:
                self._queue.put(control, timeout=timeout)
            except Full:
                raise TimeoutError("websearch log writer is busy") from None
        if not control.done.wait(timeout):
            raise TimeoutError("websearch log writer did not acknowledge in time")

    def _writer_main(self) -> None:
        connection = sqlite3.connect(self._db_path)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
            _ensure_auto_vacuum(connection)
            _initialize_schema(connection)
            while True:
                batch = self._collect_batch()
                if batch:
                    self._write_batch(connection, batch)
                elif self._stopping.is_set():
                    return
                else:
                    # Nothing to write: one bounded step at a time, back to
                    # the queue the moment a record or a close arrives.
                    self._run_history_conversion(connection)
        except Exception:
            logger.exception("websearch analytics writer crashed")
        finally:
            connection.close()

    def _collect_batch(
        self,
    ) -> list[SearchOutcome | SearchRouteOutcome | _Control]:
        try:
            first = self._queue.get(timeout=_POLL_SECONDS)
        except Empty:
            return []
        items: list[SearchOutcome | SearchRouteOutcome | _Control] = [first]
        while len(items) < _BATCH_SIZE:
            try:
                items.append(self._queue.get_nowait())
            except Empty:
                break
        return items

    def _write_batch(
        self,
        connection: sqlite3.Connection,
        items: list[SearchOutcome | SearchRouteOutcome | _Control],
    ) -> None:
        attempt_rows: list[tuple[object, ...]] = []
        route_rows: list[tuple[object, ...]] = []
        for item in items:
            if isinstance(item, _Control):
                self._insert_rows(connection, attempt_rows, route_rows)
                attempt_rows = []
                route_rows = []
                if item.clear:
                    item.deleted = self._clear_rows(connection)
                item.done.set()
            elif isinstance(item, SearchRouteOutcome):
                route_rows.append(
                    _route_row_tuple(item, capture_content=self._capture_content)
                )
            else:
                attempt_rows.append(
                    _row_tuple(
                        item,
                        capture_content=self._capture_content,
                        max_content_chars=self._max_content_chars,
                    )
                )
        self._insert_rows(connection, attempt_rows, route_rows)

    def _insert_rows(
        self,
        connection: sqlite3.Connection,
        attempt_rows: list[tuple[object, ...]],
        route_rows: list[tuple[object, ...]],
    ) -> None:
        if not attempt_rows and not route_rows:
            return
        try:
            if attempt_rows:
                connection.executemany(_INSERT_SQL, attempt_rows)
            if route_rows:
                connection.executemany(_INSERT_ROUTE_SQL, route_rows)
            connection.commit()
        except Exception:
            connection.rollback()
            logger.exception(
                "websearch analytics insert failed; dropping {} attempt(s) and"
                " {} route(s)",
                len(attempt_rows),
                len(route_rows),
            )
            return
        self._inserts_since_prune += len(attempt_rows) + len(route_rows)
        if self._inserts_since_prune >= self._prune_every:
            self._inserts_since_prune = 0
            self._prune(connection)

    def _clear_rows(self, connection: sqlite3.Connection) -> int:
        cursor = connection.execute("DELETE FROM search_log")
        connection.execute("DELETE FROM search_route_log")
        connection.commit()
        return cursor.rowcount

    def _prune(self, connection: sqlite3.Connection) -> None:
        try:
            connection.execute(_PRUNE_SQL, (self._max_rows,))
            connection.execute(_PRUNE_ROUTES_SQL, (self._max_rows,))
            connection.execute(_PRUNE_ORPHAN_ROUTES_SQL)
            connection.commit()
            # Requires incremental auto-vacuum (see ``_ensure_auto_vacuum``).
            # The pragma yields one row per reclaimed page; an unconsumed
            # cursor leaves the statement suspended after the first step
            # and every prune would reclaim exactly one page while the
            # rest of the freed pages stay on the freelist.
            for _ in connection.execute("PRAGMA incremental_vacuum"):
                pass
            # The pragma's page moves participate in a transaction under
            # Python's implicit-transaction mode; commit them so readers see
            # the reclaimed pages immediately.
            connection.commit()
        except Exception:
            connection.rollback()
            logger.exception("websearch analytics retention prune failed")

    # ------------------------------------------------------ history conversion

    def _run_history_conversion(self, connection: sqlite3.Connection) -> None:
        """Writer thread, idle only: compress the payloads written before 7.77.0.

        Each step is one transaction that carries its bookkeeping
        (``_HISTORY_CONVERSION_KEY``), so a kill between any two statements
        leaves the database consistent and the walk resumes where the last
        committed step ended. A payload is replaced only once its compressed
        form has been read back by the reader's own decoder as exactly the
        bytes it replaces; anything else stays as it was and is counted. The
        pages this frees go back to the filesystem a few at a time, only when
        the database's ``auto_vacuum`` is incremental. A step stops on a time
        budget, and the slice the moment a record, a flush or a close arrives.
        """
        if self._history_done or time.monotonic() < self._history_retry_at:
            return
        try:
            deadline = time.monotonic() + _HISTORY_IDLE_SLICE_SECONDS
            while (
                self._queue.empty()
                and not self._stopping.is_set()
                and time.monotonic() < deadline
            ):
                if not self._history_step(connection):
                    break
        except sqlite3.Error as exc:
            with contextlib.suppress(sqlite3.Error):
                connection.rollback()
            # Another process may hold the write lock for longer than the busy
            # timeout. Nothing is lost by trying again later.
            logger.warning("Websearch log history conversion paused: {}", exc)
            self._history_retry_at = time.monotonic() + _HISTORY_RETRY_SECONDS

    @staticmethod
    def _new_history_state(connection: sqlite3.Connection) -> dict[str, Any]:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
        return {
            "started_at": time.time(),
            "page_size": page_size,
            "bytes_at_start": page_count * page_size,
            # Pages already free before the conversion freed any: never
            # handed back by it (they are not its own to give).
            "freelist_at_start": _freelist_count(connection),
            "payloads": {
                "through": 0,
                "end": None,
                "converted": 0,
                "kept": 0,
                "failed": 0,
                "bytes_before": 0,
                "bytes_after": 0,
                "done_at": None,
            },
            "freed_pages": 0,
            "returned_pages": 0,
            "done_at": None,
            "bytes_at_end": None,
        }

    def _load_history_state(self, connection: sqlite3.Connection) -> dict[str, Any]:
        row = connection.execute(
            "SELECT value FROM search_log_meta WHERE key = ?",
            (_HISTORY_CONVERSION_KEY,),
        ).fetchone()
        if row is not None:
            with contextlib.suppress(ValueError, TypeError):
                state = json.loads(row[0])
                if isinstance(state, dict) and isinstance(state.get("payloads"), dict):
                    return state
        return self._new_history_state(connection)

    @staticmethod
    def _save_history_state(
        connection: sqlite3.Connection, state: dict[str, Any]
    ) -> None:
        connection.execute(
            "INSERT INTO search_log_meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_HISTORY_CONVERSION_KEY, json.dumps(state, sort_keys=True)),
        )

    def _history_step(self, connection: sqlite3.Connection) -> bool:
        """One bounded step; False when there is nothing to do now."""
        state = self._load_history_state(connection)
        if state.get("done_at") is not None:
            self._history_done = True
            return False
        if not self._history_announced:
            self._history_announced = True
            self._history_logged_at = time.monotonic()
            # Silent when there is nothing to convert: a new database, or one
            # every row of which this version wrote.
            if connection.execute(
                "SELECT 1 FROM search_log WHERE id > ?"
                " AND typeof(output_json) = 'text' LIMIT 1",
                (int(state["payloads"]["through"] or 0),),
            ).fetchone():
                logger.info(
                    "Websearch log history conversion {}: older search results"
                    " are compressed in the background, each checked before it"
                    " is replaced ({:.2f} GB on disk)",
                    "resumed" if int(state["payloads"]["through"] or 0) else "started",
                    int(state["bytes_at_start"]) / 1e9,
                )
        progressed = False
        if state["payloads"]["done_at"] is None:
            progressed = self._convert_payload_step(connection)
        if self._queue.empty() and self._return_space_step(connection):
            progressed = True
        state = self._load_history_state(connection)
        if state["payloads"]["done_at"] is not None and not self._space_owed(
            connection, state
        ):
            self._finish_history(connection)
            return False
        self._log_history_progress(state)
        return progressed

    def _convert_payload_step(self, connection: sqlite3.Connection) -> bool:
        """Turn the next plain ``output_json`` values into the envelope; one transaction."""
        budget_end = time.perf_counter() + _HISTORY_STEP_SECONDS
        connection.execute("BEGIN IMMEDIATE")
        try:
            state = self._load_history_state(connection)
            phase = state["payloads"]
            if phase["end"] is None:
                phase["end"] = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(id), 0) FROM search_log"
                    ).fetchone()[0]
                )
            freelist_before = _freelist_count(connection)
            cursor = int(phase["through"])
            updates: list[tuple[bytes, int]] = []
            finished = False
            while True:
                rows = connection.execute(
                    "SELECT id, CASE WHEN typeof(output_json) = 'text'"
                    " THEN CAST(output_json AS BLOB) END FROM search_log"
                    " WHERE id > ? ORDER BY id LIMIT ?",
                    (cursor, _HISTORY_FETCH_ROWS),
                ).fetchall()
                if not rows:
                    finished = True
                    break
                for row in rows:
                    cursor = int(row[0])
                    if row[1] is not None:
                        original = bytes(row[1])
                        envelope = _payload_envelope(original)
                        if envelope is None:
                            phase["kept"] += 1
                        elif not _reads_back(envelope, original):
                            phase["failed"] += 1
                        else:
                            updates.append((envelope, cursor))
                            phase["converted"] += 1
                            phase["bytes_before"] += len(original)
                            phase["bytes_after"] += len(envelope)
                    if time.perf_counter() >= budget_end:
                        break
                # At least one row per step, however slow, so the walk always
                # moves; then the budget decides.
                if time.perf_counter() >= budget_end:
                    break
            if updates:
                connection.executemany(
                    "UPDATE search_log SET output_json = ? WHERE id = ?", updates
                )
            phase["through"] = cursor
            if finished:
                phase["done_at"] = time.time()
            state["freed_pages"] = int(state["freed_pages"]) + max(
                0, _freelist_count(connection) - freelist_before
            )
            self._save_history_state(connection, state)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        if finished and phase["failed"]:
            logger.warning(
                "Websearch log history conversion left {} search results as they"
                " were: their compressed form did not read back as the same bytes",
                phase["failed"],
            )
        return True

    def _space_owed(self, connection: sqlite3.Connection, state: dict[str, Any]) -> int:
        """Pages the conversion freed that are not handed back yet.

        Never more than the freelist holds above what it held before the
        conversion freed anything, and zero when the database cannot hand
        pages back without a full rewrite (``auto_vacuum`` not incremental).
        """
        if self._history_space_mode is None:
            self._history_space_mode = int(
                connection.execute("PRAGMA auto_vacuum").fetchone()[0]
            )
            if self._history_space_mode != 2:
                logger.info(
                    "Websearch log auto_vacuum is not incremental: the space the"
                    " conversion frees stays inside the file for new rows"
                )
        if self._history_space_mode != 2:
            return 0
        owed = int(state["freed_pages"]) - int(state["returned_pages"])
        above = _freelist_count(connection) - int(state["freelist_at_start"])
        return max(0, min(owed, above))

    def _return_space_step(self, connection: sqlite3.Connection) -> bool:
        """Hand a few freed pages back to the filesystem; one transaction.

        A step is sized to fit ``_HISTORY_STEP_SECONDS``, adapting to how long
        the last one took. In WAL mode the file itself shrinks at the next
        automatic checkpoint; nothing here forces one.
        """
        state = self._load_history_state(connection)
        converting = state["payloads"]["done_at"] is None
        owed = self._space_owed(connection, state)
        if owed <= 0 or (converting and owed < self._space_step_pages):
            return False
        connection.execute("BEGIN IMMEDIATE")
        try:
            # Again under the write lock: what was owed a moment ago may have
            # been handed back by a retention prune since.
            state = self._load_history_state(connection)
            owed = self._space_owed(connection, state)
            if owed <= 0:
                connection.commit()
                return False
            pages = min(self._space_step_pages, owed)
            before = _freelist_count(connection)
            started = time.perf_counter()
            connection.execute(f"PRAGMA incremental_vacuum({int(pages)})").fetchall()
            elapsed = time.perf_counter() - started
            returned = max(0, before - _freelist_count(connection))
            state["returned_pages"] = int(state["returned_pages"]) + returned
            self._save_history_state(connection, state)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        if elapsed > 0:
            scaled = min(pages * 2, int(pages * _HISTORY_STEP_SECONDS / elapsed))
            self._space_step_pages = max(
                _SPACE_STEP_PAGES_MIN, min(_SPACE_STEP_PAGES_MAX, scaled)
            )
        return returned > 0

    def _finish_history(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            state = self._load_history_state(connection)
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            state["bytes_at_end"] = page_count * int(state["page_size"])
            state["done_at"] = time.time()
            self._save_history_state(connection, state)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        self._history_done = True
        phase = state["payloads"]
        if not phase["converted"] and not phase["kept"] and not phase["failed"]:
            return
        logger.info(
            "Websearch log history conversion done in {:.0f} min: {} search results"
            " compressed ({:.2f} GB -> {:.2f} GB), {} left as they were, {} failed"
            " the check; database {:.2f} GB -> {:.2f} GB, {:.2f} GB handed back",
            (state["done_at"] - float(state["started_at"])) / 60,
            phase["converted"],
            int(phase["bytes_before"]) / 1e9,
            int(phase["bytes_after"]) / 1e9,
            phase["kept"],
            phase["failed"],
            int(state["bytes_at_start"]) / 1e9,
            int(state["bytes_at_end"]) / 1e9,
            int(state["returned_pages"]) * int(state["page_size"]) / 1e9,
        )

    def _log_history_progress(self, state: dict[str, Any]) -> None:
        now = time.monotonic()
        if now - self._history_logged_at < _HISTORY_PROGRESS_LOG_SECONDS:
            return
        self._history_logged_at = now
        phase = state["payloads"]
        end = int(phase["end"] or 0)
        percent = min(99, 100 * int(phase["through"]) // end) if end else 0
        logger.info(
            "Websearch log history conversion: {}%, {:.2f} GB handed back so far",
            100 if phase["done_at"] is not None else percent,
            int(state["returned_pages"]) * int(state["page_size"]) / 1e9,
        )

    def _connect_reader(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
        # Every reader may run the content search, which matches a compressed
        # payload through this function (``_output_matches``); one per
        # connection, so what it remembers lives as long as one call.
        connection.create_function(
            _PAYLOAD_SQL_FUNCTION, 3, _payload_matcher(), deterministic=True
        )
        _initialize_schema(connection)
        return connection


_shared_lock = threading.Lock()
_shared_store: WebSearchLogStore | None = None
_log_enabled_cache: bool | None = None


def get_shared_store() -> WebSearchLogStore:
    """Lazily created process-wide store (``max_rows`` from settings)."""

    global _shared_store
    with _shared_lock:
        store = _shared_store
        if store is None or store.closed:
            settings = Settings()
            store = WebSearchLogStore(
                max_rows=settings.websearch_log_max_rows,
                capture_content=settings.websearch_log_capture_content,
                max_content_chars=settings.websearch_log_content_max_chars,
            )
            _shared_store = store
        return store


def retune_shared_store(settings: Settings) -> None:
    """Move the process-wide store and switch onto a saved configuration.

    Called by the runtime after an admin apply. Both were read once, at first
    use, and kept: the store's three numbers and ``WEBSEARCH_LOG_ENABLED``. A
    store that is not open yet is left alone -- it is built from the current
    settings on first use -- and nothing is closed.
    """

    global _log_enabled_cache
    with _shared_lock:
        store = _shared_store
        if _log_enabled_cache is not None:
            _log_enabled_cache = bool(settings.websearch_log_enabled)
    if store is None or store.closed:
        return
    store.retune(
        max_rows=settings.websearch_log_max_rows,
        capture_content=settings.websearch_log_capture_content,
        max_content_chars=settings.websearch_log_content_max_chars,
    )


def record_search(outcome: SearchOutcome) -> None:
    """Registry recorder seam: persist one outcome unless logging is disabled.

    Never raises into the search path; failures are logged and dropped.
    """

    try:
        if not _log_enabled():
            return
        get_shared_store().record(outcome)
    except Exception:
        logger.exception("failed to record web search usage")


def record_search_route(outcome: SearchRouteOutcome) -> None:
    """Persist one logical route outcome unless analytics is disabled."""

    try:
        if not _log_enabled():
            return
        get_shared_store().record_route(outcome)
    except Exception:
        logger.exception("failed to record web search route")


def reset_analytics_state() -> None:
    """Close the shared store and drop cached flags (tests/settings reloads)."""

    global _log_enabled_cache, _shared_store
    with _shared_lock:
        store = _shared_store
        _shared_store = None
        _log_enabled_cache = None
    if store is not None:
        store.close()


def _log_enabled() -> bool:
    """``WEBSEARCH_LOG_ENABLED`` (default True), cached at first use."""

    global _log_enabled_cache
    with _shared_lock:
        if _log_enabled_cache is None:
            _log_enabled_cache = Settings().websearch_log_enabled
        return _log_enabled_cache


def _row_tuple(
    outcome: SearchOutcome,
    *,
    capture_content: bool,
    max_content_chars: int,
) -> tuple[object, ...]:
    input_capture = _capture_payload(
        outcome.input_payload,
        capture=capture_content,
        max_chars=max_content_chars,
    )
    output_capture = _capture_payload(
        outcome.output_payload,
        capture=capture_content,
        max_chars=max_content_chars,
    )
    provider_config_json = _bounded_json(
        outcome.provider_config,
        max_chars=_CONFIG_JSON_MAX_CHARS,
    )
    return (
        outcome.ts_epoch,
        outcome.ts_iso,
        outcome.provider,
        outcome.key_index,
        outcome.key_label,
        # The query is search content too. ``WEBSEARCH_LOG_CAPTURE_CONTENT``
        # exists so operators can stop persisting what was searched for, so it
        # has to cover this column as well as the payload JSON -- otherwise the
        # setting silently leaves the most readable part on disk.
        outcome.query[:QUERY_LOG_CHARS] if capture_content else "",
        outcome.results_count,
        outcome.duration_ms,
        outcome.status,
        outcome.error_kind,
        (
            outcome.error_message[:ERROR_MESSAGE_LOG_CHARS]
            if outcome.error_message
            else None
        ),
        outcome.cost_usd,
        outcome.route_id,
        max(1, outcome.attempt_number),
        input_capture.stored_json,
        _stored_output(output_capture.stored_json),
        provider_config_json,
        input_capture.original_chars,
        output_capture.original_chars,
        input_capture.sha256,
        output_capture.sha256,
        int(capture_content),
    )


def _route_row_tuple(
    outcome: SearchRouteOutcome, *, capture_content: bool
) -> tuple[object, ...]:
    return (
        outcome.route_id,
        outcome.ts_epoch,
        outcome.ts_iso,
        # Withheld with the attempt-row query; see ``_row_tuple``.
        outcome.query[:QUERY_LOG_CHARS] if capture_content else "",
        outcome.primary_provider,
        outcome.terminal_provider,
        _encode_provider_path(outcome.provider_path),
        max(0, outcome.attempt_count),
        int(outcome.fallback_used),
        outcome.duration_ms,
        outcome.status,
        outcome.results_count,
        outcome.cost_usd,
        outcome.error_kind,
        (
            outcome.error_message[:ERROR_MESSAGE_LOG_CHARS]
            if outcome.error_message
            else None
        ),
    )


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(zip(row.keys(), row, strict=True))


def _attempt_dict(row: sqlite3.Row) -> dict[str, Any]:
    shaped = _row_dict(row)
    shaped["content_captured"] = bool(shaped["content_captured"])
    # The raw columns are *_json; the export path aliases them to the decoded
    # names ("input"/"output"/"provider_config"). Decode whichever shape a
    # query projected.
    for decoded, raw in (
        ("input", "input_json"),
        ("output", "output_json"),
        ("provider_config", "provider_config_json"),
    ):
        if raw in shaped:
            shaped[decoded] = _decode_json(shaped.pop(raw))
        elif decoded in shaped:
            shaped[decoded] = _decode_json(shaped[decoded])
    return shaped


def _capture_payload(
    payload: Mapping[str, object] | None,
    *,
    capture: bool,
    max_chars: int,
) -> _PayloadCapture:
    if payload is None:
        return _PayloadCapture(None, 0, None)
    serialized = _json_dumps(_sanitize_payload(payload))
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    stored = _bounded_serialized_json(serialized, max_chars) if capture else None
    return _PayloadCapture(stored, len(serialized), digest)


def _bounded_json(
    payload: Mapping[str, object] | None,
    *,
    max_chars: int,
) -> str | None:
    if payload is None:
        return None
    return _bounded_serialized_json(
        _json_dumps(_sanitize_payload(payload)),
        max_chars,
    )


def _bounded_serialized_json(serialized: str, max_chars: int) -> str:
    if len(serialized) <= max_chars:
        return serialized
    if max_chars <= 0:
        return _json_dumps(
            {
                "_truncated": True,
                "original_chars": len(serialized),
                "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
                "preview": "",
            }
        )
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    preview_chars = max(0, max_chars - 180)
    envelope = _json_dumps(
        {
            "_truncated": True,
            "original_chars": len(serialized),
            "sha256": digest,
            "preview": serialized[:preview_chars],
        }
    )
    while len(envelope) > max_chars and preview_chars > 0:
        preview_chars = max(0, preview_chars - (len(envelope) - max_chars))
        envelope = _json_dumps(
            {
                "_truncated": True,
                "original_chars": len(serialized),
                "sha256": digest,
                "preview": serialized[:preview_chars],
            }
        )
    return envelope


def _sanitize_payload(value: object, key: str = "") -> object:
    if _is_secret_key(key):
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            str(nested_key): _sanitize_payload(nested_value, str(nested_key))
            for nested_key, nested_value in value.items()
        }
    if isinstance(value, list | tuple):
        return [_sanitize_payload(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)


def _is_secret_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return normalized in {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "key",
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "client_secret",
    } or normalized.endswith(("_api_key", "_password", "_secret", "_token"))


def _json_dumps(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _stored_output(text: str | None) -> str | bytes | None:
    """What ``output_json`` stores for ``text``: the envelope, or the text itself.

    The envelope only when it is smaller and reads back, through the reader's
    own decoder, as exactly the bytes of ``text``; anything else -- NULL, a
    payload compression does not shrink, text that is not valid UTF-8 -- is
    stored as it always was.
    """
    if text is None:
        return None
    try:
        raw = text.encode("utf-8")
    except UnicodeEncodeError:
        return text
    envelope = _payload_envelope(raw)
    if envelope is None or not _reads_back(envelope, raw):
        return text
    return envelope


def _payload_envelope(raw: bytes) -> bytes | None:
    """The envelope for ``raw`` when it is smaller than ``raw``, else None."""
    try:
        envelope = bytes((_PAYLOAD_ENVELOPE_V1,)) + zstd.compress(
            raw, level=_PAYLOAD_COMPRESSION_LEVEL
        )
    except zstd.ZstdError, ValueError, MemoryError:
        return None
    return envelope if len(envelope) < len(raw) else None


def _reads_back(envelope: bytes, raw: bytes) -> bool:
    """True when ``envelope`` reads back as exactly ``raw``, and as its text.

    Both through the readers' own paths: the bytes the search matches on and
    the text the detail and the exports decode.
    """
    try:
        return _unwrap_payload(envelope) == raw and _payload_text(
            envelope
        ) == raw.decode("utf-8")
    except zstd.ZstdError, ValueError, MemoryError:
        return False


def _unwrap_payload(data: bytes) -> bytes:
    """The UTF-8 bytes an ``output_json`` envelope holds, or raise.

    The one decoder: the detail, the exports, the search and the history
    conversion's proof all go through it, so what the proof checked is what a
    reader gets.
    """
    if not data or data[0] != _PAYLOAD_ENVELOPE_V1:
        raise ValueError("unknown search result envelope")
    return zstd.decompress(data[1:])


def _payload_text(stored: object) -> str | None:
    """The JSON text a stored ``output_json`` holds, in either encoding.

    TEXT is returned as it is; a BLOB is unwrapped from its envelope. A value
    that cannot be decoded reads as no payload and says so in the log, rather
    than failing the whole page.
    """
    if stored is None or isinstance(stored, str):
        return stored
    if not isinstance(stored, bytes | bytearray | memoryview):
        return None
    try:
        return _unwrap_payload(bytes(stored)).decode("utf-8")
    except (zstd.ZstdError, ValueError, MemoryError) as exc:
        logger.warning("Websearch log search result decode failed: {}", exc)
        return None


def _payload_matcher() -> Callable[[object, object, object], int]:
    """A fresh SQL ``mcc_websearch_match(id, output_json, needle)`` for one reader.

    1 when the compressed payload holds ``needle``, the way
    ``instr(lower(text), lower(?)) > 0`` decides on the TEXT it replaced:
    SQLite's ``lower()`` folds ASCII letters only, as ``bytes.lower()`` does,
    and on UTF-8 a character substring is a byte substring. ``needle`` arrives
    already through SQLite's ``lower()``.

    Remembers its answer per row and needle for the life of the connection: a
    statistics call runs a dozen queries with the same filter, and decoding
    every payload for each of them measured 1.6x slower than the plain text
    did. A row's payload never changes once written (the conversion keeps its
    bytes, and ids are never reused), so a remembered answer cannot go stale.
    Total, because an exception in a SQL function fails the whole query:
    anything undecodable holds nothing, as the detail shows no payload.
    """
    seen: dict[tuple[object, str], int] = {}

    def match(row_id: object, value: object, needle: object) -> int:
        if not isinstance(value, bytes) or not isinstance(needle, str):
            return 0
        key = (row_id, needle)
        found = seen.get(key)
        if found is None:
            try:
                data = _unwrap_payload(value)
            except zstd.ZstdError, ValueError, MemoryError:
                found = 0
            else:
                found = int(needle.encode("utf-8").lower() in data.lower())
            seen[key] = found
        return found

    return match


def _output_matches(id_column: str, column: str) -> str:
    """SQL: does ``column`` (output_json) hold the search term, in either encoding.

    A TEXT row stays on SQLite's own ``instr(lower(...), lower(?))``, untouched;
    only a BLOB goes through ``_payload_matcher``. Two placeholders, both bound
    to the term.
    """
    return (
        f"CASE WHEN typeof({column}) = 'blob'"
        f" THEN {_PAYLOAD_SQL_FUNCTION}({id_column}, {column}, lower(?))"
        f" ELSE instr(lower(COALESCE({column}, '')), lower(?)) > 0 END"
    )


def _freelist_count(connection: sqlite3.Connection) -> int:
    return int(connection.execute("PRAGMA freelist_count").fetchone()[0])


def _decode_json(value: object) -> object | None:
    if isinstance(value, bytes | bytearray | memoryview):
        value = _payload_text(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {"_invalid_json": True, "raw": value}


def _attempt_filter_where(
    *,
    provider: str | None,
    status: str | None,
    q: str | None,
    since_epoch: float | None,
    until_epoch: float | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if provider:
        # Comma-separated values mean "any of these providers" (multi-select).
        providers = [part for part in provider.split(",") if part]
        if providers:
            placeholders = ",".join("?" * len(providers))
            clauses.append(f"provider IN ({placeholders})")
            params.extend(providers)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if q:
        clauses.append(
            "(instr(lower(query), lower(?)) > 0"
            " OR instr(lower(COALESCE(input_json, '')), lower(?)) > 0"
            " OR " + _output_matches("id", "output_json") + ")"
        )
        params.extend((q, q, q, q))
    if since_epoch is not None:
        clauses.append("ts_epoch >= ?")
        params.append(since_epoch)
    if until_epoch is not None:
        clauses.append("ts_epoch <= ?")
        params.append(until_epoch)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def _route_filter_where(
    *,
    provider: str | None,
    status: str | None,
    q: str | None,
    since_epoch: float | None,
    until_epoch: float | None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if provider:
        clauses.append("instr(provider_path, '|' || ? || '|') > 0")
        params.append(provider)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if q:
        clauses.append(
            "(instr(lower(search_route_log.query), lower(?)) > 0"
            " OR EXISTS (SELECT 1 FROM search_log AS attempt"
            " WHERE attempt.route_id = search_route_log.route_id"
            " AND (instr(lower(COALESCE(attempt.input_json, '')), lower(?)) > 0"
            " OR " + _output_matches("attempt.id", "attempt.output_json") + ")))"
        )
        params.extend((q, q, q, q))
    if since_epoch is not None:
        clauses.append("ts_epoch >= ?")
        params.append(since_epoch)
    if until_epoch is not None:
        clauses.append("ts_epoch <= ?")
        params.append(until_epoch)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def _attempt_stats(
    connection: sqlite3.Connection,
    where: str,
    params: list[Any],
    period: str,
) -> dict[str, Any]:
    totals = _row_dict(
        connection.execute(
            "SELECT COUNT(*) AS requests,"
            " COALESCE(SUM(status = 'error'), 0) AS errors,"
            " AVG(duration_ms) AS avg_duration_ms,"
            " COALESCE(SUM(results_count), 0) AS results,"
            " SUM(cost_usd) AS cost_usd"
            f" FROM search_log {where}",
            params,
        ).fetchone()
    )
    by_provider = [
        _shaped_aggregate(row)
        for row in connection.execute(
            "SELECT provider, COUNT(*) AS requests,"
            " COALESCE(SUM(status = 'error'), 0) AS errors,"
            " AVG(duration_ms) AS avg_duration_ms,"
            " COALESCE(SUM(results_count), 0) AS results,"
            " SUM(cost_usd) AS cost_usd"
            f" FROM search_log {where} GROUP BY provider"
            " ORDER BY requests DESC, provider ASC",
            params,
        ).fetchall()
    ]
    by_key = [
        _shaped_aggregate(row)
        for row in connection.execute(
            "SELECT provider, key_label, COUNT(*) AS requests,"
            " COALESCE(SUM(status = 'error'), 0) AS errors,"
            " AVG(duration_ms) AS avg_duration_ms,"
            " COALESCE(SUM(results_count), 0) AS results"
            f" FROM search_log {where} GROUP BY provider, key_label"
            " ORDER BY requests DESC, provider ASC, key_label ASC",
            params,
        ).fetchall()
    ]
    error_where = f"{where} {'AND' if where else 'WHERE'} status = 'error'"
    top_errors = [
        _row_dict(row)
        for row in connection.execute(
            "SELECT error_kind, error_message, COUNT(*) AS count"
            f" FROM search_log {error_where}"
            " GROUP BY error_kind, error_message"
            " ORDER BY count DESC, error_kind ASC, error_message ASC"
            " LIMIT ?",
            (*params, _TOP_ERRORS_LIMIT),
        ).fetchall()
    ]
    series_rows = connection.execute(
        f"SELECT ts_epoch, provider, status, results_count FROM search_log {where}",
        params,
    ).fetchall()
    bounds = _row_dict(
        connection.execute(
            f"SELECT MIN(ts_epoch) AS first_ts_epoch,"
            f" MAX(ts_epoch) AS last_ts_epoch FROM search_log {where}",
            params,
        ).fetchone()
    )
    requests_total = int(totals["requests"])
    errors_total = int(totals["errors"])
    return {
        "totals": {
            "requests": requests_total,
            "successes": requests_total - errors_total,
            "errors": errors_total,
            "avg_duration_ms": _rounded(totals["avg_duration_ms"]),
            "results": int(totals["results"]),
            "cost_usd": totals["cost_usd"],
        },
        "by_provider": by_provider,
        "by_key": by_key,
        "series": _series(series_rows, period),
        "top_errors": top_errors,
        "window": {
            "since_epoch": bounds["first_ts_epoch"],
            "until_epoch": bounds["last_ts_epoch"],
        },
    }


def _route_stats(
    connection: sqlite3.Connection,
    where: str,
    params: list[Any],
    period: str,
) -> dict[str, Any]:
    totals = _row_dict(
        connection.execute(
            "SELECT COUNT(*) AS routes,"
            " COALESCE(SUM(status = 'error'), 0) AS errors,"
            " COALESCE(SUM(fallback_used), 0) AS fallbacks,"
            " AVG(attempt_count) AS avg_attempts,"
            " AVG(duration_ms) AS avg_duration_ms,"
            " COALESCE(SUM(results_count), 0) AS results,"
            " SUM(cost_usd) AS cost_usd"
            f" FROM search_route_log {where}",
            params,
        ).fetchone()
    )
    by_primary_provider = _route_breakdown(
        connection, "primary_provider", where, params
    )
    by_terminal_provider = _route_breakdown(
        connection, "terminal_provider", where, params
    )
    error_where = f"{where} {'AND' if where else 'WHERE'} status = 'error'"
    top_errors = [
        _row_dict(row)
        for row in connection.execute(
            "SELECT error_kind, error_message, COUNT(*) AS count"
            f" FROM search_route_log {error_where}"
            " GROUP BY error_kind, error_message"
            " ORDER BY count DESC, error_kind ASC, error_message ASC"
            " LIMIT ?",
            (*params, _TOP_ERRORS_LIMIT),
        ).fetchall()
    ]
    series_rows = connection.execute(
        "SELECT ts_epoch, terminal_provider AS provider, status, fallback_used,"
        f" results_count FROM search_route_log {where}",
        params,
    ).fetchall()
    bounds = _row_dict(
        connection.execute(
            f"SELECT MIN(ts_epoch) AS first_ts_epoch,"
            f" MAX(ts_epoch) AS last_ts_epoch FROM search_route_log {where}",
            params,
        ).fetchone()
    )
    last_row = connection.execute(
        f"SELECT {_ROUTE_COLUMNS} FROM search_route_log {where}"
        " ORDER BY ts_epoch DESC, id DESC LIMIT 1",
        params,
    ).fetchone()
    routes_total = int(totals["routes"])
    errors_total = int(totals["errors"])
    fallbacks_total = int(totals["fallbacks"])
    return {
        "totals": {
            "searches": routes_total,
            "successes": routes_total - errors_total,
            "errors": errors_total,
            "fallbacks": fallbacks_total,
            "fallback_rate": (
                round(fallbacks_total / routes_total, 6) if routes_total else 0.0
            ),
            "avg_attempts": _rounded(totals["avg_attempts"]),
            "avg_duration_ms": _rounded(totals["avg_duration_ms"]),
            "results": int(totals["results"]),
            "cost_usd": totals["cost_usd"],
        },
        "by_primary_provider": by_primary_provider,
        "by_terminal_provider": by_terminal_provider,
        "series": _route_series(series_rows, period),
        "top_errors": top_errors,
        "last_route": _route_dict(last_row) if last_row is not None else None,
        "window": {
            "since_epoch": bounds["first_ts_epoch"],
            "until_epoch": bounds["last_ts_epoch"],
        },
    }


def _route_breakdown(
    connection: sqlite3.Connection,
    column: str,
    where: str,
    params: list[Any],
) -> list[dict[str, Any]]:
    return [
        _shaped_route_aggregate(row)
        for row in connection.execute(
            f"SELECT {column} AS provider, COUNT(*) AS searches,"
            " COALESCE(SUM(status = 'error'), 0) AS errors,"
            " COALESCE(SUM(fallback_used), 0) AS fallbacks,"
            " AVG(duration_ms) AS avg_duration_ms,"
            " COALESCE(SUM(results_count), 0) AS results,"
            " SUM(cost_usd) AS cost_usd"
            f" FROM search_route_log {where} GROUP BY {column}"
            " ORDER BY searches DESC, provider ASC",
            params,
        ).fetchall()
    ]


def _shaped_route_aggregate(row: sqlite3.Row) -> dict[str, Any]:
    shaped = _row_dict(row)
    shaped["searches"] = int(shaped["searches"])
    shaped["errors"] = int(shaped["errors"])
    shaped["fallbacks"] = int(shaped["fallbacks"])
    shaped["avg_duration_ms"] = _rounded(shaped["avg_duration_ms"])
    shaped["results"] = int(shaped["results"])
    return shaped


def _route_dict(row: sqlite3.Row) -> dict[str, Any]:
    shaped = _row_dict(row)
    shaped["providers"] = _decode_provider_path(str(shaped.pop("provider_path")))
    shaped["fallback_used"] = bool(shaped["fallback_used"])
    return shaped


def _shaped_aggregate(row: sqlite3.Row) -> dict[str, Any]:
    shaped = _row_dict(row)
    shaped["requests"] = int(shaped["requests"])
    shaped["errors"] = int(shaped["errors"])
    shaped["avg_duration_ms"] = _rounded(shaped["avg_duration_ms"])
    shaped["results"] = int(shaped["results"])
    return shaped


def _series(rows: list[sqlite3.Row], period: str) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        bucket = _time_bucket(float(row["ts_epoch"]), period)
        key = (bucket, row["provider"])
        entry = buckets.setdefault(
            key,
            {
                "bucket": bucket,
                "provider": row["provider"],
                "requests": 0,
                "errors": 0,
                "results": 0,
            },
        )
        entry["requests"] += 1
        entry["errors"] += 1 if row["status"] == "error" else 0
        entry["results"] += row["results_count"]
    return sorted(
        buckets.values(), key=lambda entry: (entry["bucket"], entry["provider"])
    )


def _route_series(rows: list[sqlite3.Row], period: str) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        bucket = _time_bucket(float(row["ts_epoch"]), period)
        key = (bucket, row["provider"])
        entry = buckets.setdefault(
            key,
            {
                "bucket": bucket,
                "provider": row["provider"],
                "searches": 0,
                "errors": 0,
                "fallbacks": 0,
                "results": 0,
            },
        )
        entry["searches"] += 1
        entry["errors"] += 1 if row["status"] == "error" else 0
        entry["fallbacks"] += int(row["fallback_used"])
        entry["results"] += row["results_count"]
    return sorted(
        buckets.values(), key=lambda entry: (entry["bucket"], entry["provider"])
    )


def _time_bucket(ts_epoch: float, period: str) -> str:
    moment = datetime.fromtimestamp(ts_epoch, tz=UTC)
    if period == "hourly":
        return moment.strftime("%Y-%m-%dT%H:00")
    if period == "daily":
        return moment.strftime("%Y-%m-%d")
    if period == "weekly":
        iso = moment.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    return f"{moment.year}-{moment.month:02d}"


def _encode_provider_path(providers: tuple[str, ...]) -> str:
    return f"|{'|'.join(providers)}|"


def _decode_provider_path(encoded: str) -> list[str]:
    return [provider for provider in encoded.split("|") if provider]


def _ensure_auto_vacuum(connection: sqlite3.Connection) -> None:
    """Enable incremental auto-vacuum so pruned pages leave the file.

    Without it ``_prune`` only moves pages onto the internal freelist: the
    database file grows forever under row-based retention. A fresh database
    adopts the pragma directly; an existing one (live installs started with
    ``auto_vacuum=0``) needs a one-time full ``VACUUM`` rebuild followed by
    ``ANALYZE`` so the planner keeps statistics for the rewritten pages. This
    runs on the writer thread at startup, never on a request path.
    """
    try:
        mode = connection.execute("PRAGMA auto_vacuum").fetchone()[0]
        if int(mode) == 2:
            return
        previous = connection.isolation_level
        connection.isolation_level = None
        try:
            # Autocommit: the pragma is a no-op inside a transaction and
            # VACUUM refuses to run inside one.
            connection.execute("PRAGMA auto_vacuum=INCREMENTAL")
            connection.execute("VACUUM")
            connection.execute("ANALYZE")
        finally:
            connection.isolation_level = previous
    except sqlite3.Error as exc:
        logger.warning("Websearch analytics auto_vacuum setup failed: {}", exc)


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(_SCHEMA)
    _ensure_column(
        connection,
        "search_log",
        "route_id",
        "ALTER TABLE search_log ADD COLUMN route_id TEXT",
    )
    _ensure_column(
        connection,
        "search_log",
        "attempt_number",
        "ALTER TABLE search_log ADD COLUMN attempt_number INTEGER NOT NULL DEFAULT 1",
    )
    for column, alter_sql in (
        ("input_json", "ALTER TABLE search_log ADD COLUMN input_json TEXT"),
        ("output_json", "ALTER TABLE search_log ADD COLUMN output_json TEXT"),
        (
            "provider_config_json",
            "ALTER TABLE search_log ADD COLUMN provider_config_json TEXT",
        ),
        ("input_chars", "ALTER TABLE search_log ADD COLUMN input_chars INTEGER"),
        ("output_chars", "ALTER TABLE search_log ADD COLUMN output_chars INTEGER"),
        ("input_sha256", "ALTER TABLE search_log ADD COLUMN input_sha256 TEXT"),
        ("output_sha256", "ALTER TABLE search_log ADD COLUMN output_sha256 TEXT"),
        (
            "content_captured",
            "ALTER TABLE search_log ADD COLUMN content_captured"
            " INTEGER NOT NULL DEFAULT 0",
        ),
    ):
        _ensure_column(connection, "search_log", column, alter_sql)
    connection.executescript(_POST_MIGRATION_SCHEMA)
    connection.commit()


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    alter_sql: str,
) -> None:
    columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    if column in columns:
        return
    try:
        connection.execute(alter_sql)
    except sqlite3.OperationalError:
        # A concurrent reader/writer initialization may have won the migration race.
        columns = {
            str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")
        }
        if column not in columns:
            raise


def _rounded(value: Any) -> float | None:
    return round(float(value), 3) if value is not None else None
