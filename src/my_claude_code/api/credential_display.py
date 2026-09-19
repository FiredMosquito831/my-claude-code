"""One resolver for "what is this credential called", used by every surface.

A name is a display join, never a stored dimension (see
``config.credential_names``). That is only safe if *every* renderer resolves it
the same way, from the same pools, at the same moment -- otherwise a rename
would be visible on the provider card and stale in the request log, which is
exactly the kind of half-truth the join was chosen to avoid.

So there is one function. The key listing, the request log, the analytics
breakdown, the ladder rows and all three export scopes go through it, and a
rename is consistent everywhere the next time anything renders.

The index is keyed on ``mask_key_label`` -- the mask the request log, the
rollup tables and the web-search log are all keyed on -- so a caller needs only
the label a row already carries. A label two keys disagree about resolves to no
name rather than to a guess.
"""

from collections.abc import Iterator, Sequence

from my_claude_code.config.admin.values import load_value_state
from my_claude_code.config.credential_names import (
    custom_pool_id,
    env_pool_id,
    merged_label_names,
    websearch_pool_id,
)
from my_claude_code.config.credentials import parse_credential_keys
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.websearch_catalog import WEBSEARCH_CATALOG


def configured_pools() -> list[tuple[str, Sequence[str]]]:
    """Every named-able pool this machine has, as ``(pool id, secrets)``.

    Built defensively: a registry that cannot be read, or a value state that
    raises, must cost the caller a *name*, not a page. Nothing here is required
    for the numbers beside it to be correct.
    """

    return list(_iter_pools())


def credential_name_index() -> dict[str, str]:
    """Return ``masked label -> name`` across every configured pool."""

    try:
        return merged_label_names(configured_pools())
    except Exception:  # pragma: no cover - a name may never break a payload
        return {}


def _iter_pools() -> Iterator[tuple[str, Sequence[str]]]:
    try:
        state = load_value_state()
    except Exception:  # pragma: no cover - defensive
        state = {}

    def secrets_for(env_key: str) -> Sequence[str]:
        entry = state.get(env_key) or {}
        return parse_credential_keys(str(entry.get("value", "")))

    for descriptor in PROVIDER_CATALOG.values():
        env_key = descriptor.credential_env
        if env_key is None:
            continue
        keys = secrets_for(env_key)
        if keys:
            yield (env_pool_id(env_key), keys)

    for descriptor in WEBSEARCH_CATALOG.values():
        env_key = descriptor.credential_env
        if env_key is None:
            continue
        keys = secrets_for(env_key)
        if keys:
            yield (websearch_pool_id(env_key), keys)

    try:
        entries = get_provider_registry().list_custom()
    except Exception:  # pragma: no cover - defensive
        return
    for entry in entries:
        if entry.api_keys:
            yield (custom_pool_id(entry.provider_id), tuple(entry.api_keys))
