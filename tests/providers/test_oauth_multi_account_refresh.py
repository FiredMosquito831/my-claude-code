"""Refresh is per account: its own lock, its own 401, its own retirement.

No token endpoint is contacted. Every exchange here is a fixture served by an
``httpx.MockTransport``, the same style ``test_anthropic_oauth_selection.py``
already uses.
"""

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.providers.anthropic_oauth import credentials as creds
from my_claude_code.providers.anthropic_oauth.constants import REFRESH_LEEWAY_SECONDS
from my_claude_code.providers.chatgpt_oauth import credentials as chat
from my_claude_code.providers.oauth_account_store import ORIGIN_MCC


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "my_claude_code.config.credential_names.credential_names_path",
        lambda: tmp_path / "credential_names.json",
    )
    monkeypatch.setattr(
        creds, "managed_store_path", lambda: tmp_path / "anthropic_oauth.json"
    )
    creds._REFRESH_LOCKS.clear()


def _mock_token_endpoint(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:
    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr(creds.httpx, "AsyncClient", factory)


def _seed_two_accounts() -> None:
    for suffix in ("a", "b"):
        creds.add_or_update_account(
            creds.OAuthTokens(
                access_token=f"access-{suffix}",
                refresh_token=f"refresh-{suffix}",
                expires_at=int(time.time()) + 60,
                account_uuid=f"uuid-{suffix}",
                subscription_type="max",
            ),
            origin=ORIGIN_MCC,
        )


def test_the_proactive_leeway_is_unchanged_at_one_hundred_and_twenty_seconds() -> None:
    assert REFRESH_LEEWAY_SECONDS == 120
    # The ChatGPT side's 300 was inlined twice; naming it changed nothing but
    # the number of places it is written down.
    assert chat.REFRESH_LEEWAY_SECONDS == 300


def test_a_refresh_lock_is_keyed_on_the_account_not_only_the_file() -> None:
    first = creds._refresh_lock("uuid-a")
    second = creds._refresh_lock("uuid-b")

    assert first is not second
    assert creds._refresh_lock("uuid-a") is first


def test_two_accounts_refresh_independently_and_each_is_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_accounts()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content)
        seen.append(body["refresh_token"])
        suffix = body["refresh_token"].split("-")[-1]
        return httpx.Response(
            200,
            json={
                "access_token": f"fresh-{suffix}",
                "refresh_token": f"rotated-{suffix}",
                "expires_in": 3600,
                "account": {"uuid": f"uuid-{suffix}"},
            },
        )

    _mock_token_endpoint(monkeypatch, handler)

    async def run() -> None:
        stored = {record.id: record.tokens for record in creds.load_accounts()}
        # Two concurrent refreshes of the SAME account must perform one
        # exchange; the two different accounts must not wait on each other.
        await asyncio.gather(
            creds.refresh_tokens(stored["uuid-a"], account_id="uuid-a"),
            creds.refresh_tokens(stored["uuid-a"], account_id="uuid-a"),
            creds.refresh_tokens(stored["uuid-b"], account_id="uuid-b"),
        )

    asyncio.run(run())

    assert seen.count("refresh-a") == 1
    assert seen.count("refresh-b") == 1
    stored = {record.id: record.tokens for record in creds.load_accounts()}
    assert stored["uuid-a"].access_token == "fresh-a"
    assert stored["uuid-b"].access_token == "fresh-b"


def test_a_refresh_updates_only_the_account_it_was_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_accounts()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "fresh-a",
                "refresh_token": "rotated-a",
                "expires_in": 3600,
                "account": {"uuid": "uuid-a"},
            },
        )

    _mock_token_endpoint(monkeypatch, handler)
    stored = {record.id: record.tokens for record in creds.load_accounts()}
    asyncio.run(creds.refresh_tokens(stored["uuid-a"], account_id="uuid-a"))

    after = creds.load_accounts()
    assert len(after) == 2
    assert after[0].tokens.access_token == "fresh-a"
    assert after[1].tokens.access_token == "access-b"


def test_a_definitive_rejection_retires_only_the_account_that_was_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _seed_two_accounts()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    _mock_token_endpoint(monkeypatch, handler)
    stored = {record.id: record.tokens for record in creds.load_accounts()}
    with pytest.raises(creds.AnthropicOAuthRefreshError):
        asyncio.run(creds.refresh_tokens(stored["uuid-a"], account_id="uuid-a"))

    remaining = creds.load_accounts()
    assert [record.id for record in remaining] == ["uuid-b"]
    # And the store itself survived -- the other account is still serving.
    assert creds.managed_store_path().is_file()
    assert list(tmp_path.glob("anthropic_oauth.json.dead-*"))


def test_a_transient_rejection_retires_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_two_accounts()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="slow down")

    _mock_token_endpoint(monkeypatch, handler)
    stored = {record.id: record.tokens for record in creds.load_accounts()}
    with pytest.raises(creds.AnthropicOAuthRefreshUnavailable):
        asyncio.run(creds.refresh_tokens(stored["uuid-a"], account_id="uuid-a"))

    assert len(creds.load_accounts()) == 2


def test_the_401_path_refreshes_only_the_account_that_served_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AnthropicOAuthAuth`` resolves and refreshes *its* account, not any."""
    from my_claude_code.providers.anthropic_oauth.auth import AnthropicOAuthAuth

    _seed_two_accounts()
    refreshed_for: list[str] = []

    async def fake_refresh(tokens, *, account_id=""):
        refreshed_for.append(account_id)
        return tokens

    monkeypatch.setattr(
        "my_claude_code.providers.anthropic_oauth.auth.refresh_tokens", fake_refresh
    )
    auth = AnthropicOAuthAuth(account_id="uuid-b")
    asyncio.run(auth.force_refresh())

    assert refreshed_for == ["uuid-b"]
    assert auth.tokens is not None
    assert auth.tokens.access_token == "access-b"


def test_the_chatgpt_refresh_lock_is_keyed_on_the_account(tmp_path: Path) -> None:
    path = tmp_path / "chatgpt-oauth.json"
    first = chat._refresh_lock(path, "acct-1")
    second = chat._refresh_lock(path, "acct-2")

    assert first is not second
    assert chat._refresh_lock(path, "acct-1") is first
