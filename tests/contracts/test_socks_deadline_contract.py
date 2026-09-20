"""The private shapes ``socks_deadline`` reads, pinned so an upgrade says so.

``bound_socks_handshake`` reaches four names that belong to ``httpx`` and
``httpcore`` rather than to this repo: ``AsyncClient._transport``,
``AsyncClient._mounts``, ``AsyncHTTPTransport._pool`` and
``AsyncSOCKSProxy._network_backend``. Every one of them is read with
``getattr``, so a rename would not raise -- it would silently do nothing, and
the unbounded handshake would be back with no test failing.

That is the failure mode this file exists to prevent. It also pins the two
library facts the fix is an answer to: that ``_init_socks5_connection`` still
takes no timeout, and that the backend's ``read`` still defaults to ``None``.
When a future ``httpcore`` closes the hole itself, these tests are where that
shows up, and the wrapper can then be reconsidered rather than carried
silently.
"""

import inspect

import httpcore
import httpx
import pytest
from httpcore._async import socks_proxy

from my_claude_code.providers.socks_deadline import (
    POOL_BACKEND_ATTR,
    TRANSPORT_POOL_ATTR,
    bound_socks_handshake,
)

#: The versions this module's reasoning was read off. A bump is not a failure
#: in itself -- it is a prompt to re-read ``socks_proxy.py`` and this file.
PINNED_HTTPCORE = "1.0.9"
PINNED_HTTPX = "0.28.1"


def test_the_pinned_versions_are_the_ones_installed():
    assert httpcore.__version__ == PINNED_HTTPCORE
    assert httpx.__version__ == PINNED_HTTPX


def test_the_socks5_handshake_still_takes_no_timeout():
    """The defect itself, asserted rather than described.

    If this ever fails because a ``timeout`` parameter appeared, ``httpcore``
    has grown its own bound and the wrapper is belt and braces. If it fails
    because the function is gone, the wrapper's premise has changed and the
    module must be re-read before the next release.
    """

    parameters = inspect.signature(socks_proxy._init_socks5_connection).parameters

    assert set(parameters) == {"stream", "host", "port", "auth"}
    assert "timeout" not in parameters


def test_an_unbounded_stream_read_is_what_that_means():
    """The other half: a read with no timeout has no deadline at all."""

    read = inspect.signature(httpcore.AsyncNetworkStream.read).parameters

    assert read["timeout"].default is None


def test_the_attribute_names_the_module_reads_are_the_ones_that_exist():
    """Each private name, on a real object built the way MCC builds it."""

    client = httpx.AsyncClient(proxy="socks5://127.0.0.1:9")

    assert hasattr(client, "_transport")
    assert hasattr(client, "_mounts") and client._mounts

    pools = [
        pool
        for transport in client._mounts.values()
        if (pool := getattr(transport, TRANSPORT_POOL_ATTR, None)) is not None
    ]

    assert pools, f"no transport exposed {TRANSPORT_POOL_ATTR!r}"
    assert any(isinstance(pool, httpcore.AsyncSOCKSProxy) for pool in pools)
    for pool in pools:
        assert hasattr(pool, POOL_BACKEND_ATTR)


@pytest.mark.parametrize("scheme", ["socks5", "socks5h"])
def test_both_socks_schemes_are_recognised_and_bounded(scheme):
    """``socks5h`` is what the operator's own chain file stores."""

    from my_claude_code.providers.socks_deadline import _HandshakeDeadlineBackend

    client = bound_socks_handshake(httpx.AsyncClient(proxy=f"{scheme}://127.0.0.1:9"))
    wrapped = [
        pool
        for transport in client._mounts.values()
        if isinstance(
            pool := getattr(transport, TRANSPORT_POOL_ATTR, None),
            httpcore.AsyncSOCKSProxy,
        )
        and isinstance(getattr(pool, POOL_BACKEND_ATTR), _HandshakeDeadlineBackend)
    ]

    assert len(wrapped) == 1


def test_binding_twice_wraps_once():
    """Construction sites may be called from more than one path."""

    from my_claude_code.providers.socks_deadline import _HandshakeDeadlineBackend

    client = httpx.AsyncClient(proxy="socks5://127.0.0.1:9")
    bound_socks_handshake(client)
    pool = next(
        pool
        for transport in client._mounts.values()
        if isinstance(
            pool := getattr(transport, TRANSPORT_POOL_ATTR, None),
            httpcore.AsyncSOCKSProxy,
        )
    )
    once = getattr(pool, POOL_BACKEND_ATTR)
    bound_socks_handshake(client)

    assert getattr(pool, POOL_BACKEND_ATTR) is once
    assert isinstance(once, _HandshakeDeadlineBackend)
    assert not isinstance(once._backend, _HandshakeDeadlineBackend)


def test_the_module_names_no_tls_argument():
    """The security control, restated where this module can be read with it.

    ``tests/contracts/test_tls_verification_is_never_weakened.py`` enforces
    this across the whole of ``src``; it is repeated here because a transport
    shim is exactly the kind of file where a ``verify`` argument would look
    reasonable. It is never reasonable: trust stays the library's default.
    """

    from pathlib import Path

    from my_claude_code.providers import socks_deadline

    source = Path(socks_deadline.__file__).read_text(encoding="utf-8")

    for forbidden in ("verify", "CERT_NONE", "check_hostname", "trust_env"):
        assert forbidden not in source
