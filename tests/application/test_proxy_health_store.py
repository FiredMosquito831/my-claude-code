"""A dead proxy stays dead across a restart.

The reachability ledger is process-lifetime state on a monotonic clock. Up to
7.18 losing it at a restart cost one extra connect timeout per dead address,
because a bench expiring re-admitted an address anyway. Since 7.19.0 only a
check that passes re-admits one, so losing the ledger would be the opposite
mistake: every dead proxy in every chain would look healthy again.
"""

import time
from dataclasses import replace

import pytest

from my_claude_code.application.proxy_health_store import (
    arm_health_from_store,
    flush_health,
    install_listener,
    remove_listener,
)
from my_claude_code.config import proxy_chains as store_module
from my_claude_code.config.proxy_chains import (
    ProxyChain,
    ProxyChainEntry,
    load_proxy_chains,
    save_proxy_chains,
)
from my_claude_code.core.proxy_rotation import PROXY_REACHABILITY, reset_proxy_health


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A store on disk, and the ledger wired to it, both torn down after."""

    path = tmp_path / "proxy_chains.json"
    monkeypatch.setattr(store_module, "proxy_chains_path", lambda: path)
    reset_proxy_health()
    install_listener()
    yield path
    remove_listener()
    reset_proxy_health()


def _seed() -> str:
    table = load_proxy_chains()
    table, proxy_id = table.add_endpoint("http://198.51.100.9:8080")
    table = table.with_chain(
        "nvidia_nim",
        ProxyChain(enabled=True, entries=(ProxyChainEntry(proxy=proxy_id),)),
    )
    save_proxy_chains(table)
    return proxy_id


def test_a_failure_is_written_down_and_survives_a_restart(store) -> None:
    """The whole of the durability claim, end to end.

    ``reset_proxy_health`` is the simulated restart: it is exactly what the
    process has after a start -- nothing measured, nothing benched -- and if
    re-arming did not happen the address would come back healthy.
    """

    proxy_id = _seed()
    label = load_proxy_chains().proxies[proxy_id].label or "198.51.100.9:8080"

    PROXY_REACHABILITY.note_failure(label, "ConnectError")
    assert flush_health() == 1

    saved = load_proxy_chains().proxies[proxy_id].health
    assert saved is not None
    assert saved.failures == 1
    assert saved.reason == "ConnectError"
    assert saved.until > time.time()

    # The restart.
    reset_proxy_health()
    assert PROXY_REACHABILITY.is_unhealthy(label) is False

    assert arm_health_from_store() == 1
    assert PROXY_REACHABILITY.is_unhealthy(label) is True
    assert PROXY_REACHABILITY.remaining(label) > 0


def test_a_bench_that_expired_while_the_process_was_down_is_due_not_healthy(
    store,
) -> None:
    """Nothing checked it, so nothing has earned it a way back.

    Time passing while MCC was not running is not evidence about a proxy. The
    address comes back as unhealthy and immediately due for a re-probe, which
    is what the re-prober will act on at its next pass.
    """

    proxy_id = _seed()
    label = load_proxy_chains().proxies[proxy_id].label or "198.51.100.9:8080"
    PROXY_REACHABILITY.note_failure(label, "ConnectError")
    flush_health(now=time.time() - 7200)

    reset_proxy_health()
    arm_health_from_store()

    assert PROXY_REACHABILITY.is_unhealthy(label) is True
    assert PROXY_REACHABILITY.due_for_reprobe(label) is True


def test_a_success_clears_the_stored_bench(store) -> None:
    """A check that passed is the only thing that retires a record."""

    proxy_id = _seed()
    label = load_proxy_chains().proxies[proxy_id].label or "198.51.100.9:8080"
    PROXY_REACHABILITY.note_failure(label, "ConnectError")
    flush_health()
    assert load_proxy_chains().proxies[proxy_id].health is not None

    PROXY_REACHABILITY.note_success(label)
    assert flush_health() == 1

    assert load_proxy_chains().proxies[proxy_id].health is None


def test_the_flush_re_reads_and_does_not_lose_an_edit_made_beside_it(store) -> None:
    """The operator edits a chain while the writer is holding news.

    The writer derives from a fresh read, the way ``check_endpoints`` does, so
    what it writes back is the operator's edit plus the health -- not the store
    as it was when the failure happened.
    """

    proxy_id = _seed()
    label = load_proxy_chains().proxies[proxy_id].label or "198.51.100.9:8080"
    PROXY_REACHABILITY.note_failure(label, "ConnectError")

    # The edit lands between the failure and the flush.
    edited = load_proxy_chains()
    chain = edited.chain("nvidia_nim")
    assert chain is not None
    save_proxy_chains(
        edited.with_chain("nvidia_nim", replace(chain, policy="round_robin"))
    )

    flush_health()

    fresh = load_proxy_chains()
    reread = fresh.chain("nvidia_nim")
    assert reread is not None
    assert reread.policy == "round_robin"
    assert fresh.proxies[proxy_id].health is not None


def test_nothing_is_written_when_nothing_changed(store) -> None:
    """A flush on a quiet install is a set copy and no file write at all."""

    _seed()
    before = store.read_text(encoding="utf-8")

    assert flush_health() == 0

    assert store.read_text(encoding="utf-8") == before
