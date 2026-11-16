"""OpenAI Responses API product flow for Codex clients."""

from collections.abc import Mapping
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
from my_claude_code.core.anthropic import MessagesRequest
from my_claude_code.core.client_fingerprint import harness_from_headers
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import ExecutionFailure, find_execution_failure
from my_claude_code.core.openai_responses import (
    OpenAIResponsesAdapter,
    OpenAIResponsesRequest,
    ResponsesWebSearchSpec,
    build_responses_payload_from_loop,
    collect_responses_final_text,
    extract_responses_web_search_spec,
    iter_responses_sse_from_loop,
    openai_error_type_for_failure,
    openai_failure_payload,
)


class ResponsesHandler:
    """Handle streaming OpenAI Responses-compatible requests."""

    def __init__(
        self,
        settings: Settings,
        provider_resolver: ProviderResolver,
        *,
        model_router: ModelRouter | None = None,
        responses_adapter: OpenAIResponsesAdapter | None = None,
        provider_executor: ProviderExecutor | None = None,
        generation_id: int | None = None,
    ) -> None:
        self._settings = settings
        self._model_router = model_router or ModelRouter(settings)
        self._responses_adapter = responses_adapter or OpenAIResponsesAdapter()

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
            # No model in hand at this point: the answer is the pool's best
            # case over all models, which is what a header can honestly say.
            return provider.throttle_remaining()
        except Exception:
            return None

    async def create(
        self,
        request_data: OpenAIResponsesRequest,
        *,
        request_id: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> object:
        """Create a Responses-compatible response (SSE or opt-in web_search JSON)."""
        request_id = request_id or new_request_id()
        request_payload = request_data.model_dump(mode="json", exclude_none=True)
        allow_web_search = bool(self._settings.web_search_responses_enabled)

        capture: RequestCapture | None = None
        try:
            web_search_spec = extract_responses_web_search_spec(
                request_data.tools,
                request_data.tool_choice,
                allow_web_search=allow_web_search,
            )
            if request_data.stream is False and web_search_spec is None:
                raise InvalidRequestError(
                    "MCC /v1/responses supports streaming only; omit stream or set stream=true."
                )

            anthropic_payload = self._responses_adapter.to_anthropic_payload(
                request_data,
                allow_web_search=allow_web_search,
            )
            response_request = MessagesRequest(**anthropic_payload)
            stamp_prompt_cache_ask(self._settings, response_request)
            capture = build_capture(
                self._settings,
                response_request,
                request_id=request_id,
                endpoint="/v1/responses",
                protocol="openai_responses",
                headers=headers,
            )
            require_non_empty_messages(response_request.messages)
            harness = harness_from_headers(headers).harness
            if web_search_spec is not None:
                return await self._create_web_search_response(
                    request_data,
                    response_request,
                    web_search_spec,
                    request_payload=request_payload,
                    request_id=request_id,
                    harness=harness,
                    capture=capture,
                )

            plan = self._model_router.resolve_messages_plan(
                response_request, harness=harness
            )
            capture.set_plan(plan)
            capture.set_routing(plan.primary)

            streamed = capture.wrap(
                self._provider_executor.stream(
                    plan,
                    wire_api="responses",
                    raw_log_label="FULL_RESPONSES_PAYLOAD",
                    raw_log_payload=request_payload,
                    request_id=request_id,
                    on_attempt=capture.set_routing,
                    on_attempt_result=capture.record_attempt_result,
                ),
                discard_after_terminal=True,
            )
            return await openai_sse_streaming_response(
                self._responses_adapter.iter_sse_from_anthropic(
                    streamed,
                    request_data,
                    on_post_start_terminal_failure=lambda exc: (
                        self._trace_post_start_terminal_failure(
                            exc,
                            request_id=request_id,
                        )
                    ),
                ),
                headers=self._responses_adapter.sse_headers,
                pre_start_error_response=lambda exc: self._pre_start_error_response(
                    exc, request_id=request_id
                ),
                keepalive=StreamKeepalive.from_settings(self._settings),
            )
        except OpenAIResponsesAdapter.ConversionError as exc:
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
                context="CREATE_RESPONSE_ERROR",
            )
            return JSONResponse(
                status_code=http_status_for_unexpected_api_exception(exc),
                content=self._responses_adapter.error_payload(
                    message=safe_exception_message(exc),
                    error_type="api_error",
                ),
            )

    async def _create_web_search_response(
        self,
        request_data: OpenAIResponsesRequest,
        response_request: MessagesRequest,
        spec: ResponsesWebSearchSpec,
        *,
        request_payload: dict[str, Any],
        request_id: str,
        harness: str | None,
        capture: RequestCapture,
    ) -> object:
        """Execute Responses ``web_search*`` tools through MCC's local tool loop."""
        if self._settings.web_search_door_mode == "passthrough":
            raise InvalidRequestError(
                "WEB_SEARCH_DOOR_MODE='passthrough' is not supported on POST /v1/responses; "
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
            response_request,
            config,
            self._settings,
            model_router=self._model_router,
            provider_executor=self._provider_executor,
            wire_api="responses",
            request_id=request_id,
            raw_log_label="FULL_RESPONSES_PAYLOAD",
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

        full_text = collect_responses_final_text(loop_result.final_step_blocks)
        annotations = extract_grounded_citations(
            full_text, loop_result.gathered_sources
        )

        if request_data.stream is False:
            return JSONResponse(
                content=build_responses_payload_from_loop(
                    request_data,
                    web_calls=loop_result.web_calls,
                    final_step_blocks=loop_result.final_step_blocks,
                    annotations=annotations,
                    loop_usage=loop_result.usage,
                )
            )

        return await openai_sse_streaming_response(
            iter_responses_sse_from_loop(
                request_data,
                web_calls=loop_result.web_calls,
                final_step_blocks=loop_result.final_step_blocks,
                annotations=annotations,
                loop_usage=loop_result.usage,
            ),
            headers=self._responses_adapter.sse_headers,
            pre_start_error_response=lambda exc: self._pre_start_error_response(
                exc, request_id=request_id
            ),
            keepalive=StreamKeepalive.from_settings(self._settings),
        )

    def _pre_start_error_response(
        self, exc: BaseException, *, request_id: str
    ) -> JSONResponse:
        failure = find_execution_failure(exc)
        if failure is not None:
            return self._execution_failure_response(failure, request_id=request_id)
        log_unexpected_api_exception(
            self._settings,
            exc,
            context="CREATE_RESPONSE_STREAM_START_ERROR",
            request_id=request_id,
        )
        status_code = http_status_for_unexpected_api_exception(exc)
        trace_terminal_execution_error(
            wire_api="responses",
            request_id=request_id,
            status_code=status_code,
            error_type="api_error",
            error=exc,
        )
        return terminal_execution_error_response(
            status_code=status_code,
            content=self._responses_adapter.error_payload(
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
            wire_api="responses",
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
            wire_api="responses",
            request_id=request_id,
            status_code=failure.status_code if failure is not None else 500,
            error_type=(
                openai_error_type_for_failure(failure)
                if failure is not None
                else "api_error"
            ),
            error=exc,
        )
