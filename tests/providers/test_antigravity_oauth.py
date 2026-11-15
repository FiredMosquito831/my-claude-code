"""The Antigravity P1 scaffold, proven against a loopback fake upstream.

Every test here is a sentence about one seam the brief names: the store, the
two-account pool, one well-formed turn, the refusal pairs, the usage
invariant, terminality without a ``finishReason``, the listing filter, the
per-model cap, and the identity set. **No test contacts the network** — the
fake is on 127.0.0.1 and the token endpoint is monkeypatched to it.
"""

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.config.admin.status import provider_config_status
from my_claude_code.config.constants import (
    ANTIGRAVITY_CLIENT_IDENTITY_DEFAULT,
    ANTIGRAVITY_CLIENT_PROFILE_DEFAULT,
    ANTIGRAVITY_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
)
from my_claude_code.config.paths import antigravity_oauth_store_path
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.providers import failure_policy
from my_claude_code.providers.antigravity_oauth import (
    constants,
    credentials,
    identity,
    listing,
    refusals,
)
from my_claude_code.providers.antigravity_oauth import (
    provider as provider_module,
)
from my_claude_code.providers.antigravity_oauth.provider import (
    AntigravityOAuthProvider,
    usage_from_metadata,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.credential_rotation import CREDENTIAL_SHAPED_KINDS
from my_claude_code.providers.runtime import factory
from my_claude_code.providers.runtime.discovery import (
    model_list_provider_ids_for_settings,
)
from tests.providers.antigravity_fake_upstream import (
    DEFAULT_TEXT_TURN,
    FAKE_ACCESS_TOKEN,
    FAKE_ACCESS_TOKEN_2,
    FAKE_REFRESH_TOKEN,
    MINIMAL_USAGE,
    ROSTER,
    SIX_COUNTER_USAGE,
    TOS_BODY,
    UNSUPPORTED_CLIENT_BODY,
    AntigravityFakeUpstream,
    json_turn,
    sse,
    text_frame,
    tool_frame,
    usage_frame,
)

BASELINE = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "antigravity_identity_baseline.json"
)


def _fake(**kwargs: Any) -> AntigravityFakeUpstream:
    return AntigravityFakeUpstream(**kwargs)


def _request(
    text: str = "Hello", *, model: str = "gemini-3.8-flash"
) -> MessagesRequest:
    return MessagesRequest(
        model=model,
        max_tokens=64,
        messages=[Message(role="user", content=text)],
    )


def _provider(
    fake: AntigravityFakeUpstream,
    *,
    access_token: str = FAKE_ACCESS_TOKEN,
    store_path: Path | None = None,
    account_id: str = "",
    **kwargs: Any,
) -> AntigravityOAuthProvider:
    return AntigravityOAuthProvider(
        ProviderConfig(
            api_key="",
            base_url=fake.url,
            http_read_timeout=30.0,
            http_connect_timeout=5.0,
            http_write_timeout=5.0,
        ),
        access_token=access_token,
        store_path=store_path,
        account_id=account_id,
        **kwargs,
    )


async def _events(provider: AntigravityOAuthProvider) -> list[dict[str, Any]]:
    """Collect the provider's SSE events as parsed ``(name, data)`` pairs."""

    parsed: list[dict[str, Any]] = []
    async for frame_text in provider.stream_response(_request()):
        name = ""
        data: dict[str, Any] = {}
        for line in frame_text.splitlines():
            if line.startswith("event:"):
                name = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:") :].strip())
        parsed.append({"event": name, "data": data})
    return parsed


# ---------------------------------------------------------------------------
# 1. the store
# ---------------------------------------------------------------------------


def test_the_store_round_trips_two_accounts_chatgpt_shaped(tmp_path: Path) -> None:
    path = tmp_path / "antigravity-oauth.json"
    first = credentials.OAuthTokens(
        access_token="first-token-shape",
        refresh_token=FAKE_REFRESH_TOKEN,
        expires_at=1_800_000_000,
        project_id="default-cli-project",
    )
    second = credentials.OAuthTokens(
        access_token="second-token-shape",
        refresh_token="1//second-refresh",
        expires_at=1_800_000_100,
    )
    credentials.save_accounts(
        [
            credentials.AccountRecord(id="acct-1", tokens=first),
            credentials.AccountRecord(id="acct-2", tokens=second, ordinal=2),
        ],
        path=path,
    )

    document = json.loads(path.read_text(encoding="utf-8"))
    assert set(document) == {"version", "tokens", "accounts_version", "accounts"}
    assert document["version"] == 1
    assert document["accounts_version"] == 1
    assert document["tokens"]["access_token"] == "first-token-shape"

    records = credentials.load_accounts(path=path, migrate=False)
    assert [record.id for record in records] == ["acct-1", "acct-2"]
    assert records[0].tokens.project_id == "default-cli-project"
    assert records[1].tokens.expires_at == 1_800_000_100


def test_a_refresh_updates_the_named_account_in_place(tmp_path: Path) -> None:
    path = tmp_path / "antigravity-oauth.json"
    credentials.save_accounts(
        [
            credentials.AccountRecord(
                id="acct-1",
                tokens=credentials.OAuthTokens(access_token="one"),
                added_at="2026-11-15T00:00:00Z",
            ),
            credentials.AccountRecord(
                id="acct-2",
                tokens=credentials.OAuthTokens(access_token="two"),
                added_at="2026-11-15T00:00:01Z",
                ordinal=2,
            ),
        ],
        path=path,
    )

    credentials.add_or_update_account(
        credentials.OAuthTokens(access_token="two-refreshed"),
        account_id="acct-2",
        path=path,
    )

    records = credentials.load_accounts(path=path, migrate=False)
    # Nothing appended, nothing duplicated, the name and add date kept.
    assert [record.id for record in records] == ["acct-1", "acct-2"]
    assert records[1].tokens.access_token == "two-refreshed"
    assert records[1].added_at == "2026-11-15T00:00:01Z"


# ---------------------------------------------------------------------------
# 2. two accounts = two pool slots
# ---------------------------------------------------------------------------


def test_two_stored_accounts_are_two_pool_slots() -> None:
    store = antigravity_oauth_store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    credentials.save_accounts(
        [
            credentials.AccountRecord(id="slot-a", tokens=credentials.OAuthTokens("a")),
            credentials.AccountRecord(
                id="slot-b", tokens=credentials.OAuthTokens("b"), ordinal=2
            ),
        ],
        path=store,
    )

    assert factory.oauth_account_ids("antigravity_oauth") == ["slot-a", "slot-b"]
    assert factory._oauth_account_pool("antigravity_oauth") is not None


def test_one_stored_account_keeps_todays_single_leaf_shape() -> None:
    store = antigravity_oauth_store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    credentials.save_accounts(
        [credentials.AccountRecord(id="only", tokens=credentials.OAuthTokens("a"))],
        path=store,
    )

    assert factory.oauth_account_ids("antigravity_oauth") == ["only"]
    assert factory._oauth_account_pool("antigravity_oauth") is None


# ---------------------------------------------------------------------------
# 3. one well-formed turn, and the envelope it came from
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_well_formed_turn() -> None:
    fake = _fake(
        turns=[
            sse(
                text_frame("Hello"),
                text_frame(" from the fake"),
                usage_frame(MINIMAL_USAGE),
            )
        ]
    )
    provider = _provider(fake)
    try:
        events = await _events(provider)
    finally:
        await provider.cleanup()
        fake.close()

    assert events[0]["event"] == "message_start"
    assert events[0]["data"]["message"]["model"] == "gemini-3.8-flash"
    text = "".join(
        event["data"]["delta"]["text"]
        for event in events
        if event["data"].get("delta", {}).get("type") == "text_delta"
    )
    assert text == "Hello from the fake"
    assert events[-2]["data"]["delta"]["stop_reason"] == "end_turn"
    assert events[-2]["data"]["usage"] == {"input_tokens": 12, "output_tokens": 3}
    assert events[-1]["event"] == "message_stop"

    request = fake.last_request(":streamGenerateContent")
    assert request.query == {"alt": ["sse"]}
    assert set(request.body) == {
        "project",
        "model",
        "request",
        "requestType",
        "userAgent",
        "requestId",
    }
    assert request.body["project"] == "default-cli-project"
    assert request.body["requestType"] == "agent"
    assert request.body["userAgent"] == "antigravity"
    assert re.fullmatch(r"agent-[0-9a-f-]{36}", request.body["requestId"])
    inner = request.body["request"]
    assert inner["sessionId"].startswith("antigravity-")
    assert "safetySettings" not in inner
    assert inner["contents"] == [{"role": "user", "parts": [{"text": "Hello"}]}]


@pytest.mark.asyncio
async def test_the_session_id_is_derived_from_the_whole_first_turn() -> None:
    """Two conversations with different openings are two sessions."""

    first = provider_module.derived_session_id(
        "acct", "a long shared preamble\nand the real ask"
    )
    second = provider_module.derived_session_id(
        "acct", "a long shared preamble\nand a different ask"
    )
    assert first != second
    assert first == provider_module.derived_session_id(
        "acct", "a long shared preamble\nand the real ask"
    )


# ---------------------------------------------------------------------------
# 4. F15 — the identity is proven by absence too
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_declared_headers_and_none_of_the_forbidden_ones() -> None:
    fake = _fake()
    provider = _provider(fake)
    try:
        await _events(provider)
    finally:
        await provider.cleanup()
        fake.close()

    request = fake.last_request(":streamGenerateContent")
    names = request.header_names()
    assert {"authorization", "content-type", "user-agent", "accept-encoding"} <= names
    assert (
        names
        & {
            "client-metadata",
            "x-goog-api-client",
            "x-goog-user-project",
            "x-goog-api-key",
            "accept",
        }
        == set()
    )
    assert request.headers["authorization"] == f"Bearer {FAKE_ACCESS_TOKEN}"
    assert request.headers["content-type"] == "application/json"
    assert request.headers["accept-encoding"] == "gzip"
    assert request.headers["user-agent"] == identity.CLI_PROFILE.user_agent()
    # The fake saw nothing it refuses, which is the same claim from its side.
    assert fake.violations == []


# ---------------------------------------------------------------------------
# 5. the refusal pairs — the difference IS the proof
# ---------------------------------------------------------------------------


def _status_error(status: int, payload: Any) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://127.0.0.1/v1internal:streamGenerateContent")
    response = httpx.Response(status, json=payload, request=request)
    return httpx.HTTPStatusError("upstream refused", request=request, response=response)


def test_the_tos_body_is_free_tier_through_the_override_and_auth_generically() -> None:
    generic = failure_policy.classify_provider_failure(
        _status_error(403, TOS_BODY),
        provider_name="antigravity_oauth",
        read_timeout_s=30.0,
        request_id=None,
        mark_rate_limited=lambda _seconds: None,
    )
    assert generic.kind in (FailureKind.AUTHENTICATION, FailureKind.PERMISSION)

    refusal = refusals.refusal_from_error_response(403, TOS_BODY)
    assert refusal is not None
    override = refusals.classify_antigravity_failure(refusal)
    assert override is not None
    assert override.kind == FailureKind.FREE_TIER
    assert override.status_code == 402
    assert override.retryable is False
    # The zero-charge proof: FREE_TIER is not a credential-shaped kind.
    assert override.kind not in CREDENTIAL_SHAPED_KINDS
    assert "TOS_VIOLATION" in override.message
    assert "https://forms.gle/fake-appeal-url" in override.message
    for banned in ("top up", "wait", "check the key", "try again"):
        assert banned not in override.message.lower()


def test_a_non_tos_403_is_not_a_refusal() -> None:
    body = {
        "error": {
            "code": 403,
            "status": "PERMISSION_DENIED",
            "message": "The caller does not have permission.",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
                    "reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                }
            ],
        }
    }
    assert refusals.refusal_from_error_response(403, body) is None


def test_a_200_without_current_tier_and_unsupported_client_is_a_refusal() -> None:
    """M1: delete the ``currentTier``-absent check and this fails."""

    refusal = refusals.refusal_from_load_code_assist(UNSUPPORTED_CLIENT_BODY)
    assert refusal is not None
    assert refusal.kind == refusals.REFUSAL_UNSUPPORTED_CLIENT
    # A 200 that merely lacks a project is NOT a refusal — it triggers
    # onboarding, so the absence check alone must not fire.
    assert (
        refusals.refusal_from_load_code_assist({"allowedTiers": [{"id": "free-tier"}]})
        is None
    )
    # And a healthy answer WITH a current tier is not one either, even if a
    # stale ineligible entry is still in the payload: the absence of
    # ``currentTier`` is what makes UNSUPPORTED_CLIENT a refusal.
    assert (
        refusals.refusal_from_load_code_assist(
            {
                "currentTier": {"id": "free-tier"},
                "ineligibleTiers": [{"reasonCode": "UNSUPPORTED_CLIENT"}],
            }
        )
        is None
    )


@pytest.mark.asyncio
async def test_a_tos_403_through_the_provider_is_free_tier_and_untouched() -> None:
    fake = _fake(turns=[json_turn(TOS_BODY, status=403)])
    provider = _provider(fake)
    try:
        with pytest.raises(ExecutionFailure) as caught:
            await _events(provider)
    finally:
        await provider.cleanup()
        fake.close()

    failure = caught.value
    assert isinstance(failure, Exception)
    assert failure.kind == FailureKind.FREE_TIER
    assert failure.status_code == 402
    assert failure.retryable is False


# ---------------------------------------------------------------------------
# 6. refresh: a 429 keeps the token, a 400 + OAuth body retires one of two
# ---------------------------------------------------------------------------


def _stored_account(tokens: credentials.OAuthTokens, path: Path) -> None:
    credentials.save_accounts(
        [credentials.AccountRecord(id="acct-1", tokens=tokens)], path=path
    )


@pytest.mark.asyncio
async def test_a_429_on_refresh_keeps_the_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake(
        token_turns=[
            json_turn(
                {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED"}},
                status=429,
                headers={"Retry-After": "3"},
            )
        ]
    )
    monkeypatch.setattr(credentials, "TOKEN_URL", f"{fake.url}/token")
    path = tmp_path / "antigravity-oauth.json"
    stored = credentials.OAuthTokens(
        access_token="stale", refresh_token=FAKE_REFRESH_TOKEN
    )
    _stored_account(stored, path)
    try:
        with pytest.raises(credentials.AntigravityRefreshUnavailable) as caught:
            await credentials.refresh_tokens(stored, account_id="acct-1", path=path)
    finally:
        fake.close()

    assert caught.value.definitive is False
    records = credentials.load_accounts(path=path, migrate=False)
    assert len(records) == 1
    assert records[0].tokens.access_token == "stale"


@pytest.mark.asyncio
async def test_a_400_with_an_oauth_body_retires_exactly_one_of_two(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake(token_turns=[json_turn({"error": "invalid_grant"}, status=400)])
    monkeypatch.setattr(credentials, "TOKEN_URL", f"{fake.url}/token")
    path = tmp_path / "antigravity-oauth.json"
    credentials.save_accounts(
        [
            credentials.AccountRecord(
                id="acct-1",
                tokens=credentials.OAuthTokens(
                    access_token="one", refresh_token=FAKE_REFRESH_TOKEN
                ),
            ),
            credentials.AccountRecord(
                id="acct-2",
                tokens=credentials.OAuthTokens(
                    access_token="two", refresh_token="1//second"
                ),
                ordinal=2,
            ),
        ],
        path=path,
    )
    stored = credentials.load_accounts(path=path, migrate=False)[0]
    try:
        with pytest.raises(credentials.AntigravityRefreshRejected):
            await credentials.refresh_tokens(
                stored.tokens, account_id="acct-1", path=path
            )
    finally:
        fake.close()

    remaining = credentials.load_accounts(path=path, migrate=False)
    assert [record.id for record in remaining] == ["acct-2"]
    # Renamed, never deleted: the retired record survives on disk.
    dead = list(path.parent.glob(f"{path.name}.dead-*"))
    assert len(dead) == 1
    assert json.loads(dead[0].read_text(encoding="utf-8"))["id"] == "acct-1"


@pytest.mark.asyncio
async def test_a_401_refreshes_once_and_retries_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake(
        turns=[
            json_turn(
                {"error": {"code": 401, "status": "UNAUTHENTICATED"}}, status=401
            ),
            DEFAULT_TEXT_TURN,
        ],
        token_turns=[
            json_turn({"access_token": FAKE_ACCESS_TOKEN_2, "expires_in": 3599})
        ],
    )
    monkeypatch.setattr(credentials, "TOKEN_URL", f"{fake.url}/token")
    path = tmp_path / "antigravity-oauth.json"
    _stored_account(
        credentials.OAuthTokens(access_token="stale", refresh_token=FAKE_REFRESH_TOKEN),
        path,
    )
    provider = _provider(fake, access_token="", store_path=path, account_id="acct-1")
    try:
        events = await _events(provider)
    finally:
        await provider.cleanup()
        fake.close()

    assert any(event["event"] == "message_start" for event in events)
    stream_requests = fake.requests_for(":streamGenerateContent")
    assert len(stream_requests) == 2
    assert stream_requests[0].headers["authorization"] == "Bearer stale"
    assert (
        stream_requests[1].headers["authorization"] == f"Bearer {FAKE_ACCESS_TOKEN_2}"
    )
    assert len(fake.requests_for("/token")) == 1
    # The refreshed credential is stored.
    assert (
        credentials.load_accounts(path=path, migrate=False)[0].tokens.access_token
        == FAKE_ACCESS_TOKEN_2
    )


def test_the_refresh_classification_matches_the_other_two_providers() -> None:
    from my_claude_code.providers.anthropic_oauth.credentials import (
        DEFINITIVE_REFRESH_STATUSES as ANTHROPIC_DEFINITIVE,
    )
    from my_claude_code.providers.chatgpt_oauth.credentials import (
        DEFINITIVE_REFRESH_STATUSES as CHATGPT_DEFINITIVE,
    )

    assert (
        credentials.DEFINITIVE_REFRESH_STATUSES
        == ANTHROPIC_DEFINITIVE
        == CHATGPT_DEFINITIVE
        == frozenset({400, 401, 403})
    )
    transient = credentials.classify_refresh_failure(
        httpx.Response(429, json={"error": "rate_limited"})
    )
    assert transient.definitive is False
    definite = credentials.classify_refresh_failure(
        httpx.Response(400, json={"error": "invalid_grant"})
    )
    assert definite.definitive is True
    # A definitive status WITHOUT a parseable OAuth body is still transient.
    assert (
        credentials.classify_refresh_failure(
            httpx.Response(403, text="error code: 1010")
        ).definitive
        is False
    )


# ---------------------------------------------------------------------------
# 7. no credential -> the honest 503, never a dial
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_credential_is_unavailable_and_never_dials() -> None:
    fake = _fake()
    provider = _provider(fake, access_token="")
    try:
        with pytest.raises(ExecutionFailure) as preflight_error:
            provider.preflight_stream(_request())
        with pytest.raises(ExecutionFailure) as caught:
            await _events(provider)
    finally:
        await provider.cleanup()
        fake.close()

    for failure in (preflight_error.value, caught.value):
        assert failure.kind == FailureKind.UNAVAILABLE
        assert failure.status_code == 503
    assert fake.requests == []


# ---------------------------------------------------------------------------
# 8. listing: 4 visible, the other 5 still routable
# ---------------------------------------------------------------------------


def test_the_listing_filters_exactly_four_visible_five_routable_only() -> None:
    parse = listing.parse_available_models({"models": ROSTER})

    visible = parse.listed_ids()
    assert visible == {
        "gemini-3.8-flash",
        "gemini-3.7-flash-high",
        "claude-sonnet-4-6",
        "gpt-oss-120b-medium",
    }
    assert parse.routable_ids() == set(ROSTER)
    assert len(parse.routable_ids() - visible) == 5
    by_id = parse.by_id()
    assert by_id["gemini-3.8-flash-disabled"].hidden_reason == listing.HIDE_DISABLED
    assert by_id["gemini-2.5-pro"].hidden_reason == listing.HIDE_OLD_GEMINI
    assert by_id["gemini-3.0-flash-preview"].hidden_reason == listing.HIDE_OLD_GEMINI


@pytest.mark.asyncio
async def test_the_provider_lists_the_roster_and_keeps_hidden_ids_routable() -> None:
    fake = _fake()
    provider = _provider(fake)
    try:
        ids = await provider.list_model_ids()
        infos = await provider.list_model_infos()
    finally:
        await provider.cleanup()
        fake.close()

    assert ids == {
        "gemini-3.8-flash",
        "gemini-3.7-flash-high",
        "claude-sonnet-4-6",
        "gpt-oss-120b-medium",
    }
    assert {info.model_id for info in infos} == ids
    # A hidden id is still accepted by the request path: the provider does not
    # gate on the listing.
    provider.preflight_stream(_request(model="gemini-2.5-pro"))


def test_an_unreadable_row_is_skipped_never_dropped_with_the_roster() -> None:
    parse = listing.parse_available_models(
        {"models": {"gemini-3.8-flash": ROSTER["gemini-3.8-flash"], "bad": "nope"}}
    )
    assert parse.listed_ids() == {"gemini-3.8-flash"}
    assert parse.skipped == ("bad",)


# ---------------------------------------------------------------------------
# 9. one malformed body is not a roster
# ---------------------------------------------------------------------------


def test_a_response_with_no_models_object_is_an_empty_parse() -> None:
    assert listing.parse_available_models({}).entries == ()
    assert listing.parse_available_models(None).entries == ()


# ---------------------------------------------------------------------------
# 10. per-model caps, from the roster (M5)
# ---------------------------------------------------------------------------


def test_each_model_carries_its_own_published_cap() -> None:
    parse = listing.parse_available_models({"models": ROSTER})

    assert listing.output_cap_for(parse, "gemini-3.8-flash") == 65536
    assert listing.output_cap_for(parse, "claude-sonnet-4-6") == 65535
    assert listing.output_cap_for(parse, "gemini-3.7-flash-high") == 65536
    # The model with no published cap answers None — the ladder decides, never
    # a constant invented here.
    assert listing.output_cap_for(parse, "gpt-oss-120b-medium") is None
    assert listing.output_cap_for(parse, "not-a-model") is None
    caps = {
        info.model_id: info.max_output_tokens for info in listing.model_infos(parse)
    }
    assert caps == {
        "gemini-3.8-flash": 65536,
        "gemini-3.7-flash-high": 65536,
        "claude-sonnet-4-6": 65535,
        "gpt-oss-120b-medium": None,
    }


def test_quota_is_kept_and_an_absent_quota_is_not_zero() -> None:
    parse = listing.parse_available_models({"models": ROSTER})
    by_id = parse.by_id()
    assert by_id["gemini-3.8-flash"].quota_info["remainingFraction"] == 0.5
    assert by_id["gemini-3.7-flash-high"].quota_info == {}


# ---------------------------------------------------------------------------
# 11. usage: six counters, disjoint, toolUsePrompt recorded never summed
# ---------------------------------------------------------------------------


def test_the_six_counter_usage_mapping_closes_the_arithmetic() -> None:
    usage = usage_from_metadata(SIX_COUNTER_USAGE)
    assert usage is not None
    assert usage.input_tokens == 153609 - 1000
    assert usage.output_tokens == 17 + 94
    assert usage.invariant_holds() is True
    assert usage.side_record() == {
        "tool_use_prompt_tokens": 500,
        "total_tokens": 153720,
    }
    # The Anthropic fields never carry the side counters.
    anthropic = usage.anthropic_usage(include_cache=True)
    assert anthropic == {
        "input_tokens": 152609,
        "output_tokens": 111,
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 0,
    }


def test_absent_counters_stay_absent_and_are_never_zero() -> None:
    usage = usage_from_metadata({"promptTokenCount": 10})
    assert usage is not None
    assert usage.input_tokens == 10
    assert usage.output_tokens is None
    assert "output_tokens" not in usage.anthropic_usage(include_cache=False)


@pytest.mark.asyncio
async def test_a_tool_using_turn_reports_the_disjoint_usage_and_the_tool_stop() -> None:
    """F5: the captured tool chunk — a ``functionCall``, a usage block, no reason."""

    fake = _fake(turns=[sse(tool_frame(usage=SIX_COUNTER_USAGE))])
    provider = _provider(fake)
    try:
        events = await _events(provider)
    finally:
        await provider.cleanup()
        fake.close()

    tool_start = next(
        event
        for event in events
        if event["event"] == "content_block_start"
        and event["data"]["content_block"]["type"] == "tool_use"
    )
    assert tool_start["data"]["content_block"]["id"] == "call_524881"
    assert tool_start["data"]["content_block"]["name"] == "get_weather"
    delta = next(
        event
        for event in events
        if event["data"].get("delta", {}).get("type") == "input_json_delta"
    )
    assert json.loads(delta["data"]["delta"]["partial_json"]) == {"city": "Paris"}
    final = events[-2]["data"]
    # Terminality without a finishReason: the body ended, and a functionCall
    # alone is what makes this a tool stop.
    assert final["delta"]["stop_reason"] == "tool_use"
    assert final["usage"]["output_tokens"] == 111
    assert final["usage"]["input_tokens"] == 152609
    assert provider.stop_reason_details == []


# ---------------------------------------------------------------------------
# 12. terminality and honest finish reasons (M3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_stream_with_no_finish_reason_at_all_still_terminates() -> None:
    fake = _fake(
        turns=[
            sse(
                text_frame("partial"),
                usage_frame(MINIMAL_USAGE, finish_reason=None),
            )
        ]
    )
    provider = _provider(fake)
    try:
        events = await _events(provider)
    finally:
        await provider.cleanup()
        fake.close()

    assert events[-1]["event"] == "message_stop"
    assert events[-2]["data"]["delta"]["stop_reason"] == "end_turn"


@pytest.mark.asyncio
async def test_a_blocklist_finish_reason_is_a_refusal_never_a_clean_stop() -> None:
    """M3: mapping an unknown reason to STOP makes this fail."""

    fake = _fake(
        turns=[
            sse(
                text_frame("blocked"),
                usage_frame(MINIMAL_USAGE, finish_reason="BLOCKLIST"),
            )
        ]
    )
    provider = _provider(fake)
    try:
        events = await _events(provider)
    finally:
        await provider.cleanup()
        fake.close()

    assert events[-2]["data"]["delta"]["stop_reason"] == "refusal"
    assert provider.stop_reason_details == [
        {"finishReason": "BLOCKLIST", "stop_reason": "refusal"}
    ]


@pytest.mark.asyncio
async def test_an_unmapped_finish_reason_is_recorded_with_its_raw_value() -> None:
    fake = _fake(
        turns=[
            sse(
                text_frame("odd"),
                usage_frame(MINIMAL_USAGE, finish_reason="MALFORMED_FUNCTION_CALL"),
            )
        ]
    )
    provider = _provider(fake)
    try:
        events = await _events(provider)
    finally:
        await provider.cleanup()
        fake.close()

    assert events[-2]["data"]["delta"]["stop_reason"] == "end_turn"
    assert provider.stop_reason_details == [
        {"finishReason": "MALFORMED_FUNCTION_CALL", "stop_reason": "end_turn"}
    ]


@pytest.mark.asyncio
async def test_max_tokens_maps_to_max_tokens() -> None:
    fake = _fake(
        turns=[
            sse(
                text_frame("cut"),
                usage_frame(MINIMAL_USAGE, finish_reason="MAX_TOKENS"),
            )
        ]
    )
    provider = _provider(fake)
    try:
        events = await _events(provider)
    finally:
        await provider.cleanup()
        fake.close()

    assert events[-2]["data"]["delta"]["stop_reason"] == "max_tokens"


@pytest.mark.asyncio
async def test_a_zero_output_stream_is_a_classified_failure() -> None:
    fake = _fake(turns=[sse(usage_frame(MINIMAL_USAGE))])
    provider = _provider(fake)
    try:
        with pytest.raises(ExecutionFailure) as caught:
            await _events(provider)
    finally:
        await provider.cleanup()
        fake.close()

    assert caught.value.kind == FailureKind.OVERLOADED
    assert caught.value.status_code == 502


@pytest.mark.asyncio
async def test_an_in_stream_error_frame_is_an_error_not_an_empty_answer() -> None:
    fake = _fake(stream_error_after=1)
    provider = _provider(fake)
    try:
        with pytest.raises(ExecutionFailure) as caught:
            await _events(provider)
    finally:
        await provider.cleanup()
        fake.close()

    assert caught.value.kind == FailureKind.UPSTREAM


# ---------------------------------------------------------------------------
# 13. identity: the dated profile, the defaults, the switch
# ---------------------------------------------------------------------------


def test_the_cli_profile_is_the_default_and_sends_no_accept() -> None:
    """M6 flips the default to mcc; M7 adds Accept. Both fail here."""

    assert ANTIGRAVITY_CLIENT_IDENTITY_DEFAULT == "antigravity"
    assert constants.CLIENT_IDENTITY_DEFAULT == "antigravity"
    assert identity.client_identity("") == "antigravity"
    profile = identity.active_profile()
    assert profile.flavour == "cli"
    assert profile.accept_header is None
    assert profile.metadata_shape == "string"
    headers = identity.request_headers(profile, "token")
    assert "accept" not in headers
    assert set(headers) == set(identity.HEADERS_PRESENT)


def test_the_user_agent_is_asserted_by_grammar_with_the_dated_constant() -> None:
    profile = identity.active_profile()
    assert profile.version == constants.CLI_VERSION == "1.4.4"
    user_agent = profile.user_agent()
    pattern = re.compile(
        r"^antigravity/cli/1\.4\.4 \(aidev_client; os_type=(windows|linux|darwin); "
        r"arch=(amd64|arm64); cl=\d+; auth_method=consumer\)$"
    )
    assert pattern.fullmatch(user_agent), user_agent


def test_the_metadata_shapes_are_the_cli_string_and_the_snake_case_onboard() -> None:
    profile = identity.active_profile()
    assert profile.metadata_for("loadCodeAssist") == {
        "metadata": {"ideType": "ANTIGRAVITY"}
    }
    onboard = profile.metadata_for("onboardUser")["metadata"]
    assert onboard["ide_type"] == "ANTIGRAVITY"
    assert "pluginType" not in str(profile.metadata_for("loadCodeAssist"))


def test_mcc_identity_moves_only_the_user_agent() -> None:
    profile = identity.active_profile(identity="mcc")
    assert profile.accept_header is None
    assert profile.metadata_shape == "string"
    assert identity.user_agent_for(profile, "mcc").startswith("my-claude-code/")
    headers = identity.request_headers(profile, "token", identity="mcc")
    assert headers["user-agent"].startswith("my-claude-code/")
    assert "accept" not in headers


def test_the_ide_profile_is_declared_but_never_the_default() -> None:
    assert identity.profile_named("").flavour == "cli"
    ide = identity.profile_named("ide")
    assert ide.flavour == "ide"
    assert ide.accept_header == "text/event-stream"
    assert ide.metadata_shape == "numeric"
    assert ANTIGRAVITY_CLIENT_PROFILE_DEFAULT == "cli"


def test_no_forbidden_header_name_is_ever_built() -> None:
    headers = identity.request_headers(identity.active_profile(), "token")
    assert not any(identity.is_forbidden_header(name) for name in headers)
    assert identity.is_forbidden_header("Client-Metadata")
    assert identity.is_forbidden_header("x-goog-api-client")
    assert identity.is_forbidden_header("x-stainless-lang")


# ---------------------------------------------------------------------------
# 14. the identity baseline file
# ---------------------------------------------------------------------------


def test_the_identity_baseline_pins_the_cli_profile() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    profile = identity.active_profile()

    assert baseline["identity_id"] == "antigravity-cli"
    assert baseline["verified_on"] == profile.verified_on
    # M6: flipping the default identity to ``mcc`` fails here.
    assert baseline["default_client_identity"] == identity.client_identity("")
    assert baseline["default_client_identity"] == "antigravity"
    assert baseline["user_agent"]["prefix"] == "antigravity/cli/"
    assert baseline["user_agent"]["version"] == constants.CLI_VERSION
    assert baseline["accept_header"] is None
    assert baseline["metadata_shape"] == "string"
    # The client pair belongs to the sign-in surface (P2a); P1 records why, so
    # a later reader does not "fix" the gap by pasting a literal here.
    assert baseline["oauth_client_id"] is None
    assert "push protection" in baseline["oauth_client_id_note"]
    assert set(baseline["headers_present"]) == set(identity.HEADERS_PRESENT)
    assert set(baseline["headers_absent"]) >= {
        "client-metadata",
        "x-goog-api-client",
        "x-goog-user-project",
        "x-goog-api-key",
    }
    assert baseline["body_metadata"] == {"loadCodeAssist": {"ideType": "ANTIGRAVITY"}}
    # The UA matches the pinned grammar with the pinned version.
    regex = re.compile(baseline["user_agent"]["tail_regex"])
    user_agent = profile.user_agent()
    assert user_agent.startswith(
        f"{baseline['user_agent']['prefix']}{baseline['user_agent']['version']} "
    )
    assert regex.fullmatch(user_agent[len("antigravity/cli/1.4.4 ") :])


# ---------------------------------------------------------------------------
# 15. the wiring: catalogue, status card, discovery, factory owner, constants
# ---------------------------------------------------------------------------


def test_the_catalog_descriptor_is_the_subscription_provider() -> None:
    descriptor = PROVIDER_CATALOG["antigravity_oauth"]
    assert descriptor.display_name == "Google Antigravity (OAuth, Caution)"
    assert descriptor.credential_env == "ANTIGRAVITY_OAUTH_ACCESS_TOKEN"
    assert descriptor.credential_attr == "antigravity_oauth_access_token"
    assert descriptor.credential_discoverable is True
    assert descriptor.group == "subscription"
    assert descriptor.media_surfaces == ()
    assert descriptor.proxy_attr == "antigravity_oauth_proxy"
    assert descriptor.credential_missing_label == "Not signed in"


def test_the_card_says_not_signed_in_rather_than_missing_key() -> None:
    by_id = {entry["provider_id"]: entry for entry in provider_config_status({})}
    card = by_id["antigravity_oauth"]
    assert card["status"] == "missing_key"
    assert card["label"] == "Not signed in"
    # Every other keyless provider keeps the shipped wording.
    assert by_id["nvidia_nim"]["label"] == "Missing key"


def test_the_factory_has_exactly_one_construction_owner() -> None:
    assert "antigravity_oauth" in factory._SPECIAL_PROVIDER_FACTORIES
    assert "antigravity_oauth" not in factory.OPENAI_CHAT_PROFILES
    assert "antigravity_oauth" in factory._OAUTH_PROVIDER_IDS


def test_discovery_never_lists_the_provider_without_a_credential() -> None:
    """The mechanical proof of "nothing happens until you sign in"."""

    settings = Settings.model_validate({})
    assert settings.antigravity_oauth_access_token == ""
    ids = model_list_provider_ids_for_settings(settings)
    assert "antigravity_oauth" not in ids


def test_a_stored_credential_writes_the_managed_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = antigravity_oauth_store_path()
    store.parent.mkdir(parents=True, exist_ok=True)
    credentials.save_accounts(
        [credentials.AccountRecord(id="acct", tokens=credentials.OAuthTokens("t"))],
        path=store,
    )
    settings = Settings.model_validate({})
    assert (
        settings.antigravity_oauth_access_token
        == ANTIGRAVITY_OAUTH_MANAGED_CREDENTIAL_REFERENCE
    )


def test_the_config_mirror_and_the_provider_package_agree() -> None:
    """``config`` is a leaf; the two spellings are pinned equal here."""

    assert ANTIGRAVITY_CLIENT_IDENTITY_DEFAULT == constants.CLIENT_IDENTITY_DEFAULT
    assert ANTIGRAVITY_CLIENT_PROFILE_DEFAULT == constants.CLIENT_PROFILE_DEFAULT
    assert (
        ANTIGRAVITY_OAUTH_MANAGED_CREDENTIAL_REFERENCE
        == "fcc-managed-antigravity-oauth"
    )


def test_the_route_status_rail_says_sign_in_is_not_available_yet() -> None:
    from my_claude_code.api.route_status import (
        NO_CREDENTIALS,
        SIGN_IN_UNAVAILABLE,
        credential_problems,
    )

    problems = credential_problems(
        [
            {
                "provider_id": "antigravity_oauth",
                "display_name": "Google Antigravity (OAuth, Caution)",
                "kind": "remote",
                "status": "missing_key",
            }
        ],
        {},
        lambda _provider: None,
    )
    assert problems["antigravity_oauth"]["state"] == NO_CREDENTIALS
    assert problems["antigravity_oauth"]["action"] == SIGN_IN_UNAVAILABLE
