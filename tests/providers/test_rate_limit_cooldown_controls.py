"""The 429 cooldown is the operator's to set, all the way down to zero.

Three settings, one rule, and one function that owns it
(:meth:`RateLimitCooldown.resolve`). The first test in this file is the one
that matters most: with every new setting at its default, the answer for a
matrix of real 429 shapes is byte-for-byte the answer 7.21.0 gave. The golden
in ``rate_limit_cooldown_baseline.json`` was generated from a detached
worktree at ``e852fdec`` (v7.21.0) and is regenerated here in-process.
"""

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from my_claude_code.config.settings import Settings
from my_claude_code.core.credential_rotation import PoolHealthState
from my_claude_code.core.failures import ExecutionFailure, FailureKind
from my_claude_code.core.rate_limit import (
    DEFAULT_RATE_LIMIT_COOLDOWN,
    MAX_HOST_STATED_COOLDOWN_SECONDS,
    RATE_LIMIT_COOLDOWN_MODES,
    RateLimitCooldown,
)
from my_claude_code.core.upstream_ladder import (
    _LADDER,
    install_ladder_trace,
    ladder_payload,
    ladder_root_cause,
    record_upstream_try,
)
from my_claude_code.providers.base import ProviderConfig
from my_claude_code.providers.credential_rotation import CredentialRotationState
from my_claude_code.providers.failure_policy import (
    classify_provider_failure,
    rate_limit_cooldown_seconds,
    retry_after_from_error,
)
from my_claude_code.providers.rate_limit import ProviderRateLimiter

BASELINE = Path(__file__).with_name("rate_limit_cooldown_baseline.json")

LOCKOUT_TIERS = (300.0, 3600.0, 86400.0)


def _header_error(value: str) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://upstream.invalid/v1/chat")
    response = httpx.Response(429, headers={"retry-after": value}, request=request)
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


def _body_error(seconds: object) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://upstream.invalid/v1/chat")
    response = httpx.Response(
        429, json={"error": {"retryAfter": seconds}}, request=request
    )
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


def _bare_error() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://upstream.invalid/v1/chat")
    response = httpx.Response(429, request=request)
    return httpx.HTTPStatusError("rate limited", request=request, response=response)


def _cases() -> dict[str, httpx.HTTPStatusError]:
    return {
        "header-15": _header_error("15"),
        "header-60": _header_error("60"),
        "header-5000": _header_error("5000"),
        "header-0": _header_error("0"),
        "header-garbage": _header_error("soon"),
        "header-negative": _header_error("-9"),
        "header-empty": _header_error(""),
        "body-68400": _body_error(68400),
        "body-0": _body_error(0),
        "body-garbage": _body_error("later"),
        "body-negative": _body_error(-5),
        "none": _bare_error(),
    }


def _rate_limited(retry_after: float | None) -> ExecutionFailure:
    return ExecutionFailure(
        kind=FailureKind.RATE_LIMIT,
        status_code=429,
        message="Rate limited.",
        retryable=True,
        retry_after_seconds=retry_after,
    )


def _out_of_credits(cooldown: float) -> ExecutionFailure:
    return ExecutionFailure(
        kind=FailureKind.QUOTA,
        status_code=402,
        message="Provider account is out of credits. (insufficient credits)",
        retryable=False,
        retry_after_seconds=cooldown,
    )


def _pool(
    keys: int = 2,
    *,
    cooldown: RateLimitCooldown | None = None,
    rate_limit_seconds: float = 60.0,
    model_bench_escalation: int = 2,
) -> CredentialRotationState:
    return CredentialRotationState(
        keys,
        "round_robin",
        rate_limit_seconds=rate_limit_seconds,
        lockout_tiers=LOCKOUT_TIERS,
        model_bench_escalation=model_bench_escalation,
        cooldown=cooldown,
    )


# -- The bar: defaults are 7.21.0, exactly -------------------------------


def test_the_defaults_answer_exactly_what_7_21_0_answered() -> None:
    """The golden was generated from a detached worktree at v7.21.0."""

    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    measured = {
        name: {
            "published": retry_after_from_error(exc),
            "cooldown_default": rate_limit_cooldown_seconds(exc),
            "cooldown_7s": rate_limit_cooldown_seconds(exc, 7.0),
            "cooldown_0s": rate_limit_cooldown_seconds(exc, 0.0),
        }
        for name, exc in _cases().items()
    }
    assert measured == baseline


def test_the_shipped_policy_is_the_one_the_settings_default_to() -> None:
    settings = Settings.model_validate({})
    assert settings.rate_limit_cooldown_mode == "provider"
    assert settings.rate_limit_cooldown_max_seconds == 3600.0
    assert ProviderConfig(api_key="k", base_url="https://x.invalid/v1")
    config = ProviderConfig(
        api_key="k",
        base_url="https://x.invalid/v1",
        rate_limit_cooldown_seconds=settings.rate_limit_cooldown_seconds,
        rate_limit_cooldown_mode=settings.rate_limit_cooldown_mode,
        rate_limit_cooldown_max_seconds=settings.rate_limit_cooldown_max_seconds,
    )
    assert config.rate_limit_cooldown() == DEFAULT_RATE_LIMIT_COOLDOWN
    # And a config nobody configured -- a provider built by hand, a test --
    # carries the same policy, so nothing has to know about the settings.
    assert (
        ProviderConfig(
            api_key="k", base_url="https://x.invalid/v1"
        ).rate_limit_cooldown()
        == DEFAULT_RATE_LIMIT_COOLDOWN
    )


# -- The ceiling ----------------------------------------------------------


@pytest.mark.parametrize(
    ("max_seconds", "expected"),
    [(3600.0, 3600.0), (0.0, 5000.0), (120.0, 120.0)],
)
def test_the_ceiling_on_a_header_is_the_operators(max_seconds, expected) -> None:
    policy = RateLimitCooldown(max_seconds=max_seconds)
    error = _header_error("5000")
    assert retry_after_from_error(error, cooldown=policy) == expected
    assert rate_limit_cooldown_seconds(error, cooldown=policy) == expected


def test_the_ceiling_does_not_touch_a_wait_published_in_the_body() -> None:
    """The decision, pinned: lowering the header ceiling must not re-create
    hammering a host that already said "not until midnight" (the 7.6.3
    defect). A body-stated reset keeps its own one-day bound; an operator who
    wants it ignored says so with ``fixed`` or ``off``."""

    error = _body_error(68400)
    for max_seconds in (3600.0, 120.0, 0.0):
        policy = RateLimitCooldown(max_seconds=max_seconds)
        assert rate_limit_cooldown_seconds(error, cooldown=policy) == 68400.0
    absurd = _body_error(999_999)
    assert (
        rate_limit_cooldown_seconds(absurd, cooldown=RateLimitCooldown(max_seconds=120))
        == MAX_HOST_STATED_COOLDOWN_SECONDS
    )
    assert RateLimitCooldown(mode="fixed", fallback_seconds=7).resolve(68400.0) == 7.0
    assert RateLimitCooldown(mode="off").resolve(68400.0) == 0.0


# -- The mode -------------------------------------------------------------


def test_the_three_modes_are_the_only_three() -> None:
    assert RATE_LIMIT_COOLDOWN_MODES == ("provider", "fixed", "off")
    with pytest.raises(ValueError, match="Unknown rate-limit cooldown mode"):
        Settings.model_validate({"RATE_LIMIT_COOLDOWN_MODE": "sometimes"})
    assert (
        Settings.model_validate(
            {"RATE_LIMIT_COOLDOWN_MODE": "  OFF "}
        ).rate_limit_cooldown_mode
        == "off"
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("provider", 15.0), ("fixed", 7.0), ("off", 0.0)],
)
def test_the_mode_decides_the_key_bench(mode, expected) -> None:
    pool = _pool(
        cooldown=RateLimitCooldown(mode=mode, fallback_seconds=7.0),
        rate_limit_seconds=7.0,
        model_bench_escalation=1,
    )
    rotated = asyncio.run(pool.report_failure(0, _rate_limited(15.0)))
    slot = pool.get_metrics()[0]
    assert rotated is True
    assert round(slot["cooldown_remaining"]) == expected
    assert slot["state"] == (
        PoolHealthState.HEALTHY.name if mode == "off" else PoolHealthState.COOLDOWN.name
    )


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("provider", 15.0), ("fixed", 7.0), ("off", 0.0)],
)
def test_the_mode_decides_the_model_scoped_bench(mode, expected) -> None:
    pool = _pool(
        cooldown=RateLimitCooldown(mode=mode, fallback_seconds=7.0),
        rate_limit_seconds=7.0,
    )
    asyncio.run(pool.report_failure(0, _rate_limited(15.0), model="a-model"))
    benches = pool.get_metrics()[0]["model_benches"]
    if mode == "off":
        assert benches == []
    else:
        assert [b["model"] for b in benches] == ["a-model"]
        assert round(benches[0]["remaining"]) == expected


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("provider", 7.0), ("fixed", 7.0), ("off", 0.0)],
)
def test_the_mode_decides_the_credits_bench(mode, expected) -> None:
    """The QUOTA bench rides the same fixed window, so it follows the mode."""

    pool = _pool(
        cooldown=RateLimitCooldown(mode=mode, fallback_seconds=7.0),
        rate_limit_seconds=7.0,
    )
    asyncio.run(pool.report_failure(0, _out_of_credits(7.0)))
    slot = pool.get_metrics()[0]
    assert round(slot["cooldown_remaining"]) == expected


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("provider", 15.0), ("fixed", 7.0), ("off", 0.0)],
)
def test_the_mode_decides_the_probe_escalation(mode, expected) -> None:
    pool = _pool(
        cooldown=RateLimitCooldown(mode=mode, fallback_seconds=7.0),
        rate_limit_seconds=7.0,
    )
    benched = asyncio.run(pool.escalate_to_key_bench(0, "a-model", 15.0))
    assert round(benched) == expected


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("provider", 15.0), ("fixed", 7.0), ("off", 0.0)],
)
def test_the_mode_decides_the_reactive_block(mode, expected) -> None:
    """The provider-wide pause installed by classification, not the pool's."""

    marks: list[float] = []
    failure = classify_provider_failure(
        _header_error("15"),
        provider_name="upstream",
        read_timeout_s=None,
        request_id=None,
        mark_rate_limited=marks.append,
        cooldown=RateLimitCooldown(mode=mode, fallback_seconds=7.0),
    )
    assert failure.kind is FailureKind.RATE_LIMIT
    assert marks == ([] if expected == 0.0 else [expected])


def test_off_installs_no_retry_backoff_block_either() -> None:
    """The limiter's own 429 pause is a bench the whole provider serves."""

    async def _always_429() -> None:
        raise _header_error("15")

    installed: dict[str, list[float]] = {}
    for mode in ("provider", "off"):
        limiter = ProviderRateLimiter(
            max_retries=1,
            backoff_base_seconds=0.01,
            backoff_max_seconds=0.01,
            backoff_jitter_seconds=0.0,
            cooldown=RateLimitCooldown(mode=mode),
        )
        blocks: list[float] = []
        installed[mode] = blocks
        with (
            patch.object(limiter, "extend_reactive_block", blocks.append),
            pytest.raises(httpx.HTTPStatusError),
        ):
            asyncio.run(limiter.execute_with_retry(_always_429))

    # The retry itself is untouched: both modes tried twice and both raised.
    assert len(installed["provider"]) == 1
    assert installed["off"] == []


def test_off_still_rotates_and_still_offers_every_key() -> None:
    """Rotation and health are separate axes; ``off`` moves only the second."""

    pool = _pool(cooldown=RateLimitCooldown(mode="off"))
    first = asyncio.run(pool.acquire())
    assert asyncio.run(pool.report_failure(first, _rate_limited(3600.0))) is True
    second = asyncio.run(pool.acquire())
    assert second != first
    assert asyncio.run(pool.report_failure(second, _rate_limited(3600.0))) is True
    # Both keys 429'd and both are still healthy and still selectable.
    assert [slot["state"] for slot in pool.get_metrics()] == [
        PoolHealthState.HEALTHY.name,
        PoolHealthState.HEALTHY.name,
    ]
    assert asyncio.run(pool.acquire()) in (0, 1)


def test_off_does_not_claim_a_bench_in_the_root_cause_sentence() -> None:
    """The stored sentence must not say a key was benched when none was."""

    trace = install_ladder_trace()
    try:
        record_upstream_try(key_index=0, key_label="k0", status=429, retry_after=3600.0)
        record_upstream_try(key_index=1, key_label="k1", status=429, retry_after=3600.0)
        pool = _pool(cooldown=RateLimitCooldown(mode="off"))
        asyncio.run(pool.report_failure(0, _rate_limited(3600.0), model="a-model"))
        payload = ladder_payload(trace.slot())
        sentence = ladder_root_cause(payload)
    finally:
        _LADDER.set(None)

    decision = payload["credentials"][0]
    assert decision["class"] is None
    assert decision.get("benched_for_s") is None
    assert "RATE_LIMIT_COOLDOWN_MODE=off, nothing benched" in decision["reason"]
    assert "benched" not in sentence.replace("nothing benched", "")
    assert "RATE_LIMIT_COOLDOWN_MODE=off" in sentence


# -- The two settings together --------------------------------------------


def test_a_zero_cooldown_still_honours_a_wait_the_host_published() -> None:
    """The existing meaning of 0 is kept: no bench when nothing was said."""

    policy = RateLimitCooldown(fallback_seconds=0.0)
    assert rate_limit_cooldown_seconds(_header_error("15"), cooldown=policy) == 15.0
    assert rate_limit_cooldown_seconds(_bare_error(), cooldown=policy) == 0.0


def test_the_engines_own_fixed_mode_is_a_different_axis() -> None:
    """``PROVIDER_TUNING.rate_limit_mode`` picks the *shape* of the window --
    a flat bench rather than the generic ladder -- and the operator's mode
    picks the *number* that fills it. A global ``fixed`` must not silently
    rewrite a pool whose tuning already says fixed, so it does not touch the
    tuning at all."""

    from my_claude_code.core.credential_rotation import PROVIDER_TUNING

    assert PROVIDER_TUNING.rate_limit_mode == "fixed"
    for mode in RATE_LIMIT_COOLDOWN_MODES:
        pool = _pool(cooldown=RateLimitCooldown(mode=mode))
        assert pool._engine._tuning.rate_limit_mode == "fixed"
        assert (
            pool._engine._tuning.rate_limit_max_seconds
            == MAX_HOST_STATED_COOLDOWN_SECONDS
        )
