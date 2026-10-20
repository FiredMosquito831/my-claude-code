"""Anthropic gets the ``thinking`` it accepts, and MCC stops learning false refusals (7.102.4).

Three things were wrong after 7.102.1, all in the Anthropic Messages family:

* **The ``enabled`` leak.** ``ThinkingConfig.enabled`` defaults to ``True``,
  the client's ``thinking`` object passes through the builder as a dump, and
  Anthropic answered every such body ``thinking.<type>.enabled: Extra inputs
  are not permitted``. The strip rung then retried without the ``thinking``
  field, so every Claude-subscription request since 2026-10-08 paid a 400 and
  a second upstream call. Anthropic still thought on the retry -- Claude 5
  thinks by default when ``thinking`` is omitted, and 100 of 104 answers
  carried a signed thinking block -- but the text was withheld, as the
  client's own display choice asks.
* **The false fact.** That retry was accepted, so the rung's acceptance
  remembered "claude-haiku-5-5 rejects ``thinking``" -- from a 400 about a key
  MCC itself added. Any 400 that names the word ``thinking`` (a signature in
  the history, Anthropic's manual-mode rule) taught the same thing.
* **Manual mode after another model's tool call.** Anthropic's manual
  thinking requires its own thinking at the start of the final assistant turn
  of a tool loop; once 7.102.1 drops the other model's thinking there, that
  request must go without the ``thinking`` field -- what the DeepSeek builder
  already does for a tool follow-up it cannot replay.

Pinned here: the leak is gone from both pass-through exits and nowhere else;
the manual-mode request drops the field only in that exact case; the learner
skips 400s that fault the body and keeps real refusals; and the store
forgets, on load, the facts the old learner wrote from such a 400.
"""

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningDialectOrigin, ReasoningPolicy
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.anthropic_messages import AnthropicMessagesProvider
from my_claude_code.providers.anthropic_messages.request import (
    FOREIGN_THINKING_DROPPED,
    build_anthropic_messages_body,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.recovery import (
    LearnedFactStore,
    complaint_is_about_the_body,
)
from tests.providers.support import passthrough_rate_limiter

MODEL = "claude-haiku-5-5"
FOREIGN = {"type": "thinking", "thinking": "agnes planned the read", "signature": ""}
SIGNED = {"type": "thinking", "thinking": "Anthropic planned it", "signature": "EqQBCk"}
TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"p": "a"}}
TOOL_RESULT = {"type": "tool_result", "tool_use_id": "toolu_1", "content": "x = 1"}
MANUAL = {"type": "enabled", "budget_tokens": 4000}

#: Anthropic's own wordings, as they reached the live request log.
ENABLED_400 = "thinking.adaptive.enabled: Extra inputs are not permitted"
SIGNATURE_400 = "messages.1.content.0: Invalid `signature` in `thinking` block"
MANUAL_MODE_400 = (
    "messages.1.content.0.type: Expected `thinking` or `redacted_thinking`, but "
    "found `tool_use`. When `thinking` is enabled, a final `assistant` message "
    "must start with a thinking block (preceeding the lastmost set of `tool_use` "
    "and `tool_result` blocks). We recommend you include thinking blocks from "
    "previous turns. To avoid this requirement, disable `thinking`."
)


def _request(
    messages: list[dict[str, Any]], thinking: dict[str, Any] | None
) -> MessagesRequest:
    payload: dict[str, Any] = {
        "model": MODEL,
        "max_tokens": 16000,
        "messages": messages,
    }
    if thinking is not None:
        payload["thinking"] = thinking
    return MessagesRequest.model_validate(payload)


def _thinking_sent(
    thinking: dict[str, Any] | None,
    policy: ReasoningPolicy,
    messages: list[dict[str, Any]] | None = None,
) -> Any:
    body = build_anthropic_messages_body(
        _request(messages or [{"role": "user", "content": "hi"}], thinking),
        reasoning=policy,
    )
    return body.get("thinking", "<absent>")


# --------------------------------------------------------------------------
# The enabled leak
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("client", "policy", "sent"),
    [
        (
            {"type": "adaptive"},
            ReasoningPolicy.provider_default(),
            {"type": "adaptive"},
        ),
        (MANUAL, ReasoningPolicy.provider_default(), MANUAL),
        (
            {"type": "disabled"},
            ReasoningPolicy.provider_default(),
            {"type": "disabled"},
        ),
        ({"type": "adaptive"}, ReasoningPolicy.on(), {"type": "adaptive"}),
        (MANUAL, ReasoningPolicy.on(), MANUAL),
    ],
    ids=[
        "default-adaptive",
        "default-manual",
        "default-disabled",
        "on-adaptive",
        "on-manual",
    ],
)
def test_a_passed_through_thinking_object_carries_no_enabled_key(
    client: dict[str, Any], policy: ReasoningPolicy, sent: dict[str, Any]
) -> None:
    thinking = _thinking_sent(client, policy)

    assert thinking == sent
    assert list(thinking) == list(sent)


def test_an_object_without_a_type_is_left_alone() -> None:
    sent = _thinking_sent({"budget_tokens": 2000}, ReasoningPolicy.provider_default())

    assert sent == {"enabled": True, "budget_tokens": 2000}


@pytest.mark.parametrize(
    ("policy", "sent"),
    [
        (
            ReasoningPolicy.on(budget_tokens=2048),
            {"type": "enabled", "budget_tokens": 2048},
        ),
        (ReasoningPolicy.adaptive(), {"type": "adaptive"}),
        (ReasoningPolicy.off(), "<absent>"),
    ],
    ids=["budget", "adaptive", "off"],
)
def test_objects_mcc_builds_itself_are_unchanged(
    policy: ReasoningPolicy, sent: Any
) -> None:
    assert _thinking_sent({"type": "adaptive"}, policy) == sent


def test_the_model_dump_other_dialects_read_still_says_enabled() -> None:
    request = _request([{"role": "user", "content": "hi"}], {"type": "adaptive"})

    assert request.model_dump(exclude_none=True)["thinking"]["enabled"] is True


# --------------------------------------------------------------------------
# Manual mode after another model's tool call
# --------------------------------------------------------------------------


def _tool_loop(*final_turn: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"role": "user", "content": "Read a.py"},
        {"role": "assistant", "content": list(final_turn)},
        {"role": "user", "content": [TOOL_RESULT]},
    ]


def test_a_manual_tool_follow_up_to_another_models_call_drops_the_field() -> None:
    notes: dict[str, str] = {}
    body = build_anthropic_messages_body(
        _request(_tool_loop(FOREIGN, TOOL_USE), MANUAL),
        reasoning=ReasoningPolicy.provider_default(),
        wire_notes=notes,
    )

    assert "thinking" not in body
    assert body["messages"][1] == {"role": "assistant", "content": [TOOL_USE]}
    assert notes[FOREIGN_THINKING_DROPPED].endswith(
        "; this tool follow-up was sent without the thinking field, because "
        "manual thinking needs Anthropic's own thinking ahead of a tool call and "
        "another model made this one"
    )


def test_a_budget_mcc_built_is_manual_too() -> None:
    sent = _thinking_sent(
        {"type": "adaptive"},
        ReasoningPolicy.on(budget_tokens=2048),
        _tool_loop(FOREIGN, TOOL_USE),
    )

    assert sent == "<absent>"


def test_adaptive_mode_keeps_its_thinking_on_the_same_history() -> None:
    notes: dict[str, str] = {}
    body = build_anthropic_messages_body(
        _request(_tool_loop(FOREIGN, TOOL_USE), {"type": "adaptive"}),
        reasoning=ReasoningPolicy.provider_default(),
        wire_notes=notes,
    )

    assert body["thinking"] == {"type": "adaptive"}
    assert notes[FOREIGN_THINKING_DROPPED].endswith("own signed thinking")


@pytest.mark.parametrize(
    "history",
    [
        _tool_loop(SIGNED, TOOL_USE),
        _tool_loop(TOOL_USE),
        _tool_loop(SIGNED, FOREIGN, TOOL_USE),
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": [FOREIGN, {"type": "text", "text": "t"}]},
            {"role": "user", "content": "b"},
            {"role": "assistant", "content": [SIGNED, TOOL_USE]},
            {"role": "user", "content": [TOOL_RESULT]},
        ],
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": [FOREIGN, {"type": "text", "text": "t"}]},
            {"role": "user", "content": "b"},
        ],
    ],
    ids=[
        "own-signed-turn",
        "no-thinking-at-all",
        "still-starts-signed",
        "foreign-only-earlier",
        "no-tool-call",
    ],
)
def test_manual_mode_keeps_its_thinking_everywhere_else(
    history: list[dict[str, Any]],
) -> None:
    assert _thinking_sent(MANUAL, ReasoningPolicy.provider_default(), history) == MANUAL


def test_a_thinking_only_turn_after_the_tool_call_does_not_hide_it() -> None:
    history = [
        {"role": "user", "content": "Read a.py"},
        {"role": "assistant", "content": [FOREIGN, TOOL_USE]},
        {"role": "user", "content": [TOOL_RESULT]},
        {"role": "assistant", "content": [FOREIGN]},
        {"role": "user", "content": "go on"},
    ]

    assert _thinking_sent(MANUAL, ReasoningPolicy.provider_default(), history) == (
        "<absent>"
    )


# --------------------------------------------------------------------------
# What the learner may remember
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "complaint",
    [
        ENABLED_400,
        "thinking.enabled.enabled: Extra inputs are not permitted",
        SIGNATURE_400,
        MANUAL_MODE_400,
        "messages.3.content.0.thinking: each thinking block must contain non-whitespace thinking",
        "error invalid_request_error thinking.adaptive.enabled: extra inputs are not permitted",
    ],
    ids=[
        "enabled-adaptive",
        "enabled-manual",
        "signature",
        "manual-mode",
        "blank",
        "stored",
    ],
)
def test_a_400_that_faults_the_body_is_not_a_refusal(complaint: str) -> None:
    assert complaint_is_about_the_body(complaint, "thinking") is True


@pytest.mark.parametrize(
    "complaint",
    [
        "thinking: Extra inputs are not permitted",
        "thinking.budget_tokens: Input should be greater than or equal to 1024",
        "thinking.type: Input tag 'adaptive' found using 'type' does not match any "
        "of the expected tags: 'disabled', 'enabled'",
        "Unsupported parameter: thinking",
        "Invalid input",
    ],
    ids=["field-extra", "budget", "adaptive-tag", "unsupported", "commandcode"],
)
def test_a_400_about_the_field_itself_still_is(complaint: str) -> None:
    assert complaint_is_about_the_body(complaint, "thinking") is False


_SSE = (
    b'event: message_start\ndata: {"type":"message_start"}\n\n'
    b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
)


def _error(message: str) -> httpx.Response:
    return httpx.Response(
        400,
        json={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": message},
        },
    )


def _provider(handler: Any, sent: list[dict[str, Any]]) -> AnthropicMessagesProvider:
    provider = AnthropicMessagesProvider(
        ProviderConfig(api_key="sk-ant-test", base_url="https://upstream.invalid/v1"),
        provider_name="UPSTREAM",
        rate_limiter=passthrough_rate_limiter(),
    )

    def wrapped(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return handler(sent[-1])

    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(wrapped))
    return provider


async def _drain(stream: AsyncIterator[str]) -> list[str]:
    return [frame async for frame in stream]


def _anthropic_rules(body: dict[str, Any]) -> httpx.Response:
    thinking = body.get("thinking")
    if isinstance(thinking, dict) and "enabled" in thinking:
        return _error(
            f"thinking.{thinking.get('type')}.enabled: Extra inputs are not permitted"
        )
    return httpx.Response(200, content=_SSE)


@pytest.mark.asyncio
async def test_a_passed_through_object_is_accepted_in_one_call() -> None:
    sent: list[dict[str, Any]] = []
    provider = _provider(_anthropic_rules, sent)

    await _drain(
        provider.stream_response(
            _request([{"role": "user", "content": "hi"}], {"type": "adaptive"}),
            reasoning=ReasoningPolicy.provider_default(),
        )
    )

    assert [item.get("thinking") for item in sent] == [{"type": "adaptive"}]
    assert (
        provider.reasoning_dialect(MODEL).origin is not ReasoningDialectOrigin.LEARNED
    )
    await provider.cleanup()


@pytest.mark.asyncio
async def test_a_retry_after_a_400_about_the_body_is_not_remembered() -> None:
    """The retry is unchanged; only the durable refusal is withheld."""

    sent: list[dict[str, Any]] = []

    def handler(body: dict[str, Any]) -> httpx.Response:
        if "thinking" in body:
            return _error(MANUAL_MODE_400)
        return httpx.Response(200, content=_SSE)

    provider = _provider(handler, sent)
    trace = install_wire_trace()

    frames = await _drain(
        provider.stream_response(
            _request([{"role": "user", "content": "hi"}], None),
            reasoning=ReasoningPolicy.on(budget_tokens=2048),
        )
    )

    assert any("message_stop" in frame for frame in frames)
    assert ["thinking" in item for item in sent] == [True, False]
    assert (
        provider.reasoning_dialect(MODEL).origin is not ReasoningDialectOrigin.LEARNED
    )
    assert provider._recovery_memory.rejections_for(MODEL) is None
    assert trace.reasoning_adaptations == []
    await provider.cleanup()


@pytest.mark.asyncio
async def test_a_real_refusal_of_the_field_is_still_remembered() -> None:
    sent: list[dict[str, Any]] = []

    def handler(body: dict[str, Any]) -> httpx.Response:
        if "thinking" in body:
            return _error("thinking: Extra inputs are not permitted")
        return httpx.Response(200, content=_SSE)

    provider = _provider(handler, sent)

    await _drain(
        provider.stream_response(
            _request([{"role": "user", "content": "hi"}], None),
            reasoning=ReasoningPolicy.on(budget_tokens=2048),
        )
    )

    assert provider.reasoning_dialect(MODEL).origin is ReasoningDialectOrigin.LEARNED
    await provider.cleanup()


# --------------------------------------------------------------------------
# Forgetting what the old learner wrote
# --------------------------------------------------------------------------


def _row(
    evidence: str,
    model: str = MODEL,
    kind: str = "reasoning_field_rejected",
    detail: str = "thinking",
    provider: str = "anthropic_oauth",
) -> dict[str, Any]:
    return {
        "provider_id": provider,
        "model_id": model,
        "fact_kind": kind,
        "value": True,
        "learned_at": "2026-10-08T23:08:26Z",
        "last_confirmed_at": "2026-10-11T19:20:16Z",
        "source": "rejection",
        "evidence": evidence,
        "hits": 101,
        "detail": detail,
    }


def test_another_familys_fact_is_never_forgotten_here(tmp_path: Path) -> None:
    """Only the field the tightened learner owns is swept on load."""

    path = tmp_path / "learned_facts.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "facts": [
                    _row(
                        "error chat_template_kwargs.enable_thinking: extra inputs "
                        "are not permitted",
                        model="kimi-k3",
                        detail="chat_template_kwargs",
                        provider="nvidia_nim",
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    store = LearnedFactStore(path=path, flush_debounce_seconds=0.0)

    store.load()

    assert [(f.provider_id, f.detail) for f in store.all_facts()] == [
        ("nvidia_nim", "chat_template_kwargs")
    ]
    assert store.flush() is False


def test_the_store_forgets_a_refusal_learned_from_a_400_about_the_body(
    tmp_path: Path,
) -> None:
    path = tmp_path / "learned_facts.json"
    the_false_fact = _row(
        "error invalid_request_error thinking.adaptive.enabled: extra inputs are "
        "not permitted"
    )
    a_real_one = _row(
        "error invalid_request_error thinking: extra inputs are not permitted",
        model="claude-3-haiku",
    )
    path.write_text(
        json.dumps({"version": 2, "facts": [the_false_fact, a_real_one]}),
        encoding="utf-8",
    )
    store = LearnedFactStore(path=path, flush_debounce_seconds=0.0)

    store.load()

    assert [(f.model_id, f.detail) for f in store.all_facts()] == [
        ("claude-3-haiku", "thinking")
    ]
    assert store.memory_for("anthropic_oauth").rejections_for(MODEL) is None
    assert store.flush() is True
    written = json.loads(path.read_text(encoding="utf-8"))
    assert [row["model_id"] for row in written["facts"]] == ["claude-3-haiku"]


def test_a_document_with_nothing_to_forget_is_not_rewritten(tmp_path: Path) -> None:
    path = tmp_path / "learned_facts.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "facts": [_row("error invalid_request_error thinking: extra inputs")],
            }
        ),
        encoding="utf-8",
    )
    store = LearnedFactStore(path=path, flush_debounce_seconds=0.0)

    store.load()

    assert len(store.all_facts()) == 1
    assert store.flush() is False
