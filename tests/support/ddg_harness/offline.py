"""A network guard for the harness's command-line scripts.

Under pytest the suite's own network gate (``tests/support/hermetic.py``)
already refuses every non-loopback lookup and connection. The scripts
(``baseline``, ``table``) run outside pytest, so they install this smaller
guard for their whole run: a name lookup of anything but a loopback name or an
IP literal, a connection to anything but loopback, and a ``ddgs``/``primp``
request to a non-loopback host all raise ``OfflineViolation``. Nothing in the
harness should ever reach one of them -- the fakes serve every request -- so a
raise is a harness bug, caught before it becomes a live call.
"""

import asyncio.proactor_events
import contextlib
import ipaddress
import socket
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import pytest
from ddgs import http_client as ddgs_http_client


class OfflineViolation(BaseException):
    """A harness script tried to reach past this machine (never caught by ``except Exception``)."""


def _local(host: object) -> bool:
    if host is None:
        return True
    if isinstance(host, bytes | bytearray):
        host = bytes(host).decode("ascii", "ignore")
    if not isinstance(host, str):
        return False
    name = host.strip().strip("[]").rstrip(".").lower()
    if name in {"", "localhost"} or name.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def _literal(host: object) -> bool:
    if not isinstance(host, str):
        return False
    try:
        ipaddress.ip_address(host.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    return True


def _refuse(what: str, target: object) -> None:
    raise OfflineViolation(f"DDG harness: {what} {target!r} refused (offline only)")


@contextlib.contextmanager
def guard() -> Iterator[None]:
    real_getaddrinfo = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    proactor = asyncio.proactor_events.BaseProactorEventLoop
    real_sock_connect = proactor.sock_connect
    real_ddgs_request = ddgs_http_client.HttpClient.request

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not (_local(host) or _literal(host)):
            _refuse("name lookup of", host)
        return real_getaddrinfo(host, *args, **kwargs)

    def connect(self: socket.socket, address: Any) -> None:
        if isinstance(address, tuple) and address and not _local(address[0]):
            _refuse("connection to", address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        if isinstance(address, tuple) and address and not _local(address[0]):
            _refuse("connection to", address)
        return real_connect_ex(self, address)

    async def sock_connect(self: Any, sock: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple) and address and not _local(address[0]):
            _refuse("connection to", address)
        return await real_sock_connect(self, sock, address)

    def ddgs_request(self: Any, *args: Any, **kwargs: Any) -> Any:
        url = kwargs.get("url", args[1] if len(args) > 1 else "")
        if not _local(urlsplit(str(url)).hostname):
            _refuse("ddgs (primp) request to", str(url))
        return real_ddgs_request(self, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(socket, "getaddrinfo", getaddrinfo)
        patch.setattr(socket.socket, "connect", connect)
        patch.setattr(socket.socket, "connect_ex", connect_ex)
        patch.setattr(proactor, "sock_connect", sock_connect)
        patch.setattr(ddgs_http_client.HttpClient, "request", ddgs_request)
        yield
