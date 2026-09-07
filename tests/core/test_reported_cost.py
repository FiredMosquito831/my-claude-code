"""Reading a host's own answer about what a request cost.

Every shape here was taken from OpenRouter's usage-accounting documentation and
from a real final SSE chunk, but nothing in the module under test names
OpenRouter: any OpenAI-shaped host reporting the same keys is read the same way.
"""

from typing import ClassVar

import pytest

from my_claude_code.core.reported_cost import (
    ReportedCost,
    install_reported_cost,
    paused_reported_cost,
    record_reported_usage,
    reported_cost,
)


def _usage(**overrides):
    """A final usage chunk in the shape a reporting host actually sends."""
    usage = {
        "prompt_tokens": 194,
        "completion_tokens": 2,
        "total_tokens": 196,
        "cost": 0.95,
        "cost_details": {"upstream_inference_cost": None},
        "is_byok": False,
        "prompt_tokens_details": {"cached_tokens": 0},
        "completion_tokens_details": {"reasoning_tokens": 0},
    }
    usage.update(overrides)
    return usage


def test_usage_cost_is_read_from_the_final_usage_block():
    slot = install_reported_cost()
    record_reported_usage(_usage())
    assert slot.cost_usd == 0.95
    assert slot.total_usd == 0.95


def test_recording_outside_a_tracked_request_is_a_no_op():
    """Providers call this unconditionally; a bare unit test must not blow up."""
    with paused_reported_cost():
        record_reported_usage(_usage())
        assert reported_cost() is None


def test_a_byok_response_is_not_priced_at_the_surcharge_alone():
    """The trap: on BYOK, ``cost`` is OpenRouter's ~5% cut, not the bill."""
    slot = install_reported_cost()
    record_reported_usage(
        _usage(
            cost=0.05,
            is_byok=True,
            cost_details={"upstream_inference_cost": 1.0},
        )
    )
    assert slot.cost_usd == 0.05
    assert slot.total_usd == pytest.approx(1.05)


def test_a_byok_response_with_no_upstream_figure_declines_the_rung():
    """A number that might be a surcharge is worse than an honest estimate."""
    slot = install_reported_cost()
    record_reported_usage(
        _usage(cost=0.05, is_byok=True, cost_details={"server_tool_cost": 0})
    )
    assert slot.cost_usd == 0.05
    assert slot.total_usd is None


def test_a_bare_cost_with_no_itemisation_declines_the_rung():
    """Without ``is_byok`` or ``cost_details`` there is no way to tell which it is."""
    slot = install_reported_cost()
    record_reported_usage({"cost": 0.42})
    assert slot.cost_usd == 0.42
    assert slot.is_byok is None
    assert slot.total_usd is None


def test_an_itemised_response_with_no_upstream_charge_reads_as_not_byok():
    slot = install_reported_cost()
    record_reported_usage(
        {"cost": 0.42, "cost_details": {"upstream_inference_cost": None}}
    )
    assert slot.is_byok is False
    assert slot.total_usd == 0.42


def test_a_host_that_reports_no_cost_leaves_the_slot_empty():
    slot = install_reported_cost()
    record_reported_usage({"prompt_tokens": 10, "completion_tokens": 2})
    assert slot.cost_usd is None
    assert slot.total_usd is None


def test_reasoning_tokens_are_collected_for_the_pricing_ladder():
    slot = install_reported_cost()
    record_reported_usage(_usage(completion_tokens_details={"reasoning_tokens": 128}))
    assert slot.reasoning_tokens == 128


def test_a_cost_reported_as_a_string_is_read_as_a_number():
    """Some catalogues publish decimal strings; a usage block may too."""
    slot = install_reported_cost()
    record_reported_usage({"cost": "0.25", "cost_details": {}})
    assert slot.cost_usd == 0.25


def test_a_sdk_model_extra_carries_the_cost():
    """The reporting fields are undeclared extras on the OpenAI usage model."""

    class _Usage:
        prompt_tokens = 10
        model_extra: ClassVar[dict[str, object]] = {
            "cost": 0.5,
            "cost_details": {},
            "is_byok": False,
        }

    slot = install_reported_cost()
    record_reported_usage(_Usage())
    assert slot.total_usd == 0.5


def test_pausing_keeps_a_describe_hop_off_the_parent_row():
    parent = install_reported_cost()
    with paused_reported_cost():
        record_reported_usage(_usage(cost=9.99))
    assert parent.cost_usd is None
    record_reported_usage(_usage(cost=0.01))
    assert parent.cost_usd == 0.01


def test_an_empty_collector_reports_nothing():
    assert ReportedCost().total_usd is None
