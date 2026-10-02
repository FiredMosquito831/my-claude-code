"""A console a human can read: timestamps on every line, pollers left out.

Two small, related things ``cli/commands.py`` hands to ``uvicorn.Config`` as
one ``log_config``:

* **Timestamps** (decision 8 / item 2). uvicorn's own ``default`` and
  ``access`` formatters print no clock at all -- every line before this module
  existed read like ``INFO:     Uvicorn running on ...`` with no way to tell
  when. This prepends the same local-time stamp shape
  ``runtime/listener_guard.py``'s own console line already uses, so a
  transcript made of both reads as one timeline.
* **A quiet console** (decision 10 / item 3). A dashboard left open polls
  ``/health``, the request-log pulse and the in-flight list every second or
  two, and a proxy-chain ingest panel does the same. A *successful* answer to
  one of those is not news -- the uvicorn access line it produces is filtered
  out of the console here, through a ``logging.Filter`` on the
  ``uvicorn.access`` handler, while the exact same line still reaches
  ``server.log`` untouched (the filter is console-only; it is never attached
  to any file sink). A non-2xx answer on any of these paths is kept, because a
  *failing* health probe or a broken poll is exactly the kind of thing the
  console exists to show. Every ``/v1/*`` line, every admin write, page loads
  and 401s are untouched -- this filter only ever looks at the small,
  declared list below.

``cli/commands.py`` is frozen, so the only change there is the one line that
hands ``uvicorn.Config`` this module's ``build_uvicorn_log_config()`` as
``log_config=``; everything that decides *what* the config looks like lives
here, where it can be tested without starting a server.
"""

import copy
import logging
from typing import Any

from uvicorn.config import LOGGING_CONFIG

#: The same stamp shape ``runtime/listener_guard.py`` already prints to the
#: console, so every console line a user sees -- uvicorn's or ours -- shares
#: one timeline.
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

#: GET paths whose 2xx answers are a poller talking to itself, not news.
#: Declared once, here, so the filter and anything that documents or tests it
#: reads from the same list. Matched by exact path or prefix (a prefix for
#: ``/admin/api/requests/in-flight``, which carries a query string the access
#: log renders inline, e.g. ``?limit=50``).
QUIET_POLL_PATHS: tuple[str, ...] = (
    "/health",
    "/admin/api/requests/pulse",
    "/admin/api/requests/in-flight",
    "/admin/api/proxy-chains/ingest/status",
)


def _is_quiet_poll_path(path: str) -> bool:
    return any(path == quiet or path.startswith(quiet) for quiet in QUIET_POLL_PATHS)


class QuietPollFilter(logging.Filter):
    """Drop a successful poll's access line; keep everything else.

    uvicorn's access line is logged as
    ``logger.info('%s - "%s %s HTTP/%s" %d', client_addr, method,
    path_with_query, http_version, status_code)`` (both the ``h11`` and
    ``httptools`` protocol implementations use this exact positional shape),
    so ``record.args`` is read directly rather than relying on a rendered
    message. A record this filter cannot make sense of -- wrong arity, a
    status that will not parse -- is kept: the filter's job is to recognise
    the boring case confidently, not to guess.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        _client_addr, method, path_with_query, _http_version, status = args[:5]
        if method != "GET":
            return True
        path = str(path_with_query).split("?", 1)[0]
        if not _is_quiet_poll_path(path):
            return True
        try:
            status_code = int(str(status))
        except TypeError, ValueError:
            return True
        return not (200 <= status_code < 300)


def build_uvicorn_log_config() -> dict[str, Any]:
    """uvicorn's own default log config, with a timestamp and a quiet filter.

    Starts from ``uvicorn.config.LOGGING_CONFIG`` (a deep copy -- the module
    dict is never mutated) so every formatter option, handler and logger
    uvicorn ships keeps working; only ``fmt``/``datefmt`` and the ``access``
    handler's filter list change.
    """

    config = copy.deepcopy(LOGGING_CONFIG)
    for formatter_name in ("default", "access"):
        formatter = config["formatters"][formatter_name]
        formatter["fmt"] = f"%(asctime)s {formatter['fmt']}"
        formatter["datefmt"] = TIMESTAMP_FORMAT
    config.setdefault("filters", {})["quiet_poll"] = {
        "()": f"{__name__}.QuietPollFilter"
    }
    config["handlers"]["access"].setdefault("filters", []).append("quiet_poll")
    return config


__all__ = [
    "QUIET_POLL_PATHS",
    "TIMESTAMP_FORMAT",
    "QuietPollFilter",
    "build_uvicorn_log_config",
]
