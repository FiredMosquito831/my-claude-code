"""Native Anthropic Messages upstream provider family."""

import asyncio
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

import httpx
from loguru import logger

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningAdaptationKind,
    ReasoningDialect,
    ReasoningPolicy,
    narrow_dialect_by_rejections,
)
from my_claude_code.core.trace import trace_event
from my_claude_code.core.wire_capture import (
    record_reasoning_adaptation,
    record_response_shape,
    record_wire_request,
    start_response_shape,
)
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.failure_policy import (
    ProviderFailureOverride,
    classify_provider_failure,
)
from my_claude_code.providers.http import close_provider_stream
from my_claude_code.providers.model_listing import model_infos_from_ids
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.recovery import (
    ANTHROPIC_OUTPUT_FIELDS,
    OutputCapRecovery,
    ReasoningStripRecovery,
    RecoveryLadder,
    RecoveryMemory,
)
from my_claude_code.providers.stream_recovery import (
    RecoveryController,
    RecoveryFailureAction,
)

from .auth import AnthropicMessagesAuth, BearerTokenAuth
from .request import build_anthropic_messages_body
from .streaming import iter_anthropic_sse_frames

ANTHROPIC_REASONING_DIALECT = ReasoningDialect(
    budget=True,
    toggle=True,
    off=True,
    adaptive=True,
    toggle_field="thinking.type",
    budget_field="thinking.budget_tokens",
)
"""Anthropic's ``thinking`` object: a budget, an on/off, and adaptive.

The one host in the fleet with a genuine adaptive channel, which is why
``ReasoningControl.ADAPTIVE`` encodes to something here and to nothing
everywhere else. No effort field: the wire takes a number.
"""


class AnthropicMessagesProvider(BaseProvider):
    """Provider for upstream APIs implementing native Anthropic Messages SSE."""

    def reasoning_dialect(self, model_id: str) -> ReasoningDialect:
        """See :data:`ANTHROPIC_REASONING_DIALECT`, minus what this host refused.

        An Anthropic-protocol gateway is not always Anthropic. Command Code,
        Bedrock-style relays and self-hosted bridges all answer ``/messages``
        and none of them is obliged to parse ``thinking``; where one says so in
        its own 400 the declaration above is simply wrong for that model, and a
        rejection the host itself sent outranks it.
        """
        rejections = self._recovery_memory.rejections_for(model_id)
        if not rejections:
            return ANTHROPIC_REASONING_DIALECT
        return narrow_dialect_by_rejections(ANTHROPIC_REASONING_DIALECT, rejections)

    def __init__(
        self,
        config: ProviderConfig,
        *,
        provider_name: str,
        rate_limiter: ProviderRateLimiter,
        auth: AnthropicMessagesAuth | None = None,
        extra_headers: dict[str, str] | None = None,
        body_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(config)
        self._provider_name = provider_name
        self._base_url = config.base_url.rstrip("/")
        # Default preserves the bearer-token shape every existing caller uses;
        # upstreams with a different credential contract inject their own.
        self._auth = auth if auth is not None else BearerTokenAuth(config.api_key)
        self._extra_headers = dict(extra_headers or {})
        # Applied to the serialized body just before it goes upstream, for
        # upstreams whose wire format differs from the canonical one.
        self._body_transform = body_transform
        # Optional, installed by a subclass after construction. Both default to
        # off, so every provider in this family behaves exactly as before
        # unless it opts in.
        #
        # ``_response_observer`` sees the *response* headers of every upstream
        # call. Anthropic reports a subscription's 5-hour and weekly windows
        # only there, and MCC read them nowhere.
        #
        # ``_auth_retry`` is asked, once, whether a 4xx was a stale credential
        # worth refreshing. Returning True re-sends the same body with freshly
        # built headers, once. It sits inside one upstream attempt on purpose:
        # a rejected token must cost one retry, not a new rung on the ladder.
        self._response_observer: Callable[[Mapping[str, str], int], None] | None = None
        self._auth_retry: Callable[[int], Awaitable[bool]] | None = None
        # ``_failure_override`` lets the owning provider classify an error it
        # understands better than the shared policy does -- the Claude
        # subscription provider uses it for token-endpoint failures, which
        # surface from the same ``try`` as an inference error and would
        # otherwise be reported as a generic upstream 502.
        self._failure_override: ProviderFailureOverride | None = None
        # What this host has taught this process about itself. Same two tables,
        # same lifetime and the same matchers as the OpenAI-chat family's --
        # only the body keys differ, because this dialect spells the output
        # budget ``max_tokens`` and its reasoning instruction ``thinking``.
        self._recovery_memory = RecoveryMemory()
        log_tag = f"{provider_name}_STREAM"
        self._output_cap_recovery = OutputCapRecovery(
            self._recovery_memory,
            log_tag=log_tag,
            fields=ANTHROPIC_OUTPUT_FIELDS,
        )
        self._recovery_ladder = RecoveryLadder(
            (
                self._output_cap_recovery.rung(),
                ReasoningStripRecovery(log_tag=log_tag).rung(),
            )
        )
        self._rate_limiter = rate_limiter
        self._client = httpx.AsyncClient(
            proxy=config.proxy or None,
            timeout=httpx.Timeout(
                config.http_read_timeout,
                connect=config.http_connect_timeout,
                read=config.http_read_timeout,
                write=config.http_write_timeout,
            ),
        )

    def set_response_observer(
        self, observer: Callable[[Mapping[str, str], int], None] | None
    ) -> None:
        """Watch every upstream response's headers. See ``__init__``."""
        self._response_observer = observer

    def set_auth_retry(self, retry: Callable[[int], Awaitable[bool]] | None) -> None:
        """Let a credential refresh itself once on an auth rejection."""
        self._auth_retry = retry

    def set_failure_override(self, override: ProviderFailureOverride | None) -> None:
        """Classify provider-specific errors ahead of the shared policy.

        The override is threaded into *both* the retry ladder and the final
        classification, because they have to agree about what an error means:
        wiring only one produces a failure that is retried but reported wrong,
        or reported right and never retried.
        """
        self._failure_override = override

    def throttle_remaining(self, model: str | None = None) -> float:
        return self._rate_limiter.remaining_wait()

    async def cleanup(self) -> None:
        await self._client.aclose()

    async def list_model_ids(self) -> frozenset[str]:
        return frozenset()

    async def list_model_infos(self) -> frozenset[ProviderModelInfo]:
        return model_infos_from_ids(await self.list_model_ids())

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        build_anthropic_messages_body(request, reasoning=reasoning)

    async def _send_stream_request(self, body: dict[str, Any]) -> httpx.Response:
        response, request = await self._open_stream(body)
        if response.status_code >= 400 and self._auth_retry is not None:
            status = response.status_code
            await response.aread()
            await response.aclose()
            if await self._auth_retry(status):
                response, request = await self._open_stream(body)
        if response.status_code >= 400:
            if not response.is_closed:
                await response.aread()
                await response.aclose()
            raise httpx.HTTPStatusError(
                f"{self._provider_name} Messages API error {response.status_code}",
                request=request,
                response=response,
            )
        return response

    async def _open_stream(
        self, body: dict[str, Any]
    ) -> tuple[httpx.Response, httpx.Request]:
        headers = {"Content-Type": "application/json"}
        headers.update(self._extra_headers)
        # Resolved per request: a short-lived credential may refresh between
        # attempts, so the header cannot be captured once at construction.
        headers.update(await self._auth.headers())
        request = self._client.build_request(
            "POST",
            f"{self._base_url}/messages",
            headers=headers,
            json=body,
        )
        response = await self._client.send(request, stream=True)
        if self._response_observer is not None:
            self._response_observer(response.headers, response.status_code)
        return response, request

    def _remember_reasoning_rejection(self, body: dict[str, Any], field: str) -> None:
        """Record that this model refused a reasoning field, once it is proven.

        Reached only after the stripped body was actually accepted, so the
        strip is what fixed it. Later requests skip the field without paying
        the 400: the dialect this provider reports no longer claims it (see
        :meth:`reasoning_dialect`), so gating never asks the encoder for it.
        """
        model = body.get("model")
        if not isinstance(model, str):
            return
        if not self._recovery_memory.remember_rejection(model, field):
            return
        record_reasoning_adaptation(
            ReasoningAdaptationKind.SUPPRESSED,
            f"{self._provider_name} rejected {field!r} for {model}; the request "
            f"was retried without it and this model will not be sent it again.",
        )
        logger.warning(
            "{}_STREAM: {!r} learned as rejected for {} -- later requests omit it",
            self._provider_name,
            field,
            model,
        )

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> AsyncIterator[str]:
        del input_tokens
        return self._stream_response(
            request,
            request_id=request_id,
            reasoning=reasoning,
        )

    async def _stream_response(
        self,
        request: MessagesRequest,
        *,
        request_id: str | None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        tag = self._provider_name
        req_tag = f" request_id={request_id}" if request_id else ""
        body = build_anthropic_messages_body(request, reasoning=reasoning)
        if self._body_transform is not None:
            body = self._body_transform(body)
        body = self._output_cap_recovery.apply_learned(body)
        trace_event(
            stage="provider",
            event="provider.request.sent",
            source="provider",
            provider=tag,
            request_id=request_id,
            gateway_model=request.model,
            downstream_model=body.get("model"),
            message_count=len(body.get("messages", [])),
            tool_count=len(body.get("tools", [])),
            body={
                "model": body.get("model"),
                "message_count": len(body.get("messages", [])),
                "tool_count": len(body.get("tools", [])),
                "stream": True,
            },
        )
        recovery = RecoveryController(
            provider_name=tag,
            request_id=request_id,
            holdback_seconds=self._config.commit_holdback_seconds,
            holdback_chars=self._config.commit_holdback_chars,
            reasoning_commits=not self._config.fallback_on_reasoning_only,
            early_retry_attempts=self._config.early_retry_attempts,
            midstream_recovery_attempts=0,
        )

        # Per attempt chain, deliberately locals: concurrent requests share
        # this provider, and both values are consumed once a retry is accepted.
        used_retry_kinds: set[str] = set()
        stripped_reasoning: str | None = None

        async with self._rate_limiter.concurrency_slot():
            while True:
                response: httpx.Response | None = None
                stream_opened = False
                try:
                    # Commit boundary: the body is final once it is handed
                    # to the sender.
                    record_wire_request(body)
                    response = await self._rate_limiter.execute_with_retry(
                        self._send_stream_request,
                        body,
                        provider_failure_override=self._failure_override,
                    )
                    stream_opened = True
                    if stripped_reasoning is not None:
                        self._remember_reasoning_rejection(body, stripped_reasoning)
                    recovery.upstream_opened()
                    shape = start_response_shape()
                    async for event in iter_anthropic_sse_frames(
                        response.aiter_bytes(), shape
                    ):
                        for held_event in recovery.push(event):
                            yield held_event
                    for held_event in recovery.flush():
                        yield held_event
                    record_response_shape(shape)
                    return
                except asyncio.CancelledError, GeneratorExit:
                    raise
                except Exception as error:
                    if not stream_opened:
                        recovered = self._recovery_ladder.next_body(
                            error, body, used_retry_kinds
                        )
                        if recovered.body is not None:
                            if recovered.stripped_reasoning_field is not None:
                                stripped_reasoning = recovered.stripped_reasoning_field
                            body = recovered.body
                            continue
                    decision = recovery.advance_failure(
                        error,
                        stream_opened=stream_opened,
                        generated_output=recovery.has_buffered or recovery.committed,
                        complete_tool_salvageable=False,
                    )
                    if decision.action is RecoveryFailureAction.EARLY_RETRY:
                        continue
                    self._log_stream_transport_error(
                        tag,
                        req_tag,
                        error,
                        request_id=request_id,
                    )
                    failure = classify_provider_failure(
                        error,
                        provider_name=tag,
                        read_timeout_s=self._config.http_read_timeout,
                        request_id=request_id,
                        mark_rate_limited=self._rate_limiter.extend_reactive_block,
                        cooldown_seconds=self._config.rate_limit_cooldown_seconds,
                        mark_rate_limited_enabled=(
                            not self._config.routes_around_model
                        ),
                        provider_failure_override=self._failure_override,
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
                    if not decision.committed:
                        recovery.discard()
                    raise failure from error
                finally:
                    if response is not None:
                        await close_provider_stream(
                            response,
                            active_error=sys.exception(),
                            provider_name=tag,
                            request_id=request_id,
                        )
