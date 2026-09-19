"""An OAuth account is named like every other credential, and shown like one.

The one difference is that its credential cannot be fingerprinted: a rotating
token has no stable secret to hash, so the id is ``account:<id>`` and the
label the pool and the log carry is the account id itself. These pin that the
join still reaches every surface a key's name reaches.
"""

from pathlib import Path

import pytest

from my_claude_code.config.credential_names import (
    oauth_credential_id,
    oauth_label_names,
    oauth_pool_id,
    pool_names,
    set_name,
)
from my_claude_code.providers.anthropic_oauth import credentials as creds
from my_claude_code.providers.oauth_account_store import ORIGIN_MCC
from my_claude_code.providers.oauth_names import (
    fallback_name,
    seed_default_name,
)


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


def test_the_default_name_is_the_accounts_email_when_it_is_knowable_offline() -> None:
    record = creds.add_or_update_account(
        creds.OAuthTokens(
            access_token="a",
            refresh_token="r",
            account_uuid="uuid-1",
            account_email="someone@example.test",
        ),
        origin=ORIGIN_MCC,
    )

    names = pool_names(oauth_pool_id("anthropic_oauth"))
    assert names[oauth_credential_id(record.id)] == "someone@example.test"


def test_a_user_chosen_name_is_never_overwritten_by_a_default() -> None:
    pool = oauth_pool_id("anthropic_oauth")
    creds.add_or_update_account(
        creds.OAuthTokens(access_token="a", refresh_token="r", account_uuid="uuid-1"),
        origin=ORIGIN_MCC,
    )
    set_name(pool, oauth_credential_id("uuid-1"), "work")

    # A later refresh brings the email back. It must not win.
    creds.add_or_update_account(
        creds.OAuthTokens(
            access_token="b",
            refresh_token="r2",
            account_uuid="uuid-1",
            account_email="someone@example.test",
        ),
        account_id="uuid-1",
    )

    assert pool_names(pool)[oauth_credential_id("uuid-1")] == "work"


def test_the_default_name_falls_back_to_provider_account_n() -> None:
    first = creds.add_or_update_account(
        creds.OAuthTokens(access_token="a", refresh_token="r", account_uuid="u1"),
        origin=ORIGIN_MCC,
    )
    second = creds.add_or_update_account(
        creds.OAuthTokens(access_token="b", refresh_token="r", account_uuid="u2"),
        origin=ORIGIN_MCC,
    )

    names = pool_names(oauth_pool_id("anthropic_oauth"))
    assert names[oauth_credential_id(first.id)] == "Claude account 1"
    assert names[oauth_credential_id(second.id)] == "Claude account 2"


def test_the_fallback_number_does_not_renumber_when_an_earlier_account_is_removed() -> (
    None
):
    creds.add_or_update_account(
        creds.OAuthTokens(access_token="a", refresh_token="r", account_uuid="u1"),
        origin=ORIGIN_MCC,
    )
    creds.add_or_update_account(
        creds.OAuthTokens(access_token="b", refresh_token="r", account_uuid="u2"),
        origin=ORIGIN_MCC,
    )
    creds.remove_account("u1")

    # The ordinal is stored on the record, not recomputed from the position,
    # so removing an earlier account cannot quietly rename somebody's second.
    remaining = creds.load_accounts()
    assert remaining[0].ordinal == 2
    assert (
        pool_names(oauth_pool_id("anthropic_oauth"))[oauth_credential_id("u2")]
        == "Claude account 2"
    )

    third = creds.add_or_update_account(
        creds.OAuthTokens(access_token="c", refresh_token="r", account_uuid="u3"),
        origin=ORIGIN_MCC,
    )
    assert third.ordinal == 3


def test_seeding_a_default_twice_is_a_no_op() -> None:
    pool = oauth_pool_id("anthropic_oauth")
    seed_default_name(
        "anthropic_oauth", "u1", email="a@b.test", provider_label="Claude", ordinal=1
    )
    seed_default_name(
        "anthropic_oauth",
        "u1",
        email="other@b.test",
        provider_label="Claude",
        ordinal=1,
    )

    assert pool_names(pool)[oauth_credential_id("u1")] == "a@b.test"


def test_the_fallback_name_reads_the_way_the_card_shows_it() -> None:
    assert fallback_name("Claude", 2) == "Claude account 2"
    assert fallback_name("ChatGPT", 1) == "ChatGPT account 1"


def test_the_oauth_label_join_maps_the_account_id_to_the_name() -> None:
    pool = oauth_pool_id("anthropic_oauth")
    set_name(pool, oauth_credential_id("uuid-1"), "work")

    index = oauth_label_names([(pool, (("uuid-1", "uuid-1"), ("uuid-2", "uuid-2")))])

    assert index == {"uuid-1": "work"}


def test_an_oauth_name_reaches_the_shared_credential_name_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One resolver: the card, the request log and all three exports.

    ``credential_name_index`` is the single join every surface goes through,
    so an account name reaching it is an account name reaching all of them.
    """
    from my_claude_code.api import credential_display

    set_name(oauth_pool_id("anthropic_oauth"), oauth_credential_id("uuid-1"), "work")
    monkeypatch.setattr(
        "my_claude_code.providers.runtime.factory.oauth_account_ids",
        lambda provider_id: ["uuid-1"] if provider_id == "anthropic_oauth" else [],
    )

    assert credential_display.credential_name_index()["uuid-1"] == "work"


def test_an_unnamed_account_resolves_to_no_name_rather_than_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from my_claude_code.api import credential_display

    monkeypatch.setattr(
        "my_claude_code.providers.runtime.factory.oauth_account_ids",
        lambda provider_id: ["uuid-9"] if provider_id == "anthropic_oauth" else [],
    )

    assert "uuid-9" not in credential_display.credential_name_index()
