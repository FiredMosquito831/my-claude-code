"""Admin export endpoint: stream analytics in JSON / CSV / XLSX / TXT.

Loopback-guarded like the other admin routes. The endpoint streams the entire
matching row set (bypassing the 500-row page cap) as a ``StreamingResponse``
fed by a synchronous generator. Starlette iterates that generator in a worker
threadpool, so SQLite work stays off the event loop and a large export never
loads the whole table into memory.
"""

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from my_claude_code.api.admin_websearch_routes import get_websearch_log_store
from my_claude_code.config.settings import Settings
from my_claude_code.core import export as export_engine
from my_claude_code.core.request_log import RequestLogStore, store_from_settings
from my_claude_code.websearch.analytics import WebSearchLogStore

from .admin_routes import require_loopback_admin
from .dependencies import get_settings

router = APIRouter()

_WEBSEARCH_BODY_FIELDS = frozenset({"input", "output", "provider_config"})


def _parse_bound(value: str | None, name: str) -> float | None:
    """Accept an ISO timestamp or an epoch-seconds float for since/until."""
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"invalid {name} timestamp: {value!r}",
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _request_store(settings: Settings) -> RequestLogStore | None:
    return store_from_settings(settings)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _disabled_response() -> StreamingResponse:
    return StreamingResponse(
        export_engine.render_json_object({"enabled": False}),
        media_type=export_engine.media_type("json"),
    )


def _render(
    fmt: export_engine.Format,
    rows: Iterator[dict[str, Any]],
    columns: list[str],
    headers: list[str],
    filename: str,
    exported_at: str,
) -> Iterator[bytes]:
    if fmt == "json":
        return export_engine.render_json_array(rows)
    if fmt == "csv":
        return export_engine.render_csv(rows, columns, headers)
    if fmt == "xlsx":
        return export_engine.render_xlsx(rows, columns, headers)
    return export_engine.render_txt(
        rows,
        columns,
        headers,
        title=f"Export {filename}",
        summary=f"Exported {exported_at}",
    )


def _stream(
    fmt: export_engine.Format,
    rows: Iterator[dict[str, Any]],
    columns: list[str],
    headers: list[str],
    filename: str,
    exported_at: str,
) -> StreamingResponse:
    body = _render(fmt, rows, columns, headers, filename, exported_at)
    return StreamingResponse(
        body,
        media_type=export_engine.media_type(fmt),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/admin/api/export")
async def export_analytics(
    request: Request,
    format: str = Query(..., description="json | csv | xlsx | txt"),
    scope: str = Query(..., description="requests | websearch"),
    fields: str | None = Query(None, description="comma-separated field ids"),
    group_by: str | None = Query(
        None, description="comma-separated group dimensions, in output order"
    ),
    since: str | None = Query(None, description="ISO timestamp or epoch seconds"),
    until: str | None = Query(None, description="ISO timestamp or epoch seconds"),
    provider: str | None = Query(None),
    model: str | None = Query(None),
    status: str | None = Query(None),
    endpoint: str | None = Query(None),
    key: str | None = Query(None),
    q: str | None = Query(None),
    settings: Settings = Depends(get_settings),
    websearch_store: WebSearchLogStore = Depends(get_websearch_log_store),
):
    """Stream an analytics export in the requested format and scope.

    ``group_by`` empty (or absent) produces a flat detail export; otherwise the
    server groups with the requested dimensions in order.
    """
    require_loopback_admin(request)
    try:
        fmt = export_engine.validate_format(format)
        scp = export_engine.validate_scope(scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    since_epoch = _parse_bound(since, "since")
    until_epoch = _parse_bound(until, "until")
    group_by_list = _split_csv(group_by)

    if scp == export_engine.REQUEST_SCOPE:
        return _request_export(
            fmt,
            fields,
            group_by_list,
            since_epoch=since_epoch,
            until_epoch=until_epoch,
            provider=provider,
            model=model,
            status=status,
            endpoint=endpoint,
            key=key,
            q=q,
            settings=settings,
        )
    return _websearch_export(
        fmt,
        fields,
        group_by_list,
        since_epoch=since_epoch,
        until_epoch=until_epoch,
        provider=provider,
        status=status,
        q=q,
        store=websearch_store,
    )


def _request_export(
    fmt: export_engine.Format,
    fields: str | None,
    group_by_list: list[str],
    *,
    since_epoch: float | None,
    until_epoch: float | None,
    provider: str | None,
    model: str | None,
    status: str | None,
    endpoint: str | None,
    key: str | None,
    q: str | None,
    settings: Settings,
) -> StreamingResponse:
    store = _request_store(settings)
    if store is None:
        return _disabled_response()
    if status is not None and status not in {"success", "error", "cancelled"}:
        raise HTTPException(status_code=422, detail="Invalid status filter")
    selected = (
        _split_csv(fields)
        if fields is not None
        else list(export_engine.DEFAULT_REQUEST_FIELDS)
    )
    try:
        export_engine.validate_fields(export_engine.REQUEST_SCOPE, selected)
        dims = export_engine.validate_group_by(
            export_engine.REQUEST_SCOPE, group_by_list
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    exported_at = datetime.now(UTC).isoformat()
    filename = export_engine.export_filename(
        export_engine.REQUEST_SCOPE, fmt, exported_at
    )

    if dims:
        select, names = export_engine.request_aggregate_sql(selected, dims)
        derived = export_engine.request_aggregate_derived_columns(selected)
        output_columns = names + [column for column in derived if column not in names]
        iterator = store.iter_export_aggregates(
            select=select,
            names=names,
            group_by=dims,
            provider=provider,
            model=model,
            status=status,
            endpoint=endpoint,
            key=key,
            since=since_epoch,
            until=until_epoch,
            q=q,
        )

        def agg_rows() -> Iterator[dict[str, Any]]:
            for row in iterator:
                export_engine.compute_request_aggregate_derived(row, selected)
                yield {column: row.get(column) for column in output_columns}

        headers = output_columns
        return _stream(fmt, agg_rows(), headers, headers, filename, exported_at)

    sql_columns = export_engine.request_detail_columns(selected)
    derived_columns = export_engine.request_detail_derived_columns(selected)
    output_columns = sql_columns + derived_columns
    headers = export_engine.request_detail_headers(output_columns)
    need_bodies = export_engine.requires_request_bodies(selected)
    iterator = store.iter_export_rows(
        columns=sql_columns,
        need_bodies=need_bodies,
        # The ladder columns come from ``request_attempts``, which no export
        # SQL joins; the store fills them per page when they were asked for.
        need_ladder="ladder" in selected,
        provider=provider,
        model=model,
        status=status,
        endpoint=endpoint,
        key=key,
        since=since_epoch,
        until=until_epoch,
        q=q,
    )

    def detail_rows() -> Iterator[dict[str, Any]]:
        for row in iterator:
            export_engine.compute_request_detail_derived(row, selected)
            yield {column: row.get(column) for column in output_columns}

    return _stream(fmt, detail_rows(), output_columns, headers, filename, exported_at)


def _websearch_export(
    fmt: export_engine.Format,
    fields: str | None,
    group_by_list: list[str],
    *,
    since_epoch: float | None,
    until_epoch: float | None,
    provider: str | None,
    status: str | None,
    q: str | None,
    store: WebSearchLogStore,
) -> StreamingResponse:
    if store is None:
        return _disabled_response()
    selected = (
        _split_csv(fields)
        if fields is not None
        else list(export_engine.DEFAULT_WEBSEARCH_FIELDS)
    )
    try:
        export_engine.validate_fields(export_engine.WEBSEARCH_SCOPE, selected)
        dims = export_engine.validate_group_by(
            export_engine.WEBSEARCH_SCOPE, group_by_list
        )
        detail_only = export_engine.websearch_detail_only_fields(selected)
        if dims and detail_only:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"fields {detail_only!r} cannot be aggregated; drop them "
                    "or clear Group by"
                ),
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None

    exported_at = datetime.now(UTC).isoformat()
    filename = export_engine.export_filename(
        export_engine.WEBSEARCH_SCOPE, fmt, exported_at
    )

    if dims:
        select, names = export_engine.websearch_aggregate_sql(selected, dims)
        derived = export_engine.websearch_aggregate_derived_columns(selected)
        output_columns = names + [column for column in derived if column not in names]
        iterator = store.iter_export_aggregates(
            select=select,
            names=names,
            group_by=dims,
            provider=provider,
            status=status,
            q=q,
            since_epoch=since_epoch,
            until_epoch=until_epoch,
        )

        def agg_rows() -> Iterator[dict[str, Any]]:
            for row in iterator:
                export_engine.compute_websearch_aggregate_derived(row, selected)
                yield {column: row.get(column) for column in output_columns}

        return _stream(
            fmt, agg_rows(), output_columns, output_columns, filename, exported_at
        )

    columns = export_engine.websearch_detail_columns(selected)
    headers = export_engine.websearch_detail_headers(columns)
    include_content = bool(_WEBSEARCH_BODY_FIELDS.intersection(selected))
    iterator = store.iter_export_rows(
        columns=columns,
        include_content=include_content,
        provider=provider,
        status=status,
        q=q,
        since_epoch=since_epoch,
        until_epoch=until_epoch,
    )

    def detail_rows() -> Iterator[dict[str, Any]]:
        for row in iterator:
            yield {column: row.get(column) for column in columns}

    return _stream(fmt, detail_rows(), columns, headers, filename, exported_at)
