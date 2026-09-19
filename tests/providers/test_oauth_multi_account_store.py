"""The multi-account store: migration, the mirror, and the downgrade promise.

Every fixture here is written by hand. No token endpoint is contacted and no
real credential file is read: the shapes come from the masked dumps in the
spec, and the ``account`` object's key names come from Claude Code's own
mapping rather than from a captured response body.
"""

import json
import time
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.providers.anthropic_oauth import credentials as creds
from my_claude_code.providers.chatgpt_oauth import credentials as chat
from my_claude_code.providers.oauth_account_store import (
    ORIGIN_CODEX,
    ORIGIN_MCC,
)

_TOKEN = "sk-ant-oat01-not-a-real-token"
_SECOND = "sk-ant-oat01-second-not-real"


@pytest.fixture(autouse=True)
def _isolated_names(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Names go to a scratch file, never the operator's own."""
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


def _legacy_document(token: str = _TOKEN) -> dict[str, Any]:
    """Exactly what 7.29.x wrote: seven flat keys, no accounts, no identity."""
    return {
        "accessToken": token,
        "refreshToken": "refresh-one",
        "expiresAt": (int(time.time()) + 3600) * 1000,
        "scopes": ["user:inference", "user:profile"],
        "subscriptionType": "max",
        "refreshTokenExpiresAt": (int(time.time()) + 86400) * 1000,
        "rateLimitTier": "default_claude_max_5x",
    }


def test_first_read_of_a_single_account_store_migrates_and_backs_up_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _redirect(monkeypatch, tmp_path)
    store.write_text(json.dumps(_legacy_document()), encoding="utf-8")

    records = creds.load_accounts()

    assert len(records) == 1
    assert records[0].tokens.access_token == _TOKEN
    assert records[0].origin == ORIGIN_MCC
    # Provenance was never persisted before 7.30.0, so nothing may be claimed
    # about where this came from -- and write-back must stay off until the
    # operator imports again and says otherwise.
    assert records[0].write_back is False
    backups = list(tmp_path.glob("anthropic_oauth.json.bak-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8"))["accessToken"] == _TOKEN
    document = json.loads(store.read_text(encoding="utf-8"))
    assert len(document["accounts"]) == 1
    assert document["accountsVersion"] == 1


def test_the_backup_is_written_only_on_the_first_migration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _redirect(monkeypatch, tmp_path)
    store.write_text(json.dumps(_legacy_document()), encoding="utf-8")

    creds.load_accounts()
    first = sorted(tmp_path.glob("anthropic_oauth.json.bak-*"))
    creds.load_accounts()
    creds.load_accounts()

    assert sorted(tmp_path.glob("anthropic_oauth.json.bak-*")) == first


def test_an_old_parser_still_reads_the_primary_account_from_a_multi_account_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The whole downgrade story, asserted against the shipped parser itself."""
    _redirect(monkeypatch, tmp_path)
    creds.add_or_update_account(
        creds.OAuthTokens(access_token=_TOKEN, refresh_token="r1", account_uuid="a"),
        origin=ORIGIN_MCC,
    )
    creds.add_or_update_account(
        creds.OAuthTokens(access_token=_SECOND, refresh_token="r2", account_uuid="b"),
        origin=ORIGIN_MCC,
    )

    document = json.loads(creds.managed_store_path().read_text(encoding="utf-8"))
    # ``_tokens_from_payload`` is the shipped reader: it looks only at the top
    # level, which is precisely why the seven keys are mirrored there.
    primary = creds._tokens_from_payload(document, source="mcc")

    assert primary is not None
    assert primary.access_token == _TOKEN
    assert len(document["accounts"]) == 2


def test_a_chatgpt_store_keeps_version_one_so_an_old_build_degrades_instead_of_raising(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chatgpt-oauth.json"
    chat.save_chatgpt_accounts(
        [
            chat.ChatGPTAccountRecord(
                id="acct-1", tokens={"access_token": "a", "account_id": "acct-1"}
            ),
            chat.ChatGPTAccountRecord(
                id="acct-2", tokens={"access_token": "b", "account_id": "acct-2"}
            ),
        ],
        auth_path=path,
    )

    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["version"] == chat.MANAGED_CREDENTIAL_SCHEMA_VERSION == 1
    # The shipped reader raises on a version mismatch, so a bump would have
    # turned a downgrade into a hard failure. It degrades instead.
    assert chat._load_managed_source(path).access_token == "a"


def test_saving_accounts_mirrors_the_primary_to_the_legacy_top_level_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _redirect(monkeypatch, tmp_path)
    creds.save_accounts(
        [
            creds.AccountRecord(
                id="a",
                tokens=creds.OAuthTokens(
                    access_token=_TOKEN,
                    refresh_token="r1",
                    expires_at=1_700_000_000,
                    subscription_type="max",
                    rate_limit_tier="default_claude_max_5x",
                ),
            ),
            creds.AccountRecord(id="b", tokens=creds.OAuthTokens(access_token=_SECOND)),
        ]
    )

    document = json.loads(store.read_text(encoding="utf-8"))
    assert document["accessToken"] == _TOKEN
    assert document["subscriptionType"] == "max"
    assert document["rateLimitTier"] == "default_claude_max_5x"
    assert document["expiresAt"] == 1_700_000_000 * 1000


def test_the_account_id_survives_a_refresh_that_rotates_the_refresh_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect(monkeypatch, tmp_path)
    first = creds.add_or_update_account(
        creds.OAuthTokens(
            access_token=_TOKEN, refresh_token="r1", account_uuid="uuid-1"
        ),
        origin=ORIGIN_MCC,
    )
    rotated = creds.add_or_update_account(
        creds.OAuthTokens(
            access_token=_SECOND, refresh_token="r2-rotated", account_uuid="uuid-1"
        ),
        origin=ORIGIN_MCC,
    )

    assert first.id == rotated.id == "uuid-1"
    assert len(creds.load_accounts()) == 1


def test_a_synthetic_anthropic_account_id_is_minted_once_and_never_recomputed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect(monkeypatch, tmp_path)
    minted = creds.add_or_update_account(
        creds.OAuthTokens(access_token=_TOKEN, refresh_token="r1"), origin=ORIGIN_MCC
    )

    assert minted.id.startswith("local-")
    # Re-reading must not mint a second id: the name is attached to this one.
    assert [record.id for record in creds.load_accounts()] == [minted.id]
    assert [record.id for record in creds.load_accounts()] == [minted.id]


def test_an_unreadable_store_is_read_as_empty_and_never_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = _redirect(monkeypatch, tmp_path)
    store.write_text("{not json at all", encoding="utf-8")

    assert creds.load_accounts() == []
    # And the same on the ChatGPT side, whose runtime reader *does* raise.
    bad = tmp_path / "chatgpt-oauth.json"
    bad.write_text('{"version": 999}', encoding="utf-8")
    assert chat.load_chatgpt_accounts(auth_path=bad) == []


def test_a_codex_import_persists_its_origin_so_write_back_is_possible(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chatgpt-oauth.json"
    source = tmp_path / "auth.json"
    chat.add_or_update_chatgpt_account(
        {"access_token": "a", "refresh_token": "r", "account_id": "acct-1"},
        origin=ORIGIN_CODEX,
        origin_path=str(source),
        auth_path=path,
    )

    record = chat.load_chatgpt_accounts(auth_path=path)[0]
    assert record.origin == ORIGIN_CODEX
    assert record.write_back is True
    assert record.origin_path == str(source)
