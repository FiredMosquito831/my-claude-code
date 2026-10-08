"""A dial corrected after it was announced (7.79.2).

``amend_proxy`` is how a leg says the dial its pool just announced is not what
happened: a Direct fallback withheld (nothing dialled -- the label goes back
to what it said, the ladder row goes) or Direct carried by the system proxy
(the label and the row say so). Only the latest dial can be corrected, and
only while it is still the latest.
"""

import pytest

from my_claude_code.core.proxy_attribution import (
    _CURRENT,
    DIRECT_PROXY_LABEL,
    SYSTEM_PROXY_LABEL_PREFIX,
    amend_proxy,
    install_proxy_attribution,
    is_direct_label,
    record_proxy,
    system_proxy_label,
)
from my_claude_code.core.proxy_speed import ProxySpeedLedger
from my_claude_code.core.upstream_ladder import (
    _LADDER,
    MAX_DIALS_PER_ATTEMPT,
    amend_proxy_dial,
    current_ladder,
    install_ladder_trace,
    record_proxy_dial,
    record_upstream_try,
)


@pytest.fixture(autouse=True)
def _fresh():
    _CURRENT.set(None)
    _LADDER.set(None)
    yield
    _CURRENT.set(None)
    _LADDER.set(None)


def _dials() -> list[str | None]:
    ladder = current_ladder()
    assert ladder is not None
    return [dial.proxy for dial in ladder.slot().dials]


def _tracked() -> None:
    install_proxy_attribution(on_dial=record_proxy_dial, on_amend=amend_proxy_dial)
    install_ladder_trace()


def test_a_withdrawn_dial_puts_the_label_back_and_drops_the_row() -> None:
    _tracked()
    record_proxy("a:1")
    record_proxy(DIRECT_PROXY_LABEL)

    amend_proxy(DIRECT_PROXY_LABEL, None)

    slot = _CURRENT.get()
    assert slot is not None and slot.label == "a:1"
    assert _dials() == ["a:1"]


def test_a_relabelled_dial_names_the_system_proxy() -> None:
    _tracked()
    record_proxy(DIRECT_PROXY_LABEL)

    amend_proxy(DIRECT_PROXY_LABEL, system_proxy_label("corp:3128"))

    slot = _CURRENT.get()
    assert slot is not None and slot.label == "direct via system proxy corp:3128"
    assert _dials() == ["direct via system proxy corp:3128"]


def test_only_the_latest_dial_can_be_corrected() -> None:
    _tracked()
    record_proxy(DIRECT_PROXY_LABEL)
    record_proxy("b:2")

    amend_proxy(DIRECT_PROXY_LABEL, None)

    slot = _CURRENT.get()
    assert slot is not None and slot.label == "b:2"
    assert _dials() == [DIRECT_PROXY_LABEL, "b:2"]


def test_a_dial_with_a_try_on_it_is_not_dropped() -> None:
    _tracked()
    record_proxy(DIRECT_PROXY_LABEL)
    record_upstream_try(status=503)

    amend_proxy(DIRECT_PROXY_LABEL, None)

    assert _dials() == [DIRECT_PROXY_LABEL]


def test_past_the_cap_a_withdrawn_dial_is_simply_not_counted() -> None:
    _tracked()
    for index in range(MAX_DIALS_PER_ATTEMPT):
        record_proxy(f"x:{index}")
    record_proxy(DIRECT_PROXY_LABEL)
    ladder = current_ladder()
    assert ladder is not None
    assert ladder.slot().dials_dropped == 1

    amend_proxy(DIRECT_PROXY_LABEL, None)

    assert ladder.slot().dials_dropped == 0
    assert len(ladder.slot().dials) == MAX_DIALS_PER_ATTEMPT


def test_untracked_and_unlogged_requests_are_left_alone() -> None:
    amend_proxy(DIRECT_PROXY_LABEL, None)  # no slot at all: a no-op
    install_proxy_attribution()  # a request whose log is off
    record_proxy(DIRECT_PROXY_LABEL)

    amend_proxy(DIRECT_PROXY_LABEL, "direct via system proxy corp:3128")

    slot = _CURRENT.get()
    assert slot is not None and slot.label == "direct via system proxy corp:3128"


def test_direct_labels_are_recognised_both_ways() -> None:
    assert is_direct_label(DIRECT_PROXY_LABEL)
    assert is_direct_label(system_proxy_label("corp:3128"))
    assert system_proxy_label("corp:3128").startswith(SYSTEM_PROXY_LABEL_PREFIX)
    assert not is_direct_label("203.0.113.7:1080")
    assert not is_direct_label(None)
    assert not is_direct_label("")


def test_the_speed_ledger_never_rates_a_direct_dial() -> None:
    """Direct through the system proxy is still no chain address to rate."""

    ledger = ProxySpeedLedger()
    rows = [
        {"proxy": "direct", "connect_ms": 10.0, "outcome": "answered"},
        {
            "proxy": "direct via system proxy corp:3128",
            "connect_ms": 10.0,
            "outcome": "answered",
        },
        {"proxy": "203.0.113.7:1080", "connect_ms": 10.0, "outcome": "answered"},
    ]

    # Only the chain address is a sample.
    assert ledger.note_dial_rows("prov", rows) == 1
