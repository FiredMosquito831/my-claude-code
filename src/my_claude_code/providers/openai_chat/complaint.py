"""Read an upstream rejection and decide which field it actually complains about.

A downgrade retry is only a recovery when the provider objected to the thing
being removed. A rung that fires on the mere *shape* of a 400 turns every
unrelated rejection -- a sampling-parameter complaint such as
``Validation: top_p is immutable for this model and must be 0.95, got 1`` --
into a silent downgrade of the request. Match on what the provider *said*, the
way ``providers/openai_chat/output_cap.py`` matches a named cap.

The subtlety, and the reason this lives in a module of its own: pydantic-style
validation errors echo the whole submitted request back under ``input``/``body``
/``ctx``. Anything found there names a field *we sent*, not a field the provider
objected to, so reading it as evidence makes every rung fire indiscriminately.
:func:`upstream_complaint` prunes those keys before returning the provider's
words.

It started as NVIDIA NIM's private helper. It is fleet-wide now because the
create-level reasoning safety net in ``reasoning_reject.py`` needs exactly the
same judgement for every OpenAI-compatible host, and a provider must not import
another provider's utilities.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any

import openai

# Keys whose values carry the provider's own words about what was wrong.
_COMPLAINT_KEYS = frozenset(
    {
        "message",
        "detail",
        "details",
        "msg",
        "error",
        "errors",
        "reason",
        "param",
        "loc",
        "title",
        "type",
        "code",
    }
)

# Keys under which validation errors echo the *request* back. Anything found
# there names a field we sent, not a field the provider objected to, so reading
# it as evidence is what makes a rung fire indiscriminately.
_ECHO_KEYS = frozenset(
    {
        "input",
        "body",
        "request",
        "payload",
        "data",
        "ctx",
        "value",
        "received",
    }
)

# Sampling knobs. A 400 naming one of these is a complaint about how the model
# is sampled and has nothing to do with a reasoning instruction, so it must
# never cost the request its thinking.
_SAMPLING_PARAM_PATTERN = re.compile(
    r"\b("
    r"top_p|top_k|min_p|temperature|seed|"
    r"frequency_penalty|presence_penalty|repetition_penalty|length_penalty"
    r")\b"
)

# Enough of the provider's words to make a log line answerable, not so much
# that a verbose validation error floods the log.
_EVIDENCE_CHARS = 200


def is_bad_request(error: Exception) -> bool:
    """Whether an upstream error is a request-validation rejection.

    ``422`` alongside ``400`` because Mistral answers a rejected reasoning
    field with the pydantic status rather than the HTTP one, and this module is
    the fleet-wide owner of that judgement now.
    """
    status_code = getattr(error, "status_code", None)
    return isinstance(error, openai.BadRequestError) or status_code in (400, 422)


def upstream_complaint(error: Exception) -> str:
    """Return the provider's lowercased complaint, with the echoed request removed.

    Prefers the structured error body: pydantic-style validation errors carry
    the objection in ``msg``/``loc``/``type`` and the whole submitted request
    under ``input``, and only the former is evidence. Falls back to ``str`` when
    the body yields nothing readable.
    """
    parts: list[str] = []
    _collect_complaint(getattr(error, "body", None), parts, keyed=False)
    if parts:
        return " ".join(parts).lower()
    return str(error).lower()


def _collect_complaint(value: Any, parts: list[str], *, keyed: bool) -> None:
    if isinstance(value, str):
        if keyed:
            parts.append(value)
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key).lower()
            if name in _ECHO_KEYS:
                continue
            _collect_complaint(item, parts, keyed=name in _COMPLAINT_KEYS)
        return
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        for item in value:
            _collect_complaint(item, parts, keyed=keyed)


def matched_token(pattern: re.Pattern[str], complaint: str) -> str | None:
    """Return the first substring of ``complaint`` the pattern names, if any."""
    match = pattern.search(complaint)
    return match.group(0) if match else None


def sampling_parameter_evidence(complaint: str) -> str | None:
    """Return the sampling parameter the provider named, if it named one."""
    return matched_token(_SAMPLING_PARAM_PATTERN, complaint)


def complaint_evidence_snippet(complaint: str) -> str:
    """Return a bounded excerpt of the complaint, for logs."""
    text = " ".join(complaint.split())
    if len(text) <= _EVIDENCE_CHARS:
        return text
    return f"{text[:_EVIDENCE_CHARS]}..."
