"""The create-level reasoning safety net: strip once, retry once, remember.

A host that refuses ``reasoning_effort`` used to need a hand-written
``NO_REASONING`` verdict, which cannot be right for a gateway fronting many
models. These tests pin the replacement: the host's own 400 is the evidence,
the strip is budgeted to one attempt, and the table is written only when the
retry actually succeeds -- because a rejection is an inference, not a stated
fact the way an output cap is.
"""

from datetime import date
from typing import Any

import httpx
import openai
import pytest

from my_claude_code.core.reasoning import (
    ReasoningAdaptationKind,
    ReasoningDialectOrigin,
)
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.openai_chat import OpenAIChatProvider
from my_claude_code.providers.openai_chat.profiles import OPENAI_CHAT_PROFILES
from my_claude_code.providers.rate_limit import ProviderRateLimiter

_MODEL = "m"


def _bad_request(body: Any, message: str = "Bad Request") -> openai.BadRequestError:
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(400, request=request)
    return openai.BadRequestError(message, response=response, body=body)


def _unprocessable(body: Any) -> openai.UnprocessableEntityError:
    request = httpx.Request("POST", "https://example.invalid/v1/chat/completions")
    response = httpx.Response(422, request=request)
    return openai.UnprocessableEntityError(
        "Unprocessable", response=response, body=body
    )


def _named_field_error(field: str = "reasoning_effort") -> openai.BadRequestError:
    return _bad_request({"error": {"message": f"Unsupported parameter: {field}"}})


class _FakeCreate:
    """Raises a scripted error per call, then succeeds, recording each body."""

    def __init__(self, errors: list[Exception | None]) -> None:
        self._errors = errors
        self.bodies: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> str:
        self.bodies.append(dict(kwargs))
        index = len(self.bodies) - 1
        error = self._errors[index] if index < len(self._errors) else None
        if error is not None:
            raise error
        return "stream"


def _provider(provider_id: str = "xai") -> OpenAIChatProvider:
    return OpenAIChatProvider(
        ProviderConfig(api_key="k", base_url="https://example.invalid/v1"),
        profile=OPENAI_CHAT_PROFILES[provider_id],
        rate_limiter=ProviderRateLimiter(),
        provider_id=provider_id,
    )


def _install(provider: OpenAIChatProvider, create: _FakeCreate) -> None:
    """Swap the SDK's ``create`` for a scripted double.

    Bound through an ``Any`` alias because the SDK types ``create`` as a
    three-way overload that no test double is assignable to, and this project
    bans suppression comments.
    """
    client: Any = provider._client
    client.chat.completions.create = create


def _body(**extra: Any) -> dict[str, Any]:
    return {"model": _MODEL, "messages": [], "reasoning_effort": "high", **extra}


@pytest.mark.asyncio
async def test_a_400_naming_the_reasoning_field_strips_it_and_retries_once():
    provider = _provider()
    create = _FakeCreate([_named_field_error()])
    _install(provider, create)

    await provider._create_stream(_body())

    assert len(create.bodies) == 2
    assert create.bodies[0]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in create.bodies[1]


@pytest.mark.asyncio
async def test_the_retry_is_budgeted_to_one_strip():
    """A 400 on both attempts raises rather than looping."""
    provider = _provider()
    create = _FakeCreate([_named_field_error(), _named_field_error()])
    _install(provider, create)

    with pytest.raises(openai.BadRequestError):
        await provider._create_stream(_body())
    assert len(create.bodies) == 2


@pytest.mark.asyncio
async def test_a_successful_retry_is_remembered_for_that_model():
    provider = _provider()
    _install(provider, _FakeCreate([_named_field_error()]))

    await provider._create_stream(_body())

    assert provider._rejected_reasoning_fields[_MODEL] == {
        "reasoning_effort": date.today().isoformat()
    }


@pytest.mark.asyncio
async def test_a_failed_retry_teaches_nothing():
    """The deviation from the output cap, pinned.

    If the stripped retry also fails, the field was probably not the problem,
    and teaching the process otherwise costs thinking on a model that was never
    at fault.
    """
    provider = _provider()
    _install(provider, _FakeCreate([_named_field_error(), _named_field_error()]))

    with pytest.raises(openai.BadRequestError):
        await provider._create_stream(_body())
    assert provider._rejected_reasoning_fields == {}


@pytest.mark.asyncio
async def test_a_remembered_rejection_disappears_from_the_reported_dialect():
    provider = _provider()
    _install(provider, _FakeCreate([_named_field_error()]))
    assert provider.reasoning_dialect(_MODEL).effort_values is not None

    await provider._create_stream(_body())

    dialect = provider.reasoning_dialect(_MODEL)
    assert dialect.effort_values is None
    assert dialect.effort_field == ""
    assert dialect.origin is ReasoningDialectOrigin.LEARNED
    assert dialect.learned_rejections == (
        ("reasoning_effort", date.today().isoformat()),
    )


@pytest.mark.asyncio
async def test_a_rejection_is_learned_per_model_not_per_provider():
    provider = _provider()
    _install(provider, _FakeCreate([_named_field_error()]))

    await provider._create_stream(_body())

    assert provider.reasoning_dialect("another-model").effort_values is not None


@pytest.mark.asyncio
async def test_a_400_naming_a_sampling_parameter_is_not_a_reasoning_rejection():
    """The 5.69.2 regression, pinned: sampling complaints never cost thinking."""
    provider = _provider()
    error = _bad_request(
        {
            "error": {
                "message": "top_p is immutable for this model and must be 0.95, got 1"
            }
        }
    )
    create = _FakeCreate([error])
    _install(provider, create)

    with pytest.raises(openai.BadRequestError):
        await provider._create_stream(_body())
    assert len(create.bodies) == 1
    assert create.bodies[0]["reasoning_effort"] == "high"
    assert provider._rejected_reasoning_fields == {}


@pytest.mark.asyncio
async def test_an_unrecognised_400_is_raised_rather_than_downgraded():
    provider = _provider()
    create = _FakeCreate([_bad_request({"error": {"message": "model not found"}})])
    _install(provider, create)

    with pytest.raises(openai.BadRequestError):
        await provider._create_stream(_body())
    assert len(create.bodies) == 1


@pytest.mark.asyncio
async def test_an_echoed_request_payload_is_not_evidence():
    """The reason ``upstream_complaint`` was moved rather than reinvented.

    A pydantic-shaped 400 echoes the whole submitted request under ``input``.
    Reading that back as evidence would make every unrelated rejection look
    like a reasoning rejection.
    """
    provider = _provider()
    error = _bad_request(
        {
            "detail": [
                {
                    "loc": ["body", "messages"],
                    "msg": "field required",
                    "type": "value_error.missing",
                    "input": _body(),
                }
            ]
        }
    )
    create = _FakeCreate([error])
    _install(provider, create)

    with pytest.raises(openai.BadRequestError):
        await provider._create_stream(_body())
    assert len(create.bodies) == 1
    assert provider._rejected_reasoning_fields == {}


@pytest.mark.asyncio
async def test_an_extra_body_reasoning_field_is_stripped_too():
    provider = _provider("zenmux")
    create = _FakeCreate([_named_field_error("reasoning")])
    _install(provider, create)

    await provider._create_stream(
        {
            "model": _MODEL,
            "messages": [],
            "extra_body": {"reasoning": {"effort": "high"}},
        }
    )

    assert len(create.bodies) == 2
    assert "extra_body" not in create.bodies[1]
    assert provider._rejected_reasoning_fields[_MODEL]


@pytest.mark.asyncio
async def test_the_wire_capture_keeps_the_body_that_succeeded():
    trace = install_wire_trace()
    provider = _provider()
    _install(provider, _FakeCreate([_named_field_error()]))

    await provider._create_stream(_body())

    assert "reasoning_effort" not in trace.requests[0].body_json


@pytest.mark.asyncio
async def test_the_strip_is_recorded_as_a_reasoning_adaptation():
    trace = install_wire_trace()
    provider = _provider()
    _install(provider, _FakeCreate([_named_field_error()]))

    await provider._create_stream(_body())

    assert len(trace.reasoning_adaptations) == 1
    adaptation = trace.reasoning_adaptations[0]
    assert adaptation.kind is ReasoningAdaptationKind.SUPPRESSED
    assert adaptation.message is not None
    assert "reasoning_effort" in adaptation.message
    assert _MODEL in adaptation.message


@pytest.mark.asyncio
async def test_a_422_counts_as_a_rejection():
    """Mistral answers a rejected reasoning field with the pydantic status."""
    provider = _provider()
    create = _FakeCreate(
        [
            _unprocessable(
                {"error": {"message": "Unsupported parameter: reasoning_effort"}}
            )
        ]
    )
    _install(provider, create)

    await provider._create_stream(_body())

    assert len(create.bodies) == 2
    assert "reasoning_effort" not in create.bodies[1]
