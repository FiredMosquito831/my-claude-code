"""DeepSeek forced tool_choice rejection recovery.

DeepSeek's documented Chat Completions API accepts a forced named
``tool_choice`` (``{"type": "function", "function": {"name": ...}}``), but
some DeepSeek reasoning models reject it at request time with an HTTP 400
even though the shape is spec-compliant. This module detects that specific
rejection reactively (from the upstream error, never from the model name --
DeepSeek's model ids have already shifted once and are not a stable signal)
and produces a retry body that keeps the "must call some tool" half of the
caller's intent by downgrading to ``"required"``.
"""

from copy import deepcopy
from typing import Any

from my_claude_code.providers.recovery import is_bad_request, upstream_complaint

_REJECTION_HINTS = (
    "tool_choice",
    "tool choice",
)
_UNSUPPORTED_WORDS = (
    "not support",
    "unsupported",
    "does not support",
    "invalid",
    "not allowed",
    "not permitted",
)


def is_deepseek_tool_choice_rejection(error: Exception) -> bool:
    """Return whether an upstream error rejects a forced ``tool_choice``.

    Matches narrowly: the error must be a request-validation rejection AND the
    provider's own words must mention ``tool_choice`` (or "tool choice")
    together with an unsupported/rejection word (e.g. "does not support",
    "invalid", "not allowed"). Requiring both keeps this from firing on
    unrelated 400s such as context-length-exceeded or bad-schema errors, which
    never mention tool_choice at all.

    Echo-safe since 6.33.0: the words come from the fleet-wide matcher, which
    prunes the keys under which a validation error echoes the submitted request
    back. Reading that echo made every forced-tool request its own evidence --
    the body it sent contains ``tool_choice`` by definition, and one unrelated
    "invalid" anywhere in the same 400 was enough to complete the pair.
    """
    if not is_bad_request(error):
        return False
    complaint = upstream_complaint(error)
    mentions_tool_choice = any(hint in complaint for hint in _REJECTION_HINTS)
    mentions_rejection = any(word in complaint for word in _UNSUPPORTED_WORDS)
    return mentions_tool_choice and mentions_rejection


def clone_body_with_required_tool_choice(body: dict[str, Any]) -> dict[str, Any] | None:
    """Return a body clone with a forced named ``tool_choice`` downgraded.

    Downgrades OpenAI-shaped ``{"type": "function", "function": {"name": ...}}``
    to the string ``"required"`` -- the caller demanded a tool call, and
    ``"required"`` preserves that half even when the *which tool* half
    cannot be honoured. Returns ``None`` if there is nothing to downgrade
    (i.e. ``tool_choice`` was not a forced named choice), so the caller can
    tell "no retry available" apart from "retry with an unchanged body".
    """
    tool_choice = body.get("tool_choice")
    if not isinstance(tool_choice, dict) or tool_choice.get("type") != "function":
        return None

    cloned = deepcopy(body)
    cloned["tool_choice"] = "required"
    return cloned
