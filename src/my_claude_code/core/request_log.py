"""SQLite-backed request log with a non-blocking background writer."""

import base64
import contextlib
import copy
import hashlib
import json
import math
import os
import queue
import sqlite3
import struct
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import (
    Callable,
    Collection,
    Generator,
    Iterator,
    Mapping,
    Sequence,
)
from compression import zstd
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from loguru import logger

from my_claude_code.core.cancelled_reasons import (
    CANCELLED_STATUS,
    CANCELLED_SUB_LABELS,
    classify_cancelled,
    restart_boundaries,
    split_status_filter,
    sub_label_case_sql,
)
from my_claude_code.core.client_fingerprint import harness_from_headers
from my_claude_code.core.media_store import (
    MediaOutputRecord,
    delete_media_files,
    media_file_path,
    media_root,
    remove_media_file,
)
from my_claude_code.core.proxy_attribution import exit_filter, masked_exit_label
from my_claude_code.core.request_images import CapturedImage
from my_claude_code.core.request_origin import (
    BACKFILL_SIGNAL,
    PROMPT_HARNESSES,
    PROMPT_SCAN_MAX_CHARS,
    folder_filter,
    merge_origin_source,
    origin_provenance,
    project_dir_from_prompt,
    project_short,
    session_filter,
    session_short,
)
from my_claude_code.core.success_reasons import (
    SUCCESS_REASON_SOURCE_COLUMNS,
    SUCCESS_STATUS,
    SUCCESS_SUB_LABELS,
    classify_success,
    classify_success_row,
    split_success_status_filter,
    success_sub_label_case_sql,
)
from my_claude_code.core.tool_catalogue import (
    TOOL_SHA_BYTES,
    ToolCatalogue,
    ToolFingerprinter,
    split_member_shas,
)
from my_claude_code.core.upstream_ladder import format_status_census
from my_claude_code.core.version import package_version

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

#: What prices one new row on the writer thread (7.69.0): a zero-argument
#: callable answering ``(cost_usd, cost_source)``. See ``RequestRecord.pricer``.
RowPricer = Callable[[], tuple[float | None, str | None]]


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
# The orphan sweeps ``prune`` runs, in the order it runs them. Each link table
# points at a request; each blob table is named by exactly one link table.
_ORPHAN_SWEEP_TABLES = (
    "request_bodies",
    "body_blobs",
    "request_images",
    "request_attempts",
    # 7.76.0: compact skipped attempts are keyed by request id like a link
    # table. Swept whole on a process's first pass too, which collects any an
    # older version's prune left behind (it does not know the table).
    "request_attempt_skips",
    "image_blobs",
    "request_media",
    "media_blobs",
)
_LINK_BLOB_TABLES = (
    ("request_bodies", "body_blobs"),
    ("request_images", "image_blobs"),
    ("request_media", "media_blobs"),
)
# Above this many requests removed by one pass, ``prune`` sweeps whole tables
# (what every pass did before 7.72.2) rather than following each removed row:
# both find the same orphans, and a whole-table sweep is the cheaper of the two
# once most of the table is going anyway.
_TARGETED_SWEEP_MAX_ROWS = 10_000
# Values per ``IN (...)`` list, well under SQLite's bound-variable limit.
_SWEEP_CHUNK = 500
# How often, at most, ``prune`` sweeps tool catalogues no retained request
# carries any more. The sweep reads one column of every ``requests`` row, and
# on a capped log ``prune`` runs every hundred inserts; the tables it cleans are
# a few megabytes over a lifetime, so an hour of lag costs nothing.
_TOOL_SWEEP_INTERVAL_SECONDS = 3600.0
# ``IN (...)`` lists are chunked well below SQLite's variable limit.
_SHA_LOOKUP_CHUNK = 500
_WRITER_BATCH_SIZE = 50
_WRITER_POLL_SECONDS = 0.25
_QUEUE_MAX_SIZE = 10_000
_STOP = object()
# "Write the session row now" -- for a fact the next heartbeat is too late for,
# such as the listener having gone. See ``touch_server_sessions``.
_TOUCH = object()
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

# The escape character of the Folder filter's LIKE. Not a backslash: every
# Windows path is full of them, and each would have to be doubled.
_LIKE_ESCAPE = "!"


def _like_contains(text: str) -> str:
    """``text`` as a LIKE pattern that matches it anywhere, wildcards disarmed."""
    escaped = (
        text.replace(_LIKE_ESCAPE, _LIKE_ESCAPE * 2)
        .replace("%", f"{_LIKE_ESCAPE}%")
        .replace("_", f"{_LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"


def _note_exit(tried: list[str], label: Any) -> None:
    """Append a stored exit label to ``tried``, masked, once, in dial order."""
    if not isinstance(label, str) or not label:
        return
    masked = masked_exit_label(label)
    if masked and masked not in tried:
        tried.append(masked)


# Newest measured attempts pulled per ``latency_by_model`` call so its p50/p95
# can be taken in Python. SQLite has no percentile function here and the
# duration percentiles elsewhere use a bucket histogram this column has no
# equivalent of. The cap is what keeps the call bounded on a log that grows
# without limit; when it bites, the payload says ``p50_source = "sampled"``
# rather than presenting a truncated answer as a complete one.
_LATENCY_SAMPLE_ROWS = 20_000

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
# ``request_attempts.ts_epoch``, copied from each attempt's parent request.
# Versioned by the same rule as the rollup keys: the day the copy is defined
# differently, bumping the name is how every installation redoes it.
_ATTEMPTS_TS_BACKFILL_KEY = "attempts_ts_backfilled_at_v1"
#: The highest rowid the walk has passed. A cursor rather than a predicate,
#: unlike the harness and cost backfills: an attempt whose parent request has
#: been pruned can never be filled in, so "still NULL" is not progress here and
#: a predicate-only walk would re-read those rows forever.
_ATTEMPTS_TS_BACKFILL_THROUGH_KEY = "attempts_ts_backfilled_through_v1"
# Rows per committed chunk. Measured on the real table: 571,665 attempts in
# 54.0 s at this size.
_ATTEMPTS_TS_CHUNK_ROWS = 5_000
# The explicit folder backfill (7.42.0). Never automatic: it runs only after
# the operator presses the button, because it decompresses stored prompts and
# that is minutes of work on a large log. The cursor makes a second press
# continue where an interrupted walk stopped; the ``at`` key says it finished.
_ORIGIN_BACKFILL_KEY = "origin_backfilled_at_v1"
_ORIGIN_BACKFILL_THROUGH_KEY = "origin_backfilled_through_v1"
# Rows examined per committed chunk. Each one may mean a decompression, so
# this is smaller than the arithmetic-only backfills' 5,000: one chunk is
# what a request queued behind the walk can wait for.
_ORIGIN_BACKFILL_CHUNK_ROWS = 500
# How much of a stored prompt the backfill reads: the same 64 KiB the live
# extractor scans. The stored prompt starts with the system blocks.
_PROMPT_HEAD_CHARS = PROMPT_SCAN_MAX_CHARS
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

#: ``optimization`` values that name MCC's own web tools rather than a rule.
#: Since 7.69.5 a web search or web fetch that MCC answers itself is recorded
#: as a local answer under the tool's name (``local:web_search``), because no
#: model was asked. It is not an optimization -- it does real work and avoids
#: no tokens -- so the Token Optimizer leaves these out, as it always did.
LOCAL_WEB_TOOL_ANSWERS: tuple[str, ...] = ("web_search", "web_fetch")

#: ``optimization`` set by a rule that answered locally, web tools excluded.
_OPTIMIZATION_RULE_SQL = (
    "(optimization IS NOT NULL AND optimization NOT IN ("
    + ", ".join(f"'{name}'" for name in LOCAL_WEB_TOOL_ANSWERS)
    + "))"
)

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
    # Beside it, never instead of it: ``ttft_ms`` is what the client waited,
    # fallbacks included, and this is what the model that answered actually
    # took. A list row can show both, and their difference is the time the
    # chain lost. NULL on every row written before 7.4.0.
    "ttft_winner_ms",
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
    # Where the request came from (7.42.0): which conversation, which
    # subagent, which folder, and how each is known. Projected so the list can
    # show Session and Folder without opening the row. NULL on older rows.
    "session_id",
    "agent_id",
    "parent_session_id",
    "project_dir",
    "origin_source",
)

#: Everything :func:`classify_cancelled` reads off a request row.
#:
#: Named once so the three queries that have to carry them -- the list, the
#: detail and the two exports -- cannot drift from the classifier, and so the
#: export's SELECT can top itself up without the caller knowing.
_CANCEL_REASON_SOURCE_COLUMNS: tuple[str, ...] = (
    "status",
    "ts_epoch",
    "ttft_ms",
    "duration_ms",
    "output_chars",
    "thinking_chars",
)

#: Columns :meth:`RequestLogStore._percentiles` may be asked for.
#:
#: A column name cannot be a bound parameter, so it is interpolated into the
#: SQL -- and an allow-list, rather than a promise about callers, is what makes
#: that safe to read years from now. Both members are latency in milliseconds
#: on ``requests``; ``ttft_winner_ms`` is deliberately absent, because it is
#: NULL on every row written before 7.4.0 and a percentile over the remainder
#: would describe a different population from the one the card names.
_PERCENTILE_COLUMNS: frozenset[str] = frozenset({"duration_ms", "ttft_ms"})

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
    port INTEGER,
    listening INTEGER,
    version TEXT
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
    # Added in 7.4.0, and the *request-level* half of per-attempt latency only.
    # A rollup bucket's dimensions are read from the ``requests`` row, so the
    # winner's model genuinely is this bucket's model -- which is exactly why
    # per-*attempt* latency is not here: a model that failed never appears in a
    # ``requests`` row at all, so folding its TTFT into these tables would file
    # it under the model that rescued the request, which is the misattribution
    # the whole feature exists to end. That question is served live by
    # ``latency_by_model()`` over ``request_attempts`` instead.
    #
    # No rebuild marker, deliberately. ``_ensure_rollup_counter_columns`` adds a
    # counter with ``DEFAULT 0``, and its docstring's argument holds here
    # exactly: ``ttft_winner_ms`` is NULL on 100% of the rows written before
    # this release, so an already-rolled-up hour reporting sum 0 / count 0 is
    # the truth, and ``_mean(0, 0)`` is None -- the same answer ``AVG()`` over
    # those rows gives on the scan path. The two paths agree row for row, which
    # a contract test asserts.
    ("ttft_winner_sum", "REAL", "COALESCE(SUM(ttft_winner_ms), 0)"),
    (
        "ttft_winner_count",
        "INTEGER",
        "SUM(CASE WHEN ttft_winner_ms IS NOT NULL THEN 1 ELSE 0 END)",
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
-- Dictionaries for compressed ``request_attempts.wire_body`` snapshots
-- (7.73.0). Their own table, never ``body_dictionaries``: an older version
-- takes the highest id there as its dictionary for every body, and a wire
-- dictionary would quietly make its prompts compress worse. AUTOINCREMENT, so
-- an id is never handed out twice, even after a delete. Rows are never deleted:
-- every compressed snapshot names the dictionary it needs.
CREATE TABLE IF NOT EXISTS wire_dictionaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    content BLOB NOT NULL
);
-- Request metadata stored once (7.75.0): each distinct ``headers`` /
-- ``route_chain`` / ``params`` text, named by ``requests.<column>_ref``.
-- ``digest`` is the first bytes of the text's SHA-256, a lookup key only.
-- AUTOINCREMENT, so an id is never handed out twice: a value no retained row
-- names is deleted by ``prune``, and a later row naming the same text gets a
-- new id rather than an old one.
CREATE TABLE IF NOT EXISTS request_values (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    digest BLOB NOT NULL,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_request_values_digest_v1
    ON request_values(digest);
-- Skipped route attempts stored compactly (7.76.0). Nine attempt rows in ten
-- record a model the chain never asked -- "never reached", "paused by you" --
-- and repeat the same few thousand facts across millions of rows. A request's
-- skipped attempts are one row of ``request_attempt_skips`` naming one
-- ``attempt_skip_sets`` row: the JSON array of their
-- ``[attempt, provider, model_ref, error_kind, error_message]``, stored once
-- however many requests share it. ``ts_epoch`` / ``key_index`` / ``key_label``
-- are the same for every skipped attempt of a request and live on its row;
-- every other attempt column of such a row is NULL by definition (an attempt
-- that holds anything more stays in ``request_attempts``). Their own tables,
-- never ``request_values``: an older version's sweep of that table knows only
-- its own three refs and would delete these. AUTOINCREMENT, so a set id is
-- never handed out twice.
CREATE TABLE IF NOT EXISTS attempt_skip_sets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    digest BLOB NOT NULL,
    attempts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempt_skip_sets_digest_v1
    ON attempt_skip_sets(digest);
CREATE TABLE IF NOT EXISTS request_attempt_skips (
    request_id TEXT PRIMARY KEY,
    set_id INTEGER NOT NULL,
    ts_epoch REAL,
    key_index INTEGER,
    key_label TEXT
) WITHOUT ROWID;
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
-- Media a media endpoint generated (7.60.0), content-addressed. The bytes
-- live in files beside this database (core/media_store.py) and only when
-- MEDIA_STORE_ENABLED is on; ``stored`` says whether a file was written.
-- The hash, type and size are recorded either way.
CREATE TABLE IF NOT EXISTS media_blobs (
    sha256 TEXT PRIMARY KEY,
    mime TEXT,
    bytes INTEGER,
    created_at REAL,
    stored INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS request_media (
    request_id TEXT NOT NULL,
    direction TEXT NOT NULL,
    idx INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY (request_id, direction, idx)
);
-- Video jobs a host accepted (7.64.0), one row per job, keyed on MCC's own
-- id. ``upstream_id`` is the host's name for the job and the key it was
-- accepted with is pinned by index, masked label and fingerprint -- never
-- the key itself, and never a URL the host serves the file at. Written the
-- moment the job is accepted (not by the batched writer), so a client's first
-- poll finds it; pruned with its request row after an hour's grace.
CREATE TABLE IF NOT EXISTS media_jobs (
    job_id TEXT PRIMARY KEY,
    request_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    requested_model TEXT,
    upstream_id TEXT NOT NULL,
    key_index INTEGER,
    key_fingerprint TEXT,
    key_label TEXT,
    proxy_label TEXT,
    status TEXT,
    status_raw TEXT,
    progress INTEGER,
    seconds REAL,
    size TEXT,
    prompt TEXT,
    error TEXT,
    usage_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL,
    completed_at REAL,
    content_sha TEXT,
    content_bytes INTEGER,
    content_mime TEXT,
    row_seconds_written INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_media_jobs_created ON media_jobs(created_at);
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
-- The tools a request carried, content-addressed twice over. Before 7.40.0 the
-- log kept only ``params.tools_count``, and when one tool pattern in a 212-tool
-- catalogue broke every ChatGPT-OAuth request on 2026-09-20, finding the tool
-- took hours because the array had never been kept anywhere.
--
-- ``tool_schemas`` holds each distinct tool definition once, keyed on the
-- SHA-256 of its canonical JSON (``core.tool_catalogue``). ``definition`` is
-- that JSON as plain text, NULL when the operator has turned body capture off
-- -- the hash and the name are kept either way. Plain rather than compressed
-- like a body blob: a lifetime of distinct definitions is a few MB, and plain
-- text lets the 2026-09-20 question be one query --
-- ``SELECT name FROM tool_schemas WHERE definition LIKE '%videoScale%'``.
-- ``tool_catalogues`` holds each distinct tools array once: its hash is the
-- SHA-256 of ``member_shas``, the members' 32-byte hashes concatenated in the
-- order the client sent them. The request itself carries one 32-byte hash,
-- ``requests.tool_catalogue_sha``. ``first_seen``/``last_seen``/``seen`` are
-- counted by the writer, like ``request_totals``, and outlive retention.
CREATE TABLE IF NOT EXISTS tool_schemas (
    sha BLOB PRIMARY KEY,
    name TEXT NOT NULL,
    definition TEXT
);
CREATE INDEX IF NOT EXISTS idx_tool_schemas_name ON tool_schemas(name);
CREATE TABLE IF NOT EXISTS tool_catalogues (
    sha BLOB PRIMARY KEY,
    tool_count INTEGER NOT NULL,
    member_shas BLOB NOT NULL,
    first_seen REAL,
    last_seen REAL,
    seen INTEGER NOT NULL DEFAULT 0
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

# Dictionaries per kind of content (7.73.0). Until then one dictionary was
# trained once, on whatever blobs were newest, and never again: on a real log
# it dated from 2026-08-09, and by October last week's prompts compressed 3.9x
# against it where a fresh prompt-only dictionary gave 1.8-2.0x smaller blobs
# still. Prompts, replies and wire snapshots share almost nothing, so each kind
# learns from its own traffic: one mixed dictionary measured 28.0 MB where a
# prompt-only one wrote 19.7 MB for the same prompts.
_DICT_KIND_PROMPT = "prompt"
_DICT_KIND_REST = "rest"
_DICT_KIND_WIRE = "wire"
# Wire snapshots first: their dictionary trains in about a second and is worth
# 37x, where a prompt dictionary is tens of seconds of CPU for about 2x.
_DICT_KINDS = (_DICT_KIND_WIRE, _DICT_KIND_PROMPT, _DICT_KIND_REST)
# The refresh rule the user approved: a kind is relearned once its newest
# dictionary is older than the last 14 days of traffic and at least 1,024
# samples of that kind arrived in those 14 days. A kind with no dictionary at
# all needs only ``_BODY_DICT_MIN_SAMPLES``, as the first dictionary always has.
_DICT_REFRESH_AGE_SECONDS = 14 * 86_400
_DICT_REFRESH_MIN_SAMPLES = 1_024
# A training that raised is not retried on every pass: a prompt dictionary is
# tens of seconds of CPU, and the same samples would fail the same way.
_DICT_TRAIN_RETRY_SECONDS = 3_600.0
# Wire snapshots are 5 KB of JSON each; 64 KB measured 31.3x against 32.2x for
# 110 KB, at two thirds of the encode time (0.95 against 1.49 ms per row).
_WIRE_DICT_SIZE = 64 * 1024
# Candidate attempt rows read per wanted wire sample: about one attempt in ten
# carries a snapshot, almost all of them ``succeeded`` or ``failed`` ones.
_WIRE_SAMPLE_CANDIDATES_PER_SAMPLE = 2
# A compressed wire snapshot is a BLOB in the same ``wire_body`` column a plain
# one is TEXT in, so the storage class says which encoding a row holds: TEXT is
# the JSON as written, BLOB is this envelope. Byte 0 is the envelope version,
# then the ``wire_dictionaries`` id as an unsigned LEB128 varint (0 = none),
# then one zstd frame -- which also carries zstd's own id of the dictionary it
# needs, so a frame handed the wrong dictionary fails rather than decoding to
# garbage. A version this code does not know reads as no snapshot, never as
# bytes passed through.
_WIRE_ENVELOPE_V1 = 1

# History conversion (7.74.0); see ``_run_history_conversion``. One JSON
# document in ``request_log_meta``, written in the same transaction as the rows
# each step changes, so a step either happened with its bookkeeping or not at
# all. Versioned name per the marker rule.
_HISTORY_CONVERSION_KEY = "history_conversion_v1"
# Writer time one step may take before it commits and looks at the queue
# again: the longest a request's row can wait behind the conversion. Measured
# on the full-size copy against the writer's idle batch latency; see the PR.
_HISTORY_STEP_SECONDS = 0.25
# Rows read per query inside a step. Only bounds a single SELECT; the step
# itself ends on the time budget above, never on a row count.
_HISTORY_FETCH_ROWS = 64
# The longest the conversion keeps the writer's idle branch before handing it
# back for one poll, so the session heartbeat and dictionary installs run.
_HISTORY_IDLE_SLICE_SECONDS = 5.0
# At most one progress line per this many seconds.
_HISTORY_PROGRESS_LOG_SECONDS = 300.0
# ``PRAGMA incremental_vacuum(N)`` step bounds. A step adapts N to fit
# ``_HISTORY_STEP_SECONDS``, at most doubling from one step to the next: the
# cost of a page varies with where it moves from, and on the full-size copy a
# 4,096-page step that followed cheap ones took 2.06 s. 1,024 bounds the worst
# case near the conversion's own step.
_SPACE_STEP_PAGES_MIN = 16
_SPACE_STEP_PAGES_START = 256
_SPACE_STEP_PAGES_MAX = 1_024
# Chosen by none of the 136 statements the admin read paths issue, nor by the
# all-time shapes or prune (measured, investigation round 2, s11): 284 MB on
# the real log. ``idx_request_attempts_ts_v1`` serves every query it could.
_UNUSED_ATTEMPT_INDEX = "idx_request_attempts_model_v1"
# Index entries the read-ahead takes per slice before it checks for a close.
_INDEX_WARM_FETCH_ROWS = 20_000
# Markers that do not describe the data: the conversion rewrites how values
# are stored, never what they are, so a derived payload stays right across it.
_DATA_MARK_IGNORED_KEYS = frozenset({_HISTORY_CONVERSION_KEY})
# The conversion's parts, in the order they run. Each is one key of the
# ``_HISTORY_CONVERSION_KEY`` document. A part added by a later release is
# missing from a document an earlier one finished; the document is then opened
# again for that part alone (``_reopen_history_state``).
_HISTORY_PHASES = ("wire", "bodies", "metadata", "skipped")

# Request metadata stored once (7.75.0). ``headers``, ``route_chain`` and
# ``params`` repeat a few hundred distinct values across hundreds of thousands
# of rows (measured: 353 / 253 / 233 values, 484 MB, on a 551,774-row log).
# Each distinct value lives once in ``request_values``; a row names it in the
# ``<column>_ref`` beside the column and leaves the column itself NULL. A row
# with an inline value and no ref -- every row an older version wrote -- reads
# exactly as before. Readers return the text either way, byte for byte, and
# never the ref.
_STORED_ONCE_COLUMNS = ("headers", "route_chain", "params")
_STORED_ONCE_REFS = tuple(f"{column}_ref" for column in _STORED_ONCE_COLUMNS)
# The history conversion's read of the next rows: per column its storage
# class, its stored bytes exactly as they are, and its ref.
_METADATA_FETCH_SQL = (
    "SELECT rowid, "
    + ", ".join(
        f"typeof({column}), CAST({column} AS BLOB), {column}_ref"
        for column in _STORED_ONCE_COLUMNS
    )
    + " FROM requests WHERE rowid > ? ORDER BY rowid LIMIT ?"
)
# Bytes of the SHA-256 of a value kept as its lookup key. Not unique on its
# own: a lookup compares the stored text, so a collision costs a second row,
# never a wrong one.
_VALUE_DIGEST_BYTES = 8
# Values a reading process keeps in memory, by id. A value never changes once
# written and its id is never reused (AUTOINCREMENT), so a cached entry cannot
# go stale; the bound only stops a log of unusual values from growing it.
_VALUE_CACHE_MAX = 4_096

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
#
# ``listening`` was added in 7.69.2, when a server that had lost its listening
# socket kept heartbeating for 7 h 12 min and every reader called it live: 1 =
# the listener was open at the last heartbeat, 0 = this server lost its
# listening socket and is draining to exit, NULL = not measured (every row
# written before 7.69.2, and a session whose listener was never watched).
#
# ``version`` was added in 7.72.0, when a starting server began stopping the old
# servers of its own port and configuration folder that lost their listener: the
# line that says it stopped one names the version that server was running.
# Every server from 7.72.0 on writes it when it opens its row, so NULL means the
# row was written by a server older than 7.72.0 (SESSION_VERSION_SINCE).
_SESSION_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("host", "ALTER TABLE server_sessions ADD COLUMN host TEXT"),
    ("port", "ALTER TABLE server_sessions ADD COLUMN port INTEGER"),
    ("listening", "ALTER TABLE server_sessions ADD COLUMN listening INTEGER"),
    ("version", "ALTER TABLE server_sessions ADD COLUMN version TEXT"),
)

#: The first release whose session rows record the server's version. A row
#: without one was written by an older server.
SESSION_VERSION_SINCE = "7.72.0"

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


# Whether this process's listening socket is still open, published by the
# listener guard (``runtime/listener_guard.py``). ``None`` until it has looked.
_listening: bool | None = None


def set_server_listening(listening: bool | None) -> None:
    """Record whether this process's listening socket is open, for its session row.

    ``True`` once the listener guard is watching an open socket, ``False`` the
    moment it finds the socket closed by anything other than a requested stop.
    The heartbeat writes it as the row's ``listening`` column; see
    :func:`touch_server_sessions` for writing it without waiting for one.
    """

    global _listening
    with _bind_lock:
        _listening = listening


def server_listening() -> bool | None:
    """Whether this process's listener is open; ``None`` if nobody has looked."""

    with _bind_lock:
        return _listening


def _listening_column() -> int | None:
    listening = server_listening()
    return None if listening is None else int(listening)


@dataclass(frozen=True, slots=True)
class ServerSession:
    """One recorded server run, as the log remembers it."""

    id: int
    pid: int | None
    started_at: float
    last_seen_at: float
    host: str | None = None
    port: int | None = None
    #: The version the server was running; ``None`` for a row written before
    #: :data:`SESSION_VERSION_SINCE`, which did not record it.
    version: str | None = None

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
        # touch before it can be read. The same for ``version`` (7.72.0).
        if {"host", "port", "version"} <= columns:
            query = (
                "SELECT id, pid, started_at, last_seen_at, host, port, version"
                " FROM server_sessions ORDER BY started_at DESC LIMIT ?"
            )
        elif {"host", "port"} <= columns:
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
                version=str(row["version"])
                if "version" in keys and row["version"]
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
    # Added in 7.4.0. The winning attempt's own first-token time, beside the
    # untouched ``ttft_ms``; the difference between them is what the chain lost
    # to models that did not answer. Deliberately NOT backfilled and not
    # backfillable: nothing in the log records when each attempt's own stream
    # started, so every row written before this column stays NULL forever, and
    # NULL keeps meaning "not measured" rather than "the fallback cost nothing".
    ("ttft_winner_ms", "ALTER TABLE requests ADD COLUMN ttft_winner_ms REAL"),
    # Added in 7.9.0. Reasoning tokens the host itself reported, parsed off
    # ``completion_tokens_details.reasoning_tokens`` by
    # ``core/reported_cost.py:48-65``. They were already *used*:
    # ``api/request_capture.py`` hands them to ``_price()`` so a source with a
    # reasoning rate charges them at it rather than at the output rate -- and
    # then dropped the number. A number that changes a price is stored beside
    # the price it changed.
    #
    # NULL is "not measured", and it covers three different things that are the
    # same thing for a reader: a row written before this column, a host that
    # does not report the field, and a request with no reasoning. It is
    # emphatically not zero. Deliberately NOT backfilled: the counts were never
    # written down, and nothing can recover them.
    (
        "reasoning_tokens",
        "ALTER TABLE requests ADD COLUMN reasoning_tokens INTEGER",
    ),
    # 7.40.0: the 32-byte hash of the tools array this request carried, or
    # NULL when it carried none. The array itself lives once, in
    # ``tool_catalogues`` and ``tool_schemas``; see ``_BODIES_SCHEMA``. NULL
    # on every row written before the column existed, which is "not
    # recorded", not "no tools" -- ``params.tools_count`` says which.
    (
        "tool_catalogue_sha",
        "ALTER TABLE requests ADD COLUMN tool_catalogue_sha BLOB",
    ),
    # 7.42.0: where the request came from, as the client stated it. See
    # ``core/request_origin.py`` for every signal read and in what order.
    # NULL is "not measured": a row older than the columns, a client that
    # states nothing, or a setting that turned capture off. Never "none".
    #
    # The conversation id the client sent (``x-claude-code-session-id``),
    # verbatim and capped. It identifies a conversation, not a person, and it
    # is stored locally only -- nothing new is sent upstream.
    ("session_id", "ALTER TABLE requests ADD COLUMN session_id TEXT"),
    # The subagent id (``x-claude-code-agent-id``) when a subagent is speaking.
    ("agent_id", "ALTER TABLE requests ADD COLUMN agent_id TEXT"),
    # Set only when a child signal exists: the session a subagent said it
    # belongs to. Whether that is the parent's own id is checked on real rows
    # before anything groups by it.
    ("parent_session_id", "ALTER TABLE requests ADD COLUMN parent_session_id TEXT"),
    # The working directory the agent reported, verbatim (backslashes kept,
    # trailing separator stripped, capped). The short display form is derived
    # at read time and never stored.
    ("project_dir", "ALTER TABLE requests ADD COLUMN project_dir TEXT"),
    # ``field=source.signal`` per captured field, so the detail pane can say
    # how each value is known.
    ("origin_source", "ALTER TABLE requests ADD COLUMN origin_source TEXT"),
    # 7.47.0: how many empty-delta keepalive frames (STREAM_KEEPALIVE_MODE=
    # frames) the client was sent while the model's own text or tool-call
    # block was open and silent. Written at the HTTP boundary, outside the
    # capture, so no other column counts them. NULL is "frames mode was not
    # running for this request" -- ping mode, a surface without frames, a
    # non-stream request, the log's own older rows -- and 0 is "it ran and was
    # never needed". Not backfillable: nothing recorded them before.
    ("keepalive_frames", "ALTER TABLE requests ADD COLUMN keepalive_frames INTEGER"),
    # 7.60.0: media endpoints (images, later speech/transcription/video).
    # ``media_operation`` names what the row did (``image_generate``);
    # NULL is "not a media request". The three output facts are NULL for
    # "not measured" (a URL-only answer has no bytes here) and never a
    # guessed zero. ``endpoint`` stays the modality dimension, so the
    # rollup needs no rebuild.
    ("media_operation", "ALTER TABLE requests ADD COLUMN media_operation TEXT"),
    (
        "output_image_count",
        "ALTER TABLE requests ADD COLUMN output_image_count INTEGER",
    ),
    ("media_bytes_out", "ALTER TABLE requests ADD COLUMN media_bytes_out INTEGER"),
    ("media_sha_out", "ALTER TABLE requests ADD COLUMN media_sha_out TEXT"),
    # 7.62.0: seconds of audio a speech answer carried, when its container
    # states it (WAV); NULL for MP3/Opus/AAC -- not measured, never 0.
    (
        "output_audio_seconds",
        "ALTER TABLE requests ADD COLUMN output_audio_seconds REAL",
    ),
    # 7.63.0: seconds of audio a transcription heard -- the host's own usage
    # or duration, else a WAV upload's header; NULL = not measured.
    (
        "input_audio_seconds",
        "ALTER TABLE requests ADD COLUMN input_audio_seconds REAL",
    ),
    # 7.64.0: the video job a ``video_create`` row accepted (``media_jobs``),
    # and the seconds of video it produced -- written when a poll first reads
    # the job completed with a stated length; NULL = not (yet) measured.
    ("media_job_id", "ALTER TABLE requests ADD COLUMN media_job_id TEXT"),
    (
        "output_video_seconds",
        "ALTER TABLE requests ADD COLUMN output_video_seconds REAL",
    ),
    # 7.69.1: the OAuth credential decision this request triggered -- one
    # stable code (``shared:adopted``, ``shared:refreshed+wrote-back``,
    # ``native:refreshed``, ...). NULL = a plain use, nothing decided.
    ("credential_event", "ALTER TABLE requests ADD COLUMN credential_event TEXT"),
    # 7.75.0: ``request_values`` ids of ``headers``, ``route_chain`` and
    # ``params`` stored once; see ``_STORED_ONCE_COLUMNS``. A ref and its
    # column are never both set. NULL = the column holds its own value (or is
    # NULL), which is every row an older version wrote. Never returned to a
    # reader: ``_row_to_dict`` puts the text back in the column and drops these.
    ("headers_ref", "ALTER TABLE requests ADD COLUMN headers_ref INTEGER"),
    ("route_chain_ref", "ALTER TABLE requests ADD COLUMN route_chain_ref INTEGER"),
    ("params_ref", "ALTER TABLE requests ADD COLUMN params_ref INTEGER"),
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
    # When the request this attempt belongs to happened. A copy of the parent
    # row's ``ts_epoch``, deliberately: ``reasoning_by_model`` filters on time
    # and the time lived only on ``requests``, so the plan was "walk all 571,665
    # attempts through a covering index, then one rowid lookup into ``requests``
    # per attempt". NULL means the row predates the column, which is why
    # ``reasoning_by_model`` keeps using the old query until the backfill marker
    # is set: a time filter against NULL would silently drop history.
    ("ts_epoch", "ALTER TABLE request_attempts ADD COLUMN ts_epoch REAL"),
    # Added in 7.4.0. How long this attempt took to produce its first answer
    # content, and its first sign of thinking, on its own clock.
    #
    # The only per-attempt first-token number that existed before was
    # ``params.response_shape.first_chunk_ms``, installed in three provider
    # modules and only on the success path: present on 16,362 attempt rows,
    # which is exactly the number of succeeded ones. The attempts that burn the
    # time are the failures, and they had nothing. These two are measured in
    # the executor's own chunk loop instead -- one place every provider family
    # passes through -- and are written whatever the verdict.
    #
    # NULL is "not measured": an attempt that never produced answer content, a
    # model that gave no reasoning signal, or a row that predates the columns.
    # Not backfillable, for the same reason ``requests.ttft_winner_ms`` is not.
    ("ttft_ms", "ALTER TABLE request_attempts ADD COLUMN ttft_ms REAL"),
    (
        "first_reasoning_ms",
        "ALTER TABLE request_attempts ADD COLUMN first_reasoning_ms REAL",
    ),
    # Which egress address this attempt actually went out through, denormalised
    # out of the ladder's last ``upstream`` row exactly as ``ladder_tries`` is,
    # so the analytics breakdown can group by address without scanning JSON.
    #
    # A label -- ``host:port`` with any ``user:pass`` removed -- never the URL.
    # A proxy password in the request log would be a worse leak than the thing
    # a proxy chain is trying to avoid.
    #
    # NULL means "not measured", which is every attempt on a provider with no
    # chain and every row that predates the column. The literal ``"direct"`` is
    # a chain rung the operator chose, and the two are different facts.
    ("proxy_label", "ALTER TABLE request_attempts ADD COLUMN proxy_label TEXT"),
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
    "ttft_winner_ms",
    "reasoning_tokens",
    "tool_catalogue_sha",
    "session_id",
    "agent_id",
    "parent_session_id",
    "project_dir",
    "origin_source",
    "keepalive_frames",
    "media_operation",
    "output_image_count",
    "media_bytes_out",
    "media_sha_out",
    "output_audio_seconds",
    "input_audio_seconds",
    "media_job_id",
    "output_video_seconds",
    "credential_event",
    "headers_ref",
    "route_chain_ref",
    "params_ref",
)

_REQUEST_INSERT_SQL = (
    "INSERT OR REPLACE INTO requests"
    f" ({', '.join(_REQUEST_INSERT_COLUMNS)})"
    f" VALUES ({', '.join('?' * len(_REQUEST_INSERT_COLUMNS))})"
)
# Where ``_record_to_row`` puts each stored-once column and its ref.
_STORED_ONCE_COLUMN_INDEXES = tuple(
    _REQUEST_INSERT_COLUMNS.index(column) for column in _STORED_ONCE_COLUMNS
)
_STORED_ONCE_REF_INDEXES = tuple(
    _REQUEST_INSERT_COLUMNS.index(ref) for ref in _STORED_ONCE_REFS
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
    "ts_epoch",
    "ttft_ms",
    "first_reasoning_ms",
    "proxy_label",
)

#: Key under which the attempt export hands the parsed ``request_attempts.params``
#: object to ``core.export``'s derived computations. Declared here rather than
#: in ``core.export`` because ``export`` already imports from this module and
#: the reverse import would be a cycle; ``export.ATTEMPT_PARAMS_KEY`` is this
#: name. Leading underscore on purpose: it is never an output column. The
#: params blob is a private diagnostic whose shape is not a contract, and the
#: columns derived from it are.
ATTEMPT_PARAMS_KEY = "_attempt_params"

# Skipped route attempts stored compactly (7.76.0); see the ``attempt_skip_sets``
# comment in ``_BODIES_SCHEMA``. Measured on a 2,817,195-attempt log: 2,496,969
# rows (88.6 %) were skipped attempts, 2,496,100 of them holding nothing but
# these eight values, and their rows and index entries filled 1.33 GB of pages.
#
# What one member of a stored set holds, in this order.
_SKIP_SET_FIELDS = ("attempt", "provider", "model_ref", "error_kind", "error_message")
# Shared by every skipped attempt of one request; one ``request_attempt_skips``
# row each.
_SKIP_SHARED_FIELDS = ("ts_epoch", "key_index", "key_label")
# Every other attempt column, derived rather than listed so a column added to
# ``_ATTEMPT_INSERT_COLUMNS`` later is one a compact attempt must not hold:
# an attempt with anything in one of these stays a row of ``request_attempts``.
_SKIP_EMPTY_FIELDS = tuple(
    column
    for column in _ATTEMPT_INSERT_COLUMNS
    if column not in ("request_id", "outcome", *_SKIP_SET_FIELDS, *_SKIP_SHARED_FIELDS)
)
# Text columns of a compact attempt; anything else in them keeps the row.
_SKIP_TEXT_FIELDS = (
    "provider",
    "model_ref",
    "error_kind",
    "error_message",
    "key_label",
)
# A set of one request's skipped attempts is a few hundred bytes; a reading
# process keeps the sets it has read, by id (never reused: AUTOINCREMENT).
_SKIP_SET_CACHE_MAX = 8_192
# The history conversion's read of one request's attempts: per column its
# storage class and its stored bytes, plus whether every column a compact
# attempt leaves empty is NULL.
_SKIP_FETCH_SQL = (
    "SELECT rowid, typeof(outcome), CAST(outcome AS BLOB),"
    " typeof(attempt), attempt, typeof(ts_epoch), ts_epoch,"
    " typeof(key_index), key_index, "
    + ", ".join(
        f"typeof({column}), CAST({column} AS BLOB)" for column in _SKIP_TEXT_FIELDS
    )
    + ", ("
    + " AND ".join(f"{column} IS NULL" for column in _SKIP_EMPTY_FIELDS)
    + ") FROM request_attempts WHERE request_id = ?"
)
_ATTEMPT_INSERT_SQL = (
    "INSERT OR REPLACE INTO request_attempts"
    f" ({', '.join(_ATTEMPT_INSERT_COLUMNS)}) VALUES"
    f" ({', '.join('?' * len(_ATTEMPT_INSERT_COLUMNS))})"
)
_SKIP_PACK_INSERT_SQL = (
    "INSERT OR REPLACE INTO request_attempt_skips"
    " (request_id, set_id, ts_epoch, key_index, key_label) VALUES (?, ?, ?, ?, ?)"
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
    "CREATE INDEX IF NOT EXISTS idx_requests_origin_v1 ON requests("
    " ts_epoch, project_dir, session_id)"
    " WHERE project_dir IS NOT NULL OR session_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS idx_requests_optimization_v1 ON requests("
    " optimization, ts_epoch, optimization_tokens_saved)"
    " WHERE optimization IS NOT NULL",
    # 7.67.0: the Analytics Media card reads only the rows a media endpoint
    # wrote. Without this its all-time window was a full scan -- measured
    # read-only on the real 473,493-row log: 25.0 s (SCAN requests), and 0.31 s
    # for a 7-day window over idx_requests_ts. Media rows are a sliver of the
    # log, so this index is tiny and walked only on media inserts.
    "CREATE INDEX IF NOT EXISTS idx_requests_media_v1 ON requests("
    " ts_epoch, media_operation)"
    " WHERE media_operation IS NOT NULL",
)
# ``idx_requests_origin_v1`` (7.43.0) serves the Session and Folder filters and
# the two breakdowns beside them. Every row written before 7.42.0 has neither
# value and is not in it. Measured on a synthetic 440,000-row log sized like
# the real one (1.6 KB a row, 43,000 rows a week, origin on the newest week),
# best of three:
#
# - folders, all time: 0.978 s -> 0.009 s (COVERING INDEX); 7 days
#   0.172 s -> 0.011 s.
# - the per-session breakdown, all time: 1.068 s -> 0.173 s.
# - a folder filter beside ``local=hide``: 1.185 s -> 0.096 s, through the
#   ``rowid IN`` form ``_where`` uses.
# - 1.28 s to build, one pass over ``requests``, on the writer thread.
#
# ``ts_epoch`` leads so a window is a range seek; the two origin columns follow
# so the filter subqueries never touch a row.


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
    """True when JSON encoding writes ``needle`` as one predictable run of bytes.

    ``json.dumps`` rewrites ``"``, ``\\`` and control characters in ways a
    substring cannot predict; anything else is written either as itself or,
    with ``ensure_ascii`` on, as its ``\\uXXXX`` escape (``_stored_probes``).
    """
    return not any(char in '"\\' or char < " " for char in needle)


def _stored_probes(term: str) -> tuple[bytes, ...]:
    """Byte strings, lower-cased, one of which a body blob holds wherever ``term`` is.

    ``pack_fields`` writes JSON with ``ensure_ascii`` on, so a non-ASCII
    character is stored as its ``\\uXXXX`` escape ("ș" as ``\\u0219``), never as
    its UTF-8 bytes. Until 7.77.1 the search looked only for the UTF-8 bytes,
    found them in no blob, and rejected every row before its text was read: a
    search for "ș", "ă", "—" or "→" found nothing. Both spellings are probed,
    so a blob written either way passes, and the decoded text decides. For a
    term of plain ASCII the two are the same bytes, which leaves every ASCII
    search exactly as it was. Empty for a term no substring of the blob is
    guaranteed to hold (``_is_json_transparent``): the decoded text alone
    decides.
    """
    if not _is_json_transparent(term):
        return ()
    literal = term.encode("utf-8", "surrogatepass").lower()
    escaped = json.dumps(term)[1:-1].encode("ascii").lower()
    return (literal,) if escaped == literal else (literal, escaped)


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


#: The outcomes ``latency_by_model`` measures, as a literal ``IN`` list.
#:
#: Written as ``IN (...)`` rather than the ``!= 'skipped'`` it is equivalent to,
#: and derived from the enum so the two cannot drift. ``outcome`` leads
#: ``idx_request_attempts_ts_v1``, and an inequality on a leading column cannot
#: seek: SQLite abandoned that index entirely and walked
#: ``idx_request_attempts_model_v1`` with a rowid lookup per attempt. Measured on
#: the real 571,665-row table, byte-identical answers both ways: all time
#: 1.772 s -> 0.871 s, a seven-day window 3.041 s -> 1.025 s. A dedicated
#: covering index was built and measured too (35.3 MB, 3.6 s to build) and the
#: planner ignored it, so it is not shipped.
_LATENCY_OUTCOMES_SQL = "a.outcome IN ({})".format(
    ", ".join(
        f"'{outcome.value}'"
        for outcome in (RouteAttemptOutcome.SUCCEEDED, RouteAttemptOutcome.FAILED)
    )
)

#: The attempt ``error_kind`` that means "the client hung up", not "the model
#: failed".
#:
#: Written by ``RouteLedger.interrupted()`` with the message "client cancelled
#: before the stream finished". The attempt is stored as ``outcome='failed'``
#: because from the route's point of view that model did not answer -- but it
#: is the *client's* clock that ended it, usually its own 300 s or 600 s idle
#: watchdog, so its 300-600 s latency describes the wait, not the model.
INTERRUPTED_ERROR_KIND = "interrupted"

#: The third ``latency_by_model`` group, beside ``succeeded`` and ``failed``.
LATENCY_INTERRUPTED_OUTCOME = "interrupted"

#: How ``latency_by_model`` derives the group a row belongs to.
#:
#: Deliberately a projection, not a filter. ``_LATENCY_OUTCOMES_SQL`` stays in
#: the ``WHERE`` exactly as it was -- byte for byte -- because it is the
#: documented seek on the leading ``outcome`` column of
#: ``idx_request_attempts_ts_v1`` (1.772 s -> 0.871 s all time). Adding
#: ``AND a.error_kind IS NOT 'interrupted'`` there would break that equality and
#: put an unindexed predicate in the seek path. A ``CASE`` in the select list
#: costs nothing at the index and moves the hang-ups into their own rows, so a
#: model's ``failed`` percentiles stop carrying the client's watchdog.
#:
#: Used by **both** the aggregate and the percentile pull: they bucket on the
#: same key, so if only one of them grouped this way the two would not line up
#: and every interrupted row would come back without percentiles.
_LATENCY_OUTCOME_GROUP_SQL = (
    f"CASE WHEN a.error_kind = '{INTERRUPTED_ERROR_KIND}'"
    f" THEN '{LATENCY_INTERRUPTED_OUTCOME}' ELSE a.outcome END"
)


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
    # This attempt's own time to first answer content, and to the first sign it
    # was thinking, both in milliseconds from the moment this model was asked.
    # None is "not measured, and not zero": no answer content ever arrived, no
    # reasoning signal was ever seen, or the row predates the columns.
    #
    # Measured per attempt because ``requests.ttft_ms`` cannot be: that clock
    # starts when the *client's* request arrived, so a fallback is charged with
    # every predecessor's stall. Across the 290,074-row log that is a mean of
    # 8.6 s at ``route_attempt = 0`` against 25.2 s above it -- roughly 16.5 s
    # of somebody else's time, filed under the model that rescued the request.
    #
    # Reasoning is deliberately NOT first content. A model that thinks for 40 s
    # and then answers in 200 ms is honestly slow on one axis and honestly fast
    # on the other, and folding them together makes every reasoning model read
    # as either instant or catastrophic.
    ttft_ms: float | None = None
    first_reasoning_ms: float | None = None
    # The egress address this attempt last went out through: ``host:port``
    # with any ``user:pass`` removed, or the literal ``"direct"`` for a chain
    # rung that deliberately uses none. None is "not measured" -- which is
    # every attempt on a provider with no proxy chain -- and is deliberately
    # distinct from "direct".
    proxy_label: str | None = None


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


@dataclass(frozen=True, slots=True)
class MediaJobRecord:
    """One accepted video job, as ``media_jobs`` stores it.

    Never a key and never an upstream URL: the key is pinned by index, masked
    label and fingerprint, and the file's address is asked for again when it
    is fetched.
    """

    job_id: str
    request_id: str
    provider: str
    model: str
    upstream_id: str
    created_at: float
    requested_model: str | None = None
    key_index: int | None = None
    key_fingerprint: str | None = None
    key_label: str | None = None
    proxy_label: str | None = None
    status: str | None = None
    status_raw: str | None = None
    progress: int | None = None
    seconds: float | None = None
    size: str | None = None
    prompt: str | None = None
    error: str | None = None
    usage_json: str | None = None
    updated_at: float | None = None
    completed_at: float | None = None


# Written in this order by ``insert_media_job``: the names are the record's
# own fields, and the placeholders are generated from the same tuple.
_MEDIA_JOB_INSERT_COLUMNS = (
    "job_id",
    "request_id",
    "provider",
    "model",
    "upstream_id",
    "created_at",
    "requested_model",
    "key_index",
    "key_fingerprint",
    "key_label",
    "proxy_label",
    "status",
    "status_raw",
    "progress",
    "seconds",
    "size",
    "prompt",
    "error",
    "usage_json",
    "updated_at",
    "completed_at",
)
_MEDIA_JOB_INSERT_SQL = (
    "INSERT INTO media_jobs"
    f" ({', '.join(_MEDIA_JOB_INSERT_COLUMNS)})"
    f" VALUES ({', '.join('?' * len(_MEDIA_JOB_INSERT_COLUMNS))})"
)
#: What a poll or a download may change on a job; anything else is refused.
_MEDIA_JOB_UPDATABLE = frozenset(
    {
        "status",
        "status_raw",
        "progress",
        "seconds",
        "size",
        "error",
        "usage_json",
        "updated_at",
        "completed_at",
        "content_sha",
        "content_bytes",
        "content_mime",
        "row_seconds_written",
    }
)
#: A job's request row is written by the batched writer, after the job row:
#: prune waits this long before calling a job without one an orphan.
_MEDIA_JOB_ORPHAN_GRACE_SECONDS = 3600.0

#: What ``media_stats`` sums per group: ``(payload name, requests column or
#: expression)``. Every one is NULL on a row that did not measure it, so each
#: is summed beside a count of the rows that did.
#:
#: The cost three (7.69.0) follow the cost card's rules: a bare ``SUM`` so a
#: group nothing priced sums to NULL, never to 0; reported and estimated
#: summed apart, never added into one another by a reader; and
#: ``cost_usd_measured`` is the count of priced rows -- the rest of the group's
#: requests are unpriced, and are counted as such rather than as $0.
_MEDIA_STAT_MEASURES: tuple[tuple[str, str], ...] = (
    ("images_out", "output_image_count"),
    ("audio_seconds_out", "output_audio_seconds"),
    ("audio_seconds_in", "input_audio_seconds"),
    ("video_seconds", "output_video_seconds"),
    ("bytes_out", "media_bytes_out"),
    ("cost_usd", "cost_usd"),
    ("cost_reported_usd", "CASE WHEN cost_source = 'provider' THEN cost_usd END"),
    ("cost_estimated_usd", "CASE WHEN cost_source <> 'provider' THEN cost_usd END"),
)
#: The counters ``media_stats`` adds up, a group's total being the sum of its
#: provider/model rows'.
_MEDIA_STAT_COUNTERS: tuple[str, ...] = (
    "requests",
    "succeeded",
    "failed",
    "cancelled",
    "video_jobs",
    "duration_count",
)


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
    # What the *winning* attempt's own first token cost, as opposed to what the
    # client waited. ``ttft_ms`` above is unchanged and always will be -- it has
    # meant "first byte the reader saw, fallbacks included" for 279,175 rows and
    # rewriting it would silently rebase two years of history. The two side by
    # side are the fact: ``ttft_ms - ttft_winner_ms`` is the time this request
    # lost to models that did not answer, and for a clean single-model request
    # they differ only by the one envelope frame the client-side clock counts
    # and the attempt-side clock does not (``_observe`` stamps on the first SSE
    # chunk of any kind, including ``message_start``; the attempt stamps on the
    # first chunk carrying answer content).
    #
    # None is "no attempt won, or the row predates the column". Never zero.
    ttft_winner_ms: float | None = None
    #: Reasoning tokens the host reported for this request. Already priced at
    #: the reasoning rate since 6.54.0 and discarded until 7.9.0; NULL means
    #: not measured, never zero.
    reasoning_tokens: int | None = None
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
    # The tools array the client sent, held by reference until the writer
    # thread fingerprints it. Never a column: the writer turns it into
    # ``tool_catalogue`` below and the request row keeps only its hash, so the
    # hashing costs the request path nothing.
    tools: tuple[Any, ...] = ()
    # Whether the tool definitions themselves may be stored. Follows
    # ``request_log_capture_bodies``, because a definition is request content;
    # the hashes and names are kept either way.
    keep_tool_definitions: bool = True
    # Filled by the writer from ``tools``; None when there were no tools.
    tool_catalogue: ToolCatalogue | None = None
    # Where the request came from (7.42.0); see ``core/request_origin.py``.
    # All five None unless the client stated them and capture is on.
    session_id: str | None = None
    agent_id: str | None = None
    parent_session_id: str | None = None
    project_dir: str | None = None
    origin_source: str | None = None
    # Empty-delta keepalive frames sent (7.47.0). None unless
    # STREAM_KEEPALIVE_MODE=frames ran on this stream.
    keepalive_frames: int | None = None
    # Media endpoints (7.60.0). None on every chat row.
    media_operation: str | None = None
    output_image_count: int | None = None
    media_bytes_out: int | None = None
    media_sha_out: str | None = None
    output_audio_seconds: float | None = None
    input_audio_seconds: float | None = None
    media_job_id: str | None = None
    output_video_seconds: float | None = None
    #: The OAuth credential decision this request triggered (7.69.1), or
    #: ``None`` for a plain use.
    credential_event: str | None = None
    #: Generated outputs to link in ``request_media`` (sha256, mime, bytes,
    #: stored). Written by the writer thread with the row.
    media_outputs: tuple[MediaOutputRecord, ...] = ()
    #: ``MEDIA_STORE_MAX_MB`` in bytes, as it stood when this request was
    #: served (7.68.0). When the row stored a file, the writer trims the media
    #: store to it once the batch is committed. 0 sets no cap. Never a column.
    media_store_max_bytes: int = 0
    #: Prices this row on the writer thread just before it is written (7.69.0,
    #: media rows): ``() -> (cost_usd, cost_source)``. ``core`` holds no
    #: pricing ladder, so the callable is the caller's, closed over what the
    #: request measured. Catalogue lookups are milliseconds no request should
    #: wait for -- the reason the tool catalogue is fingerprinted here too --
    #: and a streamed answer's row is finished from a fire-and-forget task
    #: that must not gain a thread hop. Used once, then dropped. Never a column.
    pricer: RowPricer | None = field(default=None, repr=False, compare=False)

    @property
    def ts_iso(self) -> str:
        return datetime.fromtimestamp(self.ts_epoch, tz=UTC).isoformat()


#: One generated file's content address, recorded or re-recorded. ``stored``
#: only ever rises on a conflict -- a later request that kept the file makes
#: the address stored even if an earlier one recorded metadata only -- and
#: when it rises from 0, ``created_at`` moves to that moment (7.68.0): the
#: file on disk is then that new, and ``MEDIA_STORE_MAX_MB`` deletes the
#: oldest stored files first.
_MEDIA_BLOB_UPSERT_SQL = (
    "INSERT INTO media_blobs (sha256, mime, bytes, created_at, stored)"
    " VALUES (?, ?, ?, ?, ?)"
    " ON CONFLICT(sha256) DO UPDATE SET"
    " created_at = CASE WHEN media_blobs.stored = 0 AND excluded.stored = 1"
    " THEN excluded.created_at ELSE media_blobs.created_at END,"
    " stored = MAX(media_blobs.stored, excluded.stored)"
)


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
        # A saved REQUEST_LOG_COMPRESS_BODIES, handed to the writer thread to
        # adopt between two batches; see ``retune``.
        self._tuning_lock = threading.Lock()
        self._pending_compress_bodies: bool | None = None
        # Dictionaries are immutable once written, so caching them by id is
        # safe for the lifetime of the process.
        self._dict_cache: dict[int, Any] = {}
        self._dict_lock = threading.Lock()
        self._active_dict_id: int | None = None
        # The dictionary each kind of content is written with now, and when it
        # was trained: ``kind -> (id, created_at)``. Set by the writer thread.
        self._kind_dicts: dict[str, tuple[int, float]] = {}
        # ``wire_dictionaries`` ids live in their own table, so their cache is
        # their own: id 3 there and id 3 in ``body_dictionaries`` differ.
        self._wire_dict_cache: dict[int, Any] = {}
        # Background dictionary training (7.73.0); see
        # ``_maybe_refresh_dictionaries``. The trainer thread only reads and
        # trains; it hands each result over, and the writer thread inserts it
        # between two batches. ``_trainer_conn_lock`` is held while the trainer
        # has the database open, so ``close`` can return with it shut.
        self._trainer: threading.Thread | None = None
        self._trainer_lock = threading.Lock()
        self._trainer_conn_lock = threading.Lock()
        self._trained: list[tuple[str, bytes, float]] = []
        self._dict_train_failed_at: dict[str, float] = {}
        # Kinds whose last training found too few samples to learn from. The
        # history conversion waits for a dictionary unless its kind is here.
        self._dict_too_few_samples: set[str] = set()
        # History conversion (7.74.0); writer thread only. See
        # ``_run_history_conversion``.
        self._history_done = False
        self._history_retry_at = 0.0
        self._history_announced = False
        self._history_logged_at = 0.0
        self._history_index_checked = False
        # The unused index is read ahead of its drop on a thread of its own;
        # see ``_drop_unused_attempt_index``.
        self._index_warm = threading.Event()
        self._index_warmer: threading.Thread | None = None
        self._history_space_mode: int | None = None
        self._space_step_pages = _SPACE_STEP_PAGES_START
        # Set once the writer thread has loaded the dictionaries and made its
        # start-up decision about training, so a caller can tell "nothing was
        # due" from "not decided yet".
        self._dictionaries_checked = threading.Event()
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=self._queue_max_size)
        self._inserts_since_prune = 0
        # Session-level "stop asking": set when the historical cost backfill
        # finishes, when its marker is already there, or when it gave up. The
        # writer's idle branch runs every 0.25 s and must not pay a meta read
        # that often for the rest of the process's life.
        self._cost_backfill_done = False
        # The same "stop asking" flag for the attempt-timestamp walk beside it.
        self._attempts_ts_backfill_done = False
        # The folder backfill runs only when asked. Set by
        # ``request_origin_backfill`` from any thread, read and cleared by the
        # writer thread; the counters are what the status endpoint reports.
        self._origin_backfill_requested = threading.Event()
        self._origin_backfill_lock = threading.Lock()
        self._origin_backfill_progress: dict[str, Any] = {
            "running": False,
            "scanned": 0,
            "filled": 0,
            "started_at": None,
            "finished_at": None,
            "error": None,
        }
        # Monotonic time of the last tool-catalogue sweep; see ``prune``.
        self._last_tool_sweep: float | None = None
        # ``request_values`` texts by id, for readers on any thread; see
        # ``_VALUE_CACHE_MAX``.
        self._value_cache: dict[int, str] = {}
        # Parsed ``attempt_skip_sets`` by id, for readers on any thread; see
        # ``_SKIP_SET_CACHE_MAX``.
        self._skip_set_cache: dict[int, tuple[tuple[Any, ...], ...]] = {}
        # The orphan sweeps the next prune pass owes over a whole table; see
        # ``prune``. All of them to begin with, so the first pass of a process
        # is exactly the pass every prune ran before 7.72.2. Any thread may
        # add to it; only ``prune`` takes from it.
        self._sweep_lock = threading.Lock()
        self._full_sweeps_owed: set[str] = set(_ORPHAN_SWEEP_TABLES)
        # Used by the writer thread only: it remembers the tools it last
        # hashed, so an unchanged array is recognised rather than re-serialised.
        self._tool_fingerprinter = ToolFingerprinter()
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
            name="mcc-request-log-writer",
            daemon=True,
        )
        self._writer.start()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def retune(
        self,
        *,
        max_rows: int,
        text_max_chars: int,
        compression_level: int,
        queue_max_size: int,
        compress_bodies: bool,
    ) -> None:
        """Adopt a saved tuning without reopening the database.

        The store is shared for the life of the process, so the numbers it was
        built with used to be the numbers until a restart. Each one is a plain
        value the writer reads where it uses it, so each can move while the
        store runs -- with the one care each needs:

        * the queue bound is changed under the queue's own mutex, and waiters
          are woken when it grows, so ``queue.Queue`` never sees a half-made
          change; records already queued above a smaller bound stay queued;
        * ``compress_bodies`` decides two things about the same batch -- whether
          the text goes inline and whether it is packed -- so the writer thread
          adopts it between batches rather than this thread flipping it under
          a batch in flight;
        * the three integers are single assignments the writer reads per use.
        """

        bound = max(1, int(queue_max_size))
        with self._queue.mutex:
            grew = bound > self._queue.maxsize
            self._queue.maxsize = bound
            self._queue_max_size = bound
            if grew:
                self._queue.not_full.notify_all()
        self._max_rows = max(0, int(max_rows))
        self._text_max_chars = max(0, int(text_max_chars))
        self._compression_level = int(compression_level)
        with self._tuning_lock:
            self._pending_compress_bodies = bool(compress_bodies)

    def _adopt_pending_tuning(self) -> None:
        """Writer thread only: take a saved ``compress_bodies`` between batches."""

        with self._tuning_lock:
            pending = self._pending_compress_bodies
            self._pending_compress_bodies = None
        if pending is not None:
            self._compress_bodies = pending

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

    def _wire_dictionary(self, dict_id: int) -> Any:
        """Return the cached ``ZstdDict`` for a ``wire_dictionaries`` id."""
        cached = self._wire_dict_cache.get(dict_id)
        if cached is not None:
            return cached
        with self._dict_lock:
            cached = self._wire_dict_cache.get(dict_id)
            if cached is not None:
                return cached
            with self._connection() as conn:
                row = conn.execute(
                    "SELECT content FROM wire_dictionaries WHERE id = ?", (dict_id,)
                ).fetchone()
            if row is None:
                return None
            loaded = zstd.ZstdDict(bytes(row[0]))
            self._wire_dict_cache[dict_id] = loaded
            return loaded

    def _encode_wire_body(
        self, text: str | None, *, level: int, compress: bool
    ) -> str | bytes | None:
        """Return what ``request_attempts.wire_body`` stores for ``text``.

        The envelope described at ``_WIRE_ENVELOPE_V1`` when it is smaller than
        the text, and the text itself otherwise -- an empty or tiny snapshot,
        or any snapshot while ``REQUEST_LOG_COMPRESS_BODIES`` is off. NULL
        stays NULL: no snapshot is not an empty one.
        """
        if text is None or not compress:
            return text
        raw = text.encode("utf-8", "surrogatepass")
        active = self._kind_dicts.get(_DICT_KIND_WIRE)
        dict_id = active[0] if active is not None else 0
        frame = zstd.compress(
            raw,
            level=level,
            zstd_dict=self._wire_dictionary(dict_id) if dict_id else None,
        )
        envelope = bytes((_WIRE_ENVELOPE_V1,)) + _varint(dict_id) + frame
        return envelope if len(envelope) < len(raw) else text

    def _decode_wire_body(self, stored: Any) -> str | None:
        """Return the JSON text a stored ``wire_body`` holds, in either encoding.

        TEXT is returned as it is; a BLOB is unwrapped from its envelope. A
        value that cannot be decoded reads as no snapshot, as it did before
        7.73.0, and says so in the log rather than failing the whole request.
        """
        if stored is None or isinstance(stored, str):
            return stored
        try:
            return self._unwrap_wire_envelope(bytes(stored)).decode(
                "utf-8", "surrogatepass"
            )
        except (zstd.ZstdError, ValueError) as exc:
            logger.warning("Request log wire snapshot decode failed: {}", exc)
            return None

    def _unwrap_wire_envelope(self, data: bytes) -> bytes:
        """Return the UTF-8 bytes a ``wire_body`` envelope holds, or raise.

        The one decoder: the reader above and the history conversion's proof
        both go through it, so what the proof checked is what a reader gets.
        """
        if not data or data[0] != _WIRE_ENVELOPE_V1:
            raise ValueError("unknown wire snapshot envelope")
        dict_id, offset = _read_varint(data, 1)
        zstd_dict = None
        if dict_id:
            zstd_dict = self._wire_dictionary(dict_id)
            if zstd_dict is None:
                raise ValueError(f"wire dictionary {dict_id} is missing")
        return zstd.decompress(data[offset:], zstd_dict=zstd_dict)

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
        for term in terms:
            stored = _stored_probes(term)
            if stored and not any(
                probe in candidate for probe in stored for candidate in lowered
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
        # A term the blob must hold, in one of the spellings ``_stored_probes``
        # names, wherever the text holds it: absent from the encoded bytes
        # proves absent from the text. The converse does not hold -- it can
        # match structure -- so a survivor is still verified against the
        # decoded content below.
        for term in terms:
            stored = _stored_probes(term)
            if stored and not any(probe in lowered_raw for probe in stored):
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
                self._ensure_dictionary_kind_column(conn)
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
        its stored wire body. Versioned name per the index rule.

        ``idx_request_attempts_model_v1`` (``model_ref, outcome,
        reasoning_emitted, request_id``) was created here until 7.73.0. No
        query chose it once the index below existed, so it is no longer
        created, and the writer drops it in the background
        (``_drop_unused_attempt_index``) -- never here, on the constructing
        thread, because dropping 284 MB is seconds of work.
        """

        with contextlib.suppress(sqlite3.Error):
            # The same query's time-windowed shape, once ``ts_epoch`` exists on
            # the attempt: filter by time on the attempt rather than on its
            # parent. Covering on purpose -- it carries every column the
            # attempts side of the query reads. Measured on the real 571,665-row
            # table: the narrow ``(outcome, ts_epoch, model_ref)`` the spec asked
            # for takes 1.758 s to 1.649 s for 33.8 MB, which is not worth
            # having; this one takes it to 0.849 s for 56.1 MB, and it is the
            # difference between a scan of every attempt and a seek. The
            # remaining 0.8 s is the rowid lookup into ``requests`` for
            # ``thinking_chars``, which no index on this table can remove.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_request_attempts_ts_v1"
                " ON request_attempts(outcome, ts_epoch, model_ref,"
                " reasoning_emitted, request_id)"
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
        """Index only the rows a dashboard panel ever looks at.

        The first two of these panels filter on a column that is NULL on the
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

        ``idx_requests_origin_v1`` (7.43.0) is the third, and the one that is
        *not* small for long: nearly every request since 7.42.0 carries a
        session id, so it grows with new traffic. What it leaves out is the
        history before the columns existed, which is exactly what made the
        Session and Folder queries scan the whole log. Its measurements sit
        beside ``_PARTIAL_INDEXES``.

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
                    "SELECT id, headers, headers_ref FROM requests"
                    " WHERE harness IS NULL LIMIT ?",
                    (_HARNESS_CHUNK_ROWS,),
                ).fetchall()
                if not rows:
                    break
                # Headers stored once (7.75.0) are read back like any reader's.
                values = _load_request_values(
                    conn, {int(row[2]) for row in rows if row[2] is not None}
                )
                updates = [
                    (
                        harness_from_headers(
                            _stored_headers(
                                values.get(int(row[2]))
                                if row[2] is not None
                                else row[1]
                            )
                        ).harness,
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

    def _ensure_attempts_ts_backfill(self, conn: sqlite3.Connection) -> None:
        """Copy each attempt's parent request instant onto the attempt.

        ``reasoning_by_model`` asks a question about a window of time, and the
        time lived only on ``requests`` -- so the plan walked all 571,665
        attempts through a covering index and did one rowid lookup per attempt
        to find out when it happened. With the column and its index the filter
        is a seek: measured 1.758 s to 0.849 s on the real log.

        Chunked by rowid and committed per chunk, and it yields to the writer
        the moment a request is queued behind it -- the same shape as the cost
        backfill beside it, for the same reason.

        The cursor is real, unlike the harness backfill's. An attempt whose
        parent request has been pruned has nothing to copy, so it stays NULL
        forever; without a cursor the walk would re-read those rows on every
        chunk and never finish.
        """
        if self._attempts_ts_backfill_done:
            return
        try:
            if self._meta_get(conn, _ATTEMPTS_TS_BACKFILL_KEY) is not None:
                self._attempts_ts_backfill_done = True
                return
            resumed = self._meta_get(conn, _ATTEMPTS_TS_BACKFILL_THROUGH_KEY)
            cursor = int(resumed) if resumed and resumed.isdigit() else 0
            started = time.monotonic()
            filled = 0
            while True:
                rowids = [
                    int(row[0])
                    for row in conn.execute(
                        "SELECT rowid FROM request_attempts WHERE rowid > ?"
                        " AND ts_epoch IS NULL ORDER BY rowid LIMIT ?",
                        (cursor, _ATTEMPTS_TS_CHUNK_ROWS),
                    )
                ]
                if not rowids:
                    break
                with conn:
                    conn.execute(
                        "UPDATE request_attempts SET ts_epoch = (SELECT r.ts_epoch"
                        " FROM requests r WHERE r.id = request_attempts.request_id)"
                        " WHERE rowid BETWEEN ? AND ? AND ts_epoch IS NULL",
                        (rowids[0], rowids[-1]),
                    )
                    cursor = rowids[-1]
                    self._meta_set(conn, _ATTEMPTS_TS_BACKFILL_THROUGH_KEY, str(cursor))
                filled += len(rowids)
                if not self._queue.empty():
                    return
            with conn:
                self._meta_set(conn, _ATTEMPTS_TS_BACKFILL_KEY, str(time.time()))
        except sqlite3.Error as exc:
            # Same rule as its siblings: a concurrent store may have won the
            # race, and its marker means "already done", not "corrupt".
            logger.warning("Request log attempt timestamp backfill skipped: {}", exc)
            self._attempts_ts_backfill_done = True
            return
        self._attempts_ts_backfill_done = True
        if filled:
            logger.info(
                "Request log dated {} route attempts in {:.1f}s",
                filled,
                time.monotonic() - started,
            )

    # ------------------------------------------------------ history conversion

    def _run_history_conversion(self, conn: sqlite3.Connection) -> None:
        """Writer thread, idle only: make the stored history smaller, losslessly.

        7.73.0 compresses what it writes; this converts what was written
        before, and hands the freed space back to the filesystem. Five parts,
        in this order, each step a transaction of its own that carries its
        bookkeeping (``_HISTORY_CONVERSION_KEY``), so a kill between any two
        statements leaves the database consistent and the walk resumes where
        the last committed step ended:

        1. drop ``idx_request_attempts_model_v1``, which nothing reads;
        2. wire snapshots still stored as TEXT become the 7.73.0 envelope;
        3. bodies are recompressed with the newest dictionary of their kind;
        4. (7.75.0) request metadata still stored inline is stored once;
        5. the pages 1-4 freed go back to the filesystem, a few at a time.

        A value is replaced only once its new form has been decoded by the
        reader's own decoder and found byte-identical to the original (and,
        for a body, hashed back to its address); anything else stays exactly
        as it was and is counted. Each step stops on a time budget and the
        whole slice the moment a request is queued, so a request's row waits
        behind at most one step.
        """
        if not self._compress_bodies:
            return
        if self._history_done and self._history_index_checked:
            return
        now = time.monotonic()
        if now < self._history_retry_at:
            return
        try:
            # Checked every start, even after the conversion is done: an older
            # version run in between recreates the index.
            if not self._history_index_checked and self._drop_unused_attempt_index(
                conn
            ):
                self._history_index_checked = True
                if not self._queue.empty():
                    return
            if self._history_done:
                return
            deadline = now + _HISTORY_IDLE_SLICE_SECONDS
            while self._queue.empty() and time.monotonic() < deadline:
                if not self._history_step(conn):
                    break
        except sqlite3.Error as exc:
            with contextlib.suppress(sqlite3.Error):
                conn.rollback()
            # Another process may hold the write lock for longer than the busy
            # timeout. Nothing is lost by trying again later.
            logger.warning("Request log history conversion paused: {}", exc)
            self._history_retry_at = time.monotonic() + 60.0

    @staticmethod
    def _new_history_state(conn: sqlite3.Connection) -> dict[str, Any]:
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        phase = {
            "through": 0,
            "end": None,
            "converted": 0,
            "kept": 0,
            "failed": 0,
            "bytes_before": 0,
            "bytes_after": 0,
            "done_at": None,
        }
        return {
            "started_at": time.time(),
            "page_size": page_size,
            "bytes_at_start": page_count * page_size,
            # Pages already free before the conversion freed any: never handed
            # back by it, because they may hold rows somebody deleted (IV.13).
            "freelist_at_start": int(
                conn.execute("PRAGMA freelist_count").fetchone()[0]
            ),
            **{name: dict(phase) for name in _HISTORY_PHASES},
            "freed_pages": 0,
            "returned_pages": 0,
            "done_at": None,
            "bytes_at_end": None,
        }

    def _load_history_state(self, conn: sqlite3.Connection) -> dict[str, Any]:
        raw = self._meta_get(conn, _HISTORY_CONVERSION_KEY)
        if raw:
            with contextlib.suppress(ValueError, TypeError):
                state = json.loads(raw)
                if isinstance(state, dict):
                    return self._reopen_history_state(conn, state)
        return self._new_history_state(conn)

    @staticmethod
    def _reopen_history_state(
        conn: sqlite3.Connection, state: dict[str, Any]
    ) -> dict[str, Any]:
        """A document an earlier release wrote, with this release's parts added.

        A part it does not have starts from the beginning. If the earlier
        release had finished, the document is open again: its own parts stay
        done, and the space it already handed back stays counted, but the
        freelist is measured afresh -- pages free now were freed by somebody
        else, and may hold a deleted row (IV.13). An older release reading the
        reopened document runs nothing it does not know and closes it again;
        the next start of this one reopens it.
        """
        missing = [
            name
            for name in _HISTORY_PHASES
            if not isinstance(state.get(name), dict)
            or state[name].get("done_at") is None
        ]
        added = [
            name for name in _HISTORY_PHASES if not isinstance(state.get(name), dict)
        ]
        if not added and (state.get("done_at") is None or not missing):
            return state
        for name in added:
            state[name] = {
                "through": 0,
                "end": None,
                "converted": 0,
                "kept": 0,
                "failed": 0,
                "bytes_before": 0,
                "bytes_after": 0,
                "done_at": None,
            }
        if state.get("done_at") is not None and missing:
            page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            returned = int(state.get("returned_pages") or 0)
            state["done_at"] = None
            state["bytes_at_end"] = None
            state["reopened_at"] = time.time()
            state["reopened_for"] = missing
            state["bytes_at_reopen"] = page_count * page_size
            state["freelist_at_start"] = int(
                conn.execute("PRAGMA freelist_count").fetchone()[0]
            )
            # Nothing owed from before: what is free now is somebody else's.
            state["freed_pages"] = returned
            state["returned_at_reopen"] = returned
        return state

    def _save_history_state(
        self, conn: sqlite3.Connection, state: dict[str, Any]
    ) -> None:
        self._meta_set(conn, _HISTORY_CONVERSION_KEY, json.dumps(state, sort_keys=True))

    @staticmethod
    def _freelist_count(conn: sqlite3.Connection) -> int:
        return int(conn.execute("PRAGMA freelist_count").fetchone()[0])

    def _drop_unused_attempt_index(self, conn: sqlite3.Connection) -> bool:
        """Drop ``idx_request_attempts_model_v1`` if it exists; once a process.

        Guarded and idempotent: an older version recreates it at start, and
        the next start of this one drops it again. The pages it held go on the
        freelist and are counted as the conversion's own, so step 4 returns
        them -- they held only copies of columns that are still there.

        The drop is one statement that reads every page of the index. Cold,
        that measured 23.0 s on the full-size copy against 1.7 s warm, and the
        writer cannot take a request while it runs. So a thread of its own
        first reads the index through a connection of its own -- a WAL reader,
        which never blocks the writer -- and the drop waits for it. True once
        the index is gone; False while it is still being read.
        """
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?",
            (_UNUSED_ATTEMPT_INDEX,),
        ).fetchone()
        if exists is None:
            return True
        if not self._index_warm.is_set():
            if self._index_warmer is None:
                self._index_warmer = threading.Thread(
                    target=self._warm_unused_index,
                    name="mcc-request-log-index-reader",
                    daemon=True,
                )
                self._index_warmer.start()
            return False
        started = time.perf_counter()
        conn.execute("BEGIN IMMEDIATE")
        try:
            state = self._load_history_state(conn)
            before = self._freelist_count(conn)
            conn.execute(f"DROP INDEX IF EXISTS {_UNUSED_ATTEMPT_INDEX}")
            freed = max(0, self._freelist_count(conn) - before)
            state["freed_pages"] = int(state.get("freed_pages", 0)) + freed
            self._save_history_state(conn, state)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        logger.info(
            "Request log dropped the unused index {} in {:.1f}s ({} pages freed)",
            _UNUSED_ATTEMPT_INDEX,
            time.perf_counter() - started,
            freed,
        )
        return True

    def _warm_unused_index(self) -> None:
        """Index reader thread: read every entry of the index about to go.

        Only reads, on its own connection, in slices, and stops between two
        slices when the store closes. The writer drops the index once this has
        set ``_index_warm``; a failure sets it too, and the drop then simply
        reads the index itself.
        """
        started = time.perf_counter()
        try:
            with self._trainer_conn_lock, self._connection() as conn:
                cursor = conn.execute(
                    f"SELECT model_ref FROM request_attempts INDEXED BY"
                    f" {_UNUSED_ATTEMPT_INDEX}"
                )
                while not self._closed.is_set():
                    if not cursor.fetchmany(_INDEX_WARM_FETCH_ROWS):
                        break
        except sqlite3.Error as exc:
            logger.warning(
                "Request log could not pre-read {}: {}", _UNUSED_ATTEMPT_INDEX, exc
            )
        finally:
            self._index_warm.set()
        logger.info(
            "Request log read {} ahead of dropping it in {:.1f}s",
            _UNUSED_ATTEMPT_INDEX,
            time.perf_counter() - started,
        )

    def _history_step(self, conn: sqlite3.Connection) -> bool:
        """One bounded step of the conversion; False when there is nothing to do now.

        The parts run in ``_HISTORY_PHASES`` order, one step of the first that
        is not done. A document an earlier release finished comes back open
        from ``_load_history_state`` when this release added a part to it, so
        the earlier release's ``done_at`` stops nothing here.
        """
        state = self._load_history_state(conn)
        if state.get("done_at") is not None:
            self._history_done = True
            return False
        pending = [name for name in _HISTORY_PHASES if state[name]["done_at"] is None]
        if not self._history_announced:
            self._history_announced = True
            self._history_logged_at = time.monotonic()
            self._announce_history(state, pending)
        progressed = False
        if pending and pending[0] == "wire":
            progressed = self._convert_wire_step(conn)
        elif pending and pending[0] == "bodies":
            progressed = self._recompress_body_step(conn)
        elif pending and pending[0] == "metadata":
            progressed = self._convert_metadata_step(conn)
        elif pending and pending[0] == "skipped":
            progressed = self._convert_skipped_step(conn)
        if self._queue.empty() and self._return_space_step(conn):
            progressed = True
        state = self._load_history_state(conn)
        converted = all(state[name]["done_at"] is not None for name in _HISTORY_PHASES)
        if converted and not self._space_owed(conn, state):
            self._finish_history(conn)
            return False
        self._log_history_progress(state)
        return progressed

    @staticmethod
    def _announce_history(state: dict[str, Any], pending: list[str]) -> None:
        """The one line a start of the conversion logs: which parts are left."""
        resumed = any(int(state[name]["through"] or 0) for name in pending)
        recompressed = [
            label
            for name, label in (("wire", "wire snapshots"), ("bodies", "bodies"))
            if name in pending
        ]
        work: list[str] = []
        if recompressed:
            work.append(f"older {' and '.join(recompressed)} are recompressed")
        if "metadata" in pending:
            work.append("repeated request metadata of older rows is stored once")
        if "skipped" in pending:
            work.append("older skipped route attempts are stored compactly")
        on_disk = int(state.get("bytes_at_reopen") or state["bytes_at_start"]) / 1e9
        if not work:
            logger.info(
                "Request log history conversion resumed: the space it freed is"
                " handed back to the disk ({:.2f} GB on disk)",
                on_disk,
            )
            return
        logger.info(
            "Request log history conversion {}: {} in the background, each"
            " checked before it is replaced ({:.2f} GB on disk)",
            "resumed" if resumed else "started",
            ", and ".join(work),
            on_disk,
        )

    def _convert_wire_step(self, conn: sqlite3.Connection) -> bool:
        """Turn the next TEXT wire snapshots into the envelope; one transaction.

        With the newest wire dictionary only: without one the ratio is 4.67x
        against 37.6x, so the step waits for the trainer -- unless the trainer
        found too few snapshots to learn from, and then it goes without.
        """
        active = self._kind_dicts.get(_DICT_KIND_WIRE)
        if active is None and _DICT_KIND_WIRE not in self._dict_too_few_samples:
            return False
        dict_id = active[0] if active is not None else 0
        if dict_id and self._wire_dictionary(dict_id) is None:
            return False
        level = self._compression_level
        budget_end = time.perf_counter() + _HISTORY_STEP_SECONDS
        conn.execute("BEGIN IMMEDIATE")
        try:
            state = self._load_history_state(conn)
            phase = state["wire"]
            if phase["end"] is None:
                phase["end"] = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(rowid), 0) FROM request_attempts"
                    ).fetchone()[0]
                )
            freelist_before = self._freelist_count(conn)
            cursor = int(phase["through"])
            updates: list[tuple[bytes, int]] = []
            finished = False
            while True:
                rows = conn.execute(
                    "SELECT rowid, CASE WHEN typeof(wire_body) = 'text'"
                    " THEN CAST(wire_body AS BLOB) END FROM request_attempts"
                    " WHERE rowid > ? ORDER BY rowid LIMIT ?",
                    (cursor, _HISTORY_FETCH_ROWS),
                ).fetchall()
                if not rows:
                    finished = True
                    break
                for row in rows:
                    cursor = int(row[0])
                    original = row[1]
                    if original is not None:
                        original = bytes(original)
                        envelope = self._proven_wire_envelope(original, level)
                        if envelope is None:
                            phase["failed"] += 1
                        elif envelope is original:
                            phase["kept"] += 1
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
                conn.executemany(
                    "UPDATE request_attempts SET wire_body = ? WHERE rowid = ?",
                    updates,
                )
            phase["through"] = cursor
            if finished:
                phase["done_at"] = time.time()
            state["freed_pages"] = int(state["freed_pages"]) + max(
                0, self._freelist_count(conn) - freelist_before
            )
            self._save_history_state(conn, state)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if finished and phase["failed"]:
            logger.warning(
                "Request log history conversion left {} wire snapshots as they"
                " were: their compressed form did not decode to the same bytes",
                phase["failed"],
            )
        return True

    def _proven_wire_envelope(self, original: bytes, level: int) -> bytes | None:
        """The envelope for one stored TEXT snapshot, proven to decode back to it.

        Returns ``original`` itself when the envelope would not be smaller (the
        row stays TEXT, as the writer would store it), and None when the proof
        fails or anything raises -- the row is then left exactly as it is.
        """
        try:
            text = original.decode("utf-8")
            stored = self._encode_wire_body(text, level=level, compress=True)
            if not isinstance(stored, bytes):
                return original
            if self._unwrap_wire_envelope(stored) != original:
                return None
            if self._decode_wire_body(stored) != text:
                return None
            return stored
        except Exception:  # every failure means "leave the row alone"
            return None

    def _newest_kind_dictionaries(
        self, conn: sqlite3.Connection
    ) -> dict[str, int | None] | None:
        """The dictionary each body kind is recompressed with, or None to wait.

        Only a dictionary trained for that kind (7.73.0 onward) counts: the
        legacy one is what most history already uses. A kind with none waits
        for the trainer, unless the trainer found too few samples to train
        one, and then its bodies stay as they are.
        """
        targets: dict[str, int | None] = {}
        for kind in (_DICT_KIND_PROMPT, _DICT_KIND_REST):
            row = conn.execute(
                "SELECT id FROM body_dictionaries WHERE kind = ?"
                " ORDER BY id DESC LIMIT 1",
                (kind,),
            ).fetchone()
            if row is not None:
                targets[kind] = int(row[0])
            elif kind in self._dict_too_few_samples:
                targets[kind] = None
            else:
                return None
        return targets

    def _recompress_body_step(self, conn: sqlite3.Connection) -> bool:
        """Recompress the next bodies with their kind's newest dictionary.

        ``body_blobs.sha`` hashes the uncompressed content, so the address --
        and every ``request_bodies`` row naming it -- stays as it is. The old
        dictionary stays too: nothing ever deletes one.
        """
        targets = self._newest_kind_dictionaries(conn)
        if targets is None:
            return False
        level = self._compression_level
        budget_end = time.perf_counter() + _HISTORY_STEP_SECONDS
        conn.execute("BEGIN IMMEDIATE")
        try:
            state = self._load_history_state(conn)
            phase = state["bodies"]
            if phase["end"] is None:
                phase["end"] = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(rowid), 0) FROM body_blobs"
                    ).fetchone()[0]
                )
            freelist_before = self._freelist_count(conn)
            cursor = int(phase["through"])
            updates: list[tuple[int, bytes, int]] = []
            finished = False
            while True:
                rows = conn.execute(
                    "SELECT b.rowid, b.sha, b.dict_id,"
                    " EXISTS (SELECT 1 FROM request_bodies WHERE input_sha = b.sha),"
                    " EXISTS (SELECT 1 FROM request_bodies WHERE sha = b.sha)"
                    " FROM body_blobs b WHERE b.rowid > ? ORDER BY b.rowid LIMIT ?",
                    (cursor, _HISTORY_FETCH_ROWS),
                ).fetchall()
                if not rows:
                    finished = True
                    break
                for row in rows:
                    cursor = int(row[0])
                    as_prompt, as_rest = bool(row[3]), bool(row[4])
                    kind = (
                        _DICT_KIND_PROMPT
                        if as_prompt and not as_rest
                        else _DICT_KIND_REST
                        if as_rest and not as_prompt
                        else None
                    )
                    target = targets.get(kind) if kind is not None else None
                    dict_id = None if row[2] is None else int(row[2])
                    if target is None or dict_id == target:
                        phase["kept"] += 1
                    else:
                        payload = conn.execute(
                            "SELECT payload FROM body_blobs WHERE rowid = ?", (cursor,)
                        ).fetchone()
                        old = bytes(payload[0]) if payload is not None else b""
                        new = self._proven_body_payload(
                            old, dict_id, target, str(row[1]), level
                        )
                        if new is None:
                            phase["failed"] += 1
                        elif new is old:
                            phase["kept"] += 1
                        else:
                            updates.append((target, new, cursor))
                            phase["converted"] += 1
                            phase["bytes_before"] += len(old)
                            phase["bytes_after"] += len(new)
                    if time.perf_counter() >= budget_end:
                        break
                if time.perf_counter() >= budget_end:
                    break
            if updates:
                conn.executemany(
                    "UPDATE body_blobs SET dict_id = ?, payload = ? WHERE rowid = ?",
                    updates,
                )
            phase["through"] = cursor
            if finished:
                phase["done_at"] = time.time()
            state["freed_pages"] = int(state["freed_pages"]) + max(
                0, self._freelist_count(conn) - freelist_before
            )
            self._save_history_state(conn, state)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if finished and phase["failed"]:
            logger.warning(
                "Request log history conversion left {} bodies as they were:"
                " they did not decode, or did not hash back to their address",
                phase["failed"],
            )
        return True

    def _proven_body_payload(
        self,
        old: bytes,
        dict_id: int | None,
        target: int,
        sha: str,
        level: int,
    ) -> bytes | None:
        """One body recompressed with dictionary ``target``, proven lossless.

        The stored payload is decoded with its own dictionary and must hash to
        its address; the new payload is decoded by the reader's own path and
        must give the same bytes and the same hash. ``old`` itself comes back
        when the new form is not smaller, and None when any of it fails.
        """
        try:
            if dict_id is not None and self._dictionary(dict_id) is None:
                return None
            raw = zstd.decompress(old, zstd_dict=self._dictionary(dict_id))
            if hashlib.sha256(raw).hexdigest() != sha:
                return None
            zstd_dict = self._dictionary(target)
            if zstd_dict is None:
                return None
            new = zstd.compress(raw, level=level, zstd_dict=zstd_dict)
            if len(new) >= len(old):
                return old
            check = self._raw_payload(new, target)
            if check is None or check != raw:
                return None
            if hashlib.sha256(check).hexdigest() != sha:
                return None
            return new
        except Exception:  # every failure means "leave the body alone"
            return None

    def _convert_metadata_step(self, conn: sqlite3.Connection) -> bool:
        """Store the next rows' inline metadata once, moving each row; one transaction.

        A row's ``headers`` / ``route_chain`` / ``params`` text is replaced by
        the id of its ``request_values`` row only once that id reads back,
        through the readers' own lookup, as exactly the bytes the row held. A
        row with any value that does not -- or that is not TEXT, or not UTF-8
        -- is left exactly as it is and counted. NULL stays NULL.

        A converted row also moves to a new rowid at the end of the table.
        Clearing the columns in place shrinks the row, but SQLite does not
        merge the half-empty pages that leaves: on 100,000 rows of the real
        log an in-place update freed no page at all, and moving the same rows
        freed 48 % of the table's pages. Nothing names a request by its rowid
        -- every reader, and the retention cap, goes by ``id`` and
        ``ts_epoch`` -- and rows move in rowid order, so they keep their order.

        The walk goes on past the rowid the table ended at when it began, over
        the rows it moved there and any written since, until no row is left;
        those already name their values and are passed over, uncounted.
        """
        budget_end = time.perf_counter() + _HISTORY_STEP_SECONDS
        conn.execute("BEGIN IMMEDIATE")
        try:
            state = self._load_history_state(conn)
            phase = state["metadata"]
            top = int(
                conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM requests").fetchone()[
                    0
                ]
            )
            if phase["end"] is None:
                phase["end"] = top
            end = int(phase["end"])
            freelist_before = self._freelist_count(conn)
            cursor = int(phase["through"])
            known: dict[bytes, int | None] = {}
            added: list[int] = []
            finished = False
            while True:
                rows = conn.execute(
                    _METADATA_FETCH_SQL, (cursor, _HISTORY_FETCH_ROWS)
                ).fetchall()
                if not rows:
                    finished = True
                    break
                for row in rows:
                    cursor = int(row[0])
                    refs = self._stored_once_refs(conn, row, known, added)
                    if refs is None:
                        phase["failed"] += 1
                    elif refs:
                        top += 1
                        assignments = ", ".join(
                            f"{column} = NULL, {column}_ref = ?"
                            for column, _, _ in refs
                        )
                        conn.execute(
                            f"UPDATE requests SET {assignments}, rowid = ?"
                            " WHERE rowid = ?",
                            [*(value_id for _, value_id, _ in refs), top, cursor],
                        )
                        phase["converted"] += 1
                        phase["bytes_before"] += sum(size for _, _, size in refs)
                    elif cursor <= end:
                        # Nothing inline: written by this release, or empty.
                        phase["kept"] += 1
                    if time.perf_counter() >= budget_end:
                        break
                # At least one row per step, however slow, so the walk always
                # moves; then the budget decides.
                if time.perf_counter() >= budget_end:
                    break
            phase["bytes_after"] += sum(added)
            phase["through"] = cursor
            if finished:
                phase["done_at"] = time.time()
            state["freed_pages"] = int(state["freed_pages"]) + max(
                0, self._freelist_count(conn) - freelist_before
            )
            self._save_history_state(conn, state)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if finished and phase["failed"]:
            logger.warning(
                "Request log history conversion left {} rows' metadata as it"
                " was: a value was not text, or did not read back as the same"
                " bytes",
                phase["failed"],
            )
        return True

    def _stored_once_refs(
        self,
        conn: sqlite3.Connection,
        row: Sequence[Any],
        known: dict[bytes, int | None],
        added: list[int],
    ) -> list[tuple[str, int, int]] | None:
        """One ``_METADATA_FETCH_SQL`` row's inline values as proven value ids.

        ``(column, value id, bytes the column held)`` for every column that
        holds a value inline; empty when none does; None when any of them
        cannot be stored once, and then the row must stay exactly as it is.
        """
        refs: list[tuple[str, int, int]] = []
        for index, column in enumerate(_STORED_ONCE_COLUMNS):
            kind, raw, ref = row[1 + 3 * index : 4 + 3 * index]
            if kind == "null":
                continue
            if kind != "text" or ref is not None:
                return None
            original = bytes(raw)
            if original not in known:
                known[original] = self._proven_metadata_id(conn, original, added)
            value_id = known[original]
            if value_id is None:
                return None
            refs.append((column, value_id, len(original)))
        return refs

    def _proven_metadata_id(
        self, conn: sqlite3.Connection, original: bytes, added: list[int]
    ) -> int | None:
        """The value id for one column's stored bytes, or None to leave the row.

        The bytes are the column exactly as stored (``CAST(... AS BLOB)``);
        they must be strict UTF-8, as every reader decodes them, and the id
        must read back as these same bytes (``_proven_value_id``).
        """
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if text.encode("utf-8") != original:
            return None
        return self._proven_value_id(conn, text, added)

    def _convert_skipped_step(self, conn: sqlite3.Connection) -> bool:
        """Store the next requests' skipped attempts compactly; one transaction.

        Walks ``request_attempts`` by rowid up to where the table ended when
        the part began; rows written since are compact already. At each
        request it meets, every skipped attempt of that request that fits the
        compact form is read as stored (storage class and bytes), its compact
        form written, read back through the readers' own path, and only if
        that gives exactly the same rows are the rows deleted. Anything else
        -- an attempt holding more, a value that is not what the form holds,
        a request whose skipped attempts differ in time or key -- stays a row
        and is counted.

        Deleting is enough to free the pages: on the full-size copy the
        2,496,100 rows and their index entries gave back 324,179 pages. No row
        moves, so the rowid order ``latency_by_model`` samples by is kept.
        """
        budget_end = time.perf_counter() + _HISTORY_STEP_SECONDS
        conn.execute("BEGIN IMMEDIATE")
        try:
            state = self._load_history_state(conn)
            phase = state["skipped"]
            if phase["end"] is None:
                phase["end"] = int(
                    conn.execute(
                        "SELECT COALESCE(MAX(rowid), 0) FROM request_attempts"
                    ).fetchone()[0]
                )
            end = int(phase["end"])
            freelist_before = self._freelist_count(conn)
            cursor = int(phase["through"])
            known: dict[str, int | None] = {}
            added: list[int] = []
            seen: set[str] = set()
            finished = False
            while True:
                rows = conn.execute(
                    "SELECT rowid, request_id FROM request_attempts"
                    " WHERE rowid > ? AND rowid <= ? ORDER BY rowid LIMIT ?",
                    (cursor, end, _HISTORY_FETCH_ROWS),
                ).fetchall()
                if not rows:
                    finished = True
                    break
                for row in rows:
                    cursor = int(row[0])
                    request_id = row[1]
                    if isinstance(request_id, str) and request_id not in seen:
                        seen.add(request_id)
                        self._compact_request_skips(
                            conn, request_id, cursor, phase, known, added
                        )
                    if time.perf_counter() >= budget_end:
                        break
                # At least one request per step, however slow, so the walk
                # always moves; then the budget decides.
                if time.perf_counter() >= budget_end:
                    break
            phase["bytes_after"] += sum(added)
            phase["through"] = cursor
            if finished:
                phase["done_at"] = time.time()
            state["freed_pages"] = int(state["freed_pages"]) + max(
                0, self._freelist_count(conn) - freelist_before
            )
            self._save_history_state(conn, state)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if finished and phase["failed"]:
            logger.warning(
                "Request log history conversion left {} skipped route attempts"
                " as they were: their compact form did not read back as the"
                " same rows",
                phase["failed"],
            )
        return True

    def _compact_request_skips(
        self,
        conn: sqlite3.Connection,
        request_id: str,
        rowid: int,
        phase: dict[str, Any],
        known: dict[str, int | None],
        added: list[int],
    ) -> None:
        """Store one request's compactable skipped attempts as one row, proven.

        Done once per request, where the walk (at ``rowid``) first meets it,
        so every count is the same however the walk was cut into steps.
        """
        if (
            conn.execute(
                "SELECT 1 FROM request_attempt_skips WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            is not None
        ):
            # Compact already: by this walk a step ago, or by the writer.
            return
        if (
            conn.execute(
                "SELECT 1 FROM request_attempts WHERE request_id = ? AND rowid < ?"
                " LIMIT 1",
                (request_id, rowid),
            ).fetchone()
            is not None
        ):
            # The walk met this request at an earlier row, in an earlier step,
            # and left what it left: done.
            return
        compact: list[tuple[int, dict[str, Any], int]] = []
        for row in conn.execute(_SKIP_FETCH_SQL, (request_id,)).fetchall():
            if row[1] != "text" or bytes(row[2]) != b"skipped":
                continue
            stored = _stored_skip_values(request_id, row)
            if stored is None:
                phase["kept"] += 1
                continue
            values, size = stored
            compact.append((int(row[0]), values, size))
        if not compact:
            return
        shared = {
            (
                struct.pack("<d", values["ts_epoch"]),
                values["key_index"],
                values["key_label"],
            )
            for _, values, _ in compact
        }
        parent = conn.execute(
            "SELECT 1 FROM requests WHERE id = ?", (request_id,)
        ).fetchone()
        if len(shared) != 1 or parent is None:
            phase["kept"] += len(compact)
            return
        members = [
            tuple(values[column] for column in _ATTEMPT_INSERT_COLUMNS)
            for _, values, _ in compact
        ]
        text = _skip_set_text(members)
        if text not in known:
            known[text] = self._proven_skip_set_id(conn, text, added)
        set_id = known[text]
        if set_id is None:
            phase["failed"] += len(compact)
            return
        first = compact[0][1]
        conn.execute("SAVEPOINT history_skip")
        # A plain INSERT: a request already compact was passed over above,
        # and a compact row is never overwritten from here.
        conn.execute(
            "INSERT INTO request_attempt_skips"
            " (request_id, set_id, ts_epoch, key_index, key_label)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                request_id,
                set_id,
                first["ts_epoch"],
                first["key_index"],
                first["key_label"],
            ),
        )
        restored = self._skip_rows(conn, [request_id], cache=False).get(request_id)
        if not _same_attempt_rows(restored, [values for _, values, _ in compact]):
            conn.execute("ROLLBACK TO history_skip")
            conn.execute("RELEASE history_skip")
            phase["failed"] += len(compact)
            return
        conn.executemany(
            "DELETE FROM request_attempts WHERE rowid = ?",
            [(rowid,) for rowid, _, _ in compact],
        )
        conn.execute("RELEASE history_skip")
        phase["converted"] += len(compact)
        phase["bytes_before"] += sum(size for _, _, size in compact)
        added.append(
            len(request_id.encode("utf-8"))
            + 8
            + 8
            + (len(first["key_label"].encode("utf-8")) if first["key_label"] else 0)
        )

    def _space_owed(self, conn: sqlite3.Connection, state: dict[str, Any]) -> int:
        """Pages the conversion freed that are not handed back yet.

        Never more than the freelist holds above what it held before the
        conversion freed anything: a page freed by somebody else may hold a
        deleted row, and handing it back would destroy it (IV.13). Zero when
        the database cannot hand pages back without a full rewrite.
        """
        if self._history_space_mode is None:
            self._history_space_mode = int(
                conn.execute("PRAGMA auto_vacuum").fetchone()[0]
            )
            if self._history_space_mode != 2:
                logger.info(
                    "Request log auto_vacuum is not incremental: the space the"
                    " conversion frees stays inside the file for new rows"
                )
        if self._history_space_mode != 2:
            return 0
        owed = int(state["freed_pages"]) - int(state["returned_pages"])
        above = self._freelist_count(conn) - int(state["freelist_at_start"])
        return max(0, min(owed, above))

    def _return_space_step(self, conn: sqlite3.Connection) -> bool:
        """Hand a few freed pages back to the filesystem; one transaction.

        Starts only once the conversion's own marker counts pages it freed. A
        step is sized to fit ``_HISTORY_STEP_SECONDS``, adapting to how long
        the last one took. In WAL mode the file itself shrinks at the next
        automatic checkpoint; nothing here forces one.
        """
        state = self._load_history_state(conn)
        converting = any(state[name]["done_at"] is None for name in _HISTORY_PHASES)
        owed = self._space_owed(conn, state)
        if owed <= 0 or (converting and owed < self._space_step_pages):
            return False
        conn.execute("BEGIN IMMEDIATE")
        try:
            # Again under the write lock: what was owed a moment ago may have
            # been handed back by a prune since.
            state = self._load_history_state(conn)
            owed = self._space_owed(conn, state)
            if owed <= 0:
                conn.commit()
                return False
            pages = min(self._space_step_pages, owed)
            before = self._freelist_count(conn)
            started = time.perf_counter()
            conn.execute(f"PRAGMA incremental_vacuum({int(pages)})").fetchall()
            elapsed = time.perf_counter() - started
            returned = max(0, before - self._freelist_count(conn))
            state["returned_pages"] = int(state["returned_pages"]) + returned
            self._save_history_state(conn, state)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if elapsed > 0:
            scaled = min(pages * 2, int(pages * _HISTORY_STEP_SECONDS / elapsed))
            self._space_step_pages = max(
                _SPACE_STEP_PAGES_MIN, min(_SPACE_STEP_PAGES_MAX, scaled)
            )
        return returned > 0

    def _finish_history(self, conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            state = self._load_history_state(conn)
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            state["bytes_at_end"] = page_count * int(state["page_size"])
            state["done_at"] = time.time()
            self._save_history_state(conn, state)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        self._history_done = True
        # A document an earlier release finished and this one reopened reports
        # only what ran since: the earlier release logged its own parts.
        ran = [
            name
            for name in state.get("reopened_for") or _HISTORY_PHASES
            if name in _HISTORY_PHASES
        ]
        work: list[str] = []
        if "wire" in ran or "bodies" in ran:
            snapshots = state["wire"]["converted"] if "wire" in ran else 0
            bodies = state["bodies"]["converted"] if "bodies" in ran else 0
            recompressed = [state[name] for name in ("wire", "bodies") if name in ran]
            before = sum(int(phase["bytes_before"]) for phase in recompressed) / 1e9
            after = sum(int(phase["bytes_after"]) for phase in recompressed) / 1e9
            work.append(
                f"{snapshots} wire snapshots and {bodies} bodies compressed"
                f" ({before:.2f} GB -> {after:.2f} GB)"
            )
        if "metadata" in ran:
            metadata = state["metadata"]
            work.append(
                f"{metadata['converted']} rows' repeated metadata stored once"
                f" ({int(metadata['bytes_before']) / 1e9:.2f} GB ->"
                f" {int(metadata['bytes_after']) / 1e6:.2f} MB)"
            )
        if "skipped" in ran:
            skipped = state["skipped"]
            work.append(
                f"{skipped['converted']} skipped route attempts stored compactly"
                f" ({int(skipped['bytes_before']) / 1e9:.2f} GB ->"
                f" {int(skipped['bytes_after']) / 1e6:.2f} MB)"
            )
        returned = int(state["returned_pages"]) - int(
            state.get("returned_at_reopen") or 0
        )
        logger.info(
            "Request log history conversion done in {:.0f} min: {}, {} left as they"
            " were, {} failed the check; database {:.2f} GB -> {:.2f} GB,"
            " {:.2f} GB handed back",
            (state["done_at"] - float(state.get("reopened_at") or state["started_at"]))
            / 60,
            ", ".join(work),
            sum(int(state[name]["kept"]) for name in ran),
            sum(int(state[name]["failed"]) for name in ran),
            int(state.get("bytes_at_reopen") or state["bytes_at_start"]) / 1e9,
            int(state["bytes_at_end"]) / 1e9,
            max(0, returned) * int(state["page_size"]) / 1e9,
        )

    def _log_history_progress(self, state: dict[str, Any]) -> None:
        now = time.monotonic()
        if now - self._history_logged_at < _HISTORY_PROGRESS_LOG_SECONDS:
            return
        self._history_logged_at = now
        status = _history_status(state)
        logger.info(
            "Request log history conversion: {} {}%, {:.2f} GB handed back so far",
            status["phase"],
            status["percent"],
            status["returned_bytes"] / 1e9,
        )

    def history_conversion_status(self) -> dict[str, Any]:
        """Read-only progress of the history conversion, for the dashboard."""
        idle: dict[str, Any] = {
            "state": "off",
            "phase": None,
            "percent": None,
            "returned_bytes": 0,
        }
        if not self._compress_bodies:
            return idle
        try:
            with self._connection() as conn:
                raw = self._meta_get(conn, _HISTORY_CONVERSION_KEY)
            state = json.loads(raw) if raw else None
        except sqlite3.Error, ValueError:
            return {**idle, "state": "unknown"}
        if not isinstance(state, dict):
            return {**idle, "state": "pending"}
        return _history_status(state)

    # ------------------------------------------------ origin folder backfill

    def request_origin_backfill(self) -> dict[str, Any]:
        """Ask the writer thread to fill ``project_dir`` for older rows.

        Only rows of a harness with a declared prompt extractor, only rows whose
        folder is still NULL, only from the stored prompt, and only on the
        writer thread's idle branch -- one committed chunk at a time, yielding
        to any request queued behind it. The session id cannot be backfilled:
        its value was never stored.
        """
        with self._origin_backfill_lock:
            progress = self._origin_backfill_progress
            if not progress["running"]:
                progress.update(
                    running=True,
                    scanned=0,
                    filled=0,
                    started_at=time.time(),
                    finished_at=None,
                    error=None,
                )
        self._origin_backfill_requested.set()
        return self.origin_backfill_status()

    def origin_backfill_status(self) -> dict[str, Any]:
        """What the folder backfill has done, for the Request log card."""
        with self._origin_backfill_lock:
            status = dict(self._origin_backfill_progress)
        try:
            with self._connection() as conn:
                finished = self._meta_get(conn, _ORIGIN_BACKFILL_KEY)
                through = self._meta_get(conn, _ORIGIN_BACKFILL_THROUGH_KEY)
        except sqlite3.Error:
            finished = through = None
        status["completed_at"] = float(finished) if finished else None
        status["through_rowid"] = (
            int(through) if through and through.isdigit() else None
        )
        status["harnesses"] = sorted(PROMPT_HARNESSES)
        return status

    def _run_origin_backfill(self, conn: sqlite3.Connection) -> None:
        """Walk older rows by rowid, filling ``project_dir`` from the stored prompt.

        Resumable through ``origin_backfilled_through_v1``; a finished walk
        stamps ``origin_backfilled_at_v1``. A second request after that re-walks
        only rows newer than the cursor, so pressing the button again is cheap.
        """
        harnesses = sorted(PROMPT_HARNESSES)
        if not harnesses:
            self._finish_origin_backfill(error=None)
            return
        marks = ", ".join("?" * len(harnesses))
        try:
            resumed = self._meta_get(conn, _ORIGIN_BACKFILL_THROUGH_KEY)
            cursor = int(resumed) if resumed and resumed.isdigit() else 0
            while True:
                rows = conn.execute(
                    "SELECT rowid, id, substr(input_text, 1, ?) AS head,"
                    " origin_source FROM requests"
                    f" WHERE rowid > ? AND harness IN ({marks})"
                    " AND project_dir IS NULL ORDER BY rowid LIMIT ?",
                    (
                        _PROMPT_HEAD_CHARS,
                        cursor,
                        *harnesses,
                        _ORIGIN_BACKFILL_CHUNK_ROWS,
                    ),
                ).fetchall()
                if not rows:
                    break
                heads = self._stored_prompt_heads(
                    conn, [str(row["id"]) for row in rows if not row["head"]]
                )
                updates: list[tuple[str, str | None, int]] = []
                for row in rows:
                    head = row["head"] or heads.get(str(row["id"]))
                    folder = project_dir_from_prompt(head)
                    if folder is None:
                        continue
                    updates.append(
                        (
                            folder,
                            merge_origin_source(
                                row["origin_source"],
                                "project_dir",
                                "prompt",
                                BACKFILL_SIGNAL,
                            ),
                            int(row["rowid"]),
                        )
                    )
                with conn:
                    conn.executemany(
                        "UPDATE requests SET project_dir = ?, origin_source = ?"
                        " WHERE rowid = ? AND project_dir IS NULL",
                        updates,
                    )
                    cursor = int(rows[-1]["rowid"])
                    self._meta_set(conn, _ORIGIN_BACKFILL_THROUGH_KEY, str(cursor))
                with self._origin_backfill_lock:
                    self._origin_backfill_progress["scanned"] += len(rows)
                    self._origin_backfill_progress["filled"] += len(updates)
                if not self._queue.empty():
                    # A request is waiting. Hand the thread back; the flag is
                    # still set, so the next idle tick continues from here.
                    return
            with conn:
                self._meta_set(conn, _ORIGIN_BACKFILL_KEY, str(time.time()))
        except sqlite3.Error as exc:
            logger.warning("Request log folder backfill stopped: {}", exc)
            self._finish_origin_backfill(error=str(exc))
            return
        self._finish_origin_backfill(error=None)

    def _finish_origin_backfill(self, *, error: str | None) -> None:
        self._origin_backfill_requested.clear()
        with self._origin_backfill_lock:
            progress = self._origin_backfill_progress
            progress.update(running=False, finished_at=time.time(), error=error)
            scanned, filled = progress["scanned"], progress["filled"]
        logger.info(
            "Request log folder backfill: {} rows examined, {} folders filled",
            scanned,
            filled,
        )

    def _stored_prompt_heads(
        self, conn: sqlite3.Connection, ids: list[str]
    ) -> dict[str, str]:
        """The head of each row's stored prompt, decompressing only the prompt blob.

        A row written before the prompt/rest split keeps its prompt inside the
        one blob it has, so that blob is decoded instead.
        """
        if not ids:
            return {}
        placeholders = ", ".join("?" * len(ids))
        found = conn.execute(
            "SELECT r.request_id,"
            " COALESCE(bi.payload, br.payload) AS payload,"
            " CASE WHEN bi.payload IS NOT NULL THEN bi.dict_id ELSE br.dict_id END"
            " AS dict_id"
            " FROM request_bodies r"
            " LEFT JOIN body_blobs bi ON bi.sha = r.input_sha"
            " LEFT JOIN body_blobs br ON br.sha = r.sha"
            f" WHERE r.request_id IN ({placeholders})",
            ids,
        ).fetchall()
        heads: dict[str, str] = {}
        for row in found:
            text = self._decode_bodies(row["payload"], row["dict_id"]).get("input_text")
            if isinstance(text, str) and text:
                heads[str(row["request_id"])] = text[:_PROMPT_HEAD_CHARS]
        return heads

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
        # The same window for the one pass that joins two tables. Both halves
        # qualified, not just the first: ``request_attempts`` has a
        # ``ts_epoch`` of its own now, and a bare second half is "ambiguous
        # column name" to SQLite -- which the rollup backfill swallows as
        # "skipped", leaving ``stats()`` serving from rows forever.
        joined_window = "r.ts_epoch >= ? AND r.ts_epoch < ?"
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
            f" WHERE r.id = a.request_id AND {joined_window}"
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
            f" WHERE r.id = a.request_id AND {joined_window}"
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
        ``mcc-compact-log`` splits them when it runs.
        """
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(request_bodies)")
        }
        if "sha" in columns and "input_sha" not in columns:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ALTER TABLE request_bodies ADD COLUMN input_sha TEXT")

    @staticmethod
    def _ensure_dictionary_kind_column(conn: sqlite3.Connection) -> None:
        """Add ``body_dictionaries.kind`` to a table created before 7.73.0.

        ``prompt`` or ``rest``: which blobs a dictionary was trained on and is
        written with. NULL is every dictionary trained before kinds existed --
        one dictionary for all blobs -- and stays the fallback of a kind that
        has none of its own yet. An older version ignores the column and keeps
        taking the highest id, which decodes everything and is only weaker.
        """
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(body_dictionaries)")
        }
        if "kind" in columns:
            return
        try:
            conn.execute("ALTER TABLE body_dictionaries ADD COLUMN kind TEXT")
        except sqlite3.OperationalError:
            # Another process may have won the migration race.
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(body_dictionaries)")
            }
            if "kind" not in columns:
                raise

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
        """Read which dictionary each kind of content is written with now.

        ``_active_dict_id`` keeps its meaning -- the highest body dictionary id,
        which is what every version before 7.73.0 wrote with. A kind uses its
        own newest dictionary, else the newest one trained before kinds
        existed, else none. A process restart therefore picks up the newest.
        """
        row = conn.execute(
            "SELECT id FROM body_dictionaries ORDER BY id DESC LIMIT 1"
        ).fetchone()
        self._active_dict_id = int(row[0]) if row is not None else None
        kinds: dict[str, tuple[int, float]] = {}
        legacy = conn.execute(
            "SELECT id, created_at FROM body_dictionaries WHERE kind IS NULL"
            " ORDER BY id DESC LIMIT 1"
        ).fetchone()
        for kind in (_DICT_KIND_PROMPT, _DICT_KIND_REST):
            own = conn.execute(
                "SELECT id, created_at FROM body_dictionaries WHERE kind = ?"
                " ORDER BY id DESC LIMIT 1",
                (kind,),
            ).fetchone()
            chosen = own if own is not None else legacy
            if chosen is not None:
                kinds[kind] = (int(chosen[0]), float(chosen[1]))
        wire = conn.execute(
            "SELECT id, created_at FROM wire_dictionaries ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if wire is not None:
            kinds[_DICT_KIND_WIRE] = (int(wire[0]), float(wire[1]))
        self._kind_dicts = kinds
        # Loaded now, on the writer thread at start, so the first batch that
        # compresses with them does not open a connection to fetch each one.
        for kind, (dict_id, _created) in kinds.items():
            if kind == _DICT_KIND_WIRE:
                self._wire_dictionary(dict_id)
            else:
                self._dictionary(dict_id)

    def _dictionaries_due(self, now: float) -> list[tuple[str, int]]:
        """The kinds to relearn now, each with the samples it needs at least.

        A kind is due when its dictionary is older than the last 14 days of
        traffic, or when it has none. Nothing is due while bodies are stored
        uncompressed, and a kind whose training failed waits an hour.
        """
        if not self._compress_bodies:
            return []
        due: list[tuple[str, int]] = []
        for kind in _DICT_KINDS:
            failed_at = self._dict_train_failed_at.get(kind)
            if failed_at is not None and now - failed_at < _DICT_TRAIN_RETRY_SECONDS:
                continue
            active = self._kind_dicts.get(kind)
            if active is None:
                due.append((kind, _BODY_DICT_MIN_SAMPLES))
            elif active[1] < now - _DICT_REFRESH_AGE_SECONDS:
                due.append((kind, _DICT_REFRESH_MIN_SAMPLES))
        return due

    def _maybe_refresh_dictionaries(self) -> None:
        """Writer thread: start the background trainer if a kind is due.

        Called at start and every ``_PRUNE_EVERY_INSERTS`` inserts. Deciding is
        arithmetic on what is already in memory; counting and reading samples,
        and training itself, happen on the trainer's own thread -- never here
        and never on the event loop, because a prompt dictionary is tens of
        seconds of CPU. ``zstd.train_dict`` releases the GIL while it trains.
        """
        due = self._dictionaries_due(time.time())
        if not due or self._closed.is_set():
            return
        with self._trainer_lock:
            # A trainer still running, or results it left that the writer has
            # not stored yet: deciding now would train the same kind twice.
            if self._trained or (
                self._trainer is not None and self._trainer.is_alive()
            ):
                return
            self._trainer = threading.Thread(
                target=self._train_dictionaries,
                args=(due,),
                name="mcc-request-log-dictionary-trainer",
                daemon=True,
            )
            self._trainer.start()

    def _train_dictionaries(self, due: list[tuple[str, int]]) -> None:
        """Trainer thread: learn one dictionary per due kind, and hand it over.

        Reads the samples on a connection of its own and closes it before it
        trains, so nothing is held open for the length of a training. Writes
        nothing: the writer thread inserts each result between two batches. A
        failure, or a store closed meanwhile, leaves everything as it was.
        """
        for kind, min_samples in due:
            with self._trainer_conn_lock:
                if self._closed.is_set():
                    return
                try:
                    samples = self._dictionary_samples(kind, time.time())
                except sqlite3.Error as exc:
                    logger.warning(
                        "Request log {} dictionary samples unreadable: {}", kind, exc
                    )
                    self._dict_train_failed_at[kind] = time.time()
                    continue
            if len(samples) < min_samples:
                self._dict_too_few_samples.add(kind)
                continue
            self._dict_too_few_samples.discard(kind)
            size = _WIRE_DICT_SIZE if kind == _DICT_KIND_WIRE else _BODY_DICT_SIZE
            started = time.perf_counter()
            try:
                trained = zstd.train_dict(samples, size)
            except (zstd.ZstdError, ValueError, MemoryError) as exc:
                logger.warning(
                    "Request log {} dictionary training failed: {}", kind, exc
                )
                self._dict_train_failed_at[kind] = time.time()
                continue
            logger.info(
                "Request log {} dictionary trained from {} samples ({} bytes) in {:.1f}s",
                kind,
                len(samples),
                sum(len(sample) for sample in samples),
                time.perf_counter() - started,
            )
            with self._trainer_lock:
                self._trained.append((kind, trained.dict_content, time.time()))
            del samples

    def _dictionary_samples(self, kind: str, now: float) -> list[bytes]:
        """Up to ``_BODY_DICT_TRAINING_SAMPLES`` of one kind from the last 14 days.

        Spread evenly over the window rather than the newest ones: the newest
        thousand prompts are often a handful of sessions, each turn repeating
        the last, and a dictionary learned from them knows only those.
        """
        since = now - _DICT_REFRESH_AGE_SECONDS
        wanted = _BODY_DICT_TRAINING_SAMPLES
        with self._connection() as conn:
            if kind == _DICT_KIND_WIRE:
                # ``idx_request_attempts_ts_v1`` leads with ``outcome``; skipped
                # attempts are nine rows in ten and carry no snapshot.
                outcomes = [
                    outcome.value
                    for outcome in RouteAttemptOutcome
                    if outcome is not RouteAttemptOutcome.SKIPPED
                ]
                rowids = sorted(
                    int(row[0])
                    for row in conn.execute(
                        "SELECT rowid FROM request_attempts"
                        f" WHERE outcome IN ({', '.join('?' * len(outcomes))})"
                        " AND ts_epoch >= ?",
                        (*outcomes, since),
                    )
                )
                picked = _spread(rowids, wanted * _WIRE_SAMPLE_CANDIDATES_PER_SAMPLE)
                texts: list[bytes] = []
                for start in range(0, len(picked), _SWEEP_CHUNK):
                    if self._closed.is_set():
                        return []
                    chunk = picked[start : start + _SWEEP_CHUNK]
                    for row in conn.execute(
                        "SELECT wire_body FROM request_attempts WHERE rowid IN"
                        f" ({', '.join('?' * len(chunk))}) AND wire_body IS NOT NULL",
                        chunk,
                    ):
                        text = self._decode_wire_body(row[0])
                        if text:
                            texts.append(text.encode("utf-8", "surrogatepass"))
                return _spread(texts, wanted)
            column = "rb.input_sha" if kind == _DICT_KIND_PROMPT else "rb.sha"
            shas = list(
                dict.fromkeys(
                    str(row[0])
                    for row in conn.execute(
                        f"SELECT {column} FROM requests r"
                        " JOIN request_bodies rb ON rb.request_id = r.id"
                        f" WHERE r.ts_epoch >= ? AND {column} IS NOT NULL"
                        " ORDER BY r.ts_epoch",
                        (since,),
                    )
                )
            )
            picked_shas = _spread(shas, wanted)
            samples: list[bytes] = []
            for start in range(0, len(picked_shas), _SWEEP_CHUNK):
                if self._closed.is_set():
                    return []
                chunk_shas = picked_shas[start : start + _SWEEP_CHUNK]
                for row in conn.execute(
                    "SELECT dict_id, payload FROM body_blobs WHERE sha IN"
                    f" ({', '.join('?' * len(chunk_shas))})",
                    chunk_shas,
                ):
                    raw = self._raw_payload(row["payload"], row["dict_id"])
                    if raw:
                        samples.append(raw)
            return samples

    def _install_trained_dictionaries(self, conn: sqlite3.Connection) -> None:
        """Writer thread, between batches: store what the trainer learned.

        Each dictionary is its own small transaction, and the kind switches to
        it only once that committed, so a failed insert leaves the kind on the
        dictionary it had. Older dictionaries are never deleted: every blob
        and every compressed snapshot names the one it needs.
        """
        with self._trainer_lock:
            if not self._trained:
                return
            results, self._trained = self._trained, []
        for kind, content, created_at in results:
            try:
                with conn:
                    if kind == _DICT_KIND_WIRE:
                        cursor = conn.execute(
                            "INSERT INTO wire_dictionaries (created_at, content)"
                            " VALUES (?, ?)",
                            (created_at, content),
                        )
                    else:
                        cursor = conn.execute(
                            "INSERT INTO body_dictionaries (created_at, content, kind)"
                            " VALUES (?, ?, ?)",
                            (created_at, content, kind),
                        )
            except sqlite3.Error as exc:
                logger.warning("Request log {} dictionary not stored: {}", kind, exc)
                self._dict_train_failed_at[kind] = time.time()
                continue
            dict_id = int(cursor.lastrowid or 0)
            if not dict_id:
                continue
            loaded = zstd.ZstdDict(content)
            if kind == _DICT_KIND_WIRE:
                self._wire_dict_cache[dict_id] = loaded
            else:
                self._dict_cache[dict_id] = loaded
                self._active_dict_id = dict_id
            self._kind_dicts[kind] = (dict_id, created_at)
            logger.info("Request log {} dictionary {} in use", kind, dict_id)

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
                    " (started_at, last_seen_at, pid, host, port, listening, version)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        now,
                        now,
                        os.getpid(),
                        address[0] if address else None,
                        address[1] if address else None,
                        _listening_column(),
                        package_version(),
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
                " SET last_seen_at = ?, host = ?, port = ?, listening = ?"
                " WHERE id = ?",
                (
                    now,
                    address[0] if address else None,
                    address[1] if address else None,
                    _listening_column(),
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

    def touch_session_soon(self) -> None:
        """Ask the writer to rewrite this session's row now, not at the next beat.

        Never blocks. A full queue skips it: the next heartbeat, and the final
        one at shutdown, write the same facts.
        """
        if self._closed.is_set():
            return
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(_TOUCH)

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
            self._maybe_refresh_dictionaries()
            self._dictionaries_checked.set()
            session_id = self._open_session(conn)
            last_heartbeat = time.monotonic()
            while not stopping:
                # Between batches, never inside one: see ``retune``. A
                # dictionary the trainer finished is stored here for the same
                # reason, so one batch is written with one dictionary per kind.
                if not pending:
                    self._adopt_pending_tuning()
                    self._install_trained_dictionaries(conn)
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
                    self._ensure_attempts_ts_backfill(conn)
                    # Only after the operator asked; see
                    # ``request_origin_backfill``.
                    if self._origin_backfill_requested.is_set():
                        self._run_origin_backfill(conn)
                    # Last, and with the same yield: one bounded step at a
                    # time, back to the queue the moment a request arrives.
                    self._run_history_conversion(conn)
                    continue
                if item is _STOP:
                    stopping = True
                elif item is _TOUCH:
                    self._touch_session(conn, session_id, time.time())
                    continue
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
                if item is not None and item is not _STOP and item is not _TOUCH:
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
        ttft_winner = record.ttft_winner_ms
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
            "ttft_winner_sum": ttft_winner if ttft_winner is not None else 0.0,
            "ttft_winner_count": int(ttft_winner is not None),
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
        self, packed: bytes, *, level: int | None = None, kind: str | None = None
    ) -> tuple[int | None, bytes]:
        """Compress one packed body with the dictionary of its ``kind``.

        No kind, or a kind with no dictionary of its own and none from before
        kinds existed, writes with ``_active_dict_id`` exactly as before 7.73.0.
        """
        level = self._compression_level if level is None else level
        active = self._kind_dicts.get(kind) if kind is not None else None
        dict_id = active[0] if active is not None else self._active_dict_id
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
    def _store_media(conn: sqlite3.Connection, batch: list[RequestRecord]) -> None:
        """Link each record's generated media to its content address.

        ``stored`` only ever rises: a later request that kept the file makes
        the address stored even if an earlier one recorded metadata only.
        """
        blobs: list[tuple[str, str | None, int, float, int]] = []
        links: list[tuple[str, str, int, str]] = []
        for record in batch:
            for output in record.media_outputs:
                blobs.append(
                    (
                        output.sha256,
                        output.mime,
                        output.bytes,
                        record.ts_epoch,
                        1 if output.stored else 0,
                    )
                )
                links.append((record.id, output.direction, output.idx, output.sha256))
        if not links:
            return
        conn.executemany(_MEDIA_BLOB_UPSERT_SQL, blobs)
        conn.executemany(
            "INSERT OR REPLACE INTO request_media (request_id, direction, idx, sha256)"
            " VALUES (?, ?, ?, ?)",
            links,
        )

    def _store_attempts(
        self,
        conn: sqlite3.Connection,
        batch: list[RequestRecord],
        rewritten: Collection[str] = (),
    ) -> None:
        """Persist each record's route attempts.

        ``INSERT OR REPLACE``: an attempt is identified by (request, index), so
        replacing is the correct merge if a record is ever written twice.

        ``wire_body`` is stored compressed when that is smaller (7.73.0; see
        ``_encode_wire_body``), at the level and with the switch the store
        holds now, each read once so one batch is written one way.

        Skipped attempts that hold nothing but the compact form's values are
        stored as one ``request_attempt_skips`` row (7.76.0; see
        ``_store_skip_packs``), with the same switch. ``rewritten`` names the
        requests already stored or written twice in this batch: for those the
        merge above has to see every attempt as a row, so their compact
        attempts are first turned back into rows and nothing new is packed.
        """
        level = self._compression_level
        compress = self._compress_bodies
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
                self._encode_wire_body(
                    attempt.wire_body, level=level, compress=compress
                ),
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
                # The parent's instant, not a fresh clock: an attempt happened
                # when its request did, and a second reading here would make
                # the stored copy disagree with the row it was copied from.
                record.ts_epoch,
                # This attempt's own clocks. Written on every verdict, not just
                # on the one that worked: an attempt that streamed a first
                # token and then died is precisely the row that explains where
                # a slow request's time went.
                attempt.ttft_ms,
                attempt.first_reasoning_ms,
                attempt.proxy_label,
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
        if rewritten:
            # Before the rows: a compact attempt the new write also names must
            # be a row for ``INSERT OR REPLACE`` to replace it, exactly as it
            # would have been before 7.76.0.
            self._expand_skip_packs(
                conn, sorted({str(row[0]) for row in rows} & set(rewritten))
            )
        if compress:
            rows = self._store_skip_packs(conn, rows, rewritten)
        if rows:
            conn.executemany(_ATTEMPT_INSERT_SQL, rows)

    def _store_skip_packs(
        self,
        conn: sqlite3.Connection,
        rows: list[tuple[Any, ...]],
        rewritten: Collection[str],
    ) -> list[tuple[Any, ...]]:
        """Store each request's compactable skipped attempts as one row.

        Called inside the write transaction. Returns the rows still to be
        inserted into ``request_attempts``, in their original order. A request
        is packed only when its set reads back through ``_load_skip_sets`` as
        the same text and its whole compact form reads back through
        ``_skip_rows`` -- the readers' own path -- as exactly the rows it
        replaces; otherwise every one of its rows is inserted as before.
        """
        by_request: dict[str, list[int]] = {}
        for index, row in enumerate(rows):
            request_id = row[0]
            if isinstance(request_id, str) and request_id not in rewritten:
                by_request.setdefault(request_id, []).append(index)
        known: dict[str, int | None] = {}
        packs: dict[str, tuple[list[int], tuple[Any, ...]]] = {}
        for request_id, all_indexes in by_request.items():
            numbers = [rows[index][1] for index in all_indexes]
            if len(set(numbers)) != len(numbers):
                # Two attempts with one number: which survives is the order of
                # the ``INSERT OR REPLACE``, which only rows keep.
                continue
            indexes = [
                index for index in all_indexes if _compactable_attempt_row(rows[index])
            ]
            if not indexes:
                continue
            members = [rows[index] for index in indexes]
            shared = {_skip_shared_values(row) for row in members}
            if len(shared) != 1:
                continue
            text = _skip_set_text(members)
            if text not in known:
                known[text] = self._proven_skip_set_id(conn, text)
            set_id = known[text]
            if set_id is None:
                continue
            ts_epoch, key_index, key_label = next(iter(shared))
            packs[request_id] = (
                indexes,
                (request_id, set_id, ts_epoch, key_index, key_label),
            )
        if not packs:
            return rows
        conn.executemany(_SKIP_PACK_INSERT_SQL, [pack for _, pack in packs.values()])
        restored = self._skip_rows(conn, list(packs), cache=False)
        packed: set[int] = set()
        unproven: list[tuple[str]] = []
        for request_id, (indexes, _) in packs.items():
            expected = [
                dict(zip(_ATTEMPT_INSERT_COLUMNS, rows[index], strict=True))
                for index in indexes
            ]
            if _same_attempt_rows(restored.get(request_id), expected):
                packed.update(indexes)
            else:
                unproven.append((request_id,))
        if unproven:
            conn.executemany(
                "DELETE FROM request_attempt_skips WHERE request_id = ?", unproven
            )
        return [row for index, row in enumerate(rows) if index not in packed]

    def _expand_skip_packs(
        self, conn: sqlite3.Connection, request_ids: list[str]
    ) -> None:
        """Turn these requests' compact skipped attempts back into rows.

        For a request written again: its attempts are then rows only, and the
        ``INSERT OR REPLACE`` that follows merges exactly as it always has.
        ``OR IGNORE``: a row already there for an attempt is what every reader
        shows for it, and stays.
        """
        if not request_ids:
            return
        restored = self._skip_rows(conn, request_ids, cache=False)
        conn.executemany(
            "INSERT OR IGNORE INTO request_attempts"
            f" ({', '.join(_ATTEMPT_INSERT_COLUMNS)}) VALUES"
            f" ({', '.join('?' * len(_ATTEMPT_INSERT_COLUMNS))})",
            [
                tuple(row[column] for column in _ATTEMPT_INSERT_COLUMNS)
                for members in restored.values()
                for row in members
            ],
        )
        # Every one of these requests: a compact row whose set is gone reads
        # as nothing, and nothing is lost by dropping it.
        conn.executemany(
            "DELETE FROM request_attempt_skips WHERE request_id = ?",
            [(request_id,) for request_id in request_ids],
        )

    def _proven_skip_set_id(
        self, conn: sqlite3.Connection, text: str, added: list[int] | None = None
    ) -> int | None:
        """The ``attempt_skip_sets`` id holding ``text``, adding it if needed.

        None -- keep the attempts as rows -- unless the id reads back as
        exactly ``text`` through ``_load_skip_sets``, the readers' own lookup.
        Must run inside a write transaction. ``added`` collects the size of
        each set this call had to add.
        """
        try:
            encoded = text.encode("utf-8")
            digest = hashlib.sha256(encoded).digest()[:_VALUE_DIGEST_BYTES]
            set_id: int | None = None
            for found_id, found in conn.execute(
                "SELECT id, attempts FROM attempt_skip_sets WHERE digest = ?"
                " ORDER BY id",
                (digest,),
            ):
                if found == text:
                    set_id = int(found_id)
                    break
            if set_id is None:
                cursor = conn.execute(
                    "INSERT INTO attempt_skip_sets (digest, attempts) VALUES (?, ?)",
                    (digest, text),
                )
                set_id = int(cursor.lastrowid or 0)
                if added is not None:
                    added.append(len(encoded))
            if not set_id:
                return None
            check = _load_skip_sets(conn, {set_id}).get(set_id)
            if check is None or check.encode("utf-8") != encoded:
                return None
            return set_id
        except sqlite3.Error:
            raise
        except Exception:  # every other failure means "keep the rows"
            return None

    def _skip_sets(
        self, conn: sqlite3.Connection, set_ids: set[int], *, cache: bool = True
    ) -> dict[int, tuple[tuple[Any, ...], ...]]:
        """Parsed ``attempt_skip_sets`` by id; a bad or missing one is absent.

        ``cache`` is for readers only. Inside a write transaction a set may be
        one this transaction added, and a rollback would hand its id out again
        to a different set; so a writer neither reads nor fills the cache.
        """
        held = self._skip_set_cache
        found: dict[int, tuple[tuple[Any, ...], ...]] = {}
        missing: set[int] = set()
        for set_id in set_ids:
            # One ``get``, never ``in`` then ``[]``: another reader may clear
            # the cache between the two.
            cached = held.get(set_id) if cache else None
            if cached is None:
                missing.add(set_id)
            else:
                found[set_id] = cached
        if missing:
            for set_id, text in _load_skip_sets(conn, missing).items():
                members = _parse_skip_set(text)
                if members is None:
                    continue
                found[set_id] = members
                if cache:
                    if len(held) >= _SKIP_SET_CACHE_MAX:
                        held.clear()
                    held[set_id] = members
        return found

    def _skip_rows(
        self,
        conn: sqlite3.Connection,
        request_ids: Sequence[str],
        *,
        cache: bool = True,
    ) -> dict[str, list[dict[str, Any]]]:
        """These requests' compact skipped attempts, as whole attempt rows.

        Each comes back as a mapping of every ``_ATTEMPT_INSERT_COLUMNS``
        column to exactly the value a ``request_attempts`` row holding it
        returns, in attempt order. A request with no compact attempts is
        absent; so is one whose set is gone, which reads as no rows rather
        than raising. Pass ``cache=False`` inside a write transaction.
        """
        sides: list[sqlite3.Row | tuple[Any, ...]] = []
        for start in range(0, len(request_ids), _SHA_LOOKUP_CHUNK):
            chunk = list(request_ids[start : start + _SHA_LOOKUP_CHUNK])
            sides.extend(
                conn.execute(
                    "SELECT request_id, set_id, ts_epoch, key_index, key_label"
                    " FROM request_attempt_skips"
                    f" WHERE request_id IN ({', '.join('?' * len(chunk))})",
                    chunk,
                ).fetchall()
            )
        if not sides:
            return {}
        sets = self._skip_sets(conn, {int(side[1]) for side in sides}, cache=cache)
        out: dict[str, list[dict[str, Any]]] = {}
        for request_id, set_id, ts_epoch, key_index, key_label in sides:
            members = sets.get(int(set_id))
            if members is None:
                continue
            shared = {
                "ts_epoch": ts_epoch,
                "key_index": key_index,
                "key_label": key_label,
            }
            out[str(request_id)] = [
                _skip_row(str(request_id), member, shared) for member in members
            ]
        return out

    def _fetch_attempts(
        self, conn: sqlite3.Connection, request_id: str
    ) -> list[dict[str, Any]]:
        """Return one request's attempts in the order the chain tried them.

        ``wire_body`` comes back as the same parsed JSON whichever encoding the
        row holds: TEXT as written before 7.73.0, or the compressed envelope.
        Skipped attempts stored compactly (7.76.0) come back exactly as the
        rows they replaced, in their place in the chain.
        """
        with _one_snapshot(conn):
            rows = conn.execute(
                "SELECT attempt, provider, model_ref, outcome, error_kind,"
                " error_message, duration_ms, params, wire_body, reasoning_emitted,"
                " key_index, key_label, ladder_tries, tokens_in, tokens_out,"
                " cost_usd, cost_source, ts_epoch, ttft_ms, first_reasoning_ms,"
                " proxy_label"
                " FROM request_attempts"
                " WHERE request_id = ? ORDER BY attempt",
                (request_id,),
            ).fetchall()
            packed = self._skip_rows(conn, [request_id]).get(request_id)
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
                "wire_body": _loads_or_none(self._decode_wire_body(row["wire_body"])),
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
                # The egress address this attempt last used. NULL is "not
                # measured" -- no chain, or a row written before the column --
                # and the literal "direct" is a rung the operator chose.
                "proxy_label": row["proxy_label"],
                # A copy of the parent request's instant, so the attempt can be
                # found by time without asking the parent. NULL on every
                # attempt written before the column existed, which is "not
                # recorded", never "at the epoch".
                "ts_epoch": row["ts_epoch"],
                # What this hop cost on its own. A describe attempt is a real
                # call to a real model on a real key; NULL everywhere else,
                # because an ordinary attempt's cost is the request row's and
                # repeating it here would double every joined total.
                "cost_usd": row["cost_usd"],
                "cost_source": row["cost_source"],
                # This attempt's own first-token clocks, in milliseconds from
                # the moment this model was asked -- not from when the client
                # asked, which is what the request row's ``ttft_ms`` measures.
                # NULL is "not measured": no answer content, no reasoning
                # signal, or a row written before 7.4.0. Drawn as a dash.
                "ttft_ms": row["ttft_ms"],
                "first_reasoning_ms": row["first_reasoning_ms"],
            }
            for row in _merge_skip_rows(rows, packed)
        ]

    @staticmethod
    def _fetch_exits(
        conn: sqlite3.Connection, answering: Mapping[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """The exit each request went out through, for the Exit column (7.88.0).

        ``answering`` maps each request id to its ``route_attempt``: the
        attempt the row names, which is the one that answered on a success and
        the last one tried otherwise. Per request:

        * ``label`` -- that attempt's stored exit (``request_attempts
          .proxy_label``: a chain entry's name or ``host:port``, a one-entry
          chain's or static proxy's, ``direct``, ``direct via system proxy
          host:port``), or ``None`` when it recorded none. ``None`` is what an
          attempt on a provider with no chain and no proxy has always stored.
        * ``tried`` -- every distinct exit the request went out through, in
          the order it was dialled: each attempt's dials (7.81.0 rotation keeps
          them on the ladder, the stored label is only the last), then its own
          label. The answering exit is in it.

        Nothing new is read that the log did not already hold, and nothing is
        written. Media attempts record no exit; a video job does
        (``media_jobs.proxy_label``), so a request whose attempts named none
        takes its job's. Every label is masked again on the way out
        (:func:`masked_exit_label`). One batched read per page, never per row.
        """

        ids = list(answering)
        out: dict[str, dict[str, Any]] = {
            request_id: {"label": None, "tried": []} for request_id in ids
        }
        if not ids:
            return out
        markers = ", ".join("?" * len(ids))
        attempts = conn.execute(
            "SELECT request_id, attempt, proxy_label,"
            " CASE WHEN params LIKE '%\"dials\"%' AND json_valid(params)"
            " THEN json_extract(params, '$.ladder.dials') END AS dials"
            " FROM request_attempts"
            f" WHERE request_id IN ({markers}) ORDER BY request_id, attempt",
            ids,
        ).fetchall()
        by_request: dict[str, list[sqlite3.Row]] = {}
        for row in attempts:
            by_request.setdefault(str(row["request_id"]), []).append(row)
        jobs: dict[str, list[str]] = {}
        for row in conn.execute(
            "SELECT request_id, proxy_label FROM media_jobs"
            f" WHERE request_id IN ({markers}) AND proxy_label IS NOT NULL"
            " ORDER BY created_at, job_id",
            ids,
        ).fetchall():
            jobs.setdefault(str(row["request_id"]), []).append(str(row["proxy_label"]))

        for request_id in ids:
            rows = by_request.get(request_id, [])
            tried: list[str] = []
            label: str | None = None
            for row in rows:
                dials = _loads_or_none(row["dials"]) if row["dials"] else None
                if isinstance(dials, list):
                    for dial in dials:
                        if isinstance(dial, Mapping):
                            _note_exit(tried, dial.get("proxy"))
                _note_exit(tried, row["proxy_label"])
            if rows:
                route_attempt = answering[request_id]
                chosen = next(
                    (row for row in rows if row["attempt"] == route_attempt),
                    rows[-1],
                )
                label = masked_exit_label(chosen["proxy_label"]) or None
            if not tried and request_id in jobs:
                for job_label in jobs[request_id]:
                    _note_exit(tried, job_label)
                label = tried[-1] if tried else None
            out[request_id] = {"label": label, "tried": tried}
        return out

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
        level: int | None = None,
    ) -> None:
        """Point each request at its blobs, compressing only unseen content.

        ``level`` defaults to ``REQUEST_LOG_COMPRESSION_LEVEL`` as the store
        holds it now, read once so one batch is written at one level. Until
        7.72.2 this default was a fixed 9, so the setting changed nothing.
        """
        if not packed:
            return
        if level is None:
            level = self._compression_level
        mapping: list[tuple[str, str | None, str | None]] = []
        # sha -> (packed body, the kind of dictionary it is compressed with).
        blobs: dict[str, tuple[bytes, str]] = {}
        for request_id, (input_blob, rest_blob) in packed.items():
            shas: list[str | None] = []
            for blob, kind in (
                (rest_blob, _DICT_KIND_REST),
                (input_blob, _DICT_KIND_PROMPT),
            ):
                if blob is None:
                    shas.append(None)
                    continue
                sha = hashlib.sha256(blob).hexdigest()
                blobs.setdefault(sha, (blob, kind))
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
            fresh = [
                (sha, blob, kind)
                for sha, (blob, kind) in blobs.items()
                if sha not in known
            ]
            if fresh:
                conn.executemany(
                    "INSERT OR IGNORE INTO body_blobs (sha, dict_id, payload)"
                    " VALUES (?, ?, ?)",
                    [
                        (sha, *self._compress_packed(blob, level=level, kind=kind))
                        for sha, blob, kind in fresh
                    ],
                )
        conn.executemany(
            "INSERT OR REPLACE INTO request_bodies (request_id, sha, input_sha)"
            " VALUES (?, ?, ?)",
            mapping,
        )

    @staticmethod
    def _existing_shas(
        conn: sqlite3.Connection, table: str, shas: Sequence[bytes]
    ) -> dict[bytes, bool]:
        """Map each of ``shas`` already in ``table`` to whether it is complete.

        ``table`` is one of this module's own two tool tables, never input. A
        ``tool_schemas`` row is complete once it holds its definition;
        ``tool_catalogues`` rows always are.
        """

        has_payload = "definition IS NOT NULL" if table == "tool_schemas" else "1"
        found: dict[bytes, bool] = {}
        for start in range(0, len(shas), _SHA_LOOKUP_CHUNK):
            chunk = list(shas[start : start + _SHA_LOOKUP_CHUNK])
            placeholders = ", ".join("?" * len(chunk))
            found.update(
                (bytes(row[0]), bool(row[1]))
                for row in conn.execute(
                    f"SELECT sha, {has_payload} FROM {table}"
                    f" WHERE sha IN ({placeholders})",
                    chunk,
                )
            )
        return found

    def _store_tool_catalogues(
        self,
        conn: sqlite3.Connection,
        batch: list[RequestRecord],
        fresh: list[RequestRecord],
    ) -> None:
        """Store each unseen tools array and tool definition once; count the rest.

        A catalogue already stored costs one indexed lookup per batch and an
        UPDATE of its counters -- Claude Code sends the same array on every
        turn of a session, so that is the overwhelmingly common case. Only an
        unseen catalogue touches ``tool_schemas``.

        ``fresh`` is the subset of ``batch`` not already in the table, the
        same list the totals and the rollup fold in, so a record written twice
        is counted once.
        """

        catalogues: dict[bytes, ToolCatalogue] = {}
        keep: set[bytes] = set()
        for record in batch:
            catalogue = record.tool_catalogue
            if catalogue is None:
                continue
            catalogues.setdefault(catalogue.sha, catalogue)
            if record.keep_tool_definitions:
                keep.update(member.sha for member in catalogue.members)
        if not catalogues:
            return
        known = self._existing_shas(conn, "tool_catalogues", list(catalogues))
        unseen = [
            catalogue for sha, catalogue in catalogues.items() if sha not in known
        ]
        if unseen:
            members = {
                member.sha: member
                for catalogue in unseen
                for member in catalogue.members
            }
            stored = self._existing_shas(conn, "tool_schemas", list(members))
            conn.executemany(
                "INSERT OR IGNORE INTO tool_schemas (sha, name, definition)"
                " VALUES (?, ?, ?)",
                [
                    (sha, member.name, member.definition if sha in keep else None)
                    for sha, member in members.items()
                    if sha not in stored
                ],
            )
            # A definition first seen while body capture was off is stored
            # without its text; the first unseen catalogue that brings it back
            # with capture on fills it in.
            conn.executemany(
                "UPDATE tool_schemas SET definition = ?"
                " WHERE sha = ? AND definition IS NULL",
                [
                    (members[sha].definition, sha)
                    for sha, complete in stored.items()
                    if not complete and sha in keep
                ],
            )
            conn.executemany(
                "INSERT OR IGNORE INTO tool_catalogues"
                " (sha, tool_count, member_shas) VALUES (?, ?, ?)",
                [
                    (catalogue.sha, len(catalogue.members), catalogue.member_shas)
                    for catalogue in unseen
                ],
            )
        counts: dict[bytes, tuple[int, float, float]] = {}
        for record in fresh:
            catalogue = record.tool_catalogue
            if catalogue is None:
                continue
            seen, first, last = counts.get(
                catalogue.sha, (0, record.ts_epoch, record.ts_epoch)
            )
            counts[catalogue.sha] = (
                seen + 1,
                min(first, record.ts_epoch),
                max(last, record.ts_epoch),
            )
        if counts:
            conn.executemany(
                "UPDATE tool_catalogues SET seen = seen + ?,"
                " first_seen = min(coalesce(first_seen, ?), ?),"
                " last_seen = max(coalesce(last_seen, ?), ?)"
                " WHERE sha = ?",
                [
                    (seen, first, first, last, last, sha)
                    for sha, (seen, first, last) in counts.items()
                ],
            )

    def _flush(self, batch: list[RequestRecord], conn: sqlite3.Connection) -> None:
        # Fingerprinted here, on the writer thread, and nowhere else: a
        # 212-tool catalogue is milliseconds of hashing that no request should
        # wait for. Before the rows, because the row carries the hash.
        for record in batch:
            if record.tool_catalogue is None and record.tools:
                try:
                    record.tool_catalogue = self._tool_fingerprinter.fingerprint(
                        record.tools
                    )
                except Exception as exc:
                    logger.debug("Tool catalogue fingerprint skipped: {}", exc)
            self._price_record(record)
        rows = [self._record_to_row(record) for record in batch]
        packed: dict[str, tuple[bytes | None, bytes | None]] = {}
        if self._compress_bodies:
            for record in batch:
                blobs = self._pack_record(record)
                if blobs != (None, None):
                    packed[record.id] = blobs
        already_stored: set[str] = set()
        try:
            with conn:
                already_stored = self._existing_ids(
                    conn, [record.id for record in batch]
                )
                if self._compress_bodies:
                    # Inside the transaction, so no prune of another process
                    # can delete a value between its lookup and the row naming it.
                    rows = self._store_request_values(conn, rows)
                conn.executemany(_REQUEST_INSERT_SQL, rows)
                self._store_bodies(conn, packed)
                self._store_images(conn, batch)
                self._store_media(conn, batch)
                written = Counter(record.id for record in batch)
                self._store_attempts(
                    conn,
                    batch,
                    already_stored
                    | {
                        request_id for request_id, count in written.items() if count > 1
                    },
                )
                # One list, computed once and shared, so the two aggregates
                # provably fold in the same set of records.
                fresh = [record for record in batch if record.id not in already_stored]
                self._store_tool_catalogues(conn, batch, fresh)
                self._accumulate_totals(conn, fresh)
                # Inside the same ``with conn:`` as the rows themselves, so a
                # failed batch rolls the rollup back with it and the aggregate
                # can never drift ahead of the table it summarises.
                self._accumulate_rollup(conn, fresh)
        except sqlite3.Error as exc:
            logger.warning("Request log write failed: {}", exc)
            return
        if already_stored or len({record.id for record in batch}) < len(batch):
            # A request written again replaces its links, so a body, picture
            # or file its earlier write named may now be named by nothing.
            # After the commit, so the pass that sweeps for it can see it.
            self._owe_full_sweeps("body_blobs", "image_blobs", "media_blobs")
        # After the rows are committed, so the files this batch stored are
        # counted -- with the cap of the newest request that stored one.
        cap = next(
            (
                record.media_store_max_bytes
                for record in reversed(batch)
                if any(output.stored for output in record.media_outputs)
            ),
            0,
        )
        if cap > 0:
            self._trim_media(conn, cap)
        self._inserts_since_prune += len(batch)
        if self._inserts_since_prune >= _PRUNE_EVERY_INSERTS:
            self._inserts_since_prune = 0
            self.prune()
            # Arithmetic on what is in memory unless a kind is due; this is
            # what lets a fresh install start compressing properly, and a
            # dictionary be relearned, without waiting for a restart.
            self._maybe_refresh_dictionaries()

    def _store_request_values(
        self, conn: sqlite3.Connection, rows: list[tuple[Any, ...]]
    ) -> list[tuple[Any, ...]]:
        """Swap each row's metadata text for the id of its ``request_values`` row.

        Called inside the write transaction. A text is replaced only by an id
        whose stored value reads back as that exact text through the reader's
        own lookup; anything else stays in its column, as an older version
        would write it.
        """
        known: dict[str, int | None] = {}
        stored: list[tuple[Any, ...]] = []
        for row in rows:
            values = list(row)
            for column, ref in zip(
                _STORED_ONCE_COLUMN_INDEXES, _STORED_ONCE_REF_INDEXES, strict=True
            ):
                text = values[column]
                if not isinstance(text, str):
                    continue
                if text not in known:
                    known[text] = self._proven_value_id(conn, text)
                value_id = known[text]
                if value_id is not None:
                    values[column] = None
                    values[ref] = value_id
            stored.append(tuple(values))
        return stored

    def _proven_value_id(
        self, conn: sqlite3.Connection, text: str, added: list[int] | None = None
    ) -> int | None:
        """The ``request_values`` id holding ``text``, adding it if needed.

        None -- keep the text inline -- unless the id reads back as exactly
        ``text`` through ``_load_request_values``, the readers' own lookup.
        Must run inside a write transaction. ``added`` collects the size of
        each value this call had to add.
        """
        try:
            encoded = text.encode("utf-8")
            digest = hashlib.sha256(encoded).digest()[:_VALUE_DIGEST_BYTES]
            value_id: int | None = None
            for found_id, found in conn.execute(
                "SELECT id, value FROM request_values WHERE digest = ? ORDER BY id",
                (digest,),
            ):
                if found == text:
                    value_id = int(found_id)
                    break
            if value_id is None:
                cursor = conn.execute(
                    "INSERT INTO request_values (digest, value) VALUES (?, ?)",
                    (digest, text),
                )
                value_id = int(cursor.lastrowid or 0)
                if added is not None:
                    added.append(len(encoded))
            if not value_id:
                return None
            check = _load_request_values(conn, {value_id}).get(value_id)
            if check is None or check.encode("utf-8") != encoded:
                return None
            return value_id
        except sqlite3.Error:
            raise
        except Exception:  # every other failure means "keep the text inline"
            return None

    def _request_values(
        self, conn: sqlite3.Connection, rows: Sequence[sqlite3.Row]
    ) -> dict[int, str]:
        """The stored-once texts the given rows name, by id, cached."""
        wanted: set[int] = set()
        for row in rows:
            keys = row.keys()
            for ref in _STORED_ONCE_REFS:
                if ref in keys and row[ref] is not None:
                    wanted.add(int(row[ref]))
        if not wanted:
            return {}
        cache = self._value_cache
        found: dict[int, str] = {}
        for value_id in wanted:
            # One ``get``, never ``in`` then ``[]``: another reader may clear
            # the cache between the two.
            cached = cache.get(value_id)
            if cached is not None:
                found[value_id] = cached
        missing = wanted - found.keys()
        if missing:
            loaded = _load_request_values(conn, missing)
            if len(cache) + len(loaded) > _VALUE_CACHE_MAX:
                cache.clear()
            cache.update(loaded)
            found.update(loaded)
        return found

    @staticmethod
    def _price_record(record: RequestRecord) -> None:
        """Run a row's own pricer, once, on this (the writer) thread.

        A row that already carries a price or a source keeps it. A pricer
        that fails leaves the row unpriced rather than unwritten: a request
        already answered is never lost to arithmetic about it.
        """
        pricer = record.pricer
        if pricer is None:
            return
        record.pricer = None
        if record.cost_usd is not None or record.cost_source is not None:
            return
        try:
            record.cost_usd, record.cost_source = pricer()
        except Exception as exc:
            logger.debug("Request cost skipped: {}", exc)

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
            record.ttft_winner_ms,
            record.reasoning_tokens,
            record.tool_catalogue.sha if record.tool_catalogue is not None else None,
            record.session_id,
            record.agent_id,
            record.parent_session_id,
            record.project_dir,
            record.origin_source,
            record.keepalive_frames,
            record.media_operation,
            record.output_image_count,
            record.media_bytes_out,
            record.media_sha_out,
            record.output_audio_seconds,
            record.input_audio_seconds,
            record.media_job_id,
            record.output_video_seconds,
            record.credential_event,
            # The refs: filled inside the write transaction, once the values
            # are in ``request_values``; see ``_store_request_values``.
            None,
            None,
            None,
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
        # A dictionary trainer reading samples notices the close between two
        # chunks and lets go of its connection; once this lock is free it has
        # none open. A training already under way holds no connection, and a
        # result that arrives after the writer stopped is never stored.
        with self._trainer_conn_lock:
            pass
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
        session: str | None = None,
        folder: str | None = None,
        exit: str | None = None,
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
            # ``cancelled:<sub-label>`` narrows to one of the four things
            # "cancelled" means. The status half is still the plain indexed
            # equality it always was -- the sub-label is an extra predicate
            # beside it, never instead of it -- so ``status=cancelled`` and
            # every URL that carries it keep selecting all four.
            status_value, sub_label = split_status_filter(status)
            # ``success:<sub-label>`` the same way: ``status = 'success'``
            # stays the indexed equality and the label is a predicate beside
            # it. A cancelled value passes through this untouched.
            status_value, success_label = split_success_status_filter(status_value)
            clauses.append("status = ?")
            args.append(status_value)
            if sub_label is not None:
                clauses.append(f"{sub_label_case_sql()} = ?")
                args.append(sub_label)
            if success_label is not None:
                clauses.append(f"{success_sub_label_case_sql()} = ?")
                args.append(success_label)
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
        # Where the request came from (7.43.0). Last, so a query that sets
        # neither is the same SQL, argument for argument, as before it.
        #
        # Written as ``rowid IN (...)`` over ``idx_requests_origin_v1`` rather
        # than as a plain predicate, because the plain predicate lost the plan:
        # the dashboard always sends ``local``, and SQLite, which has no
        # statistics here, prefers the ``is_local = ?`` equality on the
        # covering stats index and then reads every row to test the folder.
        # Measured on a synthetic 440,000-row log sized like the real one:
        # folder + ``local=hide`` count 1.185 s -> 0.096 s, session +
        # ``harness`` count 0.535 s -> 0.019 s. The outer query keeps its own
        # covering index and checks each row against a bloom filter of the
        # subquery's rowids.
        #
        # The window is repeated inside the subquery so it is a range seek on
        # the index's leading ``ts_epoch`` rather than a walk of every row
        # that has ever carried an origin -- which, once capture has been on
        # for a while, is most of the log.
        window_sql = ""
        window_args: list[Any] = []
        if since is not None:
            window_sql += " AND o.ts_epoch >= ?"
            window_args.append(since)
        if until is not None:
            window_sql += " AND o.ts_epoch <= ?"
            window_args.append(until)
        session_value = session_filter(session)
        if session_value is not None:
            clauses.append(
                "rowid IN (SELECT o.rowid FROM requests AS o"
                " WHERE o.session_id IS NOT NULL"
                f" AND substr(o.session_id, 1, ?) = ?{window_sql})"
            )
            args.extend([len(session_value), session_value, *window_args])
        folder_match = folder_filter(folder)
        if folder_match is not None:
            match_kind, folder_value = folder_match
            if match_kind == "exact":
                predicate = "o.project_dir = ?"
                args_for_folder: list[Any] = [folder_value]
            else:
                predicate = f"o.project_dir LIKE ? ESCAPE '{_LIKE_ESCAPE}'"
                args_for_folder = [_like_contains(folder_value)]
            clauses.append(
                "rowid IN (SELECT o.rowid FROM requests AS o"
                f" WHERE o.project_dir IS NOT NULL AND {predicate}{window_sql})"
            )
            args.extend([*args_for_folder, *window_args])
        # Which exit the request went out through (7.88.0), after the origin
        # clauses and only when set, so a query that does not ask is the SQL
        # it was. It matches what the Requests table's Exit cell shows: the
        # exit each attempt ended on (``request_attempts.proxy_label``), every
        # exit a chain dialled on the way (``params.ladder.dials``, 7.81.0
        # rotation), and a video job's exit (media attempts record none).
        #
        # Correlated on the attempt's primary key, so the outer query keeps
        # its own index and window and each row it reads costs one seek; the
        # JSON is opened only on attempts whose params mention dials, and only
        # when it parses. ``media_jobs`` is small (pruned an hour after its
        # request), so it is read once as a list.
        exit_value = exit_filter(exit)
        if exit_value is not None:
            pattern = _like_contains(exit_value)
            like = f"LIKE ? ESCAPE '{_LIKE_ESCAPE}'"
            clauses.append(
                "(EXISTS (SELECT 1 FROM request_attempts AS xa"
                " WHERE xa.request_id = requests.id"
                f" AND (xa.proxy_label {like}"
                " OR (xa.params LIKE '%\"dials\"%' AND EXISTS (SELECT 1"
                " FROM json_each(CASE WHEN json_valid(xa.params)"
                " THEN xa.params END, '$.ladder.dials') AS xd"
                f" WHERE json_extract(xd.value, '$.proxy') {like}))))"
                " OR requests.id IN (SELECT xj.request_id FROM media_jobs AS xj"
                f" WHERE xj.proxy_label {like}))"
            )
            args.extend([pattern, pattern, pattern])
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
        session: str | None = None,
        folder: str | None = None,
        exit: str | None = None,
        body_preview_chars: int | None = LIST_BODY_PREVIEW_CHARS,
    ) -> tuple[list[dict[str, Any]], int]:
        """Return (rows, total) newest-first, with bodies truncated for list views.

        The counting form. :meth:`list_requests_page` is the same query without
        the ``COUNT(*)``; this wrapper stays because every caller that wants a
        total wants it in exactly this shape.
        """

        rows, total, _has_more = self.list_requests_page(
            limit=limit,
            offset=offset,
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
            session=session,
            folder=folder,
            exit=exit,
            body_preview_chars=body_preview_chars,
            include_total=True,
        )
        return rows, total if total is not None else 0

    def list_requests_page(
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
        session: str | None = None,
        folder: str | None = None,
        exit: str | None = None,
        body_preview_chars: int | None = LIST_BODY_PREVIEW_CHARS,
        include_total: bool = True,
        include_exits: bool = False,
    ) -> tuple[list[dict[str, Any]], int | None, bool]:
        """Return ``(rows, total, has_more)``; ``total`` is ``None`` when skipped.

        The page and the count are wildly different queries against the same
        predicate. The page walks the timestamp index backwards and stops at
        the first ``limit`` matches; the count has to consider every row, and
        for a free-text ``q`` that means decompressing a stored body per row.
        Measured on a 4.5 GB log for a term present in real traffic: the 25-row
        page **0.06 s**, the ``COUNT(*)`` over the same predicate **383.66 s**.

        So ``include_total=False`` asks only the cheap question. ``has_more``
        replaces the total for the one thing the pager needed it for: it is
        answered by fetching ``limit + 1`` rows and returning ``limit`` of
        them, which costs one extra row rather than a full scan.

        Page membership and ordering are identical either way -- the same
        ``WHERE``, the same ``ORDER BY ts_epoch DESC``, the same ``LIMIT``/
        ``OFFSET``. Only the count moves.

        ``include_exits`` (7.88.0, the Requests table's Exit column) adds one
        key to each row, ``exit`` (see :meth:`_fetch_exits`), read in one
        batched query of the page's attempts. Off by default, so every other
        caller's rows are exactly what they were.
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
            session=session,
            folder=folder,
            exit=exit,
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
        columns = ", ".join(_LIST_METADATA_COLUMNS + _STORED_ONCE_REFS)
        # Read before the page's own connection is opened, not inside it: it is
        # cached for five seconds and shared with every other derived answer, so
        # a page normally pays nothing for it.
        boundaries = self.restart_boundaries()
        # One more row than the page, so "is there a next page" is answered
        # without a count. It is discarded before anything is rendered.
        fetch = limit if include_total else limit + 1
        with self._connection() as conn:
            total: int | None = None
            if include_total:
                total = conn.execute(
                    f"SELECT COUNT(*) FROM requests{where}", args
                ).fetchone()[0]
            cursor = conn.execute(
                f"SELECT {columns}, {body_select} FROM requests{where}"
                " ORDER BY ts_epoch DESC LIMIT ? OFFSET ?",
                [*body_args, *args, fetch, offset],
            )
            raw_rows = cursor.fetchall()
            has_more = len(raw_rows) > limit
            raw_rows = raw_rows[:limit]
            bodies = self._fetch_bodies(conn, [str(row["id"]) for row in raw_rows])
            values = self._request_values(conn, raw_rows)
            rows = [
                self._row_to_dict(
                    row,
                    body_preview_chars=body_preview_chars,
                    bodies=bodies.get(str(row["id"])),
                    boundaries=boundaries,
                    values=values,
                )
                for row in raw_rows
            ]
            if include_exits:
                exits = self._fetch_exits(
                    conn, {str(row["id"]): row["route_attempt"] for row in raw_rows}
                )
                for row in rows:
                    row["exit"] = exits[str(row["id"])]
        if total is not None:
            has_more = offset + len(rows) < total
        return rows, total, has_more

    def count_requests(
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
        session: str | None = None,
        folder: str | None = None,
        exit: str | None = None,
    ) -> int:
        """How many rows match, and nothing else.

        The other half of :meth:`list_requests_page` with ``include_total``
        off: the page is rendered from that one, and this is fired beside it
        and awaited by nobody. Exactly the same ``WHERE``, so the number that
        eventually arrives is the number the page was counting.
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
            session=session,
            folder=folder,
            exit=exit,
        )
        with self._connection() as conn:
            return int(
                conn.execute(f"SELECT COUNT(*) FROM requests{where}", args).fetchone()[
                    0
                ]
            )

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
        session: str | None = None,
        folder: str | None = None,
        exit: str | None = None,
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
        # Fifteen elements, and the first is a literal string. ``stats()`` keys
        # on a thirteen-element tuple of filters (``exit`` joined in 7.88.0;
        # both grew by one together), and the two live in the same dict:
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
            session,
            folder,
            exit,
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
            session=session,
            folder=folder,
            exit=exit,
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
        boundaries = self.restart_boundaries()
        with self._connection() as conn:
            cursor = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            bodies = self._fetch_bodies(conn, [request_id])
            images = self._fetch_images(conn, request_id)
            media = self._fetch_media(conn, request_id)
            attempts = self._fetch_attempts(conn, request_id)
            values = self._request_values(conn, [row])
            # The guarded ALTER in ``_init_db`` guarantees the column.
            catalogue_sha = row["tool_catalogue_sha"]
            tool_catalogue = (
                self._fetch_tool_catalogue(conn, bytes(catalogue_sha))
                if catalogue_sha
                else None
            )
        data = self._row_to_dict(
            row,
            body_preview_chars=None,
            bodies=bodies.get(request_id),
            boundaries=boundaries,
            values=values,
        )
        data["input_images"] = images
        data["media"] = media
        data["route_attempts"] = attempts
        data["tool_catalogue"] = tool_catalogue
        return data

    @staticmethod
    def _fetch_media(conn: sqlite3.Connection, request_id: str) -> list[dict[str, Any]]:
        """One request's media, inputs then outputs (7.68.0).

        The facts are always there -- direction, position, content address,
        type, size -- and ``stored`` says whether the media store holds the
        file itself, which is the only case the request detail previews.
        """
        rows = conn.execute(
            "SELECT m.direction, m.idx, m.sha256, b.mime, b.bytes, b.stored"
            " FROM request_media AS m LEFT JOIN media_blobs AS b"
            " ON b.sha256 = m.sha256"
            " WHERE m.request_id = ? ORDER BY m.direction, m.idx",
            (request_id,),
        ).fetchall()
        return [
            {
                "direction": row["direction"],
                "idx": row["idx"],
                "sha256": row["sha256"],
                "mime": row["mime"],
                "bytes": row["bytes"],
                "stored": bool(row["stored"]),
            }
            for row in rows
        ]

    @staticmethod
    def _fetch_tool_catalogue(
        conn: sqlite3.Connection, sha: bytes
    ) -> dict[str, Any] | None:
        """One catalogue as the request modal shows it: names and hashes, no bodies."""

        row = conn.execute(
            "SELECT tool_count, member_shas, first_seen, last_seen, seen"
            " FROM tool_catalogues WHERE sha = ?",
            (sha,),
        ).fetchone()
        if row is None:
            return None
        member_shas = split_member_shas(bytes(row["member_shas"]))
        names: dict[bytes, str] = {}
        distinct = list(dict.fromkeys(member_shas))
        for start in range(0, len(distinct), _SHA_LOOKUP_CHUNK):
            chunk = distinct[start : start + _SHA_LOOKUP_CHUNK]
            names.update(
                (bytes(found[0]), str(found[1]))
                for found in conn.execute(
                    "SELECT sha, name FROM tool_schemas"
                    f" WHERE sha IN ({', '.join('?' * len(chunk))})",
                    chunk,
                )
            )
        return {
            "sha": sha.hex(),
            "tool_count": int(row["tool_count"]),
            "tools": [
                {"name": names.get(member), "sha": member.hex()}
                for member in member_shas
            ],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "seen": int(row["seen"]),
        }

    def requests_carrying_tool(self, name: str, *, limit: int = 25) -> dict[str, Any]:
        """Which requests carried a tool of this name, newest first.

        A tool is found by name through every definition it has had, and
        through every catalogue that carried one of them. The request query
        is bounded by those catalogues' first and last sightings, so it walks
        the timestamp index over that window rather than the whole log: a tool
        seen in one afternoon's session costs that afternoon.

        ``seen`` is the catalogues' own counter -- every request that carried
        one since it was first stored -- so it is not reduced by retention the
        way the listed rows are.
        """

        limit = max(1, min(limit, 500))
        carriers = (
            "SELECT c.sha FROM tool_catalogues c WHERE EXISTS ("
            " SELECT 1 FROM tool_schemas t WHERE t.name = ?"
            # instr() on blobs is byte-wise; a member starts on a 32-byte
            # boundary, so only a match at 1, 33, 65, ... is a member.
            f" AND instr(c.member_shas, t.sha) % {TOOL_SHA_BYTES} = 1)"
        )
        with self._connection() as conn:
            definitions = int(
                conn.execute(
                    "SELECT COUNT(*) FROM tool_schemas WHERE name = ?", (name,)
                ).fetchone()[0]
            )
            summary = conn.execute(
                "SELECT COUNT(*), SUM(seen), MIN(first_seen), MAX(last_seen)"
                f" FROM tool_catalogues WHERE sha IN ({carriers})",
                (name,),
            ).fetchone()
            catalogues = int(summary[0] or 0)
            rows: list[dict[str, Any]] = []
            has_more = False
            if catalogues:
                window = ""
                args: list[Any] = [name]
                if summary[2] is not None and summary[3] is not None:
                    window = " AND ts_epoch BETWEEN ? AND ?"
                    args.extend([summary[2], summary[3]])
                found = conn.execute(
                    "SELECT id, ts_epoch, ts_iso, requested_model, resolved_model,"
                    " provider, status, tool_catalogue_sha FROM requests"
                    f" WHERE tool_catalogue_sha IN ({carriers}){window}"
                    " ORDER BY ts_epoch DESC LIMIT ?",
                    [*args, limit + 1],
                ).fetchall()
                has_more = len(found) > limit
                for record in found[:limit]:
                    item = dict(record)
                    item["tool_catalogue_sha"] = bytes(item["tool_catalogue_sha"]).hex()
                    rows.append(item)
        return {
            "name": name,
            "definitions": definitions,
            "catalogues": catalogues,
            "seen": int(summary[1] or 0),
            "first_seen": summary[2],
            "last_seen": summary[3],
            "rows": rows,
            "has_more": has_more,
        }

    def iter_export_rows(
        self,
        *,
        columns: list[str],
        need_bodies: bool,
        need_ladder: bool = False,
        need_exits: bool = False,
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
        session: str | None = None,
        folder: str | None = None,
        exit: str | None = None,
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

        The sub-label is derived, so the SELECT carries the columns it is
        derived from even when the caller did not name them. They go into the
        SQL, never into the caller's ``columns``: the route projects each row
        down to the columns it asked for, so an export gains exactly one field
        and not five. The success sub-label tops the SELECT up the same way.
        """
        sql_columns = list(columns) + [
            column for column in _CANCEL_REASON_SOURCE_COLUMNS if column not in columns
        ]
        sql_columns += [
            column
            for column in SUCCESS_REASON_SOURCE_COLUMNS
            if column not in sql_columns
        ]
        # The Exit columns (7.88.0) name the answering attempt by the row's
        # ``route_attempt``; topped up the same way, never into ``columns``.
        if need_exits and "route_attempt" not in sql_columns:
            sql_columns.append("route_attempt")
        # A stored-once column is read with its ref; ``_row_to_dict`` puts the
        # text back and drops the ref, so the row the caller projects is the
        # same either way.
        sql_columns += [
            ref
            for column, ref in zip(_STORED_ONCE_COLUMNS, _STORED_ONCE_REFS, strict=True)
            if column in sql_columns and ref not in sql_columns
        ]
        boundaries = self.restart_boundaries()
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
            session=session,
            folder=folder,
            exit=exit,
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
                    f"SELECT {', '.join(sql_columns)} FROM requests{page_where}"
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
                # The Exit columns, from the same one batched read the
                # Requests table's Exit cell uses, so a download and the page
                # it came from name the same exits.
                exits = (
                    self._fetch_exits(
                        conn, {str(row["id"]): row["route_attempt"] for row in rows}
                    )
                    if need_exits
                    else {}
                )
                values = self._request_values(conn, rows)
                for row in rows:
                    data = self._row_to_dict(
                        row,
                        body_preview_chars=None,
                        bodies=bodies.get(str(row["id"])),
                        boundaries=boundaries,
                        values=values,
                    )
                    if need_ladder:
                        data.update(_EMPTY_LADDER_ROLLUP)
                        data.update(ladders.get(str(row["id"]), {}))
                    if need_exits:
                        found = exits[str(row["id"])]
                        # Empty, not "direct", when nothing was recorded: no
                        # chain and no proxy is not a measurement of Direct.
                        data["exit"] = found["label"] or ""
                        data["exits"] = "; ".join(found["tried"])
                    yield data
                cursor = (rows[-1]["ts_epoch"], rows[-1]["id"])
        finally:
            conn.close()

    def iter_export_attempt_rows(
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
        session: str | None = None,
        folder: str | None = None,
        exit: str | None = None,
        page_size: int = 1_000,
    ) -> Generator[dict[str, Any]]:
        """Yield one row per *attempt*, carrying its request's dimensions.

        The filters are the request-log filters, applied to ``requests`` by the
        same ``_where`` the request export uses: an attempt is in the export
        exactly when the request it belongs to would have been. That is a
        deliberate reuse rather than a second predicate -- ``provider``,
        ``key_label``, ``ts_epoch``, ``tokens_in``, ``cost_usd``, ``error_kind``,
        ``duration_ms`` and ``params`` all exist on *both* tables, so a literal
        join would have made every clause in ``_where`` ambiguous, and a
        hand-qualified copy of it would drift from the original on the first
        filter either side gained.

        Streams the same way ``iter_export_rows`` does: keyset pagination over
        ``(ts_epoch, id)`` newest-first, one batched read of the attempt side
        per page, one connection for the whole walk. Peak memory is a page of
        requests and their attempts, never the table -- a real log has 571k of
        them.

        A request with no recorded attempts contributes no rows. Ordering is
        newest request first, attempts in ascending chain order within it, so
        a fallback chain reads top to bottom.
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
            session=session,
            folder=folder,
            exit=exit,
        )
        boundaries = self.restart_boundaries()
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
                    "SELECT id, ts_epoch, ts_iso, harness, endpoint,"
                    " requested_model, resolved_model, status,"
                    # Not exported as columns of their own: they are here only
                    # so the parent's cancelled sub-label can ride on the
                    # attempt beside ``request_status``, which is the column
                    # that raised the question. The last three are the same
                    # for its success sub-label.
                    " ttft_ms, duration_ms, output_chars, thinking_chars,"
                    " tool_call_count, tokens_out, optimization"
                    f" FROM requests{page_where}"
                    " ORDER BY ts_epoch DESC, id DESC LIMIT ?"
                )
                rows = conn.execute(page_sql, [*page_args, page_size]).fetchall()
                if not rows:
                    return
                parents = {str(row["id"]): row for row in rows}
                for attempt in self._fetch_export_attempts(conn, list(parents)):
                    parent = parents.get(str(attempt["request_id"]))
                    if parent is None:  # pragma: no cover - defensive
                        continue
                    attempt["ts_iso"] = parent["ts_iso"]
                    attempt["harness"] = parent["harness"]
                    attempt["endpoint"] = parent["endpoint"]
                    attempt["requested_model"] = parent["requested_model"]
                    attempt["resolved_model"] = parent["resolved_model"]
                    attempt["request_status"] = parent["status"]
                    attempt["request_cancel_reason"] = classify_cancelled(
                        status=parent["status"],
                        ts_epoch=parent["ts_epoch"],
                        ttft_ms=parent["ttft_ms"],
                        duration_ms=parent["duration_ms"],
                        output_chars=parent["output_chars"],
                        thinking_chars=parent["thinking_chars"],
                        boundaries=boundaries,
                    )
                    attempt["request_success_reason"] = classify_success(
                        status=parent["status"],
                        output_chars=parent["output_chars"],
                        tool_call_count=parent["tool_call_count"],
                        thinking_chars=parent["thinking_chars"],
                        tokens_out=parent["tokens_out"],
                        optimization=parent["optimization"],
                    )
                    yield attempt
                cursor = (rows[-1]["ts_epoch"], rows[-1]["id"])
        finally:
            conn.close()

    def _fetch_export_attempts(
        self, conn: sqlite3.Connection, request_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Read one page's attempts, newest request first, chain order within.

        ``wire_body`` is deliberately not selected: it is the largest column on
        the table and the only thing the export wants from it -- which of a
        multi-surface gateway's endpoints the attempt was posted to -- is
        already summarised in ``params.wire.surface``.

        Skipped attempts stored compactly (7.76.0) are exported exactly as the
        rows they replaced; a stored row wins over a compact attempt of the
        same number.
        """
        if not request_ids:
            return []
        order = {request_id: index for index, request_id in enumerate(request_ids)}
        markers = ", ".join("?" * len(request_ids))
        with _one_snapshot(conn):
            rows: list[Mapping[str, Any]] = list(
                conn.execute(
                    "SELECT request_id, attempt, provider, model_ref, outcome,"
                    " error_kind, error_message, duration_ms, params,"
                    " reasoning_emitted, key_index, key_label, ladder_tries,"
                    " tokens_in, tokens_out, cost_usd, cost_source, ttft_ms,"
                    " first_reasoning_ms, proxy_label"
                    " FROM request_attempts"
                    f" WHERE request_id IN ({markers})",
                    request_ids,
                ).fetchall()
            )
            packed = self._skip_rows(conn, request_ids)
        if packed:
            present = {(str(row["request_id"]), row["attempt"]) for row in rows}
            rows.extend(
                row
                for request_id, members in packed.items()
                for row in members
                if (request_id, row["attempt"]) not in present
            )
        out: list[dict[str, Any]] = [
            {
                "request_id": str(row["request_id"]),
                "attempt": row["attempt"],
                "attempt_provider": row["provider"],
                "attempt_model": row["model_ref"],
                "outcome": row["outcome"],
                "error_kind": row["error_kind"],
                "error_message": row["error_message"],
                "duration_ms": row["duration_ms"],
                "reasoning_emitted": row["reasoning_emitted"],
                "key_index": row["key_index"],
                "key_label": row["key_label"],
                "ladder_tries": row["ladder_tries"],
                "tokens_in": row["tokens_in"],
                "tokens_out": row["tokens_out"],
                "cost_usd": row["cost_usd"],
                "cost_source": row["cost_source"],
                "ttft_ms": row["ttft_ms"],
                "first_reasoning_ms": row["first_reasoning_ms"],
                "proxy_label": row["proxy_label"],
                ATTEMPT_PARAMS_KEY: _loads_or_none(row["params"]),
            }
            for row in rows
        ]
        # Sorted here rather than in SQL: the page's request order is the
        # keyset order (``ts_epoch DESC, id DESC``), which ``ORDER BY
        # request_id`` would not reproduce.
        out.sort(key=lambda item: (order[item["request_id"]], item["attempt"]))
        return out

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
        session: str | None = None,
        folder: str | None = None,
        exit: str | None = None,
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
            session=session,
            folder=folder,
            exit=exit,
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
        boundaries: Sequence[float] = (),
        values: Mapping[int, str] | None = None,
    ) -> dict[str, Any]:
        data = dict(row)
        # First, so everything below sees exactly the row an inline write
        # would have produced.
        _restore_stored_once(data, values or {})
        data["stream"] = bool(data["stream"])
        # Stored as the raw 32 bytes, half the size of hex on every row; read
        # back as hex so JSON, CSV and the modal all carry the same string.
        catalogue_sha = data.get("tool_catalogue_sha")
        if isinstance(catalogue_sha, bytes):
            data["tool_catalogue_sha"] = catalogue_sha.hex()
        # Display forms of the origin, derived here so the list and the detail
        # say the same thing, and never stored: a presentation decision frozen
        # into history could not be changed. Only when the query projected the
        # column, for the same reason ``cancel_reason`` below is.
        if "session_id" in data:
            data["session_short"] = session_short(data.get("session_id"))
        if "project_dir" in data:
            data["project_short"] = project_short(data.get("project_dir"))
        if "origin_source" in data:
            data["origin_provenance"] = origin_provenance(data.get("origin_source"))
        # Which of the four things "cancelled" means, derived here so that the
        # list, the detail and the export all answer it the same way and no
        # caller has to know the rule. NULL on every other status -- a
        # successful request was not cancelled for any reason -- and absent
        # entirely when the query did not project ``status``, because a label
        # guessed from columns nobody selected would be a fabrication.
        if "status" in data:
            data["cancel_reason"] = classify_cancelled(
                status=data.get("status"),
                ts_epoch=data.get("ts_epoch"),
                ttft_ms=data.get("ttft_ms"),
                duration_ms=data.get("duration_ms"),
                output_chars=data.get("output_chars"),
                thinking_chars=data.get("thinking_chars"),
                boundaries=boundaries,
            )
            # Which of the two things a success with no answer carried, by the
            # same rule. Only when every column the rule reads was projected:
            # a label computed from a column the query left out -- say
            # ``optimization`` -- would call a local answer an empty turn.
            data["success_reason"] = (
                classify_success_row(data)
                if all(column in data for column in SUCCESS_REASON_SOURCE_COLUMNS)
                else None
            )
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
        session: str | None = None,
        folder: str | None = None,
        exit: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate analytics, served from the rollup where it can be.

        The payload carries ``served_from``: ``"rollup"`` when the whole answer
        came from the pre-aggregated tables, ``"rows"`` when it was computed by
        scanning ``requests``. Free-text search forces the scan -- it is a
        correlated EXISTS over compressed bodies and is not a rollup dimension
        -- and so do ``session``, ``folder`` and ``exit``, which are
        deliberately not rollup dimensions either (a session id is unbounded
        and would multiply the hour buckets by the number of conversations in
        each; an exit belongs to the attempts). So does the window before the
        one-time backfill has finished.
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
            session,
            folder,
            exit,
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
        # A cancelled sub-label is not a rollup dimension and deliberately never
        # will be: the rollup tables are incremental counters keyed on
        # ``requests.status``, so teaching them a fifth dimension would mean
        # rebuilding every historical bucket for 0.4% of traffic. The sub-label
        # is derived from the request row instead, which only the row scan can
        # do -- exactly the way a free-text search already forces this path.
        _, sub_label = split_status_filter(status)
        # A success sub-label is derived from the row for the same reason and
        # takes the same path.
        _, success_label = split_success_status_filter(status)
        # The exit (7.88.0) is not a rollup dimension either: it lives on the
        # attempts, not on the request row the rollup counts.
        origin_filtered = (
            session_filter(session) is not None
            or folder_filter(folder) is not None
            or exit_filter(exit) is not None
        )
        if (
            not q
            and sub_label is None
            and success_label is None
            and not origin_filtered
        ):
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
                session=session,
                folder=folder,
                exit=exit,
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
        session: str | None = None,
        folder: str | None = None,
        exit: str | None = None,
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
            session=session,
            folder=folder,
            exit=exit,
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
                       AVG(ttft_ms) AS avg_ttft_ms,
                       -- ``ttft_winner_ms`` is NOT in ``idx_requests_stats_v4``
                       -- and is deliberately not added to it. Measured on the
                       -- real 343,389-row copy: this one average turns the
                       -- totals query from COVERING INDEX into INDEX, 0.118 s
                       -- -> 0.642 s. Widening the index to ``_v5`` restores the
                       -- coverage (0.672 -> 0.239 s, 2.3 s to build, 42.2 MB)
                       -- but makes the two other queries that share it *worse*
                       -- -- the per-provider breakdown 0.463 -> 0.601 s and the
                       -- latency buckets 0.077 -> 0.146 s -- which is exactly
                       -- the planner regression ``_ensure_stats_index``
                       -- warns about, for a net gain of 0.23 s across the
                       -- three. So the half-second is paid here instead, on the
                       -- path that only runs when a filter defeats the rollup
                       -- (a ``q=`` search, which costs seconds on its own).
                       AVG(ttft_winner_ms) AS avg_ttft_winner_ms
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
            # What the reader waited, and what the model that answered actually
            # took. ``avg_ttft_ms`` includes every fallback's stall, because it
            # always has; this one does not. None when nothing in the window
            # carried a winner -- every row written before 7.4.0 -- which the
            # reader must render as a dash, never as zero.
            "avg_ttft_winner_ms": _rounded(totals["avg_ttft_winner_ms"]),
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
            # The rollup twin of the scan path's ``avg_ttft_winner_ms``. A
            # bucket rolled up before 7.4.0 carries 0/0 here, which ``_mean``
            # turns into None -- the same answer ``AVG()`` gives over rows whose
            # column is NULL, which is why this counter needed no rebuild.
            "avg_ttft_winner_ms": _rounded(
                _mean(counters["ttft_winner_sum"], counters["ttft_winner_count"])
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
        args: list[Any] = [] if since is None else [since]
        with self._connection() as conn:
            # The time filter moves onto the attempt only once every attempt
            # carries one. Until the marker is set some rows have NULL there,
            # and ``a.ts_epoch >= ?`` would drop exactly the history this
            # question is about -- so the old shape stays the answer, and the
            # new one is an optimisation of it rather than a replacement.
            dated = (
                since is not None
                and self._meta_get(conn, _ATTEMPTS_TS_BACKFILL_KEY) is not None
            )
            since_clause = (
                ""
                if since is None
                else (" AND a.ts_epoch >= ?" if dated else " AND r.ts_epoch >= ?")
            )
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

    def ttft_percentiles(
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
        session: str | None = None,
        folder: str | None = None,
        exit: str | None = None,
    ) -> dict[str, Any]:
        """Overall p50/p95 time-to-first-token, over the same filters as stats.

        An exact scan of ``requests.ttft_ms``, and deliberately so.

        There are no ttft percentiles anywhere else: ``stats()`` only averages
        it (``:5196-5212``) and the histogram writer buckets ``duration_ms``
        alone (``:2932-2940``). 7.5.0's per-model view samples the 20,000
        newest attempt rows and computes percentiles in Python, and those
        per-model figures cannot be composed into an overall one -- percentiles
        do not average.

        The alternative was a ttft histogram beside ``request_stats_latency``,
        with a backfill marker, a backfill walk and a reader change. Measured
        against it: the exact scan is **0.69-0.99 s** over 331,086 measured
        rows on a 4.5 GB log, the same cost class as the ``duration_ms`` scan
        the dashboard already accepts (0.68-0.70 s on the same database), and
        it needs no migration and no backfill because ``requests.ttft_ms`` has
        existed for many releases.

        **It is not inside ``stats()``.** The rollup-served path answers in
        about a tenth of a second, and adding most of a second to it would
        slow the most-used page on the install that has no filter at all. This
        is its own call, fired beside the cost and latency panels and awaited
        by nobody, and it shares ``stats()``'s cache and its 5 s TTL under a
        key of its own arity.

        ``measured`` is the denominator and is part of the answer: a window
        whose rows all predate ttft instrumentation returns ``None`` for both
        percentiles and ``0`` here, which a reader can tell apart from "the
        requests were instant".
        """

        cache_key = (
            "ttft_percentiles",
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
            session,
            folder,
            exit,
        )
        now = time.monotonic()
        with self._stats_lock:
            cached = self._stats_cache.get(cache_key)
            if cached is not None:
                if now - cached[0] < _STATS_CACHE_TTL_SECONDS:
                    self._stats_cache.move_to_end(cache_key)
                    return dict(cached[1])
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
            session=session,
            folder=folder,
            exit=exit,
        )
        with self._connection() as conn:
            percentiles = self._percentiles(
                conn, where, args, (0.50, 0.95), column="ttft_ms"
            )
            connector = " AND" if where else " WHERE"
            measured = conn.execute(
                f"SELECT COUNT(*) FROM requests{where}{connector} ttft_ms IS NOT NULL",
                args,
            ).fetchone()[0]
        payload: dict[str, Any] = {
            "p50_ttft_ms": _rounded(percentiles[0.50]),
            "p95_ttft_ms": _rounded(percentiles[0.95]),
            "measured": int(measured or 0),
        }
        with self._stats_lock:
            # Shares ``stats()``'s cache and its 5 s TTL. The key starts with a
            # string and has its own arity, so it can never collide with the
            # filter tuple ``stats()`` uses.
            self._stats_cache[cache_key] = (now, payload)
            self._stats_cache.move_to_end(cache_key)
            while len(self._stats_cache) > _STATS_CACHE_MAX_ENTRIES:
                self._stats_cache.popitem(last=False)
        return dict(payload)

    def restart_boundaries(self) -> tuple[float, ...]:
        """When a server session stopped being heard from and another started.

        ``server_sessions`` is tiny -- a few hundred rows on a log with four
        hundred thousand requests -- so this is a full read, cached behind
        ``stats()``'s 5 s TTL like every other derived answer here. It is the
        only input :func:`classify_cancelled` needs that is not on the request
        row itself.

        An unavailable table is not an error: it means "no restart is known",
        and every cancelled row then falls through to one of the other three
        labels rather than the page losing its breakdown.
        """

        cache_key = ("restart_boundaries",)
        now = time.monotonic()
        with self._stats_lock:
            cached = self._stats_cache.get(cache_key)
            if cached is not None:
                if now - cached[0] < _STATS_CACHE_TTL_SECONDS:
                    self._stats_cache.move_to_end(cache_key)
                    return tuple(cached[1]["boundaries"])
                del self._stats_cache[cache_key]
        try:
            with self._connection() as conn:
                sessions = [
                    (float(row["started_at"]), float(row["last_seen_at"]))
                    for row in conn.execute(
                        "SELECT started_at, last_seen_at FROM server_sessions"
                    )
                ]
        except sqlite3.Error as exc:
            logger.warning("Server session history unavailable: {}", exc)
            return ()
        boundaries = restart_boundaries(sessions)
        with self._stats_lock:
            self._stats_cache[cache_key] = (now, {"boundaries": boundaries})
            self._stats_cache.move_to_end(cache_key)
            while len(self._stats_cache) > _STATS_CACHE_MAX_ENTRIES:
                self._stats_cache.popitem(last=False)
        return boundaries

    def cancelled_breakdown(
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
        session: str | None = None,
        folder: str | None = None,
        exit: str | None = None,
    ) -> dict[str, Any]:
        """The Cancelled card, split into the four things "cancelled" means.

        A live query, deliberately, and never a rollup dimension. The rollup
        tables are incremental counters keyed on ``requests.status``; adding a
        fifth dimension to them would mean a rebuild of every historical bucket
        for a population that is 0.6% of traffic. Cancelled rows are rare and
        ``idx_requests_status`` seeks straight to them -- 935 rows in 0.07 s on
        a 6.3 GB log -- so the honest shape is to count them when asked.

        Every filter the caller can apply to ``stats()`` applies here too and is
        part of the cache key: a breakdown that ignored the page's filters would
        contradict the card it sits under. ``status`` is the one exception it
        has to think about -- the breakdown is *about* cancelled rows, so a page
        filtered to some other status has nothing to break down and says so.
        """

        cache_key = (
            "cancelled_breakdown",
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
            session,
            folder,
            exit,
        )
        now = time.monotonic()
        with self._stats_lock:
            cached = self._stats_cache.get(cache_key)
            if cached is not None:
                if now - cached[0] < _STATS_CACHE_TTL_SECONDS:
                    self._stats_cache.move_to_end(cache_key)
                    return dict(cached[1])
                del self._stats_cache[cache_key]
        counts = dict.fromkeys(CANCELLED_SUB_LABELS, 0)
        asked_status, asked_sub_label = split_status_filter(status)
        if asked_status is not None and asked_status != CANCELLED_STATUS:
            payload: dict[str, Any] = {
                "total": 0,
                "counts": counts,
                "selected": asked_sub_label,
            }
        else:
            # The page's own status filter is *not* forwarded: a page narrowed
            # to one sub-label still wants to see how that one compares with the
            # other three, and a breakdown that answered "100% of the rows you
            # asked for" would be a tautology. Every other filter is forwarded,
            # so the breakdown and the card above it describe one population.
            where, args = self._where(
                provider=provider,
                model=model,
                status=CANCELLED_STATUS,
                endpoint=endpoint,
                key=key,
                since=since,
                until=until,
                q=q,
                local=local,
                harness=harness,
                session=session,
                folder=folder,
                exit=exit,
            )
            try:
                with self._connection() as conn:
                    rows = conn.execute(
                        f"SELECT {sub_label_case_sql()} AS reason,"
                        f" COUNT(*) AS n FROM requests{where} GROUP BY reason",
                        args,
                    ).fetchall()
            except sqlite3.Error as exc:
                # A readout, not a control: an unreadable log means "no
                # measurement", never an error banner over the page.
                logger.warning("Cancelled breakdown unavailable: {}", exc)
                return {"total": 0, "counts": counts, "selected": asked_sub_label}
            for row in rows:
                reason = str(row["reason"])
                if reason in counts:
                    counts[reason] = int(row["n"] or 0)
            payload = {
                "total": sum(counts.values()),
                "counts": counts,
                "selected": asked_sub_label,
            }
        with self._stats_lock:
            self._stats_cache[cache_key] = (now, payload)
            self._stats_cache.move_to_end(cache_key)
            while len(self._stats_cache) > _STATS_CACHE_MAX_ENTRIES:
                self._stats_cache.popitem(last=False)
        return dict(payload)

    def no_answer_breakdown(
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
        session: str | None = None,
        folder: str | None = None,
        exit: str | None = None,
    ) -> dict[str, Any]:
        """The successes that carried no answer, split into the two shapes.

        ``successes`` is every success the filters select; ``counts`` holds
        the two labels and ``total`` is their sum, so the panel can say "N of
        M successes had no answer" and the parts add up to the whole they
        claim. The rows that are neither answered nor labelled -- written
        before ``tool_call_count`` existed -- are inside ``successes`` and
        outside ``total``, which is the honest place for rows nobody can
        classify.

        A live query and never a rollup dimension, for the reason
        :meth:`cancelled_breakdown` gives -- but *not* part of ``stats()``'s
        answer the way that one is. Cancelled rows are rare and
        ``idx_requests_status`` seeks straight to them; successes are nearly
        every row, and the label reads columns no index carries. Measured
        read-only on an 8.4 GB, 462,567-row log with the dashboard's own
        ``local=hide``: 0.32 s for 24 hours, 0.92 s for 7 days, 3.6 s for 30
        days and 5.2 s all time, against the ~0.1 s rollup-served stats. So
        it has its own route and the dashboard fetches it off the paint path,
        the way the TTFT and cost panels already are.

        Every filter ``stats()`` takes applies here and is part of the cache
        key. ``status`` is handled the way :meth:`cancelled_breakdown`
        handles it: a page filtered to another status has nothing to break
        down, and a page narrowed to one success sub-label still sees both.
        """

        cache_key = (
            "no_answer_breakdown",
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
            session,
            folder,
            exit,
        )
        now = time.monotonic()
        with self._stats_lock:
            cached = self._stats_cache.get(cache_key)
            if cached is not None:
                if now - cached[0] < _STATS_CACHE_TTL_SECONDS:
                    self._stats_cache.move_to_end(cache_key)
                    return dict(cached[1])
                del self._stats_cache[cache_key]
        counts = dict.fromkeys(SUCCESS_SUB_LABELS, 0)
        asked_status, asked_sub_label = split_success_status_filter(status)
        if asked_status is not None and asked_status != SUCCESS_STATUS:
            payload: dict[str, Any] = {
                "successes": 0,
                "total": 0,
                "counts": counts,
                "selected": asked_sub_label,
            }
        else:
            where, args = self._where(
                provider=provider,
                model=model,
                status=SUCCESS_STATUS,
                endpoint=endpoint,
                key=key,
                since=since,
                until=until,
                q=q,
                local=local,
                harness=harness,
                session=session,
                folder=folder,
                exit=exit,
            )
            try:
                with self._connection() as conn:
                    rows = conn.execute(
                        f"SELECT {success_sub_label_case_sql()} AS reason,"
                        f" COUNT(*) AS n FROM requests{where} GROUP BY reason",
                        args,
                    ).fetchall()
            except sqlite3.Error as exc:
                logger.warning("No-answer breakdown unavailable: {}", exc)
                return {
                    "successes": 0,
                    "total": 0,
                    "counts": counts,
                    "selected": asked_sub_label,
                }
            successes = 0
            for row in rows:
                count = int(row["n"] or 0)
                successes += count
                reason = row["reason"]
                if reason is not None and str(reason) in counts:
                    counts[str(reason)] = count
            payload = {
                "successes": successes,
                "total": sum(counts.values()),
                "counts": counts,
                "selected": asked_sub_label,
            }
        with self._stats_lock:
            self._stats_cache[cache_key] = (now, payload)
            self._stats_cache.move_to_end(cache_key)
            while len(self._stats_cache) > _STATS_CACHE_MAX_ENTRIES:
                self._stats_cache.popitem(last=False)
        return dict(payload)

    def latency_by_model(
        self, *, since: float | None = None, limit: int = _BREAKDOWN_LIMIT
    ) -> list[dict[str, Any]]:
        """Per model and outcome: how long it took to say anything, and to finish.

        Asked of ``request_attempts`` and never of the rollup, and this is the
        whole design decision worth knowing about this feature. A rollup
        bucket's dimensions are read off the ``requests`` row -- its provider,
        its resolved model, its key, its harness -- and a model that *failed*
        never reaches a ``requests`` row at all. Summing attempt latency into
        those buckets would therefore file every failed model's time under the
        model that rescued the request, which is precisely the misattribution
        this feature exists to end. The measured size of it: mean
        ``requests.ttft_ms`` is 8.6 s at ``route_attempt = 0`` and 25.2 s above
        it, so roughly 16.5 s of predecessor time is charged to fallbacks today.
        A live attempt-grouped query is the only shape that can answer honestly,
        and it is the shape ``reasoning_by_model`` already uses.

        Failed attempts come back as their own ``outcome`` group rather than
        being filtered out, because "this model is quick when it works and
        takes a minute when it does not" is the fact the question is about.
        ``skipped`` is excluded: a model that was never asked has no latency.

        ``ttft_measured`` is the attempts in each group that actually carry a
        first-token time, kept beside ``attempts`` so a caller can never report
        an average over three rows as if it described three hundred. Every row
        written before 7.4.0 is unmeasured, and is not backfillable: nothing in
        the log records when each attempt's own stream began.

        Percentiles are computed in Python over the ``ttft_ms`` column for the
        window. SQLite has no percentile function here, and the existing
        duration percentiles use a bucket histogram that this column has no
        equivalent of. The pull is capped at ``_LATENCY_SAMPLE_ROWS`` newest
        rows and the payload says which it did in ``p50_source``: ``exact``
        when every measured attempt in the window was read, ``sampled`` when
        the cap truncated it.
        """

        cache_key = ("latency_by_model", since, limit)
        now = time.monotonic()
        with self._stats_lock:
            cached = self._stats_cache.get(cache_key)
            if cached is not None:
                if now - cached[0] < _STATS_CACHE_TTL_SECONDS:
                    self._stats_cache.move_to_end(cache_key)
                    return [dict(row) for row in cached[1]["rows"]]
                del self._stats_cache[cache_key]
        try:
            with self._connection() as conn:
                # The same dual shape ``reasoning_by_model`` uses, and for the
                # same reason: the attempt's own ``ts_epoch`` is only a usable
                # filter once every attempt carries one, and until the backfill
                # marker is set ``a.ts_epoch >= ?`` would silently drop the
                # history the question is about.
                #
                # Every column is qualified. ``request_attempts`` and
                # ``requests`` now share four column names, and an unqualified
                # reference in a join is ambiguous at best and silently the
                # wrong table's at worst.
                dated = (
                    since is not None
                    and self._meta_get(conn, _ATTEMPTS_TS_BACKFILL_KEY) is not None
                )
                join = (
                    ""
                    if since is None or dated
                    else " JOIN requests r ON r.id = a.request_id"
                )
                since_clause = (
                    ""
                    if since is None
                    else (" AND a.ts_epoch >= ?" if dated else " AND r.ts_epoch >= ?")
                )
                args: list[Any] = [] if since is None else [since]
                rows = conn.execute(
                    "SELECT a.model_ref AS model_ref,"
                    f" {_LATENCY_OUTCOME_GROUP_SQL} AS outcome,"
                    " COUNT(*) AS attempts,"
                    " SUM(CASE WHEN a.ttft_ms IS NOT NULL THEN 1 ELSE 0 END)"
                    " AS ttft_measured,"
                    " AVG(a.ttft_ms) AS avg_ttft_ms,"
                    " AVG(a.first_reasoning_ms) AS avg_first_reasoning_ms,"
                    " AVG(a.duration_ms - a.ttft_ms) AS avg_generating_ms,"
                    " SUM(a.tokens_out) AS tokens_out"
                    " FROM request_attempts a"
                    f"{join}"
                    f" WHERE a.model_ref IS NOT NULL AND {_LATENCY_OUTCOMES_SQL}"
                    f"{since_clause}"
                    f" GROUP BY a.model_ref, {_LATENCY_OUTCOME_GROUP_SQL}"
                    " ORDER BY attempts DESC"
                    " LIMIT ?",
                    [*args, limit],
                ).fetchall()
                # The percentile pull is skipped entirely when the aggregate
                # above already said nothing in the window carries a
                # first-token time. That is not an optimisation for the empty
                # case, it is the *only* case that matters on an installed
                # log: every row written before 7.4.0 is unmeasured, so
                # ``ttft_ms IS NOT NULL`` matches nothing and this query
                # degrades to a reverse scan of every attempt row -- 571,665 of
                # them on the measured copy, each co-located with its stored
                # wire body. Measured there: 4.15 s with the pull, 0.68 s
                # without it, for a percentile block that could only ever have
                # come back empty.
                measured = any(int(row["ttft_measured"] or 0) for row in rows)
                samples = (
                    conn.execute(
                        "SELECT a.model_ref AS model_ref,"
                        f" {_LATENCY_OUTCOME_GROUP_SQL} AS outcome,"
                        " a.ttft_ms AS ttft_ms"
                        " FROM request_attempts a"
                        f"{join}"
                        f" WHERE a.model_ref IS NOT NULL AND {_LATENCY_OUTCOMES_SQL}"
                        " AND a.ttft_ms IS NOT NULL"
                        f"{since_clause}"
                        " ORDER BY a.rowid DESC"
                        " LIMIT ?",
                        [*args, _LATENCY_SAMPLE_ROWS + 1],
                    ).fetchall()
                    if measured
                    else []
                )
        except sqlite3.Error as exc:
            # A readout, not a control: an unavailable log means "no
            # measurement", never an error banner over the page that asked.
            logger.warning("Latency breakdown unavailable: {}", exc)
            return []
        truncated = len(samples) > _LATENCY_SAMPLE_ROWS
        buckets: dict[tuple[str, str], list[float]] = {}
        for sample in samples[:_LATENCY_SAMPLE_ROWS]:
            buckets.setdefault(
                (str(sample["model_ref"]), str(sample["outcome"])), []
            ).append(float(sample["ttft_ms"]))
        payload: list[dict[str, Any]] = []
        for row in rows:
            key = (str(row["model_ref"]), str(row["outcome"]))
            measured = sorted(buckets.get(key, ()))
            payload.append(
                {
                    "model_ref": row["model_ref"],
                    "outcome": row["outcome"],
                    "attempts": int(row["attempts"] or 0),
                    "ttft_measured": int(row["ttft_measured"] or 0),
                    "avg_ttft_ms": _rounded(row["avg_ttft_ms"]),
                    "avg_first_reasoning_ms": _rounded(row["avg_first_reasoning_ms"]),
                    # ``duration_ms - ttft_ms``: the time this model spent
                    # producing the rest of the answer once it had started. NULL
                    # whenever either input is, which SQLite does for us.
                    "avg_generating_ms": _rounded(row["avg_generating_ms"]),
                    # NULL, not 0: only the attempt that answered carries token
                    # counts, so a group of failures has nothing to sum and
                    # "0 tokens out" would be a claim nobody can support.
                    "tokens_out": (
                        None if row["tokens_out"] is None else int(row["tokens_out"])
                    ),
                    "p50_ttft_ms": _rounded(_percentile(measured, 0.50)),
                    "p95_ttft_ms": _rounded(_percentile(measured, 0.95)),
                    "p50_source": "sampled" if truncated else "exact",
                }
            )
        with self._stats_lock:
            # Shares ``stats()``'s cache and its 5 s TTL, exactly as
            # ``reasoning_by_model`` does; the string-led key cannot collide
            # with the positional tuple ``stats`` uses.
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

        MCC's own web tools (``LOCAL_WEB_TOOL_ANSWERS``) are local answers but
        not rules, and are left out of every figure here.
        """
        days = max(1, days)
        where, args = self._where(since=since, until=until)
        connector = " AND" if where else " WHERE"
        with self._connection() as conn:
            totals = conn.execute(
                f"SELECT COUNT(*),"
                f" SUM(CASE WHEN {_OPTIMIZATION_RULE_SQL} THEN 1 ELSE 0 END),"
                # A web-tool row never carries a saving (NULL), so this sum
                # needs no exclusion of its own.
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
                f" FROM requests{where}{connector} {_OPTIMIZATION_RULE_SQL}"
                " GROUP BY rule ORDER BY requests DESC",
                args,
            ).fetchall()
            series_rows = conn.execute(
                "SELECT optimization AS rule,"
                " strftime('%Y-%m-%d', ts_epoch, 'unixepoch') AS bucket,"
                " COUNT(*) AS requests,"
                " COALESCE(SUM(optimization_tokens_saved), 0) AS tokens_saved"
                f" FROM requests{where}{connector} {_OPTIMIZATION_RULE_SQL}"
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
        session: str | None = None,
        folder: str | None = None,
        exit: str | None = None,
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
            session=session,
            folder=folder,
            exit=exit,
        )
        with self._connection() as conn:
            total, last_ts = conn.execute(
                f"SELECT COUNT(*), MAX(ts_epoch) FROM requests{where}", args
            ).fetchone()
        return {"total": total or 0, "last_ts": last_ts}

    def origin_breakdown(
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
        session: str | None = None,
        folder: str | None = None,
        exit: str | None = None,
    ) -> dict[str, Any]:
        """Requests by folder and by session, over the same filters as ``stats``.

        Its own call, beside the cost, latency and TTFT panels, and never a
        part of ``stats()``: a session id is unbounded, so neither is a rollup
        dimension, and folding a row scan into the rollup-served payload would
        slow the page for every reader who never looks at these two tables.

        Each breakdown counts only the rows that carry its value. A row with
        no folder is "not stated", not a folder called ``(unknown)``, and
        counting those would mean reading every row the log has ever written
        -- which is what ``idx_requests_origin_v1`` exists to avoid. The
        restriction is the same ``rowid IN`` shape ``_where`` uses, and for the
        same measured reason.

        **Sessions are listed flat.** Grouping subagents under a parent would
        rest on a subagent stating its *parent's* session id rather than one
        of its own, and that had not been seen on a real log when this
        shipped: no row of the operator's log carried the columns yet. So a
        session row counts the requests a subagent sent under it
        (``subagent_requests``) and how many distinct subagents that was
        (``subagents``) -- facts about the rows -- and claims nothing about
        which conversation started which.
        """

        cache_key = (
            "origin_breakdown",
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
            session,
            folder,
            exit,
        )
        now = time.monotonic()
        with self._stats_lock:
            cached = self._stats_cache.get(cache_key)
            if cached is not None:
                if now - cached[0] < _STATS_CACHE_TTL_SECONDS:
                    self._stats_cache.move_to_end(cache_key)
                    return copy.deepcopy(cached[1])
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
            session=session,
            folder=folder,
            exit=exit,
        )
        window_sql = ""
        window_args: list[Any] = []
        if since is not None:
            window_sql += " AND o.ts_epoch >= ?"
            window_args.append(since)
        if until is not None:
            window_sql += " AND o.ts_epoch <= ?"
            window_args.append(until)

        def carrying(column: str) -> tuple[str, list[Any]]:
            clause = (
                "rowid IN (SELECT o.rowid FROM requests AS o"
                f" WHERE o.{column} IS NOT NULL{window_sql})"
            )
            return (
                f"{where}{' AND' if where else ' WHERE'} {clause}",
                [*args, *window_args],
            )

        with self._connection() as conn:
            folder_where, folder_args = carrying("project_dir")
            by_folder, by_folder_truncated = self._breakdown(
                conn,
                "project_dir",
                folder_where,
                folder_args,
                key_sql="project_dir",
                extra=(
                    ("sessions", "COUNT(DISTINCT session_id)"),
                    ("last_ts", "MAX(ts_epoch)"),
                ),
            )
            session_where, session_args = carrying("session_id")
            by_session, by_session_truncated = self._breakdown(
                conn,
                "session_id",
                session_where,
                session_args,
                key_sql="session_id",
                extra=(
                    (
                        "subagent_requests",
                        "SUM(CASE WHEN agent_id IS NOT NULL THEN 1 ELSE 0 END)",
                    ),
                    ("subagents", "COUNT(DISTINCT agent_id)"),
                    ("folders", "COUNT(DISTINCT project_dir)"),
                    ("folder", "MAX(project_dir)"),
                    ("last_ts", "MAX(ts_epoch)"),
                ),
            )
        for row in by_folder:
            row["short"] = project_short(row["key"])
        for row in by_session:
            row["short"] = session_short(row["key"])
            row["folder_short"] = project_short(row["folder"])
        payload: dict[str, Any] = {
            "by_folder": by_folder,
            "by_folder_truncated": by_folder_truncated,
            "by_session": by_session,
            "by_session_truncated": by_session_truncated,
        }
        with self._stats_lock:
            self._stats_cache[cache_key] = (now, copy.deepcopy(payload))
            self._stats_cache.move_to_end(cache_key)
            while len(self._stats_cache) > _STATS_CACHE_MAX_ENTRIES:
                self._stats_cache.popitem(last=False)
        return payload

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
        extra: tuple[tuple[str, str], ...] = (),
    ) -> tuple[list[dict[str, Any]], bool]:
        """Return (rows, truncated) for a GROUP BY breakdown, capped at ``_BREAKDOWN_LIMIT``.

        Fetches one row past the cap to detect truncation without a second
        COUNT(DISTINCT ...) query, then trims it back off before returning.

        ``key_sql`` overrides the grouping expression for a column whose NULLs
        are not all the same fact -- provider being the case that needs it.

        ``extra`` is ``(alias, aggregate)`` pairs appended after the standard
        measures and returned under their alias. Empty for every breakdown
        that existed before the origin ones, so their SQL is unchanged.
        """
        key_expression = key_sql or f"COALESCE({column}, '{UNKNOWN_PROVIDER_KEY}')"
        extra_sql = "".join(f", {aggregate} AS {alias}" for alias, aggregate in extra)
        cursor = conn.execute(
            f"SELECT {key_expression} AS key, COUNT(*) AS requests,"
            " COALESCE(SUM(tokens_in),0) AS tokens_in,"
            " COALESCE(SUM(tokens_out),0) AS tokens_out,"
            " COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,"
            " COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens,"
            " SUM(CASE WHEN cache_read_tokens IS NOT NULL THEN 1 ELSE 0 END)"
            " AS cache_reported,"
            " SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errors,"
            f" AVG(duration_ms) AS avg_duration_ms{extra_sql}"
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
                **{alias: row[alias] for alias, _aggregate in extra},
            }
            for row in rows
        ], truncated

    @staticmethod
    def _percentiles(
        conn: sqlite3.Connection,
        where: str,
        args: list[Any],
        fractions: tuple[float, ...],
        column: str = "duration_ms",
    ) -> dict[float, float | None]:
        """Compute percentiles from one ordered pass over ``column``.

        ``column`` is a literal from this module and never user input: the two
        callers pass ``"duration_ms"`` and ``"ttft_ms"``. It is interpolated
        into the SQL because a column name cannot be a bound parameter, and
        :data:`_PERCENTILE_COLUMNS` is the allow-list that keeps that true.

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
        if column not in _PERCENTILE_COLUMNS:
            raise ValueError(f"Not a percentile column: {column!r}")
        connector = " AND" if where else " WHERE"
        values = [
            row[0]
            for row in conn.execute(
                f"SELECT {column} FROM requests{where}{connector}"
                f" {column} IS NOT NULL ORDER BY {column}",
                args,
            ).fetchall()
        ]
        return _interpolated_percentiles(values, fractions)

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

        What the deleted requests leave behind goes in the same transaction;
        see ``_sweep_orphans``. Until 7.72.2 that meant seven whole-table
        scans on every pass -- about a minute on a 12 GB log, every hundred
        requests, even when the pass deleted nothing. A pass now follows only
        the requests it deleted, and sweeps a whole table only when something
        other than this pass may have left an orphan in it (``_owe_full_sweeps``).
        """
        if self._max_rows <= 0:
            return 0
        with self._sweep_lock:
            owed = set(self._full_sweeps_owed)
            self._full_sweeps_owed.clear()
        committed = False
        conn = self._connect()
        try:
            with conn:
                removed_ids = [
                    str(row[0])
                    for row in conn.execute(
                        "DELETE FROM requests WHERE id IN ("
                        " SELECT id FROM requests ORDER BY ts_epoch DESC"
                        " LIMIT -1 OFFSET ?"
                        ") RETURNING id",
                        (self._max_rows,),
                    ).fetchall()
                ]
                removed = len(removed_ids)
                self._sweep_orphans(conn, removed_ids, owed)
                # A video job goes with its request row, after the grace: the
                # job is written the moment it is accepted, its row only when
                # the writer next flushes.
                conn.execute(
                    "DELETE FROM media_jobs WHERE created_at < ? AND NOT EXISTS ("
                    " SELECT 1 FROM requests WHERE requests.id ="
                    " media_jobs.request_id)",
                    (time.time() - _MEDIA_JOB_ORPHAN_GRACE_SECONDS,),
                )
                now = time.monotonic()
                if removed and (
                    self._last_tool_sweep is None
                    or now - self._last_tool_sweep >= _TOOL_SWEEP_INTERVAL_SECONDS
                ):
                    self._last_tool_sweep = now
                    self._sweep_tool_catalogues(conn)
                    self._sweep_request_values(conn)
                    self._sweep_skip_sets(conn)
            committed = True
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
            if not committed:
                # Rolled back: every sweep this pass owed is still owed.
                self._owe_full_sweeps(*owed)
            conn.close()

    def _owe_full_sweeps(self, *tables: str) -> None:
        """Make the next prune pass sweep these tables whole.

        For anything that may leave an orphan other than a pass deleting
        requests: a request written again (its new links replace the old),
        a picture described before its request is logged, a video's file
        linked to a request that may be gone. Called after that write commits,
        so the pass that pays the debt can see what it has to remove; a write
        that lands while a pass is starting is swept by the next one.
        """
        with self._sweep_lock:
            self._full_sweeps_owed.update(tables)

    def _sweep_orphans(
        self, conn: sqlite3.Connection, removed_ids: list[str], owed: set[str]
    ) -> None:
        """Remove what the requests a prune pass deleted leave behind.

        Links (bodies, pictures, attempts, media) are keyed by request id with
        no cascade, so they would otherwise outlive their request and keep the
        file growing forever. Blobs are shared -- one prompt, picture or file
        can serve many requests -- so a blob goes only once no surviving link
        names it.

        A table in ``owed`` is swept whole, by the statements every pass ran
        before 7.72.2. Any other table follows only ``removed_ids``: their links
        are deleted by request id, and each blob those links named goes only if
        no link left names it. The two find the same rows. A table is owed
        whenever anything but a pass may have left an orphan in it, so a table
        that is not owed held no orphan before this pass; then its orphans are
        exactly the links of the removed requests, and the blobs that only those
        links named.
        """
        owed = set(owed)
        targeted = 0 < len(removed_ids) <= _TARGETED_SWEEP_MAX_ROWS
        if len(removed_ids) > _TARGETED_SWEEP_MAX_ROWS:
            owed.update(_ORPHAN_SWEEP_TABLES)
        # A link table swept whole may drop links whose blobs were never
        # collected, so its blob table is swept whole with it.
        for link, blob in _LINK_BLOB_TABLES:
            if link in owed:
                owed.add(blob)
        id_chunks = (
            [
                removed_ids[start : start + _SWEEP_CHUNK]
                for start in range(0, len(removed_ids), _SWEEP_CHUNK)
            ]
            if targeted
            else []
        )

        def follow(table: str, returning: str = "") -> set[str]:
            named: set[str] = set()
            for chunk in id_chunks:
                rows = conn.execute(
                    f"DELETE FROM {table} WHERE request_id IN"
                    f" ({', '.join('?' * len(chunk))}){returning}",
                    chunk,
                ).fetchall()
                named.update(
                    str(value) for row in rows for value in row if value is not None
                )
            return named

        def blob_chunks(shas: set[str]) -> list[list[str]]:
            ordered = sorted(shas)
            return [
                ordered[start : start + _SWEEP_CHUNK]
                for start in range(0, len(ordered), _SWEEP_CHUNK)
            ]

        body_shas: set[str] = set()
        if "request_bodies" in owed:
            conn.execute(
                "DELETE FROM request_bodies WHERE NOT EXISTS ("
                " SELECT 1 FROM requests WHERE requests.id ="
                " request_bodies.request_id)"
            )
        else:
            body_shas = follow("request_bodies", " RETURNING sha, input_sha")
        if "body_blobs" in owed:
            conn.execute(
                "DELETE FROM body_blobs WHERE NOT EXISTS ("
                " SELECT 1 FROM request_bodies WHERE request_bodies.sha ="
                " body_blobs.sha OR request_bodies.input_sha = body_blobs.sha)"
            )
        else:
            # Two NOT EXISTS, one per indexed column: the same test as the
            # whole sweep's OR, each answered by an index lookup.
            for chunk in blob_chunks(body_shas):
                conn.execute(
                    "DELETE FROM body_blobs WHERE sha IN"
                    f" ({', '.join('?' * len(chunk))})"
                    " AND NOT EXISTS (SELECT 1 FROM request_bodies"
                    " WHERE request_bodies.sha = body_blobs.sha)"
                    " AND NOT EXISTS (SELECT 1 FROM request_bodies"
                    " WHERE request_bodies.input_sha = body_blobs.sha)",
                    chunk,
                )
        # Images follow the same rule as bodies: the link goes when its
        # request does, and the picture itself only once no surviving
        # request still points at it.
        image_shas: set[str] = set()
        if "request_images" in owed:
            conn.execute(
                "DELETE FROM request_images WHERE NOT EXISTS ("
                " SELECT 1 FROM requests WHERE requests.id ="
                " request_images.request_id)"
            )
        else:
            image_shas = follow("request_images", " RETURNING sha")
        if "request_attempts" in owed:
            conn.execute(
                "DELETE FROM request_attempts WHERE NOT EXISTS ("
                " SELECT 1 FROM requests WHERE requests.id ="
                " request_attempts.request_id)"
            )
        else:
            follow("request_attempts")
        if "request_attempt_skips" in owed:
            conn.execute(
                "DELETE FROM request_attempt_skips WHERE NOT EXISTS ("
                " SELECT 1 FROM requests WHERE requests.id ="
                " request_attempt_skips.request_id)"
            )
        else:
            follow("request_attempt_skips")
        # ``request_images`` has no index on ``sha``, so the NOT EXISTS every
        # pass ran before 7.72.2 scanned every link once per picture (24 s for
        # 1,020 pictures x 86,763 links on a real log). ``NOT IN`` over the
        # non-NULL links reads them once; with ``sha IS NULL`` it deletes
        # exactly the rows that NOT EXISTS did.
        still_named = (
            "sha NOT IN (SELECT sha FROM request_images WHERE sha IS NOT NULL)"
        )
        if "image_blobs" in owed:
            conn.execute(f"DELETE FROM image_blobs WHERE sha IS NULL OR {still_named}")
        else:
            for chunk in blob_chunks(image_shas):
                conn.execute(
                    "DELETE FROM image_blobs WHERE sha IN"
                    f" ({', '.join('?' * len(chunk))}) AND {still_named}",
                    chunk,
                )
        # Generated media follow the same rule, and a stored file goes with
        # its last row. The files are deleted here, on the writer thread,
        # never on the event loop.
        media_shas: set[str] = set()
        if "request_media" in owed:
            conn.execute(
                "DELETE FROM request_media WHERE NOT EXISTS ("
                " SELECT 1 FROM requests WHERE requests.id ="
                " request_media.request_id)"
            )
        else:
            media_shas = follow("request_media", " RETURNING sha256")
        orphaned_media: list[str] = []
        if "media_blobs" in owed:
            orphaned_media = [
                str(row[0])
                for row in conn.execute(
                    "SELECT sha256 FROM media_blobs WHERE stored = 1"
                    " AND NOT EXISTS (SELECT 1 FROM request_media"
                    " WHERE request_media.sha256 = media_blobs.sha256)"
                )
            ]
            conn.execute(
                "DELETE FROM media_blobs WHERE NOT EXISTS ("
                " SELECT 1 FROM request_media WHERE request_media.sha256 ="
                " media_blobs.sha256)"
            )
        else:
            for chunk in blob_chunks(media_shas):
                unnamed = (
                    f"sha256 IN ({', '.join('?' * len(chunk))})"
                    " AND NOT EXISTS (SELECT 1 FROM request_media"
                    " WHERE request_media.sha256 = media_blobs.sha256)"
                )
                orphaned_media.extend(
                    str(row[0])
                    for row in conn.execute(
                        f"SELECT sha256 FROM media_blobs WHERE stored = 1 AND {unnamed}",
                        chunk,
                    )
                )
                conn.execute(f"DELETE FROM media_blobs WHERE {unnamed}", chunk)
        if orphaned_media:
            delete_media_files(media_root(self._db_path), orphaned_media)

    @staticmethod
    def _sweep_tool_catalogues(conn: sqlite3.Connection) -> None:
        """Drop catalogues no retained request carries, then orphaned definitions.

        Catalogues first, in one pass over ``requests`` (the ``NOT IN``
        subquery is materialised once, not re-run per catalogue). Then the
        definitions, against the members of every surviving catalogue --
        collected in Python, because matching each definition against every
        ``member_shas`` blob in SQL is a cross product.
        """

        conn.execute(
            "DELETE FROM tool_catalogues WHERE sha NOT IN ("
            " SELECT tool_catalogue_sha FROM requests"
            " WHERE tool_catalogue_sha IS NOT NULL)"
        )
        live: set[bytes] = set()
        for (member_shas,) in conn.execute("SELECT member_shas FROM tool_catalogues"):
            live.update(split_member_shas(bytes(member_shas)))
        dead = [
            (row[0],)
            for row in conn.execute("SELECT sha FROM tool_schemas")
            if bytes(row[0]) not in live
        ]
        if dead:
            conn.executemany("DELETE FROM tool_schemas WHERE sha = ?", dead)

    @staticmethod
    def _sweep_request_values(conn: sqlite3.Connection) -> None:
        """Drop stored-once values no retained request names any more.

        Mark and sweep, never a count: one pass over the three ref columns of
        every row collects what is still named, and only the rest goes. A
        mistake here can only leave a value behind, never delete one a row
        still names. Same cadence as the tool-catalogue sweep beside it.
        """
        named: set[int] = set()
        for row in conn.execute(
            f"SELECT {', '.join(_STORED_ONCE_REFS)} FROM requests"
            f" WHERE {' OR '.join(f'{ref} IS NOT NULL' for ref in _STORED_ONCE_REFS)}"
        ):
            named.update(int(value) for value in row if value is not None)
        dead = [
            (int(row[0]),)
            for row in conn.execute("SELECT id FROM request_values")
            if int(row[0]) not in named
        ]
        if dead:
            conn.executemany("DELETE FROM request_values WHERE id = ?", dead)

    @staticmethod
    def _sweep_skip_sets(conn: sqlite3.Connection) -> None:
        """Drop skip sets no retained request names any more.

        Mark and sweep, as ``_sweep_request_values``: one pass over the set ids
        ``request_attempt_skips`` names, then only the rest goes. A mistake can
        only leave a set behind, never delete one a request still names.
        """
        named = {
            int(row[0])
            for row in conn.execute("SELECT DISTINCT set_id FROM request_attempt_skips")
        }
        dead = [
            (int(row[0]),)
            for row in conn.execute("SELECT id FROM attempt_skip_sets")
            if int(row[0]) not in named
        ]
        if dead:
            conn.executemany("DELETE FROM attempt_skip_sets WHERE id = ?", dead)

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
            stored_media = [
                str(row[0])
                for row in conn.execute(
                    "SELECT sha256 FROM media_blobs WHERE stored = 1"
                )
            ]
            conn.execute("DELETE FROM request_media")
            conn.execute("DELETE FROM media_blobs")
            conn.execute("DELETE FROM media_jobs")
            conn.execute("DELETE FROM request_attempts")
            conn.execute("DELETE FROM request_attempt_skips")
            conn.execute("DELETE FROM attempt_skip_sets")
            conn.execute("DELETE FROM tool_catalogues")
            conn.execute("DELETE FROM tool_schemas")
            conn.execute("DELETE FROM request_values")
            removed = cursor.rowcount
        if stored_media:
            delete_media_files(media_root(self._db_path), stored_media)
        return removed

    # ------------------------------------------------------------ media store ---
    # MEDIA_STORE_MAX_MB (7.68.0). The cap is always handed in by the caller:
    # the writer thread with a record's own number, a video download through
    # ``asyncio.to_thread`` with its request's. Only files go; every row and
    # link stays, with ``stored`` set to 0.

    def trim_media_store(self, max_bytes: int) -> int:
        """Delete the oldest stored media files until the rest fit ``max_bytes``.

        Its own short connection; call it off the event loop. Returns how
        many files went. A cap of 0 or less never scans.
        """
        if max_bytes <= 0:
            return 0
        conn = self._connect()
        try:
            return self._trim_media(conn, max_bytes)
        finally:
            conn.close()

    def _trim_media(self, conn: sqlite3.Connection, max_bytes: int) -> int:
        """Oldest first (``created_at``, then address) until the total fits.

        A file that cannot be deleted (held open elsewhere) is still on disk,
        so it stays ``stored`` and counted, and the next oldest goes instead.
        """
        if max_bytes <= 0:
            return 0
        released: list[tuple[str]] = []
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(bytes), 0) FROM media_blobs WHERE stored = 1"
            ).fetchone()
            total = int(row[0] or 0)
            if total <= max_bytes:
                return 0
            root = media_root(self._db_path)
            oldest = conn.execute(
                "SELECT sha256, bytes FROM media_blobs WHERE stored = 1"
                " ORDER BY created_at, sha256"
            )
            try:
                for sha256, size in oldest:
                    if total <= max_bytes:
                        break
                    if remove_media_file(root, str(sha256)):
                        released.append((str(sha256),))
                        total -= int(size or 0)
            finally:
                oldest.close()
            if released:
                with conn:
                    conn.executemany(
                        "UPDATE media_blobs SET stored = 0 WHERE sha256 = ?",
                        released,
                    )
        except sqlite3.Error as exc:
            logger.warning("MEDIA STORE: trimming to the cap failed: {}", exc)
            return 0
        if released:
            logger.info(
                "MEDIA STORE: deleted the {} oldest stored media files to stay"
                " under {} MB",
                len(released),
                max_bytes // (1024 * 1024),
            )
        return len(released)

    def stored_media_file(self, sha256: str) -> tuple[Path, str | None] | None:
        """Where the media store keeps one address, and its type.

        ``None`` unless the address is recorded as stored and its file is on
        disk. Call it off the event loop.
        """
        with self._connection() as conn:
            row = conn.execute(
                "SELECT mime, stored FROM media_blobs WHERE sha256 = ?", (sha256,)
            ).fetchone()
        if row is None or not row["stored"]:
            return None
        mime = row["mime"]
        path = media_file_path(media_root(self._db_path), sha256, mime)
        return (path, mime) if path.is_file() else None

    # ------------------------------------------------------------ media jobs ---
    # Video jobs (7.64.0). Each call opens its own short connection and is
    # made only through ``asyncio.to_thread``: a job is read and written by the
    # request serving a client's poll, not by the batched writer, so a job
    # accepted a moment ago is readable at once.

    def insert_media_job(self, job: MediaJobRecord) -> None:
        """Record a job the moment a host accepted it."""
        row = tuple(getattr(job, column) for column in _MEDIA_JOB_INSERT_COLUMNS)
        with self._connection() as conn:
            conn.execute(_MEDIA_JOB_INSERT_SQL, row)

    def media_job(self, job_id: str) -> dict[str, Any] | None:
        """One job's row, or ``None`` when MCC has no such job."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM media_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return None if row is None else dict(row)

    def update_media_job(self, job_id: str, **fields: Any) -> bool:
        """Change what a poll or a download learned; ``True`` if the job exists."""
        unknown = set(fields) - _MEDIA_JOB_UPDATABLE
        if unknown:
            raise ValueError(f"not updatable on a media job: {sorted(unknown)}")
        if not fields:
            return False
        names = sorted(fields)
        assignments = ", ".join(f"{name} = ?" for name in names)
        with self._connection() as conn:
            cursor = conn.execute(
                f"UPDATE media_jobs SET {assignments} WHERE job_id = ?",
                (*(fields[name] for name in names), job_id),
            )
        return cursor.rowcount > 0

    def list_media_jobs(
        self,
        *,
        after: str | None = None,
        limit: int | None = None,
        order: Literal["asc", "desc"] = "desc",
    ) -> list[dict[str, Any]]:
        """MCC's own jobs, newest first by default; ``after`` is a job id cursor."""
        direction = "ASC" if order == "asc" else "DESC"
        comparison = ">" if order == "asc" else "<"
        sql = "SELECT * FROM media_jobs"
        params: list[Any] = []
        if after is not None:
            sql += (
                f" WHERE (created_at, job_id) {comparison}"
                " (SELECT created_at, job_id FROM media_jobs WHERE job_id = ?)"
            )
            params.append(after)
        sql += f" ORDER BY created_at {direction}, job_id {direction}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        with self._connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def delete_media_job(self, job_id: str) -> bool:
        """Forget one job; ``True`` if it existed."""
        with self._connection() as conn:
            cursor = conn.execute("DELETE FROM media_jobs WHERE job_id = ?", (job_id,))
        return cursor.rowcount > 0

    def set_request_video_seconds(
        self,
        request_id: str,
        seconds: float,
        *,
        cost: tuple[float, str] | None = None,
    ) -> bool:
        """Put a finished video's length on its create row; ``True`` once written.

        ``False`` while the writer has not flushed that row yet -- the caller
        tries again on the next poll.

        ``cost`` (7.69.0) is ``(cost_usd, cost_source)`` for the video now that
        its length is known. It fills only a row with no amount yet -- one the
        create stored as ``unpriced`` or not at all -- so a figure the host
        reported at create time is never replaced by an estimate.
        """
        with self._connection() as conn:
            if cost is None:
                cursor = conn.execute(
                    "UPDATE requests SET output_video_seconds = ? WHERE id = ?",
                    (seconds, request_id),
                )
            else:
                # Every SET expression reads the row as it was, so both CASEs
                # test the old ``cost_usd``.
                cursor = conn.execute(
                    "UPDATE requests SET output_video_seconds = ?,"
                    " cost_usd = CASE WHEN cost_usd IS NULL THEN ? ELSE cost_usd END,"
                    " cost_source = CASE WHEN cost_usd IS NULL THEN ?"
                    " ELSE cost_source END"
                    " WHERE id = ?",
                    (seconds, cost[0], cost[1], request_id),
                )
        return cursor.rowcount > 0

    def record_media_job_content(
        self, job_id: str, request_id: str, output: MediaOutputRecord, *, at: float
    ) -> None:
        """What a download of the job's video measured, linked to its create row."""
        with self._connection() as conn:
            conn.execute(
                "UPDATE media_jobs SET content_sha = ?, content_bytes = ?,"
                " content_mime = ? WHERE job_id = ?",
                (output.sha256, output.bytes, output.mime, job_id),
            )
            conn.execute(
                _MEDIA_BLOB_UPSERT_SQL,
                (
                    output.sha256,
                    output.mime,
                    output.bytes,
                    at,
                    1 if output.stored else 0,
                ),
            )
            conn.execute(
                "INSERT OR REPLACE INTO request_media"
                " (request_id, direction, idx, sha256) VALUES (?, ?, ?, ?)",
                (request_id, output.direction, output.idx, output.sha256),
            )
        # Linked to a request that may be gone already, or never be written,
        # and possibly replacing a link to another file.
        self._owe_full_sweeps("request_media", "media_blobs")

    # ---------------------------------------------------------- media stats ---

    def media_stats(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        groups: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """Media requests over a window, per group and per provider/model (7.67.0).

        Reads only the rows a media endpoint wrote (``media_operation IS NOT
        NULL``). ``stats()``, its rollups and the permanent totals are not
        touched by this and go on counting every row, media included. The
        window is the inclusive ``ts_epoch`` pair ``_where`` applies, first in
        the predicate so a windowed read is a range seek on ``idx_requests_ts``.

        ``groups`` maps an operation to the group -- the rail -- it is counted
        under. ``core`` does not own that vocabulary, so the caller hands it in;
        an operation it does not name is a group of its own, never dropped.

        A SUM over a column no row of a group measured is ``None`` (SQLite's
        SUM over only NULLs is NULL), so "not measured" never reads as 0;
        ``<name>_measured`` counts the rows that did measure it. The average
        duration is SUM/COUNT of the non-NULL ``duration_ms`` and the median is
        the interpolated p50 the stats row path computes.

        Video job states come from ``media_jobs`` for jobs *created* in the
        window, by the status a poll last recorded (``unknown`` when none).
        """

        mapping = dict(groups or {})
        if mapping:
            group_sql = (
                "CASE media_operation"
                + " WHEN ? THEN ?" * len(mapping)
                + " ELSE media_operation END"
            )
            group_args: list[Any] = [part for pair in mapping.items() for part in pair]
        else:
            group_sql = "media_operation"
            group_args = []
        clauses: list[str] = []
        window_args: list[Any] = []
        job_clauses: list[str] = []
        if since is not None:
            clauses.append("ts_epoch >= ?")
            job_clauses.append("created_at >= ?")
            window_args.append(since)
        if until is not None:
            clauses.append("ts_epoch <= ?")
            job_clauses.append("created_at <= ?")
            window_args.append(until)
        clauses.append("media_operation IS NOT NULL")
        where = f" WHERE {' AND '.join(clauses)}"
        job_where = f" WHERE {' AND '.join(job_clauses)}" if job_clauses else ""
        measures_sql = "".join(
            f", SUM({column}) AS {name}, COUNT({column}) AS {name}_measured"
            for name, column in _MEDIA_STAT_MEASURES
        )
        with self._connection() as conn:
            rows = [
                dict(row)
                for row in conn.execute(
                    f"SELECT {group_sql} AS grp, provider,"
                    " resolved_model AS model, COUNT(*) AS requests,"
                    " SUM(CASE WHEN status='success' THEN 1 ELSE 0 END)"
                    " AS succeeded,"
                    " SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS failed,"
                    " SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END)"
                    " AS cancelled,"
                    " COUNT(media_job_id) AS video_jobs,"
                    " SUM(duration_ms) AS duration_sum,"
                    f" COUNT(duration_ms) AS duration_count{measures_sql}"
                    f" FROM requests{where}"
                    " GROUP BY grp, provider, resolved_model"
                    " ORDER BY requests DESC, provider, resolved_model",
                    [*group_args, *window_args],
                ).fetchall()
            ]
            durations: dict[tuple[Any, ...], list[float]] = {}
            for grp, provider, model, duration in conn.execute(
                f"SELECT {group_sql} AS grp, provider, resolved_model, duration_ms"
                f" FROM requests{where} AND duration_ms IS NOT NULL"
                " ORDER BY duration_ms",
                [*group_args, *window_args],
            ).fetchall():
                # One ordered fetch: each list is filled in ascending order,
                # so every one of them is already sorted.
                durations.setdefault((grp, provider, model), []).append(duration)
                durations.setdefault((grp,), []).append(duration)
            job_rows = conn.execute(
                "SELECT provider, model, COALESCE(NULLIF(status, ''), 'unknown')"
                f" AS state, COUNT(*) AS jobs FROM media_jobs{job_where}"
                " GROUP BY provider, model, state ORDER BY provider, model, state",
                window_args,
            ).fetchall()

        def finished(entry: dict[str, Any], key: tuple[Any, ...]) -> dict[str, Any]:
            median = _interpolated_percentiles(durations.get(key, []), (0.5,))[0.5]
            total = entry.pop("duration_sum")
            entry["avg_duration_ms"] = _rounded(_mean(total, entry["duration_count"]))
            entry["median_duration_ms"] = _rounded(median)
            # Requests nothing priced: a count, so a partly priced sum reads
            # as partial rather than as a cheap window.
            entry["cost_unpriced"] = (entry["requests"] or 0) - (
                entry["cost_usd_measured"] or 0
            )
            return entry

        folded: dict[Any, dict[str, Any]] = {}
        models: list[dict[str, Any]] = []
        for row in rows:
            group = row["grp"]
            total = folded.get(group)
            if total is None:
                total: dict[str, Any] = {"group": group, "duration_sum": None}
                total.update(dict.fromkeys(_MEDIA_STAT_COUNTERS, 0))
                for name, _column in _MEDIA_STAT_MEASURES:
                    total[name] = None
                    total[f"{name}_measured"] = 0
                folded[group] = total
            for name in _MEDIA_STAT_COUNTERS:
                total[name] += row[name] or 0
            for name in ("duration_sum", *(name for name, _ in _MEDIA_STAT_MEASURES)):
                if row[name] is not None:
                    total[name] = (total[name] or 0) + row[name]
            for name, _column in _MEDIA_STAT_MEASURES:
                total[f"{name}_measured"] += row[f"{name}_measured"] or 0
            entry = {"group": group, **{k: v for k, v in row.items() if k != "grp"}}
            models.append(finished(entry, (group, row["provider"], row["model"])))

        jobs: dict[tuple[Any, Any], dict[str, Any]] = {}
        job_states: dict[str, int] = {}
        for job in job_rows:
            key = (job["provider"], job["model"])
            bucket = jobs.setdefault(
                key,
                {"provider": key[0], "model": key[1], "states": {}, "total": 0},
            )
            bucket["states"][job["state"]] = job["jobs"]
            bucket["total"] += job["jobs"]
            job_states[job["state"]] = job_states.get(job["state"], 0) + job["jobs"]
        return {
            "total": sum(row["requests"] for row in rows),
            "groups": [finished(total, (group,)) for group, total in folded.items()],
            "models": models,
            "jobs": list(jobs.values()),
            "job_states": job_states,
        }

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
            # A picture row no request may ever point at: owed to the next
            # whole sweep of ``image_blobs``, exactly as before 7.72.2.
            self._owe_full_sweeps("image_blobs")
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
            # 7.74.0: how far the background history conversion is. Read-only.
            "history": self.history_conversion_status(),
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
            if row[0] in _DATA_MARK_IGNORED_KEYS:
                continue
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


def _interpolated_percentiles(
    values: Sequence[float], fractions: tuple[float, ...]
) -> dict[float, float | None]:
    """Linear-interpolated percentiles of an already-sorted list.

    The arithmetic ``RequestLogStore._percentiles`` has always applied to its
    one ordered fetch, lifted out unchanged so the media block's median is the
    same number the stats row path would compute. ``None`` per fraction when
    the list is empty.
    """
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


def _percentile(ordered: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile of an already-sorted list; None when it is empty.

    Nearest rank rather than interpolation on purpose: every value here is a
    measurement that actually happened, and a p95 halfway between two real
    attempts is a latency no request ever had.
    """
    if not ordered:
        return None
    rank = math.ceil(fraction * len(ordered))
    return ordered[min(max(rank, 1), len(ordered)) - 1]


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


def touch_server_sessions() -> None:
    """Write every open store's session row now (address and ``listening``)."""
    with _store_lock:
        stores = list(_stores.values())
    for store in stores:
        store.touch_session_soon()


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


def _load_request_values(conn: sqlite3.Connection, ids: set[int]) -> dict[int, str]:
    """``request_values`` texts by id; an id with no row is simply absent."""
    found: dict[int, str] = {}
    ordered = sorted(ids)
    for start in range(0, len(ordered), _SHA_LOOKUP_CHUNK):
        chunk = ordered[start : start + _SHA_LOOKUP_CHUNK]
        for value_id, value in conn.execute(
            "SELECT id, value FROM request_values"
            f" WHERE id IN ({', '.join('?' * len(chunk))})",
            chunk,
        ):
            if isinstance(value, str):
                found[int(value_id)] = value
    return found


def _load_skip_sets(conn: sqlite3.Connection, ids: set[int]) -> dict[int, str]:
    """``attempt_skip_sets`` texts by id; an id with no row is simply absent."""
    found: dict[int, str] = {}
    ordered = sorted(ids)
    for start in range(0, len(ordered), _SHA_LOOKUP_CHUNK):
        chunk = ordered[start : start + _SHA_LOOKUP_CHUNK]
        for set_id, text in conn.execute(
            "SELECT id, attempts FROM attempt_skip_sets"
            f" WHERE id IN ({', '.join('?' * len(chunk))})",
            chunk,
        ):
            if isinstance(text, str):
                found[int(set_id)] = text
    return found


def _is_text_or_none(value: Any) -> bool:
    return value is None or type(value) is str


def _compactable_attempt_row(row: Sequence[Any]) -> bool:
    """Whether one ``_ATTEMPT_INSERT_COLUMNS`` row fits the compact form.

    A skipped attempt whose every column outside the compact form's values is
    NULL, with values of exactly the types a row read back gives: anything
    else -- a wire snapshot, a ladder, a number where text belongs -- is
    stored as a row, exactly as before.
    """
    values = dict(zip(_ATTEMPT_INSERT_COLUMNS, row, strict=True))
    if values["outcome"] != RouteAttemptOutcome.SKIPPED.value:
        return False
    if type(values["attempt"]) is not int:
        return False
    if any(values[column] is not None for column in _SKIP_EMPTY_FIELDS):
        return False
    if not all(_is_text_or_none(values[column]) for column in _SKIP_TEXT_FIELDS):
        return False
    if values["key_index"] is not None and type(values["key_index"]) is not int:
        return False
    ts_epoch = values["ts_epoch"]
    if type(ts_epoch) is not float or not math.isfinite(ts_epoch):
        return False
    try:
        for column in _SKIP_TEXT_FIELDS:
            text = values[column]
            if text is not None:
                text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _stored_skip_values(
    request_id: str, row: Sequence[Any]
) -> tuple[dict[str, Any], int] | None:
    """One ``_SKIP_FETCH_SQL`` row as the values a reader gets, if compactable.

    ``(every _ATTEMPT_INSERT_COLUMNS value, stored bytes)``, decoded from the
    storage class and the stored bytes exactly as a reader decodes them (text
    as strict UTF-8 that encodes back to the same bytes); None when the row
    holds anything the compact form cannot give back exactly.
    """
    attempt_class, attempt, ts_class, ts_epoch, key_class, key_index = row[3:9]
    if attempt_class != "integer" or ts_class != "real" or row[-1] != 1:
        return None
    if type(attempt) is not int or type(ts_epoch) is not float:
        return None
    if not math.isfinite(ts_epoch):
        return None
    if key_class not in ("null", "integer"):
        return None
    values: dict[str, Any] = dict.fromkeys(_ATTEMPT_INSERT_COLUMNS)
    values["request_id"] = request_id
    values["attempt"] = attempt
    values["outcome"] = RouteAttemptOutcome.SKIPPED.value
    values["ts_epoch"] = ts_epoch
    values["key_index"] = None if key_class == "null" else key_index
    size = len(request_id.encode("utf-8")) + len(b"skipped") + 8 + 8 + 8
    for index, column in enumerate(_SKIP_TEXT_FIELDS):
        kind, raw = row[9 + 2 * index : 11 + 2 * index]
        if kind == "null":
            continue
        if kind != "text":
            return None
        original = bytes(raw)
        try:
            text = original.decode("utf-8")
        except UnicodeDecodeError:
            return None
        if text.encode("utf-8") != original:
            return None
        values[column] = text
        size += len(original)
    return values, size


def _skip_shared_values(row: Sequence[Any]) -> tuple[Any, ...]:
    """The values every compact attempt of one request must share."""
    values = dict(zip(_ATTEMPT_INSERT_COLUMNS, row, strict=True))
    return tuple(values[column] for column in _SKIP_SHARED_FIELDS)


def _skip_set_text(rows: Sequence[Sequence[Any]]) -> str:
    """The canonical ``attempt_skip_sets`` text of these attempt rows.

    One JSON array per attempt, ``_SKIP_SET_FIELDS`` in order, sorted by
    attempt number: the same attempts always give the same text, so a set is
    stored once however many requests repeat it.
    """
    members = sorted(
        (
            [values[column] for column in _SKIP_SET_FIELDS]
            for values in (
                dict(zip(_ATTEMPT_INSERT_COLUMNS, row, strict=True)) for row in rows
            )
        ),
        key=lambda member: member[0],
    )
    return json.dumps(members, ensure_ascii=False, separators=(",", ":"))


def _parse_skip_set(text: str) -> tuple[tuple[Any, ...], ...] | None:
    """A stored set as member tuples, or None when it is not one this code wrote."""
    try:
        members = json.loads(text)
    except ValueError:
        return None
    if not isinstance(members, list):
        return None
    parsed: list[tuple[Any, ...]] = []
    for member in members:
        if not isinstance(member, list) or len(member) != len(_SKIP_SET_FIELDS):
            return None
        attempt, *texts = member
        if type(attempt) is not int or not all(_is_text_or_none(t) for t in texts):
            return None
        parsed.append(tuple(member))
    return tuple(parsed)


def _skip_row(
    request_id: str, member: Sequence[Any], shared: Mapping[str, Any]
) -> dict[str, Any]:
    """One compact attempt as the whole row ``request_attempts`` would hold."""
    row: dict[str, Any] = dict.fromkeys(_ATTEMPT_INSERT_COLUMNS)
    row["request_id"] = request_id
    row["outcome"] = RouteAttemptOutcome.SKIPPED.value
    row.update(zip(_SKIP_SET_FIELDS, member, strict=True))
    row.update(shared)
    return row


def _same_value(left: Any, right: Any) -> bool:
    """Equal as stored values: same type, same value; a REAL to the bit."""
    if type(left) is not type(right):
        return False
    if type(left) is float:
        return struct.pack("<d", left) == struct.pack("<d", right)
    return left == right


def _same_attempt_rows(
    restored: Sequence[Mapping[str, Any]] | None,
    expected: Sequence[Mapping[str, Any]],
) -> bool:
    """Whether a compact form reads back as exactly the rows it replaces."""
    if restored is None or len(restored) != len(expected):
        return False
    ordered = sorted(expected, key=lambda values: values["attempt"])
    return all(
        set(back) == set(_ATTEMPT_INSERT_COLUMNS)
        and all(
            _same_value(back[column], values[column])
            for column in _ATTEMPT_INSERT_COLUMNS
        )
        for back, values in zip(restored, ordered, strict=True)
    )


def _merge_skip_rows(
    rows: Sequence[Mapping[str, Any]], packed: Sequence[Mapping[str, Any]] | None
) -> list[Mapping[str, Any]]:
    """One request's attempt rows with its compact ones, in attempt order.

    A stored row wins over a compact attempt of the same number. Without
    compact attempts the rows come back exactly as the query returned them.
    """
    if not packed:
        return list(rows)
    present = {row["attempt"] for row in rows}
    merged = [*rows, *(row for row in packed if row["attempt"] not in present)]
    merged.sort(key=lambda row: _sqlite_order_key(row["attempt"]))
    return merged


def _sqlite_order_key(value: Any) -> tuple[int, Any]:
    """``ORDER BY`` order across storage classes: NULL, numbers, text, blobs."""
    if value is None:
        return (0, 0)
    if isinstance(value, (int, float)):
        return (1, value)
    if isinstance(value, str):
        return (2, value.encode("utf-8"))
    return (3, bytes(value))


@contextlib.contextmanager
def _one_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """Run the enclosed reads in one read transaction.

    A request's attempts are read from two tables; a write between the two
    reads -- a conversion step, or a request written again -- must not show
    the reader half of each. Inside a transaction already, that one is used.
    """
    if conn.in_transaction:
        yield
        return
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.execute("COMMIT")


def _restore_stored_once(data: dict[str, Any], values: Mapping[int, str]) -> None:
    """Put each stored-once text back in its column and drop the refs.

    Whatever the query projected comes out exactly as a row with the text
    inline would: the ref keys never reach a caller. A ref whose value is gone
    reads as NULL rather than raising.
    """
    for column, ref in zip(_STORED_ONCE_COLUMNS, _STORED_ONCE_REFS, strict=True):
        if ref not in data:
            continue
        value_id = data.pop(ref)
        if value_id is not None and column in data:
            data[column] = values.get(int(value_id))


def _loads_or_none(raw: Any) -> Any:
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _varint(value: int) -> bytes:
    """Unsigned LEB128: 7 bits per byte, low bits first, high bit = more."""
    if value < 0:
        raise ValueError("varint of a negative number")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _history_status(state: Mapping[str, Any]) -> dict[str, Any]:
    """The dashboard's view of a ``_HISTORY_CONVERSION_KEY`` document."""
    page_size = int(state.get("page_size") or 0)
    returned = int(state.get("returned_pages") or 0)
    status: dict[str, Any] = {
        "state": "done",
        "phase": None,
        "percent": 100,
        "returned_bytes": returned * page_size,
    }
    if state.get("done_at") is not None:
        return status
    labels = {
        "wire": "snapshots",
        "bodies": "bodies",
        "metadata": "metadata",
        "skipped": "skipped",
    }
    for name in _HISTORY_PHASES:
        phase = state.get(name) or {}
        if phase.get("done_at") is None:
            end = int(phase.get("end") or 0)
            through = int(phase.get("through") or 0)
            status["state"] = "converting"
            status["phase"] = labels[name]
            status["percent"] = min(99, 100 * through // end) if end else 0
            return status
    freed = int(state.get("freed_pages") or 0)
    status["state"] = "returning_space"
    status["phase"] = "space"
    status["percent"] = min(99, 100 * returned // freed) if freed else 99
    return status


def _read_varint(data: bytes, offset: int) -> tuple[int, int]:
    """Decode a ``_varint`` at ``offset``: ``(value, offset after it)``."""
    value = 0
    shift = 0
    while True:
        if offset >= len(data) or shift > 63:
            raise ValueError("truncated varint")
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7


def _spread[T](items: Sequence[T], count: int) -> list[T]:
    """``count`` items evenly spaced over ``items``, in order; all if fewer."""
    if len(items) <= count:
        return list(items)
    return [items[index * len(items) // count] for index in range(count)]


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


def retune_request_log_store(settings: Any) -> RequestLogStore | None:
    """Hand a saved configuration to the shared store already open, if any.

    ``get_request_log_store`` builds the store once per path and ignores the
    numbers on every later call, which is what made these fields need a
    restart. This is the other half: called by the runtime after an admin
    apply, it moves the open store onto the new numbers. It opens nothing -- a
    store that does not exist yet is built from the new settings on first use
    anyway -- and it never closes one, so the writer and every queued record
    carry on.
    """

    path = default_request_log_path()
    with _store_lock:
        store = _stores.get(path)
    if store is None or store._closed.is_set():
        return None
    store.retune(
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
    return store


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
