"""ChatGPT OAuth Codex Responses image node for ``MODEL_IMAGE``.

Implements :class:`MediaNode` for ``chatgpt_oauth`` without touching frozen
``IV.8`` modules (``providers/runtime/factory.py`` etc.):

* Resolves credentials via :func:`load_chatgpt_oauth_credentials` and refreshes
  expired managed credentials on ``401`` via
  :func:`force_refresh_managed_chatgpt_oauth_credentials` inside
  ``claimed_scope(...)`` + ``refresh_exit(config.proxy)``.
* Sends Codex Responses headers (``OpenAI-Beta``, ``originator``,
  ``User-Agent``, ``session-id``, ``ChatGPT-Account-ID``, ``Accept: text/event-stream``)
  and conditional ``zstd`` compression.
* Executes through ``self._rate_limiter.execute_with_retry(...)``, classifies
  failures via :func:`classify_provider_failure`, observes ``x-codex-*``
  headers, records ``media_line(DOOR_MEDIA, ...)`` and ``record_proxy(...)``,
  and folds Codex SSE ``image_generation_call`` events into canonical OpenAI
  Images JSON.
"""

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Any

import httpx

from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.application.media.endpoint_ladder import ResolvedMediaEndpointLadder
from my_claude_code.application.media.request import (
    MediaAttempt,
    MediaChunk,
    MediaRequest,
    MediaResponse,
)
from my_claude_code.config.constants import (
    HTTP_CONNECT_TIMEOUT_DEFAULT,
    MEDIA_APPROXIMATE_PARAM_MAPPING_DEFAULT,
    MEDIA_PARAM_RECOVERY_MODE_DEFAULT,
)
from my_claude_code.config.credentials import mask_key_label
from my_claude_code.config.media_overrides import current_media_overrides
from my_claude_code.config.media_surfaces import (
    MediaSurface,
    effective_surfaces_for,
    media_url,
    model_path,
    surface_key,
)
from my_claude_code.config.settings import (
    Settings,
    configured_media_approximate_param_mapping,
    configured_media_param_recovery_mode,
)
from my_claude_code.config.system_proxy import system_proxy_for
from my_claude_code.core.chatgpt_oauth_media import (
    ChatGPTOAuthMediaAnswerError,
    parse_chatgpt_oauth_image_answer,
)
from my_claude_code.core.credential_refresh_scope import (
    claimed_scope,
    refresh_exit,
    stream_purpose,
)
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.proxy_attribution import (
    DIRECT_PROXY_LABEL,
    record_proxy,
    system_proxy_label,
)
from my_claude_code.core.upstream_ladder import note_response_head
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.chatgpt_oauth.credentials import (
    ChatGPTOAuthError,
    force_refresh_managed_chatgpt_oauth_credentials,
    load_chatgpt_oauth_credentials,
)
from my_claude_code.providers.chatgpt_oauth.provider import (
    CHATGPT_OAUTH_DEFAULT_BASE,
    CHATGPT_OAUTH_PROVIDER_ID,
    _build_headers,
    _encode_request_payload,
)
from my_claude_code.providers.chatgpt_oauth.response_headers import (
    OBSERVER as RESPONSE_HEADER_OBSERVER,
)
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.providers.http import error_response_headers, read_error_body
from my_claude_code.providers.oauth_names import account_name
from my_claude_code.providers.proxy_keepalive import leg_pool_kwargs
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.raw_upstream import (
    DOOR_MEDIA,
    capture_enabled,
    emit,
    media_line,
)
from my_claude_code.providers.recovery import learned_fact_store
from my_claude_code.providers.socks_deadline import bound_socks_handshake

from .adapters import build_wire_body
from .leaf import MediaLeaf, surfaces_serve
from .recovery import apply_media_preflight

_KEPT_HEADERS = ("content-type", "x-request-id", "request-id")
_MEDIA_REFUSAL_HEAD_BYTES = 4096


def normalize_chatgpt_oauth_base_url(raw_base_url: str | None) -> str:
    """Normalize Codex OAuth base URL, stripping any trailing ``/codex``."""

    base_url = (raw_base_url or CHATGPT_OAUTH_DEFAULT_BASE).rstrip("/")
    if base_url.endswith("/codex"):
        base_url = base_url[: -len("/codex")]
    return base_url or CHATGPT_OAUTH_DEFAULT_BASE


class ChatGPTOAuthMediaNode:
    """One ChatGPT OAuth account/leg serving image generation & edit on Codex."""

    def __init__(
        self,
        *,
        provider_id: str,
        config: ProviderConfig,
        surfaces: tuple[MediaSurface, ...],
        rate_limiter: ProviderRateLimiter,
        transport: httpx.AsyncBaseTransport | None = None,
        proxy_label: str = "",
        account_id: str = "",
        pinned_account_id: str = "",
        endpoint_ladder: Callable[
            [str, str, tuple[MediaSurface, ...]], ResolvedMediaEndpointLadder
        ]
        | None = None,
        param_recovery_mode: str = MEDIA_PARAM_RECOVERY_MODE_DEFAULT,
        approximate_param_mapping: bool = MEDIA_APPROXIMATE_PARAM_MAPPING_DEFAULT,
        settings_getter: Callable[[], Settings] | None = None,
    ) -> None:
        self._provider_id = provider_id
        self._config = config
        self._base_url = normalize_chatgpt_oauth_base_url(config.base_url)
        self._surfaces = surfaces
        self._rate_limiter = rate_limiter
        self._proxy_label = proxy_label
        self._account_id = account_id
        self._pinned_account_id = pinned_account_id
        self._credential_label_override = (
            account_name(CHATGPT_OAUTH_PROVIDER_ID, pinned_account_id)
            if pinned_account_id
            else ""
        )
        self._session_id = str(uuid.uuid4())
        self._endpoint_ladder = endpoint_ladder
        self._param_recovery_mode = param_recovery_mode
        self._approximate_param_mapping = approximate_param_mapping
        self._settings_getter = settings_getter
        connect_timeout = config.http_connect_timeout or HTTP_CONNECT_TIMEOUT_DEFAULT
        timeout = httpx.Timeout(
            config.http_read_timeout,
            connect=connect_timeout,
            read=config.http_read_timeout,
            write=config.http_write_timeout,
        )
        if transport is not None:
            self._client = httpx.AsyncClient(transport=transport, timeout=timeout)
        else:
            self._client = bound_socks_handshake(
                httpx.AsyncClient(
                    proxy=config.proxy or None,
                    timeout=timeout,
                    **leg_pool_kwargs(config.http_keepalive_expiry),
                )
            )

    @property
    def credential_label(self) -> str | None:
        if self._credential_label_override:
            return self._credential_label_override
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
        del key_index, proxy_label
        return None

    def throttle_remaining(self, model: str | None = None) -> float:
        del model
        return self._rate_limiter.remaining_wait()

    def supports(self, request: MediaRequest) -> bool:
        return surfaces_serve(self._surfaces, request)

    def preflight(self, attempt: MediaAttempt) -> None:
        del attempt

    async def cleanup(self) -> None:
        await self._client.aclose()

    def execute(
        self, attempt: MediaAttempt, *, request_id: str | None = None
    ) -> AsyncIterator[MediaChunk]:
        if self._proxy_label:
            record_proxy(self._dial_label())
        return self._execute(attempt, request_id=request_id)

    def _dial_label(self) -> str:
        if self._proxy_label != DIRECT_PROXY_LABEL:
            return self._proxy_label
        address = system_proxy_for(self._base_url)
        return system_proxy_label(address) if address else self._proxy_label

    def _recovery_settings(self) -> tuple[str, bool]:
        if self._settings_getter is not None:
            settings = self._settings_getter()
            return (
                configured_media_param_recovery_mode(settings),
                configured_media_approximate_param_mapping(settings),
            )
        return self._param_recovery_mode, self._approximate_param_mapping

    async def _raise_http_error(
        self, response: httpx.Response, request: httpx.Request
    ) -> None:
        error = await read_error_body(response)
        await response.aclose()
        note_response_head(error.head)
        if capture_enabled():
            for line in media_line(
                DOOR_MEDIA,
                status=response.status_code,
                content_type=response.headers.get("content-type", ""),
                nbytes=len(error.raw),
                refusal_head=error.raw[:_MEDIA_REFUSAL_HEAD_BYTES],
            ):
                emit(line)
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

    async def _send_post(
        self,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any],
    ) -> httpx.Response:
        content, wire_headers, compressed = _encode_request_payload(body, headers)
        request = self._client.build_request(
            "POST", url, headers=wire_headers, content=content
        )
        response = await self._client.send(request, stream=True)
        if response.status_code == 413 and not compressed:
            await response.aclose()
            content, wire_headers, _ = _encode_request_payload(
                body, headers, force_zstd=True
            )
            request = self._client.build_request(
                "POST", url, headers=wire_headers, content=content
            )
            response = await self._client.send(request, stream=True)
        RESPONSE_HEADER_OBSERVER.observe(
            response.headers,
            status_code=response.status_code,
            account_id=self._pinned_account_id,
        )
        if response.status_code >= 400 and response.status_code != 401:
            await self._raise_http_error(response, request)
        return response

    async def _execute(
        self, attempt: MediaAttempt, *, request_id: str | None
    ) -> AsyncIterator[MediaChunk]:
        ladder = (
            self._endpoint_ladder(
                attempt.resolved.provider_model_ref,
                attempt.request.operation,
                self._surfaces,
            )
            if self._endpoint_ladder is not None and attempt.resolved.provider_model_ref
            else None
        )
        if ladder is not None and ladder.winner is not None:
            active_surface: MediaSurface | None = ladder.winner.surface
            surface_source = ladder.winner.source
        else:
            candidates = effective_surfaces_for(
                self._surfaces, attempt.request.operation
            )
            active_surface = candidates[0] if candidates else None
            surface_source = "provider_default"
        if active_surface is None:  # pragma: no cover - the gate runs first
            raise RuntimeError(
                f"{self._provider_id} declares no {attempt.request.operation} surface"
            )

        mode, approximate_mapping = self._recovery_settings()
        learned_facts = learned_fact_store().all_facts()
        overrides = current_media_overrides()
        current_attempt, preflight_adaptations = apply_media_preflight(
            attempt,
            active_surface,
            mode=mode,
            approximate_mapping=approximate_mapping,
            learned_facts=learned_facts,
            overrides=overrides,
        )
        body = await build_wire_body(
            active_surface,
            current_attempt,
            approximate_mapping=approximate_mapping,
        )
        all_adaptations = tuple(
            dict.fromkeys([*preflight_adaptations, *body.adaptations])
        )
        url = media_url(
            self._base_url,
            model_path(active_surface.path, current_attempt.resolved.provider_model),
        )

        purpose = stream_purpose()
        try:
            with claimed_scope(purpose), refresh_exit(self._config.proxy):
                active_credentials = await asyncio.to_thread(
                    load_chatgpt_oauth_credentials,
                    access_token=self._config.api_key or None,
                    account_id=self._account_id or None,
                    pinned_account_id=self._pinned_account_id or None,
                )
        except ChatGPTOAuthError as exc:
            raise ApplicationUnavailableError(str(exc)) from exc

        active_headers = _build_headers(active_credentials, self._session_id)
        payload = body.json or {}

        async with self._rate_limiter.concurrency_slot():
            try:
                response = await self._rate_limiter.execute_with_retry(
                    self._send_post, url, active_headers, payload
                )
                if (
                    response.status_code == 401
                    and active_credentials.source_name == "fcc-managed"
                ):
                    await response.aclose()
                    try:
                        with claimed_scope(purpose), refresh_exit(self._config.proxy):
                            active_credentials = await asyncio.to_thread(
                                force_refresh_managed_chatgpt_oauth_credentials,
                                self._pinned_account_id
                                or active_credentials.account_id
                                or None,
                            )
                    except ChatGPTOAuthError as exc:
                        raise ExecutionFailure(
                            kind=FailureKind.AUTHENTICATION,
                            status_code=401,
                            message=str(exc),
                            retryable=False,
                        ) from exc
                    active_headers = _build_headers(
                        active_credentials, self._session_id
                    )
                    response = await self._rate_limiter.execute_with_retry(
                        self._send_post, url, active_headers, payload
                    )
                if response.status_code == 401:
                    await self._raise_http_error(response, response.request)
                try:
                    raw_sse = await response.aread()
                finally:
                    await response.aclose()
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

        try:
            translated = await asyncio.to_thread(
                parse_chatgpt_oauth_image_answer, raw_sse
            )
        except ChatGPTOAuthMediaAnswerError as error:
            raise ExecutionFailure(
                kind=FailureKind.UPSTREAM,
                status_code=502,
                message=f"{self._provider_id} answered {error}",
                retryable=False,
            ) from error

        headers = {
            name: response.headers[name]
            for name in _KEPT_HEADERS
            if name in response.headers
        }
        headers["content-type"] = translated.content_type
        answer = MediaResponse(
            status_code=response.status_code,
            content_type=translated.content_type,
            body=translated.body,
            headers=headers,
            usage=translated.usage,
            text=translated.text,
            not_forwarded=body.not_forwarded,
            adaptations=all_adaptations,
            media_surface=surface_key(active_surface),
            media_surface_source=surface_source,
        )
        if capture_enabled():
            for line in media_line(
                DOOR_MEDIA,
                status=answer.status_code,
                content_type=answer.content_type,
                nbytes=len(answer.body),
            ):
                emit(line)
        yield answer
