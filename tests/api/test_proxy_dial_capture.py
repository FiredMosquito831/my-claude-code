"""The request log stores each proxy dial on the attempt it belongs to.

``RequestCapture`` is what joins ``record_proxy`` to the ladder, so these tests
go through it and through the store: what lands in ``request_attempts`` is what
the modal and the exports read.
"""

from typing import Literal

import pytest

from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.application.execution import RouteAttemptRecord
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.proxy_attribution import _CURRENT, record_proxy
from my_claude_code.core.request_log import RequestLogStore
from my_claude_code.core.upstream_ladder import (
    _LADDER,
    current_ladder,
    record_proxy_connect,
    record_proxy_handshake,
    record_upstream_try,
)
from tests.api.test_request_capture import _make_capture, _vision_router


@pytest.fixture
def store(tmp_path):
    store = RequestLogStore(tmp_path / "requests.db")
    yield store
    store.close()


@pytest.fixture(autouse=True)
def _fresh_slots():
    """Both slots are context variables a worker keeps between tests."""

    _LADDER.set(None)
    _CURRENT.set(None)
    yield
    _LADDER.set(None)
    _CURRENT.set(None)


def _attempt(
    index: int, outcome: Literal["succeeded", "failed", "skipped"], **extra
) -> RouteAttemptRecord:
    return RouteAttemptRecord(
        attempt=index,
        provider_id="opencode",
        model_ref="opencode/muse",
        outcome=outcome,
        **extra,
    )


def test_a_logged_request_stores_every_dial_and_the_switch(store) -> None:
    capture = _make_capture(store, request_id="req_dials")
    record_proxy("173.249.24.121:1080")
    record_proxy_connect(0.05)
    record_proxy_handshake(0.2)
    record_upstream_try(key_index=0, key_label="sk-8...Kofx", status=429)
    record_proxy("45.77.244.108:1080")
    record_upstream_try(key_index=0, key_label="sk-8...Kofx", upstream_ms=900.0)

    capture.record_attempt_result(_attempt(0, "succeeded", duration_ms=1200.0))
    capture.finish_success("ok")
    store.close()

    attempt = store.get_request("req_dials")["route_attempts"][0]
    dials = attempt["params"]["ladder"]["dials"]
    assert [(d["proxy"], d["outcome"]) for d in dials] == [
        ("173.249.24.121:1080", "switched"),
        ("45.77.244.108:1080", "answered"),
    ]
    assert dials[0]["reason"] == "429"
    assert (dials[0]["connect_ms"], dials[0]["handshake_ms"]) == (50.0, 200.0)
    # The denormalised columns say exactly what they said before.
    assert attempt["ladder_tries"] == 2
    assert attempt["proxy_label"] == "45.77.244.108:1080"


def test_a_dial_that_never_completed_is_stored_with_no_try(store) -> None:
    """The row that used to be missing altogether: no try, so no ladder."""

    capture = _make_capture(store, request_id="req_parked")
    record_proxy("173.249.24.121:1080")

    capture.record_attempt_result(
        _attempt(0, "failed", error_kind="interrupted", duration_ms=5000.0)
    )
    capture.finish_error(RuntimeError("client went away"))
    store.close()

    attempt = store.get_request("req_parked")["route_attempts"][0]
    ladder = attempt["params"]["ladder"]
    assert ladder["tries"] == []
    assert ladder["summary"]["tries"] == 0
    assert ladder["root_cause"] == ""
    assert [d["outcome"] for d in ladder["dials"]] == ["dialing"]
    # Not measured, as before: no try was recorded.
    assert attempt["ladder_tries"] is None


def test_an_unlogged_request_installs_no_observer() -> None:
    capture = _make_capture(None, request_id="req_off")
    assert capture.enabled is False
    record_proxy("10.0.0.1:1080")
    slot = _CURRENT.get()
    assert slot is not None
    assert slot.label == "10.0.0.1:1080"
    assert slot.on_dial is None
    assert current_ladder() is None


def test_moving_to_the_next_attempt_closes_the_last_one(store) -> None:
    plan = _vision_router().resolve_messages_plan(
        MessagesRequest(
            model="claude-sonnet-4-6",
            max_tokens=8,
            messages=[Message(role="user", content="hi")],
        )
    )
    capture = RequestCapture(
        store,
        request_id="req_close",
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="claude-sonnet-4-6",
        input_text="hi",
        params=None,
    )
    capture.set_plan(plan)
    capture.set_routing(plan.attempts[0], 0)
    record_proxy("10.0.0.1:1080")
    ladder = current_ladder()
    assert ladder is not None
    capture.set_routing(plan.attempts[0], 0)  # announced twice: not a new one
    assert ladder.ladders[0].closed_at is None
    capture.set_routing(plan.attempts[1], 1)
    assert ladder.ladders[0].closed_at is not None
    assert ladder.current_attempt == 1
    capture.finish_success("ok")


# ------------------------------------------------------------- 7.54.0 speed


def _speed(address: str, provider: str = "opencode"):
    from my_claude_code.core.proxy_speed import PROXY_SPEED

    return PROXY_SPEED.score(address, provider, failure_cost_ms=5000.0, slow_ms=3000.0)


def test_dial_rows_feed_speed_ledger(store) -> None:
    """The stored dial rows are the samples: one failure, one answered dial."""

    capture = _make_capture(store, request_id="req_speed")
    record_proxy("173.249.24.121:1080")
    record_proxy_connect(0.05)
    record_proxy_handshake(0.2)
    record_upstream_try(key_index=0, key_label="sk-8...Kofx", status=429)
    record_proxy("45.77.244.108:1080")
    record_proxy_connect(0.03)
    record_proxy_handshake(0.12)
    record_upstream_try(key_index=0, key_label="sk-8...Kofx", upstream_ms=900.0)

    capture.record_attempt_result(_attempt(0, "succeeded", duration_ms=1200.0))
    capture.finish_success("ok")
    store.close()

    failed = _speed("173.249.24.121:1080")
    answered = _speed("45.77.244.108:1080")
    assert (failed.successes, failed.samples) == (0, 1)
    assert (answered.successes, answered.samples) == (1, 1)
    assert answered.setup_ms == 150.0
    # Filed under the attempt's provider, and nowhere else.
    assert _speed("45.77.244.108:1080", "nvidia_nim").samples == 0


def test_no_ledger_write_when_request_log_off(tmp_path) -> None:
    from my_claude_code.core.proxy_speed import PROXY_SPEED

    capture = _make_capture(None, request_id="req_off_speed")
    record_proxy("45.77.244.108:1080")
    capture.record_attempt_result(
        _attempt(0, "succeeded", duration_ms=1200.0, ttft_ms=900.0)
    )
    capture.finish_success("ok")

    assert len(PROXY_SPEED) == 0
    assert not PROXY_SPEED.dirty


def test_live_ttft_is_normalised_against_the_models_own_median(store) -> None:
    """Fake upstream timings only: three direct answers set the model's median,
    then a proxied one twice as slow is recorded as a 2.0x first token."""

    for index, ttft in enumerate((1000.0, 1200.0, 800.0)):
        capture = _make_capture(store, request_id=f"req_base_{index}")
        capture.record_attempt_result(
            _attempt(0, "succeeded", duration_ms=3000.0, ttft_ms=ttft)
        )
        capture.finish_success("ok")
        _LADDER.set(None)
        _CURRENT.set(None)
    for index in range(3):
        capture = _make_capture(store, request_id=f"req_proxied_{index}")
        record_proxy("45.77.244.108:1080")
        record_upstream_try(key_index=0, key_label="sk-8...Kofx", upstream_ms=900.0)
        capture.record_attempt_result(
            _attempt(0, "succeeded", duration_ms=3000.0, ttft_ms=2000.0)
        )
        capture.finish_success("ok")
        _LADDER.set(None)
        _CURRENT.set(None)

    score = _speed("45.77.244.108:1080")
    assert score.live_samples == 3
    # The first proxied answer is compared with the three direct ones (1000
    # median -> 2.0); later ones with a median that already includes it.
    assert 1.5 <= score.ttft_factor <= 2.0
