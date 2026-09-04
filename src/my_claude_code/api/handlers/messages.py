"""Claude Messages API product flow."""

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace

from fastapi.responses import JSONResponse, Response
from loguru import logger

from my_claude_code.api.detection import is_safety_classifier_request
from my_claude_code.api.optimization_handlers import (
    PROBE_AUTO_RESPONSE,
    LocalOptimization,
    try_optimizations,
)
from my_claude_code.api.request_capture import (
    build_capture,
)
from my_claude_code.api.request_errors import (
    http_status_for_unexpected_api_exception,
    log_unexpected_api_exception,
    require_non_empty_messages,
    unexpected_http_exception,
)
from my_claude_code.api.request_ids import new_request_id
from my_claude_code.api.response_streams import (
    EmptyStreamError,
    anthropic_sse_streaming_response,
    terminal_execution_error_response,
    trace_terminal_execution_error,
)
from my_claude_code.api.web_tools.egress import (
    WebFetchEgressPolicy,
    web_fetch_allowed_scheme_set,
)
from my_claude_code.api.web_tools.request import (
    is_web_server_tool_request,
    unsupported_server_tool_error,
)
from my_claude_code.application.errors import ApplicationError, InvalidRequestError
from my_claude_code.application.execution import (
    ProviderExecutor,
    TokenCounter,
    route_execution_policy,
    route_health_registry,
)
from my_claude_code.application.ports import ProviderResolver
from my_claude_code.application.routing import (
    ModelRouter,
    RoutedMessagesPlan,
    RoutedMessagesRequest,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import (
    MessagesRequest,
    MessagesResponse,
    ToolResultTrimPolicy,
    ToolResultTrimReport,
    TrimMode,
    aggregate_anthropic_sse_to_message,
    anthropic_error_payload,
    anthropic_error_type_for_failure,
    anthropic_failure_payload,
    anthropic_status_for_error_type,
    get_token_count,
    trim_tool_results,
)
from my_claude_code.core.client_fingerprint import harness_from_headers
from my_claude_code.core.diagnostics import safe_exception_message
from my_claude_code.core.failures import ExecutionFailure, find_execution_failure
from my_claude_code.core.reasoning import ReasoningControl, ReasoningPolicy
from my_claude_code.core.trace import trace_event


@dataclass(frozen=True)
class _MessagesStreamResult:
    body: AsyncIterator[str]


@dataclass(frozen=True)
class _MessagesCompleteResult:
    response: object
    # Set only when a local rule produced this response rather than a provider.
    # The web-server-tool intercept also completes without a provider, but it
    # does real work and is not an optimization, so it leaves these None.
    optimization: str | None = None
    tokens_saved: int = 0


_MessagesResult = _MessagesStreamResult | _MessagesCompleteResult
MessageIntercept = Callable[[RoutedMessagesPlan], _MessagesResult | None]


class MessagesHandler:
    """Handle Anthropic-compatible Messages requests."""

    def __init__(
        self,
        settings: Settings,
        provider_resolver: ProviderResolver,
        *,
        model_router: ModelRouter | None = None,
        token_counter: TokenCounter = get_token_count,
        provider_executor: ProviderExecutor | None = None,
        generation_id: int | None = None,
    ) -> None:
        self._settings = settings
        self._model_router = model_router or ModelRouter(settings)
        self._token_counter = token_counter
        self._provider_executor = provider_executor or ProviderExecutor(
            provider_resolver,
            policy=route_execution_policy(settings),
            health=route_health_registry(settings),
            token_counter=token_counter,
            generation_id=generation_id,
            log_raw_payloads=settings.log_raw_api_payloads,
            retry_first=settings.fallback_retry_first,
            provider_lookup=self._throttle_lookup,
        )
        self._trim_policy = _tool_result_trim_policy(settings)
        self._message_intercepts: tuple[MessageIntercept, ...] = (
            self._intercept_web_server_tool,
            self._intercept_local_optimization,
        )

    def _throttle_lookup(self, provider_id: str) -> float | None:
        """Return provider throttle seconds for a provider_id, or None.

        Used by the executor's rate-limit skip: if a provider is currently
        in a reactive rate-limit block, the chain steps over it instead of
        paying the wait. Returns None if the provider is unknown to the
        live generation, so the skip is best-effort and never errors out
        the chain.
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
        request_data: MessagesRequest,
        *,
        request_id: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> object:
        """Create an Anthropic-compatible message response."""
        request_id = request_id or new_request_id()
        self._trim_tool_results(request_data, request_id=request_id)
        capture = build_capture(
            self._settings,
            request_data,
            request_id=request_id,
            endpoint="/v1/messages",
            protocol="anthropic",
            headers=headers,
        )
        try:
            require_non_empty_messages(request_data.messages)
            plan = self._model_router.resolve_messages_plan(
                request_data, harness=harness_from_headers(headers).harness
            )
            plan = self._apply_message_routing_policies(plan)
            capture.set_plan(plan)
            routed = plan.primary
            self._reject_unsupported_server_tools(routed)
            capture.set_routing(routed)

            result = self._run_message_intercepts(plan)
            if result is None:
                logger.debug("No optimization matched, routing to provider")
                result = _MessagesStreamResult(
                    self._provider_executor.stream(
                        plan,
                        wire_api="messages",
                        raw_log_label="FULL_PAYLOAD",
                        raw_log_payload=routed.request.model_dump(),
                        request_id=request_id,
                        # Analytics must name the model that actually answered,
                        # not the one the route started from.
                        on_attempt=capture.set_routing,
                        on_attempt_result=capture.record_attempt_result,
                    )
                )
            if isinstance(result, _MessagesStreamResult):
                result = _MessagesStreamResult(capture.wrap(result.body))
            else:
                if result.optimization is not None:
                    capture.set_optimization(result.optimization, result.tokens_saved)
                capture.finish_success_from_message(result.response)
            return await self._to_public_response(
                result,
                stream=request_data.stream,
                request_id=request_id,
            )
        except ApplicationError as exc:
            capture.finish_error(exc)
            raise
        except ExecutionFailure as exc:
            capture.finish_error(exc)
            return self._execution_failure_response(exc, request_id=request_id)
        except Exception as exc:
            failure = find_execution_failure(exc)
            capture.finish_error(failure if failure is not None else exc)
            if failure is not None:
                return self._execution_failure_response(failure, request_id=request_id)
            raise unexpected_http_exception(
                self._settings, exc, context="CREATE_MESSAGE_ERROR"
            ) from exc

    def _trim_tool_results(
        self, request_data: MessagesRequest, *, request_id: str
    ) -> ToolResultTrimReport:
        """Apply the tool-result trim layer, and record what it did either way.

        Called before the capture is built, so the request log holds the bytes
        that actually went upstream rather than the ones the client sent. The
        measurement of the difference lives in the trace row below, which is
        what makes the saving a number somebody read rather than a percentage
        somebody claimed.

        Applied to ``request_data`` before the plan is resolved, so every
        attempt in a fallback chain carries the same body -- a request must not
        regain content merely by being served by the second model in a chain.
        """
        report = trim_tool_results(request_data, self._trim_policy)
        if not report.outcomes:
            return report
        trace_event(
            stage="request",
            event="my_claude_code.api.tool_result_trim",
            source="api",
            request_id=request_id,
            model=request_data.model,
            applied=report.applied,
            results_scanned=report.scanned,
            results_matched=len(report.outcomes),
            chars_before=report.chars_before,
            chars_after=report.chars_after,
            chars_removed=report.chars_removed,
            by_tool=report.by_tool(),
        )
        return report

    async def _to_public_response(
        self,
        result: _MessagesResult,
        *,
        stream: bool,
        request_id: str,
    ) -> object:
        if isinstance(result, _MessagesCompleteResult):
            return result.response
        if not stream:
            # Non-streaming clients (e.g. Claude Code utility calls) need a
            # complete JSON Message; the internal pipeline is always SSE, so
            # serving that raw here breaks the client SDK's response parse.
            try:
                message, error = await aggregate_anthropic_sse_to_message(result.body)
            except GeneratorExit:
                raise
            except asyncio.CancelledError:
                raise
            except ExecutionFailure as exc:
                return self._execution_failure_response(exc, request_id=request_id)
            except BaseExceptionGroup as exc:
                failure = find_execution_failure(exc)
                if failure is not None:
                    return self._execution_failure_response(
                        failure, request_id=request_id
                    )
                return self._unexpected_execution_error_response(
                    exc,
                    request_id=request_id,
                    context="CREATE_MESSAGE_NON_STREAM_ERROR",
                )
            except Exception as exc:
                return self._unexpected_execution_error_response(
                    exc,
                    request_id=request_id,
                    context="CREATE_MESSAGE_NON_STREAM_ERROR",
                )
            if error is not None:
                error_type, message_text = _stream_error_fields(error)
                status_code = anthropic_status_for_error_type(error_type)
                trace_terminal_execution_error(
                    wire_api="messages",
                    request_id=request_id,
                    status_code=status_code,
                    error_type=error_type,
                )
                return terminal_execution_error_response(
                    status_code=status_code,
                    content=anthropic_error_payload(
                        error_type=error_type,
                        message=message_text,
                        request_id=request_id,
                    ),
                )
            return JSONResponse(content=message)
        return await anthropic_sse_streaming_response(
            result.body,
            pre_start_error_response=lambda exc: self._pre_start_error_response(
                exc, request_id=request_id
            ),
            request_id=request_id,
        )

    def _pre_start_error_response(
        self, exc: BaseException, *, request_id: str
    ) -> Response:
        failure = find_execution_failure(exc)
        if failure is not None:
            return self._execution_failure_response(failure, request_id=request_id)
        context = (
            "CREATE_MESSAGE_EMPTY_STREAM"
            if isinstance(exc, EmptyStreamError)
            else "CREATE_MESSAGE_STREAM_START_ERROR"
        )
        return self._unexpected_execution_error_response(
            exc,
            request_id=request_id,
            context=context,
        )

    def _execution_failure_response(
        self, failure: ExecutionFailure, *, request_id: str
    ) -> JSONResponse:
        error_type = anthropic_error_type_for_failure(failure)
        trace_terminal_execution_error(
            wire_api="messages",
            request_id=request_id,
            status_code=failure.status_code,
            error_type=error_type,
            error=failure,
        )
        return terminal_execution_error_response(
            status_code=failure.status_code,
            content=anthropic_failure_payload(failure, request_id=request_id),
        )

    def _unexpected_execution_error_response(
        self,
        exc: BaseException,
        *,
        request_id: str,
        context: str,
    ) -> JSONResponse:
        log_unexpected_api_exception(
            self._settings,
            exc,
            context=context,
            request_id=request_id,
        )
        status_code = http_status_for_unexpected_api_exception(exc)
        trace_terminal_execution_error(
            wire_api="messages",
            request_id=request_id,
            status_code=status_code,
            error_type="api_error",
            error=exc,
        )
        return terminal_execution_error_response(
            status_code=status_code,
            content=anthropic_error_payload(
                error_type="api_error",
                message=safe_exception_message(exc),
                request_id=request_id,
            ),
        )

    def _reject_unsupported_server_tools(self, routed: RoutedMessagesRequest) -> None:
        tool_err = unsupported_server_tool_error(
            routed.request,
            web_tools_enabled=self._settings.enable_web_server_tools,
        )
        if tool_err is not None:
            raise InvalidRequestError(tool_err)

    def _apply_message_routing_policies(
        self, plan: RoutedMessagesPlan
    ) -> RoutedMessagesPlan:
        """Apply request-shaped policies to every attempt in the plan.

        A policy that depends on the request rather than the model has to hold
        for a fallback too, or the same request would gain thinking back merely
        by being served by the second model in a chain.
        """
        if not is_safety_classifier_request(plan.primary.request):
            return plan
        changed = any(
            attempt.reasoning.control is not ReasoningControl.OFF
            for attempt in plan.attempts
        )
        trace_event(
            stage="routing",
            event="my_claude_code.api.optimization.safety_classifier_no_thinking",
            source="api",
            model=plan.primary.request.model,
            changed=changed,
        )
        if not changed:
            return plan
        return replace(
            plan,
            attempts=tuple(
                replace(attempt, reasoning=ReasoningPolicy.off())
                for attempt in plan.attempts
            ),
        )

    def _run_message_intercepts(
        self, plan: RoutedMessagesPlan
    ) -> _MessagesResult | None:
        """Try each local answer in turn.

        The whole plan is passed, not just its head: an intercept that answers
        without a provider still has to say which model would have answered,
        and that is a property of the chain plus current route health, not of
        the primary alone.
        """
        for intercept in self._message_intercepts:
            result = intercept(plan)
            if result is not None:
                return result
        return None

    def _intercept_web_server_tool(
        self, plan: RoutedMessagesPlan
    ) -> _MessagesResult | None:
        routed = plan.primary
        if not self._settings.enable_web_server_tools:
            return None
        if not is_web_server_tool_request(routed.request):
            return None

        input_tokens = self._token_counter(
            routed.request.messages, routed.request.system, routed.request.tools
        )
        trace_event(
            stage="routing",
            event="my_claude_code.api.optimization.web_server_tool",
            source="api",
            model=routed.request.model,
        )
        egress = WebFetchEgressPolicy(
            allow_private_network_targets=self._settings.web_fetch_allow_private_networks,
            allowed_schemes=web_fetch_allowed_scheme_set(
                self._settings.web_fetch_allowed_schemes
            ),
        )
        # Deferred import: the outbound web-tool stack pulls aiohttp
        # (~0.2 s) and only a request that carries a web server tool
        # reaches this line.
        from my_claude_code.api.web_tools.streaming import (
            stream_web_server_tool_response,
        )

        return _MessagesStreamResult(
            stream_web_server_tool_response(
                routed.request,
                input_tokens=input_tokens,
                web_fetch_egress=egress,
                verbose_client_errors=self._settings.log_api_error_tracebacks,
            ),
        )

    def _intercept_local_optimization(
        self, plan: RoutedMessagesPlan
    ) -> _MessagesResult | None:
        routed = plan.primary
        optimized = try_optimizations(
            routed.request, self._settings, self._token_counter
        )
        if optimized is None:
            return None
        response = self._answering_model_echo(optimized, plan)
        trace_event(
            stage="routing",
            event="my_claude_code.api.optimization.short_circuit",
            source="api",
            model=routed.request.model,
            rule=optimized.rule,
            tokens_saved=optimized.tokens_saved,
        )
        return _MessagesCompleteResult(
            response,
            optimization=optimized.rule,
            tokens_saved=optimized.tokens_saved,
        )

    def _answering_model_echo(
        self, optimized: LocalOptimization, plan: RoutedMessagesPlan
    ) -> MessagesResponse:
        """Stamp a probe's reply with the model that would really have answered.

        A model-routing probe exists to catch a proxy quietly serving a
        different model than the one asked for, so its reply has to name the
        model this request would have reached -- not the head of the chain.
        Those differ whenever recent failures have benched the primary, which
        is precisely the case the harness is looking for. The executor is asked
        rather than the health registry directly, so the answer cannot drift
        from the order execution really uses.

        The id stamped is ``provider_model``, the bare upstream id, because
        that is what a normal streamed reply echoes in ``message_start``
        (routing rewrites ``request.model`` to it at
        ``application/routing.py`` before the provider builds its ledger). A
        probe answered with a ``provider/model`` ref would read as a
        substitution against a real reply that carries neither.

        Every other rule answers a request whose model was never in question,
        so they keep the response they built.
        """
        if optimized.rule != PROBE_AUTO_RESPONSE.rule:
            return optimized.response
        answering = self._provider_executor.first_usable_attempt(plan)
        return optimized.response.model_copy(
            update={"model": answering.resolved.provider_model}
        )


def _tool_result_trim_policy(settings: Settings) -> ToolResultTrimPolicy:
    """Translate configuration into the policy the protocol layer obeys.

    The split is deliberate: ``core`` owns the transform and knows nothing about
    settings, ``config`` owns the thresholds and knows nothing about the
    protocol, and this one function is where the two meet.
    """
    return ToolResultTrimPolicy(
        enabled=settings.enable_tool_result_trimming,
        modes={
            "Read": TrimMode(settings.tool_result_trim_read),
            "Grep": TrimMode(settings.tool_result_trim_grep),
            "Glob": TrimMode(settings.tool_result_trim_glob),
        },
        threshold_chars=settings.tool_result_trim_threshold_chars,
        keep_head_chars=settings.tool_result_trim_keep_head_chars,
        keep_tail_chars=settings.tool_result_trim_keep_tail_chars,
        protect_recent_results=settings.tool_result_trim_protect_recent_results,
    )


def _stream_error_fields(error: dict[str, object]) -> tuple[str, str]:
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
    return error_type, message
