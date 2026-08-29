"""NVIDIA NIM retry-body downgrade helpers.

The *surgery* side of a downgrade retry: cloning a request body with only the
field NIM named removed. The *evidence* side -- reading an upstream rejection
and deciding which field it actually complains about -- moved to
``providers/openai_chat/complaint.py`` when the create-level reasoning safety
net needed the same judgement fleet-wide; the patterns below are the ones only
NIM speaks, because NIM's reasoning control lives in
``extra_body.chat_template_kwargs`` (``thinking`` / ``enable_thinking``, plus
``reasoning_budget``) rather than in the standard field.
"""

import re
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from my_claude_code.providers.openai_chat import matched_token

_CHAT_TEMPLATE_PATTERN = re.compile(r"\bchat_template(_kwargs)?\b")
_REASONING_CONTENT_PATTERN = re.compile(r"\breasoning_content\b")
_REASONING_BUDGET_PATTERN = re.compile(r"\breasoning_budget\b")
_THINKING_BUDGET_PATTERN = re.compile(r"\bthinking_token_budget\b")
_REASONING_CONFIG_PATTERN = re.compile(r"\breasoning_config\b")


def chat_template_evidence(complaint: str) -> str | None:
    """Return the chat-template field the provider named, if it named one."""
    return matched_token(_CHAT_TEMPLATE_PATTERN, complaint)


def reasoning_content_evidence(complaint: str) -> str | None:
    """Return the replayed-reasoning field the provider named, if it named one."""
    return matched_token(_REASONING_CONTENT_PATTERN, complaint)


def reasoning_budget_evidence(complaint: str) -> str | None:
    """Return the thinking-budget control the provider named, if it named one."""
    budget = matched_token(_REASONING_BUDGET_PATTERN, complaint)
    if budget is not None:
        return budget
    thinking_budget = matched_token(_THINKING_BUDGET_PATTERN, complaint)
    if thinking_budget is not None and _REASONING_CONFIG_PATTERN.search(complaint):
        return thinking_budget
    return None


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
