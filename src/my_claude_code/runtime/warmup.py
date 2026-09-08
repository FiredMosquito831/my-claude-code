"""Work the first request should not have to pay for.

Two things on the request path are expensive exactly once per process and are
pure setup: importing the OpenAI SDK, and turning the 4.9 MB models.dev cache
into the indexes the routing ladder reads. Before 6.62.0 both were paid by
whichever request happened to arrive first, on the event loop, so that request
and every request concurrent with it stalled -- the in-process heartbeat
measured a single 2,170 ms gap for the SDK import alone.

Neither is moved earlier in the sense of blocking anything: the socket still
binds first (6.59.0), and this runs on a worker thread while the rest of
startup proceeds. A request that arrives before the thread finishes still does
the work itself and is correct; it simply stops being the common case.
"""

import importlib
import threading
import time

from loguru import logger

_lock = threading.Lock()
#: Whether this process has already started the warmup. One server start, one
#: thread; a module flag rather than a lock-guarded object because the only
#: caller is the single startup transition.
_STARTED = False


def _warm_openai_sdk() -> None:
    """Import the OpenAI SDK, which every OpenAI-compatible provider needs.

    ``providers/openai_chat/provider.py`` imports it lazily on purpose -- an
    install with no OpenAI-compatible provider configured should not pay for
    the Assistants type tree, and nothing between process launch and the first
    ``/health`` answer should either. That is still true. What changed is who
    pays when it *is* needed: a worker thread during startup rather than the
    first request's own event loop.
    """

    started = time.perf_counter()
    try:
        importlib.import_module("openai")
    except Exception as exc:
        # A missing or broken SDK is the provider's problem to report, with a
        # message about the provider. It is not a reason for a warmup to shout.
        logger.debug("The OpenAI SDK could not be pre-imported: {}", exc)
        return
    logger.debug(
        "WARMUP: openai SDK imported in {:.0f}ms",
        (time.perf_counter() - started) * 1000.0,
    )


def _warm_models_dev_indexes() -> None:
    """Build the models.dev indexes from the on-disk cache, once.

    Imported here rather than at module scope for the same reason the desktop
    updater is: a contract test asserts what building the ASGI app pulls in,
    and a post-readiness thread is not part of that answer.
    """

    started = time.perf_counter()
    try:
        from my_claude_code.providers.runtime.models_dev import (
            prewarm_models_dev_indexes,
        )

        built = prewarm_models_dev_indexes()
    except Exception as exc:
        logger.debug("The models.dev indexes could not be pre-built: {}", exc)
        return
    if not built:
        # No cache on disk yet: a fresh install, or an offline first start.
        # The background refresh writes one and the next start warms it.
        return
    logger.debug(
        "WARMUP: models.dev indexes built in {:.0f}ms",
        (time.perf_counter() - started) * 1000.0,
    )


def _warm() -> None:
    _warm_openai_sdk()
    _warm_models_dev_indexes()


def _spawn() -> None:
    """Start the warmup thread. A seam, so a test need not really spawn one."""

    threading.Thread(target=_warm, name="mcc-request-path-warmup", daemon=True).start()


def start_request_path_warmup() -> None:
    """Schedule the warmup on a daemon thread, once per server start."""

    global _STARTED
    with _lock:
        if _STARTED:
            return
        _STARTED = True
    _spawn()


def reset_request_path_warmup_for_tests() -> None:
    """Forget that the warmup has run. Tests only."""

    global _STARTED
    with _lock:
        _STARTED = False
