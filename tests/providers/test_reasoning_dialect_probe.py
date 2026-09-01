"""The two-request probe, and what each outcome is allowed to claim."""

import httpx
import pytest

from my_claude_code.providers.runtime.reasoning_probe import (
    PROBE_INVALID_EFFORT,
    probe_reasoning_dialect,
)

B_AI_400 = (
    '{"error":{"message":"The request is invalid: '
    "\u8be5\u6a21\u578b\u59cb\u7ec8\u601d\u8003\uff0c\u4e0d\u652f\u6301\u5173\u95ed\u601d\u8003\uff1b"
    "\u8bf7\u4f7f\u7528 low\u3001high \u6216 max\u3002"
    '. Please check the request body.","type":"invalid_request_error"}}'
)


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _probe(handler, model: str = "glm-5.3-flash"):
    async with _client(handler) as client:
        return await probe_reasoning_dialect(
            "https://api.example/v1", "sk-secret-key", model, client=client
        )


@pytest.mark.anyio
async def test_a_400_naming_an_enum_is_learned() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text=B_AI_400)

    outcome = await _probe(handler)

    assert outcome.status == "learned"
    assert outcome.effort_enum == ("low", "high", "max")
    assert outcome.field_ignored is False
    assert outcome.probed_at


@pytest.mark.anyio
async def test_the_probe_is_one_tiny_request_when_the_enum_is_named() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400, text=B_AI_400)

    await _probe(handler)

    assert len(calls) == 1
    body = calls[0].read().decode()
    assert PROBE_INVALID_EFFORT in body
    assert '"max_tokens": 16' in body or '"max_tokens":16' in body
    assert str(calls[0].url).endswith("/v1/chat/completions")


@pytest.mark.anyio
async def test_a_200_means_the_host_does_not_read_the_field() -> None:
    outcome = await _probe(lambda request: httpx.Response(200, json={"choices": []}))

    assert outcome.status == "ignored"
    assert outcome.field_ignored is True
    assert outcome.effort_enum == ()


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 402, 403, 429, 500, 503])
async def test_an_answer_before_validation_claims_nothing(status: int) -> None:
    outcome = await _probe(lambda request: httpx.Response(status, text="nope"))

    assert outcome.status == "unknown"
    assert outcome.detail == str(status)
    assert outcome.effort_enum == ()


@pytest.mark.anyio
async def test_a_400_naming_no_enum_costs_one_more_request_and_still_claims_nothing() -> (
    None
):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(400, text="model is not available on your plan")
        return httpx.Response(200, json={"choices": []})

    outcome = await _probe(handler)

    assert len(calls) == 2
    assert outcome.status == "unknown"
    assert outcome.detail == "400 (no enum named)"


@pytest.mark.anyio
async def test_a_transport_failure_claims_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    outcome = await _probe(handler)

    assert outcome.status == "unknown"
    assert outcome.detail == "ConnectError"


@pytest.mark.anyio
async def test_nothing_is_sent_when_there_is_nothing_to_probe() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    outcome = await _probe(handler, model="")

    assert outcome.status == "unknown"
    assert outcome.detail == "not configured"


@pytest.mark.anyio
async def test_the_outcome_payload_never_carries_the_key_or_the_body() -> None:
    outcome = await _probe(lambda request: httpx.Response(400, text=B_AI_400))
    rendered = repr(outcome.as_payload())

    assert "sk-secret-key" not in rendered
    assert "invalid_request_error" not in rendered
