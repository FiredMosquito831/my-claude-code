"""One media call on one credential through one address.

The media counterpart of a chat leaf provider: an ``httpx.AsyncClient`` bound to
one key and one proxy leg, with a :class:`ProviderRateLimiter` of its own built
from the same provider configuration a chat leaf gets. Its retry ladder is the
frozen limiter's ``execute_with_retry`` (5xx and transport faults retried on the
same key, a 429 re-raised at once when ``RATE_LIMIT_ROUTES_AROUND_MODEL`` is on),
and whatever survives the ladder is classified by the frozen
``classify_provider_failure`` -- so the pools above read exactly the failure a
chat leaf would have raised for the same answer.

The limiter is a fresh instance, never the chat leaf's: media owns its own
books (user decision 2026-09-26 03:38 #4).
"""

import json
from collections.abc import AsyncIterator
from typing import Protocol

import httpx

from my_claude_code.application.media.ports import MediaProviderPort
from my_claude_code.application.media.request import (
    MediaAttempt,
    MediaChunk,
    MediaRequest,
    MediaResponse,
)
from my_claude_code.config.credentials import mask_key_label
from my_claude_code.config.media_surfaces import (
    MediaSurface,
    media_url,
    surface_for,
)
from my_claude_code.core.upstream_ladder import note_response_head
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.providers.http import error_response_headers, read_error_body
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.socks_deadline import bound_socks_handshake

from .adapters import WireBody, build_request_body

#: Upstream response headers carried onto a buffered media response. Only what
#: the client or the log can use; never anything that could carry a secret.
_KEPT_HEADERS = ("content-type", "x-request-id", "request-id")


class MediaNode(MediaProviderPort, Protocol):
    """A media provider the registry can close."""

    async def cleanup(self) -> None: ...


class MediaLeaf:
    """One key, one address, one client."""

    def __init__(
        self,
        *,
        provider_id: str,
        config: ProviderConfig,
        surfaces: tuple[MediaSurface, ...],
        rate_limiter: ProviderRateLimiter,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._config = config
        self._surfaces = surfaces
        self._rate_limiter = rate_limiter
        timeout = httpx.Timeout(
            config.http_read_timeout,
            connect=config.http_connect_timeout,
            read=config.http_read_timeout,
            write=config.http_write_timeout,
        )
        if transport is not None:
            # Tests hand a MockTransport; a real leaf always dials.
            self._client = httpx.AsyncClient(transport=transport, timeout=timeout)
        else:
            self._client = bound_socks_handshake(
                httpx.AsyncClient(proxy=config.proxy or None, timeout=timeout)
            )

    @property
    def credential_label(self) -> str | None:
        key = self._config.api_key
        return mask_key_label(key) if key else None

    def throttle_remaining(self, model: str | None = None) -> float:
        return self._rate_limiter.remaining_wait()

    def _surface(self, request: MediaRequest) -> MediaSurface | None:
        return surface_for(self._surfaces, request.operation)

    def supports(self, request: MediaRequest) -> bool:
        surface = self._surface(request)
        if surface is None:
            return False
        return surface.stream or not request.stream

    def preflight(self, attempt: MediaAttempt) -> None:
        """Nothing to validate before sending; the upstream judges the body."""

    async def cleanup(self) -> None:
        await self._client.aclose()

    def execute(
        self, attempt: MediaAttempt, *, request_id: str | None = None
    ) -> AsyncIterator[MediaChunk]:
        return self._execute(attempt, request_id=request_id)

    async def _send(self, url: str, body: WireBody, stream: bool) -> httpx.Response:
        """POST one body; a refusal is read whole and raised as HTTPStatusError.

        The same shape the Responses transport uses, so ``classify_provider_failure``
        and every error matcher read the host's own words and real status.
        """
        headers: dict[str, str] = {}
        if self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        if body.multipart is not None:
            headers["Content-Type"] = body.multipart.content_type
            headers["Content-Length"] = str(body.multipart.content_length)
            request = self._client.build_request(
                "POST", url, headers=headers, content=body.multipart.stream()
            )
        else:
            headers["Content-Type"] = "application/json"
            request = self._client.build_request(
                "POST",
                url,
                headers=headers,
                content=json.dumps(body.json or {}).encode(),
            )
        response = await self._client.send(request, stream=stream)
        if response.status_code >= 400:
            error = await read_error_body(response)
            await response.aclose()
            note_response_head(error.head)
            raise httpx.HTTPStatusError(
                f"{self._provider_id} media API error {response.status_code}",
                request=request,
                response=httpx.Response(
                    response.status_code,
                    headers=error_response_headers(response.headers),
                    content=error.content,
                    request=request,
                ),
            )
        if not stream:
            await response.aread()
        return response

    async def _execute(
        self, attempt: MediaAttempt, *, request_id: str | None
    ) -> AsyncIterator[MediaChunk]:
        surface = self._surface(attempt.request)
        if surface is None:  # pragma: no cover - the gate runs first
            raise RuntimeError(
                f"{self._provider_id} declares no {attempt.request.operation} surface"
            )
        url = media_url(self._config.base_url, surface.path)
        body = build_request_body(surface, attempt)
        stream = bool(attempt.request.stream)
        async with self._rate_limiter.concurrency_slot():
            try:
                response = await self._rate_limiter.execute_with_retry(
                    self._send, url, body, stream
                )
            except Exception as error:
                raise classify_provider_failure(
                    error,
                    provider_name=self._provider_id.upper(),
                    read_timeout_s=self._config.http_read_timeout,
                    request_id=request_id,
                    mark_rate_limited=self._rate_limiter.extend_reactive_block,
                    cooldown=self._config.rate_limit_cooldown(),
                    mark_rate_limited_enabled=not self._config.routes_around_model,
                ) from error
            if not stream:
                yield MediaResponse(
                    status_code=response.status_code,
                    content_type=response.headers.get(
                        "content-type", "application/json"
                    ),
                    body=response.content,
                    headers={
                        name: response.headers[name]
                        for name in _KEPT_HEADERS
                        if name in response.headers
                    },
                )
                return
            try:
                async for raw in response.aiter_bytes():
                    if raw:
                        yield raw
            finally:
                await response.aclose()
