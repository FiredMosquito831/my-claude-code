"""PARITY CONTRACT: search keys under a chain decide exactly as model keys do.

User decisions (specs/INVESTIGATION-WEBSEARCH-ROUTING.md, 2026-10-25 ~21:10):
Q3 -- when a web search chain is set, search keys follow the model provider
rules; Q10 -- by reusing the model engine's own rule objects with only the
walk new, "plus the parity test", the safeguard the user required for media.

How: one scripted upstream world (``search_chat_parity_world.py``), two
stacks over it, the same cells:

* chat   = ``RotatingProvider`` -> one ``ProviderRateLimiter`` per key ->
  ``classify_provider_failure`` (all frozen, unmodified);
* search = ``SearchKeyBooks`` (the same objects, built for search keys) ->
  ``drive_keys`` (the chain's key walk) -> the real Exa adapter over an
  ``httpx.MockTransport``, so the adapter's own status mapping runs.

A cell's record is what the stack decided: tries per key, backoff slept, the
ending, what each failure charged, each key's bench and limiter window, the
default ``FALLBACK_SKIP_KINDS`` verdict, and what the next request does once
the upstream answers again. The chat records were written by the base tree
(v7.111.6, before the chain existed) into ``search_chat_parity_baseline.json``;
both stacks must reproduce every one of them.

Known boundary, stated rather than hidden: a model 429 benches the (key,
model) pair while a search 429 benches the whole key -- a search service has
no models -- so a key's bench is compared *for the model in hand*, which is
the only one a search ever asks about.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from my_claude_code.api.web_tools.search_books import (
    SearchBooksPolicy,
    SearchKeyBooks,
)
from my_claude_code.api.web_tools.search_chain import drive_keys
from my_claude_code.config.constants import RATE_LIMIT_COOLDOWN_SECONDS_DEFAULT
from my_claude_code.config.settings import Settings
from my_claude_code.core.credential_attribution import NO_CREDENTIAL_INDEX
from my_claude_code.core.rate_limit import MAX_RATE_LIMIT_COOLDOWN_SECONDS
from my_claude_code.websearch.adapters.exa import ExaWebSearchProvider
from my_claude_code.websearch.base import WebSearchProviderConfig
from my_claude_code.websearch.registry import build_provider
from tests.contracts.search_chat_parity_world import (
    ANSWERS,
    BASELINE_NAME,
    LOCKOUT_TIERS,
    MODEL_BENCH_ESCALATION,
    Answer,
    Cell,
    FakeLimiterClock,
    Leg,
    Script,
    cells,
    chat_cell,
    ends_route,
    fake_limiter_time,
    health_text,
    outcome_text,
)

BASELINE_PATH = Path(__file__).with_name(BASELINE_NAME)
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _baseline() -> dict[str, str]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


# -------------------------------------------------------------- the search side


def _search_response(answer: Answer, request: httpx.Request) -> httpx.Response:
    if answer.kind == "ok":
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "parity",
                        "url": "https://example.invalid/parity",
                        "highlights": ["parity"],
                    }
                ]
            },
            request=request,
        )
    if answer.kind == "timeout":
        raise httpx.ReadTimeout("timed out", request=request)
    if answer.kind == "connect":
        raise httpx.ConnectError("connection refused", request=request)
    if answer.kind == "bad_json":
        return httpx.Response(200, content=b"<<not json>>", request=request)
    return httpx.Response(
        answer.status,
        headers=dict(answer.headers),
        content=json.dumps(answer.body).encode(),
        request=request,
    )


def _search_provider(cell: Cell, script: Script) -> ExaWebSearchProvider:
    keys = tuple(f"exa-parity-key-{index}" for index in range(cell.keys))

    def handler(request: httpx.Request) -> httpx.Response:
        index = keys.index(request.headers["x-api-key"])
        return _search_response(script.next_answer(index), request)

    provider = ExaWebSearchProvider(
        WebSearchProviderConfig(
            api_keys=keys,
            credential_rotation=cell.policy,
            base_url="http://exa.parity.invalid",
            proxy=None,
            http_timeout=20.0,
        )
    )
    provider.__dict__["_client"] = httpx.AsyncClient(
        transport=httpx.MockTransport(handler)
    )
    return provider


def _search_books(cell: Cell, provider: ExaWebSearchProvider) -> SearchKeyBooks:
    base, ceiling, jitter = cell.backoff_seconds
    policy = SearchBooksPolicy(
        rotation=cell.policy,
        rate_limit_cooldown_seconds=RATE_LIMIT_COOLDOWN_SECONDS_DEFAULT,
        rate_limit_cooldown_mode=cell.mode,
        rate_limit_cooldown_max_seconds=MAX_RATE_LIMIT_COOLDOWN_SECONDS,
        lockout_tiers=LOCKOUT_TIERS,
        model_bench_escalation=MODEL_BENCH_ESCALATION,
        retry_attempts=cell.attempts,
        backoff_base_seconds=base,
        backoff_max_seconds=ceiling,
        backoff_jitter_seconds=jitter,
    )
    labels = tuple(provider.key_label(index) for index in range(cell.keys))
    return SearchKeyBooks("exa", provider.config.api_keys, labels, policy)


async def _search_leg(
    books: SearchKeyBooks,
    provider: ExaWebSearchProvider,
    script: Script,
    clock: FakeLimiterClock,
) -> Leg:
    clock.slept = []

    async def one_try(index: int, key: str) -> object:
        return await provider.search_once("parity", key, index, max_results=5)

    result = await drive_keys(books, one_try)
    decisions = [
        (int(str(row["key_index"])), _cls(row.get("charged")))
        for row in result.tries
        if "charged" in row
    ]
    served = None if result.key_index == NO_CREDENTIAL_INDEX else result.key_index
    return Leg(
        calls=list(script.calls),
        slept=list(clock.slept),
        outcome=outcome_text(result.failure, served),
        decisions=decisions,
        route_ends=ends_route(result.failure),
    )


def _cls(value: Any) -> str | None:
    return None if value is None else str(value)


async def search_cell(cell: Cell) -> str:
    """The search books' record for one cell, in the chat record's shape."""
    with fake_limiter_time() as clock:
        script = Script(cell)
        provider = _search_provider(cell, script)
        try:
            books = _search_books(cell, provider)
            first = await _search_leg(books, provider, script, clock)
            after = health_text(books.state, books.limiters, None)
            throttle = round(books.throttle_remaining())
            script.recover()
            second = await _search_leg(books, provider, script, clock)
        finally:
            await provider.close()
    return (
        f"{first.text()} | health={after} throttle={throttle} | next: {second.text()}"
    )


def _records(run: Any, answer: Answer) -> dict[str, str]:
    async def collect() -> dict[str, str]:
        out: dict[str, str] = {}
        for cell in cells():
            if cell.answer == answer:
                out[cell.cell_id] = await run(cell)
        return out

    return asyncio.run(collect())


def _diff(expected: dict[str, str], actual: dict[str, str]) -> list[str]:
    lines = [
        f"{cell_id}\n  baseline: {expected[cell_id]}\n  got:      {actual.get(cell_id)}"
        for cell_id in sorted(expected)
        if actual.get(cell_id) != expected[cell_id]
    ]
    lines.extend(
        f"{cell_id}: not in the baseline"
        for cell_id in sorted(set(actual) - set(expected))
    )
    return lines


# ------------------------------------------------------------------- the tests


def test_the_baseline_covers_every_cell_once() -> None:
    baseline = _baseline()
    ids = [cell.cell_id for cell in cells()]
    assert len(ids) == len(set(ids)) == len(baseline) == 4320
    assert set(ids) == set(baseline)


@pytest.mark.parametrize("answer", ANSWERS, ids=lambda answer: answer.name)
def test_the_chat_stack_still_decides_what_the_base_tree_recorded(
    answer: Answer,
) -> None:
    """The frozen objects are the reference: today's chat stack == the base tree."""
    baseline = _baseline()
    expected = {
        key: value
        for key, value in baseline.items()
        if key.startswith(f"{answer.name}|")
    }
    mismatches = _diff(expected, _records(chat_cell, answer))
    assert not mismatches, "\n".join(mismatches[:20])


@pytest.mark.parametrize("answer", ANSWERS, ids=lambda answer: answer.name)
def test_search_keys_decide_exactly_as_model_keys(answer: Answer) -> None:
    """Every cell: tries, sleeps, ending, charge, bench, limiter, route end, next."""
    baseline = _baseline()
    expected = {
        key: value
        for key, value in baseline.items()
        if key.startswith(f"{answer.name}|")
    }
    mismatches = _diff(expected, _records(search_cell, answer))
    assert not mismatches, "\n".join(mismatches[:20])


def test_a_one_key_search_provider_is_never_benched_into_an_outage() -> None:
    """IV.4, measured as F1 in the spec: every failure, then the next search."""
    baseline = _baseline()
    for cell_id, record in baseline.items():
        if "|k1|" not in cell_id:
            continue
        nxt = record.split(" | next: ", 1)[1]
        assert "out=ok@0" in nxt, (cell_id, record)


# ---------------------------------------------------------- Q12: on_error, docs


def test_on_error_is_accepted_as_failover_for_search_keys(monkeypatch) -> None:
    monkeypatch.setenv("EXA_API_KEY", "exa-alias-key-one,exa-alias-key-two")
    monkeypatch.setenv("EXA_API_KEY_ROTATION", "on_error")
    settings = Settings.model_validate(
        {"EXA_API_KEY": "exa-alias-key-one,exa-alias-key-two"}
    )
    provider = build_provider(settings, "exa")
    assert provider.config.credential_rotation == "failover"


def test_the_docs_say_which_rules_apply_with_and_without_a_chain() -> None:
    text = (_REPO_ROOT / "docs" / "WEB-SEARCH.md").read_text(encoding="utf-8")
    # The 7.111 sentence was false for semantics (spec §8 F6).
    assert "share the same engine and health semantics" not in text
    assert "No `WEB_SEARCH_CHAIN` (the default)" in text
    assert "`WEB_SEARCH_CHAIN` set (7.112.0)" in text
    assert "`on_error` is accepted" in text
