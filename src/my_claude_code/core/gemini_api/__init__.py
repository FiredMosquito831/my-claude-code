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
from .events import GEMINI_SSE_HEADERS
from .media import (
    MODALITY_AUDIO,
    MODALITY_IMAGE,
    GeminiMediaAsk,
    InlineMedia,
    audio_part,
    encode_media_answer,
    fetched_image_part,
    file_id_for_job,
    gemini_operation,
    generate_content_media_response,
    image_ask,
    job_id_for_file,
    media_output,
    openai_image_parts,
    operation_name,
    response_modalities,
    speech_ask,
    video_ask,
)
from .models import GeminiGenerateContentRequest
from .paths import (
    COUNT_TOKENS,
    GENERATE_CONTENT,
    PREDICT_LONG_RUNNING,
    STREAM_GENERATE_CONTENT,
    SUPPORTED_METHODS,
    GeminiModelPath,
    model_resource_name,
    parse_model_method_path,
    strip_models_prefix,
)

__all__ = [
    "COUNT_TOKENS",
    "GEMINI_SSE_HEADERS",
    "GENERATE_CONTENT",
    "MODALITY_AUDIO",
    "MODALITY_IMAGE",
    "PREDICT_LONG_RUNNING",
    "STREAM_GENERATE_CONTENT",
    "SUPPORTED_METHODS",
    "GeminiApiAdapter",
    "GeminiConversionError",
    "GeminiGenerateContentRequest",
    "GeminiMediaAsk",
    "GeminiModelPath",
    "InlineMedia",
    "audio_part",
    "encode_media_answer",
    "fetched_image_part",
    "file_id_for_job",
    "gemini_count_tokens_payload",
    "gemini_error_payload",
    "gemini_failure_payload",
    "gemini_model_entry",
    "gemini_models_payload",
    "gemini_operation",
    "gemini_status_for_code",
    "gemini_status_for_failure",
    "generate_content_media_response",
    "image_ask",
    "job_id_for_file",
    "media_output",
    "model_resource_name",
    "openai_image_parts",
    "operation_name",
    "parse_model_method_path",
    "response_modalities",
    "speech_ask",
    "strip_models_prefix",
    "video_ask",
]
