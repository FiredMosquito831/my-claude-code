"""A SOCKS5 candidate that accepts and stalls is dead, not a hang.

The checker has two depths and the hole was only in one of them. At ``tls``
depth the handshake is MCC's own, step for step, inside
``asyncio.wait_for(..., timeout)`` -- bounded since 7.22.1. At ``request``
depth, which is what the Test button, "Add all working" and the background
re-prober all use, the tunnel is ``httpx``'s, and ``httpcore`` 1.0.9 gave its
SOCKS5 handshake no deadline at all. The sweep survived that because it wraps
each address in ``check_budget``; ``runtime/proxy_check_timer.py`` and the
per-provider Test route pass no budget, so for them an address that accepted
and said nothing was a permanent worker.

Both depths are asserted here, against the same listener, so the two can never
drift apart again.
"""

import asyncio
import time

import pytest

from my_claude_code.application.proxy_check import (
    CHECK_DEPTH_REQUEST,
    CHECK_DEPTH_TLS,
    check_proxy,
)
from my_claude_code.config.proxy_chains import TLS_UNKNOWN
from tests.support.fake_socks5 import GREET_THEN_STALL, SILENT, FakeSocks5Server

pytestmark = pytest.mark.asyncio

#: Both the per-leg timeout and the connect timeout the check is given.
TIMEOUT = 1.0
#: The suite's own bound. Well clear of two legs of ``TIMEOUT`` plus the
#: closes, and far short of "for ever".
OUTER = 15.0


@pytest.mark.parametrize("depth", [CHECK_DEPTH_REQUEST, CHECK_DEPTH_TLS])
@pytest.mark.parametrize("behaviour", [SILENT, GREET_THEN_STALL])
async def test_a_stalling_socks5_candidate_is_a_verdict_not_a_worker(depth, behaviour):
    socks = FakeSocks5Server(behaviour)
    await socks.start()
    began = time.monotonic()
    task = asyncio.create_task(
        check_proxy(
            socks.url,
            "https://upstream.invalid/v1",
            timeout=TIMEOUT,
            connect_timeout=TIMEOUT,
            depth=depth,
        )
    )
    try:
        done, _ = await asyncio.wait({task}, timeout=OUTER)
        elapsed = time.monotonic() - began
        assert done, f"the check was still running after {elapsed:.1f}s"
        record = task.result()
        # Dead, and never a pass: nothing measured this address's tunnel.
        assert record.ok is False
        assert record.tls == TLS_UNKNOWN
        assert record.detail
        assert elapsed < OUTER / 2, f"took {elapsed:.1f}s"
        # The request depth reaches the address twice on purpose -- once for
        # the reachability dial, once inside ``httpx``, which owns its own
        # pool and cannot be handed a socket. The tls depth reaches it once.
        assert socks.accepted == (2 if depth == CHECK_DEPTH_REQUEST else 1)
    finally:
        if not task.done():
            task.cancel()
        await socks.stop()
