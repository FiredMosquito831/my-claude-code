"""Readers for what a vendor publishes about a proxy source (7.91.0).

Pure functions over text the operator pasted or a vendor's API answered --
no network, no store. Three shapes:

* :func:`parse_nordvpn_servers` -- NordVPN's server list JSON
  (``api.nordvpn.com/v1/servers``): an array of server objects, each with a
  ``hostname``, a ``status``, ``locations[].country.code`` /
  ``.city.name`` and ``technologies[].identifier``. Only servers that list the
  ``socks`` technology (when the object says which technologies it has) and
  are not reported offline are kept.
* :func:`parse_proxy_list` -- the ``ip:port:username:password`` lines a
  Webshare list download (and most commercial list exports) gives, and the
  bare ``ip:port`` lines of a list authorised by source address.
* :func:`parse_host_lines` -- the hosts an operator typed for an account, one
  per line (or separated by commas), each optionally ``host:port``.

Every reader keeps at most :data:`~my_claude_code.config.proxy_feeds.FEED_MAX_ENDPOINTS`
rows: the catalogue is a list an operator reads, not a database.
"""

import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Self
from urllib.parse import urlsplit

from my_claude_code.config.proxy_feeds import FEED_MAX_ENDPOINTS

#: A DNS name: labels of letters, digits and hyphens, dot-separated.
_HOSTNAME = re.compile(
    r"^(?=.{1,253}$)(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))*$"
)


def normalise_host(raw: object) -> str:
    """A host name or IPv4 address, lower-cased; ``""`` for anything else.

    Nothing that could break out of a URL's authority survives: no ``@``,
    ``/``, ``:``, space or bracket.
    """

    text = str(raw or "").strip().lower().rstrip(".")
    if not text:
        return ""
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return text if _HOSTNAME.match(text) and not text.isdigit() else ""
    return str(address) if address.version == 4 else ""


def url_host(url: str) -> str:
    """``https://host`` of a vendor URL -- the only part of one ever shown.

    A download link carries the vendor's token in its path; the host says
    whose list it is and nothing more.
    """

    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
    except ValueError:
        return ""
    return f"{parsed.scheme}://{host}" if host else ""


def _port(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    try:
        port = int(str(raw).strip())
    except ValueError:
        return None
    return port if 0 < port < 65536 else None


@dataclass(frozen=True, slots=True)
class ListedHost:
    """One server a vendor's list names, with where the vendor says it is."""

    host: str
    country: str = ""
    city: str = ""

    def as_document(self) -> dict[str, str]:
        document = {"host": self.host}
        if self.country:
            document["country"] = self.country
        if self.city:
            document["city"] = self.city
        return document

    @classmethod
    def from_document(cls, raw: object) -> Self | None:
        if not isinstance(raw, Mapping):
            return None
        host = normalise_host(raw.get("host"))
        if not host:
            return None
        return cls(
            host=host,
            country=_country(raw.get("country")),
            city=str(raw.get("city") or "").strip()[:60],
        )


def _country(raw: object) -> str:
    code = str(raw or "").strip().upper()
    return code if len(code) == 2 and code.isalpha() else ""


def _server_rows(document: object) -> Sequence[object]:
    if isinstance(document, Sequence) and not isinstance(document, str):
        return document
    if isinstance(document, Mapping):
        servers = document.get("servers")
        if isinstance(servers, Sequence) and not isinstance(servers, str):
            return servers
    return ()


def _location(row: Mapping[Any, object]) -> tuple[str, str]:
    locations = row.get("locations")
    if not isinstance(locations, Sequence) or isinstance(locations, str):
        return "", ""
    for location in locations:
        if not isinstance(location, Mapping):
            continue
        country = location.get("country")
        if not isinstance(country, Mapping):
            continue
        city = country.get("city")
        city_name = (
            str(city.get("name") or "").strip()[:60]
            if isinstance(city, Mapping)
            else ""
        )
        return _country(country.get("code")), city_name
    return "", ""


def _offers_socks(row: Mapping[Any, object]) -> bool:
    technologies = row.get("technologies")
    if not isinstance(technologies, Sequence) or isinstance(technologies, str):
        # The object does not say: the list was asked for SOCKS servers.
        return True
    return any(
        isinstance(item, Mapping)
        and str(item.get("identifier") or "").strip().lower() == "socks"
        for item in technologies
    )


def parse_nordvpn_servers(text: str) -> tuple[ListedHost, ...]:
    """NordVPN's server list -> the SOCKS servers it names, in list order."""

    try:
        document = json.loads(text)
    except ValueError:
        return ()
    found: list[ListedHost] = []
    seen: set[str] = set()
    for row in _server_rows(document):
        if not isinstance(row, Mapping):
            continue
        status = str(row.get("status") or "online").strip().lower()
        if status != "online" or not _offers_socks(row):
            continue
        host = normalise_host(row.get("hostname"))
        if not host or host in seen:
            continue
        seen.add(host)
        country, city = _location(row)
        found.append(ListedHost(host=host, country=country, city=city))
        if len(found) >= FEED_MAX_ENDPOINTS:
            break
    return tuple(found)


@dataclass(frozen=True, slots=True)
class ListRow:
    """One proxy a list names: where it is, and its login if it has one."""

    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def key(self) -> str:
        """What tells two rows apart: the address and the login's user."""

        return f"{self.host}:{self.port}:{self.username}"


def parse_proxy_list(text: str) -> tuple[ListRow, ...]:
    """``ip:port:username:password`` (or ``ip:port``) lines -> rows.

    A line starting with ``#`` is a comment; blank lines and lines that do not
    parse are skipped. A password may itself contain ``:`` or ``#`` --
    everything after the third separator is the password. Duplicates (same
    address and user) are kept once.
    """

    rows: list[ListRow] = []
    seen: set[str] = set()
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 3)
        if len(parts) not in {2, 4}:
            continue
        host = normalise_host(parts[0])
        port = _port(parts[1])
        if not host or port is None:
            continue
        username = parts[2].strip() if len(parts) == 4 else ""
        password = parts[3].strip() if len(parts) == 4 else ""
        if len(parts) == 4 and not username:
            continue
        row = ListRow(host=host, port=port, username=username, password=password)
        if row.key in seen:
            continue
        seen.add(row.key)
        rows.append(row)
        if len(rows) >= FEED_MAX_ENDPOINTS:
            break
    return tuple(rows)


@dataclass(frozen=True, slots=True)
class TypedHost:
    """A host an operator typed for an account, with its own port if given."""

    host: str
    port: int | None = None

    @property
    def text(self) -> str:
        return self.host if self.port is None else f"{self.host}:{self.port}"


def parse_typed_host(raw: object) -> TypedHost | None:
    text = str(raw or "").strip()
    if not text:
        return None
    host_part, sep, port_part = text.rpartition(":")
    if sep and port_part.isdigit():
        host = normalise_host(host_part)
        port = _port(port_part)
        if host and port is not None:
            return TypedHost(host=host, port=port)
        return None
    host = normalise_host(text)
    return TypedHost(host=host) if host else None


def parse_host_lines(text: str) -> tuple[tuple[TypedHost, ...], tuple[str, ...]]:
    """Typed hosts -> (the ones that parse, in order; the ones that do not)."""

    good: list[TypedHost] = []
    bad: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,;]+", str(text or "")):
        if not token:
            continue
        parsed = parse_typed_host(token)
        if parsed is None:
            bad.append(token[:80])
            continue
        if parsed.text in seen:
            continue
        seen.add(parsed.text)
        good.append(parsed)
        if len(good) >= FEED_MAX_ENDPOINTS:
            break
    return tuple(good), tuple(bad)


__all__ = [
    "ListRow",
    "ListedHost",
    "TypedHost",
    "normalise_host",
    "parse_host_lines",
    "parse_nordvpn_servers",
    "parse_proxy_list",
    "parse_typed_host",
    "url_host",
]
