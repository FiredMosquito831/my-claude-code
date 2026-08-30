"""Tests for per-request analytics capture at the handler/stream layer."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from my_claude_code.api.request_capture import (
    RequestCapture,
    _image_pixels,
    build_capture,
    extract_input_text,
    extract_request_params,
)
from my_claude_code.api.response_streams import ManagedStreamingResponse
from my_claude_code.application.execution import RouteAttemptRecord
from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import request_image_inputs
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.async_iterators import AsyncCloseable
from my_claude_code.core.credential_attribution import record_credential
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.request_log import (
    _RECOVERY_TRACE,
    RequestLogStore,
    RequestRecord,
    get_request_log_store,
    record_recovery_event,
)
from my_claude_code.core.upstream_ladder import (
    _LADDER,
    current_ladder,
    record_credential_decision,
    record_upstream_try,
    record_upstream_wait,
)
from my_claude_code.core.wire_capture import _WIRE_TRACE, record_wire_request


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db")
    yield store
    store.close()


def _events(*frames: tuple[str, dict]) -> list[str]:
    return [f"event: {event}\ndata: {json.dumps(data)}\n\n" for event, data in frames]


def _make_capture(store: RequestLogStore | None, **overrides) -> RequestCapture:
    defaults: dict[str, Any] = {
        "request_id": "req_test",
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "stream": True,
        "requested_model": "claude-sonnet-4-5",
        "input_text": "hello",
        "params": {"max_tokens": 100},
    }
    defaults.update(overrides)
    return RequestCapture(store, **defaults)


async def _collect(body: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in body]


def _final_row(store: RequestLogStore) -> dict:
    rows, total = store.list_requests()
    assert total == 1
    return rows[0]


@pytest.mark.asyncio
async def test_streaming_success_records_usage_and_text(store: RequestLogStore) -> None:
    async def body() -> AsyncIterator[str]:
        for chunk in _events(
            (
                "message_start",
                {"type": "message_start", "message": {"usage": {"input_tokens": 42}}},
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "Hello "},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "world"},
                },
            ),
            ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 7}}),
            ("message_stop", {"type": "message_stop"}),
        ):
            yield chunk

    capture = _make_capture(store)
    chunks = await _collect(capture.wrap(body()))
    store.close()

    assert len(chunks) == 5
    row = _final_row(store)
    assert row["status"] == "success"
    assert row["tokens_in"] == 42
    assert row["tokens_out"] == 7
    assert row["output_text"] == "Hello world"
    assert row["input_text"] == "hello"
    assert row["ttft_ms"] is not None
    assert row["duration_ms"] is not None
    assert row["stream"] is True


@pytest.mark.asyncio
async def test_mid_stream_failure_records_error(store: RequestLogStore) -> None:
    async def body() -> AsyncIterator[str]:
        yield _events(
            (
                "message_start",
                {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
            )
        )[0]
        raise ExecutionFailure(
            kind=FailureKind.RATE_LIMIT,
            status_code=429,
            message="slow down",
            retryable=True,
        )

    capture = _make_capture(store)
    with pytest.raises(ExecutionFailure):
        await _collect(capture.wrap(body()))
    store.close()

    row = _final_row(store)
    assert row["status"] == "error"
    assert row["error_kind"] == "rate_limit"
    assert row["error_message"] == "slow down"
    assert row["tokens_in"] == 3


@pytest.mark.asyncio
async def test_sse_error_event_records_error(store: RequestLogStore) -> None:
    async def body() -> AsyncIterator[str]:
        yield _events(
            (
                "error",
                {
                    "type": "error",
                    "error": {"type": "overloaded_error", "message": "busy"},
                },
            )
        )[0]

    capture = _make_capture(store)
    await _collect(capture.wrap(body()))
    store.close()

    row = _final_row(store)
    assert row["status"] == "error"
    assert row["error_kind"] == "overloaded_error"


@pytest.mark.asyncio
async def test_client_disconnect_records_cancelled(store: RequestLogStore) -> None:
    closed = asyncio.Event()

    async def body() -> AsyncIterator[str]:
        try:
            yield _events(("message_start", {"type": "message_start", "message": {}}))[
                0
            ]
            await asyncio.sleep(60)
            yield "never"
        finally:
            closed.set()

    capture = _make_capture(store)
    stream = capture.wrap(body())
    await anext(stream)
    assert isinstance(stream, AsyncCloseable)
    await stream.aclose()
    store.close()

    assert closed.is_set()
    row = _final_row(store)
    assert row["status"] == "cancelled"
    assert row["ttft_ms"] is not None


@pytest.mark.asyncio
async def test_task_cancellation_records_cancelled(store: RequestLogStore) -> None:
    async def body() -> AsyncIterator[str]:
        yield _events(("message_start", {"type": "message_start", "message": {}}))[0]
        await asyncio.sleep(60)
        yield "never"

    capture = _make_capture(store)

    async def consume() -> None:
        async for _ in capture.wrap(body()):
            pass

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store.close()

    row = _final_row(store)
    assert row["status"] == "cancelled"


@pytest.mark.asyncio
async def test_pre_start_error_via_finish_error(store: RequestLogStore) -> None:
    capture = _make_capture(store)
    capture.finish_error(
        ExecutionFailure(
            kind=FailureKind.AUTHENTICATION,
            status_code=401,
            message="bad key",
            retryable=False,
        )
    )
    store.close()

    row = _final_row(store)
    assert row["status"] == "error"
    assert row["error_kind"] == "authentication"


@pytest.mark.asyncio
async def test_finish_is_single_shot(store: RequestLogStore) -> None:
    async def body() -> AsyncIterator[str]:
        yield _events(("message_stop", {"type": "message_stop"}))[0]

    capture = _make_capture(store)
    await _collect(capture.wrap(body()))
    capture.finish_error(RuntimeError("late error"))
    store.close()

    row = _final_row(store)
    assert row["status"] == "success"
    assert row["error_kind"] is None


@pytest.mark.asyncio
async def test_privacy_mode_stores_hashes_not_bodies(store: RequestLogStore) -> None:
    async def body() -> AsyncIterator[str]:
        yield _events(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "secret out"},
                },
            )
        )[0]

    capture = _make_capture(store, input_text="secret in", capture_bodies=False)
    await _collect(capture.wrap(body()))
    store.close()

    row = _final_row(store)
    assert row["input_text"] is None
    assert row["output_text"] is None
    assert row["input_sha256"] is not None
    assert row["output_sha256"] is not None
    assert row["input_chars"] == len("secret in")
    assert row["output_chars"] == len("secret out")


@pytest.mark.asyncio
async def test_disabled_capture_passes_stream_through() -> None:
    async def body() -> AsyncIterator[str]:
        yield "chunk"

    capture = _make_capture(None)
    assert capture.enabled is False
    assert capture.wrap(body()) is not None
    chunks = await _collect(capture.wrap(body()))
    assert chunks == ["chunk"]


def test_build_capture_from_messages_request(store: RequestLogStore) -> None:
    settings = Settings()
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=100,
        temperature=0.5,
        stream=True,
        system="be nice",
        messages=[Message(role="user", content="hi there")],
    )
    capture = build_capture(
        settings,
        request,
        request_id="req_x",
        endpoint="/v1/messages",
        protocol="anthropic",
    )
    assert capture.enabled is True
    assert extract_input_text(request) == "be nice\nhi there"
    params = extract_request_params(request)
    assert params["max_tokens"] == 100
    assert params["temperature"] == 0.5
    capture.finish_success("done")
    store_from = get_request_log_store()
    assert store_from is not None
    store_from.close()
    rows, total = store_from.list_requests()
    assert total == 1
    assert rows[0]["requested_model"] == "nvidia_nim/test-model"
    assert rows[0]["output_text"] == "done"


def test_build_capture_disabled_by_settings(monkeypatch) -> None:
    monkeypatch.setenv("REQUEST_LOG_ENABLED", "false")
    settings = Settings()
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        messages=[Message(role="user", content="hi")],
    )
    capture = build_capture(
        settings,
        request,
        request_id="req_x",
        endpoint="/v1/messages",
        protocol="anthropic",
    )
    assert capture.enabled is False
    capture.finish_success("done")


def test_records_exactly_once_for_non_stream_aggregate(store: RequestLogStore) -> None:
    # Simulates the non-streaming path: the same wrapped stream is consumed
    # to completion by the SSE aggregator.
    async def body() -> AsyncIterator[str]:
        yield _events(("message_stop", {"type": "message_stop"}))[0]

    capture = _make_capture(store, stream=False)
    asyncio.run(_collect(capture.wrap(body())))
    store.close()
    rows, total = store.list_requests()
    assert total == 1
    assert rows[0]["stream"] is False


def test_request_record_defaults() -> None:
    record = RequestRecord(id="r", endpoint="/v1/messages", protocol="anthropic")
    assert record.status == "success"
    assert record.ts_epoch > 0


class _FakeProvider:
    """Minimal provider stub compatible with ProviderExecutor."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def throttle_remaining(self) -> float:
        return 0.0

    @property
    def credential_label(self) -> str | None:
        return None

    def preflight_stream(self, request, *, reasoning) -> None:
        return None

    async def cleanup(self) -> None:
        return None

    async def list_model_ids(self) -> frozenset[str]:
        return frozenset({"test-model"})

    async def stream_response(
        self,
        request,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning,
    ) -> AsyncIterator[str]:
        for event in self.events:
            yield event


@pytest.mark.asyncio
async def test_messages_handler_end_to_end_capture() -> None:
    from my_claude_code.api.handlers import MessagesHandler

    events = _events(
        (
            "message_start",
            {"type": "message_start", "message": {"usage": {"input_tokens": 11}}},
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hi"},
            },
        ),
        ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 2}}),
        ("message_stop", {"type": "message_stop"}),
    )
    handler = MessagesHandler(
        Settings(),
        provider_resolver=lambda _: _FakeProvider(events),
    )
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=50,
        stream=True,
        messages=[Message(role="user", content="hello")],
    )
    response = await handler.create(request, request_id="req_e2e")
    assert isinstance(response, ManagedStreamingResponse)
    async for _ in response.body_iterator:
        pass
    await response.aclose()

    store = get_request_log_store()
    assert store is not None
    store.close()
    row = store.get_request("req_e2e")
    assert row is not None
    assert row["status"] == "success"
    assert row["provider"] == "nvidia_nim"
    assert row["resolved_model"] == "test-model"
    assert row["requested_model"] == "nvidia_nim/test-model"
    assert row["tokens_in"] == 11
    assert row["tokens_out"] == 2
    assert row["output_text"] == "hi"
    assert row["input_text"] == "hello"
    assert row["reasoning"] is not None
    assert row["params"]["max_tokens"] == 50


class _RotatingFakeProvider(_FakeProvider):
    """Stands in for RotatingProvider: picks a credential per request."""

    def __init__(self, events: list[str], *, index: int, label: str) -> None:
        super().__init__(events)
        self._index = index
        self._label = label

    @property
    def credential_label(self) -> str | None:
        return None

    async def stream_response(
        self,
        request,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning,
    ) -> AsyncIterator[str]:
        from my_claude_code.core.credential_attribution import record_credential

        record_credential(self._index, self._label)
        for event in self.events:
            yield event


@pytest.mark.asyncio
async def test_capture_records_the_credential_across_the_streaming_response() -> None:
    """The credential picked deep in the provider must reach the stored row.

    Exercises the whole chain: the capture installs the attribution slot, the
    provider writes into it while the response streams (potentially in another
    task), and finalize reads it back.
    """
    from my_claude_code.api.handlers import MessagesHandler

    events = _events(
        (
            "message_start",
            {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
        ),
        ("message_stop", {"type": "message_stop"}),
    )
    handler = MessagesHandler(
        Settings(),
        provider_resolver=lambda _: _RotatingFakeProvider(
            events, index=2, label="abcd…wxyz"
        ),
    )
    request = MessagesRequest(
        model="nvidia_nim/test-model",
        max_tokens=50,
        stream=True,
        messages=[Message(role="user", content="hello")],
    )
    response = await handler.create(request, request_id="req_key_attr")
    assert isinstance(response, ManagedStreamingResponse)
    async for _ in response.body_iterator:
        pass
    await response.aclose()

    store = get_request_log_store()
    assert store is not None
    store.close()
    row = store.get_request("req_key_attr")
    assert row is not None
    assert row["key_index"] == 2
    assert row["key_label"] == "abcd…wxyz"


class TestCacheUsageCapture:
    """Cache counters arrive on different events depending on the upstream."""

    @pytest.mark.asyncio
    async def test_reads_cache_counters_from_message_delta(self, store) -> None:
        """OpenAI-shaped providers only learn them from the final usage chunk.

        Verified against live OpenRouter: 4011 prompt tokens of which 3968 were
        served from cache. Reading only message_start recorded nothing at all.
        """

        frames = _events(
            (
                "message_start",
                {"type": "message_start", "message": {"usage": {"input_tokens": 4011}}},
            ),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "usage": {"output_tokens": 4, "cache_read_input_tokens": 3968},
                },
            ),
        )
        capture = _make_capture(store)

        async def body() -> AsyncIterator[str]:
            for frame in frames:
                yield frame

        await _collect(capture.wrap(body()))
        store.close()

        row = _final_row(store)
        assert row["tokens_in"] == 4011
        assert row["cache_read_tokens"] == 3968

    @pytest.mark.asyncio
    async def test_reads_cache_counters_from_message_start(self, store) -> None:
        """Anthropic-native upstreams report them up front instead."""

        frames = _events(
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "usage": {
                            "input_tokens": 100,
                            "cache_read_input_tokens": 900,
                            "cache_creation_input_tokens": 50,
                        }
                    },
                },
            ),
            ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 7}}),
        )
        capture = _make_capture(store)

        async def body() -> AsyncIterator[str]:
            for frame in frames:
                yield frame

        await _collect(capture.wrap(body()))
        store.close()

        row = _final_row(store)
        assert row["cache_read_tokens"] == 900
        assert row["cache_write_tokens"] == 50


def _vision_router() -> ModelRouter:
    """A router whose sonnet route is blind and whose vision adapter is not."""
    settings = Settings()
    settings.model = "nvidia_nim/blind"
    settings.model_fable = None
    settings.model_opus = None
    settings.model_haiku = None
    settings.model_sonnet = "nvidia_nim/blind"
    settings.model_sonnet_fallbacks = "groq/backup"
    settings.model_fallbacks = None
    settings.model_vision = "groq/eyes"
    return ModelRouter(
        settings,
        vision_lookup=lambda _provider, model: {"blind": False}.get(model),
    )


def _image_request() -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": "claude-sonnet-4-6",
            "max_tokens": 8,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": "aGk=",
                            },
                        }
                    ],
                }
            ],
        }
    )


def test_capture_records_a_vision_diversion(store: RequestLogStore) -> None:
    """The only trace that the adapter did anything at all."""
    plan = _vision_router().resolve_messages_plan(_image_request())
    capture = RequestCapture(
        store,
        request_id="req_vision",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="claude-sonnet-4-6",
        input_text="hi",
        params=None,
    )
    capture.set_plan(plan)
    capture.set_routing(plan.primary, 0)
    capture.finish_success("ok")
    store.close()

    row = store.get_request("req_vision")
    assert row is not None
    assert row["route_diversion"] == "vision"
    assert row["route_diverted_from"] == "nvidia_nim/blind"
    # groq/backup publishes no modality metadata, and silence is not a refusal,
    # so it survives the diversion as a fallback behind the adapter.
    assert row["route_chain"] == "groq/eyes,groq/backup"
    assert row["resolved_model"] == "eyes"


def test_capture_records_the_chain_even_when_the_primary_answers(
    store: RequestLogStore,
) -> None:
    """ "A chain existed and was not needed" is not the same as "no chain"."""
    plan = _vision_router().resolve_messages_plan(
        MessagesRequest(
            model="claude-sonnet-4-6",
            max_tokens=8,
            messages=[Message(role="user", content="hi")],
        )
    )
    capture = RequestCapture(
        store,
        request_id="req_plain",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="claude-sonnet-4-6",
        input_text="hi",
        params=None,
    )
    capture.set_plan(plan)
    capture.set_routing(plan.primary, 0)
    capture.finish_success("ok")
    store.close()

    row = store.get_request("req_plain")
    assert row is not None
    assert row["route_chain"] == "nvidia_nim/blind,groq/backup"
    assert row["route_diversion"] is None
    assert row["route_attempt"] == 0


def test_route_attempt_indexes_the_recorded_chain(store: RequestLogStore) -> None:
    """The dashboard highlights ``route_chain[route_attempt]``, so it must fit.

    These two columns are written by different calls -- ``set_plan`` once and
    ``set_routing`` per attempt -- and nothing else checks they stay in step.
    """
    plan = _vision_router().resolve_messages_plan(
        MessagesRequest(
            model="claude-sonnet-4-6",
            max_tokens=8,
            messages=[Message(role="user", content="hi")],
        )
    )
    capture = RequestCapture(
        store,
        request_id="req_chain",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="claude-sonnet-4-6",
        input_text="hi",
        params=None,
    )
    capture.set_plan(plan)
    for index, attempt in enumerate(plan.attempts):
        capture.set_routing(attempt, index)
    capture.finish_success("ok")
    store.close()

    row = store.get_request("req_chain")
    assert row is not None
    chain = row["route_chain"].split(",")
    assert row["route_attempt"] == len(plan.attempts) - 1
    assert chain[row["route_attempt"]] == "groq/backup"
    assert chain[row["route_attempt"]] == f"{row['provider']}/{row['resolved_model']}"


def _png_data() -> str:
    import base64
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (900, 700), (12, 90, 200)).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _image_request() -> MessagesRequest:
    """A screenshot delivered the way a tool delivers one."""
    return MessagesRequest.model_validate(
        {
            "model": "claude-sonnet-5",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": [
                                {
                                    "type": "image",
                                    "source": {
                                        "type": "base64",
                                        "media_type": "image/png",
                                        "data": _png_data(),
                                    },
                                }
                            ],
                        }
                    ],
                }
            ],
        }
    )


def test_capture_stores_a_tool_delivered_image(store: RequestLogStore) -> None:
    """The end the user sees: a screenshot a tool returned reaches the row."""
    capture = _make_capture(
        store,
        request_id="req_img",
        images=request_image_inputs(_image_request()),
        capture_images_pixels=512,
    )
    capture.finish_success("done")
    store.close()

    reader = RequestLogStore(store.db_path)
    try:
        row = reader.get_request("req_img")
        assert row is not None
        assert row["input_image_count"] == 1
        image = row["input_images"][0]
        assert image["width"] == 900
        assert image["media_type"] == "image/png"
        assert image["thumbnail_base64"]
    finally:
        reader.close()


def test_capture_records_the_image_without_pixels_when_disabled(
    store: RequestLogStore,
) -> None:
    capture = _make_capture(
        store,
        request_id="req_nopix",
        images=request_image_inputs(_image_request()),
        capture_images_pixels=0,
    )
    capture.finish_success("done")
    store.close()

    reader = RequestLogStore(store.db_path)
    try:
        row = reader.get_request("req_nopix")
        assert row is not None
        # The fact that an image arrived survives; only the pixels are dropped.
        assert row["input_image_count"] == 1
        assert row["input_images"][0]["thumbnail_base64"] is None
    finally:
        reader.close()


def test_the_thumbnail_setting_decides_whether_pixels_are_kept(monkeypatch) -> None:
    monkeypatch.setenv("REQUEST_LOG_IMAGE_MAX_PIXELS", "256")
    assert _image_pixels(Settings()) == 256
    monkeypatch.setenv("REQUEST_LOG_CAPTURE_IMAGES", "false")
    assert _image_pixels(Settings()) == 0


def test_local_optimization_row_names_no_provider(store: RequestLogStore) -> None:
    """A request no provider served must not be attributed to one.

    ``set_routing`` runs before the intercepts, so without this the row claims
    the provider the request would have gone to. In a production log that put
    3,246 phantom requests onto six real providers' per-provider averages.
    """
    plan = _vision_router().resolve_messages_plan(
        MessagesRequest(
            model="claude-sonnet-4-6",
            max_tokens=16,
            messages=[Message(role="user", content="hi")],
        )
    )
    capture = _make_capture(store, request_id="req_opt")
    capture.set_plan(plan)
    capture.set_routing(plan.primary, 0)
    # Whatever routing decided is now overridden by the local answer.
    assert capture._record.provider is not None
    capture.set_optimization("title_generation_skip", 4931)
    capture.finish_success("Conversation")
    store.close()

    row = store.get_request("req_opt")
    assert row is not None
    assert row["optimization"] == "title_generation_skip"
    assert row["optimization_tokens_saved"] == 4931
    assert row["provider"] is None
    assert row["resolved_model"] is None
    # NULL, not 0: no provider spoke, and silence is not a reported zero.
    assert row["tokens_in"] is None
    assert row["tokens_out"] is None
    # What the request *would* have used stays answerable.
    assert row["requested_model"] == "claude-sonnet-4-5"
    assert row["route_chain"]


def test_ordinary_row_leaves_the_optimization_columns_null(
    store: RequestLogStore,
) -> None:
    capture = _make_capture(store, request_id="req_plain")
    capture.finish_success("hello")
    store.close()

    row = store.get_request("req_plain")
    assert row is not None
    assert row["optimization"] is None
    assert row["optimization_tokens_saved"] is None


def test_recovery_counters_land_on_their_own_attempt_row(
    store: RequestLogStore,
) -> None:
    """Events recorded between attempt boundaries attach to that attempt."""
    plan = _vision_router().resolve_messages_plan(
        MessagesRequest(
            model="claude-sonnet-4-6",
            max_tokens=8,
            messages=[Message(role="user", content="hi")],
        )
    )
    capture = _make_capture(store, request_id="req_rec")
    capture.set_plan(plan)
    capture.set_routing(plan.attempts[0], 0)
    record_recovery_event("early_retries")
    record_recovery_event("early_retries")
    record_recovery_event("salvages")
    capture.set_routing(plan.attempts[1], 1)
    record_recovery_event("midstream_recoveries")

    capture.record_attempt_result(
        RouteAttemptRecord(
            attempt=0,
            provider_id="nvidia_nim",
            model_ref="nvidia_nim/blind",
            outcome="failed",
            error_kind="timeout",
        )
    )
    capture.record_attempt_result(
        RouteAttemptRecord(
            attempt=1,
            provider_id="groq",
            model_ref="groq/backup",
            outcome="succeeded",
        )
    )
    capture.finish_success("ok")
    store.close()
    _RECOVERY_TRACE.set(None)

    row = store.get_request("req_rec")
    assert row is not None
    first, second = row["route_attempts"]
    assert first["params"] == {"early_retries": 2, "salvages": 1}
    assert second["params"] == {"midstream_recoveries": 1}
    # The aggregates read the same numbers the rows do.
    assert store.stats()["recovery"] == {
        "early_retries": 2,
        "midstream_recoveries": 1,
        "salvages": 1,
    }


def test_a_disabled_capture_installs_no_recovery_trace() -> None:
    """Providers run unrecorded when logging is off."""
    _RECOVERY_TRACE.set(None)
    capture = _make_capture(None, request_id="req_off")
    assert capture.enabled is False

    record_recovery_event("early_retries")
    capture.finish_success("ok")

    assert _RECOVERY_TRACE.get() is None


def test_the_capture_records_the_wire_body_not_the_clients_ask(
    store: RequestLogStore,
) -> None:
    """The defect, end to end: 64,000 asked for, 16,384 sent, 16,384 recorded.

    ``params`` on the request row is still the client's ask, deliberately --
    that is a real fact about the request. What was missing was the other one.
    """
    plan = _vision_router().resolve_messages_plan(
        MessagesRequest(
            model="claude-sonnet-4-6",
            max_tokens=64000,
            messages=[Message(role="user", content="hi")],
        )
    )
    capture = _make_capture(
        store, request_id="req_wire", params={"max_tokens": 64000, "tools": 40}
    )
    capture.set_plan(plan)
    capture.set_routing(plan.attempts[0], 0)
    # What the provider hands its SDK, after the budget and every postprocessor.
    record_wire_request(
        {
            "model": "thinkingmachines/inkling",
            "max_tokens": 16384,
            "reasoning_effort": "high",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"function": {"name": "Read"}}],
        }
    )
    capture.record_attempt_result(
        RouteAttemptRecord(
            attempt=0,
            provider_id="nvidia_nim",
            model_ref="nvidia_nim/thinkingmachines/inkling",
            outcome="succeeded",
        )
    )
    capture.finish_success("ok")
    store.close()
    _WIRE_TRACE.set(None)

    row = store.get_request("req_wire")
    assert row is not None
    assert row["params"] == {"max_tokens": 64000, "tools": 40}, "the client's ask"
    (attempt,) = row["route_attempts"]
    assert attempt["params"]["wire"]["max_tokens"] == 16384, "what was sent"
    assert attempt["params"]["wire"]["tools"] == 1
    assert attempt["reasoning_emitted"] is True
    assert attempt["wire_body"]["max_tokens"] == 16384


def test_wire_bodies_land_on_their_own_attempt_row(store: RequestLogStore) -> None:
    """Each attempt's body attaches to that attempt, not to the whole request."""
    plan = _vision_router().resolve_messages_plan(
        MessagesRequest(
            model="claude-sonnet-4-6",
            max_tokens=8,
            messages=[Message(role="user", content="hi")],
        )
    )
    capture = _make_capture(store, request_id="req_two")
    capture.set_plan(plan)
    capture.set_routing(plan.attempts[0], 0)
    record_wire_request({"model": "blind", "max_tokens": 4096})
    capture.set_routing(plan.attempts[1], 1)
    record_wire_request({"model": "backup", "max_tokens": 8192, "thinking": {}})

    capture.record_attempt_result(
        RouteAttemptRecord(
            attempt=0, provider_id="nvidia_nim", model_ref="x", outcome="failed"
        )
    )
    capture.record_attempt_result(
        RouteAttemptRecord(
            attempt=1, provider_id="groq", model_ref="y", outcome="succeeded"
        )
    )
    capture.finish_success("ok")
    store.close()
    _WIRE_TRACE.set(None)

    row = store.get_request("req_two")
    assert row is not None
    first, second = row["route_attempts"]
    assert first["params"]["wire"]["max_tokens"] == 4096
    assert second["params"]["wire"]["max_tokens"] == 8192
    # An empty ``thinking`` object is not an instruction.
    assert second["reasoning_emitted"] is False


def test_an_untracked_capture_installs_no_wire_trace(store: RequestLogStore) -> None:
    """A disabled log must not start collecting bodies it will never write."""
    _WIRE_TRACE.set(None)
    capture = _make_capture(None, request_id="req_off")
    assert capture.enabled is False
    record_wire_request({"model": "m", "max_tokens": 1})
    assert _WIRE_TRACE.get() is None


# --------------------------------------------------------- the retry ladder --


def test_attempt_params_nests_ladder_beside_wire(store) -> None:
    """Both are per-attempt facts of variable shape; ``params`` holds both."""
    capture = _make_capture(store, request_id="req_ladder")
    record_wire_request({"model": "m", "max_tokens": 100})
    record_recovery_event("early_retries")
    record_upstream_try(key_index=0, key_label="ab...cd", status=429)
    record_upstream_wait(2.7)
    record_upstream_try(key_index=0, key_label="ab...cd", status=502)
    record_credential_decision(
        key_index=0, cls="rate_limit", benched_for_s=60.0, status=429
    )

    capture.record_attempt_result(
        RouteAttemptRecord(
            attempt=0,
            provider_id="nvidia_nim",
            model_ref="nvidia_nim/kimi",
            outcome="failed",
            error_kind="upstream",
            duration_ms=9_000.0,
        )
    )
    capture.finish_error(RuntimeError("boom"))
    store.close()
    _RECOVERY_TRACE.set(None)
    _WIRE_TRACE.set(None)
    _LADDER.set(None)

    attempt = store.get_request("req_ladder")["route_attempts"][0]
    params = attempt["params"]
    assert params["early_retries"] == 1
    assert params["wire"]["model"] == "m"
    assert params["ladder"]["summary"]["statuses_by_code"] == {"429": 1, "502": 1}
    assert params["ladder"]["tries"][0]["waited_ms"] == 2700.0
    assert "key 0 benched 60s on 429" in params["ladder"]["root_cause"]
    assert attempt["ladder_tries"] == 2


def test_a_single_try_attempt_stores_no_root_cause_sentence(store) -> None:
    """Nothing was hidden, so nothing is added to the reason already there."""
    capture = _make_capture(store, request_id="req_one")
    record_upstream_try(key_index=0, status=400)

    capture.record_attempt_result(
        RouteAttemptRecord(
            attempt=0,
            provider_id="nvidia_nim",
            model_ref="nvidia_nim/kimi",
            outcome="failed",
            error_kind="invalid_request",
        )
    )
    capture.finish_error(RuntimeError("boom"))
    store.close()
    _LADDER.set(None)

    attempt = store.get_request("req_one")["route_attempts"][0]
    assert attempt["params"]["ladder"]["root_cause"] == ""
    assert attempt["ladder_tries"] == 1


def test_attempt_key_index_is_the_key_that_attempt_used(store) -> None:
    """The credential regression: every row used to carry the chain's LAST key.

    ``record_attempt_result`` fires once per attempt, but the ledger publishes
    every record in one loop at the end of the chain -- so reading the shared
    attribution slot stamped whichever key finished the request onto all of
    them, including attempts against a different provider's pool. The ladder
    knows which key each try actually held.
    """
    capture = _make_capture(store, request_id="req_keys")
    ladder = current_ladder()
    assert ladder is not None
    record_upstream_try(key_index=0, key_label="aa...aa", status=429)
    record_upstream_try(key_index=0, key_label="aa...aa", status=502)
    ladder.current_attempt = 1
    record_upstream_try(key_index=3, key_label="zz...zz", status=200)

    capture.record_attempt_result(
        RouteAttemptRecord(
            attempt=0,
            provider_id="nvidia_nim",
            model_ref="nvidia_nim/kimi",
            outcome="failed",
            error_kind="upstream",
        )
    )
    capture.record_attempt_result(
        RouteAttemptRecord(
            attempt=1,
            provider_id="groq",
            model_ref="groq/llama",
            outcome="succeeded",
        )
    )
    capture.finish_success("ok")
    store.close()
    _LADDER.set(None)

    first, second = store.get_request("req_keys")["route_attempts"]
    assert (first["key_index"], first["key_label"]) == (0, "aa...aa")
    assert (second["key_index"], second["key_label"]) == (3, "zz...zz")


def test_an_attempt_with_no_ladder_falls_back_to_the_attribution_slot(store) -> None:
    """A skipped attempt records no try, and the old behaviour still applies."""
    capture = _make_capture(store, request_id="req_skip")
    record_credential(2, "cc...cc")

    capture.record_attempt_result(
        RouteAttemptRecord(
            attempt=0,
            provider_id="groq",
            model_ref="groq/llama",
            outcome="skipped",
            error_message="never reached",
        )
    )
    capture.finish_success("ok")
    store.close()
    _LADDER.set(None)

    attempt = store.get_request("req_skip")["route_attempts"][0]
    assert attempt["key_index"] == 2
    assert attempt["ladder_tries"] is None
