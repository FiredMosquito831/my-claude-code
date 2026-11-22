"""OpenAI Responses protocol adapter."""

from .adapter import OpenAIResponsesAdapter
from .carrier import (
    CARRIER_MAX_CHARS,
    CARRIER_PREFIX,
    CARRIER_PROTOCOL_RESPONSES,
    ORIGIN_FIELDS,
    CarrierPayload,
    ReplayOrigin,
    accepts,
    connection_id,
    decode_carrier,
    encode_carrier,
    is_carrier,
    normalise_endpoint,
    normalised_model,
)
from .errors import (
    openai_error_payload,
    openai_error_type_for_failure,
    openai_failure_payload,
)
from .models import OpenAIResponsesRequest
from .tools import (
    ResponsesWebSearchSpec,
    call_id_from_item,
    extract_responses_web_search_spec,
    responses_tool_name_to_anthropic_name,
)
from .web_search_response import (
    build_responses_payload_from_loop,
    collect_responses_final_text,
    iter_responses_sse_from_loop,
)

__all__ = [
    "CARRIER_MAX_CHARS",
    "CARRIER_PREFIX",
    "CARRIER_PROTOCOL_RESPONSES",
    "ORIGIN_FIELDS",
    "CarrierPayload",
    "OpenAIResponsesAdapter",
    "OpenAIResponsesRequest",
    "ReplayOrigin",
    "ResponsesWebSearchSpec",
    "accepts",
    "build_responses_payload_from_loop",
    "call_id_from_item",
    "collect_responses_final_text",
    "connection_id",
    "decode_carrier",
    "encode_carrier",
    "extract_responses_web_search_spec",
    "is_carrier",
    "iter_responses_sse_from_loop",
    "normalise_endpoint",
    "normalised_model",
    "openai_error_payload",
    "openai_error_type_for_failure",
    "openai_failure_payload",
    "responses_tool_name_to_anthropic_name",
]
