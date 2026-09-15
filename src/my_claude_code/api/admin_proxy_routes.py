"""Proxy chain routes: what each provider's egress chain is, and the write.

Two loopback-only routes behind the **Proxying** page.

``GET /admin/api/proxy-chains`` returns one record per *configured* provider --
custom providers included -- in the order the Providers page uses, together
with the vocabulary the page renders: the four rotation policies, the eleven
failure kinds and which of them may be armed, the entry cap and the switch
bound's range. A provider that is not configured is not listed: a chain for a
provider with no credential could never route anything, and fifty-six cards for
the four providers an operator actually uses is not a page.

``PUT /admin/api/proxy-chains`` writes one provider's chain and returns the
whole refreshed payload. A JSON document rather than settings keys, so it
cannot go through ``/admin/api/config/apply`` -- that route's flat
env-key-to-string map has no way to express an ordered list of endpoints with a
per-entry pause, and widening it is what would break the dirty-state diff on
every other page. The same argument ``admin_harness_routes`` makes for
``harness_tiers``.

**A proxy URL never travels back to the browser.** The ``GET`` sends
:func:`mask_proxy_label` -- ``host:port`` with any ``user:pass`` removed -- and
the scheme as its own field. An entry the operator did not edit comes back on
the ``PUT`` as the endpoint's id, which *is* the "unchanged" sentinel: there is
no code path that needs the password on the client at all, so there is none
that can leak it.

**This release stores and shows chains; it does not route through them.** The
runtime still reads ``ProviderConfig.proxy`` exactly as it did before, so this
write deliberately does *not* republish the provider generation. Republishing
resets the credential pools' counters, and spending that on a change with no
effect on the request path would make key health read zeros for no reason.
The release that adds the runtime seam adds the republish with it.
"""

import asyncio
import threading
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field

from my_claude_code.api.admin_routes import require_loopback_admin
from my_claude_code.api.dependencies import get_services
from my_claude_code.api.ports import ApiServices
from my_claude_code.application.proxy_check import (
    PROXY_CHECK_TIMEOUT_SECONDS,
    check_endpoints,
    destination_for_provider,
)
from my_claude_code.config.admin.manifest import FIELDS
from my_claude_code.config.admin.status import provider_config_status
from my_claude_code.config.constants import ROTATION_POLICY_ORDER
from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.proxy_chains import (
    DEFAULT_TRIGGER_KINDS,
    DIRECT,
    MAX_SWITCHES_MAX,
    MAX_SWITCHES_MIN,
    OAUTH_PROVIDER_IDS,
    PROXY_CHAIN_MAX_ENTRIES,
    PROXY_URL_SCHEMES,
    REFUSED_TRIGGER_KINDS,
    SCOPES,
    TLS_INTERCEPTED,
    TRIGGER_KIND_ORDER,
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    clamp_max_switches,
    current_proxy_chains,
    is_valid_proxy_url,
    load_proxy_chains,
    normalise_policy,
    save_proxy_chains,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.proxy_attribution import DIRECT_PROXY_LABEL
from my_claude_code.core.proxy_rotation import PROXY_HEALTH, PROXY_INTERCEPTION

router = APIRouter()

# One writer at a time. Two chain edits landing together would each derive a
# new document from a base read before the other committed, and the second
# would silently drop the first -- the race ``apply_admin_config_with`` closes
# for the env-var settings, which this file cannot use.
_CHAIN_WRITE_LOCK = threading.Lock()

#: What each policy actually does to a per-address allowance. This is the point
#: of the feature for an operator whose quota is metered by IP, and it is the
#: one thing about rotation that is not obvious from the four names.
POLICY_HELP: dict[str, str] = {
    "single": (
        "Always the first entry that is not paused. The rest of the chain is "
        "never used, so this is the chain switched off without losing it."
    ),
    "round_robin": (
        "Spreads requests across every healthy entry from the first request "
        "onwards. This is the one that multiplies a per-address allowance: "
        "with four working proxies a per-IP limit applies four times over."
    ),
    "least_used": (
        "Picks the entry with the fewest requests so far. Evens out a chain "
        "whose entries were added at different times."
    ),
    "failover": (
        "Pins to the first healthy entry and moves only after a failure you "
        "selected below. Four proxies still use one address until it fails, "
        "so this changes nothing until something breaks -- which is why it is "
        "the default."
    ),
}

#: Why the two refused kinds are refused, on the chip itself.
REFUSED_REASON: dict[str, str] = {
    "authentication": (
        "A new address does not fix a rejected key. Rotating on it burns the "
        "whole chain in one request and benches every proxy it touched."
    ),
    "permission": (
        "A new address does not fix a refused scope. Rotating on it burns the "
        "whole chain in one request and benches every proxy it touched."
    ),
}

#: Kinds that are offered but off: a new address is unlikely to help, and
#: choosing one costs the operator latency and nothing else.
UNLIKELY_REASON: dict[str, str] = {
    "invalid_request": "The request body is the problem, not the route to it.",
    "model_rejected": "The model does not exist on that endpoint.",
    "context_length": "A larger context window is the fix; the model chain owns it.",
    "overloaded": "The provider is busy. A different address occasionally helps.",
    "upstream": "A fault on the provider's side. A different address occasionally helps.",
    "unavailable": "A dead or refused connection. Often the proxy, sometimes the provider.",
}


class ProxyEntryPayload(BaseModel):
    """One rung of a chain, as the page sends it.

    Exactly one of three shapes. ``direct`` marks the no-proxy rung. ``proxy``
    names an endpoint already in the catalogue and is how an untouched entry
    travels -- the page never holds the URL, so referencing by id *is* the
    "unchanged" sentinel. ``url`` is a new or edited address.
    """

    proxy: str = ""
    url: str = ""
    direct: bool = False
    paused: bool = False
    #: "Use this provider's own ``<PROVIDER>_PROXY`` as this entry." The
    #: server resolves it, because the page has never been told that URL and
    #: must not be: this is how the static proxy becomes entry 1 of the chain
    #: without its password ever crossing the wire. The ``.env`` key itself is
    #: not touched -- it keeps its value and simply stops being consulted
    #: while the chain has entries.
    inherit: bool = False


class ProxyChainPayload(BaseModel):
    """One provider's whole chain. A ``PUT`` replaces it outright.

    Replace rather than patch because the order of the entries *is* the
    setting: a partial write would have to express "move rung 2 above rung 1",
    and a list the page already holds says it without a vocabulary for it.
    """

    provider: str
    remove: bool = False
    enabled: bool = False
    policy: str = "failover"
    scope: str = "provider"
    max_switches: int = 2
    on: list[str] = Field(default_factory=lambda: list(DEFAULT_TRIGGER_KINDS))
    oauth_acknowledged: bool = False
    entries: list[ProxyEntryPayload] = Field(default_factory=list)


@router.get("/admin/api/proxy-chains")
async def get_proxy_chains(
    request: Request, services: ApiServices = Depends(get_services)
):
    """Return every configured provider's chain and the page's vocabulary."""

    require_loopback_admin(request)
    return await asyncio.to_thread(_payload, services)


@router.put("/admin/api/proxy-chains")
async def put_proxy_chain(
    payload: ProxyChainPayload,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Write one provider's chain and return the whole refreshed state."""

    require_loopback_admin(request)
    settings = services.requests.current_settings()
    providers = {
        entry["provider_id"]: entry for entry in _configured_providers(settings)
    }
    provider_id = payload.provider.strip().lower()
    if provider_id not in providers:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Not a configured provider: {payload.provider}. A chain can "
                "only be set on a provider that has a credential."
            ),
        )

    if payload.remove:
        await asyncio.to_thread(_commit, provider_id, None)
        await _republish(services)
        return await asyncio.to_thread(_payload, services)

    _reject_bad_policy(payload)
    _reject_bad_triggers(payload.on)
    if len(payload.entries) > PROXY_CHAIN_MAX_ENTRIES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"A chain may hold at most {PROXY_CHAIN_MAX_ENTRIES} entries; "
                f"this one has {len(payload.entries)}. Each entry is a separate "
                "client, rate limiter and recovery ladder per credential."
            ),
        )
    if (
        provider_id in OAUTH_PROVIDER_IDS
        and payload.entries
        and not payload.oauth_acknowledged
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{providers[provider_id]['display_name']} signs in with your "
                "personal subscription. Acknowledge what changing source "
                "address means for it before giving it a chain."
            ),
        )

    await asyncio.to_thread(
        _commit_chain, provider_id, payload, providers[provider_id]["inherited_proxy"]
    )
    await _republish(services)
    return await asyncio.to_thread(_payload, services)


class ProxyCheckPayload(BaseModel):
    """Which of one provider's addresses to measure.

    ``proxy`` names one stored endpoint; omitting it tests every entry of that
    provider's chain. There is no by-URL form: a check is a network call made
    with the operator's own stored credentials in mind, and accepting a URL on
    this route would make it a general-purpose outbound fetch with an admin
    session behind it.
    """

    provider: str
    proxy: str = ""


@router.post("/admin/api/proxy-chains/check")
async def check_proxy_chain(
    payload: ProxyCheckPayload,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Measure one address, or a whole chain, and return the refreshed state.

    Three questions per address: does it answer, does its tunnel keep
    certificate validation intact, and how long did that take. The destination
    is this provider's own base-URL host, so the check tests the thing the
    chain will actually do and contacts nobody the operator has not already
    chosen.

    A press of this button is the only outbound request this feature makes on
    an install where the background checker is off, which is every install
    until somebody turns it on.
    """

    require_loopback_admin(request)
    settings = services.requests.current_settings()
    providers = {
        entry["provider_id"]: entry for entry in _configured_providers(settings)
    }
    provider_id = payload.provider.strip().lower()
    if provider_id not in providers:
        raise HTTPException(
            status_code=404, detail=f"Not a configured provider: {payload.provider}"
        )
    destination = str(providers[provider_id].get("base_url") or "").strip()
    if not destination.lower().startswith("https://"):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{providers[provider_id]['display_name']} has no https base "
                "URL to test against, so there is no certificate to verify "
                "through the tunnel. Set its base URL first."
            ),
        )

    store = current_proxy_chains()
    chain = store.chain(provider_id)
    wanted = _ids_to_check(store, chain, payload.proxy.strip())
    if not wanted:
        raise HTTPException(
            status_code=422,
            detail=(
                "Nothing to test. A chain entry has to be saved before it can "
                "be measured, and Direct has no proxy to measure."
            ),
        )

    outcomes = await check_endpoints(
        wanted,
        dict.fromkeys(wanted, destination),
        timeout=PROXY_CHECK_TIMEOUT_SECONDS,
        exit_ip_url=settings.proxy_check_exit_ip_url.strip(),
    )
    refreshed = await asyncio.to_thread(_payload, services)
    refreshed["checked"] = {
        proxy_id: outcome.record.as_document() | {"label": outcome.label}
        for proxy_id, outcome in outcomes.items()
    }
    return refreshed


def _ids_to_check(
    store: ProxyChains, chain: ProxyChain | None, requested: str
) -> tuple[str, ...]:
    """The endpoint ids one press should measure, in chain order.

    Direct is silently absent rather than refused: it is a legal rung with no
    address to dial, and "test all" on a chain that contains it means the rest.
    """

    if chain is None:
        return ()
    ids = tuple(
        entry.proxy
        for entry in chain.entries
        if entry.proxy and store.endpoint(entry.proxy) is not None
    )
    if not requested:
        return ids
    return tuple(proxy_id for proxy_id in ids if proxy_id == requested)


async def _republish(services: ApiServices) -> None:
    """Rebuild the provider generation so the new chain is what routes.

    A proxy is read once, in a provider's constructor, and baked into a
    long-lived client; a chain is read in the same place. So a chain edit that
    did not republish would be stored, shown, and ignored until the next
    restart -- which is exactly what the release that shipped the page did on
    purpose, because there was no runtime to tell.

    The known cost, stated on the page's save confirmation rather than hidden:
    a generation replace resets the credential pools' counters, so key health
    reads zeros immediately after a chain is saved. The numbers were never
    wrong; the pools they were measured on no longer exist.
    """

    # Never fail the write for it. The chain is already on disk, and a
    # republish that could not run leaves the operator with a saved chain that
    # starts routing at the next restart -- worse than a 500 that suggests
    # nothing was saved at all.
    try:
        await services.admin.reload_providers("proxy_chains")
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("PROXY CHAINS: saved, but could not republish: {}", exc)


def _reject_bad_policy(payload: ProxyChainPayload) -> None:
    policy = payload.policy.strip().lower()
    if normalise_policy(policy) != policy and policy != "on_error":
        raise HTTPException(
            status_code=422,
            detail="policy must be one of: " + ", ".join(ROTATION_POLICY_ORDER),
        )
    if payload.scope.strip().lower() not in SCOPES:
        raise HTTPException(
            status_code=422, detail="scope must be one of: " + ", ".join(SCOPES)
        )
    if not MAX_SWITCHES_MIN <= payload.max_switches <= MAX_SWITCHES_MAX:
        raise HTTPException(
            status_code=422,
            detail=(
                "max_switches must be between "
                f"{MAX_SWITCHES_MIN} and {MAX_SWITCHES_MAX}"
            ),
        )


def _reject_bad_triggers(kinds: list[str]) -> None:
    """Refuse the two destructive kinds loudly rather than dropping them.

    The store drops them silently, because it is also reached by a file a
    human edited and a chain that quietly ignores one word is better than a
    server that will not start. The API is a contract with the page, and the
    page cannot send one, so anything that does is a caller worth telling.
    """

    wanted = [str(kind).strip().lower() for kind in kinds]
    refused = sorted({kind for kind in wanted if kind in REFUSED_TRIGGER_KINDS})
    if refused:
        raise HTTPException(
            status_code=422,
            detail=(
                "These failures may not move a proxy chain: "
                f"{', '.join(refused)}. " + REFUSED_REASON[refused[0]]
            ),
        )
    unknown = sorted({kind for kind in wanted if kind not in TRIGGER_KIND_ORDER})
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"Not a failure kind: {', '.join(unknown)}"
        )


def _commit(provider_id: str, chain: ProxyChain | None) -> None:
    with _CHAIN_WRITE_LOCK:
        # Re-read inside the lock, never from the mtime cache: the base for
        # this edit has to be what is on disk right now.
        save_proxy_chains(load_proxy_chains().with_chain(provider_id, chain))


def _commit_chain(provider_id: str, payload: ProxyChainPayload, inherited: str) -> None:
    with _CHAIN_WRITE_LOCK:
        store = load_proxy_chains()
        store, entries = _resolve_entries(store, payload.entries, inherited)
        chain = ProxyChain(
            enabled=payload.enabled,
            policy=normalise_policy(payload.policy),
            entries=entries,
            on=tuple(
                kind
                for kind in TRIGGER_KIND_ORDER
                if kind in {name.strip().lower() for name in payload.on}
            ),
            scope=payload.scope.strip().lower(),
            max_switches=clamp_max_switches(payload.max_switches),
            oauth_acknowledged=payload.oauth_acknowledged,
        )
        save_proxy_chains(store.with_chain(provider_id, chain))


def _resolve_entries(
    store: ProxyChains, payloads: list[ProxyEntryPayload], inherited: str
) -> tuple[ProxyChains, tuple[ProxyChainEntry, ...]]:
    entries: list[ProxyChainEntry] = []
    for index, entry in enumerate(payloads):
        url = inherited.strip() if entry.inherit else entry.url.strip()
        if entry.inherit and not url:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Entry {index + 1} asks for this provider's stored proxy, "
                    "and it has none."
                ),
            )
        proxy_id = entry.proxy.strip()
        if entry.direct or (not url and not proxy_id):
            entries.append(ProxyChainEntry(proxy=DIRECT, paused=entry.paused))
            continue
        if url:
            if not is_valid_proxy_url(url):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Entry {index + 1} is not a proxy URL. Give a scheme "
                        "and a host, for example "
                        "socks5h://198.51.100.9:1080 (schemes: "
                        + ", ".join(sorted(PROXY_URL_SCHEMES))
                        + ")."
                    ),
                )
            store, proxy_id = store.add_endpoint(url)
        elif store.endpoint(proxy_id) is None:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Entry {index + 1} names a proxy this install no longer "
                    "has. Reload the page and set it again."
                ),
            )
        _refuse_if_intercepted(store, proxy_id, index)
        entries.append(ProxyChainEntry(proxy=proxy_id, paused=entry.paused))
    return store, tuple(entries)


def _refuse_if_intercepted(store: ProxyChains, proxy_id: str, index: int) -> None:
    """Refuse an address the checker found terminating TLS.

    The security control, enforced at the one moment it matters: the write that
    would put this address in front of a credential. Two sources agree before
    this raises -- the durable verdict in the store and the live ledger the
    running pools read -- so neither a restart nor a hand-edited document can
    get an intercepting address into a chain through this route.

    There is no override. An operator who believes the verdict is wrong presses
    Test again; a passing check clears both, which is the only way out and is a
    measurement rather than a confirmation dialog.
    """

    endpoint = store.endpoint(proxy_id)
    label = (
        "" if endpoint is None else (endpoint.label or mask_proxy_label(endpoint.url))
    )
    if (endpoint is not None and endpoint.refused) or PROXY_INTERCEPTION.is_refused(
        label
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                f"Entry {index + 1} ({label}) breaks certificate validation: "
                "its tunnel presented a certificate this machine does not "
                "trust, which means it is reading the traffic rather than "
                "relaying it. MCC will not route a credential through it. "
                "Press Test on that row again if you believe this has changed."
            ),
        )


# ------------------------------------------------------------------- payload


def _payload(services: ApiServices) -> dict[str, Any]:
    settings = services.requests.current_settings()
    store = current_proxy_chains()
    return {
        "vocabulary": {
            "policies": [
                {"id": policy, "help": POLICY_HELP[policy]}
                for policy in ROTATION_POLICY_ORDER
            ],
            "default_policy": "failover",
            "kinds": [_kind_payload(kind) for kind in TRIGGER_KIND_ORDER],
            "default_kinds": list(DEFAULT_TRIGGER_KINDS),
            "scopes": list(SCOPES),
            "max_entries": PROXY_CHAIN_MAX_ENTRIES,
            "switch_bound": {
                "min": MAX_SWITCHES_MIN,
                "max": MAX_SWITCHES_MAX,
                "default": 2,
            },
            "tls_intercepted": TLS_INTERCEPTED,
            # What the page says about the checker, so it can tell the operator
            # whether anything is measuring these addresses without them
            # pressing a button. Off is the shipped answer and the page says so
            # rather than leaving a stale "not checked yet" unexplained.
            "checker": {
                "enabled": bool(settings.proxy_check_enabled),
                "interval_minutes": int(settings.proxy_check_interval_minutes),
                "exit_ip_configured": bool(settings.proxy_check_exit_ip_url.strip()),
            },
        },
        "providers": [
            _provider_payload(entry, store) for entry in _configured_providers(settings)
        ],
    }


def _kind_payload(kind: str) -> dict[str, Any]:
    if kind in REFUSED_TRIGGER_KINDS:
        return {"id": kind, "state": "refused", "reason": REFUSED_REASON[kind]}
    if kind in DEFAULT_TRIGGER_KINDS:
        return {"id": kind, "state": "recommended", "reason": ""}
    return {"id": kind, "state": "selectable", "reason": UNLIKELY_REASON.get(kind, "")}


def _value_state(settings: Settings) -> dict[str, dict[str, Any]]:
    """Render the live settings in the shape ``provider_config_status`` reads.

    That helper is written against the dashboard's ``load_value_state()``,
    which re-reads the ``.env`` files. Here the question is "what would a
    request sent right now actually use", and the answer to that is the
    ``Settings`` object the server is running on -- a key set in the process
    environment after the last file write is real, and a file the server has
    not reloaded is not. Reading one source for "is it configured" and another
    for "what is its proxy" is how a card ends up describing two installs.
    """

    return {
        field.key: {
            "value": str(getattr(settings, field.settings_attr, "") or "")
            if field.settings_attr
            else ""
        }
        for field in FIELDS
    }


def _configured_providers(settings: Settings) -> list[dict[str, Any]]:
    """Every provider that could route a request today, in page order.

    Built on the same ``provider_config_status`` the Providers page renders, so
    the two lists cannot disagree about what "configured" means: a static
    provider with every configuration attribute set, and a custom provider that
    is enabled and has a key. A local runtime has no upstream to proxy to and
    is skipped.
    """

    custom_entries = {
        entry.provider_id: entry for entry in get_provider_registry().list_custom()
    }
    configured: list[dict[str, Any]] = []
    for status in provider_config_status(_value_state(settings)):
        if status.get("status") != "configured":
            continue
        provider_id = str(status["provider_id"])
        descriptor = PROVIDER_CATALOG.get(provider_id)
        custom = bool(status.get("custom"))
        if custom:
            entry = custom_entries.get(provider_id)
            inherited = (entry.proxy or "") if entry is not None else ""
            base_url = entry.base_url if entry is not None else ""
            env_var = None
        elif descriptor is not None and descriptor.proxy_attr:
            inherited = str(getattr(settings, descriptor.proxy_attr, "") or "")
            base_url = destination_for_provider(provider_id, settings)
            env_var = descriptor.proxy_attr.upper()
        else:
            # A provider with no proxy attribute at all has nothing to inherit
            # and nothing to override; it still gets a card, because a chain is
            # the first egress control it has ever had.
            inherited, env_var = "", None
            base_url = destination_for_provider(provider_id, settings)
        configured.append(
            {
                "provider_id": provider_id,
                "display_name": str(status["display_name"]),
                "group": str(status.get("group") or ""),
                "custom": custom,
                "oauth": provider_id in OAUTH_PROVIDER_IDS,
                "key_count": int(status.get("key_count") or 0),
                "env_var": env_var,
                "inherited_proxy": inherited,
                "base_url": base_url,
            }
        )
    return configured


def _provider_payload(entry: dict[str, Any], store: ProxyChains) -> dict[str, Any]:
    chain = store.chain(entry["provider_id"])
    inherited = str(entry.pop("inherited_proxy") or "")
    payload = dict(entry)
    # The inherited value is reported as a label and a scheme, never as the
    # URL: a static `<PROVIDER>_PROXY` can carry a password too, and it is
    # already masked everywhere else on the dashboard.
    payload["inherited_label"] = mask_proxy_label(inherited)
    payload["inherited_scheme"] = _scheme(inherited)
    payload["chain"] = (
        _chain_payload(chain, store, str(entry["provider_id"]))
        if chain is not None
        else None
    )
    return payload


def _chain_payload(
    chain: ProxyChain, store: ProxyChains, provider_id: str
) -> dict[str, Any]:
    return {
        "enabled": chain.enabled,
        "policy": chain.policy,
        "scope": chain.scope,
        "max_switches": chain.max_switches,
        "on": list(chain.on),
        "oauth_acknowledged": chain.oauth_acknowledged,
        "entries": [_entry_payload(item, store, provider_id) for item in chain.entries],
    }


def _entry_payload(
    entry: ProxyChainEntry, store: ProxyChains, provider_id: str
) -> dict[str, Any]:
    endpoint = store.endpoint(entry.proxy) if entry.proxy else None
    url = endpoint.url if endpoint is not None else ""
    label = (endpoint.label if endpoint is not None else "") or mask_proxy_label(url)
    last_check = endpoint.last_check if endpoint is not None else None
    return {
        "proxy": entry.proxy,
        "paused": entry.paused,
        "direct": entry.is_direct,
        "label": label,
        "scheme": _scheme(url),
        "source": endpoint.source if endpoint is not None else "",
        "source_count": endpoint.source_count if endpoint is not None else 0,
        # What the checker last measured about this address, out of the store
        # rather than out of a ledger: it is a durable verdict that survives a
        # restart, and the refusal it can carry has to survive one too.
        "last_check": None if last_check is None else last_check.as_document(),
        "refused": bool(endpoint is not None and endpoint.refused),
        # What the running pools have actually measured about this address, out
        # of the process-wide ledger the pools write to. A registry rather than
        # a walk of the live provider tree: the answer outlives the generation
        # replace a chain edit performs, and nothing on the request path has to
        # be reachable from an admin request. ``state: "unknown"`` is the
        # honest reading of an address no request has gone through yet, and it
        # is what the card renders as "not checked yet".
        "health": PROXY_HEALTH.snapshot(
            provider_id, DIRECT_PROXY_LABEL if entry.is_direct else label
        ),
    }


def _scheme(url: str) -> str:
    scheme, separator, _ = (url or "").strip().partition("://")
    return scheme.lower() if separator and scheme.lower() in PROXY_URL_SCHEMES else ""
