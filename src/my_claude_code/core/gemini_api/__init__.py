"""Google Gemini protocol adapter.

The third inbound protocol MCC serves, beside ``/v1/messages`` (Anthropic) and
the two OpenAI surfaces. It exists because a whole family of clients speaks
only this one -- Gemini CLI, the ``google-genai`` SDKs, every IDE plugin built
on them -- and none of them can be pointed at an OpenAI-shaped endpoint.

It is served through exactly the same router, executor, fallback chain and
request log as every other surface: this package only translates. The outbound
``gemini`` **provider** in ``providers/gemini`` is a different thing entirely
and shares nothing with it -- that one is a gateway MCC buys tokens *from*,
this one is a protocol MCC answers *in*.
"""

from .adapter import GeminiApiAdapter
from .catalog import (
    gemini_count_tokens_payload,
    gemini_model_entry,
    gemini_models_payload,
)
from .errors import (
    GeminiConversionError,
    gemini_error_payload,
    gemini_failure_payload,
    gemini_status_for_code,
    gemini_status_for_failure,
)
from .models import GeminiGenerateContentRequest
from .paths import (
    COUNT_TOKENS,
    GENERATE_CONTENT,
    STREAM_GENERATE_CONTENT,
    SUPPORTED_METHODS,
    GeminiModelPath,
    model_resource_name,
    parse_model_method_path,
    strip_models_prefix,
)

__all__ = [
    "COUNT_TOKENS",
    "GENERATE_CONTENT",
    "STREAM_GENERATE_CONTENT",
    "SUPPORTED_METHODS",
    "GeminiApiAdapter",
    "GeminiConversionError",
    "GeminiGenerateContentRequest",
    "GeminiModelPath",
    "gemini_count_tokens_payload",
    "gemini_error_payload",
    "gemini_failure_payload",
    "gemini_model_entry",
    "gemini_models_payload",
    "gemini_status_for_code",
    "gemini_status_for_failure",
    "model_resource_name",
    "parse_model_method_path",
    "strip_models_prefix",
]
