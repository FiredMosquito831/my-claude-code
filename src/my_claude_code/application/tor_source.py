"""Bring-your-own Tor: a ``tor`` source's ports, offers and buttons (7.90.0).

The user runs tor; MCC never downloads, installs or starts it (decision 4 of
2026-10-06). They tell the Proxying page which SOCKS ports and which control
port their tor has -- any free ports: on some Windows machines 9050, 9052 and
9150 sit inside a reserved range nothing can listen on -- and the page shows
the matching torrc lines to paste.

* **Each SOCKS port is one chain address** (decision 5(a)): an ordinary
  ``socks5h://127.0.0.1:<port>`` entry in the catalogue, offered like a scanned
  listener, named ``Tor · 127.0.0.1:<port>`` -- distinct ports, distinct names,
  so the health ledgers, the exit memory and the request log keep one book per
  identity. Several Tor ports in a chain with *Keep trying exits until one
  answers* move a refused request to the next port at once (7.81.0); nothing
  here adds to or changes that rotation.
* **New Tor identity** (``SIGNAL NEWNYM``) is a button and only a button: no
  automatic trigger after a refusal (decision 5(a), not (b) or (c)).
* **Check Tor** reads tor's own facts from its control port.

Both buttons speak only to ``127.0.0.1:<control port>``
(:mod:`~my_claude_code.application.tor_control`), log in with the cookie file
tor names -- read at that moment, never copied (decision 7) -- or the control
password stored owner-only in ``proxy_sources.json`` and never sent back.
"""

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from loguru import logger

from my_claude_code.application.proxy_sources import SourceEditError
from my_claude_code.application.tor_control import (
    TOR_READINGS,
    NewnymOutcome,
    TorReading,
    read_tor_status,
    send_newnym,
)
from my_claude_code.config.proxy_chains import (
    SOURCE_SOURCE,
    ProxyChains,
    ProxyEndpoint,
)
from my_claude_code.config.proxy_sources import (
    KIND_TOR,
    LOCAL_HOST,
    SECRET_PASSWORD,
    TOR_AUTH_COOKIE,
    TOR_AUTH_KINDS,
    TOR_AUTH_PASSWORD,
    TOR_SOURCE_NAME,
    TOR_SOURCE_PREFIX,
    ProxySource,
    ProxySources,
    SourceSecret,
    TorPort,
    TorSettings,
    mint_secret_id,
)


@dataclass(frozen=True, slots=True)
class TorEdit:
    """What the Tor form sends: the ports the user's tor has, and its login.

    ``password`` is write-only: empty keeps a stored password, and nothing
    ever sends one back.
    """

    socks_ports: tuple[int, ...]
    control_port: int
    auth: str = TOR_AUTH_COOKIE
    password: str = ""


def tor_source_ids(sources: ProxySources) -> tuple[str, ...]:
    return tuple(
        source_id
        for source_id, source in sources.sources.items()
        if source.kind == KIND_TOR
    )


def next_tor_source_id(sources: ProxySources) -> str:
    """``src_tor`` for the first, then ``src_tor_2``, ``src_tor_3`` ..."""

    if TOR_SOURCE_PREFIX not in sources.sources:
        return TOR_SOURCE_PREFIX
    number = 2
    while f"{TOR_SOURCE_PREFIX}_{number}" in sources.sources:
        number += 1
    return f"{TOR_SOURCE_PREFIX}_{number}"


def tor_offer_id(source_id: str, port: int) -> str:
    """The catalogue id a Tor port is filed under: derived, so an edit finds it."""

    digest = hashlib.sha256(f"{source_id}:socks5:{port}".encode()).hexdigest()
    return f"px_{digest[:10]}"


def tor_label(port: int) -> str:
    """A Tor port's name everywhere: one per port, so one per identity."""

    return f"Tor · {LOCAL_HOST}:{port}"


def tor_url(port: int) -> str:
    """``socks5h``: the destination's name is resolved by tor's exit, never here."""

    return f"socks5h://{LOCAL_HOST}:{port}"


def _port(value: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 65536:
        raise SourceEditError(f"{what} {value!r} is not a TCP port (1 to 65535).")
    return value


def _checked(
    edit: TorEdit, sources: ProxySources, source_id: str
) -> tuple[tuple[int, ...], int]:
    ports: list[int] = []
    for value in edit.socks_ports:
        port = _port(value, "SOCKS port")
        if port not in ports:
            ports.append(port)
    if not ports:
        raise SourceEditError(
            "Give at least one SOCKS port: each one is one Tor identity, and two "
            "or more let a chain move a refused request to another."
        )
    control = _port(edit.control_port, "Control port")
    if control in ports:
        raise SourceEditError(
            f"Port {control} cannot be both a SOCKS port and the control port."
        )
    if edit.auth not in TOR_AUTH_KINDS:
        raise SourceEditError(
            "Log in with the cookie file tor names, or with a control password."
        )
    for other_id, other in sources.sources.items():
        if other_id == source_id or other.tor is None:
            continue
        taken = {*other.tor.ports, other.tor.control_port}
        clash = [port for port in (*ports, control) if port in taken]
        if clash:
            raise SourceEditError(
                f"Port {clash[0]} already belongs to another Tor source "
                f"(control port {other.tor.control_port})."
            )
    return tuple(ports), control


def _existing_loopback_socks(chains: ProxyChains, port: int, own: str) -> str:
    """An address already in the catalogue for this Tor port: reused, not doubled.

    Typed by hand, or offered by the scan of this computer: the same
    ``127.0.0.1`` port, a SOCKS scheme, no login (a login on a Tor port is a
    different identity, ``IsolateSOCKSAuth``).
    """

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
            and parsed.scheme in {"socks5", "socks5h"}
            and not parsed.username
            and not parsed.password
        ):
            return proxy_id
    return ""


def _offers(
    chains: ProxyChains, source_id: str, ports: Sequence[int], at: str
) -> tuple[tuple[TorPort, ...], list[tuple[str, ProxyEndpoint]]]:
    kept: list[TorPort] = []
    offers: list[tuple[str, ProxyEndpoint]] = []
    for port in ports:
        own = tor_offer_id(source_id, port)
        known = chains.endpoint(own)
        if known is not None:
            offers.append((own, known))
            kept.append(TorPort(port=port, proxy=own))
            continue
        reused = _existing_loopback_socks(chains, port, own)
        if reused:
            offers.append((reused, chains.proxies[reused]))
            kept.append(TorPort(port=port, proxy=reused))
            continue
        offers.append(
            (
                own,
                ProxyEndpoint(
                    url=tor_url(port),
                    label=tor_label(port),
                    added_at=at,
                    source=SOURCE_SOURCE,
                    source_id=source_id,
                ),
            )
        )
        kept.append(TorPort(port=port, proxy=own))
    return tuple(kept), offers


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def save_tor_source(
    chains: ProxyChains,
    sources: ProxySources,
    source_id: str,
    edit: TorEdit,
    *,
    at: str | None = None,
) -> tuple[ProxyChains, ProxySources, str]:
    """Create (``source_id`` empty) or change a Tor source, and offer its ports.

    Returns both stores and the source's id. A port the edit drops leaves the
    offers; a chain that took it keeps it, as with every source. Nothing is
    contacted: the user's tor is asked only when a button is pressed.
    """

    stamp = at or _now()
    existing = sources.source(source_id) if source_id else None
    if source_id and (existing is None or existing.tor is None):
        raise SourceEditError(f"No Tor source {source_id!r}.")
    target = source_id or next_tor_source_id(sources)
    ports, control = _checked(edit, sources, target)
    previous = existing.tor if existing is not None else None
    stored = sources
    secret_id = ""
    if edit.auth == TOR_AUTH_PASSWORD:
        kept = previous.secret if previous is not None else ""
        if edit.password:
            secret_id = kept or mint_secret_id(sources.secrets)
            stored = sources.with_secret(
                secret_id, SourceSecret(type=SECRET_PASSWORD, password=edit.password)
            )
        elif kept and sources.secret(kept) is not None:
            secret_id = kept
        else:
            raise SourceEditError(
                "Type the control password your tor was given "
                "(HashedControlPassword in its torrc)."
            )
    tor_ports, offers = _offers(chains, target, ports, stamp)
    source = ProxySource(
        id=target,
        kind=KIND_TOR,
        name=existing.name if existing is not None else TOR_SOURCE_NAME,
        enabled=True,
        added_at=existing.added_at if existing is not None else stamp,
        tor=TorSettings(
            socks_ports=tor_ports,
            control_port=control,
            auth=edit.auth,
            secret=secret_id,
        ),
    )
    # ``with_source`` drops a password the source no longer names (cookie now).
    return chains.with_source_offers(target, offers), stored.with_source(source), target


def _password(tor: TorSettings, sources: ProxySources) -> str:
    if tor.auth != TOR_AUTH_PASSWORD:
        return ""
    secret = sources.secret(tor.secret)
    return secret.password if secret is not None else ""


def _tor_of(source: ProxySource) -> TorSettings:
    if source.tor is None:
        raise SourceEditError(f"{source.name or source.id} is not a Tor source.")
    return source.tor


async def check_tor(source: ProxySource, sources: ProxySources) -> TorReading:
    """*Check Tor*: log in to its control port and read the card's facts."""

    tor = _tor_of(source)
    reading = await read_tor_status(
        tor.control_port, auth=tor.auth, password=_password(tor, sources)
    )
    TOR_READINGS.record(source.id, reading)
    logger.info(
        "PROXY SOURCES: checked Tor on {}:{}: {}",
        LOCAL_HOST,
        tor.control_port,
        "answers" if reading.ok else "not usable",
    )
    return reading


async def new_tor_identity(source: ProxySource, sources: ProxySources) -> NewnymOutcome:
    """*New Tor identity*: ``SIGNAL NEWNYM``, at most once per 10 s per tor."""

    tor = _tor_of(source)
    outcome = await send_newnym(
        tor.control_port, auth=tor.auth, password=_password(tor, sources)
    )
    if outcome.reading is not None:
        TOR_READINGS.record(source.id, outcome.reading)
    elif not outcome.refused_locally:
        TOR_READINGS.record(
            source.id,
            TorReading(at=_now(), ok=False, sentence=outcome.sentence),
        )
    logger.info(
        "PROXY SOURCES: new Tor identity on {}:{}: {}",
        LOCAL_HOST,
        tor.control_port,
        "accepted"
        if outcome.accepted
        else (
            f"not sent, {outcome.wait_seconds} s left of tor's 10 s"
            if outcome.refused_locally
            else "failed"
        ),
    )
    return outcome


def forget_readings(source_ids: Iterable[str]) -> None:
    """A changed or removed Tor source's last reading is no longer about it."""

    for source_id in source_ids:
        TOR_READINGS.forget(source_id)


__all__ = [
    "TorEdit",
    "check_tor",
    "forget_readings",
    "new_tor_identity",
    "next_tor_source_id",
    "save_tor_source",
    "tor_label",
    "tor_offer_id",
    "tor_source_ids",
    "tor_url",
]
