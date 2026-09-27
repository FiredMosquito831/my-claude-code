"""Calls on an accepted video job, pinned to the key and leg that accepted it.

A video job exists only on the provider, account and key that accepted it:
another key cannot read it, another model never had it. So a read of the job
(retrieve, content, delete) goes out through that key's own leaf -- its own
client, limiter and proxy leg -- one upstream call per client call, and never
rotates or falls back.

It is charged like a chat attempt on that key (review M7): a failure is
reported on the pool's key books (``report_failure`` -- a 401/403 lockout, a
429 bench, exhausted credits: the same charging an attempt gets) and on the
media route-health books; a success clears them. What differs, by design:

* nothing moves -- a benched key is still asked about its own job (the host
  answers 429 itself if it must), and a failure is the answer;
* the proxy leg's books are not charged: the leg is the job's address, not a
  choice this call made (documented residual).
"""

import contextlib
from collections.abc import Mapping
from urllib.parse import urlsplit

import httpx

from my_claude_code.application.media.request import MediaDownload, MediaResponse
from my_claude_code.application.route_health import RouteHealthRegistry
from my_claude_code.config.media_surfaces import (
    MEDIA_OPERATION_VIDEO_DELETE,
    MediaSurface,
    job_path,
    media_url,
    surface_for,
)
from my_claude_code.core.failures import (
    ExecutionFailure,
    FailureKind,
    failure_kind,
    find_execution_failure,
)
from my_claude_code.providers.credential_rotation import CredentialRotationState
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.providers.http import maybe_await_aclose

from .leaf import MediaLeaf

#: Upstream response headers carried onto a buffered answer (never auth).
_KEPT_HEADERS = ("content-type", "x-request-id", "request-id")

#: What a finished file is served as when the host names no type.
DEFAULT_VIDEO_TYPE = "video/mp4"


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def same_host(url: str, base_url: str) -> bool:
    """Whether ``url`` is on the provider's own host (the only one given a key)."""

    host = _host(url)
    return bool(host) and host == _host(base_url)


class PinnedMediaClient:
    """One accepted job's calls, through the leaf of the key that accepted it."""

    def __init__(
        self,
        leaf: MediaLeaf,
        *,
        key_index: int,
        state: CredentialRotationState | None,
        health: RouteHealthRegistry,
    ) -> None:
        self._leaf = leaf
        self._key_index = key_index
        self._state = state
        self._health = health

    @property
    def key_index(self) -> int:
        return self._key_index

    @property
    def leaf(self) -> MediaLeaf:
        return self._leaf

    def _surface(self, operation: str) -> MediaSurface:
        surface = surface_for(self._leaf.surfaces, operation)
        if surface is None:
            raise ExecutionFailure(
                kind=FailureKind.INVALID_REQUEST,
                status_code=400,
                message=(f"{self._leaf.provider_id} declares no {operation} endpoint."),
                retryable=False,
            )
        return surface

    def _target(
        self,
        surface: MediaSurface,
        upstream_id: str,
        query: Mapping[str, str] | None,
    ) -> tuple[str, dict[str, str]]:
        url = media_url(self._leaf.config.base_url, job_path(surface.path, upstream_id))
        params = {
            name: value
            for name, value in (query or {}).items()
            if name in surface.query
        }
        return url, params

    async def call(
        self,
        operation: str,
        upstream_id: str,
        *,
        model: str,
        request_id: str,
        query: Mapping[str, str] | None = None,
    ) -> MediaResponse:
        surface = self._surface(operation)
        url, params = self._target(surface, upstream_id, query)
        method = "DELETE" if operation == MEDIA_OPERATION_VIDEO_DELETE else "GET"
        async with self._leaf.rate_limiter.concurrency_slot():
            response = await self._send(
                method,
                url,
                auth=True,
                stream=False,
                params=params,
                model=model,
                request_id=request_id,
            )
        return MediaResponse(
            status_code=response.status_code,
            content_type=response.headers.get("content-type", "application/json"),
            body=response.content,
            headers={
                name: response.headers[name]
                for name in _KEPT_HEADERS
                if name in response.headers
            },
        )

    async def download(
        self,
        *,
        operation: str | None,
        upstream_id: str,
        url: str | None,
        model: str,
        request_id: str,
        query: Mapping[str, str] | None = None,
    ) -> MediaDownload:
        if operation is not None:
            target, params = self._target(self._surface(operation), upstream_id, query)
            auth = True
        elif url and urlsplit(url).scheme in {"https", "http"}:
            # A key goes only to the provider's own host: a URL the host
            # handed out elsewhere (a storage bucket, a CDN) is fetched bare.
            target, params = url, {}
            auth = same_host(url, self._leaf.config.base_url)
        else:
            raise ExecutionFailure(
                kind=FailureKind.UPSTREAM,
                status_code=502,
                message=(
                    f"{self._leaf.provider_id} gave no address to fetch the video from."
                ),
                retryable=False,
            )
        stack = contextlib.AsyncExitStack()
        # The slot is held for the body's whole life, as a streamed attempt
        # holds it, and freed by ``close``.
        await stack.enter_async_context(self._leaf.rate_limiter.concurrency_slot())
        try:
            response = await self._send(
                "GET",
                target,
                auth=auth,
                stream=True,
                params=params,
                model=model,
                request_id=request_id,
            )
        except BaseException:
            await stack.aclose()
            raise
        chunks = response.aiter_bytes()
        # Unwound last-in first-out: the reader, the response, then the slot.
        stack.push_async_callback(response.aclose)
        stack.push_async_callback(maybe_await_aclose, chunks)
        return MediaDownload(
            status_code=response.status_code,
            content_type=response.headers.get("content-type") or DEFAULT_VIDEO_TYPE,
            chunks=chunks,
            close=stack.aclose,
        )

    async def _send(
        self,
        method: str,
        url: str,
        *,
        auth: bool,
        stream: bool,
        params: Mapping[str, str],
        model: str,
        request_id: str,
    ) -> httpx.Response:
        """One call up the attempt's own retry ladder, classified the same way."""
        leaf = self._leaf
        config = leaf.config
        limiter = leaf.rate_limiter
        try:
            response = await limiter.execute_with_retry(
                leaf.request, method, url, auth=auth, stream=stream, params=params
            )
        except Exception as error:
            failure = classify_provider_failure(
                error,
                provider_name=leaf.provider_id.upper(),
                read_timeout_s=config.http_read_timeout,
                request_id=request_id,
                mark_rate_limited=limiter.extend_reactive_block,
                cooldown=config.rate_limit_cooldown(),
                mark_rate_limited_enabled=not config.routes_around_model,
            )
            await self._charge(failure, model)
            raise failure from error
        if self._state is not None:
            await self._state.report_success(self._key_index)
        self._health.record_success(f"{leaf.provider_id}/{model}")
        return response

    async def _charge(self, failure: ExecutionFailure, model: str) -> None:
        """The key books and the model books, as an attempt's failure charges them."""
        if self._state is not None:
            await self._state.report_failure(self._key_index, failure, model=model)
        kind = failure_kind(failure)
        execution = find_execution_failure(failure)
        self._health.record_failure(
            f"{self._leaf.provider_id}/{model}",
            failure_kind=kind.value if kind is not None else None,
            status_code=None if execution is None else execution.status_code,
        )
