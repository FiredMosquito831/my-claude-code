"""The vision adapter's second mode: describe the picture, keep the model.

Every test here pins one rung of the contract in section 5A of the spec: the
default is byte-for-byte what 6.50.1 did, describe mode replaces the picture
rather than the request, a description is paid for once, and no failure of any
of it can cost the client an answer.
"""

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi.responses import StreamingResponse

from my_claude_code.api.handlers import MessagesHandler
from my_claude_code.application.routing import ModelRouter, RouteDiversion
from my_claude_code.application.vision_describe import (
    DescribeResult,
    VisionDescribeAdapter,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.reasoning import ReasoningPolicy
from my_claude_code.core.request_log import store_from_settings

# 1x1 PNG, base64. Small enough to inline, real enough to hash.
PIXEL = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
DESCRIPTION = "A terminal showing: ModuleNotFoundError: no module named 'ruff'."


def _sse(*frames: tuple[str, dict[str, Any]]) -> list[str]:
    return [f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in frames]


def _text_stream(text: str, model: str) -> list[str]:
    return _sse(
        ("message_start", {"type": "message_start", "message": {"model": model}}),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_stop", {"type": "message_stop"}),
    )


class RecordingProvider:
    """A provider that remembers every body it was handed."""

    def __init__(self, model: str, text: str, *, fail: bool = False) -> None:
        self.model = model
        self.text = text
        self.fail = fail
        self.requests: list[MessagesRequest] = []
        self.input_tokens: list[int] = []

    def throttle_remaining(self, model: str | None = None) -> float:
        return 0.0

    @property
    def credential_label(self) -> str | None:
        return None

    def preflight_stream(
        self, request: MessagesRequest, *, reasoning: ReasoningPolicy
    ) -> None:
        return None

    async def cleanup(self) -> None:
        return None

    async def list_model_ids(self) -> frozenset[str]:
        return frozenset({self.model})

    async def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        self.requests.append(request)
        self.input_tokens.append(input_tokens)
        if self.fail:
            raise RuntimeError("the vision model is down")
        for event in _text_stream(self.text, self.model):
            yield event

    def body_text(self, index: int = 0) -> str:
        return json.dumps(self.requests[index].model_dump(), default=str)


def _settings(mode: str) -> Settings:
    settings = Settings()
    settings.model = "nvidia_nim/blind"
    settings.model_sonnet = "nvidia_nim/blind"
    settings.model_fable = None
    settings.model_opus = None
    settings.model_haiku = None
    settings.model_fallbacks = None
    settings.model_sonnet_fallbacks = None
    settings.model_vision = "groq/eyes"
    settings.model_vision_fallbacks = None
    settings.vision_adapter_mode = mode
    return settings


def _router(settings: Settings) -> ModelRouter:
    return ModelRouter(
        settings,
        vision_lookup=lambda _provider, model: {"blind": False, "eyes": True}.get(
            model
        ),
    )


def _handler(
    settings: Settings, providers: dict[str, Any]
) -> tuple[MessagesHandler, dict[str, Any]]:
    handler = MessagesHandler(
        settings,
        provider_resolver=lambda provider_id: providers[provider_id],
        model_router=_router(settings),
    )
    return handler, providers


def _screenshot(width: int = 1920, height: int = 1080) -> str:
    """A real picture, at a size a real screenshot has.

    Since 6.53.0 the estimator bills an image on its actual pixel dimensions,
    so a 1x1 PNG genuinely costs about one token. Any test comparing "the
    picture" against "words about the picture" therefore has to use a picture,
    or it is comparing a description against a single pixel.
    """
    import base64
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), "red").save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _image_request(
    *, stream: bool = False, tool_result: bool = False, data: str = PIXEL
) -> MessagesRequest:
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": data},
    }
    if tool_result:
        content: list[dict[str, Any]] = [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": [{"type": "text", "text": "here it is"}, image],
            }
        ]
        messages = [
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "take_screenshot",
                        "input": {},
                    }
                ],
            },
            {"role": "user", "content": content},
        ]
    else:
        messages = [{"role": "user", "content": [image, {"type": "text", "text": "?"}]}]
    return MessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 64,
            "stream": stream,
            "messages": messages,
        }
    )


async def _drain(response: object) -> str:
    if isinstance(response, StreamingResponse):
        parts = [
            chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk)
            async for chunk in response.body_iterator
        ]
        return "".join(parts)
    return json.dumps(response, default=str)


def _row(settings: Settings, request_id: str) -> dict[str, Any]:
    store = store_from_settings(settings)
    assert store is not None
    # The writer is a background thread; the row lands a beat after the
    # response does. Polling beats sleeping a fixed amount and beats closing
    # the store, which the caching tests still need open.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        row = store.get_request(request_id)
        if row is not None:
            return row
        time.sleep(0.02)
    raise AssertionError(f"{request_id} was never written to the request log")


# ------------------------------------------------------------------ route ---


@pytest.mark.asyncio
async def test_route_mode_is_exactly_what_6_50_1_did() -> None:
    """The default must not change what happens on upgrade.

    The whole request goes to the vision model, base64 and all, and nothing
    describes anything. This is the pin: if describe mode ever leaks into the
    default, this is what fails.
    """
    settings = _settings("route")
    blind = RecordingProvider("blind", "answer")
    eyes = RecordingProvider("eyes", "answer")
    handler, _ = _handler(settings, {"nvidia_nim": blind, "groq": eyes})

    await _drain(await handler.create(_image_request(), request_id="req_route"))

    assert blind.requests == []
    assert len(eyes.requests) == 1
    assert PIXEL in eyes.body_text()
    row = _row(settings, "req_route")
    assert row["route_diversion"] == "vision"
    assert row["image_delivery"] == "image"


# --------------------------------------------------------------- describe ---


@pytest.mark.asyncio
async def test_describe_mode_sends_words_to_the_model_the_route_picked() -> None:
    settings = _settings("describe")
    blind = RecordingProvider("blind", "answer")
    eyes = RecordingProvider("eyes", DESCRIPTION)
    handler, _ = _handler(settings, {"nvidia_nim": blind, "groq": eyes})

    await _drain(await handler.create(_image_request(), request_id="req_describe"))

    # One describe call per image, on the vision chain, carrying the picture.
    assert len(eyes.requests) == 1
    assert PIXEL in eyes.body_text()
    # The route's own model answered, and it never saw a byte of base64.
    assert len(blind.requests) == 1
    body = blind.body_text()
    assert PIXEL not in body
    assert DESCRIPTION in body
    assert "described by groq/eyes" in body

    row = _row(settings, "req_describe")
    assert row["route_diversion"] == "vision_described"
    assert row["image_delivery"] == "described"
    assert row["resolved_model"] == "blind"
    # The number of pictures that arrived is a fact about the request, not
    # about how they were delivered.
    assert row["input_image_count"] == 1


@pytest.mark.asyncio
async def test_the_describe_call_gets_its_own_row_against_the_parent() -> None:
    settings = _settings("describe")
    blind = RecordingProvider("blind", "answer")
    eyes = RecordingProvider("eyes", DESCRIPTION)
    handler, _ = _handler(settings, {"nvidia_nim": blind, "groq": eyes})

    await _drain(await handler.create(_image_request(), request_id="req_rows"))

    row = _row(settings, "req_rows")
    attempts = row["route_attempts"]
    describes = [a for a in attempts if (a["params"] or {}).get("kind") == "describe"]
    assert len(describes) == 1
    assert describes[0]["model_ref"] == "groq/eyes"
    assert describes[0]["outcome"] == "succeeded"
    assert describes[0]["params"]["cached"] is False
    assert describes[0]["params"]["image_sha"]
    # And it does not pretend to be a rung of the route's own chain.
    assert describes[0]["attempt"] >= 1000
    route_rows = [a for a in attempts if (a["params"] or {}).get("kind") != "describe"]
    assert [a["model_ref"] for a in route_rows] == ["nvidia_nim/blind"]


@pytest.mark.asyncio
async def test_a_tool_returned_image_names_the_tool_in_both_directions() -> None:
    """The prompt says where the picture came from; so does the replacement."""
    settings = _settings("describe")
    blind = RecordingProvider("blind", "answer")
    eyes = RecordingProvider("eyes", DESCRIPTION)
    handler, _ = _handler(settings, {"nvidia_nim": blind, "groq": eyes})

    await _drain(
        await handler.create(_image_request(tool_result=True), request_id="req_tool")
    )

    assert "take_screenshot" in eyes.body_text()
    body = blind.body_text()
    assert "returned by the 'take_screenshot' tool, described by groq/eyes" in body
    assert PIXEL not in body


@pytest.mark.asyncio
async def test_the_same_picture_is_described_once_however_often_it_arrives() -> None:
    """The cache key is the picture, so a re-sent screenshot is free."""
    settings = _settings("describe")
    blind = RecordingProvider("blind", "answer")
    eyes = RecordingProvider("eyes", DESCRIPTION)
    handler, _ = _handler(settings, {"nvidia_nim": blind, "groq": eyes})

    await _drain(await handler.create(_image_request(), request_id="req_first"))
    await _drain(await handler.create(_image_request(), request_id="req_second"))

    assert len(eyes.requests) == 1, "the second request paid for a describe call"
    assert len(blind.requests) == 2
    assert DESCRIPTION in blind.body_text(1)
    second = _row(settings, "req_second")
    assert second["route_diversion"] == "vision_described"
    # A cached description writes no attempt row: nothing went upstream.
    assert not [
        a
        for a in second["route_attempts"]
        if (a["params"] or {}).get("kind") == "describe"
    ]
    # The description is on the picture, where the request detail reads it.
    assert second["input_images"][0]["description"] == DESCRIPTION
    assert second["input_images"][0]["described_by"] == "groq/eyes"


@pytest.mark.asyncio
async def test_clearing_the_descriptions_makes_the_next_request_pay_again() -> None:
    settings = _settings("describe")
    blind = RecordingProvider("blind", "answer")
    eyes = RecordingProvider("eyes", DESCRIPTION)
    handler, _ = _handler(settings, {"nvidia_nim": blind, "groq": eyes})

    await _drain(await handler.create(_image_request(), request_id="req_c1"))
    store = store_from_settings(settings)
    assert store is not None
    assert store.clear_image_descriptions() == 1
    await _drain(await handler.create(_image_request(), request_id="req_c2"))

    assert len(eyes.requests) == 2


@pytest.mark.asyncio
async def test_the_estimate_measures_the_text_the_model_receives() -> None:
    """Section 5A.5: a described image costs what its description costs.

    Substitution happens before the plan is resolved, so the count the
    executor hands the provider is a count of words, not of base64.
    """
    settings = _settings("describe")
    blind = RecordingProvider("blind", "answer")
    eyes = RecordingProvider("eyes", DESCRIPTION)
    handler, _ = _handler(settings, {"nvidia_nim": blind, "groq": eyes})

    screenshot = _screenshot()
    await _drain(
        await handler.create(_image_request(data=screenshot), request_id="req_tokens")
    )

    route_settings = _settings("route")
    route_blind = RecordingProvider("blind", "answer")
    route_eyes = RecordingProvider("eyes", "answer")
    route_handler, _ = _handler(
        route_settings, {"nvidia_nim": route_blind, "groq": route_eyes}
    )
    await _drain(
        await route_handler.create(
            _image_request(data=screenshot), request_id="req_tok2"
        )
    )

    assert blind.input_tokens[0] < route_eyes.input_tokens[0]


@pytest.mark.asyncio
async def test_a_sighted_primary_gets_the_picture_and_no_describe_call() -> None:
    """Q14: describe mode is for a blind primary, and only for one."""
    settings = _settings("describe")
    settings.model = "groq/eyes"
    settings.model_sonnet = "groq/eyes"
    seer = RecordingProvider("eyes", "answer")
    handler, _ = _handler(settings, {"groq": seer})

    await _drain(await handler.create(_image_request(), request_id="req_sighted"))

    assert len(seer.requests) == 1
    assert PIXEL in seer.body_text()
    row = _row(settings, "req_sighted")
    assert row["route_diversion"] is None
    assert row["image_delivery"] == "image"


# ---------------------------------------------------------------- failure ---


@pytest.mark.asyncio
async def test_a_failed_describe_falls_back_to_route_mode() -> None:
    """Section 5A.7, rung one: divert the whole request, as route mode would.

    The route keeps a fallback of unpublished capability behind the adapter,
    so the diversion has somewhere real to go and the client gets an answer
    from it -- with the picture intact, which is what route mode is.
    """
    settings = _settings("describe")
    settings.model_sonnet_fallbacks = "nvidia_nim/backup"
    blind = RecordingProvider("blind", "answer")
    eyes = RecordingProvider("eyes", DESCRIPTION, fail=True)
    handler, _ = _handler(settings, {"nvidia_nim": blind, "groq": eyes})

    await _drain(await handler.create(_image_request(), request_id="req_fail"))

    # The describe call was tried and failed; the request was then diverted
    # whole, exactly as route mode does, and the route's own fallback served
    # it -- with the picture, because nothing described it.
    assert len(eyes.requests) == 2, "one describe try, then the diversion"
    assert len(blind.requests) == 1
    assert PIXEL in blind.body_text()
    row = _row(settings, "req_fail")
    assert row["route_diversion"] == "vision"
    assert row["status"] == "success"


@pytest.mark.asyncio
async def test_a_dead_vision_chain_ends_in_a_sentence_not_an_error() -> None:
    """Section 5A.7, rung two, reached the way it actually happens.

    Route mode diverts to the models that just failed to describe. Walking
    into the same wall twice is not a fallback, so the ladder skips straight
    to the placeholder and the blind model answers without the picture. The
    client is never the one who pays for the vision model being down.
    """
    settings = _settings("describe")
    blind = RecordingProvider("blind", "answer")
    eyes = RecordingProvider("eyes", DESCRIPTION, fail=True)
    handler, _ = _handler(settings, {"nvidia_nim": blind, "groq": eyes})

    await _drain(await handler.create(_image_request(), request_id="req_dead"))

    assert len(eyes.requests) == 1, "the dead chain is not walked a second time"
    assert len(blind.requests) == 1
    body = blind.body_text()
    assert PIXEL not in body
    assert "could not be described and this model cannot read it" in body
    row = _row(settings, "req_dead")
    assert row["status"] == "success"
    assert row["route_diversion"] == "vision_unavailable"
    assert row["image_delivery"] == "stripped"


def test_the_last_rung_is_a_placeholder_not_a_failed_request() -> None:
    """Section 5A.7, rung two: nowhere to divert to, so say what is missing."""
    settings = _settings("describe")
    settings.model_vision = None
    handler = MessagesHandler(
        settings,
        provider_resolver=lambda _provider: RecordingProvider("blind", "answer"),
        model_router=_router(settings),
    )
    request = _image_request()
    plan = handler._model_router.resolve_messages_plan(request)
    assert plan.diversion is RouteDiversion.VISION_UNAVAILABLE

    updated = handler._apply_describe_outcome(
        plan, request, DescribeResult(applied=False, failed=True), None
    )

    assert updated.diversion is RouteDiversion.VISION_UNAVAILABLE
    assert str(updated.primary.image_delivery) == "stripped"
    body = json.dumps(request.model_dump(), default=str)
    assert PIXEL not in body
    assert "could not be described and this model cannot read it" in body


@pytest.mark.asyncio
async def test_an_empty_description_is_a_failure_not_an_empty_answer() -> None:
    settings = _settings("describe")
    blind = RecordingProvider("blind", "answer")
    eyes = RecordingProvider("eyes", "   ")
    handler, _ = _handler(settings, {"nvidia_nim": blind, "groq": eyes})

    adapter = VisionDescribeAdapter(
        router=_router(settings),
        executor=handler._provider_executor,
        store=store_from_settings(settings),
    )
    result = await adapter.apply(_image_request(), request_id="req_empty")

    assert result.applied is False
    assert result.failed is True


@pytest.mark.asyncio
async def test_describe_mode_declines_a_request_it_cannot_describe() -> None:
    """A document is pixels no image block can carry; route mode owns that."""
    settings = _settings("describe")
    handler, _ = _handler(
        settings,
        {
            "nvidia_nim": RecordingProvider("blind", "a"),
            "groq": RecordingProvider("eyes", "b"),
        },
    )
    adapter = VisionDescribeAdapter(
        router=_router(settings),
        executor=handler._provider_executor,
        store=store_from_settings(settings),
    )
    request = MessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 8,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "source": {
                                "type": "base64",
                                "media_type": "application/pdf",
                                "data": PIXEL,
                            },
                        }
                    ],
                }
            ],
        }
    )
    result = await adapter.apply(request, request_id="req_doc")

    assert result.applied is False
    assert result.failed is False


# ------------------------------------------- what the describe hop cost ---


class MeteredProvider(RecordingProvider):
    """A provider whose reply reports its own usage, as a real host does."""

    def __init__(self, model: str, text: str, *, tokens_in: int, tokens_out: int):
        super().__init__(model, text)
        self.tokens_in = tokens_in
        self.tokens_out = tokens_out

    async def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: ReasoningPolicy,
    ) -> AsyncIterator[str]:
        self.requests.append(request)
        self.input_tokens.append(input_tokens)
        for event in _text_stream(self.text, self.model):
            yield event
        for event in _sse(
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {
                        "input_tokens": self.tokens_in,
                        "output_tokens": self.tokens_out,
                    },
                },
            )
        ):
            yield event


@pytest.mark.asyncio
async def test_describe_usage_reaches_the_attempt_row() -> None:
    """The hole this PR closes: the aggregator returned it, nothing read it."""
    settings = _settings("describe")
    blind = RecordingProvider("blind", "answer")
    eyes = MeteredProvider("eyes", DESCRIPTION, tokens_in=1560, tokens_out=64)
    handler, _ = _handler(settings, {"nvidia_nim": blind, "groq": eyes})

    await _drain(await handler.create(_image_request(), request_id="req_usage"))

    row = _row(settings, "req_usage")
    describes = [
        a
        for a in row["route_attempts"]
        if (a["params"] or {}).get("kind") == "describe"
    ]
    assert len(describes) == 1
    assert describes[0]["tokens_in"] == 1560
    assert describes[0]["tokens_out"] == 64


@pytest.mark.asyncio
async def test_describe_usage_rolls_up_to_the_request_row() -> None:
    settings = _settings("describe")
    blind = RecordingProvider("blind", "answer")
    eyes = MeteredProvider("eyes", DESCRIPTION, tokens_in=1560, tokens_out=64)
    handler, _ = _handler(settings, {"nvidia_nim": blind, "groq": eyes})

    await _drain(await handler.create(_image_request(), request_id="req_rollup"))

    row = _row(settings, "req_rollup")
    assert row["adapter_tokens_in"] == 1560
    assert row["adapter_tokens_out"] == 64


@pytest.mark.asyncio
async def test_parent_tokens_in_is_unchanged_by_describe() -> None:
    """The regression guard: the adapter's cost is never folded into tokens_in.

    ``tokens_in`` measures the model that answered the client and has measured
    exactly that since the request log existed. Adding a describe hop into it
    would silently change what every historical chart means.
    """
    settings = _settings("describe")
    blind = RecordingProvider("blind", "answer")
    eyes = MeteredProvider("eyes", DESCRIPTION, tokens_in=1560, tokens_out=64)
    handler, _ = _handler(settings, {"nvidia_nim": blind, "groq": eyes})

    await _drain(await handler.create(_image_request(), request_id="req_apart"))

    row = _row(settings, "req_apart")
    assert row["adapter_tokens_in"] == 1560
    assert (row["tokens_in"] or 0) != 1560


@pytest.mark.asyncio
async def test_no_describe_leaves_adapter_tokens_null() -> None:
    """NULL, not 0: nothing was measured because nothing ran."""
    settings = _settings("route")
    blind = RecordingProvider("blind", "answer")
    eyes = RecordingProvider("eyes", DESCRIPTION)
    handler, _ = _handler(settings, {"nvidia_nim": blind, "groq": eyes})

    await _drain(await handler.create(_image_request(), request_id="req_null"))

    row = _row(settings, "req_null")
    assert row["adapter_tokens_in"] is None
    assert row["adapter_tokens_out"] is None


@pytest.mark.asyncio
async def test_a_described_image_costs_no_image_tokens_in_the_estimate() -> None:
    """The estimate describes what was sent, not what arrived.

    In describe mode the pictures are sentences by the time the request goes
    out, so the honest split is "0 image tokens, and their words are already
    inside est_tokens_in". Counting the client's original pictures instead
    would make the image share of the estimate describe a request nobody sent.
    """
    settings = _settings("describe")
    blind = RecordingProvider("blind", "answer")
    eyes = RecordingProvider("eyes", DESCRIPTION)
    handler, _ = _handler(settings, {"nvidia_nim": blind, "groq": eyes})

    await _drain(
        await handler.create(
            _image_request(data=_screenshot()), request_id="req_est_described"
        )
    )

    row = _row(settings, "req_est_described")
    assert row["est_image_tokens"] == 0
    assert row["est_tokens_in"] is not None


@pytest.mark.asyncio
async def test_an_attached_image_records_its_estimated_share() -> None:
    settings = _settings("route")
    blind = RecordingProvider("blind", "answer")
    eyes = RecordingProvider("eyes", "answer")
    handler, _ = _handler(settings, {"nvidia_nim": blind, "groq": eyes})

    await _drain(
        await handler.create(
            _image_request(data=_screenshot()), request_id="req_est_attached"
        )
    )

    row = _row(settings, "req_est_attached")
    # 1920x1080 on the Anthropic fallback: resized to 1456x819 and billed at
    # 52 x 30 patches, which is the number in Anthropic's own worked example.
    assert row["est_image_tokens"] == 1560
