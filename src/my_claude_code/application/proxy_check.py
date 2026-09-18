"""Measure one proxy before a credential is carried through it.

Three questions, in order, and the second one is not a convenience:

1. **Does the address answer at all?** A TCP connect to the proxy's own
   host and port. A free address that has stopped listening is the commonest
   thing in this catalogue and it costs one socket to find out.
2. **Does the tunnel preserve certificate validation?** An HTTPS request
   *through* the proxy to the provider's own host, built with an ordinary
   client and nothing said about trust -- which is the whole point, and which
   ``tests/contracts/test_tls_verification_is_never_weakened.py`` is what keeps
   true across this package. If the certificate that comes back does not verify, something
   between here and the provider terminated the TLS and is reading the
   plaintext: an API key, an OAuth token, a prompt, a reply. That address is
   marked :data:`~my_claude_code.config.proxy_chains.TLS_INTERCEPTED` and
   **refused** -- it cannot be added to a chain, and one already in a chain is
   held out of selection by the runtime's own ledger. This is the one test that
   catches that class of proxy, and it costs nothing beyond a request the
   checker was already making.
3. **How long did it take?** Wall-clock around step 2, which is the number that
   matters: a handshake through the tunnel to the host the chain will actually
   use, not a ping.

The destination is **the provider's own base-URL host**, never a third-party
echo service. It is the only destination that tests what the chain will
actually do, and it is an address the operator already chose to talk to.

**The exit-IP check is opt-in and against a URL the operator types.** It proves
the address really changed, which is the whole point of the feature, and it is
an outbound request to a stranger. MCC ships no default URL and makes no such
call unless one is configured.

Out of band, always. Nothing here runs inside a request, nothing on the request
path imports this module, and every network call is an ordinary await on the
loop that started it -- the checker's own timer yields between addresses so a
sweep of a long catalogue cannot sit in front of ``/v1/messages``.
"""

import asyncio
import contextlib
import ssl
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from loguru import logger

from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.provider_registry import get_provider_registry
from my_claude_code.config.proxy_chains import (
    TLS_INTERCEPTED,
    TLS_STRICT,
    TLS_UNKNOWN,
    ProxyChains,
    ProxyCheckRecord,
    load_proxy_chains,
    save_proxy_chains,
)
from my_claude_code.core.proxy_rotation import PROXY_INTERCEPTION, PROXY_REACHABILITY

#: How long any single leg of the check may take. Deliberately shorter than the
#: request path's own connect timeout: a checker that waits sixty seconds on a
#: dead address turns a sweep of twelve into four minutes of nothing.
PROXY_CHECK_TIMEOUT_SECONDS = 10.0

#: The most bytes of an exit-IP answer that are kept. The operator's URL is
#: their own choice and may return anything at all; the store holds a short
#: string, not a page.
EXIT_IP_MAX_CHARS = 64

#: How many addresses one *operator-initiated* sweep may have in flight. The
#: background checker keeps the serial default of 1 -- it has all day and it
#: must not sit in front of a request -- but a person who has just ticked
#: twelve candidates and pressed Add is waiting at the screen, and twelve
#: ten-second timeouts in a row is two minutes of a spinner. Four is the bound
#: because the slow half of a check is a TLS handshake through a stranger's
#: machine: four of those overlap comfortably and forty would be a small
#: outbound flood from an admin page.
PROXY_CHECK_MAX_CONCURRENCY = 4


@dataclass(frozen=True, slots=True)
class ProxyCheckOutcome:
    """One address's verdict, plus the label it is filed under."""

    label: str
    record: ProxyCheckRecord

    @property
    def refused(self) -> bool:
        return self.record.intercepted


def destination_for_provider(provider_id: str, settings: Any) -> str:
    """The https URL a check for one provider should be aimed at.

    The provider's own base URL: the operator's override if they set one, the
    catalogue's default otherwise, and a custom provider's registered URL for
    a custom provider. Never a third-party echo service -- this is the only
    destination that tests what the chain will actually do, and it is a host
    the operator already chose to talk to.
    """

    descriptor = PROVIDER_CATALOG.get(provider_id)
    if descriptor is not None:
        attr = descriptor.base_url_attr
        if attr:
            configured = str(getattr(settings, attr, "") or "").strip()
            if configured:
                return configured
        return str(descriptor.default_base_url or "").strip()
    for entry in get_provider_registry().list_custom():
        if entry.provider_id == provider_id:
            return entry.base_url.strip()
    return ""


def check_targets(
    settings: Any, store: ProxyChains, *, enabled_only: bool = False
) -> dict[str, str]:
    """Map every address in a chain to the host a check should aim at.

    An address shared between two providers is checked against the first
    provider that names it, in store order. One check is enough: the question
    is whether this tunnel keeps *any* certificate honest, and a machine that
    terminates one terminates them all.

    ``enabled_only`` narrows it to chains the operator actually armed, which is
    what the health re-prober asks for: a chain that is switched off routes no
    traffic, so re-testing its addresses would be an outbound request nobody
    asked for. A *paused* entry inside an enabled chain is still included --
    it is held out of selection, not out of the catalogue, and an operator who
    un-pauses it wants a current answer rather than a stale one.
    """

    targets: dict[str, str] = {}
    for provider_id, chain in store.chains.items():
        if enabled_only and not chain.enabled:
            continue
        destination = destination_for_provider(provider_id, settings)
        if not destination.lower().startswith("https://"):
            continue
        for entry in chain.entries:
            if entry.proxy and entry.proxy not in targets:
                targets[entry.proxy] = destination
    return targets


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _is_certificate_failure(error: BaseException) -> bool:
    """Whether a failure was the destination's certificate failing to verify.

    Read off the whole cause chain: ``httpx`` raises ``ConnectError`` with the
    ``ssl`` error as ``__cause__``, and a proxy adds another link. The message
    check is the fallback for a transport that flattened the chain -- some
    SOCKS implementations raise their own error type with the OpenSSL text
    carried only in ``str()``.
    """

    seen: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and len(seen) < 8:
        seen.append(current)
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        current = current.__cause__ or current.__context__
    text = " ".join(str(link) for link in seen).lower()
    return (
        "certificate verify failed" in text
        or "self-signed certificate" in text
        or "self signed certificate" in text
        or "certificate_verify_failed" in text
    )


def _host_and_port(url: str) -> tuple[str, int] | None:
    parsed = urlsplit(url.strip())
    host = parsed.hostname
    if not host:
        return None
    if parsed.port:
        return host, parsed.port
    # A proxy URL with no port is legal and the default depends on what it is:
    # 1080 is the SOCKS port, 443 and 80 the HTTP ones.
    if parsed.scheme in {"socks5", "socks5h"}:
        return host, 1080
    return host, 443 if parsed.scheme == "https" else 80


async def _tcp_connect(host: str, port: int, timeout: float) -> str:
    """Empty string when the address answered; a reason when it did not."""

    writer = None
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        del reader
        return ""
    except TimeoutError:
        return f"no answer from {host}:{port} within {timeout:.0f}s"
    except OSError as exc:
        return f"{host}:{port} refused the connection: {exc.strerror or exc}"
    finally:
        if writer is not None:
            writer.close()
            # Closing a socket the peer already dropped raises, and nothing
            # about this check depends on the close succeeding.
            with contextlib.suppress(OSError):
                await writer.wait_closed()


async def check_proxy(
    url: str,
    destination: str,
    *,
    timeout: float = PROXY_CHECK_TIMEOUT_SECONDS,
    exit_ip_url: str = "",
    connect_timeout: float | None = None,
) -> ProxyCheckRecord:
    """Run the three-step check against one address and return its verdict.

    ``destination`` is an https URL belonging to the provider whose chain this
    address serves. ``exit_ip_url`` is fetched only when the operator supplied
    one, and a failure there never changes the verdict: it is extra evidence,
    not a gate.

    ``connect_timeout`` bounds **step 1 only** -- the plain TCP connection to
    the proxy's own port. ``None`` means "the same as ``timeout``", which is
    what every caller did before the fetch sweep existed and is therefore what
    the Test and Add buttons still get, byte for byte. The sweep passes a
    shorter one because the commonest thing in a public list is an address that
    has stopped listening, and the difference between five seconds and ten,
    multiplied by six hundred dead addresses, is the difference between a fetch
    an operator waits for and one they abandon. It never shortens the HTTPS leg
    that follows: an address that answered has earned the full handshake.
    """

    label = mask_proxy_label(url)
    endpoint = _host_and_port(url)
    if endpoint is None:
        return ProxyCheckRecord(
            at=_now(), ok=False, tls=TLS_UNKNOWN, detail="not a usable proxy address"
        )

    dial = timeout if connect_timeout is None else max(0.1, float(connect_timeout))
    reason = await _tcp_connect(endpoint[0], endpoint[1], dial)
    if reason:
        return ProxyCheckRecord(at=_now(), ok=False, tls=TLS_UNKNOWN, detail=reason)

    started = time.monotonic()
    try:
        # Nothing is said about trust here, and that is the point: the client
        # gets the library's own default context -- the system trust store,
        # hostnames matched, certificates required -- which is byte for byte
        # the client the request path builds for this same proxy. A check made
        # with anything more permissive would measure nothing at all.
        async with httpx.AsyncClient(
            proxy=url, timeout=timeout, follow_redirects=False
        ) as client:
            response = await client.head(destination)
            del response
    except Exception as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        if _is_certificate_failure(exc):
            # The security control. The tunnel answered, and what came back was
            # not the provider's certificate.
            logger.warning(
                "PROXY CHECK: {} terminates TLS to {} -- refusing it",
                label,
                destination,
            )
            return ProxyCheckRecord(
                at=_now(),
                ok=False,
                latency_ms=elapsed,
                tls=TLS_INTERCEPTED,
                detail=(
                    "this proxy breaks certificate validation -- MCC will not "
                    "route through it"
                ),
            )
        return ProxyCheckRecord(
            at=_now(),
            ok=False,
            latency_ms=elapsed,
            tls=TLS_UNKNOWN,
            detail=_transport_reason(exc),
        )

    # Any status at all is a pass. The question was whether the tunnel carries
    # a verified HTTPS conversation to this host, and a 404 or a 405 from the
    # provider's own base URL answers it as well as a 200 does.
    latency_ms = int((time.monotonic() - started) * 1000)
    exit_ip = ""
    if exit_ip_url:
        exit_ip = await _exit_ip(url, exit_ip_url, timeout)
    return ProxyCheckRecord(
        at=_now(),
        ok=True,
        latency_ms=latency_ms,
        tls=TLS_STRICT,
        detail="",
        exit_ip=exit_ip,
    )


def _transport_reason(exc: BaseException) -> str:
    if isinstance(exc, httpx.ProxyError):
        return f"the proxy refused the tunnel: {exc}"
    if isinstance(exc, httpx.ConnectTimeout | httpx.ReadTimeout | TimeoutError):
        return "the proxy did not answer in time"
    if isinstance(exc, httpx.ConnectError):
        return f"could not reach the destination through this proxy: {exc}"
    return f"{type(exc).__name__}: {exc}"


async def _exit_ip(proxy_url: str, exit_ip_url: str, timeout: float) -> str:
    """The operator's own URL, fetched through the proxy. Never a default.

    Best effort in every direction: a URL that is down, slow or answers with a
    page says nothing about whether the tunnel verifies, so a failure here is
    recorded as an absence rather than as a verdict.
    """

    try:
        async with httpx.AsyncClient(
            proxy=proxy_url, timeout=timeout, follow_redirects=True
        ) as client:
            response = await client.get(exit_ip_url)
            response.raise_for_status()
            return response.text.strip()[:EXIT_IP_MAX_CHARS]
    except Exception as exc:
        logger.debug("PROXY CHECK: exit-IP URL did not answer: {}", exc)
        return ""


def apply_outcome(label: str, record: ProxyCheckRecord) -> None:
    """Tell the running pools what one check found.

    Three ledgers, three different meanings, and this is the only place the
    three are written together:

    * An intercepted address is **refused** -- held out of every chain in the
      process until a later check *succeeds* and shows the certificate
      verifying again.
    * A dead address walks the ordinary reachability ladder, which is the whole
      of "a dead free proxy should stop being tried": the operator does
      nothing and the chain routes around it.
    * A working address clears both, because a measurement that just succeeded
      is better evidence than a bench taken before it.

    **A refusal is lifted only by success.** This used to clear the refusal on
    any record that was not itself an interception -- including "did not
    answer". A proxy caught terminating TLS, later merely offline, therefore
    lost its verdict and could be added to a chain again. That is backwards:
    failing to connect is not evidence that a machine stopped reading the
    traffic, it is no evidence at all, and the one control standing between a
    credential and a hostile proxy must not be cleared by an absence. An
    address that cannot be reached keeps whatever verdict it had earned.
    """

    if not label:
        return
    if record.intercepted:
        PROXY_INTERCEPTION.mark(label, record.detail)
        return
    if record.ok:
        # Success is the only evidence that retires a refusal: the tunnel was
        # opened and the destination's certificate verified through it.
        PROXY_INTERCEPTION.clear_endpoint(label)
        PROXY_REACHABILITY.note_success(label)
    else:
        PROXY_REACHABILITY.note_failure(label, record.detail or "check failed")


def apply_fetch_outcome(label: str, record: ProxyCheckRecord, *, in_use: bool) -> None:
    """:func:`apply_outcome`, for an address that may belong to nobody yet.

    A fetch tests every address a public list offered -- hundreds of them, and
    most of those are strangers this install has never routed a byte through.
    Two of the three ledgers must therefore be written differently here, and
    the difference is not a nicety:

    * **Interception is written exactly as always.** It is the security
      control, it is rare, and an address caught terminating TLS must be
      refused whether or not anybody is using it. ``in_use`` does not enter
      into it.
    * **The reachability ladder is only charged for an address that is
      actually in a chain.** That ladder holds
      :data:`~my_claude_code.core.proxy_rotation.MAX_TRACKED_ENDPOINTS` rows
      and it exists to tell the request path which of *this install's own*
      addresses are worth dialling. A sweep of eight hundred candidates would
      evict every one of those rows to record benches for addresses no chain
      references -- so the sweep would break the thing it was meant to
      inform. An address nobody uses needs no ladder row.
    * **An address that IS in a chain is charged normally.** A fetch-test is
      the same three questions the Test button asks, against the same
      destination, so its answer about an address the operator is routing
      through is ordinary evidence and is recorded as such. Deciding otherwise
      would mean throwing away a measurement because of where it came from.
    """

    if not label:
        return
    if record.intercepted:
        PROXY_INTERCEPTION.mark(label, record.detail)
        return
    if record.ok:
        # Success retires a refusal wherever it is measured: the tunnel was
        # opened and the destination's certificate verified through it. That is
        # the 7.17.1 rule and it is the same rule here.
        PROXY_INTERCEPTION.clear_endpoint(label)
        if in_use:
            PROXY_REACHABILITY.note_success(label)
        return
    if in_use:
        PROXY_REACHABILITY.note_failure(label, record.detail or "check failed")


def arm_refusals_from_store(store: ProxyChains | None = None) -> int:
    """Re-arm the interception ledger from what the checker already found.

    The ledger is process-lifetime state and the store is durable, so without
    this a restart would quietly re-admit every address a previous run refused.
    Called once at startup, before the first request.
    """

    table = load_proxy_chains() if store is None else store
    armed = 0
    for endpoint in table.proxies.values():
        if endpoint.refused:
            PROXY_INTERCEPTION.mark(
                endpoint.label or mask_proxy_label(endpoint.url),
                endpoint.last_check.detail if endpoint.last_check else "",
            )
            armed += 1
    if armed:
        logger.info(
            "PROXY CHECK: {} address(es) stay refused from a previous check", armed
        )
    return armed


async def check_endpoints(
    proxy_ids: Iterable[str],
    destinations: dict[str, str],
    *,
    timeout: float = PROXY_CHECK_TIMEOUT_SECONDS,
    exit_ip_url: str = "",
    persist: bool = True,
    concurrency: int = 1,
) -> dict[str, ProxyCheckOutcome]:
    """Check several stored addresses, persist the verdicts, arm the ledgers.

    ``destinations`` maps a proxy id to the https URL to test it against. The
    store is re-read before the write rather than held across the checks: a
    sweep takes seconds and an operator editing a chain in the meantime must
    not lose the edit to a result about a different address.

    ``concurrency`` is 1 by default, which is the background checker's contract
    with the request path: one address at a time, yielding between them. A
    caller with a person waiting on the answer -- the Proxying page's bulk add
    -- raises it to at most :data:`PROXY_CHECK_MAX_CONCURRENCY`. The result is
    keyed by proxy id either way, and the verdicts are written in one save at
    the end either way, so nothing downstream can tell which was used.
    """

    table = load_proxy_chains()
    wanted = [
        proxy_id
        for proxy_id in proxy_ids
        if table.endpoint(proxy_id) is not None and destinations.get(proxy_id, "")
    ]
    outcomes: dict[str, ProxyCheckOutcome] = {}
    limit = asyncio.Semaphore(
        max(1, min(int(concurrency), PROXY_CHECK_MAX_CONCURRENCY))
    )

    async def measure(proxy_id: str) -> None:
        endpoint = table.endpoint(proxy_id)
        if endpoint is None:  # pragma: no cover - filtered above
            return
        label = endpoint.label or mask_proxy_label(endpoint.url)
        async with limit:
            record = await check_proxy(
                endpoint.url,
                destinations[proxy_id],
                timeout=timeout,
                exit_ip_url=exit_ip_url,
            )
        apply_outcome(label, record)
        outcomes[proxy_id] = ProxyCheckOutcome(label=label, record=record)
        # One yield per address. A sweep of a full catalogue is a dozen network
        # calls and this is what keeps them from sitting in front of a request.
        await asyncio.sleep(0)

    await asyncio.gather(*(measure(proxy_id) for proxy_id in wanted))
    # Back into the order asked for: a caller reports these to a person reading
    # a list, and gather finishes them in whatever order the network allows.
    outcomes = {
        proxy_id: outcomes[proxy_id] for proxy_id in wanted if proxy_id in outcomes
    }

    if persist and outcomes:
        fresh = load_proxy_chains()
        for proxy_id, outcome in outcomes.items():
            fresh = fresh.with_check(proxy_id, outcome.record)
        save_proxy_chains(fresh)
    return outcomes


__all__ = [
    "EXIT_IP_MAX_CHARS",
    "PROXY_CHECK_MAX_CONCURRENCY",
    "PROXY_CHECK_TIMEOUT_SECONDS",
    "ProxyCheckOutcome",
    "apply_fetch_outcome",
    "apply_outcome",
    "arm_refusals_from_store",
    "check_endpoints",
    "check_proxy",
    "check_targets",
    "destination_for_provider",
]
