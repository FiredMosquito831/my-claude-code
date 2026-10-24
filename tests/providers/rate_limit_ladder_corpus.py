"""The retry ladder's decision table (RATE-LIMIT-VALUEERROR, 7.111.2).

``ProviderRateLimiter.execute_with_retry`` decides, for every failed try,
whether to try the same key again, to raise (so the pool rotates or the
executor moves on), and how long to block and sleep in between. This module
enumerates every input that loop reads and records what it did:

* the failure -- a ``429`` carrying each wait source a host publishes
  (``Retry-After`` seconds, an http-date, ``retry-after-ms``,
  ``x-ratelimit-reset-*``, a body ``retryAfter``, none), as an SDK error, an
  ``httpx`` error and a classified ``ExecutionFailure``; ``5xx``; overloaded;
  transport; timeout; ``400`` / ``401`` / ``402`` / ``403``; a plain bug;
* whether it repeats on every try or clears on the next one;
* ``RATE_LIMIT_COOLDOWN_MODE`` / seconds / max;
* ``RATE_LIMIT_ROUTES_AROUND_MODEL`` on and off;
* ``PROVIDER_RETRY_ATTEMPTS`` 0-3, turned into ``max_retries`` exactly the way
  ``providers/runtime/factory.py`` does;
* the backoff base / max / jitter at 0 and at the shipped defaults, and the
  jitter draw at both ends of its range;
* the limiter class: the plain one, and a proxied leg unarmed and armed with
  the exit stops of a ticked chain (7.81.0).

Per cell it records the calls made, every ladder try and wait, every reactive
block, every sleep, every scheduled retry and the outcome. Time, sleep and
the jitter draw are replaced by deterministic stand-ins, so one run takes
seconds and every record is reproducible.

``rate_limit_ladder_baseline.json`` was written by this module run with the
v7.111.1 code (``c7265d49``), before the fix::

    uv run --offline python tests/providers/rate_limit_ladder_corpus.py \
        tests/providers/rate_limit_ladder_baseline.json
"""

import asyncio
import copy
import hashlib
import itertools
import json
import sys
import types
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import openai

import my_claude_code
from my_claude_code.config.constants import (
    PROVIDER_RATE_LIMIT_DEFAULT,
    PROVIDER_RATE_WINDOW_DEFAULT,
)
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.rate_limit import RateLimitCooldown
from my_claude_code.providers import rate_limit as ladder
from my_claude_code.providers.failure_policy import overloaded_provider_failure
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.runtime.proxy_leg import ProxiedLegRateLimiter

#: The smallest positive backoff the table compares a zero one against.
EPSILON = 1e-6
_PLACES = 9
_URL = "https://upstream.test/v1/chat/completions"
_HTTP_DATE = "Wed, 21 Oct 2037 07:28:00 GMT"


def _request() -> httpx.Request:
    return httpx.Request("POST", _URL)


def _response(
    status: int, headers: dict[str, str] | None = None, body: Any = None
) -> httpx.Response:
    if body is None:
        return httpx.Response(status, headers=headers or {}, request=_request())
    return httpx.Response(status, headers=headers or {}, json=body, request=_request())


def _sdk(
    cls: type[openai.APIStatusError],
    status: int,
    headers: dict[str, str] | None = None,
    body: Any = None,
) -> Callable[[], BaseException]:
    def build() -> BaseException:
        return cls(
            "upstream said no",
            response=_response(status, headers, body),
            body=body,
        )

    return build


def _httpx(
    status: int, headers: dict[str, str] | None = None
) -> Callable[[], BaseException]:
    def build() -> BaseException:
        return httpx.HTTPStatusError(
            "upstream said no",
            request=_request(),
            response=_response(status, headers, {"error": {"message": "no"}}),
        )

    return build


def _rate_limited_failure(retryable: bool) -> Callable[[], BaseException]:
    def build() -> BaseException:
        return ExecutionFailure(
            kind=FailureKind.RATE_LIMIT,
            status_code=429,
            message="Provider rate limit reached. Please retry shortly.",
            retryable=retryable,
            retry_after_seconds=15.0,
        )

    return build


_BODY_WAIT = {
    "error": {"type": "rate_limit_error", "message": "Too many requests"},
    "retryAfter": 30,
}

#: Every failure shape the loop is asked about, by name.
ERRORS: dict[str, Callable[[], BaseException]] = {
    "429/sdk-retry-after-15": _sdk(openai.RateLimitError, 429, {"retry-after": "15"}),
    "429/sdk-retry-after-0": _sdk(openai.RateLimitError, 429, {"retry-after": "0"}),
    "429/sdk-retry-after-http-date": _sdk(
        openai.RateLimitError, 429, {"retry-after": _HTTP_DATE}
    ),
    "429/sdk-retry-after-ms-1500": _sdk(
        openai.RateLimitError, 429, {"retry-after-ms": "1500"}
    ),
    "429/sdk-x-ratelimit-reset-requests-2s": _sdk(
        openai.RateLimitError, 429, {"x-ratelimit-reset-requests": "2s"}
    ),
    "429/sdk-body-retryAfter-30": _sdk(openai.RateLimitError, 429, None, _BODY_WAIT),
    "429/sdk-none": _sdk(openai.RateLimitError, 429),
    "429/httpx-retry-after-15": _httpx(429, {"retry-after": "15"}),
    "429/httpx-none": _httpx(429),
    "429/failure-retryable": _rate_limited_failure(True),
    "429/failure-not-retryable": _rate_limited_failure(False),
    "500/sdk-internal": _sdk(openai.InternalServerError, 500),
    "502/httpx": _httpx(502),
    "503/httpx": _httpx(503),
    "504/sdk-status": _sdk(openai.APIStatusError, 504),
    "529/overloaded-failure": overloaded_provider_failure,
    "overloaded/text": lambda: RuntimeError("upstream overloaded_error"),
    "transport/httpx-connect": lambda: httpx.ConnectError(
        "refused", request=_request()
    ),
    "transport/sdk-connection": lambda: openai.APIConnectionError(request=_request()),
    "timeout/httpx-read": lambda: httpx.ReadTimeout("slow", request=_request()),
    "timeout/sdk": lambda: openai.APITimeoutError(request=_request()),
    "400/sdk-bad-request": _sdk(openai.BadRequestError, 400),
    "401/sdk-auth": _sdk(openai.AuthenticationError, 401),
    "402/sdk-status": _sdk(openai.APIStatusError, 402),
    "403/sdk-permission": _sdk(openai.PermissionDeniedError, 403),
    "other/runtime-error": lambda: RuntimeError("a bug, not an upstream"),
}

#: How many tries fail before one succeeds; ``None`` = every try fails.
SEQUENCES: dict[str, int | None] = {"always": None, "once-then-ok": 1}

#: ``RATE_LIMIT_COOLDOWN_MODE`` / ``_SECONDS`` / ``_MAX_SECONDS``.
COOLDOWNS: dict[str, RateLimitCooldown] = {
    "provider-60-3600": RateLimitCooldown(
        mode="provider", fallback_seconds=60.0, max_seconds=3600.0
    ),
    "provider-0-0": RateLimitCooldown(
        mode="provider", fallback_seconds=0.0, max_seconds=0.0
    ),
    "fixed-7-3600": RateLimitCooldown(
        mode="fixed", fallback_seconds=7.0, max_seconds=3600.0
    ),
    "fixed-0-3600": RateLimitCooldown(
        mode="fixed", fallback_seconds=0.0, max_seconds=3600.0
    ),
    "off-60-3600": RateLimitCooldown(
        mode="off", fallback_seconds=60.0, max_seconds=3600.0
    ),
}

ROUTES_AROUND = (False, True)
RETRY_ATTEMPTS = (0, 1, 2, 3)

#: (base, max, jitter): zero, the shipped defaults (2 / 5 / 0.5), and mixes.
BACKOFFS: tuple[tuple[float, float, float], ...] = (
    (0.0, 0.0, 0.0),
    (0.0, 5.0, 0.0),
    (2.0, 0.0, 0.0),
    (2.0, 5.0, 0.0),
    (0.0, 0.0, 0.5),
    (0.0, 5.0, 0.5),
    (2.0, 5.0, 0.5),
)

#: Where in ``[0, jitter]`` the draw lands; only read when jitter > 0.
DRAWS = (0.0, 0.5)

LIMITERS = ("plain", "proxied-leg", "proxied-leg-armed")


class _Cell:
    """One recording in progress: the deterministic clock and what happened."""

    def __init__(self) -> None:
        self.reset(draw=0.0, epsilon=0.0)

    def reset(self, *, draw: float, epsilon: float) -> None:
        self.now = 1_000.0
        self.draw = draw
        self.epsilon = epsilon
        self.events: dict[str, list[Any]] = {
            "tries": [],
            "blocks": [],
            "sleeps": [],
            "upstream_waits": [],
            "limiter_waits": [],
            "credited": [],
            "scheduled": [],
        }


_CURRENT = _Cell()
_REAL_SLEEP = asyncio.sleep
_ORIGINAL_EXTEND = ProviderRateLimiter.extend_reactive_block


def _r(value: float) -> float:
    return round(float(value), _PLACES)


def _monotonic() -> float:
    return _CURRENT.now


async def _sleep(seconds: float) -> None:
    _CURRENT.events["sleeps"].append(_r(seconds))
    # Still a real suspension point: a proxied leg's exit stop is delivered
    # as a cancellation at exactly this await, as it is in production -- and,
    # as there, a sleep cut short spends none of its seconds.
    await _REAL_SLEEP(0)
    _CURRENT.now += max(0.0, seconds)


def _uniform(low: float, high: float) -> float:
    return low + (high - low) * _CURRENT.draw + _CURRENT.epsilon


def _extend(self: ProviderRateLimiter, seconds: float) -> None:
    _CURRENT.events["blocks"].append(_r(seconds))
    _ORIGINAL_EXTEND(self, seconds)


def _record_try(**kwargs: Any) -> None:
    _CURRENT.events["tries"].append(
        [
            kwargs.get("status"),
            kwargs.get("kind"),
            kwargs.get("error_kind"),
            kwargs.get("retry_after"),
        ]
    )


def _record_wait(seconds: float, **_: Any) -> None:
    _CURRENT.events["upstream_waits"].append(_r(seconds))


def _record_limiter_wait(seconds: float) -> None:
    _CURRENT.events["limiter_waits"].append(_r(seconds))


def _credit(seconds: float) -> None:
    _CURRENT.events["credited"].append(_r(seconds))


def _trace(**kwargs: Any) -> None:
    if kwargs.get("event") == "provider.retry.scheduled":
        _CURRENT.events["scheduled"].append(
            [kwargs.get("attempt"), kwargs.get("max_attempts"), kwargs.get("delay_s")]
        )


class _QuietLogger:
    def __getattr__(self, _name: str) -> Callable[..., None]:
        return lambda *_args, **_kwargs: None


@contextmanager
def deterministic_ladder() -> Iterator[None]:
    """Replace time, sleep, the draw and the recorders on the ladder module.

    Only names the loop *calls* are replaced, and each recorder calls nothing
    the loop's decision reads; ``extend_reactive_block`` still runs the real
    method (and still raises where it raises).
    """

    fake_time = types.SimpleNamespace(monotonic=_monotonic)
    fake_asyncio = types.SimpleNamespace(sleep=_sleep, Semaphore=asyncio.Semaphore)
    fake_random = types.SimpleNamespace(uniform=_uniform)
    with ExitStack() as stack:
        for name, value in (
            ("time", fake_time),
            ("asyncio", fake_asyncio),
            ("random", fake_random),
            ("logger", _QuietLogger()),
            ("record_upstream_try", _record_try),
            ("record_upstream_wait", _record_wait),
            ("record_limiter_wait", _record_limiter_wait),
            ("credit_waiting", _credit),
            ("trace_event", _trace),
        ):
            stack.enter_context(patch.object(ladder, name, value))
        stack.enter_context(
            patch.object(ProviderRateLimiter, "extend_reactive_block", _extend)
        )
        yield


def cell_key(
    limiter: str,
    error: str,
    sequence: str,
    cooldown: str,
    routes_around: bool,
    attempts: int,
    backoff: tuple[float, float, float],
    draw: float,
) -> str:
    base, cap, jitter = backoff
    return (
        f"{limiter}|{error}|{sequence}|cooldown={cooldown}"
        f"|routes_around={'on' if routes_around else 'off'}"
        f"|attempts={attempts}|backoff={base:g},{cap:g},{jitter:g}|draw={draw:g}"
    )


def cells() -> Iterator[tuple[str, dict[str, Any]]]:
    """Every combination, keyed; the draw is varied only when jitter > 0."""

    for (
        limiter,
        error,
        sequence,
        cooldown,
        routes,
        attempts,
        backoff,
    ) in itertools.product(
        LIMITERS,
        ERRORS,
        SEQUENCES,
        COOLDOWNS,
        ROUTES_AROUND,
        RETRY_ATTEMPTS,
        BACKOFFS,
    ):
        for draw in DRAWS if backoff[2] > 0 else (0.0,):
            spec = {
                "limiter": limiter,
                "error": error,
                "sequence": sequence,
                "cooldown": cooldown,
                "routes_around": routes,
                "attempts": attempts,
                "backoff": backoff,
                "draw": draw,
            }
            yield cell_key(**spec), spec


def _build_limiter(spec: dict[str, Any]) -> ProviderRateLimiter:
    base, cap, jitter = spec["backoff"]
    kwargs: dict[str, Any] = {
        "rate_limit": PROVIDER_RATE_LIMIT_DEFAULT,
        "rate_window": PROVIDER_RATE_WINDOW_DEFAULT,
        # ``factory.py``: PROVIDER_RETRY_ATTEMPTS counts tries, the limiter
        # counts retries after the first.
        "max_retries": max(0, spec["attempts"] - 1),
        "backoff_base_seconds": base,
        "backoff_max_seconds": cap,
        "backoff_jitter_seconds": jitter,
        "routes_around_model": spec["routes_around"],
        "cooldown": COOLDOWNS[spec["cooldown"]],
    }
    if spec["limiter"] == "plain":
        return ProviderRateLimiter(**kwargs)
    leg = ProxiedLegRateLimiter(**kwargs)
    if spec["limiter"] == "proxied-leg-armed":
        leg.arm_exit_stops(transport=True, rate_limit=True)
    return leg


async def _record_cell(spec: dict[str, Any], *, epsilon: float) -> dict[str, Any]:
    _CURRENT.reset(draw=spec["draw"], epsilon=epsilon)
    limiter = _build_limiter(spec)
    build = ERRORS[spec["error"]]
    fails = SEQUENCES[spec["sequence"]]
    raised: list[BaseException] = []

    async def call() -> str:
        if fails is None or len(raised) < fails:
            error = build()
            raised.append(error)
            raise error
        return "served"

    try:
        outcome = f"ok:{await limiter.execute_with_retry(call)}"
        which = None
    except BaseException as exc:
        outcome = f"raise:{type(exc).__name__}"
        which = next((i for i, e in enumerate(raised) if e is exc), -1)
    task = asyncio.current_task()
    record: dict[str, Any] = dict(_CURRENT.events)
    record["calls"] = len(raised) + (1 if outcome.startswith("ok:") else 0)
    record["outcome"] = outcome
    record["raised_try"] = which
    record["blocked_for"] = _r(limiter.remaining_wait())
    record["pending_cancel"] = 0 if task is None else task.cancelling()
    return record


async def _record_all(
    epsilon: float, only: frozenset[str] | None
) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for key, spec in cells():
        if only is not None and key not in only:
            continue
        # A task of its own: a proxied leg cancels the task it runs in.
        records[key] = await asyncio.create_task(_record_cell(spec, epsilon=epsilon))
    return records


def snapshot(
    *, epsilon: float = 0.0, only: frozenset[str] | None = None
) -> dict[str, dict[str, Any]]:
    """Record every cell (or ``only`` those); ``epsilon`` is added to every delay."""

    with deterministic_ladder():
        return asyncio.run(_record_all(epsilon, only))


def crashed(record: dict[str, Any]) -> bool:
    """The defect: the ladder's own block raised out of the request."""

    return record["outcome"] == "raise:ValueError"


def without_the_sleep(record: dict[str, Any]) -> dict[str, Any]:
    """An epsilon-backoff record with its epsilon of blocking and sleeping removed.

    What a zero backoff must equal: the same tries, retries and outcome as the
    smallest positive backoff, with no reactive block (there is nothing to
    block for) and every epsilon sleep, wait and credit read as zero.
    """

    tiny = _r(EPSILON)
    out = copy.deepcopy(record)
    out["blocks"] = [b for b in out["blocks"] if b != tiny]
    for name in ("sleeps", "upstream_waits", "credited"):
        out[name] = [0.0 if v == tiny else v for v in out[name]]
    if out["blocked_for"] == tiny:
        # A proxied leg's exit stop cuts the epsilon sleep short, leaving the
        # epsilon block it installed outstanding.
        out["blocked_for"] = 0.0
    return out


def record_digest(record: dict[str, Any]) -> str:
    text = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def keys_digest(keys: list[str]) -> str:
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()


def compact(records: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The baseline document.

    The distinct records are stored once, readable, in first-seen order of the
    sorted keys; ``cells`` holds one index into them per key, in that order.
    The keys themselves are regenerated from :func:`cells` and pinned by
    digest, so a moved cell is located by its index.
    """

    keys = sorted(records)
    distinct: list[dict[str, Any]] = []
    index: dict[str, int] = {}
    cell_indices: list[int] = []
    for key in keys:
        digest = record_digest(records[key])
        if digest not in index:
            index[digest] = len(distinct)
            distinct.append(records[key])
        cell_indices.append(index[digest])
    return {
        "count": len(keys),
        "keys_sha256": keys_digest(keys),
        "crashed": [i for i, key in enumerate(keys) if crashed(records[key])],
        "records": distinct,
        "cells": cell_indices,
    }


def expand(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The baseline back as ``{key: record}``."""

    keys = sorted(key for key, _spec in cells())
    assert len(keys) == document["count"]
    assert keys_digest(keys) == document["keys_sha256"]
    records = document["records"]
    return {key: records[i] for key, i in zip(keys, document["cells"], strict=True)}


def main(argv: list[str]) -> int:
    target = Path(argv[1])
    records = snapshot()
    document = compact(records)
    target.write_text(
        json.dumps(document, separators=(",", ":"), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"package={Path(my_claude_code.__file__).parent} cells={document['count']}"
        f" distinct={len(document['records'])} crashed={len(document['crashed'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
