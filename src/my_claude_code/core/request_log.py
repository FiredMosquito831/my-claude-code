"""SQLite-backed request log with a non-blocking background writer."""

import base64
import contextlib
import hashlib
import json
import math
import os
import queue
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Generator, Iterator, Mapping, Sequence
from compression import zstd
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from my_claude_code.core.client_fingerprint import harness_from_headers
from my_claude_code.core.request_images import CapturedImage
from my_claude_code.core.upstream_ladder import format_status_census

# ``core`` must not import ``config`` (import-boundary contract), so the
# request-log path is not computed here. ``config.paths.request_log_path``
# owns the directory; ``set_request_log_path`` records the resolved default
# for this process and every entrypoint calls it before the first store is
# opened. See ``tests/contracts/test_config_dir_is_single_sourced.py``, which
# pins the two modules' column inventories together.
_default_request_log_path: Path | None = None

#: What the one-time cost backfill calls to price one historical row:
#: ``(provider, model, tokens_in, tokens_out, cache_read, cache_write)`` ->
#: ``(cost_usd, cost_source)``.
#:
#: Injected for the same reason the path above is. The pricing ladder lives in
#: ``application.cost`` and its rungs are catalogues in ``providers``; ``core``
#: may import neither. Re-implementing the ladder here would be a second answer
#: to the question ``cost_source`` exists to record the answer to, and the two
#: would drift the first time a rung changed. So the composition root hands the
#: real one in, and a process that never registers one simply never backfills.
CostBackfillPricer = Callable[
    [str | None, str | None, int | None, int | None, int | None, int | None],
    tuple[float | None, str | None],
]

_cost_backfill_pricer: CostBackfillPricer | None = None


def _utc_day(ts_epoch: float) -> str:
    """The UTC calendar day one timestamp falls in, as ``YYYY-MM-DD``.

    The same spelling ``_COST_DIMENSION_SQL`` groups by, so a backfill's
    progress and the cost card's day buckets name the same days.
    """
    return datetime.fromtimestamp(ts_epoch, tz=UTC).strftime("%Y-%m-%d")


def _next_utc_day(day: str) -> str | None:
    """The day after ``day``, or None if that is not a day at all.

    None rather than a raise: the value arrives from a stored marker, and a
    database carrying a nonsense one must cost a rescan, never a start.
    """
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None
    return (parsed + timedelta(days=1)).strftime("%Y-%m-%d")


def _utc_day_bounds(day: str) -> tuple[float, float]:
    """``[start, end)`` epoch seconds for one UTC day.

    Half-open, so consecutive days neither overlap nor leave a second between
    them -- a row at exactly midnight belongs to the day that starts there.
    """
    start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=UTC)
    return (start.timestamp(), (start + timedelta(days=1)).timestamp())


def set_cost_backfill_pricer(pricer: CostBackfillPricer | None) -> None:
    """Register (or clear) the pricer the historical cost backfill may use.

    Called by the server's composition root once the models.dev catalogue is
    known to be on disk, and by tests in their teardown. Until it is called the
    backfill does not run at all -- which is the desired behaviour on a cold
    cache, because the backfill's "nobody publishes a rate for this" is
    recorded permanently and must never be recorded about a catalogue that was
    merely missing.
    """

    global _cost_backfill_pricer
    _cost_backfill_pricer = pricer


RequestStatus = Literal["success", "error", "cancelled"]

MAX_TEXT_CHARS = 50_000
MAX_ERROR_CHARS = 2_000
LIST_BODY_PREVIEW_CHARS = 4_096
_PRUNE_EVERY_INSERTS = 100
_WRITER_BATCH_SIZE = 50
_WRITER_POLL_SECONDS = 0.25
_QUEUE_MAX_SIZE = 10_000
_STOP = object()
# Shutdown budget for draining the queue. Compressing a full batch is real CPU
# work, so this is a floor that grows with whatever is still queued.
_CLOSE_TIMEOUT_SECONDS = 10.0
_CLOSE_SECONDS_PER_RECORD = 0.01
_STATS_CACHE_TTL_SECONDS = 5.0
# Memory-mapped reads hand SQLite the operating system's page cache directly
# instead of copying every page into the connection's own buffer. On the 4.5 GB
# log this was measured against, that is the largest single win available
# without changing one line of SQL: the per-host image estimate went 1.221 s ->
# 0.183 s and a filtered COUNT(*) 0.133 s -> 0.080 s, while `cache_size` tuning
# did nothing at all.
#
# Sized from the file rather than fixed: a small log should not reserve a
# gigabyte of address space, and a huge one gains nothing past the cap. The
# headroom factor keeps a growing database mapped between connections.
#
# The cost, which sqlite.org/mmap.html is explicit about: an I/O error inside a
# mapped region arrives as SIGBUS / EXCEPTION_IN_PAGE_ERROR instead of
# SQLITE_IOERR. This is a local file on a local disk, which is the case that
# documentation calls acceptable. Builds compiled with SQLITE_MAX_MMAP_SIZE=0
# ignore the pragma, and any error applying it is suppressed -- an unmapped
# connection is the behaviour of every release before this one.
_MMAP_HEADROOM = 1.25
_MMAP_MAX_BYTES = 1 << 30
# Bounds the stats cache to the most recently used filter combinations. Without
# this, every distinct filter tuple a user tries leaks an entry holding a full
# stats payload for the lifetime of the process.
_STATS_CACHE_MAX_ENTRIES = 64
# Caps each breakdown (by provider/model/key) so a gateway with hundreds of
# distinct models does not return hundreds of rows on every poll.
_BREAKDOWN_LIMIT = 50

# ------------------------------------------------------------ stats rollup --
#
# ``stats()`` used to scan ``requests`` (and ``request_attempts``) once per
# aggregate. On a 244k-row / 766k-attempt database that measured 31.0 seconds
# for an all-time call under ``local=hide``. The three ``request_stats_*``
# tables below are a pre-aggregated mirror of exactly those aggregates, keyed
# on one UTC hour plus every dimension ``_where`` can filter on, maintained on
# insert inside the writer's existing transaction. The same call measures
# ~0.1 s against them.
#
# These are schema constants, not settings. Changing a bucket edge or a
# dimension invalidates every stored row, so they are deliberately not
# configurable: a knob here would silently corrupt history.

# Log-spaced latency histogram. 64 buckets from 1 ms to 30 minutes was picked
# by measurement: it holds all-time p50/p95/p99 error to <= 2.3% on the real
# log at 26k stored rows, against 1.9-2.6% for 48 buckets and 0.15-0.96% for
# 80. Bucket 0 is "under a millisecond"; bucket 63 is the open-ended tail.
_LATENCY_BUCKETS = 64
_LATENCY_FLOOR_MS = 1.0
_LATENCY_CEILING_MS = 1_800_000.0
_LATENCY_STEP = math.log(_LATENCY_CEILING_MS / _LATENCY_FLOOR_MS) / (
    _LATENCY_BUCKETS - 2
)

# Backfill markers in ``request_log_meta``. ``_ROLLUP_BACKFILL_THROUGH_KEY``
# carries the exclusive upper hour of the last committed chunk so a restart
# resumes rather than restarting; ``_ROLLUP_BACKFILL_KEY`` is written only when
# the walk reaches the end and is what ``stats()`` checks before serving from
# the rollup at all.
_IS_LOCAL_BACKFILL_KEY = "is_local_backfilled_at"
_HARNESS_BACKFILL_KEY = "harness_backfilled_at"
# Versioned, and the version is load-bearing. ``harness`` joined
# ``_ROLLUP_DIMENSIONS`` after these tables shipped, so an installed database
# carries a completion marker for a rollup keyed on nine columns. Reusing the
# old names would let ``_ensure_rollup_backfill`` read that marker as "already
# done" and serve a rollup with no harness dimension in it -- a confident wrong
# answer, which is worse than the seconds a rebuild costs. Nothing an older
# release wrote can satisfy the v2 names.
_ROLLUP_BACKFILL_KEY = "rollup_backfilled_at_v2"
_ROLLUP_BACKFILL_THROUGH_KEY = "rollup_backfilled_through_v2"
# The names their predecessors used, deleted when the rebuild is scheduled so a
# database stops carrying a marker nothing will ever read again.
_SUPERSEDED_ROLLUP_KEYS = ("rollup_backfilled_at", "rollup_backfilled_through")
# Hours folded per committed transaction. One 14-second transaction would push
# the whole rollup into the WAL before any checkpoint could run.
_ROLLUP_CHUNK_HOURS = 24
# Rows updated per committed chunk of the ``is_local`` backfill.
_IS_LOCAL_CHUNK_ROWS = 5_000
# Rows classified per committed chunk of the ``harness`` backfill. Measured on
# the real 272 132-row log: 55 chunks, 15.8 s in total.
_HARNESS_CHUNK_ROWS = 5_000
# Versioned for the reason the rollup keys above are: the ladder that priced
# these rows is a moving thing, and bumping the name is how a future release
# says "price them again" without a user step. Nothing an older release wrote
# can satisfy the v1 name, because no older release wrote anything here.
_COST_BACKFILL_KEY = "cost_backfilled_at_v1"
#: The last UTC day the walk completed, ``YYYY-MM-DD``. Progress, not
#: correctness: the predicate below is what makes the walk resumable, and this
#: only saves a finished day from being re-scanned. A missing or nonsense value
#: costs one extra pass over days that have nothing left to do.
_COST_BACKFILL_THROUGH_KEY = "cost_backfilled_through_v1"
# Rows priced per committed chunk. A day is already a good tick -- 0.48-3.10 s
# on the measured log, busiest day 24,004 rows -- and this bounds a future day
# ten times that size from holding the writer.
_COST_CHUNK_ROWS = 5_000
#: Stored on a row the backfill tried and could not price. The same string as
#: ``application.cost.SOURCE_UNPRICED``, which owns the vocabulary; ``core``
#: may not import it, so ``tests/contracts`` pins the two together. It is
#: spelled here because ``cost_breakdown`` has to keep it out of the list of
#: sources that priced something.
_UNPRICED_COST_SOURCE = "unpriced"
_HOUR_SECONDS = 3_600

# A request answered by a local optimization rule never reached a provider, so
# its ``provider`` column is NULL by design. Grouping it under "(unknown)" was
# accurate about the column and wrong about the fact: we know exactly what
# served it, and the ``optimization`` column names the rule. These two keys let
# every provider-shaped surface -- breakdowns, filters, exports -- distinguish
# "answered inside the proxy by this rule" from "we genuinely have no idea".
LOCAL_PROVIDER_PREFIX = "local:"
UNKNOWN_PROVIDER_KEY = "(unknown)"

#: SQL matching a request MCC answered itself: no provider was called and a
#: rule named the answer. ``provider IS NULL AND optimization IS NULL`` is the
#: ``(unknown)`` case instead -- traffic whose provider we genuinely do not
#: know -- and is deliberately NOT a local answer.
LOCAL_ANSWER_SQL = "(provider IS NULL AND optimization IS NOT NULL)"

#: The same fact as a stored column. ``LOCAL_ANSWER_SQL`` remains the single
#: definition of the *rule* -- it is what computes this column on insert and
#: what the backfill matches -- but reading it back through a predicate over
#: ``optimization`` cost the covering index: SQLite abandoned
#: ``idx_requests_stats_v3`` (which does not carry ``optimization``) and read
#: the base table, making ``local=hide`` slower than ``local=all`` on every
#: scan. Indexed as the leading column of ``idx_requests_stats_v4``, the same
#: filter is an equality seek.
LOCAL_ANSWER_COLUMN_SQL = "is_local"

#: Accepted values for the ``local`` read filter. ``all`` is the default
#: everywhere in the store and the API; only the dashboard prefers ``hide``.
LOCAL_FILTER_VALUES = frozenset({"all", "hide", "only"})

#: SQL producing the provider grouping key. Kept as one expression so the
#: breakdown, the export dimension and the filter predicate cannot drift apart.
PROVIDER_KEY_SQL = (
    "CASE WHEN provider IS NOT NULL THEN provider"
    f" WHEN optimization IS NOT NULL THEN '{LOCAL_PROVIDER_PREFIX}' || optimization"
    f" ELSE '{UNKNOWN_PROVIDER_KEY}' END"
)

#: ``PROVIDER_KEY_SQL`` against the rollup, whose dimension columns store the
#: empty string where ``requests`` stores SQL NULL. Kept beside the original so
#: the two groupings cannot drift; both produce the same keys on the same data.
ROLLUP_PROVIDER_KEY_SQL = (
    "CASE WHEN provider <> '' THEN provider"
    f" WHEN optimization <> '' THEN '{LOCAL_PROVIDER_PREFIX}' || optimization"
    f" ELSE '{UNKNOWN_PROVIDER_KEY}' END"
)

#: "provider/model" as the fallback and diversion lists render it. One
#: constant so the raw query, the rollup writer and the rollup reader cannot
#: disagree about how a served-by string is spelled.
SERVED_BY_KEY_SQL = (
    f"COALESCE(provider, '{UNKNOWN_PROVIDER_KEY}') || '/' ||"
    f" COALESCE(resolved_model, '{UNKNOWN_PROVIDER_KEY}')"
)

#: Dimensions the cost breakdown is grouped by, and the SQL that names each
#: key. Mirrors ``core.export._REQUEST_DIMENSION_SQL`` deliberately: a cost card
#: and an export of the same window must group the same rows under the same key
#: or one of the two is lying about the other.
_COST_DIMENSION_SQL: dict[str, str] = {
    "provider": PROVIDER_KEY_SQL,
    "model": "COALESCE(resolved_model, '(unknown)')",
    "harness": "COALESCE(harness, '(unknown)')",
    "day": "strftime('%Y-%m-%d', ts_epoch, 'unixepoch')",
}


def _cost_row(row: Any, *, key: str | None) -> dict[str, Any]:
    """Shape one cost aggregate, keeping every NULL a NULL."""
    shaped: dict[str, Any] = {
        "reported_usd": row["reported_usd"],
        "estimated_usd": row["estimated_usd"],
        "priced": row["priced"] or 0,
        "requests": row["requests"] or 0,
    }
    if key is not None:
        shaped["key"] = key
    return shaped


def _copy_cost_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a cost breakdown deeply enough that a caller cannot edit the cache.

    ``stats()`` gets away with a shallow ``dict()`` because its own callers do
    not mutate the lists inside it; this payload is four lists of dictionaries
    and a route could reasonably annotate one. Two hundred rows at most, so the
    copy is far cheaper than the bug it forecloses.
    """

    copied: dict[str, Any] = {}
    for name, value in payload.items():
        if isinstance(value, list):
            copied[name] = [dict(row) for row in value]
        elif isinstance(value, Mapping):
            copied[name] = dict(value)
        else:
            copied[name] = value
    return copied


def _cost_sort_key(row: Mapping[str, Any]) -> tuple[float, int]:
    """Order cost rows by spend, then by volume.

    The zeros here are an ordering decision, not a stored value: an unpriced
    group sorts last rather than being renamed to free.
    """
    return (
        (row.get("reported_usd") or 0.0) + (row.get("estimated_usd") or 0.0),
        row.get("requests") or 0,
    )


# Days of per-rule history the optimizer page plots. Fourteen daily buckets is
# what a sparkline can carry legibly; the companion table shows the same rows.
_OPTIMIZATION_SERIES_DAYS = 14

# Columns read for list views. Body columns are deliberately excluded and
# replaced by SQL-side ``substr`` previews so list queries never load full
# request/response bodies into memory just to truncate them in Python.
_LIST_METADATA_COLUMNS = (
    "id",
    "ts_epoch",
    "ts_iso",
    "endpoint",
    "protocol",
    "requested_model",
    "provider",
    "resolved_model",
    "stream",
    "input_sha256",
    "output_sha256",
    "input_chars",
    "output_chars",
    "reasoning",
    "params",
    "tokens_in",
    "tokens_out",
    "cache_read_tokens",
    "cache_write_tokens",
    "ttft_ms",
    "duration_ms",
    "status",
    "error_kind",
    "error_message",
    "headers",
    "route_attempt",
    "route_primary_model",
    "route_chain",
    "route_diverted_from",
    "route_diversion",
    "key_index",
    "key_label",
    # Why the applied reasoning policy differs from what was asked for: the
    # warning gating would otherwise emit only to the server log. NULL whenever
    # gating changed nothing, so a list row never carries an empty warning.
    "reasoning_adaptation",
    "reasoning_adaptation_kind",
    # Shape of the assistant turn. These are counts, not bodies, so list views
    # can show what a turn contained without loading the transcript.
    "thinking_chars",
    "tool_call_count",
    # How many images or documents the request carried. A count, not pixels,
    # so a list row can say "this turn had a screenshot in it" for free.
    "input_image_count",
    # How those images actually travelled: "image" when the model received a
    # picture, "stripped" when it was replaced by a sentence because the model
    # is published as blind, "text" if a path ever flattens one to base64
    # again, "none" when the request carried nothing visual. NULL means the row
    # predates the column, which is not the same as "none" and is drawn as a
    # dash rather than as a measurement.
    "image_delivery",
    # Which local rule answered this request without contacting a provider,
    # and the input tokens that never went upstream because it did. NULL on
    # every ordinary request and on every row written before the column
    # existed -- "no rule matched" and "nobody was recording" are the same
    # shape here only because no rule could have fired unrecorded.
    "optimization",
    "optimization_tokens_saved",
    # Which coding agent sent this request. Projected into the list so a row
    # can be labelled without being opened, and so the harness filter and the
    # rows it selects are visibly the same fact.
    "harness",
    # What it cost and who said so. Both, always: an amount whose provenance
    # the reader cannot see is a number they cannot act on, and the list row
    # is where the "est." badge is decided.
    "cost_usd",
    "cost_source",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    ts_epoch REAL NOT NULL,
    ts_iso TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    protocol TEXT NOT NULL,
    requested_model TEXT,
    provider TEXT,
    resolved_model TEXT,
    route_attempt INTEGER,
    route_primary_model TEXT,
    route_chain TEXT,
    route_diverted_from TEXT,
    route_diversion TEXT,
    stream INTEGER NOT NULL DEFAULT 0,
    input_text TEXT,
    output_text TEXT,
    input_sha256 TEXT,
    output_sha256 TEXT,
    input_chars INTEGER,
    output_chars INTEGER,
    reasoning TEXT,
    requested_reasoning TEXT,
    params TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER,
    ttft_ms REAL,
    duration_ms REAL,
    status TEXT NOT NULL,
    error_kind TEXT,
    error_message TEXT,
    headers TEXT,
    key_index INTEGER,
    key_label TEXT,
    thinking_text TEXT,
    thinking_chars INTEGER,
    tool_calls TEXT,
    tool_call_count INTEGER,
    optimization TEXT,
    optimization_tokens_saved INTEGER,
    input_image_count INTEGER,
    image_delivery TEXT,
    harness TEXT
);
CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(ts_epoch);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS idx_requests_provider ON requests(provider);
CREATE INDEX IF NOT EXISTS idx_requests_model ON requests(resolved_model);
"""

# Permanent aggregates, deliberately outside ``requests``.
#
# ``prune`` caps ``requests`` at ``max_rows``, so every figure derived from that
# table is a rolling window: once the cap is reached one row leaves for every
# row that arrives and the sums stop moving. These counters are incremented once
# per request and never deleted by retention, so "all time" stays true however
# far the window has slid.
#
# ``server_sessions`` answers the other half of the same question. A flat
# stretch in the request series is ambiguous on its own -- no traffic and no
# server look identical -- so the writer records when a server was actually
# running.
_TOTALS_SCHEMA = """
CREATE TABLE IF NOT EXISTS request_totals (
    day TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 0,
    error INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0,
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    served_by_fallback INTEGER NOT NULL DEFAULT 0,
    diverted INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (day, provider, model)
);
CREATE TABLE IF NOT EXISTS server_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    last_seen_at REAL NOT NULL,
    pid INTEGER,
    host TEXT,
    port INTEGER
);
CREATE INDEX IF NOT EXISTS idx_server_sessions_started
    ON server_sessions(started_at);
CREATE TABLE IF NOT EXISTS request_log_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Dimensions every rollup row is keyed on, in the order the primary key
# declares them. Nine columns, one UTC hour of grain.
#
# ``requested_model`` and ``optimization`` are dimensions even though no card
# groups by them, because ``_where`` filters on both: a model filter matches
# ``resolved_model`` OR ``requested_model``, and a ``local:<rule>`` provider
# key resolves to ``provider IS NULL AND optimization = ?``. Without them a
# filter the dashboard itself can produce would be unservable.
#
# Hour and not day: ``_series`` switches to hourly buckets for windows under
# 48 hours. Hour grain costs 4.6x the rows of day grain (4 626 against 993 on
# the measured log) and is what makes that switch possible.
#
# ``harness`` is the tenth and is cheap to key on: 12 distinct values across
# the whole measured log, and a harness barely varies inside an hour bucket
# already split by provider, model and key, so the table grows by a small
# factor rather than by twelve.
_ROLLUP_DIMENSIONS = (
    "hour_epoch",
    "is_local",
    "provider",
    "resolved_model",
    "requested_model",
    "status",
    "endpoint",
    "key_label",
    "optimization",
    "harness",
)

# Columns added to ``image_blobs`` after it shipped. Same rule as the request
# and attempt lists above: ``CREATE TABLE IF NOT EXISTS`` never revises an
# existing table, so each one needs its own guarded ``ALTER TABLE``.
_IMAGE_BLOB_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("description", "ALTER TABLE image_blobs ADD COLUMN description TEXT"),
    ("described_by", "ALTER TABLE image_blobs ADD COLUMN described_by TEXT"),
    ("described_at", "ALTER TABLE image_blobs ADD COLUMN described_at REAL"),
    ("sent_width", "ALTER TABLE image_blobs ADD COLUMN sent_width INTEGER"),
    ("sent_height", "ALTER TABLE image_blobs ADD COLUMN sent_height INTEGER"),
)

# NULL is stored as the empty string, not as a sentinel word, so the reverse
# mapping is one ``CASE WHEN x <> ''`` and no sentinel can collide with a real
# value. Measured on the real log: no empty-string provider, model, key or
# optimization exists in 244 425 rows.
_ROLLUP_DIMENSION_DDL = "\n".join(
    f"    {name} {'INTEGER' if name in {'hour_epoch', 'is_local'} else 'TEXT'}"
    " NOT NULL,"
    for name in _ROLLUP_DIMENSIONS
)

# (column, DDL type, expression that produces it from a ``requests`` scan).
#
# Every entry maps 1:1 onto a ``CASE WHEN`` in the ``totals`` query of
# ``stats()``. Keeping the three uses -- DDL, backfill SQL and the insert-time
# accumulator -- generated from this one tuple is what stops them drifting.
#
# Averages are not stored, because averages are not additive: the sum and the
# non-NULL count are, and ``sum / count`` reproduces SQLite's ``AVG`` exactly.
_ROLLUP_COUNTERS: tuple[tuple[str, str, str], ...] = (
    ("requests", "INTEGER", "COUNT(*)"),
    ("tokens_in", "INTEGER", "COALESCE(SUM(tokens_in), 0)"),
    ("tokens_out", "INTEGER", "COALESCE(SUM(tokens_out), 0)"),
    ("cache_read_tokens", "INTEGER", "COALESCE(SUM(cache_read_tokens), 0)"),
    ("cache_write_tokens", "INTEGER", "COALESCE(SUM(cache_write_tokens), 0)"),
    (
        "cache_reported",
        "INTEGER",
        "SUM(CASE WHEN cache_read_tokens IS NOT NULL THEN 1 ELSE 0 END)",
    ),
    ("tool_calls", "INTEGER", "COALESCE(SUM(tool_call_count), 0)"),
    (
        "turns_with_tools",
        "INTEGER",
        "SUM(CASE WHEN tool_call_count > 0 THEN 1 ELSE 0 END)",
    ),
    (
        "turns_with_reasoning",
        "INTEGER",
        "SUM(CASE WHEN thinking_chars > 0 THEN 1 ELSE 0 END)",
    ),
    (
        "served_by_fallback",
        "INTEGER",
        "SUM(CASE WHEN route_attempt > 0 THEN 1 ELSE 0 END)",
    ),
    (
        "route_reported",
        "INTEGER",
        "SUM(CASE WHEN route_attempt IS NOT NULL THEN 1 ELSE 0 END)",
    ),
    (
        "diverted",
        "INTEGER",
        "SUM(CASE WHEN route_diverted_from IS NOT NULL THEN 1 ELSE 0 END)",
    ),
    (
        "vision_unavailable",
        "INTEGER",
        "SUM(CASE WHEN route_diversion = 'vision_unavailable' THEN 1 ELSE 0 END)",
    ),
    (
        "vision_described",
        "INTEGER",
        "SUM(CASE WHEN route_diversion = 'vision_described' THEN 1 ELSE 0 END)",
    ),
    (
        "with_images",
        "INTEGER",
        "SUM(CASE WHEN input_image_count > 0 THEN 1 ELSE 0 END)",
    ),
    ("duration_sum", "REAL", "COALESCE(SUM(duration_ms), 0)"),
    (
        "duration_count",
        "INTEGER",
        "SUM(CASE WHEN duration_ms IS NOT NULL THEN 1 ELSE 0 END)",
    ),
    ("ttft_sum", "REAL", "COALESCE(SUM(ttft_ms), 0)"),
    (
        "ttft_count",
        "INTEGER",
        "SUM(CASE WHEN ttft_ms IS NOT NULL THEN 1 ELSE 0 END)",
    ),
    # Attempt-derived, so the ``requests`` pass leaves them at zero and a
    # second pass over ``request_attempts`` adds them in the same transaction.
    ("early_retries", "INTEGER", "0"),
    ("midstream_recoveries", "INTEGER", "0"),
    ("salvages", "INTEGER", "0"),
)

_ROLLUP_COUNTER_NAMES = tuple(name for name, _type, _sql in _ROLLUP_COUNTERS)

# Dimensions of the "grouped list, LIMIT 10" facts. Same as the main rollup:
# ``status`` stays a dimension because a status filter genuinely restricts the
# fallback, diversion and upstream lists (only the error list implies its own
# status), and dropping it would make those three wrong under a status filter.
_DETAIL_DIMENSIONS = _ROLLUP_DIMENSIONS

#: The four grouped lists, distinguished by ``kind`` and carrying up to three
#: extra grouping values in ``a``/``b``/``c``.
_DETAIL_ERROR = "error"
_DETAIL_FALLBACK = "fallback"
_DETAIL_DIVERTED = "diverted"
_DETAIL_UPSTREAM = "upstream"

_ROLLUP_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS request_stats_rollup (
{_ROLLUP_DIMENSION_DDL}
{
    chr(10).join(
        f"    {name} {ddl} NOT NULL DEFAULT 0," for name, ddl, _sql in _ROLLUP_COUNTERS
    )
}
    PRIMARY KEY ({", ".join(_ROLLUP_DIMENSIONS)})
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS request_stats_latency (
{_ROLLUP_DIMENSION_DDL}
    bucket INTEGER NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY ({", ".join(_ROLLUP_DIMENSIONS)}, bucket)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS request_stats_detail (
{_ROLLUP_DIMENSION_DDL}
    kind TEXT NOT NULL,
    a TEXT NOT NULL DEFAULT '',
    b TEXT NOT NULL DEFAULT '',
    c TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY ({", ".join(_DETAIL_DIMENSIONS)}, kind, a, b, c)
) WITHOUT ROWID;
"""

# Names of the three tables, for the places that treat them as one unit
# (``clear``, and the test that asserts ``prune`` leaves them alone).
_ROLLUP_TABLES = (
    "request_stats_rollup",
    "request_stats_latency",
    "request_stats_detail",
)


def _upsert_sql(table: str, keys: tuple[str, ...], counters: tuple[str, ...]) -> str:
    """Build an additive upsert from one column tuple.

    The column list, the placeholder list and the ``DO UPDATE SET`` clause are
    all generated from the same tuple, so a column can never be added without
    its marker -- the failure mode that once shipped a 43-column INSERT with 42
    markers and broke every write.
    """
    columns = (*keys, *counters)
    return (
        f"INSERT INTO {table} ({', '.join(columns)})"
        f" VALUES ({', '.join('?' * len(columns))})"
        f" ON CONFLICT({', '.join(keys)}) DO UPDATE SET "
        + ", ".join(f"{name} = {name} + excluded.{name}" for name in counters)
    )


def _stored_headers(raw: Any) -> dict[str, str] | None:
    """Read one row's stored ``headers`` column back into a mapping.

    Anything unusable -- NULL, invalid JSON, a JSON scalar -- becomes ``None``,
    which the classifier answers ``unknown`` for. Raising instead would let one
    malformed blob abort the backfill and leave every row after it
    unattributed, which is a far larger loss than the one row it protects.
    """
    if not isinstance(raw, str):
        return None
    try:
        decoded = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(decoded, dict):
        return None
    return {
        str(name): value for name, value in decoded.items() if isinstance(value, str)
    }


def _is_local_value(record: RequestRecord) -> int:
    """``LOCAL_ANSWER_SQL`` applied to a record about to be written.

    One definition, three users: the stored column, the chunked backfill's
    predicate and the rollup dimension all come from this rule.
    """
    return int(record.provider is None and record.optimization is not None)


def _floor_hour(ts_epoch: float) -> int:
    """Return the start of the UTC hour containing ``ts_epoch``."""
    return int(ts_epoch // _HOUR_SECONDS) * _HOUR_SECONDS


def _latency_bucket(duration_ms: float) -> int:
    """Return the histogram bucket one duration falls in."""
    if duration_ms < _LATENCY_FLOOR_MS:
        return 0
    index = 1 + int(math.log(duration_ms / _LATENCY_FLOOR_MS) / _LATENCY_STEP)
    return min(_LATENCY_BUCKETS - 1, max(0, index))


def _latency_bucket_edges(bucket: int) -> tuple[float, float]:
    """Return the [low, high) millisecond edges of one bucket."""
    if bucket <= 0:
        return (0.0, _LATENCY_FLOOR_MS)
    return (
        _LATENCY_FLOOR_MS * math.exp((bucket - 1) * _LATENCY_STEP),
        _LATENCY_FLOOR_MS * math.exp(bucket * _LATENCY_STEP),
    )


# The same assignment in SQL, for the backfill's ``GROUP BY``.
#
# ``LN`` and not ``LOG``: SQLite's ``LOG(X)`` is base 10, and using it against a
# natural-log step is exactly the bug that produced 94-99% percentile error in
# the first measurement pass of this design. The step is emitted at full
# precision from the Python constant rather than rounded into the string, so
# the two implementations cannot disagree at a bucket edge.
_LATENCY_BUCKET_SQL = (
    f"MIN({_LATENCY_BUCKETS - 1}, MAX(0,"
    f" CASE WHEN duration_ms < {_LATENCY_FLOOR_MS!r} THEN 0"
    f" ELSE 1 + CAST(LN(duration_ms / {_LATENCY_FLOOR_MS!r})"
    f" / {_LATENCY_STEP!r} AS INTEGER) END))"
)


def _rollup_dimension_select(prefix: str = "") -> tuple[str, ...]:
    """Return the ten dimension expressions read off a ``requests`` row."""
    return (
        f"CAST({prefix}ts_epoch / {_HOUR_SECONDS} AS INTEGER) * {_HOUR_SECONDS}",
        f"{prefix}is_local",
        f"COALESCE({prefix}provider, '')",
        f"COALESCE({prefix}resolved_model, '')",
        f"COALESCE({prefix}requested_model, '')",
        f"{prefix}status",
        f"{prefix}endpoint",
        f"COALESCE({prefix}key_label, '')",
        f"COALESCE({prefix}optimization, '')",
        f"COALESCE({prefix}harness, '')",
    )


_ROLLUP_UPSERT_SQL = _upsert_sql(
    "request_stats_rollup", _ROLLUP_DIMENSIONS, _ROLLUP_COUNTER_NAMES
)
_LATENCY_UPSERT_SQL = _upsert_sql(
    "request_stats_latency", (*_ROLLUP_DIMENSIONS, "bucket"), ("count",)
)
_DETAIL_UPSERT_SQL = _upsert_sql(
    "request_stats_detail",
    (*_DETAIL_DIMENSIONS, "kind", "a", "b", "c"),
    ("count", "requests"),
)

# Request and response text, moved out of ``requests`` and compressed.
#
# Bodies are 99% of the bytes on a real database: 30.7 KB a row against 332
# bytes of metadata. Two things follow. They belong in their own table, because
# a row larger than a page spills into a chain of overflow pages that every
# table scan then has to walk. And they compress extremely well, because
# consecutive requests repeat a near-identical system prompt and conversation
# history -- 2.7x compressed individually, 9x against a dictionary trained on
# the traffic itself.
#
# Rows written by an older version keep their text in the ``requests`` columns
# and are read from there. ``compact_request_log`` converts them in place; until
# it runs, or retention drains them, both forms coexist.
#
# Bodies are content-addressed: identical content is stored once and shared, and
# a repeat skips compression entirely. Keyed on the whole body that is nearly
# worthless -- 1.4% on a real log, because two requests sharing a prompt still
# differ in their reply -- which is why the prompt is stored separately below.
_BODIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS body_blobs (
    sha TEXT PRIMARY KEY,
    dict_id INTEGER,
    payload BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS request_bodies (
    request_id TEXT PRIMARY KEY,
    sha TEXT,
    input_sha TEXT
);
CREATE TABLE IF NOT EXISTS body_dictionaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    content BLOB NOT NULL
);
-- Images a request carried, content-addressed on the *source* bytes. Claude
-- Code re-sends the whole conversation every turn, so one pasted screenshot
-- reaches the proxy again on every following request; keying on the image
-- itself stores it once instead of once per turn. Only a downscaled copy is
-- kept -- the request detail needs to show what the model looked at, not to
-- reproduce the original file.
CREATE TABLE IF NOT EXISTS image_blobs (
    sha TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    media_type TEXT,
    source_bytes INTEGER,
    width INTEGER,
    height INTEGER,
    thumbnail_media_type TEXT,
    thumbnail BLOB,
    -- What a sighted model said this picture shows, when the vision adapter
    -- ran in ``describe`` mode. It belongs on the picture rather than on the
    -- request because the picture is what it describes: Claude Code re-sends
    -- the same screenshot on every turn, and one description then serves all
    -- of them. NULL means nobody has described it, which is the common case.
    description TEXT,
    described_by TEXT,
    described_at REAL,
    -- The size this picture actually left at, when the outbound downscaler
    -- shrank it. NULL means it was not resized -- either because it was
    -- already inside the budget or because resizing is off -- which is a
    -- different fact from "it was sent at its stored width", and only NULL
    -- says the first one. Stored on the picture rather than on the request
    -- because the resize is a function of the picture and the budget, so one
    -- screenshot re-sent every turn is resized to the same size every time.
    sent_width INTEGER,
    sent_height INTEGER
);
CREATE TABLE IF NOT EXISTS request_images (
    request_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    sha TEXT NOT NULL,
    PRIMARY KEY (request_id, position)
);
-- One row per model the chain reached, or deliberately did not reach.
--
-- ``requests`` holds one row per request, so it can only ever name the model
-- that answered: ``route_attempt`` is the index that won and ``error_kind`` is
-- the *final* outcome. When a primary failed and a fallback succeeded, the row
-- said "success" and the reason the primary failed existed only in a log line.
-- Measured over 21 days of real traffic: 1,144 successful fallbacks, and for
-- the largest cohort of 319 the reason was recoverable from the database in
-- exactly 0 of them.
--
-- A side table rather than more columns, because the number of attempts is a
-- property of the chain, not of the schema, and because "attempt 2 was skipped
-- because the budget was already spent" is a fact about an attempt that never
-- ran and therefore has no column on the request.
CREATE TABLE IF NOT EXISTS request_attempts (
    request_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    provider TEXT,
    model_ref TEXT,
    outcome TEXT NOT NULL,
    error_kind TEXT,
    error_message TEXT,
    duration_ms REAL,
    -- What the provider did to keep this attempt alive, as a small JSON
    -- object: {"early_retries": n, "midstream_recoveries": n, "salvages": n}.
    -- Written only when something was counted; NULL on every row written
    -- before recovery was recorded, which is "not measured", not zero.
    params TEXT,
    -- The outbound body actually handed to the provider SDK, as redacted JSON
    -- with prompt text replaced by prompt structure. Recorded at the commit
    -- boundary, so it is the body after every postprocessor, override and
    -- retry rewrite -- not the client's original ask.
    wire_body TEXT,
    -- 1/0: did that outbound body end up carrying a reasoning instruction at
    -- all? ``requests.reasoning_adaptation`` says what gating decided; this
    -- says whether the encoder acted on it. NULL means "not measured".
    reasoning_emitted INTEGER,
    -- Number of upstream tries this attempt actually made, across every
    -- credential the pool handed it. 1 on a clean single-try attempt; NULL on
    -- every row written before the ladder existed, which is "not measured"
    -- and emphatically not "no retries happened". Denormalised out of
    -- ``params.ladder`` so the analytics status breakdown can restrict its
    -- JSON scan to the rows that have a ladder at all.
    ladder_tries INTEGER,
    -- What this attempt itself reported spending. Filled today by the vision
    -- adapter's describe calls, whose tokens had no column at all before
    -- 6.53.0 and were therefore thrown away; nullable and general so any
    -- attempt that can report usage may use them. NULL is "not measured".
    tokens_in INTEGER,
    tokens_out INTEGER,
    PRIMARY KEY (request_id, attempt)
);
"""

# Keys inside the packed payload. Short because they repeat in every blob.
#
# The prompt is stored in its own blob, apart from the reply, the reasoning and
# the tool calls. It is 98% of the bytes and 35.3% of those bytes are exact
# repeats -- a retry or a parallel subagent re-sends the same context, while the
# reply that came back differs every time. Keeping them together meant a body
# only deduplicated when *everything* matched, which measured 1.4%.
_INPUT_FIELDS = (("i", "input_text"),)
_REST_FIELDS = (
    ("o", "output_text"),
    ("t", "thinking_text"),
    ("c", "tool_calls"),
)
_BODY_FIELDS = _INPUT_FIELDS + _REST_FIELDS

# Level 9 is the knee: 19 buys about 5% more ratio for 11x the CPU (1.8 MB/s
# against 20 MB/s measured on real bodies).
_BODY_COMPRESSION_LEVEL = 9
# 110 KB is where the dictionary stops paying: 16 KB gives 4.3x, 110 KB gives
# 9.9x, 512 KB gives 10.0x.
_BODY_DICT_SIZE = 110 * 1024
# Below this there is not enough traffic to train anything useful, so bodies are
# compressed without a dictionary until the log has seen enough.
_BODY_DICT_MIN_SAMPLES = 256
_BODY_DICT_TRAINING_SAMPLES = 1_024

# Columns a content search covers on rows still stored inline. Reasoning and
# tool calls are more than half of what a real log contains -- 55% of requests
# carry thinking text and 78% carry tool calls -- so omitting them made search
# quietly blind to most of the transcript.
_SEARCHED_COLUMNS = ("input_text", "output_text", "thinking_text", "tool_calls")

# Aggregate columns of ``request_totals``, in the order the upsert binds them.
_TOTALS_COUNTERS = (
    "requests",
    "success",
    "error",
    "cancelled",
    "tokens_in",
    "tokens_out",
    "cache_read_tokens",
    "cache_write_tokens",
    "tool_calls",
    "served_by_fallback",
    "diverted",
)

# Upsert one (day, provider, model) bucket. Unqualified names on the right of
# ``DO UPDATE SET`` resolve to the stored row, so this adds to the running total
# rather than replacing it.
_TOTALS_UPSERT_SQL = (
    f"INSERT INTO request_totals (day, provider, model, {', '.join(_TOTALS_COUNTERS)})"
    f" VALUES (?, ?, ?, {', '.join('?' * len(_TOTALS_COUNTERS))})"
    " ON CONFLICT(day, provider, model) DO UPDATE SET "
    + ", ".join(f"{name} = {name} + excluded.{name}" for name in _TOTALS_COUNTERS)
)

_TOTALS_BACKFILL_KEY = "totals_backfilled_at"
# How often the writer thread refreshes its session row. Small enough that a
# hard kill leaves at most this much uncertainty about when the server stopped.
_SESSION_HEARTBEAT_SECONDS = 30.0
# Bounds ``server_sessions`` growth; one row per server start, so this is years
# of restarts on any normal machine.
_SESSION_HISTORY_LIMIT = 1_000

# Columns added to ``server_sessions`` in 6.72.2. Until then a session row said
# a server had been running and said nothing about *where*, so "this pid's port
# now belongs to somebody else" -- the only safe evidence that a server has
# been superseded -- was not a question the log could answer. Same guarded
# ALTER rule as every other post-release column.
_SESSION_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("host", "ALTER TABLE server_sessions ADD COLUMN host TEXT"),
    ("port", "ALTER TABLE server_sessions ADD COLUMN port INTEGER"),
)

# The address the server in THIS process is bound to, published by the
# supervisor once it knows. A module-level fact rather than a constructor
# argument because the request log is built (and its session row opened) before
# the listener exists, and because ``set_request_log_path`` beside it already
# establishes that shape for process-wide facts the store needs.
_bind_lock = threading.Lock()
_bind_address: tuple[str, int] | None = None


def set_server_bind_address(host: str | None, port: int | None) -> None:
    """Record where this process's server is listening, for its session row.

    Called by the supervisor immediately after a successful bind, and with
    ``None`` when the listener closes: a session that is no longer serving
    must stop claiming a port, or the next server to take that port would read
    the stale claim as a rival.
    """

    global _bind_address
    with _bind_lock:
        _bind_address = None if host is None or port is None else (host, int(port))


def server_bind_address() -> tuple[str, int] | None:
    """Where this process's server is listening, or ``None`` if it is not."""

    with _bind_lock:
        return _bind_address


@dataclass(frozen=True, slots=True)
class ServerSession:
    """One recorded server run, as the log remembers it."""

    id: int
    pid: int | None
    started_at: float
    last_seen_at: float
    host: str | None = None
    port: int | None = None

    def heartbeat_age(self, now: float | None = None) -> float:
        return max(0.0, (time.time() if now is None else now) - self.last_seen_at)


def read_server_sessions(
    db_path: Path | str, *, limit: int = _SESSION_HISTORY_LIMIT
) -> list[ServerSession]:
    """Read recorded sessions from ``db_path`` without opening it for writing.

    ``mode=ro`` deliberately: every caller of this is asking a question about
    somebody *else's* server, frequently while an install is in flight, and a
    read-only handle cannot create a journal, cannot upgrade a schema and
    cannot be the reason another process's write fails.
    """

    path = Path(db_path)
    if not path.exists():
        return []
    uri = f"file:{path.as_posix()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        conn.row_factory = sqlite3.Row
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(server_sessions)")
        }
        if not columns:
            return []
        # Two literal statements rather than one interpolated column list: a
        # database written before 6.72.2 has no address columns, and a log
        # this old is exactly the one a migration must not be required to
        # touch before it can be read.
        if {"host", "port"} <= columns:
            query = (
                "SELECT id, pid, started_at, last_seen_at, host, port"
                " FROM server_sessions ORDER BY started_at DESC LIMIT ?"
            )
        else:
            query = (
                "SELECT id, pid, started_at, last_seen_at"
                " FROM server_sessions ORDER BY started_at DESC LIMIT ?"
            )
        rows = conn.execute(query, (int(limit),)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    sessions: list[ServerSession] = []
    for row in rows:
        keys = row.keys()
        sessions.append(
            ServerSession(
                id=int(row["id"]),
                pid=int(row["pid"]) if row["pid"] is not None else None,
                started_at=float(row["started_at"]),
                last_seen_at=float(row["last_seen_at"]),
                host=row["host"] if "host" in keys else None,
                port=int(row["port"])
                if "port" in keys and row["port"] is not None
                else None,
            )
        )
    return sessions


# Columns added after the initial release. ``CREATE TABLE IF NOT EXISTS`` is a
# no-op on an existing database, so each one needs an explicit ALTER TABLE.
_ADDED_COLUMNS = (
    ("key_index", "ALTER TABLE requests ADD COLUMN key_index INTEGER"),
    ("key_label", "ALTER TABLE requests ADD COLUMN key_label TEXT"),
    ("cache_read_tokens", "ALTER TABLE requests ADD COLUMN cache_read_tokens INTEGER"),
    (
        "cache_write_tokens",
        "ALTER TABLE requests ADD COLUMN cache_write_tokens INTEGER",
    ),
    ("thinking_text", "ALTER TABLE requests ADD COLUMN thinking_text TEXT"),
    ("thinking_chars", "ALTER TABLE requests ADD COLUMN thinking_chars INTEGER"),
    ("tool_calls", "ALTER TABLE requests ADD COLUMN tool_calls TEXT"),
    ("tool_call_count", "ALTER TABLE requests ADD COLUMN tool_call_count INTEGER"),
    ("route_attempt", "ALTER TABLE requests ADD COLUMN route_attempt INTEGER"),
    (
        "route_primary_model",
        "ALTER TABLE requests ADD COLUMN route_primary_model TEXT",
    ),
    ("route_chain", "ALTER TABLE requests ADD COLUMN route_chain TEXT"),
    (
        "route_diverted_from",
        "ALTER TABLE requests ADD COLUMN route_diverted_from TEXT",
    ),
    ("route_diversion", "ALTER TABLE requests ADD COLUMN route_diversion TEXT"),
    (
        "input_image_count",
        "ALTER TABLE requests ADD COLUMN input_image_count INTEGER",
    ),
    # Added in 6.49.0 with the fix that made a tool-returned image arrive as an
    # image. Rows written before it keep NULL forever: "we were not measuring"
    # is a different fact from "nothing visual was sent", and only NULL says
    # the first one.
    (
        "image_delivery",
        "ALTER TABLE requests ADD COLUMN image_delivery TEXT",
    ),
    # The reasoning intent as asked for, before per-model capability gating.
    # ``reasoning`` holds the *applied* policy: since per-model gating landed it
    # records what was actually sent, and rewriting that history would be worse
    # than the ambiguity it fixes. Rows written before this column existed keep
    # NULL here forever -- deliberately NOT backfilled, because "we do not know
    # what was requested" is a different fact from "the request was sent
    # unchanged", and only NULL can say the first one.
    (
        "requested_reasoning",
        "ALTER TABLE requests ADD COLUMN requested_reasoning TEXT",
    ),
    (
        "reasoning_adaptation",
        "ALTER TABLE requests ADD COLUMN reasoning_adaptation TEXT",
    ),
    # The programmatic half of the adaptation. ``reasoning_adaptation`` is
    # prose written for an operator and reworded whenever gating is reworded;
    # the kind is a fixed vocabulary (unchanged/substituted/clamped/dropped/
    # suppressed) the UI can style on without pattern-matching a sentence.
    # NULL on every unadapted request and on every row written before the
    # column existed, which is why the wire pane badges nothing without it.
    (
        "reasoning_adaptation_kind",
        "ALTER TABLE requests ADD COLUMN reasoning_adaptation_kind TEXT",
    ),
    (
        "optimization",
        "ALTER TABLE requests ADD COLUMN optimization TEXT",
    ),
    (
        "optimization_tokens_saved",
        "ALTER TABLE requests ADD COLUMN optimization_tokens_saved INTEGER",
    ),
    # "This request never reached a provider", stored rather than re-derived.
    # ``DEFAULT 0`` makes an un-backfilled database wrong but safe: until
    # ``_ensure_is_local_backfill`` runs, ``local=hide`` shows the local rows
    # it should be hiding rather than hiding rows it should be showing.
    (
        "is_local",
        "ALTER TABLE requests ADD COLUMN is_local INTEGER NOT NULL DEFAULT 0",
    ),
    # Which coding agent sent the request. Nullable with no default, unlike
    # ``is_local``: every row written from now on is classified at capture time
    # and can never be NULL, so NULL means exactly "written before this column
    # existed and not yet backfilled" -- which is what
    # ``_ensure_harness_backfill`` uses as its own progress cursor. A
    # ``DEFAULT 'unknown'`` would have erased that distinction and left the
    # whole history unrecoverably unattributed.
    ("harness", "ALTER TABLE requests ADD COLUMN harness TEXT"),
    # Added in 6.53.0. What the vision adapter's own describe calls cost,
    # rolled up from this request's describe attempts so analytics can scan it
    # without a JSON walk. Deliberately NOT folded into ``tokens_in``: that
    # column measures the model that answered, it has measured exactly that
    # since the log existed, and quietly widening its meaning would change
    # every historical chart without changing a single stored number. NULL
    # means not measured -- which covers every row written before this column
    # and every request where no describe call ran.
    (
        "adapter_tokens_in",
        "ALTER TABLE requests ADD COLUMN adapter_tokens_in INTEGER",
    ),
    (
        "adapter_tokens_out",
        "ALTER TABLE requests ADD COLUMN adapter_tokens_out INTEGER",
    ),
    # What the proxy's own estimator thought this request would cost, and how
    # much of that was pictures. Stored because the single biggest reason a
    # tokens-per-pixel table could not be produced from 275,304 logged requests
    # is that the proxy had never once written down what it estimated: without
    # this there is nothing to compare a bill against. Two integers per row,
    # and the only thing that can audit the estimator over the next 30 days.
    ("est_tokens_in", "ALTER TABLE requests ADD COLUMN est_tokens_in INTEGER"),
    (
        "est_image_tokens",
        "ALTER TABLE requests ADD COLUMN est_image_tokens INTEGER",
    ),
    # Image payload before and after the outbound downscaler, in bytes. NULL
    # when nothing was resized, which is distinct from 0.
    ("image_bytes_in", "ALTER TABLE requests ADD COLUMN image_bytes_in INTEGER"),
    (
        "image_bytes_out",
        "ALTER TABLE requests ADD COLUMN image_bytes_out INTEGER",
    ),
    # Added in 6.54.0. What this request cost, in USD, and which source said
    # so. Resolved once, at the capture commit, and never recomputed at read
    # time: a price that changes next month must not silently rewrite last
    # month's bill.
    #
    # NULL is "not priced" and it has to survive every layer above this one.
    # It is emphatically NOT zero -- zero is a claim that the request was free,
    # which is a claim only a source that publishes a zero may make. Three of
    # the five implementations surveyed for this feature destroy that
    # distinction, each at a different layer, so there is no
    # ``COALESCE(cost_usd, 0)`` anywhere that reads this column.
    #
    # Deliberately NOT backfilled. Historical rows stay NULL forever: pricing
    # 275,000 old requests at today's rates would produce a confident number
    # that was never anybody's bill.
    ("cost_usd", "ALTER TABLE requests ADD COLUMN cost_usd REAL"),
    ("cost_source", "ALTER TABLE requests ADD COLUMN cost_source TEXT"),
)

# Indexes over post-release columns, created only once those columns exist.
# Keeping them out of ``_SCHEMA`` matters: that script runs before the ALTER
# TABLE migration, so indexing ``key_label`` there would fail outright on a
# database created by an earlier version.
# Same rule for the per-attempt side table: ``CREATE TABLE IF NOT EXISTS``
# never revises an existing definition, so each column added after
# ``request_attempts`` shipped needs its own guarded ALTER.
_ATTEMPT_ADDED_COLUMNS = (
    ("params", "ALTER TABLE request_attempts ADD COLUMN params TEXT"),
    ("wire_body", "ALTER TABLE request_attempts ADD COLUMN wire_body TEXT"),
    (
        "reasoning_emitted",
        "ALTER TABLE request_attempts ADD COLUMN reasoning_emitted INTEGER",
    ),
    ("key_index", "ALTER TABLE request_attempts ADD COLUMN key_index INTEGER"),
    ("key_label", "ALTER TABLE request_attempts ADD COLUMN key_label TEXT"),
    ("ladder_tries", "ALTER TABLE request_attempts ADD COLUMN ladder_tries INTEGER"),
    # Added in 6.53.0. ``request_attempts`` shipped with fourteen columns and
    # none of them were tokens, so the vision adapter's describe hop -- which
    # is recorded here and nowhere else -- had its usage discarded entirely
    # even though the SSE aggregator hands it over. Nullable and general: any
    # attempt that can report its own usage may fill these, and NULL keeps its
    # "not measured" meaning on every row that already exists.
    ("tokens_in", "ALTER TABLE request_attempts ADD COLUMN tokens_in INTEGER"),
    ("tokens_out", "ALTER TABLE request_attempts ADD COLUMN tokens_out INTEGER"),
    # Added in 6.54.0 with per-request costing. A describe hop is a real call
    # to a real model on a real key, and since 6.53.0 it reports its own
    # tokens, so it can be priced -- separately from the request that provoked
    # it, because it is a different model on a different route. NULL is "not
    # priced", never zero, exactly as on the parent row.
    ("cost_usd", "ALTER TABLE request_attempts ADD COLUMN cost_usd REAL"),
    ("cost_source", "ALTER TABLE request_attempts ADD COLUMN cost_source TEXT"),
)

# Written in this order by ``_record_to_row``. The INSERT's column list, its
# placeholder list and the runtime width assertion are all generated from this
# tuple for the same reason ``_ATTEMPT_INSERT_COLUMNS`` exists: a hand-written
# 43-column INSERT with 42 markers once shipped and broke every write, and the
# ``requests`` INSERT was the one site that still had no guard.
_REQUEST_INSERT_COLUMNS = (
    "id",
    "ts_epoch",
    "ts_iso",
    "endpoint",
    "protocol",
    "requested_model",
    "provider",
    "resolved_model",
    "stream",
    "input_text",
    "output_text",
    "input_sha256",
    "output_sha256",
    "input_chars",
    "output_chars",
    "reasoning",
    "requested_reasoning",
    "reasoning_adaptation",
    "reasoning_adaptation_kind",
    "params",
    "tokens_in",
    "tokens_out",
    "cache_read_tokens",
    "cache_write_tokens",
    "ttft_ms",
    "duration_ms",
    "status",
    "error_kind",
    "error_message",
    "headers",
    "key_index",
    "key_label",
    "thinking_text",
    "thinking_chars",
    "tool_calls",
    "tool_call_count",
    "route_attempt",
    "route_primary_model",
    "route_chain",
    "route_diverted_from",
    "route_diversion",
    "input_image_count",
    "image_delivery",
    "optimization",
    "optimization_tokens_saved",
    "is_local",
    "harness",
    "adapter_tokens_in",
    "adapter_tokens_out",
    "est_tokens_in",
    "est_image_tokens",
    "image_bytes_in",
    "image_bytes_out",
    "cost_usd",
    "cost_source",
)

_REQUEST_INSERT_SQL = (
    "INSERT OR REPLACE INTO requests"
    f" ({', '.join(_REQUEST_INSERT_COLUMNS)})"
    f" VALUES ({', '.join('?' * len(_REQUEST_INSERT_COLUMNS))})"
)

# Every column of ``requests`` this rollup was designed against.
#
# A future column that carries a new fact -- a new ``CASE WHEN`` in the totals
# query, a new grouping -- would otherwise land silently, and the rollup would
# report a confident zero for it forever. The contract test compares this set
# against ``PRAGMA table_info(requests)`` so adding a column fails the suite
# until its author has decided, explicitly, whether it is a rollup dimension, a
# rollup counter, or neither.
_ROLLUP_ACKNOWLEDGED_COLUMNS = frozenset(_REQUEST_INSERT_COLUMNS)


# The columns a ``requests`` table must have to be one this store wrote. This
# is the original ``CREATE TABLE`` set: every column in ``_REQUEST_INSERT_COLUMNS``
# that is not added later by ``_ADDED_COLUMNS``. It is mirrored in
# ``config.paths.LEGACY_REQUEST_LOG_COLUMNS`` (``core`` may not import
# ``config``), and ``tests/contracts/test_config_dir_is_single_sourced.py`` fails
# if the two ever drift. Used by the legacy-config health check.
def required_request_columns() -> frozenset[str]:
    """Return the columns a ``requests`` table must have to be from this store."""

    added = {column for column, _ in _ADDED_COLUMNS}
    return frozenset(
        column for column in _REQUEST_INSERT_COLUMNS if column not in added
    )


# Written in this order by ``_store_attempts``; the placeholder count is
# asserted against it so a column can never be added without its marker.
_ATTEMPT_INSERT_COLUMNS = (
    "request_id",
    "attempt",
    "provider",
    "model_ref",
    "outcome",
    "error_kind",
    "error_message",
    "duration_ms",
    "params",
    "wire_body",
    "reasoning_emitted",
    "key_index",
    "key_label",
    "ladder_tries",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "cost_source",
)

# Blank, not zero: a request whose attempts predate the ladder measured
# nothing, and "0 tries" would be a claim the database cannot support.
_EMPTY_LADDER_ROLLUP: dict[str, Any] = {
    "ladder_tries": None,
    "ladder_statuses": "",
    "ladder_root_cause": "",
}

# Created here rather than in ``_SCHEMA`` for the same reason as the columns
# above: this runs after the ALTER TABLE pass, which is the only point at which
# ``harness`` is guaranteed to exist.
#
# Its own index rather than a widening of ``idx_requests_stats_v4``: the
# comment on ``_ensure_stats_index`` records that every column added to that
# one is another chance of the planner regression already measured there.
# Versioned by name, so changing the column list means ``_v2`` plus a drop.
# Measured: 0.7 s to build, and it takes the all-time harness breakdown from a
# full table scan to 756 ms.
_ADDED_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_requests_key ON requests(key_label)",
    "CREATE INDEX IF NOT EXISTS idx_requests_harness_v1 ON requests(harness, ts_epoch)",
)

# Created on the writer thread by ``_ensure_partial_indexes``, which carries the
# measurements. Kept here beside ``_ADDED_INDEXES`` so every index this module
# creates after the schema script is readable in one place.
#
# The image index carries every column its query reads, so the plan is
# index-only and never touches a row: measured 0.072 s with the narrow column
# list against 0.002 s with this one, for 168 KB more index. That is the one
# place a wide index pays, because the index holds 2.7% of the rows rather than
# all of them.
_PARTIAL_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_requests_image_v1 ON requests("
    " ts_epoch, provider, status, cache_read_tokens, tokens_in,"
    " est_tokens_in, est_image_tokens, input_image_count)"
    " WHERE est_image_tokens IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_requests_optimization_v1 ON requests("
    " optimization, ts_epoch, optimization_tokens_saved)"
    " WHERE optimization IS NOT NULL",
)


def pack_fields(values: dict[str, Any], fields: tuple[tuple[str, str], ...]) -> bytes:
    """Serialise the named body fields of one request into a blob."""
    packed = {
        short: values[name] for short, name in fields if values.get(name) is not None
    }
    return json.dumps(packed, separators=(",", ":")).encode("utf-8")


def pack_bodies(values: dict[str, Any]) -> bytes:
    """Serialise every body field together, as a single combined blob."""
    return pack_fields(values, _BODY_FIELDS)


def _strings_in(value: Any) -> Iterator[str]:
    """Yield every string *value* inside a nested structure.

    Used for ``tool_calls``. Searching its JSON encoding instead would both
    miss (``C:\\Users`` is stored escaped) and mislead (every row contains the
    key name ``command``), so only the values a reader actually sees count.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings_in(item)


def searchable_text(bodies: dict[str, Any]) -> str:
    """Everything about a request that a content search should look at."""
    parts = [
        value
        for key in ("input_text", "output_text", "thinking_text")
        if isinstance(value := bodies.get(key), str)
    ]
    parts.extend(_strings_in(bodies.get("tool_calls")))
    return "\n".join(parts)


def _packed_or_none(packed: bytes) -> bytes | None:
    return None if packed == b"{}" else packed


def _is_json_transparent(needle: str) -> bool:
    """True when JSON encoding leaves ``needle`` byte-identical.

    ``json.dumps`` rewrites only ``"``, ``\\`` and control characters, so any
    other string appears verbatim inside the encoded payload.
    """
    return not any(char in '"\\' or char < " " for char in needle)


def unpack_bodies(raw: bytes) -> dict[str, Any]:
    """Inverse of :func:`pack_bodies`, tolerant of a corrupt or truncated blob."""
    try:
        packed = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return {}
    if not isinstance(packed, dict):
        return {}
    # Only the keys actually present: absence is what distinguishes a blob
    # holding just the reply from an older one that also carried the prompt,
    # which is how both layouts can be read without a version flag.
    return {name: packed[short] for short, name in _BODY_FIELDS if short in packed}


def set_request_log_path(path: Path | None) -> None:
    """Record the resolved request-log path as this process's default.

    Every MCC entrypoint calls this (via ``cli.entrypoints``) right after the
    config directory is resolved and before any store is opened, so the path
    honours ``MCC_CONFIG_DIR`` and the ``~/.mcc``/legacy-``~/.fcc`` rule. Tests call it
    directly to point the log at a scratch directory; pass ``None`` to clear
    the recorded path (used in test teardown).
    """

    global _default_request_log_path
    _default_request_log_path = Path(path) if path is not None else None


def default_request_log_path() -> Path:
    """Return the canonical request log database path for this process.

    Returns the path last passed to ``set_request_log_path``. ``core`` must not
    import ``config`` (import-boundary contract, enforced by
    ``tests/contracts/test_import_boundaries.py``), so there is no fallback
    that resolves the directory here -- every entrypoint registers the path
    before the first store is opened. Raises ``RuntimeError`` if it was never
    registered.
    """

    if _default_request_log_path is None:
        raise RuntimeError(
            "Request log path is not registered for this process. "
            "set_request_log_path() must be called from the entrypoint before "
            "the first request log store is opened; ``core`` cannot resolve it "
            "itself because the import-boundary contract forbids ``core`` from "
            "importing ``config``."
        )
    return _default_request_log_path


def cap_text(text: str | None, limit: int = MAX_TEXT_CHARS) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit]


class RouteAttemptOutcome(StrEnum):
    """What became of one model on a route.

    ``SKIPPED`` is the one that pays for itself: an attempt that never ran is
    invisible in every other signal, so a three-model chain that only ever
    tried one looked identical to a one-model route. The reason it was skipped
    travels in ``error_message``.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class RouteAttempt:
    """One model the chain reached, and what happened when it did."""

    attempt: int
    provider: str | None
    model_ref: str | None
    outcome: RouteAttemptOutcome
    error_kind: str | None = None
    error_message: str | None = None
    duration_ms: float | None = None
    # What this attempt's provider did to survive: transparent early retries,
    # midstream recoveries, salvaged continuations. Absent (None) on every
    # attempt where nothing was counted -- including everything written
    # before recovery observability existed.
    params: dict[str, Any] | None = None
    # The redacted, text-free outbound body this attempt actually sent, as a
    # JSON string. None on every attempt written before wire capture existed
    # and on any attempt whose provider has no instrumented commit boundary.
    wire_body: str | None = None
    # Whether that body carried a reasoning instruction. None is "not
    # measured" and is deliberately distinct from False.
    reasoning_emitted: bool | None = None
    # The credential this attempt used, captured at the attempt boundary
    # rather than at the end of the request: a route that rotates keys or
    # crosses providers used to be attributed entirely to its last one.
    # ``key_index`` -1 with the sentinel label means the pool had nothing
    # available and the attempt never reached a key.
    key_index: int | None = None
    key_label: str | None = None
    # How many upstream tries this attempt made, counting every credential the
    # pool handed it. None on every attempt written before the ladder existed,
    # which is "not measured" -- not "it went through on the first try".
    ladder_tries: int | None = None
    # What this attempt itself reported spending upstream. Filled by the
    # vision adapter's describe calls, whose usage the SSE aggregator has
    # always returned and which nothing has ever read. None is "not measured",
    # which is what every ordinary attempt still says: the request row's own
    # counters come from the client-facing stream, not from here.
    tokens_in: int | None = None
    tokens_out: int | None = None
    # What this attempt cost on its own, and which rung of the pricing ladder
    # said so. Filled for a describe hop, which is the one attempt kind that
    # reports usage; NULL everywhere else, because an ordinary attempt's cost
    # is the request row's and duplicating it here would double every total
    # that ever joined the two tables.
    cost_usd: float | None = None
    cost_source: str | None = None


# ---------------------------------------------------- recovery observability --
#
# The recovery decisions live deep inside a provider's stream runner, while the
# request log is finalized at the API boundary. Rather than widening every
# provider signature with out-parameters, the capture installs one mutable
# collector for the life of the request and the runner increments it as its
# RecoveryController takes each action -- the same shape credential
# attribution uses, and for the same reason: mutating one shared object stays
# visible through any number of context copies, including the child tasks a
# streaming response runs in.
#
# Counters are bucketed by route-chain index. The capture advances
# ``current_attempt`` at every attempt boundary (``set_routing`` fires before
# the attempt's provider stream starts), so an event recorded between two
# boundaries belongs to the attempt in flight -- including the common case
# where every counter lands on attempt 0 of a single-model route.

RECOVERY_EARLY_RETRIES = "early_retries"
RECOVERY_MIDSTREAM_RECOVERIES = "midstream_recoveries"
RECOVERY_SALVAGES = "salvages"

# The three recovery counters, in the order ``_ROLLUP_COUNTERS`` declares them.
# They are attempt-derived, so the rollup's ``requests`` pass leaves them at
# zero and a second pass over ``request_attempts`` adds them; the names are the
# JSON keys ``_store_attempts`` writes into ``request_attempts.params``, which
# is why they are shared rather than repeated.
_ROLLUP_RECOVERY_COUNTERS = (
    RECOVERY_EARLY_RETRIES,
    RECOVERY_MIDSTREAM_RECOVERIES,
    RECOVERY_SALVAGES,
)


@dataclass(slots=True)
class RecoveryTrace:
    """Mutable per-request collector of provider stream-recovery counters."""

    current_attempt: int = 0
    # Chain index -> {"early_retries": n, "midstream_recoveries": n, ...}
    events: dict[int, dict[str, int]] = field(default_factory=dict)

    def record(self, kind: str) -> None:
        bucket = self.events.setdefault(self.current_attempt, {})
        bucket[kind] = bucket.get(kind, 0) + 1


_RECOVERY_TRACE: ContextVar[RecoveryTrace | None] = ContextVar(
    "fcc_recovery_trace", default=None
)


def install_recovery_trace() -> RecoveryTrace:
    """Start recording stream recovery for the current request."""
    slot = RecoveryTrace()
    _RECOVERY_TRACE.set(slot)
    return slot


@contextlib.contextmanager
def paused_recovery_trace() -> Iterator[None]:
    """Stop counting stream recovery for the duration of a nested call.

    A describe call is a request MCC issues on its own behalf. Its retries are
    real and are recorded on its own attempt row; adding them to the parent's
    counters would say the client's answer was rescued when it was never in
    trouble.
    """
    token = _RECOVERY_TRACE.set(None)
    try:
        yield
    finally:
        _RECOVERY_TRACE.reset(token)


def record_recovery_event(kind: str) -> None:
    """Record one recovery action for the tracked request, if any.

    A no-op outside a tracked request, so providers exercised directly (unit
    tests, token counting) need no special handling.
    """
    slot = _RECOVERY_TRACE.get()
    if slot is not None:
        slot.record(kind)


@dataclass(slots=True)
class RequestRecord:
    """One completed request, queued for the background writer."""

    id: str
    endpoint: str
    protocol: str
    ts_epoch: float = field(default_factory=time.time)
    requested_model: str | None = None
    provider: str | None = None
    resolved_model: str | None = None
    # 0 when the route's own model answered, 1+ when a fallback did. ``None``
    # on rows written before fallback chains existed, which is distinct from
    # 0 and must stay that way: an old row cannot claim it used its primary.
    route_attempt: int | None = None
    # The model the route resolved to first, recorded only when a later
    # attempt answered -- otherwise it just repeats ``resolved_model``.
    route_primary_model: str | None = None
    # Every model this request was prepared to try, in order, comma-joined.
    # Stored even when the primary answers: "the chain existed and was not
    # needed" and "there was no chain" are different facts about a route.
    route_chain: str | None = None
    # The route's own model, when a policy replaced the head of the chain, and
    # which policy did it (today only the vision adapter). Both null on an
    # ordinary route, so a non-null pair is the whole signal.
    route_diverted_from: str | None = None
    route_diversion: str | None = None
    stream: bool = False
    input_text: str | None = None
    output_text: str | None = None
    input_sha256: str | None = None
    output_sha256: str | None = None
    input_chars: int | None = None
    output_chars: int | None = None
    reasoning: str | None = None
    # The reasoning policy actually applied (post per-model gating) and the one
    # originally requested. ``requested_reasoning`` stays None on a row whose
    # writer did not know it; that is distinct from "requested == applied".
    requested_reasoning: str | None = None
    # The warning gating would otherwise emit only to the server log: why the
    # applied policy differs from what was asked for. NULL whenever gating
    # changed nothing and on every row written before this column existed --
    # "no warning was raised" and "nobody was recording" are the same shape
    # here only because no warning could have fired unrecorded.
    reasoning_adaptation: str | None = None
    # The same verdict as a fixed word rather than a sentence, so the UI
    # can distinguish a suppression from a clamp without reading prose.
    reasoning_adaptation_kind: str | None = None
    params: dict[str, Any] | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    # Anthropic reports these beside input_tokens; tokens_in is the
    # *uncached* portion, so total input is the sum of all three.
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    ttft_ms: float | None = None
    duration_ms: float | None = None
    status: RequestStatus = "success"
    error_kind: str | None = None
    error_message: str | None = None
    headers: dict[str, str] | None = None
    # Which credential served this request: pool index plus a masked
    # ``first4…last4`` label. The raw key is never stored.
    key_index: int | None = None
    key_label: str | None = None
    # An assistant turn streams three kinds of block. ``output_text`` holds only
    # the model's prose; reasoning and tool calls are kept apart so the detail
    # view can show each for what it is, and so a tool-only turn (the common
    # case under Claude Code) still records what the model actually did.
    thinking_text: str | None = None
    thinking_chars: int | None = None
    # Images and documents the request carried. The count is a column so list
    # rows can show it; the pictures themselves live in their own tables.
    input_image_count: int | None = None
    # How the images this request carried reached the model: "image",
    # "stripped", "text" or "none". ``None`` means not measured.
    image_delivery: str | None = None
    # What the vision adapter's describe calls cost, summed over this
    # request's describe attempts. Shown beneath the answering model's tokens
    # as a separate "+ adapter" line and never added into ``tokens_in``, which
    # measures the model that answered and has always measured only that.
    adapter_tokens_in: int | None = None
    adapter_tokens_out: int | None = None
    # What the proxy's own estimator expected this request to cost, and how
    # much of that was pictures. The Models page reads them back as "billed vs
    # estimated"; a ratio near 1.0 means the host's declared image-token family
    # is right, and a host far from 1.0 has either the wrong family or an
    # undocumented formula.
    est_tokens_in: int | None = None
    est_image_tokens: int | None = None
    # Image payload before and after the outbound downscaler, in bytes. None
    # when nothing was resized.
    image_bytes_in: int | None = None
    image_bytes_out: int | None = None
    # What this request cost in USD and which rung of the pricing ladder said
    # so -- "provider", "models_dev", "litellm" or "cross_provider". Both None
    # when nothing priced it, which is a different fact from a cost of zero and
    # is stored, read and rendered as a different fact all the way out.
    cost_usd: float | None = None
    cost_source: str | None = None
    images: tuple[CapturedImage, ...] = ()
    attempts: tuple[RouteAttempt, ...] = ()
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_count: int | None = None
    # The local rule that answered this request inside the proxy, if any, and
    # the input tokens it kept off the wire. ``provider`` is NULL on such a row
    # because none was involved: attributing a request to a provider that never
    # saw it is what made these invisible in analytics for their whole life.
    optimization: str | None = None
    optimization_tokens_saved: int | None = None
    # Which coding agent sent this request, as ``client_fingerprint``
    # classified its headers. Left None only by a caller that never saw any
    # headers -- the API boundary always fills it, because that classifier
    # answers ``unknown`` rather than nothing when it recognises nothing.
    harness: str | None = None

    @property
    def ts_iso(self) -> str:
        return datetime.fromtimestamp(self.ts_epoch, tz=UTC).isoformat()


class RequestLogStore:
    """Durable per-request log drained by a single background writer thread."""

    def __init__(
        self,
        db_path: Path | str,
        *,
        max_rows: int = 50_000,
        text_max_chars: int = MAX_TEXT_CHARS,
        compression_level: int = _BODY_COMPRESSION_LEVEL,
        queue_max_size: int = _QUEUE_MAX_SIZE,
        compress_bodies: bool = True,
    ) -> None:
        self._db_path = Path(db_path)
        self._max_rows = max(0, max_rows)
        self._text_max_chars = max(0, text_max_chars)
        self._compression_level = compression_level
        self._queue_max_size = max(1, queue_max_size)
        self._compress_bodies = compress_bodies
        # Dictionaries are immutable once written, so caching them by id is
        # safe for the lifetime of the process.
        self._dict_cache: dict[int, Any] = {}
        self._dict_lock = threading.Lock()
        self._active_dict_id: int | None = None
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=self._queue_max_size)
        self._inserts_since_prune = 0
        # Session-level "stop asking": set when the historical cost backfill
        # finishes, when its marker is already there, or when it gave up. The
        # writer's idle branch runs every 0.25 s and must not pay a meta read
        # that often for the rest of the process's life.
        self._cost_backfill_done = False
        self._closed = threading.Event()
        self._stats_lock = threading.Lock()
        # OrderedDict as an LRU: ``move_to_end`` on every hit/insert keeps the
        # least recently used filter combination at the front for eviction.
        self._stats_cache: OrderedDict[
            tuple[Any, ...], tuple[float, dict[str, Any]]
        ] = OrderedDict()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._writer = threading.Thread(
            target=self._writer_loop,
            name="fcc-request-log-writer",
            daemon=True,
        )
        self._writer.start()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _mmap_size(self) -> int:
        """Bytes of this database to memory-map on a connection.

        ``min(size * headroom, cap)``, and zero when the file is not there yet
        or cannot be stat'ed -- zero is the pragma's own "do not map" value, so
        a missing file costs nothing and raises nothing.
        """
        try:
            size = self._db_path.stat().st_size
        except OSError:
            return 0
        if size <= 0:
            return 0
        return min(int(size * _MMAP_HEADROOM), _MMAP_MAX_BYTES)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        mmap_size = self._mmap_size()
        if mmap_size:
            # Not parameterisable: PRAGMA takes a literal. The value is an int
            # this method computed, never caller input.
            with contextlib.suppress(sqlite3.Error):
                conn.execute(f"PRAGMA mmap_size={mmap_size}")
        # Text search has to reach inside compressed bodies. Doing it in SQL
        # keeps the scan in SQLite instead of pulling every blob into Python
        # just to discard it, and costs no extra storage -- unlike an FTS index,
        # which would give back much of what the compression saves.
        conn.create_function(
            "fcc_body_matches", 3, self._body_matches, deterministic=True
        )
        conn.create_function(
            "fcc_bodies_match", 5, self._bodies_match, deterministic=True
        )
        return conn

    # --------------------------------------------------------- body storage ---

    def _dictionary(self, dict_id: int | None) -> Any:
        """Return the cached ``ZstdDict`` for ``dict_id``, loading it if needed.

        Blobs record which dictionary compressed them, so retraining later can
        never make an existing row unreadable.
        """
        if dict_id is None:
            return None
        cached = self._dict_cache.get(dict_id)
        if cached is not None:
            return cached
        with self._dict_lock:
            cached = self._dict_cache.get(dict_id)
            if cached is not None:
                return cached
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT content FROM body_dictionaries WHERE id = ?", (dict_id,)
                ).fetchone()
            if row is None:
                return None
            loaded = zstd.ZstdDict(bytes(row[0]))
            self._dict_cache[dict_id] = loaded
            return loaded

    def _decode_bodies(self, payload: Any, dict_id: Any) -> dict[str, Any]:
        if payload is None:
            return {}
        try:
            raw = zstd.decompress(bytes(payload), zstd_dict=self._dictionary(dict_id))
        except (zstd.ZstdError, ValueError) as exc:
            logger.warning("Request log body decompression failed: {}", exc)
            return {}
        return unpack_bodies(raw)

    def _bodies_match(
        self,
        rest_payload: Any,
        rest_dict: Any,
        input_payload: Any,
        input_dict: Any,
        needle: Any,
    ) -> int:
        """SQL predicate over a request's two blobs, considered together.

        They must be considered together: a search for "proxy 8082" can have
        one word in the prompt and the other in the reasoning, and requiring
        every word within a single blob would silently stop finding it.
        """
        if not needle:
            return 0
        terms = str(needle).split()
        if not terms:
            return 0
        raws = [
            raw
            for payload, dict_id in (
                (rest_payload, rest_dict),
                (input_payload, input_dict),
            )
            if payload is not None
            and (raw := self._raw_payload(payload, dict_id)) is not None
        ]
        if not raws:
            return 0
        probes = [term.encode("utf-8", "surrogatepass").lower() for term in terms]
        lowered = [raw.lower() for raw in raws]
        for term, probe in zip(terms, probes, strict=True):
            if _is_json_transparent(term) and not any(
                probe in candidate for candidate in lowered
            ):
                return 0
        merged: dict[str, Any] = {}
        for raw in raws:
            merged.update(unpack_bodies(raw))
        haystack = searchable_text(merged).encode("utf-8", "surrogatepass").lower()
        return int(all(probe in haystack for probe in probes))

    def _body_matches(self, payload: Any, dict_id: Any, needle: Any) -> int:
        """SQL predicate: does this request's stored content match ``needle``?

        Every term must appear somewhere in the request -- prompt, reply,
        reasoning or tool calls. Requiring all of them rather than the exact
        phrase is what makes a typed-out description find the request the
        reader had in mind; for a single word the two are identical.

        Called once per candidate row, so the slow path is the cost of search.
        Most rows match nothing, and for those the JSON parse and UTF-8 decode
        are pure waste -- hence the byte-level rejection first.
        """
        if payload is None or not needle:
            return 0
        terms = str(needle).split()
        if not terms:
            return 0
        raw = self._raw_payload(payload, dict_id)
        if raw is None:
            return 0
        # Case folding in bytes rather than text: 7x cheaper on a 43 KB body,
        # and it matches SQLite's own LIKE, which is case-insensitive for ASCII
        # only. Folding in Python text would make compressed rows match things
        # the inline rows beside them do not.
        probes = [term.encode("utf-8", "surrogatepass").lower() for term in terms]
        lowered_raw = raw.lower()
        # JSON escaping only ever rewrites quotes, backslashes and control
        # characters, so a term containing none of them survives into the blob
        # byte for byte: absent from the encoded bytes proves absent from the
        # text. The converse does not hold -- it can match structure -- so a
        # survivor is still verified against the decoded content below.
        for term, probe in zip(terms, probes, strict=True):
            if _is_json_transparent(term) and probe not in lowered_raw:
                return 0
        haystack = (
            searchable_text(unpack_bodies(raw)).encode("utf-8", "surrogatepass").lower()
        )
        return int(all(probe in haystack for probe in probes))

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a connection that is always closed.

        ``sqlite3.Connection.__exit__`` only commits or rolls back; it never
        closes. Connections are garbage-collected rather than reference-counted,
        so relying on scope exit leaks file descriptors until the next GC pass.
        """
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            # A fresh database can adopt incremental auto-vacuum for free, but
            # only if the pragma is applied outside a transaction and before the
            # first table exists. Converting a populated database needs a full
            # VACUUM, which the writer thread performs in the background
            # instead (see ``_writer_loop``).
            previous = conn.isolation_level
            conn.isolation_level = None
            try:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            finally:
                conn.isolation_level = previous
            with conn:
                conn.executescript(_SCHEMA)
                conn.executescript(_TOTALS_SCHEMA)
                # Deliberately not part of ``_SCHEMA``: that script runs before
                # the ALTER TABLE migration, and these tables are independent of
                # ``requests`` anyway.
                #
                # The drop has to sit between the two scripts. It needs
                # ``request_log_meta``, which ``_TOTALS_SCHEMA`` above creates;
                # and it has to precede the CREATEs, because
                # ``CREATE TABLE IF NOT EXISTS`` would find the stale tables and
                # leave them, while dropping afterwards would delete the correct
                # new ones and leave nothing behind.
                self._drop_superseded_rollup_tables(conn)
                conn.executescript(_ROLLUP_SCHEMA)
                conn.executescript(_BODIES_SCHEMA)
                self._ensure_added_columns(conn)
                self._ensure_input_sha_column(conn)
                self._ensure_attempt_columns(conn)
                self._ensure_image_blob_columns(conn)
                self._ensure_session_columns(conn)
                self._ensure_rollup_counter_columns(conn)
                # After the ALTERs: the index does not reference the new
                # columns, but the table must exist before it is created.
                self._ensure_attempt_index(conn)
                self._relax_bodies_sha_constraint(conn)
                self._ensure_bodies_index(conn)
        finally:
            conn.close()

    @staticmethod
    def _drop_superseded_rollup_tables(conn: sqlite3.Connection) -> None:
        """Discard rollup tables keyed on fewer dimensions than we now store.

        The three ``request_stats_*`` tables are ``WITHOUT ROWID`` with the
        whole dimension tuple as their PRIMARY KEY, and
        ``CREATE TABLE IF NOT EXISTS`` never revises an existing definition. A
        database written before ``harness`` became a dimension would therefore
        keep its nine-column tables, and every later upsert would fold two
        different harnesses into one bucket -- silently, and forever.

        There is nothing to migrate in place: the missing dimension was never
        recorded, so those rows cannot be split apart again. Dropping them and
        letting ``_ensure_rollup_backfill`` rebuild is the only correct move,
        and it is safe because ``requests`` still holds every fact the rollup
        summarises.
        """
        columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(request_stats_rollup)")
        }
        # An empty result is a database that has no rollup yet -- a fresh file,
        # or one created before these tables existed. Nothing to drop.
        if not columns or "harness" in columns:
            return
        for table in _ROLLUP_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        # The v2 keys cannot have been written by a release that lacked the
        # dimension, but clearing them keeps this correct if the dimension list
        # ever grows again under the same names.
        for key in (
            *_SUPERSEDED_ROLLUP_KEYS,
            _ROLLUP_BACKFILL_KEY,
            _ROLLUP_BACKFILL_THROUGH_KEY,
        ):
            conn.execute("DELETE FROM request_log_meta WHERE key = ?", (key,))

    @staticmethod
    def _ensure_added_columns(conn: sqlite3.Connection) -> None:
        """Add post-release columns to a database created by an older version."""
        existing = {str(row[1]) for row in conn.execute("PRAGMA table_info(requests)")}
        for column, alter_sql in _ADDED_COLUMNS:
            if column in existing:
                continue
            try:
                conn.execute(alter_sql)
            except sqlite3.OperationalError:
                # Another process may have won the migration race; only a
                # genuinely missing column is an error.
                columns = {
                    str(row[1]) for row in conn.execute("PRAGMA table_info(requests)")
                }
                if column not in columns:
                    raise
        for index_sql in _ADDED_INDEXES:
            conn.execute(index_sql)

    @staticmethod
    def _ensure_attempt_index(conn: sqlite3.Connection) -> None:
        """Covering index for the per-model reasoning query.

        ``reasoning_by_model`` groups succeeded attempts by model and reads
        only ``reasoning_emitted`` and ``request_id`` off each one; without
        this the scan walks every attempt row, and an attempt row co-locates
        its stored wire body. Versioned name per the index rule: changing the
        column list means ``_v2`` plus an explicit drop of ``_v1``.
        """

        with contextlib.suppress(sqlite3.Error):
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_attempts_model_v1"
                " ON request_attempts(model_ref, outcome, reasoning_emitted,"
                " request_id)"
            )

    @staticmethod
    def _ensure_stats_index(conn: sqlite3.Connection) -> None:
        """Add a covering index for the aggregate queries.

        SQLite stores every column of a row together, so a scan over the
        numeric columns ``stats`` needs still walks the overflow pages holding
        up to 100k characters of request/response text per row. An index that
        carries those columns lets the aggregates run index-only and skip the
        bodies entirely.
        """
        with contextlib.suppress(sqlite3.Error):
            # Versioned name: ``CREATE INDEX IF NOT EXISTS`` would silently keep
            # an older index built before ``key_label`` joined the column list,
            # leaving the per-key aggregate without index-only coverage.
            conn.execute("DROP INDEX IF EXISTS idx_requests_stats")
            conn.execute("DROP INDEX IF EXISTS idx_requests_stats_v2")
            conn.execute("DROP INDEX IF EXISTS idx_requests_stats_v3")
            # ``is_local`` leads so ``local=hide``/``only`` is an equality seek
            # rather than a predicate that abandons the index; ``optimization``
            # joins the column list so the ``local:<rule>`` and ``(unknown)``
            # provider predicates stay index-only.
            #
            # Deliberately NOT widened with the route columns. The docstring on
            # ``_percentiles`` records that an index leading on ``duration_ms``
            # made ``stats()`` 2.2x slower by confusing a planner with no
            # ``ANALYZE``, and every added column is another chance of that.
            # The measured cost is one query: ``fallback_routes`` on raw rows
            # went 867 -> 1047 ms because ``route_primary_model`` is uncovered,
            # and that list is served from the rollup now.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_requests_stats_v4 ON requests("
                " is_local, ts_epoch, status, provider, resolved_model, endpoint,"
                " requested_model, key_label, duration_ms, ttft_ms,"
                " tokens_in, tokens_out, cache_read_tokens, cache_write_tokens,"
                " optimization)"
            )

    @staticmethod
    def _ensure_partial_indexes(conn: sqlite3.Connection) -> None:
        """Index only the rows two dashboard panels ever look at.

        Both of these panels filter on a column that is NULL on the
        overwhelming majority of the log -- ``est_image_tokens`` on 2.7% of
        rows, ``optimization`` on 2.2% -- and SQLite, which has no statistics
        here, answered them by walking an equality index over
        ``status = 'success'`` that matches 99.2% of the table. A partial index
        holds only the rows that satisfy its own WHERE, so these cost a few
        hundred kilobytes against a multi-gigabyte log and are touched on an
        insert only when that insert would appear in them.

        Measured on a 4.5 GB, 333,838-row copy of a real log, best of three:

        - image estimate per host, 7 days: 1.051 s -> **0.002 s**, 504 KB of
          index (and it needs the ``INDEXED BY`` hint on
          ``_image_estimate_sql`` to be chosen at all).
        - optimization rules: 0.951 s -> **0.003 s**; its daily series
          0.976 s -> **0.015 s**; 303 KB of index, chosen with no hint.
        - insert cost of both together, 2,000 real rows re-inserted inside a
          rolled-back transaction, three alternating cycles: **below the noise
          floor** -- 44.6 us/row without them, 36.7 us/row with them, i.e. the
          difference is smaller than the spread between cycles. A partial
          index over 2-3% of rows is only walked on 2-3% of inserts.

        A third candidate, ``(status, ts_epoch) WHERE status <> 'success'``,
        was measured and **left out**: the query it would serve already answers
        in 0.000-0.016 s from the covering ``idx_requests_status``, so it would
        have been an index with no measurement behind it.

        Versioned names, per the index rule: changing a column list means
        ``_v2`` and an explicit drop of ``_v1`` in the same migration.
        ``ANALYZE`` is deliberately not run -- measured on the same copy it
        took 2.2 s and made the image-estimate plan *worse*.
        """
        for statement in _PARTIAL_INDEXES:
            with contextlib.suppress(sqlite3.Error):
                conn.execute(statement)

    @staticmethod
    def _ensure_auto_vacuum(conn: sqlite3.Connection) -> None:
        """Enable incremental auto-vacuum so pruned pages can be reclaimed.

        Without this the database file only ever grows: ``prune`` frees pages
        onto the internal freelist but never returns them to the filesystem.

        Converting an existing database requires a full VACUUM, which on a
        multi-hundred-megabyte file takes many seconds. This must therefore run
        on the writer thread, never on a request path.
        """
        try:
            mode = conn.execute("PRAGMA auto_vacuum").fetchone()[0]
            if int(mode) == 2:
                return
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            previous = conn.isolation_level
            conn.isolation_level = None
            try:
                started = time.monotonic()
                conn.execute("VACUUM")
                logger.info(
                    "Request log converted to incremental auto-vacuum in {:.1f}s",
                    time.monotonic() - started,
                )
            finally:
                conn.isolation_level = previous
        except sqlite3.Error as exc:
            logger.warning("Request log auto_vacuum setup failed: {}", exc)

    @staticmethod
    def _ensure_totals_backfill(conn: sqlite3.Connection) -> None:
        """Seed the permanent counters from rows already in the table.

        Runs once, before the writer accepts its first flush, so no request can
        be folded in twice -- by the rollup and again by this aggregate. On an
        upgrade this recovers whatever history retention has not yet eaten;
        anything already pruned is gone and cannot be recovered.
        """
        marker = conn.execute(
            "SELECT value FROM request_log_meta WHERE key = ?",
            (_TOTALS_BACKFILL_KEY,),
        ).fetchone()
        if marker is not None:
            return
        started = time.monotonic()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO request_totals"
                    f" (day, provider, model, {', '.join(_TOTALS_COUNTERS)})"
                    " SELECT strftime('%Y-%m-%d', ts_epoch, 'unixepoch'),"
                    " COALESCE(provider, ''), COALESCE(resolved_model, ''),"
                    " COUNT(*),"
                    " SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END),"
                    " SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END),"
                    " SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END),"
                    " COALESCE(SUM(tokens_in), 0), COALESCE(SUM(tokens_out), 0),"
                    " COALESCE(SUM(cache_read_tokens), 0),"
                    " COALESCE(SUM(cache_write_tokens), 0),"
                    " COALESCE(SUM(tool_call_count), 0),"
                    " SUM(CASE WHEN route_attempt > 0 THEN 1 ELSE 0 END),"
                    " SUM(CASE WHEN route_diversion IS NOT NULL THEN 1 ELSE 0 END)"
                    " FROM requests GROUP BY 1, 2, 3"
                )
                conn.execute(
                    "INSERT OR REPLACE INTO request_log_meta (key, value)"
                    " VALUES (?, ?)",
                    (_TOTALS_BACKFILL_KEY, str(time.time())),
                )
        except sqlite3.Error as exc:
            # A concurrent store on the same file may have won the race and
            # already written these buckets; the transaction rolled back whole,
            # so the next start simply finds the marker and skips.
            logger.warning("Request log totals backfill skipped: {}", exc)
            return
        logger.info(
            "Request log lifetime totals seeded from existing rows in {:.1f}s",
            time.monotonic() - started,
        )

    @staticmethod
    def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
        """Read one ``request_log_meta`` value, or None if it was never set."""
        row = conn.execute(
            "SELECT value FROM request_log_meta WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row[0])

    @staticmethod
    def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            "INSERT OR REPLACE INTO request_log_meta (key, value) VALUES (?, ?)",
            (key, value),
        )

    @classmethod
    def _ensure_is_local_backfill(cls, conn: sqlite3.Connection) -> None:
        """Compute the stored ``is_local`` column for rows written before it.

        Chunked and committed per chunk: a single UPDATE over every local
        answer on a large log would hold one long write transaction and push
        the whole change into the WAL before any checkpoint could run.

        ``DEFAULT 0`` means an un-backfilled row reads as upstream traffic, so
        ``local=hide`` shows a few rows it should hide until this finishes.
        That window is sub-second on the measured log (3 561 rows in 0.7 s) but
        it scales with the local-answer count, which is why this runs before
        the rollup backfill rather than beside it.
        """
        if cls._meta_get(conn, _IS_LOCAL_BACKFILL_KEY) is not None:
            return
        started = time.monotonic()
        updated = 0
        try:
            while True:
                with conn:
                    cursor = conn.execute(
                        "UPDATE requests SET is_local = 1 WHERE id IN ("
                        " SELECT id FROM requests WHERE is_local = 0"
                        f" AND {LOCAL_ANSWER_SQL} LIMIT ?)",
                        (_IS_LOCAL_CHUNK_ROWS,),
                    )
                    changed = cursor.rowcount
                if changed <= 0:
                    break
                updated += changed
            with conn:
                cls._meta_set(conn, _IS_LOCAL_BACKFILL_KEY, str(time.time()))
        except sqlite3.Error as exc:
            # A concurrent store on the same file may have won the race. Its
            # marker means "already done", not "corrupt"; the next start finds
            # the marker and skips.
            logger.warning("Request log is_local backfill skipped: {}", exc)
            return
        if updated:
            logger.info(
                "Request log marked {} locally answered rows in {:.1f}s",
                updated,
                time.monotonic() - started,
            )

    @classmethod
    def _ensure_harness_backfill(cls, conn: sqlite3.Connection) -> None:
        """Attribute rows written before the ``harness`` column existed.

        Classified in Python, not in SQL. The rule is a table of user-agent
        patterns owned by ``client_fingerprint``; re-expressing it as a
        ``CASE WHEN`` ladder would be a second classifier, drifting from the
        first the day Claude Code changes the shape of its user-agent. The
        stored ``headers`` dict is exactly what ``harness_from_headers``
        accepts, which is what lets the history and the live path share one
        implementation instead of agreeing by inspection.

        Chunked and committed per chunk, for the reason
        ``_ensure_is_local_backfill`` gives: a single UPDATE over the whole
        table would hold one long write transaction and push every changed page
        into the WAL before a checkpoint could run.

        Resumability needs no cursor of its own, unlike the rollup's stored
        hour marker. ``harness IS NULL`` *is* the progress: a committed chunk
        stops matching the predicate, so a restart resumes exactly where the
        last commit left off and can never redo work. The rollup needs a marker
        only because it writes to a different table and cannot see its own
        progress in the rows it is reading.
        """
        if cls._meta_get(conn, _HARNESS_BACKFILL_KEY) is not None:
            return
        started = time.monotonic()
        updated = 0
        try:
            while True:
                rows = conn.execute(
                    "SELECT id, headers FROM requests WHERE harness IS NULL LIMIT ?",
                    (_HARNESS_CHUNK_ROWS,),
                ).fetchall()
                if not rows:
                    break
                updates = [
                    (
                        harness_from_headers(_stored_headers(row[1])).harness,
                        str(row[0]),
                    )
                    for row in rows
                ]
                with conn:
                    conn.executemany(
                        "UPDATE requests SET harness = ? WHERE id = ?", updates
                    )
                updated += len(updates)
            with conn:
                cls._meta_set(conn, _HARNESS_BACKFILL_KEY, str(time.time()))
        except sqlite3.Error as exc:
            # Same rule as the siblings: a concurrent store on the same file may
            # have won the race, and its marker means "already done", not
            # "corrupt". Committed chunks stay; the next start resumes.
            logger.warning("Request log harness backfill skipped: {}", exc)
            return
        if updated:
            logger.info(
                "Request log attributed {} rows to a harness in {:.1f}s",
                updated,
                time.monotonic() - started,
            )

    def _ensure_cost_backfill(self, conn: sqlite3.Connection) -> None:
        """Price the rows that were logged before anything priced anything.

        ``cost_usd``/``cost_source`` arrived in 6.54.0 and were deliberately
        not backfilled: "pricing 275,000 old requests at today's rates would
        produce a confident number that was never anybody's bill". That is
        right about confidence and wrong about availability -- the answer is
        not to refuse the number but to **label** it, which is what the
        retroactive ``cost_source`` values exist for. A backfilled price is an
        estimate, it says so in the column, and ``cost_breakdown``'s
        ``reported_usd`` keeps matching only the rows a host really reported.

        Three properties, each bought deliberately:

        - **Resumable with no cursor of its own.** ``cost_usd IS NULL AND
          cost_source IS NULL`` *is* the progress, exactly as ``harness IS
          NULL`` is for :meth:`_ensure_harness_backfill`: a committed row stops
          matching, so a kill mid-walk costs only the uncommitted chunk. The
          stored day is a scan-saver, not the mechanism.
        - **A row that cannot be priced stops matching too.** It is stored as
          ``cost_source = 'unpriced'`` with ``cost_usd`` still NULL -- the
          150,000-odd rows naming a model nobody publishes a rate for are
          answered once rather than re-asked on every start for the life of the
          log. NULL survives, because NULL is still the honest cost, and
          ``cost_usd`` remains the only thing anything sums.
        - **It never holds the writer.** One committed chunk at a time, and it
          hands the thread back the moment a request is queued behind it.

        Runs on the writer thread's idle branch, never on a request path and
        never before the first flush: it is a migration over hundreds of
        thousands of rows, and the file already says where those belong.
        """
        if self._cost_backfill_done:
            return
        pricer = _cost_backfill_pricer
        if pricer is None:
            # No catalogue, no backfill. Deliberately re-checked rather than
            # latched: the composition root registers the pricer once the
            # models.dev cache is on disk, which can be after this store opened.
            return
        try:
            if self._meta_get(conn, _COST_BACKFILL_KEY) is not None:
                self._cost_backfill_done = True
                return
            bounds = conn.execute(
                "SELECT MIN(ts_epoch), MAX(ts_epoch) FROM requests"
            ).fetchone()
        except sqlite3.Error as exc:
            logger.warning("Request log cost backfill skipped: {}", exc)
            self._cost_backfill_done = True
            return
        if bounds is None or bounds[0] is None or bounds[1] is None:
            with conn:
                self._meta_set(conn, _COST_BACKFILL_KEY, str(time.time()))
            self._cost_backfill_done = True
            return
        day = _utc_day(float(bounds[0]))
        last = _utc_day(float(bounds[1]))
        resumed = self._meta_get(conn, _COST_BACKFILL_THROUGH_KEY)
        if resumed:
            day = max(day, _next_utc_day(resumed) or day)
        started = time.monotonic()
        priced = 0
        unpriced = 0
        try:
            while day <= last:
                chunk_priced, chunk_unpriced, status = self._backfill_cost_day(
                    conn, day, pricer
                )
                priced += chunk_priced
                unpriced += chunk_unpriced
                if status == "stalled":
                    # Every row in a chunk refused by the pricer. Nothing here
                    # can make progress, and retrying every 0.25 s would be a
                    # spin; the next start tries again with a fresh pricer.
                    logger.warning(
                        "Request log cost backfill paused at {}: nothing priceable",
                        day,
                    )
                    self._cost_backfill_done = True
                    return
                if status == "yield":
                    self._log_cost_backfill(priced, unpriced, started, done=False)
                    return
                with conn:
                    self._meta_set(conn, _COST_BACKFILL_THROUGH_KEY, day)
                following = _next_utc_day(day)
                if following is None:
                    break
                day = following
                if day <= last and not self._queue.empty():
                    self._log_cost_backfill(priced, unpriced, started, done=False)
                    return
            with conn:
                self._meta_set(conn, _COST_BACKFILL_KEY, str(time.time()))
        except sqlite3.Error as exc:
            # Same rule as its siblings: a concurrent store on the same file may
            # have won the race, and its marker means "already done", not
            # "corrupt". Committed chunks stay; the next start resumes.
            logger.warning("Request log cost backfill skipped: {}", exc)
            self._cost_backfill_done = True
            return
        self._cost_backfill_done = True
        self._log_cost_backfill(priced, unpriced, started, done=True)

    def _backfill_cost_day(
        self,
        conn: sqlite3.Connection,
        day: str,
        pricer: CostBackfillPricer,
    ) -> tuple[int, int, str]:
        """Price one UTC day, committing per chunk.

        Returns ``(priced, unpriced, status)`` where status is ``"done"`` when
        the day has no unanswered rows left, ``"yield"`` when a request arrived
        and the writer is owed its thread back, and ``"stalled"`` when a whole
        chunk came back with no answer at all -- which only the loss of the
        pricer's catalogue between two chunks can produce, and which must stop
        the walk rather than spin it.

        The day bounds are what makes this cheap: they are a range on
        ``ts_epoch``, so each chunk is an index seek rather than the full scan
        ``cost_usd IS NULL`` on its own would have to be.
        """
        start, end = _utc_day_bounds(day)
        priced = 0
        unpriced = 0
        while True:
            rows = conn.execute(
                "SELECT id, provider, resolved_model, tokens_in, tokens_out,"
                " cache_read_tokens, cache_write_tokens FROM requests"
                " WHERE ts_epoch >= ? AND ts_epoch < ?"
                " AND cost_usd IS NULL AND cost_source IS NULL LIMIT ?",
                (start, end, _COST_CHUNK_ROWS),
            ).fetchall()
            if not rows:
                return (priced, unpriced, "done")
            updates: list[tuple[float | None, str, str]] = []
            for row in rows:
                cost, source = pricer(row[1], row[2], row[3], row[4], row[5], row[6])
                if source is None:
                    # The pricer declined to answer at all. Left untouched, so
                    # the row is asked about again rather than being recorded
                    # as unpriceable on the strength of a missing catalogue.
                    continue
                if cost is not None and not cost > 0.0:
                    # The never-zero rule, at the last gate before storage. A
                    # zero here would read as "this request was free", a claim
                    # only a source that publishes a zero may make, and a
                    # backfill is in no position to make it about a request
                    # from six weeks ago. Measured on the 333,838-row log this
                    # was written against: no row took this branch.
                    cost, source = None, _UNPRICED_COST_SOURCE
                updates.append((cost, source, str(row[0])))
                if cost is None:
                    unpriced += 1
                else:
                    priced += 1
            if not updates:
                return (priced, unpriced, "stalled")
            with conn:
                conn.executemany(
                    "UPDATE requests SET cost_usd = ?, cost_source = ?"
                    " WHERE id = ? AND cost_usd IS NULL AND cost_source IS NULL",
                    updates,
                )
            if not self._queue.empty():
                return (priced, unpriced, "yield")

    @staticmethod
    def _log_cost_backfill(
        priced: int, unpriced: int, started: float, *, done: bool
    ) -> None:
        """Say what the walk did, once per pass, and only when it did something."""
        if not priced and not unpriced:
            return
        logger.info(
            "Request log priced {} older requests ({} have no published rate)"
            " in {:.1f}s{}",
            priced,
            unpriced,
            time.monotonic() - started,
            "" if done else ", continuing",
        )

    @staticmethod
    def _has_ln_function(conn: sqlite3.Connection) -> bool:
        """Whether this SQLite was built with ``SQLITE_ENABLE_MATH_FUNCTIONS``.

        Without ``LN`` the histogram cannot be bucketed in SQL. Falling back to
        a Python pass is slower; silently mis-bucketing is not an option, and
        using the built-in ``LOG`` instead would do exactly that -- it is base
        10, and against a natural-log step it produces 94-99% percentile error.
        """
        try:
            conn.execute("SELECT LN(2.0)").fetchone()
        except sqlite3.Error:
            return False
        return True

    @classmethod
    def _backfill_rollup_chunk(
        cls, conn: sqlite3.Connection, low: int, high: int, *, has_ln: bool
    ) -> None:
        """Fold ``[low, high)`` seconds of ``requests`` into the three tables.

        Called inside one transaction per chunk. Every statement is a plain
        INSERT except the attempt passes, which must add onto the bucket the
        ``requests`` pass just created; resumability comes from the stored
        marker alone, so a chunk is either wholly committed or wholly absent.
        """
        dims = _rollup_dimension_select()
        joined = ", ".join(dims)
        group = ", ".join(str(index) for index in range(1, len(dims) + 1))
        window = "ts_epoch >= ? AND ts_epoch < ?"
        bounds = (low, high)

        conn.execute(
            f"INSERT INTO request_stats_rollup ({', '.join(_ROLLUP_DIMENSIONS)},"
            f" {', '.join(_ROLLUP_COUNTER_NAMES)})"
            f" SELECT {joined},"
            f" {', '.join(sql for _name, _ddl, sql in _ROLLUP_COUNTERS)}"
            f" FROM requests WHERE {window} GROUP BY {group}",
            bounds,
        )

        # Recovery counters live on ``request_attempts.params``. The comma join
        # (rather than JOIN ... ON) keeps the upsert clause unambiguous to the
        # parser, and the WHERE window keeps the id list to one chunk instead
        # of the quarter-million-row LIST SUBQUERY the live query built.
        attempt_dims = ", ".join(_rollup_dimension_select("r."))
        recovery_columns = (*_ROLLUP_DIMENSIONS, *_ROLLUP_RECOVERY_COUNTERS)
        conn.execute(
            f"INSERT INTO request_stats_rollup ({', '.join(recovery_columns)})"
            f" SELECT {attempt_dims},"
            + ", ".join(
                f"COALESCE(SUM(json_extract(a.params, '$.{name}')), 0)"
                for name in _ROLLUP_RECOVERY_COUNTERS
            )
            + " FROM request_attempts AS a, requests AS r"
            f" WHERE r.id = a.request_id AND r.{window}"
            f" GROUP BY {group}"
            f" ON CONFLICT({', '.join(_ROLLUP_DIMENSIONS)}) DO UPDATE SET "
            + ", ".join(
                f"{name} = {name} + excluded.{name}"
                for name in _ROLLUP_RECOVERY_COUNTERS
            ),
            bounds,
        )

        if has_ln:
            conn.execute(
                f"INSERT INTO request_stats_latency"
                f" ({', '.join(_ROLLUP_DIMENSIONS)}, bucket, count)"
                f" SELECT {joined}, {_LATENCY_BUCKET_SQL}, COUNT(*)"
                f" FROM requests WHERE {window} AND duration_ms IS NOT NULL"
                f" GROUP BY {group}, {len(dims) + 1}",
                bounds,
            )
        else:
            cls._backfill_latency_chunk_in_python(conn, dims, window, bounds)

        detail_columns = (
            f"{', '.join(_DETAIL_DIMENSIONS)}, kind, a, b, c, count, requests"
        )
        detail_group = f"{group}, {len(dims) + 2}, {len(dims) + 3}, {len(dims) + 4}"
        for kind, a_sql, b_sql, c_sql, predicate in (
            (
                _DETAIL_ERROR,
                "error_message",
                "''",
                "''",
                "status = 'error' AND error_message IS NOT NULL",
            ),
            (
                _DETAIL_FALLBACK,
                "route_primary_model",
                SERVED_BY_KEY_SQL,
                "''",
                "route_attempt > 0 AND route_primary_model IS NOT NULL",
            ),
            (
                _DETAIL_DIVERTED,
                "route_diverted_from",
                "route_diversion",
                SERVED_BY_KEY_SQL,
                "route_diversion IS NOT NULL AND route_diverted_from IS NOT NULL",
            ),
        ):
            conn.execute(
                f"INSERT INTO request_stats_detail ({detail_columns})"
                f" SELECT {joined}, '{kind}', {a_sql}, {b_sql}, {c_sql},"
                " COUNT(*), COUNT(*)"
                f" FROM requests WHERE {window} AND {predicate}"
                f" GROUP BY {detail_group}",
                bounds,
            )

        # ``requests`` here is a distinct-request count, and it stays exact
        # under SUM without a DISTINCT: a request lives in exactly one
        # dimension bucket, so it contributes 1 per distinct upstream status it
        # saw, and summing that over buckets reproduces
        # ``COUNT(DISTINCT request_id)`` grouped by status. This is the only
        # non-obvious additivity claim in the design.
        conn.execute(
            f"INSERT INTO request_stats_detail ({detail_columns})"
            f" SELECT {attempt_dims}, '{_DETAIL_UPSTREAM}',"
            " CAST(json_extract(t.value, '$.status') AS TEXT), '', '',"
            " COUNT(*), COUNT(DISTINCT a.request_id)"
            " FROM request_attempts AS a,"
            " json_each(json_extract(a.params, '$.ladder.tries')) AS t,"
            " requests AS r"
            f" WHERE r.id = a.request_id AND r.{window}"
            " AND a.ladder_tries > 1"
            " AND json_extract(t.value, '$.status') IS NOT NULL"
            f" GROUP BY {detail_group}",
            bounds,
        )

    @staticmethod
    def _backfill_latency_chunk_in_python(
        conn: sqlite3.Connection,
        dims: tuple[str, ...],
        window: str,
        bounds: tuple[int, int],
    ) -> None:
        """Bucket one chunk's durations without SQL math functions."""
        counts: dict[tuple[Any, ...], int] = {}
        for row in conn.execute(
            f"SELECT {', '.join(dims)}, duration_ms FROM requests"
            f" WHERE {window} AND duration_ms IS NOT NULL",
            bounds,
        ):
            key = (*row[:-1], _latency_bucket(float(row[-1])))
            counts[key] = counts.get(key, 0) + 1
        if counts:
            conn.executemany(
                f"INSERT INTO request_stats_latency"
                f" ({', '.join(_ROLLUP_DIMENSIONS)}, bucket, count)"
                f" VALUES ({', '.join('?' * (len(_ROLLUP_DIMENSIONS) + 2))})",
                [(*key, count) for key, count in counts.items()],
            )

    @classmethod
    def _ensure_rollup_backfill(cls, conn: sqlite3.Connection) -> None:
        """Seed the stats rollup from rows already in the table.

        Runs on the writer thread before the first flush, so no request is
        counted twice -- once here and again by ``_accumulate_rollup``. Walks
        UTC hours in ascending order, commits every ``_ROLLUP_CHUNK_HOURS``,
        and records the hour it reached in ``request_log_meta``. A restart
        resumes there, which is the whole idempotence mechanism: a chunk is
        either fully committed or not written, and the marker only ever
        advances past a committed chunk.

        ``stats()`` serves from raw rows until the completion marker lands, so
        a partially built rollup is never read.
        """
        if cls._meta_get(conn, _ROLLUP_BACKFILL_KEY) is not None:
            return
        started = time.monotonic()
        try:
            bounds = conn.execute(
                "SELECT MIN(ts_epoch), MAX(ts_epoch) FROM requests"
            ).fetchone()
            if bounds is None or bounds[0] is None:
                with conn:
                    cls._meta_set(conn, _ROLLUP_BACKFILL_KEY, str(time.time()))
                return
            first = _floor_hour(float(bounds[0]))
            end = _floor_hour(float(bounds[1])) + _HOUR_SECONDS
            resumed = cls._meta_get(conn, _ROLLUP_BACKFILL_THROUGH_KEY)
            cursor_hour = max(first, int(resumed)) if resumed else first
            has_ln = cls._has_ln_function(conn)
            if not has_ln:
                logger.warning(
                    "SQLite has no LN(); bucketing request latencies in Python"
                )
            chunk = _ROLLUP_CHUNK_HOURS * _HOUR_SECONDS
            while cursor_hour < end:
                chunk_end = min(cursor_hour + chunk, end)
                with conn:
                    cls._backfill_rollup_chunk(
                        conn, cursor_hour, chunk_end, has_ln=has_ln
                    )
                    cls._meta_set(conn, _ROLLUP_BACKFILL_THROUGH_KEY, str(chunk_end))
                cursor_hour = chunk_end
            with conn:
                cls._meta_set(conn, _ROLLUP_BACKFILL_KEY, str(time.time()))
        except sqlite3.Error as exc:
            # Same rule as the totals backfill: a second process's marker means
            # "already done", not "corrupt". Whatever chunks committed stay,
            # and the next start resumes from the recorded hour.
            logger.warning("Request log stats rollup backfill skipped: {}", exc)
            return
        logger.info(
            "Request log stats rollup seeded from existing rows in {:.1f}s",
            time.monotonic() - started,
        )

    @staticmethod
    def _ensure_input_sha_column(conn: sqlite3.Connection) -> None:
        """Add the prompt reference to a table created before the split.

        Rows keep ``input_sha`` NULL and their existing blob keeps carrying the
        prompt inside it, which reads correctly without any rewrite;
        ``fcc-compact-log`` splits them when it runs.
        """
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(request_bodies)")
        }
        if "sha" in columns and "input_sha" not in columns:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ALTER TABLE request_bodies ADD COLUMN input_sha TEXT")

    @staticmethod
    def _ensure_attempt_columns(conn: sqlite3.Connection) -> None:
        """Add per-attempt columns to a table created before they existed.

        ``CREATE TABLE IF NOT EXISTS`` is a no-op on an existing database, so
        every column added after the table shipped needs its own guarded
        ``ALTER TABLE``. Rows written earlier keep the column NULL, which reads
        downstream as "not measured" -- distinct from zero and from false:

        * ``params`` -- the transparent stream recovery that happened while
          this model held the request, plus the resolved wire parameters.
        * ``wire_body`` -- the redacted, text-free outbound body.
        * ``reasoning_emitted`` -- whether that body carried reasoning.
        * ``key_index``/``key_label`` -- the credential this attempt used,
          captured at the attempt boundary instead of at the end of the
          request. Index -1 with the sentinel label means the pool was
          fully benched and the attempt never reached a key.
        * ``ladder_tries`` -- how many upstream tries hid behind this one row.
        """
        for column, ddl in _ATTEMPT_ADDED_COLUMNS:
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(request_attempts)")
            }
            if column in columns:
                continue
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                # Another process may have won the migration race; only a
                # genuinely missing column is an error.
                columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(request_attempts)")
                }
                if column not in columns:
                    raise

    @staticmethod
    def _ensure_image_blob_columns(conn: sqlite3.Connection) -> None:
        """Add the description columns to a picture table created before them.

        A row written before 6.51.0 keeps ``description`` NULL, which reads as
        "nobody has described this picture" -- the same thing a fresh row says,
        and exactly what the describe cache should conclude.
        """
        for column, ddl in _IMAGE_BLOB_ADDED_COLUMNS:
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(image_blobs)")
            }
            if column in columns:
                continue
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(image_blobs)")
                }
                if column not in columns:
                    raise

    @staticmethod
    def _ensure_rollup_counter_columns(conn: sqlite3.Connection) -> None:
        """Add a rollup counter declared after the table shipped.

        A new *dimension* cannot be added this way -- it would fold two
        different buckets into one, which is why
        ``_drop_superseded_rollup_tables`` rebuilds instead. A counter is
        additive and independent, so an ``ALTER`` with ``DEFAULT 0`` is
        correct: hours rolled up before the counter existed report zero for
        it, which is the truth, because the thing it counts had not shipped.
        """
        existing = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(request_stats_rollup)")
        }
        # An empty result is a database whose rollup table was just created by
        # the schema script above, with every declared column already on it.
        if not existing:
            return
        for name, ddl, _sql in _ROLLUP_COUNTERS:
            if name in existing:
                continue
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute(
                    "ALTER TABLE request_stats_rollup ADD COLUMN"
                    f" {name} {ddl} NOT NULL DEFAULT 0"
                )

    @staticmethod
    def _relax_bodies_sha_constraint(conn: sqlite3.Connection) -> None:
        """Allow a request to have a prompt but no reply blob.

        The column was declared NOT NULL before the two were separated, when
        every request had exactly one blob. ``CREATE TABLE IF NOT EXISTS`` will
        not revise that, and SQLite cannot drop a column constraint in place,
        so the table is rebuilt -- three short columns, cheap even at 500,000
        rows.
        """
        info = {
            str(row[1]): row
            for row in conn.execute("PRAGMA table_info(request_bodies)")
        }
        sha = info.get("sha")
        if sha is None or not sha[3] or "input_sha" not in info:
            return
        conn.execute("ALTER TABLE request_bodies RENAME TO request_bodies_old")
        conn.executescript(_BODIES_SCHEMA)
        conn.execute(
            "INSERT INTO request_bodies (request_id, sha, input_sha)"
            " SELECT request_id, sha, input_sha FROM request_bodies_old"
        )
        conn.execute("DROP TABLE request_bodies_old")
        logger.info("Request log body table rebuilt to allow reply-less requests")

    @staticmethod
    def _ensure_bodies_index(conn: sqlite3.Connection) -> None:
        """Index the blob reference, once the column it names exists.

        A database written by 4.45 still has the one-payload-per-request shape
        at this point, so creating the index unconditionally fails outright.
        """
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(request_bodies)")
        }
        if "sha" in columns:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_bodies_sha"
                " ON request_bodies(sha)"
            )
        if "input_sha" in columns:
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_bodies_input_sha"
                " ON request_bodies(input_sha)"
            )

    def _migrate_bodies_to_content_addressing(self, conn: sqlite3.Connection) -> None:
        """Re-key 4.45-era body rows, which stored one payload per request.

        Only installs that ran 4.45 or 4.46 have any, and only for as long as
        those versions were running, so this is normally a handful of rows.
        """
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(request_bodies)")
        }
        if "payload" not in columns:
            return
        try:
            legacy = conn.execute(
                "SELECT request_id, dict_id, payload FROM request_bodies"
            ).fetchall()
            with conn:
                conn.execute("ALTER TABLE request_bodies RENAME TO request_bodies_v1")
                conn.executescript(_BODIES_SCHEMA)
                for row in legacy:
                    packed = self._raw_payload(row["payload"], row["dict_id"])
                    if packed is None:
                        continue
                    sha = hashlib.sha256(packed).hexdigest()
                    conn.execute(
                        "INSERT OR IGNORE INTO body_blobs (sha, dict_id, payload)"
                        " VALUES (?, ?, ?)",
                        (sha, row["dict_id"], row["payload"]),
                    )
                    conn.execute(
                        "INSERT OR REPLACE INTO request_bodies"
                        " (request_id, sha, input_sha) VALUES (?, ?, NULL)",
                        (str(row["request_id"]), sha),
                    )
                conn.execute("DROP TABLE request_bodies_v1")
                self._ensure_bodies_index(conn)
            logger.info(
                "Request log bodies re-keyed for deduplication: {} rows", len(legacy)
            )
        except sqlite3.Error as exc:
            logger.warning("Request log body re-keying failed: {}", exc)

    def train_dictionary_from_inline_bodies(self, conn: sqlite3.Connection) -> None:
        """Seed a dictionary from history when nothing is compressed yet.

        A database being compacted for the first time has no blobs to learn
        from, so the samples have to come from the inline columns that are
        about to be replaced.
        """
        self._load_active_dictionary(conn)
        if self._active_dict_id is not None:
            return
        rows = conn.execute(
            "SELECT input_text, output_text, thinking_text, tool_calls FROM requests"
            " WHERE input_text IS NOT NULL ORDER BY ts_epoch DESC LIMIT ?",
            (_BODY_DICT_TRAINING_SAMPLES,),
        ).fetchall()
        samples = [
            blob
            for row in rows
            if (
                blob := pack_bodies(
                    {
                        "input_text": row["input_text"],
                        "output_text": row["output_text"],
                        "thinking_text": row["thinking_text"],
                        "tool_calls": _loads_or_none(row["tool_calls"]),
                    }
                )
            )
            != b"{}"
        ]
        if len(samples) < _BODY_DICT_MIN_SAMPLES:
            return
        trained = zstd.train_dict(samples, _BODY_DICT_SIZE)
        with conn:
            cursor = conn.execute(
                "INSERT INTO body_dictionaries (created_at, content) VALUES (?, ?)",
                (time.time(), trained.dict_content),
            )
        dict_id = int(cursor.lastrowid or 0)
        if dict_id:
            self._dict_cache[dict_id] = trained
            self._active_dict_id = dict_id

    def _load_active_dictionary(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT id FROM body_dictionaries ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self._active_dict_id = int(row[0]) if row is not None else None

    def _maybe_train_dictionary(self, conn: sqlite3.Connection) -> None:
        """Train a compression dictionary once there is enough traffic to learn from.

        A dictionary is what turns 2.7x into 9x, because it can exploit the
        system prompt and conversation history that repeat almost verbatim
        between requests -- redundancy that per-row compression cannot see.
        Until one exists, bodies are still compressed, just less well.
        """
        if not self._compress_bodies or self._active_dict_id is not None:
            return
        try:
            rows = conn.execute(
                "SELECT dict_id, payload FROM body_blobs ORDER BY rowid DESC LIMIT ?",
                (_BODY_DICT_TRAINING_SAMPLES,),
            ).fetchall()
            if len(rows) < _BODY_DICT_MIN_SAMPLES:
                return
            samples = [
                packed
                for packed in (
                    self._raw_payload(row["payload"], row["dict_id"]) for row in rows
                )
                if packed
            ]
            if len(samples) < _BODY_DICT_MIN_SAMPLES:
                return
            started = time.monotonic()
            trained = zstd.train_dict(samples, _BODY_DICT_SIZE)
            with conn:
                cursor = conn.execute(
                    "INSERT INTO body_dictionaries (created_at, content) VALUES (?, ?)",
                    (time.time(), trained.dict_content),
                )
            dict_id = int(cursor.lastrowid or 0)
            if not dict_id:
                return
            self._dict_cache[dict_id] = trained
            self._active_dict_id = dict_id
            logger.info(
                "Request log body dictionary trained from {} samples in {:.1f}s",
                len(samples),
                time.monotonic() - started,
            )
        except (sqlite3.Error, zstd.ZstdError) as exc:
            logger.warning("Request log dictionary training skipped: {}", exc)

    def _raw_payload(self, payload: Any, dict_id: Any) -> bytes | None:
        if payload is None:
            return None
        try:
            return zstd.decompress(bytes(payload), zstd_dict=self._dictionary(dict_id))
        except zstd.ZstdError, ValueError:
            return None

    @staticmethod
    def _ensure_session_columns(conn: sqlite3.Connection) -> None:
        """Add the bind-address columns to a session table created before them.

        A row written before 6.72.2 keeps both NULL, which reads as "this
        session never said where it was listening" -- and that is the honest
        answer, so nothing infers a port for it and nothing acts on it.
        """
        for column, ddl in _SESSION_ADDED_COLUMNS:
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(server_sessions)")
            }
            if column in columns:
                continue
            try:
                conn.execute(ddl)
            except sqlite3.OperationalError:
                # Another process may have won the migration race; only a
                # genuinely missing column is an error.
                columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(server_sessions)")
                }
                if column not in columns:
                    raise

    def _open_session(self, conn: sqlite3.Connection) -> int | None:
        """Record that a server is running, so quiet periods stay explainable."""
        now = time.time()
        address = server_bind_address()
        try:
            with conn:
                cursor = conn.execute(
                    "INSERT INTO server_sessions"
                    " (started_at, last_seen_at, pid, host, port)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        now,
                        now,
                        os.getpid(),
                        address[0] if address else None,
                        address[1] if address else None,
                    ),
                )
                conn.execute(
                    "DELETE FROM server_sessions WHERE id NOT IN ("
                    " SELECT id FROM server_sessions"
                    " ORDER BY started_at DESC LIMIT ?)",
                    (_SESSION_HISTORY_LIMIT,),
                )
            return int(cursor.lastrowid) if cursor.lastrowid else None
        except sqlite3.Error as exc:
            logger.warning("Request log session open failed: {}", exc)
            return None

    @staticmethod
    def _touch_session(
        conn: sqlite3.Connection, session_id: int | None, now: float
    ) -> None:
        if session_id is None:
            return
        # The address rides on every heartbeat rather than only on the insert:
        # the session row is opened while the process is still starting, which
        # on this code path is *before* the listener binds, so a row that only
        # ever recorded what was known at open would record nothing at all.
        address = server_bind_address()
        with contextlib.suppress(sqlite3.Error), conn:
            conn.execute(
                "UPDATE server_sessions"
                " SET last_seen_at = ?, host = ?, port = ? WHERE id = ?",
                (
                    now,
                    address[0] if address else None,
                    address[1] if address else None,
                    session_id,
                ),
            )

    # ------------------------------------------------------------------ writes

    def enqueue(self, record: RequestRecord) -> None:
        """Queue one record without blocking the request path."""
        if self._closed.is_set():
            return
        # Cap before queueing, not at flush time: an uncapped record sits in the
        # queue holding its full body, so a backlog could retain far more than
        # the persisted per-row limit.
        record.input_text = cap_text(record.input_text, self._text_max_chars)
        record.output_text = cap_text(record.output_text, self._text_max_chars)
        record.error_message = cap_text(record.error_message, MAX_ERROR_CHARS)
        try:
            self._queue.put_nowait(record)
        except queue.Full:
            logger.warning("Request log queue full; dropping record {}", record.id)

    def _writer_loop(self) -> None:
        pending: list[RequestRecord] = []
        stopping = False
        # One connection for the writer thread's lifetime; reconnecting per
        # batch re-runs the WAL/synchronous pragmas on every flush.
        conn = self._connect()
        try:
            # Both of these are one-time migrations that can take seconds on a
            # large existing database, so they belong here and never on a
            # request path.
            self._ensure_stats_index(conn)
            # Sub-second to build even on a 4.5 GB log, but a migration all the
            # same, and migrations live here rather than on a request path.
            self._ensure_partial_indexes(conn)
            # Before the rollup backfill, which keys every bucket on the stored
            # column this fills in.
            self._ensure_is_local_backfill(conn)
            # And so is this one, for exactly the same reason: ``harness`` is a
            # rollup dimension, so a bucket built while the column was still
            # NULL would be keyed on the empty string and never agree with the
            # rows it summarises.
            self._ensure_harness_backfill(conn)
            # Must precede the first flush: these aggregates and the live
            # accumulator would otherwise both count any request written in
            # between.
            self._ensure_rollup_backfill(conn)
            self._ensure_totals_backfill(conn)
            self._ensure_auto_vacuum(conn)
            # Before any write, or a flush would insert into a table shape that
            # is about to be replaced.
            self._migrate_bodies_to_content_addressing(conn)
            self._load_active_dictionary(conn)
            self._maybe_train_dictionary(conn)
            session_id = self._open_session(conn)
            last_heartbeat = time.monotonic()
            while not stopping:
                now = time.monotonic()
                if now - last_heartbeat >= _SESSION_HEARTBEAT_SECONDS:
                    last_heartbeat = now
                    self._touch_session(conn, session_id, time.time())
                try:
                    item = self._queue.get(timeout=_WRITER_POLL_SECONDS)
                except queue.Empty:
                    item = None
                if item is None:
                    if pending:
                        self._flush(pending, conn)
                        pending.clear()
                        continue
                    # Nothing to write. This -- after the first flush, behind
                    # readiness, on the thread migrations already live on -- is
                    # where the one-time historical cost backfill runs. It
                    # commits per chunk and returns the moment a request is
                    # queued behind it, so the cost of it being in progress is
                    # bounded by one chunk rather than by the walk.
                    self._ensure_cost_backfill(conn)
                    continue
                if item is _STOP:
                    stopping = True
                else:
                    pending.append(item)
                if len(pending) >= _WRITER_BATCH_SIZE:
                    self._flush(pending, conn)
                    pending.clear()
            # Drain anything enqueued behind the stop sentinel, then exit.
            while True:
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                if item is not None and item is not _STOP:
                    pending.append(item)
            if pending:
                self._flush(pending, conn)
            # Stamp the clean shutdown so the recorded session ends where the
            # server actually stopped rather than up to one heartbeat earlier.
            self._touch_session(conn, session_id, time.time())
        finally:
            conn.close()

    @staticmethod
    def _existing_ids(conn: sqlite3.Connection, ids: list[str]) -> set[str]:
        """Return which of ``ids`` are already stored.

        The insert below is ``INSERT OR REPLACE``, so re-flushing a record that
        is already persisted rewrites the row rather than adding one. The
        permanent counters must not move in that case, and a row already
        counted is the only way to tell.
        """
        if not ids:
            return set()
        placeholders = ", ".join("?" * len(ids))
        return {
            str(row[0])
            for row in conn.execute(
                f"SELECT id FROM requests WHERE id IN ({placeholders})", ids
            )
        }

    @staticmethod
    def _accumulate_totals(
        conn: sqlite3.Connection, records: list[RequestRecord]
    ) -> None:
        """Fold newly stored records into the permanent per-day counters."""
        if not records:
            return
        buckets: dict[tuple[str, str, str], list[int]] = {}
        for record in records:
            day = datetime.fromtimestamp(record.ts_epoch, tz=UTC).strftime("%Y-%m-%d")
            key = (day, record.provider or "", record.resolved_model or "")
            counters = buckets.get(key)
            if counters is None:
                counters = [0] * len(_TOTALS_COUNTERS)
                buckets[key] = counters
            counters[0] += 1
            counters[1] += record.status == "success"
            counters[2] += record.status == "error"
            counters[3] += record.status == "cancelled"
            counters[4] += record.tokens_in or 0
            counters[5] += record.tokens_out or 0
            counters[6] += record.cache_read_tokens or 0
            counters[7] += record.cache_write_tokens or 0
            counters[8] += record.tool_call_count or 0
            counters[9] += bool(record.route_attempt)
            # Counts a real diversion only. ``route_diversion`` also carries
            # ``vision_unavailable``, where nothing was replaced.
            counters[10] += record.route_diverted_from is not None
        conn.executemany(
            _TOTALS_UPSERT_SQL,
            [(*key, *counters) for key, counters in buckets.items()],
        )

    @staticmethod
    def _rollup_key(record: RequestRecord) -> tuple[Any, ...]:
        """The ten dimension values of one record, as the rollup stores them.

        SQL NULL is stored as the empty string, matching the ``COALESCE(x, '')``
        the backfill uses, so a row written live and the same row rebuilt by the
        backfill land in the same bucket.
        """
        return (
            _floor_hour(record.ts_epoch),
            _is_local_value(record),
            record.provider or "",
            record.resolved_model or "",
            record.requested_model or "",
            record.status,
            record.endpoint,
            record.key_label or "",
            record.optimization or "",
            record.harness or "",
        )

    @staticmethod
    def _rollup_counter_values(record: RequestRecord) -> dict[str, float]:
        """One record's contribution to each rollup counter.

        Every entry is the Python twin of the SQL expression in
        ``_ROLLUP_COUNTERS``; the two are asserted to cover the same names
        below, so a counter cannot be declared and left unfilled.
        """
        duration = record.duration_ms
        ttft = record.ttft_ms
        tool_calls = record.tool_call_count or 0
        values: dict[str, float] = {
            "requests": 1,
            "tokens_in": record.tokens_in or 0,
            "tokens_out": record.tokens_out or 0,
            "cache_read_tokens": record.cache_read_tokens or 0,
            "cache_write_tokens": record.cache_write_tokens or 0,
            "cache_reported": int(record.cache_read_tokens is not None),
            "tool_calls": tool_calls,
            "turns_with_tools": int(tool_calls > 0),
            "turns_with_reasoning": int((record.thinking_chars or 0) > 0),
            "served_by_fallback": int((record.route_attempt or 0) > 0),
            "route_reported": int(record.route_attempt is not None),
            "diverted": int(record.route_diverted_from is not None),
            "vision_unavailable": int(record.route_diversion == "vision_unavailable"),
            "vision_described": int(record.route_diversion == "vision_described"),
            "with_images": int((record.input_image_count or 0) > 0),
            "duration_sum": duration if duration is not None else 0.0,
            "duration_count": int(duration is not None),
            "ttft_sum": ttft if ttft is not None else 0.0,
            "ttft_count": int(ttft is not None),
        }
        for name in _ROLLUP_RECOVERY_COUNTERS:
            values[name] = 0
        for attempt in record.attempts:
            params = attempt.params
            if not isinstance(params, dict):
                continue
            for name in _ROLLUP_RECOVERY_COUNTERS:
                values[name] += int(params.get(name) or 0)
        if set(values) != set(_ROLLUP_COUNTER_NAMES):
            raise RuntimeError(
                "request_stats_rollup counters"
                f" {sorted(set(_ROLLUP_COUNTER_NAMES) ^ set(values))}"
                " are declared but not accumulated"
            )
        return values

    @staticmethod
    def _upstream_status_counts(record: RequestRecord) -> dict[str, int]:
        """Tries per upstream status across this record's retried attempts.

        Mirrors the SQL pass exactly, ``a.ladder_tries > 1`` included: the
        denormalised column is what keeps the JSON walk off the ~95% of
        attempts that never retried.
        """
        counts: dict[str, int] = {}
        for attempt in record.attempts:
            if (attempt.ladder_tries or 0) <= 1:
                continue
            params = attempt.params
            ladder = params.get("ladder") if isinstance(params, dict) else None
            tries = ladder.get("tries") if isinstance(ladder, dict) else None
            if not isinstance(tries, list):
                continue
            for entry in tries:
                if not isinstance(entry, dict):
                    continue
                status = entry.get("status")
                if status is None:
                    continue
                key = str(status)
                counts[key] = counts.get(key, 0) + 1
        return counts

    @classmethod
    def _accumulate_rollup(
        cls, conn: sqlite3.Connection, records: list[RequestRecord]
    ) -> None:
        """Fold newly stored records into the three stats rollup tables.

        Written from the in-memory batch, in the writer's existing transaction,
        on the same filtered record list the permanent totals use -- so a
        re-flushed record that ``INSERT OR REPLACE`` rewrites rather than adds
        is not counted twice here either.

        The attempt-derived counters come from the record's own attempt list,
        never from a re-read of ``request_attempts``: ``_store_attempts`` is
        writing that table from the same objects in the same transaction.
        """
        if not records:
            return
        rollup: dict[tuple[Any, ...], dict[str, float]] = {}
        latency: dict[tuple[Any, ...], int] = {}
        detail: dict[tuple[Any, ...], list[int]] = {}
        for record in records:
            key = cls._rollup_key(record)
            bucket = rollup.get(key)
            if bucket is None:
                bucket = dict.fromkeys(_ROLLUP_COUNTER_NAMES, 0.0)
                rollup[key] = bucket
            for name, value in cls._rollup_counter_values(record).items():
                bucket[name] += value

            if record.duration_ms is not None:
                latency_key = (*key, _latency_bucket(float(record.duration_ms)))
                latency[latency_key] = latency.get(latency_key, 0) + 1

            for detail_key, count in cls._detail_rows(record, key):
                totals = detail.get(detail_key)
                if totals is None:
                    totals = [0, 0]
                    detail[detail_key] = totals
                totals[0] += count
                totals[1] += 1

        conn.executemany(
            _ROLLUP_UPSERT_SQL,
            [
                (*key, *(bucket[name] for name in _ROLLUP_COUNTER_NAMES))
                for key, bucket in rollup.items()
            ],
        )
        if latency:
            conn.executemany(
                _LATENCY_UPSERT_SQL,
                [(*key, count) for key, count in latency.items()],
            )
        if detail:
            conn.executemany(
                _DETAIL_UPSERT_SQL,
                [(*key, *totals) for key, totals in detail.items()],
            )

    @classmethod
    def _detail_rows(
        cls, record: RequestRecord, key: tuple[Any, ...]
    ) -> Iterator[tuple[tuple[Any, ...], int]]:
        """Yield ((dimensions, kind, a, b, c), count) for one record.

        The caller adds 1 to ``requests`` per yielded row, which is what makes
        the upstream count exact under SUM: one request contributes one to each
        distinct status it saw and lives in exactly one dimension bucket, so
        summing reproduces ``COUNT(DISTINCT request_id)`` without a DISTINCT.
        """
        message = cap_text(record.error_message, MAX_ERROR_CHARS)
        if record.status == "error" and message is not None:
            yield (*key, _DETAIL_ERROR, message, "", ""), 1
        # ``COALESCE``, not ``or``: an empty-string provider is a different
        # fact from a NULL one, and only NULL becomes "(unknown)" in the SQL
        # this mirrors.
        provider = UNKNOWN_PROVIDER_KEY if record.provider is None else record.provider
        model = (
            UNKNOWN_PROVIDER_KEY
            if record.resolved_model is None
            else record.resolved_model
        )
        served_by = f"{provider}/{model}"
        if (record.route_attempt or 0) > 0 and record.route_primary_model is not None:
            yield (
                (
                    *key,
                    _DETAIL_FALLBACK,
                    record.route_primary_model,
                    served_by,
                    "",
                ),
                1,
            )
        if (
            record.route_diversion is not None
            and record.route_diverted_from is not None
        ):
            yield (
                (
                    *key,
                    _DETAIL_DIVERTED,
                    record.route_diverted_from,
                    record.route_diversion,
                    served_by,
                ),
                1,
            )
        for status, count in cls._upstream_status_counts(record).items():
            yield (*key, _DETAIL_UPSTREAM, status, "", ""), count

    def _pack_record(self, record: RequestRecord) -> tuple[bytes | None, bytes | None]:
        """Return this record's (prompt, everything-else) blobs."""
        cap = self._text_max_chars
        values = {
            "input_text": cap_text(record.input_text, cap),
            "output_text": cap_text(record.output_text, cap),
            "thinking_text": cap_text(record.thinking_text, cap),
            "tool_calls": record.tool_calls,
        }
        return (
            _packed_or_none(pack_fields(values, _INPUT_FIELDS)),
            _packed_or_none(pack_fields(values, _REST_FIELDS)),
        )

    def _compress_packed(
        self, packed: bytes, *, level: int | None = None
    ) -> tuple[int | None, bytes]:
        level = self._compression_level if level is None else level
        dict_id = self._active_dict_id
        return dict_id, zstd.compress(
            packed, level=level, zstd_dict=self._dictionary(dict_id)
        )

    @staticmethod
    def _store_images(conn: sqlite3.Connection, batch: list[RequestRecord]) -> None:
        """Point each request at its images, storing unseen pictures once.

        A screenshot re-sent on every turn of a conversation has the same
        content address every time, so ``INSERT OR IGNORE`` keeps exactly one
        copy however many requests reference it.
        """
        blobs: dict[str, tuple[Any, ...]] = {}
        links: list[tuple[str, int, str]] = []
        for record in batch:
            for position, image in enumerate(record.images):
                blobs.setdefault(
                    image.sha256,
                    (
                        image.sha256,
                        image.kind,
                        image.media_type,
                        image.source_bytes,
                        image.width,
                        image.height,
                        image.thumbnail_media_type,
                        image.thumbnail,
                        image.sent_width,
                        image.sent_height,
                    ),
                )
                links.append((record.id, position, image.sha256))
        if not links:
            return
        # Not ``INSERT OR IGNORE``: describe mode writes a row for a picture
        # the moment it has a description, which is before the request that
        # carried it is flushed. Ignoring the conflict would then leave that
        # picture without its thumbnail forever. ``COALESCE`` fills only what
        # is still missing, so a complete row is never overwritten and the
        # dedup that makes one screenshot cost one row is unchanged.
        conn.executemany(
            "INSERT INTO image_blobs (sha, kind, media_type,"
            " source_bytes, width, height, thumbnail_media_type, thumbnail,"
            " sent_width, sent_height)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(sha) DO UPDATE SET"
            " media_type = COALESCE(image_blobs.media_type, excluded.media_type),"
            " source_bytes = COALESCE(image_blobs.source_bytes,"
            " excluded.source_bytes),"
            " width = COALESCE(image_blobs.width, excluded.width),"
            " height = COALESCE(image_blobs.height, excluded.height),"
            " thumbnail_media_type = COALESCE(image_blobs.thumbnail_media_type,"
            " excluded.thumbnail_media_type),"
            " thumbnail = COALESCE(image_blobs.thumbnail, excluded.thumbnail),"
            # Assigned, not COALESCEd, unlike every column above it. The
            # others are facts about the picture and only ever get filled in;
            # this one is a fact about the last request that *sent* it, and a
            # NULL here means "that request sent it as it arrived" -- which is
            # exactly what turning resizing off produces. Keeping the previous
            # value would then show a resize on a request that did none.
            " sent_width = excluded.sent_width,"
            " sent_height = excluded.sent_height",
            list(blobs.values()),
        )
        conn.executemany(
            "INSERT OR REPLACE INTO request_images (request_id, position, sha)"
            " VALUES (?, ?, ?)",
            links,
        )

    @staticmethod
    def _store_attempts(conn: sqlite3.Connection, batch: list[RequestRecord]) -> None:
        """Persist each record's route attempts.

        ``INSERT OR REPLACE``: an attempt is identified by (request, index), so
        replacing is the correct merge if a record is ever written twice.
        """
        rows = [
            (
                record.id,
                attempt.attempt,
                attempt.provider,
                attempt.model_ref,
                attempt.outcome.value,
                attempt.error_kind,
                attempt.error_message,
                attempt.duration_ms,
                json.dumps(attempt.params) if attempt.params else None,
                attempt.wire_body,
                None
                if attempt.reasoning_emitted is None
                else int(attempt.reasoning_emitted),
                attempt.key_index,
                attempt.key_label,
                attempt.ladder_tries,
                attempt.tokens_in,
                attempt.tokens_out,
                attempt.cost_usd,
                attempt.cost_source,
            )
            for record in batch
            for attempt in record.attempts
        ]
        if not rows:
            return
        # Placeholders are counted against the column list mechanically -- a
        # 43-column INSERT with 42 markers once shipped and broke every write.
        # The marker string is generated from the same tuple the row is built
        # against, so the two cannot drift; this catches the row itself.
        if len(rows[0]) != len(_ATTEMPT_INSERT_COLUMNS):
            raise RuntimeError(
                "request_attempts row width"
                f" {len(rows[0])} != {len(_ATTEMPT_INSERT_COLUMNS)} columns"
            )
        conn.executemany(
            "INSERT OR REPLACE INTO request_attempts"
            f" ({', '.join(_ATTEMPT_INSERT_COLUMNS)}) VALUES"
            f" ({', '.join('?' * len(_ATTEMPT_INSERT_COLUMNS))})",
            rows,
        )

    @staticmethod
    def _fetch_attempts(
        conn: sqlite3.Connection, request_id: str
    ) -> list[dict[str, Any]]:
        """Return one request's attempts in the order the chain tried them."""
        rows = conn.execute(
            "SELECT attempt, provider, model_ref, outcome, error_kind,"
            " error_message, duration_ms, params, wire_body, reasoning_emitted,"
            " key_index, key_label, ladder_tries, tokens_in, tokens_out,"
            " cost_usd, cost_source"
            " FROM request_attempts"
            " WHERE request_id = ? ORDER BY attempt",
            (request_id,),
        ).fetchall()
        return [
            {
                "attempt": row["attempt"],
                "provider": row["provider"],
                "model_ref": row["model_ref"],
                "outcome": row["outcome"],
                "error_kind": row["error_kind"],
                "error_message": row["error_message"],
                "duration_ms": row["duration_ms"],
                "params": _loads_or_none(row["params"]),
                "wire_body": _loads_or_none(row["wire_body"]),
                "reasoning_emitted": (
                    None
                    if row["reasoning_emitted"] is None
                    else bool(row["reasoning_emitted"])
                ),
                # NULL on every attempt written before 6.53.0, and on every
                # attempt that is not a describe hop: the request row's own
                # counters come from the client-facing stream, not from here,
                # so an ordinary attempt has nothing to put in these.
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                # NULL on every attempt written before these columns existed:
                # not measured, which the UI renders as a dash rather than as
                # a keyless request.
                "key_index": row["key_index"],
                "key_label": row["key_label"],
                "ladder_tries": row["ladder_tries"],
                # What this hop cost on its own. A describe attempt is a real
                # call to a real model on a real key; NULL everywhere else,
                # because an ordinary attempt's cost is the request row's and
                # repeating it here would double every joined total.
                "cost_usd": row["cost_usd"],
                "cost_source": row["cost_source"],
            }
            for row in rows
        ]

    @staticmethod
    def _fetch_ladder_rollup(
        conn: sqlite3.Connection, request_ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Roll one request's attempt ladders up into three export columns.

        ``ladder_tries`` sums the tries across every attempt the chain made;
        ``ladder_statuses`` merges their status censuses; ``ladder_root_cause``
        is the stored sentence of the first *failed* attempt -- the one that
        explains why there was a fallback at all. Rows written before the
        ladder existed contribute nothing and are left blank rather than zero.
        """
        if not request_ids:
            return {}
        markers = ", ".join("?" * len(request_ids))
        rows = conn.execute(
            "SELECT request_id, attempt, outcome, params, ladder_tries"
            " FROM request_attempts"
            f" WHERE request_id IN ({markers}) ORDER BY request_id, attempt",
            request_ids,
        ).fetchall()
        out: dict[str, dict[str, Any]] = {}
        census: dict[str, dict[str, int]] = {}
        for row in rows:
            tries = row["ladder_tries"]
            if tries is None:
                continue
            request_id = str(row["request_id"])
            entry = out.setdefault(
                request_id, {**_EMPTY_LADDER_ROLLUP, "_failed_root_cause": ""}
            )
            entry["ladder_tries"] = int(entry["ladder_tries"] or 0) + int(tries)
            params = _loads_or_none(row["params"])
            ladder = params.get("ladder") if isinstance(params, dict) else None
            if not isinstance(ladder, dict):
                continue
            summary = ladder.get("summary")
            if isinstance(summary, dict):
                counts = summary.get("statuses_by_code")
                if isinstance(counts, dict):
                    bucket = census.setdefault(request_id, {})
                    for code, count in counts.items():
                        bucket[str(code)] = bucket.get(str(code), 0) + int(count)
            root_cause = str(ladder.get("root_cause") or "")
            if not root_cause:
                continue
            # The first *failed* attempt is the one that explains the fallback,
            # so it wins outright. A chain that retried its way to a success
            # still has a story, though -- "a fallback that quietly works" is
            # the blind spot this whole surface exists for -- so the first
            # attempt with anything to say is kept when nothing failed.
            if str(row["outcome"]) == "failed":
                if not entry["_failed_root_cause"]:
                    entry["_failed_root_cause"] = root_cause
            elif not entry["ladder_root_cause"]:
                entry["ladder_root_cause"] = root_cause
        for request_id, bucket in census.items():
            out[request_id]["ladder_statuses"] = format_status_census(bucket)
        for entry in out.values():
            failed = entry.pop("_failed_root_cause")
            if failed:
                entry["ladder_root_cause"] = failed
        return out

    @staticmethod
    def _fetch_images(
        conn: sqlite3.Connection, request_id: str
    ) -> list[dict[str, Any]]:
        """Return one request's images in the order they appeared."""
        rows = conn.execute(
            "SELECT i.sha, i.kind, i.media_type, i.source_bytes, i.width,"
            " i.height, i.thumbnail_media_type, i.thumbnail, i.description,"
            " i.described_by, i.described_at, i.sent_width, i.sent_height"
            " FROM request_images AS r JOIN image_blobs AS i ON i.sha = r.sha"
            " WHERE r.request_id = ? ORDER BY r.position",
            (request_id,),
        ).fetchall()
        images: list[dict[str, Any]] = []
        for row in rows:
            thumbnail = row["thumbnail"]
            images.append(
                {
                    "sha256": row["sha"],
                    "kind": row["kind"],
                    "media_type": row["media_type"],
                    "source_bytes": row["source_bytes"],
                    "width": row["width"],
                    "height": row["height"],
                    # The size it actually left at, when the downscaler shrank
                    # it. NULL means it was sent as it arrived.
                    "sent_width": row["sent_width"],
                    "sent_height": row["sent_height"],
                    "thumbnail_media_type": row["thumbnail_media_type"],
                    # Base64 so the payload is JSON, and the client can use it
                    # directly as a data URI without a second round trip.
                    "thumbnail_base64": (
                        base64.b64encode(thumbnail).decode("ascii")
                        if isinstance(thumbnail, bytes | bytearray)
                        else None
                    ),
                    # What describe mode replaced this picture with, if it ran.
                    # NULL on every picture that was sent as a picture, which
                    # is what lets the request detail say which happened.
                    "description": row["description"],
                    "described_by": row["described_by"],
                    "described_at": row["described_at"],
                }
            )
        return images

    def _store_bodies(
        self,
        conn: sqlite3.Connection,
        packed: dict[str, tuple[bytes | None, bytes | None]],
        *,
        level: int = _BODY_COMPRESSION_LEVEL,
    ) -> None:
        """Point each request at its blobs, compressing only unseen content."""
        if not packed:
            return
        mapping: list[tuple[str, str | None, str | None]] = []
        blobs: dict[str, bytes] = {}
        for request_id, (input_blob, rest_blob) in packed.items():
            shas: list[str | None] = []
            for blob in (rest_blob, input_blob):
                if blob is None:
                    shas.append(None)
                    continue
                sha = hashlib.sha256(blob).hexdigest()
                blobs.setdefault(sha, blob)
                shas.append(sha)
            mapping.append((request_id, shas[0], shas[1]))
        if blobs:
            placeholders = ", ".join("?" * len(blobs))
            known = {
                str(row[0])
                for row in conn.execute(
                    f"SELECT sha FROM body_blobs WHERE sha IN ({placeholders})",
                    sorted(blobs),
                )
            }
            fresh = [(sha, blob) for sha, blob in blobs.items() if sha not in known]
            if fresh:
                conn.executemany(
                    "INSERT OR IGNORE INTO body_blobs (sha, dict_id, payload)"
                    " VALUES (?, ?, ?)",
                    [
                        (sha, *self._compress_packed(blob, level=level))
                        for sha, blob in fresh
                    ],
                )
        conn.executemany(
            "INSERT OR REPLACE INTO request_bodies (request_id, sha, input_sha)"
            " VALUES (?, ?, ?)",
            mapping,
        )

    def _flush(self, batch: list[RequestRecord], conn: sqlite3.Connection) -> None:
        rows = [self._record_to_row(record) for record in batch]
        packed: dict[str, tuple[bytes | None, bytes | None]] = {}
        if self._compress_bodies:
            for record in batch:
                blobs = self._pack_record(record)
                if blobs != (None, None):
                    packed[record.id] = blobs
        try:
            with conn:
                already_stored = self._existing_ids(
                    conn, [record.id for record in batch]
                )
                conn.executemany(_REQUEST_INSERT_SQL, rows)
                self._store_bodies(conn, packed)
                self._store_images(conn, batch)
                self._store_attempts(conn, batch)
                # One list, computed once and shared, so the two aggregates
                # provably fold in the same set of records.
                fresh = [record for record in batch if record.id not in already_stored]
                self._accumulate_totals(conn, fresh)
                # Inside the same ``with conn:`` as the rows themselves, so a
                # failed batch rolls the rollup back with it and the aggregate
                # can never drift ahead of the table it summarises.
                self._accumulate_rollup(conn, fresh)
        except sqlite3.Error as exc:
            logger.warning("Request log write failed: {}", exc)
            return
        self._inserts_since_prune += len(batch)
        if self._inserts_since_prune >= _PRUNE_EVERY_INSERTS:
            self._inserts_since_prune = 0
            self.prune()
            # Cheap no-op once a dictionary exists; this lets a fresh install
            # start compressing properly without waiting for a restart.
            self._maybe_train_dictionary(conn)

    def _record_to_row(self, record: RequestRecord) -> tuple[Any, ...]:
        # With compression on, the text lives in ``request_bodies`` and these
        # columns stay NULL. Reads fall back to them so rows written by an
        # older version keep working until retention drains them.
        inline = not self._compress_bodies

        def body(text: str | None) -> str | None:
            return cap_text(text, self._text_max_chars) if inline else None

        row = (
            record.id,
            record.ts_epoch,
            record.ts_iso,
            record.endpoint,
            record.protocol,
            record.requested_model,
            record.provider,
            record.resolved_model,
            int(record.stream),
            body(record.input_text),
            body(record.output_text),
            record.input_sha256,
            record.output_sha256,
            record.input_chars,
            record.output_chars,
            record.reasoning,
            record.requested_reasoning,
            record.reasoning_adaptation,
            record.reasoning_adaptation_kind,
            json.dumps(record.params) if record.params is not None else None,
            record.tokens_in,
            record.tokens_out,
            record.cache_read_tokens,
            record.cache_write_tokens,
            record.ttft_ms,
            record.duration_ms,
            record.status,
            record.error_kind,
            cap_text(record.error_message, MAX_ERROR_CHARS),
            json.dumps(record.headers) if record.headers else None,
            record.key_index,
            record.key_label,
            body(record.thinking_text),
            record.thinking_chars,
            json.dumps(record.tool_calls) if inline and record.tool_calls else None,
            record.tool_call_count,
            record.route_attempt,
            record.route_primary_model,
            record.route_chain,
            record.route_diverted_from,
            record.route_diversion,
            record.input_image_count,
            record.image_delivery,
            record.optimization,
            record.optimization_tokens_saved,
            _is_local_value(record),
            record.harness,
            record.adapter_tokens_in,
            record.adapter_tokens_out,
            record.est_tokens_in,
            record.est_image_tokens,
            record.image_bytes_in,
            record.image_bytes_out,
            record.cost_usd,
            record.cost_source,
        )
        # Placeholders are counted against the column list mechanically, the
        # same guard ``_store_attempts`` carries: a hand-written INSERT whose
        # marker count drifted from its column list once broke every write.
        if len(row) != len(_REQUEST_INSERT_COLUMNS):
            raise RuntimeError(
                f"requests row width {len(row)} !="
                f" {len(_REQUEST_INSERT_COLUMNS)} columns"
            )
        return row

    def close(self, *, timeout: float = _CLOSE_TIMEOUT_SECONDS) -> None:
        """Stop the writer thread after flushing queued records.

        The wait scales with the backlog. A fixed deadline silently discarded
        whatever was still queued, and compressing bodies made that far easier
        to hit: a full batch is real CPU work, so a deep queue can need tens of
        seconds to drain and the writer is a daemon thread that dies with the
        interpreter. Anything genuinely abandoned is reported rather than lost
        quietly.
        """
        if self._closed.is_set():
            return
        self._closed.set()
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            with contextlib.suppress(queue.Full):
                self._queue.put(_STOP, timeout=timeout)
        deadline = time.monotonic() + max(
            timeout, self._queue.qsize() * _CLOSE_SECONDS_PER_RECORD
        )
        while self._writer.is_alive() and time.monotonic() < deadline:
            self._writer.join(timeout=0.5)
        remaining = self._queue.qsize()
        if self._writer.is_alive() and remaining:
            logger.warning(
                "Request log writer still draining at shutdown; {} records unwritten",
                remaining,
            )

    # ------------------------------------------------------------------ reads

    def _where(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
        endpoint: str | None = None,
        key: str | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
        local: str | None = None,
        harness: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        args: list[Any] = []
        # Locally answered rows are real traffic but they are not upstream
        # traffic, and on a busy install they outnumber it. "hide" removes
        # exactly them, leaving "(unknown)" rows -- a different claim -- alone.
        #
        # Read off the stored column rather than re-derived from ``provider``
        # and ``optimization``: the predicate form referenced a column the
        # covering index did not carry, so ``hide`` fell back to a base-table
        # scan and was slower than ``all`` on every aggregate.
        if local == "hide":
            clauses.append(f"{LOCAL_ANSWER_COLUMN_SQL} = 0")
        elif local == "only":
            clauses.append(f"{LOCAL_ANSWER_COLUMN_SQL} = 1")
        if provider:
            # Comma-separated values mean "any of these providers" (multi-select).
            providers = [part for part in provider.split(",") if part]
            if providers:
                # The breakdown emits synthetic keys for traffic that never had
                # a provider (see ``PROVIDER_KEY_SQL``). Those keys are what a
                # reader sees and therefore what they will filter by, so they
                # have to resolve to a predicate rather than to ``IN`` against a
                # column that is NULL for exactly those rows.
                named = [
                    part
                    for part in providers
                    if not part.startswith(LOCAL_PROVIDER_PREFIX)
                    and part != UNKNOWN_PROVIDER_KEY
                ]
                alternatives: list[str] = []
                named_args: list[Any] = []
                local_args: list[Any] = []
                if named:
                    placeholders = ",".join("?" * len(named))
                    alternatives.append(f"provider IN ({placeholders})")
                    named_args.extend(named)
                for part in providers:
                    if part.startswith(LOCAL_PROVIDER_PREFIX):
                        alternatives.append("(provider IS NULL AND optimization = ?)")
                        local_args.append(part[len(LOCAL_PROVIDER_PREFIX) :])
                    elif part == UNKNOWN_PROVIDER_KEY:
                        alternatives.append(
                            "(provider IS NULL AND optimization IS NULL)"
                        )
                clauses.append(f"({' OR '.join(alternatives)})")
                args.extend(named_args)
                args.extend(local_args)
        if harness:
            # Comma-separated values mean "any of these harnesses", mirroring
            # the model filter rather than the single-valued ones: what a
            # reader filters by is a row of the ``by_harness`` breakdown, and
            # comparing two agents is the question that breakdown invites.
            harnesses = [part for part in harness.split(",") if part]
            if harnesses:
                placeholders = ",".join("?" * len(harnesses))
                clauses.append(f"harness IN ({placeholders})")
                args.extend(harnesses)
        if key:
            clauses.append("key_label = ?")
            args.append(key)
        if model:
            # Comma-separated values mean "any of these models".
            models = [part for part in model.split(",") if part]
            if models:
                placeholders = ",".join("?" * len(models))
                clauses.append(
                    f"(resolved_model IN ({placeholders})"
                    f" OR requested_model IN ({placeholders}))"
                )
                args.extend(models)
                args.extend(models)
        if status:
            clauses.append("status = ?")
            args.append(status)
        if endpoint:
            clauses.append("endpoint = ?")
            args.append(endpoint)
        if since is not None:
            clauses.append("ts_epoch >= ?")
            args.append(since)
        if until is not None:
            clauses.append("ts_epoch <= ?")
            args.append(until)
        terms = q.split() if q else []
        if terms:
            # Legacy inline text and compressed bodies coexist, so search has to
            # cover both. The correlated subquery keeps this self-contained --
            # no caller of ``_where`` needs to know about the second table --
            # and takes the whole query rather than one term at a time, so a
            # row is decompressed once however many terms were typed.
            inline = " AND ".join(
                "("
                + " OR ".join(f"{column} LIKE ?" for column in _SEARCHED_COLUMNS)
                + ")"
                for _ in terms
            )
            for term in terms:
                args.extend([f"%{term}%"] * len(_SEARCHED_COLUMNS))
            clauses.append(
                f"(({inline}) OR EXISTS ("
                " SELECT 1 FROM request_bodies r"
                " LEFT JOIN body_blobs br ON br.sha = r.sha"
                " LEFT JOIN body_blobs bi ON bi.sha = r.input_sha"
                " WHERE r.request_id = requests.id"
                " AND fcc_bodies_match(br.payload, br.dict_id,"
                " bi.payload, bi.dict_id, ?)))"
            )
            args.append(q)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, args

    def list_requests(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
        endpoint: str | None = None,
        key: str | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
        local: str | None = None,
        harness: str | None = None,
        body_preview_chars: int | None = LIST_BODY_PREVIEW_CHARS,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return (rows, total) newest-first, with bodies truncated for list views."""
        where, args = self._where(
            provider=provider,
            model=model,
            status=status,
            endpoint=endpoint,
            key=key,
            since=since,
            until=until,
            q=q,
            local=local,
            harness=harness,
        )
        limit = max(1, min(limit, 500))
        offset = max(0, offset)
        if body_preview_chars is None:
            body_select = "input_text, output_text"
            body_args: list[Any] = []
        else:
            preview = max(0, body_preview_chars)
            body_select = (
                "substr(input_text, 1, ?) AS input_text,"
                " length(input_text) AS input_text_length,"
                " substr(output_text, 1, ?) AS output_text,"
                " length(output_text) AS output_text_length"
            )
            body_args = [preview, preview]
        columns = ", ".join(_LIST_METADATA_COLUMNS)
        with self._connection() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) FROM requests{where}", args
            ).fetchone()[0]
            cursor = conn.execute(
                f"SELECT {columns}, {body_select} FROM requests{where}"
                " ORDER BY ts_epoch DESC LIMIT ? OFFSET ?",
                [*body_args, *args, limit, offset],
            )
            raw_rows = cursor.fetchall()
            bodies = self._fetch_bodies(conn, [str(row["id"]) for row in raw_rows])
            rows = [
                self._row_to_dict(
                    row,
                    body_preview_chars=body_preview_chars,
                    bodies=bodies.get(str(row["id"])),
                )
                for row in raw_rows
            ]
        return rows, total

    def cost_breakdown(
        self,
        *,
        limit: int = 20,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
        endpoint: str | None = None,
        key: str | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
        local: str | None = None,
        harness: str | None = None,
    ) -> dict[str, Any]:
        """What the filtered traffic cost, split by provenance, per dimension.

        Deliberately not served from the stats rollup. The rollup is keyed on
        nine dimensions and counts integers; adding a currency to it would mean
        a versioned rebuild of every bucket on every installation, and the
        question here -- "what did this cost, and how much of the answer is a
        guess" -- is asked on a page, not on the hot path.

        **Reported and estimated are summed apart and never added together.**
        A merged total silently launders an estimate into a fact, and no reader
        can tell afterwards which half was which. The two arrive as two numbers
        and are rendered as two numbers.

        **Every sum ships with its denominator.** ``priced`` of ``requests`` is
        what makes a partial total readable as a partial total; without it a
        window where nine of ten models are unpriced looks like a cheap week.

        Bare ``SUM`` throughout, with no ``COALESCE``: SQLite sums zero rows to
        NULL, and NULL -- not zero -- is the correct answer for a group nothing
        priced.
        """
        limit = max(1, min(limit, 200))
        # Twelve elements, and the first is a literal string. ``stats()`` keys
        # on a ten-element tuple of filters, and the two live in the same dict:
        # a different arity is what makes a collision impossible, which matters
        # here because a user really can filter on ``provider=cost_breakdown``.
        # The same shape ``reasoning_by_model`` and ``image_estimate_by_provider``
        # already use.
        cache_key = (
            "cost_breakdown",
            limit,
            provider,
            model,
            status,
            endpoint,
            key,
            since,
            until,
            q,
            local,
            harness,
        )
        now = time.monotonic()
        with self._stats_lock:
            cached = self._stats_cache.get(cache_key)
            if cached is not None:
                if now - cached[0] < _STATS_CACHE_TTL_SECONDS:
                    self._stats_cache.move_to_end(cache_key)
                    return _copy_cost_payload(cached[1])
                del self._stats_cache[cache_key]
        where, args = self._where(
            provider=provider,
            model=model,
            status=status,
            endpoint=endpoint,
            key=key,
            since=since,
            until=until,
            q=q,
            local=local,
            harness=harness,
        )
        measures = (
            "SUM(CASE WHEN cost_source = 'provider' THEN cost_usd END)"
            " AS reported_usd,"
            " SUM(CASE WHEN cost_source IS NOT NULL"
            " AND cost_source <> 'provider' THEN cost_usd END) AS estimated_usd,"
            " SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END) AS priced,"
            " COUNT(*) AS requests"
        )
        result: dict[str, Any] = {}
        with self._connection() as conn:
            totals = conn.execute(
                f"SELECT {measures} FROM requests{where}", args
            ).fetchone()
            result["totals"] = _cost_row(totals, key=None)
            result["by_source"] = [
                {
                    "key": row["key"],
                    "cost_usd": row["cost_usd"],
                    "requests": row["requests"],
                }
                for row in conn.execute(
                    "SELECT cost_source AS key, SUM(cost_usd) AS cost_usd,"
                    f" COUNT(*) AS requests FROM requests{where}"
                    f"{' AND' if where else ' WHERE'} cost_source IS NOT NULL"
                    # "Priced by" is a list of sources that priced something,
                    # and the unpriced marker is the record of an attempt that
                    # did not. Counting it here would put "nobody" at the top
                    # of the list of who priced this month.
                    f" AND cost_source <> '{_UNPRICED_COST_SOURCE}'"
                    " GROUP BY cost_source ORDER BY cost_source",
                    args,
                ).fetchall()
            ]
            for name, expression in _COST_DIMENSION_SQL.items():
                rows = conn.execute(
                    f"SELECT {expression} AS key, {measures}"
                    f" FROM requests{where} GROUP BY key",
                    args,
                ).fetchall()
                ordered = sorted(
                    (_cost_row(row, key=row["key"]) for row in rows),
                    key=_cost_sort_key,
                    reverse=True,
                )
                result[f"by_{name}"] = ordered[:limit]
        with self._stats_lock:
            # Stamped when the answer was *finished*, not when it was asked for.
            # Every neighbouring method in this file stamps the start, and for
            # them that is the same instant -- ``stats()`` answers in 0.11 s.
            # This one measured 12 s on a 4.5 GB log, which is longer than the
            # whole TTL: stamping the start meant every entry was already
            # expired the moment it was written, and the cache never once hit.
            # Measured: two consecutive calls 12.0 s and 12.2 s before this
            # line, 13.4 s and 0.004 s after it.
            self._stats_cache[cache_key] = (
                time.monotonic(),
                _copy_cost_payload(result),
            )
            self._stats_cache.move_to_end(cache_key)
            while len(self._stats_cache) > _STATS_CACHE_MAX_ENTRIES:
                self._stats_cache.popitem(last=False)
        return result

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            cursor = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            bodies = self._fetch_bodies(conn, [request_id])
            images = self._fetch_images(conn, request_id)
            attempts = self._fetch_attempts(conn, request_id)
        data = self._row_to_dict(
            row, body_preview_chars=None, bodies=bodies.get(request_id)
        )
        data["input_images"] = images
        data["route_attempts"] = attempts
        return data

    def iter_export_rows(
        self,
        *,
        columns: list[str],
        need_bodies: bool,
        need_ladder: bool = False,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
        endpoint: str | None = None,
        key: str | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
        local: str | None = None,
        harness: str | None = None,
        page_size: int = 1_000,
    ) -> Generator[dict[str, Any]]:
        """Yield every matching row for an export, bypassing the 500-row page cap.

        Uses keyset pagination over ``(ts_epoch, id)`` instead of OFFSET so a
        full-table export stays O(n) rather than O(n^2) on the offset walk, and
        keeps a single connection open for the whole stream (closed in
        ``finally`` so abandoning the generator mid-iteration leaks nothing).
        Bodies are decompressed only when ``need_bodies`` is true and a row
        actually references a stored blob.

        Typed ``Generator`` rather than ``Iterator`` because ``close()`` is
        part of the contract: a caller that stops early -- a bounded scan, an
        aborted download -- calls it to run the ``finally`` above now, instead
        of leaving the connection open until the garbage collector notices.
        """
        where, args = self._where(
            provider=provider,
            model=model,
            status=status,
            endpoint=endpoint,
            key=key,
            since=since,
            until=until,
            q=q,
            local=local,
            harness=harness,
        )
        conn = self._connect()
        try:
            cursor: Any = None
            while True:
                page_where = where
                page_args: list[Any] = list(args)
                if cursor is not None:
                    last_ts, last_id = cursor
                    page_where = f"{where}{' AND' if where else ' WHERE'}"
                    page_where += " (ts_epoch, id) < (?, ?)"
                    page_args.extend([last_ts, last_id])
                page_sql = (
                    f"SELECT {', '.join(columns)} FROM requests{page_where}"
                    " ORDER BY ts_epoch DESC, id DESC LIMIT ?"
                )
                rows = conn.execute(page_sql, [*page_args, page_size]).fetchall()
                if not rows:
                    return
                ids = [str(row["id"]) for row in rows]
                bodies = self._fetch_bodies(conn, ids) if need_bodies else {}
                # One batched read of the attempt side per page, not per row:
                # ``request_attempts`` is joined by no export path, so the
                # ladder columns would otherwise be unexportable entirely.
                ladders = self._fetch_ladder_rollup(conn, ids) if need_ladder else {}
                for row in rows:
                    data = self._row_to_dict(
                        row,
                        body_preview_chars=None,
                        bodies=bodies.get(str(row["id"])),
                    )
                    if need_ladder:
                        data.update(_EMPTY_LADDER_ROLLUP)
                        data.update(ladders.get(str(row["id"]), {}))
                    yield data
                cursor = (rows[-1]["ts_epoch"], rows[-1]["id"])
        finally:
            conn.close()

    def iter_export_aggregates(
        self,
        *,
        select: str,
        names: list[str],
        group_by: list[str],
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
        endpoint: str | None = None,
        key: str | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
        local: str | None = None,
        harness: str | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield the aggregated (grouped) records for an export.

        ``select``/``names`` come from ``core.export.request_aggregate_sql``;
        ``group_by`` is the ordered dimension list, which becomes both GROUP BY
        and ORDER BY so the output is deterministically grouped.
        """
        where, args = self._where(
            provider=provider,
            model=model,
            status=status,
            endpoint=endpoint,
            key=key,
            since=since,
            until=until,
            q=q,
            local=local,
            harness=harness,
        )
        group_sql = ", ".join(group_by)
        order_sql = ", ".join(group_by)
        sql = (
            f"SELECT {select} FROM requests{where}"
            f" GROUP BY {group_sql} ORDER BY {order_sql}"
        )
        with self._connection() as conn:
            for row in conn.execute(sql, args).fetchall():
                yield {name: row[name] for name in names}

    def _fetch_bodies(
        self, conn: sqlite3.Connection, ids: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Decompress the stored text for one page of rows.

        Looked up by id rather than joined: the page is at most a few hundred
        rows, and a join would let SQLite decide to decompress far more of the
        table than the page actually needs.
        """
        if not ids or not self._compress_bodies:
            return {}
        placeholders = ", ".join("?" * len(ids))
        found = conn.execute(
            "SELECT r.request_id, r.sha, r.input_sha,"
            " br.dict_id AS rest_dict, br.payload AS rest_payload,"
            " bi.dict_id AS input_dict, bi.payload AS input_payload"
            " FROM request_bodies r"
            " LEFT JOIN body_blobs br ON br.sha = r.sha"
            " LEFT JOIN body_blobs bi ON bi.sha = r.input_sha"
            f" WHERE r.request_id IN ({placeholders})",
            ids,
        ).fetchall()
        # Many requests share a blob after dedup, so decode each distinct one
        # once rather than once per request pointing at it.
        decoded: dict[str, dict[str, Any]] = {}
        result: dict[str, dict[str, Any]] = {}
        for row in found:
            merged: dict[str, Any] = {}
            for sha, payload, dict_id in (
                (row["sha"], row["rest_payload"], row["rest_dict"]),
                (row["input_sha"], row["input_payload"], row["input_dict"]),
            ):
                if sha is None or payload is None:
                    continue
                key = str(sha)
                if key not in decoded:
                    decoded[key] = self._decode_bodies(payload, dict_id)
                # The prompt blob is applied last so it wins, but a blob
                # written before the split still carries its own prompt and
                # there is no second blob to override it.
                merged.update(decoded[key])
            result[str(row["request_id"])] = merged
        return result

    @staticmethod
    def _row_to_dict(
        row: sqlite3.Row,
        *,
        body_preview_chars: int | None,
        bodies: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = dict(row)
        data["stream"] = bool(data["stream"])
        if bodies:
            # Only fill columns this query actually projected: list views carry
            # ``thinking_chars`` instead of ``thinking_text`` and must keep
            # their shape.
            for key in ("input_text", "output_text", "thinking_text"):
                if key not in data:
                    continue
                value = bodies.get(key)
                if value is not None:
                    data[key] = value
                    # The SQL-side length belongs to the (now empty) column;
                    # truncation is recomputed from the real text below.
                    data.pop(f"{key}_length", None)
            if "tool_calls" in data and bodies.get("tool_calls") is not None:
                data["tool_calls"] = bodies["tool_calls"]
        # ``thinking_text`` is only projected by the detail query; list views
        # carry ``thinking_chars`` instead, so skip whatever is absent.
        body_keys = [
            key
            for key in ("input_text", "output_text", "thinking_text")
            if key in data or f"{key}_length" in data
        ]
        for key in body_keys:
            # List queries project a SQL-side preview plus the untruncated
            # length, so the full body never reaches Python.
            length = data.pop(f"{key}_length", None)
            if length is not None:
                data[f"{key}_truncated"] = (
                    body_preview_chars is not None and int(length) > body_preview_chars
                )
                continue
            text = data.get(key)
            if (
                body_preview_chars is not None
                and isinstance(text, str)
                and len(text) > body_preview_chars
            ):
                data[key] = text[:body_preview_chars]
                data[f"{key}_truncated"] = True
            else:
                data[f"{key}_truncated"] = False
        for key in ("params", "headers", "tool_calls"):
            raw = data.get(key)
            if isinstance(raw, str):
                try:
                    data[key] = json.loads(raw)
                except json.JSONDecodeError:
                    data[key] = None
        return data

    # ------------------------------------------------------------------ stats

    def stats(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
        endpoint: str | None = None,
        key: str | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
        local: str | None = None,
        harness: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate analytics, served from the rollup where it can be.

        The payload carries ``served_from``: ``"rollup"`` when the whole answer
        came from the pre-aggregated tables, ``"rows"`` when it was computed by
        scanning ``requests``. Free-text search is the only filter that forces
        the scan today -- it is a correlated EXISTS over compressed bodies and
        is not a rollup dimension -- along with the window before the one-time
        backfill has finished.
        """
        # ``local`` belongs in the key: without it a "hide" call inside the TTL
        # would be served the "all" numbers it just cached, and the cards would
        # contradict the table.
        cache_key = (
            provider,
            model,
            status,
            endpoint,
            key,
            since,
            until,
            q,
            local,
            harness,
        )
        now = time.monotonic()
        with self._stats_lock:
            cached = self._stats_cache.get(cache_key)
            if cached is not None:
                if now - cached[0] < _STATS_CACHE_TTL_SECONDS:
                    self._stats_cache.move_to_end(cache_key)
                    return dict(cached[1])
                # Expired: drop it now rather than waiting for LRU eviction to
                # get around to it.
                del self._stats_cache[cache_key]
        payload: dict[str, Any] | None = None
        if not q:
            payload = self._stats_from_rollup(
                provider=provider,
                model=model,
                status=status,
                endpoint=endpoint,
                key=key,
                since=since,
                until=until,
                local=local,
                harness=harness,
            )
        if payload is None:
            payload = self._stats_from_rows(
                provider=provider,
                model=model,
                status=status,
                endpoint=endpoint,
                key=key,
                since=since,
                until=until,
                q=q,
                local=local,
                harness=harness,
            )
        with self._stats_lock:
            self._stats_cache[cache_key] = (now, payload)
            self._stats_cache.move_to_end(cache_key)
            while len(self._stats_cache) > _STATS_CACHE_MAX_ENTRIES:
                self._stats_cache.popitem(last=False)
        return dict(payload)

    def _stats_from_rows(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
        endpoint: str | None = None,
        key: str | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
        local: str | None = None,
        harness: str | None = None,
    ) -> dict[str, Any]:
        """Compute the whole payload by scanning ``requests``.

        This is not test-only scaffolding and must not be deleted. It is the
        live path for a free-text search, the live path until the one-time
        rollup backfill finishes, and the oracle the rollup's equality contract
        test is asserted against. It is also the only path that computes exact
        percentiles rather than interpolating a histogram.
        """
        where, args = self._where(
            provider=provider,
            model=model,
            status=status,
            endpoint=endpoint,
            key=key,
            since=since,
            until=until,
            q=q,
            local=local,
            harness=harness,
        )
        with self._connection() as conn:
            totals = conn.execute(
                f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) AS success,
                       SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS error,
                       SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) AS cancelled,
                       COALESCE(SUM(tokens_in), 0) AS tokens_in,
                       COALESCE(SUM(tokens_out), 0) AS tokens_out,
                       COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens,
                       COALESCE(SUM(cache_write_tokens), 0) AS cache_write_tokens,
                       SUM(CASE WHEN cache_read_tokens IS NOT NULL THEN 1 ELSE 0 END)
                           AS cache_reported,
                       COALESCE(SUM(tool_call_count), 0) AS tool_calls,
                       SUM(CASE WHEN tool_call_count > 0 THEN 1 ELSE 0 END)
                           AS turns_with_tools,
                       SUM(CASE WHEN thinking_chars > 0 THEN 1 ELSE 0 END)
                           AS turns_with_reasoning,
                       SUM(CASE WHEN route_attempt > 0 THEN 1 ELSE 0 END)
                           AS served_by_fallback,
                       SUM(CASE WHEN route_attempt IS NOT NULL THEN 1 ELSE 0 END)
                           AS route_reported,
                       SUM(CASE WHEN route_diverted_from IS NOT NULL THEN 1 ELSE 0 END)
                           AS diverted,
                       SUM(CASE WHEN route_diversion = 'vision_unavailable'
                           THEN 1 ELSE 0 END) AS vision_unavailable,
                       SUM(CASE WHEN route_diversion = 'vision_described'
                           THEN 1 ELSE 0 END) AS vision_described,
                       SUM(CASE WHEN input_image_count > 0 THEN 1 ELSE 0 END)
                           AS with_images,
                       AVG(duration_ms) AS avg_duration_ms,
                       AVG(ttft_ms) AS avg_ttft_ms
                FROM requests{where}
                """,
                args,
            ).fetchone()
            percentiles = self._percentiles(conn, where, args, (0.50, 0.95))
            by_provider, by_provider_truncated = self._breakdown(
                conn, "provider", where, args, key_sql=PROVIDER_KEY_SQL
            )
            by_model, by_model_truncated = self._breakdown(
                conn, "resolved_model", where, args
            )
            by_key, by_key_truncated = self._breakdown(conn, "key_label", where, args)
            by_harness, by_harness_truncated = self._breakdown(
                conn, "harness", where, args
            )
            top_errors = [
                {"message": row[0], "count": row[1]}
                for row in conn.execute(
                    f"SELECT error_message, COUNT(*) FROM requests{where}"
                    f"{' AND' if where else ' WHERE'} status='error'"
                    " AND error_message IS NOT NULL"
                    " GROUP BY error_message ORDER BY COUNT(*) DESC LIMIT 10",
                    args,
                ).fetchall()
            ]
            fallback_routes = [
                {
                    "primary": row[0],
                    "served_by": row[1],
                    "count": row[2],
                }
                for row in conn.execute(
                    f"SELECT route_primary_model,"
                    " COALESCE(provider, '(unknown)') || '/' ||"
                    " COALESCE(resolved_model, '(unknown)'), COUNT(*)"
                    f" FROM requests{where}"
                    f"{' AND' if where else ' WHERE'} route_attempt > 0"
                    " AND route_primary_model IS NOT NULL"
                    " GROUP BY 1, 2 ORDER BY COUNT(*) DESC LIMIT 10",
                    args,
                ).fetchall()
            ]
            diverted_routes = [
                {
                    "diverted_from": row[0],
                    "reason": row[1],
                    "served_by": row[2],
                    "count": row[3],
                }
                for row in conn.execute(
                    f"SELECT route_diverted_from, route_diversion,"
                    " COALESCE(provider, '(unknown)') || '/' ||"
                    " COALESCE(resolved_model, '(unknown)'), COUNT(*)"
                    f" FROM requests{where}"
                    f"{' AND' if where else ' WHERE'} route_diversion IS NOT NULL"
                    " AND route_diverted_from IS NOT NULL"
                    " GROUP BY 1, 2, 3 ORDER BY COUNT(*) DESC LIMIT 10",
                    args,
                ).fetchall()
            ]
            try:
                recovery = conn.execute(
                    "SELECT"
                    " COALESCE(SUM(json_extract(a.params, '$.early_retries')), 0),"
                    " COALESCE(SUM(json_extract(a.params,"
                    " '$.midstream_recoveries')), 0),"
                    " COALESCE(SUM(json_extract(a.params, '$.salvages')), 0)"
                    " FROM request_attempts AS a"
                    " WHERE a.request_id IN (SELECT id FROM requests"
                    f"{where})",
                    args,
                ).fetchone()
            except sqlite3.Error as exc:
                # One unreadable params value must not take the analytics
                # page down; report nothing measured instead.
                logger.warning("Request log recovery aggregate skipped: {}", exc)
                recovery = (0, 0, 0)
            try:
                # Count by the status the upstream actually returned, not by
                # the one mapped kind that survived the ladder: a request that
                # saw twelve 429s before a 502 used to be counted once, as
                # ``upstream``. ``ladder_tries > 1`` is what the denormalised
                # column exists for -- it keeps the JSON scan off the ~95% of
                # attempt rows that never retried.
                upstream_statuses = [
                    {
                        "status": int(row[0]),
                        "count": int(row[1]),
                        "requests": int(row[2]),
                    }
                    for row in conn.execute(
                        "SELECT json_extract(t.value, '$.status'), COUNT(*),"
                        " COUNT(DISTINCT a.request_id)"
                        " FROM request_attempts AS a,"
                        " json_each(json_extract(a.params, '$.ladder.tries'))"
                        " AS t"
                        " WHERE a.ladder_tries > 1"
                        " AND a.request_id IN (SELECT id FROM requests"
                        f"{where})"
                        " AND json_extract(t.value, '$.status') IS NOT NULL"
                        " GROUP BY 1 ORDER BY 2 DESC LIMIT 12",
                        args,
                    ).fetchall()
                ]
            except sqlite3.Error as exc:
                logger.warning("Request log upstream status breakdown skipped: {}", exc)
                upstream_statuses = []
            series = self._series(conn, where, args, since=since, until=until)

        total = totals["total"] or 0
        payload = {
            # A raw scan honours the window exactly, so the snapped bounds it
            # reports are the requested ones. Only the rollup rounds outward.
            "window": {
                "since": since,
                "until": until,
                "snapped_since": since,
                "snapped_until": until,
            },
            "total": total,
            "success": totals["success"] or 0,
            "error": totals["error"] or 0,
            "cancelled": totals["cancelled"] or 0,
            "error_rate": (totals["error"] or 0) / total if total else 0.0,
            "tokens_in": totals["tokens_in"] or 0,
            "tokens_out": totals["tokens_out"] or 0,
            "cache_read_tokens": totals["cache_read_tokens"] or 0,
            "cache_write_tokens": totals["cache_write_tokens"] or 0,
            "cache_reported": totals["cache_reported"] or 0,
            "tool_calls": totals["tool_calls"] or 0,
            "turns_with_tools": totals["turns_with_tools"] or 0,
            "turns_with_reasoning": totals["turns_with_reasoning"] or 0,
            # ``route_reported`` separates "no fallback was used" from "these
            # rows predate fallback chains", so the UI can show a dash rather
            # than a reassuring 0% for traffic it knows nothing about.
            "served_by_fallback": totals["served_by_fallback"] or 0,
            "route_reported": totals["route_reported"] or 0,
            "fallback_routes": fallback_routes,
            "diverted": totals["diverted"] or 0,
            "diverted_routes": diverted_routes,
            # Stream recovery summed over every attempt in the window. A zero
            # is a real measured zero; rows written before recovery was
            # recorded carry nothing to sum and do not drag it down.
            "recovery": {
                "early_retries": int(recovery[0]),
                "midstream_recoveries": int(recovery[1]),
                "salvages": int(recovery[2]),
            },
            # Requests that carried an image or a document, whether or not the
            # route had to divert: a vision-capable primary needs no diversion
            # and still received a picture.
            "with_images": totals["with_images"] or 0,
            # An image arrived and no model on the route could read it, so
            # nothing was diverted and the request went out anyway. Counted
            # apart from ``diverted``: one is the safety net working, the
            # other is the safety net having nowhere to put the request.
            "vision_unavailable": totals["vision_unavailable"] or 0,
            # An image was replaced by a description a second model wrote and
            # the route's own model answered as usual. Counted apart from
            # ``diverted``: nothing moved, the picture became words.
            "vision_described": totals["vision_described"] or 0,
            "avg_duration_ms": _rounded(totals["avg_duration_ms"]),
            "p50_duration_ms": _rounded(percentiles[0.50]),
            "p95_duration_ms": _rounded(percentiles[0.95]),
            "avg_ttft_ms": _rounded(totals["avg_ttft_ms"]),
            "by_provider": by_provider,
            "by_provider_truncated": by_provider_truncated,
            "by_model": by_model,
            "by_model_truncated": by_model_truncated,
            "by_key": by_key,
            "by_key_truncated": by_key_truncated,
            # Which coding agent sent the traffic. ``(unknown)`` here is a row
            # the backfill has not reached yet, not a client we failed to
            # recognise -- an unrecognised client is stored as ``unknown``.
            "by_harness": by_harness,
            "by_harness_truncated": by_harness_truncated,
            "series": series,
            "top_errors": top_errors,
            # Every upstream status behind the recorded attempts, not just the
            # one that ended each of them. Empty on a database whose rows all
            # predate the ladder: nothing was measured, so nothing is claimed.
            "upstream_statuses": upstream_statuses,
            "served_from": "rows",
        }
        return payload

    @staticmethod
    def _rollup_where(
        *,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
        endpoint: str | None = None,
        key: str | None = None,
        since: float | None = None,
        until: float | None = None,
        local: str | None = None,
        harness: str | None = None,
    ) -> tuple[str, list[Any]]:
        """``_where`` translated onto the rollup's dimension columns.

        Clause for clause the same predicate, with two differences forced by
        the storage: SQL NULL is the empty string here, and the time bounds are
        snapped outward to the UTC hour because one hour is the finest grain
        the rollup has. ``q`` has no translation at all -- it is why the caller
        falls back to a raw scan.
        """
        clauses: list[str] = []
        args: list[Any] = []
        if local == "hide":
            clauses.append("is_local = 0")
        elif local == "only":
            clauses.append("is_local = 1")
        if provider:
            providers = [part for part in provider.split(",") if part]
            if providers:
                named = [
                    part
                    for part in providers
                    if not part.startswith(LOCAL_PROVIDER_PREFIX)
                    and part != UNKNOWN_PROVIDER_KEY
                ]
                alternatives: list[str] = []
                named_args: list[Any] = []
                local_args: list[Any] = []
                if named:
                    placeholders = ",".join("?" * len(named))
                    alternatives.append(f"provider IN ({placeholders})")
                    named_args.extend(named)
                for part in providers:
                    if part.startswith(LOCAL_PROVIDER_PREFIX):
                        alternatives.append("(provider = '' AND optimization = ?)")
                        local_args.append(part[len(LOCAL_PROVIDER_PREFIX) :])
                    elif part == UNKNOWN_PROVIDER_KEY:
                        alternatives.append("(provider = '' AND optimization = '')")
                clauses.append(f"({' OR '.join(alternatives)})")
                args.extend(named_args)
                args.extend(local_args)
        if harness:
            # Comma-separated values mean "any of these harnesses", mirroring
            # the model filter rather than the single-valued ones: what a
            # reader filters by is a row of the ``by_harness`` breakdown, and
            # comparing two agents is the question that breakdown invites.
            harnesses = [part for part in harness.split(",") if part]
            if harnesses:
                placeholders = ",".join("?" * len(harnesses))
                clauses.append(f"harness IN ({placeholders})")
                args.extend(harnesses)
        if key:
            clauses.append("key_label = ?")
            args.append(key)
        if model:
            models = [part for part in model.split(",") if part]
            if models:
                placeholders = ",".join("?" * len(models))
                clauses.append(
                    f"(resolved_model IN ({placeholders})"
                    f" OR requested_model IN ({placeholders}))"
                )
                args.extend(models)
                args.extend(models)
        if status:
            clauses.append("status = ?")
            args.append(status)
        if endpoint:
            clauses.append("endpoint = ?")
            args.append(endpoint)
        if since is not None:
            clauses.append("hour_epoch >= ?")
            args.append(_floor_hour(since))
        if until is not None:
            # ``<=`` against the floored hour keeps the whole hour containing
            # ``until``, which is the outward half of the snap.
            clauses.append("hour_epoch <= ?")
            args.append(_floor_hour(until))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, args

    def _stats_from_rollup(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
        endpoint: str | None = None,
        key: str | None = None,
        since: float | None = None,
        until: float | None = None,
        local: str | None = None,
        harness: str | None = None,
    ) -> dict[str, Any] | None:
        """Compute the whole payload from the rollup, or None if it cannot.

        Returns None -- rather than a partial answer -- when the one-time
        backfill has not finished, so a half-built rollup is never read.
        """
        where, args = self._rollup_where(
            provider=provider,
            model=model,
            status=status,
            endpoint=endpoint,
            key=key,
            since=since,
            until=until,
            local=local,
            harness=harness,
        )
        sums = ", ".join(f"COALESCE(SUM({name}), 0)" for name in _ROLLUP_COUNTER_NAMES)
        with self._connection() as conn:
            if self._meta_get(conn, _ROLLUP_BACKFILL_KEY) is None:
                return None
            row = conn.execute(
                "SELECT"
                " SUM(CASE WHEN status = 'success' THEN requests ELSE 0 END),"
                " SUM(CASE WHEN status = 'error' THEN requests ELSE 0 END),"
                " SUM(CASE WHEN status = 'cancelled' THEN requests ELSE 0 END),"
                f" {sums}"
                f" FROM request_stats_rollup{where}",
                args,
            ).fetchone()
            counters = dict(zip(_ROLLUP_COUNTER_NAMES, list(row)[3:], strict=True))
            percentiles = self._percentiles_from_histogram(
                conn, where, args, (0.50, 0.95)
            )
            by_provider, by_provider_truncated = self._rollup_breakdown(
                conn, ROLLUP_PROVIDER_KEY_SQL, where, args
            )
            by_model, by_model_truncated = self._rollup_breakdown(
                conn, self._rollup_key_sql("resolved_model"), where, args
            )
            by_key, by_key_truncated = self._rollup_breakdown(
                conn, self._rollup_key_sql("key_label"), where, args
            )
            by_harness, by_harness_truncated = self._rollup_breakdown(
                conn, self._rollup_key_sql("harness"), where, args
            )
            connector = " AND" if where else " WHERE"
            top_errors = [
                {"message": detail[0], "count": detail[1]}
                for detail in conn.execute(
                    "SELECT a, SUM(count) FROM request_stats_detail"
                    f"{where}{connector} kind = '{_DETAIL_ERROR}'"
                    " GROUP BY a ORDER BY 2 DESC LIMIT 10",
                    args,
                ).fetchall()
            ]
            fallback_routes = [
                {
                    "primary": detail[0],
                    "served_by": detail[1],
                    "count": detail[2],
                }
                for detail in conn.execute(
                    "SELECT a, b, SUM(count) FROM request_stats_detail"
                    f"{where}{connector} kind = '{_DETAIL_FALLBACK}'"
                    " GROUP BY a, b ORDER BY 3 DESC LIMIT 10",
                    args,
                ).fetchall()
            ]
            diverted_routes = [
                {
                    "diverted_from": detail[0],
                    "reason": detail[1],
                    "served_by": detail[2],
                    "count": detail[3],
                }
                for detail in conn.execute(
                    "SELECT a, b, c, SUM(count) FROM request_stats_detail"
                    f"{where}{connector} kind = '{_DETAIL_DIVERTED}'"
                    " GROUP BY a, b, c ORDER BY 4 DESC LIMIT 10",
                    args,
                ).fetchall()
            ]
            upstream_statuses = [
                {
                    "status": int(detail[0]),
                    "count": int(detail[1]),
                    "requests": int(detail[2]),
                }
                for detail in conn.execute(
                    "SELECT a, SUM(count), SUM(requests)"
                    " FROM request_stats_detail"
                    f"{where}{connector} kind = '{_DETAIL_UPSTREAM}'"
                    " GROUP BY a ORDER BY 2 DESC LIMIT 12",
                    args,
                ).fetchall()
            ]
            series = self._series_from_rollup(
                conn, where, args, since=since, until=until
            )

        total = int(counters["requests"])
        errors = int(row[1] or 0)
        return {
            # Both bounds are reported: what was asked for, and the UTC-hour
            # window actually summed. They differ only when the request was not
            # hour-aligned, and that difference is real -- on the measured log a
            # 24 h p95 moves 20% between the two -- so it is stated rather than
            # smoothed over.
            "window": {
                "since": since,
                "until": until,
                "snapped_since": None if since is None else _floor_hour(since),
                "snapped_until": (
                    None if until is None else _floor_hour(until) + _HOUR_SECONDS - 1
                ),
            },
            "total": total,
            "success": int(row[0] or 0),
            "error": errors,
            "cancelled": int(row[2] or 0),
            "error_rate": errors / total if total else 0.0,
            "tokens_in": int(counters["tokens_in"]),
            "tokens_out": int(counters["tokens_out"]),
            "cache_read_tokens": int(counters["cache_read_tokens"]),
            "cache_write_tokens": int(counters["cache_write_tokens"]),
            "cache_reported": int(counters["cache_reported"]),
            "tool_calls": int(counters["tool_calls"]),
            "turns_with_tools": int(counters["turns_with_tools"]),
            "turns_with_reasoning": int(counters["turns_with_reasoning"]),
            "served_by_fallback": int(counters["served_by_fallback"]),
            "route_reported": int(counters["route_reported"]),
            "fallback_routes": fallback_routes,
            "diverted": int(counters["diverted"]),
            "diverted_routes": diverted_routes,
            "recovery": {
                name: int(counters[name]) for name in _ROLLUP_RECOVERY_COUNTERS
            },
            "with_images": int(counters["with_images"]),
            "vision_unavailable": int(counters["vision_unavailable"]),
            "vision_described": int(counters["vision_described"]),
            "avg_duration_ms": _rounded(
                _mean(counters["duration_sum"], counters["duration_count"])
            ),
            "p50_duration_ms": _rounded(percentiles[0.50]),
            "p95_duration_ms": _rounded(percentiles[0.95]),
            "avg_ttft_ms": _rounded(
                _mean(counters["ttft_sum"], counters["ttft_count"])
            ),
            "by_provider": by_provider,
            "by_provider_truncated": by_provider_truncated,
            "by_model": by_model,
            "by_model_truncated": by_model_truncated,
            "by_key": by_key,
            "by_key_truncated": by_key_truncated,
            "by_harness": by_harness,
            "by_harness_truncated": by_harness_truncated,
            "series": series,
            "top_errors": top_errors,
            "upstream_statuses": upstream_statuses,
            "served_from": "rollup",
        }

    @staticmethod
    def _rollup_key_sql(column: str) -> str:
        """Rollup mirror of ``_breakdown``'s ``COALESCE(column, '(unknown)')``."""
        return (
            f"CASE WHEN {column} <> '' THEN {column} ELSE '{UNKNOWN_PROVIDER_KEY}' END"
        )

    @staticmethod
    def _rollup_breakdown(
        conn: sqlite3.Connection,
        key_sql: str,
        where: str,
        args: list[Any],
    ) -> tuple[list[dict[str, Any]], bool]:
        """``_breakdown`` against the rollup, same shape and same cap.

        The fetch-one-past-the-cap trick is kept verbatim so
        ``by_*_truncated`` keeps meaning exactly what it meant before.
        """
        rows = conn.execute(
            f"SELECT {key_sql} AS key, SUM(requests) AS requests,"
            " SUM(tokens_in), SUM(tokens_out),"
            " SUM(cache_read_tokens), SUM(cache_write_tokens),"
            " SUM(cache_reported),"
            " SUM(CASE WHEN status = 'error' THEN requests ELSE 0 END),"
            " SUM(duration_sum), SUM(duration_count)"
            f" FROM request_stats_rollup{where}"
            " GROUP BY key ORDER BY requests DESC LIMIT ?",
            [*args, _BREAKDOWN_LIMIT + 1],
        ).fetchall()
        truncated = len(rows) > _BREAKDOWN_LIMIT
        rows = rows[:_BREAKDOWN_LIMIT]
        return [
            {
                "key": row[0],
                "requests": int(row[1] or 0),
                "tokens_in": int(row[2] or 0),
                "tokens_out": int(row[3] or 0),
                "cache_read_tokens": int(row[4] or 0),
                "cache_write_tokens": int(row[5] or 0),
                "cache_reported": int(row[6] or 0),
                "errors": int(row[7] or 0),
                "avg_duration_ms": _rounded(_mean(row[8], row[9])),
            }
            for row in rows
        ], truncated

    @staticmethod
    def _percentiles_from_histogram(
        conn: sqlite3.Connection,
        where: str,
        args: list[Any],
        fractions: tuple[float, ...],
    ) -> dict[float, float | None]:
        """Interpolate percentiles out of the stored latency histogram.

        At most 64 rows are read however large the window, against the
        quarter-million floats ``_percentiles`` pulls into Python on an
        all-time call. The rank formula is the one ``_percentiles`` uses, so
        the two agree in shape; the difference is that the position inside the
        chosen bucket is interpolated across the bucket's edges rather than
        between two real observations. Measured error against the exact value
        on the real log: <= 2.3% on every all-time percentile.
        """
        buckets = [
            (int(row[0]), int(row[1] or 0))
            for row in conn.execute(
                "SELECT bucket, SUM(count) FROM request_stats_latency"
                f"{where} GROUP BY bucket ORDER BY bucket",
                args,
            ).fetchall()
        ]
        total = sum(count for _bucket, count in buckets)
        if not total:
            return dict.fromkeys(fractions)
        results: dict[float, float | None] = {}
        for fraction in fractions:
            target = min(float(total - 1), max(0.0, fraction * (total - 1)))
            seen = 0
            value: float | None = None
            for bucket, count in buckets:
                if seen + count > target:
                    low, high = _latency_bucket_edges(bucket)
                    value = low + (high - low) * ((target - seen) / count)
                    break
                seen += count
            if value is None:
                value = _latency_bucket_edges(buckets[-1][0])[1]
            results[fraction] = value
        return results

    @staticmethod
    def _series_from_rollup(
        conn: sqlite3.Connection,
        where: str,
        args: list[Any],
        *,
        since: float | None,
        until: float | None,
    ) -> list[dict[str, Any]]:
        """``_series`` against the rollup.

        Hour grain is exact for both formats the series uses: an hourly bucket
        is one rollup row's key, and a UTC day is a whole number of UTC hours.
        The bounds probe reads thousands of rows instead of hundreds of
        thousands.
        """
        bounds = conn.execute(
            f"SELECT MIN(hour_epoch), MAX(hour_epoch) FROM request_stats_rollup{where}",
            args,
        ).fetchone()
        low = since if since is not None else bounds[0]
        # The last bucket covers a whole hour, so the span the rollup can see
        # ends at that hour's end -- the same outward rounding the window uses.
        high = until
        if high is None and bounds[1] is not None:
            high = bounds[1] + _HOUR_SECONDS - 1
        hourly = low is not None and high is not None and (high - low) < 48 * 3600
        fmt = "%Y-%m-%dT%H:00" if hourly else "%Y-%m-%d"
        cursor = conn.execute(
            "SELECT strftime(?, hour_epoch, 'unixepoch') AS bucket,"
            " SUM(requests),"
            " COALESCE(SUM(tokens_in), 0) + COALESCE(SUM(tokens_out), 0),"
            " SUM(CASE WHEN status = 'error' THEN requests ELSE 0 END)"
            f" FROM request_stats_rollup{where} GROUP BY bucket ORDER BY bucket",
            [fmt, *args],
        )
        return [
            {
                "bucket": row[0],
                "requests": int(row[1] or 0),
                "tokens": int(row[2] or 0),
                "errors": int(row[3] or 0),
            }
            for row in cursor.fetchall()
            if row[0] is not None
        ]

    @staticmethod
    def _image_estimate_sql(conn: sqlite3.Connection, since_clause: str) -> str:
        """The per-host image-estimate query, pinned to its own index.

        The predicate this question is really about -- ``est_image_tokens IS
        NOT NULL`` -- is true on 2.7% of the log, and ``status = 'success'`` is
        true on 99.2%. With no ``ANALYZE`` statistics SQLite cannot know that,
        so it picked ``idx_requests_status`` and walked almost the whole table:
        measured 1.051 s on a 4.5 GB copy. ``INDEXED BY`` hands it the partial
        index instead, which is index-only over 8,867 entries: **0.002 s, the
        same rows** (verified by comparing the result sets).

        ``ANALYZE`` is not the alternative. Measured on the same copy it took
        2.2 s and moved this plan onto ``idx_requests_provider``, which was
        slower than doing nothing.

        The hint is dropped when the index is not there. ``INDEXED BY`` is an
        error, not a preference, if the named index is missing -- and it is
        created by a writer-thread migration that a very early read can race,
        so the unhinted query stays as the fallback and answers identically.
        """
        hint = ""
        with contextlib.suppress(sqlite3.Error):
            present = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
                ("idx_requests_image_v1",),
            ).fetchone()
            if present is not None:
                hint = " INDEXED BY idx_requests_image_v1"
        return (
            "SELECT provider AS provider,"
            " COUNT(*) AS requests,"
            " SUM(COALESCE(tokens_in, 0)) AS billed_tokens_in,"
            " SUM(COALESCE(est_tokens_in, 0)) AS est_tokens_in,"
            " SUM(COALESCE(est_image_tokens, 0)) AS est_image_tokens,"
            " SUM(COALESCE(input_image_count, 0)) AS images"
            f" FROM requests{hint}"
            " WHERE est_image_tokens IS NOT NULL"
            " AND provider IS NOT NULL"
            " AND status = 'success'"
            " AND COALESCE(cache_read_tokens, 0) = 0"
            f"{since_clause}"
            " GROUP BY provider"
            " ORDER BY requests DESC"
            " LIMIT ?"
        )

    def image_estimate_by_provider(
        self, *, since: float | None = None, limit: int = _BREAKDOWN_LIMIT
    ) -> list[dict[str, Any]]:
        """Per host: what images were estimated to cost, against what was billed.

        The question this exists to answer could not be asked before 6.53.0,
        because the proxy had never stored what it estimated -- 275,304 logged
        requests and not one recorded prediction, which is why a measured
        tokens-per-pixel table was impossible to produce from the log. Now the
        ratio of billed to estimated is a number per host: near 1.0 means that
        host's declared ``image_token_family`` is right, and a host far from it
        has either the wrong family or a formula nobody publishes.

        Restricted to rows that carried an estimate at all, and to uncached
        ones: prompt caching moves an image's cost into ``cache_read_tokens``,
        and comparing an estimate against a bill the cache already paid would
        read as a wildly over-confident estimator. Only successful requests
        count -- a failed one was billed for nothing.
        """

        cache_key = ("image_estimate_by_provider", since, limit)
        now = time.monotonic()
        with self._stats_lock:
            cached = self._stats_cache.get(cache_key)
            if cached is not None:
                if now - cached[0] < _STATS_CACHE_TTL_SECONDS:
                    self._stats_cache.move_to_end(cache_key)
                    return [dict(row) for row in cached[1]["rows"]]
                del self._stats_cache[cache_key]
        since_clause = "" if since is None else " AND ts_epoch >= ?"
        args: list[Any] = [] if since is None else [since]
        args.append(limit)
        rows: list[dict[str, Any]] = []
        try:
            with self._connection() as conn:
                rows = [
                    dict(row)
                    for row in conn.execute(
                        self._image_estimate_sql(conn, since_clause), args
                    ).fetchall()
                ]
        except sqlite3.Error as exc:
            # A page that cannot measure still renders. This is a readout, not
            # a control: an unavailable log means "no measurement", never an
            # error banner over the model tree.
            logger.warning("Image estimate breakdown unavailable: {}", exc)
            return []
        with self._stats_lock:
            self._stats_cache[cache_key] = (now, {"rows": [dict(r) for r in rows]})
            self._stats_cache.move_to_end(cache_key)
            while len(self._stats_cache) > _STATS_CACHE_MAX_ENTRIES:
                self._stats_cache.popitem(last=False)
        return rows

    def reasoning_by_model(
        self, *, since: float | None = None, limit: int = _BREAKDOWN_LIMIT
    ) -> list[dict[str, Any]]:
        """Per model: how often reasoning was asked for, and how often it came back.

        Requested is what the outbound body carried (``reasoning_emitted``);
        returned is whether the reply contained any thinking text. They are
        independent, and every "does this model actually think" question is one
        of the four combinations -- a model asked three times that never
        thought, and one never asked that thought every time, are both real and
        both invisible until the two are counted side by side. Only succeeded
        attempts count: a model that failed answered nothing either way.

        ``unmeasured`` is the attempts whose ``reasoning_emitted`` is NULL,
        kept apart from a measured zero so the caller never reports "asked 0
        times" for a provider with no instrumented commit boundary.
        """

        cache_key = ("reasoning_by_model", since, limit)
        now = time.monotonic()
        with self._stats_lock:
            cached = self._stats_cache.get(cache_key)
            if cached is not None:
                if now - cached[0] < _STATS_CACHE_TTL_SECONDS:
                    self._stats_cache.move_to_end(cache_key)
                    return [dict(row) for row in cached[1]["rows"]]
                del self._stats_cache[cache_key]
        since_clause = "" if since is None else " AND r.ts_epoch >= ?"
        args: list[Any] = [] if since is None else [since]
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT a.model_ref AS model_ref,"
                " COUNT(*) AS attempts,"
                " SUM(CASE WHEN a.reasoning_emitted = 1 THEN 1 ELSE 0 END)"
                " AS requested,"
                " SUM(CASE WHEN a.reasoning_emitted IS NULL THEN 1 ELSE 0 END)"
                " AS unmeasured,"
                " SUM(CASE WHEN COALESCE(r.thinking_chars, 0) > 0 THEN 1 ELSE 0 END)"
                " AS returned"
                " FROM request_attempts a"
                " JOIN requests r ON r.id = a.request_id"
                " WHERE a.outcome = 'succeeded' AND a.model_ref IS NOT NULL"
                f"{since_clause}"
                " GROUP BY a.model_ref"
                " ORDER BY attempts DESC"
                " LIMIT ?",
                [*args, limit],
            ).fetchall()
        payload = [
            {
                "model_ref": row["model_ref"],
                "attempts": int(row["attempts"] or 0),
                "requested": int(row["requested"] or 0),
                "returned": int(row["returned"] or 0),
                "unmeasured": int(row["unmeasured"] or 0),
            }
            for row in rows
        ]
        with self._stats_lock:
            # Shares ``stats()``'s cache and its 5 s TTL. The key starts with a
            # string, so it can never collide with the 8-tuple ``stats`` uses,
            # and the value is wrapped in a dict because the cache stores one.
            self._stats_cache[cache_key] = (now, {"rows": payload})
            self._stats_cache.move_to_end(cache_key)
            while len(self._stats_cache) > _STATS_CACHE_MAX_ENTRIES:
                self._stats_cache.popitem(last=False)
        return [dict(row) for row in payload]

    def optimization_stats(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        days: int = _OPTIMIZATION_SERIES_DAYS,
    ) -> dict[str, Any]:
        """Aggregate what the local optimization rules actually did.

        One row per rule that has ever fired in the window, plus a daily series
        for the sparkline the optimizer page draws and the table underneath it.

        ``tokens_saved`` is a sum of ``optimization_tokens_saved``, which rows
        written before that column existed do not carry. ``tokens_reported``
        counts the rows that did carry it, so a caller can tell "this rule saved
        nothing" from "we stopped being able to say" instead of printing a
        reassuring zero over the gap. Rules that exist but have never fired are
        not invented here: the store reports what is in the log, and the caller
        that knows the rule registry merges the rest in.
        """
        days = max(1, days)
        where, args = self._where(since=since, until=until)
        connector = " AND" if where else " WHERE"
        with self._connection() as conn:
            totals = conn.execute(
                f"SELECT COUNT(*),"
                " SUM(CASE WHEN optimization IS NOT NULL THEN 1 ELSE 0 END),"
                " COALESCE(SUM(optimization_tokens_saved), 0)"
                f" FROM requests{where}",
                args,
            ).fetchone()
            rule_rows = conn.execute(
                f"SELECT optimization AS rule, COUNT(*) AS requests,"
                " COALESCE(SUM(optimization_tokens_saved), 0) AS tokens_saved,"
                " SUM(CASE WHEN optimization_tokens_saved IS NOT NULL THEN 1 ELSE 0 END)"
                " AS tokens_reported,"
                " MIN(ts_epoch) AS first_ts, MAX(ts_epoch) AS last_ts"
                f" FROM requests{where}{connector} optimization IS NOT NULL"
                " GROUP BY rule ORDER BY requests DESC",
                args,
            ).fetchall()
            series_rows = conn.execute(
                "SELECT optimization AS rule,"
                " strftime('%Y-%m-%d', ts_epoch, 'unixepoch') AS bucket,"
                " COUNT(*) AS requests,"
                " COALESCE(SUM(optimization_tokens_saved), 0) AS tokens_saved"
                f" FROM requests{where}{connector} optimization IS NOT NULL"
                " GROUP BY rule, bucket ORDER BY bucket DESC",
                args,
            ).fetchall()

        daily: dict[str, list[dict[str, Any]]] = {}
        for row in series_rows:
            if row["bucket"] is None:
                continue
            buckets = daily.setdefault(row["rule"], [])
            if len(buckets) >= days:
                continue
            buckets.append(
                {
                    "bucket": row["bucket"],
                    "requests": int(row["requests"] or 0),
                    "tokens_saved": int(row["tokens_saved"] or 0),
                }
            )
        # Oldest first, so the sparkline reads left to right like a calendar.
        for buckets in daily.values():
            buckets.reverse()

        return {
            "window": {"since": since, "until": until},
            "series_days": days,
            "total_requests": int(totals[0] or 0),
            "answered_locally": int(totals[1] or 0),
            "tokens_saved": int(totals[2] or 0),
            "rules": [
                {
                    "rule": row["rule"],
                    "requests": int(row["requests"] or 0),
                    "tokens_saved": int(row["tokens_saved"] or 0),
                    "tokens_reported": int(row["tokens_reported"] or 0),
                    "first_ts": row["first_ts"],
                    "last_ts": row["last_ts"],
                    "daily": daily.get(row["rule"], []),
                }
                for row in rule_rows
            ],
        }

    def pulse(
        self,
        *,
        provider: str | None = None,
        model: str | None = None,
        status: str | None = None,
        endpoint: str | None = None,
        key: str | None = None,
        since: float | None = None,
        until: float | None = None,
        q: str | None = None,
        local: str | None = None,
        harness: str | None = None,
    ) -> dict[str, Any]:
        """Return a cheap heartbeat: row count and latest timestamp for these filters.

        Auto-refresh polls this instead of ``stats()``: a single COUNT/MAX query
        lets the caller detect "nothing changed" without paying for percentiles,
        breakdowns, or series buckets on every tick.
        """
        where, args = self._where(
            provider=provider,
            model=model,
            status=status,
            endpoint=endpoint,
            key=key,
            since=since,
            until=until,
            q=q,
            local=local,
            harness=harness,
        )
        with self._connection() as conn:
            total, last_ts = conn.execute(
                f"SELECT COUNT(*), MAX(ts_epoch) FROM requests{where}", args
            ).fetchone()
        return {"total": total or 0, "last_ts": last_ts}

    def harness_usage(self, *, since: float) -> dict[str, int]:
        """Requests per harness since ``since``, newest-heaviest first.

        Read off ``requests`` rather than the rollup on purpose. It answers one
        narrow question for one small card, ``idx_requests_harness_v1`` makes it
        a range seek, and a caller that cannot be wrong about a half-built
        rollup is simpler than one that has to check whether the backfill
        finished.
        """
        with self._connection() as conn:
            rows = conn.execute(
                f"SELECT COALESCE(harness, '{UNKNOWN_PROVIDER_KEY}') AS key,"
                " COUNT(*) AS requests FROM requests WHERE ts_epoch >= ?"
                " GROUP BY key ORDER BY requests DESC",
                (since,),
            ).fetchall()
        return {str(row["key"]): int(row["requests"]) for row in rows}

    @staticmethod
    def _breakdown(
        conn: sqlite3.Connection,
        column: str,
        where: str,
        args: list[Any],
        *,
        key_sql: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return (rows, truncated) for a GROUP BY breakdown, capped at ``_BREAKDOWN_LIMIT``.

        Fetches one row past the cap to detect truncation without a second
        COUNT(DISTINCT ...) query, then trims it back off before returning.

        ``key_sql`` overrides the grouping expression for a column whose NULLs
        are not all the same fact -- provider being the case that needs it.
        """
        key_expression = key_sql or f"COALESCE({column}, '{UNKNOWN_PROVIDER_KEY}')"
        cursor = conn.execute(
            f"SELECT {key_expression} AS key, COUNT(*) AS requests,"
            " COALESCE(SUM(tokens_in),0) AS tokens_in,"
            " COALESCE(SUM(tokens_out),0) AS tokens_out,"
            " COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,"
            " COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens,"
            " SUM(CASE WHEN cache_read_tokens IS NOT NULL THEN 1 ELSE 0 END)"
            " AS cache_reported,"
            " SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,"
            " AVG(duration_ms) AS avg_duration_ms"
            f" FROM requests{where} GROUP BY key ORDER BY requests DESC LIMIT ?",
            [*args, _BREAKDOWN_LIMIT + 1],
        )
        rows = cursor.fetchall()
        truncated = len(rows) > _BREAKDOWN_LIMIT
        rows = rows[:_BREAKDOWN_LIMIT]
        return [
            {
                "key": row["key"],
                "requests": row["requests"],
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "cache_read_tokens": row["cache_read_tokens"],
                "cache_write_tokens": row["cache_write_tokens"],
                "cache_reported": row["cache_reported"],
                "errors": row["errors"],
                "avg_duration_ms": _rounded(row["avg_duration_ms"]),
            }
            for row in rows
        ], truncated

    @staticmethod
    def _percentiles(
        conn: sqlite3.Connection,
        where: str,
        args: list[Any],
        fractions: tuple[float, ...],
    ) -> dict[float, float | None]:
        """Compute percentiles from one ordered pass over ``duration_ms``.

        Two cleverer mechanisms were tried and measured, and both lost to this:

        - An index leading on ``duration_ms`` makes an isolated rank lookup 68x
          faster, and made the whole of ``stats()`` 2.2x slower (1525 ms against
          701 ms). With no ``ANALYZE`` statistics SQLite starts preferring it for
          the totals and breakdown aggregates it does not cover.
        - Streaming the sorted cursor and stopping once the highest needed rank
          has gone past measured 1.5x (unfiltered) to 1.7x (provider-filtered)
          the cost of a plain ``fetchall()``. ``p95`` needs a rank near the end
          of the row count whatever the filter, so there is almost nothing to
          stop early from, while ``fetchall()`` is one bulk C-level fetch
          against a Python-level ``__next__`` per row.

        So this is the same one query and one fetch the removed ``_percentile``
        helper used, with p50 and p95 sharing a single sorted list instead of
        two separate module-level calls. It is not faster than what it replaces;
        the wins in this area are the bounded stats cache, the capped
        breakdowns, and ``pulse()``.

        Interpolation matches the removed helper's formula exactly.
        """
        connector = " AND" if where else " WHERE"
        values = [
            row[0]
            for row in conn.execute(
                f"SELECT duration_ms FROM requests{where}{connector}"
                " duration_ms IS NOT NULL ORDER BY duration_ms",
                args,
            ).fetchall()
        ]
        if not values:
            return dict.fromkeys(fractions)

        count = len(values)
        results: dict[float, float | None] = {}
        for fraction in fractions:
            position = min(count - 1, max(0.0, fraction * (count - 1)))
            lower_index = int(position)
            upper_index = min(count - 1, lower_index + 1)
            weight = position - lower_index
            lower_val = values[lower_index]
            upper_val = values[upper_index]
            results[fraction] = lower_val + (upper_val - lower_val) * weight
        return results

    @staticmethod
    def _series(
        conn: sqlite3.Connection,
        where: str,
        args: list[Any],
        *,
        since: float | None,
        until: float | None,
    ) -> list[dict[str, Any]]:
        bounds = conn.execute(
            f"SELECT MIN(ts_epoch), MAX(ts_epoch) FROM requests{where}", args
        ).fetchone()
        low = since if since is not None else bounds[0]
        high = until if until is not None else bounds[1]
        hourly = low is not None and high is not None and (high - low) < 48 * 3600
        fmt = "%Y-%m-%dT%H:00" if hourly else "%Y-%m-%d"
        cursor = conn.execute(
            "SELECT strftime(?, ts_epoch, 'unixepoch') AS bucket, COUNT(*) AS requests,"
            " COALESCE(SUM(tokens_in),0) + COALESCE(SUM(tokens_out),0) AS tokens,"
            " SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors"
            f" FROM requests{where} GROUP BY bucket ORDER BY bucket",
            [fmt, *args],
        )
        return [
            {
                "bucket": row["bucket"],
                "requests": row["requests"],
                "tokens": row["tokens"],
                "errors": row["errors"],
            }
            for row in cursor.fetchall()
            if row["bucket"] is not None
        ]

    # -------------------------------------------------------------- maintenance

    def prune(self) -> int:
        """Delete oldest rows beyond the configured retention cap.

        Only ``requests`` is capped. ``request_totals``, ``server_sessions``
        and the three ``request_stats_*`` rollup tables are deliberately left
        alone -- they exist precisely to outlive the rows this deletes. That is
        what lets the analytics page answer "all time" honestly on a capped
        table, and it is also why the rollup and a raw scan legitimately
        disagree once retention has bitten.
        """
        if self._max_rows <= 0:
            return 0
        conn = self._connect()
        try:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM requests WHERE id IN ("
                    " SELECT id FROM requests ORDER BY ts_epoch DESC"
                    " LIMIT -1 OFFSET ?"
                    ")",
                    (self._max_rows,),
                )
                removed = cursor.rowcount
                # Bodies are keyed by request id with no cascade configured, so
                # they would otherwise outlive the rows that reference them and
                # keep the file growing forever. Blobs go only once the last
                # request pointing at them is gone -- deduplication means one
                # blob can serve many requests.
                conn.execute(
                    "DELETE FROM request_bodies WHERE NOT EXISTS ("
                    " SELECT 1 FROM requests WHERE requests.id ="
                    " request_bodies.request_id)"
                )
                conn.execute(
                    "DELETE FROM body_blobs WHERE NOT EXISTS ("
                    " SELECT 1 FROM request_bodies WHERE request_bodies.sha ="
                    " body_blobs.sha OR request_bodies.input_sha = body_blobs.sha)"
                )
                # Images follow the same rule as bodies: the link goes when its
                # request does, and the picture itself only once no surviving
                # request still points at it.
                conn.execute(
                    "DELETE FROM request_images WHERE NOT EXISTS ("
                    " SELECT 1 FROM requests WHERE requests.id ="
                    " request_images.request_id)"
                )
                conn.execute(
                    "DELETE FROM request_attempts WHERE NOT EXISTS ("
                    " SELECT 1 FROM requests WHERE requests.id ="
                    " request_attempts.request_id)"
                )
                conn.execute(
                    "DELETE FROM image_blobs WHERE NOT EXISTS ("
                    " SELECT 1 FROM request_images WHERE request_images.sha ="
                    " image_blobs.sha)"
                )
            if removed:
                # Return the freed pages to the filesystem instead of leaving
                # them on the freelist, where they would grow the file forever.
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("PRAGMA incremental_vacuum")
            return removed
        except sqlite3.Error as exc:
            logger.warning("Request log prune failed: {}", exc)
            return 0
        finally:
            conn.close()

    def clear(self) -> int:
        """Erase the stored history, including the permanent counters.

        "Clear log" is an explicit erase, so the all-time figures go with it.
        Leaving them behind would report millions of requests over an empty
        table, which reads as a bug rather than as retained history.
        """
        with self._stats_lock:
            self._stats_cache.clear()
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM requests")
            conn.execute("DELETE FROM request_totals")
            # The stats rollup survives retention, but not an explicit erase --
            # same rule as ``request_totals``, and for the same reason: an
            # empty table reporting millions of requests reads as a bug.
            for table in _ROLLUP_TABLES:
                conn.execute(f"DELETE FROM {table}")
            conn.execute("DELETE FROM request_bodies")
            conn.execute("DELETE FROM body_blobs")
            conn.execute("DELETE FROM request_images")
            conn.execute("DELETE FROM image_blobs")
            conn.execute("DELETE FROM request_attempts")
            return cursor.rowcount

    # ------------------------------------------------- image descriptions ---

    def image_descriptions(
        self, shas: Sequence[str]
    ) -> dict[str, tuple[str, str | None]]:
        """Return the stored description of each picture that has one.

        Keyed on the content address of the *source* bytes, which is already
        this table's primary key, so a screenshot re-sent on every turn of a
        conversation is looked up -- and paid for -- once. A picture with no
        description is simply absent from the result; there is no sentinel,
        because "not described" and "described as nothing" would then be the
        same value.
        """
        wanted = [sha for sha in dict.fromkeys(shas) if sha]
        if not wanted:
            return {}
        found: dict[str, tuple[str, str | None]] = {}
        try:
            with self._connection() as conn:
                # Chunked because SQLite caps a statement at 999 variables by
                # default and a long conversation can carry more images than
                # that.
                for start in range(0, len(wanted), 500):
                    chunk = wanted[start : start + 500]
                    placeholders = ", ".join("?" * len(chunk))
                    rows = conn.execute(
                        "SELECT sha, description, described_by FROM image_blobs"
                        f" WHERE sha IN ({placeholders})"
                        " AND description IS NOT NULL",
                        chunk,
                    ).fetchall()
                    for row in rows:
                        found[str(row["sha"])] = (
                            str(row["description"]),
                            row["described_by"],
                        )
        except sqlite3.Error as exc:
            # A cache miss is always survivable: the caller describes the
            # picture again. A request must never fail because the log did.
            logger.warning("Image description lookup failed: {}", exc)
            return {}
        return found

    def store_image_description(
        self,
        *,
        sha: str,
        kind: str,
        media_type: str | None,
        source_bytes: int | None,
        description: str,
        described_by: str,
    ) -> None:
        """Remember what a sighted model said one picture shows.

        Written while the request that carried the picture is still in flight,
        so the row may not exist yet -- hence the upsert, and hence
        ``_store_images`` filling only the columns that are still NULL when the
        request is finally flushed. The two meet on the same row from either
        direction and neither erases the other's work.
        """
        if not sha or not description:
            return
        try:
            with self._connection() as conn:
                conn.execute(
                    "INSERT INTO image_blobs (sha, kind, media_type,"
                    " source_bytes, description, described_by, described_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(sha) DO UPDATE SET"
                    " description = excluded.description,"
                    " described_by = excluded.described_by,"
                    " described_at = excluded.described_at",
                    (
                        sha,
                        kind or "image",
                        media_type,
                        source_bytes,
                        description,
                        described_by,
                        time.time(),
                    ),
                )
        except sqlite3.Error as exc:
            logger.warning("Image description store failed: {}", exc)

    def clear_image_descriptions(self) -> int:
        """Forget every stored description, keeping the pictures themselves.

        The cache key is the picture's own content, so nothing about a
        description ever goes stale on its own. This exists for the other
        reason an operator wants it gone: a better vision model was configured,
        or a bad one wrote nonsense into the log. Returns how many pictures
        lost a description.
        """
        with self._connection() as conn:
            cursor = conn.execute(
                "UPDATE image_blobs SET description = NULL, described_by = NULL,"
                " described_at = NULL WHERE description IS NOT NULL"
            )
            return int(cursor.rowcount or 0)

    def storage_footprint(self) -> dict[str, Any]:
        """How many rows the log holds and what it costs on disk.

        Invisible until now, which is the whole reason it could reach four and
        a half gigabytes without anybody deciding that was acceptable. The
        answer to a large log is fast queries, not a cap nobody asked for --
        but a number a user cannot see is a number they cannot act on either,
        so the dashboard says it.

        Three files, because all three are the log: the database, the
        write-ahead log beside it (which on a busy install is tens of
        megabytes) and the shared-memory index. A missing file contributes
        nothing rather than failing the readout.

        ``rows`` is ``COUNT(*)``, which answers in 0.018 s on a 333,838-row log
        through the covering index -- this is the retained count the cap
        applies to, not the all-time total the lifetime panel shows.
        """

        bytes_by_file: dict[str, int] = {}
        for label, path in (
            ("database", self._db_path),
            ("wal", self._db_path.with_name(self._db_path.name + "-wal")),
            ("shm", self._db_path.with_name(self._db_path.name + "-shm")),
        ):
            try:
                bytes_by_file[label] = path.stat().st_size
            except OSError:
                bytes_by_file[label] = 0
        rows: int | None
        try:
            with self._connection() as conn:
                rows = int(conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0])
        except sqlite3.Error:
            # NULL means not measured, here as everywhere: a log that cannot be
            # counted right now must not report zero rows.
            rows = None
        return {
            "rows": rows,
            "bytes": sum(bytes_by_file.values()),
            "bytes_by_file": bytes_by_file,
            "path": str(self._db_path),
        }

    def data_mark(self) -> str:
        """A short string that changes whenever this log's contents change.

        The invalidation key for anything derived from the log, and the reason
        a derived payload can be kept across a restart at all: it is a fact
        about the *data*, not a clock. Two calls that return the same mark
        describe a database nothing has been written to, pruned from or
        migrated in between, and a payload computed under one mark is still the
        right answer under the same mark however long ago it was computed.

        Three parts, all cheap:

        - ``MAX(rowid)`` of ``requests`` -- O(1) off the index, and monotonic,
          so an insert always moves it.
        - ``COUNT(*)`` -- because ``prune`` deletes from the *front* and leaves
          the maximum rowid alone, so the count is what notices a prune. It
          answers in 0.018 s on a 333,838-row log through the covering index.
        - every ``request_log_meta`` value -- the migration and backfill
          markers, so a backfill that rewrites columns in place invalidates
          everything derived from them even though no row was added.

        An unreadable database answers ``"unavailable"``, which matches nothing
        that was ever stored and so forces a recomputation rather than serving
        a payload whose provenance cannot be checked.
        """

        try:
            with self._connection() as conn:
                high_water = conn.execute(
                    "SELECT COALESCE(MAX(rowid), 0) FROM requests"
                ).fetchone()[0]
                rows = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
                markers = conn.execute(
                    "SELECT key, value FROM request_log_meta ORDER BY key"
                ).fetchall()
        except sqlite3.Error:
            return "unavailable"
        digest = hashlib.sha256()
        digest.update(f"{int(high_water)}:{int(rows)}".encode())
        for row in markers:
            digest.update(f"\x00{row[0]}\x00{row[1]}".encode())
        return f"log-{int(high_water)}-{int(rows)}-{digest.hexdigest()[:16]}"

    def lifetime(self) -> dict[str, Any]:
        """Return all-time counters, unaffected by retention.

        Every figure in ``stats`` is a sum over ``requests``, which ``prune``
        caps: once the cap is reached those sums stop growing because a row
        leaves for each one that arrives. These come from ``request_totals``,
        which is only ever added to.
        """
        with self._connection() as conn:
            totals = conn.execute(
                f"SELECT {', '.join(f'COALESCE(SUM({name}), 0)' for name in _TOTALS_COUNTERS)},"
                " MIN(day), MAX(day) FROM request_totals"
            ).fetchone()
            by_provider = self._lifetime_breakdown(conn, "provider")
            by_model = self._lifetime_breakdown(conn, "model")
        counters = {
            name: int(totals[index]) for index, name in enumerate(_TOTALS_COUNTERS)
        }
        return {
            **counters,
            "first_day": totals[len(_TOTALS_COUNTERS)],
            "last_day": totals[len(_TOTALS_COUNTERS) + 1],
            "by_provider": by_provider,
            "by_model": by_model,
        }

    @staticmethod
    def _lifetime_breakdown(
        conn: sqlite3.Connection, column: str
    ) -> list[dict[str, Any]]:
        rows = conn.execute(
            f"SELECT {column}, SUM(requests), SUM(tokens_in), SUM(tokens_out),"
            " SUM(error) FROM request_totals"
            f" GROUP BY {column} ORDER BY SUM(requests) DESC LIMIT ?",
            (_BREAKDOWN_LIMIT,),
        ).fetchall()
        return [
            {
                "name": row[0] or None,
                "requests": int(row[1] or 0),
                "tokens_in": int(row[2] or 0),
                "tokens_out": int(row[3] or 0),
                "error": int(row[4] or 0),
            }
            for row in rows
        ]

    def coverage(
        self, *, since: float | None = None, until: float | None = None
    ) -> dict[str, Any]:
        """Report when a server was actually running over a window.

        Without this a flat stretch in the request series is ambiguous: no
        traffic and no server look identical. ``tracking_since`` marks the point
        before which nothing was recorded, so the caller can say "not recorded"
        rather than wrongly claiming downtime.
        """
        with self._connection() as conn:
            first = conn.execute(
                "SELECT MIN(started_at) FROM server_sessions"
            ).fetchone()[0]
            args: list[Any] = []
            where = ""
            if since is not None:
                where += " AND last_seen_at >= ?"
                args.append(since)
            if until is not None:
                where += " AND started_at <= ?"
                args.append(until)
            rows = conn.execute(
                "SELECT started_at, last_seen_at FROM server_sessions"
                f" WHERE 1{where} ORDER BY started_at",
                args,
            ).fetchall()
        sessions = [
            {"started_at": float(row[0]), "last_seen_at": float(row[1])} for row in rows
        ]
        # Clip to the window, then merge: two servers sharing a database would
        # otherwise have their overlapping uptime counted twice.
        clipped: list[tuple[float, float]] = []
        for session in sessions:
            start = session["started_at"]
            end = session["last_seen_at"]
            if since is not None:
                start = max(start, since)
            if until is not None:
                end = min(end, until)
            if end > start:
                clipped.append((start, end))
        covered = 0.0
        merged_end = None
        for start, end in sorted(clipped):
            if merged_end is None or start > merged_end:
                covered += end - start
                merged_end = end
            elif end > merged_end:
                covered += end - merged_end
                merged_end = end
        return {
            "tracking_since": float(first) if first is not None else None,
            "sessions": sessions,
            "covered_seconds": covered,
            "heartbeat_seconds": _SESSION_HEARTBEAT_SECONDS,
        }


def _rounded(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def _mean(total: float | None, count: float | None) -> float | None:
    """Rebuild an average from a stored sum and a stored non-NULL count.

    Averages are not additive, which is why the rollup stores the two
    components instead. A zero count is SQLite's ``AVG`` over no non-NULL rows,
    which is NULL -- not zero.
    """
    if not count:
        return None
    return float(total or 0.0) / float(count)


# --------------------------------------------------------------------- registry

_store_lock = threading.Lock()
_stores: dict[Path, RequestLogStore] = {}


def get_request_log_store(
    db_path: Path | str | None = None,
    *,
    max_rows: int = 50_000,
    enabled: bool = True,
    compress_bodies: bool = True,
    text_max_chars: int = MAX_TEXT_CHARS,
    compression_level: int = _BODY_COMPRESSION_LEVEL,
    queue_max_size: int = _QUEUE_MAX_SIZE,
) -> RequestLogStore | None:
    """Return the shared store for a database path, creating it on first use."""
    if not enabled:
        return None
    path = Path(db_path) if db_path is not None else default_request_log_path()
    with _store_lock:
        store = _stores.get(path)
        if store is None or store._closed.is_set():
            store = RequestLogStore(
                path,
                max_rows=max_rows,
                compress_bodies=compress_bodies,
                text_max_chars=text_max_chars,
                compression_level=compression_level,
                queue_max_size=queue_max_size,
            )
            _stores[path] = store
        return store


def reset_request_log_stores() -> None:
    """Close and forget all shared stores (test isolation / shutdown)."""
    with _store_lock:
        stores = list(_stores.values())
        _stores.clear()
    for store in stores:
        store.close()


_COMPACT_BATCH = 200
# Compaction uses the same level as the write path. Paying once for storage
# that lasts sounds like a reason to turn it up, but measured on real prompts
# level 19 is only 4.9% smaller than level 9 (10.57x against 10.08x) at a ninth
# of the speed -- about three hours instead of twenty minutes on a full log.
_COMPACT_COMPRESSION_LEVEL = _BODY_COMPRESSION_LEVEL


def compact_request_log(
    db_path: Path | str,
    *,
    progress: Any = None,
) -> dict[str, Any]:
    """Convert stored-inline bodies to deduplicated compressed blobs, in place.

    Compression only ever applied to newly written requests, so a database
    carried across the upgrade keeps paying the old price for its whole history
    -- on a real 1.7 GB log, every one of its 50,000 rows. This rewrites them,
    then reclaims the freed pages.

    Safe to interrupt: each batch commits on its own and rows are converted only
    after their blob is stored, so a kill leaves a consistent database with the
    work simply unfinished. Running it again resumes.
    """
    path = Path(db_path)
    before = path.stat().st_size if path.exists() else 0
    # max_rows=0 disables pruning: compaction must never decide to delete.
    store = RequestLogStore(path, max_rows=0, compress_bodies=True)
    converted = 0
    try:
        # A dictionary is what makes this worth doing at all -- without one the
        # saving is 2.7x instead of 8x -- and on a database that has never
        # compressed anything, history is the only place to learn from.
        with store._connection() as conn:
            store.train_dictionary_from_inline_bodies(conn)
        while True:
            with store._connection() as conn:
                rows = conn.execute(
                    "SELECT id, input_text, output_text, thinking_text, tool_calls"
                    " FROM requests WHERE input_text IS NOT NULL"
                    " OR output_text IS NOT NULL OR thinking_text IS NOT NULL"
                    " OR tool_calls IS NOT NULL LIMIT ?",
                    (_COMPACT_BATCH,),
                ).fetchall()
                if not rows:
                    break
                packed: dict[str, tuple[bytes | None, bytes | None]] = {}
                for row in rows:
                    values = {
                        "input_text": row["input_text"],
                        "output_text": row["output_text"],
                        "thinking_text": row["thinking_text"],
                        "tool_calls": _loads_or_none(row["tool_calls"]),
                    }
                    blobs = (
                        _packed_or_none(pack_fields(values, _INPUT_FIELDS)),
                        _packed_or_none(pack_fields(values, _REST_FIELDS)),
                    )
                    if blobs != (None, None):
                        packed[str(row["id"])] = blobs
                store._store_bodies(conn, packed, level=_COMPACT_COMPRESSION_LEVEL)
                ids = [str(row["id"]) for row in rows]
                placeholders = ", ".join("?" * len(ids))
                conn.execute(
                    "UPDATE requests SET input_text = NULL, output_text = NULL,"
                    " thinking_text = NULL, tool_calls = NULL"
                    f" WHERE id IN ({placeholders})",
                    ids,
                )
            converted += len(rows)
            if progress is not None:
                progress(converted)
        resplit = _resplit_combined_blobs(store, progress, converted)
        converted += resplit
    finally:
        store.close()

    reclaimed = _vacuum(path)
    after = path.stat().st_size
    return {
        "converted": converted,
        "bytes_before": before,
        "bytes_after": after,
        "vacuumed": reclaimed,
    }


def _resplit_combined_blobs(store: RequestLogStore, progress: Any, already: int) -> int:
    """Split blobs that still carry the prompt alongside the reply.

    Written before the prompt got its own reference, so they only ever
    deduplicated when the whole body matched -- which almost never happens,
    because the reply differs even when the prompt repeats.
    """
    done = 0
    while True:
        with store._connection() as conn:
            rows = conn.execute(
                "SELECT r.request_id, r.sha, b.dict_id, b.payload"
                " FROM request_bodies r JOIN body_blobs b ON b.sha = r.sha"
                " WHERE r.input_sha IS NULL LIMIT ?",
                (_COMPACT_BATCH,),
            ).fetchall()
            if not rows:
                return done
            packed: dict[str, tuple[bytes | None, bytes | None]] = {}
            stale: list[str] = []
            for row in rows:
                values = store._decode_bodies(row["payload"], row["dict_id"])
                if not values:
                    # Unreadable: leave it exactly as it is rather than
                    # replacing a body with an empty one.
                    continue
                blobs = (
                    _packed_or_none(pack_fields(values, _INPUT_FIELDS)),
                    _packed_or_none(pack_fields(values, _REST_FIELDS)),
                )
                if blobs == (None, None):
                    continue
                packed[str(row["request_id"])] = blobs
                stale.append(str(row["sha"]))
            if not packed:
                return done
            store._store_bodies(conn, packed, level=_COMPACT_COMPRESSION_LEVEL)
            # Drop combined blobs nothing points at any more.
            placeholders = ", ".join("?" * len(stale))
            conn.execute(
                f"DELETE FROM body_blobs WHERE sha IN ({placeholders})"
                " AND NOT EXISTS (SELECT 1 FROM request_bodies WHERE"
                " request_bodies.sha = body_blobs.sha"
                " OR request_bodies.input_sha = body_blobs.sha)",
                stale,
            )
        done += len(packed)
        if progress is not None:
            progress(already + done)


def _loads_or_none(raw: Any) -> Any:
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _vacuum(path: Path) -> bool:
    """Return freed pages to the filesystem.

    ``VACUUM`` needs the whole database to itself, so this reports failure
    rather than raising when a server still holds it open -- the conversion
    above has already landed either way.

    It deliberately does not widen ``page_size``. That looked like a free win
    to fold in here, but SQLite refuses to change page size on a WAL database,
    so the pragma was silently ignored and the claim was simply false. Undoing
    it would mean dropping out of WAL around the vacuum, which is a real risk
    to take on someone's only copy of their history for an uncertain few
    percent.
    """
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.isolation_level = None
        conn.execute("VACUUM")
        return True
    except sqlite3.Error as exc:
        logger.warning("Request log vacuum skipped: {}", exc)
        return False
    finally:
        conn.close()


@dataclass(frozen=True, slots=True)
class ServedModelObservation:
    """One model id this log has seen a provider answer with, and how often.

    Proof of entitlement, obtainable with no network and no credential: a
    model that has already answered successfully for this credential exists on
    this plan, whatever a catalogue says. ``successes`` and ``last_ts_iso``
    are what the log recorded, never a derived or rounded figure.
    """

    model_id: str
    successes: int
    last_ts_iso: str


def observed_served_models(
    provider_id: str,
    *,
    db_path: Path | str | None = None,
    limit: int = 500,
) -> tuple[ServedModelObservation, ...]:
    """Model ids one provider has successfully served, newest activity first.

    Opened read-only and never through the shared store: this is a discovery
    read, it must not create a writer thread, must not migrate a schema and
    must not keep a handle open. Every failure -- no log registered, no file,
    a schema older than the columns below, a lock -- returns an empty tuple,
    because a rung that cannot answer declines rather than emptying a picker.
    """
    if not provider_id.strip():
        return ()
    try:
        path = Path(db_path) if db_path is not None else default_request_log_path()
    except RuntimeError:
        return ()
    if not path.is_file():
        return ()
    # ``mode=ro`` first, then ``immutable=1``. A WAL database needs a shared
    # -shm segment even to be read, and a reader that cannot get one would
    # otherwise decline outright; ``immutable=1`` reads the main file alone,
    # deliberately ignoring the WAL. The newest rows can be missing under the
    # fallback, which for "has this model ever answered?" costs nothing --
    # and is far better than the rung going silent on every live install.
    conn = None
    for query in ("mode=ro", "mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect(
                f"file:{path.as_posix()}?{query}", uri=True, timeout=1.0
            )
            break
        except sqlite3.Error:
            continue
    if conn is None:
        return ()
    try:
        rows = conn.execute(
            "SELECT resolved_model, COUNT(*) AS n, MAX(ts_iso) AS last_iso"
            " FROM requests"
            " WHERE provider = ? AND status = 'success'"
            "   AND resolved_model IS NOT NULL AND resolved_model <> ''"
            " GROUP BY resolved_model"
            " ORDER BY last_iso DESC"
            " LIMIT ?",
            (provider_id, int(limit)),
        ).fetchall()
    except sqlite3.Error as error:
        logger.debug("Served-model observation query failed: {}", error)
        return ()
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()
    return tuple(
        ServedModelObservation(
            model_id=str(row[0]),
            successes=int(row[1] or 0),
            last_ts_iso=str(row[2] or ""),
        )
        for row in rows
        if str(row[0]).strip()
    )


def store_from_settings(settings: Any) -> RequestLogStore | None:
    """Resolve the shared store for the active settings, if logging is enabled."""
    if not getattr(settings, "request_log_enabled", True):
        return None
    # The path is whatever the entrypoint registered with
    # ``set_request_log_path`` before the first store was opened; passing no
    # path here honours that registration. Reading ``settings.request_log_path``
    # instead would recompute the path from ``config_dir_path()`` and bypass
    # both the registration and the test isolation that depends on it.
    return get_request_log_store(
        max_rows=int(getattr(settings, "request_log_max_rows", 50_000) or 50_000),
        text_max_chars=int(
            getattr(settings, "request_log_text_max_chars", MAX_TEXT_CHARS)
            or MAX_TEXT_CHARS
        ),
        compression_level=int(
            getattr(settings, "request_log_compression_level", _BODY_COMPRESSION_LEVEL)
            or _BODY_COMPRESSION_LEVEL
        ),
        queue_max_size=int(
            getattr(settings, "request_log_queue_max_size", _QUEUE_MAX_SIZE)
            or _QUEUE_MAX_SIZE
        ),
        compress_bodies=bool(getattr(settings, "request_log_compress_bodies", True)),
    )
