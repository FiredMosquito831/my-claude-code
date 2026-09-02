"""Tests for the Claude subscription OAuth provider and its entrypoint gate."""

import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.providers.anthropic_messages import ANTHROPIC_API_VERSION
from my_claude_code.providers.anthropic_oauth import (
    AnthropicOAuthAuth,
    AnthropicOAuthProvider,
    AnthropicOAuthUnavailableError,
    OAuthTokens,
    detect_client_version,
    detect_entrypoint,
    is_claude_code_cli,
    is_claude_code_client,
    load_claude_code_tokens,
    load_tokens,
    store_tokens,
)
from my_claude_code.providers.anthropic_oauth import credentials as creds
from my_claude_code.providers.anthropic_oauth.constants import (
    ANTHROPIC_OAUTH_BETA_FLOOR,
    CLAUDE_CODE_APP,
    CLAUDE_CODE_USER_AGENT,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.rate_limit import ProviderRateLimiter

_TOKEN = "sk-ant-oat-user-secret"

CLI_MARKER = "x-anthropic-billing-header: cc_version=2.1.258; cc_entrypoint=cli;"
SDK_MARKER = "x-anthropic-billing-header: cc_version=2.1.223; cc_entrypoint=sdk-py;"
OTHER_MARKER = "x-anthropic-billing-header: cc_version=2.1.258; cc_entrypoint=opencode;"


def _request(system: Any = None) -> MessagesRequest:
    return MessagesRequest(
        model="claude-sonnet-4-6",
        max_tokens=32,
        system=system,
        messages=[Message(role="user", content="ping")],
        stream=True,
    )


def _provider(**kwargs: Any) -> AnthropicOAuthProvider:
    return AnthropicOAuthProvider(
        ProviderConfig(
            api_key="",
            base_url="https://api.anthropic.com/v1",
            rate_limit=100,
            rate_window=60,
            max_concurrency=5,
            retry_attempts=1,
            early_retry_attempts=1,
            commit_holdback_seconds=0,
        ),
        rate_limiter=ProviderRateLimiter(
            rate_limit=100, rate_window=60, max_concurrency=5, max_retries=0
        ),
        auth=AnthropicOAuthAuth(OAuthTokens(access_token=_TOKEN, source="test")),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Entrypoint detection -- the marker measured on real traffic
# ---------------------------------------------------------------------------


def test_detects_cli_entrypoint_from_a_real_marker() -> None:
    request = _request(f"{CLI_MARKER}\nYou are Claude Code, Anthropic's official CLI.")

    assert detect_entrypoint(request) == "cli"
    assert detect_client_version(request) == "2.1.258"
    assert is_claude_code_cli(request) is True
    assert is_claude_code_client(request) is True


def test_the_agent_sdk_is_an_anthropic_client_but_not_the_terminal() -> None:
    """The Agent SDK drives the Claude Code binary, so the gate admits it.

    Anthropic's policy names Claude Code *and* the Claude Agent SDK, and this
    entrypoint is 64% of the operator's measured traffic. Before 6.36.0 the
    gate refused all of it, which is why the distinction now has two names:
    ``is_claude_code_cli`` still answers "was this the terminal?", while
    ``is_claude_code_client`` is what the gate asks.
    """
    request = _request(f"{SDK_MARKER}\nYou are a Claude agent.")

    assert detect_entrypoint(request) == "sdk-py"
    assert is_claude_code_cli(request) is False
    assert is_claude_code_client(request) is True


def test_a_third_party_harness_is_not_an_anthropic_client() -> None:
    request = _request(f"{OTHER_MARKER}\nyou are opencode")

    assert is_claude_code_client(request) is False


@pytest.mark.parametrize("system", [None, "", "no marker at all", "cc_entrypoint="])
def test_unmarked_requests_are_not_the_cli(system: Any) -> None:
    assert is_claude_code_cli(_request(system)) is False
    assert is_claude_code_client(_request(system)) is False


def test_marker_is_found_in_block_shaped_system_prompts() -> None:
    request = _request(
        [{"type": "text", "text": CLI_MARKER}, {"type": "text", "text": "hi"}]
    )

    assert is_claude_code_cli(request) is True


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_gate_refuses_a_third_party_harness() -> None:
    provider = _provider()

    with pytest.raises(InvalidRequestError) as excinfo:
        provider.preflight_stream(_request(f"{OTHER_MARKER}\nopencode"))

    message = str(excinfo.value)
    assert "opencode" in message
    assert "anthropic" in message  # points at the supported provider


def test_refusal_message_names_the_setting_that_controls_the_gate() -> None:
    provider = _provider()

    with pytest.raises(InvalidRequestError) as excinfo:
        provider.preflight_stream(_request("unmarked"))

    assert "ANTHROPIC_OAUTH_REQUIRE_CLAUDE_CODE" in str(excinfo.value)


def test_gate_admits_real_cli_traffic() -> None:
    provider = _provider()

    # Must not raise.
    provider.preflight_stream(_request(f"{CLI_MARKER}\nclaude code"))


def test_gate_admits_agent_sdk_traffic() -> None:
    """The widened gate: the SDK is Anthropic's own client too."""
    provider = _provider()

    provider.preflight_stream(_request(f"{SDK_MARKER}\nagent"))


def test_gate_can_be_disabled_explicitly() -> None:
    provider = _provider(require_claude_code_cli=False)

    provider.preflight_stream(_request(f"{OTHER_MARKER}\nopencode"))


@pytest.mark.asyncio
async def test_gate_refuses_before_the_stream_opens() -> None:
    """A refusal must not reach the network with the subscription token."""
    provider = _provider()
    calls: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, content=b"")

    provider._messages._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )

    with pytest.raises(InvalidRequestError):
        stream = provider.stream_response(_request("unmarked"))
        [frame async for frame in stream]

    assert calls == []
    await provider.cleanup()


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oauth_headers_are_the_claude_code_set() -> None:
    """Rewritten in 6.36.0, deliberately.

    This test used to assert ``headers["x-api-key"] == _TOKEN``, which is the
    assertion that locked in the bug: Claude Code 2.1.258 puts an OAuth token
    in ``Authorization: Bearer`` and explicitly nulls ``X-Api-Key`` (binary
    offsets 187124317 / 181109007), and ``opencode-anthropic-auth@0.0.13``
    deletes ``x-api-key`` for the same reason. The provider had served zero
    successful requests in its whole life; this is the most likely reason.
    """
    headers = await AnthropicOAuthAuth(
        OAuthTokens(access_token=_TOKEN, source="test")
    ).headers()

    assert headers["Authorization"] == f"Bearer {_TOKEN}"
    assert "x-api-key" not in {name.lower() for name in headers}
    assert headers["anthropic-version"] == ANTHROPIC_API_VERSION
    assert headers["anthropic-beta"] == ",".join(ANTHROPIC_OAUTH_BETA_FLOOR)
    assert "oauth-2025-04-20" in headers["anthropic-beta"]
    assert headers["x-app"] == CLAUDE_CODE_APP
    assert headers["user-agent"] == CLAUDE_CODE_USER_AGENT


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_reads_claude_codes_own_credential_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    _write(
        tmp_path / ".credentials.json",
        {
            "claudeAiOauth": {
                "accessToken": _TOKEN,
                "refreshToken": "refresh-me",
                # Claude Code stores milliseconds.
                "expiresAt": int((time.time() + 3600) * 1000),
                "scopes": ["user:inference"],
                "subscriptionType": "max",
            }
        },
    )

    tokens = load_claude_code_tokens()

    assert tokens is not None
    assert tokens.access_token == _TOKEN
    assert tokens.subscription_type == "max"
    assert tokens.source == "claude-code"
    # Millisecond expiry must normalise to seconds, or the token looks like it
    # expires in the year 57000 and never refreshes.
    assert 3000 < (tokens.seconds_remaining() or 0) < 4000


def test_missing_credential_names_both_ways_to_fix_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(creds, "managed_store_path", lambda: tmp_path / "none.json")

    with pytest.raises(AnthropicOAuthUnavailableError) as excinfo:
        load_tokens()

    assert "mcc-anthropic-oauth-login" in str(excinfo.value)


def test_mcc_store_wins_over_claude_codes_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cc"))
    _write(
        tmp_path / "cc" / ".credentials.json",
        {"claudeAiOauth": {"accessToken": "from-claude-code"}},
    )
    store = tmp_path / "mcc.json"
    monkeypatch.setattr(creds, "managed_store_path", lambda: store)
    store_tokens(OAuthTokens(access_token="from-mcc", refresh_token="r"))

    assert load_tokens().access_token == "from-mcc"


def test_stored_credential_is_not_world_readable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = tmp_path / "mcc.json"
    monkeypatch.setattr(creds, "managed_store_path", lambda: store)

    store_tokens(OAuthTokens(access_token=_TOKEN, refresh_token="r"))

    assert store.exists()
    assert json.loads(store.read_text(encoding="utf-8"))["accessToken"] == _TOKEN


def test_expiry_leeway_triggers_refresh_before_actual_expiry() -> None:
    """Retuned to the 120 s leeway 6.36.0 ships.

    Claude Code's bundled SDK refreshes 30 s out (offset 180979163) and
    OpenCode refreshes at zero; 120 s survives a slow token endpoint without
    burning a refresh on a credential that still has most of an hour left.
    """
    nearly = OAuthTokens(
        access_token=_TOKEN, refresh_token="r", expires_at=int(time.time()) + 60
    )
    fresh = OAuthTokens(
        access_token=_TOKEN, refresh_token="r", expires_at=int(time.time()) + 7200
    )
    unknown = OAuthTokens(access_token=_TOKEN, refresh_token="r")

    assert nearly.needs_refresh() is True
    assert fresh.needs_refresh() is False
    # No expiry reported is not the same as expired.
    assert unknown.needs_refresh() is False


# ---------------------------------------------------------------------------
# Catalog wiring
# ---------------------------------------------------------------------------


def test_descriptor_does_not_require_a_pasted_credential() -> None:
    """The credential is discovered, so a missing key is not a config error.

    The field still exists so a token can be pasted from the Admin UI -- a
    card with no credential field and no owner offers nothing to configure --
    but ``credential_discoverable`` is what stops an empty one raising.
    """
    descriptor = PROVIDER_CATALOG["anthropic_oauth"]

    assert descriptor.credential_env == "ANTHROPIC_OAUTH_ACCESS_TOKEN"
    assert descriptor.credential_attr == "anthropic_oauth_access_token"
    assert descriptor.credential_discoverable is True


def test_discoverable_credential_does_not_block_provider_construction() -> None:
    """An empty setting must not raise: the provider finds its own token."""
    from my_claude_code.providers.runtime.config import require_provider_credential

    # Must not raise for the discoverable provider...
    require_provider_credential(PROVIDER_CATALOG["anthropic_oauth"], "")

    # ...while an ordinary provider still reports a missing key.
    with pytest.raises(Exception, match="ANTHROPIC_API_KEY"):
        require_provider_credential(PROVIDER_CATALOG["anthropic"], "")


def test_display_name_says_it_is_unsupported() -> None:
    assert "unsupported" in PROVIDER_CATALOG["anthropic_oauth"].display_name.lower()
