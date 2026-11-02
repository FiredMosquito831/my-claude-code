"""8.6.0: the automatic refresh's arithmetic, and the per-provider backoff.

The user's instruction (2026-11-02) is that the Usage page reads and updates
every configured provider **every 30 seconds**, with that interval on Limits &
Resilience. A 30-second default is only safe because of three properties, and
this file proves the two that are arithmetic rather than UI:

1. **The interval is the operator's, clamped and reversible.** A configured
   number is what the gate uses; the floor and ceiling are the declared
   constants; **0 turns it off** and reverts to 8.5.0's 30-minute on-demand
   floor -- because "on demand only" must not mean a held-down button becomes a
   poll.
2. **A failing provider backs off on its own.** Consecutive failures double its
   wait up to a cap, one success puts it back on the operator's interval, and
   **one provider's failures never lengthen another's wait**.

The third -- that no amount of polling can charge a key or bench a provider --
is a property of the transport rather than of this arithmetic, and it is proved
against a fake host in ``tests/api/test_admin_usage_reads.py``: the poller calls
the same route, so it inherits that proof rather than needing a second one.
"""

import pytest

from my_claude_code.api.admin_usage_routes import (
    BACKOFF_CAP_SECONDS,
    MINIMUM_INTERVAL_SECONDS,
    _interval_for,
    _next_read_at,
)
from my_claude_code.config.constants import (
    USAGE_AUTO_REFRESH_MAX_SECONDS,
    USAGE_AUTO_REFRESH_MIN_SECONDS,
    USAGE_AUTO_REFRESH_SECONDS_DEFAULT,
)
from my_claude_code.core.usage.reading import (
    MAX_BACKOFF_DOUBLINGS,
    READINGS,
    ReadRecord,
    backoff_seconds,
)


class _Settings:
    """Just the one attribute this route reads, so a test states its own value."""

    def __init__(self, seconds: object) -> None:
        self.usage_auto_refresh_seconds = seconds


# ------------------------------------------------------------------- the default


def test_the_shipped_default_is_thirty_seconds() -> None:
    """The operator's own number, not an opinion of mine."""

    assert USAGE_AUTO_REFRESH_SECONDS_DEFAULT == 30.0
    assert _interval_for(_Settings(USAGE_AUTO_REFRESH_SECONDS_DEFAULT)) == 30


def test_a_configured_interval_is_used_as_given() -> None:
    assert _interval_for(_Settings(15)) == 15
    assert _interval_for(_Settings(600)) == 600


def test_a_configured_interval_is_clamped_to_the_declared_bounds() -> None:
    assert _interval_for(_Settings(1)) == USAGE_AUTO_REFRESH_MIN_SECONDS
    assert _interval_for(_Settings(0.5)) == USAGE_AUTO_REFRESH_MIN_SECONDS
    assert _interval_for(_Settings(10_000)) == USAGE_AUTO_REFRESH_MAX_SECONDS


def test_zero_turns_the_automatic_refresh_off_and_restores_the_on_demand_floor() -> (
    None
):
    """0 is a real value: the page goes back to 8.5.0's behaviour exactly."""

    assert _interval_for(_Settings(0)) == MINIMUM_INTERVAL_SECONDS
    assert MINIMUM_INTERVAL_SECONDS == 30 * 60


def test_a_missing_or_nonsense_setting_falls_back_to_the_safe_floor() -> None:
    """A settings snapshot without the field must never become "poll every 0 s"."""

    assert _interval_for(_Settings(None)) == MINIMUM_INTERVAL_SECONDS
    assert _interval_for(_Settings("thirty")) == MINIMUM_INTERVAL_SECONDS
    assert _interval_for(object()) == MINIMUM_INTERVAL_SECONDS


# ------------------------------------------------------------------- the backoff


def test_a_healthy_provider_is_asked_at_the_operators_interval() -> None:
    assert backoff_seconds(30, 0, cap=BACKOFF_CAP_SECONDS) == 30


def test_each_consecutive_failure_doubles_that_providers_wait() -> None:
    assert backoff_seconds(30, 1, cap=BACKOFF_CAP_SECONDS) == 60
    assert backoff_seconds(30, 2, cap=BACKOFF_CAP_SECONDS) == 120
    assert backoff_seconds(30, 3, cap=BACKOFF_CAP_SECONDS) == 240
    assert backoff_seconds(30, 4, cap=BACKOFF_CAP_SECONDS) == 480


def test_the_backoff_is_capped_rather_than_growing_without_end() -> None:
    """A host down for a week has the same wait as one down for an hour.

    The point is to stop a broken host being asked every 30 seconds, not to
    give up on it: at the cap MCC still tries, just rarely.
    """

    assert backoff_seconds(30, MAX_BACKOFF_DOUBLINGS, cap=BACKOFF_CAP_SECONDS) == min(
        BACKOFF_CAP_SECONDS, 30 * 2**MAX_BACKOFF_DOUBLINGS
    )
    assert backoff_seconds(30, 500, cap=BACKOFF_CAP_SECONDS) == min(
        BACKOFF_CAP_SECONDS, 30 * 2**MAX_BACKOFF_DOUBLINGS
    )
    # And the cap is the setting's own ceiling: one broken host is never asked
    # more rarely than the rarest legitimate configuration.
    assert BACKOFF_CAP_SECONDS == USAGE_AUTO_REFRESH_MAX_SECONDS


def test_a_success_puts_a_provider_back_on_the_operators_interval() -> None:
    assert backoff_seconds(30, 0, cap=BACKOFF_CAP_SECONDS) == 30


# --------------------------------------------------- the streak the store keeps


@pytest.fixture(autouse=True)
def _clean_store():
    READINGS.reset()
    yield
    READINGS.reset()


def _streak(provider_id: str) -> int:
    """The failures the store holds for one provider.

    A helper rather than a bare ``record_for(...).consecutive_failures`` at each
    assertion: ``record_for`` is honest about returning ``None`` for a provider
    nothing has been recorded for, and a test that asserted through that union
    would be one typo away from a confusing ``AttributeError``.
    """

    record = READINGS.record_for(provider_id)
    assert record is not None, provider_id
    return record.consecutive_failures


def test_a_failure_extends_the_streak_and_a_success_clears_it() -> None:
    READINGS.claim("deepseek", now=1000.0, interval=30)
    READINGS.finish("deepseek", None, ReadRecord(attempted_at=1000.0, error="boom"))
    assert _streak("deepseek") == 1

    READINGS.claim("deepseek", now=1060.0, interval=30)
    READINGS.finish("deepseek", None, ReadRecord(attempted_at=1060.0, error="boom"))
    assert _streak("deepseek") == 2

    from my_claude_code.core.usage.reading import Reading

    READINGS.claim("deepseek", now=1180.0, interval=30)
    READINGS.finish(
        "deepseek", Reading(fetched_at=1180.0), ReadRecord(attempted_at=1180.0)
    )
    assert _streak("deepseek") == 0


def test_a_credential_refusal_counts_too() -> None:
    """`not_sent` is still a failure: a removed key does not fix itself."""

    READINGS.claim("kimi", now=1000.0, interval=30)
    READINGS.finish(
        "kimi",
        None,
        ReadRecord(attempted_at=1000.0, error="KIMI_API_KEY is not set", not_sent=True),
    )
    assert _streak("kimi") == 1


def test_each_provider_keeps_its_own_streak() -> None:
    """One provider's failures never lengthen another's wait."""

    READINGS.claim("deepseek", now=1000.0, interval=30)
    READINGS.finish("deepseek", None, ReadRecord(attempted_at=1000.0, error="boom"))
    READINGS.claim("deepseek", now=1060.0, interval=30)
    READINGS.finish("deepseek", None, ReadRecord(attempted_at=1060.0, error="boom"))

    READINGS.claim("open_router", now=1000.0, interval=30)
    READINGS.finish(
        "open_router", None, ReadRecord(attempted_at=1000.0, error="different boom")
    )

    assert _streak("deepseek") == 2
    assert _streak("open_router") == 1


# ------------------------------------------------ next_read_in_seconds, per row


def test_next_read_in_seconds_is_the_interval_when_the_provider_is_healthy() -> None:
    record = ReadRecord(attempted_at=1000.0)
    assert _next_read_at(record, 1000.0, 30) == 30
    assert _next_read_at(record, 1015.0, 30) == 15
    # Past it: "now", which the page reads as "may be asked".
    assert _next_read_at(record, 1031.0, 30) is None


def test_next_read_in_seconds_reflects_that_providers_failures() -> None:
    record = ReadRecord(attempted_at=1000.0, error="boom", consecutive_failures=2)
    assert _next_read_at(record, 1000.0, 30) == 120


def test_the_providers_own_retry_after_wins_when_it_is_later() -> None:
    """A 429 is a fact about the host; the interval cannot shorten it.

    The rule is "the later of the two wins", not "the host beats everything":
    a 15-minute ``Retry-After`` on a 30-minute interval is still a 30-minute
    wait, because MCC's own interval is the floor it promised the provider.
    What the host's number must never do is be *shortened*, which is the half
    that matters -- a 429 saying "wait 15 minutes" is not permission to retry
    after 30 seconds.
    """

    record = ReadRecord(
        attempted_at=1000.0, error="rate limited", retry_at=1000.0 + 900
    )
    # 900 s of host wait against a 30 s interval: the host's wait wins.
    assert _next_read_at(record, 1000.0, 30) == 900
    # ... and against a 1800 s interval, MCC's own floor is the longer one.
    assert _next_read_at(record, 1000.0, 1800) == 1800
    # The half that matters: the host's wait is never shortened to the
    # interval, whatever the interval is.
    for interval in (10, 30, 120, 600, 899):
        wait = _next_read_at(record, 1000.0, interval)
        assert wait is not None and wait >= 900, interval


def test_a_provider_never_read_has_no_wait_at_all() -> None:
    assert _next_read_at(None, 1000.0, 30) is None
    assert _next_read_at(ReadRecord(), 1000.0, 30) is None
