"""``check_endpoints(off_loop=True)``: the same verdicts, off the server's loop.

The mechanism this release ships for item 3 is deliberately the smallest one
that works: :func:`check_proxy` -- and nothing else -- runs on one worker thread
with an event loop of its own, for the duration of one sweep. The semaphore, the
per-address yield, the verdict handling, the ledgers and the single save all stay
on the caller's loop, in the caller's order, on the caller's thread.

Two things have to be true of that, and neither is a matter of opinion:

* **the verdicts do not move.** Same rig, same addresses, both ways, compared
  field by field -- including the TLS-interception refusal, which is the one
  verdict that is a security decision.
* **the loop is not held.** A heartbeat task asks for 50 ms of sleep and records
  how much later it woke, exactly as the out-of-process measurement in the PR
  did, at a size a test suite can afford.

The rig is the one 7.22.2 already built and this file does not fork: a CA, an
https origin holding a certificate that chains to it, an honest CONNECT proxy
and a wiretapping one, all on loopback, all torn down in the fixture.
"""

import asyncio
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from my_claude_code.application.proxy_check import check_endpoints, check_proxy
from my_claude_code.config.proxy_chains import (
    ProxyChains,
    reset_proxy_chains_cache,
    save_proxy_chains,
)
from my_claude_code.core.proxy_rotation import reset_proxy_health
from tests.application import test_proxy_check_tls as rig
from tests.application.test_proxy_check_tls import (
    HOSTNAME,
    _ConnectProxy,
    _Origin,
)

#: The 7.22.2 rig's CA/origin/rogue-certificate fixture, borrowed rather than
#: forked: one PKI, one set of servers, one place they are torn down.
pki = rig.pki

pytestmark = pytest.mark.asyncio

#: A dead address that cannot be routed anywhere, so the check spends its
#: connect timeout and nothing else. RFC 5737 documentation space.
BLACK_HOLE = "http://192.0.2.201:8080"


@pytest.fixture
def store_path(monkeypatch, tmp_path: Path) -> Iterator[Path]:
    from my_claude_code.config import proxy_chains

    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(proxy_chains, "proxy_chains_path", lambda: path)
    reset_proxy_chains_cache()
    reset_proxy_health()
    yield path
    reset_proxy_chains_cache()
    reset_proxy_health()


def _seed(urls: list[str]) -> tuple[str, ...]:
    store = ProxyChains()
    ids: list[str] = []
    for url in urls:
        store, proxy_id = store.add_endpoint(url)
        ids.append(proxy_id)
    save_proxy_chains(replace(store, candidates=tuple(ids)))
    return tuple(ids)


def _verdicts(outcomes) -> list[tuple[bool, str, str, str]]:
    """Everything a verdict says, in a form two runs can be compared on."""

    return [
        (
            outcome.record.ok,
            outcome.record.tls,
            outcome.record.depth,
            outcome.label,
        )
        for outcome in outcomes.values()
    ]


async def test_the_verdicts_are_identical_with_and_without_the_worker_loop(
    pki, store_path
) -> None:
    """The whole trust story, both ways, including the refusal."""

    origin = _Origin(pki["honest"])
    honest = _ConnectProxy()
    wiretap = _ConnectProxy(mitm=pki["rogue"])
    try:
        destination = f"https://{HOSTNAME}:{origin.port}"
        urls = [
            f"http://127.0.0.1:{honest.port}",
            f"http://127.0.0.1:{wiretap.port}",
            BLACK_HOLE,
        ]

        ids = _seed(urls)
        on_loop = await check_endpoints(
            ids, dict.fromkeys(ids, destination), timeout=20.0, concurrency=3
        )

        reset_proxy_health()
        reset_proxy_chains_cache()
        ids = _seed(urls)
        off_loop = await check_endpoints(
            ids,
            dict.fromkeys(ids, destination),
            timeout=20.0,
            concurrency=3,
            off_loop=True,
        )

        assert _verdicts(off_loop) == _verdicts(on_loop)
        # And the one that matters most, named rather than implied.
        refused = [
            outcome for outcome in off_loop.values() if outcome.record.intercepted
        ]
        assert len(refused) == 1, "the wiretap must still be refused off the loop"
    finally:
        wiretap.close()
        honest.close()
        origin.close()


async def test_the_handshakes_do_not_run_on_the_callers_thread(
    pki, store_path, monkeypatch
) -> None:
    """The claim, asserted on thread identity rather than on a timing."""

    origin = _Origin(pki["honest"])
    honest = _ConnectProxy()
    try:
        destination = f"https://{HOSTNAME}:{origin.port}"
        caller = threading.get_ident()
        threads: set[int] = set()
        original = check_proxy

        async def spy(*args, **kwargs):
            threads.add(threading.get_ident())
            return await original(*args, **kwargs)

        monkeypatch.setattr("my_claude_code.application.proxy_check.check_proxy", spy)

        ids = _seed([f"http://127.0.0.1:{honest.port}", BLACK_HOLE])
        await check_endpoints(
            ids,
            dict.fromkeys(ids, destination),
            timeout=8.0,
            concurrency=2,
            off_loop=True,
        )

        assert threads and caller not in threads

        # And the thread is given back: a sweep that leaked one would be a
        # thread per bulk add for the life of the process.
        def workers() -> list[str]:
            return [
                thread.name
                for thread in threading.enumerate()
                if thread.name.startswith("mcc-proxy-check-loop")
            ]

        deadline = time.monotonic() + 5.0
        while workers() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert workers() == []
    finally:
        honest.close()
        origin.close()


async def test_a_wide_sweep_does_not_hold_the_callers_loop(pki, store_path) -> None:
    """The bound, with the same instrument the PR's measurement used.

    Smaller than the 300-address, concurrency-100 run in the PR body -- this is
    a test suite, not a bench -- but the same shape and the same question. The
    bound is deliberately loose, because a CI runner with sixteen pytest workers
    on it is allowed to be slow and a timing assertion that flakes teaches
    nobody anything. The *deterministic* guard on the mechanism is
    ``test_the_handshakes_do_not_run_on_the_callers_thread`` above; this one is
    here so a change that quietly puts a hundred handshakes back on the caller's
    loop cannot pass without somebody looking at a number.
    """

    origin = _Origin(pki["honest"])
    proxies = [_ConnectProxy() for _ in range(12)]
    try:
        destination = f"https://{HOSTNAME}:{origin.port}"
        urls = [f"http://127.0.0.1:{proxy.port}" for proxy in proxies]
        urls += [f"http://192.0.2.{index}:8080" for index in range(1, 13)]
        ids = _seed(urls)

        gaps: list[float] = []

        async def beat() -> None:
            interval = 0.05
            previous = time.monotonic()
            while True:
                await asyncio.sleep(interval)
                now = time.monotonic()
                gaps.append((now - previous - interval) * 1000.0)
                previous = now

        heartbeat = asyncio.create_task(beat())
        try:
            outcomes = await check_endpoints(
                ids,
                dict.fromkeys(ids, destination),
                timeout=20.0,
                concurrency=len(ids),
                max_concurrency=len(ids),
                off_loop=True,
            )
        finally:
            heartbeat.cancel()
            with pytest.raises(asyncio.CancelledError):
                await heartbeat

        assert sum(1 for outcome in outcomes.values() if outcome.record.ok) == 12
        assert gaps, "the heartbeat never got a turn at all"
        worst = max(gaps)
        assert worst < 600.0, (
            f"the caller's loop was {worst:.0f} ms late during a "
            f"{len(ids)}-wide sweep; the handshakes are back on it"
        )
    finally:
        for proxy in proxies:
            proxy.close()
        origin.close()
