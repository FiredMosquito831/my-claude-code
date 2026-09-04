"""Read an upstream rejection and decide which field it actually complains about.

A downgrade retry is only a recovery when the provider objected to the thing
being removed. A rung that fires on the mere *shape* of a 400 turns every
unrelated rejection -- a sampling-parameter complaint such as
``Validation: top_p is immutable for this model and must be 0.95, got 1`` --
into a silent downgrade of the request. Match on what the provider *said*, the
way :mod:`~my_claude_code.providers.recovery.output_cap` matches a named cap.

The subtlety, and the reason this lives in a module of its own: pydantic-style
validation errors echo the whole submitted request back under ``input``/``body``
/``ctx``. Anything found there names a field *we sent*, not a field the provider
objected to, so reading it as evidence makes every rung fire indiscriminately.
:func:`upstream_complaint` prunes those keys before returning the provider's
words.

It started as NVIDIA NIM's private helper, became the OpenAI-chat family's, and
is dialect-neutral here. The two error carriers differ by protocol and neither
is a dialect: the OpenAI SDK raises ``openai.BadRequestError`` with a parsed
``body``, while the Anthropic Messages and Responses paths speak raw ``httpx``
and carry the words in the response instead. Both are read here so a matcher is
written once for every provider in the fleet.
"""

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import httpx

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

# Statuses a host uses to say "your request was malformed". ``422`` alongside
# ``400`` because Mistral answers a rejected reasoning field with the pydantic
# status rather than the HTTP one.
_BAD_REQUEST_STATUSES = frozenset({400, 422})


def is_echo_key(name: str) -> bool:
    """Whether a body key echoes the *request* back rather than naming a fault.

    Public because a provider-specific matcher that walks the structured error
    itself -- Mistral's, which reads a pydantic ``loc`` path -- has to prune the
    same keys, and pruning them differently is exactly how a detector starts
    firing on the request it just sent.
    """
    return name.lower() in _ECHO_KEYS


def upstream_status_code(error: Exception) -> int | None:
    """Return the HTTP status an upstream answered with, across error carriers.

    The OpenAI SDK publishes ``status_code`` on the exception; a raw ``httpx``
    path raises :class:`httpx.HTTPStatusError`, which carries it on the
    response. Reading only the first is why the Anthropic-protocol providers
    never saw a recoverable 400 at all.
    """
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    return status if isinstance(status, int) else None


def is_bad_request(error: Exception) -> bool:
    """Whether an upstream error is a request-validation rejection."""
    # Deferred: ~2 s to import, and no startup path asks it anything.
    import openai

    if isinstance(error, openai.BadRequestError):
        return True
    return upstream_status_code(error) in _BAD_REQUEST_STATUSES


def upstream_error_payload(error: Exception) -> Any:
    """Return the parsed structured error body, whichever carrier holds it.

    ``None`` when the host sent nothing readable. The parse is deliberate:
    reading the response as one flat string would drag the echoed request back
    in, and pruning it is this module's entire reason to exist.
    """
    body = getattr(error, "body", None)
    if body is not None:
        return body
    response = getattr(error, "response", None)
    if not isinstance(response, httpx.Response):
        return None
    try:
        return json.loads(response.text)
    except Exception:
        return None


def upstream_complaint(error: Exception) -> str:
    """Return the provider's lowercased complaint, with the echoed request removed.

    Prefers the structured error body: pydantic-style validation errors carry
    the objection in ``msg``/``loc``/``type`` and the whole submitted request
    under ``input``, and only the former is evidence. Falls back to ``str`` when
    the body yields nothing readable.
    """
    parts: list[str] = []
    _collect_complaint(upstream_error_payload(error), parts, keyed=False)
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
