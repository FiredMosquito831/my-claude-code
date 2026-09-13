"""The two scans the attempt clocks are built on, against real emitted frames.

Per-attempt TTFT is measured in one place -- the executor's chunk loop -- and is
therefore only as provider-agnostic as the frames that reach it. Every family
here (Anthropic Messages, OpenAI chat, the shared Responses adapter and so
chatgpt-oauth, Gemini, Cloudflare, OpenRouter) normalises its stream through
:class:`AnthropicStreamLedger` before the executor sees a chunk, so what this
pins is that the ledger's own output answers the two questions correctly: a
thinking frame is reasoning and is *not* the answer starting, a text frame is,
and an envelope frame is neither.
"""

from my_claude_code.core.anthropic.stream_contracts import (
    sse_carries_content,
    sse_carries_reasoning,
)
from my_claude_code.core.anthropic.streaming.ledger import AnthropicStreamLedger


def _ledger() -> AnthropicStreamLedger:
    return AnthropicStreamLedger("msg_test", "some/model", input_tokens=3)


def test_a_ledger_thinking_delta_is_reasoning_not_answer_content() -> None:
    frame = _ledger().emit_thinking_delta("weighing it up")
    assert sse_carries_reasoning(frame)
    # It *is* a content_block_delta, which is why the loop asks the reasoning
    # question first: otherwise a reasoning model's TTFT would be the moment it
    # started thinking, and every such model would read as instant.
    assert sse_carries_content(frame)


def test_a_ledger_text_delta_is_answer_content_and_not_reasoning() -> None:
    ledger = _ledger()
    ledger.emit_thinking_delta("think")
    frame = ledger.emit_text_delta("hello")
    assert sse_carries_content(frame)
    assert not sse_carries_reasoning(frame)


def test_a_ledger_tool_argument_delta_counts_as_answer_content() -> None:
    """A streamed tool call is the answer, even though no prose arrives."""
    ledger = _ledger()
    ledger.emit_text_delta("x")
    ledger.start_tool_block(0, "toolu_1", "read_file")
    frame = ledger.emit_tool_delta(0, '{"path":')
    assert sse_carries_content(frame)
    assert not sse_carries_reasoning(frame)


def test_ledger_scaffolding_is_neither() -> None:
    ledger = _ledger()
    opening = ledger.message_start()
    assert not sse_carries_content(opening)
    assert not sse_carries_reasoning(opening)
