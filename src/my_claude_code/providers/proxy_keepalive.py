"""Keep a proxied leg's idle connections open for the next request (7.94.0).

What was wrong
--------------

``httpx`` closes a pooled connection that has been idle for **5 seconds**, and
no client MCC builds ever said otherwise. For a provider with no proxy that is
harmless: a new connection to an origin costs a TCP and a TLS handshake. For a
leg of a proxy chain it is the most expensive thing a request does. Every
request that arrives more than five seconds after the last one through the
same exit opens a new connection to the proxy, runs the tunnel handshake
(``CONNECT`` or SOCKS5) and then the TLS handshake through it, and through a
free proxy that measured 2.8-51.9 s (``specs/INVESTIGATION-ZEN-IDENTITY-AND-
PROXY-VS-OPENCODE-LITE.md`` §3.3, point 3). A coding session's turns are
rarely five seconds apart, so nearly every proxied request paid it.

What this does
--------------

``PROXY_KEEPALIVE_SECONDS`` arrives on a proxied leg's ``ProviderConfig`` as
``http_keepalive_expiry``, handed over by the chain fan-out on the same line
as the leg's connect timeout. Every client constructor that builds from a
leg's configuration passes ``**leg_pool_kwargs(config.http_keepalive_expiry)``.

* ``None`` or ``0`` -- every unproxied provider, every direct rung, the Direct
  fallback, a single static proxy, and every leg while the setting is 0 --
  returns ``{}``. The constructor is called with exactly the arguments it had
  before this module existed; nothing is restated as an explicit default.
* A number returns ``{"limits": httpx.Limits(...)}`` with the client's own
  pool sizes and only the keep-alive changed. The sizes are read from the
  library that owns them -- ``httpx.AsyncClient``'s default for a bare client,
  the OpenAI SDK's ``DEFAULT_CONNECTION_LIMITS`` for the SDK's client -- so a
  proxied leg's pool is never smaller or larger than an unproxied one's.

Nothing about trust, timeouts, redirects or the proxy itself is touched. The
SOCKS5 handshake bound and the dial clock wrap the pool's network backend
after construction and do not read the limits.
"""

import inspect
from typing import Any

import httpx


def _client_default_limits() -> httpx.Limits:
    """What ``httpx.AsyncClient()`` builds its pool with when told nothing."""

    default = inspect.signature(httpx.AsyncClient).parameters["limits"].default
    if not isinstance(default, httpx.Limits):  # pragma: no cover - library change
        raise TypeError("httpx.AsyncClient no longer declares a default Limits")
    return default


#: ``httpx.AsyncClient``'s own default pool, read from its signature rather
#: than restated: 100 connections, 20 kept alive, idle ones closed after 5 s.
HTTPX_DEFAULT_LIMITS: httpx.Limits = _client_default_limits()


def proxied_leg_keepalive(settings: object) -> float | None:
    """``PROXY_KEEPALIVE_SECONDS`` as a proxied leg's ``http_keepalive_expiry``.

    ``None`` for 0 -- what ships -- and for anything that is not a number, so
    a leg built while the setting is off carries exactly the configuration it
    always did.
    """

    value = getattr(settings, "proxy_keepalive_seconds", None)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return float(value)


def leg_pool_kwargs(
    keepalive_expiry: float | None,
    *,
    base: httpx.Limits = HTTPX_DEFAULT_LIMITS,
) -> dict[str, Any]:
    """The extra constructor arguments a client of this configuration gets.

    ``{}`` unless ``keepalive_expiry`` is a positive number, so every client
    that is not a proxied leg with the setting on is built exactly as before.
    ``base`` is the pool the client would have had: its connection counts are
    kept, and only how long an idle connection lives changes.
    """

    if keepalive_expiry is None or keepalive_expiry <= 0:
        return {}
    return {
        "limits": httpx.Limits(
            max_connections=base.max_connections,
            max_keepalive_connections=base.max_keepalive_connections,
            keepalive_expiry=float(keepalive_expiry),
        )
    }


__all__ = ["HTTPX_DEFAULT_LIMITS", "leg_pool_kwargs", "proxied_leg_keepalive"]
