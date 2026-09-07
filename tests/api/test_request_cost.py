"""End to end: a finished request carries a cost and says where it came from.

Everything below drives the real ladder against a real (temporary) models.dev
cache. The point of the feature is that a price and a token count finally meet,
and a test that stubs the ladder would not prove they did.
"""

import json
import os
from datetime import UTC, datetime

import pytest

from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.core.reported_cost import install_reported_cost
from my_claude_code.core.request_log import (
    RequestRecord,
    RouteAttempt,
    RouteAttemptOutcome,
    get_request_log_store,
)
from my_claude_code.providers.runtime import models_dev

#: One provider with a bucket of its own, one free model that says so, and a
#: describe model on a second provider. Shaped exactly like the live index.
_INDEX = {
    "acme": {
        "models": {
            "acme/flash": {
                "limit": {"context": 131072, "output": 8192},
                # USD per MILLION, which is what models.dev publishes and the
                # opposite of what LiteLLM publishes.
                "cost": {
                    "input": 3.0,
                    "output": 15.0,
                    "cache_read": 0.3,
                    "cache_write": 3.75,
                },
            },
            "acme/gratis:free": {"cost": {"input": 0, "output": 0}},
        }
    },
    "eyes": {"models": {"eyes/vision": {"cost": {"input": 1.0, "output": 2.0}}}},
}


@pytest.fixture(autouse=True)
def models_dev_cache(tmp_path, monkeypatch):
    """Seed a models.dev cache the ladder will actually read."""
    config_dir = tmp_path / "config"
    (config_dir / "cache").mkdir(parents=True)
    path = config_dir / "cache" / "models-dev.json"
    path.write_text(
        json.dumps({"fetched_at": datetime.now(UTC).isoformat(), "index": _INDEX}),
        encoding="utf-8",
    )
    # The parsed index is memoized per (path, mtime), and a fresh tmp_path per
    # test is what keeps two tests from sharing one.
    now = datetime.now(UTC).timestamp()
    os.utime(path, (now, now))
    monkeypatch.setattr(models_dev, "config_dir_path", lambda: config_dir)
    return path


@pytest.fixture
def store(tmp_path):
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    yield store
    store.close()


def _capture(store, **kwargs):
    return RequestCapture(
        store,
        request_id="req-1",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="acme/flash",
        input_text=None,
        params=None,
        **kwargs,
    )


def _finalize(capture, *, provider, model, **usage):
    record = capture._record
    record.provider = provider
    record.resolved_model = model
    capture._tokens_in = usage.get("tokens_in")
    capture._tokens_out = usage.get("tokens_out")
    capture._cache_read_tokens = usage.get("cache_read_tokens")
    capture._cache_write_tokens = usage.get("cache_write_tokens")
    capture._finalize("success")
    return record


def _stored(store, request_id="req-1"):
    store.close()
    return store.get_request(request_id)


def test_a_request_prices_from_the_providers_own_bucket(store):
    capture = _capture(store)
    _finalize(
        capture,
        provider="acme",
        model="acme/flash",
        tokens_in=1_000_000,
        tokens_out=1_000_000,
    )
    row = _stored(store)
    assert row["cost_source"] == "models_dev"
    assert row["cost_usd"] == pytest.approx(18.0)


def test_cache_reads_and_writes_price_at_their_own_rates(store):
    capture = _capture(store)
    _finalize(
        capture,
        provider="acme",
        model="acme/flash",
        tokens_in=0,
        tokens_out=0,
        cache_read_tokens=1_000_000,
        cache_write_tokens=1_000_000,
    )
    row = _stored(store)
    assert row["cost_usd"] == pytest.approx(0.3 + 3.75)


def test_a_reported_cost_wins_and_carries_the_provider_source(store):
    capture = _capture(store)
    slot = install_reported_cost()
    capture._reported_cost = slot
    slot.cost_usd = 0.42
    slot.is_byok = False
    _finalize(
        capture,
        provider="acme",
        model="acme/flash",
        tokens_in=1_000_000,
        tokens_out=1_000_000,
    )
    row = _stored(store)
    assert row["cost_source"] == "provider"
    assert row["cost_usd"] == 0.42


def test_a_free_model_prices_at_zero_because_its_source_says_zero(store):
    capture = _capture(store)
    _finalize(
        capture,
        provider="acme",
        model="acme/gratis:free",
        tokens_in=5_000,
        tokens_out=1_000,
    )
    row = _stored(store)
    assert row["cost_usd"] == 0.0
    assert row["cost_source"] == "models_dev"


def test_an_unknown_model_stores_null_not_zero(store):
    capture = _capture(store)
    _finalize(
        capture,
        provider="acme",
        model="acme/never-heard-of-it",
        tokens_in=5_000,
        tokens_out=1_000,
    )
    row = _stored(store)
    assert row["cost_usd"] is None
    assert row["cost_source"] is None


def test_a_locally_answered_request_is_unpriced(store):
    capture = _capture(store)
    record = capture._record
    record.optimization = "title_generation"
    _finalize(capture, provider=None, model=None, tokens_in=0, tokens_out=0)
    row = _stored(store)
    assert row["cost_usd"] is None


def test_costing_off_stores_nothing(store):
    capture = _capture(store, cost_enabled=False)
    _finalize(
        capture,
        provider="acme",
        model="acme/flash",
        tokens_in=1_000_000,
        tokens_out=0,
    )
    row = _stored(store)
    assert row["cost_usd"] is None


def test_reported_only_leaves_a_computable_request_unpriced(store):
    capture = _capture(store, cost_mode="reported_only")
    _finalize(
        capture,
        provider="acme",
        model="acme/flash",
        tokens_in=1_000_000,
        tokens_out=0,
    )
    row = _stored(store)
    assert row["cost_usd"] is None


def test_a_describe_attempt_is_priced_on_its_own_row(store):
    """A different model on a different key, so a different number.

    Folding it into the parent would make the answering model look more
    expensive than it was and would hide what describe mode actually cost.
    """
    capture = _capture(store)
    capture._attempts.append(
        RouteAttempt(
            attempt=1000,
            provider="eyes",
            model_ref="eyes/vision",
            outcome=RouteAttemptOutcome.SUCCEEDED,
            tokens_in=1_000_000,
            tokens_out=1_000_000,
            params={"kind": "describe", "image_index": 0},
        )
    )
    _finalize(
        capture,
        provider="acme",
        model="acme/flash",
        tokens_in=1_000_000,
        tokens_out=0,
    )
    row = _stored(store)
    assert row["cost_usd"] == pytest.approx(3.0), "the answering model only"
    attempt = row["route_attempts"][0]
    assert attempt["cost_usd"] == pytest.approx(3.0)
    assert attempt["cost_source"] == "models_dev"


def test_an_ordinary_attempt_carries_no_cost_of_its_own(store):
    """Or every total that joined the two tables would double."""
    capture = _capture(store)
    capture._attempts.append(
        RouteAttempt(
            attempt=0,
            provider="acme",
            model_ref="acme/flash",
            outcome=RouteAttemptOutcome.SUCCEEDED,
        )
    )
    _finalize(
        capture,
        provider="acme",
        model="acme/flash",
        tokens_in=1_000_000,
        tokens_out=0,
    )
    row = _stored(store)
    assert row["route_attempts"][0]["cost_usd"] is None


def test_the_cost_breakdown_splits_reported_from_estimated(store):
    for index, (cost, source) in enumerate(
        ((0.5, "provider"), (0.25, "models_dev"), (None, None))
    ):
        store.enqueue(
            RequestRecord(
                id=f"r{index}",
                endpoint="/v1/messages",
                protocol="anthropic",
                provider="acme",
                resolved_model="acme/flash",
                harness="claude_code",
                cost_usd=cost,
                cost_source=source,
            )
        )
    store.close()
    breakdown = store.cost_breakdown()
    assert breakdown["totals"]["reported_usd"] == 0.5
    assert breakdown["totals"]["estimated_usd"] == 0.25
    assert breakdown["totals"]["priced"] == 2
    assert breakdown["totals"]["requests"] == 3
    assert [row["key"] for row in breakdown["by_provider"]] == ["acme"]
    assert breakdown["by_harness"][0]["key"] == "claude_code"
    assert {row["key"] for row in breakdown["by_source"]} == {
        "provider",
        "models_dev",
    }


def test_an_entirely_unpriced_window_totals_to_null(store):
    store.enqueue(
        RequestRecord(
            id="r0",
            endpoint="/v1/messages",
            protocol="anthropic",
            provider="acme",
            resolved_model="acme/flash",
        )
    )
    store.close()
    totals = store.cost_breakdown()["totals"]
    assert totals["reported_usd"] is None, "not 0.0 -- nobody priced it"
    assert totals["estimated_usd"] is None
    assert totals["priced"] == 0
    assert totals["requests"] == 1
