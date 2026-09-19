"""Default names for OAuth accounts, in the 7.29.0 naming store.

An account is a pool slot now, and a pool slot the operator cannot tell apart
from the one beside it is the problem the naming store was built for. The only
difference here is that an OAuth credential rotates, so it cannot be
fingerprinted: the credential id is ``account:<id>`` rather than
``sha256:<digest>``, which is the namespace ``config/credential_names.py``
reserved for exactly this.

Two rules run through everything below:

* **A user-chosen name is never overwritten by a default.** A default is only
  ever written into an *empty* slot, and "empty" is decided by reading the
  store, never by remembering what we last wrote.
* **A name follows its account.** When an account's id is upgraded -- a
  synthetic id replaced by the real ``account.uuid`` the first refresh brings
  back -- the name moves with ``set_name`` on the new id plus
  ``forget_credentials`` on the old, which is the promise
  ``test_a_name_survives_the_key_moving_to_a_new_position`` already makes for
  API keys.

The email that seeds an Anthropic default arrives on the token response at no
upstream cost; the one that seeds a ChatGPT default is read from the id_token
already on disk, **only** to seed the name. Neither is ever logged, returned
raw by an endpoint, or used as a request dimension.
"""

from my_claude_code.config.credential_names import (
    forget_credentials,
    oauth_credential_id,
    oauth_pool_id,
    pool_names,
    set_name,
)


def account_name(provider_id: str, account_id: str) -> str:
    """The stored name of one account, or ``""`` when it has none."""

    if not account_id:
        return ""
    return pool_names(oauth_pool_id(provider_id)).get(
        oauth_credential_id(account_id), ""
    )


def fallback_name(provider_label: str, ordinal: int) -> str:
    """``"Claude account 2"`` -- the name an account gets when nothing else is.

    ``ordinal`` is the position the account held **when it was added**, stored
    on the record and never recomputed, so removing an earlier account does not
    renumber the ones after it and quietly rename somebody's second account.
    """

    return f"{provider_label} account {max(1, int(ordinal))}"


def seed_default_name(
    provider_id: str,
    account_id: str,
    *,
    email: str | None,
    provider_label: str,
    ordinal: int,
) -> str:
    """Write a default name for ``account_id`` unless it already has one.

    Returns the name the account now has. Called on every sign-in, import and
    refresh, which is safe precisely because it reads before it writes: the
    second call is a no-op, and a call that arrives after the user renamed the
    account leaves their name alone.
    """

    if not account_id:
        return ""
    existing = account_name(provider_id, account_id)
    if existing:
        return existing
    resolved = (email or "").strip() or fallback_name(provider_label, ordinal)
    return set_name(
        oauth_pool_id(provider_id), oauth_credential_id(account_id), resolved
    )


def move_name(provider_id: str, old_account_id: str, new_account_id: str) -> None:
    """Carry a name across an id upgrade, dropping the entry it came from."""

    if not old_account_id or not new_account_id or old_account_id == new_account_id:
        return
    pool = oauth_pool_id(provider_id)
    existing = pool_names(pool).get(oauth_credential_id(old_account_id), "")
    if existing:
        set_name(pool, oauth_credential_id(new_account_id), existing)
    forget_credentials(pool, [oauth_credential_id(old_account_id)])


def forget_account_name(provider_id: str, account_id: str) -> None:
    """Drop a disconnected account's name, so it cannot re-name a new one."""

    if not account_id:
        return
    forget_credentials(oauth_pool_id(provider_id), [oauth_credential_id(account_id)])
