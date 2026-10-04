"""``proxy_chains.json`` across threads: one writer at a time, never an empty read.

Two failures, both closed in 7.72.1.

**Lost updates.** The dashboard's edits, the health flush, the checker's
verdicts, a fetch's offer and an ingest each re-read the store, changed their
part and saved it -- under three different locks or none. Two of them landing
together each derived a document from the same read, and the second save
dropped the first one's change. Every case below holds each writer between its
read and its save until the others have read too, which is the interleaving
that loses an update, made certain rather than likely.

**An empty table handed to a provider build.** The cache the event loop reads
while building providers kept its signature and its table in two globals, and
cached a read that failed -- on Windows, any read that lands on another
thread's ``os.replace`` of the file -- as "no chains". A provider built from
that has no chain and, with no ``<PROVIDER>_PROXY`` of its own, routes direct.
"""

import asyncio
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from my_claude_code.api import admin_proxy_routes
from my_claude_code.application import (
    proxy_check,
    proxy_fetch,
    proxy_health_store,
    proxy_ingest,
)
from my_claude_code.application.proxy_health_store import (
    install_listener,
    remove_listener,
)
from my_claude_code.application.proxy_ingest import FeedResult, candidate_id
from my_claude_code.config import proxy_chains
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.proxy_chains import (
    TLS_STRICT,
    ProxyChain,
    ProxyChainEntry,
    ProxyChains,
    ProxyCheckRecord,
    ProxyEndpoint,
    current_proxy_chains,
    load_proxy_chains,
    reset_proxy_chains_cache,
    save_proxy_chains,
)
from my_claude_code.config.proxy_feeds import FeedEndpoint
from my_claude_code.config.settings import Settings
from my_claude_code.core.proxy_rotation import PROXY_REACHABILITY, reset_proxy_health
from my_claude_code.providers.runtime.config import build_provider_config

#: How long a writer held between its read and its save waits for the others
#: to have read too. Under one lock none of them can, so each waits this long
#: once; without it they all have, and the wait ends at once.
GATE_PATIENCE_SECONDS = 0.3

PROVIDER = "nvidia_nim"


@pytest.fixture
def store_path(monkeypatch, tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(proxy_chains, "proxy_chains_path", lambda: path)
    reset_proxy_chains_cache()
    reset_proxy_health()
    proxy_check.reset_refusal_lifts()
    yield path
    remove_listener()
    reset_proxy_chains_cache()
    reset_proxy_health()
    proxy_check.reset_refusal_lifts()


# ------------------------------------------------------------------ writers


class _SaveGate:
    """Holds every writer between its read and its save until all have read.

    ``one_by_one`` then lets them save in turn rather than together: the
    together case is two saves racing for one staging file, the one-by-one
    case is the plain lost update -- every save correct, each from a stale read.
    """

    def __init__(self, writers: int, *, one_by_one: bool) -> None:
        self._writers = writers
        self._one_by_one = one_by_one
        self._arrived = 0
        self._turn = 0
        self._condition = threading.Condition()

    def wrap(self, save: Callable[..., None]) -> Callable[..., None]:
        def gated(chains: ProxyChains, path: Path | None = None) -> None:
            with self._condition:
                ticket = self._arrived
                self._arrived += 1
                self._condition.notify_all()
                all_read = self._condition.wait_for(
                    lambda: self._arrived >= self._writers,
                    timeout=GATE_PATIENCE_SECONDS,
                )
                if all_read and self._one_by_one:
                    self._condition.wait_for(lambda: self._turn == ticket, 10.0)
            try:
                save(chains, path)
            finally:
                with self._condition:
                    self._turn += 1
                    self._condition.notify_all()

        return gated


def _record(detail: str) -> ProxyCheckRecord:
    return ProxyCheckRecord(
        at="2026-10-04T00:00:00Z", ok=True, latency_ms=12, tls=TLS_STRICT, detail=detail
    )


def _seed_shared_store() -> None:
    """Three addresses in one chain, so no offer replacement can drop them."""

    save_proxy_chains(
        ProxyChains(
            proxies={
                "px_a": ProxyEndpoint(url="http://198.51.100.1:8080", label="pa"),
                "px_b": ProxyEndpoint(url="http://198.51.100.2:8080", label="pb"),
                "px_d": ProxyEndpoint(url="http://198.51.100.4:8080", label="pd"),
            },
            chains={
                "alpha": ProxyChain(
                    enabled=True,
                    entries=(
                        ProxyChainEntry(proxy="px_a"),
                        ProxyChainEntry(proxy="px_b"),
                        ProxyChainEntry(proxy="px_d"),
                    ),
                )
            },
        )
    )


@pytest.mark.parametrize("release", ["together", "one_by_one"])
@pytest.mark.parametrize("offer_writer", ["fetch", "ingest"])
def test_every_writer_of_the_store_keeps_every_other_writers_change(
    store_path, monkeypatch, offer_writer, release
) -> None:
    """Dashboard save, health flush, check commit and an offer, all at once.

    A fetch and an ingest each *replace* the offer list by design, so they are
    run in separate cases rather than against each other: what is asserted is
    that no change is lost to a race, not that two offers merge.
    """

    _seed_shared_store()
    gate = _SaveGate(writers=4, one_by_one=release == "one_by_one")
    real_save = proxy_chains.save_proxy_chains
    for module in (
        admin_proxy_routes,
        proxy_health_store,
        proxy_check,
        proxy_fetch,
        proxy_ingest,
    ):
        monkeypatch.setattr(module, "save_proxy_chains", gate.wrap(real_save))

    checking = threading.Event()

    async def canned_check(url: str, destination: str, **_: Any) -> ProxyCheckRecord:
        checking.set()
        return _record("check")

    monkeypatch.setattr(proxy_check, "check_proxy", canned_check)

    offered_address = FeedEndpoint(protocol="http", ip="203.0.113.50", port=3128)

    async def canned_harvest(**_: Any):
        return (
            [FeedResult(feed_id="f1", name="f1", ok=True, count=1)],
            [("f1", (offered_address,))],
            1,
        )

    monkeypatch.setattr(proxy_ingest, "harvest_feeds", canned_harvest)

    install_listener()
    PROXY_REACHABILITY.note_failure("pa", "ConnectError")

    def admin_save() -> None:
        admin_proxy_routes._commit(
            "beta", ProxyChain(enabled=True, entries=(ProxyChainEntry(proxy="px_a"),))
        )

    def health_flush() -> None:
        assert proxy_health_store.flush_health() == 1

    def check_commit() -> None:
        asyncio.run(
            proxy_check.check_endpoints(
                ["px_b"], {"px_b": "https://origin.invalid"}, timeout=5.0
            )
        )

    fetched_row = replace(
        load_proxy_chains().proxies["px_d"], last_check=_record("fetch")
    )

    def fetch_commit() -> None:
        proxy_fetch._commit_fetch([("px_d", fetched_row)], [])

    def ingest_commit() -> None:
        asyncio.run(proxy_ingest.ingest(persist=True))

    errors: dict[str, BaseException] = {}
    go = threading.Event()

    def run(name: str, writer: Callable[[], None], wait: bool = True) -> None:
        if wait:
            assert go.wait(10.0)
        try:
            writer()
        except BaseException as exc:
            errors[name] = exc

    offer = fetch_commit if offer_writer == "fetch" else ingest_commit
    threads = [
        # The checker first, and the others only once it is past its own
        # read of the table: that read is not part of its read-modify-write.
        threading.Thread(target=run, args=("check", check_commit, False)),
        threading.Thread(target=run, args=("admin", admin_save)),
        threading.Thread(target=run, args=("health", health_flush)),
        threading.Thread(target=run, args=(offer_writer, offer)),
    ]
    threads[0].start()
    assert checking.wait(10.0), "the check never reached its probe"
    for thread in threads[1:]:
        thread.start()
    go.set()
    for thread in threads:
        thread.join(30.0)
        assert not thread.is_alive(), "a writer never finished"

    assert errors == {}
    store = load_proxy_chains()
    assert [entry.proxy for entry in store.chains["alpha"].entries] == [
        "px_a",
        "px_b",
        "px_d",
    ]
    beta = store.chain("beta")
    assert beta is not None, "the dashboard's save was lost"
    assert [entry.proxy for entry in beta.entries] == ["px_a"]
    health = store.proxies["px_a"].health
    assert health is not None and health.failures >= 1, "the health flush was lost"
    check = store.proxies["px_b"].last_check
    assert check is not None and check.detail == "check", "the verdict was lost"
    if offer_writer == "fetch":
        fetched = store.proxies["px_d"].last_check
        assert fetched is not None and fetched.detail == "fetch", "the fetch was lost"
    else:
        assert candidate_id(offered_address.address) in store.candidates, (
            "the ingest's offer was lost"
        )


# ------------------------------------------------------------------ readers


def _two_proxy_chain(ids: tuple[str, str], urls: tuple[str, str]) -> ProxyChains:
    return ProxyChains(
        proxies={
            ids[0]: ProxyEndpoint(url=urls[0]),
            ids[1]: ProxyEndpoint(url=urls[1]),
        },
        chains={
            PROVIDER: ProxyChain(
                enabled=True,
                entries=(ProxyChainEntry(proxy=ids[0]), ProxyChainEntry(proxy=ids[1])),
            )
        },
    )


#: Two generations of one provider's chain that share no address id, the way a
#: chain looks after its operator replaced every proxy in it: the old chain's
#: entries name nothing in the new catalogue.
TABLE_ONE = _two_proxy_chain(
    ("px_one", "px_two"), ("http://198.51.100.11:8080", "http://198.51.100.12:8080")
)
TABLE_TWO = _two_proxy_chain(
    ("px_three", "px_four"), ("http://198.51.100.21:8080", "http://198.51.100.22:8080")
)
LEGS_ONE = ["http://198.51.100.11:8080", "http://198.51.100.12:8080"]
LEGS_TWO = ["http://198.51.100.21:8080", "http://198.51.100.22:8080"]


def _settings() -> Settings:
    # No NVIDIA_NIM_PROXY: a build that loses the chain goes out direct.
    return Settings.model_validate({"nvidia_nim_api_key": "k1"})


def _legs() -> list[str] | None:
    """The proxy legs a provider built now would get; ``None`` means direct."""

    config = build_provider_config(PROVIDER_CATALOG[PROVIDER], _settings())
    if config.proxy_chain is None:
        return [config.proxy] if config.proxy else None
    return [leg.url for leg in config.proxy_chain.legs]


def _lines_traced(tracer_for: Callable[[], Any], body: Callable[[], Any]) -> Any:
    previous = sys.gettrace()
    sys.settrace(tracer_for())
    try:
        return body()
    finally:
        sys.settrace(previous)


def _count_cache_lines() -> int:
    seen = 0

    def tracer():
        def local(frame, event, arg):
            nonlocal seen
            if event == "line":
                seen += 1
            return local

        def outer(frame, event, arg):
            if frame.f_code is current_proxy_chains.__code__:
                return local
            return None

        return outer

    _lines_traced(tracer, _legs)
    return seen


def _prime(cache: str) -> None:
    """A cache as a running server has it, or as a save just left it."""

    if cache == "warm":
        current_proxy_chains()
    else:
        reset_proxy_chains_cache()


@pytest.mark.parametrize("cache", ["warm", "cold"])
def test_a_reset_landing_before_any_line_of_the_cache_read_never_drops_the_chain(
    store_path, cache
) -> None:
    """Every place a worker's save could interrupt a provider build, one by one.

    A tracer resets the cache immediately before the n-th line the cache read
    executes, for every n a build reaches -- from a warm cache and from one
    that has to read the file. With the GIL that interleaving is rare between
    two particular lines; without it, or with one more call between them, it
    is not -- and the answer must never be "no chain".
    """

    save_proxy_chains(TABLE_ONE)
    expected = ["http://198.51.100.11:8080", "http://198.51.100.12:8080"]
    _prime(cache)
    lines = _count_cache_lines()
    assert lines >= 4

    for target in range(1, lines + 1):
        _prime(cache)
        seen = 0

        def tracer(target: int = target):
            def local(frame, event, arg):
                nonlocal seen
                if event == "line":
                    seen += 1
                    if seen == target:
                        reset_proxy_chains_cache()
                return local

            def outer(frame, event, arg):
                if frame.f_code is current_proxy_chains.__code__:
                    return local
                return None

            return outer

        legs = _lines_traced(tracer, _legs)
        assert legs == expected, f"a reset before line {target} lost the chain"


def test_a_save_landing_anywhere_in_a_build_gives_the_old_chain_or_the_new(
    store_path,
) -> None:
    """Never neither: a build is made from one table, whichever it was.

    A tracer makes a whole save of the next generation of the chain -- one that
    shares no address with the current one -- land immediately before the n-th
    line of ``resolve_proxy_chain``, for every n. A build that read the chain
    from one table and its addresses from the next would find none of them.
    """

    from my_claude_code.providers.runtime import config as runtime_config

    code = runtime_config.resolve_proxy_chain.__code__
    save_proxy_chains(TABLE_ONE)
    current_proxy_chains()
    seen = 0

    def counter():
        def local(frame, event, arg):
            nonlocal seen
            if event == "line":
                seen += 1
            return local

        return lambda frame, event, arg: local if frame.f_code is code else None

    _lines_traced(counter, _legs)
    lines = seen
    assert lines >= 4

    for target in range(1, lines + 1):
        save_proxy_chains(TABLE_ONE)
        current_proxy_chains()
        seen = 0

        def tracer(target: int = target):
            def local(frame, event, arg):
                nonlocal seen
                if event == "line":
                    seen += 1
                    if seen == target:
                        save_proxy_chains(TABLE_TWO)
                return local

            return lambda frame, event, arg: local if frame.f_code is code else None

        legs = _lines_traced(tracer, _legs)
        assert legs in (LEGS_ONE, LEGS_TWO), (
            f"a save before line {target} of the build gave {legs}"
        )


class _FailingReads:
    """Reads of the store fail like a read that lands on another's rename.

    ``failures`` reads fail, or every read while ``limit`` is ``None`` and
    ``active`` is set.
    """

    def __init__(self, monkeypatch, path: Path, limit: int | None) -> None:
        self.failed = 0
        self.active = True
        original = Path.read_text

        def read_text(target: Path, *args: Any, **kwargs: Any) -> str:
            if (
                self.active
                and target == path
                and (limit is None or self.failed < limit)
            ):
                self.failed += 1
                raise PermissionError(13, "simulated sharing violation", str(target))
            return original(target, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read_text)


def test_a_provider_built_while_another_writer_renames_the_file_keeps_its_chain(
    store_path, monkeypatch
) -> None:
    save_proxy_chains(TABLE_ONE)
    assert _legs() is not None
    save_proxy_chains(TABLE_TWO)  # another writer's save: the cache is stale

    reads = _FailingReads(monkeypatch, store_path, limit=1)
    built = _legs()

    assert reads.failed == 1, "the injected failure never fired"
    assert built == ["http://198.51.100.21:8080", "http://198.51.100.22:8080"]
    # And nothing poisoned was cached: the next build agrees.
    assert _legs() == built


def test_a_store_that_stays_unreadable_serves_the_table_read_before(
    store_path, monkeypatch, caplog
) -> None:
    """Past the retry bound: the previous table, not none, and not remembered."""

    save_proxy_chains(TABLE_ONE)
    assert _legs() == ["http://198.51.100.11:8080", "http://198.51.100.12:8080"]
    save_proxy_chains(TABLE_TWO)

    reads = _FailingReads(monkeypatch, store_path, limit=None)
    built = _legs()
    assert reads.failed
    assert built == ["http://198.51.100.11:8080", "http://198.51.100.12:8080"]
    assert any(
        "PROXY CHAINS: cannot read" in record.getMessage() for record in caplog.records
    )

    # Readable again: the table on disk now, because the stand-in was never
    # recorded as what the file holds.
    reads.active = False
    assert _legs() == ["http://198.51.100.21:8080", "http://198.51.100.22:8080"]


def test_two_seconds_of_saves_never_show_a_reader_an_empty_table(store_path) -> None:
    """The real thing: a worker saving over and over while a reader builds.

    On Windows a read that lands on the rename fails with a sharing violation;
    elsewhere it does not, and this passes everywhere for the same reason the
    cases above do.
    """

    save_proxy_chains(TABLE_ONE)
    assert current_proxy_chains().chain(PROVIDER) is not None
    stop = threading.Event()
    saves = {"ok": 0, "refused": 0}

    def writer() -> None:
        tables = (TABLE_ONE, TABLE_TWO)
        index = 0
        while not stop.is_set():
            try:
                save_proxy_chains(tables[index % 2])
                saves["ok"] += 1
            except OSError:
                # The reader holding the file open refuses the rename on
                # Windows; that is a failed write, reported to its caller.
                saves["refused"] += 1
            index += 1

    thread = threading.Thread(target=writer)
    thread.start()
    reads = 0
    empty = 0
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            reads += 1
            if current_proxy_chains().chain(PROVIDER) is None:
                empty += 1
    finally:
        stop.set()
        thread.join(10.0)

    assert saves["ok"] > 0
    assert empty == 0, f"{empty} of {reads} reads came back with no chain"
