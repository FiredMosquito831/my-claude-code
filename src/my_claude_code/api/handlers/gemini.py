"""Google Gemini API product flow for Gemini-protocol clients."""

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

from fastapi.responses import JSONResponse

from my_claude_code.api.request_capture import RequestCapture, build_capture
from my_claude_code.api.request_errors import (
    http_status_for_unexpected_api_exception,
    log_unexpected_api_exception,
    require_non_empty_messages,
)
from my_claude_code.api.request_ids import new_request_id
from my_claude_code.api.response_streams import (
    StreamKeepalive,
    openai_sse_streaming_response,
    terminal_execution_error_response,
    trace_terminal_execution_error,
)
from my_claude_code.application.errors import ApplicationError, InvalidRequestError
from my_claude_code.application.execution import (
    ProviderExecutor,
    route_execution_policy,
    route_health_registry,
)
from my_claude_code.application.ports import ProviderResolver
from my_claude_code.application.prompt_cache import stamp_prompt_cache_ask
from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import (
    MessagesRequest,
    aggregate_anthropic_sse_to_message,
)
from my_claude_code.core.carrier_surface import GEMINI_THOUGHT_UNSIGNED
from my_claude_code.core.client_fingerprint import harness_from_headers
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import ExecutionFailure, find_execution_failure
from my_claude_code.core.gemini_api import (
    GeminiApiAdapter,
    GeminiGenerateContentRequest,
    GeminiWebSearchSpec,
    build_gemini_payload_from_loop,
    collect_gemini_final_text,
    extract_gemini_web_search_spec,
    gemini_failure_payload,
    gemini_status_for_failure,
    iter_gemini_sse_from_loop,
)
from my_claude_code.core.trace import trace_event


class GeminiHandler:
    """Handle Gemini ``generateContent`` requests, streaming and not.

    A near twin of :class:`ChatCompletionsHandler` by design: it translates a
    Gemini-shaped request into one ``MessagesRequest`` and hands it to the same
    :class:`ProviderExecutor`, so routing, fallback, pause, reasoning gating,
    output budgets, wire capture, the upstream ladder, the request log and the
    optimizer rules apply to all four inbound surfaces identically.

    Two things are its own. The endpoint recorded in the request log carries
    the model, because ``/v1beta/models/{model}:generateContent`` *is* the
    path and collapsing it to a constant would hide which model a row was for.
    And a request that dropped a Google-hosted tool traces what it dropped:
    that is the only place a user can learn why the model never searched.
    """

    def __init__(
        self,
        settings: Settings,
        provider_resolver: ProviderResolver,
        *,
        model_router: ModelRouter | None = None,
        gemini_adapter: GeminiApiAdapter | None = None,
        provider_executor: ProviderExecutor | None = None,
        generation_id: int | None = None,
    ) -> None:
        self._settings = settings
        self._model_router = model_router or ModelRouter(settings)
        self._gemini_adapter = gemini_adapter or GeminiApiAdapter()

        self._provider_executor = provider_executor or ProviderExecutor(
            provider_resolver,
            policy=route_execution_policy(settings),
            health=route_health_registry(settings),
            generation_id=generation_id,
            log_raw_payloads=settings.log_raw_api_payloads,
            retry_first=settings.fallback_retry_first,
            provider_lookup=self._throttle_lookup,
        )

    def _throttle_lookup(self, provider_id: str) -> float | None:
        """Return provider throttle seconds for a provider_id, or None.

        See ``MessagesHandler._throttle_lookup`` for the full contract.
        """
        try:
            provider = self._provider_executor._provider_resolver(provider_id)
        except Exception:
            return None
        try:
            return provider.throttle_remaining()
        except Exception:
            return None

    async def create(
        self,
        request_data: GeminiGenerateContentRequest,
        *,
        endpoint: str,
        stream: bool,
        request_id: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> object:
        """Create a Gemini response, streamed or complete."""
        request_id = request_id or new_request_id()
        request_payload = request_data.model_dump(mode="json", exclude_none=True)
        response_id = self._gemini_adapter.new_response_id()

        allow_web_search = bool(self._settings.web_search_gemini_enabled)

        capture: RequestCapture | None = None
        try:
            conversion = self._gemini_adapter.to_anthropic_payload(
                request_data,
                allow_web_search=allow_web_search,
            )
            self._trace_dropped(conversion.dropped, model=request_data.model)
            web_search_spec = extract_gemini_web_search_spec(
                request_data.tools,
                request_data.tool_config,
                allow_web_search=allow_web_search,
            )
            gemini_request = MessagesRequest(**conversion.payload)
            stamp_prompt_cache_ask(self._settings, gemini_request)
            capture = build_capture(
                self._settings,
                gemini_request,
                request_id=request_id,
                endpoint=endpoint,
                protocol="gemini",
                headers=headers,
                stream=stream,
                # 8.20.0: the two ingress cells this door drops (a replayed
                # ``thought: true`` part and every ``thoughtSignature``), counted
                # by the conversion that drops them.
                carrier_surface=conversion.carrier_surface,
                # The egress cell: a thought this door emits carries no
                # ``thoughtSignature``, so the client has nothing to echo back.
                # Only when the client asked for thoughts -- a request that did
                # not ask emits no thought part at all, and counting one would
                # claim a drop that never happened.
                carrier_surface_egress=(
                    frozenset({GEMINI_THOUGHT_UNSIGNED})
                    if conversion.include_thoughts
                    else frozenset()
                ),
            )
            require_non_empty_messages(gemini_request.messages)
            harness = harness_from_headers(headers).harness
            if web_search_spec is not None:
                return await self._create_web_search_response(
                    request_data,
                    gemini_request,
                    web_search_spec,
                    stream=stream,
                    response_id=response_id,
                    include_thoughts=conversion.include_thoughts,
                    request_payload=request_payload,
                    request_id=request_id,
                    harness=harness,
                    capture=capture,
                )

            plan = self._model_router.resolve_messages_plan(
                gemini_request, harness=harness
            )
            capture.set_plan(plan)
            capture.set_routing(plan.primary)

            streamed = capture.wrap(
                self._provider_executor.stream(
                    plan,
                    wire_api="gemini",
                    raw_log_label="FULL_GEMINI_PAYLOAD",
                    raw_log_payload=request_payload,
                    request_id=request_id,
                    on_attempt=capture.set_routing,
                    on_attempt_result=capture.record_attempt_result,
                ),
                discard_after_terminal=True,
            )
            if not stream:
                return await self._complete_response(
                    streamed,
                    model=request_data.model,
                    response_id=response_id,
                    include_thoughts=conversion.include_thoughts,
                    request_id=request_id,
                )
            return await openai_sse_streaming_response(
                self._gemini_adapter.iter_sse_from_anthropic(
                    streamed,
                    model=request_data.model,
                    response_id=response_id,
                    include_thoughts=conversion.include_thoughts,
                    on_post_start_terminal_failure=lambda exc: (
                        self._trace_post_start_terminal_failure(
                            exc, request_id=request_id
                        )
                    ),
                ),
                headers=self._gemini_adapter.sse_headers,
                pre_start_error_response=lambda exc: self._pre_start_error_response(
                    exc, request_id=request_id
                ),
                keepalive=StreamKeepalive.from_settings(self._settings),
            )
        except GeminiApiAdapter.ConversionError as exc:
            raise InvalidRequestError(str(exc)) from exc
        except ApplicationError as exc:
            if capture is not None:
                capture.finish_error(exc)
            raise
        except ExecutionFailure as exc:
            if capture is not None:
                capture.finish_error(exc)
            return self._execution_failure_response(exc, request_id=request_id)
        except Exception as exc:
            failure = find_execution_failure(exc)
            if capture is not None:
                capture.finish_error(failure if failure is not None else exc)
            if failure is not None:
                return self._execution_failure_response(failure, request_id=request_id)
            log_unexpected_api_exception(
                self._settings,
                exc,
                context="CREATE_GEMINI_CONTENT_ERROR",
            )
            return JSONResponse(
                status_code=http_status_for_unexpected_api_exception(exc),
                content=self._gemini_adapter.error_payload(
                    message=safe_exception_message(exc),
                    code=http_status_for_unexpected_api_exception(exc),
                ),
            )

    async def _create_web_search_response(
        self,
        request_data: GeminiGenerateContentRequest,
        gemini_request: MessagesRequest,
        spec: GeminiWebSearchSpec,
        *,
        stream: bool,
        response_id: str,
        include_thoughts: bool,
        request_payload: dict[str, Any],
        request_id: str,
        harness: str | None,
        capture: RequestCapture,
    ) -> object:
        """Execute Gemini ``googleSearch``/``urlContext`` tools through MCC's local loop."""
        if self._settings.web_search_door_mode == "passthrough":
            raise InvalidRequestError(
                "WEB_SEARCH_DOOR_MODE='passthrough' is not supported on /v1beta Gemini "
                "endpoints; use 'local' or 'auto' instead."
            )

        from my_claude_code.api.web_tools.request import WebSearchToolOptions
        from my_claude_code.api.web_tools.tool_loop import (
            WebToolLoopConfig,
            extract_grounded_citations,
            iter_anthropic_sse_from_loop,
            run_routed_web_tool_loop,
        )

        config = WebToolLoopConfig(
            enable_search=spec.enable_search,
            enable_fetch=spec.enable_fetch,
            search_options=WebSearchToolOptions(blocked_domains=spec.blocked_domains),
            max_steps=self._settings.web_search_tool_loop_max_steps,
        )
        loop_result = await run_routed_web_tool_loop(
            gemini_request,
            config,
            self._settings,
            model_router=self._model_router,
            provider_executor=self._provider_executor,
            wire_api="gemini",
            request_id=request_id,
            raw_log_label="FULL_GEMINI_PAYLOAD",
            raw_log_payload=request_payload,
            capture=capture,
            harness=harness,
        )
        # Feed RequestCapture through the canonical Anthropic SSE observer so tokens/cost/text record cleanly.
        async for _frame in capture.wrap(
            iter_anthropic_sse_from_loop(
                loop_result, response_model=request_data.model
            ),
            discard_after_terminal=True,
        ):
            pass

        full_text = collect_gemini_final_text(loop_result.final_step_blocks)
        citations = extract_grounded_citations(full_text, loop_result.gathered_sources)

        if not stream:
            return JSONResponse(
                content=build_gemini_payload_from_loop(
                    model=request_data.model,
                    response_id=response_id,
                    include_thoughts=include_thoughts,
                    stop_reason=loop_result.stop_reason,
                    web_calls=loop_result.web_calls,
                    gathered_sources=loop_result.gathered_sources,
                    final_step_blocks=loop_result.final_step_blocks,
                    grounded_citations=citations,
                    loop_usage=loop_result.usage,
                )
            )

        return await openai_sse_streaming_response(
            iter_gemini_sse_from_loop(
                model=request_data.model,
                response_id=response_id,
                include_thoughts=include_thoughts,
                stop_reason=loop_result.stop_reason,
                web_calls=loop_result.web_calls,
                gathered_sources=loop_result.gathered_sources,
                final_step_blocks=loop_result.final_step_blocks,
                grounded_citations=citations,
                loop_usage=loop_result.usage,
            ),
            headers=self._gemini_adapter.sse_headers,
            pre_start_error_response=lambda exc: self._pre_start_error_response(
                exc, request_id=request_id
            ),
            keepalive=StreamKeepalive.from_settings(self._settings),
        )

    @staticmethod
    def _trace_dropped(dropped: list[str], *, model: str) -> None:
        """Say what a Gemini request asked for that this proxy cannot serve."""
        if not dropped:
            return
        trace_event(
            stage="gemini",
            event="gemini.input.unsupported_fields_ignored",
            source="gemini_api",
            model=model,
            fields=dropped,
        )

    async def _complete_response(
        self,
        streamed: AsyncIterator[str],
        *,
        model: str,
        response_id: str,
        include_thoughts: bool,
        request_id: str,
    ) -> object:
        """Assemble the internal SSE stream into one ``GenerateContentResponse``.

        A failure here is terminal for the whole request, not a partial body:
        a client that asked for JSON must never be handed the half of an answer
        that arrived before the provider dropped.
        """
        try:
            message, error = await aggregate_anthropic_sse_to_message(streamed)
        except GeneratorExit:
            raise
        except asyncio.CancelledError:
            raise
        except ExecutionFailure as exc:
            return self._execution_failure_response(exc, request_id=request_id)
        except BaseExceptionGroup as exc:
            failure = find_execution_failure(exc)
            if failure is not None:
                return self._execution_failure_response(failure, request_id=request_id)
            return self._unexpected_terminal_response(exc, request_id=request_id)
        except Exception as exc:
            return self._unexpected_terminal_response(exc, request_id=request_id)

        if error is not None:
            return self._stream_error_response(error, request_id=request_id)
        return JSONResponse(
            content=self._gemini_adapter.response_from_anthropic_message(
                message,
                model=model,
                response_id=response_id,
                include_thoughts=include_thoughts,
            )
        )

    def _stream_error_response(
        self, error: Mapping[str, object], *, request_id: str
    ) -> JSONResponse:
        raw_message = error.get("message")
        message = (
            raw_message.strip()
            if isinstance(raw_message, str) and raw_message.strip()
            else "Provider request failed unexpectedly."
        )
        trace_terminal_execution_error(
            wire_api="gemini",
            request_id=request_id,
            status_code=500,
            error_type="INTERNAL",
        )
        return terminal_execution_error_response(
            status_code=500,
            content=self._gemini_adapter.error_payload(message=message, code=500),
        )

    def _pre_start_error_response(
        self, exc: BaseException, *, request_id: str
    ) -> JSONResponse:
        failure = find_execution_failure(exc)
        if failure is not None:
            return self._execution_failure_response(failure, request_id=request_id)
        return self._unexpected_terminal_response(
            exc,
            request_id=request_id,
            context="CREATE_GEMINI_CONTENT_STREAM_START_ERROR",
        )

    def _unexpected_terminal_response(
        self,
        exc: BaseException,
        *,
        request_id: str,
        context: str = "CREATE_GEMINI_CONTENT_ERROR",
    ) -> JSONResponse:
        log_unexpected_api_exception(
            self._settings,
            exc,
            context=context,
            request_id=request_id,
        )
        status_code = http_status_for_unexpected_api_exception(exc)
        trace_terminal_execution_error(
            wire_api="gemini",
            request_id=request_id,
            status_code=status_code,
            error_type="INTERNAL",
            error=exc,
        )
        return terminal_execution_error_response(
            status_code=status_code,
            content=self._gemini_adapter.error_payload(
                message=safe_exception_message(exc),
                code=status_code,
            ),
        )

    def _execution_failure_response(
        self,
        failure: ExecutionFailure,
        *,
        request_id: str,
    ) -> JSONResponse:
        trace_terminal_execution_error(
            wire_api="gemini",
            request_id=request_id,
            status_code=failure.status_code,
            error_type=gemini_status_for_failure(failure),
            error=failure,
        )
        return terminal_execution_error_response(
            status_code=failure.status_code,
            content=gemini_failure_payload(failure),
        )

    @staticmethod
    def _trace_post_start_terminal_failure(
        exc: BaseException,
        *,
        request_id: str,
    ) -> None:
        failure = find_execution_failure(exc)
        trace_terminal_execution_error(
            wire_api="gemini",
            request_id=request_id,
            status_code=failure.status_code if failure is not None else 500,
            error_type=(
                gemini_status_for_failure(failure)
                if failure is not None
                else "INTERNAL"
            ),
            error=exc,
        )
