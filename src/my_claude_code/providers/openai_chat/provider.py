"""Concrete OpenAI-compatible provider and per-request stream execution."""

import asyncio
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Mapping
from typing import Any

import httpx
from loguru import logger
from openai import AsyncOpenAI

from my_claude_code.application.model_metadata import ProviderModelInfo
from my_claude_code.core.anthropic import (
    ContentType,
    HeuristicToolParser,
    ThinkTagParser,
)
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.stream_contracts import REASONING_HEARTBEAT
from my_claude_code.core.anthropic.streaming import (
    AnthropicStreamLedger,
    accept_tool_json_repair,
    continuation_suffix,
    make_text_recovery_body,
    make_tool_repair_body,
    map_stop_reason,
    parse_complete_tool_input,
    tool_schemas_by_name,
)
from my_claude_code.core.failures import ExecutionFailure
from my_claude_code.core.reasoning import (
    DEFAULT_REASONING_POLICY,
    ReasoningAdaptationKind,
    ReasoningDialect,
    ReasoningPolicy,
    narrow_dialect_by_rejections,
)
from my_claude_code.core.request_log import (
    RECOVERY_EARLY_RETRIES,
    RECOVERY_MIDSTREAM_RECOVERIES,
    RECOVERY_SALVAGES,
    record_recovery_event,
)
from my_claude_code.core.trace import provider_chat_body_snapshot, trace_event
from my_claude_code.core.wire_capture import (
    ResponseShape,
    record_reasoning_adaptation,
    record_response_shape,
    record_wire_request,
    start_response_shape,
)
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.providers.http import (
    close_provider_stream,
    maybe_await_aclose,
)
from my_claude_code.providers.model_listing import (
    extract_openai_model_ids,
    extract_openai_model_infos,
    merge_model_list_pages,
    validate_model_list_page,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.recovery import (
    OutputCapRecovery,
    ProviderRecovery,
    ReasoningStripRecovery,
    RecoveryLadder,
    RecoveryMemory,
)
from my_claude_code.providers.stream_recovery import (
    MIDSTREAM_RECOVERY_ATTEMPTS,
    RecoveryController,
    RecoveryFailureAction,
    TruncatedProviderStreamError,
    is_retryable_stream_error,
)

from .profiles import OpenAIChatProfile
from .request_policy import build_openai_chat_request_body
from .tool_calls import (
    OpenAIToolCallAssembler,
    all_emitted_tools_complete,
    has_committed_sse_output,
    iter_heuristic_tool_use_sse,
    started_tool_states,
    tool_call_extra_content,
)
from .usage import (
    clone_without_stream_usage,
    is_stream_usage_rejection,
    prompt_tokens_details,
    request_stream_usage,
    usage_int,
)

OpenAIAsyncCredentialProvider = Callable[[], Awaitable[str]]


class OpenAIChatProvider(BaseProvider):
    """OpenAI-compatible ``/chat/completions`` provider configured by a profile."""

    def __init__(
        self,
        config: ProviderConfig,
        *,
        profile: OpenAIChatProfile,
        rate_limiter: ProviderRateLimiter,
        default_headers: Mapping[str, str] | None = None,
        api_key_provider: OpenAIAsyncCredentialProvider | None = None,
        provider_id: str = "",
    ):
        super().__init__(config)
        self._profile = profile
        # The catalogue id (``nvidia_nim``), which is what the user's override
        # file keys on. ``_provider_name`` is the profile's log label (``NIM``)
        # and is deliberately not reused for it: several profiles share a label
        # and none of them are catalogue ids.
        self._provider_id = provider_id
        self._provider_name = profile.provider_name
        if config.api_key is None and api_key_provider is None:
            raise ValueError(
                f"{profile.provider_name} requires an API key or credential provider"
            )
        self._api_key = config.api_key
        self._base_url = profile.base_url(config.base_url).rstrip("/")
        # What this host has taught this process about itself: per-model output
        # caps it stated, and reasoning fields it refused. Per provider
        # instance and keyed by the bare model id, so a gateway that takes
        # ``reasoning_effort`` on one model and rejects it on another -- which
        # the OpenCode probe showed is real -- learns per model. The ISO date
        # is what the Models page shows.
        self._recovery_memory = RecoveryMemory()
        log_tag = f"{profile.provider_name}_STREAM"
        self._output_cap_recovery = OutputCapRecovery(
            self._recovery_memory, log_tag=log_tag
        )
        # Narrowest-and-most-certain first, generic reasoning strip last. See
        # ``providers/recovery/ladder.py`` for why that order is the whole
        # point of having a ladder rather than a chain of ``if``s.
        self._recovery_ladder = RecoveryLadder(
            (
                self._output_cap_recovery.rung(),
                ProviderRecovery(
                    kind="stream_usage", rewrite=self._rewrite_without_stream_usage
                ).rung(),
                ProviderRecovery(
                    kind="provider_specific", rewrite=self._get_retry_request_body
                ).rung(),
                ReasoningStripRecovery(log_tag=log_tag).rung(),
            )
        )
        self._rate_limiter = rate_limiter
        http_client = None
        if config.proxy:
            http_client = httpx.AsyncClient(
                proxy=config.proxy,
                timeout=httpx.Timeout(
                    config.http_read_timeout,
                    connect=config.http_connect_timeout,
                    read=config.http_read_timeout,
                    write=config.http_write_timeout,
                ),
            )
        self._client = AsyncOpenAI(
            api_key=api_key_provider or self._api_key,
            base_url=self._base_url,
            max_retries=0,
            default_headers=default_headers,
            timeout=httpx.Timeout(
                config.http_read_timeout,
                connect=config.http_connect_timeout,
                read=config.http_read_timeout,
                write=config.http_write_timeout,
            ),
            http_client=http_client,
        )

    @property
    def _model_output_caps(self) -> dict[str, int]:
        """The learned output caps, under the name this family has always used."""
        return self._recovery_memory.output_caps

    @property
    def _rejected_reasoning_fields(self) -> dict[str, dict[str, str]]:
        """The learned reasoning refusals, under this family's long-standing name."""
        return self._recovery_memory.rejected_reasoning_fields

    def reasoning_dialect(self, model_id: str) -> ReasoningDialect:
        """What this profile's encoder can put on the wire, for this model.

        Provider-wide by construction, minus anything this host has been
        measured refusing for this model. A learned rejection outranks the
        declaration: the profile is a claim about the gateway, a 400 is the
        gateway itself, and where they disagree the gateway is right.

        The per-model narrowing from the gateway's own ``supported_parameters``
        happens above this, in the provider manager, which is the layer that
        holds the model cache. Both only ever remove, so they compose in either
        order.
        """
        dialect = self._profile.reasoning.dialect
        rejections = self._recovery_memory.rejections_for(model_id)
        if not rejections:
            return dialect
        return narrow_dialect_by_rejections(dialect, rejections)

    def throttle_remaining(self, model: str | None = None) -> float:
        """Seconds this credential is rate-limited for; 0 when free to serve."""
        return self._rate_limiter.remaining_wait()

    async def cleanup(self) -> None:
        """Release HTTP client resources."""
        client = getattr(self, "_client", None)
        if client is not None:
            await client.close()

    async def list_models_payload(self) -> Any:
        """Return the raw OpenAI-compatible model-list payload."""
        return await self._client.models.list()

    async def list_model_ids(self) -> frozenset[str]:
        """Return model ids from the provider's OpenAI-compatible models endpoint."""
        payload = await self.list_models_payload()
        return extract_openai_model_ids(payload, provider_name=self._provider_name)

    async def list_model_infos(self) -> frozenset[ProviderModelInfo]:
        """Return model metadata from the OpenAI-compatible models endpoint."""
        payload = await self._list_models_payload()
        if not self._profile.model_ids_are_routable:
            return frozenset()
        listing = self._profile.model_listing
        return extract_openai_model_infos(
            payload,
            provider_name=self._provider_name,
            collection_field=listing.collection_field,
            id_field=listing.id_field,
            aliases_field=listing.aliases_field,
            required_path_values=listing.required_path_values,
            required_null_field=listing.required_null_field,
            required_sequence_items=listing.required_sequence_items,
            exclude_missing_sequence_fields=listing.exclude_missing_sequence_fields,
            tags_field=listing.tags_field,
            thinking_tag=listing.thinking_tag,
            non_thinking_tag=listing.non_thinking_tag,
            thinking_boolean_path=listing.thinking_boolean_path,
        )

    async def _list_models_payload(self) -> Any:
        """Fetch one OpenAI-compatible model-list payload through the fetcher."""
        return await self._fetch_models_payload()

    async def _fetch_models_payload(self) -> Any:
        """Fetch the profile-selected model-list endpoint once."""
        listing = self._profile.model_listing
        path = listing.path
        if path is None:
            return await self.list_models_payload()
        if listing.pagination is not None:
            return await self._fetch_paginated_models_payload(path)
        if listing.query_params:
            return await self._client.get(
                path,
                cast_to=object,
                options={"params": dict(listing.query_params)},
            )
        return await self._client.get(path, cast_to=object)

    async def _fetch_paginated_models_payload(self, path: str) -> Any:
        """Fetch one complete bounded model catalog."""
        listing = self._profile.model_listing
        pagination = listing.pagination
        if pagination is None:
            raise RuntimeError("paginated model fetch requires a pagination policy")

        payloads: list[Any] = []
        total_pages: int | None = None
        page = pagination.first_page
        while total_pages is None or page < pagination.first_page + total_pages:
            params = dict(listing.query_params)
            params[pagination.page_param] = str(page)
            payload = await self._client.get(
                path,
                cast_to=object,
                options={"params": params},
            )
            total_pages = validate_model_list_page(
                payload,
                provider_name=self._provider_name,
                expected_page=page,
                current_page_path=pagination.current_page_path,
                total_pages_path=pagination.total_pages_path,
                max_pages=pagination.max_pages,
                expected_total_pages=total_pages,
            )
            payloads.append(payload)
            page += 1

        return merge_model_list_pages(
            payloads,
            provider_name=self._provider_name,
            collection_field=listing.collection_field,
        )

    def _build_request_body(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> dict[str, Any]:
        """Build a provider request from the immutable profile."""
        return build_openai_chat_request_body(
            request,
            reasoning=reasoning,
            policy=self._profile.request_policy,
            postprocessors=self._profile.request_postprocessors,
            provider_id=self._provider_id,
        )

    def preflight_stream(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> None:
        """Validate OpenAI-chat request conversion before streaming."""
        self._build_request_body(request, reasoning=reasoning)

    def _handle_extra_reasoning(
        self, delta: Any, ledger: AnthropicStreamLedger, *, output_reasoning: bool
    ) -> Iterator[str]:
        """Hook for provider-specific reasoning."""
        return iter(())

    def _get_retry_request_body(self, error: Exception, body: dict) -> dict | None:
        """Return a modified request body for one retry, or None."""
        return None

    def _provider_failure_override(self, error: Exception) -> ExecutionFailure | None:
        """Return provider-specific failure semantics, or defer to shared policy."""
        return None

    def _prepare_create_body(self, body: dict[str, Any]) -> dict[str, Any]:
        """Return the body passed to the upstream OpenAI-compatible client."""
        return body

    def _record_tool_call_extra_content(
        self, tool_call_id: str, extra_content: dict[str, Any]
    ) -> None:
        """Hook for providers that must replay OpenAI tool-call metadata later."""

    def _tool_argument_aliases(self, body: dict[str, Any]) -> dict[str, dict[str, str]]:
        """Return provider-specific per-tool argument aliases for this request."""
        return {}

    def _anthropic_usage_fields(self, usage_info: Any) -> dict[str, int]:
        """Return provider-specific Anthropic usage fields for final SSE usage.

        The OpenAI schema reports cache hits under
        ``usage.prompt_tokens_details.cached_tokens``, and every
        OpenAI-compatible upstream that supports prompt caching uses it --
        NVIDIA NIM included. Reading it here means the whole family reports
        cache usage instead of each provider re-implementing the same lookup.

        Note ``prompt_tokens`` already *includes* the cached tokens, so this is
        a breakdown of the existing input count, not an addition to it. The
        streaming path subtracts the read count back out before emitting
        Anthropic's ``input_tokens``, which excludes it by definition.
        """
        details = prompt_tokens_details(usage_info)
        fields: dict[str, int] = {}
        cached = usage_int(details, "cached_tokens")
        if cached is not None:
            fields["cache_read_input_tokens"] = cached
        # OpenRouter reports the write side here too, under the name Anthropic
        # calls cache creation. Providers that never write leave it absent.
        written = usage_int(details, "cache_write_tokens")
        if written is not None:
            fields["cache_creation_input_tokens"] = written
        return fields

    async def _create_stream(self, body: dict) -> tuple[Any, dict]:
        """Create a streaming chat completion with bounded request fallbacks."""
        body = self._output_cap_recovery.apply_learned(body)
        used_retry_kinds: set[str] = set()
        # Per attempt-chain, deliberately a local rather than instance state:
        # concurrent requests share this provider, and unlike the monotonic
        # output-cap table this value is consumed once the retry is accepted.
        stripped_reasoning: str | None = None

        while True:
            try:
                create_body = self._prepare_create_body(body)
                # The commit boundary. Everything upstream of this line can
                # still change the body -- base conversion, common policy,
                # provider postprocessors, the override postprocessor, tool
                # name encoding, the learned output cap and the create-level
                # retry rewrites all run before it -- and nothing downstream
                # can. ``stream`` is passed as a keyword, so it is recorded
                # alongside rather than read back out of the dict.
                record_wire_request(create_body, stream=True)
                stream = await self._rate_limiter.execute_with_retry(
                    self._client.chat.completions.create,
                    provider_failure_override=self._provider_failure_override,
                    **create_body,
                    stream=True,
                )
                if stripped_reasoning is not None:
                    self._remember_reasoning_rejection(body, stripped_reasoning)
                return stream, body
            except Exception as error:
                decision = self._recovery_ladder.next_body(
                    error, body, used_retry_kinds
                )
                if decision.body is None:
                    raise
                if decision.stripped_reasoning_field is not None:
                    stripped_reasoning = decision.stripped_reasoning_field
                body = decision.body

    def _rewrite_without_stream_usage(
        self, error: Exception, body: dict
    ) -> dict | None:
        """Drop ``stream_options.include_usage`` when the SDK names that shape.

        A rung of this provider's ladder rather than a matcher of its own: the
        judgement is the OpenAI SDK's, not a reading of the host's words.
        """
        if not is_stream_usage_rejection(error):
            return None
        retry_body = clone_without_stream_usage(body)
        if retry_body is None:
            return None
        logger.warning(
            "{}_STREAM: retrying without stream_options.include_usage "
            "after upstream rejection",
            self._provider_name,
        )
        return retry_body

    def _remember_reasoning_rejection(self, body: dict, field: str) -> None:
        """Record that this model refused a reasoning field, once it is proven.

        Reached only after the stripped body was accepted, so the strip is what
        fixed it. Later requests skip the field without paying the 400: the
        dialect this provider reports no longer claims it (see
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
        """Stream response in Anthropic SSE format."""
        runner = _OpenAIChatStreamRunner(
            self,
            request=request,
            input_tokens=input_tokens,
            request_id=request_id,
            reasoning=reasoning,
        )
        return runner.run()


class _OpenAIChatStreamRunner:
    """Own one OpenAI-chat request's stream, parsing, and recovery state."""

    def __init__(
        self,
        provider: OpenAIChatProvider,
        *,
        request: MessagesRequest,
        input_tokens: int,
        request_id: str | None,
        reasoning: ReasoningPolicy,
    ) -> None:
        self._provider = provider
        self._request = request
        self._input_tokens = input_tokens
        self._request_id = request_id
        self._reasoning = reasoning
        self._message_id = f"msg_{uuid.uuid4()}"
        self._tool_calls = OpenAIToolCallAssembler(
            record_extra_content=provider._record_tool_call_extra_content
        )

    async def run(self) -> AsyncIterator[str]:
        """Convert the upstream OpenAI-chat stream into Anthropic SSE."""
        tag = self._provider._provider_name
        req_tag = f" request_id={self._request_id}" if self._request_id else ""
        ledger = self._new_ledger()
        config = self._provider._config
        recovery = RecoveryController(
            provider_name=tag,
            request_id=self._request_id,
            holdback_seconds=config.commit_holdback_seconds,
            holdback_chars=config.commit_holdback_chars,
            reasoning_commits=not config.fallback_on_reasoning_only,
            early_retry_attempts=config.early_retry_attempts,
            midstream_recovery_attempts=config.midstream_recovery_attempts,
        )

        def hold_event(event: str) -> Iterator[str]:
            yield from recovery.push(event)

        def hold_events(events: Iterator[str]) -> Iterator[str]:
            for event in events:
                yield from hold_event(event)

        body = self._provider._build_request_body(
            self._request,
            reasoning=self._reasoning,
        )
        request_stream_usage(body)
        output_reasoning = self._reasoning.output_enabled
        trace_event(
            stage="provider",
            event="provider.request.sent",
            source="provider",
            provider=tag,
            request_id=self._request_id,
            gateway_model=self._request.model,
            downstream_model=body.get("model"),
            message_count=len(body.get("messages", [])),
            tool_count=len(body.get("tools", [])),
            body=provider_chat_body_snapshot(body),
        )

        think_parser = ThinkTagParser()
        heuristic_parser = HeuristicToolParser()
        finish_reason = None
        usage_info = None
        tool_argument_aliases: dict[str, dict[str, str]] = {}
        tool_argument_alias_buffers: dict[int, str] = {}

        async with self._provider._rate_limiter.concurrency_slot():
            while True:
                if not ledger.message_started:
                    for event in hold_event(ledger.message_start()):
                        yield event
                stream: Any | None = None
                stream_opened = False
                shape: ResponseShape | None = None
                try:
                    stream, body = await self._provider._create_stream(body)
                    stream_opened = True
                    # Opposite the wire body recorded at the commit boundary:
                    # what came back, by shape only. Started here because a
                    # create that never opened has no reply to describe.
                    shape = start_response_shape()
                    # Only now can upstream bytes arrive, so only now does the
                    # holdback window mean anything.
                    recovery.upstream_opened()
                    tool_argument_aliases = self._provider._tool_argument_aliases(body)
                    async for chunk in stream:
                        if shape is not None:
                            shape.note_chunk(time.monotonic())
                        chunk_usage = getattr(chunk, "usage", None)
                        if chunk_usage is not None:
                            usage_info = chunk_usage
                            if shape is not None:
                                shape.note_usage(chunk_usage)

                        if not chunk.choices:
                            continue

                        choice = chunk.choices[0]
                        delta = choice.delta
                        if delta is None:
                            continue

                        if choice.finish_reason:
                            finish_reason = choice.finish_reason
                            if shape is not None:
                                shape.note_finish(finish_reason)
                            logger.debug("{} finish_reason: {}", tag, finish_reason)

                        reasoning = self._provider._profile.reasoning_delta(delta)
                        if shape is not None and reasoning:
                            shape.note_field(
                                self._provider._profile.reasoning_delta_field,
                                len(reasoning),
                            )
                        if shape is not None and delta.content:
                            shape.note_field("content", len(delta.content))
                        if shape is not None and delta.tool_calls:
                            shape.note_field("tool_calls", len(delta.tool_calls))
                        if output_reasoning and reasoning is not None:
                            for event in hold_events(ledger.ensure_thinking_block()):
                                yield event
                            if reasoning:
                                for event in hold_event(
                                    ledger.emit_thinking_delta(reasoning)
                                ):
                                    yield event

                        for event in self._provider._handle_extra_reasoning(
                            delta,
                            ledger,
                            output_reasoning=output_reasoning,
                        ):
                            for out_event in hold_event(event):
                                yield out_event

                        if delta.content:
                            for part in think_parser.feed(delta.content):
                                if part.type == ContentType.THINKING:
                                    if not output_reasoning:
                                        continue
                                    for event in hold_events(
                                        ledger.ensure_thinking_block()
                                    ):
                                        yield event
                                    for event in hold_event(
                                        ledger.emit_thinking_delta(part.content)
                                    ):
                                        yield event
                                else:
                                    (
                                        filtered_text,
                                        detected_tools,
                                    ) = heuristic_parser.feed(part.content)

                                    if filtered_text:
                                        for event in hold_events(
                                            ledger.ensure_text_block()
                                        ):
                                            yield event
                                        for event in hold_event(
                                            ledger.emit_text_delta(filtered_text)
                                        ):
                                            yield event

                                    for tool_use in detected_tools:
                                        for event in iter_heuristic_tool_use_sse(
                                            ledger, tool_use
                                        ):
                                            for out_event in hold_event(event):
                                                yield out_event

                        if delta.tool_calls:
                            for event in hold_events(ledger.close_content_blocks()):
                                yield event
                            for tool_call in delta.tool_calls:
                                extra_content = tool_call_extra_content(tool_call)
                                tool_call_info = {
                                    "index": tool_call.index,
                                    "id": tool_call.id,
                                    "function": {
                                        "name": tool_call.function.name,
                                        "arguments": tool_call.function.arguments,
                                    },
                                }
                                if extra_content:
                                    tool_call_info["extra_content"] = extra_content
                                for event in self._tool_calls.process_tool_call(
                                    tool_call_info,
                                    ledger,
                                    tool_argument_aliases=tool_argument_aliases,
                                    tool_argument_alias_buffers=tool_argument_alias_buffers,
                                ):
                                    if event == REASONING_HEARTBEAT:
                                        # Not SSE and not output: it says the
                                        # stream is alive while arguments
                                        # buffer. Pushing it into the holdback
                                        # would commit the route on nothing.
                                        yield event
                                        continue
                                    for out_event in hold_event(event):
                                        yield out_event

                    if finish_reason is None:
                        raise TruncatedProviderStreamError(
                            "Provider stream ended without finish_reason."
                        )
                    record_response_shape(shape)
                    break

                except asyncio.CancelledError, GeneratorExit:
                    raise
                except Exception as error:
                    generated_output = has_committed_sse_output(ledger)
                    complete_tool_salvageable = (
                        generated_output
                        and ledger.has_emitted_tool_block()
                        and all_emitted_tools_complete(ledger, self._request)
                    )
                    decision = recovery.advance_failure(
                        error,
                        stream_opened=stream_opened,
                        generated_output=generated_output,
                        complete_tool_salvageable=complete_tool_salvageable,
                    )
                    if decision.action == RecoveryFailureAction.EARLY_RETRY:
                        record_recovery_event(RECOVERY_EARLY_RETRIES)
                        ledger = self._new_ledger()
                        think_parser = ThinkTagParser()
                        heuristic_parser = HeuristicToolParser()
                        finish_reason = None
                        usage_info = None
                        tool_argument_aliases = {}
                        tool_argument_alias_buffers = {}
                        continue

                    if decision.action == RecoveryFailureAction.MIDSTREAM_RECOVERY:
                        try:
                            recovery_events = await self._recovery_events(
                                body=body,
                                ledger=ledger,
                                error=error,
                                tool_argument_alias_buffers=tool_argument_alias_buffers,
                                output_reasoning=output_reasoning,
                            )
                        except Exception as recovery_error:
                            trace_event(
                                stage="provider",
                                event="provider.recovery.failed",
                                source="provider",
                                provider=tag,
                                request_id=self._request_id,
                                exc_type=type(recovery_error).__name__,
                            )
                            recovery_events = None
                        if recovery_events is not None:
                            record_recovery_event(RECOVERY_MIDSTREAM_RECOVERIES)
                            for event in recovery.flush_uncommitted(decision):
                                yield event
                            for event in recovery_events:
                                yield event
                            return

                    self._provider._log_stream_transport_error(
                        tag, req_tag, error, request_id=self._request_id
                    )
                    failure = classify_provider_failure(
                        error,
                        provider_name=tag,
                        read_timeout_s=self._provider._config.http_read_timeout,
                        request_id=self._request_id,
                        mark_rate_limited=(
                            self._provider._rate_limiter.extend_reactive_block
                        ),
                        provider_failure_override=(
                            self._provider._provider_failure_override
                        ),
                        cooldown_seconds=config.rate_limit_cooldown_seconds,
                        mark_rate_limited_enabled=not config.routes_around_model,
                    )
                    error_trace: dict[str, Any] = {
                        "stage": "provider",
                        "event": "provider.response.error",
                        "source": "provider",
                        "provider": tag,
                        "request_id": self._request_id,
                        "exc_type": type(error).__name__,
                        "failure_kind": failure.kind.value,
                        "status_code": failure.status_code,
                        "provider_retryable": failure.retryable,
                    }
                    if self._provider._config.log_api_error_tracebacks:
                        error_trace["error_message"] = failure.message
                    trace_event(**error_trace)
                    if (
                        not decision.committed
                        and decision.has_buffered
                        and complete_tool_salvageable
                    ):
                        for event in recovery.flush():
                            yield event
                    elif not decision.committed:
                        recovery.discard()
                        raise failure from error
                    for event in ledger.close_unclosed_blocks():
                        yield event
                    raise failure from error
                finally:
                    if stream is not None:
                        await close_provider_stream(
                            stream,
                            active_error=sys.exception(),
                            provider_name=tag,
                            request_id=self._request_id,
                        )

        remaining = think_parser.flush()
        if remaining:
            if remaining.type == ContentType.THINKING:
                if not output_reasoning:
                    remaining = None
                else:
                    for event in hold_events(ledger.ensure_thinking_block()):
                        yield event
                    for event in hold_event(
                        ledger.emit_thinking_delta(remaining.content)
                    ):
                        yield event
            if remaining and remaining.type == ContentType.TEXT:
                for event in hold_events(ledger.ensure_text_block()):
                    yield event
                for event in hold_event(ledger.emit_text_delta(remaining.content)):
                    yield event

        for tool_use in heuristic_parser.flush():
            for event in iter_heuristic_tool_use_sse(ledger, tool_use):
                for out_event in hold_event(event):
                    yield out_event

        has_emitted_tool = ledger.has_emitted_tool_block()
        has_content_blocks = (
            ledger.blocks.text_index != -1
            or ledger.blocks.thinking_index != -1
            or has_emitted_tool
        )
        if not has_content_blocks or (
            not has_emitted_tool
            and not ledger.accumulated_text.strip()
            and ledger.accumulated_reasoning.strip()
        ):
            for event in hold_events(ledger.ensure_text_block()):
                yield event
            for event in hold_event(ledger.emit_text_delta(" ")):
                yield event

        for event in self._tool_calls.flush_tool_argument_alias_buffers(
            ledger, tool_argument_aliases, tool_argument_alias_buffers
        ):
            for out_event in hold_event(event):
                yield out_event

        for event in self._tool_calls.flush_task_arg_buffers(ledger):
            for out_event in hold_event(event):
                yield out_event

        for event in hold_events(ledger.close_all_blocks()):
            yield event

        completion = usage_int(usage_info, "completion_tokens")
        if isinstance(completion, int):
            output_tokens = completion
        else:
            output_tokens = ledger.estimate_output_tokens()
        provider_input = usage_int(usage_info, "prompt_tokens")
        if provider_input is not None:
            logger.debug(
                "TOKEN_ESTIMATE: our={} provider={} diff={:+d}",
                self._input_tokens,
                provider_input,
                provider_input - self._input_tokens,
            )
        usage_fields = self._provider._anthropic_usage_fields(usage_info)
        input_tokens = (
            provider_input if provider_input is not None else self._input_tokens
        )
        cache_read = usage_fields.get("cache_read_input_tokens")
        if provider_input is not None and isinstance(cache_read, int):
            # The two protocols count this differently: an OpenAI-family
            # ``prompt_tokens`` *includes* the tokens served from cache, while
            # Anthropic's ``input_tokens`` excludes them and expects the caller
            # to add ``cache_read_input_tokens`` back for the total. Emitting
            # the OpenAI number under the Anthropic name double-counts every
            # cache hit, which on a warm 268k-token prompt is nearly 2x.
            input_tokens = max(0, provider_input - cache_read)
        trace_event(
            stage="provider",
            event="provider.response.completed",
            source="provider",
            provider=tag,
            request_id=self._request_id,
            finish_reason=(None if finish_reason is None else str(finish_reason)),
            output_tokens=output_tokens,
            prompt_tokens=input_tokens,
            prompt_tokens_estimate=self._input_tokens,
        )
        for event in hold_event(
            ledger.message_delta(
                ledger.final_stop_reason(map_stop_reason(finish_reason)),
                output_tokens,
                input_tokens=input_tokens,
                usage_fields=self._provider._anthropic_usage_fields(usage_info),
            )
        ):
            yield event
        for event in hold_event(ledger.message_stop()):
            yield event
        for event in recovery.flush():
            yield event

    async def _collect_recovery_text(
        self, body: dict[str, Any], *, include_reasoning: bool
    ) -> tuple[str, str]:
        """Collect a complete text/reasoning continuation stream."""
        last_error: Exception | None = None
        for attempt in range(MIDSTREAM_RECOVERY_ATTEMPTS):
            stream: Any | None = None
            try:
                stream, _ = await self._provider._create_stream(body)
                text_parts: list[str] = []
                thinking_parts: list[str] = []
                terminal_seen = False
                async for chunk in stream:
                    if not getattr(chunk, "choices", None):
                        continue
                    choice = chunk.choices[0]
                    if choice.finish_reason is not None:
                        terminal_seen = True
                    delta = choice.delta
                    if delta is None:
                        continue
                    if include_reasoning:
                        reasoning = self._provider._profile.reasoning_delta(delta)
                        if reasoning:
                            thinking_parts.append(reasoning)
                    content = getattr(delta, "content", None)
                    if isinstance(content, str) and content:
                        text_parts.append(content)
                if not terminal_seen:
                    raise TruncatedProviderStreamError(
                        "Recovery stream ended without finish_reason."
                    )
                return "".join(text_parts), "".join(thinking_parts)
            except Exception as error:
                last_error = error
                if not is_retryable_stream_error(error):
                    raise
                trace_event(
                    stage="provider",
                    event="provider.recovery.retry",
                    source="provider",
                    provider=self._provider._provider_name,
                    recovery_kind="openai_text",
                    attempt=attempt + 1,
                    max_attempts=MIDSTREAM_RECOVERY_ATTEMPTS,
                    exc_type=type(error).__name__,
                )
            finally:
                if stream is not None:
                    await maybe_await_aclose(stream)
        if last_error is not None:
            raise last_error
        return "", ""

    async def _recovery_events(
        self,
        *,
        body: dict[str, Any],
        ledger: AnthropicStreamLedger,
        error: Exception,
        tool_argument_alias_buffers: dict[int, str],
        output_reasoning: bool,
    ) -> list[str] | None:
        """Build terminal recovery events when the interrupted stream permits it."""
        if not is_retryable_stream_error(error):
            return None

        if ledger.has_emitted_tool_block():
            if not all_emitted_tools_complete(ledger, self._request):
                repair_events = await self._repair_tool_args(
                    body=body,
                    ledger=ledger,
                    tool_argument_alias_buffers=tool_argument_alias_buffers,
                )
                if repair_events is None:
                    return None
            else:
                repair_events = []
            events = list(repair_events)
            events.extend(ledger.close_all_blocks())
            events.append(
                ledger.message_delta(
                    ledger.final_stop_reason("end_turn"),
                    ledger.estimate_output_tokens(),
                )
            )
            events.append(ledger.message_stop())
            trace_event(
                stage="provider",
                event="provider.recovery.tool_salvaged",
                source="provider",
                provider=self._provider._provider_name,
                request_id=self._request_id,
            )
            record_recovery_event(RECOVERY_SALVAGES)
            return events

        partial_text = ledger.accumulated_text
        partial_thinking = ledger.accumulated_reasoning
        if not partial_text and not partial_thinking:
            return None

        recovery_body = make_text_recovery_body(body, partial_text, partial_thinking)
        text, thinking = await self._collect_recovery_text(
            recovery_body, include_reasoning=output_reasoning
        )
        text_suffix = continuation_suffix(partial_text, text)
        thinking_suffix = continuation_suffix(partial_thinking, thinking)
        events: list[str] = []
        if thinking_suffix:
            events.extend(ledger.ensure_thinking_block())
            events.append(ledger.emit_thinking_delta(thinking_suffix))
        if text_suffix:
            events.extend(ledger.ensure_text_block())
            events.append(ledger.emit_text_delta(text_suffix))
        if not events:
            return None
        events.extend(ledger.close_all_blocks())
        events.append(
            ledger.message_delta(
                ledger.final_stop_reason("end_turn"), ledger.estimate_output_tokens()
            )
        )
        events.append(ledger.message_stop())
        trace_event(
            stage="provider",
            event="provider.recovery.continued",
            source="provider",
            provider=self._provider._provider_name,
            request_id=self._request_id,
        )
        record_recovery_event(RECOVERY_SALVAGES)
        return events

    async def _repair_tool_args(
        self,
        *,
        body: dict[str, Any],
        ledger: AnthropicStreamLedger,
        tool_argument_alias_buffers: dict[int, str],
    ) -> list[str] | None:
        schemas = tool_schemas_by_name(self._request)
        events: list[str] = []
        for tool_index, state in started_tool_states(ledger):
            block = ledger.tool_block_for_tool_index(tool_index)
            emitted_prefix = block.content if block is not None else ""
            repair_prefix = emitted_prefix
            if not repair_prefix and state.name == "Task" and state.task_arg_buffer:
                repair_prefix = state.task_arg_buffer
            if not repair_prefix and tool_index in tool_argument_alias_buffers:
                repair_prefix = tool_argument_alias_buffers[tool_index]
            if (
                parse_complete_tool_input(repair_prefix, state.name, schemas)
                is not None
            ):
                if not emitted_prefix and repair_prefix:
                    events.append(ledger.emit_tool_delta(tool_index, repair_prefix))
                continue

            schema = schemas.get(state.name)
            recovery_body = make_tool_repair_body(
                body,
                tool_name=state.name,
                prefix=repair_prefix,
                input_schema=schema.input_schema if schema is not None else None,
            )
            accepted_suffix: str | None = None
            for attempt in range(MIDSTREAM_RECOVERY_ATTEMPTS):
                text, _ = await self._collect_recovery_text(
                    recovery_body, include_reasoning=False
                )
                repair = accept_tool_json_repair(
                    repair_prefix,
                    text,
                    tool_name=state.name,
                    schemas=schemas,
                )
                if repair is not None:
                    accepted_suffix = repair.suffix
                    trace_event(
                        stage="provider",
                        event="provider.recovery.tool_repaired",
                        source="provider",
                        provider=self._provider._provider_name,
                        tool_name=state.name,
                        attempt=attempt + 1,
                    )
                    break
            if accepted_suffix is None:
                return None
            to_emit = (
                accepted_suffix if emitted_prefix else repair_prefix + accepted_suffix
            )
            if to_emit:
                events.append(ledger.emit_tool_delta(tool_index, to_emit))
        if not all_emitted_tools_complete(ledger, self._request):
            return None
        return events

    def _new_ledger(self) -> AnthropicStreamLedger:
        return AnthropicStreamLedger(
            self._message_id,
            self._request.model,
            self._input_tokens,
            log_raw_events=self._provider._config.log_raw_sse_events,
        )
