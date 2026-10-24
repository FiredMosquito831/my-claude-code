"""The InferHub page's payloads, built from the stores PR A created (7.110.0).

The page is a dedicated admin view for a price marketplace (spec §9.1),
driven by the declared dialect so a second marketplace is one more tab of the
same code. Everything it shows is read from what already exists:

* the profile store (``config/marketplace_profiles.py``) -- listed, and edited
  as a whole through its own owner-only writer;
* the override rows' 15th key (``marketplace_profile``) -- shown here, and
  written only through the Models page's existing override route, the one
  write path;
* the market snapshot (``application/marketplace.py``) -- the price ladder,
  the speed and cache columns, the route notes and the combos;
* the account's own reads (``config/marketplace_reads.py``), made on page
  open and on Refresh only;
* the request log -- the last requests the marketplace served, with the
  ceiling MCC sent and the charge InferHub reported.

Pure builders: no network, no file write, no request path. Every number
the marketplace did not state stays ``None`` -- "not reported", drawn as a
dash -- never a zero (III.8).
"""

import hashlib
import json
import math
import re
import threading
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from my_claude_code.application.errors import InvalidRequestError
from my_claude_code.application.marketplace import (
    FROM_MODEL_OVERRIDE,
    FROM_PROVIDER_OVERRIDE,
    MarketModel,
    MarketSnapshot,
    price_text,
    profile_choice,
)
from my_claude_code.config.credentials import parse_credential_keys
from my_claude_code.config.marketplace_profiles import (
    FIELD_CAP_INPUT,
    FIELD_CAP_OUTPUT,
    FIELD_CAP_PERCENT,
    FIELD_EXCLUDE,
    FIELD_MAX_INPUT,
    FIELD_MAX_OUTPUT,
    FIELD_MAX_PER_REQUEST,
    FIELD_MIN_ACCOUNTS,
    FIELD_PERCENT,
    FOLLOWS_THE_MARKET_NOTE,
    KIND_FIELDS,
    MARKETPLACE_DECIDES_WARNING,
    PROFILE_NAME_PATTERN,
    MarketplaceProfile,
    MarketplaceProfiles,
)
from my_claude_code.config.marketplace_reads import (
    READ_BUDGET_ALIASES,
    READ_BUDGET_DEFAULT,
    READ_BUDGETS,
    READ_KEYS,
    READ_USAGE,
    account_reads_for,
)
from my_claude_code.config.marketplaces import (
    DERIVED_KINDS,
    KIND_CHEAPEST_LEVEL_MIN_ACCOUNTS,
    KIND_CHEAPEST_LEVEL_NOW,
    KIND_MARKETPLACE_DECIDES,
    KIND_MAX_PRICE,
    KIND_PCT_OF_OFFICIAL,
    PROFILE_KINDS,
    PROVIDER_MARKETPLACE,
    MarketplaceDialect,
    marketplace_for_provider,
    upstream_prefix,
)
from my_claude_code.config.model_overrides import (
    MARKETPLACE_PROFILE_OVERRIDE,
    ModelParameterOverrides,
    model_ref_for,
    normalize_override_key,
)
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import Settings

#: What every keyed block says while nothing has been seen on a real account
#: (the user has no InferHub key; spec §16).
UNVERIFIED_NOTE = (
    "Built from InferHub's documentation and public pages; not verified on a "
    "real account."
)
#: A read the key may not make: never a zero, never a blank read as one.
NOT_READABLE = "not readable with this key"
#: The setting the Snapshot block surfaces beside its card on Providers.
PUBLIC_MARKET_SETTING = "MARKETPLACE_PUBLIC_MARKET"
#: The longest list of "would match now" lines one profile carries.
MAX_MATCH_LINES = 400
#: The most requests one "last N" read returns.
MAX_RECENT = 100
#: A price compared with this much slack (the request path's own epsilon).
_EPSILON = 1e-9
#: A decimal string as the management surface writes money ("12.500000").
_DECIMAL = re.compile(r"-?\d{1,15}(?:\.\d{1,12})?")


def _mapping(value: Any) -> Mapping[str, Any]:
    """``value`` when it is an object, else an empty one (untrusted shapes)."""

    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


# --------------------------------------------------------------- the editor

#: Each kind, in the page's words (the store's kinds, PR A's -- no new one).
KIND_WORDS: Mapping[str, tuple[str, str]] = {
    KIND_MARKETPLACE_DECIDES: (
        "InferHub decides (no ceiling)",
        "Nothing is sent: InferHub picks a seller regardless of price.",
    ),
    KIND_PCT_OF_OFFICIAL: (
        "At most N % of official",
        "Both sides capped at this share of each model's own official price. "
        "One number that means the same on a cheap model and a dear one.",
    ),
    KIND_MAX_PRICE: (
        "At most $X / $Y per Mtok",
        "Your dollar ceilings per million tokens; a side left empty is not capped.",
    ),
    KIND_CHEAPEST_LEVEL_NOW: (
        "The cheapest level now",
        "Re-read from the market snapshot on every request; it follows the "
        "market up as well as down unless you add a cap.",
    ),
    KIND_CHEAPEST_LEVEL_MIN_ACCOUNTS: (
        "The cheapest level with at least N accounts",
        "The cheapest price with at least N seller accounts at or below it, "
        "re-read on every request; add a cap to be sure.",
    ),
}

#: Each field, in the page's words: label, unit, help.
FIELD_WORDS: Mapping[str, tuple[str, str, str]] = {
    FIELD_PERCENT: (
        "Percent of official",
        "%",
        "1 to 100; InferHub caps every ask at 50.",
    ),
    FIELD_MAX_INPUT: ("Input ceiling", "$ / Mtok", "Empty: input is not capped."),
    FIELD_MAX_OUTPUT: ("Output ceiling", "$ / Mtok", "Empty: output is not capped."),
    FIELD_MIN_ACCOUNTS: (
        "At least N accounts",
        "accounts",
        "Whole number; no default.",
    ),
    FIELD_CAP_PERCENT: (
        "Cap: percent of official",
        "%",
        "A cap of your own; or the two dollar caps, not both.",
    ),
    FIELD_CAP_INPUT: ("Cap: input", "$ / Mtok", "A dollar cap on the input side."),
    FIELD_CAP_OUTPUT: ("Cap: output", "$ / Mtok", "A dollar cap on the output side."),
    FIELD_EXCLUDE: (
        "Never these upstreams",
        "prefixes",
        "Comma-separated upstream prefixes (kiro, cb ...), sent as x-ignore-upstream.",
    ),
    FIELD_MAX_PER_REQUEST: (
        "At most $ per request",
        "$",
        "Refuse a request whose worst case at the ceiling is above this.",
    ),
}


def editor_payload() -> dict[str, Any]:
    """What the profile editor draws and validates with: the store's own rules."""

    kinds = []
    for kind in PROFILE_KINDS:
        label, help_text = KIND_WORDS[kind]
        kinds.append(
            {
                "kind": kind,
                "label": label,
                "help": help_text,
                "fields": sorted(KIND_FIELDS[kind]),
                "derived": kind in DERIVED_KINDS,
            }
        )
    return {
        "kinds": kinds,
        "fields": {
            name: {"label": label, "unit": unit, "help": help_text}
            for name, (label, unit, help_text) in FIELD_WORDS.items()
        },
        "name_pattern": PROFILE_NAME_PATTERN.pattern,
        "follows_market_note": FOLLOWS_THE_MARKET_NOTE,
        "warning": MARKETPLACE_DECIDES_WARNING,
    }


# ---------------------------------------------------------------- the store


def store_digest(raw: bytes | None) -> str:
    """The store file as the page last read it; ``""`` when there is no file."""

    return "" if raw is None else hashlib.sha256(raw).hexdigest()


def store_state(raw: bytes | None, profiles: MarketplaceProfiles) -> dict[str, Any]:
    """Whether the file can be written from the page without losing anything.

    An unreadable file, or entries MCC cannot use, are shown word for word and
    never dropped by a Save the operator did not confirm.
    """

    unreadable: str | None = None
    raw_profiles: Mapping[str, Any] = {}
    if raw is not None and raw.strip():
        try:
            document = json.loads(raw.decode("utf-8"))
        except UnicodeDecodeError, ValueError:
            unreadable = "the file is not valid JSON"
        else:
            if not isinstance(document, Mapping):
                unreadable = "the file's top level is not an object"
            else:
                section = document.get("profiles")
                if section is not None and not isinstance(section, Mapping):
                    unreadable = "its 'profiles' is not an object"
                elif isinstance(section, Mapping):
                    raw_profiles = section
    unusable = [
        {
            "name": name,
            "why": why,
            "document": json.dumps(raw_profiles.get(name), sort_keys=True)[:600],
        }
        for name, why in sorted(profiles.problems.items())
    ]
    return {
        "digest": store_digest(raw),
        "exists": raw is not None,
        "unreadable": unreadable,
        "unusable": unusable,
        "file_name": "marketplace_profiles.json",
    }


# ------------------------------------------------------------- assignments


def _row_state(row: Mapping[str, Any] | None) -> dict[str, Any]:
    if row is None or MARKETPLACE_PROFILE_OVERRIDE not in row:
        return {"state": "inherit", "value": None}
    value = row[MARKETPLACE_PROFILE_OVERRIDE]
    if value is None:
        return {"state": "unset", "value": None}
    return {"state": "value", "value": str(value)}


def assignments_for(
    overrides: ModelParameterOverrides, provider_id: str
) -> dict[str, Any]:
    """The provider row and every model row of one provider that state the 15th key."""

    prefix = normalize_override_key(provider_id) + "/"
    models = []
    for ref, row in sorted(overrides.models.items()):
        if not ref.startswith(prefix) or MARKETPLACE_PROFILE_OVERRIDE not in row:
            continue
        models.append(
            {"model_ref": ref, "model_id": ref[len(prefix) :], **_row_state(row)}
        )
    return {
        "provider": _row_state(
            overrides.providers.get(normalize_override_key(provider_id))
        ),
        "models": models,
    }


def _resolved(
    overrides: ModelParameterOverrides, provider_id: str, model_id: str
) -> tuple[str | None, str] | None:
    """``(profile name or None, origin)`` for one model, or ``None`` = nothing stated."""

    model_row, provider_row = overrides.marketplace_profile_rows(
        provider_id, model_ref_for(provider_id, model_id)
    )
    if model_row is not None:
        return model_row.name, FROM_MODEL_OVERRIDE
    if provider_row is not None:
        return provider_row.name, FROM_PROVIDER_OVERRIDE
    return None


def rows_naming(overrides: ModelParameterOverrides, name: str) -> list[dict[str, str]]:
    """Every override row, on any provider, whose 15th key names ``name``."""

    rows: list[dict[str, str]] = []
    for scope, table in (
        ("provider", overrides.providers),
        ("model", overrides.models),
    ):
        for key, row in sorted(table.items()):
            if row.get(MARKETPLACE_PROFILE_OVERRIDE) == name:
                rows.append({"scope": scope, "key": key})
    return rows


# --------------------------------------------------------- would match now


def _accounts_under(
    levels: Sequence[tuple[float, int]], ceiling: float | None
) -> tuple[int, int]:
    total = sum(count for _price, count in levels)
    if ceiling is None:
        return total, total
    under = sum(count for price, count in levels if price <= ceiling + _EPSILON)
    return under, total


def _pair(first: float | None, second: float | None) -> list[float | None]:
    return [first, second]


def _usd(value: float | None) -> str:
    return "uncapped" if value is None else f"${price_text(value)}"


def would_match(
    dialect: MarketplaceDialect,
    profile: MarketplaceProfile,
    model_id: str,
    snapshot: MarketSnapshot | None,
    origin: str = FROM_PROVIDER_OVERRIDE,
) -> dict[str, Any]:
    """What one profile would send for one model right now, and what it reaches.

    ``state``: ``matches`` (the ceiling and how many seller accounts ask at or
    under it), ``nothing`` (a request would be refused -- the refusal's own
    words), ``decides`` (no ceiling: the red warning), or ``unknown`` (a
    ceiling is sent but the snapshot holds no levels to count -- InferHub
    still enforces it).
    """

    out: dict[str, Any] = {"model_id": model_id, "origin": origin}
    try:
        choice = profile_choice(dialect, profile, model_id, origin=origin)
    except InvalidRequestError as exc:
        out.update({"state": "nothing", "text": f"matches nothing: {exc}"})
        return out
    if choice is None:
        out.update({"state": "decides", "text": MARKETPLACE_DECIDES_WARNING})
        return out
    market: MarketModel | None = snapshot.model(model_id) if snapshot else None
    out["ceiling"] = _pair(choice.ceiling_in, choice.ceiling_out)
    out["derived_from"] = choice.derived_from
    out["headers"] = dict(choice.headers)
    levels = (market.levels_in if market is not None else ()) or (
        market.levels_out if market is not None else ()
    )
    side_ceiling = (
        choice.ceiling_in
        if market is not None and market.levels_in
        else choice.ceiling_out
    )
    if not levels:
        out.update(
            {
                "state": "unknown",
                "text": (
                    f"sends {choice.ceiling_text()}; the snapshot holds no price "
                    "levels for this model, so how many accounts it reaches is "
                    "not known. InferHub enforces it either way."
                ),
            }
        )
        return out
    under, total = _accounts_under(levels, side_ceiling)
    cheapest = levels[0]
    out.update(
        {
            "state": "matches",
            "accounts": under,
            "of_accounts": total,
            "cheapest": [market.lowest(0), market.lowest(1)] if market else None,
            "text": (
                f"{choice.ceiling_text()}: {under} of {total} accounts ask at or "
                f"under it (cheapest {_usd(cheapest[0])}, {cheapest[1]} accounts)"
            ),
        }
    )
    return out


def profile_entries(
    profiles: MarketplaceProfiles,
    overrides: ModelParameterOverrides,
    catalogue: Mapping[str, Iterable[str]],
    snapshots: Mapping[str, MarketSnapshot | None],
) -> list[dict[str, Any]]:
    """Every profile, its rows, and per assigned model what it would match now."""

    out = []
    for name, profile in sorted(profiles.profiles.items()):
        matches: list[dict[str, Any]] = []
        for provider_id, dialect_id in PROVIDER_MARKETPLACE.items():
            dialect = marketplace_for_provider(provider_id)
            if dialect is None:
                continue
            snapshot = snapshots.get(dialect_id)
            models = set(catalogue.get(provider_id, ()))
            prefix = normalize_override_key(provider_id) + "/"
            models.update(
                ref[len(prefix) :] for ref in overrides.models if ref.startswith(prefix)
            )
            for model_id in sorted(models):
                resolved = _resolved(overrides, provider_id, model_id)
                if resolved is None or resolved[0] != name:
                    continue
                if len(matches) >= MAX_MATCH_LINES:
                    break
                entry = would_match(dialect, profile, model_id, snapshot, resolved[1])
                entry["provider_id"] = provider_id
                matches.append(entry)
        counts = {
            state: sum(1 for entry in matches if entry["state"] == state)
            for state in ("matches", "nothing", "unknown", "decides")
        }
        out.append(
            {
                "name": name,
                "kind": profile.kind,
                "document": profile.as_document(),
                "summary": profile.summary(),
                "follows_market": profile.kind in DERIVED_KINDS and not profile.has_cap,
                "rows": rows_naming(overrides, name),
                "would_match": matches,
                "counts": counts,
            }
        )
    return out


# ------------------------------------------------------------ the snapshot

_SOURCE_WORDS: Mapping[str, str] = {
    "listing": "your key's model list (price levels, official prices)",
}


def _age_seconds(stamp: str | None, now: datetime) -> float | None:
    if not stamp:
        return None
    try:
        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (now - when).total_seconds())


def _status_words(status: str, *, public_market: bool, keyed: bool) -> str:
    if status == "ok":
        return "read"
    if status == "off":
        return (
            f"off ({PUBLIC_MARKET_SETTING} is off)"
            if not public_market and not keyed
            else "off"
        )
    if status == "empty":
        return "no list rows yet (the next models refresh fills it)"
    if status in ("http_401", "http_403"):
        return f"{NOT_READABLE} ({status[5:]}); the last copy read is kept"
    if status.startswith("http_"):
        return f"answered {status[5:]}; the last copy read is kept"
    if status.startswith("failed:"):
        return f"could not be read ({status[7:]}); the last copy read is kept"
    return status


def snapshot_block(
    dialect: MarketplaceDialect,
    snapshot: MarketSnapshot | None,
    *,
    public_market: bool,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Age, per-source state, the platform cap and the setting."""

    moment = now or datetime.now(UTC)
    payload = snapshot.payload if snapshot is not None else {}
    statuses = payload.get("sources") if isinstance(payload, Mapping) else None
    statuses = statuses if isinstance(statuses, Mapping) else {}
    sources = []
    declared = {source.name: source for source in dialect.market_sources}
    for name in ["listing", *declared]:
        source = declared.get(name)
        status = statuses.get(name)
        keyed = source.keyed if source is not None else True
        sources.append(
            {
                "name": name,
                "status": status if isinstance(status, str) else None,
                "keyed": keyed,
                "purpose": source.purpose
                if source is not None
                else _SOURCE_WORDS[name],
                "words": (
                    _status_words(status, public_market=public_market, keyed=keyed)
                    if isinstance(status, str)
                    else "not read yet"
                ),
            }
        )
    platform = payload.get("platform") if isinstance(payload, Mapping) else None
    cap = (
        platform.get("max_ask_pct_of_official")
        if isinstance(platform, Mapping)
        else None
    )
    fetched_at = snapshot.fetched_at if snapshot is not None else None
    return {
        "fetched_at": fetched_at,
        "stamp": snapshot.stamp() if snapshot is not None else "no snapshot yet",
        "age_seconds": _age_seconds(fetched_at, moment),
        "sources": sources,
        "platform_cap": cap if isinstance(cap, int | float) else None,
        "models": len(snapshot.models) if snapshot is not None else 0,
        "public_market": public_market,
        "public_market_setting": PUBLIC_MARKET_SETTING,
    }


# ---------------------------------------- "offers moved since" (in memory)

_seen_lock = threading.Lock()
#: dialect id -> (fetched_at, {model: (lowest in, lowest out)}) last seen.
_SEEN: dict[str, tuple[str, dict[str, tuple[float | None, float | None]]]] = {}
#: dialect id -> the last movement observed between two snapshots.
_MOVED: dict[str, dict[str, Any]] = {}


def reset_market_memory() -> None:
    """Forget what the page has seen (tests)."""

    with _seen_lock:
        _SEEN.clear()
        _MOVED.clear()


def note_snapshot(
    dialect: MarketplaceDialect, snapshot: MarketSnapshot | None
) -> dict[str, Any] | None:
    """Compare this snapshot with the last one this server's page saw.

    Process memory only -- no store -- so "offers moved since" means since
    the snapshot this page last showed. Returns the latest movement, kept
    until the next snapshot that differs.
    """

    if snapshot is None or not snapshot.fetched_at:
        with _seen_lock:
            return _MOVED.get(dialect.dialect_id)
    lowest = {
        model_id: (market.lowest(0), market.lowest(1))
        for model_id, market in snapshot.models.items()
    }
    with _seen_lock:
        seen = _SEEN.get(dialect.dialect_id)
        if seen is not None and seen[0] != snapshot.fetched_at:
            before = seen[1]
            moves = {
                model_id: {"before": list(before[model_id]), "after": list(now)}
                for model_id, now in lowest.items()
                if model_id in before and before[model_id] != now
            }
            if moves:
                since = MarketSnapshot(fetched_at=seen[0]).stamp()
                _MOVED[dialect.dialect_id] = {
                    "since": since,
                    "until": snapshot.stamp(),
                    "models": moves,
                }
        _SEEN[dialect.dialect_id] = (snapshot.fetched_at, lowest)
        return _MOVED.get(dialect.dialect_id)


# --------------------------------------------------------------- the account

_account_lock = threading.Lock()
#: provider id -> (when read, ISO; what the reads answered). Read on page open
#: and Refresh only; a re-render after a Save shows this copy and says when.
_ACCOUNT: dict[str, tuple[str, dict[str, Any]]] = {}


def remember_account(provider_id: str, answer: Mapping[str, Any]) -> None:
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _account_lock:
        _ACCOUNT[provider_id] = (stamp, dict(answer))


def remembered_account(provider_id: str) -> tuple[str, dict[str, Any]] | None:
    with _account_lock:
        return _ACCOUNT.get(provider_id)


def reset_account_memory() -> None:
    with _account_lock:
        _ACCOUNT.clear()


def _read_state(answer: Mapping[str, Any] | None) -> tuple[str, str]:
    """``(state, words)`` for one read: read / not_readable / failed / not_read."""

    if answer is None:
        return "not_read", "not read"
    status = str(answer.get("status") or "")
    if status == "ok":
        return "read", "read"
    if status in ("http_401", "http_403"):
        code = answer.get("code")
        detail = f", {code}" if isinstance(code, str) else ""
        return "not_readable", f"{NOT_READABLE} ({status[5:]}{detail})"
    if status.startswith("http_"):
        return "failed", f"InferHub answered {status[5:]}"
    if status.startswith("failed:"):
        return "failed", f"could not be read ({status[7:]})"
    return "failed", status or "not read"


def _decimal_text(value: Any) -> str | None:
    """A management-surface money value (a decimal string), kept as written."""

    if isinstance(value, str):
        text = value.strip()
        # A number as the marketplace wrote it, or nothing: never arbitrary text.
        return text if _DECIMAL.fullmatch(text) else None
    if isinstance(value, int | float) and not isinstance(value, bool):
        number = float(value)
        return price_text(number) if math.isfinite(number) else None
    return None


def _usage_part(body: Any) -> dict[str, Any] | None:
    if not isinstance(body, Mapping):
        return None
    balance = body.get("balance")
    window = body.get("window")
    all_time = body.get("all_time")
    return {
        "currency": body.get("currency")
        if isinstance(body.get("currency"), str)
        else None,
        "balance": _decimal_text(balance.get("amount_usdc"))
        if isinstance(balance, Mapping)
        else None,
        "balance_at": balance.get("updated_at")
        if isinstance(balance, Mapping) and isinstance(balance.get("updated_at"), str)
        else None,
        "window": window.get("kind")
        if isinstance(window, Mapping) and isinstance(window.get("kind"), str)
        else None,
        "window_spend": _decimal_text(window.get("spend_usdc"))
        if isinstance(window, Mapping)
        else None,
        "window_requests": window.get("requests")
        if isinstance(window, Mapping) and isinstance(window.get("requests"), int)
        else None,
        "all_time_spend": _decimal_text(all_time.get("spend_usdc"))
        if isinstance(all_time, Mapping)
        else None,
        "all_time_requests": all_time.get("requests")
        if isinstance(all_time, Mapping) and isinstance(all_time.get("requests"), int)
        else None,
    }


def _budget_rows(body: Any) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The budgets set on InferHub, and its catalog uuid -> model id map."""

    items = body if isinstance(body, list) else []
    rows: list[dict[str, Any]] = []
    ids: dict[str, str] = {}
    for item in items:
        if not isinstance(item, Mapping):
            continue
        prefix, model = item.get("prefix"), item.get("upstreamModelId")
        if not isinstance(prefix, str) or not isinstance(model, str):
            continue
        model_id = f"{prefix}/{model}"
        uuid = item.get("upstreamCatalogModelId")
        if isinstance(uuid, str):
            ids[uuid] = model_id
        caps = {
            "max_input": _decimal_text(item.get("maxInputPerMtok")),
            "max_output": _decimal_text(item.get("maxOutputPerMtok")),
            "min_discount_pct": _decimal_text(item.get("minDiscountPct")),
            "max_discount_pct": _decimal_text(item.get("maxDiscountPct")),
            "floor_min_providers": item.get("floorMinProviders")
            if isinstance(item.get("floorMinProviders"), int)
            else None,
        }
        if not any(value is not None for value in caps.values()):
            continue
        rows.append(
            {
                "model_id": model_id,
                **caps,
                "enabled": item.get("enabled")
                if isinstance(item.get("enabled"), bool)
                else None,
            }
        )
    return rows, ids


def _alias_groups(body: Any, ids: Mapping[str, str]) -> list[dict[str, Any]]:
    items = body if isinstance(body, list) else []
    groups = []
    for item in items:
        if not isinstance(item, Mapping) or item.get("isAlias") is not True:
            continue
        alias = item.get("alias")
        if not isinstance(alias, str):
            continue
        members = item.get("memberModelIds")
        named = sorted(
            {
                ids[member]
                for member in (members if isinstance(members, list) else [])
                if isinstance(member, str) and member in ids
            }
        )
        groups.append(
            {
                "alias": alias,
                "label": item.get("label")
                if isinstance(item.get("label"), str)
                else alias,
                "members": named,
                "member_count": item.get("memberCount")
                if isinstance(item.get("memberCount"), int)
                else None,
            }
        )
    return sorted(groups, key=lambda group: group["alias"])


_KEY_KIND_WORDS: Mapping[str, str] = {
    "inference": "inference (inference + management)",
    "inference_only": "inference only (management reads refused)",
    "config": "configuration (management only)",
    "config_read": "configuration, read-only",
}


def account_block(
    provider_id: str, has_key: bool, remembered: tuple[str, Mapping[str, Any]] | None
) -> dict[str, Any]:
    """The Account block: what the last page-open or Refresh read, in its words."""

    base: dict[str, Any] = {"unverified": UNVERIFIED_NOTE}
    if not has_key:
        return {
            **base,
            "state": "no_key",
            "words": "No InferHub key is set: nothing is read.",
        }
    if remembered is None:
        return {
            **base,
            "state": "not_read",
            "words": "Not read yet: open the page or press Refresh.",
        }
    read_at, answer = remembered
    state = str(answer.get("state") or "")
    if state != "read":
        words = {
            "no_key": "No InferHub key is set: nothing is read.",
            "not_sent": str(answer.get("words") or "the proxy chain sent nothing"),
            "off": "This provider declares no account reads.",
        }.get(state, "The account could not be read.")
        return {**base, "state": state or "failed", "words": words, "read_at": read_at}
    reads = answer.get("reads")
    reads = reads if isinstance(reads, Mapping) else {}

    def part(name: str) -> tuple[str, str, Any]:
        one = reads.get(name)
        state_, words = _read_state(one if isinstance(one, Mapping) else None)
        body = one.get("body") if isinstance(one, Mapping) else None
        return state_, words, body

    usage_state, usage_words, usage_body = part(READ_USAGE)
    budgets_state, budgets_words, budgets_body = part(READ_BUDGETS)
    aliases_state, aliases_words, aliases_body = part(READ_BUDGET_ALIASES)
    default_state, default_words, default_body = part(READ_BUDGET_DEFAULT)
    keys_state, keys_words, _keys_body = part(READ_KEYS)
    budget_rows, ids = (
        _budget_rows(budgets_body) if budgets_state == "read" else ([], {})
    )
    kind = answer.get("key_kind")
    key_kind = None
    if isinstance(kind, Mapping) and isinstance(kind.get("kind"), str):
        name = str(kind["kind"])
        key_kind = {
            "kind": name,
            "words": _KEY_KIND_WORDS.get(name, name),
            "how": "from your key list"
            if kind.get("how") == "key_list"
            else "from a refusal",
        }
    default = None
    if default_state == "read" and isinstance(default_body, Mapping):
        default = {
            "min_discount_pct": _decimal_text(default_body.get("minDiscountPct")),
            "queue_wait_seconds": default_body.get("queueWaitSeconds")
            if isinstance(default_body.get("queueWaitSeconds"), int)
            else None,
        }
    return {
        **base,
        "state": "read",
        "read_at": read_at,
        "usage": {
            "state": usage_state,
            "words": usage_words,
            **({"values": _usage_part(usage_body)} if usage_state == "read" else {}),
        },
        "key_kind": {
            "state": "read" if key_kind else keys_state,
            "words": key_kind["words"] if key_kind else keys_words,
            **({"kind": key_kind["kind"], "how": key_kind["how"]} if key_kind else {}),
        },
        "budgets": {
            "state": budgets_state,
            "words": budgets_words,
            "rows": budget_rows,
            "default": {
                "state": default_state,
                "words": default_words,
                "values": default,
            },
        },
        "aliases": {
            "state": aliases_state if budgets_state == "read" else budgets_state,
            "words": aliases_words if budgets_state == "read" else budgets_words,
            "groups": _alias_groups(aliases_body, ids)
            if aliases_state == "read" and budgets_state == "read"
            else [],
        },
    }


# ----------------------------------------------------------------- the page


def provider_has_key(settings: Settings, provider_id: str) -> bool:
    descriptor = PROVIDER_CATALOG.get(provider_id)
    if descriptor is None or not descriptor.credential_attr:
        return False
    value = getattr(settings, descriptor.credential_attr, "")
    return bool(parse_credential_keys(value if isinstance(value, str) else ""))


def marketplace_views(settings: Settings) -> list[dict[str, Any]]:
    """One entry per marketplace provider: the sidebar shows those with a key."""

    views = []
    for provider_id, dialect_id in PROVIDER_MARKETPLACE.items():
        dialect = marketplace_for_provider(provider_id)
        descriptor = PROVIDER_CATALOG.get(provider_id)
        if dialect is None or descriptor is None:
            continue
        views.append(
            {
                "provider_id": provider_id,
                "dialect_id": dialect_id,
                "label": dialect.label,
                "display_name": descriptor.display_name,
                "has_key": provider_has_key(settings, provider_id),
                "account_reads": account_reads_for(dialect_id) is not None,
            }
        )
    return views


def _snapshot_combos(snapshot: MarketSnapshot | None) -> list[dict[str, Any]]:
    payload = snapshot.payload if snapshot is not None else {}
    combos = payload.get("combos") if isinstance(payload, Mapping) else None
    return (
        [combo for combo in combos if isinstance(combo, Mapping)]
        if isinstance(combos, list)
        else []
    )


def _snapshot_notes(snapshot: MarketSnapshot | None) -> dict[str, str]:
    payload = snapshot.payload if snapshot is not None else {}
    notes = payload.get("notes") if isinstance(payload, Mapping) else None
    if not isinstance(notes, Mapping):
        return {}
    return {str(key): value for key, value in notes.items() if isinstance(value, str)}


def page_payload(
    *,
    settings: Settings,
    profiles: MarketplaceProfiles,
    store_raw: bytes | None,
    overrides: ModelParameterOverrides,
    catalogue: Mapping[str, Iterable[str]],
    snapshots: Mapping[str, MarketSnapshot | None],
    now: datetime | None = None,
) -> dict[str, Any]:
    """The whole page: one entry per marketplace provider, the shared profiles."""

    public_market = bool(getattr(settings, "marketplace_public_market", True))
    providers = []
    for view in marketplace_views(settings):
        provider_id = view["provider_id"]
        dialect = marketplace_for_provider(provider_id)
        if dialect is None:
            continue
        snapshot = snapshots.get(dialect.dialect_id)
        models = sorted(set(catalogue.get(provider_id, ())))
        market_models = sorted(set(models) | set(snapshot.models if snapshot else ()))
        providers.append(
            {
                **view,
                "ceiling_headers": list(dialect.ceiling_headers),
                "exclude_header": dialect.exclude_header,
                "assignments": assignments_for(overrides, provider_id),
                "models": models,
                "market_models": market_models,
                "snapshot": snapshot_block(
                    dialect, snapshot, public_market=public_market, now=now
                ),
                "moved": note_snapshot(dialect, snapshot),
                "combos": _snapshot_combos(snapshot),
                "notes": _snapshot_notes(snapshot),
                "account": account_block(
                    provider_id, view["has_key"], remembered_account(provider_id)
                ),
            }
        )
    return {
        "override_key": MARKETPLACE_PROFILE_OVERRIDE,
        "inherit": "inherit",
        "unverified_note": UNVERIFIED_NOTE,
        "not_readable": NOT_READABLE,
        "editor": editor_payload(),
        "store": store_state(store_raw, profiles),
        "profiles": profile_entries(profiles, overrides, catalogue, snapshots),
        "providers": providers,
    }


# ------------------------------------------------------- the offer explorer


def _levels_table(market: MarketModel) -> list[dict[str, Any]]:
    """One row per level. Input and output counts are equal per level on this
    marketplace (RES §4.1), so the two sides are paired by position when they
    have the same length and listed apart otherwise."""

    if market.levels_in and len(market.levels_in) == len(market.levels_out):
        return [
            {"price_in": price_in, "price_out": price_out, "accounts": count_in}
            for (price_in, count_in), (price_out, _count) in zip(
                market.levels_in, market.levels_out, strict=True
            )
        ]
    rows: list[dict[str, Any]] = [
        {"price_in": price, "price_out": None, "accounts": count}
        for price, count in market.levels_in
    ]
    rows.extend(
        {"price_in": None, "price_out": price, "accounts": count}
        for price, count in market.levels_out
    )
    return rows


def _entry_number(entry: Mapping[str, Any], name: str) -> float | None:
    value = entry.get(name)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _entry_pair(entry: Mapping[str, Any], name: str) -> list[float | None] | None:
    value = entry.get(name)
    if not isinstance(value, list) or len(value) != 2:
        return None
    return [
        float(item)
        if isinstance(item, int | float) and not isinstance(item, bool)
        else None
        for item in value
    ]


def offers_payload(
    *,
    provider_id: str,
    model_id: str,
    profiles: MarketplaceProfiles,
    overrides: ModelParameterOverrides,
    snapshot: MarketSnapshot | None,
    account: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """One model's market: the ladder with the ceiling it would be sent."""

    dialect = marketplace_for_provider(provider_id)
    if dialect is None:
        return {"model_id": model_id, "known": False}
    market = snapshot.model(model_id) if snapshot is not None else None
    payload = snapshot.payload if snapshot is not None else {}
    raw_models = payload.get("models") if isinstance(payload, Mapping) else None
    entry = _mapping(
        raw_models.get(model_id) if isinstance(raw_models, Mapping) else None
    )
    platform = payload.get("platform") if isinstance(payload, Mapping) else None
    cap = (
        platform.get("max_ask_pct_of_official")
        if isinstance(platform, Mapping)
        else None
    )
    cap = (
        float(cap)
        if isinstance(cap, int | float) and not isinstance(cap, bool)
        else None
    )
    official = list(market.official) if market is not None and market.official else None
    perf = _mapping(entry.get("perf"))
    prefix = upstream_prefix(model_id)
    notes = _snapshot_notes(snapshot)
    combos = [
        {
            "id": combo.get("id"),
            "selection": combo.get("selection"),
        }
        for combo in _snapshot_combos(snapshot)
        if model_id in (combo.get("members") or [])
    ]
    aliases: dict[str, Any]
    groups = (account or {}).get("aliases") if isinstance(account, Mapping) else None
    if isinstance(groups, Mapping) and groups.get("state") == "read":
        aliases = {
            "state": "read",
            "names": [
                group["alias"]
                for group in groups.get("groups") or []
                if model_id in (group.get("members") or [])
            ],
        }
    else:
        aliases = {
            "state": groups.get("state") if isinstance(groups, Mapping) else "not_read",
            "words": groups.get("words") if isinstance(groups, Mapping) else "not read",
        }
    resolved = _resolved(overrides, provider_id, model_id)
    assignment: dict[str, Any] | None = None
    ceiling: dict[str, Any]
    if resolved is None or resolved[0] is None:
        ceiling = {"state": "decides", "text": MARKETPLACE_DECIDES_WARNING}
        if resolved is not None:
            assignment = {"profile": None, "origin": resolved[1]}
    else:
        assignment = {"profile": resolved[0], "origin": resolved[1]}
        profile = profiles.get(resolved[0])
        if profile is None:
            ceiling = {
                "state": "nothing",
                "text": (
                    f"matches nothing: profile '{resolved[0]}' does not exist, so "
                    "this model's requests are refused"
                ),
            }
        else:
            ceiling = would_match(dialect, profile, model_id, snapshot, resolved[1])
    return {
        "provider_id": provider_id,
        "model_id": model_id,
        "prefix": prefix,
        "known": market is not None or bool(entry),
        "stamp": snapshot.stamp() if snapshot is not None else "no snapshot yet",
        "official": official,
        "levels": _levels_table(market) if market is not None else [],
        "min": [market.lowest(0), market.lowest(1)] if market is not None else None,
        "max": _entry_pair(entry, "max"),
        "last": _entry_number(entry, "last"),
        "platform_cap": cap,
        "cap_prices": (
            [official[0] * cap, official[1] * cap]
            if official is not None and cap is not None
            else None
        ),
        "perf": {
            "success_pct": _entry_number(perf, "success_pct"),
            "ttft_ms": _entry_number(perf, "ttft_ms"),
            "tps": _entry_number(perf, "tps"),
        },
        "cache_hit": _entry_number(entry, "cache_hit"),
        "note": notes.get(prefix) if prefix else None,
        "combos": combos,
        "aliases": aliases,
        "assignment": assignment,
        "ceiling": ceiling,
    }


# ---------------------------------------------------------- the last N rows


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _float_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _ceiling_numbers(
    ceiling: Mapping[str, Any] | None, dialect: MarketplaceDialect
) -> tuple[float | None, float | None, bool]:
    """The two sent ceilings as numbers, and whether a ceiling was sent at all."""

    if not isinstance(ceiling, Mapping):
        return None, None, False
    sent = ceiling.get("sent")
    if not isinstance(sent, Mapping):
        return None, None, False

    def number(header: str) -> float | None:
        value = sent.get(header)
        try:
            parsed = float(value) if isinstance(value, str | int | float) else None
        except ValueError:
            return None
        return parsed if parsed is not None and math.isfinite(parsed) else None

    return number(dialect.ceiling_headers[0]), number(dialect.ceiling_headers[1]), True


def recent_rows(
    provider_id: str,
    requests: Sequence[Mapping[str, Any]],
    attempts: Iterable[Mapping[str, Any]],
    attempt_params_key: str,
) -> list[dict[str, Any]]:
    """Each request the marketplace served: what was sent, served and charged.

    ``charged_vs_ceiling`` is evidence, never an action (IV.8): ``over`` when
    InferHub's reported charge is above every token at the ceiling that was
    sent (cached and cache-written tokens counted at the input rate, the
    worst case; never below the marketplace's declared minimum charge);
    ``within`` when it is not; ``None`` when it cannot be said
    -- no charge reported, no ceiling sent, an uncapped side, or a token
    count not recorded.
    """

    dialect = marketplace_for_provider(provider_id)
    if dialect is None:
        return []
    table = account_reads_for(dialect.dialect_id)
    floor = table.minimum_charge_usd if table is not None else 0.0
    by_request: dict[str, Mapping[str, Any]] = {}
    for attempt in attempts:
        if attempt.get("attempt_provider") != provider_id:
            continue
        params = attempt.get(attempt_params_key)
        if not isinstance(params, Mapping) or not isinstance(
            params.get("marketplace"), Mapping
        ):
            continue
        # The LAST attempt on this provider wins: it is the one that served.
        by_request[str(attempt.get("request_id"))] = {
            "attempt": attempt.get("attempt"),
            "outcome": attempt.get("outcome"),
            "marketplace": params["marketplace"],
        }
    rows = []
    for request in requests:
        request_id = str(request.get("id"))
        found = by_request.get(request_id)
        market: Mapping[str, Any] = found["marketplace"] if found is not None else {}
        headers = _mapping(market.get("headers"))
        ceiling = (
            market.get("ceiling")
            if isinstance(market.get("ceiling"), Mapping)
            else None
        )
        ceil_in, ceil_out, sent = _ceiling_numbers(ceiling, dialect)
        charged = _float_or_none(market.get("charged_usd"))
        tokens_in = _int_or_none(request.get("tokens_in"))
        tokens_out = _int_or_none(request.get("tokens_out"))
        cache_read = _int_or_none(request.get("cache_read_tokens"))
        cache_write = _int_or_none(request.get("cache_write_tokens"))
        bound: float | None = None
        flag: str | None = None
        if (
            sent
            and ceil_in is not None
            and ceil_out is not None
            and tokens_in is not None
            and tokens_out is not None
        ):
            input_tokens = tokens_in + (cache_read or 0) + (cache_write or 0)
            # A billed request costs at least the marketplace's minimum charge,
            # whatever its tokens: a tiny request paying it is not "over".
            bound = max(
                (input_tokens * ceil_in + tokens_out * ceil_out) / 1_000_000, floor
            )
            if charged is not None:
                flag = "over" if charged > bound + _EPSILON else "within"
        rows.append(
            {
                "request_id": request_id,
                "ts_iso": request.get("ts_iso"),
                "ts_epoch": _float_or_none(request.get("ts_epoch")),
                "model": request.get("resolved_model"),
                "status": request.get("status"),
                "attempt": found.get("attempt") if found is not None else None,
                "inferhub_model": headers.get("x-inferhub-model"),
                "inferhub_publisher": headers.get("x-inferhub-publisher"),
                "inferhub_request_id": headers.get("x-inferhub-request-id"),
                "inferhub_attempt": headers.get("x-inferhub-attempt"),
                "ceiling": (
                    {
                        "in": ceil_in,
                        "out": ceil_out,
                        "profile": ceiling.get("profile"),
                        "kind": ceiling.get("kind"),
                    }
                    if sent and ceiling is not None
                    else None
                ),
                "charged_usd": charged,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cache_read_tokens": cache_read,
                "cache_write_tokens": cache_write,
                "bound_usd": bound,
                "charged_vs_ceiling": flag,
            }
        )
    return rows


# ------------------------------------------------- the charged ask (Q17)


def _parse_time(value: Any) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


#: How far apart MCC's clock and InferHub's may put one request, in seconds,
#: for a match by time when the request id does not match. A matching rule,
#: not a limit: a wider window only ever finds more candidates, each of which
#: must also agree on the model and, when both are known, the token counts.
MATCH_WINDOW_SECONDS = 120.0


def charged_window(rows: Sequence[Mapping[str, Any]]) -> tuple[str, str] | None:
    """The usage-log window covering the shown rows, ISO instants."""

    times = [row["ts_epoch"] for row in rows if isinstance(row.get("ts_epoch"), float)]
    if not times:
        return None
    start = datetime.fromtimestamp(min(times) - MATCH_WINDOW_SECONDS, UTC)
    end = datetime.fromtimestamp(max(times) + MATCH_WINDOW_SECONDS, UTC)
    return start.isoformat(), end.isoformat()


def match_charged(
    rows: Sequence[Mapping[str, Any]], log_rows: Iterable[Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Each shown request's row in InferHub's usage log, and the ask it was charged at.

    By InferHub's request id first (``X-Inferhub-Request-Id`` == the log row's
    ``id``); else the one log row of the same model within
    :data:`MATCH_WINDOW_SECONDS` whose token counts agree where both are
    known -- labelled "matched by time", because it is an inference.
    """

    logs: list[dict[str, Any]] = []
    for item in log_rows:
        if not isinstance(item, Mapping):
            continue
        logs.append(
            {
                "id": item.get("id") if isinstance(item.get("id"), str) else None,
                "ts": _parse_time(item.get("ts")),
                "model": item.get("model")
                if isinstance(item.get("model"), str)
                else None,
                "prompt_tokens": _int_or_none(item.get("prompt_tokens")),
                "completion_tokens": _int_or_none(item.get("completion_tokens")),
                "ask_in": _decimal_text(item.get("ask_input_per_mtok")),
                "ask_out": _decimal_text(item.get("ask_output_per_mtok")),
                "cost": _decimal_text(item.get("cost_consumer_usdc")),
                "status": item.get("status")
                if isinstance(item.get("status"), str)
                else None,
            }
        )
    used: set[int] = set()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        request_id = str(row.get("request_id"))
        wanted = row.get("inferhub_request_id")
        index: int | None = None
        how = ""
        if isinstance(wanted, str) and wanted:
            for position, log in enumerate(logs):
                if position not in used and log["id"] == wanted:
                    index, how = position, "request id"
                    break
        if index is None:
            when = row.get("ts_epoch")
            model = row.get("inferhub_model") or row.get("model")
            candidates = []
            for position, log in enumerate(logs):
                if position in used or log["ts"] is None or not isinstance(when, float):
                    continue
                if abs(log["ts"] - when) > MATCH_WINDOW_SECONDS:
                    continue
                if not isinstance(model, str) or log["model"] is None:
                    continue
                if not (model == log["model"] or model.endswith("/" + log["model"])):
                    continue
                if (
                    log["completion_tokens"] is not None
                    and row.get("tokens_out") is not None
                    and log["completion_tokens"] != row.get("tokens_out")
                ):
                    continue
                candidates.append((abs(log["ts"] - when), position))
            if candidates:
                index, how = min(candidates)[1], "time and model"
        if index is None:
            out[request_id] = {"found": False}
            continue
        used.add(index)
        log = logs[index]
        out[request_id] = {
            "found": True,
            "matched_by": how,
            "ask_in": log["ask_in"],
            "ask_out": log["ask_out"],
            "cost_usdc": log["cost"],
            "status": log["status"],
        }
    return out


__all__ = [
    "MATCH_WINDOW_SECONDS",
    "MAX_RECENT",
    "NOT_READABLE",
    "PUBLIC_MARKET_SETTING",
    "UNVERIFIED_NOTE",
    "account_block",
    "assignments_for",
    "charged_window",
    "editor_payload",
    "marketplace_views",
    "match_charged",
    "note_snapshot",
    "offers_payload",
    "page_payload",
    "profile_entries",
    "provider_has_key",
    "recent_rows",
    "remember_account",
    "remembered_account",
    "reset_account_memory",
    "reset_market_memory",
    "rows_naming",
    "snapshot_block",
    "store_digest",
    "store_state",
    "would_match",
]
