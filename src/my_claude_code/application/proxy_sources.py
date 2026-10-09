"""Proxy sources: the scan of this computer, and what a source offers (7.89.0).

PR-S3's first source kind, ``local``: a proxy the user already runs on this
computer. One press of *Scan this computer* knocks on ``127.0.0.1`` -- never
another address -- at the ports the common tools listen on by default, asks
each one the opening question of SOCKS5 (and, if that is not what it speaks,
of an HTTP proxy), and offers every one that answered as a proxy as an
ordinary chain address. Nothing is installed, started or contacted beyond this
computer, and nothing is scanned until somebody presses the button.

The two questions send nothing anywhere but the listener itself:

* SOCKS5: the greeting ``05 02 00 02`` -- "I can do no authentication, or a
  username and password" -- and the two-byte answer says which the listener
  wants. No ``CONNECT`` follows.
* HTTP: ``CONNECT 127.0.0.1:9`` -- a tunnel to this computer's own discard
  port, so a forwarding proxy that tries it stays on the machine. Any HTTP
  status from a proxy's vocabulary (200, 403, 407, 502-504) says it is one;
  ``407`` says it wants a username and password.

What an offered address is afterwards is exactly what a feed's candidate is:
in the catalogue, in no chain, added to one through the bulk add -- which
tests it against the provider's own host first and refuses one that breaks
certificate validation.
"""

import asyncio
import contextlib
import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from loguru import logger

from my_claude_code.application.tor_control import (
    NEWNYM_GUARD,
    TOR_READINGS,
)
from my_claude_code.config.proxy_chains import (
    SOURCE_SOURCE,
    ProxyChains,
    ProxyEndpoint,
)
from my_claude_code.config.proxy_sources import (
    AUTH_NONE,
    AUTH_UNSUPPORTED,
    AUTH_USERPASS,
    BUILT_KINDS,
    KIND_LOCAL,
    LOCAL_HOST,
    LOCAL_SOURCE_ID,
    LOCAL_SOURCE_NAME,
    PROTOCOL_HTTP,
    PROTOCOL_SOCKS5,
    SOURCE_KINDS,
    TOR_SOURCE_PREFIX,
    LocalListener,
    ProxySource,
    ProxySources,
    SourceSecret,
    TorSettings,
    mint_secret_id,
)

#: How long one port gets to answer each question. Loopback answers in well
#: under a millisecond; this bounds a listener that accepts and never speaks.
LOCAL_SCAN_TIMEOUT_SECONDS = 2.0

#: What any proxy, VPN, WARP or Tor exit still sees (spec §5.9).
EXIT_SEES = (
    "Any exit -- a proxy, a VPN, WARP or Tor -- sees which host you contact "
    "(the name travels in plain text in the TLS handshake), when, and how "
    "much; never your keys, prompts or replies, because TLS is verified end "
    "to end."
)
#: The one-line terms reminder (the user's decision 11 of 2026-09-25).
TERMS_LINE = (
    "Spreading requests over many exits can conflict with a provider's terms "
    "-- a free quota counted per address, for example. That is your choice."
)
#: The Tor Project's own ask, on a Tor port (decision 16 of 2026-10-06).
TOR_LINE = (
    "The Tor Project asks people not to push heavy or automated traffic "
    "through its volunteer exits."
)
#: What Tor's relays see, on every Tor port: a scanned one or a tor source's.
TOR_SEES = (
    "Tor's first relay sees your address and its exit sees the "
    "destination's name; no single relay sees both."
)


@dataclass(frozen=True, slots=True)
class ScanPort:
    """A port a common tool listens on, and what is usually behind it."""

    port: int
    usually: str
    sees: str = ""
    tor: bool = False


#: The ports the scan knocks on (spec §7 PR-S3 plus 9052 by the user's
#: decision 12 of 2026-10-06), lowest first. The tool is a guess shown beside
#: the port, never stored as the address's name.
LOCAL_SCAN_PORTS: tuple[ScanPort, ...] = (
    ScanPort(
        1080,
        "ssh -D, or another SOCKS5 server you run",
        "With ssh -D your VPS provider sees the destinations; your ISP sees only SSH.",
    ),
    ScanPort(
        8888,
        "gluetun's HTTP proxy (a VPN in Docker)",
        "The VPN provider also sees your real address, so it knows you "
        "talked to this provider.",
    ),
    ScanPort(9050, "Tor", TOR_SEES, tor=True),
    ScanPort(9052, "Tor (the port opencode_lite uses)", TOR_SEES, tor=True),
    ScanPort(9150, "Tor Browser", TOR_SEES, tor=True),
    ScanPort(
        25344,
        "wireproxy (a WireGuard tunnel)",
        "The VPN provider also sees your real address, so it knows you "
        "talked to this provider.",
    ),
    ScanPort(
        40000,
        "Cloudflare WARP in proxy mode",
        "Cloudflare sees your address and the destination, and a "
        "Cloudflare-fronted provider can tell a request came through WARP. "
        "Cloudflare documents a 10-second limit per request for WARP's "
        "proxy mode in its Zero Trust client.",
    ),
)

#: SOCKS5: version 5, two methods offered -- none, and username/password.
_SOCKS5_GREETING = b"\x05\x02\x00\x02"
_SOCKS5_METHODS = {0x00: AUTH_NONE, 0x02: AUTH_USERPASS, 0xFF: AUTH_UNSUPPORTED}
#: HTTP: a tunnel to this computer's own discard port.
_HTTP_PROBE = b"CONNECT 127.0.0.1:9 HTTP/1.1\r\nHost: 127.0.0.1:9\r\n\r\n"
#: Statuses only a forwarding proxy answers ``CONNECT`` with.
_HTTP_PROXY_STATUSES = frozenset({200, 403, 407, 502, 503, 504})


@dataclass(frozen=True, slots=True)
class ScanAnswer:
    """What one port said. ``protocol`` empty: nothing a chain can use."""

    port: int
    answering: bool
    protocol: str = ""
    auth: str = AUTH_NONE
    note: str = ""


async def _ask(port: int, payload: bytes, size: int, timeout: float) -> bytes | None:
    """Open, send ``payload``, read up to ``size`` bytes. ``None``: nothing listens."""

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(LOCAL_HOST, port), timeout
        )
    except OSError, TimeoutError:
        return None
    try:
        writer.write(payload)
        await asyncio.wait_for(writer.drain(), timeout)
        try:
            return await asyncio.wait_for(reader.read(size), timeout)
        except TimeoutError:
            return b""
    except OSError:
        return b""
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), timeout)


async def probe_local_port(
    port: int, *, timeout: float = LOCAL_SCAN_TIMEOUT_SECONDS
) -> ScanAnswer:
    """Ask one port on ``127.0.0.1`` whether it is a SOCKS5 or HTTP proxy."""

    reply = await _ask(port, _SOCKS5_GREETING, 2, timeout)
    if reply is None:
        return ScanAnswer(port=port, answering=False)
    if len(reply) == 2 and reply[0] == 0x05:
        auth = _SOCKS5_METHODS.get(reply[1], AUTH_UNSUPPORTED)
        note = (
            "answers SOCKS5 but accepts neither no login nor a username and password"
            if auth == AUTH_UNSUPPORTED
            else ""
        )
        return ScanAnswer(port, True, PROTOCOL_SOCKS5, auth, note)
    head = await _ask(port, _HTTP_PROBE, 64, timeout)
    if head is None:
        return ScanAnswer(port=port, answering=False)
    status = _http_status(head)
    if status in _HTTP_PROXY_STATUSES:
        auth = AUTH_USERPASS if status == 407 else AUTH_NONE
        return ScanAnswer(port, True, PROTOCOL_HTTP, auth)
    if status is not None:
        return ScanAnswer(
            port, True, note=f"answers HTTP ({status}) but not as a proxy"
        )
    return ScanAnswer(port, True, note="answered, but not as a SOCKS5 or HTTP proxy")


def _http_status(head: bytes) -> int | None:
    line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = line.split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/1."):
        return None
    try:
        return int(parts[1])
    except ValueError:
        return None


async def scan_local(
    ports: Iterable[int] | None = None, *, timeout: float = LOCAL_SCAN_TIMEOUT_SECONDS
) -> list[ScanAnswer]:
    """Ask every scan port at once. Only ``127.0.0.1``, only when called."""

    wanted = sorted(
        set(ports if ports is not None else (item.port for item in LOCAL_SCAN_PORTS))
    )
    answers = await asyncio.gather(
        *(probe_local_port(port, timeout=timeout) for port in wanted)
    )
    return list(answers)


def scan_port_info(port: int) -> ScanPort | None:
    return next((item for item in LOCAL_SCAN_PORTS if item.port == port), None)


def local_offer_id(port: int, protocol: str) -> str:
    """The catalogue id a local listener's address is always filed under.

    Derived, not minted, so a rescan finds the same row -- with its checks,
    its health and the chains using it -- rather than offering a stranger.
    """

    digest = hashlib.sha256(f"{LOCAL_SOURCE_ID}:{protocol}:{port}".encode()).hexdigest()
    return f"px_{digest[:10]}"


def local_label(port: int, protocol: str) -> str:
    """What a local listener's address is called everywhere: a fact, not a guess."""

    kind = "SOCKS5" if protocol == PROTOCOL_SOCKS5 else "HTTP"
    return f"Local {kind} · {LOCAL_HOST}:{port}"


def _family(scheme: str) -> str:
    return "socks5" if scheme in {"socks5", "socks5h"} else "http"


def _existing_local(chains: ProxyChains, port: int, protocol: str) -> str:
    """An address the catalogue already holds for this listener, typed by hand.

    The same loopback port under the same family of scheme -- one the
    operator added before the scan existed, or (7.90.0) one a Tor source
    already offers for that port. Reused rather than duplicated, so one
    listener never has two rows and two sets of books.
    """

    wanted = _family("socks5" if protocol == PROTOCOL_SOCKS5 else "http")
    own = local_offer_id(port, protocol)
    for proxy_id, endpoint in chains.proxies.items():
        if proxy_id == own:
            continue
        parsed = urlsplit(endpoint.url)
        try:
            same_port = parsed.port == port
        except ValueError:
            continue
        if (
            same_port
            and parsed.hostname in {LOCAL_HOST, "localhost"}
            and _family(parsed.scheme) == wanted
            and (
                endpoint.source != SOURCE_SOURCE
                or endpoint.source_id.startswith(TOR_SOURCE_PREFIX)
            )
        ):
            return proxy_id
    return ""


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _offer(
    chains: ProxyChains,
    listener: LocalListener,
    secret: SourceSecret | None,
    at: str,
) -> tuple[str, ProxyEndpoint]:
    """The catalogue row a usable listener is offered as."""

    reused = _existing_local(chains, listener.port, listener.protocol)
    if reused:
        endpoint = chains.proxies[reused]
        return reused, endpoint
    proxy_id = local_offer_id(listener.port, listener.protocol)
    url = listener.url(secret)
    known = chains.endpoint(proxy_id)
    if known is not None:
        return proxy_id, replace(known, url=url)
    return proxy_id, ProxyEndpoint(
        url=url,
        label=local_label(listener.port, listener.protocol),
        added_at=at,
        source=SOURCE_SOURCE,
        source_id=LOCAL_SOURCE_ID,
    )


def _local_offers(
    chains: ProxyChains,
    sources: ProxySources,
    listeners: Sequence[LocalListener],
    at: str,
) -> tuple[tuple[LocalListener, ...], list[tuple[str, ProxyEndpoint]]]:
    kept: list[LocalListener] = []
    offers: list[tuple[str, ProxyEndpoint]] = []
    for listener in listeners:
        if listener.usable:
            proxy_id, endpoint = _offer(
                chains, listener, sources.secret(listener.secret), at
            )
            offers.append((proxy_id, endpoint))
            listener = replace(listener, proxy=proxy_id)
        else:
            listener = replace(listener, proxy="")
        kept.append(listener)
    return tuple(kept), offers


def apply_local_scan(
    chains: ProxyChains,
    sources: ProxySources,
    answers: Sequence[ScanAnswer],
    *,
    at: str | None = None,
) -> tuple[ProxyChains, ProxySources]:
    """Record what a scan found and offer every listener a chain can use.

    A port that stopped answering leaves the offers -- "none without a
    listener" -- but a credential somebody set for it is kept on its row, so
    starting the tool again and scanning brings it back as it was.
    """

    stamp = at or _now()
    previous = sources.source(LOCAL_SOURCE_ID)
    listeners: list[LocalListener] = []
    seen: set[int] = set()
    for answer in sorted(answers, key=lambda item: item.port):
        if not answer.answering:
            continue
        seen.add(answer.port)
        before = previous.listener(answer.port) if previous is not None else None
        keeps_secret = (
            before is not None
            and bool(before.secret)
            and answer.auth == AUTH_USERPASS
            and before.protocol == answer.protocol
        )
        listeners.append(
            LocalListener(
                port=answer.port,
                protocol=answer.protocol,
                auth=answer.auth,
                answering=True,
                secret=before.secret if keeps_secret and before is not None else "",
                note=answer.note,
            )
        )
    if previous is not None:
        listeners.extend(
            replace(listener, answering=False, proxy="")
            for listener in previous.listeners
            if listener.port not in seen and listener.secret
        )
    listeners.sort(key=lambda item: item.port)
    kept, offers = _local_offers(chains, sources, listeners, stamp)
    source = ProxySource(
        id=LOCAL_SOURCE_ID,
        kind=KIND_LOCAL,
        name=previous.name if previous is not None else LOCAL_SOURCE_NAME,
        enabled=True,
        added_at=previous.added_at if previous is not None else stamp,
        scanned_at=stamp,
        listeners=kept,
    )
    return (
        chains.with_source_offers(LOCAL_SOURCE_ID, offers),
        sources.with_source(source),
    )


class SourceEditError(ValueError):
    """A source edit the store cannot make, with the sentence the page shows."""


def set_local_credential(
    chains: ProxyChains,
    sources: ProxySources,
    port: int,
    *,
    username: str = "",
    password: str = "",
    clear: bool = False,
) -> tuple[ProxyChains, ProxySources]:
    """Give a local listener that asked for a login its username and password.

    Stored as a secret in ``proxy_sources.json`` and carried in the offered
    address's URL in ``proxy_chains.json`` (decision 6, option A) -- both
    owner-only files. The address keeps its id and its name, so every chain
    using it keeps it; only its URL changes. ``clear`` forgets the login.
    """

    source = sources.source(LOCAL_SOURCE_ID)
    listener = source.listener(port) if source is not None else None
    if source is None or listener is None:
        raise SourceEditError(f"No listener on port {port} was found by the scan.")
    if listener.auth != AUTH_USERPASS:
        raise SourceEditError(
            f"Port {port} did not ask for a username and password, so there is "
            "nothing to set."
        )
    secrets_now = sources
    if clear:
        updated = replace(listener, secret="")
    else:
        if not username:
            raise SourceEditError("Give the username the listener expects.")
        secret_id = listener.secret or mint_secret_id(sources.secrets)
        secrets_now = sources.with_secret(
            secret_id, SourceSecret(username=username, password=password)
        )
        updated = replace(listener, secret=secret_id)
    listeners = tuple(
        updated if item.port == port else item for item in source.listeners
    )
    kept, offers = _local_offers(
        chains, secrets_now, [item for item in listeners if item.answering], _now()
    )
    by_port = {item.port: item for item in kept}
    merged = tuple(by_port.get(item.port, item) for item in listeners)
    return (
        chains.with_source_offers(LOCAL_SOURCE_ID, offers),
        secrets_now.with_source(replace(source, listeners=merged)),
    )


def remove_source(
    chains: ProxyChains, sources: ProxySources, source_id: str
) -> tuple[ProxyChains, ProxySources]:
    """Forget a source and withdraw its offers. A chain using one keeps it."""

    return chains.with_source_offers(source_id, []), sources.without_source(source_id)


# ------------------------------------------------------------------- payload


def _chained_in(chains: ProxyChains, proxy_id: str) -> list[str]:
    return [
        provider_id
        for provider_id, chain in chains.chains.items()
        if proxy_id and proxy_id in chain.proxy_ids()
    ]


def _listener_payload(
    listener: LocalListener,
    chains: ProxyChains,
    sources: ProxySources,
    offered: set[str],
) -> dict[str, Any]:
    info = scan_port_info(listener.port)
    endpoint = chains.endpoint(listener.proxy) if listener.proxy else None
    secret = sources.secret(listener.secret)
    check = None if endpoint is None else endpoint.last_check
    payload: dict[str, Any] = {
        "port": listener.port,
        "protocol": listener.protocol,
        "auth": listener.auth,
        "answering": listener.answering,
        "note": listener.note,
        "usually": info.usually if info is not None else "",
        "sees": info.sees if info is not None else "",
        "tor": bool(info is not None and info.tor),
        "proxy": listener.proxy if endpoint is not None else "",
        "label": chains.ledger_label(listener.proxy) if endpoint is not None else "",
        "scheme": listener.scheme if listener.protocol else "",
        "offered": listener.proxy in offered,
        "chained": _chained_in(chains, listener.proxy),
        # Whether a login is stored, and the username's masked label -- never
        # the username, never the password.
        "secret_set": secret is not None,
        "secret_label": secret.label if secret is not None else "",
        "last_check": None if check is None else check.as_document(),
    }
    return payload


def _tor_port_payload(
    port: int, proxy_id: str, chains: ProxyChains, offered: set[str]
) -> dict[str, Any]:
    endpoint = chains.endpoint(proxy_id) if proxy_id else None
    check = None if endpoint is None else endpoint.last_check
    return {
        "port": port,
        "proxy": proxy_id if endpoint is not None else "",
        "label": chains.ledger_label(proxy_id) if endpoint is not None else "",
        "offered": proxy_id in offered,
        "chained": _chained_in(chains, proxy_id),
        "last_check": None if check is None else check.as_document(),
    }


def _tor_payload(
    source: ProxySource, tor: TorSettings, chains: ProxyChains, sources: ProxySources
) -> dict[str, Any]:
    """A Tor source's card: its ports as offers, its torrc lines, its status.

    Masked like everything else: whether a control password is stored, never
    the password; the cookie is never anywhere to send. ``status`` and
    ``newnym`` appear only once a button has been pressed in this process.
    """

    offered = set(chains.source_offers.get(source.id, ()))
    payload: dict[str, Any] = {
        "id": source.id,
        "kind": source.kind,
        "built": True,
        "name": source.name,
        "enabled": source.enabled,
        "added_at": source.added_at,
        "control_port": tor.control_port,
        "auth": tor.auth,
        "secret_set": sources.secret(tor.secret) is not None,
        "ports": [
            _tor_port_payload(item.port, item.proxy, chains, offered)
            for item in tor.socks_ports
        ],
        "torrc": tor.torrc_lines(),
        "sees": TOR_SEES,
    }
    reading = TOR_READINGS.get(source.id)
    if reading is not None:
        payload["status"] = reading.as_payload()
    last = NEWNYM_GUARD.last_at(tor.control_port)
    if last:
        payload["newnym"] = {
            "at": last,
            "wait_seconds": NEWNYM_GUARD.wait_seconds(tor.control_port),
        }
    return payload


def _source_payload(
    source: ProxySource, chains: ProxyChains, sources: ProxySources
) -> dict[str, Any]:
    if source.tor is not None:
        return _tor_payload(source, source.tor, chains, sources)
    offered = set(chains.source_offers.get(source.id, ()))
    base: dict[str, Any] = {
        "id": source.id,
        "kind": source.kind,
        "built": source.built,
        "name": source.name,
        "enabled": source.enabled,
        "added_at": source.added_at,
        "scanned_at": source.scanned_at,
    }
    if not source.built:
        # A kind a later release builds: named and counted, never echoed --
        # its raw fields are that release's to read.
        base["secret_set"] = any(sources.secret(key) for key in source.secret_ids())
        base["offers"] = len(offered)
        return base
    base["listeners"] = [
        _listener_payload(listener, chains, sources, offered)
        for listener in source.listeners
    ]
    return base


def sources_document(chains: ProxyChains, sources: ProxySources) -> dict[str, Any]:
    """What the Sources section shows: masked through and through."""

    return {
        "sources": [
            _source_payload(source, chains, sources)
            for source in sources.sources.values()
        ],
        "scan_ports": [
            {"port": item.port, "usually": item.usually, "tor": item.tor}
            for item in LOCAL_SCAN_PORTS
        ],
        "kinds": [
            {"id": kind, "available": kind in BUILT_KINDS} for kind in SOURCE_KINDS
        ],
        "sees": EXIT_SEES,
        "terms": TERMS_LINE,
        "tor_line": TOR_LINE,
        "unreadable": sources.unreadable,
    }


def scan_summary(answers: Sequence[ScanAnswer]) -> dict[str, Any]:
    """The scan's own answer, for the page's announcement."""

    found = [answer for answer in answers if answer.answering]
    usable = [
        answer
        for answer in found
        if answer.protocol and answer.auth != AUTH_UNSUPPORTED
    ]
    return {
        "ports": [answer.port for answer in answers],
        "answering": [answer.port for answer in found],
        "offered": [answer.port for answer in usable],
    }


def log_scan(answers: Sequence[ScanAnswer]) -> None:
    summary = scan_summary(answers)
    logger.info(
        "PROXY SOURCES: scanned 127.0.0.1 ports {}: {} answered, {} offered as "
        "proxies ({})",
        ", ".join(str(port) for port in summary["ports"]),
        len(summary["answering"]),
        len(summary["offered"]),
        ", ".join(str(port) for port in summary["offered"]) or "none",
    )


def offered_by(chains: ProxyChains) -> Mapping[str, str]:
    """Offered catalogue id -> the source that offers it."""

    return {
        proxy_id: source_id
        for source_id, ids in chains.source_offers.items()
        for proxy_id in ids
    }


__all__ = [
    "EXIT_SEES",
    "LOCAL_SCAN_PORTS",
    "LOCAL_SCAN_TIMEOUT_SECONDS",
    "TERMS_LINE",
    "TOR_LINE",
    "ScanAnswer",
    "ScanPort",
    "SourceEditError",
    "apply_local_scan",
    "local_label",
    "local_offer_id",
    "log_scan",
    "offered_by",
    "probe_local_port",
    "remove_source",
    "scan_local",
    "scan_port_info",
    "scan_summary",
    "set_local_credential",
    "sources_document",
]
