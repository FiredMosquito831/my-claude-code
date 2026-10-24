"""The Models page's marketplace rows, served beside the cached payload (7.109.0).

Three things the page draws for a provider whose profile declares a price
marketplace (InferHub), and nothing for any other provider:

* the overridable **Marketplace profile** row (the 15th override key,
  ``marketplace_profile``) on that provider's row and on each of its models'
  rows: Inherit / Force "InferHub decides" (``null``) / Force a profile by name,
  with the default always written and the red warning on the default;
* a read-only **Offers** row per model, from the market snapshot: the cheapest
  level, how many accounts, how many levels, when the snapshot was read;
* a read-only **Route note** row per model: the upstream's own prompt note,
  word for word (the user's Q15).

Served as ``marketplaces`` next to ``prompt_cache_lifetimes``, never inside the
capability half: that half is cached and pinned
(``test_metadata_ladder_equality``), and a snapshot that moves hourly must not
move it. The profile *editor* and the full offer explorer are the InferHub
page's (a later release).
"""

from collections.abc import Iterable, Mapping
from typing import Any

from my_claude_code.application.marketplace import (
    MarketSnapshot,
    current_market_snapshot,
    price_text,
)
from my_claude_code.config.marketplace_profiles import (
    FOLLOWS_THE_MARKET_NOTE,
    MARKETPLACE_DECIDES_WARNING,
    current_marketplace_profiles,
    profile_name_problem,
)
from my_claude_code.config.marketplaces import (
    DERIVED_KINDS,
    MARKETPLACE_DIALECTS,
    PROVIDER_MARKETPLACE,
    marketplace_for_provider,
    upstream_prefix,
)
from my_claude_code.config.model_overrides import MARKETPLACE_PROFILE_OVERRIDE

#: The control's kind on the page.
MARKETPLACE_PROFILE_KIND = "profile"
#: What each scope's control says.
MARKETPLACE_PROFILE_MODEL_NOTE = (
    "Which marketplace profile decides the price ceiling sent with this "
    'model\'s requests. Force unset is "InferHub decides": no ceiling at all '
    "for this model, even where the provider row names a profile."
)
MARKETPLACE_PROFILE_PROVIDER_NOTE = (
    "The profile every model of this provider uses unless its own row names "
    'another. Force unset is "InferHub decides" for the whole provider. '
    "Profiles are kept in marketplace_profiles.json in the config folder."
)
#: What the page says while nothing on InferHub can be checked against a real
#: account (the user has no key; spec §16).
MARKETPLACE_UNVERIFIED_NOTE = (
    "Built from InferHub's documentation and public pages; not verified on a "
    "real account."
)


def marketplace_profile_update_problem(value: object) -> str | None:
    """Why one Save of the 15th key cannot be stored, or ``None`` if it can.

    ``null`` (the marketplace decides) and the inherit sentinel are handled by
    the caller; a name must be well formed AND name a profile that exists --
    a row naming no profile would refuse every request it covers.
    """

    shape = profile_name_problem(value)
    if shape is not None:
        return f"{MARKETPLACE_PROFILE_OVERRIDE} {shape}"
    if current_marketplace_profiles().get(str(value)) is None:
        return (
            f"{MARKETPLACE_PROFILE_OVERRIDE} names no profile: there is no "
            f"'{value}' in marketplace_profiles.json"
        )
    return None


def _offers_line(snapshot: MarketSnapshot | None, model_id: str) -> str | None:
    if snapshot is None:
        return None
    market = snapshot.model(model_id)
    if market is None:
        return None
    lowest_in, lowest_out = market.lowest(0), market.lowest(1)
    if lowest_in is None and lowest_out is None:
        return f"no live ask · snapshot {snapshot.stamp()}"
    text = (
        f"cheapest ${price_text(lowest_in) or '-'} / "
        f"${price_text(lowest_out) or '-'} per Mtok"
    )
    if market.levels_in:
        cheapest_accounts = market.levels_in[0][1]
        accounts = sum(count for _price, count in market.levels_in)
        text += (
            f" · {cheapest_accounts} of {accounts} accounts at it"
            f" · {len(market.levels_in)} levels"
        )
    if market.official is not None:
        text += (
            f" · official ${price_text(market.official[0])} / "
            f"${price_text(market.official[1])}"
        )
    return f"{text} · snapshot {snapshot.stamp()}"


def _inferhub_models(page: Mapping[str, Any], provider_id: str) -> Iterable[str]:
    for provider in page.get("providers", []) or []:
        if str(provider.get("provider_id") or "") != provider_id:
            continue
        for model in provider.get("models", []) or []:
            ref = str(model.get("model_ref") or "")
            if ref:
                yield ref


def marketplaces_payload(page: Mapping[str, Any]) -> dict[str, Any]:
    """Everything the page's marketplace rows need, beside the cached half."""

    profiles = current_marketplace_profiles()
    dialects: dict[str, Any] = {}
    snapshots: dict[str, MarketSnapshot | None] = {}
    for dialect_id, dialect in MARKETPLACE_DIALECTS.items():
        snapshot = current_market_snapshot(dialect)
        snapshots[dialect_id] = snapshot
        payload = snapshot.payload if snapshot is not None else {}
        dialects[dialect_id] = {
            "label": dialect.label,
            "ceiling_headers": list(dialect.ceiling_headers),
            "exclude_header": dialect.exclude_header,
            "warning": MARKETPLACE_DECIDES_WARNING,
            "decides_label": f"{dialect.label} decides (no ceiling)",
            "unverified_note": MARKETPLACE_UNVERIFIED_NOTE,
            "snapshot": {
                "fetched_at": None if snapshot is None else snapshot.fetched_at,
                "stamp": "no snapshot yet" if snapshot is None else snapshot.stamp(),
                "sources": dict(payload.get("sources") or {}),
                "platform": dict(payload.get("platform") or {}),
                "combos": list(payload.get("combos") or []),
                "models": len(payload.get("models") or {}),
            },
        }
    models: dict[str, Any] = {}
    for provider_id, dialect_id in PROVIDER_MARKETPLACE.items():
        dialect = marketplace_for_provider(provider_id)
        if dialect is None:
            continue
        snapshot = snapshots.get(dialect_id)
        notes = (snapshot.payload.get("notes") if snapshot is not None else None) or {}
        for ref in _inferhub_models(page, provider_id):
            model_id = ref[len(provider_id) + 1 :]
            entry: dict[str, Any] = {}
            offers = _offers_line(snapshot, model_id)
            if offers is not None:
                entry["offers"] = offers
            note = (
                notes.get(upstream_prefix(model_id))
                if isinstance(notes, Mapping)
                else None
            )
            if isinstance(note, str):
                entry["route_note"] = note
            models[ref] = entry
    return {
        "override_key": MARKETPLACE_PROFILE_OVERRIDE,
        "providers": dict(PROVIDER_MARKETPLACE),
        "dialects": dialects,
        "profiles": [
            {
                "name": profile.name,
                "kind": profile.kind,
                "summary": profile.summary(),
                "follows_market": profile.kind in DERIVED_KINDS and not profile.has_cap,
            }
            for profile in sorted(
                profiles.profiles.values(), key=lambda item: item.name
            )
        ],
        "problems": dict(profiles.problems),
        "control": {
            "kind": MARKETPLACE_PROFILE_KIND,
            "model_note": MARKETPLACE_PROFILE_MODEL_NOTE,
            "provider_note": MARKETPLACE_PROFILE_PROVIDER_NOTE,
            "follows_market_note": FOLLOWS_THE_MARKET_NOTE,
        },
        "models": models,
    }


__all__ = [
    "MARKETPLACE_PROFILE_KIND",
    "marketplace_profile_update_problem",
    "marketplaces_payload",
]
