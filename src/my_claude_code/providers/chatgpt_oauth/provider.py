"""Direct ChatGPT/Codex OAuth provider using the Responses API."""

import asyncio
import platform
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
from loguru import logger

from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.config.constants import HTTP_CONNECT_TIMEOUT_DEFAULT
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.core.diagnostics import (
    exception_cause_types,
    redacted_exception_traceback,
)
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningAdaptationKind,
    ReasoningDialect,
    ReasoningEffort,
    ReasoningPolicy,
    narrow_dialect_by_rejections,
)
from my_claude_code.core.trace import trace_event
from my_claude_code.core.version import package_version
from my_claude_code.core.wire_capture import (
    record_reasoning_adaptation,
    record_response_shape,
    record_wire_request,
    start_response_shape,
)
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.providers.model_listing import model_infos_from_ids
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.recovery import (
    ReasoningStripRecovery,
    RecoveryLadder,
    RecoveryMemory,
)
from my_claude_code.providers.runtime.models_dev import (
    models_dev_provider_model_ids,
)

from .conversion import build_chatgpt_oauth_request_body
from .credentials import (
    CODEX_OAUTH_ORIGINATOR,
    ChatGPTOAuthError,
    force_refresh_managed_chatgpt_oauth_credentials,
    load_chatgpt_oauth_credentials,
)
from .streaming import (
    ChatGPTOAuthStreamConverter,
    iter_chatgpt_oauth_sse_events,
    note_responses_event_shape,
)

CHATGPT_OAUTH_DEFAULT_BASE = "https://chatgpt.com/backend-api"

# Model allowlist aligned with OpenCode's ChatGPT/Codex OAuth filter.
# https://github.com/anomalyco/opencode/blob/main/packages/opencode/src/plugin/openai/codex.ts
_CHATGPT_OAUTH_ALLOWED_MODELS = frozenset(
    {
        "gpt-5.5",
        "gpt-5.3-codex-spark",
        "gpt-5.4",
        "gpt-5.4-mini",
    }
)
# Published by models.dev under ``openai`` but not served by the ChatGPT/Codex
# OAuth backend. ``gpt-5.6`` is a family name here: only the -luna, -sol and
# -terra variants exist on this plan, and the bare id 404s.
_CHATGPT_OAUTH_DISALLOWED_MODELS = frozenset({"gpt-5.5-pro", "gpt-5.6"})
_CHATGPT_OAUTH_GPT_VERSION_RE = re.compile(r"^gpt-(\d+\.\d+)")
_CHATGPT_OAUTH_MIN_GPT_VERSION = 5.4

# Known ids to fall back on when the models.dev catalog is unavailable -- a
# fresh install with no network still gets a usable picker.
_CHATGPT_OAUTH_STATIC_MODELS = frozenset(
    {
        "gpt-5",
        "gpt-5.2",
        "gpt-5.4",
        "gpt-5.5",
        "gpt-5.6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5-codex",
        "gpt-5.1-codex",
        "gpt-5.2-codex",
        "gpt-5.3-codex",
        "gpt-5.3-codex-spark",
        "gpt-5.4-mini",
        "gpt-5.5-pro",
        "codex-mini-latest",
    }
)


def _user_agent() -> str:
    """Return the Codex OAuth client identity used by the upstream provider."""
    return (
        f"{CODEX_OAUTH_ORIGINATOR}/{package_version()} "
        f"({platform.system()} {platform.release()}; {platform.machine()})"
    )


def _is_chatgpt_oauth_model(model_id: str) -> bool:
    """Return True when ``model_id`` is exposed by the ChatGPT/Codex backend.

    Mirrors OpenCode's model filter: a small allowlist, an explicit blocklist,
    and a version heuristic for future GPT-5.x models.
    """
    if model_id in _CHATGPT_OAUTH_DISALLOWED_MODELS:
        return False
    if model_id in _CHATGPT_OAUTH_ALLOWED_MODELS:
        return True
    match = _CHATGPT_OAUTH_GPT_VERSION_RE.match(model_id)
    if match:
        return float(match.group(1)) > _CHATGPT_OAUTH_MIN_GPT_VERSION
    return False


def _build_headers(credentials: Any, session_id: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {credentials.access_token}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "OpenAI-Beta": "responses=experimental",
        "originator": CODEX_OAUTH_ORIGINATOR,
        "User-Agent": _user_agent(),
        "session-id": session_id,
    }
    if credentials.account_id:
        headers["ChatGPT-Account-ID"] = credentials.account_id
    return headers


CHATGPT_OAUTH_REASONING_DIALECT = ReasoningDialect(
    effort_values=frozenset(ReasoningEffort),
    toggle=True,
    off=False,
    effort_field="reasoning.effort",
    toggle_field="reasoning.effort",
)
"""The Responses endpoint takes ``reasoning.effort`` and nothing else.

``off=False`` on purpose: an explicit OFF omits the whole ``reasoning``
block rather than spelling a disable, so the endpoint has no OFF at
all. It also has no bare ON -- a policy naming no effort falls back to
the endpoint's long-standing ``medium`` -- so the toggle channel is
real and its on-value is a default rung.
"""


class ChatGPTOAuthProvider(BaseProvider):
    """ChatGPT/Codex OAuth provider using the Responses API."""

    def reasoning_dialect(self, model_id: str) -> ReasoningDialect:
        """See :data:`CHATGPT_OAUTH_REASONING_DIALECT`, minus what was refused.

        The endpoint has retired reasoning-shaped fields before. A model that
        answers a ``reasoning`` block with a 400 has said so itself, and that
        outranks the declaration above for that model.
        """
        rejections = self._recovery_memory.rejections_for(model_id)
        if not rejections:
            return CHATGPT_OAUTH_REASONING_DIALECT
        return narrow_dialect_by_rejections(CHATGPT_OAUTH_REASONING_DIALECT, rejections)

    def _remember_reasoning_rejection(self, body: dict[str, Any], field: str) -> None:
        """Record that this model refused a reasoning field, once it is proven.

        Reached only after the stripped body was actually accepted, so the
        strip is what fixed it.
        """
        model = body.get("model")
        if not isinstance(model, str):
            return
        if not self._recovery_memory.remember_rejection(model, field):
            return
        record_reasoning_adaptation(
            ReasoningAdaptationKind.SUPPRESSED,
            f"CHATGPT_OAUTH rejected {field!r} for {model}; the request was "
            f"retried without it and this model will not be sent it again.",
        )
        logger.warning(
            "CHATGPT_OAUTH_STREAM: {!r} learned as rejected for {} -- "
            "later requests omit it",
            field,
            model,
        )

    def __init__(
        self,
        config: ProviderConfig,
        *,
        rate_limiter: ProviderRateLimiter,
        account_id: str = "",
    ):
        super().__init__(config)
        self._rate_limiter = rate_limiter
        self._base_url = (config.base_url or CHATGPT_OAUTH_DEFAULT_BASE).rstrip("/")
        self._account_id = account_id
        self._api_key = config.api_key
        self._proxy = config.proxy
        self._session_id = str(uuid.uuid4())
        # What this endpoint has taught this process about itself. Only the
        # reasoning half is wired: the Responses encoder emits no output-token
        # field at all, so there is no budget for a host to cap and an
        # output-cap rung here would be a rung that can never fire.
        self._recovery_memory = RecoveryMemory()
        self._recovery_ladder = RecoveryLadder(
            (ReasoningStripRecovery(log_tag="CHATGPT_OAUTH_STREAM").rung(),)
        )
        self._client = httpx.AsyncClient(
            proxy=config.proxy if config.proxy else None,
            timeout=httpx.Timeout(
                config.http_read_timeout,
                connect=config.http_connect_timeout or HTTP_CONNECT_TIMEOUT_DEFAULT,
                read=config.http_read_timeout,
                write=config.http_write_timeout,
            ),
        )

    def throttle_remaining(self, model: str | None = None) -> float:
        """Seconds this credential is rate-limited for; 0 when free to serve."""
        return self._rate_limiter.remaining_wait()

    async def cleanup(self) -> None:
        await self._client.aclose()

    async def list_model_ids(self) -> frozenset[str]:
        """Return the ChatGPT/Codex OAuth model ids, discovered where possible.

        The backend's own models endpoint answers 401 for an OAuth session, so
        the catalog cannot come from the gateway. It comes from models.dev's
        ``openai`` catalog instead -- which FCC already fetches and caches for
        other providers -- filtered by the same allowlist rule. That keeps new
        GPT-5.x releases appearing without a code change, and falls back to the
        known static ids when the cache is absent.
        """
        candidates = models_dev_provider_model_ids("openai") | (
            _CHATGPT_OAUTH_STATIC_MODELS
        )
        return frozenset(m for m in candidates if _is_chatgpt_oauth_model(m))

    async def list_model_infos(self) -> frozenset[ProviderModelInfo]:
        return model_infos_from_ids(await self.list_model_ids())

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        """Validate the upstream request before streaming."""
        build_chatgpt_oauth_request_body(request, reasoning=reasoning)

    async def _send_stream_request(
        self,
        url: str,
        headers: dict[str, str],
        body: dict[str, Any],
    ) -> httpx.Response:
        """Build and send a streaming POST, preserving 401 for one refresh.

        ``httpx.AsyncClient.stream`` returns an async context manager, which is
        not awaitable and therefore cannot be passed directly to the retry
        helper. We instead build the request and call ``send(..., stream=True)``,
        which returns an awaitable ``Response`` while still keeping the body
        stream open until we explicitly close it.
        """
        request = self._client.build_request("POST", url, headers=headers, json=body)
        response = await self._client.send(request, stream=True)
        if response.status_code >= 400 and response.status_code != 401:
            error_body = await response.aread()
            await response.aclose()
            error_text = error_body.decode("utf-8", errors="replace")
            raise httpx.HTTPStatusError(
                f"ChatGPT OAuth API error {response.status_code}: {error_text[:1000]}",
                request=request,
                response=response,
            )
        return response

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        tag = "CHATGPT_OAUTH"
        req_tag = f" request_id={request_id}" if request_id else ""
        logger.debug("{}_STREAM: starting{}", tag, req_tag)

        try:
            credentials = load_chatgpt_oauth_credentials(
                access_token=self._api_key or None,
                account_id=self._account_id or None,
            )
        except ChatGPTOAuthError as exc:
            logger.error("{}_ERROR:{} {}", tag, req_tag, exc)
            raise ApplicationUnavailableError(str(exc)) from exc

        body = build_chatgpt_oauth_request_body(request, reasoning=reasoning)
        url = f"{self._base_url}/codex/responses"
        headers = _build_headers(credentials, self._session_id)

        trace_event(
            stage="provider",
            event="provider.request.sent",
            source="provider",
            provider=tag,
            request_id=request_id,
            gateway_model=request.model,
            downstream_model=body.get("model"),
            message_count=len(body.get("input", [])),
            tool_count=len(body.get("tools", [])),
            body={
                "model": body.get("model"),
                "input_count": len(body.get("input", [])),
                "tool_count": len(body.get("tools", [])),
            },
        )

        async def _stream() -> AsyncIterator[str]:
            message_id = f"msg_{uuid.uuid4()}"
            ledger = AnthropicStreamLedger(
                message_id,
                request.model,
                input_tokens,
                log_raw_events=self._config.log_raw_sse_events,
            )
            converter = ChatGPTOAuthStreamConverter(
                ledger,
                log_raw_events=self._config.log_raw_sse_events,
            )

            async with self._rate_limiter.concurrency_slot():
                try:
                    active_credentials = credentials
                    active_headers = headers
                    refreshed_after_unauthorized = False
                    # Per attempt chain: a rung fires at most once, and the
                    # refusal is only written down once the retry is accepted.
                    used_retry_kinds: set[str] = set()
                    stripped_reasoning: str | None = None
                    # A local of this generator, not the enclosing function's
                    # body: a recovery rewrites what goes on the wire for this
                    # attempt only.
                    attempt_body = body
                    while True:
                        # Commit boundary: the body is final once it is handed
                        # to the sender. Headers are not recorded -- they carry
                        # the bearer token.
                        record_wire_request(attempt_body)
                        try:
                            response = await self._rate_limiter.execute_with_retry(
                                self._send_stream_request,
                                provider_failure_override=(
                                    self._provider_failure_override
                                ),
                                url=url,
                                headers=active_headers,
                                body=attempt_body,
                            )
                            if (
                                response.status_code == 401
                                and active_credentials.source_name == "fcc-managed"
                            ):
                                await response.aclose()
                                try:
                                    active_credentials = await asyncio.to_thread(
                                        force_refresh_managed_chatgpt_oauth_credentials
                                    )
                                except ChatGPTOAuthError as exc:
                                    raise ApplicationUnavailableError(str(exc)) from exc
                                active_headers = _build_headers(
                                    active_credentials, self._session_id
                                )
                                refreshed_after_unauthorized = True
                                response = await self._rate_limiter.execute_with_retry(
                                    self._send_stream_request,
                                    provider_failure_override=(
                                        self._provider_failure_override
                                    ),
                                    url=url,
                                    headers=active_headers,
                                    body=attempt_body,
                                )
                        except ApplicationUnavailableError:
                            raise
                        except Exception as error:
                            recovered = self._recovery_ladder.next_body(
                                error, attempt_body, used_retry_kinds
                            )
                            if recovered.body is None:
                                raise
                            if recovered.stripped_reasoning_field is not None:
                                stripped_reasoning = recovered.stripped_reasoning_field
                            attempt_body = recovered.body
                            continue
                        break
                    if stripped_reasoning is not None:
                        self._remember_reasoning_rejection(
                            attempt_body, stripped_reasoning
                        )
                    try:
                        if response.status_code >= 400:
                            self._log_error(tag, req_tag, None, request_id)
                            if (
                                response.status_code == 401
                                and refreshed_after_unauthorized
                            ):
                                raise ApplicationUnavailableError(
                                    "ChatGPT OAuth authorization was rejected after "
                                    "one refresh. Reconnect in Admin."
                                )
                            if response.status_code == 401:
                                raise ApplicationUnavailableError(
                                    "ChatGPT OAuth access token was rejected. "
                                    "Sign in again in Admin."
                                )
                            raise ApplicationUnavailableError(
                                f"ChatGPT OAuth API error {response.status_code}"
                            )

                        yield ledger.message_start()
                        shape = start_response_shape()
                        async for event in iter_chatgpt_oauth_sse_events(
                            response.aiter_raw()
                        ):
                            note_responses_event_shape(shape, event)
                            for sse_event in converter.feed(event):
                                yield sse_event

                        for sse_event in converter.finish():
                            yield sse_event
                        record_response_shape(shape)
                    finally:
                        await response.aclose()

                except ApplicationUnavailableError:
                    raise
                except Exception as error:
                    self._log_error(tag, req_tag, error, request_id)
                    failure = classify_provider_failure(
                        error,
                        provider_name=tag,
                        read_timeout_s=self._config.http_read_timeout,
                        request_id=request_id,
                        mark_rate_limited=self._rate_limiter.extend_reactive_block,
                        provider_failure_override=self._provider_failure_override,
                        mark_rate_limited_enabled=(
                            not self._config.routes_around_model
                        ),
                    )
                    trace_event(
                        stage="provider",
                        event="provider.response.error",
                        source="provider",
                        provider=tag,
                        request_id=request_id,
                        exc_type=type(error).__name__,
                        failure_kind=failure.kind.value,
                        status_code=failure.status_code,
                        provider_retryable=failure.retryable,
                    )
                    raise failure from error

        return _stream()

    def _provider_failure_override(self, error: Exception) -> ExecutionFailure | None:
        return None

    def _log_error(
        self,
        tag: str,
        req_tag: str,
        error: Exception | None,
        request_id: str | None,
    ) -> None:
        if error is None:
            logger.error("{}_ERROR:{} transport error", tag, req_tag)
            return
        if self._config.log_api_error_tracebacks:
            logger.error(
                "{}_ERROR:{} exc_type={}\n{}",
                tag,
                req_tag,
                type(error).__name__,
                redacted_exception_traceback(error),
            )
        else:
            logger.error(
                "{}_ERROR:{} exc_type={} cause_types={}",
                tag,
                req_tag,
                type(error).__name__,
                ",".join(exception_cause_types(error)),
            )
