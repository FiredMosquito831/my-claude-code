"""Which providers on the Model Config rails cannot serve a request right now.

The Model Config page draws every route rail as the operator wrote it, and a
rail whose provider has no usable credential looked exactly like a rail that
works: on 2026-09-20 ``MODEL_FABLE`` pointed at ``chatgpt_oauth`` after its
store had been retired, every Fable request failed in the executor with "No
usable MCC-managed ChatGPT OAuth credentials found", and the page gave no sign
of it -- so the setting read as ignored rather than as unable to serve.

This answers that one question, per provider, from what the server already
holds: the settings it loaded, the OAuth stores on disk, and the key pools of
providers it has already built. Nothing here contacts an upstream, refreshes a
token, builds a provider, or writes a file (both account listings are read with
``migrate=False``). It is joined onto the config payload the dashboard already
fetches on every load, so a key added or a sign-in completed shows the next
time the page reloads its status.

Only a provider that *cannot* serve appears. A healthy one is absent, so a
dashboard that predates this field, or a page with every provider healthy,
renders byte-identically.
"""

import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from my_claude_code.config.constants import (
    ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
    CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
)
from my_claude_code.providers.anthropic_oauth.credentials import (
    credential_viability,
    load_accounts,
    load_claude_code_tokens,
)
from my_claude_code.providers.chatgpt_oauth.credentials import load_chatgpt_accounts
from my_claude_code.providers.runtime.opencode_credentials import OPENCODE_PROVIDER_ID

#: The providers whose credential is a sign-in rather than a pasted key, with
#: the setting that can still carry a raw token and the managed-store sentinel
#: that setting holds when the store is meant instead.
_SIGN_IN_PROVIDERS: dict[str, tuple[str, str]] = {
    "chatgpt_oauth": (
        "CHATGPT_OAUTH_ACCESS_TOKEN",
        CHATGPT_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
    ),
    "anthropic_oauth": (
        "ANTHROPIC_OAUTH_ACCESS_TOKEN",
        ANTHROPIC_OAUTH_MANAGED_CREDENTIAL_REFERENCE,
    ),
}

NO_CREDENTIALS = "no_credentials"
ALL_BENCHED = "all_benched"


def _chatgpt_store_usable() -> bool:
    # Records are only returned for entries that carry an access token.
    return bool(load_chatgpt_accounts(migrate=False))


def _anthropic_store_usable() -> bool:
    if any(
        credential_viability(record.tokens)[0]
        for record in load_accounts(migrate=False)
    ):
        return True
    return credential_viability(load_claude_code_tokens())[0]


_STORE_CHECKS: dict[str, Callable[[], bool]] = {
    "chatgpt_oauth": _chatgpt_store_usable,
    "anthropic_oauth": _anthropic_store_usable,
}


def sign_in_usable(provider_id: str, values: Mapping[str, Mapping[str, Any]]) -> bool:
    """Whether a sign-in provider has any credential the executor could use.

    Mirrors the executor's own order: a raw token in the setting is used as
    is; otherwise the managed store (and, for Claude, Claude Code's file) must
    hold one. Presence and local expiry only -- a refresh is never attempted.
    """

    env_key, reference = _SIGN_IN_PROVIDERS[provider_id]
    raw = str(values.get(env_key, {}).get("value", "") or "").strip()
    if raw and raw != reference:
        return True
    try:
        return _STORE_CHECKS[provider_id]()
    except Exception:
        # An unreadable store is a store with nothing usable in it, which is
        # what the executor concludes too.
        return False


def benched_until(
    slots: Iterable[Mapping[str, Any]], wait: float | None, now: float
) -> float | None:
    """When the first key of a pool is back, if every key is benched now.

    ``wait`` is the provider's own ``throttle_remaining()``: 0 whenever the
    rotation engine could hand a key out this instant, else the shortest wait
    until one can. It is the deciding answer because it applies the engine's
    own selection rules, policy included, rather than a second reading of the
    slot table. The slot states only separate a bench from a client-side
    throttle, which also makes ``wait`` positive while a key is healthy.

    ``None`` when the pool is empty, any key is healthy, or a key can serve.
    """

    states = [slot.get("state") for slot in slots]
    if not states or "HEALTHY" in states:
        return None
    try:
        left = float(wait or 0.0)
    except TypeError, ValueError:
        return None
    if left <= 0:
        return None
    return now + left


def credential_problems(
    provider_status: Iterable[Mapping[str, Any]],
    values: Mapping[str, Mapping[str, Any]],
    pool_health: Callable[[str], Mapping[str, Any] | None],
    *,
    now: float | None = None,
) -> dict[str, dict[str, Any]]:
    """``{provider_id: problem}`` for every provider that cannot serve now.

    ``pool_health(provider_id)`` is ``{"slots": key_health(), "wait":
    throttle_remaining()}`` for a provider the server has already built, and
    ``None`` for one it has not (which has no bench state to report).
    """

    stamp = time.time() if now is None else now
    problems: dict[str, dict[str, Any]] = {}
    for entry in provider_status:
        provider_id = str(entry.get("provider_id") or "")
        if not provider_id or entry.get("kind") == "local":
            continue
        name = str(entry.get("display_name") or provider_id)
        if provider_id in _SIGN_IN_PROVIDERS:
            if not sign_in_usable(provider_id, values):
                problems[provider_id] = {
                    "state": NO_CREDENTIALS,
                    "action": "sign_in",
                    "display_name": name,
                }
                continue
        # OpenCode Zen with no key still serves its zero-cost models on the
        # shared public credential, so a missing key there is not "cannot serve".
        elif (
            entry.get("status") == "missing_key" and provider_id != OPENCODE_PROVIDER_ID
        ):
            problems[provider_id] = {
                "state": NO_CREDENTIALS,
                "action": "add_key",
                "display_name": name,
            }
            continue
        pool = pool_health(provider_id) or {}
        slots = list(pool.get("slots") or [])
        until = benched_until(slots, pool.get("wait"), stamp)
        if until is not None:
            problems[provider_id] = {
                "state": ALL_BENCHED,
                "until": round(until, 1),
                "keys": len(slots),
                "display_name": name,
            }
    return problems


def config_changed_at(managed_path: str | None) -> float | None:
    """When the managed settings file was last written, or ``None``."""

    if not managed_path:
        return None
    try:
        return round(Path(managed_path).stat().st_mtime, 1)
    except OSError:
        return None
