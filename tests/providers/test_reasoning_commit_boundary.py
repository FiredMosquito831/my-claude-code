"""Whether a stream that has only reasoned may still fall back.

The setting is one boolean travelling four hops -- ``Settings`` ->
``ProviderConfig`` -> ``RecoveryController`` -> ``RecoveryHoldbackBuffer``.
Every hop is a one-line assignment, which is exactly the shape that registers
a control and then reaches nothing, so the important test here is the one that
drives a real provider stream and reads what came out.
"""

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import ReasoningReplayMode
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.stream_contracts import REASONING_HEARTBEAT
from my_claude_code.core.reasoning import DEFAULT_REASONING_POLICY, ReasoningPolicy
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import (
    OpenAIChatProfile,
    OpenAIChatProvider,
    OpenAIChatRequestPolicy,
)
from my_claude_code.providers.openai_chat.reasoning import NO_REASONING
from tests.providers.request_factory import make_messages_request
from tests.providers.support import passthrough_rate_limiter


class _ReasoningProvider(OpenAIChatProvider):
    def __init__(self, *, fallback_on_reasoning_only: bool):
        super().__init__(
            ProviderConfig(
                api_key="test_key",
                base_url="https://provider.example/v1",
                rate_limit=100,
                rate_window=60,
                fallback_on_reasoning_only=fallback_on_reasoning_only,
            ),
            profile=OpenAIChatProfile(
                OpenAIChatRequestPolicy(
                    provider_name="REASONING_TEST",
                    reasoning_replay=ReasoningReplayMode.DISABLED,
                ),
                NO_REASONING,
            ),
            rate_limiter=passthrough_rate_limiter(),
        )

    def _build_request_body(
        self,
        request: MessagesRequest,
        *,
        reasoning: ReasoningPolicy = DEFAULT_REASONING_POLICY,
    ) -> dict:
        return {"model": request.model, "messages": [{"role": "user", "content": "x"}]}


async def _stream(chunks):
    for chunk in chunks:
        yield chunk


def _thought(text: str) -> Any:
    return _delta(content=None, reasoning_content=text)


def _answer(text: str) -> Any:
    return _delta(content=text, reasoning_content=None)


def _delta(*, content: str | None, reasoning_content: str | None) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(
                    content=content,
                    reasoning_content=reasoning_content,
                    tool_calls=None,
                ),
                finish_reason=None,
            )
        ],
        usage=None,
    )


def _finish() -> Any:
    chunk = _delta(content=None, reasoning_content=None)
    chunk.choices[0].finish_reason = "stop"
    return chunk


async def _deltas_seen_before_the_answer(*, fallback_on_reasoning_only: bool) -> int:
    """How many content frames reached the client while the model only thought.

    The upstream generator is pulled one chunk at a time by the same task that
    forwards the result, so by the time it is asked for the answer, everything
    the client was going to see from the thinking is already in ``collected``.
    That ordering is what makes the count exact rather than a race.

    The pause is real and load-bearing: the holdback window is 0.75s wide, so
    without it a committing stream would still be inside its window and the two
    settings would look identical.
    """
    provider = _ReasoningProvider(fallback_on_reasoning_only=fallback_on_reasoning_only)
    collected: list[str] = []
    before_answer: list[int] = []

    async def upstream():
        yield _thought("hmm")
        await asyncio.sleep(0.8)
        yield _thought("still hmm")
        before_answer.append(
            sum(1 for event in collected if "content_block_delta" in event)
        )
        yield _answer("done")
        yield _finish()

    create = AsyncMock(return_value=upstream())
    with patch.object(provider._client.chat.completions, "create", create):
        async for event in provider.stream_response(
            make_messages_request(), input_tokens=7
        ):
            # Appended one at a time on purpose. An async comprehension would
            # build the list only after the stream ended, so the count taken
            # mid-stream above would read an empty list and both settings
            # would score 0 -- a test that passes either way.
            collected.append(event)  # noqa: PERF401

    assert any("content_block_delta" in event for event in collected), (
        "the answer itself must always reach the client"
    )
    return before_answer[0]


@pytest.mark.asyncio
async def test_reasoning_alone_reaches_the_client_when_the_setting_is_off() -> None:
    """The old behaviour, kept for anyone who wants to watch a model think."""
    assert await _deltas_seen_before_the_answer(fallback_on_reasoning_only=False) > 0


@pytest.mark.asyncio
async def test_reasoning_alone_is_held_back_so_the_route_can_still_move() -> None:
    """The shipped default, and the whole point of the setting.

    Nothing the client has seen is nothing it would lose, so the executor's
    attempt stays uncommitted and the next model on the chain may still answer.
    If this ever flips, the fallback chain is silently unusable for every
    reasoning model again -- which 44 of 499 budget exhaustions already were.
    """
    assert await _deltas_seen_before_the_answer(fallback_on_reasoning_only=True) == 0


def test_holding_reasoning_back_is_the_shipped_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A constructor default is a separate contract from the parameter.

    The env var is cleared explicitly: ``Settings`` reads process env, and a
    value leaked from the developer's shell would make this assert the shell
    rather than the shipped default.
    """
    monkeypatch.delenv("FALLBACK_ON_REASONING_ONLY", raising=False)
    assert Settings().fallback_on_reasoning_only is True
    assert ProviderConfig(api_key="k", base_url="u").fallback_on_reasoning_only is True


@pytest.mark.asyncio
async def test_the_provider_signals_that_it_is_holding_reasoning() -> None:
    """The hop that would otherwise do nothing at all.

    Routing cannot tell a model thinking silently from a model that is simply
    silent unless the provider says so, and the two get very different
    deadlines. If this heartbeat stops being emitted, a thinking model is
    judged by the first-token share -- on an eleven-model chain, 54 seconds.
    """
    provider = _ReasoningProvider(fallback_on_reasoning_only=True)
    create = AsyncMock(return_value=_stream([_thought("hmm"), _answer("x"), _finish()]))
    with patch.object(provider._client.chat.completions, "create", create):
        events = [
            event
            async for event in provider.stream_response(
                make_messages_request(), input_tokens=7
            )
        ]

    assert REASONING_HEARTBEAT in events


@pytest.mark.asyncio
async def test_no_heartbeat_when_reasoning_is_allowed_to_commit() -> None:
    """With the setting off nothing is held, so there is nothing to signal."""
    provider = _ReasoningProvider(fallback_on_reasoning_only=False)
    create = AsyncMock(return_value=_stream([_thought("hmm"), _answer("x"), _finish()]))
    with patch.object(provider._client.chat.completions, "create", create):
        events = [
            event
            async for event in provider.stream_response(
                make_messages_request(), input_tokens=7
            )
        ]

    assert REASONING_HEARTBEAT not in events
