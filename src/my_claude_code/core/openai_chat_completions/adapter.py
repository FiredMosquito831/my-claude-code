"""Facade for OpenAI Chat Completions protocol adaptation."""

from collections.abc import AsyncIterable, AsyncIterator, Mapping
from typing import Any, ClassVar

from my_claude_code.core.openai_common import openai_error_payload

from .completion import chat_completion_from_anthropic_message
from .errors import ChatCompletionsConversionError
from .events import OPENAI_CHAT_SSE_HEADERS
from .ids import new_chat_completion_id
from .input import convert_request_to_anthropic_payload
from .models import OpenAIChatCompletionRequest
from .stream import (
    PostStartTerminalFailureObserver,
    iter_chat_sse_from_anthropic,
)


class OpenAIChatCompletionsAdapter:
    """Convert between Chat Completions and the proxy's Anthropic core path."""

    ConversionError: ClassVar[type[ChatCompletionsConversionError]] = (
        ChatCompletionsConversionError
    )
    sse_headers: ClassVar[dict[str, str]] = OPENAI_CHAT_SSE_HEADERS

    def new_completion_id(self) -> str:
        return new_chat_completion_id()

    def to_anthropic_payload(
        self, request: OpenAIChatCompletionRequest
    ) -> dict[str, Any]:
        return convert_request_to_anthropic_payload(request)

    def iter_sse_from_anthropic(
        self,
        chunks: AsyncIterable[Any],
        request: OpenAIChatCompletionRequest,
        *,
        completion_id: str,
        on_post_start_terminal_failure: PostStartTerminalFailureObserver | None = None,
    ) -> AsyncIterator[str]:
        return iter_chat_sse_from_anthropic(
            chunks,
            request,
            completion_id=completion_id,
            on_post_start_terminal_failure=on_post_start_terminal_failure,
        )

    def completion_from_anthropic_message(
        self,
        message: Mapping[str, Any],
        request: OpenAIChatCompletionRequest,
        *,
        completion_id: str,
    ) -> dict[str, Any]:
        return chat_completion_from_anthropic_message(
            message, request, completion_id=completion_id
        )

    def error_payload(
        self,
        *,
        message: str,
        error_type: str,
        param: str | None = None,
    ) -> dict[str, Any]:
        payload = openai_error_payload(message=message, error_type=error_type)
        if param is not None:
            payload["error"]["param"] = param
        return payload
