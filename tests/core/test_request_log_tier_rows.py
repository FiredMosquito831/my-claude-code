"""What one tier request looks like in the request log.

No schema change: ``requested_model`` already stores whatever arrived, and the
harness column landed in 6.37.0. What is new is the three keys in ``params``,
and they exist because the two columns cannot answer the question between them
-- an override naming the same ref as the global chain is indistinguishable from
no override at all.
"""

from typing import Any

from my_claude_code.api.request_capture import RequestCapture, build_capture
from my_claude_code.application.routing import ModelRouter
from my_claude_code.config.harness_tiers import HarnessTierOverride, HarnessTiers
from my_claude_code.config.settings import Settings
from my_claude_code.core.anthropic import Message, MessagesRequest
from my_claude_code.core.client_fingerprint import HARNESS_HEADER

PRIMARY = "nvidia_nim/primary"
OVERRIDE = "open_router/override"


def _settings(**values: Any) -> Settings:
    return Settings(**values)


def _request(model: str) -> MessagesRequest:
    return MessagesRequest(
        model=model, max_tokens=16, messages=[Message(role="user", content="hi")]
    )


def _capture(settings: Settings, request: MessagesRequest, headers) -> RequestCapture:
    return build_capture(
        settings,
        request,
        request_id="req-1",
        endpoint="/v1/messages",
        protocol="anthropic",
        headers=headers,
    )


def _record(capture: RequestCapture):
    return capture._record


def test_a_tier_request_logs_the_alias_the_harness_and_the_resolved_ref() -> None:
    """The row an operator has to be able to find and read.

    ``requested_model`` is the name they typed into the agent, ``harness`` is
    the agent, ``params.tier_source`` says whether their own override fired.
    """

    settings = _settings(model=PRIMARY, REQUEST_LOG_ENABLED=True)
    tiers = HarnessTiers(
        harnesses={"opencode": {"best": HarnessTierOverride(model=OVERRIDE)}}
    )
    router = ModelRouter(settings, harness_tiers=lambda: tiers)
    request = _request("mcc/best")

    capture = _capture(settings, request, {HARNESS_HEADER: "opencode"})
    plan = router.resolve_messages_plan(request, harness="opencode")
    capture.set_plan(plan)
    capture.set_routing(plan.primary)

    record = _record(capture)
    assert record.requested_model == "mcc/best"
    assert record.harness == "opencode"
    # ``resolved_model`` is the provider-native id and ``provider`` the routing
    # provider: together they are the ref the alias resolved to.
    assert f"{record.provider}/{record.resolved_model}" == OVERRIDE
    assert record.route_chain == OVERRIDE
    assert record.params["tier"] == "best"
    assert record.params["tier_source"] == "override"
    assert record.params["tier_harness"] == "opencode"


def test_a_tier_that_followed_the_global_chain_says_so() -> None:
    """ "Did my override fire?" has to be answerable from the row alone."""

    settings = _settings(model=PRIMARY, REQUEST_LOG_ENABLED=True)
    router = ModelRouter(settings, harness_tiers=HarnessTiers)
    request = _request("mcc/medium")

    capture = _capture(settings, request, {HARNESS_HEADER: "crush"})
    plan = router.resolve_messages_plan(request, harness="crush")
    capture.set_plan(plan)

    record = _record(capture)
    assert record.params["tier"] == "medium"
    assert record.params["tier_source"] == "global"
    assert record.params["tier_harness"] == "crush"


def test_a_non_tier_request_gains_no_tier_keys() -> None:
    """Every existing row and every export keeps its shape.

    The keys are written only for a request that named a tier, so nothing that
    reads ``params`` today has to learn about them.
    """

    settings = _settings(model=PRIMARY, REQUEST_LOG_ENABLED=True)
    router = ModelRouter(settings, harness_tiers=HarnessTiers)
    request = _request("claude-opus-5")

    capture = _capture(settings, request, {"user-agent": "claude-cli/2.0.1"})
    capture.set_plan(router.resolve_messages_plan(request, harness="claude"))

    params = _record(capture).params or {}
    assert "tier" not in params
    assert "tier_source" not in params
    assert "tier_harness" not in params


def test_the_client_supplied_params_survive_the_tier_keys() -> None:
    """``params`` is the client's ask; the tier is added beside it, not over it."""

    settings = _settings(model=PRIMARY, REQUEST_LOG_ENABLED=True)
    router = ModelRouter(settings, harness_tiers=HarnessTiers)
    request = _request("mcc/best")
    request.temperature = 0.5

    capture = _capture(settings, request, {HARNESS_HEADER: "opencode"})
    capture.set_plan(router.resolve_messages_plan(request, harness="opencode"))

    params = _record(capture).params
    assert params["temperature"] == 0.5
    assert params["tier"] == "best"
