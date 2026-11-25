"""Facade for OpenAI Responses protocol adaptation."""

from collections.abc import AsyncIterable, AsyncIterator
from typing import Any, ClassVar

from .errors import ResponsesConversionError, openai_error_payload
from .events import OPENAI_RESPONSES_SSE_HEADERS
from .input import convert_request_to_anthropic_payload
from .models import OpenAIResponsesRequest
from .stream import (
    PostStartTerminalFailureObserver,
    complete_response_from_anthropic_sse,
    iter_responses_sse_from_anthropic,
)


class OpenAIResponsesAdapter:
    """Convert between OpenAI Responses and the proxy's Anthropic core path."""

    ConversionError: ClassVar[type[ResponsesConversionError]] = ResponsesConversionError
    sse_headers: ClassVar[dict[str, str]] = OPENAI_RESPONSES_SSE_HEADERS

    def to_anthropic_payload(
        self,
        request: OpenAIResponsesRequest,
        *,
        allow_web_search: bool = False,
    ) -> dict[str, Any]:
        return convert_request_to_anthropic_payload(
            request, allow_web_search=allow_web_search
        )

    def iter_sse_from_anthropic(
        self,
        chunks: AsyncIterable[Any],
        request: OpenAIResponsesRequest,
        *,
        on_post_start_terminal_failure: PostStartTerminalFailureObserver | None = None,
    ) -> AsyncIterator[str]:
        return iter_responses_sse_from_anthropic(
            chunks,
            request,
            on_post_start_terminal_failure=on_post_start_terminal_failure,
        )

    async def complete_from_anthropic_sse(
        self,
        chunks: AsyncIterable[Any],
        request: OpenAIResponsesRequest,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        return await complete_response_from_anthropic_sse(chunks, request)

    def error_payload(
        self,
        *,
        message: str,
        error_type: str,
        param: str | None = None,
        code: str | None = None,
    ) -> dict[str, Any]:
        return openai_error_payload(
            message=message, error_type=error_type, param=param, code=code
        )
