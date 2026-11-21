"""Web search usage analytics admin API (loopback-only, like the admin UI)."""

import asyncio
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from my_claude_code.websearch.analytics import WebSearchLogStore, get_shared_store

from .admin_routes import require_loopback_admin

router = APIRouter()

_MAX_LIMIT = 500

Period = Literal["hourly", "daily", "weekly", "monthly"]
AttemptStatus = Literal["success", "error", "skipped"]
ToolKindFilter = Literal["all", "web_search", "web_fetch", "search_docs"]


def get_websearch_log_store() -> WebSearchLogStore:
    """Resolve the shared analytics store (dependency-overridable in tests)."""

    return get_shared_store()


@router.get("/admin/api/websearch/stats")
async def websearch_stats(
    request: Request,
    period: Period = "weekly",
    provider: str | None = None,
    status: AttemptStatus | None = None,
    door: str | None = None,
    tool_kind: ToolKindFilter | None = None,
    route_id: str | None = None,
    empty_only: bool = False,
    q: str | None = None,
    since: str | None = None,
    until: str | None = None,
    store: WebSearchLogStore = Depends(get_websearch_log_store),
) -> dict[str, Any]:
    """Filtered rollups using hourly, daily, ISO-weekly, or monthly buckets."""

    require_loopback_admin(request)
    # SQLite work is synchronous; keep it off the event loop.
    return await asyncio.to_thread(
        store.stats,
        period,
        provider=provider,
        status=status,
        door=door,
        tool_kind=tool_kind,
        route_id=route_id,
        empty_only=empty_only,
        q=q,
        since_epoch=_parse_iso_bound(since, "since"),
        until_epoch=_parse_iso_bound(until, "until"),
    )


@router.get("/admin/api/websearch/requests")
async def websearch_requests(
    request: Request,
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    provider: str | None = None,
    status: AttemptStatus | None = None,
    door: str | None = None,
    tool_kind: ToolKindFilter | None = None,
    route_id: str | None = None,
    empty_only: bool = False,
    q: str | None = None,
    since: str | None = None,
    until: str | None = None,
    include_content: bool = False,
    store: WebSearchLogStore = Depends(get_websearch_log_store),
) -> dict[str, Any]:
    """Paged request log (newest first) with provider/status/door/tool/route/text/date filters."""

    require_loopback_admin(request)
    return await asyncio.to_thread(
        store.requests,
        limit=limit,
        offset=offset,
        provider=provider,
        status=status,
        door=door,
        tool_kind=tool_kind,
        route_id=route_id,
        empty_only=empty_only,
        q=q,
        since_epoch=_parse_iso_bound(since, "since"),
        until_epoch=_parse_iso_bound(until, "until"),
        include_content=include_content,
    )


@router.get("/admin/api/websearch/routes")
async def websearch_routes(
    request: Request,
    limit: int = Query(50, ge=1, le=_MAX_LIMIT),
    offset: int = Query(0, ge=0),
    provider: str | None = None,
    status: Literal["success", "error"] | None = None,
    door: str | None = None,
    q: str | None = None,
    since: str | None = None,
    until: str | None = None,
    fallback_only: bool = False,
    empty_only: bool = False,
    store: WebSearchLogStore = Depends(get_websearch_log_store),
) -> dict[str, Any]:
    """Paged logical search routes (newest first) with resilience filters."""

    require_loopback_admin(request)
    return await asyncio.to_thread(
        store.routes,
        limit=limit,
        offset=offset,
        provider=provider,
        status=status,
        door=door,
        q=q,
        since_epoch=_parse_iso_bound(since, "since"),
        until_epoch=_parse_iso_bound(until, "until"),
        fallback_only=fallback_only,
        empty_only=empty_only,
    )


@router.get("/admin/api/websearch/requests/{request_id}")
async def websearch_request_detail(
    request: Request,
    request_id: int,
    store: WebSearchLogStore = Depends(get_websearch_log_store),
) -> dict[str, Any]:
    """One provider attempt with captured input/output and config snapshot."""

    require_loopback_admin(request)
    item = await asyncio.to_thread(store.request, request_id)
    if item is None:
        raise HTTPException(status_code=404, detail="web search request not found")
    return item


@router.delete("/admin/api/websearch/requests")
async def websearch_requests_clear(
    request: Request,
    store: WebSearchLogStore = Depends(get_websearch_log_store),
) -> dict[str, Any]:
    """Delete every recorded web search request."""

    require_loopback_admin(request)
    deleted = await asyncio.to_thread(store.clear)
    return {"cleared": True, "deleted": deleted}


def _parse_iso_bound(value: str | None, name: str) -> float | None:
    if value is None or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"invalid {name} ISO timestamp: {value!r}",
        ) from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()
