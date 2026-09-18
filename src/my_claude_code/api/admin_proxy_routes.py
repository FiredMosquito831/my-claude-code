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
import time
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field

from my_claude_code.api.admin_routes import require_loopback_admin
from my_claude_code.api.dependencies import get_services
from my_claude_code.api.ports import ApiServices
from my_claude_code.application.proxy_check import (
    check_budget,
    check_endpoints,
    destination_for_provider,
)
from my_claude_code.application.proxy_fetch import (
    FetchAlreadyRunning,
    fetch_status,
    resolve_fetch_concurrency,
    start_fetch,
    stop_fetch,
)
from my_claude_code.application.proxy_ingest import (
    detect_feed,
    feed_payload,
    known_feed_name,
)
from my_claude_code.config.admin.manifest import FIELDS
from my_claude_code.config.admin.status import provider_config_status
from my_claude_code.config.constants import (
    PROXY_CANDIDATE_BULK_MAX_DEFAULT,
    PROXY_FEED_MAX_DEFAULT,
    PROXY_FEED_MINIMUM_MINUTES,
    PROXY_FETCH_TEST_CONCURRENCY_MAX,
    ROTATION_POLICY_ORDER,
)
from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.proxy_chains import (
    DEFAULT_TRIGGER_KINDS,
    DIRECT,
    EMPTY_CHAIN,
    MAX_SWITCHES_MAX,
    MAX_SWITCHES_MIN,
    OAUTH_PROVIDER_IDS,
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
from my_claude_code.config.proxy_feeds import (
    FEED_NAME_MAX_LENGTH,
    LINES_DEFAULT_PROTOCOL,
    PARSER_IDS,
    PARSERS,
    CustomFeed,
    is_valid_feed_url,
    normalise_parser,
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
    #: Whether a request with no healthy address left goes out on this
    #: machine's own address. Defaults to True here as it does in the store, so
    #: an older client that does not send the field cannot turn it off by
    #: omission.
    direct_fallback: bool = True
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
    cap = _entry_cap(settings)
    if cap and len(payload.entries) > cap:
        raise HTTPException(
            status_code=422,
            detail=(
                f"A chain may hold at most {cap} entries; this one has "
                f"{len(payload.entries)}. That ceiling is yours, not MCC's: "
                "PROXY_CHAIN_MAX_ENTRIES on Limits & Resilience, and 0 -- what "
                "ships -- means no limit at all."
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
        timeout=float(settings.proxy_check_timeout_seconds),
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


def _entry_cap(settings: Any) -> int:
    """The operator's own ceiling on chain length; ``0`` means there is none.

    7.13 shipped a hard 12 in ``config/proxy_chains.py``, justified by the cost
    of a leaf provider per entry per credential. 7.19.0 builds those leaves
    lazily, so the cost is gone and the ceiling with it -- what is left is a
    number an operator may want, read from their own settings and enforced with
    a message rather than by truncation.
    """

    return max(0, int(getattr(settings, "proxy_chain_max_entries", 0) or 0))


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
            direct_fallback=payload.direct_fallback,
            oauth_acknowledged=payload.oauth_acknowledged,
        )
        # An address the operator typed may be one a feed had already offered:
        # ``add_endpoint`` files it under the id it already has rather than
        # duplicating it, so without this it would stay listed as "on offer"
        # while sitting in a chain. On offer and chosen are different states
        # and one address cannot be in both.
        for entry in chain.entries:
            if entry.proxy:
                store = store.without_candidate(entry.proxy)
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


# ------------------------------------------------------------------ feeds


#: The most feeds one install may hold. A pass is serial at fifteen seconds a
#: feed, so this is the bound that keeps the Fetch button from being unbounded
#: in wall-clock; it is far above anything an operator would curate by hand.
PROXY_FEED_MAX = PROXY_FEED_MAX_DEFAULT


class ProxyFeedPayload(BaseModel):
    """One feed as the page holds it.

    ``id`` empty means "new" -- which is what makes add and edit the same
    request. A feed the operator did not touch travels back with the id it
    already had, and keeps it.
    """

    id: str = ""
    name: str = ""
    url: str = ""
    parser: str = ""
    enabled: bool = False


class ProxyFeedsPayload(BaseModel):
    """The whole feed list. A ``PUT`` replaces it."""

    feeds: list[ProxyFeedPayload] = Field(default_factory=list)


@router.put("/admin/api/proxy-chains/feeds")
async def put_proxy_feeds(
    payload: ProxyFeedsPayload,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Write the feed list: add, edit, enable and remove, all one write.

    **One write path**, the way the chain ``PUT`` is one write path, and for
    the same reason: the list the operator is looking at *is* the setting, and
    a partial write would need a vocabulary for "move this, rename that, drop
    the other" that the list already says without one.

    Saving makes no outbound request. It records what may be read; the reading
    happens on the Fetch button, or on the timer if the operator also turned
    that on -- two switches, because "may this install read this list" and "may
    it do so unattended" are different questions.

    Removing a feed **keeps the addresses it supplied.** A candidate is an
    independent fact with its own ``source_count`` and its own health record,
    and often the whole reason the feed was added; deleting rows the operator
    may be halfway through choosing from is not what "remove this list" means.
    """

    require_loopback_admin(request)
    feed_max = int(services.requests.current_settings().proxy_feed_max)
    if len(payload.feeds) > feed_max:
        raise HTTPException(
            status_code=422,
            detail=(
                f"At most {feed_max} feeds; this list has "
                f"{len(payload.feeds)}. Each one is a separate request on "
                "every pass."
            ),
        )
    existing = {feed.id: feed for feed in current_proxy_chains().feeds}
    feeds: list[CustomFeed] = []
    seen_urls: set[str] = set()
    for index, entry in enumerate(payload.feeds):
        feeds.append(_feed_from_payload(entry, index, existing, seen_urls))
    await asyncio.to_thread(_commit_feeds, tuple(feeds))
    return await asyncio.to_thread(_payload, services)


def _feed_from_payload(
    entry: ProxyFeedPayload,
    index: int,
    existing: dict[str, CustomFeed],
    seen_urls: set[str],
) -> CustomFeed:
    """Validate one row, carrying across what the page was never told.

    ``assume_protocol`` and the rest are not on the form. They belong to the
    feed, so an edit that only renames a row must not silently strip them --
    which would turn a working plain-text feed into one that fetches fine and
    offers nothing.
    """

    url = entry.url.strip()
    name = entry.name.strip()
    where = name or url or f"feed {index + 1}"
    if not is_valid_feed_url(url):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{where} needs an https URL. A feed is a list of addresses "
                "that will end up in front of a credential, so MCC reads one "
                "only over https -- give the full URL, for example "
                "https://example.com/proxies.json."
            ),
        )
    if url in seen_urls:
        raise HTTPException(
            status_code=422,
            detail=f"{where} is listed twice. One entry per URL.",
        )
    seen_urls.add(url)
    if len(name) > FEED_NAME_MAX_LENGTH:
        raise HTTPException(
            status_code=422,
            detail=(
                f"A feed name may be at most {FEED_NAME_MAX_LENGTH} "
                f"characters; {where}'s is {len(name)}."
            ),
        )
    parser = normalise_parser(entry.parser)
    if entry.parser.strip() and not parser:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Not a format this install reads: {entry.parser}. "
                f"Choose one of: {', '.join(PARSER_IDS)}."
            ),
        )
    if not parser:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{where} needs a format. Press Detect format to have MCC "
                "read the URL once and propose one, or pick one yourself."
            ),
        )
    previous = existing.get(entry.id.strip())
    return CustomFeed(
        id=entry.id.strip(),
        name=name[:FEED_NAME_MAX_LENGTH] or url,
        url=url,
        parser=parser,
        enabled=bool(entry.enabled),
        added_at=(
            previous.added_at
            if previous is not None and previous.added_at
            else datetime.now(UTC).isoformat().replace("+00:00", "Z")
        ),
        tls_strict=previous.tls_strict if previous is not None else False,
        assume_protocol=(
            previous.assume_protocol
            if previous is not None
            else (LINES_DEFAULT_PROTOCOL if parser == "lines" else "")
        ),
        assume_https_ok=previous.assume_https_ok if previous is not None else False,
        assume_anonymity=previous.assume_anonymity if previous is not None else "",
        observed=previous.observed if previous is not None else "",
    )


class ProxyFeedDetectPayload(BaseModel):
    """A URL the operator just typed, to be read once and described."""

    url: str


@router.post("/admin/api/proxy-chains/feeds/detect")
async def detect_proxy_feed(
    payload: ProxyFeedDetectPayload,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Read a URL once and propose a format for it.

    **It proposes; it never decides.** The page's picker is shown either way,
    pre-set to whatever comes back, and the operator's choice is what the save
    stores. A body no reader recognises comes back saying so, with the feed
    still addable -- a list that 404s this afternoon may answer tomorrow, and a
    form that refused to record it would be enforcing a guess about somebody
    else's uptime.

    One outbound request, to a URL the operator typed into a form and pressed a
    button about, which is the same consent the Test button asks for. Nothing
    else here fetches: the feed makes no further request until it is saved,
    switched on, and fetched.
    """

    require_loopback_admin(request)
    url = payload.url.strip()
    if not is_valid_feed_url(url):
        raise HTTPException(
            status_code=422,
            detail=(
                "Give an https URL to read. MCC fetches a proxy list only "
                "over https, for example https://example.com/proxies.json."
            ),
        )
    detection = await detect_feed(url)
    return {
        "detection": detection.as_document(),
        "parsers": [
            {"id": parser.id, "label": parser.label, "shape": parser.shape}
            for parser in PARSERS
        ],
    }


class ProxyIngestPayload(BaseModel):
    """Which provider's host this fetch should test the addresses against.

    Empty means "choose for me", and the choice is
    :func:`fetch_destination`'s: the first provider that has a chain, else the
    first configured provider with an https base URL. A check is a question
    about one destination, so a fetch has to have one before it starts.
    """

    provider: str = ""


def pick_fetch_destination(
    settings: Settings, store: ProxyChains, requested: str = ""
) -> dict[str, Any] | None:
    """The provider a fetch tests against, and its https base URL. Or ``None``.

    The operator's choice when they made one. Otherwise **the first provider
    that has a chain** -- an install with a chain has already said which
    provider it wants proxied, and testing against that one is the answer that
    needs no explaining -- and failing that the first configured provider whose
    base URL is https.

    ``None`` means there is nothing to test against. The route turns that into
    a 422 with the reason on it and the scheduled refresh turns it into a log
    line and no outbound request; neither of them fetches anyway. Storing a
    list of addresses nothing has measured under a heading that says they work
    is the one defect this release exists to remove, and "there was no
    destination" is not a licence to put it back quietly.
    """

    eligible = [
        entry
        for entry in _configured_providers(settings)
        if str(entry.get("base_url") or "").lower().startswith("https://")
    ]
    if requested:
        return next(
            (entry for entry in eligible if entry["provider_id"] == requested), None
        )
    with_chain = next(
        (entry for entry in eligible if store.chain(entry["provider_id"]) is not None),
        None,
    )
    return with_chain or (eligible[0] if eligible else None)


def fetch_destination(
    settings: Settings, store: ProxyChains, requested: str
) -> dict[str, Any]:
    """:func:`pick_fetch_destination`, refusing with the reason instead of ``None``."""

    chosen = pick_fetch_destination(settings, store, requested)
    if chosen is not None:
        return chosen
    if requested:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{requested} is not a configured provider with an https base "
                "URL, so there is no certificate to verify through a tunnel to "
                "it. Pick another destination for this fetch."
            ),
        )
    raise HTTPException(
        status_code=422,
        detail=(
            "No provider on this install has an https base URL, so a fetch "
            "has nothing to test these addresses against -- and an address "
            "MCC has not tested is not one it will offer you. Set a "
            "provider's base URL on the Providers page, then fetch."
        ),
    )


@router.post("/admin/api/proxy-chains/ingest")
async def ingest_proxy_feeds(
    request: Request,
    payload: ProxyIngestPayload | None = None,
    services: ApiServices = Depends(get_services),
):
    """Start a fetch and return at once, with the job id to ask after.

    The one outbound call this feature makes on an install where the scheduled
    refresh is off, and only for the feeds the operator ticked.

    **A fetch now tests what it found.** Every address the enabled lists
    offered is measured against the chosen provider's own host -- does it
    answer, does its tunnel leave that host's certificate verifiable -- and
    only the ones that passed are kept and offered. An address that did not
    answer is not stored; one that broke certificate validation is recorded
    refused so no later fetch offers it again. What the operator ends up
    looking at is a list of addresses that were working a moment ago, not a
    list of claims somebody else published.

    That takes minutes for a list of several hundred, so this route does not
    wait for it: it starts a background job and answers with the id.
    ``GET .../ingest/status`` reports progress, ``POST .../ingest/stop`` ends
    it while keeping everything that has already passed, and a second start
    while one is running is a 409 naming the one that is.
    """

    require_loopback_admin(request)
    store = current_proxy_chains()
    if not store.enabled_feed_ids:
        raise HTTPException(
            status_code=422,
            detail=(
                "No feeds are switched on, so there is nothing to read. MCC "
                "ships none of its own -- add a list above and switch it on, "
                "and it contacts nobody until you do."
            ),
        )
    settings = services.requests.current_settings()
    chosen = fetch_destination(
        settings, store, (payload.provider if payload else "").strip().lower()
    )
    try:
        job = await start_fetch(
            provider_id=str(chosen["provider_id"]),
            destination=str(chosen["base_url"]).strip(),
            concurrency=int(settings.proxy_fetch_test_concurrency),
            connect_timeout=float(settings.proxy_fetch_connect_timeout_seconds),
            concurrency_mode=str(settings.proxy_fetch_concurrency_mode),
            check_depth=str(settings.proxy_fetch_check_depth),
            timeout=float(settings.proxy_check_timeout_seconds),
            feed_timeout=float(settings.proxy_feed_timeout_seconds),
            persist_interval=float(settings.proxy_fetch_persist_interval_seconds),
            limit=int(settings.proxy_candidates_max),
            exit_ip_url=settings.proxy_check_exit_ip_url.strip(),
        )
    except FetchAlreadyRunning as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"A fetch is already running ({exc.job_id}). One at a time: "
                "these are hundreds of outbound connections to strangers' "
                "machines, and two sweeps at once would double that without "
                "finding anything new. Watch it, or stop it, and start again."
            ),
        ) from exc
    refreshed = await asyncio.to_thread(_payload, services)
    refreshed["fetch"] = job.as_document() | {
        "provider_name": str(chosen["display_name"])
    }
    return refreshed


@router.get("/admin/api/proxy-chains/ingest/status")
async def proxy_ingest_status(
    request: Request, services: ApiServices = Depends(get_services)
):
    """What the running -- or last -- fetch is doing, and the page beneath it.

    Answers the same keys whether or not anything has ever run, so the page
    has one shape to read: a fresh process reports ``state: "idle"`` with every
    counter at zero rather than an absence the browser has to guess about.

    The whole payload rides along, which is what lets a reload re-attach: the
    page asks this once on load, finds a job in flight, and picks up the
    progress it left -- and finds the finished candidate list in the same
    answer when the job ended while the tab was closed.
    """

    require_loopback_admin(request)
    refreshed = await asyncio.to_thread(_payload, services)
    status = fetch_status()
    provider_id = str(status.get("provider") or "")
    if provider_id:
        settings = services.requests.current_settings()
        named = next(
            (
                entry
                for entry in _configured_providers(settings)
                if entry["provider_id"] == provider_id
            ),
            None,
        )
        status["provider_name"] = (
            str(named["display_name"]) if named is not None else provider_id
        )
    refreshed["fetch"] = status
    return refreshed


class ProxyIngestStopPayload(BaseModel):
    """Which fetch to stop. Empty means "whichever one is running".

    Naming it is how a page that has been open a while avoids stopping a sweep
    somebody else started after the one it was watching finished.
    """

    job: str = ""


@router.post("/admin/api/proxy-chains/ingest/stop")
async def stop_proxy_ingest(
    request: Request,
    payload: ProxyIngestStopPayload | None = None,
    services: ApiServices = Depends(get_services),
):
    """Stop the running fetch. Settles in seconds. What passed is kept.

    The checks in flight are cancelled, which is what makes "in seconds" true:
    thirty-two addresses mid-handshake with strangers' machines would otherwise
    hold the button at "Stopping..." for as long as the slowest of them cared
    to take -- and, before 7.22.1, for ever if one of them never returned at
    all. A cancelled check costs one verdict about one address, which is what
    Stop is asking for.

    Every address that passed is kept: the sweep writes them to the store in
    batches as it finds them, and the remainder goes in as it settles. "Stop"
    means "that is enough addresses", never "throw the work away" -- an
    operator who has watched forty working addresses arrive out of eight
    hundred should be able to take those forty and get on with it.

    Pressing it twice is the same as pressing it once.
    """

    require_loopback_admin(request)
    stopped = stop_fetch((payload.job if payload else "").strip())
    refreshed = await asyncio.to_thread(_payload, services)
    refreshed["fetch"] = fetch_status()
    refreshed["stopped"] = stopped
    return refreshed


class ProxyCandidateBulkPayload(BaseModel):
    """What to do with a set of candidates, and where.

    ``action`` is ``add`` -- test each address against ``provider``'s own host
    and append the ones that pass to that provider's chain -- or ``discard``,
    which drops the offers and touches no chain at all.

    One destination for the whole set, deliberately: the page used to carry a
    provider ``<select>`` on **every** row, which with 1,572 addresses on offer
    after a seven-feed fetch is 1,572 dropdowns and 1,572 presses. Choosing
    once and applying to a selection is the whole feature.

    ``undo_token`` continues a gesture that is being sent in batches: the first
    batch mints a token, and every later batch that carries it back extends the
    same undo point rather than minting one per batch, so Undo means "before I
    pressed Add", not "before the last ten of them".
    """

    action: str = "add"
    provider: str = ""
    proxies: list[str] = Field(default_factory=list)
    undo_token: str = ""


class ProxyUndoPayload(BaseModel):
    """The token a bulk write handed back, to put the store back as it was."""

    token: str


#: The most addresses one request may carry. The page sends a long selection in
#: batches of ten so it can show progress and stay usable, so this is a bound
#: on a misbehaving caller rather than on an operator: nothing the page does
#: comes close to it, and a chain is not capped at all unless the operator
#: capped it.
PROXY_CANDIDATE_BULK_MAX = PROXY_CANDIDATE_BULK_MAX_DEFAULT

#: Outcomes one address can have, in the words the page reports them with. This
#: is the vocabulary the summary and the per-row state both read from, so a
#: partial result cannot be described two different ways.
CANDIDATE_OUTCOMES: tuple[str, ...] = (
    "added",
    "benched",
    "refused",
    "already",
    "full",
    "gone",
    "discarded",
)

#: The one undo point, and the document it expects to find when it is used.
#: Deliberately a single slot rather than a stack: this is the Models page's
#: one-level undo (6.7.0), and a stack of proxy-store snapshots is a way to
#: restore a document whose middle has moved on.
_UNDO_SLOT: dict[str, Any] = {}
_UNDO_LOCK = threading.Lock()


@router.post("/admin/api/proxy-chains/candidates/bulk")
async def bulk_proxy_candidates(
    payload: ProxyCandidateBulkPayload,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Add a set of candidates to one provider's chain, or discard them.

    **The only write path for a candidate.** A single address is this route
    with one element in ``proxies``; there is no per-row route, because the
    release that let the Models page keep a second single-row write path
    shipped a row that silently skipped the counters (6.24.0).

    The order is the point, and it is unchanged from the single add it
    replaces. A stranger's address is measured against the provider's own host
    -- does it answer, does its tunnel leave that host's certificate verifiable
    -- *before* it is written into a chain that will carry a credential. An
    address that fails the certificate half is refused and stays a candidate;
    an address that merely did not answer is added anyway, benched, because a
    free proxy that is down now is an ordinary thing the chain routes around.

    A mixed result is the **normal** outcome of a bulk add, not an error: these
    are strangers' machines read from public lists. Every address comes back
    with its own outcome and the page reports the groups, so nothing is
    reported as a failed request when eleven of twelve worked.
    """

    require_loopback_admin(request)
    action = payload.action.strip().lower()
    if action not in {"add", "discard"}:
        raise HTTPException(
            status_code=422, detail="action must be one of: add, discard"
        )
    proxies: list[str] = []
    for raw in payload.proxies:
        proxy_id = str(raw).strip()
        if proxy_id and proxy_id not in proxies:
            proxies.append(proxy_id)
    if not proxies:
        raise HTTPException(
            status_code=422,
            detail="Select at least one address first.",
        )
    bulk_max = int(services.requests.current_settings().proxy_candidate_bulk_max)
    if len(proxies) > bulk_max:
        raise HTTPException(
            status_code=422,
            detail=(
                f"At most {bulk_max} addresses in one request; "
                f"this one carries {len(proxies)}."
            ),
        )

    if action == "discard":
        before = await asyncio.to_thread(_snapshot)
        results = await asyncio.to_thread(_commit_discard, proxies)
        token = await asyncio.to_thread(
            _remember_undo, payload.undo_token.strip(), before
        )
        return await _bulk_payload(services, action, "", results, token)

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
                "URL, so a stranger's address cannot be verified against it. "
                "Set its base URL first."
            ),
        )

    store = current_proxy_chains()
    chain = store.chain(provider_id) or EMPTY_CHAIN
    # The same gate the ``PUT`` holds, held here too. A subscription login uses
    # the operator's personal account rather than a metered key, and moving it
    # between source addresses is more likely to be flagged there than
    # anywhere else -- so the acknowledgement is a condition of the chain
    # having entries at all, by whichever door they arrive.
    if provider_id in OAUTH_PROVIDER_IDS and not chain.oauth_acknowledged:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{providers[provider_id]['display_name']} signs in with your "
                "personal subscription. Acknowledge what changing source "
                "address means for it on its card before adding addresses to "
                "its chain."
            ),
        )
    cap = _entry_cap(settings)
    free = len(proxies) if not cap else max(0, cap - len(chain.entries))
    in_chain = {entry.proxy for entry in chain.entries if entry.proxy}

    results: dict[str, dict[str, Any]] = {}
    testable: list[str] = []
    for proxy_id in proxies:
        endpoint = store.endpoint(proxy_id)
        label = (
            ""
            if endpoint is None
            else (endpoint.label or mask_proxy_label(endpoint.url))
        )
        if proxy_id in in_chain:
            results[proxy_id] = _result(proxy_id, label, "already")
        elif proxy_id not in store.candidates:
            results[proxy_id] = _result(proxy_id, label, "gone")
        elif len(testable) >= free:
            results[proxy_id] = _result(proxy_id, label, "full")
        else:
            testable.append(proxy_id)

    # The operator's own fetch number, resolved against how many addresses this
    # gesture is actually about. "Add all working" on a list of three hundred
    # used to re-test them four at a time -- a number chosen for a person
    # ticking a dozen rows -- which turned one press into a quarter of an hour
    # of re-doing a test the fetch had just done. The checks themselves are
    # unchanged: every address here is proven end to end with the full
    # ``request`` depth before it can enter a chain, whatever the sweep was set
    # to, because this is the door a credential goes through.
    pace = resolve_fetch_concurrency(
        requested=int(settings.proxy_fetch_test_concurrency),
        mode=str(settings.proxy_fetch_concurrency_mode),
        offered=len(testable),
    )
    outcomes = (
        await check_endpoints(
            tuple(testable),
            dict.fromkeys(testable, destination),
            timeout=float(settings.proxy_check_timeout_seconds),
            exit_ip_url=settings.proxy_check_exit_ip_url.strip(),
            concurrency=pace.value,
            max_concurrency=PROXY_FETCH_TEST_CONCURRENCY_MAX,
            budget=check_budget(
                connect_timeout=float(settings.proxy_check_timeout_seconds),
                timeout=float(settings.proxy_check_timeout_seconds),
                exit_ip_url=settings.proxy_check_exit_ip_url.strip(),
            ),
        )
        if testable
        else {}
    )
    keep: list[str] = []
    for proxy_id in testable:
        outcome = outcomes.get(proxy_id)
        label = "" if outcome is None else outcome.label
        if outcome is None:
            # Nothing measured it: the address left the store between the read
            # above and the check. Reported, never silently dropped.
            results[proxy_id] = _result(proxy_id, label, "gone")
            continue
        if outcome.refused:
            # The verdict is already durable -- ``check_endpoints`` wrote it to
            # the store and armed the interception ledger before returning --
            # so the refusal survives a reload without this route writing it.
            results[proxy_id] = _result(
                proxy_id,
                label,
                "refused",
                detail=(
                    f"{label} breaks certificate validation: its tunnel "
                    "presented a certificate this machine does not trust, "
                    "which means it is reading the traffic rather than "
                    "relaying it. It was not added, and it stays refused "
                    "until a later test says otherwise."
                ),
            )
            continue
        keep.append(proxy_id)
        results[proxy_id] = _result(
            proxy_id,
            label,
            "added" if outcome.record.ok else "benched",
            detail=outcome.record.detail,
            latency_ms=outcome.record.latency_ms,
        )

    before = await asyncio.to_thread(_snapshot)
    if keep:
        missed = await asyncio.to_thread(
            _commit_promotions, provider_id, keep, _entry_cap(settings)
        )
        for proxy_id in missed:
            # The store moved under the write -- another tab, or a hand edit.
            # Say so rather than reporting an add that did not happen.
            results[proxy_id] = _result(proxy_id, results[proxy_id]["label"], "gone")
        await _republish(services)
    token = (
        await asyncio.to_thread(_remember_undo, payload.undo_token.strip(), before)
        if keep
        else payload.undo_token.strip()
    )
    ordered = [results[proxy_id] for proxy_id in proxies if proxy_id in results]
    return await _bulk_payload(services, action, provider_id, ordered, token)


@router.post("/admin/api/proxy-chains/candidates/undo")
async def undo_proxy_candidates(
    payload: ProxyUndoPayload,
    request: Request,
    services: ApiServices = Depends(get_services),
):
    """Put the store back as it was before one bulk gesture.

    Refuses rather than overwrites when the document has moved on since. A
    snapshot restore is a whole-document write, so undoing across somebody
    else's edit would quietly delete it; the honest answer to that is a
    sentence, not a silent rollback of two changes.
    """

    require_loopback_admin(request)
    restored = await asyncio.to_thread(_commit_undo, payload.token.strip())
    if restored == "unknown":
        raise HTTPException(
            status_code=422,
            detail=(
                "There is nothing to undo any more. An undo point lasts until "
                "the next bulk action or a restart."
            ),
        )
    if restored == "moved":
        raise HTTPException(
            status_code=409,
            detail=(
                "Something else has changed these chains since, so this was "
                "not undone -- putting the whole document back would delete "
                "that change too. Reload the page to see where it stands."
            ),
        )
    await _republish(services)
    return await asyncio.to_thread(_payload, services)


def _result(
    proxy_id: str,
    label: str,
    outcome: str,
    *,
    detail: str = "",
    latency_ms: int | None = None,
) -> dict[str, Any]:
    return {
        "proxy": proxy_id,
        "label": label,
        "outcome": outcome,
        "detail": detail,
        "latency_ms": latency_ms,
    }


async def _bulk_payload(
    services: ApiServices,
    action: str,
    provider_id: str,
    results: list[dict[str, Any]],
    token: str,
) -> dict[str, Any]:
    refreshed = await asyncio.to_thread(_payload, services)
    counts = dict.fromkeys(CANDIDATE_OUTCOMES, 0)
    for row in results:
        counts[row["outcome"]] = counts.get(row["outcome"], 0) + 1
    refreshed["bulk"] = {
        "action": action,
        "provider": provider_id,
        "results": results,
        "counts": counts,
        "undo_token": token,
    }
    return refreshed


def _commit_feeds(feeds: tuple[CustomFeed, ...]) -> None:
    """Replace the feed list. Inside the writer lock, on a fresh read."""

    with _CHAIN_WRITE_LOCK:
        save_proxy_chains(load_proxy_chains().with_feeds(feeds))


def _commit_promotions(
    provider_id: str, proxy_ids: list[str], cap: int = 0
) -> list[str]:
    """Append several candidates to one chain, in **one** write.

    Inside the writer lock and on a store re-read from disk, so a batch that
    lands beside another tab's edit derives from what is actually there. One
    save for the whole batch rather than one per address: the per-address
    version read, derived and wrote N times, which is the read-modify-write
    race 6.7.0 found on the Models page in exactly this shape.

    The chain is created if the provider has none, switched off: adding an
    address is not the same act as arming the chain, and a first address that
    silently started routing would be the surprise this whole page exists to
    avoid.

    Returns the ids that were no longer on offer by the time the lock was held.
    """

    missed: list[str] = []
    with _CHAIN_WRITE_LOCK:
        store = load_proxy_chains()
        chain = store.chain(provider_id) or EMPTY_CHAIN
        entries = list(chain.entries)
        present = {entry.proxy for entry in entries if entry.proxy}
        for proxy_id in proxy_ids:
            if proxy_id in present:
                store = store.without_candidate(proxy_id)
                continue
            if proxy_id not in store.candidates:
                missed.append(proxy_id)
                continue
            if cap and len(entries) >= cap:
                missed.append(proxy_id)
                continue
            entries.append(ProxyChainEntry(proxy=proxy_id, paused=False))
            present.add(proxy_id)
            # Drop it from the offer list first: ``with_chain`` prunes
            # endpoints nothing references, and an address that is about to be
            # referenced by a chain must not pass through a state where it is
            # referenced by neither table.
            store = store.without_candidate(proxy_id)
        save_proxy_chains(
            store.with_chain(provider_id, replace(chain, entries=tuple(entries)))
        )
    return missed


def _commit_discard(proxy_ids: list[str]) -> list[dict[str, Any]]:
    """Drop offers. One write, and no chain is touched.

    Discarding a candidate is not removing a proxy from a chain: it says "do
    not show me this address again", and an address that is already an entry
    somewhere keeps routing exactly as it did.
    """

    results: list[dict[str, Any]] = []
    with _CHAIN_WRITE_LOCK:
        store = load_proxy_chains()
        for proxy_id in proxy_ids:
            endpoint = store.endpoint(proxy_id)
            label = (
                ""
                if endpoint is None
                else (endpoint.label or mask_proxy_label(endpoint.url))
            )
            if proxy_id not in store.candidates:
                results.append(_result(proxy_id, label, "gone"))
                continue
            store = store.without_candidate(proxy_id)
            results.append(_result(proxy_id, label, "discarded"))
        save_proxy_chains(store)
    return results


def _snapshot() -> dict[str, Any]:
    with _CHAIN_WRITE_LOCK:
        return load_proxy_chains().as_document()


def _remember_undo(continuing: str, before: dict[str, Any]) -> str:
    """Record one undo point and hand back its token.

    A token that is handed back extends the gesture it belongs to: the *first*
    batch's "before" is kept and only the "after" moves on, so Undo after a
    selection sent in six batches means before the first of them.
    """

    with _UNDO_LOCK:
        if continuing and _UNDO_SLOT.get("token") == continuing:
            _UNDO_SLOT["after"] = load_proxy_chains().as_document()
            return continuing
        token = f"undo_{int(time.time() * 1000):x}"
        _UNDO_SLOT.clear()
        _UNDO_SLOT.update(
            {
                "token": token,
                "before": before,
                "after": load_proxy_chains().as_document(),
            }
        )
        return token


def _commit_undo(token: str) -> str:
    """``"done"``, ``"unknown"`` for a stale token, ``"moved"`` if it changed."""

    with _UNDO_LOCK, _CHAIN_WRITE_LOCK:
        if not token or _UNDO_SLOT.get("token") != token:
            return "unknown"
        if load_proxy_chains().as_document() != _UNDO_SLOT.get("after"):
            return "moved"
        save_proxy_chains(ProxyChains.from_document(_UNDO_SLOT["before"]))
        _UNDO_SLOT.clear()
        return "done"


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
            "max_entries": _entry_cap(settings),
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
            # What, if anything, re-reads the public lists without being asked.
            # Off is the shipped answer to both halves and the page says so,
            # because "no feeds are selected" and "the timer is off" are
            # different reasons for an empty candidate list.
            "refresh": {
                "enabled": bool(settings.proxy_feed_refresh_enabled),
                "interval_minutes": int(settings.proxy_feed_refresh_minutes),
                "minimum_minutes": PROXY_FEED_MINIMUM_MINUTES,
            },
            # What a press of Fetch is about to do, in the operator's own
            # numbers. The page prints these rather than a constant of its own:
            # 7.19.0 shipped "a chain holds at most 12" on a card with two
            # hundred rows because the browser kept its own copy of a limit the
            # server had stopped applying.
            "fetch": {
                "concurrency": int(settings.proxy_fetch_test_concurrency),
                # How that number is read, and how far each test goes. Both
                # travel because both change what a press of Fetch does to
                # somebody else's machines, and the page must not keep its own
                # opinion about either.
                "concurrency_mode": str(settings.proxy_fetch_concurrency_mode),
                "check_depth": str(settings.proxy_fetch_check_depth),
                "connect_timeout_seconds": float(
                    settings.proxy_fetch_connect_timeout_seconds
                ),
                "check_timeout_seconds": float(settings.proxy_check_timeout_seconds),
                # 0 is UNLIMITED and is what ships. It travels as 0, and the
                # page must read it with a test for "is it a positive number",
                # never with `Number(x) || <something>` -- which cannot tell 0
                # from absent and is exactly how 7.19.0 put the old cap back.
                "candidates_max": int(settings.proxy_candidates_max),
            },
            # The readers this install ships, for the Add form's picker. MCC
            # ships no feed of its own, so this is the whole of what the page
            # can offer: formats, never sources.
            "parsers": [
                {"id": parser.id, "label": parser.label, "shape": parser.shape}
                for parser in PARSERS
            ],
            "feed_name_max_length": FEED_NAME_MAX_LENGTH,
            "max_feeds": int(settings.proxy_feed_max),
        },
        "feeds": feed_payload(store),
        "candidates": [
            _candidate_payload(proxy_id, store) for proxy_id in store.candidates
        ],
        "providers": [
            _provider_payload(entry, store) for entry in _configured_providers(settings)
        ],
    }


def _candidate_payload(proxy_id: str, store: ProxyChains) -> dict[str, Any]:
    """One address on offer, and where it came from.

    The URL never leaves the server -- the same rule every other row on this
    page follows -- so what travels is the masked ``host:port``, the scheme,
    the feeds that listed it **by name**, and what those feeds said. Naming the
    feeds is the point of ``source_count``: a number alone says "four agree"
    where the list says which four, and an operator deciding whether to put a
    stranger's machine in front of a credential should be able to see that
    without leaving the page.
    """

    endpoint = store.endpoint(proxy_id)
    if endpoint is None:  # pragma: no cover - candidates are pruned with proxies
        return {"proxy": proxy_id, "label": "", "sources": []}
    facts = endpoint.feed
    last_check = endpoint.last_check
    return {
        "proxy": proxy_id,
        "label": endpoint.label or mask_proxy_label(endpoint.url),
        "scheme": _scheme(endpoint.url),
        "source_count": endpoint.source_count,
        "sources": [
            {"id": feed_id, "name": known_feed_name(store, feed_id)}
            for feed_id in endpoint.sources
        ],
        "country": facts.country if facts is not None else "",
        "anonymity": facts.anonymity if facts is not None else "",
        "https_ok": bool(facts is not None and facts.https_ok),
        "latency_ms": facts.latency_ms if facts is not None else None,
        "uptime_pct": facts.uptime_pct if facts is not None else None,
        "last_check": None if last_check is None else last_check.as_document(),
        "refused": bool(endpoint.refused),
        # Since 7.21.0 a fetch tests everything it offers and keeps only the
        # addresses that passed, so on a freshly fetched list this is true of
        # every row -- and the three fields below are what let the page say so
        # honestly rather than by assumption.
        #
        # ``working`` is the measurement: this address answered and the
        # destination's certificate verified through its tunnel.
        "working": bool(last_check is not None and last_check.ok),
        # Which provider's host that was measured against. A check answers one
        # question about one destination, so "working" without this would be a
        # claim about hosts nobody asked about. Empty for a candidate stored by
        # 7.18-7.20, which did not record it.
        "checked_for": endpoint.checked_for,
        "checked_for_name": (
            _display_name(endpoint.checked_for) if endpoint.checked_for else ""
        ),
        # A candidate from 7.18-7.20: offered, never measured. It is shown --
        # dropping somebody's stored list on an upgrade would be a destructive
        # migration nobody asked for -- but it is never counted as working, and
        # the next fetch replaces the offer list wholesale, so it clears itself
        # the first time the operator presses the button.
        "untested": bool(last_check is None),
    }


def _display_name(provider_id: str) -> str:
    """A provider's display name, falling back to its id.

    Read from the catalogue and the custom registry rather than from
    ``_configured_providers``: a candidate may have been tested against a
    provider whose key has since been removed, and "working for opencode" is a
    better answer than an empty cell.
    """

    descriptor = PROVIDER_CATALOG.get(provider_id)
    if descriptor is not None:
        return descriptor.display_name
    for entry in get_provider_registry().list_custom():
        if entry.provider_id == provider_id:
            return entry.display_name
    return provider_id


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
        "direct_fallback": chain.direct_fallback,
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
