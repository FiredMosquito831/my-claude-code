"""The cost endpoint, served from a payload that survives a restart.

A restart used to cost twelve seconds before the Analytics cost card could say
anything, every time, whether or not one request had been logged since. These
hold the three promises that replaced that: a stored answer is served when the
log has not changed, a changed log still answers immediately (marked), and what
is served is what a fresh computation would have returned.
"""

import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from my_claude_code.application import derived_payloads
from my_claude_code.config.settings import Settings
from my_claude_code.core.request_log import RequestRecord, get_request_log_store
from tests.api.support import create_test_app

ENDPOINT = "/admin/api/requests/cost"


def _record(index: int, **overrides: Any) -> RequestRecord:
    defaults: dict[str, Any] = {
        "id": f"r{index:05d}",
        "endpoint": "/v1/messages",
        "protocol": "anthropic",
        "ts_epoch": 1_700_000_000.0 + index,
        "provider": "p1",
        "resolved_model": "m1",
        "tokens_in": 100,
        "tokens_out": 10,
        "cost_usd": 0.25,
        "cost_source": "models_dev",
    }
    defaults.update(overrides)
    return RequestRecord(**defaults)


@pytest.fixture
def config_home():
    """Where the cache lands: whatever the suite's redirected home resolves to.

    Deliberately *not* an ``MCC_CONFIG_DIR`` of its own. The hermetic harness
    unsets that variable on purpose -- it outranks ``HOME`` in
    ``resolve_config_dir``, so a test that sets it resolves a directory the
    next test inherits. Redirecting the home is the harness's job and it has
    already done it; this fixture only names the answer.
    """

    from my_claude_code.config.paths import config_dir_path

    return config_dir_path()


@pytest.fixture
def seeded(tmp_path):
    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    for index in range(4):
        store.enqueue(_record(index))
    store.close()
    return store


@pytest.fixture
def client():
    return TestClient(create_test_app(), client=("127.0.0.1", 50000))


def _settle(name: str = "cost-breakdown-local-hide") -> None:
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        with derived_payloads._refresh_lock:
            if name not in derived_payloads._refreshing:
                return
        time.sleep(0.01)


def test_the_first_call_computes_and_the_second_is_served_from_disk(
    client, seeded, config_home
) -> None:
    first = client.get(f"{ENDPOINT}?local=hide").json()

    assert first["stale"] is False
    assert first["totals"]["priced"] == 4
    stored = derived_payloads.derived_cache().read("cost-breakdown-local-hide")
    assert stored is not None

    second = client.get(f"{ENDPOINT}?local=hide").json()

    assert second["stale"] is False
    assert second["computed_at"] == first["computed_at"]
    assert second["totals"] == first["totals"]


def test_a_cached_answer_equals_a_fresh_one(client, seeded, config_home) -> None:
    """The equality oracle: the stored payload is the raw query's own answer."""

    served = client.get(f"{ENDPOINT}?local=hide").json()
    fresh = seeded.cost_breakdown(local="hide")

    for field, value in fresh.items():
        assert served[field] == value, field


def test_a_new_request_makes_the_stored_answer_stale_without_a_wait(
    client, seeded, config_home, tmp_path
) -> None:
    client.get(f"{ENDPOINT}?local=hide")

    store = get_request_log_store(tmp_path / "requests.db")
    assert store is not None
    store.enqueue(_record(99))
    store.close()

    stale = client.get(f"{ENDPOINT}?local=hide").json()
    # Answered from what was stored, and honest about it.
    assert stale["stale"] is True
    assert stale["totals"]["priced"] == 4
    _settle()

    refreshed = client.get(f"{ENDPOINT}?local=hide").json()
    assert refreshed["stale"] is False
    assert refreshed["totals"]["priced"] == 5


def test_a_filtered_question_is_answered_live_and_never_stored(
    client, seeded, config_home
) -> None:
    answer = client.get(f"{ENDPOINT}?local=hide&provider=p1").json()

    assert answer["stale"] is False
    assert answer["totals"]["requests"] == 4
    assert derived_payloads.derived_cache().read("cost-breakdown-local-hide") is None


def test_the_payload_keeps_every_field_the_page_reads(
    client, seeded, config_home
) -> None:
    """Two fields are added and nothing is taken away."""

    body = client.get(f"{ENDPOINT}?local=hide").json()

    for field in (
        "totals",
        "by_source",
        "by_provider",
        "by_model",
        "by_harness",
        "by_day",
        "enabled",
        "cost_estimation_enabled",
        "cost_estimation_mode",
        "cost_source_litellm_enabled",
        "harness_labels",
    ):
        assert field in body, field
    assert body["enabled"] is True


def test_the_endpoint_is_local_only(seeded, config_home) -> None:
    remote = TestClient(create_test_app(), client=("10.0.0.5", 50000))

    assert remote.get(ENDPOINT).status_code == 403


def test_logging_off_still_answers_without_touching_the_cache(config_home) -> None:
    settings = Settings()
    settings.request_log_enabled = False
    client = TestClient(create_test_app(settings), client=("127.0.0.1", 50000))

    body = client.get(f"{ENDPOINT}?local=hide").json()

    assert body == {"enabled": False}
