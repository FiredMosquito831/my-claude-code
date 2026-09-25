"""The speed ledger's file, and the one door every check result comes through.

:mod:`my_claude_code.core.proxy_speed` keeps the numbers; this module owns
``~/.mcc/proxy_speed.json`` and turns the application's own records into
samples. The same three rules as
:mod:`~my_claude_code.application.proxy_health_store`, for the same reasons:

1. **``core`` does not learn what a store is.** The ledger hands out a
   document and takes one back; the path is resolved here, from the one config
   directory every other store uses.
2. **Never write from the request path.** A sample marks the ledger dirty. The
   file is written by the ``ProxyHealthTimer`` tick -- every 30 s, only when
   something changed -- and once more at shutdown.
3. **A bad file is an empty ledger, never a crash.** The numbers are
   measurements, not configuration: losing them costs a day of history and
   nothing else, so a missing, truncated or foreign file is read as nothing.
"""

import json
import threading
import time
from typing import Any

from loguru import logger

from my_claude_code.config.atomic_json import write_json_document_atomically
from my_claude_code.config.paths import proxy_speed_path
from my_claude_code.config.proxy_chains import ProxyCheckRecord
from my_claude_code.core.proxy_speed import (
    KIND_CHECK,
    PROXY_SPEED,
    STATE_UNTESTED,
    SpeedSample,
)

_WRITE_LOCK = threading.Lock()


def record_check(label: str, provider_id: str, record: ProxyCheckRecord) -> None:
    """File one check's result as a sample for ``(label, provider_id)``.

    Called at the points a check reaches the ledgers -- beside
    ``hold_refusal`` in ``check_endpoints`` and in the fetch -- once per try.
    An interception is not a speed sample: it is the refusal ledger's verdict,
    and a refused address is never selected whatever its speed.
    """

    if not label or not provider_id or record.intercepted:
        return
    PROXY_SPEED.note(
        label,
        provider_id,
        SpeedSample(
            kind=KIND_CHECK,
            # When the result reached the ledgers, which is when it was
            # measured to within a round's spacing.
            at=time.time(),
            ok=bool(record.ok),
            connect_ms=_ms(record.connect_ms),
            tunnel_ms=_ms(record.tunnel_ms),
            tls_ms=_ms(record.tls_ms),
            first_byte_ms=_ms(record.first_byte_ms),
            failure="" if record.ok else (record.failure or "other"),
        ),
    )


def speed_payload(
    label: str,
    provider_id: str,
    *,
    connect_timeout_seconds: float,
    slow_ms: float,
) -> dict[str, Any]:
    """The ``speed`` object a candidate or chain entry carries on the page.

    ``connect_timeout_seconds`` is the operator's
    ``PROXY_CONNECT_TIMEOUT_SECONDS``: what one failed dial costs a live
    request, and therefore the ``F`` of the expected-setup formula.
    """

    if not label or not provider_id:
        return _untested()
    return PROXY_SPEED.score(
        label,
        provider_id,
        failure_cost_ms=max(0.0, float(connect_timeout_seconds)) * 1000.0,
        slow_ms=float(slow_ms),
    ).as_document()


def load_speed() -> int:
    """Read the stored ledger into the process. Called once, at startup."""

    path = proxy_speed_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        PROXY_SPEED.reset()
        return 0
    except OSError, ValueError:
        logger.warning(
            "PROXY SPEED: the stored ledger could not be read; starting empty"
        )
        PROXY_SPEED.reset()
        return 0
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("PROXY SPEED: the stored ledger is not JSON; starting empty")
        PROXY_SPEED.reset()
        return 0
    loaded = PROXY_SPEED.restore(document)
    if loaded:
        logger.info("PROXY SPEED: {} address/provider record(s) loaded", loaded)
    return loaded


def flush_speed() -> bool:
    """Write the ledger if anything changed since the last write. Never raises.

    Synchronous and file-bound, so callers run it off the loop.
    """

    if not PROXY_SPEED.dirty:
        return False
    with _WRITE_LOCK:
        if not PROXY_SPEED.dirty:
            return False
        document = PROXY_SPEED.snapshot()
        try:
            write_json_document_atomically(proxy_speed_path(), document)
        except Exception as exc:
            # Put the flag back: the next tick tries again with whatever has
            # arrived since.
            PROXY_SPEED.mark_dirty()
            logger.warning(
                "PROXY SPEED: could not persist the ledger: exc_type={}",
                type(exc).__name__,
            )
            return False
    return True


def _untested() -> dict[str, Any]:
    return {
        "setup_ms": None,
        "success_rate": 0.5,
        "successes": 0,
        "samples": 0,
        "live_samples": 0,
        "ttft_factor": 1.0,
        "expected_ms": None,
        "rank_key": None,
        "state": STATE_UNTESTED,
    }


def _ms(value: int | None) -> float | None:
    return None if value is None else float(value)


__all__ = [
    "flush_speed",
    "load_speed",
    "record_check",
    "speed_payload",
]
