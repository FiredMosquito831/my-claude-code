"""8.6.0: the Usage page's automatic refresh, proved in jsdom.

Scenario: ``usage_refresh`` (``tests/api/admin_jsdom_usage_refresh.mjs``),
driven through ``tests/api/admin_jsdom_harness.mjs``. The arithmetic behind it
is ``tests/core/test_usage_auto_refresh.py``; the wire behaviour is
``tests/api/test_admin_usage_auto_refresh.py``. This file is the page's own
half: the three decisions that make a 30-second default safe, measured on the
functions the product actually calls.

What it proves:

1. **The cadence is the server's.** ``auto_refresh_seconds: 30`` produces a
   30,000 ms poller; ``0`` produces none; a page that is not the active view
   produces none. No number is typed into ``admin.js``.
2. **Only the right providers are asked.** Configured, with a readable route,
   and past their own ``next_read_in_seconds``. A provider cooling down after a
   429 is skipped while the others carry on; one with no adapter and one this
   install never configured are never asked at all.
3. **One pass is one POST per due provider**, each carrying exactly that
   provider's id.
4. **A hidden tab asks nothing** -- the difference between a few hundred
   requests a day and tens of thousands.
5. **0 is off**, and the page is 8.5.0's: the Read now control and no timer.
"""

import os

import pytest

from tests.api.test_admin_static_jsdom import _run

ON_CI = os.environ.get("MCC_CI", "") not in ("", "0", "false")

pytestmark = pytest.mark.xdist_group("admin_static_jsdom")


@pytest.fixture(scope="module")
def refresh() -> dict:
    return _run(MCC_JSDOM_SCENARIO="usage_refresh")


def test_the_scenario_runs_clean(refresh: dict) -> None:
    assert refresh["fatal"] is None
    assert refresh["scenarioFatal"] is None
    assert refresh["scriptErrors"] == []
    assert refresh["consoleErrors"] == []
    if ON_CI:
        assert refresh["cadence"]["msAt30"] == 30000


# ------------------------------------------------------------------- the cadence


def test_thirty_seconds_is_a_thirty_second_poller(refresh: dict) -> None:
    """The user's number, in the units the browser schedules with."""

    cadence = refresh["cadence"]
    assert cadence["enabledAt30"] is True
    assert cadence["msAt30"] == 30000
    assert cadence["activeView"] == "usage"


def test_the_interval_is_not_typed_into_the_page(refresh: dict) -> None:
    """With the setting at 0 the poller is off -- so 30 came from the server.

    A constant in ``admin.js`` could not be turned off by a setting; this is
    the measurement that says the number travelled from
    ``USAGE_AUTO_REFRESH_SECONDS``.
    """

    assert refresh["off"]["enabled"] is False
    assert refresh["off"]["ms"] == 0


# ------------------------------------------------------------------- who is asked


def test_exactly_the_configured_readable_due_providers_are_asked(
    refresh: dict,
) -> None:
    assert refresh["due"]["dueNow"] == ["deepseek", "open_router"]


def test_a_provider_cooling_down_after_a_429_is_skipped(
    refresh: dict,
) -> None:
    """Its own ``next_read_in_seconds`` is what excludes it, not a guess here."""

    assert refresh["due"]["excluded"]["coolingDown"] is True


def test_a_provider_with_no_readable_route_is_never_asked(refresh: dict) -> None:
    assert refresh["due"]["excluded"]["noReadableRoute"] is True


def test_a_provider_this_install_never_configured_is_never_asked(
    refresh: dict,
) -> None:
    """The server reports a row for it; the page must still not contact it."""

    assert refresh["due"]["excluded"]["notConfigured"] is True


# --------------------------------------------------------------------- one pass


def test_one_pass_sends_one_post_per_due_provider(refresh: dict) -> None:
    poll = refresh["poll"]
    assert poll["clean"] is True
    assert poll["posts"] == ["deepseek", "open_router"]
    assert len(poll["posts"]) == len(refresh["due"]["dueNow"])


def test_one_pass_also_refreshes_the_pages_own_view(refresh: dict) -> None:
    """The GET is the page's picture; the POSTs are the readings.

    Both, in one pass, and the GET is one: two would be two answers to the same
    question arriving a millisecond apart.
    """

    gets = refresh["poll"]["gets"]
    assert gets.count("/admin/api/usage") == 1
    assert gets.count("/admin/api/usage/read") == 2


# ------------------------------------------------- back off per provider, not globally


def test_one_failing_provider_does_not_slow_the_others(refresh: dict) -> None:
    """The user's own requirement: "per-provider, not global".

    The per-provider half is the server's (`next_read_in_seconds`, computed
    from that provider's own failures). The risk this catches is the PAGE's:
    the poller it reuses doubles every wait after a dirty run, which would be a
    global backoff wearing a per-provider name. With one provider at three
    consecutive failures, the healthy one is still the one that gets asked.
    """

    backoff = refresh["perProviderBackoff"]
    assert backoff["dueBefore"] == ["open_router"]
    assert backoff["asked"] == ["open_router"]


def test_a_provider_failure_is_not_a_dirty_pass(refresh: dict) -> None:
    """A per-provider failure is already accounted for by the server.

    Reporting it as dirty would make ``createPoller`` slow down every provider
    on the page because one of them was down -- the opposite of what was asked.
    """

    assert refresh["perProviderBackoff"]["clean"] is True


# ------------------------------------------------------------------- a hidden tab


def test_a_hidden_tab_asks_nothing_at_all(refresh: dict) -> None:
    """The single property that makes 30 seconds affordable.

    At 30 s a visible tab is ~2,880 reads a day per provider. A hidden one that
    kept polling would keep those requests coming in a background tab nobody is
    looking at; `createPoller` stops, and this measures it.
    """

    assert refresh["hidden"]["asked"] == 0
    assert refresh["hidden"]["clean"] is True


# ------------------------------------------------------------------- 0 is off


def test_zero_leaves_the_page_on_its_read_now_button(refresh: dict) -> None:
    off = refresh["off"]
    assert off["enabled"] is False
    assert off["due"] == []
    # The control is still there: off means "only when I press", not "no way to
    # ask".
    assert off["readNowButtons"] >= 1
    assert off["buttonText"].startswith("Read now")
