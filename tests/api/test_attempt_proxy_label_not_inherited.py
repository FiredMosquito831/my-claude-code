"""A later attempt never inherits an earlier attempt's proxy exit (7.78.3).

The per-request proxy slot (``core/proxy_attribution``) is last-write-wins and
only a chain's dial writes it. Before 7.78.3 nothing cleared it when the route
moved on, so an attempt on a provider with no chain carried the exit an earlier
attempt had dialled: on every try row, on ``request_attempts.proxy_label`` and
in the in-flight view -- 109 attempt rows in 48 h on the operator's own log,
every one with 0 dials.

These tests drive ``RequestCapture`` and ``MediaCapture`` in the order the
handler, the executor and the providers do (the proxy pool calls
``record_proxy`` immediately before each dial, the retry frame records the
try), then read back what the store kept.
"""

from typing import Any, Literal

import pytest

from my_claude_code.api.media_capture import MediaCapture
from my_claude_code.api.request_capture import RequestCapture
from my_claude_code.application.execution import RouteAttemptRecord
from my_claude_code.application.media.request import (
    MediaAttempt,
    MediaRail,
    MediaRequest,
)
from my_claude_code.application.routing import ResolvedModel, RoutedMessagesPlan
from my_claude_code.config.media_surfaces import MEDIA_OPERATION_IMAGE_GENERATE
from my_claude_code.config.reasoning import ReasoningPreference
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic.models import Message, MessagesRequest
from my_claude_code.core.proxy_attribution import (
    _CURRENT,
    DIRECT_PROXY_LABEL,
    record_proxy,
)
from my_claude_code.core.request_log import RequestLogStore
from my_claude_code.core.upstream_ladder import (
    _LADDER,
    paused_ladder,
    record_proxy_connect,
    record_proxy_handshake,
    record_upstream_try,
)
from tests.api.test_request_capture import _vision_router

EXIT_A = "173.249.24.121:1080"
EXIT_B = "45.77.244.108:1080"


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


def _plan() -> RoutedMessagesPlan:
    """``nvidia_nim/blind`` with ``groq/backup`` behind it: two providers."""

    return _vision_router().resolve_messages_plan(
        MessagesRequest(
            model="claude-sonnet-4-6",
            max_tokens=8,
            messages=[Message(role="user", content="hi")],
        )
    )


def _capture(store: RequestLogStore | None, request_id: str) -> RequestCapture:
    return RequestCapture(
        store,
        request_id=request_id,
        endpoint="/v1/messages",
        protocol="anthropic",
        stream=True,
        requested_model="claude-sonnet-4-6",
        input_text="hi",
        params=None,
    )


def _result(
    plan: RoutedMessagesPlan,
    index: int,
    outcome: Literal["succeeded", "failed", "skipped"],
    **extra: Any,
) -> RouteAttemptRecord:
    resolved = plan.attempts[index].resolved
    return RouteAttemptRecord(
        attempt=index,
        provider_id=resolved.provider_id,
        model_ref=resolved.provider_model_ref,
        outcome=outcome,
        **extra,
    )


def _attempts(store: RequestLogStore, request_id: str) -> list[dict[str, Any]]:
    row = store.get_request(request_id)
    assert row is not None
    return sorted(row["route_attempts"], key=lambda attempt: attempt["attempt"])


def _try_proxies(attempt: dict[str, Any]) -> list[str | None]:
    return [entry.get("proxy") for entry in attempt["params"]["ladder"]["tries"]]


def _rate_limited_on_a_chain(capture: RequestCapture, plan: RoutedMessagesPlan) -> None:
    """Attempt 0: announced twice, one dial, one 429 through it."""

    capture.set_routing(plan.attempts[0], 0)  # the handler
    capture.set_routing(plan.attempts[0], 0)  # the executor
    record_proxy(EXIT_A)
    record_proxy_connect(0.05)
    record_proxy_handshake(0.2)
    record_upstream_try(key_index=0, key_label="nvap...0001", status=429)


# ------------------------------------------------------------ the regression


def test_a_fallback_to_a_provider_with_no_chain_records_no_proxy(store) -> None:
    """The defect: attempt 1 went direct and was stored with attempt 0's exit."""

    plan = _plan()
    capture = _capture(store, "req_fallback")
    capture.set_plan(plan)
    _rate_limited_on_a_chain(capture, plan)
    capture.set_routing(plan.attempts[1], 1)
    # ``groq`` has no chain: nothing dials, the retry frame records the try.
    record_upstream_try(key_index=0, key_label="gsk_...0002", upstream_ms=300.0)
    capture.record_attempt_result(
        _result(plan, 0, "failed", error_kind="rate_limit", duration_ms=900.0)
    )
    capture.record_attempt_result(_result(plan, 1, "succeeded", duration_ms=400.0))
    capture.finish_success("ok")
    store.close()

    first, second = _attempts(store, "req_fallback")
    # Attempt 0 keeps exactly what it always recorded.
    assert first["proxy_label"] == EXIT_A
    assert _try_proxies(first) == [EXIT_A]
    assert [dial["proxy"] for dial in first["params"]["ladder"]["dials"]] == [EXIT_A]
    # Attempt 1 dialled nothing, so it claims nothing.
    assert second["proxy_label"] is None
    assert _try_proxies(second) == [None]
    assert "proxy" not in second["params"]["ladder"]["tries"][0]
    assert "dials" not in second["params"]["ladder"]


def test_the_fallback_reads_exactly_as_a_first_attempt_on_that_provider(
    store,
) -> None:
    """The cleared state is the value a request that started there records."""

    plan = _plan()
    fallback = _capture(store, "req_second")
    fallback.set_plan(plan)
    _rate_limited_on_a_chain(fallback, plan)
    fallback.set_routing(plan.attempts[1], 1)
    record_upstream_try(key_index=0, key_label="gsk_...0002", upstream_ms=300.0)
    fallback.record_attempt_result(
        _result(plan, 0, "failed", error_kind="rate_limit", duration_ms=900.0)
    )
    fallback.record_attempt_result(_result(plan, 1, "succeeded", duration_ms=400.0))
    fallback.finish_success("ok")
    _LADDER.set(None)
    _CURRENT.set(None)

    # The same provider as the only attempt of a request that never proxied.
    alone = _capture(store, "req_alone")
    alone.set_plan(plan)
    alone.set_routing(plan.attempts[1], 1)
    alone.set_routing(plan.attempts[1], 1)
    record_upstream_try(key_index=0, key_label="gsk_...0002", upstream_ms=300.0)
    alone.record_attempt_result(_result(plan, 1, "succeeded", duration_ms=400.0))
    alone.finish_success("ok")
    store.close()

    second = _attempts(store, "req_second")[1]
    (only,) = _attempts(store, "req_alone")
    assert second["proxy_label"] == only["proxy_label"]
    assert second["proxy_label"] is None
    assert second["params"]["ladder"]["tries"] == only["params"]["ladder"]["tries"]
    assert ("dials" in second["params"]["ladder"]) is (
        "dials" in only["params"]["ladder"]
    )


@pytest.mark.parametrize("logged", [True, False])
def test_the_in_flight_view_drops_the_previous_exit(store, logged: bool) -> None:
    """Live too, and whether or not the request log is on."""

    plan = _plan()
    capture = _capture(store if logged else None, "req_live")
    capture.set_plan(plan)
    capture.set_routing(plan.attempts[0], 0)
    record_proxy(EXIT_A)
    assert capture.watchdog_progress().proxy_label == EXIT_A
    capture.set_routing(plan.attempts[1], 1)
    progress = capture.watchdog_progress()
    assert progress.attempt_index == 1
    assert progress.proxy_label is None


def test_a_describe_dial_does_not_leak_into_attempt_0(store) -> None:
    """Describe mode runs before the plan, with the ladder paused, through the
    same slot -- its chain's exit is not the client's attempt 0's."""

    capture = _capture(store, "req_described")
    # ``messages.py`` builds the capture, then describes, then plans.
    with paused_ladder():
        record_proxy(EXIT_B)
    assert capture.watchdog_progress().proxy_label == EXIT_B
    plan = _plan()
    capture.set_plan(plan)
    capture.set_routing(plan.attempts[0], 0)
    capture.set_routing(plan.attempts[0], 0)
    record_upstream_try(key_index=0, key_label="nvap...0001", upstream_ms=300.0)
    capture.record_attempt_result(_result(plan, 0, "succeeded", duration_ms=400.0))
    capture.finish_success("ok")
    store.close()

    (only,) = _attempts(store, "req_described")
    assert only["proxy_label"] is None
    assert _try_proxies(only) == [None]


# ------------------------------------------------------- what does not change


def test_announcing_attempt_0_twice_keeps_what_was_dialled_between(store) -> None:
    """The handler and the executor both announce attempt 0; the second is not
    a new attempt, so a dial made between the two stays."""

    plan = _plan()
    capture = _capture(store, "req_twice")
    capture.set_plan(plan)
    capture.set_routing(plan.attempts[0], 0)
    record_proxy(EXIT_A)
    capture.set_routing(plan.attempts[0], 0)
    assert capture.watchdog_progress().proxy_label == EXIT_A
    record_upstream_try(key_index=0, key_label="nvap...0001", upstream_ms=300.0)
    capture.record_attempt_result(_result(plan, 0, "succeeded", duration_ms=400.0))
    capture.finish_success("ok")
    store.close()

    (only,) = _attempts(store, "req_twice")
    assert only["proxy_label"] == EXIT_A
    assert _try_proxies(only) == [EXIT_A]


def test_a_switch_inside_one_attempt_is_last_write_wins_as_before(store) -> None:
    """A chain moving to its next exit, and the attempt re-announced in the
    middle (a widened budget, a retry of the same position), clear nothing."""

    plan = _plan()
    capture = _capture(store, "req_switch")
    capture.set_plan(plan)
    capture.set_routing(plan.attempts[0], 0)
    record_proxy(EXIT_A)
    record_upstream_try(key_index=0, key_label="nvap...0001", status=429)
    record_proxy(EXIT_B)
    capture.set_routing(plan.attempts[0], 0)
    record_upstream_try(key_index=0, key_label="nvap...0001", status=503)
    record_upstream_try(key_index=0, key_label="nvap...0001", upstream_ms=300.0)
    capture.record_attempt_result(_result(plan, 0, "succeeded", duration_ms=400.0))
    capture.finish_success("ok")
    store.close()

    (only,) = _attempts(store, "req_switch")
    assert _try_proxies(only) == [EXIT_A, EXIT_B, EXIT_B]
    assert only["proxy_label"] == EXIT_B
    dials = only["params"]["ladder"]["dials"]
    assert [(dial["proxy"], dial["outcome"]) for dial in dials] == [
        (EXIT_A, "switched"),
        (EXIT_B, "answered"),
    ]


def test_a_fallback_between_two_chains_keeps_each_attempts_own_exit(store) -> None:
    """Every provider on the route has a chain: each attempt dials before its
    first try, so every row is what it always was -- Direct included."""

    plan = _plan()
    capture = _capture(store, "req_chains")
    capture.set_plan(plan)
    _rate_limited_on_a_chain(capture, plan)
    capture.set_routing(plan.attempts[1], 1)
    record_proxy(EXIT_B)
    record_upstream_try(key_index=0, key_label="gsk_...0002", status=502)
    record_proxy(DIRECT_PROXY_LABEL)
    record_upstream_try(key_index=0, key_label="gsk_...0002", upstream_ms=300.0)
    capture.record_attempt_result(
        _result(plan, 0, "failed", error_kind="rate_limit", duration_ms=900.0)
    )
    capture.record_attempt_result(_result(plan, 1, "succeeded", duration_ms=400.0))
    capture.finish_success("ok")
    store.close()

    first, second = _attempts(store, "req_chains")
    assert (first["proxy_label"], _try_proxies(first)) == (EXIT_A, [EXIT_A])
    assert (second["proxy_label"], _try_proxies(second)) == (
        DIRECT_PROXY_LABEL,
        [EXIT_B, DIRECT_PROXY_LABEL],
    )
    assert [dial["proxy"] for dial in second["params"]["ladder"]["dials"]] == [
        EXIT_B,
        DIRECT_PROXY_LABEL,
    ]


# ------------------------------------------------------------------- media


def _media_attempt(provider: str) -> MediaAttempt:
    return MediaAttempt(
        MediaRequest(
            operation=MEDIA_OPERATION_IMAGE_GENERATE,
            rail=MediaRail.IMAGE,
            model="client-model",
            body={"prompt": "p"},
        ),
        ResolvedModel(
            original_model="client-model",
            provider_id=provider,
            provider_model="image-model",
            provider_model_ref=f"{provider}/image-model",
            reasoning_preference=ReasoningPreference.INHERIT,
        ),
    )


def _media_capture() -> MediaCapture:
    return MediaCapture(
        Settings.model_validate({"REQUEST_LOG_ENABLED": "false"}),
        request_id="req_media",
        endpoint="/v1/images/generations",
        request=_media_attempt("opencode").request,
        headers={},
    )


def test_a_media_fallback_to_a_provider_with_no_chain_carries_no_exit() -> None:
    """The label that names the answering leg -- stored on a video job and used
    to pin its later reads -- belongs to the attempt that dialled it."""

    capture = _media_capture()
    capture.on_attempt(_media_attempt("opencode"), 0)
    record_proxy(EXIT_A)
    capture.on_attempt(_media_attempt("opencode"), 0)  # re-announced: no change
    assert capture.proxy_label == EXIT_A
    capture.on_attempt(_media_attempt("agnes"), 1)
    assert capture.proxy_label is None


def test_a_media_fallback_to_another_chain_names_that_chains_exit() -> None:
    capture = _media_capture()
    capture.on_attempt(_media_attempt("opencode"), 0)
    record_proxy(EXIT_A)
    capture.on_attempt(_media_attempt("agnes"), 1)
    record_proxy(EXIT_B)
    assert capture.proxy_label == EXIT_B
