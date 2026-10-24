"""Shared httpx helpers for web search adapters.

Owns the async client factory (timeout, proxy), JSON request execution, and
consistent HTTP status -> :mod:`..errors` mapping. Adapters pass
``extra_status_errors`` for provider-specific statuses (e.g. Tavily 432).
"""

import json
from collections.abc import Mapping
from typing import Any

import httpx

from my_claude_code.core.rate_limit import retry_after_seconds

from ..errors import (
    KIND_DETAIL_BAD_JSON,
    KIND_DETAIL_TIMEOUT,
    KIND_DETAIL_TRANSPORT,
    WebSearchAuthError,
    WebSearchError,
    WebSearchInvalidRequestError,
    WebSearchQuotaError,
    WebSearchRateLimitError,
    WebSearchUpstreamError,
)

DEFAULT_HTTP_TIMEOUT = 20.0
_MAX_ERROR_MESSAGE_CHARS = 300


def build_async_client(
    *,
    proxy: str | None = None,
    http_timeout: float = DEFAULT_HTTP_TIMEOUT,
) -> httpx.AsyncClient:
    """Create the adapter HTTP client honoring timeout and optional proxy."""

    return httpx.AsyncClient(
        timeout=httpx.Timeout(http_timeout),
        proxy=proxy,
        follow_redirects=True,
    )


def extract_error_message(payload: Any, *, fallback: str = "") -> str:
    """Pull a short human message out of common provider error body shapes."""

    message = _extract_message(payload)
    if not message:
        message = fallback
    message = " ".join(str(message).split())
    if len(message) > _MAX_ERROR_MESSAGE_CHARS:
        return f"{message[:_MAX_ERROR_MESSAGE_CHARS]}…"
    return message


def map_status_error(
    provider_id: str,
    response: httpx.Response,
    *,
    extra_status_errors: Mapping[int, type[WebSearchError]] | None = None,
) -> WebSearchError:
    """Map an HTTP error response to the websearch error hierarchy."""

    status = response.status_code
    parsed = _try_parse_json(response)
    message = extract_error_message(parsed, fallback=response.text)
    if not message:
        message = f"HTTP {status}"
    # The body as the provider sent it, for the search chain's translation
    # (quota phrase, body-stated wait). Additive: nothing on the default route
    # reads it, and the message above is computed exactly as before.
    body = parsed if parsed is not None else response.text
    error_type = _error_type_for_status(status, extra_status_errors)
    if issubclass(error_type, WebSearchRateLimitError):
        # Providers publish the real reset on a 429; carrying it forward lets
        # the key pool cool down for exactly as long as it was told to.
        return error_type(
            provider_id,
            message,
            status_code=status,
            retry_after_seconds=retry_after_seconds(response.headers),
            upstream_body=body,
        )
    return error_type(provider_id, message, status_code=status, upstream_body=body)


async def request_json(
    client: httpx.AsyncClient,
    provider_id: str,
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, Any] | None = None,
    json_body: Any = None,
    extra_status_errors: Mapping[int, type[WebSearchError]] | None = None,
) -> Any:
    """Execute one JSON request; transport/HTTP failures become WebSearchError."""

    try:
        response = await client.request(
            method, url, headers=headers, params=params, json=json_body
        )
    except httpx.TimeoutException as exc:
        raise WebSearchUpstreamError(
            provider_id,
            f"request timed out ({type(exc).__name__})",
            kind_detail=KIND_DETAIL_TIMEOUT,
        ) from exc
    except httpx.TransportError as exc:
        raise WebSearchUpstreamError(
            provider_id,
            f"transport error ({type(exc).__name__})",
            kind_detail=KIND_DETAIL_TRANSPORT,
        ) from exc
    if response.status_code >= 400:
        raise map_status_error(
            provider_id, response, extra_status_errors=extra_status_errors
        )
    try:
        return response.json()
    except json.JSONDecodeError as exc:
        raise WebSearchUpstreamError(
            provider_id,
            "response body is not valid JSON",
            kind_detail=KIND_DETAIL_BAD_JSON,
        ) from exc


def _error_type_for_status(
    status: int,
    extra_status_errors: Mapping[int, type[WebSearchError]] | None,
) -> type[WebSearchError]:
    if extra_status_errors and status in extra_status_errors:
        return extra_status_errors[status]
    if status in (401, 403):
        return WebSearchAuthError
    if status == 402:
        return WebSearchQuotaError
    if status == 429:
        return WebSearchRateLimitError
    if status in (400, 404, 405, 422):
        return WebSearchInvalidRequestError
    return WebSearchUpstreamError


def _try_parse_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _extract_message(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, Mapping):
        return ""
    for key in ("error", "detail", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, Mapping):
            nested = _extract_message(value)
            if nested:
                return nested
    return ""
