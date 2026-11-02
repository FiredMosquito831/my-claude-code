"""The equality bar: a callback in the read path changes nothing.

``specs/INVESTIGATION-USAGE-AND-CREDITS.md`` §7 PR 2, and the brief's "this is
the load-bearing proof". An observer is installed on providers that opt in --
which means a callback now runs inside every upstream response on those
providers' read paths. The claim is that **nothing changes**, and these tests
prove it four ways:

1. **Outbound bytes are identical.** The body the Chat Completions SDK is
   called with, and the bytes the Responses/Messages door puts on the wire, are
   compared with and without an observer installed. A `dict` copied straight
   out of the recorded call, and an `httpx.Request`'s exact content, are both
   `==` comparisons rather than a reading of a log line.
2. **Inbound behaviour is identical.** The forwarded SSE text, the
   ``ResponseShape`` and the finish reason are compared the same way, for a
   provider with an observer and one without.
3. **A provider that does not opt in is untouched entirely.** The factory hands
   back the object it always built, with no observer attribute written on it,
   and the shared observation board stays empty.
4. **The observer never raises.** A deliberately throwing callback is installed
   and a real response is streamed through to the client's last byte. This is
   the mutation-shaped half: the guard is proven against the broken case, which
   is the only way to know it is there.

The instrumented provider is ``deepseek`` because it is a plain member of the
OpenAI-chat family with no marketplace and no identity profile, so every
difference a comparison reports is this release's and nothing else's.
"""

import json
import re
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from my_claude_code.core.anthropic.models import MessagesRequest
from my_claude_code.core.anthropic.stream_contracts import parse_sse_text
from my_claude_code.core.usage_capture import UsageHeaderObserver
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.deepseek import DeepSeekProvider
from my_claude_code.providers.openai_chat.responses_transport import ResponsesTransport
from my_claude_code.providers.runtime.usage_observers import install_usage_observer
from tests.providers.support import passthrough_rate_limiter, reasoning_for


def _request() -> MessagesRequest:
    return MessagesRequest.model_validate(
        {
            "model": "deepseek-chat",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hello"}],
        }
    )


#: The one field two runs cannot share: ``message_start`` mints a fresh UUID per
#: stream in every tree, because it identifies that response. Normalised by the
#: test harness so everything else in the frame is still compared.
_MESSAGE_ID = re.compile(r'"id": "msg_[0-9a-f-]+"')


def _usage_response_headers() -> dict[str, str]:
    """Headers a real host might send alongside a successful stream."""

    return {
        "content-type": "text/event-stream",
        "x-ratelimit-limit-requests": "5000",
        "x-ratelimit-remaining-requests": "4997",
        "x-ratelimit-reset-requests": "1s",
        "x-nous-credits-remaining-usd": "142.5",
        # Not allow-listed, and must not be stored:
        "set-cookie": "session=abc",
    }


def _chat_chunks() -> list[Any]:
    """Three chunks: content, a stop, then a usage-only tail.

    Byte-for-byte the shapes ``tests/providers/test_deepseek.py`` uses, so the
    conversion path exercised here is the one the suite already trusts.
    """

    return [
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content="hello", reasoning_content=None, tool_calls=None
                    ),
                    finish_reason=None,
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(
                        content=None, reasoning_content=None, tool_calls=None
                    ),
                    finish_reason="stop",
                )
            ],
            usage=None,
        ),
        SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(
                completion_tokens=3,
                prompt_tokens=30,
                prompt_cache_hit_tokens=10,
                prompt_cache_miss_tokens=20,
            ),
        ),
    ]


class _ObservedStream:
    """A chat stream that also exposes a response, as the SDK's does.

    ``adopt_chat_stream`` returns an ``openai.AsyncStream`` untouched and hands
    everything else back as it was, so this double travels the exact path a
    real SDK stream does for the observer's purposes: the code reads
    ``stream.response.headers`` off the object the create call returned.
    """

    def __init__(self, headers: dict[str, str], chunks: list[Any]) -> None:
        self.response = SimpleNamespace(headers=headers, status_code=200)
        self._chunks = chunks
        self._released = False

    def __aiter__(self) -> Any:
        return self._iterate()

    async def _iterate(self) -> Any:
        for chunk in self._chunks:
            yield chunk

    async def close(self) -> None:
        self._released = True

    async def aclose(self) -> None:
        self._released = True


def _provider() -> DeepSeekProvider:
    config = ProviderConfig(
        api_key="sk-test-not-a-real-key",
        base_url="https://api.deepseek.com",
        rate_limit=1_000_000,
        rate_window=1,
        max_concurrency=4,
    )
    return DeepSeekProvider(config, rate_limiter=passthrough_rate_limiter())


async def _run_chat(provider: DeepSeekProvider) -> tuple[list[Any], dict[str, Any]]:
    """Stream one request and report both halves the equality is about.

    Returns the recorded SDK call's kwargs (the outbound body) and the forwarded
    Anthropic SSE text with its finish reason (the inbound behaviour).
    """

    stream = _ObservedStream(_usage_response_headers(), _chat_chunks())
    create = AsyncMock(return_value=stream)
    request = _request()
    with patch.object(provider._client.chat.completions, "create", create):
        chunks = [
            chunk
            async for chunk in provider.stream_response(
                request,
                input_tokens=7,
                request_id="equality",
                reasoning=reasoning_for(request),
            )
        ]
    # Every attempt the retry loop made, in order, so a difference in retry
    # behaviour would show up as a difference here rather than being averaged
    # away by comparing only the last one.
    sent = [dict(call.kwargs) for call in create.await_args_list]
    # The one field that cannot be equal between two runs is the Anthropic
    # message id: `message_start` mints a fresh UUID per stream, in both trees,
    # because it identifies THIS response. Substituted out rather than the whole
    # frame being skipped, so every other byte of that event is still compared.
    text = _MESSAGE_ID.sub('"id": "<per-stream uuid>"', "".join(chunks))
    parsed = parse_sse_text(text)
    stops = [
        event.data.get("delta", {}).get("stop_reason")
        for event in parsed
        if event.event == "message_delta"
    ]
    return sent, {
        "sse": text,
        "events": [
            (event.event, json.dumps(event.data, sort_keys=True)) for event in parsed
        ],
        "stop_reasons": stops,
    }


# ------------------------------------------------- outbound and inbound equality


@pytest.mark.asyncio
async def test_an_installed_observer_leaves_the_outbound_body_byte_identical() -> None:
    """The body the SDK is called with, with and without an observer."""

    baseline_provider = _provider()
    assert getattr(baseline_provider, "_response_observer", None) is None
    baseline_out, baseline_in = await _run_chat(baseline_provider)

    observed_provider = _provider()
    assert install_usage_observer(observed_provider, "deepseek") is True
    assert getattr(observed_provider, "_response_observer", None) is not None
    observed_out, observed_in = await _run_chat(observed_provider)

    assert observed_out == baseline_out, "the outbound body changed"
    assert observed_in == baseline_in, "the forwarded stream changed"


@pytest.mark.asyncio
async def test_the_observer_actually_recorded_something_so_equality_is_not_vacuous() -> (
    None
):
    """Equality alone could be empty. This is the other half.

    A comparison between two runs that record nothing would pass for ever, so
    the same fixture that proves equality also proves the observer ran.
    """

    board = UsageHeaderObserver()
    provider = _provider()
    assert install_usage_observer(provider, "deepseek", observer=board) is True
    await _run_chat(provider)
    snapshot = board.latest_for("deepseek")
    assert snapshot is not None
    assert snapshot.values == {
        "x-ratelimit-limit-requests": "5000",
        "x-ratelimit-remaining-requests": "4997",
        "x-ratelimit-reset-requests": "1s",
        "x-nous-credits-remaining-usd": "142.5",
    }
    # The non-allow-listed header is absent, which is the other half of the
    # allow-list proof at the integration layer rather than the unit one.
    assert "set-cookie" not in snapshot.values


@pytest.mark.asyncio
async def test_a_provider_that_did_not_opt_in_is_untouched_entirely() -> None:
    """Nothing installed, no attribute written, the board stays empty."""

    board = UsageHeaderObserver()
    provider = _provider()
    # ``kimi_coding`` has no header mechanism in the declared table.
    assert install_usage_observer(provider, "kimi_coding", observer=board) is False
    assert getattr(provider, "_response_observer", None) is None
    await _run_chat(provider)
    assert board.latest_keys() == ()
    await provider.cleanup()


# ------------------------------------------------------ the throwing-observer proof


@pytest.mark.asyncio
async def test_a_deliberately_throwing_observer_cannot_fail_a_request() -> None:
    """The guard, proven against the broken case.

    A callback that raises on every call is installed on a real provider, and
    the request still streams to its last byte with the same forwarded text and
    the same finish reason as a run with no observer at all. Without the guard
    this test raises out of the stream; that is the mutation it exists to
    catch.
    """

    calls: list[int] = []

    def explode(headers: Mapping[str, str] | None, status_code: int) -> None:
        del headers, status_code
        calls.append(1)
        raise RuntimeError("observer is broken on purpose")

    baseline_out, baseline_in = await _run_chat(_provider())

    provider = _provider()
    provider.set_response_observer(explode)
    broken_out, broken_in = await _run_chat(provider)

    assert calls, "the observer was never called -- the test proves nothing"
    assert broken_out == baseline_out
    assert broken_in == baseline_in


@pytest.mark.asyncio
async def test_a_raising_observer_on_the_responses_door_is_also_contained() -> None:
    """The same guard on the other seam this release adds, at the same point."""

    calls: list[int] = []

    def explode(headers: Mapping[str, str] | None, status_code: int) -> None:
        del headers, status_code
        calls.append(1)
        raise ValueError("broken on purpose")

    transport = ResponsesTransport(
        _config(),
        base_url="https://example.invalid/v1",
        provider_name="TEST",
        identity=None,
        api_key="sk-test",
        rate_limiter=passthrough_rate_limiter(),
    )
    transport.set_response_observer(explode)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers=_usage_response_headers(),
            content=b'data: {"type":"response.completed"}\n\n',
            request=request,
        )

    transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    response = await transport.send(
        {"model": "m"}, {"Content-Type": "application/json"}
    )
    assert response.status_code == 200
    assert calls == [1]
    await response.aclose()
    await transport.aclose()


@pytest.mark.asyncio
async def test_the_responses_door_puts_identical_bytes_on_the_wire() -> None:
    """The other seam, compared at the request object rather than the body dict.

    ``httpx`` builds a request from explicit ``content``, so ``request.content``
    is the exact byte string that would go on a socket. Comparing it between a
    transport with an observer and one without is the strongest form of the
    outbound claim available offline: a header the observer added, a body
    re-serialised with different key order, or a changed method or URL would
    all show up here.
    """

    captured: list[httpx.Request] = []

    def _transport(installed: bool) -> ResponsesTransport:
        transport = ResponsesTransport(
            _config(),
            base_url="https://example.invalid/v1",
            provider_name="TEST",
            identity=None,
            api_key="sk-test",
            rate_limiter=passthrough_rate_limiter(),
        )
        if installed:
            transport.set_response_observer(lambda headers, status: None)

        async def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                headers=_usage_response_headers(),
                content=b'data: {"type":"response.completed"}\n\n',
                request=request,
            )

        transport._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return transport

    body = {"model": "some-model", "input": [{"role": "user"}], "stream": False}
    headers = {"Content-Type": "application/json"}

    baseline = _transport(False)
    await (await baseline.send(dict(body), dict(headers))).aclose()
    await baseline.aclose()

    observed = _transport(True)
    await (await observed.send(dict(body), dict(headers))).aclose()
    await observed.aclose()

    assert len(captured) == 2
    first, second = captured
    signature = (
        first.method,
        str(first.url),
        first.content,
        tuple(sorted(first.headers.items())),
    )
    assert signature == (
        second.method,
        str(second.url),
        second.content,
        tuple(sorted(second.headers.items())),
    )


# --------------------------------------------------------------- install plumbing


def test_commandcode_covers_both_protocol_delegates() -> None:
    """One Command Code catalogue, two delegates, one host -- both observed.

    Installing on only one would report half the user's traffic and look like a
    host that had gone quiet, which is the sort of wrong answer a card is least
    able to detect.
    """

    from my_claude_code.providers.commandcode import CommandCodeProvider

    config = _config()
    provider = CommandCodeProvider(config, rate_limiter=passthrough_rate_limiter())
    board = UsageHeaderObserver()
    assert install_usage_observer(provider, "commandcode", observer=board) is True
    assert provider._anthropic._response_observer is not None
    assert provider._openai._response_observer is not None


def test_an_installed_observer_reaches_a_transport_built_before_the_install() -> None:
    """Construction order must not decide whether the seam works."""

    provider = _provider()
    transport = provider._responses
    assert transport._response_observer is None
    board = UsageHeaderObserver()
    assert install_usage_observer(provider, "deepseek", observer=board) is True
    assert transport._response_observer is not None


def test_install_on_a_provider_without_a_seam_is_false_and_silent() -> None:
    """A provider class with no seam is not a failure, it is not a customer."""

    class _NoSeam:
        pass

    assert install_usage_observer(_NoSeam(), "deepseek") is False


def _config() -> ProviderConfig:
    return ProviderConfig(
        api_key="sk-test-not-a-real-key",
        base_url="https://example.invalid/v1",
        rate_limit=1_000_000,
        rate_window=1,
        max_concurrency=4,
    )
