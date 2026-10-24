"""7.109.0: InferHub -- the provider, its three doors, the ceiling, the 402, the read-back.

No live call anywhere: every door is driven to its own send and answered by a
fake. The key is a fake of the real shape (``sk-airo-`` + 24 characters).
"""

import dataclasses
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import openai
import pytest

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.application.marketplace import market_snapshot_cache
from my_claude_code.application.model_metadata import (
    ResponseSurface,
    ResponseSurfaceSource,
)
from my_claude_code.config.marketplace_profiles import (
    MarketplaceProfiles,
    parse_profile,
    save_marketplace_profiles,
)
from my_claude_code.config.marketplaces import INFERHUB_MARKETPLACE, snapshot_key
from my_claude_code.config.model_overrides import (
    ModelParameterOverrides,
    save_model_overrides,
)
from my_claude_code.config.provider_catalog import (
    INFERHUB_DEFAULT_BASE,
    PROVIDER_CATALOG,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.diagnostics import redact_sensitive_error_text
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.reported_cost import install_reported_cost
from my_claude_code.core.wire_capture import install_wire_trace
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.model_listing import extract_openai_model_infos
from my_claude_code.providers.openai_chat import (
    GENERIC_OPENAI_PROFILE,
    OPENAI_CHAT_PROFILES,
    create_openai_chat_provider,
)
from my_claude_code.providers.openai_chat.marketplace import (
    summarise_asks,
    summarise_listing_payload,
)
from tests.providers.request_factory import make_messages_request
from tests.providers.support import passthrough_rate_limiter

FAKE_KEY = "sk-airo-" + "a1b2c3d4e5f6g7h8i9j0k1l2"
OPUS = "cc/claude-opus-4-6"
CODEX = "cx/gpt-6-astra"
OTHER = "ag/claude-opus-4-6-thinking"
CEILING = {"x-max-input-price": "1", "x-max-output-price": "5"}


def _provider(provider_id: str = "inferhub") -> Any:
    return create_openai_chat_provider(
        provider_id,
        ProviderConfig(api_key=FAKE_KEY, base_url="https://api.inferhub.invalid/v1"),
        passthrough_rate_limiter(),
    )


def _set_profile(raw: dict[str, Any] | None = None, *, model_row: str = "") -> None:
    profile, problem = parse_profile(
        "cap", raw or {"kind": "pct_of_official", "percent": 20}
    )
    assert profile is not None, problem
    save_marketplace_profiles(MarketplaceProfiles(profiles={"cap": profile}))
    document: dict[str, Any] = {
        "providers": {"inferhub": {"marketplace_profile": "cap"}}
    }
    if model_row:
        document["models"] = {model_row: {"marketplace_profile": None}}
    save_model_overrides(ModelParameterOverrides.from_document(document))
    assert market_snapshot_cache().write(
        INFERHUB_MARKETPLACE.snapshot_entry,
        key=snapshot_key(INFERHUB_MARKETPLACE),
        payload={
            "fetched_at": "2026-10-24T01:07:35Z",
            "models": {
                model: {
                    "official": [5.0, 25.0],
                    "levels_in": [[0.75, 38], [2.5, 3]],
                    "levels_out": [[3.75, 38], [12.5, 3]],
                    "min": [0.75, 3.75],
                }
                for model in (OPUS, CODEX, OTHER)
            },
        },
        computed_at=0.0,
    )


# --------------------------------------------------------------- the provider


def test_the_catalogue_entry_and_settings() -> None:
    descriptor = PROVIDER_CATALOG["inferhub"]
    assert descriptor.display_name == "InferHub"
    assert descriptor.credential_env == "INFERHUB_API_KEY"
    assert (
        descriptor.default_base_url
        == INFERHUB_DEFAULT_BASE
        == "https://api.inferhub.dev/v1"
    )
    assert descriptor.group == "gateway"
    fields = Settings.model_fields
    assert fields["inferhub_base_url"].validation_alias == "INFERHUB_BASE_URL"
    assert fields["inferhub_proxy"].validation_alias == "INFERHUB_PROXY"
    assert (
        fields["inferhub_market_base_url"].validation_alias
        == "INFERHUB_MARKET_BASE_URL"
    )
    assert (
        fields["marketplace_public_market"].validation_alias
        == "MARKETPLACE_PUBLIC_MARKET"
    )


def test_the_profile_is_the_generic_one_plus_what_is_declared() -> None:
    inferhub = OPENAI_CHAT_PROFILES["inferhub"]
    assert inferhub.marketplace is INFERHUB_MARKETPLACE
    assert inferhub.no_information_surface is ResponseSurface.CHAT_COMPLETIONS
    stripped = dataclasses.replace(
        inferhub,
        request_policy=dataclasses.replace(
            inferhub.request_policy,
            provider_name=GENERIC_OPENAI_PROFILE.request_policy.provider_name,
        ),
        response_surfaces=(),
        no_information_surface=None,
        surface_by_upstream=(),
        marketplace=None,
    )
    assert stripped == GENERIC_OPENAI_PROFILE


def test_with_no_profile_the_body_is_b_ais_body() -> None:
    request = make_messages_request(model=OPUS)
    assert _provider()._build_request_body(request) == _provider(
        "bai"
    )._build_request_body(request)


@pytest.mark.parametrize(
    ("model", "surface", "source"),
    [
        (OPUS, ResponseSurface.MESSAGES, ResponseSurfaceSource.PROFILE),
        (CODEX, ResponseSurface.RESPONSES, ResponseSurfaceSource.PROFILE),
        (OTHER, ResponseSurface.CHAT_COMPLETIONS, ResponseSurfaceSource.PROFILE),
        (
            "claude-opus-4-6",
            ResponseSurface.CHAT_COMPLETIONS,
            ResponseSurfaceSource.PROFILE,
        ),
        ("combo/fast", ResponseSurface.CHAT_COMPLETIONS, ResponseSurfaceSource.PROFILE),
    ],
)
def test_each_upstream_has_its_declared_door(
    model: str, surface: ResponseSurface, source: ResponseSurfaceSource
) -> None:
    resolved = _provider().resolved_surface(model)
    assert (resolved.surface, resolved.source) == (surface, source)


def test_a_pin_on_the_models_page_beats_the_declared_door() -> None:
    save_model_overrides(
        ModelParameterOverrides.from_document(
            {"models": {f"inferhub/{OPUS}": {"response_surface": "chat_completions"}}}
        )
    )
    resolved = _provider().resolved_surface(OPUS)
    assert resolved.surface is ResponseSurface.CHAT_COMPLETIONS
    assert resolved.source is ResponseSurfaceSource.OVERRIDE


# ------------------------------------------------------------ the Chat door


class _Stream:
    """An SDK-shaped stream: chunks plus the response that carried them."""

    def __init__(
        self, chunks: list[Any], headers: dict[str, str] | None = None
    ) -> None:
        self._chunks = chunks
        self.response = SimpleNamespace(headers=httpx.Headers(headers or {}))

    def __aiter__(self) -> AsyncIterator[Any]:
        async def run() -> AsyncIterator[Any]:
            for chunk in self._chunks:
                yield chunk

        return run()

    async def close(self) -> None:
        return None


def _chunk(
    content: str | None = None, finish: str | None = None, usage: Any = None
) -> Any:
    if content is None and finish is None:
        return SimpleNamespace(choices=[], usage=usage)
    delta = SimpleNamespace(content=content, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(delta=delta, finish_reason=finish)], usage=usage
    )


def _answer(
    headers: dict[str, str] | None = None, cost: float | None = None
) -> _Stream:
    usage = SimpleNamespace(
        prompt_tokens=10,
        completion_tokens=2,
        model_extra={} if cost is None else {"cost": cost},
    )
    return _Stream([_chunk("hi"), _chunk(finish="stop"), _chunk(usage=usage)], headers)


async def _drain(stream: AsyncIterator[str]) -> list[str]:
    return [event async for event in stream]


def _status_error(status: int, body: dict[str, Any]) -> openai.APIStatusError:
    response = httpx.Response(
        status,
        json=body,
        request=httpx.Request(
            "POST", "https://api.inferhub.invalid/v1/chat/completions"
        ),
    )
    return openai.APIStatusError(
        "refused", response=response, body=body.get("error", body)
    )


@pytest.mark.asyncio
async def test_with_no_profile_the_chat_call_carries_no_header() -> None:
    provider = _provider()
    create = AsyncMock(return_value=_answer())
    with patch.object(provider._client.chat.completions, "create", create):
        await _drain(provider.stream_response(make_messages_request(model=OTHER)))
    assert "extra_headers" not in create.await_args.kwargs


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"kind": "pct_of_official", "percent": 20}, CEILING),
        (
            {"kind": "max_price", "max_input_usd": 0.9},
            {"x-max-input-price": "0.9"},
        ),
        (
            {"kind": "cheapest_level_now"},
            {"x-max-input-price": "0.75", "x-max-output-price": "3.75"},
        ),
        (
            {"kind": "cheapest_level_min_accounts", "min_accounts": 40},
            {"x-max-input-price": "2.5", "x-max-output-price": "12.5"},
        ),
        (
            {"kind": "cheapest_level_now", "exclude_upstreams": ["kiro"]},
            {
                "x-max-input-price": "0.75",
                "x-max-output-price": "3.75",
                "x-ignore-upstream": "kiro",
            },
        ),
    ],
)
@pytest.mark.asyncio
async def test_each_kind_sends_exactly_its_headers_on_chat(
    raw: dict[str, Any], expected: dict[str, str]
) -> None:
    _set_profile(raw)
    provider = _provider()
    create = AsyncMock(return_value=_answer())
    with patch.object(provider._client.chat.completions, "create", create):
        await _drain(provider.stream_response(make_messages_request(model=OTHER)))
    assert create.await_args.kwargs["extra_headers"] == expected


@pytest.mark.asyncio
async def test_a_create_level_retry_resends_the_ceiling() -> None:
    _set_profile()
    provider = _provider()
    rejected = openai.BadRequestError(
        "stream_options is unsupported",
        response=httpx.Response(
            400,
            request=httpx.Request(
                "POST", "https://api.inferhub.invalid/v1/chat/completions"
            ),
        ),
        body={"error": {"message": "stream_options is unsupported"}},
    )
    create = AsyncMock(side_effect=[rejected, _answer()])
    with patch.object(provider._client.chat.completions, "create", create):
        await _drain(provider.stream_response(make_messages_request(model=OTHER)))
    assert create.await_count == 2
    for call in create.await_args_list:
        assert call.kwargs["extra_headers"] == CEILING


@pytest.mark.asyncio
async def test_a_refused_ceiling_never_reaches_the_upstream() -> None:
    _set_profile({"kind": "pct_of_official", "percent": 10})
    provider = _provider()
    request = make_messages_request(model=OTHER)
    with pytest.raises(InvalidRequestError) as preflight:
        provider.preflight_stream(request)
    assert preflight.value.message.startswith("No InferHub offer is under your ceiling")
    create = AsyncMock(return_value=_answer())
    with (
        patch.object(provider._client.chat.completions, "create", create),
        pytest.raises(InvalidRequestError),
    ):
        await _drain(provider.stream_response(request))
    create.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {
            "error": {
                "code": "no_provider_under_bid",
                "message": "no provider under your bid",
                "min_input_per_mtok": 0.75,
                "min_output_per_mtok": 3.75,
            }
        },
        {
            "code": "no_provider_under_bid",
            "message": "no provider under your bid",
            "min_input_per_mtok": 0.75,
            "min_output_per_mtok": 3.75,
        },
    ],
    ids=["error.code", "top-level code"],
)
@pytest.mark.asyncio
async def test_the_402_no_provider_under_bid_is_the_short_400(
    body: dict[str, Any],
) -> None:
    _set_profile()
    provider = _provider()
    create = AsyncMock(side_effect=_status_error(402, body))
    with (
        patch.object(provider._client.chat.completions, "create", create),
        pytest.raises(ExecutionFailure) as raised,
    ):
        await _drain(provider.stream_response(make_messages_request(model=OTHER)))
    failure = raised.value
    assert failure.kind is FailureKind.INVALID_REQUEST
    assert failure.status_code == 400
    assert failure.retryable is False
    assert "it says the cheapest now is $0.75 / $3.75 per Mtok" in failure.message
    assert "profile 'cap': $1 / $5 per Mtok" in failure.message
    create.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_402_for_a_balance_stays_out_of_credits() -> None:
    _set_profile()
    provider = _provider()
    body = {"error": {"code": "payment_required", "message": "Balance too low"}}
    create = AsyncMock(side_effect=_status_error(402, body))
    with (
        patch.object(provider._client.chat.completions, "create", create),
        pytest.raises(ExecutionFailure) as raised,
    ):
        await _drain(provider.stream_response(make_messages_request(model=OTHER)))
    assert raised.value.kind is FailureKind.QUOTA


@pytest.mark.asyncio
async def test_usage_cost_is_the_whole_bill_and_the_headers_are_kept() -> None:
    _set_profile()
    provider = _provider()
    slot = install_reported_cost()
    trace = install_wire_trace()
    headers = {
        "X-Inferhub-Model": OTHER,
        "X-Inferhub-Publisher": "6b652a8e5328",
        "x-inferhub-attempt": "1",
        "Set-Cookie": "never-kept",
    }
    create = AsyncMock(return_value=_answer(headers, cost=0.000123))
    with patch.object(provider._client.chat.completions, "create", create):
        await _drain(provider.stream_response(make_messages_request(model=OTHER)))
    assert slot.total_usd == 0.000123
    record = trace.marketplace[0]
    assert record["charged_usd"] == 0.000123
    assert record["headers"] == {
        "x-inferhub-model": OTHER,
        "x-inferhub-publisher": "6b652a8e5328",
        "x-inferhub-attempt": "1",
    }
    note = trace.requests[0].params["marketplace"]
    assert note["profile"] == "cap" and note["sent"] == CEILING


@pytest.mark.asyncio
async def test_another_hosts_bare_cost_is_still_declined() -> None:
    provider = _provider("bai")
    slot = install_reported_cost()
    trace = install_wire_trace()
    create = AsyncMock(return_value=_answer({"X-Inferhub-Model": "x"}, cost=0.5))
    with patch.object(provider._client.chat.completions, "create", create):
        await _drain(provider.stream_response(make_messages_request(model="m")))
    assert slot.cost_usd == 0.5 and slot.total_usd is None
    assert trace.marketplace == {}


# ------------------------------------------------- the Messages and Responses doors


class _Sent(Exception):
    """The fake send's answer once the request is recorded."""


def _capture_send(client: httpx.AsyncClient, answer: Any = None) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    async def send(request: httpx.Request, **_kwargs: Any) -> Any:
        seen.append(request)
        if answer is None:
            raise _Sent()
        return answer(request)

    vars(client)["send"] = send
    return seen


@pytest.mark.parametrize(("model", "door"), [(OPUS, "messages"), (CODEX, "responses")])
@pytest.mark.asyncio
async def test_the_other_two_doors_send_the_ceiling(model: str, door: str) -> None:
    _set_profile()
    provider = _provider()
    client = (
        provider._messages._provider._client
        if door == "messages"
        else provider._responses._client
    )
    seen = _capture_send(client)
    with pytest.raises(BaseException):
        await _drain(provider.stream_response(make_messages_request(model=model)))
    assert seen, door
    assert seen[0].url.path.endswith(f"/{door}")
    sent = {name.lower(): value for name, value in seen[0].headers.items()}
    assert sent["x-max-input-price"] == "1"
    assert sent["x-max-output-price"] == "5"


@pytest.mark.parametrize(("model", "door"), [(OPUS, "messages"), (CODEX, "responses")])
@pytest.mark.asyncio
async def test_with_no_profile_the_other_doors_carry_no_ceiling(
    model: str, door: str
) -> None:
    provider = _provider()
    client = (
        provider._messages._provider._client
        if door == "messages"
        else provider._responses._client
    )
    seen = _capture_send(client)
    with pytest.raises(BaseException):
        await _drain(provider.stream_response(make_messages_request(model=model)))
    names = {name.lower() for name in seen[0].headers}
    assert not names & {"x-max-input-price", "x-max-output-price", "x-ignore-upstream"}


@pytest.mark.parametrize(("model", "door"), [(OPUS, "messages"), (CODEX, "responses")])
@pytest.mark.asyncio
async def test_the_402_is_the_short_400_on_the_other_doors_too(
    model: str, door: str
) -> None:
    _set_profile()
    provider = _provider()
    client = (
        provider._messages._provider._client
        if door == "messages"
        else provider._responses._client
    )
    body = {"error": {"code": "no_provider_under_bid", "min_input_per_mtok": 0.75}}
    _capture_send(
        client,
        lambda request: httpx.Response(402, json=body, request=request),
    )
    with pytest.raises(ExecutionFailure) as raised:
        await _drain(provider.stream_response(make_messages_request(model=model)))
    assert raised.value.kind is FailureKind.INVALID_REQUEST
    assert raised.value.status_code == 400
    assert "$0.75 / uncapped per Mtok" in raised.value.message


@pytest.mark.parametrize(("model", "door"), [(OPUS, "messages"), (CODEX, "responses")])
@pytest.mark.asyncio
async def test_a_balance_402_on_the_other_doors_stays_out_of_credits(
    model: str, door: str
) -> None:
    _set_profile()
    provider = _provider()
    client = (
        provider._messages._provider._client
        if door == "messages"
        else provider._responses._client
    )
    body = {"error": {"code": "payment_required", "message": "Balance too low"}}
    _capture_send(
        client, lambda request: httpx.Response(402, json=body, request=request)
    )
    with pytest.raises(ExecutionFailure) as raised:
        await _drain(provider.stream_response(make_messages_request(model=model)))
    assert raised.value.kind is FailureKind.QUOTA


# ------------------------------------------------------------- the listing


def _row(asks_in: Any, asks_out: Any, model: str = OTHER) -> dict[str, Any]:
    return {
        "id": model,
        "object": "model",
        "owned_by": "ag",
        "input_token_limit": 200000,
        "max_output_tokens": 64000,
        "pricing": {
            "official_in": 5,
            "official_out": 25,
            "asks_in": asks_in,
            "asks_out": asks_out,
            "min_ask_in": 0.005,
            "min_ask_out": 0.025,
        },
    }


def test_two_thousand_per_account_asks_become_a_few_levels() -> None:
    asks_in = [0.005] * 1052 + [0.025] * 900 + [2.5] * 343
    asks_out = [0.025] * 1052 + [0.125] * 900 + [12.5] * 343
    payload = {"object": "list", "data": [_row(asks_in, asks_out)]}
    summarised = summarise_listing_payload(
        payload, INFERHUB_MARKETPLACE, provider_name="INFERHUB"
    )
    pricing = summarised["data"][0]["pricing"]
    assert pricing["asks_in"] == {
        "summarised_from": 2295,
        "levels": [[0.005, 1052], [0.025, 900], [2.5, 343]],
    }
    infos = extract_openai_model_infos(summarised, provider_name="INFERHUB")
    (info,) = infos
    assert info.published_row is not None
    assert len(info.published_row) < 600
    assert json.loads(info.published_row)["pricing"]["asks_out"]["levels"][0] == [
        0.025,
        1052,
    ]
    unsummarised = extract_openai_model_infos(payload, provider_name="INFERHUB")
    assert len(next(iter(unsummarised)).published_row or "") > 10_000


def test_a_per_level_shape_is_kept_as_levels() -> None:
    assert summarise_asks([[0.75, 38], [2.5, 3], [0.75, 2]]) == {
        "summarised_from": 3,
        "levels": [[0.75, 40], [2.5, 3]],
    }
    assert summarise_asks([{"price": 1.0, "count": 4}, "junk"]) == {
        "summarised_from": 2,
        "levels": [[1.0, 4]],
    }
    assert summarise_asks(None) is None


@pytest.mark.asyncio
async def test_the_provider_listing_summarises_before_reading() -> None:
    provider = _provider()
    asks = [0.75] * 2000
    payload = {"object": "list", "data": [_row(asks, asks)]}
    with patch.object(
        provider, "_list_models_payload", AsyncMock(return_value=payload)
    ):
        (info,) = await provider.list_model_infos()
    assert json.loads(info.published_row or "{}")["pricing"]["asks_in"] == {
        "summarised_from": 2000,
        "levels": [[0.75, 2000]],
    }


def test_the_fake_key_shape_is_redacted() -> None:
    """The redactor's ``sk-[a-z0-9._-]{8,}`` catches an InferHub key bare."""
    redacted = redact_sensitive_error_text(f"the key was {FAKE_KEY} here")
    assert FAKE_KEY not in redacted
    assert "sk-airo" not in redacted
