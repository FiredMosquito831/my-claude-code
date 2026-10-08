"""Which system proxy, if any, a Direct dial to a provider really leaves through.

MCC never sets ``trust_env`` on a client, and httpx defaults it to ``True``: a
client built with **no** proxy of its own -- every Direct leg of a chain, the
Direct fallback, and every provider with no proxy at all -- follows the
operating system's proxy (``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``ALL_PROXY`` and
``NO_PROXY``, else the Windows or macOS setting ``urllib`` reads). A VPN app or a
corporate proxy can therefore carry a request the log calls ``direct``.

:func:`system_proxy_for` answers the question httpx itself answers when it picks
a transport for a URL, by the same rules, so the request log and the Proxying
page can say ``direct via system proxy host:port`` (7.79.2) instead of a claim
that is not true. It reads only; it never changes what any client does.

The rules are httpx 0.28's ``_utils.get_environment_proxies`` and
``URLPattern``, restated here rather than imported from a private module (and
in ``config``, which the dashboard routes may import, with ``urllib`` parsing in
place of ``httpx.URL``); ``tests/config/test_system_proxy.py`` holds the two
together on a grid of environments.
"""

import ipaddress
import re
from dataclasses import dataclass
from urllib.parse import urlsplit
from urllib.request import getproxies

from my_claude_code.config.credentials import mask_proxy_label

#: The ports httpx's URL leaves out (``URL.port`` is ``None`` for them), so a
#: pattern or a target written with one matches one written without it.
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


@dataclass(frozen=True, slots=True)
class _Target:
    scheme: str
    host: str
    port: int | None


def _split(url: str) -> _Target | None:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if port is not None and _DEFAULT_PORTS.get(scheme) == port:
        port = None
    return _Target(scheme=scheme, host=host, port=port)


@dataclass(frozen=True, slots=True)
class _Mount:
    """One proxy key, as httpx reads it: a scheme, a host pattern and a port."""

    scheme: str
    host: str
    host_regex: re.Pattern[str] | None
    port: int | None
    #: The proxy URL, or ``None`` for a ``NO_PROXY`` entry (no proxy here).
    target: str | None

    @property
    def priority(self) -> tuple[int, int, int]:
        # More specific first: a port, then a longer host, then a longer scheme.
        return (
            0 if self.port is not None else 1,
            -len(self.host),
            -len(self.scheme),
        )

    def matches(self, url: _Target) -> bool:
        if self.scheme and self.scheme != url.scheme:
            return False
        if (
            self.host
            and self.host_regex is not None
            and not self.host_regex.match(url.host)
        ):
            return False
        return not (self.port is not None and self.port != url.port)


def _mount(pattern: str, target: str | None) -> _Mount | None:
    if ":" not in pattern:
        return None
    parsed = _split(pattern)
    if parsed is None:
        return None
    scheme = "" if parsed.scheme == "all" else parsed.scheme
    host = "" if parsed.host == "*" else parsed.host
    regex: re.Pattern[str] | None
    if not host:
        regex = None
    elif host.startswith("*."):
        # *.example.com matches www.example.com, not example.com.
        regex = re.compile(f"^.+\\.{re.escape(host[2:])}$")
    elif host.startswith("*"):
        # *example.com matches www.example.com and example.com.
        regex = re.compile(f"^(.+\\.)?{re.escape(host[1:])}$")
    else:
        regex = re.compile(f"^{re.escape(host)}$")
    return _Mount(scheme, host, regex, parsed.port, target)


def _is_ipv4(hostname: str) -> bool:
    try:
        ipaddress.IPv4Address(hostname.split("/")[0])
    except ValueError:
        return False
    return True


def _is_ipv6(hostname: str) -> bool:
    try:
        ipaddress.IPv6Address(hostname.split("/")[0])
    except ValueError:
        return False
    return True


def _environment_mounts(proxies: dict[str, str]) -> list[_Mount]:
    """httpx's ``get_environment_proxies``, as mounts, most specific first."""

    raw: dict[str, str | None] = {}
    for scheme in ("http", "https", "all"):
        if proxies.get(scheme):
            hostname = proxies[scheme]
            raw[f"{scheme}://"] = (
                hostname if "://" in hostname else f"http://{hostname}"
            )
    for hostname in (host.strip() for host in proxies.get("no", "").split(",")):
        if hostname == "*":
            return []
        if not hostname:
            continue
        if "://" in hostname:
            raw[hostname] = None
        elif _is_ipv4(hostname):
            raw[f"all://{hostname}"] = None
        elif _is_ipv6(hostname):
            raw[f"all://[{hostname}]"] = None
        elif hostname.lower() == "localhost":
            raw[f"all://{hostname}"] = None
        else:
            raw[f"all://*{hostname}"] = None
    mounts = [_mount(pattern, target) for pattern, target in raw.items()]
    return sorted(
        (mount for mount in mounts if mount is not None),
        key=lambda mount: mount.priority,
    )


def system_proxy_url_for(
    url: str, *, proxies: dict[str, str] | None = None
) -> str | None:
    """The proxy URL a no-proxy httpx client would use for ``url``, or ``None``.

    ``proxies`` is ``urllib.request.getproxies()`` unless a caller (a test)
    hands one in. Never raises: anything unreadable is "no system proxy".
    """

    try:
        target = _split(url)
        found = getproxies() if proxies is None else proxies
    except Exception:
        return None
    if target is None or not target.host:
        return None
    for mount in _environment_mounts(dict(found)):
        if mount.matches(target):
            return mount.target
    return None


def system_proxy_for(url: str, *, proxies: dict[str, str] | None = None) -> str:
    """The masked ``host:port`` of the system proxy carrying ``url``, else ``""``.

    The only form that may reach the log or the page: any ``user:pass`` is
    removed by :func:`~my_claude_code.config.credentials.mask_proxy_label`.
    """

    found = system_proxy_url_for(url, proxies=proxies)
    return mask_proxy_label(found) if found else ""


__all__ = ["system_proxy_for", "system_proxy_url_for"]
