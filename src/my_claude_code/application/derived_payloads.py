"""Derived dashboard payloads that survive a restart.

The dashboard re-derives everything expensive from zero on every start. The
cost breakdown is the clearest case and the one this module is built around:
twelve seconds of six full scans over a multi-gigabyte log, recomputed after
every restart even when not one request has been logged in between.

The policy, in one sentence: **store the answer next to the fingerprint of the
data that produced it, serve the stored answer the instant that fingerprint
still matches, and when it does not, still answer immediately -- with the
stored payload, marked stale -- while a fresh one is computed off the request
path.**

Three properties this is written to have, and the tests hold each:

- A matching key is not a guess. ``RequestLogStore.data_mark`` changes on an
  insert, on a prune and on any migration marker, so an entry that matches is
  the same answer the raw query would give -- proven by the equality test that
  compares a cache hit against a fresh computation.
- Nothing blocks on a recomputation. A stale entry is served as it stands with
  ``stale: true`` and the time it was computed; the refresh runs on a worker
  thread and lands in the next request.
- A cold cache behaves exactly as every release before this one: compute, then
  answer. The first open on a fresh install is unchanged.

Where the layers sit: the key comes from ``core`` (it is a fact about the
database), the store is a ``core`` module that takes a path, and the decision
about *which* directory -- the config dir's ``cache/derived`` -- is made here,
because ``core`` may not import ``config``.
"""

import threading
import time
from collections.abc import Callable
from typing import Any

from loguru import logger

from my_claude_code.config.paths import config_dir_path
from my_claude_code.core.derived_cache import DerivedCache, DerivedEntry
from my_claude_code.core.request_log import RequestLogStore

#: The subdirectory of the config dir these documents live in. Beside
#: ``models-dev.json`` and the LiteLLM price index, which are the other two
#: derived things already kept on disk.
DERIVED_CACHE_DIRNAME = "cache"
DERIVED_CACHE_SUBDIR = "derived"

_refresh_lock = threading.Lock()
_refreshing: set[str] = set()


def derived_cache() -> DerivedCache:
    """The process's derived-payload store, under the *resolved* config dir.

    Resolved on every call rather than cached in a module global: the config
    directory is itself resolved per process and the hermetic test guard
    redirects it, and a global captured at import time would write into whatever
    home the first import happened to see.
    """

    return DerivedCache(
        config_dir_path() / DERIVED_CACHE_DIRNAME / DERIVED_CACHE_SUBDIR
    )


def _mark_fresh(payload: dict[str, Any], entry: DerivedEntry) -> dict[str, Any]:
    payload["stale"] = False
    payload["computed_at"] = entry.computed_at
    return payload


def _mark_stale(payload: dict[str, Any], entry: DerivedEntry) -> dict[str, Any]:
    payload["stale"] = True
    payload["computed_at"] = entry.computed_at
    return payload


def _start_refresh(
    name: str,
    key: str,
    compute: Callable[[], dict[str, Any]],
    cache: DerivedCache,
    compact: bool,
) -> None:
    """Recompute ``name`` on a worker thread, at most one at a time.

    At most one because the page polls: without the guard, three refreshes of a
    twelve-second payload would be in flight at once after a single reload, all
    of them computing the same answer.
    """

    with _refresh_lock:
        if name in _refreshing:
            return
        _refreshing.add(name)

    def run() -> None:
        try:
            payload = compute()
            cache.write(
                name,
                key=key,
                payload=payload,
                computed_at=time.time(),
                compact=compact,
            )
        except Exception as exc:
            logger.warning("Derived payload {} could not be refreshed: {}", name, exc)
        finally:
            with _refresh_lock:
                _refreshing.discard(name)

    threading.Thread(target=run, name=f"mcc-derived-{name}", daemon=True).start()


def cached_payload(
    name: str,
    *,
    key: str,
    compute: Callable[[], dict[str, Any]],
    cache: DerivedCache | None = None,
    compact: bool = False,
    serve_stale: bool = True,
) -> dict[str, Any]:
    """Answer from the stored payload where possible, never by waiting.

    Returns a payload carrying two extra fields, and only those two: ``stale``
    and ``computed_at``. Everything else is exactly what ``compute`` produced,
    on this call or on an earlier one under the same key.

    ``serve_stale=False`` for a payload that must never lag its inputs. The
    cost breakdown may show figures from a minute ago and say so; a page that
    renders a setting the reader just changed may not, because the stale answer
    would look like the write failed. Such an entry still skips the whole
    computation whenever the key matches -- which is the restart case this
    exists for -- and simply recomputes when it does not.
    """

    store = derived_cache() if cache is None else cache
    entry = store.read(name)
    if entry is not None and entry.matches(key) and isinstance(entry.payload, dict):
        return _mark_fresh(dict(entry.payload), entry)
    if serve_stale and entry is not None and isinstance(entry.payload, dict):
        # Something changed. The stored answer is still an answer -- it was
        # true at ``computed_at`` -- and the page says so rather than making
        # the reader wait twelve seconds for a number that moved by one
        # request.
        _start_refresh(name, key, compute, store, compact)
        return _mark_stale(dict(entry.payload), entry)
    payload = compute()
    computed_at = time.time()
    store.write(
        name, key=key, payload=payload, computed_at=computed_at, compact=compact
    )
    result = dict(payload)
    result["stale"] = False
    result["computed_at"] = computed_at
    return result


def cost_breakdown_cache_key(store: RequestLogStore, **filters: Any) -> str:
    """The key for one filtered cost breakdown.

    The filters are part of the key for the obvious reason -- a breakdown of
    one provider is not a breakdown of all of them -- and the log's data mark
    is the rest of it.
    """

    parts = [store.data_mark()]
    for name in sorted(filters):
        value = filters[name]
        parts.append(f"{name}={'' if value is None else value}")
    return "|".join(parts)


#: The only ``local`` values the dashboard itself sends. A stored document per
#: value, because the page's own default is ``hide`` and one shared file would
#: have the two answers overwriting each other on every switch.
_CACHED_LOCAL_VALUES = {None: "cost-breakdown", "hide": "cost-breakdown-local-hide"}


def cost_breakdown_entry_name(**filters: Any) -> str | None:
    """The file this cost breakdown is stored under, or ``None`` to not store it.

    Only the breakdown the page *opens* with is kept on disk: no filters except
    the ``local`` selector the dashboard defaults to. One document per filter
    combination would fill the config directory with answers nobody asks for
    twice, and the five-second in-memory cache already covers a reader flipping
    between filters. The one that costs twelve seconds after a restart is this
    one.
    """

    local = filters.get("local")
    if local not in _CACHED_LOCAL_VALUES:
        return None
    for name, value in filters.items():
        if name in {"local", "limit"}:
            continue
        if value is not None:
            return None
    return _CACHED_LOCAL_VALUES[local]
