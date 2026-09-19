"""The ``account`` object MCC used to throw away, and the id it produces.

The key names are read out of Claude Code 2.1.278's own mapping
(``formatTokens``: ``e.account.uuid``, ``e.account.email_address``), not out
of a captured response body -- no token call was made to write this. Every
fixture here is therefore written **by hand** from that mapping, and the
parsing is deliberately defensive about it.
"""

from pathlib import Path

import pytest

from my_claude_code.config.credential_names import (
    oauth_credential_id,
    oauth_pool_id,
    pool_names,
    set_name,
)
from my_claude_code.providers.anthropic_oauth import credentials as creds
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


def _exchange_response(uuid: str = "uuid-1", email: str = "a@example.test") -> dict:
    """A token response shaped the way ``formatTokens`` reads one."""
    return {
        "access_token": "sk-ant-oat01-not-real",
        "refresh_token": "refresh-1",
        "expires_in": 3600,
        "scope": "user:inference user:profile",
        "account": {"uuid": uuid, "email_address": email},
        "organization": {"uuid": "org-1", "name": "Acme"},
    }


def test_the_account_object_on_a_token_exchange_becomes_the_account_id() -> None:
    tokens = creds._tokens_from_payload(_exchange_response(), source="mcc")

    assert tokens is not None
    assert tokens.account_uuid == "uuid-1"
    assert tokens.account_email == "a@example.test"
    assert tokens.organization_name == "Acme"

    record = creds.add_or_update_account(tokens, origin=ORIGIN_MCC)
    assert record.id == "uuid-1"


def test_an_account_object_with_id_instead_of_uuid_is_accepted() -> None:
    payload = _exchange_response()
    payload["account"] = {"id": "uuid-2", "email": "b@example.test"}

    tokens = creds._tokens_from_payload(payload, source="mcc")

    assert tokens is not None
    assert tokens.account_uuid == "uuid-2"
    assert tokens.account_email == "b@example.test"


def test_an_absent_account_object_falls_back_to_a_synthetic_id() -> None:
    payload = _exchange_response()
    payload.pop("account")
    payload.pop("organization")

    tokens = creds._tokens_from_payload(payload, source="mcc")
    assert tokens is not None and tokens.account_uuid is None

    record = creds.add_or_update_account(tokens, origin=ORIGIN_MCC)
    assert record.id.startswith("local-")


def test_a_refresh_that_omits_the_account_object_keeps_the_id_and_name_we_have() -> (
    None
):
    previous = creds.OAuthTokens(
        access_token="old",
        refresh_token="r1",
        account_uuid="uuid-1",
        account_email="a@example.test",
        organization_name="Acme",
    )
    payload = _exchange_response()
    payload.pop("account")
    payload.pop("organization")

    refreshed = creds._tokens_from_refresh(payload, previous=previous)

    # ``formatTokens`` guards the object with ``e.account?``, so a refresh may
    # legitimately omit it. Blanking the id would detach the operator's name
    # from the account on the first refresh.
    assert refreshed.account_uuid == "uuid-1"
    assert refreshed.account_email == "a@example.test"
    assert refreshed.organization_name == "Acme"


def test_the_account_object_on_a_refresh_upgrades_a_synthetic_id_in_place() -> None:
    minted = creds.add_or_update_account(
        creds.OAuthTokens(access_token="old", refresh_token="r1"), origin=ORIGIN_MCC
    )
    assert minted.id.startswith("local-")

    upgraded = creds.add_or_update_account(
        creds.OAuthTokens(
            access_token="new", refresh_token="r2", account_uuid="uuid-1"
        ),
        account_id=minted.id,
    )

    assert upgraded.id == "uuid-1"
    assert [record.id for record in creds.load_accounts()] == ["uuid-1"]


def test_upgrading_an_id_moves_the_name_with_it() -> None:
    minted = creds.add_or_update_account(
        creds.OAuthTokens(access_token="old", refresh_token="r1"), origin=ORIGIN_MCC
    )
    pool = oauth_pool_id("anthropic_oauth")
    set_name(pool, oauth_credential_id(minted.id), "the personal one")

    creds.add_or_update_account(
        creds.OAuthTokens(
            access_token="new", refresh_token="r2", account_uuid="uuid-1"
        ),
        account_id=minted.id,
    )

    names = pool_names(pool)
    assert names[oauth_credential_id("uuid-1")] == "the personal one"
    assert oauth_credential_id(minted.id) not in names


def test_the_email_never_reaches_a_log_line_or_a_request_log_row(
    tmp_path: Path,
) -> None:
    """The email is a *name* the operator owns, never a log dimension.

    ``AnthropicOAuthAuth.label`` is what reaches the request-log row. With no
    name stored it reports the plan and the source exactly as it did before
    7.30.0 -- it never reaches for the email field itself.
    """
    from my_claude_code.providers.anthropic_oauth.auth import AnthropicOAuthAuth

    tokens = creds.OAuthTokens(
        access_token="x",
        subscription_type="max",
        account_email="secret@example.test",
        account_uuid="uuid-1",
        source="mcc",
    )
    auth = AnthropicOAuthAuth(tokens, account_id="uuid-1")

    label = auth.label()

    assert label == "max · mcc"
    assert "secret@example.test" not in (label or "")


def test_the_chatgpt_email_claim_is_read_only_to_seed_a_name(
    tmp_path: Path,
) -> None:
    """Q3, narrowly: ``email`` seeds a name and nothing else reads a claim."""
    import base64
    import json

    def _jwt(claims: dict) -> str:
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
        return f"header.{payload.rstrip('=')}.signature"

    path = tmp_path / "chatgpt-oauth.json"
    chat.add_or_update_chatgpt_account(
        {
            "access_token": "a",
            "account_id": "acct-1",
            "id_token": _jwt({"email": "c@example.test", "sub": "never-read"}),
        },
        auth_path=path,
    )

    assert (
        chat.stored_chatgpt_account_email(auth_path=path, account_id="acct-1")
        == "c@example.test"
    )
    names = pool_names(oauth_pool_id("chatgpt_oauth"))
    assert names[oauth_credential_id("acct-1")] == "c@example.test"
