"""This family's view of the fleet-wide complaint matcher.

The implementation moved to
:mod:`my_claude_code.providers.recovery.complaint` in 6.33.0, when the same
judgement was needed by the Anthropic Messages and Responses providers and a
provider must not import another provider's utilities. The module stays here
because ``providers/nvidia_nim`` and this package's own ``__init__`` publish
these names, and because the OpenAI-chat family is where the matcher's tests
live.
"""

from my_claude_code.providers.recovery import (
    complaint_evidence_snippet,
    is_bad_request,
    matched_token,
    sampling_parameter_evidence,
    upstream_complaint,
)

__all__ = [
    "complaint_evidence_snippet",
    "is_bad_request",
    "matched_token",
    "sampling_parameter_evidence",
    "upstream_complaint",
]
