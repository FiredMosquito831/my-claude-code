"""NVIDIA NIM request option injection."""

from copy import deepcopy
from typing import Any

from my_claude_code.config.nim import NimSettings
from my_claude_code.core.anthropic import ReasoningReplayMode, set_if_not_none
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningControl, ReasoningPolicy
from my_claude_code.providers.openai_chat import (
    OpenAIChatRequestPolicy,
    build_openai_chat_request_body,
)

from .tool_schema import sanitize_nim_tool_schemas

NIM_REQUEST_POLICY = OpenAIChatRequestPolicy(
    provider_name="NIM",
    reasoning_replay=ReasoningReplayMode.REASONING_CONTENT,
)


def build_nim_request_body(
    request_data: MessagesRequest,
    nim: NimSettings,
    *,
    reasoning: ReasoningPolicy,
    provider_id: str = "",
) -> dict[str, Any]:
    """Build OpenAI-format request body from Anthropic request plus NIM settings."""
    return build_openai_chat_request_body(
        request_data,
        reasoning=reasoning,
        policy=NIM_REQUEST_POLICY,
        postprocessors=(
            lambda body, request, policy: apply_nim_request_options(
                body,
                request,
                policy,
                nim=nim,
            ),
        ),
        provider_id=provider_id,
    )


def apply_nim_request_options(
    body: dict[str, Any],
    request_data: MessagesRequest,
    reasoning: ReasoningPolicy,
    *,
    nim: NimSettings,
) -> None:
    """Apply NIM schema repairs and configured request defaults."""
    sanitize_nim_tool_schemas(body)

    # nim.max_tokens is unset by default, so the client's value passes through
    # untouched and the model's own output limit governs. It only clamps when
    # an operator has deliberately configured a cap. The reasoning budget is
    # sized separately, against the model's real limit.output from models.dev
    # (see application/reasoning_gating.py), rather than against any constant
    # here.
    max_tokens = body.get("max_tokens") or request_data.max_tokens
    if max_tokens is None:
        max_tokens = nim.max_tokens
    elif nim.max_tokens:
        max_tokens = min(max_tokens, nim.max_tokens)
    set_if_not_none(body, "max_tokens", max_tokens)

    if body.get("temperature") is None and nim.temperature is not None:
        body["temperature"] = nim.temperature
    if body.get("top_p") is None and nim.top_p is not None:
        body["top_p"] = nim.top_p

    if "stop" not in body and nim.stop:
        body["stop"] = nim.stop

    # ``is not None`` rather than a comparison against the default: an unset
    # penalty must stay out of the body entirely so NIM applies its own,
    # while a deliberate 0.0 must still be sent. Testing against the default
    # made those two cases indistinguishable.
    if nim.presence_penalty is not None:
        body["presence_penalty"] = nim.presence_penalty
    if nim.frequency_penalty is not None:
        body["frequency_penalty"] = nim.frequency_penalty
    if nim.seed is not None:
        body["seed"] = nim.seed

    # ``is not None``, not truthiness: an explicit False must still be
    # sent, while unset stays out of the body so NIM applies its own
    # per-model default.
    if nim.parallel_tool_calls is not None:
        body["parallel_tool_calls"] = nim.parallel_tool_calls

    extra_body: dict[str, Any] = {}
    request_extra = request_data.extra_body
    if request_extra:
        extra_body.update(deepcopy(request_extra))
    for key in (
        "reasoning",
        "reasoning_budget",
        "reasoning_effort",
        "reasoning_tokens",
        "thinking",
        "thinking_budget_tokens",
    ):
        extra_body.pop(key, None)
    request_template_kwargs = extra_body.get("chat_template_kwargs")
    if isinstance(request_template_kwargs, dict):
        for key in ("thinking", "enable_thinking", "reasoning_budget"):
            request_template_kwargs.pop(key, None)
        if not request_template_kwargs:
            extra_body.pop("chat_template_kwargs", None)

    if reasoning.control is ReasoningControl.OFF or reasoning.requests_reasoning:
        chat_template_kwargs = extra_body.setdefault("chat_template_kwargs", {})
        if isinstance(chat_template_kwargs, dict):
            enabled = reasoning.control is not ReasoningControl.OFF
            chat_template_kwargs["thinking"] = enabled
            chat_template_kwargs["enable_thinking"] = enabled
            if enabled and (budget := reasoning.numeric_budget_tokens) is not None:
                chat_template_kwargs["reasoning_budget"] = budget

    req_top_k = request_data.top_k
    top_k = req_top_k if req_top_k is not None else nim.top_k
    # -1 stays an ignored sentinel here only to absorb a client that spells
    # "unset" that way; nim.top_k is already None when unset.
    _set_extra(extra_body, "top_k", top_k, ignore_value=-1)
    # No ignore_value on the rest: unset is None and is dropped by _set_extra,
    # so a configured 0.0 / 1.0 / 0 is a real choice and must reach NIM.
    _set_extra(extra_body, "min_p", nim.min_p)
    _set_extra(extra_body, "repetition_penalty", nim.repetition_penalty)
    _set_extra(extra_body, "min_tokens", nim.min_tokens)
    _set_extra(extra_body, "chat_template", nim.chat_template)
    _set_extra(extra_body, "request_id", nim.request_id)
    _set_extra(extra_body, "ignore_eos", nim.ignore_eos)

    if extra_body:
        body["extra_body"] = extra_body


def _set_extra(
    extra_body: dict[str, Any], key: str, value: Any, ignore_value: Any = None
) -> None:
    if key in extra_body:
        return
    if value is None:
        return
    if ignore_value is not None and value == ignore_value:
        return
    extra_body[key] = value
