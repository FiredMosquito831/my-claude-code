"""The suite can never reach an OAuth token host. Not by mistake, not ever.

Anthropic's and OpenAI's token endpoints rotate single-use refresh tokens and
rate-limit hard. One un-mocked test that reached ``platform.claude.com`` with
a real refresh token would do to the developer's Claude Code login exactly
what 7.69.1 exists to stop MCC doing. The filesystem guard in
:mod:`tests.support.hermetic` keeps tests out of ``~/.claude``; this keeps them
off the wire to the hosts that could spend what is in it.

The block sits **below** httpx: it wraps ``socket.getaddrinfo`` and
``socket.create_connection``, which every real connection -- sync or async,
through httpx, urllib or a raw socket -- has to call to reach a host by name.
``httpx.MockTransport`` and a monkeypatched ``_post_refresh`` never touch a
socket, so they keep working. A proxy would move the name lookup to the
proxy, which is why :func:`tests.support.hermetic.isolate_the_machine` also
drops the proxy environment variables.

It is installed from ``pytest_configure`` for the whole session and as the
first statement of every child-process script the out-of-process tests start.
This module imports nothing but the standard library, so a child can install
it before anything else is imported.
"""

import socket
from typing import Any

#: Every host that issues or renews an OAuth token MCC could hold. Blocking
#: all of ``chatgpt.com`` covers its ``/backend-api`` token paths too.
BLOCKED_TOKEN_HOSTS: tuple[str, ...] = (
    "platform.claude.com",
    "console.anthropic.com",
    "claude.ai",
    "claude.com",
    "auth.openai.com",
    "chatgpt.com",
)


class HermeticityViolation(BaseException):
    """A test tried to touch the real machine.

    Derived from ``BaseException`` so that no ``except Exception`` in the
    application -- and there are many, including the one wrapped around the
    harness-catalogue write that damaged the developer's ``~/.fcc`` -- can
    swallow it into a warning and leave the test green.
    """


_installed = False
_hits: list[str] = []


def is_blocked_host(host: object) -> bool:
    """Whether ``host`` is one of the token hosts, or a subdomain of one."""

    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if not isinstance(host, str):
        return False
    name = host.strip().rstrip(".").lower()
    return any(
        name == blocked or name.endswith("." + blocked)
        for blocked in BLOCKED_TOKEN_HOSTS
    )


def _refuse(host: object, operation: str) -> None:
    _hits.append(str(host))
    raise HermeticityViolation(
        f"HERMETICITY VIOLATION: {operation} to OAuth token host {host!r} "
        "refused.\n"
        "  The test suite may never reach a token endpoint: a real refresh "
        "rotates the developer's single-use refresh token and logs their own "
        "client out.\n"
        "  Mock the exchange (httpx.MockTransport, or monkeypatch "
        "_post_refresh / _refresh_access_token) instead.\n"
        "  See tests/support/token_host_block.py."
    )


def install_token_host_block() -> None:
    """Install the block for this process. Idempotent."""

    global _installed
    if _installed:
        return
    _installed = True
    real_getaddrinfo = socket.getaddrinfo
    real_create_connection = socket.create_connection

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if is_blocked_host(host):
            _refuse(host, "name lookup")
        return real_getaddrinfo(host, *args, **kwargs)

    def guarded_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(address, tuple) and address and is_blocked_host(address[0]):
            _refuse(address[0], "connection")
        return real_create_connection(address, *args, **kwargs)

    _replace(socket, "getaddrinfo", guarded_getaddrinfo)
    _replace(socket, "create_connection", guarded_create_connection)


def _replace(owner: Any, name: str, value: Any) -> None:
    # One deliberately untyped seam, for the reason ``hermetic._intercept``
    # gives: no wrapper forwarding ``*args`` matches typeshed's overloads.
    setattr(owner, name, value)


def block_is_installed() -> bool:
    """Whether this process has the block (the tests assert it)."""

    return _installed and getattr(socket.getaddrinfo, "__name__", "") == (
        "guarded_getaddrinfo"
    )


def refused_hosts() -> tuple[str, ...]:
    """The hosts refused so far in this process, for the self-test."""

    return tuple(_hits)
