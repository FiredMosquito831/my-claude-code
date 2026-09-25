"""Every proxy dial is a row on its attempt's ladder -- beside the tries.

``request_attempts.proxy_label`` is one last-write-wins slot, so a request that
dialled three addresses kept one, and the 09-16 park could only be read off
*which* label had survived. These tests pin the per-dial record that replaces
that inference: one row per dial, in order, with what each address answered,
what made the chain move on, and how long the connect and the tunnel took --
and they pin that nothing a ladder already said moves by a single byte.
"""

import time

import pytest

from my_claude_code.core.proxy_attribution import (
    _CURRENT,
    current_proxy,
    install_proxy_attribution,
    record_proxy,
)
from my_claude_code.core.upstream_ladder import (
    _LADDER,
    MAX_DIALS_PER_ATTEMPT,
    AttemptLadder,
    LadderTry,
    ProxyDial,
    current_ladder,
    dial_rows,
    install_ladder_trace,
    ladder_payload,
    ladder_proxy_label,
    ladder_root_cause,
    paused_ladder,
    record_proxy_connect,
    record_proxy_dial,
    record_proxy_handshake,
    record_upstream_try,
    record_upstream_wait,
)


@pytest.fixture(autouse=True)
def _fresh_slots():
    """Both slots are context variables; a sync test must not leak them."""

    _LADDER.set(None)
    _CURRENT.set(None)
    yield
    _LADDER.set(None)
    _CURRENT.set(None)


def _tracked():
    """The two slots exactly as ``RequestCapture`` installs them for a log."""

    install_proxy_attribution(on_dial=record_proxy_dial)
    ladder = install_ladder_trace()
    return ladder


def _chain_of_three() -> None:
    """429 on the first address, a dead second one, the third answers."""

    record_proxy("10.0.0.1:1080")
    record_upstream_try(key_index=0, key_label="sk-a...aaaa", status=429)
    record_proxy("10.0.0.2:3128")
    record_upstream_try(key_index=0, key_label="sk-a...aaaa", kind="ConnectError")
    record_proxy("10.0.0.3:1080")
    record_upstream_try(key_index=0, key_label="sk-a...aaaa", upstream_ms=812.0)


# ------------------------------------------------------------ the rows exist


def test_a_chain_that_dials_three_rungs_writes_three_rows() -> None:
    ladder = _tracked()
    _chain_of_three()

    rows = ladder_payload(ladder.ladders[0])["dials"]

    assert [row["proxy"] for row in rows] == [
        "10.0.0.1:1080",
        "10.0.0.2:3128",
        "10.0.0.3:1080",
    ]
    assert [row["outcome"] for row in rows] == ["switched", "switched", "answered"]
    # Why each switch happened: the answer the address gave.
    assert rows[0]["reason"] == "429"
    assert rows[1]["reason"] == "ConnectError"
    assert "reason" not in rows[2]
    # Where each dial sits among the tries: before try 1, 2 and 3.
    assert [row["at_try"] for row in rows] == [0, 1, 2]
    # A switch says how long moving on took; the answer does not.
    assert all(row["switch_ms"] >= 0 for row in rows[:2])
    assert "switch_ms" not in rows[2]
    assert all(row["verdict_ms"] >= 0 for row in rows)


def test_the_tries_a_ladder_already_recorded_are_unchanged() -> None:
    """Dials live beside ``tries``, so the count and the census cannot move."""

    with_dials = _tracked()
    _chain_of_three()
    observed = ladder_payload(with_dials.ladders[0], now=time.monotonic())

    _LADDER.set(None)
    _CURRENT.set(None)
    install_proxy_attribution()  # the label alone, as before this release
    without = install_ladder_trace()
    _chain_of_three()
    baseline = ladder_payload(without.ladders[0])

    assert "dials" not in baseline
    assert {key: value for key, value in observed.items() if key != "dials"} == (
        baseline
    )
    assert observed["summary"]["tries"] == 3
    assert len(observed["tries"]) == 3
    assert ladder_proxy_label(observed) == ladder_proxy_label(baseline)
    assert ladder_root_cause(observed) == ladder_root_cause(baseline)


def test_the_label_is_still_last_write_wins() -> None:
    _tracked()
    _chain_of_three()
    assert current_proxy() == "10.0.0.3:1080"


def test_an_attempt_with_no_chain_renders_exactly_as_before() -> None:
    ladder = _tracked()
    record_upstream_try(key_index=0, status=429)
    record_upstream_wait(1.5)
    record_upstream_try(key_index=0, upstream_ms=20.0)

    payload = ladder_payload(ladder.ladders[0])

    assert "dials" not in payload
    assert "dials_dropped" not in payload
    assert set(payload) == {"tries", "summary", "credentials"}


# ------------------------------------------------ a dial that never finished


def test_a_dial_that_never_completes_leaves_a_dialing_row() -> None:
    """The row a try cannot write: a try is only recorded when it ends."""

    ladder = _tracked()
    record_proxy("10.0.0.1:1080")
    record_upstream_try(key_index=0, status=429)
    record_proxy("10.0.0.2:1080")

    started = ladder.ladders[0].dials[-1].started
    rows = dial_rows(ladder.ladders[0], now=started + 42.0)

    assert rows[0]["outcome"] == "switched"
    assert rows[1]["outcome"] == "dialing"
    assert rows[1]["elapsed_ms"] == 42_000.0
    assert "reason" not in rows[1]
    assert "verdict_ms" not in rows[1]


def test_a_failure_that_never_switched_says_how_long_nothing_happened() -> None:
    """The 09-16 shape: one 429 on the first rung, then no second dial.

    Before this release that read as ``tries: 1`` over 47 minutes. Now the
    ladder says the address answered, the chain did not move on, and for how
    long -- "hung before the switch", told apart from "hung in the dial".
    """

    ladder = _tracked()
    record_proxy("173.249.24.121:1080")
    record_upstream_try(key_index=0, status=429)

    verdict_at = ladder.ladders[0].dials[0].verdict_at
    assert verdict_at is not None
    rows = dial_rows(ladder.ladders[0], now=verdict_at + 2_820.0)

    assert rows == [
        {
            "at_try": 0,
            "proxy": "173.249.24.121:1080",
            "verdict_ms": rows[0]["verdict_ms"],
            "outcome": "failed",
            "idle_ms": 2_820_000.0,
            "reason": "429",
        }
    ]


def test_a_later_attempt_closes_the_earlier_one() -> None:
    """An open dial is measured to the end of *its* attempt, not the request."""

    ladder = _tracked()
    record_proxy("10.0.0.1:1080")
    ladder.enter_attempt(1)
    closed_at = ladder.ladders[0].closed_at
    assert closed_at is not None

    rows = dial_rows(ladder.ladders[0], now=closed_at + 999.0)

    assert rows[0]["outcome"] == "dialing"
    assert rows[0]["elapsed_ms"] < 999_000.0


def test_announcing_the_same_attempt_twice_closes_nothing() -> None:
    ladder = _tracked()
    record_proxy("10.0.0.1:1080")
    ladder.enter_attempt(0)
    assert ladder.ladders[0].closed_at is None


def test_each_attempt_keeps_its_own_dials() -> None:
    ladder = _tracked()
    record_proxy("10.0.0.1:1080")
    record_upstream_try(key_index=0, status=429)
    ladder.enter_attempt(1)
    record_proxy("10.0.0.9:1080")
    record_upstream_try(key_index=0)

    first = ladder_payload(ladder.ladders[0])["dials"]
    second = ladder_payload(ladder.ladders[1])["dials"]
    assert [row["proxy"] for row in first] == ["10.0.0.1:1080"]
    assert first[0]["outcome"] == "failed"
    assert [row["proxy"] for row in second] == ["10.0.0.9:1080"]
    assert second[0]["outcome"] == "answered"


# ------------------------------------------------------- connect and tunnel


def test_connect_and_handshake_land_on_the_dial_in_flight() -> None:
    ladder = _tracked()
    record_proxy("10.0.0.1:1080")
    record_proxy_connect(0.0421)
    record_proxy_handshake(0.3104)
    record_upstream_try(key_index=0)

    row = ladder_payload(ladder.ladders[0])["dials"][0]
    assert row["connect_ms"] == 42.1
    assert row["handshake_ms"] == 310.4


def test_a_new_connection_forgets_the_last_ones_handshake() -> None:
    ladder = _tracked()
    record_proxy("10.0.0.1:1080")
    record_proxy_connect(0.01)
    record_proxy_handshake(0.02)
    record_proxy_connect(0.03)  # a retry on the same address, a new socket

    row = ladder_payload(ladder.ladders[0])["dials"][0]
    assert row["connect_ms"] == 30.0
    assert "handshake_ms" not in row


def test_a_reused_connection_claims_no_connect_time() -> None:
    ladder = _tracked()
    record_proxy("10.0.0.1:1080")
    record_upstream_try(key_index=0)
    row = ladder_payload(ladder.ladders[0])["dials"][0]
    assert "connect_ms" not in row
    assert "handshake_ms" not in row


def test_timings_with_no_dial_open_are_dropped() -> None:
    """A single-proxy provider has no chain and no dial to attach them to."""

    ladder = _tracked()
    record_proxy_connect(0.05)
    record_proxy_handshake(0.05)
    record_upstream_try(key_index=0)
    assert ladder.ladders[0].dials == []


# --------------------------------------------------- where nothing happens


def test_everything_is_a_no_op_outside_a_tracked_request() -> None:
    record_proxy("10.0.0.1:1080")
    record_proxy_dial("10.0.0.1:1080")
    record_proxy_connect(0.1)
    record_proxy_handshake(0.1)
    assert current_ladder() is None


def test_an_unlogged_request_keeps_only_the_label() -> None:
    """The log off installs no observer, so a dial is the label and no more."""

    slot = install_proxy_attribution()
    record_proxy("10.0.0.1:1080")
    assert slot.label == "10.0.0.1:1080"
    assert slot.on_dial is None


def test_a_probe_s_dials_are_not_this_request_s() -> None:
    ladder = _tracked()
    record_proxy("10.0.0.1:1080")
    with paused_ladder():
        record_proxy("10.0.0.2:1080")
        record_proxy_connect(0.5)
    assert [dial.proxy for dial in ladder.ladders[0].dials] == ["10.0.0.1:1080"]
    assert ladder.ladders[0].dials[0].connect_ms is None


def test_waits_and_probes_do_not_decide_a_dial() -> None:
    """Only an upstream try is an answer from the address."""

    ladder = _tracked()
    record_proxy("10.0.0.1:1080")
    record_upstream_try(key_index=0, status=429, source="probe")
    record_upstream_wait(1.0)
    row = ladder_payload(ladder.ladders[0])["dials"][0]
    assert row["outcome"] == "dialing"


def test_a_2xx_status_is_an_answer() -> None:
    ladder = _tracked()
    record_proxy("10.0.0.1:1080")
    record_upstream_try(key_index=0, status=200)
    row = ladder_payload(ladder.ladders[0])["dials"][0]
    assert row["outcome"] == "answered"
    assert "reason" not in row


def test_dials_past_the_cap_are_counted_and_not_misattributed() -> None:
    ladder = _tracked()
    for index in range(MAX_DIALS_PER_ATTEMPT + 3):
        record_proxy(f"10.0.{index // 250}.{index % 250}:1080")
    record_upstream_try(key_index=0, status=429)

    payload = ladder_payload(ladder.ladders[0])
    assert len(payload["dials"]) == MAX_DIALS_PER_ATTEMPT
    assert payload["dials_dropped"] == 3
    # The try belongs to a dial that was not stored, so no stored row claims it.
    assert payload["dials"][-1]["outcome"] == "switched"
    assert payload["dials"][-2]["outcome"] == "switched"
    assert all("reason" not in row for row in payload["dials"])


def test_a_hand_built_ladder_renders_without_the_holder() -> None:
    """``dial_rows`` reads only the dataclasses, so exports can call it."""

    ladder = AttemptLadder(
        tries=[LadderTry(status=429)],
        dials=[
            ProxyDial(
                proxy="direct",
                at_try=0,
                started=10.0,
                verdict="429",
                verdict_at=10.25,
            )
        ],
        closed_at=11.0,
    )
    assert dial_rows(ladder) == [
        {
            "at_try": 0,
            "proxy": "direct",
            "verdict_ms": 250.0,
            "outcome": "failed",
            "idle_ms": 750.0,
            "reason": "429",
        }
    ]


# ----------------------------- 7.52.1: a dead address costs no backoff sleep


@pytest.mark.asyncio
async def test_connect_failure_row_has_no_waited_ms() -> None:
    """The dial fails, the row says so, and no sleep is written onto it.

    Before 7.52.1 the retry loop slept its backoff before the proxied leg
    stopped the second dial, and back-filled that sleep onto the try as
    ``waited_ms`` (median 2,284 ms on the live log). The dial row and the try
    row are otherwise exactly what they were.
    """

    import httpx

    from my_claude_code.providers.runtime.proxy_leg import ProxiedLegRateLimiter

    ladder = _tracked()
    record_proxy("10.0.0.2:3128")

    async def dial():
        raise httpx.ConnectTimeout("timed out")

    limiter = ProxiedLegRateLimiter(
        rate_limit=0,
        rate_window=60,
        max_retries=2,
        backoff_base_seconds=2.0,
        backoff_max_seconds=2.0,
        backoff_jitter_seconds=0.0,
    )
    started = time.monotonic()
    with pytest.raises(httpx.ConnectTimeout):
        await limiter.execute_with_retry(dial)
    assert time.monotonic() - started < 0.5

    payload = ladder_payload(ladder.ladders[0], now=time.monotonic())
    (row,) = payload["tries"]
    assert row["kind"] == "ConnectTimeout"
    assert "status" not in row
    assert "waited_ms" not in row
    assert row["proxy"] == "10.0.0.2:3128"
    (dial_row,) = payload["dials"]
    assert dial_row["proxy"] == "10.0.0.2:3128"
    assert dial_row["reason"] == "ConnectTimeout"
