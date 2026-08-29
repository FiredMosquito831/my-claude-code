"""Wire-shape tests for the default OpenAI dialect and the declared ones.

Every OpenAI-compatible profile now declares ``reasoning_effort`` unless it was
probed speaking something else. Two halves are pinned here: that the twenty
profiles which used to send nothing now speak the standard field (and still
send nothing for OFF, which is what ``off=False`` on the default guarantees),
and that the hosts with a *measured* dialect -- OpenCode Zen's wider enum,
DeepSeek's thinking object, ollama, zenmux, kimi, featherless -- were not
flattened onto the default in the process.
"""

import pytest

from my_claude_code.config.model_overrides import ModelParameterOverrides
from my_claude_code.core.anthropic import ReasoningReplayMode
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import (
    ReasoningDialect,
    ReasoningDialectOrigin,
    ReasoningEffort,
    ReasoningPolicy,
)
from my_claude_code.providers.deepseek.compat import build_deepseek_request_body
from my_claude_code.providers.openai_chat.profiles import (
    GENERIC_OPENAI_PROFILE,
    OPENAI_CHAT_PROFILES,
    OPENAI_STANDARD_REASONING,
)
from my_claude_code.providers.openai_chat.reasoning import NoReasoning
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
# The twenty profiles that used to send nothing at all.
# --------------------------------------------------------------------------

# Every profile that carried ``NO_REASONING`` before PR F. Kept as data so the
# sweeps below are exhaustive and provable against the list the 5.70.0 audit
# left behind, rather than re-derived from whatever the registry happens to
# hold today.
_PREVIOUSLY_NO_REASONING = (
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


@pytest.mark.parametrize("provider_id", _PREVIOUSLY_NO_REASONING)
def test_every_previously_silent_profile_now_speaks_the_standard(
    provider_id: str,
) -> None:
    """The standard field, and only it -- no second dialect leaks in."""
    body = _profile_body(provider_id, ReasoningPolicy.on(effort=ReasoningEffort.MAX))
    extra_body = body.get("extra_body", {})

    assert body["reasoning_effort"] == "high", provider_id
    for field in ("reasoning", "thinking", "reasoning_split"):
        assert field not in body, provider_id
        assert field not in extra_body, provider_id
    assert "chat_template_kwargs" not in extra_body, provider_id


@pytest.mark.parametrize("provider_id", _PREVIOUSLY_NO_REASONING)
def test_a_previously_silent_profile_still_sends_nothing_for_off(
    provider_id: str,
) -> None:
    """OFF is byte-identical to the default body, exactly as before PR F.

    The default dialect has no ``disabled_value``, so it has no OFF spelling,
    so gating's "no OFF spelling -> nothing is sent" row applies. A user who
    explicitly turns reasoning off is never handed a level nobody asked for,
    and no host is sent a ``none`` it may not know.
    """
    assert _profile_body(provider_id, ReasoningPolicy.off()) == _profile_body(
        provider_id, ReasoningPolicy.provider_default()
    ), provider_id


@pytest.mark.parametrize("provider_id", _PREVIOUSLY_NO_REASONING)
def test_a_level_less_on_names_no_rung(provider_id: str) -> None:
    """An enabled value is a default rung, and nobody asked for a rung."""
    assert "reasoning_effort" not in _profile_body(provider_id, ReasoningPolicy.on())


def test_the_default_dialect_is_the_openai_standard_ladder() -> None:
    """The four rungs, the standard field, and no toggle, budget or OFF."""
    assert OPENAI_STANDARD_REASONING.dialect == ReasoningDialect(
        effort_values=frozenset(
            {
                ReasoningEffort.MINIMAL,
                ReasoningEffort.LOW,
                ReasoningEffort.MEDIUM,
                ReasoningEffort.HIGH,
            }
        ),
        toggle=False,
        budget=False,
        off=False,
        adaptive=False,
        effort_field="reasoning_effort",
        toggle_field="",
        budget_field="",
        origin=ReasoningDialectOrigin.DEFAULT,
    )


def test_the_default_ladder_is_a_subset_of_the_openai_sdk_enum() -> None:
    """Every word the default can emit is one the SDK's own type allows.

    Fails loudly the day the SDK narrows the enum, which is the only way this
    default could become wrong without anyone touching FCC.
    """
    from typing import get_args

    from openai.types.shared.reasoning_effort import ReasoningEffort as SdkEffort

    allowed = {value for value in get_args(get_args(SdkEffort)[0]) if value}
    emitted = {
        _profile_body("xai", ReasoningPolicy.on(effort=effort))["reasoning_effort"]
        for effort in ReasoningEffort
    }
    assert emitted <= allowed


def test_no_openai_chat_profile_is_silent_any_more() -> None:
    """``NoReasoning`` is unreachable from the registry and the fallback."""
    silent = [
        name
        for name, profile in OPENAI_CHAT_PROFILES.items()
        if isinstance(profile.reasoning, NoReasoning)
    ]
    assert silent == []
    assert not isinstance(GENERIC_OPENAI_PROFILE.reasoning, NoReasoning)


def test_declared_dialects_survive_the_default() -> None:
    """The other half of the guard: nobody else's shape was flattened."""
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
