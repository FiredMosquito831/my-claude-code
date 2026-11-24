"""OpenAI Chat Completions API product flow for OpenAI-compatible clients."""

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
from my_claude_code.api.wire_surfaces import CHAT_COMPLETIONS_ENDPOINT
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
    anthropic_status_for_error_type,
)
from my_claude_code.core.carrier_surface import CHAT_REDACTED_THINKING
from my_claude_code.core.client_fingerprint import harness_from_headers
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import ExecutionFailure, find_execution_failure
from my_claude_code.core.openai_chat_completions import (
    ChatWebSearchSpec,
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionsAdapter,
    build_chat_completion_from_loop,
    build_nested_chat_annotations,
    collect_chat_final_text,
    extract_chat_web_search_spec,
    iter_chat_sse_from_loop,
    openai_error_type_for_failure,
    openai_failure_payload,
)


class ChatCompletionsHandler:
    """Handle OpenAI Chat Completions requests, streaming and not.

    A near twin of :class:`ResponsesHandler` by design: both translate an
    OpenAI-shaped request into one ``MessagesRequest`` and hand it to the same
    :class:`ProviderExecutor`, so routing, fallback, pause, reasoning gating,
    output budgets, wire capture, the upstream ladder, the request log and the
    optimizer rules apply to all three inbound surfaces identically. The one
    real difference is that this protocol has a non-streaming form, which is
    served the way ``/v1/messages`` serves its own: by assembling the internal
    SSE stream at this boundary rather than by running a second pipeline.
    """

    def __init__(
        self,
        settings: Settings,
        provider_resolver: ProviderResolver,
        *,
        model_router: ModelRouter | None = None,
        chat_adapter: OpenAIChatCompletionsAdapter | None = None,
        provider_executor: ProviderExecutor | None = None,
        generation_id: int | None = None,
    ) -> None:
        self._settings = settings
        self._model_router = model_router or ModelRouter(settings)
        self._chat_adapter = chat_adapter or OpenAIChatCompletionsAdapter()

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
        request_data: OpenAIChatCompletionRequest,
        *,
        request_id: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> object:
        """Create a Chat Completions response, streamed or complete."""
        request_id = request_id or new_request_id()
        request_payload = request_data.model_dump(mode="json", exclude_none=True)
        completion_id = self._chat_adapter.new_completion_id()
        wants_stream = bool(request_data.stream)

        allow_web_search = bool(self._settings.web_search_chat_enabled)

        capture: RequestCapture | None = None
        try:
            web_search_spec = extract_chat_web_search_spec(
                request_data.tools,
                request_data.tool_choice,
                request_data.web_search_options,
                allow_web_search=allow_web_search,
            )
            # 8.20.0: what this door's ingress drops, by cell. The opaque
            # ``reasoning_details`` a client sends has no field to reach an
            # upstream in, and until now it vanished without a count -- Q1's
            # answer was "count instead of build", and a count that cannot say
            # which cell is not an addition.
            carrier_surface: dict[str, int] = {}
            anthropic_payload = self._chat_adapter.to_anthropic_payload(
                request_data,
                carrier_surface=carrier_surface,
                allow_web_search=allow_web_search,
            )
            chat_request = MessagesRequest(**anthropic_payload)
            stamp_prompt_cache_ask(self._settings, chat_request)
            capture = build_capture(
                self._settings,
                chat_request,
                request_id=request_id,
                endpoint=CHAT_COMPLETIONS_ENDPOINT,
                protocol="openai_chat",
                headers=headers,
                stream=wants_stream,
                carrier_surface=carrier_surface,
                # The egress half of cell 9: this door's assembler has no frame
                # for a ``redacted_thinking`` block, so every one the upstream
                # produced is a block the client was never handed -- and the
                # opaque blob it would have echoed back never existed.
                carrier_surface_egress=frozenset({CHAT_REDACTED_THINKING}),
            )
            require_non_empty_messages(chat_request.messages)
            harness = harness_from_headers(headers).harness
            if web_search_spec is not None:
                return await self._create_web_search_response(
                    request_data,
                    chat_request,
                    web_search_spec,
                    wants_stream=wants_stream,
                    completion_id=completion_id,
                    request_payload=request_payload,
                    request_id=request_id,
                    harness=harness,
                    capture=capture,
                )

            plan = self._model_router.resolve_messages_plan(
                chat_request, harness=harness
            )
            capture.set_plan(plan)
            capture.set_routing(plan.primary)

            streamed = capture.wrap(
                self._provider_executor.stream(
                    plan,
                    wire_api="chat_completions",
                    raw_log_label="FULL_CHAT_COMPLETIONS_PAYLOAD",
                    raw_log_payload=request_payload,
                    request_id=request_id,
                    on_attempt=capture.set_routing,
                    on_attempt_result=capture.record_attempt_result,
                ),
                discard_after_terminal=True,
            )
            if not wants_stream:
                return await self._complete_response(
                    streamed,
                    request_data,
                    completion_id=completion_id,
                    request_id=request_id,
                )
            return await openai_sse_streaming_response(
                self._chat_adapter.iter_sse_from_anthropic(
                    streamed,
                    request_data,
                    completion_id=completion_id,
                    on_post_start_terminal_failure=lambda exc: (
                        self._trace_post_start_terminal_failure(
                            exc, request_id=request_id
                        )
                    ),
                ),
                headers=self._chat_adapter.sse_headers,
                pre_start_error_response=lambda exc: self._pre_start_error_response(
                    exc, request_id=request_id
                ),
                keepalive=StreamKeepalive.from_settings(self._settings),
            )
        except OpenAIChatCompletionsAdapter.ConversionError as exc:
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
                context="CREATE_CHAT_COMPLETION_ERROR",
            )
            return JSONResponse(
                status_code=http_status_for_unexpected_api_exception(exc),
                content=self._chat_adapter.error_payload(
                    message=safe_exception_message(exc),
                    error_type="api_error",
                ),
            )

    async def _create_web_search_response(
        self,
        request_data: OpenAIChatCompletionRequest,
        chat_request: MessagesRequest,
        spec: ChatWebSearchSpec,
        *,
        wants_stream: bool,
        completion_id: str,
        request_payload: dict[str, Any],
        request_id: str,
        harness: str | None,
        capture: RequestCapture,
    ) -> object:
        """Execute Chat Completions ``web_search_options`` / ``web_search*`` through MCC's local loop."""
        if self._settings.web_search_door_mode == "passthrough":
            raise InvalidRequestError(
                "WEB_SEARCH_DOOR_MODE='passthrough' is not supported on POST /v1/chat/completions; "
                "use 'local' or 'auto' instead."
            )

        from my_claude_code.api.web_tools.constants import _MAX_SEARCH_RESULTS
        from my_claude_code.api.web_tools.request import WebSearchToolOptions
        from my_claude_code.api.web_tools.tool_loop import (
            WebToolLoopConfig,
            extract_grounded_citations,
            iter_anthropic_sse_from_loop,
            run_routed_web_tool_loop,
        )

        search_max_results = (
            5 if spec.search_context_size == "low" else _MAX_SEARCH_RESULTS
        )
        config = WebToolLoopConfig(
            enable_search=True,
            enable_fetch=False,
            search_options=WebSearchToolOptions(allowed_domains=spec.allowed_domains),
            search_external_web_access=spec.external_web_access,
            search_max_results=search_max_results,
            user_location=spec.user_location,
            max_steps=self._settings.web_search_tool_loop_max_steps,
            initial_force_search=spec.forced,
            search_allowed_disjoint=spec.allowed_disjoint,
        )
        loop_result = await run_routed_web_tool_loop(
            chat_request,
            config,
            self._settings,
            model_router=self._model_router,
            provider_executor=self._provider_executor,
            wire_api="chat_completions",
            request_id=request_id,
            raw_log_label="FULL_CHAT_COMPLETIONS_PAYLOAD",
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

        full_text = collect_chat_final_text(loop_result.final_step_blocks)
        flat_citations = extract_grounded_citations(
            full_text, loop_result.gathered_sources
        )
        nested_annotations = build_nested_chat_annotations(flat_citations)

        if not wants_stream:
            return JSONResponse(
                content=build_chat_completion_from_loop(
                    request_data,
                    completion_id=completion_id,
                    final_step_blocks=loop_result.final_step_blocks,
                    nested_annotations=nested_annotations,
                    loop_usage=loop_result.usage,
                    stop_reason=loop_result.stop_reason,
                )
            )

        return await openai_sse_streaming_response(
            iter_chat_sse_from_loop(
                request_data,
                completion_id=completion_id,
                final_step_blocks=loop_result.final_step_blocks,
                nested_annotations=nested_annotations,
                loop_usage=loop_result.usage,
                stop_reason=loop_result.stop_reason,
            ),
            headers=self._chat_adapter.sse_headers,
            pre_start_error_response=lambda exc: self._pre_start_error_response(
                exc, request_id=request_id
            ),
            keepalive=StreamKeepalive.from_settings(self._settings),
        )

    async def _complete_response(
        self,
        streamed: AsyncIterator[str],
        request_data: OpenAIChatCompletionRequest,
        *,
        completion_id: str,
        request_id: str,
    ) -> object:
        """Assemble the internal SSE stream into one ``chat.completion``.

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
            content=self._chat_adapter.completion_from_anthropic_message(
                message, request_data, completion_id=completion_id
            )
        )

    def _stream_error_response(
        self, error: Mapping[str, object], *, request_id: str
    ) -> JSONResponse:
        raw_type = error.get("type")
        error_type = (
            raw_type.strip()
            if isinstance(raw_type, str) and raw_type.strip()
            else "api_error"
        )
        raw_message = error.get("message")
        message = (
            raw_message.strip()
            if isinstance(raw_message, str) and raw_message.strip()
            else "Provider request failed unexpectedly."
        )
        status_code = anthropic_status_for_error_type(error_type)
        trace_terminal_execution_error(
            wire_api="chat_completions",
            request_id=request_id,
            status_code=status_code,
            error_type=error_type,
        )
        return terminal_execution_error_response(
            status_code=status_code,
            content=self._chat_adapter.error_payload(
                message=message, error_type=error_type
            ),
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
            context="CREATE_CHAT_COMPLETION_STREAM_START_ERROR",
        )

    def _unexpected_terminal_response(
        self,
        exc: BaseException,
        *,
        request_id: str,
        context: str = "CREATE_CHAT_COMPLETION_ERROR",
    ) -> JSONResponse:
        log_unexpected_api_exception(
            self._settings,
            exc,
            context=context,
            request_id=request_id,
        )
        status_code = http_status_for_unexpected_api_exception(exc)
        trace_terminal_execution_error(
            wire_api="chat_completions",
            request_id=request_id,
            status_code=status_code,
            error_type="api_error",
            error=exc,
        )
        return terminal_execution_error_response(
            status_code=status_code,
            content=self._chat_adapter.error_payload(
                message=safe_exception_message(exc),
                error_type="api_error",
            ),
        )

    def _execution_failure_response(
        self,
        failure: ExecutionFailure,
        *,
        request_id: str,
    ) -> JSONResponse:
        error_type = openai_error_type_for_failure(failure)
        trace_terminal_execution_error(
            wire_api="chat_completions",
            request_id=request_id,
            status_code=failure.status_code,
            error_type=error_type,
            error=failure,
        )
        return terminal_execution_error_response(
            status_code=failure.status_code,
            content=openai_failure_payload(failure),
        )

    @staticmethod
    def _trace_post_start_terminal_failure(
        exc: BaseException,
        *,
        request_id: str,
    ) -> None:
        failure = find_execution_failure(exc)
        trace_terminal_execution_error(
            wire_api="chat_completions",
            request_id=request_id,
            status_code=failure.status_code if failure is not None else 500,
            error_type=(
                openai_error_type_for_failure(failure)
                if failure is not None
                else "api_error"
            ),
            error=exc,
        )
