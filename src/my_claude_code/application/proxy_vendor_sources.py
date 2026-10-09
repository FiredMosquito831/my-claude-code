"""Vendor sources: VPN accounts, commercial gateways, proxy lists (7.91.0).

PR-S4 and PR-S5 of ``specs/PR-PROXY-SOURCES-SPEC.md``. Three kinds of source,
each yielding ordinary chain addresses offered exactly like a scanned
listener's or a Tor port's -- in the catalogue, in no chain, added to one
through the bulk add, which tests each against the provider's own host first:

* **account** (S4): a VPN's proxy hosts x the account's service credential.
  NordVPN's SOCKS5 servers, PIA's, IPVanish's, TorGuard's -- and Mullvad's or
  IVPN's login-free proxies *inside* their tunnels, offered with a note that
  they answer only while that tunnel is up. Hosts are typed, or read by the
  ``nordvpn_servers`` reader from the vendor's own server list.
* **gateway** (S5): a commercial gateway's session template x N. Bright Data
  (datacenter and ISP zones only), Oxylabs, IPRoyal, Decodo. MCC spells each
  vendor's session syntax with a random session id per address -- one address
  each, never two with the same id -- so N sessions are N exits with N names.
* **list** (S5): ``ip:port:username:password`` rows a vendor gave the
  operator (Webshare's list download first, by the user's decision 13 of
  2026-10-06), pasted or fetched from the operator's own download link.

**Readers, not sources.** A preset (``config/proxy_presets.py``) prefills a
form; nothing is stored until the operator saves what they confirmed, and
nothing is fetched until they press *Fetch now* or switch on that source's
schedule. A fetch goes to the vendor's URL the operator confirmed -- through
the chain of a provider they name, when they name one -- and to nothing else.

**Secrets** follow decision 6 (option A): the service credential, a
gateway's password and a list's download link live in ``proxy_sources.json``;
every offered address's URL carries its login in ``proxy_chains.json``; both
files are owner-only. A label carries no credential: ``NordVPN ·
nl.socks.nordhold.net``, ``Oxylabs · session 3f9a0c1e0001``, ``Webshare ·
1.2.3.4:8080``. Nothing here logs a URL.

**Off the event loop.** Every function here but :func:`fetch_vendor_text` is
plain computation over the two stores; the routes and the schedule run them on
a worker thread. The fetch itself is one bounded ``httpx`` GET.
"""

import re
import secrets
import string
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, unquote, urlsplit

import httpx

from my_claude_code.application.proxy_ingest import FEED_USER_AGENT
from my_claude_code.application.proxy_sources import SourceEditError
from my_claude_code.config.proxy_chains import (
    SOURCE_SOURCE,
    ProxyChains,
    ProxyEndpoint,
)
from my_claude_code.config.proxy_feeds import (
    FEED_MAX_BYTES,
    FEED_MAX_ENDPOINTS,
    is_valid_feed_url,
)
from my_claude_code.config.proxy_presets import (
    BRIGHTDATA_REFUSAL,
    PRESET_ACCOUNT,
    PRESET_GATEWAY,
    PRESET_LIST,
    READER_NORDVPN_SERVERS,
    READER_PROXY_LIST,
    TEMPLATE_BRIGHTDATA,
    TEMPLATE_DECODO,
    TEMPLATE_IPROYAL,
    TEMPLATE_OXYLABS,
    ZONE_TYPES,
    ZONES_REFUSED,
    VendorPreset,
    preset,
)
from my_claude_code.config.proxy_source_readers import (
    ListRow,
    normalise_host,
    parse_host_lines,
    parse_nordvpn_servers,
    parse_proxy_list,
    url_host,
)
from my_claude_code.config.proxy_sources import (
    KIND_ACCOUNT,
    KIND_GATEWAY,
    KIND_LIST,
    REFRESH_HOURS_MAX,
    SECRET_PASSWORD,
    SECRET_USERPASS,
    SOURCE_NAME_MAX_LENGTH,
    VENDOR_KINDS,
    VENDOR_SCHEMES,
    AccountSettings,
    FetchState,
    GatewaySettings,
    HostList,
    ListEntry,
    ListSettings,
    ProxySource,
    ProxySources,
    SourceSecret,
    account_host_key,
    mint_secret_id,
    vendor_offer_id,
)

#: What a new source of each kind is called when the operator names nothing.
_DEFAULT_NAMES = {
    KIND_ACCOUNT: "VPN account",
    KIND_GATEWAY: "Proxy gateway",
    KIND_LIST: "Proxy list",
}

#: Session ids: a random part, then a counter in base 36 that only ever goes
#: up for one source -- so a source never mints the same id twice, however
#: often its sessions are renewed -- and the random part keeps two sources (or
#: a source removed and added again) apart.
#: Base 36, digits first, so the zero-padded counter reads as a number.
_SESSION_ALPHABET = string.digits + string.ascii_lowercase
_COUNTER_WIDTH = 4
_COUNTER_MAX = len(_SESSION_ALPHABET) ** _COUNTER_WIDTH - 1
#: IPRoyal's session is exactly 8 characters; the others take 12.
_SESSION_RANDOM = {TEMPLATE_IPROYAL: 4}
_SESSION_RANDOM_DEFAULT = 8


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug[:24].strip("_")


def next_vendor_source_id(sources: ProxySources, kind: str, name: str) -> str:
    """``src_<kind>_<name>``, then ``..._2`` ... -- never another source's id.

    The kind leads, so a vendor source's id can never be ``src_local`` or
    start like a Tor source's (``src_tor``), which other code tells apart by
    that prefix.
    """

    base = f"src_{kind}_{_slug(name) or kind}"
    if base not in sources.sources:
        return base
    number = 2
    while f"{base}_{number}" in sources.sources:
        number += 1
    return f"{base}_{number}"


def unique_source_name(sources: ProxySources, wanted: str, own_id: str) -> str:
    """The name, or the name with `` 2``, `` 3`` ... where another source has it.

    Offer labels start with the source's name, so two sources may not share
    one -- or their addresses would be told apart by a suffix alone.
    """

    base = wanted.strip()[:SOURCE_NAME_MAX_LENGTH].strip()
    taken = {
        source.name.strip().lower()
        for source_id, source in sources.sources.items()
        if source_id != own_id
    }
    if base.lower() not in taken:
        return base
    number = 2
    while f"{base} {number}".lower() in taken:
        number += 1
    return f"{base} {number}"


def _labels_taken(chains: ProxyChains, own_ids: Iterable[str]) -> set[str]:
    """Every ledger label in the catalogue but those of ``own_ids``."""

    own = set(own_ids)
    return {
        label
        for proxy_id, label in chains.ledger_labels().items()
        if proxy_id not in own and label
    }


def _unique_label(wanted: str, taken: set[str]) -> str:
    """``wanted``, or ``wanted (2)`` ... -- a ledger key nothing else holds."""

    label = wanted
    number = 2
    while label in taken:
        label = f"{wanted} ({number})"
        number += 1
    taken.add(label)
    return label


def _userinfo(username: str, password: str) -> str:
    if not username:
        return ""
    return f"{quote(username, safe='')}:{quote(password, safe='')}@"


def _authority(host: str, port: int) -> str:
    return f"{host}:{port}"


def _offers(
    chains: ProxyChains,
    source_id: str,
    wanted: Sequence[tuple[str, str, str]],
    at: str,
) -> list[tuple[str, ProxyEndpoint]]:
    """``(key, url, label)`` -> the source's offers, each with a unique name.

    A row the catalogue already holds keeps its name; ``with_source_offers``
    takes only its new URL. A new row's name steps around every other name in
    the catalogue, so the ledgers keep one book per address.
    """

    keyed = [
        (vendor_offer_id(source_id, key), url, label) for key, url, label in wanted
    ]
    taken = _labels_taken(chains, (proxy_id for proxy_id, _, _ in keyed))
    offers: list[tuple[str, ProxyEndpoint]] = []
    for proxy_id, url, label in keyed:
        known = chains.endpoint(proxy_id)
        if known is not None:
            taken.add(chains.ledger_label(proxy_id))
            offers.append((proxy_id, replace(known, url=url)))
            continue
        offers.append(
            (
                proxy_id,
                ProxyEndpoint(
                    url=url,
                    label=_unique_label(label, taken),
                    added_at=at,
                    source=SOURCE_SOURCE,
                    source_id=source_id,
                ),
            )
        )
    return offers


def _checked_scheme(raw: str) -> str:
    scheme = raw.strip().lower()
    if scheme == "socks5":
        scheme = "socks5h"
    if scheme not in VENDOR_SCHEMES:
        raise SourceEditError(
            "The proxy's scheme must be SOCKS5 (socks5h), HTTP or HTTPS."
        )
    return scheme


def _checked_port(value: int, what: str = "Port") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 65536:
        raise SourceEditError(f"{what} {value!r} is not a TCP port (1 to 65535).")
    return value


def _checked_hours(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SourceEditError("The schedule is a number of hours, or 0 for off.")
    if value < 0 or value > REFRESH_HOURS_MAX:
        raise SourceEditError(
            f"The schedule is 0 (off) to {REFRESH_HOURS_MAX} hours between fetches."
        )
    return value


def _checked_url(url: str, what: str) -> str:
    cleaned = url.strip()
    if not is_valid_feed_url(cleaned):
        raise SourceEditError(
            f"{what} must be an https:// URL: a list of addresses that will "
            "carry your requests is never read over plain http."
        )
    return cleaned


def _source_name(
    sources: ProxySources,
    edit_name: str,
    vendor: VendorPreset | None,
    kind: str,
    own_id: str,
) -> str:
    wanted = edit_name.strip() or (
        vendor.name if vendor is not None else _DEFAULT_NAMES[kind]
    )
    return unique_source_name(sources, wanted, own_id)


def _existing(sources: ProxySources, source_id: str, kind: str) -> ProxySource | None:
    if not source_id:
        return None
    existing = sources.source(source_id)
    if existing is None or existing.kind != kind or existing.vendor is None:
        raise SourceEditError(f"No {kind} source {source_id!r}.")
    return existing


def _with_login(
    sources: ProxySources,
    kept: str,
    username: str,
    password: str,
    *,
    clear: bool,
) -> tuple[ProxySources, str]:
    """The source's login after an edit: ``(stores, secret id or "")``.

    An empty username and password keep a stored login; ``clear`` forgets it.
    Nothing ever sends a stored one back to fill the form.
    """

    if clear:
        return sources, ""
    if username:
        secret_id = kept or mint_secret_id(sources.secrets)
        return (
            sources.with_secret(
                secret_id,
                SourceSecret(
                    type=SECRET_USERPASS, username=username, password=password
                ),
            ),
            secret_id,
        )
    if password:
        raise SourceEditError("Give the user name that goes with the password.")
    if kept and sources.secret(kept) is not None:
        return sources, kept
    return sources, ""


# ------------------------------------------------------------- accounts


@dataclass(frozen=True, slots=True)
class AccountEdit:
    """The VPN account form. ``password`` and ``list_url`` are write-only."""

    name: str = ""
    preset: str = ""
    scheme: str = "socks5h"
    port: int = 1080
    #: Hosts typed by the operator: one per line or separated by commas,
    #: each optionally ``host:port``.
    hosts: str = ""
    username: str = ""
    password: str = ""
    clear_login: bool = False
    in_tunnel: bool = False
    #: The vendor's server list URL. Empty keeps a stored one; ``list_off``
    #: drops it.
    list_url: str = ""
    list_off: bool = False
    countries: tuple[str, ...] = ()
    fetch_via: str = ""
    refresh_hours: int = 0


def _checked_countries(raw: Iterable[str]) -> tuple[str, ...]:
    kept: list[str] = []
    for item in raw:
        code = str(item or "").strip().upper()
        if not code:
            continue
        if len(code) != 2 or not code.isalpha():
            raise SourceEditError(
                f"{item!r} is not a two-letter country code (NL, SE, US ...)."
            )
        if code not in kept:
            kept.append(code)
    return tuple(kept)


def account_offer_rows(
    name: str, account: AccountSettings, sources: ProxySources
) -> list[tuple[str, str, str]]:
    """``(key, url, label)`` for every host the account offers.

    Typed hosts always; the server list's hosts in the countries the operator
    kept (all of them when they kept none).
    """

    secret = sources.secret(account.secret)
    login = (
        _userinfo(secret.username, secret.password)
        if secret is not None and secret.type == SECRET_USERPASS
        else ""
    )
    countries = (
        set(account.host_list.countries) if account.host_list is not None else set()
    )
    rows: list[tuple[str, str, str]] = []
    for typed, listed in account.all_hosts():
        if listed is not None and countries and listed.country not in countries:
            continue
        port = typed.port or account.port
        # ``host`` follows the account's port; ``host:port`` keeps its own.
        rows.append(
            (
                account_host_key(typed),
                f"{account.scheme}://{login}{_authority(typed.host, port)}",
                f"{name} · {typed.text}",
            )
        )
    return rows


def save_account_source(
    chains: ProxyChains,
    sources: ProxySources,
    source_id: str,
    edit: AccountEdit,
    *,
    at: str | None = None,
) -> tuple[ProxyChains, ProxySources, str]:
    """Create (``source_id`` empty) or change a VPN account, and offer its hosts.

    Contacts nothing: a server list is fetched only by *Fetch now* or the
    source's schedule.
    """

    stamp = at or _now()
    existing = _existing(sources, source_id, KIND_ACCOUNT)
    vendor = preset(edit.preset)
    if vendor is not None and vendor.kind != PRESET_ACCOUNT:
        raise SourceEditError(f"{vendor.name} is not a VPN account preset.")
    scheme = _checked_scheme(edit.scheme)
    port = _checked_port(edit.port)
    typed, bad = parse_host_lines(edit.hosts)
    if bad:
        raise SourceEditError(
            "Not a host name or IPv4 address: " + ", ".join(bad[:5]) + "."
        )
    previous = existing.account if existing is not None else None
    host_list = previous.host_list if previous is not None else None
    countries = _checked_countries(edit.countries)
    hours = _checked_hours(edit.refresh_hours)
    if edit.list_off:
        host_list = None
    elif edit.list_url.strip():
        url = _checked_url(edit.list_url, "The server list URL")
        if host_list is None or host_list.url != url:
            host_list = HostList(parser=READER_NORDVPN_SERVERS, url=url)
    if host_list is not None:
        host_list = replace(
            host_list,
            countries=countries,
            fetch=replace(
                host_list.fetch, via=edit.fetch_via.strip().lower(), refresh_hours=hours
            ),
        )
    if not typed and host_list is None:
        raise SourceEditError(
            "Give at least one host, or the vendor's server list URL to read "
            "hosts from."
        )
    target = source_id or next_vendor_source_id(
        sources, KIND_ACCOUNT, edit.name or (vendor.name if vendor else "")
    )
    stored, secret_id = _with_login(
        sources,
        previous.secret if previous is not None else "",
        edit.username.strip(),
        edit.password,
        clear=edit.clear_login,
    )
    account = AccountSettings(
        scheme=scheme,
        port=port,
        hosts=typed,
        preset=vendor.id
        if vendor is not None
        else (previous.preset if previous else ""),
        secret=secret_id,
        in_tunnel=edit.in_tunnel or bool(vendor is not None and vendor.in_tunnel),
        host_list=host_list,
    )
    name = _source_name(stored, edit.name, vendor, KIND_ACCOUNT, target)
    source = ProxySource(
        id=target,
        kind=KIND_ACCOUNT,
        name=name,
        enabled=True,
        added_at=existing.added_at if existing is not None else stamp,
        account=account,
    )
    offers = _offers(chains, target, account_offer_rows(name, account, stored), stamp)
    return chains.with_source_offers(target, offers), stored.with_source(source), target


# ------------------------------------------------------------- gateways


@dataclass(frozen=True, slots=True)
class GatewayEdit:
    """The gateway form. ``password`` is write-only; ``renew`` mints new sessions."""

    preset: str
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
    renew: bool = False


def _strip_prefix(value: str, prefix: str) -> str:
    """A pasted ``customer-USER`` is the same user as ``USER``."""

    return value[len(prefix) :] if value.lower().startswith(prefix) else value


def gateway_login(
    gateway: GatewaySettings, user: str, password: str, session: str
) -> tuple[str, str]:
    """One session's ``(username, password)`` in the vendor's own syntax.

    * Bright Data: ``brd-customer-<id>-zone-<zone>[-country-<cc>]-session-<id>``
    * Oxylabs: ``customer-<user>[-cc-<CC>]-sessid-<id>[-sesstime-<minutes>]``
    * IPRoyal: the user as given; the PASSWORD carries
      ``[_country-<cc>]_session-<id>[_lifetime-<minutes>m]``
    * Decodo: ``user-<user>[-country-<cc>]-session-<id>[-sessionduration-<min>]``
    """

    vendor = preset(gateway.preset)
    template = vendor.template if vendor is not None else ""
    country = gateway.country
    if template == TEMPLATE_BRIGHTDATA:
        customer = _strip_prefix(user, "brd-customer-")
        name = f"brd-customer-{customer}-zone-{gateway.zone}"
        if country:
            name += f"-country-{country}"
        return f"{name}-session-{session}", password
    if template == TEMPLATE_OXYLABS:
        name = f"customer-{_strip_prefix(user, 'customer-')}"
        if country:
            name += f"-cc-{country.upper()}"
        name += f"-sessid-{session}"
        if gateway.minutes:
            name += f"-sesstime-{gateway.minutes}"
        return name, password
    if template == TEMPLATE_IPROYAL:
        secret = password
        if country:
            secret += f"_country-{country}"
        secret += f"_session-{session}"
        if gateway.minutes:
            secret += f"_lifetime-{gateway.minutes}m"
        return user, secret
    if template == TEMPLATE_DECODO:
        name = f"user-{_strip_prefix(user, 'user-')}"
        if country:
            name += f"-country-{country}"
        name += f"-session-{session}"
        if gateway.minutes:
            name += f"-sessionduration-{gateway.minutes}"
        return name, password
    raise SourceEditError(f"No session syntax is known for {gateway.preset!r}.")


def _base36(number: int) -> str:
    digits = ""
    while True:
        number, rest = divmod(number, len(_SESSION_ALPHABET))
        digits = _SESSION_ALPHABET[rest] + digits
        if not number:
            return digits


def _counter_of(session: str) -> int:
    tail = session[-_COUNTER_WIDTH:]
    value = 0
    for char in tail:
        index = _SESSION_ALPHABET.find(char)
        if index < 0:
            return -1
        value = value * len(_SESSION_ALPHABET) + index
    return value


def mint_sessions(
    template: str, count: int, after: int, taken: Iterable[str]
) -> tuple[tuple[str, ...], int]:
    """``count`` new session ids numbered after ``after``: ``(ids, last number)``.

    Each id is a random part and a counter that only ever goes up for the
    source, so the source never hands a gateway the same session twice.
    """

    random_width = _SESSION_RANDOM.get(template, _SESSION_RANDOM_DEFAULT)
    seen = set(taken)
    minted: list[str] = []
    counter = after
    while len(minted) < count:
        counter += 1
        if counter > _COUNTER_MAX:
            raise SourceEditError(
                "This gateway has used every session number it can; remove it "
                "and add it again."
            )
        random_part = "".join(
            secrets.choice(_SESSION_ALPHABET) for _ in range(random_width)
        )
        session = random_part + _base36(counter).rjust(_COUNTER_WIDTH, "0")
        if session in seen:
            continue
        seen.add(session)
        minted.append(session)
    return tuple(minted), counter


def _all_sessions(sources: ProxySources) -> set[str]:
    return {
        session
        for source in sources.sources.values()
        if source.gateway is not None
        for session in source.gateway.sessions
    }


def gateway_offer_rows(
    name: str, gateway: GatewaySettings, sources: ProxySources
) -> list[tuple[str, str, str]]:
    """``(key, url, label)`` for every session of a gateway."""

    secret = sources.secret(gateway.secret)
    if secret is None or secret.type != SECRET_USERPASS:
        return []
    rows: list[tuple[str, str, str]] = []
    for session in gateway.sessions:
        username, password = gateway_login(
            gateway, secret.username, secret.password, session
        )
        rows.append(
            (
                f"session:{session}",
                f"{gateway.scheme}://{_userinfo(username, password)}"
                f"{_authority(gateway.host, gateway.port)}",
                f"{name} · session {session}",
            )
        )
    return rows


def _checked_minutes(vendor: VendorPreset, minutes: int) -> int:
    if not vendor.minutes_max:
        return 0
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        raise SourceEditError("The session time is a number of minutes.")
    if not 1 <= minutes <= vendor.minutes_max:
        raise SourceEditError(
            f"{vendor.name} keeps a session 1 to {vendor.minutes_max} minutes."
        )
    return minutes


def _checked_gateway_country(raw: str) -> str:
    code = raw.strip().lower()
    if code and (len(code) != 2 or not code.isalpha()):
        raise SourceEditError(f"{raw!r} is not a two-letter country code.")
    return code


def save_gateway_source(
    chains: ProxyChains,
    sources: ProxySources,
    source_id: str,
    edit: GatewayEdit,
    *,
    at: str | None = None,
) -> tuple[ProxyChains, ProxySources, str]:
    """Create or change a gateway, and offer one address per session.

    Bright Data residential and mobile zones are refused with the reason.
    Changing N keeps the first sessions and mints the rest; ``renew`` mints
    them all again. Contacts nothing.
    """

    stamp = at or _now()
    existing = _existing(sources, source_id, KIND_GATEWAY)
    vendor = preset(edit.preset)
    if vendor is None or vendor.kind != PRESET_GATEWAY:
        raise SourceEditError(
            "Choose the gateway's vendor: Bright Data, Oxylabs, IPRoyal or Decodo."
        )
    zone_type = edit.zone_type.strip().lower()
    zone = edit.zone.strip()
    if vendor.template == TEMPLATE_BRIGHTDATA:
        if zone_type in ZONES_REFUSED:
            raise SourceEditError(BRIGHTDATA_REFUSAL)
        if zone_type not in ZONE_TYPES:
            raise SourceEditError(
                "Say which kind of Bright Data zone this is: datacenter or ISP."
            )
        if not re.fullmatch(r"[A-Za-z0-9_]{1,80}", zone):
            raise SourceEditError(
                "Give the zone's name as Bright Data shows it (letters, digits, _)."
            )
    else:
        zone, zone_type = "", ""
    host = normalise_host(edit.host)
    if not host:
        raise SourceEditError(
            "Give the gateway's host name, as the vendor documents it."
        )
    port = _checked_port(edit.port)
    scheme = _checked_scheme(edit.scheme)
    count = edit.count
    if (
        isinstance(count, bool)
        or not isinstance(count, int)
        or not 1 <= count <= FEED_MAX_ENDPOINTS
    ):
        raise SourceEditError(
            f"Ask for 1 to {FEED_MAX_ENDPOINTS} sessions: each one is one address."
        )
    previous = existing.gateway if existing is not None else None
    stored, secret_id = _with_login(
        sources,
        previous.secret if previous is not None else "",
        edit.user.strip(),
        edit.password,
        clear=False,
    )
    if not secret_id:
        raise SourceEditError(
            f"Give your {vendor.name} user name and password: every session logs in."
        )
    kept = (
        ()
        if previous is None or edit.renew or previous.preset != vendor.id
        else previous.sessions[:count]
    )
    # The highest number this gateway ever minted: renewing counts on from it.
    after = (
        0
        if previous is None
        else max([previous.minted, *(_counter_of(item) for item in previous.sessions)])
    )
    minted, last = mint_sessions(
        vendor.template, count - len(kept), after, _all_sessions(sources)
    )
    gateway = GatewaySettings(
        preset=vendor.id,
        host=host,
        port=port,
        scheme=scheme,
        sessions=(*kept, *minted),
        secret=secret_id,
        zone=zone,
        zone_type=zone_type,
        country=_checked_gateway_country(edit.country),
        minutes=_checked_minutes(vendor, edit.minutes),
        minted=last,
    )
    target = source_id or next_vendor_source_id(
        sources, KIND_GATEWAY, edit.name or vendor.name
    )
    name = _source_name(stored, edit.name, vendor, KIND_GATEWAY, target)
    source = ProxySource(
        id=target,
        kind=KIND_GATEWAY,
        name=name,
        enabled=True,
        added_at=existing.added_at if existing is not None else stamp,
        gateway=gateway,
    )
    offers = _offers(chains, target, gateway_offer_rows(name, gateway, stored), stamp)
    return chains.with_source_offers(target, offers), stored.with_source(source), target


# ---------------------------------------------------------------- lists


@dataclass(frozen=True, slots=True)
class ListEdit:
    """The proxy list form. ``paste`` replaces the rows; ``url`` is write-only."""

    name: str = ""
    preset: str = ""
    scheme: str = "socks5h"
    paste: str = ""
    url: str = ""
    clear_url: bool = False
    fetch_via: str = ""
    refresh_hours: int = 0


def _row_url(scheme: str, row: ListRow) -> str:
    return f"{scheme}://{_userinfo(row.username, row.password)}{_authority(row.host, row.port)}"


def list_offer_rows(
    name: str, scheme: str, rows: Sequence[ListRow]
) -> list[tuple[str, str, str]]:
    return [
        (
            f"row:{row.key}",
            _row_url(scheme, row),
            f"{name} · {_authority(row.host, row.port)}",
        )
        for row in rows
    ]


def _rows_from_chains(
    chains: ProxyChains, entries: Sequence[ListEntry]
) -> list[ListRow]:
    """A list's rows rebuilt from its addresses' URLs (where the logins live)."""

    rows: list[ListRow] = []
    for entry in entries:
        endpoint = chains.endpoint(entry.proxy)
        if endpoint is None:
            continue
        try:
            parsed = urlsplit(endpoint.url)
            username = unquote(parsed.username or "")
            password = unquote(parsed.password or "")
        except ValueError:
            continue
        rows.append(ListRow(entry.host, entry.port, username, password))
    return rows


def _apply_rows(
    chains: ProxyChains,
    source_id: str,
    name: str,
    settings: ListSettings,
    rows: Sequence[ListRow],
    at: str,
) -> tuple[ProxyChains, ListSettings]:
    offers = _offers(
        chains, source_id, list_offer_rows(name, settings.scheme, rows), at
    )
    entries = tuple(
        ListEntry(
            host=row.host, port=row.port, proxy=proxy_id, login=bool(row.username)
        )
        for row, (proxy_id, _) in zip(rows, offers, strict=True)
    )
    return chains.with_source_offers(source_id, offers), replace(settings, rows=entries)


def save_list_source(
    chains: ProxyChains,
    sources: ProxySources,
    source_id: str,
    edit: ListEdit,
    *,
    at: str | None = None,
) -> tuple[ProxyChains, ProxySources, str]:
    """Create or change a proxy list, and offer its rows.

    Pasted rows are read at once. A download link is stored (as a secret --
    it carries the vendor's token) and fetched only by *Fetch now* or the
    schedule.
    """

    stamp = at or _now()
    existing = _existing(sources, source_id, KIND_LIST)
    vendor = preset(edit.preset)
    if vendor is not None and vendor.kind != PRESET_LIST:
        raise SourceEditError(f"{vendor.name} is not a proxy list preset.")
    scheme = _checked_scheme(edit.scheme)
    hours = _checked_hours(edit.refresh_hours)
    previous = existing.proxy_list if existing is not None else None
    stored = sources
    secret_id = previous.secret if previous is not None else ""
    shown_host = previous.url_host if previous is not None else ""
    if edit.clear_url:
        secret_id, shown_host = "", ""
    elif edit.url.strip():
        url = _checked_url(edit.url, "The list's download link")
        secret_id = secret_id or mint_secret_id(sources.secrets)
        stored = sources.with_secret(
            secret_id, SourceSecret(type=SECRET_PASSWORD, password=url)
        )
        shown_host = url_host(url)
    if secret_id and stored.secret(secret_id) is None:
        secret_id, shown_host = "", ""
    pasted = parse_proxy_list(edit.paste) if edit.paste.strip() else ()
    if edit.paste.strip() and not pasted:
        raise SourceEditError(
            "Nothing in that text reads as ip:port:username:password (or "
            "ip:port), one per line."
        )
    if not pasted and not secret_id and not (previous and previous.rows):
        raise SourceEditError(
            "Paste the list (ip:port:username:password, one per line), or give "
            "its download link."
        )
    target = source_id or next_vendor_source_id(
        sources, KIND_LIST, edit.name or (vendor.name if vendor else "")
    )
    name = _source_name(stored, edit.name, vendor, KIND_LIST, target)
    fetch = previous.fetch if previous is not None else FetchState()
    settings = ListSettings(
        scheme=scheme,
        parser=READER_PROXY_LIST,
        preset=vendor.id
        if vendor is not None
        else (previous.preset if previous else ""),
        secret=secret_id,
        url_host=shown_host,
        fetch=replace(fetch, via=edit.fetch_via.strip().lower(), refresh_hours=hours),
        rows=previous.rows if previous is not None else (),
    )
    # Pasted rows replace the list; otherwise the rows it has are rebuilt from
    # their addresses' URLs (where the logins live) under the new scheme.
    rows: Sequence[ListRow] = pasted or _rows_from_chains(chains, settings.rows)
    chains_now, settings = _apply_rows(chains, target, name, settings, rows, stamp)
    source = ProxySource(
        id=target,
        kind=KIND_LIST,
        name=name,
        enabled=True,
        added_at=existing.added_at if existing is not None else stamp,
        proxy_list=settings,
    )
    return chains_now, stored.with_source(source), target


# ---------------------------------------------------------------- fetch


@dataclass(frozen=True, slots=True)
class FetchPlan:
    """What a fetch of one source asks for, and which reader reads it."""

    source_id: str
    url: str
    reader: str
    via: str


def fetch_plan(source: ProxySource, sources: ProxySources) -> FetchPlan | None:
    """The vendor URL a source's *Fetch now* reads, or ``None`` if it has none."""

    if source.account is not None and source.account.host_list is not None:
        host_list = source.account.host_list
        return FetchPlan(
            source.id, host_list.url, host_list.parser, host_list.fetch.via
        )
    if source.proxy_list is not None and source.proxy_list.secret:
        secret = sources.secret(source.proxy_list.secret)
        if secret is None or not secret.password:
            return None
        return FetchPlan(
            source.id, secret.password, READER_PROXY_LIST, source.proxy_list.fetch.via
        )
    return None


@dataclass(frozen=True, slots=True)
class FetchedText:
    """A vendor's answer, or why there is none. Never carries the URL."""

    ok: bool
    text: str = ""
    note: str = ""


async def fetch_vendor_text(
    url: str,
    *,
    proxy: str | None,
    timeout: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> FetchedText:
    """One GET of the URL the operator confirmed. Never raises, never logs it.

    Strict TLS (an ordinary client, nothing said about trust), no redirects
    -- MCC fetches the URL that was confirmed and nothing it points at -- and
    at most :data:`~my_claude_code.config.proxy_feeds.FEED_MAX_BYTES` read.
    ``proxy`` is the exit the fetch leaves through (``None`` or ``""``: this
    computer). Errors are reported by type: a message could quote the URL,
    and a download link carries the vendor's token.
    """

    if not is_valid_feed_url(url):
        return FetchedText(ok=False, note="the stored URL is not an https:// URL")
    client = httpx.AsyncClient(
        proxy=proxy or None,
        timeout=timeout,
        follow_redirects=False,
        headers={"user-agent": FEED_USER_AGENT},
        transport=transport,
    )
    try:
        async with client.stream("GET", url) as response:
            if response.status_code >= 300:
                return FetchedText(ok=False, note=f"answered {response.status_code}")
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                size += len(chunk)
                if size >= FEED_MAX_BYTES:
                    break
            body = b"".join(chunks)[:FEED_MAX_BYTES]
    except Exception as exc:
        return FetchedText(ok=False, note=f"did not answer ({type(exc).__name__})")
    finally:
        await client.aclose()
    return FetchedText(ok=True, text=body.decode("utf-8", "replace"))


@dataclass(frozen=True, slots=True)
class FetchOutcome:
    """What one fetch did, in a sentence for the page and the log."""

    source_id: str
    ok: bool
    sentence: str
    offered: int = 0


def _failed(source: ProxySource, fetch: FetchState, note: str, at: str) -> ProxySource:
    state = replace(fetch, fetched_at=at, ok=False, note=note[:300])
    if source.account is not None and source.account.host_list is not None:
        host_list = replace(source.account.host_list, fetch=state)
        return replace(source, account=replace(source.account, host_list=host_list))
    if source.proxy_list is not None:
        return replace(source, proxy_list=replace(source.proxy_list, fetch=state))
    return source


def record_fetch_failure(
    sources: ProxySources, source_id: str, note: str, *, at: str | None = None
) -> tuple[ProxySources, FetchOutcome]:
    """Note on the source that a fetch did not happen or did not work."""

    source = sources.source(source_id)
    stamp = at or _now()
    sentence = f"Not fetched: {note}"
    if source is None:
        return sources, FetchOutcome(source_id, False, sentence)
    fetch = _fetch_state(source)
    if fetch is None:
        return sources, FetchOutcome(source_id, False, sentence)
    return sources.with_source(_failed(source, fetch, sentence, stamp)), FetchOutcome(
        source_id, False, sentence
    )


def _fetch_state(source: ProxySource) -> FetchState | None:
    if source.account is not None and source.account.host_list is not None:
        return source.account.host_list.fetch
    if source.proxy_list is not None:
        return source.proxy_list.fetch
    return None


def apply_fetched(
    chains: ProxyChains,
    sources: ProxySources,
    source_id: str,
    fetched: FetchedText,
    *,
    at: str | None = None,
) -> tuple[ProxyChains, ProxySources, FetchOutcome]:
    """Read a vendor's answer into the source's offers. Off the event loop.

    The source is read again from ``sources`` -- the store as it is now, not
    as it was when the fetch began -- so an edit made meanwhile is kept and a
    source removed meanwhile is left removed. An answer nothing in which
    parses leaves the offers as they were and says so.
    """

    stamp = at or _now()
    source = sources.source(source_id)
    if source is None or source.vendor is None:
        return (
            chains,
            sources,
            FetchOutcome(source_id, False, "Not fetched: the source was removed."),
        )
    fetch = _fetch_state(source)
    if fetch is None:
        return (
            chains,
            sources,
            FetchOutcome(source_id, False, "Not fetched: nothing to fetch."),
        )
    if not fetched.ok:
        sentence = f"Not fetched: the vendor {fetched.note}."
        return (
            chains,
            sources.with_source(_failed(source, fetch, sentence, stamp)),
            FetchOutcome(source_id, False, sentence),
        )
    if source.account is not None and source.account.host_list is not None:
        return _apply_server_list(
            chains, sources, source, source.account, fetched.text, stamp
        )
    if source.proxy_list is not None:
        return _apply_list_text(
            chains, sources, source, source.proxy_list, fetched.text, stamp
        )
    return (
        chains,
        sources,
        FetchOutcome(source_id, False, "Not fetched: nothing to fetch."),
    )


def _apply_server_list(
    chains: ProxyChains,
    sources: ProxySources,
    source: ProxySource,
    account: AccountSettings,
    text: str,
    at: str,
) -> tuple[ProxyChains, ProxySources, FetchOutcome]:
    host_list = account.host_list
    assert host_list is not None
    listed = parse_nordvpn_servers(text)
    if not listed:
        sentence = (
            "Fetched, but nothing in the answer reads as NordVPN's server list "
            "-- the list may have changed shape. The offers are unchanged."
        )
        return (
            chains,
            sources.with_source(_failed(source, host_list.fetch, sentence, at)),
            FetchOutcome(source.id, False, sentence),
        )
    found = tuple(sorted({item.country for item in listed if item.country}))
    kept = [
        item
        for item in listed
        if not host_list.countries or item.country in host_list.countries
    ]
    sentence = (
        f"Fetched: {len(listed)} SOCKS5 server{'s' if len(listed) != 1 else ''}"
        + (f" ({', '.join(found)})" if found else "")
        + (
            f"; {len(kept)} kept for {', '.join(host_list.countries)}."
            if host_list.countries
            else "."
        )
    )
    updated = replace(
        account,
        host_list=replace(
            host_list,
            hosts=listed,
            found=found,
            fetch=replace(host_list.fetch, fetched_at=at, ok=True, note=sentence),
        ),
    )
    offers = _offers(
        chains, source.id, account_offer_rows(source.name, updated, sources), at
    )
    return (
        chains.with_source_offers(source.id, offers),
        sources.with_source(replace(source, account=updated)),
        FetchOutcome(source.id, True, sentence, len(offers)),
    )


def _apply_list_text(
    chains: ProxyChains,
    sources: ProxySources,
    source: ProxySource,
    settings: ListSettings,
    text: str,
    at: str,
) -> tuple[ProxyChains, ProxySources, FetchOutcome]:
    rows = parse_proxy_list(text)
    if not rows:
        sentence = (
            "Fetched, but nothing in the answer reads as ip:port:username:password "
            "lines. The offers are unchanged."
        )
        return (
            chains,
            sources.with_source(_failed(source, settings.fetch, sentence, at)),
            FetchOutcome(source.id, False, sentence),
        )
    sentence = f"Fetched: {len(rows)} prox{'ies' if len(rows) != 1 else 'y'}."
    chains_now, updated = _apply_rows(
        chains,
        source.id,
        source.name,
        replace(
            settings,
            fetch=replace(settings.fetch, fetched_at=at, ok=True, note=sentence),
        ),
        rows,
        at,
    )
    return (
        chains_now,
        sources.with_source(replace(source, proxy_list=updated)),
        FetchOutcome(source.id, True, sentence, len(rows)),
    )


# ------------------------------------------------------------- schedule


def _parsed_time(stamp: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def scheduled_sources(sources: ProxySources) -> list[tuple[str, int, str]]:
    """``(source id, hours, last fetch)`` of every source on a schedule."""

    found: list[tuple[str, int, str]] = []
    for source in sources.sources.values():
        if source.kind not in VENDOR_KINDS or not source.enabled:
            continue
        if fetch_plan(source, sources) is None:
            continue
        fetch = _fetch_state(source)
        if fetch is not None and fetch.refresh_hours > 0:
            found.append((source.id, fetch.refresh_hours, fetch.fetched_at))
    return found


def seconds_until_due(sources: ProxySources, now: datetime) -> float | None:
    """How long until the next scheduled fetch; ``None``: nothing is scheduled.

    A source never fetched is due at once: switching a schedule on is asking
    for a fetch.
    """

    waits: list[float] = []
    for _, hours, fetched_at in scheduled_sources(sources):
        last = _parsed_time(fetched_at) if fetched_at else None
        if last is None:
            waits.append(0.0)
            continue
        waits.append(max(0.0, (last + timedelta(hours=hours) - now).total_seconds()))
    return min(waits) if waits else None


def due_source_ids(sources: ProxySources, now: datetime) -> list[str]:
    due: list[str] = []
    for source_id, hours, fetched_at in scheduled_sources(sources):
        last = _parsed_time(fetched_at) if fetched_at else None
        if last is None or last + timedelta(hours=hours) <= now:
            due.append(source_id)
    return due


class SourceSchedule:
    """The one hook between a source save and the schedule's loop.

    The runtime attaches its loop's re-arm; a save that may change a schedule
    calls :meth:`changed`, so a schedule switched on starts counting at once.
    With nothing attached (tests, an app built without a runtime) it is a
    no-op.
    """

    def __init__(self) -> None:
        self._wake: Callable[[], object] | None = None

    def attach(self, wake: Callable[[], object]) -> None:
        self._wake = wake

    def detach(self) -> None:
        self._wake = None

    def changed(self) -> None:
        if self._wake is not None:
            self._wake()


SOURCE_SCHEDULE = SourceSchedule()


class FetchInFlight:
    """One fetch per source at a time: the button and the schedule share it."""

    def __init__(self) -> None:
        self._running: set[str] = set()

    def claim(self, source_id: str) -> bool:
        if source_id in self._running:
            return False
        self._running.add(source_id)
        return True

    def release(self, source_id: str) -> None:
        self._running.discard(source_id)

    def clear(self) -> None:
        self._running.clear()


FETCHES_IN_FLIGHT = FetchInFlight()


def reset_vendor_source_state() -> None:
    """For tests: forget the schedule hook and any fetch in flight."""

    SOURCE_SCHEDULE.detach()
    FETCHES_IN_FLIGHT.clear()


__all__ = [
    "FETCHES_IN_FLIGHT",
    "SOURCE_SCHEDULE",
    "AccountEdit",
    "FetchOutcome",
    "FetchPlan",
    "FetchedText",
    "GatewayEdit",
    "ListEdit",
    "SourceSchedule",
    "account_offer_rows",
    "apply_fetched",
    "due_source_ids",
    "fetch_plan",
    "fetch_vendor_text",
    "gateway_login",
    "gateway_offer_rows",
    "list_offer_rows",
    "mint_sessions",
    "next_vendor_source_id",
    "record_fetch_failure",
    "reset_vendor_source_state",
    "save_account_source",
    "save_gateway_source",
    "save_list_source",
    "scheduled_sources",
    "seconds_until_due",
    "unique_source_name",
    "url_host",
    "vendor_offer_id",
]
