"""The request detail's raw diagnostics: one read route and its export (7.121.0).

``GET /admin/api/requests/raw`` answers three questions about one request,
chosen by its parameters, and never anything but JSON:

* no ``kind``: every kind's counts and capture state, from one read of each
  log family -- what the four boxes print while they are still collapsed;
* ``kind`` and ``view=raw``: one page of that kind's lines, verbatim, with the
  digest the next page is checked against;
* ``kind`` and ``view=normalized``: what the request log stored for it.

``GET /admin/api/requests/raw/export`` writes the same material to one file,
in the Analytics export's four formats, through the same reader and the same
renderers -- so a file holds exactly what the boxes showed, byte for byte, and
is masked exactly as they are: not at all. That is why both are loopback-only
behind the same guard as every other admin route, ``no-store`` by the admin
middleware, and declared before ``/admin/api/requests/{request_id}``, which
would otherwise read ``raw`` as a request id.
"""

import asyncio
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from my_claude_code.config.settings import Settings
from my_claude_code.core import export as export_engine
from my_claude_code.core.request_log import store_from_settings

from . import raw_diagnostics
from .admin_export_routes import stream_export
from .admin_routes import require_loopback_admin
from .dependencies import get_settings

router = APIRouter()

_VIEW_CHOICES = {
    "raw": [raw_diagnostics.VIEW_RAW],
    "normalized": [raw_diagnostics.VIEW_NORMALIZED],
    "both": [raw_diagnostics.VIEW_RAW, raw_diagnostics.VIEW_NORMALIZED],
}
_UNSAFE_FILENAME = re.compile(r"[^A-Za-z0-9_-]")


async def _request_row(settings: Settings, request_id: str) -> dict[str, Any] | None:
    store = store_from_settings(settings)
    if store is None:
        return None
    return await asyncio.to_thread(store.get_request, request_id)


def _kind_or_400(kind: str) -> str:
    if kind not in raw_diagnostics.KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown kind {kind!r}: use one of {', '.join(raw_diagnostics.RAW_KINDS)}",
        )
    return kind


@router.get("/admin/api/requests/raw")
async def request_raw_diagnostics(
    request: Request,
    request_id: str = Query(..., alias="id", min_length=1),
    kind: str | None = Query(
        None,
        description="payloads | sse | traceback | cli; absent = all four, counts only",
    ),
    view: str = Query("raw", description="raw | normalized"),
    start: int = Query(0, ge=0, description="0-based index of the first line"),
    anchor: str | None = Query(
        None, description="the anchor the previous page returned"
    ),
    settings: Settings = Depends(get_settings),
):
    """One request's raw diagnostics: a summary, a page of lines, or the stored form."""

    require_loopback_admin(request)
    if view not in raw_diagnostics.VIEWS:
        raise HTTPException(status_code=400, detail="view must be raw or normalized")
    if kind is not None:
        _kind_or_400(kind)
    elif view == raw_diagnostics.VIEW_NORMALIZED:
        raise HTTPException(status_code=400, detail="view=normalized needs a kind")
    flags = raw_diagnostics.raw_settings_from(settings)
    row = await _request_row(settings, request_id)
    if kind is not None and view == raw_diagnostics.VIEW_NORMALIZED:
        return raw_diagnostics.normalized_view(request_id, kind, row, flags)
    sources = raw_diagnostics.current_sources()
    if kind is None:
        return await asyncio.to_thread(
            raw_diagnostics.raw_summary, sources, request_id, row, flags
        )
    return await asyncio.to_thread(
        raw_diagnostics.raw_page,
        sources,
        request_id,
        kind,
        row,
        flags,
        start=start,
        anchor=anchor,
    )


def raw_export_filename(
    request_id: str, fmt: export_engine.Format, exported_at: str
) -> str:
    """``mcc-request-<id>-<format>-<time>.<ext>``, safe in a header and on any disk."""

    safe_id = _UNSAFE_FILENAME.sub("_", request_id)[:80] or "request"
    safe_time = exported_at.replace(":", "-").replace(".", "-")
    return (
        f"mcc-request-{safe_id}-{fmt}-{safe_time}.{export_engine.file_extension(fmt)}"
    )


@router.get("/admin/api/requests/raw/export")
async def export_request_raw_diagnostics(
    request: Request,
    request_id: str = Query(..., alias="id", min_length=1),
    format: str = Query(..., description="json | csv | xlsx | txt"),
    kinds: str | None = Query(
        None, description="comma-separated kinds; absent = all four"
    ),
    view: str = Query("both", description="raw | normalized | both"),
    settings: Settings = Depends(get_settings),
):
    """One request's raw and/or normalised material as one file.

    Read in full before the first byte is sent: a limit that cannot be met --
    a line too large to hold, XLSX's cell size, a disk with no room for the
    copy -- answers with a status and a sentence instead of a shorter file.
    """

    require_loopback_admin(request)
    try:
        fmt = export_engine.validate_format(format)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    views = _VIEW_CHOICES.get(view)
    if views is None:
        raise HTTPException(
            status_code=400, detail="view must be raw, normalized or both"
        )
    asked = (
        [part.strip() for part in kinds.split(",") if part.strip()]
        if kinds is not None
        else list(raw_diagnostics.RAW_KINDS)
    )
    for kind in asked:
        _kind_or_400(kind)
    selected = [kind for kind in raw_diagnostics.RAW_KINDS if kind in asked]
    if not selected:
        raise HTTPException(
            status_code=400, detail="choose at least one kind to export"
        )

    flags = raw_diagnostics.raw_settings_from(settings)
    row = await _request_row(settings, request_id)
    try:
        plan = await asyncio.to_thread(
            raw_diagnostics.prepare_request_export,
            raw_diagnostics.current_sources(),
            request_id,
            row,
            flags,
            kinds=selected,
            views=views,
        )
    except raw_diagnostics.RawScanRefused as refusal:
        raise HTTPException(
            status_code=refusal.status, detail=refusal.message
        ) from None

    columns = raw_diagnostics.EXPORT_COLUMNS
    headers = raw_diagnostics.EXPORT_HEADERS
    widths: list[int] | None = None
    try:
        if fmt == "xlsx":
            longest, where = plan.max_cell_chars()
            raw_diagnostics.check_xlsx_cells(longest, where)
        elif fmt == "txt":
            widths = await asyncio.to_thread(
                export_engine.txt_column_widths, plan.rows(), columns, headers
            )
    except raw_diagnostics.RawScanRefused as refusal:
        plan.close()
        raise HTTPException(
            status_code=refusal.status, detail=refusal.message
        ) from None
    except BaseException:
        plan.close()
        raise

    def rows() -> Iterator[dict[str, Any]]:
        try:
            yield from plan.rows()
        finally:
            plan.close()

    exported_at = datetime.now(UTC).isoformat()
    filename = raw_export_filename(request_id, fmt, exported_at)
    return stream_export(
        fmt, rows(), columns, headers, filename, exported_at, txt_widths=widths
    )
