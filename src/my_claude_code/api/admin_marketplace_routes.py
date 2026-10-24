"""The marketplace page's routes: the InferHub admin view (7.110.0, PR B).

Loopback-only and local-Origin like every admin route
(:func:`~my_claude_code.api.admin_routes.require_loopback_admin`):

* ``GET /admin/api/marketplaces`` -- the page: each marketplace provider
  (dialect, key or not, snapshot age per source, assignments, the account
  block), the shared profile store with "would match now" per assigned
  model, and the editor's rules. ``?summary=true`` answers only which
  marketplaces exist and which have a key -- what the sidebar asks on every
  load, at the cost of no read at all. ``?account=true`` re-reads the
  account first (the page asks it when it opens).
* ``PUT /admin/api/marketplaces/profiles`` -- the whole profile store,
  validated entry by entry by the store's own parser and written by its
  owner-only writer. It refuses rather than lose anything: a file changed
  since the page read it, a file MCC cannot parse, entries MCC cannot use,
  or a profile some override row still names -- each needs the operator's
  explicit yes.
* ``POST /admin/api/marketplaces/refresh`` -- rebuild the market snapshot now
  and re-read the account (the page's Refresh button).
* ``GET /admin/api/marketplaces/{provider}/offers?model=...`` -- one model's
  price ladder, speed, cache hit, route note, combos and aliases, with the
  ceiling its row would send.
* ``GET /admin/api/marketplaces/{provider}/recent?limit=N`` -- the last
  requests the provider served: what InferHub said served them, the ceiling
  sent, the charge reported. A bounded read of the request log, off the
  event loop.
* ``POST /admin/api/marketplaces/{provider}/charged`` -- "fetch the charged
  ask" (the user's Q17): InferHub's own usage log for the shown rows, read
  only on that press.

Assignments are NOT written here: the page saves the 15th override key
through the Models page's existing ``POST /admin/api/model-admin/overrides``,
the one writer of ``model_overrides.json``.
"""

import asyncio
import threading
from collections.abc import Mapping
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from my_claude_code.api.admin_routes import require_loopback_admin
from my_claude_code.api.dependencies import get_services, get_settings
from my_claude_code.api.marketplace_page import (
    MAX_RECENT,
    account_block,
    charged_window,
    marketplace_views,
    match_charged,
    offers_payload,
    page_payload,
    provider_has_key,
    recent_rows,
    remember_account,
    remembered_account,
    rows_naming,
    store_digest,
    store_state,
)
from my_claude_code.api.ports import ApiServices
from my_claude_code.application.marketplace import current_market_snapshot
from my_claude_code.config.marketplace_profiles import (
    MarketplaceProfile,
    MarketplaceProfiles,
    current_marketplace_profiles,
    parse_profile,
    save_marketplace_profiles,
)
from my_claude_code.config.marketplace_reads import (
    ACCOUNT_BLOCK_READS,
    READ_USAGE_LOGS,
    account_reads_for,
)
from my_claude_code.config.marketplaces import (
    MARKETPLACE_DIALECTS,
    PROVIDER_MARKETPLACE,
    marketplace_for_provider,
)
from my_claude_code.config.model_overrides import current_model_overrides
from my_claude_code.config.paths import marketplace_profiles_path
from my_claude_code.config.settings import Settings
from my_claude_code.core.request_log import ATTEMPT_PARAMS_KEY, store_from_settings

router = APIRouter()

#: One writer of the profile store at a time, from this page.
MARKETPLACE_PROFILES_WRITE_LOCK = threading.Lock()


def _read_store_raw() -> bytes | None:
    try:
        return marketplace_profiles_path().read_bytes()
    except FileNotFoundError:
        return None


def _snapshots() -> dict[str, Any]:
    return {
        dialect_id: current_market_snapshot(dialect)
        for dialect_id, dialect in MARKETPLACE_DIALECTS.items()
    }


def _page(
    settings: Settings, catalogue: Mapping[str, frozenset[str]]
) -> dict[str, Any]:
    return page_payload(
        settings=settings,
        profiles=current_marketplace_profiles(),
        store_raw=_read_store_raw(),
        overrides=current_model_overrides(),
        catalogue=catalogue,
        snapshots=_snapshots(),
    )


def _marketplace_provider(provider_id: str) -> str:
    if (
        provider_id not in PROVIDER_MARKETPLACE
        or marketplace_for_provider(provider_id) is None
    ):
        raise HTTPException(
            status_code=404, detail=f"{provider_id} is not a marketplace provider"
        )
    return provider_id


async def _read_accounts(services: ApiServices, settings: Settings) -> None:
    """Re-read every keyed marketplace's account; remembered for re-renders."""

    for view in marketplace_views(settings):
        if not view["has_key"] or not view["account_reads"]:
            continue
        answer = await services.admin.marketplace_reads(
            view["provider_id"], ACCOUNT_BLOCK_READS
        )
        remember_account(view["provider_id"], answer)


@router.get("/admin/api/marketplaces")
async def get_marketplaces(
    request: Request,
    summary: bool = False,
    account: bool = False,
    services: ApiServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """The marketplace page, or (``summary``) only what the sidebar needs."""

    require_loopback_admin(request)
    if summary:
        return {"views": marketplace_views(settings)}
    if account:
        await _read_accounts(services, settings)
    catalogue = services.admin.cached_model_ids()
    return await asyncio.to_thread(_page, settings, catalogue)


class ProfilesPayload(BaseModel):
    """The whole profile store, as the page's editor holds it."""

    profiles: dict[str, Any] = Field(default_factory=dict)
    #: The store's digest as the page last read it ("" = no file). ``None``
    #: skips the check (a script that means to overwrite).
    expected_digest: str | None = None
    #: The operator confirmed dropping the entries MCC cannot use.
    drop_unusable: bool = False
    #: The operator confirmed removing a profile some row still names.
    confirm_removal: bool = False


def _save_profiles(payload: ProfilesPayload) -> None:
    with MARKETPLACE_PROFILES_WRITE_LOCK:
        raw = _read_store_raw()
        if (
            payload.expected_digest is not None
            and payload.expected_digest != store_digest(raw)
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "marketplace_profiles.json changed since this page read it. "
                    "Reload the page and make the change again."
                ),
            )
        stored = current_marketplace_profiles()
        state = store_state(raw, stored)
        if state["unreadable"]:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"marketplace_profiles.json cannot be read ({state['unreadable']}). "
                    "Fix or move the file by hand; the page does not overwrite it."
                ),
            )
        parsed: dict[str, MarketplaceProfile] = {}
        problems: list[str] = []
        for name, document in sorted(payload.profiles.items()):
            profile, problem = parse_profile(str(name), document)
            if profile is None:
                problems.append(f"profile '{name}': {problem}")
                continue
            parsed[str(name)] = profile
        if problems:
            raise HTTPException(status_code=422, detail="; ".join(problems))
        unusable = [
            entry["name"] for entry in state["unusable"] if entry["name"] not in parsed
        ]
        if unusable and not payload.drop_unusable:
            raise HTTPException(
                status_code=409,
                detail=(
                    "marketplace_profiles.json holds entries MCC cannot use: "
                    f"{', '.join(unusable)}. Saving from this page removes them; "
                    "confirm that, or fix them in the file first."
                ),
            )
        overrides = current_model_overrides()
        removed = sorted(set(stored.profiles) - set(parsed))
        named = {name: rows_naming(overrides, name) for name in removed}
        still = {name: rows for name, rows in named.items() if rows}
        if still and not payload.confirm_removal:
            listed = "; ".join(
                f"'{name}' on "
                + ", ".join(f"{row['scope']} {row['key']}" for row in rows)
                for name, rows in still.items()
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Still assigned: {listed}. Those requests would be refused "
                    "until the rows name another profile. Confirm to remove anyway."
                ),
            )
        save_marketplace_profiles(MarketplaceProfiles(profiles=parsed))


@router.put("/admin/api/marketplaces/profiles")
async def put_marketplace_profiles(
    payload: ProfilesPayload,
    request: Request,
    services: ApiServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Validate and write the whole profile store; answer the refreshed page."""

    require_loopback_admin(request)
    try:
        await asyncio.to_thread(_save_profiles, payload)
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"not saved: {exc}") from None
    catalogue = services.admin.cached_model_ids()
    return await asyncio.to_thread(_page, settings, catalogue)


@router.post("/admin/api/marketplaces/refresh")
async def refresh_marketplaces(
    request: Request,
    services: ApiServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Rebuild the market snapshot now and re-read the account (Refresh)."""

    require_loopback_admin(request)
    built = False
    before = {key: (s.fetched_at if s else None) for key, s in _snapshots().items()}
    for view in marketplace_views(settings):
        if not view["has_key"]:
            continue
        answer = await services.admin.marketplace_reads(
            view["provider_id"],
            ("snapshot", *(ACCOUNT_BLOCK_READS if view["account_reads"] else ())),
        )
        built = built or bool(answer.pop("snapshot_built", False))
        if view["account_reads"]:
            remember_account(view["provider_id"], answer)
    catalogue = services.admin.cached_model_ids()
    after = {key: (s.fetched_at if s else None) for key, s in _snapshots().items()}
    page = await asyncio.to_thread(_page, settings, catalogue)
    if not built:
        words = "Nothing was read: no marketplace provider has a key."
    elif after != before:
        words = "Snapshot rebuilt and account re-read."
    else:
        # The store is rewritten only when what it says changed, so the time
        # shown stays the last read that changed anything.
        words = (
            "Market re-read and account re-read: nothing in the market changed, "
            "so the snapshot keeps the time of the last read that did."
        )
    page["refresh"] = {"snapshot_built": built, "words": words}
    return page


@router.get("/admin/api/marketplaces/{provider_id}/offers")
async def get_marketplace_offers(
    provider_id: str,
    request: Request,
    model: str = "",
) -> dict[str, Any]:
    """One model's market, with the ceiling its row would send."""

    require_loopback_admin(request)
    _marketplace_provider(provider_id)
    model_id = model.strip()
    if not model_id:
        raise HTTPException(status_code=400, detail="model must not be empty")
    dialect = marketplace_for_provider(provider_id)
    remembered = remembered_account(provider_id)

    def build() -> dict[str, Any]:
        account = account_block(provider_id, True, remembered)
        return offers_payload(
            provider_id=provider_id,
            model_id=model_id,
            profiles=current_marketplace_profiles(),
            overrides=current_model_overrides(),
            snapshot=current_market_snapshot(dialect) if dialect else None,
            account=account,
        )

    return await asyncio.to_thread(build)


def _recent(settings: Settings, provider_id: str, limit: int) -> dict[str, Any]:
    store = store_from_settings(settings)
    if store is None:
        return {"enabled": False, "rows": []}
    requests, _total, _more = store.list_requests_page(
        limit=limit,
        provider=provider_id,
        include_total=False,
        body_preview_chars=0,
    )
    wanted = {str(row.get("id")) for row in requests}
    times = [
        float(row["ts_epoch"])
        for row in requests
        if isinstance(row.get("ts_epoch"), int | float)
    ]
    attempts: list[dict[str, Any]] = []
    if wanted and times:
        # The same filter as the page above, bounded to the shown requests'
        # own time span, so the walk can never run past them.
        walk = store.iter_export_attempt_rows(
            provider=provider_id,
            since=min(times),
            until=max(times),
            page_size=limit,
        )
        try:
            attempts.extend(
                attempt for attempt in walk if str(attempt.get("request_id")) in wanted
            )
        finally:
            walk.close()
    return {
        "enabled": True,
        "rows": recent_rows(provider_id, requests, attempts, ATTEMPT_PARAMS_KEY),
    }


@router.get("/admin/api/marketplaces/{provider_id}/recent")
async def get_marketplace_recent(
    provider_id: str,
    request: Request,
    limit: int = 20,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """The last requests this marketplace served, newest first (bounded)."""

    require_loopback_admin(request)
    _marketplace_provider(provider_id)
    bounded = max(1, min(int(limit), MAX_RECENT))
    answer = await asyncio.to_thread(_recent, settings, provider_id, bounded)
    answer["limit"] = bounded
    return answer


class ChargedPayload(BaseModel):
    """The requests on screen whose charged ask the operator asked for."""

    request_ids: list[str] = Field(default_factory=list)


@router.post("/admin/api/marketplaces/{provider_id}/charged")
async def fetch_marketplace_charged(
    provider_id: str,
    payload: ChargedPayload,
    request: Request,
    services: ApiServices = Depends(get_services),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Read InferHub's usage log for the shown rows: the ask each was charged at.

    Only on this press (the user's Q17), bounded: at most the last
    ``MAX_RECENT`` requests, at most the declared number of log pages.
    """

    require_loopback_admin(request)
    _marketplace_provider(provider_id)
    dialect = marketplace_for_provider(provider_id)
    table = account_reads_for(dialect.dialect_id) if dialect is not None else None
    if table is None or table.read(READ_USAGE_LOGS) is None:
        raise HTTPException(
            status_code=404, detail=f"{provider_id} declares no usage-log read"
        )
    if not provider_has_key(settings, provider_id):
        return {
            "state": "no_key",
            "words": "No InferHub key is set: nothing is read.",
            "rows": {},
        }
    wanted = {str(item) for item in payload.request_ids[:MAX_RECENT]}
    recent = await asyncio.to_thread(_recent, settings, provider_id, MAX_RECENT)
    shown = [row for row in recent["rows"] if row["request_id"] in wanted]
    window = charged_window(shown)
    if window is None:
        return {"state": "nothing", "words": "No shown request to look up.", "rows": {}}
    answer = await services.admin.marketplace_reads(
        provider_id,
        (READ_USAGE_LOGS,),
        {READ_USAGE_LOGS: {"range": "custom", "from": window[0], "to": window[1]}},
    )
    if answer.get("state") != "read":
        return {
            "state": str(answer.get("state") or "failed"),
            "words": str(answer.get("words") or "The usage log could not be read."),
            "rows": {},
        }
    read = (answer.get("reads") or {}).get(READ_USAGE_LOGS) or {}
    status = str(read.get("status") or "")
    if status != "ok":
        readable = status in ("http_401", "http_403")
        code = read.get("code")
        return {
            "state": "not_readable" if readable else "failed",
            "words": (
                f"Usage logs are not readable with this key ({status[5:]}"
                + (f", {code}" if isinstance(code, str) else "")
                + "). An inference_only key cannot read them."
                if readable
                else f"The usage log could not be read ({status})."
            ),
            "rows": {},
        }
    body = read.get("body")
    found = body.get("rows") if isinstance(body, Mapping) else None
    log_rows: list[Any] = list(found) if isinstance(found, list) else []
    matched = match_charged(shown, log_rows)
    return {
        "state": "read",
        "words": (
            f"Read {len(log_rows)} usage-log row(s) ({read.get('pages_read', 0)} page(s)"
            + (", more were not read" if read.get("truncated") else "")
            + f"); InferHub keeps them {table.usage_logs_retention_days} days."
        ),
        "rows": matched,
        "retention_days": table.usage_logs_retention_days,
    }


__all__ = ["MARKETPLACE_PROFILES_WRITE_LOCK", "router"]
