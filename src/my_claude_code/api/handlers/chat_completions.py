"""OpenAI Chat Completions API product flow for OpenAI-compatible clients."""

import asyncio
from collections.abc import AsyncIterator, Mapping

from fastapi.responses import JSONResponse

from my_claude_code.api.request_capture import RequestCapture, build_capture
from my_claude_code.api.request_errors import (
    http_status_for_unexpected_api_exception,
    log_unexpected_api_exception,
    require_non_empty_messages,
)
from my_claude_code.api.request_ids import new_request_id
from my_claude_code.api.response_streams import (
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
from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import (
    MessagesRequest,
    aggregate_anthropic_sse_to_message,
)
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import ExecutionFailure, find_execution_failure
from my_claude_code.core.openai_chat_completions import (
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionsAdapter,
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

        capture: RequestCapture | None = None
        try:
            anthropic_payload = self._chat_adapter.to_anthropic_payload(request_data)
            chat_request = MessagesRequest(**anthropic_payload)
            capture = build_capture(
                self._settings,
                chat_request,
                request_id=request_id,
                endpoint=CHAT_COMPLETIONS_ENDPOINT,
                protocol="openai_chat",
                headers=headers,
                stream=wants_stream,
            )
            require_non_empty_messages(chat_request.messages)
            plan = self._model_router.resolve_messages_plan(chat_request)
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
                )
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
        trace_terminal_execution_error(
            wire_api="chat_completions",
            request_id=request_id,
            status_code=500,
            error_type=error_type,
        )
        return terminal_execution_error_response(
            status_code=500,
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
