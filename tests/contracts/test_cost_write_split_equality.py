"""7.111.0's equality contract: only rows that wrote 1-hour cache entries price differently.

Pricing 1-hour prompt-cache writes at their own rate (CACHE-TTL PR 4) is an
addendum to the pricing ladder, and the user's binding line is that every
existing price stays exactly what it was unless the row carries 1-hour writes.
``cost_write_split_equality_baseline.json`` was recorded on the 7.110.0 tree by
``tests/support/cost_equality_corpus.py`` (two base runs, byte-identical): 419
routes x 144 usages x 4 mode/reported pairs x LiteLLM on/off through
``rate_cards`` + ``resolve_cost`` (the request capture's own call), the
historical backfill pricer over the same routes, and 840 request rows finished
through ``RequestCapture`` from Anthropic-shaped SSE frames that carry
Anthropic's ``usage.cache_creation`` split in seven shapes. It is a recording,
not a file to regenerate.

Here the same corpus is priced by 7.111.0 with every new source switched on --
a provider-row lookup and an OpenRouter live list that both state a 1-hour
rate, LiteLLM entries that state one -- and only the rows that reported
1-hour writes may move.
"""

import functools
import json
from contextlib import ExitStack
from typing import Any

import pytest

from my_claude_code.api import request_pricing
from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.application.cost import (
    SOURCE_LITELLM,
    SOURCE_MODELS_DEV,
    SOURCE_UNPRICED_1H_WRITE,
    TokenUsage,
)
from my_claude_code.core.request_log import get_request_log_store
from tests.support import cost_equality_corpus as corpus

BASELINE: dict[str, Any] = json.loads(corpus.BASELINE.read_text(encoding="utf-8"))

#: A provider list that states a 1-hour rate for every model it is asked about.
_EVERY_ROW_STATES_ONE_HOUR = {"pricing": {"input_cache_write_1h": "0.000009"}}


@pytest.fixture
def catalogues(tmp_path):
    config_dir = tmp_path / "config"
    corpus.write_catalogues(config_dir)
    with ExitStack() as stack:
        corpus.patched_config_dir(stack, config_dir)
        yield config_dir


def _memoised_rate_cards(monkeypatch: pytest.MonkeyPatch) -> None:
    """The base recording memoised ``rate_cards`` per route; so does this side.

    Same answers (the catalogues do not change inside a test), a fraction of
    the file stats: ``price_request`` itself is what is under test.
    """

    real = request_pricing.rate_cards
    memo = functools.cache(
        lambda provider, model, litellm: real(provider, model, litellm_enabled=litellm)
    )

    def cached(provider, model, *, litellm_enabled):
        return memo(provider, model, litellm_enabled)

    monkeypatch.setattr(request_pricing, "rate_cards", cached)


def test_the_baseline_was_recorded_on_7_110_0() -> None:
    assert BASELINE["recorded_on"] == "7.110.0"
    assert BASELINE["routes"] == len(corpus.routes()) == 419
    assert BASELINE["usages"] == 144
    assert len(BASELINE["pricing"]) == len(BASELINE["backfill"]) == 838
    assert len(BASELINE["capture"]) == 840


@pytest.mark.parametrize("split", ["absent", "zero"])
def test_a_request_without_one_hour_writes_prices_exactly_as_on_7_110_0(
    catalogues, monkeypatch, split
) -> None:
    _memoised_rate_cards(monkeypatch)
    request_pricing.set_published_row_lookup(
        lambda provider, model: _EVERY_ROW_STATES_ONE_HOUR
    )

    def price(provider, model, usage, reported, mode, litellm):
        answer = request_pricing.price_request(
            provider,
            model,
            TokenUsage(
                tokens_in=usage[0],
                tokens_out=usage[1],
                cache_read_tokens=usage[2],
                cache_write_tokens=usage[3],
                reasoning_tokens=usage[4],
                cache_write_1h_tokens=None if split == "absent" else 0,
            ),
            reported_usd=reported,
            mode=mode,
            litellm_enabled=litellm,
            openrouter_live_enabled=True,
        )
        assert answer.cache_write_1h is None
        return answer.cost_usd, answer.cost_source

    found = corpus.price_digests(price)
    moved = sorted(
        key for key, digest in found.items() if BASELINE["pricing"][key] != digest
    )
    assert moved == []
    assert found.keys() == BASELINE["pricing"].keys()


def test_the_historical_backfill_prices_exactly_as_on_7_110_0(catalogues) -> None:
    found = corpus.backfill_digests(
        lambda litellm: request_pricing.backfill_pricer(litellm_enabled=litellm)
    )
    assert found == BASELINE["backfill"]


def test_only_capture_rows_that_wrote_one_hour_entries_move(
    tmp_path, catalogues
) -> None:
    store = get_request_log_store(tmp_path / "corpus.db")
    assert store is not None

    def make_capture(request_id, litellm):
        return RequestCapture(
            store,
            request_id=request_id,
            endpoint="/v1/messages",
            protocol="anthropic",
            stream=True,
            requested_model="corpus",
            input_text=None,
            params=None,
            cost_litellm_enabled=litellm,
            cost_openrouter_live_enabled=True,
        )

    found = corpus.capture_records(store, make_capture)
    assert found.keys() == BASELINE["capture"].keys()
    unexpected: list[str] = []
    moved: dict[str, dict[str, Any]] = {}
    for key, row in found.items():
        recorded = BASELINE["capture"][key]
        may_move = _reports_one_hour_writes(key)
        if row == recorded:
            continue
        if not may_move:
            unexpected.append(key)
            continue
        # Every counter the row measured is what 7.110.0 stored; only the
        # price, its source and the 1-hour rate's provenance may differ.
        for name in (
            "tokens_in",
            "tokens_out",
            "cache_read_tokens",
            "cache_write_tokens",
        ):
            assert row[name] == recorded[name], (key, name)
        moved[key] = row
    assert unexpected == []
    sources = sorted({row["cost_source"] for row in moved.values()})
    assert sources == [SOURCE_LITELLM, SOURCE_MODELS_DEV, SOURCE_UNPRICED_1H_WRITE]
    for key, row in moved.items():
        if row["cost_source"] == SOURCE_UNPRICED_1H_WRITE:
            assert row["cost_usd"] is None, key
            assert row["params"] is None, key
        else:
            note = row["params"]["cost_write_1h"]
            # ``provider_list``: OpenRouter's own row for its own route.
            assert note["source"] in {"litellm", "openrouter_live", "provider_list"}, (
                key
            )
            assert note["usd_per_million"] > 0, key
    # Every moving row of a priced route moved (none silently kept 5 min).
    moving = [
        key
        for key in found
        if _reports_one_hour_writes(key)
        and BASELINE["capture"][key]["cost_source"] is not None
    ]
    assert sorted(moving) == sorted(moved)


def _reports_one_hour_writes(key: str) -> bool:
    """Whether this corpus row's frames say it wrote 1-hour cache entries."""

    _family, split, usage, *_rest = key.split("|")
    written = corpus.CAPTURE_USAGES[int(usage[1:])][2]
    shape = corpus.SPLITS[split](written)
    if split not in corpus.MOVING_SPLITS or not isinstance(shape, dict):
        return False
    one_hour = shape.get("ephemeral_1h_input_tokens")
    return isinstance(one_hour, int) and one_hour > 0 and written > 0
