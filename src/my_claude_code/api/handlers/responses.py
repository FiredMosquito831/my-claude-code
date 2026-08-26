"""OpenAI Responses API product flow for Codex clients."""

from collections.abc import Mapping

from fastapi.responses import JSONResponse

from my_claude_code.api.request_capture import RequestCapture, build_capture
from my_claude_code.api.request_errors import (
    http_status_for_unexpected_api_exception,
    log_unexpected_api_exception,
    require_non_empty_messages,
)
from my_claude_code.api.request_ids import new_request_id
from my_claude_code.api.response_streams import (
    openai_responses_sse_streaming_response,
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
from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import MessagesRequest
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import ExecutionFailure, find_execution_failure
from my_claude_code.core.openai_responses import (
    OpenAIResponsesAdapter,
    OpenAIResponsesRequest,
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
        """Create a streaming OpenAI Responses-compatible response."""
        request_id = request_id or new_request_id()
        request_payload = request_data.model_dump(mode="json", exclude_none=True)
        if request_data.stream is False:
            raise InvalidRequestError(
                "MCC /v1/responses supports streaming only; omit stream or set stream=true."
            )

        capture: RequestCapture | None = None
        try:
            anthropic_payload = self._responses_adapter.to_anthropic_payload(
                request_data
            )
            response_request = MessagesRequest(**anthropic_payload)
            capture = build_capture(
                self._settings,
                response_request,
                request_id=request_id,
                endpoint="/v1/responses",
                protocol="openai_responses",
                headers=headers,
            )
            require_non_empty_messages(response_request.messages)
            plan = self._model_router.resolve_messages_plan(response_request)
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
                )
            )
            return await openai_responses_sse_streaming_response(
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
