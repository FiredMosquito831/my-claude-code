"""Export field registry and format renderers (pure, store-agnostic).

Every function in this module works on already-shaped row dicts. It knows the
shape of a request-log row and a web-search row only as data (column-name
literals), never by importing the stores, so it stays inside ``core``'s
import boundary (``core`` may import only stdlib and other ``core`` modules).
The store classes own SQLite access and call back into these helpers to decide
what to project and how to format it.
"""

import csv
import io
import json
import tempfile
from collections.abc import Iterable, Iterator
from typing import Any, Literal, cast

from my_claude_code.core.request_log import ATTEMPT_PARAMS_KEY, PROVIDER_KEY_SQL

Format = Literal["json", "csv", "xlsx", "txt"]
REQUEST_SCOPE = "requests"
WEBSEARCH_SCOPE = "websearch"
#: One row per *attempt* rather than per request. A request that fell back
#: three times is one row in the request scope and four here, and the three
#: models that did not answer -- the ones that spent the time -- have no row of
#: their own anywhere else.
ATTEMPT_SCOPE = "attempts"
Scope = Literal["requests", "websearch", "attempts"]

MEDIA_TYPES: dict[Format, str] = {
    "json": "application/json",
    "csv": "text/csv; charset=utf-8",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "txt": "text/plain; charset=utf-8",
}
_EXTENSIONS: dict[Format, str] = {
    "json": "json",
    "csv": "csv",
    "xlsx": "xlsx",
    "txt": "txt",
}
_FORMAT_SET = frozenset(_EXTENSIONS)
_SCOPE_SET = frozenset((REQUEST_SCOPE, WEBSEARCH_SCOPE, ATTEMPT_SCOPE))

# Body-bearing request fields: selecting any of these means the store iterator
# must decompress stored bodies for the export.
REQUEST_BODY_FIELDS = frozenset({"input", "output", "tool_calls", "thinking"})

# Selectable fields for the request-log scope, in display order. These are the
# user-facing ids the endpoint accepts.
REQUEST_FIELD_IDS: tuple[str, ...] = (
    "input",
    "output",
    "tool_calls",
    "thinking",
    "providers",
    "models",
    "error_rate",
    "cache_hit",
    "total_input",
    "input_cached",
    "input_uncached",
    "tokens_out",
    "turns_with_tools",
    "cost",
    "ladder",
)
REQUEST_FIELD_LABELS: dict[str, str] = {
    "input": "Input",
    "output": "Output",
    "tool_calls": "Tool calls",
    "thinking": "Thinking",
    "providers": "Provider",
    "models": "Model",
    "error_rate": "Error rate",
    "cache_hit": "Cache hit",
    "total_input": "Total input",
    "input_cached": "Input cached",
    "input_uncached": "Input uncached",
    "tokens_out": "Tokens out",
    "turns_with_tools": "Turns with tools",
    "cost": "Cost",
    "ladder": "Upstream retry ladder",
}

# Selectable fields for the web-search scope.
WEBSEARCH_FIELD_IDS: tuple[str, ...] = (
    "provider",
    "key_label",
    "query",
    "results_count",
    "duration_ms",
    "status",
    "cost_usd",
    "error_kind",
    "error_message",
    "attempt_number",
    "route_id",
    "input",
    "output",
    "provider_config",
    "content_captured",
)
WEBSEARCH_FIELD_LABELS: dict[str, str] = {
    "provider": "Provider",
    "key_label": "Key",
    "query": "Query",
    "results_count": "Results",
    "duration_ms": "Duration (ms)",
    "status": "Status",
    "cost_usd": "Cost (USD)",
    "error_kind": "Error kind",
    "error_message": "Error message",
    "attempt_number": "Attempt #",
    "route_id": "Route",
    "input": "Input",
    "output": "Output",
    "provider_config": "Provider config",
    "content_captured": "Content captured",
}

# Dimension ids usable in ``group_by`` for each scope. Order in ``group_by`` is
# preserved by the caller and becomes both GROUP BY and ORDER BY order.
REQUEST_GROUP_DIMENSIONS: tuple[str, ...] = (
    "provider",
    "period",
    "model",
    "key",
    "harness",
)
WEBSEARCH_GROUP_DIMENSIONS: tuple[str, ...] = ("provider", "period", "key")

# Default field selection (safe, fast, no bodies).
DEFAULT_REQUEST_FIELDS: tuple[str, ...] = (
    "providers",
    "models",
    "error_rate",
    "cache_hit",
    "total_input",
    "input_cached",
    "input_uncached",
    "tokens_out",
    "turns_with_tools",
)
DEFAULT_WEBSEARCH_FIELDS: tuple[str, ...] = (
    "provider",
    "status",
    "results_count",
    "duration_ms",
    "cost_usd",
)

# Selectable fields for the attempt scope, in display order.
ATTEMPT_FIELD_IDS: tuple[str, ...] = (
    "failure",
    "tokens",
    "cost",
    "ladder",
    "wire",
    "recovery",
)
ATTEMPT_FIELD_LABELS: dict[str, str] = {
    "failure": "Failure and skip reason",
    "tokens": "Attempt tokens",
    "cost": "Attempt cost",
    "ladder": "Upstream retry ladder",
    "wire": "Wire surface and credential",
    "recovery": "Stream recovery counters",
}
DEFAULT_ATTEMPT_FIELDS: tuple[str, ...] = (
    "failure",
    "tokens",
    "cost",
    "ladder",
)

# The attempt scope is detail-only; see ``validate_group_by``.
ATTEMPT_GROUP_DIMENSIONS: tuple[str, ...] = ()

_SCOPE_FIELD_IDS: dict[str, tuple[str, ...]] = {
    REQUEST_SCOPE: REQUEST_FIELD_IDS,
    WEBSEARCH_SCOPE: WEBSEARCH_FIELD_IDS,
    ATTEMPT_SCOPE: ATTEMPT_FIELD_IDS,
}
_SCOPE_GROUP_DIMENSIONS: dict[str, tuple[str, ...]] = {
    REQUEST_SCOPE: REQUEST_GROUP_DIMENSIONS,
    WEBSEARCH_SCOPE: WEBSEARCH_GROUP_DIMENSIONS,
    ATTEMPT_SCOPE: ATTEMPT_GROUP_DIMENSIONS,
}


def validate_format(value: str) -> Format:
    if value not in _FORMAT_SET:
        raise ValueError(f"unknown export format: {value!r}")
    return cast(Format, value)


def validate_scope(value: str) -> Scope:
    if value not in _SCOPE_SET:
        raise ValueError(f"unknown export scope: {value!r}")
    return cast(Scope, value)


def media_type(format: Format) -> str:
    return MEDIA_TYPES[format]


def file_extension(format: Format) -> str:
    return _EXTENSIONS[format]


def export_filename(scope: Scope, format: Format, exported_at: str) -> str:
    safe_time = exported_at.replace(":", "-").replace(".", "-")
    return f"mcc-{scope}-{format}-{safe_time}.{file_extension(format)}"


def requires_request_bodies(field_ids: Iterable[str]) -> bool:
    return not REQUEST_BODY_FIELDS.isdisjoint(field_ids)


def validate_fields(scope: Scope, field_ids: Iterable[str]) -> list[str]:
    known = _SCOPE_FIELD_IDS[scope]
    unknown = [field for field in field_ids if field not in known]
    if unknown:
        raise ValueError(f"unknown export field(s) for scope {scope!r}: {unknown!r}")
    return list(field_ids)


def validate_group_by(scope: Scope, group_by: Iterable[str]) -> list[str]:
    valid = _SCOPE_GROUP_DIMENSIONS[scope]
    if group_by and not valid:
        # The attempt scope is detail-only. Grouping it would mean averaging
        # latency across attempts of different models inside one request, which
        # is the number ``latency_by_model`` already answers properly -- and a
        # silently ignored ``group_by`` would hand back a flat file the caller
        # believes is grouped.
        raise ValueError(f"scope {scope!r} cannot be grouped; clear Group by")
    ordered: list[str] = []
    for dimension in group_by:
        if dimension not in valid:
            raise ValueError(
                f"unknown group dimension {dimension!r} for scope {scope!r}"
            )
        if dimension not in ordered:
            ordered.append(dimension)
    return ordered


# --------------------------------------------------------------------------
# Request-log scope: detail columns and aggregate expressions.
# --------------------------------------------------------------------------

# Structural columns always present in a request detail export. Provider and
# model are deliberately gated by their fields below (``providers``/``models``)
# so deselecting them actually hides the column, honoring the field checklist.
# ``id``/``ts_epoch``/``stream`` are needed by the store iterator regardless of
# the user's field selection (keyset pagination and row shaping).
_REQUEST_ALWAYS_COLUMNS: tuple[str, ...] = (
    "id",
    "ts_epoch",
    "stream",
    "ts_iso",
    "endpoint",
    "protocol",
    "key_label",
    "status",
    "tokens_in",
    "tokens_out",
    "duration_ms",
    "ttft_ms",
    # Beside ``ttft_ms``, which is unchanged: that one is what the client
    # waited, fallbacks included, and this one is what the model that answered
    # took on its own. Their difference is the time the chain lost to models
    # that did not answer. Empty on every row written before 7.4.0.
    "ttft_winner_ms",
    "route_attempt",
    # Always present rather than gated by a field group. A column named in
    # ``_REQUEST_COLUMN_ORDER`` that belongs to no group is unreachable -- the
    # note below records that ``headers`` is stranded exactly that way -- and
    # "which agent sent this" is a structural fact about a row, like its
    # endpoint and its protocol, not one of the metrics the checklist selects.
    "harness",
)

# Field -> columns added to the detail export when selected.
_REQUEST_FIELD_COLUMNS: dict[str, tuple[str, ...]] = {
    "providers": ("provider",),
    "models": (
        "resolved_model",
        "requested_model",
        "route_primary_model",
        "route_chain",
        "route_diverted_from",
        "route_diversion",
    ),
    "error_rate": ("error_kind", "error_message"),
    "cache_hit": ("cache_read_tokens", "cache_write_tokens"),
    "total_input": ("tokens_in", "cache_read_tokens", "cache_write_tokens"),
    "input_cached": ("cache_read_tokens",),
    "input_uncached": ("tokens_in",),
    # The reasoning count rides with both: a reader looking at output tokens
    # wants to know how much of the bill was thinking, and a reader who asked
    # for thinking wants the number as well as the transcript. A column may
    # belong to several groups -- ``cache_read_tokens`` belongs to three.
    "tokens_out": ("tokens_out", "reasoning_tokens"),
    "turns_with_tools": ("tool_call_count",),
    # Both columns, always together: an amount whose source is unknown is not
    # a number a reader may act on, and a source with no amount is the record
    # of an attempt to price, not a price.
    "cost": ("cost_usd", "cost_source"),
    "input": ("input_text", "input_chars"),
    "output": ("output_text", "output_chars"),
    "tool_calls": ("tool_calls", "tool_call_count"),
    # ``reasoning``/``requested_reasoning`` are the *policies* (what was asked
    # for and what was sent), not the thinking transcript, but they belong to
    # the same question and had no gating field of their own -- they were named
    # in the display order and labels while no field selected them, so they
    # could never actually be exported.
    "thinking": (
        "thinking_text",
        "thinking_chars",
        "reasoning_tokens",
        "reasoning",
        "requested_reasoning",
        "reasoning_adaptation",
    ),
    # Every column here is derived from ``request_attempts``, which no export
    # path joins; they are filled per row by the export route. The field id
    # exists so the three names are reachable at all -- five names already sit
    # in the display order unreachable because they belong to no field group.
    "ladder": (),
}

# Derived detail columns present on every request export, gated by no field.
#
# ``ttft_lost_to_fallbacks_ms`` is ``ttft_ms - ttft_winner_ms``: the part of
# what the client waited that belongs to models which did not answer. Both its
# inputs are in ``_REQUEST_ALWAYS_COLUMNS``, so like them it is a structural
# fact about the row rather than one of the metrics the checklist selects --
# and a reader who exports TTFT without it has the misattributed number and no
# way to see that it is misattributed.
_REQUEST_ALWAYS_DERIVED: tuple[str, ...] = ("ttft_lost_to_fallbacks_ms",)

# Derived detail columns computed per row (not raw SQL columns).
_REQUEST_DETAIL_DERIVED: dict[str, tuple[str, ...]] = {
    "cache_hit": ("cache_hit_rate",),
    # Derived, not SQL: they come from a per-request rollup of
    # ``request_attempts``, so ``request_detail_columns()`` must not name them
    # to the ``requests`` SELECT.
    "ladder": ("ladder_tries", "ladder_statuses", "ladder_root_cause"),
}

# Reverse map: column -> gating field id.
_REQUEST_COLUMN_FIELD: dict[str, str] = {}
for _field, _columns in _REQUEST_FIELD_COLUMNS.items():
    for _column in _columns:
        _REQUEST_COLUMN_FIELD[_column] = _field

# Preferred display order for a detail export (columns not listed here and not
# always-present are appended in mapping order).
_REQUEST_COLUMN_ORDER: tuple[str, ...] = (
    "id",
    "ts_iso",
    "endpoint",
    "protocol",
    "provider",
    "requested_model",
    "resolved_model",
    "key_label",
    "status",
    "error_kind",
    "error_message",
    "tokens_in",
    "cache_read_tokens",
    "cache_write_tokens",
    "tokens_out",
    "input_chars",
    "output_chars",
    "thinking_chars",
    "tool_call_count",
    "input_image_count",
    "ttft_ms",
    "ttft_winner_ms",
    "ttft_lost_to_fallbacks_ms",
    "duration_ms",
    "reasoning_tokens",
    "route_attempt",
    "route_primary_model",
    "route_chain",
    "route_diverted_from",
    "route_diversion",
    "input_text",
    "output_text",
    "thinking_text",
    "tool_calls",
    "reasoning",
    "requested_reasoning",
    "reasoning_adaptation",
    "params",
    "headers",
    "harness",
    "input_sha256",
    "output_sha256",
    "cache_hit_rate",
    "cost_usd",
    "cost_source",
    "ladder_tries",
    "ladder_statuses",
    "ladder_root_cause",
)

_REQUEST_COLUMN_LABELS: dict[str, str] = {
    "id": "ID",
    "ts_epoch": "Timestamp (epoch)",
    "stream": "Stream",
    "ts_iso": "Time",
    "endpoint": "Endpoint",
    "protocol": "Protocol",
    "provider": "Provider",
    "requested_model": "Requested model",
    "resolved_model": "Resolved model",
    "key_label": "Key",
    "status": "Status",
    "error_kind": "Error kind",
    "error_message": "Error message",
    "tokens_in": "Input (uncached)",
    "cache_read_tokens": "Cached input",
    "cache_write_tokens": "Cache writes",
    "tokens_out": "Tokens out",
    "input_chars": "Input chars",
    "output_chars": "Output chars",
    "thinking_chars": "Thinking chars",
    "tool_call_count": "Tool calls",
    "input_image_count": "Images in",
    "ttft_ms": "TTFT (ms)",
    "ttft_winner_ms": "Winner TTFT (ms)",
    "ttft_lost_to_fallbacks_ms": "Lost to fallbacks (ms)",
    "duration_ms": "Duration (ms)",
    "reasoning_tokens": "Reasoning tokens",
    "route_attempt": "Route attempt",
    "route_primary_model": "Route primary",
    "route_chain": "Route chain",
    "route_diverted_from": "Diverted from",
    "route_diversion": "Diversion",
    "input_text": "Input",
    "output_text": "Output",
    "thinking_text": "Reasoning",
    "tool_calls": "Tool calls",
    "reasoning": "Reasoning policy (applied)",
    "requested_reasoning": "Reasoning policy (requested)",
    "reasoning_adaptation": "Reasoning adaptation",
    "params": "Params",
    "headers": "Headers",
    "harness": "Harness",
    "input_sha256": "Input SHA-256",
    "output_sha256": "Output SHA-256",
    "cache_hit_rate": "Cache hit rate",
    "cost_usd": "Cost (USD)",
    "cost_source": "Cost source",
    "ladder_tries": "Upstream tries",
    "ladder_statuses": "Upstream statuses",
    "ladder_root_cause": "Root cause",
}


def request_field_labels() -> list[tuple[str, str]]:
    return [
        (field_id, REQUEST_FIELD_LABELS[field_id]) for field_id in REQUEST_FIELD_IDS
    ]


def websearch_field_labels() -> list[tuple[str, str]]:
    return [
        (field_id, WEBSEARCH_FIELD_LABELS[field_id]) for field_id in WEBSEARCH_FIELD_IDS
    ]


def request_detail_columns(field_ids: Iterable[str]) -> list[str]:
    """Return the *SQL* detail-export columns implied by the selected fields.

    Excludes derived columns (e.g. ``cache_hit_rate``), which are computed per
    row by :func:`compute_request_detail_derived` and surfaced separately via
    :func:`request_detail_derived_columns`.
    """
    selected = set(field_ids)
    chosen: list[str] = []
    chosen_set: set[str] = set()
    derived = {item for group in _REQUEST_DETAIL_DERIVED.values() for item in group}
    derived.update(_REQUEST_ALWAYS_DERIVED)
    # Always-columns that are not part of the display-order list (keyset
    # pagination needs ``ts_epoch``; row shaping needs ``stream``) come first.
    for column in _REQUEST_ALWAYS_COLUMNS:
        if column not in _REQUEST_COLUMN_ORDER and column not in chosen_set:
            chosen.append(column)
            chosen_set.add(column)
    for column in _REQUEST_COLUMN_ORDER:
        if column in derived:
            continue
        if column in _REQUEST_ALWAYS_COLUMNS:
            if column not in chosen_set:
                chosen.append(column)
                chosen_set.add(column)
        else:
            field = _REQUEST_COLUMN_FIELD.get(column)
            if field is not None and field in selected and column not in chosen_set:
                chosen.append(column)
                chosen_set.add(column)
    return chosen


def request_detail_derived_columns(field_ids: Iterable[str]) -> list[str]:
    """Return the derived (computed) columns for the selected fields.

    The always-derived ones come first, beside the always-present SQL columns
    they are computed from.
    """
    selected = set(field_ids)
    result: list[str] = list(_REQUEST_ALWAYS_DERIVED)
    seen: set[str] = set(result)
    for field_id in REQUEST_FIELD_IDS:
        if field_id in selected:
            for derived in _REQUEST_DETAIL_DERIVED.get(field_id, ()):
                if derived not in seen:
                    seen.add(derived)
                    result.append(derived)
    return result


def request_detail_headers(columns: Iterable[str]) -> list[str]:
    return [_REQUEST_COLUMN_LABELS.get(column, column) for column in columns]


def compute_request_detail_derived(
    row: dict[str, Any], field_ids: Iterable[str]
) -> None:
    """Mutate ``row`` in place, filling derived columns for selected fields."""
    selected = set(field_ids)
    if "ttft_lost_to_fallbacks_ms" not in row:
        row["ttft_lost_to_fallbacks_ms"] = _ttft_lost_to_fallbacks(row)
    if "cache_hit" in selected and "cache_hit_rate" not in row:
        row["cache_hit_rate"] = _cache_hit_ratio(row)


def _ttft_lost_to_fallbacks(row: dict[str, Any]) -> Any:
    """``ttft_ms - ttft_winner_ms``, or None when either was never measured.

    Empty rather than zero on a row that predates per-attempt measurement:
    "this request lost nothing to fallbacks" and "we cannot say what it lost"
    are different claims, and a column of zeroes would make the second read as
    the first for every row written before 7.4.0.
    """

    client = row.get("ttft_ms")
    winner = row.get("ttft_winner_ms")
    if client is None or winner is None:
        return None
    return round(float(client) - float(winner), 3)


def _cache_hit_ratio(row: dict[str, Any]) -> Any:
    cached = row.get("cache_read_tokens")
    if cached is None:
        return None
    total = _total_input(row)
    if not total:
        return None
    return round(cached / total, 6)


def _total_input(row: dict[str, Any]) -> float:
    return float(
        (row.get("tokens_in") or 0)
        + (row.get("cache_read_tokens") or 0)
        + (row.get("cache_write_tokens") or 0)
    )


# --------------------------------------------------------------------------
# Request-log scope: aggregate expressions.
# --------------------------------------------------------------------------

# alias -> (sql expression). Aggregate rows carry these aliases; derived ratios
# (error_rate, cache_hit_rate) are computed per row afterwards.
_REQUEST_AGGREGATE: dict[str, tuple[tuple[str, str], ...]] = {
    "error_rate": (
        ("requests", "COUNT(*)"),
        ("errors", "SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END)"),
    ),
    "cache_hit": (
        ("cache_read_tokens", "COALESCE(SUM(cache_read_tokens), 0)"),
        (
            "cache_reported",
            "SUM(CASE WHEN cache_read_tokens IS NOT NULL THEN 1 ELSE 0 END)",
        ),
    ),
    "total_input": (
        (
            "total_input",
            "COALESCE(SUM(tokens_in), 0) + COALESCE(SUM(cache_read_tokens), 0)"
            " + COALESCE(SUM(cache_write_tokens), 0)",
        ),
    ),
    "input_cached": (("cache_read_tokens", "COALESCE(SUM(cache_read_tokens), 0)"),),
    "input_uncached": (("tokens_in", "COALESCE(SUM(tokens_in), 0)"),),
    "tokens_out": (("tokens_out", "COALESCE(SUM(tokens_out), 0)"),),
    "turns_with_tools": (
        ("turns_with_tools", "SUM(CASE WHEN tool_call_count > 0 THEN 1 ELSE 0 END)"),
    ),
    # Reported and estimated are summed separately and never added together.
    # A total that mixes what a host billed with what MCC computed launders a
    # guess into a fact, and no reader can tell afterwards which half was
    # which. ``cost_priced``/``cost_requests`` are the "N of M priced"
    # denominator: a sum over a partly priced group is not a group total, and
    # without the count beside it there is no way to know that.
    #
    # Bare ``SUM`` on purpose, with no ``COALESCE``: SQLite sums zero rows to
    # NULL, and NULL is the correct answer for a group nothing priced.
    "cost": (
        ("cost_usd", "SUM(cost_usd)"),
        (
            "cost_reported_usd",
            "SUM(CASE WHEN cost_source = 'provider' THEN cost_usd END)",
        ),
        (
            "cost_estimated_usd",
            "SUM(CASE WHEN cost_source IS NOT NULL"
            " AND cost_source <> 'provider' THEN cost_usd END)",
        ),
        (
            "cost_priced",
            "SUM(CASE WHEN cost_usd IS NOT NULL THEN 1 ELSE 0 END)",
        ),
        ("cost_requests", "COUNT(*)"),
    ),
    "tool_calls": (
        ("tool_calls", "COALESCE(SUM(tool_call_count), 0)"),
        ("turns_with_tools", "SUM(CASE WHEN tool_call_count > 0 THEN 1 ELSE 0 END)"),
    ),
    "input": (("input_chars", "COALESCE(SUM(input_chars), 0)"),),
    "output": (("output_chars", "COALESCE(SUM(output_chars), 0)"),),
    "thinking": (("thinking_chars", "COALESCE(SUM(thinking_chars), 0)"),),
}

# Dimension -> SQL alias expression for the request-log scope.
_REQUEST_DIMENSION_SQL: dict[str, str] = {
    # Shared with the dashboard breakdown so an export and the page it was
    # exported from group the same rows under the same key. Traffic a local
    # rule answered has no provider by design and is named for its rule
    # instead of being pooled into "(unknown)" with genuinely unattributed rows.
    "provider": PROVIDER_KEY_SQL,
    "model": "COALESCE(resolved_model, '(unknown)')",
    "key": "COALESCE(key_label, '(unknown)')",
    "harness": "COALESCE(harness, '(unknown)')",
    "period": "strftime('%Y-%m-%d', ts_epoch, 'unixepoch')",
}

# Derived aggregate metrics computed per row after SQL.
_REQUEST_AGGREGATE_DERIVED: dict[str, tuple[str, ...]] = {
    "error_rate": ("error_rate",),
    "cache_hit": ("cache_hit_rate",),
}


def request_aggregate_sql(
    field_ids: Iterable[str], group_by: Iterable[str]
) -> tuple[str, list[str]]:
    """Build ``SELECT expr, ...`` plus output column names for a request aggregate.

    Emits group dimensions (in ``group_by`` order) followed by the measurable
    metrics for the selected non-dimension fields. Dimension fields that are
    selected but not part of ``group_by`` are omitted (a per-provider number is
    meaningless when not grouped by provider).
    """
    dimensions = list(group_by)
    dimension_aliases = [f"{_REQUEST_DIMENSION_SQL[d]} AS {d}" for d in dimensions]
    dimension_names = list(dimensions)
    selected = set(field_ids)

    selects: list[str] = list(dimension_aliases)
    names: list[str] = dimension_names
    seen: set[str] = set(dimension_names)
    for field_id in REQUEST_FIELD_IDS:
        if field_id not in selected:
            continue
        if field_id in REQUEST_GROUP_DIMENSIONS:
            # Dimension handled above; the metric-only aliases like requests/
            # errors only belong to error_rate/status, which are metric fields.
            continue
        for alias, expr in _REQUEST_AGGREGATE.get(field_id, ()):
            if alias in seen:
                continue
            seen.add(alias)
            selects.append(f"{expr} AS {alias}")
            names.append(alias)
    return ", ".join(selects) if selects else "COUNT(*) AS requests", names


def request_aggregate_derived_columns(field_ids: Iterable[str]) -> list[str]:
    """Return the derived (computed) aggregate columns for the selected fields."""
    selected = set(field_ids)
    result: list[str] = []
    seen: set[str] = set()
    for field_id in REQUEST_FIELD_IDS:
        if field_id in selected:
            for derived in _REQUEST_AGGREGATE_DERIVED.get(field_id, ()):
                if derived not in seen:
                    seen.add(derived)
                    result.append(derived)
    return result


def compute_request_aggregate_derived(
    row: dict[str, Any], field_ids: Iterable[str]
) -> None:
    selected = set(field_ids)
    if "error_rate" in selected and "error_rate" not in row:
        requests = row.get("requests") or 0
        errors = row.get("errors") or 0
        row["error_rate"] = round(errors / requests, 6) if requests else None
    if "cache_hit" in selected and "cache_hit_rate" not in row:
        row["cache_hit_rate"] = _cache_hit_ratio(row)


# --------------------------------------------------------------------------
# Web-search scope.
# --------------------------------------------------------------------------

_WEBSEARCH_ALWAYS_COLUMNS: tuple[str, ...] = (
    "id",
    "ts_iso",
    "provider",
    "key_label",
    "query",
    "status",
    "duration_ms",
    "results_count",
)

_WEBSEARCH_FIELD_COLUMNS: dict[str, tuple[str, ...]] = {
    "cost_usd": ("cost_usd",),
    "error_kind": ("error_kind",),
    "error_message": ("error_message",),
    "attempt_number": ("attempt_number",),
    "route_id": ("route_id",),
    "input": ("input",),
    "output": ("output",),
    "provider_config": ("provider_config",),
    "content_captured": ("content_captured",),
}

_WEBSEARCH_COLUMN_FIELD: dict[str, str] = {}
for _field, _columns in _WEBSEARCH_FIELD_COLUMNS.items():
    for _column in _columns:
        _WEBSEARCH_COLUMN_FIELD[_column] = _field

_WEBSEARCH_COLUMN_ORDER: tuple[str, ...] = (
    "id",
    "ts_iso",
    "provider",
    "key_label",
    "query",
    "status",
    "results_count",
    "duration_ms",
    "cost_usd",
    "error_kind",
    "error_message",
    "attempt_number",
    "route_id",
    "content_captured",
    "input",
    "output",
    "provider_config",
)

_WEBSEARCH_COLUMN_LABELS: dict[str, str] = {
    "id": "ID",
    "ts_iso": "Time",
    "provider": "Provider",
    "key_label": "Key",
    "query": "Query",
    "status": "Status",
    "results_count": "Results",
    "duration_ms": "Duration (ms)",
    "cost_usd": "Cost (USD)",
    "error_kind": "Error kind",
    "error_message": "Error message",
    "attempt_number": "Attempt #",
    "route_id": "Route",
    "content_captured": "Content captured",
    "input": "Input",
    "output": "Output",
    "provider_config": "Provider config",
}

# Websearch fields that are raw text and cannot be aggregated (detail-only).
_WEBSEARCH_DETAIL_ONLY = frozenset(
    {
        "query",
        "error_kind",
        "error_message",
        "attempt_number",
        "route_id",
        "input",
        "output",
        "provider_config",
    }
)

_WEBSEARCH_AGGREGATE: dict[str, tuple[tuple[str, str], ...]] = {
    "results_count": (("results", "COALESCE(SUM(results_count), 0)"),),
    "duration_ms": (("avg_duration_ms", "AVG(duration_ms)"),),
    "cost_usd": (("cost_usd", "COALESCE(SUM(cost_usd), 0)"),),
    "status": (
        ("requests", "COUNT(*)"),
        ("errors", "COALESCE(SUM(status = 'error'), 0)"),
    ),
}

_WEBSEARCH_DIMENSION_SQL: dict[str, str] = {
    "provider": "COALESCE(provider, '(unknown)')",
    "key": "COALESCE(key_label, '(unknown)')",
    "period": "strftime('%Y-%m-%d', ts_epoch, 'unixepoch')",
}


def websearch_detail_columns(field_ids: Iterable[str]) -> list[str]:
    selected = set(field_ids)
    chosen: list[str] = []
    chosen_set: set[str] = set()
    for column in _WEBSEARCH_COLUMN_ORDER:
        if column in _WEBSEARCH_ALWAYS_COLUMNS:
            if column not in chosen_set:
                chosen.append(column)
                chosen_set.add(column)
        else:
            field = _WEBSEARCH_COLUMN_FIELD.get(column)
            if field is not None and field in selected and column not in chosen_set:
                chosen.append(column)
                chosen_set.add(column)
    return chosen


def websearch_detail_headers(columns: Iterable[str]) -> list[str]:
    return [_WEBSEARCH_COLUMN_LABELS.get(column, column) for column in columns]


def websearch_aggregate_sql(
    field_ids: Iterable[str], group_by: Iterable[str]
) -> tuple[str, list[str]]:
    dimensions = list(group_by)
    dimension_aliases = [f"{_WEBSEARCH_DIMENSION_SQL[d]} AS {d}" for d in dimensions]
    dimension_names = list(dimensions)
    selected = set(field_ids)

    selects: list[str] = list(dimension_aliases)
    names: list[str] = dimension_names
    seen: set[str] = set(dimension_names)
    for field_id in WEBSEARCH_FIELD_IDS:
        if field_id not in selected:
            continue
        if field_id in WEBSEARCH_GROUP_DIMENSIONS:
            continue
        for alias, expr in _WEBSEARCH_AGGREGATE.get(field_id, ()):
            if alias in seen:
                continue
            seen.add(alias)
            selects.append(f"{expr} AS {alias}")
            names.append(alias)
    if not selects:
        selects = ["COUNT(*) AS requests"]
        names = ["requests"]
    return ", ".join(selects), names


_WEBSEARCH_AGGREGATE_DERIVED = {
    "status": ("error_rate",),
}


def websearch_aggregate_derived_columns(field_ids: Iterable[str]) -> list[str]:
    """Return the derived (computed) aggregate columns for the selected fields."""
    selected = set(field_ids)
    result: list[str] = []
    seen: set[str] = set()
    for field_id in WEBSEARCH_FIELD_IDS:
        if field_id in selected:
            for derived in _WEBSEARCH_AGGREGATE_DERIVED.get(field_id, ()):
                if derived not in seen:
                    seen.add(derived)
                    result.append(derived)
    return result


def compute_websearch_aggregate_derived(
    row: dict[str, Any], field_ids: Iterable[str]
) -> None:
    if "status" in set(field_ids) and "error_rate" not in row:
        requests = row.get("requests") or 0
        errors = row.get("errors") or 0
        row["error_rate"] = round(errors / requests, 6) if requests else None


def websearch_detail_only_fields(field_ids: Iterable[str]) -> list[str]:
    return [field for field in field_ids if field in _WEBSEARCH_DETAIL_ONLY]


# --------------------------------------------------------------------------
# Attempt scope: one row per attempt, joined to its request's dimensions.
# --------------------------------------------------------------------------
#
# Until 7.6.0 no export path read ``request_attempts`` at all except the
# per-request ladder rollup, so the models that *did not* answer -- the ones a
# fallback chain actually spends its time on -- were unexportable. The request
# scope has one row per request and therefore exactly one provider, one model
# and one verdict per row; a three-model chain's first two models are simply
# not in it.
#
# Every column here is NULL-not-zero. The latency columns did not exist before
# 7.4.0 and are not backfillable, ``tokens_out`` is filled only on the winning
# attempt (and on a vision describe hop), and ``cost_usd`` is filled only where
# an attempt reported its own usage. A zero in any of them would read as a
# measurement.

#: Parent-request dimensions carried onto every attempt row. A failed attempt
#: has no endpoint, harness or requested model of its own -- those are facts
#: about the request it belongs to -- and without them an attempt row cannot be
#: filtered or grouped by the reader afterwards.
_ATTEMPT_REQUEST_COLUMNS: tuple[str, ...] = (
    "request_id",
    "ts_iso",
    "harness",
    "endpoint",
    "requested_model",
    "resolved_model",
    "request_status",
)

#: Attempt columns present on every attempt export, whatever the selection.
_ATTEMPT_ALWAYS_COLUMNS: tuple[str, ...] = (
    *_ATTEMPT_REQUEST_COLUMNS,
    "attempt",
    "attempt_provider",
    "attempt_model",
    "outcome",
    "key_label",
    "ttft_ms",
    "first_reasoning_ms",
    "duration_ms",
)

_ATTEMPT_FIELD_COLUMNS: dict[str, tuple[str, ...]] = {
    "failure": ("error_kind", "error_message"),
    "tokens": ("tokens_in", "tokens_out"),
    "cost": ("cost_usd", "cost_source"),
    "ladder": ("ladder_tries",),
    "wire": ("reasoning_emitted", "key_index"),
    "recovery": (),
}

#: Derived on every attempt export: the two or three words that say what ended
#: this attempt, assembled the same way the request modal's timeline assembles
#: them so a download and the page it came from cannot disagree.
_ATTEMPT_ALWAYS_DERIVED: tuple[str, ...] = ("ended_by",)

_ATTEMPT_DETAIL_DERIVED: dict[str, tuple[str, ...]] = {
    # Why the router refused to try this model. A skipped attempt has no
    # latency, no tokens and no error -- the reason is the whole row.
    "failure": ("bench_reason",),
    "ladder": ("ladder_root_cause",),
    "wire": ("wire_surface",),
    "recovery": ("early_retries", "midstream_recoveries", "salvages"),
}

_ATTEMPT_COLUMN_FIELD: dict[str, str] = {}
for _field, _columns in _ATTEMPT_FIELD_COLUMNS.items():
    for _column in _columns:
        _ATTEMPT_COLUMN_FIELD[_column] = _field

_ATTEMPT_COLUMN_ORDER: tuple[str, ...] = (
    "request_id",
    "ts_iso",
    "attempt",
    "harness",
    "endpoint",
    "requested_model",
    "resolved_model",
    "request_status",
    "attempt_provider",
    "attempt_model",
    "outcome",
    "ended_by",
    "error_kind",
    "error_message",
    "bench_reason",
    "key_label",
    "key_index",
    "ttft_ms",
    "first_reasoning_ms",
    "duration_ms",
    "tokens_in",
    "tokens_out",
    "cost_usd",
    "cost_source",
    "ladder_tries",
    "ladder_root_cause",
    "wire_surface",
    "reasoning_emitted",
    "early_retries",
    "midstream_recoveries",
    "salvages",
)

_ATTEMPT_COLUMN_LABELS: dict[str, str] = {
    "request_id": "Request ID",
    "ts_iso": "Time",
    "attempt": "Attempt #",
    "harness": "Harness",
    "endpoint": "Endpoint",
    "requested_model": "Requested model",
    "resolved_model": "Resolved model",
    "request_status": "Request status",
    "attempt_provider": "Attempt provider",
    "attempt_model": "Attempt model",
    "outcome": "Outcome",
    "ended_by": "Ended by",
    "error_kind": "Failure kind",
    "error_message": "Failure or skip reason",
    "bench_reason": "Bench reason",
    "key_label": "Key",
    "key_index": "Key index",
    "ttft_ms": "TTFT (ms)",
    "first_reasoning_ms": "First reasoning (ms)",
    "duration_ms": "Duration (ms)",
    "tokens_in": "Attempt input tokens",
    "tokens_out": "Attempt output tokens",
    "cost_usd": "Attempt cost (USD)",
    "cost_source": "Attempt cost source",
    "ladder_tries": "Upstream tries",
    "ladder_root_cause": "Root cause",
    "wire_surface": "Wire surface",
    "reasoning_emitted": "Reasoning emitted",
    "early_retries": "Early retries",
    "midstream_recoveries": "Midstream recoveries",
    "salvages": "Salvages",
}


def attempt_field_labels() -> list[tuple[str, str]]:
    return [
        (field_id, ATTEMPT_FIELD_LABELS[field_id]) for field_id in ATTEMPT_FIELD_IDS
    ]


def attempt_detail_columns(field_ids: Iterable[str]) -> list[str]:
    """Return the non-derived attempt columns implied by the selected fields."""
    selected = set(field_ids)
    derived = {item for group in _ATTEMPT_DETAIL_DERIVED.values() for item in group}
    derived.update(_ATTEMPT_ALWAYS_DERIVED)
    chosen: list[str] = []
    for column in _ATTEMPT_COLUMN_ORDER:
        if column in derived or column in chosen:
            continue
        if column in _ATTEMPT_ALWAYS_COLUMNS:
            chosen.append(column)
            continue
        field = _ATTEMPT_COLUMN_FIELD.get(column)
        if field is not None and field in selected:
            chosen.append(column)
    return chosen


def attempt_detail_derived_columns(field_ids: Iterable[str]) -> list[str]:
    """Return the derived attempt columns for the selected fields."""
    selected = set(field_ids)
    result: list[str] = list(_ATTEMPT_ALWAYS_DERIVED)
    seen: set[str] = set(result)
    for field_id in ATTEMPT_FIELD_IDS:
        if field_id in selected:
            for derived in _ATTEMPT_DETAIL_DERIVED.get(field_id, ()):
                if derived not in seen:
                    seen.add(derived)
                    result.append(derived)
    return result


def attempt_output_columns(field_ids: Iterable[str]) -> list[str]:
    """Return every output column, in display order, for the selected fields."""
    wanted = set(attempt_detail_columns(field_ids))
    wanted.update(attempt_detail_derived_columns(field_ids))
    return [column for column in _ATTEMPT_COLUMN_ORDER if column in wanted]


def attempt_detail_headers(columns: Iterable[str]) -> list[str]:
    return [_ATTEMPT_COLUMN_LABELS.get(column, column) for column in columns]


def compute_attempt_detail_derived(
    row: dict[str, Any], field_ids: Iterable[str]
) -> None:
    """Mutate ``row`` in place, filling the derived attempt columns.

    Reads the parsed ``request_attempts.params`` object the store left under
    :data:`ATTEMPT_PARAMS_KEY`. Everything it cannot find stays ``None``: an
    attempt written before a given diagnostic existed did not record it, which
    is not the same claim as "it was zero".
    """
    selected = set(field_ids)
    params = row.get(ATTEMPT_PARAMS_KEY)
    params = params if isinstance(params, dict) else {}
    bench = params.get("bench")
    bench = bench if isinstance(bench, dict) else None
    ladder = params.get("ladder")
    ladder = ladder if isinstance(ladder, dict) else None

    row["ended_by"] = _attempt_ended_by(row, bench, ladder)
    if "failure" in selected:
        row["bench_reason"] = _bench_reason(bench)
    if "ladder" in selected:
        root_cause = ladder.get("root_cause") if ladder else None
        row["ladder_root_cause"] = str(root_cause) if root_cause else None
    if "wire" in selected:
        wire = params.get("wire")
        surface = wire.get("surface") if isinstance(wire, dict) else None
        row["wire_surface"] = str(surface) if surface else None
    if "recovery" in selected:
        for counter in ("early_retries", "midstream_recoveries", "salvages"):
            value = params.get(counter)
            # Absent means the collector never counted anything for this
            # attempt -- including every attempt written before recovery
            # observability existed. Zero is what a row says when it was
            # watched and nothing happened.
            row[counter] = int(value) if isinstance(value, int) else None


def _attempt_ended_by(
    row: dict[str, Any],
    bench: dict[str, Any] | None,
    ladder: dict[str, Any] | None,
) -> Any:
    if bench is not None:
        kind = bench.get("last_kind")
        return f"benched ({kind})" if kind else "benched"
    error_kind = row.get("error_kind")
    if error_kind:
        return str(error_kind)
    if ladder is not None:
        root_cause = ladder.get("root_cause")
        if root_cause:
            return str(root_cause)
    outcome = row.get("outcome")
    return str(outcome) if outcome else None


def _bench_reason(bench: dict[str, Any] | None) -> Any:
    """The registry's account of a bench, flattened into one cell.

    The prose the router wrote is already in ``error_message``; this is the
    structured half of the same fact, so a reader can sort and count benches
    without parsing sentences. Keys follow ``route_health.BenchSnapshot``.
    """
    if bench is None:
        return None
    parts: list[str] = []
    mode = bench.get("mode")
    if mode:
        parts.append(str(mode))
    failures = bench.get("failures")
    if isinstance(failures, int):
        plural = "" if failures == 1 else "s"
        parts.append(f"{failures} failure{plural}")
    kind = bench.get("last_kind")
    status = bench.get("last_status")
    if kind:
        parts.append(f"last {status} {kind}" if status is not None else f"last {kind}")
    remaining = bench.get("remaining_seconds")
    if isinstance(remaining, (int, float)):
        parts.append(f"{remaining:g} s left")
    return " · ".join(parts) if parts else "benched"


# --------------------------------------------------------------------------
# Format renderers.
# --------------------------------------------------------------------------


def render_csv(
    rows: Iterable[dict[str, Any]], columns: list[str], headers: list[str]
) -> Iterator[bytes]:
    """Yield UTF-8 BOM then one CSV line per row.

    ``headers`` are the human-readable column labels (the first row);
    ``columns`` are the row-dict keys each data cell is read from. They differ
    for the detail export (labels vs SQL column names), and reading cells by
    label produced empty data rows.
    """
    yield b"\xef\xbb\xbf"
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(headers)
    yield buffer.getvalue().encode("utf-8")
    for row in rows:
        buffer.seek(0)
        buffer.truncate(0)
        writer.writerow([_csv_cell(row.get(column)) for column in columns])
        yield buffer.getvalue().encode("utf-8")


def _csv_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def render_json_array(rows: Iterable[dict[str, Any]]) -> Iterator[bytes]:
    """Yield a JSON array streamed as ``[`` … first row … ``,``-prefixed rows … ``]``."""
    iterator = iter(rows)
    try:
        first = next(iterator)
    except StopIteration:
        yield b"[]"
        return
    yield b"[" + _json_bytes(first)
    for row in iterator:
        yield b"," + _json_bytes(row)
    yield b"]"


def render_json_object(payload: dict[str, Any]) -> Iterator[bytes]:
    yield json.dumps(payload, ensure_ascii=False, default=_json_default).encode("utf-8")


def _json_bytes(row: dict[str, Any]) -> bytes:
    return json.dumps(row, ensure_ascii=False, default=_json_default).encode("utf-8")


def _json_default(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"object of type {type(value).__name__} is not JSON serializable")


def render_xlsx(
    rows: Iterable[dict[str, Any]],
    columns: list[str],
    headers: list[str],
    sheet_title: str = "Export",
) -> Iterator[bytes]:
    """Stream an XLSX workbook built in ``write_only`` mode via a spool file.

    ``headers`` are the human-readable labels (the first row); ``columns`` are
    the row-dict keys each data cell is read from (see :func:`render_csv`).
    """
    # Deferred import: openpyxl costs ~0.25 s and only this one renderer
    # needs it, so a server that never exports an XLSX never pays it.
    import openpyxl

    workbook = openpyxl.Workbook(write_only=True)
    sheet = workbook.create_sheet(title=sheet_title[:31])
    sheet.append(headers)
    for row in rows:
        sheet.append([_xlsx_cell(row.get(column)) for column in columns])
    with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as spool:
        workbook.save(spool)
        spool.seek(0)
        while True:
            chunk = spool.read(256 * 1024)
            if not chunk:
                break
            yield chunk


def _xlsx_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return value


def render_txt(
    rows: Iterable[dict[str, Any]],
    columns: list[str],
    headers: list[str],
    title: str,
    summary: str,
) -> Iterator[bytes]:
    """Yield a readable report: title, summary, aligned columns, group subtotals.

    Column widths are derived from the widest value seen (capped at 40 chars);
    values are padded, never truncated, so long model ids and endpoints survive
    the report.
    """
    yield f"{title}\n".encode()
    yield f"{summary}\n\n".encode()
    widths = [min(max(len(header), 1), 40) for header in headers]
    pending: list[list[str]] = []
    for row in rows:
        values = [
            "" if row.get(column) is None else _serialize_cell(row.get(column))
            for column in columns
        ]
        for index, value in enumerate(values):
            length = len(value)
            if index < len(widths) and length > widths[index]:
                widths[index] = min(length, 40)
        pending.append(values)
    yield _txt_row(headers, widths).encode("utf-8")
    yield _txt_rule(widths).encode("utf-8")
    for values in pending:
        yield _txt_row(values, widths).encode("utf-8")
    yield b"\n"


def _txt_row(values: list[str], widths: list[int]) -> str:
    # Pad only, never clip: the widths cap at 40 for header sizing, but a
    # wider value keeps its column overflow rather than losing characters --
    # silently dropping content broke the fidelity this renderer promises.
    cells = [value.ljust(width) for value, width in zip(values, widths, strict=True)]
    return " | ".join(cells) + "\n"


def _txt_rule(widths: list[int]) -> str:
    return "-" * (sum(widths) + 3 * (len(widths) - 1)) + "\n"


def _serialize_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
