"""The rate limiter one *proxied* leg of a chain gets, and only that leg.

Why this exists
---------------

``PROVIDER_RETRY_ATTEMPTS`` runs its ladder *inside* one provider, which is
below the proxy pool. So when a chain dials an address that is simply dead,
the leg dials it, waits out the connect timeout, sleeps the backoff, and dials
**the same dead address again** before the pool upstairs is ever told. Measured
on this operator's install: 2 x 21 s per dead entry, and the pool saw one
failure after 42 seconds instead of one after 21.

Retrying a connect failure on the same address is the one case where a second
knock cannot help and a *different address* obviously can -- and a different
address is exactly what the frame above this one is for. So a proxied leg
surfaces a connect-class failure to the pool on the first dial.

What it deliberately does not do
--------------------------------

* It does not change the retry ladder. ``providers/rate_limit.py`` is not
  edited, not subclassed around, and not reached by a different path: the base
  ``execute_with_retry`` runs, records its try and takes every decision it
  takes today.
* It does not touch **status** retries. A ``429`` or a ``5xx`` through a proxy
  is the origin answering, the address is demonstrably alive, and the leg
  retries it exactly as it always has.
* It does not exist at all for an unproxied provider. The factory hands this
  class only to a leg that has an address, so a provider with no chain
  configured is constructed from the same line, with the same class, as in
  every release before this one.

How
---

The base loop's every decision hangs off the exception the callable raised. So
the callable is wrapped, and the *second* invocation after a connect-class
failure raises a sentinel derived from ``BaseException`` -- which the base's
``except Exception`` cannot catch, so it leaves the loop untouched, without a
second dial and without a second recorded try. This class catches it outside
the loop and re-raises the original error object, so everything above sees the
identical exception it would have seen.
"""

from collections.abc import Callable
from typing import Any

from my_claude_code.providers.rate_limit import ProviderRateLimiter

from .proxy_rotating import proxy_reachability_failure


class _ProxyConnectStop(BaseException):
    """Carries the original connect failure past the retry loop, once.

    ``BaseException`` and not ``Exception`` on purpose: that is what makes the
    base retry loop's ``except Exception`` decline it, which is the whole
    mechanism. It never escapes this module -- :meth:`
    ProxiedLegRateLimiter.execute_with_retry` catches it and re-raises the
    error it carries.
    """

    def __init__(self, error: BaseException) -> None:
        super().__init__(str(error))
        self.error = error


class ProxiedLegRateLimiter(ProviderRateLimiter):
    """A leg's limiter that will not dial the same dead address twice."""

    async def execute_with_retry(
        self, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        stopped: list[BaseException] = []

        async def guarded(*call_args: Any, **call_kwargs: Any) -> Any:
            if stopped:
                raise _ProxyConnectStop(stopped[0])
            try:
                return await fn(*call_args, **call_kwargs)
            except Exception as error:
                # The same question the pool upstairs asks, asked with the
                # same function, so the two can never drift apart: is this
                # the *address* failing?
                if proxy_reachability_failure(error, proxied=True) is not None:
                    stopped.append(error)
                raise

        carried: BaseException | None = None
        try:
            return await super().execute_with_retry(guarded, *args, **kwargs)
        except _ProxyConnectStop as stop:
            carried = stop.error
        # Raised outside the ``except`` block so the original exception keeps
        # the ``__cause__`` and ``__context__`` it was born with -- the pool
        # classifies by walking exactly that chain.
        raise carried
