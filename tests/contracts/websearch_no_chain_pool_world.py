"""The no-chain search pool decision table (7.112.1, WEBSEARCH-Q8).

``test_websearch_no_chain_pool.py`` pins what the search route used with NO
``WEB_SEARCH_CHAIN`` decides after one failed search: what the failure costs
the key, whether the next search makes a call, and what the client is told.

This module builds the table and imports nothing 7.112.1 added. That is what
lets the baseline be generated on the base tree (7.112.0, before the fix): run
as a script with the base worktree's interpreter it writes
``websearch_no_chain_pool_baseline.json``::

    <base>/.venv/Scripts/python.exe -I tests/contracts/websearch_no_chain_pool_world.py OUT.json

A cell is one upstream answer on one key pool. Every search goes through the
product's own entry point, ``outbound._run_web_search``, with the route
``WEB_SEARCH_PROVIDER=<provider>`` + ``WEB_SEARCH_FALLBACK_POLICY=none``, so a
failure reaches the client. The provider is the real adapter (Exa, or SearXNG
for a keyless pool) built exactly as the registry builds it, with its HTTP
client swapped for an ``httpx.MockTransport`` -- the adapter's own status
mapping runs -- and its pool's clock swapped for a fake one, so a bench is
read back rather than waited out.

Three searches per cell: at t=0 the failing keys answer the cell's failure;
at t=0.5 s and t=8 s every key answers 200 (a 429's ``Retry-After`` is 7 s).
A record says, per search, which keys were called, how it ended, the client's
``error_code`` and sentence, and each key's health afterwards.
"""

import asyncio
import contextlib
import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from my_claude_code.api.web_tools import outbound
from my_claude_code.api.web_tools.streaming import _web_search_error_code
from my_claude_code.config.settings import Settings
from my_claude_code.websearch import registry
from my_claude_code.websearch.adapters.exa import ExaWebSearchProvider
from my_claude_code.websearch.adapters.searxng import SearxngWebSearchProvider
from my_claude_code.websearch.base import (
    BaseWebSearchProvider,
    WebSearchProviderConfig,
)
from my_claude_code.websearch.errors import WebSearchError
from my_claude_code.websearch.registry import (
    SearchRecorder,
    SearchRouteOutcome,
    SearchRouteRecorder,
)

BASELINE_NAME = "websearch_no_chain_pool_baseline.json"

#: When each search of a cell runs, on the pool's fake clock (seconds).
SEARCH_TIMES: tuple[tuple[str, float], ...] = (("s1", 0.0), ("s2", 0.5), ("s3", 8.0))


def _error_body(message: str) -> dict[str, Any]:
    return {"error": {"message": message}}


@dataclass(frozen=True, slots=True)
class Answer:
    """One upstream answer: an HTTP status, or a transport-level outcome."""

    name: str
    status: int = 200
    body: Any = None
    headers: tuple[tuple[str, str], ...] = ()
    #: ``http`` | ``ok`` | ``timeout`` | ``connect`` | ``bad_json`` | ``raw``
    kind: str = "http"


ANSWERS: tuple[Answer, ...] = (
    Answer("ok", kind="ok"),
    Answer("s400", status=400, body=_error_body("unsupported parameter: depth")),
    Answer("s400_credits", status=400, body=_error_body("insufficient credits")),
    Answer("s401", status=401, body=_error_body("invalid api key")),
    Answer("s402_bare", status=402, body=_error_body("plan limit reached")),
    Answer("s402_credits", status=402, body=_error_body("insufficient credits")),
    Answer("s403", status=403, body=_error_body("forbidden")),
    Answer(
        "s403_region",
        status=403,
        body=_error_body("This service is not available in your country"),
    ),
    Answer("s429", status=429, body=_error_body("too many requests")),
    Answer(
        "s429_ra7",
        status=429,
        body=_error_body("too many requests"),
        headers=(("Retry-After", "7"),),
    ),
    Answer("s500", status=500, body=_error_body("internal error")),
    Answer("s503", status=503, body=_error_body("unavailable")),
    Answer("timeout", kind="timeout"),
    Answer("connect", kind="connect"),
    Answer("bad_json", kind="bad_json"),
    # An exception no adapter classified (a bug, an unexpected library error).
    Answer("raw_error", kind="raw"),
)


@dataclass(frozen=True, slots=True)
class Setup:
    """One key pool: how many keys (0 = keyless), its policy, which fail."""

    name: str
    keys: int
    policy: str
    failing: frozenset[int] = field(default_factory=lambda: frozenset({0}))

    @property
    def one_slot(self) -> bool:
        """The pool can only ever serve from one slot."""
        return self.keys <= 1 or self.policy == "single"


SETUPS: tuple[Setup, ...] = (
    Setup("keyless", 0, "single"),
    Setup("one_key_single", 1, "single"),
    Setup("one_key_failover", 1, "failover"),
    Setup("two_failover_first_fails", 2, "failover"),
    Setup("two_round_robin_first_fails", 2, "round_robin"),
    Setup("two_least_used_both_fail", 2, "least_used", frozenset({0, 1})),
    Setup("two_failover_both_fail", 2, "failover", frozenset({0, 1})),
    Setup("three_single_first_fails", 3, "single"),
)

_OK_BODY = {
    "results": [
        {
            "title": "table",
            "url": "https://example.invalid/table",
            "content": "table",
            "highlights": ["table"],
        }
    ]
}


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _response(answer: Answer, request: httpx.Request) -> httpx.Response:
    if answer.kind == "ok":
        return httpx.Response(200, json=_OK_BODY, request=request)
    if answer.kind == "timeout":
        raise httpx.ReadTimeout("timed out", request=request)
    if answer.kind == "connect":
        raise httpx.ConnectError("connection refused", request=request)
    if answer.kind == "bad_json":
        return httpx.Response(200, content=b"<<not json>>", request=request)
    if answer.kind == "raw":
        raise RuntimeError("unclassified failure")
    return httpx.Response(
        answer.status,
        headers=dict(answer.headers),
        content=json.dumps(answer.body).encode(),
        request=request,
    )


@dataclass(slots=True)
class _World:
    """One cell's provider, upstream script, call log and clock."""

    provider: BaseWebSearchProvider
    clock: FakeClock
    keys: tuple[str, ...]
    calls: list[int] = field(default_factory=list)
    search_number: int = 0


async def _world(answer: Answer, setup: Setup) -> _World:
    keys = tuple(f"exa-table-key-{index}-abcd" for index in range(setup.keys))
    config = WebSearchProviderConfig(
        api_keys=keys,
        credential_rotation=setup.policy,
        base_url=(
            "http://searxng.table.invalid" if not keys else "http://exa.table.invalid"
        ),
        proxy=None,
        http_timeout=20.0,
        options={},
    )
    provider: BaseWebSearchProvider = (
        ExaWebSearchProvider(config) if keys else SearxngWebSearchProvider(config)
    )
    clock = FakeClock()
    world = _World(provider=provider, clock=clock, keys=keys)

    def handler(request: httpx.Request) -> httpx.Response:
        sent = request.headers.get("x-api-key", "")
        index = keys.index(sent) if sent in keys else 0
        world.calls.append(index)
        if world.search_number == 1 and index in setup.failing:
            return _response(answer, request)
        return _response(Answer("ok", kind="ok"), request)

    original = provider._client
    if original is not None:
        await original.aclose()
    provider._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    pool = provider.key_pool
    pool._clock = clock
    pool._engine._clock = clock
    return world


@contextlib.contextmanager
def _route_to(provider: BaseWebSearchProvider) -> Iterator[None]:
    """Serve the route from ``provider``; record no analytics rows."""

    async def runtime_provider(
        settings: Settings, provider_id: str
    ) -> BaseWebSearchProvider:
        return provider

    def emit_route_outcome(
        outcome: SearchRouteOutcome, recorder: SearchRouteRecorder | None = None
    ) -> None:
        return None

    def default_recorder() -> SearchRecorder | None:
        return None

    replacements: tuple[tuple[dict[str, Any], str, object], ...] = (
        (vars(outbound), "runtime_provider", runtime_provider),
        (vars(outbound), "emit_route_outcome", emit_route_outcome),
        (vars(registry), "_default_recorder", default_recorder),
    )
    saved = [(namespace, name, namespace[name]) for namespace, name, _ in replacements]
    for namespace, name, value in replacements:
        namespace[name] = value
    try:
        yield
    finally:
        for namespace, name, value in saved:
            namespace[name] = value


def _key_health(provider: BaseWebSearchProvider) -> list[list[Any]]:
    snapshot = provider.key_pool.snapshot()
    return [
        [key["state"], round(float(key["state_remaining_seconds"])), key["lockouts"]]
        for key in snapshot["keys"]
    ]


def _ending(error: BaseException | None) -> dict[str, Any]:
    if error is None:
        return {"result": "results", "code": None, "text": None, "message": None}
    kind = error.kind if isinstance(error, WebSearchError) else "internal"
    return {
        "result": f"{type(error).__name__}:{kind}",
        "code": _web_search_error_code(error),
        "text": outbound._web_tool_client_error_summary(
            "web_search", error, verbose=False
        ),
        "message": str(error),
    }


async def run_cell(answer: Answer, setup: Setup) -> dict[str, Any]:
    world = await _world(answer, setup)
    provider_id = "exa" if world.keys else "searxng"
    settings = Settings.model_validate(
        {"WEB_SEARCH_PROVIDER": provider_id, "WEB_SEARCH_FALLBACK_POLICY": "none"}
    )
    record: dict[str, Any] = {}
    with _route_to(world.provider):
        for label, at in SEARCH_TIMES:
            world.clock.now = 1000.0 + at
            world.search_number += 1
            before = len(world.calls)
            error: BaseException | None = None
            try:
                await outbound._run_web_search("table query", settings)
            except Exception as caught:
                error = caught
            record[f"{label}_calls"] = world.calls[before:]
            for name, value in _ending(error).items():
                record[f"{label}_{name}"] = value
            record[f"{label}_keys_after"] = _key_health(world.provider)
    await world.provider.close()
    return record


def cell_id(answer: Answer, setup: Setup) -> str:
    return f"{answer.name}|{setup.name}"


async def build_table() -> dict[str, dict[str, Any]]:
    table: dict[str, dict[str, Any]] = {}
    for answer in ANSWERS:
        for setup in SETUPS:
            table[cell_id(answer, setup)] = await run_cell(answer, setup)
    return table


def main(out_path: str) -> None:
    table = asyncio.run(build_table())
    with open(out_path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(table, handle, indent=1, sort_keys=True)
        handle.write("\n")
    print(f"{len(table)} cells -> {out_path}")


if __name__ == "__main__":
    main(sys.argv[1])
