"""Recover from upstream output-token too-large 400 rejections.

Some hosts cap the per-request output token count below what Claude Code asks
for and reject the whole request with an HTTP 400 that names the allowed
maximum. Every dialect in the fleet says it differently::

    OpenAI-compatible   max_completion_tokens must be less than or equal to 40960
    Anthropic Messages  max_tokens: 100000 > 64000, which is the maximum allowed
                        number of output tokens for claude-sonnet-4-5-20250929
    OpenAI Responses    max_output_tokens must be at most 100000

This module parses that maximum and clamps the request body so the provider can
retry once and succeed. The caller also remembers the learned cap per model so
later requests clamp proactively instead of paying the 400 every time.

The field names differ per dialect and the *phrasings* deliberately do not: a
gateway may answer in any of these shapes regardless of protocol, so every
comparator pattern is tried for every dialect and only the body keys are
parameterised.
"""

import json
import re
from typing import Any

from .complaint import is_bad_request

# Body keys that carry the output-token budget across OpenAI-compatible policies.
OPENAI_CHAT_OUTPUT_FIELDS = ("max_completion_tokens", "max_tokens")

#: Anthropic Messages spells the budget with the one required field.
ANTHROPIC_OUTPUT_FIELDS = ("max_tokens",)

#: The OpenAI Responses endpoint renamed it.
RESPONSES_OUTPUT_FIELDS = ("max_output_tokens",)

# Comparator phrases that precede the allowed maximum in provider error text.
# ``cap`` is a named group because Anthropic states the *rejected* number first
# ("100000 > 64000"), so the allowed maximum is not always group 1.
_CAP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"less than or equal to\s+(?P<cap>\d+)"),
    re.compile(r"smaller than or equal to\s+(?P<cap>\d+)"),
    re.compile(r"<=\s*(?P<cap>\d+)"),
    re.compile(r"at most\s+(?P<cap>\d+)"),
    re.compile(r"must not exceed\s+(?P<cap>\d+)"),
    re.compile(r"maximum(?:\s+value)?(?:\s+for\s+\S+)?\s+is\s+(?P<cap>\d+)"),
    re.compile(r"maximum(?:\s+allowed)?(?:\s+value)?\s+of\s+(?P<cap>\d+)"),
    # Anthropic's own wording, quoted from its published error text.
    re.compile(
        r"\d+\s*>\s*(?P<cap>\d+)\s*,?\s*which is the maximum allowed number"
        r"\s+of output tokens"
    ),
)


def _error_text(error: Exception) -> str:
    text = str(error)
    body = getattr(error, "body", None)
    if body is not None:
        text = f"{text} {json.dumps(body, default=str)}"
    else:
        # Raw ``httpx`` paths carry the host's words only in the response, and
        # the exception message is a status line. Read it only when there is no
        # parsed body, so the OpenAI SDK path sees exactly the text it always
        # saw.
        response = getattr(error, "response", None)
        response_text = getattr(response, "text", None)
        if isinstance(response_text, str) and response_text:
            text = f"{text} {response_text}"
    return text.lower()


def parse_output_token_cap(
    error: Exception,
    *,
    fields: tuple[str, ...] = OPENAI_CHAT_OUTPUT_FIELDS,
) -> int | None:
    """Return the allowed output-token maximum named in a 400 rejection, if any.

    ``fields`` are the body keys this dialect uses for the budget; the error
    must name one of them, which is what keeps a context-length or
    schema-shaped 400 from being read as a cap.
    """
    if not is_bad_request(error):
        return None

    text = _error_text(error)
    if not any(keyword in text for keyword in fields):
        return None

    for pattern in _CAP_PATTERNS:
        match = pattern.search(text)
        if match:
            cap = int(match.group("cap"))
            if cap > 0:
                return cap
    return None


def clamp_output_tokens(
    body: dict[str, Any],
    cap: int,
    *,
    fields: tuple[str, ...] = OPENAI_CHAT_OUTPUT_FIELDS,
) -> dict[str, Any] | None:
    """Return a shallow clone with output-token fields clamped to ``cap``.

    Returns ``None`` when nothing needs clamping (no output field exceeds the
    cap), so callers can avoid a pointless identical retry.
    """
    clamped: dict[str, Any] | None = None
    for field in fields:
        value = body.get(field)
        if isinstance(value, int) and not isinstance(value, bool) and value > cap:
            if clamped is None:
                clamped = dict(body)
            clamped[field] = cap
    return clamped
