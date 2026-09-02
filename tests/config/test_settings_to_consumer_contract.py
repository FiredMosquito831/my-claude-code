"""A setting the form can edit must reach the code that acts on it.

Each of these knobs replaced a hardcoded literal. A literal that becomes a
`Settings` field and a manifest entry but never reaches its consumer is worse
than the literal was: the dashboard shows a number, saving it restarts the
server, and nothing changes. One test per hop, from `Settings` to the object
that reads the value.
"""

from unittest.mock import patch

import pytest

from my_claude_code.application.execution import route_execution_policy
from my_claude_code.config.provider_catalog import PROVIDER_CATALOG
from my_claude_code.config.settings import Settings, parse_lockout_tiers
from my_claude_code.core.credential_rotation import PROVIDER_TUNING
from my_claude_code.core.failures import FailureKind
from my_claude_code.core.rate_limit import MAX_RATE_LIMIT_COOLDOWN_SECONDS
from my_claude_code.providers.nvidia_nim import NvidiaNimProvider
from my_claude_code.providers.runtime.config import build_provider_config
from my_claude_code.providers.runtime.factory import create_provider
from my_claude_code.providers.runtime.rotating import RotatingProvider


def _settings(**overrides) -> Settings:
    return Settings(**overrides)


def test_the_backoff_and_lockout_settings_reach_the_provider_config() -> None:
    config = build_provider_config(
        PROVIDER_CATALOG["nvidia_nim"],
        _settings(
            nvidia_nim_api_key="k1",
            PROVIDER_RETRY_BACKOFF_BASE_SECONDS=3.5,
            PROVIDER_RETRY_BACKOFF_MAX_SECONDS=99.0,
            PROVIDER_RETRY_BACKOFF_JITTER_SECONDS=0.25,
            CREDENTIAL_LOCKOUT_TIERS="1,2,3",
        ),
    )

    assert config.retry_backoff_base_seconds == 3.5
    assert config.retry_backoff_max_seconds == 99.0
    assert config.retry_backoff_jitter_seconds == 0.25
    assert config.lockout_tiers == (1.0, 2.0, 3.0)


def test_the_backoff_settings_reach_the_limiter_that_schedules_retries() -> None:
    provider = create_provider(
        "nvidia_nim",
        _settings(
            nvidia_nim_api_key="k1",
            PROVIDER_RETRY_BACKOFF_BASE_SECONDS=3.5,
            PROVIDER_RETRY_BACKOFF_MAX_SECONDS=99.0,
            PROVIDER_RETRY_BACKOFF_JITTER_SECONDS=0.25,
        ),
    )

    assert isinstance(provider, NvidiaNimProvider)
    limiter = provider._rate_limiter
    assert limiter._backoff_base_seconds == 3.5
    assert limiter._backoff_max_seconds == 99.0
    assert limiter._backoff_jitter_seconds == 0.25


@pytest.mark.asyncio
async def test_the_lockout_ladder_and_429_window_reach_the_credential_pool() -> None:
    provider = create_provider(
        "nvidia_nim",
        _settings(
            nvidia_nim_api_key="k1,k2",
            NVIDIA_NIM_API_KEY_ROTATION="round_robin",
            CREDENTIAL_LOCKOUT_TIERS="11,22",
            RATE_LIMIT_COOLDOWN_SECONDS=44.0,
        ),
    )

    assert isinstance(provider, RotatingProvider)
    tuning = provider._state._engine.tuning
    assert tuning.lockout_tiers == (11.0, 22.0)
    assert tuning.rate_limit_seconds == 44.0


def test_the_step_over_floor_reaches_the_route_policy() -> None:
    policy = route_execution_policy(_settings(FALLBACK_COOLDOWN_STEP_OVER_FLOOR=12.0))
    assert policy.cooldown_step_over_floor == 12.0


def test_the_attempt_share_floor_reaches_the_route_policy() -> None:
    """The one hop between the operator's number and the code that divides.

    A setting nothing reads looks identical to a setting that works, right up
    until a silent model is cut at 75s again.
    """
    policy = route_execution_policy(_settings(FALLBACK_ATTEMPT_SHARE_FLOOR=210.0))
    assert policy.attempt_share_floor == 210.0

    # Ships 0 since 6.16.0: no shipped deadline, so nothing to floor.
    assert route_execution_policy(_settings()).attempt_share_floor == 0.0
    assert (
        route_execution_policy(
            _settings(FALLBACK_ATTEMPT_SHARE_FLOOR=0.0)
        ).attempt_share_floor
        == 0.0
    )


def test_the_429_bench_is_capped_by_the_same_bound_a_header_is() -> None:
    """A hostile Retry-After cannot bench a key past the one-hour sanity cap.

    The cap lives in ``core.rate_limit`` and is applied in two places -- when a
    header is parsed and when the pool benches a slot. Pinning the identity
    keeps the two from drifting into disagreeing about what "too long" means.
    """
    assert PROVIDER_TUNING.rate_limit_max_seconds == MAX_RATE_LIMIT_COOLDOWN_SECONDS


def test_a_removed_env_key_is_ignored_rather_than_fatal() -> None:
    """An existing ``.env`` still carrying CREDENTIAL_CIRCUIT_THRESHOLD must start.

    The breaker it configured is gone. ``Settings`` is declared with
    ``extra="ignore"``, so the stale line is inert -- but the whole point of
    removing a key is that nobody has to edit a file for the upgrade, so this
    is pinned rather than assumed.
    """
    with patch.dict("os.environ", {"CREDENTIAL_CIRCUIT_THRESHOLD": "3"}, clear=False):
        settings = _settings()
    assert not hasattr(settings, "credential_circuit_threshold")


@pytest.mark.parametrize("value", ["", "0", "-5", "abc", "300,nope", "300,0"])
def test_a_lockout_ladder_that_cannot_be_walked_is_refused(value: str) -> None:
    with pytest.raises(ValueError):
        parse_lockout_tiers(value)


def test_a_bad_lockout_ladder_is_refused_at_load() -> None:
    with pytest.raises(ValueError, match="CREDENTIAL_LOCKOUT_TIERS"):
        _settings(CREDENTIAL_LOCKOUT_TIERS="300,nope")


def test_quota_is_a_skip_kind_an_operator_can_choose_but_never_inherits() -> None:
    """The new kind round-trips settings -> policy, and is absent by default.

    An account out of credits ends nothing by default: the next key, then the
    next model, is exactly what a chain is for. An operator who wants the old
    abort can still ask for it, and the validator has to accept the name.
    """
    default = route_execution_policy(_settings())
    assert FailureKind.QUOTA not in default.skip_kinds

    chosen = route_execution_policy(_settings(FALLBACK_SKIP_KINDS="quota"))
    assert chosen.skip_kinds == frozenset({FailureKind.QUOTA})
