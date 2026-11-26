"""Read-only live join of ``requests.db`` and ``core/request_tasks`` per RC session."""

import contextlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from my_claude_code.api.model_admin import request_endpoint_routing
from my_claude_code.config.paths import request_log_path
from my_claude_code.core.request_tasks import inflight_report

_RECENT_REQUEST_LIMIT = 10


def _opt_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def _opt_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except TypeError, ValueError:
        return None


def _norm_path(raw: str) -> str:
    text = raw.strip()
    if not text:
        return ""
    if len(text) > 1 and not (len(text) == 3 and text[1:] in (":\\", ":/")):
        text = text.rstrip("\\/") or text
    return text.replace("\\", "/").casefold()


def _cwd_variants(*raw_paths: str) -> tuple[list[str], set[str]]:
    exact: list[str] = []
    seen: set[str] = set()
    normed: set[str] = set()
    for raw in raw_paths:
        text = (raw or "").strip()
        if not text:
            continue
        candidates = [text]
        trimmed = text.rstrip("\\/")
        if trimmed and trimmed != text:
            candidates.append(trimmed)
        with contextlib.suppress(OSError, RuntimeError):
            resolved = str(Path(text).expanduser().resolve())
            candidates.append(resolved)
            resolved_trimmed = resolved.rstrip("\\/")
            if resolved_trimmed:
                candidates.append(resolved_trimmed)
        candidates.extend([c.replace("\\", "/") for c in list(candidates)])
        candidates.extend([c.replace("/", "\\") for c in list(candidates)])
        for cand in candidates:
            if cand and cand not in seen:
                seen.add(cand)
                exact.append(cand)
            norm = _norm_path(cand)
            if norm:
                normed.add(norm)
    return exact, normed


def _open_sqlite_readonly(path: Path) -> sqlite3.Connection | None:
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return {str(row[1]) for row in rows}
    except sqlite3.Error:
        return set()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _extract_wire_surface(raw_params: Any) -> str | None:
    if isinstance(raw_params, Mapping):
        wire = raw_params.get("wire")
        if isinstance(wire, Mapping):
            surf = wire.get("surface")
            return str(surf).strip() if isinstance(surf, str) and surf.strip() else None
        return None
    if isinstance(raw_params, str) and raw_params.strip():
        try:
            parsed = json.loads(raw_params)
        except json.JSONDecodeError:
            return None
        return _extract_wire_surface(parsed)
    return None


def _fetch_attempt_surfaces(
    conn: sqlite3.Connection, request_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    if not request_ids or not _table_exists(conn, "request_attempts"):
        return {}
    cols = _table_columns(conn, "request_attempts")
    if not {"request_id", "attempt", "provider", "outcome"} <= cols:
        return {}
    select_parts = ["request_id", "attempt", "provider", "outcome"]
    select_parts.append("model_ref" if "model_ref" in cols else "'' AS model_ref")
    select_parts.append("params" if "params" in cols else "NULL AS params")
    markers = ", ".join("?" * len(request_ids))
    try:
        rows = conn.execute(
            f"SELECT {', '.join(select_parts)} FROM request_attempts "
            f"WHERE request_id IN ({markers}) ORDER BY request_id, attempt",
            request_ids,
        ).fetchall()
    except sqlite3.Error:
        return {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["request_id"]), []).append(
            {
                "attempt": row["attempt"],
                "provider": row["provider"],
                "model_ref": row["model_ref"],
                "outcome": row["outcome"],
                "wire_surface": _extract_wire_surface(row["params"]),
            }
        )
    return grouped


def _row_cost_split(
    row: Mapping[str, Any],
) -> tuple[float | None, float | None, float]:
    """Return ``(reported_cost_usd, estimated_cost_usd, effective_cost_usd)``."""

    raw_cost = _opt_float(row.get("cost_usd"))
    raw_est = _opt_float(row.get("estimated_cost_usd"))
    cost_source = str(row.get("cost_source") or "").strip()

    if cost_source == "provider":
        reported = raw_cost
        estimated = raw_est
    elif cost_source and cost_source != "unpriced" and raw_cost is not None:
        reported = None
        estimated = raw_est if raw_est is not None else raw_cost
    elif raw_cost is not None and not cost_source and raw_est is None:
        reported = raw_cost
        estimated = None
    else:
        reported = None
        estimated = raw_est

    effective = (
        raw_cost if raw_cost is not None else (raw_est if raw_est is not None else 0.0)
    )
    return reported, estimated, effective


def _matching_inflight(
    inflight_data: Mapping[str, Any],
    *,
    session_ids: set[str],
    cwd_normed: set[str],
) -> list[dict[str, Any]]:
    if not inflight_data or inflight_data.get("enabled") is False:
        return []
    rows = inflight_data.get("rows")
    if not isinstance(rows, list):
        return []
    matched: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        row_sid = str(row.get("session_id") or "").strip()
        if row_sid:
            if row_sid not in session_ids:
                continue
        else:
            row_proj = _norm_path(str(row.get("project_dir") or ""))
            if not row_proj or row_proj not in cwd_normed:
                continue
        matched.append(
            {
                "request_id": str(row.get("id") or ""),
                "phase": str(row.get("phase") or ""),
                "attempt": row.get("attempt_index"),
                "waited_s": float(row.get("waited_s") or 0.0),
                "model": row.get("model_ref") or row.get("requested_model") or "",
                "provider": row.get("provider") or "",
                "ttft_ms": row.get("ttft_ms"),
                "elapsed_ms": row.get("elapsed_ms"),
                "phase_elapsed_ms": row.get("phase_elapsed_ms"),
                "tier": row.get("tier"),
            }
        )
    return matched


def collect_session_telemetry(
    session: Mapping[str, Any],
    *,
    db_path: Path | None = None,
    inflight_data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Join ``requests.db`` (read-only) and ``inflight_report()`` for one RC session."""

    rc_session_id = str(session.get("id") or "").strip()
    ext_session_id = str(session.get("external_session_id") or "").strip()
    session_ids = [
        item for item in dict.fromkeys([rc_session_id, ext_session_id]) if item
    ]
    session_id_set = set(session_ids)
    cwd_exact, cwd_normed = _cwd_variants(
        str(session.get("cwd") or ""),
        str(session.get("worktree_path") or ""),
    )

    live_inflight_raw = (
        inflight_data if inflight_data is not None else inflight_report()
    )
    inflight = _matching_inflight(
        live_inflight_raw,
        session_ids=session_id_set,
        cwd_normed=cwd_normed,
    )

    empty: dict[str, Any] = {
        "session_id": rc_session_id,
        "request_count": 0,
        "error_count": 0,
        "total_cost_usd": 0.0,
        "reported_cost_usd": None,
        "estimated_cost_usd": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "last_request": None,
        "recent_requests": [],
        "inflight": inflight,
    }

    target_db = db_path if db_path is not None else request_log_path()
    conn = _open_sqlite_readonly(target_db)
    if conn is None:
        return empty

    try:
        cols = _table_columns(conn, "requests")
        if not cols or "id" not in cols or "ts_epoch" not in cols:
            return empty

        has_session = "session_id" in cols
        has_project = "project_dir" in cols

        clauses: list[str] = []
        args: list[Any] = []
        if has_session and session_ids:
            placeholders = ", ".join("?" * len(session_ids))
            clauses.append(f"session_id IN ({placeholders})")
            args.extend(session_ids)
        if has_project and cwd_exact:
            cwd_placeholders = ", ".join("?" * len(cwd_exact))
            if has_session:
                # Match by folder only when the request carried no session_id,
                # preventing cross-session cost bleed in shared workspaces.
                clauses.append(
                    f"((session_id IS NULL OR session_id = '') AND project_dir IN ({cwd_placeholders}))"
                )
            else:
                clauses.append(f"project_dir IN ({cwd_placeholders})")
            args.extend(cwd_exact)

        if not clauses:
            return empty

        where_sql = " WHERE " + " OR ".join(clauses)

        optional_cols = (
            "endpoint",
            "protocol",
            "requested_model",
            "provider",
            "resolved_model",
            "route_attempt",
            "tokens_in",
            "tokens_out",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "ttft_ms",
            "duration_ms",
            "status",
            "status_code",
            "error_kind",
            "error_message",
            "cost_usd",
            "estimated_cost_usd",
            "cost_source",
            "session_id",
            "project_dir",
            "params",
        )
        select_exprs = ["id", "ts_epoch"]
        for col in optional_cols:
            if col in cols:
                select_exprs.append(col)
            else:
                select_exprs.append(f"NULL AS {col}")

        rows = conn.execute(
            f"SELECT {', '.join(select_exprs)} FROM requests{where_sql} "
            "ORDER BY ts_epoch DESC, rowid DESC",
            args,
        ).fetchall()

        if not rows:
            return empty

        request_count = 0
        error_count = 0
        total_cost = 0.0
        reported_sum = 0.0
        reported_count = 0
        estimated_sum = 0.0
        estimated_count = 0
        input_tokens = 0
        output_tokens = 0
        cached_tokens = 0
        cache_write_tokens = 0
        reasoning_tokens = 0

        recent_raw: list[dict[str, Any]] = []
        for row in rows:
            row_map = dict(row)
            request_count += 1
            status_str = str(row_map.get("status") or "").strip().lower()
            status_code = _opt_int(row_map.get("status_code"))
            if status_str == "error" or (
                status_code is not None and status_code >= 400
            ):
                error_count += 1

            reported, estimated, effective = _row_cost_split(row_map)
            total_cost += effective
            if reported is not None:
                reported_sum += reported
                reported_count += 1
            if estimated is not None:
                estimated_sum += estimated
                estimated_count += 1

            input_tokens += _opt_int(row_map.get("tokens_in")) or 0
            output_tokens += _opt_int(row_map.get("tokens_out")) or 0
            cached_tokens += _opt_int(row_map.get("cache_read_tokens")) or 0
            cache_write_tokens += _opt_int(row_map.get("cache_write_tokens")) or 0
            reasoning_tokens += _opt_int(row_map.get("reasoning_tokens")) or 0

            if len(recent_raw) < _RECENT_REQUEST_LIMIT:
                recent_raw.append(row_map)

        recent_ids = [str(r["id"]) for r in recent_raw]
        attempt_surfaces = _fetch_attempt_surfaces(conn, recent_ids)

        recent_requests: list[dict[str, Any]] = []
        for row_map in recent_raw:
            req_id = str(row_map["id"])
            surfaces = attempt_surfaces.get(req_id, [])
            if not surfaces:
                row_wire = _extract_wire_surface(row_map.get("params"))
                if row_wire:
                    surfaces = [
                        {
                            "attempt": row_map.get("route_attempt") or 0,
                            "provider": row_map.get("provider"),
                            "model_ref": row_map.get("resolved_model")
                            or row_map.get("requested_model")
                            or "",
                            "outcome": (
                                "succeeded"
                                if str(row_map.get("status") or "").lower() == "success"
                                else "failed"
                            ),
                            "wire_surface": row_wire,
                        }
                    ]
            routing = request_endpoint_routing(row_map, attempt_surfaces=surfaces)
            inbound = routing.get("harness_endpoint") or {}
            outbound = routing.get("provider_endpoint")
            reported, estimated, _effective = _row_cost_split(row_map)
            raw_status_code = _opt_int(row_map.get("status_code"))
            status_str = str(row_map.get("status") or "")
            if raw_status_code is None and status_str == "success":
                raw_status_code = 200
            err_msg = str(row_map.get("error_message") or "").strip()
            recent_requests.append(
                {
                    "request_id": req_id,
                    "ts_epoch": _opt_float(row_map.get("ts_epoch")),
                    "status": status_str,
                    "status_code": raw_status_code,
                    "model": str(
                        row_map.get("resolved_model")
                        or row_map.get("requested_model")
                        or ""
                    ),
                    "provider_id": str(row_map.get("provider") or ""),
                    "harness_endpoint": str(
                        inbound.get("short_label") or inbound.get("label") or ""
                    ),
                    "provider_endpoint": (
                        str(outbound.get("short_label") or outbound.get("label") or "")
                        if isinstance(outbound, Mapping)
                        else None
                    ),
                    "translation_label": str(routing.get("translation_label") or ""),
                    "short_translation": str(routing.get("short_translation") or ""),
                    "translated": routing.get("translated"),
                    "attempt_routes": routing.get("attempt_routes") or [],
                    "ttft_ms": _opt_float(row_map.get("ttft_ms")),
                    "total_ms": _opt_float(row_map.get("duration_ms")),
                    "cost_usd": _opt_float(row_map.get("cost_usd")),
                    "reported_cost_usd": reported,
                    "estimated_cost_usd": estimated,
                    "cost_source": row_map.get("cost_source"),
                    "tokens_in": _opt_int(row_map.get("tokens_in")),
                    "tokens_out": _opt_int(row_map.get("tokens_out")),
                    "cached_tokens": _opt_int(row_map.get("cache_read_tokens")),
                    "reasoning_tokens": _opt_int(row_map.get("reasoning_tokens")),
                    "error_preview": err_msg[:200] if err_msg else "",
                }
            )

        return {
            "session_id": rc_session_id,
            "request_count": request_count,
            "error_count": error_count,
            "total_cost_usd": round(total_cost, 8),
            "reported_cost_usd": (
                round(reported_sum, 8) if reported_count > 0 else None
            ),
            "estimated_cost_usd": (
                round(estimated_sum, 8) if estimated_count > 0 else None
            ),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "cache_write_tokens": cache_write_tokens,
            "reasoning_tokens": reasoning_tokens,
            "last_request": recent_requests[0] if recent_requests else None,
            "recent_requests": recent_requests,
            "inflight": inflight,
        }
    except sqlite3.Error:
        return empty
    finally:
        conn.close()
