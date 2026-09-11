"""Replay the real Zen Responses stream through MCC's translation.

The other half of the reference capture. ``opencode_reference_request.json``
pins what MCC *sends*; this pins what MCC *makes of what comes back* --
every SSE frame OpenCode Zen sent the real ``opencode-ai@1.18.30`` CLI for
``muse-spark-1.3-contributor-free``, replayed through the converter the
OpenCode provider now uses, asserting that the two visible characters the
model produced reach the client as Anthropic text.

Why this exists as well as the three live calls: the live calls prove the
endpoint answers, and they were capped at a tiny output allowance which the
model spent thinking. These are the real model's real frames, so the
translation is proven on the actual wire shape rather than on a hand-written
one -- and it costs no quota, so it can run in CI forever.

The fixture is honest about its own edges: the recorder capped the body, so
the terminal ``response.completed`` frame is absent and nothing here asserts a
usage block. The model's encrypted reasoning and the client's own system
prompt and tool schemas are replaced by their lengths.
"""

import json
from pathlib import Path
from typing import Any

from my_claude_code.core.anthropic.streaming import AnthropicStreamLedger
from my_claude_code.providers.openai_responses import ResponsesStreamConverter

FIXTURE = json.loads(
    Path(__file__)
    .with_name("opencode_reference_responses_stream.json")
    .read_text(encoding="utf-8")
)
FRAMES: list[dict[str, Any]] = FIXTURE["frames"]


def _replay(*, output_reasoning: bool = True) -> list[str]:
    ledger = AnthropicStreamLedger(
        "msg_replay", "muse-spark-1.3-contributor-free", input_tokens=0
    )
    converter = ResponsesStreamConverter(ledger, output_reasoning=output_reasoning)
    events = [ledger.message_start()]
    for frame in FRAMES:
        events.extend(converter.feed(frame))
    events.extend(converter.finish())
    return events


def _text_of(events: list[str]) -> str:
    text = ""
    for event in events:
        for line in event.splitlines():
            if not line.startswith("data: "):
                continue
            payload = json.loads(line[len("data: ") :])
            delta = payload.get("delta") or {}
            if delta.get("type") == "text_delta":
                text += delta.get("text", "")
    return text


def test_the_fixture_is_the_real_clients_real_answer() -> None:
    assert FIXTURE["_model"] == "muse-spark-1.3-contributor-free"
    assert FIXTURE["_truncated"] and FIXTURE["_elided"]
    assert any(frame.get("type") == "response.created" for frame in FRAMES)
    assert any(frame.get("type") == "response.output_text.delta" for frame in FRAMES), (
        "a reference answer with no visible output would prove nothing"
    )


def test_the_models_visible_answer_reaches_the_client_as_anthropic_text() -> None:
    """The user's actual complaint, answered: this model now produces an answer."""

    assert _text_of(_replay()) == "ok"


def test_the_replayed_stream_is_a_well_formed_anthropic_message() -> None:
    events = _replay()
    kinds = [
        json.loads(line[len("data: ") :]).get("type")
        for event in events
        for line in event.splitlines()
        if line.startswith("data: ")
    ]
    assert kinds[0] == "message_start"
    assert kinds[-1] == "message_stop"
    assert "content_block_start" in kinds
    assert "content_block_stop" in kinds
    assert kinds.count("message_delta") == 1


def test_an_encrypted_reasoning_item_opens_no_empty_thinking_block() -> None:
    """A reasoning item carrying only ciphertext has nothing anybody may read.

    Zen sends exactly that for this model, so the honest rendering is no
    thinking block at all rather than an empty one claiming it thought aloud.
    """

    reasoning_items = [
        frame
        for frame in FRAMES
        if frame.get("type") == "response.output_item.done"
        and (frame.get("item") or {}).get("type") == "reasoning"
    ]
    assert reasoning_items, "the fixture must contain the reasoning item it describes"
    kinds = [
        json.loads(line[len("data: ") :]).get("delta", {}).get("type")
        for event in _replay()
        for line in event.splitlines()
        if line.startswith("data: ")
    ]
    assert "thinking_delta" not in kinds
