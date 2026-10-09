"""Proxy sources: ``~/.mcc/proxy_sources.json`` (7.89.0, PR-S3).

A **source** is something that *yields* proxy addresses -- ordinary
``socks5h://`` / ``http://`` URLs that go into the catalogue of
``proxy_chains.json`` and from there into chains, exactly like a typed or a
feed address. Nothing downstream knows a source exists: the chain, the checker,
the health ledgers and the request log see addresses.

This release builds one kind, ``local``: the listeners a scan of this computer
found (``application/proxy_sources.py``) -- Cloudflare WARP in proxy mode,
``ssh -D``, Tor, gluetun, wireproxy. The kinds the spec names next (``account``,
``gateway``, ``list``, ``runner``) are part of the vocabulary and of the
document's shape, so a later release adds a reader and a writer without
migrating anything; a source of a kind this release does not build is kept
verbatim and shown as such.

::

    {
      "version": 1,
      "sources": {
        "src_local": {"kind": "local", "name": "This computer", "enabled": true,
                      "added_at": "...Z", "scanned_at": "...Z",
                      "listeners": [
                        {"port": 9050, "protocol": "socks5", "auth": "none",
                         "answering": true, "proxy": "px_..."},
                        {"port": 1080, "protocol": "socks5", "auth": "userpass",
                         "answering": true, "proxy": "px_...", "secret": "sec_..."}]}
      },
      "secrets": {"sec_...": {"type": "userpass", "username": "...",
                              "password": "..."}}
    }

**Secrets live here and in the chain store's URL only** (the user's decision 6
of 2026-09-25: "option A"). Both files are written owner-only
(:mod:`~my_claude_code.config.owner_only`). Nothing here is ever rendered back:
:meth:`ProxySources.public_document` names a secret by
:func:`~my_claude_code.config.credentials.mask_key_label` of its username and
whether one is set, and the routes send only that.

7.90.0 adds a second built kind, ``tor``: a tor the user runs themselves
(MCC never downloads or starts one). Its SOCKS ports -- one identity each --
are offered as chain addresses, and its control port carries the *New Tor
identity* button::

    "src_tor": {"kind": "tor", "name": "Tor", "enabled": true,
                "added_at": "...Z", "control_port": 19260, "auth": "cookie",
                "socks_ports": [{"port": 19250, "proxy": "px_..."},
                                {"port": 19251, "proxy": "px_..."}]}

``auth`` is ``cookie`` -- the file tor names in its ``PROTOCOLINFO`` answer,
read only when a button is pressed and **never copied** into any MCC file --
or ``password``, whose secret (``{"type": "password", "password": "..."}``)
lives under ``secrets`` like a listener's login and is never sent back.

7.91.0 (PR-S4 + PR-S5, vendor presets) builds the spec's next three kinds.
Each yields ordinary addresses, offered like a scanned listener's, and keeps
its one credential under ``secrets``, named by a top-level ``secret`` -- the
key 7.89.0 and 7.90.0 honour for a kind they keep verbatim, so going back a
release keeps the source and its credential::

    "src_nordvpn": {"kind": "account", "name": "NordVPN", "enabled": true,
                    "added_at": "...Z", "preset": "nordvpn",
                    "scheme": "socks5h", "port": 1080,
                    "hosts": ["nl.socks.nordhold.net"], "secret": "sec_...",
                    "host_list": {"parser": "nordvpn_servers", "url": "https://...",
                                  "countries": ["NL"], "fetched_at": "...Z",
                                  "fetch_ok": true, "countries_found": ["NL", "SE", "US"],
                                  "hosts": [{"host": "...", "country": "NL"}]}}
    "src_oxylabs": {"kind": "gateway", "name": "Oxylabs", "preset": "oxylabs",
                    "host": "pr.oxylabs.io", "port": 7777, "scheme": "socks5h",
                    "country": "US", "minutes": 30,
                    "sessions": ["3f9a0c1e", "..."], "secret": "sec_..."}
    "src_webshare": {"kind": "list", "name": "Webshare", "preset": "webshare",
                     "parser": "webshare_list", "scheme": "socks5h",
                     "url_host": "https://proxy.webshare.io", "secret": "sec_...",
                     "rows": [{"host": "...", "port": 8080, "proxy": "px_...",
                               "login": true}]}

* ``account``: a VPN's proxy hosts x the account's service credential (a
  ``userpass`` secret; none for an in-tunnel proxy such as Mullvad's). Hosts
  are typed, or read from the vendor's server list at the URL the operator
  confirmed (``host_list``, kept here and never sent back).
* ``gateway``: a commercial gateway's session template x N. The customer or
  user name and the password are the ``userpass`` secret; each session id is
  random, minted once, never reused.
* ``list``: a list of ``ip:port:username:password`` rows. Each row's login
  lives only in its address's URL in ``proxy_chains.json`` (decision 6,
  option A). A download link carries the vendor's token, so it is a
  ``password`` secret and only its ``scheme://host`` is shown.

``host_list`` and a ``list`` source can be fetched on a schedule the operator
switches on (``refresh_hours``, 0 -- off -- unless they choose one) and
through the chain of a provider they name (``fetch_via``).
"""

import hashlib
import json
import re
import secrets
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote

from loguru import logger

from my_claude_code.config.credentials import mask_key_label
from my_claude_code.config.owner_only import write_owner_only_json
from my_claude_code.config.paths import config_dir_path
from my_claude_code.config.proxy_feeds import FEED_MAX_ENDPOINTS
from my_claude_code.config.proxy_presets import (
    GATEWAY_TEMPLATES,
    READER_NORDVPN_SERVERS,
    READER_PROXY_LIST,
    ZONE_TYPES,
    preset,
)
from my_claude_code.config.proxy_source_readers import (
    ListedHost,
    TypedHost,
    normalise_host,
    parse_typed_host,
)

PROXY_SOURCES_FILENAME = "proxy_sources.json"
DOCUMENT_VERSION = 1

KIND_LOCAL = "local"
#: A tor the user runs themselves (7.90.0). Not one of the base spec's five
#: kinds, so it is not in :data:`SOURCE_KINDS` -- whose list the Sources
#: payload names, byte for byte as 7.89.0 did -- but it is read, written and
#: offered like ``local``.
KIND_TOR = "tor"
#: The three vendor kinds 7.91.0 builds (PR-S4 + PR-S5).
KIND_ACCOUNT = "account"
KIND_GATEWAY = "gateway"
KIND_LIST = "list"
VENDOR_KINDS: tuple[str, ...] = (KIND_ACCOUNT, KIND_GATEWAY, KIND_LIST)
#: Every kind the store knows the name of, in the spec's order (§5.1). Only
#: :data:`BUILT_KINDS` are read, written and offered by this release.
SOURCE_KINDS: tuple[str, ...] = (
    KIND_LOCAL,
    KIND_ACCOUNT,
    KIND_GATEWAY,
    KIND_LIST,
    "runner",
)
BUILT_KINDS: tuple[str, ...] = (KIND_LOCAL, KIND_TOR, *VENDOR_KINDS)
#: Every kind a document may hold: the spec's five, and ``tor``.
KNOWN_KINDS: tuple[str, ...] = (*SOURCE_KINDS, KIND_TOR)
#: Tor sources are ``src_tor``, then ``src_tor_2``, ``src_tor_3`` ...
TOR_SOURCE_PREFIX = "src_tor"
TOR_SOURCE_NAME = "Tor"
#: How MCC logs in to a tor's control port: the cookie file tor names, or a
#: control password (``HashedControlPassword`` in its torrc).
TOR_AUTH_COOKIE = "cookie"
TOR_AUTH_PASSWORD = "password"
TOR_AUTH_KINDS: tuple[str, ...] = (TOR_AUTH_COOKIE, TOR_AUTH_PASSWORD)

PROTOCOL_SOCKS5 = "socks5"
PROTOCOL_HTTP = "http"
#: What a listener asked for: nothing, a username and password, or neither
#: method MCC can speak (a SOCKS5 server refusing both).
AUTH_NONE = "none"
AUTH_USERPASS = "userpass"
AUTH_UNSUPPORTED = "unsupported"
AUTH_KINDS: tuple[str, ...] = (AUTH_NONE, AUTH_USERPASS, AUTH_UNSUPPORTED)

SECRET_USERPASS = "userpass"
#: A tor control password (7.90.0): no username, so nothing to mask either.
SECRET_PASSWORD = "password"
SECRET_TYPES: tuple[str, ...] = (SECRET_USERPASS, SECRET_PASSWORD)

#: The one local source an install has: this computer.
LOCAL_SOURCE_ID = "src_local"
LOCAL_SOURCE_NAME = "This computer"
LOCAL_HOST = "127.0.0.1"

#: The schemes a vendor source's addresses may use. ``socks5h`` -- the
#: destination's name resolved at the exit -- for SOCKS5; ``http`` for an HTTP
#: CONNECT proxy (which also hands the proxy the name); ``https`` for a proxy
#: reached over TLS itself.
VENDOR_SCHEMES: tuple[str, ...] = ("socks5h", "http", "https")
#: The longest name a source may carry.
SOURCE_NAME_MAX_LENGTH = 60
#: The longest a schedule may wait between fetches: thirty days. ``0`` is off.
REFRESH_HOURS_MAX = 24 * 30
#: A gateway session id as MCC mints them: 8 to 32 lower-case letters and
#: digits, which every gateway's syntax accepts (IPRoyal's wants exactly 8).
SESSION_ID_PATTERN = re.compile(r"^[a-z0-9]{8,32}$")


def proxy_sources_path() -> Path:
    """Beside ``proxy_chains.json``: configuration, not a log."""

    return config_dir_path() / PROXY_SOURCES_FILENAME


@dataclass(frozen=True, slots=True)
class SourceSecret:
    """A credential a source needs. Never rendered back to anybody."""

    type: str = SECRET_USERPASS
    username: str = ""
    password: str = ""

    @property
    def label(self) -> str:
        """The masked name a page may show: the username's ``first4…last4``.

        A control password has no username, and no part of a password is
        ever shown, so its label is empty.
        """

        if self.type == SECRET_PASSWORD:
            return ""
        return mask_key_label(self.username)

    def as_document(self) -> dict[str, Any]:
        if self.type == SECRET_PASSWORD:
            return {"type": self.type, "password": self.password}
        return {"type": self.type, "username": self.username, "password": self.password}

    @classmethod
    def from_document(cls, raw: object) -> Self | None:
        if not isinstance(raw, Mapping):
            return None
        kind = str(raw.get("type") or "").strip().lower()
        if kind not in SECRET_TYPES:
            return None
        return cls(
            type=kind,
            username="" if kind == SECRET_PASSWORD else str(raw.get("username") or ""),
            password=str(raw.get("password") or ""),
        )


@dataclass(frozen=True, slots=True)
class LocalListener:
    """One port on this computer that answered the scan as a proxy, or did not.

    ``proxy`` is the catalogue id of the address it yields (``""`` when it
    yields none: not a proxy, a method MCC cannot speak, or not answering).
    """

    port: int
    protocol: str = ""
    auth: str = AUTH_NONE
    answering: bool = True
    proxy: str = ""
    secret: str = ""
    #: What answered when it was not a proxy MCC can use, in a few words.
    note: str = ""

    @property
    def scheme(self) -> str:
        return "socks5h" if self.protocol == PROTOCOL_SOCKS5 else PROTOCOL_HTTP

    def url(self, secret: SourceSecret | None = None) -> str:
        """The address this listener yields, with its credential when it has one."""

        userinfo = ""
        if secret is not None and secret.username:
            userinfo = (
                f"{quote(secret.username, safe='')}:{quote(secret.password, safe='')}@"
            )
        return f"{self.scheme}://{userinfo}{LOCAL_HOST}:{self.port}"

    @property
    def usable(self) -> bool:
        """Whether this listener yields an address a chain could dial."""

        return (
            self.answering
            and self.protocol in {PROTOCOL_SOCKS5, PROTOCOL_HTTP}
            and self.auth in {AUTH_NONE, AUTH_USERPASS}
        )

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "port": self.port,
            "protocol": self.protocol,
            "auth": self.auth,
            "answering": self.answering,
            "proxy": self.proxy,
        }
        if self.secret:
            document["secret"] = self.secret
        if self.note:
            document["note"] = self.note
        return document

    @classmethod
    def from_document(cls, raw: object) -> Self | None:
        if not isinstance(raw, Mapping):
            return None
        raw_port = raw.get("port")
        if isinstance(raw_port, bool) or not isinstance(raw_port, int | str):
            return None
        try:
            port = int(raw_port)
        except ValueError:
            return None
        if not 0 < port < 65536:
            return None
        protocol = str(raw.get("protocol") or "").strip().lower()
        auth = str(raw.get("auth") or AUTH_NONE).strip().lower()
        return cls(
            port=port,
            protocol=protocol if protocol in {PROTOCOL_SOCKS5, PROTOCOL_HTTP} else "",
            auth=auth if auth in AUTH_KINDS else AUTH_NONE,
            answering=bool(raw.get("answering", True)),
            proxy=str(raw.get("proxy") or "").strip(),
            secret=str(raw.get("secret") or "").strip(),
            note=str(raw.get("note") or "").strip()[:200],
        )


def _port_number(raw: object) -> int | None:
    """A TCP port from a document value, or ``None`` for anything else."""

    if isinstance(raw, bool) or not isinstance(raw, int | str):
        return None
    try:
        port = int(raw)
    except ValueError:
        return None
    return port if 0 < port < 65536 else None


@dataclass(frozen=True, slots=True)
class TorPort:
    """One of a tor's SOCKS ports, and the catalogue id it is offered as."""

    port: int
    proxy: str = ""

    def as_document(self) -> dict[str, Any]:
        return {"port": self.port, "proxy": self.proxy}

    @classmethod
    def from_document(cls, raw: object) -> Self | None:
        if not isinstance(raw, Mapping):
            return None
        port = _port_number(raw.get("port"))
        if port is None:
            return None
        return cls(port=port, proxy=str(raw.get("proxy") or "").strip())


@dataclass(frozen=True, slots=True)
class TorSettings:
    """A tor the user runs: where its SOCKS and control ports are, how to log in.

    Each SOCKS port is one identity -- tor keeps streams arriving on separate
    ``SocksPort`` lines on separate circuits by default -- and becomes one
    chain address. The control port is asked only when a button is pressed.
    """

    socks_ports: tuple[TorPort, ...]
    control_port: int
    auth: str = TOR_AUTH_COOKIE
    #: The ``sec_`` id of the control password, when :attr:`auth` is
    #: ``password``. The cookie is never stored: tor names its file and MCC
    #: reads it at the moment it logs in.
    secret: str = ""

    @property
    def ports(self) -> tuple[int, ...]:
        return tuple(item.port for item in self.socks_ports)

    def torrc_lines(self) -> str:
        """What the user pastes into their own torrc: the ports they chose.

        Loopback only, so no other computer can use this tor. A control
        password is never written here -- tor wants its hash, which only
        ``tor --hash-password`` makes -- so the password form names the
        command instead of a value.
        """

        lines = [
            "# My Claude Code: one SocksPort per identity, and the control port",
            *(f"SocksPort {LOCAL_HOST}:{port}" for port in self.ports),
            f"ControlPort {LOCAL_HOST}:{self.control_port}",
        ]
        if self.auth == TOR_AUTH_PASSWORD:
            lines += [
                "# Replace the value with what `tor --hash-password <password>` prints",
                "HashedControlPassword 16:PASTE-THE-HASH-HERE",
            ]
        else:
            lines.append("CookieAuthentication 1")
        return "\n".join(lines) + "\n"

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "control_port": self.control_port,
            "auth": self.auth,
            "socks_ports": [item.as_document() for item in self.socks_ports],
        }
        if self.secret:
            document["secret"] = self.secret
        return document

    @classmethod
    def from_document(cls, raw: Mapping[Any, object]) -> Self | None:
        control_port = _port_number(raw.get("control_port"))
        raw_ports = raw.get("socks_ports")
        ports: list[TorPort] = []
        if isinstance(raw_ports, Sequence) and not isinstance(raw_ports, str):
            seen: set[int] = set()
            for item in raw_ports:
                parsed = TorPort.from_document(item)
                if (
                    parsed is not None
                    and parsed.port not in seen
                    and parsed.port != control_port
                ):
                    seen.add(parsed.port)
                    ports.append(parsed)
        if control_port is None or not ports:
            return None
        auth = str(raw.get("auth") or TOR_AUTH_COOKIE).strip().lower()
        return cls(
            socks_ports=tuple(ports),
            control_port=control_port,
            auth=auth if auth in TOR_AUTH_KINDS else TOR_AUTH_COOKIE,
            secret=str(raw.get("secret") or "").strip(),
        )


def _scheme(raw: object) -> str:
    scheme = str(raw or "").strip().lower()
    if scheme == "socks5":
        # A SOCKS5 address from a source always resolves at the exit.
        return "socks5h"
    return scheme if scheme in VENDOR_SCHEMES else ""


def _hours(raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int | str):
        return 0
    try:
        hours = int(raw)
    except ValueError:
        return 0
    return hours if 0 < hours <= REFRESH_HOURS_MAX else 0


def _countries(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        return ()
    kept: list[str] = []
    for item in raw:
        code = str(item or "").strip().upper()
        if len(code) == 2 and code.isalpha() and code not in kept:
            kept.append(code)
    return tuple(kept)


@dataclass(frozen=True, slots=True)
class FetchState:
    """A vendor list's fetch: through which chain, how often, how it last went.

    ``via`` names the provider whose proxy chain carries the fetch (the exit
    one of that provider's probes would take); ``""`` is this computer.
    ``refresh_hours`` 0 means "only when Fetch now is pressed" and is what
    every source starts with.
    """

    via: str = ""
    refresh_hours: int = 0
    fetched_at: str = ""
    ok: bool = False
    note: str = ""

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {}
        if self.via:
            document["fetch_via"] = self.via
        if self.refresh_hours:
            document["refresh_hours"] = self.refresh_hours
        if self.fetched_at:
            document["fetched_at"] = self.fetched_at
            document["fetch_ok"] = self.ok
        if self.note:
            document["fetch_note"] = self.note
        return document

    @classmethod
    def from_document(cls, raw: Mapping[Any, object]) -> Self:
        fetched_at = str(raw.get("fetched_at") or "").strip()
        return cls(
            via=str(raw.get("fetch_via") or "").strip().lower(),
            refresh_hours=_hours(raw.get("refresh_hours")),
            fetched_at=fetched_at,
            ok=bool(raw.get("fetch_ok")) if fetched_at else False,
            note=str(raw.get("fetch_note") or "").strip()[:300],
        )


@dataclass(frozen=True, slots=True)
class HostList:
    """A vendor's server list the operator confirmed, and what it last said.

    ``url`` is the vendor's own API URL, kept owner-only here and never sent
    back (the page shows its host). ``countries`` empty keeps every server the
    list names; ``found`` is every country the last answer named, so the page
    can offer them; ``hosts`` is what the last answer gave after the filter.
    """

    parser: str
    url: str
    countries: tuple[str, ...] = ()
    fetch: FetchState = field(default_factory=FetchState)
    found: tuple[str, ...] = ()
    hosts: tuple[ListedHost, ...] = ()

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {"parser": self.parser, "url": self.url}
        if self.countries:
            document["countries"] = list(self.countries)
        document |= self.fetch.as_document()
        if self.found:
            document["countries_found"] = list(self.found)
        if self.hosts:
            document["hosts"] = [item.as_document() for item in self.hosts]
        return document

    @classmethod
    def from_document(cls, raw: object) -> Self | None:
        if not isinstance(raw, Mapping):
            return None
        parser = str(raw.get("parser") or "").strip().lower()
        url = str(raw.get("url") or "").strip()
        if parser != READER_NORDVPN_SERVERS or not url:
            return None
        hosts: list[ListedHost] = []
        raw_hosts = raw.get("hosts")
        if isinstance(raw_hosts, Sequence) and not isinstance(raw_hosts, str):
            seen: set[str] = set()
            for item in raw_hosts:
                listed = ListedHost.from_document(item)
                if listed is not None and listed.host not in seen:
                    seen.add(listed.host)
                    hosts.append(listed)
        return cls(
            parser=parser,
            url=url,
            countries=_countries(raw.get("countries")),
            fetch=FetchState.from_document(raw),
            found=_countries(raw.get("countries_found")),
            hosts=tuple(hosts[:FEED_MAX_ENDPOINTS]),
        )


@dataclass(frozen=True, slots=True)
class AccountSettings:
    """A VPN account's proxy: its hosts x its service credential (7.91.0)."""

    scheme: str
    port: int
    hosts: tuple[TypedHost, ...] = ()
    preset: str = ""
    #: The ``sec_`` id of the service credential; ``""`` for a proxy that
    #: takes no login (Mullvad's and IVPN's inside their tunnels).
    secret: str = ""
    #: The addresses answer only while the vendor's own tunnel is up.
    in_tunnel: bool = False
    host_list: HostList | None = None

    def all_hosts(self) -> tuple[tuple[TypedHost, ListedHost | None], ...]:
        """Typed hosts first, then the list's, each ``host[:port]`` once.

        One host on two ports is two addresses (TorGuard's 1080 and 1085).
        """

        seen: set[str] = set()
        merged: list[tuple[TypedHost, ListedHost | None]] = []
        for typed in self.hosts:
            if typed.text not in seen:
                seen.add(typed.text)
                merged.append((typed, None))
        listed = self.host_list.hosts if self.host_list is not None else ()
        for item in listed:
            if item.host not in seen:
                seen.add(item.host)
                merged.append((TypedHost(host=item.host), item))
        return tuple(merged[:FEED_MAX_ENDPOINTS])

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {}
        if self.preset:
            document["preset"] = self.preset
        document |= {
            "scheme": self.scheme,
            "port": self.port,
            "hosts": [item.text for item in self.hosts],
        }
        if self.in_tunnel:
            document["in_tunnel"] = True
        if self.host_list is not None:
            document["host_list"] = self.host_list.as_document()
        if self.secret:
            document["secret"] = self.secret
        return document

    @classmethod
    def from_document(cls, raw: Mapping[Any, object]) -> Self | None:
        scheme = _scheme(raw.get("scheme"))
        port = _port_number(raw.get("port"))
        if not scheme or port is None:
            return None
        hosts: list[TypedHost] = []
        raw_hosts = raw.get("hosts")
        if isinstance(raw_hosts, Sequence) and not isinstance(raw_hosts, str):
            seen: set[str] = set()
            for item in raw_hosts:
                typed = parse_typed_host(item)
                if typed is not None and typed.text not in seen:
                    seen.add(typed.text)
                    hosts.append(typed)
        host_list = HostList.from_document(raw.get("host_list"))
        if not hosts and host_list is None:
            return None
        return cls(
            scheme=scheme,
            port=port,
            hosts=tuple(hosts[:FEED_MAX_ENDPOINTS]),
            preset=str(raw.get("preset") or "").strip().lower(),
            secret=str(raw.get("secret") or "").strip(),
            in_tunnel=raw.get("in_tunnel") is True,
            host_list=host_list,
        )


@dataclass(frozen=True, slots=True)
class GatewaySettings:
    """A commercial gateway's session template x N sessions (7.91.0).

    ``preset`` names the vendor whose session syntax builds each login
    (one of :data:`~my_claude_code.config.proxy_presets.GATEWAY_TEMPLATES`).
    The customer or user name and the password are the ``userpass`` secret;
    ``zone``, ``zone_type``, ``country`` and ``minutes`` are the rest of the
    template, and ``sessions`` are the random ids, one per offered address.
    """

    preset: str
    host: str
    port: int
    scheme: str
    sessions: tuple[str, ...] = ()
    secret: str = ""
    zone: str = ""
    zone_type: str = ""
    country: str = ""
    minutes: int = 0
    #: The highest session number this gateway has minted. Session ids end in
    #: it (base 36), so renewing never hands the vendor an id used before.
    minted: int = 0

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "preset": self.preset,
            "host": self.host,
            "port": self.port,
            "scheme": self.scheme,
        }
        if self.zone:
            document["zone"] = self.zone
        if self.zone_type:
            document["zone_type"] = self.zone_type
        if self.country:
            document["country"] = self.country
        if self.minutes:
            document["minutes"] = self.minutes
        document["sessions"] = list(self.sessions)
        if self.minted:
            document["minted"] = self.minted
        if self.secret:
            document["secret"] = self.secret
        return document

    @classmethod
    def from_document(cls, raw: Mapping[Any, object]) -> Self | None:
        vendor = preset(str(raw.get("preset") or ""))
        host = normalise_host(raw.get("host"))
        port = _port_number(raw.get("port"))
        scheme = _scheme(raw.get("scheme"))
        if (
            vendor is None
            or vendor.template not in GATEWAY_TEMPLATES
            or not host
            or port is None
            or not scheme
        ):
            return None
        sessions: list[str] = []
        raw_sessions = raw.get("sessions")
        if isinstance(raw_sessions, Sequence) and not isinstance(raw_sessions, str):
            for item in raw_sessions:
                session = str(item or "").strip().lower()
                if SESSION_ID_PATTERN.match(session) and session not in sessions:
                    sessions.append(session)
        zone_type = str(raw.get("zone_type") or "").strip().lower()
        minutes = _port_number(raw.get("minutes"))
        raw_minted = raw.get("minted")
        minted = (
            raw_minted
            if isinstance(raw_minted, int) and not isinstance(raw_minted, bool)
            else 0
        )
        return cls(
            preset=vendor.id,
            host=host,
            port=port,
            scheme=scheme,
            sessions=tuple(sessions[:FEED_MAX_ENDPOINTS]),
            secret=str(raw.get("secret") or "").strip(),
            zone=str(raw.get("zone") or "").strip()[:80],
            zone_type=zone_type if zone_type in ZONE_TYPES else "",
            country=str(raw.get("country") or "").strip().lower()[:2],
            minutes=minutes or 0,
            minted=max(0, minted),
        )


@dataclass(frozen=True, slots=True)
class ListEntry:
    """One row of a proxy list: where it is, its catalogue id, if it logs in.

    The row's login is NOT here: it lives only in its address's URL in
    ``proxy_chains.json``.
    """

    host: str
    port: int
    proxy: str
    login: bool = False

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "proxy": self.proxy,
        }
        if self.login:
            document["login"] = True
        return document

    @classmethod
    def from_document(cls, raw: object) -> Self | None:
        if not isinstance(raw, Mapping):
            return None
        host = normalise_host(raw.get("host"))
        port = _port_number(raw.get("port"))
        proxy = str(raw.get("proxy") or "").strip()
        if not host or port is None or not proxy:
            return None
        return cls(host=host, port=port, proxy=proxy, login=raw.get("login") is True)


@dataclass(frozen=True, slots=True)
class ListSettings:
    """A proxy list a vendor gave the operator: pasted, or their download link."""

    scheme: str
    parser: str = READER_PROXY_LIST
    preset: str = ""
    #: The ``sec_`` id of the download link (a ``password`` secret: the link
    #: carries the vendor's token). ``""``: the list was pasted.
    secret: str = ""
    #: ``scheme://host`` of that link -- all of it the page ever shows.
    url_host: str = ""
    fetch: FetchState = field(default_factory=FetchState)
    rows: tuple[ListEntry, ...] = ()

    def as_document(self) -> dict[str, Any]:
        document: dict[str, Any] = {}
        if self.preset:
            document["preset"] = self.preset
        document |= {"parser": self.parser, "scheme": self.scheme}
        if self.url_host:
            document["url_host"] = self.url_host
        document |= self.fetch.as_document()
        document["rows"] = [row.as_document() for row in self.rows]
        if self.secret:
            document["secret"] = self.secret
        return document

    @classmethod
    def from_document(cls, raw: Mapping[Any, object]) -> Self | None:
        parser = str(raw.get("parser") or READER_PROXY_LIST).strip().lower()
        scheme = _scheme(raw.get("scheme"))
        if parser != READER_PROXY_LIST or not scheme:
            return None
        rows: list[ListEntry] = []
        raw_rows = raw.get("rows")
        if isinstance(raw_rows, Sequence) and not isinstance(raw_rows, str):
            seen: set[str] = set()
            for item in raw_rows:
                entry = ListEntry.from_document(item)
                if entry is not None and entry.proxy not in seen:
                    seen.add(entry.proxy)
                    rows.append(entry)
        return cls(
            scheme=scheme,
            parser=parser,
            preset=str(raw.get("preset") or "").strip().lower(),
            secret=str(raw.get("secret") or "").strip(),
            url_host=str(raw.get("url_host") or "").strip()[:200],
            fetch=FetchState.from_document(raw),
            rows=tuple(rows[:FEED_MAX_ENDPOINTS]),
        )


@dataclass(frozen=True, slots=True)
class ProxySource:
    """One source. ``raw`` keeps a kind this release does not build, verbatim."""

    id: str
    kind: str
    name: str = ""
    enabled: bool = True
    added_at: str = ""
    scanned_at: str = ""
    listeners: tuple[LocalListener, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict)
    #: A ``tor`` source's ports and login (7.90.0); ``None`` for every other kind.
    tor: TorSettings | None = None
    #: The vendor kinds (7.91.0); each ``None`` for every other kind.
    account: AccountSettings | None = None
    gateway: GatewaySettings | None = None
    proxy_list: ListSettings | None = None

    @property
    def built(self) -> bool:
        return self.kind in BUILT_KINDS

    @property
    def vendor(self) -> AccountSettings | GatewaySettings | ListSettings | None:
        """The vendor kind's settings, when this is one."""

        return self.account or self.gateway or self.proxy_list

    def secret_ids(self) -> tuple[str, ...]:
        if not self.built:
            value = self.raw.get("secret")
            return (str(value),) if isinstance(value, str) and value else ()
        if self.tor is not None:
            return (self.tor.secret,) if self.tor.secret else ()
        vendor = self.vendor
        if vendor is not None:
            return (vendor.secret,) if vendor.secret else ()
        return tuple(listener.secret for listener in self.listeners if listener.secret)

    def listener(self, port: int) -> LocalListener | None:
        return next((item for item in self.listeners if item.port == port), None)

    def as_document(self) -> dict[str, Any]:
        if not self.built:
            return dict(self.raw)
        if self.tor is not None:
            return {
                "kind": self.kind,
                "name": self.name,
                "enabled": self.enabled,
                "added_at": self.added_at,
            } | self.tor.as_document()
        vendor = self.vendor
        if vendor is not None:
            return {
                "kind": self.kind,
                "name": self.name,
                "enabled": self.enabled,
                "added_at": self.added_at,
            } | vendor.as_document()
        return {
            "kind": self.kind,
            "name": self.name,
            "enabled": self.enabled,
            "added_at": self.added_at,
            "scanned_at": self.scanned_at,
            "listeners": [listener.as_document() for listener in self.listeners],
        }

    @classmethod
    def from_document(cls, source_id: str, raw: object) -> Self | None:
        if not isinstance(raw, Mapping):
            logger.warning(
                "PROXY SOURCES: '{}' is not an object; ignoring it", source_id
            )
            return None
        kind = str(raw.get("kind") or "").strip().lower()
        if kind not in KNOWN_KINDS:
            logger.warning(
                "PROXY SOURCES: '{}' has a kind MCC does not know; ignoring it",
                source_id,
            )
            return None
        if kind == KIND_TOR:
            tor = TorSettings.from_document(raw)
            if tor is None:
                logger.warning(
                    "PROXY SOURCES: '{}' names no usable SOCKS or control port; "
                    "ignoring it",
                    source_id,
                )
                return None
            return cls(
                id=source_id,
                kind=kind,
                name=str(raw.get("name") or "").strip() or TOR_SOURCE_NAME,
                enabled=raw.get("enabled") is not False,
                added_at=str(raw.get("added_at") or "").strip(),
                tor=tor,
            )
        if kind in VENDOR_KINDS:
            return cls._vendor_from_document(source_id, kind, raw)
        listeners: list[LocalListener] = []
        raw_listeners = raw.get("listeners")
        if (
            kind in BUILT_KINDS
            and isinstance(raw_listeners, Sequence)
            and not isinstance(raw_listeners, str)
        ):
            seen: set[int] = set()
            for item in raw_listeners:
                listener = LocalListener.from_document(item)
                if listener is not None and listener.port not in seen:
                    seen.add(listener.port)
                    listeners.append(listener)
        return cls(
            id=source_id,
            kind=kind,
            name=str(raw.get("name") or "").strip(),
            enabled=raw.get("enabled") is not False,
            added_at=str(raw.get("added_at") or "").strip(),
            scanned_at=str(raw.get("scanned_at") or "").strip(),
            listeners=tuple(listeners),
            # A kind this release does not build is kept verbatim, so the
            # release that builds it reads what was written.
            raw={}
            if kind in BUILT_KINDS
            else {str(key): value for key, value in raw.items()},
        )

    @classmethod
    def _vendor_from_document(
        cls, source_id: str, kind: str, raw: Mapping[Any, object]
    ) -> Self | None:
        """An ``account``, ``gateway`` or ``list`` source (7.91.0)."""

        account = AccountSettings.from_document(raw) if kind == KIND_ACCOUNT else None
        gateway = GatewaySettings.from_document(raw) if kind == KIND_GATEWAY else None
        proxy_list = ListSettings.from_document(raw) if kind == KIND_LIST else None
        if account is None and gateway is None and proxy_list is None:
            logger.warning(
                "PROXY SOURCES: '{}' is a {} source MCC cannot read; ignoring it",
                source_id,
                kind,
            )
            return None
        return cls(
            id=source_id,
            kind=kind,
            name=str(raw.get("name") or "").strip()[:SOURCE_NAME_MAX_LENGTH]
            or source_id,
            enabled=raw.get("enabled") is not False,
            added_at=str(raw.get("added_at") or "").strip(),
            account=account,
            gateway=gateway,
            proxy_list=proxy_list,
        )


@dataclass(frozen=True, slots=True)
class ProxySources:
    """Every source and every secret this install holds."""

    sources: Mapping[str, ProxySource] = field(default_factory=dict)
    secrets: Mapping[str, SourceSecret] = field(default_factory=dict)
    #: Why the file could not be read, when this table is NOT what it holds.
    #: Never persisted; a save of a table derived from it is refused, so a
    #: file nobody could read is never replaced by one built from nothing.
    unreadable: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.sources and not self.secrets

    def source(self, source_id: str) -> ProxySource | None:
        return self.sources.get(source_id)

    def secret(self, secret_id: str) -> SourceSecret | None:
        return self.secrets.get(secret_id) if secret_id else None

    def with_source(self, source: ProxySource) -> ProxySources:
        sources = dict(self.sources)
        sources[source.id] = source
        return replace(self, sources=sources).without_orphan_secrets()

    def without_source(self, source_id: str) -> ProxySources:
        sources = {
            key: value for key, value in self.sources.items() if key != source_id
        }
        return replace(self, sources=sources).without_orphan_secrets()

    def with_secret(self, secret_id: str, secret: SourceSecret) -> ProxySources:
        stored = dict(self.secrets)
        stored[secret_id] = secret
        return replace(self, secrets=stored)

    def without_orphan_secrets(self) -> ProxySources:
        """Drop secrets no source names any more: a credential is not kept for nothing."""

        named = {
            secret_id
            for source in self.sources.values()
            for secret_id in source.secret_ids()
        }
        kept = {key: value for key, value in self.secrets.items() if key in named}
        if len(kept) == len(self.secrets):
            return self
        return replace(self, secrets=kept)

    def as_document(self) -> dict[str, Any]:
        return {
            "version": DOCUMENT_VERSION,
            "sources": {
                source_id: source.as_document()
                for source_id, source in self.sources.items()
            },
            "secrets": {
                secret_id: secret.as_document()
                for secret_id, secret in self.secrets.items()
            },
        }

    @classmethod
    def from_document(cls, document: object) -> Self:
        if not isinstance(document, Mapping):
            logger.warning("PROXY SOURCES: top-level JSON value is not an object")
            return cls()
        sources: dict[str, ProxySource] = {}
        raw_sources = document.get("sources")
        if isinstance(raw_sources, Mapping):
            for raw_id, raw in raw_sources.items():
                source_id = str(raw_id).strip()
                if not source_id:
                    continue
                source = ProxySource.from_document(source_id, raw)
                if source is not None:
                    sources[source_id] = source
        stored: dict[str, SourceSecret] = {}
        raw_secrets = document.get("secrets")
        if isinstance(raw_secrets, Mapping):
            for raw_id, raw in raw_secrets.items():
                secret = SourceSecret.from_document(raw)
                if secret is not None and str(raw_id).strip():
                    stored[str(raw_id).strip()] = secret
        return cls(sources=sources, secrets=stored)


EMPTY_PROXY_SOURCES = ProxySources()

#: Every write of ``proxy_sources.json``, from its re-read to its save.
PROXY_SOURCES_WRITE_LOCK = threading.RLock()


def mint_secret_id(existing: Mapping[str, SourceSecret]) -> str:
    while True:
        secret_id = f"sec_{secrets.token_hex(4)}"
        if secret_id not in existing:
            return secret_id


def account_host_key(typed: TypedHost) -> str:
    """An account host's offer key: ``host:<host>`` or ``host:<host>:<port>``.

    A host typed without a port follows the account's port, so changing that
    port rewrites the same row; a host typed with its own port is its own
    address.
    """

    return f"host:{typed.text}"


def vendor_offer_id(source_id: str, key: str) -> str:
    """The catalogue id one of a vendor source's addresses is always filed under.

    Derived, not minted (7.91.0): a refetch or an edit finds the same row --
    its checks, its health, the chains using it -- and only its URL changes.
    ``key`` is ``host:<host>`` (an account), ``session:<id>`` (a gateway) or
    ``row:<host>:<port>:<user>`` (a list).
    """

    digest = hashlib.sha256(f"{source_id}:{key}".encode()).hexdigest()
    return f"px_{digest[:10]}"


def load_proxy_sources(path: Path | None = None) -> ProxySources:
    """Read the store, never raising. A missing file is no sources at all."""

    resolved = path if path is not None else proxy_sources_path()
    try:
        raw = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        return EMPTY_PROXY_SOURCES
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(
            "PROXY SOURCES: cannot read {}: {}", resolved.name, type(exc).__name__
        )
        return ProxySources(unreadable=f"could not be read ({type(exc).__name__})")
    if not raw.strip():
        return ProxySources(unreadable="is empty")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("PROXY SOURCES: {} is not valid JSON", resolved.name)
        return ProxySources(unreadable="is not valid JSON")
    if not isinstance(document, Mapping):
        return ProxySources(unreadable="is not a JSON object")
    return ProxySources.from_document(document)


# (signature, table): the file's own stat and the table read under it, so
# the Proxying page -- which asks on every repaint, and every 1.5 s while a
# fetch runs -- reads the file again only when it changed.
_CACHE: tuple[tuple[str, int, int] | None, ProxySources] = (None, EMPTY_PROXY_SOURCES)


def current_proxy_sources(path: Path | None = None) -> ProxySources:
    """The store for a reader, re-read only when the file changed."""

    global _CACHE
    resolved = path if path is not None else proxy_sources_path()
    try:
        stat = resolved.stat()
    except FileNotFoundError:
        _CACHE = (None, EMPTY_PROXY_SOURCES)
        return EMPTY_PROXY_SOURCES
    except OSError:
        return load_proxy_sources(resolved)
    signature = (str(resolved), stat.st_mtime_ns, stat.st_size)
    if _CACHE[0] == signature:
        return _CACHE[1]
    table = load_proxy_sources(resolved)
    if not table.unreadable:
        _CACHE = (signature, table)
    return table


def reset_proxy_sources_cache() -> None:
    global _CACHE
    _CACHE = (None, EMPTY_PROXY_SOURCES)


class ProxySourcesUnreadableError(OSError):
    """A save refused because the table was derived from a failed read."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(
            f"Not saved: {path.name} {reason}. MCC does not overwrite a source "
            "file it cannot read. Fix or remove it, then try again."
        )


def save_proxy_sources(table: ProxySources, path: Path | None = None) -> None:
    """Write the store owner-only, atomically. Off the event loop."""

    resolved = path if path is not None else proxy_sources_path()
    if table.unreadable:
        raise ProxySourcesUnreadableError(resolved, table.unreadable)
    with PROXY_SOURCES_WRITE_LOCK:
        write_owner_only_json(resolved, table.as_document())
        reset_proxy_sources_cache()


__all__ = [
    "AUTH_KINDS",
    "AUTH_NONE",
    "AUTH_UNSUPPORTED",
    "AUTH_USERPASS",
    "BUILT_KINDS",
    "DOCUMENT_VERSION",
    "EMPTY_PROXY_SOURCES",
    "KIND_ACCOUNT",
    "KIND_GATEWAY",
    "KIND_LIST",
    "KIND_LOCAL",
    "KIND_TOR",
    "KNOWN_KINDS",
    "LOCAL_HOST",
    "LOCAL_SOURCE_ID",
    "LOCAL_SOURCE_NAME",
    "PROTOCOL_HTTP",
    "PROTOCOL_SOCKS5",
    "PROXY_SOURCES_FILENAME",
    "PROXY_SOURCES_WRITE_LOCK",
    "REFRESH_HOURS_MAX",
    "SECRET_PASSWORD",
    "SECRET_TYPES",
    "SECRET_USERPASS",
    "SESSION_ID_PATTERN",
    "SOURCE_KINDS",
    "SOURCE_NAME_MAX_LENGTH",
    "TOR_AUTH_COOKIE",
    "TOR_AUTH_KINDS",
    "TOR_AUTH_PASSWORD",
    "TOR_SOURCE_NAME",
    "TOR_SOURCE_PREFIX",
    "VENDOR_KINDS",
    "VENDOR_SCHEMES",
    "AccountSettings",
    "FetchState",
    "GatewaySettings",
    "HostList",
    "ListEntry",
    "ListSettings",
    "LocalListener",
    "ProxySource",
    "ProxySources",
    "ProxySourcesUnreadableError",
    "SourceSecret",
    "TorPort",
    "TorSettings",
    "account_host_key",
    "current_proxy_sources",
    "load_proxy_sources",
    "mint_secret_id",
    "proxy_sources_path",
    "reset_proxy_sources_cache",
    "save_proxy_sources",
    "vendor_offer_id",
]
