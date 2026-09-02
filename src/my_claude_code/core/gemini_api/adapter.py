"""Facade for Google Gemini protocol adaptation."""

import uuid
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from typing import Any, ClassVar

from .errors import GeminiConversionError, gemini_error_payload
from .events import GEMINI_SSE_HEADERS
from .input import GeminiConversion, convert_request_to_anthropic_payload
from .models import GeminiGenerateContentRequest
from .response import generate_content_response_from_anthropic_message
from .stream import (
    PostStartTerminalFailureObserver,
    iter_gemini_sse_from_anthropic,
)


class GeminiApiAdapter:
    """Convert between Gemini ``generateContent`` and the Anthropic core path."""

    ConversionError: ClassVar[type[GeminiConversionError]] = GeminiConversionError
    sse_headers: ClassVar[dict[str, str]] = GEMINI_SSE_HEADERS

    def new_response_id(self) -> str:
        """Return an id in the shape Google's ``responseId`` carries."""

        return uuid.uuid4().hex[:22]

    def to_anthropic_payload(
        self, request: GeminiGenerateContentRequest
    ) -> GeminiConversion:
        return convert_request_to_anthropic_payload(request)

    def iter_sse_from_anthropic(
        self,
        chunks: AsyncIterable[Any],
        *,
        model: str,
        response_id: str,
        include_thoughts: bool,
        on_post_start_terminal_failure: PostStartTerminalFailureObserver | None = None,
    ) -> AsyncIterator[str]:
        return iter_gemini_sse_from_anthropic(
            chunks,
            model=model,
            response_id=response_id,
            include_thoughts=include_thoughts,
            on_post_start_terminal_failure=on_post_start_terminal_failure,
        )

    def response_from_anthropic_message(
        self,
        message: Mapping[str, Any],
        *,
        model: str,
        response_id: str,
        include_thoughts: bool,
    ) -> dict[str, Any]:
        return generate_content_response_from_anthropic_message(
            message,
            model=model,
            response_id=response_id,
            include_thoughts=include_thoughts,
        )

    def error_payload(
        self, *, message: str, code: int, status: str | None = None
    ) -> dict[str, Any]:
        return gemini_error_payload(message=message, code=code, status=status)
