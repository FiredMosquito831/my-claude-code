"""Work the first request should not have to pay for.

Three things on the request path are expensive exactly once per process and
are pure setup: importing the OpenAI SDK, turning the 4.9 MB models.dev cache
into the indexes the routing ladder reads, and (7.39.0) the Unicode-property
tables the Responses tool-schema dialect translates ``\\p{...}`` with. Before
6.62.0 the first two were paid by whichever request happened to arrive first,
on the event loop, so that request and every request concurrent with it stalled -- the in-process heartbeat
measured a single 2,170 ms gap for the SDK import alone.

Neither is moved earlier in the sense of blocking anything: the socket still
binds first (6.59.0), and this runs on a worker thread while the rest of
startup proceeds. A request that arrives before the thread finishes still does
the work itself and is correct; it simply stops being the common case.
"""

import importlib
import threading
import time
from pathlib import Path

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


def _warm_models_dev_indexes(cache_path: Path | None) -> None:
    """Build the models.dev indexes from the on-disk cache, once.

    ``cache_path`` is resolved by the caller, on the thread that starts this
    one, and is never resolved here. See ``start_request_path_warmup``.

    Imported here rather than at module scope for the same reason the desktop
    updater is: a contract test asserts what building the ASGI app pulls in,
    and a post-readiness thread is not part of that answer.
    """

    if cache_path is None:
        # The path could not be resolved on the calling thread. Resolving it
        # here is exactly what this function must never do.
        return
    started = time.perf_counter()
    try:
        from my_claude_code.providers.runtime.models_dev import (
            prewarm_models_dev_indexes,
        )

        built = prewarm_models_dev_indexes(cache_path)
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


def _warm_unicode_property_ranges() -> None:
    """Build the Unicode-property tables the Responses dialect translates with.

    ``core/tool_schema_patterns`` expands ``\\p{Cc}``/``\\p{Cf}``/``\\p{Zl}``/
    ``\\p{Zp}`` from this interpreter's Unicode database by walking every code
    point once -- about 300 ms, measured. Without this, the first request whose
    tool catalogue carries such a pattern would pay that on the event loop.
    The tables are built under a lock, so a request that arrives mid-walk waits
    for this one rather than starting a second.
    """

    started = time.perf_counter()
    try:
        from my_claude_code.core.tool_schema_patterns import (
            warm_unicode_property_ranges,
        )

        warm_unicode_property_ranges()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("The Unicode-property tables could not be pre-built: {}", exc)
        return
    logger.debug(
        "WARMUP: Unicode-property tables built in {:.0f}ms",
        (time.perf_counter() - started) * 1000.0,
    )


def _warm(cache_path: Path | None) -> None:
    _warm_openai_sdk()
    _warm_models_dev_indexes(cache_path)
    _warm_unicode_property_ranges()


def _spawn(cache_path: Path | None) -> None:
    """Start the warmup thread. A seam, so a test need not really spawn one."""

    threading.Thread(
        target=_warm,
        args=(cache_path,),
        name="mcc-request-path-warmup",
        daemon=True,
    ).start()


def _resolve_models_dev_cache_path() -> Path | None:
    """Resolve the models.dev cache path, on the caller's thread.

    ``models_dev_cache_path()`` goes through ``config/paths.config_dir_path``,
    which resolves the config directory from the environment on first call and
    then caches the answer in a process-wide global. A daemon thread that
    resolves it is reading whatever ``HOME`` happens to be at the instant it
    ticks, and writing that answer where the whole process will read it --
    which is how an unjoined warmup thread poisoned an entire pytest worker
    after a hermetic fixture had restored the real ``HOME``.
    """

    try:
        from my_claude_code.providers.runtime.models_dev import models_dev_cache_path

        return models_dev_cache_path()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("The models.dev cache path could not be resolved: {}", exc)
        return None


def start_request_path_warmup() -> None:
    """Schedule the warmup on a daemon thread, once per server start.

    Every path the thread needs is bound *here*, on the calling thread, and
    handed to it. A background worker binds no paths of its own: the pattern
    ``core/request_log.py`` has used since it was written (``self._db_path``
    in ``__init__``) and the one 6.72.2 applied to the survey thread.
    """

    global _STARTED
    with _lock:
        if _STARTED:
            return
        _STARTED = True
    _spawn(_resolve_models_dev_cache_path())


def reset_request_path_warmup_for_tests() -> None:
    """Forget that the warmup has run. Tests only."""

    global _STARTED
    with _lock:
        _STARTED = False
