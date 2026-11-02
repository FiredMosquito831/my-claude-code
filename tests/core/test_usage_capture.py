"""The generic usage observer: allow-list, verbatim values, and never raising.

``specs/INVESTIGATION-USAGE-AND-CREDITS.md`` §7 PR 2. This module is the third
observer in the codebase, and the contract it inherits is the one its two
siblings state in their own docstrings: **verbatim or absent, never computed**.
These tests pin that contract, the allow-lists as shipped, and the two
properties that make it safe to put a callback in the read path of every
opted-in provider:

* a name that is **not** on the allow-list is not stored, so forgetting to
  extend a list costs diagnostics rather than storing whatever an edge added;
* a value the observer cannot read is **absent**, never a zero, never an empty
  string, and it never raises -- a usage figure is never a reason to fail a
  request.

The allow-list contents are asserted rather than trusted to a comment, because
a header name is exactly the kind of thing that is silently dropped by a typo.
"""

import pytest

from my_claude_code.config.usage_sources import (
    MECHANISM_ENDPOINT,
    MECHANISM_HEADER,
    PROVIDER_SOURCES,
    SOURCE_UNSUPPORTED,
    header_capture_providers,
    source_for,
)
from my_claude_code.core.usage_capture import (
    GENERIC_RATE_LIMIT_HEADERS,
    MAX_VALUE_CHARS,
    NOUS_CREDITS_HEADERS,
    USAGE_HEADER_ALLOW_LIST,
    UsageHeaderObserver,
    capture_usage_headers,
)


def _nous_headers() -> dict[str, str]:
    """A realistic Nous portal response: several credits headers, as it sends."""

    return {
        "x-nous-credits-remaining-micros": "142500000",
        "x-nous-credits-remaining-usd": "142.5",
        "x-nous-credits-subscription-micros": "20000000",
        "x-nous-credits-subscription-usd": "20.00",
        "x-nous-credits-subscription-limit-micros": "6000000",
        "x-nous-credits-subscription-limit-usd": "6.00",
        "x-nous-credits-rollover-micros": "0",
        "x-nous-credits-purchased-micros": "15000000",
        "x-nous-credits-purchased-usd": "15.00",
        "x-nous-credits-as-of-ms": "1793600000123",
        "x-nous-credits-paid-access": "true",
        "x-nous-credits-version": "1",
        "x-nous-credits-window-gauges": '{"5h":0.38,"weekly":0.11}',
        "x-nous-tool-pool-micros": "0",
        "x-nous-tool-pool-gated-off": "false",
    }


def _ratelimit_headers() -> dict[str, str]:
    """The de-facto ``x-ratelimit-*`` family as OpenAI-shaped hosts send it."""

    return {
        "x-ratelimit-limit-requests": "5000",
        "x-ratelimit-limit-tokens": "2000000",
        "x-ratelimit-remaining-requests": "4997",
        "x-ratelimit-remaining-tokens": "1998000",
        "x-ratelimit-reset-requests": "1s",
        "x-ratelimit-reset-tokens": "6m0s",
    }


# ------------------------------------------------------------- the allow-lists


def test_nous_family_is_exactly_the_documented_set() -> None:
    """The Nous names, as ``hermes-agent``'s ``credits_tracker.py`` spells them.

    Every name here was read out of that module's own composition
    (``"x-nous-" + ("credits-" | "tool-") + field``), not extrapolated from a
    pattern -- which is the difference between an allow-list and a wildcard.
    """

    assert (
        frozenset(
            {
                "x-nous-credits-version",
                "x-nous-credits-denominator-kind",
                "x-nous-credits-remaining-micros",
                "x-nous-credits-remaining-usd",
                "x-nous-credits-subscription-micros",
                "x-nous-credits-subscription-usd",
                "x-nous-credits-subscription-limit-micros",
                "x-nous-credits-subscription-limit-usd",
                "x-nous-credits-rollover-micros",
                "x-nous-credits-purchased-micros",
                "x-nous-credits-purchased-usd",
                "x-nous-credits-as-of-ms",
                "x-nous-credits-paid-access",
                "x-nous-credits-disabled-reason",
                "x-nous-credits-spend-source",
                "x-nous-credits-window-gauges",
                "x-nous-credits-window-paused-reason",
                "x-nous-credits-keep-going-on-credits",
                "x-nous-tool-pool-micros",
                "x-nous-tool-pool-gated-off",
            }
        )
        == NOUS_CREDITS_HEADERS
    )


def test_every_allow_listed_name_is_lower_case_and_prefix_shaped() -> None:
    """A name that is not lower-case can never match: httpx lower-cases.

    And a name outside the two declared prefixes is a mistake by construction --
    this observer captures exactly two families, so a third spelling arriving
    through a copy-paste is the failure this catches.
    """

    for name in USAGE_HEADER_ALLOW_LIST:
        assert name == name.lower(), name
        assert name == name.strip(), name
        assert name.startswith(("x-nous-", "x-ratelimit-", "ratelimit-")), name


def test_the_two_families_are_disjoint_but_both_inside_the_union() -> None:
    assert not (NOUS_CREDITS_HEADERS & GENERIC_RATE_LIMIT_HEADERS)
    assert NOUS_CREDITS_HEADERS | GENERIC_RATE_LIMIT_HEADERS == USAGE_HEADER_ALLOW_LIST


def test_retry_after_is_deliberately_not_captured() -> None:
    """It is an instruction about this request, not a figure about the account.

    Storing it as if it were a usage reading would be IV.16's defect in the
    other direction: a per-request courtesy printed where an account balance
    belongs.
    """

    assert "retry-after" not in USAGE_HEADER_ALLOW_LIST
    assert "retry-after-ms" not in USAGE_HEADER_ALLOW_LIST


# ------------------------------------------------------------------- capturing


def test_a_nous_response_captures_every_allow_listed_name_verbatim() -> None:
    """The fixture's values come back character for character."""

    headers = _nous_headers()
    captured = capture_usage_headers(headers)
    expected = {
        name: value for name, value in headers.items() if name in NOUS_CREDITS_HEADERS
    }
    assert captured == expected


def test_the_ratelimit_family_captures_verbatim() -> None:
    headers = _ratelimit_headers()
    assert capture_usage_headers(headers) == headers


def test_a_header_outside_the_allow_list_is_not_stored() -> None:
    """The load-bearing negative: the observer is an allow-list, not a dump.

    ``x-account-email`` and an echoed ``authorization`` are exactly the sort of
    thing an edge may add, and neither may become a stored value.
    """

    captured = capture_usage_headers(
        {
            "x-ratelimit-remaining-requests": "12",
            "x-account-email": "someone@example.com",
            "authorization": "Bearer secret",
            "x-nous-credits-unknown-field": "1",
            "set-cookie": "session=abc",
        }
    )
    assert captured == {"x-ratelimit-remaining-requests": "12"}


def test_names_match_case_insensitively_and_values_are_trimmed() -> None:
    """HTTP header names are case-insensitive; ``httpx`` keeps the server's case."""

    captured = capture_usage_headers(
        {"X-Nous-Credits-Remaining-USD": "  142.5  ", "X-RateLimit-Limit": " 9 "}
    )
    assert captured == {
        "x-nous-credits-remaining-usd": "142.5",
        "x-ratelimit-limit": "9",
    }


@pytest.mark.parametrize(
    "headers",
    [
        None,
        {},
        {"x-nous-credits-remaining-usd": ""},
        {"x-nous-credits-remaining-usd": " "},
    ],
)
def test_nothing_to_record_is_an_empty_mapping_not_a_zero(headers) -> None:
    """Absent stays absent: an empty value is not a measured zero (III.8)."""

    assert capture_usage_headers(headers) == {}


def test_a_non_string_value_is_dropped_rather_than_stringified() -> None:
    """``str(value)`` on an arbitrary object is a new value nobody sent.

    Cast deliberately: the declaration says ``Mapping[str, str]`` because that
    is what ``httpx`` hands back, and the point of this test is the branch that
    survives a caller who breaks that promise.
    """

    hostile: dict = {"x-nous-credits-remaining-usd": 142.5, "x-ratelimit-limit": None}
    assert capture_usage_headers(hostile) == {}


def test_a_value_longer_than_the_bound_is_cut_at_the_bound() -> None:
    long_value = "9" * (MAX_VALUE_CHARS + 500)
    captured = capture_usage_headers({"x-nous-credits-window-gauges": long_value})
    assert captured == {"x-nous-credits-window-gauges": long_value[:MAX_VALUE_CHARS]}


# -------------------------------------------------------------------- observer


def test_the_observer_keeps_one_snapshot_per_provider_and_account() -> None:
    board = UsageHeaderObserver()
    board.observe(
        {"x-nous-credits-remaining-usd": "1"},
        status_code=200,
        provider_id="nous_portal",
        now=100.0,
    )
    board.observe(
        {"x-ratelimit-remaining-requests": "7"},
        status_code=200,
        provider_id="open_router",
        now=200.0,
    )
    assert board.latest_for("nous_portal") is not None
    assert board.latest_for("open_router") is not None
    # One provider's headers must never answer another provider's card.
    nous = board.latest_for("nous_portal")
    assert nous is not None
    assert nous.values == {"x-nous-credits-remaining-usd": "1"}
    assert board.latest_for("deepseek") is None


def test_the_same_provider_with_two_accounts_keeps_them_apart() -> None:
    """The argument both sibling observers make, at the same layer."""

    board = UsageHeaderObserver()
    board.observe(
        {"x-ratelimit-remaining-requests": "1"},
        status_code=200,
        provider_id="open_router",
        account_id="acc-a",
        now=1.0,
    )
    board.observe(
        {"x-ratelimit-remaining-requests": "2"},
        status_code=200,
        provider_id="open_router",
        account_id="acc-b",
        now=2.0,
    )
    account_a = board.latest_for("open_router", "acc-a")
    account_b = board.latest_for("open_router", "acc-b")
    assert account_a is not None
    assert account_b is not None
    assert account_a.values == {"x-ratelimit-remaining-requests": "1"}
    assert account_b.values == {"x-ratelimit-remaining-requests": "2"}


def test_a_response_with_no_allow_listed_header_does_not_erase_a_reading() -> None:
    """The difference between "the host said nothing" and "the host said 40 ago".

    A response that carries none of the names is not an observation at all, so
    it must not blank the snapshot a real reading left behind.
    """

    board = UsageHeaderObserver()
    board.observe(
        {"x-nous-credits-remaining-usd": "5"},
        status_code=200,
        provider_id="nous_portal",
        now=10.0,
    )
    board.observe(
        {"content-type": "text/event-stream"},
        status_code=200,
        provider_id="nous_portal",
        now=20.0,
    )
    snapshot = board.latest_for("nous_portal")
    assert snapshot is not None
    assert snapshot.observed_at == 10.0
    assert snapshot.values == {"x-nous-credits-remaining-usd": "5"}


def test_the_observation_stamp_is_the_instant_passed_in() -> None:
    """``observed_at`` is when the provider's bytes arrived, not when read."""

    board = UsageHeaderObserver()
    board.observe(
        {"x-ratelimit-limit": "1"},
        status_code=429,
        provider_id="open_router",
        now=1793600000.5,
    )
    snapshot = board.latest_for("open_router")
    assert snapshot is not None
    assert snapshot.observed_at == 1793600000.5
    assert snapshot.status_code == 429


def test_latest_keys_names_every_provider_observed() -> None:
    board = UsageHeaderObserver()
    board.observe({"x-ratelimit-limit": "1"}, status_code=200, provider_id="a", now=1.0)
    board.observe({"x-ratelimit-limit": "1"}, status_code=200, provider_id="b", now=2.0)
    assert board.latest_keys() == (("a", ""), ("b", ""))


# ------------------------------------------------------------ the declared table


def test_only_declared_header_providers_opt_in() -> None:
    """The installer reads this, so it must name exactly the capture rows."""

    assert header_capture_providers() == (
        "nous_portal",
        "open_router",
        "deepseek",
        "kimi",
        "commandcode",
    )


def test_a_provider_with_no_mechanism_reports_unsupported() -> None:
    assert source_for("novita").source == SOURCE_UNSUPPORTED
    assert source_for("cline").source == SOURCE_UNSUPPORTED
    # And one MCC has never heard of -- every custom provider -- is the same.
    assert source_for("some_custom_provider").source == SOURCE_UNSUPPORTED
    assert source_for("some_custom_provider").supported is False


def test_a_provider_with_an_endpoint_is_not_unsupported() -> None:
    """'Not built yet' is not 'nothing to read' (IV.16).

    A provider whose only mechanism is a read of its own must not be reported
    with the "publishes no usage endpoint" sentence, which would be a claim
    that is not true.
    """

    opencode_go = source_for("opencode_go")
    assert opencode_go.supported is True
    assert opencode_go.source == MECHANISM_ENDPOINT
    assert MECHANISM_HEADER not in opencode_go.mechanisms


def test_a_provider_may_hold_both_mechanisms_and_reports_the_cheaper() -> None:
    """DeepSeek and OpenRouter send x-ratelimit headers *and* publish a balance.

    One row with two mechanisms rather than two rows, so the page never has to
    decide which of two entries wins.
    """

    for provider_id in ("open_router", "deepseek"):
        row = source_for(provider_id)
        assert row.mechanisms == (MECHANISM_HEADER, MECHANISM_ENDPOINT)
        assert row.source == MECHANISM_HEADER


def test_no_provider_id_appears_twice_in_the_declared_table() -> None:
    ids = [row.provider_id for row in PROVIDER_SOURCES]
    assert len(ids) == len(set(ids))
