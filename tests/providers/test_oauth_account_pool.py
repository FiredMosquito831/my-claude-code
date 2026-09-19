"""Each account is a pool slot, and nothing else about construction moved.

The safety boundary for this PR is that ``core/credential_rotation.py`` ends
it byte-unchanged and that **every non-OAuth provider is constructed exactly
as before**. The golden below is the second half of that: it builds every
provider in the catalogue with and without accounts stored and asserts the
construction is identical.
"""

from pathlib import Path

import pytest

from my_claude_code.config.settings import Settings
from my_claude_code.providers.anthropic_oauth import credentials as creds
from my_claude_code.providers.oauth_account_store import ORIGIN_MCC
from my_claude_code.providers.runtime import factory
from my_claude_code.providers.runtime.rotating import RotatingProvider


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


def _seed(count: int) -> list[str]:
    ids = []
    for index in range(count):
        record = creds.add_or_update_account(
            creds.OAuthTokens(
                access_token=f"access-{index}",
                refresh_token=f"refresh-{index}",
                account_uuid=f"uuid-{index}",
                subscription_type="max",
            ),
            origin=ORIGIN_MCC,
        )
        ids.append(record.id)
    return ids


def _settings() -> Settings:
    return Settings(  # a scratch Settings: no .env, only what this test says
        anthropic_oauth_access_token="fcc-managed-anthropic-oauth",
    )


def test_one_account_still_builds_a_single_leaf_provider() -> None:
    _seed(1)

    provider = factory.create_provider("anthropic_oauth", _settings())

    # Unchanged from today: no RotatingProvider, no state, no labels. This is
    # what keeps every single-account test green.
    assert not isinstance(provider, RotatingProvider)


def test_two_accounts_build_a_two_slot_rotating_provider() -> None:
    account_ids = _seed(2)

    provider = factory.create_provider("anthropic_oauth", _settings())

    assert isinstance(provider, RotatingProvider)
    assert provider._key_labels == tuple(account_ids)
    assert len(provider._providers) == 2


def test_the_pool_slot_order_is_the_account_list_order() -> None:
    account_ids = _seed(3)

    provider = factory.create_provider("anthropic_oauth", _settings())

    assert isinstance(provider, RotatingProvider)
    # The list order **is** the slot order, exactly as the comma-separated
    # .env value is for an API-key pool, so the rotation engine keys on an
    # integer index without knowing anything about accounts.
    assert list(provider._key_labels) == account_ids
    assert [
        getattr(leaf, "_account_id", "") for leaf in provider._providers
    ] == account_ids


def test_each_slot_is_pinned_to_its_own_account() -> None:
    account_ids = _seed(2)

    provider = factory.create_provider("anthropic_oauth", _settings())

    assert isinstance(provider, RotatingProvider)
    # Without this every slot would resolve the primary account's credential
    # and the whole pool would be one credential wearing two labels.
    assert [leaf._config.oauth_account_id for leaf in provider._providers] == (
        account_ids
    )


def test_the_rotation_policy_env_var_applies_to_the_account_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``<ENV>_ROTATION`` now means something for OAuth, and it is the same one.

    The policy is resolved by the shared ``credential_rotation_policy``, which
    reads the same env key it reads for every API-key pool. Nothing about the
    rotation engine learned what an account is.
    """
    _seed(2)
    monkeypatch.setenv("ANTHROPIC_OAUTH_ACCESS_TOKEN_ROTATION", "round_robin")

    provider = factory.create_provider("anthropic_oauth", _settings())

    assert isinstance(provider, RotatingProvider)
    assert provider._state.policy == "round_robin"


def test_a_comma_separated_raw_token_is_still_refused_with_multi_account_on() -> None:
    """Unchanged, and checked before anything about accounts.

    Multi-account OAuth is several credentials MCC can *refresh*, which is the
    opposite of several pasted tokens that all expire and stay expired.
    """
    from my_claude_code.application.errors import InvalidRequestError
    from my_claude_code.providers.anthropic_oauth.provider import _auth_for
    from my_claude_code.providers.base import ProviderConfig

    _seed(2)
    config = ProviderConfig(api_key="sk-ant-oat01-a,sk-ant-oat01-b", base_url="")

    with pytest.raises(InvalidRequestError):
        _auth_for(config)


def test_every_non_oauth_provider_is_constructed_identically_with_accounts_stored() -> (
    None
):
    """The factory golden. Adding accounts must move nothing else."""
    from my_claude_code.config.provider_catalog import PROVIDER_CATALOG

    settings = Settings(
        anthropic_oauth_access_token="fcc-managed-anthropic-oauth",
    )

    def snapshot() -> dict[str, str]:
        built: dict[str, str] = {}
        for provider_id in PROVIDER_CATALOG:
            if provider_id in factory._OAUTH_PROVIDER_IDS:
                continue
            try:
                provider = factory.create_provider(provider_id, settings)
            except Exception as error:  # a provider with no credential
                built[provider_id] = f"raised:{type(error).__name__}"
                continue
            built[provider_id] = (
                f"{type(provider).__name__}:"
                f"{getattr(provider, '_key_labels', ())}:"
                f"{provider._config.oauth_account_id!r}"
            )
        return built

    before = snapshot()
    _seed(3)
    after = snapshot()

    assert before == after
    # And the new field is empty for every one of them, which is the only
    # thing that could have changed about their construction.
    assert all(":''" in value or "raised:" in value for value in after.values())
