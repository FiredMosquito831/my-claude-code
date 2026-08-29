"""NVIDIA NIM retry-body downgrade helpers.

Two concerns live here:

* the *evidence* side -- reading an upstream rejection and deciding which
  request field, if any, it actually complains about; and
* the *surgery* side -- cloning a request body with only that field removed.

The evidence side exists because a downgrade retry is only a recovery when the
provider objected to the thing being removed. NIM's reasoning instruction lives
in ``extra_body.chat_template_kwargs`` (``thinking`` / ``enable_thinking``, plus
``reasoning_budget``), so a rung that fires on the mere *shape* of a 400 turns
every unrelated rejection -- a sampling-parameter complaint such as
``Validation: top_p is immutable for this model and must be 0.95, got 1`` --
into a silent downgrade to a non-thinking request. Match on what the provider
said, the way ``providers/openai_chat/output_cap.py`` matches a named cap.
"""

import re
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from typing import Any

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

_CHAT_TEMPLATE_PATTERN = re.compile(r"\bchat_template(_kwargs)?\b")
_REASONING_CONTENT_PATTERN = re.compile(r"\breasoning_content\b")
_REASONING_BUDGET_PATTERN = re.compile(r"\breasoning_budget\b")
_THINKING_BUDGET_PATTERN = re.compile(r"\bthinking_token_budget\b")
_REASONING_CONFIG_PATTERN = re.compile(r"\breasoning_config\b")

# Sampling knobs. A 400 naming one of these is a complaint about how the model
# is sampled and has nothing to do with the chat template, so it must never
# cost the request its reasoning instruction.
_SAMPLING_PARAM_PATTERN = re.compile(
    r"\b("
    r"top_p|top_k|min_p|temperature|seed|"
    r"frequency_penalty|presence_penalty|repetition_penalty|length_penalty"
    r")\b"
)

# Enough of the provider's words to make a log line answerable, not so much
# that a verbose validation error floods the log.
_EVIDENCE_CHARS = 200


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


def _matched(pattern: re.Pattern[str], complaint: str) -> str | None:
    match = pattern.search(complaint)
    return match.group(0) if match else None


def chat_template_evidence(complaint: str) -> str | None:
    """Return the chat-template field the provider named, if it named one."""
    return _matched(_CHAT_TEMPLATE_PATTERN, complaint)


def reasoning_content_evidence(complaint: str) -> str | None:
    """Return the replayed-reasoning field the provider named, if it named one."""
    return _matched(_REASONING_CONTENT_PATTERN, complaint)


def reasoning_budget_evidence(complaint: str) -> str | None:
    """Return the thinking-budget control the provider named, if it named one."""
    budget = _matched(_REASONING_BUDGET_PATTERN, complaint)
    if budget is not None:
        return budget
    thinking_budget = _matched(_THINKING_BUDGET_PATTERN, complaint)
    if thinking_budget is not None and _REASONING_CONFIG_PATTERN.search(complaint):
        return thinking_budget
    return None


def sampling_parameter_evidence(complaint: str) -> str | None:
    """Return the sampling parameter the provider named, if it named one."""
    return _matched(_SAMPLING_PARAM_PATTERN, complaint)


def complaint_evidence_snippet(complaint: str) -> str:
    """Return a bounded excerpt of the complaint, for logs."""
    text = " ".join(complaint.split())
    if len(text) <= _EVIDENCE_CHARS:
        return text
    return f"{text[:_EVIDENCE_CHARS]}..."


def clone_body_without_reasoning_budget(body: dict[str, Any]) -> dict[str, Any] | None:
    """Clone a request body and strip only reasoning_budget fields."""
    return _clone_strip_extra_body(body, _strip_reasoning_budget_fields)


def clone_body_without_chat_template(body: dict[str, Any]) -> dict[str, Any] | None:
    """Clone a request body and strip NIM chat-template control fields."""
    return _clone_strip_extra_body(body, _strip_chat_template_fields)


def clone_body_without_reasoning_content(
    body: dict[str, Any],
) -> dict[str, Any] | None:
    """Clone a request body and strip assistant message ``reasoning_content`` fields."""
    cloned_body = deepcopy(body)
    if not _strip_message_reasoning_content(cloned_body):
        return None
    return cloned_body


def _clone_strip_extra_body(
    body: dict[str, Any],
    strip: Callable[[dict[str, Any]], bool],
) -> dict[str, Any] | None:
    cloned_body = deepcopy(body)
    extra_body = cloned_body.get("extra_body")
    if not isinstance(extra_body, dict):
        return None
    if not strip(extra_body):
        return None
    if not extra_body:
        cloned_body.pop("extra_body", None)
    return cloned_body


def _strip_reasoning_budget_fields(extra_body: dict[str, Any]) -> bool:
    removed = extra_body.pop("reasoning_budget", None) is not None
    chat_template_kwargs = extra_body.get("chat_template_kwargs")
    if (
        isinstance(chat_template_kwargs, dict)
        and chat_template_kwargs.pop("reasoning_budget", None) is not None
    ):
        removed = True
    return removed


def _strip_chat_template_fields(extra_body: dict[str, Any]) -> bool:
    removed = extra_body.pop("chat_template", None) is not None
    if extra_body.pop("chat_template_kwargs", None) is not None:
        removed = True
    return removed


def _strip_message_reasoning_content(body: dict[str, Any]) -> bool:
    removed = False
    messages = body.get("messages")
    if not isinstance(messages, list):
        return False
    for message in messages:
        if (
            isinstance(message, dict)
            and message.pop("reasoning_content", None) is not None
        ):
            removed = True
    return removed
