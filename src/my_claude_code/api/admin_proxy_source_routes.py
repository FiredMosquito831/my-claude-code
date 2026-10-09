"""Proxy source routes: the Proxying page's "Sources" section (7.89.0, PR-S3).

Three loopback-only routes, beside the chain routes they feed:

* ``GET /admin/api/proxy-sources`` -- every source, masked: a stored login is
  ``secret_set`` plus the username's ``first4…last4``, never the username or
  the password, and an address is its ledger label, never its URL.
* ``POST /admin/api/proxy-sources/scan`` -- scan this computer
  (``127.0.0.1`` only) for a proxy somebody already runs, and offer each one
  that answered. The only thing that scans; nothing does on its own.
* ``PUT /admin/api/proxy-sources`` -- edit one source: give a listener that
  asked for one its username and password, forget it, or remove the source.
  A source of a kind a later release builds cannot be created here yet.

Adding an offered address to a chain is not a route of this file: it is the
chain page's one bulk add (``POST /admin/api/proxy-chains/candidates/bulk``),
which tests it against the provider's own host first.

7.90.0 (bring-your-own Tor) adds a ``tor`` source -- created and changed by
the same ``PUT`` with ``kind: "tor"`` and a ``tor`` block -- and its two
buttons, loopback-only like everything here:

* ``POST /admin/api/proxy-sources/tor/status`` -- *Check Tor*;
* ``POST /admin/api/proxy-sources/tor/newnym`` -- *New Tor identity*.

Both talk to ``127.0.0.1:<control port>`` and nothing else, and answer the
refreshed Proxying payload plus ``tor_result`` (what the press did, in a
sentence). MCC never starts tor and never presses either button by itself.

7.91.0 (vendor presets, PR-S4 + PR-S5) adds the ``account``, ``gateway`` and
``list`` kinds -- created and changed by the same ``PUT`` with
``kind`` and an ``account`` / ``gateway`` / ``proxy_list`` block -- and:

* ``GET /admin/api/proxy-sources/presets`` -- what the forms prefill from:
  each vendor's documented host, port, session syntax and documentation
  link. Data, never an offer: nothing is stored or contacted.
* ``POST /admin/api/proxy-sources/fetch`` -- *Fetch now*: read a source's
  vendor list (NordVPN's server list, a proxy list's download link) from the
  URL the operator confirmed, through the chain they named for it, if any.
  Answers the refreshed payload plus ``fetch_result``.

A save contacts nothing. A fetch happens only on that press, or on the
source's own schedule, which is off until the operator picks one.
"""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field

from my_claude_code.api.admin_proxy_routes import (
    _payload,
    _republish,
    changed_chain_providers,
    configured_provider_ids,
    vendor_fetch_exit,
)
from my_claude_code.api.admin_routes import require_loopback_admin
from my_claude_code.api.dependencies import get_services
from my_claude_code.api.ports import ApiServices
from my_claude_code.application.proxy_sources import (
    ScanAnswer,
    SourceEditError,
    apply_local_scan,
    log_scan,
    remove_source,
    scan_local,
    scan_summary,
    set_local_credential,
    sources_document,
)
from my_claude_code.application.proxy_vendor_sources import (
    FETCHES_IN_FLIGHT,
    SOURCE_SCHEDULE,
    AccountEdit,
    FetchedText,
    FetchOutcome,
    GatewayEdit,
    ListEdit,
    apply_fetched,
    fetch_plan,
    fetch_vendor_text,
    record_fetch_failure,
    save_account_source,
    save_gateway_source,
    save_list_source,
)
from my_claude_code.application.tor_source import (
    TorEdit,
    check_tor,
    forget_readings,
    new_tor_identity,
    save_tor_source,
)
from my_claude_code.config.proxy_chains import (
    PROXY_CHAINS_WRITE_LOCK,
    ProxyChains,
    current_proxy_chains,
    load_proxy_chains,
    save_proxy_chains,
)
from my_claude_code.config.proxy_presets import presets_document
from my_claude_code.config.proxy_source_readers import url_host
from my_claude_code.config.proxy_sources import (
    BUILT_KINDS,
    KIND_ACCOUNT,
    KIND_GATEWAY,
    KIND_LIST,
    KIND_TOR,
    PROXY_SOURCES_WRITE_LOCK,
    SOURCE_KINDS,
    TOR_AUTH_COOKIE,
    VENDOR_KINDS,
    ProxySource,
    ProxySources,
    current_proxy_sources,
    load_proxy_sources,
    save_proxy_sources,
)
from my_claude_code.config.settings import Settings
from my_claude_code.core.loop_health import loop_health

router = APIRouter()


@router.get("/admin/api/proxy-sources")
async def get_proxy_sources(request: Request) -> dict[str, Any]:
    """Every source, masked through and through."""

    require_loopback_admin(request)
    return sources_document(current_proxy_chains(), current_proxy_sources())


@router.post("/admin/api/proxy-sources/scan")
async def scan_proxy_sources(
    request: Request, services: ApiServices = Depends(get_services)
) -> dict[str, Any]:
    """Look for a proxy already running on this computer, and offer it.

    ``127.0.0.1`` only, the ports in ``LOCAL_SCAN_PORTS``, two questions each
    and no ``CONNECT`` anywhere but this computer's own discard port. Answers
    the whole refreshed Proxying payload, plus ``scan`` -- which ports were
    asked, which answered and which are offered.
    """

    require_loopback_admin(request)
    with loop_health().working("this computer is being scanned for proxies"):
        answers = await scan_local()
    log_scan(answers)
    try:
        await asyncio.to_thread(_commit_scan, answers)
    except OSError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    refreshed = await asyncio.to_thread(_payload, services)
    refreshed["scan"] = scan_summary(answers)
    return refreshed


def _commit_scan(answers: list[ScanAnswer]) -> None:
    # Always the chain lock first, then the sources lock: the one order every
    # writer of both files takes them in.
    with PROXY_CHAINS_WRITE_LOCK, PROXY_SOURCES_WRITE_LOCK:
        chains, sources = apply_local_scan(
            load_proxy_chains(), load_proxy_sources(), answers
        )
        save_proxy_sources(sources)
        save_proxy_chains(chains)


class SourceCredentialPayload(BaseModel):
    """A login for one listener. The password is write-only: nothing sends it back."""

    port: int
    username: str = ""
    password: str = ""
    clear: bool = False


class TorSourcePayload(BaseModel):
    """A Tor source's ports and login (7.90.0). The password is write-only."""

    socks_ports: list[int] = Field(default_factory=list)
    control_port: int = 0
    auth: str = TOR_AUTH_COOKIE
    #: Empty keeps a stored password. Nothing ever sends one back.
    password: str = ""


class AccountSourcePayload(BaseModel):
    """A VPN account (7.91.0). ``password`` and ``list_url`` are write-only:
    empty keeps what is stored, and nothing ever sends either back."""

    name: str = ""
    preset: str = ""
    scheme: str = "socks5h"
    port: int = 1080
    hosts: str = ""
    username: str = ""
    password: str = ""
    clear_login: bool = False
    in_tunnel: bool = False
    list_url: str = ""
    list_off: bool = False
    countries: list[str] = Field(default_factory=list)
    fetch_via: str = ""
    refresh_hours: int = 0


class GatewaySourcePayload(BaseModel):
    """A commercial gateway (7.91.0). ``password`` is write-only."""

    preset: str = ""
    name: str = ""
    host: str = ""
    port: int = 0
    scheme: str = "socks5h"
    user: str = ""
    password: str = ""
    zone: str = ""
    zone_type: str = ""
    country: str = ""
    minutes: int = 0
    count: int = 1
    #: Mint every session again: N new ids, N new addresses at the vendor.
    renew: bool = False


class ListSourcePayload(BaseModel):
    """A proxy list (7.91.0). ``url`` (a download link with the vendor's
    token) is write-only; ``paste`` replaces the rows."""

    name: str = ""
    preset: str = ""
    scheme: str = "socks5h"
    paste: str = ""
    url: str = ""
    clear_url: bool = False
    fetch_via: str = ""
    refresh_hours: int = 0


class ProxySourcePayload(BaseModel):
    """One edit to one source."""

    source: str
    #: Only to say which kind a NEW source would be; refused for every kind
    #: this release does not build. ``tor`` with a ``tor`` block creates one,
    #: and since 7.91.0 ``account`` / ``gateway`` / ``list`` with theirs.
    kind: str = ""
    remove: bool = False
    credentials: list[SourceCredentialPayload] = Field(default_factory=list)
    tor: TorSourcePayload | None = None
    account: AccountSourcePayload | None = None
    gateway: GatewaySourcePayload | None = None
    proxy_list: ListSourcePayload | None = None

    def vendor_block(
        self,
    ) -> AccountSourcePayload | GatewaySourcePayload | ListSourcePayload | None:
        return self.account or self.gateway or self.proxy_list


class SourceButtonPayload(BaseModel):
    """Which source a button press is for."""

    source: str


class TorButtonPayload(BaseModel):
    """Which Tor source a button press is for."""

    source: str


@router.put("/admin/api/proxy-sources")
async def put_proxy_source(
    payload: ProxySourcePayload,
    request: Request,
    services: ApiServices = Depends(get_services),
) -> dict[str, Any]:
    """Edit one source and answer the refreshed Proxying payload.

    A login change rewrites the offered address's URL -- the id, the name and
    every chain using it stay -- and rebuilds exactly the providers whose
    chains dial it. A removal withdraws the source's offers; a chain that took
    one keeps it.
    """

    require_loopback_admin(request)
    source_id = payload.source.strip()
    existing = current_proxy_sources().source(source_id)
    kind = payload.kind.strip().lower()
    if (
        existing is None
        and kind == KIND_TOR
        and payload.tor is not None
        and not payload.remove
    ):
        return await _put_tor(services, "", payload.tor)
    if existing is None and kind in VENDOR_KINDS and not payload.remove:
        return await _put_vendor(services, "", kind, payload)
    if (
        existing is not None
        and existing.vendor is not None
        and payload.vendor_block() is not None
        and not payload.remove
    ):
        return await _put_vendor(services, source_id, existing.kind, payload)
    if existing is not None and existing.vendor is not None and payload.credentials:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{existing.name or source_id} keeps its login in its own form, "
                "not as a listener login."
            ),
        )
    if existing is None:
        if kind in SOURCE_KINDS and kind not in BUILT_KINDS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"A {kind} source comes in a later release. This one finds "
                    "proxies already running on this computer: press Scan "
                    "this computer."
                ),
            )
        raise HTTPException(
            status_code=404,
            detail=f"No source {source_id!r}. Press Scan this computer first.",
        )
    if not existing.built:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{existing.name or source_id} is a {existing.kind} source, which "
                "this release keeps but does not edit."
            ),
        )
    if payload.tor is not None and not payload.remove:
        if existing.tor is None:
            raise HTTPException(
                status_code=422,
                detail=f"{existing.name or source_id} is not a Tor source.",
            )
        return await _put_tor(services, source_id, payload.tor)
    if existing.tor is not None and payload.credentials:
        raise HTTPException(
            status_code=422,
            detail=(
                "A Tor source has no listener logins: its control password "
                "goes in the Tor form."
            ),
        )
    try:
        changed = await asyncio.to_thread(_commit_edit, source_id, payload)
    except SourceEditError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except OSError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if existing.tor is not None:
        forget_readings([source_id])
    if existing.vendor is not None:
        # A removed source's schedule ends with it.
        SOURCE_SCHEDULE.changed()
    failure = await _republish(services, changed) if changed else ""
    refreshed = await asyncio.to_thread(_payload, services)
    if failure:
        refreshed["republish_failed"] = failure
    return refreshed


def _vendor_edit(
    kind: str, payload: ProxySourcePayload
) -> AccountEdit | GatewayEdit | ListEdit:
    """The form's block as the edit the store applies, or a 422."""

    if kind == KIND_ACCOUNT and payload.account is not None:
        block = payload.account
        return AccountEdit(
            name=block.name,
            preset=block.preset,
            scheme=block.scheme,
            port=block.port,
            hosts=block.hosts,
            username=block.username,
            password=block.password,
            clear_login=block.clear_login,
            in_tunnel=block.in_tunnel,
            list_url=block.list_url,
            list_off=block.list_off,
            countries=tuple(block.countries),
            fetch_via=block.fetch_via,
            refresh_hours=block.refresh_hours,
        )
    if kind == KIND_GATEWAY and payload.gateway is not None:
        gateway = payload.gateway
        return GatewayEdit(
            preset=gateway.preset,
            name=gateway.name,
            host=gateway.host,
            port=gateway.port,
            scheme=gateway.scheme,
            user=gateway.user,
            password=gateway.password,
            zone=gateway.zone,
            zone_type=gateway.zone_type,
            country=gateway.country,
            minutes=gateway.minutes,
            count=gateway.count,
            renew=gateway.renew,
        )
    if kind == KIND_LIST and payload.proxy_list is not None:
        listed = payload.proxy_list
        return ListEdit(
            name=listed.name,
            preset=listed.preset,
            scheme=listed.scheme,
            paste=listed.paste,
            url=listed.url,
            clear_url=listed.clear_url,
            fetch_via=listed.fetch_via,
            refresh_hours=listed.refresh_hours,
        )
    block_name = "proxy_list" if kind == KIND_LIST else kind
    raise HTTPException(
        status_code=422,
        detail=f"Send the {kind} source's settings in a {block_name!r} block.",
    )


def _check_fetch_via(services: ApiServices, edit: object) -> None:
    via = str(getattr(edit, "fetch_via", "") or "").strip().lower()
    if not via:
        return
    settings = services.requests.current_settings()
    if via not in configured_provider_ids(settings):
        raise HTTPException(
            status_code=422,
            detail=(
                f"{via!r} is not a configured provider, so there is no chain "
                "to fetch through. Choose one of the providers on this page, "
                "or this computer."
            ),
        )


async def _put_vendor(
    services: ApiServices, source_id: str, kind: str, payload: ProxySourcePayload
) -> dict[str, Any]:
    """Create (``source_id`` empty) or change a vendor source. Contacts nothing."""

    edit = _vendor_edit(kind, payload)
    await asyncio.to_thread(_check_fetch_via, services, edit)
    try:
        changed, saved, offered = await asyncio.to_thread(
            _commit_vendor, source_id, kind, edit
        )
    except SourceEditError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except OSError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    # A schedule switched on (or off, or a link changed) counts from now.
    SOURCE_SCHEDULE.changed()
    failure = await _republish(services, changed) if changed else ""
    refreshed = await asyncio.to_thread(_payload, services)
    if failure:
        refreshed["republish_failed"] = failure
    refreshed["source_result"] = {
        "action": "saved",
        "source": saved,
        "kind": kind,
        "offered": offered,
        "sentence": _saved_sentence(kind, offered),
    }
    return refreshed


def _saved_sentence(kind: str, offered: int) -> str:
    noun = {KIND_ACCOUNT: "address", KIND_GATEWAY: "session", KIND_LIST: "proxy"}[kind]
    plural = {"address": "addresses", "session": "sessions", "proxy": "proxies"}[noun]
    if not offered:
        return (
            "Saved. Nothing is on offer yet: press Fetch now to read the list "
            "from the vendor."
        )
    return (
        f"Saved: {offered} {noun if offered == 1 else plural} on offer. Choose a "
        "chain and press Add to chain -- each is tested against that "
        "provider's own host first. Nothing was contacted."
    )


def _commit_vendor(
    source_id: str, kind: str, edit: AccountEdit | GatewayEdit | ListEdit
) -> tuple[frozenset[str], str, int]:
    with PROXY_CHAINS_WRITE_LOCK, PROXY_SOURCES_WRITE_LOCK:
        before = load_proxy_chains()
        sources = load_proxy_sources()
        if isinstance(edit, AccountEdit):
            chains, stored, saved = save_account_source(
                before, sources, source_id, edit
            )
        elif isinstance(edit, GatewayEdit):
            chains, stored, saved = save_gateway_source(
                before, sources, source_id, edit
            )
        else:
            chains, stored, saved = save_list_source(before, sources, source_id, edit)
        save_proxy_sources(stored)
        save_proxy_chains(chains)
    offered = len(chains.source_offers.get(saved, ()))
    logger.info(
        "PROXY SOURCES: {} source {} saved: {} address(es) on offer",
        kind,
        saved,
        offered,
    )
    return changed_chain_providers(before, chains), saved, offered


async def _put_tor(
    services: ApiServices, source_id: str, tor: TorSourcePayload
) -> dict[str, Any]:
    """Create (``source_id`` empty) or change a Tor source. Contacts nothing."""

    edit = TorEdit(
        socks_ports=tuple(tor.socks_ports),
        control_port=tor.control_port,
        auth=tor.auth.strip().lower(),
        password=tor.password,
    )
    try:
        changed, saved = await asyncio.to_thread(_commit_tor, source_id, edit)
    except SourceEditError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except OSError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    # The last reading was about the old ports or login.
    forget_readings([saved])
    failure = await _republish(services, changed) if changed else ""
    refreshed = await asyncio.to_thread(_payload, services)
    if failure:
        refreshed["republish_failed"] = failure
    refreshed["tor_result"] = {
        "action": "saved",
        "source": saved,
        "sentence": (
            "Saved. Paste the torrc lines into your tor's torrc, restart tor, "
            "then press Check Tor."
        ),
    }
    return refreshed


def _commit_tor(source_id: str, edit: TorEdit) -> tuple[frozenset[str], str]:
    with PROXY_CHAINS_WRITE_LOCK, PROXY_SOURCES_WRITE_LOCK:
        before = load_proxy_chains()
        chains, sources, saved = save_tor_source(
            before, load_proxy_sources(), source_id, edit
        )
        save_proxy_sources(sources)
        save_proxy_chains(chains)
    logger.info(
        "PROXY SOURCES: Tor source {} saved: SOCKS ports {}, control port {}, "
        "login by {}",
        saved,
        ", ".join(str(port) for port in dict.fromkeys(edit.socks_ports)),
        edit.control_port,
        edit.auth,
    )
    return changed_chain_providers(before, chains), saved


def _tor_source(source_id: str) -> ProxySource:
    source = current_proxy_sources().source(source_id.strip())
    if source is None or source.tor is None:
        raise HTTPException(
            status_code=404,
            detail=f"No Tor source {source_id!r}. Save your tor's ports first.",
        )
    return source


@router.post("/admin/api/proxy-sources/tor/status")
async def check_tor_source(
    payload: TorButtonPayload,
    request: Request,
    services: ApiServices = Depends(get_services),
) -> dict[str, Any]:
    """*Check Tor*: log in to ``127.0.0.1:<control port>`` and read its facts.

    Version, whether a circuit is established, how far it bootstrapped, and
    which SOCKS ports tor itself says it listens on. Nothing else is dialled.
    """

    require_loopback_admin(request)
    source = _tor_source(payload.source)
    with loop_health().working("a tor control port is being asked"):
        reading = await check_tor(source, current_proxy_sources())
    refreshed = await asyncio.to_thread(_payload, services)
    refreshed["tor_result"] = {
        "action": "status",
        "source": source.id,
        "ok": reading.ok,
        "sentence": reading.sentence,
    }
    return refreshed


@router.post("/admin/api/proxy-sources/tor/newnym")
async def new_tor_identity_route(
    payload: TorButtonPayload,
    request: Request,
    services: ApiServices = Depends(get_services),
) -> dict[str, Any]:
    """*New Tor identity*: ``SIGNAL NEWNYM`` to ``127.0.0.1:<control port>``.

    Refused here -- nothing sent -- within 10 s of the last one, with the
    seconds still to wait. Only a press sends one: no refusal, timer or
    rotation ever does (the user's decision 5(a) of 2026-10-06).
    """

    require_loopback_admin(request)
    source = _tor_source(payload.source)
    with loop_health().working("a tor control port is being asked"):
        outcome = await new_tor_identity(source, current_proxy_sources())
    refreshed = await asyncio.to_thread(_payload, services)
    refreshed["tor_result"] = outcome.as_payload() | {"source": source.id}
    return refreshed


def _commit_edit(source_id: str, payload: ProxySourcePayload) -> frozenset[str]:
    with PROXY_CHAINS_WRITE_LOCK, PROXY_SOURCES_WRITE_LOCK:
        before = load_proxy_chains()
        chains: ProxyChains = before
        sources = load_proxy_sources()
        if payload.remove:
            chains, sources = remove_source(chains, sources, source_id)
            logger.info("PROXY SOURCES: removed source {}", source_id)
        for credential in payload.credentials:
            chains, sources = set_local_credential(
                chains,
                sources,
                credential.port,
                username=credential.username,
                password=credential.password,
                clear=credential.clear,
            )
            logger.info(
                "PROXY SOURCES: login {} for the listener on 127.0.0.1:{}",
                "cleared" if credential.clear else "set",
                credential.port,
            )
        save_proxy_sources(sources)
        save_proxy_chains(chains)
    return changed_chain_providers(before, chains)


# ---------------------------------------------------- vendor presets (7.91.0)


@router.get("/admin/api/proxy-sources/presets")
async def get_proxy_source_presets(request: Request) -> dict[str, Any]:
    """What the vendor forms prefill from. Data only: nothing is contacted."""

    require_loopback_admin(request)
    return presets_document()


def _fetch_failure(source_id: str, sentence: str) -> FetchOutcome:
    with PROXY_SOURCES_WRITE_LOCK:
        sources, outcome = record_fetch_failure(
            load_proxy_sources(), source_id, sentence
        )
        save_proxy_sources(sources)
    return outcome


def _commit_fetched(
    source_id: str, fetched: FetchedText
) -> tuple[FetchOutcome, frozenset[str]]:
    # Read the answer into the stores as they are NOW: an edit made while the
    # fetch ran is kept, and a source removed meanwhile stays removed.
    with PROXY_CHAINS_WRITE_LOCK, PROXY_SOURCES_WRITE_LOCK:
        before = load_proxy_chains()
        chains, sources, outcome = apply_fetched(
            before, load_proxy_sources(), source_id, fetched
        )
        if sources.source(source_id) is not None:
            save_proxy_sources(sources)
            save_proxy_chains(chains)
    return outcome, changed_chain_providers(before, chains)


async def fetch_source_now(
    settings: Settings,
    source_id: str,
    republish: Callable[[Iterable[str]], Awaitable[object]],
) -> FetchOutcome:
    """Fetch one source's vendor list now: *Fetch now*, and the schedule's tick.

    The URL is the one the operator confirmed, read from the owner-only
    store and never logged. With a chain named for the source, the fetch
    leaves through that provider's exit (:func:`vendor_fetch_exit`) and sends
    nothing at all where the chain refuses; with none, it leaves from this
    computer, as a feed's fetch does. One fetch per source at a time. The
    request is one bounded ``await``; reading the answer and writing both
    stores happen on a worker thread.
    """

    sources = await asyncio.to_thread(current_proxy_sources)
    source = sources.source(source_id)
    if source is None or source.vendor is None:
        return FetchOutcome(source_id, False, "Not fetched: no such source.")
    plan = fetch_plan(source, sources)
    if plan is None:
        return FetchOutcome(
            source_id,
            False,
            "Nothing to fetch: give the list's download link, or the vendor's "
            "server list URL, first.",
        )
    if not FETCHES_IN_FLIGHT.claim(source_id):
        return FetchOutcome(
            source_id, False, "A fetch of this source is already running."
        )
    try:
        proxy: str | None = None
        through = "this computer"
        if plan.via:
            exit_ = await asyncio.to_thread(vendor_fetch_exit, settings, plan.via)
            if exit_.refused:
                outcome = await asyncio.to_thread(
                    _fetch_failure, source_id, exit_.refused
                )
                logger.info(
                    "PROXY SOURCES: {} not fetched: the {} chain refused it",
                    source_id,
                    plan.via,
                )
                return outcome
            proxy = exit_.proxy
            through = exit_.label or (
                "this computer" if not exit_.proxy else f"{plan.via}'s route"
            )
        with loop_health().working("a vendor's proxy list is being fetched"):
            fetched = await fetch_vendor_text(
                plan.url,
                proxy=proxy,
                timeout=float(settings.proxy_feed_timeout_seconds),
            )
        outcome, changed = await asyncio.to_thread(_commit_fetched, source_id, fetched)
    finally:
        FETCHES_IN_FLIGHT.release(source_id)
    logger.info(
        "PROXY SOURCES: fetched {} from {} through {}: {}",
        source_id,
        url_host(plan.url),
        through,
        outcome.sentence,
    )
    if changed:
        await republish(changed)
    return outcome


@router.post("/admin/api/proxy-sources/fetch")
async def fetch_proxy_source(
    payload: SourceButtonPayload,
    request: Request,
    services: ApiServices = Depends(get_services),
) -> dict[str, Any]:
    """*Fetch now*: read one source's vendor list from the URL the operator
    confirmed, through the chain they named for it, and offer what it lists."""

    require_loopback_admin(request)
    source_id = payload.source.strip()
    sources: ProxySources = await asyncio.to_thread(current_proxy_sources)
    source = sources.source(source_id)
    if source is None or source.vendor is None:
        raise HTTPException(
            status_code=404, detail=f"No account, gateway or list source {source_id!r}."
        )
    outcome = await fetch_source_now(
        services.requests.current_settings(),
        source_id,
        lambda changed: _republish(services, changed),
    )
    refreshed = await asyncio.to_thread(_payload, services)
    refreshed["fetch_result"] = {
        "source": source_id,
        "ok": outcome.ok,
        "offered": outcome.offered,
        "sentence": outcome.sentence,
    }
    return refreshed
