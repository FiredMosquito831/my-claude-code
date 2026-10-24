"""The chat side of the search-vs-chat parity contract (7.112.0).

``test_search_chat_parity.py`` asserts that a search provider's keys, driven by
the web search chain's books (``api/web_tools/search_books.py``) and its key
walk (``api/web_tools/search_chain.drive_keys``), decide exactly what a model
provider's credential pool decides on the same upstream answers -- the
safeguard the user required for media (decision 4, 2026-09-26 03:38) and
again here (decision Q10, 2026-10-25).

This module is the CHAT half only, and it imports nothing 7.112.0 added. That
is what lets the baseline be generated on the base tree: run as a script with
the base worktree's interpreter it writes ``search_chat_parity_baseline.json``::

    <base>/.venv/Scripts/python.exe -I tests/contracts/search_chat_parity_world.py OUT.json

The chat stack is the model path's own frozen objects, unmodified:
``RotatingProvider`` over one sub-provider per key, each sub-provider's call
running inside its own ``ProviderRateLimiter.execute_with_retry`` and failing
through ``classify_provider_failure`` -- with ``mark_rate_limited`` wired to
the limiter's reactive block, as a model provider does with
``RATE_LIMIT_ROUTES_AROUND_MODEL`` off (the user's value) -- and one
``CredentialRotationState`` for the pool.

A cell is one upstream answer script on one pool under one set of the
operator's settings. Its record says what the stack decided: which key was
tried how often, how long it slept between tries, how the request ended, what
each failure charged, how long each key stays benched for the model in hand,
each key's own limiter window, whether the failure kind ends the route under
the default ``FALLBACK_SKIP_KINDS``, and what the next request on the same
pool does once the upstream answers again.

Time: the limiter's ``time``, ``asyncio.sleep`` and jitter are replaced for the
duration of a cell (``fake_limiter_time``) so a backoff is recorded rather
than slept and every number is deterministic. The rotation engine keeps its
real clock; nothing in a cell waits on it, and its windows are read back
rounded to the second.
"""

import asyncio
import contextlib
import json
import sys
import types
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger

from my_claude_code.application.errors import ApplicationUnavailableError
from my_claude_code.config.constants import (
    CREDENTIAL_LOCKOUT_TIERS_DEFAULT,
    PROVIDER_RETRY_BACKOFF_BASE_SECONDS_DEFAULT,
    PROVIDER_RETRY_BACKOFF_JITTER_SECONDS_DEFAULT,
    PROVIDER_RETRY_BACKOFF_MAX_SECONDS_DEFAULT,
    RATE_LIMIT_COOLDOWN_SECONDS_DEFAULT,
)
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.failures import FailureKind, failure_kind
from my_claude_code.core.rate_limit import (
    MAX_RATE_LIMIT_COOLDOWN_SECONDS,
    UNLIMITED_RATE_LIMIT,
    RateLimitCooldown,
)
from my_claude_code.core.upstream_ladder import install_ladder_trace
from my_claude_code.providers import rate_limit as rate_limit_module
from my_claude_code.providers.base import BaseProvider, ProviderConfig
from my_claude_code.providers.credential_rotation import CredentialRotationState
from my_claude_code.providers.failure_policy import classify_provider_failure
from my_claude_code.providers.rate_limit import ProviderRateLimiter
from my_claude_code.providers.runtime.rotating import RotatingProvider

PARITY_MODEL = "parity-model"
BASELINE_NAME = "search_chat_parity_baseline.json"

#: The model path's default ``FALLBACK_SKIP_KINDS``.
DEFAULT_SKIP_KINDS = frozenset({FailureKind.INVALID_REQUEST})

_NO_CONCURRENCY_CAP = 2**31 - 1


# ------------------------------------------------------------- the answers


@dataclass(frozen=True, slots=True)
class Answer:
    """One upstream answer: an HTTP status, or a transport-level fault."""

    name: str
    kind: str = "status"  # status | ok | timeout | connect | bad_json
    status: int = 200
    body: Any = None
    headers: tuple[tuple[str, str], ...] = ()


OK = Answer("ok", kind="ok")


def _error_body(message: str) -> dict[str, Any]:
    return {"error": {"message": message}}


ANSWERS: tuple[Answer, ...] = (
    Answer("s400", status=400, body=_error_body("unsupported parameter: depth")),
    Answer("s400_credits", status=400, body=_error_body("insufficient credits")),
    Answer("s401", status=401, body=_error_body("invalid api key")),
    Answer("s402_bare", status=402, body=_error_body("plan limit reached")),
    Answer("s402_credits", status=402, body=_error_body("insufficient credits")),
    Answer("s403", status=403, body=_error_body("forbidden")),
    Answer("s403_credits", status=403, body=_error_body("out of credits")),
    # A country refusal: rotates, and charges the key nothing (7.96.0).
    Answer(
        "s403_region",
        status=403,
        body=_error_body("This service is not available in your country"),
    ),
    Answer("s404", status=404, body=_error_body("not found")),
    Answer("s422", status=422, body=_error_body("unprocessable")),
    Answer("s429", status=429, body=_error_body("too many requests")),
    Answer(
        "s429_ra7",
        status=429,
        body=_error_body("too many requests"),
        headers=(("Retry-After", "7"),),
    ),
    Answer(
        "s429_body120",
        status=429,
        body={"error": {"message": "slow down", "retryAfter": 120}},
    ),
    Answer("s500", status=500, body=_error_body("internal error")),
    Answer("s502", status=502, body=_error_body("bad gateway")),
    Answer("s503", status=503, body=_error_body("unavailable")),
    Answer("s504", status=504, body=_error_body("gateway timeout")),
    Answer("timeout", kind="timeout"),
    Answer("connect", kind="connect"),
    Answer("bad_json", kind="bad_json"),
)


# --------------------------------------------------------------- the cells


@dataclass(frozen=True, slots=True)
class Cell:
    answer: Answer
    #: ``same``: every try meets the answer. ``then_ok``: the first try on a
    #: failing key meets it, every later try is answered.
    sequence: str
    mode: str  # RATE_LIMIT_COOLDOWN_MODE
    policy: str  # {ENV}_ROTATION
    keys: int
    #: ``first``: only key 0 fails. ``all``: every key fails the same way.
    layout: str
    attempts: int  # PROVIDER_RETRY_ATTEMPTS
    backoff: str  # default | zero

    @property
    def cell_id(self) -> str:
        return "|".join(
            (
                self.answer.name,
                self.sequence,
                self.mode,
                self.policy,
                f"k{self.keys}",
                self.layout,
                f"a{self.attempts}",
                self.backoff,
            )
        )

    @property
    def backoff_seconds(self) -> tuple[float, float, float]:
        if self.backoff == "zero":
            return (0.0, 0.0, 0.0)
        return (
            PROVIDER_RETRY_BACKOFF_BASE_SECONDS_DEFAULT,
            PROVIDER_RETRY_BACKOFF_MAX_SECONDS_DEFAULT,
            PROVIDER_RETRY_BACKOFF_JITTER_SECONDS_DEFAULT,
        )

    def cooldown(self) -> RateLimitCooldown:
        return RateLimitCooldown(
            mode=self.mode,
            fallback_seconds=RATE_LIMIT_COOLDOWN_SECONDS_DEFAULT,
            max_seconds=MAX_RATE_LIMIT_COOLDOWN_SECONDS,
        )

    def failing(self, key: int) -> bool:
        return self.layout == "all" or key == 0


_POOLS: tuple[tuple[int, str, str], ...] = (
    (1, "failover", "first"),
    *(
        (3, policy, layout)
        for policy in ("single", "round_robin", "least_used", "failover")
        for layout in ("first", "all")
    ),
)


def cells() -> tuple[Cell, ...]:
    return tuple(
        Cell(answer, sequence, mode, policy, keys, layout, attempts, backoff)
        for answer in ANSWERS
        for sequence in ("same", "then_ok")
        for mode in ("provider", "fixed", "off")
        for keys, policy, layout in _POOLS
        for attempts in (1, 3)
        for backoff in ("default", "zero")
    )


LOCKOUT_TIERS: tuple[float, ...] = tuple(
    float(part) for part in CREDENTIAL_LOCKOUT_TIERS_DEFAULT.split(",")
)
MODEL_BENCH_ESCALATION = 2


class Script:
    """Answers per key, in order; records every call."""

    def __init__(self, cell: Cell) -> None:
        self._cell = cell
        self._served: dict[int, int] = {}
        self.calls: list[tuple[int, int]] = []
        self.recovered = False

    def next_answer(self, key: int) -> Answer:
        count = self._served.get(key, 0) + 1
        self._served[key] = count
        self.calls.append((key, count))
        if self.recovered or not self._cell.failing(key):
            return OK
        if self._cell.sequence == "then_ok" and count > 1:
            return OK
        return self._cell.answer

    def recover(self) -> None:
        """The upstream answers from now on (the "next request" leg)."""
        self.recovered = True
        self.calls = []
        self._served = {}


# -------------------------------------------------------------- the clock


class FakeLimiterClock:
    """The limiter's clock and sleep: a sleep is recorded, then advances it."""

    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []
        self._real_sleep = asyncio.sleep

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        # Yield first, then advance: a cancelled sleep must not read as spent.
        await self._real_sleep(0)
        self.slept.append(round(float(seconds), 3))
        self.now += float(seconds)


@contextlib.contextmanager
def fake_limiter_time() -> Iterator[FakeLimiterClock]:
    """Swap ``providers/rate_limit.py``'s clock, sleep and jitter for one cell.

    Module globals only: the event loop, the rotation engine and everything
    else keep the real ones. The jitter draws its maximum, so a non-zero
    jitter is still deterministic.
    """
    clock = FakeLimiterClock()
    saved = (
        rate_limit_module.time,
        rate_limit_module.asyncio,
        rate_limit_module.random,
    )
    rate_limit_module.__dict__["time"] = types.SimpleNamespace(
        monotonic=clock.monotonic
    )
    rate_limit_module.__dict__["asyncio"] = types.SimpleNamespace(
        sleep=clock.sleep, Semaphore=asyncio.Semaphore
    )
    rate_limit_module.__dict__["random"] = types.SimpleNamespace(
        uniform=lambda low, high: high
    )
    try:
        yield clock
    finally:
        (
            rate_limit_module.__dict__["time"],
            rate_limit_module.__dict__["asyncio"],
            rate_limit_module.__dict__["random"],
        ) = saved


def build_limiter(cell: Cell, cooldown: RateLimitCooldown) -> ProviderRateLimiter:
    """A key's limiter, as the factory builds one for a single-key leaf."""
    base, ceiling, jitter = cell.backoff_seconds
    return ProviderRateLimiter(
        rate_limit=UNLIMITED_RATE_LIMIT,
        max_concurrency=_NO_CONCURRENCY_CAP,
        max_retries=max(0, cell.attempts - 1),
        backoff_base_seconds=base,
        backoff_max_seconds=ceiling,
        backoff_jitter_seconds=jitter,
        routes_around_model=False,
        cooldown=cooldown,
    )


def build_state(cell: Cell, cooldown: RateLimitCooldown) -> CredentialRotationState:
    return CredentialRotationState(
        cell.keys,
        cell.policy,
        rate_limit_seconds=RATE_LIMIT_COOLDOWN_SECONDS_DEFAULT,
        lockout_tiers=LOCKOUT_TIERS,
        model_bench_escalation=MODEL_BENCH_ESCALATION,
        cooldown=cooldown,
    )


# ---------------------------------------------------------- the chat stack


_REQUEST = httpx.Request("POST", "http://parity.invalid/v1/chat/completions")


def raise_chat_answer(answer: Answer) -> None:
    """What a model provider's HTTP call raises for ``answer``."""
    if answer.kind == "ok":
        return
    if answer.kind == "timeout":
        raise httpx.ReadTimeout("timed out", request=_REQUEST)
    if answer.kind == "connect":
        raise httpx.ConnectError("connection refused", request=_REQUEST)
    if answer.kind == "bad_json":
        raise json.JSONDecodeError("Expecting value", "<<not json>>", 0)
    response = httpx.Response(
        answer.status,
        headers=dict(answer.headers),
        content=json.dumps(answer.body).encode(),
        request=_REQUEST,
    )
    raise httpx.HTTPStatusError(
        f"HTTP {answer.status}", request=_REQUEST, response=response
    )


class ChatLeaf(BaseProvider):
    """One credential's sub-provider: the limiter ladder, then classification."""

    def __init__(
        self,
        index: int,
        script: Script,
        limiter: ProviderRateLimiter,
        cooldown: RateLimitCooldown,
    ) -> None:
        super().__init__(
            ProviderConfig(
                api_key=f"chat-key-{index}", base_url="http://parity.invalid"
            )
        )
        self._index = index
        self._script = script
        self._limiter = limiter
        self._cooldown = cooldown

    def throttle_remaining(self, model: str | None = None) -> float:
        return self._limiter.remaining_wait()

    def preflight_stream(
        self, request: MessagesRequest, *, reasoning: Any = None
    ) -> None:
        return None

    async def cleanup(self) -> None:
        return None

    async def list_model_ids(self) -> frozenset[str]:
        return frozenset({PARITY_MODEL})

    async def _call(self) -> str:
        raise_chat_answer(self._script.next_answer(self._index))
        return "ok"

    def stream_response(
        self,
        request: MessagesRequest,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
        reasoning: Any = None,
    ) -> AsyncIterator[str]:
        return self._stream()

    async def _stream(self) -> AsyncIterator[str]:
        try:
            await self._limiter.execute_with_retry(self._call)
        except Exception as error:
            raise classify_provider_failure(
                error,
                provider_name="parity",
                read_timeout_s=None,
                request_id=None,
                mark_rate_limited=self._limiter.extend_reactive_block,
                cooldown=self._cooldown,
                mark_rate_limited_enabled=True,
            ) from error
        yield "event: message_start\n\n"


def chat_request() -> MessagesRequest:
    return MessagesRequest(
        model=PARITY_MODEL,
        max_tokens=16,
        messages=[Message(role="user", content="parity")],
    )


@dataclass(slots=True)
class Leg:
    """How one request on the pool ended."""

    calls: list[tuple[int, int]]
    slept: list[float]
    outcome: str  # ok@<key> | failed | pool_benched
    decisions: list[tuple[int, str | None]]
    route_ends: bool

    def text(self) -> str:
        calls = ",".join(f"{key}.{n}" for key, n in self.calls) or "-"
        slept = ",".join(f"{value:g}" for value in self.slept) or "-"
        decisions = (
            ",".join(f"{key}:{cls or 'none'}" for key, cls in self.decisions) or "-"
        )
        return (
            f"calls={calls} slept={slept} out={self.outcome}"
            f" charged={decisions} ends={int(self.route_ends)}"
        )


def outcome_text(error: BaseException | None, served: int | None) -> str:
    if error is None:
        return f"ok@{served}"
    if isinstance(error, ApplicationUnavailableError):
        return "pool_benched"
    return "failed"


def ends_route(error: BaseException | None) -> bool:
    if error is None:
        return False
    kind = failure_kind(error)
    return kind is not None and kind in DEFAULT_SKIP_KINDS


def health_text(
    state: CredentialRotationState,
    limiters: tuple[ProviderRateLimiter, ...],
    model: str | None,
) -> str:
    """Each key's bench for the model in hand, its lockout tier, its limiter."""
    parts: list[str] = []
    for index, metrics in enumerate(state.get_metrics()):
        benched = max(metrics["cooldown_remaining"], metrics["lockout_remaining"])
        if model is not None:
            for entry in metrics["model_benches"]:
                if entry["model"] == model:
                    benched = max(benched, entry["remaining"])
        parts.append(
            f"{index}:b{round(benched)}:t{metrics['auth_failures']}"
            f":r{metrics['rate_limits']}:w{round(limiters[index].remaining_wait())}"
        )
    return ";".join(parts)


async def _chat_leg(
    provider: RotatingProvider, script: Script, clock: FakeLimiterClock
) -> Leg:
    ladder = install_ladder_trace()
    clock.slept = []
    error: BaseException | None = None
    try:
        # Consumed to the end: the pool reports a success only once the
        # stream has finished.
        async for _chunk in provider.stream_response(chat_request()):
            pass
    except Exception as exc:
        error = exc
    served = script.calls[-1][0] if script.calls else None
    decisions = [
        (decision.key_index, decision.cls)
        for attempt in ladder.ladders.values()
        for decision in attempt.decisions
    ]
    return Leg(
        calls=list(script.calls),
        slept=list(clock.slept),
        outcome=outcome_text(error, served),
        decisions=decisions,
        route_ends=ends_route(error),
    )


async def chat_cell(cell: Cell) -> str:
    """The chat stack's record for one cell."""
    with fake_limiter_time() as clock:
        cooldown = cell.cooldown()
        script = Script(cell)
        limiters = tuple(build_limiter(cell, cooldown) for _ in range(cell.keys))
        leaves = [
            ChatLeaf(index, script, limiters[index], cooldown)
            for index in range(cell.keys)
        ]
        state = build_state(cell, cooldown)
        provider = RotatingProvider(
            ProviderConfig(api_key="chat-key-0", base_url="http://parity.invalid"),
            leaves,
            state,
            key_labels=tuple(f"key{index}" for index in range(cell.keys)),
            provider_id="parity",
        )
        first = await _chat_leg(provider, script, clock)
        after = health_text(state, limiters, PARITY_MODEL)
        throttle = round(provider.throttle_remaining(PARITY_MODEL))
        script.recover()
        second = await _chat_leg(provider, script, clock)
    return (
        f"{first.text()} | health={after} throttle={throttle} | next: {second.text()}"
    )


def chat_records(selected: Callable[[Cell], bool] | None = None) -> dict[str, str]:
    """Every cell's chat record, by cell id."""

    async def run() -> dict[str, str]:
        records: dict[str, str] = {}
        for cell in cells():
            if selected is not None and not selected(cell):
                continue
            records[cell.cell_id] = await chat_cell(cell)
        return records

    return asyncio.run(run())


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} OUT.json", file=sys.stderr)
        return 2
    logger.remove()
    records = chat_records()
    Path(argv[1]).write_text(
        json.dumps(records, indent=0, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"{len(records)} cells written to {argv[1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
