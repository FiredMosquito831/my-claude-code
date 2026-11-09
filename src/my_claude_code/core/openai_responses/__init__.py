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
)
from .errors import (
    openai_error_payload,
    openai_error_type_for_failure,
    openai_failure_payload,
)
from .models import OpenAIResponsesRequest
from .tools import call_id_from_item

__all__ = [
    "CARRIER_MAX_CHARS",
    "CARRIER_PREFIX",
    "CARRIER_PROTOCOL_RESPONSES",
    "ORIGIN_FIELDS",
    "CarrierPayload",
    "OpenAIResponsesAdapter",
    "OpenAIResponsesRequest",
    "ReplayOrigin",
    "accepts",
    "call_id_from_item",
    "connection_id",
    "decode_carrier",
    "encode_carrier",
    "is_carrier",
    "normalise_endpoint",
    "openai_error_payload",
    "openai_error_type_for_failure",
    "openai_failure_payload",
]
