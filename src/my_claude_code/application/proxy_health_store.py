"""Durability for the reachability bench.

The ledger in :mod:`my_claude_code.core.proxy_rotation` is process-lifetime
state on a monotonic clock. Up to 7.18 that was survivable: a bench expiring
re-admitted an address by itself, so a restart only cost the operator one extra
connect timeout per dead address. Since 7.19.0 an address that failed comes
back **only** after a check passes, so losing the ledger at a restart would be
the opposite mistake -- every dead proxy in every chain would look healthy
again, and the operator would pay for the discovery a second time on live
requests.

So the bench is written down. Three rules, and each is a bug this codebase has
already had somewhere else:

1. **``core`` does not learn what a store is.** The ledger names the address
   that changed through a listener; this module owns the file. That keeps the
   import boundary ``tests/contracts/test_import_boundaries.py`` enforces.
2. **Never write from the request path.** A failure marks the address dirty and
   returns. The flush happens on a timer, which is what keeps a chain of three
   hundred dead addresses from being three hundred writes of the same file
   inside one request.
3. **Re-read before writing.** The operator may be editing chains in another
   tab while the flush lands, exactly as
   :func:`~my_claude_code.application.proxy_check.check_endpoints` re-reads
   before persisting its verdicts. The flush derives from a fresh read and
   touches only the ``health`` key of endpoints it actually has news about.
"""

import threading
import time
from datetime import UTC, datetime

from loguru import logger

from my_claude_code.config.credentials import mask_proxy_label
from my_claude_code.config.proxy_chains import (
    ProxyChains,
    ProxyHealthState,
    load_proxy_chains,
    save_proxy_chains,
)
from my_claude_code.core.proxy_rotation import PROXY_REACHABILITY

#: Addresses whose bench moved since the last flush, by the ``host:port`` label
#: the ledger keys on. A set rather than a queue: the same address failing four
#: times in one request is one thing to write, not four.
_DIRTY: set[str] = set()
_DIRTY_LOCK = threading.Lock()
_WRITE_LOCK = threading.Lock()


def note_changed(label: str) -> None:
    """The ledger's listener. Cheap, non-blocking, and never raises."""

    if not label:
        return
    with _DIRTY_LOCK:
        _DIRTY.add(label)


def install_listener() -> None:
    """Wire the ledger to this module. Called once, at startup."""

    PROXY_REACHABILITY.set_listener(note_changed)


def remove_listener() -> None:
    """Unwire it again, for a test or a runtime shutting down."""

    PROXY_REACHABILITY.set_listener(None)
    with _DIRTY_LOCK:
        _DIRTY.clear()


def _take_dirty() -> set[str]:
    with _DIRTY_LOCK:
        taken = set(_DIRTY)
        _DIRTY.clear()
    return taken


def _label_for(store: ProxyChains, proxy_id: str) -> str:
    endpoint = store.proxies.get(proxy_id)
    if endpoint is None:  # pragma: no cover - callers iterate the store
        return ""
    return endpoint.label or mask_proxy_label(endpoint.url)


def flush_health(now: float | None = None) -> int:
    """Write every dirty address's bench into the store. Returns how many.

    Synchronous and file-bound, so callers run it off the loop. Returning 0 is
    the ordinary case and costs one set copy.
    """

    dirty = _take_dirty()
    if not dirty:
        return 0
    wall = time.time() if now is None else now
    stamp = _iso(wall)
    with _WRITE_LOCK:
        try:
            store = load_proxy_chains()
        except Exception as exc:  # pragma: no cover - a read failure is logged
            logger.warning(
                "PROXY HEALTH: could not read the store to persist health: exc_type={}",
                type(exc).__name__,
            )
            return 0
        written = 0
        fresh = store
        for proxy_id in store.proxies:
            label = _label_for(store, proxy_id)
            if label not in dirty:
                continue
            failures, remaining, reason = PROXY_REACHABILITY.state(label)
            health = (
                None
                if failures <= 0
                else ProxyHealthState(
                    failures=failures,
                    until=wall + max(0.0, remaining),
                    reason=reason,
                    at=stamp,
                )
            )
            updated = fresh.with_health(proxy_id, health)
            if updated is not fresh:
                written += 1
            fresh = updated
        if written:
            try:
                save_proxy_chains(fresh)
            except Exception as exc:  # pragma: no cover - a write failure is logged
                logger.warning(
                    "PROXY HEALTH: could not persist health: exc_type={}",
                    type(exc).__name__,
                )
                return 0
    return written


def arm_health_from_store(store: ProxyChains | None = None) -> int:
    """Re-arm the reachability ledger from the durable record.

    Called once at startup, beside
    :func:`~my_claude_code.application.proxy_check.arm_refusals_from_store`,
    and for the same reason: without it a restart makes dead addresses look
    healthy, which now means a chain would route through them again rather
    than waiting for a check to pass.

    A bench that expired while the process was down is re-armed as unhealthy
    and immediately **due**, never as healthy. Nothing checked it, and the only
    thing that clears a bench is a check that passes.
    """

    table = load_proxy_chains() if store is None else store
    wall = time.time()
    armed = 0
    for endpoint in table.proxies.values():
        health = endpoint.health
        if health is None or health.failures <= 0:
            continue
        label = endpoint.label or mask_proxy_label(endpoint.url)
        PROXY_REACHABILITY.restore(
            label,
            health.failures,
            max(0.0, health.until - wall),
            health.reason,
        )
        armed += 1
    if armed:
        logger.info(
            "PROXY HEALTH: {} address(es) stay out of rotation from a previous "
            "run until a check passes",
            armed,
        )
    # Re-arming is not news. Without this the very first flush would rewrite
    # every row it just read.
    with _DIRTY_LOCK:
        _DIRTY.clear()
    return armed


def _iso(wall: float) -> str:
    return datetime.fromtimestamp(wall, UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "arm_health_from_store",
    "flush_health",
    "install_listener",
    "note_changed",
    "remove_listener",
]
