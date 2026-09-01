"""Rewriting the ``MODEL*`` route settings that name one provider.

A static provider can never leave ``PROVIDER_CATALOG``, so no route setting can
ever name a provider that stopped existing. A custom provider can, and until
now the two ways of making it stop existing had the same consequence: the next
``Settings()`` raised ``ValidationError`` on every route that named it, which
is the whole process rather than the one route.

The two gestures are answered differently, because the user means different
things by them:

**Delete** is permanent, so the references go with it. Every ``MODEL*`` key
losing a ref is listed back to the caller -- a silent rewrite of somebody's
routing is not an improvement on a crash.

**Disable** is temporary, so the references stay exactly where they are and are
switched off instead, through the pause mechanism the Model Config page already
owns. Re-enabling removes precisely the pauses the disable added, which is why
they are recorded rather than recomputed: a ref the operator paused by hand
before disabling must still be paused afterwards.
"""

from my_claude_code.config.admin.manifest import FIELD_BY_KEY
from my_claude_code.config.admin.values import ROUTE_PAUSE_KEYS
from my_claude_code.config.model_refs import (
    format_model_ref_list,
    parse_model_ref_list,
)
from my_claude_code.config.settings import Settings

PausedPair = tuple[str, str]
"""One ``(MODEL_*_PAUSED key, model ref)`` pause this module added."""


def _text(settings: Settings, key: str) -> str:
    field = FIELD_BY_KEY[key]
    attr = field.settings_attr
    if attr is None:
        return ""
    return str(getattr(settings, attr, "") or "")


def _names(model_ref: str, provider_id: str) -> bool:
    return model_ref.split("/", 1)[0] == provider_id


def updates_removing_provider(
    settings: Settings, provider_id: str
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Return the ``MODEL*`` writes that erase ``provider_id``, and what went.

    A primary key is cleared rather than rewritten: there is no honest
    replacement for "the model this route points at", and an empty managed
    value is how every other field says "unset me back to the default".
    """
    updates: dict[str, str] = {}
    removed: list[str] = []
    for model_key, chain_key, paused_key in ROUTE_PAUSE_KEYS:
        primary = _text(settings, model_key).strip()
        if primary and _names(primary, provider_id):
            updates[model_key] = ""
            removed.append(f"{model_key}={primary}")
        for key in (chain_key, paused_key):
            refs = parse_model_ref_list(_text(settings, key))
            kept = tuple(ref for ref in refs if not _names(ref, provider_id))
            if kept != refs:
                updates[key] = format_model_ref_list(kept)
                removed.extend(
                    f"{key}={ref}" for ref in refs if _names(ref, provider_id)
                )
    return updates, tuple(removed)


def updates_pausing_provider(
    settings: Settings, provider_id: str
) -> tuple[dict[str, str], tuple[PausedPair, ...]]:
    """Return the pause writes that switch ``provider_id`` off on every route.

    The second element is the pauses this call is responsible for -- only
    those, never the ones already there -- so that re-enabling can undo its own
    work and nobody else's.
    """
    updates: dict[str, str] = {}
    added: list[PausedPair] = []
    for model_key, chain_key, paused_key in ROUTE_PAUSE_KEYS:
        chain: list[str] = []
        primary = _text(settings, model_key).strip()
        if primary:
            chain.append(primary)
        chain.extend(parse_model_ref_list(_text(settings, chain_key)))
        paused = list(parse_model_ref_list(_text(settings, paused_key)))
        changed = False
        for ref in chain:
            if not _names(ref, provider_id) or ref in paused:
                continue
            paused.append(ref)
            added.append((paused_key, ref))
            changed = True
        if changed:
            updates[paused_key] = format_model_ref_list(tuple(paused))
    return updates, tuple(added)


def updates_unpausing(
    settings: Settings, pairs: tuple[PausedPair, ...]
) -> dict[str, str]:
    """Return the pause writes that lift exactly ``pairs``."""
    updates: dict[str, str] = {}
    by_key: dict[str, set[str]] = {}
    for paused_key, model_ref in pairs:
        by_key.setdefault(paused_key, set()).add(model_ref)
    for paused_key, refs in by_key.items():
        if paused_key not in FIELD_BY_KEY:
            continue
        current = parse_model_ref_list(_text(settings, paused_key))
        kept = tuple(ref for ref in current if ref not in refs)
        if kept != current:
            updates[paused_key] = format_model_ref_list(kept)
    return updates
