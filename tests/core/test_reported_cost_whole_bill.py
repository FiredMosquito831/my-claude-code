"""E5 (7.109.0): ``whole_bill`` answers the BYOK question for a declaring host only."""

from types import SimpleNamespace

import pytest

from my_claude_code.core.reported_cost import (
    install_reported_cost,
    record_reported_usage,
    reported_usage_cost,
)

#: Every usage shape the provider-reported rung reads today.
SHAPES = [
    {"cost": 0.5},
    {"cost": 0.5, "is_byok": False},
    {"cost": 0.02, "is_byok": True, "cost_details": {"upstream_inference_cost": 0.4}},
    {"cost": 0.02, "is_byok": True},
    {"cost": 0.5, "cost_details": {}},
    {"cost": "0.5"},
    {"prompt_tokens": 3},
    SimpleNamespace(model_extra={"cost": 0.5}),
]


@pytest.mark.parametrize("usage", SHAPES)
def test_without_the_keyword_every_shape_reads_as_it_always_did(usage: object) -> None:
    default = install_reported_cost()
    record_reported_usage(usage)
    explicit = install_reported_cost()
    record_reported_usage(usage, whole_bill=False)
    assert default == explicit


def test_a_bare_cost_is_declined_unless_the_host_declares_a_whole_bill() -> None:
    slot = install_reported_cost()
    record_reported_usage({"cost": 0.5})
    assert slot.total_usd is None
    slot = install_reported_cost()
    record_reported_usage({"cost": 0.5}, whole_bill=True)
    assert slot.total_usd == 0.5


def test_a_whole_bill_never_overrides_what_the_block_said() -> None:
    slot = install_reported_cost()
    record_reported_usage({"cost": 0.02, "is_byok": True}, whole_bill=True)
    assert slot.is_byok is True
    assert slot.total_usd is None


def test_no_cost_stays_not_reported() -> None:
    slot = install_reported_cost()
    record_reported_usage({"prompt_tokens": 3}, whole_bill=True)
    assert slot.cost_usd is None and slot.total_usd is None
    assert reported_usage_cost({"prompt_tokens": 3}) is None
    assert reported_usage_cost({"cost": 0}) == 0.0
