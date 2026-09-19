"""Human names for credentials: ``~/.mcc/credential_names.json``.

A pool of API keys is a list of near-identical strings. The dashboard has
always shown a mask (``nvapi-…epz9``), which is honest and nearly useless when
three keys share a prefix. This file is the one place a person can say "that
one is the work account", and it is deliberately the *only* thing this store
holds.

**The name is display only.** It is never written to the request log, never a
rollup dimension, never an env value, and never part of a request that leaves
the machine. Analytics still group by ``mask_key_label``; a name is resolved at
render time by joining the *current* pool's masks to this file, so renaming a
key can never break, rewrite or mislabel a single row of history.

**The store never holds a secret.** A credential is identified by
``sha256(secret)[:16]`` -- a fingerprint, the same way ``core/wire_capture``
already recognises a credential it must never keep. That makes the name follow
the key across a reorder, a rotation and a restart, because none of those
change the secret.

**Credential ids are namespaced** so the next thing that needs a name does not
need a migration: ``sha256:<digest>`` for a key this machine holds, and
``account:<id>`` for an OAuth account, whose token rotates and therefore cannot
be hashed. Pool ids are namespaced the same way -- ``env:<ENV_KEY>``,
``custom:<provider_id>``, ``websearch:<ENV_KEY>``, ``oauth:<provider_id>`` --
so three surfaces that already share a row shape share one file too.

A document whose ``version`` this build does not recognise is read as empty and
never rewritten, so downgrading and upgrading are both safe: an older build
ignores the file, and this one refuses to clobber a newer one.
"""

import json
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from my_claude_code.config.atomic_json import (
    write_json_document_atomically_if_changed,
)
from my_claude_code.config.credentials import mask_key_label
from my_claude_code.config.paths import credential_names_path

STORE_VERSION = 1
VERSION_KEY = "version"
POOLS_KEY = "pools"
CREDENTIALS_KEY = "credentials"
NAME_KEY = "name"
ADDED_AT_KEY = "added_at"

#: Long enough that a name is a name and not a paragraph; short enough that a
#: key row on a laptop does not wrap. Enforced here as well as in the browser,
#: because the route is reachable without the page.
MAX_NAME_LENGTH = 60

ENV_POOL_PREFIX = "env:"
CUSTOM_POOL_PREFIX = "custom:"
WEBSEARCH_POOL_PREFIX = "websearch:"
OAUTH_POOL_PREFIX = "oauth:"

#: Matches ``MIN_HASHED_CREDENTIAL_CHARS`` in ``core/wire_capture``: enough of
#: the digest that a collision is not a thing that happens, short enough to
#: read in a JSON file the operator may open.
FINGERPRINT_CHARS = 16


def credential_fingerprint(secret: str) -> str:
    """Return the stable id of one credential, without keeping the credential."""

    digest = sha256(secret.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]
    return f"sha256:{digest}"


def oauth_credential_id(account_id: str) -> str:
    """Return the credential id of one OAuth account.

    Unused today. It exists so that multi-account OAuth adds entries to this
    file rather than inventing a second format for the same question.
    """

    return f"account:{account_id}"


def env_pool_id(env_key: str) -> str:
    """Return the pool id of a built-in provider's ``.env`` credential."""

    return f"{ENV_POOL_PREFIX}{env_key}"


def custom_pool_id(provider_id: str) -> str:
    """Return the pool id of a custom provider's key list."""

    return f"{CUSTOM_POOL_PREFIX}{provider_id}"


def websearch_pool_id(env_key: str) -> str:
    """Return the pool id of a web search provider's credential."""

    return f"{WEBSEARCH_POOL_PREFIX}{env_key}"


def oauth_pool_id(provider_id: str) -> str:
    """Return the pool id of an OAuth provider's accounts. Reserved."""

    return f"{OAUTH_POOL_PREFIX}{provider_id}"


def normalise_name(name: str | None) -> str:
    """Trim a submitted name and clamp it to :data:`MAX_NAME_LENGTH`."""

    return (name or "").strip()[:MAX_NAME_LENGTH]


def load_document(path: Path | None = None) -> dict[str, Any]:
    """Read the whole store, tolerating every way a JSON file can disappoint.

    Never raises. A missing file, an unparsable one, one holding a list, or one
    written by a version this build does not know all read as an empty store --
    a name is a convenience, and no failure to read one may cost the operator a
    key listing.
    """

    resolved = path if path is not None else credential_names_path()
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except OSError, ValueError:
        return _empty_document()
    if not isinstance(raw, Mapping):
        return _empty_document()
    if raw.get(VERSION_KEY) != STORE_VERSION:
        return _empty_document()
    pools = raw.get(POOLS_KEY)
    if not isinstance(pools, Mapping):
        return _empty_document()
    return {
        VERSION_KEY: STORE_VERSION,
        POOLS_KEY: {
            str(pool_id): _clean_pool(entry) for pool_id, entry in pools.items()
        },
    }


def pool_names(pool_id: str, path: Path | None = None) -> dict[str, str]:
    """Return ``credential id -> name`` for one pool."""

    pool = load_document(path).get(POOLS_KEY, {}).get(pool_id)
    if not isinstance(pool, Mapping):
        return {}
    credentials = pool.get(CREDENTIALS_KEY)
    if not isinstance(credentials, Mapping):
        return {}
    return {
        str(credential_id): str(entry.get(NAME_KEY, ""))
        for credential_id, entry in credentials.items()
        if isinstance(entry, Mapping) and str(entry.get(NAME_KEY, ""))
    }


def set_name(
    pool_id: str,
    credential_id: str,
    name: str,
    path: Path | None = None,
) -> str:
    """Store (or, for an empty name, clear) one credential's name.

    Returns the name as stored. An empty name deletes the entry rather than
    storing a blank, so a cleared name leaves no trace to migrate later.
    """

    resolved_name = normalise_name(name)
    document = load_document(path)
    pools = document[POOLS_KEY]
    pool = pools.setdefault(pool_id, {CREDENTIALS_KEY: {}})
    credentials = pool[CREDENTIALS_KEY]
    if resolved_name:
        existing = credentials.get(credential_id)
        added_at = (
            existing.get(ADDED_AT_KEY)
            if isinstance(existing, Mapping) and existing.get(ADDED_AT_KEY)
            else _now()
        )
        credentials[credential_id] = {
            NAME_KEY: resolved_name,
            ADDED_AT_KEY: str(added_at),
        }
    else:
        credentials.pop(credential_id, None)
    _prune_empty_pools(document)
    _write(document, path)
    return resolved_name


def forget_credentials(
    pool_id: str,
    credential_ids: Iterable[str],
    path: Path | None = None,
) -> None:
    """Drop the names of credentials that no longer exist.

    Called by every delete path. A store that accumulated the name of every key
    the operator ever removed would grow forever and, worse, would silently
    re-name a key that happened to be added back.
    """

    wanted = {str(credential_id) for credential_id in credential_ids}
    if not wanted:
        return
    document = load_document(path)
    pool = document[POOLS_KEY].get(pool_id)
    if not isinstance(pool, Mapping):
        return
    credentials = pool[CREDENTIALS_KEY]
    if not any(credential_id in credentials for credential_id in wanted):
        return
    for credential_id in wanted:
        credentials.pop(credential_id, None)
    _prune_empty_pools(document)
    _write(document, path)


def forget_pool(pool_id: str, path: Path | None = None) -> None:
    """Drop every name in one pool, for a provider that was deleted outright."""

    document = load_document(path)
    if document[POOLS_KEY].pop(pool_id, None) is None:
        return
    _write(document, path)


def names_for_secrets(
    pool_id: str,
    secrets: Sequence[str],
    path: Path | None = None,
) -> list[str]:
    """Return the name of each secret, positionally, empty where unnamed."""

    stored = pool_names(pool_id, path)
    return [stored.get(credential_fingerprint(secret), "") for secret in secrets]


def label_name_map(
    pool_id: str,
    secrets: Sequence[str],
    path: Path | None = None,
) -> dict[str, str]:
    """Return ``mask_key_label(secret) -> name`` for one pool's *current* keys.

    This is the display join, and the reason a name is never stored as a
    dimension: history is keyed on the mask, so a name only has to be resolvable
    from the mask at the moment something is rendered.

    A mask produced by more than one secret is **dropped**. ``mask_key_label``
    is ``first4…last4``, and keys from one vendor routinely share a prefix, so
    an ambiguous mask is not a hypothetical. Showing no name is a small loss;
    showing the wrong key's name in a log row would be a lie.
    """

    return merged_label_names([(pool_id, secrets)], path)


def merged_label_names(
    pools: Iterable[tuple[str, Sequence[str]]],
    path: Path | None = None,
) -> dict[str, str]:
    """The same join across every configured pool at once.

    One index for the whole dashboard, because a request-log row carries a mask
    and a provider name, not a pool id. A mask that two pools both produce is
    dropped unless they agree on the name, for the same reason a mask two keys
    in one pool produce is dropped: the join has to be certain or absent.
    """

    document = load_document(path)
    stored_pools = document[POOLS_KEY]
    by_label: dict[str, str] = {}
    ambiguous: set[str] = set()
    for pool_id, secrets in pools:
        entry = stored_pools.get(pool_id, {})
        stored = {
            credential_id: value[NAME_KEY]
            for credential_id, value in entry.get(CREDENTIALS_KEY, {}).items()
        }
        for secret in secrets:
            label = mask_key_label(secret)
            if not label or label in ambiguous:
                continue
            name = stored.get(credential_fingerprint(secret), "")
            if label in by_label and by_label[label] != name:
                ambiguous.add(label)
                by_label.pop(label, None)
                continue
            by_label[label] = name
    return {label: name for label, name in by_label.items() if name}


def _empty_document() -> dict[str, Any]:
    return {VERSION_KEY: STORE_VERSION, POOLS_KEY: {}}


def _clean_pool(entry: object) -> dict[str, Any]:
    credentials: dict[str, Any] = {}
    if isinstance(entry, Mapping):
        raw = entry.get(CREDENTIALS_KEY)
        if isinstance(raw, Mapping):
            for credential_id, value in raw.items():
                if not isinstance(value, Mapping):
                    continue
                name = normalise_name(str(value.get(NAME_KEY, "")))
                if not name:
                    continue
                credentials[str(credential_id)] = {
                    NAME_KEY: name,
                    ADDED_AT_KEY: str(value.get(ADDED_AT_KEY, "")),
                }
    return {CREDENTIALS_KEY: credentials}


def _prune_empty_pools(document: dict[str, Any]) -> None:
    pools = document[POOLS_KEY]
    for pool_id in [
        pool_id for pool_id, pool in pools.items() if not pool.get(CREDENTIALS_KEY)
    ]:
        pools.pop(pool_id, None)


def _write(document: dict[str, Any], path: Path | None) -> None:
    resolved = path if path is not None else credential_names_path()
    write_json_document_atomically_if_changed(resolved, document)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
