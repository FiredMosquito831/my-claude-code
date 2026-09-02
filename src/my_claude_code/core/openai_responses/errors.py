"""Errors and error envelopes for OpenAI Responses compatibility.

The envelope itself and the failure-kind mapping moved to
``core.openai_common.errors`` when Chat Completions arrived and needed to
report failure the same way; only the conversion error is Responses-specific.
The shared names stay importable from here because they are this package's
published surface -- ``core.openai_responses`` re-exports them and the
Responses handler reads them through that facade.
"""

from my_claude_code.core.openai_common import (
    openai_error_from_failure,
    openai_error_payload,
    openai_error_type_for_failure,
    openai_failure_payload,
)

__all__ = [
    "ResponsesConversionError",
    "openai_error_from_failure",
    "openai_error_payload",
    "openai_error_type_for_failure",
    "openai_failure_payload",
]


class ResponsesConversionError(ValueError):
    """Raised when a Responses request cannot be converted deterministically."""
