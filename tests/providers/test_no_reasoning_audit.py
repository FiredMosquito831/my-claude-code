"""Wire-shape tests for the 2026-08-29 ``NO_REASONING`` audit.

Two providers were changed by that audit -- OpenCode Zen gained the
``reasoning_effort`` enum its gateway actually validates, and DeepSeek's
existing ``reasoning_effort`` stopped collapsing six requested efforts onto two
wire values. Every other profile was examined and deliberately left alone, so
the third test here pins their outbound bodies byte-for-byte: an audit that
documents a decision is only worth as much as the guard that keeps a later edit
to the shared encoders from quietly undoing it.
"""

import pytest

from my_claude_code.config.model_overrides import ModelParameterOverrides
from my_claude_code.core.anthropic import ReasoningReplayMode
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningEffort, ReasoningPolicy
from my_claude_code.providers.deepseek.compat import build_deepseek_request_body
from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES
from my_claude_code.providers.openai_chat.request_policy import (
    build_openai_chat_request_body,
)

# The enum OpenCode Zen names when it rejects an invalid ``reasoning_effort``,
# reproduced verbatim from the probe. Kept as data so the mapping test below
# fails loudly if FCC's own ladder ever grows a rung the gateway cannot take.
_OPENCODE_ACCEPTED_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)

# DeepSeek's enum, from its own deserialization error. Identical set.
_DEEPSEEK_ACCEPTED_EFFORTS = frozenset(
    {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
)


def _profile_body(provider_id: str, reasoning: ReasoningPolicy) -> dict:
    profile = OPENAI_CHAT_PROFILES[provider_id]
    return build_openai_chat_request_body(
        MessagesRequest.model_validate(
            {
                "model": "m",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "ping"}],
            }
        ),
        reasoning=reasoning,
        policy=profile.request_policy,
        postprocessors=profile.request_postprocessors,
        provider_id=provider_id,
        overrides=ModelParameterOverrides(),
    )


def _deepseek_body(reasoning: ReasoningPolicy) -> dict:
    return build_deepseek_request_body(
        MessagesRequest.model_validate(
            {
                "model": "deepseek-v4-flash",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": "ping"}],
            }
        ),
        reasoning=reasoning,
        provider_id="deepseek",
    )


# --------------------------------------------------------------------------
# OpenCode Zen: newly wired.
# --------------------------------------------------------------------------


def test_opencode_effort_max_sends_the_probed_gateway_enum() -> None:
    """The validated dialect is a top-level ``reasoning_effort`` string alone."""
    body = _profile_body("opencode", ReasoningPolicy.on(effort=ReasoningEffort.MAX))

    assert body["reasoning_effort"] == "max"
    # The gateway returns 200 and silently discards all three of these, so
    # emitting one would look like reasoning was requested when it was not.
    assert "reasoning" not in body
    assert "thinking" not in body
    assert "chat_template_kwargs" not in body.get("extra_body", {})


@pytest.mark.parametrize("effort", list(ReasoningEffort))
def test_opencode_every_effort_maps_onto_an_accepted_value(
    effort: ReasoningEffort,
) -> None:
    """The mapping is the identity: FCC's ladder is a subset of the enum."""
    body = _profile_body("opencode", ReasoningPolicy.on(effort=effort))

    assert body["reasoning_effort"] == effort.value
    assert body["reasoning_effort"] in _OPENCODE_ACCEPTED_EFFORTS


def test_opencode_off_sends_the_dialects_own_disabled_rung() -> None:
    """ "none" is in the probed enum, so OFF is expressible rather than silent."""
    body = _profile_body("opencode", ReasoningPolicy.off())

    assert body["reasoning_effort"] == "none"
    assert body["reasoning_effort"] in _OPENCODE_ACCEPTED_EFFORTS
    assert "thinking" not in body.get("extra_body", {})


def test_opencode_on_without_an_effort_keeps_the_gateway_default() -> None:
    """OpenCode reasons by default; inventing a rung would reduce it, not add."""
    body = _profile_body("opencode", ReasoningPolicy.on())

    assert "reasoning_effort" not in body


def test_opencode_replays_reasoning_on_the_field_it_arrives_on() -> None:
    """Deltas arrive as ``reasoning_content``, never as ``<think>`` tags."""
    profile = OPENAI_CHAT_PROFILES["opencode"]

    assert profile.reasoning_delta_field == "reasoning_content"
    assert profile.request_policy.reasoning_replay is (
        ReasoningReplayMode.REASONING_CONTENT
    )

    body = build_openai_chat_request_body(
        MessagesRequest.model_validate(
            {
                "model": "m",
                "max_tokens": 1024,
                "messages": [
                    {"role": "user", "content": "hi"},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "thinking",
                                "thinking": "earlier thought",
                                "signature": "sig",
                            },
                            {"type": "text", "text": "hello"},
                        ],
                    },
                    {"role": "user", "content": "again"},
                ],
            }
        ),
        reasoning=ReasoningPolicy.provider_default(),
        policy=profile.request_policy,
        postprocessors=profile.request_postprocessors,
        provider_id="opencode",
        overrides=ModelParameterOverrides(),
    )

    assistant = next(m for m in body["messages"] if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "earlier thought"
    assert "reasoning" not in assistant
    assert "<think>" not in str(assistant.get("content"))


# --------------------------------------------------------------------------
# DeepSeek: the effort vocabulary widened to the enum the API publishes.
# --------------------------------------------------------------------------


@pytest.mark.parametrize("effort", list(ReasoningEffort))
def test_deepseek_sends_the_effort_that_was_asked_for(
    effort: ReasoningEffort,
) -> None:
    """No collapsing: MINIMAL must not buy HIGH's latency and tokens."""
    body = _deepseek_body(ReasoningPolicy.on(effort=effort))

    assert body["reasoning_effort"] == effort.value
    assert body["reasoning_effort"] in _DEEPSEEK_ACCEPTED_EFFORTS
    assert "thinking" not in body.get("extra_body", {})


def test_deepseek_off_still_sends_the_disabled_thinking_object() -> None:
    """The OFF shape is unchanged by the audit; only the effort scale moved."""
    body = _deepseek_body(ReasoningPolicy.off())

    assert body["extra_body"]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in body


def test_deepseek_on_without_an_effort_enables_thinking_explicitly() -> None:
    body = _deepseek_body(ReasoningPolicy.on())

    assert body["extra_body"]["thinking"] == {"type": "enabled"}
    assert "reasoning_effort" not in body


# --------------------------------------------------------------------------
# Everything else the audit examined and deliberately did not change.
# --------------------------------------------------------------------------

# Examined on 2026-08-29 and left as ``NO_REASONING``. Each profile carries the
# evidence in a comment; this list is the executable half of that record.
_AUDITED_UNCHANGED = (
    "mistral_codestral",
    "opencode_go",
    "huggingface",
    "kimi_coding",
    "novita",
    "cline",
    "qwencloud",
    "qwencloud_coding",
    "xai",
    "together",
    "siliconflow",
    "chutes",
    "bedrock",
    "tokenrouter",
    "alibaba",
    "alibaba_cn",
    "alibaba_coding",
    "alibaba_coding_cn",
    "azure_openai",
)


@pytest.mark.parametrize("provider_id", _AUDITED_UNCHANGED)
@pytest.mark.parametrize(
    "reasoning",
    [
        ReasoningPolicy.provider_default(),
        ReasoningPolicy.on(effort=ReasoningEffort.MAX),
        ReasoningPolicy.off(),
    ],
    ids=["default", "max", "off"],
)
def test_audited_but_unchanged_providers_send_no_reasoning_control(
    provider_id: str, reasoning: ReasoningPolicy
) -> None:
    """Regression guard: the audit's verdicts stay verdicts.

    A body that is byte-identical across all three intents is the wire-level
    statement that this provider is asked for nothing. If a later edit to the
    shared encoders leaks a control into one of these, that is a provider the
    audit found no evidence for and the request would 400 on part of its
    roster.
    """
    body = _profile_body(provider_id, reasoning)
    extra_body = body.get("extra_body", {})

    for field in ("reasoning_effort", "reasoning", "thinking", "reasoning_split"):
        assert field not in body, provider_id
        assert field not in extra_body, provider_id
    assert "chat_template_kwargs" not in extra_body, provider_id
    assert body == _profile_body(provider_id, ReasoningPolicy.provider_default())


def test_neighbouring_dialects_are_untouched_by_the_audit() -> None:
    """The other half of the guard: nobody else's shape was erased either."""
    max_on = ReasoningPolicy.on(effort=ReasoningEffort.MAX)

    # Ollama: its own named-effort vocabulary, topping out at "max".
    assert _profile_body("ollama", max_on)["reasoning_effort"] == "max"
    # Zenmux: an ``extra_body`` reasoning object, not a flat string.
    zenmux = _profile_body("zenmux", max_on)
    assert zenmux["extra_body"]["reasoning"] == {"effort": "xhigh"}
    assert "reasoning_effort" not in zenmux
    # Kimi: an enabled/disabled thinking object.
    assert _profile_body("kimi", max_on)["extra_body"]["thinking"] == {
        "type": "enabled"
    }
    # Featherless: a chat-template boolean.
    assert _profile_body("featherless", max_on)["extra_body"][
        "chat_template_kwargs"
    ] == {"enable_thinking": True}
