"""Caller ``extra_body`` must reach every chat provider that accepts it."""

from typing import Any

import pytest

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.providers.openai_chat.extra_body import (
    validate_extra_body_does_not_override_canonical_fields,
)
from my_claude_code.providers.openai_chat.profiles import (
    GENERIC_OPENAI_PROFILE,
    OPENAI_CHAT_PROFILES,
    OpenAIChatProfile,
)
from my_claude_code.providers.openai_chat.request_policy import (
    build_openai_chat_request_body,
)

# Loud rejection and validated allowlist merging are deliberate contracts, not
# silent drops: neither loses a caller's extra_body without saying so.
LOUD_REJECTERS = frozenset({"kimi", "kimi_coding", "zai"})
ALLOWLIST_MERGERS = frozenset({"cohere"})


def _all_profiles() -> dict[str, OpenAIChatProfile]:
    return {**OPENAI_CHAT_PROFILES, "CUSTOM": GENERIC_OPENAI_PROFILE}


def _passthrough_names() -> list[str]:
    return sorted(
        name
        for name in _all_profiles()
        if name not in LOUD_REJECTERS and name not in ALLOWLIST_MERGERS
    )


def _request(**overrides: object) -> MessagesRequest:
    data: dict[str, object] = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 100,
        "temperature": 0.5,
        "tools": [],
        "extra_body": {},
        "thinking": {"enabled": True},
    }
    data.update(overrides)
    return MessagesRequest.model_validate(data)


def _build(profile_name: str, extra: dict[str, object]) -> dict[str, Any]:
    profile = _all_profiles()[profile_name]
    return build_openai_chat_request_body(
        _request(extra_body=extra),
        reasoning=ReasoningPolicy.on(),
        policy=profile.request_policy,
        postprocessors=profile.request_postprocessors,
    )


def test_every_former_dropper_now_passes_caller_extra_body_through() -> None:
    for name in _passthrough_names():
        policy = _all_profiles()[name].request_policy
        assert policy.include_extra_body, f"{name} silently drops caller extra_body"
        assert (
            policy.extra_body_validator
            is validate_extra_body_does_not_override_canonical_fields
        ), f"{name} guards extras with a validator other than the canonical one"


def test_loud_rejecters_keep_their_explicit_contract() -> None:
    for name in sorted(LOUD_REJECTERS):
        policy = OPENAI_CHAT_PROFILES[name].request_policy
        assert not policy.include_extra_body
        assert policy.reject_extra_body_message


def test_cohere_keeps_its_allowlist_merge_contract() -> None:
    policy = OPENAI_CHAT_PROFILES["cohere"].request_policy
    assert not policy.include_extra_body
    assert any(
        getattr(postprocessor, "__name__", "") == "_apply_cohere_request_quirks"
        for postprocessor in OPENAI_CHAT_PROFILES["cohere"].postprocessors
    )


@pytest.mark.parametrize("name", _passthrough_names())
def test_extra_body_reaches_the_wire(name: str) -> None:
    # Reasoning encoders for some providers write their control keys into the
    # same mapping, so the contract is caller-key survival, not exact equality.
    body = _build(name, {"frequency_penalty": 0.5})

    assert body["extra_body"]["frequency_penalty"] == 0.5


@pytest.mark.parametrize("name", _passthrough_names())
def test_extra_body_cannot_override_canonical_fields(name: str) -> None:
    with pytest.raises(InvalidRequestError):
        _build(name, {"temperature": 1})
