"""Signing a second account in ADDS; signing the same one in UPDATES.

Before 7.30.0 every sign-in replaced whatever was stored, which is exactly
what made a second Claude account impossible. These pin the conflict rule.
"""

import json
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
from my_claude_code.providers.oauth_account_store import ORIGIN_CLAUDE_CODE, ORIGIN_MCC


@pytest.fixture(autouse=True)
def _isolated_names(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    names = tmp_path / "credential_names.json"
    monkeypatch.setattr(
        "my_claude_code.config.credential_names.credential_names_path",
        lambda: names,
    )


def _redirect(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    store = tmp_path / "anthropic_oauth.json"
    monkeypatch.setattr(creds, "managed_store_path", lambda: store)
    creds._REFRESH_LOCKS.clear()
    return store


def _tokens(token: str, uuid: str, email: str = "") -> creds.OAuthTokens:
    return creds.OAuthTokens(
        access_token=token,
        refresh_token=f"refresh-{uuid}",
        account_uuid=uuid,
        account_email=email or None,
        subscription_type="max",
    )


def test_signing_in_a_second_account_appends_rather_than_replacing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect(monkeypatch, tmp_path)
    creds.add_or_update_account(_tokens("a", "uuid-a"), origin=ORIGIN_MCC)
    creds.add_or_update_account(_tokens("b", "uuid-b"), origin=ORIGIN_MCC)

    records = creds.load_accounts()
    assert [record.id for record in records] == ["uuid-a", "uuid-b"]
    assert [record.tokens.access_token for record in records] == ["a", "b"]
    assert [record.ordinal for record in records] == [1, 2]


def test_signing_in_an_account_that_is_already_stored_updates_it_in_place(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect(monkeypatch, tmp_path)
    creds.add_or_update_account(_tokens("a", "uuid-a"), origin=ORIGIN_MCC)
    creds.add_or_update_account(_tokens("b", "uuid-b"), origin=ORIGIN_MCC)
    creds.add_or_update_account(_tokens("a-fresh", "uuid-a"), origin=ORIGIN_MCC)

    records = creds.load_accounts()
    assert len(records) == 2
    assert records[0].tokens.access_token == "a-fresh"
    assert records[1].tokens.access_token == "b"


def test_an_updated_account_keeps_its_name_origin_and_added_at(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect(monkeypatch, tmp_path)
    imported = creds.add_or_update_account(
        _tokens("a", "uuid-a"),
        origin=ORIGIN_CLAUDE_CODE,
        origin_path="C:/Users/someone/.claude/.credentials.json",
        write_back=True,
    )
    set_name(
        oauth_pool_id("anthropic_oauth"),
        oauth_credential_id("uuid-a"),
        "the work one",
    )

    updated = creds.add_or_update_account(_tokens("a-fresh", "uuid-a"))

    assert updated.origin == ORIGIN_CLAUDE_CODE
    assert updated.origin_path == imported.origin_path
    assert updated.write_back is True
    assert updated.added_at == imported.added_at
    assert updated.ordinal == imported.ordinal
    names = pool_names(oauth_pool_id("anthropic_oauth"))
    assert names[oauth_credential_id("uuid-a")] == "the work one"


def test_removing_one_account_leaves_the_others_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect(monkeypatch, tmp_path)
    creds.add_or_update_account(_tokens("a", "uuid-a"), origin=ORIGIN_MCC)
    creds.add_or_update_account(_tokens("b", "uuid-b"), origin=ORIGIN_MCC)

    assert creds.remove_account("uuid-a") is not None

    remaining = creds.load_accounts()
    assert [record.id for record in remaining] == ["uuid-b"]
    # The primary mirror moved with it, so the store still serves.
    primary = creds.load_managed_tokens()
    assert primary is not None
    assert primary.access_token == "b"


def test_a_removed_anthropic_account_is_written_to_a_dead_file_not_deleted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect(monkeypatch, tmp_path)
    creds.add_or_update_account(_tokens("a", "uuid-a"), origin=ORIGIN_MCC)
    creds.add_or_update_account(_tokens("b", "uuid-b"), origin=ORIGIN_MCC)

    creds.remove_account("uuid-a")

    dead = list(tmp_path.glob("anthropic_oauth.json.dead-*"))
    assert len(dead) == 1
    assert json.loads(dead[0].read_text(encoding="utf-8"))["id"] == "uuid-a"


def test_removing_an_account_forgets_its_name_so_it_cannot_rename_a_new_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect(monkeypatch, tmp_path)
    creds.add_or_update_account(_tokens("a", "uuid-a"), origin=ORIGIN_MCC)
    set_name(oauth_pool_id("anthropic_oauth"), oauth_credential_id("uuid-a"), "gone")
    creds.add_or_update_account(_tokens("b", "uuid-b"), origin=ORIGIN_MCC)

    creds.remove_account("uuid-a")

    assert oauth_credential_id("uuid-a") not in pool_names(
        oauth_pool_id("anthropic_oauth")
    )


def test_a_chatgpt_second_account_appends_and_the_same_one_updates(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chatgpt-oauth.json"
    chat.add_or_update_chatgpt_account(
        {"access_token": "a", "account_id": "acct-1"}, auth_path=path
    )
    chat.add_or_update_chatgpt_account(
        {"access_token": "b", "account_id": "acct-2"}, auth_path=path
    )
    chat.add_or_update_chatgpt_account(
        {"access_token": "a-fresh", "account_id": "acct-1"}, auth_path=path
    )

    records = chat.load_chatgpt_accounts(auth_path=path)
    assert [record.id for record in records] == ["acct-1", "acct-2"]
    assert records[0].tokens["access_token"] == "a-fresh"


def test_removing_a_chatgpt_account_leaves_the_file_and_the_others(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chatgpt-oauth.json"
    chat.add_or_update_chatgpt_account(
        {"access_token": "a", "account_id": "acct-1"}, auth_path=path
    )
    chat.add_or_update_chatgpt_account(
        {"access_token": "b", "account_id": "acct-2"}, auth_path=path
    )

    assert chat.remove_chatgpt_account("acct-1", auth_path=path) is not None

    assert path.is_file()
    assert [r.id for r in chat.load_chatgpt_accounts(auth_path=path)] == ["acct-2"]
