"""The in-flight view, driven through the real ``RequestCapture`` and handlers.

Three promises, each asserted rather than argued:

* every phase the view names is stamped by a statement the capture executed,
  with the request log on *and* off;
* no entry outlives its request on any exit path -- including the one the
  ``self._store is None`` early return would otherwise leak -- on all four
  inbound surfaces;
* nothing a client wrote -- prompt, system block, reply, header value, key --
  reaches the endpoint's bytes.

No upstream is called: providers are doubles that yield recorded frames.
"""

import asyncio
import json
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from my_claude_code.api.request_capture import RequestCapture, build_capture
from my_claude_code.application.routing import (
    ResolvedModel,
    RoutedMessagesPlan,
    RoutedMessagesRequest,
)
from my_claude_code.application.tier_chains import TierChain
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core import request_tasks
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.anthropic.streaming import format_sse_event
from my_claude_code.core.async_iterators import try_close_async_iterator
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.reasoning import (
    ReasoningAdaptation,
    ReasoningAdaptationKind,
    ReasoningPolicy,
)
from my_claude_code.core.request_log import RequestLogStore
from my_claude_code.core.request_tasks import (
    PHASE_ATTEMPT,
    PHASE_AWAITING_CONTENT,
    PHASE_DESCRIBE,
    PHASE_RECEIVED,
    PHASE_ROUTING,
    PHASE_STREAMING,
    inflight_report,
)
from my_claude_code.core.tier_refs import ModelTier
from my_claude_code.core.upstream_ladder import record_upstream_try
from tests.api.support import create_test_app

SESSION = "0f3c2a1b-6d5e-4f70-9a8b-1c2d3e4f5a6b"
FOLDER = "C:\\Users\\devuser\\Projects\\demo"
SECRET = "sk-inflight-SECRET-7777777777"
PROMPT_WORDS = "the quick brown fox jumps"
SYSTEM = (
    "You are Claude Code.\n# Environment\n"
    "You have been invoked in the following environment:\n"
    f" - Primary working directory: {FOLDER}\n - Platform: win32\n"
)
CLAUDE_HEADERS = {
    "user-agent": "claude-cli/2.1.258 (external, cli)",
    "x-app": "cli",
    "x-claude-code-session-id": SESSION,
    "authorization": f"Bearer {SECRET}",
    "x-api-key": SECRET,
}


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db")
    yield store
    store.close()


def _capture(store: RequestLogStore | None, **overrides: Any) -> RequestCapture:
    defaults: dict[str, Any] = {
        "request_id": "req_inflight",
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "stream": True,
        "requested_model": "mcc/best",
        "input_text": PROMPT_WORDS,
        "params": {"max_tokens": 100},
        "harness": "claude",
    }
    defaults.update(overrides)
    return RequestCapture(store, **defaults)


def _routed(provider: str = "opencode", model: str = "big-pickle"):
    return RoutedMessagesRequest(
        request=MessagesRequest(
            model=model, messages=[Message(role="user", content="hi")], stream=True
        ),
        resolved=ResolvedModel(
            original_model="mcc/best",
            provider_id=provider,
            provider_model=model,
            provider_model_ref=f"{provider}/{model}",
            reasoning_preference=ReasoningPreference.CLIENT,
        ),
        reasoning=ReasoningPolicy.on(),
        requested_reasoning=ReasoningPolicy.on(),
        reasoning_adaptation=ReasoningAdaptation(
            ReasoningAdaptationKind.UNCHANGED, None
        ),
    )


def _plan(*routed: RoutedMessagesRequest, tier: bool = True) -> RoutedMessagesPlan:
    tier_route = (
        TierChain(
            tier=ModelTier.BEST,
            harness="claude",
            source="global",
            refs=tuple(item.resolved.provider_model_ref for item in routed),
            paused=(),
            paused_label="MODEL_BEST",
        )
        if tier
        else None
    )
    return RoutedMessagesPlan(tuple(routed), tier_route=tier_route)


def _frames(*events: str) -> list[str]:
    return [f"event: {name}\ndata: {json.dumps({'type': name})}\n\n" for name in events]


async def _feed(chunks: list[str]):
    for chunk in chunks:
        yield chunk


def _row() -> dict[str, Any]:
    report = inflight_report()
    assert report["total"] == 1, report
    return report["rows"][0]


def _describe_attempt() -> Any:
    attempt = MagicMock()
    attempt.provider_id = "vision"
    attempt.model_ref = "vision/eye"
    attempt.attempt = 0
    attempt.outcome = "succeeded"
    attempt.error_kind = None
    attempt.error_message = None
    attempt.duration_ms = 5.0
    attempt.ttft_ms = None
    attempt.first_reasoning_ms = None
    return attempt


@pytest.mark.parametrize("logged", [True, False], ids=["log-on", "log-off"])
class TestPhasesThroughTheCapture:
    @pytest.mark.asyncio
    async def test_every_transition_is_stamped_where_it_happens(
        self, store, logged
    ) -> None:
        capture = _capture(store if logged else None)
        assert _row()["phase"] == PHASE_RECEIVED

        capture.record_describe_attempt(_describe_attempt(), "sha", 0)
        row = _row()
        assert row["phase"] == PHASE_DESCRIBE
        assert row["describe_hops"] == 1

        before_plan = time.monotonic()
        capture.set_plan(_plan(_routed(), _routed("nim", "kimi")))
        row = _row()
        assert row["phase"] == PHASE_ROUTING
        assert row["phase_since"] >= round(before_plan, 3)
        assert row["tier"] == "best"
        assert row["tier_source"] == "global"

        capture.set_routing(_routed(), 0)
        first_attempt = _row()
        assert first_attempt["phase"] == PHASE_ATTEMPT
        assert first_attempt["attempt_index"] == 0
        assert first_attempt["provider"] == "opencode"
        assert first_attempt["model_ref"] == "opencode/big-pickle"

        # The executor announces attempt 0 again; that is not a new attempt.
        await asyncio.sleep(0.01)
        capture.set_routing(_routed(), 0)
        assert _row()["phase_since"] == first_attempt["phase_since"]

        capture.set_routing(_routed("nim", "kimi"), 1)
        fallback = _row()
        assert fallback["attempt_index"] == 1
        assert fallback["provider"] == "nim"
        assert fallback["phase_since"] > first_attempt["phase_since"]

        # With the log off the capture has no observer: the stream's own
        # chunk counter (``_PrefetchedStream``) is the witness, as it is on
        # the server.
        text_delta = format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "hello"},
            },
        )
        body = capture.wrap(
            _feed([*_frames("message_start"), text_delta, *_frames("message_stop")])
        )
        first = await anext(body)
        if not logged:
            request_tasks.note_stream_chunk()
        assert first
        opened = _row()
        assert opened["ttft_ms"] is not None
        assert opened["observed"] is logged
        if logged:
            # MCC's opening frame is out; the model has said nothing yet.
            assert opened["phase"] == PHASE_AWAITING_CONTENT
            assert opened["output_chars"] == 0
            assert opened["first_content_ms"] is None
        else:
            assert opened["phase"] == PHASE_STREAMING
            assert opened["output_chars"] is None
        await asyncio.sleep(0.01)
        await anext(body)
        talking = _row()
        assert talking["phase"] == PHASE_STREAMING
        if logged:
            assert talking["output_chars"] == len("hello")
            assert talking["first_content_ms"] is not None
            assert talking["phase_since"] > opened["phase_since"]
        await try_close_async_iterator(body)
        if not logged:
            capture.finish_success(None)
        assert request_tasks.count() == 0

    def test_the_ladder_tries_are_counted_only_while_it_is_installed(
        self, store, logged
    ) -> None:
        capture = _capture(store if logged else None)
        capture.set_routing(_routed(), 0)
        record_upstream_try(status=429, error_kind="rate_limit")
        row = _row()
        if logged:
            assert row["attempt_tries"] == 1
            assert row["last_try_status"] == 429
            assert row["last_try_error_kind"] == "rate_limit"
        else:
            assert row["attempt_tries"] is None
            assert row["last_try_status"] is None
        capture.finish_success("x")


def _request(system: str | None = SYSTEM) -> MessagesRequest:
    return MessagesRequest(
        model="mcc/best",
        max_tokens=100,
        stream=True,
        system=system,
        messages=[Message(role="user", content=PROMPT_WORDS)],
    )


class TestOrigin:
    def test_the_session_shows_at_once_and_the_folder_is_pending(self) -> None:
        capture = build_capture(
            Settings(),
            _request(),
            request_id="req_origin",
            endpoint="/v1/messages",
            protocol="anthropic",
            headers=CLAUDE_HEADERS,
        )
        row = _row()
        assert row["harness"] == "claude"
        assert row["session_id"] == SESSION
        assert row["session_short"]
        # The folder lives in the system prompt, read off the loop at
        # finalize -- the view says so instead of guessing.
        assert row["project_dir"] is None
        assert row["project_dir_pending"] is True
        capture.finish_success("done")
        assert request_tasks.count() == 0

    def test_the_capture_settings_govern_memory_too(self) -> None:
        settings = Settings()
        settings.request_log_capture_session = False
        settings.request_log_capture_folder = False
        capture = build_capture(
            settings,
            _request(),
            request_id="req_private",
            endpoint="/v1/messages",
            protocol="anthropic",
            headers=CLAUDE_HEADERS,
        )
        row = _row()
        assert row["session_id"] is None
        assert row["project_dir"] is None
        assert row["project_dir_pending"] is False
        capture.finish_success("done")

    def test_with_the_log_off_the_session_still_shows_and_no_folder_is_promised(
        self,
    ) -> None:
        settings = Settings()
        settings.request_log_enabled = False
        capture = build_capture(
            settings,
            _request(),
            request_id="req_unlogged",
            endpoint="/v1/messages",
            protocol="anthropic",
            headers=CLAUDE_HEADERS,
        )
        assert capture.enabled is False
        row = _row()
        assert row["session_id"] == SESSION
        assert row["project_dir_pending"] is False
        assert row["tools_count"] == 0
        assert row["input_chars"] and row["input_chars"] > len(PROMPT_WORDS)
        capture.finish_success("done")
        assert request_tasks.count() == 0

    def test_nothing_the_client_wrote_reaches_the_report(self) -> None:
        capture = build_capture(
            Settings(),
            _request(system=SYSTEM + f"\napi key {SECRET}"),
            request_id="req_secret",
            endpoint="/v1/messages",
            protocol="anthropic",
            headers=CLAUDE_HEADERS,
        )
        capture.set_routing(_routed(), 0)
        text = json.dumps(inflight_report())
        assert SECRET not in text
        assert PROMPT_WORDS not in text
        assert "Primary working directory" not in text
        assert "claude-cli" not in text
        capture.finish_success("done")


EXIT_PATHS = [
    "success",
    "error",
    "cancelled",
    "generator_exit",
    "optimizer",
    "stream_error",
]


@pytest.mark.parametrize("logged", [True, False], ids=["log-on", "log-off"])
@pytest.mark.parametrize("path", EXIT_PATHS)
@pytest.mark.asyncio
async def test_inflight_unregisters_on_every_exit_path(store, logged, path) -> None:
    """``_begin_finalize`` runs first; the log-off early return cannot leak.

    Where the log is off the capture has no stream observer at all, so the
    handler's own terminal call (``finish_*``) is what finalizes -- and the
    weakref reaper covers the one shape that never finalizes (see the route
    tests). Both are asserted here, per exit path.
    """

    capture = _capture(store if logged else None)
    assert request_tasks.count() == 1
    if path == "success":
        capture.finish_success("hi")
    elif path == "error":
        capture.finish_error(
            ExecutionFailure(
                kind=FailureKind.UPSTREAM,
                message="no",
                status_code=502,
                retryable=False,
            )
        )
    elif path == "optimizer":
        capture.set_optimization("cache_hit", 42)
        capture.finish_success_from_message(
            type("Msg", (), {"content": [], "usage": None, "stop_reason": "end_turn"})()
        )
    elif not logged:
        # No observer exists with the log off: the stream is the provider's own
        # and the handler finalizes on its terminal call, exactly as before.
        capture.finish_success(None)
    elif path == "generator_exit":
        body = capture.wrap(_feed(_frames("message_start", "content_block_start")))
        await anext(body)
        await try_close_async_iterator(body)
    elif path == "cancelled":
        release = asyncio.Event()

        async def slow():
            yield _frames("message_start")[0]
            await release.wait()
            yield "never"

        async def consume() -> None:
            async for _chunk in capture.wrap(slow()):
                pass

        task = asyncio.create_task(consume())
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    elif path == "stream_error":

        async def broken():
            raise RuntimeError("socket closed")
            yield "unreachable"

        with pytest.raises(RuntimeError):
            async for _chunk in capture.wrap(broken()):
                pass
    assert request_tasks.count() == 0, path
    assert inflight_report()["total"] == 0


# --- the four inbound surfaces, end to end through the real handlers ------


def _anthropic_text_stream(text: str) -> list[str]:
    return [
        format_sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 3, "output_tokens": 0}},
            },
        ),
        format_sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        format_sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        format_sse_event(
            "content_block_stop", {"type": "content_block_stop", "index": 0}
        ),
        format_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 4},
            },
        ),
        format_sse_event("message_stop", {"type": "message_stop"}),
    ]


class SnapshotProvider:
    """Yields a recorded answer, reading the in-flight view while it does.

    The read happens inside the provider, i.e. while the request is genuinely
    in flight on the server's own loop -- so what it sees is what an operator
    polling the endpoint at that moment would have seen.
    """

    def __init__(self, *, fail: bool = False) -> None:
        self.preflight_stream = MagicMock()
        self.seen: list[dict[str, Any]] = []
        self.fail = fail

    @property
    def credential_label(self) -> str | None:
        return None

    async def stream_response(self, request_data, **_kwargs):
        self.seen.append(inflight_report())
        if self.fail:
            raise ExecutionFailure(
                kind=FailureKind.RATE_LIMIT,
                status_code=429,
                message="upstream is busy",
                retryable=False,
            )
        for chunk in _anthropic_text_stream("Hello from provider"):
            yield chunk


SURFACES = {
    "messages": (
        "/v1/messages",
        "anthropic",
        {
            "model": "nvidia_nim/test-model",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": PROMPT_WORDS}],
        },
    ),
    "chat": (
        "/v1/chat/completions",
        "openai_chat",
        {
            "model": "nvidia_nim/test-model",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": PROMPT_WORDS}],
        },
    ),
    "responses": (
        "/v1/responses",
        "openai_responses",
        {"model": "nvidia_nim/test-model", "input": PROMPT_WORDS},
    ),
    "gemini": (
        "/v1beta/models/nvidia_nim/test-model:streamGenerateContent?alt=sse",
        "gemini",
        {
            "contents": [{"role": "user", "parts": [{"text": PROMPT_WORDS}]}],
            "generationConfig": {"maxOutputTokens": 32},
        },
    ),
}


@pytest.mark.parametrize("fail", [False, True], ids=["answered", "failed"])
@pytest.mark.parametrize("stream", [True, False], ids=["stream", "json"])
@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_inflight_for_each_of_four_surfaces(surface, stream, fail) -> None:
    path, protocol, payload = SURFACES[surface]
    if surface == "gemini" and not stream:
        path = "/v1beta/models/nvidia_nim/test-model:generateContent"
    elif surface != "gemini":
        payload = {**payload, "stream": stream}
    provider = SnapshotProvider(fail=fail)
    app = create_test_app()
    target = (
        "my_claude_code.api.gemini_routes.resolve_provider"
        if surface == "gemini"
        else "my_claude_code.api.routes.resolve_provider"
    )
    with (
        patch(target, return_value=provider),
        TestClient(app) as client,
    ):
        response = client.post(path, json=payload)
        text = response.text
    if surface == "responses" and not stream:
        # ``/v1/responses`` is streaming-only and refuses a JSON ask before a
        # capture exists -- so there is nothing to register and nothing left.
        assert response.status_code == 400
        assert not provider.seen
        assert request_tasks.count() == 0
        return
    assert provider.seen, (surface, response.status_code, text[:300])
    during = provider.seen[0]
    assert during["total"] == 1, during
    row = during["rows"][0]
    assert row["protocol"] == protocol
    assert row["endpoint"].startswith("/v1")
    assert row["phase"] == PHASE_ATTEMPT
    assert row["provider"] == "nvidia_nim"
    assert PROMPT_WORDS not in json.dumps(during)
    # And gone once the answer is: no entry outlives its request.
    assert request_tasks.count() == 0, (surface, stream, fail)


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_a_streamed_request_with_the_log_off_drains_through_the_reaper(
    surface,
) -> None:
    """The one path ``_begin_finalize`` cannot see, end to end.

    With the request log off ``RequestCapture.wrap`` hands the provider's
    stream straight through, so a streamed answer never finalizes. Its entry
    must still be gone once the answer is: every task that served it is done,
    and the next read reaps it.
    """

    path, _protocol, payload = SURFACES[surface]
    if surface != "gemini":
        payload = {**payload, "stream": True}
    settings = Settings()
    settings.request_log_enabled = False
    provider = SnapshotProvider()
    app = create_test_app(settings)
    target = (
        "my_claude_code.api.gemini_routes.resolve_provider"
        if surface == "gemini"
        else "my_claude_code.api.routes.resolve_provider"
    )
    with patch(target, return_value=provider), TestClient(app) as client:
        response = client.post(path, json=payload)
        assert response.status_code == 200, response.text[:300]
    assert provider.seen and provider.seen[0]["total"] == 1
    assert provider.seen[0]["rows"][0]["observed"] is False
    report = inflight_report()
    assert report["total"] == 0
    assert request_tasks.inflight_count() == 0
