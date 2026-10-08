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

A video create is accepted only when the host names the job: a 2xx without a
job id is raised as an upstream failure here, so the executor charges the
model and falls back exactly as for any other failed attempt.

A Gemini native answer (``generateContent``, 7.66.0) is translated here, off
the loop, before it is yielded: the audio the client named, or the transcript.
An answer with neither is raised the same way (charged, falls back).

An image request marked ``inline_urls`` (``MEDIA_FALLBACK_ON_UNDOWNLOADABLE``,
7.68.0) has its URL-only pictures downloaded here, before the answer is
yielded, and handed on as ``b64_json``. A picture that cannot be downloaded
is raised the same way: the model is charged and the next one is tried --
although the host has usually billed the picture it could not deliver.
"""

import asyncio
import dataclasses
import json
from collections.abc import AsyncIterator, Mapping
from typing import Protocol
from urllib.parse import urlsplit

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
    MEDIA_ENCODING_JSON,
    MEDIA_OPERATION_VIDEO_CREATE,
    MEDIA_SHAPE_GEMINI_TRANSCRIBE,
    MEDIA_SHAPE_GEMINI_TTS,
    MediaSurface,
    media_url,
    model_path,
    surface_for,
)
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.gemini_native_media import (
    GeminiAnswerError,
    speech_answer,
    transcript_answer,
)
from my_claude_code.core.openai_images import inline_image_urls, url_only_images
from my_claude_code.core.openai_videos import parse_job
from my_claude_code.core.upstream_ladder import note_response_head
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.providers.http import error_response_headers, read_error_body
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.socks_deadline import bound_socks_handshake

from .adapters import WireBody, build_wire_body

#: Upstream response headers carried onto a buffered media response. Only what
#: the client or the log can use; never anything that could carry a secret.
_KEPT_HEADERS = ("content-type", "x-request-id", "request-id")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def same_host(url: str, base_url: str) -> bool:
    """Whether ``url`` is on the provider's own host (the only one given a key)."""

    host = _host(url)
    return bool(host) and host == _host(base_url)


def surfaces_serve(surfaces: tuple[MediaSurface, ...], request: MediaRequest) -> bool:
    """Whether a provider declaring ``surfaces`` can serve ``request`` at all.

    :meth:`MediaLeaf.supports`, as a function of what was declared, so a node
    that never builds a leaf (the masked refusal) answers it identically.
    """

    surface = surface_for(surfaces, request.operation)
    if surface is None:
        return False
    if request.stream and not surface.stream:
        return False
    # A file cannot be sent where the host documents JSON only.
    if request.uploads and surface.encoding == MEDIA_ENCODING_JSON:
        return False
    # A format the client NAMED and the host does not document is a
    # format this host cannot produce: skipped uncharged, never
    # transcoded (user decision 9). No list = the host judges.
    named = request.body.get("response_format")
    if surface.formats is not None and isinstance(named, str) and named:
        return named in surface.formats
    return True


class MediaNode(MediaProviderPort, Protocol):
    """A media provider the registry can close."""

    async def cleanup(self) -> None: ...

    def leaf_for(self, key_index: int, proxy_label: str | None) -> MediaLeaf | None:
        """The leaf behind key ``key_index`` and proxy leg ``proxy_label``.

        What a pinned call on an accepted job goes out through; ``None`` when
        this node has no such key.
        """
        ...


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

    @property
    def provider_id(self) -> str:
        return self._provider_id

    @property
    def config(self) -> ProviderConfig:
        return self._config

    @property
    def surfaces(self) -> tuple[MediaSurface, ...]:
        return self._surfaces

    @property
    def rate_limiter(self) -> ProviderRateLimiter:
        return self._rate_limiter

    def leaf_for(self, key_index: int, proxy_label: str | None) -> MediaLeaf | None:
        """One key, one address: itself, for the only key it has."""
        return self if key_index == 0 else None

    def throttle_remaining(self, model: str | None = None) -> float:
        return self._rate_limiter.remaining_wait()

    def _surface(self, request: MediaRequest) -> MediaSurface | None:
        return surface_for(self._surfaces, request.operation)

    def supports(self, request: MediaRequest) -> bool:
        return surfaces_serve(self._surfaces, request)

    def preflight(self, attempt: MediaAttempt) -> None:
        """Nothing to validate before sending; the upstream judges the body."""

    async def cleanup(self) -> None:
        await self._client.aclose()

    def execute(
        self, attempt: MediaAttempt, *, request_id: str | None = None
    ) -> AsyncIterator[MediaChunk]:
        return self._execute(attempt, request_id=request_id)

    async def _send(
        self,
        url: str,
        body: WireBody,
        stream: bool,
        auth_header: str | None = None,
    ) -> httpx.Response:
        """POST one body; a refusal is read whole and raised as HTTPStatusError.

        The same shape the Responses transport uses, so ``classify_provider_failure``
        and every error matcher read the host's own words and real status.
        ``auth_header`` is the surface's declared key header (the bare key);
        ``None`` sends ``Authorization: Bearer``.
        """
        headers: dict[str, str] = {}
        if self._config.api_key:
            if auth_header is None:
                headers["Authorization"] = f"Bearer {self._config.api_key}"
            else:
                headers[auth_header] = self._config.api_key
        if body.multipart is not None:
            headers["Content-Type"] = body.multipart.content_type
            headers["Content-Length"] = str(body.multipart.content_length)
            request = self._client.build_request(
                "POST", url, headers=headers, content=body.multipart.stream()
            )
        elif body.encoded is not None:
            headers["Content-Type"] = "application/json"
            request = self._client.build_request(
                "POST", url, headers=headers, content=body.encoded
            )
        else:
            headers["Content-Type"] = "application/json"
            request = self._client.build_request(
                "POST",
                url,
                headers=headers,
                content=json.dumps(body.json or {}).encode(),
            )
        return await self._checked(request, stream)

    async def request(
        self,
        method: str,
        url: str,
        *,
        auth: bool,
        stream: bool,
        params: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """A body-less call (GET, DELETE) on a job; refusals read like ``_send``'s.

        ``auth`` is decided by the caller: a key is only ever sent to the
        provider's own host. A GET follows redirects (a finished video's
        address commonly redirects to storage); httpx drops the
        ``Authorization`` header when a redirect leaves the origin, so the key
        still never reaches another host.
        """
        headers: dict[str, str] = {}
        if auth and self._config.api_key:
            headers["Authorization"] = f"Bearer {self._config.api_key}"
        request = self._client.build_request(
            method, url, headers=headers, params=dict(params) if params else None
        )
        return await self._checked(
            request, stream, follow_redirects=method.upper() == "GET"
        )

    async def _checked(
        self, request: httpx.Request, stream: bool, *, follow_redirects: bool = False
    ) -> httpx.Response:
        """Send; a refusal is read whole and raised as ``HTTPStatusError``."""
        response = await self._client.send(
            request, stream=stream, follow_redirects=follow_redirects
        )
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
        url = media_url(
            self._config.base_url,
            model_path(surface.path, attempt.resolved.provider_model),
        )
        body = await build_wire_body(surface, attempt)
        stream = bool(attempt.request.stream)
        async with self._rate_limiter.concurrency_slot():
            try:
                response = await self._rate_limiter.execute_with_retry(
                    self._send, url, body, stream, surface.auth_header
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
            if attempt.request.operation == MEDIA_OPERATION_VIDEO_CREATE:
                self._require_job_id(response)
            if not stream:
                answer = MediaResponse(
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
                if surface.shape in (
                    MEDIA_SHAPE_GEMINI_TTS,
                    MEDIA_SHAPE_GEMINI_TRANSCRIBE,
                ):
                    answer = await self._translated(surface, attempt, answer, body)
                elif attempt.request.inline_urls:
                    answer = await self._inlined(answer)
                yield answer
                return
            try:
                async for raw in response.aiter_bytes():
                    if raw:
                        yield raw
            finally:
                await response.aclose()

    async def _translated(
        self,
        surface: MediaSurface,
        attempt: MediaAttempt,
        answer: MediaResponse,
        body: WireBody,
    ) -> MediaResponse:
        """A native Gemini answer as the client's audio or transcript, off the loop.

        No audio (or no transcript) in a 2xx is no answer: raised as an
        upstream failure so the model is charged and the chain moves on.
        """
        named = attempt.request.body.get("response_format")
        translate = (
            speech_answer
            if surface.shape == MEDIA_SHAPE_GEMINI_TTS
            else transcript_answer
        )
        try:
            translated = await asyncio.to_thread(
                translate,
                answer.body,
                named if isinstance(named, str) and named else None,
            )
        except GeminiAnswerError as error:
            raise ExecutionFailure(
                kind=FailureKind.UPSTREAM,
                status_code=502,
                message=f"{self._provider_id} answered {error}",
                retryable=False,
            ) from error
        return MediaResponse(
            status_code=answer.status_code,
            content_type=translated.content_type,
            body=translated.body,
            headers={**answer.headers, "content-type": translated.content_type},
            usage=translated.usage,
            audio_seconds=translated.audio_seconds,
            not_forwarded=body.not_forwarded,
        )

    async def _inlined(self, answer: MediaResponse) -> MediaResponse:
        """The answer with every URL-only picture downloaded and inlined.

        One download per picture, up this key's own retry ladder; the key is
        sent only to the provider's own host (a CDN URL is fetched bare), and
        httpx drops it on a redirect that leaves that host.
        """
        urls = await asyncio.to_thread(url_only_images, answer.body)
        if not urls:
            return answer
        fetched: dict[int, bytes] = {}
        for position, url in urls:
            fetched[position] = await self._download(url)
        body = await asyncio.to_thread(inline_image_urls, answer.body, fetched)
        return dataclasses.replace(answer, body=body)

    async def _download(self, url: str) -> bytes:
        """One picture's bytes; any failure is this attempt's failure."""
        if urlsplit(url).scheme not in {"https", "http"}:
            raise self._undownloadable("the answer gave no http(s) address")
        try:
            response = await self._rate_limiter.execute_with_retry(
                self.request,
                "GET",
                url,
                auth=same_host(url, self._config.base_url),
                stream=False,
            )
        except httpx.HTTPStatusError as error:
            raise self._undownloadable(f"HTTP {error.response.status_code}") from error
        except Exception as error:
            # The type only: a transport error's text can carry the address,
            # and a signed address is a credential of its own.
            raise self._undownloadable(type(error).__name__) from error
        return response.content

    def _undownloadable(self, reason: str) -> ExecutionFailure:
        return ExecutionFailure(
            kind=FailureKind.UPSTREAM,
            status_code=502,
            message=(
                f"{self._provider_id} produced the image but it could not be "
                f"downloaded: {reason}"
            ),
            retryable=False,
        )

    def _require_job_id(self, response: httpx.Response) -> None:
        """A video create is accepted only when the answer names the job.

        A 2xx without a string ``id`` is no job anyone can poll: raised as an
        upstream failure so the model is charged and the chain moves on --
        it is not acceptance.
        """
        job = parse_job(response.content)
        if job is None or job.id is None:
            raise ExecutionFailure(
                kind=FailureKind.UPSTREAM,
                status_code=502,
                message=(
                    f"{self._provider_id} answered the video request without a "
                    "job id; not accepted"
                ),
                retryable=False,
            )
