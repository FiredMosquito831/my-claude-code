"""Anthropic protocol helpers shared across API, providers, and integrations."""

from .content import extract_text_from_content, get_block_attr, get_block_type
from .conversion import (
    AnthropicToOpenAIConverter,
    OpenAIConversionError,
    ReasoningReplayMode,
    build_base_request_body,
    is_synthetic_openai_tool_turn_boundary,
)
from .errors import (
    anthropic_error_payload,
    anthropic_error_type_for_failure,
    anthropic_failure_payload,
    anthropic_status_for_error_type,
)
from .models import (
    ContentBlockDocument,
    ContentBlockImage,
    ContentBlockRedactedThinking,
    ContentBlockServerToolUse,
    ContentBlockText,
    ContentBlockThinking,
    ContentBlockToolResult,
    ContentBlockToolUse,
    ContentBlockWebFetchToolResult,
    ContentBlockWebSearchToolResult,
    Message,
    MessagesRequest,
    MessagesResponse,
    SystemContent,
    ThinkingConfig,
    TokenCountRequest,
    TokenCountResponse,
    Tool,
    Usage,
)
from .openai_tool_names import EMPTY_TOOL_CATALOGUE, OpenAIToolNameCodec
from .request_modalities import (
    ImageInput,
    request_carries_image,
    request_image_inputs,
)
from .request_serialization import dump_messages_request, serialize_tool_result_content
from .request_snapshot import anthropic_request_snapshot
from .sse_aggregation import aggregate_anthropic_sse_to_message
from .streaming import (
    AnthropicStreamLedger,
    StreamBlockLedger,
    ToolBlockState,
    format_sse_event,
    map_stop_reason,
)
from .thinking import ContentChunk, ContentType, ThinkTagParser
from .tokens import count_text_tokens, get_token_count
from .tool_result_trimming import (
    TRIM_MARKER_OPEN,
    TRIM_MODE_NAMES,
    TRIMMABLE_TOOL_NAMES,
    ToolResultTrimPolicy,
    ToolResultTrimReport,
    TrimMode,
    trim_tool_results,
)
from .tools import FunctionTagToolParser, HeuristicToolParser
from .utils import set_if_not_none

__all__ = [
    "EMPTY_TOOL_CATALOGUE",
    "TRIMMABLE_TOOL_NAMES",
    "TRIM_MARKER_OPEN",
    "TRIM_MODE_NAMES",
    "AnthropicStreamLedger",
    "AnthropicToOpenAIConverter",
    "ContentBlockDocument",
    "ContentBlockImage",
    "ContentBlockRedactedThinking",
    "ContentBlockServerToolUse",
    "ContentBlockText",
    "ContentBlockThinking",
    "ContentBlockToolResult",
    "ContentBlockToolUse",
    "ContentBlockWebFetchToolResult",
    "ContentBlockWebSearchToolResult",
    "ContentChunk",
    "ContentType",
    "FunctionTagToolParser",
    "HeuristicToolParser",
    "ImageInput",
    "Message",
    "MessagesRequest",
    "MessagesResponse",
    "OpenAIConversionError",
    "OpenAIToolNameCodec",
    "ReasoningReplayMode",
    "StreamBlockLedger",
    "SystemContent",
    "ThinkTagParser",
    "ThinkingConfig",
    "TokenCountRequest",
    "TokenCountResponse",
    "Tool",
    "ToolBlockState",
    "ToolResultTrimPolicy",
    "ToolResultTrimReport",
    "TrimMode",
    "Usage",
    "aggregate_anthropic_sse_to_message",
    "anthropic_error_payload",
    "anthropic_error_type_for_failure",
    "anthropic_failure_payload",
    "anthropic_request_snapshot",
    "anthropic_status_for_error_type",
    "build_base_request_body",
    "count_text_tokens",
    "dump_messages_request",
    "extract_text_from_content",
    "format_sse_event",
    "get_block_attr",
    "get_block_type",
    "get_token_count",
    "is_synthetic_openai_tool_turn_boundary",
    "map_stop_reason",
    "request_carries_image",
    "request_image_inputs",
    "serialize_tool_result_content",
    "set_if_not_none",
    "trim_tool_results",
]
