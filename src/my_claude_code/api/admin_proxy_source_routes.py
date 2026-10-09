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
"""

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from loguru import logger
from pydantic import BaseModel, Field

from my_claude_code.api.admin_proxy_routes import (
    _payload,
    _republish,
    changed_chain_providers,
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
from my_claude_code.config.proxy_chains import (
    PROXY_CHAINS_WRITE_LOCK,
    ProxyChains,
    current_proxy_chains,
    load_proxy_chains,
    save_proxy_chains,
)
from my_claude_code.config.proxy_sources import (
    BUILT_KINDS,
    PROXY_SOURCES_WRITE_LOCK,
    SOURCE_KINDS,
    current_proxy_sources,
    load_proxy_sources,
    save_proxy_sources,
)
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


class ProxySourcePayload(BaseModel):
    """One edit to one source."""

    source: str
    #: Only to say which kind a NEW source would be; refused for every kind
    #: this release does not build.
    kind: str = ""
    remove: bool = False
    credentials: list[SourceCredentialPayload] = Field(default_factory=list)


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
    if existing is None:
        kind = payload.kind.strip().lower()
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
    try:
        changed = await asyncio.to_thread(_commit_edit, source_id, payload)
    except SourceEditError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    except OSError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    failure = await _republish(services, changed) if changed else ""
    refreshed = await asyncio.to_thread(_payload, services)
    if failure:
        refreshed["republish_failed"] = failure
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
