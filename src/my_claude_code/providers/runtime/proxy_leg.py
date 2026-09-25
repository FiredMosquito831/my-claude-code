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

The base loop's every decision hangs off the exception the callable raised, so
the callable is wrapped. When it fails with a connect-class error the wrapper
asks for its own task to be cancelled, then re-raises the error unchanged. The
base loop sees exactly the exception it always saw: it classifies it, records
its one try, and -- for a retryable transport error -- goes to sleep before a
second dial. That backoff sleep is the loop's first suspension point after the
try, so the pending cancellation is delivered *there*, as a ``CancelledError``
the base's ``except Exception`` cannot catch. The loop is left without the
sleep, without a second dial, without a second recorded try, and without the
wait being written onto the try it just recorded. This class catches that
``CancelledError`` outside the loop, withdraws its own cancellation request,
and re-raises the original error object, so everything above sees the
identical exception it would have seen -- one backoff sooner.

Before 7.52.1 the wrapper stopped the *second* invocation instead, which is
after the sleep: every dead address still cost the chain ~2.3 s of backoff
before the pool was told (measured on 571 of 590 live connect failures). That
sentinel is kept as a backstop and still ends the loop if a second invocation
ever happens.

The cancellation is only ever this leg's own, and is always withdrawn:

* when the loop ends without sleeping (a non-retryable proxy error, the last
  attempt, a routed-around 429) the request is withdrawn on the way out, so no
  later ``await`` in the caller ever sees it;
* when somebody else cancelled the task too -- a client that hung up, a
  deadline -- withdrawing ours leaves theirs outstanding and their
  ``CancelledError`` propagates exactly as it would have.

Why a cancellation and not the loop's ``provider_failure_override``: that hook
must return an ``ExecutionFailure``, and the loop records the try from it -- a
failure *kind* in the row's ``error_kind`` and its ``status_code`` as the row's
status. The row a connect failure writes today (``kind="ConnectTimeout"``, no
status, ``error_kind="ConnectTimeout"``) could not survive that route byte for
byte.
"""

import asyncio
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
        task = asyncio.current_task()
        stopped: list[BaseException] = []
        # True while a cancellation *this leg* asked for is outstanding.
        requested = False

        async def guarded(*call_args: Any, **call_kwargs: Any) -> Any:
            nonlocal requested
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
                    if task is not None and not requested:
                        # Delivered at the loop's backoff sleep, the first
                        # suspension point after it records this try.
                        task.cancel()
                        requested = True
                raise

        carried: BaseException | None = None
        try:
            return await super().execute_with_retry(guarded, *args, **kwargs)
        except _ProxyConnectStop as stop:
            carried = stop.error
        except asyncio.CancelledError:
            if not requested or task is None:
                raise
            requested = False
            if task.uncancel() > 0:
                # Somebody else cancelled this task as well. Ours is
                # withdrawn; theirs is not ours to swallow.
                raise
            carried = stopped[0]
        finally:
            if requested and task is not None:
                # The loop ended without sleeping, so the request was never
                # delivered. Withdrawn here, before any ``await`` above this
                # frame could receive it.
                requested = False
                task.uncancel()
        # Raised outside the ``except`` block so the original exception keeps
        # the ``__cause__`` and ``__context__`` it was born with -- the pool
        # classifies by walking exactly that chain.
        raise carried
