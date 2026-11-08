"""OpenAI Responses protocol adapter."""

from .adapter import OpenAIResponsesAdapter
from .errors import (
    openai_error_payload,
    openai_error_type_for_failure,
    openai_failure_payload,
)
from .models import OpenAIResponsesRequest
from .tools import call_id_from_item

__all__ = [
    "OpenAIResponsesAdapter",
    "OpenAIResponsesRequest",
    "call_id_from_item",
    "openai_error_payload",
    "openai_error_type_for_failure",
    "openai_failure_payload",
]
